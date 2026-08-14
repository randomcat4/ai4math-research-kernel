"""HTTP adapters for the single product query operation family."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any, cast
from urllib.parse import parse_qs, urlsplit

from rk.http_shell import (
    HttpErrorClass,
    HttpResponse,
    JsonValue,
    ProductHttpError,
    RouteSpec,
    SessionPrincipal,
    SessionRequest,
    error_response,
)
from rk.product.api import JsonObject, ProductSession, QueryResult, QuerySpec
from rk.product.graph_index import ProjectionLag
from rk.product.graph_query import (
    GraphQueryError,
    GraphSeedNotFound,
    InvalidQueryCursor,
    StaleQuery,
)
from rk.product.query_service import (
    ProductQueryService,
    ProductQueryServiceError,
    QueryAuthenticationError,
    QueryCursorInvalid,
    QueryObjectNotFound,
    QueryScopeDenied,
    QueryVariantUnavailable,
)
from rk.product.receipt_query import ProductObjectNotFound, ProductQueryScopeMismatch

_RUN_QUERY_PATH = re.compile(r"^/v1/research/([A-Za-z0-9_.:-]+)/queries$")


class QueryRouter:
    """Decode public HTTP shapes into the one QuerySpec dispatch service."""

    def __init__(self, service: ProductQueryService, *, deployment_id: str) -> None:
        if not deployment_id:
            raise ValueError("deployment_id must be non-empty")
        self._service = service
        self._deployment_id = deployment_id
        self._routes = (
            RouteSpec("GET", "/v1/research", self.list_research, "research-list-query"),
            RouteSpec(
                "POST",
                "/v1/research/{run_id}/queries",
                self.run_query,
                "run-product-query",
            ),
            RouteSpec(
                "POST",
                "/v1/deployment/queries",
                self.deployment_query,
                "deployment-product-query",
            ),
        )

    def routes(self) -> Sequence[RouteSpec]:
        return self._routes

    async def list_research(self, request: SessionRequest) -> HttpResponse:
        if request.request.body:
            return _problem("REQUEST_BODY_NOT_ALLOWED", HttpErrorClass.SCHEMA, "$")
        try:
            spec = _list_spec(request.request.path, self._deployment_id)
            return await self._execute(request.principal, spec)
        except ValueError:
            return _problem("LIST_RESEARCH_QUERY_INVALID", HttpErrorClass.SCHEMA, "$.query")

    async def run_query(self, request: SessionRequest) -> HttpResponse:
        split = urlsplit(request.request.path)
        match = _RUN_QUERY_PATH.fullmatch(split.path)
        if match is None or split.query or split.fragment:
            return _problem("RUN_QUERY_PATH_INVALID", HttpErrorClass.SCHEMA, "$.path")
        try:
            spec = _query_spec(request)
        except ValueError:
            return _problem("QUERY_ENVELOPE_INVALID", HttpErrorClass.SCHEMA, "$")
        if spec.scope.get("kind") != "RUN" or spec.scope.get("run_id") != match.group(1):
            return _problem("QUERY_PATH_SCOPE_MISMATCH", HttpErrorClass.AUTHORIZATION, "$.scope")
        return await self._execute(request.principal, spec)

    async def deployment_query(self, request: SessionRequest) -> HttpResponse:
        split = urlsplit(request.request.path)
        if split.path != "/v1/deployment/queries" or split.query or split.fragment:
            return _problem("DEPLOYMENT_QUERY_PATH_INVALID", HttpErrorClass.SCHEMA, "$.path")
        try:
            spec = _query_spec(request)
        except ValueError:
            return _problem("QUERY_ENVELOPE_INVALID", HttpErrorClass.SCHEMA, "$")
        if spec.scope.get("kind") not in {"GLOBAL", "DEPLOYMENT"}:
            return _problem("QUERY_PATH_SCOPE_MISMATCH", HttpErrorClass.AUTHORIZATION, "$.scope")
        return await self._execute(request.principal, spec)

    async def _execute(self, principal: SessionPrincipal, spec: QuerySpec) -> HttpResponse:
        try:
            session = _session(principal)
            result = await asyncio.to_thread(self._service.execute, session, spec)
        except QueryAuthenticationError:
            return _problem("QUERY_SESSION_INVALID", HttpErrorClass.AUTHENTICATION, "$.session")
        except (QueryScopeDenied, ProductQueryScopeMismatch):
            return _problem("QUERY_SCOPE_DENIED", HttpErrorClass.AUTHORIZATION, "$.scope")
        except (QueryObjectNotFound, ProductObjectNotFound, GraphSeedNotFound):
            return _problem("QUERY_OBJECT_NOT_FOUND", HttpErrorClass.NOT_FOUND, "$.query.payload")
        except (StaleQuery, ProjectionLag):
            return _problem("STALE_QUERY", HttpErrorClass.CONFLICT, "$.query")
        except QueryVariantUnavailable:
            return _problem("QUERY_VARIANT_UNAVAILABLE", HttpErrorClass.UNAVAILABLE, "$.query.type")
        except (QueryCursorInvalid, InvalidQueryCursor):
            return _problem(
                "QUERY_CURSOR_INVALID", HttpErrorClass.SCHEMA, "$.query.payload.page.cursor"
            )
        except (GraphQueryError, ValueError):
            return _problem("QUERY_SPEC_INVALID", HttpErrorClass.SCHEMA, "$.query")
        except ProductQueryServiceError:
            return _problem("QUERY_UNAVAILABLE", HttpErrorClass.UNAVAILABLE, "$.query")
        return HttpResponse(200, _result_body(result))


def query_router_factory(*, service: ProductQueryService, deployment_id: str) -> QueryRouter:
    return QueryRouter(service, deployment_id=deployment_id)


def _list_spec(path: str, deployment_id: str) -> QuerySpec:
    split = urlsplit(path)
    if split.path != "/v1/research" or split.fragment:
        raise ValueError("research list path is invalid")
    try:
        values = parse_qs(split.query, keep_blank_values=True, strict_parsing=True)
    except ValueError as error:
        raise ValueError("research list query string is invalid") from error
    allowed = {
        "limit",
        "cursor",
        "owner",
        "label",
        "outcome",
        "text",
        "sort",
        "at_catalog_revision",
    }
    if set(values) - allowed or "limit" not in values:
        raise ValueError("research list query fields are invalid")
    limit = _one_integer(values, "limit")
    payload: dict[str, JsonValue] = {"page": {"limit": limit}}
    cursor = _optional_one(values, "cursor")
    if cursor is not None:
        cast(dict[str, JsonValue], payload["page"])["cursor"] = cursor
    for wire, field in (("owner", "owners"), ("label", "labels"), ("outcome", "outcomes")):
        if wire in values:
            payload[field] = _nonempty_values(values[wire])
    for field in ("text", "sort"):
        item = _optional_one(values, field)
        if item is not None:
            payload[field] = item
    scope: dict[str, JsonValue] = {"kind": "GLOBAL", "deployment_id": deployment_id}
    if "at_catalog_revision" in values:
        scope["at_catalog_revision"] = _one_integer(values, "at_catalog_revision")
    return QuerySpec(cast(JsonObject, scope), "LIST_RESEARCH", cast(JsonObject, payload))


class _DuplicateJsonKey(ValueError):
    pass


def _query_spec(request: SessionRequest) -> QuerySpec:
    content_type = _header(request.request.headers, "content-type")
    if (
        content_type is None
        or content_type.partition(";")[0].strip().casefold() != "application/json"
    ):
        raise ValueError("JSON content type is required")

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
        raise ValueError("query body is invalid JSON") from error
    if not isinstance(value, dict) or set(value) != {"schema_version", "scope", "query"}:
        raise ValueError("query envelope fields are invalid")
    if value["schema_version"] != "rk.product.query.v1":
        raise ValueError("query schema version is invalid")
    scope, query = value["scope"], value["query"]
    if (
        not isinstance(scope, dict)
        or not isinstance(query, dict)
        or set(query) != {"type", "payload"}
    ):
        raise ValueError("query envelope union is invalid")
    query_type, payload = query["type"], query["payload"]
    if not isinstance(query_type, str) or not query_type or not isinstance(payload, dict):
        raise ValueError("query type or payload is invalid")
    return QuerySpec(cast(JsonObject, scope), query_type, cast(JsonObject, payload))


def _session(principal: SessionPrincipal) -> ProductSession:
    if not principal.session_id or not principal.subject_id or not principal.capability_ids:
        raise QueryAuthenticationError("session principal is absent")
    return ProductSession(principal.session_id, principal.subject_id, principal.capability_ids)


def _result_body(result: QueryResult) -> Mapping[str, JsonValue]:
    return cast(
        Mapping[str, JsonValue],
        {
            "schema_version": "rk.product.query_result.v1",
            "result_type": result.result_type,
            "stable_entity_id": result.stable_entity_id,
            **dict(result.fence),
            "result": dict(result.data),
        },
    )


def _one_integer(values: Mapping[str, list[str]], name: str) -> int:
    raw = _one(values, name)
    if not raw.isascii() or not raw.isdecimal():
        raise ValueError(f"{name} must be a non-negative decimal")
    return int(raw)


def _optional_one(values: Mapping[str, list[str]], name: str) -> str | None:
    return _one(values, name) if name in values else None


def _one(values: Mapping[str, list[str]], name: str) -> str:
    items = values.get(name)
    if items is None or len(items) != 1 or not items[0]:
        raise ValueError(f"{name} must occur exactly once and be non-empty")
    return items[0]


def _nonempty_values(values: list[str]) -> list[JsonValue]:
    if not values or any(not value for value in values) or len(values) != len(set(values)):
        raise ValueError("repeated query values must be non-empty and unique")
    return list(values)


def _header(headers: Mapping[str, str], name: str) -> str | None:
    lowered = name.casefold()
    matches = [value for key, value in headers.items() if key.casefold() == lowered]
    if len(matches) > 1 and len(set(matches)) != 1:
        raise ValueError("conflicting header values")
    return matches[0] if matches else None


def _problem(code: str, error_class: HttpErrorClass, path: str) -> HttpResponse:
    return error_response(ProductHttpError(code=code, error_class=error_class, path=path))


__all__ = ["QueryRouter", "query_router_factory"]
