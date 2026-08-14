"""Unified validation routing without a fact-graph write capability."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any

from rk.product.claims import ClaimKind, ClaimRecord


class ValidationError(RuntimeError):
    """Validation evidence is malformed, insufficient, or bound to another Claim."""


class ValidationBackend(StrEnum):
    LEAN = "LEAN"
    DETERMINISTIC_CHECKER = "DETERMINISTIC_CHECKER"
    MANAGED_HUMAN = "MANAGED_HUMAN"
    SOFT_VERIFIER = "SOFT_VERIFIER"


class ValidationVerdict(StrEnum):
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"


@dataclass(frozen=True, slots=True)
class VerifierPlan:
    claim_id: str
    required_backends: tuple[ValidationBackend, ...]
    supplementary_backends: tuple[ValidationBackend, ...]
    selected_subgraph_digest: str


@dataclass(frozen=True, slots=True)
class ValidationEvidence:
    validation_id: str
    claim_id: str
    run_id: str
    contract_version: int
    statement_digest: str
    selected_subgraph_digest: str
    backend: ValidationBackend
    verdict: ValidationVerdict
    verifier_reference_id: str
    authority_effect: str
    proof_checked: bool
    scope_checked: bool
    independence_verified: bool
    repair_feedback: str | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> ValidationEvidence:
        required = {
            "validation_id",
            "claim_id",
            "run_id",
            "contract_version",
            "statement_digest",
            "selected_subgraph_digest",
            "backend",
            "verdict",
            "verifier_reference_id",
            "authority_effect",
            "proof_checked",
            "scope_checked",
            "independence_verified",
        }
        if set(value) not in (required, required | {"repair_feedback"}):
            raise ValidationError("validation evidence fields are not exact")
        booleans = (
            value["proof_checked"],
            value["scope_checked"],
            value["independence_verified"],
        )
        if any(not isinstance(item, bool) for item in booleans):
            raise ValidationError("validation checks must be booleans")
        try:
            return cls(
                validation_id=_nonempty(value["validation_id"]),
                claim_id=_nonempty(value["claim_id"]),
                run_id=_nonempty(value["run_id"]),
                contract_version=_integer(value["contract_version"]),
                statement_digest=_digest(value["statement_digest"]),
                selected_subgraph_digest=_digest(value["selected_subgraph_digest"]),
                backend=ValidationBackend(value["backend"]),
                verdict=ValidationVerdict(value["verdict"]),
                verifier_reference_id=_nonempty(value["verifier_reference_id"]),
                authority_effect=_nonempty(value["authority_effect"]),
                proof_checked=bool(value["proof_checked"]),
                scope_checked=bool(value["scope_checked"]),
                independence_verified=bool(value["independence_verified"]),
                repair_feedback=(
                    _nonempty(value["repair_feedback"]) if "repair_feedback" in value else None
                ),
            )
        except (TypeError, ValueError) as error:
            raise ValidationError("validation evidence values are invalid") from error


@dataclass(frozen=True, slots=True)
class ValidationResult:
    validation_id: str
    claim_id: str
    statement_digest: str
    contract_version: int
    selected_subgraph_digest: str
    backend: ValidationBackend
    verdict: ValidationVerdict
    verifier_reference_id: str
    promotion_eligible: bool
    authority_effect: str
    repair_feedback: str | None

    def mutation_value(self) -> Mapping[str, Any]:
        return MappingProxyType(
            {
                "validation_id": self.validation_id,
                "claim_id": self.claim_id,
                "statement_digest": self.statement_digest,
                "contract_version": self.contract_version,
                "selected_subgraph_digest": self.selected_subgraph_digest,
                "backend": self.backend.value,
                "verdict": self.verdict.value,
                "verifier_reference_id": self.verifier_reference_id,
                "promotion_eligible": self.promotion_eligible,
                "authority_effect": self.authority_effect,
                "repair_feedback": self.repair_feedback,
            }
        )


class ValidationGateway:
    """Route and check evidence; it deliberately exposes no graph mutation method."""

    def plan(
        self,
        claim: ClaimRecord,
        *,
        selected_subgraph_digest: str,
        allowed_backends: Sequence[str],
    ) -> VerifierPlan:
        digest = _digest(selected_subgraph_digest)
        allowed = tuple(ValidationBackend(item) for item in allowed_backends)
        if len(set(allowed)) != len(allowed):
            raise ValidationError("allowed verifier backends must be unique")
        preferred = (
            (ValidationBackend.LEAN, ValidationBackend.MANAGED_HUMAN)
            if claim.claim_kind in {ClaimKind.ROOT, ClaimKind.LEMMA, ClaimKind.DEFINITION}
            else (ValidationBackend.DETERMINISTIC_CHECKER, ValidationBackend.MANAGED_HUMAN)
        )
        required = tuple(item for item in preferred if item in allowed)
        if not required:
            raise ValidationError("contract permits no authority-bearing verifier for Claim kind")
        supplementary = (
            (ValidationBackend.SOFT_VERIFIER,) if ValidationBackend.SOFT_VERIFIER in allowed else ()
        )
        return VerifierPlan(claim.claim_id, required, supplementary, digest)

    def evaluate(
        self,
        claim: ClaimRecord,
        evidence: ValidationEvidence,
        *,
        expected_subgraph_digest: str,
    ) -> ValidationResult:
        expected = _digest(expected_subgraph_digest)
        if (
            evidence.claim_id != claim.claim_id
            or evidence.run_id != claim.run_id
            or evidence.contract_version != claim.contract_version
            or evidence.statement_digest != claim.statement_digest
            or evidence.selected_subgraph_digest != expected
        ):
            raise ValidationError("validation evidence binding does not match Claim")
        if evidence.verdict is ValidationVerdict.REJECTED:
            if not evidence.repair_feedback:
                raise ValidationError("rejected Claim requires repair feedback")
            return ValidationResult(
                evidence.validation_id,
                claim.claim_id,
                claim.statement_digest,
                claim.contract_version,
                expected,
                evidence.backend,
                evidence.verdict,
                evidence.verifier_reference_id,
                False,
                evidence.authority_effect,
                evidence.repair_feedback,
            )
        eligible = False
        if evidence.backend in {
            ValidationBackend.LEAN,
            ValidationBackend.DETERMINISTIC_CHECKER,
        }:
            eligible = (
                evidence.proof_checked
                and evidence.scope_checked
                and evidence.authority_effect == "MACHINE_CHECKED"
            )
        elif evidence.backend is ValidationBackend.MANAGED_HUMAN:
            eligible = (
                evidence.proof_checked
                and evidence.scope_checked
                and evidence.independence_verified
                and evidence.authority_effect == "HUMAN_ATTESTED"
            )
        elif evidence.backend is ValidationBackend.SOFT_VERIFIER:
            if evidence.authority_effect != "NONE":
                raise ValidationError("soft verifier must have authority_effect NONE")
            eligible = False
        if not eligible and evidence.backend is not ValidationBackend.SOFT_VERIFIER:
            raise ValidationError("authority-bearing verifier checks are incomplete")
        return ValidationResult(
            evidence.validation_id,
            claim.claim_id,
            claim.statement_digest,
            claim.contract_version,
            expected,
            evidence.backend,
            evidence.verdict,
            evidence.verifier_reference_id,
            eligible,
            evidence.authority_effect,
            None,
        )

    @staticmethod
    def digest_plan(plan: VerifierPlan) -> str:
        value = {
            "claim_id": plan.claim_id,
            "required_backends": [item.value for item in plan.required_backends],
            "supplementary_backends": [item.value for item in plan.supplementary_backends],
            "selected_subgraph_digest": plan.selected_subgraph_digest,
        }
        return hashlib.sha256(
            json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False).encode()
        ).hexdigest()


def _nonempty(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("value must be a non-empty string")
    return value


def _integer(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise ValueError("value must be a positive integer")
    return value


def _digest(value: object) -> str:
    text = _nonempty(value)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise ValueError("value must be a lowercase sha256")
    return text


__all__ = [
    "ValidationBackend",
    "ValidationError",
    "ValidationEvidence",
    "ValidationGateway",
    "ValidationResult",
    "ValidationVerdict",
    "VerifierPlan",
]
