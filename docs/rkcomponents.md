# RK-PRD-2 部件与权重实接审计

> 2026-08-12 证据口径更正：本文下方保留了旧远程数值作历史记录，
> 不再默认它们是当前树发布证据。当前树产品 run
> `019ff814-eb93-7b93-82c9-b96c88f444a3` 真实调用了 Crossref、研究模型、
> LeanSearch、jixia 和 Lean；确定性工具 run
> `019ff82c-212d-7100-af73-f8e759676f0d` 真实调用 Z3、SymPy 和精确枚举，
> 三者均为 `COMPLETED`。Rethlas 仍是 `SCAFFOLD`，QED-Nano/DeepSeek-Prover
> 仍只有旧 benchmark 与当前调度接缝，不是当前产品 E2E。权威口径以
> `prdevidenceaudit.md` 为准。

审计日期：2026-08-12。状态词：

- `E2E_PASS`：当前树在远端真实调用且证据已回传；
- `SMOKE_PASS`：真实组件级调用通过，但未进入主 E2E；
- `ADAPTER_TESTED`：adapter/契约测试通过，未完成真实全链；
- `ASSET_ONLY`：资产存在或模型加载通过，但没有本次调用绑定；
- `MISSING/OPTIONAL_MISSING`：未接；
- `BY_DESIGN`：需人类或闭源服务，不能当成本地权重下载。

| PRD 部件/角色 | 远端资产或版本 | 状态 | 真实接线与证据 | 仍有限制 |
|---|---|---|---|---|
| ResearchKernel | 当前工作树 | E2E_PASS | 146 tests；远端 run `019ff489-0268-7e8c-888a-6095c24788a9` | v1 明确限制见 implementation-status |
| Lean 4 | 4.28.0-rc1，binary `3e0d…8bbf` | E2E_PASS | 23.173s（编译 11.761s，公理审计 11.410s）；持久 root-only 密钥签名 receipt；`KERNEL_VERIFIED` | OS 网络/只读 cache 未强制 |
| Mathlib | commit `5352afc…5f44` | E2E_PASS | worktree HEAD、tracked clean、依赖输入摘要均记录 | `.lake` 为共享可写 cache |
| LeanSearch | public endpoint；本地 repo `94f488…18080` | E2E_PASS | 2.796s，返回 8 条 premise candidates | 线上 commit/权重版本 UNKNOWN |
| jixia | commit `995437…d42d2`，binary `8d454…e7b5` | E2E_PASS | 49.636s（预编译 13.289s，分析 36.125s）；全新模块路径回归通过 | 只做结构/状态，不授予真值 |
| DeepSeek V4-Pro | `deepseek-v4-pro` API；当前官方后端名为 V4-Pro-0813 | E2E_PASS | 历史 OpenCode E2E；另在服务器直测 Responses 标准 function 两轮成功 | API 只返回滚动别名，缺少可核验的精确 0813 指纹；不得回填旧运行版本 |
| OpenCode | 1.18.16，binary `8e4ac…1768` | E2E_PASS | `leane2efinal9` 中以非 root、E2E 自派生全工具禁用策略调用 DeepSeek；29.724s | 上游版本在完成事件后偶发不退出；adapter 已按 JSONL `step_finish` 收束并保留强制终止标记 |
| Z3 SMT | 4.15.3.0 | SMOKE_PASS | `unsat`，43ms，固定 argv | 无 proof certificate，保持 heuristic |
| SymPy CAS | 1.14.0 | SMOKE_PASS | 展开式核对，592ms | 无独立证书，保持 heuristic |
| 精确有限枚举 | pinned Python runner | SMOKE_PASS | 11 个见证，46ms | 只有 checker replay 后才可硬化 |
| 通用代码执行 | registered file runner | SMOKE_PASS | 固定代码模板，47ms | 不是任意命令执行；默认 heuristic |
| 文献检索 | Crossref REST | SMOKE_PASS | 真实 bibliographic query 返回 3 项 | 只给候选，NO_HIT 不是证明 |
| 人类同行审查 | `RecordPeerReview` | ADAPTER_TESTED | command/独立性/晋级门测试 | 无真实人类签名 |
| Archon-Horizon | commit `a4565a…122e1` | ADAPTER_TESTED | adapter 和 JSON 契约测试 | 完整真实模型 run 未验证 |
| Rethlas | commit `887cc4…1d040` | PRODUCT_SOFT_BACKEND | GAP_REVIEW 通过注册 `verify_rethlas` 工具调用；批评与修复提示回到同一 Claim | 永久 `SOFT_MODEL`；`correct` 不能独立晋级 |
| Qwen3-Embedding-8B | 15G | ASSET_ONLY | ROCm shape `[3,4096]`、ranking_ok、peak 15.20GB | 未绑定本次公共 LeanSearch |
| Qwen3-Reranker-8B | 16G | ASSET_ONLY | ROCm ranking_ok、peak 16.52GB | 未绑定本次公共 LeanSearch |
| e5-mistral-7b-instruct | 27G | ASSET_ONLY | 权重在远端 | 本次无调用 |
| GPT-5.6 Pro | 闭源服务 | BY_DESIGN | PRD 角色存在 | RK provider 未配置；无本地权重可下载 |
| Codex + DeepSeek | Codex 0.147.0、官方 Responses 配置 | PARTIAL_REMOTE | 服务器纯文本 1/1；custom apply_patch 调用被识别 | shell 0/2 真成功，apply_patch 执行器失败；保持 soft-only，禁止工具控制权 |
| CC Switch / Reasonix | 外部控制器候选 | RESEARCHED_NOT_CONNECTED | 已审阅转换、历史修复与 DeepSeek-native harness 方案 | 未接 RK runner；须经副作用、跨轮、流中断与重放验收 |
| QED-Nano | `lm-provers/QED-Nano`，revision `1016dd…bb`，4B | BENCHMARKED | 远端 ROCm BF16，官方采样参数和 32768 输出上限，五个自然语言证明题；逐题 token/计时/GPU 峰值见模型报告 | 只产生自然语言候选；单样本 smoke 不是论文基准复现，更不授予真值 |
| DeepSeek-Prover-V2-7B | `deepseek-ai/DeepSeek-Prover-V2-7B`，revision `a8d9e1…b7b` | PRODUCT_SOFT_GENERATOR | 远端 ROCm 五题 4 个 Lean 工件过内核、1 个撞上限被拒；硬件计划选中后经统一 local proof-model 工具进入逐 Claim 流程 | 输出仍须独立 Lean replay；不替代真值门 |

