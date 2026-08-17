"""Restart-safe browser chunk uploads committed through the canonical CAS."""

from __future__ import annotations

import hashlib
import os
import re
import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Protocol

from rk.cas import CommittedArtifact, ContentAddressedStore
from rk.domain import ArtifactInput, ArtifactRef
from rk.runtime import format_utc
from rk.sqlite import open_sqlite
from rk.storage import SQLiteStorage

_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_SPOOL_NAME = re.compile(r"^[A-Za-z0-9_-]+\.part$")


class ArtifactUploadError(RuntimeError):
    """An upload identity, offset, transfer digest, or commit invariant failed."""


class ArtifactRegistry(Protocol):
    """Canonical artifact mapping owned by the existing storage layer."""

    def find_by_sha256(self, sha256: str) -> ArtifactRef | None: ...

    def register(self, artifact: CommittedArtifact) -> ArtifactRef: ...


class SQLiteArtifactRegistry:
    """Adapter to the existing immutable artifacts table."""

    def __init__(self, storage: SQLiteStorage) -> None:
        self._storage = storage

    def find_by_sha256(self, sha256: str) -> ArtifactRef | None:
        row = self._storage.get_artifact_by_sha256(sha256)
        if row is None or row.get("ingest_state") != "COMMITTED":
            return None
        return ArtifactRef(
            artifact_id=str(row["artifact_id"]),
            sha256=str(row["sha256"]),
            byte_count=int(row["byte_count"]),
            media_type=str(row["media_type"]),
            at_revision=0,
        )

    def register(self, artifact: CommittedArtifact) -> ArtifactRef:
        with self._storage.transaction() as connection:
            row = self._storage.insert_artifact(connection, artifact.to_record())
        return ArtifactRef(
            artifact_id=str(row["artifact_id"]),
            sha256=str(row["sha256"]),
            byte_count=int(row["byte_count"]),
            media_type=str(row["media_type"]),
            at_revision=0,
        )


@dataclass(frozen=True, slots=True)
class UploadSession:
    upload_id: str
    request_id: str
    state: str
    logical_name: str
    media_type: str
    declared_byte_count: int
    declared_sha256: str
    received_byte_count: int
    artifact_id: str | None
    created_at: str
    updated_at: str
    committed_at: str | None


FaultHook = Callable[[str, UploadSession], None]


