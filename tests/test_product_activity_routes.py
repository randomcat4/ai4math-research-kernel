from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from types import MappingProxyType

from rk.extensions import ProductActivity
from rk.http_shell import (
    HttpErrorClass,
    HttpRequest,
    HttpResponse,
    HttpResult,
    HttpStreamResponse,
    ProductHttpError,
    RouteRegistry,
    SessionPrincipal,
    SessionRequest,
)
from rk.product.activity_routes import (
    ActivityRouter,
    SseActivityBody,
    activity_router_factory,
    encode_sse_frame,
)
from rk.product.activity_store import ActivityStore
from rk.product.activity_stream import StreamFrame
from rk.product_migrations import ProductMigrationAssembler, ProductMigrationRegistry


class Authorizer:
    def __init__(self, *, deny: bool = False) -> None:
        self.deny = deny
        self.calls: list[tuple[str, str]] = []

    def authorize_subscription(self, principal: SessionPrincipal, run_id: str) -> None:
        if self.deny:
            raise ProductHttpError("EVENTS_FORBIDDEN", HttpErrorClass.AUTHORIZATION, "$.run_id")
        self.calls.append((principal.subject_id, run_id))


def database(tmp_path: Path) -> Path:
    path = tmp_path / "product.sqlite"
    with sqlite3.connect(path, isolation_level=None) as connection:
        ProductMigrationAssembler(ProductMigrationRegistry(Path("schema_fragments"))).apply(
            connection
        )
    return path


def activity(event_id: str, run_id: str, revision: int, event_type: str) -> ProductActivity:
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


def principal() -> SessionPrincipal:
    return SessionPrincipal("session-1", "subject-1", ("capability-1",))


def invoke(router: ActivityRouter, request: HttpRequest) -> HttpResult:
    route = router.routes()[0]

    async def call() -> HttpResult:
        return await route.handler(SessionRequest(request, principal()))

    return asyncio.run(call())


def router(
    path: Path,
    store: ActivityStore,
    authorizer: Authorizer,
    *,
    page_size: int = 200,
    heartbeat_interval: float = 15.0,
    wait: object = None,
    monotonic: object = None,
) -> ActivityRouter:
    kwargs: dict[str, object] = {}
    if wait is not None:
        kwargs["wait"] = wait
    if monotonic is not None:
        kwargs["monotonic"] = monotonic
    return activity_router_factory(
        db_path=path,
        store=store,
        authorizer=authorizer,
        clock=lambda: "2026-08-13T00:01:00Z",
        page_size=page_size,
        poll_interval_seconds=0.5,
        heartbeat_interval_seconds=heartbeat_interval,
        **kwargs,  # type: ignore[arg-type]
    )


def stream_body(response: HttpResult) -> SseActivityBody:
    assert isinstance(response, HttpStreamResponse)
    assert isinstance(response.body, SseActivityBody)
    return response.body


def sse_data(frame: bytes) -> dict[str, object]:
    line = next(item for item in frame.decode().splitlines() if item.startswith("data: "))
    value = json.loads(line.removeprefix("data: "))
    assert isinstance(value, dict)
    return value


def test_snapshot_fence_drains_concurrent_backlog_as_real_sse_bytes(tmp_path: Path) -> None:
    path = database(tmp_path)
    store = ActivityStore(path)
    store.append(activity("event-1", "run-1", 7, "SNAPSHOT_VISIBLE"))
    snapshot_cursor = store.snapshot(run_id="run-1").last_cursor
    store.append(activity("event-2", "run-1", 3, "OLDER_REVISION_FINISHED"))
    authorizer = Authorizer()
    response = invoke(
        router(path, store, authorizer),
        HttpRequest("GET", f"/v1/research/run-1/events?after_cursor={snapshot_cursor}"),
    )

    assert isinstance(response, HttpStreamResponse)
    assert response.status == 200
    assert response.headers["content-type"] == "text/event-stream; charset=utf-8"
    assert "content-length" not in response.headers
    body = stream_body(response)
    frame = next(iter(body))
    assert frame.startswith(b"id: 2\nevent: activity\ndata: ")
    assert frame.endswith(b"\n\n")
    assert sse_data(frame)["research_revision"] == 3
    assert sse_data(frame)["payload"] == {"type": "OLDER_REVISION_FINISHED"}
    assert authorizer.calls == [("subject-1", "run-1")]
    body.close()


def test_backlog_pages_preserve_strict_cursor_order_in_one_sse_stream(tmp_path: Path) -> None:
    path = database(tmp_path)
    store = ActivityStore(path)
    for index in range(1, 4):
        store.append(activity(f"event-{index}", "run-1", 1, f"EVENT_{index}"))
    response = invoke(
        router(path, store, Authorizer(), page_size=1),
        HttpRequest("GET", "/v1/research/run-1/events?after_cursor=0"),
    )
    body = stream_body(response)
    iterator = iter(body)

    frames = [next(iterator), next(iterator), next(iterator)]

    assert [sse_data(frame)["cursor"] for frame in frames] == [1, 2, 3]
    assert [frame.splitlines()[0] for frame in frames] == [b"id: 1", b"id: 2", b"id: 3"]
    body.close()


