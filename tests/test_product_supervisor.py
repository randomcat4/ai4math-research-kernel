from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

import pytest

from rk.product.api import ProductCommand, ProductSession, RunScope
from rk.product.command_service import CommandPlan, ExecutionClass, ProductCommandService
from rk.product.jobs import (
    ExecutionOutcome,
    ExecutionReceipt,
    JobLeaseLost,
    JobState,
    JobStore,
    JobStoreError,
    RetrySafety,
)
from rk.product.operations import OperationStore
from rk.product.supervisor import OrphanJobSpec, RuntimeSupervisor
from rk.product_migrations import ProductMigrationAssembler, ProductMigrationRegistry


def migrated_db(tmp_path: Path) -> Path:
    db = tmp_path / "product.sqlite"
    with sqlite3.connect(db) as connection:
        ProductMigrationAssembler(ProductMigrationRegistry(Path("schema_fragments"))).apply(
            connection
        )
    return db


def pending_receipt(
    db: Path,
    *,
    job_id: str,
    receipt_id: str = "receipt-1",
    request_id: str = "request-1",
) -> str:
    body = {
        "schema_version": "rk.product.receipt.v1",
        "request_id": request_id,
        "scope": {
            "kind": "RUN",
            "run_id": "run-1",
            "expected_revision": 3,
            "expected_contract_version": 1,
        },
        "updated_at": "2026-08-13T00:00:00Z",
        "state": "PENDING",
        "job_id": job_id,
    }
    store = OperationStore(db, lambda: receipt_id)
    return store.reserve(
        scope_key="RUN:run-1",
        request_id=request_id,
        request_digest="a" * 64,
        pending_receipt=body,
        now="2026-08-13T00:00:00Z",
    ).receipt.receipt_id


def store_with_ids(db: Path, *ids: str) -> JobStore:
    return JobStore(db, iter(ids).__next__)


def enqueue(
    store: JobStore,
    *,
    job_id: str = "job-1",
    receipt_id: str = "receipt-1",
    retry: RetrySafety = RetrySafety.IDEMPOTENT,
) -> None:
    store.enqueue(
        job_id=job_id,
        receipt_id=receipt_id,
        scope_kind="RUN",
        run_id="run-1",
        deployment_id=None,
        kind="RUN_TOOL",
        requested_by="subject-1",
        request_id="request-1",
        retry_safety=retry,
        idempotency_key=None,
        now="2026-08-13T00:00:00Z",
    )


def test_job_survives_restart_and_lease_heartbeat_is_generation_bound(
    tmp_path: Path,
) -> None:
    db = migrated_db(tmp_path)
    pending_receipt(db, job_id="job-1")
    first = store_with_ids(db, "lease-1")
    enqueue(first)
    claimed = first.claim_next(
        holder_id="daemon-1",
        process_token="process-1",
        now="2026-08-13T00:00:01Z",
        expires_at="2026-08-13T00:00:10Z",
    )
    assert claimed is not None
    job, lease = claimed
    assert job.state == JobState.RUNNING
    assert lease.lease_generation == 1

    restarted = store_with_ids(db, "unused")
    heartbeat = restarted.heartbeat(
        lease,
        now="2026-08-13T00:00:05Z",
        expires_at="2026-08-13T00:00:15Z",
    )
    assert heartbeat.heartbeat_at == "2026-08-13T00:00:05Z"
    assert restarted.recover_expired(now="2026-08-13T00:00:11Z").requeued_job_ids == ()
    assert restarted.get("job-1").state == JobState.RUNNING


def test_restart_requeues_only_safe_expired_job_and_rejects_old_process_receipt(
    tmp_path: Path,
) -> None:
    db = migrated_db(tmp_path)
    pending_receipt(db, job_id="job-1")
    first = store_with_ids(db, "lease-1")
    enqueue(first)
    claimed = first.claim_next(
        holder_id="daemon-1",
        process_token="process-1",
        now="2026-08-13T00:00:01Z",
        expires_at="2026-08-13T00:00:02Z",
    )
    assert claimed is not None
    _, old_lease = claimed

    restarted = store_with_ids(db, "lease-2", "execution-receipt-2")
    recovery = restarted.recover_expired(now="2026-08-13T00:00:03Z")
    assert recovery.requeued_job_ids == ("job-1",)
    assert restarted.get("job-1").state == JobState.QUEUED
    second = restarted.claim_next(
        holder_id="daemon-2",
        process_token="process-2",
        now="2026-08-13T00:00:04Z",
        expires_at="2026-08-13T00:00:20Z",
    )
    assert second is not None
    _, new_lease = second
    assert new_lease.lease_generation == 2

    success = ExecutionReceipt(ExecutionOutcome.SUCCEEDED, 0, ({"artifact_id": "a-1"},))
    with pytest.raises(JobLeaseLost):
        restarted.record_execution(old_lease, success, now="2026-08-13T00:00:05Z")
    finished = restarted.record_execution(new_lease, success, now="2026-08-13T00:00:06Z")
    assert finished.state == JobState.SUCCEEDED
    assert finished.authority_effect == "NONE"
    with sqlite3.connect(db) as connection:
        assert connection.execute(
            "SELECT outcome,authority_effect FROM product_job_execution_receipts"
        ).fetchone() == ("SUCCEEDED", "NONE")


