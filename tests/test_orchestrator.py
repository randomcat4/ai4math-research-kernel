from __future__ import annotations

import threading
import time
from collections import defaultdict
from collections.abc import Callable, Mapping
from dataclasses import replace
from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from rk.orchestrator import (
    BudgetPlan,
    ComponentRequest,
    ComponentResult,
    HardwarePlan,
    HumanReview,
    OrchestrationPlan,
    OrchestrationStatus,
    PauseReason,
    ResearchOrchestrator,
    ResearchOutcome,
    ResearchPhase,
    ToolRequest,
    WorkKind,
)


def test_lean_declaration_name_keeps_namespace_ownership() -> None:
    from rk.orchestrator import _lean_declaration_name

    assert (
        _lean_declaration_name(
            "namespace A\nnamespace B\ntheorem t : True := by trivial\nend B\nend A"
        )
        == "A.B.t"
    )

HASH_A = "a" * 64
HASH_B = "b" * 64


class FixedClock:
    def now(self) -> datetime:
        return datetime(2026, 8, 12, 12, 0, tzinfo=UTC)


class SequentialIds:
    def __init__(self) -> None:
        self.value = 0

    def __call__(self) -> str:
        self.value += 1
        return f"id-{self.value:04d}"


class ScriptedRuntime:
    def __init__(
        self,
        script: Mapping[str, Callable[[ComponentRequest, int], ComponentResult]],
    ) -> None:
        self.script = dict(script)
        self.calls: list[ComponentRequest] = []
        self.counts: defaultdict[str, int] = defaultdict(int)

    def execute(self, request: ComponentRequest) -> ComponentResult:
        self.calls.append(request)
        self.counts[request.work_kind] += 1
        return self.script[request.work_kind](request, self.counts[request.work_kind])


def result(payload: Mapping[str, Any] | None = None, *artifacts: str) -> ComponentResult:
    return ComponentResult(
        status="COMPLETED",
        payload=payload or {},
        artifact_ids=tuple(artifacts),
        usage={"input_tokens": 10, "output_tokens": 5, "wall_time_ms": 20},
    )


def plan(*, work_units: int = 100, route_revisions: int = 1) -> OrchestrationPlan:
    return OrchestrationPlan(
        run_id="run-1",
        contract_id="contract-1",
        contract_version=1,
        contract_hash=HASH_A,
        statement_hash=HASH_B,
        contract={"statement": "For every finite graph G, prove P(G)."},
        budget=BudgetPlan(work_units, 100_000, 100_000, 100_000),
        hardware=HardwarePlan("PORTABLE_CPU_API", "plan-1", {"model": "remote"}),
        minimum_routes=2,
        maximum_routes=3,
        max_route_revisions=route_revisions,
        max_composition_revisions=1,
        max_tool_cycles=1,
    )


def test_product_plan_detects_host_and_records_real_schedule() -> None:
    runtime = ScriptedRuntime({})
    orchestrator = ResearchOrchestrator(runtime)
    orchestrator._kernel = SimpleNamespace(  # type: ignore[assignment]
        inspect=lambda run_id: SimpleNamespace(
            projection={
                "contract": {
                    "contract_id": f"contract:{run_id}",
                    "contract_hash": HASH_A,
                    "statement_hash": HASH_B,
                    "contract": {"statement": "Prove a finite identity."},
                }
            },
            current_contract_version=1,
        )
    )
    orchestrator._config = SimpleNamespace(  # type: ignore[assignment]
        product={
            "hardware_plan": {
                "mode": "AUTO",
                "api_candidate_available": True,
                "public_retrieval_available": True,
            }
        }
    )

    detected = orchestrator._plan_for_run("run-hardware").hardware

    assert detected.mode != "AUTO"
    assert detected.plan_digest != HASH_A
    assert "_detected_hardware" in detected.placements
    assert detected.placements["_detected_hardware"]["final_replay_required"] is True


