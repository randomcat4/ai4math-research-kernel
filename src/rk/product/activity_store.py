"""One durable cursor space for kernel, host, worker and tool product activity."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rk.extensions import ProductActivity


class ActivityStoreError(RuntimeError):
    """Activity persistence or cursor invariants were violated."""


@dataclass(frozen=True, slots=True)
class ActivityRecord:
    cursor: int
    event_id: str
    scope_kind: str
    source: str
    recorded_at: str
    payload: Mapping[str, Any]
    entity_refs: Mapping[str, Any]
    run_id: str | None
    deployment_id: str | None
    research_revision: int | None
    kernel_event_id: str | None


@dataclass(frozen=True, slots=True)
class ActivitySnapshot:
    last_cursor: int
    records: tuple[ActivityRecord, ...]


class ActivityStore:
    def __init__(
        self,
        db_path: Path,
        *,
        busy_timeout_ms: int = 5_000,
        connection_factory: Callable[[], sqlite3.Connection] | None = None,
    ) -> None:
        self._db_path = Path(db_path)
        self._busy_timeout_ms = busy_timeout_ms
        self._connection_factory = connection_factory

    def append_in_transaction(
        self, connection: sqlite3.Connection, activity: ProductActivity
    ) -> int:
        self._validate_scope(activity)
        try:
            cursor = connection.execute(
                "INSERT INTO product_activity_events("
                "event_id,scope_kind,run_id,deployment_id,source,research_revision,"
                "kernel_event_id,entity_refs,payload_json,recorded_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?) RETURNING cursor",
                (
                    activity.event_id,
                    activity.scope_kind,
                    activity.run_id,
                    activity.deployment_id,
                    activity.source,
                    activity.research_revision,
                    activity.kernel_event_id,
                    _json(activity.entity_refs),
                    _json(activity.payload),
                    activity.recorded_at,
                ),
            ).fetchone()
        except sqlite3.IntegrityError as error:
            existing = connection.execute(
                "SELECT cursor,event_id,scope_kind,run_id,deployment_id,source,"
                "research_revision,kernel_event_id,entity_refs,payload_json,recorded_at "
                "FROM product_activity_events WHERE event_id=?",
                (activity.event_id,),
            ).fetchone()
            if existing is None or self._record(existing) != self._from_activity(
                activity, int(existing[0])
            ):
                raise ActivityStoreError(
                    "activity identity was reused with different content"
                ) from error
            return int(existing[0])
        if cursor is None:
            raise ActivityStoreError("activity insert did not allocate a cursor")
        return int(cursor[0])

    def append(self, activity: ProductActivity) -> int:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            cursor = self.append_in_transaction(connection, activity)
            connection.commit()
            return cursor

    def snapshot(
        self,
        *,
        after_cursor: int = 0,
        limit: int = 200,
        run_id: str | None = None,
        deployment_id: str | None = None,
    ) -> ActivitySnapshot:
        if after_cursor < 0 or not 1 <= limit <= 1_000:
            raise ValueError("invalid activity page")
        if run_id is not None and deployment_id is not None:
            raise ValueError("activity snapshot accepts one scope filter")
        where = ["cursor > ?"]
        params: list[object] = [after_cursor]
        if run_id is not None:
            where.append("run_id = ?")
            params.append(run_id)
        if deployment_id is not None:
            where.append("deployment_id = ?")
            params.append(deployment_id)
        params.append(limit)
        with self._connect() as connection:
            connection.execute("BEGIN")
            fence_row = connection.execute(
                "SELECT COALESCE(MAX(cursor),0) FROM product_activity_events"
            ).fetchone()
            rows = connection.execute(
                "SELECT cursor,event_id,scope_kind,run_id,deployment_id,source,"
                "research_revision,kernel_event_id,entity_refs,payload_json,recorded_at "
                f"FROM product_activity_events WHERE {' AND '.join(where)} "
                "ORDER BY cursor LIMIT ?",
                params,
            ).fetchall()
            connection.commit()
        if fence_row is None:
            raise ActivityStoreError("activity snapshot fence is unavailable")
        return ActivitySnapshot(int(fence_row[0]), tuple(self._record(row) for row in rows))

    def _connect(self) -> sqlite3.Connection:
        connection = (
            self._connection_factory()
            if self._connection_factory is not None
            else sqlite3.connect(self._db_path, timeout=self._busy_timeout_ms / 1_000)
        )
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
        return connection

    @staticmethod
    def _validate_scope(activity: ProductActivity) -> None:
        valid = (
            (
                activity.scope_kind == "GLOBAL"
                and activity.run_id is None
                and activity.deployment_id is None
            )
            or (
                activity.scope_kind == "RUN"
                and activity.run_id is not None
                and activity.deployment_id is None
            )
            or (
                activity.scope_kind == "DEPLOYMENT"
                and activity.run_id is None
                and activity.deployment_id is not None
            )
        )
        if not valid:
            raise ValueError("activity scope fields do not match scope_kind")

    @staticmethod
    def _record(row: tuple[object, ...]) -> ActivityRecord:
        return ActivityRecord(
            cursor=int(str(row[0])),
            event_id=str(row[1]),
            scope_kind=str(row[2]),
            run_id=str(row[3]) if row[3] is not None else None,
            deployment_id=str(row[4]) if row[4] is not None else None,
            source=str(row[5]),
            research_revision=int(str(row[6])) if row[6] is not None else None,
            kernel_event_id=str(row[7]) if row[7] is not None else None,
            entity_refs=_object(row[8]),
            payload=_object(row[9]),
            recorded_at=str(row[10]),
        )

    @staticmethod
    def _from_activity(activity: ProductActivity, cursor: int) -> ActivityRecord:
        return ActivityRecord(
            cursor,
            activity.event_id,
            activity.scope_kind,
            activity.source,
            activity.recorded_at,
            dict(activity.payload),
            dict(activity.entity_refs),
            activity.run_id,
            activity.deployment_id,
            activity.research_revision,
            activity.kernel_event_id,
        )


def _json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _object(value: object) -> dict[str, Any]:
    result = json.loads(str(value))
    if not isinstance(result, dict):
        raise ActivityStoreError("stored activity JSON is not an object")
    return result
