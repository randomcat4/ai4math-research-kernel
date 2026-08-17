# ruff: noqa: RUF001, RUF005
"""Event-driven mathematical research orchestration.

This module is deliberately not a mathematical authority path.  It coordinates soft roles and
registered tools, records what happened, and stops at explicit gates.  Promotion of any artifact
to a kernel, peer, or composition verdict remains the responsibility of ``ResearchKernel``.

The external seam of this module is small: a deployment supplies one ``ComponentRuntime`` and
callers start, advance, and resume a serializable checkpoint.  Model providers, premise search,
Lean, SMT, CAS, enumeration, and human-facing harnesses all sit behind that one runtime seam.
"""

from __future__ import annotations

import hashlib
import json
import mimetypes
import os
import re
import time
from collections.abc import Callable, Mapping, Sequence
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field, replace
from datetime import datetime
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

from rk.extensions import AuthorityInvalidation, ExtensionRegistry
from rk.roles import MathRole
from rk.runtime import SystemClock, Uuid7Generator, format_utc
from rk.scheduler import (
    HardwareMode,
    ScheduleRequest,
    detect_local_inventory,
    place_registered_work,
    schedule_research,
)
from rk.wire import canonical_json_bytes

if TYPE_CHECKING:
    from rk.config import KernelConfig
    from rk.domain import VerifiedCapability
    from rk.kernel import ResearchKernel


JsonMap = Mapping[str, Any]


def _frozen(value: Mapping[str, Any] | None = None) -> JsonMap:
    return MappingProxyType(dict(value or {}))


def _canonical(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest(value: object) -> str:
    return hashlib.sha256(_canonical(value).encode("utf-8")).hexdigest()


def _lean_declaration_name(source: str) -> str | None:
    match = re.search(
        r"(?m)^\s*(?:private\s+)?(?:theorem|lemma|def|example)\s+"
        r"([A-Za-z_][A-Za-z0-9_'.]*)?",
        source,
    )
    if match is None:
        return None
    name = match.group(1)
    if not name or name == "example":
        return None
    namespaces: list[str] = []
    for line in source[: match.start()].splitlines():
        opening = re.match(r"^\s*namespace\s+([A-Za-z_][A-Za-z0-9_'.]*)\s*$", line)
        if opening:
            namespaces.append(opening.group(1))
            continue
        if re.match(r"^\s*end(?:\s+[A-Za-z_][A-Za-z0-9_'.]*)?\s*$", line) and namespaces:
            namespaces.pop()
    return ".".join((*namespaces, name)) if namespaces else name


class OrchestrationStatus(StrEnum):
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    WAITING_HUMAN_REVIEW = "WAITING_HUMAN_REVIEW"
    COMPLETED = "COMPLETED"


class ResearchPhase(StrEnum):
    CONTRACT = "CONTRACT"
    LITERATURE = "LITERATURE"
    ROUTES = "ROUTES"
    COMPOSITION = "COMPOSITION"
    HUMAN_REVIEW = "HUMAN_REVIEW"
    SYNTHESIS = "SYNTHESIS"
    COMPLETE = "COMPLETE"


class PauseReason(StrEnum):
    USER_REQUEST = "USER_REQUEST"
    CONTRACT_AMBIGUOUS = "CONTRACT_AMBIGUOUS"
    BUDGET_EXHAUSTED = "BUDGET_EXHAUSTED"
    RUNTIME_UNAVAILABLE = "RUNTIME_UNAVAILABLE"
    HUMAN_REVIEW_REQUIRED = "HUMAN_REVIEW_REQUIRED"


class RouteStatus(StrEnum):
    SCOUTED = "SCOUTED"
    FALSIFYING = "FALSIFYING"
    DEVELOPING = "DEVELOPING"
    REVIEWING = "REVIEWING"
    REVISING = "REVISING"
    FORMALIZING = "FORMALIZING"
    SEMANTIC_AUDIT = "SEMANTIC_AUDIT"
    READY = "READY"
    REFUTED = "REFUTED"
    FAILED = "FAILED"


class ResearchOutcome(StrEnum):
    PROVED_CANDIDATE = "PROVED_CANDIDATE"
    DISPROVED_CANDIDATE = "DISPROVED_CANDIDATE"
    KNOWN_RESULT = "KNOWN_RESULT"
    UNRESOLVED = "UNRESOLVED"


class WorkKind(StrEnum):
    CONTRACT_CLARIFY = "CONTRACT_CLARIFY"
    LITERATURE_AUDIT = "LITERATURE_AUDIT"
    ROUTE_SCOUT = "ROUTE_SCOUT"
    FALSIFY_ROUTE = "FALSIFY_ROUTE"
    DEVELOP_ROUTE = "DEVELOP_ROUTE"
    GAP_REVIEW = "GAP_REVIEW"
    REVISE_ROUTE = "REVISE_ROUTE"
    FORMALIZE_ROUTE = "FORMALIZE_ROUTE"
    SEMANTIC_AUDIT = "SEMANTIC_AUDIT"
    COMPOSITION_CHECK = "COMPOSITION_CHECK"
    REVISE_COMPOSITION = "REVISE_COMPOSITION"
    FINAL_SYNTHESIS = "FINAL_SYNTHESIS"
    TOOL_REQUEST = "TOOL_REQUEST"
    INTEGRATE_TOOLS = "INTEGRATE_TOOLS"


_ROLE_FOR_WORK: Mapping[WorkKind, MathRole] = MappingProxyType(
    {
        WorkKind.CONTRACT_CLARIFY: MathRole.CONTRACT_CLARIFIER,
        WorkKind.LITERATURE_AUDIT: MathRole.LITERATURE_NOVELTY_AUDITOR,
        WorkKind.ROUTE_SCOUT: MathRole.ROUTE_SCOUT,
        WorkKind.FALSIFY_ROUTE: MathRole.PROOF_COUNTEREXAMPLE,
        WorkKind.DEVELOP_ROUTE: MathRole.PROOF_COUNTEREXAMPLE,
        WorkKind.GAP_REVIEW: MathRole.ANONYMOUS_GAP_REVIEWER,
        WorkKind.REVISE_ROUTE: MathRole.TARGETED_REVISER,
        WorkKind.FORMALIZE_ROUTE: MathRole.LEAN_FORMALIZER,
        WorkKind.SEMANTIC_AUDIT: MathRole.SEMANTIC_FIDELITY_AUDITOR,
        WorkKind.COMPOSITION_CHECK: MathRole.ANONYMOUS_GAP_REVIEWER,
        WorkKind.REVISE_COMPOSITION: MathRole.TARGETED_REVISER,
        WorkKind.FINAL_SYNTHESIS: MathRole.FINAL_SYNTHESIZER,
    }
)


@dataclass(frozen=True, slots=True)
class BudgetPlan:
    """Hard orchestration envelope; provider-side reservations remain runtime responsibility."""

    max_work_units: int
    max_input_tokens: int
    max_output_tokens: int
    max_wall_time_ms: int

    def __post_init__(self) -> None:
        if (
            min(
                self.max_work_units,
                self.max_input_tokens,
                self.max_output_tokens,
                self.max_wall_time_ms,
            )
            <= 0
        ):
            raise ValueError("all orchestration budget limits must be positive")

    def to_dict(self) -> dict[str, int]:
        return {
            "max_work_units": self.max_work_units,
            "max_input_tokens": self.max_input_tokens,
            "max_output_tokens": self.max_output_tokens,
            "max_wall_time_ms": self.max_wall_time_ms,
        }


@dataclass(frozen=True, slots=True)
class HardwarePlan:
    """Opaque, recorded placement input produced by the loss-aware hardware scheduler."""

    mode: str
    plan_digest: str
    placements: JsonMap = field(default_factory=_frozen)

    def __post_init__(self) -> None:
        if not self.mode or not self.plan_digest:
            raise ValueError("hardware mode and plan digest are required")
        object.__setattr__(self, "placements", _frozen(self.placements))

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "plan_digest": self.plan_digest,
            "placements": dict(self.placements),
        }


@dataclass(frozen=True, slots=True)
class OrchestrationPlan:
    run_id: str
    contract_id: str
    contract_version: int
    contract_hash: str
    statement_hash: str
    contract: JsonMap
    budget: BudgetPlan
    hardware: HardwarePlan
    minimum_routes: int = 2
    maximum_routes: int = 4
    max_route_revisions: int = 2
    max_composition_revisions: int = 2
    max_tool_cycles: int = 2

    def __post_init__(self) -> None:
        if not self.run_id or not self.contract_id or self.contract_version <= 0:
            raise ValueError("run, contract id, and positive contract version are required")
        for name, value in (
            ("contract_hash", self.contract_hash),
            ("statement_hash", self.statement_hash),
        ):
            if len(value) != 64 or any(char not in "0123456789abcdef" for char in value):
                raise ValueError(f"{name} must be a lowercase SHA-256")
        if not 2 <= self.minimum_routes <= self.maximum_routes:
            raise ValueError("orchestration requires at least two routes")
        if (
            min(
                self.max_route_revisions,
                self.max_composition_revisions,
                self.max_tool_cycles,
            )
            < 0
        ):
            raise ValueError("revision and tool-cycle limits cannot be negative")
        object.__setattr__(self, "contract", _frozen(self.contract))

    def to_dict(self) -> dict[str, Any]:
        return {
            "run_id": self.run_id,
            "contract_id": self.contract_id,
            "contract_version": self.contract_version,
            "contract_hash": self.contract_hash,
            "statement_hash": self.statement_hash,
            "contract": dict(self.contract),
            "budget": self.budget.to_dict(),
            "hardware": self.hardware.to_dict(),
            "minimum_routes": self.minimum_routes,
            "maximum_routes": self.maximum_routes,
            "max_route_revisions": self.max_route_revisions,
            "max_composition_revisions": self.max_composition_revisions,
            "max_tool_cycles": self.max_tool_cycles,
        }


@dataclass(frozen=True, slots=True)
class ToolRequest:
    request_key: str
    tool: str
    operation: str
    payload: JsonMap = field(default_factory=_frozen)
    required: bool = True

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any], *, fallback_key: str) -> ToolRequest:
        return cls(
            request_key=str(value.get("request_key") or fallback_key),
            tool=str(value.get("tool", "")),
            operation=str(value.get("operation", "")),
            payload=_frozen(
                value.get("payload") if isinstance(value.get("payload"), Mapping) else {}
            ),
            required=bool(value.get("required", True)),
        )

    def __post_init__(self) -> None:
        if not self.request_key or not self.tool or not self.operation:
            raise ValueError("tool request key, tool, and operation are required")
        object.__setattr__(self, "payload", _frozen(self.payload))

    def to_dict(self) -> dict[str, Any]:
        return {
            "request_key": self.request_key,
            "tool": self.tool,
            "operation": self.operation,
            "payload": dict(self.payload),
            "required": self.required,
        }


@dataclass(frozen=True, slots=True)
class ComponentRequest:
    request_id: str
    run_id: str
    work_kind: str
    role: str | None
    route_id: str | None
    round: int
    contract_scope: JsonMap
    inputs: JsonMap
    budget_remaining: JsonMap
    hardware_plan: JsonMap

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "rk.component-request.v1",
            "request_id": self.request_id,
            "run_id": self.run_id,
            "work_kind": self.work_kind,
            "role": self.role,
            "route_id": self.route_id,
            "round": self.round,
            "contract_scope": dict(self.contract_scope),
            "inputs": dict(self.inputs),
            "budget_remaining": dict(self.budget_remaining),
            "hardware_plan": dict(self.hardware_plan),
            "authority_ceiling": "SOFT_CANDIDATE_ONLY",
        }


@dataclass(frozen=True, slots=True)
class ComponentResult:
    status: str
    component_id: str | None = None
    component_receipt_id: str | None = None
    payload: JsonMap = field(default_factory=_frozen)
    artifact_ids: tuple[str, ...] = ()
    tool_requests: tuple[ToolRequest, ...] = ()
    usage: JsonMap = field(default_factory=_frozen)
    started_ns: int | None = None
    finished_ns: int | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> ComponentResult:
        raw_requests = value.get("tool_requests", ())
        requests: list[ToolRequest] = []
        if isinstance(raw_requests, Sequence) and not isinstance(raw_requests, (str, bytes)):
            for index, item in enumerate(raw_requests):
                if isinstance(item, Mapping):
                    requests.append(
                        ToolRequest.from_mapping(item, fallback_key=f"tool-{index + 1}")
                    )
        payload = value.get("payload")
        usage = value.get("usage")
        artifact_ids = value.get("artifact_ids", ())
        return cls(
            status=str(value.get("status", "FAILED")),
            component_id=(
                str(value["component_id"]) if value.get("component_id") is not None else None
            ),
            component_receipt_id=(
                str(value["component_receipt_id"])
                if value.get("component_receipt_id") is not None
                else None
            ),
            payload=_frozen(payload if isinstance(payload, Mapping) else {}),
            artifact_ids=tuple(str(item) for item in artifact_ids),
            tool_requests=tuple(requests),
            usage=_frozen(usage if isinstance(usage, Mapping) else {}),
            started_ns=(
                int(value["component_started_ns"])
                if isinstance(value.get("component_started_ns"), int)
                else None
            ),
            finished_ns=(
                int(value["component_finished_ns"])
                if isinstance(value.get("component_finished_ns"), int)
                else None
            ),
        )

    def __post_init__(self) -> None:
        object.__setattr__(self, "payload", _frozen(self.payload))
        object.__setattr__(self, "usage", _frozen(self.usage))


@runtime_checkable
class ComponentRuntime(Protocol):
    """Single seam for role models, retrieval, theorem provers, and registered tools."""

    def execute(self, request: ComponentRequest) -> ComponentResult | Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class UsageLedger:
    work_units: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    wall_time_ms: int = 0

    def add(self, usage: Mapping[str, Any]) -> UsageLedger:
        return UsageLedger(
            work_units=self.work_units + 1,
            input_tokens=self.input_tokens + max(0, int(usage.get("input_tokens", 0))),
            output_tokens=self.output_tokens + max(0, int(usage.get("output_tokens", 0))),
            wall_time_ms=self.wall_time_ms + max(0, int(usage.get("wall_time_ms", 0))),
        )

    def exhausted(self, budget: BudgetPlan) -> bool:
        return (
            self.work_units >= budget.max_work_units
            or self.input_tokens >= budget.max_input_tokens
            or self.output_tokens >= budget.max_output_tokens
            or self.wall_time_ms >= budget.max_wall_time_ms
        )

    def remaining(self, budget: BudgetPlan) -> dict[str, int]:
        return {
            "work_units": max(0, budget.max_work_units - self.work_units),
            "input_tokens": max(0, budget.max_input_tokens - self.input_tokens),
            "output_tokens": max(0, budget.max_output_tokens - self.output_tokens),
            "wall_time_ms": max(0, budget.max_wall_time_ms - self.wall_time_ms),
        }

    def to_dict(self) -> dict[str, int]:
        return {
            "work_units": self.work_units,
            "input_tokens": self.input_tokens,
            "output_tokens": self.output_tokens,
            "wall_time_ms": self.wall_time_ms,
        }


