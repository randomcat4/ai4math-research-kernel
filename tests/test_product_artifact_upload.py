from __future__ import annotations

import hashlib
import inspect
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from rk.cas import CommittedArtifact, ContentAddressedStore
from rk.domain import ArtifactRef
from rk.migrations import MigrationRunner
from rk.product.artifact_upload import (
    ArtifactRegistry,
    ArtifactUploadError,
    ArtifactUploadStore,
    SQLiteArtifactRegistry,
)
from rk.product_migrations import ProductMigrationAssembler, ProductMigrationRegistry
from rk.storage import SQLiteStorage

NOW = datetime(2026, 8, 13, 12, tzinfo=UTC)
MIB = 1024 * 1024


class CasIds:
    def __init__(self) -> None:
        self.value = 0

    def new(self) -> str:
        self.value += 1
        return f"artifact-{self.value}"


class UploadIds:
    def __init__(self) -> None:
        self.value = 0

    def __call__(self) -> str:
        self.value += 1
        return f"upload-{self.value}"


class Registry(ArtifactRegistry):
    def __init__(self) -> None:
        self.by_digest: dict[str, ArtifactRef] = {}

    def find_by_sha256(self, sha256: str) -> ArtifactRef | None:
        return self.by_digest.get(sha256)

    def register(self, artifact: CommittedArtifact) -> ArtifactRef:
        value = artifact.to_ref(at_revision=0)
        existing = self.by_digest.setdefault(artifact.sha256, value)
        if existing.byte_count != value.byte_count or existing.media_type != value.media_type:
            raise ArtifactUploadError("canonical artifact registry conflict")
        return existing


def migrated_db(tmp_path: Path) -> Path:
    db = tmp_path / "product.sqlite"
    with sqlite3.connect(db) as connection:
        ProductMigrationAssembler(ProductMigrationRegistry(Path("schema_fragments"))).apply(
            connection
        )
    return db


def service(
    tmp_path: Path,
    *,
    db: Path | None = None,
    registry: Registry | None = None,
    uploads: UploadIds | None = None,
    hook: Any = None,
    max_upload_bytes: int = 110 * MIB,
) -> tuple[ArtifactUploadStore, Registry, Path, Path]:
    database = db or migrated_db(tmp_path)
    spool = tmp_path / "upload-spool"
    spool.mkdir(exist_ok=True)
    cas_root = tmp_path / "cas"
    canonical = registry or Registry()
    cas = ContentAddressedStore(
        cas_root,
        max_bytes=max_upload_bytes,
        inbox_roots=(spool,),
        orphan_grace_seconds=60,
        id_generator=CasIds(),
    )
    return (
        ArtifactUploadStore(
            db_path=database,
            spool_root=spool,
            cas=cas,
            registry=canonical,
            id_generator=uploads or UploadIds(),
            clock=lambda: NOW,
            max_upload_bytes=max_upload_bytes,
            max_chunk_bytes=2 * MIB,
            fault_hook=hook,
        ),
        canonical,
        database,
        cas_root,
    )


def declared(
    store: ArtifactUploadStore,
    data: bytes,
    *,
    request_id: str = "request-1",
    logical_name: str = "material.bin",
    media_type: str = "application/octet-stream",
) -> str:
    return store.begin(
        request_id=request_id,
        logical_name=logical_name,
        media_type=media_type,
        byte_count=len(data),
        sha256=hashlib.sha256(data).hexdigest(),
    ).upload_id


@pytest.mark.parametrize(
    ("logical_name", "media_type", "data"),
    [
        ("paper.pdf", "application/pdf", b"%PDF-1.7\nRK\n%%EOF\n"),
        ("proof.tex", "application/x-tex", b"\\documentclass{article}\\n"),
        ("formula.png", "image/png", b"\x89PNG\r\n\x1a\nRK"),
    ],
)
def test_browser_chunks_commit_real_pdf_tex_and_image_to_canonical_cas(
    tmp_path: Path,
    logical_name: str,
    media_type: str,
    data: bytes,
) -> None:
    store, registry, _db, cas_root = service(tmp_path)
    upload_id = declared(store, data, logical_name=logical_name, media_type=media_type)
    split = len(data) // 2
    first, second = data[:split], data[split:]
    store.append(
        upload_id,
        offset=0,
        data=first,
        transfer_sha256=hashlib.sha256(first).hexdigest(),
    )
    store.append(
        upload_id,
        offset=len(first),
        data=second,
        transfer_sha256=hashlib.sha256(second).hexdigest(),
    )

    ref = store.commit(upload_id)

    assert ref == registry.find_by_sha256(hashlib.sha256(data).hexdigest())
    final = cas_root / ref.sha256[:2] / ref.sha256[2:4] / ref.sha256
    assert final.read_bytes() == data
    assert store.get(upload_id).artifact_id == ref.artifact_id
    assert not (tmp_path / "upload-spool" / f"{upload_id}.part").exists()


