"""Atomic Claim workflow projection, repair lineage, and verified-fact reuse search."""

from __future__ import annotations

import hashlib
import json
import sqlite3
import unicodedata
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any


class ClaimError(RuntimeError):
    """A Claim identity, transition, binding, or dependency invariant failed."""


class ClaimConflict(ClaimError):
    """An immutable Claim or attempt identity was reused with different content."""


class ClaimScopeError(ClaimError):
    """A Claim crossed run, contract, revision, or predecessor authority scope."""


class ClaimLifecycle(StrEnum):
    PENDING_VERIFICATION = "PENDING_VERIFICATION"
    ACCEPTED = "ACCEPTED"
    REJECTED = "REJECTED"
    INVALIDATED = "INVALIDATED"
    REVOKED = "REVOKED"


class ClaimKind(StrEnum):
    ROOT = "ROOT"
    LEMMA = "LEMMA"
    DEFINITION = "DEFINITION"
    COUNTEREXAMPLE = "COUNTEREXAMPLE"
    COMPUTATION = "COMPUTATION"


class ClaimAuthorityClass(StrEnum):
    RESEARCH_HISTORY = "RESEARCH_HISTORY"
    VERIFIED = "VERIFIED"


@dataclass(frozen=True, slots=True)
class ClaimArtifactBinding:
    artifact_id: str
    sha256: str
    byte_count: int
    media_type: str

    def __post_init__(self) -> None:
        if not self.artifact_id or not _hash(self.sha256):
            raise ValueError("artifact identity and sha256 are required")
        if self.byte_count < 0 or not self.media_type:
            raise ValueError("artifact byte count and media type are invalid")

    def to_dict(self) -> dict[str, object]:
        return {
            "artifact_id": self.artifact_id,
            "sha256": self.sha256,
            "byte_count": self.byte_count,
            "media_type": self.media_type,
        }


