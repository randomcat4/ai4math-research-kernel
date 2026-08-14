from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from types import MappingProxyType

import pytest

from rk.http.app import BootstrapAdmin, bootstrap_admin_session
from rk.http.production_runtime import ProductionRuntimeConfig, build_production_runtime
from rk.product.api import ProductCommand, ProductDecision, ProductSession, RunScope
from rk.product.attestation_import import (
    AuthorityEffect,
    HmacAttestationKey,
    TrustClass,
)
from rk.product.command_service import CommandPlan, ExecutionClass, ProductCommandService
from rk.product.durable_runtime import DurableJobResolver, TypedExecution
from rk.product.jobs import ExecutionOutcome, ExecutionReceipt, JobStore, JobStoreError, RetrySafety
from rk.product.operations import OperationStore
from rk.product.supervisor import RuntimeSupervisor
from rk.product_migrations import ProductMigrationAssembler, ProductMigrationRegistry


class Ids:
    def __init__(self, *values: str) -> None:
        self._values = iter(values)

    def __call__(self) -> str:
        return next(self._values)


class UnusedAuthority:
    def apply(self, session: ProductSession, request: ProductCommand) -> ProductDecision:
        raise AssertionError((session, request))


def test_request_execution_and_product_receipt_resolve_atomically(tmp_path: Path) -> None:
    db = tmp_path / "product.sqlite"
    with sqlite3.connect(db) as connection:
        ProductMigrationAssembler(ProductMigrationRegistry(Path("schema_fragments"))).apply(
            connection
        )
    jobs = JobStore(db, Ids("lease-1"))
    supervisor = RuntimeSupervisor(
        store=jobs,
        holder_id="daemon-1",
        clock=lambda: "2026-08-14T00:00:01Z",
        retry_policy={"RUN_TOOL": RetrySafety.MANUAL_ONLY},
    )
    commands = ProductCommandService(
        operations=OperationStore(db, Ids("receipt-1")),
        authority=UnusedAuthority(),
        jobs=supervisor,
        plans={"RUN_TOOL": CommandPlan(ExecutionClass.DURABLE_JOB, "RUN_TOOL")},
        id_generator=Ids("job-1"),
        clock=lambda: "2026-08-14T00:00:00Z",
    )
    request = ProductCommand(
        "request-1",
        RunScope("run-1", 3, 1),
        "RUN_TOOL",
        MappingProxyType({"tool_id": "tool-1"}),
    )
    receipt = commands.execute(ProductSession("session-1", "subject-1", ("capability-1",)), request)
    assert receipt.state == "PENDING"
    persisted = jobs.request("job-1")
    assert persisted.value["command"] == {"type": "RUN_TOOL", "payload": {"tool_id": "tool-1"}}

    claimed = supervisor.claim(process_token="process-1", expires_at="2026-08-14T00:01:00Z")
    assert claimed is not None
    _job, lease = claimed
    result = TypedExecution(
        ExecutionReceipt(
            ExecutionOutcome.FAILED,
            None,
            (),
            failure_code="RUN_TOOL_DEPENDENCY_UNAVAILABLE",
        ),
        ProductDecision(
            False,
            3,
            3,
            1,
            3,
            rejection_code="RUN_TOOL_DEPENDENCY_UNAVAILABLE",
        ),
    )
    resolver = DurableJobResolver(db, Ids("execution-1"))
    digest = resolver.resolve(lease, result, now="2026-08-14T00:00:02Z")
    assert resolver.resolve(lease, result, now="2026-08-14T00:00:03Z") == digest

    with sqlite3.connect(db) as connection:
        row = connection.execute(
            "SELECT state,receipt_version,receipt_json FROM product_receipts "
            "WHERE receipt_id='receipt-1'"
        ).fetchone()
        job = connection.execute(
            "SELECT state,failure_code FROM product_jobs WHERE job_id='job-1'"
        ).fetchone()
    assert row is not None and row[:2] == ("DECIDED", 2)
    body = json.loads(str(row[2]))
    assert body["decision"]["accepted"] is False
    assert job == ("FAILED", "RUN_TOOL_DEPENDENCY_UNAVAILABLE")

    conflicting = TypedExecution(
        ExecutionReceipt(ExecutionOutcome.SUCCEEDED, 0, ()),
        ProductDecision(True, 3, 3, 1, 3),
    )
    with pytest.raises(JobStoreError, match="replay conflicts"):
        resolver.resolve(lease, conflicting, now="2026-08-14T00:00:04Z")


def test_production_root_builds_complete_query_command_and_worker_graph(tmp_path: Path) -> None:
    data_root = tmp_path / "runtime"
    bootstrap_admin_session(
        data_root=data_root,
        schema_fragments=Path("schema_fragments"),
        deployment_id="deployment-1",
        organization_id="organization-1",
        limits={"upload_bytes": 4096, "graph_nodes": 200},
        admin=BootstrapAdmin(
            identity_id="identity-admin",
            subject_id="admin:one",
            display_name="Administrator",
            login_secret="administrator-login-secret",
        ),
        now="2026-08-14T00:00:00Z",
        expires_at="2026-08-15T00:00:00Z",
    )
    runtime = build_production_runtime(
        ProductionRuntimeConfig(
            data_root=data_root,
            deployment_id="deployment-1",
            organization_id="organization-1",
            port=0,
            max_upload_bytes=4096,
            max_chunk_bytes=1024,
            review_keys={
                "review-key": HmacAttestationKey(
                    secret=b"r" * 32,
                    verifier_identity_id="identity-admin",
                    trust_class=TrustClass.MANAGED_PEER_REVIEW,
                    authority_effect=AuthorityEffect.PEER_PROMOTION_ELIGIBLE,
                    promotion_eligible=True,
                )
            },
        )
    )
    assert runtime.job_pump is not None
    assert runtime.job_pump.kinds == {
        "START_RESEARCH",
        "RESUME_RESEARCH",
        "RUN_LITERATURE_QUERY",
        "REPLAY_SOURCE_SNAPSHOT",
        "BATCH_CREATE_RESEARCH",
        "ASSIGN_ABLATION",
        "IMPORT_RESEARCH_LINEAGE",
        "CREATE_COMPUTE_TASK",
        "RUN_TOOL",
        "GENERATE_CANDIDATE_TEX",
        "COMPILE_FINAL_PDF",
        "RETRY_UNKNOWN_OUTCOME",
        "DEPLOYMENT_OPERATION",
    }
