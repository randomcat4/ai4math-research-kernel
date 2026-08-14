"""Production recovery of managed Python requests from durable product state."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any, cast
from uuid import NAMESPACE_URL, uuid5

from rk.product.artifact_read import ArtifactReadService, ExactArtifactRef
from rk.product.compute import (
    AuthorityCeiling,
    B04bArtifactJsonReader,
    ResourceRequest,
    ToolContractError,
    prepare_tool_invocation,
)
from rk.product.jobs import DurableJob, JobLease, JobStore
from rk.product.managed_python import (
    ManagedPythonProfileStore,
    ManagedPythonRequest,
    NamedInputArtifact,
)
from rk.product.tool_runs import ToolCatalogStore, ToolRunStore


class ManagedBindingRejected(ValueError):
    """A durable managed request names unavailable or invalid deployed capability."""

    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


class ActiveLeaseResolver:
    """Resolve only the exact active lease generation held by the running job."""

    def __init__(self, db_path: Path) -> None:
        self._db_path = Path(db_path)

    def __call__(self, job: DurableJob) -> JobLease:
        with sqlite3.connect(self._db_path) as connection:
            row = connection.execute(
                "SELECT lease_id,job_id,lease_generation,holder_id,process_token,"
                "claimed_at,heartbeat_at,expires_at FROM product_job_leases "
                "WHERE job_id=? AND lease_generation=? AND state='ACTIVE'",
                (job.job_id, job.lease_generation),
            ).fetchone()
        if row is None:
            raise ValueError("running durable job has no exact active lease")
        return JobLease(
            lease_id=str(row[0]),
            job_id=str(row[1]),
            lease_generation=int(row[2]),
            holder_id=str(row[3]),
            process_token=str(row[4]),
            claimed_at=str(row[5]),
            heartbeat_at=str(row[6]),
            expires_at=str(row[7]),
        )


class PersistentManagedRequestResolver:
    """Build one managed request from B03b request, B12 catalog/profile, and lease."""

    def __init__(
        self,
        *,
        jobs: JobStore,
        catalog: ToolCatalogStore,
        tool_runs: ToolRunStore,
        profiles: ManagedPythonProfileStore,
        artifacts: ArtifactReadService,
        clock: Callable[[], str],
    ) -> None:
        self._jobs = jobs
        self._catalog = catalog
        self._tool_runs = tool_runs
        self._profiles = profiles
        self._artifacts = artifacts
        self._json = B04bArtifactJsonReader(artifacts)
        self._clock = clock

    def __call__(
        self,
        job: DurableJob,
        supplied_request: Mapping[str, object],
        lease: JobLease,
    ) -> ManagedPythonRequest:
        stored = self._jobs.request(job.job_id).value
        if dict(stored) != dict(supplied_request):
            raise ValueError("executor request differs from immutable B03b request")
        command = _object(stored, "command")
        if _text(command, "type") != job.kind or job.kind not in {
            "CREATE_COMPUTE_TASK",
            "RUN_TOOL",
        }:
            raise ValueError("durable managed command differs from job kind")
        payload = _object(command, "payload")
        scope = _object(stored, "scope")
        if _text(scope, "run_id") != job.run_id:
            raise ValueError("durable managed request run fence differs from job")
        try:
            if job.kind == "CREATE_COMPUTE_TASK":
                resolved = self._create_compute(payload)
            else:
                resolved = self._run_tool(payload)
        except KeyError as error:
            raise ManagedBindingRejected("MANAGED_CATALOG_OR_PROFILE_NOT_DEPLOYED") from error
        except ToolContractError as error:
            raise ManagedBindingRejected("MANAGED_INVOCATION_CONTRACT_REJECTED") from error
        invocation, script, inputs = resolved
        tool_run_id = _stable_id("tool-run", job.job_id)
        attempt_id = _stable_id("attempt", job.job_id)
        self._tool_runs.create_for_active_lease(
            tool_run_id=tool_run_id,
            run_id=_text(scope, "run_id"),
            research_revision=_integer(scope, "expected_revision"),
            contract_version=_integer(scope, "expected_contract_version"),
            request_id=job.request_id,
            requested_by=job.requested_by,
            invocation=invocation,
            attempt_id=attempt_id,
            job_kind=job.kind,
            lease=lease,
            now=self._clock(),
        )
        return ManagedPythonRequest(
            execution_id=_stable_id("managed-execution", job.job_id),
            tool_run_id=tool_run_id,
            attempt_id=attempt_id,
            profile_id=invocation.spec.profile_id,
            script_artifact=script,
            inputs=inputs,
        )

    def _create_compute(
        self, payload: Mapping[str, object]
    ) -> tuple[Any, ExactArtifactRef, tuple[NamedInputArtifact, ...]]:
        profile_id = _text(payload, "environment_profile_id")
        version = _text(payload, "environment_profile_version")
        spec = self._catalog.get("managed-python", version, "run_script")
        profile = self._profiles.get(profile_id)
        if spec.profile_id != profile.profile_id or spec.build_version != version:
            raise ManagedBindingRejected("MANAGED_PROFILE_VERSION_MISMATCH")
        script = self._binding(_object(payload, "script_artifact"))
        arguments = self._binding(_object(payload, "parameters_artifact"))
        limits = self._binding(_object(payload, "limits_artifact"))
        resources = _resources(self._json.read_json(limits))
        refs = tuple(self._binding(item) for item in _objects(payload, "input_artifacts"))
        invocation = prepare_tool_invocation(
            spec=spec,
            arguments_artifact=arguments,
            input_artifact_ids=tuple(item.artifact_id for item in refs),
            resources=resources,
            authority_ceiling=AuthorityCeiling.SOFT_TOOL_RESULT,
            artifacts=self._json,
        )
        inputs = (
            *(NamedInputArtifact(f"input-{index:04d}", ref) for index, ref in enumerate(refs, 1)),
            NamedInputArtifact("parameters.json", arguments),
        )
        return invocation, script, inputs

    def _run_tool(
        self, payload: Mapping[str, object]
    ) -> tuple[Any, ExactArtifactRef, tuple[NamedInputArtifact, ...]]:
        spec = self._catalog.get(
            _text(payload, "tool_id"),
            _text(payload, "tool_version"),
            _text(payload, "function_name"),
        )
        if spec.function_schema_digest != _text(payload, "function_schema_digest"):
            raise ManagedBindingRejected("TOOL_SCHEMA_DIGEST_MISMATCH")
        self._profiles.get(spec.profile_id)
        arguments = self._binding(_object(payload, "arguments_artifact"))
        authority = AuthorityCeiling(_text(payload, "authority_ceiling"))
        ids = _strings(payload, "input_artifact_ids")
        refs = tuple(self._artifacts.describe(item).ref for item in ids)
        invocation = prepare_tool_invocation(
            spec=spec,
            arguments_artifact=arguments,
            input_artifact_ids=ids,
            resources=_managed_resources(self._json.read_json(arguments)),
            authority_ceiling=authority,
            artifacts=self._json,
        )
        script = self._binding(
            _object(_object(invocation.arguments, "managed_python"), "script_artifact")
        )
        inputs = (
            *(NamedInputArtifact(f"input-{index:04d}", ref) for index, ref in enumerate(refs, 1)),
            NamedInputArtifact("arguments.json", arguments),
        )
        return invocation, script, inputs

    def _binding(self, value: Mapping[str, object]) -> ExactArtifactRef:
        artifact_id = _text(value, "artifact_id")
        ref = self._artifacts.describe(artifact_id).ref
        if ref.sha256 != _text(value, "sha256"):
            raise ManagedBindingRejected("ARTIFACT_BINDING_MISMATCH")
        return ref


def _managed_resources(arguments: Mapping[str, Any]) -> ResourceRequest:
    managed = _object(arguments, "managed_python")
    if set(managed) != {"script_artifact", "resources"}:
        raise ManagedBindingRejected("MANAGED_TOOL_BINDING_FIELDS_INVALID")
    return _resources(_object(managed, "resources"))


def _resources(value: Mapping[str, Any]) -> ResourceRequest:
    if set(value) != {"cpu_millis", "memory_bytes", "wall_time_ms", "gpu_count"}:
        raise ManagedBindingRejected("MANAGED_RESOURCE_FIELDS_INVALID")
    return ResourceRequest(
        cpu_millis=_integer(value, "cpu_millis"),
        memory_bytes=_integer(value, "memory_bytes"),
        wall_time_ms=_integer(value, "wall_time_ms"),
        gpu_count=_integer(value, "gpu_count"),
    )


def _stable_id(kind: str, job_id: str) -> str:
    return str(uuid5(NAMESPACE_URL, f"rk-product:{kind}:{job_id}"))


def _object(value: Mapping[str, Any], name: str) -> Mapping[str, Any]:
    item = value.get(name)
    if not isinstance(item, Mapping):
        raise ManagedBindingRejected(f"{name.upper()}_OBJECT_REQUIRED")
    return cast(Mapping[str, Any], item)


def _objects(value: Mapping[str, Any], name: str) -> tuple[Mapping[str, Any], ...]:
    item = value.get(name)
    if not isinstance(item, list) or any(not isinstance(entry, Mapping) for entry in item):
        raise ManagedBindingRejected(f"{name.upper()}_ARRAY_REQUIRED")
    return tuple(cast(Mapping[str, Any], entry) for entry in item)


def _strings(value: Mapping[str, Any], name: str) -> tuple[str, ...]:
    item = value.get(name)
    if not isinstance(item, list) or any(not isinstance(entry, str) or not entry for entry in item):
        raise ManagedBindingRejected(f"{name.upper()}_ARRAY_REQUIRED")
    return tuple(item)


def _text(value: Mapping[str, Any], name: str) -> str:
    item = value.get(name)
    if not isinstance(item, str) or not item:
        raise ManagedBindingRejected(f"{name.upper()}_TEXT_REQUIRED")
    return item


def _integer(value: Mapping[str, Any], name: str) -> int:
    item = value.get(name)
    if not isinstance(item, int) or isinstance(item, bool):
        raise ManagedBindingRejected(f"{name.upper()}_INTEGER_REQUIRED")
    return item


__all__ = ["ActiveLeaseResolver", "ManagedBindingRejected", "PersistentManagedRequestResolver"]
