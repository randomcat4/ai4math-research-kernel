"""Dependency-free ResearchProduct client and lossless JSON codec."""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from .types import COMMAND_CONTRACTS, QUERY_CONTRACTS, ArtifactOperationType, CommandType, QueryType

type JsonScalar = bool | int | str | None
type JsonValue = JsonScalar | list[JsonValue] | dict[str, JsonValue]
type JsonObject = dict[str, JsonValue]
MAX_SAFE_INTEGER = 9_007_199_254_740_991
FORBIDDEN_IDENTITY_KEYS = frozenset(
    {"actor", "role", "capability", "capability_id", "principal_subject_id"}
)


class ProductSdkError(ValueError):
    """Base class for deterministic client-side contract errors."""


class UnknownVariantError(ProductSdkError):
    """The server or caller used a variant this SDK version does not know."""


class UnsafeJsonValueError(ProductSdkError):
    """A value cannot make a lossless Python/TypeScript JSON round trip."""


class InvalidEnvelopeError(ProductSdkError):
    """A transport response is not a product envelope."""


class ProductTransport(Protocol):
    def __call__(self, operation: str, body: JsonObject) -> Mapping[str, Any]: ...


def _lossless_json(value: Any, path: str = "$") -> JsonValue:
    if value is None or isinstance(value, bool | str):
        return value
    if isinstance(value, int):
        if abs(value) > MAX_SAFE_INTEGER:
            raise UnsafeJsonValueError(f"{path}: integer exceeds the cross-SDK safe range")
        return value
    if isinstance(value, float):
        raise UnsafeJsonValueError(f"{path}: floating-point values are not contract values")
    if isinstance(value, list | tuple):
        return [_lossless_json(item, f"{path}[{index}]") for index, item in enumerate(value)]
    if isinstance(value, Mapping):
        result: JsonObject = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise UnsafeJsonValueError(f"{path}: object keys must be strings")
            if key in result:
                raise UnsafeJsonValueError(f"{path}: duplicate object key {key!r}")
            result[key] = _lossless_json(item, f"{path}.{key}")
        return result
    raise UnsafeJsonValueError(f"{path}: unsupported JSON value {type(value).__name__}")


def _reject_identity_injection(value: JsonValue, path: str = "$") -> None:
    if isinstance(value, list):
        for index, item in enumerate(value):
            _reject_identity_injection(item, f"{path}[{index}]")
        return
    if not isinstance(value, dict):
        return
    for key, item in value.items():
        if key in FORBIDDEN_IDENTITY_KEYS:
            raise InvalidEnvelopeError(f"{path}.{key}: identity fields come from Session")
        _reject_identity_injection(item, f"{path}.{key}")


def lossless_json_bytes(value: Mapping[str, Any]) -> bytes:
    normalized = _lossless_json(value)
    if not isinstance(normalized, dict):
        raise UnsafeJsonValueError("$: product payload must be an object")
    return json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def lossless_json_loads(value: bytes | str) -> JsonObject:
    decoded = json.loads(value)
    normalized = _lossless_json(decoded)
    if not isinstance(normalized, dict):
        raise UnsafeJsonValueError("$: product payload must be an object")
    return normalized


@dataclass(frozen=True, slots=True)
class GlobalScope:
    deployment_id: str

    def to_dict(self) -> JsonObject:
        return {"kind": "GLOBAL", "deployment_id": self.deployment_id}


@dataclass(frozen=True, slots=True)
class RunScope:
    run_id: str
    expected_revision: int
    expected_contract_version: int

    def to_dict(self) -> JsonObject:
        return {
            "kind": "RUN",
            "run_id": self.run_id,
            "expected_revision": self.expected_revision,
            "expected_contract_version": self.expected_contract_version,
        }


@dataclass(frozen=True, slots=True)
class QueryRunScope:
    run_id: str
    at_revision: int | None = None
    at_contract_version: int | None = None

    def to_dict(self) -> JsonObject:
        value: JsonObject = {"kind": "RUN", "run_id": self.run_id}
        if self.at_revision is not None:
            value["at_revision"] = self.at_revision
        if self.at_contract_version is not None:
            value["at_contract_version"] = self.at_contract_version
        return value


@dataclass(frozen=True, slots=True)
class QueryDeploymentScope:
    deployment_id: str
    at_deployment_revision: int | None = None

    def to_dict(self) -> JsonObject:
        value: JsonObject = {"kind": "DEPLOYMENT", "deployment_id": self.deployment_id}
        if self.at_deployment_revision is not None:
            value["at_deployment_revision"] = self.at_deployment_revision
        return value


QueryScope = GlobalScope | QueryRunScope | QueryDeploymentScope


@dataclass(frozen=True, slots=True)
class DeploymentScope:
    deployment_id: str
    expected_deployment_revision: int

    def to_dict(self) -> JsonObject:
        return {
            "kind": "DEPLOYMENT",
            "deployment_id": self.deployment_id,
            "expected_deployment_revision": self.expected_deployment_revision,
        }


