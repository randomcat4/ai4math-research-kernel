# ruff: noqa: E501
from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from rk.cas import ContentAddressedStore
from rk.product.artifact_read import ArtifactReadService, ExactArtifactRef
from rk.product.claims import ClaimLifecycle, ClaimStore, KernelVerdictReceipt
from rk.product.obligation_adapter import ObligationAdapterError, ResearchObligationAdapter
from rk.product.research_draft import ResearchDraftStore
from rk.product.validation_gateway import (
    ValidationBackend,
    ValidationEvidence,
    ValidationGateway,
    ValidationVerdict,
)
from rk.product.verifier_planner import (
    ResearchVerifierPlan,
    ResearchVerifierPlanner,
    VerifierPlannerError,
)
from rk.product_migrations import ProductMigrationAssembler, ProductMigrationRegistry

ROOT = Path(__file__).parents[1]
NOW = "2026-08-14T02:00:00Z"

INITIAL_DRAFT = """Defined-Symbols: H, Delta, V, Rd, L2, sigma_ess, z, R0, VR0, Phi

:::claim
Label: essential_gap_bad
Type: LEMMA
Statement: For H=-Delta+V on L2(Rd), compactness of V R0(z) alone yields a uniform positive gap below sigma_ess(H) for every admissible V.
Predecessors: -
Fact-Predecessors: -
Symbols: H, Delta, V, Rd, L2, sigma_ess, z, R0, VR0
Proof: Apply the resolvent identity and compactness of V R0(z); the submitted derivation then asserts uniform coercivity without controlling threshold resonances or the dependence on V.
:::end

:::claim
Label: fredholm_reduction
Type: ROOT
Statement: The spectral exclusion problem for H reduces to invertibility of the Birman-Schwinger family throughout the proposed uniform gap interval.
Predecessors: essential_gap_bad
Fact-Predecessors: -
Symbols: H, V, Phi
Proof: Factor the shifted operator through the free resolvent, identify the Birman-Schwinger family, and transfer invertibility back by the analytic Fredholm alternative with the domain fixed.
:::end
"""

REPAIR_DRAFT = """Defined-Symbols: H, Delta, V, Rd, L2, sigma_ess, z, R0, VR0, Phi, eta, K

:::claim
Label: essential_gap_repaired
Type: LEMMA
Statement: For H=-Delta+V on L2(Rd), compactness of V R0(z) and the quantitative bound sup norm(K(z)) <= 1-eta on the contour imply spectral exclusion inside that contour.
Predecessors: -
Fact-Predecessors: -
Symbols: H, Delta, V, Rd, L2, z, R0, VR0, eta, K
Proof: The quantitative norm bound makes I+K(z) invertible by the uniformly convergent Neumann series; the resolvent factorization then transfers invertibility to H-z on the fixed domain.
:::end

:::claim
Label: spectral_exclusion
Type: ROOT
Statement: Under the repaired Birman-Schwinger bound, H has no spectrum in the compact contour interior and the essential spectral threshold remains unchanged.
Predecessors: essential_gap_repaired
Fact-Predecessors: -
Symbols: H, sigma_ess, K, eta
Proof: Use the verified predecessor to obtain resolvent existence on the contour, apply analytic Fredholm continuation in the interior, and use relative compactness to preserve the essential spectrum.
:::end
"""


class ArtifactIds:
    def __init__(self) -> None:
        self.value = 0

    def new(self) -> str:
        self.value += 1
        return f"artifact-{self.value}"


class ClaimIds:
    def __init__(self) -> None:
        self.value = 0

    def __call__(self) -> str:
        self.value += 1
        return f"claim-{self.value}"


class Publisher:
    def __init__(self, root: Path) -> None:
        self.records: dict[str, dict[str, object]] = {}
        self.cas = ContentAddressedStore(
            root,
            max_bytes=10 * 1024 * 1024,
            inbox_roots=(),
            orphan_grace_seconds=60,
            id_generator=ArtifactIds(),
        )

    def publish(self, text: str, name: str) -> ExactArtifactRef:
        committed = self.cas.commit(
            self.cas.stage_bytes(text.encode(), media_type="text/markdown", source_name=name),
            now=datetime(2026, 8, 14, 2, tzinfo=UTC),
        )
        self.records[committed.artifact_id] = committed.to_record()
        return ExactArtifactRef(
            committed.artifact_id,
            committed.sha256,
            committed.byte_count,
            committed.media_type,
        )

    def get_artifact(self, artifact_id: str) -> dict[str, object] | None:
        return self.records.get(artifact_id)


