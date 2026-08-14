from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from rk.product.activity_store import ActivityStore
from rk.product.guidance import (
    FormalRouteActionRequired,
    GuidanceError,
    GuidanceFenceMismatch,
    GuidanceStore,
)
from rk.product.orchestrator_guidance import GuidedWorkDeriver
from rk.product.route_plan import (
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
        return f"id-{self.value:05d}"


class Clock:
    def __init__(self) -> None:
        self.value = 0

    def __call__(self) -> str:
        self.value += 1
        return f"2026-08-14T10:00:{self.value:02d}Z"


def _database(tmp_path: Path) -> Path:
    path = tmp_path / "guidance.sqlite"
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


def _services(
    tmp_path: Path,
) -> tuple[
    Path,
    GuidanceStore,
    GuidedWorkDeriver,
    RoutePlanStore,
    WorkActivityStore,
    ActivityStore,
]:
    path = _database(tmp_path)
    ids = Ids()
    clock = Clock()
    activities = ActivityStore(path)
    plans = RoutePlanStore(
        db_path=path,
        activities=activities,
        id_generator=ids,
        clock=clock,
    )
    proposal = RoutePlanProposal(
        route_plan_id="plan-1",
        run_id="run-1",
        research_revision=7,
        contract_version=3,
        routes=(
            RouteProposal(
                route_id="route-1",
                method="direct",
                target="prove target",
                expected_verifier="Lean",
                milestones=("lemma",),
                termination_condition="verified or falsified",
                dependencies=("contract-3",),
                priority=1,
                budget={"wall_seconds": 600},
            ),
        ),
    )
    plans.register_proposal(proposal)
    plans.apply(
        run_id="run-1",
        request_id="approve",
        expected_revision=7,
        contract_version=3,
        action="APPROVE",
        route_plan_id="plan-1",
        plan_digest=proposal.digest,
    )
    plans.apply(
        run_id="run-1",
        request_id="start",
        expected_revision=7,
        contract_version=3,
        action="START",
        route_plan_id="plan-1",
    )
    work = WorkActivityStore(
        db_path=path,
        activities=activities,
        id_generator=ids,
        clock=clock,
    )
    parent = work.create_work_item(
        run_id="run-1",
        logical_key="route-1:parent",
        work_kind="DEVELOP_ROUTE",
        route_id="route-1",
        assignment_summary="initial route work",
        research_revision=7,
    )
    work.assign_worker(
        parent.work_item_id,
        worker_kind="ROLE_EXECUTION",
        role_id="PROVER",
        process_token="worker-token",
        budget_plan={"wall_seconds": 60},
        research_revision=7,
        checkpoint_id="checkpoint-1",
    )
    guidance = GuidanceStore(
        db_path=path,
        activities=activities,
        event_id_generator=ids,
        clock=clock,
    )
    deriver = GuidedWorkDeriver(
        db_path=path,
        guidance=guidance,
        route_plans=plans,
        work_activity=work,
        activities=activities,
        id_generator=ids,
        clock=clock,
    )
    return path, guidance, deriver, plans, work, activities


def _submit(
    guidance: GuidanceStore,
    guidance_id: str,
    kind: str,
    *,
    target_kind: str = "ROUTE",
    target_id: str = "route-1",
    supersedes: str | None = None,
):
    return guidance.submit(
        guidance_id=guidance_id,
        run_id="run-1",
        research_revision=7,
        contract_version=3,
        checkpoint_id="checkpoint-1",
        target_kind=target_kind,
        target_id=target_id,
        route_id="route-1",
        kind=kind,
        content_artifact_id=f"artifact-{guidance_id}",
        submitted_by="main-identity",
        supersedes_guidance_id=supersedes,
    )


@pytest.mark.parametrize(
    ("kind", "effect_kind"),
    [
        ("CHANGE_REPRESENTATION", "REPRESENTATION_INPUT"),
        ("PRIORITIZE_LEMMA", "LEMMA_PRIORITY_INPUT"),
    ],
)
def test_guidance_changes_future_work_input_atomically(
    tmp_path: Path,
    kind: str,
    effect_kind: str,
) -> None:
    path, guidance, deriver, _plans, _work, activities = _services(tmp_path)
    queued = _submit(guidance, "hint-1", kind)
    assert queued.state == "QUEUED"
    with sqlite3.connect(path) as connection:
        graph_before = connection.execute("SELECT COUNT(*) FROM product_graph_nodes").fetchone()

    derived = deriver.derive_work_item(
        guidance_id="hint-1",
        run_id="run-1",
        route_id="route-1",
        logical_key="next",
        work_kind="DEVELOP_ROUTE",
        assignment_summary="continue proof search",
        research_revision=7,
        contract_version=3,
        input_artifact_ids=("contract-artifact",),
    )

    assert derived.input_artifact_ids == (
        "contract-artifact",
        "artifact-hint-1",
    )
    assert "Human guidance (hint-1)" in derived.assignment_summary
    applied = guidance.get("hint-1")
    assert applied.state == "APPLIED"
    assert applied.applied_work_item_id == derived.work_item_id
    with sqlite3.connect(path) as connection:
        effect = connection.execute(
            "SELECT effect_kind,content_artifact_id,input_artifact_ids_json "
            "FROM product_guidance_effects WHERE guidance_id='hint-1'"
        ).fetchone()
        assert effect == (
            effect_kind,
            "artifact-hint-1",
            '["contract-artifact","artifact-hint-1"]',
        )
        assert (
            connection.execute("SELECT COUNT(*) FROM product_graph_nodes").fetchone()
            == graph_before
        )
    records = activities.snapshot(run_id="run-1", limit=1000).records
    assert [row.payload["type"] for row in records][-2:] == [
        "WORK_ITEM_CREATED",
        "GUIDANCE_APPLIED",
    ]


def test_supersede_cancel_and_identity_replay_are_explicit(tmp_path: Path) -> None:
    _path, guidance, _deriver, _plans, _work, _activities = _services(tmp_path)
    first = _submit(guidance, "hint-old", "PRIORITIZE_LEMMA")
    assert _submit(guidance, "hint-old", "PRIORITIZE_LEMMA") == first

    replacement = _submit(
        guidance,
        "hint-new",
        "CHANGE_REPRESENTATION",
        supersedes="hint-old",
    )
    assert replacement.state == "QUEUED"
    assert guidance.get("hint-old").state == "SUPERSEDED"
    cancelled = guidance.cancel("hint-new", actor_id="main-identity")
    assert cancelled.state == "CANCELLED"
    with pytest.raises(GuidanceError, match="only queued"):
        guidance.cancel("hint-old", actor_id="main-identity")


def test_stop_hint_is_rejected_and_only_formal_b09b_action_stops_route(
    tmp_path: Path,
) -> None:
    _path, guidance, deriver, plans, _work, _activities = _services(tmp_path)
    _submit(guidance, "hint-stop", "STOP_ROUTE_REQUEST")

    with pytest.raises(FormalRouteActionRequired, match="APPLY_ROUTE_PLAN STOP"):
        deriver.derive_work_item(
            guidance_id="hint-stop",
            run_id="run-1",
            route_id="route-1",
            logical_key="must-not-exist",
            work_kind="DEVELOP_ROUTE",
            assignment_summary="stop",
            research_revision=7,
            contract_version=3,
        )

    assert guidance.get("hint-stop").state == "REJECTED"
    assert guidance.get("hint-stop").resolution_code == "FORMAL_B09B_STOP_REQUIRED"
    assert plans.get("plan-1").state == "ACTIVE"
    stopped = plans.apply(
        run_id="run-1",
        request_id="formal-stop",
        expected_revision=7,
        contract_version=3,
        action="STOP",
        route_plan_id="plan-1",
        reason="human requested formal stop",
    )
    assert stopped.plan.state == "STOPPED"


def test_stale_fence_is_rejected_before_work_derivation(tmp_path: Path) -> None:
    path, guidance, deriver, _plans, _work, _activities = _services(tmp_path)
    _submit(guidance, "hint-stale", "CHANGE_REPRESENTATION")
    with sqlite3.connect(path) as connection:
        connection.execute("UPDATE runs SET revision=8 WHERE run_id='run-1'")

    with pytest.raises(GuidanceFenceMismatch, match="current fence is revision 8"):
        deriver.derive_work_item(
            guidance_id="hint-stale",
            run_id="run-1",
            route_id="route-1",
            logical_key="stale",
            work_kind="DEVELOP_ROUTE",
            assignment_summary="must not derive",
            research_revision=7,
            contract_version=3,
        )

    assert guidance.get("hint-stale").state == "REJECTED"
    assert guidance.get("hint-stale").resolution_code == "STALE_RESEARCH_FENCE"


def test_checkpoint_and_work_target_are_exact_run_bindings(tmp_path: Path) -> None:
    _path, guidance, deriver, _plans, work, _activities = _services(tmp_path)
    with pytest.raises(GuidanceError, match="checkpoint"):
        guidance.submit(
            guidance_id="bad-checkpoint",
            run_id="run-1",
            research_revision=7,
            contract_version=3,
            checkpoint_id="missing",
            target_kind="ROUTE",
            target_id="route-1",
            route_id="route-1",
            kind="CHANGE_REPRESENTATION",
            content_artifact_id="artifact",
            submitted_by="main-identity",
        )
    parent = work.create_work_item(
        run_id="run-1",
        logical_key="route-1:target-parent",
        work_kind="DEVELOP_ROUTE",
        route_id="route-1",
        assignment_summary="targeted parent",
        research_revision=7,
    )
    _submit(
        guidance,
        "hint-targeted",
        "PRIORITIZE_LEMMA",
        target_kind="WORK_ITEM",
        target_id=parent.work_item_id,
    )
    with pytest.raises(GuidanceError, match="bound parent"):
        deriver.derive_work_item(
            guidance_id="hint-targeted",
            run_id="run-1",
            route_id="route-1",
            logical_key="wrong-parent",
            work_kind="DEVELOP_ROUTE",
            assignment_summary="wrong",
            research_revision=7,
            contract_version=3,
        )
