"""Online SQLite backups with immutable CAS bundles and exact restore manifests."""

from __future__ import annotations

import hashlib
import io
import json
import re
import sqlite3
import tempfile
import zipfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from rk.cas import ContentAddressedStore
from rk.domain import ArtifactInput, ArtifactRef
from rk.product.artifact_upload import ArtifactRegistry
from rk.runtime import format_utc

_LOGICAL_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_CAS_RELPATH = re.compile(r"^[0-9a-f]{2}/[0-9a-f]{2}/[0-9a-f]{64}$")
_TERMINAL_JOBS = (
    "CANCELLED",
    "SUCCEEDED",
    "FAILED",
    "OUTCOME_UNKNOWN",
    "STALE",
    "INVALIDATED",
)


class BackupError(RuntimeError):
    """The online snapshot, referenced CAS set, or immutable bundle is invalid."""


@dataclass(frozen=True, slots=True)
class BackupReceipt:
    backup_id: str
    deployment_id: str
    request_id: str
    artifact: ArtifactRef
    manifest_digest: str
    activity_cursor: int
    job_count: int
    checkpoint_count: int
    terminal_job_count: int
    created_at: str
    completed_at: str


class BackupArtifactReader(Protocol):
    def read_bytes(self, artifact: ArtifactRef) -> bytes: ...


class CasBackupArtifactReader:
    def __init__(self, cas: ContentAddressedStore) -> None:
        self._cas = cas

    def read_bytes(self, artifact: ArtifactRef) -> bytes:
        data = self._cas.read_bytes(artifact.artifact_id)
        if len(data) != artifact.byte_count or hashlib.sha256(data).hexdigest() != artifact.sha256:
            raise BackupError("backup ArtifactRef differs from canonical CAS bytes")
        return data


