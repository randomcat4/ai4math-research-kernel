"""Budget policy and ProductAuthority submission; the kernel remains the only ledger."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any, Protocol

from rk.product.api import ProductCommand, ProductDecision, ProductSession, RunScope, frozen_json
from rk.product.authority import KernelBinding, ProductAuthority, exact_payload


class BudgetControlError(RuntimeError):
    """A budget event, kernel receipt, or backpressure transition is invalid."""


class BudgetEventKind(StrEnum):
    RESERVATION = "RESERVATION"
    ACTUAL = "ACTUAL"
    REFUND = "REFUND"
    UNKNOWN_COST = "UNKNOWN_COST"
    FUSE_TRIP = "FUSE_TRIP"


@dataclass(frozen=True, slots=True)
class BudgetEvent:
    event_kind: BudgetEventKind
    resource_kind: str
    amount_microunits: int | None
    unit: str
    provider_usage: Mapping[str, Any]
    route_id: str | None = None
    attempt_id: str | None = None
    currency: str | None = None

    def __post_init__(self) -> None:
        if not self.resource_kind or not self.unit:
            raise ValueError("budget resource and unit are required")
        if self.event_kind is BudgetEventKind.UNKNOWN_COST:
            if self.amount_microunits is not None:
                raise ValueError("UNKNOWN_COST cannot invent an amount")
        elif (
            isinstance(self.amount_microunits, bool)
            or not isinstance(self.amount_microunits, int)
            or self.amount_microunits < 0
        ):
            raise ValueError("known budget events require non-negative microunits")
        component = self.provider_usage.get("component")
        if not isinstance(component, str) or not component.strip():
            raise ValueError("provider usage requires a component")
        if any(str(key).startswith("_rk_") for key in self.provider_usage):
            raise ValueError("provider usage cannot claim host trust fields")

    def payload(self) -> dict[str, Any]:
        value: dict[str, Any] = {
            "event_kind": self.event_kind.value,
            "resource_kind": self.resource_kind,
            "amount_microunits": self.amount_microunits,
            "unit": self.unit,
            "provider_usage": dict(self.provider_usage),
        }
        for key, item in (
            ("route_id", self.route_id),
            ("attempt_id", self.attempt_id),
            ("currency", self.currency),
        ):
            if item is not None:
                value[key] = item
        return value


@dataclass(frozen=True, slots=True)
class KernelBudgetFence:
    run_id: str
    revision: int
    contract_version: int


class MeasuredBudgetAuthority(Protocol):
    """ProductAuthority-owned host-receipt path for ACTUAL and UNKNOWN_COST."""

    def record_measured_budget(
        self,
        session: ProductSession,
        *,
        fence: KernelBudgetFence,
        request_id: str,
        event: BudgetEvent,
        host_receipt_artifact_id: str,
    ) -> ProductDecision: ...


class BudgetSubmission:
    """Submit policy events and measured receipts without persisting a product-side ledger."""

    def __init__(
        self,
        authority: ProductAuthority,
        measured_authority: MeasuredBudgetAuthority,
    ) -> None:
        self._authority = authority
        self._measured_authority = measured_authority

    def submit(
        self,
        session: ProductSession,
        *,
        fence: KernelBudgetFence,
        request_id: str,
        event: BudgetEvent,
        host_receipt_artifact_id: str | None = None,
    ) -> ProductDecision:
        if event.event_kind in {BudgetEventKind.ACTUAL, BudgetEventKind.UNKNOWN_COST}:
            if not host_receipt_artifact_id:
                raise BudgetControlError("measured usage requires a host execution receipt")
            decision = self._measured_authority.record_measured_budget(
                session,
                fence=fence,
                request_id=request_id,
                event=event,
                host_receipt_artifact_id=host_receipt_artifact_id,
            )
        else:
            if host_receipt_artifact_id is not None:
                raise BudgetControlError("policy budget events do not consume host receipts")
            decision = self._authority.apply(
                session,
                ProductCommand(
                    request_id=request_id,
                    scope=RunScope(fence.run_id, fence.revision, fence.contract_version),
                    command_type="RECORD_BUDGET",
                    payload=frozen_json(event.payload()),
                ),
            )
        if not decision.accepted:
            raise BudgetControlError(
                f"kernel rejected budget event: {decision.rejection_code or 'UNKNOWN'}"
            )
        return decision


def budget_kernel_binding() -> KernelBinding:
    return KernelBinding(
        "RecordBudget",
        exact_payload(
            ("event_kind", "resource_kind", "amount_microunits", "unit", "provider_usage"),
            ("route_id", "attempt_id", "currency"),
        ),
    )


@dataclass(frozen=True, slots=True)
class KernelBudgetSnapshot:
    remaining_microunits: Mapping[str, int]
    fuse_tripped: bool
    unknown_cost_components: frozenset[str]


@dataclass(frozen=True, slots=True)
class BudgetDemand:
    amounts_microunits: Mapping[str, int]
    component: str
    requires_known_cost: bool = False

    def __post_init__(self) -> None:
        if not self.component or any(
            not key
            or isinstance(value, bool)
            or not isinstance(value, int)
            or value < 0
            for key, value in self.amounts_microunits.items()
        ):
            raise ValueError("budget demand is invalid")


@dataclass(frozen=True, slots=True)
class BackpressureDecision:
    allowed: bool
    reason: str | None
    shortages: Mapping[str, int]


class BudgetBackpressure:
    """Read a kernel projection and decide admission; it owns no totals."""

    @staticmethod
    def evaluate(
        snapshot: KernelBudgetSnapshot, demand: BudgetDemand
    ) -> BackpressureDecision:
        if snapshot.fuse_tripped:
            return BackpressureDecision(False, "KERNEL_BUDGET_FUSE_TRIPPED", {})
        if demand.requires_known_cost and demand.component in snapshot.unknown_cost_components:
            return BackpressureDecision(False, "UNKNOWN_COST_REQUIRES_REVIEW", {})
        shortages = {
            resource: amount - int(snapshot.remaining_microunits.get(resource, 0))
            for resource, amount in demand.amounts_microunits.items()
            if amount > int(snapshot.remaining_microunits.get(resource, 0))
        }
        if shortages:
            return BackpressureDecision(False, "BUDGET_INSUFFICIENT", shortages)
        return BackpressureDecision(True, None, {})


__all__ = [
    "BackpressureDecision",
    "BudgetBackpressure",
    "BudgetControlError",
    "BudgetDemand",
    "BudgetEvent",
    "BudgetEventKind",
    "BudgetSubmission",
    "KernelBudgetFence",
    "KernelBudgetSnapshot",
    "MeasuredBudgetAuthority",
    "budget_kernel_binding",
]
