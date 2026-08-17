from __future__ import annotations

import hashlib
import sqlite3
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from rk.extensions import ExtensionRegistry, ToolReceipt
from rk.product.artifact_read import ExactArtifactRef
from rk.product.compute import (
    AuthorityCeiling,
    ResourceRequest,
    ToolAvailability,
    ToolContractError,
    ToolFunctionSpec,
    prepare_tool_invocation,
)
from rk.product.jobs import (
    ExecutionOutcome,
    ExecutionReceipt,
    JobState,
    JobStore,
    RetrySafety,
)
from rk.product.operations import OperationStore
from rk.product.tool_runs import (
    ToolCatalogConflict,
    ToolCatalogStore,
    ToolReceiptAdapter,
    ToolRunConflict,
    ToolRunError,
    ToolRunStore,
    ValidationStatus,
)
from rk.product_migrations import ProductMigrationAssembler, ProductMigrationRegistry
from rk.wire import canonical_json_bytes

NOW = "2026-08-13T00:00:00Z"
SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["n"],
    "properties": {"n": {"type": "integer", "minimum": 1}},
}


class JsonReader:
    def __init__(self, value: Mapping[str, Any]) -> None:
        self.value = value

    def read_json(self, artifact_ref: ExactArtifactRef) -> Mapping[str, Any]:
        assert artifact_ref.artifact_id == "arguments-1"
        return self.value


def migrated_db(tmp_path: Path) -> Path:
    db = tmp_path / "product.sqlite"
    with sqlite3.connect(db) as connection:
        ProductMigrationAssembler(ProductMigrationRegistry(Path("schema_fragments"))).apply(
            connection
        )
    return db


def spec(
    *,
    availability: ToolAvailability = ToolAvailability.AVAILABLE,
    ceiling: AuthorityCeiling = AuthorityCeiling.CERTIFICATE_REQUIRES_VALIDATION,
    profile: str = "cpu-small",
) -> ToolFunctionSpec:
    return ToolFunctionSpec(
        tool_id="lean-checker",
        tool_version="4.19.0",
        function_name="check",
        provider="rk",
        build_version="build-17",
        profile_id=profile,
        function_schema=SCHEMA,
        function_schema_digest=hashlib.sha256(canonical_json_bytes(SCHEMA)).hexdigest(),
        availability=availability,
        authority_ceiling=ceiling,
    )


def invocation(
    declaration: ToolFunctionSpec,
    *,
    arguments: Mapping[str, Any] | None = None,
    ceiling: AuthorityCeiling | None = None,
):
    return prepare_tool_invocation(
        spec=declaration,
        arguments_artifact=ExactArtifactRef(
            artifact_id="arguments-1",
            sha256="a" * 64,
            byte_count=7,
            media_type="application/json",
        ),
        input_artifact_ids=("source-1",),
        resources=ResourceRequest(
            cpu_millis=2_000,
            memory_bytes=1_000_000,
            wall_time_ms=30_000,
        ),
        authority_ceiling=ceiling or declaration.authority_ceiling,
        artifacts=JsonReader(arguments or {"n": 3}),
    )


def pending(
    db: Path,
    *,
    receipt_id: str,
    job_id: str,
    request_id: str,
    run_id: str = "run-1",
) -> None:
    body = {
        "schema_version": "rk.product.receipt.v1",
        "request_id": request_id,
        "scope": {
            "kind": "RUN",
            "run_id": run_id,
            "expected_revision": 3,
            "expected_contract_version": 1,
        },
        "updated_at": NOW,
        "state": "PENDING",
        "job_id": job_id,
    }
    OperationStore(db, iter([receipt_id]).__next__).reserve(
        scope_key=f"RUN:{run_id}",
        request_id=request_id,
        request_digest=hashlib.sha256(request_id.encode()).hexdigest(),
        pending_receipt=body,
        now=NOW,
    )


def enqueue(
    jobs: JobStore,
    db: Path,
    *,
    job_id: str = "job-1",
    receipt_id: str = "receipt-1",
    request_id: str = "request-1",
    run_id: str = "run-1",
) -> None:
    pending(
        db,
        receipt_id=receipt_id,
        job_id=job_id,
        request_id=request_id,
        run_id=run_id,
    )
    jobs.enqueue(
        job_id=job_id,
        receipt_id=receipt_id,
        scope_kind="RUN",
        run_id=run_id,
        deployment_id=None,
        kind="RUN_TOOL",
        requested_by="subject-1",
        request_id=request_id,
        retry_safety=RetrySafety.IDEMPOTENT,
        idempotency_key=None,
        now=NOW,
    )


