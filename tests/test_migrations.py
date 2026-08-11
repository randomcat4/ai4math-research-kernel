import hashlib
import shutil
import sqlite3
from pathlib import Path

import pytest

from rk.migrations import (
    MigrationDriftError,
    MigrationRunner,
    UnmanagedDatabaseError,
)

SOURCE_MIGRATIONS = Path(__file__).parents[1] / "migrations"


def _copy_migrations(target: Path) -> Path:
    destination = target / "migrations"
    destination.mkdir()
    shutil.copy2(SOURCE_MIGRATIONS / "0001.sql", destination / "0001.sql")
    return destination


def test_empty_database_migrates_and_restart_is_idempotent(tmp_path: Path) -> None:
    migrations = _copy_migrations(tmp_path)
    db_path = tmp_path / "state" / "rk.sqlite"
    runner = MigrationRunner(
        db_path,
        migrations,
        1_250,
        now=lambda: "2026-08-11T12:00:00.000Z",
    )

    first = runner.migrate()
    second = runner.migrate()
    verified = runner.verify()

    expected = hashlib.sha256((migrations / "0001.sql").read_bytes()).hexdigest()
    assert first == second == verified
    assert first[0].sha256 == expected
    assert first[0].name == "rk_v1_initial"
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone() == (1,)


def test_applied_file_drift_is_refused(tmp_path: Path) -> None:
    migrations = _copy_migrations(tmp_path)
    runner = MigrationRunner(tmp_path / "rk.sqlite", migrations, 500)
    runner.migrate()
    (migrations / "0001.sql").write_text(
        (migrations / "0001.sql").read_text(encoding="utf-8") + "\n-- drift\n",
        encoding="utf-8",
    )

    with pytest.raises(MigrationDriftError, match="drifted"):
        runner.verify()
    with pytest.raises(MigrationDriftError, match="drifted"):
        runner.migrate()


def test_crash_after_schema_commit_is_never_silently_adopted(tmp_path: Path) -> None:
    migrations = _copy_migrations(tmp_path)
    db_path = tmp_path / "rk.sqlite"

    def crash(_migration: object) -> None:
        raise RuntimeError("simulated process loss")

    with pytest.raises(RuntimeError, match="simulated"):
        MigrationRunner(
            db_path,
            migrations,
            500,
            after_schema_commit=crash,
        ).migrate()

    with sqlite3.connect(db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM schema_migrations"
        ).fetchone() == (0,)
        assert connection.execute(
            "SELECT COUNT(*) FROM sqlite_master WHERE type='table' AND name='runs'"
        ).fetchone() == (1,)
    with pytest.raises(UnmanagedDatabaseError, match="without a migration ledger"):
        MigrationRunner(db_path, migrations, 500).migrate()


def test_unmanaged_nonempty_database_is_refused(tmp_path: Path) -> None:
    migrations = _copy_migrations(tmp_path)
    db_path = tmp_path / "rk.sqlite"
    with sqlite3.connect(db_path) as connection:
        connection.execute("CREATE TABLE foreign_state(value TEXT)")

    with pytest.raises(UnmanagedDatabaseError, match="no schema_migrations"):
        MigrationRunner(db_path, migrations, 500).migrate()
