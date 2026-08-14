from __future__ import annotations

import hashlib
import json
import sqlite3
import subprocess
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path

import pytest

from rk.cas import ContentAddressedStore
from rk.product.artifact_read import ArtifactReadService, ExactArtifactRef
from rk.product.case_import import (
    HistoricalCaseImporter,
    HistoricalClaimCandidate,
    HistoricalMaterialInput,
)
from rk.product.claims import ClaimKind, ClaimLifecycle, ClaimStore
from rk.product.contracts import ContractContent, ContractStore
from rk.product.jobs import JobStore
from rk.product.materials import MaterialStore
from rk.product.research_lineage import (
    CertificateVerifierReceipt,
    LineageMode,
    ResearchLineageError,
    ResearchLineageStore,
)
from rk.product.tool_runs import ToolRunStore
from rk.product_migrations import ProductMigrationAssembler, ProductMigrationRegistry
from rk.wire import canonical_json_bytes

ROOT = Path(__file__).parents[1]
NOW = "2026-08-14T04:00:00Z"


class ArtifactIds:
    def __init__(self) -> None:
        self.value = 0

    def new(self) -> str:
        self.value += 1
        return f"artifact-{self.value}"


class ClaimIds:
    def __init__(self) -> None:
        self.value = 0

    def __call__(self) -> str:
        self.value += 1
        return f"claim-{self.value}"


class Publisher:
    def __init__(self, root: Path) -> None:
        self.records: dict[str, dict[str, object]] = {}
        self.cas = ContentAddressedStore(
            root,
            max_bytes=20 * 1024 * 1024,
            inbox_roots=(),
            orphan_grace_seconds=60,
            id_generator=ArtifactIds(),
        )

    def publish(self, *, data: bytes, logical_name: str, media_type: str) -> ExactArtifactRef:
        committed = self.cas.commit(
            self.cas.stage_bytes(data, media_type=media_type, source_name=logical_name),
            now=datetime(2026, 8, 14, 4, tzinfo=UTC),
        )
        self.records[committed.artifact_id] = committed.to_record()
        return ExactArtifactRef(
            committed.artifact_id,
            committed.sha256,
            committed.byte_count,
            committed.media_type,
        )

    def json(self, value: Mapping[str, object], name: str) -> ExactArtifactRef:
        return self.publish(
            data=canonical_json_bytes(value),
            logical_name=name,
            media_type="application/json",
        )

    def get_artifact(self, artifact_id: str) -> dict[str, object] | None:
        return self.records.get(artifact_id)


def setup(
    tmp_path: Path,
) -> tuple[
    Path,
    Publisher,
    ResearchLineageStore,
    ClaimStore,
    MaterialStore,
    ContractStore,
    HistoricalCaseImporter,
]:
    db = tmp_path / "product.sqlite"
    with sqlite3.connect(db) as connection:
        ProductMigrationAssembler(ProductMigrationRegistry(ROOT / "schema_fragments")).apply(
            connection
        )
    publisher = Publisher(tmp_path / "cas")
    reader = ArtifactReadService(metadata=publisher, cas_root=tmp_path / "cas")
    lineage = ResearchLineageStore(db_path=db, artifacts=reader)
    claims = ClaimStore(db, ClaimIds(), lambda: NOW)
    jobs = JobStore(db, iter(["unused-job-id"]).__next__)
    materials = MaterialStore(
        db_path=db,
        artifacts=reader,
        publisher=publisher,
        tool_runs=ToolRunStore(db, jobs),
    )
    contracts = ContractStore(db)
    importer = HistoricalCaseImporter(
        db_path=db,
        artifacts=reader,
        lineage=lineage,
        materials=materials,
        contracts=contracts,
        claims=claims,
    )
    return db, publisher, lineage, claims, materials, contracts, importer


