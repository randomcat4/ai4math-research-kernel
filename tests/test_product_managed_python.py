from __future__ import annotations

import hashlib
import json
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest

from rk.product.artifact_read import ExactArtifactRef
from rk.product.compute import (
    AuthorityCeiling,
    ResourceRequest,
    ToolAvailability,
    ToolFunctionSpec,
    prepare_tool_invocation,
)
from rk.product.jobs import JobState, JobStore, RetrySafety
from rk.product.managed_python import (
    ManagedPythonConflict,
    ManagedPythonError,
    ManagedPythonExecutor,
    ManagedPythonProfileStore,
    ManagedPythonRequest,
    NamedInputArtifact,
)
from rk.product.operations import OperationStore
from rk.product.tool_runs import ToolCatalogStore, ToolRunStore, ValidationStatus
from rk.product_migrations import ProductMigrationAssembler, ProductMigrationRegistry
from rk.wire import canonical_json_bytes

NOW = "2026-08-13T00:00:00Z"


@dataclass(frozen=True)
class StreamResult:
    stream: tuple[bytes, ...]


class MemoryArtifacts:
    def __init__(self) -> None:
        self.values: dict[str, tuple[ExactArtifactRef, bytes]] = {}

    def add(self, artifact_id: str, data: bytes, media_type: str) -> ExactArtifactRef:
        ref = ExactArtifactRef(
            artifact_id=artifact_id,
            sha256=hashlib.sha256(data).hexdigest(),
            byte_count=len(data),
            media_type=media_type,
        )
        self.values[artifact_id] = (ref, data)
        return ref

    def open_range(self, artifact_id: str, *, expected_ref: ExactArtifactRef) -> StreamResult:
        ref, data = self.values[artifact_id]
        if ref != expected_ref:
            raise RuntimeError("artifact binding mismatch")
        return StreamResult((data,))


class MemoryPublisher:
    def __init__(self) -> None:
        self.values: dict[str, tuple[str, str, bytes]] = {}
        self._counter = 0

    def publish(self, *, data: bytes, logical_name: str, media_type: str) -> ExactArtifactRef:
        self._counter += 1
        artifact_id = f"generated-{self._counter}"
        self.values[artifact_id] = (logical_name, media_type, data)
        return ExactArtifactRef(
            artifact_id=artifact_id,
            sha256=hashlib.sha256(data).hexdigest(),
            byte_count=len(data),
            media_type=media_type,
        )


class ArgumentReader:
    def read_json(self, artifact_ref: ExactArtifactRef) -> dict[str, int]:
        return {"seed": 7}


class FailOnceToolRuns:
    def __init__(self, inner: ToolRunStore) -> None:
        self.inner = inner
        self.failed = False

    def __getattr__(self, name: str) -> Any:
        return getattr(self.inner, name)

    def record_receipt(self, *args: Any, **kwargs: Any) -> Any:
        if not self.failed:
            self.failed = True
            raise RuntimeError("simulated crash after B03 receipt")
        return self.inner.record_receipt(*args, **kwargs)


def migrated_db(tmp_path: Path) -> Path:
    db = tmp_path / "product.sqlite"
    with sqlite3.connect(db) as connection:
        ProductMigrationAssembler(ProductMigrationRegistry(Path("schema_fragments"))).apply(
            connection
        )
    return db


def lock_ref(
    artifacts: MemoryArtifacts,
    *,
    packages: dict[str, str] | None = None,
    artifact_id: str = "lock-1",
) -> ExactArtifactRef:
    value = {
        "schema_version": "rk.managed-python-lock.v1",
        "python": f"{sys.version_info.major}.{sys.version_info.minor}",
        "packages": packages or {},
    }
    return artifacts.add(
        artifact_id,
        json.dumps(value, sort_keys=True).encode(),
        "application/json",
    )


