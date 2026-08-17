# ResearchKernel 数学研究智能体系统 PRD

> 2026-08-13 实现注记：冻结需求的逐项产品状态以 `docs/prdledger.md` 为准。只有证据与
> 声明范围匹配的能力才标为 `E2E_PASS`；adapter、schema、测试或历史 demo 不算产品完成。
> 当前为 53 个非排除项中 `E2E_PASS 52 / SCAFFOLD 1`。P31 因 Rethlas 真实请求返回
> HTTP 504 保持外部阻塞；P30/P33/P38 已由 run `019ff910-…` 的注册工具恢复与并发/串行
> 事件验收，P32 已由 run `019ff922-…` 的真实 DeepSeek-Prover 产品调用验收。两个 run 后续
> 暂停不抹除已持久化子能力，也不冒充整个研究完成。

## 0. 版本与冻结信息

- 文档版本：`RK-PRD-2`
- 状态：`FROZEN_FOR_IMPLEMENTATION`
- 冻结日期：2026-08-11（America/New_York）
- 产品代号：`ResearchKernel`
- 前一基线：`prd1.md`，SHA-256 `D3F2D16DC5101E8D3E21B5C1AE962CC6A86D1E8B36279098139BFA84EF065EE1`
- 修订依据：`reviews/r1.md`、`reviews/r2.md`、`reviews/r3.md`、`reviews/sum.md`
- 当前实施范围：单工作区、单服务器、ResearchKernel 单写者、少数并行研究任务、内部使用
- 当前主数学项目不因本 PRD 改变；`N2` 始终只指 `N2_AJT5`
- 本文是完整实施基线，不需要与 `prd1.md` 拼接阅读
- `prd1.md` 保留为不可变历史基线，不回写、不覆盖

## 1. 一句话产品定义

ResearchKernel 是嵌入 Archon-Horizon 的可信数学研究内核：它允许多个模型、数学策略和外部工具并行提出候选、构造跨领域桥梁、运行计算并与 Lean 双向联动，但把契约、数学状态、证据类型、组合闭包、预算和最终晋级权集中保留在宿主侧。

它不是新的基础模型，不是新的 Lean prover，也不是新的通用多智能体运行时。它把现有“小数学能力”中已经验证过的研究纪律，变成一个可恢复、可审计、可测试的深模块。

## 2. 本次修订的核心决定

`RK-PRD-2` 接受三份评审的全部 P0，并吸收适合可信 v1 的 P1。修订原则不是扩大系统，而是把概念性要求变成机器可执行对象。

相对 `RK-PRD-1`，本版固定以下改变：

1. 外部接口收缩为 `create / apply / inspect / export` 四个入口。
2. 所有写操作携带幂等键、期望修订号和运行时能力。
3. SQLite、内容寻址工件库和 Archon ledger 分别拥有唯一且不重叠的权威范围。
4. 运行、契约、文献、路线和 Claim 裁决改为正交状态轴。
5. 人类方法卡从“方法名目录”升级为带 `proof_spine`、锋利例和闭合义务的完整 worked card。
6. 所有局部结果必须通过 `CompositionObligation` 和 `ClosureWitness` 才能推进父 Claim。
7. 独立性拆成思想、推导、验证、实现和检索五个维度，并传播交叉授粉污染。
8. 跨领域桥梁升级为一等 `BridgeSpec`，要求目标领域审计和源领域回译。
9. Lean 不只返回编译成败，还产生结构化研究反馈事件。
10. 删除按模型产品名分支的浅 `ReasoningAdapter`；模型执行复用 Archon Harness。
11. 物理并行使用路线隔离目录和多个 execution binding；共享主线只走串行 promotion lane。
12. EvidenceIngest 采用 CAS 原子落盘与 SQLite 事务接收。
13. 统一高成本扩展门覆盖 RSA、大枚举、深检索、批量形式化和桥梁扫描。
14. 完整 N2 历史迁移、常驻单卡双 8B 服务和敌对隔离 runner 不阻塞 v1。
15. 可信内部 v1 估计为 22–34 人日。

## 3. 已有基础与复用边界

### 3.1 已经验证过的本地研究纪律

现有“小数学能力”已在 N2/AJT(5) 等真实任务中验证：

- 冻结命题、量词、精确否定和允许依赖；
- 路线登记、关键引理强度、最快证伪测试和失败留账；
- `NO_HIT_NOT_A_PROOF`，有限无命中不得晋升全称结论；
- 原始回传保全、哈希、混线隔离和空回传槽不计数；
- 主审与新鲜验证分离；
- 路线局部结果不得自动改变根问题状态；
- Lean 工具链、构建、公理集合、已覆盖范围和未覆盖范围分开记录；
- 外置状态表可以在上下文压缩后恢复项目身份与数学状态；
- 形式骨架通过不等于端到端数学定理通过；
- 数学状态改变必须绑定可追溯证据路径。

这些纪律是 ResearchKernel 的产品来源，不是待重新发明的研究设想。

### 3.2 直接复用

- Archon-Horizon：Harness、workspace、run/task/session、Git 来源账、usage、暂停和恢复；
- Rethlas：自然语言候选的生成—批评—修订策略；
- LeanSearch-v2：Mathlib 前提检索；
- jixia：Lean 声明、AST、局部状态和依赖抽取；
- Lean 4 / Mathlib：形式核与库；
- OpenCode + DeepSeek V4-Pro：当前远程工具执行外壳；
- GPT-5.6 Pro：数学路线、承重引理和跨领域桥梁主攻；
- Codex 5.6：代码、精确计算、Lean、仓库和实验执行；
- QED-Nano、DeepSeek-Prover 等：可选高吞吐候选生成器；
- Open Proof Corpus、MathArena 等：评测和审查校准材料。

### 3.3 只吸收原则，不完整移植

- AXLE：快检与干净重放分层；
- SEVerA：宿主拥有形式契约与硬 fallback；
- HorizonMath：优先寻找难生成、易验证的证书；
- TorchLean：事实、运行对象和证书绑定同一内容版本；
- Goedel-Architect：保存负节点与局部失效；
- Faithful Autoformalization：定位首个语义漂移节点；
- Sorries Are Not the Hard Part：kernel 正确与数学质量正交；
- QEDBench / Not All Proofs Are Equal：软 judge 与人类验收分离；
- Squeeze Evolve / V1：只吸收候选调度，不授予真值权限；
- MathArena / MLS-Bench：评测生命周期是摊销成本。

## 4. 用户与核心场景

### 4.1 主要用户

- 与模型共同研究开放问题的数学研究者；
- 将关键自然语言证明逐步机械化的 Lean 工程师；
- 审计 AI 数学结果来源、作用域、独立性和可靠性的研究团队。

### 4.2 核心场景

1. 对一个冻结命题先进行廉价广搜，再并行晋级 2–3 条实质不同路线。
2. 在无需更换总体思路的局部节点上复演人类数学家的完整证明骨架。
3. 在相邻领域间构造可证伪、可回译的桥梁，而不是只做术语类比。
4. 将承重节点交给 LeanSearch、jixia、Lean、SMT、CAS 或精确程序。
5. 把 Lean 暴露的缺失前提、类型冲突和新义务反馈给数学规划。
6. 局部失败时只失效相关 Claim 和下游，不重启整个研究。
7. 契约错误时版本化修订，保留旧分支和证据。
8. 未形式化证明可以通过独立同行审查合法结束。
9. 形成包含证明、反例、桥梁、失败、文献、预算和限制的 ResearchDossier。

## 5. 产品目标与非目标

### 5.1 必须实现

- 把手工数学协议变成机器可读、具有合法状态转移的 ResearchKernel；
- 模型只能提交候选和证据，不能直接写数学事实；
- 支持自然语言、精确计算、Lean 和人类同行审查；
- 对父结论实施强制组合闭包门；
- 精确记录路线独立性、证据根和交叉授粉；
- 支持带证明骨架、锋利例和近失配例的人类方法卡；
- 支持一等跨领域桥梁及双向义务；
- 对任务、路线和所有高成本扩展实行全局预算；
- 原始工件不可静默覆盖，失败可恢复、负知识可检索；
- 所有运行允许诚实终止为 `UNRESOLVED`。

### 5.2 v1 成功标准

在三类任务上完成端到端运行：

1. 廉价确定性验证器任务；
2. 自然语言研究证明、只形式化承重节点的任务；
3. 跨领域桥接并包含 Lean 或其他可重放工件的任务。

