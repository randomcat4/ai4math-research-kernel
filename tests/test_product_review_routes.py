from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from rk.http_shell import (
    HttpErrorClass,
    HttpRequest,
    HttpResponse,
    ProductHttpError,
    RouteRegistry,
    SessionPrincipal,
    SessionRequest,
)
from rk.product.identity import IdentityStore, ProductRole
from rk.product.review_routes import ReviewRouter, review_router
from rk.product.reviews import ReviewArtifactRef, ReviewTaskStatus, ReviewType
from rk.product.sessions import SessionStore
from tests.test_product_reviews import (
    ARTIFACT_ID,
    EXPIRES,
    REVIEWER_ID,
    REVIEWER_SUBJECT,
    TASK_ID,
    _harness,
    _put,
    _review,
)

NOW = "2026-08-13T18:10:00Z"


class SessionIds:
    def __init__(self) -> None:
        self.value = 0

    def __call__(self) -> str:
        self.value += 1
        return f"review-session-{self.value}"


class Inbox:
    def __init__(self, *task_ids: str) -> None:
        self.task_ids = task_ids
        self.calls: list[str] = []

    def task_ids_for_assignee(self, assignee_identity_id: str) -> tuple[str, ...]:
        self.calls.append(assignee_identity_id)
        return self.task_ids


def _setup(
    tmp_path: Path,
    *,
    inbox: Inbox | None = None,
) -> tuple[Any, ReviewRouter, SessionStore, SessionPrincipal, Inbox]:
    harness = _harness(tmp_path, ReviewType.ATOMIC)
    identities = IdentityStore(harness.db_path, lambda: b"3" * 16)
    sessions = SessionStore(
        harness.db_path,
        identities,
        SessionIds(),
        "organization-one",
    )
    session = sessions.login(
        identity_id=REVIEWER_ID,
        login_secret="reviewer-login-secret",
        now=NOW,
        expires_at=EXPIRES,
    )
    derived = sessions.derive(session.session_id, now=NOW)
    principal = SessionPrincipal(
        session_id=derived.session_id,
        subject_id=derived.principal_subject_id,
        capability_ids=derived.capability_ids,
    )
    index = inbox or Inbox(TASK_ID)
    router = review_router(
        sessions=sessions,
        tasks=harness.tasks,
        importer=harness.importer,
        inbox=index,
        clock=lambda: NOW,
    )
    return harness, router, sessions, principal, index


def _json_request(path: str, body: dict[str, Any]) -> HttpRequest:
    return HttpRequest(
        method="POST",
        path=path,
        headers={"content-type": "application/json"},
        body=json.dumps(body, sort_keys=True, separators=(",", ":")).encode(),
    )


def _invoke(
    handler: Any,
    request: HttpRequest,
    principal: SessionPrincipal,
) -> HttpResponse:
    result = asyncio.run(handler(SessionRequest(request, principal)))
    assert isinstance(result, HttpResponse)
    return result


def _ref_body(ref: ReviewArtifactRef) -> dict[str, Any]:
    return {
        "artifact_id": ref.artifact_id,
        "sha256": ref.sha256,
        "byte_count": ref.byte_count,
        "media_type": ref.media_type,
    }


def test_review_router_registers_inbox_claim_and_submit_routes(tmp_path: Path) -> None:
    _harness_value, router, _sessions, _principal_value, _index = _setup(tmp_path)
    registry = RouteRegistry()
    registry.register(router)
    assert [(route.method, route.path) for route in registry.routes] == [
        ("GET", "/v1/reviews/inbox"),
        ("POST", "/v1/reviews/claim"),
        ("POST", "/v1/reviews/submit"),
    ]


def test_reviewer_inbox_and_claim_use_current_session_identity(tmp_path: Path) -> None:
    harness, router, _sessions, principal, index = _setup(tmp_path)

    inbox = _invoke(
        router.inbox,
        HttpRequest(method="GET", path="/v1/reviews/inbox"),
        principal,
    )
    tasks = inbox.body["tasks"]
    assert isinstance(tasks, list)
    assert len(tasks) == 1
    assert tasks[0]["review_task_id"] == TASK_ID
    assert tasks[0]["assignee_subject_id"] == REVIEWER_SUBJECT
    assert "verdict" not in tasks[0]
    assert "checks" not in tasks[0]
    assert index.calls == [REVIEWER_ID]

    claimed = _invoke(
        router.claim,
        _json_request("/v1/reviews/claim", {"review_task_id": TASK_ID}),
        principal,
    )
    assert claimed.body["state"] == "CLAIMED"
    assert harness.tasks.get(TASK_ID).status is ReviewTaskStatus.CLAIMED


def test_submit_accepts_only_signed_artifact_ref_and_runs_b05b_gate(
    tmp_path: Path,
) -> None:
    harness, router, _sessions, principal, _index = _setup(tmp_path)
    ref = _put(harness, _review(harness))

    response = _invoke(
        router.submit,
        _json_request(
            "/v1/reviews/submit",
            {
                "review_task_id": TASK_ID,
                "signed_artifact_ref": _ref_body(ref),
            },
        ),
        principal,
    )

    assert response.body["state"] == "SUBMITTED"
    assert response.body["independence_status"] == "VERIFIED"
    assert response.body["signed_artifact_ref"] == _ref_body(ref)
    assert "verdict" not in response.body
    assert "checks" not in response.body
    assert harness.tasks.get(TASK_ID).signed_artifact_ref == ref


