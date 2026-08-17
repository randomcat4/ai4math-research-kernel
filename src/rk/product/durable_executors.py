"""Complete typed-executor registry for the C00 durable command catalog.

Every catalog entry is present even when its deployment-owned B-package capability is
absent.  An absent capability resolves to a typed rejected ProductDecision; it is never
reported as process success or mathematical acceptance.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

from rk.product.api import ProductDecision, frozen_json
from rk.product.durable_runtime import DurableExecutor, TypedExecution
from rk.product.jobs import DurableJob, ExecutionOutcome, ExecutionReceipt

DURABLE_COMMAND_TYPES = (
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
)


@dataclass(frozen=True, slots=True)
class DurableExecutorPorts:
    """One explicitly typed B-package executor per durable product command."""

    start_research: DurableExecutor | None = None
    resume_research: DurableExecutor | None = None
    run_literature_query: DurableExecutor | None = None
    replay_source_snapshot: DurableExecutor | None = None
    batch_create_research: DurableExecutor | None = None
    assign_ablation: DurableExecutor | None = None
    import_research_lineage: DurableExecutor | None = None
    create_compute_task: DurableExecutor | None = None
    run_tool: DurableExecutor | None = None
    generate_candidate_tex: DurableExecutor | None = None
    compile_final_pdf: DurableExecutor | None = None
    retry_unknown_outcome: DurableExecutor | None = None
    deployment_operation: DurableExecutor | None = None


def build_durable_executors(
    ports: DurableExecutorPorts,
) -> Mapping[str, DurableExecutor]:
    """Return all 13 executors; startup can compare this set to the C00 catalog."""

    configured = {
        "START_RESEARCH": ports.start_research,
        "RESUME_RESEARCH": ports.resume_research,
        "RUN_LITERATURE_QUERY": ports.run_literature_query,
        "REPLAY_SOURCE_SNAPSHOT": ports.replay_source_snapshot,
        "BATCH_CREATE_RESEARCH": ports.batch_create_research,
        "ASSIGN_ABLATION": ports.assign_ablation,
        "IMPORT_RESEARCH_LINEAGE": ports.import_research_lineage,
        "CREATE_COMPUTE_TASK": ports.create_compute_task,
        "RUN_TOOL": ports.run_tool,
        "GENERATE_CANDIDATE_TEX": ports.generate_candidate_tex,
        "COMPILE_FINAL_PDF": ports.compile_final_pdf,
        "RETRY_UNKNOWN_OUTCOME": ports.retry_unknown_outcome,
        "DEPLOYMENT_OPERATION": ports.deployment_operation,
    }
    result: dict[str, DurableExecutor] = {}
    for command_type in DURABLE_COMMAND_TYPES:
        executor = configured[command_type]
        result[command_type] = _BoundExecutor(
            command_type,
            executor if executor is not None else _UnavailableExecutor(command_type),
        )
    return MappingProxyType(result)


@dataclass(frozen=True, slots=True)
class _BoundExecutor:
    command_type: str
    delegate: DurableExecutor

    def __call__(self, job: DurableJob, request: Mapping[str, Any]) -> TypedExecution:
        actual = _command_type(request)
        if actual != self.command_type or job.kind != self.command_type:
            raise ValueError(
                f"durable executor binding mismatch: expected {self.command_type}, "
                f"request={actual}, job={job.kind}"
            )
        _assert_job_scope(job, request)
        result = self.delegate(job, request)
        _assert_typed_execution(self.command_type, result)
        return result


@dataclass(frozen=True, slots=True)
class _UnavailableExecutor:
    command_type: str

    def __call__(self, job: DurableJob, request: Mapping[str, Any]) -> TypedExecution:
        scope = _scope(request)
        revision, contract = _fence(scope)
        code = f"{self.command_type}_CAPABILITY_NOT_DEPLOYED"
        decision = ProductDecision(
            accepted=False,
            rejection_code=code,
            revision_before=revision,
            revision_after=revision,
            contract_version=contract,
            event_cursor_after=0,
            missing_conditions=(
                frozen_json(
                    {
                        "code": "DEPLOYMENT_CAPABILITY",
                        "path": "/command/type",
                        "params": {
                            "command_type": self.command_type,
                            "job_id": job.job_id,
                        },
                    }
                ),
            ),
        )
        return TypedExecution(
            execution=ExecutionReceipt(
                outcome=ExecutionOutcome.FAILED,
                exit_code=None,
                result_refs=(),
                failure_code=code,
            ),
            decision=decision,
        )


def unknown_external_outcome(
    *,
    external_call_ref: str,
    result_refs: tuple[Mapping[str, Any], ...] = (),
) -> TypedExecution:
    """Return UNKNOWN only when a named external attempt has no trustworthy outcome."""

    if not external_call_ref.strip():
        raise ValueError("unknown external outcome requires a stable call reference")
    return TypedExecution(
        execution=ExecutionReceipt(
            outcome=ExecutionOutcome.OUTCOME_UNKNOWN,
            exit_code=None,
            result_refs=(
                *result_refs,
                MappingProxyType({"external_call_ref": external_call_ref}),
            ),
            failure_code="EXTERNAL_OUTCOME_UNKNOWN",
        ),
        decision=None,
    )


def rejected_execution(
    *,
    request: Mapping[str, Any],
    code: str,
    failure_code: str | None = None,
    result_refs: tuple[Mapping[str, Any], ...] = (),
) -> TypedExecution:
    """A B-service completed and produced a real domain rejection."""

    if not code.strip():
        raise ValueError("durable rejection code is required")
    revision, contract = _fence(_scope(request))
    return TypedExecution(
        execution=ExecutionReceipt(
            outcome=ExecutionOutcome.FAILED,
            exit_code=None,
            result_refs=result_refs,
            failure_code=failure_code or code,
        ),
        decision=ProductDecision(
            accepted=False,
            rejection_code=code,
            revision_before=revision,
            revision_after=revision,
            contract_version=contract,
            event_cursor_after=0,
        ),
    )


def domain_success(
    *,
    request: Mapping[str, Any],
    affected_entity_ids: tuple[str, ...],
    result_refs: tuple[Mapping[str, Any], ...],
    created_artifact_refs: tuple[Mapping[str, Any], ...] = (),
) -> TypedExecution:
    """Record execution success with no mathematical authority effect."""

    if not affected_entity_ids:
        raise ValueError("durable domain success requires affected stable identities")
    revision, contract = _fence(_scope(request))
    return TypedExecution(
        execution=ExecutionReceipt(
            outcome=ExecutionOutcome.SUCCEEDED,
            exit_code=0,
            result_refs=result_refs,
            failure_code=None,
        ),
        decision=ProductDecision(
            accepted=True,
            revision_before=revision,
            revision_after=revision,
            contract_version=contract,
            event_cursor_after=0,
            affected_entity_ids=affected_entity_ids,
            created_artifact_refs=tuple(frozen_json(dict(item)) for item in created_artifact_refs),
        ),
    )


def kernel_execution(
    *,
    decision: ProductDecision,
    result_refs: tuple[Mapping[str, Any], ...] = (),
) -> TypedExecution:
    """Wrap a real ResearchKernel decision; never infer it from exit status."""

    if decision.accepted and not decision.kernel_receipts:
        raise ValueError("accepted kernel execution requires a persisted kernel receipt")
    outcome = ExecutionOutcome.SUCCEEDED if decision.accepted else ExecutionOutcome.FAILED
    return TypedExecution(
        execution=ExecutionReceipt(
            outcome=outcome,
            exit_code=0 if decision.accepted else None,
            result_refs=result_refs,
            failure_code=None if decision.accepted else decision.rejection_code,
        ),
        decision=decision,
    )


def _assert_typed_execution(command_type: str, result: TypedExecution) -> None:
    execution = result.execution
    decision = result.decision
    if execution.outcome is ExecutionOutcome.SUCCEEDED:
        if decision is None or not decision.accepted:
            raise ValueError(
                f"{command_type} reported SUCCEEDED without an accepted typed decision"
            )
    elif execution.outcome is ExecutionOutcome.OUTCOME_UNKNOWN:
        if decision is not None:
            raise ValueError(f"{command_type} UNKNOWN cannot contain a decision")
        if not execution.result_refs:
            raise ValueError(f"{command_type} UNKNOWN requires an external call reference")
    elif execution.outcome is ExecutionOutcome.FAILED:
        if decision is None or decision.accepted or not execution.failure_code:
            raise ValueError(f"{command_type} FAILED requires a rejected decision and failure code")
    elif execution.outcome is ExecutionOutcome.CANCELLED and (
        decision is None or decision.accepted
    ):
        raise ValueError(f"{command_type} CANCELLED requires a rejected typed decision")
    if (
        command_type == "RUN_TOOL"
        and decision is not None
        and decision.accepted
        and decision.kernel_receipts
    ):
        raise ValueError("RUN_TOOL cannot claim mathematical kernel authority")


def _command_type(request: Mapping[str, Any]) -> str:
    command = request.get("command")
    if not isinstance(command, Mapping):
        raise ValueError("durable request command must be an object")
    value = command.get("type")
    if not isinstance(value, str) or not value:
        raise ValueError("durable request command type is missing")
    return value


def _scope(request: Mapping[str, Any]) -> Mapping[str, Any]:
    value = request.get("scope")
    if not isinstance(value, Mapping):
        raise ValueError("durable request scope must be an object")
    return value


def _fence(scope: Mapping[str, Any]) -> tuple[int, int]:
    kind = scope.get("kind")
    if kind == "RUN":
        revision = scope.get("expected_revision")
        contract = scope.get("expected_contract_version")
    elif kind in {"GLOBAL", "DEPLOYMENT"}:
        revision = scope.get("expected_deployment_revision")
        contract = 0
    else:
        raise ValueError("durable request scope kind is invalid")
    if (
        isinstance(revision, bool)
        or not isinstance(revision, int)
        or revision < 0
        or isinstance(contract, bool)
        or not isinstance(contract, int)
        or contract < 0
    ):
        raise ValueError("durable request fence is invalid")
    return revision, contract


def _assert_job_scope(job: DurableJob, request: Mapping[str, Any]) -> None:
    scope = _scope(request)
    kind = scope.get("kind")
    if kind != job.scope_kind:
        raise ValueError("durable job scope differs from immutable request")
    if kind == "RUN" and scope.get("run_id") != job.run_id:
        raise ValueError("durable job run differs from immutable request")
    if kind in {"GLOBAL", "DEPLOYMENT"} and (scope.get("deployment_id") != job.deployment_id):
        raise ValueError("durable job deployment differs from immutable request")


__all__ = [
    "DURABLE_COMMAND_TYPES",
    "DurableExecutorPorts",
    "build_durable_executors",
    "domain_success",
    "kernel_execution",
    "rejected_execution",
    "unknown_external_outcome",
]
