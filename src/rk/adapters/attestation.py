"""Verification seam for independently signed human-review artifacts.

The adapter validates identity, signature, exact target bindings and review structure.  It never
creates review answers: blindness, author/subject identities, the mathematical verdict and every
check conclusion must already be present in the signed artifact supplied by the verifier.
"""

from __future__ import annotations

import base64
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from rk.adapters.base import AdapterRequestError, canonical_json_sha256, require_exact_keys

_SIX_PARTS = (
    "coverage",
    "compatibility",
    "invariant",
    "progress",
    "boundary",
    "simultaneous_choice",
)
_COMPOSITION_CHECKS = ("proof_checked", "scope_checked", *_SIX_PARTS)
_PAPER_CHECKS = (
    "mathematical_consistency",
    "dependency_closure",
    "claim_statements",
    "proof_bodies",
    "citations",
    "revoked_facts_excluded",
)


def _canonical(value: Mapping[str, Any]) -> bytes:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    ).encode("utf-8")


def _decode_signature(value: str) -> bytes:
    try:
        return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))
    except (ValueError, TypeError) as error:
        raise AdapterRequestError("verifier signature is malformed") from error


def _digest(value: Any, *, label: str) -> str:
    if (
        not isinstance(value, str)
        or len(value) != 64
        or any(char not in "0123456789abcdef" for char in value)
    ):
        raise AdapterRequestError(f"{label} must be a lowercase SHA-256 digest")
    return value


@dataclass(frozen=True, slots=True)
class VerifierIdentity:
    identity_id: str
    subject_id: str
    public_key_sha256: str
    verify_signature: Callable[[bytes, bytes], bool]
    active: bool = True

    def __post_init__(self) -> None:
        if not self.identity_id or not self.subject_id:
            raise ValueError("verifier identity requires identity and subject IDs")
        _digest(self.public_key_sha256, label="public_key_sha256")


