from __future__ import annotations

import sqlite3
from pathlib import Path
from types import MappingProxyType

import pytest

from rk.domain import TypedCommand, VerifiedCapability, frozen_mapping
from rk.extensions import ExtensionConflict, ExtensionRegistry, ProductCommandContext
from rk.product.claim_handlers import ClaimHandlers, register_claim_handlers
from rk.product.claims import (
    ClaimArtifactBinding,
    ClaimConflict,
    ClaimKind,
    ClaimLifecycle,
    ClaimScopeError,
    ClaimStore,
    ClaimSubmission,
    KernelVerdictReceipt,
)
from rk.product.validation_gateway import (
    ValidationBackend,
    ValidationError,
    ValidationEvidence,
    ValidationGateway,
    ValidationVerdict,
)
from rk.product_migrations import ProductMigrationAssembler, ProductMigrationRegistry
from rk.projector import ProjectionContext

ROOT = Path(__file__).parents[1]
NOW = "2026-08-13T18:00:00Z"
SHA = "a" * 64


class Ids:
    def __init__(self) -> None:
        self.value = 0

    def __call__(self) -> str:
        self.value += 1
        return f"claim-{self.value}"


def _store(tmp_path: Path) -> tuple[ClaimStore, Path]:
    db = tmp_path / "product.sqlite"
    with sqlite3.connect(db) as connection:
        ProductMigrationAssembler(ProductMigrationRegistry(ROOT / "schema_fragments")).apply(
            connection
        )
    return ClaimStore(db, Ids(), lambda: NOW), db


def _artifact() -> ClaimArtifactBinding:
    return ClaimArtifactBinding("artifact-one", SHA, 12, "text/plain")


def _submission(
    statement: str,
    *,
    attempt: str,
    worker: str = "worker-run-one",
    predecessors: tuple[str, ...] = (),
    supersedes: str | None = None,
) -> ClaimSubmission:
    return ClaimSubmission(
        run_id="run-one",
        contract_version=2,
        kernel_revision=4,
        statement=statement,
        claim_kind=ClaimKind.LEMMA,
        proof_or_evidence_artifacts=(_artifact(),),
        predecessor_fact_ids=predecessors,
        source_binding_artifact=_artifact(),
        work_item_id="work-one",
        worker_run_id=worker,
        attempt_id=attempt,
        supersedes_claim_id=supersedes,
    )


def _kernel(
    claim_id: str,
    digest: str,
    *,
    accepted: bool,
    suffix: str,
    source: str = "RESEARCH_KERNEL",
    feedback: str | None = None,
) -> KernelVerdictReceipt:
    return KernelVerdictReceipt(
        authority_source=source,
        command_type="IMPORT_VERIFICATION",
        receipt_id=f"receipt-{suffix}",
        event_id=f"event-{suffix}",
        kernel_revision=5,
        claim_id=claim_id,
        statement_digest=digest,
        contract_version=2,
        validation_id=f"validation-{suffix}",
        accepted=accepted,
        promotion_eligible=accepted,
        repair_feedback=feedback,
    )


def test_one_attempt_submits_exactly_one_atomic_claim(tmp_path: Path) -> None:
    store, _db = _store(tmp_path)
    first = store.submit(
        _submission("Every even integer is divisible by two.", attempt="a1"), subject_id="worker-a"
    )
    repeated = store.submit(
        _submission("Every even integer is divisible by two.", attempt="a1"), subject_id="worker-a"
    )

    assert first.lifecycle is ClaimLifecycle.PENDING_VERIFICATION
    assert repeated.claim_id == first.claim_id
    with pytest.raises(ClaimConflict, match="one attempt"):
        store.submit(_submission("Changed statement.", attempt="a1"), subject_id="worker-a")


def test_only_research_kernel_receipt_can_promote(tmp_path: Path) -> None:
    store, _db = _store(tmp_path)
    claim = store.submit(_submission("Machine checked lemma.", attempt="a1"), subject_id="worker-a")

    with pytest.raises(ClaimScopeError):
        store.apply_kernel_verdict(
            _kernel(
                claim.claim_id,
                claim.statement_digest,
                accepted=True,
                suffix="tool",
                source="TOOL_RUN",
            )
        )
    assert store.get(claim.claim_id).lifecycle is ClaimLifecycle.PENDING_VERIFICATION

    accepted = store.apply_kernel_verdict(
        _kernel(claim.claim_id, claim.statement_digest, accepted=True, suffix="kernel")
    )
    assert accepted.lifecycle is ClaimLifecycle.ACCEPTED
    assert accepted.authority_class.value == "VERIFIED"


def test_verified_predecessors_form_only_necessary_subgraph(tmp_path: Path) -> None:
    store, _db = _store(tmp_path)
    fact = store.submit(_submission("Reusable parity fact.", attempt="a1"), subject_id="worker-a")
    store.apply_kernel_verdict(
        _kernel(fact.claim_id, fact.statement_digest, accepted=True, suffix="fact")
    )
    target = store.submit(
        _submission(
            "The target follows from parity.",
            attempt="a2",
            worker="worker-run-two",
            predecessors=(fact.claim_id,),
        ),
        subject_id="worker-b",
    )

    subgraph = store.necessary_subgraph(target.claim_id)
    assert [item.claim_id for item in subgraph.facts] == [fact.claim_id]
    assert len(subgraph.digest) == 64


