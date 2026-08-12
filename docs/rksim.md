# RK 真实数理用户模拟与风险审计

审计日期：2026-08-12（America/New_York）

## 结论

本报告最初记录的本地原型问题已经经过独立审计和远端修复。当前树已在用户指定 SSH
主机真实调用公共 LeanSearch、OpenCode/DeepSeek、jixia 与 Lean 4/Mathlib；同时在 AMD
GPU 下载并运行 QED-Nano 4B 和 DeepSeek-Prover-V2-7B。最新 E2E
`leane2efinal9` 以全新 worktree 通过，机器轴到达 `KERNEL_VERIFIED`。

下面“已确认问题”保留为历史基线，列出的 P0/P1 已在本轮逐项修复，只有合同修订和
产品易用性等明确限制仍开放。当前准确状态以 `docs/implementation-status.md`、
`docs/rkcomponents.md` 和 `docs/rkmodelreport.md` 为准。

配套自然语言 Mermaid 状态机：`docs/rkfsm.md`。

## 模拟用户

用户是一位研究有限域组合问题的数学研究者。她有一条精确命题、一个自然语言证明
草稿、一个 Lean 文件和一次外部同行审阅，希望 RK 做三件事：保存来源、组织局部引理
与组合关系、最后只在证据闭合时导出 `PROVED` dossier。

## 实际旅程

1. 用户先复制配置、建立 inbox、宿主密钥和 capability 文件，然后手写完整 contract
   JSON。`create` 可创建 `OPEN / DRAFT / revision=0`。
2. 用户必须从 `inspect` 找出初始 artifact ID，再计算规范化 statement 的 SHA-256，
   调用 `RegisterClaim(ROOT)`。之后 `FreezeContract` 和 `StartRun` 可正常进入
   `RUNNING / FROZEN`。
3. 用户可以登记 route、attempt、execution binding、lease、evidence、failure、
   literature、review、bridge 和 composition obligation。接受的 apply 每次 revision
   加一；并发用户若拿旧 revision 会得到稳定的 `REVISION_CONFLICT`。
4. `SubmitEvidence` 只记录证据，不自动改变 verdict。这一点符合设计。
5. 如果用户只要求诚实收尾，`Finalize(UNRESOLVED)` 可以成功，并可确定性导出 dossier。
6. 机器轴现可由真实 replay 晋级；完整 `PROVED` 仍要求独立语义忠实性、路线闭合和
   同行条件，不能只凭模型或 Lean 单轴通过。

## 历史问题与本轮处置

### 已修复：`ROUTE_PROVED` 循环依赖

`PromoteClaim(target_axis=ROUTE, target_value=ROUTE_PROVED)` 在写入前调用
`_claim_proved(claim)`；而 `_claim_proved` 的第一个条件就是 claim 当前 route 已经等于
`ROUTE_PROVED`。新 claim 初始 route 是 `UNASSESSED`，所以无法通过唯一的公开晋级命令
第一次进入 `ROUTE_PROVED`。

影响：`Finalize(PROVED)` 又要求 terminal claim 满足 `_claim_proved`，因此正常公开路径
无法结束为 `PROVED`。

证据：`src/rk/guard.py` 的 `_handle_PromoteClaim` 与 `_claim_proved`。

### 已修复：机器轴信任调用者自报 replay

最小公开接口复现使用一个内容仅为 `Not a Lean replay.` 的文本文件，把 payload 标成
`LEAN_REPLAY / HARD_MACHINE`，并在 `provenance.replay` 自报：

```json
{"passed":true,"sorry_count":0,"axiom_violations":[],"native_decide":false,"environment_drift":false}
```

`SubmitEvidence` 被接受；随后 `PromoteClaim(MACHINE, KERNEL_VERIFIED)` 也被接受，机器轴
变为 `KERNEL_VERIFIED`。目前没有在这条路径上执行注册 verifier profile，也没有核对
Lean toolchain、入口声明、输出、环境或 artifact 内容。

影响：即使 `PROVED` 目前因其他门不可达，机器验证标签本身已经可能是假阳性；任何
依赖该轴的组合或下游 UI 都会被误导。

证据：`src/rk/guard.py` 的 `_machine_promotion`、`src/rk/storage.py` 对 provenance.replay
的提升逻辑，以及本次最小复现输出。

### 已修复：语义轴与同行轴缺少持久化输入

