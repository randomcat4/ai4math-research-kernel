"""HTTP routes for immutable artifact ranges and durable public log tails."""

from __future__ import annotations

import json
import re
from collections.abc import Mapping, Sequence
from typing import Protocol, cast

from rk.http_shell import (
    HttpErrorClass,
    HttpHandler,
    HttpResponse,
    HttpResult,
    HttpStreamResponse,
    ProductHttpError,
    RouteSpec,
    SessionPrincipal,
    SessionRequest,
    error_response,
)
from rk.product.artifact_read import (
    ArtifactDescriptor,
    ArtifactNotCommitted,
    ArtifactNotFound,
    ArtifactReadError,
    ArtifactReadService,
    InvalidByteRange,
)
from rk.product.log_tail import LogCursorAhead, PublicLog, PublicLogError, PublicLogStore

_ARTIFACT_PATH = re.compile(r"^/v1/artifacts/([A-Za-z0-9_.:-]+)$")


class ArtifactAccessAuthorizer(Protocol):
    """Enforce session-scoped visibility before any artifact bytes are opened."""

    def authorize_artifact(
        self, principal: SessionPrincipal, descriptor: ArtifactDescriptor
    ) -> None: ...

    def authorize_log(self, principal: SessionPrincipal, log: PublicLog) -> None: ...


class ArtifactRouter:
    """Mount B04b without buffering or encoding binary response bodies."""

    def __init__(
        self,
        *,
        artifacts: ArtifactReadService,
        logs: PublicLogStore,
        authorizer: ArtifactAccessAuthorizer,
        other_operations: HttpHandler | None = None,
    ) -> None:
        self._artifacts = artifacts
        self._logs = logs
        self._authorizer = authorizer
        self._other_operations = other_operations

    def routes(self) -> Sequence[RouteSpec]:
        return (
            RouteSpec(
                method="GET",
                path="/v1/artifacts/{artifact_id}",
                handler=self._download,
                name="artifact-range-read",
            ),
            RouteSpec(
                method="POST",
                path="/v1/artifacts/operations",
                handler=self._operation,
                name="artifact-operation",
            ),
        )

    async def _download(self, request: SessionRequest) -> HttpResult:
        match = _ARTIFACT_PATH.fullmatch(request.request.path)
        if match is None:
            return _problem("ARTIFACT_PATH_INVALID", HttpErrorClass.SCHEMA, "$.path")
        artifact_id = match.group(1)
        try:
            descriptor = self._artifacts.describe(artifact_id)
            self._authorizer.authorize_artifact(request.principal, descriptor)
            range_header = _header(request.request.headers, "range")
            result = self._artifacts.open_range(
                artifact_id,
                range_header=range_header,
                expected_ref=descriptor.ref,
            )
        except ArtifactNotFound:
            return _problem("ARTIFACT_NOT_FOUND", HttpErrorClass.NOT_FOUND, "$.artifact_id")
        except ArtifactNotCommitted:
            return _problem("ARTIFACT_NOT_COMMITTED", HttpErrorClass.CONFLICT, "$.artifact_id")
        except InvalidByteRange:
            return _range_problem(descriptor.ref.byte_count)
        except ProductHttpError as error:
            return error_response(error)
        except ArtifactReadError:
            return _problem("ARTIFACT_UNAVAILABLE", HttpErrorClass.UNAVAILABLE, "$.artifact_id")
        return HttpStreamResponse(
            status=206 if result.partial else 200,
            body=result.stream,
            headers=result.headers,
        )

    async def _operation(self, request: SessionRequest) -> HttpResult:
        try:
            value = _json_object(request.request.body)
            operation_type = _operation_type(value)
        except ValueError:
            return _problem("ARTIFACT_OPERATION_INVALID", HttpErrorClass.SCHEMA, "$.operation")
        if operation_type != "TAIL_LOG":
            if self._other_operations is None:
                return _problem(
                    "ARTIFACT_OPERATION_UNSUPPORTED", HttpErrorClass.SCHEMA, "$.operation.type"
                )
            return await self._other_operations(request)
        try:
            log_id, cursor, limit = _tail_payload(value)
            log = self._logs.get(log_id)
            self._authorizer.authorize_log(request.principal, log)
            tail = self._logs.tail(log_id, cursor=cursor, limit=limit)
        except ValueError:
            return _problem("LOG_TAIL_INVALID", HttpErrorClass.SCHEMA, "$.operation.payload")
        except KeyError:
            return _problem(
                "PUBLIC_LOG_NOT_FOUND", HttpErrorClass.NOT_FOUND, "$.operation.payload.log_id"
            )
        except LogCursorAhead:
            return _problem(
                "LOG_CURSOR_AHEAD", HttpErrorClass.CONFLICT, "$.operation.payload.cursor"
            )
        except ProductHttpError as error:
            return error_response(error)
        except PublicLogError:
            return _problem(
                "PUBLIC_LOG_UNAVAILABLE", HttpErrorClass.UNAVAILABLE, "$.operation.payload.log_id"
            )
        headers = {
            "content-type": "text/plain; charset=utf-8",
            "content-length": str(len(tail.data)),
            "x-rk-log-id": tail.log_id,
            "x-rk-log-stream": tail.stream,
            "x-rk-log-cursor": str(tail.cursor),
            "x-rk-log-next-cursor": str(tail.next_cursor),
            "x-rk-log-durable-byte-count": str(tail.durable_byte_count),
            "x-rk-log-caught-up": str(tail.caught_up).lower(),
            "x-rk-log-end-of-log": str(tail.end_of_log).lower(),
        }
        if tail.artifact_id is not None:
            headers["x-rk-artifact-id"] = tail.artifact_id
        body: tuple[bytes, ...] = (tail.data,) if tail.data else ()
        return HttpStreamResponse(status=200, body=iter(body), headers=headers)


