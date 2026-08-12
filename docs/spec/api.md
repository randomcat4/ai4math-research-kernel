# ResearchKernel v1 物理接口

状态：`NORMATIVE_V1`

## 1. 进程内接口

```python
class ResearchKernel:
    def create(self, request: CreateRequest, capability: VerifiedCapability) -> RunHandle: ...
    def apply(self, request: ApplyRequest, capability: VerifiedCapability) -> CommandReceipt: ...
    def inspect(self, run_id: str, after_cursor: int | None, limit: int) -> RunSnapshot | EventPage: ...
    def export(self, request: ExportRequest, capability: VerifiedCapability) -> ArtifactRef: ...
```

调用者不导入内部 repository、guard 或 adapter 对象。`TransitionGuard` 接收不可变
snapshot 并返回 `Decision`；它不打开 DB、CAS、网络或时钟。

## 2. CLI wire protocol

唯一外部入口为 `rkctl`。请求从 stdin 读取**恰好一个 JSON object**；stdout 输出
恰好一个 JSON object 加 LF。诊断只写 stderr。编码必须是 UTF-8，无 BOM。

```text
rkctl create --cap-file CRED.json < request.json
rkctl apply --cap-file CRED.json < request.json
rkctl inspect --handle RUN [--after-cursor N] [--limit N]
rkctl export --cap-file CRED.json < request.json
```

CLI 先按 `json/command.schema.json` 校验，再验证 capability；业务层不会接收
`capability_role`、`actor` 或 `is_admin` 这类自报字段。

### 2.1 create

`create` 请求的 `operation=create`，包含完整 draft `contract` 与 `request_id`。
它原子创建：run（`OPEN/revision=0`）、contract v1（`DRAFT`）和初始审计记录。
`create` 的幂等域是全局 `(issuer, request_id)`，实现用 `runs.create_issuer +
create_request_id + create_request_digest`；
同 ID 同 digest 返回原 `run_id`，同 ID 不同 digest 拒绝。

`create` 不冻结契约、不启动模型，也不增加 run revision。

### 2.2 apply

`apply` 请求包含：

```json
{
  "schema_version": "rk.command.v1",
  "operation": "apply",
  "request_id": "uuid-v4",
  "run_id": "uuid-v7",
  "expected_revision": 7,
  "command": {"type": "SubmitEvidence", "payload": {}},
  "artifact_inputs": []
}
```

`artifact_inputs[].path` 是宿主本地的绝对路径；不得出现在持久化 command payload。
接收器先做路径、大小、压缩、密钥和 hash 检查，再把内容 stage 到同盘 CAS。入库
后的 payload 只保留 artifact ID。

### 2.3 inspect

- 无 `after_cursor`：返回 run 当前 projection、当前 contract、terminal claims、open
  obligations、routes、预算汇总和 `last_cursor`；
- 有 `after_cursor`：返回 `event_seq > after_cursor` 的事件页；
- `limit` 默认 100，范围 1–500；
- inspect 无 capability 时只允许宿主同用户本机读取；服务化前不得复用这一假设；
- inspect 不修改 revision，不触发 adapter，不读取模型上下文。

### 2.4 export

`export` 请求固定 `operation=export`、run、request、`at_revision` 与 dossier spec。
若 `at_revision` 不是当前 revision，必须从事件投影重建或拒绝，不能导出混合时点。
同 `(run_id, at_revision, dossier_spec_digest)` 输出字节完全相同；时间戳取事件中的
持久值，不取导出时钟。输出进入 CAS 并返回 artifact ref；它不增加 run revision。

## 3. capability 凭证

凭证文件格式：

