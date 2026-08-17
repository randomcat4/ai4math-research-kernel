"""Human-facing summaries and safe HTML rendering for RK dossiers."""
# ruff: noqa: RUF001 -- Chinese user-facing prose intentionally uses Chinese punctuation.

from __future__ import annotations

import hashlib
import html
import re
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

_STATUS_ZH = {
    "OPEN": "题目已建立，尚未开始研究",
    "RUNNING": "研究进行中",
    "PAUSED": "研究已暂停",
    "CLOSED": "研究已结案",
    "CONTRACT_DEFECTIVE": "题目合同存在缺陷",
    "WAITING_HUMAN_REVIEW": "等待数学家审查",
    "COMPLETED": "本轮自动研究已完成",
}

_OUTCOME_ZH = {
    "PROVED": "已证明",
    "DISPROVED": "已否证",
    "ROUTE_LOCAL": "仅路线局部成立",
    "PREVIOUSLY_KNOWN": "文献中已有",
    "CONTRACT_DEFECTIVE": "题目合同有缺陷",
    "UNRESOLVED": "未解决",
}

_ROUTE_ZH = {
    "PROPOSED": "待尝试",
    "ACTIVE": "进行中",
    "BLOCKED": "受阻",
    "RETIRED": "已停止",
    "PROVED": "路线完成",
    "REFUTED": "路线被否证",
    "SCOUTED": "已提出，等待快速证伪",
    "FALSIFYING": "正在快速证伪",
    "DEVELOPING": "正在展开证明或反例",
    "REVIEWING": "正在找漏洞",
    "REVISING": "正在针对缺口修订",
    "FORMALIZING": "正在形式化",
    "SEMANTIC_AUDIT": "正在核对题意",
    "READY": "候选路线已备齐，等待闭合",
    "FAILED": "路线未完成",
}

_ROLE_ZH = {
    "CONTRACT_CLARIFIER": "题意澄清者",
    "ROUTE_SCOUT": "路线侦察者",
    "PROOF_COUNTEREXAMPLE": "证明与反例工作者",
    "LEAN_FORMALIZER": "Lean 形式化者",
    "ANONYMOUS_GAP_REVIEWER": "匿名找漏洞者",
    "TARGETED_REVISER": "定向修订者",
    "SEMANTIC_FIDELITY_AUDITOR": "语义忠实审计者",
    "LITERATURE_NOVELTY_AUDITOR": "文献与新颖性审计者",
    "FINAL_SYNTHESIZER": "最终综合者",
}

_WORK_STATUS_ZH = {
    "QUEUED": "等待开始",
    "RUNNING": "正在工作",
    "COMPLETED": "本轮完成",
    "SUCCEEDED": "完成",
    "FAILED": "未完成",
    "TIMEOUT": "超时",
    "ENVIRONMENT_FAILURE": "运行环境不可用",
    "ADAPTER_SCHEMA_MISMATCH": "组件返回格式不正确",
}

_OPERATION_ZH = {
    "submit_run": "题目已保存，并已交给研究编排器",
    "start": "研究已开始",
    "continue": "新一轮研究已启动",
    "pause": "研究已安全暂停",
    "resume": "研究已恢复",
    "review": "审查材料已导入",
}


def snapshot_summary(value: dict[str, Any]) -> str:
    """Render a bounded mathematician-facing status card without audit internals."""

    status = str(value.get("workflow_status") or value.get("status", "UNKNOWN"))
    raw_outcome = value.get("final_outcome")
    outcome = _OUTCOME_ZH.get(str(raw_outcome), str(raw_outcome)) if raw_outcome else "尚无最终结论"
    claims = value.get("claims", [])
    obligations = value.get("open_obligation_ids", [])
    run_id = str(value.get("run_id", "未知"))
    lines = [
        f"研究编号：{run_id}",
        f"当前状态：{_STATUS_ZH.get(status, status)}",
        f"最终结论：{outcome}",
        f"命题数量：{len(claims) if isinstance(claims, list) else '未知'}",
        f"未解决义务：{len(obligations) if isinstance(obligations, list) else '未知'}",
    ]
    lines.extend(_role_lines(value))
    lines.extend(_route_lines(value))
    lines.extend(_component_lines(value))
    lines.extend(_failure_lines(value))
    lines.extend(["", f"下一步：{_next_step(status, run_id)}"])
    return "\n".join(lines)


