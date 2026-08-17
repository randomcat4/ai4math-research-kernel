"""Import exact C00 review artifacts through artifact, schema, signature, and task gates."""

from __future__ import annotations

import hashlib
import hmac
import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol

from jsonschema import Draft202012Validator, FormatChecker

from rk.product.artifact_read import ArtifactReadService, ExactArtifactRef
from rk.product.reviews import (
    ReviewArtifactRef,
    ReviewTask,
    ReviewTaskStateError,
    ReviewTaskStatus,
    ReviewTaskStore,
    ReviewType,
)
from rk.wire import canonical_json_bytes


class AttestationImportError(RuntimeError):
    """A signed review artifact failed a named import gate."""

    def __init__(self, code: str, detail: str) -> None:
        super().__init__(f"{code}: {detail}")
        self.code = code
        self.detail = detail


class TrustClass(StrEnum):
    MANAGED_PEER_REVIEW = "MANAGED_PEER_REVIEW"
    UNMANAGED_REVIEW = "UNMANAGED_REVIEW"


class AuthorityEffect(StrEnum):
    PEER_PROMOTION_ELIGIBLE = "PEER_PROMOTION_ELIGIBLE"
    NONE = "NONE"


@dataclass(frozen=True, slots=True)
class SignatureAuthority:
    signature_valid: bool
    trust_class: TrustClass
    authority_effect: AuthorityEffect
    promotion_eligible: bool


class ArtifactContentReader(Protocol):
    """Read immutable CAS content by exact artifact identity; never accepts a host path."""

    def read_bytes(self, artifact_ref: ReviewArtifactRef) -> bytes: ...


class ArtifactReadContentReader:
    """B04b adapter that resolves an exact ref and consumes its bounded byte stream."""

    def __init__(self, service: ArtifactReadService) -> None:
        self._service = service

    def read_bytes(self, artifact_ref: ReviewArtifactRef) -> bytes:
        expected = ExactArtifactRef(
            artifact_id=artifact_ref.artifact_id,
            sha256=artifact_ref.sha256,
            byte_count=artifact_ref.byte_count,
            media_type=artifact_ref.media_type,
        )
        result = self._service.open_range(
            artifact_ref.artifact_id, expected_ref=expected
        )
        return b"".join(result.stream)


class SignatureVerifier(Protocol):
    """Verify bytes and return authority already registered for the signing key."""

    def verify(
        self,
        *,
        algorithm: str,
        key_id: str,
        verifier_identity_id: str,
        signed_payload: bytes,
        signature_value: str,
    ) -> SignatureAuthority: ...


@dataclass(frozen=True, slots=True)
class HmacAttestationKey:
    secret: bytes
    verifier_identity_id: str
    trust_class: TrustClass
    authority_effect: AuthorityEffect
    promotion_eligible: bool

    def __post_init__(self) -> None:
        if len(self.secret) < 32:
            raise ValueError("HMAC attestation keys must contain at least 32 bytes")
        if not self.verifier_identity_id:
            raise ValueError("verifier_identity_id must be non-empty")


class HmacKeyringVerifier:
    """Real HMAC-SHA256 verifier with host-owned key authority metadata."""

    def __init__(self, keys: Mapping[str, HmacAttestationKey]) -> None:
        if not keys or any(not key_id for key_id in keys):
            raise ValueError("attestation keyring must contain named keys")
        self._keys = MappingProxyType(dict(keys))

    def verify(
        self,
        *,
        algorithm: str,
        key_id: str,
        verifier_identity_id: str,
        signed_payload: bytes,
        signature_value: str,
    ) -> SignatureAuthority:
        try:
            key = self._keys[key_id]
        except KeyError as error:
            raise AttestationImportError("SIGNING_KEY_UNKNOWN", key_id) from error
        valid = (
            algorithm == "HMAC_SHA256"
            and verifier_identity_id == key.verifier_identity_id
            and hmac.compare_digest(
                hmac.new(key.secret, signed_payload, hashlib.sha256).hexdigest(),
                signature_value,
            )
        )
        return SignatureAuthority(
            signature_valid=valid,
            trust_class=key.trust_class,
            authority_effect=key.authority_effect,
            promotion_eligible=key.promotion_eligible,
        )


