from __future__ import annotations

import sqlite3
from dataclasses import replace
from pathlib import Path

import pytest

from rk.product.invalidation import AuthorityInvalidationEngine, AuthorityObjectKind
from rk.product.revocation import (
    KernelReplacementReceipt,
    ReplacementObject,
    RevocationClosure,
    RevocationConflict,
    RevocationPreview,
    RevocationPreviewStale,
    RevocationService,
)
from rk.product_migrations import ProductMigrationAssembler, ProductMigrationRegistry

ROOT = Path(__file__).parents[1]
NOW = "2026-08-13T18:00:00Z"


class Ids:
    def __init__(self) -> None:
        self.value = 0

    def __call__(self) -> str:
        self.value += 1
        return f"preview-{self.value}"


class KernelAuthority:
    def __init__(self, closure: RevocationClosure) -> None:
        self.current = closure
        self.replacement: tuple[ReplacementObject, ...] = ()
        self.recomputations = 0

    def preview(self, run_id: str, target_fact_id: str) -> RevocationClosure:
        assert run_id == self.current.run_id
        assert target_fact_id == self.current.target_fact_id
        return self.current

    def recompute_in_transaction(
        self, connection: sqlite3.Connection, preview: RevocationPreview
    ) -> RevocationClosure:
        assert connection.in_transaction
        assert preview.closure.target_fact_id == self.current.target_fact_id
        self.recomputations += 1
        return self.current

    def validate_replacement_in_transaction(
        self,
        connection: sqlite3.Connection,
        receipt: KernelReplacementReceipt,
    ) -> tuple[ReplacementObject, ...]:
        assert connection.in_transaction
        assert receipt.authority_source == "RESEARCH_KERNEL"
        return self.replacement


def _closure() -> RevocationClosure:
    return RevocationClosure(
        run_id="run-one",
        research_revision=4,
        contract_version=2,
        target_fact_id="fact-target",
        target_fact_digest="a" * 64,
        affected_fact_ids=("fact-target", "fact-downstream"),
        preserved_sibling_ids=("fact-sibling",),
        reopened_obligation_ids=("obligation-one",),
    )


def _setup(
    tmp_path: Path,
) -> tuple[RevocationService, AuthorityInvalidationEngine, KernelAuthority]:
    db = tmp_path / "product.sqlite"
    with sqlite3.connect(db) as connection:
        ProductMigrationAssembler(
            ProductMigrationRegistry(ROOT / "schema_fragments")
        ).apply(connection)
    invalidations = AuthorityInvalidationEngine(db, lambda: NOW)
    authority = KernelAuthority(_closure())
    service = RevocationService(db, authority, invalidations, Ids(), lambda: NOW)
    objects = (
        (AuthorityObjectKind.REVIEW, "review-target", "fact-target", "b"),
        (
            AuthorityObjectKind.COMPOSITION,
            "composition-downstream",
            "fact-downstream",
            "c",
        ),
        (AuthorityObjectKind.PUBLICATION, "paper-sibling", "fact-sibling", "d"),
    )
    for kind, object_id, fact_id, character in objects:
        invalidations.register_binding(
            object_kind=kind,
            object_id=object_id,
            run_id="run-one",
            contract_version=2,
            bound_revision=4,
            stable_label=f"label-{object_id}",
            object_digest=character * 64,
        )
        service.register_dependency(
            object_kind=kind,
            object_id=object_id,
            fact_id=fact_id,
            run_id="run-one",
        )
    return service, invalidations, authority


@pytest.mark.parametrize("change", ["NEW_DOWNSTREAM", "TARGET_REPLACED", "CONTRACT_CHANGED"])
def test_confirm_recomputes_in_kernel_transaction_and_marks_old_preview_stale(
    tmp_path: Path, change: str
) -> None:
    service, _invalidations, authority = _setup(tmp_path)
    preview = service.preview(run_id="run-one", target_fact_id="fact-target")
    if change == "NEW_DOWNSTREAM":
        authority.current = replace(
            authority.current,
            research_revision=5,
            affected_fact_ids=(
                "fact-target",
                "fact-downstream",
                "fact-new-downstream",
            ),
        )
    elif change == "TARGET_REPLACED":
        authority.current = replace(
            authority.current,
            research_revision=5,
            target_fact_digest="e" * 64,
        )
    else:
        authority.current = replace(
            authority.current,
            research_revision=5,
            contract_version=3,
        )

    with pytest.raises(RevocationPreviewStale) as stale:
        service.confirm(
            preview_id=preview.preview_id,
            preview_digest=preview.preview_digest,
            kernel_event_id="kernel-event-six",
            kernel_revision=6,
        )
    assert stale.value.code == "REVOCATION_PREVIEW_STALE"
    assert authority.recomputations == 1
    assert service.get_preview(preview.preview_id).state == "STALE"


