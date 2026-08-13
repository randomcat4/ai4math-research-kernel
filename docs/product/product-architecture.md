# RK 可视化数学研究产品工程架构

状态：`NORMATIVE_TARGET / NOT_IMPLEMENTED`  
版本：`RK-PRODUCT-ARCH-1.1`  
日期：2026-08-13  
对应产品范围：`product-prd.md`、`frontend-product.md`、`product-authority.md`

## 1. 目的与工程结论

本规格把可视化工作台落实为可实施的前后端架构。主要工程量在后端产品化，而不是页面绘制：
现有 RK 已有数学合同、Claim、验证、事实图、撤销、组合、论文、预算和编排能力，但尚无统一的
产品服务、动态 Worker 活动模型、后台运行守护、浏览器工件流、可扩展图查询、服务身份会话和实时
订阅。前端不得绕过这些缺口直接读取 SQLite、checkpoint 文件或 CAS。

系统只新增一个对外深模块 `ResearchProduct`。其内部私有 `ProductAuthority` 统一完成权威提交、权威
查询和工件访问判定；任何客户端和其他内部模块都不能直调它。`ResearchProduct` 还协调：

- `ResearchKernel`：数学状态和事实晋级的唯一裁决者；
- `ResearchOrchestrator`：路线、队列、Worker 工作项和检查点推进；
- `ContentAddressedStore`：不可变工件内容；
- `RuntimeSupervisor`：持久后台作业、进程和恢复；
- `ResearchGraphQuery`：可重建的全文与图查询；
- `DeploymentManager`：部署配置、能力注册和健康状态。

HTTP/SSE 只是 `ResearchProduct` 的 adapter。桌面壳、浏览器、CLI 和生成 SDK 都是调用者，不能成为
第二编排器、第二权限裁决者或第二事实数据库。

## 2. 不可违反的架构约束

1. **唯一数学真值。** 合同、Claim、Edge、六轴状态、撤销、ClosureWitness、ROOT、Finalize、预算
   和数学权限只由 `ResearchKernel.apply` 改变。任何投影、索引、活动记录或前端状态都无晋级权。
2. **一个产品入口。** 前端只调用 `ResearchProduct`；不得读取数据库表、checkpoint 文件、运行目录或
   CAS 路径。
3. **深模块，不做平行系统。** `ResearchProduct` 隐藏 revision 对账、身份映射、作业调度、事件追赶、
   工件提交和图查询复杂性。调用者不拼接内核命令序列来模拟产品动作。
4. **完整产品，不做 MVP。** 静态 dashboard、轮询脚本、mock Worker、单题演示、仅图组件或仅 SDK
   均不是完成。实施批次只是依赖顺序。
5. **禁止过度防御。** 只保留真实执行正确性所必需的角色隔离、revision、工件摘要、资源终止和恢复
   语义；不增加重复签名、重复哈希、通用零信任平台或与本机/单组织部署无关的安全层。
6. **不显示隐藏思维链。** 活动流只记录公开 assignment、动作、工具调用、回执、Claim、反馈和决策
   结果；不索取或持久化模型私有 chain-of-thought。
7. **不伪造实时性与进度。** 无可测分母的 Worker 只显示状态和已耗时；事件必须先持久化再推送。
8. **候选论文不进入研究主页。** 候选 TeX 只存在于独立整篇复核任务；最终论文门仍由 RK 裁决。

## 3. 模块图与真值归属

```text
React/TypeScript 静态应用 / 产品 CLI       管理员/审查者应用
          │ 生成 SDK                         │
          └──────── HTTP + SSE ──────────────┘
                          │
                 HTTP/SSE Adapter
                          │
                ┌───────────────────┐
                │  ResearchProduct  │  唯一公共产品模块
                └───────────────────┘
                  │       │       │       │       │
       ┌──────────┘       │       │       │       └────────────┐
       ▼                  ▼       ▼       ▼                    ▼
 ProductAuthority  RuntimeSupervisor  ResearchGraphQuery  WorkflowModules  DeploymentManager
        │
        ▼
 ResearchKernel
       │               │            │                 │
       │       ResearchOrchestrator  │           部署配置/能力健康
       │               │            │
       └──────────── SQLite ─────────┘
                       │
                      CAS
```

只有私有 `ProductAuthority` 可以提交数学/预算/终态命令给 `ResearchKernel`。RuntimeSupervisor、
ResearchGraphQuery、WorkflowModules、DeploymentManager 和 HTTP adapter 没有绕过它的调用路径。ProductAuthority 不生成
公共 SDK，也不成为产品测试入口。

真值分为三类，不能混称：

| 类别 | 所有者 | 是否影响数学裁决 | 重建语义 |
|---|---|---:|---|
| 数学与研究状态 | ResearchKernel/SQLite | 是 | 由迁移和事件/投影规则维护 |
| 执行状态与活动账 | RuntimeSupervisor/同一 SQLite | 否，但影响恢复与用户操作 | 由持久作业和活动事件维护 |
| 部署配置与能力健康 | DeploymentManager | 否 | 由部署配置和真实探测维护 |
| 搜索索引、邻接索引、聚类提示 | ResearchGraphQuery | 否 | 可从权威投影重建 |
| 前端布局、选中项、折叠项、已读通知 | 客户端偏好库 | 否 | 不要求从研究状态重建，可安全丢失 |

执行账和部署配置不是数学真值，但它们是各自领域的正式产品状态；不得用前端 JSON 或自然语言日志
替代。SQLite 仍由同一个产品守护进程单写，不引入第二数据库或通用消息总线。

`ResearchWorkflowModules` 是 `ResearchProduct` 内部的一组高内聚模块：材料提取、文献图、研究稿规范化、
BridgeOpportunity、批量题池和科研谱系。它们只通过现有 `command/query/artifact/ActivitySink` 与
ProductAuthority 协调，不能另开公共 service、数据库、幂等缓存、预算账或失效算法。生成数学候选时
必须交给 ProductAuthority/ResearchKernel；外部调用必须进入统一 ToolCatalog/ToolRun。