def tool_spec() -> ToolFunctionSpec:
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["seed"],
        "properties": {"seed": {"type": "integer"}},
    }
    return ToolFunctionSpec(
        tool_id="managed-python",
        tool_version="1",
        function_name="run_script",
        provider="rk",
        build_version="build-1",
        profile_id="managed_py",
        function_schema=schema,
        function_schema_digest=hashlib.sha256(canonical_json_bytes(schema)).hexdigest(),
        availability=ToolAvailability.AVAILABLE,
        authority_ceiling=AuthorityCeiling.SOFT_TOOL_RESULT,
    )


def create_running(
    db: Path,
    artifacts: MemoryArtifacts,
    *,
    wall_time_ms: int = 5_000,
) -> tuple[JobStore, ToolRunStore, Any]:
    body = {
        "schema_version": "rk.product.receipt.v1",
        "request_id": "request-1",
        "scope": {
            "kind": "RUN",
            "run_id": "run-1",
            "expected_revision": 3,
            "expected_contract_version": 1,
        },
        "updated_at": NOW,
        "state": "PENDING",
        "job_id": "job-1",
    }
    OperationStore(db, iter(["receipt-1"]).__next__).reserve(
        scope_key="RUN:run-1",
        request_id="request-1",
        request_digest="a" * 64,
        pending_receipt=body,
        now=NOW,
    )
    jobs = JobStore(db, iter(["lease-1", "execution-receipt-1"]).__next__)
    jobs.enqueue(
        job_id="job-1",
        receipt_id="receipt-1",
        scope_kind="RUN",
        run_id="run-1",
        deployment_id=None,
        kind="RUN_TOOL",
        requested_by="subject-1",
        request_id="request-1",
        retry_safety=RetrySafety.IDEMPOTENT,
        idempotency_key=None,
        now=NOW,
    )
    declaration = ToolCatalogStore(db).register(tool_spec(), now=NOW)
    arguments_ref = artifacts.add("arguments-1", b'{"seed":7}', "application/json")
    prepared = prepare_tool_invocation(
        spec=declaration,
        arguments_artifact=arguments_ref,
        input_artifact_ids=("input-1",),
        resources=ResourceRequest(
            cpu_millis=3_000,
            memory_bytes=512 * 1024 * 1024,
            wall_time_ms=wall_time_ms,
        ),
        authority_ceiling=AuthorityCeiling.SOFT_TOOL_RESULT,
        artifacts=ArgumentReader(),
    )
    runs = ToolRunStore(db, jobs)
    runs.create(
        tool_run_id="tool-run-1",
        run_id="run-1",
        research_revision=3,
        contract_version=1,
        request_id="request-1",
        requested_by="subject-1",
        invocation=prepared,
        attempt_id="attempt-1",
        job_id="job-1",
        now=NOW,
    )
    claimed = jobs.claim_next(
        holder_id="daemon-1",
        process_token="process-1",
        now="2026-08-13T00:00:01Z",
        expires_at="2026-08-13T00:01:00Z",
    )
    assert claimed is not None
    return jobs, runs, claimed[1]


def profile_store(db: Path, artifacts: MemoryArtifacts) -> ManagedPythonProfileStore:
    store = ManagedPythonProfileStore(db, artifacts)  # type: ignore[arg-type]
    store.register(
        profile_id="managed_py",
        interpreter=Path(sys.executable),
        lock_artifact=lock_ref(artifacts),
        authority_ceiling=AuthorityCeiling.SOFT_TOOL_RESULT,
        now=NOW,
    )
    return store


def executor(
    db: Path,
    tmp_path: Path,
    artifacts: MemoryArtifacts,
    publisher: MemoryPublisher,
    profiles: ManagedPythonProfileStore,
    jobs: JobStore,
    runs: Any,
) -> ManagedPythonExecutor:
    return ManagedPythonExecutor(
        db_path=db,
        workspace_root=tmp_path / "managed-work",
        artifacts=artifacts,  # type: ignore[arg-type]
        publisher=publisher,
        profiles=profiles,
        jobs=jobs,
        tool_runs=runs,
        clock=lambda: "2026-08-13T00:00:10Z",
        poll_seconds=0.01,
    )


