"""Revision-fenced graph search, slices, and closures over the B06a projection."""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import sqlite3
from collections import Counter
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from statistics import quantiles
from time import perf_counter
from typing import Any, Literal, cast

from rk.product.graph_index import (
    GraphIndex,
    GraphIndexWatermark,
    IndexedGraphEdge,
    IndexedGraphNode,
    ProjectionLag,
)
from rk.sqlite import open_sqlite

GraphMode = Literal["VERIFIED", "RESEARCH_HISTORY"]
SliceDirection = Literal["PREDECESSORS", "SUCCESSORS", "BOTH"]
LagPolicy = Literal["REJECT", "CATCH_UP"]


class GraphQueryError(RuntimeError):
    code = "GRAPH_QUERY_INVALID"
    http_status = 400


class GraphSeedNotFound(GraphQueryError):
    code = "GRAPH_SEED_NOT_FOUND"
    http_status = 404


class InvalidQueryCursor(GraphQueryError):
    code = "INVALID_QUERY_CURSOR"


class StaleQuery(GraphQueryError):
    code = "STALE_QUERY"
    http_status = 409


@dataclass(frozen=True, slots=True)
class GraphFilters:
    claim_types: tuple[str, ...] = ()
    route_ids: tuple[str, ...] = ()
    verification_methods: tuple[str, ...] = ()
    lifecycles: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class GraphSliceRequest:
    run_id: str
    at_cursor: int
    at_revision: int
    contract_version: int
    mode: GraphMode
    seed_ids: tuple[str, ...]
    direction: SliceDirection
    depth: int
    filters: GraphFilters
    node_limit: int
    continuation_cursor: str | None = None
    lag_policy: LagPolicy = "REJECT"


@dataclass(frozen=True, slots=True)
class GraphSearchRequest:
    run_id: str
    at_cursor: int
    at_revision: int
    contract_version: int
    mode: GraphMode
    text: str
    page_limit: int
    continuation_cursor: str | None = None
    lag_policy: LagPolicy = "REJECT"


@dataclass(frozen=True, slots=True)
class ClosureRequest:
    run_id: str
    at_cursor: int
    at_revision: int
    contract_version: int
    claim_id: str
    node_limit: int
    continuation_cursor: str | None = None
    lag_policy: LagPolicy = "REJECT"


@dataclass(frozen=True, slots=True)
class GraphGroup:
    group_id: str
    group_kind: Literal["ROUTE"]
    membership_rule: Mapping[str, str]
    total: int
    status_counts: Mapping[str, int]


@dataclass(frozen=True, slots=True)
class CrossRouteBoundary:
    boundary_id: str
    claim_id: str
    source_route_id: str
    direction: Literal["PREDECESSOR", "SUCCESSOR"]
    dependable: bool
    folded_count: int
    path_to_target: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class GraphSlice:
    schema_version: str
    run_id: str
    at_revision: int
    contract_version: int
    mode: GraphMode
    nodes: tuple[IndexedGraphNode, ...]
    edges: tuple[IndexedGraphEdge, ...]
    groups: tuple[GraphGroup, ...]
    cross_route_boundary: tuple[CrossRouteBoundary, ...]
    total_matches: int
    returned_nodes: int
    returned_edges: int
    truncated: bool
    continuation_cursor: str | None
    query_digest: str
    boundary_digest: str


@dataclass(frozen=True, slots=True)
class GraphSearchHit:
    claim_id: str
    stable_label: str
    statement: str
    lifecycle: str
    dependable: bool
    route_id: str


@dataclass(frozen=True, slots=True)
class GraphSearchPage:
    schema_version: str
    run_id: str
    at_revision: int
    contract_version: int
    mode: GraphMode
    items: tuple[GraphSearchHit, ...]
    returned: int
    total: int
    truncated: bool
    next_cursor: str | None
    query_digest: str
    boundary_digest: str


@dataclass(frozen=True, slots=True)
class GraphBenchmarkRequest:
    run_id: str
    at_cursor: int
    at_revision: int
    contract_version: int
    search_text: str
    seed_id: str
    iterations: int = 5


@dataclass(frozen=True, slots=True)
class GraphBenchmarkReport:
    schema_version: str
    run_id: str
    at_revision: int
    node_count: int
    edge_count: int
    reachable_depth: int
    search_p95_ms: float
    neighborhood_p95_ms: float
    profile_eligible: bool
    acceptance_scope: Literal["IN_PROCESS_BENCHMARK_ONLY"]


