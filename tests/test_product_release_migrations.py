from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from pathlib import Path

import pytest

from rk.product_migrations import (
    ProductMigrationAssembler,
    ProductMigrationError,
    ProductMigrationRegistry,
)
from rk.product_release_migrations import (
    ProductReleaseMigrationAssembler,
    ProductReleaseMigrationError,
)

ROOT = Path(__file__).parents[1]
FRAGMENTS = ROOT / "schema_fragments"
MANIFEST = ROOT / "docs/spec/product/migration-manifest.json"
LOCK = ROOT / "migrations/release/current.lock"


def _release(root: Path, manifest: Path, lock: Path) -> ProductReleaseMigrationAssembler:
    return ProductReleaseMigrationAssembler(
        fragment_root=root,
        manifest_path=manifest,
        lock_path=lock,
    )


def _write_fragment(root: Path, package: str, slug: str, sql: str) -> None:
    target = root / package
    target.mkdir(parents=True, exist_ok=True)
    (target / f"{slug}.sql").write_text(sql, encoding="utf-8")


def _write_manifest(
    root: Path,
    manifest: Path,
    lock: Path,
    order: tuple[str, ...],
) -> None:
    fragments = {item.fragment_id: item for item in ProductMigrationRegistry(root).discover()}
    document = {
        "schema_version": "rk.product.migration-manifest.v1",
        "release_id": "test-release",
        "product_version": "RK-PRODUCT-1.1",
        "release_status": "BACKEND_ONLY",
        "sealed": False,
        "source_registry": "schema_fragments",
        "fragments": [
            {
                "release_position": position,
                "fragment_id": fragment_id,
                "sha256": fragments[fragment_id].sha256,
            }
            for position, fragment_id in enumerate(order, 1)
        ],
    }
    raw = (json.dumps(document, indent=2) + "\n").encode()
    manifest.write_bytes(raw)
    lock.write_text(hashlib.sha256(raw).hexdigest() + "\n", encoding="ascii")


def test_project_release_manifest_empty_install_and_repeat_are_exact(
    tmp_path: Path,
) -> None:
    release = _release(FRAGMENTS, MANIFEST, LOCK)
    manifest = release.manifest()
    assert manifest.release_status == "BACKEND_ONLY"
    assert manifest.sealed is False
    assert len(manifest.fragments) == 30
    assert manifest.fragments[-2].fragment_id == "B18/research_lineage"
    assert manifest.fragments[-1].fragment_id == "B17/problem_pool"

    db = tmp_path / "empty.sqlite"
    with sqlite3.connect(db, isolation_level=None) as connection:
        first = release.apply(connection)
        second = release.apply(connection)
        assert first == second
        assert len(first) == 30
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []


def test_release_upgrades_the_previous_29_fragment_database_atomically(
    tmp_path: Path,
) -> None:
    old_root = tmp_path / "old-fragments"
    shutil.copytree(FRAGMENTS, old_root)
    (old_root / "B17" / "problem_pool.sql").unlink()
    (old_root / "B17").rmdir()
    db = tmp_path / "current.sqlite"
    with sqlite3.connect(db, isolation_level=None) as connection:
        ProductMigrationAssembler(ProductMigrationRegistry(old_root)).apply(connection)
        assert connection.execute(
            "SELECT package,assembly_position FROM product_schema_fragments "
            "ORDER BY assembly_position DESC LIMIT 1"
        ).fetchone() == ("B18", 29)

        result = _release(FRAGMENTS, MANIFEST, LOCK).apply(connection)
        assert len(result) == 30
        assert connection.execute(
            "SELECT package,assembly_position FROM product_schema_fragments "
            "ORDER BY assembly_position DESC LIMIT 1"
        ).fetchone() == ("B17", 30)
        assert connection.execute(
            "SELECT name FROM sqlite_master WHERE name='product_problem_pools'"
        ).fetchone() == ("product_problem_pools",)


def test_release_failure_rolls_back_all_prior_fragments(tmp_path: Path) -> None:
    fragments = tmp_path / "fragments"
    _write_fragment(
        fragments,
        "B01a",
        "first",
        "CREATE TABLE release_first(id INTEGER PRIMARY KEY) STRICT;",
    )
    _write_fragment(
        fragments,
        "B02a",
        "second",
        "CREATE TABLE release_second(id INTEGER PRIMARY KEY) STRICT;"
        "INSERT INTO absent_table VALUES(1);",
    )
    manifest, lock = tmp_path / "manifest.json", tmp_path / "manifest.lock"
    _write_manifest(
        fragments,
        manifest,
        lock,
        ("B01a/first", "B02a/second"),
    )
    with sqlite3.connect(tmp_path / "failed.sqlite", isolation_level=None) as connection:
        with pytest.raises(ProductMigrationError, match="assembly failed"):
            _release(fragments, manifest, lock).apply(connection)
        names = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }
        assert names == set()


def test_release_rejects_lock_fragment_digest_and_installed_order_drift(
    tmp_path: Path,
) -> None:
    fragments = tmp_path / "fragments"
    _write_fragment(
        fragments,
        "B01a",
        "alpha",
        "CREATE TABLE release_alpha(id INTEGER PRIMARY KEY) STRICT;",
    )
    _write_fragment(
        fragments,
        "B02a",
        "beta",
        "CREATE TABLE release_beta(id INTEGER PRIMARY KEY) STRICT;",
    )
    manifest, lock = tmp_path / "manifest.json", tmp_path / "manifest.lock"
    _write_manifest(
        fragments,
        manifest,
        lock,
        ("B01a/alpha", "B02a/beta"),
    )
    release = _release(fragments, manifest, lock)
    db = tmp_path / "state.sqlite"
    with sqlite3.connect(db, isolation_level=None) as connection:
        release.apply(connection)
        before = connection.execute(
            "SELECT package,slug,sha256,assembly_position FROM product_schema_fragments "
            "ORDER BY assembly_position"
        ).fetchall()

        _write_manifest(
            fragments,
            manifest,
            lock,
            ("B02a/beta", "B01a/alpha"),
        )
        with pytest.raises(ProductMigrationError, match="has drifted"):
            _release(fragments, manifest, lock).apply(connection)
        assert (
            connection.execute(
                "SELECT package,slug,sha256,assembly_position FROM product_schema_fragments "
                "ORDER BY assembly_position"
            ).fetchall()
            == before
        )

        _write_manifest(
            fragments,
            manifest,
            lock,
            ("B01a/alpha", "B02a/beta"),
        )
        (fragments / "B01a" / "alpha.sql").write_text(
            "CREATE TABLE release_alpha(id INTEGER PRIMARY KEY, changed TEXT) STRICT;",
            encoding="utf-8",
        )
        with pytest.raises(ProductReleaseMigrationError, match="digest has drifted"):
            _release(fragments, manifest, lock).apply(connection)
        lock.write_text("0" * 64 + "\n", encoding="ascii")
        with pytest.raises(ProductReleaseMigrationError, match="lock digest differs"):
            _release(fragments, manifest, lock).plan()
