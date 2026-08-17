from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any

import pytest

from rk.adapters.base import AdapterRequestError
from rk.component_runtime import (
    ComponentRegistration,
    ComponentRuntime,
    ComponentRuntimeError,
    RuntimeComponentKind,
    UnknownComponent,
    _StructuredRoleAdapter,
    build_component_runtime,
    without_environment,
)
from rk.config import KernelConfig
from rk.extensions import ExtensionRegistry, ToolReceipt
from rk.roles import MathRole, get_role_spec
from rk.scheduler import (
    HardwareInventory,
    ScheduleRequest,
    schedule_research,
)


class FakeAdapter:
    name = "fixture"
    version = "1"

    def __init__(self, result: Mapping[str, Any]) -> None:
        self.result = result
        self.requests: list[Mapping[str, Any]] = []

    def run(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        self.requests.append(dict(request))
        return self.result


def test_structured_role_adapter_unpacks_model_json_and_preserves_usage() -> None:
    model_payload = {
        "status": "COMPLETED",
        "payload": {"routes": [{"label": "algebra"}]},
        "artifact_ids": [],
        "tool_requests": [
            {
                "request_key": "s1",
                "tool": "research-search",
                "operation": "search_lean",
                "payload": {
                    "query": ["add zero"],
                    "num_results": 8,
                    "rerank": True,
                    "retrieve_k": 50,
                },
                "required": True,
            }
        ],
    }
    adapter = _StructuredRoleAdapter(
        FakeAdapter(
            {
                "status": "COMPLETED",
                "payload": {
                    "text": "```json\n"
                    + json.dumps(model_payload, ensure_ascii=False)
                    + "\n```"
                },
                "usage": {"input_tokens": 7, "output_tokens": 9},
            }
        )
    )
    result = adapter.run({"prompt": "x"})
    assert result["status"] == "COMPLETED"
    assert result["payload"] == {"routes": [{"label": "algebra"}]}
    assert result["tool_requests"][0]["operation"] == "search_lean"
    assert result["usage"] == {"input_tokens": 7, "output_tokens": 9}


def test_gap_review_prompt_requires_rethlas_but_keeps_it_soft() -> None:
    model = FakeAdapter(
        {"status": "COMPLETED", "payload": {"text": '{"status":"COMPLETED"}'}}
    )
    adapter = _StructuredRoleAdapter(model)
    request = adapter.run(
        {
            "prompt": "base\n若 available_tools 含 verify_rethlas, 必须先调用; 永远 SOFT_MODEL",
            "model": "fixture",
            "max_tokens": 100,
            "environment": {},
        }
    )
    assert request["status"] == "COMPLETED"
    assert "verify_rethlas" in model.requests[0]["prompt"]
    assert "SOFT_MODEL" in model.requests[0]["prompt"]


def registration(
    adapter: FakeAdapter,
    *,
    component_id: str = "leansearch-public",
    function_name: str = "search_lean",
    kind: RuntimeComponentKind = RuntimeComponentKind.LEANSEARCH,
    scheduled_names: tuple[str, ...] = ("public LeanSearch retriever",),
) -> ComponentRegistration:
    return ComponentRegistration(
        component_id=component_id,
        kind=kind,
        adapter=adapter,
        function_name=function_name,
        description="Search registered mathematical data",
        function_schema={
            "type": "object",
            "properties": {"query": {"type": "string"}},
            "required": ["query"],
            "additionalProperties": False,
        },
        scheduled_names=scheduled_names,
        request_builder=without_environment,
    )


def test_runtime_executes_registered_function_and_meters_usage() -> None:
    adapter = FakeAdapter(
        {
            "status": "COMPLETED",
            "payload": {"hits": ["Nat.add_comm"]},
            "usage": {"input_tokens": 3, "output_tokens": 2, "total_tokens": 5},
        }
    )
    runtime = ComponentRuntime([registration(adapter)])

    receipt = runtime.execute_function(
        call_id="call-1",
        function_name="search_lean",
        arguments={"query": "commutativity"},
        environment={},
    )

    assert receipt.succeeded
    assert receipt.result["payload"] == {"hits": ["Nat.add_comm"]}
    assert receipt.usage["input_tokens"] == 3
    assert receipt.usage["wall_time_ms"] >= 0
    assert adapter.requests == [{"query": "commutativity"}]


def test_runtime_emits_sanitized_registered_tool_receipt() -> None:
    observed: list[ToolReceipt] = []
    adapter = FakeAdapter(
        {
            "status": "COMPLETED",
            "payload": {"raw_completion": "must not enter the execution ledger"},
            "artifact_ids": ["artifact-1"],
            "usage": {"input_tokens": 3},
        }
    )
    registry = ExtensionRegistry().register_tool_receipt_consumer(
        "leansearch-public", observed.append
    )
    runtime = ComponentRuntime([registration(adapter)], registry)

    receipt = runtime.execute_function(
        call_id="attempt-1",
        function_name="search_lean",
        arguments={"query": "commutativity"},
        environment={},
    )

    assert len(observed) == 1
    assert observed[0].tool_run_id == receipt.receipt_id
    assert observed[0].attempt_id == "attempt-1"
    assert observed[0].artifact_ids == ("artifact-1",)
    assert observed[0].payload["component_id"] == "leansearch-public"
    assert "raw_completion" not in observed[0].payload


def test_runtime_rejects_unknown_functions_and_invalid_arguments_before_execution() -> None:
    adapter = FakeAdapter({"status": "COMPLETED"})
    runtime = ComponentRuntime([registration(adapter)])

    with pytest.raises(UnknownComponent):
        runtime.execute_function(
            call_id="call-1", function_name="shell", arguments={}, environment={}
        )
    with pytest.raises(ComponentRuntimeError, match="arguments"):
        runtime.execute_function(
            call_id="call-2",
            function_name="search_lean",
            arguments={"query": 9},
            environment={},
        )
    assert adapter.requests == []


def test_runtime_supports_all_required_component_kinds() -> None:
    registrations = []
    for kind in (
        RuntimeComponentKind.MODEL,
        RuntimeComponentKind.LEANSEARCH,
        RuntimeComponentKind.JIXIA,
        RuntimeComponentKind.SMT,
        RuntimeComponentKind.CAS,
        RuntimeComponentKind.EXACT_ENUMERATION,
        RuntimeComponentKind.LITERATURE,
        RuntimeComponentKind.LEAN_REPLAY,
    ):
        suffix = kind.value.lower()
        registrations.append(
            registration(
                FakeAdapter({"status": "COMPLETED", "payload": suffix}),
                component_id=suffix,
                function_name=f"run_{suffix}",
                kind=kind,
                scheduled_names=(),
            )
        )
    runtime = ComponentRuntime(registrations)
    assert len(runtime.function_definitions()) == 8


def test_scheduler_binding_selects_concrete_runtime_components_and_exposes_gaps() -> None:
    runtime = ComponentRuntime(
        [
            registration(FakeAdapter({"status": "COMPLETED"})),
            registration(
                FakeAdapter({"status": "COMPLETED"}),
                component_id="api-model",
                function_name="ask_model",
                kind=RuntimeComponentKind.MODEL,
                scheduled_names=("API candidate model",),
            ),
        ]
    )
    decision = schedule_research(
        ScheduleRequest(require_jixia=False),
        HardwareInventory(
            "macos",
            32,
            apple_unified_memory=True,
            api_candidate_available=True,
            public_retrieval_available=True,
        ),
        (),
    )

    plan = runtime.bind_schedule(decision)

    assert plan.steps[0].component_ids == ("leansearch-public",)
    assert plan.steps[1].component_ids == ("api-model",)
    assert "registered CAS/SMT probes" in plan.missing_steps
    assert "Lean clean replay and axiom audit" in plan.missing_steps
    assert plan.runnable is False


def test_adapter_failure_is_a_real_receipt_not_a_fake_success() -> None:
    runtime = ComponentRuntime(
        [registration(FakeAdapter({"status": "ENVIRONMENT_DRIFT", "payload": None}))]
    )
    receipt = runtime.execute_function(
        call_id="call-fail",
        function_name="search_lean",
        arguments={"query": "x"},
        environment={},
    )
    assert receipt.status == "ENVIRONMENT_DRIFT"
    assert receipt.succeeded is False


@dataclass(frozen=True)
class OrchestratorRequest:
    request_id: str
    work_kind: str
    inputs: Mapping[str, Any]
    run_id: str = "run-1"
    role: str | None = None
    route_id: str | None = None
    round: int = 0
    contract_scope: Mapping[str, Any] = field(default_factory=dict)
    budget_remaining: Mapping[str, Any] = field(default_factory=dict)
    hardware_plan: Mapping[str, Any] = field(default_factory=dict)


def test_runtime_satisfies_orchestrator_tool_request_seam() -> None:
    adapter = FakeAdapter({"status": "COMPLETED", "payload": {"hits": ["x"]}})
    runtime = ComponentRuntime([registration(adapter)])
    result = runtime.execute(
        OrchestratorRequest(
            request_id="request-1",
            work_kind="TOOL_REQUEST",
            inputs={
                "tool_request": {
                    "request_key": "tool-1",
                    "tool": "leansearch-public",
                    "operation": "search_lean",
                    "payload": {"query": "x"},
                }
            },
        )
    )
    assert result["status"] == "COMPLETED"
    assert result["payload"] == {"hits": ["x"]}
    assert result["component_id"] == "leansearch-public"
    assert result["usage"]["wall_time_ms"] >= 0


def test_runtime_preserves_registered_tool_provenance_in_feedback() -> None:
    adapter = FakeAdapter(
        {
            "status": "COMPLETED",
            "payload": {"negated_claim": "unsat"},
            "binary_sha256": "a" * 64,
            "input_sha256": "b" * 64,
            "exit_code": 0,
        }
    )
    runtime = ComponentRuntime([registration(adapter)])

    result = runtime.execute(
        OrchestratorRequest(
            request_id="request-provenance",
            work_kind="TOOL_REQUEST",
            inputs={
                "tool_request": {
                    "request_key": "tool-provenance",
                    "tool": "leansearch-public",
                    "operation": "search_lean",
                    "payload": {"query": "x"},
                }
            },
        )
    )

    assert result["payload"]["negated_claim"] == "unsat"
    assert result["payload"]["_component_provenance"] == {
        "binary_sha256": "a" * 64,
        "input_sha256": "b" * 64,
        "exit_code": 0,
    }


def test_malformed_registered_tool_call_returns_repairable_feedback() -> None:
    runtime = ComponentRuntime(
        [registration(FakeAdapter({"status": "COMPLETED", "payload": {}}))]
    )

    result = runtime.execute(
        OrchestratorRequest(
            request_id="request-malformed",
            work_kind="TOOL_REQUEST",
            inputs={
                "tool_request": {
                    "request_key": "bad-call",
                    "tool": "leansearch-public",
                    "operation": "search_lean",
                    "payload": {"unexpected": 1},
                }
            },
        )
    )

    assert result["status"] == "RUNTIME_EXCEPTION"
    assert result["payload"]["repairable_tool_request"] is True
    assert result["component_id"] == "leansearch-public"
    assert result["component_started_ns"] <= result["component_finished_ns"]


def test_adapter_request_error_is_repairable_but_environment_error_is_not() -> None:
    class RequestRejectingAdapter:
        name = "request-rejecting"
        version = "1"

        def run(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
            del request
            raise AdapterRequestError("declaration name must not contain theorem source")

    class MissingProcessAdapter:
        name = "missing-process"
        version = "1"

        def run(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
            del request
            raise FileNotFoundError("missing executable")

    schema = {"type": "object", "additionalProperties": False}
    registrations = [
        ComponentRegistration(
            component_id="request-rejecting",
            kind=RuntimeComponentKind.CAS,
            adapter=RequestRejectingAdapter(),
            function_name="reject_request",
            description="reject malformed request",
            function_schema=schema,
            request_builder=lambda arguments, environment: {},
        ),
        ComponentRegistration(
            component_id="missing-process",
            kind=RuntimeComponentKind.CAS,
            adapter=MissingProcessAdapter(),
            function_name="missing_process",
            description="simulate missing process",
            function_schema=schema,
            request_builder=lambda arguments, environment: {},
        ),
    ]
    runtime = ComponentRuntime(registrations)
    repairable = runtime.execute_function(
        call_id="repairable",
        function_name="reject_request",
        arguments={},
        environment={},
    )
    environmental = runtime.execute_function(
        call_id="environmental",
        function_name="missing_process",
        arguments={},
        environment={},
    )
    assert repairable.status == "RUNTIME_EXCEPTION"
    assert repairable.result["payload"]["repairable_tool_request"] is True
    assert environmental.status == "RUNTIME_EXCEPTION"
    assert environmental.result["payload"] is None


@pytest.mark.parametrize("status", ["EXPECTATION_MISMATCH", "ADAPTER_SCHEMA_MISMATCH"])
def test_deterministic_request_result_mismatches_are_repairable_feedback(status: str) -> None:
    adapter = FakeAdapter({"status": status, "payload": None})
    runtime = ComponentRuntime(
        [
            ComponentRegistration(
                component_id="deterministic-tool",
                kind=RuntimeComponentKind.EXACT_ENUMERATION,
                adapter=adapter,
                function_name="check_expected",
                description="check deterministic expected output",
                function_schema={"type": "object", "additionalProperties": False},
                request_builder=lambda arguments, environment: {},
            )
        ]
    )
    receipt = runtime.execute_function(
        call_id=f"call-{status}",
        function_name="check_expected",
        arguments={},
        environment={},
    )
    assert receipt.status == status
    assert receipt.result["payload"]["repairable_tool_request"] is True


@pytest.mark.parametrize("status", ["FAILED", "ENVIRONMENT_DRIFT", "RUNTIME_EXCEPTION"])
def test_process_and_environment_statuses_are_not_reclassified_as_repairable(
    status: str,
) -> None:
    adapter = FakeAdapter({"status": status, "payload": None})
    runtime = ComponentRuntime(
        [
            ComponentRegistration(
                component_id="process-tool",
                kind=RuntimeComponentKind.CAS,
                adapter=adapter,
                function_name="run_process",
                description="run process",
                function_schema={"type": "object", "additionalProperties": False},
                request_builder=lambda arguments, environment: {},
            )
        ]
    )
    receipt = runtime.execute_function(
        call_id=f"call-{status}",
        function_name="run_process",
        arguments={},
        environment={},
    )
    assert receipt.status == status
    assert receipt.result["payload"] is None


def test_runtime_binds_non_tool_orchestration_work_to_exactly_one_registration() -> None:
    adapter = FakeAdapter({"status": "COMPLETED", "payload": {"candidate": "proof"}})

    def build_role_request(
        arguments: Mapping[str, Any], _environment: Mapping[str, str]
    ) -> Mapping[str, Any]:
        return {"query": arguments["inputs"]["topic"]}

    item = registration(
        adapter,
        component_id="route-model",
        function_name="develop_route",
        kind=RuntimeComponentKind.MODEL,
        scheduled_names=(),
    )
    item = ComponentRegistration(
        component_id=item.component_id,
        kind=item.kind,
        adapter=item.adapter,
        function_name=item.function_name,
        description=item.description,
        function_schema={
            "type": "object",
            "properties": {
                "request_id": {"type": "string"},
                "run_id": {"type": "string"},
                "work_kind": {"type": "string"},
                "role": {"type": ["string", "null"]},
                "route_id": {"type": ["string", "null"]},
                "round": {"type": "integer"},
                "contract_scope": {"type": "object"},
                "inputs": {"type": "object"},
                "budget_remaining": {"type": "object"},
                "hardware_plan": {"type": "object"},
            },
            "required": [
                "request_id", "run_id", "work_kind", "role", "route_id", "round",
                "contract_scope", "inputs", "budget_remaining", "hardware_plan",
            ],
            "additionalProperties": False,
        },
        orchestration_kinds=("DEVELOP_ROUTE",),
        request_builder=build_role_request,
    )
    runtime = ComponentRuntime([item])
    result = runtime.execute(
        OrchestratorRequest(
            request_id="request-2", work_kind="DEVELOP_ROUTE", inputs={"topic": "sqrt2"}
        )
    )
    assert result["payload"] == {"candidate": "proof"}
    assert adapter.requests == [{"query": "sqrt2"}]


def model_profile() -> dict[str, Any]:
    return {
        "name": "research-model",
        "version": "test-v1",
        "source_commit": "a" * 40,
        "timeout_seconds": 5,
        "max_response_bytes": 1024 * 1024,
        "env_whitelist": ["DEEPSEEK_API_KEY"],
        "endpoint": "https://api.deepseek.com/chat/completions",
    }


def test_production_factory_requires_research_model(tmp_path: Any) -> None:
    config = KernelConfig.from_mapping({"workspace_root": str(tmp_path), "product": {}})
    with pytest.raises(ComponentRuntimeError, match="research-model"):
        build_component_runtime(config, {})


def test_production_factory_captures_environment_and_injects_role_prompt(
    tmp_path: Any,
) -> None:
    config = KernelConfig.from_mapping(
        {
            "workspace_root": str(tmp_path),
            "adapter_profiles": {"research-model": model_profile()},
            "product": {"model": "deepseek-v4-pro", "model_max_tokens": 1234},
        }
    )
    runtime = build_component_runtime(config, {"DEEPSEEK_API_KEY": "captured"})
    registration_item = runtime._by_id["research-model"]
    role = get_role_spec(MathRole.ROUTE_SCOUT)
    request = registration_item.request_builder(
        {
            "request_id": "request-1",
            "run_id": "run-1",
            "work_kind": "ROUTE_SCOUT",
            "role": MathRole.ROUTE_SCOUT.value,
            "route_id": None,
            "round": 0,
            "contract_scope": {},
            "inputs": {"topic": "sqrt2"},
            "budget_remaining": {},
            "hardware_plan": {},
        },
        {"DEEPSEEK_API_KEY": "caller-must-not-win"},
    )
    assert request["model"] == "deepseek-v4-pro"
    assert request["max_tokens"] == 1234
    assert request["environment"] == {"DEEPSEEK_API_KEY": "captured"}
    assert request["prompt"].startswith(role.system_prompt)
    assert "sqrt2" in request["prompt"]
    assert "ROUTE_SCOUT" in registration_item.orchestration_kinds


@pytest.mark.parametrize(
    ("work_kind", "required_text"),
    [
        ("FALSIFY_ROUTE", "research-smt 或 research-enumeration"),
        ("DEVELOP_ROUTE", "research-cas"),
    ],
)
def test_product_prompt_requires_configured_deterministic_tools(
    tmp_path: Any, work_kind: str, required_text: str
) -> None:
    config = KernelConfig.from_mapping(
        {
            "workspace_root": str(tmp_path),
            "adapter_profiles": {"research-model": model_profile()},
            "product": {"model": "deepseek-v4-pro"},
        }
    )
    runtime = build_component_runtime(config, {"DEEPSEEK_API_KEY": "captured"})
    request = runtime._by_id["research-model"].request_builder(
        {
            "request_id": "request-tool-routing",
            "run_id": "run-1",
            "work_kind": work_kind,
            "role": MathRole.PROOF_COUNTEREXAMPLE.value,
            "route_id": "route-1",
            "round": 0,
            "contract_scope": {},
            "inputs": {"claim": "x"},
            "budget_remaining": {},
            "hardware_plan": {},
        },
        {},
    )
    assert required_text in request["prompt"]


def test_product_prompts_route_rethlas_and_local_generators_as_soft_tools(tmp_path: Any) -> None:
    config = KernelConfig.from_mapping(
        {
            "workspace_root": str(tmp_path),
            "adapter_profiles": {"research-model": model_profile()},
            "product": {"model": "deepseek-v4-pro"},
        }
    )
    runtime = build_component_runtime(config, {"DEEPSEEK_API_KEY": "captured"})
    builder = runtime._by_id["research-model"].request_builder
    gap = builder(
        {
            "request_id": "gap-1",
            "run_id": "run-1",
            "work_kind": "GAP_REVIEW",
            "role": MathRole.ANONYMOUS_GAP_REVIEWER.value,
            "route_id": "route-1",
            "round": 0,
            "contract_scope": {},
            "inputs": {"atomic_claim": "c", "proof": "p"},
            "budget_remaining": {},
            "hardware_plan": {},
        },
        {},
    )
    develop = builder(
        {
            "request_id": "develop-1",
            "run_id": "run-1",
            "work_kind": "DEVELOP_ROUTE",
            "role": MathRole.PROOF_COUNTEREXAMPLE.value,
            "route_id": "route-1",
            "round": 0,
            "contract_scope": {},
            "inputs": {"claim": "c"},
            "budget_remaining": {},
            "hardware_plan": {"placements": {"QED-Nano candidate": {"placement": "GPU 0"}}},
        },
        {},
    )
    assert "verify_rethlas" in gap["prompt"] and "SOFT_MODEL" in gap["prompt"]
    assert "run_local_proof_model" in develop["prompt"]
    assert "软候选" in develop["prompt"]


def test_product_local_generator_exposes_direct_standard_function_schema(tmp_path: Any) -> None:
    binary = tmp_path / "runner"
    binary.write_bytes(b"runner")
    workspace = tmp_path / "model-workspace"
    output = tmp_path / "model-output"
    workspace.mkdir()
    output.mkdir()
    local_profile = model_profile() | {
        "name": "research-local-proof-model",
        "env_whitelist": [],
        "endpoint": None,
        "argv_prefix": [str(binary)],
        "workspace_root": str(workspace),
        "output_root": str(output),
        "binary_path": str(binary),
        "binary_sha256": hashlib.sha256(binary.read_bytes()).hexdigest(),
    }
    config = KernelConfig.from_mapping(
        {
            "workspace_root": str(tmp_path),
            "adapter_profiles": {
                "research-model": model_profile(),
                "research-local-proof-model": local_profile,
            },
            "product": {"model": "deepseek-v4-pro"},
        }
    )
    runtime = build_component_runtime(config, {"DEEPSEEK_API_KEY": "captured"})

    schema = runtime.function_definitions()["run_local_proof_model"]["parameters"]
    assert set(schema["required"]) == {"model", "prompt", "max_new_tokens", "output_relpath"}
    assert schema["properties"]["model"]["enum"] == ["qed-nano", "deepseek-prover"]


def test_production_factory_registers_only_configured_optional_components(
    tmp_path: Any,
) -> None:
    search = model_profile() | {
        "name": "research-search",
        "env_whitelist": [],
        "endpoint": "https://leansearch.example/search",
    }
    literature = model_profile() | {
        "name": "research-literature",
        "env_whitelist": [],
        "endpoint": "https://api.crossref.org/works",
    }
    config = KernelConfig.from_mapping(
        {
            "workspace_root": str(tmp_path),
            "adapter_profiles": {
                "research-model": model_profile(),
                "research-search": search,
                "research-literature": literature,
            },
            "product": {"model": "deepseek-v4-pro"},
        }
    )
    runtime = build_component_runtime(config, {"DEEPSEEK_API_KEY": "captured"})
    assert set(runtime.function_definitions()) == {
        "ask_mathematical_role",
        "search_lean",
        "search_literature",
    }
    assert runtime._by_schedule["public LeanSearch retriever"] == ("research-search",)
    assert "research-jixia" not in runtime._by_id
