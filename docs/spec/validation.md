# 配套实施规格机械验收记录

日期：2026-08-11

本记录验证“规格是否自洽、依赖接口是否如实”，不代表 ResearchKernel 已实现，亦不
代表任何数学结论获得新证明。

## 1. 冻结边界

```text
prd2.md bytes: 58478
prd2.md lines: 1880
prd2.md SHA-256:
CAA52B1BA4BB9B8366E05AD5F02875D50865754D6AB38577A2741BE04EE71FAC
```

与 `freeze2.json` 冻结值一致。本轮未修改 `prd2.md`、`prd1.md` 或 reviews。

## 2. SQLite schema

使用工作区自带 Python/SQLite 在 `:memory:` 执行完整 `schema.sql`：

```text
executescript: PASS
user tables: 27
PRAGMA integrity_check: ok
PRAGMA foreign_key_check: []
schema_migrations rows after raw migration: 0 (expected)
```

27 张构成：冻结 `§27.1` 逐项列出的 25 张业务表，加 `schema_migrations` 与
`capabilities`。migration runner 须在 migration commit 后另事务写真实文件 hash，
避免迁移自包含自身 hash。

## 3. JSON 与 command 覆盖

本目录 10 个 JSON 首轮均可 UTF-8 解析。之后 adapter fixture 更新仍再次解析。

服务器 `/root/ai4math_repro_20260811/env/rethlas` 的 `jsonschema` 对
`command.schema.json` 和 `receipt.schema.json` 执行
`Draft202012Validator.check_schema`：

```text
command schema self-check: PASS
valid FreezeContract apply: 0 errors
same request missing contract_version: REJECTED
receipt schema self-check: PASS
valid accepted receipt: 0 errors
accepted=false but rejection_code=null/missing_conditions=[]: REJECTED
```

`command.schema.json` 的 command union 为 27 个；逐项搜索 `transitions.md`，缺失为
空集合。27 = 21 个公开 command + 6 个宿主 command。

## 4. fixture 数量与结构

```text
POSITIVE: 8
NEAR_MISS: 8
GLUE_TRAP: 8
method cards: 5
```

五张卡 ID：

```text
MC_DOUBLE_COUNTING_V1
MC_MINIMAL_COUNTEREXAMPLE_V1
MC_PARITY_INVARIANT_V1
MC_AUX_INDUCTION_V1
MC_SYMMETRY_ORBIT_V1
```

fixture 的数学期望尚未由独立 reviewer 运行 AC5 实验；当前只完成主规格审计与 JSON
结构检查。AC5 不能在实验前标 PASS。

## 5. N2 小切片

`n2slice.json` 引用的 12 个本地工件逐项检查 path 存在、byte_count 和 SHA-256：

```text
checked: 12
size/hash mismatches: 0
```

导入预期明确保留：N2 = `N2_AJT5`；根状态 `UNRESOLVED`；R01/R02 退役；有限无
命中不是证明；R03 sparse 是纸面/同行路线局部结果而非 machine；Lean 为部分覆盖。

## 6. AMD 外部依赖

### Archon-Horizon

固定 commit `a4565a48b4b84189384a05b9a4e6409e875122e1`。在临时 workspace 用 pinned
源码入口运行：

```json
{
  "dry_run": true,
  "rounds": [{"round": 0, "planned": ["workspace-all"]}]
}
```

exit code 0；stderr 含 `Starting Run (ID: 0001)` 与 `Run 0001 finished`；临时目录
结束即清理。它只验证 JSON adapter contract，不验证真实模型研究 run。

### Rethlas

固定 commit `887cc46427636bbdd235160a112f9a30ae81d040`。health 已通过；既有 receipt
显示 Codex CLI 0.80 + DeepSeek v4-pro 的单轮全循环两次失败、无 verification JSON、
未调用 Lean/jixia。因此只标 `VERIFIER_HEALTH_PASS / FULL_LOOP_FAIL`。

### LeanSearch-v2

固定 commit `94f4888cbaf9f4322535755f86cbac690ec18080`。已核源码 endpoint、请求和
SearchResult 类型；未在 AMD 本地启动 full service。现实现使用 CUDA/cuVS 且 rerank
布局需要 embedding GPU + 至少一个 reranker GPU，v1 固定公共 retriever-only。

### jixia

固定 commit `755fde27a9cf1fb25c17a015b1cc4ac68384aa63`，checkout toolchain 4.29.0。
已核构建/调用/`-i` 与精确版本要求；尚未执行本规格自己的 jixia fixture smoke。

## 7. 明确未完成

- 未创建 `magi/rk` 代码仓库；
- 未实现/性质测试 TransitionGuard；
- 未做 CAS crash injection、12 进程幂等、secret/path/zip 安全测试；
- 未运行 360 次 AC5 三臂实验；
- 未跑真实 Archon-Horizon 模型轮；
- 未修复 Rethlas + Codex 0.80/DeepSeek v4-pro 兼容失败；
- 未移植 LeanSearch CUDA/cuVS 到 ROCm；
- 未用 jixia 分析实际 Lean fixture；
- 未把 N2 工件真实写入 ResearchKernel DB（因为实现尚不存在）。

因此本目录可称 `IMPLEMENTATION_READY_SPEC`，不可称 `IMPLEMENTATION_COMPLETE`。