def complete_script(*, composition_closed: bool = True) -> dict[str, Callable[..., Any]]:
    return {
        WorkKind.CONTRACT_CLARIFY.value: lambda request, count: result({"ambiguous": False}),
        WorkKind.LITERATURE_AUDIT.value: lambda request, count: result(
            {"exact_match": False}, "literature"
        ),
        WorkKind.ROUTE_SCOUT.value: lambda request, count: result(
            {
                "routes": [
                    {
                        "route_id": "route-a",
                        "label": "minimal counterexample",
                        "representation": "graph reduction",
                        "tool_family": "enumeration",
                        "method_card_id": "MC_MINIMAL_COUNTEREXAMPLE",
                        "proof_skeleton": ["choose a minimum witness", "reduce it"],
                        "sharp_example": "a triangle",
                        "near_miss": "a path without the boundary condition",
                        "fast_falsifier": "enumerate graphs on at most five vertices",
                        "sentinel_result": {"status": "PASSED", "case_id": "N01"},
                        "independence_profile": {
                            "idea_family": "minimal-counterexample",
                            "derivation_family": "graph-reduction",
                            "verification_family": "exact-enumeration",
                            "implementation_family": "python-enumerator",
                            "retrieval_family": "graph-library",
                        },
                    },
                    {
                        "route_id": "route-b",
                        "label": "algebraic invariant",
                        "representation": "polynomial encoding",
                        "tool_family": "CAS",
                        "method_card_id": "MC_PARITY_INVARIANT",
                        "proof_skeleton": ["encode the invariant", "compare coefficients"],
                        "sharp_example": "the equality case",
                        "near_miss": "the same polynomial in characteristic two",
                        "fast_falsifier": "expand the smallest symbolic instance",
                        "sentinel_result": {"status": "PASSED", "case_id": "N02"},
                        "independence_profile": {
                            "idea_family": "algebraic-invariant",
                            "derivation_family": "coefficient-comparison",
                            "verification_family": "lean-ring",
                            "implementation_family": "sympy-cas",
                            "retrieval_family": "algebra-library",
                        },
                    },
                ]
            }
        ),
        WorkKind.FALSIFY_ROUTE.value: lambda request, count: result(
            {"falsified": False, "open_obligations": []}, f"falsify-{request.route_id}"
        ),
        WorkKind.DEVELOP_ROUTE.value: lambda request, count: result(
            {"candidate_kind": "PROOF", "open_obligations": []},
            f"proof-{request.route_id}",
        ),
        WorkKind.GAP_REVIEW.value: lambda request, count: result(
            {"verdict": "PASS", "open_obligations": []}, f"gap-{request.route_id}"
        ),
        WorkKind.FORMALIZE_ROUTE.value: lambda request, count: result(
            {"machine_verified": True, "open_obligations": []},
            f"lean-{request.route_id}",
        ),
        WorkKind.SEMANTIC_AUDIT.value: lambda request, count: result(
            {"faithful": True}, f"semantic-{request.route_id}"
        ),
        WorkKind.COMPOSITION_CHECK.value: lambda request, count: result(
            {
                "closed": composition_closed,
                "open_obligations": [] if composition_closed else ["glue.compatibility"],
            },
            "closure",
        ),
        WorkKind.REVISE_COMPOSITION.value: lambda request, count: result(
            {"open_obligations": ["glue.compatibility"]}, "closure-revision"
        ),
        WorkKind.FINAL_SYNTHESIS.value: lambda request, count: result(
            {"summary": "candidate only"}, "synthesis"
        ),
    }


def orchestrator(runtime: ScriptedRuntime) -> ResearchOrchestrator:
    return ResearchOrchestrator(runtime, clock=FixedClock(), id_factory=SequentialIds())


def test_full_research_loop_reaches_human_gate_then_synthesizes() -> None:
    runtime = ScriptedRuntime(complete_script())
    engine = orchestrator(runtime)

    waiting = engine.advance(engine.start(plan())).checkpoint

    assert waiting.status is OrchestrationStatus.WAITING_HUMAN_REVIEW
    assert waiting.phase is ResearchPhase.HUMAN_REVIEW
    assert waiting.composition_closed is True
    assert {route.status.value for route in waiting.routes} == {"READY"}
    work = [call.work_kind for call in runtime.calls]
    assert work.index(WorkKind.LITERATURE_AUDIT.value) < work.index(WorkKind.ROUTE_SCOUT.value)
    assert work.index(WorkKind.FALSIFY_ROUTE.value) < work.index(WorkKind.DEVELOP_ROUTE.value)
    assert work.count(WorkKind.FALSIFY_ROUTE.value) == 2
    assert work.count(WorkKind.GAP_REVIEW.value) == 2
    assert work.count(WorkKind.FORMALIZE_ROUTE.value) == 2
    assert work.count(WorkKind.SEMANTIC_AUDIT.value) == 2

    resumed = engine.resume(
        waiting,
        human_reviews=(HumanReview("review-1", "mathematician-1", "ACCEPTED", "review.md"),),
    )
    completed = engine.advance(resumed).checkpoint

    assert completed.status is OrchestrationStatus.COMPLETED
    assert completed.outcome == ResearchOutcome.UNRESOLVED.value
    assert completed.events[-1].event_type == "ORCHESTRATION_COMPLETED"
    assert completed.events[-1].payload["authority_ceiling"] == "SOFT_CANDIDATE_ONLY"


def test_tool_request_is_executed_and_feedback_returns_to_same_work_item() -> None:
    script = complete_script()

    def falsify(request: ComponentRequest, count: int) -> ComponentResult:
        if count == 1:
            return ComponentResult(
                status="COMPLETED",
                payload={"open_obligations": ["need-small-case-search"]},
                tool_requests=(
                    ToolRequest(
                        "small-cases",
                        "SMT",
                        "find-counterexample",
                        {"bound": 8},
                    ),
                ),
                usage={"input_tokens": 3, "output_tokens": 2, "wall_time_ms": 5},
            )
        if request.route_id == "route-a":
            assert request.inputs["tool_feedback"]["small-cases"]["status"] == "COMPLETED"
        return result({"falsified": False, "open_obligations": []}, "falsification")

    script[WorkKind.FALSIFY_ROUTE.value] = falsify
    script[WorkKind.TOOL_REQUEST.value] = lambda request, count: result(
        {"counterexample": None}, "smt-log"
    )
    runtime = ScriptedRuntime(script)
    engine = orchestrator(runtime)
    waiting = engine.advance(engine.start(replace(plan(), max_tool_cycles=2))).checkpoint

    assert waiting.status is OrchestrationStatus.WAITING_HUMAN_REVIEW
    assert runtime.counts[WorkKind.TOOL_REQUEST.value] == 1
    assert runtime.counts[WorkKind.FALSIFY_ROUTE.value] == 3
    event_types = [event.event_type for event in waiting.events]
    assert "TOOL_REQUESTS_QUEUED" in event_types
    assert "TOOL_FEEDBACK_ATTACHED" in event_types


