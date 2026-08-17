from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from rk.product.activity_store import ActivityStore
from rk.product.orchestrator_route_control import RouteControlledWorkDeriver
from rk.product.route_plan import (
    RouteDerivationStopped,
    RoutePlanCASMismatch,
    RoutePlanConflict,
    RoutePlanError,
    RoutePlanProposal,
    RoutePlanStore,
    RouteProposal,
)
from rk.product.work_activity import WorkActivityStore
from rk.product_migrations import ProductMigrationAssembler, ProductMigrationRegistry


class Ids:
    def __init__(self) -> None:
        self.value = 0

    def __call__(self) -> str:
        self.value += 1
        return f"route-generated-{self.value:04d}"


class Clock:
    def __init__(self) -> None:
        self.value = 0

    def __call__(self) -> str:
        self.value += 1
        return f"2026-08-13T01:00:{self.value:02d}Z"


def database(tmp_path: Path) -> Path:
    path = tmp_path / "route-plan.sqlite"
    with sqlite3.connect(path, isolation_level=None) as connection:
        connection.execute(
            "CREATE TABLE runs(run_id TEXT PRIMARY KEY,revision INTEGER NOT NULL,"
            "current_contract_version INTEGER NOT NULL) STRICT"
        )
        connection.execute("INSERT INTO runs VALUES('run-1',7,3)")
        ProductMigrationAssembler(ProductMigrationRegistry(Path("schema_fragments"))).apply(
            connection
        )
    return path


def services(
    path: Path,
) -> tuple[RoutePlanStore, RouteControlledWorkDeriver, ActivityStore]:
    ids = Ids()
    clock = Clock()
    activities = ActivityStore(path)
    plans = RoutePlanStore(
        db_path=path,
        activities=activities,
        id_generator=ids,
        clock=clock,
    )
    work = WorkActivityStore(
        db_path=path,
        activities=activities,
        id_generator=ids,
        clock=clock,
    )
    return (
        plans,
        RouteControlledWorkDeriver(
            db_path=path,
            route_plans=plans,
            work_activity=work,
            activities=activities,
            id_generator=ids,
            clock=clock,
        ),
        activities,
    )


def proposal(plan_id: str, route_id: str, method: str, priority: int = 1) -> RoutePlanProposal:
    return RoutePlanProposal(
        route_plan_id=plan_id,
        run_id="run-1",
        research_revision=7,
        contract_version=3,
        routes=(
            RouteProposal(
                route_id=route_id,
                method=method,
                target=f"target for {method}",
                expected_verifier=f"verifier for {method}",
                milestones=("death test", "candidate certificate", "source-side review"),
                termination_condition="death test fails or budget is exhausted",
                dependencies=("contract-v3",),
                priority=priority,
                budget={"wall_seconds": 600, "output_tokens": 12_000},
            ),
        ),
    )


def apply(
    plans: RoutePlanStore,
    item: RoutePlanProposal,
    action: str,
    request: str,
    **values: object,
):
    return plans.apply(
        run_id="run-1",
        request_id=request,
        expected_revision=7,
        contract_version=3,
        action=action,
        route_plan_id=item.route_plan_id,
        **values,
    )


def activate(plans: RoutePlanStore, item: RoutePlanProposal, prefix: str) -> None:
    plans.register_proposal(item)
    apply(plans, item, "APPROVE", f"{prefix}-approve", plan_digest=item.digest)
    apply(plans, item, "START", f"{prefix}-start")


def test_three_structurally_distinct_routes_require_formal_approval_and_start(
    tmp_path: Path,
) -> None:
    path = database(tmp_path)
    plans, _deriver, activities = services(path)
    proposals = (
        proposal("plan-direct", "route-direct", "direct symbolic attack", 1),
        proposal("plan-near", "route-near", "near-domain algebraic transfer", 2),
        proposal("plan-far", "route-far", "far-domain topological bridge", 3),
    )

    for index, item in enumerate(proposals):
        registered = plans.register_proposal(item)
        assert registered.state == "PROPOSED"
        approved = apply(
            plans,
            item,
            "APPROVE",
            f"request-{index}-approve",
            plan_digest=item.digest,
        )
        assert approved.plan.state == "APPROVED"
        started = apply(plans, item, "START", f"request-{index}-start")
        assert started.plan.state == "ACTIVE"
        assert started.plan.routes[0].state == "ACTIVE"

    records = activities.snapshot(run_id="run-1", limit=1000).records
    assert [record.cursor for record in records] == list(range(1, 10))
    assert [record.payload["type"] for record in records].count("ROUTE_PLAN_APPROVED") == 3
    assert [record.payload["type"] for record in records].count("ROUTE_PLAN_STARTED") == 3


