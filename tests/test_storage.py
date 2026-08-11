import hashlib
import sqlite3
from pathlib import Path

import pytest

from rk.domain import VerifiedCapability
from rk.migrations import MigrationRunner
from rk.storage import RevisionConflict, SQLiteStorage, StorageConflict

MIGRATIONS = Path(__file__).parents[1] / "migrations"
NOW = "2026-08-11T12:00:00.000Z"


def _storage(tmp_path: Path) -> SQLiteStorage:
    db_path = tmp_path / "rk.sqlite"
    MigrationRunner(db_path, MIGRATIONS, 750, now=lambda: NOW).migrate()
    return SQLiteStorage(db_path, 750)


def _capability(identifier: str = "cap-1") -> VerifiedCapability:
    return VerifiedCapability(
        capability_id=identifier,
        subject_id="tester",
        issuer="test-host",
        allowed_actions=frozenset({"*"}),
        run_scope=frozenset({"*"}),
        issued_at="2026-08-11T11:00:00.000Z",
        expires_at="2026-08-12T11:00:00.000Z",
    )


def _artifact(identifier: str, content: bytes) -> dict[str, object]:
    digest = hashlib.sha256(content).hexdigest()
    return {
        "artifact_id": identifier,
        "sha256": digest,
        "byte_count": len(content),
        "media_type": "application/json",
        "cas_relpath": f"{digest[:2]}/{digest[2:4]}/{digest}",
        "ingest_state": "COMMITTED",
        "created_at": NOW,
        "committed_at": NOW,
    }


def _create(storage: SQLiteStorage) -> dict[str, object]:
    capability = _capability()
    storage.ensure_capability(capability)
    return storage.create_run_atomic(
        run_id="run-1",
        stable_project_id="project-1",
        create_issuer=capability.issuer,
        create_request_id="create-1",
        create_request_digest="a" * 64,
        capability_id=capability.capability_id,
        contract_artifact=_artifact("artifact-contract", b'{"contract":1}'),
        additional_artifacts=(_artifact("artifact-complete", b'{"complete":true}'),),
        contract_json={"statement": "test", "fields_complete": True},
        statement_hash="b" * 64,
        created_at=NOW,
    )


def test_connection_pragmas_and_atomic_idempotent_create(tmp_path: Path) -> None:
    storage = _storage(tmp_path)
    handle = _create(storage)

    with storage.connect() as connection:
        assert connection.execute("PRAGMA foreign_keys").fetchone()[0] == 1
        assert connection.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
        assert connection.execute("PRAGMA busy_timeout").fetchone()[0] == 750
    replay = storage.create_run_atomic(
        run_id="ignored-on-replay",
        stable_project_id="project-1",
        create_issuer="test-host",
        create_request_id="create-1",
        create_request_digest="a" * 64,
        capability_id="cap-1",
        contract_artifact=_artifact("unused", b"unused"),
        contract_json={"different": "ignored because digest is authoritative"},
        statement_hash="c" * 64,
        created_at=NOW,
    )

    assert handle == replay
    assert storage.get_artifact("artifact-complete") is not None
    with pytest.raises(StorageConflict, match="different content"):
        storage.create_run_atomic(
            run_id="run-2",
            stable_project_id="project-1",
            create_issuer="test-host",
            create_request_id="create-1",
            create_request_digest="d" * 64,
            capability_id="cap-1",
            contract_artifact=_artifact("unused-2", b"unused-2"),
            contract_json={"statement": "x"},
            statement_hash="e" * 64,
            created_at=NOW,
        )