class BackupService:
    """Create restart-idempotent bundles from one SQLite snapshot.

    The SQLite online-backup API establishes the database fence.  Only COMMITTED CAS
    objects referenced by that snapshot are copied, so the bundle never mixes a later
    artifact mapping with an earlier database.
    """

    def __init__(
        self,
        *,
        db_path: Path,
        cas_root: Path,
        work_root: Path,
        cas: ContentAddressedStore,
        registry: ArtifactRegistry,
        id_generator: Callable[[], str],
        clock: Callable[[], datetime],
        busy_timeout_ms: int = 5_000,
    ) -> None:
        self._db_path = Path(db_path).resolve()
        self._cas_root = Path(cas_root).resolve()
        self._work_root = Path(work_root).resolve()
        self._cas = cas
        self._registry = registry
        self._ids = id_generator
        self._clock = clock
        self._busy_timeout_ms = busy_timeout_ms
        self._work_root.mkdir(parents=True, exist_ok=True)

    def create(
        self,
        *,
        deployment_id: str,
        request_id: str,
        include_cas: bool,
        include_configuration: bool,
        configuration_files: Mapping[str, Path],
    ) -> BackupReceipt:
        if not deployment_id or not request_id:
            raise ValueError("deployment_id and request_id are required")
        if bool(configuration_files) != include_configuration:
            raise BackupError("configuration inclusion and file manifest differ")
        existing = self._by_request(deployment_id, request_id)
        if existing is not None:
            return existing
        created_at = format_utc(self._clock())
        backup_id = self._ids()
        with tempfile.TemporaryDirectory(prefix="rk-backup-", dir=self._work_root) as raw:
            workspace = Path(raw)
            snapshot = workspace / "database.sqlite"
            self._online_snapshot(snapshot)
            consistency = _consistency(snapshot)
            database_entry = _entry(snapshot)
            configs = self._configuration_entries(configuration_files)
            cas_entries = self._cas_entries(snapshot) if include_cas else ()
            manifest = {
                "schema_version": "rk.product.backup-manifest.v1",
                "backup_id": backup_id,
                "deployment_id": deployment_id,
                "created_at": created_at,
                "include_cas": include_cas,
                "include_configuration": include_configuration,
                "database": {"archive_path": "database.sqlite", **database_entry},
                "configuration": [item[0] for item in configs],
                "cas_objects": [item[0] for item in cas_entries],
                "consistency": consistency,
            }
            manifest_bytes = _canonical_json(manifest)
            manifest_digest = hashlib.sha256(manifest_bytes).hexdigest()
            archive = workspace / "backup.rkzip"
            with zipfile.ZipFile(archive, "w", compression=zipfile.ZIP_STORED) as bundle:
                bundle.writestr("manifest.json", manifest_bytes)
                bundle.write(snapshot, "database.sqlite")
                for entry, source in configs:
                    bundle.write(source, str(entry["archive_path"]))
                for entry, source in cas_entries:
                    bundle.write(source, str(entry["archive_path"]))
            archive_digest = _sha256_file(archive)
            staged = self._cas.stage_input(
                ArtifactInput(
                    name=f"{backup_id}.rkzip",
                    path=str(archive),
                    sha256=archive_digest,
                    byte_count=archive.stat().st_size,
                    media_type="application/vnd.rk.backup+zip",
                )
            )
            committed = self._cas.commit(staged, now=self._clock())
            artifact = self._registry.register(committed)
            _require_artifact(artifact, archive_digest, archive.stat().st_size)
        completed_at = format_utc(self._clock())
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "INSERT INTO product_backups("
                    "backup_id,deployment_id,request_id,state,include_cas,include_configuration,"
                    "backup_artifact_id,backup_digest,manifest_digest,activity_cursor,job_count,"
                    "checkpoint_count,terminal_job_count,created_at,completed_at"
                    ") VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        backup_id,
                        deployment_id,
                        request_id,
                        "SUCCEEDED",
                        int(include_cas),
                        int(include_configuration),
                        artifact.artifact_id,
                        artifact.sha256,
                        manifest_digest,
                        int(consistency["activity_cursor"]),
                        int(consistency["job_count"]),
                        int(consistency["checkpoint_count"]),
                        int(consistency["terminal_job_count"]),
                        created_at,
                        completed_at,
                    ),
                )
                connection.commit()
        except sqlite3.IntegrityError:
            winner = self._by_request(deployment_id, request_id)
            if winner is None:
                raise
            return winner
        return BackupReceipt(
            backup_id,
            deployment_id,
            request_id,
            artifact,
            manifest_digest,
            int(consistency["activity_cursor"]),
            int(consistency["job_count"]),
            int(consistency["checkpoint_count"]),
            int(consistency["terminal_job_count"]),
            created_at,
            completed_at,
        )

    def get(self, backup_id: str) -> BackupReceipt:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT backup_id,deployment_id,request_id,backup_artifact_id,backup_digest,"
                "manifest_digest,activity_cursor,job_count,checkpoint_count,terminal_job_count,"
                "created_at,completed_at FROM product_backups WHERE backup_id=?",
                (backup_id,),
            ).fetchone()
            if row is None:
                raise KeyError(backup_id)
            artifact_row = connection.execute(
                "SELECT artifact_id,sha256,byte_count,media_type FROM artifacts "
                "WHERE artifact_id=? AND ingest_state='COMMITTED'",
                (row[3],),
            ).fetchone()
        if artifact_row is None or str(artifact_row[1]) != str(row[4]):
            raise BackupError("backup row is not bound to a committed canonical artifact")
        return _receipt(row, artifact_row)

    def _by_request(self, deployment_id: str, request_id: str) -> BackupReceipt | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT backup_id FROM product_backups WHERE deployment_id=? AND request_id=?",
                (deployment_id, request_id),
            ).fetchone()
        return self.get(str(row[0])) if row is not None else None

    def _online_snapshot(self, target: Path) -> None:
        source = sqlite3.connect(
            self._db_path, timeout=self._busy_timeout_ms / 1_000, isolation_level=None
        )
        destination = sqlite3.connect(target, isolation_level=None)
        try:
            source.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms}")
            source.backup(destination, pages=256)
            if destination.execute("PRAGMA integrity_check").fetchone() != ("ok",):
                raise BackupError("online SQLite snapshot failed integrity_check")
            if destination.execute("PRAGMA foreign_key_check").fetchall():
                raise BackupError("online SQLite snapshot failed foreign_key_check")
        finally:
            destination.close()
            source.close()

    def _configuration_entries(
        self, files: Mapping[str, Path]
    ) -> tuple[tuple[dict[str, object], Path], ...]:
        result: list[tuple[dict[str, object], Path]] = []
        for logical_name, raw_path in sorted(files.items()):
            if _LOGICAL_NAME.fullmatch(logical_name) is None:
                raise BackupError("configuration logical name is invalid")
            path = Path(raw_path)
            if not path.is_absolute() or path.is_symlink() or not path.is_file():
                raise BackupError("configuration source must be an absolute regular file")
            result.append(
                (
                    {
                        "logical_name": logical_name,
                        "archive_path": f"configuration/{logical_name}",
                        **_entry(path),
                    },
                    path,
                )
            )
        return tuple(result)

    def _cas_entries(self, snapshot: Path) -> tuple[tuple[dict[str, object], Path], ...]:
        with sqlite3.connect(snapshot) as connection:
            rows = connection.execute(
                "SELECT artifact_id,sha256,byte_count,cas_relpath FROM artifacts "
                "WHERE ingest_state='COMMITTED' ORDER BY artifact_id"
            ).fetchall()
        result: list[tuple[dict[str, object], Path]] = []
        for artifact_id, digest, byte_count, relpath in rows:
            relative = str(relpath)
            if _CAS_RELPATH.fullmatch(relative) is None:
                raise BackupError("snapshot contains an invalid CAS relative path")
            source = (self._cas_root / relative).resolve()
            if (
                not source.is_relative_to(self._cas_root)
                or source.is_symlink()
                or not source.is_file()
            ):
                raise BackupError(f"referenced CAS object is unavailable: {artifact_id}")
            actual = _entry(source)
            if actual != {"sha256": str(digest), "byte_count": int(byte_count)}:
                raise BackupError(f"referenced CAS object differs from SQLite: {artifact_id}")
            result.append(
                (
                    {
                        "artifact_id": str(artifact_id),
                        "cas_relpath": relative,
                        "archive_path": f"cas/{relative}",
                        **actual,
                    },
                    source,
                )
            )
        return tuple(result)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self._db_path, timeout=self._busy_timeout_ms / 1_000, isolation_level=None
        )
        connection.execute("PRAGMA foreign_keys=ON")
        return connection