def test_unsafe_expired_remote_job_becomes_outcome_unknown(tmp_path: Path) -> None:
    db = migrated_db(tmp_path)
    pending_receipt(db, job_id="job-1")
    store = store_with_ids(db, "lease-1")
    enqueue(store, retry=RetrySafety.MANUAL_ONLY)
    assert store.claim_next(
        holder_id="daemon-1",
        process_token="remote-call-1",
        now="2026-08-13T00:00:01Z",
        expires_at="2026-08-13T00:00:02Z",
    )

    result = store.recover_expired(now="2026-08-13T00:00:03Z")

    assert result.unknown_job_ids == ("job-1",)
    assert store.get("job-1").state == JobState.OUTCOME_UNKNOWN


@dataclass
class Process:
    receipt: ExecutionReceipt | None = None
    cancel_requested: bool = False

    def request_cancel(self) -> None:
        self.cancel_requested = True

    def poll_receipt(self) -> ExecutionReceipt | None:
        return self.receipt


def test_cancel_requested_waits_for_real_process_receipt(tmp_path: Path) -> None:
    db = migrated_db(tmp_path)
    pending_receipt(db, job_id="job-1")
    times = iter(
        (
            "2026-08-13T00:00:00Z",
            "2026-08-13T00:00:01Z",
            "2026-08-13T00:00:02Z",
            "2026-08-13T00:00:03Z",
        )
    )
    store = store_with_ids(db, "lease-1", "execution-receipt-1")
    enqueue(store)
    supervisor = RuntimeSupervisor(
        store=store,
        holder_id="daemon-1",
        clock=times.__next__,
        retry_policy={"RUN_TOOL": RetrySafety.IDEMPOTENT},
    )
    claimed = supervisor.claim(process_token="process-1", expires_at="2026-08-13T00:00:30Z")
    assert claimed is not None
    _, lease = claimed
    process = Process()
    supervisor.attach_process(lease, process)

    requested = supervisor.request_cancel("job-1", lease=lease)
    assert requested.state == JobState.CANCEL_REQUESTED
    assert process.cancel_requested is True
    assert supervisor.harvest(lease).state == JobState.CANCEL_REQUESTED

    process.receipt = ExecutionReceipt(ExecutionOutcome.CANCELLED, -15, ())
    assert supervisor.harvest(lease).state == JobState.CANCELLED


def test_cancelled_receipt_without_cancel_request_is_rejected(tmp_path: Path) -> None:
    db = migrated_db(tmp_path)
    pending_receipt(db, job_id="job-1")
    store = store_with_ids(db, "lease-1")
    enqueue(store)
    claimed = store.claim_next(
        holder_id="daemon-1",
        process_token="process-1",
        now="2026-08-13T00:00:01Z",
        expires_at="2026-08-13T00:00:30Z",
    )
    assert claimed is not None
    with pytest.raises(JobStoreError, match="persisted cancel request"):
        store.record_execution(
            claimed[1],
            ExecutionReceipt(ExecutionOutcome.CANCELLED, -15, ()),
            now="2026-08-13T00:00:02Z",
        )
    assert store.get("job-1").state == JobState.RUNNING


def test_checkpoint_is_revision_bound_and_invalidation_expires_live_lease(
    tmp_path: Path,
) -> None:
    db = migrated_db(tmp_path)
    pending_receipt(db, job_id="job-1")
    store = store_with_ids(db, "checkpoint-1", "lease-1")
    enqueue(store)
    checkpoint = store.save_checkpoint(
        "job-1",
        research_revision=7,
        contract_version=2,
        artifact_id="artifact-checkpoint",
        checkpoint_digest="c" * 64,
        now="2026-08-13T00:00:01Z",
    )
    claimed = store.claim_next(
        holder_id="daemon-1",
        process_token="process-1",
        now="2026-08-13T00:00:02Z",
        expires_at="2026-08-13T00:00:30Z",
    )
    assert claimed is not None

    restarted = store_with_ids(db, "unused")
    assert restarted.get_checkpoint(checkpoint.checkpoint_id).research_revision == 7
    invalidated = restarted.invalidate_checkpoint(
        checkpoint.checkpoint_id,
        reason="CONTRACT_VERSION_CHANGED",
        now="2026-08-13T00:00:03Z",
    )
    assert invalidated.state == JobState.INVALIDATED
    assert restarted.get_checkpoint(checkpoint.checkpoint_id).state == "INVALIDATED"
    with pytest.raises(JobLeaseLost):
        restarted.heartbeat(
            claimed[1],
            now="2026-08-13T00:00:04Z",
            expires_at="2026-08-13T00:00:40Z",
        )


