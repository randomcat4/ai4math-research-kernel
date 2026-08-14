from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest

from rk.product.activity_store import ActivityStore
from rk.product.api import QuerySpec
from rk.product.domain_queries import (
    DomainObjectNotFound,
    DomainQueries,
    FenceSource,
)
from rk.product.jobs import JobStore
from rk.product.problem_pool import ProblemPoolStore
from rk.product.research_lineage import ResearchLineageStore
from rk.product.route_plan import RoutePlanProposal, RoutePlanStore, RouteProposal
from rk.product.tool_runs import ToolCatalogStore, ToolRunStore
from rk.product.work_activity import WorkActivityStore
from rk.product_migrations import ProductMigrationAssembler, ProductMigrationRegistry


class Ids:
    def __init__(self) -> None:
        self.value = 0

    def __call__(self) -> str:
        self.value += 1
        return f"domain-query-{self.value}"


class Artifacts:
    def open_range(self, artifact_id: str, *, expected_ref: object = None) -> object:
        raise AssertionError(f"unexpected artifact read: {artifact_id}:{expected_ref}")


@dataclass(frozen=True)
class Fence:
    run_id: str = "run-1"
    research_revision: int = 7
    contract_version: int = 3
    last_cursor: int = 11


class Fences:
    def run(self, run_id: str) -> Fence:
        if run_id != "run-1":
            raise KeyError(run_id)
        return Fence()


def adapter(tmp_path: Path) -> tuple[DomainQueries, RoutePlanStore]:
    db_path = tmp_path / "domain-queries.sqlite"
    with sqlite3.connect(db_path, isolation_level=None) as connection:
        connection.execute(
            "CREATE TABLE runs(run_id TEXT PRIMARY KEY,revision INTEGER NOT NULL,"
            "current_contract_version INTEGER NOT NULL) STRICT"
        )
        connection.execute("INSERT INTO runs VALUES('run-1',7,3)")
        ProductMigrationAssembler(ProductMigrationRegistry(Path("schema_fragments"))).apply(
            connection
        )
    ids = Ids()
    activities = ActivityStore(db_path)
    plans = RoutePlanStore(
        db_path=db_path,
        activities=activities,
        id_generator=ids,
        clock=lambda: "2026-08-14T00:00:00Z",
    )
    work = WorkActivityStore(
        db_path=db_path,
        activities=activities,
        id_generator=ids,
        clock=lambda: "2026-08-14T00:00:00Z",
    )
    jobs = JobStore(db_path, ids)
    return (
        DomainQueries(
            db_path=db_path,
            deployment_id="deployment-1",
            fences=cast(FenceSource, Fences()),
            route_plans=plans,
            work=work,
            tool_catalog=ToolCatalogStore(db_path),
            tool_runs=ToolRunStore(db_path, jobs),
            problem_pools=ProblemPoolStore(db_path),
            lineages=ResearchLineageStore(db_path=db_path, artifacts=Artifacts()),
            cursor_secret=b"domain-query-test-cursor-secret-32-bytes",
        ),
        plans,
    )


def test_route_plan_projects_store_object_with_current_fence(tmp_path: Path) -> None:
    queries, plans = adapter(tmp_path)
    plan = plans.register_proposal(
        RoutePlanProposal(
            route_plan_id="plan-1",
            run_id="run-1",
            research_revision=7,
            contract_version=3,
            routes=(
                RouteProposal(
                    route_id="route-1",
                    method="direct",
                    target="claim-root",
                    expected_verifier="lean",
                    milestones=("formalize", "verify"),
                    termination_condition="verified or refuted",
                    dependencies=(),
                    priority=1,
                    budget={"token": 1000},
                ),
            ),
        )
    )
    result = queries.execute(
        QuerySpec(
            {"kind": "RUN", "run_id": "run-1"},
            "ROUTE_PLAN",
            {"route_plan_id": "plan-1"},
        )
    )
    assert result.stable_entity_id == plan.route_plan_id
    assert result.fence["research_revision"] == 7
    assert result.data["plan_digest"] == plan.plan_digest
    routes = cast(tuple[dict[str, object], ...], result.data["routes"])
    assert routes[0]["expected_verifier"] == "lean"


def test_absent_compute_task_is_not_relabelled_from_another_store(tmp_path: Path) -> None:
    queries, _ = adapter(tmp_path)
    with pytest.raises(DomainObjectNotFound):
        queries.execute(
            QuerySpec(
                {"kind": "RUN", "run_id": "run-1"},
                "COMPUTE_TASK",
                {"compute_task_id": "missing-compute-task"},
            )
        )
