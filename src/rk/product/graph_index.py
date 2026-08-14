"""Rebuildable FTS and adjacency projection over kernel graph activity.

The index is only a read accelerator. Kernel activity or an explicit authority
snapshot supplies every value; index writes have no mathematical authority.
"""

from __future__ import annotations

import json
import re
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, cast


class GraphIndexError(RuntimeError):
    """The graph projection contract was violated."""


class GraphIndexEventError(GraphIndexError):
    """A kernel graph projection event is invalid."""


class GraphIndexFenceError(GraphIndexError):
    """A requested cursor/revision fence is impossible or stale."""


@dataclass(frozen=True, slots=True)
class GraphIndexWatermark:
    run_id: str
    processed_cursor: int
    research_revision: int
    rebuilt_at: str | None


class ProjectionLag(GraphIndexError):
    code = "PROJECTION_LAG"
    http_status = 409

    def __init__(
        self, current: GraphIndexWatermark, target_cursor: int, target_revision: int
    ) -> None:
        self.current = current
        self.target_cursor = target_cursor
        self.target_revision = target_revision
        super().__init__(
            "graph projection is behind the requested authority fence: "
            f"cursor {current.processed_cursor}/{target_cursor}, "
            f"revision {current.research_revision}/{target_revision}"
        )


@dataclass(frozen=True, slots=True)
class IndexedGraphNode:
    claim_id: str
    stable_label: str
    statement: str
    lifecycle: str
    dependable: bool
    claim_type: str
    authority_axes: Mapping[str, Any]
    contract_version: int
    verification_method: str
    source_worker_run_id: str | None = None
    route_id: str | None = None


@dataclass(frozen=True, slots=True)
class IndexedGraphEdge:
    edge_id: str
    from_claim_id: str
    to_claim_id: str
    logical_direction: Literal["FORWARD", "REVERSE", "BIDIRECTIONAL"]
    obligation_status: str
    bridge_spec_id: str | None = None


@dataclass(frozen=True, slots=True)
class GraphAuthoritySnapshot:
    run_id: str
    processed_cursor: int
    research_revision: int
    source_kernel_event_id: str
    captured_at: str
    nodes: tuple[IndexedGraphNode, ...]
    edges: tuple[IndexedGraphEdge, ...]


@dataclass(frozen=True, slots=True)
class AdjacencyHit:
    edge_id: str
    neighbor_claim_id: str


LagPolicy = Literal["REJECT", "CATCH_UP"]
Direction = Literal["PREDECESSOR", "SUCCESSOR"]
_DELTA_SCHEMA = "rk.product.graph_projection_delta.v1"
_WORD = re.compile(r"[^\W_]+", re.UNICODE)