@dataclass(frozen=True, slots=True)
class _BoundaryState:
    offset: int
    last_claim_id: str | None


@dataclass(frozen=True, slots=True)
class _Traversal:
    ordered_ids: tuple[str, ...]
    nodes: Mapping[str, IndexedGraphNode]
    boundaries: tuple[tuple[str, CrossRouteBoundary], ...]
    max_depth: int


class _CursorCodec:
    def __init__(self, secret: bytes) -> None:
        if len(secret) < 32:
            raise ValueError("cursor_secret must contain at least 32 bytes")
        self._secret = secret

    def encode(
        self,
        *,
        kind: str,
        run_id: str,
        revision: int,
        activity_cursor: int,
        query_digest: str,
        boundary: _BoundaryState,
    ) -> str:
        body = _canonical_json(
            {
                "v": 1,
                "kind": kind,
                "run_id": run_id,
                "revision": revision,
                "activity_cursor": activity_cursor,
                "query_digest": query_digest,
                "boundary": {
                    "offset": boundary.offset,
                    "last_claim_id": boundary.last_claim_id,
                },
            }
        )
        signature = hmac.digest(self._secret, body, "sha256")
        return "rkq1." + base64.urlsafe_b64encode(body + signature).rstrip(b"=").decode("ascii")

    def decode(
        self,
        token: str,
        *,
        kind: str,
        run_id: str,
        revision: int,
        activity_cursor: int,
        query_digest: str,
    ) -> _BoundaryState:
        if not token.startswith("rkq1."):
            raise InvalidQueryCursor("query cursor has an unknown format")
        encoded = token[5:]
        try:
            raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        except ValueError as error:
            raise InvalidQueryCursor("query cursor is not valid base64url") from error
        if len(raw) <= 32:
            raise InvalidQueryCursor("query cursor is truncated")
        body, signature = raw[:-32], raw[-32:]
        if not hmac.compare_digest(signature, hmac.digest(self._secret, body, "sha256")):
            raise InvalidQueryCursor("query cursor signature is invalid")
        value = _json_mapping(body)
        expected = {
            "v": 1,
            "kind": kind,
            "run_id": run_id,
            "revision": revision,
            "activity_cursor": activity_cursor,
            "query_digest": query_digest,
        }
        if any(value.get(key) != expected_value for key, expected_value in expected.items()):
            raise StaleQuery(
                "query cursor no longer matches run, revision, query, or activity fence"
            )
        boundary = value.get("boundary")
        if not isinstance(boundary, dict) or set(boundary) != {"offset", "last_claim_id"}:
            raise InvalidQueryCursor("query cursor boundary is invalid")
        offset = boundary["offset"]
        last_claim_id = boundary["last_claim_id"]
        if isinstance(offset, bool) or not isinstance(offset, int) or offset < 1:
            raise InvalidQueryCursor("query cursor offset is invalid")
        if not isinstance(last_claim_id, str) or not last_claim_id:
            raise InvalidQueryCursor("query cursor boundary claim is invalid")
        return _BoundaryState(offset, last_claim_id)


