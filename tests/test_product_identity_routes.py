from __future__ import annotations

import asyncio
import json
import sqlite3
from pathlib import Path
from typing import Any

import pytest

from rk.domain import CapabilityError
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
from rk.product.identity_routes import IdentityRouter, identity_router
from rk.product.sessions import SessionAuthenticationError, SessionStore

ROOT = Path(__file__).parents[1]
B05A = ROOT / "schema_fragments/B05a/identity.sql"
B05B = ROOT / "schema_fragments/B05b/reviews.sql"
B05C = ROOT / "schema_fragments/B05c/identity_roles.sql"
NOW = "2026-08-13T18:00:00Z"
EXPIRES = "2026-08-14T18:00:00Z"
EMPTY = SessionPrincipal(session_id="", subject_id="", capability_ids=())


class SessionIds:
    def __init__(self) -> None:
        self.value = 0

    def __call__(self) -> str:
        self.value += 1
        return f"session-{self.value}"


def _setup(tmp_path: Path) -> tuple[IdentityRouter, SessionStore]:
    db_path = tmp_path / "identity.sqlite"
    with sqlite3.connect(db_path) as connection:
        connection.executescript(B05A.read_text(encoding="utf-8"))
        connection.executescript(B05B.read_text(encoding="utf-8"))
        connection.executescript(B05C.read_text(encoding="utf-8"))
    identities = IdentityStore(db_path, lambda: b"0" * 16)
    identities.register(
        identity_id="identity-main",
        subject_id="main:one",
        display_name="Main",
        role=ProductRole.MAIN,
        capability_id="cap:main",
        login_secret="main-login-secret",
        now=NOW,
    )
    identities.register(
        identity_id="identity-reviewer",
        subject_id="reviewer:one",
        display_name="Reviewer",
        role=ProductRole.PEER_REVIEWER,
        capability_id="cap:reviewer",
        login_secret="reviewer-login-secret",
        now=NOW,
    )
    sessions = SessionStore(db_path, identities, SessionIds(), "organization-one")
    return (
        identity_router(
            sessions=sessions,
            clock=lambda: NOW,
            expires_at=lambda _now: EXPIRES,
            secure_cookie=True,
        ),
        sessions,
    )


def _request(path: str, body: dict[str, Any]) -> HttpRequest:
    return HttpRequest(
        method="POST",
        path=path,
        headers={"content-type": "application/json"},
        body=json.dumps(body, sort_keys=True, separators=(",", ":")).encode(),
    )


def _invoke(
    handler: Any,
    request: HttpRequest,
    principal: SessionPrincipal = EMPTY,
) -> HttpResponse:
    result = asyncio.run(handler(SessionRequest(request, principal)))
    assert isinstance(result, HttpResponse)
    return result


def _principal(sessions: SessionStore, session_id: str) -> SessionPrincipal:
    derived = sessions.derive(session_id, now=NOW)
    return SessionPrincipal(
        session_id=derived.session_id,
        subject_id=derived.principal_subject_id,
        capability_ids=derived.capability_ids,
    )


def _login(
    router: IdentityRouter,
    identity_id: str,
    secret: str,
    principal: SessionPrincipal = EMPTY,
) -> HttpResponse:
    return _invoke(
        router.login,
        _request(
            "/v1/session/login",
            {"identity_id": identity_id, "login_secret": secret},
        ),
        principal,
    )


def test_identity_router_registers_fixed_session_lifecycle_routes(tmp_path: Path) -> None:
    router, _sessions = _setup(tmp_path)
    registry = RouteRegistry()
    registry.register(router)
    assert [(route.method, route.path) for route in registry.routes] == [
        ("GET", "/v1/session/options"),
        ("POST", "/v1/session/enter"),
        ("POST", "/v1/session/login"),
        ("POST", "/v1/session/switch"),
        ("POST", "/v1/session/logout"),
        ("GET", "/v1/session/me"),
    ]


