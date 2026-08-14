from __future__ import annotations

import hashlib
import json
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from rk.cas import ContentAddressedStore
from rk.migrations import MigrationRunner
from rk.product.artifact_upload import SQLiteArtifactRegistry
from rk.product.backup import BackupService, CasBackupArtifactReader, read_backup_manifest
from rk.product.restore import RestoreRunner
from rk.product.upgrade import UpgradeRunner
from rk.product_release_migrations import ProductReleaseMigrationAssembler
from rk.storage import SQLiteStorage

ROOT = Path(__file__).parents[1]
NOW = datetime(2026, 8, 14, 12, tzinfo=UTC)


class Ids:
    def __init__(self) -> None:
        self.value = 0

    def new(self) -> str:
        self.value += 1
        return f"id-{self.value}"


def release(
    fragment_root: Path = ROOT / "schema_fragments",
    manifest: Path = ROOT / "docs/spec/product/migration-manifest.json",
    lock: Path = ROOT / "migrations/release/current.lock",
) -> ProductReleaseMigrationAssembler:
    return ProductReleaseMigrationAssembler(
        fragment_root=fragment_root,
        manifest_path=manifest,
        lock_path=lock,
    )


def database(tmp_path: Path) -> Path:
    db = tmp_path / "product.sqlite"
    MigrationRunner(db, ROOT / "migrations", 5_000, now=lambda: "2026-08-14T12:00:00Z").migrate()
    with sqlite3.connect(db, isolation_level=None) as connection:
        release().apply(connection)
    return db


def services(tmp_path: Path):
    db = database(tmp_path)
    cas_root = tmp_path / "cas"
    work_root = tmp_path / "backup-work"
    work_root.mkdir()
    ids = Ids()
    storage = SQLiteStorage(db, 5_000)
    cas = ContentAddressedStore(
        cas_root,
        max_bytes=32 * 1024 * 1024,
        inbox_roots=(work_root,),
        orphan_grace_seconds=60,
        id_generator=ids,
        artifact_lookup=lambda artifact_id: storage.get_artifact(artifact_id),
    )
    registry = SQLiteArtifactRegistry(storage)
    return db, cas_root, work_root, ids, storage, cas, registry


def test_online_backup_restore_and_next_release_preserve_exact_fences(tmp_path: Path) -> None:
    db, cas_root, work_root, ids, _storage, cas, registry = services(tmp_path)
    evidence = cas.commit(
        cas.stage_bytes(b"proof-output", media_type="text/plain", source_name="proof.txt"),
        now=NOW,
    )
    evidence_ref = registry.register(evidence)
    with sqlite3.connect(db) as connection:
        connection.execute(
            "INSERT INTO product_activity_events(event_id,scope_kind,run_id,deployment_id,"
            "source,research_revision,kernel_event_id,entity_refs,payload_json,recorded_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                "event-1",
                "DEPLOYMENT",
                None,
                "deployment-1",
                "TEST",
                None,
                None,
                "[]",
                "{}",
                "2026-08-14T12:00:00Z",
            ),
        )
        connection.execute(
            "INSERT INTO product_receipts(receipt_id,receipt_version,scope_key,request_id,"
            "request_digest,state,receipt_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                "receipt-1",
                1,
                "deployment:deployment-1",
                "job-request",
                "a" * 64,
                "DECIDED",
                "{}",
                "2026-08-14T12:00:00Z",
                "2026-08-14T12:00:00Z",
            ),
        )
        connection.execute(
            "INSERT INTO product_jobs(job_id,receipt_id,scope_key,scope_kind,run_id,deployment_id,"
            "kind,requested_by,request_id,state,retry_safety,idempotency_key,result_refs_json,"
            "authority_effect,created_at,finished_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "job-1",
                "receipt-1",
                "deployment:deployment-1",
                "DEPLOYMENT",
                None,
                "deployment-1",
                "BACKUP_TEST",
                "admin",
                "job-request",
                "SUCCEEDED",
                "IDEMPOTENT",
                None,
                "[]",
                "NONE",
                "2026-08-14T12:00:00Z",
                "2026-08-14T12:00:01Z",
            ),
        )
        connection.execute(
            "INSERT INTO product_job_checkpoints(checkpoint_id,job_id,research_revision,"
            "contract_version,artifact_id,checkpoint_digest,state,invalidation_reason,"
            "created_at,invalidated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                "checkpoint-1",
                "job-1",
                0,
                1,
                evidence_ref.artifact_id,
                "b" * 64,
                "ACTIVE",
                None,
                "2026-08-14T12:00:00Z",
                None,
            ),
        )
    config = tmp_path / "deployment.json"
    config.write_text('{"deployment_id":"deployment-1","required_tool":"lean"}\n')
    backup = BackupService(
        db_path=db,
        cas_root=cas_root,
        work_root=work_root,
        cas=cas,
        registry=registry,
        id_generator=ids.new,
        clock=lambda: NOW,
    )
    first = backup.create(
        deployment_id="deployment-1",
        request_id="backup-request",
        include_cas=True,
        include_configuration=True,
        configuration_files={"deployment.json": config.resolve()},
    )
    assert (
        backup.create(
            deployment_id="deployment-1",
            request_id="backup-request",
            include_cas=True,
            include_configuration=True,
            configuration_files={"deployment.json": config.resolve()},
        )
        == first
    )
    manifest = read_backup_manifest(cas.read_bytes(first.artifact.artifact_id))
    assert manifest["consistency"] == {
        "activity_cursor": 1,
        "checkpoint_count": 1,
        "job_count": 1,
        "terminal_job_count": 1,
    }
    assert manifest["cas_objects"][0]["artifact_id"] == evidence_ref.artifact_id

    restored_root = (tmp_path / "restored").resolve()
    restored = RestoreRunner(
        tracking_db_path=db,
        artifact_reader=CasBackupArtifactReader(cas),
        release=release(),
        id_generator=ids.new,
        clock=lambda: NOW,
    ).restore(
        source_backup_id=first.backup_id,
        backup_artifact=first.artifact,
        deployment_id="deployment-restored",
        request_id="restore-request",
        new_data_root=restored_root,
    )
    assert restored.activity_cursor == 1
    assert restored.job_count == 1
    assert restored.checkpoint_count == 1
    assert (restored_root / "configuration/deployment.json").read_bytes() == config.read_bytes()
    copied = restored_root / "cas" / str(manifest["cas_objects"][0]["cas_relpath"])
    assert copied.read_bytes() == b"proof-output"
    with sqlite3.connect(restored_root / "database.sqlite") as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("SELECT state FROM product_jobs").fetchone() == ("SUCCEEDED",)
        assert connection.execute("SELECT state FROM product_job_checkpoints").fetchone() == (
            "ACTIVE",
        )