class GraphQueryService:
    """Serve bounded graph reads from one exact B06a cursor/revision fence."""

    def __init__(
        self,
        db_path: Path,
        graph_index: GraphIndex,
        *,
        cursor_secret: bytes,
        busy_timeout_ms: int = 5_000,
        timer: Callable[[], float] = perf_counter,
    ) -> None:
        self._db_path = Path(db_path)
        self._graph_index = graph_index
        self._codec = _CursorCodec(cursor_secret)
        self._busy_timeout_ms = busy_timeout_ms
        self._timer = timer

    def slice(self, request: GraphSliceRequest) -> GraphSlice:
        self._validate_slice_request(request)
        digest = _query_digest(
            {
                "kind": "GRAPH_SLICE",
                "mode": request.mode,
                "seed_ids": sorted(request.seed_ids),
                "direction": request.direction,
                "depth": request.depth,
                "filters": _filters_value(request.filters),
                "node_limit": request.node_limit,
            }
        )
        boundary = self._decode_boundary(
            request.continuation_cursor,
            "GRAPH_SLICE",
            request.run_id,
            request.at_revision,
            request.at_cursor,
            digest,
        )
        return self._aligned_read(
            request.run_id,
            request.at_cursor,
            request.at_revision,
            request.lag_policy,
            lambda connection: self._slice_at(connection, request, digest, boundary),
        )

    def search(self, request: GraphSearchRequest) -> GraphSearchPage:
        self._validate_search_request(request)
        digest = _query_digest(
            {
                "kind": "GRAPH_SEARCH",
                "mode": request.mode,
                "text": request.text,
                "page_limit": request.page_limit,
            }
        )
        boundary = self._decode_boundary(
            request.continuation_cursor,
            "GRAPH_SEARCH",
            request.run_id,
            request.at_revision,
            request.at_cursor,
            digest,
        )
        return self._aligned_read(
            request.run_id,
            request.at_cursor,
            request.at_revision,
            request.lag_policy,
            lambda connection: self._search_at(connection, request, digest, boundary),
        )

    def dependency_closure(self, request: ClosureRequest) -> GraphSlice:
        return self.slice(self._closure_slice(request, "PREDECESSORS"))

    def reverse_closure(self, request: ClosureRequest) -> GraphSlice:
        return self.slice(self._closure_slice(request, "SUCCESSORS"))

    def benchmark_profile(self, request: GraphBenchmarkRequest) -> GraphBenchmarkReport:
        _nonempty(request.search_text, "search_text")
        _nonempty(request.seed_id, "seed_id")
        if not 3 <= request.iterations <= 30:
            raise ValueError("benchmark iterations must be between 3 and 30")
        search_times: list[float] = []
        slice_times: list[float] = []
        search_request = GraphSearchRequest(
            request.run_id,
            request.at_cursor,
            request.at_revision,
            request.contract_version,
            "VERIFIED",
            request.search_text,
            200,
        )
        slice_request = GraphSliceRequest(
            request.run_id,
            request.at_cursor,
            request.at_revision,
            request.contract_version,
            "VERIFIED",
            (request.seed_id,),
            "BOTH",
            60,
            GraphFilters(),
            200,
        )
        for _ in range(request.iterations):
            started = self._timer()
            self.search(search_request)
            search_times.append((self._timer() - started) * 1_000)
            started = self._timer()
            self.slice(slice_request)
            slice_times.append((self._timer() - started) * 1_000)
        node_count, edge_count, reachable_depth = self._aligned_read(
            request.run_id,
            request.at_cursor,
            request.at_revision,
            "REJECT",
            lambda connection: self._profile_counts(connection, slice_request),
        )
        return GraphBenchmarkReport(
            "rk.product.graph_benchmark.v1",
            request.run_id,
            request.at_revision,
            node_count,
            edge_count,
            reachable_depth,
            _p95(search_times),
            _p95(slice_times),
            node_count >= 10_000 and edge_count >= 30_000 and reachable_depth >= 60,
            "IN_PROCESS_BENCHMARK_ONLY",
        )

    def _slice_at(
        self,
        connection: sqlite3.Connection,
        request: GraphSliceRequest,
        query_digest: str,
        boundary: _BoundaryState,
    ) -> GraphSlice:
        traversal = self._traverse(connection, request)
        self._verify_boundary(traversal.ordered_ids, boundary)
        start = boundary.offset
        end = min(start + request.node_limit, len(traversal.ordered_ids))
        selected_ids = traversal.ordered_ids[start:end]
        nodes = tuple(traversal.nodes[claim_id] for claim_id in selected_ids)
        edges = self._page_edges(connection, request.run_id, selected_ids)
        boundary_items = tuple(
            item for anchor, item in traversal.boundaries if anchor in set(selected_ids)
        )[:200]
        groups = _groups(nodes)
        page_boundary = _BoundaryState(end, selected_ids[-1] if selected_ids else None)
        truncated = end < len(traversal.ordered_ids)
        next_cursor = (
            self._codec.encode(
                kind="GRAPH_SLICE",
                run_id=request.run_id,
                revision=request.at_revision,
                activity_cursor=request.at_cursor,
                query_digest=query_digest,
                boundary=page_boundary,
            )
            if truncated
            else None
        )
        return GraphSlice(
            "rk.product.graph_slice.v1",
            request.run_id,
            request.at_revision,
            request.contract_version,
            request.mode,
            nodes,
            edges,
            groups,
            boundary_items,
            len(traversal.ordered_ids),
            len(nodes),
            len(edges),
            truncated,
            next_cursor,
            query_digest,
            _boundary_digest(page_boundary),
        )

    def _search_at(
        self,
        connection: sqlite3.Connection,
        request: GraphSearchRequest,
        query_digest: str,
        boundary: _BoundaryState,
    ) -> GraphSearchPage:
        match = _fts_match(request.text)
        dependable = 1 if request.mode == "VERIFIED" else 0
        total_row = connection.execute(
            "SELECT COUNT(*) FROM product_graph_fts f "
            "JOIN product_graph_nodes n ON n.run_id=f.run_id AND n.claim_id=f.claim_id "
            "WHERE product_graph_fts MATCH ? AND f.run_id=? AND n.dependable=?",
            (match, request.run_id, dependable),
        ).fetchone()
        if total_row is None:
            raise GraphQueryError("graph search total is unavailable")
        total = int(total_row[0])
        if boundary.offset > total:
            raise StaleQuery("query cursor boundary no longer exists")
        if boundary.offset:
            previous = connection.execute(
                "SELECT f.claim_id FROM product_graph_fts f "
                "JOIN product_graph_nodes n ON n.run_id=f.run_id AND n.claim_id=f.claim_id "
                "WHERE product_graph_fts MATCH ? AND f.run_id=? AND n.dependable=? "
                "ORDER BY bm25(product_graph_fts),f.claim_id LIMIT 1 OFFSET ?",
                (match, request.run_id, dependable, boundary.offset - 1),
            ).fetchone()
            if previous is None or str(previous[0]) != boundary.last_claim_id:
                raise StaleQuery("query cursor boundary changed")
        rows = connection.execute(
            "SELECT n.claim_id,n.stable_label,n.statement,n.lifecycle,n.dependable,n.route_id "
            "FROM product_graph_fts f "
            "JOIN product_graph_nodes n ON n.run_id=f.run_id AND n.claim_id=f.claim_id "
            "WHERE product_graph_fts MATCH ? AND f.run_id=? AND n.dependable=? "
            "ORDER BY bm25(product_graph_fts),f.claim_id LIMIT ? OFFSET ?",
            (match, request.run_id, dependable, request.page_limit, boundary.offset),
        ).fetchall()
        items = tuple(_search_hit(row) for row in rows)
        end = boundary.offset + len(items)
        page_boundary = _BoundaryState(end, items[-1].claim_id if items else None)
        truncated = end < total
        next_cursor = (
            self._codec.encode(
                kind="GRAPH_SEARCH",
                run_id=request.run_id,
                revision=request.at_revision,
                activity_cursor=request.at_cursor,
                query_digest=query_digest,
                boundary=page_boundary,
            )
            if truncated
            else None
        )
        return GraphSearchPage(
            "rk.product.graph_search.v1",
            request.run_id,
            request.at_revision,
            request.contract_version,
            request.mode,
            items,
            len(items),
            total,
            truncated,
            next_cursor,
            query_digest,
            _boundary_digest(page_boundary),
        )

    def _traverse(self, connection: sqlite3.Connection, request: GraphSliceRequest) -> _Traversal:
        seeds = tuple(sorted(set(request.seed_ids)))
        seed_nodes = self._load_nodes(connection, request.run_id, seeds)
        if set(seed_nodes) != set(seeds):
            raise GraphSeedNotFound("one or more graph seeds are absent")
        if any(not _matches(node, request.mode, request.filters) for node in seed_nodes.values()):
            raise GraphSeedNotFound(
                "one or more graph seeds are outside the requested mode or filters"
            )
        visited = set(seeds)
        ordered = list(seeds)
        nodes: dict[str, IndexedGraphNode] = dict(seed_nodes)
        paths: dict[str, tuple[str, ...]] = {seed: (seed,) for seed in seeds}
        depths: dict[str, int] = {seed: 0 for seed in seeds}
        frontier = seeds
        boundary_hits: dict[tuple[str, str], tuple[str, CrossRouteBoundary]] = {}
        for level in range(1, request.depth + 1):
            if not frontier:
                break
            edges = self._touching_edges(connection, request.run_id, frontier)
            frontier_set = set(frontier)
            transitions: list[tuple[str, str, Literal["PREDECESSOR", "SUCCESSOR"]]] = []
            for edge in edges:
                touching = {edge.from_claim_id, edge.to_claim_id} & frontier_set
                for current in touching:
                    transitions.extend(_edge_transitions(edge, current, request.direction))
            candidate_ids = tuple(sorted({neighbor for _, neighbor, _ in transitions} - visited))
            candidates = self._load_nodes(connection, request.run_id, candidate_ids)
            next_frontier: list[str] = []
            for current, neighbor, relationship in sorted(set(transitions)):
                if neighbor in visited:
                    continue
                node = candidates.get(neighbor)
                if node is None or not _mode_matches(node, request.mode):
                    continue
                if _route_only_mismatch(node, request.filters):
                    if (
                        node.dependable
                        and node.route_id is not None
                        and _other_filters_match(node, request.filters)
                    ):
                        path = (
                            (neighbor, *paths[current])
                            if relationship == "PREDECESSOR"
                            else (*paths[current], neighbor)
                        )
                        key = (neighbor, relationship)
                        existing = boundary_hits.get(key)
                        count = 1 if existing is None else existing[1].folded_count + 1
                        item = CrossRouteBoundary(
                            _query_digest(
                                {
                                    "run_id": request.run_id,
                                    "revision": request.at_revision,
                                    "claim_id": neighbor,
                                    "direction": relationship,
                                    "path": path,
                                }
                            ),
                            neighbor,
                            node.route_id,
                            relationship,
                            node.dependable,
                            count,
                            path,
                        )
                        boundary_hits[key] = (current, item)
                    continue
                if not _other_filters_match(node, request.filters):
                    continue
                visited.add(neighbor)
                ordered.append(neighbor)
                nodes[neighbor] = node
                depths[neighbor] = level
                paths[neighbor] = (
                    (neighbor, *paths[current])
                    if relationship == "PREDECESSOR"
                    else (*paths[current], neighbor)
                )
                next_frontier.append(neighbor)
            frontier = tuple(sorted(set(next_frontier)))
        boundaries = tuple(
            boundary_hits[key] for key in sorted(boundary_hits, key=lambda item: (item[1], item[0]))
        )
        return _Traversal(tuple(ordered), nodes, boundaries, max(depths.values(), default=0))

    def _aligned_read[T](
        self,
        run_id: str,
        cursor: int,
        revision: int,
        lag_policy: LagPolicy,
        reader: Callable[[sqlite3.Connection], T],
    ) -> T:
        _nonempty(run_id, "run_id")
        _validate_fence(cursor, revision)
        if lag_policy not in ("REJECT", "CATCH_UP"):
            raise ValueError("lag_policy must be REJECT or CATCH_UP")
        attempted_catch_up = False
        while True:
            with self._connect() as connection:
                connection.execute("BEGIN")
                watermark = _watermark(connection, run_id)
                if watermark.processed_cursor == cursor and watermark.research_revision == revision:
                    result = reader(connection)
                    connection.commit()
                    return result
                connection.rollback()
            if watermark.processed_cursor > cursor or (
                watermark.processed_cursor == cursor and watermark.research_revision != revision
            ):
                raise StaleQuery(
                    "requested graph revision is no longer present in the current index"
                )
            if lag_policy == "REJECT" or attempted_catch_up:
                raise ProjectionLag(watermark, cursor, revision)
            self._graph_index.catch_up(run_id, target_cursor=cursor, target_revision=revision)
            attempted_catch_up = True

    def _decode_boundary(
        self,
        token: str | None,
        kind: str,
        run_id: str,
        revision: int,
        activity_cursor: int,
        digest: str,
    ) -> _BoundaryState:
        if token is None:
            return _BoundaryState(0, None)
        return self._codec.decode(
            token,
            kind=kind,
            run_id=run_id,
            revision=revision,
            activity_cursor=activity_cursor,
            query_digest=digest,
        )

    @staticmethod
    def _verify_boundary(ordered: Sequence[str], boundary: _BoundaryState) -> None:
        if boundary.offset > len(ordered):
            raise StaleQuery("query cursor boundary no longer exists")
        if boundary.offset == 0:
            if boundary.last_claim_id is not None:
                raise InvalidQueryCursor("initial query boundary cannot have a claim")
            return
        if ordered[boundary.offset - 1] != boundary.last_claim_id:
            raise StaleQuery("query cursor boundary changed")

    @staticmethod
    def _load_nodes(
        connection: sqlite3.Connection, run_id: str, claim_ids: Sequence[str]
    ) -> dict[str, IndexedGraphNode]:
        if not claim_ids:
            return {}
        placeholders = ",".join("?" for _ in claim_ids)
        rows = connection.execute(
            "SELECT claim_id,stable_label,statement,lifecycle,dependable,claim_type,"
            "authority_axes_json,contract_version,verification_method,"
            "source_worker_run_id,route_id "
            f"FROM product_graph_nodes WHERE run_id=? AND claim_id IN ({placeholders})",
            (run_id, *claim_ids),
        ).fetchall()
        nodes = (_indexed_node(row) for row in rows)
        return {node.claim_id: node for node in nodes}

    @staticmethod
    def _touching_edges(
        connection: sqlite3.Connection, run_id: str, claim_ids: Sequence[str]
    ) -> tuple[IndexedGraphEdge, ...]:
        if not claim_ids:
            return ()
        placeholders = ",".join("?" for _ in claim_ids)
        rows = connection.execute(
            "SELECT edge_id,from_claim_id,to_claim_id,logical_direction,"
            "obligation_status,bridge_spec_id FROM product_graph_edges WHERE run_id=? AND "
            f"(from_claim_id IN ({placeholders}) OR to_claim_id IN ({placeholders})) "
            "ORDER BY edge_id",
            (run_id, *claim_ids, *claim_ids),
        ).fetchall()
        return tuple(_indexed_edge(row) for row in rows)

    @staticmethod
    def _page_edges(
        connection: sqlite3.Connection, run_id: str, claim_ids: Sequence[str]
    ) -> tuple[IndexedGraphEdge, ...]:
        if not claim_ids:
            return ()
        placeholders = ",".join("?" for _ in claim_ids)
        rows = connection.execute(
            "SELECT edge_id,from_claim_id,to_claim_id,logical_direction,"
            "obligation_status,bridge_spec_id FROM product_graph_edges WHERE run_id=? "
            f"AND from_claim_id IN ({placeholders}) AND to_claim_id IN ({placeholders}) "
            "ORDER BY edge_id",
            (run_id, *claim_ids, *claim_ids),
        ).fetchall()
        return tuple(_indexed_edge(row) for row in rows)

    def _profile_counts(
        self, connection: sqlite3.Connection, slice_request: GraphSliceRequest
    ) -> tuple[int, int, int]:
        node_row = connection.execute(
            "SELECT COUNT(*) FROM product_graph_nodes WHERE run_id=? AND dependable=1",
            (slice_request.run_id,),
        ).fetchone()
        edge_row = connection.execute(
            "SELECT COUNT(*) FROM product_graph_edges e "
            "JOIN product_graph_nodes f ON f.run_id=e.run_id AND f.claim_id=e.from_claim_id "
            "JOIN product_graph_nodes t ON t.run_id=e.run_id AND t.claim_id=e.to_claim_id "
            "WHERE e.run_id=? AND f.dependable=1 AND t.dependable=1",
            (slice_request.run_id,),
        ).fetchone()
        if node_row is None or edge_row is None:
            raise GraphQueryError("benchmark graph counts are unavailable")
        traversal = self._traverse(connection, slice_request)
        return int(node_row[0]), int(edge_row[0]), traversal.max_depth

    @staticmethod
    def _closure_slice(request: ClosureRequest, direction: SliceDirection) -> GraphSliceRequest:
        return GraphSliceRequest(
            request.run_id,
            request.at_cursor,
            request.at_revision,
            request.contract_version,
            "VERIFIED",
            (request.claim_id,),
            direction,
            60,
            GraphFilters(),
            request.node_limit,
            request.continuation_cursor,
            request.lag_policy,
        )

    @staticmethod
    def _validate_slice_request(request: GraphSliceRequest) -> None:
        _base_request(
            request.run_id, request.at_cursor, request.at_revision, request.contract_version
        )
        if request.mode not in ("VERIFIED", "RESEARCH_HISTORY"):
            raise ValueError("unknown graph mode")
        if not request.seed_ids or len(request.seed_ids) != len(set(request.seed_ids)):
            raise ValueError("seed_ids must be non-empty and unique")
        if any(not value or value != value.strip() for value in request.seed_ids):
            raise ValueError("seed_ids must contain trimmed identifiers")
        if request.direction not in ("PREDECESSORS", "SUCCESSORS", "BOTH"):
            raise ValueError("unknown graph direction")
        if not 0 <= request.depth <= 60:
            raise ValueError("depth must be between 0 and 60")
        if not 1 <= request.node_limit <= 200:
            raise ValueError("node_limit must be between 1 and 200")
        if request.mode == "VERIFIED" and request.filters.lifecycles:
            raise ValueError("VERIFIED graph does not accept lifecycle filters")
        _validate_filters(request.filters)

    @staticmethod
    def _validate_search_request(request: GraphSearchRequest) -> None:
        _base_request(
            request.run_id, request.at_cursor, request.at_revision, request.contract_version
        )
        if request.mode not in ("VERIFIED", "RESEARCH_HISTORY"):
            raise ValueError("unknown graph mode")
        _nonempty(request.text, "text")
        if not 1 <= request.page_limit <= 200:
            raise ValueError("page_limit must be between 1 and 200")

    def _connect(self) -> sqlite3.Connection:
        connection = open_sqlite(self._db_path, timeout=self._busy_timeout_ms / 1_000)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
        return connection


