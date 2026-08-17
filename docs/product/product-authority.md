# RK 可视化产品权威与状态机规格

状态：`NORMATIVE_TARGET / IMPLEMENTATION_REQUIRED`  
版本：`RK-PRODUCT-AUTHORITY-1.1`  
日期：2026-08-13

## 1. 目的、范围与唯一真值

本规格定义桌面/Web 工作台接入 RK 时唯一允许的权威语义。它覆盖命令、查询、材料、文献、题池、
路线策略、动态 Worker、Claim、事实图、工具、恢复、审查、研究闭合和论文发布。前端布局、颜色和组件库不在
本规格内，但任何界面都必须遵守这里的状态和术语。

`ProductAuthority` 是 `ResearchProduct` 内部的权威深模块，不是第二个公共产品入口。它只向
`ResearchProduct` 实现暴露三个内部接口：

```text
submit(CommandEnvelope, PrincipalCapability) -> ProductReceipt
query(QueryEnvelope, PrincipalCapability) -> AuthorityView
read_artifact(ArtifactRequest, PrincipalCapability) -> ArtifactChunk
```

复杂性——角色判定、并发、失效、晋级、闭包和发布门——全部留在该模块内。公共调用者只使用
`product-architecture.md` 定义的一个 `ResearchProduct` 模块；它把 `command/query/artifact` 归一化
后调用上述内部接口，并负责 create/list、事件订阅和部署操作。HTTP Gateway、CLI、图形前端和产品
测试必须走 `ResearchProduct`，不得直接调用 `ProductAuthority`、repository、guard、SQLite、CAS、
execution ledger 或图内部对象。事件投影只读取 `ProductAuthority` 已裁决的权威状态和正式活动事件，
不反向参与裁决。

唯一业务真值是 `ResearchKernel` 事务治理的权威状态体系。SQLite、CAS 和 execution ledger
分别承载状态、不可变字节与执行回执，由同一内核解释和关联；它们不是三套可任选的真值。
读模型、搜索索引、通知、图坐标、聚类和 Gateway 缓存均为可删除重建的派生实现，不得产生
Claim 裁决、权限、预算、下一动作或论文状态。

本规格不引入重复签名、第二套**权威**事件账、Gateway 幂等库、敌对容器或通用消息总线。产品可有
一个只承载可观察 Worker/工具/作业活动且不参与数学裁决的活动流。它只保留数学
正确性、角色分离、实际重连和副作用至多一次所必需的约束。

## 2. 不变量

下列不变量在所有入口、恢复路径和部署形态下成立：

1. 前端只提交用户原意命令，只渲染内核投影及 `available_actions`；它不维护第二状态机。
2. Gateway 不持有万能 capability，不更换 principal，不代签，不补写或推导任何权威字段。
3. Claim 工作流与 `VerifiedFactGraph` 是不同投影；“可见”不等于“可依赖”。
4. 工具执行成功与数学晋级是两个独立事件；退出码为零不产生事实。
5. 所有晋级只由内核在统一验证门后产生；Worker、Verifier adapter 和 UI 均不能写事实图。
6. 隐藏思维链、provider reasoning、scratchpad、raw completion 和内部 prompt 不进入事件账、
   读模型、搜索、诊断包或卷宗。
7. `PROVED`/`DISPROVED` 的论文交付链固定为：有效 ClosureWitness → 唯一 ROOT terminal →
   Finalize(CLOSED/final_outcome) → 从 finalized snapshot 生成候选 TeX → 独立整篇复核 → 该精确 TeX 编译的 PDF。
8. revision、contract 或承重 digest 变化后，旧 checkpoint、回执和审查不能静默复用。
9. run 关闭后不可重开；继续研究创建新 run 并引用旧卷宗。
10. 材料提取、文献命中、Matlas 返回、领域距离评分和模型研究稿都是来源或候选，不是数学事实；
    只有经当前合同统一验证门接受的原子 Claim 才能进入有效事实图。
11. “在线查询”“历史响应重放”“证书导入核验”“不给历史结论的净室重新发现”是不同 provenance，
    不能合并成同一成功状态。
12. 无检索命中、桥评分高、专家任务已创建或批量任务成功退出均不能产生新颖性或数学 verdict。

## 3. 身份与命令权限

### 3.1 窄身份

`PrincipalCapability` 是宿主签发并由内核验证的窄凭证。请求正文中的 `role`、`actor`、
`is_admin`、`capability` 等自报字段没有权限意义，出现时按 schema 拒绝。一个浏览器会话可以切换
已登录身份，但一次命令只能携带一个 principal；Gateway 不能把不同身份合并为一个主体。

