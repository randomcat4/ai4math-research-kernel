from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from rk.extensions import ProductActivity
from rk.product.activity_store import ActivityStore, ActivityStoreError
from rk.product.operations import OperationConflict, OperationStore, ReceiptTransitionError
from rk.product_migrations import ProductMigrationAssembler, ProductMigrationRegistry


def migrated_db(tmp_path: Path) -> Path:
    db = tmp_path / "product.sqlite"
    connection = sqlite3.connect(db)
    ProductMigrationAssembler(ProductMigrationRegistry(Path("schema_fragments"))).apply(connection)
    connection.close()
    return db


def pending() -> dict[str, object]:
    return {
        "schema_version": "rk.product.receipt.v1",
        "request_id": "request-1",
        "scope": {"kind": "RUN", "run_id": "run-1"},
        "updated_at": "2026-08-13T00:00:00Z",
        "state": "PENDING",
        "job_id": "job-1",
    }


def test_same_request_digest_reuses_receipt_and_different_digest_conflicts(
    tmp_path: Path,
) -> None:
    store = OperationStore(migrated_db(tmp_path), iter(("receipt-1",)).__next__)
    request = {"request_id": "request-1", "command": {"type": "RUN_TOOL"}}
    digest = store.request_digest(request)
    first = store.reserve(
        scope_key="RUN:run-1",
        request_id="request-1",
        request_digest=digest,
        pending_receipt=pending(),
        now="2026-08-13T00:00:00Z",
    )
    second = store.reserve(
        scope_key="RUN:run-1",
        request_id="request-1",
        request_digest=digest,
        pending_receipt=pending(),
        now="2026-08-13T00:00:01Z",
    )

    assert first.created is True
    assert second.created is False
    assert second.receipt.receipt_id == first.receipt.receipt_id
    assert second.receipt.receipt_version == 1
    with pytest.raises(OperationConflict):
        store.reserve(
            scope_key="RUN:run-1",
            request_id="request-1",
            request_digest="0" * 64,
            pending_receipt=pending(),
            now="2026-08-13T00:00:02Z",
        )


def test_receipt_survives_restart_and_state_is_monotonic(tmp_path: Path) -> None:
    db = migrated_db(tmp_path)
    store = OperationStore(db, iter(("receipt-1",)).__next__)
    reservation = store.reserve(
        scope_key="GLOBAL:deployment-1",
        request_id="request-1",
        request_digest="1" * 64,
        pending_receipt=pending(),
        now="2026-08-13T00:00:00Z",
    )
    decided = store.decide(
        reservation.receipt.receipt_id,
        decision_receipt={**pending(), "state": "DECIDED", "decision": {"accepted": True}},
        now="2026-08-13T00:00:01Z",
    )

    restarted = OperationStore(db, lambda: "unused")
    loaded = restarted.get(decided.receipt_id)
    assert loaded.state == "DECIDED"
    assert loaded.receipt_version == 2
    with pytest.raises(ReceiptTransitionError):
        restarted.outcome_unknown(
            loaded.receipt_id,
            unknown_receipt={
                **pending(),
                "state": "OUTCOME_UNKNOWN",
                "unknown_external_call_ref": "call-1",
            },
            now="2026-08-13T00:00:02Z",
        )


def activity(event_id: str, source: str, *, kernel_event_id: str | None = None) -> ProductActivity:
    return ProductActivity(
        event_id=event_id,
        scope_kind="RUN",
        run_id="run-1",
        source=source,
        research_revision=3,
        kernel_event_id=kernel_event_id,
        entity_refs={"claim_id": "claim-1"},
        payload={"kind": source},
        recorded_at="2026-08-13T00:00:00Z",
    )


def test_kernel_and_host_activity_share_cursor_and_snapshot_fence(tmp_path: Path) -> None:
    db = migrated_db(tmp_path)
    store = ActivityStore(db)
    connection = sqlite3.connect(db)
    connection.execute("BEGIN IMMEDIATE")
    kernel_cursor = store.append_in_transaction(
        connection, activity("activity-1", "KERNEL", kernel_event_id="kernel-event-1")
    )
    connection.commit()
    connection.close()
    host_cursor = store.append(activity("activity-2", "HOST"))

    snapshot = store.snapshot(run_id="run-1")
    assert (kernel_cursor, host_cursor) == (1, 2)
    assert snapshot.last_cursor == 2
    assert [item.source for item in snapshot.records] == ["KERNEL", "HOST"]


def test_activity_event_identity_is_idempotent_but_not_mutable(tmp_path: Path) -> None:
    store = ActivityStore(migrated_db(tmp_path))
    original = activity("activity-1", "HOST")
    assert store.append(original) == store.append(original)
    with pytest.raises(ActivityStoreError):
        store.append(activity("activity-1", "WORKER"))
