"""Thin product CLI and HTTP command adapters sharing one JSON translation path."""

from __future__ import annotations

import json
from collections.abc import Mapping
from typing import Protocol, cast

from rk.product.api import (
    DeploymentScope,
    GlobalScope,
    JsonObject,
    JsonValue,
    ProductCommand,
    ProductReceipt,
    ProductSession,
    RunScope,
)


class CommandProduct(Protocol):
    """The command facet consumed by command transports."""

    def command(self, session: ProductSession, request: ProductCommand) -> ProductReceipt: ...


class ProductWireError(ValueError):
    """The adapter received a value outside the generated product contract."""


def command_from_json(value: Mapping[str, JsonValue]) -> ProductCommand:
    required = {"schema_version", "request_id", "scope", "command"}
    allowed = required | {"artifact_inputs"}
    if (
        set(value) not in (required, allowed)
        or value.get("schema_version") != "rk.product.command.v1"
        or ("artifact_inputs" in value and value["artifact_inputs"] != [])
    ):
        raise ProductWireError("invalid product command envelope")
    scope_value = _object(value["scope"], "scope")
    command_value = _object(value["command"], "command")
    scope = _scope(scope_value)
    if set(command_value) != {"type", "payload"}:
        raise ProductWireError("invalid product command union")
    command_type = command_value["type"]
    request_id = value["request_id"]
    if not isinstance(command_type, str) or not isinstance(request_id, str):
        raise ProductWireError("command type and request id must be strings")
    payload = _object(command_value["payload"], "command.payload")
    _reject_identity(payload, "command.payload")
    return ProductCommand(
        request_id=request_id,
        scope=scope,
        command_type=command_type,
        payload=payload,
    )


def receipt_to_json(receipt: ProductReceipt) -> dict[str, JsonValue]:
    scope: dict[str, JsonValue]
    if isinstance(receipt.scope, RunScope):
        scope = {
            "kind": receipt.scope.kind,
            "run_id": receipt.scope.run_id,
            "expected_revision": receipt.scope.expected_revision,
            "expected_contract_version": receipt.scope.expected_contract_version,
        }
    else:
        scope = {
            "kind": receipt.scope.kind,
            "deployment_id": receipt.scope.deployment_id,
            "expected_deployment_revision": receipt.scope.expected_deployment_revision,
        }
    result: dict[str, JsonValue] = {
        "schema_version": "rk.product.receipt.v1",
        "receipt_id": receipt.receipt_id,
        "receipt_version": receipt.receipt_version,
        "request_id": receipt.request_id,
        "scope": scope,
        "state": receipt.state,
        "updated_at": receipt.updated_at,
    }
    if receipt.job_id is not None:
        result["job_id"] = receipt.job_id
    if receipt.decided_at is not None:
        result["decided_at"] = receipt.decided_at
    if receipt.unknown_external_call_ref is not None:
        result["unknown_external_call_ref"] = receipt.unknown_external_call_ref
    return result


class CommandJsonAdapter:
    def __init__(self, product: CommandProduct) -> None:
        self._product = product

    def invoke(
        self, session: ProductSession, value: Mapping[str, JsonValue]
    ) -> dict[str, JsonValue]:
        return receipt_to_json(self._product.command(session, command_from_json(value)))


class ProductCliAdapter:
    def __init__(self, adapter: CommandJsonAdapter) -> None:
        self._adapter = adapter

    def command(self, session: ProductSession, line: str) -> str:
        try:
            value = json.loads(line)
        except json.JSONDecodeError as error:
            raise ProductWireError("invalid JSON") from error
        if not isinstance(value, dict):
            raise ProductWireError("command envelope must be an object")
        result = self._adapter.invoke(session, cast(dict[str, JsonValue], value))
        return json.dumps(result, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class ProductHttpCommandAdapter:
    def __init__(self, adapter: CommandJsonAdapter) -> None:
        self._adapter = adapter

    def command(
        self, session: ProductSession, body: Mapping[str, JsonValue]
    ) -> dict[str, JsonValue]:
        return self._adapter.invoke(session, body)


def _scope(value: JsonObject) -> GlobalScope | RunScope | DeploymentScope:
    kind = value.get("kind")
    if kind == "RUN" and set(value) == {
        "kind",
        "run_id",
        "expected_revision",
        "expected_contract_version",
    }:
        return RunScope(
            _string(value["run_id"], "run_id"),
            _integer(value["expected_revision"], "expected_revision"),
            _integer(value["expected_contract_version"], "expected_contract_version"),
        )
    if kind == "GLOBAL" and set(value) == {"kind", "deployment_id"}:
        return GlobalScope(_string(value["deployment_id"], "deployment_id"))
    if kind in {"GLOBAL", "DEPLOYMENT"} and set(value) == {
        "kind",
        "deployment_id",
        "expected_deployment_revision",
    }:
        scope_type = GlobalScope if kind == "GLOBAL" else DeploymentScope
        return scope_type(
            _string(value["deployment_id"], "deployment_id"),
            _integer(value["expected_deployment_revision"], "expected_deployment_revision"),
        )
    raise ProductWireError("invalid command scope")


def _object(value: JsonValue, path: str) -> JsonObject:
    if not isinstance(value, dict):
        raise ProductWireError(f"{path} must be an object")
    return value


def _string(value: JsonValue, path: str) -> str:
    if not isinstance(value, str):
        raise ProductWireError(f"{path} must be a string")
    return value


def _integer(value: JsonValue, path: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ProductWireError(f"{path} must be an integer")
    return value


def _reject_identity(value: JsonValue | JsonObject, path: str) -> None:
    forbidden = {"actor", "role", "capability", "capability_id", "principal_subject_id"}
    if isinstance(value, Mapping):
        for key, item in value.items():
            if key in forbidden:
                raise ProductWireError(f"identity field is forbidden at {path}.{key}")
            _reject_identity(item, f"{path}.{key}")
    elif isinstance(value, list):
        for index, item in enumerate(value):
            _reject_identity(item, f"{path}[{index}]")