| 身份 | 可以做 | 明确禁止 |
|---|---|---|
| `MAIN` | 创建/冻结/修订合同，启动/暂停/恢复，路线与高层提示，撤销请求，提交闭包命令，Finalize，导出 | 注册 Claim/Edge，制造验证或审查结论，直接晋级事实 |
| `LITERATURE_REVIEWER` | 核查材料提取、来源版本、定理适用性、题池语义和新颖性覆盖边界 | 把无命中写成新颖，替代数学 Verifier，直接写有效事实图 |
| `WORKER` | 提交一个原子 Claim、逻辑前驱和候选证据，请求已注册工具，修订被拒 Claim | 调用 `PromoteClaim`，写事实图，Finalize，伪装 Verifier |
| `MACHINE_VERIFIER` | 对精确绑定输入执行预注册 verifier/checker，提交不可变执行回执 | 改 Claim，填写人工审查，直接写事实图 |
| `PEER_REVIEWER` | 以独立身份签署结构化 Claim/组合审查工件 | 作者身份审查自己的对象，提交 Claim，改图，Finalize |
| `PAPER_REVIEWER` | 在 Finalize 后对精确候选 TeX 和闭包签署整篇复核工件 | 修改 TeX、代作者补证明、发布 PDF、复用旧 digest |
| `PUBLICATION_WORKER` | 确定性生成候选 TeX；仅从已接受整篇复核的同一 TeX 编译 PDF | 提交审查、改 TeX 正文、改合同/图/ROOT/outcome |
| `ADMIN` | 部署配置、能力探测、诊断和身份管理 | 获得数学晋级特权或替代 Reviewer 结论 |
| `GATEWAY` | 验证会话后透传当前 principal，映射传输格式，返回查询和事件 | 持有跨角色 capability、生成签名、补权威字段、直写存储 |

按钮隐藏只是体验优化。原始 HTTP、过期页面和构造请求必须由内核运行时再次拒绝。

### 3.2 非数学工作流对象的权威边界

`MaterialExtraction`、`SourceSnapshot`、`CitationAnchor`、`PriorArtComparison`、`ProblemCandidate`、
`BridgeOpportunity`、`AblationAssignment` 和 `ResearchCaseLineage` 是由 `ResearchProduct` 管理、可追溯且
可查询的研究工作流对象，不是第二套数学真值。它们必须绑定来源 ArtifactRef、创建身份、run 或
deployment scope、schema/version 和活动 cursor；状态只描述抓取、提取、人工冻结、分配、执行或阻塞。

这些对象只能通过类型化命令生成候选 Claim、RouteProposal 或 BridgeSpec；真正的 statement、依赖边、
验证结果与晋级仍由 `ResearchKernel.apply` 裁决。文献审查者可确认引用、适用性与覆盖边界，但不能用
工作流状态生成 `VALIDATION_ACCEPTED`。专家或作者确认作为签名/归属明确的审查工件保存，不自动替代
当前合同的 Claim 验证门。

外部来源返回必须冻结 endpoint、请求时间、请求摘要、原始响应 ArtifactRef、响应摘要、可见服务版本和
覆盖声明。`LIVE_QUERY` 与 `REPLAYED_SNAPSHOT` 使用不同状态；重放保持可复现，不声称外部服务当前仍在线。
arXiv 文本、撤稿和版本变化产生新来源版本，不能原地改写旧引用锚点。

### 3.3 签名审查工件

签名正文采用确定性 schema；adapter 只验证签名、身份、独立性、精确 binding 和字段完整性，
不得生成答案。`COMPOSITION_REVIEW` 必须由签名正文逐字段直接提供：

```text
schema_version, review_id, verifier_identity_id, issued_at
binding {
  run_id, kernel_revision, contract_version, claim_id, statement_hash,
  selected_subgraph_digest
}
independence {
  blind_review, author_subject_ids[], saw_other_verdicts
}
verdict
checks {
  proof_checked, scope_checked,
  coverage, compatibility, invariant, progress, boundary, simultaneous_choice
}
signature
```

每个 check 都必须是签名对象中的完整结构化结论，而不是 UI 默认值。任一 check 缺失、
`passed=false`、status 非 `HUMAN_ATTESTED`、结论为空或 evidence refs 不合格，都令
`promotion_eligible=false`。`blind_review`、`author_subject_ids` 和身份独立性也只能来自签名工件
与宿主身份谱系的核对结果，Main、CLI、Gateway 和 adapter 不得代填。

