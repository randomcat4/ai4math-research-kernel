# RK 数学研究产品总 PRD

状态：`FROZEN_FOR_IMPLEMENTATION`  
版本：`RK-PRODUCT-1.1`  
日期：2026-08-13  
视觉基线：[路线工作台概念图](../assets/rk-route-workspace-v1.png)

## 1. 文档权威与适用范围

本文是 RK 从当前中文 CLI 产品升级为完整图形数学研究产品的唯一总入口。实现人员不应从聊天记录、
截图或旧台账猜需求。规范分工如下，任何文档都不能靠“排序靠前”覆盖另一份文档的精确定义：

1. 本文定义用户、产品范围、完整流程、完成条件和发布验收；
2. `product-authority.md` 定义数学真值、身份、命令、失效和最终发布状态机；
3. `product-architecture.md` 定义深模块、接口、数据与部署拓扑；
4. `frontend-product.md` 定义已选定的页面、交互和可访问性；
5. `work-packages.md` 是实施任务和唯一工作队列；
6. `../prd2.md`、旧 `../prdledger.md`、`../implementation-status.md` 和既有证据目录只用于识别可复用的
   CLI/内核能力与历史回执，不再定义、补充或覆盖产品范围；
7. 明确被本规范取代的旧产品草案已从当前 `docs/` 清理，不作为独立完成口径。

本文是范围总入口；数学权威、状态、身份、命令、失效和发布冲突时以 `product-authority.md` 为准；
接口与部署冲突时以 `product-architecture.md` 为准；交互冲突时以 `frontend-product.md` 为准；
`work-packages.md` 只能分解工作，不能改写规范。发现冲突必须回修源文档，不允许调用者选择有利版本。

本 PRD 覆盖数学家、独立审查者、文献审查者和管理员从创建研究到最终发布的全部产品流程。
明确不扩展为多租户 SaaS、社交网络、通用聊天产品、任意远程 Shell、移动端大图编辑器或分布式
数据库平台。

## 2. 产品定义

RK 是一个以逐 Claim 事实图为工作记忆、以多路线智能体和科学工具为研究执行层、以独立验证和
可追溯论文为交付物的数学研究工作台。

产品不是“让多个模型聊天”，也不是“把日志画成图”。它必须让真实用户回答：

- 题目究竟按哪个合同版本研究；
- 现在有哪些数学路线，哪些人在做什么，为什么等待；
- 哪些只是候选 Claim，哪些已是当前合同下可依赖的事实；
- 某个工具是调用成功，还是确实产生了可晋级的数学证据；
- 一个错误事实被撤销后，哪些下游失效，哪些 sibling 保留；
- 当前结论为什么仍然开放，或者为什么可以诚实宣告已证明、已证伪；
- 最终论文是否来自唯一 ROOT 的有效闭包，并由独立身份复核了同一份精确 TeX。

## 3. 强制产品与工程原则

### 3.1 禁止 MVP 偷缩

可以按依赖顺序分批开发，但任一以下产物都不得称为产品完成：静态页面、mock 事件、单题脚本、
测试桩、类或 schema 存在、只读 dashboard、组件截图、远端 demo、一次模型回答或一组通过的单元测试。
只有所有非排除能力进入同一个研究编号的真实用户流程并通过对应验收，才可发布。

### 3.2 禁止过度防御

只实现数学正确性、写命令对账、窄身份、数据恢复和受管执行所必需的保护。不建设通用 IAM、
多租户权限平台、WAF、敌对容器编排、重复签名层或重复哈希账本。一个对象有一个内容摘要，一个
用户意图有一个命令标识，一个内核执行账本；不得叠加第二套“更安全”的真值。

### 3.3 一个深产品模块

所有调用者只通过 `ResearchProduct` 使用产品能力。它在内部协调 ResearchKernel、Orchestrator、
CAS、活动日志、后台运行守护和 DeploymentManager。数学事实晋级、撤销、合同与终态仍只由
ResearchKernel 裁决。HTTP、桌面壳、前端缓存、搜索索引、图布局和通知都是适配或可重建投影。

### 3.4 产品主流程优先

每项底层能力必须由实际编排器和数学家工作流消费，并在状态、活动、证据或报告中可观察。
没有真实入口和实际跨模块运行的能力只能标记为底层存在，不能标记完成。

### 3.5 成熟代码优先