## 主 E2E 账本

| 组件 | 输入 token | 输出 token | reasoning token | wall time |
|---|---:|---:|---:|---:|
| LeanSearch public | UNKNOWN | UNKNOWN | UNKNOWN | 2.796s |
| OpenCode / DeepSeek V4-Pro | 1647 | 33 | 1143 | 29.724s |
| jixia | 0 | 0 | 0 | 49.636s |
| Lean replay | 0 | 0 | 0 | 23.173s |

LeanSearch 的 token/API 费用、DeepSeek API 费用以及 jixia/Lean 未采样的 CPU 时间均以
`UNKNOWN_COST` 记账，没有按 0 处理。调用前先做 hard-budget reservation，调用后退款并
写 actual。总墙钟组件计量为 105.329s；launcher 从 05:54:05Z 到 05:55:55Z。

## 证据位置

- 本地当前 E2E：`docs/rkleane2e.json`，SHA-256
  `c458e427c9accaf4f0ecd919ef1a2c78034dc13cc780cb1b82a4b72a432c6aa6`。
- 远端同字节：`/root/ai4math_repro_20260811/rk/leane2efinal9/outputs/rkleane2e.json`。
- 远端 receipt/log：`/root/ai4math_repro_20260811/rk/leane2efinal9.receipt`、
  `/root/ai4math_repro_20260811/rk/leane2efinal9.log`。
- 工具 smoke：`docs/rktoolsmoke.json`，SHA-256
  `865164aeeac5b455a2480cf410180346204af7955693087c119cb0dd5edfb7f3`。
- 权重 ROCm smoke：远端
  `/root/ai4math_repro_20260811/repro_outputs/leansearchv2/rocm_model_smoke.json`。

## 独立代码审查结论

独立子智能体只读审查专门检查了：MVP 假接线、只写不读、后面失败而前面仍成功、
陈旧输出、自报验证、预算只记账、attempt 隔离元数据不落地等问题。它判定旧结果不可
信；本轮已按其 P0/P1 修复自报 replay、工具副作用、租约泄漏、陈旧输出、空输出成功、
硬预算、jixia 旁路和环境摘要。第二轮又修复了跨 attempt 回执重放：由内核预发一次性
nonce，并把 run/attempt/binding/profile/commit/version 全部签入回执。仍未达到的 OS 级敌对隔离与未接模型在上表保持红线，
没有用“适配器文件存在”代替 E2E。