## 4. `ResearchProduct` 最小接口

接口使用少量 tagged request/response，HTTP、桌面调用和测试跨同一 seam。所有写请求含 request_id 与
scope：GLOBAL（创建）、RUN（含 expected research/contract revision）或 DEPLOYMENT（含 deployment
revision）。冲突返回对应 scope 的当前 fence 和可读差异，不自动换 revision 重放用户意图。

```python
class ResearchProduct:
    def command(self, session: Session, request: ProductCommand) -> ProductReceipt: ...
    def query(self, session: Session, spec: QuerySpec) -> QueryResult: ...
    def subscribe(self, session: Session, spec: SubscriptionSpec) -> EventStream: ...
    def artifact(self, session: Session, request: ArtifactOperation) -> ArtifactResult: ...
```

这四个入口按副作用与传输形态分，而不是按页面分：所有研究、部署、执行与权限状态改变进入
`command`；所有产品读取进入 `query`；持久事件续传进入 `subscribe`；`artifact` 只改变临时上传/字节
传输状态，COMMIT 产生尚未关联研究语义的 ArtifactRef。session 是 HTTP authentication adapter，不是
第五个产品业务入口。不为研究列表、图、审查、
管理员或后续页面增加新的顶层方法。

### 4.1 创建与研究列表

`command(CREATE_RESEARCH)` 接收自然语言题目、结构化合同草稿、附件 ArtifactRef、owner、labels 和初始
预算；它是唯一不带 run_id 的 CommandEnvelope，以 deployment + 已验证 session principal + request_id
幂等（subject 不在请求正文中），成功
decision 返回 created_run_id。创建结果仍由 `ResearchKernel.create` 产生 run。owner/labels 是产品元数据。

`query(LIST_RESEARCH)` 支持状态、owner、label、最近活动和全文条件，使用不透明游标分页。返回
`ResearchSummary`：
`run_id`、题目摘要、正交状态、当前阶段、阻塞、下一动作、预算、最近活动和 `last_cursor`。当前内核
没有公共 `list_runs`，因此该能力是待实现后端，不得标记为已有。

### 4.2 `command`

`ProductCommand` 是 tagged union，包括 `CREATE_RESEARCH`、合同确认/修订、`APPLY_ROUTE_PLAN`（批准、
启动、暂停、停止、改优先级/预算）、研究开始/暂停/恢复/取消、高层指导、逐 Claim 提交、验证产物导入、
撤销预览后的确认、BridgeSpec、ClosureWitness、审查任务处理、候选 TeX 生成、整篇复核提交、Finalize、
精确 PDF 编译，以及材料提取确认、文献查询/快照重放/适用性审查、题池冻结与批量创建、
BridgeOpportunity/消融分配、科研谱系导入、计算、工具、后台作业和部署控制。部署只改变部署配置/作业
状态，不能改变数学权威。

单个产品动作可以在模块内部协调多个步骤，但所有数学状态改变必须逐项调用 `ResearchKernel.apply`，
保留原 command receipt。`ProductReceipt` 返回：

```text
receipt_id, receipt_version, request_id, scope, run_id?, deployment_id?, command_id?, updated_at,
state=PENDING|DECIDED|OUTCOME_UNKNOWN, job_id?, supersedes_or_resolves_receipt_id?,
decision? { accepted, rejection_code?, missing_conditions[],
  revision_before, revision_after, contract_version, event_cursor_after,
  affected_entity_ids[], created_artifact_refs[], created_run_id?,
  kernel_receipts[], available_actions[] },
unknown_external_call_ref?, decided_at?
```

长操作只接受并持久化作业，不在 HTTP 请求内同步跑完。Claim 晋级、撤销和 Finalize 不得由作业状态
推导；必须等待内核回执。

### 4.3 `query` 与 `QuerySpec`

`QuerySpec` 是唯一读入口，冻结为以下 tagged variants；新增页面必须先扩这个 union，不能私建 endpoint：

- 研究/合同：`LIST_RESEARCH`、`RESEARCH_OVERVIEW`、`CONTRACT`、`CONTRACT_IMPACT`；
- 材料：`MATERIAL`、`MATERIAL_EXTRACTION`、`CITATION_ANCHOR`、`EXTRACTION_DIFF`；
- 文献：`LITERATURE_QUERY`、`SOURCE_SNAPSHOT`、`LITERATURE_SOURCE`、`LITERATURE_GRAPH`、
  `THEOREM_APPLICABILITY`、`PRIOR_ART_COMPARISON`、`NOVELTY_REVIEW`；
- 题池：`PROBLEM_POOL`、`PROBLEM_CANDIDATE`、`SOURCE_VERSION_HISTORY`、`BATCH_RESEARCH_JOB`；
- 路线/执行：`ROUTE_PLAN`、`BRIDGE_OPPORTUNITIES`、`ABLATION_PLAN`、`ABLATION_RESULTS`、`WORKFLOW`、
  `WORK_ITEM`、`WORKER_RUN`、`CHECKPOINT`；
- 数学：`CLAIM`、`CLAIM_HISTORY`、`AVAILABLE_ACTIONS`、`GRAPH_SLICE`、`GRAPH_SEARCH`、
  `DEPENDENCY_CLOSURE`、`REVERSE_CLOSURE`、`REVOKE_PREVIEW`；
- 计算/工具：`COMPUTE_TASK`、`TOOL_CATALOG`、`TOOL_RUN`；
- 人工/审查：`GUIDANCE_INBOX`、`HINT`、`REVIEW_INBOX`、`REVIEW_TASK`；
- 交付：`DOSSIER`、`PUBLICATION_STATUS`、`ARTIFACT_INDEX`；
- 科研谱系：`RESEARCH_CASE_LINEAGE`、`CLEAN_ROOM_INPUT_MANIFEST`、`CERTIFICATE_IMPORT_REPORT`；
- 跨研究/恢复：`ACTION_ITEMS`、`PRODUCT_RECEIPT`、`JOB`；
- 管理：`DEPLOYMENT_STATUS`、`DEPLOYMENT_JOB`、`BACKUP_STATUS`、`ADMIN_HEALTH`、`USAGE`。

