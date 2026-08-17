from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import cast

import pytest

from rk.extensions import ExtensionRegistry
from rk.product.api import ProductCommand, ProductDecision, ProductSession
from rk.product.authority import ProductAuthority
from rk.product.budgeting import (
    BudgetBackpressure,
    BudgetControlError,
    BudgetDemand,
    BudgetEvent,
    BudgetEventKind,
    BudgetSubmission,
    KernelBudgetFence,
    KernelBudgetSnapshot,
    MeasuredBudgetAuthority,
    budget_kernel_binding,
)
from rk.product.deployment import (
    DeploymentHealthService,
    DeploymentProbeConfig,
    ProbeStatus,
)
from rk.product.placement import (
    ExecutionOutcome,
    ExecutionTarget,
    ExecutorKind,
    HardwareProfile,
    PlacementError,
    PlacementExecutionReceipt,
    PlacementPlanner,
    ProductSchedulingStore,
    TargetAvailability,
    WorkRequirement,
    execution_intervals_overlap,
    target_from_deployment_probe,
)
from rk.product_migrations import ProductMigrationAssembler, ProductMigrationRegistry

NOW = "2026-08-14T00:00:00Z"
GIB = 1024**3


def database(tmp_path: Path) -> Path:
    path = tmp_path / "scheduling.sqlite"
    with sqlite3.connect(path, isolation_level=None) as connection:
        ProductMigrationAssembler(ProductMigrationRegistry(Path("schema_fragments"))).apply(
            connection
        )
    return path


def server_hardware() -> HardwareProfile:
    return HardwareProfile(
        profile_id="injected-server-profile",
        os_family="linux",
        system_memory_bytes=192 * GIB,
        targets=(
            ExecutionTarget(
                "server-cpu",
                ExecutorKind.CPU,
                "server-cpu-workers",
                8,
                160 * GIB,
                availability=TargetAvailability.AVAILABLE,
                probe_receipt_id="server-probe-receipt",
                availability_fault=None,
                assets=frozenset({"verifier", "sympy", "z3"}),
            ),
            ExecutionTarget(
                "server-amd",
                ExecutorKind.ROCM,
                "server-amd-device-0",
                1,
                48 * GIB,
                availability=TargetAvailability.AVAILABLE,
                probe_receipt_id="server-probe-receipt",
                availability_fault=None,
                assets=frozenset({"prover-model", "reranker"}),
            ),
            ExecutionTarget(
                "deepseek-api",
                ExecutorKind.API,
                "deepseek-api-quota",
                4,
                0,
                provider="deepseek",
                availability=TargetAvailability.AVAILABLE,
                probe_receipt_id="server-probe-receipt",
                availability_fault=None,
            ),
        ),
    )


def desktop_hardware() -> HardwareProfile:
    return HardwareProfile(
        profile_id="injected-desktop-profile",
        os_family="windows",
        system_memory_bytes=64 * GIB,
        targets=(
            ExecutionTarget(
                "desktop-cpu",
                ExecutorKind.CPU,
                "desktop-cpu-workers",
                3,
                48 * GIB,
                availability=TargetAvailability.AVAILABLE,
                probe_receipt_id="desktop-probe-receipt",
                availability_fault=None,
                assets=frozenset({"verifier"}),
            ),
            ExecutionTarget(
                "openai-api",
                ExecutorKind.API,
                "openai-api-quota",
                2,
                0,
                provider="openai",
                availability=TargetAvailability.AVAILABLE,
                probe_receipt_id="desktop-probe-receipt",
                availability_fault=None,
            ),
        ),
    )


def cpu_requirement(work_item_id: str, *, top_k: int = 40) -> WorkRequirement:
    return WorkRequirement(
        work_item_id,
        "exact-verification",
        (ExecutorKind.CPU,),
        GIB,
        None,
        None,
        top_k,
        False,
        True,
    )