def test_shared_options_enter_without_exposing_identity_or_secret(tmp_path: Path) -> None:
    router, sessions = _setup(tmp_path)

    options = _invoke(
        router.options,
        HttpRequest(method="GET", path="/v1/session/options"),
    )
    assert options.status == 200
    assert options.body["default"] == "SHARED"
    rendered_options = json.dumps(dict(options.body))
    assert "identity-main" not in rendered_options
    assert "main-login-secret" not in rendered_options

    entered = _invoke(
        router.enter,
        _request("/v1/session/enter", {"option": "SHARED"}),
    )
    assert entered.body["role"] == "VIEWER"
    session_id = str(entered.body["session_id"])
    assert session_id.startswith("shared.")
    assert sessions.derive(session_id, now=NOW).principal_subject_id == "main:one"
    assert "HttpOnly" in entered.headers["set-cookie"]
    assert entered.body["access_mode"] == "SHARED_READ_ONLY"
    with pytest.raises(CapabilityError):
        sessions.verified_capability(
            sessions.derive(session_id, now=NOW),
            action="CREATE_RESEARCH",
            run_id=None,
            now=NOW,
        )


def test_login_sets_httponly_cookie_without_capability_secret(tmp_path: Path) -> None:
    router, sessions = _setup(tmp_path)

    response = _login(router, "identity-main", "main-login-secret")

    assert response.status == 200
    assert response.body["role"] == "MAIN"
    session_id = str(response.body["session_id"])
    assert sessions.derive(session_id, now=NOW).principal_subject_id == "main:one"
    cookie = response.headers["set-cookie"]
    assert cookie.startswith(f"rk_session={session_id};")
    assert "HttpOnly" in cookie
    assert "SameSite=Strict" in cookie
    assert "Secure" in cookie
    rendered = json.dumps(dict(response.body))
    assert "main-login-secret" not in rendered
    assert "cap:main" not in rendered


def test_second_identity_login_switch_me_and_logout_use_same_session(
    tmp_path: Path,
) -> None:
    router, sessions = _setup(tmp_path)
    first = _login(router, "identity-main", "main-login-secret")
    session_id = str(first.body["session_id"])
    main = _principal(sessions, session_id)

    second = _login(
        router,
        "identity-reviewer",
        "reviewer-login-secret",
        main,
    )
    assert second.body["session_id"] == session_id
    assert second.body["role"] == "PEER_REVIEWER"
    assert second.body["linked_identity_ids"] == [
        "identity-main",
        "identity-reviewer",
    ]

    reviewer = _principal(sessions, session_id)
    switched = _invoke(
        router.switch,
        _request("/v1/session/switch", {"identity_id": "identity-main"}),
        reviewer,
    )
    assert switched.body["role"] == "MAIN"

    current = _principal(sessions, session_id)
    me = _invoke(
        router.me,
        HttpRequest(method="GET", path="/v1/session/me"),
        current,
    )
    assert me.body["principal_subject_id"] == "main:one"

    logged_out = _invoke(
        router.logout,
        _request("/v1/session/logout", {}),
        current,
    )
    assert logged_out.body["logged_out"] is True
    assert "Max-Age=0" in logged_out.headers["set-cookie"]
    with pytest.raises(SessionAuthenticationError):
        sessions.derive(session_id, now=NOW)


@pytest.mark.parametrize("field", ["role", "capability_id", "session_id", "principal_subject_id"])
def test_login_rejects_role_capability_and_session_in_body(tmp_path: Path, field: str) -> None:
    router, _sessions = _setup(tmp_path)
    body = {
        "identity_id": "identity-main",
        "login_secret": "main-login-secret",
        field: "ADMIN",
    }

    with pytest.raises(ProductHttpError) as caught:
        _invoke(router.login, _request("/v1/session/login", body))

    assert caught.value.error_class is HttpErrorClass.SCHEMA


def test_bad_credentials_unlinked_switch_and_stale_principal_are_rejected(
    tmp_path: Path,
) -> None:
    router, sessions = _setup(tmp_path)
    with pytest.raises(ProductHttpError) as bad:
        _login(router, "identity-main", "wrong-login-secret")
    assert bad.value.error_class is HttpErrorClass.AUTHENTICATION

    first = _login(router, "identity-main", "main-login-secret")
    session_id = str(first.body["session_id"])
    main = _principal(sessions, session_id)
    with pytest.raises(ProductHttpError) as unlinked:
        _invoke(
            router.switch,
            _request("/v1/session/switch", {"identity_id": "identity-reviewer"}),
            main,
        )
    assert unlinked.value.error_class is HttpErrorClass.AUTHENTICATION

    forged = SessionPrincipal(
        session_id=session_id,
        subject_id="reviewer:forged",
        capability_ids=("cap:admin",),
    )
    with pytest.raises(ProductHttpError) as stale:
        _invoke(
            router.switch,
            _request("/v1/session/switch", {"identity_id": "identity-main"}),
            forged,
        )
    assert stale.value.code == "SESSION_PRINCIPAL_STALE"