QueryResult 使用 scope-aware envelope：单研究查询含 `run_id/research_revision/contract_version/last_cursor`；
LIST_RESEARCH、ACTION_ITEMS、创建阶段 PRODUCT_RECEIPT 和 DEPLOYMENT 查询含 deployment_id，并让每个
研究项携带自己的 run context。所有结果有 schema_version 与稳定实体 ID。`AVAILABLE_ACTIONS` 由 guard
语义派生；前端不得自行猜测晋级资格。

`GRAPH_SLICE` 请求：

```text
run_id, mode=VERIFIED|RESEARCH_HISTORY, seed_ids[],
direction=PREDECESSORS|SUCCESSORS|BOTH, depth,
filters, node_limit<=200, continuation_cursor?, at_revision?
```

返回：

```text
GraphSlice {
  run_id, at_revision, contract_version, mode,
  nodes[], edges[], groups[], cross_route_boundary[],
  total_matches, returned_nodes, returned_edges,
  continuation_cursor?, truncated
}
```

节点明确包含 `lifecycle`、`dependable`、Claim 类型、六轴、来源 Worker、合同版本和当前验证方式。
`VERIFIED` 只含当前可依赖事实；`RESEARCH_HISTORY` 可含候选、待验证、拒绝、撤销、上游失效和
superseded。边包括逻辑方向、BridgeSpec 和义务状态。两种图不可混为一张“事实图”。

`GraphGroup` 固定含 `group_id`、`group_kind=ROUTE|MILESTONE|TOPOLOGY_BAND|BOUNDARY`、
`parent_group_id?`、`membership_rule`、`total`、`status_counts`。overview 用路线/里程碑，局部展开用拓扑带；
有效事实图不按 lifecycle 分组，谱系视图才可按 lifecycle 筛选。`cross_route_boundary[]` 必须显示单路线
视图之外的承重前驱/后继、来源路线、dependable、折叠计数和到目标路径；闭包、撤销、论文从不受
单路线显示筛选限制。继续游标绑定 `run_id + at_revision + query digest + boundary`；revision 变化时
返回 `STALE_QUERY`，浏览器不猜计数。

### 4.4 `subscribe`

`subscribe` 只读取已持久化活动日志。HTTP adapter 使用单向 SSE，不再维护 WebSocket 第二协议。

连接顺序固定：

1. `query(RESEARCH_OVERVIEW)` 获得一致 snapshot 和 `last_cursor=C`；
2. 用 `after_cursor=C` 或 `Last-Event-ID: C` 订阅；
3. 服务端先分页排空已持久化 backlog，再等待新事件；
4. 客户端以 `cursor/event_id` 去重并按 cursor 应用；
5. heartbeat 不占 cursor，也不冒充活动。

SQLite 的 `event_seq` 是全库递增，某一 run 的 cursor 可以自然跳号；跳号不代表事件空洞。只要事件
不设保留期，就没有 cursor expiry。未来若增加保留期，过期游标返回 `410 CURSOR_EXPIRED` 并要求
重新取 snapshot，而不是静默补猜。

事件 envelope：

```text
ActivityEvent {
  schema_version, cursor, event_id, run_id, source,
  research_revision, contract_version?, type,
  work_item_id?, worker_run_id?, attempt_id?, route_id?, claim_id?, tool_run_id?, review_task_id?,
  payload, recorded_at
}
```

本机单进程用持久化后 condition 唤醒；跨进程/服务器部署可以用不超过 500ms 的 SQLite 轮询追赶。
不为此引入 Kafka、Redis 或通用消息总线。

### 4.5 `artifact`

`ArtifactOperation` 覆盖：`BEGIN_UPLOAD`、`APPEND_CHUNK`、`COMMIT_UPLOAD`、`READ_RANGE`、
`TAIL_LOG`、`DESCRIBE`。浏览器上传不能提交宿主绝对路径。

- 分段上传完成后经现有 ingest/CAS 形成不可变 ArtifactRef；
- 工件只有被内核命令或正式执行记录引用后才进入研究语义；
- 下载支持 Range、media type、长度、逻辑下载名和精确 ArtifactRef；
- 日志支持 byte cursor/tail，前端不一次加载 10MB 以上内容；
- 文本、JSON、Lean、图片、TeX、PDF 使用类型化查看器；编辑结果永远是新工件；
- 数学家主页不打开候选论文；审查者只能从精确绑定的 ReviewTask 打开候选 TeX。

### 4.6 部署操作

管理员写操作进入 `command(DEPLOYMENT_OPERATION)`，读取进入 `query(DEPLOYMENT_STATUS)`，覆盖 bootstrap、
配置读取/更新、真实 capability probe、硬件清单、组件注册、健康状态、备份、恢复、升级预检和诊断包。
它由 `DeploymentManager` 实现，不得改变数学权威。

bootstrap 的前提是安装包和产品守护已启动、数据根为空；不声称尚未安装的软件能从自身 UI 安装
自己。管理员会话无事实写权限，工具可用状态只由当前部署的真实探测着色。

## 5. 持久活动日志与动态 Worker

### 5.1 统一活动账

当前内核事件在 SQLite，而编排事件、queue 和 tool feedback 还有部分位于 checkpoint 文件。正式
产品必须把 checkpoint 和活动转换成由 `ResearchProduct` 托管的持久模型；网关不得解析文件或自然
语言日志生成 Worker 状态。

活动表至少记录：`cursor`、`event_id`、`run_id`、`source`、`research_revision`、实体外键、结构化
payload 和时间。运行活动写入不增加 mathematical `research_revision`，否则每次 Worker 心跳都会使
当前 checkpoint 因 kernel revision 改变而自我失效。活动事件可引用当时的 research revision。

