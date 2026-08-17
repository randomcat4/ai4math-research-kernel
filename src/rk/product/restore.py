"""Crash-visible restore into a new data root with exact bundle verification."""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import sqlite3
import tempfile
import zipfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from rk.domain import ArtifactRef
from rk.product.backup import BackupArtifactReader, BackupError
from rk.product_release_migrations import ProductReleaseMigrationAssembler
from rk.runtime import format_utc
from rk.sqlite import open_sqlite

_ARCHIVE_PATH = re.compile(
    r"^(?:database\.sqlite|configuration/[A-Za-z0-9][A-Za-z0-9_.-]{0,127}|cas/[0-9a-f]{2}/[0-9a-f]{2}/[0-9a-f]{64})$"
)


class RestoreError(RuntimeError):
    """A bundle, target root, migration, index rebuild, or restored fence is invalid."""


@dataclass(frozen=True, slots=True)
class RestoreReceipt:
    restore_id: str
    source_backup_id: str
    deployment_id: str
    request_id: str
    new_data_root: Path
    restored_database_digest: str
    activity_cursor: int
    job_count: int
    checkpoint_count: int
    started_at: str
    finished_at: str


class RestoreRunner:
    def __init__(
        self,
        *,
        tracking_db_path: Path,
        artifact_reader: BackupArtifactReader,
        release: ProductReleaseMigrationAssembler,
        id_generator: Callable[[], str],
        clock: Callable[[], datetime],
        busy_timeout_ms: int = 5_000,
    ) -> None:
        self._tracking_db = Path(tracking_db_path)
        self._reader = artifact_reader
        self._release = release
        self._ids = id_generator
        self._clock = clock
        self._busy_timeout_ms = busy_timeout_ms

    def restore(
        self,
        *,
        source_backup_id: str,
        backup_artifact: ArtifactRef,
        deployment_id: str,
        request_id: str,
        new_data_root: Path,
    ) -> RestoreReceipt:
        if not source_backup_id or not deployment_id or not request_id:
            raise ValueError("restore identities are required")
        existing = self._successful(deployment_id, request_id, new_data_root)
        if existing is not None:
            return existing
        target = Path(new_data_root)
        if not target.is_absolute():
            raise RestoreError("restore target must be an absolute new data root")
        target = target.resolve()
        parent = target.parent
        if not parent.is_dir() or parent.is_symlink():
            raise RestoreError("restore target parent must be an existing regular directory")
        target_digest = hashlib.sha256(str(target).encode()).hexdigest()
        descriptor, bundle_name = tempfile.mkstemp(prefix=".rk-restore-bundle-", dir=parent)
        os.close(descriptor)
        bundle_path = Path(bundle_name)
        bundle_path.unlink()
        try:
            materialize = getattr(self._reader, "materialize", None)
            if callable(materialize):
                materialize(backup_artifact, bundle_path)
            else:
                # Compatibility for third-party v0.2 readers; the in-tree CAS
                # adapter always takes the streaming path above.
                bundle_path.write_bytes(self._reader.read_bytes(backup_artifact))
            if (
                bundle_path.stat().st_size != backup_artifact.byte_count
                or _sha256_file(bundle_path) != backup_artifact.sha256
            ):
                raise RestoreError("backup ArtifactRef does not match bundle bytes")
        except BaseException:
            bundle_path.unlink(missing_ok=True)
            raise
        restore_id, started_at = self._start(
            source_backup_id,
            deployment_id,
            request_id,
            target_digest,
        )
        staging = parent / f".rk-restore-{restore_id}.partial"
        try:
            manifest = self._extract_verified(bundle_path, staging)
            if manifest.get("backup_id") != source_backup_id:
                raise RestoreError("source backup identity differs from bundle manifest")
            database = staging / "database.sqlite"
            self._upgrade_and_rebuild(database)
            consistency = _consistency(database)
            expected = _object(manifest, "consistency")
            for key in ("activity_cursor", "job_count", "checkpoint_count", "terminal_job_count"):
                if consistency[key] != _integer(expected, key):
                    raise RestoreError(f"restored consistency fence differs: {key}")
            if target.exists():
                raise RestoreError("restore target ceased to be a new directory")
            os.replace(staging, target)
            _fsync_directory(parent)
            digest = _sha256_file(target / "database.sqlite")
            finished_at = format_utc(self._clock())
            self._finish_success(
                restore_id,
                digest,
                consistency,
                finished_at,
            )
            return RestoreReceipt(
                restore_id,
                source_backup_id,
                deployment_id,
                request_id,
                target,
                digest,
                consistency["activity_cursor"],
                consistency["job_count"],
                consistency["checkpoint_count"],
                started_at,
                finished_at,
            )
        except BaseException as error:
            if staging.exists():
                resolved = staging.resolve()
                if resolved.parent != parent or not resolved.name.startswith(".rk-restore-"):
                    raise RestoreError("restore staging path escaped its target parent") from error
                shutil.rmtree(resolved)
            self._finish_failure(
                restore_id, type(error).__name__.upper(), format_utc(self._clock())
            )
            if isinstance(error, (RestoreError, BackupError)):
                raise
            raise RestoreError("restore failed before atomic publication") from error
        finally:
            bundle_path.unlink(missing_ok=True)

    def _extract_verified(self, bundle_path: Path, staging: Path) -> dict[str, object]:
        if staging.exists():
            resolved = staging.resolve()
            if resolved.parent != staging.parent.resolve() or not resolved.name.startswith(
                ".rk-restore-"
            ):
                raise RestoreError("existing restore staging directory is unsafe")
            shutil.rmtree(resolved)
        staging.mkdir(mode=0o700)
        try:
            with zipfile.ZipFile(bundle_path) as bundle:
                names = set(bundle.namelist())
                if "manifest.json" not in names:
                    raise RestoreError("backup bundle has no manifest")
                manifest_value = json.loads(bundle.read("manifest.json"))
                if (
                    not isinstance(manifest_value, dict)
                    or manifest_value.get("schema_version") != "rk.product.backup-manifest.v1"
                ):
                    raise RestoreError("backup manifest schema is unsupported")
                entries = _manifest_entries(manifest_value)
                expected_names = {"manifest.json", *(str(item["archive_path"]) for item in entries)}
                if names != expected_names:
                    raise RestoreError("bundle members differ from the signed manifest")
                for entry in entries:
                    archive_path = str(entry["archive_path"])
                    if _ARCHIVE_PATH.fullmatch(archive_path) is None:
                        raise RestoreError("manifest archive path is unsafe")
                    target = (staging / archive_path).resolve()
                    if not target.is_relative_to(staging.resolve()):
                        raise RestoreError("bundle member escaped restore staging")
                    target.parent.mkdir(parents=True, exist_ok=True)
                    digest = hashlib.sha256()
                    count = 0
                    with bundle.open(archive_path) as source, target.open("xb") as writer:
                        while chunk := source.read(1024 * 1024):
                            count += len(chunk)
                            digest.update(chunk)
                            writer.write(chunk)
                        writer.flush()
                        os.fsync(writer.fileno())
                    if count != _integer(entry, "byte_count") or digest.hexdigest() != _string(
                        entry, "sha256"
                    ):
                        raise RestoreError(f"bundle member digest differs: {archive_path}")
                return manifest_value
        except (OSError, zipfile.BadZipFile, json.JSONDecodeError) as error:
            raise RestoreError("backup bundle is unreadable") from error

    def _upgrade_and_rebuild(self, database: Path) -> None:
        with open_sqlite(
            database, timeout=self._busy_timeout_ms / 1_000, isolation_level=None
        ) as connection:
            self._release.apply(connection)
            connection.execute("REINDEX")
            connection.execute("ANALYZE")
            if connection.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                raise RestoreError("restored database failed integrity_check")
            if connection.execute("PRAGMA foreign_key_check").fetchall():
                raise RestoreError("restored database failed foreign_key_check")
            connection.execute("PRAGMA wal_checkpoint(TRUNCATE)")

    def _start(
        self,
        source_backup_id: str,
        deployment_id: str,
        request_id: str,
        target_digest: str,
    ) -> tuple[str, str]:
        restore_id = self._ids()
        started_at = format_utc(self._clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT restore_id,state,started_at,target_root_digest "
                "FROM product_deployment_restores WHERE deployment_id=? AND request_id=?",
                (deployment_id, request_id),
            ).fetchone()
            if row is not None:
                if str(row[1]) == "RUNNING" and str(row[3]) == target_digest:
                    connection.commit()
                    return str(row[0]), str(row[2])
                raise RestoreError("restore request is already terminal or targets another root")
            connection.execute(
                "INSERT INTO product_deployment_restores("
                "restore_id,source_backup_id,deployment_id,request_id,target_root_digest,state,"
                "restored_database_digest,restored_activity_cursor,restored_job_count,"
                "restored_checkpoint_count,failure_code,started_at,finished_at"
                ") VALUES(?,?,?,?,?,'RUNNING',NULL,NULL,NULL,NULL,NULL,?,?)",
                (
                    restore_id,
                    source_backup_id,
                    deployment_id,
                    request_id,
                    target_digest,
                    started_at,
                    started_at,
                ),
            )
            connection.commit()
        return restore_id, started_at

    def _finish_success(
        self, restore_id: str, digest: str, consistency: Mapping[str, int], finished_at: str
    ) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                "UPDATE product_deployment_restores SET state='SUCCEEDED',"
                "restored_database_digest=?,restored_activity_cursor=?,restored_job_count=?,"
                "restored_checkpoint_count=?,failure_code=NULL,finished_at=? "
                "WHERE restore_id=? AND state='RUNNING'",
                (
                    digest,
                    consistency["activity_cursor"],
                    consistency["job_count"],
                    consistency["checkpoint_count"],
                    finished_at,
                    restore_id,
                ),
            ).rowcount
            if changed != 1:
                raise RestoreError("restore terminal receipt lost its RUNNING state")
            connection.commit()

    def _finish_failure(self, restore_id: str, failure_code: str, finished_at: str) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE product_deployment_restores SET state='FAILED',failure_code=?,"
                "finished_at=? WHERE restore_id=? AND state='RUNNING'",
                (failure_code, finished_at, restore_id),
            )
            connection.commit()

    def _successful(
        self, deployment_id: str, request_id: str, requested_root: Path
    ) -> RestoreReceipt | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT restore_id,source_backup_id,deployment_id,request_id,"
                "restored_database_digest,restored_activity_cursor,restored_job_count,"
                "restored_checkpoint_count,started_at,finished_at,target_root_digest "
                "FROM product_deployment_restores WHERE deployment_id=? AND request_id=? "
                "AND state='SUCCEEDED'",
                (deployment_id, request_id),
            ).fetchone()
        if row is None:
            return None
        root = Path(requested_root).resolve()
        if hashlib.sha256(str(root).encode()).hexdigest() != str(row[10]) or not root.is_dir():
            raise RestoreError("successful restore request is bound to another or missing root")
        return RestoreReceipt(
            str(row[0]),
            str(row[1]),
            str(row[2]),
            str(row[3]),
            root,
            str(row[4]),
            int(row[5]),
            int(row[6]),
            int(row[7]),
            str(row[8]),
            str(row[9]),
        )

    def _connect(self) -> sqlite3.Connection:
        connection = open_sqlite(
            self._tracking_db, timeout=self._busy_timeout_ms / 1_000, isolation_level=None
        )
        connection.execute("PRAGMA foreign_keys=ON")
        return connection


