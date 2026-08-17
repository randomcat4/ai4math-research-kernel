from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any

import pytest

from rk.extensions import ProductActivity
from rk.product.activity_store import ActivityStore
from rk.product.graph_index import (
    AdjacencyHit,
    GraphAuthoritySnapshot,
    GraphIndex,
    GraphIndexEventError,
    GraphIndexFenceError,
    IndexedGraphNode,
    ProjectionLag,
)
from rk.product_migrations import ProductMigrationAssembler, ProductMigrationRegistry


def _database(tmp_path: Path) -> Path:
    path = tmp_path / "product.sqlite"
    with sqlite3.connect(path, isolation_level=None) as connection:
        ProductMigrationAssembler(ProductMigrationRegistry(Path("schema_fragments"))).apply(
            connection
        )
    return path


def _index(path: Path) -> GraphIndex:
    return GraphIndex(path, clock=lambda: "2026-08-13T00:01:00Z")


def _node(claim_id: str, statement: str, *, label: str | None = None) -> dict[str, Any]:
    return {
        "claim_id": claim_id,
        "stable_label": label or claim_id.upper(),
        "statement": statement,
        "lifecycle": "VERIFIED",
        "dependable": True,
        "claim_type": "LEMMA",
        "authority_axes": {"mathematical": "KERNEL"},
        "contract_version": 1,
        "verification_method": "LEAN",
        "route_id": "route-1",
    }


def _edge(
    edge_id: str,
    from_id: str,
    to_id: str,
    direction: str = "FORWARD",
) -> dict[str, Any]:
    return {
        "edge_id": edge_id,
        "from_claim_id": from_id,
        "to_claim_id": to_id,
        "logical_direction": direction,
        "obligation_status": "DISCHARGED",
    }


def _delta(
    event_id: str,
    *,
    revision: int,
    nodes: list[dict[str, Any]] | None = None,
    edges: list[dict[str, Any]] | None = None,
    delete_nodes: list[str] | None = None,
    delete_edges: list[str] | None = None,
    source: str = "KERNEL",
    authority_effect: str = "NO_FACT_GRAPH_WRITE",
) -> ProductActivity:
    return ProductActivity(
        event_id=event_id,
        scope_kind="RUN",
        run_id="run-1",
        source=source,
        research_revision=revision,
        kernel_event_id=f"kernel-{event_id}" if source == "KERNEL" else None,
        entity_refs={"run_id": "run-1"},
        payload={
            "type": "GRAPH_PROJECTION_DELTA",
            "schema_version": "rk.product.graph_projection_delta.v1",
            "authority_effect": authority_effect,
            "upsert_nodes": nodes or [],
            "delete_node_ids": delete_nodes or [],
            "upsert_edges": edges or [],
            "delete_edge_ids": delete_edges or [],
        },
        recorded_at="2026-08-13T00:00:00Z",
    )


def test_lag_is_explicit_or_synchronously_caught_up_without_partial_results(
    tmp_path: Path,
) -> None:
    path = _database(tmp_path)
    cursor = ActivityStore(path).append(
        _delta(
            "event-1",
            revision=7,
            nodes=[_node("a", "compactness argument"), _node("b", "spectral conclusion")],
            edges=[_edge("e1", "a", "b")],
        )
    )
    index = _index(path)

    with pytest.raises(ProjectionLag) as captured:
        index.search_claim_ids("run-1", "spectral", target_cursor=cursor, target_revision=7)
    assert captured.value.code == "PROJECTION_LAG"
    assert captured.value.http_status == 409
    assert captured.value.current.processed_cursor == 0

    assert index.search_claim_ids(
        "run-1",
        "spectral",
        target_cursor=cursor,
        target_revision=7,
        lag_policy="CATCH_UP",
    ) == ("b",)
    assert index.adjacent("run-1", "a", "SUCCESSOR", target_cursor=cursor, target_revision=7) == (
        AdjacencyHit("e1", "b"),
    )


def test_global_cursor_gaps_and_old_revision_activity_are_delivered_in_cursor_order(
    tmp_path: Path,
) -> None:
    path = _database(tmp_path)
    store = ActivityStore(path)
    first = store.append(
        _delta(
            "event-1",
            revision=8,
            nodes=[_node("a", "first theorem"), _node("b", "second theorem")],
            edges=[_edge("e1", "a", "b")],
        )
    )
    store.append(
        ProductActivity(
            event_id="other-run",
            scope_kind="RUN",
            run_id="run-2",
            source="WORKER",
            research_revision=99,
            entity_refs={},
            payload={"type": "WORK_FINISHED"},
            recorded_at="2026-08-13T00:00:01Z",
        )
    )
    last = store.append(_delta("event-3", revision=2, delete_nodes=["a"]))
    assert (first, last) == (1, 3)

    index = _index(path)
    watermark = index.catch_up("run-1", target_cursor=last, target_revision=8)

    assert watermark.processed_cursor == 3
    assert watermark.research_revision == 8
    assert index.search_claim_ids("run-1", "theorem", target_cursor=last, target_revision=8) == (
        "b",
    )
    assert index.adjacent("run-1", "b", "PREDECESSOR", target_cursor=last, target_revision=8) == ()