def artifact_router_factory(
    *,
    artifacts: ArtifactReadService,
    logs: PublicLogStore,
    authorizer: ArtifactAccessAuthorizer,
    other_operations: HttpHandler | None = None,
) -> ArtifactRouter:
    return ArtifactRouter(
        artifacts=artifacts,
        logs=logs,
        authorizer=authorizer,
        other_operations=other_operations,
    )


def _header(headers: Mapping[str, str], name: str) -> str | None:
    lowered = name.lower()
    return next((value for key, value in headers.items() if key.lower() == lowered), None)


def _json_object(data: bytes) -> dict[str, object]:
    try:
        value = json.loads(data)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise ValueError("artifact operation body is not JSON") from error
    if not isinstance(value, dict):
        raise ValueError("artifact operation body is not an object")
    return cast(dict[str, object], value)


def _operation_type(value: Mapping[str, object]) -> str:
    if set(value) != {"schema_version", "request_id", "operation"}:
        raise ValueError("artifact operation envelope fields are invalid")
    if value["schema_version"] != "rk.product.artifact.v1" or not isinstance(
        value["request_id"], str
    ):
        raise ValueError("artifact operation envelope identity is invalid")
    operation = value["operation"]
    if not isinstance(operation, dict) or set(operation) != {"type", "payload"}:
        raise ValueError("artifact operation union is invalid")
    operation_type = operation["type"]
    if not isinstance(operation_type, str):
        raise ValueError("artifact operation type is invalid")
    return operation_type


def _tail_payload(value: Mapping[str, object]) -> tuple[str, int, int | None]:
    operation = value["operation"]
    assert isinstance(operation, dict)
    payload = operation["payload"]
    if not isinstance(payload, dict) or set(payload) not in (
        {"log_id", "cursor"},
        {"log_id", "cursor", "limit"},
    ):
        raise ValueError("log tail payload fields are invalid")
    log_id = payload["log_id"]
    cursor = payload["cursor"]
    limit = payload.get("limit")
    if (
        not isinstance(log_id, str)
        or not log_id
        or isinstance(cursor, bool)
        or not isinstance(cursor, int)
        or cursor < 0
        or (
            limit is not None
            and (isinstance(limit, bool) or not isinstance(limit, int) or limit <= 0)
        )
    ):
        raise ValueError("log tail payload values are invalid")
    return log_id, cursor, limit


def _problem(code: str, error_class: HttpErrorClass, path: str) -> HttpResponse:
    return error_response(ProductHttpError(code=code, error_class=error_class, path=path))


def _range_problem(total: int) -> HttpResponse:
    response = _problem("RANGE_NOT_SATISFIABLE", HttpErrorClass.RANGE, "$.headers.range")
    return HttpResponse(
        status=response.status,
        body=response.body,
        headers={
            **response.headers,
            "accept-ranges": "bytes",
            "content-range": f"bytes */{total}",
        },
    )


__all__ = ["ArtifactAccessAuthorizer", "ArtifactRouter", "artifact_router_factory"]
