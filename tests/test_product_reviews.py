from __future__ import annotations

import hashlib
import hmac
import json
import sqlite3
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from rk.product.artifact_read import ExactArtifactRef
from rk.product.attestation_import import (
    ArtifactReadContentReader,
    AttestationImportError,
    AuthorityEffect,
    HmacAttestationKey,
    HmacKeyringVerifier,
    ReviewAttestationImporter,
    TrustClass,
    canonical_review_payload,
)
from rk.product.identity import IdentityStore, ProductRole
from rk.product.reviews import (
    IndependenceStatus,
    ReviewArtifactRef,
    ReviewBinding,
    ReviewIndependenceError,
    ReviewTaskConflict,
    ReviewTaskStatus,
    ReviewTaskStore,
    ReviewType,
)

ROOT = Path(__file__).parents[1]
SCHEMA = ROOT / "docs/spec/product/review.schema.json"
B05A = ROOT / "schema_fragments/B05a/identity.sql"
B05B = ROOT / "schema_fragments/B05b/reviews.sql"
SECRET = b"managed-review-key-material-32bytes!"
TASK_ID = "76e89cf5-2d2e-461b-b03d-c4ed076fd6c1"
RUN_ID = "c73f6387-2ea0-487a-aebf-dd2b8dad8ec2"
TARGET_ID = "2999d48e-d478-4094-b212-d33e061a448a"
REVIEW_ID = "66e89cf5-2d2e-461b-b03d-c4ed076fd6c1"
ARTIFACT_ID = "77e89cf5-2d2e-461b-b03d-c4ed076fd6c1"
CLOSURE_ID = "1999d48e-d478-4094-b212-d33e061a448a"
TEX_ID = "3999d48e-d478-4094-b212-d33e061a448a"
REVIEWER_ID = "verifier:managed:one"
REVIEWER_SUBJECT = "reviewer:one"
AUTHOR = "worker:one"
CREATED = "2026-08-13T18:00:00Z"
CLAIMED = "2026-08-13T18:05:00Z"
SUBMITTED = "2026-08-13T18:10:00Z"
EXPIRES = "2026-08-14T18:00:00Z"

CHECKS = {
    ReviewType.ATOMIC: (
        "statement_correct",
        "proof_valid",
        "dependency_scope_valid",
        "evidence_sufficient",
    ),
    ReviewType.COMPOSITION: (
        "proof_checked",
        "scope_checked",
        "coverage",
        "compatibility",
        "invariant",
        "progress",
        "boundary",
        "simultaneous_choice",
    ),
    ReviewType.PAPER: (
        "statement_alignment",
        "proof_completeness",
        "citation_accuracy",
        "novelty_boundary",
        "artifact_binding",
        "outcome_alignment",
    ),
}


class MemoryArtifacts:
    def __init__(self) -> None:
        self.values: dict[str, bytes] = {}

    def read_bytes(self, artifact_ref: ReviewArtifactRef) -> bytes:
        return self.values[artifact_ref.artifact_id]


@dataclass
class Harness:
    db_path: Path
    tasks: ReviewTaskStore
    artifacts: MemoryArtifacts
    importer: ReviewAttestationImporter
    review_type: ReviewType
    binding: ReviewBinding


def _harness(
    tmp_path: Path,
    review_type: ReviewType,
    *,
    trust_class: TrustClass = TrustClass.MANAGED_PEER_REVIEW,
    authority_effect: AuthorityEffect = AuthorityEffect.PEER_PROMOTION_ELIGIBLE,
    promotion_eligible: bool = True,
) -> Harness:
    db_path = tmp_path / "product.sqlite"
    with sqlite3.connect(db_path) as connection:
        connection.executescript(B05A.read_text(encoding="utf-8"))
        connection.executescript(B05B.read_text(encoding="utf-8"))
    identities = IdentityStore(db_path, lambda: b"0" * 16)
    identities.register(
        identity_id=REVIEWER_ID,
        subject_id=REVIEWER_SUBJECT,
        display_name="Independent reviewer",
        role=ProductRole.REVIEWER,
        capability_id="cap:reviewer:one",
        login_secret="reviewer-login-secret",
        now=CREATED,
    )
    binding = _binding(review_type)
    tasks = ReviewTaskStore(db_path, identities)
    tasks.create(
        review_task_id=TASK_ID,
        review_type=review_type,
        binding=binding,
        author_subject_ids=(AUTHOR,),
        assignee_identity_id=REVIEWER_ID,
        created_at=CREATED,
        expires_at=EXPIRES,
    )
    tasks.claim(TASK_ID, identity_id=REVIEWER_ID, now=CLAIMED)
    artifacts = MemoryArtifacts()
    verifier = HmacKeyringVerifier(
        {
            "reviewer-key-one": HmacAttestationKey(
                secret=SECRET,
                verifier_identity_id=REVIEWER_ID,
                trust_class=trust_class,
                authority_effect=authority_effect,
                promotion_eligible=promotion_eligible,
            )
        }
    )
    importer = ReviewAttestationImporter(
        tasks=tasks,
        artifacts=artifacts,
        signatures=verifier,
        review_schema_path=SCHEMA,
    )
    return Harness(db_path, tasks, artifacts, importer, review_type, binding)