每类任务都必须：

- 可从冷启动恢复；
- 产生确定性 ResearchDossier；
- 无法靠伪造状态词绕过晋级；
- 无法把局部节点全绿误报为根结论完成；
- 能明确区分同源多数与真正独立证据。

### 5.3 非目标

v1 明确不做：

- 训练新的基础模型；
- 重写 Lean kernel、LeanSearch、jixia、Rethlas 或 Archon；
- 创建新的通用多智能体运行时；
- 多租户、网页前端、分布式数据库或通用云平台；
- 把所有自然语言证明强制转成 Lean；
- 用 LLM judge、模型置信度或多数票写入数学真值；
- 自动声称期刊等级或新颖性；
- 完整迁移 N2/M5/M6 全部历史；
- 常驻低延迟单卡双 8B LeanSearch 完整服务；
- 敌对隔离级独立 runner；
- 通用 event-sourcing、Postgres 或远程 CAS。

## 6. 产品原则

1. 模型产生候选；ResearchKernel 拥有契约、状态、证据类型和晋级规则。
2. 局部正确不推出全局正确；父结论必须有组合闭包见证。
3. 先证伪后扩算力；没有新可检查对象的路线不续费。
4. 人类方法库用于确定性局部迁移；跨领域搜索用于真正需要换表示或工具箱的节点。
5. 并行增加探索覆盖，不自动增加证据独立性。
6. Lean kernel、可重放证书、人类签名和模型 judge 分别证明不同强度的事情。
7. 失败定位到首个义务，只重算受影响下游。
8. 生成方可以提交工件，不能定义验证环境。
9. 所有状态改变都通过命令和事件发生，不允许直接改最终状态字段。
10. 一个可理解的深接口优先于多个浅模块。
11. 任何任务都可以合法结束为 `UNRESOLVED`、`CONTRACT_DEFECTIVE` 或局部结果。
12. 数学能力按可复演工件增长，不按模型输出长度增长。

## 7. 深模块设计

### 7.1 唯一外部模块：`ResearchKernel`

外部接口固定为：

```text
create(contract, request_id) -> RunHandle

apply(
  handle,
  command,
  expected_revision,
  request_id,
  actor_capability
) -> CommandReceipt

inspect(handle, after_cursor?) -> RunSnapshot | EventPage

export(handle, dossier_spec, request_id) -> ArtifactRef
```

调用者和测试只通过此接口观察行为。

`AmendContract`、`Interrupt`、`Resume`、`Finalize`、`SubmitEvidence`、`RecordFailure`、`RequestExpansion`、`ProposeContractDefect`、`RecordPeerReview`、`RecordQualityReview`、`RecordLiterature`、`RegisterBridge` 和 `RecordLeanFeedback` 都是 `apply` 接收的 typed command。

### 7.2 接口不变量

- 相同 `request_id` 重放必须返回同一回执；
- `expected_revision` 过期返回 `REVISION_CONFLICT`；
- 调用者不得直接写 Claim verdict、独立性或最终状态；
- `actor_capability` 由宿主发放，不能由请求体自报角色；
- `inspect` 不依赖模型上下文；
- `export` 只读取已持久化状态；
- `Finalize` 关闭当前 run 并生成不可变 dossier；
- 继续研究必须创建引用旧 dossier 的新 run。

### 7.3 内部实现

ResearchKernel 内部包含：

- `CommandProcessor`；
- `ContractKernel`；
- `ClaimGraph`；
- `CompositionGuard`；
- `TransitionGuard`；
- `EvidenceIngest`；
- `ResearchBudgetPolicy`；
- `IndependenceTracker`；
- `StrategyRunner`；
- `CapabilityRegistry`；
- `DossierBuilder`；
- SQLite 状态与内容寻址工件库。

这些是内部实现和内部缝隙，不是外部调用者必须学习的接口，也不分别部署成远程进程。

### 7.4 删除浅 `ReasoningAdapter`

模型执行直接复用 Archon Harness。ResearchKernel 只向 Harness 下发：

- 任务角色；
- 所需能力；
- 可见工件；
- 隔离 epoch；
- 预算；
- 允许写集；
- 停止条件。

Rethlas、一次性候选、局部修复和对抗验证是 `StrategyRunner` 的不同内部策略。模型、provider、effort 和账号留在 Harness 配置中，ResearchKernel 不按模型名称写业务分支。

Rethlas 的 verifier 结果固定接收为 `SOFT_MODEL`。

### 7.5 外部执行绑定

一个数学运行可以产生多个 Archon run、task、session 和重试：

```text
ExecutionBinding {
  rk_run_id
  route_id
  attempt_id
  archon_run_id?
  archon_task_id?
  archon_session_ids[]
  workspace_commit?
  adapter_version
}
```

Archon 适配优先使用稳定 CLI JSON，不导入其内部 Python 对象。录制 JSON 与假适配器用于契约测试。

### 7.6 Lean 内部信任缝隙

对外仍只有 ResearchKernel；Lean 实现内部分为：

- `PremiseRetriever`：LeanSearch/jixia，只产生候选；
- `LeanWorker`：Codex 或专用 prover，可修改自己的路线目录；
- `ReplayVerifier`：宿主持有，只执行预注册验证 profile。

生成与验证环境不得由同一不可信角色决定。

## 8. 权威状态与跨存储一致性

三个唯一所有者固定为：

- SQLite：数学语义状态、命令、事件、裁决和预算账的唯一权威；
- 内容寻址工件库（CAS）：不可变原始字节的唯一权威；
- Archon Git ledger：代码、工作区变更、执行 provenance 和人类可读快照的权威。

Markdown 和 ResearchDossier 是只读投影，不是可写状态源。

SQLite 事务写 `integration_outbox`。对账器把摘要、commit SHA 和状态快照幂等同步到 Archon。同步失败记 `DELIVERY_PENDING`，不得回滚已经持久化的数学事件，也不得产生第二套数学状态。

不实现 SQLite 与 Git 的伪两阶段事务。

## 9. 命令、回执与错误语义

### 9.1 `CommandReceipt`

```text
CommandReceipt {
  request_id
  command_id
  accepted
  revision_before
  revision_after
  event_ids[]
  artifact_ids[]
  rejection_code?
  missing_conditions[]
}
```

### 9.2 核心拒绝码

```text
REVISION_CONFLICT
CAPABILITY_DENIED
RUN_CLOSED
CONTRACT_NOT_FROZEN
CONTRACT_DEFECTIVE
INVALID_TRANSITION
EVIDENCE_INSUFFICIENT
EVIDENCE_SCOPE_MISMATCH
ARTIFACT_MISSING
INGEST_SCHEMA_INVALID
MIXED_OUTPUT
SECRET_QUARANTINED
COMPOSITION_OPEN
INDEPENDENCE_UNKNOWN
BUDGET_DENIED
LEASE_CONFLICT
REPLAY_FAILED
ENVIRONMENT_DRIFT
TERMINAL_CLAIM_UNSUPPORTED
```

拒绝必须返回缺失条件，不能只返回布尔失败。

## 10. 正交状态模型

### 10.1 生命周期轴

```text
RunLifecycle:
  OPEN | RUNNING | PAUSED | CLOSED

ContractVersionStatus:
  DRAFT | FROZEN | DEFECT_PROPOSED | SUPERSEDED

LiteratureStatus:
  PENDING | HIT | NO_HIT_AFTER_SEARCH | INCOMPLETE

RouteLifecycle:
  SCOUT | ACTIVE | BLOCKED | RETIRED | CLOSED

CompositionStatus:
  OPEN | LOCAL_LEMMAS_VERIFIED | PARTIAL_GRAPH_CLOSED | CLOSED
```

这些状态正交存储，不拼成复合字符串。

### 10.2 路线结果与失败

```text
ROUTE_LEMMA_FALSE
ROUTE_PROVED
ROUTE_LOCAL
PREVIOUSLY_KNOWN
UNRESOLVED
MECHANICAL_UNBOUNDED
```

路线生命周期与路线结果分开。

### 10.3 引理相对强度

```text
STRICTLY_WEAKER
EQUIVALENT
STRONGER
INCOMPARABLE
UNKNOWN
```

允许合法辅助强化 `AUXILIARY_STRENGTHENING`：