由 `ActivityStore/ActivitySink` 唯一分配产品 cursor。`ProductActivityAppend` 是 kernel transaction 的
注册扩展：accepted/rejected kernel command 在同一 SQLite 事务写 product activity 并取得 cursor，
所以 ProductReceipt 可精确记录 event_cursor_after；外部 integration outbox 只投递，不分配 cursor。
Host/Worker/Tool 活动在独立事务调用 ActivitySink，不能直插表。snapshot 在同一 SQLite 一致读里同时
取得研究投影和最大 product cursor。只有内核命令推进 research revision。

D00a 冻结 `product_activity_events(cursor PK AUTOINCREMENT, event_id UNIQUE, scope_kind, run_id?,
deployment_id?, source, research_revision?, kernel_event_id UNIQUE?, entity_refs, payload_json, recorded_at)`，
并用 scope CHECK 保证 RUN/DEPLOYMENT 字段一致。它是统一用户活动流，不是第二数学真值账。

### 5.2 Worker 运行模型

“Worker”在 RK 中定义为一次公开的编排工作执行 span，不承诺任意 spawn、自由聊天或通用 shell。
它可以调用宿主注册函数并逐 Claim 提交，但不能绕过验证写图。

```text
WorkerRun {
  worker_run_id, work_item_id, run_id, worker_kind, role_id,
  route_id?, parent_worker_run_id?,
  assignment_summary, assignment_artifact_ids[], input_artifact_ids[],
  state, enqueued_at, started_at?, finished_at?, last_activity_at,
  budget_plan, usage, stop_reason?,
  attempt_ids[], claim_ids[], tool_run_ids[], output_artifact_ids[], checkpoint_id
}
```

`work_item_id` 在逻辑任务创建时生成并跨恢复稳定；`worker_run_id` 在一次角色执行/重分派时生成，同一
pending process 的恢复可沿用，真正重新调用角色必须产生新 worker_run；`attempt_id` 是一次宿主执行
尝试；`request_id/tool_run_id` 是一次组件调用。WorkItem 聚合状态优先显示当前非终态或最终成功，历史
失败不能覆盖后续成功，但必须保留可见。

状态：`QUEUED`、`RUNNING`、`WAITING_TOOL`、`WAITING_REVIEW`、`PAUSED`、
`CANCEL_REQUESTED`、`COMPLETED`、`FAILED`、`CANCELLED`。核心事件至少包含：
`WORKER_ENQUEUED/STARTED/WAITING_TOOL/TOOL_BOUND/RESUMED/CLAIM_SUBMITTED/COMPLETED/
FAILED/CANCEL_REQUESTED/CANCELLED`。

父子关系只能来自正式派生工作项，不能由同一 route 或日志相似性猜测。前端显示 assignment、动作、
工具、回执和结果，不显示隐藏思维链。大量历史 Worker 由查询分页和虚拟列表处理。

## 6. `RuntimeSupervisor` 与后台执行

当前同步 CLI/进程内编排不能支撑“关掉桌面继续研究”。正式部署由一个产品守护进程拥有 SQLite
写入、orchestrator、执行队列和运行目录；桌面壳、HTTP adapter 和 CLI 都只是客户端。

```text
DurableJob {
  job_id, run_id, kind, requested_by, request_id,
  state, created_at, started_at?, finished_at?,
  lease_holder?, lease_expires_at?, checkpoint_id?,
  worker_run_ids[], result_refs[], failure_code?
}
```

守护必须实现：持久 job queue、单写者 lease、子进程生命周期、暂停/恢复、cooperative cancel、服务
重启恢复和 checkpoint 失效。取消外部调用时先进入 `CANCEL_REQUESTED`；只有进程结束且回执落账后
才是 `CANCELLED`。

自动恢复只适用于幂等调用、只读工具或已有精确 idempotency key 的工具。远端调用在崩溃窗口中无法
判断结果时进入 `OUTCOME_UNKNOWN`，保留原 attempt，由用户决定查询远端、接纳回执或重试；不得
宣称 exactly-once。

checkpoint、queue item、pending tool feedback、composition closed、ReviewTask 和人工动作绑定创建
时 research revision、contract version 与所消费实体。合同修订、撤销或 ROOT/闭包变化后，旧项目
变为 `STALE/INVALIDATED`，不能静默迁移。

数学资格失效在 kernel 承重命令事务内写权威 invalidation ledger/watermark；异步消费者只物化执行/UI
状态。若消费者 watermark 落后，ProductAuthority 返回 `AUTHORITY_PROJECTION_LAG` 并关闭相关动作，
直到按 event_id 幂等追平，避免 kernel commit 与产品投影之间消费旧 binding 的窗口。

## 7. 身份、会话与窄 capability

服务化后不能沿用 `inspect` 的“本机同用户无 capability 读取”假设。`ResearchProduct` 把登录会话
映射为服务器持有的窄 capability；capability secret 永不进入浏览器。

| 会话 | 可做 | 不可做 |
|---|---|---|
| 数学家/Main | 建题、合同、路线、提示、暂停恢复、撤销、导出 | 直接提交事实、代填审查结论 |
| Worker | 读取必要子图、调用已注册函数、提交一个原子 Claim | 绕过验证晋级、任意写图 |
| Verifier/审查者 | 读取精确审查包、提交逐字段签名产物 | 修改 Claim、自己写最终事实 |
| 管理员 | 部署配置、诊断、预算与运行操作 | 以管理员身份写数学结论 |

`ReviewTask` 只持久化任务类型、assignee、author subjects、独立性约束、Claim/TeX/闭包精确 binding、
状态和 `signed_artifact_ref`。私有草稿不是权威输入；逐字段 checks/verdict 只从已验签工件投影。
任务行、UI 预填或 Main 修改都不能影响 promotion。

多客户端提交都带 `expected_revision`。冲突返回 HTTP 409、当前 revision 和受影响实体；网关不得
自动重跑撤销、修订、晋级或 Finalize。

## 8. `ResearchGraphQuery` 与索引

