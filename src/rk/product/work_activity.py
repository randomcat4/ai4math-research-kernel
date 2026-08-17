"""Stable work identities and append-only worker/attempt history on one activity cursor."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from rk.extensions import ProductActivity
from rk.product.activity_store import ActivityStore

_WORKER_TERMINAL = frozenset({"COMPLETED", "FAILED", "CANCELLED"})
_WORKER_TRANSITIONS: Mapping[str, frozenset[str]] = {
    "QUEUED": frozenset({"RUNNING", "FAILED", "CANCELLED"}),
    "RUNNING": frozenset(
        {
            "WAITING_TOOL",
            "WAITING_REVIEW",
            "PAUSED",
            "CANCEL_REQUESTED",
            "COMPLETED",
            "FAILED",
            "CANCELLED",
        }
    ),
    "WAITING_TOOL": frozenset({"RUNNING", "FAILED", "CANCEL_REQUESTED", "CANCELLED"}),
    "WAITING_REVIEW": frozenset({"RUNNING", "FAILED", "CANCEL_REQUESTED", "CANCELLED"}),
    "PAUSED": frozenset({"RUNNING", "CANCEL_REQUESTED", "CANCELLED"}),
    "CANCEL_REQUESTED": frozenset({"CANCELLED", "FAILED"}),
}
_ATTEMPT_TERMINAL = frozenset({"SUCCEEDED", "FAILED", "CANCELLED", "OUTCOME_UNKNOWN"})
_PUBLIC_FIELDS: Mapping[str, frozenset[str]] = {
    "DIAGNOSTIC_RECORDED": frozenset(
        {"diagnostic_code", "diagnostic_summary", "severity", "exit_code", "artifact_ids"}
    ),
    "SEARCH_RECORDED": frozenset(
        {
            "connector",
            "status",
            "result_count",
            "query_artifact_id",
            "snapshot_artifact_id",
            "duration_ms",
        }
    ),
}


class WorkActivityError(RuntimeError):
    """A stable identity, lifecycle transition, recovery, or public projection failed."""


@dataclass(frozen=True, slots=True)
class WorkerAttempt:
    attempt_id: str
    worker_run_id: str
    ordinal: int
    state: str
    started_at: str
    finished_at: str | None
    exit_code: int | None
    diagnostic_code: str | None
    output_artifact_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class WorkerRun:
    worker_run_id: str
    work_item_id: str
    ordinal: int
    worker_kind: str
    role_id: str
    parent_worker_run_id: str | None
    state: str
    checkpoint_id: str | None
    enqueued_at: str
    started_at: str | None
    finished_at: str | None
    last_activity_at: str
    stop_reason: str | None
    attempts: tuple[WorkerAttempt, ...]


@dataclass(frozen=True, slots=True)
class WorkItem:
    work_item_id: str
    run_id: str
    logical_key: str
    work_kind: str
    route_id: str | None
    parent_work_item_id: str | None
    assignment_summary: str
    assignment_artifact_ids: tuple[str, ...]
    input_artifact_ids: tuple[str, ...]
    created_at: str
    aggregate_state: str
    worker_runs: tuple[WorkerRun, ...]


class WorkActivityStore:
    """Own work execution metadata while ActivityStore remains the only event cursor owner."""

    def __init__(
        self,
        *,
        db_path: Path,
        activities: ActivityStore,
        id_generator: Callable[[], str],
        clock: Callable[[], str],
        busy_timeout_ms: int = 5_000,
    ) -> None:
        self._db_path = Path(db_path)
        self._activities = activities
        self._ids = id_generator
        self._clock = clock
        self._busy_timeout_ms = busy_timeout_ms

    def create_work_item(
        self,
        *,
        run_id: str,
        logical_key: str,
        work_kind: str,
        assignment_summary: str,
        research_revision: int,
        route_id: str | None = None,
        parent_work_item_id: str | None = None,
        assignment_artifact_ids: Sequence[str] = (),
        input_artifact_ids: Sequence[str] = (),
    ) -> WorkItem:
        if (
            not run_id
            or not logical_key
            or not work_kind
            or not assignment_summary
            or research_revision < 0
        ):
            raise ValueError("invalid work item declaration")
        assignments = _ids(assignment_artifact_ids)
        inputs = _ids(input_artifact_ids)
        now = self._clock()
        work_item_id = self._ids()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                _WORK_SELECT + " WHERE run_id=? AND logical_key=?", (run_id, logical_key)
            ).fetchone()
            if existing is not None:
                if (
                    str(existing[2]) != work_kind
                    or _optional(existing[3]) != route_id
                    or _optional(existing[4]) != parent_work_item_id
                    or str(existing[5]) != assignment_summary
                    or _json_ids(existing[6]) != assignments
                    or _json_ids(existing[7]) != inputs
                ):
                    raise WorkActivityError(
                        "work logical key is already bound to another immutable declaration"
                    )
                connection.commit()
                return self.get_work_item(str(existing[0]))
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
            self._append_activity(
                connection,
                run_id=run_id,
                research_revision=research_revision,
                source="ORCHESTRATOR",
                event_type="WORK_ITEM_CREATED",
                entity_refs={"work_item_id": work_item_id},
                payload={"work_kind": work_kind, "route_id": route_id},
                recorded_at=now,
            )
            connection.commit()
        return self.get_work_item(work_item_id)

    def assign_worker(
        self,
        work_item_id: str,
        *,
        worker_kind: str,
        role_id: str,
        process_token: str,
        budget_plan: Mapping[str, Any],
        research_revision: int,
        parent_worker_run_id: str | None = None,
        checkpoint_id: str | None = None,
    ) -> WorkerRun:
        if not worker_kind or not role_id or not process_token or research_revision < 0:
            raise ValueError("invalid worker assignment")
        now = self._clock()
        worker_run_id = self._ids()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            work = connection.execute(
                "SELECT run_id FROM product_work_items WHERE work_item_id=?", (work_item_id,)
            ).fetchone()
            if work is None:
                raise KeyError(work_item_id)
            run_id = str(work[0])
            states = [
                str(row[0])
                for row in connection.execute(
                    "SELECT state FROM product_worker_runs WHERE work_item_id=? ORDER BY ordinal",
                    (work_item_id,),
                )
            ]
            if "COMPLETED" in states:
                raise WorkActivityError("completed work items cannot be reassigned")
            if parent_worker_run_id is not None:
                parent = connection.execute(
                    "SELECT wi.run_id FROM product_worker_runs wr JOIN product_work_items wi "
                    "ON wi.work_item_id=wr.work_item_id WHERE wr.worker_run_id=?",
                    (parent_worker_run_id,),
                ).fetchone()
                if parent is None or str(parent[0]) != run_id:
                    raise WorkActivityError(
                        "parent worker run must belong to the same research run"
                    )
            ordinal = len(states) + 1
            connection.execute(
                "INSERT INTO product_worker_runs("
                "worker_run_id,work_item_id,ordinal,worker_kind,role_id,parent_worker_run_id,"
                "state,process_token,budget_plan_json,checkpoint_id,enqueued_at,last_activity_at) "
                "VALUES(?,?,?,?,?,?,'QUEUED',?,?,?,?,?)",
                (
                    worker_run_id,
                    work_item_id,
                    ordinal,
                    worker_kind,
                    role_id,
                    parent_worker_run_id,
                    process_token,
                    _json(dict(budget_plan)),
                    checkpoint_id,
                    now,
                    now,
                ),
            )
            self._append_activity(
                connection,
                run_id=run_id,
                research_revision=research_revision,
                source="ORCHESTRATOR",
                event_type="WORKER_ENQUEUED",
                entity_refs={"work_item_id": work_item_id, "worker_run_id": worker_run_id},
                payload={
                    "worker_kind": worker_kind,
                    "role_id": role_id,
                    "ordinal": ordinal,
                    "checkpoint_id": checkpoint_id,
                },
                recorded_at=now,
            )
            connection.commit()
        return self._worker(worker_run_id)

    def recover_pending_worker(
        self,
        worker_run_id: str,
        *,
        process_token: str,
        research_revision: int,
    ) -> WorkerRun:
        now = self._clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._worker_row(connection, worker_run_id)
            if str(row[6]) in _WORKER_TERMINAL or str(row[7]) != process_token:
                raise WorkActivityError("only the exact pending process can recover a worker run")
            connection.execute(
                "UPDATE product_worker_runs SET last_activity_at=? WHERE worker_run_id=?",
                (now, worker_run_id),
            )
            run_id = self._run_id(connection, str(row[1]))
            self._append_activity(
                connection,
                run_id=run_id,
                research_revision=research_revision,
                source="ORCHESTRATOR",
                event_type="WORKER_RECOVERED",
                entity_refs={"work_item_id": str(row[1]), "worker_run_id": worker_run_id},
                payload={"state": str(row[6])},
                recorded_at=now,
            )
            connection.commit()
        return self._worker(worker_run_id)

    def set_worker_state(
        self,
        worker_run_id: str,
        *,
        state: str,
        research_revision: int,
        stop_reason: str | None = None,
    ) -> WorkerRun:
        now = self._clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._worker_row(connection, worker_run_id)
            before = str(row[6])
            if state not in _WORKER_TRANSITIONS.get(before, frozenset()):
                raise WorkActivityError(f"worker transition {before}->{state} is not allowed")
            started_at = now if before == "QUEUED" and state == "RUNNING" else row[11]
            finished_at = now if state in _WORKER_TERMINAL else None
            connection.execute(
                "UPDATE product_worker_runs SET state=?,started_at=?,finished_at=?,"
                "last_activity_at=?,stop_reason=? WHERE worker_run_id=? AND state=?",
                (
                    state,
                    started_at,
                    finished_at,
                    now,
                    stop_reason,
                    worker_run_id,
                    before,
                ),
            )
            run_id = self._run_id(connection, str(row[1]))
            self._append_activity(
                connection,
                run_id=run_id,
                research_revision=research_revision,
                source="ORCHESTRATOR",
                event_type=_worker_event(state),
                entity_refs={"work_item_id": str(row[1]), "worker_run_id": worker_run_id},
                payload={"state": state, "stop_reason": stop_reason},
                recorded_at=now,
            )
            connection.commit()
        return self._worker(worker_run_id)

    def begin_attempt(
        self,
        worker_run_id: str,
        *,
        process_token: str,
        research_revision: int,
    ) -> WorkerAttempt:
        if not process_token or research_revision < 0:
            raise ValueError("invalid host attempt declaration")
        now = self._clock()
        attempt_id = self._ids()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            worker = self._worker_row(connection, worker_run_id)
            if str(worker[6]) not in {"RUNNING", "WAITING_TOOL"}:
                raise WorkActivityError("host attempts require an active worker run")
            ordinal_row = connection.execute(
                "SELECT COALESCE(MAX(ordinal),0)+1 FROM product_worker_attempts "
                "WHERE worker_run_id=?",
                (worker_run_id,),
            ).fetchone()
            assert ordinal_row is not None
            ordinal = int(ordinal_row[0])
            connection.execute(
                "INSERT INTO product_worker_attempts("
                "attempt_id,worker_run_id,ordinal,process_token,state,started_at) "
                "VALUES(?,?,?,?,'RUNNING',?)",
                (attempt_id, worker_run_id, ordinal, process_token, now),
            )
            run_id = self._run_id(connection, str(worker[1]))
            self._append_activity(
                connection,
                run_id=run_id,
                research_revision=research_revision,
                source="HOST",
                event_type="ATTEMPT_STARTED",
                entity_refs={
                    "work_item_id": str(worker[1]),
                    "worker_run_id": worker_run_id,
                    "attempt_id": attempt_id,
                },
                payload={"ordinal": ordinal, "state": "RUNNING"},
                recorded_at=now,
            )
            connection.commit()
        return self._attempt(attempt_id)

    def recover_pending_attempt(
        self,
        attempt_id: str,
        *,
        process_token: str,
        research_revision: int,
    ) -> WorkerAttempt:
        now = self._clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._attempt_row(connection, attempt_id)
            if str(row[4]) != "RUNNING" or str(row[3]) != process_token:
                raise WorkActivityError(
                    "only the exact pending host process can recover an attempt"
                )
            worker = self._worker_row(connection, str(row[1]))
            run_id = self._run_id(connection, str(worker[1]))
            self._append_activity(
                connection,
                run_id=run_id,
                research_revision=research_revision,
                source="HOST",
                event_type="ATTEMPT_RECOVERED",
                entity_refs={
                    "work_item_id": str(worker[1]),
                    "worker_run_id": str(row[1]),
                    "attempt_id": attempt_id,
                },
                payload={"ordinal": int(row[2]), "state": "RUNNING"},
                recorded_at=now,
            )
            connection.commit()
        return self._attempt(attempt_id)

    def finish_attempt(
        self,
        attempt_id: str,
        *,
        state: str,
        research_revision: int,
        exit_code: int | None = None,
        diagnostic_code: str | None = None,
        output_artifact_ids: Sequence[str] = (),
    ) -> WorkerAttempt:
        if state not in _ATTEMPT_TERMINAL:
            raise ValueError("attempt outcome must be terminal")
        outputs = _ids(output_artifact_ids)
        now = self._clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = self._attempt_row(connection, attempt_id)
            if str(row[4]) != "RUNNING":
                raise WorkActivityError("only a running attempt can receive an outcome")
            connection.execute(
                "UPDATE product_worker_attempts SET state=?,finished_at=?,exit_code=?,"
                "diagnostic_code=?,output_artifact_ids_json=? "
                "WHERE attempt_id=? AND state='RUNNING'",
                (state, now, exit_code, diagnostic_code, _json(outputs), attempt_id),
            )
            worker = self._worker_row(connection, str(row[1]))
            run_id = self._run_id(connection, str(worker[1]))
            self._append_activity(
                connection,
                run_id=run_id,
                research_revision=research_revision,
                source="HOST",
                event_type=f"ATTEMPT_{state}",
                entity_refs={
                    "work_item_id": str(worker[1]),
                    "worker_run_id": str(row[1]),
                    "attempt_id": attempt_id,
                },
                payload={
                    "ordinal": int(row[2]),
                    "state": state,
                    "exit_code": exit_code,
                    "diagnostic_code": diagnostic_code,
                    "output_artifact_ids": list(outputs),
                },
                recorded_at=now,
            )
            connection.commit()
        return self._attempt(attempt_id)

    def record_public_activity(
        self,
        worker_run_id: str,
        *,
        event_type: str,
        raw_payload: Mapping[str, Any],
        research_revision: int,
    ) -> int:
        payload = public_activity_payload(event_type, raw_payload)
        now = self._clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            worker = self._worker_row(connection, worker_run_id)
            run_id = self._run_id(connection, str(worker[1]))
            cursor = self._append_activity(
                connection,
                run_id=run_id,
                research_revision=research_revision,
                source="ORCHESTRATOR",
                event_type=event_type,
                entity_refs={
                    "work_item_id": str(worker[1]),
                    "worker_run_id": worker_run_id,
                },
                payload=payload,
                recorded_at=now,
            )
            connection.execute(
                "UPDATE product_worker_runs SET last_activity_at=? WHERE worker_run_id=?",
                (now, worker_run_id),
            )
            connection.commit()
            return cursor

    def get_work_item(self, work_item_id: str) -> WorkItem:
        with self._connect() as connection:
            row = connection.execute(
                _WORK_SELECT + " WHERE work_item_id=?", (work_item_id,)
            ).fetchone()
            if row is None:
                raise KeyError(work_item_id)
            worker_ids = [
                str(item[0])
                for item in connection.execute(
                    "SELECT worker_run_id FROM product_worker_runs WHERE work_item_id=? "
                    "ORDER BY ordinal",
                    (work_item_id,),
                )
            ]
        workers = tuple(self._worker(worker_id) for worker_id in worker_ids)
        return WorkItem(
            work_item_id=str(row[0]),
            run_id=str(row[1]),
            logical_key=str(row[9]),
            work_kind=str(row[2]),
            route_id=_optional(row[3]),
            parent_work_item_id=_optional(row[4]),
            assignment_summary=str(row[5]),
            assignment_artifact_ids=_json_ids(row[6]),
            input_artifact_ids=_json_ids(row[7]),
            created_at=str(row[8]),
            aggregate_state=_aggregate(workers),
            worker_runs=workers,
        )

    def _worker(self, worker_run_id: str) -> WorkerRun:
        with self._connect() as connection:
            row = self._worker_row(connection, worker_run_id)
            attempt_ids = [
                str(item[0])
                for item in connection.execute(
                    "SELECT attempt_id FROM product_worker_attempts WHERE worker_run_id=? "
                    "ORDER BY ordinal",
                    (worker_run_id,),
                )
            ]
        return WorkerRun(
            worker_run_id=str(row[0]),
            work_item_id=str(row[1]),
            ordinal=int(row[2]),
            worker_kind=str(row[3]),
            role_id=str(row[4]),
            parent_worker_run_id=_optional(row[5]),
            state=str(row[6]),
            checkpoint_id=_optional(row[9]),
            enqueued_at=str(row[10]),
            started_at=_optional(row[11]),
            finished_at=_optional(row[12]),
            last_activity_at=str(row[13]),
            stop_reason=_optional(row[14]),
            attempts=tuple(self._attempt(attempt_id) for attempt_id in attempt_ids),
        )

    def _attempt(self, attempt_id: str) -> WorkerAttempt:
        with self._connect() as connection:
            row = self._attempt_row(connection, attempt_id)
        return WorkerAttempt(
            attempt_id=str(row[0]),
            worker_run_id=str(row[1]),
            ordinal=int(row[2]),
            state=str(row[4]),
            started_at=str(row[5]),
            finished_at=_optional(row[6]),
            exit_code=int(row[7]) if row[7] is not None else None,
            diagnostic_code=_optional(row[8]),
            output_artifact_ids=_json_ids(row[9]),
        )

    def _append_activity(
        self,
        connection: sqlite3.Connection,
        *,
        run_id: str,
        research_revision: int,
        source: str,
        event_type: str,
        entity_refs: Mapping[str, Any],
        payload: Mapping[str, Any],
        recorded_at: str,
    ) -> int:
        return self._activities.append_in_transaction(
            connection,
            ProductActivity(
                event_id=self._ids(),
                scope_kind="RUN",
                run_id=run_id,
                source=source,
                research_revision=research_revision,
                entity_refs=dict(entity_refs),
                payload={"type": event_type, **dict(payload)},
                recorded_at=recorded_at,
            ),
        )

    @staticmethod
    def _worker_row(connection: sqlite3.Connection, worker_run_id: str) -> sqlite3.Row:
        row = connection.execute(
            _WORKER_SELECT + " WHERE worker_run_id=?", (worker_run_id,)
        ).fetchone()
        if row is None:
            raise KeyError(worker_run_id)
        return cast(sqlite3.Row, row)

    @staticmethod
    def _attempt_row(connection: sqlite3.Connection, attempt_id: str) -> sqlite3.Row:
        row = connection.execute(_ATTEMPT_SELECT + " WHERE attempt_id=?", (attempt_id,)).fetchone()
        if row is None:
            raise KeyError(attempt_id)
        return cast(sqlite3.Row, row)

    @staticmethod
    def _run_id(connection: sqlite3.Connection, work_item_id: str) -> str:
        row = connection.execute(
            "SELECT run_id FROM product_work_items WHERE work_item_id=?", (work_item_id,)
        ).fetchone()
        if row is None:
            raise KeyError(work_item_id)
        return str(row[0])

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self._db_path,
            timeout=self._busy_timeout_ms / 1_000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms}")
        return connection


def public_activity_payload(event_type: str, raw_payload: Mapping[str, Any]) -> dict[str, Any]:
    fields = _PUBLIC_FIELDS.get(event_type)
    if fields is None:
        raise WorkActivityError("orchestrator event type is not public")
    projected = {key: _public_value(key, raw_payload[key]) for key in fields if key in raw_payload}
    required = (
        {"diagnostic_code", "severity"}
        if event_type == "DIAGNOSTIC_RECORDED"
        else {"connector", "status", "result_count"}
    )
    if not required.issubset(projected):
        raise WorkActivityError("public orchestrator event is missing formal fields")
    return projected


def _public_value(key: str, value: Any) -> Any:
    if key in {"result_count", "duration_ms"}:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise WorkActivityError(f"public field {key} must be a non-negative integer")
        return value
    if key == "exit_code":
        if isinstance(value, bool) or not isinstance(value, int):
            raise WorkActivityError("public field exit_code must be an integer")
        return value
    if key == "artifact_ids":
        if not isinstance(value, (list, tuple)):
            raise WorkActivityError("public field artifact_ids must be a string array")
        return list(_ids(value))
    if not isinstance(value, str) or not value:
        raise WorkActivityError(f"public field {key} must be a non-empty string")
    return value


def _worker_event(state: str) -> str:
    return {
        "RUNNING": "WORKER_STARTED",
        "WAITING_TOOL": "WORKER_WAITING_TOOL",
        "WAITING_REVIEW": "WORKER_WAITING_REVIEW",
        "PAUSED": "WORKER_PAUSED",
        "CANCEL_REQUESTED": "WORKER_CANCEL_REQUESTED",
        "COMPLETED": "WORKER_COMPLETED",
        "FAILED": "WORKER_FAILED",
        "CANCELLED": "WORKER_CANCELLED",
    }[state]


def _aggregate(workers: Sequence[WorkerRun]) -> str:
    if not workers:
        return "QUEUED"
    if any(worker.state == "COMPLETED" for worker in workers):
        return "COMPLETED"
    for worker in reversed(workers):
        if worker.state not in _WORKER_TERMINAL:
            return worker.state
    return workers[-1].state


def _ids(values: Sequence[Any]) -> tuple[str, ...]:
    if not all(isinstance(value, str) and value for value in values):
        raise ValueError("artifact identities must be non-empty strings")
    return tuple(str(value) for value in values)


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_ids(value: Any) -> tuple[str, ...]:
    parsed = json.loads(str(value))
    if not isinstance(parsed, list):
        raise WorkActivityError("stored identity collection is not an array")
    return _ids(parsed)


def _optional(value: Any) -> str | None:
    return str(value) if value is not None else None


_WORK_SELECT = (
    "SELECT work_item_id,run_id,work_kind,route_id,parent_work_item_id,"
    "assignment_summary,assignment_artifact_ids_json,input_artifact_ids_json,created_at,"
    "logical_key FROM product_work_items"
)
_WORKER_SELECT = (
    "SELECT worker_run_id,work_item_id,ordinal,worker_kind,role_id,parent_worker_run_id,"
    "state,process_token,budget_plan_json,checkpoint_id,enqueued_at,started_at,finished_at,"
    "last_activity_at,stop_reason FROM product_worker_runs"
)
_ATTEMPT_SELECT = (
    "SELECT attempt_id,worker_run_id,ordinal,process_token,state,started_at,finished_at,"
    "exit_code,diagnostic_code,output_artifact_ids_json FROM product_worker_attempts"
)


__all__ = [
    "WorkActivityError",
    "WorkActivityStore",
    "WorkItem",
    "WorkerAttempt",
    "WorkerRun",
    "public_activity_payload",
]