- 不增加原目标假设；
- 显式证明强化结论推出原结论；
- 新增参数、不变量或状态变量有消去接口；
- 不得用未证命题重述原困难。

增加假设或改变研究对象仍走契约修订。

### 10.4 Claim 多轴裁决

```text
ClaimVerdict {
  machine:
    UNCHECKED | CERTIFICATE_VERIFIED | KERNEL_VERIFIED

  semantic:
    UNCHECKED | TESTED | HUMAN_ATTESTED | DEFECT_FOUND

  peer:
    UNREVIEWED | PEER_ACCEPTED | PEER_REJECTED

  quality:
    UNREVIEWED | QUALITY_ACCEPTED | REVISION_REQUIRED
}
```

不得使用无类型的 `verified`。

### 10.5 TruthGate 与 QualityGate

`TruthGate` 与 `QualityGate` 是正交门，不是串联流水线：

- `TruthGate` 只判断声明在给定作用域内是否有足够的机械、语义或同行正确性证据；
- `QualityGate` 只判断结果的自然性、一般性、清晰度、库质量和发表强度；
- 一个 Claim 可以先通过同行正确性而尚未 Lean 化；
- 一个 Claim 也可以先通过 Lean kernel，而语义忠实或数学质量仍待审；
- 未选入 Lean 化的节点仍可通过合法的 `PEER_ACCEPTED` 正确性路径结束；
- QualityGate 不得读取“kernel 已通过”并自动接受，TruthGate 也不得读取“写得很好”并自动接受。

LearningFactory 必须消费明确分型的门结果，不能把质量分数或专家偏好伪装成硬真值轨迹。

### 10.6 合法终态

- `KERNEL_VERIFIED + semantic TESTED/HUMAN_ATTESTED`，具体最低语义级别由契约规定；
- `CERTIFICATE_VERIFIED + semantic TESTED/HUMAN_ATTESTED`，具体最低语义级别由契约规定；
- `PEER_ACCEPTED` 的未形式化自然语言证明；
- 机械与同行双接受；
- `ROUTE_LOCAL`；
- `PREVIOUSLY_KNOWN`；
- `CONTRACT_DEFECTIVE`；
- `UNRESOLVED`。

同行正确性、质量、kernel 正确性彼此不能反向制造。

### 10.6 最小状态转移表

| 命令 | 前置状态 | 能力 | 必需对象 | 后置状态/事件 | 主要拒绝码 |
|---|---|---|---|---|---|
| `FreezeContract` | run OPEN；contract DRAFT | `CONTRACT_OWNER` | 契约完整性检查 | contract FROZEN | `INVALID_TRANSITION` |
| `StartRun` | contract FROZEN；run OPEN/PAUSED | `KERNEL_OPERATOR` | 文献计划、预算 | run RUNNING | `CONTRACT_NOT_FROZEN` |
| `PauseRun` | run RUNNING | `KERNEL_OPERATOR` | checkpoint | run PAUSED | `INVALID_TRANSITION` |
| `ResumeRun` | run PAUSED | `KERNEL_OPERATOR` | lease/预算可用 | run RUNNING | `LEASE_CONFLICT` |
| `SubmitEvidence` | run 未 CLOSED | 候选或验证能力 | artifact、scope、provenance | 接收事件；裁决可能不变 | `EVIDENCE_SCOPE_MISMATCH` |
| `PromoteClaim` | Claim 依赖有效 | `KERNEL_OPERATOR` | 合格证据、闭包见证 | 对应 verdict 晋级 | `COMPOSITION_OPEN` |
| `RecordPeerReview` | Claim 可审查 | `PEER_REVIEWER` | 作用域、来源图 | peer 轴更新 | `CAPABILITY_DENIED` |
| `ProposeContractDefect` | contract FROZEN | 任意写能力 | 缺陷证据 | DEFECT_PROPOSED | `EVIDENCE_INSUFFICIENT` |
| `AmendContract` | DEFECT_PROPOSED | 所需批准能力 | patch、批准、影响分析 | 新 FROZEN 版本；旧版 SUPERSEDED | `CAPABILITY_DENIED` |
| `Finalize` | run OPEN/RUNNING/PAUSED | `KERNEL_OPERATOR` | 终态审计、预算结算、未闭合义务清单 | dossier；run CLOSED | 若声称已证明但组合仍开放，则 `TERMINAL_CLAIM_UNSUPPORTED`；选择 `UNRESOLVED` 可合法关闭 |
| `RequestExpansion` | route ACTIVE/BLOCKED | `CANDIDATE_WRITER` | 扩展申请 | 批次预算或拒绝 | `BUDGET_DENIED` |

`Finalize` 不要求根 Claim 已证明；它要求所有未闭合部分被显式记录。

## 11. 契约与修订

### 11.1 `ResearchContract`

至少包含：

- 稳定项目 ID；
- 原始题面与来源；
- 对象、定义和全部量词；
- 精确否定；
- 允许依赖和禁止信息；
- 边界、随机性和并列规则；
- 成功证书类型；
- 明确不主张；
- 文献检索范围和截止日期；
- 预算与停止规则；
- 语义审查者；
- 允许的合同修订权限。

### 11.2 契约缺陷

任何智能体、验证器、文献模块或审查者可提出：

```text
ProposeContractDefect {
  contract_version
  defect_type
  evidence_refs[]
  affected_claims[]
  proposed_patch
}
```

批准规则：

- 文字澄清：`CONTRACT_OWNER`；
- 量词、定义域、对象类或成功标准改变：`CONTRACT_OWNER` + 独立 `PEER_REVIEWER`；
- 改变研究对象：`USER_APPROVER`。

新版本不可覆盖旧版本。旧分支标记 `SUPERSEDED_BY(vN)`；只有依赖审计通过的节点才能 `REVALIDATED_UNDER(vN)`。

## 12. 文献先行门

昂贵路线启动前必须完成：

```text
LITERATURE_HIT(ref, relation, scope)
NO_HIT_AFTER_SEARCH(query_log, cutoff_date)
SEARCH_INCOMPLETE(reason)
```

关系只能为：

```text
EQUIVALENT
STRICTLY_STRONGER
STRICTLY_WEAKER
OVERLAP
CONTRADICTS
INCOMPARABLE
```

只有 `EQUIVALENT` 且假设完全匹配，才能进入 `PREVIOUSLY_KNOWN`。搜索无命中不是新颖性证明。

跨领域路线的检索计划必须分别覆盖：

- 原题原词；
- 规范化数学对象；
- 目标领域映射词；
- 关键不变量或证书词；
- 已知作者和定理链。

缺任一规定查询族时只能是 `SEARCH_INCOMPLETE`。

## 13. Claim 图与组合闭包

### 13.1 Claim 身份

- Claim 使用稳定业务 ID；
- 陈述、规范化内容和契约版本另存 hash；
- 文本 hash 不充当稳定业务身份；
- 插入依赖边前检查环；
- 契约或父 Claim 改变时，在同一事务中失效下游闭包；
- 已失效证据保留历史，但不能参与当前 revision 裁决。

### 13.2 `CompositionObligation`

任何依赖局部变换、局部证书、块分解或逐步修复的路线必须建立：

```text
CompositionObligation {
  obligation_id
  parent_claim_id
  child_claim_ids[]
  local_domain
  coverage_statement
  overlap_or_compatibility
  preserved_global_invariant
  progress_or_well_foundedness
  boundary_and_exception_terms
  simultaneous_choice_condition
  composition_rule
  closure_theorem
  missing_conditions[]
  status
}
```

局部节点全部成立而组合义务未闭合时，路线最高为 `LOCAL_LEMMAS_VERIFIED`。

若闭合义务等价于或强于原命题，标记 `OBLIGATION_DISPLACEMENT`，不得把困难搬家计作进展。

### 13.3 `ClosureWitness`

父 Claim 晋级前必须生成：

```text
ClosureWitness {
  parent_claim_id
  contract_version
  selected_subgraph_digest
  discharged_obligations[]
  open_obligations[]
  edge_justifications[]
  bridge_dependencies[]
  composition_mode
  verification_refs[]
}
```

`composition_mode` 为 `MACHINE | PEER | HYBRID`。

TransitionGuard 必须检查：

- 每条进入父结论的逻辑边有证据；
- 方向正确；
- 契约版本一致；
- 没有未声明的开放 cut；
- 机械、人类和剩余部分分别列出。

