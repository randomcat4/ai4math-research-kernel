"""Runtime owner for durable job leases and managed process acknowledgements."""

from __future__ import annotations

import sqlite3
import threading
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from rk.product.api import DeploymentScope, GlobalScope, ProductCommand, ProductSession, RunScope
from rk.product.command_service import command_request_value
from rk.product.jobs import (
    DurableJob,
    ExecutionReceipt,
    JobLease,
    JobState,
    JobStore,
    JobStoreError,
    PendingJobOrphan,
    RecoveryResult,
    RetrySafety,
)
from rk.product.operations import OperationStore


class ManagedProcess(Protocol):
    """A B12/B12c process adapter bound to one durable lease generation."""

    def request_cancel(self) -> None: ...

    def poll_receipt(self) -> ExecutionReceipt | None: ...


@dataclass(frozen=True, slots=True)
class OrphanJobSpec:
    kind: str
    requested_by: str
    retry_safety: RetrySafety
    idempotency_key: str | None = None


@dataclass(frozen=True, slots=True)
class StartupRecovery:
    expired_leases: RecoveryResult
    rebound_job_ids: tuple[str, ...]


class RuntimeSupervisor:
    """Coordinates the persisted queue without becoming a mathematical authority."""

    def __init__(
        self,
        *,
        store: JobStore,
        holder_id: str,
        clock: Callable[[], str],
        retry_policy: Mapping[str, RetrySafety],
        orphan_resolver: Callable[[PendingJobOrphan], OrphanJobSpec] | None = None,
    ) -> None:
        self._store = store
        self._holder_id = holder_id
        self._clock = clock
        self._retry_policy = dict(retry_policy)
        self._orphan_resolver = orphan_resolver
        self._processes: dict[str, ManagedProcess] = {}
        self._process_lock = threading.RLock()

    def enqueue(
        self,
        *,
        job_id: str,
        session: ProductSession,
        request: ProductCommand,
        receipt_id: str,
    ) -> None:
        safety = self._retry_policy[request.command_type]
        idempotency_key = request.request_id if safety == RetrySafety.IDEMPOTENCY_KEY else None
        run_id: str | None = None
        deployment_id: str | None = None
        if isinstance(request.scope, RunScope):
            run_id = request.scope.run_id
        elif isinstance(request.scope, (GlobalScope, DeploymentScope)):
            deployment_id = request.scope.deployment_id
        self._store.enqueue(
            job_id=job_id,
            receipt_id=receipt_id,
            scope_kind=request.scope.kind,
            run_id=run_id,
            deployment_id=deployment_id,
            kind=request.command_type,
            requested_by=session.principal_subject_id,
            request_id=request.request_id,
            retry_safety=safety,
            idempotency_key=idempotency_key,
            now=self._clock(),
        )

    def enqueue_in_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        job_id: str,
        session: ProductSession,
        request: ProductCommand,
        receipt_id: str,
    ) -> None:
        safety = self._retry_policy[request.command_type]
        idempotency_key = request.request_id if safety == RetrySafety.IDEMPOTENCY_KEY else None
        run_id = request.scope.run_id if isinstance(request.scope, RunScope) else None
        deployment_id = (
            request.scope.deployment_id
            if isinstance(request.scope, (GlobalScope, DeploymentScope))
            else None
        )
        self._store.enqueue_in_transaction(
            connection,
            job_id=job_id,
            receipt_id=receipt_id,
            scope_kind=request.scope.kind,
            run_id=run_id,
            deployment_id=deployment_id,
            kind=request.command_type,
            requested_by=session.principal_subject_id,
            request_id=request.request_id,
            retry_safety=safety,
            idempotency_key=idempotency_key,
            now=self._clock(),
            request_value=command_request_value(request),
            request_digest=OperationStore.request_digest(command_request_value(request)),
        )

    def claim(self, *, process_token: str, expires_at: str) -> tuple[DurableJob, JobLease] | None:
        return self._store.claim_next(
            holder_id=self._holder_id,
            process_token=process_token,
            now=self._clock(),
            expires_at=expires_at,
        )

    def attach_process(self, lease: JobLease, process: ManagedProcess) -> None:
        if lease.holder_id != self._holder_id:
            raise JobStoreError("cannot attach a process owned by another supervisor")
        with self._process_lock:
            if lease.lease_id in self._processes:
                raise JobStoreError("lease already has a managed process")
            self._processes[lease.lease_id] = process

    def heartbeat(self, lease: JobLease, *, expires_at: str) -> JobLease:
        return self._store.heartbeat(lease, now=self._clock(), expires_at=expires_at)

    def request_cancel(self, job_id: str, *, lease: JobLease | None = None) -> DurableJob:
        job = self._store.request_cancel(job_id, now=self._clock())
        if job.state == JobState.CANCEL_REQUESTED:
            if lease is None:
                raise JobStoreError("running cancellation requires the active process lease")
            with self._process_lock:
                process = self._processes.get(lease.lease_id)
            if process is None:
                raise JobStoreError("running cancellation requires an attached process")
            process.request_cancel()
        return job

    def harvest(self, lease: JobLease) -> DurableJob:
        with self._process_lock:
            process = self._processes.get(lease.lease_id)
        if process is None:
            raise JobStoreError("lease has no attached process")
        receipt = process.poll_receipt()
        if receipt is None:
            return self._store.get(lease.job_id)
        try:
            return self._store.record_execution(lease, receipt, now=self._clock())
        finally:
            # A terminal child must never remain attached even if persistence fails.
            with self._process_lock:
                self._processes.pop(lease.lease_id, None)

    def recover_startup(self) -> StartupRecovery:
        result = self._store.recover_expired(now=self._clock())
        recovered = set(result.requeued_job_ids) | set(result.unknown_job_ids)
        with self._process_lock:
            for lease_id in tuple(self._processes):
                if self._processes_job_id(lease_id) in recovered:
                    del self._processes[lease_id]
        orphans = self._store.pending_orphans()
        if orphans and self._orphan_resolver is None:
            raise JobStoreError("PENDING receipt has no durable job and no orphan resolver")
        rebound: list[str] = []
        for orphan in orphans:
            if self._orphan_resolver is None:
                raise AssertionError("orphan resolver invariant")
            spec = self._orphan_resolver(orphan)
            self._store.enqueue(
                job_id=orphan.job_id,
                receipt_id=orphan.receipt_id,
                scope_kind=orphan.scope_kind,
                run_id=orphan.run_id,
                deployment_id=orphan.deployment_id,
                kind=spec.kind,
                requested_by=spec.requested_by,
                request_id=orphan.request_id,
                retry_safety=spec.retry_safety,
                idempotency_key=spec.idempotency_key,
                now=self._clock(),
            )
            rebound.append(orphan.job_id)
        return StartupRecovery(result, tuple(rebound))

    def _processes_job_id(self, lease_id: str) -> str:
        return self._store.job_id_for_lease(lease_id)
