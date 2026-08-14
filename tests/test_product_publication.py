from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from rk.cas import ContentAddressedStore
from rk.migrations import MigrationRunner
from rk.product.artifact_read import ArtifactReadService
from rk.product.artifact_upload import SQLiteArtifactRegistry
from rk.product.attestation_import import (
    ArtifactReadContentReader,
    AuthorityEffect,
    HmacAttestationKey,
    HmacKeyringVerifier,
    ReviewAttestationImporter,
    TrustClass,
    canonical_review_payload,
)
from rk.product.dossier_product import ProductDossierService
from rk.product.identity import IdentityStore, ProductRole
from rk.product.log_tail import PublicLogStore
from rk.product.publication import (
    CandidateAccessDenied,
    PublicationArtifactError,
    PublicationArtifactService,
)
from rk.product.reviews import ReviewArtifactRef, ReviewTaskStore
from rk.product_migrations import ProductMigrationAssembler, ProductMigrationRegistry
from rk.storage import SQLiteStorage

ROOT = Path(__file__).parents[1]
NOW = datetime(2026, 8, 14, 12, tzinfo=UTC)
STAMP = "2026-08-14T12:00:00Z"
LATER = "2026-08-14T12:05:00Z"
EXPIRES = "2026-08-15T12:00:00Z"
RUN = "c73f6387-2ea0-487a-aebf-dd2b8dad8ec2"
ROOT_CLAIM = "66e89cf5-2d2e-461b-b03d-c4ed076fd6c1"
WITNESS = "1999d48e-d478-4094-b212-d33e061a448a"
GENERATION = "2999d48e-d478-4094-b212-d33e061a448a"
GENERATION_TWO = "3999d48e-d478-4094-b212-d33e061a448a"
TASK = "76e89cf5-2d2e-461b-b03d-c4ed076fd6c1"
REVIEW = "56e89cf5-2d2e-461b-b03d-c4ed076fd6c1"
REVIEWER = "verifier:paper:one"
REVIEWER_SUBJECT = "reviewer:paper:one"
AUTHOR = "publication-worker:one"
SECRET = b"managed-paper-review-key-material!!"
ROOT_DIGEST = "d" * 64
CLOSURE_DIGEST = "e" * 64


class Ids:
    def __init__(self, prefix: str) -> None:
        self.prefix = prefix
        self.value = 0

    def __call__(self) -> str:
        self.value += 1
        return f"{self.prefix}-{self.value}"

    def new(self) -> str:
        self.value += 1
        return f"00000000-0000-4000-8000-{self.value:012d}"


