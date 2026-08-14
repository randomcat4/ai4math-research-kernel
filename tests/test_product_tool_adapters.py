from __future__ import annotations

import hashlib
import sqlite3
import sys
from collections.abc import Mapping
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest

from rk.adapters.base import AdapterProfile
from rk.adapters.deterministic import RegisteredFileToolAdapter
from rk.product.compute import AuthorityCeiling, ResourceUsage, ToolAvailability
from rk.product.model_functions import ModelFunctionBridge, deepseek_responses_schema
from rk.product.tool_adapters import (
    AdapterBinding,
    CostObservation,
    DeploymentProbe,
    ProductAdapterBridge,
    ProductToolAdapterError,
    ProductToolRegistry,
)
from rk.product.tool_runs import ToolCatalogStore
from rk.product_migrations import ProductMigrationAssembler, ProductMigrationRegistry

NOW = "2026-08-13T02:00:00Z"
SIMPLE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["input", "expected"],
    "properties": {"input": {"type": "object"}, "expected": {}},
}


def database(tmp_path: Path) -> Path:
    path = tmp_path / "tools.sqlite"
    with sqlite3.connect(path, isolation_level=None) as connection:
        ProductMigrationAssembler(ProductMigrationRegistry(Path("schema_fragments"))).apply(
            connection
        )
    return path


def binding(
    tool_id: str,
    function_name: str,
    *,
    provider: str = "rk",
    model: str | None = None,
    adapter_version: str = "adapter-v1",
    build: str | None = None,
    profile: str | None = None,
    schema: Mapping[str, Any] = SIMPLE_SCHEMA,
    ceiling: AuthorityCeiling = AuthorityCeiling.SOFT_TOOL_RESULT,
) -> AdapterBinding:
    return AdapterBinding(
        tool_id=tool_id,
        function_name=function_name,
        provider=provider,
        model_id=model or f"{tool_id}-model-v1",
        adapter_version=adapter_version,
        build_version=build or (tool_id[0] * 64),
        profile_id=profile or f"{tool_id}-profile",
        function_schema=schema,
        authority_ceiling=ceiling,
    )


def required_bindings() -> tuple[AdapterBinding, ...]:
    return (
        binding(
            "deepseek-v4-pro-text",
            "generate_text",
            provider="deepseek",
            model="deepseek-v4-pro",
        ),
        binding(
            "deepseek-responses",
            "request_functions",
            provider="deepseek",
            model="deepseek-v4-pro",
            schema=deepseek_responses_schema(),
        ),
        binding("leansearch", "search_lean", provider="leansearch"),
        binding("jixia", "analyze_lean", provider="jixia"),
        binding(
            "lean",
            "replay_lean",
            provider="leanprover-community",
            ceiling=AuthorityCeiling.CERTIFICATE_REQUIRES_VALIDATION,
        ),
        binding(
            "z3",
            "run_smt",
            provider="microsoft",
            ceiling=AuthorityCeiling.CERTIFICATE_REQUIRES_VALIDATION,
        ),
        binding("sympy", "run_cas", provider="sympy"),
        binding(
            "exact-enumeration",
            "run_exact_enumeration",
            provider="rk",
            ceiling=AuthorityCeiling.CERTIFICATE_REQUIRES_VALIDATION,
        ),
        binding(
            "deepseek-prover",
            "generate_candidate",
            provider="deepseek-ai",
            model="deepseek-ai/DeepSeek-Prover-V2-7B",
        ),
    )


