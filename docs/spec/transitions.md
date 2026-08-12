# TransitionGuard v1 完整决定表

状态：`NORMATIVE_V1`

本文件把冻结 PRD 中散落的规则反向编译为一个可实现的纯函数。命令全集以
`api.md §5` 的 27 个类型为准；旧名 `PauseRun/ResumeRun` 不进入 wire format。

## 1. 纯函数签名

```text
decide(
  now_utc,
  run_snapshot,
  command,
  evidence_summary,
  verified_capability,
  policy_snapshot
) -> Decision

Decision = {
  accepted: bool,
  rejection_code: string?,
  missing_conditions: MissingCondition[],
  projection_mutations: MutationIntent[],
  event_intents: EventIntent[],
  artifact_requirements: ArtifactRequirement[]
}
```

输入均为不可变值。函数不读文件、不查 DB、不取网络、不执行 Lean/模型、不签名、
不生成 ID、不读环境变量。`now_utc` 由 CommandProcessor 注入，仅用于 lease/凭证
的确定性比较。生成 ID、stage CAS、事务和 outbox 在 guard 外。

## 2. 固定求值顺序

以下顺序决定稳定 rejection code；测试不得因重构改变：

1. wire schema 与纯格式检查（guard 外）；
2. capability signature、时效、revocation、请求 run scope 与 action（guard 外）；
3. artifact 安全扫描与 stage（guard 外）；
4. idempotency 查重（guard 外；只有已验证 capability 才能取回旧 receipt）；
5. run 存在；
6. `expected_revision == run.revision`，否则 `REVISION_CONFLICT`；
7. run 是否 CLOSED；仅 inspect/export 不经 apply，任何 apply 均 `RUN_CLOSED`；
8. contract version、statement hash、claim/route/attempt 归属一致；
9. 命令专属前置；
10. 组合、独立性、预算等承重门；
11. 产生 mutation/event intents。

同一步多个缺失条件全部返回；跨步骤只返回最早失败步骤，避免根据后续状态泄漏
额外信息。

## 3. 稳定 missing condition

```text
MissingCondition.code ∈
  RUN_NOT_FOUND | REQUIRED_ACTION | RUN_STATE | CONTRACT_STATE | CONTRACT_VERSION |
  OBJECT_NOT_FOUND | OBJECT_SCOPE | ARTIFACT_STATE | LITERATURE_PLAN | BUDGET_POLICY |
  CHECKPOINT | ACTIVE_LEASE | LEASE_HOLDER | LEASE_EXPIRED | EVIDENCE_TYPE |
  EVIDENCE_SCOPE | EVIDENCE_ROOT | INDEPENDENT_REVIEW | SEMANTIC_REVIEW |
  MACHINE_REPLAY | CLOSURE_WITNESS | OPEN_OBLIGATION | EDGE_JUSTIFICATION |
  BRIDGE_DIRECTION | SOURCE_GRAPH | NOVELTY_DELTA | DECISION_TARGET |
  TERMINAL_SUPPORT | USER_APPROVAL | CONTRACT_OWNER_APPROVAL | PEER_APPROVAL
```

`path` 使用 command JSON pointer；`params` 只含稳定 ID/枚举/计数。

## 4. 生命周期

```text
create -> OPEN(contract DRAFT)
OPEN --FreezeContract--> OPEN(contract FROZEN)
OPEN --StartRun--> RUNNING
RUNNING --Interrupt--> PAUSED
PAUSED --Resume--> RUNNING
OPEN|RUNNING|PAUSED --Finalize--> CLOSED
FROZEN --ProposeContractDefect--> DEFECT_PROPOSED
DEFECT_PROPOSED --AmendContract--> new FROZEN version; old SUPERSEDED
```

`CONTRACT_DEFECTIVE` 是 Finalize outcome；提出缺陷本身不关闭 run。amend 完成后，
run 返回 PAUSED，直到明确 Resume/StartRun。