def test_upgrade_runner_applies_only_appended_d00b_release_after_backup(tmp_path: Path) -> None:
    db, cas_root, work_root, ids, _storage, cas, registry = services(tmp_path)
    backup = BackupService(
        db_path=db,
        cas_root=cas_root,
        work_root=work_root,
        cas=cas,
        registry=registry,
        id_generator=ids.new,
        clock=lambda: NOW,
    ).create(
        deployment_id="deployment-1",
        request_id="backup-before-upgrade",
        include_cas=False,
        include_configuration=False,
        configuration_files={},
    )
    fragments = tmp_path / "next-fragments"
    shutil.copytree(ROOT / "schema_fragments", fragments)
    extra = fragments / "B19a"
    extra.mkdir()
    (extra / "upgrade_probe.sql").write_text(
        "CREATE TABLE product_upgrade_probe(id TEXT PRIMARY KEY) STRICT;\n"
    )
    current = json.loads((ROOT / "docs/spec/product/migration-manifest.json").read_text())
    current["release_id"] = "rk-product-next"
    raw_fragment = (extra / "upgrade_probe.sql").read_bytes()
    current["fragments"].append(
        {
            "release_position": 32,
            "fragment_id": "B19a/upgrade_probe",
            "sha256": hashlib.sha256(raw_fragment).hexdigest(),
        }
    )
    manifest = tmp_path / "next-manifest.json"
    raw = (json.dumps(current, indent=2) + "\n").encode()
    manifest.write_bytes(raw)
    lock = tmp_path / "next.lock"
    lock.write_text(hashlib.sha256(raw).hexdigest() + "\n")
    runner = UpgradeRunner(
        db_path=db,
        release=release(fragments, manifest, lock),
        id_generator=ids.new,
        clock=lambda: NOW,
    )
    preflight = runner.preflight()
    assert preflight.pending_fragment_ids == ("B19a/upgrade_probe",)
    receipt = runner.execute(
        deployment_id="deployment-1",
        request_id="upgrade-request",
        backup_id=backup.backup_id,
    )
    assert receipt.fragments_before == 31
    assert receipt.fragments_after == 32
    assert (
        runner.execute(
            deployment_id="deployment-1",
            request_id="upgrade-request",
            backup_id=backup.backup_id,
        )
        == receipt
    )
    with sqlite3.connect(db) as connection:
        assert connection.execute(
            "SELECT assembly_position FROM product_schema_fragments "
            "WHERE package='B19a' AND slug='upgrade_probe'"
        ).fetchone() == (32,)
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