def test_offset_length_and_chunk_digest_are_strict_and_retry_is_idempotent(
    tmp_path: Path,
) -> None:
    store, _registry, _db, _cas = service(tmp_path)
    data = b"abcdef"
    upload_id = declared(store, data)
    chunk = data[:3]
    digest = hashlib.sha256(chunk).hexdigest()

    with pytest.raises(ArtifactUploadError, match="digest"):
        store.append(upload_id, offset=0, data=chunk, transfer_sha256="0" * 64)
    first = store.append(upload_id, offset=0, data=chunk, transfer_sha256=digest)
    duplicate = store.append(upload_id, offset=0, data=chunk, transfer_sha256=digest)
    assert duplicate.received_byte_count == first.received_byte_count == 3
    with pytest.raises(ArtifactUploadError, match="next durable byte"):
        store.append(
            upload_id,
            offset=1,
            data=b"x",
            transfer_sha256=hashlib.sha256(b"x").hexdigest(),
        )
    with pytest.raises(ArtifactUploadError, match="declared upload length"):
        store.append(
            upload_id,
            offset=3,
            data=b"toolong",
            transfer_sha256=hashlib.sha256(b"toolong").hexdigest(),
        )


def test_restart_discards_fsynced_but_uncommitted_tail_and_resumes(
    tmp_path: Path,
) -> None:
    tripped = False

    def crash(point: str, _session: object) -> None:
        nonlocal tripped
        if point == "after_chunk_fsync" and not tripped:
            tripped = True
            raise RuntimeError("lost before SQLite offset commit")

    uploads = UploadIds()
    first, registry, db, _cas = service(tmp_path, uploads=uploads, hook=crash)
    data = b"restart-safe"
    upload_id = declared(first, data)
    chunk = data[:7]
    with pytest.raises(RuntimeError, match="lost"):
        first.append(
            upload_id,
            offset=0,
            data=chunk,
            transfer_sha256=hashlib.sha256(chunk).hexdigest(),
        )
    assert first.get(upload_id).received_byte_count == 0

    restarted, _registry, _db, _cas = service(tmp_path, db=db, registry=registry, uploads=uploads)
    restarted.append(
        upload_id,
        offset=0,
        data=chunk,
        transfer_sha256=hashlib.sha256(chunk).hexdigest(),
    )
    tail = data[len(chunk) :]
    restarted.append(
        upload_id,
        offset=len(chunk),
        data=tail,
        transfer_sha256=hashlib.sha256(tail).hexdigest(),
    )
    assert restarted.commit(upload_id).sha256 == hashlib.sha256(data).hexdigest()


def test_crash_after_registry_commit_rebinds_same_artifact_on_restart(
    tmp_path: Path,
) -> None:
    def crash(point: str, _session: object) -> None:
        if point == "after_artifact_register":
            raise RuntimeError("lost before upload row bind")

    uploads = UploadIds()
    first, registry, db, cas_root = service(tmp_path, uploads=uploads, hook=crash)
    data = b"canonical-before-row"
    upload_id = declared(first, data)
    first.append(
        upload_id,
        offset=0,
        data=data,
        transfer_sha256=hashlib.sha256(data).hexdigest(),
    )
    with pytest.raises(RuntimeError, match="lost"):
        first.commit(upload_id)
    assert first.get(upload_id).state == "COMMITTING"
    assert len(registry.by_digest) == 1

    restarted, _registry, _db, _cas = service(tmp_path, db=db, registry=registry, uploads=uploads)
    ref = restarted.commit(upload_id)
    assert restarted.get(upload_id).state == "COMMITTED"
    assert len(list(cas_root.glob("[0-9a-f][0-9a-f]/[0-9a-f][0-9a-f]/*"))) == 1
    assert ref == next(iter(registry.by_digest.values()))


def test_begin_and_commit_replays_do_not_duplicate_upload_or_artifact(
    tmp_path: Path,
) -> None:
    store, registry, _db, cas_root = service(tmp_path)
    data = b"one identity"
    first = declared(store, data)
    again = declared(store, data)
    assert first == again
    store.append(
        first,
        offset=0,
        data=data,
        transfer_sha256=hashlib.sha256(data).hexdigest(),
    )
    one = store.commit(first)
    two = store.commit(first)
    assert one == two
    assert len(registry.by_digest) == 1
    assert len(list(cas_root.glob("[0-9a-f][0-9a-f]/[0-9a-f][0-9a-f]/*"))) == 1
    assert declared(store, data) == first


def test_public_begin_surface_has_no_host_path(tmp_path: Path) -> None:
    store, _registry, _db, _cas = service(tmp_path)
    assert "path" not in inspect.signature(store.begin).parameters
    with pytest.raises(TypeError):
        store.begin(  # type: ignore[call-arg]
            request_id="request-1",
            logical_name="x.txt",
            media_type="text/plain",
            byte_count=1,
            sha256=hashlib.sha256(b"x").hexdigest(),
            host_path="/etc/passwd",
        )


