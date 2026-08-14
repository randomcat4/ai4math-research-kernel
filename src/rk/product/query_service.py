"""Unified, session-authorized product query assembly over persisted read stores."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any, Literal, cast

from rk.product.action_items import RunActions, aggregate_action_items
from rk.product.api import JsonObject, ProductSession, QueryResult, QuerySpec
from rk.product.graph_query import (
    ClosureRequest,
    GraphFilters,
    GraphQueryService,
    GraphSearchPage,
    GraphSearchRequest,
    GraphSlice,
    GraphSliceRequest,
    StaleQuery,
)
from rk.product.listing import (
    CatalogCursorError,
    CatalogFenceChanged,
    ResearchCatalog,
    ResearchListQuery,
)
from rk.product.receipt_query import ReceiptJobQuery
from rk.product.sessions import SessionAuthenticationError, SessionStore


class ProductQueryServiceError(RuntimeError):
    """Unified product query assembly failed with a stable public category."""


class QueryVariantUnavailable(ProductQueryServiceError):
    code = "QUERY_VARIANT_UNAVAILABLE"
    http_status = 503

    def __init__(self, query_type: str) -> None:
        self.query_type = query_type
        super().__init__(f"query variant is not implemented: {query_type}")


class QueryAuthenticationError(ProductQueryServiceError):
    code = "QUERY_SESSION_INVALID"
    http_status = 401


class QueryScopeDenied(ProductQueryServiceError):
    code = "QUERY_SCOPE_DENIED"
    http_status = 403


class QueryObjectNotFound(ProductQueryServiceError):
    code = "QUERY_OBJECT_NOT_FOUND"
    http_status = 404


class QueryCursorInvalid(ProductQueryServiceError):
    code = "QUERY_CURSOR_INVALID"
    http_status = 400


@dataclass(frozen=True, slots=True)
class RunQueryFence:
    run_id: str
    research_revision: int
    contract_version: int
    last_cursor: int


@dataclass(frozen=True, slots=True)
class CatalogQueryFence:
    deployment_id: str
    catalog_revision: int
    last_cursor: int


class SQLiteQueryFenceSource:
    """Read existing catalog/summary projections; never derive new authority."""

    def __init__(self, db_path: Path, deployment_id: str) -> None:
        if not deployment_id:
            raise ValueError("deployment_id must be non-empty")
        self._db_path = Path(db_path)
        self._deployment_id = deployment_id

    def run(self, run_id: str) -> RunQueryFence:
        with self._connect() as connection:
            connection.execute("BEGIN")
            row = connection.execute(
                "SELECT research_revision,contract_version,last_cursor "
                "FROM research_summary_projection WHERE run_id=?",
                (run_id,),
            ).fetchone()
            connection.commit()
        if row is None:
            raise QueryObjectNotFound(run_id)
        return RunQueryFence(run_id, int(row[0]), int(row[1]), int(row[2]))

    def catalog(self) -> CatalogQueryFence:
        with self._connect() as connection:
            connection.execute("BEGIN")
            row = connection.execute(
                "SELECT revision FROM research_catalog_fence WHERE singleton=1"
            ).fetchone()
            cursor_row = connection.execute(
                "SELECT COALESCE(MAX(last_cursor),0) FROM research_summary_projection"
            ).fetchone()
            connection.commit()
        if row is None or cursor_row is None:
            raise ProductQueryServiceError("catalog fence projection is unavailable")
        return CatalogQueryFence(self._deployment_id, int(row[0]), int(cursor_row[0]))

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._db_path)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection


class SessionQueryAuthorizer:
    """Authenticate the middleware principal and constrain it to one deployment."""

    def __init__(
        self,
        sessions: SessionStore,
        *,
        deployment_id: str,
        clock: Callable[[], str],
    ) -> None:
        if not deployment_id:
            raise ValueError("deployment_id must be non-empty")
        self._sessions = sessions
        self._deployment_id = deployment_id
        self._clock = clock

    def authorize(self, session: ProductSession, scope: JsonObject) -> None:
        try:
            current = self._sessions.derive(session.session_id, now=self._clock())
        except SessionAuthenticationError as error:
            raise QueryAuthenticationError("session is not active") from error
        if current != session:
            raise QueryAuthenticationError("session principal or capability is stale")
        kind = scope.get("kind")
        if kind == "RUN":
            _scope_string(scope, "run_id")
            return
        if kind in {"GLOBAL", "DEPLOYMENT"}:
            if _scope_string(scope, "deployment_id") != self._deployment_id:
                raise QueryScopeDenied("query crossed the configured deployment")
            return
        raise QueryScopeDenied("query scope kind is not authorized")


class _QueryCursorCodec:
    def __init__(self, secret: bytes) -> None:
        if len(secret) < 32:
            raise ValueError("query cursor secret must contain at least 32 bytes")
        self._secret = secret

    def encode(self, binding: Mapping[str, Any], boundary: Mapping[str, Any]) -> str:
        body = _canonical({"v": 1, "binding": binding, "boundary": boundary})
        signature = hmac.digest(self._secret, body, "sha256")
        return "rkq1." + base64.urlsafe_b64encode(body + signature).rstrip(b"=").decode()

    def decode(self, token: str, binding: Mapping[str, Any]) -> dict[str, Any]:
        if not token.startswith("rkq1."):
            raise QueryCursorInvalid("query cursor format is invalid")
        encoded = token[5:]
        try:
            raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        except ValueError as error:
            raise QueryCursorInvalid("query cursor base64url is invalid") from error
        if len(raw) <= 32:
            raise QueryCursorInvalid("query cursor is truncated")
        body, signature = raw[:-32], raw[-32:]
        if not hmac.compare_digest(signature, hmac.digest(self._secret, body, "sha256")):
            raise QueryCursorInvalid("query cursor signature is invalid")
        value = _json_object(body)
        if value.get("v") != 1 or value.get("binding") != dict(binding):
            raise StaleQuery("query cursor binding is stale")
        boundary = value.get("boundary")
        if not isinstance(boundary, dict):
            raise QueryCursorInvalid("query cursor boundary is invalid")
        return cast(dict[str, Any], boundary)


class ProductQueryService:
    """Dispatch only implemented query variants and preserve their source fences."""

    def __init__(
        self,
        *,
        catalog: ResearchCatalog,
        receipt_jobs: ReceiptJobQuery,
        graph: GraphQueryService,
        fences: SQLiteQueryFenceSource,
        authorizer: SessionQueryAuthorizer,
        cursor_secret: bytes,
    ) -> None:
        self._catalog = catalog
        self._receipt_jobs = receipt_jobs
        self._graph = graph
        self._fences = fences
        self._authorizer = authorizer
        self._cursors = _QueryCursorCodec(cursor_secret)

    def execute(self, session: ProductSession, spec: QuerySpec) -> QueryResult:
        self._authorizer.authorize(session, spec.scope)
        handlers: Mapping[str, Callable[[ProductSession, QuerySpec], QueryResult]] = {
            "LIST_RESEARCH": self._list_research,
            "ACTION_ITEMS": self._action_items,
            "PRODUCT_RECEIPT": self._receipt_or_job,
            "JOB": self._receipt_or_job,
            "GRAPH_SLICE": self._graph_slice,
            "GRAPH_SEARCH": self._graph_search,
            "DEPENDENCY_CLOSURE": self._dependency_closure,
            "REVERSE_CLOSURE": self._reverse_closure,
        }
        handler = handlers.get(spec.query_type)
        if handler is None:
            raise QueryVariantUnavailable(spec.query_type)
        return handler(session, spec)

    def _list_research(self, _session: ProductSession, spec: QuerySpec) -> QueryResult:
        deployment_id = _global_scope(spec.scope)
        page, owners, labels, outcomes, text, sort = _list_payload(spec.payload)
        if sort not in (None, "RECENT_ACTIVITY_DESC"):
            raise QueryVariantUnavailable(f"LIST_RESEARCH:{sort}")
        fence = self._fences.catalog()
        _expected_catalog_revision(spec.scope, fence.catalog_revision)
        binding = {
            "kind": "LIST_RESEARCH",
            "deployment_id": deployment_id,
            "catalog_revision": fence.catalog_revision,
            "query_digest": _digest(
                {
                    "owners": owners,
                    "labels": labels,
                    "outcomes": outcomes,
                    "text": text,
                    "sort": sort,
                    "limit": page[0],
                }
            ),
        }
        inner_cursor = None
        stable_total: int | None = None
        if page[1] is not None:
            boundary = self._cursors.decode(page[1], binding)
            if (
                set(boundary) != {"catalog_cursor", "total"}
                or not isinstance(boundary["catalog_cursor"], str)
                or isinstance(boundary["total"], bool)
                or not isinstance(boundary["total"], int)
                or boundary["total"] < 0
            ):
                raise QueryCursorInvalid("research cursor boundary is invalid")
            inner_cursor = boundary["catalog_cursor"]
            stable_total = boundary["total"]
        try:
            result = self._catalog.list(
                ResearchListQuery(page[0], owners, labels, outcomes, text, inner_cursor)
            )
        except (CatalogCursorError, CatalogFenceChanged) as error:
            raise StaleQuery("research catalog cursor is stale") from error
        if result.catalog_revision != fence.catalog_revision:
            raise StaleQuery("research catalog changed during query")
        total = result.total if stable_total is None else stable_total
        if stable_total is not None and result.total > stable_total:
            raise StaleQuery("research catalog page boundary changed")
        next_cursor = (
            self._cursors.encode(binding, {"catalog_cursor": result.next_cursor, "total": total})
            if result.next_cursor is not None
            else None
        )
        items = tuple(_research_item(item) for item in result.items)
        data: dict[str, Any] = {
            "items": list(items),
            "page": _page_info(len(items), total, next_cursor),
        }
        return QueryResult(
            "LIST_RESEARCH",
            f"research-catalog:{deployment_id}",
            _frozen(
                {
                    "scope_kind": "GLOBAL",
                    "deployment_id": deployment_id,
                    "catalog_revision": fence.catalog_revision,
                    "last_cursor": fence.last_cursor,
                }
            ),
            _frozen(data),
        )

    def _action_items(self, session: ProductSession, spec: QuerySpec) -> QueryResult:
        deployment_id = _global_scope(spec.scope)
        limit, token = _page_payload(spec.payload)
        fence = self._fences.catalog()
        _expected_catalog_revision(spec.scope, fence.catalog_revision)
        binding = {
            "kind": "ACTION_ITEMS",
            "deployment_id": deployment_id,
            "catalog_revision": fence.catalog_revision,
            "subject_id": session.principal_subject_id,
            "limit": limit,
        }
        offset, last_id = 0, None
        if token is not None:
            boundary = self._cursors.decode(token, binding)
            if set(boundary) != {"offset", "last_id"}:
                raise QueryCursorInvalid("action cursor boundary is invalid")
            offset, last_id = boundary["offset"], boundary["last_id"]
            if (
                isinstance(offset, bool)
                or not isinstance(offset, int)
                or offset < 1
                or not isinstance(last_id, str)
                or not last_id
            ):
                raise QueryCursorInvalid("action cursor values are invalid")
        runs, observed_revision = self._all_runs()
        if observed_revision != fence.catalog_revision:
            raise StaleQuery("research catalog changed during action query")
        items = aggregate_action_items(runs, session.principal_subject_id)
        if offset > len(items) or (offset and items[offset - 1]["stable_entity_id"] != last_id):
            raise StaleQuery("action cursor boundary changed")
        selected = items[offset : offset + limit]
        end = offset + len(selected)
        next_cursor = None
        if end < len(items):
            next_cursor = self._cursors.encode(
                binding,
                {"offset": end, "last_id": selected[-1]["stable_entity_id"]},
            )
        return QueryResult(
            "ACTION_ITEMS",
            f"action-items:{session.principal_subject_id}",
            _frozen(
                {
                    "scope_kind": "GLOBAL",
                    "deployment_id": deployment_id,
                    "catalog_revision": fence.catalog_revision,
                    "last_cursor": fence.last_cursor,
                }
            ),
            _frozen(
                {
                    "items": [dict(item) for item in selected],
                    "page": _page_info(len(selected), len(items), next_cursor),
                }
            ),
        )

    def _receipt_or_job(self, _session: ProductSession, spec: QuerySpec) -> QueryResult:
        scope_kind = spec.scope.get("kind")
        if scope_kind == "RUN":
            fence = self._run_fence(spec.scope)
            source = self._receipt_jobs.execute(
                QuerySpec(
                    _frozen({"kind": "RUN", "run_id": fence.run_id}),
                    spec.query_type,
                    spec.payload,
                )
            )
            scope_fence: dict[str, Any] = {
                "scope_kind": "RUN",
                "run_id": fence.run_id,
                "research_revision": fence.research_revision,
                "contract_version": fence.contract_version,
                "last_cursor": fence.last_cursor,
            }
        elif scope_kind == "GLOBAL":
            deployment_id = _global_scope(spec.scope)
            fence_global = self._fences.catalog()
            _expected_catalog_revision(spec.scope, fence_global.catalog_revision)
            source = self._receipt_jobs.execute(
                QuerySpec(
                    _frozen({"kind": "GLOBAL", "deployment_id": deployment_id}),
                    spec.query_type,
                    spec.payload,
                )
            )
            scope_fence = {
                "scope_kind": "GLOBAL",
                "deployment_id": deployment_id,
                "catalog_revision": fence_global.catalog_revision,
                "last_cursor": fence_global.last_cursor,
            }
        else:
            raise QueryVariantUnavailable(f"{spec.query_type}:DEPLOYMENT_SCOPE")
        entity = {
            **dict(source.data),
            "schema_version": "rk.product.projection.v1",
            "stable_entity_id": source.stable_entity_id,
            "projection_type": source.result_type,
            "status": str(source.data.get("state", "UNKNOWN")),
            "artifact_ids": _artifact_ids(source.data),
        }
        return QueryResult(
            source.result_type,
            source.stable_entity_id,
            _frozen({**scope_fence, **dict(source.fence)}),
            _frozen({"entity": entity}),
        )

    def _graph_slice(self, _session: ProductSession, spec: QuerySpec) -> QueryResult:
        request = self._slice_request(spec)
        return _graph_result("GRAPH_SLICE", self._graph.slice(request), request.at_cursor)

    def _graph_search(self, _session: ProductSession, spec: QuerySpec) -> QueryResult:
        request = self._search_request(spec)
        return _search_result(self._graph.search(request), request.at_cursor)

    def _dependency_closure(self, _session: ProductSession, spec: QuerySpec) -> QueryResult:
        request = self._closure_request(spec)
        return _graph_result(
            "DEPENDENCY_CLOSURE", self._graph.dependency_closure(request), request.at_cursor
        )

    def _reverse_closure(self, _session: ProductSession, spec: QuerySpec) -> QueryResult:
        request = self._closure_request(spec)
        return _graph_result(
            "REVERSE_CLOSURE", self._graph.reverse_closure(request), request.at_cursor
        )

    def _slice_request(self, spec: QuerySpec) -> GraphSliceRequest:
        fence = self._run_fence(spec.scope)
        payload = dict(spec.payload)
        required = {
            "mode",
            "seed_ids",
            "direction",
            "depth",
            "filters",
            "node_limit",
            "at_revision",
        }
        _exact(payload, required, {"continuation_cursor"})
        _payload_revision(payload, fence.research_revision)
        filters = _filters(payload["filters"], str(payload["mode"]))
        return GraphSliceRequest(
            fence.run_id,
            fence.last_cursor,
            fence.research_revision,
            fence.contract_version,
            _mode(payload["mode"]),
            _strings(payload["seed_ids"], "seed_ids"),
            _direction(payload["direction"]),
            _integer(payload["depth"], "depth"),
            filters,
            _integer(payload["node_limit"], "node_limit"),
            _optional_string(payload.get("continuation_cursor"), "continuation_cursor"),
        )

    def _search_request(self, spec: QuerySpec) -> GraphSearchRequest:
        fence = self._run_fence(spec.scope)
        payload = dict(spec.payload)
        _exact(payload, {"page", "text", "mode", "at_revision"}, set())
        _payload_revision(payload, fence.research_revision)
        limit, cursor = _parse_page(payload["page"])
        return GraphSearchRequest(
            fence.run_id,
            fence.last_cursor,
            fence.research_revision,
            fence.contract_version,
            _mode(payload["mode"]),
            _string(payload["text"], "text"),
            limit,
            cursor,
        )

    def _closure_request(self, spec: QuerySpec) -> ClosureRequest:
        fence = self._run_fence(spec.scope)
        payload = dict(spec.payload)
        _exact(
            payload,
            {"claim_id", "at_revision", "node_limit"},
            {"continuation_cursor"},
        )
        _payload_revision(payload, fence.research_revision)
        return ClosureRequest(
            fence.run_id,
            fence.last_cursor,
            fence.research_revision,
            fence.contract_version,
            _string(payload["claim_id"], "claim_id"),
            _integer(payload["node_limit"], "node_limit"),
            _optional_string(payload.get("continuation_cursor"), "continuation_cursor"),
        )

    def _run_fence(self, scope: JsonObject) -> RunQueryFence:
        run_id = _run_scope(scope)
        fence = self._fences.run(run_id)
        expected_revision = scope.get("at_revision")
        expected_contract = scope.get("at_contract_version")
        if expected_revision is not None and expected_revision != fence.research_revision:
            raise StaleQuery("run revision changed")
        if expected_contract is not None and expected_contract != fence.contract_version:
            raise StaleQuery("run contract version changed")
        return fence

    def _all_runs(self) -> tuple[tuple[RunActions, ...], int]:
        cursor = None
        runs: list[RunActions] = []
        revision: int | None = None
        while True:
            page = self._catalog.list(ResearchListQuery(200, cursor=cursor))
            if revision is None:
                revision = page.catalog_revision
            elif page.catalog_revision != revision:
                raise StaleQuery("research catalog changed during scan")
            runs.extend(
                RunActions(
                    str(item["run_id"]),
                    int(item["research_revision"]),
                    int(item["contract_version"]),
                    tuple(cast(Sequence[dict[str, Any]], item["available_actions"])),
                )
                for item in page.items
            )
            if page.next_cursor is None:
                return tuple(runs), revision or 0
            cursor = page.next_cursor


def _graph_result(result_type: str, value: GraphSlice, last_cursor: int) -> QueryResult:
    return QueryResult(
        result_type,
        f"{result_type.lower()}:{value.query_digest}",
        _frozen(
            {
                "scope_kind": "RUN",
                "run_id": value.run_id,
                "research_revision": value.at_revision,
                "contract_version": value.contract_version,
                "last_cursor": last_cursor,
            }
        ),
        _frozen(_slice_data(value)),
    )


def _search_result(value: GraphSearchPage, last_cursor: int) -> QueryResult:
    return QueryResult(
        "GRAPH_SEARCH",
        f"graph-search:{value.query_digest}",
        _frozen(
            {
                "scope_kind": "RUN",
                "run_id": value.run_id,
                "research_revision": value.at_revision,
                "contract_version": value.contract_version,
                "last_cursor": last_cursor,
            }
        ),
        _frozen(
            {
                "mode": value.mode,
                "items": [
                    {
                        "claim_id": item.claim_id,
                        "stable_label": item.stable_label,
                        "statement": item.statement,
                        "lifecycle": item.lifecycle,
                        "dependable": item.dependable,
                        "route_id": item.route_id,
                    }
                    for item in value.items
                ],
                "page": _page_info(value.returned, value.total, value.next_cursor),
                "query_digest": value.query_digest,
                "boundary_digest": value.boundary_digest,
            }
        ),
    )


def _slice_data(value: GraphSlice) -> dict[str, Any]:
    return {
        "mode": value.mode,
        "nodes": [
            {
                "claim_id": node.claim_id,
                "stable_label": node.stable_label,
                "statement": node.statement,
                "lifecycle": node.lifecycle,
                "dependable": node.dependable,
                "claim_type": node.claim_type,
                "authority_axes": dict(node.authority_axes),
                "contract_version": node.contract_version,
                "verification_method": node.verification_method,
                "route_id": node.route_id,
            }
            for node in value.nodes
        ],
        "edges": [
            {
                "edge_id": edge.edge_id,
                "from_claim_id": edge.from_claim_id,
                "to_claim_id": edge.to_claim_id,
                "logical_direction": edge.logical_direction,
                "obligation_status": edge.obligation_status,
                **({"bridge_spec_id": edge.bridge_spec_id} if edge.bridge_spec_id else {}),
            }
            for edge in value.edges
        ],
        "groups": [
            {
                "group_id": group.group_id,
                "group_kind": group.group_kind,
                "membership_rule": dict(group.membership_rule),
                "total": group.total,
                "status_counts": dict(group.status_counts),
            }
            for group in value.groups
        ],
        "cross_route_boundary": [
            {
                "boundary_id": item.boundary_id,
                "claim_id": item.claim_id,
                "source_route_id": item.source_route_id,
                "direction": item.direction,
                "dependable": item.dependable,
                "folded_count": item.folded_count,
                "path_to_target": list(item.path_to_target),
            }
            for item in value.cross_route_boundary
        ],
        "total_matches": value.total_matches,
        "returned_nodes": value.returned_nodes,
        "returned_edges": value.returned_edges,
        "truncated": value.truncated,
        **({"continuation_cursor": value.continuation_cursor} if value.continuation_cursor else {}),
        "query_digest": value.query_digest,
        "boundary_digest": value.boundary_digest,
    }


def _research_item(item: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": "rk.product.projection.v1",
        "stable_entity_id": str(item["run_id"]),
        "projection_type": "LIST_RESEARCH",
        "status": str(item["outcome_state"]),
        "artifact_ids": [],
        **dict(item),
    }


def _page_info(returned: int, total: int, cursor: str | None) -> dict[str, Any]:
    result: dict[str, Any] = {
        "returned": returned,
        "total": total,
        "truncated": cursor is not None,
    }
    if cursor is not None:
        result["next_cursor"] = cursor
    return result


def _artifact_ids(data: Mapping[str, Any]) -> list[str]:
    refs = data.get("result_refs", [])
    if not isinstance(refs, list):
        return []
    return sorted(
        {
            str(ref["artifact_id"])
            for ref in refs
            if isinstance(ref, dict) and isinstance(ref.get("artifact_id"), str)
        }
    )


def _list_payload(
    payload: JsonObject,
) -> tuple[
    tuple[int, str | None],
    tuple[str, ...],
    tuple[str, ...],
    tuple[str, ...],
    str | None,
    str | None,
]:
    value = dict(payload)
    _exact(value, {"page"}, {"owners", "labels", "outcomes", "text", "sort"})
    return (
        _parse_page(value["page"]),
        _strings(value.get("owners", []), "owners"),
        _strings(value.get("labels", []), "labels"),
        _strings(value.get("outcomes", []), "outcomes"),
        _optional_string(value.get("text"), "text"),
        _optional_string(value.get("sort"), "sort"),
    )


def _page_payload(payload: JsonObject) -> tuple[int, str | None]:
    value = dict(payload)
    _exact(value, {"page"}, set())
    return _parse_page(value["page"])


def _parse_page(value: object) -> tuple[int, str | None]:
    if not isinstance(value, dict):
        raise ValueError("page must be an object")
    _exact(value, {"limit"}, {"cursor"})
    limit = _integer(value["limit"], "limit")
    if not 1 <= limit <= 200:
        raise ValueError("page limit must be between 1 and 200")
    return limit, _optional_string(value.get("cursor"), "cursor")


def _filters(value: object, mode: str) -> GraphFilters:
    if not isinstance(value, dict):
        raise ValueError("filters must be an object")
    allowed = {"claim_types", "route_ids", "verification_methods"}
    if mode == "RESEARCH_HISTORY":
        allowed.add("lifecycles")
    _exact(value, set(), allowed)
    return GraphFilters(
        _strings(value.get("claim_types", []), "claim_types"),
        _strings(value.get("route_ids", []), "route_ids"),
        _strings(value.get("verification_methods", []), "verification_methods"),
        _strings(value.get("lifecycles", []), "lifecycles"),
    )


def _run_scope(scope: JsonObject) -> str:
    allowed = {"kind", "run_id", "at_revision", "at_contract_version"}
    if scope.get("kind") != "RUN" or not {"kind", "run_id"} <= set(scope) or set(scope) - allowed:
        raise ValueError("query requires an exact RUN scope")
    return _scope_string(scope, "run_id")


def _global_scope(scope: JsonObject) -> str:
    allowed = {"kind", "deployment_id", "at_catalog_revision"}
    if (
        scope.get("kind") != "GLOBAL"
        or not {"kind", "deployment_id"} <= set(scope)
        or set(scope) - allowed
    ):
        raise ValueError("query requires an exact GLOBAL scope")
    return _scope_string(scope, "deployment_id")


def _scope_string(scope: JsonObject, name: str) -> str:
    return _string(scope.get(name), name)


def _expected_catalog_revision(scope: JsonObject, current: int) -> None:
    expected = scope.get("at_catalog_revision")
    if expected is not None and expected != current:
        raise StaleQuery("catalog revision changed")


def _payload_revision(payload: Mapping[str, Any], current: int) -> None:
    revision = _integer(payload["at_revision"], "at_revision")
    if revision != current:
        raise StaleQuery("graph query revision changed")


def _mode(value: object) -> Literal["VERIFIED", "RESEARCH_HISTORY"]:
    if value not in ("VERIFIED", "RESEARCH_HISTORY"):
        raise ValueError("graph mode is invalid")
    return value


def _direction(value: object) -> Literal["PREDECESSORS", "SUCCESSORS", "BOTH"]:
    if value not in ("PREDECESSORS", "SUCCESSORS", "BOTH"):
        raise ValueError("graph direction is invalid")
    return value


def _strings(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) and not isinstance(value, tuple):
        raise ValueError(f"{label} must be an array")
    result = tuple(_string(item, label) for item in value)
    if len(result) != len(set(result)):
        raise ValueError(f"{label} must be unique")
    return result


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise ValueError(f"{label} must be a non-empty trimmed string")
    return value


def _optional_string(value: object, label: str) -> str | None:
    return None if value is None else _string(value, label)


def _integer(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError(f"{label} must be a non-negative integer")
    return value


def _exact(value: Mapping[str, Any], required: set[str], optional: set[str]) -> None:
    if required - value.keys() or value.keys() - required - optional:
        raise ValueError("query payload fields are invalid")


def _canonical(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode()


def _digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical(value)).hexdigest()


def _json_object(value: bytes) -> dict[str, Any]:
    try:
        result = json.loads(value)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise QueryCursorInvalid("query cursor JSON is invalid") from error
    if not isinstance(result, dict):
        raise QueryCursorInvalid("query cursor JSON is not an object")
    return cast(dict[str, Any], result)


def _frozen(value: Mapping[str, Any]) -> JsonObject:
    return cast(JsonObject, MappingProxyType(dict(value)))


__all__ = [
    "CatalogQueryFence",
    "ProductQueryService",
    "ProductQueryServiceError",
    "QueryAuthenticationError",
    "QueryCursorInvalid",
    "QueryObjectNotFound",
    "QueryScopeDenied",
    "QueryVariantUnavailable",
    "RunQueryFence",
    "SQLiteQueryFenceSource",
    "SessionQueryAuthorizer",
]
