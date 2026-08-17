# Implementation status

更新时间：2026-08-17。本文区分“历史集成实测”和“当前 v0.3 权威状态”；历史成功日志
不等于当前内核授予数学权威。

## 当前 v0.3 可执行

- `ResearchKernel.create/apply/inspect/export`、SQLite/CAS、幂等收据、能力校验、27 个
  命令的 guard 与投影可以执行。
- 新 ROOT 必须使用合同的规范 JSON 工件、同一 SHA-256 和同一规范对象；同一运行只允许
  一个 ACTIVE ROOT。v0.1 ROOT 在迁移时失效；旧 lease 被撤销，未完成 attempt 被终止，
  旧 route 被退役，run 回到 OPEN。宿主必须按冻结合同重建 ROOT 并 StartRun，不能 Resume
  或复用旧 binding/执行上下文。旧 route label 与 work path 是历史唯一键；新一代必须用
  新 label/path，不能宣称原地续跑。若当前合同仍是 DEFECT_PROPOSED，则只能修订（尚未
  实现）或诚实结束 UNRESOLVED，不能启动新执行。
- dossier 的 JSON 与中文 Markdown 同源，包含题面、精确否定、规范化 claim、六轴状态、
  最终 outcome 和开放义务。Finalize 与后续 export 对同一修订保持字节一致。
- 普通证据只能记录候选。机器、同行、人类语义、质量、合同缺陷、局部路线、已知结论和
  反例终态在缺少受信宿主回执时 fail closed；`UNRESOLVED` 是当前诚实可达的结案结果。
  公开 JSON/中文卷宗把普通 evidence/review 标成 `UNMANAGED_CANDIDATE/REVIEW`、
  `authority_effect=NONE`、`promotion_eligible=false`；原始 `ACCEPTED/ACCEPT` 只表示摄入
  或意见内容，不表示内核认可数学结论。
- 预算 reservation 和 route attempt 上限仍由 guard 执行。模型 token、wall time、
  UNKNOWN_COST 等 ACTUAL 统计在接入宿主执行回执前不进入权威账本。v0.1 自报统计保留
  为 `legacy_untrusted_component_usage`，不会计入 `component_usage` 或可信 budget totals。

v0.3 的安全与可靠性修复、验证环境及明确未关闭的架构风险见
`docs/audit/v0.3-backend-audit.md`。

## 历史 v0.1 集成实测（不授予 v0.2 权威）

- 远端 `leane2efinal9` 曾真实调用公共 LeanSearch、OpenCode 1.18.16 + DeepSeek V4-Pro、
  jixia 4.28、Lean 4.28.0-rc1 和固定 Mathlib。
- Z3 4.15.3、SymPy 1.14、精确枚举、固定代码执行和 Crossref 做过 smoke。
- QED-Nano 4B 与 DeepSeek-Prover-V2-7B 已在远端 AMD GPU 按上游配置跑过五题；前者是
  自然语言候选生成器，后者是 Lean 候选生成器。DeepSeek-Prover 候选历史上 4/5 经
  Lean 接受，1/5 因 token 上限与 `sorry` 被拒。
- `docs/rkleane2e.json`、`docs/rktoolsmoke.json`、`docs/rkcomponents.md` 和
  `docs/evidence/models/` 只作为历史性能/接线证据保存。迁移 0004 会撤销其中旧 ROOT、
  machine、human、quality、closure 和终态的物化权威。

## 当前明确不可用

- 已有 DB-backed `HostExecutionReceiptService`，并执行一次性 nonce、scope、profile、
  invocation/result/usage 与环境绑定。只有配置并通过独立宿主回执的路径才可晋级；普通
  caller-supplied receipt 仍然 fail closed。
- 没有受管人类身份、盲审包和一次性签名，因此同行、HUMAN_ATTESTED、质量晋级不可用。
- 没有受信的文献等价性、合同缺陷裁决或反例 checker 回执，因此
  `PREVIOUSLY_KNOWN`、`CONTRACT_DEFECTIVE`、`DISPROVED` 不可作为终态。
- `AmendContract` 仍为 `TEMPORARILY_UNAVAILABLE`；raw artifact 嵌入和旧 revision
  dossier 导出仍未实现。
- Mathlib `.lake` cache 仍共享可写，Lean/jixia 的断网和只读挂载尚未由 OS 探针强制，
  因而不是 `ISOLATED_KERNEL_REPLAY`。
- 公共 LeanSearch 没有服务端 commit/model/index attestation；单卡本地 LeanSearch-v2
  还存在 reranker 静默退化风险，未完成等价验收前不得作为无损替代。

## 下一验收门

第一实现切片必须由独立宿主服务持有签名密钥。现有 `RegisterAttempt`/`BindExecution`
字段也是普通调用者提交的，只能视为未受信提示，不能成为签名输入的信任根。宿主必须
自己冻结或从实际 request、mount、进程和工具回执重新计算 input snapshot、adapter、
environment、invocation artifact bytes 与 nonce，再核验 route 的目标正是当前 ACTIVE
canonical ROOT/current claim，并装配 run、contract version、statement hash、profile、
request/result 和 usage。调用者不能传 scope。只有该回执通过一次性消费与环境复验后，
machine axis 和 ACTUAL budget 才能重新开放；跨 claim、伪 digest 和伪环境都必须有负测。
