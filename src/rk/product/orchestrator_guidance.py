"""Atomic application of queued human guidance to future B09a work."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Sequence
from pathlib import Path
from typing import Any

from rk.extensions import ProductActivity
from rk.product.activity_store import ActivityStore
from rk.product.guidance import (
    FormalRouteActionRequired,
    Guidance,
    GuidanceError,
    GuidanceFenceMismatch,
    GuidanceStore,
)
from rk.product.route_plan import RoutePlanStore
from rk.product.work_activity import WorkActivityError, WorkActivityStore, WorkItem
from rk.sqlite import open_sqlite


class GuidedWorkDeriver:
    """Turn one eligible hint into one immutable work input under the B09b route gate."""

    def __init__(
        self,
        *,
        db_path: Path,
        guidance: GuidanceStore,
        route_plans: RoutePlanStore,
        work_activity: WorkActivityStore,
        activities: ActivityStore,
        id_generator: Callable[[], str],
        clock: Callable[[], str],
        busy_timeout_ms: int = 5_000,
    ) -> None:
        self._db_path = Path(db_path)
        self._guidance = guidance
        self._route_plans = route_plans
        self._work_activity = work_activity
        self._activities = activities
        self._ids = id_generator
        self._clock = clock
        self._busy_timeout_ms = busy_timeout_ms

    def derive_work_item(
        self,
        *,
        guidance_id: str,
        run_id: str,
        route_id: str,
        logical_key: str,
        work_kind: str,
        assignment_summary: str,
        research_revision: int,
        contract_version: int,
        parent_work_item_id: str | None = None,
        assignment_artifact_ids: Sequence[str] = (),
        input_artifact_ids: Sequence[str] = (),
    ) -> WorkItem:
        item = self._guidance.get(guidance_id)
        self._assert_request_binding(
            item,
            run_id=run_id,
            route_id=route_id,
            research_revision=research_revision,
            contract_version=contract_version,
            parent_work_item_id=parent_work_item_id,
        )
        if item.kind == "STOP_ROUTE_REQUEST":
            rejected = self._guidance.reject(
                guidance_id,
                resolution_code="FORMAL_B09B_STOP_REQUIRED",
            )
            raise FormalRouteActionRequired(
                f"guidance {rejected.guidance_id} was rejected; submit B09b APPLY_ROUTE_PLAN STOP"
            )
        assignments = _ids(assignment_artifact_ids)
        original_inputs = _ids(input_artifact_ids)
        if item.content_artifact_id in original_inputs:
            self._guidance.reject(guidance_id, resolution_code="NO_INPUT_CHANGE")
            raise GuidanceError("guidance artifact is already present in the requested work input")
        effective_inputs = (*original_inputs, item.content_artifact_id)
        effect_kind, directive = {
            "CHANGE_REPRESENTATION": (
                "REPRESENTATION_INPUT",
                "apply the bound change-of-representation artifact",
            ),
            "PRIORITIZE_LEMMA": (
                "LEMMA_PRIORITY_INPUT",
                "prioritize the lemma identified by the bound artifact",
            ),
        }[item.kind]
        effective_summary = (
            f"{assignment_summary}\nHuman guidance ({item.guidance_id}): {directive}."
        )
        effective_key = f"guidance:{guidance_id}:{logical_key}"
        now = self._clock()
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                current = self._guidance_row(connection, guidance_id)
                if current.state != "QUEUED":
                    raise GuidanceError("guidance is no longer queued")
                self._guidance.assert_fence(
                    connection,
                    run_id,
                    research_revision,
                    contract_version,
                )
                self._route_plans.assert_route_active(
                    connection,
                    run_id=run_id,
                    route_id=route_id,
                )
                existing = connection.execute(
                    "SELECT work_item_id,work_kind,route_id,parent_work_item_id,"
                    "assignment_summary,assignment_artifact_ids_json,"
                    "input_artifact_ids_json FROM product_work_items "
                    "WHERE run_id=? AND logical_key=?",
                    (run_id, effective_key),
                ).fetchone()
                if existing is None:
                    work_item_id = self._ids()
                    connection.execute(
                        "INSERT INTO product_work_items("
                        "work_item_id,run_id,logical_key,work_kind,route_id,"
                        "parent_work_item_id,assignment_summary,"
                        "assignment_artifact_ids_json,input_artifact_ids_json,created_at) "
                        "VALUES(?,?,?,?,?,?,?,?,?,?)",
                        (
                            work_item_id,
                            run_id,
                            effective_key,
                            work_kind,
                            route_id,
                            parent_work_item_id,
                            effective_summary,
                            _json(assignments),
                            _json(effective_inputs),
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
                            entity_refs={
                                "work_item_id": work_item_id,
                                "route_id": route_id,
                                "guidance_id": guidance_id,
                            },
                            payload={
                                "type": "WORK_ITEM_CREATED",
                                "work_kind": work_kind,
                                "route_id": route_id,
                                "guidance_effect": effect_kind,
                            },
                            recorded_at=now,
                        ),
                    )
                else:
                    work_item_id = str(existing[0])
                    stored = (
                        str(existing[1]),
                        _optional(existing[2]),
                        _optional(existing[3]),
                        str(existing[4]),
                        _json_ids(existing[5]),
                        _json_ids(existing[6]),
                    )
                    expected = (
                        work_kind,
                        route_id,
                        parent_work_item_id,
                        effective_summary,
                        assignments,
                        effective_inputs,
                    )
                    if stored != expected:
                        raise WorkActivityError(
                            "guided work logical key is bound to another declaration"
                        )
                self._guidance.mark_applied(
                    connection,
                    guidance_id=guidance_id,
                    work_item_id=work_item_id,
                    effect_kind=effect_kind,
                    content_artifact_id=item.content_artifact_id,
                    input_artifact_ids_json=_json(effective_inputs),
                    now=now,
                )
                self._guidance.append_applied_activity(
                    connection,
                    item,
                    work_item_id=work_item_id,
                    now=now,
                )
                connection.commit()
        except GuidanceFenceMismatch:
            self._guidance.reject(guidance_id, resolution_code="STALE_RESEARCH_FENCE")
            raise
        return self._work_activity.get_work_item(work_item_id)

    @staticmethod
    def _assert_request_binding(
        guidance: Guidance,
        *,
        run_id: str,
        route_id: str,
        research_revision: int,
        contract_version: int,
        parent_work_item_id: str | None,
    ) -> None:
        if guidance.state != "QUEUED":
            raise GuidanceError("only queued guidance can affect future work")
        if (
            guidance.run_id != run_id
            or guidance.route_id != route_id
            or guidance.research_revision != research_revision
            or guidance.contract_version != contract_version
        ):
            raise GuidanceError("work derivation does not match the exact guidance binding")
        if guidance.target_kind == "WORK_ITEM" and guidance.target_id != parent_work_item_id:
            raise GuidanceError("work-target guidance must derive from its bound parent work item")

    def _guidance_row(self, connection: sqlite3.Connection, guidance_id: str) -> Guidance:
        row = connection.execute(
            "SELECT guidance_id,run_id,research_revision,contract_version,checkpoint_id,"
            "target_kind,target_id,route_id,kind,content_artifact_id,submitted_by,"
            "supersedes_guidance_id,state,resolution_code,applied_work_item_id,"
            "created_at,resolved_at FROM product_guidance WHERE guidance_id=?",
            (guidance_id,),
        ).fetchone()
        if row is None:
            raise KeyError(guidance_id)
        return Guidance(
            str(row[0]),
            str(row[1]),
            int(row[2]),
            int(row[3]),
            str(row[4]),
            str(row[5]),
            str(row[6]),
            str(row[7]),
            str(row[8]),
            str(row[9]),
            str(row[10]),
            _optional(row[11]),
            str(row[12]),
            _optional(row[13]),
            _optional(row[14]),
            str(row[15]),
            _optional(row[16]),
        )

    def _connect(self) -> sqlite3.Connection:
        connection = open_sqlite(
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
    result = tuple(values)
    if len(result) != len(set(result)):
        raise ValueError("artifact identities must be unique")
    return result


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_ids(value: object) -> tuple[str, ...]:
    parsed = json.loads(str(value))
    if not isinstance(parsed, list) or any(not isinstance(item, str) for item in parsed):
        raise WorkActivityError("stored artifact identities are invalid")
    return tuple(parsed)


def _optional(value: object) -> str | None:
    return str(value) if value is not None else None


__all__ = ["GuidedWorkDeriver"]
