# RK 远端模型与 Lean 链路实测报告

日期：2026-08-12。机器：`36.150.116.220:30412`，AMD Radeon 48GB，PyTorch
2.9.1 + ROCm 7.2。本文只报告本次可复现实测，不把论文数字、模型文本或静态分析
冒充 Lean 内核结论。

## 结论

1. OpenCode 不是卡在模型请求本身。1.18.16 能发出完整 `step_finish`，但某些 headless
   运行因残留事件循环/文件监听不退出。adapter 现以协议完成事件收束，留 2 秒清理期，
必要时终止进程组并记录 `forced_termination`。当前 E2E 实测 11.377 秒、无工具调用。
   收束器只接受 `step_finish.reason=stop`；`tool-calls` 之类中间 step 不会提前结束整轮。
2. Lean 链慢的主要原因是重复启动完整 Mathlib 环境，而不是 `n + 0 = n` 难：
   `import Mathlib` 的小文件每次约 11.5–12 秒；缩小到 `Mathlib.Data.Nat.Basic` 后约
   3.7 秒。热跑并没有明显改善。
3. jixia 当前先编译 `.olean`（约 12 秒），再做结构分析（约 35 秒），合计约 46–48
   秒。最终 Lean replay 又独立编译一次并跑一次 `#print axioms`，约 23 秒。独立 replay
   不能为省时与生成方合并；可优化的是精确 import、常驻 Lean 服务和避免非承重点做
   全量 jixia。
4. QED-Nano 五个基础自然语言证明都通过逐题逻辑审阅；DeepSeek-Prover 五个 Lean 题中
   4 个通过内核和公理审计，1 个在 8192-token 上限处退化并被拒绝。专用模型有用，但
   不能取消验证器和 token 熔断。

## PRD 中的正确角色

原 PRD 把三者定位得很清楚：

- OpenCode + DeepSeek V4-Pro：当前远端执行外壳/LeanWorker 候选生成路径；
- QED-Nano：自然语言数学证明的高吞吐候选生成器，可用于有廉价验证器、高中心性且
  确认属于 `GENERATION_HARD` 的 RSA；没有数学真值权限；
- DeepSeek-Prover：Lean 专用候选生成器；生成结果只能在独立、固定环境 replay 后成为
  机器证据。

PRD 还规定 QED-Nano RSA 初始只开一个节点，且默认不超过非 5.6 token 预算的 10%。
本次只做五题单轮 smoke，没有冒充论文的完整 IMOProofBench 或多轮 RSA 复现。

## OpenCode 挂死：原因、修复与实测

旧成功工件里出现过 `tool_use bash`，与后来的禁工具策略冲突，已经作废。随后复现出
另一个独立问题：OpenCode 已输出文本、usage 和 `step_finish`，日志也进入 disposing，
但进程仍不退出。这与上游公开的 headless hang 症状一致。

修复包含：

- 全局和 build agent 两层 `permission={"*":"deny"}`、`tools={"*":false}`；
- E2E 自己从源配置派生策略配置并记录 SHA-256，不再盲信外部已经处理好的配置；
- 每次调用使用全新的 HOME/XDG 目录和工作目录；
- 以非 root `ai4mathpod` 执行；
- JSONL runner 识别 `step_finish`，并把协议完成与进程是否被清理分开记录；
- 空文本、无完成事件、工具调用、旧 runtime 目录均 fail closed。

远端 `leane2eopenfix1` 首次证明修复有效：OpenCode 11.377 秒，1648 input、35 output、
346 reasoning、1920 cache-read tokens；`tool_calls=[]`，协议完成，未发生强制终止。
最新 `leane2efinal9` 又以 E2E 自派生策略配置和全新 Mathlib worktree 重跑：OpenCode
29.724 秒，1647 input、33 output、1143 reasoning，随后 jixia、Lean replay 通过，Claim
到达 `KERNEL_VERIFIED`。

## Lean / jixia 为什么慢

| 测项 | 实测墙钟 | 解释 |
|---|---:|---|
| 完整 `import Mathlib` 小源文件 | 11.47–11.96s/进程 | 装载 Mathlib 环境占主导 |
| 同一源文件连续热跑 | 11.58–11.96s/进程 | OS cache 没消除 Lean 进程初始化成本 |
| 精确 import `Mathlib.Data.Nat.Basic` | 3.665–3.775s/进程 | 比全 Mathlib 快约 3.2 倍 |
| jixia 预编译 | 13.289s | jixia 需要对应 `.olean` |
| jixia 结构分析 | 36.125s | 声明、符号、elaboration、line map 全量抽取 |
| Lean clean replay | 11.761s | 独立编译候选 |
| Lean axiom audit | 11.410s | 独立 `#print axioms` 运行 |

因此一次当前链路约为 `13.3 + 36.1 + 11.8 + 11.4 = 72.6s` 的 Lean/jixia 工作，再加检索和模型。
代码现在把 `preflight_compile`、`analysis`、`compile`、`axiom_audit` 分阶段写入 adapter
结果，下一次 E2E 不再只有组件总时间。

推荐的性能修复顺序：

1. LeanWorker 输出精确 imports，避免默认 `import Mathlib`；
2. 只有承重或需要局部状态反馈的节点跑 jixia 全量分析；
3. 为交互快检做常驻 Lean server，但最终 replay 继续使用独立新进程；
4. DeepSeek-Prover 撞 token 上限时立即记 `GENERATION_LIMIT`，不要把长输出继续喂给 Lean；
5. 多候选先用便宜语法/禁用词门，再进入两次内核过程。

