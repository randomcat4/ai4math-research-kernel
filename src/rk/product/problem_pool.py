"""Frozen arXiv problem pools with complete denominators and human semantic audit."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Literal, cast

from rk.product.artifact_read import ExactArtifactRef


class ProblemPoolError(RuntimeError):
    pass


class ProblemPoolConflict(ProblemPoolError):
    pass


@dataclass(frozen=True, slots=True)
class ProblemPool:
    problem_pool_id: str
    deployment_id: str
    date_from: str
    date_to: str
    subjects: tuple[str, ...]
    version_rule: str
    withdrawal_rule: str
    exclusion_rules: tuple[str, ...]
    state: str
    frozen_by: str | None
    frozen_at: str | None


@dataclass(frozen=True, slots=True)
class SourceEntry:
    source_record_id: str
    arxiv_id: str | None
    version: int | None
    title: str | None
    summary: str | None
    published_at: str | None
    updated_at: str | None
    subjects: tuple[str, ...]
    withdrawn: bool
    denominator_status: str
    reason_code: str
    marker_kind: str | None = None
    extracted_statement: str | None = None


@dataclass(frozen=True, slots=True)
class ProblemCandidate:
    problem_candidate_id: str
    problem_pool_id: str
    source_record_id: str
    arxiv_id: str
    version: int
    marker_kind: str
    extracted_statement: str
    audit_status: str
    normalized_statement: str | None
    definitions: tuple[str, ...]
    quantifiers: tuple[str, ...]
    hypotheses: tuple[str, ...]
    recommendation_status: str
    recommendation_score: int | None
    machine_certificate_status: str
    heterogeneous_review_status: str
    expert_confirmation_status: str
    author_confirmation_status: str
    created_run_id: str | None


@dataclass(frozen=True, slots=True)
class PoolDenominator:
    included: int
    excluded: int
    failed: int
    blocked: int
    reasons: tuple[tuple[str, int], ...]

    @property
    def total(self) -> int:
        return self.included + self.excluded + self.failed + self.blocked


ArtifactBindingKind = Literal["SEMANTIC_AUDIT", "CONTRACT_TEMPLATE"]


@dataclass(frozen=True, slots=True)
class ProblemPoolArtifactBinding:
    problem_pool_id: str
    binding_kind: ArtifactBindingKind
    artifact: ExactArtifactRef
    bound_by: str
    binding_digest: str
    authority_effect: str
    created_at: str


class ProblemPoolStore:
    def __init__(self, db_path: Path, *, busy_timeout_ms: int = 5_000) -> None:
        self._db_path = Path(db_path)
        self._busy_timeout_ms = busy_timeout_ms

    def create(
        self,
        *,
        problem_pool_id: str,
        deployment_id: str,
        date_from: str,
        date_to: str,
        subjects: tuple[str, ...],
        version_rule: str,
        withdrawal_rule: str,
        exclusion_rules: tuple[str, ...],
        now: str,
    ) -> ProblemPool:
        start, end = date.fromisoformat(date_from), date.fromisoformat(date_to)
        if start > end or not problem_pool_id or not deployment_id:
            raise ValueError("problem pool identity or frozen date window is invalid")
        if not subjects or len(set(subjects)) != len(subjects):
            raise ValueError("problem pool subjects must be non-empty and unique")
        if version_rule not in {"LATEST_VISIBLE", "ALL_VERSIONS"}:
            raise ValueError("problem pool version rule is invalid")
        if withdrawal_rule not in {"EXCLUDE_WITHDRAWN", "INCLUDE_FLAGGED"}:
            raise ValueError("problem pool withdrawal rule is invalid")
        if len(set(exclusion_rules)) != len(exclusion_rules):
            raise ValueError("problem pool exclusion rules must be unique")
        immutable_values = (
            deployment_id,
            date_from,
            date_to,
            _json(list(subjects)),
            version_rule,
            withdrawal_rule,
            _json(list(exclusion_rules)),
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT deployment_id,date_from,date_to,subjects_json,version_rule,"
                "withdrawal_rule,exclusion_rules_json FROM "
                "product_problem_pools WHERE problem_pool_id=?",
                (problem_pool_id,),
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO product_problem_pools("
                    "problem_pool_id,deployment_id,date_from,date_to,subjects_json,"
                    "version_rule,withdrawal_rule,exclusion_rules_json,state,created_at,"
                    "updated_at) VALUES(?,?,?,?,?,?,?,?,'COLLECTING',?,?)",
                    (problem_pool_id, *immutable_values, now, now),
                )
            elif tuple(row) != immutable_values:
                raise ProblemPoolConflict("problem pool ID is bound differently")
            connection.commit()
        return self.get(problem_pool_id)

    def record_snapshot(
        self,
        *,
        problem_pool_id: str,
        snapshot_id: str,
        ordinal: int,
        ingest_status: str,
        failure_code: str | None,
        entries: tuple[SourceEntry, ...],
        now: str,
    ) -> tuple[ProblemCandidate, ...]:
        pool = self.get(problem_pool_id)
        if pool.state != "COLLECTING":
            raise ProblemPoolError("source snapshots can only enter a collecting pool")
        if ingest_status not in {"INGESTED", "FAILED", "BLOCKED"}:
            raise ValueError("snapshot ingest status is invalid")
        if (ingest_status == "INGESTED") != (failure_code is None):
            raise ValueError("snapshot failure code does not match ingest status")
        if ingest_status == "INGESTED" and not entries:
            raise ProblemPoolError("an ingested snapshot must contribute denominator rows")
        if ingest_status != "INGESTED" and entries:
            raise ProblemPoolError("failed snapshots use their explicit failure denominator row")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT ordinal,ingest_status,failure_code FROM "
                "product_problem_pool_snapshots WHERE problem_pool_id=? AND snapshot_id=?",
                (problem_pool_id, snapshot_id),
            ).fetchone()
            snapshot_values = (ordinal, ingest_status, failure_code)
            if existing is None:
                connection.execute(
                    "INSERT INTO product_problem_pool_snapshots("
                    "problem_pool_id,snapshot_id,ordinal,ingest_status,failure_code) "
                    "VALUES(?,?,?,?,?)",
                    (problem_pool_id, snapshot_id, *snapshot_values),
                )
            elif tuple(existing) != snapshot_values:
                raise ProblemPoolConflict("snapshot is already bound differently")
            if ingest_status != "INGESTED":
                self._insert_source(
                    connection,
                    problem_pool_id,
                    snapshot_id,
                    SourceEntry(
                        f"source-failure-{snapshot_id}",
                        None,
                        None,
                        None,
                        None,
                        None,
                        None,
                        (),
                        False,
                        ingest_status,
                        str(failure_code),
                    ),
                    now,
                )
            for entry in entries:
                self._validate_entry(entry)
                self._insert_source(connection, problem_pool_id, snapshot_id, entry, now)
                if entry.denominator_status == "INCLUDED":
                    self._insert_candidate(connection, problem_pool_id, entry, now)
            connection.commit()
        return self.candidates(problem_pool_id)

    def audit_candidate(
        self,
        candidate_id: str,
        *,
        decision: str,
        normalized_statement: str | None,
        definitions: tuple[str, ...],
        quantifiers: tuple[str, ...],
        hypotheses: tuple[str, ...],
        audit_note: str,
        audited_by: str,
        actor_kind: str,
        now: str,
    ) -> ProblemCandidate:
        if actor_kind != "USER":
            raise ProblemPoolError("semantic freeze requires a human audit")
        if decision not in {"INCLUDE", "EXCLUDE"}:
            raise ValueError("candidate audit decision is invalid")
        if not audited_by or not audit_note:
            raise ValueError("human audit identity and note are required")
        if decision == "INCLUDE" and (
            not normalized_statement
            or not definitions
            or not quantifiers
            or not hypotheses
            or any(
                len(set(items)) != len(items) for items in (definitions, quantifiers, hypotheses)
            )
        ):
            raise ProblemPoolError(
                "included statement requires restored definitions, quantifiers, and hypotheses"
            )
        if decision == "EXCLUDE" and (
            normalized_statement is not None or definitions or quantifiers or hypotheses
        ):
            raise ProblemPoolError("excluded audit cannot retain a normalized problem statement")
        candidate = self.get_candidate(candidate_id)
        pool = self.get(candidate.problem_pool_id)
        if pool.state == "FROZEN" or candidate.audit_status != "HUMAN_AUDIT_PENDING":
            raise ProblemPoolError("candidate is not awaiting human audit")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                "UPDATE product_problem_candidates SET audit_status=?,normalized_statement=?,"
                "definitions_json=?,quantifiers_json=?,hypotheses_json=?,audited_by=?,"
                "audited_at=?,audit_note=?,updated_at=? WHERE problem_candidate_id=? "
                "AND audit_status='HUMAN_AUDIT_PENDING'",
                (
                    "HUMAN_INCLUDED" if decision == "INCLUDE" else "HUMAN_EXCLUDED",
                    normalized_statement,
                    _json(list(definitions)),
                    _json(list(quantifiers)),
                    _json(list(hypotheses)),
                    audited_by,
                    now,
                    audit_note,
                    now,
                    candidate_id,
                ),
            )
            if changed.rowcount != 1:
                raise ProblemPoolConflict("candidate audit state changed concurrently")
            connection.execute(
                "UPDATE product_problem_pools SET state='HUMAN_AUDIT',updated_at=? "
                "WHERE problem_pool_id=? AND state='COLLECTING'",
                (now, candidate.problem_pool_id),
            )
            connection.commit()
        return self.get_candidate(candidate_id)

    def freeze(
        self,
        problem_pool_id: str,
        *,
        frozen_by: str,
        actor_kind: str,
        semantic_audit_artifact: ExactArtifactRef,
        now: str,
    ) -> ProblemPool:
        if actor_kind != "USER" or not frozen_by:
            raise ProblemPoolError("only a human can freeze the audited problem pool")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self._bind_artifact(
                connection,
                problem_pool_id=problem_pool_id,
                binding_kind="SEMANTIC_AUDIT",
                artifact=semantic_audit_artifact,
                bound_by=frozen_by,
                now=now,
            )
            pending = connection.execute(
                "SELECT COUNT(*) FROM product_problem_candidates WHERE problem_pool_id=? "
                "AND audit_status='HUMAN_AUDIT_PENDING'",
                (problem_pool_id,),
            ).fetchone()
            included = connection.execute(
                "SELECT COUNT(*) FROM product_problem_candidates WHERE problem_pool_id=? "
                "AND audit_status='HUMAN_INCLUDED'",
                (problem_pool_id,),
            ).fetchone()
            snapshots = connection.execute(
                "SELECT COUNT(*) FROM product_problem_pool_snapshots WHERE problem_pool_id=?",
                (problem_pool_id,),
            ).fetchone()
            if pending != (0,) or included == (0,) or snapshots == (0,):
                raise ProblemPoolError(
                    "pool needs complete snapshots and human decisions before freeze"
                )
            changed = connection.execute(
                "UPDATE product_problem_pools SET state='FROZEN',frozen_by=?,frozen_at=?,"
                "updated_at=? WHERE problem_pool_id=? AND state IN ('COLLECTING','HUMAN_AUDIT')",
                (frozen_by, now, now, problem_pool_id),
            )
            if changed.rowcount != 1:
                raise ProblemPoolConflict("problem pool state changed before freeze")
            connection.commit()
        return self.get(problem_pool_id)

    def score_candidate(
        self,
        candidate_id: str,
        *,
        importance: int,
        verifiability: int,
        bridge_potential: int,
        estimated_cost: int,
        recommend_threshold: int,
        now: str,
    ) -> ProblemCandidate:
        candidate = self.get_candidate(candidate_id)
        if self.get(candidate.problem_pool_id).state != "FROZEN":
            raise ProblemPoolError("recommendations require a frozen pool")
        if candidate.audit_status != "HUMAN_INCLUDED":
            raise ProblemPoolError("only human-included candidates can be scored")
        scores = (importance, verifiability, bridge_potential, estimated_cost)
        if any(isinstance(item, bool) or not 0 <= item <= 100 for item in scores):
            raise ValueError("candidate scores must be integers from 0 to 100")
        recommendation = (
            35 * importance + 30 * verifiability + 25 * bridge_potential - 20 * estimated_cost
        ) // 100
        status = "RECOMMENDED" if recommendation >= recommend_threshold else "NOT_RECOMMENDED"
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT importance_score,verifiability_score,bridge_potential_score,"
                "estimated_cost_score,recommendation_score,recommendation_status FROM "
                "product_problem_candidates WHERE problem_candidate_id=?",
                (candidate_id,),
            ).fetchone()
            expected = (*scores, recommendation, status)
            if existing is not None and existing[5] != "UNSCORED":
                if tuple(existing) != expected:
                    raise ProblemPoolConflict("candidate score is bound differently")
                connection.commit()
                return self.get_candidate(candidate_id)
            changed = connection.execute(
                "UPDATE product_problem_candidates SET importance_score=?,"
                "verifiability_score=?,bridge_potential_score=?,estimated_cost_score=?,"
                "recommendation_score=?,recommendation_status=?,updated_at=? "
                "WHERE problem_candidate_id=? AND recommendation_status='UNSCORED'",
                (*scores, recommendation, status, now, candidate_id),
            )
            if changed.rowcount != 1:
                raise ProblemPoolConflict("candidate scoring state changed concurrently")
            connection.commit()
        return self.get_candidate(candidate_id)

    def denominator(self, problem_pool_id: str) -> PoolDenominator:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT denominator_status,reason_code,COUNT(*) FROM "
                "product_problem_source_records WHERE problem_pool_id=? "
                "GROUP BY denominator_status,reason_code ORDER BY denominator_status,reason_code",
                (problem_pool_id,),
            ).fetchall()
        counts = {name: 0 for name in ("INCLUDED", "EXCLUDED", "FAILED", "BLOCKED")}
        reasons = []
        for status, reason, count in rows:
            counts[str(status)] += int(count)
            reasons.append((str(reason), int(count)))
        return PoolDenominator(
            counts["INCLUDED"],
            counts["EXCLUDED"],
            counts["FAILED"],
            counts["BLOCKED"],
            tuple(reasons),
        )

    def bind_artifact(
        self,
        problem_pool_id: str,
        *,
        binding_kind: ArtifactBindingKind,
        artifact: ExactArtifactRef,
        bound_by: str,
        now: str,
    ) -> ProblemPoolArtifactBinding:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            binding = self._bind_artifact(
                connection,
                problem_pool_id=problem_pool_id,
                binding_kind=binding_kind,
                artifact=artifact,
                bound_by=bound_by,
                now=now,
            )
            connection.commit()
        return binding

    def artifact_bindings(
        self, problem_pool_id: str
    ) -> tuple[ProblemPoolArtifactBinding, ...]:
        self.get(problem_pool_id)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT problem_pool_id,binding_kind,artifact_id,artifact_sha256,"
                "artifact_byte_count,artifact_media_type,artifact_at_revision,bound_by,"
                "binding_digest,authority_effect,created_at FROM "
                "product_problem_pool_artifact_bindings WHERE problem_pool_id=? "
                "ORDER BY binding_kind",
                (problem_pool_id,),
            ).fetchall()
        return tuple(_artifact_binding(row) for row in rows)

    @staticmethod
    def _bind_artifact(
        connection: sqlite3.Connection,
        *,
        problem_pool_id: str,
        binding_kind: ArtifactBindingKind,
        artifact: ExactArtifactRef,
        bound_by: str,
        now: str,
    ) -> ProblemPoolArtifactBinding:
        if (
            binding_kind not in {"SEMANTIC_AUDIT", "CONTRACT_TEMPLATE"}
            or not artifact.artifact_id
            or len(artifact.sha256) != 64
            or any(character not in "0123456789abcdef" for character in artifact.sha256)
            or isinstance(artifact.byte_count, bool)
            or artifact.byte_count < 0
            or not artifact.media_type
            or isinstance(artifact.at_revision, bool)
            or artifact.at_revision < 0
            or not bound_by
        ):
            raise ValueError("problem pool ArtifactRef is invalid")
        identity = {
            "problem_pool_id": problem_pool_id,
            "binding_kind": binding_kind,
            "artifact": {
                "artifact_id": artifact.artifact_id,
                "sha256": artifact.sha256,
                "byte_count": artifact.byte_count,
                "media_type": artifact.media_type,
                "at_revision": artifact.at_revision,
            },
            "bound_by": bound_by,
            "authority_effect": "NO_FACT",
        }
        digest = hashlib.sha256(_json(identity).encode()).hexdigest()
        values = (
            artifact.artifact_id,
            artifact.sha256,
            artifact.byte_count,
            artifact.media_type,
            artifact.at_revision,
            bound_by,
            digest,
            "NO_FACT",
        )
        row = connection.execute(
            "SELECT artifact_id,artifact_sha256,artifact_byte_count,artifact_media_type,"
            "artifact_at_revision,bound_by,binding_digest,authority_effect,created_at FROM "
            "product_problem_pool_artifact_bindings WHERE problem_pool_id=? AND binding_kind=?",
            (problem_pool_id, binding_kind),
        ).fetchone()
        if row is None:
            connection.execute(
                "INSERT INTO product_problem_pool_artifact_bindings("
                "problem_pool_id,binding_kind,artifact_id,artifact_sha256,"
                "artifact_byte_count,artifact_media_type,artifact_at_revision,bound_by,"
                "binding_digest,authority_effect,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (problem_pool_id, binding_kind, *values, now),
            )
        elif tuple(row[:8]) != values:
            raise ProblemPoolConflict("problem pool artifact binding differs")
        created_at = now if row is None else str(row[8])
        return ProblemPoolArtifactBinding(
            problem_pool_id,
            binding_kind,
            artifact,
            bound_by,
            digest,
            "NO_FACT",
            created_at,
        )

    def get(self, problem_pool_id: str) -> ProblemPool:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT problem_pool_id,deployment_id,date_from,date_to,subjects_json,"
                "version_rule,withdrawal_rule,exclusion_rules_json,state,frozen_by,frozen_at "
                "FROM product_problem_pools WHERE problem_pool_id=?",
                (problem_pool_id,),
            ).fetchone()
        if row is None:
            raise KeyError(problem_pool_id)
        return ProblemPool(
            str(row[0]),
            str(row[1]),
            str(row[2]),
            str(row[3]),
            _strings(row[4]),
            str(row[5]),
            str(row[6]),
            _strings(row[7]),
            str(row[8]),
            str(row[9]) if row[9] is not None else None,
            str(row[10]) if row[10] is not None else None,
        )

    def candidates(self, problem_pool_id: str) -> tuple[ProblemCandidate, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                _CANDIDATE_SELECT + " WHERE problem_pool_id=? ORDER BY problem_candidate_id",
                (problem_pool_id,),
            ).fetchall()
        return tuple(_candidate(row) for row in rows)

    def get_candidate(self, candidate_id: str) -> ProblemCandidate:
        with self._connect() as connection:
            row = connection.execute(
                _CANDIDATE_SELECT + " WHERE problem_candidate_id=?",
                (candidate_id,),
            ).fetchone()
        if row is None:
            raise KeyError(candidate_id)
        return _candidate(row)

    @staticmethod
    def _validate_entry(entry: SourceEntry) -> None:
        if entry.denominator_status not in {"INCLUDED", "EXCLUDED", "FAILED", "BLOCKED"}:
            raise ValueError("source denominator status is invalid")
        is_no_hit = (
            entry.reason_code == "NO_HIT"
            and entry.denominator_status == "EXCLUDED"
            and entry.arxiv_id is None
            and entry.version is None
        )
        if not is_no_hit and (entry.arxiv_id is None or entry.version is None or entry.version < 1):
            raise ProblemPoolError("ingested source rows require exact arXiv ID/version")
        if entry.denominator_status == "INCLUDED" and (
            entry.marker_kind not in {"CONJECTURE", "PROBLEM", "QUESTION"}
            or not entry.extracted_statement
        ):
            raise ProblemPoolError("included row lacks an explicit open-problem marker")
        if entry.denominator_status != "INCLUDED" and (
            entry.marker_kind is not None or entry.extracted_statement is not None
        ):
            raise ProblemPoolError("excluded source row cannot become a candidate")

    @staticmethod
    def _insert_source(
        connection: sqlite3.Connection,
        pool_id: str,
        snapshot_id: str,
        entry: SourceEntry,
        now: str,
    ) -> None:
        values = (
            pool_id,
            snapshot_id,
            entry.arxiv_id,
            entry.version,
            entry.title,
            entry.summary,
            entry.published_at,
            entry.updated_at,
            _json(list(entry.subjects)),
            int(entry.withdrawn),
            entry.denominator_status,
            entry.reason_code,
            now,
        )
        row = connection.execute(
            "SELECT problem_pool_id,snapshot_id,arxiv_id,version,title,summary,published_at,"
            "updated_at,subjects_json,withdrawn,denominator_status,reason_code "
            "FROM product_problem_source_records WHERE source_record_id=?",
            (entry.source_record_id,),
        ).fetchone()
        if row is None:
            connection.execute(
                "INSERT INTO product_problem_source_records("
                "source_record_id,problem_pool_id,snapshot_id,arxiv_id,version,title,summary,"
                "published_at,updated_at,subjects_json,withdrawn,denominator_status,reason_code,"
                "created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (entry.source_record_id, *values),
            )
        elif tuple(row) != values[:-1]:
            replayed_duplicate = (
                tuple(row[:10]) == values[:10]
                and tuple(row[10:]) == ("EXCLUDED", "DUPLICATE_STATEMENT")
                and values[10:12] == ("INCLUDED", "EXPLICIT_OPEN_MARKER")
            )
            if not replayed_duplicate:
                raise ProblemPoolConflict("source record ID is bound differently")

    @staticmethod
    def _insert_candidate(
        connection: sqlite3.Connection,
        pool_id: str,
        entry: SourceEntry,
        now: str,
    ) -> None:
        assert entry.arxiv_id is not None
        assert entry.version is not None
        assert entry.marker_kind is not None
        assert entry.extracted_statement is not None
        existing = connection.execute(
            "SELECT arxiv_id,version,marker_kind,extracted_statement FROM "
            "product_problem_candidates WHERE source_record_id=?",
            (entry.source_record_id,),
        ).fetchone()
        candidate_binding = (
            entry.arxiv_id,
            entry.version,
            entry.marker_kind,
            entry.extracted_statement,
        )
        if existing is not None:
            if tuple(existing) != candidate_binding:
                raise ProblemPoolConflict("candidate source is bound differently")
            return
        normalized = " ".join(entry.extracted_statement.casefold().split())
        digest = hashlib.sha256(normalized.encode()).hexdigest()
        duplicate = connection.execute(
            "SELECT problem_candidate_id FROM product_problem_candidates "
            "WHERE problem_pool_id=? AND dedupe_digest=?",
            (pool_id, digest),
        ).fetchone()
        if duplicate is not None:
            connection.execute(
                "UPDATE product_problem_source_records SET denominator_status='EXCLUDED',"
                "reason_code='DUPLICATE_STATEMENT' WHERE source_record_id=?",
                (entry.source_record_id,),
            )
            return
        candidate_id = (
            "problem-"
            + hashlib.sha256(
                f"{pool_id}:{entry.arxiv_id}:v{entry.version}:{digest}".encode()
            ).hexdigest()[:32]
        )
        connection.execute(
            "INSERT INTO product_problem_candidates("
            "problem_candidate_id,problem_pool_id,source_record_id,arxiv_id,version,"
            "marker_kind,extracted_statement,dedupe_digest,audit_status,normalized_statement,"
            "definitions_json,quantifiers_json,hypotheses_json,recommendation_status,"
            "machine_certificate_status,heterogeneous_review_status,"
            "expert_confirmation_status,author_confirmation_status,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,'HUMAN_AUDIT_PENDING',NULL,'[]','[]','[]','UNSCORED',"
            "'NOT_ATTEMPTED','NOT_REQUESTED','EXTERNAL_CONFIRMATION_PENDING',"
            "'EXTERNAL_CONFIRMATION_PENDING',?,?)",
            (
                candidate_id,
                pool_id,
                entry.source_record_id,
                entry.arxiv_id,
                entry.version,
                entry.marker_kind,
                entry.extracted_statement,
                digest,
                now,
                now,
            ),
        )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._db_path, isolation_level=None)
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms}")
        return connection


_CANDIDATE_SELECT = (
    "SELECT problem_candidate_id,problem_pool_id,source_record_id,arxiv_id,version,"
    "marker_kind,extracted_statement,audit_status,normalized_statement,definitions_json,"
    "quantifiers_json,hypotheses_json,recommendation_status,recommendation_score,"
    "machine_certificate_status,heterogeneous_review_status,expert_confirmation_status,"
    "author_confirmation_status,created_run_id FROM product_problem_candidates"
)


def _candidate(row: tuple[object, ...]) -> ProblemCandidate:
    return ProblemCandidate(
        str(row[0]),
        str(row[1]),
        str(row[2]),
        str(row[3]),
        int(str(row[4])),
        str(row[5]),
        str(row[6]),
        str(row[7]),
        str(row[8]) if row[8] is not None else None,
        _strings(row[9]),
        _strings(row[10]),
        _strings(row[11]),
        str(row[12]),
        int(str(row[13])) if row[13] is not None else None,
        str(row[14]),
        str(row[15]),
        str(row[16]),
        str(row[17]),
        str(row[18]) if row[18] is not None else None,
    )


def _artifact_binding(row: tuple[object, ...]) -> ProblemPoolArtifactBinding:
    return ProblemPoolArtifactBinding(
        str(row[0]),
        cast(ArtifactBindingKind, str(row[1])),
        ExactArtifactRef(
            str(row[2]),
            str(row[3]),
            int(str(row[4])),
            str(row[5]),
            int(str(row[6])),
        ),
        str(row[7]),
        str(row[8]),
        str(row[9]),
        str(row[10]),
    )


def _strings(value: object) -> tuple[str, ...]:
    decoded = json.loads(str(value))
    if not isinstance(decoded, list) or any(not isinstance(item, str) for item in decoded):
        raise ProblemPoolError("stored string array is invalid")
    return tuple(decoded)


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


__all__ = [
    "ArtifactBindingKind",
    "PoolDenominator",
    "ProblemCandidate",
    "ProblemPool",
    "ProblemPoolArtifactBinding",
    "ProblemPoolConflict",
    "ProblemPoolError",
    "ProblemPoolStore",
    "SourceEntry",
]
