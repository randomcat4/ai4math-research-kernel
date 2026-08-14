from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import cast

import pytest

from rk.product.api import ProductSession, QuerySpec
from rk.product.domain_queries import DomainObjectNotFound
from rk.product.operational_queries import OperationalFenceSource, OperationalQueries
from rk.product_migrations import ProductMigrationAssembler, ProductMigrationRegistry


@dataclass(frozen=True)
class RunFence:
    run_id: str
    research_revision: int = 4
    contract_version: int = 2
    last_cursor: int = 9


@dataclass(frozen=True)
class CatalogFence:
    deployment_id: str = "deployment-1"
    catalog_revision: int = 3
    last_cursor: int = 9


class Fences:
    def run(self, run_id: str) -> RunFence:
        return RunFence(run_id)

    def catalog(self) -> CatalogFence:
        return CatalogFence()


def adapter(tmp_path: Path) -> OperationalQueries:
    db_path = tmp_path / "operational.sqlite"
    with sqlite3.connect(db_path, isolation_level=None) as connection:
        connection.execute(
            "CREATE TABLE runs(run_id TEXT PRIMARY KEY,revision INTEGER NOT NULL,"
            "current_contract_version INTEGER NOT NULL) STRICT"
        )
        connection.execute("INSERT INTO runs VALUES('run-1',4,2)")
        ProductMigrationAssembler(ProductMigrationRegistry(Path("schema_fragments"))).apply(
            connection
        )
        connection.execute(
            "INSERT INTO research_catalog VALUES(?,?,?,?,?,?)",
            ("run-1", "Title", "Question", "owner", "[]", "2026-08-14T00:00:00Z"),
        )
        connection.execute(
            "INSERT INTO research_summary_projection VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "run-1", "OPEN", "RUNNING", "HISTORY_ONLY", "NOT_READY", "RESEARCH",
                "[]", "[]", '[{"action":"continue"}]', '{"remaining":1}',
                "2026-08-14T00:00:00Z", "running", 4, 2, 9, "a" * 64,
            ),
        )
    return OperationalQueries(
        db_path=db_path,
        deployment_id="deployment-1",
        fences=cast(OperationalFenceSource, Fences()),
        cursor_secret=b"operational-query-test-secret-32-bytes",
    )


SESSION = ProductSession("session-1", "subject-1", ())


def test_overview_reads_catalog_summary_under_run_fence(tmp_path: Path) -> None:
    result = adapter(tmp_path).execute(
        SESSION,
        QuerySpec({"kind": "RUN", "run_id": "run-1"}, "RESEARCH_OVERVIEW", {}),
    )
    assert result.data["title"] == "Title"
    actions = cast(tuple[dict[str, object], ...], result.data["available_actions_json"])
    assert actions == ({"action": "continue"},)
    assert result.fence["research_revision"] == 4


def test_missing_deployment_probe_is_not_reported_healthy(tmp_path: Path) -> None:
    with pytest.raises(DomainObjectNotFound):
        adapter(tmp_path).execute(
            SESSION,
            QuerySpec(
                {"kind": "DEPLOYMENT", "deployment_id": "deployment-1"},
                "DEPLOYMENT_STATUS",
                {},
            ),
        )