def test_host_forces_configured_probe_when_model_omits_tool_request() -> None:
    script = complete_script()
    script[WorkKind.TOOL_REQUEST.value] = lambda request, count: result(
        {"negated_claim": "unsat"}, "smt-log"
    )
    runtime = ScriptedRuntime(script)
    engine = orchestrator(runtime)
    engine._config = SimpleNamespace(  # type: ignore[assignment]
        adapter_profiles={},
        product={
            "forced_tool_requests": {
                "FALSIFY_ROUTE": [
                    {
                        "request_key": "host-smt",
                        "tool": "research-smt",
                        "operation": "run_smt",
                        "payload": {
                            "input": {
                                "claim": "forall integer n, n + 0 = n",
                                "negate": True,
                            },
                            "expected": {"negated_claim": "unsat"},
                        },
                    }
                ]
            }
        }
    )

    waiting = engine.advance(engine.start(plan())).checkpoint

    assert waiting.status is OrchestrationStatus.WAITING_HUMAN_REVIEW
    assert runtime.counts[WorkKind.TOOL_REQUEST.value] == 2
    assert runtime.counts[WorkKind.FALSIFY_ROUTE.value] == 4
    requests = [
        call.inputs["tool_request"]
        for call in runtime.calls
        if call.work_kind == WorkKind.TOOL_REQUEST.value
    ]
    assert all(item["operation"] == "run_smt" for item in requests)


def test_awaiting_tool_with_registered_request_executes_instead_of_pausing() -> None:
    script = complete_script()

    def falsify(request: ComponentRequest, count: int) -> ComponentResult:
        if request.route_id == "route-a" and count == 1:
            return ComponentResult(
                "AWAITING_TOOL",
                tool_requests=(
                    ToolRequest("probe", "research-smt", "run_smt", {"case": 1}),
                ),
            )
        return result({"falsified": False, "open_obligations": []}, "falsification")

    script[WorkKind.FALSIFY_ROUTE.value] = falsify
    script[WorkKind.TOOL_REQUEST.value] = lambda request, count: result(
        {"negated_claim": "unsat"}, "smt-log"
    )
    runtime = ScriptedRuntime(script)
    engine = orchestrator(runtime)

    waiting = engine.advance(engine.start(plan())).checkpoint

    assert waiting.pause_reason != PauseReason.RUNTIME_UNAVAILABLE.value
    assert any(call.work_kind == WorkKind.TOOL_REQUEST.value for call in runtime.calls)


@pytest.mark.parametrize("status", ["TOOL_REQUEST", "INCOMPLETE", "REQUESTS_NEEDED"])
def test_controller_tool_status_executes_registered_request_before_status_gate(
    status: str,
) -> None:
    script = complete_script()

    def falsify(request: ComponentRequest, count: int) -> ComponentResult:
        if request.route_id == "route-a" and count == 1:
            return ComponentResult(
                status,
                tool_requests=(
                    ToolRequest("probe", "research-smt", "run_smt", {"case": 1}),
                ),
            )
        return result({"falsified": False, "open_obligations": []})

    script[WorkKind.FALSIFY_ROUTE.value] = falsify
    script[WorkKind.TOOL_REQUEST.value] = lambda request, count: result(
        {"negated_claim": "unsat"}
    )
    runtime = ScriptedRuntime(script)
    engine = orchestrator(runtime)
    waiting = engine.advance(engine.start(plan())).checkpoint
    assert waiting.pause_reason != PauseReason.RUNTIME_UNAVAILABLE.value
    assert runtime.counts[WorkKind.TOOL_REQUEST.value] == 1


def test_forced_probe_merges_with_model_requests_and_wins_duplicate_key() -> None:
    script = complete_script()

    def falsify(request: ComponentRequest, count: int) -> ComponentResult:
        if request.route_id == "route-a" and count == 1:
            return ComponentResult(
                "TOOL_REQUEST",
                tool_requests=(
                    ToolRequest("model", "research-cas", "run_cas", {"case": 1}),
                    ToolRequest("forced", "wrong", "wrong_operation", {}),
                ),
            )
        return result({"falsified": False, "open_obligations": []})

    script[WorkKind.FALSIFY_ROUTE.value] = falsify
    script[WorkKind.TOOL_REQUEST.value] = lambda request, count: result({"ok": True})
    runtime = ScriptedRuntime(script)
    engine = orchestrator(runtime)
    engine._config = SimpleNamespace(  # type: ignore[assignment]
        adapter_profiles={},
        product={
            "forced_tool_requests": {
                "FALSIFY_ROUTE": [
                    {
                        "request_key": "forced",
                        "tool": "research-smt",
                        "operation": "run_smt",
                        "payload": {"case": 2},
                    }
                ]
            }
        },
    )
    waiting = engine.advance(engine.start(plan())).checkpoint
    requests = [
        call.inputs["tool_request"]
        for call in runtime.calls
        if call.work_kind == WorkKind.TOOL_REQUEST.value and call.route_id == "route-a"
    ]
    assert [request["request_key"] for request in requests] == ["model", "forced"]
    assert requests[1]["operation"] == "run_smt"
    assert waiting.pause_reason != PauseReason.RUNTIME_UNAVAILABLE.value