当前 `VerifiedFactGraph` 每次从完整 snapshot 重建图，BM25 每次重新分词全部事实，而且只覆盖当前
有效事实。它保留为内核级小图/闭包基线，但不能直接承担 10,000 facts / 30,000 edges 的工作台。

新增 `ResearchGraphQuery` 深模块，内部使用同一 SQLite 中可重建的：

- Claim/statement FTS5 索引；
- 按 run、contract、lifecycle、route、kind、verification 建立的过滤索引；
- predecessor/successor 邻接表及 topology band；
- BridgeSpec、obligation 和版本谱系的只读关联；
- 聚类摘要。 

投影更新由持久数学事件驱动，每个索引维护 processed kernel cursor/revision watermark。只能把
`at_revision <= watermark` 的完整投影标成该 revision；请求当前 revision 而索引落后时同步追赶或返回
`PROJECTION_LAG`（含 watermark），绝不能把部分图冒充当前图。索引丢失时可从 RK 权威投影重建，
不能反向修改 Claim。查询必须服务器端限界，不先把全图发给浏览器。

布局坐标属于展示状态：局部图默认在前端计算；可缓存 `at_revision + slice digest + layout version`
的提示，但它不是查询真值。图和等价列表使用同一个 `GraphSlice`。

## 9. 材料、文献与研究工作流模块

这些模块是 `ResearchProduct` 内部能力，不新增顶层接口或状态真值：

1. `MaterialPipeline`：以 ArtifactRef 为输入，调度 PDF/TeX/图片/文本解析与 OCR；保存原件、提取文本、
   公式/版面对象、页段锚点、解析器 build 和 diff。人工修订形成新 extraction artifact，不覆盖旧结果。
   当前 `EvidenceIngest` 只作为接收检查前置，不承担内容提取。
2. `LiteratureWorkspace`：把 Matlas、OpenAlex、Crossref、arXiv 注册为 ToolCatalog connector；每次调用都
   生成 SourceSnapshot 和 ToolRun。Matlas 只复用 Danus 薄客户端的协议适配，不引入其服务端假设；
   arXiv fetcher 按精确 ID/version 保存原文与局部上下文。LiteratureGraph 用来源带类型的边组合
   author/paper/theorem/citation，Matlas 没提供的边不得标记为 Matlas。
3. `ResearchDraftCompiler`：消费公开研究稿 ArtifactRef 和必要事实子图，产出候选原子 Claim、前驱、类型、
   未定义符号和 verifier plan；逐 Claim 经现有 ProductAuthority 提交。它不直接写事实图或关闭义务。
4. `BridgeOpportunitySelector`：生成候选目标域，计算领域距离、成熟度、目标缺席度、原生工具优势、证书
   压缩、映射损失、回译成本与死亡测试；产出 RouteProposal/BridgeOpportunity，只有通过现有 BridgeSpec
   handler 才进入数学工作流。
5. `ProblemBatchPipeline`：按冻结日期/学科获取 arXiv 版本，抽取开放陈述、去重、恢复语义、保存题池和
   全部分母，再以 GLOBAL/RUN 类型化命令创建研究；不能直接复制一条研究状态或跳过合同确认。
6. `ResearchCaseLineage`：引用历史项目工件、净室输入 manifest 与证书导入报告。Zhao 的净室重新发现和
   导入核验使用不同模式；`N2_AJT5` 历史迁移只形成候选材料/Claim。

外部 connector 的原始响应先进入 CAS，快照含 endpoint、request/response digest、请求时间、服务可见版本、
覆盖和错误。端点不可用时只能重放快照；无命中不能调用“确认新颖性”命令。Matlas 服务端、8.07M
statements/435K papers/1.9K textbooks、文档依赖图、递归索引和 Qwen3-Embedding-8B 都不随薄客户端进入
RK 部署；若未来获得正式数据/许可，仍作为同一 connector 的新 profile 接入。

消融执行器必须在分配前冻结题池、模型、tool profile/build、候选数、预算和 source-side final verifier，
然后为 `direct/near/far-random/far-retrieval/full-RK` 创建可比较作业。结果仓只保存每组全部任务、失败、
成本、证书长度和终局 verifier receipt，不包含“远域应当胜出”的硬编码判据。

## 10. 受管 Python 与科学计算

受管 Python 是当前后端缺失项，不能用 CAS/枚举 adapter 或测试 runner 冒充。它是一个正式注册的
工具 profile，而不是给每个 Worker 任意 shell。

profile 固定解释器/环境镜像或 lockfile、可用包和资源上限；首批完整环境至少支持 NumPy、SciPy、
SymPy、NetworkX 和绘图工件，可选 Sage 为独立 profile。每个 attempt 使用独立工作目录，声明的
输入只读，输出只从指定目录收集。记录：脚本 ArtifactRef、结构化参数、环境/包版本、stdout/stderr、
退出码、墙钟、CPU/内存、输出工件和 attempt 状态。

这不是额外安全平台，而是防止正常脚本污染其他研究、无法停止或无法复现的最低执行边界。部署必须
选择并验收一种实现：容器 profile，或 Windows 专用低权限账户加 Job Object。普通宿主用户下直接
执行模型生成脚本不属于完成。

执行前先落 attempt/start；完成后原子收集工件和回执。进程树可被预算、暂停或取消终止。普通 Python
结果固定为 `SOFT_TOOL_RESULT / NO_FACT_GRAPH_WRITE`；需要硬权威时另注册确定性 checker，不能从
Python exit 0 推导数学正确。实时日志通过 artifact tail 和活动事件呈现。

预算 reservation/actual/refund/UNKNOWN_COST 的唯一账仍是 ResearchKernel 的 RecordBudget 与事件投影；
调度模块只制定策略、采样用量和 placement，再经 ProductAuthority 提交 RecordBudget。不得建立可覆盖
kernel 合计的“权威预算账本”，UI 合计只读取 kernel receipt/event 投影。

## 11. 前后端契约与 SDK

### 11.1 HTTP 映射

