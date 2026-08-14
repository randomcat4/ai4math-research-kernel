"""Review assignments that bind signed artifacts without copying verdict truth."""

from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from typing import Any
from uuid import UUID

from rk.product.identity import IdentityStore, ProductIdentity, ProductRole

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ReviewTaskError(RuntimeError):
    """A review assignment, binding, independence, or state invariant failed."""


class ReviewTaskConflict(ReviewTaskError):
    """An immutable task identity or signed artifact was reused inconsistently."""


class ReviewIndependenceError(ReviewTaskError):
    """The assigned reviewer is not independent of the task authors."""


class ReviewTaskStateError(ReviewTaskError):
    """The task is not in the state required by the requested transition."""


class ReviewType(StrEnum):
    ATOMIC = "ATOMIC"
    COMPOSITION = "COMPOSITION"
    PAPER = "PAPER"


class ReviewTaskStatus(StrEnum):
    OPEN = "OPEN"
    CLAIMED = "CLAIMED"
    SUBMITTED = "SUBMITTED"
    EXPIRED = "EXPIRED"
    REASSIGNED = "REASSIGNED"
    INVALIDATED = "INVALIDATED"


class IndependenceStatus(StrEnum):
    PENDING = "PENDING"
    VERIFIED = "VERIFIED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class ReviewArtifactRef:
    artifact_id: str
    sha256: str
    byte_count: int
    media_type: str

    def __post_init__(self) -> None:
        _uuid(self.artifact_id, "artifact_id")
        _hash(self.sha256, "artifact sha256")
        if self.byte_count < 0:
            raise ValueError("artifact byte_count must be non-negative")
        if not self.media_type:
            raise ValueError("artifact media_type must be non-empty")


@dataclass(frozen=True, slots=True)
class ReviewBinding:
    run_id: str
    kernel_revision: int
    contract_version: int
    target_id: str
    target_digest: str
    selected_subgraph_digest: str | None = None
    closure_witness_id: str | None = None
    candidate_tex_artifact_id: str | None = None
    terminal_root_digest: str | None = None
    dependency_closure_digest: str | None = None

    def validate_for(self, review_type: ReviewType) -> None:
        _uuid(self.run_id, "run_id")
        _uuid(self.target_id, "target_id")
        _hash(self.target_digest, "target_digest")
        if self.kernel_revision < 0 or self.contract_version < 1:
            raise ValueError("invalid kernel revision or contract version")
        if self.selected_subgraph_digest is not None:
            _hash(self.selected_subgraph_digest, "selected_subgraph_digest")
        paper_fields = (
            self.candidate_tex_artifact_id,
            self.terminal_root_digest,
            self.dependency_closure_digest,
        )
        if review_type is ReviewType.COMPOSITION:
            if self.selected_subgraph_digest is None or self.closure_witness_id is None:
                raise ValueError("composition requires subgraph digest and closure witness")
            _uuid(self.closure_witness_id, "closure_witness_id")
            if any(value is not None for value in paper_fields):
                raise ValueError("composition cannot contain paper binding fields")
        elif review_type is ReviewType.PAPER:
            if any(value is None for value in paper_fields):
                raise ValueError("paper requires TeX, root, and dependency closure")
            assert self.candidate_tex_artifact_id is not None
            assert self.terminal_root_digest is not None
            assert self.dependency_closure_digest is not None
            _uuid(self.candidate_tex_artifact_id, "candidate_tex_artifact_id")
            _hash(self.terminal_root_digest, "terminal_root_digest")
            _hash(self.dependency_closure_digest, "dependency_closure_digest")
            if self.closure_witness_id is not None:
                raise ValueError("paper cannot contain closure_witness_id")
        elif self.closure_witness_id is not None or any(
            value is not None for value in paper_fields
        ):
            raise ValueError("atomic cannot contain composition or paper binding fields")

    def review_binding(self) -> dict[str, object]:
        value: dict[str, object] = {
            "run_id": self.run_id,
            "kernel_revision": self.kernel_revision,
            "contract_version": self.contract_version,
            "target_id": self.target_id,
            "target_digest": self.target_digest,
        }
        if self.selected_subgraph_digest is not None:
            value["selected_subgraph_digest"] = self.selected_subgraph_digest
        return value

    def to_dict(self) -> dict[str, object]:
        value = self.review_binding()
        for name in (
            "closure_witness_id",
            "candidate_tex_artifact_id",
            "terminal_root_digest",
            "dependency_closure_digest",
        ):
            field = getattr(self, name)
            if field is not None:
                value[name] = field
        return value

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ReviewBinding:
        return cls(
            run_id=str(value["run_id"]),
            kernel_revision=int(value["kernel_revision"]),
            contract_version=int(value["contract_version"]),
            target_id=str(value["target_id"]),
            target_digest=str(value["target_digest"]),
            selected_subgraph_digest=_optional(value, "selected_subgraph_digest"),
            closure_witness_id=_optional(value, "closure_witness_id"),
            candidate_tex_artifact_id=_optional(value, "candidate_tex_artifact_id"),
            terminal_root_digest=_optional(value, "terminal_root_digest"),
            dependency_closure_digest=_optional(value, "dependency_closure_digest"),
        )


