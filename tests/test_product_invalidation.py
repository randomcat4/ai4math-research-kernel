from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path
from types import MappingProxyType

import pytest

from rk.extensions import AuthorityInvalidation, ExtensionConflict, ExtensionRegistry
from rk.product.invalidation import (
    AuthorityInvalidationEngine,
    AuthorityObjectInvalidated,
    AuthorityObjectKind,
    AuthorityProjectionLag,
    InvalidationConflict,
    register_invalidation_engine,
)
from rk.product_migrations import ProductMigrationAssembler, ProductMigrationRegistry

ROOT = Path(__file__).parents[1]
NOW = "2026-08-13T18:00:00Z"


def _database(tmp_path: Path) -> Path:
    db = tmp_path / "product.sqlite"
    with sqlite3.connect(db) as connection:
        ProductMigrationAssembler(
            ProductMigrationRegistry(ROOT / "schema_fragments")
        ).apply(connection)
    return db


def _engine(
    tmp_path: Path,
    fault_hook: Callable[[str, AuthorityInvalidation], None] | None = None,
) -> AuthorityInvalidationEngine:
    return AuthorityInvalidationEngine(
        _database(tmp_path),
        lambda: NOW,
        fault_hook=fault_hook,
    )


def _register(
    engine: AuthorityInvalidationEngine,
    kind: AuthorityObjectKind,
    object_id: str,
    *,
    digest_character: str,
) -> None:
    engine.register_binding(
        object_kind=kind,
        object_id=object_id,
        run_id="run-one",
        contract_version=2,
        bound_revision=4,
        stable_label=f"label-{object_id}",
        object_digest=digest_character * 64,
    )


def _invalidation(
    affected: tuple[tuple[AuthorityObjectKind, str, str], ...],
    *,
    event_id: str = "kernel-event-five",
    revision: int = 5,
    siblings: tuple[str, ...] = (),
) -> AuthorityInvalidation:
    return AuthorityInvalidation(
        event_id,
        "run-one",
        revision,
        MappingProxyType(
            {
                "schema_version": "rk.authority_invalidation.v1",
                "reason": "UPSTREAM_FACT_REVOKED",
                "affected_objects": [
                    {
                        "object_kind": kind.value,
                        "object_id": object_id,
                        "stable_label": f"label-{object_id}",
                        "object_digest": character * 64,
                    }
                    for kind, object_id, character in affected
                ],
                "preserved_sibling_ids": list(siblings),
                "reopened_obligation_ids": ["obligation-one"],
            }
        ),
    )


def test_commit_then_crash_returns_projection_lag_until_restart_catches_up(
    tmp_path: Path,
) -> None:
    class CrashAfterLedger(RuntimeError):
        pass

    def crash(phase: str, invalidation: AuthorityInvalidation) -> None:
        assert phase == "AFTER_LEDGER_COMMIT"
        assert invalidation.kernel_event_id == "kernel-event-five"
        raise CrashAfterLedger

    engine = _engine(tmp_path, crash)
    _register(engine, AuthorityObjectKind.REVIEW, "review-one", digest_character="a")
    _register(engine, AuthorityObjectKind.REVIEW, "review-sibling", digest_character="b")
    event = _invalidation(
        ((AuthorityObjectKind.REVIEW, "review-one", "a"),),
        siblings=("review-sibling",),
    )

    with pytest.raises(CrashAfterLedger):
        engine.consume(event)
    restarted = AuthorityInvalidationEngine(tmp_path / "product.sqlite", lambda: NOW)
    watermark = restarted.watermark("run-one")
    assert watermark.recorded_revision == 5
    assert watermark.processed_revision == 4
    with pytest.raises(AuthorityProjectionLag) as lag:
        restarted.assert_consumable(
            run_id="run-one",
            object_kind=AuthorityObjectKind.REVIEW,
            object_id="review-one",
            required_kernel_revision=5,
        )
    assert lag.value.code == "AUTHORITY_PROJECTION_LAG"

    result = restarted.catch_up("run-one")
    assert result.processed_event_ids == ("kernel-event-five",)
    assert result.watermark.caught_up
    with pytest.raises(AuthorityObjectInvalidated):
        restarted.assert_consumable(
            run_id="run-one",
            object_kind=AuthorityObjectKind.REVIEW,
            object_id="review-one",
            required_kernel_revision=5,
        )
    sibling = restarted.assert_consumable(
        run_id="run-one",
        object_kind=AuthorityObjectKind.REVIEW,
        object_id="review-sibling",
        required_kernel_revision=5,
    )
    assert sibling.state == "VALID"


def test_one_algorithm_materializes_every_authority_bound_object_kind(
    tmp_path: Path,
) -> None:
    engine = _engine(tmp_path)
    affected: list[tuple[AuthorityObjectKind, str, str]] = []
    for index, kind in enumerate(AuthorityObjectKind):
        object_id = f"object-{kind.value.lower()}"
        character = "abcdef0123456789"[index]
        _register(engine, kind, object_id, digest_character=character)
        affected.append((kind, object_id, character))

    event = _invalidation(tuple(affected))
    engine.record(event)
    result = engine.catch_up("run-one")

    assert result.watermark.caught_up
    assert all(
        engine.get_binding(kind, object_id).state == "INVALIDATED"
        for kind, object_id, _character in affected
    )


def test_record_and_materialization_are_idempotent_by_kernel_event(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    _register(engine, AuthorityObjectKind.WITNESS, "witness-one", digest_character="c")
    event = _invalidation(((AuthorityObjectKind.WITNESS, "witness-one", "c"),))

    first = engine.record(event)
    second = engine.record(event)
    engine.catch_up("run-one")
    repeated = engine.catch_up("run-one")

    assert first == second
    assert repeated.processed_event_ids == ()
    with sqlite3.connect(tmp_path / "product.sqlite") as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM product_authority_invalidation_ledger"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM product_authority_invalidation_materializations"
        ).fetchone() == (1,)


def test_event_reuse_with_changed_intent_is_rejected(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    _register(engine, AuthorityObjectKind.PUBLICATION, "paper-one", digest_character="d")
    _register(engine, AuthorityObjectKind.PUBLICATION, "paper-two", digest_character="e")
    engine.record(
        _invalidation(((AuthorityObjectKind.PUBLICATION, "paper-one", "d"),))
    )

    with pytest.raises(InvalidationConflict, match="event ID"):
        engine.record(
            _invalidation(((AuthorityObjectKind.PUBLICATION, "paper-two", "e"),))
        )


def test_same_stable_label_with_different_digest_is_rejected(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    engine.register_binding(
        object_kind=AuthorityObjectKind.COMPOSITION,
        object_id="composition-one",
        run_id="run-one",
        contract_version=2,
        bound_revision=4,
        stable_label="composition-root",
        object_digest="a" * 64,
    )

    with pytest.raises(InvalidationConflict, match="stable label"):
        engine.register_binding(
            object_kind=AuthorityObjectKind.COMPOSITION,
            object_id="composition-two",
            run_id="run-one",
            contract_version=2,
            bound_revision=4,
            stable_label="composition-root",
            object_digest="b" * 64,
        )


def test_s00_registers_exactly_one_invalidation_consumer(tmp_path: Path) -> None:
    engine = _engine(tmp_path)
    registry = register_invalidation_engine(ExtensionRegistry(), engine)
    assert "B11A_AUTHORITY_INVALIDATION" in registry.invalidation_consumers

    with pytest.raises(ExtensionConflict):
        register_invalidation_engine(registry, engine)
