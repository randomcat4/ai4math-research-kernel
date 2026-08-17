"""SQLite research catalog with fence-bound opaque keyset pagination."""

from __future__ import annotations

import base64
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rk.product.summary import ResearchSummaryProjection
from rk.sqlite import open_sqlite


class CatalogCursorError(ValueError):
    pass


class CatalogFenceChanged(CatalogCursorError):
    pass


@dataclass(frozen=True, slots=True)
class ResearchListQuery:
    limit: int
    owners: tuple[str, ...] = ()
    labels: tuple[str, ...] = ()
    outcomes: tuple[str, ...] = ()
    text: str | None = None
    cursor: str | None = None


@dataclass(frozen=True, slots=True)
class ResearchPage:
    items: tuple[dict[str, Any], ...]
    catalog_revision: int
    next_cursor: str | None
    total: int


class ResearchCatalog:
    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)

    def register(
        self,
        *,
        run_id: str,
        title: str,
        question_summary: str,
        owner: str,
        labels: tuple[str, ...],
        created_at: str,
    ) -> int:
        with self._connect() as c:
            c.execute("BEGIN IMMEDIATE")
            c.execute(
                "INSERT INTO research_catalog VALUES(?,?,?,?,?,?)",
                (run_id, title, question_summary, owner, _json(sorted(set(labels))), created_at),
            )
            revision = self._bump(c)
            c.commit()
            return revision

    def project(self, run_id: str, value: ResearchSummaryProjection) -> int:
        row = (
            run_id,
            value.outcome_state,
            value.execution_state,
            value.authority_state,
            value.publication_state,
            value.phase,
            _json(value.blockers),
            _json(value.next_actions),
            _json(value.available_actions),
            _json(value.budget.as_dict()),
            value.recent_activity_at,
            value.recent_activity_summary,
            value.research_revision,
            value.contract_version,
            value.last_cursor,
            value.projection_source_digest,
        )
        with self._connect() as c:
            c.execute("BEGIN IMMEDIATE")
            c.execute(
                "INSERT INTO research_summary_projection VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
                "ON CONFLICT(run_id) DO UPDATE SET outcome_state=excluded.outcome_state,"
                "execution_state=excluded.execution_state,authority_state=excluded.authority_state,"
                "publication_state=excluded.publication_state,phase=excluded.phase,"
                "blockers_json=excluded.blockers_json,next_actions_json=excluded.next_actions_json,"
                "available_actions_json=excluded.available_actions_json,budget_json=excluded.budget_json,"
                "recent_activity_at=excluded.recent_activity_at,recent_activity_summary=excluded.recent_activity_summary,"
                "research_revision=excluded.research_revision,contract_version=excluded.contract_version,"
                "last_cursor=excluded.last_cursor,projection_source_digest=excluded.projection_source_digest",
                row,
            )
            revision = self._bump(c)
            c.commit()
            return revision

    def list(self, query: ResearchListQuery) -> ResearchPage:
        if query.limit < 1 or query.limit > 200:
            raise ValueError("limit must be 1..200")
        digest = _digest(
            {
                "owners": sorted(query.owners),
                "labels": sorted(query.labels),
                "outcomes": sorted(query.outcomes),
                "text": query.text,
            }
        )
        with self._connect() as c:
            c.execute("BEGIN")
            fence = int(
                c.execute(
                    "SELECT revision FROM research_catalog_fence WHERE singleton=1"
                ).fetchone()[0]
            )
            after: tuple[str, str] | None = None
            if query.cursor:
                after = self._decode(query.cursor, digest, fence)
            rows = c.execute(
                "SELECT c.run_id,c.title,c.question_summary,c.owner,c.labels_json,"
                "s.outcome_state,s.execution_state,s.authority_state,s.publication_state,s.phase,"
                "s.blockers_json,s.next_actions_json,s.available_actions_json,s.budget_json,"
                "s.recent_activity_at,s.recent_activity_summary,s.research_revision,"
                "s.contract_version,s.last_cursor "
                "FROM research_catalog c JOIN research_summary_projection s USING(run_id) "
                "ORDER BY s.recent_activity_at DESC,c.run_id ASC"
            ).fetchall()
        items = [self._item(r) for r in rows if self._matches(r, query)]
        if after:
            items = [
                x
                for x in items
                if (
                    x["recent_activity_at"] < after[0]
                    or (x["recent_activity_at"] == after[0] and x["run_id"] > after[1])
                )
            ]
        total = len(items)
        selected = items[: query.limit]
        cursor = None
        if len(items) > query.limit:
            last = selected[-1]
            cursor = self._encode(digest, fence, last["recent_activity_at"], last["run_id"])
        return ResearchPage(tuple(selected), fence, cursor, total)

    @staticmethod
    def _matches(r: tuple[Any, ...], q: ResearchListQuery) -> bool:
        labels = set(json.loads(r[4]))
        hay = f"{r[1]} {r[2]}".casefold()
        return (
            (not q.owners or r[3] in q.owners)
            and (not q.labels or set(q.labels) <= labels)
            and (not q.outcomes or r[5] in q.outcomes)
            and (q.text is None or q.text.casefold() in hay)
        )

    @staticmethod
    def _item(r: tuple[Any, ...]) -> dict[str, Any]:
        return {
            "run_id": r[0],
            "title": r[1],
            "question_summary": r[2],
            "owner": r[3],
            "labels": json.loads(r[4]),
            "outcome_state": r[5],
            "execution_state": r[6],
            "authority_state": r[7],
            "publication_state": r[8],
            "phase": r[9],
            "blockers": json.loads(r[10]),
            "next_actions": json.loads(r[11]),
            "available_actions": json.loads(r[12]),
            "budget": json.loads(r[13]),
            "recent_activity_at": r[14],
            "recent_activity_summary": r[15],
            "research_revision": r[16],
            "contract_version": r[17],
            "last_cursor": r[18],
        }

    @staticmethod
    def _encode(digest: str, fence: int, recent: str, run_id: str) -> str:
        body = _json({"v": 1, "q": digest, "f": fence, "a": recent, "r": run_id})
        check = hashlib.sha256(("rk.catalog.cursor.v1\0" + body).encode()).hexdigest()
        return base64.urlsafe_b64encode((body + "\n" + check).encode()).decode().rstrip("=")

    @staticmethod
    def _decode(token: str, digest: str, fence: int) -> tuple[str, str]:
        try:
            raw = base64.urlsafe_b64decode(token + "=" * (-len(token) % 4)).decode()
            body, check = raw.rsplit("\n", 1)
            if hashlib.sha256(("rk.catalog.cursor.v1\0" + body).encode()).hexdigest() != check:
                raise CatalogCursorError("cursor checksum mismatch")
            value = json.loads(body)
        except (ValueError, UnicodeError, json.JSONDecodeError) as e:
            raise CatalogCursorError("invalid cursor") from e
        if value["q"] != digest:
            raise CatalogCursorError("cursor query mismatch")
        if value["f"] != fence:
            raise CatalogFenceChanged("catalog changed")
        return str(value["a"]), str(value["r"])

    @staticmethod
    def _bump(c: sqlite3.Connection) -> int:
        c.execute("UPDATE research_catalog_fence SET revision=revision+1 WHERE singleton=1")
        return int(
            c.execute("SELECT revision FROM research_catalog_fence WHERE singleton=1").fetchone()[0]
        )

    def _connect(self) -> sqlite3.Connection:
        c = open_sqlite(self._db_path)
        c.execute("PRAGMA foreign_keys=ON")
        return c


def _json(v: Any) -> str:
    return json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _digest(v: Any) -> str:
    return hashlib.sha256(_json(v).encode()).hexdigest()
