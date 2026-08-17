"""One fail-closed validator for DB-backed authority-bearing machine evidence."""

from __future__ import annotations

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
    """Accept only a one-shot host receipt already scoped and consumed in SQLite."""

    del policy
    evidence_type = evidence.get("evidence_type")
    expected_roots = {
        "LEAN_REPLAY": {"LEAN_KERNEL"},
        "CHECKER_CERTIFICATE": {"CHECKER", "ENUMERATION"},
    }
    expected_root = expected_roots.get(str(evidence_type))
    if (
        expected_root is None
        or (expected_type is not None and evidence_type != expected_type)
        or evidence.get("evidence_strength") != "HARD_MACHINE"
        or _status(evidence) not in {"ACTIVE", "COMMITTED", "ACCEPTED"}
        or evidence.get("run_id", run_id) != run_id
        or evidence.get("claim_id") != target_claim.get("claim_id")
        or evidence.get("contract_version") != contract_version
        or evidence.get("statement_hash") != target_claim.get("statement_hash")
        or evidence.get("root_kind") not in expected_root
    ):
        return False
    feedback = _records(projection.get("lean_feedback"), "lean_feedback_id")
    receipts = _records(projection.get("host_execution_receipts"), "receipt_id")
    artifacts = _records(projection.get("artifacts"), "artifact_id")
    output = artifacts.get(str(evidence.get("artifact_id")))
    if output is None or _status(output) != "COMMITTED":
        return False
    if evidence_type == "CHECKER_CERTIFICATE":
        verification_id = next(
            (
                str(item.get("verification_id"))
                for item in projection.get("atomic_verifications", ())
                if isinstance(item, Mapping)
                and item.get("claim_id") == target_claim.get("claim_id")
                and item.get("verification_ref") == evidence.get("evidence_id")
                and item.get("backend") == "DETERMINISTIC_CHECKER"
                and item.get("verdict") == "ACCEPTED"
            ),
            "",
        )
        return bool(
            verification_id
            and any(
                receipt.get("authority_eligible") == 1
                and receipt.get("block_reasons") in ([], ())
                and receipt.get("run_id") == run_id
                and receipt.get("claim_id") == target_claim.get("claim_id")
                and receipt.get("contract_version") == contract_version
                and receipt.get("statement_hash") == target_claim.get("statement_hash")
                and receipt.get("status") == "COMPLETED"
                and receipt.get("exit_code") == 0
                and receipt.get("consumed_by_verification_id") == verification_id
                and receipt.get("checker_consumed_at")
                for receipt in receipts.values()
            )
        )
    for record in feedback.values():
        diagnostic = record.get("diagnostic")
        receipt_id = diagnostic.get("host_receipt_id") if isinstance(diagnostic, Mapping) else None
        receipt = receipts.get(str(receipt_id))
        if (
            receipt is None
            or receipt.get("adapter_name") != "lean-replay"
            or receipt.get("authority_eligible") != 1
            or receipt.get("block_reasons") not in ([], ())
            or not receipt.get("dependency_closure_digest")
            or receipt.get("consumed_by_feedback_id") != record.get("lean_feedback_id")
            or receipt.get("run_id") != run_id
            or receipt.get("claim_id") != target_claim.get("claim_id")
            or receipt.get("contract_version") != contract_version
            or receipt.get("statement_hash") != target_claim.get("statement_hash")
            or receipt.get("status") != "COMPLETED"
            or receipt.get("exit_code") != 0
            or record.get("feedback_kind") != "REPLAY_PASS"
            or not isinstance(diagnostic, Mapping)
            or diagnostic.get("host_receipt_id") != receipt.get("receipt_id")
            or record.get("claim_id") != target_claim.get("claim_id")
            or record.get("contract_version") != contract_version
            or record.get("output_artifact_id") != evidence.get("artifact_id")
            or receipt.get("output_sha256") != output.get("sha256")
            or not receipt.get("consumed_at")
        ):
            continue
        return True
    return False


__all__ = ["machine_evidence_is_trusted"]