## 5. 命令级决定表

“action”指 capability `allowed_actions` 中必须出现的精确 command type。

| 命令 | 允许 run/对象状态 | 附加硬前置 | 接受后的唯一语义 | 主要拒绝码 |
|---|---|---|---|---|
| `FreezeContract` | run OPEN；指定 contract DRAFT 且为 current | completeness artifact COMMITTED；合同字段完整；无 defect proposal | contract→FROZEN，记录 statement hash | `INVALID_TRANSITION`, `ARTIFACT_MISSING` |
| `StartRun` | run OPEN；current contract FROZEN | 文献计划 artifact；全局与 route 1/10 预算规则可解析 | run→RUNNING | `CONTRACT_NOT_FROZEN`, `BUDGET_DENIED` |
| `AmendContract` | run OPEN/RUNNING/PAUSED；current contract DEFECT_PROPOSED | base=current；批准满足 §6；patch/impact artifacts；所有 affected claims 已列 | 建 v+1 FROZEN；旧版 SUPERSEDED；依赖闭包 INVALIDATED；run→PAUSED | `CAPABILITY_DENIED`, `CONTRACT_DEFECTIVE` |
| `Interrupt` | RUNNING | checkpoint COMMITTED，含 active attempts、leases、outbox cursor | run→PAUSED；active attempts→PAUSED；lease 可保留到 TTL | `INVALID_TRANSITION`, `ARTIFACT_MISSING` |
| `Resume` | PAUSED | checkpoint 与最后 interrupt 一致；无冲突 active lease；预算未熔断；current contract FROZEN | run→RUNNING；不自动重启 attempt | `LEASE_CONFLICT`, `BUDGET_DENIED`, `ENVIRONMENT_DRIFT` |
| `Finalize` | OPEN/RUNNING/PAUSED | §7 终态审计；无 active lease；所有开放项显式列出 | 生成 immutable dossier；run→CLOSED/outcome | `TERMINAL_CLAIM_UNSUPPORTED`, `LEASE_CONFLICT` |
| `SubmitEvidence` | run OPEN/RUNNING/PAUSED；claim ACTIVE | contract/statement/scope 精确；artifact COMMITTED；evidence type 与 strength 合法；root provenance 完整 | 新 evidence/evidence root；**不改变 verdict** | `EVIDENCE_SCOPE_MISMATCH`, `MIXED_OUTPUT`, `SECRET_QUARANTINED` |
| `RecordFailure` | run 未关闭；route/claim 若给出须属本 run/current contract | fingerprint、适用域、首失败义务；evidence 若声明必须 COMMITTED | append FailureRecord；按依赖图使下游失效；不改变根真值 | `EVIDENCE_INSUFFICIENT`, `EVIDENCE_SCOPE_MISMATCH` |
| `RequestExpansion` | RUNNING；route ACTIVE/BLOCKED | 文献门已完成；decision_ids 非空；EIG>0；预算预留；单 route≤全局 CPU/GPU 上限 1/10；连续两批 novelty=0 时拒绝 | 记 reservation/批准 batch；不直接启动进程 | `BUDGET_DENIED`, `EVIDENCE_INSUFFICIENT` |
| `ProposeContractDefect` | current contract FROZEN | 非空证据、affected claims、patch artifact | contract→DEFECT_PROPOSED；run→PAUSED；不自动判 CONTRACT_DEFECTIVE | `EVIDENCE_INSUFFICIENT`, `INVALID_TRANSITION` |
| `RecordPeerReview` | claim ACTIVE/INVALIDATED 均可记录，只有 ACTIVE 可晋级 | reviewer action；scope/hash；来源图；review artifact；不得与作者根同源达到合同阈值 | append review；若阈值满足可更新 peer projection，其他轴不变 | `CAPABILITY_DENIED`, `INDEPENDENCE_UNKNOWN` |
| `RecordQualityReview` | claim ACTIVE/INVALIDATED 均可记录 | reviewer action；维度齐全；training_pool 只能软池/排除 | append quality review；可更新 quality projection，其他轴不变 | `CAPABILITY_DENIED`, `EVIDENCE_SCOPE_MISMATCH` |
| `RecordLiterature` | run 未关闭；contract current/superseded 均可按 scope 记录 | 五类 query family；cutoff；query log + assessment；HIT 必有 reference/relation | append literature record；只有 EQUIVALENT+assumptions exact 可供 PREVIOUSLY_KNOWN 晋级 | `INGEST_SCHEMA_INVALID`, `EVIDENCE_INSUFFICIENT` |
| `RegisterBridge` | source/target claims ACTIVE，同 contract | 双向义务分列；term map；loss accounting；VALID 需目标审计；EQUIVALENT 需双向全闭合 | 新建/版本化 bridge；不直接改变 claim | `EVIDENCE_INSUFFICIENT`, `EVIDENCE_SCOPE_MISMATCH` |
| `RecordLeanFeedback` | claim ACTIVE/INVALIDATED 可记录 | 预注册 env；source/output artifacts；toolchain；diagnostic；scope | append feedback；环境错不改数学轴；数学/语义错只失效首失败义务下游 | `ENVIRONMENT_DRIFT`, `REPLAY_FAILED` |
| `RegisterClaim` | run OPEN/RUNNING/PAUSED；contract current FROZEN（ROOT 可在 freeze 前建一次） | statement artifact/hash/normalized object 一致；stable label 未占用 | 新 ACTIVE claim，四轴初始值；组合默认 OPEN（原子 claim 可 NOT_REQUIRED） | `CONTRACT_NOT_FROZEN`, `INGEST_SCHEMA_INVALID` |
| `RegisterClaimEdge` | 两 claim ACTIVE、同 run/contract | 无环；方向合法；justification ref 存在；bridge 单向不得登记双向 | 新 ACTIVE edge；使受影响父 closure INVALIDATED/OPEN | `INVALID_TRANSITION`, `COMPOSITION_OPEN` |
| `RegisterRoute` | target claim ACTIVE；contract current FROZEN | approach root 来源图；预算策略；representation/tool family 非空 | 新 SCOUT/ACTIVE route | `EVIDENCE_SCOPE_MISMATCH`, `BUDGET_DENIED` |
| `RegisterCompositionObligation` | parent/children ACTIVE、同 contract | 六项均有 ref/status；composition_rule 枚举；displacement 已评或明确 NOT_ASSESSED | 新 OPEN/已 discharge 义务；parent closure→OPEN | `INGEST_SCHEMA_INVALID`, `EVIDENCE_SCOPE_MISMATCH` |
| `SubmitClosureWitness` | parent ACTIVE | digest 重算相等；所选 DAG 无环；图版本一致；所有进入边有 justification；模式满足 `composition.md` | witness DRAFT→ACCEPTED 或拒绝；parent closure 更新对应 CLOSED_* | `COMPOSITION_OPEN`, `EVIDENCE_SCOPE_MISMATCH` |
| `PromoteClaim` | claim ACTIVE；contract current FROZEN | 目标轴的 §8 门；所有 evidence ACTIVE；closure 若需则 ACCEPTED 且 digest 当前 | 只更新指定 axis，append verdict event；不得顺带更新其他轴 | `EVIDENCE_INSUFFICIENT`, `COMPOSITION_OPEN`, `INDEPENDENCE_UNKNOWN` |
| `RegisterAttempt` | run RUNNING；route ACTIVE/BLOCKED | ordinal 连续；路径符合固定模板；write set 不重叠；input digest 当前 | 新 QUEUED attempt | `LEASE_CONFLICT`, `BUDGET_DENIED` |
| `AcquireLease` | attempt QUEUED/PAUSED；run RUNNING | 无未过期 active lease；TTL 30..3600；holder 与执行 binding 一致 | 新 ACTIVE lease；attempt→RUNNING | `LEASE_CONFLICT`, `CAPABILITY_DENIED` |
| `HeartbeatLease` | attempt RUNNING；lease ACTIVE | holder 精确；未过期；extend 30..3600 | 更新 heartbeat/expires；只属于运行控制但仍 +1 revision | `LEASE_CONFLICT` |
| `ReleaseLease` | lease ACTIVE；attempt RUNNING | holder 精确；terminal status 合法；结果/检查点 artifact 要求由 adapter profile 决定 | lease→RELEASED；attempt→给定终态 | `LEASE_CONFLICT`, `ARTIFACT_MISSING` |
| `RecordBudget` | run 未关闭 | BUDGET_CONTROLLER；UNKNOWN_COST 可无 amount，其余必有非负值；refund 不超对应 reservation-actual | append budget event；若 fuse 则 route BLOCKED/run PAUSED | `CAPABILITY_DENIED`, `BUDGET_DENIED` |
| `BindExecution` | attempt QUEUED；route/run 匹配 | adapter/profile 注册；commit 符合 adapter spec；invocation artifact；一个 attempt 仅一 binding | 新 execution binding；不启动执行 | `ENVIRONMENT_DRIFT`, `INVALID_TRANSITION` |

