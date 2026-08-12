# 承重术语表

状态：`NORMATIVE_V1`。下列定义覆盖自然语言歧义；实现不得另造近义状态。

## 身份、版本与顺序

### `rk_run_id`

一个研究运行的稳定 UUIDv7。run 关闭后永不复开；继续研究创建新 run，并在
`parent_dossier_artifact_id` 引用旧卷宗。

### `revision`

**每个 run 独立的单调 int64 计数器**。创建成功后为 0；每个被接受且改变
持久状态的 `apply` 恰好加 1；拒绝、幂等重放、`inspect` 和 `export` 不增加。
一个命令产生多个事件也只增加一次。它不是 claim revision、contract version
或全局事件序号。

权威规则迁移若会改变既有数学投影，也必须为每个受影响 run 写入一个受信的
系统命令与事件，并恰好增加一次 revision。该合成修订使用独立迁移 capability，
目的是使迁移前的并发令牌与卷宗身份立即失效；不得只在数据库中静默改状态。

### `event_cursor`

`events.event_seq` 的数据库级单调整数，仅用于分页；不能替代 run revision。

### `contract_version`

同一 run 内从 1 开始的整数。冻结后不可变；amend 创建 `v+1`，旧版本保留并
标 `SUPERSEDED`。改变数学对象必须另有 `USER_APPROVER`。

### `claim_id`

稳定业务 UUIDv7。陈述修改不会复用旧身份：若语义变化，创建新 claim 并以
`SUPERSEDES` 关系引用旧 claim；仅格式规范化可保留 identity 并增加 statement
revision。

### `statement_hash`

对声明对象的 RFC 8785 风格受限规范 JSON（本项目采用 `composition.md` 的精确
序列化子集）做 SHA-256。它是内容标识，不是 claim 身份。

## 请求与权限

### `request_id`

调用方生成的 UUIDv4。作用域为 `(rk_run_id, request_id)`；同作用域、同请求摘要
重放返回原回执；同 ID 不同摘要拒绝 `IDEMPOTENCY_KEY_REUSED`。

### `command_id`

内核在首次接收 request 时生成的 UUIDv7。幂等重放保持不变。

### `actor_capability`

宿主签发的 HMAC-SHA256 凭证文件，不是请求体中自报的角色字符串。凭证绑定
`capability_id`、主体、允许动作、run scope、签发/到期时间与 nonce。内核只在
入口验证；事件记录 `capability_id`，不记录凭证或密钥。

### `missing_condition`

机器可操作的拒绝解释，含稳定 `code`、对象路径与参数；不是自由文本理由。

## 工件与证据

### `artifact`

CAS 中一段不可变原始字节及其 provenance。相同字节可去重为同一 artifact，
但不同提交来源仍产生不同 evidence/provenance 记录。

### `evidence`

对 artifact 在确定 claim、contract version、scope 下的解释性提交。接收 evidence
只说明格式、来源和 scope 合法，不说明数学结论成立。

### `EvidenceRoot`

产生硬证据的独立来源根。相同证书重复重放仍是一个根；不同验证程序若共同消费
同一错误中间对象，独立性按来源图降低。

### `ApproachRoot`

一条数学思路在接触其他路线以前的最早可恢复祖先。交叉授粉后产生新的派生根，
不得继续声称双方完全独立。

### `scope`

三元组 `(claim_id, contract_version, statement_hash)`，可再附局部 domain。缺任一
承重项即不能参加 verdict。

### `provenance`

来源 actor、输入 artifact、工具/模型、commit、环境 profile、时间、父事件与
执行 binding 的有向记录。不能只写“由某模型生成”。

## 运行与路线

### `route`

为一个或多个 claim 服务的数学策略容器；有稳定 route ID、表示、工具族、方法
根和预算。route 状态不等于 claim 真值。

### `attempt`

route 的一次隔离执行。每个 attempt 有独立写目录、lease、输入快照和 execution
binding。重试必须新建 attempt，除非 adapter 明确支持同一外部 run 的 resume。

### `lease`

宿主对 attempt 写集的限时独占授权。TTL 到期只允许回收执行权，不允许推断该
attempt 数学失败。

### `novelty_delta`

相对同 route 最近一个已付费 batch 新增的**可检查对象集合**，只能来自：新 claim、
新反例、首个未闭合义务、新证书、新表示映射或新适用定理。纯改写、更多解释、
同源投票不计。`novelty_delta_count` 是去重后的对象数，不设主观连续分值。

### `expected_information_gain`

调度用离散等级 `0..3`，不是概率或物理单位：0 无新可检查对象预期；1 可能淘汰
单路线；2 可区分至少两个承重分支；3 可闭合/证伪根义务。必须附 `decision_ids`
说明将改变哪些预注册决策；无 decision ID 一律为 0。

## Claim 图与组合

### `selected_subgraph`

为父 claim 晋级而选择的最小 DAG 投影：父/子 claim 的指定 statement revision、
方向化边、组合义务、bridge 与 verification ref。不包含 UI、时间或自由文本日志。

