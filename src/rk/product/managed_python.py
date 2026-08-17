"""Fixed-profile Python execution with bounded processes and artifact-only I/O."""

from __future__ import annotations

import hashlib
import json
import os
import re
import resource
import shutil
import signal
import sqlite3
import subprocess
import time
from collections.abc import Callable, Mapping, Sequence
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from rk.extensions import ToolReceipt
from rk.product.artifact_read import ArtifactReadService, ExactArtifactRef
from rk.product.compute import AuthorityCeiling, ResourceRequest, ResourceUsage
from rk.product.jobs import (
    ExecutionOutcome,
    ExecutionReceipt,
    JobLease,
    JobState,
    JobStore,
)
from rk.product.tool_runs import ToolRunStore
from rk.sqlite import open_sqlite
from rk.wire import canonical_json_bytes


class ManagedPythonError(RuntimeError):
    """A managed environment, script, process, or output invariant failed."""


class ManagedPythonConflict(ManagedPythonError):
    """A stable profile or execution identity was rebound."""


_PROFILE_ID = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
_OUTPUT_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$")
_PYTHON_MEDIA = frozenset({"text/x-python", "application/x-python"})
_OUTPUT_MEDIA = {
    "TABLE": frozenset({"text/csv", "application/vnd.apache.parquet"}),
    "IMAGE": frozenset({"image/png", "image/svg+xml"}),
    "JSON": frozenset({"application/json"}),
    "TEXT": frozenset({"text/plain", "text/markdown"}),
}


@dataclass(frozen=True, slots=True)
class ManagedPythonProfile:
    profile_id: str
    environment_digest: str
    interpreter_path: str
    interpreter_sha256: str
    lock_artifact: ExactArtifactRef
    packages: Mapping[str, str]
    authority_ceiling: AuthorityCeiling
    availability: str
    registered_at: str


@dataclass(frozen=True, slots=True)
class NamedInputArtifact:
    logical_name: str
    artifact: ExactArtifactRef


@dataclass(frozen=True, slots=True)
class ManagedPythonRequest:
    execution_id: str
    tool_run_id: str
    attempt_id: str
    profile_id: str
    script_artifact: ExactArtifactRef
    inputs: tuple[NamedInputArtifact, ...]


@dataclass(frozen=True, slots=True)
class FailureAdjustment:
    failure_code: str
    retryable: bool
    parameter_changes: Mapping[str, int | str]

    def to_dict(self) -> dict[str, object]:
        return {
            "failure_code": self.failure_code,
            "retryable": self.retryable,
            "parameter_changes": dict(self.parameter_changes),
        }


@dataclass(frozen=True, slots=True)
class ManagedPythonResult:
    execution_id: str
    outcome: ExecutionOutcome
    output_artifacts: tuple[ExactArtifactRef, ...]
    public_log_artifact: ExactArtifactRef
    resource_usage: ResourceUsage
    failure_adjustment: FailureAdjustment | None


class ArtifactPublisher(Protocol):
    def publish(self, *, data: bytes, logical_name: str, media_type: str) -> ExactArtifactRef: ...


