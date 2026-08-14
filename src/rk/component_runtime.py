"""Host-owned component registry, schedule binding, execution, and metering.

The scheduler deliberately speaks in deployment-neutral component labels.  This module is the
seam that binds those labels and model function names to registered adapter implementations.
Callers never supply an executable, command line, endpoint, or trust class.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Any, Protocol

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

from rk.adapters.base import AdapterRequestError, canonical_json_sha256
from rk.config import KernelConfig
from rk.extensions import ExtensionRegistry, ToolReceipt
from rk.roles import MathRole, get_role_spec
from rk.scheduler import ScheduleDecision, ScheduledStep


class RunnableAdapter(Protocol):
    """The existing adapter interface consumed by the runtime."""

    name: str
    version: str

    def run(self, request: Mapping[str, Any]) -> Mapping[str, Any]: ...


class RuntimeComponentKind(StrEnum):
    MODEL = "MODEL"
    LEANSEARCH = "LEANSEARCH"
    JIXIA = "JIXIA"
    SMT = "SMT"
    CAS = "CAS"
    EXACT_ENUMERATION = "EXACT_ENUMERATION"
    LITERATURE = "LITERATURE"
    LEAN_REPLAY = "LEAN_REPLAY"
    LOCAL_PROOF_MODEL = "LOCAL_PROOF_MODEL"
    SOFT_VERIFIER = "SOFT_VERIFIER"


class ComponentRuntimeError(RuntimeError):
    """A registry, schedule, or execution invariant was violated."""


_REPAIRABLE_ADAPTER_STATUSES = frozenset(
    {
        "EXPECTATION_MISMATCH",
        "ADAPTER_SCHEMA_MISMATCH",
    }
)


class UnknownComponent(ComponentRuntimeError):
    """No host registration covers the requested component or function."""


class _StructuredRoleAdapter:
    """Turn a text model response into the orchestration result contract."""

    def __init__(self, adapter: RunnableAdapter) -> None:
        self._adapter = adapter
        self.name = adapter.name
        self.version = adapter.version

    def run(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        raw = dict(self._adapter.run(request))
        if raw.get("status") != "COMPLETED":
            return raw
        payload = raw.get("payload")
        text = payload.get("text") if isinstance(payload, Mapping) else None
        if not isinstance(text, str):
            return {**raw, "status": "ADAPTER_SCHEMA_MISMATCH", "payload": None}
        candidate = text.strip()
        if candidate.startswith("```"):
            lines = candidate.splitlines()
            if lines and lines[0].strip().lower() in {"```", "```json"}:
                lines = lines[1:]
            if lines and lines[-1].strip() == "```":
                lines = lines[:-1]
            candidate = "\n".join(lines).strip()
        try:
            parsed = json.loads(candidate)
        except (TypeError, ValueError):
            return {
                **raw,
                "status": "ADAPTER_SCHEMA_MISMATCH",
                "payload": {
                    "open_obligations": ["ROLE_OUTPUT_NOT_JSON"],
                    "model_text": text,
                },
            }
        if not isinstance(parsed, Mapping):
            return {**raw, "status": "ADAPTER_SCHEMA_MISMATCH", "payload": None}
        parsed_payload = parsed.get("payload")
        if not isinstance(parsed_payload, Mapping):
            parsed_payload = {
                key: value
                for key, value in parsed.items()
                if key not in {"status", "artifact_ids", "tool_requests"}
            }
        artifact_ids = parsed.get("artifact_ids", [])
        tool_requests = parsed.get("tool_requests", [])
        if not isinstance(artifact_ids, list) or not all(
            isinstance(item, str) for item in artifact_ids
        ):
            return {**raw, "status": "ADAPTER_SCHEMA_MISMATCH", "payload": None}
        if not isinstance(tool_requests, list) or not all(
            isinstance(item, Mapping) for item in tool_requests
        ):
            return {**raw, "status": "ADAPTER_SCHEMA_MISMATCH", "payload": None}
        return {
            **raw,
            "status": str(parsed.get("status", "COMPLETED")),
            "payload": dict(parsed_payload),
            "artifact_ids": artifact_ids,
            "tool_requests": [dict(item) for item in tool_requests],
            "model_text": text,
        }


RequestBuilder = Callable[[Mapping[str, Any], Mapping[str, str]], Mapping[str, Any]]


def _default_builder(
    arguments: Mapping[str, Any], environment: Mapping[str, str]
) -> Mapping[str, Any]:
    request = dict(arguments)
    request["environment"] = dict(environment)
    return request


def without_environment(
    arguments: Mapping[str, Any], _environment: Mapping[str, str]
) -> Mapping[str, Any]:
    """Request builder for adapters such as LeanSearch and literature search."""

    return dict(arguments)


@dataclass(frozen=True, slots=True)
class ComponentRegistration:
    """One host-owned adapter registration.

    ``function_schema`` is the only model-controlled surface.  ``request_builder`` is trusted
    host code and may add deployment-owned fields, but must not introduce a caller command line.
    """

    component_id: str
    kind: RuntimeComponentKind
    adapter: RunnableAdapter
    function_name: str
    description: str
    function_schema: Mapping[str, Any]
    scheduled_names: tuple[str, ...] = ()
    orchestration_kinds: tuple[str, ...] = ()
    request_builder: RequestBuilder = _default_builder

    def __post_init__(self) -> None:
        if not self.component_id or not self.function_name or not self.description.strip():
            raise ValueError("component id, function name, and description are required")
        if self.function_schema.get("type") != "object":
            raise ValueError("component function schema must describe an object")
        try:
            Draft202012Validator.check_schema(dict(self.function_schema))
        except SchemaError as error:
            raise ValueError("component function schema is invalid") from error


@dataclass(frozen=True, slots=True)
class RuntimePlanStep:
    ordinal: int
    scheduled: ScheduledStep
    component_ids: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ordinal": self.ordinal,
            "scheduled": self.scheduled.to_dict(),
            "component_ids": list(self.component_ids),
        }


@dataclass(frozen=True, slots=True)
class RuntimePlan:
    schedule_digest: str
    steps: tuple[RuntimePlanStep, ...]
    missing_steps: tuple[str, ...]

    @property
    def runnable(self) -> bool:
        return not self.missing_steps

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "rk.runtime-plan.v1",
            "schedule_digest": self.schedule_digest,
            "steps": [step.to_dict() for step in self.steps],
            "missing_steps": list(self.missing_steps),
            "runnable": self.runnable,
        }


@dataclass(frozen=True, slots=True)
class ComponentReceipt:
    receipt_id: str
    call_id: str
    component_id: str
    function_name: str
    status: str
    result: Mapping[str, Any]
    usage: Mapping[str, int]
    wall_time_ms: int
    started_ns: int
    finished_ns: int

    @property
    def succeeded(self) -> bool:
        return self.status == "COMPLETED"

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "rk.component-receipt.v1",
            "receipt_id": self.receipt_id,
            "call_id": self.call_id,
            "component_id": self.component_id,
            "function_name": self.function_name,
            "status": self.status,
            "result": dict(self.result),
            "usage": dict(self.usage),
            "wall_time_ms": self.wall_time_ms,
            "started_ns": self.started_ns,
            "finished_ns": self.finished_ns,
        }


class ComponentRuntime:
    """Resolve and execute only host-registered components through one small interface."""

    def __init__(
        self,
        registrations: Sequence[ComponentRegistration],
        extensions: ExtensionRegistry | None = None,
    ) -> None:
        if not registrations:
            raise ValueError("at least one component registration is required")
        by_id: dict[str, ComponentRegistration] = {}
        by_function: dict[str, ComponentRegistration] = {}
        by_schedule: dict[str, list[str]] = {}
        by_orchestration: dict[str, list[str]] = {}
        for registration in registrations:
            if registration.component_id in by_id:
                raise ValueError(f"duplicate component id: {registration.component_id}")
            if registration.function_name in by_function:
                raise ValueError(f"duplicate function name: {registration.function_name}")
            by_id[registration.component_id] = registration
            by_function[registration.function_name] = registration
            for name in registration.scheduled_names:
                by_schedule.setdefault(name, []).append(registration.component_id)
            for work_kind in registration.orchestration_kinds:
                by_orchestration.setdefault(work_kind, []).append(registration.component_id)
        self._by_id = MappingProxyType(by_id)
        self._by_function = MappingProxyType(by_function)
        self._by_schedule = MappingProxyType(
            {name: tuple(component_ids) for name, component_ids in by_schedule.items()}
        )
        self._by_orchestration = MappingProxyType(
            {name: tuple(component_ids) for name, component_ids in by_orchestration.items()}
        )
        self._extensions = extensions or ExtensionRegistry()

    def function_definitions(self) -> Mapping[str, Mapping[str, Any]]:
        """Return the exact standard-function surface safe to expose to a controller model."""

        return MappingProxyType(
            {
                name: MappingProxyType(
                    {
                        "description": registration.description.strip(),
                        "parameters": dict(registration.function_schema),
                    }
                )
                for name, registration in self._by_function.items()
            }
        )

    def bind_schedule(self, decision: ScheduleDecision) -> RuntimePlan:
        """Bind every scheduler step to concrete registrations without silently substituting."""

        steps: list[RuntimePlanStep] = []
        missing: list[str] = []
        for ordinal, scheduled in enumerate(decision.steps):
            component_ids = self._by_schedule.get(scheduled.component, ())
            if not component_ids:
                missing.append(scheduled.component)
            steps.append(RuntimePlanStep(ordinal, scheduled, component_ids))
        return RuntimePlan(decision.plan_digest, tuple(steps), tuple(missing))

    def execute_function(
        self,
        *,
        call_id: str,
        function_name: str,
        arguments: Mapping[str, Any],
        environment: Mapping[str, str],
    ) -> ComponentReceipt:
        """Validate, run, and meter one registered model directive.

        A receipt is returned for adapter-declared failures as well as successes.  Unknown
        functions and invalid arguments fail before any adapter can run.
        """

        if not call_id:
            raise ComponentRuntimeError("call_id is required")
        registration = self._by_function.get(function_name)
        if registration is None:
            raise UnknownComponent(f"unregistered function: {function_name}")
        try:
            Draft202012Validator(dict(registration.function_schema)).validate(dict(arguments))
        except ValidationError as error:
            raise ComponentRuntimeError("function arguments do not match the registry") from error
        request = registration.request_builder(dict(arguments), dict(environment))
        if not isinstance(request, Mapping):
            raise ComponentRuntimeError("registered request builder returned a non-object")
        started_ns = time.time_ns()
        monotonic_started = time.monotonic_ns()
        try:
            raw = registration.adapter.run(request)
            if not isinstance(raw, Mapping):
                raise TypeError("adapter result is not an object")
            result: Mapping[str, Any] = dict(raw)
        except AdapterRequestError as error:
            result = {
                "status": "RUNTIME_EXCEPTION",
                "payload": {"repairable_tool_request": True},
                "error_type": type(error).__name__,
                "error": str(error),
            }
        except (OSError, TimeoutError, ValueError, TypeError) as error:
            result = {
                "status": "RUNTIME_EXCEPTION",
                "payload": None,
                "error_type": type(error).__name__,
                "error": str(error),
            }
        finished_ns = time.time_ns()
        wall_time_ms = max(0, (time.monotonic_ns() - monotonic_started) // 1_000_000)
        status = str(result.get("status", "ADAPTER_SCHEMA_MISMATCH"))
        if status in _REPAIRABLE_ADAPTER_STATUSES:
            payload = result.get("payload")
            repairable_payload = dict(payload) if isinstance(payload, Mapping) else {}
            repairable_payload["repairable_tool_request"] = True
            result = {**dict(result), "payload": repairable_payload}
        usage = self._normalize_usage(result.get("usage"), wall_time_ms)
        receipt_material = {
            "call_id": call_id,
            "component_id": registration.component_id,
            "function_name": function_name,
            "status": status,
            "result_hash": canonical_json_sha256(result),
            "started_ns": started_ns,
            "finished_ns": finished_ns,
        }
        receipt_id = hashlib.sha256(
            json.dumps(receipt_material, sort_keys=True, separators=(",", ":")).encode()
        ).hexdigest()
        receipt = ComponentReceipt(
            receipt_id=receipt_id,
            call_id=call_id,
            component_id=registration.component_id,
            function_name=function_name,
            status=status,
            result=result,
            usage=usage,
            wall_time_ms=wall_time_ms,
            started_ns=started_ns,
            finished_ns=finished_ns,
        )
        if registration.component_id in self._extensions.tool_receipt_consumers:
            artifact_ids = result.get("artifact_ids", ())
            self._extensions.consume_tool_receipt(
                registration.component_id,
                ToolReceipt(
                    tool_run_id=receipt.receipt_id,
                    attempt_id=call_id,
                    status=receipt.status,
                    payload=MappingProxyType(
                        {
                            "component_id": registration.component_id,
                            "function_name": function_name,
                            "result_digest": canonical_json_sha256(result),
                            "usage": dict(usage),
                        }
                    ),
                    artifact_ids=(
                        tuple(str(item) for item in artifact_ids)
                        if isinstance(artifact_ids, list)
                        and all(isinstance(item, str) for item in artifact_ids)
                        else ()
                    ),
                ),
            )
        return receipt

    def execute_component(
        self,
        component_id: str,
        request: Mapping[str, Any],
        *,
        environment: Mapping[str, str],
        call_id: str,
    ) -> ComponentReceipt:
        """Run a concrete registration selected from a bound scheduler plan."""

        registration = self._by_id.get(component_id)
        if registration is None:
            raise UnknownComponent(f"unregistered component: {component_id}")
        return self.execute_function(
            call_id=call_id,
            function_name=registration.function_name,
            arguments=request,
            environment=environment,
        )

    def execute(self, request: Any) -> Mapping[str, Any]:
        """Satisfy the orchestrator's single ``execute(ComponentRequest)`` seam.

        Ordinary work kinds bind through ``orchestration_kinds``.  A ``TOOL_REQUEST`` binds only
        when its host-recorded ``tool`` or ``operation`` names a registered component/function.
        The runtime never interprets either field as an executable command.
        """

        work_kind = getattr(request, "work_kind", None)
        inputs = getattr(request, "inputs", None)
        request_id = getattr(request, "request_id", None)
        if not isinstance(work_kind, str) or not isinstance(request_id, str) or not request_id:
            raise ComponentRuntimeError("orchestrator request identity is invalid")
        if not isinstance(inputs, Mapping):
            raise ComponentRuntimeError("orchestrator inputs must be an object")
        arguments: Mapping[str, Any]
        if work_kind == "TOOL_REQUEST":
            tool_request = inputs.get("tool_request")
            if not isinstance(tool_request, Mapping):
                raise ComponentRuntimeError("tool request payload is missing")
            selectors = (tool_request.get("operation"), tool_request.get("tool"))
            registration = None
            for selector in selectors:
                if isinstance(selector, str):
                    registration = self._by_function.get(selector) or self._by_id.get(selector)
                    if registration is not None:
                        break
            if registration is None:
                raise UnknownComponent("orchestrator tool request is not registered")
            payload = tool_request.get("payload", {})
            if not isinstance(payload, Mapping):
                raise ComponentRuntimeError("orchestrator tool payload must be an object")
            arguments = payload
        else:
            component_ids = self._by_orchestration.get(work_kind, ())
            if len(component_ids) != 1:
                raise UnknownComponent(
                    f"orchestration work kind requires exactly one registration: {work_kind}"
                )
            registration = self._by_id[component_ids[0]]
            arguments = {
                "request_id": request_id,
                "run_id": getattr(request, "run_id", ""),
                "work_kind": work_kind,
                "role": getattr(request, "role", None),
                "route_id": getattr(request, "route_id", None),
                "round": getattr(request, "round", 0),
                "contract_scope": dict(getattr(request, "contract_scope", {})),
                "inputs": dict(inputs),
                "budget_remaining": dict(getattr(request, "budget_remaining", {})),
                "hardware_plan": dict(getattr(request, "hardware_plan", {})),
            }
        try:
            receipt = self.execute_function(
                call_id=request_id,
                function_name=registration.function_name,
                arguments=arguments,
                environment={},
            )
        except ComponentRuntimeError as exc:
            # A controller-produced standard-function call can be malformed.  This is tool
            # feedback for that route, not a host outage: return it through the ordinary
            # checkpoint loop so the controller can repair only the rejected call.
            now = time.time_ns()
            return {
                "status": "RUNTIME_EXCEPTION",
                "payload": {"error": str(exc), "repairable_tool_request": True},
                "usage": {"wall_time_ms": 0},
                "component_receipt_id": canonical_json_sha256(
                    {
                        "request_id": request_id,
                        "component_id": registration.component_id,
                        "error": str(exc),
                    }
                ),
                "component_id": registration.component_id,
                "component_started_ns": now,
                "component_finished_ns": now,
            }
        result = dict(receipt.result)
        if work_kind == "TOOL_REQUEST":
            payload = result.get("payload")
            payload = dict(payload) if isinstance(payload, Mapping) else {}
            provenance = {
                key: value
                for key, value in receipt.result.items()
                if key
                not in {
                    "status",
                    "payload",
                    "usage",
                    "transient_execution_output",
                    "tools",
                    "tool_surface",
                    "policy_violation",
                }
            }
            if provenance:
                payload["_component_provenance"] = provenance
            result["payload"] = payload
        if receipt.status == "RUNTIME_EXCEPTION":
            error = receipt.result.get("error")
            if isinstance(error, str) and error:
                payload = result.get("payload")
                payload = dict(payload) if isinstance(payload, Mapping) else {}
                payload["error"] = error
                if receipt.result.get("error_type") == "AdapterRequestError":
                    payload["repairable_tool_request"] = True
                result["payload"] = payload
        result["usage"] = dict(receipt.usage)
        result["component_receipt_id"] = receipt.receipt_id
        result["component_id"] = receipt.component_id
        result["component_started_ns"] = receipt.started_ns
        result["component_finished_ns"] = receipt.finished_ns
        return result

    @staticmethod
    def _normalize_usage(value: Any, wall_time_ms: int) -> Mapping[str, int]:
        raw = value if isinstance(value, Mapping) else {}

        def counter(name: str) -> int:
            item = raw.get(name, 0)
            return item if isinstance(item, int) and not isinstance(item, bool) and item >= 0 else 0

        return MappingProxyType(
            {
                "input_tokens": counter("input_tokens"),
                "output_tokens": counter("output_tokens"),
                "reasoning_tokens": counter("reasoning_tokens"),
                "total_tokens": counter("total_tokens"),
                "wall_time_ms": wall_time_ms,
            }
        )


_ORCHESTRATION_REQUEST_SCHEMA: Mapping[str, Any] = MappingProxyType(
    {
        "type": "object",
        "properties": {
            "request_id": {"type": "string"},
            "run_id": {"type": "string"},
            "work_kind": {"type": "string"},
            "role": {"type": ["string", "null"]},
            "route_id": {"type": ["string", "null"]},
            "round": {"type": "integer", "minimum": 0},
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
    }
)


_WORK_PAYLOAD_GUIDANCE: Mapping[str, Mapping[str, Any]] = MappingProxyType(
    {
        "CONTRACT_CLARIFY": {
            "ambiguous": "boolean",
            "questions": ["only questions whose answers change the mathematical statement"],
            "normalized_statement": "string",
        },
        "LITERATURE_AUDIT": {
            "exact_match": "boolean",
            "references": [],
            "open_obligations": [],
        },
        "ROUTE_SCOUT": {
            "routes": [
                {
                    "route_id": "short stable id",
                    "label": "human-readable route",
                    "representation": "mathematical representation",
                    "tool_family": "distinct verification/tool family",
                    "key_lemma": "main load-bearing lemma",
                    "method_card_id": "registered method-card id",
                    "proof_skeleton": ["at least two load-bearing steps"],
                    "sharp_example": "boundary case where the claim is tight",
                    "near_miss": "nearby false statement and witness",
                    "fast_falsifier": "cheapest decisive attack",
                    "sentinel_result": {"status": "PASSED or REFUTED", "case_id": "id"},
                    "independence_profile": {
                        "idea_family": "family id",
                        "derivation_family": "family id",
                        "verification_family": "family id",
                        "implementation_family": "family id",
                        "retrieval_family": "family id",
                    },
                }
            ]
        },
        "FALSIFY_ROUTE": {"falsified": "boolean", "candidate_kind": "", "open_obligations": []},
        "DEVELOP_ROUTE": {
            "candidate_kind": "PROOF or COUNTEREXAMPLE",
            "derivation": [],
            "open_obligations": [],
        },
        "GAP_REVIEW": {"verdict": "PASS, REVISE, GAP, or FATAL", "open_obligations": []},
        "REVISE_ROUTE": {"revised_derivation": [], "open_obligations": []},
        "FORMALIZE_ROUTE": {
            "lean_statement": "string",
            "lean_candidate": (
                "complete Lean source defining a named theorem; never use an unnamed example"
            ),
            "verification_requests": [],
            "open_obligations": [],
        },
        "SEMANTIC_AUDIT": {"faithful": "boolean", "mismatches": [], "open_obligations": []},
        "COMPOSITION_CHECK": {"closed": "boolean", "open_obligations": []},
        "REVISE_COMPOSITION": {"revised_composition": [], "open_obligations": []},
        "FINAL_SYNTHESIS": {
            "summary": "string",
            "proved": [],
            "disproved": [],
            "unresolved": [],
        },
    }
)


def _profile(config: KernelConfig, key: str) -> Mapping[str, Any] | None:
    raw = config.adapter_profiles.get(key)
    return raw if isinstance(raw, Mapping) else None


def build_component_runtime(
    config: KernelConfig, environment: Mapping[str, str]
) -> ComponentRuntime:
    """Build the production registry from real deployment adapter profiles."""

    from rk.adapters import (
        AdapterProfile,
        CrossrefLiteratureAdapter,
        JixiaAdapter,
        LeanReplayAdapter,
        LeanSearchAdapter,
        LocalProofModelAdapter,
        OpenAICompatibleAdapter,
        RegisteredFileToolAdapter,
        RethlasAdapter,
    )

    model_profile = _profile(config, "research-model")
    model_name = config.product.get("model")
    if model_profile is None or not isinstance(model_name, str) or not model_name:
        raise ComponentRuntimeError(
            "管理员尚未配置 research-model 与 product.model, 研究无法启动"
        )
    captured_environment = MappingProxyType(dict(environment))

    def environment_builder(allowed: frozenset[str]) -> RequestBuilder:
        selected = MappingProxyType(
            {
                name: value
                for name, value in captured_environment.items()
                if name in allowed
            }
        )

        def build(
            arguments: Mapping[str, Any], _environment: Mapping[str, str]
        ) -> Mapping[str, Any]:
            return _default_builder(arguments, selected)

        return build

    model_adapter_profile = AdapterProfile.from_mapping(model_profile)
    model_environment = MappingProxyType(
        {
            name: value
            for name, value in captured_environment.items()
            if name in model_adapter_profile.env_whitelist
        }
    )
    model_adapter = _StructuredRoleAdapter(OpenAICompatibleAdapter(model_adapter_profile))
    registrations: list[ComponentRegistration] = []

    def build_role_request(
        arguments: Mapping[str, Any], _environment: Mapping[str, str]
    ) -> Mapping[str, Any]:
        try:
            specification = get_role_spec(MathRole(str(arguments.get("role"))))
        except ValueError as error:
            raise AdapterRequestError("orchestration role is not registered") from error
        task = {
            "task": dict(arguments),
            "required_payload_shape": dict(
                _WORK_PAYLOAD_GUIDANCE.get(str(arguments.get("work_kind")), {})
            ),
            "available_tools": [
                {
                    "component_id": item.component_id,
                    "operation": item.function_name,
                    "description": item.description,
                    "parameters": dict(item.function_schema),
                }
                for item in registrations
                if item.kind is not RuntimeComponentKind.MODEL
            ],
            "response_contract": {
                "status": "COMPLETED",
                "payload": "exactly follow required_payload_shape",
                "artifact_ids": [],
                "tool_requests": [
                    {
                        "request_key": "unique-name",
                        "tool": "registered-component-id",
                        "operation": "registered-function-name",
                        "payload": {},
                        "required": True,
                    }
                ],
            },
        }
        work_kind = str(arguments.get("work_kind"))
        inputs = arguments.get("inputs")
        has_tool_feedback = isinstance(inputs, Mapping) and bool(inputs.get("tool_feedback"))
        tool_instruction = ""
        if work_kind == "ROUTE_SCOUT":
            tool_instruction = (
                "\n每条路线必须引用输入中的 method card, 给至少两步证明骨架、锋利例、"
                "近失配、最快证伪器和已执行 sentinel 结果, 并声明五维来源 family。"
                "编排器会按五维签名去重, 缺字段、未执行 sentinel 或同源复制均不晋级。"
            )
        elif work_kind == "LITERATURE_AUDIT" and not has_tool_feedback:
            tool_instruction = (
                "\n若 available_tools 含 search_literature, 你必须先只返回一个 required "
                "tool_request 调用它; 收到 tool_feedback 后再判断是否存在精确同题结果。"
            )
        elif work_kind == "FORMALIZE_ROUTE" and not has_tool_feedback:
            tool_instruction = (
                "\n若 available_tools 含 search_lean, 先请求检索最相关的 Mathlib 声明; "
                "收到结果后再给 Lean 陈述与候选。"
            )
        elif work_kind == "GAP_REVIEW" and not has_tool_feedback:
            tool_instruction = (
                "\n若 available_tools 含 verify_rethlas, 必须先把当前原子 Claim 与证明交给"
                " Rethlas 取得批评和修复提示; 该结果永远是 SOFT_MODEL, 即使返回 correct"
                " 也不能独立接受 Claim。"
            )
        elif work_kind == "FALSIFY_ROUTE" and not has_tool_feedback:
            tool_instruction = (
                "\n若 available_tools 含 research-smt 或 research-enumeration, 必须按当前 "
                "Claim 类型选择至少一个最低成本反例工具并先返回 required tool_request; "
                "有限范围无反例只能报告范围内未命中, 不能报告全称成立。"
            )
        elif work_kind in {"DEVELOP_ROUTE", "REVISE_ROUTE"} and not has_tool_feedback:
            tool_instruction = (
                "\n若 available_tools 含 research-cas 且路线含代数恒等式或符号化简, 必须先 "
                "请求 CAS; CAS 结果仅作候选/反例线索, 除非另有独立 checker, 不得冒充证明。"
            )
        if work_kind in {"DEVELOP_ROUTE", "FORMALIZE_ROUTE"} and not has_tool_feedback:
            tool_instruction += (
                "\n若 hardware_plan 把 QED-Nano 或 DeepSeek-Prover 放置为候选生成器且"
                " available_tools 含 run_local_proof_model, 必须先请求该生成器; 其输出只作为"
                "当前 Claim 的软候选, 仍须经过找缝、Lean/checker/受管同行验证。"
            )
        return {
            "prompt": (
                specification.system_prompt
                + "\n只输出一个 JSON 对象, 不要使用代码围栏。"
                + "不要输出未由调用消息提供的哈希或内部标识。以下是本次任务:\n"
                + json.dumps(task, ensure_ascii=False, sort_keys=True)
                + tool_instruction
            ),
            "model": model_name,
            "max_tokens": int(config.product.get("model_max_tokens", 8192)),
            "environment": dict(model_environment),
        }

    ordinary_kinds = (
        "CONTRACT_CLARIFY", "LITERATURE_AUDIT", "ROUTE_SCOUT", "FALSIFY_ROUTE",
        "DEVELOP_ROUTE", "GAP_REVIEW", "REVISE_ROUTE", "FORMALIZE_ROUTE",
        "SEMANTIC_AUDIT", "COMPOSITION_CHECK", "REVISE_COMPOSITION", "FINAL_SYNTHESIS",
    )
    registrations.append(
        ComponentRegistration(
            component_id=str(config.product.get("model_component_id", "research-model")),
            kind=RuntimeComponentKind.MODEL,
            adapter=model_adapter,
            function_name="ask_mathematical_role",
            description="运行一个受合同约束的数学研究角色",
            function_schema=_ORCHESTRATION_REQUEST_SCHEMA,
            scheduled_names=("API candidate model",),
            orchestration_kinds=ordinary_kinds,
            request_builder=build_role_request,
        )
    )

    def add(
        key: str,
        kind: RuntimeComponentKind,
        factory: Callable[[Any], RunnableAdapter],
        function_name: str,
        description: str,
        schema: Mapping[str, Any],
        *,
        scheduled_names: tuple[str, ...] = (),
        builder: RequestBuilder = _default_builder,
    ) -> None:
        raw = _profile(config, key)
        if raw is None:
            return
        adapter_profile = AdapterProfile.from_mapping(raw)
        effective_builder = builder
        if builder is _default_builder:
            component_environment = MappingProxyType(
                {
                    name: value
                    for name, value in captured_environment.items()
                    if name in adapter_profile.env_whitelist
                }
            )

            def configured_builder(
                arguments: Mapping[str, Any],
                _environment: Mapping[str, str],
                *,
                _captured: Mapping[str, str] = component_environment,
            ) -> Mapping[str, Any]:
                return _default_builder(arguments, _captured)

            effective_builder = configured_builder
        registrations.append(
            ComponentRegistration(
                component_id=key,
                kind=kind,
                adapter=factory(adapter_profile),
                function_name=function_name,
                description=description,
                function_schema=schema,
                scheduled_names=scheduled_names,
                request_builder=effective_builder,
            )
        )

    add(
        "research-search", RuntimeComponentKind.LEANSEARCH, LeanSearchAdapter, "search_lean",
        "在 Lean/Mathlib 语料中检索前提候选",
        {
            "type": "object",
            "properties": {
                "query": {"type": "array", "items": {"type": "string"}, "minItems": 1},
                "num_results": {"type": "integer", "minimum": 1, "maximum": 50},
                "rerank": {"type": "boolean"},
                "retrieve_k": {"type": ["integer", "null"]},
            },
            "required": ["query", "num_results", "rerank", "retrieve_k"],
            "additionalProperties": False,
        },
        scheduled_names=(
            "public LeanSearch retriever", "local LeanSearch retriever",
            "LeanSearch embedding/retrieval", "LeanSearch reranker",
        ),
        builder=without_environment,
    )
    add(
        "research-literature", RuntimeComponentKind.LITERATURE, CrossrefLiteratureAdapter,
        "search_literature", "检索论文与书目候选",
        {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "rows": {"type": "integer", "minimum": 1, "maximum": 20},
            },
            "required": ["query", "rows"],
            "additionalProperties": False,
        },
        builder=without_environment,
    )
    add(
        "research-rethlas",
        RuntimeComponentKind.SOFT_VERIFIER,
        RethlasAdapter,
        "verify_rethlas",
        "用 Rethlas 批评自然语言证明(永久 SOFT_MODEL)",
        {
            "type": "object",
            "properties": {"statement": {"type": "string"}, "proof": {"type": "string"}},
            "required": ["statement", "proof"],
            "additionalProperties": False,
        },
        builder=without_environment,
    )
    add(
        "research-jixia", RuntimeComponentKind.JIXIA, JixiaAdapter, "analyze_lean",
        "提取 Lean 声明、符号、位置与证明状态",
        {
            "type": "object",
            "properties": {
                "source_relpath": {"type": "string"},
                "output_relpath": {"type": "string"},
                "include_initializers": {"type": "boolean"},
            },
            "required": ["source_relpath", "output_relpath", "include_initializers"],
            "additionalProperties": False,
        },
        scheduled_names=("jixia structural analysis",),
    )
    add(
        "research-lean", RuntimeComponentKind.LEAN_REPLAY, LeanReplayAdapter, "replay_lean",
        "用固定 Lean 环境重放候选声明并审计公理",
        {
            "type": "object",
            "properties": {
                "source_relpath": {"type": "string"},
                "output_relpath": {"type": "string"},
                "declarations": {
                    "type": "array", "items": {"type": "string"},
                    "minItems": 1, "uniqueItems": True,
                },
            },
            "required": ["source_relpath", "output_relpath", "declarations"],
            "additionalProperties": False,
        },
        scheduled_names=("Lean clean replay and axiom audit",),
    )
    add(
        "research-local-proof-model", RuntimeComponentKind.LOCAL_PROOF_MODEL,
        LocalProofModelAdapter, "run_local_proof_model", "运行本地证明模型生成软候选",
        {
            "type": "object",
            "properties": {
                "model": {"enum": ["qed-nano", "deepseek-prover"]},
                "prompt": {"type": "string", "minLength": 1},
                "max_new_tokens": {"type": "integer", "minimum": 1, "maximum": 1024},
                "output_relpath": {"type": "string"},
            },
            "required": ["model", "prompt", "max_new_tokens", "output_relpath"],
            "additionalProperties": False,
        },
        scheduled_names=("QED-Nano candidate", "DeepSeek-Prover candidate"),
    )
    for key, kind, capability, function_name in (
        ("research-smt", RuntimeComponentKind.SMT, "SMT", "run_smt"),
        ("research-cas", RuntimeComponentKind.CAS, "CAS", "run_cas"),
        (
            "research-enumeration", RuntimeComponentKind.EXACT_ENUMERATION,
            "EXACT_ENUMERATION", "run_exact_enumeration",
        ),
    ):
        raw = _profile(config, key)
        if raw is None:
            continue
        settings_by_key = config.product.get("deterministic_tools", {})
        settings = settings_by_key.get(key, {}) if isinstance(settings_by_key, Mapping) else {}
        settings = settings if isinstance(settings, Mapping) else {}
        adapter_profile = AdapterProfile.from_mapping(raw)
        adapter = RegisteredFileToolAdapter(
            adapter_profile,
            capability_kind=capability,
            trust_limit=str(settings.get("trust_limit", "SOFT_TOOL_RESULT")),
            output_mode=str(settings.get("output_mode", "json")),
        )
        registrations.append(
            ComponentRegistration(
                component_id=key,
                kind=kind,
                adapter=adapter,
                function_name=function_name,
                description=f"运行已注册的 {capability} 检查",
                function_schema={
                    "type": "object",
                    "properties": {
                        "input": dict(settings.get("input_schema", {"type": "object"})),
                        "expected": dict(settings.get("expected_schema", {})),
                    },
                    "required": ["input", "expected"],
                    "additionalProperties": False,
                },
                scheduled_names=("registered CAS/SMT probes",),
                request_builder=environment_builder(adapter_profile.env_whitelist),
            )
        )
    return ComponentRuntime(registrations)
