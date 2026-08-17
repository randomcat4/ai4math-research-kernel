from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from rk.product.adapters import ProductWireError, command_from_json
from rk.product.identity import (
    IdentityAuthenticationError,
    IdentityStore,
    ProductRole,
    role_actions,
)
from rk.product_migrations import ProductMigrationAssembler, ProductMigrationRegistry


def _database(tmp_path: Path) -> Path:
    path = tmp_path / "product.sqlite"
    with sqlite3.connect(path, isolation_level=None) as connection:
        ProductMigrationAssembler(ProductMigrationRegistry(Path("schema_fragments"))).apply(connection)
    return path


def test_eight_roles_have_frozen_narrow_product_capabilities() -> None:
    assert set(ProductRole) == {
        ProductRole.MAIN,
        ProductRole.LITERATURE_REVIEWER,
        ProductRole.WORKER,
        ProductRole.MACHINE_VERIFIER,
        ProductRole.PEER_REVIEWER,
        ProductRole.PAPER_REVIEWER,
        ProductRole.PUBLICATION_WORKER,
        ProductRole.ADMIN,
    }
    assert all("*" not in role_actions(role) for role in ProductRole)
    assert "FINALIZE_RESEARCH" in role_actions(ProductRole.MAIN)
    assert "FINALIZE_RESEARCH" not in role_actions(ProductRole.WORKER)
    assert "SUBMIT_CLAIM" in role_actions(ProductRole.WORKER)
    assert "SUBMIT_REVIEW" in role_actions(ProductRole.PEER_REVIEWER)
    assert "SUBMIT_REVIEW" not in role_actions(ProductRole.PAPER_REVIEWER)
    assert "SUBMIT_PAPER_REVIEW" in role_actions(ProductRole.PAPER_REVIEWER)
    assert "GENERATE_CANDIDATE_TEX" in role_actions(ProductRole.PUBLICATION_WORKER)
    assert "DEPLOYMENT_OPERATION" in role_actions(ProductRole.ADMIN)
    assert "CREATE_RESEARCH" not in role_actions(ProductRole.ADMIN)
    assert all(role.value != "GATEWAY" for role in ProductRole)


def test_identity_login_secret_is_verified_and_disabled_identity_cannot_login(
    tmp_path: Path,
) -> None:
    store = IdentityStore(_database(tmp_path), lambda: b"a" * 16)
    identity = store.register(
        identity_id="main-one",
        subject_id="subject:main-one",
        display_name="Main One",
        role=ProductRole.MAIN,
        capability_id="cap:main-one",
        login_secret="main-login-secret",
        now="2026-08-13T00:00:00Z",
    )

    assert store.authenticate("main-one", "main-login-secret") == identity
    with pytest.raises(IdentityAuthenticationError):
        store.authenticate("main-one", "wrong-login-secret")
    store.disable("main-one", now="2026-08-13T00:01:00Z")
    with pytest.raises(IdentityAuthenticationError):
        store.authenticate("main-one", "main-login-secret")


@pytest.mark.parametrize(
    "payload",
    [
        {"role": "ADMIN"},
        {"nested": {"capability": "cap:any"}},
        {"items": [{"capability_id": "cap:any"}]},
        {"principal_subject_id": "subject:admin"},
    ],
)
def test_raw_http_command_cannot_forge_session_identity(payload: dict[str, object]) -> None:
    command = {
        "schema_version": "rk.product.command.v1",
        "request_id": "request-1",
        "scope": {
            "kind": "GLOBAL",
            "deployment_id": "deployment-1",
            "expected_deployment_revision": 0,
        },
        "command": {"type": "CREATE_RESEARCH", "payload": payload},
    }

    with pytest.raises(ProductWireError, match="identity field is forbidden"):
        command_from_json(command)  # type: ignore[arg-type]
