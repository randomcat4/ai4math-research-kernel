"""Measured deployment health with explicit configuration and durable probe receipts."""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from functools import partial
from pathlib import Path
from types import MappingProxyType
from urllib.parse import urlsplit


class ProbeStatus(StrEnum):
    AVAILABLE = "AVAILABLE"
    DEGRADED = "DEGRADED"
    UNAVAILABLE = "UNAVAILABLE"
    UNCONFIGURED = "UNCONFIGURED"


class CapabilityKind(StrEnum):
    CPU = "CPU"
    RAM = "RAM"
    ROCM = "ROCM"
    GPU = "GPU"
    CAS = "CAS"
    SQLITE = "SQLITE"
    SERVICE_ENDPOINT = "SERVICE_ENDPOINT"
    TOOL_CATALOG = "TOOL_CATALOG"


@dataclass(frozen=True, slots=True)
class ServiceEndpoint:
    endpoint_id: str
    url: str
    expected_status: int = 200

    def __post_init__(self) -> None:
        parsed = urlsplit(self.url)
        if (
            not self.endpoint_id.strip()
            or parsed.scheme not in {"http", "https"}
            or not parsed.hostname
        ):
            raise ValueError("endpoint requires a stable ID and absolute HTTP(S) URL")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("endpoint credentials must not be embedded in probe configuration")
        if not 100 <= self.expected_status <= 599:
            raise ValueError("expected HTTP status is invalid")


@dataclass(frozen=True, slots=True)
class DeploymentProbeConfig:
    deployment_id: str
    db_path: Path
    cas_root: Path | None = None
    rocm_probe_argv: tuple[str, ...] | None = None
    gpu_probe_argv: tuple[str, ...] | None = None
    service_endpoints: tuple[ServiceEndpoint, ...] = ()
    required_tool_keys: tuple[tuple[str, str, str], ...] = ()
    timeout_seconds: float = 3.0
    probe_cost_microunits: Mapping[CapabilityKind, int] = MappingProxyType({})

    def __post_init__(self) -> None:
        if not self.deployment_id.strip() or self.timeout_seconds <= 0:
            raise ValueError("deployment ID and timeout must be valid")
        endpoint_ids = [item.endpoint_id for item in self.service_endpoints]
        if len(endpoint_ids) != len(set(endpoint_ids)):
            raise ValueError("endpoint IDs must be unique")
        if len(self.required_tool_keys) != len(set(self.required_tool_keys)):
            raise ValueError("required tool keys must be unique")
        if any(value < 0 for value in self.probe_cost_microunits.values()):
            raise ValueError("probe costs cannot be negative")


@dataclass(frozen=True, slots=True)
class ProbeResult:
    capability_key: str
    kind: CapabilityKind
    status: ProbeStatus
    latency_ms: int
    cost_microunits: int
    fault_code: str | None
    public_details: Mapping[str, str | int | bool]


@dataclass(frozen=True, slots=True)
class DeploymentHealthReport:
    probe_run_id: str
    deployment_id: str
    started_at: str
    finished_at: str
    status: ProbeStatus
    total_cost_microunits: int
    results: tuple[ProbeResult, ...]


