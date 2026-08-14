"""Revision-bound human guidance lifecycle without mathematical or route-control authority."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from rk.extensions import ProductActivity
from rk.product.activity_store import ActivityStore
from rk.product.route_plan import RunFence, sqlite_run_fence


class GuidanceError(RuntimeError):
    """A guidance identity, lifecycle transition, or target binding is invalid."""


class GuidanceConflict(GuidanceError):
    """A stable guidance identity was reused with different content."""


class GuidanceFenceMismatch(GuidanceError):
    """Guidance is not bound to the current research revision and contract."""


class FormalRouteActionRequired(GuidanceError):
    """A human stop request cannot replace B09b APPLY_ROUTE_PLAN."""


@dataclass(frozen=True, slots=True)
class Guidance:
    guidance_id: str
    run_id: str
    research_revision: int
    contract_version: int
    checkpoint_id: str
    target_kind: str
    target_id: str
    route_id: str
    kind: str
    content_artifact_id: str
    submitted_by: str
    supersedes_guidance_id: str | None
    state: str
    resolution_code: str | None
    applied_work_item_id: str | None
    created_at: str
    resolved_at: str | None


class GuidanceStore:
    """Persist human guidance and exact lifecycle transitions on ProductActivity."""

    def __init__(
        self,
        *,
        db_path: Path,
        activities: ActivityStore,
        event_id_generator: Callable[[], str],
        clock: Callable[[], str],
        busy_timeout_ms: int = 5_000,
    ) -> None:
        self._db_path = Path(db_path)
        self._activities = activities
        self._event_ids = event_id_generator
        self._clock = clock
        self._busy_timeout_ms = busy_timeout_ms

    def submit(
        self,
        *,
        guidance_id: str,
        run_id: str,
        research_revision: int,
        contract_version: int,
        checkpoint_id: str,
        target_kind: str,
        target_id: str,
        route_id: str,
        kind: str,
        content_artifact_id: str,
        submitted_by: str,
        supersedes_guidance_id: str | None = None,
    ) -> Guidance:
        declaration = (
            run_id,
            research_revision,
            contract_version,
            checkpoint_id,
            target_kind,
            target_id,
            route_id,
            kind,
            content_artifact_id,
            submitted_by,
            supersedes_guidance_id,
        )
        _validate_declaration(guidance_id, declaration)
        now = self._clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = self._row(connection, guidance_id)
            if existing is not None:
                stored = (
                    str(existing[1]),
                    int(str(existing[2])),
                    int(str(existing[3])),
                    str(existing[4]),
                    str(existing[5]),
                    str(existing[6]),
                    str(existing[7]),
                    str(existing[8]),
                    str(existing[9]),
                    str(existing[10]),
                    _optional(existing[11]),
                )
                if stored != declaration:
                    raise GuidanceConflict("guidance identity was reused with different content")
                connection.commit()
                return _guidance(existing)
            self.assert_fence(connection, run_id, research_revision, contract_version)
            self._assert_target(
                connection,
                run_id=run_id,
                checkpoint_id=checkpoint_id,
                target_kind=target_kind,
                target_id=target_id,
                route_id=route_id,
            )
            if supersedes_guidance_id is not None:
                previous = self._required_row(connection, supersedes_guidance_id)
                if (
                    str(previous[1]) != run_id
                    or str(previous[10]) != submitted_by
                    or str(previous[12]) != "QUEUED"
                    or str(previous[4]) != checkpoint_id
                    or str(previous[5]) != target_kind
                    or str(previous[6]) != target_id
                ):
                    raise GuidanceError(
                        "superseded guidance must be queued in the same author and target binding"
                    )
                self._resolve(
                    connection,
                    supersedes_guidance_id,
                    state="SUPERSEDED",
                    resolution_code="REPLACED_BY_NEW_GUIDANCE",
                    now=now,
                )
                self._activity(
                    connection,
                    previous,
                    event_type="GUIDANCE_SUPERSEDED",
                    state="SUPERSEDED",
                    now=now,
                    extra_ref={"replacement_guidance_id": guidance_id},
                )
            connection.execute(
                "INSERT INTO product_guidance("
                "guidance_id,run_id,research_revision,contract_version,checkpoint_id,"
                "target_kind,target_id,route_id,kind,content_artifact_id,submitted_by,"
                "supersedes_guidance_id,state,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,'QUEUED',?)",
                (guidance_id, *declaration, now),
            )
            row = self._required_row(connection, guidance_id)
            self._activity(
                connection,
                row,
                event_type="GUIDANCE_QUEUED",
                state="QUEUED",
                now=now,
            )
            connection.commit()
        return self.get(guidance_id)

    def cancel(self, guidance_id: str, *, actor_id: str) -> Guidance:
        if not actor_id:
            raise ValueError("cancelling identity is required")
        now = self._clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._required_row(connection, guidance_id)
            if str(row[10]) != actor_id:
                raise GuidanceError("only the submitting identity may cancel guidance")
            if str(row[12]) != "QUEUED":
                raise GuidanceError("only queued guidance may be cancelled")
            self._resolve(
                connection,
                guidance_id,
                state="CANCELLED",
                resolution_code="CANCELLED_BY_SUBMITTER",
                now=now,
            )
            self._activity(
                connection,
                row,
                event_type="GUIDANCE_CANCELLED",
                state="CANCELLED",
                now=now,
            )
            connection.commit()
        return self.get(guidance_id)

    def reject(
        self,
        guidance_id: str,
        *,
        resolution_code: str,
    ) -> Guidance:
        if not resolution_code:
            raise ValueError("rejection requires a typed resolution code")
        now = self._clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._required_row(connection, guidance_id)
            if str(row[12]) != "QUEUED":
                raise GuidanceError("only queued guidance may be rejected")
            self._resolve(
                connection,
                guidance_id,
                state="REJECTED",
                resolution_code=resolution_code,
                now=now,
            )
            self._activity(
                connection,
                row,
                event_type="GUIDANCE_REJECTED",
                state="REJECTED",
                now=now,
                resolution_code=resolution_code,
            )
            connection.commit()
        return self.get(guidance_id)

    def get(self, guidance_id: str) -> Guidance:
        with self._connect() as connection:
            return _guidance(self._required_row(connection, guidance_id))

    def queued_for_route(self, *, run_id: str, route_id: str) -> tuple[Guidance, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                _SELECT + " WHERE run_id=? AND route_id=? AND state='QUEUED' "
                "ORDER BY created_at,guidance_id",
                (run_id, route_id),
            ).fetchall()
        return tuple(_guidance(row) for row in rows)

    @staticmethod
    def assert_fence(
        connection: sqlite3.Connection,
        run_id: str,
        research_revision: int,
        contract_version: int,
    ) -> None:
        actual = sqlite_run_fence(connection, run_id)
        if actual != RunFence(research_revision, contract_version):
            raise GuidanceFenceMismatch(
                f"guidance fence is revision {research_revision}, contract {contract_version}; "
                f"current fence is revision {actual.research_revision}, "
                f"contract {actual.contract_version}"
            )

    @staticmethod
    def mark_applied(
        connection: sqlite3.Connection,
        *,
        guidance_id: str,
        work_item_id: str,
        effect_kind: str,
        content_artifact_id: str,
        input_artifact_ids_json: str,
        now: str,
    ) -> None:
        result = connection.execute(
            "UPDATE product_guidance SET state='APPLIED',"
            "resolution_code='WORK_INPUT_CHANGED',applied_work_item_id=?,resolved_at=? "
            "WHERE guidance_id=? AND state='QUEUED'",
            (work_item_id, now, guidance_id),
        )
        if result.rowcount != 1:
            raise GuidanceError("guidance is no longer queued")
        connection.execute(
            "INSERT INTO product_guidance_effects("
            "guidance_id,work_item_id,effect_kind,content_artifact_id,"
            "input_artifact_ids_json,applied_at) VALUES(?,?,?,?,?,?)",
            (
                guidance_id,
                work_item_id,
                effect_kind,
                content_artifact_id,
                input_artifact_ids_json,
                now,
            ),
        )

    def append_applied_activity(
        self,
        connection: sqlite3.Connection,
        guidance: Guidance,
        *,
        work_item_id: str,
        now: str,
    ) -> None:
        self._activities.append_in_transaction(
            connection,
            ProductActivity(
                event_id=self._event_ids(),
                scope_kind="RUN",
                run_id=guidance.run_id,
                source="ORCHESTRATOR",
                research_revision=guidance.research_revision,
                entity_refs={
                    "guidance_id": guidance.guidance_id,
                    "checkpoint_id": guidance.checkpoint_id,
                    "target_id": guidance.target_id,
                    "route_id": guidance.route_id,
                    "work_item_id": work_item_id,
                },
                payload={
                    "type": "GUIDANCE_APPLIED",
                    "kind": guidance.kind,
                    "state": "APPLIED",
                    "content_artifact_id": guidance.content_artifact_id,
                },
                recorded_at=now,
            ),
        )

    def _assert_target(
        self,
        connection: sqlite3.Connection,
        *,
        run_id: str,
        checkpoint_id: str,
        target_kind: str,
        target_id: str,
        route_id: str,
    ) -> None:
        checkpoint = connection.execute(
            "SELECT wi.run_id FROM product_worker_runs wr "
            "JOIN product_work_items wi ON wi.work_item_id=wr.work_item_id "
            "WHERE wr.checkpoint_id=?",
            (checkpoint_id,),
        ).fetchone()
        if checkpoint is None or str(checkpoint[0]) != run_id:
            raise GuidanceError("checkpoint is outside the research run")
        route = connection.execute(
            "SELECT rp.run_id FROM product_planned_routes pr "
            "JOIN product_route_plans rp ON rp.route_plan_id=pr.route_plan_id "
            "WHERE pr.route_id=?",
            (route_id,),
        ).fetchone()
        if route is None or str(route[0]) != run_id:
            raise GuidanceError("route is outside the research run")
        if target_kind == "ROUTE":
            if target_id != route_id:
                raise GuidanceError("route target must equal the bound route")
            return
        target = connection.execute(
            "SELECT run_id,route_id FROM product_work_items WHERE work_item_id=?",
            (target_id,),
        ).fetchone()
        if target is None or str(target[0]) != run_id or _optional(target[1]) != route_id:
            raise GuidanceError("work-item target is outside the route binding")

    def _activity(
        self,
        connection: sqlite3.Connection,
        row: sqlite3.Row | tuple[object, ...],
        *,
        event_type: str,
        state: str,
        now: str,
        resolution_code: str | None = None,
        extra_ref: dict[str, str] | None = None,
    ) -> None:
        refs = {
            "guidance_id": str(row[0]),
            "checkpoint_id": str(row[4]),
            "target_id": str(row[6]),
            "route_id": str(row[7]),
        }
        refs.update(extra_ref or {})
        payload = {"type": event_type, "kind": str(row[8]), "state": state}
        if resolution_code is not None:
            payload["resolution_code"] = resolution_code
        self._activities.append_in_transaction(
            connection,
            ProductActivity(
                event_id=self._event_ids(),
                scope_kind="RUN",
                run_id=str(row[1]),
                source="HUMAN_GUIDANCE",
                research_revision=int(str(row[2])),
                entity_refs=refs,
                payload=payload,
                recorded_at=now,
            ),
        )

    @staticmethod
    def _resolve(
        connection: sqlite3.Connection,
        guidance_id: str,
        *,
        state: str,
        resolution_code: str,
        now: str,
    ) -> None:
        result = connection.execute(
            "UPDATE product_guidance SET state=?,resolution_code=?,resolved_at=? "
            "WHERE guidance_id=? AND state='QUEUED'",
            (state, resolution_code, now, guidance_id),
        )
        if result.rowcount != 1:
            raise GuidanceError("guidance is no longer queued")

    @staticmethod
    def _row(
        connection: sqlite3.Connection, guidance_id: str
    ) -> sqlite3.Row | tuple[object, ...] | None:
        return cast(
            sqlite3.Row | tuple[object, ...] | None,
            connection.execute(_SELECT + " WHERE guidance_id=?", (guidance_id,)).fetchone(),
        )

    @classmethod
    def _required_row(
        cls, connection: sqlite3.Connection, guidance_id: str
    ) -> sqlite3.Row | tuple[object, ...]:
        row = cls._row(connection, guidance_id)
        if row is None:
            raise KeyError(guidance_id)
        return row

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


def _validate_declaration(guidance_id: str, declaration: tuple[object, ...]) -> None:
    if not guidance_id or any(not value for value in declaration if isinstance(value, str)):
        raise ValueError("guidance identities and bindings must be non-empty")
    revision = declaration[1]
    contract = declaration[2]
    if not isinstance(revision, int) or isinstance(revision, bool) or revision < 0:
        raise ValueError("research revision is invalid")
    if not isinstance(contract, int) or isinstance(contract, bool) or contract <= 0:
        raise ValueError("contract version is invalid")
    if declaration[4] not in {"ROUTE", "WORK_ITEM"}:
        raise ValueError("unknown guidance target kind")
    if declaration[7] not in {
        "CHANGE_REPRESENTATION",
        "PRIORITIZE_LEMMA",
        "STOP_ROUTE_REQUEST",
    }:
        raise ValueError("unknown guidance kind")


def _guidance(row: sqlite3.Row | tuple[object, ...]) -> Guidance:
    return Guidance(
        guidance_id=str(row[0]),
        run_id=str(row[1]),
        research_revision=int(str(row[2])),
        contract_version=int(str(row[3])),
        checkpoint_id=str(row[4]),
        target_kind=str(row[5]),
        target_id=str(row[6]),
        route_id=str(row[7]),
        kind=str(row[8]),
        content_artifact_id=str(row[9]),
        submitted_by=str(row[10]),
        supersedes_guidance_id=_optional(row[11]),
        state=str(row[12]),
        resolution_code=_optional(row[13]),
        applied_work_item_id=_optional(row[14]),
        created_at=str(row[15]),
        resolved_at=_optional(row[16]),
    )


def _optional(value: object) -> str | None:
    return str(value) if value is not None else None


_SELECT = (
    "SELECT guidance_id,run_id,research_revision,contract_version,checkpoint_id,"
    "target_kind,target_id,route_id,kind,content_artifact_id,submitted_by,"
    "supersedes_guidance_id,state,resolution_code,applied_work_item_id,"
    "created_at,resolved_at FROM product_guidance"
)


__all__ = [
    "FormalRouteActionRequired",
    "Guidance",
    "GuidanceConflict",
    "GuidanceError",
    "GuidanceFenceMismatch",
    "GuidanceStore",
]