@dataclass(frozen=True, slots=True)
class ClaimSubmission:
    run_id: str
    contract_version: int
    kernel_revision: int
    statement: str
    claim_kind: ClaimKind
    proof_or_evidence_artifacts: tuple[ClaimArtifactBinding, ...]
    predecessor_fact_ids: tuple[str, ...]
    source_binding_artifact: ClaimArtifactBinding
    work_item_id: str
    worker_run_id: str
    attempt_id: str
    route_id: str | None = None
    supersedes_claim_id: str | None = None
    stable_label: str | None = None
    public_summary: str | None = None

    def __post_init__(self) -> None:
        if (
            not self.run_id
            or self.contract_version < 1
            or self.kernel_revision < 0
            or not _statement(self.statement)
            or not self.work_item_id
            or not self.worker_run_id
            or not self.attempt_id
        ):
            raise ValueError("Claim submission scope or identity is invalid")
        if len(set(self.predecessor_fact_ids)) != len(self.predecessor_fact_ids):
            raise ValueError("predecessor facts must be unique")
        evidence_ids = [item.artifact_id for item in self.proof_or_evidence_artifacts]
        if len(set(evidence_ids)) != len(evidence_ids):
            raise ValueError("proof or evidence artifacts must be unique")
        if self.supersedes_claim_id is not None and not self.supersedes_claim_id:
            raise ValueError("supersedes_claim_id must be non-empty")

    @property
    def normalized_statement(self) -> str:
        return _statement(self.statement)

    @property
    def statement_digest(self) -> str:
        return hashlib.sha256(self.normalized_statement.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class ClaimRecord:
    claim_id: str
    run_id: str
    contract_version: int
    kernel_revision_created: int
    statement: str
    statement_digest: str
    claim_kind: ClaimKind
    lifecycle: ClaimLifecycle
    authority_class: ClaimAuthorityClass
    promotion_eligible: bool
    predecessor_fact_ids: tuple[str, ...]
    evidence_artifacts: tuple[ClaimArtifactBinding, ...]
    source_binding_artifact: ClaimArtifactBinding
    work_item_id: str
    worker_run_id: str
    attempt_id: str
    submitted_by_subject_id: str
    route_id: str | None
    supersedes_claim_id: str | None
    superseded_by_claim_id: str | None
    stable_label: str
    repair_feedback: str | None
    validation_id: str | None
    kernel_receipt_id: str | None
    kernel_event_id: str | None
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class NecessarySubgraph:
    target_claim_id: str
    facts: tuple[ClaimRecord, ...]
    digest: str


@dataclass(frozen=True, slots=True)
class ReusableClaimHit:
    claim_id: str
    statement: str
    statement_digest: str
    claim_kind: ClaimKind
    reused_by_subject_id: str


@dataclass(frozen=True, slots=True)
class KernelVerdictReceipt:
    authority_source: str
    command_type: str
    receipt_id: str
    event_id: str
    kernel_revision: int
    claim_id: str
    statement_digest: str
    contract_version: int
    validation_id: str
    accepted: bool
    promotion_eligible: bool
    repair_feedback: str | None = None


class ClaimStore:
    """Workflow history only; ResearchKernel remains the sole mathematical fact authority."""

    def __init__(
        self,
        db_path: Path,
        id_generator: Callable[[], str],
        clock: Callable[[], str],
        *,
        busy_timeout_ms: int = 5_000,
    ) -> None:
        self._db_path = Path(db_path)
        self._ids = id_generator
        self._clock = clock
        self._busy_timeout_ms = busy_timeout_ms

    def submit(self, submission: ClaimSubmission, *, subject_id: str) -> ClaimRecord:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            record = self.submit_in_transaction(connection, submission, subject_id=subject_id)
            connection.commit()
        return record

    def submit_in_transaction(
        self,
        connection: sqlite3.Connection,
        submission: ClaimSubmission,
        *,
        subject_id: str,
    ) -> ClaimRecord:
        if not subject_id:
            raise ValueError("subject_id must be non-empty")
        existing = connection.execute(
            "SELECT claim_id,statement_digest FROM product_claims "
            "WHERE run_id=? AND worker_run_id=? AND attempt_id=?",
            (submission.run_id, submission.worker_run_id, submission.attempt_id),
        ).fetchone()
        if existing is not None:
            if str(existing[1]) != submission.statement_digest:
                raise ClaimConflict("one attempt cannot submit more than one Claim")
            return self._get(connection, str(existing[0]))
        self._validate_predecessors(connection, submission)
        if submission.supersedes_claim_id is not None:
            old = self._get(connection, submission.supersedes_claim_id)
            if (
                old.run_id != submission.run_id
                or old.contract_version != submission.contract_version
                or old.lifecycle is not ClaimLifecycle.REJECTED
                or old.superseded_by_claim_id is not None
                or old.statement_digest == submission.statement_digest
            ):
                raise ClaimScopeError("repair must supersede one rejected Claim with new content")
        claim_id = self._ids()
        stable_label = submission.stable_label or claim_id
        conflict = connection.execute(
            "SELECT statement_digest FROM product_claims WHERE run_id=? AND stable_label=?",
            (submission.run_id, stable_label),
        ).fetchone()
        if conflict is not None and str(conflict[0]) != submission.statement_digest:
            raise ClaimConflict("STABLE_LABEL_CONFLICT")
        now = self._clock()
        connection.execute(
            "INSERT INTO product_claims("
            "claim_id,run_id,contract_version,kernel_revision_created,statement,"
            "statement_digest,claim_kind,lifecycle,authority_class,promotion_eligible,"
            "source_binding_json,work_item_id,worker_run_id,attempt_id,submitted_by_subject_id,"
            "route_id,supersedes_claim_id,stable_label,public_summary,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,'PENDING_VERIFICATION','RESEARCH_HISTORY',0,?,?,?,?,?,?,?,?,?,?,?)",
            (
                claim_id,
                submission.run_id,
                submission.contract_version,
                submission.kernel_revision,
                submission.normalized_statement,
                submission.statement_digest,
                submission.claim_kind.value,
                _json(submission.source_binding_artifact.to_dict()),
                submission.work_item_id,
                submission.worker_run_id,
                submission.attempt_id,
                subject_id,
                submission.route_id,
                submission.supersedes_claim_id,
                stable_label,
                submission.public_summary,
                now,
                now,
            ),
        )
        for ordinal, fact_id in enumerate(submission.predecessor_fact_ids):
            connection.execute(
                "INSERT INTO product_claim_predecessors(claim_id,fact_id,ordinal) VALUES(?,?,?)",
                (claim_id, fact_id, ordinal),
            )
        for ordinal, artifact in enumerate(submission.proof_or_evidence_artifacts):
            connection.execute(
                "INSERT INTO product_claim_evidence(claim_id,ordinal,binding_json) VALUES(?,?,?)",
                (claim_id, ordinal, _json(artifact.to_dict())),
            )
        if submission.supersedes_claim_id is not None:
            changed = connection.execute(
                "UPDATE product_claims SET superseded_by_claim_id=?,updated_at=? "
                "WHERE claim_id=? AND lifecycle='REJECTED' AND superseded_by_claim_id IS NULL",
                (claim_id, now, submission.supersedes_claim_id),
            )
            if changed.rowcount != 1:
                raise ClaimConflict("rejected Claim was concurrently superseded")
        return self._get(connection, claim_id)

    def record_kernel_verdict_in_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        claim_id: str,
        statement_digest: str,
        contract_version: int,
        validation_id: str,
        accepted: bool,
        promotion_eligible: bool,
        repair_feedback: str | None,
        kernel_receipt_id: str,
        kernel_event_id: str,
        kernel_revision: int,
        authority_source: str,
        command_type: str,
    ) -> ClaimRecord:
        claim = self._get(connection, claim_id)
        if (
            authority_source != "RESEARCH_KERNEL"
            or command_type != "IMPORT_VERIFICATION"
            or claim.statement_digest != statement_digest
            or claim.contract_version != contract_version
            or claim.lifecycle is not ClaimLifecycle.PENDING_VERIFICATION
            or kernel_revision < claim.kernel_revision_created
            or not kernel_receipt_id
            or not kernel_event_id
        ):
            raise ClaimScopeError("kernel verdict is not bound to the pending Claim")
        if accepted != promotion_eligible:
            raise ClaimScopeError("only promotion-eligible acceptance can enter VERIFIED")
        if not accepted and not (repair_feedback and repair_feedback.strip()):
            raise ClaimScopeError("rejection requires actionable repair feedback")
        lifecycle = "ACCEPTED" if accepted else "REJECTED"
        authority = "VERIFIED" if accepted else "RESEARCH_HISTORY"
        now = self._clock()
        connection.execute(
            "UPDATE product_claims SET lifecycle=?,authority_class=?,promotion_eligible=?,"
            "repair_feedback=?,validation_id=?,kernel_receipt_id=?,kernel_event_id=?,"
            "kernel_revision_decided=?,updated_at=? WHERE claim_id=?",
            (
                lifecycle,
                authority,
                int(promotion_eligible),
                repair_feedback,
                validation_id,
                kernel_receipt_id,
                kernel_event_id,
                kernel_revision,
                now,
                claim_id,
            ),
        )
        return self._get(connection, claim_id)

    def apply_kernel_verdict(self, receipt: KernelVerdictReceipt) -> ClaimRecord:
        """Consume a kernel receipt/event pair; Worker and ToolRun sources always fail."""

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            record = self.record_kernel_verdict_in_transaction(
                connection,
                claim_id=receipt.claim_id,
                statement_digest=receipt.statement_digest,
                contract_version=receipt.contract_version,
                validation_id=receipt.validation_id,
                accepted=receipt.accepted,
                promotion_eligible=receipt.promotion_eligible,
                repair_feedback=receipt.repair_feedback,
                kernel_receipt_id=receipt.receipt_id,
                kernel_event_id=receipt.event_id,
                kernel_revision=receipt.kernel_revision,
                authority_source=receipt.authority_source,
                command_type=receipt.command_type,
            )
            connection.commit()
        return record

    def record_validation_in_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        result: Mapping[str, Any],
        kernel_receipt_id: str,
        kernel_event_id: str,
    ) -> None:
        required = {
            "validation_id",
            "claim_id",
            "statement_digest",
            "contract_version",
            "selected_subgraph_digest",
            "backend",
            "verdict",
            "verifier_reference_id",
            "promotion_eligible",
            "authority_effect",
            "repair_feedback",
        }
        if set(result) != required:
            raise ClaimError("validation result fields are not exact")
        claim = self._get(connection, str(result["claim_id"]))
        if (
            claim.statement_digest != result["statement_digest"]
            or claim.contract_version != result["contract_version"]
            or claim.lifecycle is not ClaimLifecycle.PENDING_VERIFICATION
        ):
            raise ClaimScopeError("validation result is not bound to pending Claim")
        encoded = _json(dict(result))
        existing = connection.execute(
            "SELECT result_json,kernel_receipt_id,kernel_event_id "
            "FROM product_claim_validations WHERE validation_id=?",
            (result["validation_id"],),
        ).fetchone()
        if existing is not None:
            if tuple(str(item) for item in existing) != (
                encoded,
                kernel_receipt_id,
                kernel_event_id,
            ):
                raise ClaimConflict("validation_id is already bound to another result")
            return
        connection.execute(
            "INSERT INTO product_claim_validations("
            "validation_id,claim_id,backend,verdict,verifier_reference_id,"
            "selected_subgraph_digest,authority_effect,promotion_eligible,result_json,"
            "kernel_receipt_id,kernel_event_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                result["validation_id"],
                claim.claim_id,
                result["backend"],
                result["verdict"],
                result["verifier_reference_id"],
                result["selected_subgraph_digest"],
                result["authority_effect"],
                int(bool(result["promotion_eligible"])),
                encoded,
                kernel_receipt_id,
                kernel_event_id,
                self._clock(),
            ),
        )

    def get(self, claim_id: str) -> ClaimRecord:
        with self._connect() as connection:
            return self._get(connection, claim_id)

    def necessary_subgraph(self, claim_id: str, *, node_limit: int = 200) -> NecessarySubgraph:
        if not 1 <= node_limit <= 200:
            raise ValueError("necessary subgraph node_limit must be between 1 and 200")
        with self._connect() as connection:
            target = self._get(connection, claim_id)
            pending = list(target.predecessor_fact_ids)
            visited: set[str] = set()
            ordered: list[ClaimRecord] = []
            while pending:
                fact_id = pending.pop(0)
                if fact_id in visited:
                    continue
                fact = self._get(connection, fact_id)
                if (
                    fact.lifecycle is not ClaimLifecycle.ACCEPTED
                    or fact.authority_class is not ClaimAuthorityClass.VERIFIED
                    or fact.run_id != target.run_id
                    or fact.contract_version != target.contract_version
                ):
                    raise ClaimScopeError("necessary subgraph contains a non-current fact")
                visited.add(fact_id)
                ordered.append(fact)
                if len(ordered) > node_limit:
                    raise ClaimScopeError("necessary subgraph exceeds the declared node limit")
                pending.extend(
                    predecessor
                    for predecessor in fact.predecessor_fact_ids
                    if predecessor not in visited
                )
        ordered.sort(key=lambda item: item.claim_id)
        digest = hashlib.sha256(
            _json(
                [
                    {
                        "claim_id": item.claim_id,
                        "statement_digest": item.statement_digest,
                        "predecessor_fact_ids": list(item.predecessor_fact_ids),
                    }
                    for item in ordered
                ]
            ).encode("utf-8")
        ).hexdigest()
        return NecessarySubgraph(claim_id, tuple(ordered), digest)

    def search_reusable(
        self,
        *,
        run_id: str,
        query: str,
        worker_subject_id: str,
        limit: int = 20,
    ) -> tuple[ReusableClaimHit, ...]:
        if not run_id or not query.strip() or not worker_subject_id or not 1 <= limit <= 100:
            raise ValueError("reuse search scope, query, subject, or limit is invalid")
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT c.claim_id,c.statement,c.statement_digest,c.claim_kind "
                "FROM product_claim_fts f JOIN product_claims c ON c.rowid=f.rowid "
                "WHERE product_claim_fts MATCH ? AND c.run_id=? AND c.lifecycle='ACCEPTED' "
                "AND c.authority_class='VERIFIED' ORDER BY bm25(product_claim_fts),"
                "c.claim_id LIMIT ?",
                (query, run_id, limit),
            ).fetchall()
            now = self._clock()
            for row in rows:
                connection.execute(
                    "INSERT INTO product_claim_reuse("
                    "claim_id,reused_by_subject_id,query,created_at) "
                    "VALUES(?,?,?,?)",
                    (row[0], worker_subject_id, query, now),
                )
        return tuple(
            ReusableClaimHit(
                claim_id=str(row[0]),
                statement=str(row[1]),
                statement_digest=str(row[2]),
                claim_kind=ClaimKind(str(row[3])),
                reused_by_subject_id=worker_subject_id,
            )
            for row in rows
        )

    def _validate_predecessors(
        self, connection: sqlite3.Connection, submission: ClaimSubmission
    ) -> None:
        for fact_id in submission.predecessor_fact_ids:
            fact = self._get(connection, fact_id)
            if (
                fact.run_id != submission.run_id
                or fact.contract_version != submission.contract_version
                or fact.lifecycle is not ClaimLifecycle.ACCEPTED
                or fact.authority_class is not ClaimAuthorityClass.VERIFIED
                or not fact.promotion_eligible
            ):
                raise ClaimScopeError("predecessor is not a current verified fact")

    def _get(self, connection: sqlite3.Connection, claim_id: str) -> ClaimRecord:
        row = connection.execute(_SELECT + " WHERE c.claim_id=?", (claim_id,)).fetchone()
        if row is None:
            raise KeyError(claim_id)
        predecessors = tuple(
            str(item[0])
            for item in connection.execute(
                "SELECT fact_id FROM product_claim_predecessors WHERE claim_id=? ORDER BY ordinal",
                (claim_id,),
            ).fetchall()
        )
        evidence = tuple(
            _artifact(json.loads(str(item[0])))
            for item in connection.execute(
                "SELECT binding_json FROM product_claim_evidence WHERE claim_id=? ORDER BY ordinal",
                (claim_id,),
            ).fetchall()
        )
        return _record(row, predecessors, evidence)

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._db_path, timeout=self._busy_timeout_ms / 1_000)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
        return connection