def test_two_tool_rounds_keep_route_context_and_recover_failed_registered_call() -> None:
    script = complete_script()
    route_a_turn = 0

    def develop(request: ComponentRequest, count: int) -> ComponentResult:
        nonlocal route_a_turn
        del count
        if request.route_id != "route-a":
            return result({"candidate_kind": "PROOF", "open_obligations": []})
        assert request.route_id == "route-a"
        route_a_turn += 1
        if route_a_turn == 1:
            return ComponentResult(
                "COMPLETED",
                tool_requests=(
                    ToolRequest("expand", "research-cas", "run_cas", {"case": 1}),
                ),
            )
        if route_a_turn == 2:
            assert request.inputs["tool_feedback"]["expand"]["payload"]["expanded"] == "x+1"
            return ComponentResult(
                "COMPLETED",
                tool_requests=(
                    ToolRequest(
                        "check", "research-enumeration", "run_exact_enumeration", {"bound": 8}
                    ),
                ),
            )
        assert request.inputs["tool_feedback"]["check"]["payload"]["checked"] == 9
        return result({"candidate_kind": "PROOF", "open_obligations": []})

    tool_attempts = 0

    def tool(request: ComponentRequest, count: int) -> ComponentResult:
        nonlocal tool_attempts
        tool_attempts += 1
        assert request.route_id == "route-a"
        operation = request.inputs["tool_request"]["operation"]
        if operation == "run_cas":
            return result({"expanded": "x+1"})
        if tool_attempts == 2:
            raise RuntimeError("enumerator unavailable once")
        return result({"checked": 9})

    script[WorkKind.DEVELOP_ROUTE.value] = develop
    script[WorkKind.TOOL_REQUEST.value] = tool
    runtime = ScriptedRuntime(script)
    engine = orchestrator(runtime)
    two_cycles = replace(plan(), max_tool_cycles=2)
    paused = engine.advance(engine.start(two_cycles)).checkpoint

    assert paused.status is OrchestrationStatus.PAUSED
    assert paused.pause_reason == PauseReason.RUNTIME_UNAVAILABLE.value
    restored = engine.resume(paused)
    waiting = engine.advance(restored).checkpoint

    assert waiting.status is OrchestrationStatus.WAITING_HUMAN_REVIEW
    assert runtime.counts[WorkKind.DEVELOP_ROUTE.value] >= 4
    attached = [event for event in waiting.events if event.event_type == "TOOL_FEEDBACK_ATTACHED"]
    assert any(event.payload["route_id"] == "route-a" for event in attached)
    function_events = [
        event for event in waiting.events if event.event_type == "REGISTERED_FUNCTION_EXECUTED"
    ]
    route_functions = [
        event.payload["function_name"]
        for event in function_events
        if event.payload["route_id"] == "route-a"
    ]
    assert route_functions == [
        "run_cas", "run_exact_enumeration"
    ]
    recovered = next(
        event
        for event in function_events
        if event.payload["function_name"] == "run_exact_enumeration"
    )
    assert recovered.payload["recovered_after_failure"] is True
    assert recovered.payload["runtime_attempt"] == 2
    assert recovered.payload["authority_ceiling"] == "NO_FACT_GRAPH_WRITE"
    pause = next(
        event for event in paused.events if event.event_type == "ORCHESTRATION_PAUSED"
    )
    assert pause.payload["same_checkpoint_retry"] is True
    assert pause.payload["runtime_attempt"] == 1


def test_runtime_exception_receipt_pauses_before_feedback_then_recovers_attempt_two() -> None:
    script = complete_script()

    def develop(request: ComponentRequest, count: int) -> ComponentResult:
        if request.route_id == "route-a" and count == 1:
            return ComponentResult(
                "TOOL_REQUEST",
                tool_requests=(
                    ToolRequest("exec", "research-enumeration", "run_exact_enumeration", {}),
                ),
            )
        if request.route_id == "route-a":
            assert request.inputs["tool_feedback"]["exec"]["status"] == "COMPLETED"
        return result({"candidate_kind": "PROOF", "open_obligations": []})

    attempts = 0

    def tool(request: ComponentRequest, count: int) -> ComponentResult:
        nonlocal attempts
        del request, count
        attempts += 1
        if attempts == 1:
            return ComponentResult(
                "RUNTIME_EXCEPTION",
                component_id="research-enumeration",
                component_receipt_id="failed-receipt",
                payload={"error_type": "FileNotFoundError"},
            )
        return ComponentResult(
            "COMPLETED",
            component_id="research-enumeration",
            component_receipt_id="successful-receipt",
            payload={"checked": 9},
        )

    script[WorkKind.DEVELOP_ROUTE.value] = develop
    script[WorkKind.TOOL_REQUEST.value] = tool
    runtime = ScriptedRuntime(script)
    engine = orchestrator(runtime)
    paused = engine.advance(engine.start(plan())).checkpoint

    assert paused.status is OrchestrationStatus.PAUSED
    assert "exec" not in paused.tool_feedback
    assert paused.queue[0].kind is WorkKind.TOOL_REQUEST
    assert paused.queue[0].context["runtime_attempt"] == 1
    assert paused.queue[0].context["last_error_type"] == "RUNTIME_EXCEPTION"
    failed = [
        event
        for event in paused.events
        if event.event_type == "REGISTERED_FUNCTION_EXECUTED"
        and event.payload["request_key"] == "exec"
    ]
    assert failed[-1].payload["runtime_attempt"] == 1
    assert failed[-1].payload["status"] == "RUNTIME_EXCEPTION"
    pause = paused.events[-1]
    assert pause.event_type == "ORCHESTRATION_PAUSED"
    assert pause.payload["same_checkpoint_retry"] is True

    waiting = engine.advance(engine.resume(paused)).checkpoint
    recovered = [
        event
        for event in waiting.events
        if event.event_type == "REGISTERED_FUNCTION_EXECUTED"
        and event.payload["request_key"] == "exec"
    ][-1]
    assert recovered.payload["runtime_attempt"] == 2
    assert recovered.payload["recovered_after_failure"] is True
    assert waiting.tool_feedback["exec"]["status"] == "COMPLETED"