def create_run(
    db: Path,
    jobs: JobStore,
    declaration: ToolFunctionSpec,
    *,
    tool_run_id: str = "tool-run-1",
    attempt_id: str = "attempt-1",
    job_id: str = "job-1",
    request_id: str = "request-1",
) -> ToolRunStore:
    store = ToolRunStore(db, jobs)
    store.create(
        tool_run_id=tool_run_id,
        run_id="run-1",
        research_revision=3,
        contract_version=1,
        request_id=request_id,
        requested_by="subject-1",
        invocation=invocation(declaration),
        attempt_id=attempt_id,
        job_id=job_id,
        now=NOW,
    )
    return store


def public_receipt(
    *,
    status: str = "SUCCEEDED",
    tool_run_id: str = "tool-run-1",
    attempt_id: str = "attempt-1",
) -> ToolReceipt:
    return ToolReceipt(
        tool_run_id=tool_run_id,
        attempt_id=attempt_id,
        status=status,
        payload={
            "exit_code": 0 if status == "SUCCEEDED" else 2,
            "resource_usage": {
                "cpu_millis": 800,
                "memory_peak_bytes": 900_000,
                "wall_time_ms": 1_200,
                "gpu_millis": 0,
            },
            "public_log_artifact_id": "log-1",
            "failure_code": "CHECK_FAILED" if status == "FAILED" else None,
            "public_summary": "public verifier output",
        },
        artifact_ids=("log-1", "proof-output-1"),
    )


def test_catalog_is_stable_paginated_and_persistent(tmp_path: Path) -> None:
    db = migrated_db(tmp_path)
    catalog = ToolCatalogStore(db)
    declaration = spec()
    assert catalog.register(declaration, now=NOW) == declaration
    assert ToolCatalogStore(db).get(*declaration.key) == declaration
    assert catalog.list(limit=1) == (declaration,)
    assert catalog.list(after=declaration.key) == ()
    with pytest.raises(ToolCatalogConflict):
        catalog.register(spec(profile="different-profile"), now=NOW)
    unavailable = catalog.set_availability(
        declaration.key, ToolAvailability.EXTERNAL_BLOCKED, now=NOW
    )
    assert unavailable.availability == ToolAvailability.EXTERNAL_BLOCKED


def test_structured_arguments_and_authority_ceiling_are_strict() -> None:
    declaration = spec(ceiling=AuthorityCeiling.SOFT_TOOL_RESULT)
    prepared = invocation(declaration, arguments={"n": 8})
    assert prepared.arguments["n"] == 8
    with pytest.raises(ToolContractError, match="failed schema"):
        invocation(declaration, arguments={"n": 0})
    with pytest.raises(ToolContractError, match="exceeds"):
        invocation(
            declaration,
            ceiling=AuthorityCeiling.CERTIFICATE_REQUIRES_VALIDATION,
        )


def test_run_creation_is_restart_safe_idempotent_and_transactional(tmp_path: Path) -> None:
    db = migrated_db(tmp_path)
    declaration = ToolCatalogStore(db).register(spec(), now=NOW)
    jobs = JobStore(db, iter(["unused"]).__next__)
    enqueue(jobs, db)
    store = create_run(db, jobs, declaration)
    first = store.get("tool-run-1")
    assert first.invocation_status == JobState.QUEUED
    assert first.validation_status == ValidationStatus.NOT_SUBMITTED
    create_run(db, jobs, declaration)
    assert ToolRunStore(db, jobs).get("tool-run-1") == first
    with pytest.raises(ToolRunConflict):
        store.create(
            tool_run_id="different-id",
            run_id="run-1",
            research_revision=3,
            contract_version=1,
            request_id="request-1",
            requested_by="subject-1",
            invocation=invocation(declaration),
            attempt_id="attempt-1",
            job_id="job-1",
            now=NOW,
        )
    enqueue(
        jobs,
        db,
        job_id="job-rollback",
        receipt_id="receipt-rollback",
        request_id="request-rollback",
    )
    with sqlite3.connect(db, isolation_level=None) as connection:
        connection.execute("BEGIN IMMEDIATE")
        store.create_in_transaction(
            connection,
            tool_run_id="rolled-back",
            run_id="run-1",
            research_revision=3,
            contract_version=1,
            request_id="request-rollback",
            requested_by="subject-1",
            invocation=invocation(declaration),
            attempt_id="attempt-rollback",
            job_id="job-rollback",
            now=NOW,
        )
        connection.rollback()
    with pytest.raises(KeyError):
        store.get("rolled-back")