叶子全部 `KERNEL_VERIFIED` 也不能绕过本门。

## 14. 人类数学家方法库

### 14.1 目标

对于总体表示、目标形态和闭合接口已经明确的局部节点，优先复演来源可靠的人类证明骨架，避免模型从零盲猜。

方法库不是“极值法、双计数、概率法”的标签目录，而是可迁移、可拒绝、可回放的证明决定链。

### 14.2 确定性局部节点准入

只有同时满足以下条件的 Claim 才进入方法卡通道：

1. 对象、量词、依赖和目标形态已冻结；
2. 存在来源明确的已知证明骨架；
3. 来源前提可逐项映射到当前 Claim；
4. 差异只涉及参数、记号、有限边界、标准编码或已证明接口引理；
5. 不要求发明新的核心概念或跨范畴桥梁；
6. 关键步骤存在廉价证伪测试、锋利例或局部检查器；
7. 不把等价于或强于父问题的未证命题隐藏成例行引理。

不满足时标记 `METHOD_TRANSFER_UNJUSTIFIED`，返回一般探索通道。

### 14.3 `MethodCard`

```text
MethodCard {
  method_card_id
  source_theorem
  source_proof_scope
  object_signature
  hypothesis_map
  target_shape
  representation_change
  decisive_choice
  proof_spine[]
  invariant_or_certificate
  progress_measure
  local_to_global_closure
  equality_and_sharp_examples[]
  failure_modes[]
  forbidden_shortcuts[]
  mechanically_checkable_obligations[]
  unresolved_adaptation_risks[]
  success_transfer_example
  near_miss_rejection_example
}
```

`proof_spine[]` 必须记录因果步骤：

1. 规范化对象；
2. 选择表示或极值对象；
3. 作出决定性选择；
4. 施加局部变换；
5. 保持不变量或证书；
6. 使用良基量严格推进；
7. 通过组合义务闭合全局；
8. 回验锋利例和等号结构。

每一步同时记录“为什么需要”和“缺哪项前提时失败”。

没有 proof spine 和近失配例的条目只能记 `METHOD_TAG`，不得作为路线晋级依据。

### 14.4 初始方法族

- 极小反例与删除—收缩；
- 不变量、单调量和势函数；
- 对称化、轨道压缩和规范形；
- 双计数、容斥与生成函数；
- 概率法、条件化和耦合；
- 极值原则、紧致性和局部改进；
- 对偶性、分离与证书；
- 归纳、递推、块分解和局部到全局；
- 退化分层和一般位置；
- 锋利例、等号结构和稳定性；
- 构造—验证分离；
- 最便宜反例、边界例和低维分类。

v1 不追求覆盖全部方法族，只建设 12–20 张完整 worked cards。只有新增经回放通过的 worked card 才计能力增长。

### 14.5 表示改变

`representation_change` 必须记录：

- 原对象和目标表示；
- 映射定义；
- 可逆性或单向性；
- 信息损失；
- 新表示中显现的局部、线性或单调结构；
- 回译步骤；
- 新结论相对原 Claim 的强弱。

更易计算不等于数学进展。

### 14.6 方法卡选择

按以下字典序选择：

1. 前提映射完整；
2. 锋利例、等号结构和边界保持；
3. 决定性变换有严格推进量或闭合证书；
4. 新增义务严格弱于当前 Claim；
5. 可机械或局部复核比例；
6. 预期成本。

文本相似度、模型投票和历史成功率只能作末级排序。每次保存首选、次选和拒绝次选的数学理由。

### 14.7 `SentinelSuite`

长证明前必须运行：

- 最小非平凡例；
- 边界与退化例；
- 已知等号例或极值构型；
- 接近等号的稳定性族；
- 删除关键假设后的反例或失败预期；
- 改变常数、量词方向或不等号方向后的失败预期；
- 来源成立而目标迁移应失败的近失配例。

破坏锋利常数、等号结构或必要退化情形时，标记 `SHARPNESS_LOST` 并停止晋级。

有限无命中仍只记 `NO_HIT_NOT_A_PROOF`。

### 14.8 定向修复

方法迁移失败定位到 proof spine 的首个失败义务：

```text
HYPOTHESIS_MISMATCH
REPRESENTATION_LOSS
LOCAL_MOVE_FALSE
PROGRESS_NOT_STRICT
EQUALITY_CASE_DESTROYED
GLOBAL_GLUE_GAP
OBLIGATION_DISPLACEMENT
```

只修改首个失败节点和下游，不在已审计上游中静默加入新假设。

## 15. 并行探索、独立性与交叉授粉

### 15.1 廉价广搜与昂贵深挖

每个非平凡任务先进入有上限的 `SCOUT_EPOCH`：

- 并行产生 8–20 张廉价路线卡；
- 不展开长证明；
- 覆盖“对象表示 × 方法族 × 目标领域 × 验证器”不同单元格；
- 规范化并结构去重；
- 选择 3–5 条详细规划；
- 首批只晋级 2–3 条昂贵路线。

晋级以覆盖优先，不以模型自评分优先。每条晋级路线必须声明预期新工件和最快失败测试。

### 15.2 `ApproachRoot`

```text
ApproachRoot {
  approach_id
  normalized_object
  representation
  method_families[]
  key_intermediate_claims[]
  tool_families[]
  ancestor_approach_ids[]
  imported_artifact_ids[]
  prompt_lineage_digest
  retrieval_snapshot_digest
  model_and_checkpoint
  isolation_epoch
}
```

### 15.3 `EvidenceRoot`

```text
EvidenceRoot {
  evidence_root_id
  claim_id
  evidence_type
  producer_lineage
  certificate_digest
  dependency_closure_digest
  checker_family
  checker_environment_digest
}
```

相同证书的多次重放是一个 EvidenceRoot、多个 VerificationEvent。

### 15.4 `IndependenceProfile`

```text
IndependenceProfile {
  idea_independence
  derivation_independence
  verification_independence
  implementation_independence
  retrieval_independence
  reasons[]
  shared_ancestors[]
}
```

每个维度为 `INDEPENDENT | SHARED | UNKNOWN`。

不同模型、会话或措辞不自动产生独立性。祖先未知时保守为 `UNKNOWN`。

### 15.5 交叉授粉

首轮隔离有明确 `isolation_epoch`。

允许共享的对象必须封装为：

```text
AuditedExchangeArtifact {
  artifact_id
  statement_hash
  contract_version
  source_route
  evidence_type
  audit_status
  applicable_scope
  known_limitations[]
}
```

路线导入另一条路线工件后生成新的后继 ApproachRoot，并永久记录 `CROSS_POLLINATED_FROM`。导入后的推导不能再相对来源路线声称思想或推导独立。

## 16. 跨领域桥梁

### 16.1 `BridgeSpec`

```text
BridgeSpec {
  bridge_id
  source_domain
  target_domain
  source_objects[]
  target_objects[]
  source_claim
  target_claim
  forward_map
  backward_map
  forward_obligations[]
  backward_obligations[]
  preserved_invariants[]
  imported_assumptions[]
  lost_assumptions[]
  gained_assumptions[]
  loss_profile[]
  target_domain_tools[]
  fastest_countertests[]
  roundtrip_tests[]
  source_to_target_dictionary
  target_to_source_dictionary
  source_domain_auditor
  target_domain_auditor
  lean_claim_refs[]
  literature_refs[]
}
```

两个方向分别为：

```text
UNSTATED | CANDIDATE | CHECKED | REFUTED
```

桥梁总体状态：

```text
PROPOSED
MAPPED
ONE_WAY_VALID
EQUIVALENCE_VALID
REFUTED
TRANSLATION_DEFECT
```

### 16.2 四步隔离动作

1. 源领域陈述规范化；
2. 目标领域映射构造；
3. 目标领域工具或审查者核验；
4. 回译源领域并检查量词、对象类和损失。

构造映射的智能体不能同时成为唯一目标领域审查者。

单向桥梁可保留为路线局部结果，但不能报告等价。

跨领域路线只有产生可证伪映射义务、可重放中间对象或目标领域可检查结论后才续费。

## 17. Lean 双向联动

### 17.1 工作流

1. jixia 抽取当前目标、局部假设、声明和依赖；
2. LeanSearch 检索候选前提；
3. LeanWorker 生成或局部修复 Lean；
4. 快速检查产生协作反馈；
5. 最终工件进入受信 `CLEAN_REPLAY_LOCAL`；
6. 编译和语义事件映射回 Claim 图；
7. 只失效首个受影响节点及下游；
8. 未受影响节点继续复用。

