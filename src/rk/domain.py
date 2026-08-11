"""Stable domain values crossing the ResearchKernel interface.

The internal implementation may change freely. These immutable values, their invariants, and
their wire representations are the public seam shared by callers and tests.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Mapping, Sequence

JsonObject = Mapping[str, Any]


class RunStatus(StrEnum):
    OPEN = "OPEN"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    CLOSED = "CLOSED"
    CONTRACT_DEFECTIVE = "CONTRACT_DEFECTIVE"


class ContractStatus(StrEnum):
    DRAFT = "DRAFT"
    FROZEN = "FROZEN"
    DEFECT_PROPOSED = "DEFECT_PROPOSED"
    SUPERSEDED = "SUPERSEDED"


class CompositionMode(StrEnum):
    MACHINE = "MACHINE"
    PEER = "PEER"
    HYBRID = "HYBRID"


class RejectionCode(StrEnum):
    REVISION_CONFLICT = "REVISION_CONFLICT"
    CAPABILITY_DENIED = "CAPABILITY_DENIED"
    RUN_CLOSED = "RUN_CLOSED"
    CONTRACT_NOT_FROZEN = "CONTRACT_NOT_FROZEN"
    CONTRACT_DEFECTIVE = "CONTRACT_DEFECTIVE"
    INVALID_TRANSITION = "INVALID_TRANSITION"
    EVIDENCE_INSUFFICIENT = "EVIDENCE_INSUFFICIENT"
    EVIDENCE_SCOPE_MISMATCH = "EVIDENCE_SCOPE_MISMATCH"
    ARTIFACT_MISSING = "ARTIFACT_MISSING"
    INGEST_SCHEMA_INVALID = "INGEST_SCHEMA_INVALID"
    MIXED_OUTPUT = "MIXED_OUTPUT"
    SECRET_QUARANTINED = "SECRET_QUARANTINED"
    COMPOSITION_OPEN = "COMPOSITION_OPEN"
    INDEPENDENCE_UNKNOWN = "INDEPENDENCE_UNKNOWN"
    BUDGET_DENIED = "BUDGET_DENIED"
    LEASE_CONFLICT = "LEASE_CONFLICT"
    REPLAY_FAILED = "REPLAY_FAILED"
    ENVIRONMENT_DRIFT = "ENVIRONMENT_DRIFT"
    TERMINAL_CLAIM_UNSUPPORTED = "TERMINAL_CLAIM_UNSUPPORTED"
    IDEMPOTENCY_KEY_REUSED = "IDEMPOTENCY_KEY_REUSED"
    TEMPORARILY_UNAVAILABLE = "TEMPORARILY_UNAVAILABLE"


def frozen_mapping(value: Mapping[str, Any] | None = None) -> Mapping[str, Any]:
    """Return a shallow immutable copy suitable for frozen interface values."""

    return MappingProxyType(dict(value or {}))


@dataclass(frozen=True, slots=True)
class ArtifactInput:
    name: str
    path: str
    sha256: str
    byte_count: int
    media_type: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> ArtifactInput:
        return cls(
            name=str(value["name"]),
            path=str(value["path"]),
            sha256=str(value["sha256"]),
            byte_count=int(value["byte_count"]),
            media_type=str(value["media_type"]),
        )


@dataclass(frozen=True, slots=True)
class TypedCommand:
    type: str
    payload: Mapping[str, Any] = field(default_factory=frozen_mapping)

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> TypedCommand:
        return cls(type=str(value["type"]), payload=frozen_mapping(value.get("payload", {})))


@dataclass(frozen=True, slots=True)
class CreateRequest:
    request_id: str
    contract: Mapping[str, Any]
    artifact_inputs: tuple[ArtifactInput, ...] = ()

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> CreateRequest:
        return cls(
            request_id=str(value["request_id"]),
            contract=frozen_mapping(value["contract"]),
            artifact_inputs=tuple(
                ArtifactInput.from_mapping(item) for item in value.get("artifact_inputs", ())
            ),
        )


@dataclass(frozen=True, slots=True)
class ApplyRequest:
    request_id: str
    run_id: str
    expected_revision: int
    command: TypedCommand
    artifact_inputs: tuple[ArtifactInput, ...] = ()

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> ApplyRequest:
        return cls(
            request_id=str(value["request_id"]),
            run_id=str(value["run_id"]),
            expected_revision=int(value["expected_revision"]),
            command=TypedCommand.from_mapping(value["command"]),
            artifact_inputs=tuple(
                ArtifactInput.from_mapping(item) for item in value.get("artifact_inputs", ())
            ),
        )


@dataclass(frozen=True, slots=True)
class ExportRequest:
    request_id: str
    run_id: str
    at_revision: int
    dossier_spec: Mapping[str, Any]

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> ExportRequest:
        return cls(
            request_id=str(value["request_id"]),
            run_id=str(value["run_id"]),
            at_revision=int(value["at_revision"]),
            dossier_spec=frozen_mapping(value["dossier_spec"]),
        )


@dataclass(frozen=True, slots=True)
class VerifiedCapability:
    capability_id: str
    subject_id: str
    issuer: str
    allowed_actions: frozenset[str]
    run_scope: frozenset[str]
    issued_at: str
    expires_at: str

    def allows(self, action: str, run_id: str | None = None) -> bool:
        action_allowed = action in self.allowed_actions or "*" in self.allowed_actions
        scope_allowed = run_id is None or "*" in self.run_scope or run_id in self.run_scope
        return action_allowed and scope_allowed


@dataclass(frozen=True, slots=True)
class MissingCondition:
    code: str
    path: str
    params: Mapping[str, Any] = field(default_factory=frozen_mapping)

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "path": self.path, "params": dict(self.params)}


@dataclass(frozen=True, slots=True)
class Decision:
    accepted: bool
    rejection_code: str | None = None
    missing_conditions: tuple[MissingCondition, ...] = ()
    projection_mutations: tuple[Mapping[str, Any], ...] = ()
    event_intents: tuple[Mapping[str, Any], ...] = ()
    artifact_requirements: tuple[Mapping[str, Any], ...] = ()

    def __post_init__(self) -> None:
        if self.accepted and (self.rejection_code is not None or self.missing_conditions):
            raise ValueError("accepted decisions cannot contain rejection details")
        if not self.accepted and (self.rejection_code is None or not self.missing_conditions):
            raise ValueError("rejected decisions require a code and missing conditions")


@dataclass(frozen=True, slots=True)
class RunHandle:
    run_id: str
    revision: int
    status: str
    current_contract_version: int
    created_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "rk.handle.v1",
            "run_id": self.run_id,
            "revision": self.revision,
            "status": self.status,
            "current_contract_version": self.current_contract_version,
            "created_at": self.created_at,
        }


@dataclass(frozen=True, slots=True)
class CommandReceipt:
    request_id: str
    command_id: str
    run_id: str
    accepted: bool
    revision_before: int
    revision_after: int
    event_ids: tuple[str, ...]
    artifact_ids: tuple[str, ...]
    rejection_code: str | None
    missing_conditions: tuple[MissingCondition, ...]
    decided_at: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "rk.receipt.v1",
            "request_id": self.request_id,
            "command_id": self.command_id,
            "run_id": self.run_id,
            "accepted": self.accepted,
            "revision_before": self.revision_before,
            "revision_after": self.revision_after,
            "event_ids": list(self.event_ids),
            "artifact_ids": list(self.artifact_ids),
            "rejection_code": self.rejection_code,
            "missing_conditions": [item.to_dict() for item in self.missing_conditions],
            "decided_at": self.decided_at,
        }


@dataclass(frozen=True, slots=True)
class ArtifactRef:
    artifact_id: str
    sha256: str
    byte_count: int
    media_type: str
    at_revision: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "rk.artifact_ref.v1",
            "artifact_id": self.artifact_id,
            "sha256": self.sha256,
            "byte_count": self.byte_count,
            "media_type": self.media_type,
            "at_revision": self.at_revision,
        }


@dataclass(frozen=True, slots=True)
class RunSnapshot:
    run_id: str
    status: str
    revision: int
    current_contract_version: int
    last_cursor: int
    projection: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "rk.snapshot.v1",
            "run_id": self.run_id,
            "status": self.status,
            "revision": self.revision,
            "current_contract_version": self.current_contract_version,
            "last_cursor": self.last_cursor,
            **dict(self.projection),
        }


@dataclass(frozen=True, slots=True)
class EventPage:
    run_id: str
    after_cursor: int
    events: tuple[Mapping[str, Any], ...]
    next_cursor: int
    has_more: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "rk.events.v1",
            "run_id": self.run_id,
            "after_cursor": self.after_cursor,
            "events": [dict(event) for event in self.events],
            "next_cursor": self.next_cursor,
            "has_more": self.has_more,
        }


class KernelError(RuntimeError):
    """Base exception for failures that cannot be represented by a business receipt."""


class RequestValidationError(KernelError):
    """The request is not a valid wire-level object."""


class CapabilityError(KernelError):
    """A credential could not be authenticated before entering the business guard."""


def ensure_unique(values: Sequence[str], label: str) -> tuple[str, ...]:
    result = tuple(values)
    if len(result) != len(set(result)):
        raise ValueError(f"{label} must not contain duplicates")
    return result