@dataclass(frozen=True, slots=True)
class RouteState:
    route_id: str
    label: str
    representation: str
    tool_family: str
    status: RouteStatus = RouteStatus.SCOUTED
    revision_round: int = 0
    artifact_ids: tuple[str, ...] = ()
    open_obligations: tuple[str, ...] = ()
    candidate_kind: str | None = None
    machine_evidence: bool = False
    lean_replay_status: str | None = None
    method_card_id: str | None = None
    proof_skeleton: tuple[str, ...] = ()
    sharp_example: str | None = None
    near_miss: str | None = None
    fast_falsifier: str | None = None
    sentinel_result: JsonMap = field(default_factory=_frozen)
    independence_profile: JsonMap = field(default_factory=_frozen)
    promotion_reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "route_id": self.route_id,
            "label": self.label,
            "representation": self.representation,
            "tool_family": self.tool_family,
            "status": self.status.value,
            "revision_round": self.revision_round,
            "artifact_ids": list(self.artifact_ids),
            "open_obligations": list(self.open_obligations),
            "candidate_kind": self.candidate_kind,
            "machine_evidence": self.machine_evidence,
            "lean_replay_status": self.lean_replay_status,
            "method_card_id": self.method_card_id,
            "proof_skeleton": list(self.proof_skeleton),
            "sharp_example": self.sharp_example,
            "near_miss": self.near_miss,
            "fast_falsifier": self.fast_falsifier,
            "sentinel_result": dict(self.sentinel_result),
            "independence_profile": dict(self.independence_profile),
            "promotion_reasons": list(self.promotion_reasons),
        }


@dataclass(frozen=True, slots=True)
class HumanReview:
    review_id: str
    reviewer_id: str
    verdict: str
    artifact_id: str
    notes: str = ""

    def __post_init__(self) -> None:
        if self.verdict not in {"ACCEPTED", "CHANGES_REQUESTED", "REJECTED"}:
            raise ValueError("unsupported human review verdict")
        if not self.review_id or not self.reviewer_id or not self.artifact_id:
            raise ValueError("human review identity and artifact are required")

    def to_dict(self) -> dict[str, str]:
        return {
            "review_id": self.review_id,
            "reviewer_id": self.reviewer_id,
            "verdict": self.verdict,
            "artifact_id": self.artifact_id,
            "notes": self.notes,
        }


@dataclass(frozen=True, slots=True)
class OrchestrationEvent:
    sequence: int
    event_id: str
    event_type: str
    recorded_at: str
    payload: JsonMap

    def to_dict(self) -> dict[str, Any]:
        return {
            "sequence": self.sequence,
            "event_id": self.event_id,
            "event_type": self.event_type,
            "recorded_at": self.recorded_at,
            "payload": dict(self.payload),
        }


@dataclass(frozen=True, slots=True)
class _WorkItem:
    kind: WorkKind
    route_id: str | None = None
    round: int = 0
    context: JsonMap = field(default_factory=_frozen)

    def __post_init__(self) -> None:
        object.__setattr__(self, "context", _frozen(self.context))

    def to_dict(self) -> dict[str, Any]:
        return {
            "kind": self.kind.value,
            "route_id": self.route_id,
            "round": self.round,
            "context": dict(self.context),
        }


@dataclass(frozen=True, slots=True)
class OrchestrationCheckpoint:
    plan: OrchestrationPlan
    status: OrchestrationStatus
    phase: ResearchPhase
    queue: tuple[_WorkItem, ...]
    routes: tuple[RouteState, ...]
    usage: UsageLedger
    events: tuple[OrchestrationEvent, ...]
    next_sequence: int
    pause_reason: str | None = None
    literature_exact_match: bool = False
    composition_closed: bool = False
    composition_round: int = 0
    composition_artifact_ids: tuple[str, ...] = ()
    tool_feedback: JsonMap = field(default_factory=_frozen)
    human_reviews: tuple[HumanReview, ...] = ()
    outcome: str | None = None
    kernel_revision: int | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "tool_feedback", _frozen(self.tool_feedback))

    @property
    def checkpoint_digest(self) -> str:
        return _digest(self.to_dict(include_digest=False))

    def to_dict(self, *, include_digest: bool = True) -> dict[str, Any]:
        stage_zh = {
            ResearchPhase.CONTRACT: "澄清题目",
            ResearchPhase.LITERATURE: "核对已有文献",
            ResearchPhase.ROUTES: "多路线研究与复核",
            ResearchPhase.COMPOSITION: "检查局部结果能否拼合",
            ResearchPhase.HUMAN_REVIEW: "等待数学家独立审查",
            ResearchPhase.SYNTHESIS: "汇总证据与未决缺口",
            ResearchPhase.COMPLETE: "研究轮次完成",
        }[self.phase]
        component_usage: dict[str, dict[str, int]] = {}
        role_states: dict[str, dict[str, str]] = {}
        for event in self.events:
            if event.event_type != "COMPONENT_COMPLETED":
                continue
            name = str(event.payload.get("component_id") or "未标识组件")
            usage = event.payload.get("usage", {})
            usage = usage if isinstance(usage, Mapping) else {}
            item = component_usage.setdefault(
                name, {"input_tokens": 0, "output_tokens": 0, "wall_time_ms": 0}
            )
            for key in item:
                item[key] += max(0, int(usage.get(key, 0)))
            role = event.payload.get("role")
            if role:
                role_name = str(role)
                role_states[role_name] = {
                    "role": role_name,
                    "status": str(event.payload.get("status", "UNKNOWN")),
                }
        result = {
            "schema_version": "rk.orchestration-checkpoint.v1",
            "plan": self.plan.to_dict(),
            "status": self.status.value,
            "phase": self.phase.value,
            "queue": [item.to_dict() for item in self.queue],
            "routes": [route.to_dict() for route in self.routes],
            "usage": self.usage.to_dict(),
            "events": [event.to_dict() for event in self.events],
            "next_sequence": self.next_sequence,
            "pause_reason": self.pause_reason,
            "literature_exact_match": self.literature_exact_match,
            "composition_closed": self.composition_closed,
            "composition_round": self.composition_round,
            "composition_artifact_ids": list(self.composition_artifact_ids),
            "tool_feedback": dict(self.tool_feedback),
            "human_reviews": [review.to_dict() for review in self.human_reviews],
            "outcome": self.outcome,
            "kernel_revision": self.kernel_revision,
            "message_zh": self._message_zh(),
            "stage_zh": stage_zh,
            "component_usage": component_usage,
            "roles": list(role_states.values()),
        }
        if include_digest:
            result["checkpoint_digest"] = _digest(result)
        return result

    def _message_zh(self) -> str:
        if self.status is OrchestrationStatus.WAITING_HUMAN_REVIEW:
            return "自动研究已到独立审查门，尚未把候选结论当作数学事实。"
        if self.status is OrchestrationStatus.PAUSED:
            return f"研究已安全暂停（{self.pause_reason or '未说明原因'}）。"
        if self.status is OrchestrationStatus.COMPLETED:
            if self.outcome == ResearchOutcome.UNRESOLVED.value:
                return "本轮未获得闭合证据，结论诚实保留为未解决。"
            return f"本轮候选结论为 {self.outcome}；其数学权威仍由 RK 内核另行裁决。"
        return "研究正在按文献、证伪、多路线、形式化和组合闭包顺序推进。"


@dataclass(frozen=True, slots=True)
class OrchestrationUpdate:
    checkpoint: OrchestrationCheckpoint
    new_events: tuple[OrchestrationEvent, ...]