def test_fresh_preview_revokes_full_recomputed_closure_and_preserves_sibling(
    tmp_path: Path,
) -> None:
    service, invalidations, authority = _setup(tmp_path)
    old = service.preview(run_id="run-one", target_fact_id="fact-target")
    authority.current = replace(authority.current, research_revision=5)
    with pytest.raises(RevocationPreviewStale):
        service.confirm(
            preview_id=old.preview_id,
            preview_digest=old.preview_digest,
            kernel_event_id="unused-event",
            kernel_revision=6,
        )
    fresh = service.preview(run_id="run-one", target_fact_id="fact-target")

    confirmed = service.confirm(
        preview_id=fresh.preview_id,
        preview_digest=fresh.preview_digest,
        kernel_event_id="kernel-event-six",
        kernel_revision=6,
    )
    invalidations.record(confirmed.invalidation)
    invalidations.catch_up("run-one")

    assert invalidations.get_binding(
        AuthorityObjectKind.REVIEW, "review-target"
    ).state == "INVALIDATED"
    assert invalidations.get_binding(
        AuthorityObjectKind.COMPOSITION, "composition-downstream"
    ).state == "INVALIDATED"
    assert invalidations.get_binding(
        AuthorityObjectKind.PUBLICATION, "paper-sibling"
    ).state == "VALID"
    assert confirmed.invalidation.intent["reopened_obligation_ids"] == [
        "obligation-one"
    ]


def test_confirm_accepts_only_preview_identity_and_digest(tmp_path: Path) -> None:
    service, _invalidations, _authority = _setup(tmp_path)
    preview = service.preview(run_id="run-one", target_fact_id="fact-target")

    with pytest.raises(RevocationPreviewStale):
        service.confirm(
            preview_id=preview.preview_id,
            preview_digest="f" * 64,
            kernel_event_id="kernel-event-five",
            kernel_revision=5,
        )
    assert service.get_preview(preview.preview_id).state == "ACTIVE"


def test_kernel_proved_replacement_restores_revoked_closure(tmp_path: Path) -> None:
    service, invalidations, authority = _setup(tmp_path)
    preview = service.preview(run_id="run-one", target_fact_id="fact-target")
    confirmed = service.confirm(
        preview_id=preview.preview_id,
        preview_digest=preview.preview_digest,
        kernel_event_id="kernel-event-five",
        kernel_revision=5,
    )
    invalidations.record(confirmed.invalidation)
    invalidations.catch_up("run-one")
    restored = (
        ReplacementObject(AuthorityObjectKind.REVIEW, "review-target"),
        ReplacementObject(
            AuthorityObjectKind.COMPOSITION, "composition-downstream"
        ),
    )
    authority.replacement = restored

    service.recover_with_replacement(
        KernelReplacementReceipt(
            authority_source="RESEARCH_KERNEL",
            command_type="PROVE_REPLACEMENT",
            run_id="run-one",
            revoked_target_fact_id="fact-target",
            replacement_fact_id="fact-replacement",
            replacement_fact_digest="e" * 64,
            restored_objects=restored,
            kernel_revision=6,
            kernel_receipt_id="kernel-receipt-six",
            kernel_event_id="kernel-event-six",
        )
    )

    assert service.effective_state(
        run_id="run-one",
        object_kind=AuthorityObjectKind.REVIEW,
        object_id="review-target",
    ) == "RESTORED"
    assert service.effective_state(
        run_id="run-one",
        object_kind=AuthorityObjectKind.COMPOSITION,
        object_id="composition-downstream",
    ) == "RESTORED"


def test_worker_or_tool_cannot_claim_replacement_recovery(tmp_path: Path) -> None:
    service, invalidations, authority = _setup(tmp_path)
    preview = service.preview(run_id="run-one", target_fact_id="fact-target")
    confirmed = service.confirm(
        preview_id=preview.preview_id,
        preview_digest=preview.preview_digest,
        kernel_event_id="kernel-event-five",
        kernel_revision=5,
    )
    invalidations.record(confirmed.invalidation)
    invalidations.catch_up("run-one")
    restored = (ReplacementObject(AuthorityObjectKind.REVIEW, "review-target"),)
    authority.replacement = restored

    with pytest.raises(RevocationConflict, match="authority-bearing kernel receipt"):
        service.recover_with_replacement(
            KernelReplacementReceipt(
                authority_source="TOOL_RUN",
                command_type="PROVE_REPLACEMENT",
                run_id="run-one",
                revoked_target_fact_id="fact-target",
                replacement_fact_id="fact-replacement",
                replacement_fact_digest="e" * 64,
                restored_objects=restored,
                kernel_revision=6,
                kernel_receipt_id="tool-receipt",
                kernel_event_id="tool-event",
            )
        )
