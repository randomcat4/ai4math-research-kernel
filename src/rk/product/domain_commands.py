"""Strict synchronous domain-command authority composition.

The module executes no durable work and owns no mathematical state machine. It checks
the public fence and authenticated capability, calls one explicitly bound real service,
then audits that service's receipt boundary.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from types import MappingProxyType
from typing import Protocol

from rk.domain import VerifiedCapability
from rk.product.api import (
    DeploymentScope,
    GlobalScope,
    JsonObject,
    ProductCommand,
    ProductDecision,
    ProductSession,
    RunScope,
    frozen_json,
)
from rk.product.authority import CapabilitySource, ProductAuthority


class DomainCommandError(RuntimeError):
    """A synchronous binding, session, scope, fence, or receipt is invalid."""


class DomainCommandNotBound(DomainCommandError):
    """Startup omitted or duplicated one normative immediate command binding."""


class DurableCommandMisrouted(DomainCommandError):
    """B03-owned work was incorrectly sent through the synchronous authority port."""


class DomainCommandRejected(DomainCommandError):
    """One real service rejected without mutating authority state."""

    def __init__(
        self,
        code: str,
        *,
        missing_conditions: Sequence[JsonObject] = (),
    ) -> None:
        if not code:
            raise ValueError("domain rejection code is required")
        super().__init__(code)
        self.code = code
        self.missing_conditions = tuple(missing_conditions)


class AuthorityBoundary(StrEnum):
    DOMAIN_ONLY = "DOMAIN_ONLY"
    KERNEL_REQUIRED = "KERNEL_REQUIRED"
    KERNEL_CREATE = "KERNEL_CREATE"


@dataclass(frozen=True, slots=True)
class CommandFence:
    scope_kind: str
    scope_id: str
    revision: int
    contract_version: int

    def __post_init__(self) -> None:
        if self.scope_kind not in {"GLOBAL", "RUN", "DEPLOYMENT"}:
            raise ValueError("command fence scope kind is invalid")
        if not self.scope_id or self.revision < 0 or self.contract_version < 0:
            raise ValueError("command fence identity or revision is invalid")
        if self.scope_kind == "RUN" and self.contract_version < 1:
            raise ValueError("RUN command fence requires a positive contract version")
        if self.scope_kind != "RUN" and self.contract_version != 0:
            raise ValueError("non-RUN command fence cannot carry a contract version")


class CommandFenceSource(Protocol):
    """Read the same authoritative fence used by query and service CAS checks."""

    def current(self, request: ProductCommand) -> CommandFence: ...


@dataclass(frozen=True, slots=True)
class DomainInvocation:
    session: ProductSession
    request: ProductCommand
    capability: VerifiedCapability
    fence: CommandFence


class DomainCommandHandler(Protocol):
    def __call__(self, invocation: DomainInvocation) -> ProductDecision: ...


@dataclass(frozen=True, slots=True)
class CommandSpec:
    package: str
    scopes: frozenset[str]
    authority_boundary: AuthorityBoundary
    default_roles: frozenset[str]


@dataclass(frozen=True, slots=True)
class DomainCommandBinding:
    command_type: str
    package: str
    allowed_roles: frozenset[str]
    handler: DomainCommandHandler

    def __post_init__(self) -> None:
        if self.command_type not in IMMEDIATE_COMMAND_SPECS:
            raise ValueError(f"not an immediate command: {self.command_type}")
        if (
            not self.package
            or not self.allowed_roles
            or any(not role.strip() for role in self.allowed_roles)
        ):
            raise ValueError("domain command package and allowed roles are required")


def _spec(
    package: str,
    scopes: str | tuple[str, ...],
    boundary: AuthorityBoundary,
    *roles: str,
) -> CommandSpec:
    values = (scopes,) if isinstance(scopes, str) else scopes
    return CommandSpec(package, frozenset(values), boundary, frozenset(roles))


IMMEDIATE_COMMAND_SPECS: Mapping[str, CommandSpec] = MappingProxyType(
    {
        "CREATE_RESEARCH": _spec("C01", "GLOBAL", AuthorityBoundary.KERNEL_CREATE, "MAIN"),
        "CONFIRM_CONTRACT": _spec("B07b", "RUN", AuthorityBoundary.KERNEL_REQUIRED, "MAIN"),
        "AMEND_CONTRACT": _spec("B07b", "RUN", AuthorityBoundary.KERNEL_REQUIRED, "MAIN"),
        "APPLY_ROUTE_PLAN": _spec("B09a", "RUN", AuthorityBoundary.DOMAIN_ONLY, "MAIN"),
        "PAUSE_RESEARCH": _spec("B03", "RUN", AuthorityBoundary.KERNEL_REQUIRED, "MAIN"),
        "CANCEL_RESEARCH": _spec("B03", "RUN", AuthorityBoundary.KERNEL_REQUIRED, "MAIN"),
        "SUBMIT_GUIDANCE": _spec("B14", "RUN", AuthorityBoundary.DOMAIN_ONLY, "MAIN"),
        "WITHDRAW_GUIDANCE": _spec("B14", "RUN", AuthorityBoundary.DOMAIN_ONLY, "MAIN"),
        "SUBMIT_CLAIM": _spec("B10", "RUN", AuthorityBoundary.KERNEL_REQUIRED, "WORKER"),
        "IMPORT_VERIFICATION": _spec("B10", "RUN", AuthorityBoundary.KERNEL_REQUIRED, "WORKER"),
        "CONFIRM_REVOKE": _spec("B11b", "RUN", AuthorityBoundary.KERNEL_REQUIRED, "MAIN"),
        "REGISTER_BRIDGE_SPEC": _spec("B06b", "RUN", AuthorityBoundary.KERNEL_REQUIRED, "MAIN"),
        "SUBMIT_CLOSURE_WITNESS": _spec("B06b", "RUN", AuthorityBoundary.KERNEL_REQUIRED, "MAIN"),
        "CREATE_REVIEW_TASK": _spec("B05b", "RUN", AuthorityBoundary.DOMAIN_ONLY, "MAIN"),
        "CLAIM_REVIEW_TASK": _spec("B05b", "RUN", AuthorityBoundary.DOMAIN_ONLY, "REVIEWER"),
        "SUBMIT_REVIEW": _spec("B05b", "RUN", AuthorityBoundary.DOMAIN_ONLY, "REVIEWER"),
        "SUBMIT_PAPER_REVIEW": _spec(
            "B15a", "RUN", AuthorityBoundary.KERNEL_REQUIRED, "PAPER_REVIEWER"
        ),
        "FINALIZE_RESEARCH": _spec("B15a", "RUN", AuthorityBoundary.KERNEL_REQUIRED, "MAIN"),
        "CONFIRM_MATERIAL_EXTRACTION": _spec("B07a", "RUN", AuthorityBoundary.DOMAIN_ONLY, "MAIN"),
        "REVIEW_THEOREM_APPLICABILITY": _spec(
            "B08b", "RUN", AuthorityBoundary.DOMAIN_ONLY, "LITERATURE_REVIEWER"
        ),
        "FREEZE_PROBLEM_POOL": _spec("B17", "GLOBAL", AuthorityBoundary.DOMAIN_ONLY, "MAIN"),
        "REGISTER_BRIDGE_OPPORTUNITY": _spec(
            "B09c", "RUN", AuthorityBoundary.DOMAIN_ONLY, "MAIN", "WORKER"
        ),
        "CANCEL_JOB": _spec(
            "B03",
            ("RUN", "DEPLOYMENT"),
            AuthorityBoundary.DOMAIN_ONLY,
            "MAIN",
            "WORKER",
            "ADMIN",
        ),
    }
)

NON_SYNCHRONOUS_COMMANDS: Mapping[str, str] = MappingProxyType(
    {
        "START_RESEARCH": "DURABLE_JOB",
        "RESUME_RESEARCH": "DURABLE_JOB",
        "GENERATE_CANDIDATE_TEX": "DURABLE_JOB",
        "COMPILE_FINAL_PDF": "DURABLE_JOB",
        "RUN_LITERATURE_QUERY": "EXTERNAL_SIDE_EFFECT",
        "REPLAY_SOURCE_SNAPSHOT": "DURABLE_JOB",
        "BATCH_CREATE_RESEARCH": "DURABLE_JOB",
        "ASSIGN_ABLATION": "DURABLE_JOB",
        "IMPORT_RESEARCH_LINEAGE": "DURABLE_JOB",
        "CREATE_COMPUTE_TASK": "DURABLE_JOB",
        "RUN_TOOL": "EXTERNAL_SIDE_EFFECT",
        "RETRY_UNKNOWN_OUTCOME": "EXTERNAL_SIDE_EFFECT",
        "DEPLOYMENT_OPERATION": "DURABLE_JOB",
    }
)


class DomainCommandAuthority:
    """Complete synchronous implementation of the command-service authority port."""

    def __init__(
        self,
        *,
        capabilities: CapabilitySource,
        fences: CommandFenceSource,
        bindings: Sequence[DomainCommandBinding],
    ) -> None:
        by_type: dict[str, DomainCommandBinding] = {}
        for binding in bindings:
            spec = IMMEDIATE_COMMAND_SPECS[binding.command_type]
            if binding.command_type in by_type:
                raise DomainCommandNotBound(
                    f"duplicate synchronous binding: {binding.command_type}"
                )
            if binding.package != spec.package:
                raise DomainCommandNotBound(
                    f"{binding.command_type} package must be {spec.package}"
                )
            if binding.allowed_roles != spec.default_roles:
                raise DomainCommandNotBound(
                    f"{binding.command_type} role boundary differs from the contract"
                )
            by_type[binding.command_type] = binding
        missing = set(IMMEDIATE_COMMAND_SPECS) - set(by_type)
        if missing:
            raise DomainCommandNotBound(
                "missing synchronous command bindings: " + ", ".join(sorted(missing))
            )
        self._capabilities = capabilities
        self._fences = fences
        self._bindings = MappingProxyType(by_type)

    def apply(self, session: ProductSession, request: ProductCommand) -> ProductDecision:
        if request.command_type in NON_SYNCHRONOUS_COMMANDS:
            execution = NON_SYNCHRONOUS_COMMANDS[request.command_type]
            raise DurableCommandMisrouted(
                f"{request.command_type} is {execution} and must be submitted to B03"
            )
        try:
            binding = self._bindings[request.command_type]
            spec = IMMEDIATE_COMMAND_SPECS[request.command_type]
        except KeyError as error:
            raise DomainCommandNotBound(
                f"command is not in the frozen catalog: {request.command_type}"
            ) from error
        scope_kind, scope_id, run_id = _request_scope(request)
        if scope_kind not in spec.scopes:
            raise DomainCommandError(f"{request.command_type} does not accept {scope_kind} scope")
        capability = self._capabilities.resolve(
            session,
            action=request.command_type,
            run_id=run_id,
        )
        _assert_session_capability(session, capability, request.command_type, run_id)
        if capability.subject_role is None or capability.subject_role not in binding.allowed_roles:
            return _rejected(request, "CAPABILITY_DENIED")
        fence = self._fences.current(request)
        try:
            _assert_fence(request, fence, scope_kind, scope_id)
            decision = binding.handler(DomainInvocation(session, request, capability, fence))
        except DomainCommandRejected as rejection:
            return _rejected(
                request,
                rejection.code,
                fence=fence,
                missing_conditions=rejection.missing_conditions,
            )
        _assert_decision(request, decision, fence, spec.authority_boundary)
        return decision


def kernel_handler(authority: ProductAuthority) -> DomainCommandHandler:
    """Adapt the existing sole kernel writer without exposing it to domain services."""

    def apply(invocation: DomainInvocation) -> ProductDecision:
        return authority.apply(invocation.session, invocation.request)

    return apply


def bindings_from_handlers(
    handlers: Mapping[str, DomainCommandHandler],
) -> tuple[DomainCommandBinding, ...]:
    """Freeze p00 real-service closures into the complete immediate command set."""

    unknown = set(handlers) - set(IMMEDIATE_COMMAND_SPECS)
    missing = set(IMMEDIATE_COMMAND_SPECS) - set(handlers)
    if unknown or missing:
        raise DomainCommandNotBound(
            f"handler set differs; missing={sorted(missing)}, unknown={sorted(unknown)}"
        )
    return tuple(
        DomainCommandBinding(
            command_type=command_type,
            package=spec.package,
            allowed_roles=spec.default_roles,
            handler=handlers[command_type],
        )
        for command_type, spec in IMMEDIATE_COMMAND_SPECS.items()
    )


def _request_scope(request: ProductCommand) -> tuple[str, str, str | None]:
    if isinstance(request.scope, RunScope):
        return "RUN", request.scope.run_id, request.scope.run_id
    if isinstance(request.scope, GlobalScope):
        return "GLOBAL", request.scope.deployment_id, None
    if isinstance(request.scope, DeploymentScope):
        return "DEPLOYMENT", request.scope.deployment_id, None
    raise TypeError("product command scope is unknown")


def _assert_session_capability(
    session: ProductSession,
    capability: VerifiedCapability,
    action: str,
    run_id: str | None,
) -> None:
    if capability.capability_id not in session.capability_ids:
        raise DomainCommandError("resolved capability is not authenticated in this session")
    if capability.subject_id != session.principal_subject_id:
        raise DomainCommandError("resolved capability belongs to another principal")
    if not capability.allows(action, run_id):
        raise DomainCommandError("resolved capability does not grant this action and scope")


def _assert_fence(
    request: ProductCommand,
    fence: CommandFence,
    scope_kind: str,
    scope_id: str,
) -> None:
    if fence.scope_kind != scope_kind or fence.scope_id != scope_id:
        raise DomainCommandError("authority fence belongs to another command scope")
    scope = request.scope
    if isinstance(scope, RunScope):
        if scope.expected_revision != fence.revision:
            raise DomainCommandRejected("REVISION_CONFLICT")
        if scope.expected_contract_version != fence.contract_version:
            raise DomainCommandRejected("CONTRACT_VERSION_MISMATCH")
    elif scope.expected_deployment_revision != fence.revision:
        raise DomainCommandRejected("REVISION_CONFLICT")


def _assert_decision(
    request: ProductCommand,
    decision: ProductDecision,
    fence: CommandFence,
    boundary: AuthorityBoundary,
) -> None:
    if decision.revision_before != fence.revision:
        raise DomainCommandError("service receipt revision_before differs from authority fence")
    if decision.contract_version != fence.contract_version:
        raise DomainCommandError("service receipt contract version differs from authority fence")
    if decision.revision_after < decision.revision_before:
        raise DomainCommandError("service receipt moves revision backwards")
    if decision.event_cursor_after < 0:
        raise DomainCommandError("service receipt event cursor is invalid")
    if decision.accepted:
        if decision.rejection_code is not None or decision.missing_conditions:
            raise DomainCommandError("accepted service receipt contains rejection fields")
    else:
        if not decision.rejection_code:
            raise DomainCommandError("rejected service receipt has no typed rejection code")
        if (
            decision.revision_after != decision.revision_before
            or decision.created_artifact_refs
            or decision.created_run_id is not None
            or decision.kernel_receipts
        ):
            raise DomainCommandError("rejected service receipt reports authority mutation")
        return
    if boundary is AuthorityBoundary.DOMAIN_ONLY:
        if decision.revision_after != fence.revision or decision.kernel_receipts:
            raise DomainCommandError(
                "domain-only command cannot advance or impersonate kernel authority"
            )
    elif boundary is AuthorityBoundary.KERNEL_REQUIRED:
        if not decision.kernel_receipts:
            raise DomainCommandError("mathematical authority command lacks a kernel receipt")
    elif boundary is AuthorityBoundary.KERNEL_CREATE and (
        request.command_type != "CREATE_RESEARCH" or not decision.created_run_id
    ):
        raise DomainCommandError("kernel create receipt has no created run identity")


def _rejected(
    request: ProductCommand,
    code: str,
    *,
    fence: CommandFence | None = None,
    missing_conditions: Sequence[JsonObject] = (),
) -> ProductDecision:
    if fence is None:
        scope = request.scope
        revision = (
            scope.expected_revision
            if isinstance(scope, RunScope)
            else scope.expected_deployment_revision
        )
        contract = scope.expected_contract_version if isinstance(scope, RunScope) else 0
    else:
        revision = fence.revision
        contract = fence.contract_version
    return ProductDecision(
        accepted=False,
        rejection_code=code,
        revision_before=revision,
        revision_after=revision,
        contract_version=contract,
        event_cursor_after=0,
        missing_conditions=tuple(frozen_json(dict(item)) for item in missing_conditions),
    )


__all__ = [
    "IMMEDIATE_COMMAND_SPECS",
    "NON_SYNCHRONOUS_COMMANDS",
    "AuthorityBoundary",
    "CommandFence",
    "CommandFenceSource",
    "CommandSpec",
    "DomainCommandAuthority",
    "DomainCommandBinding",
    "DomainCommandError",
    "DomainCommandHandler",
    "DomainCommandNotBound",
    "DomainCommandRejected",
    "DomainInvocation",
    "DurableCommandMisrouted",
    "bindings_from_handlers",
    "kernel_handler",
]
