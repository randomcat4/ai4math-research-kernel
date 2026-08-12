from __future__ import annotations

import hashlib
import hmac
import json
from copy import deepcopy
from typing import Any

import pytest

from rk.machine_trust import machine_evidence_is_trusted


def _case() -> tuple[dict[str, Any], dict[str, Any], dict[str, Any], dict[str, Any]]:
    key = b"k" * 32
    source_sha, output_sha, binary_sha = "1" * 64, "2" * 64, "3" * 64
    receipt_payload = {
        "adapter_name": "lean-replay",
        "adapter_version": "v2",
        "run_id": "run-1",
        "attempt_id": "attempt-1",
        "binding_id": "binding-1",
        "environment_profile_id": "lean-clean",
        "source_commit": "a" * 40,
        "invocation_nonce": "nonce-1",
        "request_hash": "4" * 64,
        "result_hash": "5" * 64,
        "status": "COMPLETED",
        "source_sha256": source_sha,
        "output_sha256": output_sha,
        "binary_sha256": binary_sha,
        "exit_code": 0,
    }
    digest = hashlib.sha256(
        json.dumps(
            receipt_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    receipt = {
        "payload": receipt_payload,
        "signature": hmac.new(key, digest.encode("ascii"), hashlib.sha256).hexdigest(),
    }
    diagnostic = {
        "request_hash": "4" * 64,
        "result_hash": "5" * 64,
        "source_sha256": source_sha,
        "output_sha256": output_sha,
        "binary_sha256": binary_sha,
        "exit_code": 0,
        "host_receipt": receipt,
    }
    claim = {
        "claim_id": "claim-1",
        "run_id": "run-1",
        "contract_version": 1,
        "statement_hash": "6" * 64,
        "status": "ACTIVE",
    }
    evidence = {
        "evidence_id": "evidence-1",
        "run_id": "run-1",
        "claim_id": "claim-1",
        "contract_version": 1,
        "statement_hash": "6" * 64,
        "artifact_id": "output-1",
        "evidence_type": "LEAN_REPLAY",
        "evidence_strength": "HARD_MACHINE",
        "root_kind": "LEAN_KERNEL",
        "verifier_profile_id": "lean-clean",
        "submitter_subject_id": "verifier-subject",
        "status": "ACCEPTED",
    }
    projection = {
        "attempts": [{"attempt_id": "attempt-1", "status": "SUCCEEDED"}],
        "bindings": [
            {
                "binding_id": "binding-1",
                "run_id": "run-1",
                "attempt_id": "attempt-1",
                "adapter_name": "lean-replay",
                "adapter_version": "v2",
                "environment_profile_id": "lean-clean",
                "source_commit": "a" * 40,
                "invocation_nonce": "nonce-1",
            }
        ],
        "artifacts": [
            {"artifact_id": "source-1", "sha256": source_sha, "status": "COMMITTED"},
            {"artifact_id": "output-1", "sha256": output_sha, "status": "COMMITTED"},
        ],
        "lean_feedback": [
            {
                "lean_feedback_id": "feedback-1",
                "run_id": "run-1",
                "claim_id": "claim-1",
                "attempt_id": "attempt-1",
                "contract_version": 1,
                "environment_profile_id": "lean-clean",
                "toolchain": "lean-4.28",
                "mathlib_commit": "a" * 40,
                "source_artifact_id": "source-1",
                "output_artifact_id": "output-1",
                "feedback_kind": "REPLAY_PASS",
                "diagnostic": diagnostic,
                "receipt_nonce": "nonce-1",
            }
        ],
    }
    policy = {
        "verifier_profiles": {
            "lean-clean": {
                "adapter_name": "lean-replay",
                "toolchain": "lean-4.28",
                "mathlib_commit": "a" * 40,
                "binary_sha256": binary_sha,
                "receipt_hmac_key_hex": key.hex(),
                "forbidden_submitter_subject_ids": ["candidate-subject"],
            }
        }
    }
    return evidence, claim, projection, policy


def _trusted(
    evidence: dict[str, Any],
    claim: dict[str, Any],
    projection: dict[str, Any],
    policy: dict[str, Any],
) -> bool:
    return machine_evidence_is_trusted(
        evidence,
        target_claim=claim,
        run_id="run-1",
        contract_version=1,
        projection=projection,
        policy=policy,
        expected_type="LEAN_REPLAY",
    )


def test_v01_public_receipt_context_never_grants_machine_authority() -> None:
    assert not _trusted(*_case())


@pytest.mark.parametrize("attack", ["null_nonce", "cross_claim", "old_version", "old_hash"])
def test_receipt_scope_and_nonce_fail_closed(attack: str) -> None:
    evidence, claim, projection, policy = deepcopy(_case())
    if attack == "null_nonce":
        projection["lean_feedback"][0]["receipt_nonce"] = None
    elif attack == "cross_claim":
        claim["claim_id"] = "claim-2"
    elif attack == "old_version":
        evidence["contract_version"] = 0
    else:
        evidence["statement_hash"] = "7" * 64
    assert not _trusted(evidence, claim, projection, policy)


@pytest.mark.parametrize("drift", ["adapter_name", "mathlib_commit", "toolchain", "binary_sha256"])
def test_current_verifier_profile_drift_fails_closed(drift: str) -> None:
    evidence, claim, projection, policy = deepcopy(_case())
    policy["verifier_profiles"]["lean-clean"][drift] = "drifted"
    assert not _trusted(evidence, claim, projection, policy)


def test_forbidden_submitter_fails_closed() -> None:
    evidence, claim, projection, policy = deepcopy(_case())
    evidence["submitter_subject_id"] = "candidate-subject"
    assert not _trusted(evidence, claim, projection, policy)