def _binding(review_type: ReviewType) -> ReviewBinding:
    common: dict[str, Any] = {
        "run_id": RUN_ID,
        "kernel_revision": 8,
        "contract_version": 2,
        "target_id": TARGET_ID,
        "target_digest": "a" * 64,
    }
    if review_type is ReviewType.COMPOSITION:
        common.update(
            selected_subgraph_digest="b" * 64,
            closure_witness_id=CLOSURE_ID,
        )
    if review_type is ReviewType.PAPER:
        common.update(
            candidate_tex_artifact_id=TEX_ID,
            terminal_root_digest="d" * 64,
            dependency_closure_digest="e" * 64,
        )
    return ReviewBinding(**common)


def _review(harness: Harness) -> dict[str, Any]:
    review: dict[str, Any] = {
        "schema_version": "rk.product.review.v1",
        "review_id": REVIEW_ID,
        "review_type": harness.review_type.value,
        "review_task_id": TASK_ID,
        "verifier_identity_id": REVIEWER_ID,
        "reviewer_subject_id": REVIEWER_SUBJECT,
        "binding": harness.binding.review_binding(),
        "independence": {
            "blind_review": True,
            "author_subject_ids": [AUTHOR],
            "saw_other_verdicts": False,
        },
        "verdict": "ACCEPT",
        "issued_at": SUBMITTED,
        "checks": {
            name: {
                "passed": True,
                "status": "HUMAN_ATTESTED",
                "conclusion": f"independently checked {name}",
                "evidence_refs": [f"artifact:evidence:{name}"],
            }
            for name in CHECKS[harness.review_type]
        },
    }
    if harness.review_type is ReviewType.COMPOSITION:
        review["closure_witness_id"] = CLOSURE_ID
    if harness.review_type is ReviewType.PAPER:
        review.update(
            candidate_tex_artifact_id=TEX_ID,
            terminal_root_digest="d" * 64,
            dependency_closure_digest="e" * 64,
        )
    return review


def _sign(review: dict[str, Any]) -> None:
    payload = canonical_review_payload(review)
    review["signature"] = {
        "algorithm": "HMAC_SHA256",
        "key_id": "reviewer-key-one",
        "signed_payload_sha256": hashlib.sha256(payload).hexdigest(),
        "value": hmac.new(SECRET, payload, hashlib.sha256).hexdigest(),
    }


def _put(harness: Harness, review: dict[str, Any]) -> ReviewArtifactRef:
    _sign(review)
    raw = json.dumps(
        review, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    harness.artifacts.values[ARTIFACT_ID] = raw
    return ReviewArtifactRef(
        artifact_id=ARTIFACT_ID,
        sha256=hashlib.sha256(raw).hexdigest(),
        byte_count=len(raw),
        media_type="application/json",
    )


@pytest.mark.parametrize("review_type", tuple(ReviewType))
def test_three_signed_review_variants_attach_only_original_artifact(
    tmp_path: Path, review_type: ReviewType
) -> None:
    harness = _harness(tmp_path, review_type)
    review = _review(harness)
    ref = _put(harness, review)

    imported = harness.importer.import_artifact(
        review_task_id=TASK_ID, artifact_ref=ref, submitted_at=SUBMITTED
    )

    assert imported.review_id == REVIEW_ID
    assert imported.review_type is review_type
    assert imported.task.status is ReviewTaskStatus.SUBMITTED
    assert imported.task.independence_status is IndependenceStatus.VERIFIED
    assert imported.task.signed_artifact_ref == ref
    assert imported.trust_class is TrustClass.MANAGED_PEER_REVIEW
    assert imported.authority_effect is AuthorityEffect.PEER_PROMOTION_ELIGIBLE
    with sqlite3.connect(harness.db_path) as connection:
        columns = {
            str(row[1])
            for row in connection.execute("PRAGMA table_info(product_review_tasks)")
        }
        assert not {
            "verdict",
            "checks",
            "promotion_eligible",
            "authority_effect",
            "mathematical_status",
        } & columns


NEGATIVE_CHECKS = (
    (ReviewType.ATOMIC, "proof_valid"),
    (ReviewType.ATOMIC, "dependency_scope_valid"),
    *((ReviewType.COMPOSITION, name) for name in CHECKS[ReviewType.COMPOSITION]),
    *((ReviewType.PAPER, name) for name in CHECKS[ReviewType.PAPER]),
)


@pytest.mark.parametrize(("review_type", "check_name"), NEGATIVE_CHECKS)
@pytest.mark.parametrize("mutation", ["false", "missing"])
def test_accept_review_false_or_missing_check_is_rejected_without_task_mutation(
    tmp_path: Path,
    review_type: ReviewType,
    check_name: str,
    mutation: str,
) -> None:
    harness = _harness(tmp_path, review_type)
    review = _review(harness)
    if mutation == "false":
        review["checks"][check_name]["passed"] = False
    else:
        review["checks"].pop(check_name)
    before = deepcopy(review)
    ref = _put(harness, review)

    with pytest.raises(AttestationImportError, match="REVIEW_SCHEMA_INVALID"):
        harness.importer.import_artifact(
            review_task_id=TASK_ID, artifact_ref=ref, submitted_at=SUBMITTED
        )

    assert {key: value for key, value in review.items() if key != "signature"} == before
    task = harness.tasks.get(TASK_ID)
    assert task.status is ReviewTaskStatus.CLAIMED
    assert task.signed_artifact_ref is None


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("kernel_revision", 9),
        ("contract_version", 3),
        ("target_digest", "f" * 64),
    ],
)
def test_exact_task_binding_mismatch_is_rejected(
    tmp_path: Path, field: str, value: object
) -> None:
    harness = _harness(tmp_path, ReviewType.ATOMIC)
    review = _review(harness)
    review["binding"][field] = value
    ref = _put(harness, review)

    with pytest.raises(AttestationImportError, match="TASK_BINDING_MISMATCH"):
        harness.importer.import_artifact(
            review_task_id=TASK_ID, artifact_ref=ref, submitted_at=SUBMITTED
        )
    assert harness.tasks.get(TASK_ID).status is ReviewTaskStatus.CLAIMED


