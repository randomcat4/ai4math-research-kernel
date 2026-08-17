"""Prior-art comparison and independently confirmed novelty boundaries."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path


class NoveltyBoundaryError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class NoveltyReview:
    novelty_review_id: str
    run_id: str
    target_link_id: str
    conclusion: str
    reviewed_by: str


class NoveltyStore:
    def __init__(self, db_path: Path) -> None:
        self._db = Path(db_path)

    def compare(
        self,
        *,
        comparison_id: str,
        run_id: str,
        target_link_id: str,
        entity_id: str,
        overlap: dict[str, object],
        difference: dict[str, object],
        assessed_by: str,
        assessed_at: str,
    ) -> None:
        if not overlap or not difference or not assessed_by:
            raise ValueError("explicit prior-art comparison is required")
        with self._connect() as c:
            c.execute(
                "INSERT INTO product_prior_art_comparisons VALUES(?,?,?,?,?,?,?,?)",
                (
                    comparison_id,
                    run_id,
                    target_link_id,
                    entity_id,
                    _j(overlap),
                    _j(difference),
                    assessed_by,
                    assessed_at,
                ),
            )

    def review(
        self,
        *,
        novelty_review_id: str,
        run_id: str,
        target_link_id: str,
        boundary: dict[str, object],
        conclusion: str,
        reviewed_by: str,
        reviewed_at: str,
    ) -> NoveltyReview:
        required = {"snapshot_ids", "queries", "limitations"}
        if set(boundary) != required or not all(boundary[k] for k in required):
            raise NoveltyBoundaryError(
                "novelty boundary must state snapshots, queries, and limitations"
            )
        ids = boundary["snapshot_ids"]
        if not isinstance(ids, list) or any(not isinstance(x, str) for x in ids):
            raise NoveltyBoundaryError("snapshot boundary is invalid")
        with self._connect() as c:
            if conclusion == "NOVEL_WITHIN_REVIEWED_BOUNDARY":
                self._gate(c, target_link_id, tuple(ids), reviewed_by)
            c.execute(
                "INSERT INTO product_novelty_reviews VALUES(?,?,?,?,?,?,?)",
                (
                    novelty_review_id,
                    run_id,
                    target_link_id,
                    _j(boundary),
                    conclusion,
                    reviewed_by,
                    reviewed_at,
                ),
            )
        return NoveltyReview(novelty_review_id, run_id, target_link_id, conclusion, reviewed_by)

    def _gate(self, c: sqlite3.Connection, link: str, ids: tuple[str, ...], reviewer: str) -> None:
        if not ids:
            raise NoveltyBoundaryError("no-hit cannot establish novelty")
        q = ",".join("?" for _ in ids)
        rows = c.execute(
            "SELECT snapshot_id,connector,result_status FROM product_source_snapshots "
            f"WHERE snapshot_id IN ({q})",
            ids,
        ).fetchall()
        if len(rows) != len(set(ids)) or any(str(r[2]) != "SUCCESS" for r in rows):
            raise NoveltyBoundaryError("NO_HIT or failed snapshots cannot establish novelty")
        kinds = {str(r[1]) for r in rows}
        imports = c.execute(
            "SELECT COUNT(*) FROM product_literature_entity_sources s "
            "JOIN product_literature_links l ON l.entity_id=s.entity_id "
            "WHERE l.link_id=? AND s.source_kind='HUMAN_IMPORT'",
            (link,),
        ).fetchone()
        if len(kinds) < 2 and int(imports[0] if imports else 0) == 0:
            raise NoveltyBoundaryError("thin-client-only coverage cannot establish novelty")
        comparisons = c.execute(
            "SELECT assessed_by FROM product_prior_art_comparisons WHERE target_link_id=?", (link,)
        ).fetchall()
        if not comparisons:
            raise NoveltyBoundaryError("prior-art comparison is required")
        if any(str(r[0]) == reviewer for r in comparisons):
            raise NoveltyBoundaryError("novelty boundary requires an independent reviewer")

    def _connect(self) -> sqlite3.Connection:
        c = sqlite3.connect(self._db)
        c.execute("PRAGMA foreign_keys=ON")
        return c


def _j(v: object) -> str:
    return json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


__all__ = ["NoveltyBoundaryError", "NoveltyReview", "NoveltyStore"]