def test_complete_catalog_records_identity_schema_authority_and_honest_boundaries(
    tmp_path: Path,
) -> None:
    catalog = ToolCatalogStore(database(tmp_path))
    configured = required_bindings()
    z3 = next(item for item in configured if item.tool_id == "z3")
    probe = DeploymentProbe(
        key=z3.key,
        provider=z3.provider,
        build_version=z3.build_version,
        profile_id=z3.profile_id,
        receipt_artifact_id="z3-current-probe-receipt",
    )
    entries = ProductToolRegistry(catalog).register_required(configured, probes=(probe,), now=NOW)

    assert len(entries) == 16
    by_id = {entry.specification.tool_id: entry for entry in entries}
    assert by_id["z3"].specification.availability == ToolAvailability.AVAILABLE
    assert by_id["z3"].specification.authority_ceiling == (
        AuthorityCeiling.CERTIFICATE_REQUIRES_VALIDATION
    )
    assert by_id["deepseek-v4-pro-text"].model_id == "deepseek-v4-pro"
    assert by_id["deepseek-v4-pro-text"].specification.provider == "deepseek"
    assert by_id["leansearch"].specification.availability == (ToolAvailability.CONFIGURED_UNPROBED)
    assert len(by_id["leansearch"].specification.function_schema_digest) == 64
    assert by_id["gpt-5.6"].specification.availability == ToolAvailability.UNAVAILABLE
    assert by_id["codex-text"].specification.availability == ToolAvailability.SMOKE_ONLY
    assert by_id["codex-shell"].specification.availability == ToolAvailability.UNAVAILABLE
    assert by_id["codex-apply-patch"].specification.availability == (ToolAvailability.UNAVAILABLE)
    assert by_id["qed-nano"].specification.availability == ToolAvailability.SMOKE_ONLY
    assert by_id["rethlas"].specification.availability == ToolAvailability.EXTERNAL_BLOCKED
    assert by_id["archon"].specification.availability == (ToolAvailability.CONFIGURED_UNPROBED)
    catalog.set_availability(z3.key, ToolAvailability.UNAVAILABLE, now=NOW)
    catalog.set_availability(
        by_id["rethlas"].specification.key, ToolAvailability.AVAILABLE, now=NOW
    )
    refreshed = ProductToolRegistry(catalog).register_required(
        configured, probes=(probe,), now="2026-08-13T02:01:00Z"
    )
    refreshed_by_id = {entry.specification.tool_id: entry for entry in refreshed}
    assert refreshed_by_id["z3"].specification.availability == ToolAvailability.AVAILABLE
    assert (
        refreshed_by_id["rethlas"].specification.availability == ToolAvailability.EXTERNAL_BLOCKED
    )


def test_probe_cannot_borrow_another_provider_build_or_color_fixed_boundaries(
    tmp_path: Path,
) -> None:
    registry = ProductToolRegistry(ToolCatalogStore(database(tmp_path)))
    configured = required_bindings()
    lean = next(item for item in configured if item.tool_id == "lean")
    with pytest.raises(ProductToolAdapterError, match="provider build"):
        registry.register_required(
            configured,
            probes=(
                DeploymentProbe(
                    lean.key,
                    "some-other-provider",
                    lean.build_version,
                    lean.profile_id,
                    "borrowed-receipt",
                ),
            ),
            now=NOW,
        )
    with pytest.raises(ProductToolAdapterError, match="unregistered"):
        registry.register_required(
            configured,
            probes=(
                DeploymentProbe(
                    ("rethlas", "current-upstream", "verify_rethlas"),
                    "rethlas",
                    "old-success",
                    "old-profile",
                    "historical-receipt",
                ),
            ),
            now=NOW,
        )


def test_real_process_receipt_records_usage_cost_and_never_grants_authority(
    tmp_path: Path,
) -> None:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    tool = tmp_path / "exact_tool.py"
    tool.write_text(
        "import json,sys\n"
        "p=json.load(open(sys.argv[1],encoding='utf-8'))\n"
        "print(json.dumps({'double': p['n'] * 2},sort_keys=True))\n",
        encoding="utf-8",
    )
    input_path = workspace / "input.json"
    input_path.write_text('{"n":6}', encoding="utf-8")
    python = Path(sys.executable).resolve()
    script_digest = hashlib.sha256(tool.read_bytes()).hexdigest()
    python_digest = hashlib.sha256(python.read_bytes()).hexdigest()
    profile = AdapterProfile.from_mapping(
        {
            "name": "exact-process-profile",
            "version": "adapter-v9",
            "source_commit": script_digest,
            "timeout_seconds": 10,
            "max_response_bytes": 10_000,
            "env_whitelist": [],
            "argv_prefix": [str(python), str(tool)],
            "workspace_root": str(workspace),
            "binary_path": str(python),
            "binary_sha256": python_digest,
        }
    )
    adapter = RegisteredFileToolAdapter(
        profile,
        capability_kind="EXACT_ENUMERATION",
        trust_limit="HARD_ONLY_AFTER_CHECKER_REPLAY",
        output_mode="json",
    )
    declaration = binding(
        "exact-enumeration",
        "run_exact_enumeration",
        adapter_version="adapter-v9",
        build=script_digest,
        profile="exact-process-profile",
        ceiling=AuthorityCeiling.CERTIFICATE_REQUIRES_VALIDATION,
    )
    receipt = ProductAdapterBridge().execute(
        binding=declaration,
        adapter=adapter,
        request={
            "input_relpath": "input.json",
            "expected": {"double": 12},
            "environment": {},
        },
        tool_run_id="tool-run-real",
        attempt_id="attempt-real",
        accounting_artifact_id="accounting-real",
        output_artifact_ids=("output-real",),
        resource_usage=ResourceUsage(10, 1000, 20, 0),
        cost=CostObservation(Decimal("0"), "USD", "local-cost-receipt", None),
        public_summary="exact process completed against fixed expected output",
    )

    assert receipt.invocation_status == "SUCCEEDED"
    assert receipt.tool_receipt.status == "SUCCEEDED"
    assert "authority_effect" not in receipt.tool_receipt.payload
    assert receipt.accounting_record["authority_ceiling"] == ("CERTIFICATE_REQUIRES_VALIDATION")
    assert receipt.accounting_record["cost"] == {
        "amount": "0",
        "currency": "USD",
        "source_artifact_id": "local-cost-receipt",
        "unknown_reason": None,
    }
    assert receipt.tool_receipt.artifact_ids == ("accounting-real", "output-real")