def workflow_summary(operation: str, value: Any, *, run_id: str) -> str:
    """Summarize an orchestrator response without exposing its internal envelope."""

    result = _plain_result(value)
    lines = [_OPERATION_ZH.get(operation, "研究动作已完成"), f"研究编号：{run_id}"]
    message = _first_text(result, "message_zh", "summary_zh", "message", "summary")
    if message:
        lines.append(f"进展：{message}")
    stage = _first_text(result, "stage_zh", "current_stage_zh", "stage")
    if stage:
        lines.append(f"当前阶段：{stage}")
    lines.extend(_role_lines(result))
    lines.extend(_route_lines(result))
    lines.extend(_component_lines(result))
    lines.extend(_failure_lines(result))
    status = str(result.get("status", "RUNNING"))
    lines.extend(["", f"下一步：{_next_step(status, run_id)}"])
    return "\n".join(lines)


def merge_workflow_snapshot(
    kernel_snapshot: Mapping[str, Any], workflow: Any
) -> dict[str, Any]:
    """Overlay human-facing workflow progress without changing kernel authority fields."""

    result = dict(kernel_snapshot)
    progress = _plain_result(workflow)
    if not progress:
        return result
    result["workflow_status"] = progress.get("status")
    result["stage_zh"] = progress.get("stage_zh")
    result["message_zh"] = progress.get("message_zh")
    result["roles"] = progress.get("roles", [])
    workflow_routes = progress.get("routes")
    if isinstance(workflow_routes, Sequence) and not isinstance(workflow_routes, str | bytes):
        result["routes"] = list(workflow_routes)
    result["component_usage"] = _merge_usage(
        result.get("component_usage"), progress.get("component_usage")
    )
    result["human_reviews"] = progress.get("human_reviews", [])
    result["failures"] = list(_records(result, "failures")) + _workflow_failures(progress)
    return result


def workflow_report_appendix(workflow: Any) -> str:
    """Create a report section from an orchestration checkpoint's public projection."""

    value = _plain_result(workflow)
    if not value:
        return ""
    lines = ["", "## 自动研究进展", ""]
    stage = _first_text(value, "stage_zh")
    message = _first_text(value, "message_zh")
    if stage:
        lines.append(f"- 当前阶段：{stage}")
    if message:
        lines.append(f"- 情况说明：{message}")
    lines.extend(_role_lines(value))
    lines.extend(_route_lines(value))
    lines.extend(_component_lines(value))
    reviews = _records(value, "human_reviews")
    lines.extend(["", "人类审查："])
    if reviews:
        for item in reviews:
            verdict = {
                "ACCEPTED": "通过",
                "CHANGES_REQUESTED": "需要修订",
                "REJECTED": "不通过",
            }.get(str(item.get("verdict")), "未给出结论")
            lines.append(f"- {verdict}；材料已保存，但不因导入动作自动成为证明。")
    else:
        lines.append("- 尚未导入。")
    return "\n".join(lines) + "\n"


def event_page_summary(value: dict[str, Any]) -> str:
    """Render an event page without dumping payloads or pretending it is a snapshot."""

    events = value.get("events", [])
    if not isinstance(events, list):
        events = []
    lines = [f"研究编号：{value.get('run_id', '未知')}", f"最近进展：{len(events)} 条"]
    for event in events:
        if isinstance(event, dict):
            event_type = event.get("event_type", event.get("type", "事件"))
            lines.append(f"- {_event_label(str(event_type))}")
    lines.extend(
        [
            f"是否还有更多：{'是' if value.get('has_more') else '否'}",
        ]
    )
    return "\n".join(lines)


def result_summary(operation: str, value: dict[str, Any]) -> str:
    if operation == "create":
        return "\n".join(
            [
                "题目已登记，但尚未获得任何数学结论。",
                f"研究编号：{value.get('run_id')}",
                f"下一步：rkctl 开始 {value.get('run_id')}",
            ]
        )
    if operation == "apply":
        accepted = bool(value.get("accepted"))
        if accepted:
            return "\n".join(
                [
                    "操作已记录。",
                    f"研究编号：{value.get('run_id')}",
                    "这只表示状态变更成功，不代表命题已经证明。",
                ]
            )
        return "\n".join(
            [
                "操作未被接受。",
                f"原因代码：{value.get('rejection_code')}",
                "请根据原因修正后重试；如无法判断，请联系管理员。",
            ]
        )
    return "操作完成。"