def _edge_transitions(
    edge: IndexedGraphEdge, current: str, direction: SliceDirection
) -> tuple[tuple[str, str, Literal["PREDECESSOR", "SUCCESSOR"]], ...]:
    arcs: list[tuple[str, str]] = []
    if edge.logical_direction in ("FORWARD", "BIDIRECTIONAL"):
        arcs.append((edge.from_claim_id, edge.to_claim_id))
    if edge.logical_direction in ("REVERSE", "BIDIRECTIONAL"):
        arcs.append((edge.to_claim_id, edge.from_claim_id))
    result: list[tuple[str, str, Literal["PREDECESSOR", "SUCCESSOR"]]] = []
    for source, target in arcs:
        if direction in ("SUCCESSORS", "BOTH") and source == current:
            result.append((current, target, "SUCCESSOR"))
        if direction in ("PREDECESSORS", "BOTH") and target == current:
            result.append((current, source, "PREDECESSOR"))
    return tuple(result)


def _indexed_node(row: tuple[object, ...]) -> IndexedGraphNode:
    axes = json.loads(str(row[6]))
    if not isinstance(axes, dict) or not axes:
        raise GraphQueryError("indexed authority axes are invalid")
    route_id = str(row[10]) if row[10] is not None else None
    if route_id is None:
        raise GraphQueryError("graph node has no route_id")
    return IndexedGraphNode(
        str(row[0]),
        str(row[1]),
        str(row[2]),
        str(row[3]),
        bool(row[4]),
        str(row[5]),
        cast(dict[str, Any], axes),
        int(str(row[7])),
        str(row[8]),
        str(row[9]) if row[9] is not None else None,
        route_id,
    )


