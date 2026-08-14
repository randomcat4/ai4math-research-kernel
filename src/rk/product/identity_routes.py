"""HTTP routes for login, second-identity authentication, switching, and logout."""

from __future__ import annotations

import asyncio
import json
from collections.abc import Callable, Mapping, Sequence
from typing import Any

from rk.http_shell import (
    HeaderMap,
    HttpErrorClass,
    HttpResponse,
    JsonValue,
    ProductHttpError,
    RouteSpec,
    SessionPrincipal,
    SessionRequest,
)
from rk.product.identity import IdentityAuthenticationError, IdentityConflict
from rk.product.sessions import SessionAuthenticationError, SessionStore, SessionView


class IdentityRouter:
    """Session lifecycle routes; roles and capabilities are never accepted from JSON."""

    def __init__(
        self,
        *,
        sessions: SessionStore,
        clock: Callable[[], str],
        expires_at: Callable[[str], str],
        cookie_name: str = "rk_session",
        secure_cookie: bool = True,
    ) -> None:
        if not cookie_name or any(character in cookie_name for character in "=; \t\r\n"):
            raise ValueError("cookie_name is not a valid cookie token")
        self._sessions = sessions
        self._clock = clock
        self._expires_at = expires_at
        self._cookie_name = cookie_name
        self._secure_cookie = secure_cookie
        self._routes = (
            RouteSpec("POST", "/v1/session/login", self.login, "session-login"),
            RouteSpec("POST", "/v1/session/switch", self.switch, "session-switch"),
            RouteSpec("POST", "/v1/session/logout", self.logout, "session-logout"),
            RouteSpec("GET", "/v1/session/me", self.me, "session-me"),
        )

    def routes(self) -> Sequence[RouteSpec]:
        return self._routes

    async def login(self, request: SessionRequest) -> HttpResponse:
        body = _json_body(request)
        _exact_keys(body, {"identity_id", "login_secret"})
        now = self._clock()
        session_id = await self._existing_session_id(request.principal, now)
        try:
            view = await asyncio.to_thread(
                self._sessions.login,
                identity_id=_string(body, "identity_id"),
                login_secret=_string(body, "login_secret"),
                now=now,
                expires_at=self._expires_at(now),
                session_id=session_id,
            )
        except IdentityAuthenticationError as error:
            raise _http_error(
                "IDENTITY_AUTHENTICATION_FAILED",
                HttpErrorClass.AUTHENTICATION,
                "$.payload.login_secret",
            ) from error
        except SessionAuthenticationError as error:
            raise _http_error(
                "SESSION_NOT_ACTIVE", HttpErrorClass.AUTHENTICATION, "$.session"
            ) from error
        except IdentityConflict as error:
            raise _http_error(
                "IDENTITY_CONFLICT", HttpErrorClass.CONFLICT, "$.payload.identity_id"
            ) from error
        return HttpResponse(
            200,
            _session_body(view),
            self._cookie_headers(view.session_id),
        )

    async def switch(self, request: SessionRequest) -> HttpResponse:
        body = _json_body(request)
        _exact_keys(body, {"identity_id"})
        now = self._clock()
        session_id = await self._require_current(request.principal, now)
        try:
            view = await asyncio.to_thread(
                self._sessions.switch,
                session_id,
                _string(body, "identity_id"),
                now=now,
            )
        except (SessionAuthenticationError, KeyError) as error:
            raise _http_error(
                "SESSION_IDENTITY_NOT_AUTHENTICATED",
                HttpErrorClass.AUTHENTICATION,
                "$.payload.identity_id",
            ) from error
        return HttpResponse(200, _session_body(view), self._cookie_headers(session_id))

    async def logout(self, request: SessionRequest) -> HttpResponse:
        body = _json_body(request)
        _exact_keys(body, set())
        now = self._clock()
        session_id = await self._require_current(request.principal, now)
        try:
            await asyncio.to_thread(self._sessions.logout, session_id, now=now)
        except SessionAuthenticationError as error:
            raise _http_error(
                "SESSION_NOT_ACTIVE", HttpErrorClass.AUTHENTICATION, "$.session"
            ) from error
        return HttpResponse(
            200,
            {
                "schema_version": "rk.product.session_logout.v1",
                "logged_out": True,
            },
            self._clear_cookie_headers(),
        )

    async def me(self, request: SessionRequest) -> HttpResponse:
        if request.request.body:
            raise _http_error("REQUEST_BODY_NOT_ALLOWED", HttpErrorClass.SCHEMA, "$")
        now = self._clock()
        session_id = await self._require_current(request.principal, now)
        try:
            view = await asyncio.to_thread(self._sessions.view, session_id, now=now)
        except SessionAuthenticationError as error:
            raise _http_error(
                "SESSION_NOT_ACTIVE", HttpErrorClass.AUTHENTICATION, "$.session"
            ) from error
        return HttpResponse(200, _session_body(view))

    async def _existing_session_id(self, principal: SessionPrincipal, now: str) -> str | None:
        if not principal.session_id:
            if principal.subject_id or principal.capability_ids:
                raise _http_error(
                    "SESSION_PRINCIPAL_INVALID",
                    HttpErrorClass.AUTHENTICATION,
                    "$.session",
                )
            return None
        return await self._require_current(principal, now)

    async def _require_current(self, principal: SessionPrincipal, now: str) -> str:
        if not principal.session_id or not principal.subject_id or not principal.capability_ids:
            raise _http_error(
                "SESSION_PRINCIPAL_REQUIRED",
                HttpErrorClass.AUTHENTICATION,
                "$.session",
            )
        try:
            derived = await asyncio.to_thread(self._sessions.derive, principal.session_id, now=now)
        except SessionAuthenticationError as error:
            raise _http_error(
                "SESSION_NOT_ACTIVE", HttpErrorClass.AUTHENTICATION, "$.session"
            ) from error
        if (
            derived.principal_subject_id != principal.subject_id
            or derived.capability_ids != principal.capability_ids
        ):
            raise _http_error(
                "SESSION_PRINCIPAL_STALE",
                HttpErrorClass.AUTHENTICATION,
                "$.session",
            )
        return principal.session_id

    def _cookie_headers(self, session_id: str) -> HeaderMap:
        secure = "; Secure" if self._secure_cookie else ""
        return {
            "content-type": "application/json",
            "set-cookie": (
                f"{self._cookie_name}={session_id}; Path=/; HttpOnly; SameSite=Strict{secure}"
            ),
        }

    def _clear_cookie_headers(self) -> HeaderMap:
        secure = "; Secure" if self._secure_cookie else ""
        return {
            "content-type": "application/json",
            "set-cookie": (
                f"{self._cookie_name}=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0{secure}"
            ),
        }