@dataclass(frozen=True, slots=True)
class ReviewTask:
    review_task_id: str
    review_type: ReviewType
    binding: ReviewBinding
    author_subject_ids: tuple[str, ...]
    assignee_identity_id: str
    assignee_subject_id: str
    independence_required: bool
    independence_status: IndependenceStatus
    status: ReviewTaskStatus
    signed_artifact_ref: ReviewArtifactRef | None
    created_at: str
    expires_at: str
    claimed_at: str | None
    submitted_at: str | None


class ReviewTaskStore:
    """Persist task routing only; the signed artifact remains the sole review content."""

    def __init__(
        self,
        db_path: Path,
        identity_store: IdentityStore,
        *,
        busy_timeout_ms: int = 5_000,
    ) -> None:
        self._db_path = Path(db_path)
        self._identities = identity_store
        self._busy_timeout_ms = busy_timeout_ms

    def create(
        self,
        *,
        review_task_id: str,
        review_type: ReviewType,
        binding: ReviewBinding,
        author_subject_ids: tuple[str, ...],
        assignee_identity_id: str,
        created_at: str,
        expires_at: str,
    ) -> ReviewTask:
        _uuid(review_task_id, "review_task_id")
        binding.validate_for(review_type)
        authors = _authors(author_subject_ids)
        assignee = self._reviewer(assignee_identity_id)
        if assignee.subject_id in authors:
            raise ReviewIndependenceError("REVIEWER_IS_TASK_AUTHOR")
        if _instant(expires_at) <= _instant(created_at):
            raise ValueError("expires_at must follow created_at")
        try:
            with self._connect() as connection:
                connection.execute(
                    "INSERT INTO product_review_tasks("
                    "review_task_id,review_type,binding_json,author_subject_ids_json,"
                    "assignee_identity_id,created_at,expires_at) VALUES(?,?,?,?,?,?,?)",
                    (
                        review_task_id,
                        review_type.value,
                        _canonical(binding.to_dict()),
                        _canonical(list(authors)),
                        assignee_identity_id,
                        created_at,
                        expires_at,
                    ),
                )
        except sqlite3.IntegrityError as error:
            raise ReviewTaskConflict("review task identity already exists") from error
        return self.get(review_task_id)

    def get(self, review_task_id: str) -> ReviewTask:
        with self._connect() as connection:
            row = connection.execute(
                _TASK_SELECT + " WHERE task.review_task_id=?", (review_task_id,)
            ).fetchone()
        if row is None:
            raise KeyError(review_task_id)
        return _task(row)

    def claim(self, review_task_id: str, *, identity_id: str, now: str) -> ReviewTask:
        reviewer = self._reviewer(identity_id)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                _TASK_SELECT + " WHERE task.review_task_id=?", (review_task_id,)
            ).fetchone()
            if row is None:
                raise KeyError(review_task_id)
            task = _task(row)
            if task.assignee_identity_id != reviewer.identity_id:
                raise ReviewTaskStateError("TASK_ASSIGNED_TO_ANOTHER_IDENTITY")
            if reviewer.subject_id in task.author_subject_ids:
                raise ReviewIndependenceError("REVIEWER_IS_TASK_AUTHOR")
            if _instant(now) >= _instant(task.expires_at):
                connection.execute(
                    "UPDATE product_review_tasks SET status='EXPIRED' "
                    "WHERE review_task_id=? AND status IN ('OPEN','REASSIGNED')",
                    (review_task_id,),
                )
                connection.commit()
                raise ReviewTaskStateError("REVIEW_TASK_EXPIRED")
            if task.status is ReviewTaskStatus.CLAIMED:
                connection.commit()
                return task
            if task.status not in (ReviewTaskStatus.OPEN, ReviewTaskStatus.REASSIGNED):
                raise ReviewTaskStateError("REVIEW_TASK_NOT_CLAIMABLE")
            changed = connection.execute(
                "UPDATE product_review_tasks SET status='CLAIMED',claimed_at=? "
                "WHERE review_task_id=? AND status IN ('OPEN','REASSIGNED') "
                "AND EXISTS(SELECT 1 FROM product_identities "
                "WHERE identity_id=assignee_identity_id AND enabled=1 AND role='REVIEWER')",
                (now, review_task_id),
            ).rowcount
            if changed != 1:
                raise ReviewTaskConflict("review task changed while claimed")
            connection.commit()
        return self.get(review_task_id)

    def reassign(
        self,
        review_task_id: str,
        *,
        assignee_identity_id: str,
        reassigned_at: str,
        expires_at: str,
    ) -> ReviewTask:
        assignee = self._reviewer(assignee_identity_id)
        if _instant(expires_at) <= _instant(reassigned_at):
            raise ValueError("expires_at must follow reassigned_at")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                _TASK_SELECT + " WHERE task.review_task_id=?", (review_task_id,)
            ).fetchone()
            if row is None:
                raise KeyError(review_task_id)
            task = _task(row)
            if task.status in (
                ReviewTaskStatus.SUBMITTED,
                ReviewTaskStatus.INVALIDATED,
            ):
                raise ReviewTaskStateError("REVIEW_TASK_NOT_REASSIGNABLE")
            if assignee.subject_id in task.author_subject_ids:
                raise ReviewIndependenceError("REVIEWER_IS_TASK_AUTHOR")
            changed = connection.execute(
                "UPDATE product_review_tasks SET assignee_identity_id=?,"
                "status='REASSIGNED',independence_status='PENDING',"
                "expires_at=?,claimed_at=NULL "
                "WHERE review_task_id=? AND status NOT IN ('SUBMITTED','INVALIDATED') "
                "AND EXISTS(SELECT 1 FROM product_identities "
                "WHERE identity_id=? AND enabled=1 AND role='REVIEWER')",
                (
                    assignee_identity_id,
                    expires_at,
                    review_task_id,
                    assignee_identity_id,
                ),
            ).rowcount
            if changed != 1:
                raise ReviewTaskConflict("review task changed while reassigned")
            connection.commit()
        return self.get(review_task_id)

    def _record_verified_artifact(
        self,
        review_task_id: str,
        *,
        artifact_ref: ReviewArtifactRef,
        submitted_at: str,
    ) -> ReviewTask:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                _TASK_SELECT + " WHERE task.review_task_id=?", (review_task_id,)
            ).fetchone()
            if row is None:
                raise KeyError(review_task_id)
            task = _task(row)
            if task.status is ReviewTaskStatus.SUBMITTED:
                if task.signed_artifact_ref != artifact_ref:
                    raise ReviewTaskConflict("task is bound to another signed artifact")
                connection.commit()
                return task
            if task.status is not ReviewTaskStatus.CLAIMED:
                raise ReviewTaskStateError("REVIEW_TASK_NOT_CLAIMED")
            if _instant(submitted_at) >= _instant(task.expires_at):
                raise ReviewTaskStateError("REVIEW_TASK_EXPIRED")
            changed = connection.execute(
                "UPDATE product_review_tasks SET status='SUBMITTED',"
                "independence_status='VERIFIED',signed_artifact_id=?,"
                "signed_artifact_sha256=?,signed_artifact_byte_count=?,"
                "signed_artifact_media_type=?,submitted_at=? "
                "WHERE review_task_id=? AND status='CLAIMED' "
                "AND EXISTS(SELECT 1 FROM product_identities "
                "WHERE identity_id=assignee_identity_id AND enabled=1 AND role='REVIEWER')",
                (
                    artifact_ref.artifact_id,
                    artifact_ref.sha256,
                    artifact_ref.byte_count,
                    artifact_ref.media_type,
                    submitted_at,
                    review_task_id,
                ),
            ).rowcount
            if changed != 1:
                raise ReviewTaskConflict("review task changed while artifact was recorded")
            connection.commit()
        return self.get(review_task_id)

    def _reviewer(self, identity_id: str) -> ProductIdentity:
        identity = self._identities.get(identity_id)
        if not identity.enabled or identity.role is not ProductRole.REVIEWER:
            raise ReviewTaskStateError("ASSIGNEE_IS_NOT_AN_ENABLED_REVIEWER")
        return identity

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._db_path, timeout=self._busy_timeout_ms / 1_000)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
        return connection