@pytest.mark.parametrize("extra", ["verdict", "checks", "review", "role", "capability_id"])
def test_submit_rejects_inline_review_truth_role_and_capability_fields(
    tmp_path: Path, extra: str
) -> None:
    harness, router, _sessions, principal, _index = _setup(tmp_path)
    ref = _put(harness, _review(harness))
    body: dict[str, Any] = {
        "review_task_id": TASK_ID,
        "signed_artifact_ref": _ref_body(ref),
        extra: True,
    }

    with pytest.raises(ProductHttpError) as caught:
        _invoke(
            router.submit,
            _json_request("/v1/reviews/submit", body),
            principal,
        )

    assert caught.value.error_class is HttpErrorClass.SCHEMA
    assert harness.tasks.get(TASK_ID).status is ReviewTaskStatus.CLAIMED


@pytest.mark.parametrize("extra", ["path", "host_path", "signed_review_json"])
def test_signed_artifact_ref_rejects_host_path_and_inline_artifact_content(
    tmp_path: Path, extra: str
) -> None:
    harness, router, _sessions, principal, _index = _setup(tmp_path)
    ref = _put(harness, _review(harness))
    ref_body = _ref_body(ref)
    ref_body[extra] = "C:/server/private/review.json"

    with pytest.raises(ProductHttpError) as caught:
        _invoke(
            router.submit,
            _json_request(
                "/v1/reviews/submit",
                {
                    "review_task_id": TASK_ID,
                    "signed_artifact_ref": ref_body,
                },
            ),
            principal,
        )

    assert caught.value.error_class is HttpErrorClass.SCHEMA
    assert harness.tasks.get(TASK_ID).signed_artifact_ref is None


def test_invalid_signed_artifact_digest_is_business_rejection(
    tmp_path: Path,
) -> None:
    harness, router, _sessions, principal, _index = _setup(tmp_path)
    ref = _put(harness, _review(harness))
    wrong = _ref_body(ref)
    wrong["sha256"] = "0" * 64

    with pytest.raises(ProductHttpError) as caught:
        _invoke(
            router.submit,
            _json_request(
                "/v1/reviews/submit",
                {"review_task_id": TASK_ID, "signed_artifact_ref": wrong},
            ),
            principal,
        )

    assert caught.value.error_class is HttpErrorClass.BUSINESS_GATE
    assert caught.value.code == "REVIEW_ARTIFACT_DIGEST_MISMATCH"
    assert harness.tasks.get(TASK_ID).status is ReviewTaskStatus.CLAIMED


def test_unsigned_all_true_artifact_is_rejected_by_b05b(
    tmp_path: Path,
) -> None:
    harness, router, _sessions, principal, _index = _setup(tmp_path)
    review = _review(harness)
    raw = json.dumps(
        review, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    harness.artifacts.values[ARTIFACT_ID] = raw
    ref = ReviewArtifactRef(
        ARTIFACT_ID,
        hashlib.sha256(raw).hexdigest(),
        len(raw),
        "application/json",
    )

    with pytest.raises(ProductHttpError) as caught:
        _invoke(
            router.submit,
            _json_request(
                "/v1/reviews/submit",
                {
                    "review_task_id": TASK_ID,
                    "signed_artifact_ref": _ref_body(ref),
                },
            ),
            principal,
        )

    assert caught.value.code == "REVIEW_SCHEMA_INVALID"
    assert harness.tasks.get(TASK_ID).status is ReviewTaskStatus.CLAIMED


def test_non_reviewer_forged_principal_and_other_assignee_are_rejected(
    tmp_path: Path,
) -> None:
    harness, router, sessions, reviewer, _index = _setup(tmp_path)
    identities = IdentityStore(harness.db_path, lambda: b"4" * 16)
    identities.register(
        identity_id="identity-main",
        subject_id="main:one",
        display_name="Main",
        role=ProductRole.MAIN,
        capability_id="cap:main",
        login_secret="main-login-secret",
        now=NOW,
    )
    main_view = sessions.login(
        identity_id="identity-main",
        login_secret="main-login-secret",
        now=NOW,
        expires_at=EXPIRES,
    )
    main_derived = sessions.derive(main_view.session_id, now=NOW)
    main = SessionPrincipal(
        main_derived.session_id,
        main_derived.principal_subject_id,
        main_derived.capability_ids,
    )
    with pytest.raises(ProductHttpError) as role_denied:
        _invoke(
            router.inbox,
            HttpRequest(method="GET", path="/v1/reviews/inbox"),
            main,
        )
    assert role_denied.value.error_class is HttpErrorClass.AUTHORIZATION

    forged = SessionPrincipal(
        reviewer.session_id,
        "reviewer:forged",
        reviewer.capability_ids,
    )
    with pytest.raises(ProductHttpError) as stale:
        _invoke(
            router.claim,
            _json_request("/v1/reviews/claim", {"review_task_id": TASK_ID}),
            forged,
        )
    assert stale.value.code == "SESSION_PRINCIPAL_STALE"


def test_inbox_projection_crossing_or_duplicate_ids_fails_without_partial_list(
    tmp_path: Path,
) -> None:
    duplicate = Inbox(TASK_ID, TASK_ID)
    _harness_value, router, _sessions, principal, _index = _setup(
        tmp_path, inbox=duplicate
    )

    with pytest.raises(ProductHttpError) as caught:
        _invoke(
            router.inbox,
            HttpRequest(method="GET", path="/v1/reviews/inbox"),
            principal,
        )

    assert caught.value.code == "REVIEW_INBOX_PROJECTION_INVALID"
    assert caught.value.error_class is HttpErrorClass.UNAVAILABLE