def test_success_receipt_and_validation_are_strictly_separate(tmp_path: Path) -> None:
    db = migrated_db(tmp_path)
    declaration = ToolCatalogStore(db).register(spec(), now=NOW)
    jobs = JobStore(db, iter(["lease-1", "execution-1"]).__next__)
    enqueue(jobs, db)
    store = create_run(db, jobs, declaration)
    claimed = jobs.claim_next(
        holder_id="daemon-1",
        process_token="process-1",
        now="2026-08-13T00:00:01Z",
        expires_at="2026-08-13T00:01:00Z",
    )
    assert claimed is not None
    _, lease = claimed
    jobs.record_execution(
        lease,
        ExecutionReceipt(
            ExecutionOutcome.SUCCEEDED,
            0,
            ({"artifact_id": "proof-output-1"},),
        ),
        now="2026-08-13T00:00:02Z",
    )
    registry = ExtensionRegistry().register_tool_receipt_consumer(
        "b12a", ToolReceiptAdapter(store, lambda: "2026-08-13T00:00:03Z")
    )
    registry.consume_tool_receipt("b12a", public_receipt())
    completed = store.get("tool-run-1")
    assert completed.invocation_status == JobState.SUCCEEDED
    assert completed.validation_status == ValidationStatus.NOT_SUBMITTED
    attempt = store.attempts("tool-run-1")[0]
    assert attempt.authority_effect == "NONE"
    assert attempt.public_log_artifact_id == "log-1"
    assert attempt.output_artifact_ids == ("log-1", "proof-output-1")
    assert attempt.resource_usage is not None
    assert (
        ToolCatalogStore(db).get(*declaration.key).availability
        == ToolAvailability.PRODUCT_RECEIPT_AVAILABLE
    )
    accepted = store.record_validation(
        "tool-run-1",
        status=ValidationStatus.VALIDATION_ACCEPTED,
        validation_receipt_id="validation-1",
        now="2026-08-13T00:00:04Z",
    )
    assert accepted.invocation_status == JobState.SUCCEEDED
    assert accepted.validation_status == ValidationStatus.VALIDATION_ACCEPTED
    assert accepted.validation_receipt_id == "validation-1"
    assert not hasattr(store, "write_graph")


def test_failed_receipt_replay_is_exact_and_private_fields_are_rejected(
    tmp_path: Path,
) -> None:
    db = migrated_db(tmp_path)
    declaration = ToolCatalogStore(db).register(spec(), now=NOW)
    jobs = JobStore(db, iter(["lease-1", "execution-1"]).__next__)
    enqueue(jobs, db)
    store = create_run(db, jobs, declaration)
    claimed = jobs.claim_next(
        holder_id="daemon-1",
        process_token="process-1",
        now="2026-08-13T00:00:01Z",
        expires_at="2026-08-13T00:01:00Z",
    )
    assert claimed is not None
    _, lease = claimed
    jobs.record_execution(
        lease,
        ExecutionReceipt(
            ExecutionOutcome.FAILED,
            2,
            (),
            failure_code="CHECK_FAILED",
        ),
        now="2026-08-13T00:00:02Z",
    )
    receipt = public_receipt(status="FAILED")
    first = store.record_receipt(receipt, now="2026-08-13T00:00:03Z")
    replay = store.record_receipt(receipt, now="2026-08-13T00:00:04Z")
    assert first.invocation_status == JobState.FAILED
    assert replay.invocation_status == JobState.FAILED
    changed = ToolReceipt(
        receipt.tool_run_id,
        receipt.attempt_id,
        receipt.status,
        {**receipt.payload, "public_summary": "changed"},
        receipt.artifact_ids,
    )
    with pytest.raises(ToolRunConflict):
        store.record_receipt(changed, now="2026-08-13T00:00:05Z")
    private = ToolReceipt(
        receipt.tool_run_id,
        receipt.attempt_id,
        receipt.status,
        {**receipt.payload, "reasoning": "hidden chain"},
        receipt.artifact_ids,
    )
    with pytest.raises(ToolRunError, match="exact public"):
        store.record_receipt(private, now="2026-08-13T00:00:05Z")


def test_cancel_requested_waits_for_process_receipt(tmp_path: Path) -> None:
    db = migrated_db(tmp_path)
    declaration = ToolCatalogStore(db).register(spec(), now=NOW)
    jobs = JobStore(db, iter(["lease-1", "execution-1"]).__next__)
    enqueue(jobs, db)
    store = create_run(db, jobs, declaration)
    claimed = jobs.claim_next(
        holder_id="daemon-1",
        process_token="process-1",
        now="2026-08-13T00:00:01Z",
        expires_at="2026-08-13T00:01:00Z",
    )
    assert claimed is not None
    _, lease = claimed
    requested = store.request_cancel("tool-run-1", now="2026-08-13T00:00:02Z")
    assert requested.invocation_status == JobState.CANCEL_REQUESTED
    assert jobs.get("job-1").state == JobState.CANCEL_REQUESTED
    jobs.record_execution(
        lease,
        ExecutionReceipt(ExecutionOutcome.CANCELLED, None, ()),
        now="2026-08-13T00:00:03Z",
    )
    cancelled = ToolReceipt(
        tool_run_id="tool-run-1",
        attempt_id="attempt-1",
        status="CANCELLED",
        payload={
            "exit_code": None,
            "resource_usage": {
                "cpu_millis": 10,
                "memory_peak_bytes": 20,
                "wall_time_ms": 30,
                "gpu_millis": 0,
            },
            "public_log_artifact_id": None,
            "failure_code": None,
            "public_summary": "cancel acknowledged",
        },
        artifact_ids=(),
    )
    recorded = store.record_receipt(cancelled, now="2026-08-13T00:00:04Z")
    assert recorded.invocation_status == JobState.CANCELLED