def _plain_result(value: Any) -> dict[str, Any]:
    if isinstance(value, Mapping):
        return {str(key): item for key, item in value.items()}
    to_dict = getattr(value, "to_dict", None)
    if callable(to_dict):
        result = to_dict()
        if isinstance(result, Mapping):
            return {str(key): item for key, item in result.items()}
    return {}


def _first_text(value: Mapping[str, Any], *keys: str) -> str | None:
    for key in keys:
        item = value.get(key)
        if isinstance(item, str) and item.strip():
            return item.strip()[:600]
    return None


def _records(value: Mapping[str, Any], *keys: str) -> list[Mapping[str, Any]]:
    for key in keys:
        items = value.get(key)
        if isinstance(items, Sequence) and not isinstance(items, str | bytes):
            return [item for item in items if isinstance(item, Mapping)]
    return []


def _role_lines(value: Mapping[str, Any]) -> list[str]:
    roles = _records(value, "roles", "role_runs", "role_assignments")
    if not roles:
        return ["", "角色进展：尚无已登记的角色活动"]
    lines = ["", "角色进展："]
    for item in roles[:8]:
        raw_name = _first_text(item, "title_zh", "role_name_zh", "role", "role_id")
        name = _ROLE_ZH.get(str(raw_name), str(raw_name or "数学角色"))
        raw_state = _first_text(item, "status_zh", "state_zh", "status", "state") or "已登记"
        state = _WORK_STATUS_ZH.get(raw_state, raw_state)
        note = _first_text(item, "summary_zh", "result_zh", "failure_zh")
        lines.append(f"- {name}：{state}" + (f"；{note}" if note else ""))
    if len(roles) > 8:
        lines.append(f"- 另有 {len(roles) - 8} 项角色活动")
    return lines


def _route_lines(value: Mapping[str, Any]) -> list[str]:
    routes = _records(value, "routes", "route_progress")
    if not routes:
        return ["", "路线进展：尚未登记研究路线"]
    lines = ["", "路线进展："]
    for index, item in enumerate(routes[:8], 1):
        label = _first_text(item, "label", "title_zh", "name") or f"路线 {index}"
        raw = _first_text(item, "status", "state", "result") or "PROPOSED"
        state = _ROUTE_ZH.get(raw, raw)
        replay = _first_text(item, "lean_replay_status")
        replay_zh = {
            "COMPLETED": "Lean 重放通过",
            "POLICY_VIOLATION": "Lean 重放拒绝",
            "LEAN_FEEDBACK": "Lean 编译未通过",
            "RUNTIME_EXCEPTION": "Lean 未能启动",
            "MISSING_FORMAL_CANDIDATE": "未产出 Lean 候选",
            "MISSING_FORMAL_DECLARATION": "Lean 候选没有命名声明",
            "NOT_CONFIGURED": "未配置 Lean 重放",
        }.get(str(replay), f"Lean 状态 {replay}" if replay else "Lean 尚未重放")
        lines.append(f"- {label}：{state}；{replay_zh}")
    if len(routes) > 8:
        lines.append(f"- 另有 {len(routes) - 8} 条路线")
    return lines


def _component_lines(value: Mapping[str, Any]) -> list[str]:
    usage = value.get("component_usage", {})
    if not isinstance(usage, Mapping) or not usage:
        return ["", "组件用量：尚无可信计量"]
    lines = ["", "组件用量："]
    for name, raw in list(sorted(usage.items(), key=lambda pair: str(pair[0])))[:12]:
        item = raw if isinstance(raw, Mapping) else {}
        total = _nonnegative_int(item.get("total_tokens"))
        if total == 0:
            total = sum(
                _nonnegative_int(item.get(key))
                for key in ("input_tokens", "output_tokens", "reasoning_tokens")
            )
        wall = _duration(_nonnegative_int(item.get("wall_time_ms")))
        unknown = _nonnegative_int(item.get("unknown_count"))
        suffix = f"；{unknown} 次用量未知" if unknown else ""
        lines.append(f"- {name}：{total:,} token，{wall}{suffix}")
    return lines


