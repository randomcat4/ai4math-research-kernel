# RK-PRD-2 配套实施规格

状态：`IMPLEMENTATION_BASELINE_V1`

上位规格：`../prd2.md`，SHA-256
`CAA52B1BA4BB9B8366E05AD5F02875D50865754D6AB38577A2741BE04EE71FAC`。

本目录补齐物理接口、数据模型、纯决定规则、外部依赖和可重放验收夹具。
它不修改、覆盖或重新冻结 `prd2.md`。出现冲突时按以下顺序处理：

1. `prd2.md` 的数学与证据不变量优先；
2. 本目录负责把未定实现选择固定为可施工约束；
3. 若二者真实冲突，停止实现并登记 `SPEC_CONFLICT`，不得自行解释；
4. 对数学真值，任何软件状态都不能替代原始证据和独立复核。

## 1. 冻结版已知缺陷

以下仅登记，不回写冻结文件：

- `prd2.md` 的 `§10.6` 出现两次；引用时使用逻辑别名
  `S10_6_TERMINALS` 与 `S10_6_TRANSITIONS`，不得只写编号。
- `§27.1` 自称“最小表”，逐项计数实际为 25 张，不是 24 张。本规格完整实现
  这 25 张，并另加 `schema_migrations` 与 `capabilities` 两张基础设施表。
- `§0` 的“自足”承诺与 `§38`、`reviews/r1–r3` 的来源说明不完全一致。本目录
  不依赖评审文件才能实现；来源只用于 provenance。
- `§32` 的 22–34 人日建立在 schema、协议、fixture 已由实现者心内补齐的假设上。
  完成本规格后，可信内部 v1 的工程基线改估为 **40–60 人日**；不包含数学内容
  生产、研究级 Lean 库建设、模型训练或长期多租户运维。

## 2. v1 唯一物理形态

- 实现语言：Python 3.12；
- 持久层：本地 SQLite 3，WAL，单工作区单写者；
- 工件层：同盘内容寻址存储（CAS）；
- 外部协议：`rkctl` CLI，stdin/stdout 均为一行 UTF-8 JSON；
- v1 不提供 HTTP、本地 socket、gRPC 或可写 Web UI；
- 进程内实现：`ResearchKernel` Python 类；CLI 是其薄封装；
- 外部进程不得直接打开 SQLite；
- 所有时间为 UTC RFC 3339，精度到毫秒并以 `Z` 结尾；
- 所有标识符为 UUIDv7 小写字符串，调用方 `request_id` 例外，固定 UUIDv4；
- 所有摘要为小写十六进制 SHA-256；
- 所有 JSON 数字若参与 digest，只允许整数；成本小数用十进制定点字符串。

命令示例：

```text
rkctl create --cap-file <path> < command.json
rkctl apply --cap-file <path> < command.json
rkctl inspect --handle <run_id> [--after-cursor <int>] [--limit <1..500>]
rkctl export --cap-file <path> < export.json
```

退出码：`0` 接受或成功读取，`2` schema 错，`3` 业务拒绝，`4` revision 冲突，
`5` capability 拒绝，`6` 暂时不可用，`7` 内部错误。业务拒绝仍必须向 stdout
输出合法 `CommandReceipt`。

## 3. 仓库与首行代码

目标实现仓库固定为：

```text
magi/rk/
  pyproject.toml
  src/rk/cli.py
  src/rk/kernel.py
  src/rk/domain.py
  src/rk/guard.py
  src/rk/composition.py
  src/rk/ingest.py
  src/rk/storage.py
  src/rk/dossier.py
  src/rk/adapters/
  migrations/0001.sql
  tests/
```

从工作区根开始的固定开发命令：

```text
python -m venv .venv
.venv/Scripts/python -m pip install -e "magi/rk[dev]"
.venv/Scripts/python -m pytest magi/rk/tests
.venv/Scripts/python -m ruff check magi/rk
.venv/Scripts/python -m mypy magi/rk/src
```

Linux/AMD 上只把解释器路径改为 `.venv/bin/python`。不得让 adapter 的环境取代
内核开发环境。