许可证、归属、接口和数据模型审计通过后，优先清理、适配和复用 Danus、Archon Horizon、Archon、
jixia、LeanSearch 等成熟实现。若重写，能力不得缩水，并应说明为何适配会形成长期双真值源或更高
技术债。

### 3.6 不显示隐藏思维链

产品只持久化和展示公开 assignment、结构化工具调用与结果、候选工件、Claim、验证反馈、公开
摘要和状态变化。provider reasoning、隐藏 scratchpad、内部提示词和 raw completion 不进入事件流、
搜索、诊断包或页面。公开摘要不参与权威裁决。

### 3.7 测试不套娃

纯图算法、TransitionGuard 和规范化可做直接测试；产品行为通过 `ResearchProduct` 契约测试；跨模块
承诺通过真实 UI/公共入口 E2E。不得把同一行为在 adapter、controller、service、HTTP、CLI 五层各写
一套同义 mock 测试。新深模块稳定后，删除被其接口测试取代的浅层测试。

### 3.8 能力与证据必须分级

规范、台账、能力目录和页面统一使用以下四种证据类别，禁止把计划或科研假设写成已实现：

- `REUSABLE_BASELINE`：当前 CLI/内核或远端曾有可核验真实回执，可作为新产品实现的底座，但尚不等于
  图形产品完成；
- `ENGINEERING_REQUIRED`：接口、数据和验收可确定实现，当前仍须开发并接入正式产品；
- `EXTERNAL_DEPENDENCY`：依赖当前部署外的服务、语料、专家、作者或凭据，必须显示具体阻塞与已保存证据；
- `RESEARCH_HYPOTHESIS`：功能可实施，但效果、排序有效性或数学成功率只能经冻结实验检验，不能在 PRD
  中预设为真。

同一能力可以同时有可复用底层和未完成的产品接缝；完成状态取最低的未闭合承诺，不按历史回执向上取整。

## 4. 用户和完整结果

### 4.1 数学家

从自然语言问题和附件开始，确认合同，核验文献，选择多条路线，观察逐 Claim 工作，调用模型和
科学工具，给出高层提示，处理拒绝与撤销，恢复中断研究，最后得到诚实状态、卷宗和满足发布门的
论文。

### 4.2 独立审查者

使用自己的窄身份领取精确绑定的 Claim、ClosureWitness 或整篇论文审查任务，查看必要依赖闭包、
证据和引用，逐字段填写结论并签名。审查者不能是作者，不能借主会话代填，也不能直接写事实图。

### 4.3 文献与新颖性审查者

维护检索问题、来源版本、原文定位、Claim/路线关联和相似工作对照。检索无命中只能表示覆盖范围内
未命中，不自动证明新颖性。

### 4.4 管理员

从已启动安装包的空数据根完成初始化，配置模型、工具和窄身份，观察预算、硬件、队列和故障，
执行升级、备份、恢复和诊断。管理员不得因此获得数学 Verifier 身份。

## 5. 正交产品状态

所有页面使用同一术语表，不把多种状态压成一个标签：

- `outcome`：`OPEN / PROVED / DISPROVED / UNRESOLVED`；
- `execution`：`QUEUED / RUNNING / PAUSED / WAITING / FAILED / CLOSED`；
- `authority`：各验证轴、promotion eligibility、ClosureWitness readiness；
- `publication`：`NOT_ELIGIBLE / CANDIDATE_TEX / REVIEW_REQUIRED / REVIEWED / COMPILE_FAILED / FINAL`；
- `blockers[]`：原因、责任角色、受影响对象、下一动作；
- `revision` 与 `contract_version`：所有队列、审查、回执和页面动作的精确上下文。

一个研究可以同时是 `OPEN + PAUSED + WAITING_PEER_REVIEW`，并带一个外部服务阻塞。前端不能选择
一个“最像”的单一状态覆盖其余状态。

## 6. 已冻结的信息架构

主导航固定为：

1. 研究（含“合同与材料”二级页）；
2. 文献与新颖性；
3. 路线与任务；
4. 事实与谱系；
5. 计算与工具；
6. 人工与审查；
7. 状态卷宗；
8. 管理。

默认研究入口采用已选定的“路线图 2”：进入当前路线的局部有效事实图，而不是全局聊天或候选论文。

### 6.1 路线工作台