所有接受的 apply 都使 run revision 恰加 1，包括 heartbeat；这是为了让 inspect 的
运行态快照也具有严格顺序。若 heartbeat 频率造成写放大，只能批量降低频率，不得
建立第二套无 revision 状态。

唯一例外是改变既有权威投影的受信 schema migration：迁移必须为每个受影响 run
原子写入一条 accepted system command 和对应 migration event，并恰好增加一次
revision。它与普通 apply 共享同一审计账和并发令牌，不是静默的第二状态通道。
v0.2 使用内部 `SystemRevalidateAuthority` 与 `AUTHORITY_REVALIDATED`；其 command、
event、request ID 都是迁移事务内生成并持久化的 UUID，receipt 必须通过与公开命令
相同的 receipt schema。迁移账本保证该事务不会作为同一版本重复执行。
这些内部身份仍遵循 UUIDv7 形状；若进程在状态事务提交后、迁移 ledger 写入前退出，
重试必须以已持久化的 `SystemRevalidateAuthority` command 为幂等标记，不得再次推进
revision。

## 6. Contract 修订批准矩阵

| 变更 | 必需批准 |
|---|---|
| 排版、错字、非承重说明 | 当前 `CONTRACT_OWNER` |
| 承重术语澄清但不改变模型集合 | `CONTRACT_OWNER` + semantic reviewer |
| 量词、定义域、对象类、成功证书 | `CONTRACT_OWNER` + 独立 `PEER_REVIEWER` |
| 稳定项目 ID、根研究对象、成功/失败语义 | 上述全部 + `USER_APPROVER` |