def request(
    artifacts: MemoryArtifacts, script: str, *, execution_id: str = "execution-1"
) -> ManagedPythonRequest:
    script_ref = artifacts.add(f"script-{execution_id}", script.encode(), "text/x-python")
    input_ref = artifacts.add("input-1", b"2,3\n", "text/csv")
    return ManagedPythonRequest(
        execution_id=execution_id,
        tool_run_id="tool-run-1",
        attempt_id="attempt-1",
        profile_id="managed_py",
        script_artifact=script_ref,
        inputs=(NamedInputArtifact("data.csv", input_ref),),
    )


SUCCESS_SCRIPT = r"""import base64
import json
import os
from pathlib import Path

inputs = Path(os.environ["RK_INPUT_DIR"])
outputs = Path(os.environ["RK_OUTPUT_DIR"])
rows = (inputs / "data.csv").read_text().strip().split(",")
(outputs / "table.csv").write_text("value\n" + "\n".join(rows) + "\n")
(outputs / "plot.png").write_bytes(base64.b64decode(
    "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAQAAAC1HAwCAAAAC0lEQVR42mP8/x8AAusB9Wl2nH0AAAAASUVORK5CYII="
))
(outputs / "manifest.json").write_text(json.dumps({
    "schema_version": "rk.managed-python-output.v1",
    "public_summary": "generated one table and one image",
    "artifacts": [
        {"path": "table.csv", "kind": "TABLE", "media_type": "text/csv"},
        {"path": "plot.png", "kind": "IMAGE", "media_type": "image/png"},
    ],
}))
"""


def test_profile_lock_is_immutable_and_scientific_lock_is_honestly_unavailable(
    tmp_path: Path,
) -> None:
    db = migrated_db(tmp_path)
    artifacts = MemoryArtifacts()
    profiles = profile_store(db, artifacts)
    assert profiles.get("managed_py").availability == "AVAILABLE"
    with pytest.raises(ManagedPythonConflict):
        profiles.register(
            profile_id="managed_py",
            interpreter=Path(sys.executable),
            lock_artifact=lock_ref(artifacts, artifact_id="different-lock"),
            authority_ceiling=AuthorityCeiling.NO_FACT_GRAPH_WRITE,
            now=NOW,
        )
    scientific = {
        "numpy": "0",
        "scipy": "0",
        "sympy": "0",
        "networkx": "0",
        "pandas": "0",
        "matplotlib": "0",
    }
    unavailable = profiles.register(
        profile_id="scientific_py",
        interpreter=Path(sys.executable),
        lock_artifact=lock_ref(artifacts, packages=scientific, artifact_id="scientific-lock"),
        authority_ceiling=AuthorityCeiling.SOFT_TOOL_RESULT,
        now=NOW,
    )
    assert unavailable.packages == scientific
    assert unavailable.availability == "UNAVAILABLE"
    with pytest.raises(ManagedPythonError, match="certificate"):
        profiles.register(
            profile_id="invalid_cert",
            interpreter=Path(sys.executable),
            lock_artifact=lock_ref(artifacts, artifact_id="cert-lock"),
            authority_ceiling=AuthorityCeiling.CERTIFICATE_REQUIRES_VALIDATION,
            now=NOW,
        )


