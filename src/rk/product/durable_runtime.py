"""Typed durable execution and atomic ProductReceipt resolution."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

from rk.product.api import ProductDecision
from rk.product.command_service import decision_value
from rk.product.jobs import (
    DurableJob,
    ExecutionOutcome,
    ExecutionReceipt,
    JobLease,
    JobLeaseLost,
    JobState,
    JobStore,
    JobStoreError,
)
from rk.product.supervisor import RuntimeSupervisor


class DurableExecutor(Protocol):
    def __call__(self, job: DurableJob, request: Mapping[str, Any]) -> TypedExecution: ...


@dataclass(frozen=True, slots=True)
class TypedExecution:
    execution: ExecutionReceipt
    decision: ProductDecision | None

    def __post_init__(self) -> None:
        unknown = self.execution.outcome is ExecutionOutcome.OUTCOME_UNKNOWN
        if unknown != (self.decision is None):
            raise ValueError("only OUTCOME_UNKNOWN may omit a typed ProductDecision")


class DurableJobResolver:
    """Commit job, lease, execution evidence and ProductReceipt as one transaction."""

    def __init__(
        self,
        db_path: Path,
        id_generator: Callable[[], str],
        *,
        busy_timeout_ms: int = 5_000,
    ) -> None:
        self._db_path = Path(db_path)
        self._ids = id_generator
        self._busy_timeout_ms = busy_timeout_ms

    def resolve(
        self,
        lease: JobLease,
        result: TypedExecution,
        *,
        now: str,
    ) -> str:
        decision = decision_value(result.decision) if result.decision is not None else None
        digest = _execution_digest(result.execution, decision)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            previous = connection.execute(
                "SELECT execution_digest FROM product_durable_resolutions WHERE job_id=?",
                (lease.job_id,),
            ).fetchone()
            if previous is not None:
                if str(previous[0]) != digest:
                    raise JobStoreError("durable execution replay conflicts with prior resolution")
                connection.commit()
                return digest
            row = connection.execute(
                "SELECT j.receipt_id,j.state,j.lease_generation,r.request_digest,"
                "p.request_digest,p.state,p.receipt_json,p.receipt_version "
                "FROM product_jobs j JOIN product_job_requests r ON r.job_id=j.job_id "
                "JOIN product_receipts p ON p.receipt_id=j.receipt_id WHERE j.job_id=?",
                (lease.job_id,),
            ).fetchone()
            active = connection.execute(
                "SELECT state FROM product_job_leases WHERE lease_id=? AND job_id=? "
                "AND lease_generation=? AND holder_id=? AND process_token=?",
                (
                    lease.lease_id,
                    lease.job_id,
                    lease.lease_generation,
                    lease.holder_id,
                    lease.process_token,
                ),
            ).fetchone()
            if row is None or active is None or active[0] != "ACTIVE":
                raise JobLeaseLost("durable resolution has no active exact lease")
            if (
                int(row[2]) != lease.lease_generation
                or row[1] not in {"RUNNING", "WAITING", "CANCEL_REQUESTED"}
                or row[3] != row[4]
                or row[5] != "PENDING"
            ):
                raise JobStoreError("job, request, receipt, or lease fence changed")
            execution = result.execution
            if execution.outcome is ExecutionOutcome.CANCELLED and row[1] != "CANCEL_REQUESTED":
                raise JobStoreError("CANCELLED requires a persisted cancel request")
            execution_receipt_id = self._ids()
            connection.execute(
                "INSERT INTO product_job_execution_receipts("
                "execution_receipt_id,job_id,lease_id,lease_generation,outcome,exit_code,"
                "result_refs_json,failure_code,authority_effect,received_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    execution_receipt_id,
                    lease.job_id,
                    lease.lease_id,
                    lease.lease_generation,
                    execution.outcome,
                    execution.exit_code,
                    _json(list(execution.result_refs)),
                    execution.failure_code,
                    "NONE",
                    now,
                ),
            )
            receipt = json.loads(str(row[6]))
            if not isinstance(receipt, dict):
                raise JobStoreError("pending product receipt is not an object")
            receipt["receipt_version"] = int(row[7]) + 1
            receipt["updated_at"] = now
            if decision is None:
                receipt["state"] = "OUTCOME_UNKNOWN"
                receipt["unknown_external_call_ref"] = (
                    f"job:{lease.job_id}:lease-generation:{lease.lease_generation}"
                )
                receipt.pop("decision", None)
                receipt.pop("decided_at", None)
                product_state = "OUTCOME_UNKNOWN"
            else:
                receipt["state"] = "DECIDED"
                receipt["decision"] = decision
                receipt["decided_at"] = now
                receipt.pop("unknown_external_call_ref", None)
                product_state = "DECIDED"
            connection.execute(
                "UPDATE product_job_leases SET state='RELEASED',released_at=? "
                "WHERE lease_id=? AND state='ACTIVE'",
                (now, lease.lease_id),
            )
            connection.execute(
                "UPDATE product_jobs SET state=?,result_refs_json=?,failure_code=?,"
                "finished_at=?,authority_effect='NONE' WHERE job_id=?",
                (
                    JobState(execution.outcome),
                    _json(list(execution.result_refs)),
                    execution.failure_code,
                    now,
                    lease.job_id,
                ),
            )
            connection.execute(
                "UPDATE product_receipts SET receipt_version=receipt_version+1,state=?,"
                "receipt_json=?,updated_at=? WHERE receipt_id=? AND state='PENDING'",
                (product_state, _json(receipt), now, str(row[0])),
            )
            connection.execute(
                "INSERT INTO product_durable_resolutions(job_id,receipt_id,lease_generation,"
                "execution_digest,outcome,decision_json,resolved_at) VALUES(?,?,?,?,?,?,?)",
                (
                    lease.job_id,
                    str(row[0]),
                    lease.lease_generation,
                    digest,
                    execution.outcome,
                    _json(decision) if decision is not None else None,
                    now,
                ),
            )
            connection.commit()
        return digest

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._db_path, timeout=self._busy_timeout_ms / 1_000)
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms}")
        return connection


class DurableJobPump:
    """Claim one queued job and invoke only its explicitly registered typed executor."""

    def __init__(
        self,
        *,
        supervisor: RuntimeSupervisor,
        jobs: JobStore,
        resolver: DurableJobResolver,
        executors: Mapping[str, DurableExecutor],
        clock: Callable[[], str],
        process_tokens: Callable[[], str],
        lease_seconds: int = 60,
    ) -> None:
        if lease_seconds < 1:
            raise ValueError("lease_seconds must be positive")
        self._supervisor = supervisor
        self._jobs = jobs
        self._resolver = resolver
        self._executors = dict(executors)
        self._clock = clock
        self._tokens = process_tokens
        self._lease_seconds = lease_seconds

    @property
    def kinds(self) -> frozenset[str]:
        return frozenset(self._executors)

    def run_once(self) -> bool:
        now = self._clock()
        expires = _plus_seconds(now, self._lease_seconds)
        claimed = self._supervisor.claim(process_token=self._tokens(), expires_at=expires)
        if claimed is None:
            return False
        job, lease = claimed
        executor = self._executors.get(job.kind)
        if executor is None:
            raise JobStoreError(f"claimed job has no typed executor: {job.kind}")
        request = self._jobs.request(job.job_id)
        result = executor(job, request.value)
        self._resolver.resolve(lease, result, now=self._clock())
        return True


def _execution_digest(execution: ExecutionReceipt, decision: Mapping[str, object] | None) -> str:
    return hashlib.sha256(
        _json(
            {
                "outcome": execution.outcome,
                "exit_code": execution.exit_code,
                "result_refs": list(execution.result_refs),
                "failure_code": execution.failure_code,
                "decision": decision,
            }
        ).encode()
    ).hexdigest()


def _json(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _plus_seconds(value: str, seconds: int) -> str:
    instant = datetime.fromisoformat(value.replace("Z", "+00:00")) + timedelta(seconds=seconds)
    return instant.isoformat().replace("+00:00", "Z")


__all__ = [
    "DurableExecutor",
    "DurableJobPump",
    "DurableJobResolver",
    "TypedExecution",
]