def test_rejection_repair_and_other_worker_reuse_preserve_lineage(tmp_path: Path) -> None:
    store, _db = _store(tmp_path)
    rejected = store.submit(_submission("First flawed proof.", attempt="a1"), subject_id="worker-a")
    store.apply_kernel_verdict(
        _kernel(
            rejected.claim_id,
            rejected.statement_digest,
            accepted=False,
            suffix="reject",
            feedback="The induction step assumes the conclusion.",
        )
    )
    repaired = store.submit(
        _submission(
            "Repaired proof with an explicit induction step.",
            attempt="b1",
            worker="worker-run-two",
            supersedes=rejected.claim_id,
        ),
        subject_id="worker-b",
    )
    store.apply_kernel_verdict(
        _kernel(repaired.claim_id, repaired.statement_digest, accepted=True, suffix="repair")
    )

    hits = store.search_reusable(
        run_id="run-one",
        query="explicit induction",
        worker_subject_id="worker-c",
    )
    assert repaired.supersedes_claim_id == rejected.claim_id
    assert store.get(rejected.claim_id).superseded_by_claim_id == repaired.claim_id
    assert [item.claim_id for item in hits] == [repaired.claim_id]
    assert hits[0].reused_by_subject_id == "worker-c"


def test_gateway_routes_heterogeneous_verifiers_and_soft_never_promotes(tmp_path: Path) -> None:
    store, _db = _store(tmp_path)
    claim = store.submit(
        _submission("Verifier routing lemma.", attempt="a1"), subject_id="worker-a"
    )
    gateway = ValidationGateway()
    subgraph = store.necessary_subgraph(claim.claim_id)
    plan = gateway.plan(
        claim,
        selected_subgraph_digest=subgraph.digest,
        allowed_backends=("LEAN", "MANAGED_HUMAN", "SOFT_VERIFIER"),
    )
    assert plan.required_backends == (
        ValidationBackend.LEAN,
        ValidationBackend.MANAGED_HUMAN,
    )
    soft = ValidationEvidence(
        "validation-soft",
        claim.claim_id,
        claim.run_id,
        claim.contract_version,
        claim.statement_digest,
        subgraph.digest,
        ValidationBackend.SOFT_VERIFIER,
        ValidationVerdict.ACCEPTED,
        "soft-run-one",
        "NONE",
        True,
        True,
        False,
    )
    assert not gateway.evaluate(
        claim, soft, expected_subgraph_digest=subgraph.digest
    ).promotion_eligible


def test_incomplete_machine_or_human_binding_is_rejected(tmp_path: Path) -> None:
    store, _db = _store(tmp_path)
    claim = store.submit(
        _submission("Bound validation lemma.", attempt="a1"), subject_id="worker-a"
    )
    digest = store.necessary_subgraph(claim.claim_id).digest
    incomplete = ValidationEvidence(
        "validation-machine",
        claim.claim_id,
        claim.run_id,
        claim.contract_version,
        claim.statement_digest,
        digest,
        ValidationBackend.LEAN,
        ValidationVerdict.ACCEPTED,
        "lean-receipt",
        "MACHINE_CHECKED",
        True,
        False,
        False,
    )

    with pytest.raises(ValidationError, match="incomplete"):
        ValidationGateway().evaluate(claim, incomplete, expected_subgraph_digest=digest)


def _capability(role: str) -> VerifiedCapability:
    return VerifiedCapability(
        capability_id="cap-worker",
        subject_id="worker-a",
        issuer="issuer",
        allowed_actions=frozenset({"SUBMIT_CLAIM"}),
        run_scope=frozenset({"run-one"}),
        issued_at="2026-08-13T00:00:00Z",
        expires_at="2026-08-14T00:00:00Z",
        subject_role=role,
    )


def test_s00_handler_records_candidate_in_kernel_projection_transaction(tmp_path: Path) -> None:
    store, db = _store(tmp_path)
    handlers = ClaimHandlers(store, ValidationGateway())
    registry = register_claim_handlers(ExtensionRegistry(), handlers)
    artifact = {"artifact_id": "artifact-one", "sha256": SHA}
    context = ProductCommandContext(
        run_id="run-one",
        revision=4,
        contract_version=2,
        command=TypedCommand(
            "SUBMIT_CLAIM",
            frozen_mapping(
                {
                    "statement": "Atomic product claim.",
                    "claim_kind": "LEMMA",
                    "proof_or_evidence_artifacts": [artifact],
                    "predecessor_fact_ids": [],
                    "source_binding_artifact": artifact,
                    "work_item_id": "work-one",
                    "worker_run_id": "worker-run-one",
                    "attempt_id": "attempt-one",
                }
            ),
        ),
        capability=_capability("WORKER"),
        snapshot=MappingProxyType({"contract": {"status": "FROZEN"}}),
        evidence_summary=MappingProxyType(
            {
                "committed_artifacts": {
                    "artifact-one": {
                        "sha256": SHA,
                        "byte_count": 12,
                        "media_type": "text/plain",
                        "ingest_state": "COMMITTED",
                    }
                }
            }
        ),
    )
    decision = registry.handle_product_command(context)
    assert decision.accepted
    with sqlite3.connect(db) as connection:
        connection.execute("BEGIN IMMEDIATE")
        registry.apply_projection_mutation(
            connection,
            ProjectionContext(
                "run-one",
                "command-one",
                "event-one",
                5,
                2,
                context.command,
                "cap-worker",
                NOW,
                MappingProxyType({}),
                MappingProxyType({}),
            ),
            decision.projection_mutations[0],
        )
        connection.commit()
        assert connection.execute("SELECT lifecycle FROM product_claims").fetchone() == (
            "PENDING_VERIFICATION",
        )

    with pytest.raises(ExtensionConflict):
        register_claim_handlers(registry, handlers)
