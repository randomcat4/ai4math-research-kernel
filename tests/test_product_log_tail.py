from __future__ import annotations

import hashlib
import inspect
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from rk.cas import ContentAddressedStore
from rk.migrations import MigrationRunner
from rk.product.artifact_upload import SQLiteArtifactRegistry
from rk.product.log_tail import LogCursorAhead, PublicLogError, PublicLogStore
from rk.product_migrations import ProductMigrationAssembler, ProductMigrationRegistry
from rk.storage import SQLiteStorage

NOW = datetime(2026, 8, 13, 12, tzinfo=UTC)


class Ids:
    def __init__(self, prefix: str) -> None:
        self.prefix = prefix
        self.value = 0

    def __call__(self) -> str:
        self.value += 1
        return f"{self.prefix}-{self.value}"

    def new(self) -> str:
        return self()


def migrated_db(tmp_path: Path) -> Path:
    db = tmp_path / "rk.sqlite"
    MigrationRunner(db, Path("migrations"), 5_000, minimum_sqlite=(3, 0, 0)).migrate()
    with sqlite3.connect(db) as connection:
        ProductMigrationAssembler(ProductMigrationRegistry(Path("schema_fragments"))).apply(
            connection
        )
    return db


def service(
    tmp_path: Path,
    *,
    db: Path | None = None,
    log_ids: Ids | None = None,
    hook: Any = None,
) -> tuple[PublicLogStore, Path, Path]:
    database = db or migrated_db(tmp_path)
    spool = tmp_path / "log-spool"
    spool.mkdir(exist_ok=True)
    cas_root = tmp_path / "cas"
    storage = SQLiteStorage(database, 5_000)
    cas = ContentAddressedStore(
        cas_root,
        max_bytes=20 * 1024 * 1024,
        inbox_roots=(spool,),
        orphan_grace_seconds=60,
        id_generator=Ids("artifact"),
    )
    return (
        PublicLogStore(
            db_path=database,
            cas=cas,
            registry=SQLiteArtifactRegistry(storage),
            spool_root=spool,
            id_generator=log_ids or Ids("log"),
            clock=lambda: NOW,
            max_chunk_bytes=1024 * 1024,
            max_tail_bytes=64 * 1024,
            fault_hook=hook,
        ),
        database,
        cas_root,
    )


def append(store: PublicLogStore, log_id: str, offset: int, data: bytes) -> None:
    store.append(
        log_id,
        offset=offset,
        data=data,
        transfer_sha256=hashlib.sha256(data).hexdigest(),
    )


def test_byte_cursor_tails_continuous_appends_across_service_restart(tmp_path: Path) -> None:
    ids = Ids("log")
    first, database, _cas = service(tmp_path, log_ids=ids)
    log = first.create(
        scope_kind="RUN",
        scope_id="run-1",
        producer_run_id="worker-1",
        stream="STDOUT",
        logical_name="worker-1.stdout.log",
    )
    append(first, log.log_id, 0, b"alpha\n")
    one = first.tail(log.log_id, cursor=0, limit=4)
    assert one.data == b"alph"
    assert one.next_cursor == 4
    assert one.caught_up is False

    restarted, _database, _cas = service(tmp_path, db=database, log_ids=ids)
    append(restarted, log.log_id, 6, b"beta\n")
    two = restarted.tail(log.log_id, cursor=one.next_cursor)
    assert two.data == b"a\nbeta\n"
    assert two.next_cursor == 11
    assert two.caught_up is True
    assert two.end_of_log is False


def test_real_10mb_log_returns_only_the_requested_bounded_tail(tmp_path: Path) -> None:
    store, _database, _cas = service(tmp_path)
    log = store.create(
        scope_kind="RUN",
        scope_id="run-large",
        producer_run_id="worker-large",
        stream="STDOUT",
        logical_name="worker-large.stdout.log",
    )
    chunk = bytes(range(256)) * 4096
    assert len(chunk) == 1024 * 1024
    digest = hashlib.sha256(chunk).hexdigest()
    for index in range(10):
        state = store.append(
            log.log_id,
            offset=index * len(chunk),
            data=chunk,
            transfer_sha256=digest,
        )
        assert state.byte_count == (index + 1) * len(chunk)

    tail = store.tail(log.log_id, cursor=5 * len(chunk) + 123)

    assert len(tail.data) == 64 * 1024
    assert tail.data == (chunk * 2)[123 : 123 + 64 * 1024]
    assert tail.durable_byte_count == 10 * 1024 * 1024
    assert tail.caught_up is False