def _indexed_edge(row: tuple[object, ...]) -> IndexedGraphEdge:
    direction = str(row[3])
    if direction not in ("FORWARD", "REVERSE", "BIDIRECTIONAL"):
        raise GraphQueryError("indexed edge direction is invalid")
    return IndexedGraphEdge(
        str(row[0]),
        str(row[1]),
        str(row[2]),
        cast(Literal["FORWARD", "REVERSE", "BIDIRECTIONAL"], direction),
        str(row[4]),
        str(row[5]) if row[5] is not None else None,
    )


def _search_hit(row: tuple[object, ...]) -> GraphSearchHit:
    if row[5] is None:
        raise GraphQueryError("graph search node has no route_id")
    return GraphSearchHit(
        str(row[0]), str(row[1]), str(row[2]), str(row[3]), bool(row[4]), str(row[5])
    )


def _mode_matches(node: IndexedGraphNode, mode: GraphMode) -> bool:
    return node.dependable if mode == "VERIFIED" else not node.dependable


def _matches(node: IndexedGraphNode, mode: GraphMode, filters: GraphFilters) -> bool:
    return (
        _mode_matches(node, mode)
        and not _route_only_mismatch(node, filters)
        and _other_filters_match(node, filters)
    )


def _route_only_mismatch(node: IndexedGraphNode, filters: GraphFilters) -> bool:
    return bool(filters.route_ids) and node.route_id not in filters.route_ids


