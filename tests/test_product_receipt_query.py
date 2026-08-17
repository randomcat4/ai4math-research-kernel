from __future__ import annotations

import sqlite3
from pathlib import Path
from types import MappingProxyType

import pytest

from rk.product.api import QuerySpec
from rk.product.jobs import JobStore, RetrySafety
from rk.product.operations import OperationStore
from rk.product.receipt_query import (
    ProductObjectNotFound,
    ProductQueryScopeMismatch,
    ReceiptJobQuery,
)
from rk.product_migrations import ProductMigrationAssembler, ProductMigrationRegistry


def _database(tmp_path: Path) -> Path:
    path = tmp_path / "product.sqlite"
    with sqlite3.connect(path, isolation_level=None) as connection:
        ProductMigrationAssembler(ProductMigrationRegistry(Path("schema_fragments"))).apply(connection)
    return path


def _reader(tmp_path: Path) -> tuple[Path, OperationStore, JobStore, ReceiptJobQuery]:
    path = _database(tmp_path)
    operations = OperationStore(path, iter(("receipt-1",)).__next__)
    jobs = JobStore(path, iter(("lease-1",)).__next__)
    return path, operations, jobs, ReceiptJobQuery(operations, jobs)


def _scope(run_id: str = "run-1") -> MappingProxyType[str, object]:
    return MappingProxyType({"kind": "RUN", "run_id": run_id})


def test_product_receipt_query_returns_latest_persisted_projection(tmp_path: Path) -> None:
    _, operations, _, reader = _reader(tmp_path)
    reservation = operations.reserve(
        scope_key="RUN:run-1",
        request_id="request-1",
        request_digest="a" * 64,
        pending_receipt={
            "schema_version": "rk.product.receipt.v1",
            "request_id": "request-1",
            "scope": {"kind": "RUN", "run_id": "run-1"},
            "updated_at": "2026-08-13T00:00:00Z",
            "state": "PENDING",
            "job_id": "job-1",
        },
        now="2026-08-13T00:00:00Z",
    )
    operations.decide(
        reservation.receipt.receipt_id,
        decision_receipt={
            "schema_version": "rk.product.receipt.v1",
            "request_id": "request-1",
            "scope": {"kind": "RUN", "run_id": "run-1"},
            "updated_at": "2026-08-13T00:00:01Z",
            "state": "DECIDED",
            "decision": {"accepted": False, "rejection_code": "COMPOSITION_OPEN"},
        },
        now="2026-08-13T00:00:01Z",
    )

    result = reader.execute(
        QuerySpec(_scope(), "PRODUCT_RECEIPT", MappingProxyType({"receipt_id": "receipt-1"}))
    )

    assert result.result_type == "PRODUCT_RECEIPT"
    assert result.stable_entity_id == "receipt-1"
    assert result.fence == {"receipt_version": 2, "updated_at": "2026-08-13T00:00:01Z"}
    assert result.data["state"] == "DECIDED"
    assert result.data["decision"] == {
        "accepted": False,
        "rejection_code": "COMPOSITION_OPEN",
    }


def test_receipt_query_rejects_scope_confusion_unknown_id_and_payload_extensions(
    tmp_path: Path,
) -> None:
    _, operations, _, reader = _reader(tmp_path)
    operations.reserve(
        scope_key="RUN:run-1",
        request_id="request-1",
        request_digest="a" * 64,
        pending_receipt={"state": "PENDING", "job_id": "job-1"},
        now="now",
    )

    with pytest.raises(ProductQueryScopeMismatch):
        reader.product_receipt(_scope("run-2"), "receipt-1")
    with pytest.raises(ProductObjectNotFound):
        reader.product_receipt(_scope(), "missing")
    with pytest.raises(ValueError, match="only receipt_id"):
        reader.execute(
            QuerySpec(
                _scope(),
                "PRODUCT_RECEIPT",
                MappingProxyType({"receipt_id": "receipt-1", "role": "ADMIN"}),
            )
        )


def test_job_query_reads_one_durable_job_without_deriving_authority(tmp_path: Path) -> None:
    _, operations, jobs, reader = _reader(tmp_path)
    receipt = operations.reserve(
        scope_key="RUN:run-1",
        request_id="request-1",
        request_digest="a" * 64,
        pending_receipt={"state": "PENDING", "job_id": "job-1"},
        now="2026-08-13T00:00:00Z",
    ).receipt
    jobs.enqueue(
        job_id="job-1",
        receipt_id=receipt.receipt_id,
        scope_kind="RUN",
        run_id="run-1",
        deployment_id=None,
        kind="MATLAS_QUERY",
        requested_by="subject-1",
        request_id="request-1",
        retry_safety=RetrySafety.READ_ONLY,
        idempotency_key=None,
        now="2026-08-13T00:00:00Z",
        worker_run_ids=("worker-1",),
    )
    assert jobs.claim_next(
        holder_id="daemon-1",
        process_token="process-1",
        now="2026-08-13T00:00:01Z",
        expires_at="2026-08-13T00:01:01Z",
    ) is not None

    result = reader.execute(QuerySpec(_scope(), "JOB", MappingProxyType({"job_id": "job-1"})))

    assert result.result_type == "JOB"
    assert result.fence == {"lease_generation": 1, "state": "RUNNING"}
    assert result.data["worker_run_ids"] == ["worker-1"]
    assert result.data["authority_effect"] == "NONE"
    with pytest.raises(ProductQueryScopeMismatch):
        reader.job(_scope("run-2"), "job-1")


def test_receipt_job_reader_has_no_mutating_operation() -> None:
    assert {name for name in dir(ReceiptJobQuery) if not name.startswith("_")} == {
        "execute",
        "job",
        "product_receipt",
    }