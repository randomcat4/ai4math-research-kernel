from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from types import MappingProxyType

import pytest
from jsonschema import Draft202012Validator

from rk.domain import Decision, TypedCommand, VerifiedCapability, frozen_mapping
from rk.extensions import ExtensionRegistry, ProductCommandContext
from rk.guard import TransitionGuard
from rk.product.publication_handlers import (
    PublicationBindingError,
    PublicationHandlers,
    register_publication_handlers,
)
from rk.product_migrations import ProductMigrationAssembler, ProductMigrationRegistry
from rk.projector import ProjectionContext

ROOT = Path(__file__).parents[1]
NOW = "2026-08-14T12:00:00Z"
ROOT_SHA = "a" * 64
CLOSURE_SHA = "b" * 64
TEX_SHA = "c" * 64
REVIEW_SHA = "d" * 64
PDF_SHA = "e" * 64
TEX = {
    "artifact_id": "tex-artifact",
    "sha256": TEX_SHA,
    "byte_count": 120,
    "media_type": "application/x-tex",
}
SIGNED = {
    "artifact_id": "review-artifact",
    "sha256": REVIEW_SHA,
    "byte_count": 80,
    "media_type": "application/json",
}
PDF = {
    "artifact_id": "pdf-artifact",
    "sha256": PDF_SHA,
    "byte_count": 400,
    "media_type": "application/pdf",
}


def _database(tmp_path: Path) -> Path:
    path = tmp_path / "publication.sqlite"
    with sqlite3.connect(path) as connection:
        ProductMigrationAssembler(ProductMigrationRegistry(ROOT / "schema_fragments")).apply(
            connection
        )
    return path


def _capability(role: str, subject: str, action: str) -> VerifiedCapability:
    return VerifiedCapability(
        capability_id=f"cap-{subject}",
        subject_id=subject,
        issuer="product-authority",
        allowed_actions=frozenset({action}),
        run_scope=frozenset({"run-1"}),
        issued_at="2026-08-14T00:00:00Z",
        expires_at="2026-08-15T00:00:00Z",
        subject_role=role,
    )


def _root_snapshot(*, roots: int = 1, witness: bool = True):
    claims = [
        {
            "claim_id": f"root-{index}",
            "contract_version": 3,
            "claim_kind": "ROOT",
            "lifecycle_status": "ACTIVE",
            "route_result": "ROUTE_PROVED",
            "machine_verdict": "KERNEL_VERIFIED",
            "semantic_verdict": "TESTED",
            "peer_verdict": "UNREVIEWED",
            "closure_state": "CLOSED_MACHINE",
            "statement_hash": ROOT_SHA,
        }
        for index in range(1, roots + 1)
    ]
    witnesses = (
        [
            {
                "witness_id": "witness-1",
                "parent_claim_id": "root-1",
                "contract_version": 3,
                "selected_subgraph_digest": CLOSURE_SHA,
                "status": "ACCEPTED",
            }
        ]
        if witness
        else []
    )
    return MappingProxyType(
        {
            "claims": claims,
            "closure_witnesses": witnesses,
            "open_obligation_ids": [],
        }
    )


def _context(
    command_type: str,
    payload: dict[str, object],
    *,
    role: str,
    subject: str,
    revision: int,
    snapshot=None,
    artifacts: tuple[dict[str, object], ...] = (),
) -> ProductCommandContext:
    catalog = {
        str(item["artifact_id"]): {**item, "ingest_state": "COMMITTED"} for item in artifacts
    }
    return ProductCommandContext(
        run_id="run-1",
        revision=revision,
        contract_version=3,
        command=TypedCommand(command_type, frozen_mapping(payload)),
        capability=_capability(role, subject, command_type),
        snapshot=snapshot or MappingProxyType({}),
        evidence_summary=MappingProxyType({"committed_artifacts": catalog}),
    )


def _projection(
    command: TypedCommand,
    revision: int,
    command_id: str,
    event_id: str,
) -> ProjectionContext:
    return ProjectionContext(
        run_id="run-1",
        command_id=command_id,
        event_id=event_id,
        revision=revision,
        contract_version=3,
        command=command,
        capability_id="capability",
        recorded_at=NOW,
        artifacts_by_name=MappingProxyType({}),
        generated_artifact_ids=MappingProxyType({}),
    )