def _other_filters_match(node: IndexedGraphNode, filters: GraphFilters) -> bool:
    return (
        (not filters.claim_types or node.claim_type in filters.claim_types)
        and (
            not filters.verification_methods
            or node.verification_method in filters.verification_methods
        )
        and (not filters.lifecycles or node.lifecycle in filters.lifecycles)
    )


def _groups(nodes: Sequence[IndexedGraphNode]) -> tuple[GraphGroup, ...]:
    by_route: dict[str, list[IndexedGraphNode]] = {}
    for node in nodes:
        if node.route_id is None:
            raise GraphQueryError("graph node has no route_id")
        by_route.setdefault(node.route_id, []).append(node)
    return tuple(
        GraphGroup(
            f"route:{route_id}",
            "ROUTE",
            {"route_id": route_id},
            len(route_nodes),
            dict(sorted(Counter(node.lifecycle for node in route_nodes).items())),
        )
        for route_id, route_nodes in sorted(by_route.items())
    )


def _watermark(connection: sqlite3.Connection, run_id: str) -> GraphIndexWatermark:
    row = connection.execute(
        "SELECT processed_cursor,research_revision,rebuilt_at "
        "FROM product_graph_index_watermarks WHERE run_id=?",
        (run_id,),
    ).fetchone()
    return (
        GraphIndexWatermark(run_id, 0, 0, None)
        if row is None
        else GraphIndexWatermark(run_id, int(row[0]), int(row[1]), str(row[2]))
    )