### `selected_subgraph_digest`

按 `composition.md` 的 NFC、排序、无浮点规范化算法得到的 SHA-256。任何承重
节点、边、版本或义务改变都必须改变 digest。

### `CompositionObligation`

说明“局部成立为何能推出父结论”的显式义务，不是一个自然语言备注。它由带类型
字段组成，且每项分别标 `MACHINE_CHECKED | HUMAN_ATTESTED | OPEN | NOT_APPLICABLE`。

### `composition_rule`

闭集枚举，而非自由文本：`LEAN_DECLARATION`、`CHECKER_PROFILE`、
`HUMAN_ARGUMENT`、`HYBRID_CUTS`、`DIRECT_EDGE`。具体规则载荷存 artifact。

### `closure_theorem`

组合规则的可寻址承重对象：Lean declaration 名、checker profile + certificate
schema，或人工论证 artifact。空字符串、只写定理名、未固定版本均不合格。

### `coverage_statement`

对局部 domain 如何覆盖父 domain 的结构化陈述。机器模式必须是 verifier 可读
输入或 Lean term；人工模式可引用文本 artifact，但只能得到人工轴。

### `open cut`

所选子图中仍需外部结论、未满足兼容条件或未签认推理才能到达父 claim 的边界。
所有 open cut 必须有 ID；“未声明开放 cut”指图分析发现但 witness 未列出的 cut。

### `OBLIGATION_DISPLACEMENT`

所谓闭包义务在同一契约下蕴含原命题，或验证成本/量词强度不低于原命题，却被
包装成中间步骤。该标记不说路线错误，只禁止把搬家计作进展。

## 证据与裁决轴

### `machine verdict`

仅由 Lean kernel 或预注册确定性 checker 的 clean replay 产生。枚举无命中、
模型 judge、人工签字都不能填此轴。

### `semantic verdict`

形式陈述与原契约之间的忠实性：`UNREVIEWED | TESTED | HUMAN_ATTESTED |
REFUTED`。它与 kernel 编译正交。

### `peer verdict`

独立同行对自然语言证明正确性的 scope 化裁决：`UNREVIEWED | ACCEPTED |
REJECTED | NEEDS_REVISION`。它不制造 machine evidence。

### `quality verdict`

对价值、清晰度、原创性和发表完成度的评价：`UNREVIEWED | ACCEPTED |
REJECTED | NEEDS_REVISION`。它不证明正确性。

### `HUMAN_ATTESTED`

具名/稳定 reviewer capability 对明确 statement hash、contract version、subgraph
digest 和检查清单的签认。可审计但不可机械重放，禁止进入 hard-evidence 训练池。

### `PEER_ACCEPTED`

满足合同规定的独立同行数、来源图与 scope 后的 peer 轴状态。不是“LLM 多数认为
正确”，也不是无类型 `verified`。

### `CLEAN_REPLAY_LOCAL`

宿主预注册环境中，从只读输入重放成功；记录 toolchain、dependency commit、
镜像 digest、命令、退出码、输出 hash、axiom/sorry 扫描。只说明该形式/证书对象
通过，不自动说明原题语义忠实。

### `NO_HIT_NOT_A_PROOF`

有限搜索没有发现反例。必须记录域、枚举/抽样方式、seed、完整性和边界；永不
晋升为 proof。

## 失败、修订与终态

### `first_failed_obligation`

按方法卡 proof spine 或组合 DAG 的稳定拓扑顺序，第一个未满足/被证伪的义务
ID。修复默认只使其下游失效。

### `CONTRACT_DEFECTIVE`

原题契约在量词、定义域、对象、成功标准或一致性上有可证据化缺陷，导致当前
研究对象不宜继续。它是合法终态，不是 route failed 的同义词。

### `SIDE_FINDING`

运行中产生但不等同于根合同目标的结果。记录、通知、可另建 run；不得自动替换
当前项目或根 claim。

### `UNRESOLVED`

预算/证据结束时根 claim 未获合同要求的裁决。它是诚实完成运行，不是系统异常。

### `terminal claim`

run finalize 时 dossier 声称的根或局部结果。每个 terminal claim 必须有完整四轴
裁决、scope、closure witness 或显式开放义务。

## 验收词

### `baseline`

AC5 同一 fixture、同模型/effort/预算、同随机种子集合下的对照策略。B0 无方法卡；
B1 只有方法名；T 使用完整卡。具体协议见 `ac5.md`。

### `independent acceptance`

两个互盲 reviewer 先独立评分；分歧由第三位裁决。reviewer 不得看到策略标签、
成本结果或另一人的意见。模型 reviewer 只能用于管线 smoke，不计本指标。

### `invalid promotion`

系统把不满足真值、scope、组合或独立性门的对象提升到 `ROUTE_PROVED` 或终端
已证明状态。拒绝一个其实正确但证据不足的对象不属于 invalid promotion。
