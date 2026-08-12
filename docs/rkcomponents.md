# RK-PRD-2 部件与权重实接审计

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
| DeepSeek V4-Pro | `deepseek-v4-pro` API | E2E_PASS | 经 OpenCode 29.724s；1647 in / 33 out / 1143 reasoning；tool_calls=[] | 闭源服务版本由 provider 响应标识，不是本地权重 |
| OpenCode | 1.18.16，binary `8e4ac…1768` | E2E_PASS | `leane2efinal9` 中以非 root、E2E 自派生全工具禁用策略调用 DeepSeek；29.724s | 上游版本在完成事件后偶发不退出；adapter 已按 JSONL `step_finish` 收束并保留强制终止标记 |
| Z3 SMT | 4.15.3.0 | SMOKE_PASS | `unsat`，43ms，固定 argv | 无 proof certificate，保持 heuristic |
| SymPy CAS | 1.14.0 | SMOKE_PASS | 展开式核对，592ms | 无独立证书，保持 heuristic |
| 精确有限枚举 | pinned Python runner | SMOKE_PASS | 11 个见证，46ms | 只有 checker replay 后才可硬化 |
| 通用代码执行 | registered file runner | SMOKE_PASS | 固定代码模板，47ms | 不是任意命令执行；默认 heuristic |
| 文献检索 | Crossref REST | SMOKE_PASS | 真实 bibliographic query 返回 3 项 | 只给候选，NO_HIT 不是证明 |
| 人类同行审查 | `RecordPeerReview` | ADAPTER_TESTED | command/独立性/晋级门测试 | 无真实人类签名 |
| Archon-Horizon | commit `a4565a…122e1` | ADAPTER_TESTED | adapter 和 JSON 契约测试 | 完整真实模型 run 未验证 |
| Rethlas | commit `887cc4…1d040` | ADAPTER_TESTED | verifier health 既有通过；soft-only adapter 测试 | Codex 0.80 + DeepSeek full loop 失败 |
| Qwen3-Embedding-8B | 15G | ASSET_ONLY | ROCm shape `[3,4096]`、ranking_ok、peak 15.20GB | 未绑定本次公共 LeanSearch |
| Qwen3-Reranker-8B | 16G | ASSET_ONLY | ROCm ranking_ok、peak 16.52GB | 未绑定本次公共 LeanSearch |
| e5-mistral-7b-instruct | 27G | ASSET_ONLY | 权重在远端 | 本次无调用 |
| GPT-5.6 Pro | 闭源服务 | BY_DESIGN | PRD 角色存在 | RK provider 未配置；无本地权重可下载 |
| Codex 5.6 | 闭源服务 | BY_DESIGN | PRD 角色存在 | RK provider 未配置；无本地权重可下载 |
| QED-Nano | `lm-provers/QED-Nano`，revision `1016dd…bb`，4B | BENCHMARKED | 远端 ROCm BF16，官方采样参数和 32768 输出上限，五个自然语言证明题；逐题 token/计时/GPU 峰值见模型报告 | 只产生自然语言候选；单样本 smoke 不是论文基准复现，更不授予真值 |
| DeepSeek-Prover-V2-7B | `deepseek-ai/DeepSeek-Prover-V2-7B`，revision `a8d9e1…b7b` | BENCHMARKED | 远端 ROCm BF16、seed 30、官方 CoT prompt、8192 输出上限；五题中 4 个最终 Lean 工件过内核，1 个撞上限并含 `sorry` 被拒 | 已有统一 local proof-model adapter，但尚未替代主 E2E 的 OpenCode worker |

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