def _filters_value(filters: GraphFilters) -> Mapping[str, Sequence[str]]:
    return {
        "claim_types": sorted(filters.claim_types),
        "route_ids": sorted(filters.route_ids),
        "verification_methods": sorted(filters.verification_methods),
        "lifecycles": sorted(filters.lifecycles),
    }


def _validate_filters(filters: GraphFilters) -> None:
    for label, values in _filters_value(filters).items():
        if len(values) != len(set(values)) or any(
            not value or value != value.strip() for value in values
        ):
            raise ValueError(f"{label} must contain unique trimmed values")


def _base_request(run_id: str, cursor: int, revision: int, contract_version: int) -> None:
    _nonempty(run_id, "run_id")
    _validate_fence(cursor, revision)
    if isinstance(contract_version, bool) or contract_version < 1:
        raise ValueError("contract_version must be positive")


def _validate_fence(cursor: int, revision: int) -> None:
    if isinstance(cursor, bool) or cursor < 0:
        raise ValueError("at_cursor must be non-negative")
    if isinstance(revision, bool) or revision < 0:
        raise ValueError("at_revision must be non-negative")


def _fts_match(text: str) -> str:
    terms = re.findall(r"[^\W_]+", text, re.UNICODE)
    if not terms:
        raise ValueError("text must contain searchable Unicode terms")
    return " AND ".join(f'"{term}"*' for term in terms)


