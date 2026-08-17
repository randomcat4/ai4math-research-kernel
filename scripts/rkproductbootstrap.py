#!/usr/bin/env python3
"""Initialize one empty RK-PRODUCT-1.1 data root without demo business data."""

from __future__ import annotations

import argparse
import json
import os
import secrets
import sqlite3
import sys
import uuid
from collections.abc import Mapping, Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import cast

from rk.http.app import BootstrapAdmin
from rk.migrations import MigrationRunner
from rk.product.identity import IdentityStore, ProductRole
from rk.product.sessions import SessionStore
from rk.product_release_migrations import ProductReleaseMigrationAssembler
from rk.runtime import Uuid7Generator, format_utc

PRODUCT_VERSION = "RK-PRODUCT-1.1"
_DEFAULT_LIMITS: Mapping[str, int] = {
    "upload_bytes": 256 * 1024 * 1024,
    "upload_chunk_bytes": 4 * 1024 * 1024,
    "graph_nodes": 2_000,
    "log_tail_bytes": 1024 * 1024,
}


def _stable_id(kind: str, deployment_id: str, role: ProductRole) -> str:
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"rk:{deployment_id}:{kind}:{role.value}"))


def _write_private_json(path: Path, value: object) -> None:
    raw = (json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False) + "\n").encode()
    descriptor = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "wb", closefd=True) as writer:
            writer.write(raw)
            writer.flush()
            os.fsync(writer.fileno())
    except BaseException:
        path.unlink(missing_ok=True)
        raise
    os.chmod(path, 0o600)


def _require_empty_root(root: Path) -> None:
    if root.exists():
        if not root.is_dir() or root.is_symlink() or any(root.iterdir()):
            raise ValueError("bootstrap requires a genuinely empty data root")
    else:
        root.mkdir(parents=True, mode=0o700)
    os.chmod(root, 0o700)


