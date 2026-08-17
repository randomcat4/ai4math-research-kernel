"""Durable byte-cursor logs for explicitly public managed-process streams."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import tempfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from rk.cas import ContentAddressedStore
from rk.domain import ArtifactInput, ArtifactRef
from rk.product.artifact_upload import ArtifactRegistry
from rk.runtime import format_utc
from rk.sqlite import open_sqlite

_SCOPES = frozenset({"GLOBAL", "RUN", "DEPLOYMENT"})
_STREAMS = frozenset({"STDOUT", "STDERR"})


class PublicLogError(RuntimeError):
    """A public log identity, append, cursor, or seal invariant failed."""


class LogCursorAhead(PublicLogError):
    """A client cursor points beyond the current durable log."""


@dataclass(frozen=True, slots=True)
class PublicLog:
    log_id: str
    scope_kind: str
    scope_id: str
    producer_run_id: str
    stream: str
    state: str
    logical_name: str
    byte_count: int
    artifact_id: str | None
    created_at: str
    updated_at: str
    sealed_at: str | None


@dataclass(frozen=True, slots=True)
class LogTail:
    log_id: str
    stream: str
    cursor: int
    next_cursor: int
    durable_byte_count: int
    data: bytes
    caught_up: bool
    end_of_log: bool
    artifact_id: str | None


FaultHook = Callable[[str, PublicLog], None]


class PublicLogStore:
    """One formal active-log ledger, sealed into the existing immutable CAS."""

    def __init__(
        self,
        *,
        db_path: Path,
        cas: ContentAddressedStore,
        registry: ArtifactRegistry,
        spool_root: Path,
        id_generator: Callable[[], str],
        clock: Callable[[], datetime],
        max_chunk_bytes: int,
        max_tail_bytes: int,
        busy_timeout_ms: int = 5_000,
        fault_hook: FaultHook | None = None,
    ) -> None:
        if max_chunk_bytes <= 0 or max_tail_bytes <= 0:
            raise ValueError("log chunk and tail limits must be positive")
        self._db_path = Path(db_path)
        self._cas = cas
        self._registry = registry
        self._spool_root = Path(spool_root).resolve()
        self._ids = id_generator
        self._clock = clock
        self._max_chunk_bytes = max_chunk_bytes
        self._max_tail_bytes = max_tail_bytes
        self._busy_timeout_ms = busy_timeout_ms
        self._fault_hook = fault_hook
        self._spool_root.mkdir(parents=True, exist_ok=True)

    def create(
        self,
        *,
        scope_kind: str,
        scope_id: str,
        producer_run_id: str,
        stream: str,
        logical_name: str,
    ) -> PublicLog:
        if (
            scope_kind not in _SCOPES
            or stream not in _STREAMS
            or not scope_id
            or not producer_run_id
            or not logical_name
        ):
            raise ValueError("invalid public managed-process log declaration")
        now = format_utc(self._clock())
        log_id = self._ids()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                _SELECT + " WHERE producer_run_id=? AND stream=?",
                (producer_run_id, stream),
            ).fetchone()
            if existing is not None:
                current = _log(existing)
                if (
                    current.scope_kind != scope_kind
                    or current.scope_id != scope_id
                    or current.logical_name != logical_name
                ):
                    raise PublicLogError(
                        "producer stream is already bound to another public log declaration"
                    )
                connection.commit()
                return current
            connection.execute(
                "INSERT INTO product_public_logs("
                "log_id,scope_kind,scope_id,producer_run_id,producer_kind,stream,state,"
                "logical_name,byte_count,created_at,updated_at) "
                "VALUES(?,?,?,?,'MANAGED_PROCESS',?,'OPEN',?,0,?,?)",
                (
                    log_id,
                    scope_kind,
                    scope_id,
                    producer_run_id,
                    stream,
                    logical_name,
                    now,
                    now,
                ),
            )
            connection.commit()
        return self.get(log_id)

    def get(self, log_id: str) -> PublicLog:
        with self._connect() as connection:
            row = connection.execute(_SELECT + " WHERE log_id=?", (log_id,)).fetchone()
        if row is None:
            raise KeyError(log_id)
        return _log(row)

    def append(
        self,
        log_id: str,
        *,
        offset: int,
        data: bytes,
        transfer_sha256: str,
    ) -> PublicLog:
        if offset < 0 or not data or len(data) > self._max_chunk_bytes:
            raise ValueError("invalid public log chunk")
        if hashlib.sha256(data).hexdigest() != transfer_sha256:
            raise PublicLogError("log chunk transfer digest does not match bytes")
        now = format_utc(self._clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(_SELECT + " WHERE log_id=?", (log_id,)).fetchone()
            if row is None:
                raise KeyError(log_id)
            current = _log(row)
            if current.state != "OPEN":
                raise PublicLogError(f"public log does not accept bytes from {current.state}")
            duplicate = connection.execute(
                "SELECT byte_count,transfer_sha256,data FROM product_public_log_chunks "
                "WHERE log_id=? AND chunk_offset=?",
                (log_id, offset),
            ).fetchone()
            if offset != current.byte_count:
                if (
                    duplicate is not None
                    and int(duplicate[0]) == len(data)
                    and str(duplicate[1]) == transfer_sha256
                    and bytes(duplicate[2]) == data
                    and offset + len(data) <= current.byte_count
                ):
                    connection.commit()
                    return current
                raise PublicLogError("log chunk offset is not the next durable byte cursor")
            connection.execute(
                "INSERT INTO product_public_log_chunks("
                "log_id,chunk_offset,byte_count,transfer_sha256,data,appended_at) "
                "VALUES(?,?,?,?,?,?)",
                (log_id, offset, len(data), transfer_sha256, data, now),
            )
            updated = connection.execute(
                "UPDATE product_public_logs SET byte_count=?,updated_at=? "
                "WHERE log_id=? AND state='OPEN' AND byte_count=?",
                (offset + len(data), now, log_id, offset),
            )
            if updated.rowcount != 1:
                raise PublicLogError("public log cursor changed during append")
            connection.commit()
        return self.get(log_id)

    def tail(self, log_id: str, *, cursor: int, limit: int | None = None) -> LogTail:
        if cursor < 0:
            raise ValueError("log byte cursor cannot be negative")
        requested = self._max_tail_bytes if limit is None else limit
        if requested <= 0 or requested > self._max_tail_bytes:
            raise ValueError("log tail limit is outside the configured bound")
        with self._connect() as connection:
            connection.execute("BEGIN")
            row = connection.execute(_SELECT + " WHERE log_id=?", (log_id,)).fetchone()
            if row is None:
                raise KeyError(log_id)
            current = _log(row)
            if cursor > current.byte_count:
                raise LogCursorAhead(
                    f"cursor {cursor} is beyond durable byte count {current.byte_count}"
                )
            end = min(cursor + requested, current.byte_count)
            rows = connection.execute(
                "SELECT chunk_offset,byte_count,data FROM product_public_log_chunks "
                "WHERE log_id=? AND chunk_offset<? AND chunk_offset+byte_count>? "
                "ORDER BY chunk_offset",
                (log_id, end, cursor),
            ).fetchall()
            data = _slice_chunks(rows, cursor=cursor, end=end)
            connection.commit()
        next_cursor = cursor + len(data)
        if next_cursor != end:
            raise PublicLogError("durable log chunks do not cover the registered byte cursor")
        caught_up = next_cursor == current.byte_count
        return LogTail(
            log_id=current.log_id,
            stream=current.stream,
            cursor=cursor,
            next_cursor=next_cursor,
            durable_byte_count=current.byte_count,
            data=data,
            caught_up=caught_up,
            end_of_log=current.state == "SEALED" and caught_up,
            artifact_id=current.artifact_id,
        )

    def seal(self, log_id: str) -> ArtifactRef:
        current = self._begin_seal(log_id)
        if current.state == "SEALED":
            assert current.artifact_id is not None
            artifact = self._registry_artifact(current.artifact_id)
            if artifact is None:
                raise PublicLogError("sealed log artifact is absent from canonical registry")
            return artifact
        path, digest = self._materialize(current)
        try:
            existing = self._registry.find_by_sha256(digest)
            if existing is None:
                staged = self._cas.stage_input(
                    ArtifactInput(
                        name=current.logical_name,
                        path=str(path),
                        sha256=digest,
                        byte_count=current.byte_count,
                        media_type="text/plain; charset=utf-8",
                    )
                )
                committed = self._cas.commit(staged, now=self._clock())
                self._call_fault("after_cas_commit", current)
                artifact = self._registry.register(committed)
            else:
                artifact = existing
            if (
                artifact.sha256 != digest
                or artifact.byte_count != current.byte_count
                or artifact.media_type != "text/plain; charset=utf-8"
            ):
                raise PublicLogError("canonical artifact does not match sealed public log")
            self._call_fault("after_artifact_register", current)
            return self._bind_sealed(current, artifact)
        finally:
            path.unlink(missing_ok=True)

    def _begin_seal(self, log_id: str) -> PublicLog:
        now = format_utc(self._clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(_SELECT + " WHERE log_id=?", (log_id,)).fetchone()
            if row is None:
                raise KeyError(log_id)
            current = _log(row)
            if current.state == "OPEN":
                connection.execute(
                    "UPDATE product_public_logs SET state='SEALING',updated_at=? "
                    "WHERE log_id=? AND state='OPEN'",
                    (now, log_id),
                )
            connection.commit()
        return self.get(log_id)

    def _materialize(self, log: PublicLog) -> tuple[Path, str]:
        descriptor, name = tempfile.mkstemp(prefix="rk-public-log-", dir=self._spool_root)
        path = Path(name)
        hasher = hashlib.sha256()
        byte_count = 0
        try:
            with os.fdopen(descriptor, "wb", buffering=0) as writer, self._connect() as connection:
                rows = connection.execute(
                    "SELECT chunk_offset,data FROM product_public_log_chunks "
                    "WHERE log_id=? ORDER BY chunk_offset",
                    (log.log_id,),
                )
                for row in rows:
                    if int(row[0]) != byte_count:
                        raise PublicLogError("public log chunk ledger has a byte gap")
                    data = bytes(row[1])
                    writer.write(data)
                    hasher.update(data)
                    byte_count += len(data)
                os.fsync(writer.fileno())
            if byte_count != log.byte_count:
                raise PublicLogError("public log chunks do not match registered byte count")
            return path, hasher.hexdigest()
        except BaseException:
            path.unlink(missing_ok=True)
            raise

    def _bind_sealed(self, log: PublicLog, artifact: ArtifactRef) -> ArtifactRef:
        now = format_utc(self._clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(_SELECT + " WHERE log_id=?", (log.log_id,)).fetchone()
            if row is None:
                raise KeyError(log.log_id)
            current = _log(row)
            if current.state == "SEALED":
                if current.artifact_id != artifact.artifact_id:
                    raise PublicLogError("sealed log was rebound to another artifact")
                connection.commit()
                return artifact
            if current.state != "SEALING" or current.byte_count != log.byte_count:
                raise PublicLogError("public log changed while it was being sealed")
            connection.execute(
                "UPDATE product_public_logs SET state='SEALED',artifact_id=?,updated_at=?,"
                "sealed_at=? WHERE log_id=? AND state='SEALING'",
                (artifact.artifact_id, now, now, log.log_id),
            )
            connection.commit()
        return artifact

    def _registry_artifact(self, artifact_id: str) -> ArtifactRef | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT sha256,byte_count,media_type FROM artifacts "
                "WHERE artifact_id=? AND ingest_state='COMMITTED'",
                (artifact_id,),
            ).fetchone()
        if row is None:
            return None
        return ArtifactRef(artifact_id, str(row[0]), int(row[1]), str(row[2]), 0)

    def _connect(self) -> sqlite3.Connection:
        connection = open_sqlite(
            self._db_path,
            timeout=self._busy_timeout_ms / 1_000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA synchronous=FULL")
        connection.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms}")
        return connection

    def _call_fault(self, point: str, log: PublicLog) -> None:
        if self._fault_hook is not None:
            self._fault_hook(point, log)


_SELECT = (
    "SELECT log_id,scope_kind,scope_id,producer_run_id,stream,state,logical_name,"
    "byte_count,artifact_id,created_at,updated_at,sealed_at FROM product_public_logs"
)


def _log(row: sqlite3.Row) -> PublicLog:
    return PublicLog(
        log_id=str(row[0]),
        scope_kind=str(row[1]),
        scope_id=str(row[2]),
        producer_run_id=str(row[3]),
        stream=str(row[4]),
        state=str(row[5]),
        logical_name=str(row[6]),
        byte_count=int(row[7]),
        artifact_id=str(row[8]) if row[8] is not None else None,
        created_at=str(row[9]),
        updated_at=str(row[10]),
        sealed_at=str(row[11]) if row[11] is not None else None,
    )


def _slice_chunks(rows: list[sqlite3.Row], *, cursor: int, end: int) -> bytes:
    output = bytearray()
    position = cursor
    for row in rows:
        chunk_offset = int(row[0])
        chunk = bytes(row[2])
        start_in_chunk = max(position - chunk_offset, 0)
        available = min(len(chunk), end - chunk_offset) - start_in_chunk
        if available <= 0:
            continue
        if chunk_offset + start_in_chunk != position:
            raise PublicLogError("public log chunk ledger has a byte gap")
        output.extend(chunk[start_in_chunk : start_in_chunk + available])
        position += available
    return bytes(output)


__all__ = [
    "LogCursorAhead",
    "LogTail",
    "PublicLog",
    "PublicLogError",
    "PublicLogStore",
]
