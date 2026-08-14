from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import sys
import urllib.request
import zipfile
from pathlib import Path

import pytest

from rk.http.app import BootstrapAdmin, bootstrap_admin_session
from rk.product.service_lifecycle import (
    ServiceLaunchConfig,
    ServiceLifecycle,
    ServiceLifecycleError,
    ServiceStatus,
)

ROOT = Path(__file__).parents[1]
NOW = "2026-08-14T15:00:00Z"
EXPIRES = "2026-08-15T15:00:00Z"

DAEMON_PROGRAM = """
import os
import signal
import threading
from rk.http.daemon_main import ProductHttpDaemon
from rk.http_shell import HttpResponse

class App:
    async def __call__(self, request):
        return HttpResponse(404, {"code": "ROUTE_NOT_FOUND"})

stopping = threading.Event()
signal.signal(signal.SIGTERM, lambda _signum, _frame: stopping.set())
daemon = ProductHttpDaemon(
    app=App(),
    deployment_id=os.environ["RK_DEPLOYMENT_ID"],
    host=os.environ["RK_LISTEN_HOST"],
    port=int(os.environ["RK_LISTEN_PORT"]),
)
daemon.start()
stopping.wait()
daemon.stop()
"""


def _free_port() -> int:
    with socket.socket() as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _bootstrap(tmp_path: Path) -> None:
    bootstrap_admin_session(
        data_root=tmp_path / "data",
        schema_fragments=ROOT / "schema_fragments",
        deployment_id="deployment-package",
        organization_id="organization-package",
        limits={"upload_bytes": 1024},
        admin=BootstrapAdmin(
            "admin-package",
            "admin:package",
            "Package Administrator",
            "package-administrator-secret",
        ),
        now=NOW,
        expires_at=EXPIRES,
    )


def _config(tmp_path: Path, port: int) -> ServiceLaunchConfig:
    return ServiceLaunchConfig(
        deployment_id="deployment-package",
        argv=(sys.executable, "-c", DAEMON_PROGRAM),
        working_directory=ROOT,
        environment={
            "RK_DEPLOYMENT_ID": "deployment-package",
            "RK_LISTEN_HOST": "127.0.0.1",
            "RK_LISTEN_PORT": str(port),
            "RK_MODEL_SECRET": "must-not-enter-receipt",
        },
        health_url=f"http://127.0.0.1:{port}/healthz",
        state_path=tmp_path / "service" / "state.json",
        stdout_path=tmp_path / "logs" / "stdout.log",
        stderr_path=tmp_path / "logs" / "stderr.log",
        startup_timeout_seconds=5,
        stop_timeout_seconds=5,
        poll_interval_seconds=0.05,
    )


@pytest.mark.skipif(os.name == "nt", reason="Windows native lifecycle runs on its release runner")
def test_empty_root_daemon_survives_desktop_disconnect_and_restarts(tmp_path: Path) -> None:
    _bootstrap(tmp_path)
    config = _config(tmp_path, _free_port())
    desktop = ServiceLifecycle(config, clock=lambda: NOW)
    started = desktop.start()
    detached = desktop.disconnect_desktop()

    health = json.load(urllib.request.urlopen(config.health_url))
    persisted = json.loads(config.state_path.read_text(encoding="utf-8"))
    controller = ServiceLifecycle(config, clock=lambda: NOW)
    stopped = controller.stop()
    restarted = controller.start()
    restarted_again = controller.restart()
    controller.stop()

    assert started.status is ServiceStatus.RUNNING
    assert detached.pid == started.pid
    assert health["deployment_id"] == config.deployment_id
    assert persisted["status"] == "RUNNING"
    assert persisted["configured_environment_keys"] == sorted(config.environment)
    assert "must-not-enter-receipt" not in config.state_path.read_text(encoding="utf-8")
    assert stopped.status is ServiceStatus.STOPPED
    assert restarted.status is ServiceStatus.RUNNING
    assert restarted.generation == 2
    assert restarted_again.status is ServiceStatus.RUNNING
    assert restarted_again.generation == 3


def test_nonzero_daemon_never_becomes_running(tmp_path: Path) -> None:
    config = ServiceLaunchConfig(
        deployment_id="deployment-fail",
        argv=(sys.executable, "-c", "raise SystemExit(9)"),
        working_directory=ROOT,
        environment={},
        health_url=f"http://127.0.0.1:{_free_port()}/healthz",
        state_path=tmp_path / "state.json",
        stdout_path=tmp_path / "stdout.log",
        stderr_path=tmp_path / "stderr.log",
        startup_timeout_seconds=2,
        stop_timeout_seconds=2,
        poll_interval_seconds=0.05,
    )
    lifecycle = ServiceLifecycle(config, clock=lambda: NOW)

    with pytest.raises(ServiceLifecycleError, match="DAEMON_EXITED_BEFORE_HEALTH"):
        lifecycle.start()

    latest = lifecycle.latest()
    assert latest is not None
    assert latest.status is ServiceStatus.FAILED
    assert latest.pid is None


def test_release_contract_archive_is_deterministic_and_hash_verified(tmp_path: Path) -> None:
    first = tmp_path / "first.zip"
    second = tmp_path / "second.zip"
    command = [sys.executable, str(ROOT / "packaging" / "build_release.py"), "--output"]
    subprocess.run([*command, str(first)], check=True)
    subprocess.run([*command, str(second)], check=True)

    assert (
        hashlib.sha256(first.read_bytes()).digest() == hashlib.sha256(second.read_bytes()).digest()
    )
    with zipfile.ZipFile(first) as archive:
        names = set(archive.namelist())
        hashes = json.loads(archive.read("SHA256SUMS.json"))["files"]
        manifest = json.loads(archive.read("release-manifest.json"))
        for name, expected in hashes.items():
            assert hashlib.sha256(archive.read(name)).hexdigest() == expected

    assert set(manifest["payloads"]) <= names
    assert set(manifest["native_acceptance"].values()) == {"REQUIRES_WINDOWS_RUNNER"}


def test_windows_service_and_tauri_contracts_are_parameterized_and_honest() -> None:
    service = (ROOT / "packaging" / "windows" / "rk-product-service.xml").read_text()
    installer = (ROOT / "packaging" / "windows" / "install-service.ps1").read_text()
    tauri = json.loads((ROOT / "packaging" / "tauri" / "sidecar-lifecycle.json").read_text())
    linux = (ROOT / "packaging" / "linux" / "rk-product.service").read_text()

    assert "@RK_DAEMON_EXECUTABLE@" in service
    assert "@RK_DATA_ROOT@" in service
    assert "@RK_LISTEN_HOST@" in service and "@RK_LISTEN_PORT@" in service
    assert "Unresolved RK service template token" in installer
    assert tauri["daemon_ownership"] == "SYSTEM_SERVICE"
    assert tauri["desktop_exit"] == "LEAVE_DAEMON_RUNNING"
    assert tauri["capability_transport"] == "OPAQUE_SESSION_COOKIE_ONLY"
    assert "@RK_DAEMON_EXECUTABLE@" in linux and "@RK_ENV_FILE@" in linux