_SELECT = (
    "SELECT c.claim_id,c.run_id,c.contract_version,c.kernel_revision_created,c.statement,"
    "c.statement_digest,c.claim_kind,c.lifecycle,c.authority_class,c.promotion_eligible,"
    "c.source_binding_json,c.work_item_id,c.worker_run_id,c.attempt_id,"
    "c.submitted_by_subject_id,c.route_id,c.supersedes_claim_id,c.superseded_by_claim_id,"
    "c.stable_label,c.repair_feedback,c.validation_id,c.kernel_receipt_id,c.kernel_event_id,"
    "c.created_at,c.updated_at FROM product_claims c"
)


def _record(
    row: Sequence[object],
    predecessors: tuple[str, ...],
    evidence: tuple[ClaimArtifactBinding, ...],
) -> ClaimRecord:
    return ClaimRecord(
        claim_id=str(row[0]),
        run_id=str(row[1]),
        contract_version=int(str(row[2])),
        kernel_revision_created=int(str(row[3])),
        statement=str(row[4]),
        statement_digest=str(row[5]),
        claim_kind=ClaimKind(str(row[6])),
        lifecycle=ClaimLifecycle(str(row[7])),
        authority_class=ClaimAuthorityClass(str(row[8])),
        promotion_eligible=bool(row[9]),
        source_binding_artifact=_artifact(json.loads(str(row[10]))),
        work_item_id=str(row[11]),
        worker_run_id=str(row[12]),
        attempt_id=str(row[13]),
        submitted_by_subject_id=str(row[14]),
        route_id=str(row[15]) if row[15] is not None else None,
        supersedes_claim_id=str(row[16]) if row[16] is not None else None,
        superseded_by_claim_id=str(row[17]) if row[17] is not None else None,
        stable_label=str(row[18]),
        repair_feedback=str(row[19]) if row[19] is not None else None,
        validation_id=str(row[20]) if row[20] is not None else None,
        kernel_receipt_id=str(row[21]) if row[21] is not None else None,
        kernel_event_id=str(row[22]) if row[22] is not None else None,
        created_at=str(row[23]),
        updated_at=str(row[24]),
        predecessor_fact_ids=predecessors,
        evidence_artifacts=evidence,
    )


def _artifact(value: Mapping[str, Any]) -> ClaimArtifactBinding:
    if set(value) != {"artifact_id", "sha256", "byte_count", "media_type"}:
        raise ClaimError("stored artifact binding fields are invalid")
    return ClaimArtifactBinding(
        artifact_id=str(value["artifact_id"]),
        sha256=str(value["sha256"]),
        byte_count=int(value["byte_count"]),
        media_type=str(value["media_type"]),
    )


def _statement(value: str) -> str:
    return unicodedata.normalize("NFC", value).strip()


def _hash(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _json(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


__all__ = [
    "ClaimArtifactBinding",
    "ClaimAuthorityClass",
    "ClaimConflict",
    "ClaimError",
    "ClaimKind",
    "ClaimLifecycle",
    "ClaimRecord",
    "ClaimScopeError",
    "ClaimStore",
    "ClaimSubmission",
    "KernelVerdictReceipt",
    "NecessarySubgraph",
    "ReusableClaimHit",
]