class ManagedPythonProfileStore:
    """Register immutable profiles only after exact lock and interpreter probes pass."""

    def __init__(
        self,
        db_path: Path,
        artifacts: ArtifactReadService,
        *,
        busy_timeout_ms: int = 5_000,
    ) -> None:
        self._db_path = Path(db_path)
        self._artifacts = artifacts
        self._busy_timeout_ms = busy_timeout_ms

    def register(
        self,
        *,
        profile_id: str,
        interpreter: Path,
        lock_artifact: ExactArtifactRef,
        authority_ceiling: AuthorityCeiling,
        now: str,
    ) -> ManagedPythonProfile:
        if _PROFILE_ID.fullmatch(profile_id) is None:
            raise ManagedPythonError("profile_id is not stable")
        if authority_ceiling == AuthorityCeiling.CERTIFICATE_REQUIRES_VALIDATION:
            raise ManagedPythonError("ordinary Python cannot have certificate authority")
        resolved = Path(interpreter).resolve(strict=True)
        if not resolved.is_file() or not os.access(resolved, os.X_OK):
            raise ManagedPythonError("profile interpreter is not an executable file")
        lock = self._read_lock(lock_artifact)
        packages = lock["packages"]
        assert isinstance(packages, dict)
        interpreter_sha256 = _file_sha256(resolved)
        observed = _probe_packages(resolved, tuple(sorted(packages)))
        available = "AVAILABLE" if observed == packages else "UNAVAILABLE"
        environment_digest = hashlib.sha256(canonical_json_bytes(lock)).hexdigest()
        immutable = (
            environment_digest,
            str(resolved),
            interpreter_sha256,
            lock_artifact.artifact_id,
            lock_artifact.sha256,
            lock_artifact.byte_count,
            lock_artifact.media_type,
            _json(packages),
            str(authority_ceiling),
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT environment_digest,interpreter_path,interpreter_sha256,"
                "lock_artifact_id,lock_artifact_sha256,lock_artifact_byte_count,"
                "lock_artifact_media_type,packages_json,authority_ceiling "
                "FROM product_managed_python_profiles WHERE profile_id=?",
                (profile_id,),
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO product_managed_python_profiles("
                    "profile_id,environment_digest,interpreter_path,interpreter_sha256,"
                    "lock_artifact_id,lock_artifact_sha256,lock_artifact_byte_count,"
                    "lock_artifact_media_type,packages_json,authority_ceiling,availability,"
                    "registered_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (profile_id, *immutable, available, now),
                )
            elif tuple(row) != immutable:
                raise ManagedPythonConflict("managed profile metadata differs")
            connection.commit()
        return self.get(profile_id)

    def get(self, profile_id: str) -> ManagedPythonProfile:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT profile_id,environment_digest,interpreter_path,interpreter_sha256,"
                "lock_artifact_id,lock_artifact_sha256,lock_artifact_byte_count,"
                "lock_artifact_media_type,packages_json,authority_ceiling,availability,"
                "registered_at FROM product_managed_python_profiles WHERE profile_id=?",
                (profile_id,),
            ).fetchone()
        if row is None:
            raise KeyError(profile_id)
        return _profile(row)

    def _read_lock(self, ref: ExactArtifactRef) -> dict[str, object]:
        if ref.media_type.partition(";")[0].strip().lower() != "application/json":
            raise ManagedPythonError("environment lock must be application/json")
        raw = b"".join(self._artifacts.open_range(ref.artifact_id, expected_ref=ref).stream)
        try:
            value = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ManagedPythonError("environment lock is not UTF-8 JSON") from error
        if not isinstance(value, dict) or set(value) != {
            "schema_version",
            "python",
            "packages",
        }:
            raise ManagedPythonError("environment lock fields are not exact")
        if value["schema_version"] != "rk.managed-python-lock.v1":
            raise ManagedPythonError("environment lock schema is unsupported")
        if not isinstance(value["python"], str) or not value["python"]:
            raise ManagedPythonError("environment lock Python version is invalid")
        packages = value["packages"]
        if not isinstance(packages, dict) or any(
            not isinstance(key, str) or not key or not isinstance(version, str) or not version
            for key, version in packages.items()
        ):
            raise ManagedPythonError("environment package lock is invalid")
        return value

    def _connect(self) -> sqlite3.Connection:
        return _connect(self._db_path, self._busy_timeout_ms)