ClosureWitness 只接受 `trust_class=MANAGED_PEER_REVIEW`、
`authority_effect=PEER_PROMOTION_ELIGIBLE` 且 `promotion_eligible=true` 的当前受管审查。
`UNMANAGED_REVIEW`、`authority_effect=NONE`、软 verifier 或普通意见只能作为反馈。全 MACHINE
模式走预注册 checker/Lean 的机器门，不用人工字段伪装。

`ATOMIC_CLAIM_REVIEW` 必须逐字段签署 `proof_checked`、`scope_checked`，并精确绑定 run、合同、
Claim、statement hash 和审查者独立性。`PAPER_REVIEW` 同样逐字段签署 `mathematical_consistency`、`dependency_closure`、
`claim_statements`、`proof_bodies`、`citations`、`revoked_facts_excluded`，并绑定第 9 节全部摘要。

材料提取差异、定理适用性、开放题语义冻结和专家/作者确认使用各自独立 schema；它们可由
`LITERATURE_REVIEWER` 或外部主体签署，但不存在 `authority_effect=PEER_PROMOTION_ELIGIBLE` 字段，也不能
被 adapter 映射成 Claim/组合/PAPER review。若其中提出数学结论，须另建原子 Claim 并走对应审查门。

## 4. 少而深的命令接口

### 4.1 CommandEnvelope

```text
ProductCommandEnvelope {
  schema_version,
  request_id,              # 调用方一次用户意图的稳定 UUID；重试不变
  scope:
    { kind: GLOBAL, deployment_id } |
    { kind: RUN, run_id, expected_revision, expected_contract_version } |
    { kind: DEPLOYMENT, deployment_id, expected_deployment_revision },
  command { type, payload },
  artifact_inputs[]
}
```

前端 SDK 只提供类型化构造器，最终全部进入一个 `submit`。业务命令沿用
`docs/spec/api.md` 的冻结命令名；UI 不另造“快速接受”“强制完成”或“直接保存事实”。

首次接收后，内核生成 `command_id`。普通命令以 `(run_id, request_id)` 为幂等范围；CREATE_RESEARCH
在 run 尚不存在时以 `(deployment_id, verified_principal_subject_id, request_id)` 为范围；subject 只从
已验证 session/capability 派生，调用者不可自报。成功 decision 返回
`created_run_id`。同一范围/request_id 且 digest 相同的重放返回同一 receipt_id 的当前 ProductReceipt
投影，不再次执行；同 ID 不同 digest 返回 `IDEMPOTENCY_KEY_REUSED`。
Gateway 不建第二幂等账。revision 冲突时不得自动换 revision 重放状态改变命令；调用者必须
重新查询并由用户或编排器重构新意图、新 request ID。

### 4.2 ProductReceipt

```text
ProductReceipt {
  schema_version, receipt_id, receipt_version, request_id, command_id?, scope,
  run_id?, deployment_id?, updated_at,
  state: PENDING | DECIDED | OUTCOME_UNKNOWN,
  job_id?,
  decision? {
    accepted, rejection_code?, missing_conditions[],
    revision_before, revision_after, contract_version,
    event_cursor_after, affected_entity_ids[], created_artifact_refs[], created_run_id?,
    kernel_receipts[]
  },
  unknown_external_call_ref?, supersedes_or_resolves_receipt_id?
}
```

`PENDING` 只证明持久产品作业已收；`DECIDED` 必须含不可变内核决定，接受/拒绝只读取
`decision.accepted`；`OUTCOME_UNKNOWN` 只用于远端副作用无法确认。不得另造 RECEIVED、ACCEPTED、
COMMITTED 产品真值，HTTP 202 不是状态真值。长操作的接收回执不能显示为“已验证”或“已完成”；
最终业务状态由 snapshot/event 给出。ProductReceipt 是一个持久操作记录：状态只允许
`PENDING -> DECIDED | OUTCOME_UNKNOWN`；DECIDED 的 decision 一旦产生即不可变。OUTCOME_UNKNOWN 对原
外部 attempt 是终态；查询上游、导入回执或新 attempt 使用新 request_id 并引用
`supersedes_or_resolves_receipt_id`。查询和同 digest 命令重放返回同 receipt_id 的**最新规范投影**，
不是首次 HTTP 响应的旧字节，也绝不重新执行；只有同 receipt_version 的序列化要求字节稳定。

### 4.3 available_actions

每个可操作实体的权威查询返回：

```text
available_actions[] {
  command_type,
  principal_subject_id,
  target_ids[],
  expected_revision,
  expected_contract_version,
  required_inputs[],
  blocked_by[]
}
```