批准记录必须绑定 base contract hash 与 patch hash；普通 `PEER_ACCEPTED` review 不能
被重用为 amendment approval。

## 7. Finalize 决策

| outcome | 最低条件 |
|---|---|
| `PROVED` | 每个 terminal root 满足合同真值轴；需要组合者 closure ACCEPTED；semantic 达合同阈值；无未列 open obligation |
| `DISPROVED` | 可重放反例/硬证书，或合同允许的独立同行证明；scope 与 exact negation 对齐 |
| `ROUTE_LOCAL` | 局部 claim 有合法裁决；dossier 明确与 root 的不可比/不足；不得把局部结果命名为根证明 |
| `PREVIOUSLY_KNOWN` | Literature HIT=EQUIVALENT，假设/量词/对象全部匹配，原始来源 artifact 在案 |
| `CONTRACT_DEFECTIVE` | defect proposal 未被修复；批准满足变更类型；dossier 列明缺陷与旧分支 |
| `UNRESOLVED` | 任何时点可诚实结束；预算结算、开放义务和 side findings 均列出 |

`UNRESOLVED` 不要求根 claim 关闭；它要求**开放是显式的**。

## 8. PromoteClaim 轴门

| target axis/value | 必需条件 | 明确不能使用 |
|---|---|---|
| MACHINE/KERNEL_VERIFIED | clean Lean replay；toolchain/commit/image 固定；sorry=0；axiom 白名单；禁高信任 native_decide | 模型 judge、同行签名、原作者环境日志 |
| MACHINE/CERTIFICATE_VERIFIED | 预注册 checker profile；证书 schema；clean replay；输入/输出 hash | 只有程序输出、抽样、无命中 |
| SEMANTIC/TESTED | 原题↔形式化↔反译；satisfying witness；negation test；量词 mutation sentinel | 只做自然语言回译 |
| SEMANTIC/HUMAN_ATTESTED | TESTED + 独立 semantic reviewer 对精确 hash 签认 | kernel 编译成功 |
| PEER/ACCEPTED | 合同数量的独立 review 全 ACCEPT；来源图满足阈值 | 同模型多数、共享草稿 reviewer |
| QUALITY/ACCEPTED | quality policy 的评分/签认 | truth/machine 轴 |
| CLOSURE/CLOSED_MACHINE | 当前 MACHINE closure witness，所有组合组件 machine-checked | 清单非空检查、人工自然语言 |
| CLOSURE/CLOSED_HUMAN | 当前 PEER closure witness，所需人工签认独立且全 scope | machine 标签 |
| CLOSURE/CLOSED_HYBRID | 当前 HYBRID witness；无 OPEN cuts；各 cut 分型 | 未声明人工 cut |
| ROUTE/LOCAL_LEMMAS_VERIFIED | 局部 claim 达各自门；closure 仍可 OPEN | 推成 ROUTE_PROVED |
| ROUTE/ROUTE_LOCAL | 局部目标 closure 合法；根合同关系明确 | 冒充 root proof |
| ROUTE/ROUTE_PROVED | 根/route target 的 truth+semantic+closure 均达合同 | 任意单轴、软 majority |
| ROUTE/PREVIOUSLY_KNOWN | 等价文献门 | NO_HIT |

