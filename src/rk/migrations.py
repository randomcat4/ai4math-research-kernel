"""Deterministic, drift-refusing SQLite schema migrations."""

from __future__ import annotations

import hashlib
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

_FILE_RE = re.compile(r"^(?P<version>[0-9]+)\.sql$")
_NAME_RE = re.compile(r"^-- migration-name: (?P<name>[A-Za-z0-9_]+)$", re.MULTILINE)


class MigrationError(RuntimeError):
    """Base error for migration discovery, execution, or verification failures."""


class MigrationDriftError(MigrationError):
    """An applied migration no longer matches the immutable file on disk."""


class UnmanagedDatabaseError(MigrationError):
    """The database contains state without a trustworthy migration ledger."""


@dataclass(frozen=True, slots=True)
class Migration:
    version: int
    name: str
    path: Path
    sha256: str
    sql: str


@dataclass(frozen=True, slots=True)
class AppliedMigration:
    version: int
    name: str
    sha256: str
    applied_at: str


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class MigrationRunner:
    """Apply numbered SQL files and bind each version to its exact file digest.

    Migration SQL owns its schema transaction. The ledger row is deliberately written in a
    second ``BEGIN IMMEDIATE`` transaction, matching the frozen schema contract. If a process
    dies between those commits, the next startup refuses the unledgered database rather than
    guessing that a partly matching schema is safe.
    """

    def __init__(
        self,
        db_path: Path,
        migrations_dir: Path,
        busy_timeout_ms: int,
        *,
        now: Callable[[], str] = _utc_now,
        after_schema_commit: Callable[[Migration], None] | None = None,
        minimum_sqlite: tuple[int, int, int] = (3, 45, 0),
    ) -> None:
        if busy_timeout_ms <= 0:
            raise ValueError("busy_timeout_ms must be positive")
        self._db_path = Path(db_path)
        self._migrations_dir = Path(migrations_dir)
        self._busy_timeout_ms = busy_timeout_ms
        self._now = now
        self._after_schema_commit = after_schema_commit
        self._minimum_sqlite = minimum_sqlite

    def discover(self) -> tuple[Migration, ...]:
        if not self._migrations_dir.is_dir():
            raise MigrationError("migration directory does not exist")
        migrations: list[Migration] = []
        for path in sorted(self._migrations_dir.iterdir(), key=lambda item: item.name):
            if not path.is_file():
                continue
            match = _FILE_RE.fullmatch(path.name)
            if path.suffix == ".sql" and match is None:
                raise MigrationError(f"invalid migration filename: {path.name}")
            if match is None:
                continue
            raw = path.read_bytes()
            try:
                sql = raw.decode("utf-8")
            except UnicodeDecodeError as exc:
                raise MigrationError(f"migration is not UTF-8: {path.name}") from exc
            if sql.startswith("\ufeff"):
                raise MigrationError(f"migration must not contain a UTF-8 BOM: {path.name}")
            name_match = _NAME_RE.search(sql)
            name = name_match.group("name") if name_match else path.stem
            migrations.append(
                Migration(
                    version=int(match.group("version")),
                    name=name,
                    path=path,
                    sha256=hashlib.sha256(raw).hexdigest(),
                    sql=sql,
                )
            )
        if not migrations:
            raise MigrationError("no migration files found")
        versions = [migration.version for migration in migrations]
        if versions != list(range(1, len(migrations) + 1)):
            raise MigrationError("migration versions must be contiguous and start at 1")
        if len({migration.name for migration in migrations}) != len(migrations):
            raise MigrationError("migration names must be unique")
        return tuple(migrations)

    def migrate(self) -> tuple[AppliedMigration, ...]:
        self._require_supported_sqlite()
        migrations = self.discover()
        self._db_path.parent.mkdir(parents=True, exist_ok=True)
        connection = self._connect()
        try:
            applied = self._read_applied(connection)
            objects = self._user_tables(connection)
            if applied is None:
                if objects:
                    raise UnmanagedDatabaseError(
                        "database has user tables but no schema_migrations ledger"
                    )
                applied_by_version: dict[int, AppliedMigration] = {}
            else:
                applied_by_version = {item.version: item for item in applied}
                if not applied and objects - {"schema_migrations"}:
                    raise UnmanagedDatabaseError(
                        "database schema committed without a migration ledger entry"
                    )
            self._verify_applied(migrations, applied_by_version)
            for migration in migrations:
                if migration.version in applied_by_version:
                    continue
                self._apply_one(connection, migration)
                item = AppliedMigration(
                    version=migration.version,
                    name=migration.name,
                    sha256=migration.sha256,
                    applied_at=self._last_applied_at(connection, migration.version),
                )
                applied_by_version[migration.version] = item
            self._check_database(connection)
            return tuple(applied_by_version[version] for version in sorted(applied_by_version))
        finally:
            connection.close()

    def verify(self) -> tuple[AppliedMigration, ...]:
        self._require_supported_sqlite()
        migrations = self.discover()
        if not self._db_path.exists() or self._db_path.stat().st_size == 0:
            return ()
        connection = self._connect()
        try:
            applied = self._read_applied(connection)
            if applied is None:
                if self._user_tables(connection):
                    raise UnmanagedDatabaseError(
                        "database has user tables but no schema_migrations ledger"
                    )
                return ()
            if not applied and self._user_tables(connection) - {"schema_migrations"}:
                raise UnmanagedDatabaseError(
                    "database schema committed without a migration ledger entry"
                )
            by_version = {item.version: item for item in applied}
            self._verify_applied(migrations, by_version)
            self._check_database(connection)
            return applied
        finally:
            connection.close()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self._db_path,
            timeout=self._busy_timeout_ms / 1_000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
        return connection

    @staticmethod
    def _read_applied(connection: sqlite3.Connection) -> tuple[AppliedMigration, ...] | None:
        exists = connection.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name='schema_migrations'"
        ).fetchone()
        if exists is None:
            return None
        rows = connection.execute(
            "SELECT version, name, sha256, applied_at FROM schema_migrations ORDER BY version"
        ).fetchall()
        return tuple(
            AppliedMigration(
                version=int(row["version"]),
                name=str(row["name"]),
                sha256=str(row["sha256"]),
                applied_at=str(row["applied_at"]),
            )
            for row in rows
        )

    @staticmethod
    def _user_tables(connection: sqlite3.Connection) -> set[str]:
        return {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        }

    @staticmethod
    def _verify_applied(
        migrations: tuple[Migration, ...],
        applied: dict[int, AppliedMigration],
    ) -> None:
        available = {migration.version: migration for migration in migrations}
        for version, recorded in applied.items():
            migration = available.get(version)
            if migration is None:
                raise MigrationDriftError(f"applied migration file is missing: {version}")
            if recorded.name != migration.name or recorded.sha256 != migration.sha256:
                raise MigrationDriftError(f"applied migration has drifted: {version}")
        if applied and sorted(applied) != list(range(1, max(applied) + 1)):
            raise MigrationDriftError("migration ledger contains a version gap")

    def _apply_one(self, connection: sqlite3.Connection, migration: Migration) -> None:
        try:
            connection.executescript(migration.sql)
        except sqlite3.DatabaseError as exc:
            raise MigrationError(f"migration {migration.version} failed") from exc
        if self._after_schema_commit is not None:
            self._after_schema_commit(migration)
        applied_at = self._now()
        try:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO schema_migrations(version, name, sha256, applied_at) "
                "VALUES (?, ?, ?, ?)",
                (migration.version, migration.name, migration.sha256, applied_at),
            )
            connection.commit()
        except BaseException:
            connection.rollback()
            raise

    @staticmethod
    def _last_applied_at(connection: sqlite3.Connection, version: int) -> str:
        row = connection.execute(
            "SELECT applied_at FROM schema_migrations WHERE version = ?", (version,)
        ).fetchone()
        if row is None:
            raise MigrationError(f"migration ledger write missing: {version}")
        return str(row[0])

    @staticmethod
    def _check_database(connection: sqlite3.Connection) -> None:
        integrity = connection.execute("PRAGMA integrity_check").fetchone()
        if integrity is None or integrity[0] != "ok":
            raise MigrationError("SQLite integrity_check failed")
        foreign_keys = connection.execute("PRAGMA foreign_key_check").fetchall()
        if foreign_keys:
            raise MigrationError("SQLite foreign_key_check failed")

    def _require_supported_sqlite(self) -> None:
        if sqlite3.sqlite_version_info < self._minimum_sqlite:
            required = ".".join(str(item) for item in self._minimum_sqlite)
            raise MigrationError(f"SQLite {required} or newer is required")