def execution_receipt(
    work_item_id: str,
    target_id: str,
    *,
    outcome: ExecutionOutcome = ExecutionOutcome.SUCCEEDED,
    started_at: str = "2026-08-14T00:00:02Z",
    finished_at: str = "2026-08-14T00:00:03Z",
    started_ns: int = 10,
    finished_ns: int = 20,
    failure_code: str | None = None,
    exit_code: int | None = 0,
) -> PlacementExecutionReceipt:
    return PlacementExecutionReceipt(
        work_item_id=work_item_id,
        target_id=target_id,
        outcome=outcome,
        started_at=started_at,
        finished_at=finished_at,
        started_monotonic_ns=started_ns,
        finished_monotonic_ns=finished_ns,
        exit_code=exit_code,
        failure_code=failure_code,
        receipt_artifact_id=f"receipt-{work_item_id}",
    )


def test_injected_server_desktop_and_s00_preserve_exact_quality_contract() -> None:
    server = PlacementPlanner(server_hardware())
    request = WorkRequirement(
        "research-1",
        "retrieval-rerank",
        (ExecutorKind.ROCM,),
        32 * GIB,
        "prover-model",
        None,
        137,
        True,
        False,
    )
    registry = server.register(ExtensionRegistry())
    result = registry.place("b13-research", request.to_dict())
    assert result == server.place(request).to_dict()
    assert result["target_id"] == "server-amd"
    assert result["retrieval_top_k"] == 137
    assert result["rerank_required"] is True
    assert result["fallback_reason"] is None

    desktop = PlacementPlanner(desktop_hardware())
    decision = desktop.place(cpu_requirement("desktop-proof", top_k=61))
    assert decision.target_id == "desktop-cpu"
    assert decision.retrieval_top_k == 61
    assert decision.verifier_required is True


def test_placement_rejects_quality_downgrade_provider_borrow_and_oom_fallback() -> None:
    planner = PlacementPlanner(server_hardware())
    with pytest.raises(PlacementError, match="exact requirement"):
        planner.place(
            WorkRequirement(
                "missing-reranker",
                "retrieval-rerank",
                (ExecutorKind.CPU,),
                GIB,
                None,
                None,
                100,
                True,
                False,
            )
        )
    with pytest.raises(PlacementError, match="exact requirement"):
        planner.place(
            WorkRequirement(
                "wrong-provider",
                "remote-model",
                (ExecutorKind.API,),
                0,
                None,
                "openai",
                20,
                False,
                False,
            )
        )
    with pytest.raises(PlacementError, match="exact requirement"):
        planner.place(
            WorkRequirement(
                "too-large",
                "gpu-proof",
                (ExecutorKind.ROCM, ExecutorKind.CPU),
                170 * GIB,
                None,
                None,
                20,
                False,
                False,
            )
        )


def test_fifty_nonterminal_items_obey_capacity_and_promote_in_stable_order(
    tmp_path: Path,
) -> None:
    path = database(tmp_path)
    store = ProductSchedulingStore(path)
    hardware = server_hardware()
    requirements = tuple(cpu_requirement(f"work-{ordinal:02d}") for ordinal in range(1, 51))
    digest = store.create_plan(
        schedule_plan_id="plan-50",
        run_id="run-50",
        hardware=hardware,
        requirements=requirements,
        planner=PlacementPlanner(hardware),
        created_at=NOW,
    )
    assert len(digest) == 64
    assert store.nonterminal_count("plan-50") == 50
    store.start("plan-50", now="2026-08-14T00:00:01Z")
    first = store.claim_ready("plan-50", started_at="2026-08-14T00:00:02Z")
    assert [item.work_item_id for item in first] == [f"work-{i:02d}" for i in range(1, 9)]

    store.finish(execution_receipt("work-02", "server-cpu"))
    assert store.claim_next_promotion("plan-50", claimed_at="2026-08-14T00:00:04Z") is None
    store.finish(execution_receipt("work-01", "server-cpu", finished_at="2026-08-14T00:00:05Z"))
    promoted = store.claim_next_promotion("plan-50", claimed_at="2026-08-14T00:00:06Z")
    assert promoted is not None and promoted.work_item_id == "work-01"
    store.mark_promoted("work-01", promoted_at="2026-08-14T00:00:07Z")
    promoted = store.claim_next_promotion("plan-50", claimed_at="2026-08-14T00:00:08Z")
    assert promoted is not None and promoted.work_item_id == "work-02"

    replacements = store.claim_ready("plan-50", started_at="2026-08-14T00:00:09Z")
    assert [item.work_item_id for item in replacements] == ["work-09", "work-10"]
    assert store.get("work-01").started_at == store.get("work-02").started_at
    assert store.get("work-01").finished_at != store.get("work-02").finished_at