def test_exit_zero_with_wrong_binding_is_failed_and_cannot_be_promoted() -> None:
    class ExitZeroWrongBinding:
        def run(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
            return {
                "adapter_name": "wrong-profile",
                "adapter_version": "adapter-v1",
                "source_commit": "f" * 64,
                "status": "COMPLETED",
                "exit_code": 0,
                "payload": {"claimed": "success"},
            }

    receipt = ProductAdapterBridge().execute(
        binding=binding("z3", "run_smt"),
        adapter=ExitZeroWrongBinding(),
        request={},
        tool_run_id="tool-run-wrong",
        attempt_id="attempt-wrong",
        accounting_artifact_id="accounting-wrong",
        output_artifact_ids=("output-wrong",),
        resource_usage=ResourceUsage(0, 0, 0, 0),
        cost=CostObservation.unknown("provider returned no cost"),
        public_summary="binding mismatch",
    )
    assert receipt.invocation_status == "FAILED"
    assert receipt.failure_code == "BINDING_MISMATCH"
    assert receipt.tool_receipt.payload["exit_code"] == 0
    assert receipt.tool_receipt.payload["failure_code"] == "BINDING_MISMATCH"


def test_responses_function_call_is_controller_intent_not_tool_success() -> None:
    declaration = binding(
        "deepseek-responses",
        "request_functions",
        provider="deepseek",
        model="deepseek-v4-pro",
        profile="deepseek-profile",
        build="d" * 64,
        schema=deepseek_responses_schema(),
    )

    class ResponsesAdapter:
        def run(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
            return {
                "adapter_name": "deepseek-profile",
                "adapter_version": "adapter-v1",
                "source_commit": "d" * 64,
                "status": "COMPLETED",
                "payload": {
                    "directives": [{"call_id": "call-1", "name": "run_smt", "arguments": {"n": 4}}],
                    "text": "request the registered checker",
                    "execution_claimed": False,
                },
                "usage": {
                    "input_tokens": 40,
                    "output_tokens": 8,
                    "reasoning_tokens": 3,
                    "total_tokens": 51,
                },
            }

    result = ModelFunctionBridge().execute(
        binding=declaration,
        adapter=ResponsesAdapter(),
        request={"prompt": "p", "model": "deepseek-v4-pro", "max_output_tokens": 100},
        tool_run_id="model-run",
        attempt_id="model-attempt",
        accounting_artifact_id="model-accounting",
        output_artifact_ids=("model-output",),
        resource_usage=ResourceUsage(1, 2, 3, 0),
        cost=CostObservation.unknown("provider response omitted invoice amount"),
        public_summary="one standard function directive returned",
    )
    assert result.execution.invocation_status == "SUCCEEDED"
    assert result.directives[0].function_name == "run_smt"
    assert result.downstream_tool_execution_count == 0
    assert result.execution.token_usage["total_tokens"] == 51
    assert result.execution.cost.unknown_reason == "provider response omitted invoice amount"


def test_model_claiming_execution_is_rejected_even_when_adapter_says_completed() -> None:
    declaration = binding(
        "deepseek-responses",
        "request_functions",
        provider="deepseek",
        model="deepseek-v4-pro",
        profile="deepseek-profile",
        build="d" * 64,
        schema=deepseek_responses_schema(),
    )

    class FalseExecutionClaim:
        def run(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
            return {
                "adapter_name": "deepseek-profile",
                "adapter_version": "adapter-v1",
                "source_commit": "d" * 64,
                "status": "COMPLETED",
                "payload": {"directives": [], "text": "done", "execution_claimed": True},
            }

    with pytest.raises(ProductToolAdapterError, match="were not executed"):
        ModelFunctionBridge().execute(
            binding=declaration,
            adapter=FalseExecutionClaim(),
            request={},
            tool_run_id="false-run",
            attempt_id="false-attempt",
            accounting_artifact_id="false-accounting",
            output_artifact_ids=("false-output",),
            resource_usage=ResourceUsage(0, 0, 0, 0),
            cost=CostObservation.unknown("no invoice"),
            public_summary="must fail",
        )
