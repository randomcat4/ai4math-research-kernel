from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from rk.domain import CapabilityError
from rk.product.api import ProductSession
from rk.product.identity import IdentityStore, ProductRole
from rk.product.sessions import SessionAuthenticationError, SessionCapabilitySource, SessionStore
from rk.product_migrations import ProductMigrationAssembler, ProductMigrationRegistry


def _stores(tmp_path: Path) -> tuple[IdentityStore, SessionStore]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    path = tmp_path / "product.sqlite"
    with sqlite3.connect(path, isolation_level=None) as connection:
        ProductMigrationAssembler(ProductMigrationRegistry(Path("schema_fragments"))).apply(connection)
    salts = iter((b"a" * 16, b"b" * 16, b"c" * 16, b"d" * 16))
    identities = IdentityStore(path, salts.__next__)
    sessions = SessionStore(path, identities, iter(("session-1",)).__next__, "org-one")
    identities.register(
        identity_id="main-one",
        subject_id="subject:main-one",
        display_name="Main One",
        role=ProductRole.MAIN,
        capability_id="cap:main-one",
        login_secret="main-login-secret",
        now="2026-08-13T00:00:00Z",
    )
    identities.register(
        identity_id="worker-one",
        subject_id="subject:worker-one",
        display_name="Worker One",
        role=ProductRole.WORKER,
        capability_id="cap:worker-one",
        login_secret="worker-login-secret",
        now="2026-08-13T00:00:00Z",
    )
    identities.register(
        identity_id="reviewer-one",
        subject_id="subject:reviewer-one",
        display_name="Reviewer One",
        role=ProductRole.PEER_REVIEWER,
        capability_id="cap:reviewer-one",
        login_secret="reviewer-secret-1",
        now="2026-08-13T00:00:00Z",
    )
    identities.register(
        identity_id="admin-one",
        subject_id="subject:admin-one",
        display_name="Admin One",
        role=ProductRole.ADMIN,
        capability_id="cap:admin-one",
        login_secret="admin-login-secret",
        now="2026-08-13T00:00:00Z",
    )
    return identities, sessions


def test_two_authenticated_identities_switch_one_session_principal(tmp_path: Path) -> None:
    _, sessions = _stores(tmp_path)
    main = sessions.login(
        identity_id="main-one",
        login_secret="main-login-secret",
        now="2026-08-13T00:00:01Z",
        expires_at="2026-08-14T00:00:00Z",
    )
    worker = sessions.login(
        session_id=main.session_id,
        identity_id="worker-one",
        login_secret="worker-login-secret",
        now="2026-08-13T00:00:03Z",
        expires_at="2026-08-14T00:00:00Z",
    )

    assert worker.identity_id == "worker-one"
    assert worker.linked_identity_ids == ("main-one", "worker-one")
    assert sessions.derive(main.session_id, now="2026-08-13T00:00:04Z") == ProductSession(
        "session-1", "subject:worker-one", ("cap:worker-one",)
    )
    worker_session = sessions.derive(main.session_id, now="2026-08-13T00:00:04Z")
    switched = sessions.switch(
        main.session_id,
        "main-one",
        now="2026-08-13T00:00:05Z",
    )
    assert switched.principal_subject_id == "subject:main-one"
    assert switched.session_version == 3

    source = SessionCapabilitySource(sessions, lambda: "2026-08-13T00:00:06Z")
    with pytest.raises(CapabilityError):
        source.resolve(worker_session, action="FINALIZE_RESEARCH", run_id="run-1")
    current = sessions.derive(main.session_id, now="2026-08-13T00:00:06Z")
    capability = source.resolve(current, action="FINALIZE_RESEARCH", run_id="run-1")
    assert capability.subject_id == "subject:main-one"
    assert capability.run_scope == frozenset({"run-1"})
    assert "*" not in capability.allowed_actions


def test_unlinked_identity_switch_and_role_escalation_are_rejected(tmp_path: Path) -> None:
    _, sessions = _stores(tmp_path)
    view = sessions.login(
        identity_id="worker-one",
        login_secret="worker-login-secret",
        now="2026-08-13T00:00:01Z",
        expires_at="2026-08-14T00:00:00Z",
    )
    with pytest.raises(SessionAuthenticationError, match="NOT_AUTHENTICATED"):
        sessions.switch(view.session_id, "admin-one", now="2026-08-13T00:00:02Z")

    session = sessions.derive(view.session_id, now="2026-08-13T00:00:02Z")
    source = SessionCapabilitySource(sessions, lambda: "2026-08-13T00:00:02Z")
    with pytest.raises(CapabilityError):
        source.resolve(session, action="Finalize", run_id="run-1")
    with pytest.raises(CapabilityError):
        source.resolve(session, action="DeploymentOperation", run_id=None)


def test_logout_and_expiry_remove_session_authority(tmp_path: Path) -> None:
    _, sessions = _stores(tmp_path)
    view = sessions.login(
        identity_id="main-one",
        login_secret="main-login-secret",
        now="2026-08-13T00:00:01Z",
        expires_at="2026-08-13T00:01:00Z",
    )
    sessions.logout(view.session_id, now="2026-08-13T00:00:30Z")
    with pytest.raises(SessionAuthenticationError, match="NOT_ACTIVE"):
        sessions.derive(view.session_id, now="2026-08-13T00:00:31Z")

    _, expiring = _stores(tmp_path / "second")
    expired = expiring.login(
        identity_id="main-one",
        login_secret="main-login-secret",
        now="2026-08-13T00:00:01Z",
        expires_at="2026-08-13T00:01:00Z",
    )
    with pytest.raises(SessionAuthenticationError, match="NOT_ACTIVE"):
        expiring.derive(expired.session_id, now="2026-08-13T00:01:00Z")


def test_product_session_contains_derived_principal_but_no_role_field(tmp_path: Path) -> None:
    _, sessions = _stores(tmp_path)
    view = sessions.login(
        identity_id="reviewer-one",
        login_secret="reviewer-secret-1",
        now="2026-08-13T00:00:01Z",
        expires_at="2026-08-14T00:00:00Z",
    )
    session = sessions.derive(view.session_id, now="2026-08-13T00:00:02Z")

    assert session.principal_subject_id == "subject:reviewer-one"
    assert session.capability_ids == ("cap:reviewer-one",)
    assert not hasattr(session, "role")