@dataclass(frozen=True, slots=True)
class ImportedReview:
    artifact_ref: ReviewArtifactRef
    task: ReviewTask
    review_id: str
    review_type: ReviewType
    review: Mapping[str, Any]
    trust_class: TrustClass
    authority_effect: AuthorityEffect
    promotion_eligible: bool


class ReviewAttestationImporter:
    """Validate an original signed artifact and attach only its immutable reference."""

    def __init__(
        self,
        *,
        tasks: ReviewTaskStore,
        artifacts: ArtifactContentReader,
        signatures: SignatureVerifier,
        review_schema_path: Path,
    ) -> None:
        schema_value = json.loads(Path(review_schema_path).read_text(encoding="utf-8"))
        if not isinstance(schema_value, dict):
            raise ValueError("review schema must be a JSON object")
        Draft202012Validator.check_schema(schema_value)
        self._validator = Draft202012Validator(
            schema_value, format_checker=FormatChecker()
        )
        self._tasks = tasks
        self._artifacts = artifacts
        self._signatures = signatures

    def import_artifact(
        self,
        *,
        review_task_id: str,
        artifact_ref: ReviewArtifactRef,
        submitted_at: str,
    ) -> ImportedReview:
        raw = self._read_exact_artifact(artifact_ref)
        review = _decode_object(raw)
        errors = sorted(self._validator.iter_errors(review), key=lambda item: list(item.path))
        if errors:
            detail = "; ".join(
                f"/{'/'.join(str(part) for part in error.path)}: {error.message}"
                for error in errors[:8]
            )
            raise AttestationImportError("REVIEW_SCHEMA_INVALID", detail)

        task = self._tasks.get(review_task_id)
        self._verify_task_binding(task, review)
        signed_payload = canonical_review_payload(review)
        signature = review["signature"]
        assert isinstance(signature, dict)
        claimed_digest = str(signature["signed_payload_sha256"])
        actual_digest = hashlib.sha256(signed_payload).hexdigest()
        if not hmac.compare_digest(claimed_digest, actual_digest):
            raise AttestationImportError(
                "SIGNED_PAYLOAD_DIGEST_MISMATCH",
                "signature digest does not bind the submitted review payload",
            )
        authority = self._signatures.verify(
            algorithm=str(signature["algorithm"]),
            key_id=str(signature["key_id"]),
            verifier_identity_id=str(review["verifier_identity_id"]),
            signed_payload=signed_payload,
            signature_value=str(signature["value"]),
        )
        if not authority.signature_valid:
            raise AttestationImportError("SIGNATURE_INVALID", "signature verification failed")
        if (
            authority.trust_class is not TrustClass.MANAGED_PEER_REVIEW
            or authority.authority_effect is not AuthorityEffect.PEER_PROMOTION_ELIGIBLE
            or authority.promotion_eligible is not True
        ):
            raise AttestationImportError(
                "REVIEW_AUTHORITY_INELIGIBLE",
                "review key is UNMANAGED_REVIEW or has NONE authority effect",
            )

        recorded = self._tasks._record_verified_artifact(
            review_task_id,
            artifact_ref=artifact_ref,
            submitted_at=submitted_at,
        )
        frozen_review = _freeze_json(review)
        assert isinstance(frozen_review, Mapping)
        return ImportedReview(
            artifact_ref=artifact_ref,
            task=recorded,
            review_id=str(review["review_id"]),
            review_type=ReviewType(str(review["review_type"])),
            review=frozen_review,
            trust_class=authority.trust_class,
            authority_effect=authority.authority_effect,
            promotion_eligible=authority.promotion_eligible,
        )

    def _read_exact_artifact(self, ref: ReviewArtifactRef) -> bytes:
        if ref.media_type != "application/json":
            raise AttestationImportError(
                "REVIEW_ARTIFACT_MEDIA_TYPE_INVALID", ref.media_type
            )
        raw = self._artifacts.read_bytes(ref)
        if len(raw) != ref.byte_count:
            raise AttestationImportError(
                "REVIEW_ARTIFACT_LENGTH_MISMATCH", "ArtifactRef byte_count differs"
            )
        digest = hashlib.sha256(raw).hexdigest()
        if not hmac.compare_digest(digest, ref.sha256):
            raise AttestationImportError(
                "REVIEW_ARTIFACT_DIGEST_MISMATCH", "ArtifactRef sha256 differs"
            )
        return raw

    @staticmethod
    def _verify_task_binding(task: ReviewTask, review: dict[str, Any]) -> None:
        if task.status not in (ReviewTaskStatus.CLAIMED, ReviewTaskStatus.SUBMITTED):
            raise ReviewTaskStateError("REVIEW_TASK_NOT_CLAIMED")
        if str(review["review_task_id"]) != task.review_task_id:
            raise AttestationImportError("TASK_BINDING_MISMATCH", "review_task_id differs")
        if str(review["review_type"]) != task.review_type.value:
            raise AttestationImportError("TASK_BINDING_MISMATCH", "review_type differs")
        if str(review["verifier_identity_id"]) != task.assignee_identity_id:
            raise AttestationImportError(
                "ASSIGNEE_BINDING_MISMATCH", "verifier identity differs"
            )
        if str(review["reviewer_subject_id"]) != task.assignee_subject_id:
            raise AttestationImportError(
                "ASSIGNEE_BINDING_MISMATCH", "reviewer subject differs"
            )
        independence = review["independence"]
        assert isinstance(independence, dict)
        authors = independence["author_subject_ids"]
        assert isinstance(authors, list)
        if tuple(str(item) for item in authors) != task.author_subject_ids:
            raise AttestationImportError("AUTHOR_BINDING_MISMATCH", "author set differs")
        if task.assignee_subject_id in task.author_subject_ids:
            raise AttestationImportError(
                "REVIEWER_IS_TASK_AUTHOR", task.assignee_subject_id
            )
        binding = review["binding"]
        assert isinstance(binding, dict)
        if binding != task.binding.review_binding():
            raise AttestationImportError("TASK_BINDING_MISMATCH", "binding differs")
        if task.review_type is ReviewType.COMPOSITION:
            if review.get("closure_witness_id") != task.binding.closure_witness_id:
                raise AttestationImportError(
                    "TASK_BINDING_MISMATCH", "closure_witness_id differs"
                )
        elif task.review_type is ReviewType.PAPER:
            expected = {
                "candidate_tex_artifact_id": task.binding.candidate_tex_artifact_id,
                "terminal_root_digest": task.binding.terminal_root_digest,
                "dependency_closure_digest": task.binding.dependency_closure_digest,
            }
            if any(review.get(name) != value for name, value in expected.items()):
                raise AttestationImportError(
                    "TASK_BINDING_MISMATCH", "paper artifact or digest differs"
                )


