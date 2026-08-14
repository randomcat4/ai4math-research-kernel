from __future__ import annotations

import asyncio
import json
from types import MappingProxyType
from typing import cast

from rk.http_shell import HttpRequest, HttpResponse, RouteRegistry, SessionPrincipal, SessionRequest
from rk.product.api import ProductSession, QueryResult, QuerySpec
from rk.product.query_routes import QueryRouter, query_router_factory
from rk.product.query_service import (
    ProductQueryService,
    QueryAuthenticationError,
    QueryVariantUnavailable,
)


class RecordingService:
    def __init__(self) -> None:
        self.calls: list[tuple[ProductSession, QuerySpec]] = []

    def execute(self, session: ProductSession, spec: QuerySpec) -> QueryResult:
        self.calls.append((session, spec))
        if session.capability_ids != ("cap-1",):
            raise QueryAuthenticationError("stale")
        if spec.query_type == "UNKNOWN":
            raise QueryVariantUnavailable("UNKNOWN")
        if "role" in spec.payload:
            raise ValueError("injected authority")
        kind = str(spec.scope["kind"])
        fence: dict[str, object] = {"scope_kind": kind, "last_cursor": 7}
        if kind == "RUN":
            fence.update(
                {
                    "run_id": spec.scope["run_id"],
                    "research_revision": 3,
                    "contract_version": 2,
                }
            )
        else:
            fence.update({"deployment_id": spec.scope["deployment_id"], "catalog_revision": 5})
        return QueryResult(
            spec.query_type,
            f"stable:{spec.query_type}",
            cast(MappingProxyType[str, object], MappingProxyType(fence)),
            MappingProxyType({"echo": spec.query_type}),
        )


def _principal(capability: str = "cap-1") -> SessionPrincipal:
    return SessionPrincipal("session-1", "subject-1", (capability,))


def _invoke(
    router: QueryRouter, name: str, request: HttpRequest, principal: SessionPrincipal | None = None
) -> HttpResponse:
    route = next(item for item in router.routes() if item.name == name)

    async def call() -> HttpResponse:
        result = await route.handler(SessionRequest(request, principal or _principal()))
        assert isinstance(result, HttpResponse)
        return result

    return asyncio.run(call())


def _router() -> tuple[QueryRouter, RecordingService]:
    service = RecordingService()
    return (
        query_router_factory(
            service=cast(ProductQueryService, service), deployment_id="deployment-1"
        ),
        service,
    )


def _body(scope: dict[str, object], query_type: str, payload: dict[str, object]) -> bytes:
    return json.dumps(
        {
            "schema_version": "rk.product.query.v1",
            "scope": scope,
            "query": {"type": query_type, "payload": payload},
        }
    ).encode()


def test_router_registers_public_query_shapes_without_conflict() -> None:
    router, _ = _router()
    registry = RouteRegistry()
    registry.register(router)
    assert {(route.method, route.path) for route in registry.routes} == {
        ("GET", "/v1/research"),
        ("POST", "/v1/research/{run_id}/queries"),
        ("POST", "/v1/deployment/queries"),
    }


def test_get_research_decodes_query_string_and_returns_query_result_envelope() -> None:
    router, service = _router()
    response = _invoke(
        router,
        "research-list-query",
        HttpRequest(
            "GET",
            "/v1/research?limit=20&owner=alice&owner=bob&label=analysis&sort=RECENT_ACTIVITY_DESC",
        ),
    )

    assert response.status == 200
    assert response.body["schema_version"] == "rk.product.query_result.v1"
    assert response.body["result_type"] == "LIST_RESEARCH"
    assert response.body["scope_kind"] == "GLOBAL"
    _, spec = service.calls[0]
    assert spec.scope == {"kind": "GLOBAL", "deployment_id": "deployment-1"}
    assert spec.payload["owners"] == ["alice", "bob"]
    assert spec.payload["labels"] == ["analysis"]


def test_run_query_binds_path_scope_and_uses_session_principal() -> None:
    router, service = _router()
    response = _invoke(
        router,
        "run-product-query",
        HttpRequest(
            "POST",
            "/v1/research/run-1/queries",
            headers={"content-type": "application/json; charset=utf-8"},
            body=_body(
                {
                    "kind": "RUN",
                    "run_id": "run-1",
                    "at_revision": 3,
                    "at_contract_version": 2,
                },
                "GRAPH_SEARCH",
                {
                    "page": {"limit": 20},
                    "text": "spectral",
                    "mode": "VERIFIED",
                    "at_revision": 3,
                },
            ),
        ),
    )

    assert response.status == 200
    assert response.body["run_id"] == "run-1"
    session, spec = service.calls[0]
    assert session == ProductSession("session-1", "subject-1", ("cap-1",))
    assert spec.query_type == "GRAPH_SEARCH"


def test_global_action_items_use_deployment_query_route() -> None:
    router, service = _router()
    response = _invoke(
        router,
        "deployment-product-query",
        HttpRequest(
            "POST",
            "/v1/deployment/queries",
            headers={"content-type": "application/json"},
            body=_body(
                {"kind": "GLOBAL", "deployment_id": "deployment-1"},
                "ACTION_ITEMS",
                {"page": {"limit": 50}},
            ),
        ),
    )
    assert response.status == 200
    assert response.body["deployment_id"] == "deployment-1"
    assert service.calls[0][1].query_type == "ACTION_ITEMS"


def test_unknown_variant_is_503_and_never_a_fake_empty_result() -> None:
    router, _ = _router()
    response = _invoke(
        router,
        "run-product-query",
        HttpRequest(
            "POST",
            "/v1/research/run-1/queries",
            headers={"content-type": "application/json"},
            body=_body({"kind": "RUN", "run_id": "run-1"}, "UNKNOWN", {}),
        ),
    )
    assert response.status == 503
    assert response.body["code"] == "QUERY_VARIANT_UNAVAILABLE"


def test_scope_mismatch_duplicate_json_and_authority_injection_are_rejected() -> None:
    router, _ = _router()
    mismatch = _invoke(
        router,
        "run-product-query",
        HttpRequest(
            "POST",
            "/v1/research/run-1/queries",
            headers={"content-type": "application/json"},
            body=_body({"kind": "RUN", "run_id": "run-2"}, "JOB", {"job_id": "job-1"}),
        ),
    )
    assert mismatch.status == 403

    duplicate = _invoke(
        router,
        "run-product-query",
        HttpRequest(
            "POST",
            "/v1/research/run-1/queries",
            headers={"content-type": "application/json"},
            body=(
                b'{"schema_version":"rk.product.query.v1","schema_version":"x",'
                b'"scope":{},"query":{}}'
            ),
        ),
    )
    assert duplicate.status == 400

    injected = _invoke(
        router,
        "run-product-query",
        HttpRequest(
            "POST",
            "/v1/research/run-1/queries",
            headers={"content-type": "application/json"},
            body=_body(
                {"kind": "RUN", "run_id": "run-1"},
                "JOB",
                {"job_id": "job-1", "role": "ADMIN"},
            ),
        ),
    )
    assert injected.status == 400
    assert injected.body["code"] == "QUERY_SPEC_INVALID"


def test_stale_session_principal_is_401() -> None:
    router, _ = _router()
    response = _invoke(
        router,
        "research-list-query",
        HttpRequest("GET", "/v1/research?limit=20"),
        _principal("forged-capability"),
    )
    assert response.status == 401
    assert response.body["code"] == "QUERY_SESSION_INVALID"