### 17.2 `LeanFeedbackEvent`

```text
LeanFeedbackEvent {
  claim_id
  declaration_hash
  local_context_hash
  minimal_goal
  event_type
  implicated_assumptions[]
  candidate_new_obligations[]
  related_mathlib_declarations[]
  affected_claim_edges[]
  raw_log_ref
}
```

`event_type` 至少包含：

```text
MISSING_PREMISE
TYPECLASS_MISMATCH
DEFINITION_MISMATCH
FAILED_REWRITE
TERMINATION_GAP
CONSTRUCTION_GAP
SEARCH_NO_HIT
ENVIRONMENT_ERROR
SEMANTIC_DRIFT
COUNTEREXAMPLE_WITNESS
DEPENDENCY_CONFLICT
NEW_OBLIGATION
```

`candidate_new_obligations` 在被人工、Lean 或硬证据确认前只是软候选。

`ENVIRONMENT_ERROR` 不触发数学重规划。`SEMANTIC_DRIFT` 返回形式化节点；只有证据指向契约缺陷时才 amend。

### 17.3 Lean 化节点选择

按以下因素排序：

- Claim DAG 中心性；
- 语义漂移风险；
- 桥梁关键性；
- 下游复用度；
- 机械可行性；
- 潜在反例价值。

优先形式化承重边、桥梁保持义务、易偷换的量词和可作为廉价验证器的节点，不以最容易编译为唯一标准。

每次记录 `formalization_selection_reason` 和 `why_not_formalized`。

### 17.4 强制忠实性

- 原题、Lean statement、反译文本逐字段对齐；
- 检查量词、定义域、非退化条件、维数和隐藏类型类；
- 有限或具体问题给出假设可满足性见证；
- 抽象问题给出标准模型实例；
- 运行量词、常数、方向和假设变异测试；
- `#print axioms` 对照白名单；
- 单独扫描 `sorry`、`admit`、`sorryAx`、新增 `axiom` 和 `unsafe`；
- 高信任配置封禁 `native_decide` 和未经验证的 oracle；
- 记录 Lean toolchain、Mathlib commit、声明 hash 和依赖闭包。

一般性“否定不可证”不作为硬门。有限可判定片段可由模型检查升级。

### 17.5 `CLEAN_REPLAY_LOCAL`

ResearchKernel 宿主调用预注册 verifier profile。生成方只提交 Artifact ID，不能提交命令、镜像或依赖路径。

profile 固定：

- executable digest；
- 参数模板；
- 环境白名单；
- 资源限制；
- Lean toolchain；
- Mathlib commit；
- 输入装配规则。

记录实际二进制、输入、stdout/stderr、退出码和环境探针 hash。

网络关闭和只读挂载只有运行时探针证明后才标记 `ENFORCED`，否则显式标记 `UNENFORCED`。

独立 runner 前不得升级为 `ISOLATED_KERNEL_REPLAY`。

### 17.6 单 AMD GPU 的 LeanSearch

v1 支持：

- public endpoint，或
- 本地 retriever-only，或
- 单卡批式 rerank job。

批式 job 汇集查询后统一 embedding/retrieve，卸载后统一 rerank；全局 GPU semaphore 为 1。cache key 包含 query、top_k、rerank 配置、模型 hash 和 corpus/index hash。

健康状态：

```text
RETRIEVER_READY
RERANKER_READY
DEGRADED_RETRIEVER_ONLY
```

常驻低延迟单卡双 8B 服务不阻塞 v1。

## 18. 工具能力注册表

调度按能力，不按模型名称：

```text
CapabilityRecord {
  capability_id
  domains[]
  accepted_input_schema
  output_schema
  evidence_strength
  trusted_base
  replay_adapter
  determinism
  cost_curve
  size_limits
  known_failure_modes[]
}
```

v1 至少登记：

- Lean；
- LeanSearch；
- jixia；
- 精确有限枚举；
- 一个 SMT 接口；
- 一个 CAS 接口；
- 通用代码执行；
- 文献检索；
- 人类同行审查。

每次调用生成：

```text
ToolInvocationArtifact {
  capability_id
  input_hash
  tool_version
  parameters
  raw_output_ref
  replay_command_profile
  usage
}
```

CAS、SMT 或外部工具结果只有在证书被可信检查器重放后才是 `HARD_REPLAYABLE`；无证书答案保持 `HEURISTIC_EMPIRICAL` 或 `SOFT_MODEL`。

## 19. 证据与原子接收

### 19.1 证据类型

```text
HARD_REPLAYABLE
HUMAN_ATTESTED
SOFT_MODEL
HEURISTIC_EMPIRICAL
```

训练和路由按类型分池。软标签不能混入硬正确性奖励。

### 19.2 EvidenceIngest 固定顺序

1. 原始字节写入 CAS 同文件系统临时文件；
2. 流式计算 SHA-256、字节数和大小上限；
3. `fsync` 后原子 rename 到 `sha256/<prefix>/<hash>`；
4. SQLite `BEGIN IMMEDIATE`；
5. 校验 schema、scope、必需段、附件、混线和来源；
6. 追加事件、更新投影、运行 TransitionGuard、写 outbox；
7. 提交事务；
8. 返回 CommandReceipt。

不变量：

- DB 不引用未持久化 blob；
- DB 失败后的孤立 blob 延迟回收；
- 同一 request 重试不生成第二份 Evidence；
- 相同字节可共享 Artifact，但不同来源仍是不同 Evidence；
- 内容相同不等于证据独立；
- `STATUS: PROVED` 只是被解析字段，不能触发 transition；
- 防路径穿越、符号链接逃逸、压缩炸弹和超大输出；
- 接收成功、证据接受、数学晋级是三个不同事件。

## 20. 运行时能力

宿主发放：

- `CANDIDATE_WRITER`：提交候选、失败和工件；
- `VERIFIER_WRITER`：提交验证事件，不能批准自己的候选；
- `PEER_REVIEWER`：提交人工正确性裁决；
- `QUALITY_REVIEWER`：提交质量裁决；
- `CONTRACT_OWNER`：批准非实质修订；
- `USER_APPROVER`：批准改变研究对象；
- `KERNEL_OPERATOR`：执行迁移、净室、恢复和晋级命令，不拥有数学判断。

请求体 actor 字符串不构成权限。独立性由来源图计算；不能证明时为 `UNKNOWN`，审查者不得自行写 true。

## 21. 同行与质量审查

### 21.1 同行正确性

```text
PeerReview {
  reviewers[]
  independence_profile_ref
  reviewed_scope
  evidence_refs[]
  limitations[]
  verdict
}
```

新上下文只降低污染，不自动等于独立证据根。

### 21.2 质量轴

独立评价：

- 定义是否自然；
- 前提是否最小；
- 结论是否一般；
- 证明是否清晰、可维护；
- Lean 库接口、导入和性能；
- 是否接近目标期刊或会议强度。

质量接受不能制造数学正确性。

### 21.3 专家签名分型

- 可重放机械证据；
- Lean kernel 证据；
- 语义忠实人工签名；
- 同行正确性签名；
- 质量签名。

LearningFactory 只有在数据治理策略明确允许时消费人工标签。任何宣称“只消费硬轨迹”的训练池不得混入专家质量标签。

## 22. 失败与负知识

### 22.1 失败路由

```text
FORMALLY_NEGATED
EXECUTION_ERROR
PLAN_GAP
ROUTE_WRONG
UNSOLVED_HARD
NO_HIT_NOT_A_PROOF
MECHANICAL_UNBOUNDED
CONTRACT_AMBIGUITY
TRANSLATION_DEFECT
VERIFIER_MISSING
ENVIRONMENT_ERROR
```

分别触发 re-prove、re-plan、rewrite、retire、amend 或 stop，不统一重试。

### 22.2 `FailureRecord`

```text
FailureRecord {
  failure_id
  contract_version
  claim_scope
  normalized_attempt
  approach_ancestors[]
  failure_type
  minimal_failing_assumptions[]
  counterexample_or_trace_ref
  applicability_predicate
  invalidated_variants[]
  still_open_variants[]
  recovery_hint
  evidence_strength
}
```

新路线晋级前必须检索旧失败。高度匹配时需要提交 `novelty_delta`，说明对象、假设、表示、工具或关键中间命题的实质变化。