class ManagedPythonExecutor:
    """Execute exactly one artifact script in one fixed profile without a shell seam."""

    def __init__(
        self,
        *,
        db_path: Path,
        workspace_root: Path,
        artifacts: ArtifactReadService,
        publisher: ArtifactPublisher,
        profiles: ManagedPythonProfileStore,
        jobs: JobStore,
        tool_runs: ToolRunStore,
        clock: Callable[[], str],
        monotonic: Callable[[], float] = time.monotonic,
        poll_seconds: float = 0.05,
        busy_timeout_ms: int = 5_000,
        defer_b03_resolution: bool = False,
    ) -> None:
        if poll_seconds <= 0:
            raise ValueError("poll_seconds must be positive")
        self._db_path = Path(db_path)
        self._root = Path(workspace_root).resolve()
        self._root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self._artifacts = artifacts
        self._publisher = publisher
        self._profiles = profiles
        self._jobs = jobs
        self._tool_runs = tool_runs
        self._clock = clock
        self._monotonic = monotonic
        self._poll_seconds = poll_seconds
        self._busy_timeout_ms = busy_timeout_ms
        self._defer_b03_resolution = defer_b03_resolution

    def execute(
        self,
        request: ManagedPythonRequest,
        lease: JobLease,
    ) -> ManagedPythonResult:
        profile, resources, job_id = self._validate_binding(request, lease)
        workspace = self._workspace(request.execution_id)
        input_dir = workspace / "input"
        output_dir = workspace / "output"
        script_path = workspace / "script.py"
        wrapper_path = workspace / "runner.py"
        log_path = workspace / "public.log"
        self._reserve(request, job_id, self._clock())
        try:
            workspace.mkdir(mode=0o700)
            # Stage inputs while the directory is private and writable, then
            # freeze it before the untrusted interpreter starts.  Creating the
            # directory as 0500 made the executor accidentally root-only.
            input_dir.mkdir(mode=0o700)
            output_dir.mkdir(mode=0o700)
            _write_readonly(script_path, self._read_script(request.script_artifact))
            _write_readonly(wrapper_path, _WRAPPER.encode("utf-8"))
            for item in request.inputs:
                _write_readonly(input_dir / item.logical_name, self._read(item.artifact))
            input_dir.chmod(0o500)
            with log_path.open("xb") as log:
                started = self._monotonic()
                process = subprocess.Popen(
                    (
                        profile.interpreter_path,
                        "-I",
                        "-B",
                        str(wrapper_path),
                        str(script_path),
                    ),
                    cwd=workspace,
                    env={
                        "PATH": str(Path(profile.interpreter_path).parent),
                        "PYTHONHASHSEED": "0",
                        "MPLBACKEND": "Agg",
                        "RK_INPUT_DIR": str(input_dir),
                        "RK_OUTPUT_DIR": str(output_dir),
                    },
                    stdin=subprocess.DEVNULL,
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    start_new_session=True,
                    close_fds=True,
                    preexec_fn=_limits(resources),
                )
                self._mark_running(request.execution_id, process.pid)
                outcome, failure, cpu_millis, memory_peak = self._wait(
                    process, job_id, resources, started
                )
            elapsed_ms = max(0, round((self._monotonic() - started) * 1000))
            usage = ResourceUsage(
                cpu_millis=cpu_millis,
                memory_peak_bytes=memory_peak,
                wall_time_ms=elapsed_ms,
                gpu_millis=0,
            )
            outputs: tuple[ExactArtifactRef, ...] = ()
            summary = "managed Python execution failed"
            if outcome == ExecutionOutcome.SUCCEEDED:
                try:
                    outputs, summary = self._publish_outputs(output_dir)
                except ManagedPythonError:
                    outcome = ExecutionOutcome.FAILED
                    failure = _adjustment("OUTPUT_MANIFEST_INVALID", resources)
            log_ref = self._publisher.publish(
                data=log_path.read_bytes(),
                logical_name="managed-python.log",
                media_type="text/plain",
            )
            if failure is not None:
                summary = f"managed Python failed: {failure.failure_code}"
            result = self._record_receipts(
                request=request,
                lease=lease,
                outcome=outcome,
                exit_code=process.returncode,
                outputs=outputs,
                log_ref=log_ref,
                usage=usage,
                summary=summary,
                failure=failure,
            )
            return result
        except BaseException:
            self._mark_abandoned(request.execution_id, self._clock())
            raise
        finally:
            shutil.rmtree(workspace, ignore_errors=True)

    def recover_receipts(self, *, now: str) -> tuple[str, ...]:
        """Finish the B03-success to S00-receipt handoff after a service restart."""
        recovered: list[str] = []
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT execution_id,pending_tool_receipt_json FROM "
                "product_managed_python_executions WHERE runtime_state='RUNNING' "
                "AND pending_tool_receipt_json IS NOT NULL ORDER BY execution_id"
            ).fetchall()
        for execution_id, encoded in rows:
            value = json.loads(str(encoded))
            receipt = ToolReceipt(
                tool_run_id=str(value["tool_run_id"]),
                attempt_id=str(value["attempt_id"]),
                status=str(value["status"]),
                payload=value["payload"],
                artifact_ids=tuple(value["artifact_ids"]),
            )
            attempt = next(
                item
                for item in self._tool_runs.attempts(receipt.tool_run_id)
                if item.attempt_id == receipt.attempt_id
            )
            if self._jobs.get(attempt.job_id).state != JobState(receipt.status):
                continue
            self._tool_runs.record_receipt(receipt, now=now)
            self._finish(str(execution_id), receipt.artifact_ids, value, now)
            recovered.append(str(execution_id))
        return tuple(recovered)

    def abandon_orphaned_processes(self, *, now: str) -> tuple[str, ...]:
        """Kill only processes whose Linux start identity still matches the durable row."""
        abandoned: list[str] = []
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT execution_id,pid,process_start_ticks FROM "
                "product_managed_python_executions WHERE runtime_state='RUNNING' "
                "AND pending_tool_receipt_json IS NULL ORDER BY execution_id"
            ).fetchall()
        for execution_id, pid_value, ticks_value in rows:
            if pid_value is not None and ticks_value is not None:
                pid = int(pid_value)
                if _process_start_ticks(pid) == int(ticks_value):
                    _terminate_group(pid)
            self._mark_abandoned(str(execution_id), now)
            abandoned.append(str(execution_id))
        return tuple(abandoned)

    def _validate_binding(
        self, request: ManagedPythonRequest, lease: JobLease
    ) -> tuple[ManagedPythonProfile, ResourceRequest, str]:
        if not request.execution_id or request.execution_id != request.execution_id.strip():
            raise ManagedPythonError("execution ID must be stable")
        script_media = request.script_artifact.media_type.partition(";")[0].strip().lower()
        if script_media not in _PYTHON_MEDIA:
            raise ManagedPythonError("managed script must be a Python source artifact")
        names = tuple(item.logical_name for item in request.inputs)
        if len(set(names)) != len(names) or any(
            _OUTPUT_NAME.fullmatch(name) is None for name in names
        ):
            raise ManagedPythonError("input logical names must be unique safe basenames")
        profile = self._profiles.get(request.profile_id)
        if profile.availability != "AVAILABLE":
            raise ManagedPythonError("managed environment did not pass its lock probe")
        run = self._tool_runs.get(request.tool_run_id)
        if run.current_attempt_id != request.attempt_id:
            raise ManagedPythonError("managed request does not bind the current attempt")
        if run.authority_ceiling == AuthorityCeiling.CERTIFICATE_REQUIRES_VALIDATION:
            raise ManagedPythonError("ordinary Python run cannot request certificate authority")
        attempt = next(
            item
            for item in self._tool_runs.attempts(request.tool_run_id)
            if item.attempt_id == request.attempt_id
        )
        if attempt.job_id != lease.job_id:
            raise ManagedPythonError("managed attempt does not bind the supplied B03 lease")
        job = self._jobs.get(lease.job_id)
        if job.state != JobState.RUNNING:
            raise ManagedPythonError("managed execution requires a running B03 lease")
        values = attempt.resources
        resources = ResourceRequest(
            cpu_millis=int(values["cpu_millis"]),
            memory_bytes=int(values["memory_bytes"]),
            wall_time_ms=int(values["wall_time_ms"]),
            gpu_count=int(values["gpu_count"]),
        )
        if resources.gpu_count:
            raise ManagedPythonError("ordinary managed Python profile is CPU-only")
        return profile, resources, attempt.job_id

    def _read_script(self, ref: ExactArtifactRef) -> bytes:
        raw = self._read(ref)
        try:
            compile(raw.decode("utf-8"), "script.py", "exec")
        except (UnicodeDecodeError, SyntaxError) as error:
            raise ManagedPythonError("script artifact is not valid UTF-8 Python") from error
        return raw

    def _read(self, ref: ExactArtifactRef) -> bytes:
        return b"".join(self._artifacts.open_range(ref.artifact_id, expected_ref=ref).stream)

    def _wait(
        self,
        process: subprocess.Popen[bytes],
        job_id: str,
        resources: ResourceRequest,
        started: float,
    ) -> tuple[ExecutionOutcome, FailureAdjustment | None, int, int]:
        wall_seconds = resources.wall_time_ms / 1000
        cpu_millis = 0
        memory_peak = 0
        while process.poll() is None:
            observed_cpu, observed_memory = _process_usage(process.pid)
            cpu_millis = max(cpu_millis, observed_cpu)
            memory_peak = max(memory_peak, observed_memory)
            if self._jobs.get(job_id).state == JobState.CANCEL_REQUESTED:
                _terminate_group(process.pid)
                process.wait()
                return (
                    ExecutionOutcome.CANCELLED,
                    _adjustment("CANCELLED", resources),
                    cpu_millis,
                    memory_peak,
                )
            if self._monotonic() - started >= wall_seconds:
                _terminate_group(process.pid)
                process.wait()
                return (
                    ExecutionOutcome.FAILED,
                    _adjustment("WALL_TIME_LIMIT", resources),
                    cpu_millis,
                    memory_peak,
                )
            time.sleep(self._poll_seconds)
        if process.returncode == 0:
            return ExecutionOutcome.SUCCEEDED, None, cpu_millis, memory_peak
        if process.returncode in {-signal.SIGXCPU, -signal.SIGKILL}:
            return (
                ExecutionOutcome.FAILED,
                _adjustment("RESOURCE_LIMIT", resources),
                cpu_millis,
                memory_peak,
            )
        return (
            ExecutionOutcome.FAILED,
            _adjustment("SCRIPT_FAILED", resources),
            cpu_millis,
            memory_peak,
        )

    def _publish_outputs(self, output_dir: Path) -> tuple[tuple[ExactArtifactRef, ...], str]:
        manifest_path = output_dir / "manifest.json"
        if not manifest_path.is_file() or manifest_path.is_symlink():
            raise ManagedPythonError("output manifest is missing")
        try:
            value = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ManagedPythonError("output manifest is invalid") from error
        if not isinstance(value, dict) or set(value) != {
            "schema_version",
            "public_summary",
            "artifacts",
        }:
            raise ManagedPythonError("output manifest fields are not exact")
        if value["schema_version"] != "rk.managed-python-output.v1":
            raise ManagedPythonError("output manifest schema is unsupported")
        summary = value["public_summary"]
        artifacts = value["artifacts"]
        if not isinstance(summary, str) or not summary or not isinstance(artifacts, list):
            raise ManagedPythonError("output manifest summary or artifacts are invalid")
        published: list[ExactArtifactRef] = []
        names: set[str] = set()
        for item in artifacts:
            if not isinstance(item, dict) or set(item) != {"path", "kind", "media_type"}:
                raise ManagedPythonError("output artifact fields are not exact")
            name, kind, media_type = item["path"], item["kind"], item["media_type"]
            if (
                not isinstance(name, str)
                or _OUTPUT_NAME.fullmatch(name) is None
                or name == "manifest.json"
                or name in names
                or not isinstance(kind, str)
                or kind not in _OUTPUT_MEDIA
                or not isinstance(media_type, str)
                or media_type not in _OUTPUT_MEDIA[kind]
            ):
                raise ManagedPythonError("output artifact declaration is invalid")
            path = output_dir / name
            if not path.is_file() or path.is_symlink():
                raise ManagedPythonError("declared output is not a regular file")
            names.add(name)
            published.append(
                self._publisher.publish(
                    data=path.read_bytes(), logical_name=name, media_type=media_type
                )
            )
        return tuple(published), summary

    def _record_receipts(
        self,
        *,
        request: ManagedPythonRequest,
        lease: JobLease,
        outcome: ExecutionOutcome,
        exit_code: int | None,
        outputs: tuple[ExactArtifactRef, ...],
        log_ref: ExactArtifactRef,
        usage: ResourceUsage,
        summary: str,
        failure: FailureAdjustment | None,
    ) -> ManagedPythonResult:
        status = str(outcome)
        artifact_ids = (log_ref.artifact_id, *(item.artifact_id for item in outputs))
        receipt = ToolReceipt(
            tool_run_id=request.tool_run_id,
            attempt_id=request.attempt_id,
            status=status,
            payload={
                "exit_code": exit_code,
                "resource_usage": usage.to_dict(),
                "public_log_artifact_id": log_ref.artifact_id,
                "failure_code": failure.failure_code if failure is not None else None,
                "public_summary": summary,
            },
            artifact_ids=artifact_ids,
        )
        encoded = {
            "tool_run_id": receipt.tool_run_id,
            "attempt_id": receipt.attempt_id,
            "status": receipt.status,
            "payload": dict(receipt.payload),
            "artifact_ids": list(receipt.artifact_ids),
            "failure_adjustment": failure.to_dict() if failure is not None else None,
        }
        self._stage_receipt(request.execution_id, encoded)
        result = ManagedPythonResult(
            execution_id=request.execution_id,
            outcome=outcome,
            output_artifacts=outputs,
            public_log_artifact=log_ref,
            resource_usage=usage,
            failure_adjustment=failure,
        )
        if self._defer_b03_resolution:
            return result
        self._jobs.record_execution(
            lease,
            ExecutionReceipt(
                outcome=outcome,
                exit_code=exit_code,
                result_refs=tuple(
                    {
                        "artifact_id": item.artifact_id,
                        "sha256": item.sha256,
                        "byte_count": item.byte_count,
                        "media_type": item.media_type,
                    }
                    for item in outputs
                ),
                failure_code=failure.failure_code if failure is not None else None,
            ),
            now=self._clock(),
        )
        self._tool_runs.record_receipt(receipt, now=self._clock())
        self._finish(request.execution_id, artifact_ids, encoded, self._clock())
        return result

    def _reserve(self, request: ManagedPythonRequest, job_id: str, now: str) -> None:
        encoded_inputs = _json(
            [
                {
                    "logical_name": item.logical_name,
                    "artifact_id": item.artifact.artifact_id,
                    "sha256": item.artifact.sha256,
                    "byte_count": item.artifact.byte_count,
                    "media_type": item.artifact.media_type,
                }
                for item in request.inputs
            ]
        )
        ref = request.script_artifact
        immutable = (
            request.tool_run_id,
            request.attempt_id,
            job_id,
            request.profile_id,
            ref.artifact_id,
            ref.sha256,
            ref.byte_count,
            ref.media_type,
            encoded_inputs,
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT tool_run_id,attempt_id,job_id,profile_id,script_artifact_id,"
                "script_artifact_sha256,script_artifact_byte_count,script_artifact_media_type,"
                "input_artifacts_json,runtime_state FROM product_managed_python_executions "
                "WHERE execution_id=?",
                (request.execution_id,),
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO product_managed_python_executions("
                    "execution_id,tool_run_id,attempt_id,job_id,profile_id,script_artifact_id,"
                    "script_artifact_sha256,script_artifact_byte_count,"
                    "script_artifact_media_type,input_artifacts_json,runtime_state,started_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,'STARTING',?)",
                    (request.execution_id, *immutable, now),
                )
            elif tuple(row[:9]) != immutable:
                raise ManagedPythonConflict("execution ID is bound to another request")
            else:
                raise ManagedPythonConflict(f"execution already entered durable state {row[9]}")
            connection.commit()

    def _mark_running(self, execution_id: str, pid: int) -> None:
        ticks = _process_start_ticks(pid)
        if ticks is None:
            raise ManagedPythonError("child process identity is unavailable")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            result = connection.execute(
                "UPDATE product_managed_python_executions SET runtime_state='RUNNING',"
                "pid=?,process_start_ticks=? WHERE execution_id=? AND runtime_state='STARTING'",
                (pid, ticks, execution_id),
            )
            if result.rowcount != 1:
                raise ManagedPythonConflict("execution state changed before process bind")
            connection.commit()

    def _stage_receipt(self, execution_id: str, receipt: Mapping[str, object]) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            result = connection.execute(
                "UPDATE product_managed_python_executions SET pending_tool_receipt_json=? "
                "WHERE execution_id=? AND runtime_state='RUNNING'",
                (_json(receipt), execution_id),
            )
            if result.rowcount != 1:
                raise ManagedPythonConflict("execution cannot stage its public receipt")
            connection.commit()

    def _finish(
        self,
        execution_id: str,
        artifact_ids: Sequence[str],
        receipt: Mapping[str, object],
        now: str,
    ) -> None:
        adjustment = receipt.get("failure_adjustment")
        if adjustment is not None and not isinstance(adjustment, Mapping):
            raise ManagedPythonError("staged failure adjustment is invalid")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            result = connection.execute(
                "UPDATE product_managed_python_executions SET "
                "runtime_state='RECEIPT_RECORDED',output_artifact_ids_json=?,"
                "public_log_artifact_id=?,failure_adjustment_json=?,finished_at=? "
                "WHERE execution_id=? AND runtime_state='RUNNING'",
                (
                    _json(list(artifact_ids[1:])),
                    artifact_ids[0],
                    _json(dict(adjustment)) if adjustment is not None else None,
                    now,
                    execution_id,
                ),
            )
            if result.rowcount != 1:
                raise ManagedPythonConflict("execution cannot finish from its durable state")
            connection.commit()

    def _mark_abandoned(self, execution_id: str, now: str) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE product_managed_python_executions SET runtime_state='ABANDONED',"
                "finished_at=? WHERE execution_id=? AND runtime_state IN ('STARTING','RUNNING') "
                "AND pending_tool_receipt_json IS NULL",
                (now, execution_id),
            )
            connection.commit()

    def _workspace(self, execution_id: str) -> Path:
        if re.fullmatch(r"[A-Za-z0-9_-]{3,80}", execution_id) is None:
            raise ManagedPythonError("execution ID is not a safe workspace identity")
        path = self._root / execution_id
        if path.resolve().parent != self._root:
            raise ManagedPythonError("execution workspace escaped its configured root")
        return path

    def _connect(self) -> sqlite3.Connection:
        return _connect(self._db_path, self._busy_timeout_ms)