def register(
    lineage: ResearchLineageStore,
    publisher: Publisher,
    *,
    lineage_artifact_id: str,
    project: str,
    content: bytes,
    media_type: str,
    content_class: str,
    version: str,
) -> ExactArtifactRef:
    artifact = publisher.publish(
        data=content,
        logical_name=f"{lineage_artifact_id}.dat",
        media_type=media_type,
    )
    lineage.register_artifact(
        lineage_artifact_id=lineage_artifact_id,
        stable_project_id=project,
        artifact=artifact,
        source_uri=f"history://{project}/{lineage_artifact_id}",
        source_version=version,
        content_class=content_class,
        captured_at="2026-08-01T00:00:00Z",
        now=NOW,
    )
    return artifact


def clean_manifest(
    *,
    run_id: str,
    tree_digest: str,
    data_root_id: str,
    worker_inputs: list[str],
    historical: list[str] | None = None,
) -> dict[str, object]:
    return {
        "schema_version": "rk.zhao_input_manifest.v1",
        "stable_project_id": "ZHAO_C61",
        "mode": "CLEAN_ROOM_REDISCOVERY",
        "run_id": run_id,
        "frozen_tree_digest": tree_digest,
        "data_root_id": data_root_id,
        "worker_input_lineage_artifact_ids": worker_inputs,
        "historical_conclusion_input_ids": historical or [],
        "imported_certificate_lineage_artifact_ids": [],
    }


def import_manifest(
    *,
    run_id: str,
    tree_digest: str,
    data_root_id: str,
    certificates: list[str],
) -> dict[str, object]:
    return {
        "schema_version": "rk.zhao_input_manifest.v1",
        "stable_project_id": "ZHAO_C61",
        "mode": "IMPORTED_CERTIFICATE_VERIFICATION",
        "run_id": run_id,
        "frozen_tree_digest": tree_digest,
        "data_root_id": data_root_id,
        "worker_input_lineage_artifact_ids": [],
        "historical_conclusion_input_ids": [],
        "imported_certificate_lineage_artifact_ids": certificates,
    }


