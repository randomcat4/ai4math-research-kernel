"""Injected hardware placement and durable, budget-aware concurrent work scheduling."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from rk.extensions import ExtensionRegistry
from rk.product.budgeting import BackpressureDecision
from rk.product.deployment import DeploymentHealthReport, ProbeStatus
from rk.wire import canonical_json_bytes


class PlacementError(RuntimeError):
    """Hardware cannot satisfy the exact quality or resource contract."""


class ExecutorKind(StrEnum):
    CPU = "CPU"
    NVIDIA = "NVIDIA"
    ROCM = "ROCM"
    API = "API"


class TargetAvailability(StrEnum):
    AVAILABLE = "AVAILABLE"
    UNAVAILABLE = "UNAVAILABLE"
    UNCONFIGURED = "UNCONFIGURED"


@dataclass(frozen=True, slots=True)
class ExecutionTarget:
    target_id: str
    kind: ExecutorKind
    concurrency_group: str
    capacity: int
    memory_bytes: int
    provider: str | None = None
    availability: TargetAvailability = TargetAvailability.UNCONFIGURED
    probe_receipt_id: str | None = None
    availability_fault: str | None = "NOT_CONFIGURED"
    assets: frozenset[str] = frozenset()

    def __post_init__(self) -> None:
        if (
            not self.target_id
            or not self.concurrency_group
            or self.capacity <= 0
            or self.memory_bytes < 0
        ):
            raise ValueError("execution target is invalid")
        if self.kind is ExecutorKind.API and not self.provider:
            raise ValueError("API target requires a provider")
        if self.availability is TargetAvailability.AVAILABLE:
            if not self.probe_receipt_id or self.availability_fault is not None:
                raise ValueError("available target requires a clean current probe receipt")
        elif not self.availability_fault:
            raise ValueError("unavailable target requires an explicit fault")

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_id": self.target_id,
            "kind": self.kind.value,
            "concurrency_group": self.concurrency_group,
            "capacity": self.capacity,
            "memory_bytes": self.memory_bytes,
            "provider": self.provider,
            "assets": sorted(self.assets),
            "availability": self.availability.value,
            "probe_receipt_id": self.probe_receipt_id,
            "availability_fault": self.availability_fault,
        }


def target_from_deployment_probe(
    report: DeploymentHealthReport,
    *,
    capability_key: str,
    target_id: str,
    kind: ExecutorKind,
    concurrency_group: str,
    capacity: int,
    memory_bytes: int,
    provider: str | None = None,
    assets: frozenset[str] = frozenset(),
) -> ExecutionTarget:
    try:
        result = next(item for item in report.results if item.capability_key == capability_key)
    except StopIteration as error:
        raise PlacementError(f"deployment probe omitted capability {capability_key}") from error
    status = {
        ProbeStatus.AVAILABLE: TargetAvailability.AVAILABLE,
        ProbeStatus.UNCONFIGURED: TargetAvailability.UNCONFIGURED,
        ProbeStatus.UNAVAILABLE: TargetAvailability.UNAVAILABLE,
        ProbeStatus.DEGRADED: TargetAvailability.UNAVAILABLE,
    }[result.status]
    return ExecutionTarget(
        target_id=target_id,
        kind=kind,
        concurrency_group=concurrency_group,
        capacity=capacity,
        memory_bytes=memory_bytes,
        provider=provider,
        availability=status,
        probe_receipt_id=report.probe_run_id,
        availability_fault=None if status is TargetAvailability.AVAILABLE else result.fault_code,
        assets=assets,
    )


@dataclass(frozen=True, slots=True)
class HardwareProfile:
    profile_id: str
    os_family: str
    system_memory_bytes: int
    targets: tuple[ExecutionTarget, ...]

    def __post_init__(self) -> None:
        if not self.profile_id or not self.os_family or self.system_memory_bytes <= 0:
            raise ValueError("hardware profile is invalid")
        target_ids = [target.target_id for target in self.targets]
        if not self.targets or len(target_ids) != len(set(target_ids)):
            raise ValueError("hardware targets must be present and unique")

    def to_dict(self) -> dict[str, Any]:
        return {
            "profile_id": self.profile_id,
            "os_family": self.os_family,
            "system_memory_bytes": self.system_memory_bytes,
            "targets": [target.to_dict() for target in self.targets],
        }


@dataclass(frozen=True, slots=True)
class WorkRequirement:
    work_item_id: str
    component: str
    allowed_kinds: tuple[ExecutorKind, ...]
    memory_bytes: int
    required_asset: str | None
    required_provider: str | None
    retrieval_top_k: int
    rerank_required: bool
    verifier_required: bool

    def __post_init__(self) -> None:
        if (
            not self.work_item_id
            or not self.component
            or not self.allowed_kinds
            or self.memory_bytes < 0
            or self.retrieval_top_k <= 0
        ):
            raise ValueError("work requirement is invalid")

    def to_dict(self) -> dict[str, Any]:
        return {
            "work_item_id": self.work_item_id,
            "component": self.component,
            "allowed_kinds": [kind.value for kind in self.allowed_kinds],
            "memory_bytes": self.memory_bytes,
            "required_asset": self.required_asset,
            "required_provider": self.required_provider,
            "retrieval_top_k": self.retrieval_top_k,
            "rerank_required": self.rerank_required,
            "verifier_required": self.verifier_required,
        }


@dataclass(frozen=True, slots=True)
class PlacementDecision:
    work_item_id: str
    target_id: str
    kind: ExecutorKind
    concurrency_group: str
    group_capacity: int
    retrieval_top_k: int
    rerank_required: bool
    verifier_required: bool
    fallback_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "work_item_id": self.work_item_id,
            "target_id": self.target_id,
            "kind": self.kind.value,
            "concurrency_group": self.concurrency_group,
            "group_capacity": self.group_capacity,
            "retrieval_top_k": self.retrieval_top_k,
            "rerank_required": self.rerank_required,
            "verifier_required": self.verifier_required,
            "fallback_reason": self.fallback_reason,
        }


class PlacementPlanner:
    """Select only an injected target that preserves every requested quality field."""

    def __init__(self, hardware: HardwareProfile) -> None:
        self.hardware = hardware

    def place(self, requirement: WorkRequirement) -> PlacementDecision:
        for kind in requirement.allowed_kinds:
            for target in self.hardware.targets:
                if target.availability is not TargetAvailability.AVAILABLE:
                    continue
                if target.kind is not kind or target.memory_bytes < requirement.memory_bytes:
                    continue
                if requirement.required_asset and requirement.required_asset not in target.assets:
                    continue
                if (
                    requirement.required_provider
                    and target.provider != requirement.required_provider
                ):
                    continue
                if requirement.rerank_required and "reranker" not in target.assets:
                    continue
                if requirement.verifier_required and "verifier" not in target.assets:
                    continue
                return PlacementDecision(
                    requirement.work_item_id,
                    target.target_id,
                    target.kind,
                    target.concurrency_group,
                    target.capacity,
                    requirement.retrieval_top_k,
                    requirement.rerank_required,
                    requirement.verifier_required,
                )
        raise PlacementError(
            f"no injected target satisfies exact requirement for {requirement.work_item_id}"
        )

    def provider(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        requirement = requirement_from_mapping(request)
        return self.place(requirement).to_dict()

    def register(self, registry: ExtensionRegistry) -> ExtensionRegistry:
        return registry.register_placement_provider("b13-research", self.provider)


class ExecutionOutcome(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class PlacementExecutionReceipt:
    work_item_id: str
    target_id: str
    outcome: ExecutionOutcome
    started_at: str
    finished_at: str
    started_monotonic_ns: int
    finished_monotonic_ns: int
    exit_code: int | None
    failure_code: str | None
    receipt_artifact_id: str

    def __post_init__(self) -> None:
        if (
            not self.work_item_id
            or not self.target_id
            or not self.started_at
            or not self.finished_at
            or not self.receipt_artifact_id
            or self.started_monotonic_ns < 0
            or self.finished_monotonic_ns < self.started_monotonic_ns
        ):
            raise ValueError("execution receipt identity or interval is invalid")
        if self.outcome is ExecutionOutcome.SUCCEEDED:
            if self.exit_code != 0 or self.failure_code is not None:
                raise ValueError("success requires exit zero and no failure")
        elif not self.failure_code:
            raise ValueError("failed execution requires a failure code")

    def to_dict(self) -> dict[str, Any]:
        return {
            "work_item_id": self.work_item_id,
            "target_id": self.target_id,
            "outcome": self.outcome.value,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "started_monotonic_ns": self.started_monotonic_ns,
            "finished_monotonic_ns": self.finished_monotonic_ns,
            "exit_code": self.exit_code,
            "failure_code": self.failure_code,
            "receipt_artifact_id": self.receipt_artifact_id,
        }


def execution_intervals_overlap(
    left: PlacementExecutionReceipt, right: PlacementExecutionReceipt
) -> bool:
    return max(left.started_monotonic_ns, right.started_monotonic_ns) <= min(
        left.finished_monotonic_ns, right.finished_monotonic_ns
    )


@dataclass(frozen=True, slots=True)
class ScheduledWork:
    work_item_id: str
    stable_ordinal: int
    state: str
    promotion_state: str
    placement: PlacementDecision
    started_at: str | None
    finished_at: str | None
    failure_code: str | None
    execution_receipt: PlacementExecutionReceipt | None


class ProductSchedulingStore:
    """Persist queue/control state; budget totals remain absent by construction."""

    def __init__(self, db_path: Path, *, busy_timeout_ms: int = 5_000) -> None:
        self._db_path = Path(db_path)
        self._busy_timeout_ms = busy_timeout_ms

    def create_plan(
        self,
        *,
        schedule_plan_id: str,
        run_id: str,
        hardware: HardwareProfile,
        requirements: Sequence[WorkRequirement],
        planner: PlacementPlanner,
        created_at: str,
    ) -> str:
        if not requirements:
            raise ValueError("schedule plan requires work")
        placements = tuple(planner.place(requirement) for requirement in requirements)
        quality = {
            "items": [
                {
                    "work_item_id": item.work_item_id,
                    "retrieval_top_k": item.retrieval_top_k,
                    "rerank_required": item.rerank_required,
                    "verifier_required": item.verifier_required,
                }
                for item in requirements
            ]
        }
        digest = hashlib.sha256(
            canonical_json_bytes(
                {
                    "run_id": run_id,
                    "hardware": hardware.to_dict(),
                    "requirements": [item.to_dict() for item in requirements],
                    "placements": [item.to_dict() for item in placements],
                }
            )
        ).hexdigest()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT run_id,plan_digest FROM product_schedule_plans "
                "WHERE schedule_plan_id=?",
                (schedule_plan_id,),
            ).fetchone()
            if existing is not None:
                if tuple(str(value) for value in existing) != (run_id, digest):
                    raise PlacementError("schedule plan identity was reused")
                connection.commit()
                return digest
            connection.execute(
                "INSERT INTO product_schedule_plans("
                "schedule_plan_id,run_id,hardware_profile_json,quality_contract_json,"
                "plan_digest,state,created_at,updated_at) VALUES(?,?,?,?,?,'READY',?,?)",
                (
                    schedule_plan_id,
                    run_id,
                    _json(hardware.to_dict()),
                    _json(quality),
                    digest,
                    created_at,
                    created_at,
                ),
            )
            connection.executemany(
                "INSERT INTO product_scheduled_work("
                "work_item_id,schedule_plan_id,stable_ordinal,requirement_json,placement_json,"
                "concurrency_group,group_capacity,state,promotion_state) "
                "VALUES(?,?,?,?,?,?,?,'QUEUED','WAITING')",
                [
                    (
                        requirement.work_item_id,
                        schedule_plan_id,
                        ordinal,
                        _json(requirement.to_dict()),
                        _json(placement.to_dict()),
                        placement.concurrency_group,
                        placement.group_capacity,
                    )
                    for ordinal, (requirement, placement) in enumerate(
                        zip(requirements, placements, strict=True), start=1
                    )
                ],
            )
            connection.commit()
        return digest

    def start(self, schedule_plan_id: str, *, now: str) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                "UPDATE product_schedule_plans SET state='RUNNING',updated_at=? "
                "WHERE schedule_plan_id=? AND state='READY'",
                (now, schedule_plan_id),
            ).rowcount
            if changed != 1:
                raise PlacementError("only a ready schedule can start")
            connection.commit()

    def apply_backpressure(
        self, schedule_plan_id: str, decision: BackpressureDecision, *, now: str
    ) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT state FROM product_schedule_plans WHERE schedule_plan_id=?",
                (schedule_plan_id,),
            ).fetchone()
            if row is None:
                raise KeyError(schedule_plan_id)
            state = str(row[0])
            if decision.allowed:
                if state == "BUDGET_PAUSED":
                    connection.execute(
                        "UPDATE product_schedule_plans SET state='RUNNING',pause_reason=NULL,"
                        "updated_at=? WHERE schedule_plan_id=?",
                        (now, schedule_plan_id),
                    )
                    connection.execute(
                        "UPDATE product_scheduled_work SET state='QUEUED' "
                        "WHERE schedule_plan_id=? AND state='BUDGET_PAUSED'",
                        (schedule_plan_id,),
                    )
            elif state == "RUNNING":
                connection.execute(
                    "UPDATE product_schedule_plans SET state='BUDGET_PAUSED',pause_reason=?,"
                    "updated_at=? WHERE schedule_plan_id=?",
                    (decision.reason, now, schedule_plan_id),
                )
                connection.execute(
                    "UPDATE product_scheduled_work SET state='BUDGET_PAUSED' "
                    "WHERE schedule_plan_id=? AND state='QUEUED'",
                    (schedule_plan_id,),
                )
            connection.commit()

    def claim_ready(self, schedule_plan_id: str, *, started_at: str) -> tuple[ScheduledWork, ...]:
        claimed: list[str] = []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            state = connection.execute(
                "SELECT state FROM product_schedule_plans WHERE schedule_plan_id=?",
                (schedule_plan_id,),
            ).fetchone()
            if state is None:
                raise KeyError(schedule_plan_id)
            if str(state[0]) != "RUNNING":
                connection.commit()
                return ()
            running = {
                str(row[0]): int(row[1])
                for row in connection.execute(
                    "SELECT concurrency_group,COUNT(*) FROM product_scheduled_work "
                    "WHERE schedule_plan_id=? AND state='RUNNING' GROUP BY concurrency_group",
                    (schedule_plan_id,),
                )
            }
            rows = connection.execute(
                "SELECT work_item_id,concurrency_group,group_capacity "
                "FROM product_scheduled_work WHERE schedule_plan_id=? AND state='QUEUED' "
                "ORDER BY stable_ordinal",
                (schedule_plan_id,),
            ).fetchall()
            for row in rows:
                work_item_id, group, capacity = str(row[0]), str(row[1]), int(row[2])
                if running.get(group, 0) >= capacity:
                    continue
                connection.execute(
                    "UPDATE product_scheduled_work SET state='RUNNING',started_at=? "
                    "WHERE work_item_id=? AND state='QUEUED'",
                    (started_at, work_item_id),
                )
                running[group] = running.get(group, 0) + 1
                claimed.append(work_item_id)
            connection.commit()
        return tuple(self.get(work_item_id) for work_item_id in claimed)

    def finish(
        self,
        receipt: PlacementExecutionReceipt,
    ) -> ScheduledWork:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT placement_json FROM product_scheduled_work "
                "WHERE work_item_id=? AND state='RUNNING'",
                (receipt.work_item_id,),
            ).fetchone()
            if row is None:
                raise PlacementError("only running work can finish")
            placement = placement_from_mapping(_object(row[0]))
            if placement.target_id != receipt.target_id:
                raise PlacementError("execution receipt target does not match placement")
            changed = connection.execute(
                "UPDATE product_scheduled_work SET state=?,finished_at=?,failure_code=?,"
                "promotion_state=?,execution_receipt_json=?,"
                "execution_started_monotonic_ns=?,execution_finished_monotonic_ns=? "
                "WHERE work_item_id=? AND state='RUNNING'",
                (
                    receipt.outcome.value,
                    receipt.finished_at,
                    receipt.failure_code,
                    "WAITING" if receipt.outcome is ExecutionOutcome.SUCCEEDED else "NOT_ELIGIBLE",
                    _json(receipt.to_dict()),
                    receipt.started_monotonic_ns,
                    receipt.finished_monotonic_ns,
                    receipt.work_item_id,
                ),
            ).rowcount
            if changed != 1:
                raise PlacementError("only running work can finish")
            connection.commit()
        return self.get(receipt.work_item_id)

    def claim_next_promotion(
        self, schedule_plan_id: str, *, claimed_at: str
    ) -> ScheduledWork | None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT work_item_id,state,promotion_state FROM product_scheduled_work "
                "WHERE schedule_plan_id=? AND promotion_state IN ('WAITING','CLAIMED') "
                "ORDER BY stable_ordinal LIMIT 1",
                (schedule_plan_id,),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            if (str(row[1]), str(row[2])) != ("SUCCEEDED", "WAITING"):
                connection.commit()
                return None
            work_item_id = str(row[0])
            connection.execute(
                "UPDATE product_scheduled_work SET promotion_state='CLAIMED',"
                "promotion_claimed_at=? WHERE work_item_id=?",
                (claimed_at, work_item_id),
            )
            connection.commit()
        return self.get(work_item_id)

    def mark_promoted(self, work_item_id: str, *, promoted_at: str) -> ScheduledWork:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                "UPDATE product_scheduled_work SET promotion_state='PROMOTED',promoted_at=? "
                "WHERE work_item_id=? AND promotion_state='CLAIMED'",
                (promoted_at, work_item_id),
            ).rowcount
            if changed != 1:
                raise PlacementError("promotion must be claimed serially")
            connection.commit()
        return self.get(work_item_id)

    def get(self, work_item_id: str) -> ScheduledWork:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT work_item_id,stable_ordinal,state,promotion_state,placement_json,"
                "started_at,finished_at,failure_code,execution_receipt_json "
                "FROM product_scheduled_work "
                "WHERE work_item_id=?",
                (work_item_id,),
            ).fetchone()
        if row is None:
            raise KeyError(work_item_id)
        return ScheduledWork(
            work_item_id=str(row[0]),
            stable_ordinal=int(row[1]),
            state=str(row[2]),
            promotion_state=str(row[3]),
            placement=placement_from_mapping(_object(row[4])),
            started_at=str(row[5]) if row[5] is not None else None,
            finished_at=str(row[6]) if row[6] is not None else None,
            failure_code=str(row[7]) if row[7] is not None else None,
            execution_receipt=receipt_from_mapping(_object(row[8]))
            if row[8] is not None
            else None,
        )

    def nonterminal_count(self, schedule_plan_id: str) -> int:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COUNT(*) FROM product_scheduled_work WHERE schedule_plan_id=? "
                "AND state IN ('QUEUED','RUNNING','BUDGET_PAUSED')",
                (schedule_plan_id,),
            ).fetchone()
        return int(row[0]) if row is not None else 0

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self._db_path,
            timeout=self._busy_timeout_ms / 1_000,
            isolation_level=None,
        )
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms}")
        return connection


def requirement_from_mapping(value: Mapping[str, Any]) -> WorkRequirement:
    required = {
        "work_item_id",
        "component",
        "allowed_kinds",
        "memory_bytes",
        "required_asset",
        "required_provider",
        "retrieval_top_k",
        "rerank_required",
        "verifier_required",
    }
    if set(value) != required or not isinstance(value["allowed_kinds"], list):
        raise ValueError("placement request fields are not exact")
    return WorkRequirement(
        work_item_id=str(value["work_item_id"]),
        component=str(value["component"]),
        allowed_kinds=tuple(ExecutorKind(str(item)) for item in value["allowed_kinds"]),
        memory_bytes=_integer(value["memory_bytes"]),
        required_asset=_optional(value["required_asset"]),
        required_provider=_optional(value["required_provider"]),
        retrieval_top_k=_integer(value["retrieval_top_k"]),
        rerank_required=_boolean(value["rerank_required"]),
        verifier_required=_boolean(value["verifier_required"]),
    )


def placement_from_mapping(value: Mapping[str, Any]) -> PlacementDecision:
    return PlacementDecision(
        work_item_id=str(value["work_item_id"]),
        target_id=str(value["target_id"]),
        kind=ExecutorKind(str(value["kind"])),
        concurrency_group=str(value["concurrency_group"]),
        group_capacity=_integer(value["group_capacity"]),
        retrieval_top_k=_integer(value["retrieval_top_k"]),
        rerank_required=_boolean(value["rerank_required"]),
        verifier_required=_boolean(value["verifier_required"]),
        fallback_reason=_optional(value["fallback_reason"]),
    )


def receipt_from_mapping(value: Mapping[str, Any]) -> PlacementExecutionReceipt:
    return PlacementExecutionReceipt(
        work_item_id=str(value["work_item_id"]),
        target_id=str(value["target_id"]),
        outcome=ExecutionOutcome(str(value["outcome"])),
        started_at=str(value["started_at"]),
        finished_at=str(value["finished_at"]),
        started_monotonic_ns=_integer(value["started_monotonic_ns"]),
        finished_monotonic_ns=_integer(value["finished_monotonic_ns"]),
        exit_code=_optional_integer(value["exit_code"]),
        failure_code=_optional(value["failure_code"]),
        receipt_artifact_id=str(value["receipt_artifact_id"]),
    )


def _integer(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError("placement integer field is invalid")
    return int(value)


def _optional_integer(value: Any) -> int | None:
    if value is None:
        return None
    return _integer(value)


def _boolean(value: Any) -> bool:
    if not isinstance(value, bool):
        raise ValueError("placement boolean field is invalid")
    return value


def _optional(value: Any) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value:
        raise ValueError("optional placement identity is invalid")
    return value


def _object(value: Any) -> dict[str, Any]:
    parsed = json.loads(str(value))
    if not isinstance(parsed, dict):
        raise PlacementError("stored scheduling JSON is not an object")
    return parsed


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


__all__ = [
    "ExecutionOutcome",
    "ExecutionTarget",
    "ExecutorKind",
    "HardwareProfile",
    "PlacementDecision",
    "PlacementError",
    "PlacementExecutionReceipt",
    "PlacementPlanner",
    "ProductSchedulingStore",
    "ScheduledWork",
    "TargetAvailability",
    "WorkRequirement",
    "execution_intervals_overlap",
    "placement_from_mapping",
    "receipt_from_mapping",
    "requirement_from_mapping",
    "target_from_deployment_probe",
]
