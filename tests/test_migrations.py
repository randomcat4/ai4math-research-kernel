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
from rk.wire import WireValidator

SOURCE_MIGRATIONS = Path(__file__).parents[1] / "migrations"


def _copy_migrations(target: Path) -> Path:
    destination = target / "migrations"
    destination.mkdir()
    for source in SOURCE_MIGRATIONS.glob("*.sql"):
        shutil.copy2(source, destination / source.name)
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
        assert connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone() == (12,)


def test_v02_migration_revokes_materialized_unmanaged_human_authority() -> None:
    connection = sqlite3.connect(":memory:")
    connection.executescript(
        """
        CREATE TABLE capabilities(
          capability_id TEXT PRIMARY KEY, subject_id TEXT, issuer TEXT, key_id TEXT,
          allowed_actions_json TEXT, run_scope_json TEXT, nonce TEXT,
          credential_digest TEXT, issued_at TEXT, expires_at TEXT
        );
        CREATE TABLE runs(
          run_id TEXT PRIMARY KEY, status TEXT, final_outcome TEXT, closed_at TEXT,
          parent_dossier_artifact_id TEXT, revision INTEGER, updated_at TEXT
        );
        CREATE TABLE commands(
          command_id TEXT PRIMARY KEY, run_id TEXT, request_id TEXT, command_type TEXT,
          request_digest TEXT, expected_revision INTEGER, capability_id TEXT, accepted INTEGER,
          revision_before INTEGER, revision_after INTEGER, rejection_code TEXT,
          missing_conditions_json TEXT, receipt_json TEXT, trace_id TEXT, decided_at TEXT
        );
        CREATE TABLE events(
          event_id TEXT PRIMARY KEY, run_id TEXT, command_id TEXT, revision INTEGER,
          event_type TEXT, payload_json TEXT, recorded_at TEXT
        );
            CREATE TABLE claims(
              claim_id TEXT PRIMARY KEY, run_id TEXT, claim_kind TEXT,
              lifecycle_status TEXT, peer_verdict TEXT, route_result TEXT,
              closure_state TEXT, semantic_verdict TEXT, machine_verdict TEXT,
              quality_verdict TEXT
        );
        CREATE TABLE closure_witnesses(
          witness_id TEXT PRIMARY KEY, composition_mode TEXT, status TEXT
        );
        CREATE TABLE composition_obligations(obligation_id TEXT PRIMARY KEY, status TEXT);
        CREATE TABLE claim_edges(
          edge_id TEXT PRIMARY KEY, justification_kind TEXT, status TEXT
        );
        CREATE TABLE bridges(bridge_id TEXT PRIMARY KEY, directionality TEXT);
        CREATE TABLE routes(
          route_id TEXT PRIMARY KEY, run_id TEXT, status TEXT, updated_at TEXT
        );
        CREATE TABLE attempts(
          attempt_id TEXT PRIMARY KEY, run_id TEXT, status TEXT, started_at TEXT, ended_at TEXT
        );
        CREATE TABLE leases(
          lease_id TEXT PRIMARY KEY, attempt_id TEXT, status TEXT, released_at TEXT
        );
        CREATE TABLE budget_events(
          budget_event_id TEXT PRIMARY KEY, run_id TEXT, event_kind TEXT,
          provider_usage_json TEXT
        );
        INSERT INTO runs VALUES (
          '11111111-1111-4111-8111-111111111111','CLOSED','PROVED',
          '2026-08-11T12:00:00Z','old-dossier',7,'old'
        );
        INSERT INTO runs VALUES (
          '22222222-2222-4222-8222-222222222222','CLOSED','PROVED',
          '2026-08-11T12:00:00Z','machine-dossier',7,'old'
        );
        INSERT INTO runs VALUES (
          '33333333-3333-4333-8333-333333333333','OPEN',NULL,NULL,NULL,7,'old'
        );
        INSERT INTO runs VALUES (
          '44444444-4444-4444-8444-444444444444','RUNNING',NULL,NULL,NULL,7,'old'
        );
        INSERT INTO runs VALUES (
          '55555555-5555-4555-8555-555555555555','PAUSED',NULL,NULL,NULL,7,'old'
        );
        INSERT INTO claims VALUES (
          'human','11111111-1111-4111-8111-111111111111','ROOT','ACTIVE','ACCEPTED',
          'ROUTE_PROVED','CLOSED_HUMAN',
          'HUMAN_ATTESTED','UNVERIFIED','ACCEPTED'
        );
        INSERT INTO claims VALUES (
          'open-root','33333333-3333-4333-8333-333333333333','ROOT','ACTIVE',
          'UNREVIEWED','UNASSESSED','OPEN',
          'UNREVIEWED','UNVERIFIED','UNREVIEWED'
        );
        INSERT INTO claims VALUES (
          'running-root','44444444-4444-4444-8444-444444444444','ROOT','ACTIVE',
          'UNREVIEWED','UNASSESSED','OPEN',
          'UNREVIEWED','UNVERIFIED','UNREVIEWED'
        );
        INSERT INTO claims VALUES (
          'paused-root','55555555-5555-4555-8555-555555555555','ROOT','ACTIVE',
          'UNREVIEWED','UNASSESSED','OPEN',
          'UNREVIEWED','UNVERIFIED','UNREVIEWED'
        );
        INSERT INTO routes VALUES (
          'old-route','44444444-4444-4444-8444-444444444444','ACTIVE','old'
        );
        INSERT INTO routes VALUES (
          'proved-route','11111111-1111-4111-8111-111111111111','PROVED','old'
        );
        INSERT INTO routes VALUES (
          'refuted-route','22222222-2222-4222-8222-222222222222','REFUTED','old'
        );
        INSERT INTO attempts VALUES (
          'queued','44444444-4444-4444-8444-444444444444','QUEUED',NULL,NULL
        );
        INSERT INTO attempts VALUES (
          'running','44444444-4444-4444-8444-444444444444','RUNNING','old',NULL
        );
        INSERT INTO attempts VALUES (
          'paused','55555555-5555-4555-8555-555555555555','PAUSED','old',NULL
        );
        INSERT INTO leases VALUES ('lease','running','ACTIVE',NULL);
        INSERT INTO budget_events VALUES (
          'usage','44444444-4444-4444-8444-444444444444','ACTUAL',
          '{"component":"legacy-model","input_tokens":99,"wall_time_ms":123}'
        );
        INSERT INTO budget_events VALUES (
          'reservation','44444444-4444-4444-8444-444444444444','RESERVATION',
          '{"component":"legacy-model","input_tokens":99,"wall_time_ms":123}'
        );
        INSERT INTO budget_events VALUES (
          'refund','44444444-4444-4444-8444-444444444444','REFUND',
          '{"component":"legacy-model","input_tokens":99,"wall_time_ms":123}'
        );
        INSERT INTO budget_events VALUES (
          'fuse','44444444-4444-4444-8444-444444444444','FUSE_TRIP',
          '{"component":"legacy-model"}'
        );
        INSERT INTO claims VALUES (
          'machine','22222222-2222-4222-8222-222222222222','ROOT','ACTIVE','UNREVIEWED',
          'ROUTE_PROVED','CLOSED_MACHINE',
          'TESTED','KERNEL_VERIFIED','UNREVIEWED'
        );
        INSERT INTO closure_witnesses VALUES ('peer','PEER','ACCEPTED');
        INSERT INTO closure_witnesses VALUES ('kernel','MACHINE','ACCEPTED');
        INSERT INTO composition_obligations VALUES ('human','DISCHARGED_HUMAN');
        INSERT INTO composition_obligations VALUES ('machine','DISCHARGED_MACHINE');
        INSERT INTO claim_edges VALUES ('human','HUMAN_ARGUMENT','ACTIVE');
        INSERT INTO claim_edges VALUES ('machine','LEAN_DECLARATION','ACTIVE');
        INSERT INTO bridges VALUES ('human','EQUIVALENT_VALID');
        INSERT INTO bridges VALUES ('candidate','CANDIDATE');
        """
    )

    connection.executescript((SOURCE_MIGRATIONS / "0004.sql").read_text(encoding="utf-8"))

    assert connection.execute(
        "SELECT peer_verdict,route_result,closure_state,semantic_verdict,machine_verdict,"
        "quality_verdict,lifecycle_status "
        "FROM claims WHERE claim_id='human'"
    ).fetchone() == (
        "UNREVIEWED", "CANDIDATE", "OPEN", "UNREVIEWED", "UNVERIFIED", "UNREVIEWED",
        "INVALIDATED"
    )
    assert connection.execute(
        "SELECT peer_verdict,route_result,closure_state,semantic_verdict,machine_verdict,"
        "quality_verdict,lifecycle_status "
        "FROM claims WHERE claim_id='machine'"
    ).fetchone() == (
        "UNREVIEWED", "CANDIDATE", "OPEN", "TESTED", "UNVERIFIED", "UNREVIEWED",
        "INVALIDATED"
    )
    assert connection.execute(
        "SELECT status FROM closure_witnesses WHERE witness_id='peer'"
    ).fetchone() == ("INVALIDATED",)
    assert connection.execute(
        "SELECT status FROM closure_witnesses WHERE witness_id='kernel'"
    ).fetchone() == ("INVALIDATED",)
    assert connection.execute(
        "SELECT status FROM claim_edges WHERE edge_id='human'"
    ).fetchone() == ("INVALIDATED",)
    assert connection.execute(
        "SELECT status FROM claim_edges WHERE edge_id='machine'"
    ).fetchone() == ("INVALIDATED",)
    assert connection.execute(
        "SELECT directionality FROM bridges WHERE bridge_id='human'"
    ).fetchone() == ("CANDIDATE",)
    assert connection.execute(
        "SELECT status,final_outcome,closed_at,parent_dossier_artifact_id "
        "FROM runs WHERE run_id='11111111-1111-4111-8111-111111111111'"
    ).fetchone() == ("OPEN", None, None, None)
    assert connection.execute(
        "SELECT status,final_outcome,closed_at,parent_dossier_artifact_id "
        "FROM runs WHERE run_id='22222222-2222-4222-8222-222222222222'"
    ).fetchone() == ("OPEN", None, None, None)
    assert connection.execute(
        "SELECT run_id,status FROM runs ORDER BY run_id"
    ).fetchall() == [
        ("11111111-1111-4111-8111-111111111111", "OPEN"),
        ("22222222-2222-4222-8222-222222222222", "OPEN"),
        ("33333333-3333-4333-8333-333333333333", "OPEN"),
        ("44444444-4444-4444-8444-444444444444", "OPEN"),
        ("55555555-5555-4555-8555-555555555555", "OPEN"),
    ]
    assert connection.execute(
        "SELECT attempt_id,status FROM attempts ORDER BY attempt_id"
    ).fetchall() == [("paused", "ABORTED"), ("queued", "ABORTED"), ("running", "ABORTED")]
    assert connection.execute("SELECT status,released_at FROM leases").fetchone()[0] == "REVOKED"
    assert connection.execute(
        "SELECT DISTINCT status FROM routes"
    ).fetchall() == [("RETIRED",)]
    legacy_usage = connection.execute(
        "SELECT budget_event_id,provider_usage_json FROM budget_events ORDER BY budget_event_id"
    ).fetchall()
    assert {row[0] for row in legacy_usage} == {"fuse", "refund", "reservation", "usage"}
    assert all('"_rk_trust":"LEGACY_UNTRUSTED"' in row[1] for row in legacy_usage)
    assert connection.execute(
        "SELECT DISTINCT revision FROM runs"
    ).fetchall() == [(8,)]
    assert connection.execute(
        "SELECT event_type,revision FROM events ORDER BY run_id"
    ).fetchall() == [("AUTHORITY_REVALIDATED", 8)] * 5
    assert connection.execute(
        "SELECT expected_revision,revision_before,revision_after FROM commands "
        "ORDER BY run_id"
    ).fetchall() == [(7, 7, 8)] * 5
    validator = WireValidator(
        SOURCE_MIGRATIONS.parent / "docs/spec/json/command.schema.json",
        SOURCE_MIGRATIONS.parent / "docs/spec/json/receipt.schema.json",
    )
    for (receipt_json,) in connection.execute("SELECT receipt_json FROM commands"):
        validator.validate_receipt(__import__("json").loads(receipt_json))
    # Simulate a crash after the schema/DML commit but before the migration-ledger insert.
    # A retry must observe the system-command marker and make no second synthetic revision.
    before = connection.execute(
        "SELECT run_id,revision FROM runs ORDER BY run_id"
    ).fetchall()
    connection.executescript((SOURCE_MIGRATIONS / "0004.sql").read_text(encoding="utf-8"))
    assert connection.execute(
        "SELECT run_id,revision FROM runs ORDER BY run_id"
    ).fetchall() == before
    assert connection.execute(
        "SELECT COUNT(*) FROM commands WHERE command_type='SystemRevalidateAuthority'"
    ).fetchone() == (5,)
    assert connection.execute(
        "SELECT COUNT(*) FROM events WHERE event_type='AUTHORITY_REVALIDATED'"
    ).fetchone() == (5,)


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
        assert connection.execute("SELECT COUNT(*) FROM schema_migrations").fetchone() == (0,)
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