class Harness:
    def __init__(self, tmp_path: Path) -> None:
        self.db = tmp_path / "rk.sqlite"
        MigrationRunner(self.db, ROOT / "migrations", 5_000, minimum_sqlite=(3, 0, 0)).migrate()
        with sqlite3.connect(self.db) as connection:
            ProductMigrationAssembler(ProductMigrationRegistry(ROOT / "schema_fragments")).apply(
                connection
            )
        self.storage = SQLiteStorage(self.db, 5_000)
        self.registry = SQLiteArtifactRegistry(self.storage)
        self.cas_root = tmp_path / "cas"
        self.spool = tmp_path / "spool"
        self.spool.mkdir()
        self.cas = ContentAddressedStore(
            self.cas_root,
            max_bytes=20 * 1024 * 1024,
            inbox_roots=(self.spool,),
            orphan_grace_seconds=60,
            id_generator=Ids("artifact"),
        )
        self.artifacts = ArtifactReadService(metadata=self.storage, cas_root=self.cas_root)
        self.identities = IdentityStore(self.db, lambda: b"0" * 16)
        self.identities.register(
            identity_id=REVIEWER,
            subject_id=REVIEWER_SUBJECT,
            display_name="Independent paper reviewer",
            role=ProductRole.REVIEWER,
            capability_id="cap:paper-reviewer:one",
            login_secret="paper-reviewer-login-secret",
            now=STAMP,
        )
        self.tasks = ReviewTaskStore(self.db, self.identities)
        self.logs = PublicLogStore(
            db_path=self.db,
            cas=self.cas,
            registry=self.registry,
            spool_root=self.spool,
            id_generator=Ids("log"),
            clock=lambda: NOW,
            max_chunk_bytes=1024 * 1024,
            max_tail_bytes=64 * 1024,
        )
        self.ids = Ids("compile")
        self.service = PublicationArtifactService(
            db_path=self.db,
            cas=self.cas,
            registry=self.registry,
            artifacts=self.artifacts,
            review_tasks=self.tasks,
            logs=self.logs,
            id_generator=self.ids,
            clock=lambda: NOW,
        )
        self.dossiers = ProductDossierService(
            db_path=self.db,
            cas=self.cas,
            registry=self.registry,
            clock=lambda: NOW,
        )
        self._insert_finalization()

    def _insert_finalization(self) -> None:
        with sqlite3.connect(self.db) as connection:
            connection.execute(
                "INSERT INTO product_publication_finalizations VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    RUN,
                    11,
                    2,
                    "PROVED",
                    ROOT_CLAIM,
                    ROOT_DIGEST,
                    WITNESS,
                    CLOSURE_DIGEST,
                    "finalize-command",
                    "finalize-event",
                    STAMP,
                ),
            )

    def insert_candidate(self, generation: str, render: Any, revision: int) -> None:
        with sqlite3.connect(self.db) as connection:
            connection.execute(
                "INSERT INTO product_publication_candidates "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    generation,
                    f"generation-event-{revision}",
                    RUN,
                    revision,
                    11,
                    2,
                    ROOT_CLAIM,
                    ROOT_DIGEST,
                    WITNESS,
                    CLOSURE_DIGEST,
                    render.candidate_tex_ref.artifact_id,
                    render.candidate_tex_ref.sha256,
                    render.candidate_tex_ref.byte_count,
                    "application/x-tex",
                    AUTHOR,
                    STAMP,
                ),
            )


def projection() -> dict[str, Any]:
    base = "10e89cf5-2d2e-461b-b03d-c4ed076fd6c1"
    return {
        "run_id": RUN,
        "revision": 11,
        "status": "CLOSED",
        "final_outcome": "PROVED",
        "root_claim_id": ROOT_CLAIM,
        "terminal_claim_ids": [ROOT_CLAIM],
        "claims": [
            {
                "claim_id": base,
                "stable_label": "Base",
                "claim_kind": "LEMMA",
                "contract_version": 2,
                "statement_hash": "a" * 64,
                "normalized_statement": {
                    "statement": "For n=0 the identity holds.",
                    "proof": "Immediate.",
                    "atomic": True,
                },
                "lifecycle": "ACTIVE",
                "machine": "KERNEL_VERIFIED",
                "semantic": "TESTED",
                "peer": "UNREVIEWED",
            },
            {
                "claim_id": ROOT_CLAIM,
                "stable_label": "Root theorem",
                "claim_kind": "ROOT",
                "contract_version": 2,
                "statement_hash": ROOT_DIGEST,
                "normalized_statement": {
                    "statement": "The target identity holds.",
                    "proof": "Apply the base lemma.",
                    "atomic": True,
                },
                "lifecycle": "ACTIVE",
                "machine": "KERNEL_VERIFIED",
                "semantic": "TESTED",
                "peer": "UNREVIEWED",
            },
        ],
        "edges": [
            {
                "from_claim_id": base,
                "to_claim_id": ROOT_CLAIM,
                "edge_kind": "DEPENDS_ON",
                "status": "ACTIVE",
            }
        ],
    }


