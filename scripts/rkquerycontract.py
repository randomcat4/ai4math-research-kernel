"""Derive SDK metadata and the transport envelope from strict query.schema.json."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

Schema = dict[str, Any]


def _rewrite_refs(value: Any) -> Any:
    if isinstance(value, list):
        return [_rewrite_refs(item) for item in value]
    if not isinstance(value, dict):
        return value
    rewritten = {key: _rewrite_refs(item) for key, item in value.items()}
    ref = rewritten.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/$defs/"):
        rewritten["$ref"] = "#/$defs/query_" + ref.removeprefix("#/$defs/")
    return rewritten


def _query_type(branch: Schema, defs: dict[str, Schema]) -> tuple[str, str, str]:
    scope_ref = branch["properties"]["scope"]["$ref"]
    scope_kind = {
        "#/$defs/runScope": "RUN",
        "#/$defs/globalScope": "GLOBAL",
        "#/$defs/deploymentScope": "DEPLOYMENT",
    }[scope_ref]
    query_ref = branch["properties"]["query"]["$ref"].removeprefix("#/$defs/")
    query_type = defs[query_ref]["properties"]["type"]["const"]
    return query_type, scope_kind, query_ref


def _payload_fields(payload: Schema) -> tuple[list[str], list[str]]:
    variants = payload.get("oneOf", [payload])
    names: set[str] = set()
    required_sets: list[set[str]] = []
    for variant in variants:
        names.update(variant["properties"])
        required_sets.append(set(variant.get("required", [])))
    required = set.intersection(*required_sets)
    return sorted(required), sorted(names - required)


def metadata(root: Path) -> dict[str, dict[str, Any]]:
    schema = json.loads((root / "docs/spec/product/query.schema.json").read_text(encoding="utf-8"))
    defs = schema["$defs"]
    result: dict[str, dict[str, Any]] = {}
    for branch in defs["querySpec"]["oneOf"]:
        query_type, scope_kind, query_ref = _query_type(branch, defs)
        payload = defs[query_ref]["properties"]["payload"]
        required, optional = _payload_fields(payload)
        entry = result.setdefault(
            query_type,
            {
                "scope_kinds": [],
                "required_payload_fields": required,
                "optional_payload_fields": optional,
            },
        )
        if scope_kind not in entry["scope_kinds"]:
            entry["scope_kinds"].append(scope_kind)
    for entry in result.values():
        entry["scope_kinds"].sort()
    return dict(sorted(result.items()))


def generate(root: Path) -> None:
    query_path = root / "docs/spec/product/query.schema.json"
    query_schema = json.loads(query_path.read_text(encoding="utf-8"))
    envelope_path = root / "docs/spec/product/envelope.schema.json"
    envelope = json.loads(envelope_path.read_text(encoding="utf-8"))
    envelope["$defs"].pop("queryPayload", None)
    for name in [key for key in envelope["$defs"] if key.startswith("query_")]:
        del envelope["$defs"][name]
    for name, definition in query_schema["$defs"].items():
        envelope["$defs"]["query_" + name] = _rewrite_refs(copy.deepcopy(definition))
    envelope["$defs"]["querySpec"] = envelope["$defs"].pop("query_querySpec")
    envelope["$defs"]["queryResult"] = envelope["$defs"].pop("query_queryResult")
    result_ref = {"$ref": "#/$defs/queryResult"}
    if result_ref not in envelope["oneOf"]:
        envelope["oneOf"].append(result_ref)
    envelope_path.write_text(
        json.dumps(envelope, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
