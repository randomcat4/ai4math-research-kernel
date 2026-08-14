from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "sdk" / "python"))

from rk_product import (  # noqa: E402
    GlobalScope,
    InvalidEnvelopeError,
    ResearchProductClient,
    RunScope,
)

SCHEMA_PATH = ROOT / "docs/spec/product/command.schema.json"
UUID = "7f857a15-bddb-4238-aa88-6dbeaec50f7a"


def _resolve(schema: dict[str, Any], root: dict[str, Any]) -> dict[str, Any]:
    while "$ref" in schema:
        schema = root["$defs"][schema["$ref"].removeprefix("#/$defs/")]
    return schema


def _example(schema: dict[str, Any], root: dict[str, Any]) -> Any:
    schema = _resolve(schema, root)
    if "oneOf" in schema:
        return _example(schema["oneOf"][0], root)
    if "const" in schema:
        return schema["const"]
    if "enum" in schema:
        return schema["enum"][0]
    schema_type = schema.get("type")
    if schema_type == "object":
        return {
            name: _example(schema["properties"][name], root) for name in schema.get("required", [])
        }
    if schema_type == "array":
        count = schema.get("minItems", 0)
        return [_example(schema["items"], root) for _ in range(count)]
    if schema_type == "integer":
        return schema.get("minimum", 0)
    if schema_type == "boolean":
        return True
    if schema.get("format") == "uuid":
        return UUID
    if schema.get("format") == "date-time":
        return "2026-08-13T18:00:00Z"
    if schema.get("pattern") == "^[0-9a-f]{64}$":
        return "a" * 64
    if schema_type == "string":
        return "contract-value"
    raise AssertionError(f"no example rule for {schema}")


def _schema() -> dict[str, Any]:
    return json.loads(SCHEMA_PATH.read_text(encoding="utf-8"))


def test_all_36_commands_have_a_satisfiable_closed_branch() -> None:
    schema = _schema()
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    branches = schema["$defs"]["productCommand"]["oneOf"]
    assert len(branches) == 36
    command_types = set()
    for branch in branches:
        instance = _example(branch, schema)
        assert not list(validator.iter_errors(instance))
        command_types.add(instance["command"]["type"])
    catalog = json.loads((SCHEMA_PATH.parent / "catalog.json").read_text(encoding="utf-8"))
    assert command_types == set(catalog["command_types"])


def _valid_create() -> dict[str, Any]:
    schema = _schema()
    branch = schema["$defs"]["productCommand"]["oneOf"][0]
    return _example(branch, schema)


def test_command_payload_is_closed_at_every_structured_level() -> None:
    schema = _schema()
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    command = _valid_create()
    command["command"]["payload"]["contract_draft"]["actor"] = "forged"
    assert list(validator.iter_errors(command))


def test_scope_is_bound_to_command_variant() -> None:
    schema = _schema()
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    command = _valid_create()
    command["scope"] = {
        "kind": "RUN",
        "run_id": UUID,
        "expected_revision": 0,
        "expected_contract_version": 1,
    }
    assert list(validator.iter_errors(command))


def test_clean_room_and_history_migration_cannot_claim_promotion() -> None:
    schema = _schema()
    validator = Draft202012Validator(schema, format_checker=FormatChecker())
    branch = next(
        item
        for item in schema["$defs"]["productCommand"]["oneOf"]
        if "ImportResearchLineage" in item["$ref"]
    )
    command = _example(branch, schema)
    command["command"]["payload"]["historical_conclusions_injected"] = True
    assert list(validator.iter_errors(command))


def test_python_sdk_rejects_scope_and_payload_shape_before_transport() -> None:
    sent: list[dict[str, Any]] = []

    def transport(_operation: str, body: dict[str, Any]) -> dict[str, Any]:
        sent.append(body)
        return {"schema_version": "rk.product.receipt.v1"}

    client = ResearchProductClient(transport)
    with pytest.raises(InvalidEnvelopeError, match="requires scope"):
        client.command(
            request_id=UUID,
            scope=RunScope(UUID, 0, 1),
            command_type="CREATE_RESEARCH",
            payload={},
        )
    with pytest.raises(InvalidEnvelopeError, match="missing fields"):
        client.command(
            request_id=UUID,
            scope=GlobalScope(UUID),
            command_type="CREATE_RESEARCH",
            payload={},
        )
    assert sent == []
