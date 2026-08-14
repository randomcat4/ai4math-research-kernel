from __future__ import annotations

import sqlite3
from collections.abc import Mapping
from dataclasses import replace
from datetime import UTC, datetime
from types import MappingProxyType
from typing import Any

import pytest

from rk.domain import Decision, TypedCommand, VerifiedCapability, frozen_mapping
from rk.extensions import (
    AuthorityInvalidation,
    ClosedRunPermission,
    ExtensionConflict,
    ExtensionNotRegistered,
    ExtensionRegistry,
    ProductActivity,
    ProductCommandContext,
    ToolReceipt,
)
from rk.guard import TransitionGuard
from rk.projector import ProjectionContext, ProjectionError, ProjectionWriter


class _Ids:
    def new(self) -> str:
        return "generated-id"


def _capability() -> VerifiedCapability:
    return VerifiedCapability(
        capability_id="cap-1",
        subject_id="subject-1",
        issuer="issuer-1",
        allowed_actions=frozenset({"*"}),
        run_scope=frozenset({"*"}),
        issued_at="2026-08-13T00:00:00Z",
        expires_at="2026-08-14T00:00:00Z",
    )


def _context(command_type: str) -> ProductCommandContext:
    return ProductCommandContext(
        run_id="run-1",
        revision=4,
        contract_version=2,
        command=TypedCommand(command_type, frozen_mapping({})),
        capability=_capability(),
        snapshot=MappingProxyType({}),
        evidence_summary=MappingProxyType({}),
    )


def test_two_product_handlers_register_and_dispatch_independently() -> None:
    calls: list[str] = []

    def alpha(context: ProductCommandContext) -> Decision:
        calls.append(context.command.type)
        return Decision(accepted=True)

    def beta(context: ProductCommandContext) -> Decision:
        calls.append(context.command.type)
        return Decision(accepted=True)

    registry = (
        ExtensionRegistry()
        .register_command_handler("Alpha", alpha)
        .register_command_handler("Beta", beta)
    )

    assert registry.handle_product_command(_context("Alpha")).accepted
    assert registry.handle_product_command(_context("Beta")).accepted
    assert calls == ["Alpha", "Beta"]


def test_duplicate_registration_is_rejected_without_overwriting_first_owner() -> None:
    def first(context: ProductCommandContext) -> Decision:
        del context
        return Decision(accepted=True)

    def second(context: ProductCommandContext) -> Decision:
        raise AssertionError(f"unexpected dispatch: {context.command.type}")

    registered = ExtensionRegistry().register_command_handler("Publish", first)

    with pytest.raises(ExtensionConflict, match="Publish"):
        registered.register_command_handler("Publish", second)

    assert registered.handle_product_command(_context("Publish")).accepted


def test_unknown_dispatch_is_explicit_and_registry_is_immutable() -> None:
    registry = ExtensionRegistry()

    with pytest.raises(ExtensionNotRegistered, match="Unknown"):
        registry.handle_product_command(_context("Unknown"))
    with pytest.raises(TypeError):
        registry.command_handlers["Unknown"] = lambda context: Decision(accepted=True)  # type: ignore[index]


