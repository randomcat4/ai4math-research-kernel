"""Deployment schema upgrade runner over the single D00b release assembler."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from rk.product_release_migrations import (
    ProductReleaseMigrationAssembler,
    ProductReleaseMigrationError,
)
from rk.runtime import format_utc


class UpgradeError(RuntimeError):
    """Release preflight, required backup, or atomic migration failed."""


@dataclass(frozen=True, slots=True)
class UpgradePreflight:
    release_id: str
    product_version: str
    release_manifest_digest: str
    installed_fragments: int
    target_fragments: int
    pending_fragment_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class UpgradeReceipt:
    upgrade_id: str
    deployment_id: str
    request_id: str
    backup_id: str
    release_id: str
    release_manifest_digest: str
    fragments_before: int
    fragments_after: int
    started_at: str
    finished_at: str


class UpgradeRunner:
    def __init__(
        self,
        *,
        db_path: Path,
        release: ProductReleaseMigrationAssembler,
        id_generator: Callable[[], str],
        clock: Callable[[], datetime],
        busy_timeout_ms: int = 5_000,
    ) -> None:
        self._db_path = Path(db_path)
        self._release = release
        self._ids = id_generator
        self._clock = clock
        self._busy_timeout_ms = busy_timeout_ms

    def preflight(self) -> UpgradePreflight:
        manifest = self._release.manifest()
        plan = self._release.plan()
        with self._connect() as connection:
            installed = {
                f"{row[0]}/{row[1]}"
                for row in connection.execute(
                    "SELECT package,slug FROM product_schema_fragments"
                ).fetchall()
            }
        target = tuple(step.fragment.fragment_id for step in plan)
        unknown = installed - set(target)
        if unknown:
            raise UpgradeError("installed fragment is absent from the target release")
        return UpgradePreflight(
            manifest.release_id,
            manifest.product_version,
            manifest.manifest_sha256,
            len(installed),
            len(target),
            tuple(item for item in target if item not in installed),
        )

    def execute(self, *, deployment_id: str, request_id: str, backup_id: str) -> UpgradeReceipt:
        if not deployment_id or not request_id or not backup_id:
            raise ValueError("deployment_id, request_id, and backup_id are required")
        existing = self._by_request(deployment_id, request_id)
        if existing is not None:
            return existing
        preflight = self.preflight()
        self._require_backup(deployment_id, backup_id)
        upgrade_id = self._ids()
        started_at = format_utc(self._clock())
        try:
            with self._connect() as connection:
                applied = self._release.apply(connection)
            after = len(applied)
            if after != preflight.target_fragments:
                raise UpgradeError("release assembler returned an incomplete target plan")
        except BaseException as error:
            finished_at = format_utc(self._clock())
            self._record_failure(
                upgrade_id,
                deployment_id,
                request_id,
                backup_id,
                preflight,
                type(error).__name__.upper(),
                started_at,
                finished_at,
            )
            if isinstance(error, UpgradeError):
                raise
            if isinstance(error, ProductReleaseMigrationError):
                raise UpgradeError("D00b release migration failed") from error
            raise
        finished_at = format_utc(self._clock())
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO product_deployment_upgrades("
                "upgrade_id,deployment_id,request_id,backup_id,release_id,"
                "release_manifest_digest,state,fragments_before,fragments_after,"
                "failure_code,started_at,finished_at) VALUES(?,?,?,?,?,?,'SUCCEEDED',?,?,NULL,?,?)",
                (
                    upgrade_id,
                    deployment_id,
                    request_id,
                    backup_id,
                    preflight.release_id,
                    preflight.release_manifest_digest,
                    preflight.installed_fragments,
                    after,
                    started_at,
                    finished_at,
                ),
            )
            connection.commit()
        return UpgradeReceipt(
            upgrade_id,
            deployment_id,
            request_id,
            backup_id,
            preflight.release_id,
            preflight.release_manifest_digest,
            preflight.installed_fragments,
            after,
            started_at,
            finished_at,
        )

    def _require_backup(self, deployment_id: str, backup_id: str) -> None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT state FROM product_backups WHERE backup_id=? AND deployment_id=?",
                (backup_id, deployment_id),
            ).fetchone()
        if row != ("SUCCEEDED",):
            raise UpgradeError("schema migration requires a successful same-deployment backup")

    def _record_failure(
        self,
        upgrade_id: str,
        deployment_id: str,
        request_id: str,
        backup_id: str,
        preflight: UpgradePreflight,
        failure_code: str,
        started_at: str,
        finished_at: str,
    ) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT OR IGNORE INTO product_deployment_upgrades("
                "upgrade_id,deployment_id,request_id,backup_id,release_id,"
                "release_manifest_digest,state,fragments_before,fragments_after,"
                "failure_code,started_at,finished_at) VALUES(?,?,?,?,?,?,'FAILED',?,NULL,?,?,?)",
                (
                    upgrade_id,
                    deployment_id,
                    request_id,
                    backup_id,
                    preflight.release_id,
                    preflight.release_manifest_digest,
                    preflight.installed_fragments,
                    failure_code,
                    started_at,
                    finished_at,
                ),
            )
            connection.commit()

    def _by_request(self, deployment_id: str, request_id: str) -> UpgradeReceipt | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT upgrade_id,deployment_id,request_id,backup_id,release_id,"
                "release_manifest_digest,fragments_before,fragments_after,started_at,finished_at "
                "FROM product_deployment_upgrades WHERE deployment_id=? AND request_id=? "
                "AND state='SUCCEEDED'",
                (deployment_id, request_id),
            ).fetchone()
        if row is None:
            return None
        return UpgradeReceipt(
            str(row[0]),
            str(row[1]),
            str(row[2]),
            str(row[3]),
            str(row[4]),
            str(row[5]),
            int(row[6]),
            int(row[7]),
            str(row[8]),
            str(row[9]),
        )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self._db_path, timeout=self._busy_timeout_ms / 1_000, isolation_level=None
        )
        connection.execute("PRAGMA foreign_keys=ON")
        return connection


__all__ = ["UpgradeError", "UpgradePreflight", "UpgradeReceipt", "UpgradeRunner"]