class GraphIndex:
    """Atomically maintain and query a disposable graph projection."""

    def __init__(
        self,
        db_path: Path,
        *,
        clock: Callable[[], str],
        busy_timeout_ms: int = 5_000,
        connection_factory: Callable[[], sqlite3.Connection] | None = None,
    ) -> None:
        self._db_path = Path(db_path)
        self._clock = clock
        self._busy_timeout_ms = busy_timeout_ms
        self._connection_factory = connection_factory

    def watermark(self, run_id: str) -> GraphIndexWatermark:
        _nonempty(run_id, "run_id")
        with self._connect() as connection:
            connection.execute("BEGIN")
            result = self._watermark(connection, run_id)
            connection.commit()
        return result

    def rebuild(self, snapshot: GraphAuthoritySnapshot) -> GraphIndexWatermark:
        _fence(snapshot.processed_cursor, snapshot.research_revision)
        _nonempty(snapshot.run_id, "run_id")
        _nonempty(snapshot.source_kernel_event_id, "source_kernel_event_id")
        _nonempty(snapshot.captured_at, "captured_at")
        self._validate_snapshot(snapshot)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._require_reachable(connection, snapshot.processed_cursor)
            connection.execute("DELETE FROM product_graph_fts WHERE run_id=?", (snapshot.run_id,))
            connection.execute("DELETE FROM product_graph_edges WHERE run_id=?", (snapshot.run_id,))
            connection.execute("DELETE FROM product_graph_nodes WHERE run_id=?", (snapshot.run_id,))
            for node in snapshot.nodes:
                self._upsert_node(
                    connection,
                    snapshot.run_id,
                    node,
                    snapshot.processed_cursor,
                    snapshot.source_kernel_event_id,
                )
            for edge in snapshot.edges:
                self._upsert_edge(
                    connection,
                    snapshot.run_id,
                    edge,
                    snapshot.processed_cursor,
                    snapshot.source_kernel_event_id,
                )
            self._write_watermark(
                connection,
                snapshot.run_id,
                snapshot.processed_cursor,
                snapshot.research_revision,
                snapshot.captured_at,
            )
            connection.commit()
        return self.watermark(snapshot.run_id)

    def catch_up(
        self, run_id: str, *, target_cursor: int, target_revision: int
    ) -> GraphIndexWatermark:
        _nonempty(run_id, "run_id")
        _fence(target_cursor, target_revision)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._watermark(connection, run_id)
            self._require_forward(current, target_cursor, target_revision)
            self._require_reachable(connection, target_cursor)
            rows = connection.execute(
                "SELECT cursor,source,research_revision,kernel_event_id,payload_json "
                "FROM product_activity_events "
                "WHERE run_id=? AND cursor>? AND cursor<=? ORDER BY cursor",
                (run_id, current.processed_cursor, target_cursor),
            ).fetchall()
            try:
                for row in rows:
                    self._apply_activity(connection, run_id, target_revision, row)
            except (GraphIndexEventError, sqlite3.IntegrityError):
                connection.rollback()
                raise
            self._write_watermark(connection, run_id, target_cursor, target_revision, self._clock())
            connection.commit()
        return self.watermark(run_id)

    def search_claim_ids(
        self,
        run_id: str,
        query: str,
        *,
        target_cursor: int,
        target_revision: int,
        limit: int = 200,
        lag_policy: LagPolicy = "REJECT",
    ) -> tuple[str, ...]:
        if not 1 <= limit <= 200:
            raise ValueError("limit must be between 1 and 200")
        match = _fts_match(query)
        return self._aligned_read(
            run_id,
            target_cursor,
            target_revision,
            lag_policy,
            lambda connection: tuple(
                str(row[0])
                for row in connection.execute(
                    "SELECT claim_id FROM product_graph_fts "
                    "WHERE product_graph_fts MATCH ? AND run_id=? "
                    "ORDER BY bm25(product_graph_fts),claim_id LIMIT ?",
                    (match, run_id, limit),
                ).fetchall()
            ),
        )

    def adjacent(
        self,
        run_id: str,
        claim_id: str,
        direction: Direction,
        *,
        target_cursor: int,
        target_revision: int,
        lag_policy: LagPolicy = "REJECT",
    ) -> tuple[AdjacencyHit, ...]:
        _nonempty(claim_id, "claim_id")
        if direction not in ("PREDECESSOR", "SUCCESSOR"):
            raise ValueError("direction must be PREDECESSOR or SUCCESSOR")
        return self._aligned_read(
            run_id,
            target_cursor,
            target_revision,
            lag_policy,
            lambda connection: self._adjacent(connection, run_id, claim_id, direction),
        )

    def _aligned_read[T](
        self,
        run_id: str,
        target_cursor: int,
        target_revision: int,
        lag_policy: LagPolicy,
        reader: Callable[[sqlite3.Connection], T],
    ) -> T:
        _nonempty(run_id, "run_id")
        _fence(target_cursor, target_revision)
        if lag_policy not in ("REJECT", "CATCH_UP"):
            raise ValueError("lag_policy must be REJECT or CATCH_UP")
        with self._connect() as connection:
            connection.execute("BEGIN")
            current = self._watermark(connection, run_id)
            if self._aligned(current, target_cursor, target_revision):
                result = reader(connection)
                connection.commit()
                return result
            connection.rollback()
        if current.processed_cursor > target_cursor or (
            current.processed_cursor == target_cursor
            and current.research_revision != target_revision
        ):
            raise GraphIndexFenceError("requested authority fence is older than graph projection")
        if lag_policy == "REJECT":
            raise ProjectionLag(current, target_cursor, target_revision)
        self.catch_up(run_id, target_cursor=target_cursor, target_revision=target_revision)
        with self._connect() as connection:
            connection.execute("BEGIN")
            current = self._watermark(connection, run_id)
            if not self._aligned(current, target_cursor, target_revision):
                connection.rollback()
                raise ProjectionLag(current, target_cursor, target_revision)
            result = reader(connection)
            connection.commit()
            return result

    @staticmethod
    def _aligned(watermark: GraphIndexWatermark, cursor: int, revision: int) -> bool:
        return watermark.processed_cursor == cursor and watermark.research_revision == revision

    @staticmethod
    def _require_forward(current: GraphIndexWatermark, cursor: int, revision: int) -> None:
        if cursor < current.processed_cursor:
            raise GraphIndexFenceError("target cursor precedes the processed cursor")
        if cursor == current.processed_cursor:
            if revision != current.research_revision:
                raise GraphIndexFenceError("one cursor cannot be rebound to another revision")
            return
        if revision < current.research_revision:
            raise GraphIndexFenceError("target revision precedes the indexed authority revision")

    @staticmethod
    def _require_reachable(connection: sqlite3.Connection, cursor: int) -> None:
        row = connection.execute(
            "SELECT COALESCE(MAX(cursor),0) FROM product_activity_events"
        ).fetchone()
        if row is None or cursor > int(row[0]):
            raise GraphIndexFenceError("target cursor is beyond durable product activity")

    def _apply_activity(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        target_revision: int,
        row: tuple[object, ...],
    ) -> None:
        cursor, source = int(str(row[0])), str(row[1])
        event_revision = int(str(row[2])) if row[2] is not None else None
        kernel_event_id = str(row[3]) if row[3] is not None else None
        payload = _json_object(row[4], "activity payload")
        if payload.get("type") != "GRAPH_PROJECTION_DELTA":
            return
        if source != "KERNEL" or not kernel_event_id:
            raise GraphIndexEventError("graph projection delta must be kernel-sourced")
        if event_revision is None:
            raise GraphIndexEventError("graph projection delta requires research_revision")
        if event_revision > target_revision:
            raise GraphIndexFenceError(
                "graph projection delta exceeds the requested authority revision"
            )
        _exact_keys(
            payload,
            {
                "type",
                "schema_version",
                "authority_effect",
                "upsert_nodes",
                "delete_node_ids",
                "upsert_edges",
                "delete_edge_ids",
            },
            set(),
            "graph projection delta",
        )
        if payload["schema_version"] != _DELTA_SCHEMA:
            raise GraphIndexEventError("unknown graph projection delta schema")
        if payload["authority_effect"] != "NO_FACT_GRAPH_WRITE":
            raise GraphIndexEventError("graph projection delta must forbid fact graph writes")
        for edge_id in _string_list(payload["delete_edge_ids"], "delete_edge_ids"):
            connection.execute(
                "DELETE FROM product_graph_edges WHERE run_id=? AND edge_id=?", (run_id, edge_id)
            )
        for claim_id in _string_list(payload["delete_node_ids"], "delete_node_ids"):
            connection.execute(
                "DELETE FROM product_graph_fts WHERE run_id=? AND claim_id=?", (run_id, claim_id)
            )
            connection.execute(
                "DELETE FROM product_graph_nodes WHERE run_id=? AND claim_id=?", (run_id, claim_id)
            )
        for value in _mapping_list(payload["upsert_nodes"], "upsert_nodes"):
            self._upsert_node(connection, run_id, _node(value), cursor, kernel_event_id)
        for value in _mapping_list(payload["upsert_edges"], "upsert_edges"):
            self._upsert_edge(connection, run_id, _edge(value), cursor, kernel_event_id)

    @staticmethod
    def _upsert_node(
        connection: sqlite3.Connection,
        run_id: str,
        node: IndexedGraphNode,
        cursor: int,
        kernel_event_id: str,
    ) -> None:
        axes = json.dumps(
            node.authority_axes, ensure_ascii=False, sort_keys=True, separators=(",", ":")
        )
        connection.execute(
            "INSERT INTO product_graph_nodes("
            "run_id,claim_id,stable_label,statement,lifecycle,dependable,claim_type,"
            "authority_axes_json,contract_version,verification_method,"
            "source_worker_run_id,route_id,source_activity_cursor,source_kernel_event_id) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(run_id,claim_id) DO UPDATE SET "
            "stable_label=excluded.stable_label,"
            "statement=excluded.statement,"
            "lifecycle=excluded.lifecycle,"
            "dependable=excluded.dependable,"
            "claim_type=excluded.claim_type,"
            "authority_axes_json=excluded.authority_axes_json,"
            "contract_version=excluded.contract_version,"
            "verification_method=excluded.verification_method,"
            "source_worker_run_id=excluded.source_worker_run_id,"
            "route_id=excluded.route_id,"
            "source_activity_cursor=excluded.source_activity_cursor,"
            "source_kernel_event_id=excluded.source_kernel_event_id",
            (
                run_id,
                node.claim_id,
                node.stable_label,
                node.statement,
                node.lifecycle,
                int(node.dependable),
                node.claim_type,
                axes,
                node.contract_version,
                node.verification_method,
                node.source_worker_run_id,
                node.route_id,
                cursor,
                kernel_event_id,
            ),
        )
        connection.execute(
            "DELETE FROM product_graph_fts WHERE run_id=? AND claim_id=?", (run_id, node.claim_id)
        )
        connection.execute(
            "INSERT INTO product_graph_fts(run_id,claim_id,stable_label,statement) VALUES(?,?,?,?)",
            (run_id, node.claim_id, node.stable_label, node.statement),
        )

    @staticmethod
    def _upsert_edge(
        connection: sqlite3.Connection,
        run_id: str,
        edge: IndexedGraphEdge,
        cursor: int,
        kernel_event_id: str,
    ) -> None:
        connection.execute(
            "INSERT INTO product_graph_edges("
            "run_id,edge_id,from_claim_id,to_claim_id,logical_direction,bridge_spec_id,"
            "obligation_status,source_activity_cursor,source_kernel_event_id) "
            "VALUES(?,?,?,?,?,?,?,?,?) ON CONFLICT(run_id,edge_id) DO UPDATE SET "
            "from_claim_id=excluded.from_claim_id,"
            "to_claim_id=excluded.to_claim_id,"
            "logical_direction=excluded.logical_direction,"
            "bridge_spec_id=excluded.bridge_spec_id,"
            "obligation_status=excluded.obligation_status,"
            "source_activity_cursor=excluded.source_activity_cursor,"
            "source_kernel_event_id=excluded.source_kernel_event_id",
            (
                run_id,
                edge.edge_id,
                edge.from_claim_id,
                edge.to_claim_id,
                edge.logical_direction,
                edge.bridge_spec_id,
                edge.obligation_status,
                cursor,
                kernel_event_id,
            ),
        )

    @staticmethod
    def _adjacent(
        connection: sqlite3.Connection, run_id: str, claim_id: str, direction: Direction
    ) -> tuple[AdjacencyHit, ...]:
        rows = connection.execute(
            "SELECT edge_id,from_claim_id,to_claim_id,logical_direction "
            "FROM product_graph_edges "
            "WHERE run_id=? AND (from_claim_id=? OR to_claim_id=?) ORDER BY edge_id",
            (run_id, claim_id, claim_id),
        ).fetchall()
        hits: set[AdjacencyHit] = set()
        for row in rows:
            edge_id, from_id, to_id, logical = (
                str(row[0]),
                str(row[1]),
                str(row[2]),
                str(row[3]),
            )
            if logical in ("FORWARD", "BIDIRECTIONAL"):
                if direction == "SUCCESSOR" and from_id == claim_id:
                    hits.add(AdjacencyHit(edge_id, to_id))
                if direction == "PREDECESSOR" and to_id == claim_id:
                    hits.add(AdjacencyHit(edge_id, from_id))
            if logical in ("REVERSE", "BIDIRECTIONAL"):
                if direction == "SUCCESSOR" and to_id == claim_id:
                    hits.add(AdjacencyHit(edge_id, from_id))
                if direction == "PREDECESSOR" and from_id == claim_id:
                    hits.add(AdjacencyHit(edge_id, to_id))
        return tuple(sorted(hits, key=lambda hit: (hit.edge_id, hit.neighbor_claim_id)))

    @staticmethod
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

    @staticmethod
    def _write_watermark(
        connection: sqlite3.Connection,
        run_id: str,
        cursor: int,
        revision: int,
        rebuilt_at: str,
    ) -> None:
        connection.execute(
            "INSERT INTO product_graph_index_watermarks("
            "run_id,processed_cursor,research_revision,rebuilt_at) VALUES(?,?,?,?) "
            "ON CONFLICT(run_id) DO UPDATE SET "
            "processed_cursor=excluded.processed_cursor,"
            "research_revision=excluded.research_revision,"
            "rebuilt_at=excluded.rebuilt_at",
            (run_id, cursor, revision, rebuilt_at),
        )

    @staticmethod
    def _validate_snapshot(snapshot: GraphAuthoritySnapshot) -> None:
        node_ids, edge_ids = (
            [node.claim_id for node in snapshot.nodes],
            [edge.edge_id for edge in snapshot.edges],
        )
        if len(node_ids) != len(set(node_ids)):
            raise ValueError("authority snapshot contains duplicate claim_id")
        if len(edge_ids) != len(set(edge_ids)):
            raise ValueError("authority snapshot contains duplicate edge_id")
        known = set(node_ids)
        if any(
            edge.from_claim_id not in known or edge.to_claim_id not in known
            for edge in snapshot.edges
        ):
            raise ValueError("authority snapshot edge endpoint is absent")

    def _connect(self) -> sqlite3.Connection:
        connection = (
            self._connection_factory()
            if self._connection_factory is not None
            else sqlite3.connect(self._db_path, timeout=self._busy_timeout_ms / 1_000)
        )
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
        return connection