def _freeze_json(value: Any) -> Any:
    if isinstance(value, dict):
        return MappingProxyType({key: _freeze_json(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


def canonical_review_payload(review: Mapping[str, Any]) -> bytes:
    """Canonical bytes covered by signature; the signature object is excluded."""

    return canonical_json_bytes({key: value for key, value in review.items() if key != "signature"})


class _DuplicateKey(ValueError):
    pass


def _decode_object(raw: bytes) -> dict[str, Any]:
    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        value: dict[str, Any] = {}
        for key, item in pairs:
            if key in value:
                raise _DuplicateKey(key)
            value[key] = item
        return value

    try:
        decoded = raw.decode("utf-8")
        value = json.loads(decoded, object_pairs_hook=reject_duplicates)
    except UnicodeDecodeError as error:
        raise AttestationImportError(
            "REVIEW_ARTIFACT_ENCODING_INVALID", "artifact is not UTF-8"
        ) from error
    except json.JSONDecodeError as error:
        raise AttestationImportError(
            "REVIEW_ARTIFACT_JSON_INVALID", str(error)
        ) from error
    except _DuplicateKey as error:
        raise AttestationImportError(
            "REVIEW_ARTIFACT_DUPLICATE_KEY", str(error)
        ) from error
    if not isinstance(value, dict):
        raise AttestationImportError(
            "REVIEW_ARTIFACT_JSON_INVALID", "top-level JSON must be an object"
        )
    return value


__all__ = [
    "ArtifactContentReader",
    "ArtifactReadContentReader",
    "AttestationImportError",
    "AuthorityEffect",
    "HmacAttestationKey",
    "HmacKeyringVerifier",
    "ImportedReview",
    "ReviewAttestationImporter",
    "SignatureAuthority",
    "SignatureVerifier",
    "TrustClass",
    "canonical_review_payload",
]
