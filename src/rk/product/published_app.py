"""Composition root for the one published RK product HTTP application."""

from __future__ import annotations

import re
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from urllib.parse import urlsplit

from rk.http_shell import (
    HttpErrorClass,
    HttpHandler,
    HttpRequest,
    HttpResponse,
    HttpResult,
    ProductHttpError,
    RouteRegistry,
    RouterProtocol,
    RouteSpec,
    SessionPrincipal,
    SessionRequest,
    error_response,
)
from rk.product.artifact_read import ArtifactReadService
from rk.product.artifact_routes import ArtifactAccessAuthorizer, artifact_router_factory
from rk.product.artifact_upload_routes import ArtifactUploadRouter
from rk.product.log_tail import PublicLogStore
from rk.product.sessions import SessionAuthenticationError, SessionStore

_PARAMETER = re.compile(r"\{[A-Za-z_][A-Za-z0-9_]*\}")
_COOKIE_VALUE = re.compile(r"^[A-Za-z0-9._~-]+$")


@dataclass(frozen=True, slots=True)
class PublishedAppConfig:
    """Deployment-owned values; no listener address or filesystem path is implicit."""

    db_path: Path
    cas_root: Path
    spool_root: Path
    deployment_id: str
    limits: Mapping[str, int]
    product_version: str = "RK-PRODUCT-1.1"
    schema_version: str = "rk.product.meta.v1"
    cookie_name: str = "rk_session"

    def __post_init__(self) -> None:
        if not self.deployment_id or not self.product_version or not self.schema_version:
            raise ValueError("deployment and product metadata must be non-empty")
        if not self.cookie_name or any(c in self.cookie_name for c in "=; \t\r\n"):
            raise ValueError("cookie_name is not a valid cookie token")
        if not self.limits or any(not key or value <= 0 for key, value in self.limits.items()):
            raise ValueError("published limits must be named positive integers")
        object.__setattr__(self, "db_path", Path(self.db_path))
        object.__setattr__(self, "cas_root", Path(self.cas_root))
        object.__setattr__(self, "spool_root", Path(self.spool_root))
        object.__setattr__(self, "limits", MappingProxyType(dict(self.limits)))


@dataclass(frozen=True, slots=True)
class PublishedAppRoutes:
    """All business routers mounted by the composition root."""

    command: RouterProtocol
    query: RouterProtocol
    upload: ArtifactUploadRouter
    activity: RouterProtocol
    identity: RouterProtocol
    review: RouterProtocol
    artifacts: ArtifactReadService
    logs: PublicLogStore
    artifact_authorizer: ArtifactAccessAuthorizer


class _MetaRouter:
    def __init__(self, config: PublishedAppConfig) -> None:
        self._config = config

    def routes(self) -> Sequence[RouteSpec]:
        return (RouteSpec("GET", "/v1/meta", self.meta, "product-meta"),)

    async def meta(self, request: SessionRequest) -> HttpResponse:
        if request.request.body:
            raise ProductHttpError(
                code="REQUEST_BODY_NOT_ALLOWED",
                error_class=HttpErrorClass.SCHEMA,
                path="$",
            )
        return HttpResponse(
            200,
            {
                "schema_version": self._config.schema_version,
                "product_version": self._config.product_version,
                "deployment_id": self._config.deployment_id,
                "limits": dict(self._config.limits),
            },
        )


class PublishedSessionMiddleware:
    """Derive every business principal from the opaque session cookie."""

    def __init__(
        self,
        sessions: SessionStore,
        *,
        clock: Callable[[], str],
        cookie_name: str,
        anonymous_routes: frozenset[tuple[str, str]],
    ) -> None:
        self._sessions = sessions
        self._clock = clock
        self._cookie_name = cookie_name
        self._anonymous_routes = anonymous_routes

    async def bind(self, request: HttpRequest, handler: HttpHandler) -> HttpResult:
        session_id = _session_cookie(request.headers, self._cookie_name)
        if session_id is None:
            if (request.method, urlsplit(request.path).path) not in self._anonymous_routes:
                raise _error("SESSION_REQUIRED", HttpErrorClass.AUTHENTICATION, "$.session")
            principal = SessionPrincipal("", "", ())
        else:
            try:
                session = self._sessions.derive(session_id, now=self._clock())
            except SessionAuthenticationError as error:
                raise _error(
                    "SESSION_NOT_ACTIVE", HttpErrorClass.AUTHENTICATION, "$.session"
                ) from error
            principal = SessionPrincipal(
                session.session_id,
                session.principal_subject_id,
                session.capability_ids,
            )
        return await handler(SessionRequest(request, principal))