def test_budget_pause_survives_restart_and_resumes_without_a_second_ledger(
    tmp_path: Path,
) -> None:
    path = database(tmp_path)
    hardware = server_hardware()
    store = ProductSchedulingStore(path)
    store.create_plan(
        schedule_plan_id="budget-plan",
        run_id="budget-run",
        hardware=hardware,
        requirements=(cpu_requirement("budget-work"),),
        planner=PlacementPlanner(hardware),
        created_at=NOW,
    )
    store.start("budget-plan", now="2026-08-14T00:01:00Z")
    demand = BudgetDemand({"usd": 600}, "deepseek", requires_known_cost=True)
    shortage = BudgetBackpressure.evaluate(
        KernelBudgetSnapshot({"usd": 500}, False, frozenset()), demand
    )
    assert shortage.reason == "BUDGET_INSUFFICIENT"
    assert shortage.shortages == {"usd": 100}
    store.apply_backpressure("budget-plan", shortage, now="2026-08-14T00:01:01Z")
    assert ProductSchedulingStore(path).claim_ready(
        "budget-plan", started_at="2026-08-14T00:01:02Z"
    ) == ()

    unknown = BudgetBackpressure.evaluate(
        KernelBudgetSnapshot({"usd": 900}, False, frozenset({"deepseek"})), demand
    )
    assert unknown.reason == "UNKNOWN_COST_REQUIRES_REVIEW"
    allowed = BudgetBackpressure.evaluate(
        KernelBudgetSnapshot({"usd": 900}, False, frozenset()), demand
    )
    restarted = ProductSchedulingStore(path)
    restarted.apply_backpressure("budget-plan", allowed, now="2026-08-14T00:01:03Z")
    assert [item.work_item_id for item in restarted.claim_ready(
        "budget-plan", started_at="2026-08-14T00:01:04Z"
    )] == ["budget-work"]

    with sqlite3.connect(path) as connection:
        schedule_columns = {
            str(row[1])
            for table in ("product_schedule_plans", "product_scheduled_work")
            for row in connection.execute(f"PRAGMA table_info({table})")
        }
        assert not {"reserved", "actual", "refund", "remaining"} & schedule_columns
        assert connection.execute(
            "SELECT hardware_profile_json FROM product_schedule_plans WHERE schedule_plan_id=?",
            ("budget-plan",),
        ).fetchone() == (json.dumps(hardware.to_dict(), sort_keys=True, separators=(",", ":")),)


def test_oom_is_terminal_failure_without_silent_replacement(tmp_path: Path) -> None:
    path = database(tmp_path)
    hardware = server_hardware()
    requirement = WorkRequirement(
        "gpu-work",
        "gpu-proof",
        (ExecutorKind.ROCM,),
        40 * GIB,
        "prover-model",
        None,
        80,
        True,
        False,
    )
    store = ProductSchedulingStore(path)
    store.create_plan(
        schedule_plan_id="oom-plan",
        run_id="oom-run",
        hardware=hardware,
        requirements=(requirement,),
        planner=PlacementPlanner(hardware),
        created_at=NOW,
    )
    store.start("oom-plan", now="2026-08-14T00:02:00Z")
    claimed = store.claim_ready(
        "oom-plan", started_at="2026-08-14T00:02:01Z"
    )
    assert claimed[0].placement.kind is ExecutorKind.ROCM
    failed = store.finish(
        execution_receipt(
            "gpu-work",
            "server-amd",
            outcome=ExecutionOutcome.FAILED,
            finished_at="2026-08-14T00:02:02Z",
            exit_code=137,
            failure_code="OOM",
        )
    )
    assert failed.state == "FAILED"
    assert failed.promotion_state == "NOT_ELIGIBLE"
    assert failed.placement.kind is ExecutorKind.ROCM
    assert store.nonterminal_count("oom-plan") == 0
    assert store.claim_ready("oom-plan", started_at="2026-08-14T00:02:03Z") == ()


