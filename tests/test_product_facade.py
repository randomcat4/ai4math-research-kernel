from __future__ import annotations

from collections.abc import Iterator
from dataclasses import dataclass
from types import MappingProxyType

import pytest

from rk.domain import CommandReceipt, RunHandle, VerifiedCapability
from rk.product.api import (
    ArtifactOperation,
    ArtifactResult,
    GlobalScope,
    ProductCommand,
    ProductReceipt,
    ProductSession,
    PublicActivity,
    QueryResult,
    QuerySpec,
    RunScope,
    SubscriptionSpec,
)
from rk.product.authority import ProductAuthority, core_kernel_bindings
from rk.product.facade import ResearchProductFacade


class Ports:
    def __init__(self) -> None:
        self.calls: list[str] = []

    def execute(self, session: ProductSession, value: object) -> object:
        self.calls.append(session.session_id)
        if isinstance(value, ProductCommand):
            return ProductReceipt(
                receipt_id="receipt-1",
                receipt_version=1,
                request_id=value.request_id,
                scope=value.scope,
                state="PENDING",
                updated_at="2026-08-13T00:00:00Z",
                job_id="job-1",
            )
        if isinstance(value, QuerySpec):
            return QueryResult("RUN", "run-1", MappingProxyType({}), MappingProxyType({}))
        if isinstance(value, ArtifactOperation):
            return ArtifactResult("COMMITTED", MappingProxyType({"artifact_id": "a1"}))
        raise AssertionError("unexpected port value")

    def open(self, session: ProductSession, spec: SubscriptionSpec) -> Stream:
        self.calls.append(session.session_id)
        return Stream(spec.after_cursor)


class Stream:
    def __init__(self, cursor: int) -> None:
        self.cursor = cursor
        self.closed = False

    def __iter__(self) -> Iterator[PublicActivity]:
        return iter(())

    def close(self) -> None:
        self.closed = True


def test_facade_exposes_exactly_four_public_operations() -> None:
    ports = Ports()
    facade = ResearchProductFacade(
        commands=ports, queries=ports, subscriptions=ports, artifacts=ports
    )
    session = ProductSession("s1", "subject-1", ("cap-1",))
    command = ProductCommand("request-1", GlobalScope("deployment-1", 0), "CREATE_RESEARCH")

    assert facade.command(session, command).request_id == "request-1"
    assert (
        facade.query(session, QuerySpec(MappingProxyType({}), "LIST_RESEARCH")).result_type == "RUN"
    )
    assert list(facade.subscribe(session, SubscriptionSpec(MappingProxyType({}), 0))) == []
    assert (
        facade.artifact(session, ArtifactOperation("COMMIT", MappingProxyType({}))).result_type
        == "COMMITTED"
    )
    assert ports.calls == ["s1", "s1", "s1", "s1"]
    assert {name for name in dir(facade) if not name.startswith("_")} == {
        "artifact",
        "command",
        "query",
        "subscribe",
    }


class KernelStub:
    def __init__(self) -> None:
        self.applied_type: str | None = None

    def create(self, request: object, capability: VerifiedCapability) -> RunHandle:
        assert capability.capability_id == "cap-1"
        return RunHandle("run-new", 0, "OPEN", 1, "2026-08-13T00:00:00Z")

    def apply(self, request: object, capability: VerifiedCapability) -> CommandReceipt:
        from rk.domain import ApplyRequest

        assert isinstance(request, ApplyRequest)
        assert capability.capability_id == "cap-1"
        self.applied_type = request.command.type
        return CommandReceipt(
            request.request_id,
            "command-1",
            request.run_id,
            True,
            request.expected_revision,
            request.expected_revision + 1,
            ("event-1",),
            (),
            None,
            (),
            "2026-08-13T00:00:00Z",
        )


@dataclass
class Capabilities:
    actions: list[str]

    def resolve(
        self,
        session: ProductSession,
        *,
        action: str,
        run_id: str | None,
    ) -> VerifiedCapability:
        assert session.principal_subject_id == "subject-1"
        self.actions.append(action)
        return VerifiedCapability(
            "cap-1",
            session.principal_subject_id,
            "issuer-1",
            frozenset({action}),
            frozenset({run_id or "*"}),
            "2026-08-12T00:00:00Z",
            "2026-08-14T00:00:00Z",
        )


def test_private_authority_maps_product_command_to_kernel_command() -> None:
    kernel = KernelStub()
    capabilities = Capabilities([])
    authority = ProductAuthority(kernel, capabilities, core_kernel_bindings())  # type: ignore[arg-type]
    session = ProductSession("s1", "subject-1", ("cap-1",))
    decision = authority.apply(
        session,
        ProductCommand(
            "request-1",
            RunScope("run-1", 3, 1),
            "CONFIRM_CONTRACT",
            MappingProxyType(
                {"contract_version": 1, "completeness_check_artifact_id": "artifact-1"}
            ),
        ),
    )

    assert decision.accepted is True
    assert kernel.applied_type == "FreezeContract"
    assert capabilities.actions == ["FreezeContract"]
    assert decision.kernel_receipts[0]["command_id"] == "command-1"


def test_private_authority_has_no_generic_kernel_command_escape() -> None:
    authority = ProductAuthority(
        KernelStub(),
        Capabilities([]),
        core_kernel_bindings(),  # type: ignore[arg-type]
    )
    session = ProductSession("s1", "subject-1", ("cap-1",))
    request = ProductCommand(
        "request-1",
        RunScope("run-1", 0, 1),
        "RUN_TOOL",
        MappingProxyType({"type": "PromoteClaim"}),
    )

    with pytest.raises(ValueError, match="no kernel binding"):
        authority.apply(session, request)


def test_binding_rejects_payload_fields_outside_normative_mapping() -> None:
    authority = ProductAuthority(
        KernelStub(),
        Capabilities([]),
        core_kernel_bindings(),  # type: ignore[arg-type]
    )
    session = ProductSession("s1", "subject-1", ("cap-1",))
    request = ProductCommand(
        "request-1",
        RunScope("run-1", 0, 1),
        "CONFIRM_CONTRACT",
        MappingProxyType(
            {
                "contract_version": 1,
                "completeness_check_artifact_id": "artifact-1",
                "capability": "forged",
            }
        ),
    )

    with pytest.raises(ValueError, match="extra=\\['capability'\\]"):
        authority.apply(session, request)
