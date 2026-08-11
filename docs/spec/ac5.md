# AC5 人类证明思路迁移验收协议

状态：`NORMATIVE_PILOT_V1`

本协议只测“方法卡是否让确定性局部节点更可靠/更省验证”，不测开放问题发现能力，
也不把 24 个小题结果外推成研究数学水平。

## 1. 三个实验臂

| 臂 | 可见材料 |
|---|---|
| `B0_NO_CARD` | 题目、普通系统约束、相同工具；无方法卡 |
| `B1_NAME_ONLY` | B0 + 适用方法名一行，如“尝试双计数”；无 spine/哨兵/闭合义务 |
| `T_FULL_CARD` | B0 + 完整 worked method card，可按准入条件拒绝该卡 |

三个臂使用同一模型快照、effort、温度、上下文上限、工具、wall/token 预算与 seed
集合。每次运行在隔离 attempt，不共享草稿或 reviewer 反馈。顺序按
`SHA256(experiment_id + case_id + seed)` 平衡随机化。

## 2. 冻结样本

`fixtures/ac5_cases.json` 恰含：

- `POSITIVE` 8 题：方法卡确实适用；
- `NEAR_MISS` 8 题：表面相似但承重前提缺失/结论错误；
- `GLUE_TRAP` 8 题：局部对象分别成立，但局部到全局组合失败。

不得在看到 T 输出后改题、答案、首失败义务或评分规则。修改 fixture 必须升版本并
整套重跑，旧报告保留。

## 3. 运行次数与预算

- 每个 `(case, arm)` 运行 5 个预注册 seed；总计 `24 × 3 × 5 = 360` 次；
- 每次最多 120k 输入 token、60k 输出 token、20 次工具调用、20 分钟 wall；
- 不允许外部互联网；可用的 Lean/精确检查器对三臂一致；
- 同一 seed 的工具返回 fixture 固定；
- 超限记 `ABSTAIN_BUDGET`，不是错误 proof，也不能从分母删除。

若所选模型为确定性且忽略 seed，仍运行 5 次以暴露服务/agent 非确定性；完全相同
输出按 provenance 去重，但每个 case 的最终判定仍只有一个 case-level vote。

## 4. 输出要求

每次必须提交：

```text
case_id
decision = PROVE | REJECT_CARD | REFUTE | ABSTAIN
proof_spine[obligation_id]
first_failed_obligation_id?
assumption_mapping
composition_obligation
expanded_proof_or_counterexample
tool_receipts
claimed_terminal_state
```

没有这些字段的输出可由 evaluator 读取数学文本，但记 `STRUCTURE_FAIL`。

## 5. 独立评分

两位 reviewer 互盲、独立评分；分歧由第三位裁决。给 reviewer 的包去掉 arm、模型、
成本、seed 与其他 reviewer 结论，保留题目和所用工具的可重放 receipt。

每个 run 的标签：

- `CORRECT_ACCEPT`：证明/反例正确，scope 与 claim 一致，组合闭合；
- `CORRECT_REJECT`：近失配在展开长证明前指出冻结的承重缺口；
- `INVALID_PROMOTION`：错误/未闭合却声称 `ROUTE_PROVED`；
- `CORRECT_LOCAL_ONLY`：局部成立并诚实保持 closure OPEN；
- `WRONG_OR_GAPPED`；
- `ABSTAIN_BUDGET`；
- `STRUCTURE_FAIL`。

reviewer 时间从首次打开包到提交 verdict，以整分钟计；复核 Lean/checker 的机器时间
另计，不混成人时。

## 6. 从 run 到 case

一个 case 在某 arm 下的 case-level 结果取 5 次 run 的多数；平票按安全顺序：

```text
INVALID_PROMOTION > WRONG_OR_GAPPED > ABSTAIN_BUDGET >
CORRECT_LOCAL_ONLY/CORRECT_REJECT > CORRECT_ACCEPT
```

即不把不稳定偶然成功当成通过。成本使用 5 次中位数。

## 7. 指标

对 `POSITIVE`：

```text
acceptance_rate = CORRECT_ACCEPT cases / 8
```

对 `NEAR_MISS`：

```text
early_rejection_rate = 在 expanded proof 超过 1,500 tokens 前 CORRECT_REJECT cases / 8
near_invalid_rate = INVALID_PROMOTION cases / 8
```

对 `GLUE_TRAP`：

```text
glue_invalid_rate = INVALID_PROMOTION cases / 8
local_honesty_rate = CORRECT_LOCAL_ONLY or CORRECT_REJECT cases / 8
```

验证成本：

```text
verification_cost = reviewer_minutes
                  + 0.25 * deterministic_tool_minutes
                  + 2 * number_of_full_rechecks
```

这是内部比较单位，不是货币。token/API/GPU 另按 budget event 报告，不混入该分值。

## 8. 30% 的唯一解释

先把两个 baseline 中表现更强者定义为 `B*`：

- 对 acceptance，取率更高者；
- 对 invalid/cost，取值更低者；
- 若不同 baseline 分别胜出，不拼接虚构臂，各指标独立比较。

```text
acceptance_gain = (T_accept - B*_accept) / max(B*_accept, 1/8)
invalid_reduction = (B*_invalid - T_invalid) / max(B*_invalid, 1/8)
cost_reduction = (B*_cost - T_cost) / B*_cost
```

“≥30%”是上述相对变化，不是 30 个百分点。由于只有 8 case，本轮是工程 pilot：
报告精确 bootstrap 95% 区间，但 gate 使用预注册 point estimate；不得把它写成统计
显著的论文结论。

## 9. AC5 通过条件

以下全部满足：

1. T 的 `POSITIVE` 至少 6/8 `CORRECT_ACCEPT`；
2. T 的 `NEAR_MISS` 至少 7/8 early reject，且 0/8 invalid promotion；
3. T 的 `GLUE_TRAP` 0/8 invalid promotion；
4. 五张方法卡中至少三张在正例产生过正确完成；
5. 辅助强化没有增加原题假设；需要增假设时明确拒绝或走 amend；
6. 与 B* 相比，至少一个主指标满足：acceptance gain ≥30%，或 invalid reduction
   ≥30%，或 verification cost reduction ≥30%；
7. 第 6 条不得以牺牲第 1–3 条安全门获得；
8. 原始 outputs、review、成本、随机化表和分析脚本全部入 CAS。

若两个 baseline 已满分且零 invalid，30% 无法达成，结论是 `CEILING_INCONCLUSIVE`，
不是擅自宣布通过；下一版本需增加更难的预注册 fixture。

## 10. 防污染

- 方法卡作者不担任独立 reviewer；
- reviewer 不看卡文本，只看 agent 产物与题目；
- 评分结束前不计算 arm 汇总；
- 任何 fixture 泄漏到模型训练/上下文，整套实验标 `CONTAMINATED`；
- 人工标签只进 `HUMAN_SOFT_LABELS`，不进入“硬验证轨迹”训练池。