HTTP adapter 不暴露内部类，固定映射：

```text
POST /v1/research                         -> command(CREATE_RESEARCH)
GET  /v1/research                         -> query(LIST_RESEARCH)
POST /v1/research/{run_id}/commands       -> command
POST /v1/research/{run_id}/queries        -> query
GET  /v1/research/{run_id}/events         -> subscribe (SSE)
POST /v1/artifacts/operations             -> artifact upload/control
GET  /v1/artifacts/{artifact_id}          -> artifact range/read
POST /v1/deployment/operations            -> command(DEPLOYMENT_OPERATION)
GET  /v1/deployment/status                -> query(DEPLOYMENT_STATUS)
GET  /v1/meta                             -> version/capability handshake
POST /v1/session/bootstrap                -> 本机一次性 bootstrap 换 HttpOnly session
POST /v1/session/login|switch|logout      -> 会话生命周期
GET  /v1/session/me                       -> 当前主体与窄角色
```

业务 rejection 保留 RK 原 code。字段/schema 错误 400，未登录 401，角色拒绝 403，不存在 404，
revision/idempotency/stale query 409，业务门 422，暂不可用 503，未处理错误 500。

### 11.2 契约来源与生成 SDK

主实例契约包唯一维护一个版本化 OpenAPI 3.1 文档和 JSON Schema；TypeScript SDK、Python 管理客户端和 SSE event
types 都由它生成。手写便利函数可以包装生成类型，但不得另定义同名 wire object。

每次连接先调用 `/v1/meta`：

```text
product_version, protocol_current, protocol_min_supported,
event_schema_versions[], deployment_id, enabled_features[]
```

兼容规则：同一 major 内只新增可选字段或新 tagged variant；删除、改义或必填字段变化升级 major。
客户端必须忽略未知可选字段，但对未知命令/事件 variant 明确显示“客户端需要升级”，不能静默当成
成功。SSE event 自带 schema version。

### 11.3 本机前端开发契约

前端仓库只依赖生成 SDK 和设计 tokens，不依赖 RK Python 源码。开发模式可连接：

1. 本机真实 `ResearchProduct` 守护；
2. 从当前 OpenAPI 生成的 fixture server，用于纯布局开发。

fixture/mock 只能用于开发和视觉回归，永远不能计为产品 E2E。涉及命令、恢复、工具或晋级的验收
必须连接真实守护和真实 SQLite/CAS。

前端功能包可先到 `IMPLEMENTED_AGAINST_CONTRACT`，只有连接真实后端完成对应旅程后才是
`PRODUCT_VERIFIED`；fixture 阶段不得写 DONE。

前端是静态构建的 React/TypeScript SPA；“静态”只指无需 SSR，不表示数据是静态 dashboard。在线
页面以 query snapshot + SSE 更新。离线/静态导出的卷宗是只读工件，不能执行命令或伪装实时状态。

### 11.4 服务器后端开发契约

后端可以在不启动桌面壳的情况下以同一 HTTP/SSE adapter 运行。前端团队只需约定：

- OpenAPI/JSON Schema 和示例必须随接口变更同步；
- snapshot 与 SSE 的 cursor fence 必须满足 §4.4；
- 所有写操作返回 ProductReceipt；
- 所有列表/图/日志都可分页或分段；
- enabled feature 来自部署能力，不来自前端配置；
- 后端错误提供稳定 code、用户可行动 message 和实体引用，不泄露绝对路径/密钥。

契约测试是质量门，但不取代打包 UI 的真实 E2E。

## 12. 前端实现与 Archon Horizon 复用

### 12.1 选定前端方向

采用第二张 Imagine 的“研究态势台”方向：React/TypeScript 工作台，中心为阶段、路线/Worker 与局部
DAG，右侧为 Claim/证据检查器，底部为公开活动；候选论文预览永久删除。默认只显示局部子图和
折叠簇，不铺开全部事实。

### 12.2 固定来源与许可证

复用审计来源：`frenzymath/Archon-Horizon`，commit
`a4565a48b4b84189384a05b9a4e6409e875122e1`，Apache License 2.0。进入 RK 前必须在
`thirdparty.md` 记录原仓库、commit、文件、许可证、Copyright/NOTICE、修改说明和保留能力；复制
文件保留必要头注。该治理到此为止，不扩张成无关供应链工程。

### 12.3 逐项决策

| Horizon 能力/文件 | 决策 | RK 使用方式 |
|---|---|---|
| `vizWorker.ts`、`vizInstance.ts` | **直接复用** | 复用 Graphviz WASM 的 Web Worker 初始化、布局隔离和错误返回；只调整包路径/类型，不承载 RK 状态 |
| `@viz-js/viz` Graphviz WASM | **直接复用依赖** | 固定版本，在 worker 中对服务端返回的 ≤200 节点 GraphSlice 布局；不把全图送入 WASM |
| `DagNetwork.tsx` 的缩放、平移、选中、路径高亮 | **改造复用** | 输入改为 RK `GraphSlice`；加入列表等价视图、键盘焦点、非颜色状态和继续展开 |
| `dagGraph.ts` 的 DOT 生成、章节折叠、cluster 展开 | **改造复用** | chapter 改为服务端 `GraphGroup`；有效图按路线/里程碑/拓扑带，谱系才可按 lifecycle 筛选，`+N` 使用后端真实计数 |
| 章节折叠与渐进呈现 | **改造复用** | 用于事实簇、路线和历史 Worker；不解析候选论文、不把章节当数学事实 |
| `App.tsx` 子智能体折叠 sublog | **改造复用** | 按 `worker_run_id` 聚合持久 ActivityEvent；父子关系来自后端字段，不从 transcript 猜 |
| `BoardPage.tsx` 看板/列布局 | **改造复用** | 用于 Worker/ReviewTask 的状态视图；卡片动作调用 ProductCommand，不能直接改状态列 |
| `BlueprintPage` 的渐进 KaTeX/交叉引用技巧 | **选择性改造** | 可用于最终卷宗和 Claim 证明阅读器；研究主页不加入候选论文预览 |
| Horizon dashboard server、workspace store、YAML task CRUD、5 秒轮询 | **不采纳** | 会形成第二运行状态和第二服务；全部由 ResearchProduct HTTP/SSE 取代 |
| Horizon Git timeline/VCS 作为状态来源 | **不采纳** | RK 以研究事件和 ArtifactRef 为来源；Git 不是数学或执行真值 |
| Horizon hgraph Python 解析器作为 RK 事实图 | **不采纳** | RK 图语义来自 Claim/Edge/Bridge/obligation；只复用前端布局思想 |
| Horizon transcript 原文和隐藏推理显示 | **不采纳** | 只展示公开结构化活动和工具回执 |