def put_signed_review(harness: Harness, candidate: Any) -> ReviewArtifactRef:
    review = {
        "schema_version": "rk.product.review.v1",
        "review_id": REVIEW,
        "review_type": "PAPER",
        "review_task_id": TASK,
        "verifier_identity_id": REVIEWER,
        "reviewer_subject_id": REVIEWER_SUBJECT,
        "binding": harness.tasks.get(TASK).binding.review_binding(),
        "independence": {
            "blind_review": True,
            "author_subject_ids": [AUTHOR],
            "saw_other_verdicts": False,
        },
        "verdict": "ACCEPT",
        "issued_at": LATER,
        "checks": {
            name: {
                "passed": True,
                "status": "HUMAN_ATTESTED",
                "conclusion": f"independently checked {name}",
                "evidence_refs": [f"artifact:evidence:{name}"],
            }
            for name in (
                "statement_alignment",
                "proof_completeness",
                "citation_accuracy",
                "novelty_boundary",
                "artifact_binding",
                "outcome_alignment",
            )
        },
        "candidate_tex_artifact_id": candidate.candidate_tex_ref.artifact_id,
        "terminal_root_digest": ROOT_DIGEST,
        "dependency_closure_digest": CLOSURE_DIGEST,
    }
    payload = canonical_review_payload(review)
    review["signature"] = {
        "algorithm": "HMAC_SHA256",
        "key_id": "paper-key",
        "signed_payload_sha256": hashlib.sha256(payload).hexdigest(),
        "value": hmac.new(SECRET, payload, hashlib.sha256).hexdigest(),
    }
    raw = json.dumps(review, sort_keys=True, separators=(",", ":")).encode()
    staged = harness.cas.stage_bytes(raw, media_type="application/json", source_name="review.json")
    artifact = harness.registry.register(harness.cas.commit(staged, now=NOW))
    ref = ReviewArtifactRef(
        artifact.artifact_id, artifact.sha256, artifact.byte_count, artifact.media_type
    )
    importer = ReviewAttestationImporter(
        tasks=harness.tasks,
        artifacts=ArtifactReadContentReader(harness.artifacts),
        signatures=HmacKeyringVerifier(
            {
                "paper-key": HmacAttestationKey(
                    secret=SECRET,
                    verifier_identity_id=REVIEWER,
                    trust_class=TrustClass.MANAGED_PEER_REVIEW,
                    authority_effect=AuthorityEffect.PEER_PROMOTION_ELIGIBLE,
                    promotion_eligible=True,
                )
            }
        ),
        review_schema_path=ROOT / "docs/spec/product/review.schema.json",
    )
    importer.import_artifact(review_task_id=TASK, artifact_ref=ref, submitted_at=LATER)
    with sqlite3.connect(harness.db) as connection:
        connection.execute(
            "INSERT INTO product_publication_reviews VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                REVIEW,
                "review-command",
                "review-event",
                RUN,
                3,
                GENERATION,
                11,
                ROOT_CLAIM,
                ROOT_DIGEST,
                WITNESS,
                CLOSURE_DIGEST,
                candidate.candidate_tex_ref.artifact_id,
                candidate.candidate_tex_ref.sha256,
                ref.artifact_id,
                ref.sha256,
                REVIEWER_SUBJECT,
                "rk.product.review.v1",
                "ACCEPT",
                LATER,
            ),
        )
    return ref


def prepare_reviewed(harness: Harness) -> Any:
    render = harness.service.render_candidate(
        render_request_id="render-one",
        run_id=RUN,
        finalized_snapshot=projection(),
        abstract="A deterministic result from the finalized authority snapshot.",
    )
    harness.insert_candidate(GENERATION, render, 2)
    harness.service.create_paper_review_task(
        generation_command_id=GENERATION,
        review_task_id=TASK,
        assignee_identity_id=REVIEWER,
        author_subject_ids=(AUTHOR,),
        created_at=STAMP,
        expires_at=EXPIRES,
    )
    harness.tasks.claim(TASK, identity_id=REVIEWER, now=STAMP)
    put_signed_review(harness, render)
    return render