def test_request_digest_replay_and_revision_contract_cas(tmp_path: Path) -> None:
    path = database(tmp_path)
    plans, _deriver, activities = services(path)
    item = proposal("plan-direct", "route-direct", "direct")
    plans.register_proposal(item)
    first = apply(
        plans,
        item,
        "APPROVE",
        "same-request",
        plan_digest=item.digest,
    )
    second = apply(
        plans,
        item,
        "APPROVE",
        "same-request",
        plan_digest=item.digest,
    )
    assert not first.replayed
    assert second.replayed
    assert first.request_digest == second.request_digest
    assert len(activities.snapshot(run_id="run-1").records) == 2

    with pytest.raises(RoutePlanConflict, match="different content"):
        apply(plans, item, "START", "same-request")
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE runs SET revision=8 WHERE run_id='run-1'")
    with pytest.raises(RoutePlanCASMismatch, match="revision 8"):
        apply(plans, item, "START", "stale-revision")
    assert plans.get(item.route_plan_id).state == "APPROVED"


def test_digest_transition_priority_and_budget_are_explicit(tmp_path: Path) -> None:
    plans, _deriver, _activities = services(database(tmp_path))
    item = proposal("plan-priority", "route-priority", "enumeration")
    plans.register_proposal(item)
    with pytest.raises(RoutePlanConflict, match="digest"):
        apply(
            plans,
            item,
            "APPROVE",
            "bad-digest",
            plan_digest="0" * 64,
        )
    apply(plans, item, "APPROVE", "approve", plan_digest=item.digest)
    priority = apply(
        plans,
        item,
        "SET_PRIORITY",
        "priority",
        priority=11,
    ).plan
    budget = apply(
        plans,
        item,
        "SET_BUDGET",
        "budget",
        budget={"wall_seconds": 90, "tool_calls": 14},
    ).plan
    assert priority.routes[0].priority == 11
    assert budget.routes[0].budget == {"tool_calls": 14, "wall_seconds": 90}
    with pytest.raises(ValueError, match="missing required"):
        apply(plans, item, "STOP", "stop-without-reason")


def test_stop_blocks_new_work_but_not_other_active_routes_or_existing_work(
    tmp_path: Path,
) -> None:
    path = database(tmp_path)
    plans, deriver, activities = services(path)
    stopped = proposal("plan-stopped", "route-stopped", "direct")
    continuing = proposal("plan-continuing", "route-continuing", "near")
    activate(plans, stopped, "stopped")
    activate(plans, continuing, "continuing")

    existing = deriver.derive_work_item(
        run_id="run-1",
        route_id="route-stopped",
        logical_key="route-stopped:first",
        work_kind="DEVELOP_ROUTE",
        assignment_summary="first assignment",
        research_revision=7,
    )
    apply(plans, stopped, "STOP", "stop-route", reason="death test falsified mapping")
    plans.record_hint(
        hint_id="hint-after-stop",
        run_id="run-1",
        route_plan_id="plan-stopped",
        content_artifact_id="artifact-hint",
        research_revision=7,
        contract_version=3,
    )
    with pytest.raises(RouteDerivationStopped, match="not active"):
        deriver.derive_work_item(
            run_id="run-1",
            route_id="route-stopped",
            logical_key="route-stopped:forbidden",
            work_kind="DEVELOP_ROUTE",
            assignment_summary="must never be persisted",
            research_revision=7,
        )
    allowed = deriver.derive_work_item(
        run_id="run-1",
        route_id="route-continuing",
        logical_key="route-continuing:first",
        work_kind="DEVELOP_ROUTE",
        assignment_summary="continue active route",
        research_revision=7,
    )

    assert existing.aggregate_state == "QUEUED"
    assert allowed.route_id == "route-continuing"
    assert plans.get("plan-stopped").routes[0].stop_reason == "death test falsified mapping"
    records = activities.snapshot(run_id="run-1", limit=1000).records
    assert [record.payload["type"] for record in records].count("WORK_ITEM_CREATED") == 2
    assert records[-2].payload["type"] == "ROUTE_HINT_RECORDED"
    assert records[-1].payload["type"] == "WORK_ITEM_CREATED"


def test_pause_resume_and_stopped_plan_are_terminal(tmp_path: Path) -> None:
    plans, _deriver, _activities = services(database(tmp_path))
    item = proposal("plan-life", "route-life", "smt")
    activate(plans, item, "life")
    paused = apply(plans, item, "PAUSE", "pause", reason="awaiting theorem review").plan
    assert paused.state == "PAUSED"
    assert paused.state_reason == "awaiting theorem review"
    assert apply(plans, item, "START", "resume").plan.state == "ACTIVE"
    assert apply(plans, item, "STOP", "stop", reason="budget exhausted").plan.state == "STOPPED"
    with pytest.raises(RoutePlanError, match="immutable"):
        apply(plans, item, "SET_PRIORITY", "after-stop", priority=1)