def test_repairable_tool_schema_error_returns_feedback_then_forced_second_round_runs() -> None:
    script = complete_script()
    develop_turns = 0

    def develop(request: ComponentRequest, count: int) -> ComponentResult:
        nonlocal develop_turns
        del count
        if request.route_id != "route-a":
            return result({"candidate_kind": "PROOF", "open_obligations": []})
        develop_turns += 1
        if develop_turns == 1:
            return ComponentResult(
                "REQUESTS_NEEDED",
                tool_requests=(
                    ToolRequest("bad-cas", "research-cas", "run_cas", {"bad": True}),
                ),
            )
        if develop_turns == 2:
            feedback = request.inputs["tool_feedback"]["bad-cas"]
            assert feedback["status"] == "RUNTIME_EXCEPTION"
            assert feedback["payload"]["repairable_tool_request"] is True
            return ComponentResult(
                "REQUESTS_NEEDED",
                tool_requests=(
                    ToolRequest("accept-cas-second-round", "research-cas", "run_cas", {"ok": 1}),
                ),
            )
        assert (
            request.inputs["tool_feedback"]["accept-cas-second-round"]["status"]
            == "COMPLETED"
        )
        return result({"candidate_kind": "PROOF", "open_obligations": []})

    def tool(request: ComponentRequest, count: int) -> ComponentResult:
        del count
        key = request.inputs["tool_request"]["request_key"]
        if key == "bad-cas":
            return ComponentResult(
                "RUNTIME_EXCEPTION",
                component_id="research-cas",
                component_receipt_id="bad-schema-receipt",
                payload={
                    "error": "arguments do not match registry",
                    "repairable_tool_request": True,
                },
            )
        return ComponentResult(
            "COMPLETED",
            component_id="research-cas",
            component_receipt_id="cas-success-receipt",
            payload={"expanded": "x+1"},
        )

    script[WorkKind.DEVELOP_ROUTE.value] = develop
    script[WorkKind.TOOL_REQUEST.value] = tool
    runtime = ScriptedRuntime(script)
    engine = orchestrator(runtime)
    two_cycles = replace(plan(), max_tool_cycles=2)
    waiting = engine.advance(engine.start(two_cycles)).checkpoint
    assert waiting.pause_reason != PauseReason.RUNTIME_UNAVAILABLE.value
    events = [
        event
        for event in waiting.events
        if event.event_type == "REGISTERED_FUNCTION_EXECUTED"
        and event.payload["route_id"] == "route-a"
    ]
    assert [(event.payload["request_key"], event.payload["status"]) for event in events] == [
        ("bad-cas", "RUNTIME_EXCEPTION"),
        ("accept-cas-second-round", "COMPLETED"),
    ]


def test_configured_jixia_runs_on_current_route_source_and_is_reported(tmp_path: Path) -> None:
    script = complete_script()
    seen: list[Mapping[str, Any]] = []

    def formalize(request: ComponentRequest, count: int) -> ComponentResult:
        del count
        return result(
            {
                "lean_candidate": "theorem route_candidate : True := by trivial",
                "open_obligations": [],
            }
        )

    def tool(request: ComponentRequest, count: int) -> ComponentResult:
        del count
        tool_request = request.inputs["tool_request"]
        seen.append(tool_request)
        if tool_request["operation"] == "replay_lean":
            return result({"kernel_verdict": "REPLAY_PASS"}, "olean")
        return result({"declarations": ["route_candidate"], "symbols": ["True"]}, "jixia")

    script[WorkKind.FORMALIZE_ROUTE.value] = formalize
    script[WorkKind.TOOL_REQUEST.value] = tool
    runtime = ScriptedRuntime(script)
    engine = orchestrator(runtime)
    engine._config = SimpleNamespace(  # type: ignore[assignment]
        adapter_profiles={
                "research-lean": {"workspace_root": str(tmp_path)},
                "research-jixia": {"workspace_root": str(tmp_path)},
            },
            inbox_roots=(tmp_path,),
    )
    engine._persist_component_result = lambda checkpoint, request, result: result  # type: ignore[method-assign]

    waiting = engine.advance(engine.start(plan())).checkpoint

    operations = [item["operation"] for item in seen]
    assert operations.count("replay_lean") == 2
    assert operations.count("analyze_lean") == 2
    jixia_events = [
        event for event in waiting.events if event.payload.get("work_kind") == "JIXIA_STRUCTURE"
    ]
    assert {event.payload["route_id"] for event in jixia_events} == {"route-a", "route-b"}
    assert all(
        event.payload["authority_ceiling"] == "STRUCTURAL_FEEDBACK_ONLY"
        for event in jixia_events
    )


