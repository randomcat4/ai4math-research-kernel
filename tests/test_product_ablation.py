from __future__ import annotations

import hashlib
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from rk.product.ablation import (
    GROUPS,
    AblationStore,
    ConfigurationDrift,
    FrozenAblationConfig,
)
from rk.product.bridge_opportunities import (
    BridgeOpportunityError,
    BridgeOpportunityStore,
    OpportunityMetrics,
)

NOW = "2026-08-14T00:00:00Z"


def database(tmp_path: Path) -> Path:
    db = tmp_path / "ablation.sqlite"
    with sqlite3.connect(db, isolation_level=None) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript(
            "CREATE TABLE product_planned_routes(route_id TEXT PRIMARY KEY) STRICT;"
            "CREATE TABLE bridges(bridge_id TEXT PRIMARY KEY,run_id TEXT NOT NULL) STRICT;"
        )
        connection.executescript(
            Path("schema_fragments/B09c/ablation.sql").read_text(encoding="utf-8")
        )
        connection.execute("INSERT INTO product_planned_routes VALUES('route-far')")
        connection.execute("INSERT INTO bridges VALUES('bridge-from-kernel-handler','run-1')")
    return db


def metrics(*, mapping_loss: int = 120_000) -> OpportunityMetrics:
    return OpportunityMetrics(
        domain_distance=900_000,
        source_method_maturity=800_000,
        target_domain_absence=700_000,
        native_tool_advantage=600_000,
        expected_certificate_compression=500_000,
        mapping_loss=mapping_loss,
        assumption_loss=80_000,
        backtranslation_cost=200_000,
    )


def propose(store: BridgeOpportunityStore, opportunity_id: str) -> None:
    store.propose(
        opportunity_id=opportunity_id,
        run_id="run-1",
        route_id="route-far",
        source_problem={"statement": "forall finite G, P(G)", "normal_form": "graph-v1"},
        target_domain="matroid intersection",
        metrics=metrics(),
        mapping_definition={"vertices": "ground elements", "edges": "circuits"},
        assumption_audit={"preserved": ["finite"], "lost": [], "gained": ["rank oracle"]},
        backtranslation_plan={"dictionary": "artifact-dictionary", "source_verifier": "lean"},
        selection_reason="distant mature target tools with explicit reversible mapping",
        created_at=NOW,
    )


def record_required_death_tests(
    store: BridgeOpportunityStore, opportunity_id: str, *, failed: bool
) -> None:
    kinds = ("COUNTEREXAMPLE", "ROUNDTRIP", "ASSUMPTION_LOSS")
    for rank, kind in enumerate(kinds, start=1):
        is_failure = failed and kind == "COUNTEREXAMPLE"
        store.record_death_test(
            opportunity_id=opportunity_id,
            death_test_id=f"{opportunity_id}-death-{rank}",
            test_rank=rank,
            test_kind=kind,
            specification={"fastest_first_rank": rank, "target": kind.lower()},
            status="FAILED" if is_failure else "PASSED",
            receipt_artifact_id=f"receipt-{opportunity_id}-{rank}",
            elapsed_ms=rank * 10,
            cost_microunits=rank * 7,
            failure_code="COUNTEREXAMPLE_FOUND" if is_failure else None,
            recorded_at=NOW,
        )


def test_complete_opportunity_metrics_death_tests_rejections_and_existing_bridge_only(
    tmp_path: Path,
) -> None:
    store = BridgeOpportunityStore(database(tmp_path))
    propose(store, "opportunity-pass")
    opportunity = store.get("opportunity-pass")
    assert opportunity.ranking_score == sum(metrics().values()[:5]) - sum(metrics().values()[5:])
    with pytest.raises(BridgeOpportunityError, match="eligible"):
        store.bind_existing_bridge(
            "opportunity-pass", bridge_spec_id="bridge-from-kernel-handler", bound_at=NOW
        )
    record_required_death_tests(store, "opportunity-pass", failed=False)
    assert store.finalize("opportunity-pass", updated_at=NOW).state == "ELIGIBLE"
    with pytest.raises(BridgeOpportunityError, match="already exist"):
        store.bind_existing_bridge(
            "opportunity-pass", bridge_spec_id="selector-must-not-create-this", bound_at=NOW
        )
    registered = store.bind_existing_bridge(
        "opportunity-pass", bridge_spec_id="bridge-from-kernel-handler", bound_at=NOW
    )
    assert registered.state == "BRIDGE_REGISTERED"

    propose(store, "opportunity-rejected")
    record_required_death_tests(store, "opportunity-rejected", failed=True)
    rejected = store.finalize("opportunity-rejected", updated_at=NOW)
    assert rejected.state == "REJECTED"
    assert rejected.rejection_reason == "COUNTEREXAMPLE:COUNTEREXAMPLE_FOUND"
    with sqlite3.connect(tmp_path / "ablation.sqlite") as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM product_bridge_opportunities"
        ).fetchone() == (2,)
        assert connection.execute("SELECT COUNT(*) FROM product_bridge_death_tests").fetchone() == (
            6,
        )


