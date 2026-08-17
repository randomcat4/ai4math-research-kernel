from __future__ import annotations

import base64
import hashlib
import hmac
import json
from typing import Any

import pytest

from rk.adapters import (
    IndependentVerifierArtifactAdapter,
    VerifierIdentity,
)


def sign(payload: dict[str, Any], key: bytes) -> dict[str, Any]:
    body = dict(payload)
    message = b"rk.independent-verifier.v1\n" + json.dumps(
        body, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode()
    body["signature"] = base64.urlsafe_b64encode(
        hmac.new(key, message, hashlib.sha256).digest()
    ).rstrip(b"=").decode()
    return body


def signed_review(
    *, kind: str = "COMPOSITION_REVIEW", key: bytes = b"v" * 32
) -> tuple[dict[str, Any], dict[str, Any]]:
    if kind == "ATOMIC_CLAIM_REVIEW":
        binding = {
            "run_id": "run-1", "contract_version": 2, "claim_id": "claim-1",
            "statement_hash": "a" * 64,
        }
        names = ("proof_checked", "scope_checked")
        status, verdict = "HUMAN_ATTESTED", "ACCEPT"
    elif kind == "COMPOSITION_REVIEW":
        binding = {
            "run_id": "run-1", "contract_version": 2, "claim_id": "claim-1",
            "statement_hash": "a" * 64, "selected_subgraph_digest": "b" * 64,
        }
        names = (
            "proof_checked", "scope_checked", "coverage", "compatibility", "invariant",
            "progress", "boundary",
            "simultaneous_choice",
        )
        status, verdict = "HUMAN_ATTESTED", "ACCEPT"
    else:
        binding = {
            "run_id": "run-1", "contract_version": 2, "final_fact_id": "fact-1",
            "paper_sha256": "c" * 64,
        }
        names = (
            "mathematical_consistency", "dependency_closure", "claim_statements",
            "proof_bodies", "citations", "revoked_facts_excluded",
        )
        status, verdict = "CHECKED", "CORRECT"
    payload = {
        "schema_version": "rk.independent-verifier.v1",
        "artifact_kind": kind,
        "review_id": "review-1",
        "verifier_identity_id": "verifier-1",
        "issued_at": "2026-08-13T01:00:00Z",
        "binding": binding,
        "independence": {
            "blind_review": True,
            "author_subject_ids": ["worker-subject-1"],
            "verifier_subject_id": "reviewer-subject-1",
            "saw_other_verdicts": False,
        },
        "verdict": verdict,
        "checks": {
            name: {
                "passed": True,
                "status": status,
                "conclusion": f"checked {name}",
                "evidence_refs": [],
            }
            for name in names
        },
    }
    return sign(payload, key), binding


def adapter(key: bytes = b"v" * 32) -> IndependentVerifierArtifactAdapter:
    def verify(message: bytes, signature: bytes) -> bool:
        return hmac.compare_digest(hmac.new(key, message, hashlib.sha256).digest(), signature)

    return IndependentVerifierArtifactAdapter(
        {
            "verifier-1": VerifierIdentity(
                "verifier-1", "reviewer-subject-1", hashlib.sha256(key).hexdigest(), verify
            )
        }
    )


def test_six_part_human_review_returns_narrow_import_fields() -> None:
    artifact, binding = signed_review()
    result = adapter().run({"artifact": artifact, "expected_binding": binding})

    assert result["status"] == "COMPLETED"
    assert result["promotion_eligible"] is True
    assert result["authority"] == "HUMAN_ATTESTED"
    imported = result["import_fields"]
    assert imported["checklist"]["blind_review"] is True
    assert imported["checklist"]["proof_checked"]["status"] == "HUMAN_ATTESTED"
    assert imported["checklist"]["scope_checked"]["status"] == "HUMAN_ATTESTED"
    assert len(imported["checklist"]["six_parts"]) == 6
    assert imported["source_graph"]["author_subject_ids"] == ["worker-subject-1"]
    assert imported["source_graph"]["verifier_subject_id"] == "reviewer-subject-1"


def test_atomic_claim_review_requires_signed_blindness_authors_and_checks() -> None:
    artifact, binding = signed_review(kind="ATOMIC_CLAIM_REVIEW")
    result = adapter().run({"artifact": artifact, "expected_binding": binding})

    assert result["status"] == "COMPLETED"
    assert result["promotion_eligible"] is True
    imported = result["import_fields"]
    assert imported["verdict"] == "ACCEPT"
    assert imported["checklist"]["blind_review"] is True
    assert imported["checklist"]["proof_checked"]["conclusion"] == "checked proof_checked"
    assert imported["checklist"]["scope_checked"]["status"] == "HUMAN_ATTESTED"
    assert imported["source_graph"]["author_subject_ids"] == ["worker-subject-1"]


def test_atomic_claim_review_rejects_cli_supplied_binding_or_missing_check() -> None:
    artifact, binding = signed_review(kind="ATOMIC_CLAIM_REVIEW")
    wrong = {**binding, "statement_hash": "f" * 64}
    mismatch = adapter().run({"artifact": artifact, "expected_binding": wrong})
    assert mismatch["reason"] == "TARGET_BINDING_MISMATCH"

    unsigned = dict(artifact)
    unsigned.pop("signature")
    unsigned["checks"] = {"proof_checked": unsigned["checks"]["proof_checked"]}
    incomplete = adapter().run(
        {"artifact": sign(unsigned, b"v" * 32), "expected_binding": binding}
    )
    assert incomplete["reason"] == "REVIEW_SCHEMA_MISMATCH"
    assert incomplete["promotion_eligible"] is False


def test_whole_paper_review_binds_exact_digest_and_all_checks() -> None:
    artifact, binding = signed_review(kind="PAPER_REVIEW")
    result = adapter().run({"artifact": artifact, "expected_binding": binding})

    assert result["status"] == "COMPLETED"
    assert result["import_fields"]["status"] == "CORRECT"
    assert result["import_fields"]["paper_sha256"] == "c" * 64
    assert len(result["import_fields"]["checks"]) == 6


@pytest.mark.parametrize("mutation", ["signature", "binding", "blind", "author"])
def test_identity_scope_and_independence_mutations_are_ineligible(mutation: str) -> None:
    artifact, binding = signed_review()
    if mutation == "signature":
        artifact["verdict"] = "REJECT"
    elif mutation == "binding":
        binding = {**binding, "selected_subgraph_digest": "d" * 64}
    else:
        unsigned = dict(artifact)
        unsigned.pop("signature")
        independence = dict(unsigned["independence"])
        if mutation == "blind":
            independence["blind_review"] = False
        else:
            independence["author_subject_ids"] = ["reviewer-subject-1"]
        unsigned["independence"] = independence
        artifact = sign(unsigned, b"v" * 32)

    result = adapter().run({"artifact": artifact, "expected_binding": binding})
    assert result["status"] == "REJECTED"
    assert result["promotion_eligible"] is False


def test_cli_cannot_invent_a_missing_sixth_conclusion() -> None:
    artifact, binding = signed_review()
    unsigned = dict(artifact)
    unsigned.pop("signature")
    unsigned["checks"] = dict(unsigned["checks"])
    unsigned["checks"].pop("boundary")
    artifact = sign(unsigned, b"v" * 32)

    result = adapter().run({"artifact": artifact, "expected_binding": binding})
    assert result["reason"] == "REVIEW_SCHEMA_MISMATCH"
    assert result["promotion_eligible"] is False


@pytest.mark.parametrize(
    "check_name",
    [
        "proof_checked", "scope_checked", "coverage", "compatibility", "invariant",
        "progress", "boundary", "simultaneous_choice",
    ],
)
def test_signed_false_composition_check_is_not_promotion_eligible(check_name: str) -> None:
    artifact, binding = signed_review()
    unsigned = dict(artifact)
    unsigned.pop("signature")
    unsigned["checks"] = dict(unsigned["checks"])
    unsigned["checks"][check_name] = {
        "passed": False,
        "status": "HUMAN_ATTESTED",
        "conclusion": f"failed {check_name}",
        "evidence_refs": [],
    }

    result = adapter().run(
        {"artifact": sign(unsigned, b"v" * 32), "expected_binding": binding}
    )

    assert result["status"] == "REJECTED"
    assert result["promotion_eligible"] is False
    assert result["reason"] == "REVIEW_SCHEMA_MISMATCH"


@pytest.mark.parametrize(
    "check_name",
    [
        "proof_checked", "scope_checked", "coverage", "compatibility", "invariant",
        "progress", "boundary", "simultaneous_choice",
    ],
)
def test_missing_signed_composition_check_is_not_promotion_eligible(check_name: str) -> None:
    artifact, binding = signed_review()
    unsigned = dict(artifact)
    unsigned.pop("signature")
    unsigned["checks"] = dict(unsigned["checks"])
    del unsigned["checks"][check_name]

    result = adapter().run(
        {"artifact": sign(unsigned, b"v" * 32), "expected_binding": binding}
    )

    assert result["status"] == "REJECTED"
    assert result["promotion_eligible"] is False
    assert result["reason"] == "REVIEW_SCHEMA_MISMATCH"


def test_signed_negative_verdict_is_feedback_not_promotion() -> None:
    artifact, binding = signed_review()
    unsigned = dict(artifact)
    unsigned.pop("signature")
    unsigned["verdict"] = "NEEDS_REVISION"
    artifact = sign(unsigned, b"v" * 32)

    result = adapter().run({"artifact": artifact, "expected_binding": binding})
    assert result["status"] == "COMPLETED"
    assert result["promotion_eligible"] is False
    assert result["authority"] == "REVIEW_FEEDBACK_ONLY"
    assert result["import_fields"]["verdict"] == "NEEDS_REVISION"
