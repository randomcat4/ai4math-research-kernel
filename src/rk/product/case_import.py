"""Controlled migration of historical N2_AJT5 notes through current product gates."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from rk.product.artifact_read import ExactArtifactRef
from rk.product.claims import (
    ClaimArtifactBinding,
    ClaimKind,
    ClaimStore,
    ClaimSubmission,
)
from rk.product.contracts import ContractStore, ContractVersion
from rk.product.materials import MaterialStore
from rk.product.research_lineage import (
    ResearchCaseLineage,
    ResearchLineageError,
    ResearchLineageStore,
)


class ArtifactReader(Protocol):
    def open_range(
        self, artifact_id: str, *, expected_ref: ExactArtifactRef | None = None
    ) -> object: ...


@dataclass(frozen=True, slots=True)
class HistoricalMaterialInput:
    lineage_artifact_id: str
    material_id: str
    material_kind: str


@dataclass(frozen=True, slots=True)
class HistoricalClaimCandidate:
    historical_input_id: str
    source_lineage_artifact_id: str
    statement: str
    claim_kind: ClaimKind
    stable_label: str
    worker_run_id: str
    attempt_id: str


class HistoricalCaseImporter:
    """Imports provenance and pending B10 Claims; it has no fact-graph write path."""

    def __init__(
        self,
        *,
        db_path: Path,
        artifacts: ArtifactReader,
        lineage: ResearchLineageStore,
        materials: MaterialStore,
        contracts: ContractStore,
        claims: ClaimStore,
        busy_timeout_ms: int = 5_000,
    ) -> None:
        self._db_path = Path(db_path)
        self._artifacts = artifacts
        self._lineage = lineage
        self._materials = materials
        self._contracts = contracts
        self._claims = claims
        self._busy_timeout_ms = busy_timeout_ms

    def migrate_n2_ajt5(
        self,
        *,
        lineage_id: str,
        run_id: str,
        contract_version: int,
        kernel_revision: int,
        frozen_tree_digest: str,
        data_root_id: str,
        input_manifest: ExactArtifactRef,
        material_inputs: tuple[HistoricalMaterialInput, ...],
        claim_candidates: tuple[HistoricalClaimCandidate, ...],
        subject_id: str,
        now: str,
    ) -> ResearchCaseLineage:
        contract = self._contract_for_run(run_id)
        if (
            contract.run_id != run_id
            or contract.version != contract_version
            or contract.state != "CONFIRMED"
        ):
            raise ResearchLineageError("N2_AJT5 migration requires the current confirmed contract")
        if not material_inputs or not claim_candidates:
            raise ResearchLineageError("N2_AJT5 migration requires materials and candidates")
        if len({item.lineage_artifact_id for item in material_inputs}) != len(material_inputs):
            raise ResearchLineageError("historical material inputs must be unique")
        if len({item.historical_input_id for item in claim_candidates}) != len(claim_candidates):
            raise ResearchLineageError("historical candidate input IDs must be unique")
        sources = {
            item.lineage_artifact_id: self._lineage.get_artifact(item.lineage_artifact_id)
            for item in material_inputs
        }
        if any(
            source.stable_project_id != "N2_AJT5"
            or source.content_class
            not in {"HISTORICAL_MATERIAL", "HISTORICAL_CONCLUSION", "HISTORICAL_PROOF"}
            for source in sources.values()
        ):
            raise ResearchLineageError("N2 migration source belongs to another project")
        if any(item.source_lineage_artifact_id not in sources for item in claim_candidates):
            raise ResearchLineageError("candidate is not bound to a migrated material")
        manifest = self._read_json(input_manifest)
        expected_manifest = {
            "schema_version": "rk.n2_history_manifest.v1",
            "stable_project_id": "N2_AJT5",
            "mode": "HISTORICAL_CANDIDATE_MIGRATION",
            "run_id": run_id,
            "contract_version": contract_version,
            "frozen_tree_digest": frozen_tree_digest,
            "data_root_id": data_root_id,
            "source_lineage_artifact_ids": [item.lineage_artifact_id for item in material_inputs],
            "historical_conclusion_input_ids": [
                item.historical_input_id for item in claim_candidates
            ],
            "candidate_count": len(claim_candidates),
            "imported_certificate_lineage_artifact_ids": [],
            "verifier_receipt_ids": [],
        }
        if manifest != expected_manifest:
            raise ResearchLineageError("N2_AJT5 history manifest is not exact")
        submitted: list[tuple[str, str, str]] = []
        for item in material_inputs:
            source = sources[item.lineage_artifact_id]
            self._materials.ingest(
                material_id=item.material_id,
                run_id=run_id,
                material_kind=item.material_kind,
                original=source.artifact,
                now=now,
            )
        material_by_source = {
            item.lineage_artifact_id: item.material_id for item in material_inputs
        }
        for candidate in claim_candidates:
            source = sources[candidate.source_lineage_artifact_id]
            claim = self._claims.submit(
                ClaimSubmission(
                    run_id=run_id,
                    contract_version=contract_version,
                    kernel_revision=kernel_revision,
                    statement=candidate.statement,
                    claim_kind=candidate.claim_kind,
                    proof_or_evidence_artifacts=(_claim_artifact(source.artifact),),
                    predecessor_fact_ids=(),
                    source_binding_artifact=_claim_artifact(source.artifact),
                    work_item_id=f"history:{lineage_id}:{candidate.historical_input_id}",
                    worker_run_id=candidate.worker_run_id,
                    attempt_id=candidate.attempt_id,
                    stable_label=candidate.stable_label,
                    public_summary=None,
                ),
                subject_id=subject_id,
            )
            if claim.lifecycle.value != "PENDING_VERIFICATION":
                raise ResearchLineageError("historical candidate bypassed the B10 pending gate")
            submitted.append(
                (
                    claim.claim_id,
                    material_by_source[candidate.source_lineage_artifact_id],
                    candidate.historical_input_id,
                )
            )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT stable_project_id,mode,run_id,input_manifest_sha256 FROM "
                "product_research_case_lineages WHERE lineage_id=?",
                (lineage_id,),
            ).fetchone()
            identity = (
                "N2_AJT5",
                "HISTORICAL_CANDIDATE_MIGRATION",
                run_id,
                input_manifest.sha256,
            )
            if existing is None:
                connection.execute(
                    "INSERT INTO product_research_case_lineages("
                    "lineage_id,stable_project_id,mode,run_id,contract_version,"
                    "frozen_tree_digest,data_root_id,input_manifest_artifact_id,"
                    "input_manifest_sha256,input_manifest_json,candidate_authority,status,"
                    "created_by_subject_id,created_at,updated_at) "
                    "VALUES(?,'N2_AJT5','HISTORICAL_CANDIDATE_MIGRATION',?,?,?,?,?,?,?,"
                    "'CANDIDATE_ONLY','HISTORY_MIGRATED_CANDIDATE_ONLY',?,?,?)",
                    (
                        lineage_id,
                        run_id,
                        contract_version,
                        frozen_tree_digest,
                        data_root_id,
                        input_manifest.artifact_id,
                        input_manifest.sha256,
                        _json(manifest),
                        subject_id,
                        now,
                        now,
                    ),
                )
                for ordinal, item in enumerate(material_inputs):
                    connection.execute(
                        "INSERT INTO product_research_lineage_inputs("
                        "lineage_id,lineage_artifact_id,input_role,ordinal) VALUES(?,?,?,?)",
                        (lineage_id, item.lineage_artifact_id, "HISTORICAL_MATERIAL", ordinal),
                    )
                for claim_id, material_id, historical_id in submitted:
                    connection.execute(
                        "INSERT INTO product_research_lineage_candidates("
                        "lineage_id,claim_id,source_material_id,historical_input_id,status) "
                        "VALUES(?,?,?,?,'PENDING_CURRENT_VERIFICATION')",
                        (lineage_id, claim_id, material_id, historical_id),
                    )
            elif tuple(existing) != identity:
                raise ResearchLineageError("N2 lineage ID is bound differently")
            connection.commit()
        return self._lineage.get(lineage_id)

    def _contract_for_run(self, run_id: str) -> ContractVersion:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT contract_id FROM product_contracts WHERE run_id=?",
                (run_id,),
            ).fetchone()
        if row is None:
            raise ResearchLineageError("run has no current contract")
        return self._contracts.get(str(row[0]))

    def _read_json(self, artifact: ExactArtifactRef) -> dict[str, object]:
        if artifact.media_type != "application/json":
            raise ResearchLineageError("history manifest must be application/json")
        result = self._artifacts.open_range(artifact.artifact_id, expected_ref=artifact)
        stream = getattr(result, "stream", None)
        if stream is None:
            raise ResearchLineageError("history manifest bytes are unavailable")
        body = b"".join(stream)
        if len(body) != artifact.byte_count or hashlib.sha256(body).hexdigest() != artifact.sha256:
            raise ResearchLineageError("history manifest ArtifactRef differs")
        try:
            value = json.loads(body)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ResearchLineageError("history manifest is invalid JSON") from error
        if not isinstance(value, dict):
            raise ResearchLineageError("history manifest must be an object")
        return {str(key): item for key, item in value.items()}

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._db_path, isolation_level=None)
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms}")
        return connection


def _claim_artifact(value: ExactArtifactRef) -> ClaimArtifactBinding:
    return ClaimArtifactBinding(
        value.artifact_id,
        value.sha256,
        value.byte_count,
        value.media_type,
    )


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


__all__ = [
    "HistoricalCaseImporter",
    "HistoricalClaimCandidate",
    "HistoricalMaterialInput",
]