def bootstrap(args: argparse.Namespace) -> Path:
    if not str(args.deployment_id).strip() or not str(args.organization_id).strip():
        raise ValueError("deployment and organization IDs must be non-empty")
    root = cast(Path, args.data_root).expanduser().resolve()
    _require_empty_root(root)
    cas_root = root / "cas"
    spool_root = root / "spool"
    cas_root.mkdir(mode=0o700)
    spool_root.mkdir(mode=0o700)
    (spool_root / "uploads").mkdir(mode=0o700)
    (spool_root / "logs").mkdir(mode=0o700)
    for directory in (cas_root, spool_root, spool_root / "uploads", spool_root / "logs"):
        os.chmod(directory, 0o700)

    db_path = root / "product.sqlite"
    MigrationRunner(db_path, args.migrations, args.busy_timeout_ms).migrate()
    release = ProductReleaseMigrationAssembler(
        fragment_root=args.schema_fragments,
        manifest_path=args.release_manifest,
        lock_path=args.release_lock,
    )
    with sqlite3.connect(db_path, isolation_level=None) as connection:
        applied = release.apply(connection)
        if len(applied) != len(release.manifest().fragments):
            raise RuntimeError("release migration assembler returned an incomplete installation")
    os.chmod(db_path, 0o600)

    created_at = format_utc(datetime.now(UTC))
    expires_at = format_utc(datetime.now(UTC) + timedelta(hours=args.admin_session_hours))
    identities = IdentityStore(db_path, lambda: secrets.token_bytes(16))
    credentials: list[dict[str, str]] = []
    registered: dict[ProductRole, str] = {}
    for role in ProductRole:
        identity_id = _stable_id("identity", args.deployment_id, role)
        subject_id = _stable_id("subject", args.deployment_id, role)
        capability_id = _stable_id("capability", args.deployment_id, role)
        login_secret = secrets.token_urlsafe(48)
        identity = identities.register(
            identity_id=identity_id,
            subject_id=subject_id,
            display_name=f"RK {role.value.replace('_', ' ').title()}",
            role=role,
            capability_id=capability_id,
            login_secret=login_secret,
            now=created_at,
        )
        registered[role] = identity.identity_id
        credentials.append(
            {
                "identity_id": identity.identity_id,
                "subject_id": identity.subject_id,
                "display_name": identity.display_name,
                "role": identity.role.value,
                "capability_id": identity.capability_id,
                "login_secret": login_secret,
            }
        )

    admin = next(item for item in credentials if item["role"] == ProductRole.ADMIN.value)
    bootstrap_admin = BootstrapAdmin(
        admin["identity_id"], admin["subject_id"], admin["display_name"], admin["login_secret"]
    )
    sessions = SessionStore(
        db_path,
        identities,
        Uuid7Generator().new,
        args.organization_id,
        busy_timeout_ms=args.busy_timeout_ms,
    )
    admin_session = sessions.login(
        identity_id=bootstrap_admin.identity_id,
        login_secret=bootstrap_admin.login_secret,
        now=created_at,
        expires_at=expires_at,
    )

    review_keys = [
        {
            "key_id": "managed-peer-review",
            "reviewer_identity_id": registered[ProductRole.PEER_REVIEWER],
            "hmac_secret": secrets.token_urlsafe(48),
        },
        {
            "key_id": "managed-paper-review",
            "reviewer_identity_id": registered[ProductRole.PAPER_REVIEWER],
            "hmac_secret": secrets.token_urlsafe(48),
        },
    ]
    credentials_path = root / "initial-credentials.json"
    _write_private_json(
        credentials_path,
        {
            "schema_version": "rk.product.initial-credentials.v1",
            "created_at": created_at,
            "deployment_id": args.deployment_id,
            "organization_id": args.organization_id,
            "identities": credentials,
            "review_attestation_keys": review_keys,
            "initial_admin_session": {
                "session_id": admin_session.session_id,
                "expires_at": admin_session.expires_at,
            },
        },
    )
    config_path = root / "deployment.json"
    _write_private_json(
        config_path,
        {
            "schema_version": "rk.product.deployment-config.v1",
            "product_version": PRODUCT_VERSION,
            "deployment_id": args.deployment_id,
            "organization_id": args.organization_id,
            "data_root": str(root),
            "database": str(db_path),
            "cas_root": str(cas_root),
            "spool_root": str(spool_root),
            "limits": dict(_DEFAULT_LIMITS),
            "release_id": release.manifest().release_id,
            "release_manifest_sha256": release.manifest().manifest_sha256,
            "managed_identity_ids": {role.value: registered[role] for role in ProductRole},
            "review_key_ids": [item["key_id"] for item in review_keys],
        },
    )
    os.chmod(root, 0o700)
    return credentials_path


def _parser() -> argparse.ArgumentParser:
    repository = Path(__file__).resolve().parents[1]
    parser = argparse.ArgumentParser(
        prog="rkproductbootstrap",
        description="Initialize an empty RK-PRODUCT-1.1 release data root.",
    )
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--deployment-id", required=True)
    parser.add_argument("--organization-id", required=True)
    parser.add_argument("--busy-timeout-ms", type=int, default=5_000)
    parser.add_argument("--admin-session-hours", type=int, default=24)
    parser.add_argument("--migrations", type=Path, default=repository / "migrations")
    parser.add_argument("--schema-fragments", type=Path, default=repository / "schema_fragments")
    parser.add_argument(
        "--release-manifest",
        type=Path,
        default=repository / "docs/spec/product/migration-manifest.json",
    )
    parser.add_argument(
        "--release-lock", type=Path, default=repository / "migrations/release/current.lock"
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = _parser()
    args = parser.parse_args(argv)
    if args.busy_timeout_ms < 1 or args.admin_session_hours < 1:
        parser.error("timeouts must be positive")
    try:
        path = bootstrap(args)
    except (OSError, RuntimeError, ValueError, sqlite3.Error) as error:
        print(f"bootstrap failed: {error}", file=sys.stderr)
        return 1
    print(path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