- 顶部显示题目、路线、合同版本、修订、正交状态和 blockers；
- 图页固定提供“有效事实图”和“Claim 谱系”两个视图，绝不混成一张可依赖图；
- 默认按路线/里程碑折叠为超节点，一次展开一个语义组；
- 使用 Graphviz 确定性自上而下布局；
- 固定显示“局部 N / 总匹配 M、深度、筛选、合同、修订”；
- 中央图只承载数学依赖；Worker、工具和审查作为节点侧栏与底部活动抽屉；
- 右侧检查器显示 statement、证明/证据、前驱、后继、来源和逐轴验证；
- 底部按稳定 `work_item_id` 聚合 Worker，每次执行以 `worker_run_id + attempt` 展示；
- 只展示公开动作，不展示隐藏思维链；
- 候选论文预览不得出现在研究主页或路线页。

### 6.2 其他必选工作台

- 研究列表与合同：创建、附件、歧义、确认、局部修订、影响预览；
- 文献与新颖性：检索、导入、去重、原文定位、Claim/路线关联、相似结果对照；
- 计算：受管 Python/SMT/CAS/枚举/Lean 任务、输入输出工件、日志、比较与候选 Claim；
- 审查：身份、任务领取、证据闭包、逐字段结论、行号/Claim 锚定反馈；
- 卷宗：任何状态均可查看的诚实研究记录；只有满足发布状态机后显示最终论文；
- 管理：能力、模型、硬件、预算、后台作业、故障、安装、升级、备份和恢复。

完整页面行为以 `frontend-product.md` 为准。

## 7. 完整产品能力

### 7.1 研究创建、合同与材料

用户可提交题目、PDF、TeX、图片和文本材料。产品必须分别保存不可变原件、提取文本、版面/公式对象、
解析器与版本、页码/段落/图号定位及差异记录；OCR 或 TeX/PDF 解析不得静默改变上下标、量词、集合符号
或公式边界。用户能并排查看原件与提取结果，修订错误锚点，并让合同或 Claim 引用精确页段。

当前 `EvidenceIngest` 仅有文件策略、摘要、schema 与 UTF-8 等接收检查，不具备 PDF/图片 OCR、数学符号
保真或页段定位；这是 `REUSABLE_BASELINE + ENGINEERING_REQUIRED`，不得将“文件已入 CAS”冒充材料可用。
合同仍须明确对象、定义域、量词、边界、精确否定、允许工具和成功条件；局部修订通过唯一失效引擎
使受影响队列、审查和闭合标记失效，未受影响事实保留。

### 7.2 文献、Matlas 与新颖性

产品保存检索问题、来源服务、查询端点、查询时间、请求、响应摘要、原始响应工件、端点/模型可见版本、
原文工件、引用锚点和覆盖边界。每次外部返回必须可在端点变化或下线后从冻结响应重放；重放不等于
重新确认外部索引仍完整。无命中只能表示该快照和覆盖范围内未命中，不生成新颖性结论。

当前事实边界：RK 只注册了 Crossref 书目适配器；Danus 在
`C:/game/ai4math/frenzymath/Danus/danus/integrations/matlas.py` 开源的是一个调用
`https://leansearch.net/thm/search` 的无鉴权 Matlas 薄客户端，返回 `title/theorem/arxiv_id/theorem_id`，
服务器同一客户端已实测返回 2 条定理。Matlas 服务端、语料、文档依赖图和向量索引不在当前部署，也未
找到可直接部署的官方完整后端、数据和 index。公开论文描述约 8.07M statements、435K peer-reviewed
papers、1.9K textbooks、文档内依赖图、递归展开与 Qwen3-Embedding-8B，并托管于 matlas.ai；这些是
外部系统描述，不是 RK 已有资产。

完整产品须把 Matlas 作为 `EXTERNAL_DEPENDENCY` 注册进统一 ToolCatalog，保存查询快照与失败回执；根据
arXiv ID 获取对应版本原文和局部上下文，核查定理的量词、假设、符号与目标 Claim 的适用性。另由 RK
组合 OpenAlex、Crossref、arXiv 与 Matlas 建立作者—论文—定理—引用搜索图；作者和引用边不是 Matlas
原生能力，页面和卷宗必须标明各边来源。来源可关联合同、Claim、路线、方法卡和 BridgeSpec；最终
新颖性边界由独立人员确认。