class DeploymentHealthService:
    """Run configured probes. A missing configuration is never reported as healthy."""

    def __init__(self, config: DeploymentProbeConfig, clock: Callable[[], str]) -> None:
        self._config = config
        self._clock = clock

    def probe(self) -> DeploymentHealthReport:
        started = self._clock()
        results = [self._timed("cpu", CapabilityKind.CPU, self._probe_cpu)]
        results.append(self._timed("ram", CapabilityKind.RAM, self._probe_ram))
        results.append(
            self._command_probe("rocm", CapabilityKind.ROCM, self._config.rocm_probe_argv)
        )
        results.append(self._command_probe("gpu", CapabilityKind.GPU, self._config.gpu_probe_argv))
        results.append(self._timed("cas", CapabilityKind.CAS, self._probe_cas))
        results.append(self._timed("sqlite", CapabilityKind.SQLITE, self._probe_sqlite))
        for endpoint in self._config.service_endpoints:
            results.append(
                self._timed(
                    f"endpoint:{endpoint.endpoint_id}",
                    CapabilityKind.SERVICE_ENDPOINT,
                    partial(self._probe_endpoint, endpoint),
                )
            )
        if not self._config.service_endpoints:
            results.append(self._unconfigured("endpoints", CapabilityKind.SERVICE_ENDPOINT))
        results.append(self._timed("tool_catalog", CapabilityKind.TOOL_CATALOG, self._probe_tools))
        status = _aggregate(tuple(item.status for item in results))
        report = DeploymentHealthReport(
            str(uuid.uuid4()),
            self._config.deployment_id,
            started,
            self._clock(),
            status,
            sum(item.cost_microunits for item in results),
            tuple(results),
        )
        self._persist(report)
        return report

    def latest(self) -> DeploymentHealthReport | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT probe_run_id,deployment_id,started_at,finished_at,status,"
                "total_cost_microunits "
                "FROM product_deployment_probe_runs WHERE deployment_id=? "
                "ORDER BY rowid DESC LIMIT 1",
                (self._config.deployment_id,),
            ).fetchone()
            if row is None:
                return None
            results = connection.execute(
                "SELECT capability_key,kind,status,latency_ms,cost_microunits,"
                "fault_code,public_details_json "
                "FROM product_deployment_probe_results WHERE probe_run_id=? ORDER BY ordinal",
                (row[0],),
            ).fetchall()
        return DeploymentHealthReport(
            str(row[0]),
            str(row[1]),
            str(row[2]),
            str(row[3]),
            ProbeStatus(str(row[4])),
            int(row[5]),
            tuple(
                ProbeResult(
                    str(item[0]),
                    CapabilityKind(str(item[1])),
                    ProbeStatus(str(item[2])),
                    int(item[3]),
                    int(item[4]),
                    str(item[5]) if item[5] is not None else None,
                    MappingProxyType(json.loads(str(item[6]))),
                )
                for item in results
            ),
        )

    def _timed(
        self,
        key: str,
        kind: CapabilityKind,
        operation: Callable[[], tuple[ProbeStatus, str | None, dict[str, str | int | bool]]],
    ) -> ProbeResult:
        before = time.monotonic_ns()
        status, fault, details = operation()
        latency = max(0, (time.monotonic_ns() - before) // 1_000_000)
        return ProbeResult(
            key, kind, status, latency, self._cost(kind), fault, MappingProxyType(details)
        )

    def _unconfigured(self, key: str, kind: CapabilityKind) -> ProbeResult:
        return ProbeResult(
            key, kind, ProbeStatus.UNCONFIGURED, 0, 0, "NOT_CONFIGURED", MappingProxyType({})
        )

    def _command_probe(
        self, key: str, kind: CapabilityKind, argv: tuple[str, ...] | None
    ) -> ProbeResult:
        if argv is None:
            return self._unconfigured(key, kind)
        if not argv or any(not part for part in argv):
            raise ValueError(f"{key} probe argv is invalid")

        def run() -> tuple[ProbeStatus, str | None, dict[str, str | int | bool]]:
            try:
                completed = subprocess.run(
                    argv,
                    capture_output=True,
                    check=False,
                    timeout=self._config.timeout_seconds,
                    shell=False,
                )
            except (OSError, subprocess.TimeoutExpired) as error:
                return ProbeStatus.UNAVAILABLE, type(error).__name__.upper(), {}
            if completed.returncode != 0:
                return ProbeStatus.UNAVAILABLE, "NONZERO_EXIT", {"exit_code": completed.returncode}
            return ProbeStatus.AVAILABLE, None, {"exit_code": 0}

        return self._timed(key, kind, run)

    def _probe_cpu(self) -> tuple[ProbeStatus, str | None, dict[str, str | int | bool]]:
        count = os.cpu_count()
        if count is None or count < 1:
            return ProbeStatus.UNAVAILABLE, "CPU_COUNT_UNAVAILABLE", {}
        return ProbeStatus.AVAILABLE, None, {"logical_cpu_count": count}

    def _probe_ram(self) -> tuple[ProbeStatus, str | None, dict[str, str | int | bool]]:
        try:
            total = int(os.sysconf("SC_PHYS_PAGES")) * int(os.sysconf("SC_PAGE_SIZE"))
            available = int(os.sysconf("SC_AVPHYS_PAGES")) * int(os.sysconf("SC_PAGE_SIZE"))
        except (OSError, ValueError):
            return ProbeStatus.UNAVAILABLE, "RAM_SYSCONF_UNAVAILABLE", {}
        if total <= 0 or available < 0:
            return ProbeStatus.UNAVAILABLE, "RAM_MEASUREMENT_INVALID", {}
        return ProbeStatus.AVAILABLE, None, {"total_bytes": total, "available_bytes": available}

    def _probe_cas(self) -> tuple[ProbeStatus, str | None, dict[str, str | int | bool]]:
        root = self._config.cas_root
        if root is None:
            return ProbeStatus.UNCONFIGURED, "NOT_CONFIGURED", {}
        if not root.is_dir():
            return ProbeStatus.UNAVAILABLE, "CAS_ROOT_UNAVAILABLE", {}
        path = root / f".rk-health-{uuid.uuid4().hex}"
        payload = b"rk-cas-health-v1"
        try:
            with path.open("xb") as writer:
                writer.write(payload)
                writer.flush()
                os.fsync(writer.fileno())
            if path.read_bytes() != payload:
                return ProbeStatus.UNAVAILABLE, "CAS_READ_MISMATCH", {}
        except OSError as error:
            return ProbeStatus.UNAVAILABLE, f"CAS_{type(error).__name__.upper()}", {}
        finally:
            path.unlink(missing_ok=True)
        return ProbeStatus.AVAILABLE, None, {"write_read_verified": True}

    def _probe_sqlite(self) -> tuple[ProbeStatus, str | None, dict[str, str | int | bool]]:
        try:
            with self._connect() as connection:
                row = connection.execute("PRAGMA quick_check").fetchone()
                version = connection.execute("SELECT sqlite_version()").fetchone()
        except sqlite3.Error as error:
            return ProbeStatus.UNAVAILABLE, f"SQLITE_{type(error).__name__.upper()}", {}
        if row != ("ok",) or version is None:
            return ProbeStatus.UNAVAILABLE, "SQLITE_QUICK_CHECK_FAILED", {}
        return ProbeStatus.AVAILABLE, None, {"quick_check": "ok", "sqlite_version": str(version[0])}

    def _probe_endpoint(
        self, endpoint: ServiceEndpoint
    ) -> tuple[ProbeStatus, str | None, dict[str, str | int | bool]]:
        request = urllib.request.Request(
            endpoint.url, method="GET", headers={"User-Agent": "rk-health/1"}
        )
        try:
            with urllib.request.urlopen(request, timeout=self._config.timeout_seconds) as response:
                status = int(response.status)
                response.read(1024)
        except urllib.error.HTTPError as error:
            status = int(error.code)
        except (urllib.error.URLError, TimeoutError, OSError) as error:
            return ProbeStatus.UNAVAILABLE, f"ENDPOINT_{type(error).__name__.upper()}", {}
        if status != endpoint.expected_status:
            return ProbeStatus.UNAVAILABLE, "UNEXPECTED_HTTP_STATUS", {"http_status": status}
        return ProbeStatus.AVAILABLE, None, {"http_status": status}

    def _probe_tools(self) -> tuple[ProbeStatus, str | None, dict[str, str | int | bool]]:
        keys = self._config.required_tool_keys
        if not keys:
            return ProbeStatus.UNCONFIGURED, "NOT_CONFIGURED", {}
        with self._connect() as connection:
            values = []
            for key in keys:
                row = connection.execute(
                    "SELECT availability FROM product_tool_catalog "
                    "WHERE tool_id=? AND tool_version=? AND function_name=?",
                    key,
                ).fetchone()
                values.append(str(row[0]) if row is not None else "MISSING")
        available = sum(value in {"AVAILABLE", "PRODUCT_RECEIPT_AVAILABLE"} for value in values)
        limited = sum(value == "SMOKE_ONLY" for value in values)
        details: dict[str, str | int | bool] = {
            "required_count": len(keys),
            "available_count": available,
            "limited_count": limited,
        }
        if available == len(keys):
            return ProbeStatus.AVAILABLE, None, details
        if available + limited == len(keys):
            return ProbeStatus.DEGRADED, "TOOL_CATALOG_LIMITED", details
        return ProbeStatus.UNAVAILABLE, "REQUIRED_TOOL_UNAVAILABLE", details

    def _cost(self, kind: CapabilityKind) -> int:
        return int(self._config.probe_cost_microunits.get(kind, 0))

    def _persist(self, report: DeploymentHealthReport) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO product_deployment_probe_runs("
                "probe_run_id,deployment_id,started_at,finished_at,status,"
                "total_cost_microunits,failure_count) VALUES(?,?,?,?,?,?,?)",
                (
                    report.probe_run_id,
                    report.deployment_id,
                    report.started_at,
                    report.finished_at,
                    report.status,
                    report.total_cost_microunits,
                    sum(item.status == ProbeStatus.UNAVAILABLE for item in report.results),
                ),
            )
            connection.executemany(
                "INSERT INTO product_deployment_probe_results("
                "probe_run_id,ordinal,capability_key,kind,status,latency_ms,"
                "cost_microunits,fault_code,public_details_json) VALUES(?,?,?,?,?,?,?,?,?)",
                [
                    (
                        report.probe_run_id,
                        ordinal,
                        item.capability_key,
                        item.kind,
                        item.status,
                        item.latency_ms,
                        item.cost_microunits,
                        item.fault_code,
                        json.dumps(
                            dict(item.public_details), sort_keys=True, separators=(",", ":")
                        ),
                    )
                    for ordinal, item in enumerate(report.results)
                ],
            )
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._config.db_path)
        connection.execute("PRAGMA foreign_keys = ON")
        return connection


def _aggregate(statuses: tuple[ProbeStatus, ...]) -> ProbeStatus:
    configured = tuple(status for status in statuses if status != ProbeStatus.UNCONFIGURED)
    if not configured:
        return ProbeStatus.UNCONFIGURED
    if any(status == ProbeStatus.UNAVAILABLE for status in configured):
        return ProbeStatus.UNAVAILABLE
    if any(status == ProbeStatus.DEGRADED for status in configured):
        return ProbeStatus.DEGRADED
    return ProbeStatus.AVAILABLE


__all__ = [
    "CapabilityKind",
    "DeploymentHealthReport",
    "DeploymentHealthService",
    "DeploymentProbeConfig",
    "ProbeResult",
    "ProbeStatus",
    "ServiceEndpoint",
]