def test_supervisor_rebinds_orphan_pending_receipt_after_crash_window(
    tmp_path: Path,
) -> None:
    db = migrated_db(tmp_path)
    pending_receipt(db, job_id="job-1")
    store = store_with_ids(db, "unused")
    supervisor = RuntimeSupervisor(
        store=store,
        holder_id="daemon-1",
        clock=lambda: "2026-08-13T00:00:01Z",
        retry_policy={"RUN_TOOL": RetrySafety.IDEMPOTENT},
        orphan_resolver=lambda orphan: OrphanJobSpec(
            "RUN_TOOL", "subject-from-command-ledger", RetrySafety.IDEMPOTENT
        ),
    )

    recovered = supervisor.recover_startup()

    assert recovered.rebound_job_ids == ("job-1",)
    assert store.get("job-1").receipt_id == "receipt-1"
    assert supervisor.recover_startup().rebound_job_ids == ()


def test_orphan_pending_receipt_without_durable_spec_is_not_silently_skipped(
    tmp_path: Path,
) -> None:
    db = migrated_db(tmp_path)
    pending_receipt(db, job_id="job-1")
    supervisor = RuntimeSupervisor(
        store=store_with_ids(db, "unused"),
        holder_id="daemon-1",
        clock=lambda: "2026-08-13T00:00:01Z",
        retry_policy={"RUN_TOOL": RetrySafety.IDEMPOTENT},
    )
    with pytest.raises(JobStoreError, match="no orphan resolver"):
        supervisor.recover_startup()


def test_supervisor_implements_command_job_queue_binding(tmp_path: Path) -> None:
    db = migrated_db(tmp_path)
    pending_receipt(db, job_id="job-1")
    supervisor = RuntimeSupervisor(
        store=store_with_ids(db, "unused"),
        holder_id="daemon-1",
        clock=lambda: "2026-08-13T00:00:01Z",
        retry_policy={"RUN_TOOL": RetrySafety.IDEMPOTENT},
    )
    supervisor.enqueue(
        job_id="job-1",
        session=ProductSession("session-1", "subject-1", ("capability-1",)),
        request=ProductCommand(
            "request-1",
            RunScope("run-1", 3, 1),
            "RUN_TOOL",
            MappingProxyType({}),
        ),
        receipt_id="receipt-1",
    )
    assert supervisor._store.get("job-1").requested_by == "subject-1"


@dataclass
class CrashingQueue:
    def enqueue_in_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        job_id: str,
        session: ProductSession,
        request: ProductCommand,
        receipt_id: str,
    ) -> None:
        assert connection.in_transaction
        raise RuntimeError("process lost during atomic handoff")


@dataclass
class UnusedAuthority:
    def apply(self, session: ProductSession, request: ProductCommand) -> object:
        raise AssertionError("durable command must not call mathematical authority")


def test_command_service_atomic_handoff_rolls_back_then_retries_cleanly(
    tmp_path: Path,
) -> None:
    db = migrated_db(tmp_path)
    commands = ProductCommandService(
        operations=OperationStore(db, lambda: "receipt-1"),
        authority=UnusedAuthority(),  # type: ignore[arg-type]
        jobs=CrashingQueue(),
        plans={"RUN_TOOL": CommandPlan(ExecutionClass.DURABLE_JOB, "TOOL_RUN")},
        id_generator=lambda: "job-1",
        clock=lambda: "2026-08-13T00:00:00Z",
    )
    request = ProductCommand(
        "request-1",
        RunScope("run-1", 3, 1),
        "RUN_TOOL",
        MappingProxyType({}),
    )
    with pytest.raises(RuntimeError, match="atomic handoff"):
        commands.execute(
            ProductSession("session-1", "subject-1", ("capability-1",)),
            request,
        )

    store = store_with_ids(db, "unused")
    assert store.pending_orphans() == ()
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT COUNT(*) FROM product_receipts").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM product_jobs").fetchone() == (0,)
    supervisor = RuntimeSupervisor(
        store=store,
        holder_id="daemon-restarted",
        clock=lambda: "2026-08-13T00:00:01Z",
        retry_policy={"RUN_TOOL": RetrySafety.IDEMPOTENT},
    )
    retry = ProductCommandService(
        operations=OperationStore(db, lambda: "receipt-1"),
        authority=UnusedAuthority(),  # type: ignore[arg-type]
        jobs=supervisor,
        plans={"RUN_TOOL": CommandPlan(ExecutionClass.DURABLE_JOB, "TOOL_RUN")},
        id_generator=lambda: "job-1",
        clock=lambda: "2026-08-13T00:00:01Z",
    )
    receipt = retry.execute(ProductSession("session-1", "subject-1", ("capability-1",)), request)
    assert receipt.state == "PENDING"
    assert store.get("job-1").state == JobState.QUEUED