def _limits(resources: ResourceRequest) -> Callable[[], None]:
    def apply() -> None:
        cpu_seconds = max(1, (resources.cpu_millis + 999) // 1000)
        resource.setrlimit(resource.RLIMIT_CPU, (cpu_seconds, cpu_seconds + 1))
        resource.setrlimit(resource.RLIMIT_AS, (resources.memory_bytes, resources.memory_bytes))
        resource.setrlimit(resource.RLIMIT_NPROC, (1, 1))
        resource.setrlimit(resource.RLIMIT_NOFILE, (64, 64))
        resource.setrlimit(resource.RLIMIT_CORE, (0, 0))

    return apply


def _probe_packages(interpreter: Path, packages: tuple[str, ...]) -> dict[str, str]:
    code = (
        "import importlib.metadata,json,sys;"
        "names=json.loads(sys.stdin.read());"
        "print(json.dumps({n:importlib.metadata.version(n) for n in names},sort_keys=True))"
    )
    completed = subprocess.run(
        (str(interpreter), "-I", "-B", "-c", code),
        input=json.dumps(packages),
        text=True,
        capture_output=True,
        timeout=30,
        check=False,
    )
    if completed.returncode != 0:
        return {}
    value = json.loads(completed.stdout)
    if not isinstance(value, dict) or any(
        not isinstance(key, str) or not isinstance(version, str) for key, version in value.items()
    ):
        return {}
    return value


def _profile(row: tuple[Any, ...]) -> ManagedPythonProfile:
    packages = json.loads(str(row[8]))
    if not isinstance(packages, dict):
        raise ManagedPythonError("persisted package lock is invalid")
    return ManagedPythonProfile(
        profile_id=str(row[0]),
        environment_digest=str(row[1]),
        interpreter_path=str(row[2]),
        interpreter_sha256=str(row[3]),
        lock_artifact=ExactArtifactRef(
            artifact_id=str(row[4]),
            sha256=str(row[5]),
            byte_count=int(row[6]),
            media_type=str(row[7]),
        ),
        packages={str(key): str(value) for key, value in packages.items()},
        authority_ceiling=AuthorityCeiling(str(row[9])),
        availability=str(row[10]),
        registered_at=str(row[11]),
    )


def _adjustment(code: str, resources: ResourceRequest) -> FailureAdjustment:
    if code in {"WALL_TIME_LIMIT", "RESOURCE_LIMIT"}:
        changes: Mapping[str, int | str] = {
            "wall_time_ms": resources.wall_time_ms * 2,
            "memory_bytes": resources.memory_bytes * 2,
        }
        return FailureAdjustment(code, True, changes)
    if code == "SCRIPT_FAILED":
        return FailureAdjustment(code, True, {"action": "inspect_public_log"})
    if code == "OUTPUT_MANIFEST_INVALID":
        return FailureAdjustment(code, True, {"action": "repair_output_manifest"})
    return FailureAdjustment(code, False, {})


def _process_start_ticks(pid: int) -> int | None:
    try:
        fields = Path(f"/proc/{pid}/stat").read_text(encoding="ascii").split()
        return int(fields[21])
    except (FileNotFoundError, PermissionError, ValueError, IndexError):
        return None


def _process_usage(pid: int) -> tuple[int, int]:
    try:
        stat_fields = Path(f"/proc/{pid}/stat").read_text(encoding="ascii").split()
        clock_ticks = int(os.sysconf("SC_CLK_TCK"))
        cpu_millis = (int(stat_fields[13]) + int(stat_fields[14])) * 1000 // clock_ticks
        memory_peak = 0
        for line in Path(f"/proc/{pid}/status").read_text(encoding="ascii").splitlines():
            if line.startswith(("VmHWM:", "VmRSS:")):
                memory_peak = max(memory_peak, int(line.split()[1]) * 1024)
        return cpu_millis, memory_peak
    except (FileNotFoundError, PermissionError, ValueError, IndexError):
        return 0, 0


def _terminate_group(pid: int) -> None:
    try:
        os.killpg(pid, signal.SIGTERM)
    except ProcessLookupError:
        return
    deadline = time.monotonic() + 0.5
    while time.monotonic() < deadline and _process_start_ticks(pid) is not None:
        time.sleep(0.02)
    if _process_start_ticks(pid) is not None:
        with suppress(ProcessLookupError):
            os.killpg(pid, signal.SIGKILL)


def _write_readonly(path: Path, data: bytes) -> None:
    with path.open("xb") as writer:
        writer.write(data)
        writer.flush()
        os.fsync(writer.fileno())
    path.chmod(0o400)


def _file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as reader:
        while chunk := reader.read(1024 * 1024):
            digest.update(chunk)
    return digest.hexdigest()


def _connect(path: Path, busy_timeout_ms: int) -> sqlite3.Connection:
    connection = open_sqlite(path, isolation_level=None)
    connection.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


_WRAPPER = r"""import os
import runpy
import sys
from pathlib import Path

script = Path(sys.argv[1]).resolve()
input_root = Path(os.environ["RK_INPUT_DIR"]).resolve()
output_root = Path(os.environ["RK_OUTPUT_DIR"]).resolve()
read_roots = (script.parent, input_root, output_root, Path(sys.prefix), Path(sys.base_prefix))

def inside(path, roots):
    try:
        resolved = Path(path).resolve()
    except (TypeError, ValueError):
        return False
    return any(resolved == root or resolved.is_relative_to(root) for root in roots)

def audit(event, args):
    if event == "open" and args and isinstance(args[0], (str, bytes, os.PathLike)):
        mode = args[1] if len(args) > 1 else "r"
        write_flags = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
        writable = (
            isinstance(mode, str) and any(flag in mode for flag in "wax+")
        ) or (
            any(isinstance(value, int) and bool(value & write_flags) for value in args[1:3])
        )
        roots = (output_root,) if writable else read_roots
        if not inside(args[0], roots):
            raise PermissionError("managed Python filesystem policy")
    if event == "import" and args and args[0] in {"ctypes", "_ctypes"}:
        raise PermissionError("managed Python native extension policy")
    if event in {
        "os.remove", "os.unlink", "os.truncate", "os.chmod", "os.chown",
        "os.rmdir", "os.mkdir",
    } and (not args or not inside(args[0], (output_root,))):
        raise PermissionError("managed Python filesystem mutation policy")
    if event in {"os.rename", "os.replace"} and (
        len(args) < 2
        or not inside(args[0], (output_root,))
        or not inside(args[1], (output_root,))
    ):
        raise PermissionError("managed Python filesystem mutation policy")
    if event.startswith(("subprocess.", "socket.", "ctypes.")) or event in {
        "os.system", "os.posix_spawn", "os.spawn", "os.fork", "os.exec",
        "os.symlink", "os.link", "os.chdir",
    }:
        raise PermissionError("managed Python process/network policy")

sys.addaudithook(audit)
runpy.run_path(str(script), run_name="__main__")
"""


__all__ = [
    "ArtifactPublisher",
    "FailureAdjustment",
    "ManagedPythonConflict",
    "ManagedPythonError",
    "ManagedPythonExecutor",
    "ManagedPythonProfile",
    "ManagedPythonProfileStore",
    "ManagedPythonRequest",
    "ManagedPythonResult",
    "NamedInputArtifact",
]