契约升级后旧失败先进入 `RECHECK_APPLICABILITY`，不永久封禁新路线。

## 23. `SIDE_FINDING`

非主线发现必须：

- 获得稳定 ID；
- 记录父运行、陈述、证据类型、相关性和通知对象；
- 默认不阻塞主流程；
- 与契约或承重依赖矛盾时升级为阻塞或契约缺陷；
- 不因有趣自动替换当前主项目。

## 24. 统一高成本扩展门

所有超过路线初始配额的操作提交：

```text
ExpansionRequest {
  expansion_type
  claim_id
  blocker_class
  supporting_approach_ids[]
  expected_new_artifact
  expected_information_gain
  verifier
  marginal_budget
  stop_condition
  cheaper_alternatives_tried[]
  fallback
}
```

`expansion_type`：

```text
RSA
ENUMERATION
DEEP_PREMISE_SEARCH
MASS_FORMALIZATION
BRIDGE_SWEEP
```

规则：

- 按小批次发放；
- 每批必须报告新增可检查对象；
- 连续两批无新可检查对象自动关闭；
- `CONTRACT_AMBIGUITY`、`LITERATURE_GAP`、`TRANSLATION_DEFECT`、`VERIFIER_MISSING`、`ENVIRONMENT_ERROR` 不能靠增加采样解决；
- RSA 只用于 `GENERATION_HARD` 且有廉价验证器的高中心性节点；
- 两条同源路线卡住不构成 RSA 开启条件。

QED-Nano RSA 默认不超过非 5.6 token 预算的 10%，初始只开一个节点。

## 25. 预算与成本

完整 EvalLifecycle 是版本/平台摊销成本，不进入单任务主预算。

单服务器 30 天硬上限：

| 用途 | GPU·时 | CPU·时 | 非 5.6 token |
|---|---:|---:|---:|
| 廉价侦察、路线生成与局部模型 | 180 | 6000 | 55M |
| LeanSearch/jixia | 20 | 200 | 5M |
| 机械证据与形式化 | 400 | 2500 | 15M |
| 净室、忠实性和回归切片 | 20 | 700 | 10M |
| 契约、文献和事件账 | 0 | 300 | 5M |
| 全局预留 | 100 | 300 | 10M |
| 总计 | 720 | 10000 | 100M |

每条路线：

- 初始 200 CPU·时；
- 只有产生新证据才追加；
- 单路线绝对上限 1000 CPU·时；
- 任一条路线不能消耗全局 CPU 上限；
- 随维数指数增长且没有维数无关证书时进入 `MECHANICAL_UNBOUNDED`。

`ResearchBudgetPolicy` 负责：

- allocation；
- reservation；
- actual；
- refund；
- route/attempt/RSA/CPU/GPU 的研究决策。

Archon 负责 session/run token 和费用的执行级熔断。ResearchKernel 从 usage 回执归集消耗，不维护第二份 token 真值。未知费用写 `cost_unknown=true`，不能按 0 计。

专家时按任务分型：

- 廉价确定性验证：4–20 小时；
- 自然语言研究证明：20–80 小时；
- 研究级 Lean 库：80–240 小时。

## 26. 并发执行模型

Archon 单 run 内不被视为并行保证。

v1 固定：

- 每条 route/attempt 使用独立目录：
  `runs/<rk_run>/routes/<route>/<attempt>/work/`；
- 每个 attempt 领取带 TTL 的 lease 并写 heartbeat；
- 崩溃后宿主回收 lease；
- 模型只能写自己的路线目录和声明写集；
- 多个 Archon run 可以并发，但不能共享文件写集；
- 共享 Lean 主线、契约和 dossier 进入串行 promotion lane；
- ResearchKernel 是单写者；
- 智能体通过 CLI 或本地 socket 提交命令，不直接打开 SQLite；
- 写集无法隔离时，允许并行思考、串行落盘；
- 分别记录逻辑并行和物理并行。

## 27. SQLite、CAS 与迁移

### 27.1 最小表

```text
runs
commands
events
contract_versions
claims
claim_edges
composition_obligations
closure_witnesses
routes
approach_roots
evidence_roots
attempts
leases
artifacts
evidence
verdict_events
peer_reviews
quality_reviews
literature_records
bridges
lean_feedback_events
failure_records
budget_events
execution_bindings
integration_outbox
```

### 27.2 SQLite 规则

- `PRAGMA foreign_keys=ON`；
- 本地磁盘使用 WAL；
- 启动确认 DB 不在 NFS/SMB；
- 使用显式 migration 版本；
- 迁移前在线备份；
- 事件 append-only，当前状态为同事务投影；
- 定期 `integrity_check`、CAS 引用检查和备份恢复演练；
- 不为假想 Postgres 建公共仓储接口。

### 27.3 LegacyImporter

旧项目只读导入：

- 稳定项目和契约；
- route、claim、artifact、evidence 和验证事件；
- 原路径、字节数、hash 和 manifest；
- 自由文本状态拆分记录 `migration_confidence`；
- 无法无歧义映射的对象进入 `NEEDS_REVIEW`；
- 源路径 + hash 为幂等键；
- 原目录不改写；
- 生成 migration report。

v1 只导入 N2 小切片：

- 一个冻结契约；
- 两条退役路线；
- 一个 `NO_HIT_NOT_A_PROOF`；
- 一个新鲜验证的路线局部结果；
- 一个 Lean 部分覆盖记录。

## 28. 安全与密钥

- 配置只保存 `secret_ref`，不保存密钥值；
- Harness 子进程使用最小环境白名单；
- 不继承无关 ambient secrets；
- stdout、stderr、异常、命令显示和 CAS 接收前执行已知值脱敏与高置信模式扫描；
- 命中密钥的工件进入 quarantine 并拒绝晋级；
- 日志不记录完整环境；
- Archon Git hook 只作第二层保护，不能是主保护；
- provider 账号只存标识和 secret reference；
- 测试植入假密钥并检查 SQLite、CAS、Git snapshot、dossier 和 transcript；
- 防路径穿越、绝对路径、junction/symlink 逃逸、超大输出和压缩炸弹。

## 29. ResearchDossier

最终卷宗至少包含：

- 最终契约版本和旧版本差异；
- 文献命中、查询族和截止日期；
- Claim 图、组合义务和 ClosureWitness；
- 路线、ApproachRoot、EvidenceRoot 和 IndependenceProfile；
- 方法卡、proof spine、哨兵和负方法记录；
- BridgeSpec、目标领域审计和回译结果；
- 证明、反例、证书和 Lean 工件；
- Lean 反馈事件和未闭合义务；
- 机械、语义、同行和质量四轴裁决；
- 失败、退役路线和 SIDE_FINDING；
- provenance、hash、工具链和环境；
- 缺失运行、错误、超时和停止原因；
- CPU/GPU/token/专家时；
- 未知费用和预算预留；
- 明确未解决部分。

同一 revision 与相同 dossier_spec 必须导出字节一致的卷宗。

## 30. 功能需求

### FR1：状态化运行

- 所有长程研究通过 RunHandle 恢复；
- `inspect` 不依赖聊天上下文；
- 暂停产生可恢复 checkpoint；
- `export` 只读持久状态；
- 所有写命令幂等并检查 revision。

### FR2：原子 EvidenceIngest

- 一个 schema 驱动接收器替代项目特定硬编码脚本；
- 支持字节、行数、hash、状态头、scope、必需段、附件和混线检测；
- CAS 与 SQLite 不产生悬空引用；
- 接收成功不等于数学接受。

### FR3：状态转移与权限

- TransitionGuard 是纯决定函数；
- 输入当前投影、命令、证据摘要和能力；
- 输出允许/拒绝、理由和缺失条件；
- 不做网络或文件写入；
- 不允许直接编辑复合状态字符串。

### FR4：Claim 图与组合

- 插边检查环；
- 父声明、契约或依赖改变时失效下游；
- 未受影响节点继续复用；
- 父 Claim 必须通过 ClosureWitness；
- 局部全绿但组合缺边时根节点保持未闭合。

### FR5：方法迁移

- 只有确定性局部节点进入 worked method card；
- 运行 SentinelSuite；
- proof spine 与 expanded proof 分开；
- 失败定位到首个义务；
- 支持合法 `AUXILIARY_STRENGTHENING`；
- 保存近失配与负方法知识。

