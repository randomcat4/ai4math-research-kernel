from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from rk.extensions import ProductActivity
from rk.product.activity_store import ActivityStore
from rk.product.activity_stream import (
    ActivityScope,
    ActivityStreamError,
    CursorExpired,
    PersistedActivityStream,
    resolve_after_cursor,
)
from rk.product_migrations import ProductMigrationAssembler, ProductMigrationRegistry


def _database(tmp_path: Path) -> Path:
    path = tmp_path / "product.sqlite"
    with sqlite3.connect(path, isolation_level=None) as connection:
        ProductMigrationAssembler(ProductMigrationRegistry(Path("schema_fragments"))).apply(connection)
    return path


def _activity(event_id: str, run_id: str, revision: int, event_type: str) -> ProductActivity:
    return ProductActivity(
        event_id=event_id,
        scope_kind="RUN",
        run_id=run_id,
        source="WORKER",
        research_revision=revision,
        entity_refs={"work_item_id": event_id},
        payload={"type": event_type},
        recorded_at="2026-08-13T00:00:00Z",
    )


def _stream(path: Path, store: ActivityStore, after_cursor: int) -> PersistedActivityStream:
    return PersistedActivityStream(
        path,
        store,
        ActivityScope(run_id="run-1"),
        after_cursor=after_cursor,
        clock=lambda: "2026-08-13T00:01:00Z",
    )


def test_snapshot_then_backlog_closes_the_concurrent_write_gap(tmp_path: Path) -> None:
    path = _database(tmp_path)
    store = ActivityStore(path)
    store.append(_activity("event-1", "run-1", 7, "SNAPSHOT_VISIBLE"))
    snapshot_cursor = store.snapshot(run_id="run-1").last_cursor
    store.append(_activity("event-2", "run-1", 3, "OLDER_REVISION_FINISHED"))

    page = _stream(path, store, snapshot_cursor).drain_backlog()

    assert [frame.event_id for frame in page.frames] == ["2"]
    assert page.frames[0].data["research_revision"] == 3
    assert page.frames[0].data["payload"] == {"type": "OLDER_REVISION_FINISHED"}
    assert page.scanned_through_cursor == 2


def test_backlog_is_cursor_ordered_and_run_cursor_may_skip_global_positions(
    tmp_path: Path,
) -> None:
    path = _database(tmp_path)
    store = ActivityStore(path)
    store.append(_activity("event-1", "run-1", 1, "ONE"))
    store.append(_activity("event-2", "run-2", 9, "OTHER_RUN"))
    store.append(_activity("event-3", "run-1", 1, "THREE"))
    stream = _stream(path, store, 0)

    first = stream.drain_backlog(limit=1)
    second = stream.drain_backlog(limit=1)

    assert [frame.event_id for frame in first.frames] == ["1"]
    assert first.has_more is True
    assert [frame.event_id for frame in second.frames] == ["3"]
    assert [frame.data["cursor"] for frame in (*first.frames, *second.frames)] == [1, 3]


def test_last_event_id_reconnect_resumes_strictly_after_delivered_cursor(tmp_path: Path) -> None:
    path = _database(tmp_path)
    store = ActivityStore(path)
    store.append(_activity("event-1", "run-1", 1, "ONE"))
    store.append(_activity("event-2", "run-1", 1, "TWO"))
    stream = PersistedActivityStream(
        path,
        store,
        ActivityScope(run_id="run-1"),
        last_event_id="1",
        clock=lambda: "2026-08-13T00:01:00Z",
    )

    assert [frame.event_id for frame in stream.drain_backlog().frames] == ["2"]
    assert resolve_after_cursor(2, "2") == 2
    with pytest.raises(ValueError, match="conflicts"):
        resolve_after_cursor(1, "2")
    with pytest.raises(ValueError, match="decimal"):
        resolve_after_cursor(None, "event-2")


def test_heartbeat_has_no_event_id_and_does_not_advance_cursor(tmp_path: Path) -> None:
    path = _database(tmp_path)
    stream = _stream(path, ActivityStore(path), 0)

    heartbeat = stream.heartbeat()

    assert heartbeat.event == "heartbeat"
    assert heartbeat.event_id is None
    assert heartbeat.data == {
        "schema_version": "rk.product.heartbeat.v1",
        "server_time": "2026-08-13T00:01:00Z",
        "after_cursor": 0,
    }
    assert stream.after_cursor == 0


def test_retention_watermark_returns_cursor_expired_instead_of_guessing(tmp_path: Path) -> None:
    path = _database(tmp_path)
    store = ActivityStore(path)
    store.append(_activity("event-1", "run-1", 1, "ONE"))
    store.append(_activity("event-2", "run-1", 1, "TWO"))
    store.append(_activity("event-3", "run-1", 1, "THREE"))
    with sqlite3.connect(path) as connection:
        connection.execute("DELETE FROM product_activity_events WHERE cursor < 3")
        connection.execute(
            "UPDATE product_activity_retention SET first_available_cursor=3,updated_at='now' "
            "WHERE singleton=1"
        )

    with pytest.raises(CursorExpired) as captured:
        _stream(path, store, 1)
    assert captured.value.code == "CURSOR_EXPIRED"
    assert captured.value.http_status == 410
    assert [frame.event_id for frame in _stream(path, store, 2).drain_backlog().frames] == ["3"]


def test_closed_stream_refuses_backlog_and_heartbeat(tmp_path: Path) -> None:
    path = _database(tmp_path)
    stream = _stream(path, ActivityStore(path), 0)
    stream.close()

    with pytest.raises(ActivityStreamError, match="closed"):
        stream.drain_backlog()
    with pytest.raises(ActivityStreamError, match="closed"):
        stream.heartbeat()