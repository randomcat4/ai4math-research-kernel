"""Durable tool catalog, runs, attempts, and public receipts."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from rk.extensions import ToolReceipt
from rk.product.artifact_read import ExactArtifactRef
from rk.product.compute import (
    AuthorityCeiling,
    PreparedToolInvocation,
    ResourceUsage,
    ToolAvailability,
    ToolFunctionSpec,
)
from rk.product.jobs import JobLease, JobState, JobStore
from rk.wire import canonical_json_bytes


class ToolRunError(RuntimeError):
    """A persisted tool invariant or state transition was violated."""


class ToolCatalogConflict(ToolRunError):
    """A stable tool key was rebound to different immutable metadata."""


class ToolRunConflict(ToolRunError):
    """A stable run, attempt, or receipt identity was rebound."""


class ValidationStatus(StrEnum):
    NOT_SUBMITTED = "NOT_SUBMITTED"
    VALIDATION_ACCEPTED = "VALIDATION_ACCEPTED"
    VALIDATION_REJECTED = "VALIDATION_REJECTED"
    STALE = "STALE"


_TERMINAL = frozenset(
    {
        JobState.CANCELLED,
        JobState.SUCCEEDED,
        JobState.FAILED,
        JobState.OUTCOME_UNKNOWN,
        JobState.STALE,
        JobState.INVALIDATED,
    }
)
_PUBLIC_FIELDS = frozenset(
    {
        "exit_code",
        "resource_usage",
        "public_log_artifact_id",
        "failure_code",
        "public_summary",
    }
)


@dataclass(frozen=True, slots=True)
class ToolAttempt:
    attempt_id: str
    tool_run_id: str
    attempt_ordinal: int
    job_id: str
    status: JobState
    resources: Mapping[str, int]
    resource_usage: ResourceUsage | None
    public_log_artifact_id: str | None
    output_artifact_ids: tuple[str, ...]
    public_summary: str | None
    exit_code: int | None
    failure_code: str | None
    authority_effect: str
    created_at: str
    started_at: str | None
    finished_at: str | None


@dataclass(frozen=True, slots=True)
class ToolRun:
    tool_run_id: str
    run_id: str
    research_revision: int
    contract_version: int
    request_id: str
    requested_by: str
    tool_key: tuple[str, str, str]
    function_schema_digest: str
    arguments_artifact: ExactArtifactRef
    input_artifact_ids: tuple[str, ...]
    resources: Mapping[str, int]
    authority_ceiling: AuthorityCeiling
    invocation_digest: str
    invocation_status: JobState
    validation_status: ValidationStatus
    validation_receipt_id: str | None
    current_attempt_id: str
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class ToolRunComparisonRow:
    tool_run_id: str
    invocation_status: JobState
    validation_status: ValidationStatus
    attempt_count: int
    latest_resource_usage: ResourceUsage | None
    output_artifact_ids: tuple[str, ...]


@dataclass(frozen=True, slots=True)
class ToolRunComparison:
    same_tool_function: bool
    same_invocation: bool
    rows: tuple[ToolRunComparisonRow, ...]


class ToolCatalogStore:
    """Immutable function declarations with explicit availability changes."""

    def __init__(self, db_path: Path, *, busy_timeout_ms: int = 5_000) -> None:
        self._db_path = Path(db_path)
        self._busy_timeout_ms = busy_timeout_ms

    def register(self, spec: ToolFunctionSpec, *, now: str) -> ToolFunctionSpec:
        schema_json = _json(dict(spec.function_schema))
        immutable = (
            spec.provider,
            spec.build_version,
            spec.profile_id,
            schema_json,
            spec.function_schema_digest,
            str(spec.authority_ceiling),
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT provider,build_version,profile_id,function_schema_json,"
                "function_schema_digest,authority_ceiling FROM product_tool_catalog "
                "WHERE tool_id=? AND tool_version=? AND function_name=?",
                spec.key,
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO product_tool_catalog("
                    "tool_id,tool_version,function_name,provider,build_version,profile_id,"
                    "function_schema_json,function_schema_digest,availability,"
                    "authority_ceiling,registered_at,status_updated_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
                    (*spec.key, *immutable[:5], str(spec.availability), immutable[5], now, now),
                )
            elif tuple(str(value) for value in row) != immutable:
                raise ToolCatalogConflict("stable tool function key has different metadata")
            connection.commit()
        return self.get(*spec.key)

    def get(self, tool_id: str, tool_version: str, function_name: str) -> ToolFunctionSpec:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT tool_id,tool_version,function_name,provider,build_version,profile_id,"
                "function_schema_json,function_schema_digest,availability,authority_ceiling "
                "FROM product_tool_catalog WHERE tool_id=? AND tool_version=? AND function_name=?",
                (tool_id, tool_version, function_name),
            ).fetchone()
        if row is None:
            raise KeyError((tool_id, tool_version, function_name))
        return _spec(row)

    def list(
        self, *, after: tuple[str, str, str] | None = None, limit: int = 50
    ) -> tuple[ToolFunctionSpec, ...]:
        if limit < 1 or limit > 200:
            raise ValueError("catalog page limit must be between 1 and 200")
        where = ""
        params: tuple[object, ...] = ()
        if after is not None:
            where = " WHERE (tool_id,tool_version,function_name) > (?,?,?)"
            params = after
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT tool_id,tool_version,function_name,provider,build_version,profile_id,"
                "function_schema_json,function_schema_digest,availability,authority_ceiling "
                "FROM product_tool_catalog"
                + where
                + " ORDER BY tool_id,tool_version,function_name LIMIT ?",
                (*params, limit),
            ).fetchall()
        return tuple(_spec(row) for row in rows)

    def set_availability(
        self,
        key: tuple[str, str, str],
        availability: ToolAvailability,
        *,
        now: str,
    ) -> ToolFunctionSpec:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            result = connection.execute(
                "UPDATE product_tool_catalog SET availability=?,status_updated_at=? "
                "WHERE tool_id=? AND tool_version=? AND function_name=?",
                (str(availability), now, *key),
            )
            if result.rowcount != 1:
                raise KeyError(key)
            connection.commit()
        return self.get(*key)

    def _connect(self) -> sqlite3.Connection:
        return _connect(self._db_path, self._busy_timeout_ms)


class ToolRunStore:
    """Persist tool invocations without granting graph-write authority."""

    def __init__(self, db_path: Path, jobs: JobStore, *, busy_timeout_ms: int = 5_000) -> None:
        self._db_path = Path(db_path)
        self._jobs = jobs
        self._busy_timeout_ms = busy_timeout_ms

    def create(
        self,
        *,
        tool_run_id: str,
        run_id: str,
        research_revision: int,
        contract_version: int,
        request_id: str,
        requested_by: str,
        invocation: PreparedToolInvocation,
        attempt_id: str,
        job_id: str,
        now: str,
    ) -> ToolRun:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self.create_in_transaction(
                connection,
                tool_run_id=tool_run_id,
                run_id=run_id,
                research_revision=research_revision,
                contract_version=contract_version,
                request_id=request_id,
                requested_by=requested_by,
                invocation=invocation,
                attempt_id=attempt_id,
                job_id=job_id,
                now=now,
            )
            connection.commit()
        return self.get(tool_run_id)

    def create_for_active_lease(
        self,
        *,
        tool_run_id: str,
        run_id: str,
        research_revision: int,
        contract_version: int,
        request_id: str,
        requested_by: str,
        invocation: PreparedToolInvocation,
        attempt_id: str,
        job_kind: str,
        lease: JobLease,
        now: str,
    ) -> ToolRun:
        """Idempotently bind an invocation recovered after the B03 claim boundary."""

        if job_kind not in {"CREATE_COMPUTE_TASK", "RUN_TOOL"}:
            raise ToolRunError("managed attempt job kind is unsupported")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            active = connection.execute(
                "SELECT holder_id,process_token,expires_at FROM product_job_leases "
                "WHERE lease_id=? AND job_id=? AND lease_generation=? AND state='ACTIVE'",
                (lease.lease_id, lease.job_id, lease.lease_generation),
            ).fetchone()
            if active is None or tuple(str(value) for value in active) != (
                lease.holder_id,
                lease.process_token,
                lease.expires_at,
            ):
                raise ToolRunError("managed attempt requires the exact active B03 lease")
            self._create_bound_in_transaction(
                connection,
                tool_run_id=tool_run_id,
                run_id=run_id,
                research_revision=research_revision,
                contract_version=contract_version,
                request_id=request_id,
                requested_by=requested_by,
                invocation=invocation,
                attempt_id=attempt_id,
                job_id=lease.job_id,
                expected_job_kind=job_kind,
                expected_job_state="RUNNING",
                now=now,
            )
            connection.commit()
        return self.get(tool_run_id)

    def create_in_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        tool_run_id: str,
        run_id: str,
        research_revision: int,
        contract_version: int,
        request_id: str,
        requested_by: str,
        invocation: PreparedToolInvocation,
        attempt_id: str,
        job_id: str,
        now: str,
    ) -> None:
        if not connection.in_transaction:
            raise ToolRunError("atomic tool-run creation requires an active transaction")
        self._create_bound_in_transaction(
            connection,
            tool_run_id=tool_run_id,
            run_id=run_id,
            research_revision=research_revision,
            contract_version=contract_version,
            request_id=request_id,
            requested_by=requested_by,
            invocation=invocation,
            attempt_id=attempt_id,
            job_id=job_id,
            expected_job_kind="RUN_TOOL",
            expected_job_state="QUEUED",
            now=now,
        )

    def _create_bound_in_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        tool_run_id: str,
        run_id: str,
        research_revision: int,
        contract_version: int,
        request_id: str,
        requested_by: str,
        invocation: PreparedToolInvocation,
        attempt_id: str,
        job_id: str,
        expected_job_kind: str,
        expected_job_state: str,
        now: str,
    ) -> None:
        if research_revision < 0 or contract_version < 1:
            raise ToolRunError("invalid revision or contract binding")
        spec = invocation.spec
        catalog = connection.execute(
            "SELECT function_schema_digest,availability,authority_ceiling "
            "FROM product_tool_catalog WHERE tool_id=? AND tool_version=? AND function_name=?",
            spec.key,
        ).fetchone()
        if catalog is None:
            raise ToolRunError("tool function is not registered")
        if (str(catalog[0]), str(catalog[2])) != (
            spec.function_schema_digest,
            str(spec.authority_ceiling),
        ):
            raise ToolRunConflict("prepared invocation does not match persisted catalog")
        if ToolAvailability(str(catalog[1])) not in {
            ToolAvailability.AVAILABLE,
            ToolAvailability.PRODUCT_RECEIPT_AVAILABLE,
        }:
            raise ToolRunError("tool function is not available for product execution")
        job = connection.execute(
            "SELECT scope_kind,run_id,kind,requested_by,request_id,state "
            "FROM product_jobs WHERE job_id=?",
            (job_id,),
        ).fetchone()
        expected_job = (
            "RUN",
            run_id,
            expected_job_kind,
            requested_by,
            request_id,
            expected_job_state,
        )
        if job is None or tuple(str(value) for value in job) != expected_job:
            raise ToolRunError("tool attempt does not match its exact B03 job fence")
        digest = _invocation_digest(
            run_id, research_revision, contract_version, requested_by, invocation
        )
        existing = connection.execute(
            "SELECT tool_run_id,invocation_digest,current_attempt_id "
            "FROM product_tool_runs WHERE run_id=? AND request_id=?",
            (run_id, request_id),
        ).fetchone()
        if existing is not None:
            if tuple(str(value) for value in existing) != (
                tool_run_id,
                digest,
                attempt_id,
            ):
                raise ToolRunConflict("run request is already bound to another invocation")
            return
        ref = invocation.arguments_artifact
        resources_json = _json(invocation.resources.to_dict())
        connection.execute(
            "INSERT INTO product_tool_runs("
            "tool_run_id,run_id,research_revision,contract_version,request_id,requested_by,"
            "tool_id,tool_version,function_name,function_schema_digest,"
            "arguments_artifact_id,arguments_artifact_sha256,arguments_artifact_byte_count,"
            "arguments_artifact_media_type,input_artifact_ids_json,resource_request_json,"
            "authority_ceiling,invocation_digest,invocation_status,current_attempt_id,"
            "created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'QUEUED',NULL,?,?)",
            (
                tool_run_id,
                run_id,
                research_revision,
                contract_version,
                request_id,
                requested_by,
                *spec.key,
                spec.function_schema_digest,
                ref.artifact_id,
                ref.sha256,
                ref.byte_count,
                ref.media_type,
                _json(list(invocation.input_artifact_ids)),
                resources_json,
                str(invocation.authority_ceiling),
                digest,
                now,
                now,
            ),
        )
        connection.execute(
            "INSERT INTO product_tool_attempts("
            "attempt_id,tool_run_id,attempt_ordinal,job_id,status,"
            "resource_request_json,created_at) VALUES(?,?,1,?,'QUEUED',?,?)",
            (attempt_id, tool_run_id, job_id, resources_json, now),
        )
        connection.execute(
            "UPDATE product_tool_runs SET current_attempt_id=? WHERE tool_run_id=?",
            (attempt_id, tool_run_id),
        )

    def get(self, tool_run_id: str) -> ToolRun:
        with self._connect() as connection:
            row = connection.execute(
                _RUN_SELECT + " WHERE tool_run_id=?", (tool_run_id,)
            ).fetchone()
        if row is None:
            raise KeyError(tool_run_id)
        return _run(row)

    def attempts(self, tool_run_id: str) -> tuple[ToolAttempt, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                _ATTEMPT_SELECT + " WHERE tool_run_id=? ORDER BY attempt_ordinal",
                (tool_run_id,),
            ).fetchall()
        return tuple(_attempt(row) for row in rows)

    def reconcile(self, tool_run_id: str, *, now: str) -> ToolRun:
        run = self.get(tool_run_id)
        attempt = self._current_attempt(run)
        job = self._jobs.get(attempt.job_id)
        state = job.state
        if state == JobState.SUCCEEDED and attempt.resource_usage is None:
            state = JobState.WAITING
        finished_at = now if state in _TERMINAL else None
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE product_tool_attempts SET status=?,started_at=?,finished_at=? "
                "WHERE attempt_id=?",
                (str(state), job.started_at, finished_at, attempt.attempt_id),
            )
            connection.execute(
                "UPDATE product_tool_runs SET invocation_status=?,updated_at=? WHERE tool_run_id=?",
                (str(state), now, tool_run_id),
            )
            connection.commit()
        return self.get(tool_run_id)

    def request_cancel(self, tool_run_id: str, *, now: str) -> ToolRun:
        run = self.get(tool_run_id)
        attempt = self._current_attempt(run)
        if attempt.status in _TERMINAL:
            raise ToolRunError("terminal tool attempt cannot be cancelled")
        self._jobs.request_cancel(attempt.job_id, now=now)
        return self.reconcile(tool_run_id, now=now)

    def rerun(self, tool_run_id: str, *, attempt_id: str, job_id: str, now: str) -> ToolRun:
        run = self.get(tool_run_id)
        current = self._current_attempt(run)
        if current.status not in _TERMINAL:
            raise ToolRunError("rerun requires a terminal current attempt")
        job = self._jobs.get(job_id)
        if (
            job.scope_kind != "RUN"
            or job.run_id != run.run_id
            or job.kind != "RUN_TOOL"
            or job.requested_by != run.requested_by
            or job.state != JobState.QUEUED
        ):
            raise ToolRunError("rerun requires an exact queued B03 RUN_TOOL job")
        ordinal = current.attempt_ordinal + 1
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "INSERT INTO product_tool_attempts("
                "attempt_id,tool_run_id,attempt_ordinal,job_id,status,"
                "resource_request_json,created_at) VALUES(?,?,?,?,'QUEUED',?,?)",
                (attempt_id, tool_run_id, ordinal, job_id, _json(dict(run.resources)), now),
            )
            connection.execute(
                "UPDATE product_tool_runs SET current_attempt_id=?,"
                "invocation_status='QUEUED',validation_status='NOT_SUBMITTED',"
                "validation_receipt_id=NULL,updated_at=? WHERE tool_run_id=?",
                (attempt_id, now, tool_run_id),
            )
            connection.commit()
        return self.get(tool_run_id)

    def record_receipt(self, receipt: ToolReceipt, *, now: str) -> ToolRun:
        if receipt.status not in {"SUCCEEDED", "FAILED", "CANCELLED", "OUTCOME_UNKNOWN"}:
            raise ToolRunError("tool receipt must be a terminal execution outcome")
        if set(receipt.payload) != _PUBLIC_FIELDS:
            raise ToolRunError("tool receipt payload must use the exact public field set")
        if len(set(receipt.artifact_ids)) != len(receipt.artifact_ids):
            raise ToolRunError("output artifact IDs must be unique")
        run = self.get(receipt.tool_run_id)
        if run.current_attempt_id != receipt.attempt_id:
            raise ToolRunConflict("receipt does not bind the current attempt")
        attempt = self._current_attempt(run)
        job = self._jobs.get(attempt.job_id)
        if str(job.state) != receipt.status:
            raise ToolRunError("S00 receipt status does not match the B03 process receipt")
        usage_value = receipt.payload["resource_usage"]
        if not isinstance(usage_value, Mapping):
            raise ToolRunError("resource_usage must be an object")
        usage = ResourceUsage.from_mapping(usage_value)
        exit_code = _optional_int(receipt.payload["exit_code"], "exit_code")
        public_log = _optional_str(
            receipt.payload["public_log_artifact_id"], "public_log_artifact_id"
        )
        failure_code = _optional_str(receipt.payload["failure_code"], "failure_code")
        summary = _optional_str(receipt.payload["public_summary"], "public_summary")
        if public_log is not None and public_log not in receipt.artifact_ids:
            raise ToolRunError("public log must be one of the declared public artifacts")
        if receipt.status == "SUCCEEDED" and exit_code not in {None, 0}:
            raise ToolRunError("successful tool receipt cannot have non-zero exit code")
        if receipt.status == "FAILED" and failure_code is None:
            raise ToolRunError("failed tool receipt requires a public failure code")
        stored = (
            _json(usage.to_dict()),
            public_log,
            _json(list(receipt.artifact_ids)),
            summary,
            exit_code,
            failure_code,
        )
        if attempt.resource_usage is not None:
            prior = (
                _json(attempt.resource_usage.to_dict()),
                attempt.public_log_artifact_id,
                _json(list(attempt.output_artifact_ids)),
                attempt.public_summary,
                attempt.exit_code,
                attempt.failure_code,
            )
            if attempt.status != JobState(receipt.status) or prior != stored:
                raise ToolRunConflict("terminal tool receipt replay differs")
            return run
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                "SELECT current_attempt_id FROM product_tool_runs WHERE tool_run_id=?",
                (receipt.tool_run_id,),
            ).fetchone()
            if current is None or current[0] != receipt.attempt_id:
                raise ToolRunConflict("current attempt changed before receipt commit")
            connection.execute(
                "UPDATE product_tool_attempts SET status=?,resource_usage_json=?,"
                "public_log_artifact_id=?,output_artifact_ids_json=?,public_summary=?,"
                "exit_code=?,failure_code=?,authority_effect='NONE',finished_at=? "
                "WHERE attempt_id=?",
                (receipt.status, *stored, now, receipt.attempt_id),
            )
            connection.execute(
                "UPDATE product_tool_runs SET invocation_status=?,updated_at=? WHERE tool_run_id=?",
                (receipt.status, now, receipt.tool_run_id),
            )
            if receipt.status == "SUCCEEDED":
                connection.execute(
                    "UPDATE product_tool_catalog SET availability=?,status_updated_at=? "
                    "WHERE tool_id=? AND tool_version=? AND function_name=?",
                    (ToolAvailability.PRODUCT_RECEIPT_AVAILABLE, now, *run.tool_key),
                )
            connection.commit()
        return self.get(receipt.tool_run_id)

    def record_validation(
        self,
        tool_run_id: str,
        *,
        status: ValidationStatus,
        validation_receipt_id: str,
        now: str,
    ) -> ToolRun:
        if status not in {
            ValidationStatus.VALIDATION_ACCEPTED,
            ValidationStatus.VALIDATION_REJECTED,
        }:
            raise ToolRunError("validation recording requires an explicit verdict")
        if not validation_receipt_id or validation_receipt_id != validation_receipt_id.strip():
            raise ToolRunError("validation receipt ID must be stable and non-empty")
        run = self.get(tool_run_id)
        if run.validation_status != ValidationStatus.NOT_SUBMITTED:
            if (
                run.validation_status == status
                and run.validation_receipt_id == validation_receipt_id
            ):
                return run
            raise ToolRunConflict("validation verdict is already recorded")
        if run.invocation_status != JobState.SUCCEEDED:
            raise ToolRunError("validation requires a succeeded invocation receipt")
        if (
            status == ValidationStatus.VALIDATION_ACCEPTED
            and run.authority_ceiling != AuthorityCeiling.CERTIFICATE_REQUIRES_VALIDATION
        ):
            raise ToolRunError("this tool ceiling cannot produce a validated certificate")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            result = connection.execute(
                "UPDATE product_tool_runs SET validation_status=?,validation_receipt_id=?,"
                "updated_at=? WHERE tool_run_id=? AND validation_status='NOT_SUBMITTED'",
                (str(status), validation_receipt_id, now, tool_run_id),
            )
            if result.rowcount != 1:
                raise ToolRunConflict("validation state changed before commit")
            connection.commit()
        return self.get(tool_run_id)

    def compare(self, tool_run_ids: tuple[str, ...]) -> ToolRunComparison:
        if len(tool_run_ids) < 2 or len(set(tool_run_ids)) != len(tool_run_ids):
            raise ToolRunError("comparison requires at least two distinct tool runs")
        runs = tuple(self.get(tool_run_id) for tool_run_id in tool_run_ids)
        rows = []
        for run in runs:
            attempts = self.attempts(run.tool_run_id)
            latest = attempts[-1]
            rows.append(
                ToolRunComparisonRow(
                    tool_run_id=run.tool_run_id,
                    invocation_status=run.invocation_status,
                    validation_status=run.validation_status,
                    attempt_count=len(attempts),
                    latest_resource_usage=latest.resource_usage,
                    output_artifact_ids=latest.output_artifact_ids,
                )
            )
        return ToolRunComparison(
            same_tool_function=len({run.tool_key for run in runs}) == 1,
            same_invocation=len({run.invocation_digest for run in runs}) == 1,
            rows=tuple(rows),
        )

    def _current_attempt(self, run: ToolRun) -> ToolAttempt:
        attempts = self.attempts(run.tool_run_id)
        for attempt in reversed(attempts):
            if attempt.attempt_id == run.current_attempt_id:
                return attempt
        raise ToolRunError("current attempt is missing")

    def _connect(self) -> sqlite3.Connection:
        return _connect(self._db_path, self._busy_timeout_ms)


class ToolReceiptAdapter:
    """S00 consumer that persists only the public B12a receipt projection."""

    def __init__(self, store: ToolRunStore, clock: Callable[[], str]) -> None:
        self._store = store
        self._clock = clock

    def __call__(self, receipt: ToolReceipt) -> None:
        self._store.record_receipt(receipt, now=self._clock())


_RUN_SELECT = (
    "SELECT tool_run_id,run_id,research_revision,contract_version,request_id,requested_by,"
    "tool_id,tool_version,function_name,function_schema_digest,arguments_artifact_id,"
    "arguments_artifact_sha256,arguments_artifact_byte_count,arguments_artifact_media_type,"
    "input_artifact_ids_json,resource_request_json,authority_ceiling,invocation_digest,"
    "invocation_status,validation_status,validation_receipt_id,current_attempt_id,"
    "created_at,updated_at FROM product_tool_runs"
)
_ATTEMPT_SELECT = (
    "SELECT attempt_id,tool_run_id,attempt_ordinal,job_id,status,resource_request_json,"
    "resource_usage_json,public_log_artifact_id,output_artifact_ids_json,public_summary,"
    "exit_code,failure_code,authority_effect,created_at,started_at,finished_at "
    "FROM product_tool_attempts"
)


def _run(row: sqlite3.Row | tuple[Any, ...]) -> ToolRun:
    return ToolRun(
        tool_run_id=str(row[0]),
        run_id=str(row[1]),
        research_revision=int(row[2]),
        contract_version=int(row[3]),
        request_id=str(row[4]),
        requested_by=str(row[5]),
        tool_key=(str(row[6]), str(row[7]), str(row[8])),
        function_schema_digest=str(row[9]),
        arguments_artifact=ExactArtifactRef(
            artifact_id=str(row[10]),
            sha256=str(row[11]),
            byte_count=int(row[12]),
            media_type=str(row[13]),
        ),
        input_artifact_ids=_str_tuple(row[14]),
        resources=_int_mapping(row[15]),
        authority_ceiling=AuthorityCeiling(str(row[16])),
        invocation_digest=str(row[17]),
        invocation_status=JobState(str(row[18])),
        validation_status=ValidationStatus(str(row[19])),
        validation_receipt_id=str(row[20]) if row[20] is not None else None,
        current_attempt_id=str(row[21]),
        created_at=str(row[22]),
        updated_at=str(row[23]),
    )


def _attempt(row: sqlite3.Row | tuple[Any, ...]) -> ToolAttempt:
    usage_value = _mapping(row[6]) if row[6] is not None else None
    return ToolAttempt(
        attempt_id=str(row[0]),
        tool_run_id=str(row[1]),
        attempt_ordinal=int(row[2]),
        job_id=str(row[3]),
        status=JobState(str(row[4])),
        resources=_int_mapping(row[5]),
        resource_usage=(
            ResourceUsage.from_mapping(usage_value) if usage_value is not None else None
        ),
        public_log_artifact_id=str(row[7]) if row[7] is not None else None,
        output_artifact_ids=_str_tuple(row[8]),
        public_summary=str(row[9]) if row[9] is not None else None,
        exit_code=int(row[10]) if row[10] is not None else None,
        failure_code=str(row[11]) if row[11] is not None else None,
        authority_effect=str(row[12]),
        created_at=str(row[13]),
        started_at=str(row[14]) if row[14] is not None else None,
        finished_at=str(row[15]) if row[15] is not None else None,
    )


def _spec(row: sqlite3.Row | tuple[Any, ...]) -> ToolFunctionSpec:
    return ToolFunctionSpec(
        tool_id=str(row[0]),
        tool_version=str(row[1]),
        function_name=str(row[2]),
        provider=str(row[3]),
        build_version=str(row[4]),
        profile_id=str(row[5]),
        function_schema=_mapping(row[6]),
        function_schema_digest=str(row[7]),
        availability=ToolAvailability(str(row[8])),
        authority_ceiling=AuthorityCeiling(str(row[9])),
    )


def _invocation_digest(
    run_id: str,
    revision: int,
    contract_version: int,
    requested_by: str,
    invocation: PreparedToolInvocation,
) -> str:
    ref = invocation.arguments_artifact
    value = {
        "run_id": run_id,
        "research_revision": revision,
        "contract_version": contract_version,
        "requested_by": requested_by,
        "tool_key": list(invocation.spec.key),
        "function_schema_digest": invocation.spec.function_schema_digest,
        "arguments_artifact": {
            "artifact_id": ref.artifact_id,
            "sha256": ref.sha256,
            "byte_count": ref.byte_count,
            "media_type": ref.media_type,
        },
        "input_artifact_ids": list(invocation.input_artifact_ids),
        "resources": invocation.resources.to_dict(),
        "authority_ceiling": str(invocation.authority_ceiling),
    }
    return hashlib.sha256(canonical_json_bytes(value)).hexdigest()


def _mapping(value: object) -> Mapping[str, Any]:
    decoded = json.loads(str(value))
    if not isinstance(decoded, dict):
        raise ToolRunError("persisted JSON object is invalid")
    return decoded


def _int_mapping(value: object) -> Mapping[str, int]:
    decoded = _mapping(value)
    if any(not isinstance(item, int) or isinstance(item, bool) for item in decoded.values()):
        raise ToolRunError("persisted resource object is invalid")
    return {str(key): int(item) for key, item in decoded.items()}


def _str_tuple(value: object) -> tuple[str, ...]:
    decoded = json.loads(str(value))
    if not isinstance(decoded, list) or any(not isinstance(item, str) for item in decoded):
        raise ToolRunError("persisted artifact ID array is invalid")
    return tuple(decoded)


def _optional_str(value: object, label: str) -> str | None:
    if value is None:
        return None
    if not isinstance(value, str) or not value or value != value.strip():
        raise ToolRunError(f"{label} must be null or a non-empty trimmed string")
    return value


def _optional_int(value: object, label: str) -> int | None:
    if value is None:
        return None
    if not isinstance(value, int) or isinstance(value, bool):
        raise ToolRunError(f"{label} must be null or an integer")
    return value


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _connect(path: Path, busy_timeout_ms: int) -> sqlite3.Connection:
    connection = sqlite3.connect(path, isolation_level=None)
    connection.execute(f"PRAGMA busy_timeout={busy_timeout_ms}")
    connection.execute("PRAGMA foreign_keys=ON")
    return connection


__all__ = [
    "ToolAttempt",
    "ToolCatalogConflict",
    "ToolCatalogStore",
    "ToolReceiptAdapter",
    "ToolRun",
    "ToolRunComparison",
    "ToolRunComparisonRow",
    "ToolRunConflict",
    "ToolRunError",
    "ToolRunStore",
    "ValidationStatus",
]