它由内核 guard 对当前 snapshot 计算。前端只能渲染、分组和翻译它；不能按本地阶段、绿色徽标、
LLM 建议或旧事件自行解锁动作。真正提交时内核仍重新判定。

## 5. Claim 工作流与有效事实图

### 5.1 两个投影

`ClaimWorkflowProjection` 保存研究过程中所有 Claim 的谱系和反馈：

```text
CANDIDATE -> PENDING_VERIFICATION -> ACCEPTED | REJECTED
ACCEPTED -> INVALIDATED | REVOKED | SUPERSEDED
REJECTED -> 新 Claim（SUPERSEDES 旧 Claim），不原地改正文
```

候选、待验证、拒绝、撤销、失效和 superseded Claim 可以在历史/谱系界面显示，但不能成为
可依赖节点。它们的边是 provenance/lineage，不是有效数学依赖。

`VerifiedFactGraph` 只包含同时满足以下条件的当前有效事实：

- Claim 属于当前合同版本且 lifecycle 为 ACTIVE；
- statement、前驱和证据精确绑定；
- 通过统一验证门且 `promotion_eligible=true`；
- authority_effect 满足该 Claim 类型的合同要求；
- 所有依赖事实仍有效，所有 BridgeSpec 方向与组合义务当前；
- 未撤销、未失效、未 supersede。

内核在一次事务中产生晋级 verdict/event 并更新派生图；Worker、Gateway、图查询和 verifier
adapter 都没有 `fact_graph_write` 接口。搜索必须返回 `claim_authority_class`，界面把工作流命中
与有效事实命中分栏，不用同一个绿色节点混淆。

同一 run 中相同 `stable_label` 与不同 statement/content digest 必须返回
`STABLE_LABEL_CONFLICT`。语义修订用新 claim_id 和显式 `SUPERSEDES`；不得最后写入覆盖。

模型研究稿、OCR 文本、Matlas theorem 命中、arXiv 开放陈述和历史项目笔记进入时只形成工件或
`CANDIDATE` 工作流对象。规范化器可以提出 statement、前驱和 Claim 类型，但不得补造证明、验证结论
或 promotion eligibility。自动 verifier routing 只能从当前合同允许的注册能力、Claim 类型和明确验证
策略选择；工具结果回到谱系后，仍由统一内核门决定拒绝、待修或晋级。组合义务与
ClosureWitness readiness 必须从内核接受事件投影，不能由模型“总结已闭合”。

Zhao 历史产物、`N2_AJT5` 手工研究与其他旧证据只能进入 `ResearchCaseLineage` 和候选工件。
`CLEAN_ROOM_REDISCOVERY` 的 Worker 输入清单不得包含被测历史结论、证明正文或证书；
`IMPORTED_CERTIFICATE_VERIFICATION` 必须列出每个导入工件和 verifier receipt。两者不得共用一个
“发现成功”标签，任何导入 Claim 仍须经过当前合同验证门。

### 5.2 失效与撤销

合同、statement、逻辑边、BridgeSpec、证据或事实状态发生承重变化时，内核沿当前有效图计算
反向依赖闭包；闭包内派生 verdict、ClosureWitness、义务 discharge 和论文资格失效，闭包外
sibling 保留。历史事件和工件永不删除。

所有影响数学资格的失效 intent 必须由 kernel guard 在承重命令事务内计算，并写入权威 invalidation
ledger/watermark；同一事务立即使旧 review、witness 和 publication binding 不可消费。产品失效消费者
可在后续同库事务物化 queue/checkpoint/tool feedback 的 UI 状态，但在
`invalidation_watermark < kernel revision/cursor` 时，ProductAuthority 对相关 command 和
available_actions 返回 `AUTHORITY_PROJECTION_LAG`，不得执行旧工作。消费者按 event_id 幂等追赶，
崩溃后可恢复；不把跨两个事务虚构为原子。

撤销是两步内核操作：

```text
preview_revoke(fact_id) -> {
  preview_revision, contract_version, target_fact_digest,
  affected_fact_ids[], reopened_obligation_ids[], preserved_sibling_ids[]
}
confirm_revoke(preview binding, reason_artifact)
```

确认命令必须原样绑定预览 revision、合同版本、目标 digest 和闭包 IDs；期间任何相关变化都返回
`REVOCATION_PREVIEW_STALE`，要求重新预览。最终集合由内核在同一事务重算并比对，前端不传入
“希望撤销的下游列表”作为权威。

## 6. 工具、验证与数学权威

工具调用有独立状态：

```text
QUEUED -> RUNNING -> SUCCEEDED | FAILED | CANCELLED | STALE
```