class IndependentVerifierArtifactAdapter:
    """Validate signed review artifacts and return narrow kernel-import fields."""

    name = "independent-verifier-artifact"
    version = "1"
    trust_limit = "HUMAN_ATTESTED_IF_ELIGIBLE"

    def __init__(self, identities: Mapping[str, VerifierIdentity]) -> None:
        if not identities:
            raise ValueError("at least one verifier identity is required")
        normalized = dict(identities)
        if any(key != identity.identity_id for key, identity in normalized.items()):
            raise ValueError("identity registry keys must match identity_id")
        self.identities = MappingProxyType(normalized)

    def run(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        require_exact_keys(
            request,
            required=frozenset({"artifact", "expected_binding"}),
            label="independent verifier import",
        )
        artifact, expected = request["artifact"], request["expected_binding"]
        if not isinstance(artifact, Mapping) or not isinstance(expected, Mapping):
            raise AdapterRequestError("artifact and expected_binding must be objects")
        require_exact_keys(
            artifact,
            required=frozenset(
                {
                    "schema_version",
                    "artifact_kind",
                    "review_id",
                    "verifier_identity_id",
                    "issued_at",
                    "binding",
                    "independence",
                    "verdict",
                    "checks",
                    "signature",
                }
            ),
            label="signed verifier artifact",
        )
        if artifact["schema_version"] != "rk.independent-verifier.v1":
            raise AdapterRequestError("unsupported verifier artifact schema")
        if not isinstance(artifact["review_id"], str) or not artifact["review_id"]:
            raise AdapterRequestError("review_id is required")
        if not isinstance(artifact["issued_at"], str) or not artifact["issued_at"]:
            raise AdapterRequestError("issued_at is required")
        kind = artifact["artifact_kind"]
        if kind not in {"ATOMIC_CLAIM_REVIEW", "COMPOSITION_REVIEW", "PAPER_REVIEW"}:
            raise AdapterRequestError("unsupported verifier artifact kind")
        identity_id = artifact["verifier_identity_id"]
        identity = self.identities.get(str(identity_id))
        if identity is None or not identity.active:
            return self._ineligible(artifact, "UNKNOWN_OR_INACTIVE_IDENTITY")
        body = dict(artifact)
        signature_raw = body.pop("signature")
        if not isinstance(signature_raw, str):
            raise AdapterRequestError("verifier signature must be a string")
        signed_message = b"rk.independent-verifier.v1\n" + _canonical(body)
        try:
            signature_valid = identity.verify_signature(
                signed_message, _decode_signature(signature_raw)
            )
        except (TypeError, ValueError):
            signature_valid = False
        if not signature_valid:
            return self._ineligible(artifact, "INVALID_SIGNATURE")
        binding = artifact["binding"]
        if not isinstance(binding, Mapping) or dict(binding) != dict(expected):
            return self._ineligible(artifact, "TARGET_BINDING_MISMATCH")
        try:
            if kind == "ATOMIC_CLAIM_REVIEW":
                normalized = self._atomic(artifact, identity)
            elif kind == "COMPOSITION_REVIEW":
                normalized = self._composition(artifact, identity)
            else:
                normalized = self._paper(artifact, identity)
        except AdapterRequestError as error:
            return self._ineligible(artifact, "REVIEW_SCHEMA_MISMATCH", detail=str(error))
        positive = artifact["verdict"] in {"ACCEPT", "CORRECT"}
        return {
            "status": "COMPLETED",
            "promotion_eligible": positive,
            "authority": "HUMAN_ATTESTED" if positive else "REVIEW_FEEDBACK_ONLY",
            "artifact_sha256": canonical_json_sha256(artifact),
            "review_id": artifact["review_id"],
            "verifier_identity_id": identity.identity_id,
            "verifier_subject_id": identity.subject_id,
            "verifier_public_key_sha256": identity.public_key_sha256,
            "artifact_kind": kind,
            "import_fields": normalized,
        }

    def _atomic(
        self, artifact: Mapping[str, Any], identity: VerifierIdentity
    ) -> Mapping[str, Any]:
        binding = self._binding(
            artifact["binding"],
            {"run_id", "contract_version", "claim_id", "statement_hash"},
        )
        independence = self._independence(artifact["independence"], identity)
        verdict = artifact["verdict"]
        if verdict not in {"ACCEPT", "REJECT", "NEEDS_REVISION"}:
            raise AdapterRequestError("invalid atomic-claim verdict")
        checks = self._checks(
            artifact["checks"], ("proof_checked", "scope_checked"), "HUMAN_ATTESTED"
        )
        return {
            "claim_id": binding["claim_id"],
            "contract_version": binding["contract_version"],
            "statement_hash": binding["statement_hash"],
            "verdict": verdict,
            "checklist": {
                "proof_checked": checks["proof_checked"],
                "scope_checked": checks["scope_checked"],
                "blind_review": independence["blind_review"],
            },
            "source_graph": {
                "author_subject_ids": list(independence["author_subject_ids"]),
                "verifier_subject_id": identity.subject_id,
                "saw_other_verdicts": independence["saw_other_verdicts"],
            },
        }

    def _composition(
        self, artifact: Mapping[str, Any], identity: VerifierIdentity
    ) -> Mapping[str, Any]:
        binding = self._binding(
            artifact["binding"],
            {
                "run_id",
                "contract_version",
                "claim_id",
                "statement_hash",
                "selected_subgraph_digest",
            },
        )
        independence = self._independence(artifact["independence"], identity)
        verdict = artifact["verdict"]
        if verdict not in {"ACCEPT", "REJECT", "NEEDS_REVISION"}:
            raise AdapterRequestError("invalid composition verdict")
        checks = self._checks(artifact["checks"], _COMPOSITION_CHECKS, "HUMAN_ATTESTED")
        return {
            "claim_id": binding["claim_id"],
            "contract_version": binding["contract_version"],
            "statement_hash": binding["statement_hash"],
            "selected_subgraph_digest": binding["selected_subgraph_digest"],
            "verdict": verdict,
            "checklist": {
                "proof_checked": checks["proof_checked"],
                "scope_checked": checks["scope_checked"],
                "blind_review": independence["blind_review"],
                "six_parts": {name: checks[name] for name in _SIX_PARTS},
            },
            "source_graph": {
                "author_subject_ids": list(independence["author_subject_ids"]),
                "verifier_subject_id": identity.subject_id,
                "saw_other_verdicts": independence["saw_other_verdicts"],
            },
        }

    def _paper(
        self, artifact: Mapping[str, Any], identity: VerifierIdentity
    ) -> Mapping[str, Any]:
        binding = self._binding(
            artifact["binding"],
            {"run_id", "contract_version", "final_fact_id", "paper_sha256"},
        )
        independence = self._independence(artifact["independence"], identity)
        verdict = artifact["verdict"]
        if verdict not in {"CORRECT", "INCORRECT", "NEEDS_REVISION"}:
            raise AdapterRequestError("invalid whole-paper verdict")
        checks = self._checks(artifact["checks"], _PAPER_CHECKS, "CHECKED")
        return {
            "contract_version": binding["contract_version"],
            "final_fact_id": binding["final_fact_id"],
            "paper_sha256": binding["paper_sha256"],
            "status": verdict,
            "whole_paper_review": True,
            "checks": checks,
            "independence": independence,
        }

    @staticmethod
    def _binding(value: Any, fields: set[str]) -> Mapping[str, Any]:
        if not isinstance(value, Mapping) or set(value) != fields:
            raise AdapterRequestError("review binding fields are incomplete or unknown")
        if not isinstance(value["run_id"], str) or not value["run_id"]:
            raise AdapterRequestError("run_id is required")
        if not isinstance(value["contract_version"], int) or value["contract_version"] < 1:
            raise AdapterRequestError("contract_version must be positive")
        for name in fields & {"statement_hash", "selected_subgraph_digest", "paper_sha256"}:
            _digest(value[name], label=name)
        for name in fields & {"claim_id", "final_fact_id"}:
            if not isinstance(value[name], str) or not value[name]:
                raise AdapterRequestError(f"{name} is required")
        return value

    @staticmethod
    def _independence(value: Any, identity: VerifierIdentity) -> Mapping[str, Any]:
        if not isinstance(value, Mapping):
            raise AdapterRequestError("independence must be an object")
        require_exact_keys(
            value,
            required=frozenset(
                {"blind_review", "author_subject_ids", "verifier_subject_id", "saw_other_verdicts"}
            ),
            label="signed independence statement",
        )
        authors = value["author_subject_ids"]
        if (
            value["blind_review"] is not True
            or value["saw_other_verdicts"] is not False
            or value["verifier_subject_id"] != identity.subject_id
            or not isinstance(authors, Sequence)
            or isinstance(authors, (str, bytes))
            or not authors
            or any(not isinstance(item, str) or not item for item in authors)
            or identity.subject_id in authors
        ):
            raise AdapterRequestError("signed independence requirements are not satisfied")
        return value

    @staticmethod
    def _checks(value: Any, names: tuple[str, ...], required_status: str) -> dict[str, Any]:
        if not isinstance(value, Mapping) or set(value) != set(names):
            raise AdapterRequestError("review checks must contain every required part exactly once")
        normalized: dict[str, Any] = {}
        for name in names:
            item = value[name]
            if not isinstance(item, Mapping):
                raise AdapterRequestError(f"check {name} must be an object")
            require_exact_keys(
                item,
                required=frozenset({"passed", "status", "conclusion", "evidence_refs"}),
                label=f"review check {name}",
            )
            refs = item["evidence_refs"]
            if (
                item["passed"] is not True
                or item["status"] != required_status
                or not isinstance(item["conclusion"], str)
                or not item["conclusion"].strip()
                or not isinstance(refs, Sequence)
                or isinstance(refs, (str, bytes))
                or any(not isinstance(ref, str) or not ref for ref in refs)
            ):
                raise AdapterRequestError(f"check {name} is incomplete")
            normalized[name] = dict(item)
        return normalized

    @staticmethod
    def _ineligible(
        artifact: Mapping[str, Any], reason: str, *, detail: str | None = None
    ) -> Mapping[str, Any]:
        result: dict[str, Any] = {
            "status": "REJECTED",
            "promotion_eligible": False,
            "authority": "NONE",
            "reason": reason,
            "artifact_sha256": canonical_json_sha256(artifact),
        }
        if detail is not None:
            result["detail"] = detail
        return result