def test_real_server_cpu_rocm_and_honest_api_share_one_run_receipt_chain(
    tmp_path: Path,
) -> None:
    rocm_smi = shutil.which("rocm-smi")
    assert rocm_smi is not None, "B13 server acceptance requires the configured ROCm probe"
    path = database(tmp_path)
    health = DeploymentHealthService(
        DeploymentProbeConfig(
            deployment_id="b13-real-server",
            db_path=path,
            rocm_probe_argv=(rocm_smi, "--showmeminfo", "vram", "--json"),
        ),
        lambda: NOW,
    ).probe()
    by_key = {item.capability_key: item for item in health.results}
    assert by_key["cpu"].status is ProbeStatus.AVAILABLE
    assert by_key["rocm"].status is ProbeStatus.AVAILABLE
    assert by_key["endpoints"].status is ProbeStatus.UNCONFIGURED

    measured = subprocess.run(
        (rocm_smi, "--showmeminfo", "vram", "--json"),
        capture_output=True,
        check=True,
        text=True,
    )
    vram_values = [
        int(value)
        for key, value in json.loads(measured.stdout)["card0"].items()
        if "Total Memory" in key
    ]
    assert len(vram_values) == 1 and vram_values[0] > 40 * GIB
    available_ram = int(by_key["ram"].public_details["available_bytes"])
    hardware = HardwareProfile(
        "measured-b13-server",
        "linux",
        int(by_key["ram"].public_details["total_bytes"]),
        (
            target_from_deployment_probe(
                health,
                capability_key="cpu",
                target_id="measured-cpu",
                kind=ExecutorKind.CPU,
                concurrency_group="measured-cpu-group",
                capacity=max(1, min(8, os.cpu_count() or 1)),
                memory_bytes=available_ram,
                assets=frozenset({"verifier"}),
            ),
            target_from_deployment_probe(
                health,
                capability_key="rocm",
                target_id="measured-rocm",
                kind=ExecutorKind.ROCM,
                concurrency_group="measured-rocm-device",
                capacity=1,
                memory_bytes=vram_values[0],
                assets=frozenset({"prover-model"}),
            ),
            target_from_deployment_probe(
                health,
                capability_key="endpoints",
                target_id="deepseek-api-unconfigured",
                kind=ExecutorKind.API,
                concurrency_group="deepseek-api",
                capacity=1,
                memory_bytes=0,
                provider="deepseek",
            ),
        ),
    )
    planner = PlacementPlanner(hardware)
    api_work = WorkRequirement(
        "api-work", "model", (ExecutorKind.API,), 0, None, "deepseek", 20, False, False
    )
    with pytest.raises(PlacementError, match="exact requirement"):
        planner.place(api_work)
    requirements = (
        cpu_requirement("real-cpu"),
        WorkRequirement(
            "real-rocm",
            "gpu-proof",
            (ExecutorKind.ROCM,),
            4 * GIB,
            "prover-model",
            None,
            40,
            False,
            False,
        ),
    )
    store = ProductSchedulingStore(path)
    store.create_plan(
        schedule_plan_id="real-plan",
        run_id="one-research-run",
        hardware=hardware,
        requirements=requirements,
        planner=planner,
        created_at=NOW,
    )
    store.start("real-plan", now=NOW)
    assert len(store.claim_ready("real-plan", started_at=NOW)) == 2

    cpu_argv = (
        sys.executable,
        "-c",
        "import hashlib; x=b'rk'; [hashlib.sha256(x).digest() for _ in range(500000)]",
    )
    cpu_started = time.monotonic_ns()
    cpu_process = subprocess.Popen(cpu_argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    rocm_started = time.monotonic_ns()
    rocm_process = subprocess.Popen(
        (rocm_smi, "--showmeminfo", "vram", "--json"),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    cpu_stdout, cpu_stderr = cpu_process.communicate(timeout=30)
    cpu_finished = time.monotonic_ns()
    rocm_stdout, rocm_stderr = rocm_process.communicate(timeout=30)
    rocm_finished = time.monotonic_ns()
    assert cpu_process.returncode == rocm_process.returncode == 0

    def real_receipt(
        work_item_id: str,
        target_id: str,
        started_ns: int,
        finished_ns: int,
        stdout: bytes,
        stderr: bytes,
    ) -> PlacementExecutionReceipt:
        artifact = hashlib.sha256(stdout + b"\0" + stderr).hexdigest()
        return PlacementExecutionReceipt(
            work_item_id,
            target_id,
            ExecutionOutcome.SUCCEEDED,
            NOW,
            NOW,
            started_ns,
            finished_ns,
            0,
            None,
            artifact,
        )

    cpu_receipt = real_receipt(
        "real-cpu", "measured-cpu", cpu_started, cpu_finished, cpu_stdout, cpu_stderr
    )
    rocm_receipt = real_receipt(
        "real-rocm", "measured-rocm", rocm_started, rocm_finished, rocm_stdout, rocm_stderr
    )
    assert execution_intervals_overlap(cpu_receipt, rocm_receipt)
    store.finish(rocm_receipt)
    assert store.claim_next_promotion("real-plan", claimed_at=NOW) is None
    store.finish(cpu_receipt)
    first = store.claim_next_promotion("real-plan", claimed_at=NOW)
    assert first is not None and first.work_item_id == "real-cpu"
    assert store.get("real-rocm").execution_receipt == rocm_receipt


@dataclass
class CapturingAuthority:
    accepted: bool = True
    commands: list[ProductCommand] = field(default_factory=list)

    def apply(self, session: ProductSession, request: ProductCommand) -> ProductDecision:
        self.commands.append(request)
        return ProductDecision(
            self.accepted,
            4,
            5,
            2,
            9,
            rejection_code=None if self.accepted else "DENIED",
        )


@dataclass
class CapturingMeasuredAuthority:
    calls: list[tuple[str, str, BudgetEvent]] = field(default_factory=list)

    def record_measured_budget(
        self,
        session: ProductSession,
        *,
        fence: KernelBudgetFence,
        request_id: str,
        event: BudgetEvent,
        host_receipt_artifact_id: str,
    ) -> ProductDecision:
        self.calls.append((request_id, host_receipt_artifact_id, event))
        return ProductDecision(True, fence.revision, fence.revision + 1, fence.contract_version, 10)


def test_budget_events_cross_authority_and_measured_usage_requires_host_receipt() -> None:
    authority = CapturingAuthority()
    measured = CapturingMeasuredAuthority()
    submission = BudgetSubmission(
        cast(ProductAuthority, authority), cast(MeasuredBudgetAuthority, measured)
    )
    session = ProductSession("session", "subject", ("budget-cap",))
    fence = KernelBudgetFence("run", 4, 2)
    reservation = BudgetEvent(
        BudgetEventKind.RESERVATION,
        "usd",
        800,
        "microusd",
        {"component": "deepseek", "input_tokens": 100},
        route_id="route-1",
    )
    submission.submit(session, fence=fence, request_id="reserve-1", event=reservation)
    assert authority.commands[0].command_type == "RECORD_BUDGET"
    assert authority.commands[0].scope.expected_revision == 4
    assert dict(authority.commands[0].payload) == reservation.payload()
    assert budget_kernel_binding().kernel_command_type == "RecordBudget"

    actual = BudgetEvent(
        BudgetEventKind.ACTUAL,
        "usd",
        730,
        "microusd",
        {"component": "deepseek", "output_tokens": 55},
        attempt_id="attempt-1",
    )
    with pytest.raises(BudgetControlError, match="host execution receipt"):
        submission.submit(session, fence=fence, request_id="actual-1", event=actual)
    submission.submit(
        session,
        fence=fence,
        request_id="actual-1",
        event=actual,
        host_receipt_artifact_id="artifact-host-receipt",
    )
    assert measured.calls == [("actual-1", "artifact-host-receipt", actual)]
    assert len(authority.commands) == 1

    rejected = CapturingAuthority(accepted=False)
    rejected_submission = BudgetSubmission(
        cast(ProductAuthority, rejected), cast(MeasuredBudgetAuthority, measured)
    )
    with pytest.raises(BudgetControlError, match="DENIED"):
        rejected_submission.submit(
            session, fence=fence, request_id="reserve-2", event=reservation
        )