Graphviz WASM 解决的是 ≤200 节点局部图的布局，不解决服务器搜索、闭包、分页或聚类；这些仍由
`ResearchGraphQuery` 提供。若性能验收显示某类局部图 Graphviz 不足，可以在同一 `GraphSlice` 接口
下更换 G6/ELK adapter，不改变后端语义。

### 12.4 OpenCovibe 边界

`AnyiWang/OpenCovibe`（Apache-2.0，Tauri v2/Svelte 5）仅作为桌面壳候选。当前应用前端已选
React，因此不直接复制其 Svelte UI、run/events JSONL 或 session actor；否则会出现第二事件账。

允许在固定 commit 审计和 Windows 实机 spike 后复用与框架无关的 Tauri/Rust 窗口、更新、设置和
sidecar 生命周期代码。壳只负责启动/连接 `ResearchProduct` 守护、打开静态 React bundle 和系统级
文件选择；不接管研究状态。若抽取成本高于薄 Tauri 壳自建，则只借鉴而不复制，并记录工程理由。

## 13. 部署拓扑

### 13.1 本机桌面

```text
Tauri 壳 ──静态 React bundle
    │ HTTP/SSE localhost
    ▼
ResearchProduct daemon（桌面关闭后继续）
    ├─ 单写 SQLite
    ├─ CAS
    └─ attempt 工作目录/工具进程
```

安装器建立 daemon、静态前端和数据根。壳退出不杀正在运行的研究；系统托盘或重新打开后可查看。
升级先停止接收新 job、等待/暂停活动 attempt、备份并迁移，再恢复。

### 13.2 单组织服务器

```text
浏览器 ─ HTTPS ─ Reverse proxy
                    ├─ React 静态资源
                    └─ ResearchProduct HTTP/SSE
                              └─ 单机 SQLite/CAS/RuntimeSupervisor
```

本版本不做多租户或分布式数据库。服务器也保持一个写守护；横向扩容不是隐含目标。长工具可以在
同机子进程或已注册远端 adapter 执行，但回执统一回到该守护。

### 13.3 开发与发布

- 前端开发：Vite + 真实本机 daemon，fixture 仅供布局；
- 后端开发：独立 daemon + 生成 OpenAPI/SDK；
- 产品验收：安装包或服务器发布包，不从源码内部直接调用 kernel；
- 发布工件：Windows 桌面安装包、服务器包、静态前端 bundle、迁移、OpenAPI、SDK、许可证清单；
- 备份单位：SQLite、CAS、部署配置和必要 checkpoint；索引与前端缓存不必备份，可重建。

## 14. 性能预算

以下指标必须记录机器规格、数据分布、冷/暖缓存和并发数：

测量协议固定为 `benchmark-profile-v1`：固定随机种子；warmup 至少 20 次、普通查询测量至少 100 次、
重操作至少 30 次；分别测并发 1/10；SSE 基准 payload 1KiB；冷缓存指新进程并重开数据库，暖缓存指
索引 watermark 追平后。分别报告 server compute、TTFB、完整页传输和浏览器布局，不用一个总耗时掩盖。

| 场景 | 目标 |
|---|---:|
| 已有研究首次摘要 | 本机 p95 ≤ 2s；先摘要，重图/日志后加载 |
| 事件从持久化到在线 UI | p95 ≤ 1s |
| SSE 断线追赶 10,000 事件 | 不丢、不重排；重复可去重；p95 ≤ 5s |
| 研究列表 10,000 runs，50 条一页 | 暖缓存 p95 ≤ 500ms |
| Worker 历史 500、非终态 50 | 筛选/首屏 p95 ≤ 500ms；前端虚拟滚动 |
| 图搜索 10,000 facts / 30,000 edges | p95 ≤ 500ms，返回 total/truncated |
| 深度 2 GraphSlice，≤200 节点 | 服务端 p95 ≤ 500ms |
| 200 节点 Graphviz WASM 局部图 | 常见桌面设备 ≤1s 可交互 |
| 依赖/反向闭包 10,000/30,000 | p95 ≤1s，超大结果以摘要/分页呈现 |
| 10MB 日志首段 | p95 ≤300ms，不整文件加载 |
| 100MB 工件 Range 首字节 | 本机 p95 ≤500ms |
| 后台守护重启恢复 | 30s 内恢复可行动状态，模糊远端结果标 UNKNOWN |

规模图固定为 10,000 ACTIVE verified facts、30,000 DAG edges、依赖深度至少 60、非孤立节点比例
至少 80%，另含撤销/拒绝/版本历史投影；不能用“一条 60 深链 + 大量孤点”冒充相互依赖图。

## 15. 真实产品验收

所有验收从打包 UI 或发布版公共 HTTP/SSE 入口执行，保存命令、run_id、ProductReceipt、kernel
receipt、SSE cursor、WorkerRun、ToolRun、ArtifactRef、耗时、退出码和最终状态。pytest、单元测试、
fixture、mock server、直接 Python 调 kernel 和静态截图只属于质量门，不是产品 E2E。

### E01 数学家全流程