### 7.3 批量 arXiv 开放问题流水线

管理员或文献审查者可按冻结日期、学科和版本规则抓取 arXiv，抽取明确标注或语义候选的
Conjecture/Problem/Question，恢复定义、量词和假设，追踪修订、撤稿和版本，去重后形成预先冻结题池。
题池同时冻结排除规则、纳入分母和选择前的来源快照；系统按重要性、可验证性、桥潜力和预计成本生成
选择建议，再通过正式命令批量创建独立研究编号。全部失败、阻塞和被排除原因都保留，不能只展示成功样本。

抓取、解析、版本追踪、去重、队列和失败分母是 `ENGINEERING_REQUIRED`；开放陈述的语义冻结必须有人类
抽查；专家/作者确认属于 `EXTERNAL_DEPENDENCY`；流水线产出真正新数学的成功率属于
`RESEARCH_HYPOTHESIS`。机器证书仍须经异源复核，再明确区分“专家确认”“作者确认”“尚未确认”。

### 7.4 远域方法套利与 BridgeSpec

远域方法套利是一级研究策略，不只是 BridgeSpec 建成后的合规字段。每个 `BridgeOpportunity` 必须记录：
源问题规范化、候选目标域、领域距离、源域方法成熟度、目标域缺席度、目标域原生工具/verifier 优势、
预期证书压缩、映射与假设损失、回译及源侧复核成本、最快死亡测试和选择理由。候选通过死亡测试、映射
定义与假设核查后，才登记现有 BridgeSpec；BridgeSpec 继续负责方向、回译、目标域审查和组合义务。

字段、检索、调度、实验和 BridgeSpec 接缝是 `ENGINEERING_REQUIRED`。远处一定更好、上述评分可预测有用
桥，以及远域优于直接/近域/随机桥，均是 `RESEARCH_HYPOTHESIS`。产品提供正式消融：
`direct / near / far-random / far-retrieval / full-RK`，冻结模型、工具、候选数、题池和预算，并让所有组
使用同一个离线或源侧终局 verifier；不得用模型自评或只挑成功案例下结论。

### 7.5 多路线与动态工作单元

路线先作为 `RouteProposal` 给出方法、目标、预期验证、预算、里程碑、终止理由和依赖，由 Main 通过
类型化 `ApplyRoutePlan` 批准、启动、暂停、停止、调优先级或调整预算；远域机会、直接路线和近域路线
进入同一计划，不建立旁路编排器。“停止路线”必须真实停止派生新工作项。真实 Worker 具有稳定逻辑
任务、执行尝试、父子关系、公开 assignment、输入工件、工具调用、Claim 输出和最后活动；系统不从
自然语言日志猜状态，也不显示没有分母的完成百分比。

### 7.6 研究稿到逐 Claim 的统一闭环

Worker 一次提交一个原子 Claim，包含 statement、proof/evidence、predecessor fact IDs、来源、类型、
合同版本和工作项。完整产品还必须把模型研究稿规范化为候选原子 Claim、前驱、Claim 类型和未决定义，
按类型自动选择 Lean、SMT、CAS/枚举 checker、受管同行、人类语义或软 verifier；验证结果返回谱系或
有效事实图，并据新事实自动更新组合义务与 ClosureWitness readiness。这个闭环必须由编排器真实消费，
不能靠用户手填内部数据库字段。

当前合同、Claim/Edge、验证写门、BM25、撤销恢复、BridgeSpec、ClosureWitness、预算、九角色、工具接缝
和 TeX/PDF 是 `REUSABLE_BASELINE`；“研究稿 → 原子化 → verifier routing → 回图 → 自动组合义务”尚未在
研究级开放题贯通，是 `ENGINEERING_REQUIRED`。只有统一验证门接受后 Claim 才进入当前合同的
VerifiedFactGraph；拒绝、待修、撤销和 superseded 只留在谱系。软模型永远不能独立晋级机器轴。

### 7.7 检索、闭包和撤销

数学家、Main 和 Worker 可按目标检索当前合同下必要事实，支持关键词、前驱、后继、依赖闭包、反向
影响闭包和论文子图。Worker 只注入必要子图。撤销确认绑定预览修订；图变化后旧预览失效。撤销使全部
有效下游、证据、义务和旧审查失效，未受影响 sibling 保留；新提交不能依赖失效事实。

### 7.8 角色、身份与审查任务