def _node(value: Mapping[str, Any]) -> IndexedGraphNode:
    _exact_keys(
        value,
        {
            "claim_id",
            "stable_label",
            "statement",
            "lifecycle",
            "dependable",
            "claim_type",
            "authority_axes",
            "contract_version",
            "verification_method",
        },
        {"source_worker_run_id", "route_id"},
        "graph node",
    )
    axes, dependable, version = (
        value["authority_axes"],
        value["dependable"],
        value["contract_version"],
    )
    if not isinstance(axes, dict) or not axes:
        raise GraphIndexEventError("authority_axes must be a non-empty object")
    if not isinstance(dependable, bool):
        raise GraphIndexEventError("dependable must be boolean")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        raise GraphIndexEventError("contract_version must be a positive integer")
    return IndexedGraphNode(
        _string(value["claim_id"], "claim_id"),
        _string(value["stable_label"], "stable_label"),
        _string(value["statement"], "statement"),
        _string(value["lifecycle"], "lifecycle"),
        dependable,
        _string(value["claim_type"], "claim_type"),
        axes,
        version,
        _string(value["verification_method"], "verification_method"),
        _optional_string(value.get("source_worker_run_id"), "source_worker_run_id"),
        _optional_string(value.get("route_id"), "route_id"),
    )


