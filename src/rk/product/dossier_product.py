"""Immutable product dossiers readable for every research lifecycle status."""

from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from rk.cas import ContentAddressedStore
from rk.product.artifact_read import ExactArtifactRef
from rk.product.artifact_upload import ArtifactRegistry
from rk.runtime import format_utc
from rk.sqlite import open_sqlite
from rk.wire import canonical_json_bytes


@dataclass(frozen=True, slots=True)
class ProductDossier:
    dossier_request_id: str
    run_id: str
    observed_revision: int
    observed_status: str
    artifact_ref: ExactArtifactRef


class ProductDossierService:
    def __init__(
        self,
        *,
        db_path: Path,
        cas: ContentAddressedStore,
        registry: ArtifactRegistry,
        clock: Callable[[], datetime],
        busy_timeout_ms: int = 5_000,
    ) -> None:
        self._db_path = Path(db_path)
        self._cas = cas
        self._registry = registry
        self._clock = clock
        self._busy_timeout_ms = busy_timeout_ms

    def build(
        self,
        *,
        dossier_request_id: str,
        run_snapshot: Mapping[str, Any],
    ) -> ProductDossier:
        run_id = _string(run_snapshot.get("run_id"))
        revision = _natural(run_snapshot.get("revision"))
        status = _string(run_snapshot.get("status"))
        if not dossier_request_id:
            raise ValueError("dossier request ID is required")
        publication = self._publication_rows(run_id)
        value = {
            "schema_version": "rk.product.dossier.v1",
            "run_snapshot": dict(run_snapshot),
            "publication": publication,
        }
        data = canonical_json_bytes(value)
        digest = hashlib.sha256(data).hexdigest()
        with self._connect() as connection:
            row = connection.execute(
                "SELECT run_id,observed_revision,observed_status,dossier_artifact_id,"
                "dossier_sha256 FROM product_dossier_artifacts WHERE dossier_request_id=?",
                (dossier_request_id,),
            ).fetchone()
        if row is not None:
            if tuple(row[:3]) != (run_id, revision, status) or str(row[4]) != digest:
                raise ValueError("dossier request ID was reused with another snapshot")
            existing_artifact = self._artifact(str(row[3]))
            return ProductDossier(dossier_request_id, run_id, revision, status, existing_artifact)
        staged = self._cas.stage_bytes(
            data,
            media_type="application/json",
            source_name=f"{dossier_request_id}.json",
        )
        committed = self._cas.commit(staged, now=self._clock())
        artifact = self._registry.register(committed)
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO product_dossier_artifacts("
                "dossier_request_id,run_id,observed_revision,observed_status,"
                "snapshot_digest,dossier_artifact_id,dossier_sha256,created_at) "
                "VALUES(?,?,?,?,?,?,?,?)",
                (
                    dossier_request_id,
                    run_id,
                    revision,
                    status,
                    hashlib.sha256(canonical_json_bytes(dict(run_snapshot))).hexdigest(),
                    artifact.artifact_id,
                    artifact.sha256,
                    format_utc(self._clock()),
                ),
            )
        return ProductDossier(
            dossier_request_id,
            run_id,
            revision,
            status,
            ExactArtifactRef(
                artifact.artifact_id,
                artifact.sha256,
                artifact.byte_count,
                artifact.media_type,
            ),
        )

    def _publication_rows(self, run_id: str) -> dict[str, object]:
        with self._connect() as connection:
            finalization = connection.execute(
                "SELECT finalized_revision,final_outcome,terminal_root_id,"
                "terminal_root_digest,closure_witness_id,dependency_closure_digest "
                "FROM product_publication_finalizations WHERE run_id=?",
                (run_id,),
            ).fetchone()
            attempts = connection.execute(
                "SELECT compilation_attempt_id,generation_command_id,paper_review_id,"
                "candidate_tex_sha256,outcome,stdout_log_artifact_id,"
                "stderr_log_artifact_id,final_pdf_artifact_id,final_pdf_sha256,failure_code "
                "FROM product_compilation_attempts WHERE run_id=? "
                "ORDER BY created_at,compilation_attempt_id",
                (run_id,),
            ).fetchall()
        return {
            "finalization": list(finalization) if finalization is not None else None,
            "compilation_attempts": [list(row) for row in attempts],
        }

    def _artifact(self, artifact_id: str) -> ExactArtifactRef:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT sha256,byte_count,media_type FROM artifacts "
                "WHERE artifact_id=? AND ingest_state='COMMITTED'",
                (artifact_id,),
            ).fetchone()
        if row is None:
            raise ValueError("dossier artifact is unavailable")
        return ExactArtifactRef(artifact_id, str(row[0]), int(row[1]), str(row[2]))

    def _connect(self) -> sqlite3.Connection:
        connection = open_sqlite(self._db_path, timeout=self._busy_timeout_ms / 1_000)
        connection.execute("PRAGMA foreign_keys=ON")
        return connection


def _string(value: object) -> str:
    if not isinstance(value, str) or not value:
        raise ValueError("snapshot string is invalid")
    return value


def _natural(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("snapshot revision is invalid")
    return value


__all__ = ["ProductDossier", "ProductDossierService"]
