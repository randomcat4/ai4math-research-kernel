# RK 真实数理用户模拟与风险审计

审计日期：2026-08-12（America/New_York）

## 当前结论

远端工具链确实跑过，但 v0.1 的成功日志不能证明 v0.2 的权威状态。独立审计发现旧实现
允许换题、跨 claim 复用证据、自报 replay/人类独立性/质量/预算、伪造局部路线与终态。
当前工作树先采取保守基线：保存研究工件与软候选，但所有缺少宿主回执的权威晋级均
fail closed，完整 `PROVED` 暂不可达；用户仍可诚实导出 `UNRESOLVED` 卷宗。

历史 `leane2efinal9` 只证明公共 LeanSearch、OpenCode/DeepSeek、jixia、Lean/Mathlib
曾经完成一次集成调用。迁移 0004 会撤销其旧 ROOT、machine 和 final outcome 权威。

## 模拟数学家旅程

一位研究有限域组合问题的数学家给出中文/LaTeX 题面、精确否定、自然语言草稿和 Lean
候选。合理旅程应当是：冻结题目，系统生成唯一规范 ROOT；模型提出路线；检索与 jixia
提供软反馈；独立 Lean 宿主验证精确 ROOT scope；匿名语义审查检查量词和对象；最后用
中文卷宗解释结论与开放义务。

v0.1 CLI 尚不能完成这条旅程：它要求用户理解 capability、UUID、CAS、revision 和完整
机器 JSON；默认查看曾输出约 19KB 内部结构；导出只返回 artifact id；中文 Markdown
曾遗漏题面并错读六轴。当前代码已修正 dossier 的题面/outcome/六轴映射，但面向数学家
的首程、凭据签发、工件取回和自然语言错误建议仍待单独实现。

## 已关闭的高危路径

- 新 claim 的规范 JSON 字节哈希必须等于 statement hash；ROOT 还必须逐字等于冻结合同
  的规范工件和 hash，且每次只有一个 ACTIVE ROOT。
- `Finalize(PROVED/DISPROVED/PREVIOUSLY_KNOWN)` 的 terminal 必须是唯一 canonical ROOT；
  不能用 SIDE_FINDING 或 lemma 代替整个问题。
- PromoteClaim 对证据做 run/claim/contract/hash 精确 scope；QUALITY 和未受管人类路径关闭。
- 自报 Lean replay、composition provenance、反例、合同缺陷、局部路线、文献等价性不能
  生成权威终态。
- v0.1 的旧 ROOT、机器/人类/质量/闭包状态和权威终态由迁移 0004 撤销，历史工件保留。
- Finalize 与 export 使用同一公开投影；中文卷宗包含题面、精确否定、claim 内容、六轴、
  outcome 和开放义务。

## 仍开放的 P0/P1 工作

1. 建立真正的宿主执行回执服务；密钥与 scope 装配不能暴露给 candidate/verifier 脚本。
2. 将 token、计时和 UNKNOWN_COST 绑定同一执行回执，不能信任模型或普通 capability 自报。
3. 设计受管语义/同行/质量审查身份与盲审来源图；在此前只存软注释。
4. 重做数学家 CLI 首程与 LaTeX 展示、报告取得和中文可行动错误。
5. 在远端完成当前策略五次真跑，再实施 Lean/jixia/LeanSearch 的有证明等价的无损加速。

## 历史性能证据

QED-Nano 与 DeepSeek-Prover 的五题 smoke、公共 LeanSearch 和 Lean/jixia 时延仍可用于性能
设计，不能作为数学真值证据。最新真实性与限制以 `docs/implementation-status.md` 为准。