`SUCCEEDED` 只表示适配器/进程按协议完成。工具回执必须记录精确输入、profile/version、退出码、
输出工件、时间和 authority ceiling。Python、CAS、枚举和软 verifier 默认
`NO_FACT_GRAPH_WRITE/SOFT_TOOL_RESULT`；Lean 或确定性 checker 也只有在证书、环境、目标、合同和
statement 全部通过验证门后，才另行产生 `VALIDATION_ACCEPTED`。界面必须并列显示 invocation
status 与 authority status，不能合成一个绿勾。

模型和工具事件只允许以下公开数据：assignment、输入工件引用、结构化 tool call/result、候选
工件、Claim、验证反馈和状态转换。provider reasoning、隐藏 scratchpad、raw completion、内部
prompt 不得持久化。显式 `public_summary` 只能帮助阅读，不驱动状态、权限、晋级或下一动作。
stdout/stderr 是普通执行工件，不是思维链或数学结论。

能力目录只能按当前部署的真实探测和 ToolRun 着色，至少区分 `CONFIGURED_UNPROBED / AVAILABLE /
SMOKE_ONLY / PRODUCT_RECEIPT_AVAILABLE / UNAVAILABLE / EXTERNAL_BLOCKED`。其他 provider 或旧版本成功不能
替具体能力变绿。模型/工具回执须记录 provider、model、build/version、profile、函数 schema、usage、
费用和输出工件；没有外部副作用或执行层失败的调用只能显示失败，不能从模型文字推断“工具已执行”。

Matlas、OpenAlex、Crossref、arXiv 和其他文献连接器进入同一个 ToolCatalog/ToolRun，不另设文献执行账。
Matlas 命中只提供 theorem 候选；按 arXiv ID 获取的原文、定理局部上下文和假设适用性审查分别成为
绑定工件。作者—论文—定理—引用图的边带 source connector 和 response snapshot，不能把 RK 组合边标成
Matlas 原生边。外部服务无命中、504 或 schema drift 都是有回执的执行结果，不是数学或新颖性结论。

远域选择器对领域距离、成熟度、缺席度、工具优势、证书压缩、映射损失、回译成本和死亡测试的计算
只产生 `BridgeOpportunity` 排序。只有映射、方向、假设损失、目标域审查和回译义务齐全后，现有
BridgeSpec 命令才可接受候选桥；高分本身没有 authority effect。消融组终局 verdict 必须来自冻结的
同一离线/源侧 verifier，模型自评不能进入结果列。

批量 arXiv 流水线的冻结题池和完整分母是不可静默重写的研究记录；排除、失败、阻塞和撤稿均留在
谱系中。语义冻结必须记录人工抽查身份与来源版本。专家/作者确认未到位时显示
`EXTERNAL_CONFIRMATION_PENDING`，不能在报告中折算成成功。

## 7. Worker、执行尝试与恢复

三个 ID 不得混用：

- `work_item_id`：checkpoint 中一个逻辑任务；
- `worker_run_id`：Worker 对该任务的一次角色执行；
- `attempt_id`：宿主隔离执行/工具绑定的一次尝试。

一个 work item 可以有多个 worker run，一个 worker run 可以发起多个受管 attempt。恢复同一外部
pending execution 时沿用其 attempt；确需重启执行时创建新 attempt 和 attempt ordinal。所有输出
Claim、tool call、feedback 和工件绑定这三个 ID、创建时 revision 与合同版本。

活动状态只由真实转换事件产生：

```text
QUEUED -> RUNNING -> WAITING_TOOL | WAITING_REVIEW | PAUSED
RUNNING/WAITING_* -> COMPLETED | FAILED | CANCELLED | STALE
PAUSED -> RUNNING | CANCELLED | STALE
```

Activity projector 不从日志文字猜测状态，也不能自行把 Worker 标为完成。相同执行副作用由
execution ledger 和 request ID 去重；历史失败 attempt 不覆盖逻辑任务的后续成功状态。

checkpoint、queue item、pending tool call/feedback、`composition_closed`、review task 和待执行
人工动作必须绑定创建时：

```text
run_id, kernel_revision, contract_version,
input_snapshot_digest, consumed_entity_digests[], checkpoint_id
```

合同修订、事实撤销、ROOT/闭包变化或 consumed digest 改变后，内核把不再匹配者标为
`STALE/INVALIDATED`，重新开放相应义务；Gateway 不得迁移、重放或静默套用。恢复返回明确的
有效队列、失效项和修复动作，不丢 verifier feedback，也不重复写事实。

## 8. 查询、快照与事件游标