def _custom_mutation(decision: Decision):
    return next(
        item for item in decision.projection_mutations if str(item["op"]).startswith("B15_")
    )


def _apply(
    db: Path,
    registry: ExtensionRegistry,
    context: ProductCommandContext,
    decision: Decision,
    *,
    revision: int,
    command_id: str,
    event_id: str,
) -> None:
    with sqlite3.connect(db) as connection:
        connection.execute("BEGIN IMMEDIATE")
        registry.apply_projection_mutation(
            connection,
            _projection(context.command, revision, command_id, event_id),
            _custom_mutation(decision),
        )
        connection.commit()


def _finalize(
    db: Path,
    registry: ExtensionRegistry,
    handlers: PublicationHandlers,
) -> None:
    context = _context(
        "Finalize",
        {
            "outcome": "PROVED",
            "terminal_claim_ids": ["root-1"],
            "open_obligation_ids": [],
            "dossier_spec": {"format": "JSON"},
        },
        role="MAIN",
        subject="main",
        revision=10,
        snapshot=_root_snapshot(),
    )
    decision = handlers.finalize(context)
    assert decision.accepted
    _apply(
        db,
        registry,
        context,
        decision,
        revision=11,
        command_id="finalize-command",
        event_id="finalize-event",
    )


def _chain() -> dict[str, object]:
    return {
        "finalized_revision": 11,
        "terminal_root_id": "root-1",
        "terminal_root_digest": ROOT_SHA,
        "closure_witness_id": "witness-1",
        "dependency_closure_digest": CLOSURE_SHA,
        "candidate_tex_artifact": TEX,
    }


def test_unique_root_closure_finalize_and_publication_positive_chain(
    tmp_path: Path,
) -> None:
    db = _database(tmp_path)
    handlers = PublicationHandlers(db)
    registry = register_publication_handlers(ExtensionRegistry(), handlers)
    _finalize(db, registry, handlers)

    generate = _context(
        "GenerateCandidateTex",
        _chain(),
        role="PUBLICATION_WORKER",
        subject="publisher-a",
        revision=11,
        artifacts=(TEX,),
    )
    generated = handlers.generate_candidate_tex(generate)
    assert generated.accepted
    _apply(
        db,
        registry,
        generate,
        generated,
        revision=12,
        command_id="generation-command",
        event_id="generation-event",
    )

    review_payload = {
        **_chain(),
        "generation_command_id": "generation-command",
        "paper_review_id": "paper-review-1",
        "signed_review_artifact": SIGNED,
        "paper_review_schema_version": "rk.paper-review.v1",
        "reviewer_subject_id": "reviewer-a",
        "verdict": "ACCEPT",
    }
    review = _context(
        "SubmitPaperReview",
        review_payload,
        role="PAPER_REVIEWER",
        subject="reviewer-a",
        revision=12,
        artifacts=(SIGNED,),
    )
    reviewed = handlers.submit_paper_review(review)
    assert reviewed.accepted
    _apply(
        db,
        registry,
        review,
        reviewed,
        revision=13,
        command_id="review-command",
        event_id="review-event",
    )

    compile_payload = {
        **_chain(),
        "generation_command_id": "generation-command",
        "paper_review_id": "paper-review-1",
        "final_pdf_artifact": PDF,
        "compiler_profile_id": "tectonic",
        "compiler_profile_version": "1.0",
    }
    compile_context = _context(
        "CompileReviewedPaper",
        compile_payload,
        role="PUBLICATION_WORKER",
        subject="publisher-b",
        revision=13,
        artifacts=(PDF,),
    )
    compiled = handlers.compile_reviewed_paper(compile_context)
    assert compiled.accepted
    _apply(
        db,
        registry,
        compile_context,
        compiled,
        revision=14,
        command_id="compile-command",
        event_id="compile-event",
    )

    with sqlite3.connect(db) as connection:
        assert connection.execute(
            "SELECT finalized_revision,finalize_command_id,finalize_event_id "
            "FROM product_publication_finalizations"
        ).fetchone() == (11, "finalize-command", "finalize-event")
        assert connection.execute(
            "SELECT publication_revision,generation_command_id FROM product_publication_candidates"
        ).fetchone() == (12, "generation-command")
        assert connection.execute(
            "SELECT publication_revision,reviewer_subject_id,verdict "
            "FROM product_publication_reviews"
        ).fetchone() == (13, "reviewer-a", "ACCEPT")
        assert connection.execute(
            "SELECT publication_revision,paper_review_id,final_pdf_sha256 "
            "FROM product_publication_compilations"
        ).fetchone() == (14, "paper-review-1", PDF_SHA)