def test_real_process_publishes_table_image_log_and_only_soft_receipt(
    tmp_path: Path,
) -> None:
    db = migrated_db(tmp_path)
    artifacts = MemoryArtifacts()
    jobs, runs, lease = create_running(db, artifacts)
    profiles = profile_store(db, artifacts)
    publisher = MemoryPublisher()

    result = executor(db, tmp_path, artifacts, publisher, profiles, jobs, runs).execute(
        request(artifacts, SUCCESS_SCRIPT), lease
    )

    assert result.outcome.value == "SUCCEEDED"
    assert [item.media_type for item in result.output_artifacts] == [
        "text/csv",
        "image/png",
    ]
    assert result.resource_usage.wall_time_ms >= 0
    assert result.resource_usage.cpu_millis >= 0
    completed = runs.get("tool-run-1")
    assert completed.invocation_status == JobState.SUCCEEDED
    assert completed.validation_status == ValidationStatus.NOT_SUBMITTED
    assert completed.authority_ceiling == AuthorityCeiling.SOFT_TOOL_RESULT
    assert runs.attempts("tool-run-1")[0].authority_effect == "NONE"
    with sqlite3.connect(db) as connection:
        assert connection.execute(
            "SELECT runtime_state FROM product_managed_python_executions"
        ).fetchone() == ("RECEIPT_RECORDED",)


def test_script_cannot_write_host_path(tmp_path: Path) -> None:
    db = migrated_db(tmp_path)
    artifacts = MemoryArtifacts()
    jobs, runs, lease = create_running(db, artifacts)
    profiles = profile_store(db, artifacts)
    publisher = MemoryPublisher()
    escape = tmp_path / "escaped.txt"
    script = f"""import os
import subprocess
from pathlib import Path
Path({str(escape)!r}).write_text("escaped")
subprocess.run(["true"])
"""

    result = executor(db, tmp_path, artifacts, publisher, profiles, jobs, runs).execute(
        request(artifacts, script), lease
    )

    assert result.outcome.value == "FAILED"
    assert result.failure_adjustment is not None
    assert result.failure_adjustment.failure_code == "SCRIPT_FAILED"
    assert not escape.exists()
    log = publisher.values[result.public_log_artifact.artifact_id][2]
    assert b"managed Python filesystem policy" in log


def test_input_artifacts_are_read_only(tmp_path: Path) -> None:
    db = migrated_db(tmp_path)
    artifacts = MemoryArtifacts()
    jobs, runs, lease = create_running(db, artifacts)
    profiles = profile_store(db, artifacts)
    publisher = MemoryPublisher()
    script = """import os
from pathlib import Path
Path(os.environ["RK_INPUT_DIR"], "data.csv").write_text("changed")
"""

    result = executor(db, tmp_path, artifacts, publisher, profiles, jobs, runs).execute(
        request(artifacts, script), lease
    )

    assert result.outcome.value == "FAILED"
    log = publisher.values[result.public_log_artifact.artifact_id][2]
    assert b"managed Python filesystem policy" in log


def test_process_creation_is_denied_without_a_shell_seam(tmp_path: Path) -> None:
    db = migrated_db(tmp_path)
    artifacts = MemoryArtifacts()
    jobs, runs, lease = create_running(db, artifacts)
    profiles = profile_store(db, artifacts)
    publisher = MemoryPublisher()
    script = """import subprocess
subprocess.run(["python", "-c", "print('unmanaged')"], check=True)
"""

    result = executor(db, tmp_path, artifacts, publisher, profiles, jobs, runs).execute(
        request(artifacts, script), lease
    )

    assert result.outcome.value == "FAILED"
    log = publisher.values[result.public_log_artifact.artifact_id][2]
    assert b"managed Python process/network policy" in log