def test_queued_cancel_is_immediate_but_not_a_success(tmp_path: Path) -> None:
    db = migrated_db(tmp_path)
    declaration = ToolCatalogStore(db).register(spec(), now=NOW)
    jobs = JobStore(db, iter(["unused"]).__next__)
    enqueue(jobs, db)
    store = create_run(db, jobs, declaration)
    cancelled = store.request_cancel("tool-run-1", now="2026-08-13T00:00:01Z")
    assert cancelled.invocation_status == JobState.CANCELLED
    assert cancelled.validation_status == ValidationStatus.NOT_SUBMITTED


def test_rerun_retains_attempt_history_and_compare_has_no_winner(tmp_path: Path) -> None:
    db = migrated_db(tmp_path)
    declaration = ToolCatalogStore(db).register(spec(), now=NOW)
    jobs = JobStore(db, iter(["lease-1", "execution-1", "unused"]).__next__)
    enqueue(jobs, db)
    store = create_run(db, jobs, declaration)
    claimed = jobs.claim_next(
        holder_id="daemon-1",
        process_token="process-1",
        now="2026-08-13T00:00:01Z",
        expires_at="2026-08-13T00:01:00Z",
    )
    assert claimed is not None
    _, lease = claimed
    jobs.record_execution(
        lease,
        ExecutionReceipt(ExecutionOutcome.FAILED, 2, (), "CHECK_FAILED"),
        now="2026-08-13T00:00:02Z",
    )
    store.record_receipt(public_receipt(status="FAILED"), now="2026-08-13T00:00:03Z")
    enqueue(
        jobs,
        db,
        job_id="job-2",
        receipt_id="receipt-2",
        request_id="request-2",
    )
    rerun = store.rerun(
        "tool-run-1",
        attempt_id="attempt-2",
        job_id="job-2",
        now="2026-08-13T00:00:04Z",
    )
    assert rerun.invocation_status == JobState.QUEUED
    assert [item.attempt_ordinal for item in store.attempts("tool-run-1")] == [1, 2]

    pending(db, receipt_id="receipt-3", job_id="job-3", request_id="request-3")
    jobs.enqueue(
        job_id="job-3",
        receipt_id="receipt-3",
        scope_kind="RUN",
        run_id="run-1",
        deployment_id=None,
        kind="RUN_TOOL",
        requested_by="subject-1",
        request_id="request-3",
        retry_safety=RetrySafety.IDEMPOTENT,
        idempotency_key=None,
        now=NOW,
    )
    store.create(
        tool_run_id="tool-run-2",
        run_id="run-1",
        research_revision=3,
        contract_version=1,
        request_id="request-3",
        requested_by="subject-1",
        invocation=invocation(declaration),
        attempt_id="attempt-3",
        job_id="job-3",
        now=NOW,
    )
    comparison = ToolRunStore(db, jobs).compare(("tool-run-1", "tool-run-2"))
    assert comparison.same_tool_function is True
    assert len(comparison.rows) == 2
    assert not hasattr(comparison, "winner")


def test_soft_result_can_succeed_but_cannot_be_validation_accepted(tmp_path: Path) -> None:
    db = migrated_db(tmp_path)
    declaration = ToolCatalogStore(db).register(
        spec(ceiling=AuthorityCeiling.SOFT_TOOL_RESULT), now=NOW
    )
    jobs = JobStore(db, iter(["lease-1", "execution-1"]).__next__)
    enqueue(jobs, db)
    store = create_run(db, jobs, declaration)
    claimed = jobs.claim_next(
        holder_id="daemon-1",
        process_token="process-1",
        now="2026-08-13T00:00:01Z",
        expires_at="2026-08-13T00:01:00Z",
    )
    assert claimed is not None
    jobs.record_execution(
        claimed[1],
        ExecutionReceipt(ExecutionOutcome.SUCCEEDED, 0, ()),
        now="2026-08-13T00:00:02Z",
    )
    store.record_receipt(public_receipt(), now="2026-08-13T00:00:03Z")
    with pytest.raises(ToolRunError, match="cannot produce"):
        store.record_validation(
            "tool-run-1",
            status=ValidationStatus.VALIDATION_ACCEPTED,
            validation_receipt_id="validation-1",
            now="2026-08-13T00:00:04Z",
        )
