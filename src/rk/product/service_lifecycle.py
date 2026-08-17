"""Auditable lifecycle control for a configured RK product daemon process."""

from __future__ import annotations

import json
import os
import signal
import subprocess
import time
import urllib.error
import urllib.request
from collections.abc import Callable, Mapping
from dataclasses import asdict, dataclass
from enum import StrEnum
from pathlib import Path
from types import MappingProxyType
from urllib.parse import urlsplit


class ServiceLifecycleError(RuntimeError):
    """The configured daemon could not complete a requested lifecycle transition."""


class ServiceStatus(StrEnum):
    STARTING = "STARTING"
    RUNNING = "RUNNING"
    STOPPED = "STOPPED"
    FAILED = "FAILED"


@dataclass(frozen=True, slots=True)
class ServiceLaunchConfig:
    deployment_id: str
    argv: tuple[str, ...]
    working_directory: Path
    environment: Mapping[str, str]
    health_url: str
    state_path: Path
    stdout_path: Path
    stderr_path: Path
    startup_timeout_seconds: float
    stop_timeout_seconds: float
    poll_interval_seconds: float

    def __post_init__(self) -> None:
        parsed = urlsplit(self.health_url)
        if not self.deployment_id or not self.argv or any(not part for part in self.argv):
            raise ValueError("service deployment and argv must be explicit")
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("health_url must be an absolute HTTP(S) URL")
        if parsed.username is not None or parsed.password is not None:
            raise ValueError("health_url must not contain credentials")
        if urlsplit(self.health_url).path != "/healthz":
            raise ValueError("health_url must target the published daemon health endpoint")
        if (
            self.startup_timeout_seconds <= 0
            or self.stop_timeout_seconds <= 0
            or not 0 < self.poll_interval_seconds <= 1
        ):
            raise ValueError("service lifecycle timeouts must be positive")
        if any(
            not key or "=" in key or "\x00" in key or "\x00" in value
            for key, value in self.environment.items()
        ):
            raise ValueError("service environment contains an invalid entry")
        object.__setattr__(self, "working_directory", Path(self.working_directory))
        object.__setattr__(self, "state_path", Path(self.state_path))
        object.__setattr__(self, "stdout_path", Path(self.stdout_path))
        object.__setattr__(self, "stderr_path", Path(self.stderr_path))
        object.__setattr__(self, "environment", MappingProxyType(dict(self.environment)))


@dataclass(frozen=True, slots=True)
class ServiceReceipt:
    schema_version: str
    deployment_id: str
    generation: int
    status: ServiceStatus
    pid: int | None
    process_marker: str | None
    recorded_at: str
    forced_stop: bool
    fault_code: str | None
    configured_environment_keys: tuple[str, ...]