def frozen_config() -> FrozenAblationConfig:
    return FrozenAblationConfig(
        problem_pool_digest=hashlib.sha256(b"same-problem-pool").hexdigest(),
        problem_ids=("problem-1", "problem-2"),
        model_identity={"provider": "deepseek", "model": "v4-pro", "build": "model-build"},
        tool_builds={"lean": "lean-build", "z3": "z3-build", "retriever": "index-build"},
        candidate_count=16,
        budget={"usd_microunits": 500_000, "gpu_ms": 60_000},
        verifier_identity={"tool": "lean", "build": "lean-build", "profile": "source-final"},
        verifier_profile_receipt_id="same-final-verifier-profile-receipt",
    )


def actual_group_receipt(group: str) -> str:
    completed = subprocess.run(
        (sys.executable, "-c", f"print('executed:{group}')"),
        capture_output=True,
        check=True,
    )
    return hashlib.sha256(completed.stdout + completed.stderr).hexdigest()


def test_five_real_groups_freeze_config_keep_denominators_and_do_not_prejudge_full_rk(
    tmp_path: Path,
) -> None:
    db = database(tmp_path)
    store = AblationStore(db)
    config = frozen_config()
    digest = store.freeze(
        ablation_plan_id="ablation-1", run_id="run-1", config=config, created_at=NOW
    )
    store.attach_opportunity(
        ablation_plan_id="ablation-1",
        group_name="far-random",
        problem_id="problem-1",
        opportunity_id=None,
        rejected_bridge_reason="death test rejected sampled bridge",
    )
    outcomes = {
        "direct": ("VERIFIED", "VERIFIED"),
        "near": ("VERIFIED", "REJECTED"),
        "far-random": (None, "INCONCLUSIVE"),
        "far-retrieval": ("EXECUTION_FAILED", "VERIFIED"),
        "full-RK": ("REJECTED", "INCONCLUSIVE"),
    }
    drift_checked = False
    verifier_drift_checked = False
    for group in GROUPS:
        if not drift_checked:
            with pytest.raises(ConfigurationDrift, match="configuration drifted"):
                store.start_group(
                    ablation_plan_id="ablation-1",
                    group_name=group,
                    frozen_digest="0" * 64,
                    run_receipt_artifact_id="must-not-start",
                    started_at=NOW,
                )
            drift_checked = True
        run_receipt = actual_group_receipt(group)
        store.start_group(
            ablation_plan_id="ablation-1",
            group_name=group,
            frozen_digest=digest,
            run_receipt_artifact_id=run_receipt,
            started_at=NOW,
        )
        with sqlite3.connect(db) as connection:
            assignments = connection.execute(
                "SELECT assignment_id,problem_id,state FROM product_ablation_assignments "
                "WHERE ablation_plan_id=? AND group_name=? ORDER BY problem_id",
                ("ablation-1", group),
            ).fetchall()
        for index, (assignment_id, _problem_id, state) in enumerate(assignments):
            outcome = outcomes[group][index]
            if state == "REJECTED_BRIDGE":
                assert outcome is None
                continue
            assert outcome is not None
            if not verifier_drift_checked:
                with pytest.raises(ConfigurationDrift, match="final verifier drifted"):
                    store.record_result(
                        assignment_id=str(assignment_id),
                        frozen_digest=digest,
                        outcome=outcome,
                        cost_microunits=100,
                        certificate_length=None,
                        verifier_profile_receipt_id="different-verifier",
                        verifier_receipt_artifact_id="unused",
                        execution_receipt_artifact_id="unused",
                        failure_code=None,
                        finished_at=NOW,
                    )
                verifier_drift_checked = True
            store.record_result(
                assignment_id=str(assignment_id),
                frozen_digest=digest,
                outcome=outcome,
                cost_microunits=100 + index,
                certificate_length=40 + index if outcome == "VERIFIED" else None,
                verifier_profile_receipt_id=config.verifier_profile_receipt_id,
                verifier_receipt_artifact_id=f"verifier-{group}-{index}",
                execution_receipt_artifact_id=f"execution-{group}-{index}",
                failure_code="PROCESS_EXIT_2" if outcome == "EXECUTION_FAILED" else None,
                finished_at=NOW,
            )
        store.complete_group(ablation_plan_id="ablation-1", group_name=group, completed_at=NOW)

    reports = {report.group_name: report for report in store.report("ablation-1")}
    assert set(reports) == set(GROUPS)
    assert all(report.denominator == 2 for report in reports.values())
    assert reports["direct"].verified == 2
    assert reports["far-random"].rejected_bridges == 1
    assert reports["far-retrieval"].execution_failed == 1
    assert reports["full-RK"].verified == 0
    assert reports["full-RK"].rejected == 1
    with sqlite3.connect(db) as connection:
        assert connection.execute(
            "SELECT COUNT(DISTINCT verifier_profile_receipt_id) FROM product_ablation_results"
        ).fetchone() == (1,)
        assert connection.execute(
            "SELECT COUNT(*) FROM product_ablation_groups WHERE run_receipt_artifact_id IS NOT NULL"
        ).fetchone() == (5,)
        assert connection.execute(
            "SELECT state FROM product_ablation_plans WHERE ablation_plan_id='ablation-1'"
        ).fetchone() == ("COMPLETED",)