def identity_router(
    *,
    sessions: SessionStore,
    clock: Callable[[], str],
    expires_at: Callable[[str], str],
    cookie_name: str = "rk_session",
    secure_cookie: bool = True,
) -> IdentityRouter:
    return IdentityRouter(
        sessions=sessions,
        clock=clock,
        expires_at=expires_at,
        cookie_name=cookie_name,
        secure_cookie=secure_cookie,
    )


class _DuplicateJsonKey(ValueError):
    pass


def _json_body(request: SessionRequest) -> dict[str, Any]:
    content_type = _header(request.request.headers, "content-type")
    if (
        content_type is None
        or content_type.partition(";")[0].strip().casefold() != "application/json"
    ):
        raise _http_error(
            "JSON_CONTENT_TYPE_REQUIRED", HttpErrorClass.SCHEMA, "$.headers.content-type"
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
            request.request.body.decode("utf-8"), object_pairs_hook=reject_duplicates
        )
    except (UnicodeDecodeError, json.JSONDecodeError, _DuplicateJsonKey) as error:
        raise _http_error("SESSION_REQUEST_JSON_INVALID", HttpErrorClass.SCHEMA, "$") from error
    if not isinstance(value, dict):
        raise _http_error("SESSION_REQUEST_OBJECT_REQUIRED", HttpErrorClass.SCHEMA, "$")
    return value


def _session_body(view: SessionView) -> Mapping[str, JsonValue]:
    return {
        "schema_version": "rk.product.session.v1",
        "session_id": view.session_id,
        "principal_subject_id": view.principal_subject_id,
        "identity_id": view.identity_id,
        "display_name": view.display_name,
        "role": view.role.value,
        "linked_identity_ids": list(view.linked_identity_ids),
        "session_version": view.session_version,
        "issued_at": view.issued_at,
        "expires_at": view.expires_at,
    }


def _exact_keys(value: Mapping[str, Any], expected: set[str]) -> None:
    if set(value) != expected:
        raise _http_error("SESSION_REQUEST_FIELDS_INVALID", HttpErrorClass.SCHEMA, "$")


def _string(value: Mapping[str, Any], name: str) -> str:
    item = value.get(name)
    if not isinstance(item, str) or not item:
        raise _http_error(
            "SESSION_REQUEST_STRING_REQUIRED",
            HttpErrorClass.SCHEMA,
            f"$.{name}",
        )
    return item


def _header(headers: Mapping[str, str], name: str) -> str | None:
    values = [value for key, value in headers.items() if key.casefold() == name]
    if len(values) > 1 and len(set(values)) != 1:
        raise _http_error(
            "CONFLICTING_HEADER_VALUES",
            HttpErrorClass.SCHEMA,
            f"$.headers.{name}",
        )
    return values[0] if values else None


def _http_error(code: str, error_class: HttpErrorClass, path: str) -> ProductHttpError:
    return ProductHttpError(code=code, error_class=error_class, path=path)


__all__ = ["IdentityRouter", "identity_router"]
