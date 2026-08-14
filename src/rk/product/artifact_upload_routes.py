"""HTTP assembly for authenticated browser uploads using raw chunk bytes."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Mapping, Sequence
from enum import StrEnum
from typing import Any, Protocol

from rk.http_shell import (
    HttpErrorClass,
    HttpResponse,
    JsonValue,
    ProductHttpError,
    RouteSpec,
    SessionPrincipal,
    SessionRequest,
)
from rk.product.artifact_upload import ArtifactUploadError, ArtifactUploadStore, UploadSession


class ArtifactUploadOperation(StrEnum):
    BEGIN_UPLOAD = "BEGIN_UPLOAD"
    APPEND_CHUNK = "APPEND_CHUNK"
    COMMIT_UPLOAD = "COMMIT_UPLOAD"


class UploadAuthorizer(Protocol):
    """Authorize only the principal established by session middleware."""

    def __call__(
        self, principal: SessionPrincipal, operation: ArtifactUploadOperation
    ) -> None: ...


class ArtifactUploadRouter:
    """Expose the fixed artifact operation endpoint without a host-path transport."""

    def __init__(
        self,
        *,
        uploads: ArtifactUploadStore,
        authorize: UploadAuthorizer,
    ) -> None:
        self._uploads = uploads
        self._authorize = authorize
        self._routes = (
            RouteSpec(
                method="POST",
                path="/v1/artifacts/operations",
                handler=self.handle,
                name="artifact-upload-operations",
            ),
        )

    def routes(self) -> Sequence[RouteSpec]:
        return self._routes

    async def handle(self, session_request: SessionRequest) -> HttpResponse:
        principal = session_request.principal
        if (
            not principal.session_id
            or not principal.subject_id
            or not principal.capability_ids
        ):
            raise ProductHttpError(
                code="SESSION_PRINCIPAL_REQUIRED",
                error_class=HttpErrorClass.AUTHENTICATION,
                path="$.session",
            )
        request = session_request.request
        operation_header = _header(request.headers, "x-rk-artifact-operation")
        try:
            if operation_header is not None:
                if operation_header != ArtifactUploadOperation.APPEND_CHUNK.value:
                    raise _schema_error(
                        "UNKNOWN_ARTIFACT_OPERATION",
                        "$.headers.x-rk-artifact-operation",
                    )
                operation = ArtifactUploadOperation.APPEND_CHUNK
                self._authorize(principal, operation)
                return await self._append(session_request)

            if _media_type(request.headers) != "application/json":
                raise _schema_error(
                    "JSON_CONTENT_TYPE_REQUIRED", "$.headers.content-type"
                )
            envelope = _json_object(request.body)
            operation = _json_operation(envelope)
            self._authorize(principal, operation)
            if operation is ArtifactUploadOperation.BEGIN_UPLOAD:
                return await self._begin(envelope)
            if operation is ArtifactUploadOperation.COMMIT_UPLOAD:
                return await self._commit(envelope)
            raise _schema_error(
                "APPEND_CHUNK_REQUIRES_RAW_BODY",
                "$.headers.x-rk-artifact-operation",
            )
        except ProductHttpError:
            raise
        except PermissionError as error:
            raise ProductHttpError(
                code="ARTIFACT_UPLOAD_FORBIDDEN",
                error_class=HttpErrorClass.AUTHORIZATION,
                path="$.session.principal",
            ) from error
        except KeyError as error:
            raise ProductHttpError(
                code="UPLOAD_NOT_FOUND",
                error_class=HttpErrorClass.NOT_FOUND,
                path="$.payload.upload_id",
            ) from error
        except ArtifactUploadError as error:
            raise ProductHttpError(
                code="ARTIFACT_UPLOAD_CONFLICT",
                error_class=HttpErrorClass.CONFLICT,
                path="$.payload",
            ) from error
        except ValueError as error:
            raise ProductHttpError(
                code="ARTIFACT_UPLOAD_SCHEMA_INVALID",
                error_class=HttpErrorClass.SCHEMA,
                path="$.payload",
            ) from error

    async def _begin(self, envelope: dict[str, Any]) -> HttpResponse:
        _exact_keys(envelope, {"type", "payload"}, "$")
        payload = _mapping(envelope["payload"], "$.payload")
        _exact_keys(
            payload,
            {"request_id", "logical_name", "media_type", "byte_count", "sha256"},
            "$.payload",
        )
        session = await asyncio.to_thread(
            self._uploads.begin,
            request_id=_string(payload, "request_id"),
            logical_name=_string(payload, "logical_name"),
            media_type=_string(payload, "media_type"),
            byte_count=_integer(payload, "byte_count"),
            sha256=_string(payload, "sha256"),
        )
        return HttpResponse(
            status=200,
            body={
                "schema_version": "rk.product.artifact_operation_result.v1",
                "type": ArtifactUploadOperation.BEGIN_UPLOAD.value,
                "upload": _upload_body(session),
            },
        )

    async def _append(self, session_request: SessionRequest) -> HttpResponse:
        request = session_request.request
        if _media_type(request.headers) != "application/octet-stream":
            raise _schema_error(
                "RAW_CHUNK_CONTENT_TYPE_REQUIRED", "$.headers.content-type"
            )
        upload_id = _required_header(request.headers, "x-rk-upload-id")
        offset_text = _required_header(request.headers, "x-rk-upload-offset")
        transfer_sha256 = _required_header(
            request.headers, "x-rk-chunk-sha256"
        )
        try:
            offset = int(offset_text)
        except ValueError as error:
            raise _schema_error(
                "UPLOAD_OFFSET_INVALID", "$.headers.x-rk-upload-offset"
            ) from error
        session = await asyncio.to_thread(
            self._uploads.append,
            upload_id,
            offset=offset,
            data=request.body,
            transfer_sha256=transfer_sha256,
        )
        return HttpResponse(
            status=200,
            body={
                "schema_version": "rk.product.artifact_operation_result.v1",
                "type": ArtifactUploadOperation.APPEND_CHUNK.value,
                "upload": _upload_body(session),
            },
        )

    async def _commit(self, envelope: dict[str, Any]) -> HttpResponse:
        _exact_keys(envelope, {"type", "payload"}, "$")
        payload = _mapping(envelope["payload"], "$.payload")
        _exact_keys(payload, {"upload_id"}, "$.payload")
        artifact = await asyncio.to_thread(
            self._uploads.commit, _string(payload, "upload_id")
        )
        artifact_body: dict[str, JsonValue] = {
            "artifact_id": artifact.artifact_id,
            "sha256": artifact.sha256,
            "byte_count": artifact.byte_count,
            "media_type": artifact.media_type,
        }
        return HttpResponse(
            status=200,
            body={
                "schema_version": "rk.product.artifact_operation_result.v1",
                "type": ArtifactUploadOperation.COMMIT_UPLOAD.value,
                "artifact_ref": artifact_body,
            },
        )


def artifact_upload_router(
    *,
    uploads: ArtifactUploadStore,
    authorize: UploadAuthorizer,
) -> ArtifactUploadRouter:
    """Build the B04a router for registration in the shared RouteRegistry."""

    return ArtifactUploadRouter(uploads=uploads, authorize=authorize)


class _DuplicateJsonKey(ValueError):
    pass


def _json_object(raw: bytes) -> dict[str, Any]:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise _DuplicateJsonKey(key)
            value[key] = item
        return value

    try:
        value = json.loads(
            raw.decode("utf-8"), object_pairs_hook=reject_duplicates
        )
    except UnicodeDecodeError as error:
        raise _schema_error("ARTIFACT_OPERATION_NOT_UTF8", "$") from error
    except json.JSONDecodeError as error:
        raise _schema_error("ARTIFACT_OPERATION_JSON_INVALID", "$") from error
    except _DuplicateJsonKey as error:
        raise _schema_error("ARTIFACT_OPERATION_DUPLICATE_KEY", "$") from error
    if not isinstance(value, dict):
        raise _schema_error("ARTIFACT_OPERATION_OBJECT_REQUIRED", "$")
    return value


def _json_operation(envelope: dict[str, Any]) -> ArtifactUploadOperation:
    value = envelope.get("type")
    if not isinstance(value, str):
        raise _schema_error("ARTIFACT_OPERATION_TYPE_REQUIRED", "$.type")
    try:
        return ArtifactUploadOperation(value)
    except ValueError as error:
        raise _schema_error("UNKNOWN_ARTIFACT_OPERATION", "$.type") from error


def _upload_body(session: UploadSession) -> dict[str, JsonValue]:
    return {
        "upload_id": session.upload_id,
        "request_id": session.request_id,
        "state": session.state,
        "logical_name": session.logical_name,
        "media_type": session.media_type,
        "declared_byte_count": session.declared_byte_count,
        "declared_sha256": session.declared_sha256,
        "received_byte_count": session.received_byte_count,
        "artifact_id": session.artifact_id,
        "created_at": session.created_at,
        "updated_at": session.updated_at,
        "committed_at": session.committed_at,
    }


def _mapping(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _schema_error("OBJECT_REQUIRED", path)
    return value


def _exact_keys(value: Mapping[str, Any], expected: set[str], path: str) -> None:
    if set(value) != expected:
        raise _schema_error("OBJECT_FIELDS_INVALID", path)


def _string(value: Mapping[str, Any], name: str) -> str:
    item = value.get(name)
    if not isinstance(item, str) or not item:
        raise _schema_error("STRING_REQUIRED", f"$.payload.{name}")
    return item


def _integer(value: Mapping[str, Any], name: str) -> int:
    item = value.get(name)
    if not isinstance(item, int) or isinstance(item, bool):
        raise _schema_error("INTEGER_REQUIRED", f"$.payload.{name}")
    return item


def _header(headers: Mapping[str, str], name: str) -> str | None:
    values = [value for key, value in headers.items() if key.casefold() == name]
    if len(values) > 1 and len(set(values)) != 1:
        raise _schema_error("CONFLICTING_HEADER_VALUES", f"$.headers.{name}")
    return values[0] if values else None


def _media_type(headers: Mapping[str, str]) -> str | None:
    value = _header(headers, "content-type")
    return value.partition(";")[0].strip().casefold() if value is not None else None


def _required_header(headers: Mapping[str, str], name: str) -> str:
    value = _header(headers, name)
    if value is None or not value:
        raise _schema_error("HEADER_REQUIRED", f"$.headers.{name}")
    return value


def _schema_error(code: str, path: str) -> ProductHttpError:
    return ProductHttpError(code=code, error_class=HttpErrorClass.SCHEMA, path=path)


__all__ = [
    "ArtifactUploadOperation",
    "ArtifactUploadRouter",
    "UploadAuthorizer",
    "artifact_upload_router",
]
