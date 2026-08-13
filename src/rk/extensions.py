"""Stable registration seams between the research kernel and product modules.

This module deliberately contains no product behaviour.  It names the extension contracts and
builds an immutable registry that later product packages can compose at process start.  A key has
exactly one owner: duplicate registration is an explicit configuration error, never an ordering
rule or a silent override.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from types import MappingProxyType
from typing import Any, Protocol

from rk.domain import Decision, TypedCommand, VerifiedCapability
from rk.projector import ProjectionContext

JsonObject = Mapping[str, Any]


class ExtensionConflict(ValueError):
    """Two product modules attempted to own the same extension key."""


class ExtensionNotRegistered(LookupError):
    """A caller attempted to dispatch through an extension key with no owner."""


@dataclass(frozen=True, slots=True)
class ProductCommandContext:
    """Authority-bearing inputs supplied to a registered product command handler."""

    run_id: str
    revision: int
    contract_version: int
    command: TypedCommand
    capability: VerifiedCapability
    snapshot: JsonObject
    evidence_summary: JsonObject


class ProductCommandHandler(Protocol):
    def __call__(self, context: ProductCommandContext) -> Decision: ...


class ProjectionMutationHandler(Protocol):
    def __call__(
        self,
        connection: sqlite3.Connection,
        context: ProjectionContext,
        mutation: JsonObject,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class ClosedRunPermission:
    """An exact CLOSED-run command/role grant; it grants no capability by itself."""

    command_type: str
    allowed_subject_roles: frozenset[str]

    def __post_init__(self) -> None:
        _require_key(self.command_type, "command_type")
        if not self.allowed_subject_roles or any(
            not role.strip() for role in self.allowed_subject_roles
        ):
            raise ValueError("allowed_subject_roles must contain non-empty role names")


@dataclass(frozen=True, slots=True)
class ProductActivity:
    """Structured activity input; cursor allocation belongs to the registered sink."""

    event_id: str
    scope_kind: str
    source: str
    recorded_at: str
    payload: JsonObject
    run_id: str | None = None
    deployment_id: str | None = None
    research_revision: int | None = None
    kernel_event_id: str | None = None
    entity_refs: JsonObject = field(default_factory=lambda: MappingProxyType({}))


class ProductActivityAppend(Protocol):
    """Append kernel activity inside the kernel's existing SQLite transaction."""

    def __call__(self, connection: sqlite3.Connection, activity: ProductActivity) -> int: ...


class ActivitySink(Protocol):
    """Append host/worker/tool activity in a sink-owned transaction."""

    def __call__(self, activity: ProductActivity) -> int: ...


@dataclass(frozen=True, slots=True)
class AuthorityInvalidation:
    kernel_event_id: str
    run_id: str
    research_revision: int
    intent: JsonObject


class InvalidationConsumer(Protocol):
    def __call__(self, invalidation: AuthorityInvalidation) -> None: ...


@dataclass(frozen=True, slots=True)
class ToolReceipt:
    """Execution receipt input; ``SUCCEEDED`` is not a mathematical verdict."""

    tool_run_id: str
    attempt_id: str
    status: str
    payload: JsonObject
    artifact_ids: tuple[str, ...] = ()


class ToolReceiptConsumer(Protocol):
    def __call__(self, receipt: ToolReceipt) -> None: ...


class PlacementProvider(Protocol):
    def __call__(self, request: JsonObject) -> JsonObject: ...


class LegacyWireDispatch(Protocol):
    """Translate one explicitly registered legacy wire variant to a typed command."""

    def __call__(self, value: JsonObject) -> TypedCommand: ...


def _require_key(key: str, label: str) -> None:
    if not key or key != key.strip():
        raise ValueError(f"{label} must be a non-empty, trimmed string")


def _frozen_add[HandlerT](
    current: Mapping[str, HandlerT], key: str, value: HandlerT, *, point: str
) -> Mapping[str, HandlerT]:
    _require_key(key, "extension key")
    if key in current:
        raise ExtensionConflict(f"duplicate {point} registration: {key}")
    return MappingProxyType({**current, key: value})


def _resolve[HandlerT](current: Mapping[str, HandlerT], key: str, *, point: str) -> HandlerT:
    try:
        return current[key]
    except KeyError as error:
        raise ExtensionNotRegistered(f"unregistered {point}: {key}") from error