def test_deterministic_tex_and_exact_reviewer_access(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    first = harness.service.render_candidate(
        render_request_id="render-one",
        run_id=RUN,
        finalized_snapshot=projection(),
        abstract="A deterministic abstract.",
    )
    again = harness.service.render_candidate(
        render_request_id="render-one",
        run_id=RUN,
        finalized_snapshot=projection(),
        abstract="A   deterministic abstract.",
    )
    assert again.candidate_tex_ref == first.candidate_tex_ref
    harness.insert_candidate(GENERATION, first, 2)
    harness.service.create_paper_review_task(
        generation_command_id=GENERATION,
        review_task_id=TASK,
        assignee_identity_id=REVIEWER,
        author_subject_ids=(AUTHOR,),
        created_at=STAMP,
        expires_at=EXPIRES,
    )
    harness.tasks.claim(TASK, identity_id=REVIEWER, now=STAMP)
    with pytest.raises(CandidateAccessDenied):
        harness.service.open_candidate_for_review(
            GENERATION, identity_id=REVIEWER, subject_role="MAIN"
        )
    with pytest.raises(CandidateAccessDenied):
        harness.service.open_candidate_for_review(
            GENERATION, identity_id="another-reviewer", subject_role="PAPER_REVIEWER"
        )
    opened = harness.service.open_candidate_for_review(
        GENERATION, identity_id=REVIEWER, subject_role="PAPER_REVIEWER"
    )
    assert hashlib.sha256(b"".join(opened.stream)).hexdigest() == first.candidate_tex_ref.sha256
    assert not hasattr(harness.service.homepage(RUN), "candidate_tex_ref")


def test_real_signed_review_compile_logs_and_failure_repair(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    render = prepare_reviewed(harness)
    failing = PublicationArtifactService(
        db_path=harness.db,
        cas=harness.cas,
        registry=harness.registry,
        artifacts=harness.artifacts,
        review_tasks=harness.tasks,
        logs=harness.logs,
        id_generator=harness.ids,
        clock=lambda: NOW,
        compiler="false",
    )
    with pytest.raises(PublicationArtifactError, match="compilation failed"):
        failing.compile_reviewed(generation_command_id=GENERATION, paper_review_id=REVIEW)
    compiled = harness.service.compile_reviewed(
        generation_command_id=GENERATION, paper_review_id=REVIEW
    )
    pdf = b"".join(
        harness.artifacts.open_range(
            compiled.pdf_ref.artifact_id, expected_ref=compiled.pdf_ref
        ).stream
    )
    assert pdf.startswith(b"%PDF-")
    assert compiled.candidate_tex_sha256 == render.candidate_tex_ref.sha256
    with sqlite3.connect(harness.db) as connection:
        attempts = connection.execute(
            "SELECT outcome,stdout_log_artifact_id,stderr_log_artifact_id "
            "FROM product_compilation_attempts ORDER BY rowid"
        ).fetchall()
    assert [row[0] for row in attempts] == ["FAILED", "SUCCEEDED"]
    for row in attempts:
        assert harness.artifacts.describe(str(row[1])).ref.media_type.startswith("text/plain")
        assert harness.artifacts.describe(str(row[2])).ref.media_type.startswith("text/plain")


def test_new_abstract_is_new_tex_and_requires_new_signed_review(tmp_path: Path) -> None:
    harness = Harness(tmp_path)
    old = prepare_reviewed(harness)
    new = harness.service.render_candidate(
        render_request_id="render-two",
        run_id=RUN,
        finalized_snapshot=projection(),
        abstract="A materially changed abstract that has not been reviewed.",
    )
    assert new.abstract_digest != old.abstract_digest
    assert new.candidate_tex_ref.sha256 != old.candidate_tex_ref.sha256
    harness.insert_candidate(GENERATION_TWO, new, 4)
    with pytest.raises(PublicationArtifactError, match="accepted paper review"):
        harness.service.compile_reviewed(
            generation_command_id=GENERATION_TWO, paper_review_id=REVIEW
        )


@pytest.mark.parametrize("status", ["OPEN", "RUNNING", "PAUSED", "CLOSED"])
def test_dossier_is_real_cas_artifact_in_every_run_status(tmp_path: Path, status: str) -> None:
    harness = Harness(tmp_path)
    dossier = harness.dossiers.build(
        dossier_request_id=f"dossier-{status.lower()}",
        run_snapshot={
            "run_id": f"run-{status.lower()}",
            "revision": 0 if status == "OPEN" else 7,
            "status": status,
        },
    )
    raw = b"".join(
        harness.artifacts.open_range(
            dossier.artifact_ref.artifact_id, expected_ref=dossier.artifact_ref
        ).stream
    )
    document = json.loads(raw)
    assert document["run_snapshot"]["status"] == status
    assert document["publication"]["compilation_attempts"] == []
