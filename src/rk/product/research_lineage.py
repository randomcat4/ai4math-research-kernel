"""Research-case provenance for clean-room, certificate-import, and historical modes."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Protocol

from rk.product.artifact_read import ExactArtifactRef


class ResearchLineageError(RuntimeError):
    pass


class ResearchLineageConflict(ResearchLineageError):
    pass


class LineageMode(StrEnum):
    CLEAN_ROOM_REDISCOVERY = "CLEAN_ROOM_REDISCOVERY"
    IMPORTED_CERTIFICATE_VERIFICATION = "IMPORTED_CERTIFICATE_VERIFICATION"
    HISTORICAL_CANDIDATE_MIGRATION = "HISTORICAL_CANDIDATE_MIGRATION"


class ArtifactReader(Protocol):
    def open_range(
        self, artifact_id: str, *, expected_ref: ExactArtifactRef | None = None
    ) -> object: ...


@dataclass(frozen=True, slots=True)
class HistoricalArtifact:
    lineage_artifact_id: str
    stable_project_id: str
    artifact: ExactArtifactRef
    source_uri: str
    source_version: str
    content_class: str
    captured_at: str


@dataclass(frozen=True, slots=True)
class CertificateVerifierReceipt:
    receipt_id: str
    run_id: str
    certificate_artifact_id: str
    certificate_sha256: str
    verifier_backend: str
    verdict: str
    checked_scope: bool
    checked_proof: bool
    authority_effect: str = "VERIFICATION_EVIDENCE_ONLY"

    def to_dict(self) -> dict[str, object]:
        return {
            "schema_version": "rk.certificate_verifier_receipt.v1",
            "receipt_id": self.receipt_id,
            "run_id": self.run_id,
            "certificate_artifact_id": self.certificate_artifact_id,
            "certificate_sha256": self.certificate_sha256,
            "verifier_backend": self.verifier_backend,
            "verdict": self.verdict,
            "checked_scope": self.checked_scope,
            "checked_proof": self.checked_proof,
            "authority_effect": self.authority_effect,
        }


@dataclass(frozen=True, slots=True)
class ResearchCaseLineage:
    lineage_id: str
    stable_project_id: str
    mode: LineageMode
    run_id: str
    contract_version: int
    frozen_tree_digest: str
    data_root_id: str
    input_manifest_artifact_id: str
    input_manifest_sha256: str
    candidate_authority: str
    status: str
    created_by_subject_id: str
    created_at: str
    updated_at: str


_CLEAN_INPUT_CLASSES = {
    "PROBLEM_STATEMENT",
    "PUBLIC_DEFINITIONS",
    "TOOLCHAIN_LOCK",
    "CONTRACT",
}
_SOURCE_CLASSES = _CLEAN_INPUT_CLASSES | {
    "HISTORICAL_MATERIAL",
    "HISTORICAL_CONCLUSION",
    "HISTORICAL_PROOF",
    "CERTIFICATE",
    "CERTIFICATE_REPORT",
}


class ResearchLineageStore:
    def __init__(
        self,
        *,
        db_path: Path,
        artifacts: ArtifactReader,
        busy_timeout_ms: int = 5_000,
    ) -> None:
        self._db_path = Path(db_path)
        self._artifacts = artifacts
        self._busy_timeout_ms = busy_timeout_ms

    def register_artifact(
        self,
        *,
        lineage_artifact_id: str,
        stable_project_id: str,
        artifact: ExactArtifactRef,
        source_uri: str,
        source_version: str,
        content_class: str,
        captured_at: str,
        now: str,
    ) -> HistoricalArtifact:
        _project(stable_project_id)
        if content_class not in _SOURCE_CLASSES:
            raise ValueError("lineage artifact content class is unsupported")
        if not all(
            value.strip()
            for value in (
                lineage_artifact_id,
                source_uri,
                source_version,
                captured_at,
                now,
            )
        ):
            raise ValueError("lineage artifact provenance is incomplete")
        self._read_bytes(artifact)
        values = (
            stable_project_id,
            artifact.artifact_id,
            artifact.sha256,
            artifact.byte_count,
            artifact.media_type,
            source_uri,
            source_version,
            content_class,
            captured_at,
            now,
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT stable_project_id,artifact_id,artifact_sha256,artifact_byte_count,"
                "artifact_media_type,source_uri,source_version,content_class,captured_at,"
                "created_at FROM product_research_lineage_artifacts "
                "WHERE lineage_artifact_id=?",
                (lineage_artifact_id,),
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO product_research_lineage_artifacts("
                    "lineage_artifact_id,stable_project_id,artifact_id,artifact_sha256,"
                    "artifact_byte_count,artifact_media_type,source_uri,source_version,"
                    "content_class,captured_at,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (lineage_artifact_id, *values),
                )
            elif tuple(row[:-1]) != values[:-1]:
                raise ResearchLineageConflict("lineage artifact ID is bound differently")
            connection.commit()
        return self.get_artifact(lineage_artifact_id)

    def get_artifact(self, lineage_artifact_id: str) -> HistoricalArtifact:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT lineage_artifact_id,stable_project_id,artifact_id,artifact_sha256,"
                "artifact_byte_count,artifact_media_type,source_uri,source_version,"
                "content_class,captured_at FROM product_research_lineage_artifacts "
                "WHERE lineage_artifact_id=?",
                (lineage_artifact_id,),
            ).fetchone()
        if row is None:
            raise KeyError(lineage_artifact_id)
        return HistoricalArtifact(
            str(row[0]),
            str(row[1]),
            ExactArtifactRef(str(row[2]), str(row[3]), int(row[4]), str(row[5])),
            str(row[6]),
            str(row[7]),
            str(row[8]),
            str(row[9]),
        )

    def start_zhao(
        self,
        *,
        lineage_id: str,
        mode: LineageMode,
        run_id: str,
        contract_version: int,
        frozen_tree_digest: str,
        data_root_id: str,
        input_manifest: ExactArtifactRef,
        created_by_subject_id: str,
        now: str,
    ) -> ResearchCaseLineage:
        if mode not in {
            LineageMode.CLEAN_ROOM_REDISCOVERY,
            LineageMode.IMPORTED_CERTIFICATE_VERIFICATION,
        }:
            raise ResearchLineageError("Zhao supports only its two isolated run modes")
        if not _digest(frozen_tree_digest) or contract_version < 1:
            raise ValueError("frozen tree or contract version is invalid")
        manifest = self._read_json(input_manifest)
        expected = {
            "schema_version",
            "stable_project_id",
            "mode",
            "run_id",
            "frozen_tree_digest",
            "data_root_id",
            "worker_input_lineage_artifact_ids",
            "historical_conclusion_input_ids",
            "imported_certificate_lineage_artifact_ids",
        }
        if set(manifest) != expected:
            raise ResearchLineageError("Zhao input manifest fields are not exact")
        if (
            manifest["schema_version"] != "rk.zhao_input_manifest.v1"
            or manifest["stable_project_id"] != "ZHAO_C61"
            or manifest["mode"] != mode.value
            or manifest["run_id"] != run_id
            or manifest["frozen_tree_digest"] != frozen_tree_digest
            or manifest["data_root_id"] != data_root_id
        ):
            raise ResearchLineageError("Zhao manifest identity or frozen fence differs")
        worker_inputs = _strings(manifest["worker_input_lineage_artifact_ids"])
        historical = _strings(manifest["historical_conclusion_input_ids"])
        certificates = _strings(manifest["imported_certificate_lineage_artifact_ids"])
        if mode is LineageMode.CLEAN_ROOM_REDISCOVERY:
            if historical or certificates or not worker_inputs:
                raise ResearchLineageError(
                    "clean-room manifest cannot contain historical conclusions or certificates"
                )
            sources = [self.get_artifact(item) for item in worker_inputs]
            if any(
                item.stable_project_id != "ZHAO_C61"
                or item.content_class not in _CLEAN_INPUT_CLASSES
                for item in sources
            ):
                raise ResearchLineageError("clean-room Worker input contains historical evidence")
            initial_status = "RUNNING"
            input_ids = worker_inputs
        else:
            if historical or worker_inputs or not certificates:
                raise ResearchLineageError(
                    "certificate-import manifest must contain only imported certificates"
                )
            sources = [self.get_artifact(item) for item in certificates]
            if any(
                item.stable_project_id != "ZHAO_C61" or item.content_class != "CERTIFICATE"
                for item in sources
            ):
                raise ResearchLineageError("import manifest contains a non-certificate artifact")
            initial_status = "CERTIFICATES_PENDING"
            input_ids = certificates
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            other = connection.execute(
                "SELECT run_id,input_manifest_sha256 FROM product_research_case_lineages "
                "WHERE stable_project_id='ZHAO_C61' AND mode<>?",
                (mode.value,),
            ).fetchall()
            if any(row[0] == run_id or row[1] == input_manifest.sha256 for row in other):
                raise ResearchLineageError(
                    "Zhao modes require different runs and different input manifests"
                )
            connection.execute(
                "INSERT INTO product_research_case_lineages("
                "lineage_id,stable_project_id,mode,run_id,contract_version,"
                "frozen_tree_digest,data_root_id,input_manifest_artifact_id,"
                "input_manifest_sha256,input_manifest_json,candidate_authority,status,"
                "created_by_subject_id,created_at,updated_at) "
                "VALUES(?,'ZHAO_C61',?,?,?,?,?,?,?,?,'CANDIDATE_ONLY',?,?,?,?)",
                (
                    lineage_id,
                    mode.value,
                    run_id,
                    contract_version,
                    frozen_tree_digest,
                    data_root_id,
                    input_manifest.artifact_id,
                    input_manifest.sha256,
                    _json(manifest),
                    initial_status,
                    created_by_subject_id,
                    now,
                    now,
                ),
            )
            for ordinal, source_id in enumerate(input_ids):
                connection.execute(
                    "INSERT INTO product_research_lineage_inputs("
                    "lineage_id,lineage_artifact_id,input_role,ordinal) VALUES(?,?,?,?)",
                    (
                        lineage_id,
                        source_id,
                        "CLEAN_WORKER_INPUT"
                        if mode is LineageMode.CLEAN_ROOM_REDISCOVERY
                        else "IMPORTED_CERTIFICATE",
                        ordinal,
                    ),
                )
            connection.commit()
        return self.get(lineage_id)

    def record_clean_room_outcome(
        self,
        *,
        lineage_id: str,
        outcome: str,
        result_artifact: ExactArtifactRef | None,
        now: str,
    ) -> ResearchCaseLineage:
        if outcome not in {"NO_REDISCOVERY", "REDISCOVERED_CANDIDATE_ONLY"}:
            raise ValueError("clean-room outcome is unsupported")
        lineage = self.get(lineage_id)
        if lineage.mode is not LineageMode.CLEAN_ROOM_REDISCOVERY or lineage.status != "RUNNING":
            raise ResearchLineageError("lineage is not a running clean-room case")
        if outcome == "REDISCOVERED_CANDIDATE_ONLY" and result_artifact is None:
            raise ResearchLineageError("rediscovery candidate requires a result artifact")
        if result_artifact is not None:
            self._read_bytes(result_artifact)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO product_research_lineage_outcomes("
                "lineage_id,outcome,result_artifact_id,result_sha256,recorded_at) "
                "VALUES(?,?,?,?,?)",
                (
                    lineage_id,
                    outcome,
                    result_artifact.artifact_id if result_artifact else None,
                    result_artifact.sha256 if result_artifact else None,
                    now,
                ),
            )
            connection.execute(
                "UPDATE product_research_case_lineages SET status=?,updated_at=? "
                "WHERE lineage_id=? AND status='RUNNING'",
                (
                    "COMPLETED_NO_REDISCOVERY"
                    if outcome == "NO_REDISCOVERY"
                    else "REDISCOVERED_CANDIDATE_ONLY",
                    now,
                    lineage_id,
                ),
            )
            connection.commit()
        return self.get(lineage_id)

    def record_certificate_report(
        self,
        *,
        lineage_id: str,
        receipts: tuple[CertificateVerifierReceipt, ...],
        report_artifact: ExactArtifactRef,
        now: str,
    ) -> ResearchCaseLineage:
        lineage = self.get(lineage_id)
        if (
            lineage.mode is not LineageMode.IMPORTED_CERTIFICATE_VERIFICATION
            or lineage.status != "CERTIFICATES_PENDING"
        ):
            raise ResearchLineageError("lineage is not awaiting certificate receipts")
        with self._connect() as connection:
            certificate_rows = connection.execute(
                "SELECT a.artifact_id,a.artifact_sha256 FROM product_research_lineage_inputs i "
                "JOIN product_research_lineage_artifacts a "
                "ON a.lineage_artifact_id=i.lineage_artifact_id WHERE i.lineage_id=? "
                "ORDER BY i.ordinal",
                (lineage_id,),
            ).fetchall()
        expected = [(str(row[0]), str(row[1])) for row in certificate_rows]
        received = [(item.certificate_artifact_id, item.certificate_sha256) for item in receipts]
        if received != expected or len({item.receipt_id for item in receipts}) != len(receipts):
            raise ResearchLineageError("every imported certificate needs one ordered receipt")
        for item in receipts:
            if (
                item.run_id != lineage.run_id
                or item.verdict not in {"ACCEPTED", "REJECTED"}
                or not item.verifier_backend
                or item.authority_effect != "VERIFICATION_EVIDENCE_ONLY"
                or not item.checked_scope
                or (item.verdict == "ACCEPTED" and not item.checked_proof)
            ):
                raise ResearchLineageError("certificate verifier receipt is incomplete or unbound")
        report = self._read_json(report_artifact)
        report_rows = [
            {
                "certificate_artifact_id": item.certificate_artifact_id,
                "certificate_sha256": item.certificate_sha256,
                "verifier_receipt_id": item.receipt_id,
                "verdict": item.verdict,
            }
            for item in receipts
        ]
        if report != {
            "schema_version": "rk.certificate_import_report.v1",
            "lineage_id": lineage_id,
            "run_id": lineage.run_id,
            "certificates": report_rows,
        }:
            raise ResearchLineageError("certificate report bytes do not match verifier receipts")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for item in receipts:
                connection.execute(
                    "INSERT INTO product_research_certificate_verifications("
                    "lineage_id,certificate_artifact_id,certificate_sha256,"
                    "verifier_receipt_id,verifier_receipt_json,verdict,checked_at) "
                    "VALUES(?,?,?,?,?,?,?)",
                    (
                        lineage_id,
                        item.certificate_artifact_id,
                        item.certificate_sha256,
                        item.receipt_id,
                        _json(item.to_dict()),
                        item.verdict,
                        now,
                    ),
                )
            connection.execute(
                "INSERT INTO product_research_lineage_reports("
                "lineage_id,report_artifact_id,report_sha256,report_json,created_at) "
                "VALUES(?,?,?,?,?)",
                (
                    lineage_id,
                    report_artifact.artifact_id,
                    report_artifact.sha256,
                    _json(report),
                    now,
                ),
            )
            connection.execute(
                "UPDATE product_research_case_lineages SET status='CERTIFICATES_CHECKED',"
                "updated_at=? WHERE lineage_id=? AND status='CERTIFICATES_PENDING'",
                (now, lineage_id),
            )
            connection.commit()
        return self.get(lineage_id)

    def get(self, lineage_id: str) -> ResearchCaseLineage:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT lineage_id,stable_project_id,mode,run_id,contract_version,"
                "frozen_tree_digest,data_root_id,input_manifest_artifact_id,"
                "input_manifest_sha256,candidate_authority,status,created_by_subject_id,"
                "created_at,updated_at FROM product_research_case_lineages WHERE lineage_id=?",
                (lineage_id,),
            ).fetchone()
        if row is None:
            raise KeyError(lineage_id)
        return ResearchCaseLineage(
            str(row[0]),
            str(row[1]),
            LineageMode(str(row[2])),
            str(row[3]),
            int(row[4]),
            str(row[5]),
            str(row[6]),
            str(row[7]),
            str(row[8]),
            str(row[9]),
            str(row[10]),
            str(row[11]),
            str(row[12]),
            str(row[13]),
        )

    def _read_json(self, artifact: ExactArtifactRef) -> dict[str, object]:
        if artifact.media_type not in {"application/json", "text/json"}:
            raise ResearchLineageError("lineage manifest/report must be JSON")
        try:
            decoded = json.loads(self._read_bytes(artifact).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ResearchLineageError("lineage JSON artifact is invalid") from error
        if not isinstance(decoded, dict):
            raise ResearchLineageError("lineage JSON artifact must be an object")
        return {str(key): value for key, value in decoded.items()}

    def _read_bytes(self, artifact: ExactArtifactRef) -> bytes:
        result = self._artifacts.open_range(artifact.artifact_id, expected_ref=artifact)
        stream = getattr(result, "stream", None)
        if stream is None:
            raise ResearchLineageError("artifact reader exposed no byte stream")
        body = b"".join(stream)
        if len(body) != artifact.byte_count or hashlib.sha256(body).hexdigest() != artifact.sha256:
            raise ResearchLineageError("lineage ArtifactRef bytes do not match")
        return body

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._db_path, isolation_level=None)
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms}")
        return connection


def _project(value: str) -> str:
    if value not in {"ZHAO_C61", "N2_AJT5"}:
        raise ResearchLineageError(
            "stable project must be exactly ZHAO_C61 or N2_AJT5; aliases are forbidden"
        )
    return value


def _digest(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) or not item for item in value):
        raise ResearchLineageError("manifest arrays must contain non-empty strings")
    if len(set(value)) != len(value):
        raise ResearchLineageError("manifest arrays must be unique")
    return tuple(value)


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


__all__ = [
    "CertificateVerifierReceipt",
    "HistoricalArtifact",
    "LineageMode",
    "ResearchCaseLineage",
    "ResearchLineageConflict",
    "ResearchLineageError",
    "ResearchLineageStore",
]
