"""Framework-neutral contracts for the RK product HTTP adapter.

This module owns only transport assembly.  Business routers are supplied by later
packages and remain responsible for decoding product commands and queries through the
single ``ResearchProduct`` facade.
"""

from __future__ import annotations

import re
from collections.abc import Awaitable, Callable, Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol

JsonScalar = str | int | bool | None
JsonValue = JsonScalar | list["JsonValue"] | dict[str, "JsonValue"]
HeaderMap = Mapping[str, str]

_PATH_PARAMETER = re.compile(r"\{[A-Za-z_][A-Za-z0-9_]*\}")
_HTTP_METHOD = re.compile(r"^[A-Z]+$")


class RouteRegistrationError(ValueError):
    """A router cannot be assembled into one unambiguous HTTP application."""


class DuplicateRouteError(RouteRegistrationError):
    """The same method and literal route were registered more than once."""


class ConflictingRouteError(RouteRegistrationError):
    """Two route templates match the same method and URL shape."""


@dataclass(frozen=True, slots=True)
class HttpRequest:
    method: str
    path: str
    headers: HeaderMap = field(default_factory=lambda: MappingProxyType({}))
    body: bytes = b""


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    body: Mapping[str, JsonValue]
    headers: HeaderMap = field(
        default_factory=lambda: MappingProxyType({"content-type": "application/json"})
    )


@dataclass(frozen=True, slots=True)
class HttpStreamResponse:
    """A transport response whose bytes are pulled incrementally by the HTTP server."""

    status: int
    body: Iterable[bytes]
    headers: HeaderMap


type HttpResult = HttpResponse | HttpStreamResponse


@dataclass(frozen=True, slots=True)
class SessionPrincipal:
    """Identity established by the session adapter, never by request JSON."""

    session_id: str
    subject_id: str
    capability_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class SessionRequest:
    request: HttpRequest
    principal: SessionPrincipal


HttpHandler = Callable[[SessionRequest], Awaitable[HttpResult]]
AnonymousHttpHandler = Callable[[HttpRequest], Awaitable[HttpResult]]


@dataclass(frozen=True, slots=True)
class RouteSpec:
    method: str
    path: str
    handler: HttpHandler
    name: str

    def __post_init__(self) -> None:
        if not _HTTP_METHOD.fullmatch(self.method):
            raise ValueError("route method must be an uppercase HTTP token")
        if not self.path.startswith("/") or "?" in self.path or "#" in self.path:
            raise ValueError("route path must be an absolute path without query or fragment")
        if not self.name:
            raise ValueError("route name must be non-empty")


class RouterProtocol(Protocol):
    """A business package contributes routes without owning app composition."""

    def routes(self) -> Sequence[RouteSpec]: ...


class SessionMiddlewareProtocol(Protocol):
    """Bind an authenticated principal before a business handler is entered."""

    def bind(self, handler: HttpHandler) -> AnonymousHttpHandler: ...


class ErrorMiddlewareProtocol(Protocol):
    """Convert failures at the outer HTTP boundary to stable responses."""

    def wrap(self, handler: AnonymousHttpHandler) -> AnonymousHttpHandler: ...


class HttpApplicationProtocol(Protocol):
    async def __call__(self, request: HttpRequest) -> HttpResult: ...


class AppFactoryProtocol(Protocol):
    def __call__(
        self,
        *,
        routers: Sequence[RouterProtocol],
        session_middleware: SessionMiddlewareProtocol,
        error_middleware: ErrorMiddlewareProtocol,
    ) -> HttpApplicationProtocol: ...


def _canonical_path(path: str) -> str:
    return _PATH_PARAMETER.sub("{}", path)


class RouteRegistry:
    """Collect route declarations and reject ambiguous application graphs."""

    def __init__(self) -> None:
        self._routes: dict[tuple[str, str], RouteSpec] = {}
        self._canonical: dict[tuple[str, str], RouteSpec] = {}

    def register(self, router: RouterProtocol) -> None:
        for route in router.routes():
            literal_key = (route.method, route.path)
            if literal_key in self._routes:
                raise DuplicateRouteError(f"duplicate route: {route.method} {route.path}")

            canonical_key = (route.method, _canonical_path(route.path))
            conflicting = self._canonical.get(canonical_key)
            if conflicting is not None:
                raise ConflictingRouteError(
                    "conflicting routes: "
                    f"{conflicting.method} {conflicting.path} and {route.method} {route.path}"
                )

            self._routes[literal_key] = route
            self._canonical[canonical_key] = route

    @property
    def routes(self) -> tuple[RouteSpec, ...]:
        return tuple(self._routes.values())


class HttpErrorClass(StrEnum):
    SCHEMA = "SCHEMA"
    AUTHENTICATION = "AUTHENTICATION"
    AUTHORIZATION = "AUTHORIZATION"
    NOT_FOUND = "NOT_FOUND"
    CONFLICT = "CONFLICT"
    BUSINESS_GATE = "BUSINESS_GATE"
    GONE = "GONE"
    UNAVAILABLE = "UNAVAILABLE"
    RANGE = "RANGE"


_STATUS_BY_ERROR_CLASS: Mapping[HttpErrorClass, int] = MappingProxyType(
    {
        HttpErrorClass.SCHEMA: 400,
        HttpErrorClass.AUTHENTICATION: 401,
        HttpErrorClass.AUTHORIZATION: 403,
        HttpErrorClass.NOT_FOUND: 404,
        HttpErrorClass.CONFLICT: 409,
        HttpErrorClass.BUSINESS_GATE: 422,
        HttpErrorClass.GONE: 410,
        HttpErrorClass.UNAVAILABLE: 503,
        HttpErrorClass.RANGE: 416,
    }
)


@dataclass(frozen=True, slots=True)
class ProductHttpError(Exception):
    """A classified product error ready to cross the HTTP adapter boundary."""

    code: str
    error_class: HttpErrorClass
    path: str
    params: Mapping[str, JsonValue] = field(default_factory=lambda: MappingProxyType({}))

    def __post_init__(self) -> None:
        if not self.code or not self.path:
            raise ValueError("HTTP product errors require code and path")


def error_response(error: BaseException) -> HttpResponse:
    """Map a classified product error, or an unhandled failure, without leaking internals."""

    if isinstance(error, ProductHttpError):
        status = _STATUS_BY_ERROR_CLASS[error.error_class]
        body: Mapping[str, JsonValue] = MappingProxyType(
            {
                "schema_version": "rk.http.problem.v1",
                "code": error.code,
                "path": error.path,
                "params": dict(error.params),
            }
        )
        return HttpResponse(status=status, body=body)

    return HttpResponse(
        status=500,
        body=MappingProxyType(
            {
                "schema_version": "rk.http.problem.v1",
                "code": "INTERNAL_ERROR",
                "path": "$",
                "params": {},
            }
        ),
    )


__all__ = [
    "AnonymousHttpHandler",
    "AppFactoryProtocol",
    "ConflictingRouteError",
    "DuplicateRouteError",
    "ErrorMiddlewareProtocol",
    "HttpApplicationProtocol",
    "HttpErrorClass",
    "HttpHandler",
    "HttpRequest",
    "HttpResponse",
    "HttpResult",
    "HttpStreamResponse",
    "ProductHttpError",
    "RouteRegistrationError",
    "RouteRegistry",
    "RouteSpec",
    "RouterProtocol",
    "SessionMiddlewareProtocol",
    "SessionPrincipal",
    "SessionRequest",
    "error_response",
]