Main 不能提交事实，Worker 不能绕过验证写图，Verifier 只读研究状态并只能提交自己签名的结构化审查
产物。浏览器不持 capability secret，请求 payload 不能覆盖角色。审查任务记录 assignee、作者、独立性、
精确摘要、合同、修订、过期与重分派状态。文献适用性、开放题语义冻结和科研终局复核同样分配明确身份，
但它们不能借任务状态直接赋予事实权威。

### 7.9 模型、工具和科学计算

统一能力目录承载研究模型、LeanSearch、jixia、Lean、SMT、CAS、精确枚举、受管 Python、本地证明模型、
软 verifier 和文献服务，并显示 provider、model、build/version、部署 profile、函数 schema、usage、费用、
真实 ToolRun、失败恢复与 authority ceiling。

截至本版规范的可复用证据边界为：DeepSeek V4-Pro 文本研究、DeepSeek Responses 标准函数两轮、
LeanSearch、jixia、Lean、Z3、SymPy、精确枚举和 DeepSeek-Prover 有真实回执；GPT-5.6 provider 未配置；
Codex 只有纯文本通过，`shell_command` 两次未产生真实工具副作用，custom `apply_patch` 在执行层失败，
因此不得显示“Codex 工具执行完成”；QED-Nano 只有简单题 smoke；Rethlas 当前上游 504；Archon 完整
研究级调用未验收。以上均只是 `REUSABLE_BASELINE/EXTERNAL_DEPENDENCY` 边界，不自动完成新产品。

科学计算工作区支持脚本与输入工件、固定环境和包清单、运行/取消、实时 stdout/stderr、表格/图片/文件
结果、固定输入重跑、两次结果比较，以及从产物创建候选 Claim 或 checker 请求。不提供任意宿主 Shell；
普通 Python 永远是探索证据，确定性 checker 是独立注册能力。

### 7.10 预算、调度与后台恢复

预算账本记录 reservation、actual、refund 和 UNKNOWN_COST。硬件计划基于真实清单和模型资产，记录
placement、并发组、稳定串行晋级顺序和回退原因。后台守护持久化作业、租约和取消请求；桌面退出后
研究按部署策略继续。模糊外部副作用标记 `OUTCOME_UNKNOWN`，不虚构 exactly-once。

### 7.11 人工高层指导

数学家可在安全检查点提交换表示、停止路线、优先引理或修改策略。提示具有目标、修订、合同、checkpoint
和 `QUEUED/APPLIED/REJECTED/SUPERSEDED/CANCELLED` 生命周期；生效前可撤回，生效后显示影响摘要。
提示进入编排上下文但不写事实图。

### 7.12 组合、终态、卷宗和论文

组合必须显式关闭覆盖、相容、不变量、终止、边界和共同选择等义务。只有 promotion-eligible 且具有
authority effect 的受管同行审查或规范允许的全机器闭合可支持 ClosureWitness。

研究闭合链为：`有效 ClosureWitness → 唯一 ROOT terminal → Finalize(CLOSED, final_outcome)`。

论文发布链为：`finalized snapshot 的唯一 ROOT 与有效依赖闭包 → 确定性候选 TeX → 独立整篇复核精确
绑定 → 同一 TeX 编译成功 → FINAL`。

研究卷宗在任何状态均可生成；未闭合时必须显示开放义务和限制。编译失败产生排版修复任务；内容摘要
改变后旧整篇复核失效。

### 7.13 科研谱系、净室复刻与历史迁移

Zhao 项目是 RK 研究协议祖型的正式科研产物，但当前版本软件尚未完成净室重跑。产品必须把历史产物以
来源、版本和 digest 归入项目谱系，并在新 run 中分开记录：`CLEAN_ROOM_REDISCOVERY`（不给 Worker 注入历史
结论）与 `IMPORTED_CERTIFICATE_VERIFICATION`（只导入证书/工件后核验）。两种证据不得合并成“重新发现”。

`N2_AJT5`（AJT(5)）是与 Zhao 独立的项目；其手工研究历史尚未迁入当前事实图。迁移只能生成带明确来源
的候选 Claim/材料，再逐项经过当前合同和统一验证门，不得批量标成有效事实。净室是否重新得到相同数学
结果属于真实科研验收结果，不在规范中预设。

### 7.14 管理与部署

