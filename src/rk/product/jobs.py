"""Durable product jobs, lease generations, checkpoints, and execution receipts."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any


class JobState(StrEnum):
    QUEUED = "QUEUED"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    WAITING = "WAITING"
    CANCEL_REQUESTED = "CANCEL_REQUESTED"
    CANCELLED = "CANCELLED"
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"
    STALE = "STALE"
    INVALIDATED = "INVALIDATED"


class RetrySafety(StrEnum):
    IDEMPOTENT = "IDEMPOTENT"
    READ_ONLY = "READ_ONLY"
    IDEMPOTENCY_KEY = "IDEMPOTENCY_KEY"
    MANUAL_ONLY = "MANUAL_ONLY"


class ExecutionOutcome(StrEnum):
    SUCCEEDED = "SUCCEEDED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    OUTCOME_UNKNOWN = "OUTCOME_UNKNOWN"


_TERMINAL = frozenset(
    {
        JobState.CANCELLED,
        JobState.SUCCEEDED,
        JobState.FAILED,
        JobState.OUTCOME_UNKNOWN,
        JobState.STALE,
        JobState.INVALIDATED,
    }
)
_AUTOMATIC_RETRY = frozenset(
    {RetrySafety.IDEMPOTENT, RetrySafety.READ_ONLY, RetrySafety.IDEMPOTENCY_KEY}
)


class JobStoreError(RuntimeError):
    """A durable job invariant or state transition was violated."""


class JobLeaseLost(JobStoreError):
    """A process attempted to act through an expired or superseded lease."""


@dataclass(frozen=True, slots=True)
class DurableJob:
    job_id: str
    receipt_id: str
    scope_kind: str
    run_id: str | None
    deployment_id: str | None
    kind: str
    requested_by: str
    request_id: str
    state: JobState
    retry_safety: RetrySafety
    idempotency_key: str | None
    lease_generation: int
    current_checkpoint_id: str | None
    worker_run_ids: tuple[str, ...]
    result_refs: tuple[Mapping[str, Any], ...]
    failure_code: str | None
    authority_effect: str
    created_at: str
    started_at: str | None
    finished_at: str | None


@dataclass(frozen=True, slots=True)
class JobLease:
    lease_id: str
    job_id: str
    lease_generation: int
    holder_id: str
    process_token: str
    claimed_at: str
    heartbeat_at: str
    expires_at: str


@dataclass(frozen=True, slots=True)
class JobCheckpoint:
    checkpoint_id: str
    job_id: str
    research_revision: int
    contract_version: int
    artifact_id: str
    checkpoint_digest: str
    state: str
    created_at: str
    invalidated_at: str | None
    invalidation_reason: str | None


@dataclass(frozen=True, slots=True)
class ExecutionReceipt:
    outcome: ExecutionOutcome
    exit_code: int | None
    result_refs: tuple[Mapping[str, Any], ...]
    failure_code: str | None = None


@dataclass(frozen=True, slots=True)
class PendingJobOrphan:
    receipt_id: str
    job_id: str
    request_id: str
    scope_kind: str
    run_id: str | None
    deployment_id: str | None
    created_at: str


@dataclass(frozen=True, slots=True)
class RecoveryResult:
    requeued_job_ids: tuple[str, ...]
    unknown_job_ids: tuple[str, ...]


class JobStore:
    """Single-writer transactional store for durable product execution."""

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

    def enqueue(
        self,
        *,
        job_id: str,
        receipt_id: str,
        scope_kind: str,
        run_id: str | None,
        deployment_id: str | None,
        kind: str,
        requested_by: str,
        request_id: str,
        retry_safety: RetrySafety,
        idempotency_key: str | None,
        now: str,
        worker_run_ids: Sequence[str] = (),
    ) -> DurableJob:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self.enqueue_in_transaction(
                connection,
                job_id=job_id,
                receipt_id=receipt_id,
                scope_kind=scope_kind,
                run_id=run_id,
                deployment_id=deployment_id,
                kind=kind,
                requested_by=requested_by,
                request_id=request_id,
                retry_safety=retry_safety,
                idempotency_key=idempotency_key,
                now=now,
                worker_run_ids=worker_run_ids,
            )
            connection.commit()
        return self.get(job_id)

    def enqueue_in_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        job_id: str,
        receipt_id: str,
        scope_kind: str,
        run_id: str | None,
        deployment_id: str | None,
        kind: str,
        requested_by: str,
        request_id: str,
        retry_safety: RetrySafety,
        idempotency_key: str | None,
        now: str,
        worker_run_ids: Sequence[str] = (),
    ) -> None:
        """Bind a receipt and job in the caller's transaction.

        OperationStore must invoke this after inserting the PENDING receipt and before
        committing it.  The method never commits or rolls back the caller's transaction.
        """
        if not connection.in_transaction:
            raise JobStoreError("atomic job handoff requires an active transaction")
        scope_key = _scope_key(scope_kind, run_id, deployment_id)
        _retry_binding(retry_safety, idempotency_key)
        existing = connection.execute(
            "SELECT job_id,receipt_id FROM product_jobs WHERE scope_key=? AND request_id=?",
            (scope_key, request_id),
        ).fetchone()
        if existing is not None:
            if existing != (job_id, receipt_id):
                raise JobStoreError("request is already bound to another durable job")
            return
        receipt = connection.execute(
            "SELECT state,receipt_json FROM product_receipts WHERE receipt_id=?",
            (receipt_id,),
        ).fetchone()
        if receipt is None or receipt[0] != "PENDING":
            raise JobStoreError("job requires its persisted PENDING receipt")
        receipt_value = json.loads(str(receipt[1]))
        if not isinstance(receipt_value, dict) or receipt_value.get("job_id") != job_id:
            raise JobStoreError("receipt job_id does not match durable job identity")
        connection.execute(
            "INSERT INTO product_jobs("
            "job_id,receipt_id,scope_key,scope_kind,run_id,deployment_id,kind,requested_by,"
            "request_id,state,retry_safety,idempotency_key,worker_run_ids_json,"
            "created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                job_id,
                receipt_id,
                scope_key,
                scope_kind,
                run_id,
                deployment_id,
                kind,
                requested_by,
                request_id,
                JobState.QUEUED,
                retry_safety,
                idempotency_key,
                _json_array(worker_run_ids),
                now,
            ),
        )

    def get(self, job_id: str) -> DurableJob:
        with self._connect() as connection:
            row = connection.execute(_JOB_SELECT + " WHERE job_id=?", (job_id,)).fetchone()
        if row is None:
            raise KeyError(job_id)
        return _job(row)

    def claim_next(
        self,
        *,
        holder_id: str,
        process_token: str,
        now: str,
        expires_at: str,
    ) -> tuple[DurableJob, JobLease] | None:
        if expires_at <= now:
            raise ValueError("lease expiry must be after claim time")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT job_id,lease_generation FROM product_jobs WHERE state='QUEUED' "
                "ORDER BY created_at,job_id LIMIT 1"
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            job_id, old_generation = str(row[0]), int(row[1])
            generation = old_generation + 1
            lease_id = self._ids()
            changed = connection.execute(
                "UPDATE product_jobs SET state='RUNNING',lease_generation=?,"
                "started_at=COALESCE(started_at,?) WHERE job_id=? AND state='QUEUED' "
                "AND lease_generation=?",
                (generation, now, job_id, old_generation),
            ).rowcount
            if changed != 1:
                raise JobStoreError("job claim lost the queue race")
            connection.execute(
                "INSERT INTO product_job_leases("
                "lease_id,job_id,lease_generation,holder_id,process_token,state,"
                "claimed_at,heartbeat_at,expires_at) VALUES(?,?,?,?,?,'ACTIVE',?,?,?)",
                (lease_id, job_id, generation, holder_id, process_token, now, now, expires_at),
            )
            connection.commit()
        return self.get(job_id), JobLease(
            lease_id, job_id, generation, holder_id, process_token, now, now, expires_at
        )

    def heartbeat(
        self,
        lease: JobLease,
        *,
        now: str,
        expires_at: str,
    ) -> JobLease:
        if expires_at <= now:
            raise ValueError("lease expiry must be after heartbeat")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            changed = connection.execute(
                "UPDATE product_job_leases SET heartbeat_at=?,expires_at=? "
                "WHERE lease_id=? AND job_id=? AND lease_generation=? AND holder_id=? "
                "AND process_token=? AND state='ACTIVE' AND expires_at>?",
                (
                    now,
                    expires_at,
                    lease.lease_id,
                    lease.job_id,
                    lease.lease_generation,
                    lease.holder_id,
                    lease.process_token,
                    now,
                ),
            ).rowcount
            if changed != 1:
                raise JobLeaseLost("heartbeat lease is expired or superseded")
            connection.commit()
        return JobLease(
            lease.lease_id,
            lease.job_id,
            lease.lease_generation,
            lease.holder_id,
            lease.process_token,
            lease.claimed_at,
            now,
            expires_at,
        )

    def request_cancel(self, job_id: str, *, now: str) -> DurableJob:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT state FROM product_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            if row is None:
                raise KeyError(job_id)
            state = JobState(str(row[0]))
            if state in _TERMINAL:
                raise JobStoreError(f"terminal job cannot be cancelled: {state}")
            if state == JobState.QUEUED:
                connection.execute(
                    "UPDATE product_jobs SET state='CANCELLED',finished_at=? WHERE job_id=?",
                    (now, job_id),
                )
            else:
                connection.execute(
                    "UPDATE product_jobs SET state='CANCEL_REQUESTED' WHERE job_id=?",
                    (job_id,),
                )
            connection.commit()
        return self.get(job_id)

    def record_execution(
        self,
        lease: JobLease,
        receipt: ExecutionReceipt,
        *,
        now: str,
    ) -> DurableJob:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            lease_row = connection.execute(
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
            job_row = connection.execute(
                "SELECT state,lease_generation FROM product_jobs WHERE job_id=?",
                (lease.job_id,),
            ).fetchone()
            if (
                lease_row is None
                or lease_row[0] != "ACTIVE"
                or job_row is None
                or int(job_row[1]) != lease.lease_generation
            ):
                raise JobLeaseLost("execution receipt lease is expired or superseded")
            current = JobState(str(job_row[0]))
            if (
                receipt.outcome == ExecutionOutcome.CANCELLED
                and current != JobState.CANCEL_REQUESTED
            ):
                raise JobStoreError("CANCELLED requires a persisted cancel request")
            if current not in {JobState.RUNNING, JobState.WAITING, JobState.CANCEL_REQUESTED}:
                raise JobStoreError(f"job cannot accept an execution receipt from {current}")
            receipt_id = self._ids()
            connection.execute(
                "INSERT INTO product_job_execution_receipts("
                "execution_receipt_id,job_id,lease_id,lease_generation,outcome,exit_code,"
                "result_refs_json,failure_code,authority_effect,received_at)"
                " VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    receipt_id,
                    lease.job_id,
                    lease.lease_id,
                    lease.lease_generation,
                    receipt.outcome,
                    receipt.exit_code,
                    _json_array(receipt.result_refs),
                    receipt.failure_code,
                    "NONE",
                    now,
                ),
            )
            connection.execute(
                "UPDATE product_job_leases SET state='RELEASED',released_at=? "
                "WHERE lease_id=? AND state='ACTIVE'",
                (now, lease.lease_id),
            )
            connection.execute(
                "UPDATE product_jobs SET state=?,result_refs_json=?,failure_code=?,"
                "finished_at=?,authority_effect='NONE' WHERE job_id=?",
                (
                    JobState(receipt.outcome),
                    _json_array(receipt.result_refs),
                    receipt.failure_code,
                    now,
                    lease.job_id,
                ),
            )
            connection.commit()
        return self.get(lease.job_id)

    def save_checkpoint(
        self,
        job_id: str,
        *,
        research_revision: int,
        contract_version: int,
        artifact_id: str,
        checkpoint_digest: str,
        now: str,
    ) -> JobCheckpoint:
        if research_revision < 0 or contract_version < 1 or len(checkpoint_digest) != 64:
            raise ValueError("invalid checkpoint binding")
        checkpoint_id = self._ids()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            job = connection.execute(
                "SELECT scope_kind,state FROM product_jobs WHERE job_id=?", (job_id,)
            ).fetchone()
            if job is None:
                raise KeyError(job_id)
            if job[0] != "RUN" or JobState(str(job[1])) in _TERMINAL:
                raise JobStoreError("checkpoint requires a live RUN job")
            connection.execute(
                "INSERT INTO product_job_checkpoints("
                "checkpoint_id,job_id,research_revision,contract_version,artifact_id,"
                "checkpoint_digest,state,created_at) VALUES(?,?,?,?,?,?,'ACTIVE',?)",
                (
                    checkpoint_id,
                    job_id,
                    research_revision,
                    contract_version,
                    artifact_id,
                    checkpoint_digest,
                    now,
                ),
            )
            connection.execute(
                "UPDATE product_jobs SET current_checkpoint_id=? WHERE job_id=?",
                (checkpoint_id, job_id),
            )
            connection.commit()
        return self.get_checkpoint(checkpoint_id)

    def get_checkpoint(self, checkpoint_id: str) -> JobCheckpoint:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT checkpoint_id,job_id,research_revision,contract_version,artifact_id,"
                "checkpoint_digest,state,created_at,invalidated_at,invalidation_reason "
                "FROM product_job_checkpoints WHERE checkpoint_id=?",
                (checkpoint_id,),
            ).fetchone()
        if row is None:
            raise KeyError(checkpoint_id)
        return JobCheckpoint(
            str(row[0]),
            str(row[1]),
            int(row[2]),
            int(row[3]),
            str(row[4]),
            str(row[5]),
            str(row[6]),
            str(row[7]),
            str(row[8]) if row[8] is not None else None,
            str(row[9]) if row[9] is not None else None,
        )

    def invalidate_checkpoint(
        self,
        checkpoint_id: str,
        *,
        reason: str,
        now: str,
        stale: bool = False,
    ) -> DurableJob:
        checkpoint_state = "STALE" if stale else "INVALIDATED"
        job_state = JobState.STALE if stale else JobState.INVALIDATED
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT job_id,state FROM product_job_checkpoints WHERE checkpoint_id=?",
                (checkpoint_id,),
            ).fetchone()
            if row is None:
                raise KeyError(checkpoint_id)
            if row[1] != "ACTIVE":
                raise JobStoreError("checkpoint is already invalidated")
            job_id = str(row[0])
            connection.execute(
                "UPDATE product_job_checkpoints SET state=?,invalidation_reason=?,"
                "invalidated_at=? WHERE checkpoint_id=? AND state='ACTIVE'",
                (checkpoint_state, reason, now, checkpoint_id),
            )
            connection.execute(
                "UPDATE product_job_leases SET state='EXPIRED',released_at=? "
                "WHERE job_id=? AND state='ACTIVE'",
                (now, job_id),
            )
            connection.execute(
                "UPDATE product_jobs SET state=?,failure_code=?,finished_at=? "
                "WHERE job_id=? AND state NOT IN "
                "('CANCELLED','SUCCEEDED','FAILED','OUTCOME_UNKNOWN','STALE','INVALIDATED')",
                (job_state, reason, now, job_id),
            )
            connection.commit()
        return self.get(job_id)

    def recover_expired(self, *, now: str) -> RecoveryResult:
        requeued: list[str] = []
        unknown: list[str] = []
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                "SELECT l.lease_id,l.job_id,l.lease_generation,j.state,j.retry_safety "
                "FROM product_job_leases l JOIN product_jobs j ON j.job_id=l.job_id "
                "WHERE l.state='ACTIVE' AND l.expires_at<=? "
                "ORDER BY l.expires_at,l.lease_id",
                (now,),
            ).fetchall()
            for lease_id, job_id, generation, state_value, retry_value in rows:
                connection.execute(
                    "UPDATE product_job_leases SET state='EXPIRED',released_at=? "
                    "WHERE lease_id=? AND state='ACTIVE'",
                    (now, lease_id),
                )
                state = JobState(str(state_value))
                retry = RetrySafety(str(retry_value))
                if state == JobState.CANCEL_REQUESTED or retry not in _AUTOMATIC_RETRY:
                    next_state = JobState.OUTCOME_UNKNOWN
                    unknown.append(str(job_id))
                    finished_at: str | None = now
                else:
                    next_state = JobState.QUEUED
                    requeued.append(str(job_id))
                    finished_at = None
                connection.execute(
                    "UPDATE product_jobs SET state=?,finished_at=?,"
                    "failure_code='LEASE_EXPIRED' WHERE job_id=? AND lease_generation=?",
                    (next_state, finished_at, job_id, generation),
                )
            connection.commit()
        return RecoveryResult(tuple(requeued), tuple(unknown))

    def pending_orphans(self) -> tuple[PendingJobOrphan, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT r.receipt_id,r.request_id,r.receipt_json,r.created_at "
                "FROM product_receipts r LEFT JOIN product_jobs j "
                "ON j.receipt_id=r.receipt_id WHERE r.state='PENDING' AND j.job_id IS NULL "
                "ORDER BY r.created_at,r.receipt_id"
            ).fetchall()
        result: list[PendingJobOrphan] = []
        for receipt_id, request_id, receipt_json, created_at in rows:
            value = json.loads(str(receipt_json))
            if not isinstance(value, dict) or not isinstance(value.get("job_id"), str):
                raise JobStoreError("orphan PENDING receipt has no stable job_id")
            scope = value.get("scope")
            if not isinstance(scope, dict) or not isinstance(scope.get("kind"), str):
                raise JobStoreError("orphan PENDING receipt has no stable scope")
            kind = str(scope["kind"])
            run_id = str(scope["run_id"]) if scope.get("run_id") is not None else None
            deployment_id = (
                str(scope["deployment_id"]) if scope.get("deployment_id") is not None else None
            )
            _scope_key(kind, run_id, deployment_id)
            result.append(
                PendingJobOrphan(
                    str(receipt_id),
                    str(value["job_id"]),
                    str(request_id),
                    kind,
                    run_id,
                    deployment_id,
                    str(created_at),
                )
            )
        return tuple(result)

    def job_id_for_lease(self, lease_id: str) -> str:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT job_id FROM product_job_leases WHERE lease_id=?", (lease_id,)
            ).fetchone()
        if row is None:
            raise KeyError(lease_id)
        return str(row[0])

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._db_path, timeout=self._busy_timeout_ms / 1_000)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
        return connection


_JOB_SELECT = (
    "SELECT job_id,receipt_id,scope_kind,run_id,deployment_id,kind,requested_by,request_id,state,"
    "retry_safety,idempotency_key,lease_generation,current_checkpoint_id,"
    "worker_run_ids_json,result_refs_json,failure_code,authority_effect,"
    "created_at,started_at,finished_at FROM product_jobs"
)


def _job(row: tuple[object, ...]) -> DurableJob:
    workers = _json_list(row[13])
    results = _json_list(row[14])
    if not all(isinstance(item, str) for item in workers):
        raise JobStoreError("stored worker_run_ids must be strings")
    if not all(isinstance(item, dict) for item in results):
        raise JobStoreError("stored result_refs must be objects")
    return DurableJob(
        job_id=str(row[0]),
        receipt_id=str(row[1]),
        scope_kind=str(row[2]),
        run_id=str(row[3]) if row[3] is not None else None,
        deployment_id=str(row[4]) if row[4] is not None else None,
        kind=str(row[5]),
        requested_by=str(row[6]),
        request_id=str(row[7]),
        state=JobState(str(row[8])),
        retry_safety=RetrySafety(str(row[9])),
        idempotency_key=str(row[10]) if row[10] is not None else None,
        lease_generation=int(str(row[11])),
        current_checkpoint_id=str(row[12]) if row[12] is not None else None,
        worker_run_ids=tuple(str(item) for item in workers),
        result_refs=tuple(item for item in results if isinstance(item, dict)),
        failure_code=str(row[15]) if row[15] is not None else None,
        authority_effect=str(row[16]),
        created_at=str(row[17]),
        started_at=str(row[18]) if row[18] is not None else None,
        finished_at=str(row[19]) if row[19] is not None else None,
    )


def _scope_key(scope_kind: str, run_id: str | None, deployment_id: str | None) -> str:
    if scope_kind == "GLOBAL" and deployment_id and run_id is None:
        return f"GLOBAL:{deployment_id}"
    if scope_kind == "RUN" and run_id and deployment_id is None:
        return f"RUN:{run_id}"
    if scope_kind == "DEPLOYMENT" and deployment_id and run_id is None:
        return f"DEPLOYMENT:{deployment_id}"
    raise ValueError("job scope fields do not match scope_kind")


def _retry_binding(safety: RetrySafety, idempotency_key: str | None) -> None:
    if (safety == RetrySafety.IDEMPOTENCY_KEY) != (idempotency_key is not None):
        raise ValueError("idempotency key must exactly match retry safety")


def _json_array(value: Sequence[object]) -> str:
    return json.dumps(list(value), ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _json_list(value: object) -> list[Any]:
    parsed = json.loads(str(value))
    if not isinstance(parsed, list):
        raise JobStoreError("stored JSON is not an array")
    return parsed