## QED-Nano 4B

权重：`lm-provers/QED-Nano`，revision
`1016ddef8dd40552b97216ce34e3fece2fffa2bb`，约 8.06GB。上游单轮默认：BF16、
`temperature=0.6`、`top_k=20`、`top_p=0.95`、`do_sample=true`、最多 32768 新 token。

| 题目 | 输出 token | 秒 | 峰值 GPU | 审阅 |
|---|---:|---:|---:|---|
| √2 无理 | 2123 | 84.953 | 8.40GB | 正确 |
| 前 n 个奇数和 | 2428 | 94.044 | 8.46GB | 正确 |
| 素数无穷 | 2276 | 87.325 | 8.43GB | 正确 |
| `a+b+c=0` 立方恒等式 | 3193 | 128.352 | 8.56GB | 正确 |
| 二元 AM-GM | 1230 | 44.935 | 8.28GB | 正确 |

合计 11,250 输出 token、439.609 秒。五份输出都有明显“内部思考 + 最终答案”冗余，
所以它适合批量候选和 RSA，而不适合直接展示或授予真值。这里的“正确”是对五个基础
证明的逐步逻辑审阅，不是形式化认证，也不能外推到奥赛难题成功率。

## DeepSeek-Prover-V2-7B

权重：`deepseek-ai/DeepSeek-Prover-V2-7B`，revision
`a8d9e14432b2e8dd9df2a4d4e70f1ba9bc8d9b7b`，约 13.83GB。使用官方 CoT prompt、
seed 30、BF16、greedy、最多 8192 新 token。

| Lean 题 | 输出 token | 生成秒 | 结果 |
|---|---:|---:|---|
| `Nat.add_zero` | 770 | 32.806 | Lean + 公理审计通过 |
| `Nat.add_comm` | 1266 | 51.699 | 通过；只有 linter warning |
| 实数平方非负 | 740 | 29.437 | 通过 |
| `List.reverse.reverse` | 8192 | 473.248 | 失败；重复 tactic、最终含 `sorry` |
| 官方 README 风格绝对值题 | 779 | 31.103 | 通过 |

合计 11,747 输出 token、618.293 秒。4 个接受工件没有 `sorry/admit/axiom/unsafe/
native_decide`，Lean 4.28.0-rc1 + 固定 Mathlib 编译通过，`#print axioms` 未出现
`sorryAx`。失败题说明默认 CoT 会进入长循环；生产路由必须在 token 上限处明确失败，
不能因为模型进程 exit 0 就把 attempt 标成证明成功。

## 接线与权限

新增 `LocalProofModelAdapter` 把固定本地推理命令纳入 `StrategyRunner`：调用者只能提交
输入工件和输出相对路径，不能选择任意命令；adapter 校验 runner 二进制哈希、拒绝
旧输出，回传文本、token、加载/生成计时和 GPU 峰值。无论模型名称如何，固定
`trust_limit=SOFT_CANDIDATE_ONLY`、`machine_axis_effect=UNCHANGED`。

这表示两模型已经“能由 RK 的统一执行缝隙调用”，但主 Lean E2E 暂仍用
OpenCode + DeepSeek V4-Pro。把 DeepSeek-Prover 设为默认 LeanWorker 属于后续路由策略，
不是本次 smoke 自动完成的数学结论。

## CLI 与复现

中文命令已经是英文命令的严格别名，wire JSON 仍使用稳定英文 operation：

```text
rkctl 创建 --凭据文件 cap.json < create.json
rkctl 应用 --凭据文件 cap.json < command.json
rkctl 查看 --句柄 RUN_ID --游标后 10 --条数 100
rkctl 导出 --凭据文件 cap.json < export.json
```

远端模型与 Lean 复现入口：

```text
python scripts/rkmodelbench.py qed MODEL_PATH OUTPUT_DIR
python scripts/rkmodelbench.py deepseek-prover MODEL_PATH OUTPUT_DIR
python scripts/rkleanverifybench.py OUTPUT_DIR MATHLIB_PROJECT LAKE_BINARY
bash scripts/rkleanbench.sh MATHLIB_PROJECT TOOLCHAIN_ROOT JIXIA_BINARY OUTPUT_DIR
```

## 证据

- `docs/evidence/models/qedreceipt.json`：QED 全部 token/计时/峰值；
- `docs/evidence/models/dspreceipt.json`：DeepSeek-Prover 全部生成回执；
- `docs/evidence/models/dsplean.json`：逐题 Lean 编译与公理审计；
- `docs/evidence/models/opencodee2e.json`：OpenCode 修复后的完整 RK E2E；
- `docs/evidence/models/*.txt`：五份 QED 原始输出；
- 远端模型目录各自含 `rk_download_receipt.json` 和权重分片 SHA-256。

## 尚未完成或不能外推的事

- 没有复现 QED-Nano 论文的完整 IMOProofBench、agent/RSA 或百万 token 设置；
- 五个简单 smoke 不能证明 QED-Nano 对当前 N2/AJT 承重节点有效；
- DeepSeek-Prover 4/5 只说明当前小样本中有实际效用，不能当总体准确率；
- 当前 OS 层仍不是敌对隔离的只读/断网 clean room；
- 公共 LeanSearch 没有服务端部署 commit attestation。