def test_all_s00_extension_points_have_strict_dispatch_and_conflict_rules() -> None:
    observed: list[str] = []

    def mutation(
        connection: sqlite3.Connection,
        context: ProjectionContext,
        value: Mapping[str, Any],
    ) -> None:
        del connection, context
        observed.append(str(value["op"]))

    def kernel_activity(connection: sqlite3.Connection, activity: ProductActivity) -> int:
        del connection
        observed.append(activity.source)
        return 11

    def host_activity(activity: ProductActivity) -> int:
        observed.append(activity.source)
        return 12

    def invalidation(value: AuthorityInvalidation) -> None:
        observed.append(value.kernel_event_id)

    def tool_receipt(value: ToolReceipt) -> None:
        observed.append(value.tool_run_id)

    def placement(value: Mapping[str, Any]) -> Mapping[str, Any]:
        observed.append(str(value["component"]))
        return MappingProxyType({"device": "CPU"})

    def legacy(value: Mapping[str, Any]) -> TypedCommand:
        observed.append(str(value["operation"]))
        return TypedCommand("Imported", frozen_mapping({}))

    registry = (
        ExtensionRegistry()
        .register_projection_mutation("PRODUCT_APPEND", mutation)
        .register_closed_run_permission(
            ClosedRunPermission("GenerateCandidateTex", frozenset({"PUBLICATION_WORKER"}))
        )
        .register_product_activity_append(kernel_activity)
        .register_activity_sink(host_activity)
        .register_invalidation_consumer("execution", invalidation)
        .register_tool_receipt_consumer("lean", tool_receipt)
        .register_placement_provider("managed-python", placement)
        .register_legacy_wire_dispatch("legacy-import", legacy)
    )
    connection = sqlite3.connect(":memory:")
    projection_context = ProjectionContext(
        run_id="run-1",
        command_id="command-1",
        event_id="event-1",
        revision=5,
        contract_version=2,
        command=TypedCommand("Product", frozen_mapping({})),
        capability_id="cap-1",
        recorded_at="2026-08-13T00:00:00Z",
        artifacts_by_name=MappingProxyType({}),
        generated_artifact_ids=MappingProxyType({}),
    )
    activity = ProductActivity(
        event_id="activity-1",
        scope_kind="RUN",
        source="KERNEL",
        recorded_at="2026-08-13T00:00:00Z",
        payload=MappingProxyType({}),
        run_id="run-1",
        research_revision=5,
    )

    registry.apply_projection_mutation(
        connection, projection_context, MappingProxyType({"op": "PRODUCT_APPEND"})
    )
    assert registry.allows_closed_run_command("GenerateCandidateTex", "PUBLICATION_WORKER")
    assert not registry.allows_closed_run_command("GenerateCandidateTex", "MAIN")
    assert registry.append_kernel_activity(connection, activity) == 11
    assert registry.append_host_activity(activity) == 12
    registry.consume_invalidation(
        "execution", AuthorityInvalidation("kernel-1", "run-1", 5, MappingProxyType({}))
    )
    registry.consume_tool_receipt(
        "lean", ToolReceipt("tool-1", "attempt-1", "SUCCEEDED", MappingProxyType({}))
    )
    assert registry.place("managed-python", MappingProxyType({"component": "python"})) == {
        "device": "CPU"
    }
    assert (
        registry.dispatch_legacy_wire(
            "legacy-import", MappingProxyType({"operation": "apply"})
        ).type
        == "Imported"
    )
    assert observed == [
        "PRODUCT_APPEND",
        "KERNEL",
        "KERNEL",
        "kernel-1",
        "tool-1",
        "python",
        "apply",
    ]

    with pytest.raises(ExtensionConflict, match="PRODUCT_APPEND"):
        registry.register_projection_mutation("PRODUCT_APPEND", mutation)
    with pytest.raises(ExtensionConflict, match="activity sink"):
        registry.register_activity_sink(host_activity)


def test_missing_singleton_and_malformed_mutation_fail_before_business_work() -> None:
    registry = ExtensionRegistry()
    connection = sqlite3.connect(":memory:")
    activity = ProductActivity(
        event_id="activity-1",
        scope_kind="RUN",
        source="HOST",
        recorded_at="2026-08-13T00:00:00Z",
        payload=MappingProxyType({}),
        run_id="run-1",
    )

    with pytest.raises(ExtensionNotRegistered, match="activity sink"):
        registry.append_host_activity(activity)
    with pytest.raises(ValueError, match="string op"):
        registry.apply_projection_mutation(
            connection,
            ProjectionContext(
                run_id="run-1",
                command_id="command-1",
                event_id="event-1",
                revision=1,
                contract_version=1,
                command=TypedCommand("X", frozen_mapping({})),
                capability_id="cap-1",
                recorded_at="2026-08-13T00:00:00Z",
                artifacts_by_name=MappingProxyType({}),
                generated_artifact_ids=MappingProxyType({}),
            ),
            MappingProxyType({}),
        )


