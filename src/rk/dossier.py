"""Deterministic dossier projection from one persisted run revision."""
# ruff: noqa: RUF001 -- Chinese dossier prose intentionally uses Chinese punctuation.

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from rk.domain import RunSnapshot

_CLAIM_STATES = {
    "route": frozenset(
        {
            "UNASSESSED",
            "CANDIDATE",
            "LOCAL_LEMMAS_VERIFIED",
            "ROUTE_LOCAL",
            "ROUTE_PROVED",
            "REFUTED",
            "PREVIOUSLY_KNOWN",
        }
    ),
    "machine": frozenset(
        {"UNVERIFIED", "KERNEL_VERIFIED", "CERTIFICATE_VERIFIED", "REPLAY_FAILED"}
    ),
    "semantic": frozenset({"UNREVIEWED", "TESTED", "HUMAN_ATTESTED", "REFUTED"}),
    "peer": frozenset({"UNREVIEWED", "ACCEPTED", "REJECTED", "NEEDS_REVISION"}),
    "quality": frozenset({"UNREVIEWED", "ACCEPTED", "REJECTED", "NEEDS_REVISION"}),
    "closure": frozenset(
        {"NOT_REQUIRED", "OPEN", "CLOSED_MACHINE", "CLOSED_HUMAN", "CLOSED_HYBRID", "INVALIDATED"}
    ),
}

_LEGACY_CLAIM_STATE_KEYS = {
    "route": "route_result",
    "machine": "machine_verdict",
    "semantic": "semantic_verdict",
    "peer": "peer_verdict",
    "quality": "quality_verdict",
    "closure": "closure_state",
}

_AXIS_LABELS = {
    "route": "路线结论",
    "machine": "机器核验",
    "semantic": "题意一致性",
    "peer": "独立审查",
    "quality": "表述质量",
    "closure": "拼装闭合",
}

_EVIDENCE_STRENGTH = {
    "HARD_MACHINE": "机器候选（只有受信宿主回执才能产生数学权威）",
    "HUMAN_ATTESTED": "人工声明（当前未受管，不能晋级）",
    "SOFT_MODEL": "模型建议（仅供探索）",
    "PROVENANCE_ONLY": "来源记录（不判断真伪）",
}

_STATE_ZH = {
    "UNASSESSED": "未评估",
    "CANDIDATE": "候选路线",
    "LOCAL_LEMMAS_VERIFIED": "局部引理已核验",
    "ROUTE_LOCAL": "仅路线局部成立",
    "ROUTE_PROVED": "路线已完成",
    "REFUTED": "已否证",
    "PREVIOUSLY_KNOWN": "文献中已有",
    "UNVERIFIED": "未核验",
    "KERNEL_VERIFIED": "内核已核验",
    "CERTIFICATE_VERIFIED": "证书已核验",
    "REPLAY_FAILED": "重放失败",
    "UNREVIEWED": "未审查",
    "TESTED": "已作题意测试",
    "HUMAN_ATTESTED": "人工声明通过",
    "ACCEPTED": "审查意见接受",
    "REJECTED": "审查意见拒绝",
    "NEEDS_REVISION": "需要修订",
    "NOT_REQUIRED": "无需拼装",
    "OPEN": "尚未闭合",
    "CLOSED_MACHINE": "机器闭合",
    "CLOSED_HUMAN": "人工闭合",
    "CLOSED_HYBRID": "混合闭合",
    "INVALIDATED": "已撤销",
}

_OUTCOME_ZH = {
    "PROVED": "已证明",
    "DISPROVED": "已否证",
    "ROUTE_LOCAL": "仅路线局部成立",
    "PREVIOUSLY_KNOWN": "文献中已有",
    "CONTRACT_DEFECTIVE": "题目合同有缺陷",
    "UNRESOLVED": "未解决",
}

_RUN_STATUS_ZH = {
    "OPEN": "已建立",
    "RUNNING": "研究中",
    "PAUSED": "已暂停",
    "CLOSED": "已结案",
    "CONTRACT_DEFECTIVE": "题目合同有缺陷",
}