@dataclass(frozen=True, slots=True)
class ExtensionRegistry:
    """Immutable, conflict-rejecting process-start registry for S00 extension points."""

    command_handlers: Mapping[str, ProductCommandHandler] = field(
        default_factory=lambda: MappingProxyType({})
    )
    projection_mutations: Mapping[str, ProjectionMutationHandler] = field(
        default_factory=lambda: MappingProxyType({})
    )
    closed_run_allowlist: Mapping[str, ClosedRunPermission] = field(
        default_factory=lambda: MappingProxyType({})
    )
    invalidation_consumers: Mapping[str, InvalidationConsumer] = field(
        default_factory=lambda: MappingProxyType({})
    )
    tool_receipt_consumers: Mapping[str, ToolReceiptConsumer] = field(
        default_factory=lambda: MappingProxyType({})
    )
    placement_providers: Mapping[str, PlacementProvider] = field(
        default_factory=lambda: MappingProxyType({})
    )
    legacy_wire_dispatches: Mapping[str, LegacyWireDispatch] = field(
        default_factory=lambda: MappingProxyType({})
    )
    product_activity_append: ProductActivityAppend | None = None
    activity_sink: ActivitySink | None = None

    def register_command_handler(
        self, command_type: str, handler: ProductCommandHandler
    ) -> ExtensionRegistry:
        return replace(
            self,
            command_handlers=_frozen_add(
                self.command_handlers, command_type, handler, point="product command handler"
            ),
        )

    def register_projection_mutation(
        self, opcode: str, handler: ProjectionMutationHandler
    ) -> ExtensionRegistry:
        return replace(
            self,
            projection_mutations=_frozen_add(
                self.projection_mutations, opcode, handler, point="projection mutation"
            ),
        )

    def register_closed_run_permission(self, permission: ClosedRunPermission) -> ExtensionRegistry:
        return replace(
            self,
            closed_run_allowlist=_frozen_add(
                self.closed_run_allowlist,
                permission.command_type,
                permission,
                point="CLOSED-run permission",
            ),
        )

    def register_product_activity_append(
        self, appender: ProductActivityAppend
    ) -> ExtensionRegistry:
        if self.product_activity_append is not None:
            raise ExtensionConflict("duplicate product activity append registration")
        return replace(self, product_activity_append=appender)

    def register_activity_sink(self, sink: ActivitySink) -> ExtensionRegistry:
        if self.activity_sink is not None:
            raise ExtensionConflict("duplicate activity sink registration")
        return replace(self, activity_sink=sink)

    def register_invalidation_consumer(
        self, consumer_id: str, consumer: InvalidationConsumer
    ) -> ExtensionRegistry:
        return replace(
            self,
            invalidation_consumers=_frozen_add(
                self.invalidation_consumers,
                consumer_id,
                consumer,
                point="invalidation consumer",
            ),
        )

    def register_tool_receipt_consumer(
        self, tool_profile: str, consumer: ToolReceiptConsumer
    ) -> ExtensionRegistry:
        return replace(
            self,
            tool_receipt_consumers=_frozen_add(
                self.tool_receipt_consumers,
                tool_profile,
                consumer,
                point="tool receipt consumer",
            ),
        )

    def register_placement_provider(
        self, placement_kind: str, provider: PlacementProvider
    ) -> ExtensionRegistry:
        return replace(
            self,
            placement_providers=_frozen_add(
                self.placement_providers,
                placement_kind,
                provider,
                point="placement provider",
            ),
        )

    def register_legacy_wire_dispatch(
        self, variant: str, dispatch: LegacyWireDispatch
    ) -> ExtensionRegistry:
        return replace(
            self,
            legacy_wire_dispatches=_frozen_add(
                self.legacy_wire_dispatches,
                variant,
                dispatch,
                point="legacy wire dispatch",
            ),
        )

    def handle_product_command(self, context: ProductCommandContext) -> Decision:
        return _resolve(
            self.command_handlers, context.command.type, point="product command handler"
        )(context)

    def apply_projection_mutation(
        self,
        connection: sqlite3.Connection,
        context: ProjectionContext,
        mutation: JsonObject,
    ) -> None:
        opcode = mutation.get("op")
        if not isinstance(opcode, str):
            raise ValueError("projection mutation requires a string op")
        _resolve(self.projection_mutations, opcode, point="projection mutation")(
            connection, context, mutation
        )

    def allows_closed_run_command(self, command_type: str, subject_role: str) -> bool:
        permission = self.closed_run_allowlist.get(command_type)
        return permission is not None and subject_role in permission.allowed_subject_roles

    def append_kernel_activity(
        self, connection: sqlite3.Connection, activity: ProductActivity
    ) -> int:
        if self.product_activity_append is None:
            raise ExtensionNotRegistered("product activity append is not registered")
        return self.product_activity_append(connection, activity)

    def append_host_activity(self, activity: ProductActivity) -> int:
        if self.activity_sink is None:
            raise ExtensionNotRegistered("activity sink is not registered")
        return self.activity_sink(activity)

    def consume_invalidation(self, consumer_id: str, invalidation: AuthorityInvalidation) -> None:
        _resolve(self.invalidation_consumers, consumer_id, point="invalidation consumer")(
            invalidation
        )

    def consume_tool_receipt(self, tool_profile: str, receipt: ToolReceipt) -> None:
        _resolve(self.tool_receipt_consumers, tool_profile, point="tool receipt consumer")(receipt)

    def place(self, placement_kind: str, request: JsonObject) -> JsonObject:
        return _resolve(self.placement_providers, placement_kind, point="placement provider")(
            request
        )

    def dispatch_legacy_wire(self, variant: str, value: JsonObject) -> TypedCommand:
        return _resolve(self.legacy_wire_dispatches, variant, point="legacy wire dispatch")(value)
