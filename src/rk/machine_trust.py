"""One fail-closed validator for authority-bearing machine evidence."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Iterable, Mapping
from typing import Any


def _records(value: Any, identifier: str) -> dict[str, Mapping[str, Any]]:
    source = value.values() if isinstance(value, Mapping) else value
    if not isinstance(source, Iterable) or isinstance(source, (str, bytes, bytearray)):
        return {}
    return {
        str(item[identifier]): item
        for item in source
        if isinstance(item, Mapping) and item.get(identifier) is not None
    }


def _status(value: Mapping[str, Any]) -> str:
    return str(value.get("ingest_status", value.get("lifecycle_status", value.get("status", ""))))


def _valid_signature(receipt: Mapping[str, Any], key_hex: Any) -> bool:
    payload = receipt.get("payload")
    signature = receipt.get("signature")
    if not isinstance(payload, Mapping) or not isinstance(signature, str) or not isinstance(
        key_hex, str
    ):
        return False
    try:
        key = bytes.fromhex(key_hex)
    except ValueError:
        return False
    if len(key) < 32:
        return False
    digest = hashlib.sha256(
        json.dumps(
            payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
    ).hexdigest()
    expected = hmac.new(key, digest.encode("ascii"), hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


def machine_evidence_is_trusted(
    evidence: Mapping[str, Any],
    *,
    target_claim: Mapping[str, Any],
    run_id: str,
    contract_version: int,
    projection: Mapping[str, Any],
    policy: Mapping[str, Any],
    expected_type: str | None = None,
) -> bool:
    """Fail closed until receipts are scoped by a DB-backed host signer.

    The v0.1 runner accepts ``receipt_context`` as a public parameter.  Even a valid HMAC
    therefore does not prove that claim/version/statement scope came from the kernel rather
    than the caller.  The structural checks below remain as the target contract, but no
    record receives authority until HostExecutionReceiptService owns both scope assembly and
    the signing key.
    """

    del evidence, target_claim, run_id, contract_version, projection, policy, expected_type
    return False


def _unscoped_v01_machine_evidence_is_structurally_valid(
    evidence: Mapping[str, Any],
    *,
    target_claim: Mapping[str, Any],
    run_id: str,
    contract_version: int,
    projection: Mapping[str, Any],
    policy: Mapping[str, Any],
    expected_type: str | None = None,
) -> bool:
    """Document the future host-service contract; never call for promotion in v0.2."""

    evidence_type = evidence.get("evidence_type")
    expected_root = {
        "LEAN_REPLAY": "LEAN_KERNEL",
        "CHECKER_CERTIFICATE": "CHECKER",
    }.get(str(evidence_type))
    if (
        expected_root is None
        or (expected_type is not None and evidence_type != expected_type)
        or evidence.get("evidence_strength") != "HARD_MACHINE"
        or _status(evidence) not in {"ACTIVE", "COMMITTED", "ACCEPTED"}
        or evidence.get("run_id", run_id) != run_id
        or evidence.get("claim_id") != target_claim.get("claim_id")
        or evidence.get("contract_version") != contract_version
        or evidence.get("statement_hash") != target_claim.get("statement_hash")
        or evidence.get("root_kind") != expected_root
    ):
        return False

    profile_id = evidence.get("verifier_profile_id")
    profiles = policy.get("verifier_profiles")
    profile = profiles.get(profile_id) if isinstance(profiles, Mapping) else None
    if not isinstance(profile, Mapping) or evidence.get("submitter_subject_id") in set(
        profile.get("forbidden_submitter_subject_ids", ())
    ):
        return False
    feedback = _records(projection.get("lean_feedback"), "lean_feedback_id")
    bindings = _records(projection.get("bindings"), "binding_id")
    attempts = _records(
        projection.get("attempts", projection.get("active_attempts")), "attempt_id"
    )
    artifacts = _records(projection.get("artifacts"), "artifact_id")
    output_artifact = artifacts.get(str(evidence.get("artifact_id")))
    if output_artifact is None or _status(output_artifact) != "COMMITTED":
        return False

    for record in feedback.values():
        nonce = record.get("receipt_nonce")
        diagnostic = record.get("diagnostic")
        receipt = diagnostic.get("host_receipt") if isinstance(diagnostic, Mapping) else None
        receipt_payload = receipt.get("payload") if isinstance(receipt, Mapping) else None
        binding = next(
            (
                item
                for item in bindings.values()
                if isinstance(nonce, str)
                and nonce
                and item.get("invocation_nonce") == nonce
                and item.get("attempt_id") == record.get("attempt_id")
            ),
            None,
        )
        attempt = attempts.get(str(record.get("attempt_id")))
        source_artifact = artifacts.get(str(record.get("source_artifact_id")))
        if (
            record.get("run_id", run_id) != run_id
            or record.get("claim_id") != target_claim.get("claim_id")
            or record.get("contract_version") != contract_version
            or record.get("environment_profile_id") != profile_id
            or record.get("output_artifact_id") != evidence.get("artifact_id")
            or record.get("feedback_kind") != "REPLAY_PASS"
            or not isinstance(nonce, str)
            or not nonce
            or not isinstance(diagnostic, Mapping)
            or not isinstance(receipt, Mapping)
            or not isinstance(receipt_payload, Mapping)
            or binding is None
            or attempt is None
            or _status(attempt) != "SUCCEEDED"
            or source_artifact is None
            or _status(source_artifact) != "COMMITTED"
            or binding.get("run_id", run_id) != run_id
            or binding.get("environment_profile_id") != profile_id
            or binding.get("adapter_name") != profile.get("adapter_name")
            or binding.get("source_commit") != profile.get("mathlib_commit")
            or record.get("toolchain") != profile.get("toolchain")
            or record.get("mathlib_commit") != profile.get("mathlib_commit")
            or receipt_payload.get("run_id") != run_id
            or receipt_payload.get("attempt_id") != record.get("attempt_id")
            or receipt_payload.get("binding_id") != binding.get("binding_id")
            or receipt_payload.get("invocation_nonce") != nonce
            or receipt_payload.get("adapter_name") != binding.get("adapter_name")
            or receipt_payload.get("adapter_version") != binding.get("adapter_version")
            or receipt_payload.get("environment_profile_id") != profile_id
            or receipt_payload.get("source_commit") != binding.get("source_commit")
            or receipt_payload.get("request_hash") != diagnostic.get("request_hash")
            or receipt_payload.get("result_hash") != diagnostic.get("result_hash")
            or receipt_payload.get("source_sha256") != source_artifact.get("sha256")
            or receipt_payload.get("source_sha256") != diagnostic.get("source_sha256")
            or receipt_payload.get("output_sha256") != output_artifact.get("sha256")
            or receipt_payload.get("output_sha256") != diagnostic.get("output_sha256")
            or receipt_payload.get("binary_sha256") != profile.get("binary_sha256")
            or receipt_payload.get("binary_sha256") != diagnostic.get("binary_sha256")
            or receipt_payload.get("exit_code") != 0
            or diagnostic.get("exit_code") != 0
            or receipt_payload.get("status") != "COMPLETED"
            or not _valid_signature(receipt, profile.get("receipt_hmac_key_hex"))
        ):
            continue
        return True
    return False


__all__ = ["machine_evidence_is_trusted"]