@pytest.mark.parametrize(
    ("snapshot", "terminal_ids"),
    [
        (_root_snapshot(roots=2), ["root-1"]),
        (_root_snapshot(witness=False), ["root-1"]),
        (_root_snapshot(), ["root-mismatch"]),
    ],
)
def test_finalize_rejects_nonunique_root_missing_witness_and_wrong_terminal(
    tmp_path: Path,
    snapshot,
    terminal_ids: list[str],
) -> None:
    handlers = PublicationHandlers(_database(tmp_path))
    context = _context(
        "Finalize",
        {
            "outcome": "PROVED",
            "terminal_claim_ids": terminal_ids,
            "open_obligation_ids": [],
            "dossier_spec": {"format": "JSON"},
        },
        role="MAIN",
        subject="main",
        revision=10,
        snapshot=snapshot,
    )

    decision = handlers.finalize(context)

    assert not decision.accepted
    assert decision.rejection_code == "TERMINAL_CLAIM_UNSUPPORTED"
    with sqlite3.connect(handlers.db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM product_publication_finalizations"
        ).fetchone() == (0,)


def test_wrong_role_revision_closure_and_tex_digest_all_reject_without_projection(
    tmp_path: Path,
) -> None:
    db = _database(tmp_path)
    handlers = PublicationHandlers(db)
    registry = register_publication_handlers(ExtensionRegistry(), handlers)
    _finalize(db, registry, handlers)
    variants = []
    wrong_revision = _chain()
    wrong_revision["finalized_revision"] = 12
    variants.append(("PUBLICATION_WORKER", "publisher", wrong_revision))
    wrong_closure = _chain()
    wrong_closure["dependency_closure_digest"] = "f" * 64
    variants.append(("PUBLICATION_WORKER", "publisher", wrong_closure))
    wrong_tex = _chain()
    wrong_tex["candidate_tex_artifact"] = {**TEX, "sha256": "f" * 64}
    variants.append(("PUBLICATION_WORKER", "publisher", wrong_tex))
    variants.append(("MAIN", "main", _chain()))

    for role, subject, payload in variants:
        context = _context(
            "GenerateCandidateTex",
            payload,
            role=role,
            subject=subject,
            revision=11,
            artifacts=(TEX,),
        )
        decision = handlers.generate_candidate_tex(context)
        assert not decision.accepted

    with sqlite3.connect(db) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM product_publication_candidates"
        ).fetchone() == (0,)


