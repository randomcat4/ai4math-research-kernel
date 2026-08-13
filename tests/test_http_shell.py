from __future__ import annotations

from collections.abc import Sequence

import pytest

from rk.http_shell import (
    ConflictingRouteError,
    DuplicateRouteError,
    HttpErrorClass,
    HttpRequest,
    HttpResponse,
    ProductHttpError,
    RouteRegistry,
    RouteSpec,
    SessionRequest,
    error_response,
)


async def _handler(request: SessionRequest) -> HttpResponse:
    return HttpResponse(status=200, body={"path": request.request.path})


class _Router:
    def __init__(self, *routes: RouteSpec) -> None:
        self._routes = routes

    def routes(self) -> Sequence[RouteSpec]:
        return self._routes


def _route(method: str, path: str, name: str) -> RouteSpec:
    return RouteSpec(method=method, path=path, handler=_handler, name=name)


def test_registry_accepts_distinct_fake_router_routes() -> None:
    registry = RouteRegistry()
    registry.register(
        _Router(
            _route("POST", "/v1/research/{run_id}/commands", "command"),
            _route("POST", "/v1/research/{run_id}/queries", "query"),
        )
    )

    assert [(route.method, route.path) for route in registry.routes] == [
        ("POST", "/v1/research/{run_id}/commands"),
        ("POST", "/v1/research/{run_id}/queries"),
    ]


def test_registry_rejects_duplicate_route_across_routers() -> None:
    registry = RouteRegistry()
    registry.register(_Router(_route("GET", "/v1/meta", "meta-one")))

    with pytest.raises(DuplicateRouteError, match=r"GET /v1/meta"):
        registry.register(_Router(_route("GET", "/v1/meta", "meta-two")))


def test_registry_rejects_equivalent_parameterized_routes() -> None:
    registry = RouteRegistry()
    registry.register(_Router(_route("GET", "/v1/artifacts/{artifact_id}", "artifact")))

    with pytest.raises(ConflictingRouteError, match="conflicting routes"):
        registry.register(_Router(_route("GET", "/v1/artifacts/{name}", "artifact-by-name")))


def test_registry_allows_same_path_for_different_methods() -> None:
    registry = RouteRegistry()
    registry.register(_Router(_route("GET", "/v1/research", "list")))
    registry.register(_Router(_route("POST", "/v1/research", "create")))

    assert len(registry.routes) == 2


@pytest.mark.parametrize(
    ("error_class", "status"),
    [
        (HttpErrorClass.SCHEMA, 400),
        (HttpErrorClass.AUTHENTICATION, 401),
        (HttpErrorClass.AUTHORIZATION, 403),
        (HttpErrorClass.NOT_FOUND, 404),
        (HttpErrorClass.CONFLICT, 409),
        (HttpErrorClass.BUSINESS_GATE, 422),
        (HttpErrorClass.UNAVAILABLE, 503),
    ],
)
def test_product_error_mapping_is_stable(error_class: HttpErrorClass, status: int) -> None:
    response = error_response(
        ProductHttpError(
            code="RK_CODE",
            error_class=error_class,
            path="$.command",
            params={"run_id": "run-1", "revision": 7},
        )
    )

    assert response.status == status
    assert dict(response.body) == {
        "schema_version": "rk.http.problem.v1",
        "code": "RK_CODE",
        "path": "$.command",
        "params": {"run_id": "run-1", "revision": 7},
    }


def test_unhandled_error_is_500_and_does_not_leak_exception_text() -> None:
    response = error_response(RuntimeError("database password is secret"))

    assert response.status == 500
    assert dict(response.body) == {
        "schema_version": "rk.http.problem.v1",
        "code": "INTERNAL_ERROR",
        "path": "$",
        "params": {},
    }
    assert "secret" not in str(response.body)


def test_request_does_not_have_an_actor_or_capability_field() -> None:
    request = HttpRequest(method="POST", path="/v1/research", body=b'{"role":"ADMIN"}')

    assert request.body == b'{"role":"ADMIN"}'
    assert not hasattr(request, "principal")
    assert not hasattr(request, "capability")


@pytest.mark.parametrize(
    "method,path",
    [("get", "/v1/meta"), ("GET", "v1/meta"), ("GET", "/v1/meta?full=true")],
)
def test_route_spec_rejects_invalid_transport_declarations(method: str, path: str) -> None:
    with pytest.raises(ValueError):
        _route(method, path, "invalid")
