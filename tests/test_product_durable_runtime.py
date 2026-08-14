from __future__ import annotations

import json
import sqlite3
import urllib.error
import urllib.request
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
from rk.product.identity import IdentityStore, ProductRole
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


def test_production_daemon_login_create_list_and_overview_journey(tmp_path: Path) -> None:
    data_root = tmp_path / "runtime-http"
    deployment_id = "11111111-1111-4111-8111-111111111111"
    bootstrap_admin_session(
        data_root=data_root,
        schema_fragments=Path("schema_fragments"),
        deployment_id=deployment_id,
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
    identities = IdentityStore(data_root / "product.sqlite", lambda: b"m" * 16)
    identities.register(
        identity_id="identity-main",
        subject_id="main:one",
        display_name="Main",
        role=ProductRole.MAIN,
        capability_id="cap:main",
        login_secret="main-production-secret",
        now="2026-08-14T00:00:00Z",
    )
    identities.register(
        identity_id="identity-peer",
        subject_id="peer:one",
        display_name="Peer reviewer",
        role=ProductRole.PEER_REVIEWER,
        capability_id="cap:peer",
        login_secret="peer-production-secret",
        now="2026-08-14T00:00:00Z",
    )
    runtime = build_production_runtime(
        ProductionRuntimeConfig(
            data_root=data_root,
            deployment_id=deployment_id,
            organization_id="organization-1",
            port=0,
            max_upload_bytes=4096,
            max_chunk_bytes=1024,
            review_keys={
                "review-key": HmacAttestationKey(
                    secret=b"r" * 32,
                    verifier_identity_id="identity-peer",
                    trust_class=TrustClass.MANAGED_PEER_REVIEW,
                    authority_effect=AuthorityEffect.PEER_PROMOTION_ELIGIBLE,
                    promotion_eligible=True,
                )
            },
        )
    )
    runtime.daemon.start()
    try:
        host, port = runtime.daemon.address
        base = f"http://{host}:{port}"
        assert _http(base, "GET", "/healthz")[0] == 200
        assert _http(base, "GET", "/v1/meta")[0] == 200
        login_status, login, headers = _http(
            base,
            "POST",
            "/v1/session/login",
            {"identity_id": "identity-main", "login_secret": "main-production-secret"},
        )
        assert login_status == 200 and login["role"] == "MAIN"
        cookie = headers["set-cookie"].split(";", 1)[0]
        command: dict[str, object] = {
            "schema_version": "rk.product.command.v1",
            "request_id": "22222222-2222-4222-8222-222222222222",
            "scope": {"kind": "GLOBAL", "deployment_id": deployment_id},
            "command": {
                "type": "CREATE_RESEARCH",
                "payload": {
                    "question": "Prove that every even integer is divisible by two.",
                    "contract_draft": {
                        "objects": ["even integers"],
                        "domain": "elementary number theory",
                        "quantifiers": ["for every even integer n"],
                        "boundary_conditions": ["n is an integer"],
                        "exact_negation": "There exists an even integer not divisible by two.",
                        "allowed_tools": ["Lean"],
                        "success_conditions": ["NATURAL_LANGUAGE_PROOF"],
                    },
                    "owner": "main:one",
                    "labels": ["smoke"],
                    "initial_budget": {"microunits": 1000, "wall_seconds": 60},
                    "material_artifacts": [],
                },
            },
            "artifact_inputs": [],
        }
        create_status, created, _ = _http(
            base, "POST", "/v1/research", command, cookie=cookie
        )
        assert create_status == 200, created
        create_decision = created["decision"]
        assert isinstance(create_decision, dict) and create_decision["accepted"] is True
        list_status, listed, _ = _http(
            base,
            "GET",
            "/v1/research?limit=20&sort=RECENT_ACTIVITY_DESC",
            cookie=cookie,
        )
        listed_result = listed["result"]
        assert isinstance(listed_result, dict)
        items = listed_result["items"]
        assert isinstance(items, list) and len(items) == 1 and isinstance(items[0], dict)
        run_id = str(items[0]["run_id"])
        assert list_status == 200 and run_id in json.dumps(listed)
        overview_status, overview, _ = _http(
            base,
            "POST",
            f"/v1/research/{run_id}/queries",
            {
                "schema_version": "rk.product.query.v1",
                "scope": {"kind": "RUN", "run_id": run_id},
                "query": {"type": "RESEARCH_OVERVIEW", "payload": {}},
            },
            cookie=cookie,
        )
        assert overview_status == 200
        assert overview["result_type"] == "RESEARCH_OVERVIEW"
        assert overview["run_id"] == run_id

        peer_status, _peer, peer_headers = _http(
            base,
            "POST",
            "/v1/session/login",
            {"identity_id": "identity-peer", "login_secret": "peer-production-secret"},
        )
        assert peer_status == 200
        peer_cookie = peer_headers["set-cookie"].split(";", 1)[0]
        denied_status, denied, _ = _http(
            base,
            "POST",
            "/v1/research",
            {**command, "request_id": "33333333-3333-4333-8333-333333333333"},
            cookie=peer_cookie,
        )
        assert denied_status == 403 and denied["code"] == "COMMAND_FORBIDDEN"
    finally:
        runtime.daemon.stop()


def _http(
    base: str,
    method: str,
    path: str,
    body: dict[str, object] | None = None,
    *,
    cookie: str | None = None,
) -> tuple[int, dict[str, object], dict[str, str]]:
    headers = {"Accept": "application/json"}
    data = None
    if body is not None:
        data = json.dumps(body, separators=(",", ":")).encode()
        headers["Content-Type"] = "application/json"
    if cookie is not None:
        headers["Cookie"] = cookie
    request = urllib.request.Request(base + path, data=data, headers=headers, method=method)
    try:
        response = urllib.request.urlopen(request, timeout=10)
    except urllib.error.HTTPError as error:
        return (
            error.code,
            json.loads(error.read()),
            {key.lower(): value for key, value in error.headers.items()},
        )
    with response:
        return (
            response.status,
            json.loads(response.read()),
            {key.lower(): value for key, value in response.headers.items()},
        )