降级/失效不是 `PromoteClaim`：由 contract amend、edge/claim 变化或 failure 命令产生
系统 verdict event，reason 必须是 `DEPENDENCY_INVALIDATED`、`CONTRACT_SUPERSEDED`
或 `EVIDENCE_REFUTED`。

## 9. 依赖失效算法

1. seed 为 statement/contract/edge/evidence 发生承重变化的 claim；
2. 沿 ACTIVE `DEPENDS_ON/IMPLIES/SPECIALIZES/GENERALIZES` 的父向闭包遍历；
3. 将相关 closure witness、obligation discharge 和 derived verdict 标 INVALIDATED；
4. 不删除 artifact、evidence、review 或历史事件；
5. 未在闭包内的节点保持原状态；
6. 若新 contract 证明某节点 statement/scope/依赖均字节等价，可用新的
   `PromoteClaim` + `REVALIDATED_UNDER` evidence 恢复，不能原地取消失效事件。

## 10. 最小性质测试

- 任意 SOFT_MODEL evidence 多重集合都无法产生 MACHINE value；
- 任意 HUMAN_ATTESTED closure 都无法产生 CLOSED_MACHINE；
- stale revision 的 Decision 不含 mutation intent；
- 相同 snapshot/command/evidence/capability/now 的 Decision 字节一致；
- contract 改量词只失效依赖闭包；
- 单向 bridge 参与反向 edge 时拒绝；
-所有叶子 kernel green、缺一组合边时 `PromoteClaim(ROUTE_PROVED)` 拒绝；
- `Finalize(UNRESOLVED)` 在开放义务全部显式列出时合法；
- run CLOSED 后全部 27 个 apply command 都拒绝 `RUN_CLOSED`。
