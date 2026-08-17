"""Cursor-strict delivery of persisted product activity for the SSE adapter."""

from __future__ import annotations

from collections.abc import Callable, Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from rk.product.activity_store import ActivityRecord, ActivityStore
from rk.sqlite import open_sqlite


class ActivityStreamError(RuntimeError):
    """A subscription cursor or persisted activity boundary is invalid."""


class CursorExpired(ActivityStreamError):
    """The client cursor predates the retained activity window."""

    code = "CURSOR_EXPIRED"
    http_status = 410


@dataclass(frozen=True, slots=True)
class ActivityScope:
    run_id: str | None = None
    deployment_id: str | None = None

    def __post_init__(self) -> None:
        if (self.run_id is None) == (self.deployment_id is None):
            raise ValueError("activity stream requires exactly one scope id")


@dataclass(frozen=True, slots=True)
class StreamFrame:
    event: str
    event_id: str | None
    data: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class BacklogPage:
    frames: tuple[StreamFrame, ...]
    scanned_through_cursor: int
    has_more: bool


class PersistedActivityStream:
    """Drain the durable backlog before a transport waits for later activity."""

    def __init__(
        self,
        db_path: Path,
        store: ActivityStore,
        scope: ActivityScope,
        *,
        after_cursor: int | None = None,
        last_event_id: str | None = None,
        clock: Callable[[], str],
        busy_timeout_ms: int = 5_000,
    ) -> None:
        self._db_path = Path(db_path)
        self._store = store
        self._scope = scope
        self._cursor = resolve_after_cursor(after_cursor, last_event_id)
        self._clock = clock
        self._busy_timeout_ms = busy_timeout_ms
        self._closed = False
        self._connection = open_sqlite(
            self._db_path,
            timeout=self._busy_timeout_ms / 1_000,
            check_same_thread=False,
        )
        self._ensure_available(self._cursor)

    @property
    def after_cursor(self) -> int:
        return self._cursor

    def drain_backlog(self, *, limit: int = 200) -> BacklogPage:
        if self._closed:
            raise ActivityStreamError("activity stream is closed")
        self._ensure_available(self._cursor)
        snapshot = self._store.snapshot(
            after_cursor=self._cursor,
            limit=limit,
            run_id=self._scope.run_id,
            deployment_id=self._scope.deployment_id,
        )
        records = snapshot.records
        if records:
            previous = self._cursor
            for record in records:
                if record.cursor <= previous:
                    raise ActivityStreamError("activity cursor did not increase")
                previous = record.cursor
            scanned = records[-1].cursor if len(records) == limit else snapshot.last_cursor
        else:
            scanned = snapshot.last_cursor
        self._ensure_available(self._cursor)
        self._cursor = max(self._cursor, scanned)
        return BacklogPage(
            frames=tuple(_activity_frame(record) for record in records),
            scanned_through_cursor=self._cursor,
            has_more=len(records) == limit and records[-1].cursor < snapshot.last_cursor,
        )

    def heartbeat(self) -> StreamFrame:
        if self._closed:
            raise ActivityStreamError("activity stream is closed")
        return StreamFrame(
            event="heartbeat",
            event_id=None,
            data=MappingProxyType(
                {
                    "schema_version": "rk.product.heartbeat.v1",
                    "server_time": self._clock(),
                    "after_cursor": self._cursor,
                }
            ),
        )

    def __iter__(self) -> Iterator[StreamFrame]:
        while True:
            page = self.drain_backlog()
            yield from page.frames
            if not page.has_more:
                return

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._connection.close()

    def _ensure_available(self, after_cursor: int) -> None:
        row = self._connection.execute(
            "SELECT first_available_cursor FROM product_activity_retention WHERE singleton=1"
        ).fetchone()
        if row is None:
            raise ActivityStreamError("activity retention watermark is missing")
        first_available = int(row[0])
        if after_cursor < first_available - 1:
            raise CursorExpired(
                f"cursor {after_cursor} predates first available cursor {first_available}"
            )


def resolve_after_cursor(after_cursor: int | None, last_event_id: str | None) -> int:
    parsed_header: int | None = None
    if last_event_id is not None:
        if not last_event_id.isascii() or not last_event_id.isdecimal():
            raise ValueError("Last-Event-ID must be a non-negative decimal cursor")
        parsed_header = int(last_event_id)
    if after_cursor is not None and after_cursor < 0:
        raise ValueError("after_cursor must be non-negative")
    if after_cursor is not None and parsed_header is not None and after_cursor != parsed_header:
        raise ValueError("after_cursor conflicts with Last-Event-ID")
    if parsed_header is not None:
        return parsed_header
    if after_cursor is not None:
        return after_cursor
    raise ValueError("after_cursor or Last-Event-ID is required")


def _activity_frame(record: ActivityRecord) -> StreamFrame:
    scope: dict[str, Any] = {"kind": record.scope_kind}
    if record.run_id is not None:
        scope["run_id"] = record.run_id
    if record.deployment_id is not None:
        scope["deployment_id"] = record.deployment_id
    body: dict[str, Any] = {
        "schema_version": "rk.product.activity.v1",
        "cursor": record.cursor,
        "event_id": record.event_id,
        "scope": scope,
        "source": record.source,
        "recorded_at": record.recorded_at,
        "entity_refs": dict(record.entity_refs),
        "payload": dict(record.payload),
    }
    if record.research_revision is not None:
        body["research_revision"] = record.research_revision
    if record.kernel_event_id is not None:
        body["kernel_event_id"] = record.kernel_event_id
    return StreamFrame(
        event="activity",
        event_id=str(record.cursor),
        data=MappingProxyType(body),
    )


__all__ = [
    "ActivityScope",
    "ActivityStreamError",
    "BacklogPage",
    "CursorExpired",
    "PersistedActivityStream",
    "StreamFrame",
    "resolve_after_cursor",
]