### FR6：并行与独立性

- 先 8–20 张 scout 卡，再晋级 2–3 条深路线；
- 方法根和证据根分开；
- 独立性按五维来源图计算；
- 交叉授粉产生新 ApproachRoot；
- 同一证书重放不增加证据根数量。

### FR7：桥梁

- BridgeSpec 为一等对象；
- 正反方向义务分别记录；
- 目标领域审计与源领域回译分离；
- 单向桥梁不得冒充等价；
- 桥梁依赖进入根结论 ClosureWitness。

### FR8：Lean

- 支持检索、局部修复、快速检查、受信重放和忠实性分离；
- 产生结构化 LeanFeedbackEvent；
- 环境错误与数学错误分流；
- Lean 编译通过不得自动标记原题证明；
- 语义漂移默认回形式化节点。

### FR9：同行与质量

- 非形式化节点有合法 `PEER_ACCEPTED`；
- 正确性、质量和语义签名分开；
- 独立性不能手填 true；
- 人工标签和硬证据训练池分开。

### FR10：预算与高成本门

- 记录 reservation、actual、refund 和 unknown cost；
- 所有扩展经过 ExpansionRequest；
- 连续两批无新可检查对象自动停止；
- 机械无界路线熔断。

## 31. 非功能需求

- 可恢复：进程、模型会话或网络中断后不丢数学状态；
- 可审计：任何晋级都可追到命令、事件、工件、证据类型和组合见证；
- 局部性：更换模型、LeanSearch 或 Harness 只改适配实现；
- 安全：密钥不进入 SQLite payload、CAS、Git、日志或 dossier；
- 可测试：主要测试只通过 ResearchKernel 接口；
- 一致性：同 request 幂等，同 revision 导出稳定；
- 可迁移：旧工件只读导入，不改写原文件；
- 可弃权：预算耗尽或证据不足时诚实结束；
- 可解释：路线选择、方法拒绝、桥梁损失和形式化选择都有数学理由；
- 性能：单工作区写入由单写者串行化，读取和外部执行可并发。

## 32. 实施切片

### Slice A：内核最小闭环，6–9 人日

实现：

- `create/apply/inspect/export`；
- SQLite + CAS + append-only event + 当前投影 + outbox；
- Contract、Claim、Route、Artifact、Evidence、四轴 verdict；
- 幂等键、revision、capability；
- EvidenceIngest 原子协议；
- TransitionGuard；
- 确定性 dossier。

先不接真实模型。用假 Harness 验证非法晋级、崩溃恢复和契约失效。

### Slice B：自然语言路线 + 机械路线，3–5 人日

- Archon CLI JSON 薄适配；
- 一个 Rethlas 候选 attempt；
- 一个确定性程序或简单 Lean 项目；
- 结果统一进入 EvidenceIngest；
- Rethlas verifier 保持 SOFT_MODEL；
- 宿主 `CLEAN_REPLAY_LOCAL`。

### Slice C：并行、隔离与故障恢复，3–5 人日

- 两个独立 route 目录；
- 多 execution binding；
- lease、heartbeat 和回收；
- 重复接收同一输出；
- 杀死一个外壳不影响另一条路线；
- promotion lane 串行写共享 Lean 主线。

### Slice D：组合、方法卡与桥梁，4–6 人日

- CompositionObligation 与 ClosureWitness；
- 12–20 张 worked method card 的 schema 和首批少量实卡；
- SentinelSuite；
- ApproachRoot/EvidenceRoot/IndependenceProfile；
- BridgeSpec 和目标领域回译流程；
- FailureRecord。

首个 v1 可以只装入 3–5 张真实方法卡，完整 12–20 张作为 v1 内容建设并行完成，不阻塞内核代码闭环。

### Slice E：Lean 双向反馈与工具注册，3–5 人日

- jixia；
- LeanSearch public/retriever-only；
- LeanFeedbackEvent；
- CapabilityRegistry；
- 一个 SMT、一个 CAS 和精确枚举能力；
- 单卡批式 rerank 作为非阻塞增强。

### Slice F：验收、安全与 N2 小切片，3–4 人日

- 崩溃点、并发、安全和契约测试；
- 假密钥植入；
- N2 小切片只读导入；
- 三类端到端验收；
- 冻结 v1 dossier。

### 工期

可信内部 v1：22–34 人日。

前提：

- 单工作区；
- ResearchKernel 单写者；
- 路线目录隔离；
- Archon CLI 薄适配；
- N2 小切片迁移；
- LeanSearch 允许退化；
- 只承诺 `CLEAN_REPLAY_LOCAL`。

额外项：

- 本地单卡双 8B 批式完整服务：+3–6 人日；
- 完整 N2/M5/M6 历史迁移：+3–7 人日；
- 独立敌对 runner：+3–6 人日；
- 多项目长期兼容：v2。

## 33. 验收标准

### AC1：接口与状态安全

- 相同 request 重试返回同一 receipt；
- stale revision 返回 `REVISION_CONFLICT`；
- actor 字符串不能冒充 capability；
- 伪造 `STATUS: PROVED` 不能晋级；
- 空 placeholder、混线、错 scope 和缺附件被拒绝；
- 软 judge 不能提升 machine axis。

### AC2：存储与崩溃一致性

在临时文件写入、rename 前后、DB commit 前后、outbox 前后和 Archon 返回前后强制终止：

- 不存在状态晋级但工件缺失；
- 不重复产生契约版本、Evidence 或同行签名；
- CAS 孤儿可回收；
- outbox 可幂等重投。

### AC3：契约修订

- 修改量词生成新版本；
- 旧分支保留并 superseded；
- 受影响节点失效；
- 未受影响节点可重验复用；
- 契约缺陷有提出、批准和用户升级路径。

### AC4：组合闭包

- 所有叶子都由 Lean 通过，但缺一条组合边时，根 Claim 不得晋级；
- 方向反转、契约版本不一致、桥梁单向时拒绝闭合；
- HYBRID 证明明确机械、人类和开放部分；
- 补边后只重验依赖闭包。

### AC5：人类证明思路迁移

- 至少三类确定性局部节点使用完整 worked method card；
- 每张卡包含 proof spine、决定性选择、推进量、锋利例、失效条件和闭合义务；
- 运行逐项前提映射、近失配和 SentinelSuite；
- 允许合法辅助强化，增加假设必须被拒绝或 amend；
- 局部成立、全局未闭合时不得进入 `ROUTE_PROVED`；
- 相对“无方法卡”和“只有方法名”基线，独立验收率提高或无效晋级/验证成本下降至少 30%；
- 正例至少 6/8 成功；
- 近失配至少 7/8 在长证明前拒绝；
- 粘合陷阱不得错误 `ROUTE_PROVED`。

### AC6：独立性与污染

- 三个共享错误草稿的不同模型不能形成三条独立路线；
- 交叉授粉前后 ApproachRoot 分开；
- 一个证书两次重放只有一个 EvidenceRoot；
- 五维独立性可从来源图计算；
- 未知祖先不得自动算独立。

### AC7：桥梁

- 无映射的术语换皮被拒绝；
- 缺目标领域必要假设时保持 CANDIDATE 或 REFUTED；
- 单向桥梁只能 `ONE_WAY_VALID`；
- 回译后量词、对象类和成功范围逐项对齐；
- 桥梁不能绕过根结论组合门。

### AC8：Lean 双向反馈

- 缺侧条件产生结构化 LeanFeedbackEvent；
- 只新增或回退受影响 Claim；
- 工具环境损坏进入 `ENVIRONMENT_ERROR`；
- LeanSearch 强假设命中不能直接采用；
- clean replay 与语义忠实分别通过；
- `sorry`、公理越界和高信任 `native_decide` 被拒绝。

### AC9：广搜、工具和高成本门

- 至少 12 张 scout 卡中术语换皮被合并；
- 晋级集合覆盖至少两种表示、两个工具族和两种验证终点；
- 无证书 CAS 结果不能晋级；
- 同源双卡点不能开启 RSA；
- 连续两批只有解释文本时关闭扩展；
- 大枚举同样必须经过 ExpansionRequest。

### AC10：失败负知识

- 同一失败桥梁换五种术语后命中同一 FailureRecord；
- 没有 novelty_delta 不得重复续费；
- 契约改变后旧失败进入适用性重审；
- 负知识不得永久封死确实解除反例的新路线。