def test_wall_timeout_kills_process_and_returns_structured_adjustment(
    tmp_path: Path,
) -> None:
    db = migrated_db(tmp_path)
    artifacts = MemoryArtifacts()
    jobs, runs, lease = create_running(db, artifacts, wall_time_ms=150)
    profiles = profile_store(db, artifacts)
    publisher = MemoryPublisher()

    result = executor(db, tmp_path, artifacts, publisher, profiles, jobs, runs).execute(
        request(artifacts, "while True:\n    pass\n"), lease
    )

    assert result.outcome.value == "FAILED"
    assert result.failure_adjustment is not None
    assert result.failure_adjustment.failure_code == "WALL_TIME_LIMIT"
    assert result.failure_adjustment.retryable is True
    assert result.failure_adjustment.parameter_changes["wall_time_ms"] == 300
    assert runs.get("tool-run-1").invocation_status == JobState.FAILED
    with sqlite3.connect(db) as connection:
        encoded = connection.execute(
            "SELECT failure_adjustment_json FROM product_managed_python_executions"
        ).fetchone()[0]
    assert json.loads(encoded)["parameter_changes"]["wall_time_ms"] == 300


def test_cancel_requested_remains_until_real_process_acknowledges(tmp_path: Path) -> None:
    db = migrated_db(tmp_path)
    artifacts = MemoryArtifacts()
    jobs, runs, lease = create_running(db, artifacts)
    profiles = profile_store(db, artifacts)
    publisher = MemoryPublisher()
    service = executor(db, tmp_path, artifacts, publisher, profiles, jobs, runs)
    received: list[Any] = []

    def run() -> None:
        try:
            received.append(service.execute(request(artifacts, "while True:\n    pass\n"), lease))
        except BaseException as error:
            received.append(error)

    thread = threading.Thread(target=run)
    thread.start()
    deadline = time.monotonic() + 5
    while time.monotonic() < deadline:
        with sqlite3.connect(db) as connection:
            row = connection.execute(
                "SELECT runtime_state FROM product_managed_python_executions"
            ).fetchone()
        if row == ("RUNNING",):
            break
        time.sleep(0.01)
    else:
        raise AssertionError("managed process did not start")

    requested = runs.request_cancel("tool-run-1", now="2026-08-13T00:00:05Z")
    assert requested.invocation_status == JobState.CANCEL_REQUESTED
    thread.join(timeout=5)
    assert not thread.is_alive()
    assert len(received) == 1 and not isinstance(received[0], BaseException)
    assert received[0].outcome.value == "CANCELLED"
    assert runs.get("tool-run-1").invocation_status == JobState.CANCELLED


def test_restart_completes_staged_s00_receipt_after_b03_commit(tmp_path: Path) -> None:
    db = migrated_db(tmp_path)
    artifacts = MemoryArtifacts()
    jobs, runs, lease = create_running(db, artifacts)
    profiles = profile_store(db, artifacts)
    publisher = MemoryPublisher()
    failing = FailOnceToolRuns(runs)
    first = executor(db, tmp_path, artifacts, publisher, profiles, jobs, failing)

    with pytest.raises(RuntimeError, match="simulated crash"):
        first.execute(request(artifacts, SUCCESS_SCRIPT), lease)
    assert jobs.get("job-1").state == JobState.SUCCEEDED
    assert runs.get("tool-run-1").invocation_status == JobState.QUEUED
    with sqlite3.connect(db) as connection:
        assert connection.execute(
            "SELECT runtime_state,pending_tool_receipt_json IS NOT NULL "
            "FROM product_managed_python_executions"
        ).fetchone() == ("RUNNING", 1)

    restarted = executor(db, tmp_path, artifacts, publisher, profiles, jobs, runs)
    assert restarted.recover_receipts(now="2026-08-13T00:00:20Z") == ("execution-1",)
    assert runs.get("tool-run-1").invocation_status == JobState.SUCCEEDED
    with sqlite3.connect(db) as connection:
        assert connection.execute(
            "SELECT runtime_state FROM product_managed_python_executions"
        ).fetchone() == ("RECEIPT_RECORDED",)


def test_execution_api_has_no_command_shell_or_host_path_fields() -> None:
    assert set(ManagedPythonRequest.__dataclass_fields__) == {
        "execution_id",
        "tool_run_id",
        "attempt_id",
        "profile_id",
        "script_artifact",
        "inputs",
    }
