"""Kernel-event adapter for research-draft obligations and ClosureWitness readiness."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from rk.product.claims import ClaimStore, KernelVerdictReceipt
from rk.product.validation_gateway import ValidationVerdict
from rk.product.verifier_planner import ResearchVerifierPlanner


class ObligationAdapterError(RuntimeError):
    """A verifier result or kernel receipt cannot update draft obligations."""


class ObligationAdapterConflict(ObligationAdapterError):
    """A kernel event or imported plan was rebound."""


@dataclass(frozen=True, slots=True)
class ClosureReadiness:
    draft_id: str
    status: str
    discharged_count: int
    total_count: int
    blocking_obligation_ids: tuple[str, ...]
    last_kernel_event_id: str | None
    updated_at: str


class ResearchObligationAdapter:
    """Updates obligations only while importing a genuine B10 kernel verdict."""

    def __init__(
        self,
        *,
        db_path: Path,
        claims: ClaimStore,
        planner: ResearchVerifierPlanner,
        busy_timeout_ms: int = 5_000,
    ) -> None:
        self._db_path = Path(db_path)
        self._claims = claims
        self._planner = planner
        self._busy_timeout_ms = busy_timeout_ms

    def consume_kernel_verdict(
        self,
        *,
        plan_id: str,
        validation_id: str,
        receipt: KernelVerdictReceipt,
        now: str,
    ) -> ClosureReadiness:
        plan = self._planner.get_plan(plan_id)
        result = self._planner.get_result(validation_id)
        if (
            receipt.validation_id != validation_id
            or receipt.claim_id != plan.claim_id
            or result.claim_id != plan.claim_id
            or receipt.statement_digest != result.statement_digest
            or receipt.contract_version != result.contract_version
            or receipt.repair_feedback != result.repair_feedback
        ):
            raise ObligationAdapterError("kernel receipt is not bound to the planned validation")
        accepted_result = result.verdict is ValidationVerdict.ACCEPTED and result.promotion_eligible
        if receipt.accepted != accepted_result or receipt.promotion_eligible != accepted_result:
            raise ObligationAdapterError("kernel verdict disagrees with verifier eligibility")
        expected_plan_status = "READY_FOR_KERNEL" if receipt.accepted else "REJECTED"
        if plan.status in {"IMPORTED_ACCEPTED", "IMPORTED_REJECTED"}:
            with self._connect() as connection:
                event = connection.execute(
                    "SELECT o.kernel_event_id,d.draft_id FROM product_research_obligations o "
                    "JOIN product_research_claim_candidates c ON c.candidate_id=o.candidate_id "
                    "JOIN product_research_drafts d ON d.draft_id=c.draft_id "
                    "WHERE o.candidate_id=?",
                    (plan.candidate_id,),
                ).fetchone()
            if event is None or event[0] != receipt.event_id:
                raise ObligationAdapterConflict("plan was imported by another kernel event")
            return self.readiness(str(event[1]))
        if plan.status != expected_plan_status:
            raise ObligationAdapterError("verifier plan is not ready for this kernel verdict")
        if receipt.authority_source != "RESEARCH_KERNEL":
            raise ObligationAdapterError("only ResearchKernel can update draft obligations")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            candidate = connection.execute(
                "SELECT draft_id,lifecycle FROM product_research_claim_candidates "
                "WHERE candidate_id=? AND submitted_claim_id=?",
                (plan.candidate_id, plan.claim_id),
            ).fetchone()
            if candidate is None or candidate[1] != "SUBMITTED":
                raise ObligationAdapterConflict("candidate is not awaiting its kernel verdict")
            self._claims.record_validation_in_transaction(
                connection,
                result=result.mutation_value(),
                kernel_receipt_id=receipt.receipt_id,
                kernel_event_id=receipt.event_id,
            )
            decided = self._claims.record_kernel_verdict_in_transaction(
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
            if receipt.accepted and decided.lifecycle.value != "ACCEPTED":
                raise ObligationAdapterError("B10 did not accept the kernel verdict")
            if not receipt.accepted and decided.lifecycle.value != "REJECTED":
                raise ObligationAdapterError("B10 did not record the kernel rejection")
            lifecycle = "ACCEPTED" if receipt.accepted else "REJECTED"
            obligation_status = "DISCHARGED_BY_KERNEL" if receipt.accepted else "REPAIR_REQUIRED"
            plan_status = "IMPORTED_ACCEPTED" if receipt.accepted else "IMPORTED_REJECTED"
            connection.execute(
                "UPDATE product_research_claim_candidates SET lifecycle=? "
                "WHERE candidate_id=? AND lifecycle='SUBMITTED'",
                (lifecycle, plan.candidate_id),
            )
            changed = connection.execute(
                "UPDATE product_research_obligations SET status=?,kernel_event_id=?,updated_at=? "
                "WHERE candidate_id=? AND status='WAITING_KERNEL' AND kernel_event_id IS NULL",
                (obligation_status, receipt.event_id, now, plan.candidate_id),
            )
            if changed.rowcount != 1:
                raise ObligationAdapterConflict("draft obligation changed before kernel import")
            connection.execute(
                "UPDATE product_research_verifier_plans SET status=?,updated_at=? "
                "WHERE plan_id=? AND status=?",
                (plan_status, now, plan_id, expected_plan_status),
            )
            self._refresh_readiness(
                connection,
                str(candidate[0]),
                now=now,
                kernel_event_id=receipt.event_id,
            )
            connection.commit()
        return self.readiness(str(candidate[0]))

    def readiness(self, draft_id: str) -> ClosureReadiness:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT draft_id,status,discharged_count,total_count,"
                "blocking_obligation_ids_json,last_kernel_event_id,updated_at "
                "FROM product_research_closure_readiness WHERE draft_id=?",
                (draft_id,),
            ).fetchone()
        if row is None:
            raise KeyError(draft_id)
        return ClosureReadiness(
            str(row[0]),
            str(row[1]),
            int(row[2]),
            int(row[3]),
            _strings(row[4]),
            str(row[5]) if row[5] is not None else None,
            str(row[6]),
        )

    @staticmethod
    def _refresh_readiness(
        connection: sqlite3.Connection,
        draft_id: str,
        *,
        now: str,
        kernel_event_id: str,
    ) -> None:
        total_row = connection.execute(
            "SELECT candidate_count FROM product_research_drafts WHERE draft_id=?",
            (draft_id,),
        ).fetchone()
        if total_row is None:
            raise KeyError(draft_id)
        total = int(total_row[0])
        rows = connection.execute(
            "SELECT obligation_id,status FROM product_research_obligations WHERE draft_id=? "
            "ORDER BY obligation_id",
            (draft_id,),
        ).fetchall()
        discharged = sum(row[1] == "DISCHARGED_BY_KERNEL" for row in rows)
        blockers = [str(row[0]) for row in rows if row[1] != "DISCHARGED_BY_KERNEL"]
        blockers.extend(f"unsubmitted-candidate-{index + 1}" for index in range(total - len(rows)))
        status = "READY_FOR_CLOSURE_WITNESS" if discharged == total else "BLOCKED"
        connection.execute(
            "INSERT INTO product_research_closure_readiness("
            "draft_id,status,discharged_count,total_count,blocking_obligation_ids_json,"
            "last_kernel_event_id,updated_at) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(draft_id) DO UPDATE SET status=excluded.status,"
            "discharged_count=excluded.discharged_count,total_count=excluded.total_count,"
            "blocking_obligation_ids_json=excluded.blocking_obligation_ids_json,"
            "last_kernel_event_id=excluded.last_kernel_event_id,updated_at=excluded.updated_at",
            (
                draft_id,
                status,
                discharged,
                total,
                _json(blockers),
                kernel_event_id,
                now,
            ),
        )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._db_path, isolation_level=None)
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms}")
        return connection


def _strings(value: object) -> tuple[str, ...]:
    decoded = json.loads(str(value))
    if not isinstance(decoded, list) or any(not isinstance(item, str) for item in decoded):
        raise ObligationAdapterError("stored blocking obligation IDs are invalid")
    return tuple(decoded)


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


__all__ = [
    "ClosureReadiness",
    "ObligationAdapterConflict",
    "ObligationAdapterError",
    "ResearchObligationAdapter",
]