```json
{
  "schema_version": "rk.cap.v1",
  "capability_id": "uuid-v7",
  "subject_id": "stable host identity",
  "issuer": "rk-host",
  "key_id": "host-2026-08",
  "allowed_actions": ["SubmitEvidence"],
  "run_scope": ["uuid-v7"],
  "issued_at": "2026-08-11T12:00:00.000Z",
  "expires_at": "2026-08-12T12:00:00.000Z",
  "nonce": "base64url-128bit",
  "signature": "base64url-hmac-sha256"
}
```

签名消息是去掉 `signature` 后，按 composition 规范 JSON 序列化的 UTF-8 字节，
前缀为 `rk.cap.v1\n`。根密钥由宿主保存为 mode 0600 文件；凭证文件同样要求
0600。Windows 上检查 ACL 只允许当前用户与 SYSTEM。凭证正文、signature 和根
密钥均不得进入 SQLite、CAS、Git、日志或 dossier。

入口验证顺序：schema → 文件权限 → signature → key 状态 → 时间窗口 → revocation
→ run scope → action。任何失败统一外显 `CAPABILITY_DENIED`，详细原因只进脱敏
宿主安全日志，防止枚举。

## 4. 事务与幂等

### 4.1 apply 单事务

1. 在 DB/CAS 外校验 capability signature、时效、action 与请求中的 run scope；
2. 读取输入流、计算 hash、执行安全扫描并 stage 临时 CAS 文件；
3. `BEGIN IMMEDIATE`；
4. 查询 `(run_id, request_id)`：
   - digest 相同：回滚当前事务、删除本次 stage、返回原 receipt；
   - digest 不同：拒绝 `IDEMPOTENCY_KEY_REUSED`；
5. 读取 run projection，比较 `expected_revision`；
6. 调用纯 `TransitionGuard`；
7. 拒绝时插入最终 commands row，revision 不变，commit；
8. 接受时把 stage 原子 rename 为 CAS final，插入 artifact/evidence/events，更新投影、
   revision 加 1，插入 outbox 与最终 commands row；
9. commit 后投递 outbox；投递失败只标 `DELIVERY_PENDING`。

为了避免 DB commit 成功而 CAS 缺失，final CAS 必须在 DB commit 前存在；崩溃造成的
无引用 CAS 文件由 recovery 扫描为 orphan，宽限 24 小时后回收。CAS 与 DB 必须同盘，
rename 必须是原子操作。

### 4.2 锁与并发

- 每个工作区一个宿主 lock 文件，加 SQLite `BEGIN IMMEDIATE`；
- 12 个进程同 request 只有一个首次决定，其余读同 receipt；
- stale revision 永不自动重试写命令；调用者须 inspect 后用新 request ID 重构意图；
- outbox worker 可并发读取，但每行用 compare-and-set 从 PENDING 到 DELIVERING；
- agent 不持有 DB 连接和宿主 capability。

## 5. 命令全集

`§7.1` 的 13 个公开命令保留原名。`§10.6` 表中的 `PauseRun/ResumeRun` 规范化为
`Interrupt/Resume`。为使所有状态改变都可表达，v1 还冻结 8 个公共构造/晋级命令
和 6 个仅宿主可用的运行时命令。

### 5.1 公开命令（21）