def read_backup_manifest(data: bytes) -> dict[str, object]:
    try:
        with zipfile.ZipFile(io.BytesIO(data)) as bundle:
            raw = bundle.read("manifest.json")
            value = json.loads(raw)
    except (KeyError, OSError, zipfile.BadZipFile, json.JSONDecodeError) as error:
        raise BackupError("backup bundle manifest is unreadable") from error
    if (
        not isinstance(value, dict)
        or value.get("schema_version") != "rk.product.backup-manifest.v1"
    ):
        raise BackupError("backup manifest schema is unsupported")
    return value


def _receipt(row: tuple[Any, ...], artifact_row: tuple[Any, ...]) -> BackupReceipt:
    return BackupReceipt(
        str(row[0]),
        str(row[1]),
        str(row[2]),
        ArtifactRef(
            str(artifact_row[0]),
            str(artifact_row[1]),
            int(artifact_row[2]),
            str(artifact_row[3]),
            0,
        ),
        str(row[5]),
        int(row[6]),
        int(row[7]),
        int(row[8]),
        int(row[9]),
        str(row[10]),
        str(row[11]),
    )


def _consistency(database: Path) -> dict[str, int]:
    with sqlite3.connect(database) as connection:
        activity = connection.execute(
            "SELECT COALESCE(MAX(cursor),0) FROM product_activity_events"
        ).fetchone()
        jobs = connection.execute("SELECT COUNT(*) FROM product_jobs").fetchone()
        checkpoints = connection.execute("SELECT COUNT(*) FROM product_job_checkpoints").fetchone()
        placeholders = ",".join("?" for _ in _TERMINAL_JOBS)
        terminal = connection.execute(
            f"SELECT COUNT(*) FROM product_jobs WHERE state IN ({placeholders})", _TERMINAL_JOBS
        ).fetchone()
    return {
        "activity_cursor": int(activity[0]),
        "job_count": int(jobs[0]),
        "checkpoint_count": int(checkpoints[0]),
        "terminal_job_count": int(terminal[0]),
    }


def _canonical_json(value: object) -> bytes:
    return (
        json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n"
    ).encode()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as reader:
        while chunk := reader.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _entry(path: Path) -> dict[str, object]:
    return {"sha256": _sha256_file(path), "byte_count": path.stat().st_size}


def _require_artifact(artifact: ArtifactRef, digest: str, byte_count: int) -> None:
    if artifact.sha256 != digest or artifact.byte_count != byte_count:
        raise BackupError("canonical artifact registry returned a conflicting backup binding")


__all__ = [
    "BackupArtifactReader",
    "BackupError",
    "BackupReceipt",
    "BackupService",
    "CasBackupArtifactReader",
    "read_backup_manifest",
]