`query` 使用一个 tagged `QueryEnvelope`，不暴露内部表；下列是四个权威读族而非四个公共方法：

```text
RunView(run_id)
EventView(run_id, after_cursor, limit)
FactView(run_id, center/search/filter/depth/cursor)
ArtifactMetadata(artifact_id)
```

材料、SourceSnapshot、LiteratureGraph、ProblemPool、BridgeOpportunity、Ablation 和 ResearchCaseLineage
由 `product-architecture.md` 的 QuerySpec variants 进入同一个 query。它们返回 provenance、工作流状态和
当前 fence，不具备 fact promotion 字段；ProductAuthority 只负责身份、binding 和“不得越权生成数学
结论”的统一门，不把这些读取复制成第二套页面 endpoint。

`RunView` 是一个原子 snapshot，至少包含 `revision`、`contract_version`、`last_cursor=C`、研究状态、
当前 ROOT/terminal/outcome、Claim/路线/Worker/checkpoint/工具/预算摘要和 `available_actions`。
snapshot 的投影必须与 C 在同一一致读取中取得。

SSE 只是 `EventView` 的连续传输 adapter：客户端拿到 snapshot 后从 `after_cursor=C` 订阅，只按
run 内严格递增 activity cursor/event_id 应用。重复事件幂等忽略。运行事件可合法引用较早
research_revision，因为它不推进数学 revision；不得因此丢弃。查询 HTTP 响应才以
`(research_revision,last_cursor)` fence 判定是否比当前视图陈旧。正常 cursor 跳号或“暂无事件”不是
空洞；只有服务明确返回 `CURSOR_EXPIRED/CURSOR_UNAVAILABLE` 时才重新获取原子 snapshot，再从新 C
续传。命令回执与事件到达顺序不同不会改变此规则。

`FactView` 在内核当前权威投影上计算 BM25、邻域、依赖闭包、反向闭包、论文闭包和撤销预览；
响应明确 `authority_class`、`total_matches`、`returned_nodes`、`collapsed_groups`、revision 和继续
游标。布局缓存只保存坐标/折叠偏好，key 包含图 revision、中心、筛选和布局版本；不保存节点
状态或边语义。

## 9. 闭合、Finalize 与精确论文发布

### 9.1 研究闭合状态机

对于声称根命题成立或被证伪的研究，状态严格按以下顺序：

```text
ACTIVE CLAIMS
  -> CLOSURE_WITNESS_ACCEPTED
  -> ROOT_AUTHORITY_SATISFIED
  -> FINALIZED(CLOSED, final_outcome, unique terminal_root)
  -> CANDIDATE_TEX_GENERATED
  -> PAPER_REVIEW_ACCEPTED
  -> FINAL_PDF_AVAILABLE
```

ClosureWitness 必须绑定当前合同、selected subgraph digest、图 revision、所有进入边 justification、
BridgeSpec、组合义务和当前 verification/review refs。任一承重对象变化使 witness INVALIDATED。

`Finalize(PROVED|DISPROVED)` 只接受 `claim_kind=ROOT`、lifecycle ACTIVE、达到合同真值/语义/闭包
门的唯一 terminal。任意 LEMMA、OPEN run 的局部闭合、多 ROOT、缺 final_outcome 或开放但未列明
的义务均不能冒充最终结论。Finalize 成功后 run 状态为 CLOSED，其后不接受普通数学 apply。
`GenerateCandidateTex`、`SubmitPaperReview`、`CompileReviewedPaper` 是 `ResearchKernel.apply` 对 CLOSED
run 的精确白名单：仍由 ProductAuthority 以窄 capability 提交，使用同一 command/event/revision/receipt
事务，只能改变 publication projection 和不可变工件引用，不能修改合同、事实图、ROOT、ClosureWitness
或 final_outcome；其余命令稳定返回 `RUN_CLOSED`。每个接受的发布命令继续推进同一 run revision。
三者身份固定为：Generate/Compile 仅 `PUBLICATION_WORKER`；SubmitPaperReview 仅 `PAPER_REVIEWER`，且
签名主体必须等于 capability subject。Main 只能请求/排队生成与编译作业，RuntimeSupervisor 用对应
publication capability 执行；Gateway 不持有通用 publication 身份。
`UNRESOLVED` 等诚实终态遵循既有终态规则，但不能出现最终“已证明论文”。

### 9.2 论文工件链

Finalize 后，产品从 finalized snapshot 的唯一 terminal ROOT 与冻结有效依赖闭包确定性生成候选 TeX 工件。
数学家主页
不展示候选论文卡片；该工件只进入独立整篇复核任务。复核签名精确绑定：

