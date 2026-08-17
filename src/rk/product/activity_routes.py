"""Session-authorized SSE transport over the persisted product activity ledger."""

from __future__ import annotations

import json
import re
import time
from collections.abc import Callable, Iterator, Mapping, Sequence
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import parse_qs, urlsplit

from rk.http_shell import (
    HttpErrorClass,
    HttpResponse,
    HttpResult,
    HttpStreamResponse,
    ProductHttpError,
    RouteSpec,
    SessionPrincipal,
    SessionRequest,
    error_response,
)
from rk.product.activity_store import ActivityStore
from rk.product.activity_stream import (
    ActivityScope,
    ActivityStreamError,
    CursorExpired,
    PersistedActivityStream,
    StreamFrame,
)

_EVENT_PATH = re.compile(r"^/v1/research/([A-Za-z0-9_.:-]+)/events$")


class ActivityAccessAuthorizer(Protocol):
    def authorize_subscription(self, principal: SessionPrincipal, run_id: str) -> None: ...


class SseActivityBody:
    """Continuously encode persisted frames; idle polling emits cursor-free heartbeats."""

    def __init__(
        self,
        stream: PersistedActivityStream,
        *,
        page_size: int,
        poll_interval_seconds: float,
        heartbeat_interval_seconds: float,
        wait: Callable[[float], None],
        monotonic: Callable[[], float],
    ) -> None:
        if not 1 <= page_size <= 1_000:
            raise ValueError("SSE page_size must be between 1 and 1000")
        if not 0 < poll_interval_seconds <= 0.5:
            raise ValueError("SSE SQLite polling interval must be within (0, 0.5]")
        if heartbeat_interval_seconds < 0:
            raise ValueError("SSE heartbeat interval cannot be negative")
        self._stream = stream
        self._page_size = page_size
        self._poll_interval = poll_interval_seconds
        self._heartbeat_interval = heartbeat_interval_seconds
        self._wait = wait
        self._monotonic = monotonic
        self._last_heartbeat = monotonic()
        self._closed = False

    def __iter__(self) -> Iterator[bytes]:
        while not self._closed:
            page = self._stream.drain_backlog(limit=self._page_size)
            if page.frames:
                for frame in page.frames:
                    if self._closed:
                        return
                    yield encode_sse_frame(frame)
                continue
            now = self._monotonic()
            if now - self._last_heartbeat >= self._heartbeat_interval:
                self._last_heartbeat = now
                yield encode_sse_frame(self._stream.heartbeat())
                continue
            self._wait(self._poll_interval)

    def close(self) -> None:
        self._closed = True
        self._stream.close()


class ActivityRouter:
    def __init__(
        self,
        *,
        db_path: Path,
        store: ActivityStore,
        authorizer: ActivityAccessAuthorizer,
        clock: Callable[[], str],
        page_size: int = 200,
        poll_interval_seconds: float = 0.5,
        heartbeat_interval_seconds: float = 15.0,
        wait: Callable[[float], None] = time.sleep,
        monotonic: Callable[[], float] = time.monotonic,
    ) -> None:
        self._db_path = Path(db_path)
        self._store = store
        self._authorizer = authorizer
        self._clock = clock
        self._page_size = page_size
        self._poll_interval = poll_interval_seconds
        self._heartbeat_interval = heartbeat_interval_seconds
        self._wait = wait
        self._monotonic = monotonic

    def routes(self) -> Sequence[RouteSpec]:
        return (
            RouteSpec(
                method="GET",
                path="/v1/research/{run_id}/events",
                handler=self._subscribe,
                name="research-activity-sse",
            ),
        )

    async def _subscribe(self, request: SessionRequest) -> HttpResult:
        split = urlsplit(request.request.path)
        match = _EVENT_PATH.fullmatch(split.path)
        if match is None or split.fragment:
            return _problem("SUBSCRIPTION_PATH_INVALID", HttpErrorClass.SCHEMA, "$.path")
        run_id = match.group(1)
        try:
            self._authorizer.authorize_subscription(request.principal, run_id)
            after_cursor = _query_cursor(split.query)
            last_event_id = _header(request, "last-event-id")
            stream = PersistedActivityStream(
                self._db_path,
                self._store,
                ActivityScope(run_id=run_id),
                after_cursor=after_cursor,
                last_event_id=last_event_id,
                clock=self._clock,
            )
            body = SseActivityBody(
                stream,
                page_size=self._page_size,
                poll_interval_seconds=self._poll_interval,
                heartbeat_interval_seconds=self._heartbeat_interval,
                wait=self._wait,
                monotonic=self._monotonic,
            )
        except CursorExpired:
            return _problem("CURSOR_EXPIRED", HttpErrorClass.GONE, "$.after_cursor")
        except ValueError:
            return _problem("SUBSCRIPTION_CURSOR_INVALID", HttpErrorClass.SCHEMA, "$.after_cursor")
        except ProductHttpError as error:
            return error_response(error)
        except ActivityStreamError:
            return _problem("ACTIVITY_STREAM_UNAVAILABLE", HttpErrorClass.UNAVAILABLE, "$.events")
        return HttpStreamResponse(
            status=200,
            body=body,
            headers={
                "content-type": "text/event-stream; charset=utf-8",
                "cache-control": "no-cache",
                "connection": "keep-alive",
                "x-accel-buffering": "no",
                "x-rk-after-cursor": str(stream.after_cursor),
            },
        )