def setup(
    tmp_path: Path,
) -> tuple[
    Path,
    Publisher,
    ResearchDraftStore,
    ClaimStore,
    ResearchVerifierPlanner,
    ResearchObligationAdapter,
]:
    db = tmp_path / "product.sqlite"
    import sqlite3

    with sqlite3.connect(db) as connection:
        ProductMigrationAssembler(ProductMigrationRegistry(ROOT / "schema_fragments")).apply(
            connection
        )
    publisher = Publisher(tmp_path / "cas")
    reader = ArtifactReadService(metadata=publisher, cas_root=tmp_path / "cas")
    drafts = ResearchDraftStore(db_path=db, artifacts=reader)
    claims = ClaimStore(db, ClaimIds(), lambda: NOW)
    planner = ResearchVerifierPlanner(
        db_path=db,
        drafts=drafts,
        claims=claims,
        gateway=ValidationGateway(),
    )
    adapter = ResearchObligationAdapter(db_path=db, claims=claims, planner=planner)
    return db, publisher, drafts, claims, planner, adapter


def evidence(
    plan: ResearchVerifierPlan,
    claims: ClaimStore,
    *,
    validation_id: str,
    backend: ValidationBackend,
    accepted: bool,
    feedback: str | None = None,
) -> ValidationEvidence:
    claim = claims.get(plan.claim_id)
    authority = (
        "HUMAN_ATTESTED" if backend is ValidationBackend.MANAGED_HUMAN else "MACHINE_CHECKED"
    )
    return ValidationEvidence(
        validation_id=validation_id,
        claim_id=claim.claim_id,
        run_id=claim.run_id,
        contract_version=claim.contract_version,
        statement_digest=claim.statement_digest,
        selected_subgraph_digest=plan.selected_subgraph_digest,
        backend=backend,
        verdict=(ValidationVerdict.ACCEPTED if accepted else ValidationVerdict.REJECTED),
        verifier_reference_id=f"{backend.value.lower()}-{validation_id}",
        authority_effect=authority,
        proof_checked=accepted,
        scope_checked=True,
        independence_verified=backend is ValidationBackend.MANAGED_HUMAN,
        repair_feedback=feedback,
    )


def receipt(
    claim_id: str,
    statement_digest: str,
    validation_id: str,
    *,
    accepted: bool,
    suffix: str,
    feedback: str | None = None,
    source: str = "RESEARCH_KERNEL",
    revision: int = 5,
) -> KernelVerdictReceipt:
    return KernelVerdictReceipt(
        authority_source=source,
        command_type="IMPORT_VERIFICATION",
        receipt_id=f"kernel-receipt-{suffix}",
        event_id=f"kernel-event-{suffix}",
        kernel_revision=revision,
        claim_id=claim_id,
        statement_digest=statement_digest,
        contract_version=2,
        validation_id=validation_id,
        accepted=accepted,
        promotion_eligible=accepted,
        repair_feedback=feedback,
    )


def accept_heterogeneous(
    planner: ResearchVerifierPlanner,
    claims: ClaimStore,
    plan_id: str,
    *,
    suffix: str,
) -> str:
    plan = planner.get_plan(plan_id)
    lean_id = f"validation-lean-{suffix}"
    human_id = f"validation-human-{suffix}"
    planner.record_evidence(
        plan_id=plan_id,
        evidence=evidence(
            plan,
            claims,
            validation_id=lean_id,
            backend=ValidationBackend.LEAN,
            accepted=True,
        ),
        now=NOW,
    )
    assert planner.get_plan(plan_id).status == "PARTIALLY_VERIFIED"
    planner.record_evidence(
        plan_id=plan_id,
        evidence=evidence(
            plan,
            claims,
            validation_id=human_id,
            backend=ValidationBackend.MANAGED_HUMAN,
            accepted=True,
        ),
        now=NOW,
    )
    assert planner.get_plan(plan_id).status == "READY_FOR_KERNEL"
    return human_id