def test_review_subject_independence_and_rejected_review_block_compile(
    tmp_path: Path,
) -> None:
    db = _database(tmp_path)
    handlers = PublicationHandlers(db)
    registry = register_publication_handlers(ExtensionRegistry(), handlers)
    _finalize(db, registry, handlers)
    generate = _context(
        "GenerateCandidateTex",
        _chain(),
        role="PUBLICATION_WORKER",
        subject="same-subject",
        revision=11,
        artifacts=(TEX,),
    )
    decision = handlers.generate_candidate_tex(generate)
    _apply(
        db,
        registry,
        generate,
        decision,
        revision=12,
        command_id="generation-command",
        event_id="generation-event",
    )
    review_payload = {
        **_chain(),
        "generation_command_id": "generation-command",
        "paper_review_id": "paper-review-reject",
        "signed_review_artifact": SIGNED,
        "paper_review_schema_version": "rk.paper-review.v1",
        "reviewer_subject_id": "same-subject",
        "verdict": "ACCEPT",
    }
    not_independent = _context(
        "SubmitPaperReview",
        review_payload,
        role="PAPER_REVIEWER",
        subject="same-subject",
        revision=12,
        artifacts=(SIGNED,),
    )
    assert not handlers.submit_paper_review(not_independent).accepted

    review_payload["reviewer_subject_id"] = "reviewer"
    review_payload["verdict"] = "REJECT"
    rejected_context = _context(
        "SubmitPaperReview",
        review_payload,
        role="PAPER_REVIEWER",
        subject="reviewer",
        revision=12,
        artifacts=(SIGNED,),
    )
    rejected = handlers.submit_paper_review(rejected_context)
    assert rejected.accepted
    _apply(
        db,
        registry,
        rejected_context,
        rejected,
        revision=13,
        command_id="review-command",
        event_id="review-event",
    )
    compile_context = _context(
        "CompileReviewedPaper",
        {
            **_chain(),
            "generation_command_id": "generation-command",
            "paper_review_id": "paper-review-reject",
            "final_pdf_artifact": PDF,
            "compiler_profile_id": "tectonic",
            "compiler_profile_version": "1.0",
        },
        role="PUBLICATION_WORKER",
        subject="publisher",
        revision=13,
        artifacts=(PDF,),
    )
    assert not handlers.compile_reviewed_paper(compile_context).accepted


def test_closed_allowlist_is_exact_and_payload_schema_is_valid(tmp_path: Path) -> None:
    db = _database(tmp_path)
    registry = register_publication_handlers(ExtensionRegistry(), PublicationHandlers(db))
    guard = TransitionGuard(registry)
    snapshot = {
        "run_id": "run-1",
        "revision": 11,
        "current_contract_version": 3,
        "status": "CLOSED",
        "projection": {},
    }
    args = {
        "now_utc": datetime(2026, 8, 14, 12, tzinfo=UTC),
        "snapshot": snapshot,
        "evidence_summary": frozen_mapping({}),
        "policy_snapshot": frozen_mapping({}),
        "expected_revision": 11,
    }
    ordinary = guard.decide(
        **args,
        command=TypedCommand("StartRun", frozen_mapping({})),
        capability=_capability("MAIN", "main", "StartRun"),
    )
    assert ordinary.rejection_code == "RUN_CLOSED"
    wrong_role = guard.decide(
        **args,
        command=TypedCommand("GenerateCandidateTex", frozen_mapping(_chain())),
        capability=_capability("MAIN", "main", "GenerateCandidateTex"),
    )
    assert wrong_role.rejection_code == "RUN_CLOSED"
    assert registry.allows_closed_run_command("GenerateCandidateTex", "PUBLICATION_WORKER")
    assert registry.allows_closed_run_command("SubmitPaperReview", "PAPER_REVIEWER")
    assert not registry.allows_closed_run_command("SubmitPaperReview", "PUBLICATION_WORKER")

    schema = json.loads(
        (ROOT / "schema_fragments/B15a/publication.payload.schema.json").read_text(encoding="utf-8")
    )
    Draft202012Validator.check_schema(schema)


def test_projection_rejects_wrong_command_binding(tmp_path: Path) -> None:
    db = _database(tmp_path)
    handlers = PublicationHandlers(db)
    registry = register_publication_handlers(ExtensionRegistry(), handlers)
    _finalize(db, registry, handlers)
    generate = _context(
        "GenerateCandidateTex",
        _chain(),
        role="PUBLICATION_WORKER",
        subject="publisher",
        revision=11,
        artifacts=(TEX,),
    )
    mutation = _custom_mutation(handlers.generate_candidate_tex(generate))
    with (
        sqlite3.connect(db) as connection,
        pytest.raises(PublicationBindingError, match="command type"),
    ):
        registry.apply_projection_mutation(
            connection,
            _projection(
                TypedCommand("SubmitPaperReview", frozen_mapping({})),
                12,
                "generation-command",
                "generation-event",
            ),
            mutation,
        )