| type | payload 必需字段 |
|---|---|
| `FreezeContract` | `contract_version`, `completeness_check_artifact_id` |
| `StartRun` | `contract_version`, `literature_plan_artifact_id`, `budget_policy` |
| `AmendContract` | `base_version`, `patch_artifact_id`, `approvals[]`, `impact_analysis_artifact_id` |
| `Interrupt` | `reason_code`, `checkpoint_artifact_id` |
| `Resume` | `checkpoint_artifact_id`, `lease_preflight`, `budget_preflight` |
| `Finalize` | `outcome`, `terminal_claim_ids[]`, `open_obligation_ids[]`, `dossier_spec` |
| `SubmitEvidence` | `claim_id`, `contract_version`, `statement_hash`, `evidence_type`, `evidence_strength`, `artifact_input_names[]`, `scope`, `provenance`, `evidence_root` |
| `RecordFailure` | `route_id?`, `claim_id?`, `failure_kind`, `normalized_fingerprint`, `equivalence_key`, `first_failed_obligation_id?`, `evidence_artifact_id?`, `applicability`, `novelty_delta` |
| `RequestExpansion` | `route_id`, `batch_kind`, `reservation`, `novelty_delta`, `expected_information_gain`, `decision_ids[]` |
| `ProposeContractDefect` | `contract_version`, `defect_type`, `evidence_refs[]`, `affected_claim_ids[]`, `proposed_patch_artifact_id` |
| `RecordPeerReview` | `claim_id`, `contract_version`, `statement_hash`, `review_artifact_id`, `verdict`, `checklist`, `source_graph`, `independence_profile` |
| `RecordQualityReview` | `claim_id`, `contract_version`, `review_artifact_id`, `verdict`, `dimensions`, `training_pool` |
| `RecordLiterature` | `contract_version`, `claim_id?`, `status`, `relation?`, `scope`, `cutoff_date`, `query_families[]`, `query_log_artifact_id`, `reference_artifact_id?`, `assessment_artifact_id` |
| `RegisterBridge` | `bridge_id?`, `contract_version`, `source_claim_id`, `target_claim_id`, `directionality`, `term_mapping`, `forward_obligations[]`, `reverse_obligations[]`, `loss_accounting`, `target_audit_review_id?`, `backtranslation_artifact_id?` |
| `RecordLeanFeedback` | `claim_id`, `attempt_id?`（`REPLAY_PASS` 时必需）, `contract_version`, `environment_profile_id`, `toolchain`, `mathlib_commit?`, `source_artifact_id`, `output_artifact_id`, `feedback_kind`, `first_failed_obligation_id?`, `diagnostic` |
| `RegisterClaim` | `contract_version`, `claim_kind`, `stable_label`, `statement_artifact_id`, `statement_hash`, `normalized_statement`, `target_route_id?` |
| `RegisterClaimEdge` | `contract_version`, `from_claim_id`, `to_claim_id`, `edge_kind`, `direction`, `justification_kind`, `justification_ref` |
| `RegisterRoute` | `contract_version`, `target_claim_id`, `label`, `representation`, `tool_family`, `approach_root`, `budget_policy` |
| `RegisterCompositionObligation` | `contract_version`, `parent_claim_id`, `child_claim_ids[]` 及 `composition.md` 的六项义务、规则和闭包定理 |
| `SubmitClosureWitness` | `parent_claim_id`, `contract_version`, `selected_subgraph`, `selected_subgraph_digest`, `discharged_obligation_ids[]`, `open_obligation_ids[]`, `edge_justifications[]`, `bridge_dependency_ids[]`, `composition_mode`, `verification_refs[]`, `human_attestation_review_ids[]` |
| `PromoteClaim` | `claim_id`, `target_axis`, `target_value`, `evidence_ids[]`, `closure_witness_id?` |

### 5.2 宿主命令（6）

| type | payload 必需字段 |
|---|---|
| `RegisterAttempt` | `route_id`, `ordinal`, `isolation_epoch`, `work_relpath`, `allowed_write_set[]`, `input_snapshot_digest` |
| `AcquireLease` | `attempt_id`, `holder_id`, `ttl_seconds` |
| `HeartbeatLease` | `lease_id`, `holder_id`, `extend_seconds` |
| `ReleaseLease` | `lease_id`, `holder_id`, `terminal_attempt_status` |
| `RecordBudget` | `route_id?`, `attempt_id?`, `event_kind`, `resource_kind`, `amount_microunits?`, `unit`, `currency?`, `provider_usage` |
| `BindExecution` | `route_id`, `attempt_id`, `adapter_name`, `adapter_version`, `source_commit?`, `environment_profile_id`, `invocation_artifact_id`, `external_ids` |

