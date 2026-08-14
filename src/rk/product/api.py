"""Stable values crossing the private product implementation boundary.

HTTP, the desktop application and the product CLI all call this four-method seam.  A
session contains only identity established by the session adapter; command bodies cannot
supply a role, principal or capability.
"""

from __future__ import annotations

from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from types import MappingProxyType
from typing import Protocol

type JsonScalar = str | int | bool | None
type JsonValue = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
type JsonObject = Mapping[str, JsonValue]


def frozen_json(value: Mapping[str, JsonValue] | None = None) -> JsonObject:
    return MappingProxyType(dict(value or {}))


@dataclass(frozen=True, slots=True)
class ProductSession:
    session_id: str
    principal_subject_id: str
    capability_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class GlobalScope:
    deployment_id: str
    expected_deployment_revision: int
    kind: str = field(default="GLOBAL", init=False)


@dataclass(frozen=True, slots=True)
class RunScope:
    run_id: str
    expected_revision: int
    expected_contract_version: int
    kind: str = field(default="RUN", init=False)


@dataclass(frozen=True, slots=True)
class DeploymentScope:
    deployment_id: str
    expected_deployment_revision: int
    kind: str = field(default="DEPLOYMENT", init=False)


type WriteScope = GlobalScope | RunScope | DeploymentScope


@dataclass(frozen=True, slots=True)
class ProductCommand:
    request_id: str
    scope: WriteScope
    command_type: str
    payload: JsonObject = field(default_factory=frozen_json)


@dataclass(frozen=True, slots=True)
class ProductDecision:
    accepted: bool
    revision_before: int
    revision_after: int
    contract_version: int
    event_cursor_after: int
    rejection_code: str | None = None
    missing_conditions: tuple[JsonObject, ...] = ()
    affected_entity_ids: tuple[str, ...] = ()
    created_artifact_refs: tuple[JsonObject, ...] = ()
    created_run_id: str | None = None
    kernel_receipts: tuple[JsonObject, ...] = ()
    available_actions: tuple[JsonObject, ...] = ()


@dataclass(frozen=True, slots=True)
class ProductReceipt:
    receipt_id: str
    receipt_version: int
    request_id: str
    scope: WriteScope
    state: str
    updated_at: str
    decision: ProductDecision | None = None
    job_id: str | None = None
    unknown_external_call_ref: str | None = None
    supersedes_or_resolves_receipt_id: str | None = None
    decided_at: str | None = None


@dataclass(frozen=True, slots=True)
class QuerySpec:
    scope: JsonObject
    query_type: str
    payload: JsonObject = field(default_factory=frozen_json)


@dataclass(frozen=True, slots=True)
class QueryResult:
    result_type: str
    stable_entity_id: str
    fence: JsonObject
    data: JsonObject


@dataclass(frozen=True, slots=True)
class SubscriptionSpec:
    scope: JsonObject
    after_cursor: int
    event_types: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class PublicActivity:
    cursor: int
    event_type: str
    scope: JsonObject
    recorded_at: str
    payload: JsonObject


class EventStream(Protocol):
    def __iter__(self) -> Iterator[PublicActivity]: ...

    def close(self) -> None: ...


@dataclass(frozen=True, slots=True)
class ArtifactOperation:
    operation_type: str
    payload: JsonObject


@dataclass(frozen=True, slots=True)
class ArtifactResult:
    result_type: str
    payload: JsonObject


class ResearchProduct(Protocol):
    """The only public backend interface for product callers."""

    def command(self, session: ProductSession, request: ProductCommand) -> ProductReceipt: ...

    def query(self, session: ProductSession, spec: QuerySpec) -> QueryResult: ...

    def subscribe(self, session: ProductSession, spec: SubscriptionSpec) -> EventStream: ...

    def artifact(self, session: ProductSession, request: ArtifactOperation) -> ArtifactResult: ...
