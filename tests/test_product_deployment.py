from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

from rk.extensions import ProductActivity
from rk.product.activity_store import ActivityStore
from rk.product.deployment import (
    CapabilityKind,
    DeploymentHealthReport,
    DeploymentHealthService,
    DeploymentProbeConfig,
    ProbeResult,
    ProbeStatus,
)
from rk.product.diagnostics import TypedDiagnosticService
from rk.product_migrations import ProductMigrationAssembler, ProductMigrationRegistry

ROOT = Path(__file__).parents[1]
NOW = "2026-08-14T12:00:00Z"


def _database(tmp_path: Path) -> Path:
    database = tmp_path / "product.sqlite"
    with sqlite3.connect(database) as connection:
        ProductMigrationAssembler(ProductMigrationRegistry(ROOT / "schema_fragments")).apply(
            connection
        )
    return database


def _result(report: DeploymentHealthReport, kind: CapabilityKind) -> ProbeResult:
    return next(item for item in report.results if item.kind == kind)


def test_real_server_probes_and_unconfigured_capabilities_are_distinct(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    cas = tmp_path / "cas"
    cas.mkdir()
    service = DeploymentHealthService(
        DeploymentProbeConfig(
            deployment_id="deployment-one",
            db_path=database,
            cas_root=cas,
            probe_cost_microunits={CapabilityKind.CPU: 7, CapabilityKind.CAS: 11},
        ),
        lambda: NOW,
    )

    report = service.probe()

    assert _result(report, CapabilityKind.CPU).status == ProbeStatus.AVAILABLE
    assert _result(report, CapabilityKind.RAM).status == ProbeStatus.AVAILABLE
    assert _result(report, CapabilityKind.CAS).public_details["write_read_verified"] is True
    assert _result(report, CapabilityKind.SQLITE).public_details["quick_check"] == "ok"
    assert _result(report, CapabilityKind.ROCM).status == ProbeStatus.UNCONFIGURED
    assert _result(report, CapabilityKind.GPU).status == ProbeStatus.UNCONFIGURED
    assert _result(report, CapabilityKind.SERVICE_ENDPOINT).status == ProbeStatus.UNCONFIGURED
    assert _result(report, CapabilityKind.TOOL_CATALOG).status == ProbeStatus.UNCONFIGURED
    assert report.total_cost_microunits == 18


def test_configured_probe_failure_recovery_and_restart_receipt(tmp_path: Path) -> None:
    database = _database(tmp_path)
    cas = tmp_path / "recovering-cas"
    config = DeploymentProbeConfig(
        deployment_id="deployment-one",
        db_path=database,
        cas_root=cas,
        rocm_probe_argv=(str(tmp_path / "missing-rocm-smi"),),
    )
    service = DeploymentHealthService(config, lambda: NOW)

    failed = service.probe()
    assert _result(failed, CapabilityKind.CAS).fault_code == "CAS_ROOT_UNAVAILABLE"
    assert _result(failed, CapabilityKind.ROCM).status == ProbeStatus.UNAVAILABLE
    assert failed.status == ProbeStatus.UNAVAILABLE

    cas.mkdir()
    recovered = service.probe()
    assert _result(recovered, CapabilityKind.CAS).status == ProbeStatus.AVAILABLE
    restarted = DeploymentHealthService(config, lambda: NOW)
    assert restarted.latest() == recovered


def test_configured_executable_probe_executes_without_shell(tmp_path: Path) -> None:
    database = _database(tmp_path)
    service = DeploymentHealthService(
        DeploymentProbeConfig(
            deployment_id="deployment-one",
            db_path=database,
            gpu_probe_argv=(sys.executable, "-c", "raise SystemExit(0)"),
        ),
        lambda: NOW,
    )

    report = service.probe()

    gpu = _result(report, CapabilityKind.GPU)
    assert gpu.status == ProbeStatus.AVAILABLE
    assert gpu.public_details == {"exit_code": 0}


def test_tool_catalog_smoke_only_is_degraded_not_available(tmp_path: Path) -> None:
    database = _database(tmp_path)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "INSERT INTO product_tool_catalog(tool_id,tool_version,function_name,provider,"
            "build_version,profile_id,function_schema_json,function_schema_digest,availability,"
            "authority_ceiling,registered_at,status_updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                "lean",
                "1",
                "check",
                "local",
                "build",
                "cpu",
                "{}",
                "a" * 64,
                "SMOKE_ONLY",
                "NO_FACT_GRAPH_WRITE",
                NOW,
                NOW,
            ),
        )
    service = DeploymentHealthService(
        DeploymentProbeConfig(
            deployment_id="deployment-one",
            db_path=database,
            required_tool_keys=(("lean", "1", "check"),),
        ),
        lambda: NOW,
    )

    tool = _result(service.probe(), CapabilityKind.TOOL_CATALOG)

    assert tool.status == ProbeStatus.DEGRADED
    assert tool.fault_code == "TOOL_CATALOG_LIMITED"


def test_diagnostics_use_public_activity_and_typed_projections_only(tmp_path: Path) -> None:
    database = _database(tmp_path)
    health = DeploymentHealthService(
        DeploymentProbeConfig(deployment_id="deployment-one", db_path=database),
        lambda: NOW,
    )
    health.probe()
    ActivityStore(database).append(
        ProductActivity(
            event_id="activity-one",
            scope_kind="DEPLOYMENT",
            deployment_id="deployment-one",
            source="DEPLOYMENT_PROBE",
            recorded_at=NOW,
            entity_refs={},
            payload={
                "event_type": "PROBE_COMPLETED",
                "public_summary": "CPU and SQLite measured",
                "secret": "never-return-this",
                "raw_reasoning": "never-return-this-either",
            },
        )
    )

    snapshot = TypedDiagnosticService(database, "deployment-one", health).snapshot()

    assert snapshot.activities[0].event_type == "PROBE_COMPLETED"
    assert snapshot.activities[0].public_summary == "CPU and SQLite measured"
    assert "never-return" not in repr(snapshot)
    assert snapshot.latest_health is not None
    assert all(item.projection in {"JOB", "REVIEW", "TOOL"} for item in snapshot.projection_counts)


def test_b16a_fragment_is_assembled_and_enforces_status_fault_consistency(
    tmp_path: Path,
) -> None:
    database = _database(tmp_path)
    with sqlite3.connect(database) as connection:
        fragment = connection.execute(
            "SELECT 1 FROM product_schema_fragments WHERE package='B16a' AND slug='deployment'"
        ).fetchone()
        assert fragment == (1,)
        connection.execute(
            "INSERT INTO product_deployment_probe_runs VALUES(?,?,?,?,?,?,?)",
            ("run", "deployment", NOW, NOW, "AVAILABLE", 0, 0),
        )
        try:
            connection.execute(
                "INSERT INTO product_deployment_probe_results VALUES(?,?,?,?,?,?,?,?,?)",
                ("run", 0, "cpu", "CPU", "AVAILABLE", 0, 0, "FALSE_GREEN", "{}"),
            )
        except sqlite3.IntegrityError:
            pass
        else:
            raise AssertionError("AVAILABLE probe with a fault must be rejected")