def test_frozen_tree_runs_both_zhao_modes_without_merging_evidence(tmp_path: Path) -> None:
    db, publisher, lineage, _claims, _materials, _contracts, _importer = setup(tmp_path)
    current_commit = subprocess.run(
        ("git", "rev-parse", "HEAD"),
        cwd=ROOT,
        check=True,
        capture_output=True,
    ).stdout
    frozen_tree_digest = hashlib.sha256(current_commit).hexdigest()
    register(
        lineage,
        publisher,
        lineage_artifact_id="zhao-frozen-tree",
        project="ZHAO_C61",
        content=current_commit,
        media_type="text/plain",
        content_class="TOOLCHAIN_LOCK",
        version=current_commit.decode().strip(),
    )
    register(
        lineage,
        publisher,
        lineage_artifact_id="zhao-problem-statement",
        project="ZHAO_C61",
        content=(
            b"Public C61 problem statement and definitions only; no historical conclusion, "
            b"proof text, or certificate bytes."
        ),
        media_type="text/plain",
        content_class="PROBLEM_STATEMENT",
        version="public-v1",
    )
    register(
        lineage,
        publisher,
        lineage_artifact_id="zhao-historical-proof",
        project="ZHAO_C61",
        content=b"Historical proof bytes excluded from the clean-room Worker manifest.",
        media_type="text/plain",
        content_class="HISTORICAL_PROOF",
        version="legacy-v7",
    )
    clean_root = tmp_path / "zhao-clean-data-root"
    import_root = tmp_path / "zhao-import-data-root"
    clean_root.mkdir()
    import_root.mkdir()
    assert list(clean_root.iterdir()) == []
    assert list(import_root.iterdir()) == []
    clean_root_id = hashlib.sha256(b"new-clean-root").hexdigest()
    import_root_id = hashlib.sha256(b"new-import-root").hexdigest()
    clean_value = clean_manifest(
        run_id="zhao-clean-run",
        tree_digest=frozen_tree_digest,
        data_root_id=clean_root_id,
        worker_inputs=["zhao-problem-statement", "zhao-frozen-tree"],
    )
    clean_ref = publisher.json(clean_value, "zhao-clean-manifest.json")
    clean = lineage.start_zhao(
        lineage_id="lineage-zhao-clean",
        mode=LineageMode.CLEAN_ROOM_REDISCOVERY,
        run_id="zhao-clean-run",
        contract_version=1,
        frozen_tree_digest=frozen_tree_digest,
        data_root_id=clean_root_id,
        input_manifest=clean_ref,
        created_by_subject_id="researcher-clean",
        now=NOW,
    )
    assert clean.status == "RUNNING"
    assert clean.candidate_authority == "CANDIDATE_ONLY"
    contaminated = clean_manifest(
        run_id="zhao-contaminated-run",
        tree_digest=frozen_tree_digest,
        data_root_id=hashlib.sha256(b"contaminated-root").hexdigest(),
        worker_inputs=["zhao-historical-proof"],
        historical=["legacy-conclusion-c61"],
    )
    with pytest.raises(ResearchLineageError, match="historical"):
        lineage.start_zhao(
            lineage_id="lineage-zhao-contaminated",
            mode=LineageMode.CLEAN_ROOM_REDISCOVERY,
            run_id="zhao-contaminated-run",
            contract_version=1,
            frozen_tree_digest=frozen_tree_digest,
            data_root_id=str(contaminated["data_root_id"]),
            input_manifest=publisher.json(contaminated, "contaminated.json"),
            created_by_subject_id="researcher-clean",
            now=NOW,
        )
    completed = lineage.record_clean_room_outcome(
        lineage_id=clean.lineage_id,
        outcome="NO_REDISCOVERY",
        result_artifact=None,
        now=NOW,
    )
    assert completed.status == "COMPLETED_NO_REDISCOVERY"

    cert_one = register(
        lineage,
        publisher,
        lineage_artifact_id="zhao-certificate-one",
        project="ZHAO_C61",
        content=b"certificate-one-exact-bytes",
        media_type="application/octet-stream",
        content_class="CERTIFICATE",
        version="certificate-v1",
    )
    cert_two = register(
        lineage,
        publisher,
        lineage_artifact_id="zhao-certificate-two",
        project="ZHAO_C61",
        content=b"certificate-two-exact-bytes",
        media_type="application/octet-stream",
        content_class="CERTIFICATE",
        version="certificate-v2",
    )
    import_value = import_manifest(
        run_id="zhao-import-run",
        tree_digest=frozen_tree_digest,
        data_root_id=import_root_id,
        certificates=["zhao-certificate-one", "zhao-certificate-two"],
    )
    import_ref = publisher.json(import_value, "zhao-import-manifest.json")
    imported = lineage.start_zhao(
        lineage_id="lineage-zhao-import",
        mode=LineageMode.IMPORTED_CERTIFICATE_VERIFICATION,
        run_id="zhao-import-run",
        contract_version=1,
        frozen_tree_digest=frozen_tree_digest,
        data_root_id=import_root_id,
        input_manifest=import_ref,
        created_by_subject_id="certificate-reviewer",
        now=NOW,
    )
    assert imported.run_id != clean.run_id
    assert imported.input_manifest_sha256 != clean.input_manifest_sha256
    receipts = (
        CertificateVerifierReceipt(
            "verifier-receipt-one",
            imported.run_id,
            cert_one.artifact_id,
            cert_one.sha256,
            "LEAN_REPLAY",
            "ACCEPTED",
            True,
            True,
        ),
        CertificateVerifierReceipt(
            "verifier-receipt-two",
            imported.run_id,
            cert_two.artifact_id,
            cert_two.sha256,
            "DETERMINISTIC_CHECKER",
            "REJECTED",
            True,
            False,
        ),
    )
    report_value = {
        "schema_version": "rk.certificate_import_report.v1",
        "lineage_id": imported.lineage_id,
        "run_id": imported.run_id,
        "certificates": [
            {
                "certificate_artifact_id": item.certificate_artifact_id,
                "certificate_sha256": item.certificate_sha256,
                "verifier_receipt_id": item.receipt_id,
                "verdict": item.verdict,
            }
            for item in receipts
        ],
    }
    checked = lineage.record_certificate_report(
        lineage_id=imported.lineage_id,
        receipts=receipts,
        report_artifact=publisher.json(report_value, "certificate-report.json"),
        now=NOW,
    )
    assert checked.status == "CERTIFICATES_CHECKED"
    with sqlite3.connect(db) as connection:
        rows = connection.execute(
            "SELECT certificate_artifact_id,verifier_receipt_id,verdict FROM "
            "product_research_certificate_verifications WHERE lineage_id=? "
            "ORDER BY certificate_artifact_id",
            (imported.lineage_id,),
        ).fetchall()
    assert rows == [
        (cert_one.artifact_id, "verifier-receipt-one", "ACCEPTED"),
        (cert_two.artifact_id, "verifier-receipt-two", "REJECTED"),
    ]
    assert clean.mode is LineageMode.CLEAN_ROOM_REDISCOVERY
    assert checked.mode is LineageMode.IMPORTED_CERTIFICATE_VERIFICATION