- 语义晋级依赖 `evidence_summary.semantic_checks`，但 `ResearchKernel.apply` 构造的
  `evidence_summary` 只包含 `contract_complete` 和本次 artifact inputs，没有公开命令
  写入四项 semantic checks。
- 同行晋级要求 `PEER_SIGNATURE` evidence 顶层含 `independent=True` 和
  `evidence_root_id`；`SubmitEvidence` 的 wire payload 没有顶层 `independent` 字段，
  持久化 evidence 也不生成它。`RecordPeerReview` 虽能保存 review，但不会直接晋级同行
  轴；`PromoteClaim` 接收的是 evidence IDs，不是 review IDs。

影响：`Finalize(PROVED)` 明确要求 `SEMANTIC=HUMAN_ATTESTED`，而 machine 或 peer 至少
一个成立；这两条晋级路径至少部分不可达。

### 已修复：暂停与租约组合卡死 attempt

`Interrupt` 把所有 RUNNING attempt 改成 `PAUSED`，但规范和实现保留 lease 到 TTL。
此时：

- `Resume` 在未过期 active lease 存在时拒绝；
- `ReleaseLease` 又要求 attempt 必须是 `RUNNING`，所以暂停后无法主动释放；
- lease 到期后，guard 不再把它视为 active，但数据库行状态仍是 `ACTIVE`；
- 再次 `AcquireLease` 会尝试插入另一条 ACTIVE lease，撞上“一 attempt 仅一 active
  lease”的唯一索引；当前没有 `ExpireLease` 或 `RevokeLease` 命令清理状态。

影响：真实长任务被中断后，恢复可能等待 TTL，之后仍以数据库完整性错误失败。

### 仍开放：合同缺陷可以提出，但无法修订

状态机允许 `FROZEN → DEFECT_PROPOSED → new FROZEN version`，guard 也会生成
`CREATE_CONTRACT_VERSION` mutation；但 `ProjectionWriter` 明确把该 opcode 列入
`unsupported_ops`。所以公开调用统一降为 `TEMPORARILY_UNAVAILABLE`。

影响：一旦发现量词、定义域或成功证书写错，用户只能把运行保持暂停、以
`CONTRACT_DEFECTIVE` 收尾，或另开运行；不能走规范声称的版本修订路径。

### 已修复：wire/guard/SQLite 枚举不一致

`evidence_root` 在 JSON schema 中只是任意 object；guard 只要求 `root_kind` 非空。
传入不属于数据库枚举的值时，命令会通过 schema 和 guard，最终触发 SQLite
`IntegrityError`。CLI 将其归入 `INTERNAL_ERROR`，不是稳定的可操作业务拒绝。

最小复现中使用 `root_kind=MACHINE` 即触发此错误；改成合法的 `LEAN_KERNEL` 才能继续。

## 可用性问题

- README 只有开发安装和四个接口名，没有首个真实项目的完整 CLI 教程，也没有
  capability 签发工具。数学用户需要理解 HMAC、UUID、ACL、CAS、artifact ID、规范化
  JSON 和 revision CAS 才能开始。
- `rkctl` 每次只读一个完整 JSON object；27 种命令虽可审计，但手工构造成本很高。
- `inspect` 返回 ID 和多轴状态，却不解释“下一条可接受命令”或把
  `missing_conditions` 转成人类任务清单。
- 当前测试 146 项通过；另有远端真实小题 E2E、模型 smoke 和 Lean 公理审计。完整数学
  `PROVED` 仍需语义与同行责任链，不能由这条小题机器链替代。

## 后续建议

1. 实现 `AmendContract`，或从公开可用能力中明确移除。
2. 用 OS 探针落实断网和 Mathlib cache 只读挂载，再升级隔离等级。
3. 为交互快检引入常驻 Lean server；最终 clean replay 继续用独立新进程。
4. 给数学用户增加从 `missing_conditions` 到中文下一步任务的辅助界面。
5. 在真实承重题上比较 OpenCode/V4-Pro、QED-Nano 和 DeepSeek-Prover，而不是外推五题
   smoke 的成功率。

## 验证记录

- `pytest`：146 passed；Ruff、strict mypy 通过。
- 远端 `leane2efinal9`：exit 0；LeanSearch、OpenCode、jixia、Lean replay 全完成。
- QED-Nano：五个自然语言 proof smoke；DeepSeek-Prover：4/5 Lean 内核通过，1/5 因
  8192-token 上限和 `sorry` 被拒。
- 规范化证据在 `docs/evidence/models/` 和 `docs/rkleane2e.json`。