### AC11：并发与安全

- 两路线目录物理隔离；
- lease holder 崩溃可恢复；
- 12 个进程提交同一 request 不重复；
- reviewer 与 amend 竞争 revision 时只有合法一方成功；
- 假密钥不出现在 DB、CAS、Git、dossier 或 transcript；
- 路径逃逸、压缩炸弹和命令注入被拒绝。

### AC12：诚实终态

- 未形式化但经独立同行接受的证明可 `PEER_ACCEPTED`；
- 仅有限无命中保持 `NO_HIT_NOT_A_PROOF`；
- 无完整证据可 `UNRESOLVED`；
- SIDE_FINDING 不自动替换项目；
- finalize 后 dossier 不因后续运行改变。

## 34. 测试策略

### 34.1 主要测试面

测试通过 ResearchKernel 外部接口。TransitionGuard 和 CompositionGuard 可补纯函数性质测试。外部工具使用录制或假适配器。

旧浅模块单元测试在深接口测试覆盖后删除，不继续叠加。

### 34.2 必须有的确定性测试

- 任意软证据组合不能产生 machine verified；
- contract 改量词只失效依赖闭包；
- `PEER_ACCEPTED` 不制造 kernel 或 quality 状态；
- CAS 相同内容去重但 provenance 不合并；
- approach 同源时独立性不得变 true；
- root 组合开放时无法晋级；
- finalize 后 dossier 字节稳定。

### 34.3 并发和故障测试

- SQLite busy、磁盘满、只读文件系统；
- lease holder 崩溃；
- outbox 重复投递；
- Archon 超时、部分输出、错误 JSON；
- usage 缺失和 provider 限流；
- clean replay 环境漂移；
- 适配器版本不兼容；
- 子进程树未退出和孤儿进程。

### 34.4 真实模型烟测

真实模型烟测与确定性测试分开，不作为单元测试前提。烟测只验证适配器和成本，不给模型输出授予数学权限。

## 35. 可观测性

每个命令和事件至少包含：

```text
rk_run_id
revision
command_id
trace_id
contract_version
route_id?
claim_id?
attempt_id?
actor_capability
adapter
adapter_version
artifact_ids[]
outcome
failure_code
usage
```

指标：

- 队列等待；
- lease 过期；
- 重试；
- adapter timeout；
- 预算 reservation/actual；
- EvidenceIngest 拒绝原因；
- 非法 transition；
- CAS 孤儿；
- outbox 延迟；
- clean replay 通过率；
- 去重前后路线数；
- 独立 ApproachRoot/EvidenceRoot 数；
- 每百万 token 新增可检查对象数；
- FailureRecord 阻止的重复成本；
- Lean 反馈最终闭合的新义务数；
- 桥梁回译保留/否决比例；
- 根结论闭合度变化。

模型原始文本只保存在 Artifact，不复制进每条结构化日志。

## 36. 风险与缓解

| 风险 | 缓解 |
|---|---|
| 局部结果堆成假全局结论 | CompositionObligation + ClosureWitness |
| 方法卡退化成技巧标签 | proof spine、近失配和 SentinelSuite |
| 多模型制造同源多数 | 五维 IndependenceProfile 与污染传播 |
| 跨领域只换术语 | BridgeSpec、目标领域审计和回译 |
| Lean 证明错题 | 契约、反译、可满足性和变异测试 |
| Lean 只成末端编译器 | LeanFeedbackEvent 反馈新义务 |
| 生成方控制验证环境 | 预注册 verifier profile |
| 多存储分叉 | 三个唯一所有者与 outbox |
| 并行写坏共享主线 | 路线目录隔离和 promotion lane |
| 计算爆炸 | 全局预算、单路线 1/10、统一扩展门 |
| 专用脚本不可维护 | schema 驱动 EvidenceIngest |
| 失败重复消耗 | FailureRecord + novelty_delta |
| 密钥进入工件 | secret_ref、最小环境、扫描和 quarantine |
| LeanSearch 单卡承诺失真 | public/retriever-only/批式退化 |
| 旧状态迁移误升格 | 只读小切片、confidence 和 NEEDS_REVIEW |
| 人工质量标签污染硬训练池 | 证据分池与专家签名分型 |

## 37. 分期

### v0：内核原型

目标：Slice A。

完成条件：

- 四入口接口；
- SQLite/CAS；
- 幂等、revision、capability；
- 原子接收；
- 组合门最小实现；
- 确定性 dossier；
- 全假适配器测试。

### v1：可信内部版本

目标：Slice A–F，22–34 人日。

必须包含：

- 一条自然语言路线和一条机械路线；
- 两路线隔离和故障恢复；
- CompositionObligation；
- 方法卡和 SentinelSuite；
- 独立性来源图；
- BridgeSpec；
- LeanFeedbackEvent；
- 同行/质量正交；
- N2 小切片迁移；
- 三类端到端验收。

### v1.1：非阻塞增强

- 单卡批式 LeanSearch 完整 rerank；
- 12–20 张完整方法卡全部录入；
- 更多 SMT/CAS 能力；
- 更完整的桥梁和失败目录；
- 更强的性能与成本监控。

### v2：长期稳定版

- 多项目稳定恢复；
- 完整 N2/M5/M6 迁移；
- 故障注入平台；
- 独立敌对 runner；
- EvalLifecycle 摊销评测；
- BridgeCatalog；
- 可选 4B/7B 后训练数据导出；
- 多版本兼容。

## 38. 相对 `RK-PRD-1` 的采纳与拒绝

### 38.1 采纳

- Claude 建议中的 EvalLifecycle 摊销化、单路线预算收紧、Truth/Quality 正交、语义漂移回形式化节点、专家签名分型、契约缺陷出口、状态化接口、文献命中、忠实性硬门、声明式净室、SIDE_FINDING 非阻塞和 RSA 开启判据；
- 三份评审的全部 P0；
- 适合 v1 的方法卡、广搜、能力注册、统一扩展门、负知识、关键节点形式化、源/目标双词文献检索、SQLite 迁移、保守导入和数学状态可观测性；
- 工程评审的 22–34 人日最小可信切片。

### 38.2 拒绝或后置

- 不把所有自然语言证明强制 Lean 化；
- 不把 Archon `max_parallel_sessions` 当成已实现物理并行；
- 不新造 ReasoningAdapter；
- 不为 SQLite 预建 Postgres 仓储接口；
- 不把独立性压成一个人工布尔值；
- 不在 v1 承诺常驻单卡双 8B 服务；
- 不在 v1 全量迁移旧 N2 历史；
- 不把新目录或约定式只读冒充敌对净室；
- 不因两个同源路线卡住就开启 RSA；
- 不用模型输出长度、投票或置信度衡量并行收益。

## 39. 冻结实施决策

本版本固定：

1. ResearchKernel 是唯一面向调用者和测试的深模块；
2. 外部接口为 `create/apply/inspect/export`；
3. SQLite 是数学状态唯一写者；
4. CAS 保存不可变原始工件；
5. Archon 只拥有执行、代码和协作来源；
6. Harness 承载模型执行，Rethlas 是不可信策略；
7. 父 Claim 必须有 ClosureWitness；
8. 方法卡必须有 proof spine 和 SentinelSuite；
9. 跨领域必须用 BridgeSpec；
10. 独立性按五维来源图计算；
11. Lean 产生双向研究反馈；
12. 候选并行、晋级串行；
13. 净室命令由宿主预注册；
14. 所有高成本操作走 ExpansionRequest；
15. v1 为单工作区、单写者、内部使用；
16. 可信 v1 工程量为 22–34 人日；
17. `prd1.md` 永久保留为历史基线；
18. 后续产品范围以 `product/product-prd.md` 为唯一最新入口；若实施偏离冻结决定，必须回修该规范或建立 ADR，不得静默漂移。

## 40. 开工顺序

严格按以下顺序：

1. Slice A：不接真实模型，先证明内核不会被伪造状态绕过；
2. Slice B：接一软一硬两条路线；
3. Slice C：验证隔离、幂等和故障恢复；
4. Slice D：加入组合门、方法卡、独立性和桥梁；
5. Slice E：接 Lean 双向反馈和工具注册；
6. Slice F：安全、N2 小切片和三类验收。

任何阶段如果需要新增公共接口，先做删除测试：删除该新模块后复杂性是否重新散落到多个调用者。若没有，它是浅模块，不加入架构。
