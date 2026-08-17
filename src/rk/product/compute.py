"""Typed tool declarations and argument preparation; this module executes no programs."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Protocol

from jsonschema import Draft202012Validator, FormatChecker

from rk.product.artifact_read import ArtifactReadService, ExactArtifactRef
from rk.wire import canonical_json_bytes


class ToolContractError(ValueError):
    """A tool declaration, argument artifact, or authority ceiling is invalid."""


class ToolAvailability(StrEnum):
    CONFIGURED_UNPROBED = "CONFIGURED_UNPROBED"
    AVAILABLE = "AVAILABLE"
    SMOKE_ONLY = "SMOKE_ONLY"
    PRODUCT_RECEIPT_AVAILABLE = "PRODUCT_RECEIPT_AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"
    EXTERNAL_BLOCKED = "EXTERNAL_BLOCKED"


class AuthorityCeiling(StrEnum):
    NO_FACT_GRAPH_WRITE = "NO_FACT_GRAPH_WRITE"
    SOFT_TOOL_RESULT = "SOFT_TOOL_RESULT"
    CERTIFICATE_REQUIRES_VALIDATION = "CERTIFICATE_REQUIRES_VALIDATION"


_AUTHORITY_RANK = {
    AuthorityCeiling.NO_FACT_GRAPH_WRITE: 0,
    AuthorityCeiling.SOFT_TOOL_RESULT: 1,
    AuthorityCeiling.CERTIFICATE_REQUIRES_VALIDATION: 2,
}


@dataclass(frozen=True, slots=True)
class ResourceRequest:
    cpu_millis: int
    memory_bytes: int
    wall_time_ms: int
    gpu_count: int = 0

    def __post_init__(self) -> None:
        if (
            self.cpu_millis <= 0
            or self.memory_bytes <= 0
            or self.wall_time_ms <= 0
            or self.gpu_count < 0
        ):
            raise ToolContractError("resource limits must be positive, with non-negative GPUs")

    def to_dict(self) -> dict[str, int]:
        return {
            "cpu_millis": self.cpu_millis,
            "memory_bytes": self.memory_bytes,
            "wall_time_ms": self.wall_time_ms,
            "gpu_count": self.gpu_count,
        }


@dataclass(frozen=True, slots=True)
class ResourceUsage:
    cpu_millis: int
    memory_peak_bytes: int
    wall_time_ms: int
    gpu_millis: int = 0

    def __post_init__(self) -> None:
        if (
            min(
                self.cpu_millis,
                self.memory_peak_bytes,
                self.wall_time_ms,
                self.gpu_millis,
            )
            < 0
        ):
            raise ToolContractError("measured resource usage cannot be negative")

    def to_dict(self) -> dict[str, int]:
        return {
            "cpu_millis": self.cpu_millis,
            "memory_peak_bytes": self.memory_peak_bytes,
            "wall_time_ms": self.wall_time_ms,
            "gpu_millis": self.gpu_millis,
        }

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> ResourceUsage:
        if set(value) != {
            "cpu_millis",
            "memory_peak_bytes",
            "wall_time_ms",
            "gpu_millis",
        }:
            raise ToolContractError("resource usage fields are not exact")
        fields = tuple(value[name] for name in value)
        if any(not isinstance(item, int) or isinstance(item, bool) for item in fields):
            raise ToolContractError("resource usage values must be integers")
        return cls(
            cpu_millis=int(value["cpu_millis"]),
            memory_peak_bytes=int(value["memory_peak_bytes"]),
            wall_time_ms=int(value["wall_time_ms"]),
            gpu_millis=int(value["gpu_millis"]),
        )


@dataclass(frozen=True, slots=True)
class ToolFunctionSpec:
    tool_id: str
    tool_version: str
    function_name: str
    provider: str
    build_version: str
    profile_id: str
    function_schema: Mapping[str, Any]
    function_schema_digest: str
    availability: ToolAvailability
    authority_ceiling: AuthorityCeiling

    def __post_init__(self) -> None:
        strings = (
            self.tool_id,
            self.tool_version,
            self.function_name,
            self.provider,
            self.build_version,
            self.profile_id,
        )
        if any(not value or value != value.strip() for value in strings):
            raise ToolContractError("tool identity strings must be non-empty and trimmed")
        schema = dict(self.function_schema)
        Draft202012Validator.check_schema(schema)
        digest = hashlib.sha256(canonical_json_bytes(schema)).hexdigest()
        if digest != self.function_schema_digest:
            raise ToolContractError("function schema digest does not match canonical schema")

    @property
    def key(self) -> tuple[str, str, str]:
        return self.tool_id, self.tool_version, self.function_name


class ArtifactJsonReader(Protocol):
    def read_json(self, artifact_ref: ExactArtifactRef) -> Mapping[str, Any]: ...


class B04bArtifactJsonReader:
    """Read one exact B04b artifact and parse it as duplicate-free JSON."""

    def __init__(self, artifacts: ArtifactReadService) -> None:
        self._artifacts = artifacts

    def read_json(self, artifact_ref: ExactArtifactRef) -> Mapping[str, Any]:
        result = self._artifacts.open_range(artifact_ref.artifact_id, expected_ref=artifact_ref)
        raw = b"".join(result.stream)
        return _decode_json_object(raw)


@dataclass(frozen=True, slots=True)
class PreparedToolInvocation:
    spec: ToolFunctionSpec
    arguments_artifact: ExactArtifactRef
    input_artifact_ids: tuple[str, ...]
    resources: ResourceRequest
    authority_ceiling: AuthorityCeiling
    arguments: Mapping[str, Any]


def prepare_tool_invocation(
    *,
    spec: ToolFunctionSpec,
    arguments_artifact: ExactArtifactRef,
    input_artifact_ids: Sequence[str],
    resources: ResourceRequest,
    authority_ceiling: AuthorityCeiling,
    artifacts: ArtifactJsonReader,
) -> PreparedToolInvocation:
    artifact_ids = tuple(input_artifact_ids)
    if any(not item for item in artifact_ids) or len(set(artifact_ids)) != len(artifact_ids):
        raise ToolContractError("input artifact IDs must be non-empty and unique")
    if _AUTHORITY_RANK[authority_ceiling] > _AUTHORITY_RANK[spec.authority_ceiling]:
        raise ToolContractError("requested authority ceiling exceeds registered tool ceiling")
    arguments = artifacts.read_json(arguments_artifact)
    validator = Draft202012Validator(dict(spec.function_schema), format_checker=FormatChecker())
    errors = sorted(validator.iter_errors(dict(arguments)), key=lambda item: list(item.path))
    if errors:
        detail = "; ".join(
            f"/{'/'.join(str(part) for part in error.path)}: {error.message}"
            for error in errors[:8]
        )
        raise ToolContractError(f"structured tool arguments failed schema: {detail}")
    return PreparedToolInvocation(
        spec=spec,
        arguments_artifact=arguments_artifact,
        input_artifact_ids=artifact_ids,
        resources=resources,
        authority_ceiling=authority_ceiling,
        arguments=_freeze(arguments),
    )


class _DuplicateKey(ValueError):
    pass


def _decode_json_object(raw: bytes) -> Mapping[str, Any]:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise _DuplicateKey(key)
            result[key] = value
        return result

    try:
        value = json.loads(raw.decode("utf-8"), object_pairs_hook=reject_duplicates)
    except (UnicodeDecodeError, json.JSONDecodeError, _DuplicateKey) as error:
        raise ToolContractError("argument artifact is not duplicate-free UTF-8 JSON") from error
    if not isinstance(value, dict):
        raise ToolContractError("argument artifact must contain a JSON object")
    return value


def _freeze(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


__all__ = [
    "ArtifactJsonReader",
    "AuthorityCeiling",
    "B04bArtifactJsonReader",
    "PreparedToolInvocation",
    "ResourceRequest",
    "ResourceUsage",
    "ToolAvailability",
    "ToolContractError",
    "ToolFunctionSpec",
    "prepare_tool_invocation",
]
