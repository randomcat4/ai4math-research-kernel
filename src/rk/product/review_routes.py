"""Authenticated review inbox, claim, and signed ArtifactRef submission routes."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping, Sequence
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
from rk.product.attestation_import import AttestationImportError, ReviewAttestationImporter
from rk.product.identity import ProductRole
from rk.product.reviews import (
    ReviewArtifactRef,
    ReviewIndependenceError,
    ReviewTask,
    ReviewTaskConflict,
    ReviewTaskError,
    ReviewTaskStateError,
    ReviewTaskStore,
)
from rk.product.sessions import SessionAuthenticationError, SessionStore, SessionView


class ReviewInboxIndex(Protocol):
    """Return stable task IDs from a rebuildable inbox index, never review verdict data."""

    def task_ids_for_assignee(self, assignee_identity_id: str) -> Sequence[str]: ...


class ReviewRouter:
    """Expose review routing while B05b remains the only signed artifact gate."""

    def __init__(
        self,
        *,
        sessions: SessionStore,
        tasks: ReviewTaskStore,
        importer: ReviewAttestationImporter,
        inbox: ReviewInboxIndex,
        clock: Callable[[], str],
    ) -> None:
        self._sessions = sessions
        self._tasks = tasks
        self._importer = importer
        self._inbox = inbox
        self._clock = clock
        self._routes = (
            RouteSpec("GET", "/v1/reviews/inbox", self.inbox, "review-inbox"),
            RouteSpec("POST", "/v1/reviews/claim", self.claim, "review-claim"),
            RouteSpec("POST", "/v1/reviews/submit", self.submit, "review-submit"),
        )

    def routes(self) -> Sequence[RouteSpec]:
        return self._routes

    async def inbox(self, request: SessionRequest) -> HttpResponse:
        if request.request.body:
            raise _error("REQUEST_BODY_NOT_ALLOWED", HttpErrorClass.SCHEMA, "$")
        reviewer = await self._reviewer(request.principal)
        try:
            task_ids = await asyncio.to_thread(
                self._inbox.task_ids_for_assignee, reviewer.identity_id
            )
            tasks = await asyncio.to_thread(
                self._load_tasks, task_ids, reviewer.identity_id
            )
        except (KeyError, ReviewTaskError) as error:
            raise _error(
                "REVIEW_INBOX_PROJECTION_INVALID",
                HttpErrorClass.UNAVAILABLE,
                "$.inbox",
            ) from error
        return HttpResponse(
            200,
            {
                "schema_version": "rk.product.review_inbox.v1",
                "tasks": [_task_body(task) for task in tasks],
            },
        )

    async def claim(self, request: SessionRequest) -> HttpResponse:
        body = _json_body(request)
        _exact_keys(body, {"review_task_id"})
        reviewer = await self._reviewer(request.principal)
        try:
            task = await asyncio.to_thread(
                self._tasks.claim,
                _string(body, "review_task_id"),
                identity_id=reviewer.identity_id,
                now=self._clock(),
            )
        except KeyError as error:
            raise _error(
                "REVIEW_TASK_NOT_FOUND",
                HttpErrorClass.NOT_FOUND,
                "$.review_task_id",
            ) from error
        except ReviewTaskConflict as error:
            raise _error(
                "REVIEW_TASK_CONFLICT",
                HttpErrorClass.CONFLICT,
                "$.review_task_id",
            ) from error
        except (ReviewTaskStateError, ReviewIndependenceError) as error:
            raise _error(
                "REVIEW_TASK_NOT_CLAIMABLE",
                HttpErrorClass.BUSINESS_GATE,
                "$.review_task_id",
            ) from error
        return HttpResponse(200, _task_body(task))

    async def submit(self, request: SessionRequest) -> HttpResponse:
        body = _json_body(request)
        _exact_keys(body, {"review_task_id", "signed_artifact_ref"})
        reviewer = await self._reviewer(request.principal)
        review_task_id = _string(body, "review_task_id")
        ref_body = _mapping(body["signed_artifact_ref"], "$.signed_artifact_ref")
        _exact_keys(
            ref_body,
            {"artifact_id", "sha256", "byte_count", "media_type"},
            path="$.signed_artifact_ref",
        )
        try:
            task = await asyncio.to_thread(self._tasks.get, review_task_id)
            if task.assignee_identity_id != reviewer.identity_id:
                raise ReviewTaskStateError("TASK_ASSIGNED_TO_ANOTHER_IDENTITY")
            artifact_ref = ReviewArtifactRef(
                artifact_id=_string(ref_body, "artifact_id", "$.signed_artifact_ref"),
                sha256=_string(ref_body, "sha256", "$.signed_artifact_ref"),
                byte_count=_integer(ref_body, "byte_count", "$.signed_artifact_ref"),
                media_type=_string(ref_body, "media_type", "$.signed_artifact_ref"),
            )
            imported = await asyncio.to_thread(
                self._importer.import_artifact,
                review_task_id=review_task_id,
                artifact_ref=artifact_ref,
                submitted_at=self._clock(),
            )
        except KeyError as error:
            raise _error(
                "REVIEW_TASK_NOT_FOUND",
                HttpErrorClass.NOT_FOUND,
                "$.review_task_id",
            ) from error
        except ReviewTaskConflict as error:
            raise _error(
                "REVIEW_TASK_CONFLICT",
                HttpErrorClass.CONFLICT,
                "$.review_task_id",
            ) from error
        except (ReviewTaskStateError, ReviewIndependenceError) as error:
            raise _error(
                "REVIEW_TASK_NOT_SUBMITTABLE",
                HttpErrorClass.BUSINESS_GATE,
                "$.review_task_id",
            ) from error
        except AttestationImportError as error:
            raise _error(
                error.code,
                HttpErrorClass.BUSINESS_GATE,
                "$.signed_artifact_ref",
            ) from error
        except ValueError as error:
            raise _error(
                "SIGNED_ARTIFACT_REF_INVALID",
                HttpErrorClass.SCHEMA,
                "$.signed_artifact_ref",
            ) from error
        return HttpResponse(200, _task_body(imported.task))

    async def _reviewer(self, principal: SessionPrincipal) -> SessionView:
        if (
            not principal.session_id
            or not principal.subject_id
            or not principal.capability_ids
        ):
            raise _error(
                "SESSION_PRINCIPAL_REQUIRED",
                HttpErrorClass.AUTHENTICATION,
                "$.session",
            )
        now = self._clock()
        try:
            derived, view = await asyncio.gather(
                asyncio.to_thread(
                    self._sessions.derive, principal.session_id, now=now
                ),
                asyncio.to_thread(
                    self._sessions.view, principal.session_id, now=now
                ),
            )
        except SessionAuthenticationError as error:
            raise _error(
                "SESSION_NOT_ACTIVE",
                HttpErrorClass.AUTHENTICATION,
                "$.session",
            ) from error
        if (
            derived.principal_subject_id != principal.subject_id
            or derived.capability_ids != principal.capability_ids
        ):
            raise _error(
                "SESSION_PRINCIPAL_STALE",
                HttpErrorClass.AUTHENTICATION,
                "$.session",
            )
        if view.role not in {
            ProductRole.PEER_REVIEWER,
            ProductRole.PAPER_REVIEWER,
        }:
            raise _error(
                "SIGNED_REVIEWER_ROLE_REQUIRED",
                HttpErrorClass.AUTHORIZATION,
                "$.session.principal",
            )
        return view

    def _load_tasks(
        self, task_ids: Sequence[str], assignee_identity_id: str
    ) -> tuple[ReviewTask, ...]:
        if len(set(task_ids)) != len(task_ids):
            raise ReviewTaskError("review inbox index returned duplicate task IDs")
        tasks = tuple(self._tasks.get(task_id) for task_id in task_ids)
        if any(task.assignee_identity_id != assignee_identity_id for task in tasks):
            raise ReviewTaskError("review inbox index crossed assignee boundary")
        return tuple(
            sorted(tasks, key=lambda task: (task.expires_at, task.review_task_id))
        )


def review_router(
    *,
    sessions: SessionStore,
    tasks: ReviewTaskStore,
    importer: ReviewAttestationImporter,
    inbox: ReviewInboxIndex,
    clock: Callable[[], str],
) -> ReviewRouter:
    return ReviewRouter(
        sessions=sessions,
        tasks=tasks,
        importer=importer,
        inbox=inbox,
        clock=clock,
    )


class _DuplicateJsonKey(ValueError):
    pass


def _json_body(request: SessionRequest) -> dict[str, Any]:
    content_type = _header(request.request.headers, "content-type")
    if (
        content_type is None
        or content_type.partition(";")[0].strip().casefold() != "application/json"
    ):
        raise _error(
            "JSON_CONTENT_TYPE_REQUIRED",
            HttpErrorClass.SCHEMA,
            "$.headers.content-type",
        )

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise _DuplicateJsonKey(key)
            result[key] = value
        return result

    try:
        value = json.loads(
            request.request.body.decode("utf-8"),
            object_pairs_hook=reject_duplicates,
        )
    except (UnicodeDecodeError, json.JSONDecodeError, _DuplicateJsonKey) as error:
        raise _error("REVIEW_REQUEST_JSON_INVALID", HttpErrorClass.SCHEMA, "$") from error
    if not isinstance(value, dict):
        raise _error("REVIEW_REQUEST_OBJECT_REQUIRED", HttpErrorClass.SCHEMA, "$")
    return value


def _task_body(task: ReviewTask) -> dict[str, JsonValue]:
    body: dict[str, JsonValue] = {
        "schema_version": "rk.product.review_task.v1",
        "review_task_id": task.review_task_id,
        "review_type": task.review_type.value,
        "run_id": task.binding.run_id,
        "assignee_subject_id": task.assignee_subject_id,
        "author_subject_ids": list(task.author_subject_ids),
        "target_digest": task.binding.target_digest,
        "contract_version": task.binding.contract_version,
        "research_revision": task.binding.kernel_revision,
        "independence_required": task.independence_required,
        "state": task.status.value,
        "created_at": task.created_at,
        "expires_at": task.expires_at,
        "independence_status": task.independence_status.value,
    }
    if task.signed_artifact_ref is not None:
        body["signed_artifact_ref"] = {
            "artifact_id": task.signed_artifact_ref.artifact_id,
            "sha256": task.signed_artifact_ref.sha256,
            "byte_count": task.signed_artifact_ref.byte_count,
            "media_type": task.signed_artifact_ref.media_type,
        }
    return body


def _mapping(value: Any, path: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise _error("OBJECT_REQUIRED", HttpErrorClass.SCHEMA, path)
    return value


def _exact_keys(
    value: Mapping[str, Any], expected: set[str], path: str = "$"
) -> None:
    if set(value) != expected:
        raise _error("REVIEW_REQUEST_FIELDS_INVALID", HttpErrorClass.SCHEMA, path)


def _string(value: Mapping[str, Any], name: str, path: str = "$") -> str:
    item = value.get(name)
    if not isinstance(item, str) or not item:
        raise _error(
            "REVIEW_REQUEST_STRING_REQUIRED",
            HttpErrorClass.SCHEMA,
            f"{path}.{name}",
        )
    return item


def _integer(value: Mapping[str, Any], name: str, path: str) -> int:
    item = value.get(name)
    if not isinstance(item, int) or isinstance(item, bool):
        raise _error(
            "REVIEW_REQUEST_INTEGER_REQUIRED",
            HttpErrorClass.SCHEMA,
            f"{path}.{name}",
        )
    return item


def _header(headers: Mapping[str, str], name: str) -> str | None:
    values = [value for key, value in headers.items() if key.casefold() == name]
    if len(values) > 1 and len(set(values)) != 1:
        raise _error(
            "CONFLICTING_HEADER_VALUES",
            HttpErrorClass.SCHEMA,
            f"$.headers.{name}",
        )
    return values[0] if values else None


def _error(code: str, error_class: HttpErrorClass, path: str) -> ProductHttpError:
    return ProductHttpError(code=code, error_class=error_class, path=path)


__all__ = ["ReviewInboxIndex", "ReviewRouter", "review_router"]
