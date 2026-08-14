from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from rk.extensions import ProductActivity
from rk.product.activity_store import ActivityStore
from rk.product.graph_index import (
    GraphAuthoritySnapshot,
    GraphIndex,
    IndexedGraphEdge,
    IndexedGraphNode,
)
from rk.product.graph_query import (
    ClosureRequest,
    GraphBenchmarkRequest,
    GraphFilters,
    GraphQueryService,
    GraphSearchRequest,
    GraphSliceRequest,
    InvalidQueryCursor,
    StaleQuery,
)
from rk.product_migrations import ProductMigrationAssembler, ProductMigrationRegistry


def _node(
    claim_id: str,
    route_id: str,
    dependable: bool,
    statement: str,
    *,
    lifecycle: str | None = None,
) -> IndexedGraphNode:
    return IndexedGraphNode(
        claim_id,
        claim_id.upper(),
        statement,
        lifecycle or ("VERIFIED" if dependable else "REJECTED"),
        dependable,
        "LEMMA",
        {"mathematical": "KERNEL"},
        1,
        "LEAN",
        route_id=route_id,
    )


def _edge(edge_id: str, from_id: str, to_id: str) -> IndexedGraphEdge:
    return IndexedGraphEdge(edge_id, from_id, to_id, "FORWARD", "DISCHARGED")


def _database(tmp_path: Path) -> tuple[Path, GraphIndex, int]:
    path = tmp_path / "product.sqlite"
    with sqlite3.connect(path, isolation_level=None) as connection:
        ProductMigrationAssembler(ProductMigrationRegistry(Path("schema_fragments"))).apply(
            connection
        )
    cursor = ActivityStore(path).append(
        ProductActivity(
            event_id="snapshot-fence",
            scope_kind="RUN",
            run_id="run-1",
            source="KERNEL",
            research_revision=1,
            kernel_event_id="kernel-snapshot-fence",
            entity_refs={"run_id": "run-1"},
            payload={"type": "SNAPSHOT_FENCE"},
            recorded_at="2026-08-13T00:00:00Z",
        )
    )
    index = GraphIndex(path, clock=lambda: "2026-08-13T00:01:00Z")
    index.rebuild(
        GraphAuthoritySnapshot(
            "run-1",
            cursor,
            1,
            "kernel-authority-1",
            "2026-08-13T00:00:01Z",
            (
                _node("a", "route-1", True, "shared verified seed"),
                _node("p", "route-1", True, "shared verified predecessor"),
                _node("x", "route-2", True, "remote load bearing fact"),
                _node("s", "route-1", True, "shared verified successor"),
                _node("h1", "route-1", False, "shared rejected attempt"),
                _node("h2", "route-1", False, "shared abandoned branch"),
            ),
            (
                _edge("e-xp", "x", "p"),
                _edge("e-pa", "p", "a"),
                _edge("e-as", "a", "s"),
                _edge("e-h", "h1", "h2"),
            ),
        )
    )
    return path, index, cursor


def _service(path: Path, index: GraphIndex) -> GraphQueryService:
    return GraphQueryService(path, index, cursor_secret=b"g" * 32)


def _slice(cursor: int, **changes: object) -> GraphSliceRequest:
    values: dict[str, object] = {
        "run_id": "run-1",
        "at_cursor": cursor,
        "at_revision": 1,
        "contract_version": 1,
        "mode": "VERIFIED",
        "seed_ids": ("a",),
        "direction": "BOTH",
        "depth": 3,
        "filters": GraphFilters(),
        "node_limit": 2,
    }
    values.update(changes)
    return GraphSliceRequest(**values)  # type: ignore[arg-type]


def test_verified_and_research_history_are_disjoint_for_search_and_slice(
    tmp_path: Path,
) -> None:
    path, index, cursor = _database(tmp_path)
    service = _service(path, index)

    verified = service.search(GraphSearchRequest("run-1", cursor, 1, 1, "VERIFIED", "shared", 20))
    history = service.search(
        GraphSearchRequest("run-1", cursor, 1, 1, "RESEARCH_HISTORY", "shared", 20)
    )
    assert {item.claim_id for item in verified.items} == {"a", "p", "s"}
    assert {item.claim_id for item in history.items} == {"h1", "h2"}
    assert all(item.dependable for item in verified.items)
    assert all(not item.dependable for item in history.items)

    historical_slice = service.slice(
        _slice(
            cursor,
            mode="RESEARCH_HISTORY",
            seed_ids=("h1",),
            direction="SUCCESSORS",
            node_limit=20,
        )
    )
    assert [node.claim_id for node in historical_slice.nodes] == ["h1", "h2"]
    assert all(not node.dependable for node in historical_slice.nodes)