def test_author_cannot_be_assignee_or_appear_in_signed_independence(
    tmp_path: Path,
) -> None:
    harness = _harness(tmp_path, ReviewType.ATOMIC)
    with pytest.raises(ReviewIndependenceError, match="REVIEWER_IS_TASK_AUTHOR"):
        harness.tasks.create(
            review_task_id="86e89cf5-2d2e-461b-b03d-c4ed076fd6c1",
            review_type=ReviewType.ATOMIC,
            binding=_binding(ReviewType.ATOMIC),
            author_subject_ids=(REVIEWER_SUBJECT,),
            assignee_identity_id=REVIEWER_ID,
            created_at=CREATED,
            expires_at=EXPIRES,
        )

    review = _review(harness)
    review["independence"]["author_subject_ids"] = [REVIEWER_SUBJECT]
    ref = _put(harness, review)
    with pytest.raises(AttestationImportError, match="AUTHOR_BINDING_MISMATCH"):
        harness.importer.import_artifact(
            review_task_id=TASK_ID, artifact_ref=ref, submitted_at=SUBMITTED
        )


@pytest.mark.parametrize(
    ("trust_class", "authority_effect", "promotion_eligible"),
    [
        (
            TrustClass.UNMANAGED_REVIEW,
            AuthorityEffect.PEER_PROMOTION_ELIGIBLE,
            True,
        ),
        (TrustClass.MANAGED_PEER_REVIEW, AuthorityEffect.NONE, True),
        (
            TrustClass.MANAGED_PEER_REVIEW,
            AuthorityEffect.PEER_PROMOTION_ELIGIBLE,
            False,
        ),
    ],
)
def test_unmanaged_none_or_ineligible_signature_chain_is_rejected(
    tmp_path: Path,
    trust_class: TrustClass,
    authority_effect: AuthorityEffect,
    promotion_eligible: bool,
) -> None:
    harness = _harness(
        tmp_path,
        ReviewType.COMPOSITION,
        trust_class=trust_class,
        authority_effect=authority_effect,
        promotion_eligible=promotion_eligible,
    )
    ref = _put(harness, _review(harness))
    with pytest.raises(AttestationImportError, match="REVIEW_AUTHORITY_INELIGIBLE"):
        harness.importer.import_artifact(
            review_task_id=TASK_ID, artifact_ref=ref, submitted_at=SUBMITTED
        )
    assert harness.tasks.get(TASK_ID).signed_artifact_ref is None