class ResearchOrchestrator:
    """Drive a research checkpoint until it reaches a gate, budget, or terminal state."""

    def __init__(
        self,
        runtime: ComponentRuntime,
        *,
        clock: SystemClock | None = None,
        id_factory: Callable[[], str] | None = None,
        research_fixtures: Mapping[str, Any] | None = None,
        config: KernelConfig | None = None,
        kernel: ResearchKernel | None = None,
        capability: VerifiedCapability | None = None,
        environment: Mapping[str, str] | None = None,
        extensions: ExtensionRegistry | None = None,
        max_parallel_falsifiers: int = 8,
    ) -> None:
        if max_parallel_falsifiers < 1:
            raise ValueError("max_parallel_falsifiers must be positive")
        self._runtime = runtime
        self._clock = clock or SystemClock()
        self._id_factory = id_factory or Uuid7Generator().new
        self._research_fixtures = _frozen(research_fixtures)
        self._config = config
        self._kernel = kernel
        self._capability = capability
        self._environment = _frozen(environment)
        self._extensions = extensions or ExtensionRegistry()
        self._max_parallel_falsifiers = max_parallel_falsifiers

    @classmethod
    def from_config(
        cls,
        *,
        config: KernelConfig,
        kernel: ResearchKernel,
        capability: VerifiedCapability,
        environment: Mapping[str, str],
        extensions: ExtensionRegistry | None = None,
    ) -> ResearchOrchestrator:
        """Build the product adapter while keeping provider details outside orchestration logic."""

        configured = config.product.get("component_runtime")
        if isinstance(configured, ComponentRuntime):
            runtime = configured
        else:
            from rk.component_runtime import build_component_runtime

            runtime = build_component_runtime(config, environment)
        return cls(
            runtime,
            research_fixtures=_load_research_fixtures(config),
            config=config,
            kernel=kernel,
            capability=capability,
            environment=environment,
            extensions=extensions,
        )

    def start(self, plan: OrchestrationPlan | str) -> OrchestrationCheckpoint:
        if isinstance(plan, str):
            path = self._checkpoint_path(plan)
            if path.is_file():
                return self.continue_run(plan)
            self._ensure_kernel_started(plan)
            checkpoint = self.start(self._plan_for_run(plan))
            self._save_checkpoint(checkpoint)
            return self.continue_run(plan)
        checkpoint = OrchestrationCheckpoint(
            plan=plan,
            status=OrchestrationStatus.RUNNING,
            phase=ResearchPhase.CONTRACT,
            queue=(_WorkItem(WorkKind.CONTRACT_CLARIFY),),
            routes=(),
            usage=UsageLedger(),
            events=(),
            next_sequence=1,
            kernel_revision=self._current_kernel_revision(plan.run_id),
        )
        checkpoint, _ = self._emit(
            checkpoint,
            "ORCHESTRATION_STARTED",
            {
                "contract_hash": plan.contract_hash,
                "statement_hash": plan.statement_hash,
                "budget": plan.budget.to_dict(),
                "hardware": plan.hardware.to_dict(),
            },
        )
        return checkpoint

    def advance(
        self, checkpoint: OrchestrationCheckpoint, *, max_work_units: int | None = None
    ) -> OrchestrationUpdate:
        if max_work_units is not None and max_work_units <= 0:
            raise ValueError("max_work_units must be positive")
        if checkpoint.status in {
            OrchestrationStatus.COMPLETED,
            OrchestrationStatus.WAITING_HUMAN_REVIEW,
        }:
            return OrchestrationUpdate(checkpoint, ())
        if checkpoint.status is OrchestrationStatus.PAUSED:
            return OrchestrationUpdate(checkpoint, ())

        current = checkpoint
        created: list[OrchestrationEvent] = []
        executed = 0
        while current.status is OrchestrationStatus.RUNNING and current.queue:
            if max_work_units is not None and executed >= max_work_units:
                break
            if current.usage.exhausted(current.plan.budget):
                current, event = self._pause(current, PauseReason.BUDGET_EXHAUSTED)
                created.append(event)
                break
            item, queue = current.queue[0], current.queue[1:]
            current = replace(current, queue=queue)
            if item.kind is WorkKind.FALSIFY_ROUTE:
                batch = [item]
                while current.queue and current.queue[0].kind is WorkKind.FALSIFY_ROUTE:
                    batch.append(current.queue[0])
                    current = replace(current, queue=current.queue[1:])
                if len(batch) > 1:
                    requests = [self._component_request(current, candidate) for candidate in batch]

                    def execute_timed(
                        request: ComponentRequest,
                    ) -> ComponentResult | Mapping[str, Any]:
                        started_ns = time.time_ns()
                        raw = self._runtime.execute(request)
                        finished_ns = time.time_ns()
                        parsed = (
                            raw
                            if isinstance(raw, ComponentResult)
                            else ComponentResult.from_mapping(raw)
                        )
                        return replace(
                            parsed,
                            started_ns=parsed.started_ns or started_ns,
                            finished_ns=parsed.finished_ns or finished_ns,
                        )

                    with ThreadPoolExecutor(
                        max_workers=min(len(batch), self._max_parallel_falsifiers)
                    ) as pool:
                        futures = [
                            None
                            if isinstance(candidate.context.get("parallel_precomputed"), Mapping)
                            else pool.submit(execute_timed, request)
                            for candidate, request in zip(batch, requests, strict=True)
                        ]
                        raw_results: list[ComponentResult | Mapping[str, Any] | Exception] = []
                        for candidate, future in zip(batch, futures, strict=True):
                            precomputed = candidate.context.get("parallel_precomputed")
                            if isinstance(precomputed, Mapping):
                                raw_results.append(precomputed)
                                continue
                            assert future is not None
                            try:
                                raw_results.append(future.result())
                            except Exception as error:  # converted into resumable data below
                                raw_results.append(error)
                    current, event = self._emit(
                        current,
                        "ROUTE_CANDIDATES_EXECUTED_IN_PARALLEL",
                        {"route_ids": [candidate.route_id for candidate in batch]},
                    )
                    created.append(event)
                    if any(isinstance(raw, Exception) for raw in raw_results):
                        retry_batch: list[_WorkItem] = []
                        error_types: dict[str, str] = {}
                        for candidate, raw in zip(batch, raw_results, strict=True):
                            context = dict(candidate.context)
                            context.pop("parallel_precomputed", None)
                            if isinstance(raw, Exception):
                                context["runtime_attempt"] = int(
                                    context.get("runtime_attempt", 0)
                                ) + 1
                                context["last_error_type"] = type(raw).__name__
                                error_types[str(candidate.route_id)] = type(raw).__name__
                            else:
                                parsed = (
                                    raw
                                    if isinstance(raw, ComponentResult)
                                    else ComponentResult.from_mapping(raw)
                                )
                                context["parallel_precomputed"] = {
                                    "status": parsed.status,
                                    "component_id": parsed.component_id,
                                    "component_receipt_id": parsed.component_receipt_id,
                                    "payload": dict(parsed.payload),
                                    "artifact_ids": list(parsed.artifact_ids),
                                    "tool_requests": [
                                        request.to_dict() for request in parsed.tool_requests
                                    ],
                                    "usage": dict(parsed.usage),
                                    "component_started_ns": parsed.started_ns,
                                    "component_finished_ns": parsed.finished_ns,
                                }
                            retry_batch.append(replace(candidate, context=context))
                        current = replace(current, queue=tuple(retry_batch) + current.queue)
                        current, pause_event = self._pause(
                            current,
                            PauseReason.RUNTIME_UNAVAILABLE,
                            details={
                                "work_kind": WorkKind.FALSIFY_ROUTE.value,
                                "route_ids": [candidate.route_id for candidate in batch],
                                "error_types": error_types,
                                "same_checkpoint_retry": True,
                                "parallel_batch_preserved": True,
                            },
                        )
                        created.append(pause_event)
                        break
                    promotion_records: list[dict[str, Any]] = []
                    for index, (candidate, request, raw) in enumerate(
                        zip(batch, requests, raw_results, strict=True)
                    ):
                        current, events = self._execute(
                            current, candidate, precomputed=(request, raw)
                        )
                        created.extend(events)
                        completed = next(
                            (
                                event
                                for event in events
                                if event.event_type == "COMPONENT_COMPLETED"
                            ),
                            None,
                        )
                        promotion_records.append(
                            {
                                "route_id": candidate.route_id,
                                "status": (
                                    completed.payload.get("status")
                                    if completed is not None
                                    else "PAUSED"
                                ),
                                "started_ns": (
                                    completed.payload.get("started_ns")
                                    if completed is not None
                                    else None
                                ),
                                "finished_ns": (
                                    completed.payload.get("finished_ns")
                                    if completed is not None
                                    else None
                                ),
                            }
                        )
                        executed += 1
                        if current.status is not OrchestrationStatus.RUNNING:
                            preserved: list[_WorkItem] = []
                            for remaining, remaining_raw in zip(
                                batch[index + 1 :], raw_results[index + 1 :], strict=True
                            ):
                                context = dict(remaining.context)
                                if not isinstance(remaining_raw, Exception):
                                    parsed = (
                                        remaining_raw
                                        if isinstance(remaining_raw, ComponentResult)
                                        else ComponentResult.from_mapping(remaining_raw)
                                    )
                                    context["parallel_precomputed"] = {
                                        "status": parsed.status,
                                        "component_id": parsed.component_id,
                                        "component_receipt_id": parsed.component_receipt_id,
                                        "payload": dict(parsed.payload),
                                        "artifact_ids": list(parsed.artifact_ids),
                                        "tool_requests": [
                                            tool.to_dict() for tool in parsed.tool_requests
                                        ],
                                        "usage": dict(parsed.usage),
                                        "component_started_ns": parsed.started_ns,
                                        "component_finished_ns": parsed.finished_ns,
                                    }
                                preserved.append(replace(remaining, context=context))
                            current = replace(
                                current,
                                queue=current.queue[:1]
                                + tuple(preserved)
                                + current.queue[1:],
                            )
                            break
                    if current.status is not OrchestrationStatus.RUNNING:
                        continue
                    current, event = self._emit(
                        current,
                        "ROUTE_CANDIDATES_PROMOTED_SERIAL",
                        {
                            "promotion_order": [record["route_id"] for record in promotion_records],
                            "parallel_execution_intervals": promotion_records,
                        },
                    )
                    created.append(event)
                    continue
            if item.kind is WorkKind.INTEGRATE_TOOLS:
                current, events = self._integrate_tools(current, item)
                created.extend(events)
                continue
            current, events = self._execute(current, item)
            created.extend(events)
            executed += 1

        if current.status is OrchestrationStatus.RUNNING and not current.queue:
            current, event = self._enter_human_review(current)
            created.append(event)
        return OrchestrationUpdate(current, tuple(created))

    def _resume_checkpoint(
        self,
        checkpoint: OrchestrationCheckpoint,
        *,
        human_reviews: Sequence[HumanReview] = (),
        budget: BudgetPlan | None = None,
        hardware: HardwarePlan | None = None,
    ) -> OrchestrationCheckpoint:
        """Resume a gated checkpoint without replaying already completed work."""

        current = checkpoint
        if current.status is OrchestrationStatus.COMPLETED:
            return current
        if budget is not None:
            if (
                budget.max_work_units < current.usage.work_units
                or budget.max_input_tokens < current.usage.input_tokens
                or budget.max_output_tokens < current.usage.output_tokens
                or budget.max_wall_time_ms < current.usage.wall_time_ms
            ):
                raise ValueError("a resumed budget cannot be below already recorded usage")
            current = replace(current, plan=replace(current.plan, budget=budget))
        if hardware is not None:
            current = replace(current, plan=replace(current.plan, hardware=hardware))

        if current.status is OrchestrationStatus.WAITING_HUMAN_REVIEW:
            if not human_reviews:
                return current
            known = {review.review_id for review in current.human_reviews}
            additions = tuple(review for review in human_reviews if review.review_id not in known)
            current = replace(
                current,
                status=OrchestrationStatus.RUNNING,
                phase=ResearchPhase.SYNTHESIS,
                pause_reason=None,
                human_reviews=current.human_reviews + additions,
            )
            for review in additions:
                current, _ = self._emit(
                    current,
                    "HUMAN_REVIEW_RECORDED",
                    {
                        "review_id": review.review_id,
                        "reviewer_id": review.reviewer_id,
                        "verdict": review.verdict,
                        "artifact_id": review.artifact_id,
                    },
                )
            latest = additions[-1]
            if latest.verdict == "CHANGES_REQUESTED":
                revisable = tuple(
                    route
                    for route in current.routes
                    if route.status is RouteStatus.READY
                    and route.revision_round < current.plan.max_route_revisions
                )
                if revisable:
                    current = replace(
                        current,
                        phase=ResearchPhase.ROUTES,
                        composition_closed=False,
                        queue=tuple(
                            _WorkItem(
                                WorkKind.REVISE_ROUTE,
                                route.route_id,
                                route.revision_round + 1,
                                {"human_review_artifact_id": latest.artifact_id},
                            )
                            for route in revisable
                        ),
                    )
                elif current.composition_round < current.plan.max_composition_revisions:
                    current = replace(
                        current,
                        phase=ResearchPhase.COMPOSITION,
                        composition_closed=False,
                        queue=(
                            _WorkItem(
                                WorkKind.REVISE_COMPOSITION,
                                round=current.composition_round + 1,
                                context={"human_review_artifact_id": latest.artifact_id},
                            ),
                        ),
                    )
                else:
                    current = replace(current, queue=(_WorkItem(WorkKind.FINAL_SYNTHESIS),))
            else:
                current = replace(current, queue=(_WorkItem(WorkKind.FINAL_SYNTHESIS),))
            return current

        if current.status is OrchestrationStatus.PAUSED:
            if current.pause_reason == PauseReason.CONTRACT_AMBIGUOUS.value:
                raise ValueError("contract ambiguity requires a new frozen contract/run")
            if current.pause_reason == PauseReason.BUDGET_EXHAUSTED.value and budget is None:
                return current
            current = replace(current, status=OrchestrationStatus.RUNNING, pause_reason=None)
            current, _ = self._emit(current, "ORCHESTRATION_RESUMED", {})
        return current

    def continue_run(self, run_id: str) -> OrchestrationCheckpoint:
        checkpoint = self._load_checkpoint(run_id)
        update = self.advance(checkpoint)
        self._save_checkpoint(update.checkpoint)
        return update.checkpoint

    def status(self, run_id: str) -> OrchestrationCheckpoint:
        """Read and verify the durable checkpoint without advancing any work."""

        return self._load_checkpoint(run_id)

    def pause(self, run_id: str) -> OrchestrationCheckpoint:
        checkpoint = self._load_checkpoint(run_id)
        if checkpoint.status is OrchestrationStatus.COMPLETED:
            return checkpoint
        paused, _ = self._pause(
            checkpoint,
            PauseReason.USER_REQUEST,
            details={"requested_by": "USER", "safe_checkpoint": True},
        )
        self._save_checkpoint(paused)
        return paused

    def resume(
        self,
        checkpoint_or_run_id: OrchestrationCheckpoint | str,
        *,
        human_reviews: Sequence[HumanReview] = (),
        budget: BudgetPlan | None = None,
        hardware: HardwarePlan | None = None,
    ) -> OrchestrationCheckpoint:
        if isinstance(checkpoint_or_run_id, OrchestrationCheckpoint):
            return self._resume_checkpoint(
                checkpoint_or_run_id,
                human_reviews=human_reviews,
                budget=budget,
                hardware=hardware,
            )
        checkpoint = self._load_checkpoint(checkpoint_or_run_id)
        resumed = self._resume_checkpoint(checkpoint, budget=budget, hardware=hardware)
        self._save_checkpoint(resumed)
        if resumed.status is OrchestrationStatus.RUNNING:
            return self.continue_run(checkpoint_or_run_id)
        return resumed

    def review(
        self,
        run_id: str,
        review_file: Path,
        verdict: str,
        *,
        review_kind: str = "peer",
        blind_review: bool = False,
    ) -> OrchestrationCheckpoint:
        checkpoint = self._load_checkpoint(run_id)
        normalized = {
            "ACCEPT": "ACCEPTED",
            "NEEDS_REVISION": "CHANGES_REQUESTED",
            "REJECT": "REJECTED",
            "ABSTAIN": "CHANGES_REQUESTED",
        }.get(verdict)
        if normalized is None:
            raise ValueError("unsupported human review verdict")
        normalized_kind = {
            "同行": "peer",
            "peer": "peer",
            "语义": "semantic",
            "semantic": "semantic",
            "质量": "quality",
            "quality": "quality",
        }.get(review_kind)
        if normalized_kind is None:
            raise ValueError("unsupported human review kind")
        if self._kernel is None or self._config is None or self._capability is None:
            raise RuntimeError("review import requires from_config")
        from rk.domain import ApplyRequest, ArtifactInput, RunSnapshot, TypedCommand

        resolved = review_file.resolve()
        if not resolved.is_file():
            raise ValueError("审查文件不存在")
        source_data = resolved.read_bytes()
        media_type = mimetypes.guess_type(resolved.name)[0]
        allowed_review_types = {
            ".md": "text/markdown",
            ".txt": "text/plain; charset=utf-8",
            ".tex": "application/x-tex",
            ".pdf": "application/pdf",
        }
        suffix = resolved.suffix.lower()
        if suffix not in allowed_review_types:
            raise ValueError("审查材料仅支持 Markdown、纯文本、TeX 或 PDF")
        digest = hashlib.sha256(source_data).hexdigest()
        bound = resolved.parent / f".rk-review-{digest[:16]}-{normalized_kind}{suffix}"
        marker = f"\n\nRK_REVIEW_KIND: {normalized_kind}\n".encode()
        data = source_data.rstrip() + marker
        if not bound.exists() or bound.read_bytes() != data:
            bound.write_bytes(data)
        resolved = bound
        media_type = allowed_review_types[suffix] if media_type is None else media_type
        artifact_name = f"human_review_{normalized_kind}_{digest[:16]}{suffix}"
        snapshot = self._kernel.inspect(run_id)
        if not isinstance(snapshot, RunSnapshot):
            raise ValueError("无法读取研究状态")
        root_claim_id = snapshot.projection.get("root_claim_id")
        claims = snapshot.projection.get("claims", [])
        root_claim = next(
            (
                item
                for item in claims
                if isinstance(item, Mapping) and item.get("claim_id") == root_claim_id
            ),
            None,
        )
        if not isinstance(root_claim, Mapping):
            active_roots = [
                item
                for item in claims
                if isinstance(item, Mapping)
                and item.get("claim_kind") == "ROOT"
                and item.get("lifecycle") == "ACTIVE"
                and item.get("contract_version") == snapshot.current_contract_version
            ]
            if len(active_roots) == 1:
                root_claim = active_roots[0]
                root_claim_id = root_claim["claim_id"]
        if not isinstance(root_claim, Mapping):
            raise ValueError("研究尚未建立根命题")
        artifact = self._kernel.import_artifact(
            run_id,
            ArtifactInput(
                name=artifact_name,
                path=str(resolved),
                sha256=hashlib.sha256(data).hexdigest(),
                byte_count=len(data),
                media_type=media_type,
            ),
            self._role_capability("verifier"),
            logical_name=f"{artifact_name}@{normalized_kind}",
            role="HUMAN_REVIEW",
        )
        command_type = "RecordQualityReview" if normalized_kind == "quality" else "RecordPeerReview"
        if normalized_kind == "quality":
            command_payload: Mapping[str, Any] = {
                "claim_id": root_claim_id,
                "contract_version": snapshot.current_contract_version,
                "review_artifact_id": artifact.artifact_id,
                "verdict": {
                    "ACCEPT": "ACCEPT",
                    "NEEDS_REVISION": "NEEDS_REVISION",
                    "REJECT": "REJECT",
                    "ABSTAIN": "NEEDS_REVISION",
                }[verdict],
                "dimensions": {
                    "mathematical_clarity": True,
                    "exposition": True,
                    "review_kind": "QUALITY",
                },
                "training_pool": "EXCLUDED",
            }
        else:
            command_payload = {
                "claim_id": root_claim_id,
                "contract_version": snapshot.current_contract_version,
                "statement_hash": root_claim["statement_hash"],
                "review_artifact_id": artifact.artifact_id,
                "verdict": {
                    "ACCEPT": "ACCEPT",
                    "NEEDS_REVISION": "NEEDS_REVISION",
                    "REJECT": "REJECT",
                    "ABSTAIN": "NEEDS_REVISION",
                }[verdict],
                "checklist": {
                    "proof_checked": True,
                    "scope_checked": True,
                    "blind_review": blind_review,
                    "semantic_attestation": normalized_kind == "semantic" and verdict == "ACCEPT",
                },
                "source_graph": {
                    "author_subject_ids": [
                        str(checkpoint.plan.contract.get("author_subject_id", "unknown-author"))
                    ],
                    "review_mode": (
                        "MANAGED_BLIND_REVIEW" if blind_review else "USER_IMPORTED_NAMED_REVIEW"
                    ),
                },
            }
        receipt = self._kernel.apply(
            ApplyRequest(
                request_id=self._id_factory(),
                run_id=run_id,
                expected_revision=snapshot.revision,
                command=TypedCommand(
                    command_type,
                    _frozen(command_payload),
                ),
            ),
            self._role_capability("verifier"),
        )
        if not receipt.accepted:
            raise ValueError(f"审查材料未能导入: {receipt.rejection_code or 'UNKNOWN'}")
        artifact_id = artifact.artifact_id
        review = HumanReview(
            review_id=self._id_factory(),
            reviewer_id=self._capability.subject_id if self._capability else "human-reviewer",
            verdict=normalized,
            artifact_id=artifact_id,
            notes=f"[{normalized_kind}] " + data.decode("utf-8", errors="replace")[:2_000],
        )
        if normalized_kind == "quality":
            self._save_checkpoint(checkpoint)
            return checkpoint
        resumed = self._resume_checkpoint(checkpoint, human_reviews=(review,))
        self._save_checkpoint(resumed)
        return (
            self.continue_run(run_id) if resumed.status is OrchestrationStatus.RUNNING else resumed
        )

    def _component_request(
        self, checkpoint: OrchestrationCheckpoint, item: _WorkItem
    ) -> ComponentRequest:
        role = _ROLE_FOR_WORK.get(item.kind)
        return ComponentRequest(
            request_id=self._id_factory(),
            run_id=checkpoint.plan.run_id,
            work_kind=item.kind.value,
            role=role.value if role else None,
            route_id=item.route_id,
            round=item.round,
            contract_scope=_frozen(
                {
                    "contract_id": checkpoint.plan.contract_id,
                    "contract_version": checkpoint.plan.contract_version,
                    "contract_hash": checkpoint.plan.contract_hash,
                    "statement_hash": checkpoint.plan.statement_hash,
                }
            ),
            inputs=_frozen(self._inputs(checkpoint, item)),
            budget_remaining=_frozen(checkpoint.usage.remaining(checkpoint.plan.budget)),
            hardware_plan=_frozen(checkpoint.plan.hardware.to_dict()),
        )

    def _execute(
        self,
        checkpoint: OrchestrationCheckpoint,
        item: _WorkItem,
        *,
        precomputed: tuple[ComponentRequest, ComponentResult | Mapping[str, Any] | Exception]
        | None = None,
    ) -> tuple[OrchestrationCheckpoint, tuple[OrchestrationEvent, ...]]:
        request = (
            precomputed[0] if precomputed is not None else self._component_request(checkpoint, item)
        )
        try:
            raw = precomputed[1] if precomputed is not None else self._runtime.execute(request)
            if isinstance(raw, Exception):
                raise raw
            result = raw if isinstance(raw, ComponentResult) else ComponentResult.from_mapping(raw)
        except Exception as error:  # runtime errors are data; the checkpoint must remain resumable
            retry_item = replace(
                item,
                context={
                    **dict(item.context),
                    "runtime_attempt": int(item.context.get("runtime_attempt", 0)) + 1,
                    "last_error_type": type(error).__name__,
                },
            )
            paused = replace(checkpoint, queue=(retry_item,) + checkpoint.queue)
            paused, event = self._pause(
                paused,
                PauseReason.RUNTIME_UNAVAILABLE,
                details={
                    "work_kind": item.kind.value,
                    "route_id": item.route_id,
                    "error_type": type(error).__name__,
                    "runtime_attempt": retry_item.context["runtime_attempt"],
                    "same_checkpoint_retry": True,
                },
            )
            return paused, (event,)

        current = replace(checkpoint, usage=checkpoint.usage.add(result.usage))
        result = self._persist_component_result(current, request, result)
        current, completed = self._emit(
            current,
            "COMPONENT_COMPLETED",
            {
                "request_id": request.request_id,
                "work_kind": item.kind.value,
                "role": request.role,
                "route_id": item.route_id,
                "round": item.round,
                "runtime_attempt": int(item.context.get("runtime_attempt", 0)) + 1,
                "recovered_after_failure": bool(item.context.get("last_error_type")),
                "status": result.status,
                "component_id": result.component_id,
                "component_receipt_id": result.component_receipt_id,
                "artifact_ids": list(result.artifact_ids),
                "usage": dict(result.usage),
                "started_ns": result.started_ns,
                "finished_ns": result.finished_ns,
                "result_digest": _digest(
                    {
                        "status": result.status,
                        "payload": dict(result.payload),
                        "artifact_ids": result.artifact_ids,
                    }
                ),
                "authority_ceiling": "SOFT_CANDIDATE_ONLY",
            },
        )
        events = [completed]

        if item.kind is WorkKind.TOOL_REQUEST:
            requested = item.context.get("request", {})
            current, function_event = self._emit(
                current,
                "REGISTERED_FUNCTION_EXECUTED",
                {
                    "route_id": item.route_id,
                    "request_key": requested.get("request_key"),
                    "component_id": result.component_id,
                    "function_name": requested.get("operation"),
                    "receipt_id": result.component_receipt_id,
                    "status": result.status,
                    "runtime_attempt": int(item.context.get("runtime_attempt", 0)) + 1,
                    "recovered_after_failure": bool(item.context.get("last_error_type")),
                    "authority_ceiling": "NO_FACT_GRAPH_WRITE",
                },
            )
            events.append(function_event)
            repairable_tool_request = result.payload.get("repairable_tool_request") is True
            if result.status != "COMPLETED" and not repairable_tool_request:
                completed_attempt = int(item.context.get("runtime_attempt", 0)) + 1
                retry_item = replace(
                    item,
                    context={
                        **dict(item.context),
                        "runtime_attempt": completed_attempt,
                        "last_error_type": result.status,
                    },
                )
                current = replace(current, queue=(retry_item,) + current.queue)
                current, pause_event = self._pause(
                    current,
                    PauseReason.RUNTIME_UNAVAILABLE,
                    details={
                        "work_kind": item.kind.value,
                        "route_id": item.route_id,
                        "component_status": result.status,
                        "runtime_attempt": completed_attempt,
                        "same_checkpoint_retry": True,
                    },
                )
                events.append(pause_event)
                return current, tuple(events)

        fuse_resources = result.payload.get("budget_fuse_resources")
        if (
            isinstance(fuse_resources, Sequence)
            and not isinstance(fuse_resources, (str, bytes))
            and fuse_resources
        ):
            current = replace(current, queue=(item,) + current.queue)
            current, event = self._pause(
                current,
                PauseReason.BUDGET_EXHAUSTED,
                details={"resource_kinds": [str(value) for value in fuse_resources]},
            )
            events.append(event)
            return current, tuple(events)

        if int(item.context.get("tool_cycle", 0)) == 0:
            forced = self._forced_tool_requests(item.kind)
            if forced:
                merged = {tool.request_key: tool for tool in result.tool_requests}
                for forced_tool in forced:
                    merged[forced_tool.request_key] = forced_tool
                result = replace(result, tool_requests=tuple(merged.values()))

        # A controller may deliberately return AWAITING_TOOL: it is not a runtime outage when
        # the same result (or the deployment policy above) supplied executable registered calls.
        # Persist and run those calls before asking the controller to integrate their feedback.
        awaiting_registered_tool = bool(result.tool_requests)
        if (
            result.status != "COMPLETED"
            and not awaiting_registered_tool
            and item.kind is not WorkKind.TOOL_REQUEST
        ):
            current = replace(current, queue=(item,) + current.queue)
            current, event = self._pause(
                current,
                PauseReason.RUNTIME_UNAVAILABLE,
                details={"work_kind": item.kind.value, "component_status": result.status},
            )
            events.append(event)
            return current, tuple(events)

        if result.tool_requests and item.kind is not WorkKind.TOOL_REQUEST:
            if int(item.context.get("tool_cycle", 0)) >= current.plan.max_tool_cycles:
                result = replace(
                    result,
                    payload=_frozen(
                        {
                            **dict(result.payload),
                            "tool_cycle_exhausted": True,
                            "open_obligations": list(result.payload.get("open_obligations", ()))
                            + ["TOOL_CYCLE_EXHAUSTED"],
                        }
                    ),
                    tool_requests=(),
                )
            else:
                tool_items = tuple(
                    _WorkItem(
                        WorkKind.TOOL_REQUEST,
                        route_id=item.route_id,
                        context={"request": tool.to_dict()},
                    )
                    for tool in result.tool_requests
                )
                integrate = _WorkItem(
                    WorkKind.INTEGRATE_TOOLS,
                    route_id=item.route_id,
                    round=item.round,
                    context={
                        "parent_kind": item.kind.value,
                        "parent_context": dict(item.context),
                        "parent_payload": dict(result.payload),
                        "parent_artifact_ids": list(result.artifact_ids),
                        "request_keys": [tool.request_key for tool in result.tool_requests],
                        "tool_cycle": int(item.context.get("tool_cycle", 0)) + 1,
                    },
                )
                current = replace(current, queue=tool_items + (integrate,) + current.queue)
                current, event = self._emit(
                    current,
                    "TOOL_REQUESTS_QUEUED",
                    {
                        "work_kind": item.kind.value,
                        "route_id": item.route_id,
                        "requests": [tool.to_dict() for tool in result.tool_requests],
                    },
                )
                events.append(event)
                return current, tuple(events)

        if item.kind is WorkKind.TOOL_REQUEST:
            request_data = item.context.get("request", {})
            key = str(request_data.get("request_key", "unknown"))
            feedback = dict(current.tool_feedback)
            feedback[key] = {
                "status": result.status,
                "component_id": result.component_id,
                "component_receipt_id": result.component_receipt_id,
                "payload": dict(result.payload),
                "artifact_ids": list(result.artifact_ids),
                "result_digest": _digest(dict(result.payload)),
            }
            current = replace(current, tool_feedback=_frozen(feedback))
            return current, tuple(events)

        current, transition_events = self._handle_result(current, item, result)
        events.extend(transition_events)
        if (
            current.usage.exhausted(current.plan.budget)
            and current.status is OrchestrationStatus.RUNNING
        ):
            current, event = self._pause(current, PauseReason.BUDGET_EXHAUSTED)
            events.append(event)
        return current, tuple(events)

    def _forced_tool_requests(self, work_kind: WorkKind) -> tuple[ToolRequest, ...]:
        """Return deployment-owned mandatory probes for a work phase.

        Model prompts may recommend tools, but product requirements cannot depend on the model
        electing to follow prose. The deployment supplies only registered component/function
        names and schema-validated arguments; execution still crosses the normal runtime seam.
        """

        if self._config is None:
            return ()
        product = getattr(self._config, "product", {})
        configured = product.get("forced_tool_requests", {}) if isinstance(product, Mapping) else {}
        if not isinstance(configured, Mapping):
            return ()
        values = configured.get(work_kind.value, ())
        if not isinstance(values, Sequence) or isinstance(values, (str, bytes)):
            raise ValueError("forced_tool_requests phase must be an array")
        requests: list[ToolRequest] = []
        for index, value in enumerate(values):
            if not isinstance(value, Mapping):
                raise ValueError("forced_tool_requests item must be an object")
            requests.append(
                ToolRequest(
                    request_key=str(value.get("request_key") or f"host-{work_kind.value}-{index}"),
                    tool=str(value["tool"]),
                    operation=str(value["operation"]),
                    payload=_frozen(value.get("payload", {})),
                    required=bool(value.get("required", True)),
                )
            )
        return tuple(requests)

    def _persist_component_result(
        self,
        checkpoint: OrchestrationCheckpoint,
        request: ComponentRequest,
        result: ComponentResult,
    ) -> ComponentResult:
        """Put the complete role/tool result in the run's reportable material collection."""

        if self._kernel is None or self._capability is None or self._config is None:
            return result
        from rk.domain import ArtifactInput

        if not self._config.inbox_roots:
            raise RuntimeError("管理员尚未配置研究结果收件箱")
        folder = self._config.inbox_roots[0] / "orchestration" / checkpoint.plan.run_id / "results"
        folder.mkdir(parents=True, exist_ok=True)
        filename = f"component_{request.request_id.replace('-', '')}.json"
        path = folder / filename
        body = json.dumps(
            {
                "schema_version": "rk.component-result.v1",
                "request": request.to_dict(),
                "component_id": result.component_id,
                "component_receipt_id": result.component_receipt_id,
                "status": result.status,
                "payload": dict(result.payload),
                "artifact_ids": list(result.artifact_ids),
                "tool_requests": [item.to_dict() for item in result.tool_requests],
                "usage": dict(result.usage),
            },
            ensure_ascii=False,
            sort_keys=True,
            indent=2,
        ).encode("utf-8")
        path.write_bytes(body)
        artifact = self._kernel.import_artifact(
            checkpoint.plan.run_id,
            ArtifactInput(
                name=filename,
                path=str(path),
                sha256=hashlib.sha256(body).hexdigest(),
                byte_count=len(body),
                media_type="application/json",
            ),
            self._capability,
            logical_name=f"{request.work_kind.lower()}@{request.request_id}",
            role="COMPONENT_RESULT",
        )
        accounting_usage = dict(result.usage)
        if request.work_kind == WorkKind.TOOL_REQUEST.value:
            accounting_usage.setdefault("input_tokens_applicable", False)
            accounting_usage.setdefault("output_tokens_applicable", False)
        else:
            accounting_usage.setdefault("cost_unknown", "cost_microunits" not in accounting_usage)
        overruns = self._kernel.record_component_usage(
            run_id=checkpoint.plan.run_id,
            request_id=request.request_id,
            component=result.component_id or request.work_kind,
            usage=accounting_usage,
            capability=self._role_capability("main"),
        )
        if overruns:
            payload = {**dict(result.payload), "budget_fuse_resources": list(overruns)}
            result = replace(result, payload=_frozen(payload))
        return replace(result, artifact_ids=result.artifact_ids + (artifact.artifact_id,))

    def _integrate_tools(
        self, checkpoint: OrchestrationCheckpoint, item: _WorkItem
    ) -> tuple[OrchestrationCheckpoint, tuple[OrchestrationEvent, ...]]:
        keys = tuple(str(key) for key in item.context.get("request_keys", ()))
        feedback = {key: checkpoint.tool_feedback.get(key, {"status": "MISSING"}) for key in keys}
        parent_kind = WorkKind(str(item.context["parent_kind"]))
        context = {
            **dict(item.context.get("parent_context", {})),
            "previous_payload": dict(item.context.get("parent_payload", {})),
            "previous_artifact_ids": list(item.context.get("parent_artifact_ids", ())),
            "tool_feedback": feedback,
            "tool_cycle": int(item.context.get("tool_cycle", 1)),
        }
        resumed = _WorkItem(parent_kind, item.route_id, item.round, context)
        current = replace(checkpoint, queue=(resumed,) + checkpoint.queue)
        current, event = self._emit(
            current,
            "TOOL_FEEDBACK_ATTACHED",
            {
                "work_kind": parent_kind.value,
                "route_id": item.route_id,
                "request_keys": list(keys),
                "feedback_digests": {key: feedback[key].get("result_digest") for key in keys},
            },
        )
        return current, (event,)

    def _handle_result(
        self,
        checkpoint: OrchestrationCheckpoint,
        item: _WorkItem,
        result: ComponentResult,
    ) -> tuple[OrchestrationCheckpoint, tuple[OrchestrationEvent, ...]]:
        current = checkpoint
        events: list[OrchestrationEvent] = []
        payload = result.payload
        failed = result.status != "COMPLETED"

        if item.kind is WorkKind.CONTRACT_CLARIFY:
            if failed or bool(payload.get("ambiguous")):
                current, event = self._pause(
                    current,
                    PauseReason.CONTRACT_AMBIGUOUS,
                    details={"questions": list(payload.get("questions", ()))},
                )
                return current, (event,)
            current = replace(
                current,
                phase=ResearchPhase.LITERATURE,
                queue=current.queue + (_WorkItem(WorkKind.LITERATURE_AUDIT),),
            )

        elif item.kind is WorkKind.LITERATURE_AUDIT:
            exact = bool(payload.get("exact_match"))
            current = replace(
                current,
                literature_exact_match=exact,
                phase=ResearchPhase.ROUTES,
                composition_artifact_ids=current.composition_artifact_ids + result.artifact_ids,
            )
            current = replace(current, queue=current.queue + (_WorkItem(WorkKind.ROUTE_SCOUT),))

        elif item.kind is WorkKind.ROUTE_SCOUT:
            routes = self._routes_from_payload(current, payload)
            if len(routes) < current.plan.minimum_routes:
                if item.round == 0:
                    current = replace(
                        current,
                        queue=current.queue
                        + (
                            _WorkItem(
                                WorkKind.ROUTE_SCOUT,
                                round=1,
                                context={
                                    "rejected_routes": [route.to_dict() for route in routes],
                                    "required_distinct_routes": current.plan.minimum_routes,
                                },
                            ),
                        ),
                    )
                else:
                    current = replace(
                        current,
                        phase=ResearchPhase.COMPOSITION,
                        queue=current.queue + (_WorkItem(WorkKind.COMPOSITION_CHECK),),
                    )
            else:
                self._register_kernel_routes(current, routes)
                current = replace(
                    current,
                    phase=ResearchPhase.ROUTES,
                    routes=routes,
                    queue=current.queue
                    + tuple(_WorkItem(WorkKind.FALSIFY_ROUTE, route.route_id) for route in routes),
                )
                current, event = self._emit(
                    current,
                    "ROUTES_PROMOTED",
                    {
                        "route_ids": [route.route_id for route in routes],
                        "decisions": {
                            route.route_id: {
                                "method_card_id": route.method_card_id,
                                "promotion_reasons": list(route.promotion_reasons),
                                "independence_profile": dict(route.independence_profile),
                                "sentinel_result": dict(route.sentinel_result),
                            }
                            for route in routes
                        },
                    },
                )
                events.append(event)

        elif item.kind is WorkKind.FALSIFY_ROUTE:
            candidate = str(payload.get("candidate_kind", ""))
            hit = bool(payload.get("falsified")) or candidate == "COUNTEREXAMPLE"
            status = RouteStatus.REVIEWING if hit else RouteStatus.DEVELOPING
            current = self._update_route(
                current,
                item.route_id,
                status=status,
                candidate_kind="COUNTEREXAMPLE" if hit else None,
                artifact_ids=result.artifact_ids,
                open_obligations=tuple(str(x) for x in payload.get("open_obligations", ())),
            )
            next_kind = WorkKind.GAP_REVIEW if hit else WorkKind.DEVELOP_ROUTE
            current = replace(current, queue=current.queue + (_WorkItem(next_kind, item.route_id),))

        elif item.kind is WorkKind.DEVELOP_ROUTE:
            atomic_claim_id = self._register_atomic_claim(current, item, payload)
            atomic_obligations = [] if atomic_claim_id else ["ATOMIC_CLAIM_REQUIRED"]
            current = self._update_route(
                current,
                item.route_id,
                status=RouteStatus.REVIEWING,
                candidate_kind=str(payload.get("candidate_kind", "PROOF")),
                artifact_ids=result.artifact_ids,
                open_obligations=tuple(str(x) for x in payload.get("open_obligations", ()))
                + tuple(atomic_obligations),
            )
            current = replace(
                current,
                queue=current.queue
                + (
                    _WorkItem(
                        WorkKind.GAP_REVIEW,
                        item.route_id,
                        context={"atomic_claim_id": atomic_claim_id},
                    ),
                ),
            )

        elif item.kind is WorkKind.GAP_REVIEW:
            route = self._route(current, item.route_id)
            verdict = "FATAL" if failed else str(payload.get("verdict", "PASS")).upper()
            obligations = tuple(str(x) for x in payload.get("open_obligations", ()))
            atomic_claim_id = item.context.get("atomic_claim_id")
            if verdict in {"FATAL", "REVISE", "GAP"} and isinstance(atomic_claim_id, str):
                feedback = str(
                    payload.get("repair_feedback")
                    or payload.get("first_gap")
                    or "; ".join(obligations)
                    or "验证者拒绝该候选，但未提供更细的说明"
                )
                self._apply_kernel(
                    current.plan.run_id,
                    "VerifyAtomicClaim",
                    {
                        "contract_version": current.plan.contract_version,
                        "claim_id": atomic_claim_id,
                        "backend": "SOFT_VERIFIER",
                        "verdict": "REJECTED",
                        "repair_feedback": feedback,
                    },
                    capability=self._role_capability("verifier"),
                )
            if verdict == "FATAL":
                current = self._update_route(
                    current,
                    item.route_id,
                    status=RouteStatus.FAILED,
                    artifact_ids=result.artifact_ids,
                    open_obligations=obligations,
                )
                current = self._maybe_schedule_composition(current)
            elif (
                verdict in {"REVISE", "GAP"}
                and route.revision_round < current.plan.max_route_revisions
            ):
                current = self._update_route(
                    current,
                    item.route_id,
                    status=RouteStatus.REVISING,
                    artifact_ids=result.artifact_ids,
                    open_obligations=obligations,
                )
                current = replace(
                    current,
                    queue=current.queue
                    + (_WorkItem(WorkKind.REVISE_ROUTE, item.route_id, route.revision_round + 1),),
                )
            else:
                current = self._update_route(
                    current,
                    item.route_id,
                    status=RouteStatus.FORMALIZING,
                    artifact_ids=result.artifact_ids,
                    open_obligations=obligations,
                )
                current = replace(
                    current,
                    queue=current.queue
                    + (
                        _WorkItem(
                            WorkKind.FORMALIZE_ROUTE,
                            item.route_id,
                            context={"atomic_claim_id": atomic_claim_id},
                        ),
                    ),
                )

        elif item.kind is WorkKind.REVISE_ROUTE:
            atomic_claim_id = self._register_atomic_claim(current, item, payload)
            current = self._update_route(
                current,
                item.route_id,
                status=RouteStatus.REVIEWING,
                revision_round=item.round,
                artifact_ids=result.artifact_ids,
                open_obligations=tuple(str(x) for x in payload.get("open_obligations", ())),
            )
            current = replace(
                current,
                queue=current.queue
                + (
                    _WorkItem(
                        WorkKind.GAP_REVIEW,
                        item.route_id,
                        context={"atomic_claim_id": atomic_claim_id},
                    ),
                ),
            )

        elif item.kind is WorkKind.FORMALIZE_ROUTE:
            route_before_replay = self._route(current, item.route_id)
            replay = self._run_product_lean_replay(current, item.route_id, payload)
            structure = self._run_product_jixia(current, item.route_id, payload)
            verified = (
                replay is not None
                and replay.status == "COMPLETED"
                and bool(replay.payload.get("kernel_verdict") == "REPLAY_PASS")
            )
            replay_artifacts = replay.artifact_ids if replay is not None else ()
            structure_artifacts = structure.artifact_ids if structure is not None else ()
            replay_status = replay.status if replay is not None else "NOT_CONFIGURED"
            atomic_claim_id = item.context.get("atomic_claim_id")
            trusted_evidence_id = (
                replay.payload.get("trusted_evidence_id") if replay is not None else None
            )
            fact_verified = False
            if (
                verified
                and isinstance(atomic_claim_id, str)
                and isinstance(trusted_evidence_id, str)
            ):
                self._apply_kernel(
                    current.plan.run_id,
                    "VerifyAtomicClaim",
                    {
                        "contract_version": current.plan.contract_version,
                        "claim_id": atomic_claim_id,
                        "backend": "LEAN",
                        "verdict": "ACCEPTED",
                        "verification_ref": trusted_evidence_id,
                    },
                    capability=self._role_capability("verifier"),
                )
                fact_verified = True
            if replay is not None:
                current = replace(current, usage=current.usage.add(replay.usage))
                current, replay_event = self._emit(
                    current,
                    "COMPONENT_COMPLETED",
                    {
                        "request_id": replay.component_receipt_id,
                        "work_kind": "LEAN_REPLAY",
                        "role": None,
                        "route_id": item.route_id,
                        "round": item.round,
                        "status": replay.status,
                        "component_id": replay.component_id,
                        "component_receipt_id": replay.component_receipt_id,
                        "artifact_ids": list(replay.artifact_ids),
                        "usage": dict(replay.usage),
                        "kernel_verdict": replay.payload.get("kernel_verdict"),
                        "authority_ceiling": "ADAPTER_REPLAY_RESULT",
                    },
                )
                events.append(replay_event)
            if structure is not None:
                current = replace(current, usage=current.usage.add(structure.usage))
                current, structure_event = self._emit(
                    current,
                    "COMPONENT_COMPLETED",
                    {
                        "request_id": structure.component_receipt_id,
                        "work_kind": "JIXIA_STRUCTURE",
                        "role": None,
                        "route_id": item.route_id,
                        "round": item.round,
                        "status": structure.status,
                        "component_id": structure.component_id,
                        "component_receipt_id": structure.component_receipt_id,
                        "artifact_ids": list(structure.artifact_ids),
                        "usage": dict(structure.usage),
                        "authority_ceiling": "STRUCTURAL_FEEDBACK_ONLY",
                    },
                )
                events.append(structure_event)
            replay_feedback = dict(replay.payload) if replay is not None else {}
            if (
                replay is not None
                and not verified
                and route_before_replay.revision_round < current.plan.max_route_revisions
            ):
                first_failed = replay_feedback.get("first_failed_obligation_id")
                diagnostic = replay_feedback.get("diagnostic")
                current = self._update_route(
                    current,
                    item.route_id,
                    status=RouteStatus.REVISING,
                    artifact_ids=result.artifact_ids + replay_artifacts + structure_artifacts,
                    lean_replay_status=replay_status,
                    open_obligations=(str(first_failed or "LEAN_REPLAY_REPAIR_REQUIRED"),),
                )
                current = replace(
                    current,
                    queue=current.queue
                    + (
                        _WorkItem(
                            WorkKind.REVISE_ROUTE,
                            item.route_id,
                            route_before_replay.revision_round + 1,
                            {
                                "lean_feedback": {
                                    "first_failed_obligation_id": first_failed,
                                    "diagnostic": diagnostic,
                                    "status": replay_status,
                                }
                            },
                        ),
                    ),
                )
                current, feedback_event = self._emit(
                    current,
                    "LEAN_REPAIR_SCHEDULED",
                    {
                        "route_id": item.route_id,
                        "first_failed_obligation_id": first_failed,
                        "revision_round": route_before_replay.revision_round + 1,
                    },
                )
                events.append(feedback_event)
                return current, tuple(events)
            current = self._update_route(
                current,
                item.route_id,
                status=RouteStatus.SEMANTIC_AUDIT,
                artifact_ids=result.artifact_ids + replay_artifacts + structure_artifacts,
                machine_evidence=fact_verified,
                lean_replay_status=replay_status,
                open_obligations=tuple(str(x) for x in payload.get("open_obligations", ()))
                + (() if fact_verified else ("TRUSTED_ATOMIC_VERIFICATION_REQUIRED",)),
            )
            current = replace(
                current,
                queue=current.queue + (_WorkItem(WorkKind.SEMANTIC_AUDIT, item.route_id),),
            )

        elif item.kind is WorkKind.SEMANTIC_AUDIT:
            route = self._route(current, item.route_id)
            faithful = not failed and bool(payload.get("faithful", False))
            if not faithful and route.revision_round < current.plan.max_route_revisions:
                current = self._update_route(current, item.route_id, status=RouteStatus.REVISING)
                current = replace(
                    current,
                    queue=current.queue
                    + (
                        _WorkItem(
                            WorkKind.REVISE_ROUTE,
                            item.route_id,
                            route.revision_round + 1,
                            {"semantic_feedback": dict(payload)},
                        ),
                    ),
                )
            else:
                current = self._update_route(
                    current,
                    item.route_id,
                    status=RouteStatus.READY if faithful else RouteStatus.FAILED,
                    artifact_ids=result.artifact_ids,
                )
                current = self._maybe_schedule_composition(current)

        elif item.kind is WorkKind.COMPOSITION_CHECK:
            obligations = tuple(str(x) for x in payload.get("open_obligations", ()))
            closed = not failed and bool(payload.get("closed", False)) and not obligations
            current = replace(
                current,
                phase=ResearchPhase.COMPOSITION,
                composition_closed=closed,
                composition_artifact_ids=current.composition_artifact_ids + result.artifact_ids,
            )
            if not closed and current.composition_round < current.plan.max_composition_revisions:
                current = replace(
                    current,
                    queue=current.queue
                    + (
                        _WorkItem(
                            WorkKind.REVISE_COMPOSITION,
                            round=current.composition_round + 1,
                            context={"open_obligations": list(obligations)},
                        ),
                    ),
                )
            else:
                current, event = self._enter_human_review(current)
                events.append(event)

        elif item.kind is WorkKind.REVISE_COMPOSITION:
            current = replace(
                current,
                composition_round=item.round,
                composition_artifact_ids=current.composition_artifact_ids + result.artifact_ids,
                queue=current.queue + (_WorkItem(WorkKind.COMPOSITION_CHECK, round=item.round),),
            )

        elif item.kind is WorkKind.FINAL_SYNTHESIS:
            outcome = self._derive_outcome(current)
            current = replace(
                current,
                status=OrchestrationStatus.COMPLETED,
                phase=ResearchPhase.COMPLETE,
                queue=(),
                outcome=outcome.value,
                composition_artifact_ids=current.composition_artifact_ids + result.artifact_ids,
            )
            current, event = self._emit(
                current,
                "ORCHESTRATION_COMPLETED",
                {
                    "outcome": outcome.value,
                    "synthesis_artifact_ids": list(result.artifact_ids),
                    "composition_closed": current.composition_closed,
                    "authority_ceiling": "SOFT_CANDIDATE_ONLY",
                },
            )
            events.append(event)

        return current, tuple(events)

    def _register_kernel_routes(
        self, checkpoint: OrchestrationCheckpoint, routes: Sequence[RouteState]
    ) -> None:
        if self._kernel is None:
            return
        snapshot = self._kernel.inspect(checkpoint.plan.run_id)
        projection = getattr(snapshot, "projection", {})
        root_claim_id = projection.get("root_claim_id") if isinstance(projection, Mapping) else None
        existing = {
            str(item.get("label"))
            for item in projection.get("routes", ())
            if isinstance(item, Mapping)
        }
        for route in routes:
            if route.label in existing:
                continue
            self._apply_kernel(
                checkpoint.plan.run_id,
                "RegisterRoute",
                {
                    "contract_version": checkpoint.plan.contract_version,
                    "target_claim_id": root_claim_id,
                    "label": route.label,
                    "representation": route.representation,
                    "tool_family": route.tool_family,
                    "approach_root": {
                        "label": route.label,
                        "parent_root_ids": [],
                        "contact_epoch": 0,
                        "contamination": {"source": "orchestrated_route_scout"},
                    },
                    "budget_policy": {"attempts": checkpoint.plan.max_route_revisions + 1},
                },
            )

    def _inputs(self, checkpoint: OrchestrationCheckpoint, item: _WorkItem) -> dict[str, Any]:
        result: dict[str, Any] = {
            "contract": dict(checkpoint.plan.contract),
            "routes": [route.to_dict() for route in checkpoint.routes],
            "composition_closed": checkpoint.composition_closed,
            "human_reviews": [review.to_dict() for review in checkpoint.human_reviews],
            "research_hints": self._research_hints(checkpoint.plan.run_id),
            "verified_fact_subgraph": self._verified_fact_context(checkpoint, item),
            **dict(item.context),
        }
        if item.kind in {WorkKind.ROUTE_SCOUT, WorkKind.FALSIFY_ROUTE}:
            result["method_cards"] = self._research_fixtures.get("method_cards", [])
            result["sentinel_cases"] = self._research_fixtures.get("ac5_cases", [])
        if item.kind in {WorkKind.COMPOSITION_CHECK, WorkKind.REVISE_COMPOSITION}:
            result["glue_sentinels"] = self._research_fixtures.get("glue_cases", [])
        if item.kind is WorkKind.TOOL_REQUEST:
            result = {"tool_request": dict(item.context.get("request", {}))}
        return result

    def _research_hints(self, run_id: str) -> list[Mapping[str, Any]]:
        if self._kernel is None:
            return []
        snapshot = self._kernel.inspect(run_id)
        projection = getattr(snapshot, "projection", {})
        values = projection.get("research_hints", ()) if isinstance(projection, Mapping) else ()
        return [value for value in values if isinstance(value, Mapping)]

    def _register_atomic_claim(
        self,
        checkpoint: OrchestrationCheckpoint,
        item: _WorkItem,
        payload: Mapping[str, Any],
    ) -> str | None:
        """Persist one worker candidate without granting it verified-fact authority."""

        if self._kernel is None or self._capability is None or self._config is None:
            return None
        candidate = payload.get("atomic_claim")
        if not isinstance(candidate, Mapping):
            return None
        statement = str(candidate.get("statement", "")).strip()
        proof = str(candidate.get("proof", candidate.get("evidence", ""))).strip()
        claim_type = str(candidate.get("claim_type", "LEMMA")).upper()
        predecessors = candidate.get("predecessor_fact_ids", ())
        if (
            not statement
            or not proof
            or claim_type not in {"LEMMA", "AUXILIARY", "COUNTEREXAMPLE", "SIDE_FINDING"}
            or not isinstance(predecessors, Sequence)
            or isinstance(predecessors, (str, bytes))
            or not all(isinstance(value, str) for value in predecessors)
        ):
            return None
        fact_snapshot = self._kernel.inspect(
            checkpoint.plan.run_id, fact_query={"operation": "summary"}
        )
        projection = getattr(fact_snapshot, "projection", {})
        summary = projection.get("fact_graph", {}) if isinstance(projection, Mapping) else {}
        verified = set(summary.get("fact_ids", ())) if isinstance(summary, Mapping) else set()
        if any(value not in verified for value in predecessors):
            return None
        stable_label = str(
            candidate.get("stable_label") or f"{item.route_id or 'route'}-claim-{item.round + 1}"
        )
        snapshot = self._kernel.inspect(checkpoint.plan.run_id)
        snapshot_projection = getattr(snapshot, "projection", {})
        claims = (
            snapshot_projection.get("claims", ())
            if isinstance(snapshot_projection, Mapping)
            else ()
        )
        existing = next(
            (
                value
                for value in claims
                if isinstance(value, Mapping)
                and value.get("stable_label") == stable_label
                and value.get("lifecycle") == "ACTIVE"
            ),
            None,
        )
        if existing is not None:
            return str(existing["claim_id"])
        normalized = {
            "atomic": True,
            "statement": statement,
            "proof": proof,
            "source": candidate.get("source", "worker"),
            "claim_type": claim_type,
            "citations": list(candidate.get("citations", ())),
        }
        body = canonical_json_bytes(normalized)
        folder = self._config.inbox_roots[0] / "orchestration" / checkpoint.plan.run_id / "claims"
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{hashlib.sha256(body).hexdigest()}.json"
        path.write_bytes(body)
        from rk.domain import ArtifactInput

        worker_capability = self._role_capability("worker")
        artifact = self._kernel.import_artifact(
            checkpoint.plan.run_id,
            ArtifactInput(
                name="atomic_claim.json",
                path=str(path),
                sha256=hashlib.sha256(body).hexdigest(),
                byte_count=len(body),
                media_type="application/json",
            ),
            worker_capability,
            logical_name=f"atomic_claim@{stable_label}",
            role="CLAIM_STATEMENT",
        )
        self._apply_kernel(
            checkpoint.plan.run_id,
            "RegisterClaim",
            {
                "contract_version": checkpoint.plan.contract_version,
                "claim_kind": claim_type,
                "stable_label": stable_label,
                "statement_artifact_id": artifact.artifact_id,
                "statement_hash": hashlib.sha256(body).hexdigest(),
                "normalized_statement": normalized,
                "target_route_id": item.route_id,
            },
            capability=worker_capability,
        )
        refreshed = self._kernel.inspect(checkpoint.plan.run_id)
        refreshed_projection = getattr(refreshed, "projection", {})
        new_claim = next(
            value
            for value in refreshed_projection.get("claims", ())
            if isinstance(value, Mapping) and value.get("stable_label") == stable_label
        )
        claim_id = str(new_claim["claim_id"])
        for predecessor in predecessors:
            self._apply_kernel(
                checkpoint.plan.run_id,
                "RegisterClaimEdge",
                {
                    "contract_version": checkpoint.plan.contract_version,
                    "from_claim_id": predecessor,
                    "to_claim_id": claim_id,
                    "edge_kind": "DEPENDS_ON",
                    "direction": "FORWARD",
                    "justification_kind": "DEFINITIONAL",
                    "justification_ref": artifact.artifact_id,
                },
                capability=worker_capability,
            )
        return claim_id

    def _role_capability(self, role: str) -> VerifiedCapability:
        if self._capability is None:
            raise RuntimeError("role capability requires a managed product capability")
        if self._config is None:
            return self._capability
        if role == "main":
            return self._capability
        path_key = {"worker": "worker_capability_file", "verifier": "verifier_capability_file"}[
            role
        ]
        configured_path = self._config.product.get(path_key)
        if not isinstance(configured_path, str) or not configured_path:
            return self._capability
        from rk.capability import FileKeyResolver, HmacCapabilityVerifier

        if self._config.capability_key_path is None or self._config.capability_key_id is None:
            raise RuntimeError("role capability files require configured capability verification")
        path = Path(configured_path).expanduser()
        if not path.is_absolute():
            path = self._config.workspace_root / path
        action = "RegisterClaim" if role == "worker" else "VerifyAtomicClaim"
        return HmacCapabilityVerifier(
            FileKeyResolver(self._config.capability_key_path, self._config.capability_key_id),
            SystemClock(),
        ).verify(path.resolve(), action, None)

    def _verified_fact_context(
        self, checkpoint: OrchestrationCheckpoint, item: _WorkItem
    ) -> list[Mapping[str, Any]]:
        """Retrieve only the verified dependency context relevant to this work item."""

        if self._kernel is None or item.kind is WorkKind.TOOL_REQUEST:
            return []
        route = next(
            (candidate for candidate in checkpoint.routes if candidate.route_id == item.route_id),
            None,
        )
        # Plans deliberately freeze nested mappings. Serialize through the canonical wire
        # normalizer so the real product path accepts the same immutable values as tests.
        contract_text = canonical_json_bytes(checkpoint.plan.contract).decode("utf-8")
        terms = [item.kind.value, contract_text]
        if route is not None:
            terms.extend((route.label, route.representation, route.tool_family))
        try:
            negative = self._kernel.inspect(
                checkpoint.plan.run_id,
                fact_query={
                    "operation": "search_negative",
                    "query": " ".join(terms),
                    "limit": 5,
                },
            )
            matches = self._kernel.inspect(
                checkpoint.plan.run_id,
                fact_query={"operation": "search", "query": " ".join(terms), "limit": 8},
            )
            projection = getattr(matches, "projection", {})
            ranked = projection.get("fact_graph", ()) if isinstance(projection, Mapping) else ()
            fact_ids = [
                str(value["fact_id"])
                for value in ranked
                if isinstance(value, Mapping) and value.get("fact_id")
            ]
            if not fact_ids:
                negative_projection = getattr(negative, "projection", {})
                return [
                    {"negative_knowledge": value}
                    for value in negative_projection.get("fact_graph", ())
                    if isinstance(value, Mapping)
                ]
            closure = self._kernel.inspect(
                checkpoint.plan.run_id,
                fact_query={"operation": "dependency_closure", "fact_ids": fact_ids},
            )
            closure_projection = getattr(closure, "projection", {})
            values = (
                closure_projection.get("fact_graph", ())
                if isinstance(closure_projection, Mapping)
                else ()
            )
            negative_projection = getattr(negative, "projection", {})
            negative_values = negative_projection.get("fact_graph", ())
            return [value for value in values if isinstance(value, Mapping)] + [
                {"negative_knowledge": value}
                for value in negative_values
                if isinstance(value, Mapping)
            ]
        except (KeyError, ValueError):
            # A concurrent revocation may invalidate a search hit between the two reads.
            # The next work item rebuilds the view; unverified data is never substituted.
            return []

    def _routes_from_payload(
        self, checkpoint: OrchestrationCheckpoint, payload: Mapping[str, Any]
    ) -> tuple[RouteState, ...]:
        raw = payload.get("routes", ())
        if not isinstance(raw, Sequence) or isinstance(raw, (str, bytes)):
            return ()
        result: list[RouteState] = []
        seen_signatures: set[tuple[str, ...]] = set()
        for index, item in enumerate(raw[: checkpoint.plan.maximum_routes]):
            if not isinstance(item, Mapping):
                continue
            representation = str(item.get("representation", "")).strip()
            tool_family = str(item.get("tool_family", "")).strip()
            method_card_id = str(item.get("method_card_id", "")).strip()
            skeleton_raw = item.get("proof_skeleton", ())
            proof_skeleton = (
                tuple(str(step).strip() for step in skeleton_raw if str(step).strip())
                if isinstance(skeleton_raw, Sequence) and not isinstance(skeleton_raw, (str, bytes))
                else ()
            )
            sharp_example = str(item.get("sharp_example", "")).strip()
            near_miss = str(item.get("near_miss", "")).strip()
            fast_falsifier = str(item.get("fast_falsifier", "")).strip()
            sentinel = item.get("sentinel_result")
            independence = item.get("independence_profile")
            axes = (
                "idea_family",
                "derivation_family",
                "verification_family",
                "implementation_family",
                "retrieval_family",
            )
            if not isinstance(independence, Mapping) or any(
                not str(independence.get(axis, "")).strip() for axis in axes
            ):
                continue
            signature = tuple(str(independence[axis]).strip().casefold() for axis in axes)
            if (
                not representation
                or not tool_family
                or not method_card_id
                or len(proof_skeleton) < 2
                or not sharp_example
                or not near_miss
                or not fast_falsifier
                or not isinstance(sentinel, Mapping)
                or sentinel.get("status") not in {"PASSED", "REFUTED"}
                or signature in seen_signatures
            ):
                continue
            seen_signatures.add(signature)
            route_id = str(item.get("route_id") or f"route-{index + 1}")
            if any(existing.route_id == route_id for existing in result):
                route_id = f"route-{index + 1}"
            result.append(
                RouteState(
                    route_id=route_id,
                    label=str(item.get("label") or route_id),
                    representation=representation,
                    tool_family=tool_family,
                    status=RouteStatus.FALSIFYING,
                    method_card_id=method_card_id,
                    proof_skeleton=proof_skeleton,
                    sharp_example=sharp_example,
                    near_miss=near_miss,
                    fast_falsifier=fast_falsifier,
                    sentinel_result=_frozen(sentinel),
                    independence_profile=_frozen(
                        {axis: str(independence[axis]).strip() for axis in axes}
                    ),
                    promotion_reasons=(
                        "METHOD_CARD_COMPLETE",
                        "SENTINEL_EXECUTED",
                        "FIVE_AXIS_SIGNATURE_UNIQUE",
                        "PROOF_SKELETON_AND_BOUNDARY_CASES_PRESENT",
                    ),
                )
            )
        return tuple(result)

    def _route(self, checkpoint: OrchestrationCheckpoint, route_id: str | None) -> RouteState:
        for route in checkpoint.routes:
            if route.route_id == route_id:
                return route
        raise ValueError(f"unknown orchestration route: {route_id}")

    def _update_route(
        self,
        checkpoint: OrchestrationCheckpoint,
        route_id: str | None,
        *,
        status: RouteStatus | None = None,
        revision_round: int | None = None,
        artifact_ids: tuple[str, ...] = (),
        open_obligations: tuple[str, ...] | None = None,
        candidate_kind: str | None = None,
        machine_evidence: bool | None = None,
        lean_replay_status: str | None = None,
    ) -> OrchestrationCheckpoint:
        original = self._route(checkpoint, route_id)
        updated = replace(
            original,
            status=status or original.status,
            revision_round=(original.revision_round if revision_round is None else revision_round),
            artifact_ids=original.artifact_ids + artifact_ids,
            open_obligations=(
                original.open_obligations if open_obligations is None else open_obligations
            ),
            candidate_kind=candidate_kind or original.candidate_kind,
            machine_evidence=(
                original.machine_evidence if machine_evidence is None else machine_evidence
            ),
            lean_replay_status=(
                original.lean_replay_status if lean_replay_status is None else lean_replay_status
            ),
        )
        return replace(
            checkpoint,
            routes=tuple(
                updated if route.route_id == route_id else route for route in checkpoint.routes
            ),
        )

    def _run_product_lean_replay(
        self,
        checkpoint: OrchestrationCheckpoint,
        route_id: str | None,
        payload: Mapping[str, Any],
    ) -> ComponentResult | None:
        if self._config is None or "research-lean" not in self._config.adapter_profiles:
            return None
        source_text = payload.get("lean_candidate")
        if not isinstance(source_text, str) or not source_text.strip():
            return ComponentResult(
                status="MISSING_FORMAL_CANDIDATE",
                payload={"kernel_verdict": "NOT_RUN"},
                component_id="research-lean",
            )
        if not self._config.inbox_roots:
            raise RuntimeError("管理员尚未配置 Lean 候选收件箱")
        safe_route = re.sub(r"[^A-Za-z0-9_]", "_", route_id or "route")[:40]
        module = f"RKProduct.Run{checkpoint.plan.run_id.replace('-', '')}.{safe_route}.Main"
        relative = Path(*module.split(".")).with_suffix(".lean")
        profile = self._config.adapter_profiles["research-lean"]
        workspace = Path(str(profile["workspace_root"]))
        source_path = workspace / relative
        source_path.parent.mkdir(parents=True, exist_ok=True)
        declaration = _lean_declaration_name(source_text)
        if declaration is None:
            return ComponentResult(
                status="MISSING_FORMAL_DECLARATION",
                payload={"kernel_verdict": "NOT_RUN"},
                component_id="research-lean",
            )
        rendered = source_text.strip()
        if not rendered.startswith("import "):
            rendered = "import Mathlib\n\n" + rendered
        source_path.write_text(rendered + "\n", encoding="utf-8")
        request = ComponentRequest(
            request_id=self._id_factory(),
            run_id=checkpoint.plan.run_id,
            work_kind=WorkKind.TOOL_REQUEST.value,
            role=None,
            route_id=route_id,
            round=0,
            inputs=_frozen(
                {
                    "tool_request": {
                        "request_key": f"lean-replay-{safe_route}",
                        "tool": "research-lean",
                        "operation": "replay_lean",
                        "payload": {
                            "source_relpath": relative.as_posix(),
                            "output_relpath": relative.with_suffix(".olean").as_posix(),
                            "declarations": [declaration],
                        },
                    }
                }
            ),
            contract_scope=_frozen(
                {
                    "contract_id": checkpoint.plan.contract_id,
                    "contract_version": checkpoint.plan.contract_version,
                    "contract_hash": checkpoint.plan.contract_hash,
                    "statement_hash": checkpoint.plan.statement_hash,
                }
            ),
            budget_remaining=_frozen(checkpoint.usage.remaining(checkpoint.plan.budget)),
            hardware_plan=_frozen(checkpoint.plan.hardware.to_dict()),
        )
        raw = self._runtime.execute(request)
        replay = raw if isinstance(raw, ComponentResult) else ComponentResult.from_mapping(raw)
        return self._persist_component_result(checkpoint, request, replay)

    def _run_product_jixia(
        self,
        checkpoint: OrchestrationCheckpoint,
        route_id: str | None,
        payload: Mapping[str, Any],
    ) -> ComponentResult | None:
        """Run optional structural feedback on the exact current route source."""

        if self._config is None or "research-jixia" not in self._config.adapter_profiles:
            return None
        source_text = payload.get("lean_candidate")
        if not isinstance(source_text, str) or not source_text.strip():
            return None
        safe_route = re.sub(r"[^A-Za-z0-9_]", "_", route_id or "route")[:40]
        module = f"RKProduct.Run{checkpoint.plan.run_id.replace('-', '')}.{safe_route}.Main"
        relative = Path(*module.split(".")).with_suffix(".lean")
        request = ComponentRequest(
            request_id=self._id_factory(),
            run_id=checkpoint.plan.run_id,
            work_kind=WorkKind.TOOL_REQUEST.value,
            role=None,
            route_id=route_id,
            round=0,
            inputs=_frozen(
                {
                    "tool_request": {
                        "request_key": f"jixia-{safe_route}",
                        "tool": "research-jixia",
                        "operation": "analyze_lean",
                        "payload": {
                            "source_relpath": relative.as_posix(),
                            "output_relpath": relative.with_suffix(".olean").as_posix(),
                            "include_initializers": False,
                        },
                    }
                }
            ),
            contract_scope=_frozen(
                {
                    "contract_id": checkpoint.plan.contract_id,
                    "contract_version": checkpoint.plan.contract_version,
                    "contract_hash": checkpoint.plan.contract_hash,
                    "statement_hash": checkpoint.plan.statement_hash,
                }
            ),
            budget_remaining=_frozen(checkpoint.usage.remaining(checkpoint.plan.budget)),
            hardware_plan=_frozen(checkpoint.plan.hardware.to_dict()),
        )
        raw = self._runtime.execute(request)
        result = raw if isinstance(raw, ComponentResult) else ComponentResult.from_mapping(raw)
        return self._persist_component_result(checkpoint, request, result)

    def _maybe_schedule_composition(
        self, checkpoint: OrchestrationCheckpoint
    ) -> OrchestrationCheckpoint:
        terminal = {RouteStatus.READY, RouteStatus.REFUTED, RouteStatus.FAILED}
        already_queued = any(item.kind is WorkKind.COMPOSITION_CHECK for item in checkpoint.queue)
        if (
            checkpoint.routes
            and all(route.status in terminal for route in checkpoint.routes)
            and not already_queued
        ):
            return replace(
                checkpoint,
                phase=ResearchPhase.COMPOSITION,
                queue=checkpoint.queue + (_WorkItem(WorkKind.COMPOSITION_CHECK),),
            )
        return checkpoint

    def _derive_outcome(self, checkpoint: OrchestrationCheckpoint) -> ResearchOutcome:
        latest_review = checkpoint.human_reviews[-1] if checkpoint.human_reviews else None
        accepted = latest_review is not None and latest_review.verdict == "ACCEPTED"
        rejected = latest_review is not None and latest_review.verdict != "ACCEPTED"
        if checkpoint.literature_exact_match and accepted and not rejected:
            return ResearchOutcome.KNOWN_RESULT
        ready_verified = [
            route
            for route in checkpoint.routes
            if route.status is RouteStatus.READY and route.machine_evidence
        ]
        if not checkpoint.composition_closed or not accepted or rejected or not ready_verified:
            return ResearchOutcome.UNRESOLVED
        if any(route.candidate_kind == "COUNTEREXAMPLE" for route in ready_verified):
            return ResearchOutcome.DISPROVED_CANDIDATE
        return ResearchOutcome.PROVED_CANDIDATE

    def _enter_human_review(
        self, checkpoint: OrchestrationCheckpoint
    ) -> tuple[OrchestrationCheckpoint, OrchestrationEvent]:
        current = replace(
            checkpoint,
            status=OrchestrationStatus.WAITING_HUMAN_REVIEW,
            phase=ResearchPhase.HUMAN_REVIEW,
            pause_reason=PauseReason.HUMAN_REVIEW_REQUIRED.value,
        )
        return self._emit(
            current,
            "HUMAN_REVIEW_REQUESTED",
            {
                "composition_closed": current.composition_closed,
                "route_statuses": {route.route_id: route.status.value for route in current.routes},
                "required_review_fields": [
                    "review_id",
                    "reviewer_id",
                    "verdict",
                    "artifact_id",
                ],
            },
        )

    def _pause(
        self,
        checkpoint: OrchestrationCheckpoint,
        reason: PauseReason,
        *,
        details: Mapping[str, Any] | None = None,
    ) -> tuple[OrchestrationCheckpoint, OrchestrationEvent]:
        current = replace(
            checkpoint,
            status=OrchestrationStatus.PAUSED,
            pause_reason=reason.value,
        )
        return self._emit(
            current,
            "ORCHESTRATION_PAUSED",
            {"reason": reason.value, **dict(details or {})},
        )

    def _emit(
        self, checkpoint: OrchestrationCheckpoint, event_type: str, payload: Mapping[str, Any]
    ) -> tuple[OrchestrationCheckpoint, OrchestrationEvent]:
        event = OrchestrationEvent(
            sequence=checkpoint.next_sequence,
            event_id=self._id_factory(),
            event_type=event_type,
            recorded_at=format_utc(self._now()),
            payload=_frozen(payload),
        )
        return (
            replace(
                checkpoint,
                events=checkpoint.events + (event,),
                next_sequence=checkpoint.next_sequence + 1,
            ),
            event,
        )

    def _now(self) -> datetime:
        return self._clock.now()

    def _plan_for_run(self, run_id: str) -> OrchestrationPlan:
        if self._kernel is None or self._config is None:
            raise RuntimeError("run-id product methods require from_config")
        snapshot = self._kernel.inspect(run_id)
        projection = getattr(snapshot, "projection", {})
        record = projection.get("contract") if isinstance(projection, Mapping) else None
        if not isinstance(record, Mapping) or not isinstance(record.get("contract"), Mapping):
            raise ValueError("研究题目尚未登记完整合同")
        contract = dict(record["contract"])
        product = self._config.product
        budget_raw = product.get("orchestration_budget", {})
        if not isinstance(budget_raw, Mapping):
            budget_raw = {}
        budget = BudgetPlan(
            max_work_units=int(budget_raw.get("max_work_units", 64)),
            max_input_tokens=int(budget_raw.get("max_input_tokens", 1_000_000)),
            max_output_tokens=int(budget_raw.get("max_output_tokens", 250_000)),
            max_wall_time_ms=int(budget_raw.get("max_wall_time_ms", 3_600_000)),
        )
        hardware_raw = product.get("hardware_plan", {})
        if not isinstance(hardware_raw, Mapping):
            hardware_raw = {}
        requested_mode = HardwareMode(str(hardware_raw.get("mode", "AUTO")))
        configured_placements = hardware_raw.get("placements")
        if requested_mode is not HardwareMode.AUTO and isinstance(configured_placements, Mapping):
            hardware = HardwarePlan(
                mode=requested_mode.value,
                plan_digest=str(hardware_raw.get("plan_digest") or _digest(hardware_raw)),
                placements=_frozen(configured_placements),
            )
        else:
            assets = hardware_raw.get("local_assets", ())
            local_assets = (
                frozenset(str(item) for item in assets)
                if isinstance(assets, Sequence) and not isinstance(assets, (str, bytes))
                else frozenset()
            )
            inventory = detect_local_inventory(
                api_candidate_available=bool(hardware_raw.get("api_candidate_available", True)),
                public_retrieval_available=bool(
                    hardware_raw.get("public_retrieval_available", True)
                ),
                local_assets=local_assets,
            )
            decision = schedule_research(
                ScheduleRequest(
                    requested_mode=requested_mode,
                    retrieval_top_k=int(hardware_raw.get("retrieval_top_k", 8)),
                    require_jixia=bool(hardware_raw.get("require_jixia", False)),
                    allow_explicit_mode_fallback=bool(
                        hardware_raw.get("allow_explicit_mode_fallback", False)
                    ),
                ),
                inventory,
                (),
            )
            placements: dict[str, Any] = {
                step.component: {
                    "placement": step.placement,
                    "concurrency_group": step.concurrency_group,
                    "quality_contract": step.quality_contract,
                }
                for step in decision.steps
            }
            placements["_detected_hardware"] = {
                "summary": decision.hardware_summary,
                "fallback_reasons": list(decision.fallback_reasons),
                "identity_gaps": list(decision.identity_gaps),
                "final_replay_required": decision.final_replay_required,
            }
            hardware = HardwarePlan(
                mode=decision.executed_mode.value,
                plan_digest=decision.plan_digest,
                placements=_frozen(placements),
            )
        return OrchestrationPlan(
            run_id=run_id,
            contract_id=str(record.get("contract_id") or f"contract:{run_id}"),
            contract_version=int(getattr(snapshot, "current_contract_version", 1)),
            contract_hash=str(record.get("contract_hash") or record.get("statement_hash")),
            statement_hash=str(record.get("statement_hash")),
            contract=contract,
            budget=budget,
            hardware=hardware,
            minimum_routes=int(product.get("orchestration_minimum_routes", 2)),
            maximum_routes=int(product.get("orchestration_maximum_routes", 4)),
            max_route_revisions=int(product.get("orchestration_route_revisions", 2)),
            max_composition_revisions=int(product.get("orchestration_composition_revisions", 2)),
            max_tool_cycles=int(product.get("orchestration_tool_cycles", 2)),
        )

    def _ensure_kernel_started(self, run_id: str) -> None:
        """Perform only the normal kernel transitions needed before product orchestration."""

        if self._kernel is None or self._capability is None:
            raise RuntimeError("run-id product methods require from_config")
        from rk.domain import RunSnapshot

        snapshot = self._kernel.inspect(run_id)
        if not isinstance(snapshot, RunSnapshot):
            raise ValueError("无法读取研究状态")
        record = snapshot.projection.get("contract")
        if not isinstance(record, Mapping) or not isinstance(record.get("contract"), Mapping):
            raise ValueError("研究题目尚未登记完整合同")
        contract = record["contract"]
        artifact_id = str(record.get("contract_artifact_id", ""))
        statement_hash = str(record.get("statement_hash", ""))

        if not snapshot.projection.get("root_claim_id"):
            self._apply_kernel(
                run_id,
                "RegisterClaim",
                {
                    "contract_version": snapshot.current_contract_version,
                    "claim_kind": "ROOT",
                    "stable_label": "root",
                    "statement_artifact_id": artifact_id,
                    "statement_hash": statement_hash,
                    "normalized_statement": dict(contract),
                },
                capability=self._role_capability("worker"),
            )
            snapshot = self._kernel.inspect(run_id)
            assert isinstance(snapshot, RunSnapshot)
        if str(record.get("status")) == "DRAFT":
            self._apply_kernel(
                run_id,
                "FreezeContract",
                {
                    "contract_version": snapshot.current_contract_version,
                    "completeness_check_artifact_id": artifact_id,
                },
            )
            snapshot = self._kernel.inspect(run_id)
            assert isinstance(snapshot, RunSnapshot)
        if snapshot.status == "OPEN":
            configured = contract.get("budget_policy")
            budget_policy = (
                dict(configured)
                if isinstance(configured, Mapping) and configured.get("global")
                else {"global": {"INPUT_TOKEN": 1_000_000, "OUTPUT_TOKEN": 250_000}}
            )
            self._apply_kernel(
                run_id,
                "StartRun",
                {
                    "contract_version": snapshot.current_contract_version,
                    "literature_plan_artifact_id": artifact_id,
                    "budget_policy": budget_policy,
                },
            )
            snapshot = self._kernel.inspect(run_id)
            assert isinstance(snapshot, RunSnapshot)
        if snapshot.status not in {"RUNNING", "PAUSED"}:
            raise ValueError(f"研究当前状态为 {snapshot.status}，不能启动编排")

    def place_work(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        """Resolve one exact B13 work requirement through the shared S00 registry."""

        return place_registered_work(self._extensions, request)

    def consume_kernel_invalidation(self, invalidation: AuthorityInvalidation) -> None:
        """Consume only an invalidation whose exact kernel event is durably visible."""

        if self._kernel is None:
            raise RuntimeError("kernel invalidation consumption requires from_config")
        snapshot = self._kernel.inspect(invalidation.run_id)
        revision = getattr(snapshot, "revision", None)
        if not isinstance(revision, int) or revision < invalidation.research_revision:
            raise RuntimeError(
                "kernel invalidation event is not committed at the required revision"
            )
        cursor = 0
        found = False
        while True:
            page = self._kernel.inspect(invalidation.run_id, after_cursor=cursor, limit=500)
            events = getattr(page, "events", ())
            if any(
                event.get("event_id") == invalidation.kernel_event_id
                and event.get("revision") == invalidation.research_revision
                for event in events
            ):
                found = True
                break
            if not getattr(page, "has_more", False):
                break
            next_cursor = getattr(page, "next_cursor", None)
            if not isinstance(next_cursor, int) or next_cursor <= cursor:
                raise RuntimeError("kernel event pagination did not advance")
            cursor = next_cursor
        if not found:
            raise RuntimeError("kernel invalidation event is not durably visible")
        self._extensions.consume_invalidation("B11A_AUTHORITY_INVALIDATION", invalidation)

    def _apply_kernel(
        self,
        run_id: str,
        command_type: str,
        payload: Mapping[str, Any],
        *,
        capability: VerifiedCapability | None = None,
    ) -> None:
        if self._kernel is None or self._capability is None:
            raise RuntimeError("kernel transition requires from_config")
        from rk.domain import ApplyRequest, RunSnapshot, TypedCommand

        snapshot = self._kernel.inspect(run_id)
        if not isinstance(snapshot, RunSnapshot):
            raise ValueError("无法读取研究状态")
        receipt = self._kernel.apply(
            ApplyRequest(
                request_id=self._id_factory(),
                run_id=run_id,
                expected_revision=snapshot.revision,
                command=TypedCommand(command_type, _frozen(payload)),
            ),
            capability or self._capability,
        )
        if not receipt.accepted:
            raise ValueError(f"{command_type} 未完成: {receipt.rejection_code or 'UNKNOWN'}")

    def _checkpoint_path(self, run_id: str) -> Path:
        if self._config is None:
            raise RuntimeError("checkpoint persistence requires from_config")
        if not run_id or any(
            character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_"
            for character in run_id
        ):
            raise ValueError("run id is not safe for checkpoint storage")
        root = (self._config.workspace_root / "orchestration" / run_id).resolve()
        workspace = self._config.workspace_root.resolve()
        if not root.is_relative_to(workspace):
            raise ValueError("checkpoint path escaped the configured workspace")
        return root / "checkpoint.json"

    def _current_kernel_revision(self, run_id: str) -> int | None:
        if self._kernel is None:
            return None
        revision = getattr(self._kernel.inspect(run_id), "revision", None)
        return revision if isinstance(revision, int) else None

    def _save_checkpoint(self, checkpoint: OrchestrationCheckpoint) -> None:
        path = self._checkpoint_path(checkpoint.plan.run_id)
        if self._kernel is not None:
            snapshot = self._kernel.inspect(checkpoint.plan.run_id)
            revision = getattr(snapshot, "revision", None)
            if isinstance(revision, int):
                checkpoint = replace(checkpoint, kernel_revision=revision)
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_suffix(".tmp")
        temporary.write_text(
            json.dumps(checkpoint.to_dict(), ensure_ascii=False, sort_keys=True, indent=2) + "\n",
            encoding="utf-8",
        )
        os.replace(temporary, path)

    def _load_checkpoint(self, run_id: str) -> OrchestrationCheckpoint:
        path = self._checkpoint_path(run_id)
        if not path.is_file():
            raise ValueError("尚无研究编排进度；请先运行开始")
        value = json.loads(path.read_text(encoding="utf-8"))
        if not isinstance(value, Mapping):
            raise ValueError("研究 checkpoint 不是对象")
        claimed = value.get("checkpoint_digest")
        body = {key: item for key, item in value.items() if key != "checkpoint_digest"}
        if claimed != _digest(body):
            raise ValueError("研究 checkpoint 完整性检查失败")
        checkpoint = _checkpoint_from_mapping(value)
        if checkpoint.plan.run_id != run_id:
            raise ValueError("研究 checkpoint 所属研究编号不匹配")
        if self._kernel is not None:
            snapshot = self._kernel.inspect(run_id)
            revision = getattr(snapshot, "revision", None)
            contract_version = getattr(snapshot, "current_contract_version", None)
            revision_changed = isinstance(revision, int) and revision != checkpoint.kernel_revision
            contract_changed = (
                isinstance(contract_version, int)
                and contract_version != checkpoint.plan.contract_version
            )
            if revision_changed or contract_changed:
                # Facts, revocations, reviews, or a contract amendment changed the authoritative
                # kernel after this queue was frozen.  Never replay old tool feedback or reuse a
                # previously closed composition against that new truth state.
                new_plan = self._plan_for_run(run_id)
                checkpoint = OrchestrationCheckpoint(
                    plan=new_plan,
                    status=OrchestrationStatus.RUNNING,
                    phase=ResearchPhase.CONTRACT,
                    queue=(_WorkItem(WorkKind.CONTRACT_CLARIFY),),
                    routes=(),
                    usage=checkpoint.usage,
                    events=checkpoint.events,
                    next_sequence=checkpoint.next_sequence,
                    kernel_revision=revision if isinstance(revision, int) else None,
                )
                checkpoint, _ = self._emit(
                    checkpoint,
                    "CHECKPOINT_INVALIDATED_BY_KERNEL_CHANGE",
                    {
                        "previous_kernel_revision": value.get("kernel_revision"),
                        "current_kernel_revision": revision,
                        "previous_contract_version": value.get("plan", {}).get(
                            "contract_version"
                        ),
                        "current_contract_version": contract_version,
                    },
                )
                self._save_checkpoint(checkpoint)
        return checkpoint


class _ConfiguredComponentRuntime:
    """Minimal production adapter using registered profiles; tests inject richer runtimes."""

    def __init__(self, config: KernelConfig, environment: Mapping[str, str]) -> None:
        self._config = config
        self._environment = dict(environment)

    def execute(self, request: ComponentRequest) -> ComponentResult:
        from rk.adapters import (
            AdapterProfile,
            CurlHttpClient,
            LeanSearchAdapter,
            OpenAICompatibleAdapter,
        )

        if request.work_kind == WorkKind.TOOL_REQUEST.value:
            tool = request.inputs.get("tool_request", {})
            if isinstance(tool, Mapping) and str(tool.get("tool", "")).lower() in {
                "leansearch",
                "premise-search",
            }:
                profile_raw = self._config.adapter_profiles.get("research-search")
                if not isinstance(profile_raw, Mapping):
                    raise RuntimeError("管理员尚未配置 research-search")
                payload = tool.get("payload", {})
                payload = payload if isinstance(payload, Mapping) else {}
                result = LeanSearchAdapter(
                    AdapterProfile.from_mapping(profile_raw), client=CurlHttpClient()
                ).run(
                    {
                        "query": list(payload.get("query", [str(payload.get("statement", ""))])),
                        "num_results": int(payload.get("num_results", 8)),
                        "rerank": bool(payload.get("rerank", True)),
                        "retrieve_k": int(payload.get("retrieve_k", 50)),
                    }
                )
                return ComponentResult.from_mapping(result)
            raise RuntimeError("所请求工具没有注册到 ComponentRuntime")

        profile_raw = self._config.adapter_profiles.get("research-model")
        model = self._config.product.get("model")
        if not isinstance(profile_raw, Mapping) or not isinstance(model, str) or not model:
            raise RuntimeError("管理员尚未配置 research-model 与 product.model")
        prompt = (
            "你是 RK 编排器调用的数学角色。只输出一个 JSON 对象，不得使用代码围栏。"
            "你的输出只有软候选权限。根据 work_kind 完成任务，并保留 open_obligations。\n"
            + json.dumps(request.to_dict(), ensure_ascii=False, sort_keys=True)
        )
        raw = OpenAICompatibleAdapter(
            AdapterProfile.from_mapping(profile_raw), client=CurlHttpClient()
        ).run(
            {
                "prompt": prompt,
                "model": model,
                "max_tokens": int(self._config.product.get("model_max_tokens", 8192)),
                "environment": self._environment,
            }
        )
        if raw.get("status") != "COMPLETED":
            return ComponentResult.from_mapping(raw)
        payload = raw.get("payload")
        text = payload.get("text") if isinstance(payload, Mapping) else None
        try:
            parsed = json.loads(str(text))
        except (TypeError, ValueError):
            return ComponentResult(
                status="ADAPTER_SCHEMA_MISMATCH",
                payload={"open_obligations": ["ROLE_OUTPUT_NOT_JSON"]},
                usage=_frozen(raw.get("usage") if isinstance(raw.get("usage"), Mapping) else {}),
            )
        if not isinstance(parsed, Mapping):
            return ComponentResult(status="ADAPTER_SCHEMA_MISMATCH")
        return ComponentResult(
            status=str(parsed.get("status", "COMPLETED")),
            payload=_frozen(
                parsed.get("payload") if isinstance(parsed.get("payload"), Mapping) else parsed
            ),
            artifact_ids=tuple(str(item) for item in parsed.get("artifact_ids", ())),
            tool_requests=ComponentResult.from_mapping(parsed).tool_requests,
            usage=_frozen(raw.get("usage") if isinstance(raw.get("usage"), Mapping) else {}),
        )


def _load_research_fixtures(config: KernelConfig) -> dict[str, Any]:
    spec_root = config.schema_path.parent
    fixtures = spec_root / "fixtures"
    cards: list[dict[str, Any]] = []
    card_root = fixtures / "method_cards"
    if card_root.is_dir():
        for path in sorted(card_root.glob("*.json")):
            value = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(value, Mapping) and value.get("schema_version") == "rk.method_card.v1":
                cards.append(dict(value))
    cases: list[dict[str, Any]] = []
    ac5_path = fixtures / "ac5_cases.json"
    if ac5_path.is_file():
        value = json.loads(ac5_path.read_text(encoding="utf-8"))
        if isinstance(value, Mapping) and value.get("schema_version") == "rk.ac5.cases.v1":
            raw = value.get("cases", ())
            if isinstance(raw, Sequence) and not isinstance(raw, (str, bytes)):
                cases = [dict(item) for item in raw if isinstance(item, Mapping)]
    return {
        "method_cards": cards,
        "ac5_cases": cases,
        "glue_cases": [item for item in cases if item.get("class") == "GLUE_TRAP"],
    }


def _checkpoint_from_mapping(value: Mapping[str, Any]) -> OrchestrationCheckpoint:
    plan_raw = value["plan"]
    budget_raw = plan_raw["budget"]
    hardware_raw = plan_raw["hardware"]
    plan = OrchestrationPlan(
        run_id=str(plan_raw["run_id"]),
        contract_id=str(plan_raw["contract_id"]),
        contract_version=int(plan_raw["contract_version"]),
        contract_hash=str(plan_raw["contract_hash"]),
        statement_hash=str(plan_raw["statement_hash"]),
        contract=_frozen(plan_raw["contract"]),
        budget=BudgetPlan(**{key: int(budget_raw[key]) for key in BudgetPlan.__dataclass_fields__}),
        hardware=HardwarePlan(
            mode=str(hardware_raw["mode"]),
            plan_digest=str(hardware_raw["plan_digest"]),
            placements=_frozen(hardware_raw.get("placements", {})),
        ),
        minimum_routes=int(plan_raw["minimum_routes"]),
        maximum_routes=int(plan_raw["maximum_routes"]),
        max_route_revisions=int(plan_raw["max_route_revisions"]),
        max_composition_revisions=int(plan_raw["max_composition_revisions"]),
        max_tool_cycles=int(plan_raw["max_tool_cycles"]),
    )
    routes = tuple(
        RouteState(
            route_id=str(item["route_id"]),
            label=str(item["label"]),
            representation=str(item["representation"]),
            tool_family=str(item["tool_family"]),
            status=RouteStatus(str(item["status"])),
            revision_round=int(item["revision_round"]),
            artifact_ids=tuple(str(x) for x in item.get("artifact_ids", ())),
            open_obligations=tuple(str(x) for x in item.get("open_obligations", ())),
            candidate_kind=item.get("candidate_kind"),
            machine_evidence=bool(item.get("machine_evidence")),
            lean_replay_status=(
                str(item["lean_replay_status"])
                if item.get("lean_replay_status") is not None
                else None
            ),
            method_card_id=(
                str(item["method_card_id"]) if item.get("method_card_id") is not None else None
            ),
            proof_skeleton=tuple(str(x) for x in item.get("proof_skeleton", ())),
            sharp_example=(
                str(item["sharp_example"]) if item.get("sharp_example") is not None else None
            ),
            near_miss=str(item["near_miss"]) if item.get("near_miss") is not None else None,
            fast_falsifier=(
                str(item["fast_falsifier"]) if item.get("fast_falsifier") is not None else None
            ),
            sentinel_result=_frozen(item.get("sentinel_result", {})),
            independence_profile=_frozen(item.get("independence_profile", {})),
            promotion_reasons=tuple(str(x) for x in item.get("promotion_reasons", ())),
        )
        for item in value.get("routes", ())
    )
    queue = tuple(
        _WorkItem(
            WorkKind(str(item["kind"])),
            item.get("route_id"),
            int(item.get("round", 0)),
            _frozen(item.get("context", {})),
        )
        for item in value.get("queue", ())
    )
    events = tuple(
        OrchestrationEvent(
            int(item["sequence"]),
            str(item["event_id"]),
            str(item["event_type"]),
            str(item["recorded_at"]),
            _frozen(item.get("payload", {})),
        )
        for item in value.get("events", ())
    )
    reviews = tuple(HumanReview(**item) for item in value.get("human_reviews", ()))
    usage_raw = value.get("usage", {})
    return OrchestrationCheckpoint(
        plan=plan,
        status=OrchestrationStatus(str(value["status"])),
        phase=ResearchPhase(str(value["phase"])),
        queue=queue,
        routes=routes,
        usage=UsageLedger(
            **{key: int(usage_raw.get(key, 0)) for key in UsageLedger.__dataclass_fields__}
        ),
        events=events,
        next_sequence=int(value["next_sequence"]),
        pause_reason=value.get("pause_reason"),
        literature_exact_match=bool(value.get("literature_exact_match")),
        composition_closed=bool(value.get("composition_closed")),
        composition_round=int(value.get("composition_round", 0)),
        composition_artifact_ids=tuple(str(x) for x in value.get("composition_artifact_ids", ())),
        tool_feedback=_frozen(value.get("tool_feedback", {})),
        human_reviews=reviews,
        outcome=value.get("outcome"),
        kernel_revision=(
            int(value["kernel_revision"])
            if isinstance(value.get("kernel_revision"), int)
            else None
        ),
    )
