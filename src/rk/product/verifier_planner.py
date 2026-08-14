"""Contract-aware verifier planning for normalized research draft Claims."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rk.product.claims import (
    ClaimStore,
    ClaimSubmission,
)
from rk.product.research_draft import DraftCandidate, ResearchDraftError, ResearchDraftStore
from rk.product.validation_gateway import (
    ValidationBackend,
    ValidationEvidence,
    ValidationGateway,
    ValidationResult,
    ValidationVerdict,
)
from rk.wire import canonical_json_bytes


class VerifierPlannerError(RuntimeError):
    """A candidate, verifier plan, or evidence transition is invalid."""


class VerifierPlannerConflict(VerifierPlannerError):
    """A stable plan or validation identity was rebound."""


@dataclass(frozen=True, slots=True)
class ResearchVerifierPlan:
    plan_id: str
    candidate_id: str
    claim_id: str
    selected_subgraph_digest: str
    required_backends: tuple[ValidationBackend, ...]
    supplementary_backends: tuple[ValidationBackend, ...]
    plan_digest: str
    status: str
    created_at: str
    updated_at: str


class ResearchVerifierPlanner:
    """Submits B10 candidates and requires every routed verifier before kernel import."""

    def __init__(
        self,
        *,
        db_path: Path,
        drafts: ResearchDraftStore,
        claims: ClaimStore,
        gateway: ValidationGateway,
        busy_timeout_ms: int = 5_000,
    ) -> None:
        self._db_path = Path(db_path)
        self._drafts = drafts
        self._claims = claims
        self._gateway = gateway
        self._busy_timeout_ms = busy_timeout_ms

    def submit_and_plan(
        self,
        *,
        plan_id: str,
        candidate_id: str,
        subject_id: str,
        worker_run_id: str,
        attempt_id: str,
        allowed_backends: Sequence[str],
        now: str,
        supersedes_claim_id: str | None = None,
    ) -> ResearchVerifierPlan:
        candidate = self._drafts.get_candidate(candidate_id)
        draft = self._drafts.get(candidate.draft_id)
        if candidate.undefined_symbols:
            raise VerifierPlannerError(
                "candidate has undefined symbols: " + ", ".join(candidate.undefined_symbols)
            )
        predecessor_ids = self._resolved_predecessors(candidate)
        claim = self._claims.submit(
            ClaimSubmission(
                run_id=draft.run_id,
                contract_version=draft.contract_version,
                kernel_revision=draft.kernel_revision,
                statement=candidate.statement,
                claim_kind=candidate.claim_kind,
                proof_or_evidence_artifacts=(draft.source_artifact,),
                predecessor_fact_ids=predecessor_ids,
                source_binding_artifact=draft.source_artifact,
                work_item_id=f"draft:{draft.draft_id}:{candidate.stable_label}",
                worker_run_id=worker_run_id,
                attempt_id=attempt_id,
                supersedes_claim_id=supersedes_claim_id,
                stable_label=candidate.stable_label,
                public_summary=None,
            ),
            subject_id=subject_id,
        )
        subgraph = self._claims.necessary_subgraph(claim.claim_id)
        routed = self._gateway.plan(
            claim,
            selected_subgraph_digest=subgraph.digest,
            allowed_backends=allowed_backends,
        )
        payload = {
            "candidate_id": candidate_id,
            "claim_id": claim.claim_id,
            "selected_subgraph_digest": subgraph.digest,
            "required_backends": [item.value for item in routed.required_backends],
            "supplementary_backends": [item.value for item in routed.supplementary_backends],
        }
        digest = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
        values = (
            candidate_id,
            claim.claim_id,
            subgraph.digest,
            _json(payload["required_backends"]),
            _json(payload["supplementary_backends"]),
            digest,
        )
        obligation_id = "draft-obligation-" + hashlib.sha256(candidate_id.encode()).hexdigest()[:32]
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT candidate_id,claim_id,selected_subgraph_digest,required_backends_json,"
                "supplementary_backends_json,plan_digest FROM "
                "product_research_verifier_plans WHERE plan_id=?",
                (plan_id,),
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO product_research_verifier_plans("
                    "plan_id,candidate_id,claim_id,selected_subgraph_digest,"
                    "required_backends_json,supplementary_backends_json,plan_digest,status,"
                    "created_at,updated_at) VALUES(?,?,?,?,?,?,?,'PLANNED',?,?)",
                    (plan_id, *values, now, now),
                )
            elif tuple(row) != values:
                raise VerifierPlannerConflict("plan ID is bound differently")
            changed = connection.execute(
                "UPDATE product_research_claim_candidates SET lifecycle='SUBMITTED',"
                "submitted_claim_id=? WHERE candidate_id=? AND lifecycle='CANDIDATE'",
                (claim.claim_id, candidate_id),
            )
            if changed.rowcount == 0:
                current = connection.execute(
                    "SELECT submitted_claim_id FROM product_research_claim_candidates "
                    "WHERE candidate_id=?",
                    (candidate_id,),
                ).fetchone()
                if current != (claim.claim_id,):
                    raise VerifierPlannerConflict("candidate was submitted as another Claim")
            connection.execute(
                "INSERT OR IGNORE INTO product_research_obligations("
                "obligation_id,draft_id,candidate_id,claim_id,status,updated_at) "
                "VALUES(?,?,?,?,'WAITING_KERNEL',?)",
                (obligation_id, candidate.draft_id, candidate_id, claim.claim_id, now),
            )
            self._refresh_readiness(connection, candidate.draft_id, now=now, event_id=None)
            connection.commit()
        return self.get_plan(plan_id)

    def record_evidence(
        self,
        *,
        plan_id: str,
        evidence: ValidationEvidence,
        now: str,
    ) -> ValidationResult:
        plan = self.get_plan(plan_id)
        if plan.status in {
            "READY_FOR_KERNEL",
            "REJECTED",
            "IMPORTED_ACCEPTED",
            "IMPORTED_REJECTED",
        }:
            raise VerifierPlannerError("verifier plan is already terminal for evidence collection")
        permitted = set(plan.required_backends) | set(plan.supplementary_backends)
        if evidence.backend not in permitted:
            raise VerifierPlannerError("verifier backend was not routed by the frozen plan")
        claim = self._claims.get(plan.claim_id)
        result = self._gateway.evaluate(
            claim,
            evidence,
            expected_subgraph_digest=plan.selected_subgraph_digest,
        )
        encoded = _json(dict(result.mutation_value()))
        digest = hashlib.sha256(encoded.encode()).hexdigest()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT plan_id,backend,result_json,result_digest FROM "
                "product_research_verifier_results WHERE validation_id=?",
                (result.validation_id,),
            ).fetchone()
            values = (plan_id, result.backend.value, encoded, digest)
            if row is None:
                try:
                    connection.execute(
                        "INSERT INTO product_research_verifier_results("
                        "validation_id,plan_id,backend,verdict,result_json,result_digest,"
                        "created_at) "
                        "VALUES(?,?,?,?,?,?,?)",
                        (
                            result.validation_id,
                            plan_id,
                            result.backend.value,
                            result.verdict.value,
                            encoded,
                            digest,
                            now,
                        ),
                    )
                except sqlite3.IntegrityError as error:
                    raise VerifierPlannerConflict(
                        "one frozen verifier backend can report only once"
                    ) from error
            elif tuple(row) != values:
                raise VerifierPlannerConflict("validation ID is bound differently")
            status = self._status_after_result(connection, plan, result)
            connection.execute(
                "UPDATE product_research_verifier_plans SET status=?,updated_at=? WHERE plan_id=?",
                (status, now, plan_id),
            )
            connection.commit()
        return result

    def get_plan(self, plan_id: str) -> ResearchVerifierPlan:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT plan_id,candidate_id,claim_id,selected_subgraph_digest,"
                "required_backends_json,supplementary_backends_json,plan_digest,status,"
                "created_at,updated_at FROM product_research_verifier_plans WHERE plan_id=?",
                (plan_id,),
            ).fetchone()
        if row is None:
            raise KeyError(plan_id)
        return ResearchVerifierPlan(
            str(row[0]),
            str(row[1]),
            str(row[2]),
            str(row[3]),
            tuple(ValidationBackend(item) for item in _strings(row[4])),
            tuple(ValidationBackend(item) for item in _strings(row[5])),
            str(row[6]),
            str(row[7]),
            str(row[8]),
            str(row[9]),
        )

    def get_result(self, validation_id: str) -> ValidationResult:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT result_json FROM product_research_verifier_results WHERE validation_id=?",
                (validation_id,),
            ).fetchone()
        if row is None:
            raise KeyError(validation_id)
        value = _object(row[0])
        return ValidationResult(
            validation_id=str(value["validation_id"]),
            claim_id=str(value["claim_id"]),
            statement_digest=str(value["statement_digest"]),
            contract_version=int(value["contract_version"]),
            selected_subgraph_digest=str(value["selected_subgraph_digest"]),
            backend=ValidationBackend(str(value["backend"])),
            verdict=ValidationVerdict(str(value["verdict"])),
            verifier_reference_id=str(value["verifier_reference_id"]),
            promotion_eligible=bool(value["promotion_eligible"]),
            authority_effect=str(value["authority_effect"]),
            repair_feedback=(
                str(value["repair_feedback"]) if value.get("repair_feedback") is not None else None
            ),
        )

    def _resolved_predecessors(self, candidate: DraftCandidate) -> tuple[str, ...]:
        resolved = list(candidate.predecessor_fact_ids)
        with self._connect() as connection:
            for label in candidate.predecessor_labels:
                row = connection.execute(
                    "SELECT c.submitted_claim_id,p.lifecycle,p.authority_class "
                    "FROM product_research_claim_candidates c JOIN product_claims p "
                    "ON p.claim_id=c.submitted_claim_id WHERE c.draft_id=? "
                    "AND c.stable_label=?",
                    (candidate.draft_id, label),
                ).fetchone()
                if row is None or tuple(row[1:]) != ("ACCEPTED", "VERIFIED"):
                    raise VerifierPlannerError(f"predecessor Claim {label} is not kernel-accepted")
                resolved.append(str(row[0]))
        if len(set(resolved)) != len(resolved):
            raise VerifierPlannerError("resolved predecessor facts are duplicated")
        return tuple(resolved)

    @staticmethod
    def _status_after_result(
        connection: sqlite3.Connection,
        plan: ResearchVerifierPlan,
        result: ValidationResult,
    ) -> str:
        if result.verdict is ValidationVerdict.REJECTED:
            return "REJECTED" if result.backend in plan.required_backends else "PARTIALLY_VERIFIED"
        rows = connection.execute(
            "SELECT backend,verdict FROM product_research_verifier_results WHERE plan_id=?",
            (plan.plan_id,),
        ).fetchall()
        accepted = {ValidationBackend(str(row[0])) for row in rows if row[1] == "ACCEPTED"}
        return (
            "READY_FOR_KERNEL"
            if set(plan.required_backends).issubset(accepted)
            else "PARTIALLY_VERIFIED"
        )

    def _refresh_readiness(
        self,
        connection: sqlite3.Connection,
        draft_id: str,
        *,
        now: str,
        event_id: str | None,
    ) -> None:
        total = connection.execute(
            "SELECT candidate_count FROM product_research_drafts WHERE draft_id=?",
            (draft_id,),
        ).fetchone()
        if total is None:
            raise KeyError(draft_id)
        obligations = connection.execute(
            "SELECT obligation_id,status FROM product_research_obligations WHERE draft_id=? "
            "ORDER BY obligation_id",
            (draft_id,),
        ).fetchall()
        discharged = sum(row[1] == "DISCHARGED_BY_KERNEL" for row in obligations)
        blockers = [str(row[0]) for row in obligations if row[1] != "DISCHARGED_BY_KERNEL"]
        unsubmitted = int(total[0]) - len(obligations)
        blockers.extend(f"unsubmitted-candidate-{index + 1}" for index in range(unsubmitted))
        connection.execute(
            "INSERT INTO product_research_closure_readiness("
            "draft_id,status,discharged_count,total_count,blocking_obligation_ids_json,"
            "last_kernel_event_id,updated_at) VALUES(?,'BLOCKED',?,?,?,?,?) "
            "ON CONFLICT(draft_id) DO UPDATE SET discharged_count=excluded.discharged_count,"
            "total_count=excluded.total_count,blocking_obligation_ids_json="
            "excluded.blocking_obligation_ids_json,last_kernel_event_id="
            "excluded.last_kernel_event_id,updated_at=excluded.updated_at",
            (draft_id, discharged, int(total[0]), _json(blockers), event_id, now),
        )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._db_path, isolation_level=None)
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms}")
        return connection


def _strings(value: object) -> tuple[str, ...]:
    decoded = json.loads(str(value))
    if not isinstance(decoded, list) or any(not isinstance(item, str) for item in decoded):
        raise ResearchDraftError("stored verifier backend array is invalid")
    return tuple(decoded)


def _object(value: object) -> dict[str, Any]:
    decoded = json.loads(str(value))
    if not isinstance(decoded, dict):
        raise ResearchDraftError("stored verifier result is invalid")
    return decoded


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


__all__ = [
    "ResearchVerifierPlan",
    "ResearchVerifierPlanner",
    "VerifierPlannerConflict",
    "VerifierPlannerError",
]
