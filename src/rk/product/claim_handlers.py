"""S00 command and projection handlers for the atomic Claim validation gate."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from rk.domain import Decision, MissingCondition, RejectionCode, frozen_mapping
from rk.extensions import ExtensionRegistry, ProductCommandContext
from rk.product.claims import (
    ClaimArtifactBinding,
    ClaimError,
    ClaimKind,
    ClaimStore,
    ClaimSubmission,
)
from rk.product.validation_gateway import (
    ValidationError,
    ValidationEvidence,
    ValidationGateway,
    ValidationVerdict,
)
from rk.projector import ProjectionContext

_SUBMIT_FIELDS = {
    "statement",
    "claim_kind",
    "proof_or_evidence_artifacts",
    "predecessor_fact_ids",
    "source_binding_artifact",
    "work_item_id",
    "worker_run_id",
    "attempt_id",
}
_SUBMIT_OPTIONAL = {
    "route_id",
    "supersedes_claim_id",
    "public_summary",
}
_IMPORT_FIELDS = {
    "review_task_id",
    "signed_review_artifact",
    "target_digest",
    "verifier_receipt_ids",
}


@dataclass(frozen=True, slots=True)
class ClaimHandlers:
    store: ClaimStore
    gateway: ValidationGateway

    def submit_claim(self, context: ProductCommandContext) -> Decision:
        if context.capability.subject_role != "WORKER":
            return _reject(
                RejectionCode.CAPABILITY_DENIED,
                "REQUIRED_ACTION",
                "/command/type",
                role="WORKER",
            )
        contract = context.snapshot.get("contract")
        if not isinstance(contract, Mapping) or contract.get("status") != "FROZEN":
            return _reject(
                RejectionCode.CONTRACT_NOT_FROZEN,
                "CONTRACT_STATE",
                "/contract",
                required="FROZEN",
            )
        try:
            submission = _submission(context)
        except (ClaimError, KeyError, TypeError, ValueError):
            return _reject(
                RejectionCode.INGEST_SCHEMA_INVALID,
                "ATOMIC_CLAIM",
                "/command/payload",
            )
        return Decision(
            accepted=True,
            projection_mutations=(
                frozen_mapping(
                    {
                        "op": "B10_SUBMIT_CLAIM",
                        "submission": _submission_value(submission),
                        "subject_id": context.capability.subject_id,
                    }
                ),
            ),
            event_intents=(
                frozen_mapping(
                    {
                        "type": "CLAIM_SUBMITTED",
                        "command_type": "SUBMIT_CLAIM",
                        "authority_effect": "NONE",
                    }
                ),
            ),
        )

    def import_verification(self, context: ProductCommandContext) -> Decision:
        if context.capability.subject_role not in {"MACHINE_VERIFIER", "PEER_REVIEWER"}:
            return _reject(
                RejectionCode.CAPABILITY_DENIED,
                "REQUIRED_ACTION",
                "/command/type",
                role="VERIFIER",
            )
        payload = context.command.payload
        if set(payload) != _IMPORT_FIELDS:
            return _reject(
                RejectionCode.INGEST_SCHEMA_INVALID,
                "VERIFICATION_ENVELOPE",
                "/command/payload",
            )
        try:
            evidence = _imported_evidence(context)
            claim = self.store.get(evidence.claim_id)
            subgraph = self.store.necessary_subgraph(claim.claim_id)
            result = self.gateway.evaluate(
                claim,
                evidence,
                expected_subgraph_digest=subgraph.digest,
            )
            if payload["target_digest"] != claim.statement_digest:
                raise ValidationError("target digest does not match Claim")
        except (ClaimError, KeyError, TypeError, ValueError, ValidationError):
            return _reject(
                RejectionCode.EVIDENCE_SCOPE_MISMATCH,
                "VERIFICATION_BINDING",
                "/command/payload",
            )
        mutation = {
            "op": "B10_RECORD_VALIDATION",
            "result": dict(result.mutation_value()),
        }
        return Decision(
            accepted=True,
            projection_mutations=(frozen_mapping(mutation),),
            event_intents=(
                frozen_mapping(
                    {
                        "type": "CLAIM_VALIDATION_RECORDED",
                        "command_type": "IMPORT_VERIFICATION",
                        "claim_id": result.claim_id,
                        "promotion_eligible": result.promotion_eligible,
                    }
                ),
            ),
        )

    def apply_submit(
        self,
        connection: sqlite3.Connection,
        context: ProjectionContext,
        mutation: Mapping[str, Any],
    ) -> None:
        if set(mutation) != {"op", "submission", "subject_id"}:
            raise ValueError("B10 Claim submission mutation fields are invalid")
        value = mutation["submission"]
        if not isinstance(value, Mapping):
            raise ValueError("B10 Claim submission mutation is not an object")
        submission = _submission_from_value(value)
        if (
            submission.run_id != context.run_id
            or submission.contract_version != context.contract_version
            or submission.kernel_revision != context.revision - 1
        ):
            raise ValueError("B10 Claim mutation is not bound to kernel projection context")
        self.store.submit_in_transaction(
            connection, submission, subject_id=str(mutation["subject_id"])
        )

    def apply_validation(
        self,
        connection: sqlite3.Connection,
        context: ProjectionContext,
        mutation: Mapping[str, Any],
    ) -> None:
        if set(mutation) != {"op", "result"} or not isinstance(mutation["result"], Mapping):
            raise ValueError("B10 validation mutation fields are invalid")
        result = mutation["result"]
        self.store.record_validation_in_transaction(
            connection,
            result=result,
            kernel_receipt_id=context.command_id,
            kernel_event_id=context.event_id,
        )
        verdict = result.get("verdict")
        promotion_eligible = result.get("promotion_eligible") is True
        if verdict == ValidationVerdict.ACCEPTED.value and not promotion_eligible:
            return
        self.store.record_kernel_verdict_in_transaction(
            connection,
            claim_id=str(result["claim_id"]),
            statement_digest=str(result["statement_digest"]),
            contract_version=int(result["contract_version"]),
            validation_id=str(result["validation_id"]),
            accepted=verdict == ValidationVerdict.ACCEPTED.value,
            promotion_eligible=promotion_eligible,
            repair_feedback=(
                str(result["repair_feedback"])
                if result.get("repair_feedback") is not None
                else None
            ),
            kernel_receipt_id=context.command_id,
            kernel_event_id=context.event_id,
            kernel_revision=context.revision,
            authority_source="RESEARCH_KERNEL",
            command_type=context.command.type,
        )


def register_claim_handlers(
    registry: ExtensionRegistry, handlers: ClaimHandlers
) -> ExtensionRegistry:
    """Register the B10 command and mutation owners; duplicate ownership fails in S00."""

    return (
        registry.register_command_handler("SUBMIT_CLAIM", handlers.submit_claim)
        .register_command_handler("IMPORT_VERIFICATION", handlers.import_verification)
        .register_projection_mutation("B10_SUBMIT_CLAIM", handlers.apply_submit)
        .register_projection_mutation("B10_RECORD_VALIDATION", handlers.apply_validation)
    )


def _submission(context: ProductCommandContext) -> ClaimSubmission:
    payload = context.command.payload
    if set(payload) - _SUBMIT_OPTIONAL != _SUBMIT_FIELDS:
        raise ValueError("Claim payload fields are invalid")
    artifacts = _artifact_catalog(context.evidence_summary)
    proof = _sequence(payload["proof_or_evidence_artifacts"], "evidence artifacts")
    predecessors = _sequence(payload["predecessor_fact_ids"], "predecessors")
    return ClaimSubmission(
        run_id=context.run_id,
        contract_version=context.contract_version,
        kernel_revision=context.revision,
        statement=_string(payload["statement"]),
        claim_kind=ClaimKind(payload["claim_kind"]),
        proof_or_evidence_artifacts=tuple(_binding(item, artifacts) for item in proof),
        predecessor_fact_ids=tuple(_string(item) for item in predecessors),
        source_binding_artifact=_binding(payload["source_binding_artifact"], artifacts),
        work_item_id=_string(payload["work_item_id"]),
        worker_run_id=_string(payload["worker_run_id"]),
        attempt_id=_string(payload["attempt_id"]),
        route_id=_optional_string(payload.get("route_id")),
        supersedes_claim_id=_optional_string(payload.get("supersedes_claim_id")),
        public_summary=_optional_string(payload.get("public_summary")),
    )


def _imported_evidence(context: ProductCommandContext) -> ValidationEvidence:
    payload = context.command.payload
    task_id = _string(payload["review_task_id"])
    values = context.evidence_summary.get("validation_evidence_by_review_task")
    if not isinstance(values, Mapping) or not isinstance(values.get(task_id), Mapping):
        raise ValidationError("review task has no imported validation evidence")
    imported = values[task_id]
    assert isinstance(imported, Mapping)
    if set(imported) != {"signed_review_artifact", "verifier_receipt_ids", "validation"}:
        raise ValidationError("imported validation package fields are not exact")
    if imported["signed_review_artifact"] != payload["signed_review_artifact"]:
        raise ValidationError("signed review ArtifactRef does not match imported evidence")
    supplied_refs = _sequence(payload["verifier_receipt_ids"], "verifier receipt IDs")
    imported_refs = _sequence(imported["verifier_receipt_ids"], "imported verifier receipts")
    if tuple(supplied_refs) != tuple(imported_refs):
        raise ValidationError("verifier receipt list does not match imported evidence")
    validation = imported["validation"]
    if not isinstance(validation, Mapping):
        raise ValidationError("validation evidence is not an object")
    return ValidationEvidence.from_mapping(validation)


def _submission_value(value: ClaimSubmission) -> dict[str, Any]:
    result: dict[str, Any] = {
        "run_id": value.run_id,
        "contract_version": value.contract_version,
        "kernel_revision": value.kernel_revision,
        "statement": value.normalized_statement,
        "claim_kind": value.claim_kind.value,
        "proof_or_evidence_artifacts": [
            item.to_dict() for item in value.proof_or_evidence_artifacts
        ],
        "predecessor_fact_ids": list(value.predecessor_fact_ids),
        "source_binding_artifact": value.source_binding_artifact.to_dict(),
        "work_item_id": value.work_item_id,
        "worker_run_id": value.worker_run_id,
        "attempt_id": value.attempt_id,
    }
    for name in ("route_id", "supersedes_claim_id", "public_summary"):
        item = getattr(value, name)
        if item is not None:
            result[name] = item
    return result


def _submission_from_value(value: Mapping[str, Any]) -> ClaimSubmission:
    required = _SUBMIT_FIELDS | {"run_id", "contract_version", "kernel_revision"}
    if set(value) - _SUBMIT_OPTIONAL != required:
        raise ValueError("stored Claim submission fields are invalid")
    return ClaimSubmission(
        run_id=_string(value["run_id"]),
        contract_version=_integer(value["contract_version"]),
        kernel_revision=_natural(value["kernel_revision"]),
        statement=_string(value["statement"]),
        claim_kind=ClaimKind(value["claim_kind"]),
        proof_or_evidence_artifacts=tuple(
            _full_binding(item)
            for item in _sequence(value["proof_or_evidence_artifacts"], "evidence")
        ),
        predecessor_fact_ids=tuple(
            _string(item) for item in _sequence(value["predecessor_fact_ids"], "predecessors")
        ),
        source_binding_artifact=_full_binding(value["source_binding_artifact"]),
        work_item_id=_string(value["work_item_id"]),
        worker_run_id=_string(value["worker_run_id"]),
        attempt_id=_string(value["attempt_id"]),
        route_id=_optional_string(value.get("route_id")),
        supersedes_claim_id=_optional_string(value.get("supersedes_claim_id")),
        public_summary=_optional_string(value.get("public_summary")),
    )


def _artifact_catalog(value: Mapping[str, Any]) -> Mapping[str, Any]:
    artifacts = value.get("committed_artifacts")
    if not isinstance(artifacts, Mapping):
        raise ValueError("committed artifact catalog is unavailable")
    return artifacts


def _binding(value: object, artifacts: Mapping[str, Any]) -> ClaimArtifactBinding:
    if not isinstance(value, Mapping) or set(value) != {"artifact_id", "sha256"}:
        raise ValueError("artifact binding fields are invalid")
    artifact_id = _string(value["artifact_id"])
    metadata = artifacts.get(artifact_id)
    if (
        not isinstance(metadata, Mapping)
        or metadata.get("sha256") != value["sha256"]
        or metadata.get("ingest_state") != "COMMITTED"
    ):
        raise ValueError("artifact binding is not committed with exact digest")
    return ClaimArtifactBinding(
        artifact_id,
        _string(value["sha256"]),
        _natural(metadata["byte_count"]),
        _string(metadata["media_type"]),
    )


def _full_binding(value: object) -> ClaimArtifactBinding:
    if not isinstance(value, Mapping) or set(value) != {
        "artifact_id",
        "sha256",
        "byte_count",
        "media_type",
    }:
        raise ValueError("full artifact binding fields are invalid")
    return ClaimArtifactBinding(
        _string(value["artifact_id"]),
        _string(value["sha256"]),
        _natural(value["byte_count"]),
        _string(value["media_type"]),
    )


def _sequence(value: object, label: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ValueError(f"{label} must be an array")
    return value


def _string(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("value must be a non-empty string")
    return value


def _optional_string(value: object) -> str | None:
    return None if value is None else _string(value)


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("value must be a positive integer")
    return value


def _natural(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise ValueError("value must be a natural integer")
    return value


def _reject(
    code: RejectionCode,
    condition: str,
    path: str,
    **params: Any,
) -> Decision:
    return Decision(
        accepted=False,
        rejection_code=code.value,
        missing_conditions=(MissingCondition(condition, path, MappingProxyType(dict(params))),),
    )


__all__ = ["ClaimHandlers", "register_claim_handlers"]