def test_slice_paging_is_stable_and_cursor_is_bound_to_query_and_revision(
    tmp_path: Path,
) -> None:
    path, index, cursor = _database(tmp_path)
    service = _service(path, index)

    first = service.slice(_slice(cursor))
    assert len(first.nodes) == 2
    assert first.total_matches == 4
    assert first.truncated is True
    assert first.continuation_cursor is not None
    assert first.continuation_cursor.startswith("rkq1.")
    second = service.slice(_slice(cursor, continuation_cursor=first.continuation_cursor))
    assert {node.claim_id for node in first.nodes}.isdisjoint(
        node.claim_id for node in second.nodes
    )
    assert first.returned_nodes + second.returned_nodes == 4

    with pytest.raises(StaleQuery):
        service.slice(
            _slice(
                cursor,
                direction="PREDECESSORS",
                continuation_cursor=first.continuation_cursor,
            )
        )
    tampered = first.continuation_cursor[:-1] + (
        "A" if first.continuation_cursor[-1] != "A" else "B"
    )
    with pytest.raises(InvalidQueryCursor):
        service.slice(_slice(cursor, continuation_cursor=tampered))


def test_graph_search_cursor_pages_without_duplicates_and_binds_text(tmp_path: Path) -> None:
    path, index, cursor = _database(tmp_path)
    service = _service(path, index)
    request = GraphSearchRequest("run-1", cursor, 1, 1, "VERIFIED", "shared", 1)

    first = service.search(request)
    assert first.returned == 1
    assert first.total == 3
    assert first.next_cursor is not None
    second = service.search(
        GraphSearchRequest("run-1", cursor, 1, 1, "VERIFIED", "shared", 1, first.next_cursor)
    )
    assert first.items[0].claim_id != second.items[0].claim_id
    with pytest.raises(StaleQuery):
        service.search(
            GraphSearchRequest("run-1", cursor, 1, 1, "VERIFIED", "verified", 1, first.next_cursor)
        )


def test_revision_change_returns_stale_query_instead_of_rebasing_cursor(
    tmp_path: Path,
) -> None:
    path, index, cursor = _database(tmp_path)
    service = _service(path, index)
    first = service.slice(_slice(cursor, node_limit=1))
    assert first.continuation_cursor is not None
    next_cursor = ActivityStore(path).append(
        ProductActivity(
            event_id="revision-2-fence",
            scope_kind="RUN",
            run_id="run-1",
            source="KERNEL",
            research_revision=2,
            kernel_event_id="kernel-revision-2",
            entity_refs={"run_id": "run-1"},
            payload={"type": "REVISION_ADVANCED"},
            recorded_at="2026-08-13T00:02:00Z",
        )
    )
    index.catch_up("run-1", target_cursor=next_cursor, target_revision=2)

    with pytest.raises(StaleQuery, match="no longer present"):
        service.slice(
            _slice(
                cursor,
                node_limit=1,
                continuation_cursor=first.continuation_cursor,
            )
        )
    with pytest.raises(StaleQuery, match="no longer matches"):
        service.slice(
            _slice(
                next_cursor,
                at_revision=2,
                node_limit=1,
                continuation_cursor=first.continuation_cursor,
            )
        )


def test_dependency_and_reverse_closure_follow_logical_edges(tmp_path: Path) -> None:
    path, index, cursor = _database(tmp_path)
    service = _service(path, index)
    request = ClosureRequest("run-1", cursor, 1, 1, "a", 200)

    dependencies = service.dependency_closure(request)
    reverse = service.reverse_closure(request)

    assert [node.claim_id for node in dependencies.nodes] == ["a", "p", "x"]
    assert [edge.edge_id for edge in dependencies.edges] == ["e-pa", "e-xp"]
    assert [node.claim_id for node in reverse.nodes] == ["a", "s"]
    assert [edge.edge_id for edge in reverse.edges] == ["e-as"]


def test_cross_route_load_bearing_predecessor_is_folded_with_path(tmp_path: Path) -> None:
    path, index, cursor = _database(tmp_path)
    result = _service(path, index).slice(
        _slice(
            cursor,
            direction="PREDECESSORS",
            filters=GraphFilters(route_ids=("route-1",)),
            node_limit=200,
        )
    )

    assert [node.claim_id for node in result.nodes] == ["a", "p"]
    assert len(result.cross_route_boundary) == 1
    boundary = result.cross_route_boundary[0]
    assert boundary.claim_id == "x"
    assert boundary.source_route_id == "route-2"
    assert boundary.direction == "PREDECESSOR"
    assert boundary.dependable is True
    assert boundary.path_to_target == ("x", "p", "a")


def test_node_limit_mode_filters_and_benchmark_claim_are_strict(tmp_path: Path) -> None:
    path, index, cursor = _database(tmp_path)
    service = _service(path, index)

    with pytest.raises(ValueError, match="node_limit"):
        service.slice(_slice(cursor, node_limit=201))
    with pytest.raises(ValueError, match="lifecycle"):
        service.slice(_slice(cursor, filters=GraphFilters(lifecycles=("VERIFIED",))))

    report = service.benchmark_profile(
        GraphBenchmarkRequest("run-1", cursor, 1, 1, "shared", "a", iterations=3)
    )
    assert report.node_count == 4
    assert report.edge_count == 3
    assert report.reachable_depth == 2
    assert report.profile_eligible is False
    assert report.acceptance_scope == "IN_PROCESS_BENCHMARK_ONLY"