def _failure_lines(value: Mapping[str, Any]) -> list[str]:
    messages: list[str] = []
    for item in _records(value, "failures", "failure_records"):
        message = _first_text(item, "message_zh", "summary_zh", "failure_kind", "reason")
        if message:
            messages.append(_failure_label(message))
    for item in _records(value, "host_execution_receipts", "executions"):
        status = str(item.get("status", ""))
        reasons = item.get("block_reasons")
        if isinstance(reasons, Sequence) and not isinstance(reasons, str | bytes):
            messages.extend(_failure_label(str(reason)) for reason in reasons if reason)
        elif status and status not in {"COMPLETED", "SUCCEEDED", "SUCCESS"}:
            messages.append(f"组件执行未成功（{status}）")
    if value.get("budget_fuse_tripped"):
        messages.append("预算保护已触发，系统停止继续消耗")
    if not messages:
        return ["", "失败与阻塞：无已记录问题"]
    lines = ["", "失败与阻塞："]
    lines.extend(f"- {message[:300]}" for message in messages[:8])
    return lines


def _nonnegative_int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _duration(milliseconds: int) -> str:
    if milliseconds < 1_000:
        return f"{milliseconds} 毫秒"
    seconds = milliseconds / 1_000
    if seconds < 60:
        return f"{seconds:.1f} 秒"
    minutes, remainder = divmod(round(seconds), 60)
    if minutes < 60:
        return f"{minutes} 分 {remainder} 秒"
    hours, minutes = divmod(minutes, 60)
    return f"{hours} 小时 {minutes} 分"


def _next_step(status: str, run_id: str) -> str:
    if status == "OPEN":
        return f"rkctl 开始 {run_id}"
    if status == "PAUSED":
        return f"rkctl 恢复 {run_id}"
    if status == "CLOSED":
        return f"rkctl 导出报告 {run_id} --格式 网页 --输出 研究报告.html"
    if status == "COMPLETED":
        return f"rkctl 导出报告 {run_id} --格式 网页 --输出 研究报告.html"
    if status == "WAITING_HUMAN_REVIEW":
        return f"rkctl 审查 {run_id} 审查意见.md --结论 通过"
    if status == "CONTRACT_DEFECTIVE":
        return "核对题面、对象、量词和精确否定，再提交修订后的题目"
    return f"rkctl 继续 {run_id}（需要暂离时可运行：rkctl 暂停 {run_id}）"


def _event_label(event_type: str) -> str:
    labels = {
        "RUN_CREATED": "题目已建立",
        "RUN_STARTED": "研究已开始",
        "RUN_PAUSED": "研究已暂停",
        "RUN_RESUMED": "研究已恢复",
        "RUN_CLOSED": "研究已结案",
        "ROUTE_ADDED": "新增研究路线",
        "EVIDENCE_SUBMITTED": "新增证据材料",
        "AUTHORITY_REVALIDATED": "历史权威状态已重新核对",
    }
    return labels.get(event_type, event_type.replace("_", " ").lower())


def _failure_label(value: str) -> str:
    labels = {
        "EXECUTION_NOT_SUCCESSFUL": "组件执行未成功，结果没有进入数学结论",
        "DEPENDENCY_VALIDATION_FAILED": "形式化依赖环境校验失败",
        "ARTIFACT_VALIDATION_FAILED": "输出材料完整性校验失败",
        "AUTHORITY_RESULT_INVALID": "结果不满足权威使用条件",
        "HOST_CAPABILITY_INVALID_AFTER_CALL": "执行结束时宿主权限已失效",
        "BUDGET_OVERRUN": "本次调用超过预留预算，已停止继续消耗",
        "ENVIRONMENT_ERROR": "运行环境不可用",
        "REPLAY_FAILED": "形式化重放失败",
    }
    return labels.get(value, value)


def _merge_usage(left: Any, right: Any) -> dict[str, dict[str, int]]:
    merged: dict[str, dict[str, int]] = {}
    for raw in (left, right):
        if not isinstance(raw, Mapping):
            continue
        for component, usage in raw.items():
            if not isinstance(usage, Mapping):
                continue
            target = merged.setdefault(str(component), {})
            for key in (
                "input_tokens",
                "output_tokens",
                "reasoning_tokens",
                "cache_read_tokens",
                "cache_write_tokens",
                "total_tokens",
                "wall_time_ms",
                "unknown_count",
            ):
                target[key] = target.get(key, 0) + _nonnegative_int(usage.get(key))
    return merged