def test_enqueue_in_transaction_rolls_back_receipt_and_job_together(tmp_path: Path) -> None:
    db = migrated_db(tmp_path)
    store = store_with_ids(db, "unused")
    with sqlite3.connect(db) as connection:
        connection.execute("BEGIN IMMEDIATE")
        body = (
            '{"job_id":"job-1","request_id":"request-1",'
            '"scope":{"kind":"RUN","run_id":"run-1"},"state":"PENDING"}'
        )
        connection.execute(
            "INSERT INTO product_receipts("
            "receipt_id,receipt_version,scope_key,request_id,request_digest,state,"
            "receipt_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
            (
                "receipt-1",
                1,
                "RUN:run-1",
                "request-1",
                "a" * 64,
                "PENDING",
                body,
                "2026-08-13T00:00:00Z",
                "2026-08-13T00:00:00Z",
            ),
        )
        store.enqueue_in_transaction(
            connection,
            job_id="job-1",
            receipt_id="receipt-1",
            scope_kind="RUN",
            run_id="run-1",
            deployment_id=None,
            kind="RUN_TOOL",
            requested_by="subject-1",
            request_id="request-1",
            retry_safety=RetrySafety.IDEMPOTENT,
            idempotency_key=None,
            now="2026-08-13T00:00:00Z",
        )
        connection.rollback()
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT COUNT(*) FROM product_receipts").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM product_jobs").fetchone() == (0,)


def test_restart_does_not_turn_unacknowledged_cancel_into_cancelled(
    tmp_path: Path,
) -> None:
    db = migrated_db(tmp_path)
    pending_receipt(db, job_id="job-1")
    store = store_with_ids(db, "lease-1")
    enqueue(store)
    claimed = store.claim_next(
        holder_id="daemon-1",
        process_token="process-1",
        now="2026-08-13T00:00:01Z",
        expires_at="2026-08-13T00:00:02Z",
    )
    assert claimed is not None
    assert (
        store.request_cancel("job-1", now="2026-08-13T00:00:01Z").state == JobState.CANCEL_REQUESTED
    )

    recovery = store.recover_expired(now="2026-08-13T00:00:03Z")

    assert recovery.unknown_job_ids == ("job-1",)
    assert store.get("job-1").state == JobState.OUTCOME_UNKNOWN


def test_successful_execution_cannot_modify_mathematical_fact_state(
    tmp_path: Path,
) -> None:
    db = tmp_path / "product.sqlite"
    with sqlite3.connect(db) as connection:
        connection.execute(
            "CREATE TABLE claims(claim_id TEXT PRIMARY KEY,lifecycle_status TEXT) STRICT"
        )
        connection.execute("INSERT INTO claims VALUES('claim-1','CANDIDATE')")
        connection.commit()
        ProductMigrationAssembler(ProductMigrationRegistry(Path("schema_fragments"))).apply(
            connection
        )
    pending_receipt(db, job_id="job-1")
    store = store_with_ids(db, "lease-1", "execution-receipt-1")
    enqueue(store)
    claimed = store.claim_next(
        holder_id="daemon-1",
        process_token="process-1",
        now="2026-08-13T00:00:01Z",
        expires_at="2026-08-13T00:00:20Z",
    )
    assert claimed is not None
    job = store.record_execution(
        claimed[1],
        ExecutionReceipt(ExecutionOutcome.SUCCEEDED, 0, ()),
        now="2026-08-13T00:00:02Z",
    )

    assert job.state == JobState.SUCCEEDED
    assert job.authority_effect == "NONE"
    with sqlite3.connect(db) as connection:
        assert connection.execute(
            "SELECT lifecycle_status FROM claims WHERE claim_id='claim-1'"
        ).fetchone() == ("CANDIDATE",)