管理员可从空数据根初始化、配置数据位置和能力、启动/停止后台守护、观察成本与故障、导出无凭据
诊断包、迁移 schema、备份 SQLite/CAS/不可重建配置并恢复到新目录。搜索索引、活动投影和布局缓存可
删除重建。支持当前版到下一版的真实升级演练。

## 8. 交互与传输正确性

- 每个用户写意图只有调用方生成的稳定 `request_id`；首次进入内核后产生只读 `command_id`；
- 公共产品只返回可查询的 `ProductReceipt`。状态仅为 `PENDING / DECIDED / OUTCOME_UNKNOWN`：
  `PENDING` 表示持久产品作业已收但尚无内核决定；`DECIDED` 必含不可变 `kernel_receipts[]`，是否接受
  只看其中的内核决定；`OUTCOME_UNKNOWN` 只用于远端外部副作用无法确认；
- 普通命令同 `(run_id, request_id)`、创建命令同 `(deployment, principal, request_id)` 且 digest 相同时，
  重试返回同 receipt_id 的当前 ProductReceipt 投影，不重复副作用；同 ID 异 digest 稳定冲突；
- 不另造 `RECEIVED/ACCEPTED/COMMITTED` 产品真值；HTTP 202 不是状态真值；
- 所有状态改变命令绑定 principal、run、expected revision 和 contract version；
- HTTP accepted 不是状态真值，页面只由原子 snapshot 与持久事件更新；
- snapshot 返回一致的 last cursor，事件从 `after=cursor` 严格递增续传；
- ActivityEvent 只按 run 内 activity cursor/event_id 排序和去重；运行事件可合法引用较早
  `research_revision`，不得因此丢弃；查询响应才以 `(research_revision,last_cursor)` fence 判陈旧；
- 正常 cursor 跳号或暂无事件不是空洞；只有明确 `CURSOR_EXPIRED/CURSOR_UNAVAILABLE` 才重取 snapshot；
- `available_actions`、阻塞原因和 next action 由内核 guard/产品投影提供，前端不自造状态机；
- 工件支持浏览器上传、不可变引用、Range 下载和日志 tail；
- 当前身份、角色和研究始终可见。

## 9. 性能与可用性指标

在发布基准机器上验收，并记录硬件、冷/暖缓存和并发数：

- 10,000 个当前有效事实、30,000 条非平凡依赖边、深度至少 60；
- 默认图只返回局部 GraphSlice，首次可交互目标小于 2 秒；
- 暖缓存关键词搜索和 200 节点邻域查询 p95 小于 500 毫秒；
- 折叠 overview 小于 150 个超节点；单次展开默认不超过 200 个实际节点；
- 500 个历史工作项、50 个活跃/等待项时，三次交互内定位指定失败、等待人工和 Claim 来源；
- 新持久事件到前端可见的正常目标小于 1 秒；
- 200% 缩放不丢操作，图有等价列表，键盘可完成核心流程；
- 桌面宽屏为主，窄屏保证查看、审查和人工动作，不承诺手机编辑大图。

## 10. 发布级真实验收

以下验收必须从打包 UI 或公共产品入口运行，保存研究编号、命令回执、事件 cursor、工具回执、工件
ID、退出/终态、耗时和截图。pytest、直接调用 Python 内核、fixture 或旧版本证据不能代替。

### A01 真实数学研究全程

一个可重复、非玩具的多 Claim 数学问题：附件与合同 → 文献核验 → 至少三条结构不同路线 → 不同
Worker 逐 Claim → 一次拒绝后修复 → 后续检索复用 → 工具与形式验证 → 人工换表示/优先引理实际改变
路线 → 组合闭合 → 唯一 ROOT → Finalize(CLOSED/final_outcome) → 从 finalized snapshot 生成候选 TeX →
独立整篇复核 → 同一 TeX 编译 PDF。用户从正式路线计划中批准并启动至少三条结构不同
路线，停止其中一条后该路线不得再派生工作项。

### A02 撤销、合同修订与恢复

撤销中间事实后全部有效下游失效、sibling 保留、义务重开、旧审查过期；重新证明后闭包恢复。合同
局部修订只失效受影响工作。进程中断后不重复 Claim/工具副作用、不丢 verifier feedback，并继续同一
工作项的新 attempt。

### A03 身份和权威负验收

