"""Private adapter that exclusively owns product-to-kernel mathematical writes."""

from __future__ import annotations

import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from typing import Any, Protocol

from rk.domain import ApplyRequest, CreateRequest, RunSnapshot, VerifiedCapability
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
        product_capability = self.__capabilities.resolve(
            session, action=request.command_type, run_id=None
        )
        handle = self.__kernel.create(
            CreateRequest(request_id=request.request_id, contract=kernel_contract(request)),
            _kernel_capability(product_capability, "create"),
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
        product_capability = self.__capabilities.resolve(
            session,
            action=request.command_type,
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
            _kernel_capability(product_capability, binding.kernel_command_type),
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


def _kernel_capability(
    product_capability: VerifiedCapability, kernel_action: str
) -> VerifiedCapability:
    """Translate one authenticated product action at the sole kernel boundary."""

    return VerifiedCapability(
        capability_id=str(
            uuid.uuid5(
                uuid.NAMESPACE_URL,
                ":".join(
                    (
                        "rk-product-kernel-capability",
                        product_capability.capability_id,
                        kernel_action,
                        ",".join(sorted(product_capability.run_scope)),
                        product_capability.issued_at,
                        product_capability.expires_at,
                    )
                ),
            )
        ),
        subject_id=product_capability.subject_id,
        issuer=product_capability.issuer,
        allowed_actions=frozenset({kernel_action}),
        run_scope=product_capability.run_scope,
        issued_at=product_capability.issued_at,
        expires_at=product_capability.expires_at,
        subject_role=product_capability.subject_role,
    )


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


def product_kernel_bindings() -> Mapping[str, KernelBinding]:
    """All product commands which are allowed to cross the sole kernel write port."""

    bindings = dict(core_kernel_bindings())
    bindings.update(
        {
            "AMEND_CONTRACT": KernelBinding("AmendContract", _mapped_amendment),
            "CANCEL_RESEARCH": KernelBinding("Interrupt", _mapped_cancel),
            "SUBMIT_CLAIM": KernelBinding(
                "SUBMIT_CLAIM",
                exact_payload(
                    (
                        "statement",
                        "claim_kind",
                        "proof_or_evidence_artifacts",
                        "predecessor_fact_ids",
                        "source_binding_artifact",
                        "work_item_id",
                        "worker_run_id",
                        "attempt_id",
                    ),
                    ("route_id", "supersedes_claim_id", "public_summary"),
                ),
            ),
            "IMPORT_VERIFICATION": KernelBinding(
                "IMPORT_VERIFICATION",
                exact_payload(
                    (
                        "review_task_id",
                        "signed_review_artifact",
                        "target_digest",
                        "verifier_receipt_ids",
                    )
                ),
            ),
            "CONFIRM_REVOKE": KernelBinding("RevokeFact", _mapped_revoke),
            "REGISTER_BRIDGE_SPEC": KernelBinding("RegisterBridge", _bridge_payload),
            "SUBMIT_CLOSURE_WITNESS": KernelBinding("SubmitClosureWitness", _closure_payload),
            "SUBMIT_PAPER_REVIEW": KernelBinding("SubmitPaperReview", _paper_review_payload),
            "FINALIZE_RESEARCH": KernelBinding("Finalize", _finalize_payload),
        }
    )
    return bindings


def _mapped_amendment(payload: JsonObject) -> Mapping[str, object]:
    required = {
        "contract_version",
        "amendment_artifact_id",
        "impact_acknowledgement",
        "replacement_contract",
        "impact_analysis_artifact_id",
        "approvals",
    }
    if set(payload) != required:
        raise ValueError("AMEND_CONTRACT kernel payload fields are not exact")
    return {
        "base_version": payload["contract_version"],
        "patch_artifact_id": payload["amendment_artifact_id"],
        "impact_analysis_artifact_id": payload["impact_analysis_artifact_id"],
        "replacement_contract": payload["replacement_contract"],
        "approvals": payload["approvals"],
        "impact_acknowledgement": payload["impact_acknowledgement"],
    }


def _mapped_cancel(payload: JsonObject) -> Mapping[str, object]:
    if set(payload) != {"reason", "cancel_pending_external_attempts"}:
        raise ValueError("CANCEL_RESEARCH kernel payload fields are not exact")
    return {"reason_code": payload["reason"], "checkpoint_artifact_id": None}


def _mapped_revoke(payload: JsonObject) -> Mapping[str, object]:
    required = {"fact_id", "target_fact_digest", "affected_fact_ids", "reason_artifact"}
    if set(payload) != required or not isinstance(payload["reason_artifact"], dict):
        raise ValueError("CONFIRM_REVOKE kernel payload fields are not exact")
    reason = payload["reason_artifact"].get("artifact_id")
    if not isinstance(reason, str) or not reason:
        raise ValueError("CONFIRM_REVOKE requires a reason ArtifactRef")
    return {"fact_id": payload["fact_id"], "reason": reason}


def _bridge_payload(payload: JsonObject) -> Mapping[str, object]:
    required = {
        "source_claim_id",
        "target_claim_id",
        "directionality",
        "term_mapping",
        "loss_accounting",
        "bridge_spec",
        "forward_obligations",
        "reverse_obligations",
        "target_audit_review_id",
        "backtranslation_artifact_id",
    }
    if set(payload) != required:
        raise ValueError("REGISTER_BRIDGE_SPEC kernel payload fields are not exact")
    return dict(payload)


def _closure_payload(payload: JsonObject) -> Mapping[str, object]:
    required = {
        "selected_subgraph",
        "selected_subgraph_digest",
        "parent_claim_id",
        "contract_version",
        "edge_justifications",
        "bridge_dependency_ids",
        "discharged_obligation_ids",
        "open_obligation_ids",
        "verification_refs",
        "human_attestation_review_ids",
        "composition_mode",
    }
    if set(payload) != required:
        raise ValueError("SUBMIT_CLOSURE_WITNESS kernel payload fields are not exact")
    return dict(payload)


def _paper_review_payload(payload: JsonObject) -> Mapping[str, object]:
    required = {
        "finalized_revision",
        "terminal_root_id",
        "terminal_root_digest",
        "closure_witness_id",
        "dependency_closure_digest",
        "candidate_tex_artifact",
        "generation_command_id",
        "paper_review_id",
        "signed_review_artifact",
        "paper_review_schema_version",
        "reviewer_subject_id",
        "verdict",
    }
    if set(payload) != required:
        raise ValueError("SUBMIT_PAPER_REVIEW kernel payload fields are not exact")
    return dict(payload)


def _finalize_payload(payload: JsonObject) -> Mapping[str, object]:
    required = {"closure_witness_id", "final_outcome", "terminal_root_id"}
    if set(payload) != required:
        raise ValueError("FINALIZE_RESEARCH kernel payload fields are not exact")
    return {
        "outcome": payload["final_outcome"],
        "terminal_claim_ids": [payload["terminal_root_id"]],
        "open_obligation_ids": [],
        "dossier_spec": {
            "closure_witness_id": payload["closure_witness_id"],
            "include_raw_artifacts": False,
        },
    }


class ResearchKernelRevocationAuthority:
    """Use ResearchKernel's verified fact graph for every revocation closure."""

    def __init__(self, kernel: ResearchKernel) -> None:
        self._kernel = kernel

    def preview(self, run_id: str, target_fact_id: str) -> Any:
        from rk.product.revocation import RevocationClosure

        snapshot = self._snapshot(run_id, target_fact_id)
        graph = snapshot.projection.get("fact_graph")
        if not isinstance(graph, list) or not graph:
            raise ValueError("kernel returned no reverse closure")
        facts = tuple(item for item in graph if isinstance(item, Mapping))
        target = next((item for item in facts if item.get("fact_id") == target_fact_id), None)
        if target is None:
            raise ValueError("kernel reverse closure omitted its target")
        digest = target.get("statement_hash")
        metadata = snapshot.projection.get("revocation_metadata_by_fact", {})
        bound = metadata.get(target_fact_id) if isinstance(metadata, Mapping) else None
        if not isinstance(digest, str) or not isinstance(bound, Mapping):
            raise ValueError("kernel revocation metadata is incomplete")
        return RevocationClosure(
            run_id=run_id,
            research_revision=snapshot.revision,
            contract_version=snapshot.current_contract_version,
            target_fact_id=target_fact_id,
            target_fact_digest=digest,
            affected_fact_ids=tuple(sorted(str(item["fact_id"]) for item in facts)),
            preserved_sibling_ids=_string_tuple(bound.get("preserved_sibling_ids")),
            reopened_obligation_ids=_string_tuple(bound.get("reopened_obligation_ids")),
        )

    def recompute_in_transaction(self, connection: Any, preview: Any) -> Any:
        del connection
        return self.preview(preview.closure.run_id, preview.closure.target_fact_id)

    def validate_replacement_in_transaction(self, connection: Any, receipt: Any) -> tuple[Any, ...]:
        del connection
        snapshot = self._kernel.inspect(
            receipt.run_id,
            fact_query={
                "operation": "summary",
                "fact_ids": [receipt.replacement_fact_id],
            },
        )
        if not isinstance(snapshot, RunSnapshot):
            raise ValueError("kernel replacement inspection returned an event page")
        if snapshot.revision != receipt.kernel_revision:
            raise ValueError("replacement receipt is not at current kernel revision")
        return tuple(receipt.restored_objects)

    def _snapshot(self, run_id: str, target_fact_id: str) -> RunSnapshot:
        snapshot = self._kernel.inspect(
            run_id,
            fact_query={"operation": "reverse_closure", "fact_ids": [target_fact_id]},
        )
        if not isinstance(snapshot, RunSnapshot):
            raise ValueError("kernel revocation inspection returned an event page")
        return snapshot


def _string_tuple(value: object) -> tuple[str, ...]:
    if not isinstance(value, (list, tuple)) or any(
        not isinstance(item, str) or not item for item in value
    ):
        raise ValueError("kernel revocation metadata identities are invalid")
    return tuple(value)


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
    raw_objects = draft.get("objects", [])
    raw_quantifiers = draft.get("quantifiers", [])
    raw_boundaries = draft.get("boundary_conditions", [])
    raw_tools = draft.get("allowed_tools", [])
    if (
        not isinstance(raw_objects, list)
        or not raw_objects
        or any(not isinstance(item, str) or not item for item in raw_objects)
        or not isinstance(raw_quantifiers, list)
        or not raw_quantifiers
        or any(not isinstance(item, str) or not item for item in raw_quantifiers)
        or not isinstance(raw_boundaries, list)
        or any(not isinstance(item, str) or not item for item in raw_boundaries)
        or not isinstance(raw_tools, list)
        or any(not isinstance(item, str) or not item for item in raw_tools)
    ):
        raise ValueError("contract draft lists cannot be losslessly mapped to kernel objects")
    return {
        "stable_project_id": f"PRODUCT_{request.request_id}",
        "statement": question,
        "source_refs": material_ids,
        "objects": [{"name": item} for item in raw_objects],
        "definitions": draft.get("definitions", []),
        "quantifiers": [{"expression": item} for item in raw_quantifiers],
        "exact_negation": draft.get("exact_negation", ""),
        "allowed_dependencies": draft.get("allowed_dependencies", []),
        "forbidden_information": draft.get("forbidden_information", []),
        "boundary_rules": {"conditions": raw_boundaries},
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
        "semantic_review_policy": {"allowed_tools": raw_tools},
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
    "ResearchKernelRevocationAuthority",
    "core_kernel_bindings",
    "kernel_contract",
    "product_kernel_bindings",
]