def test_stdout_and_stderr_are_distinct_formal_public_streams(tmp_path: Path) -> None:
    store, _database, _cas = service(tmp_path)
    stdout = store.create(
        scope_kind="RUN",
        scope_id="run-1",
        producer_run_id="worker-1",
        stream="STDOUT",
        logical_name="stdout.log",
    )
    stderr = store.create(
        scope_kind="RUN",
        scope_id="run-1",
        producer_run_id="worker-1",
        stream="STDERR",
        logical_name="stderr.log",
    )
    append(store, stdout.log_id, 0, b"public output")
    append(store, stderr.log_id, 0, b"public error")
    assert store.tail(stdout.log_id, cursor=0).data == b"public output"
    assert store.tail(stderr.log_id, cursor=0).data == b"public error"


def test_public_create_surface_cannot_label_model_completion_as_log(tmp_path: Path) -> None:
    store, database, _cas = service(tmp_path)
    assert "producer_kind" not in inspect.signature(store.create).parameters
    with sqlite3.connect(database) as connection, pytest.raises(sqlite3.IntegrityError):
        connection.execute(
            "INSERT INTO product_public_logs("
            "log_id,scope_kind,scope_id,producer_run_id,producer_kind,stream,state,logical_name,"
            "byte_count,created_at,updated_at) VALUES("
            "'raw-1','RUN','run-1','model-1','RAW_MODEL_COMPLETION','STDOUT','OPEN',"
            "'raw.txt',0,'2026-08-13T12:00:00Z','2026-08-13T12:00:00Z')"
        )


def test_append_offset_digest_and_replay_are_exact(tmp_path: Path) -> None:
    store, _database, _cas = service(tmp_path)
    log = store.create(
        scope_kind="DEPLOYMENT",
        scope_id="deployment-1",
        producer_run_id="probe-1",
        stream="STDOUT",
        logical_name="probe.stdout.log",
    )
    append(store, log.log_id, 0, b"abc")
    append(store, log.log_id, 0, b"abc")
    with pytest.raises(PublicLogError, match="digest"):
        store.append(log.log_id, offset=3, data=b"x", transfer_sha256="0" * 64)
    with pytest.raises(PublicLogError, match="next durable"):
        append(store, log.log_id, 1, b"x")
    with pytest.raises(LogCursorAhead):
        store.tail(log.log_id, cursor=4)


def test_seal_binds_exact_immutable_cas_artifact_and_marks_end_of_log(tmp_path: Path) -> None:
    store, database, cas_root = service(tmp_path)
    log = store.create(
        scope_kind="RUN",
        scope_id="run-1",
        producer_run_id="worker-1",
        stream="STDOUT",
        logical_name="worker.stdout.log",
    )
    data = b"line one\nline two\n"
    append(store, log.log_id, 0, data[:8])
    append(store, log.log_id, 8, data[8:])

    ref = store.seal(log.log_id)

    assert ref.sha256 == hashlib.sha256(data).hexdigest()
    assert ref.byte_count == len(data)
    assert ref.media_type == "text/plain; charset=utf-8"
    assert (cas_root / ref.sha256[:2] / ref.sha256[2:4] / ref.sha256).read_bytes() == data
    tail = store.tail(log.log_id, cursor=0)
    assert tail.data == data
    assert tail.end_of_log is True
    assert tail.artifact_id == ref.artifact_id
    with sqlite3.connect(database) as connection:
        assert connection.execute(
            "SELECT state,artifact_id FROM product_public_logs WHERE log_id=?", (log.log_id,)
        ).fetchone() == ("SEALED", ref.artifact_id)
    with pytest.raises(PublicLogError, match="SEALED"):
        append(store, log.log_id, len(data), b"late")


def test_restart_after_artifact_registration_reuses_one_canonical_artifact(tmp_path: Path) -> None:
    tripped = False

    def crash(point: str, _log: object) -> None:
        nonlocal tripped
        if point == "after_artifact_register" and not tripped:
            tripped = True
            raise RuntimeError("lost before log bind")

    ids = Ids("log")
    first, database, cas_root = service(tmp_path, log_ids=ids, hook=crash)
    log = first.create(
        scope_kind="RUN",
        scope_id="run-1",
        producer_run_id="worker-1",
        stream="STDERR",
        logical_name="worker.stderr.log",
    )
    append(first, log.log_id, 0, b"failure details\n")
    with pytest.raises(RuntimeError, match="lost"):
        first.seal(log.log_id)
    assert first.get(log.log_id).state == "SEALING"

    restarted, _database, _cas = service(tmp_path, db=database, log_ids=ids)
    ref = restarted.seal(log.log_id)
    assert restarted.get(log.log_id).artifact_id == ref.artifact_id
    assert len(list(cas_root.glob("[0-9a-f][0-9a-f]/[0-9a-f][0-9a-f]/*"))) == 1