宿主命令仍走同一个 `apply`、revision 和 event log；“内部”只表示 capability 不发给
agent，不表示可绕过审计。

## 6. 回执

`json/receipt.schema.json` 是权威机器格式。`missing_conditions` 每项包含：

```json
{"code":"OPEN_OBLIGATION","path":"command.payload.closure_witness_id","params":{"id":"..."}}
```

`accepted=false` 时至少一项；`accepted=true` 时必须为空。幂等重放的回执逐字相同。

## 7. 错误与退出码

| 类别 | rejection_code | CLI |
|---|---|---:|
| JSON/字段 | `INGEST_SCHEMA_INVALID` | 2 |
| request ID 复用 | `IDEMPOTENCY_KEY_REUSED` | 3 |
| revision | `REVISION_CONFLICT` | 4 |
| capability | `CAPABILITY_DENIED` | 5 |
| 业务门 | PRD2 核心拒绝码 | 3 |
| SQLite busy/adapter 暂时故障 | `TEMPORARILY_UNAVAILABLE` | 6 |
| 未捕获内部错误 | 无业务回执，输出 `ProblemDetail` | 7 |

v1 无 HTTP。将来若加 HTTP，固定映射：schema 400，capability 403，not found 404，
revision/idempotency 409，business gate 422，busy 503，internal 500；不得改变业务
`rejection_code`。

## 9. 其余响应对象

`create` 成功 stdout：

```json
{
  "schema_version": "rk.handle.v1",
  "run_id": "uuid-v7",
  "revision": 0,
  "status": "OPEN",
  "current_contract_version": 1,
  "created_at": "2026-08-11T12:00:00.000Z"
}
```

`inspect` snapshot：

```text
RunSnapshot {
  schema_version = "rk.snapshot.v1"
  run_id, stable_project_id, status, revision, current_contract_version
  contract {statement_hash, status}
  root_claim_id?
  claims[] {claim_id, statement_hash, lifecycle, route, machine, semantic, peer,
            quality, closure}
  routes[] {route_id, status, first_failed_obligation_id?}
  open_obligation_ids[]
  active_attempts[] {attempt_id, route_id, status, lease_expires_at?}
  budget_summary {resource_kind -> reserved, actual, refunded, unknown_count}
  terminal_claim_ids[]
  last_cursor
}
```

数组一律按稳定 ID 排序；budget 用整数 microunits。`EventPage`：

```text
{schema_version:"rk.events.v1", run_id, after_cursor, events[], next_cursor,
 has_more}
```

`events[]` 按 event_seq 升序，包含 event_id/revision/type/contract_version/scope IDs/
payload/recorded_at；不嵌入 artifact 原文。

`export` 成功：

```json
{
  "schema_version": "rk.artifact_ref.v1",
  "artifact_id": "uuid-v7",
  "sha256": "64 lowercase hex",
  "byte_count": 123,
  "media_type": "application/json",
  "at_revision": 7
}
```

无法产生业务回执的 CLI 级错误使用：

```json
{
  "schema_version": "rk.problem.v1",
  "code": "INGEST_SCHEMA_INVALID",
  "message": "stable non-secret summary",
  "details": [{"path":"/command/payload", "code":"REQUIRED"}]
}
```

## 8. 路径与安全

- 输入路径 `resolve()` 后必须位于宿主预注册 inbox roots；
- 拒绝 symlink、junction、device path、ADS、`..` 逃逸和网络路径；
- 单文件默认 64 MiB，压缩包展开总量 256 MiB、文件数 2048、压缩比 100:1；
- 不自动执行上传脚本、notebook、二进制或宏；
- 命令参数使用 argv 数组，不经 shell；
- 已知 secret 值与高置信 token pattern 在 CAS commit 前扫描；命中进入 quarantine；
- 错误回显只含 artifact ID 和相对逻辑名，不含密钥、环境、绝对路径或完整模型输出。
