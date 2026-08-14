"""Source-typed literature search graph over immutable B08a snapshots."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path


class LiteratureGraphError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class LiteratureEntity:
    entity_id: str
    entity_kind: str
    canonical_key: str
    title: str | None
    statement: str | None
    arxiv_id: str | None
    arxiv_version: str | None
    theorem_id: str | None


@dataclass(frozen=True, slots=True)
class TheoremContext:
    context_id: str
    theorem_entity_id: str
    arxiv_snapshot_id: str
    arxiv_id: str
    arxiv_version: str
    anchor: dict[str, object]
    excerpt: str


class LiteratureGraphStore:
    def __init__(self, db_path: Path) -> None:
        self._db = Path(db_path)

    def add_entity(
        self,
        *,
        entity_id: str,
        entity_kind: str,
        canonical_key: str,
        title: str | None,
        statement: str | None = None,
        arxiv_id: str | None = None,
        arxiv_version: str | None = None,
        theorem_id: str | None = None,
        created_at: str,
    ) -> LiteratureEntity:
        with self._connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute(
                "SELECT entity_id FROM product_literature_entities "
                "WHERE entity_kind=? AND canonical_key=?",
                (entity_kind, canonical_key),
            ).fetchone()
            if row and str(row[0]) != entity_id:
                entity_id = str(row[0])
            elif not row:
                c.execute(
                    "INSERT INTO product_literature_entities VALUES(?,?,?,?,?,?,?,?,?)",
                    (
                        entity_id,
                        entity_kind,
                        canonical_key,
                        title,
                        statement,
                        arxiv_id,
                        arxiv_version,
                        theorem_id,
                        created_at,
                    ),
                )
            c.commit()
        return self.get_entity(entity_id)

    def add_source(
        self,
        *,
        entity_id: str,
        source_kind: str,
        source_record_key: str,
        source_anchor: dict[str, object],
        observed_at: str,
        snapshot_id: str | None = None,
        import_material_id: str | None = None,
        source_version: str | None = None,
    ) -> None:
        with self._connect() as c:
            self._validate_source(
                c, source_kind, snapshot_id, import_material_id, allow_non_success=True
            )
            c.execute(
                "INSERT INTO product_literature_entity_sources VALUES(?,?,?,?,?,?,?,?)",
                (
                    entity_id,
                    snapshot_id,
                    import_material_id,
                    source_kind,
                    source_record_key,
                    source_version,
                    _json(source_anchor),
                    observed_at,
                ),
            )

    def add_edge(
        self,
        *,
        edge_id: str,
        from_entity_id: str,
        to_entity_id: str,
        edge_kind: str,
        source_kind: str,
        source_anchor: dict[str, object],
        created_at: str,
        snapshot_id: str | None = None,
        import_material_id: str | None = None,
        source_version: str | None = None,
    ) -> None:
        with self._connect() as c:
            self._validate_source(
                c, source_kind, snapshot_id, import_material_id, allow_non_success=False
            )
            if source_kind == "MATLAS" and edge_kind != "CONTAINS_THEOREM":
                raise LiteratureGraphError(
                    "Matlas thin client did not supply author or citation edges"
                )
            c.execute(
                "INSERT INTO product_literature_edges VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    edge_id,
                    from_entity_id,
                    to_entity_id,
                    edge_kind,
                    source_kind,
                    snapshot_id,
                    import_material_id,
                    source_version,
                    _json(source_anchor),
                    created_at,
                ),
            )

    def add_context(
        self,
        *,
        context_id: str,
        theorem_entity_id: str,
        arxiv_snapshot_id: str,
        arxiv_id: str,
        arxiv_version: str,
        anchor: dict[str, object],
        excerpt: str,
        created_at: str,
    ) -> TheoremContext:
        if not excerpt.strip():
            raise ValueError("theorem context excerpt is required")
        with self._connect() as c:
            row = c.execute(
                "SELECT connector,result_status,normalized_json "
                "FROM product_source_snapshots WHERE snapshot_id=?",
                (arxiv_snapshot_id,),
            ).fetchone()
            if not row or tuple(row[:2]) != ("ARXIV", "SUCCESS"):
                raise LiteratureGraphError("context requires a successful exact arXiv snapshot")
            normalized = _obj(row[2])
            visible = str(normalized.get("arxiv_id", normalized.get("id", "")))
            if visible and arxiv_id not in visible:
                raise LiteratureGraphError("arXiv context identity mismatch")
            c.execute(
                "INSERT INTO product_theorem_contexts VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    context_id,
                    theorem_entity_id,
                    arxiv_snapshot_id,
                    arxiv_id,
                    arxiv_version,
                    _json(anchor),
                    excerpt,
                    hashlib.sha256(excerpt.encode()).hexdigest(),
                    created_at,
                ),
            )
        return self.get_context(context_id)

    def link(
        self,
        *,
        link_id: str,
        entity_id: str,
        run_id: str,
        link_kind: str,
        created_by: str,
        created_at: str,
        contract_id: str | None = None,
        contract_version: int | None = None,
        claim_id: str | None = None,
        route_id: str | None = None,
        bridge_opportunity_id: str | None = None,
    ) -> None:
        with self._connect() as c:
            c.execute(
                "INSERT INTO product_literature_links VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    link_id,
                    entity_id,
                    run_id,
                    contract_id,
                    contract_version,
                    claim_id,
                    route_id,
                    bridge_opportunity_id,
                    link_kind,
                    created_by,
                    created_at,
                ),
            )

    def get_entity(self, entity_id: str) -> LiteratureEntity:
        with self._connect() as c:
            r = c.execute(
                "SELECT entity_id,entity_kind,canonical_key,title,statement,arxiv_id,"
                "arxiv_version,theorem_id FROM product_literature_entities "
                "WHERE entity_id=?",
                (entity_id,),
            ).fetchone()
        if not r:
            raise KeyError(entity_id)
        return LiteratureEntity(*[str(x) if x is not None else None for x in r])  # type: ignore[arg-type]

    def get_context(self, context_id: str) -> TheoremContext:
        with self._connect() as c:
            r = c.execute(
                "SELECT context_id,theorem_entity_id,arxiv_snapshot_id,arxiv_id,"
                "arxiv_version,anchor_json,excerpt FROM product_theorem_contexts "
                "WHERE context_id=?",
                (context_id,),
            ).fetchone()
        if not r:
            raise KeyError(context_id)
        return TheoremContext(
            str(r[0]), str(r[1]), str(r[2]), str(r[3]), str(r[4]), _obj(r[5]), str(r[6])
        )

    def _validate_source(
        self,
        c: sqlite3.Connection,
        kind: str,
        snapshot: str | None,
        material: str | None,
        *,
        allow_non_success: bool,
    ) -> None:
        if kind == "HUMAN_IMPORT":
            if not material or snapshot:
                raise LiteratureGraphError("human source requires one imported material")
            return
        if not snapshot or material:
            raise LiteratureGraphError("connector source requires one snapshot")
        row = c.execute(
            "SELECT connector,result_status FROM product_source_snapshots WHERE snapshot_id=?",
            (snapshot,),
        ).fetchone()
        if not row or str(row[0]) != kind:
            raise LiteratureGraphError("source connector binding mismatch")
        if not allow_non_success and str(row[1]) != "SUCCESS":
            raise LiteratureGraphError("graph edges require a successful source")

    def _connect(self) -> sqlite3.Connection:
        c = sqlite3.connect(self._db, isolation_level=None)
        c.execute("PRAGMA foreign_keys=ON")
        return c


def _json(v: object) -> str:
    return json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _obj(v: object) -> dict[str, object]:
    x = json.loads(str(v))
    if not isinstance(x, dict):
        raise LiteratureGraphError("stored JSON is not an object")
    return x


__all__ = ["LiteratureEntity", "LiteratureGraphError", "LiteratureGraphStore", "TheoremContext"]