def _workflow_failures(value: Mapping[str, Any]) -> list[dict[str, str]]:
    failures: list[dict[str, str]] = []
    for event in _records(value, "events"):
        event_type = str(event.get("event_type", ""))
        payload = event.get("payload")
        payload = payload if isinstance(payload, Mapping) else {}
        if event_type == "COMPONENT_COMPLETED":
            status = str(payload.get("status", ""))
            if status not in {"COMPLETED", "SUCCEEDED", "SUCCESS"}:
                role = str(payload.get("role") or "研究组件")
                name = _ROLE_ZH.get(role, role)
                reason = _WORK_STATUS_ZH.get(status, status or "未说明原因")
                failures.append({"message_zh": f"{name}未完成：{reason}"})
        elif event_type == "ORCHESTRATION_PAUSED":
            reason = str(payload.get("reason", ""))
            if reason and reason != "USER_REQUEST":
                labels = {
                    "CONTRACT_AMBIGUOUS": "题意存在会影响真值的歧义",
                    "BUDGET_EXHAUSTED": "本轮预算已用尽",
                    "RUNTIME_UNAVAILABLE": "研究组件暂时不可用",
                    "HUMAN_REVIEW_REQUIRED": "需要数学家审查",
                }
                failures.append({"message_zh": labels.get(reason, reason)})
    return failures


def read_cas_artifact(cas_root: Path, sha256: str, byte_count: int) -> bytes:
    """Read an export just returned by the kernel and recheck its content address."""

    if not re.fullmatch(r"[0-9a-f]{64}", sha256):
        raise ValueError("报告摘要值无效，拒绝读取")
    path = (cas_root / sha256[:2] / sha256[2:4] / sha256).resolve()
    root = cas_root.resolve()
    if not path.is_relative_to(root) or not path.is_file() or path.is_symlink():
        raise OSError("报告工件不存在或不是普通文件")
    data = path.read_bytes()
    if len(data) != byte_count or hashlib.sha256(data).hexdigest() != sha256:
        raise OSError("报告工件校验失败；文件可能损坏")
    return data


def markdown_to_mathjax_html(markdown: str, *, title: str = "数学研究卷宗") -> str:
    """Create safe readable HTML whose TeX is rendered by MathJax in the browser.

    Markdown is escaped before structural conversion, so a theorem statement cannot inject
    arbitrary HTML.  TeX delimiters remain text for MathJax to process after page load.
    """

    body: list[str] = []
    in_list = False
    paragraph: list[str] = []

    def flush_paragraph() -> None:
        if paragraph:
            body.append(f"<p>{_inline(' '.join(paragraph))}</p>")
            paragraph.clear()

    for raw_line in markdown.splitlines():
        line = raw_line.rstrip()
        if line.startswith("#"):
            flush_paragraph()
            if in_list:
                body.append("</ul>")
                in_list = False
            level = min(len(line) - len(line.lstrip("#")), 3)
            text = line[level:].strip()
            body.append(f"<h{level}>{_inline(text)}</h{level}>")
        elif line.startswith("- "):
            flush_paragraph()
            if not in_list:
                body.append("<ul>")
                in_list = True
            body.append(f"<li>{_inline(line[2:])}</li>")
        elif not line:
            flush_paragraph()
            if in_list:
                body.append("</ul>")
                in_list = False
        else:
            if in_list:
                body.append("</ul>")
                in_list = False
            paragraph.append(line)
    flush_paragraph()
    if in_list:
        body.append("</ul>")
    return """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>{title}</title>
<script>window.MathJax={{tex:{{inlineMath:[['$','$'],['\\\\(','\\\\)']],
displayMath:[['$$','$$'],['\\\\[','\\\\]']]}}}};</script>
<script defer src="https://cdn.jsdelivr.net/npm/mathjax@3/es5/tex-chtml.js"></script>
<style>
body{{font:17px/1.75 system-ui,-apple-system,"Noto Sans CJK SC","Microsoft YaHei",sans-serif;
max-width:920px;margin:2.5rem auto;padding:0 1.5rem;color:#202124;background:#fff}}
h1,h2,h3{{line-height:1.3;margin-top:1.8em}} code{{background:#f3f4f6;padding:.12em .35em;
border-radius:4px}} li{{margin:.4em 0}} .render-note{{color:#62676f;font-size:.9em;
border-top:1px solid #ddd;margin-top:3rem;padding-top:1rem}}
</style></head><body>
{body}
<p class="render-note">公式由 MathJax 3 在浏览器中排版；若网络受限，TeX 原文仍完整保留。</p>
</body></html>
""".format(title=html.escape(title), body="\n".join(body))


def _inline(text: str) -> str:
    escaped = html.escape(text, quote=False)
    escaped = re.sub(r"\*\*([^*]+)\*\*", r"<strong>\1</strong>", escaped)
    return re.sub(r"`([^`]+)`", r"<code>\1</code>", escaped)