class ArtifactUploadStore:
    """Persist only transfer state; CAS and ArtifactRegistry own final content identity."""

    def __init__(
        self,
        *,
        db_path: Path,
        spool_root: Path,
        cas: ContentAddressedStore,
        registry: ArtifactRegistry,
        id_generator: Callable[[], str],
        clock: Callable[[], datetime],
        max_upload_bytes: int,
        max_chunk_bytes: int,
        busy_timeout_ms: int = 5_000,
        fault_hook: FaultHook | None = None,
    ) -> None:
        if max_upload_bytes <= 0 or max_chunk_bytes <= 0:
            raise ValueError("upload and chunk limits must be positive")
        self._db_path = Path(db_path)
        self._spool_root = Path(spool_root).resolve()
        self._cas = cas
        self._registry = registry
        self._ids = id_generator
        self._clock = clock
        self._max_upload_bytes = max_upload_bytes
        self._max_chunk_bytes = max_chunk_bytes
        self._busy_timeout_ms = busy_timeout_ms
        self._fault_hook = fault_hook
        self._spool_root.mkdir(parents=True, exist_ok=True)

    def begin(
        self,
        *,
        request_id: str,
        logical_name: str,
        media_type: str,
        byte_count: int,
        sha256: str,
    ) -> UploadSession:
        if (
            not request_id
            or not logical_name
            or not media_type
            or byte_count < 0
            or byte_count > self._max_upload_bytes
            or _SHA256.fullmatch(sha256) is None
        ):
            raise ValueError("invalid upload declaration")
        now = format_utc(self._clock())
        upload_id = self._ids()
        spool_name = f"{upload_id}.part"
        if _SPOOL_NAME.fullmatch(spool_name) is None:
            raise ArtifactUploadError("generated upload id is not a safe spool identity")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                _UPLOAD_SELECT + " WHERE request_id=?", (request_id,)
            ).fetchone()
            if existing is not None:
                current = _upload(existing)
                if (
                    current.logical_name != logical_name
                    or current.media_type != media_type
                    or current.declared_byte_count != byte_count
                    or current.declared_sha256 != sha256
                ):
                    raise ArtifactUploadError(
                        "request_id is already bound to another upload declaration"
                    )
                connection.commit()
                if current.state != "COMMITTED":
                    self._require_spool_consistent(current)
                return current
            path = self._spool_path(spool_name)
            try:
                with path.open("xb") as writer:
                    writer.flush()
                    os.fsync(writer.fileno())
                _fsync_directory(self._spool_root)
                connection.execute(
                    "INSERT INTO product_uploads("
                    "upload_id,request_id,state,logical_name,media_type,declared_byte_count,"
                    "declared_sha256,received_byte_count,spool_name,created_at,updated_at)"
                    " VALUES(?,?,'OPEN',?,?,?,?,0,?,?,?)",
                    (
                        upload_id,
                        request_id,
                        logical_name,
                        media_type,
                        byte_count,
                        sha256,
                        spool_name,
                        now,
                        now,
                    ),
                )
                connection.commit()
            except BaseException:
                connection.rollback()
                path.unlink(missing_ok=True)
                raise
        return self.get(upload_id)

    def get(self, upload_id: str) -> UploadSession:
        with self._connect() as connection:
            row = connection.execute(_UPLOAD_SELECT + " WHERE upload_id=?", (upload_id,)).fetchone()
        if row is None:
            raise KeyError(upload_id)
        return _upload(row)

    def append(
        self,
        upload_id: str,
        *,
        offset: int,
        data: bytes,
        transfer_sha256: str,
    ) -> UploadSession:
        if (
            offset < 0
            or not data
            or len(data) > self._max_chunk_bytes
            or _SHA256.fullmatch(transfer_sha256) is None
        ):
            raise ValueError("invalid upload chunk")
        if hashlib.sha256(data).hexdigest() != transfer_sha256:
            raise ArtifactUploadError("chunk transfer digest does not match bytes")
        now = format_utc(self._clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                _UPLOAD_SELECT_WITH_SPOOL + " WHERE upload_id=?", (upload_id,)
            ).fetchone()
            if row is None:
                raise KeyError(upload_id)
            current, spool_name = _upload(row[:-1]), str(row[-1])
            if current.state != "OPEN":
                raise ArtifactUploadError(f"upload does not accept chunks from {current.state}")
            duplicate = connection.execute(
                "SELECT byte_count,transfer_sha256 FROM product_upload_chunks "
                "WHERE upload_id=? AND chunk_offset=?",
                (upload_id, offset),
            ).fetchone()
            if offset != current.received_byte_count:
                if (
                    duplicate is not None
                    and int(duplicate[0]) == len(data)
                    and str(duplicate[1]) == transfer_sha256
                    and offset + len(data) <= current.received_byte_count
                ):
                    connection.commit()
                    return current
                raise ArtifactUploadError("chunk offset is not the next durable byte")
            if current.received_byte_count + len(data) > current.declared_byte_count:
                raise ArtifactUploadError("chunk exceeds declared upload length")
            path = self._spool_path(spool_name)
            self._truncate_uncommitted_tail(path, current.received_byte_count)
            with path.open("r+b", buffering=0) as writer:
                writer.seek(offset)
                written = writer.write(data)
                if written != len(data):
                    raise ArtifactUploadError("chunk write was incomplete")
                os.fsync(writer.fileno())
            self._call_fault("after_chunk_fsync", current)
            connection.execute(
                "INSERT INTO product_upload_chunks("
                "upload_id,chunk_offset,byte_count,transfer_sha256,accepted_at)"
                " VALUES(?,?,?,?,?)",
                (upload_id, offset, len(data), transfer_sha256, now),
            )
            connection.execute(
                "UPDATE product_uploads SET received_byte_count=?,updated_at=? "
                "WHERE upload_id=? AND state='OPEN' AND received_byte_count=?",
                (offset + len(data), now, upload_id, offset),
            )
            connection.commit()
        return self.get(upload_id)

    def commit(self, upload_id: str) -> ArtifactRef:
        session, spool_name = self._prepare_commit(upload_id)
        existing = self._registry.find_by_sha256(session.declared_sha256)
        if existing is not None:
            self._validate_ref(existing, session)
            return self._bind_committed(session, existing)

        spool = self._spool_path(spool_name)
        self._require_spool_size(spool, session.declared_byte_count)
        staged = self._cas.stage_input(
            ArtifactInput(
                name=session.logical_name,
                path=str(spool),
                sha256=session.declared_sha256,
                byte_count=session.declared_byte_count,
                media_type=session.media_type,
            )
        )
        committed = self._cas.commit(staged, now=self._clock())
        self._call_fault("after_cas_commit", session)
        canonical = self._registry.register(committed)
        self._validate_ref(canonical, session)
        self._call_fault("after_artifact_register", session)
        return self._bind_committed(session, canonical)

    def abort(self, upload_id: str) -> UploadSession:
        now = format_utc(self._clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                _UPLOAD_SELECT_WITH_SPOOL + " WHERE upload_id=?", (upload_id,)
            ).fetchone()
            if row is None:
                raise KeyError(upload_id)
            current, spool_name = _upload(row[:-1]), str(row[-1])
            if current.state == "COMMITTED":
                raise ArtifactUploadError("committed upload cannot be aborted")
            connection.execute(
                "UPDATE product_uploads SET state='ABORTED',updated_at=? WHERE upload_id=?",
                (now, upload_id),
            )
            connection.commit()
        self._spool_path(spool_name).unlink(missing_ok=True)
        return self.get(upload_id)

    def _prepare_commit(self, upload_id: str) -> tuple[UploadSession, str]:
        now = format_utc(self._clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                _UPLOAD_SELECT_WITH_SPOOL + " WHERE upload_id=?", (upload_id,)
            ).fetchone()
            if row is None:
                raise KeyError(upload_id)
            current, spool_name = _upload(row[:-1]), str(row[-1])
            if current.state == "COMMITTED":
                connection.commit()
                existing = self._registry.find_by_sha256(current.declared_sha256)
                if existing is None or existing.artifact_id != current.artifact_id:
                    raise ArtifactUploadError("committed upload lost its canonical artifact")
                self._validate_ref(existing, current)
                self._spool_path(spool_name).unlink(missing_ok=True)
                return current, spool_name
            if current.state not in {"OPEN", "COMMITTING"}:
                raise ArtifactUploadError(f"upload cannot commit from {current.state}")
            if current.received_byte_count != current.declared_byte_count:
                raise ArtifactUploadError("upload length is incomplete")
            self._require_spool_consistent(current)
            connection.execute(
                "UPDATE product_uploads SET state='COMMITTING',updated_at=? "
                "WHERE upload_id=? AND state IN ('OPEN','COMMITTING')",
                (now, upload_id),
            )
            connection.commit()
        return self.get(upload_id), spool_name

    def _bind_committed(self, session: UploadSession, artifact: ArtifactRef) -> ArtifactRef:
        now = format_utc(self._clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT state,artifact_id FROM product_uploads WHERE upload_id=?",
                (session.upload_id,),
            ).fetchone()
            if row is None:
                raise KeyError(session.upload_id)
            if row[0] == "COMMITTED":
                if row[1] != artifact.artifact_id:
                    raise ArtifactUploadError("upload was committed to another artifact")
                connection.commit()
                return artifact
            changed = connection.execute(
                "UPDATE product_uploads SET state='COMMITTED',artifact_id=?,"
                "committed_at=?,updated_at=? WHERE upload_id=? AND state='COMMITTING'",
                (artifact.artifact_id, now, now, session.upload_id),
            ).rowcount
            if changed != 1:
                raise ArtifactUploadError("upload commit state changed concurrently")
            connection.commit()
        path = self._spool_path(f"{session.upload_id}.part")
        path.unlink(missing_ok=True)
        _fsync_directory(self._spool_root)
        return artifact

    def _require_spool_consistent(self, session: UploadSession) -> None:
        path = self._spool_path(f"{session.upload_id}.part")
        self._truncate_uncommitted_tail(path, session.received_byte_count)

    @staticmethod
    def _require_spool_size(path: Path, expected: int) -> None:
        try:
            size = path.stat().st_size
        except OSError as error:
            raise ArtifactUploadError("upload spool is missing") from error
        if size != expected:
            raise ArtifactUploadError("upload spool length differs from durable offset")

    def _truncate_uncommitted_tail(self, path: Path, durable_offset: int) -> None:
        try:
            size = path.stat().st_size
        except OSError as error:
            raise ArtifactUploadError("upload spool is missing") from error
        if size < durable_offset:
            raise ArtifactUploadError("upload spool is shorter than its durable offset")
        if size > durable_offset:
            with path.open("r+b") as writer:
                writer.truncate(durable_offset)
                writer.flush()
                os.fsync(writer.fileno())

    def _spool_path(self, spool_name: str) -> Path:
        if _SPOOL_NAME.fullmatch(spool_name) is None:
            raise ArtifactUploadError("stored spool identity is invalid")
        return self._spool_root / spool_name

    @staticmethod
    def _validate_ref(artifact: ArtifactRef, session: UploadSession) -> None:
        if (
            artifact.sha256 != session.declared_sha256
            or artifact.byte_count != session.declared_byte_count
            or artifact.media_type != session.media_type
        ):
            raise ArtifactUploadError("canonical artifact conflicts with upload declaration")

    def _call_fault(self, point: str, session: UploadSession) -> None:
        if self._fault_hook is not None:
            self._fault_hook(point, session)

    def _connect(self) -> sqlite3.Connection:
        connection = open_sqlite(self._db_path, timeout=self._busy_timeout_ms / 1_000)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
        return connection


_UPLOAD_SELECT = (
    "SELECT upload_id,request_id,state,logical_name,media_type,declared_byte_count,"
    "declared_sha256,received_byte_count,artifact_id,created_at,updated_at,committed_at "
    "FROM product_uploads"
)
_UPLOAD_SELECT_WITH_SPOOL = _UPLOAD_SELECT.replace(
    " FROM product_uploads", ",spool_name FROM product_uploads"
)


def _upload(row: tuple[object, ...]) -> UploadSession:
    return UploadSession(
        upload_id=str(row[0]),
        request_id=str(row[1]),
        state=str(row[2]),
        logical_name=str(row[3]),
        media_type=str(row[4]),
        declared_byte_count=int(str(row[5])),
        declared_sha256=str(row[6]),
        received_byte_count=int(str(row[7])),
        artifact_id=str(row[8]) if row[8] is not None else None,
        created_at=str(row[9]),
        updated_at=str(row[10]),
        committed_at=str(row[11]) if row[11] is not None else None,
    )


def _fsync_directory(path: Path) -> None:
    descriptor = os.open(path, os.O_RDONLY)
    try:
        os.fsync(descriptor)
    finally:
        os.close(descriptor)
