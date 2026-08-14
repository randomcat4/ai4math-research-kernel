"""Product catalog bindings for mature math adapters without replacing them."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from types import MappingProxyType
from typing import Any, Protocol

from rk.extensions import ToolReceipt
from rk.product.compute import (
    AuthorityCeiling,
    ResourceUsage,
    ToolAvailability,
    ToolFunctionSpec,
)
from rk.product.tool_runs import ToolCatalogStore
from rk.wire import canonical_json_bytes


class ProductToolAdapterError(RuntimeError):
    """A catalog binding, probe, accounting record, or adapter receipt is inconsistent."""


class RunnableAdapter(Protocol):
    def run(self, request: Mapping[str, Any]) -> Mapping[str, Any]: ...


@dataclass(frozen=True, slots=True)
class AdapterBinding:
    tool_id: str
    function_name: str
    provider: str
    model_id: str
    adapter_version: str
    build_version: str
    profile_id: str
    function_schema: Mapping[str, Any]
    authority_ceiling: AuthorityCeiling

    def __post_init__(self) -> None:
        values = (
            self.tool_id,
            self.function_name,
            self.provider,
            self.model_id,
            self.adapter_version,
            self.build_version,
            self.profile_id,
        )
        if any(not value or value != value.strip() for value in values):
            raise ValueError("adapter binding identities must be non-empty and trimmed")

    @property
    def key(self) -> tuple[str, str, str]:
        return self.tool_id, self.model_id, self.function_name

    def specification(self, availability: ToolAvailability) -> ToolFunctionSpec:
        schema = dict(self.function_schema)
        return ToolFunctionSpec(
            tool_id=self.tool_id,
            tool_version=self.model_id,
            function_name=self.function_name,
            provider=self.provider,
            build_version=f"{self.adapter_version}:{self.build_version}",
            profile_id=self.profile_id,
            function_schema=schema,
            function_schema_digest=hashlib.sha256(canonical_json_bytes(schema)).hexdigest(),
            availability=availability,
            authority_ceiling=self.authority_ceiling,
        )


@dataclass(frozen=True, slots=True)
class DeploymentProbe:
    key: tuple[str, str, str]
    provider: str
    build_version: str
    profile_id: str
    receipt_artifact_id: str

    def __post_init__(self) -> None:
        if any(
            not value for value in (*self.key, self.provider, self.build_version, self.profile_id)
        ):
            raise ValueError("probe binding is incomplete")
        if not self.receipt_artifact_id:
            raise ValueError("an available probe requires a receipt artifact")


@dataclass(frozen=True, slots=True)
class CatalogEntry:
    model_id: str
    status_reason: str
    specification: ToolFunctionSpec


_REQUIRED_CONFIGURED = frozenset(
    {
        "deepseek-v4-pro-text",
        "deepseek-responses",
        "leansearch",
        "jixia",
        "lean",
        "z3",
        "sympy",
        "exact-enumeration",
        "deepseek-prover",
    }
)


class ProductToolRegistry:
    """Register the complete B12c catalog against current-deployment probe evidence."""

    def __init__(self, catalog: ToolCatalogStore) -> None:
        self._catalog = catalog

    def register_required(
        self,
        bindings: Sequence[AdapterBinding],
        *,
        probes: Sequence[DeploymentProbe] = (),
        now: str,
    ) -> tuple[CatalogEntry, ...]:
        configured = {binding.tool_id: binding for binding in bindings}
        if set(configured) != _REQUIRED_CONFIGURED or len(configured) != len(bindings):
            raise ProductToolAdapterError(
                "configured bindings must cover each required B12c capability exactly once"
            )
        probes_by_key = {probe.key: probe for probe in probes}
        if len(probes_by_key) != len(probes):
            raise ProductToolAdapterError("deployment probe keys must be unique")
        entries: list[CatalogEntry] = []
        for binding in sorted(bindings, key=lambda item: item.key):
            probe = probes_by_key.pop(binding.key, None)
            availability = ToolAvailability.CONFIGURED_UNPROBED
            reason = "configured profile has no current-deployment probe receipt"
            if probe is not None:
                expected = (
                    binding.provider,
                    binding.build_version,
                    binding.profile_id,
                )
                if (probe.provider, probe.build_version, probe.profile_id) != expected:
                    raise ProductToolAdapterError(
                        "probe does not bind the registered provider build"
                    )
                availability = ToolAvailability.AVAILABLE
                reason = f"current deployment probe: {probe.receipt_artifact_id}"
            spec = self._catalog.register(binding.specification(availability), now=now)
            if spec.availability != availability:
                spec = self._catalog.set_availability(spec.key, availability, now=now)
            entries.append(CatalogEntry(binding.model_id, reason, spec))
        if probes_by_key:
            raise ProductToolAdapterError("a probe refers to an unregistered B12c function")
        for entry in fixed_honesty_entries():
            registered = self._catalog.register(entry.specification, now=now)
            if registered.availability != entry.specification.availability:
                registered = self._catalog.set_availability(
                    registered.key, entry.specification.availability, now=now
                )
            entries.append(
                CatalogEntry(
                    entry.model_id,
                    entry.status_reason,
                    registered,
                )
            )
        return tuple(sorted(entries, key=lambda item: item.specification.key))


@dataclass(frozen=True, slots=True)
class CostObservation:
    amount: Decimal | None
    currency: str | None
    source_artifact_id: str | None
    unknown_reason: str | None

    def __post_init__(self) -> None:
        known = self.amount is not None
        if known:
            if (
                self.amount is None
                or self.amount < 0
                or not self.currency
                or not self.source_artifact_id
                or self.unknown_reason is not None
            ):
                raise ValueError("known cost requires amount, currency, and source artifact")
        elif (
            self.currency is not None
            or self.source_artifact_id is not None
            or not self.unknown_reason
        ):
            raise ValueError("unknown cost requires only an explicit reason")

    @classmethod
    def unknown(cls, reason: str) -> CostObservation:
        return cls(None, None, None, reason)

    def to_dict(self) -> dict[str, Any]:
        return {
            "amount": str(self.amount) if self.amount is not None else None,
            "currency": self.currency,
            "source_artifact_id": self.source_artifact_id,
            "unknown_reason": self.unknown_reason,
        }


@dataclass(frozen=True, slots=True)
class AdapterExecutionReceipt:
    binding: AdapterBinding
    invocation_status: str
    failure_code: str | None
    resource_usage: ResourceUsage
    token_usage: Mapping[str, int]
    cost: CostObservation
    accounting_artifact_id: str
    output_artifact_ids: tuple[str, ...]
    public_summary: str
    tool_receipt: ToolReceipt

    @property
    def accounting_record(self) -> Mapping[str, Any]:
        return MappingProxyType(
            {
                "provider": self.binding.provider,
                "model": self.binding.model_id,
                "build": self.binding.build_version,
                "profile": self.binding.profile_id,
                "function_schema_digest": self.binding.specification(
                    ToolAvailability.CONFIGURED_UNPROBED
                ).function_schema_digest,
                "resource_usage": self.resource_usage.to_dict(),
                "token_usage": dict(self.token_usage),
                "cost": self.cost.to_dict(),
                "authority_ceiling": str(self.binding.authority_ceiling),
                "invocation_status": self.invocation_status,
                "failure_code": self.failure_code,
            }
        )


class ProductAdapterBridge:
    """Translate one real mature-adapter call into a B12a public tool receipt."""

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
    ) -> AdapterExecutionReceipt:
        if not accounting_artifact_id or not public_summary:
            raise ValueError("tool receipt requires accounting artifact and public summary")
        outputs = tuple(output_artifact_ids)
        if not outputs or any(not value for value in outputs):
            raise ValueError("tool receipt requires explicit output artifacts")
        result = adapter.run(request)
        binding_matches = (
            result.get("adapter_name") == binding.profile_id
            and result.get("adapter_version") == binding.adapter_version
            and result.get("source_commit") == binding.build_version
        )
        completed = result.get("status") == "COMPLETED"
        exit_code = result.get("exit_code")
        exit_valid = exit_code is None or exit_code == 0
        succeeded = binding_matches and completed and exit_valid
        failure_code = None
        if not binding_matches:
            failure_code = "BINDING_MISMATCH"
        elif not completed:
            failure_code = str(result.get("status", "ADAPTER_FAILED"))
        elif not exit_valid:
            failure_code = "NONZERO_EXIT"
        usage = _token_usage(result.get("usage"))
        status = "SUCCEEDED" if succeeded else "FAILED"
        all_artifacts = (accounting_artifact_id, *outputs)
        receipt = ToolReceipt(
            tool_run_id=tool_run_id,
            attempt_id=attempt_id,
            status=status,
            payload={
                "exit_code": int(exit_code)
                if isinstance(exit_code, int)
                else (0 if succeeded else 1),
                "resource_usage": resource_usage.to_dict(),
                "public_log_artifact_id": accounting_artifact_id,
                "failure_code": failure_code,
                "public_summary": public_summary,
            },
            artifact_ids=all_artifacts,
        )
        return AdapterExecutionReceipt(
            binding,
            status,
            failure_code,
            resource_usage,
            usage,
            cost,
            accounting_artifact_id,
            outputs,
            public_summary,
            receipt,
        )


def fixed_honesty_entries() -> tuple[CatalogEntry, ...]:
    definitions = (
        (
            "gpt-5.6",
            "gpt-5.6",
            "research_text",
            "openai",
            ToolAvailability.UNAVAILABLE,
            "provider is not configured in the current deployment",
            AuthorityCeiling.SOFT_TOOL_RESULT,
        ),
        (
            "codex-text",
            "codex-0.147.0",
            "generate_text",
            "openai-codex",
            ToolAvailability.SMOKE_ONLY,
            "historical text-only smoke; no tool execution authority",
            AuthorityCeiling.SOFT_TOOL_RESULT,
        ),
        (
            "codex-shell",
            "codex-0.147.0",
            "shell_command",
            "openai-codex",
            ToolAvailability.UNAVAILABLE,
            "two calls produced no tool event or filesystem side effect",
            AuthorityCeiling.NO_FACT_GRAPH_WRITE,
        ),
        (
            "codex-apply-patch",
            "codex-0.147.0",
            "apply_patch",
            "openai-codex",
            ToolAvailability.UNAVAILABLE,
            "protocol call was emitted but executor failed",
            AuthorityCeiling.NO_FACT_GRAPH_WRITE,
        ),
        (
            "qed-nano",
            "lm-provers/QED-Nano",
            "generate_candidate",
            "local-huggingface",
            ToolAvailability.SMOKE_ONLY,
            "simple-problem smoke only; no research-grade receipt",
            AuthorityCeiling.SOFT_TOOL_RESULT,
        ),
        (
            "rethlas",
            "current-upstream",
            "verify_rethlas",
            "rethlas",
            ToolAvailability.EXTERNAL_BLOCKED,
            "current upstream returned HTTP 504",
            AuthorityCeiling.SOFT_TOOL_RESULT,
        ),
        (
            "archon",
            "configured-baseline",
            "run_research_harness",
            "archon",
            ToolAvailability.CONFIGURED_UNPROBED,
            "configured but no research-grade product receipt exists",
            AuthorityCeiling.NO_FACT_GRAPH_WRITE,
        ),
    )
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["input_artifact_id"],
        "properties": {"input_artifact_id": {"type": "string", "minLength": 1}},
    }
    entries: list[CatalogEntry] = []
    for tool_id, model, function, provider, status, reason, ceiling in definitions:
        binding = AdapterBinding(
            tool_id=tool_id,
            function_name=function,
            provider=provider,
            model_id=model,
            adapter_version="contract-boundary-v1",
            build_version="no-current-product-receipt",
            profile_id=f"{tool_id}-honesty-boundary",
            function_schema=schema,
            authority_ceiling=ceiling,
        )
        entries.append(CatalogEntry(model, reason, binding.specification(status)))
    return tuple(entries)


def _token_usage(value: Any) -> Mapping[str, int]:
    if value is None:
        return MappingProxyType({})
    if not isinstance(value, Mapping):
        raise ProductToolAdapterError("adapter usage is not an object")
    usage: dict[str, int] = {}
    for key, item in value.items():
        if (
            not isinstance(key, str)
            or isinstance(item, bool)
            or not isinstance(item, int)
            or item < 0
        ):
            raise ProductToolAdapterError("adapter token usage must be non-negative integers")
        usage[key] = item
    return MappingProxyType(usage)


__all__ = [
    "AdapterBinding",
    "AdapterExecutionReceipt",
    "CatalogEntry",
    "CostObservation",
    "DeploymentProbe",
    "ProductAdapterBridge",
    "ProductToolAdapterError",
    "ProductToolRegistry",
    "RunnableAdapter",
    "fixed_honesty_entries",
]