def test_signature_and_artifact_digest_are_both_verified(tmp_path: Path) -> None:
    harness = _harness(tmp_path, ReviewType.PAPER)
    review = _review(harness)
    ref = _put(harness, review)
    raw = harness.artifacts.values[ARTIFACT_ID]
    harness.artifacts.values[ARTIFACT_ID] = raw.replace(b"independently", b"fraudulently", 1)
    with pytest.raises(
        AttestationImportError, match=r"REVIEW_ARTIFACT_(LENGTH|DIGEST)_MISMATCH"
    ):
        harness.importer.import_artifact(
            review_task_id=TASK_ID, artifact_ref=ref, submitted_at=SUBMITTED
        )

    harness.artifacts.values[ARTIFACT_ID] = raw
    review["signature"]["value"] = "0" * 64
    raw_bad_signature = json.dumps(
        review, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    bad_ref = ReviewArtifactRef(
        ARTIFACT_ID,
        hashlib.sha256(raw_bad_signature).hexdigest(),
        len(raw_bad_signature),
        "application/json",
    )
    harness.artifacts.values[ARTIFACT_ID] = raw_bad_signature
    with pytest.raises(AttestationImportError, match="SIGNATURE_INVALID"):
        harness.importer.import_artifact(
            review_task_id=TASK_ID, artifact_ref=bad_ref, submitted_at=SUBMITTED
        )


def test_b04b_adapter_passes_exact_ref_to_range_service() -> None:
    ref = ReviewArtifactRef(ARTIFACT_ID, "a" * 64, 2, "application/json")

    class StubReadService:
        def open_range(self, artifact_id: str, *, expected_ref: ExactArtifactRef) -> object:
            assert artifact_id == ARTIFACT_ID
            assert expected_ref.sha256 == "a" * 64
            return SimpleNamespace(stream=iter((b"{", b"}")))

    reader = ArtifactReadContentReader(StubReadService())  # type: ignore[arg-type]
    assert reader.read_bytes(ref) == b"{}"


def test_unsigned_all_true_ui_draft_has_no_authority(tmp_path: Path) -> None:
    harness = _harness(tmp_path, ReviewType.ATOMIC)
    review = _review(harness)
    raw = json.dumps(
        review, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    ref = ReviewArtifactRef(
        ARTIFACT_ID,
        hashlib.sha256(raw).hexdigest(),
        len(raw),
        "application/json",
    )
    harness.artifacts.values[ARTIFACT_ID] = raw

    with pytest.raises(AttestationImportError, match="REVIEW_SCHEMA_INVALID"):
        harness.importer.import_artifact(
            review_task_id=TASK_ID, artifact_ref=ref, submitted_at=SUBMITTED
        )

    task = harness.tasks.get(TASK_ID)
    assert task.status is ReviewTaskStatus.CLAIMED
    assert task.independence_status is IndependenceStatus.PENDING
    assert task.signed_artifact_ref is None


def test_reassignment_rechecks_identity_and_author_independence(tmp_path: Path) -> None:
    harness = _harness(tmp_path, ReviewType.ATOMIC)
    identities = IdentityStore(harness.db_path, lambda: b"1" * 16)
    identities.register(
        identity_id="verifier:managed:two",
        subject_id="reviewer:two",
        display_name="Second independent reviewer",
        role=ProductRole.REVIEWER,
        capability_id="cap:reviewer:two",
        login_secret="second-reviewer-secret",
        now=CLAIMED,
    )
    reassigned = harness.tasks.reassign(
        TASK_ID,
        assignee_identity_id="verifier:managed:two",
        reassigned_at=SUBMITTED,
        expires_at="2026-08-15T18:00:00Z",
    )
    assert reassigned.status is ReviewTaskStatus.REASSIGNED
    assert reassigned.claimed_at is None
    claimed = harness.tasks.claim(
        TASK_ID,
        identity_id="verifier:managed:two",
        now="2026-08-13T18:15:00Z",
    )
    assert claimed.status is ReviewTaskStatus.CLAIMED

    identities.register(
        identity_id="verifier:author",
        subject_id=AUTHOR,
        display_name="Author identity",
        role=ProductRole.REVIEWER,
        capability_id="cap:reviewer:author",
        login_secret="author-reviewer-secret",
        now=CLAIMED,
    )
    with pytest.raises(ReviewIndependenceError, match="REVIEWER_IS_TASK_AUTHOR"):
        harness.tasks.reassign(
            TASK_ID,
            assignee_identity_id="verifier:author",
            reassigned_at="2026-08-13T18:20:00Z",
            expires_at="2026-08-15T18:00:00Z",
        )


def test_disabled_assignee_cannot_complete_verified_import(tmp_path: Path) -> None:
    harness = _harness(tmp_path, ReviewType.ATOMIC)
    ref = _put(harness, _review(harness))
    IdentityStore(harness.db_path, lambda: b"2" * 16).disable(
        REVIEWER_ID, now="2026-08-13T18:06:00Z"
    )

    with pytest.raises(ReviewTaskConflict, match="review task changed while artifact was recorded"):
        harness.importer.import_artifact(
            review_task_id=TASK_ID, artifact_ref=ref, submitted_at=SUBMITTED
        )
    assert harness.tasks.get(TASK_ID).status is ReviewTaskStatus.CLAIMED