用同一 run 完成真实多 Claim 数学任务：创建与澄清合同、文献核验、从至少三条结构不同路线提议中
批准并启动且停止其中一条、多个 Worker、Claim
拒绝后修复、事实搜索复用、Lean + 一个确定性工具 + 受管 Python、人工高层提示、撤销与恢复、
ClosureWitness、唯一 ROOT、Finalize、从 finalized snapshot 生成候选 TeX、独立整篇复核和精确 PDF。候选论文
不出现在研究主页。

### E02 动态 Worker 与实时性

真实运行至少 500 历史/50 非终态 Worker 的规模投影，并在一个正常研究中观察排队、运行、等待工具、
故障、同 checkpoint 恢复、Claim 提交和终止。关闭/重开前端、断开 SSE 和重启 daemon 后，活动顺序
与持久账一致，父子树不依赖日志推断。

### E03 图查询与可读性

在固定 10,000/30,000 数据上从关键词找到 Claim，查看 VERIFIED 与 RESEARCH_HISTORY 两种图、深度
2 邻域、依赖闭包、反向闭包、撤销预览、折叠簇和继续展开；任何一步不加载全图。用等价列表和键盘
完成相同定位。

### E04 命令断线、并发与陈旧状态

在提交 Claim、撤销、启动工具和 Finalize 后断线；同 request ID 取得原回执且不重复副作用。同 ID
不同 payload 冲突；另一个客户端推进 revision 后，旧预览/命令返回 409，不自动重放。

### E05 后台运行与工具恢复

Worker 等待本地进程、远端工具和人工审查时分别关闭桌面、重启 daemon。可幂等调用恢复且不重复写；
不能确认结果的远端调用成为 `OUTCOME_UNKNOWN`。cooperative cancel 能终止 Python 进程树并保留日志。

### E06 角色与审查

从真实 UI 验证 Main 直接提交事实、Worker 绕 verifier、Verifier 写图、管理员写结论和 UI 代填审查
均被运行时代码拒绝。两名窄审查者对精确组合/论文工件逐字段签名；false、缺失、身份不独立、错误
digest、旧 revision/contract 均不能晋级。

### E07 管理员部署

从已安装但空数据根 bootstrap，配置并真实探测 Lean/Mathlib、SMT、CAS、枚举、Python、TeX 和已
配置模型；分别显示未安装包、外部 504、GPU 不可见和 TeX 缺失。观察预算/placement/失败，导出不含
凭据的诊断包，完成备份、迁移、恢复和索引重建。

### E08 桌面壳与服务器

Windows 安装包真实安装、升级、卸载保留数据选项和后台 daemon 生命周期通过；同一生成 SDK 驱动
本机桌面和单组织浏览器部署。服务器 SSE 经实际反向代理重连，静态 bundle 不含 capability secret。

### E09 诚实状态

分别运行证明、证伪、预算耗尽、数学未解决和外部服务阻塞。只有满足内核闭合门时显示最终结论和
论文；其余显示正交 outcome/execution/authority/blocker，不用旧回执或漂亮界面冒充完成。Rethlas
仍 504 时明确保持外部阻塞。

### E10 材料、Matlas 与多源文献图

从正式 UI 上传公式 PDF/TeX/图片/文本，完成 OCR/解析、原件对照、页段/公式锚点和一次人工修订。调用
当前可用的 Matlas、Crossref、OpenAlex、arXiv connector，保存原始快照；按 arXiv ID 拉精确版本原文和
定理上下文，完成假设适用性审查。断网重放后仍能复现使用过的返回，但明确显示非在线；作者—论文—
定理—引用边逐条标来源，无命中不生成新颖性。

### E11 研究级 Claim 闭环与科研谱系

真实模型研究稿经 ResearchDraftCompiler 拆为多个原子 Claim 和 verifier plan，至少两种异质 verifier 实际
执行，拒绝—修复—检索复用—义务更新全链进入一个 run。另在冻结树上分别运行 Zhao 净室重新发现和
导入证书核验，并迁入一批 `N2_AJT5` 历史候选；输入 manifest 证明净室未注入历史结论，未验证历史记录
不进入有效事实图。科研结果允许为负，provenance 不能含混。

### E12 远域套利消融

从同一冻结题池和配置创建五组作业，记录所有 BridgeOpportunity、死亡测试和 BridgeSpec，使用同一终局
verifier 评估并报告完整分母、成本、证书长度和不确定性。产品通过条件是实验可复现、组间配置一致、
失败不丢失和结论不预置，而不是 full-RK 必须胜出。

### E13 批量 arXiv 流水线

从真实 arXiv 日期/学科窗口抓取并保存版本，抽取、人工抽查、去重、恢复定义/量词/假设，冻结题池与
排除规则，批量创建研究。至少覆盖一个版本变化/撤稿边界和一个专家/作者确认待办；最终 manifest 含所有
纳入、排除、失败、阻塞和异源复核，不用成功样本代替分母。

## 16. 实施依赖顺序与完成定义

以下全部必做，不是可停止的 MVP：

1. 冻结 `ResearchProduct`、OpenAPI、身份/会话和部署拓扑；
2. 把 checkpoint、durable jobs、WorkerRun 和统一活动 cursor 纳入正式持久模型；
3. 通过四操作族实现创建/列表/命令/查询、工件上传下载和 snapshot/SSE fence；
4. 实现 ResearchGraphQuery、FTS/邻接索引和 GraphSlice；
5. 实现材料提取、Matlas/多源文献图、研究稿规范化、远域机会、批量题池和科研谱系模块；
6. 实现工具目录、受管 Python、日志 tail、取消和恢复；
7. 适配 Archon Horizon 前端能力，完成 React 数学工作台；
8. 完成独立审查者和管理员入口；
9. 完成桌面壳、服务器部署、升级/备份和 E01–E13；
10. 更新 PRD 台账、实现状态、README、用户/管理员手册和第三方归属。

只有 E01–E13 全部由真实产品入口通过，或某项有不可由本任务解决的明确外部阻塞并保留复现证据，
才可宣布可视化产品完成。当前本文描述的是目标架构，不表示上述缺失后端或前端已经实现。
