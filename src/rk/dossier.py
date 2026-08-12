"""Deterministic dossier projection from one persisted run revision."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Any

from rk.domain import RunSnapshot


class DossierBuilder:
    def build(self, snapshot: RunSnapshot, dossier_spec: Mapping[str, Any]) -> tuple[bytes, str]:
        if bool(dossier_spec.get("include_raw_artifacts", False)):
            raise ValueError("raw artifact materialization is not implemented")
        language = str(dossier_spec.get("language", "zh-CN"))
        output_format = str(dossier_spec.get("format", "JSON"))
        projection = dict(snapshot.projection)
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


def _markdown(payload: Mapping[str, Any]) -> str:
    projection = payload["projection"]
    claims = projection.get("claims", []) if isinstance(projection, Mapping) else []
    obligations = (
        projection.get("open_obligation_ids", []) if isinstance(projection, Mapping) else []
    )
    lines = [
        "# Research dossier",
        "",
        f"- run: `{payload['run_id']}`",
        f"- revision: `{payload['at_revision']}`",
        f"- status: `{payload['status']}`",
        f"- contract version: `{payload['current_contract_version']}`",
        "",
        "## Claims",
        "",
    ]
    for claim in claims:
        if isinstance(claim, Mapping):
            claim_id = claim.get("claim_id", "unknown")
            route_result = claim.get("route_result", "UNASSESSED")
            lines.append(
                f"- `{claim_id}`: route={route_result}, "
                f"machine={claim.get('machine_verdict', 'UNVERIFIED')}, "
                f"semantic={claim.get('semantic_verdict', 'UNREVIEWED')}, "
                f"peer={claim.get('peer_verdict', 'UNREVIEWED')}, "
                f"quality={claim.get('quality_verdict', 'UNREVIEWED')}, "
                f"closure={claim.get('closure_state', 'OPEN')}"
            )
    lines.extend(["", "## Open obligations", ""])
    lines.extend(f"- `{item}`" for item in obligations)
    lines.append("")
    return "\n".join(lines)