def _edge(value: Mapping[str, Any]) -> IndexedGraphEdge:
    _exact_keys(
        value,
        {"edge_id", "from_claim_id", "to_claim_id", "logical_direction", "obligation_status"},
        {"bridge_spec_id"},
        "graph edge",
    )
    direction = value["logical_direction"]
    if direction not in ("FORWARD", "REVERSE", "BIDIRECTIONAL"):
        raise GraphIndexEventError("unknown logical_direction")
    return IndexedGraphEdge(
        _string(value["edge_id"], "edge_id"),
        _string(value["from_claim_id"], "from_claim_id"),
        _string(value["to_claim_id"], "to_claim_id"),
        cast(Literal["FORWARD", "REVERSE", "BIDIRECTIONAL"], direction),
        _string(value["obligation_status"], "obligation_status"),
        _optional_string(value.get("bridge_spec_id"), "bridge_spec_id"),
    )


def _fts_match(query: str) -> str:
    _nonempty(query, "query")
    terms = _WORD.findall(query)
    if not terms:
        raise ValueError("query must contain searchable Unicode terms")
    return " AND ".join(f'"{term}"*' for term in terms)


def _exact_keys(
    value: Mapping[str, Any], required: set[str], optional: set[str], label: str
) -> None:
    missing, unknown = required - value.keys(), value.keys() - required - optional
    if missing or unknown:
        raise GraphIndexEventError(
            f"{label} keys mismatch; missing={sorted(missing)}, unknown={sorted(unknown)}"
        )