@dataclass(frozen=True, slots=True)
class _MountedRoute:
    spec: RouteSpec
    pattern: re.Pattern[str]


class PublishedHttpApplication:
    """Framework-neutral in-process adapter over one frozen route registry."""

    def __init__(
        self,
        *,
        routers: Sequence[RouterProtocol],
        middleware: PublishedSessionMiddleware,
    ) -> None:
        registry = RouteRegistry()
        for router in routers:
            registry.register(router)
        self._routes = registry.routes
        self._mounted = tuple(
            _MountedRoute(route, _compile_template(route.path)) for route in self._routes
        )
        self._middleware = middleware

    @property
    def routes(self) -> tuple[RouteSpec, ...]:
        return self._routes

    async def __call__(self, request: HttpRequest) -> HttpResult:
        path = urlsplit(request.path)
        if path.fragment or not path.path.startswith("/"):
            return error_response(_error("REQUEST_PATH_INVALID", HttpErrorClass.SCHEMA, "$.path"))
        matching_path = tuple(
            route for route in self._mounted if route.pattern.fullmatch(path.path)
        )
        selected = next(
            (route.spec for route in matching_path if route.spec.method == request.method), None
        )
        if selected is None:
            if matching_path:
                return HttpResponse(
                    405,
                    {
                        "schema_version": "rk.product.error.v1",
                        "code": "METHOD_NOT_ALLOWED",
                        "error_class": "SCHEMA",
                        "path": "$.method",
                    },
                    {
                        "content-type": "application/json",
                        "allow": ", ".join(
                            sorted({item.spec.method for item in matching_path})
                        ),
                    },
                )
            return error_response(_error("ROUTE_NOT_FOUND", HttpErrorClass.NOT_FOUND, "$.path"))
        try:
            return await self._middleware.bind(request, selected.handler)
        except ProductHttpError as error:
            return error_response(error)


def build_published_app(
    *,
    config: PublishedAppConfig,
    sessions: SessionStore,
    clock: Callable[[], str],
    routes: PublishedAppRoutes,
) -> PublishedHttpApplication:
    """Build the only published route graph; startup fails on every ambiguity."""

    artifact = artifact_router_factory(
        artifacts=routes.artifacts,
        logs=routes.logs,
        authorizer=routes.artifact_authorizer,
        other_operations=routes.upload.handle,
    )
    middleware = PublishedSessionMiddleware(
        sessions,
        clock=clock,
        cookie_name=config.cookie_name,
        anonymous_routes=frozenset({("POST", "/v1/session/login"), ("GET", "/v1/meta")}),
    )
    return PublishedHttpApplication(
        routers=(
            routes.command,
            routes.query,
            artifact,
            routes.activity,
            routes.identity,
            routes.review,
            _MetaRouter(config),
        ),
        middleware=middleware,
    )


def _compile_template(template: str) -> re.Pattern[str]:
    pieces: list[str] = []
    cursor = 0
    for match in _PARAMETER.finditer(template):
        pieces.append(re.escape(template[cursor : match.start()]))
        pieces.append(r"[^/]+")
        cursor = match.end()
    pieces.append(re.escape(template[cursor:]))
    return re.compile("^" + "".join(pieces) + "$")


def _session_cookie(headers: Mapping[str, str], name: str) -> str | None:
    cookie_headers = [value for key, value in headers.items() if key.casefold() == "cookie"]
    if len(cookie_headers) > 1:
        raise _error("SESSION_COOKIE_AMBIGUOUS", HttpErrorClass.AUTHENTICATION, "$.headers.cookie")
    if not cookie_headers:
        return None
    values: list[str] = []
    for item in cookie_headers[0].split(";"):
        key, separator, value = item.strip().partition("=")
        if not separator:
            raise _error(
                "SESSION_COOKIE_INVALID",
                HttpErrorClass.AUTHENTICATION,
                "$.headers.cookie",
            )
        if key == name:
            values.append(value)
    if not values:
        return None
    if len(values) != 1 or not values[0] or _COOKIE_VALUE.fullmatch(values[0]) is None:
        raise _error("SESSION_COOKIE_INVALID", HttpErrorClass.AUTHENTICATION, "$.headers.cookie")
    return values[0]


def _error(code: str, error_class: HttpErrorClass, path: str) -> ProductHttpError:
    return ProductHttpError(code=code, error_class=error_class, path=path)


__all__ = [
    "PublishedAppConfig",
    "PublishedAppRoutes",
    "PublishedHttpApplication",
    "PublishedSessionMiddleware",
    "build_published_app",
]