def _manifest_entries(manifest: Mapping[str, object]) -> tuple[Mapping[str, object], ...]:
    database = _object(manifest, "database")
    configuration = manifest.get("configuration")
    cas_objects = manifest.get("cas_objects")
    if not isinstance(configuration, list) or not all(
        isinstance(item, dict) for item in configuration
    ):
        raise RestoreError("configuration manifest is invalid")
    if not isinstance(cas_objects, list) or not all(isinstance(item, dict) for item in cas_objects):
        raise RestoreError("CAS manifest is invalid")
    return (database, *configuration, *cas_objects)


def _consistency(database: Path) -> dict[str, int]:
    terminal = ("CANCELLED", "SUCCEEDED", "FAILED", "OUTCOME_UNKNOWN", "STALE", "INVALIDATED")
    placeholders = ",".join("?" for _ in terminal)
    with open_sqlite(database) as connection:
        return {
            "activity_cursor": int(
                connection.execute(
                    "SELECT COALESCE(MAX(cursor),0) FROM product_activity_events"
                ).fetchone()[0]
            ),
            "job_count": int(connection.execute("SELECT COUNT(*) FROM product_jobs").fetchone()[0]),
            "checkpoint_count": int(
                connection.execute("SELECT COUNT(*) FROM product_job_checkpoints").fetchone()[0]
            ),
            "terminal_job_count": int(
                connection.execute(
                    f"SELECT COUNT(*) FROM product_jobs WHERE state IN ({placeholders})", terminal
                ).fetchone()[0]
            ),
        }


def _object(value: Mapping[str, object], key: str) -> Mapping[str, object]:
    item = value.get(key)
    if not isinstance(item, dict):
        raise RestoreError(f"backup manifest field is not an object: {key}")
    return item


def _string(value: Mapping[str, object], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise RestoreError(f"backup manifest string is invalid: {key}")
    return item


def _integer(value: Mapping[str, object], key: str) -> int:
    item = value.get(key)
    if not isinstance(item, int) or isinstance(item, bool) or item < 0:
        raise RestoreError(f"backup manifest integer is invalid: {key}")
    return item


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as reader:
        while chunk := reader.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)


__all__ = ["RestoreError", "RestoreReceipt", "RestoreRunner"]