```text
run_id, finalized_revision, candidate_generation_command_id, contract_version,
final_outcome, terminal_root_id, terminal_root_digest,
dependency_closure_digest, candidate_tex_artifact_id,
candidate_tex_sha256, paper_review_schema_version
```

TeX 字节、引用闭包、ROOT、合同、final outcome 或 schema 任一变化，旧复核即不可用于发布。审查提交时
当前 publication revision 可高于 finalized_revision，不能错误要求二者相等。
PAPER_REVIEW 的六项 check 全部签名通过且 reviewer 独立时，内核产生
`PAPER_REVIEW_ACCEPTED`。PDF 必须由该精确 `candidate_tex_sha256` 编译，记录 compiler profile、
退出码、log artifact 和 PDF digest。不得在复核后重新生成、润色或格式化 TeX再声称同一论文。

只有上述链完整时才能编译并出现“下载最终论文”，且 `FinalPaperView` 返回精确 TeX、
PDF、闭包事实目录和复核记录。界面不得自行生成“正确”“可发布”或“最终论文”状态。

## 10. 错误语义

拒绝不改变 revision，不产生业务副作用；同一输入在同一权威状态下返回稳定错误。至少支持：

| code | 含义 | 用户可行动作 |
|---|---|---|
| `CAPABILITY_DENIED` | 当前窄身份无此命令 | 切换合法身份或返回 |
| `REVISION_CONFLICT` | 页面/命令基于旧 revision | 重新查询，不自动重放 |
| `CONTRACT_VERSION_MISMATCH` | 对象不属于当前合同 | 查看修订影响或创建新 Claim |
| `IDEMPOTENCY_KEY_REUSED` | 同 request ID 被用于不同内容 | 修正调用方错误并用新 ID |
| `STABLE_LABEL_CONFLICT` | 同 label 指向不同 digest | 显式 supersede 或改 label |
| `AUTHORITY_INELIGIBLE` | 回执/审查无晋级资格 | 查看缺失字段或选择合法 verifier |
| `REVIEW_BINDING_MISMATCH` | 签名对象与当前目标不一致 | 对当前精确对象重新审查 |
| `COMPOSITION_OPEN` | 组合义务或边界 cut 未闭合 | 回到首个开放义务 |
| `DEPENDENCY_INVALIDATED` | 上游/合同变化导致陈旧 | 重新证明受影响闭包 |
| `CHECKPOINT_STALE` | 恢复对象绑定旧状态 | 从当前 checkpoint 重建工作 |
| `AUTHORITY_PROJECTION_LAG` | 失效投影尚未追平内核资格变更 | 等待消费者追平，不消费旧 binding |
| `REVOCATION_PREVIEW_STALE` | 确认时图已变化 | 重新预览影响闭包 |
| `CURSOR_UNAVAILABLE` | 无法从旧事件游标续传 | 获取新原子 snapshot |
| `TOOL_RESULT_NOT_AUTHORITY` | 工具完成但不具数学权威 | 送统一验证门或补证书 |
| `TERMINAL_ROOT_REQUIRED` | 最终结论不是唯一有效 ROOT | 完成根闭包或诚实结束 |
| `PAPER_REVIEW_REQUIRED` | 尚无当前精确 TeX 的合格复核 | 分配独立整篇复核 |
| `PAPER_BINDING_STALE` | TeX/闭包/ROOT 已改变 | 对新工件重新复核 |
| `RUN_CLOSED` | 已关闭 run 不接受普通写命令 | 新建关联 run |

错误响应返回机器 code、精确对象路径和稳定参数；用户文案由前端翻译，但不能隐藏数学拒绝为
“系统错误”，也不能把外部 5xx 显示成数学失败。

## 11. 必须通过的真实验收

以下验收从真实 CLI/Gateway 公共入口运行；纯算法内部不变量可有测试补充，但 mock、fixture、
schema 存在或截图不能代替产品状态机验收。

### 11.1 正链

在同一当前树、同一 run：两个 Worker 提交多个原子 Claim；一个被拒后以新 Claim 修订；另一
Worker 经搜索复用已验证事实；运行真实 Lean/确定性工具；取得真实 managed peer（或满足全
MACHINE 门）；提交 ClosureWitness；得到唯一 ROOT；Finalize 后 run 为 CLOSED 且有 final_outcome；
从 finalized snapshot 生成确定性候选 TeX；独立 PAPER_REVIEWER 对精确 TeX 六项签认；从同一 TeX digest
编译 PDF。卷宗、
图、事件和 UI 状态一致，未显示隐藏思维链。