class DossierBuilder:
    def build(self, snapshot: RunSnapshot, dossier_spec: Mapping[str, Any]) -> tuple[bytes, str]:
        if bool(dossier_spec.get("include_raw_artifacts", False)):
            raise ValueError("raw artifact materialization is not implemented")
        language = str(dossier_spec.get("language", "zh-CN"))
        output_format = str(dossier_spec.get("format", "JSON"))
        projection = _canonical_projection(snapshot.projection)
        # Exported dossiers are derived outputs, not mathematical state.  Excluding those
        # self-references makes repeated export at one revision byte-for-byte deterministic.
        artifacts = projection.get("artifacts")
        if isinstance(artifacts, tuple | list):
            projection["artifacts"] = [
                item
                for item in artifacts
                if not isinstance(item, Mapping) or item.get("role") != "DOSSIER"
            ]
        payload = {
            "schema_version": "rk.dossier.v1",
            "run_id": snapshot.run_id,
            "at_revision": snapshot.revision,
            "status": snapshot.status,
            "final_outcome": projection.get("final_outcome"),
            "current_contract_version": snapshot.current_contract_version,
            "language": language,
            "include_raw_artifacts": bool(dossier_spec.get("include_raw_artifacts", False)),
            "projection": _stable(projection),
        }
        if output_format == "JSON":
            return (
                json.dumps(
                    payload,
                    ensure_ascii=False,
                    sort_keys=True,
                    separators=(",", ":"),
                ).encode("utf-8"),
                "application/json",
            )
        if output_format == "MARKDOWN":
            return _markdown(payload).encode("utf-8"), "text/markdown; charset=utf-8"
        raise ValueError(f"unsupported dossier format: {output_format}")


def _stable(value: Any) -> Any:
    if isinstance(value, Mapping):
        ordered = sorted(value.items(), key=lambda pair: str(pair[0]))
        return {str(key): _stable(item) for key, item in ordered}
    if isinstance(value, tuple | list):
        items = [_stable(item) for item in value]
        stable_keys = ("claim_id", "route_id", "obligation_id", "event_id")
        if all(
            isinstance(item, Mapping) and any(key in item for key in stable_keys) for item in items
        ):

            def stable_id(item: Mapping[str, Any]) -> str:
                for key in stable_keys:
                    if key in item:
                        return str(item[key])
                return ""

            return sorted(items, key=stable_id)
        return items
    return value


def _canonical_projection(value: Mapping[str, Any]) -> dict[str, Any]:
    projection = dict(value)
    claims = projection.get("claims", [])
    if not isinstance(claims, tuple | list):
        raise ValueError("dossier projection claims must be an array")
    projection["claims"] = [_canonical_claim(item) for item in claims]
    return projection


def _canonical_claim(value: Any) -> dict[str, Any]:
    if not isinstance(value, Mapping):
        raise ValueError("dossier claim must be an object")
    claim = dict(value)
    claim_id = claim.get("claim_id")
    if not isinstance(claim_id, str) or not claim_id:
        raise ValueError("dossier claim_id must be a non-empty string")
    for canonical, allowed in _CLAIM_STATES.items():
        legacy = _LEGACY_CLAIM_STATE_KEYS[canonical]
        state = claim.get(canonical)
        if state is None:
            raise ValueError(f"dossier claim {claim_id} is missing canonical state {canonical}")
        if legacy in claim and claim[legacy] != state:
            raise ValueError(
                f"dossier claim {claim_id} has conflicting {canonical} and {legacy} states"
            )
        if state not in allowed:
            raise ValueError(f"dossier claim {claim_id} has invalid {canonical} state: {state}")
        claim[canonical] = state
    return claim


