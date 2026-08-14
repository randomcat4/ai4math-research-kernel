"""Concrete handler factory for the complete immediate product-command catalog.

p00 supplies the already constructed B-package services once; it never hand-writes
per-command closures. Mathematical mutations always cross ProductAuthority.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

from rk.product.api import ProductCommand, ProductDecision, RunScope, frozen_json
from rk.product.artifact_read import ArtifactReadService, ExactArtifactRef
from rk.product.attestation_import import ReviewAttestationImporter
from rk.product.authority import CapabilitySource, ProductAuthority
from rk.product.bridge_opportunities import BridgeOpportunityStore, OpportunityMetrics
from rk.product.contract_materials import ContractMaterialService
from rk.product.contracts import ContractStore
from rk.product.domain_commands import (
    CommandFenceSource,
    DomainCommandAuthority,
    DomainCommandHandler,
    DomainCommandRejected,
    DomainInvocation,
    bindings_from_handlers,
)
from rk.product.guidance import GuidanceStore
from rk.product.jobs import JobStore
from rk.product.materials import MaterialStore
from rk.product.problem_pool import ProblemPoolStore
from rk.product.reviews import ReviewArtifactRef, ReviewBinding, ReviewTaskStore, ReviewType
from rk.product.revocation import RevocationService
from rk.product.route_plan import RoutePlanStore
from rk.product.theorem_applicability import TheoremApplicabilityStore


class ArtifactPublisher(Protocol):
    def publish(self, *, data: bytes, logical_name: str, media_type: str) -> ExactArtifactRef: ...


@dataclass(frozen=True, slots=True)
class DomainHandlerServices:
    db_path: Path
    kernel: ProductAuthority
    artifacts: ArtifactReadService
    publisher: ArtifactPublisher
    routes: RoutePlanStore
    guidance: GuidanceStore
    contracts: ContractStore
    contract_materials: ContractMaterialService
    materials: MaterialStore
    review_tasks: ReviewTaskStore
    review_importer: ReviewAttestationImporter
    theorem_applicability: TheoremApplicabilityStore
    problem_pool: ProblemPoolStore
    bridge_opportunities: BridgeOpportunityStore
    revocations: RevocationService
    jobs: JobStore
    clock: Callable[[], str]
    ids: Callable[[], str]


def build_domain_command_authority(
    *,
    capabilities: CapabilitySource,
    fences: CommandFenceSource,
    services: DomainHandlerServices,
) -> DomainCommandAuthority:
    """Build all 23 bindings in one place; missing bindings fail at construction."""

    factory = _Handlers(services)
    names = {
        "CREATE_RESEARCH": factory.create_research,
        "CONFIRM_CONTRACT": factory.confirm_contract,
        "AMEND_CONTRACT": factory.amend_contract,
        "APPLY_ROUTE_PLAN": factory.apply_route_plan,
        "PAUSE_RESEARCH": factory.pause_research,
        "CANCEL_RESEARCH": factory.cancel_research,
        "SUBMIT_GUIDANCE": factory.submit_guidance,
        "WITHDRAW_GUIDANCE": factory.withdraw_guidance,
        "SUBMIT_CLAIM": factory.submit_claim,
        "IMPORT_VERIFICATION": factory.import_verification,
        "CONFIRM_REVOKE": factory.confirm_revoke,
        "REGISTER_BRIDGE_SPEC": factory.register_bridge_spec,
        "SUBMIT_CLOSURE_WITNESS": factory.submit_closure_witness,
        "CREATE_REVIEW_TASK": factory.create_review_task,
        "CLAIM_REVIEW_TASK": factory.claim_review_task,
        "SUBMIT_REVIEW": factory.submit_review,
        "SUBMIT_PAPER_REVIEW": factory.submit_paper_review,
        "FINALIZE_RESEARCH": factory.finalize_research,
        "CONFIRM_MATERIAL_EXTRACTION": factory.confirm_material_extraction,
        "REVIEW_THEOREM_APPLICABILITY": factory.review_theorem_applicability,
        "FREEZE_PROBLEM_POOL": factory.freeze_problem_pool,
        "REGISTER_BRIDGE_OPPORTUNITY": factory.register_bridge_opportunity,
        "CANCEL_JOB": factory.cancel_job,
    }
    handlers = cast(Mapping[str, DomainCommandHandler], names)
    return DomainCommandAuthority(
        capabilities=capabilities,
        fences=fences,
        bindings=bindings_from_handlers(handlers),
    )


class _Handlers:
    def __init__(self, services: DomainHandlerServices) -> None:
        self.s = services

    def create_research(self, i: DomainInvocation) -> ProductDecision:
        run_id = self.s.kernel.create(i.session, i.request)
        return ProductDecision(
            True,
            i.fence.revision,
            i.fence.revision,
            0,
            self._cursor(),
            affected_entity_ids=(run_id,),
            created_run_id=run_id,
        )

    def confirm_contract(self, i: DomainInvocation) -> ProductDecision:
        payload = i.request.payload
        contract = self.s.contracts.get(_contract_id(self.s.db_path, _run(i)))
        if contract.content_digest != _text(payload, "contract_digest"):
            raise DomainCommandRejected("CONTRACT_DIGEST_MISMATCH")
        anchors = _accepted_anchors(self.s.db_path, contract.contract_id, contract.version)
        if tuple(sorted(_strings(payload, "material_anchor_ids"))) != anchors:
            raise DomainCommandRejected("MATERIAL_ANCHOR_BINDING_MISMATCH")
        artifact = self.s.publisher.publish(
            data=_text(payload, "confirmation_note").encode(),
            logical_name=f"{i.request.request_id}.contract-confirmation.txt",
            media_type="text/plain",
        )
        self.s.contracts.confirm_by_user(
            contract.contract_id,
            confirmed_by=i.session.principal_subject_id,
            actor_kind="USER",
            now=self.s.clock(),
        )
        return self._kernel(
            i,
            "CONFIRM_CONTRACT",
            {
                "contract_version": contract.version,
                "completeness_check_artifact_id": artifact.artifact_id,
            },
        )

    def amend_contract(self, i: DomainInvocation) -> ProductDecision:
        payload = i.request.payload
        amendment = _json_artifact(self.s.artifacts, _object(payload, "amendment_artifact"))
        if _integer(payload, "base_contract_version") != i.fence.contract_version:
            raise DomainCommandRejected("CONTRACT_VERSION_MISMATCH")
        decision = self._kernel(
            i,
            "AMEND_CONTRACT",
            {
                "contract_version": i.fence.contract_version,
                "amendment_artifact_id": _text(
                    _object(payload, "amendment_artifact"), "artifact_id"
                ),
                "impact_acknowledgement": _text(payload, "impact_acknowledgement"),
            },
        )
        if decision.accepted:
            self.s.contract_materials.apply_revision(
                preview_id=_text(amendment, "preview_id"),
                preview_digest=_text(amendment, "preview_digest"),
                kernel_event_id=_event_id(decision),
                research_revision=decision.revision_after,
                now=self.s.clock(),
                actor_kind="USER",
                revised_by=i.session.principal_subject_id,
            )
        return decision

    def apply_route_plan(self, i: DomainInvocation) -> ProductDecision:
        p = i.request.payload
        result = self.s.routes.apply(
            run_id=_run(i),
            request_id=i.request.request_id,
            expected_revision=i.fence.revision,
            contract_version=i.fence.contract_version,
            action=_text(p, "action"),
            route_plan_id=_text(p, "route_plan_id"),
            plan_digest=_optional_text(p, "plan_digest"),
            reason=_optional_text(p, "reason"),
            priority=_optional_int(p, "priority"),
            budget=_optional_object(p, "budget"),
        )
        return self._domain(i, result.plan.route_plan_id)

    def pause_research(self, i: DomainInvocation) -> ProductDecision:
        p = i.request.payload
        artifact = self.s.publisher.publish(
            data=_text(p, "reason").encode(),
            logical_name=f"{i.request.request_id}.pause.txt",
            media_type="text/plain",
        )
        return self._kernel(
            i,
            "PAUSE_RESEARCH",
            {
                "reason_code": "USER_REQUEST",
                "checkpoint_artifact_id": artifact.artifact_id,
            },
        )

    def cancel_research(self, i: DomainInvocation) -> ProductDecision:
        return self._kernel(i, "CANCEL_RESEARCH", dict(i.request.payload))

    def submit_guidance(self, i: DomainInvocation) -> ProductDecision:
        p = i.request.payload
        routes = _strings(p, "affected_route_ids")
        if not routes:
            raise DomainCommandRejected("GUIDANCE_ROUTE_REQUIRED")
        artifact = self.s.publisher.publish(
            data=json.dumps(
                {"goal": _text(p, "goal"), "guidance_text": _text(p, "guidance_text")},
                sort_keys=True,
                separators=(",", ":"),
                ensure_ascii=False,
            ).encode(),
            logical_name=f"{i.request.request_id}.guidance.json",
            media_type="application/json",
        )
        ids = []
        for route_id in routes:
            guidance = self.s.guidance.submit(
                guidance_id=self.s.ids(),
                run_id=_run(i),
                research_revision=i.fence.revision,
                contract_version=i.fence.contract_version,
                checkpoint_id=_text(p, "target_checkpoint_id"),
                target_kind="ROUTE",
                target_id=route_id,
                route_id=route_id,
                kind="USER_GUIDANCE",
                content_artifact_id=artifact.artifact_id,
                submitted_by=i.session.principal_subject_id,
            )
            ids.append(guidance.guidance_id)
        return self._domain(i, *ids, artifacts=(artifact,))

    def withdraw_guidance(self, i: DomainInvocation) -> ProductDecision:
        item = self.s.guidance.cancel(
            _text(i.request.payload, "guidance_id"),
            actor_id=i.session.principal_subject_id,
        )
        return self._domain(i, item.guidance_id)

    def submit_claim(self, i: DomainInvocation) -> ProductDecision:
        return self._kernel(i, "SUBMIT_CLAIM", dict(i.request.payload))

    def import_verification(self, i: DomainInvocation) -> ProductDecision:
        return self._kernel(i, "IMPORT_VERIFICATION", dict(i.request.payload))

    def confirm_revoke(self, i: DomainInvocation) -> ProductDecision:
        p = i.request.payload
        preview = self.s.revocations.preview(run_id=_run(i), target_fact_id=_text(p, "fact_id"))
        closure = preview.closure
        if (
            closure.target_fact_digest != _text(p, "target_fact_digest")
            or closure.research_revision != _integer(p, "preview_revision")
            or closure.preserved_sibling_ids != _strings(p, "preserved_sibling_ids")
            or closure.reopened_obligation_ids != _strings(p, "reopened_obligation_ids")
        ):
            raise DomainCommandRejected("REVOCATION_PREVIEW_STALE")
        decision = self._kernel(
            i,
            "CONFIRM_REVOKE",
            {
                "fact_id": closure.target_fact_id,
                "target_fact_digest": closure.target_fact_digest,
                "affected_fact_ids": list(closure.affected_fact_ids),
                "reason_artifact": dict(_object(p, "reason_artifact")),
            },
        )
        if decision.accepted:
            self.s.revocations.confirm(
                preview_id=preview.preview_id,
                preview_digest=preview.preview_digest,
                kernel_event_id=_event_id(decision),
                kernel_revision=decision.revision_after,
            )
        return decision

    def register_bridge_spec(self, i: DomainInvocation) -> ProductDecision:
        return self._kernel(i, "REGISTER_BRIDGE_SPEC", dict(i.request.payload))

    def submit_closure_witness(self, i: DomainInvocation) -> ProductDecision:
        return self._kernel(i, "SUBMIT_CLOSURE_WITNESS", dict(i.request.payload))

    def create_review_task(self, i: DomainInvocation) -> ProductDecision:
        p = i.request.payload
        targets = _strings(p, "target_entity_ids")
        if len(targets) != 1:
            raise DomainCommandRejected("REVIEW_TARGET_CARDINALITY_INVALID")
        task = self.s.review_tasks.create(
            review_task_id=self.s.ids(),
            review_type=ReviewType(_text(p, "review_type")),
            binding=ReviewBinding(
                run_id=_run(i),
                kernel_revision=i.fence.revision,
                contract_version=i.fence.contract_version,
                target_id=targets[0],
                target_digest=_text(p, "target_digest"),
            ),
            author_subject_ids=_strings(p, "author_subject_ids"),
            assignee_identity_id=_identity(self.s.db_path, _text(p, "assignee_subject_id")),
            created_at=self.s.clock(),
            expires_at=_text(p, "expires_at"),
        )
        return self._domain(i, task.review_task_id)

    def claim_review_task(self, i: DomainInvocation) -> ProductDecision:
        task = self.s.review_tasks.claim(
            _text(i.request.payload, "review_task_id"),
            identity_id=_identity(self.s.db_path, i.session.principal_subject_id),
            now=self.s.clock(),
        )
        return self._domain(i, task.review_task_id)

    def submit_review(self, i: DomainInvocation) -> ProductDecision:
        p = i.request.payload
        ref = _review_ref(_object(p, "signed_review_artifact"))
        result = self.s.review_importer.import_artifact(
            review_task_id=_text(p, "review_task_id"),
            artifact_ref=ref,
            submitted_at=self.s.clock(),
        )
        return self._domain(i, result.task.review_task_id)

    def submit_paper_review(self, i: DomainInvocation) -> ProductDecision:
        payload = i.request.payload
        ref = _review_ref(_object(payload, "signed_paper_review_artifact"))
        imported = self.s.review_importer.import_artifact(
            review_task_id=_text(payload, "review_task_id"),
            artifact_ref=ref,
            submitted_at=self.s.clock(),
        )
        decision = self._kernel(i, "SUBMIT_PAPER_REVIEW", dict(payload))
        if not decision.accepted:
            raise RuntimeError(
                "signed paper review passed B05b but kernel rejected; "
                f"recovery required for {imported.task.review_task_id}"
            )
        return decision

    def finalize_research(self, i: DomainInvocation) -> ProductDecision:
        return self._kernel(i, "FINALIZE_RESEARCH", dict(i.request.payload))

    def confirm_material_extraction(self, i: DomainInvocation) -> ProductDecision:
        p = i.request.payload
        correction = _json_artifact(self.s.artifacts, _object(p, "corrections_artifact"))
        previous = self.s.materials.get_extraction(_text(p, "material_extraction_id"))
        revised = self.s.materials.revise(
            extraction_id=self.s.ids(),
            supersedes_extraction_id=previous.extraction_id,
            corrected_text=_text(correction, "corrected_text"),
            reason=_text(correction, "reason"),
            revised_by=i.session.principal_subject_id,
            now=self.s.clock(),
        )
        return self._domain(i, revised.extraction_id)

    def review_theorem_applicability(self, i: DomainInvocation) -> ProductDecision:
        p = i.request.payload
        audit = _json_artifact(self.s.artifacts, _object(p, "assumption_mapping_artifact"))
        item = self.s.theorem_applicability.review(
            applicability_id=self.s.ids(),
            context_id=_text(_object(p, "theorem_source_binding"), "context_id"),
            target_link_id=_text(p, "target_claim_id"),
            quantifiers=_object(audit, "quantifiers"),
            assumptions=_object(audit, "assumptions"),
            symbols=_object(audit, "symbols"),
            verdict=_text(p, "verdict"),
            reviewed_by=i.session.principal_subject_id,
            reviewed_at=self.s.clock(),
        )
        return self._domain(i, item.applicability_id)

    def freeze_problem_pool(self, i: DomainInvocation) -> ProductDecision:
        p = i.request.payload
        pool = self.s.problem_pool.freeze(
            _text(p, "pool_name"),
            frozen_by=i.session.principal_subject_id,
            actor_kind="USER",
            now=self.s.clock(),
        )
        if self.s.problem_pool.denominator(pool.problem_pool_id).total != _integer(
            p, "candidate_denominator"
        ):
            raise DomainCommandRejected("PROBLEM_POOL_DENOMINATOR_MISMATCH")
        return self._domain(i, pool.problem_pool_id)

    def register_bridge_opportunity(self, i: DomainInvocation) -> ProductDecision:
        p = i.request.payload
        raw = _object(p, "metrics")
        names = (
            "domain_distance",
            "source_method_maturity",
            "target_domain_absence",
            "native_tool_advantage",
            "expected_certificate_compression",
            "mapping_loss",
            "assumption_loss",
            "backtranslation_cost",
        )
        item = self.s.bridge_opportunities.propose(
            opportunity_id=self.s.ids(),
            run_id=_run(i),
            route_id=_optional_text(p, "route_id"),
            source_problem=_object(p, "source_problem"),
            target_domain=_text(p, "target_domain"),
            metrics=OpportunityMetrics(*(_integer(raw, name) for name in names)),
            mapping_definition=_object(p, "mapping_definition"),
            assumption_audit=_object(p, "assumption_audit"),
            backtranslation_plan=_object(p, "backtranslation_plan"),
            selection_reason=_text(p, "selection_reason"),
            created_at=self.s.clock(),
        )
        return self._domain(i, item.opportunity_id)

    def cancel_job(self, i: DomainInvocation) -> ProductDecision:
        job = self.s.jobs.request_cancel(_text(i.request.payload, "job_id"), now=self.s.clock())
        return self._domain(i, job.job_id)

    def _kernel(
        self, i: DomainInvocation, command_type: str, payload: Mapping[str, object]
    ) -> ProductDecision:
        request = ProductCommand(
            request_id=i.request.request_id,
            scope=i.request.scope,
            command_type=command_type,
            payload=frozen_json(cast(dict[str, Any], dict(payload))),
        )
        return self.s.kernel.apply(i.session, request)

    def _domain(
        self,
        i: DomainInvocation,
        *ids: str,
        artifacts: tuple[ExactArtifactRef, ...] = (),
    ) -> ProductDecision:
        refs = tuple(
            frozen_json(
                {
                    "artifact_id": ref.artifact_id,
                    "sha256": ref.sha256,
                    "byte_count": ref.byte_count,
                    "media_type": ref.media_type,
                }
            )
            for ref in artifacts
        )
        return ProductDecision(
            True,
            i.fence.revision,
            i.fence.revision,
            i.fence.contract_version,
            self._cursor(),
            affected_entity_ids=ids,
            created_artifact_refs=refs,
        )

    def _cursor(self) -> int:
        with sqlite3.connect(self.s.db_path) as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(cursor),0) FROM product_activity_events"
            ).fetchone()
        return int(row[0]) if row else 0


def _run(i: DomainInvocation) -> str:
    if not isinstance(i.request.scope, RunScope):
        raise ValueError("RUN scope required")
    return i.request.scope.run_id


def _contract_id(path: Path, run_id: str) -> str:
    with sqlite3.connect(path) as connection:
        rows = connection.execute(
            "SELECT contract_id FROM product_contracts WHERE run_id=?", (run_id,)
        ).fetchall()
    if len(rows) != 1:
        raise DomainCommandRejected("CONTRACT_BINDING_MISSING")
    return str(rows[0][0])


def _accepted_anchors(path: Path, contract_id: str, version: int) -> tuple[str, ...]:
    with sqlite3.connect(path) as connection:
        rows = connection.execute(
            "SELECT anchor_id FROM product_contract_material_references "
            "WHERE contract_id=? AND contract_version=? AND acceptance_state='USER_ACCEPTED' "
            "ORDER BY anchor_id",
            (contract_id, version),
        ).fetchall()
    return tuple(str(row[0]) for row in rows)


def _identity(path: Path, subject_id: str) -> str:
    with sqlite3.connect(path) as connection:
        rows = connection.execute(
            "SELECT identity_id FROM product_identities WHERE subject_id=? AND enabled=1",
            (subject_id,),
        ).fetchall()
    if len(rows) != 1:
        raise DomainCommandRejected("IDENTITY_BINDING_MISSING")
    return str(rows[0][0])


def _json_artifact(
    service: ArtifactReadService, binding: Mapping[str, object]
) -> dict[str, object]:
    ref = ExactArtifactRef(
        _text(binding, "artifact_id"),
        _text(binding, "sha256"),
        _integer(binding, "byte_count"),
        _text(binding, "media_type"),
    )
    value = json.loads(b"".join(service.open_range(ref.artifact_id, expected_ref=ref).stream))
    if not isinstance(value, dict):
        raise ValueError("artifact JSON must be an object")
    return cast(dict[str, object], value)


def _review_ref(value: Mapping[str, object]) -> ReviewArtifactRef:
    return ReviewArtifactRef(
        _text(value, "artifact_id"),
        _text(value, "sha256"),
        _integer(value, "byte_count"),
        _text(value, "media_type"),
    )


def _event_id(decision: ProductDecision) -> str:
    if not decision.kernel_receipts:
        raise RuntimeError("kernel receipt missing")
    events = decision.kernel_receipts[-1].get("event_ids")
    if not isinstance(events, list) or not events or not isinstance(events[-1], str):
        raise RuntimeError("kernel event identity missing")
    return events[-1]


def _text(value: Mapping[str, object], name: str) -> str:
    item = value.get(name)
    if not isinstance(item, str) or not item.strip():
        raise ValueError(f"{name} must be non-empty text")
    return item


def _optional_text(value: Mapping[str, object], name: str) -> str | None:
    return None if value.get(name) is None else _text(value, name)


def _integer(value: Mapping[str, object], name: str) -> int:
    item = value.get(name)
    if isinstance(item, bool) or not isinstance(item, int):
        raise ValueError(f"{name} must be an integer")
    return item


def _optional_int(value: Mapping[str, object], name: str) -> int | None:
    return None if value.get(name) is None else _integer(value, name)


def _object(value: Mapping[str, object], name: str) -> dict[str, object]:
    item = value.get(name)
    if not isinstance(item, dict):
        raise ValueError(f"{name} must be an object")
    return cast(dict[str, object], item)


def _optional_object(value: Mapping[str, object], name: str) -> dict[str, object] | None:
    return None if value.get(name) is None else _object(value, name)


def _strings(value: Mapping[str, object], name: str) -> tuple[str, ...]:
    item = value.get(name)
    if not isinstance(item, list) or any(not isinstance(part, str) for part in item):
        raise ValueError(f"{name} must be a string array")
    return tuple(cast(list[str], item))


__all__ = ["ArtifactPublisher", "DomainHandlerServices", "build_domain_command_authority"]
