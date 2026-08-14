import sqlite3
from pathlib import Path

import pytest

from rk.product_migrations import (
    ProductMigrationAssembler,
    ProductMigrationError,
    ProductMigrationRegistry,
)

PROJECT_FRAGMENTS = Path(__file__).parents[1] / "schema_fragments"


def _write(root: Path, package: str, slug: str, sql: str) -> Path:
    directory = root / package
    directory.mkdir(parents=True, exist_ok=True)
    path = directory / f"{slug}.sql"
    path.write_text(sql, encoding="utf-8")
    return path


def test_project_baseline_has_stable_identity_and_owned_objects() -> None:
    plan = ProductMigrationRegistry(PROJECT_FRAGMENTS).plan()

    assert [(step.position, step.fragment.fragment_id) for step in plan] == [
        (1, "D00a/product_activity"),
        (2, "B01b/catalog"),
        (3, "B02a/operations"),
        (4, "B02b/activity_retention"),
        (5, "B03/jobs"),
        (6, "B04a/upload"),
        (7, "B05a/identity"),
    ]
    assert {(item.kind, item.name) for item in plan[0].fragment.objects} == {
        ("TABLE", "product_activity_events"),
        ("INDEX", "product_activity_run_cursor"),
        ("INDEX", "product_activity_deployment_cursor"),
    }


def test_plan_is_deterministic_by_package_and_slug_not_creation_order(tmp_path: Path) -> None:
    _write(tmp_path, "B02a", "zeta", "CREATE TABLE zeta(id INTEGER PRIMARY KEY) STRICT;")
    _write(tmp_path, "B01a", "catalog", "CREATE TABLE catalog(id TEXT PRIMARY KEY) STRICT;")
    _write(tmp_path, "B02a", "alpha", "CREATE VIEW alpha AS SELECT id FROM zeta;")

    first = ProductMigrationRegistry(tmp_path).plan()
    second = ProductMigrationRegistry(tmp_path).plan()

    assert [(step.position, step.fragment.fragment_id) for step in first] == [
        (1, "B01a/catalog"),
        (2, "B02a/alpha"),
        (3, "B02a/zeta"),
    ]
    assert first == second


@pytest.mark.parametrize(
    ("package", "slug", "sql", "message"),
    [
        ("B01a", "0001_catalog", "CREATE TABLE x(id INTEGER);", "numbered fragment slug"),
        (
            "B01a",
            "catalog",
            "-- migration-name: package_owned\nCREATE TABLE x(id INTEGER);",
            "release numbering",
        ),
        (
            "B01a",
            "catalog",
            "PRAGMA user_version=17; CREATE TABLE x(id INTEGER);",
            "release numbering",
        ),
        ("B01a", "catalog", "BEGIN; CREATE TABLE x(id INTEGER); COMMIT;", "assembly transaction"),
    ],
)
def test_business_fragment_cannot_claim_numbering_or_transaction(
    tmp_path: Path, package: str, slug: str, sql: str, message: str
) -> None:
    _write(tmp_path, package, slug, sql)

    with pytest.raises(ProductMigrationError, match=message):
        ProductMigrationRegistry(tmp_path).discover()


@pytest.mark.parametrize("kind", ["TABLE", "INDEX", "TRIGGER", "VIEW"])
def test_schema_object_conflicts_are_rejected_case_insensitively(tmp_path: Path, kind: str) -> None:
    if kind == "TABLE":
        first = "CREATE TABLE Shared(id INTEGER PRIMARY KEY) STRICT;"
        second = "CREATE TABLE shared(value TEXT) STRICT;"
    elif kind == "INDEX":
        first = "CREATE TABLE one(id INTEGER); CREATE INDEX Shared ON one(id);"
        second = "CREATE TABLE two(id INTEGER); CREATE INDEX shared ON two(id);"
    elif kind == "TRIGGER":
        first = (
            "CREATE TABLE one(id INTEGER);\n"
            "CREATE TRIGGER Shared AFTER INSERT ON one\nBEGIN\nSELECT 1;\nEND;"
        )
        second = (
            "CREATE TABLE two(id INTEGER);\n"
            "CREATE TRIGGER shared AFTER INSERT ON two\nBEGIN\nSELECT 1;\nEND;"
        )
    else:
        first = "CREATE TABLE one(id INTEGER); CREATE VIEW Shared AS SELECT id FROM one;"
        second = "CREATE TABLE two(id INTEGER); CREATE VIEW shared AS SELECT id FROM two;"
    _write(tmp_path, "B01a", "first", first)
    _write(tmp_path, "B02a", "second", second)

    with pytest.raises(ProductMigrationError, match=f"schema object {kind}:"):
        ProductMigrationRegistry(tmp_path).discover()


