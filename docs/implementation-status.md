# Implementation status

更新时间：2026-08-12。本文只描述可执行实现与实测，不声明完整 RK-PRD-2 已完成。

## 已连通并实测

- 对外 `ResearchKernel.create/apply/inspect/export`、SQLite/CAS、幂等收据、能力校验、
  27 个 v1 命令 guard 和投影已执行。
- 远端 `leane2efinal9` 用当前工作树真实调用：公共 LeanSearch、OpenCode 1.18.16 +
  DeepSeek V4-Pro 无工具生成、jixia 4.28、Lean 4.28.0-rc1 + Mathlib 固定 commit 的重放。
- Lean 硬结果必须绑定成功 attempt、execution binding、源码/输出/二进制哈希和宿主
  HMAC execution receipt；仅持 verifier writer capability 不能自报 `REPLAY_PASS`。
- 预算 hard limit 和 route attempt limit 已从“只记账”变成 guard 执行；组件 token、
  wall time、UNKNOWN_COST 与 API 费用未知均进入账本。
- Z3 4.15.3、SymPy 1.14、精确枚举、固定代码执行已下载到远端并真实 smoke；这些
  工具输出默认不改变数学轴，除非另有可信证书重放。
- Crossref 文献检索 adapter 已实现并真实查询；空结果明确不是证明。人类同行审查通过
  `RecordPeerReview` 命令接入，真实人类签名尚未发生。
- QED-Nano 4B 和 DeepSeek-Prover-V2-7B 已按上游默认/官方参数下载到远端 AMD GPU
  并完成五题 smoke；专用模型统一 adapter 永远标为 `SOFT_CANDIDATE_ONLY`。DeepSeek
  候选另过 Lean 内核，其中 4/5 通过，1/5 因撞 8192-token 上限且含 `sorry` 被拒。

主证据：`docs/rkleane2e.json`、`docs/rktoolsmoke.json`、`docs/rkcomponents.md`。

## 已修复的独立审计问题

- 旧 OpenCode 成功记录含 bash 调用，已作废。当前 adapter 用全局和 build agent 双层
  deny-all permission/tool registry、非 root 用户和每次全新 XDG 目录；实测无工具调用。
- OpenCode 1.18.16 偶发已经发出完整 `step_finish` 却不退出；JSONL runner 现在识别协议
  的 `reason=stop` 完成、留清理宽限期，再终止残留进程组；`tool-calls` 中间 step 不会
  被误当终态。
- timeout/异常现在归一化，失败日志先入 CAS，并在 `finally` 终结 lease/attempt。
- Lean/jixia 拒绝已有输出，避免复用陈旧 `.olean`/JSON。
- OpenCode 空文本或无结束事件不再标成功；Lean 禁用词修复标点绕过。
- jixia 抽取的声明名实际决定后续 Lean replay 输入，不再只是旁路 telemetry。
- Mathlib HEAD、tracked dirty state、toolchain、二进制与依赖输入摘要进入实测结果。
- replay receipt 使用远端 `0600 root:root` 的持久 HMAC 密钥，重启后仍可复核；密钥本身
  不进入 SQLite、CAS 或导出结果。
- 回执绑定内核预发的一次性 nonce、run、attempt、binding、profile、commit 和 adapter
  version；重复消费或跨 attempt/run 重放由 guard 拒绝。
- 每次外部调用前做 hard-budget reservation，返回后原始结果先入 CAS，再退款和记录
  actual/UNKNOWN_COST；预算结算异常不再抹掉已经发生的调用结果。

## 明确限制

- 本次隔离不是 `ISOLATED_KERNEL_REPLAY`：Mathlib `.lake` cache 仍共享可写，Lean/jixia
  运行网络与只读挂载未由 OS 探针强制，结果明确标为 `UNENFORCED`。
- 公共 LeanSearch 响应没有服务端 commit/模型 hash attestation；本地仓库 commit 只是
  客户端来源，不能冒充线上部署版本。远端双 8B 权重已完成 ROCm smoke，但本次公共
  endpoint 没有使用它们的可验证证据。
- Archon adapter/source contract 有测试，完整真实 Horizon 模型 run 尚未通过；Rethlas
  health 曾通过，但 Codex 0.80 + DeepSeek full loop 仍失败。因此二者不是 E2E verified。
- GPT-5.6 Pro、Codex 5.6 是闭源服务角色，不存在可下载的本地权重；当前 RK 未配置
  它们的 provider adapter。QED-Nano/DeepSeek-Prover 已下载、基准运行并接入统一
  soft-only adapter，但尚未成为主 E2E 的默认路由，也未复现论文完整 benchmark/RSA。
- `AmendContract` 仍为 `TEMPORARILY_UNAVAILABLE`；旧 revision 的历史事件重放导出、
  raw artifact 嵌入仍未实现。