def test_command_event_revision_and_inspect_are_one_composable_transaction(
    tmp_path: Path,
) -> None:
    storage = _storage(tmp_path)
    _create(storage)
    receipt = {
        "schema_version": "rk.receipt.v1",
        "request_id": "request-1",
        "command_id": "command-1",
        "run_id": "run-1",
        "accepted": True,
        "revision_before": 0,
        "revision_after": 1,
        "event_ids": ["event-1"],
        "artifact_ids": [],
        "rejection_code": None,
        "missing_conditions": [],
        "decided_at": NOW,
    }
    with storage.transaction() as connection:
        storage.record_command(
            connection,
            command_id="command-1",
            run_id="run-1",
            request_id="request-1",
            command_type="FreezeContract",
            request_digest="f" * 64,
            expected_revision=0,
            capability_id="cap-1",
            accepted=True,
            revision_before=0,
            revision_after=1,
            rejection_code=None,
            missing_conditions=(),
            receipt=receipt,
            trace_id="trace-1",
            decided_at=NOW,
        )
        cursor = storage.append_event(
            connection,
            event_id="event-1",
            run_id="run-1",
            command_id="command-1",
            revision=1,
            event_type="CONTRACT_FROZEN",
            payload={"checkpoint_artifact_id": "artifact-complete"},
            recorded_at=NOW,
            contract_version=1,
        )
        assert storage.advance_revision(
            connection, "run-1", 0, updated_at=NOW
        ) == 1
    assert cursor == 1

    found = storage.find_command("run-1", "request-1")
    assert found == {"request_digest": "f" * 64, "receipt": receipt}
    page = storage.event_page("run-1", 0, 1)
    assert page["events"][0]["payload"]["checkpoint_artifact_id"] == "artifact-complete"
    assert page["next_cursor"] == 1
    assert page["has_more"] is False
    snapshot = storage.inspect_snapshot("run-1")
    assert snapshot["revision"] == 1
    assert snapshot["last_cursor"] == 1
    guard = storage.guard_snapshot("run-1")
    assert guard["projection"]["contract"]["fields_complete"] is True
    assert guard["projection"]["artifacts"][0]["status"] == "COMMITTED"
    with (
        pytest.raises(sqlite3.IntegrityError, match="append-only"),
        storage.transaction() as connection,
    ):
        connection.execute("UPDATE events SET event_type='CHANGED' WHERE event_id='event-1'")


def test_append_only_and_revision_compare_and_set_are_enforced(tmp_path: Path) -> None:
    storage = _storage(tmp_path)
    _create(storage)
    receipt = {
        "request_id": "r",
        "command_id": "c",
        "run_id": "run-1",
        "accepted": False,
    }
    with storage.transaction() as connection:
        storage.record_command(
            connection,
            command_id="command-rejected",
            run_id="run-1",
            request_id="request-rejected",
            command_type="StartRun",
            request_digest="1" * 64,
            expected_revision=0,
            capability_id="cap-1",
            accepted=False,
            revision_before=0,
            revision_after=0,
            rejection_code="CONTRACT_NOT_FROZEN",
            missing_conditions=({"code": "CONTRACT_STATE"},),
            receipt=receipt,
            trace_id="trace",
            decided_at=NOW,
        )
    with (
        pytest.raises(sqlite3.IntegrityError, match="append-only"),
        storage.transaction() as connection,
    ):
        connection.execute(
            "UPDATE commands SET trace_id='changed' WHERE command_id='command-rejected'"
        )
    with pytest.raises(RevisionConflict), storage.transaction() as connection:
        storage.advance_revision(connection, "run-1", 9, updated_at=NOW)
    assert storage.get_run("run-1")["revision"] == 0


def test_transaction_rolls_back_on_base_exception_and_capabilities_do_not_collide(
    tmp_path: Path,
) -> None:
    storage = _storage(tmp_path)
    storage.ensure_capability(_capability("cap-1"))
    storage.ensure_capability(_capability("cap-2"))
    with pytest.raises(KeyboardInterrupt), storage.transaction() as connection:
        connection.execute(
            "INSERT INTO artifacts(artifact_id, sha256, byte_count, media_type, cas_relpath, "
            "ingest_state, created_at, committed_at) "
            "VALUES ('rolled-back', ?, 0, 'text/plain', 'aa/bb/value', "
            "'COMMITTED', ?, ?)",
            ("9" * 64, NOW, NOW),
        )
        raise KeyboardInterrupt
    assert storage.get_artifact("rolled-back") is None


def test_artifact_digest_deduplication_returns_canonical_record(tmp_path: Path) -> None:
    storage = _storage(tmp_path)
    first = _artifact("artifact-1", b"same")
    second = _artifact("artifact-2", b"same")
    with storage.transaction() as connection:
        canonical_first = storage.insert_artifact(connection, first)
        canonical_second = storage.insert_artifact(connection, second)
    assert canonical_first["artifact_id"] == canonical_second["artifact_id"] == "artifact-1"
    assert storage.get_artifact_by_sha256(str(first["sha256"]))["artifact_id"] == "artifact-1"