def test_guard_consumes_registered_command_after_generic_authority_gates() -> None:
    calls: list[str] = []

    def handler(context: ProductCommandContext) -> Decision:
        calls.append(context.run_id)
        return Decision(
            accepted=True,
            event_intents=(frozen_mapping({"type": "PRODUCT_APPLIED"}),),
            projection_mutations=(frozen_mapping({"op": "PRODUCT_APPEND"}),),
        )

    registry = ExtensionRegistry().register_command_handler("ProductApply", handler)
    guard = TransitionGuard(registry)
    snapshot = {
        "run_id": "run-1",
        "revision": 4,
        "current_contract_version": 2,
        "status": "RUNNING",
        "projection": {"contract_versions": {"2": {"status": "FROZEN"}}},
    }
    accepted = guard.decide(
        now_utc=datetime(2026, 8, 13, 1, tzinfo=UTC),
        snapshot=snapshot,
        command=TypedCommand("ProductApply", frozen_mapping({})),
        evidence_summary=frozen_mapping({}),
        capability=_capability(),
        policy_snapshot=frozen_mapping({}),
        expected_revision=4,
    )
    assert accepted.accepted
    assert calls == ["run-1"]

    conflict = guard.decide(
        now_utc=datetime(2026, 8, 13, 1, tzinfo=UTC),
        snapshot=snapshot,
        command=TypedCommand("ProductApply", frozen_mapping({})),
        evidence_summary=frozen_mapping({}),
        capability=_capability(),
        policy_snapshot=frozen_mapping({}),
        expected_revision=3,
    )
    assert not conflict.accepted
    assert conflict.rejection_code == "REVISION_CONFLICT"
    assert calls == ["run-1"]


def test_closed_product_command_requires_exact_registered_subject_role() -> None:
    def handler(context: ProductCommandContext) -> Decision:
        del context
        return Decision(accepted=True)

    registry = (
        ExtensionRegistry()
        .register_command_handler("GenerateCandidateTex", handler)
        .register_closed_run_permission(
            ClosedRunPermission("GenerateCandidateTex", frozenset({"PUBLICATION_WORKER"}))
        )
    )
    guard = TransitionGuard(registry)
    snapshot = {
        "run_id": "run-1",
        "revision": 4,
        "current_contract_version": 2,
        "status": "CLOSED",
        "projection": {},
    }
    arguments = {
        "now_utc": datetime(2026, 8, 13, 1, tzinfo=UTC),
        "snapshot": snapshot,
        "command": TypedCommand("GenerateCandidateTex", frozen_mapping({})),
        "evidence_summary": frozen_mapping({}),
        "capability": _capability(),
        "expected_revision": 4,
    }
    rejected = guard.decide(**arguments, policy_snapshot=frozen_mapping({}))
    assert rejected.rejection_code == "RUN_CLOSED"
    arguments["capability"] = replace(
        _capability(), subject_role="PUBLICATION_WORKER"
    )
    allowed = guard.decide(**arguments, policy_snapshot=frozen_mapping({}))
    assert allowed.accepted


def test_projection_writer_dispatches_only_registered_unknown_opcodes() -> None:
    observed: list[str] = []

    def mutation(
        connection: sqlite3.Connection,
        context: ProjectionContext,
        value: Mapping[str, Any],
    ) -> None:
        del connection, context
        observed.append(str(value["op"]))

    registry = ExtensionRegistry().register_projection_mutation("PRODUCT_APPEND", mutation)
    writer = ProjectionWriter(_Ids(), registry)
    context = ProjectionContext(
        run_id="run-1",
        command_id="command-1",
        event_id="event-1",
        revision=5,
        contract_version=2,
        command=TypedCommand("Product", frozen_mapping({})),
        capability_id="cap-1",
        recorded_at="2026-08-13T00:00:00Z",
        artifacts_by_name=MappingProxyType({}),
        generated_artifact_ids=MappingProxyType({}),
    )
    connection = sqlite3.connect(":memory:")
    mutation_value = frozen_mapping({"op": "PRODUCT_APPEND"})
    assert writer.supports((mutation_value,))
    writer.apply(connection, context, (mutation_value,))
    assert observed == ["PRODUCT_APPEND"]
    unknown = frozen_mapping({"op": "UNREGISTERED"})
    assert not writer.supports((unknown,))
    with pytest.raises(ProjectionError, match="UNREGISTERED"):
        writer.apply(connection, context, (unknown,))