def test_if_not_exists_cannot_mask_ownership_conflict(tmp_path: Path) -> None:
    _write(tmp_path, "B01a", "catalog", "CREATE TABLE IF NOT EXISTS catalog(id INTEGER);")

    with pytest.raises(ProductMigrationError, match="masks an object conflict"):
        ProductMigrationRegistry(tmp_path).discover()


def test_real_sqlite_empty_database_assembly_is_atomic_and_idempotent(tmp_path: Path) -> None:
    db_path = tmp_path / "product.sqlite"
    with sqlite3.connect(db_path, isolation_level=None) as connection:
        assembler = ProductMigrationAssembler(ProductMigrationRegistry(PROJECT_FRAGMENTS))
        first = assembler.apply(connection)
        second = assembler.apply(connection)
        assert first == second
        assert first[0].package == "D00a"
        assert connection.execute("PRAGMA foreign_keys").fetchone() == (1,)
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("SELECT COUNT(*) FROM product_schema_fragments").fetchone() == (
            7,
        )

        connection.execute(
            "INSERT INTO product_activity_events("
            "event_id,scope_kind,run_id,deployment_id,source,research_revision,"
            "kernel_event_id,entity_refs,payload_json,recorded_at) "
            "VALUES ('event-1','RUN','run-1',NULL,'KERNEL',3,NULL,'[]','{}','now')"
        )
        assert connection.execute(
            "SELECT cursor,run_id FROM product_activity_events"
        ).fetchone() == (1, "run-1")


def test_existing_database_object_conflict_rolls_back_registry_and_prior_steps(
    tmp_path: Path,
) -> None:
    fragments = tmp_path / "fragments"
    _write(fragments, "B01a", "first", "CREATE TABLE assembled(id INTEGER) STRICT;")
    _write(fragments, "B02a", "second", "CREATE TABLE occupied(id INTEGER) STRICT;")
    with sqlite3.connect(tmp_path / "state.sqlite", isolation_level=None) as connection:
        connection.execute("CREATE TABLE occupied(original TEXT) STRICT")

        with pytest.raises(ProductMigrationError, match="assembly failed"):
            ProductMigrationAssembler(ProductMigrationRegistry(fragments)).apply(connection)

        names = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        assert names == {"occupied"}
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)


def test_applied_digest_drift_is_rejected_and_database_is_unchanged(tmp_path: Path) -> None:
    fragments = tmp_path / "fragments"
    path = _write(fragments, "B01a", "catalog", "CREATE TABLE catalog(id INTEGER) STRICT;")
    db_path = tmp_path / "state.sqlite"
    with sqlite3.connect(db_path, isolation_level=None) as connection:
        assembler = ProductMigrationAssembler(ProductMigrationRegistry(fragments))
        assembler.apply(connection)
        path.write_text("CREATE TABLE catalog(id INTEGER, label TEXT) STRICT;", encoding="utf-8")

        with pytest.raises(ProductMigrationError, match="has drifted"):
            assembler.apply(connection)

        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("PRAGMA table_info(catalog)").fetchall()[0][1] == "id"


def test_scope_check_rejects_ambiguous_activity_scope(tmp_path: Path) -> None:
    with sqlite3.connect(tmp_path / "product.sqlite", isolation_level=None) as connection:
        ProductMigrationAssembler(ProductMigrationRegistry(PROJECT_FRAGMENTS)).apply(connection)

        with pytest.raises(sqlite3.IntegrityError, match="CHECK constraint failed"):
            connection.execute(
                "INSERT INTO product_activity_events("
                "event_id,scope_kind,run_id,deployment_id,source,entity_refs,"
                "payload_json,recorded_at) "
                "VALUES ('bad','RUN',NULL,NULL,'HOST','[]','{}','now')"
            )
