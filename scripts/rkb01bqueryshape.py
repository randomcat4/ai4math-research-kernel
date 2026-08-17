"""Freeze B01b LIST_RESEARCH and ACTION_ITEMS result projections."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any


def _text() -> dict[str, Any]:
    return {"type": "string", "minLength": 1}


def _texts() -> dict[str, Any]:
    return {"type": "array", "uniqueItems": True, "items": _text()}


def generate(root: Path) -> None:
    path = root / "docs/spec/product/query.schema.json"
    schema = json.loads(path.read_text())
    defs = schema["$defs"]
    for branch in defs["queryResult"]["oneOf"]:
        props = branch["properties"]
        kind = props["result_type"]["const"]
        if props["scope_kind"]["const"] != "GLOBAL":
            continue
        if kind == "LIST_RESEARCH":
            item = props["result"]["properties"]["items"]["items"]
            extra = {
                "run_id": {"$ref": "#/$defs/uuid"},
                "title": _text(),
                "question_summary": _text(),
                "owner": _text(),
                "labels": _texts(),
                "outcome_state": _text(),
                "execution_state": _text(),
                "authority_state": _text(),
                "publication_state": _text(),
                "phase": _text(),
                "blockers": _texts(),
                "next_actions": _texts(),
                "budget": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "reserved_microunits",
                        "actual_microunits",
                        "refunded_microunits",
                        "unknown_cost_count",
                    ],
                    "properties": {
                        x: {"$ref": "#/$defs/nat"}
                        for x in (
                            "reserved_microunits",
                            "actual_microunits",
                            "refunded_microunits",
                            "unknown_cost_count",
                        )
                    },
                },
                "recent_activity_at": {"type": "string", "format": "date-time"},
                "recent_activity_summary": _text(),
                "research_revision": {"$ref": "#/$defs/nat"},
                "contract_version": {"type": "integer", "minimum": 1},
                "last_cursor": {"$ref": "#/$defs/nat"},
            }
            item["properties"].update(extra)
            item["required"] = list(dict.fromkeys(item["required"] + list(extra)))
        elif kind == "ACTION_ITEMS":
            result = props["result"]
            item = {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "stable_entity_id",
                    "run_id",
                    "command_type",
                    "target_ids",
                    "required_inputs",
                    "blocked_by",
                    "research_revision",
                    "contract_version",
                ],
                "properties": {
                    "stable_entity_id": _text(),
                    "run_id": {"$ref": "#/$defs/uuid"},
                    "command_type": _text(),
                    "target_ids": {
                        "type": "array",
                        "uniqueItems": True,
                        "items": {"$ref": "#/$defs/uuid"},
                    },
                    "required_inputs": _texts(),
                    "blocked_by": _texts(),
                    "research_revision": {"$ref": "#/$defs/nat"},
                    "contract_version": {"type": "integer", "minimum": 1},
                },
            }
            result.clear()
            result.update(
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["items", "page"],
                    "properties": {
                        "items": {"type": "array", "maxItems": 200, "items": item},
                        "page": {"$ref": "#/$defs/pageInfo"},
                    },
                }
            )
    path.write_text(json.dumps(schema, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
