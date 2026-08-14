from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest

from rk.product.api import ProductSession, QuerySpec
from rk.product.domain_queries import DomainObjectNotFound, FenceSource
from rk.product.research_queries import ResearchQueries
from rk.product_migrations import ProductMigrationAssembler, ProductMigrationRegistry


@dataclass(frozen=True)
class Fence:
    run_id: str
    research_revision: int = 7
    contract_version: int = 3
    last_cursor: int = 11


class Fences:
    def run(self, run_id: str) -> Fence:
        return Fence(run_id)


def adapter(tmp_path: Path) -> ResearchQueries:
    db_path = tmp_path / "research-queries.sqlite"
    with sqlite3.connect(db_path, isolation_level=None) as connection:
        connection.execute(
            "CREATE TABLE runs(run_id TEXT PRIMARY KEY,revision INTEGER NOT NULL,"
            "current_contract_version INTEGER NOT NULL) STRICT"
        )
        connection.execute("INSERT INTO runs VALUES('run-1',7,3)")
        ProductMigrationAssembler(ProductMigrationRegistry(Path("schema_fragments"))).apply(
            connection
        )
        connection.execute(
            "INSERT INTO product_contracts VALUES(?,?,?,?,?)",
            ("contract-1", "run-1", 3, "2026-08-14T00:00:00Z", "2026-08-14T00:00:00Z"),
        )
        connection.execute(
            "INSERT INTO product_contract_versions VALUES(?,?,?,?,?,?,?,?,?)",
            (
                "contract-1",
                3,
                "CONFIRMED",
                '{"question":"q"}',
                "a" * 64,
                None,
                "owner",
                "2026-08-14T00:00:00Z",
                "2026-08-14T00:00:00Z",
            ),
        )
    return ResearchQueries(
        db_path=db_path,
        fences=cast(FenceSource, Fences()),
        cursor_secret=b"research-query-test-cursor-secret-32-bytes",
    )


SESSION = ProductSession("session-1", "subject-1", ())
SCOPE = {"kind": "RUN", "run_id": "run-1"}


def test_contract_projects_real_current_version(tmp_path: Path) -> None:
    result = adapter(tmp_path).execute(
        SESSION, QuerySpec(SCOPE, "CONTRACT", {"contract_id": "contract-1"})
    )
    assert result.stable_entity_id == "contract-1"
    assert result.data["content_digest"] == "a" * 64
    assert result.data["content_json"] == {"question": "q"}
    assert result.fence["research_revision"] == 7


def test_absent_literature_aggregate_is_not_relabelled(tmp_path: Path) -> None:
    with pytest.raises(DomainObjectNotFound):
        adapter(tmp_path).execute(
            SESSION,
            QuerySpec(
                SCOPE,
                "LITERATURE_QUERY",
                {"literature_query_id": "missing-query"},
            ),
        )
