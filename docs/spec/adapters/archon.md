# Archon-Horizon adapter v1

信任上限：`EXECUTION_ORCHESTRATOR`，不产生数学证据。

## 固定来源

```text
host: root@36.150.116.220:30412
repo: /root/ai4math_repro_20260811/src/pku/Archon-Horizon
origin: https://github.com/frenzymath/Archon-Horizon.git
commit: a4565a48b4b84189384a05b9a4e6409e875122e1
reported version: 0.1.2
python: /root/ai4math_repro_20260811/env/archon/bin/python (3.12.3)
```

该环境安装了 Archon 0.3.3，但没有安装 `horizon` console script。v1 必须从上述
源码 commit 启动，不能假设 PATH 有 `horizon`：

```text
PYTHONPATH=/root/ai4math_repro_20260811/src/pku/Archon-Horizon/src \
/root/ai4math_repro_20260811/env/archon/bin/python -m archon_horizon \
  --root <isolated-workspace> run <target> --no-dashboard --json
```

调用使用 argv 数组和环境白名单，不经 shell。`<isolated-workspace>` 必须是当前
attempt 的 work 目录，且 config/skills 由宿主准备、生成者只读。

## 输入

adapter 接收：

```json
{
  "target": ".",
  "rounds": 1,
  "resume_external_run_id": null,
  "backend": "default",
  "workspace_relpath": "runs/<rk>/<route>/<attempt>/work",
  "environment_profile_id": "archon-horizon-a4565a-v1"
}
```

禁止 `--backend interactive`、`--bare`、`--public`。dashboard 固定关闭。rounds 必须
受 ExpansionRequest 预算约束。

## 输出契约

stdout 必须是且只能是：

```json
{
  "dry_run": false,
  "rounds": [
    {
      "round": 0,
      "tasks_run": ["task-id"],
      "tasks_blocked": [],
      "tasks_unrunnable": []
    }
  ]
}
```

dry-run 时每项改为 `{"round":0,"planned":[...]}`。源码证据为
`src/archon_horizon/commands/run.py::_emit_reports`，JSON 纯净性由
`tests/test_cli_json.py::test_run_dry_run_json` 覆盖。fixture 见
`../fixtures/archon_run_dry.json`。

JSON 不包含 Archon run ID；adapter 只能从已固定 state layout 另读 durable run
record 后再绑定，读不到时 external_run_id 留 null，不得从 banner 猜。stderr banner
作为 execution log artifact，不参与 JSON 解析。

## 退出与恢复

| exit | 解释 | RK 行为 |
|---:|---|---|
| 0 | CLI 完成；仍须检查 blocked/unrunnable | 保存输出，不自动判 route success |
| 1 | 参数/目标/一般运行错误 | attempt FAILED 或 ENVIRONMENT_ERROR，按 stderr 分类 |
| 3 | usage/budget pause | 读取 `runs/<id>/paused.json`，attempt PAUSED，可 `--resume <id>` |
| 其他 | adapter 不认识 | ENVIRONMENT_ERROR，不自动重试 |

`paused.json` 固定字段：`run_id, reason, at, focus_tasks, focus_projects, resume`，可选
`retry_after_s`。resume 命令同样加 `--no-dashboard --json`。

## 契约测试

1. pinned source 的 `--help` 必须含 `--json`, `--resume`, `--no-dashboard`；
2. dry-run stdout 通过 fixture schema，stderr 可含 banner；
3. 错误 JSON、额外 stdout、未知字段进入 `ADAPTER_SCHEMA_MISMATCH`；
4. exit 3 + pause marker 可转换为可恢复 attempt；
5. kill 后不得把 partial report 作为成功；
6. adapter 只创建 execution binding/log evidence，不触碰 claim verdict。

## 已核事实与未核事实

- Archon 核心 commit `80f93b9d3d0c5d12c6e4b23f05849ec1cf29fa18` 的 659 tests
  在该环境通过；现存 smoke launcher 后续因重复创建 venv 返回 2，不是测试回归。
- Horizon 的 CLI help 与 JSON 源码契约已核；尚未把完整真实模型 Horizon run 当作
  v1 适配验收。
- 因此 adapter 状态为 `SOURCE_CONTRACT_VERIFIED`，不是 `END_TO_END_MODEL_VERIFIED`。
