from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]
B05A = ROOT / "schema_fragments/B05a/identity.sql"
B05B = ROOT / "schema_fragments/B05b/reviews.sql"
B05C = ROOT / "schema_fragments/B05c/identity_roles.sql"


def test_role_upgrade_preserves_identity_session_review_and_foreign_keys(
    tmp_path: Path,
) -> None:
    path = tmp_path / "identity-upgrade.sqlite"
    with sqlite3.connect(path, isolation_level=None) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.executescript(B05A.read_text(encoding="utf-8"))
        connection.executescript(B05B.read_text(encoding="utf-8"))
        connection.execute(
            "INSERT INTO product_identities VALUES(?,?,?,?,?,?,?,?,?,?)",
            (
                "reviewer-one",
                "subject:reviewer-one",
                "Reviewer One",
                "REVIEWER",
                "cap:reviewer-one",
                b"s" * 16,
                b"d" * 32,
                1,
                "2026-08-13T00:00:00Z",
                None,
            ),
        )
        connection.execute(
            "INSERT INTO product_sessions VALUES(?,?,?,?,?,?,?)",
            (
                "session-one",
                "organization-one",
                "reviewer-one",
                3,
                "2026-08-13T00:00:00Z",
                "2026-08-14T00:00:00Z",
                None,
            ),
        )
        connection.execute(
            "INSERT INTO product_session_identities VALUES(?,?,?)",
            ("session-one", "reviewer-one", "2026-08-13T00:00:00Z"),
        )
        connection.execute(
            "INSERT INTO product_review_tasks("
            "review_task_id,review_type,binding_json,author_subject_ids_json,"
            "assignee_identity_id,created_at,expires_at) VALUES(?,?,?,?,?,?,?)",
            (
                "review-one",
                "ATOMIC",
                '{"run_id":"run-one","target_digest":"abc"}',
                '["subject:author"]',
                "reviewer-one",
                "2026-08-13T00:00:00Z",
                "2026-08-14T00:00:00Z",
            ),
        )

        connection.executescript(B05C.read_text(encoding="utf-8"))

        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute(
            "SELECT identity_id,subject_id,display_name,role,capability_id,"
            "credential_salt,credential_digest,enabled,created_at,disabled_at "
            "FROM product_identities"
        ).fetchone() == (
            "reviewer-one",
            "subject:reviewer-one",
            "Reviewer One",
            "PEER_REVIEWER",
            "cap:reviewer-one",
            b"s" * 16,
            b"d" * 32,
            1,
            "2026-08-13T00:00:00Z",
            None,
        )
        assert connection.execute("SELECT * FROM product_sessions").fetchone() == (
            "session-one",
            "organization-one",
            "reviewer-one",
            3,
            "2026-08-13T00:00:00Z",
            "2026-08-14T00:00:00Z",
            None,
        )
        assert connection.execute(
            "SELECT session_id,identity_id,authenticated_at "
            "FROM product_session_identities"
        ).fetchone() == (
            "session-one",
            "reviewer-one",
            "2026-08-13T00:00:00Z",
        )
        assert connection.execute(
            "SELECT review_task_id,assignee_identity_id,status FROM product_review_tasks"
        ).fetchone() == ("review-one", "reviewer-one", "OPEN")

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "INSERT INTO product_identities VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    "legacy-reviewer",
                    "subject:legacy-reviewer",
                    "Legacy Reviewer",
                    "REVIEWER",
                    "cap:legacy-reviewer",
                    b"s" * 16,
                    b"d" * 32,
                    1,
                    "2026-08-13T00:00:00Z",
                    None,
                ),
            )