_TASK_SELECT = (
    "SELECT task.review_task_id,task.review_type,task.binding_json,"
    "task.author_subject_ids_json,task.assignee_identity_id,identity.subject_id,"
    "task.independence_required,task.independence_status,task.status,"
    "task.signed_artifact_id,task.signed_artifact_sha256,"
    "task.signed_artifact_byte_count,task.signed_artifact_media_type,"
    "task.created_at,task.expires_at,task.claimed_at,task.submitted_at "
    "FROM product_review_tasks AS task "
    "JOIN product_identities AS identity ON identity.identity_id=task.assignee_identity_id"
)


def _task(row: tuple[object, ...]) -> ReviewTask:
    raw_binding = json.loads(str(row[2]))
    raw_authors = json.loads(str(row[3]))
    if not isinstance(raw_binding, dict) or not isinstance(raw_authors, list):
        raise ReviewTaskError("persisted review task JSON has the wrong shape")
    artifact = (
        ReviewArtifactRef(str(row[9]), str(row[10]), int(str(row[11])), str(row[12]))
        if row[9] is not None
        else None
    )
    return ReviewTask(
        review_task_id=str(row[0]),
        review_type=ReviewType(str(row[1])),
        binding=ReviewBinding.from_dict(raw_binding),
        author_subject_ids=tuple(str(item) for item in raw_authors),
        assignee_identity_id=str(row[4]),
        assignee_subject_id=str(row[5]),
        independence_required=bool(row[6]),
        independence_status=IndependenceStatus(str(row[7])),
        status=ReviewTaskStatus(str(row[8])),
        signed_artifact_ref=artifact,
        created_at=str(row[13]),
        expires_at=str(row[14]),
        claimed_at=str(row[15]) if row[15] is not None else None,
        submitted_at=str(row[16]) if row[16] is not None else None,
    )


def _authors(values: tuple[str, ...]) -> tuple[str, ...]:
    if not values or any(not value for value in values) or len(set(values)) != len(values):
        raise ValueError("review task authors must be non-empty and unique")
    return tuple(sorted(values))


def _uuid(value: str, name: str) -> None:
    try:
        parsed = UUID(value)
    except ValueError as error:
        raise ValueError(f"{name} must be a UUID") from error
    if str(parsed) != value:
        raise ValueError(f"{name} must use canonical lowercase UUID form")


def _hash(value: str, name: str) -> None:
    if _SHA256.fullmatch(value) is None:
        raise ValueError(f"{name} must be a lowercase SHA-256 digest")


def _instant(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        raise ValueError("review task timestamps must include a timezone")
    return parsed


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _optional(value: dict[str, Any], name: str) -> str | None:
    raw = value.get(name)
    return str(raw) if raw is not None else None


__all__ = [
    "IndependenceStatus",
    "ReviewArtifactRef",
    "ReviewBinding",
    "ReviewIndependenceError",
    "ReviewTask",
    "ReviewTaskConflict",
    "ReviewTaskError",
    "ReviewTaskStateError",
    "ReviewTaskStatus",
    "ReviewTaskStore",
    "ReviewType",
]