def activity_router_factory(
    *,
    db_path: Path,
    store: ActivityStore,
    authorizer: ActivityAccessAuthorizer,
    clock: Callable[[], str],
    page_size: int = 200,
    poll_interval_seconds: float = 0.5,
    heartbeat_interval_seconds: float = 15.0,
    wait: Callable[[float], None] = time.sleep,
    monotonic: Callable[[], float] = time.monotonic,
) -> ActivityRouter:
    return ActivityRouter(
        db_path=db_path,
        store=store,
        authorizer=authorizer,
        clock=clock,
        page_size=page_size,
        poll_interval_seconds=poll_interval_seconds,
        heartbeat_interval_seconds=heartbeat_interval_seconds,
        wait=wait,
        monotonic=monotonic,
    )


def encode_sse_frame(frame: StreamFrame) -> bytes:
    lines: list[str] = []
    if frame.event_id is not None:
        lines.append(f"id: {frame.event_id}")
    lines.append(f"event: {frame.event}")
    data = json.dumps(
        _json_tree(frame.data), ensure_ascii=False, sort_keys=True, separators=(",", ":")
    )
    lines.append(f"data: {data}")
    return ("\n".join(lines) + "\n\n").encode("utf-8")


def _query_cursor(query: str) -> int | None:
    if not query:
        return None
    try:
        values = parse_qs(query, keep_blank_values=True, strict_parsing=True)
    except ValueError as error:
        raise ValueError("subscription query is invalid") from error
    if set(values) != {"after_cursor"} or len(values["after_cursor"]) != 1:
        raise ValueError("subscription query accepts one after_cursor")
    raw = values["after_cursor"][0]
    if not raw.isascii() or not raw.isdecimal():
        raise ValueError("after_cursor must be a non-negative decimal")
    return int(raw)


def _header(request: SessionRequest, name: str) -> str | None:
    lowered = name.lower()
    return next(
        (value for key, value in request.request.headers.items() if key.lower() == lowered), None
    )


def _problem(code: str, error_class: HttpErrorClass, path: str) -> HttpResponse:
    return error_response(ProductHttpError(code=code, error_class=error_class, path=path))


def _json_tree(value: Any) -> Any:
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError("SSE JSON object keys must be strings")
            result[key] = _json_tree(item)
        return result
    if isinstance(value, (list, tuple)):
        return [_json_tree(item) for item in value]
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    raise TypeError(f"SSE activity contains unsupported JSON value {type(value).__name__}")


__all__ = [
    "ActivityAccessAuthorizer",
    "ActivityRouter",
    "SseActivityBody",
    "activity_router_factory",
    "encode_sse_frame",
]