def test_research_draft_rejection_repair_reuse_and_kernel_only_readiness(
    tmp_path: Path,
) -> None:
    _db, publisher, drafts, claims, planner, adapter = setup(tmp_path)
    initial_ref = publisher.publish(INITIAL_DRAFT, "initial-research-draft.md")
    initial = drafts.normalize(
        draft_id="draft-initial",
        run_id="run-one",
        contract_version=2,
        kernel_revision=4,
        source_artifact=initial_ref,
        now=NOW,
    )
    assert len(initial.candidates) == 2
    assert all(not item.undefined_symbols for item in initial.candidates)
    flawed, blocked_root = initial.candidates
    flawed_plan = planner.submit_and_plan(
        plan_id="plan-flawed",
        candidate_id=flawed.candidate_id,
        subject_id="worker-one",
        worker_run_id="worker-run-one",
        attempt_id="attempt-flawed",
        allowed_backends=("LEAN", "MANAGED_HUMAN", "SOFT_VERIFIER"),
        now=NOW,
    )
    assert flawed_plan.required_backends == (
        ValidationBackend.LEAN,
        ValidationBackend.MANAGED_HUMAN,
    )
    feedback = (
        "Compactness gives Fredholmness but no uniform gap; add a quantitative "
        "Birman-Schwinger norm bound excluding threshold resonances."
    )
    rejected_id = "validation-lean-rejected"
    rejected = planner.record_evidence(
        plan_id=flawed_plan.plan_id,
        evidence=evidence(
            flawed_plan,
            claims,
            validation_id=rejected_id,
            backend=ValidationBackend.LEAN,
            accepted=False,
            feedback=feedback,
        ),
        now=NOW,
    )
    assert not rejected.promotion_eligible
    before_kernel = adapter.readiness(initial.draft_id)
    assert before_kernel.status == "BLOCKED"
    assert before_kernel.discharged_count == 0
    with pytest.raises(ObligationAdapterError, match="ResearchKernel"):
        adapter.consume_kernel_verdict(
            plan_id=flawed_plan.plan_id,
            validation_id=rejected_id,
            receipt=receipt(
                flawed_plan.claim_id,
                claims.get(flawed_plan.claim_id).statement_digest,
                rejected_id,
                accepted=False,
                suffix="fake-tool",
                feedback=feedback,
                source="TOOL_RUN",
            ),
            now=NOW,
        )
    assert adapter.readiness(initial.draft_id) == before_kernel
    rejected_readiness = adapter.consume_kernel_verdict(
        plan_id=flawed_plan.plan_id,
        validation_id=rejected_id,
        receipt=receipt(
            flawed_plan.claim_id,
            claims.get(flawed_plan.claim_id).statement_digest,
            rejected_id,
            accepted=False,
            suffix="reject",
            feedback=feedback,
        ),
        now=NOW,
    )
    assert claims.get(flawed_plan.claim_id).lifecycle is ClaimLifecycle.REJECTED
    assert rejected_readiness.status == "BLOCKED"
    assert any("draft-obligation" in item for item in rejected_readiness.blocking_obligation_ids)
    with pytest.raises(VerifierPlannerError, match="not kernel-accepted"):
        planner.submit_and_plan(
            plan_id="plan-blocked-root",
            candidate_id=blocked_root.candidate_id,
            subject_id="worker-two",
            worker_run_id="worker-run-two",
            attempt_id="attempt-blocked-root",
            allowed_backends=("LEAN", "MANAGED_HUMAN"),
            now=NOW,
        )

    repair_ref = publisher.publish(REPAIR_DRAFT, "repaired-research-draft.md")
    repair = drafts.normalize(
        draft_id="draft-repair",
        run_id="run-one",
        contract_version=2,
        kernel_revision=5,
        source_artifact=repair_ref,
        now=NOW,
    )
    repaired_candidate, root_candidate = repair.candidates
    repaired_plan = planner.submit_and_plan(
        plan_id="plan-repaired",
        candidate_id=repaired_candidate.candidate_id,
        subject_id="worker-two",
        worker_run_id="worker-run-two",
        attempt_id="attempt-repaired",
        allowed_backends=("LEAN", "MANAGED_HUMAN"),
        supersedes_claim_id=flawed_plan.claim_id,
        now=NOW,
    )
    human_repair_id = accept_heterogeneous(planner, claims, repaired_plan.plan_id, suffix="repair")
    assert adapter.readiness(repair.draft_id).discharged_count == 0
    repaired_claim = claims.get(repaired_plan.claim_id)
    repair_readiness = adapter.consume_kernel_verdict(
        plan_id=repaired_plan.plan_id,
        validation_id=human_repair_id,
        receipt=receipt(
            repaired_claim.claim_id,
            repaired_claim.statement_digest,
            human_repair_id,
            accepted=True,
            suffix="repair",
            revision=6,
        ),
        now=NOW,
    )
    assert repair_readiness.status == "BLOCKED"
    assert repair_readiness.discharged_count == 1
    assert claims.get(flawed_plan.claim_id).superseded_by_claim_id == repaired_claim.claim_id
    hits = claims.search_reusable(
        run_id="run-one",
        query="compactness quantitative",
        worker_subject_id="worker-three",
    )
    assert [item.claim_id for item in hits] == [repaired_claim.claim_id]

    root_plan = planner.submit_and_plan(
        plan_id="plan-root",
        candidate_id=root_candidate.candidate_id,
        subject_id="worker-three",
        worker_run_id="worker-run-three",
        attempt_id="attempt-root",
        allowed_backends=("LEAN", "MANAGED_HUMAN"),
        now=NOW,
    )
    subgraph = claims.necessary_subgraph(root_plan.claim_id)
    assert [item.claim_id for item in subgraph.facts] == [repaired_claim.claim_id]
    human_root_id = accept_heterogeneous(planner, claims, root_plan.plan_id, suffix="root")
    assert adapter.readiness(repair.draft_id).status == "BLOCKED"
    root_claim = claims.get(root_plan.claim_id)
    ready = adapter.consume_kernel_verdict(
        plan_id=root_plan.plan_id,
        validation_id=human_root_id,
        receipt=receipt(
            root_claim.claim_id,
            root_claim.statement_digest,
            human_root_id,
            accepted=True,
            suffix="root",
            revision=7,
        ),
        now=NOW,
    )
    assert ready.status == "READY_FOR_CLOSURE_WITNESS"
    assert ready.discharged_count == ready.total_count == 2
    assert ready.blocking_obligation_ids == ()
    assert claims.get(root_claim.claim_id).lifecycle is ClaimLifecycle.ACCEPTED


def test_undefined_symbol_is_persisted_and_blocks_claim_submission(tmp_path: Path) -> None:
    _db, publisher, drafts, _claims, planner, _adapter = setup(tmp_path)
    source = INITIAL_DRAFT.replace(
        "\nSymbols: H, Delta, V, Rd, L2, sigma_ess, z, R0, VR0",
        "\nSymbols: H, Delta, V, Rd, L2, sigma_ess, z, R0, VR0, GhostThreshold",
        1,
    )
    artifact = publisher.publish(source, "undefined-symbol-draft.md")
    normalized = drafts.normalize(
        draft_id="draft-undefined",
        run_id="run-one",
        contract_version=2,
        kernel_revision=4,
        source_artifact=artifact,
        now=NOW,
    )
    candidate = normalized.candidates[0]
    assert candidate.undefined_symbols == ("GhostThreshold",)
    with pytest.raises(VerifierPlannerError, match="undefined symbols"):
        planner.submit_and_plan(
            plan_id="plan-undefined",
            candidate_id=candidate.candidate_id,
            subject_id="worker-one",
            worker_run_id="worker-run-one",
            attempt_id="attempt-undefined",
            allowed_backends=("LEAN", "MANAGED_HUMAN"),
            now=NOW,
        )