def _mapping_list(value: object, label: str) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise GraphIndexEventError(f"{label} must be an array of objects")
    return cast(tuple[Mapping[str, Any], ...], tuple(value))


def _string_list(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise GraphIndexEventError(f"{label} must be an array")
    return tuple(_string(item, label) for item in value)


def _json_object(value: object, label: str) -> dict[str, Any]:
    result = json.loads(str(value))
    if not isinstance(result, dict):
        raise GraphIndexEventError(f"{label} must be an object")
    return cast(dict[str, Any], result)


def _string(value: object, label: str) -> str:
    if not isinstance(value, str) or not value or value != value.strip():
        raise GraphIndexEventError(f"{label} must be a non-empty trimmed string")
    return value


def _optional_string(value: object, label: str) -> str | None:
    return None if value is None else _string(value, label)


def _nonempty(value: str, label: str) -> None:
    if not value or value != value.strip():
        raise ValueError(f"{label} must be a non-empty trimmed string")


def _fence(cursor: int, revision: int) -> None:
    if isinstance(cursor, bool) or cursor < 0:
        raise ValueError("cursor must be a non-negative integer")
    if isinstance(revision, bool) or revision < 0:
        raise ValueError("revision must be a non-negative integer")


__all__ = [
    "AdjacencyHit",
    "GraphAuthoritySnapshot",
    "GraphIndex",
    "GraphIndexError",
    "GraphIndexEventError",
    "GraphIndexFenceError",
    "GraphIndexWatermark",
    "IndexedGraphEdge",
    "IndexedGraphNode",
    "ProjectionLag",
]