def test_incremental_delete_and_authority_rebuild_produce_the_same_projection(
    tmp_path: Path,
) -> None:
    path = _database(tmp_path)
    store = ActivityStore(path)
    store.append(
        _delta(
            "event-1",
            revision=4,
            nodes=[_node("a", "alpha removable"), _node("b", "beta retained")],
            edges=[_edge("e1", "a", "b")],
        )
    )
    cursor = store.append(_delta("event-2", revision=4, delete_nodes=["a"]))
    index = _index(path)
    index.catch_up("run-1", target_cursor=cursor, target_revision=4)
    incremental_search = index.search_claim_ids(
        "run-1", "retained", target_cursor=cursor, target_revision=4
    )
    incremental_neighbors = index.adjacent(
        "run-1", "b", "PREDECESSOR", target_cursor=cursor, target_revision=4
    )

    snapshot = GraphAuthoritySnapshot(
        run_id="run-1",
        processed_cursor=cursor,
        research_revision=4,
        source_kernel_event_id="kernel-snapshot-4",
        captured_at="2026-08-13T00:02:00Z",
        nodes=(
            IndexedGraphNode(
                claim_id="b",
                stable_label="B",
                statement="beta retained",
                lifecycle="VERIFIED",
                dependable=True,
                claim_type="LEMMA",
                authority_axes={"mathematical": "KERNEL"},
                contract_version=1,
                verification_method="LEAN",
                route_id="route-1",
            ),
        ),
        edges=(),
    )
    index.rebuild(snapshot)

    assert (
        index.search_claim_ids("run-1", "retained", target_cursor=cursor, target_revision=4)
        == incremental_search
    )
    assert (
        index.adjacent("run-1", "b", "PREDECESSOR", target_cursor=cursor, target_revision=4)
        == incremental_neighbors
    )
    assert (
        index.search_claim_ids("run-1", "removable", target_cursor=cursor, target_revision=4) == ()
    )


def test_reverse_and_bidirectional_edges_obey_logical_direction(tmp_path: Path) -> None:
    path = _database(tmp_path)
    cursor = ActivityStore(path).append(
        _delta(
            "event-1",
            revision=1,
            nodes=[_node("a", "node a"), _node("b", "node b"), _node("c", "node c")],
            edges=[
                _edge("reverse", "a", "b", "REVERSE"),
                _edge("both", "a", "c", "BIDIRECTIONAL"),
            ],
        )
    )
    index = _index(path)
    index.catch_up("run-1", target_cursor=cursor, target_revision=1)

    assert index.adjacent("run-1", "a", "SUCCESSOR", target_cursor=cursor, target_revision=1) == (
        AdjacencyHit("both", "c"),
    )
    assert index.adjacent("run-1", "a", "PREDECESSOR", target_cursor=cursor, target_revision=1) == (
        AdjacencyHit("both", "c"),
        AdjacencyHit("reverse", "b"),
    )


def test_non_kernel_or_authority_writing_delta_rolls_back_without_advancing_watermark(
    tmp_path: Path,
) -> None:
    path = _database(tmp_path)
    store = ActivityStore(path)
    cursor = store.append(_delta("event-1", revision=1, source="WORKER"))
    index = _index(path)

    with pytest.raises(GraphIndexEventError, match="kernel-sourced"):
        index.catch_up("run-1", target_cursor=cursor, target_revision=1)
    assert index.watermark("run-1").processed_cursor == 0

    second_dir = tmp_path / "authority-write"
    second_dir.mkdir()
    second_path = _database(second_dir)
    second_store = ActivityStore(second_path)
    cursor = second_store.append(_delta("event-2", revision=1, authority_effect="WRITE_FACT_GRAPH"))
    second_index = _index(second_path)
    with pytest.raises(GraphIndexEventError, match="forbid fact graph writes"):
        second_index.catch_up("run-1", target_cursor=cursor, target_revision=1)
    assert second_index.watermark("run-1").processed_cursor == 0


def test_future_and_stale_fences_are_refused(tmp_path: Path) -> None:
    path = _database(tmp_path)
    cursor = ActivityStore(path).append(_delta("event-1", revision=3))
    index = _index(path)

    with pytest.raises(GraphIndexFenceError, match="beyond durable"):
        index.catch_up("run-1", target_cursor=cursor + 1, target_revision=3)
    index.catch_up("run-1", target_cursor=cursor, target_revision=3)
    with pytest.raises(GraphIndexFenceError, match="older"):
        index.search_claim_ids("run-1", "anything", target_cursor=0, target_revision=0)


def test_activity_newer_than_declared_revision_fence_is_refused(tmp_path: Path) -> None:
    path = _database(tmp_path)
    cursor = ActivityStore(path).append(
        _delta("event-1", revision=5, nodes=[_node("a", "newer fact")])
    )
    index = _index(path)

    with pytest.raises(GraphIndexFenceError, match="exceeds"):
        index.catch_up("run-1", target_cursor=cursor, target_revision=4)
    assert index.watermark("run-1").processed_cursor == 0