Main 提交事实、Worker 绕过 verifier、Verifier 写图、作者领取独立审查、主 UI 代填权威字段、有效
签名但任一逐项结论 false、UNMANAGED_REVIEW、错误 digest/revision/contract、OPEN+LEMMA 生成最终
论文全部由运行时代码拒绝。

### A04 文献与新颖性

同一 run 实际调用 Matlas、Crossref、OpenAlex 和 arXiv 中当前部署可用的服务，保存 endpoint、时间、请求、
原始响应与响应摘要；通过 arXiv ID 拉取匹配版本原文和定理局部上下文，逐项核查假设与适用性。断开
Matlas 后从已保存响应重放，页面仍显示“历史快照”而非在线结果。作者—论文—定理—引用图的每条边标明
来源，无命中只显示有限覆盖边界，人工确认后才形成新颖性结论；不可用服务保留具体外部阻塞。

### A05 科学计算和工具

同一研究从 UI 实际触发 Lean、Z3、CAS、精确枚举和受管 Python；显示真实进程回执、工件、attempt、
资源、provider/model/build、usage、费用和 authority ceiling；探索结果经新 Claim 和统一验证门后才能入图。
能力目录必须把 GPT-5.6 未配置、Codex 无副作用/执行失败、QED-Nano 仅 smoke、Rethlas 504 和 Archon 未有
研究级验收显示为相应边界，不得借其他模型的回执染绿。

### A06 动态 Worker、并发与断连

至少 50 个活跃/等待工作项、父子 Worker、失败后恢复 attempt 和两条真实时间区间重叠路线；稳定串行
晋级。写命令分别在请求尚未持久化、ProductReceipt 为 PENDING 且尚无内核决定、DECIDED 已持久化但
响应尚未到达时断连，重连对账后均只有一次副作用。

### A07 大图与可访问性

按第 9 节数据分布构建 10k/30k 图，真实完成搜索、局部闭包、路线折叠、单组展开、谱系切换、撤销
预览和论文子图选择。键盘、屏幕阅读器、200% 缩放、减少动画和列表替代视图通过实际操作验收。

### A08 管理与灾难恢复

从安装包和空数据根初始化，配置并启动后台研究；桌面退出后继续；备份 SQLite/CAS/配置，升级 schema，
恢复到新目录，重建索引和投影，研究编号、工件、cursor、checkpoint 和终态一致。

### A09 材料提取与公式保真

从打包 UI 上传真实含公式 PDF、TeX 源、公式图片和纯文本；保留原件，产生带解析器版本的提取工件，
展示页/段/公式锚点和原文—提取差异。至少一个 OCR/解析错误由用户修订并进入合同或 Claim 引用；刷新、
后台重启和下载后锚点不漂移。只检查摘要或 UTF-8 不算通过。

### A10 研究级自动 Claim 闭环

选择研究级开放或困难问题的一条路线，由真实研究模型产生研究稿；系统把它规范化为多个原子候选 Claim、
前驱与类型，自动路由至少两类异质 verifier，一项拒绝后经反馈修复，后续 Worker 从有效图检索复用；新事实
触发组合义务更新并形成或诚实阻塞 ClosureWitness。全程不由人手填数据库字段，不用 `n+0=n`、奇数和或
合成图冒充。

### A11 远域套利消融

对预先冻结题池，以完全相同模型、工具、候选数、预算和源侧终局 verifier 实际运行
`direct / near / far-random / far-retrieval / full-RK`。保存所有 BridgeOpportunity 特征、死亡测试、被拒桥、
BridgeSpec、成本、证书长度和终局 verdict，报告完整分母与置信区间。验收只证明实验基础设施与数据诚实；
是否远域更优由结果决定，不能作为通过条件。

### A12 Zhao 当前版净室复刻与历史迁移

在冻结当前树和新数据根上分别运行 Zhao 的 `CLEAN_ROOM_REDISCOVERY` 与
`IMPORTED_CERTIFICATE_VERIFICATION`，前者不得给 Worker 注入历史数学结论，后者必须列出每个导入证书
及验证结果；两条 run 在谱系中清楚关联但不合并证据。另将一批 `N2_AJT5` 手工历史迁为候选材料/Claim，
证明未经过当前验证门的记录不会进入有效事实图。净室未重现原结论时仍如实通过流程验收并报告科研结果。

### A13 批量 arXiv 开放问题流水线