def _query_digest(value: Mapping[str, Any]) -> str:
    return hashlib.sha256(_canonical_json(value)).hexdigest()


def _boundary_digest(boundary: _BoundaryState) -> str:
    return _query_digest({"offset": boundary.offset, "last_claim_id": boundary.last_claim_id})


def _canonical_json(value: Mapping[str, Any]) -> bytes:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode(
        "utf-8"
    )


def _json_mapping(value: bytes) -> dict[str, Any]:
    try:
        result = json.loads(value)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise InvalidQueryCursor("query cursor payload is invalid") from error
    if not isinstance(result, dict):
        raise InvalidQueryCursor("query cursor payload is not an object")
    return cast(dict[str, Any], result)


def _nonempty(value: str, label: str) -> None:
    if not value or value != value.strip():
        raise ValueError(f"{label} must be a non-empty trimmed string")


def _p95(values: Sequence[float]) -> float:
    return round(quantiles(values, n=20, method="inclusive")[18], 3)


__all__ = [
    "ClosureRequest",
    "CrossRouteBoundary",
    "GraphBenchmarkReport",
    "GraphBenchmarkRequest",
    "GraphFilters",
    "GraphGroup",
    "GraphQueryError",
    "GraphQueryService",
    "GraphSearchHit",
    "GraphSearchPage",
    "GraphSearchRequest",
    "GraphSeedNotFound",
    "GraphSlice",
    "GraphSliceRequest",
    "InvalidQueryCursor",
    "StaleQuery",
]