def confirmed_contract(db: Path, contracts: ContractStore, *, run_id: str) -> None:
    value = ContractContent(
        objective="classify AJT(5) configurations under the frozen hypotheses",
        domain="finite AJT(5) incidence configurations",
        quantifiers=("for every admissible configuration",),
        boundary_conditions=("all five AJT constraints are retained",),
        exact_negation="there exists an admissible counterconfiguration",
        allowed_tools=("LEAN", "MANAGED_HUMAN"),
        success_criteria=("each atomic Claim passes the current B10 verifier gate",),
    )
    encoded = json.dumps(value.to_dict(), sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    digest = hashlib.sha256(canonical_json_bytes(value.to_dict())).hexdigest()
    with sqlite3.connect(db) as connection:
        connection.execute(
            "INSERT INTO product_contracts("
            "contract_id,run_id,current_version,created_at,updated_at) VALUES(?,?,1,?,?)",
            ("contract-n2", run_id, NOW, NOW),
        )
        connection.execute(
            "INSERT INTO product_contract_versions("
            "contract_id,version,state,content_json,content_digest,confirmed_by,"
            "confirmed_at,created_at) VALUES('contract-n2',1,'CONFIRMED',?,?,?,?,?)",
            (encoded, digest, "n2-researcher", NOW, NOW),
        )
        connection.commit()
    assert contracts.get("contract-n2").state == "CONFIRMED"


def test_n2_ajt5_history_enters_materials_and_pending_b10_not_graph(tmp_path: Path) -> None:
    db, publisher, lineage, claims, materials, contracts, importer = setup(tmp_path)
    run_id = "n2-current-run"
    confirmed_contract(db, contracts, run_id=run_id)
    note_one = register(
        lineage,
        publisher,
        lineage_artifact_id="n2-note-one",
        project="N2_AJT5",
        content=(
            b"AJT(5) manual case split for a five-constraint incidence configuration; "
            b"historical annotations are candidates, not accepted facts."
        ),
        media_type="text/plain",
        content_class="HISTORICAL_MATERIAL",
        version="notebook-v3",
    )
    note_two = register(
        lineage,
        publisher,
        lineage_artifact_id="n2-note-two",
        project="N2_AJT5",
        content=(
            b"AJT(5) candidate obstruction calculation with unresolved boundary equality; "
            b"requires current formal and independent verification."
        ),
        media_type="text/plain",
        content_class="HISTORICAL_CONCLUSION",
        version="notebook-v4",
    )
    with pytest.raises(ResearchLineageError, match="aliases"):
        lineage.register_artifact(
            lineage_artifact_id="forbidden-n2-alias",
            stable_project_id="N2",
            artifact=note_one,
            source_uri="history://N2/alias",
            source_version="legacy",
            content_class="HISTORICAL_MATERIAL",
            captured_at=NOW,
            now=NOW,
        )
    with pytest.raises(ResearchLineageError, match="aliases"):
        lineage.register_artifact(
            lineage_artifact_id="forbidden-total22",
            stable_project_id="TOTAL_22",
            artifact=note_two,
            source_uri="history://TOTAL_22/wrong-project",
            source_version="legacy",
            content_class="HISTORICAL_MATERIAL",
            captured_at=NOW,
            now=NOW,
        )
    material_inputs = (
        HistoricalMaterialInput("n2-note-one", "material-n2-one", "TEXT"),
        HistoricalMaterialInput("n2-note-two", "material-n2-two", "TEXT"),
    )
    candidates = (
        HistoricalClaimCandidate(
            "manual-case-split-17",
            "n2-note-one",
            "Under the frozen AJT(5) incidence hypotheses, the type-I boundary case "
            "reduces to the three enumerated compatibility subcases recorded in source "
            "version notebook-v3.",
            ClaimKind.LEMMA,
            "n2_type_i_boundary_candidate",
            "history-worker-one",
            "history-attempt-one",
        ),
        HistoricalClaimCandidate(
            "manual-obstruction-23",
            "n2-note-two",
            "For the unresolved AJT(5) obstruction configuration, the historical "
            "determinant expression vanishes only if the recorded boundary equality is "
            "independently established.",
            ClaimKind.LEMMA,
            "n2_obstruction_candidate",
            "history-worker-two",
            "history-attempt-two",
        ),
    )
    frozen_tree_digest = hashlib.sha256(b"current-frozen-tree").hexdigest()
    data_root_id = hashlib.sha256(b"new-n2-data-root").hexdigest()
    manifest_value = {
        "schema_version": "rk.n2_history_manifest.v1",
        "stable_project_id": "N2_AJT5",
        "mode": "HISTORICAL_CANDIDATE_MIGRATION",
        "run_id": run_id,
        "contract_version": 1,
        "frozen_tree_digest": frozen_tree_digest,
        "data_root_id": data_root_id,
        "source_lineage_artifact_ids": ["n2-note-one", "n2-note-two"],
        "historical_conclusion_input_ids": [
            "manual-case-split-17",
            "manual-obstruction-23",
        ],
        "candidate_count": 2,
        "imported_certificate_lineage_artifact_ids": [],
        "verifier_receipt_ids": [],
    }
    migrated = importer.migrate_n2_ajt5(
        lineage_id="lineage-n2-history",
        run_id=run_id,
        contract_version=1,
        kernel_revision=4,
        frozen_tree_digest=frozen_tree_digest,
        data_root_id=data_root_id,
        input_manifest=publisher.json(manifest_value, "n2-history-manifest.json"),
        material_inputs=material_inputs,
        claim_candidates=candidates,
        subject_id="n2-migration-reviewer",
        now=NOW,
    )
    assert migrated.stable_project_id == "N2_AJT5"
    assert migrated.mode is LineageMode.HISTORICAL_CANDIDATE_MIGRATION
    assert migrated.status == "HISTORY_MIGRATED_CANDIDATE_ONLY"
    assert materials.get_material("material-n2-one").original.artifact_id == note_one.artifact_id
    assert materials.get_material("material-n2-two").original.artifact_id == note_two.artifact_id
    with sqlite3.connect(db) as connection:
        rows = connection.execute(
            "SELECT c.claim_id,c.lifecycle,c.authority_class,l.status "
            "FROM product_research_lineage_candidates l JOIN product_claims c "
            "ON c.claim_id=l.claim_id WHERE l.lineage_id=? ORDER BY c.claim_id",
            (migrated.lineage_id,),
        ).fetchall()
        graph_count = connection.execute(
            "SELECT COUNT(*) FROM product_graph_nodes WHERE run_id=?",
            (run_id,),
        ).fetchone()
    assert len(rows) == 2
    assert all(
        lifecycle == ClaimLifecycle.PENDING_VERIFICATION.value
        and authority == "RESEARCH_HISTORY"
        and status == "PENDING_CURRENT_VERIFICATION"
        for _claim_id, lifecycle, authority, status in rows
    )
    assert graph_count == (0,)
    assert all(claims.get(row[0]).promotion_eligible is False for row in rows)
