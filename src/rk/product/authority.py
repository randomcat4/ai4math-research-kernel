"""Private adapter that exclusively owns product-to-kernel mathematical writes."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Protocol

from rk.domain import ApplyRequest, CreateRequest, VerifiedCapability
from rk.kernel import ResearchKernel
from rk.product.api import (
    GlobalScope,
    JsonObject,
    JsonValue,
    ProductCommand,
    ProductDecision,
    ProductSession,
    RunScope,
    frozen_json,
)


class CapabilitySource(Protocol):
    def resolve(
        self,
        session: ProductSession,
        *,
        action: str,
        run_id: str | None,
    ) -> VerifiedCapability: ...


PayloadBuilder = Callable[[JsonObject], Mapping[str, object]]


@dataclass(frozen=True, slots=True)
class KernelBinding:
    kernel_command_type: str
    payload_builder: PayloadBuilder


class ProductAuthority:
    """The only product object allowed to invoke ``ResearchKernel.create/apply``.

    Bindings are explicit.  A product command without a binding cannot be smuggled into
    the kernel by reusing its payload or command name.
    """

    def __init__(
        self,
        kernel: ResearchKernel,
        capabilities: CapabilitySource,
        bindings: Mapping[str, KernelBinding],
    ) -> None:
        self.__kernel = kernel
        self.__capabilities = capabilities
        self.__bindings = dict(bindings)

    def create(self, session: ProductSession, request: ProductCommand) -> str:
        if request.command_type != "CREATE_RESEARCH" or not isinstance(request.scope, GlobalScope):
            raise ValueError("create requires CREATE_RESEARCH with GLOBAL scope")
        capability = self.__capabilities.resolve(session, action="create", run_id=None)
        handle = self.__kernel.create(
            CreateRequest(request_id=request.request_id, contract=kernel_contract(request)),
            capability,
        )
        return handle.run_id

    def apply(self, session: ProductSession, request: ProductCommand) -> ProductDecision:
        if not isinstance(request.scope, RunScope):
            raise ValueError("kernel mathematical commands require RUN scope")
        try:
            binding = self.__bindings[request.command_type]
        except KeyError as error:
            raise ValueError(
                f"product command has no kernel binding: {request.command_type}"
            ) from error
        capability = self.__capabilities.resolve(
            session,
            action=binding.kernel_command_type,
            run_id=request.scope.run_id,
        )
        receipt = self.__kernel.apply(
            ApplyRequest.from_mapping(
                {
                    "request_id": request.request_id,
                    "run_id": request.scope.run_id,
                    "expected_revision": request.scope.expected_revision,
                    "command": {
                        "type": binding.kernel_command_type,
                        "payload": binding.payload_builder(request.payload),
                    },
                }
            ),
            capability,
        )
        return ProductDecision(
            accepted=receipt.accepted,
            revision_before=receipt.revision_before,
            revision_after=receipt.revision_after,
            contract_version=request.scope.expected_contract_version,
            event_cursor_after=receipt.revision_after,
            rejection_code=receipt.rejection_code,
            missing_conditions=tuple(
                frozen_json(
                    {
                        "code": item.code,
                        "path": item.path,
                        "params": _json_object(item.params),
                    }
                )
                for item in receipt.missing_conditions
            ),
            affected_entity_ids=(request.scope.run_id,),
            kernel_receipts=(frozen_json(_json_object(receipt.to_dict())),),
        )


def exact_payload(required: tuple[str, ...], optional: tuple[str, ...] = ()) -> PayloadBuilder:
    """Build a lossless kernel payload while rejecting undeclared product fields."""

    allowed = frozenset((*required, *optional))

    def build(payload: JsonObject) -> Mapping[str, object]:
        missing = [name for name in required if name not in payload]
        extra = sorted(set(payload) - allowed)
        if missing or extra:
            raise ValueError(f"kernel payload mismatch; missing={missing}, extra={extra}")
        return dict(payload)

    return build


def core_kernel_bindings() -> Mapping[str, KernelBinding]:
    """Bindings whose product and kernel payloads are already normatively identical."""

    return {
        "CONFIRM_CONTRACT": KernelBinding(
            "FreezeContract",
            exact_payload(("contract_version", "completeness_check_artifact_id")),
        ),
        "START_RESEARCH": KernelBinding(
            "StartRun",
            exact_payload(("contract_version", "literature_plan_artifact_id", "budget_policy")),
        ),
        "PAUSE_RESEARCH": KernelBinding(
            "Interrupt", exact_payload(("reason_code", "checkpoint_artifact_id"))
        ),
        "RESUME_RESEARCH": KernelBinding(
            "Resume",
            exact_payload(("checkpoint_artifact_id", "lease_preflight", "budget_preflight")),
        ),
        "FINALIZE_RESEARCH": KernelBinding(
            "Finalize",
            exact_payload(("outcome", "terminal_claim_ids", "open_obligation_ids", "dossier_spec")),
        ),
    }


def kernel_contract(request: ProductCommand) -> Mapping[str, object]:
    """Adapt the frozen product draft to the one canonical kernel contract shape."""

    draft = request.payload.get("contract_draft")
    if not isinstance(draft, dict):
        raise ValueError("contract draft must be an object")
    question = request.payload.get("question")
    if not isinstance(question, str) or not question.strip():
        raise ValueError("research question must be non-empty")
    materials = request.payload.get("material_artifacts", [])
    if not isinstance(materials, list):
        raise ValueError("material_artifacts must be a list")
    material_ids: list[str] = []
    for item in materials:
        if not isinstance(item, dict):
            raise ValueError("material_artifacts must contain exact ArtifactRef objects")
        artifact_id = item.get("artifact_id")
        if not isinstance(artifact_id, str) or not artifact_id:
            raise ValueError("material_artifacts must contain exact ArtifactRef objects")
        material_ids.append(artifact_id)
    success = draft.get("success_conditions", ["NATURAL_LANGUAGE_PROOF"])
    certificate_types = (
        success
        if isinstance(success, list) and success and all(isinstance(item, str) for item in success)
        else ["NATURAL_LANGUAGE_PROOF"]
    )
    return {
        "stable_project_id": f"PRODUCT_{request.request_id}",
        "statement": question,
        "source_refs": material_ids,
        "objects": draft.get("objects", []),
        "definitions": draft.get("definitions", []),
        "quantifiers": draft.get("quantifiers", []),
        "exact_negation": draft.get("exact_negation", ""),
        "allowed_dependencies": draft.get("allowed_dependencies", []),
        "forbidden_information": draft.get("forbidden_information", []),
        "boundary_rules": draft.get("boundary_rules", {}),
        "randomness_rules": draft.get("randomness_rules", {}),
        "tie_rules": draft.get("tie_rules", {}),
        "success_certificate_types": certificate_types,
        "non_claims": draft.get("non_claims", []),
        "literature_scope": draft.get(
            "literature_scope",
            {"families": ["exact", "equivalent", "stronger", "weaker", "counterexample"]},
        ),
        "literature_cutoff_date": draft.get("literature_cutoff_date", "9999-12-31"),
        "budget_policy": request.payload.get("initial_budget", {}),
        "stop_rules": draft.get("stop_rules", [{"kind": "manual"}]),
        "semantic_review_policy": draft.get("semantic_review_policy", {}),
        "amendment_policy": draft.get("amendment_policy", {}),
    }


def _json_object(value: Mapping[str, object]) -> dict[str, JsonValue]:
    result: dict[str, JsonValue] = {}
    for key, item in value.items():
        if item is None or isinstance(item, (str, int, bool, list, dict)):
            result[key] = item
        else:
            result[key] = str(item)
    return result


__all__ = [
    "CapabilitySource",
    "KernelBinding",
    "ProductAuthority",
    "core_kernel_bindings",
    "kernel_contract",
]