def test_lean_first_failed_obligation_revises_only_affected_route(tmp_path: Path) -> None:
    script = complete_script()
    formalize_counts: defaultdict[str, int] = defaultdict(int)
    revise_inputs: list[ComponentRequest] = []

    def formalize(request: ComponentRequest, count: int) -> ComponentResult:
        del count
        assert request.route_id is not None
        formalize_counts[request.route_id] += 1
        return result({"lean_candidate": "theorem candidate : True := by trivial"})

    def revise(request: ComponentRequest, count: int) -> ComponentResult:
        del count
        revise_inputs.append(request)
        return result({"candidate_kind": "PROOF", "open_obligations": []})

    replay_a = 0

    def tool(request: ComponentRequest, count: int) -> ComponentResult:
        nonlocal replay_a
        del count
        route_id = request.route_id
        if route_id == "route-a":
            replay_a += 1
            if replay_a == 1:
                return result(
                    {
                        "kernel_verdict": "REPLAY_FAILED",
                        "first_failed_obligation_id": "lean.missing_premise",
                        "diagnostic": "unknown constant Nat.foo",
                    }
                )
        return result({"kernel_verdict": "REPLAY_PASS"})

    script[WorkKind.FORMALIZE_ROUTE.value] = formalize
    script[WorkKind.REVISE_ROUTE.value] = revise
    script[WorkKind.TOOL_REQUEST.value] = tool
    runtime = ScriptedRuntime(script)
    engine = orchestrator(runtime)
    engine._config = SimpleNamespace(  # type: ignore[assignment]
        adapter_profiles={"research-lean": {"workspace_root": str(tmp_path)}},
        inbox_roots=(tmp_path,),
    )
    engine._persist_component_result = lambda checkpoint, request, result: result  # type: ignore[method-assign]

    waiting = engine.advance(engine.start(plan(route_revisions=2))).checkpoint

    assert waiting.status is OrchestrationStatus.WAITING_HUMAN_REVIEW
    assert formalize_counts == {"route-a": 2, "route-b": 1}
    affected = next(request for request in revise_inputs if request.route_id == "route-a")
    assert affected.inputs["lean_feedback"]["first_failed_obligation_id"] == (
        "lean.missing_premise"
    )
    assert any(event.event_type == "LEAN_REPAIR_SCHEDULED" for event in waiting.events)


def test_route_candidates_execute_concurrently_but_merge_in_stable_route_order() -> None:
    script = complete_script()
    barrier = threading.Barrier(2)
    intervals: dict[str, tuple[float, float]] = {}

    def falsify(request: ComponentRequest, count: int) -> ComponentResult:
        del count
        assert request.route_id is not None
        started = time.perf_counter()
        barrier.wait(timeout=2)
        time.sleep(0.02)
        intervals[request.route_id] = (started, time.perf_counter())
        return result({"falsified": False, "open_obligations": []})

    script[WorkKind.FALSIFY_ROUTE.value] = falsify
    engine = orchestrator(ScriptedRuntime(script))
    waiting = engine.advance(engine.start(plan())).checkpoint

    assert intervals["route-a"][0] < intervals["route-b"][1]
    assert intervals["route-b"][0] < intervals["route-a"][1]
    parallel = next(
        event
        for event in waiting.events
        if event.event_type == "ROUTE_CANDIDATES_EXECUTED_IN_PARALLEL"
    )
    assert parallel.payload["route_ids"] == ["route-a", "route-b"]
    completions = [
        event.payload["route_id"]
        for event in waiting.events
        if event.event_type == "COMPONENT_COMPLETED"
        and event.payload["work_kind"] == WorkKind.FALSIFY_ROUTE.value
    ]
    assert completions == ["route-a", "route-b"]
    promotion = next(
        event
        for event in waiting.events
        if event.event_type == "ROUTE_CANDIDATES_PROMOTED_SERIAL"
    )
    assert promotion.payload["promotion_order"] == ["route-a", "route-b"]
    recorded = {
        item["route_id"]: (item["started_ns"], item["finished_ns"])
        for item in promotion.payload["parallel_execution_intervals"]
    }
    assert recorded["route-a"][0] < recorded["route-b"][1]
    assert recorded["route-b"][0] < recorded["route-a"][1]


