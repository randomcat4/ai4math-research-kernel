from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType

import pytest

from rk.product.api import ProductCommand, ProductDecision, ProductSession, RunScope
from rk.product.command_service import (
    CommandPlan,
    ExecutionClass,
    ProductCommandService,
)
from rk.product.operations import OperationConflict, OperationStore
from rk.product_migrations import ProductMigrationAssembler, ProductMigrationRegistry


def store(tmp_path: Path) -> OperationStore:
    db = tmp_path / "product.sqlite"
    connection = sqlite3.connect(db)
    ProductMigrationAssembler(ProductMigrationRegistry(Path("schema_fragments"))).apply(connection)
    connection.close()
    ids = iter(("receipt-1", "receipt-2", "receipt-3"))
    return OperationStore(db, ids.__next__)


@dataclass
class Authority:
    calls: int = 0

    def apply(self, session: ProductSession, request: ProductCommand) -> ProductDecision:
        self.calls += 1
        return ProductDecision(
            True,
            request.scope.expected_revision,  # type: ignore[union-attr]
            request.scope.expected_revision + 1,  # type: ignore[union-attr]
            request.scope.expected_contract_version,  # type: ignore[union-attr]
            9,
            kernel_receipts=(MappingProxyType({"command_id": "kernel-1"}),),
        )


@dataclass
class Jobs:
    calls: int = 0
    job_id: str | None = None

    def enqueue_in_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        job_id: str,
        session: ProductSession,
        request: ProductCommand,
        receipt_id: str,
    ) -> None:
        self.calls += 1
        assert connection.in_transaction
        self.job_id = job_id


def service(tmp_path: Path, authority: Authority, jobs: Jobs) -> ProductCommandService:
    job_ids = iter(("job-1", "job-2", "job-3"))
    times = iter(
        (
            "2026-08-13T00:00:00Z",
            "2026-08-13T00:00:01Z",
            "2026-08-13T00:00:02Z",
            "2026-08-13T00:00:03Z",
        )
    )
    return ProductCommandService(
        operations=store(tmp_path),
        authority=authority,
        jobs=jobs,
        plans={
            "CONFIRM_CONTRACT": CommandPlan(ExecutionClass.SYNCHRONOUS_AUTHORITY),
            "RUN_TOOL": CommandPlan(ExecutionClass.DURABLE_JOB, "TOOL_RUN"),
        },
        id_generator=job_ids.__next__,
        clock=times.__next__,
    )


def request(command_type: str, payload: dict[str, object] | None = None) -> ProductCommand:
    return ProductCommand(
        "request-1",
        RunScope("run-1", 3, 1),
        command_type,
        MappingProxyType(payload or {}),
    )


def test_synchronous_authority_command_is_reserved_then_decided_once(tmp_path: Path) -> None:
    authority = Authority()
    jobs = Jobs()
    commands = service(tmp_path, authority, jobs)
    session = ProductSession("session-1", "subject-1", ("cap-1",))
    command = request("CONFIRM_CONTRACT")

    first = commands.execute(session, command)
    second = commands.execute(session, command)

    assert first.state == "DECIDED"
    assert first.receipt_version == 2
    assert second.receipt_id == first.receipt_id
    assert second.decision == first.decision
    assert authority.calls == 1
    assert jobs.calls == 0


def test_durable_command_is_enqueued_once_after_reservation(tmp_path: Path) -> None:
    authority = Authority()
    jobs = Jobs()
    commands = service(tmp_path, authority, jobs)
    session = ProductSession("session-1", "subject-1", ("cap-1",))
    command = request("RUN_TOOL")

    first = commands.execute(session, command)
    second = commands.execute(session, command)

    assert first.state == "PENDING"
    assert second.receipt_id == first.receipt_id
    assert jobs.calls == 1
    assert jobs.job_id == first.job_id
    assert authority.calls == 0


def test_request_id_reuse_with_changed_payload_conflicts(tmp_path: Path) -> None:
    commands = service(tmp_path, Authority(), Jobs())
    session = ProductSession("session-1", "subject-1", ("cap-1",))
    commands.execute(session, request("RUN_TOOL", {"value": 1}))

    with pytest.raises(OperationConflict):
        commands.execute(session, request("RUN_TOOL", {"value": 2}))


def test_failed_job_handoff_rolls_back_pending_receipt(tmp_path: Path) -> None:
    class FailingJobs(Jobs):
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
            raise RuntimeError("handoff failed")

    commands = service(tmp_path, Authority(), FailingJobs())
    session = ProductSession("session-1", "subject-1", ("cap-1",))
    with pytest.raises(RuntimeError, match="handoff failed"):
        commands.execute(session, request("RUN_TOOL"))

    with sqlite3.connect(tmp_path / "product.sqlite") as connection:
        assert connection.execute("SELECT COUNT(*) FROM product_receipts").fetchone() == (0,)
        assert connection.execute("SELECT COUNT(*) FROM product_jobs").fetchone() == (0,)