### 11.2 权限与审查负链

逐项证明以下请求由内核拒绝且不改变图：Main 注册 Claim、Worker Promote、Worker 绕过 verifier、
Verifier 注册/改 Claim、Gateway 自报 role/capability、Main 代填审查。对有效签名分别令
`proof_checked=false`、`scope_checked=false`、任一 six-part=false、字段缺失、非盲、作者与
reviewer 同源、digest/revision/contract 错误、`UNMANAGED_REVIEW`、`authority_effect=NONE`；均
`promotion_eligible=false` 且 ClosureWitness 拒绝。

### 11.3 图与工具负链

候选、待验证、拒绝、撤销、失效 Claim 在谱系中可见，但不进入 VerifiedFactGraph、依赖闭包或
论文闭包；后续依赖它们被拒。Python/Z3/Lean 进程 exit 0 但目标、证书、环境或 binding 错误时，
只显示调用完成，Claim 不晋级；工具 adapter 不能写图。同 stable_label 不同 digest 冲突。

材料提取文本、Matlas 命中、无命中、远域高分、模型研究稿、批任务退出零和专家任务已创建分别尝试
直接进入有效事实图或生成新颖性/数学 verdict，全部拒绝。LIVE_QUERY 与 REPLAYED_SNAPSHOT、净室重新
发现与导入证书核验在投影和卷宗中不可合并。

### 11.4 闭合与论文负链

OPEN+LEMMA、无 witness、旧 witness、多 ROOT、缺 final_outcome、开放义务未列、未经整篇复核、
错误 reviewer、复核后 TeX 改一字、引用变化、撤销依赖、修订合同或更换 ROOT 时，均不得出现
最终论文或可发布状态。正例 PDF 的 compiler receipt 必须引用已审查 TeX 的同一 SHA-256。

### 11.5 并发、重连与恢复

- snapshot 与 SSE 建连间并发写入：从 snapshot cursor 续传，无漏项；
- 乱序 HTTP、重复 SSE、断线重连：无重复、无状态回退；
- 命令超时后同 request ID 连点及 Gateway 重启重放：副作用只发生一次；
- 同 request ID 不同 payload：稳定拒绝；
- checkpoint 后分别修订合同、撤销前驱、同 label 换 digest：旧 queue、tool feedback、
  composition_closed 和 review 不可消费，义务重开；
- 撤销预览后分别新增下游、替换 target digest、修订合同再确认：均返回 stale。RevokePreview 绑定
  preview_id/digest、revision、contract、target fact digest 和完整 affected IDs/digests；内核事务重算，
  不信任客户端 affected IDs。重新预览后完整下游失效且 sibling 保留；
- 一个 work item 两次故障恢复：显示多个 worker_run/attempt，事实和工具副作用至多一次。

### 11.6 数据最小化

让 provider 返回显式 reasoning、raw completion、scratchpad 和内部 prompt；事件账、snapshot、
搜索索引、工件目录、诊断包、报告和 UI 均不得包含这些字段。只有结构化公开动作、工具回执、
Claim、验证反馈和显式 `public_summary` 可见；`public_summary` 的变化不得改变任何权威状态。

## 12. 实施完成口径

本文件是目标规格，不证明当前后端或前端已经实现。只有第 11 节正负链均在当前产品入口取得
真实回执，且 CLI 与可视化入口均只通过同一 `ResearchProduct`（由其内部委派 ProductAuthority）后，
才能标记完成。不得以
Gateway 文件存在、按钮置灰、单元测试全绿、静态 DAG、旧 run 或模型被 prompt 要求守规矩替代。
旧 CLI/内核 `52/53` 台账只可标记 `REUSABLE_BASELINE`；材料 OCR、Matlas 接入、多源搜索图、研究稿到
Claim 闭环、远域消融、Zhao 净室复刻和批量 arXiv 均须有当前正式产品入口证据。科研假设得到负结果
不违反权威规格，但篡改题池、隐藏失败分母或用模型自评替代终局 verifier 属于验收失败。

## 13. 受控迁移决定

现有 RK 已验收主链采用“候选 TeX → 整篇复核 → Finalize”。本产品规格有意改为
“Finalize → 从 finalized snapshot 生成候选 TeX → 整篇复核 → 同 digest PDF”，因为独立审查必须看到
不可再改变数学结论的快照。该差异由 B15a 通过 ResearchKernel CLOSED-run 精确白名单迁移；在三种
publication command、窄身份、统一 revision/event/receipt 与真实正负 E2E 完成前，现有链只是可复用
基线，不能冒充本规格已实现。