def test_parallel_batch_failure_preserves_every_route_without_replaying_success() -> None:
    script = complete_script()
    calls: defaultdict[str, int] = defaultdict(int)

    def falsify(request: ComponentRequest, count: int) -> ComponentResult:
        del count
        assert request.route_id is not None
        calls[request.route_id] += 1
        if request.route_id == "route-b" and calls[request.route_id] == 1:
            raise RuntimeError("route-b transient failure")
        return result({"falsified": False, "open_obligations": []})

    script[WorkKind.FALSIFY_ROUTE.value] = falsify
    engine = orchestrator(ScriptedRuntime(script))
    paused = engine.advance(engine.start(plan())).checkpoint
    assert paused.status is OrchestrationStatus.PAUSED
    assert [item.route_id for item in paused.queue[:2]] == ["route-a", "route-b"]
    pause = next(event for event in paused.events if event.event_type == "ORCHESTRATION_PAUSED")
    assert pause.payload["parallel_batch_preserved"] is True

    waiting = engine.advance(engine.resume(paused)).checkpoint
    assert waiting.status is OrchestrationStatus.WAITING_HUMAN_REVIEW
    assert calls == {"route-a": 1, "route-b": 2}
    promotion = [
        event
        for event in waiting.events
        if event.event_type == "ROUTE_CANDIDATES_PROMOTED_SERIAL"
    ]
    assert len(promotion) == 1
    assert promotion[0].payload["promotion_order"] == ["route-a", "route-b"]


def test_parallel_batch_internal_pause_preserves_unprocessed_precomputed_routes() -> None:
    script = complete_script()
    route_calls: defaultdict[str, int] = defaultdict(int)

    def falsify(request: ComponentRequest, count: int) -> ComponentResult:
        del count
        assert request.route_id is not None
        route_calls[request.route_id] += 1
        if request.route_id == "route-a" and route_calls[request.route_id] == 1:
            return ComponentResult("REQUESTS_NEEDED")
        return result({"falsified": False, "open_obligations": []})

    script[WorkKind.FALSIFY_ROUTE.value] = falsify
    engine = orchestrator(ScriptedRuntime(script))
    paused = engine.advance(engine.start(plan())).checkpoint
    assert paused.status is OrchestrationStatus.PAUSED
    assert not any(
        event.event_type == "ROUTE_CANDIDATES_PROMOTED_SERIAL" for event in paused.events
    )
    assert [item.route_id for item in paused.queue[:2]] == ["route-a", "route-b"]
    assert "parallel_precomputed" in paused.queue[1].context

    waiting = engine.advance(engine.resume(paused)).checkpoint
    assert waiting.status is OrchestrationStatus.WAITING_HUMAN_REVIEW
    assert route_calls == {"route-a": 2, "route-b": 1}
    promotions = [
        event
        for event in waiting.events
        if event.event_type == "ROUTE_CANDIDATES_PROMOTED_SERIAL"
    ]
    assert len(promotions) == 1
    assert promotions[0].payload["promotion_order"] == ["route-a", "route-b"]


def test_budget_pause_is_serializable_and_resumes_without_replaying_completed_work() -> None:
    runtime = ScriptedRuntime(complete_script())
    engine = orchestrator(runtime)
    paused = engine.advance(engine.start(plan(work_units=2))).checkpoint

    assert paused.status is OrchestrationStatus.PAUSED
    assert paused.pause_reason == PauseReason.BUDGET_EXHAUSTED.value
    assert paused.usage.work_units == 2
    before = len(runtime.calls)
    restored = engine.resume(
        paused,
        budget=replace(paused.plan.budget, max_work_units=100),
    )
    waiting = engine.advance(restored).checkpoint

    assert waiting.status is OrchestrationStatus.WAITING_HUMAN_REVIEW
    assert len(runtime.calls) > before
    assert runtime.counts[WorkKind.CONTRACT_CLARIFY.value] == 1
    assert runtime.counts[WorkKind.LITERATURE_AUDIT.value] == 1
    serialized = waiting.to_dict()
    assert len(serialized["checkpoint_digest"]) == 64
    assert [event["sequence"] for event in serialized["events"]] == list(
        range(1, len(serialized["events"]) + 1)
    )


def test_malformed_role_output_pauses_as_runtime_failure_and_keeps_work_item() -> None:
    script = complete_script()
    script[WorkKind.CONTRACT_CLARIFY.value] = lambda request, count: ComponentResult(
        status="ADAPTER_SCHEMA_MISMATCH",
        payload={"open_obligations": ["ROLE_OUTPUT_NOT_JSON"]},
        usage={"wall_time_ms": 10},
    )
    engine = orchestrator(ScriptedRuntime(script))
    paused = engine.advance(engine.start(plan())).checkpoint

    assert paused.status is OrchestrationStatus.PAUSED
    assert paused.pause_reason == PauseReason.RUNTIME_UNAVAILABLE.value
    assert paused.queue[0].kind is WorkKind.CONTRACT_CLARIFY
    assert paused.events[-1].payload["component_status"] == "ADAPTER_SCHEMA_MISMATCH"


def test_open_composition_or_nonaccepting_review_finishes_unresolved() -> None:
    runtime = ScriptedRuntime(complete_script(composition_closed=False))
    engine = orchestrator(runtime)
    waiting = engine.advance(engine.start(plan())).checkpoint
    assert waiting.composition_closed is False
    assert runtime.counts[WorkKind.REVISE_COMPOSITION.value] == 1

    resumed = engine.resume(
        waiting,
        human_reviews=(HumanReview("review-1", "mathematician-1", "REJECTED", "review.md"),),
    )
    completed = engine.advance(resumed).checkpoint

    assert completed.status is OrchestrationStatus.COMPLETED
    assert completed.outcome == ResearchOutcome.UNRESOLVED.value