def _markdown(payload: Mapping[str, Any]) -> str:
    projection = payload["projection"]
    if not isinstance(projection, Mapping):
        projection = {}
    claims = projection.get("claims", [])
    obligations = projection.get("open_obligation_ids", [])
    evidence = projection.get("evidence", [])
    peer_reviews = projection.get("peer_reviews", [])
    quality_reviews = projection.get("quality_reviews", [])
    routes = projection.get("routes", [])
    roles = projection.get("roles", projection.get("role_runs", []))
    component_usage = projection.get("component_usage", {})
    failures = projection.get("failures", [])
    host_receipts = projection.get("host_execution_receipts", [])
    research_hints = projection.get("research_hints", [])
    atomic_verifications = projection.get("atomic_verifications", [])
    contract = projection.get("contract", {})
    contract_body = contract.get("contract", {}) if isinstance(contract, Mapping) else {}
    statement = (
        contract_body.get("statement", "[题面缺失]")
        if isinstance(contract_body, Mapping)
        else "[题面缺失]"
    )
    exact_negation = (
        contract_body.get("exact_negation", "[未提供]")
        if isinstance(contract_body, Mapping)
        else "[未提供]"
    )
    lines = [
        "# 数学研究报告",
        "",
        f"- 研究编号：{payload['run_id']}",
        f"- 当前状态：{_RUN_STATUS_ZH.get(str(payload['status']), payload['status'])}",
        f"- 最终结论：{_outcome_label(payload.get('final_outcome'))}",
        "",
        "## 问题",
        "",
        str(statement),
        "",
        "### 精确否定",
        "",
        str(exact_negation),
        "",
        "## 结论与核验边界",
        "",
        "六项状态必须分别阅读；任何一项通过都不自动等于完整命题已证明。",
        "",
    ]
    if isinstance(claims, tuple | list):
        for claim in claims:
            if isinstance(claim, Mapping):
                label = claim.get("stable_label", "未命名命题")
                states = "；".join(
                    f"{_AXIS_LABELS[axis]}={_STATE_ZH.get(str(claim[axis]), claim[axis])}"
                    for axis in _AXIS_LABELS
                )
                lines.append(f"- **{label}**：{states}")
    lines.extend(["", "## 尚未解决", ""])
    if isinstance(obligations, tuple | list) and obligations:
        lines.append(f"- 仍有 {len(obligations)} 项登记义务没有关闭。")
    else:
        lines.append("- 无（这只表示登记的开放义务为空，不单独构成已证明结论）")
    lines.extend(["", "## 研究路线", ""])
    if isinstance(routes, tuple | list) and routes:
        for index, item in enumerate(routes, 1):
            if isinstance(item, Mapping):
                label = item.get("label") or f"路线 {index}"
                lines.append(f"- {label}：{_route_label(item.get('status'))}")
    else:
        lines.append("- 尚未登记研究路线。")
    lines.extend(["", "## 人类高层提示", ""])
    if isinstance(research_hints, tuple | list) and research_hints:
        for item in research_hints:
            if isinstance(item, Mapping):
                lines.append(
                    f"- {item.get('hint_kind', 'OTHER')}：{item.get('hint_text', '')}"
                    "（只影响研究策略，不直接写入事实图）"
                )
    else:
        lines.append("- 无")
    lines.extend(["", "## 角色工作", ""])
    if isinstance(roles, tuple | list) and roles:
        for item in roles:
            if isinstance(item, Mapping):
                label = item.get("title_zh") or item.get("role_name_zh") or item.get("role")
                status = item.get("status_zh") or item.get("status") or "已登记"
                lines.append(f"- {label or '数学角色'}：{status}")
    else:
        lines.append("- 当前卷宗尚无已登记的角色活动。")
    lines.extend(["", "## 组件时间与 token", ""])
    if isinstance(component_usage, Mapping) and component_usage:
        for component, raw_usage in sorted(component_usage.items(), key=lambda pair: str(pair[0])):
            usage = raw_usage if isinstance(raw_usage, Mapping) else {}
            unknown = _safe_int(usage.get("unknown_count"))
            note = f"；{unknown} 次用量未知" if unknown else ""
            lines.append(
                f"- {component}：{_usage_tokens(usage):,} token，"
                f"{_duration_ms(_safe_int(usage.get('wall_time_ms')))}{note}"
            )
    else:
        lines.append("- 尚无可信计量。")
    lines.extend(["", "## 失败与阻塞", ""])
    failure_lines = _failure_descriptions(failures, host_receipts)
    if failure_lines:
        lines.extend(f"- {item}" for item in failure_lines)
    else:
        lines.append("- 无已记录问题。")
    lines.extend(["", "## 候选证据与审查意见", ""])
    if not evidence and not peer_reviews and not quality_reviews:
        lines.append("- 无")
    if isinstance(evidence, tuple | list):
        for item in evidence:
            if isinstance(item, Mapping):
                strength = _EVIDENCE_STRENGTH.get(str(item.get("evidence_strength")), "候选材料")
                authority = (
                    "当前可参与机器晋级。"
                    if item.get("promotion_eligible")
                    else "当前不产生数学权威。"
                )
                lines.append(f"- 一份候选证据：{strength}。材料已收下并不等于内容成立；{authority}")
    for label, reviews in (("同行意见", peer_reviews), ("质量意见", quality_reviews)):
        if isinstance(reviews, tuple | list):
            for item in reviews:
                if isinstance(item, Mapping):
                    authority = (
                        "可参与晋级。"
                        if item.get("promotion_eligible")
                        else "当前只作意见保存，不产生数学权威。"
                    )
                    lines.append(f"- {label}：{_review_label(item.get('verdict'))}；{authority}")
    if isinstance(atomic_verifications, tuple | list):
        for item in atomic_verifications:
            if not isinstance(item, Mapping):
                continue
            verdict = item.get("verdict", "UNKNOWN")
            backend = item.get("backend", "UNKNOWN")
            feedback = str(item.get("repair_feedback", "")).strip()
            suffix = f"；修复提示：{feedback}" if feedback else ""
            lines.append(f"- 原子 Claim 验证：{backend} / {verdict}{suffix}")
    next_step = _next_step(payload["status"], payload["run_id"])
    lines.extend(["", "## 建议的下一步", "", f"- {next_step}", ""])
    return "\n".join(lines)