Scope = GlobalScope | RunScope | DeploymentScope


def _enum_value[T: str](enum_type: type[T], value: T | str, label: str) -> str:
    try:
        return str(enum_type(value))
    except ValueError as error:
        raise UnknownVariantError(f"unknown {label} variant {value!r}; upgrade the SDK") from error


def _validate_command_contract(command_type: str, payload: Mapping[str, Any], scope: Scope) -> None:
    contract = COMMAND_CONTRACTS[command_type]
    scope_kind = scope.to_dict()["kind"]
    if scope_kind not in contract["scope_kinds"]:
        raise InvalidEnvelopeError(
            f"{command_type} requires scope in {contract['scope_kinds']}, got {scope_kind}"
        )
    actual = set(payload)
    required = set(contract["required_payload_fields"])
    optional = set(contract["optional_payload_fields"])
    missing = required - actual
    unknown = actual - required - optional
    if missing:
        raise InvalidEnvelopeError(f"{command_type} payload is missing fields {sorted(missing)}")
    if unknown:
        raise InvalidEnvelopeError(f"{command_type} payload has unknown fields {sorted(unknown)}")


def _validate_query_contract(
    query_type: str, payload: Mapping[str, Any], scope: QueryScope
) -> None:
    contract = QUERY_CONTRACTS[query_type]
    scope_kind = scope.to_dict()["kind"]
    if scope_kind not in contract["scope_kinds"]:
        raise InvalidEnvelopeError(
            f"{query_type} requires query scope in {contract['scope_kinds']}, got {scope_kind}"
        )
    actual = set(payload)
    required = set(contract["required_payload_fields"])
    allowed = required | set(contract["optional_payload_fields"])
    if missing := required - actual:
        raise InvalidEnvelopeError(f"{query_type} payload is missing fields {sorted(missing)}")
    if unknown := actual - allowed:
        raise InvalidEnvelopeError(f"{query_type} payload has unknown fields {sorted(unknown)}")


class ResearchProductClient:
    """Only the four frozen ResearchProduct operation families."""

    def __init__(self, transport: ProductTransport) -> None:
        self._transport = transport

    def command(
        self,
        *,
        request_id: str,
        scope: Scope,
        command_type: CommandType | str,
        payload: Mapping[str, Any],
        artifact_inputs: list[Mapping[str, Any]] | None = None,
    ) -> JsonObject:
        normalized_type = _enum_value(CommandType, command_type, "command")
        body = self._body(
            {
                "schema_version": "rk.product.command.v1",
                "request_id": request_id,
                "scope": scope.to_dict(),
                "command": {
                    "type": normalized_type,
                    "payload": payload,
                },
                "artifact_inputs": artifact_inputs or [],
            }
        )
        _reject_identity_injection(body["command"])
        _validate_command_contract(normalized_type, payload, scope)
        return self._send("command", body)

    def query(
        self,
        *,
        scope: QueryScope,
        query_type: QueryType | str,
        payload: Mapping[str, Any],
    ) -> JsonObject:
        normalized_type = _enum_value(QueryType, query_type, "query")
        _validate_query_contract(normalized_type, payload, scope)
        body = self._body(
            {
                "schema_version": "rk.product.query.v1",
                "scope": scope.to_dict(),
                "query": {
                    "type": normalized_type,
                    "payload": payload,
                },
            }
        )
        _reject_identity_injection(body["query"])
        return self._send("query", body)

    def subscribe(
        self, *, run_id: str, after_cursor: int, event_types: list[str] | None = None
    ) -> JsonObject:
        body = self._body(
            {
                "schema_version": "rk.product.subscription.v1",
                "run_id": run_id,
                "after_cursor": after_cursor,
                "event_types": event_types or [],
            }
        )
        return self._send("subscribe", body)

    def artifact(
        self,
        *,
        request_id: str,
        operation_type: ArtifactOperationType | str,
        payload: Mapping[str, Any],
    ) -> JsonObject:
        body = self._body(
            {
                "schema_version": "rk.product.artifact.v1",
                "request_id": request_id,
                "operation": {
                    "type": _enum_value(
                        ArtifactOperationType, operation_type, "artifact operation"
                    ),
                    "payload": payload,
                },
            }
        )
        _reject_identity_injection(body["operation"])
        return self._send("artifact", body)

    @staticmethod
    def _body(value: Mapping[str, Any]) -> JsonObject:
        return lossless_json_loads(lossless_json_bytes(value))

    def _send(self, operation: str, body: JsonObject) -> JsonObject:
        response = self._transport(operation, body)
        normalized = _lossless_json(response)
        if not isinstance(normalized, dict) or not isinstance(
            normalized.get("schema_version"), str
        ):
            raise InvalidEnvelopeError(f"{operation} response has no schema_version")
        if operation == "query" and normalized["schema_version"] != "rk.product.query_result.v1":
            raise UnknownVariantError(
                f"unknown query result schema {normalized['schema_version']!r}; upgrade the SDK"
            )
        return normalized
