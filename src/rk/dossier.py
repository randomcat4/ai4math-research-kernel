"""Deterministic dossier projection from one persisted run revision."""

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
    claims = projection.get("claims", []) if isinstance(projection, Mapping) else []
    obligations = (
        projection.get("open_obligation_ids", []) if isinstance(projection, Mapping) else []
    )
    evidence = projection.get("evidence", []) if isinstance(projection, Mapping) else []
    peer_reviews = projection.get("peer_reviews", []) if isinstance(projection, Mapping) else []
    quality_reviews = (
        projection.get("quality_reviews", []) if isinstance(projection, Mapping) else []
    )
    contract = projection.get("contract", {}) if isinstance(projection, Mapping) else {}
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
        "# 数学研究卷宗",
        "",
        f"- 运行编号: `{payload['run_id']}`",
        f"- 修订号: `{payload['at_revision']}`",
        f"- 运行状态: `{payload['status']}`",
        f"- 最终结论: `{payload.get('final_outcome') or '尚未结案'}`",
        f"- 合同版本: `{payload['current_contract_version']}`",
        "",
        "## 问题",
        "",
        str(statement),
        "",
        "### 精确否定",
        "",
        str(exact_negation),
        "",
        "## 数学命题与证据状态",
        "",
    ]
    for claim in claims:
        if isinstance(claim, Mapping):
            claim_id = claim["claim_id"]
            label = claim.get("stable_label", claim_id)
            statement_text = claim.get("normalized_statement", {})
            lines.append(
                f"- **{label}** (`{claim_id}`): route={claim['route']}, "
                f"machine={claim['machine']}, "
                f"semantic={claim['semantic']}, "
                f"peer={claim['peer']}, "
                f"quality={claim['quality']}, "
                f"closure={claim['closure']}"
            )
            normalized = json.dumps(statement_text, ensure_ascii=False, sort_keys=True)
            lines.append(f"  - 规范化陈述: `{normalized}`")
    lines.extend(["", "## 尚未解决的义务", ""])
    lines.extend(f"- `{item}`" for item in obligations)
    lines.extend(["", "## 候选证据与审查意见", ""])
    if not evidence and not peer_reviews and not quality_reviews:
        lines.append("- 无")
    for item in evidence:
        if isinstance(item, Mapping):
            lines.append(
                f"- 证据 `{item.get('evidence_id')}`: 摄入状态={item.get('ingest_status')}, "
                f"声明强度={item.get('evidence_strength')}, "
                f"信任分类={item.get('trust_class')}, 权威作用={item.get('authority_effect')}, "
                f"可晋级={item.get('promotion_eligible')}"
            )
    for label, reviews in (("同行意见", peer_reviews), ("质量意见", quality_reviews)):
        for item in reviews:
            if isinstance(item, Mapping):
                review_id = item.get("review_id", item.get("quality_review_id"))
                lines.append(
                    f"- {label} `{review_id}`: 原始意见={item.get('verdict')}, "
                    f"信任分类={item.get('trust_class')}, "
                    f"权威作用={item.get('authority_effect')}, "
                    f"可晋级={item.get('promotion_eligible')}"
                )
    lines.append("")
    return "\n".join(lines)