def _outcome_label(value: Any) -> str:
    return "尚未结案" if not value else str(_OUTCOME_ZH.get(str(value), value))


def _route_label(value: Any) -> str:
    labels = {
        "PROPOSED": "待尝试",
        "ACTIVE": "进行中",
        "BLOCKED": "受阻",
        "RETIRED": "已停止",
        "PROVED": "路线完成",
        "REFUTED": "路线被否证",
    }
    return labels.get(str(value), str(value or "待尝试"))


def _review_label(value: Any) -> str:
    labels = {
        "ACCEPT": "通过",
        "NEEDS_REVISION": "需要修订",
        "REJECT": "不通过",
        "ABSTAIN": "无法判断",
    }
    return labels.get(str(value), "未给出结论")


def _safe_int(value: Any) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) and value >= 0 else 0


def _usage_tokens(value: Mapping[str, Any]) -> int:
    total = _safe_int(value.get("total_tokens"))
    return total or sum(
        _safe_int(value.get(key))
        for key in ("input_tokens", "output_tokens", "reasoning_tokens")
    )


def _duration_ms(value: int) -> str:
    if value < 1_000:
        return f"{value} 毫秒"
    seconds = value / 1_000
    if seconds < 60:
        return f"{seconds:.1f} 秒"
    minutes, remainder = divmod(round(seconds), 60)
    return f"{minutes} 分 {remainder} 秒"


def _failure_descriptions(failures: Any, receipts: Any) -> list[str]:
    result: list[str] = []
    if isinstance(failures, tuple | list):
        for item in failures:
            if isinstance(item, Mapping):
                value = item.get("message_zh") or item.get("summary_zh") or item.get("failure_kind")
                if value:
                    result.append(str(value))
    if isinstance(receipts, tuple | list):
        for item in receipts:
            if not isinstance(item, Mapping):
                continue
            reasons = item.get("block_reasons")
            if isinstance(reasons, tuple | list):
                result.extend(str(reason) for reason in reasons if reason)
            elif item.get("status") not in {None, "COMPLETED", "SUCCEEDED", "SUCCESS"}:
                result.append("有一次组件执行未成功，结果未用于数学结论。")
    return result[:12]


def _next_step(status: Any, run_id: Any) -> str:
    if status == "OPEN":
        return f"运行 `rkctl 开始 {run_id}`。"
    if status == "PAUSED":
        return f"运行 `rkctl 恢复 {run_id}`。"
    if status == "CLOSED":
        return "如需进一步检查，可导入一份人类审查材料。"
    return f"运行 `rkctl 继续 {run_id}`；需要暂离时可先暂停。"
