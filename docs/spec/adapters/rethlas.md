# Rethlas adapter v1

信任上限：生成端 `CANDIDATE_ARTIFACT`；验证端永远 `SOFT_MODEL`。

## 固定来源

```text
repo: /root/ai4math_repro_20260811/src/pku/Rethlas
origin: https://github.com/frenzymath/Rethlas.git
commit: 887cc46427636bbdd235160a112f9a30ae81d040
verification API version: 0.1.0
verification env: /root/ai4math_repro_20260811/env/rethlas
```

架构是两个 Codex agent：generation 写自然语言 proof blueprint，verification 再调用
Codex 做自然语言检查。仓库本身在此路径**不调用 Lean 或 jixia**。

## 启动 verifier

固定工作目录 `agents/verification`：

```text
/root/ai4math_repro_20260811/env/rethlas/bin/python -m uvicorn \
  api.server:app --host 127.0.0.1 --port <leased-port>
```

环境白名单可含 `CODEX_BIN, CODEX_MODEL, CODEX_REASONING_EFFORT,
CODEX_TIMEOUT_SECONDS` 与 provider 所需的单个 secret ref；不得把 secret 值写入
invocation artifact。

健康检查：`GET /health -> {"status":"ok"}`。

## verify API

```text
POST http://127.0.0.1:<port>/verify
Content-Type: application/json

{"statement":"nonempty string","proof":"nonempty markdown"}
```

服务内部 run ID 为 `UTCtimestamp_statementHash12`，但当前响应不返回 run ID。成功
响应由 agent 产出的文件决定，adapter 必须严格校验：

```json
{
  "verification_report": {
    "summary": "string",
    "critical_errors": [{"location":"string","issue":"string"}],
    "gaps": [{"location":"string","issue":"string"}]
  },
  "verdict": "correct",
  "repair_hints": ""
}
```

`correct` 要求 errors/gaps 均空；`wrong` 要求 repair_hints 非空。任何额外/缺失字段、
HTML、超时或 5xx 都是 adapter failure。

无论 verdict 为何，EvidenceIngest 固定：

```text
evidence_type = MODEL_JUDGE
evidence_strength = SOFT_MODEL
machine_verdict = unchanged
```

不能因第二个 agent、结构化 JSON 或“strict verdict rule”提升为同行或机械证据。

## generation runner

仓库公开 runner：

```text
cd agents/generation
PROBLEM_FILE=data/<path>.md MAX_ITERATIONS=<N> ./tests/run_example.sh
```

它执行 `codex exec` 并恢复同一 session，交替 search-disabled/search-enabled，直到
`results/<problem>/blueprint_verified.md` 或耗尽 N。v1 不直接信任该 shell script；
StrategyRunner 复刻其状态机，用 argv 启动 Codex，并把每轮 draft、memory、log、
verifier JSON 分别 ingest，避免脚本控制宿主环境。

## 状态与失败

| 条件 | 结果 |
|---|---|
| health 不通 | `ENVIRONMENT_ERROR` |
| verifier 504 | attempt PAUSED/FAILED，按预算决定新 attempt |
| verifier 500/非法 JSON | `ADAPTER_SCHEMA_MISMATCH` |
| verdict wrong | 产生 FailureRecord/repair candidate，不自动否定 claim |
| verdict correct | 产生 SOFT_MODEL evidence，可进入人工/Lean 后续队列 |
| blueprint_verified 文件出现 | 只表示 Rethlas 内部接受，不是 RK verified |

## AMD 已有实测边界

服务器上 health 已成功。Codex CLI 0.80 + DeepSeek v4-pro 兼容代理的两次单轮 smoke
均在约 8–9 秒退出 1，没有生成 verification JSON；receipt 明确记录
`lean_or_jixia_used=no`、secret scan clean。故当前状态是：

```text
VERIFIER_HEALTH_PASS
FULL_LOOP_WITH_CODEX080_DEEPSEEKV4P_FAIL
```

不得在 PRD 或 dossier 中写成“Rethlas 已跑通”。后续若改 runner/opencode，必须新建
adapter version 和 fixture，不能覆盖这次失败。