冻结日期、学科、版本和排除规则，真实抓取一批 arXiv 记录，追踪至少一个修订/撤稿边界，抽取并人工抽查
开放陈述，去重、恢复定义/量词/假设、冻结题池和总分母，批量创建研究。至少一个候选进入机器证书与异源
复核，专家/作者未确认时保持外部待办。最终报告列出所有纳入、排除、失败和阻塞项，不用成功截图代替分母。

## 11. 成熟实现复用决定

- Danus：继续改造复用逐 Claim 图、BM25、依赖闭包、级联撤销、恢复和论文装配；不得形成第二真值源；
- Danus Matlas 客户端：在保留来源和 Apache/上游归属后改造为统一 ToolCatalog 适配器；只复用薄客户端，
  不宣称取得 Matlas 服务端、语料、依赖图或向量 index；
- Archon Horizon `a4565a48...`：改造复用 React DAG 工作台、`DagNetwork`、`dagGraph`、Graphviz WASM
  Worker、章节折叠、布局缓存/预取、子日志和看板交互；不得复用 hgraph 文件目录作为 RK 真值；
- Archon：可借鉴 LeanDAG/日志视图，不再引入另一套图引擎；
- OpenCovibe：只作为 Tauri 桌面生命周期、设置和更新实现的候选来源；其 Svelte UI 不与已冻结 React
  前端并存；
- 第三方代码进入仓库前保留 LICENSE、NOTICE、原始提交、修改说明和文件级归属。

## 12. 明确非目标

- 不解决“任意开放问题一定能证明”；
- 不展示或保存模型隐藏思维链；
- 不提供任意 Shell、远程桌面或通用代码托管；
- 不建设多租户计费、企业目录、复杂组织层级或通用权限语言；
- 不让模型、前端、工具适配器或管理员直接赋予数学真值；
- 不把全部节点、Worker 和日志强行放在一张画布；
- 不在本批次扩展新题型、无关 Lean/jixia 功能或额外哈希机制；
- Rethlas 的上游 504 保持诚实外部阻塞，不能阻止其他能力完成，也不能用重试刷成通过；
- 不承诺远域一定优于近域或直接路线，不承诺批量 arXiv 流水线产生新数学，不把 Matlas 无命中当新颖性；
- 不把 Matlas 托管数据复制、训练或重建成未获许可的本地服务端。

## 13. 当前实现基线与最新工作队列

截至本版，旧台账中的 `52/53` 只描述中文 CLI/ResearchKernel 的历史能力与一个 Rethlas 外部阻塞，不能
作为本产品的完成分母。它可证明部分合同、事实图、验证器、工具、闭合和论文代码值得复用，但不证明
`ResearchProduct` HTTP/ProductReceipt/SSE、daemon、浏览器上传、session、ReviewTask、FTS/GraphSlice、
材料 OCR、Matlas、多源文献图、远域策略、批量题池、科学计算 UI、管理、安装升级备份、Tauri 和全旅程
可访问性已经实现。

`work-packages.md` 中全部 C/S/D/P/B/F/I/R 工作包在本版冻结时均为 `NOT_STARTED`。后续状态必须按
`PRODUCT_E2E / BACKEND_ONLY / FRONTEND_ONLY / SPEC_ONLY / EXTERNAL_BLOCKED / RESEARCH_RESULT_PENDING`
逐包更新；旧 `E2E_PASS` 不得自动迁移。科研实验得到负结果不等于产品失败，只要流程、分母、终局 verifier
和边界完整；外部服务、专家或作者未到位则保持对应阻塞。

## 14. 最终完成定义

完整产品完成是：真实数学用户能从提交开放或困难问题开始，经合同、文献、多路线、逐 Claim、事实
检索、模型与科学工具、形式验证、失败修复、组合、人工高层干预、独立审查和中断恢复，最终获得
诚实状态、完整卷宗及满足精确发布门的论文；文献审查者能从材料原文、Matlas/多源搜索图与适用性核查
形成诚实边界；研究团队能运行远域消融、Zhao 净室复刻和带完整失败分母的 arXiv 题池；管理员能安装、
配置、后台运行、观察成本与故障、升级、备份和恢复。所有非排除项必须有当前树正式产品入口的发布级
证据，或保留明确外部阻塞/科研待验证结果，不能被静态 UI、测试、旧 52/53 回执或单题演示替代。