实现顺序固定为：

1. 复制 `schema.sql` 为迁移 `0001.sql`，在内存库执行；migration runner 在成功后
   另启事务记录文件 hash，迁移文件不得内嵌自身 hash；
2. 从 `json/*.schema.json` 生成或手写领域对象；
3. 实现 `TransitionGuard.decide(snapshot, command, evidence_summary, capability)`；
4. 实现单事务 `apply` 和幂等回执；
5. 实现 CAS stage/finalize/recovery；
6. 通过假适配器与组合闭包夹具；
7. 最后接真实外部工具。

## 4. 文件职责

| 文件 | 唯一职责 |
|---|---|
| `schema.sql` | 完整列、类型、键、索引、枚举约束和 append-only 触发器 |
| `api.md` | CLI、wire format、capability、幂等、错误与事务边界 |
| `json/command.schema.json` | create/apply/export 请求的机器 schema |
| `json/receipt.schema.json` | 回执的机器 schema |
| `transitions.md` | 覆盖 v1 全部命令的纯决定表 |
| `composition.md` | 组合闭包的机器/人工边界与规范 digest |
| `glossary.md` | 承重词的单一含义 |
| `ac5.md` | AC5 基线、测量和判分协议 |
| `adapters/*.md` | 固定外部路径、commit、输入、输出、失败和信任上限 |
| `fixtures/ac5_cases.json` | 8 正例、8 近失配、8 粘合陷阱 |
| `fixtures/n2slice.json` | N2_AJT5 小切片的只读迁移清单 |
| `fixtures/method_cards/*.json` | 首批五张完整方法卡 |
| `manifest.json` | 本规格全部文件的字节数和 SHA-256；最后生成 |

## 5. 不跨越的边界

- 本规格定义“系统什么时候可记录何种裁决”，不证明任何数学命题。
- Rethlas verifier 始终是 `SOFT_MODEL`，即使 JSON 写 `correct`。
- LeanSearch 与 jixia 只提供候选/结构信息，不产生证明权限。
- `HUMAN_ATTESTED` 是有身份、有 scope 的签认，不是机器重放证据。
- `MACHINE` 组合闭包只允许 Lean kernel 或预注册确定性 checker 真正检查了组合
  规则的情形；机器只检查“表填满了”时不得使用该词。
- N2 永久指 `N2_AJT5`。夹具中的局部结果不改变 AJT(5) 的 `UNRESOLVED` 状态。
- 不读取或迁移 `C:\canglan\`。

## 6. 完成定义

一个新实例只有在以下各项同时通过后才可宣称“implementation-ready”：

- `schema.sql` 可在空 SQLite 数据库完整执行，`foreign_key_check` 与
  `integrity_check` 通过；
- JSON Schema 自身可解析，正反例请求的接受/拒绝符合预期；
- 每个 v1 command 在 `transitions.md` 恰有一条命令级入口；
- 三种组合模式均有正例与反例，人工模式不会晋升 machine 轴；
- 四个真实 adapter 的 commit、调用和信任上限被记录；
- 24 个 AC5 fixture 均有预期判定，N2 manifest 中所有本地 hash 匹配；
- `prd2.md` 与 `freeze2.json` 的字节和 hash 未变化；
- `manifest.json` 覆盖本目录所有最终文件且无自身循环哈希。

## 7. 人日重估

| 工作包 | 人日 |
|---|---:|
| schema、迁移、CAS、恢复 | 8–12 |
| CLI、capability、幂等与 TransitionGuard | 8–11 |
| Claim DAG、失效传播、组合闭包 | 7–11 |
| Archon/Rethlas/LeanSearch/jixia 适配 | 7–10 |
| N2 导入、AC1–AC12、并发/安全/故障注入 | 10–16 |
| 合计 | 40–60 |

这是“一名熟悉 Python/SQLite/Lean 工具链的工程师”的净工程量，不按日历并行压缩，
也不含研究数学家的评审时间。拼装降低了模型编排和检索器的研发量，却没有消除
证据边界、迁移、崩溃一致性与组合门的定制工作；后四项才是本产品真正承重部分。