def test_last_event_id_disconnect_reconnect_resumes_without_duplicate(tmp_path: Path) -> None:
    path = database(tmp_path)
    store = ActivityStore(path)
    store.append(activity("event-1", "run-1", 1, "ONE"))
    store.append(activity("event-2", "run-1", 1, "TWO"))
    first_response = invoke(
        router(path, store, Authorizer(), page_size=1),
        HttpRequest("GET", "/v1/research/run-1/events?after_cursor=0"),
    )
    first = stream_body(first_response)
    assert next(iter(first)).startswith(b"id: 1\n")
    first.close()

    resumed_response = invoke(
        router(path, store, Authorizer()),
        HttpRequest(
            "GET",
            "/v1/research/run-1/events",
            headers={"Last-Event-ID": "1"},
        ),
    )
    resumed = stream_body(resumed_response)
    frame = next(iter(resumed))
    assert frame.startswith(b"id: 2\n")
    assert sse_data(frame)["event_id"] == "event-2"
    resumed.close()


def test_heartbeat_has_no_id_and_does_not_advance_cursor(tmp_path: Path) -> None:
    path = database(tmp_path)
    store = ActivityStore(path)
    response = invoke(
        router(path, store, Authorizer(), heartbeat_interval=0),
        HttpRequest("GET", "/v1/research/run-1/events?after_cursor=0"),
    )
    body = stream_body(response)
    frame = next(iter(body))

    assert frame.startswith(b"event: heartbeat\n")
    assert b"\nid:" not in frame and not frame.startswith(b"id:")
    assert sse_data(frame)["after_cursor"] == 0
    assert response.headers["x-rk-after-cursor"] == "0"
    body.close()


def test_idle_sqlite_poll_observes_new_persisted_event_without_json_polling(
    tmp_path: Path,
) -> None:
    path = database(tmp_path)
    store = ActivityStore(path)
    waits: list[float] = []

    def wait(seconds: float) -> None:
        waits.append(seconds)
        if len(waits) == 1:
            store.append(activity("event-1", "run-1", 1, "AFTER_CONNECT"))

    response = invoke(
        router(
            path,
            store,
            Authorizer(),
            heartbeat_interval=100,
            wait=wait,
            monotonic=lambda: 0.0,
        ),
        HttpRequest("GET", "/v1/research/run-1/events?after_cursor=0"),
    )
    body = stream_body(response)
    frame = next(iter(body))

    assert waits == [0.5]
    assert frame.startswith(b"id: 1\nevent: activity\n")
    assert sse_data(frame)["payload"] == {"type": "AFTER_CONNECT"}
    body.close()


def test_expired_cursor_returns_410_before_stream_opens(tmp_path: Path) -> None:
    path = database(tmp_path)
    store = ActivityStore(path)
    for index in range(1, 4):
        store.append(activity(f"event-{index}", "run-1", 1, f"EVENT_{index}"))
    with sqlite3.connect(path) as connection:
        connection.execute("DELETE FROM product_activity_events WHERE cursor < 3")
        connection.execute(
            "UPDATE product_activity_retention SET first_available_cursor=3,updated_at='now' "
            "WHERE singleton=1"
        )
    response = invoke(
        router(path, store, Authorizer()),
        HttpRequest("GET", "/v1/research/run-1/events?after_cursor=1"),
    )

    assert isinstance(response, HttpResponse)
    assert response.status == 410
    assert response.body["code"] == "CURSOR_EXPIRED"
    assert response.headers["content-type"] == "application/json"


def test_cursor_conflict_and_session_scope_denial_are_explicit(tmp_path: Path) -> None:
    path = database(tmp_path)
    store = ActivityStore(path)
    conflict = invoke(
        router(path, store, Authorizer()),
        HttpRequest(
            "GET",
            "/v1/research/run-1/events?after_cursor=1",
            headers={"last-event-id": "2"},
        ),
    )
    assert isinstance(conflict, HttpResponse)
    assert conflict.status == 400
    assert conflict.body["code"] == "SUBSCRIPTION_CURSOR_INVALID"

    forbidden = invoke(
        router(path, store, Authorizer(deny=True)),
        HttpRequest("GET", "/v1/research/run-1/events?after_cursor=0"),
    )
    assert isinstance(forbidden, HttpResponse)
    assert forbidden.status == 403
    assert forbidden.body["code"] == "EVENTS_FORBIDDEN"


def test_nested_read_only_mappings_are_losslessly_encoded_as_sse_json() -> None:
    frame = StreamFrame(
        event="activity",
        event_id="9",
        data=MappingProxyType(
            {
                "nested": MappingProxyType({"label": "赵", "count": 2}),
                "items": (MappingProxyType({"ok": True}), None),
            }
        ),
    )

    encoded = encode_sse_frame(frame)

    assert encoded.startswith(b"id: 9\nevent: activity\ndata: ")
    assert sse_data(encoded) == {
        "items": [{"ok": True}, None],
        "nested": {"count": 2, "label": "赵"},
    }
    assert encoded.endswith(b"\n\n")


def test_activity_router_contributes_only_the_contract_sse_route(tmp_path: Path) -> None:
    path = database(tmp_path)
    registry = RouteRegistry()
    registry.register(router(path, ActivityStore(path), Authorizer()))
    assert [(route.method, route.path) for route in registry.routes] == [
        ("GET", "/v1/research/{run_id}/events")
    ]