def test_real_100mb_upload_streams_in_browser_sized_chunks(tmp_path: Path) -> None:
    store, registry, _db, cas_root = service(tmp_path)
    chunk = bytes(range(256)) * 4096
    assert len(chunk) == MIB
    digest = hashlib.sha256()
    for _ in range(100):
        digest.update(chunk)
    upload = store.begin(
        request_id="request-100mb",
        logical_name="dataset.bin",
        media_type="application/octet-stream",
        byte_count=100 * MIB,
        sha256=digest.hexdigest(),
    )
    transfer_digest = hashlib.sha256(chunk).hexdigest()
    for index in range(100):
        state = store.append(
            upload.upload_id,
            offset=index * MIB,
            data=chunk,
            transfer_sha256=transfer_digest,
        )
        assert state.received_byte_count == (index + 1) * MIB

    ref = store.commit(upload.upload_id)

    assert ref.byte_count == 100 * MIB
    assert ref.sha256 == digest.hexdigest()
    assert registry.find_by_sha256(ref.sha256) == ref
    final = cas_root / ref.sha256[:2] / ref.sha256[2:4] / ref.sha256
    assert final.stat().st_size == 100 * MIB


def test_upload_schema_keeps_only_declaration_and_transfer_digests(
    tmp_path: Path,
) -> None:
    _store, _registry, db, _cas = service(tmp_path)
    with sqlite3.connect(db) as connection:
        upload_columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(product_uploads)")
        }
        chunk_columns = {
            str(row[1]) for row in connection.execute("PRAGMA table_info(product_upload_chunks)")
        }
    assert "computed_sha256" not in upload_columns
    assert "cas_sha256" not in upload_columns
    assert "declared_sha256" in upload_columns
    assert "transfer_sha256" in chunk_columns


def test_commit_registers_in_existing_canonical_artifacts_table(
    tmp_path: Path,
) -> None:
    db = tmp_path / "kernel-product.sqlite"
    MigrationRunner(
        db,
        Path("migrations"),
        5_000,
        minimum_sqlite=(3, 0, 0),
    ).migrate()
    with sqlite3.connect(db) as connection:
        ProductMigrationAssembler(ProductMigrationRegistry(Path("schema_fragments"))).apply(
            connection
        )
    spool = tmp_path / "canonical-spool"
    spool.mkdir()
    cas_root = tmp_path / "canonical-cas"
    cas = ContentAddressedStore(
        cas_root,
        max_bytes=MIB,
        inbox_roots=(spool,),
        orphan_grace_seconds=60,
        id_generator=CasIds(),
    )
    registry = SQLiteArtifactRegistry(SQLiteStorage(db, 5_000))
    uploads = ArtifactUploadStore(
        db_path=db,
        spool_root=spool,
        cas=cas,
        registry=registry,
        id_generator=UploadIds(),
        clock=lambda: NOW,
        max_upload_bytes=MIB,
        max_chunk_bytes=MIB,
    )
    data = b"canonical storage mapping"
    upload_id = declared(uploads, data)
    uploads.append(
        upload_id,
        offset=0,
        data=data,
        transfer_sha256=hashlib.sha256(data).hexdigest(),
    )

    ref = uploads.commit(upload_id)

    with sqlite3.connect(db) as connection:
        assert connection.execute(
            "SELECT artifact_id,sha256,byte_count,media_type,ingest_state "
            "FROM artifacts WHERE artifact_id=?",
            (ref.artifact_id,),
        ).fetchone() == (
            ref.artifact_id,
            ref.sha256,
            len(data),
            "application/octet-stream",
            "COMMITTED",
        )


def test_crash_after_cas_replace_before_registry_is_idempotently_recovered(
    tmp_path: Path,
) -> None:
    def crash(point: str, _session: object) -> None:
        if point == "after_cas_commit":
            raise RuntimeError("lost before canonical artifact registration")

    uploads = UploadIds()
    first, registry, db, cas_root = service(tmp_path, uploads=uploads, hook=crash)
    data = b"cas-final-orphan-window"
    upload_id = declared(first, data)
    first.append(
        upload_id,
        offset=0,
        data=data,
        transfer_sha256=hashlib.sha256(data).hexdigest(),
    )
    with pytest.raises(RuntimeError, match="lost"):
        first.commit(upload_id)
    assert registry.by_digest == {}
    assert first.get(upload_id).state == "COMMITTING"

    restarted, _registry, _db, _cas = service(tmp_path, db=db, registry=registry, uploads=uploads)
    ref = restarted.commit(upload_id)

    assert restarted.get(upload_id).state == "COMMITTED"
    assert registry.find_by_sha256(ref.sha256) == ref
    assert len(list(cas_root.glob("[0-9a-f][0-9a-f]/[0-9a-f][0-9a-f]/*"))) == 1