class ServiceLifecycle:
    """Launch a detached daemon and retain control in a durable, credential-free receipt."""

    def __init__(
        self,
        config: ServiceLaunchConfig,
        *,
        clock: Callable[[], str],
        monotonic: Callable[[], float] = time.monotonic,
        wait: Callable[[float], None] = time.sleep,
    ) -> None:
        self._config = config
        self._clock = clock
        self._monotonic = monotonic
        self._wait = wait
        self._child: subprocess.Popen[bytes] | None = None

    def start(self) -> ServiceReceipt:
        previous = self.latest()
        if previous is not None and previous.pid is not None and self._same_process(previous):
            raise ServiceLifecycleError("SERVICE_ALREADY_RUNNING")
        generation = 1 if previous is None else previous.generation + 1
        self._config.state_path.parent.mkdir(parents=True, exist_ok=True)
        self._config.stdout_path.parent.mkdir(parents=True, exist_ok=True)
        self._config.stderr_path.parent.mkdir(parents=True, exist_ok=True)
        environment = dict(os.environ)
        environment.update(self._config.environment)
        with (
            self._config.stdout_path.open("ab", buffering=0) as stdout_writer,
            self._config.stderr_path.open("ab", buffering=0) as stderr_writer,
        ):
            child = subprocess.Popen(
                self._config.argv,
                cwd=self._config.working_directory,
                env=environment,
                stdin=subprocess.DEVNULL,
                stdout=stdout_writer,
                stderr=stderr_writer,
                start_new_session=os.name != "nt",
                creationflags=_windows_creation_flags(),
            )
        self._child = child
        marker = _process_marker(child.pid)
        if marker is None:
            child.terminate()
            return self._fail_start(generation, "PROCESS_IDENTITY_UNAVAILABLE")
        self._record(
            generation=generation,
            status=ServiceStatus.STARTING,
            pid=child.pid,
            marker=marker,
            forced=False,
            fault=None,
        )
        deadline = self._monotonic() + self._config.startup_timeout_seconds
        while self._monotonic() < deadline:
            if child.poll() is not None:
                return self._fail_start(generation, "DAEMON_EXITED_BEFORE_HEALTH")
            if self._healthy():
                return self._record(
                    generation=generation,
                    status=ServiceStatus.RUNNING,
                    pid=child.pid,
                    marker=marker,
                    forced=False,
                    fault=None,
                )
            self._wait(self._config.poll_interval_seconds)
        self._terminate(child.pid, marker, force=False)
        return self._fail_start(generation, "DAEMON_HEALTH_TIMEOUT")

    def stop(self) -> ServiceReceipt:
        current = self.latest()
        if current is None or current.pid is None or current.process_marker is None:
            raise ServiceLifecycleError("SERVICE_NOT_RUNNING")
        if not self._same_process(current):
            raise ServiceLifecycleError("SERVICE_PROCESS_IDENTITY_MISMATCH")
        self._terminate(current.pid, current.process_marker, force=False)
        deadline = self._monotonic() + self._config.stop_timeout_seconds
        while (
            self._monotonic() < deadline and _process_marker(current.pid) == current.process_marker
        ):
            self._reap_child()
            self._wait(self._config.poll_interval_seconds)
        forced = _process_marker(current.pid) == current.process_marker
        if forced:
            self._terminate(current.pid, current.process_marker, force=True)
            self._reap_child()
        return self._record(
            generation=current.generation,
            status=ServiceStatus.STOPPED,
            pid=None,
            marker=None,
            forced=forced,
            fault=None,
        )

    def restart(self) -> ServiceReceipt:
        self.stop()
        return self.start()

    def disconnect_desktop(self) -> ServiceReceipt:
        """Drop the shell's child handle without stopping the system-owned daemon."""
        current = self.latest()
        if current is None or not self._same_process(current):
            raise ServiceLifecycleError("SERVICE_NOT_RUNNING")
        self._child = None
        return current

    def latest(self) -> ServiceReceipt | None:
        path = self._config.state_path
        if not path.exists():
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
        return ServiceReceipt(
            schema_version=str(value["schema_version"]),
            deployment_id=str(value["deployment_id"]),
            generation=int(value["generation"]),
            status=ServiceStatus(str(value["status"])),
            pid=int(value["pid"]) if value["pid"] is not None else None,
            process_marker=str(value["process_marker"])
            if value["process_marker"] is not None
            else None,
            recorded_at=str(value["recorded_at"]),
            forced_stop=bool(value["forced_stop"]),
            fault_code=str(value["fault_code"]) if value["fault_code"] is not None else None,
            configured_environment_keys=tuple(
                str(item) for item in value["configured_environment_keys"]
            ),
        )

    def _healthy(self) -> bool:
        try:
            with urllib.request.urlopen(
                self._config.health_url,
                timeout=min(1.0, self._config.poll_interval_seconds),
            ) as response:
                value = json.load(response)
        except (OSError, urllib.error.URLError, json.JSONDecodeError):
            return False
        return (
            response.status == 200
            and isinstance(value, dict)
            and value.get("schema_version") == "rk.product.daemon_health.v1"
            and value.get("deployment_id") == self._config.deployment_id
            and value.get("status") == "AVAILABLE"
        )

    def _same_process(self, receipt: ServiceReceipt) -> bool:
        return (
            receipt.deployment_id == self._config.deployment_id
            and receipt.pid is not None
            and receipt.process_marker is not None
            and _process_marker(receipt.pid) == receipt.process_marker
        )

    def _terminate(self, pid: int, marker: str, *, force: bool) -> None:
        if _process_marker(pid) != marker:
            raise ServiceLifecycleError("SERVICE_PROCESS_IDENTITY_MISMATCH")
        if os.name == "nt":
            if self._child is None or self._child.pid != pid:
                raise ServiceLifecycleError("WINDOWS_SERVICE_CONTROL_REQUIRES_NATIVE_MANAGER")
            self._child.kill() if force else self._child.terminate()
        else:
            os.killpg(pid, signal.SIGKILL if force else signal.SIGTERM)

    def _fail_start(self, generation: int, fault: str) -> ServiceReceipt:
        self._reap_child()
        receipt = self._record(
            generation=generation,
            status=ServiceStatus.FAILED,
            pid=None,
            marker=None,
            forced=False,
            fault=fault,
        )
        raise ServiceLifecycleError(receipt.fault_code or "SERVICE_START_FAILED")

    def _reap_child(self) -> None:
        if self._child is not None:
            self._child.poll()

    def _record(
        self,
        *,
        generation: int,
        status: ServiceStatus,
        pid: int | None,
        marker: str | None,
        forced: bool,
        fault: str | None,
    ) -> ServiceReceipt:
        receipt = ServiceReceipt(
            schema_version="rk.product.service_lifecycle.v1",
            deployment_id=self._config.deployment_id,
            generation=generation,
            status=status,
            pid=pid,
            process_marker=marker,
            recorded_at=self._clock(),
            forced_stop=forced,
            fault_code=fault,
            configured_environment_keys=tuple(sorted(self._config.environment)),
        )
        payload = asdict(receipt)
        payload["status"] = receipt.status.value
        temporary = self._config.state_path.with_name(self._config.state_path.name + ".new")
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(temporary, self._config.state_path)
        return receipt


def _process_marker(pid: int) -> str | None:
    if os.name == "nt":
        return f"windows-pid:{pid}" if pid > 0 else None
    path = Path("/proc") / str(pid) / "stat"
    try:
        fields = path.read_text(encoding="utf-8").split()
    except OSError:
        return None
    if len(fields) < 22 or fields[2] == "Z":
        return None
    return f"proc-start:{fields[21]}"


def _windows_creation_flags() -> int:
    if os.name != "nt":
        return 0
    return 0x00000200 | 0x00000008


__all__ = [
    "ServiceLaunchConfig",
    "ServiceLifecycle",
    "ServiceLifecycleError",
    "ServiceReceipt",
    "ServiceStatus",
]