def test_changes_requested_reenters_revision_and_requires_a_new_review() -> None:
    runtime = ScriptedRuntime(complete_script())
    script = runtime.script
    script[WorkKind.REVISE_ROUTE.value] = lambda request, count: result(
        {"open_obligations": []}, f"revision-{request.route_id}"
    )
    engine = orchestrator(runtime)
    first_gate = engine.advance(engine.start(plan(route_revisions=1))).checkpoint

    revision = engine.resume(
        first_gate,
        human_reviews=(
            HumanReview("review-1", "mathematician-1", "CHANGES_REQUESTED", "review-1.md"),
        ),
    )
    second_gate = engine.advance(revision).checkpoint

    assert second_gate.status is OrchestrationStatus.WAITING_HUMAN_REVIEW
    assert runtime.counts[WorkKind.REVISE_ROUTE.value] == 2
    assert runtime.counts[WorkKind.GAP_REVIEW.value] == 4
    final = engine.advance(
        engine.resume(
            second_gate,
            human_reviews=(HumanReview("review-2", "mathematician-1", "ACCEPTED", "review-2.md"),),
        )
    ).checkpoint
    assert final.outcome == ResearchOutcome.UNRESOLVED.value


def test_existing_method_cards_and_ac5_sentinels_are_reused() -> None:
    runtime = ScriptedRuntime(complete_script())
    fixtures = {
        "method_cards": [{"schema_version": "rk.method_card.v1", "card_id": "MC_EXISTING"}],
        "ac5_cases": [{"case_id": "N01", "class": "NEAR_MISS"}],
        "glue_cases": [{"case_id": "G01", "class": "GLUE_TRAP"}],
    }
    engine = ResearchOrchestrator(
        runtime,
        clock=FixedClock(),
        id_factory=SequentialIds(),
        research_fixtures=fixtures,
    )
    engine.advance(engine.start(plan()), max_work_units=3)

    scout = next(call for call in runtime.calls if call.work_kind == WorkKind.ROUTE_SCOUT.value)
    assert scout.inputs["method_cards"][0]["card_id"] == "MC_EXISTING"
    assert scout.inputs["sentinel_cases"][0]["case_id"] == "N01"


def test_route_promotion_requires_complete_cards_sentinels_and_five_axis_diversity() -> None:
    engine = ResearchOrchestrator(ScriptedRuntime({}))
    base = complete_script()[WorkKind.ROUTE_SCOUT.value](  # type: ignore[index]
        None, 1
    ).payload["routes"]
    copied = dict(base[0])
    copied["route_id"] = "route-copy"
    copied["label"] = "cosmetic rewrite"
    copied["representation"] = "different words"
    incomplete = dict(base[0])
    incomplete["route_id"] = "route-incomplete"
    incomplete.pop("near_miss")

    promoted = engine._routes_from_payload(
        engine.start(plan()), {"routes": [base[0], copied, incomplete]}
    )

    assert [route.route_id for route in promoted] == ["route-a"]
    assert promoted[0].promotion_reasons == (
        "METHOD_CARD_COMPLETE",
        "SENTINEL_EXECUTED",
        "FIVE_AXIS_SIGNATURE_UNIQUE",
        "PROOF_SKELETON_AND_BOUNDARY_CASES_PRESENT",
    )


def test_status_reads_checkpoint_without_executing_runtime(tmp_path: Path) -> None:
    runtime = ScriptedRuntime(complete_script())
    engine = orchestrator(runtime)
    checkpoint = engine.start(plan())
    engine._config = type("Config", (), {"workspace_root": tmp_path})()  # type: ignore[assignment]
    engine._save_checkpoint(checkpoint)
    before = len(runtime.calls)

    observed = engine.status("run-1")

    assert observed.checkpoint_digest == checkpoint.checkpoint_digest
    assert len(runtime.calls) == before


def test_kernel_change_invalidates_queue_feedback_and_closed_composition(tmp_path: Path) -> None:
    runtime = ScriptedRuntime(complete_script())
    engine = orchestrator(runtime)
    checkpoint = replace(
        engine.start(plan()),
        queue=engine.start(plan()).queue,
        tool_feedback={"stale": {"status": "COMPLETED"}},
        composition_closed=True,
        kernel_revision=4,
    )
    snapshot = SimpleNamespace(revision=5, current_contract_version=2)
    engine._kernel = SimpleNamespace(inspect=lambda run_id: snapshot)  # type: ignore[assignment]
    engine._config = SimpleNamespace(workspace_root=tmp_path)  # type: ignore[assignment]
    engine._plan_for_run = lambda run_id: replace(  # type: ignore[method-assign]
        plan(), contract_version=2, contract_hash="c" * 64
    )
    engine._save_checkpoint(checkpoint)
    # Saving binds revision 5; simulate a later authoritative mutation.
    snapshot.revision = 6

    observed = engine.status("run-1")

    assert observed.kernel_revision == 6
    assert observed.plan.contract_version == 2
    assert observed.queue[0].kind is WorkKind.CONTRACT_CLARIFY
    assert observed.tool_feedback == {}
    assert observed.composition_closed is False
    assert observed.events[-1].event_type == "CHECKPOINT_INVALIDATED_BY_KERNEL_CHANGE"
