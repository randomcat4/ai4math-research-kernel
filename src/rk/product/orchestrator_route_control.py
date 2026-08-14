"""Atomic route-state gate for orchestration-derived B09a work items."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from rk.extensions import ProductActivity
from rk.product.activity_store import ActivityStore
from rk.product.route_plan import RoutePlanStore
from rk.product.work_activity import WorkActivityError, WorkActivityStore, WorkItem


class RouteControlledWorkDeriver:
    """Create a work item only while its formal route remains ACTIVE."""

    def __init__(
        self,
        *,
        db_path: Path,
        route_plans: RoutePlanStore,
        work_activity: WorkActivityStore,
        activities: ActivityStore,
        id_generator: Callable[[], str],
        clock: Callable[[], str],
        busy_timeout_ms: int = 5_000,
    ) -> None:
        self._db_path = Path(db_path)
        self._route_plans = route_plans
        self._work_activity = work_activity
        self._activities = activities
        self._ids = id_generator
        self._clock = clock
        self._busy_timeout_ms = busy_timeout_ms

    def derive_work_item(
        self,
        *,
        run_id: str,
        route_id: str,
        logical_key: str,
        work_kind: str,
        assignment_summary: str,
        research_revision: int,
        parent_work_item_id: str | None = None,
        assignment_artifact_ids: Sequence[str] = (),
        input_artifact_ids: Sequence[str] = (),
    ) -> WorkItem:
        if not all((run_id, route_id, logical_key, work_kind, assignment_summary)):
            raise ValueError("route-derived work requires explicit identities and assignment")
        assignments = _ids(assignment_artifact_ids)
        inputs = _ids(input_artifact_ids)
        now = self._clock()
        work_item_id = self._ids()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._route_plans.assert_route_active(connection, run_id=run_id, route_id=route_id)
            existing = connection.execute(
                "SELECT work_item_id,work_kind,route_id,parent_work_item_id,"
                "assignment_summary,assignment_artifact_ids_json,input_artifact_ids_json "
                "FROM product_work_items WHERE run_id=? AND logical_key=?",
                (run_id, logical_key),
            ).fetchone()
            if existing is not None:
                declaration = (
                    str(existing[1]),
                    str(existing[2]) if existing[2] is not None else None,
                    str(existing[3]) if existing[3] is not None else None,
                    str(existing[4]),
                    _json_ids(existing[5]),
                    _json_ids(existing[6]),
                )
                if declaration != (
                    work_kind,
                    route_id,
                    parent_work_item_id,
                    assignment_summary,
                    assignments,
                    inputs,
                ):
                    raise WorkActivityError(
                        "work logical key is already bound to another immutable declaration"
                    )
                connection.commit()
                return self._work_activity.get_work_item(str(existing[0]))
            if parent_work_item_id is not None:
                parent = connection.execute(
                    "SELECT run_id FROM product_work_items WHERE work_item_id=?",
                    (parent_work_item_id,),
                ).fetchone()
                if parent is None or str(parent[0]) != run_id:
                    raise WorkActivityError("parent work item must belong to the same research run")
            connection.execute(
                "INSERT INTO product_work_items("
                "work_item_id,run_id,logical_key,work_kind,route_id,parent_work_item_id,"
                "assignment_summary,assignment_artifact_ids_json,input_artifact_ids_json,"
                "created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    work_item_id,
                    run_id,
                    logical_key,
                    work_kind,
                    route_id,
                    parent_work_item_id,
                    assignment_summary,
                    _json(assignments),
                    _json(inputs),
                    now,
                ),
            )
            self._activities.append_in_transaction(
                connection,
                ProductActivity(
                    event_id=self._ids(),
                    scope_kind="RUN",
                    run_id=run_id,
                    source="ORCHESTRATOR",
                    research_revision=research_revision,
                    entity_refs={"work_item_id": work_item_id, "route_id": route_id},
                    payload={
                        "type": "WORK_ITEM_CREATED",
                        "work_kind": work_kind,
                        "route_id": route_id,
                    },
                    recorded_at=now,
                ),
            )
            connection.commit()
        return self._work_activity.get_work_item(work_item_id)

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


def _ids(values: Sequence[Any]) -> tuple[str, ...]:
    if any(not isinstance(value, str) or not value for value in values):
        raise ValueError("artifact identities must be non-empty strings")
    return tuple(values)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_ids(value: Any) -> tuple[str, ...]:
    parsed = json.loads(str(value))
    if not isinstance(parsed, list) or any(not isinstance(item, str) for item in parsed):
        raise WorkActivityError("stored artifact identities are invalid")
    return tuple(parsed)


__all__ = ["RouteControlledWorkDeriver"]
