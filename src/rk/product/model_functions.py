"""Model-function product seam: function intent is never reported as tool execution."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Any

from rk.product.compute import ResourceUsage
from rk.product.tool_adapters import (
    AdapterBinding,
    AdapterExecutionReceipt,
    CostObservation,
    ProductAdapterBridge,
    ProductToolAdapterError,
    RunnableAdapter,
)


@dataclass(frozen=True, slots=True)
class ModelFunctionDirective:
    call_id: str
    function_name: str
    arguments: Mapping[str, Any]


@dataclass(frozen=True, slots=True)
class ModelFunctionResult:
    execution: AdapterExecutionReceipt
    directives: tuple[ModelFunctionDirective, ...]
    public_text_available: bool
    downstream_tool_execution_count: int


class ModelFunctionBridge:
    """Run a model function surface while preserving the controller/executor split."""

    def __init__(self, adapter_bridge: ProductAdapterBridge | None = None) -> None:
        self._adapter_bridge = adapter_bridge or ProductAdapterBridge()

    def execute(
        self,
        *,
        binding: AdapterBinding,
        adapter: RunnableAdapter,
        request: Mapping[str, Any],
        tool_run_id: str,
        attempt_id: str,
        accounting_artifact_id: str,
        output_artifact_ids: Sequence[str],
        resource_usage: ResourceUsage,
        cost: CostObservation,
        public_summary: str,
    ) -> ModelFunctionResult:
        captured = _CapturingAdapter(adapter)
        execution = self._adapter_bridge.execute(
            binding=binding,
            adapter=captured,
            request=request,
            tool_run_id=tool_run_id,
            attempt_id=attempt_id,
            accounting_artifact_id=accounting_artifact_id,
            output_artifact_ids=output_artifact_ids,
            resource_usage=resource_usage,
            cost=cost,
            public_summary=public_summary,
        )
        result = captured.result
        payload = result.get("payload")
        directives: tuple[ModelFunctionDirective, ...] = ()
        public_text_available = False
        if execution.invocation_status == "SUCCEEDED":
            if not isinstance(payload, Mapping):
                raise ProductToolAdapterError("completed model function has no structured payload")
            if payload.get("execution_claimed") is not False:
                raise ProductToolAdapterError(
                    "model response must state that function directives were not executed"
                )
            directives = _directives(payload.get("directives", ()))
            public_text_available = isinstance(payload.get("text"), str) and bool(
                str(payload["text"]).strip()
            )
        return ModelFunctionResult(
            execution=execution,
            directives=directives,
            public_text_available=public_text_available,
            downstream_tool_execution_count=0,
        )


class _CapturingAdapter:
    def __init__(self, adapter: RunnableAdapter) -> None:
        self._adapter = adapter
        self.result: Mapping[str, Any] = {}

    def run(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        self.result = self._adapter.run(request)
        return self.result


def _directives(value: Any) -> tuple[ModelFunctionDirective, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise ProductToolAdapterError("model function directives are not an array")
    directives: list[ModelFunctionDirective] = []
    call_ids: set[str] = set()
    for item in value:
        if not isinstance(item, Mapping) or set(item) != {"call_id", "name", "arguments"}:
            raise ProductToolAdapterError("model function directive fields are not exact")
        call_id, function_name, arguments = item["call_id"], item["name"], item["arguments"]
        if (
            not isinstance(call_id, str)
            or not call_id
            or call_id in call_ids
            or not isinstance(function_name, str)
            or not function_name
            or not isinstance(arguments, Mapping)
        ):
            raise ProductToolAdapterError("model function directive is invalid")
        call_ids.add(call_id)
        directives.append(ModelFunctionDirective(call_id, function_name, dict(arguments)))
    return tuple(directives)


def deepseek_text_schema() -> Mapping[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["prompt", "model", "max_tokens"],
        "properties": {
            "prompt": {"type": "string", "minLength": 1},
            "model": {"const": "deepseek-v4-pro"},
            "max_tokens": {"type": "integer", "minimum": 1, "maximum": 8192},
        },
    }


def deepseek_responses_schema() -> Mapping[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["prompt", "model", "max_output_tokens"],
        "properties": {
            "prompt": {"type": "string", "minLength": 1},
            "model": {"const": "deepseek-v4-pro"},
            "max_output_tokens": {"type": "integer", "minimum": 1, "maximum": 32768},
        },
    }


__all__ = [
    "ModelFunctionBridge",
    "ModelFunctionDirective",
    "ModelFunctionResult",
    "deepseek_responses_schema",
    "deepseek_text_schema",
]
