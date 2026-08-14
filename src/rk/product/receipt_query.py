"""Read-only PRODUCT_RECEIPT and JOB query projections."""

from __future__ import annotations

from types import MappingProxyType
from typing import Any

from rk.product.api import JsonObject, QueryResult, QuerySpec
from rk.product.jobs import DurableJob, JobStore
from rk.product.operations import OperationStore


class ProductObjectNotFound(KeyError):
    """A requested receipt or job does not exist."""


class ProductQueryScopeMismatch(PermissionError):
    """The requested object is outside the explicit query scope."""


class ReceiptJobQuery:
    """Project the two operation-tracking query variants without a second state store."""

    def __init__(self, operations: OperationStore, jobs: JobStore) -> None:
        self._operations = operations
        self._jobs = jobs

    def execute(self, spec: QuerySpec) -> QueryResult:
        if spec.query_type == "PRODUCT_RECEIPT":
            receipt_id = _only_id(spec.payload, "receipt_id")
            return self.product_receipt(spec.scope, receipt_id)
        if spec.query_type == "JOB":
            job_id = _only_id(spec.payload, "job_id")
            return self.job(spec.scope, job_id)
        raise ValueError(f"unsupported receipt/job query type: {spec.query_type}")

    def product_receipt(self, scope: JsonObject, receipt_id: str) -> QueryResult:
        try:
            receipt = self._operations.get(receipt_id)
        except KeyError as error:
            raise ProductObjectNotFound(receipt_id) from error
        if receipt.scope_key != _scope_key(scope):
            raise ProductQueryScopeMismatch(receipt_id)
        return QueryResult(
            result_type="PRODUCT_RECEIPT",
            stable_entity_id=receipt.receipt_id,
            fence=MappingProxyType(
                {
                    "receipt_version": receipt.receipt_version,
                    "updated_at": receipt.updated_at,
                }
            ),
            data=MappingProxyType(dict(receipt.value)),
        )

    def job(self, scope: JsonObject, job_id: str) -> QueryResult:
        try:
            job = self._jobs.get(job_id)
        except KeyError as error:
            raise ProductObjectNotFound(job_id) from error
        if _job_scope_key(job) != _scope_key(scope):
            raise ProductQueryScopeMismatch(job_id)
        data: dict[str, Any] = {
            "schema_version": "rk.product.job.v1",
            "job_id": job.job_id,
            "receipt_id": job.receipt_id,
            "scope": dict(scope),
            "kind": job.kind,
            "requested_by": job.requested_by,
            "request_id": job.request_id,
            "state": job.state.value,
            "retry_safety": job.retry_safety.value,
            "lease_generation": job.lease_generation,
            "worker_run_ids": list(job.worker_run_ids),
            "result_refs": [dict(item) for item in job.result_refs],
            "authority_effect": job.authority_effect,
            "created_at": job.created_at,
        }
        _optional(data, "idempotency_key", job.idempotency_key)
        _optional(data, "current_checkpoint_id", job.current_checkpoint_id)
        _optional(data, "failure_code", job.failure_code)
        _optional(data, "started_at", job.started_at)
        _optional(data, "finished_at", job.finished_at)
        return QueryResult(
            result_type="JOB",
            stable_entity_id=job.job_id,
            fence=MappingProxyType(
                {
                    "lease_generation": job.lease_generation,
                    "state": job.state.value,
                }
            ),
            data=MappingProxyType(data),
        )


def _only_id(payload: JsonObject, name: str) -> str:
    if set(payload) != {name}:
        raise ValueError(f"query payload must contain only {name}")
    value = payload[name]
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be a non-empty string")
    return value


def _scope_key(scope: JsonObject) -> str:
    kind = scope.get("kind")
    if kind == "RUN" and set(scope) == {"kind", "run_id"}:
        value = scope["run_id"]
    elif kind in {"GLOBAL", "DEPLOYMENT"} and set(scope) == {"kind", "deployment_id"}:
        value = scope["deployment_id"]
    else:
        raise ValueError("query scope is not an exact product scope")
    if not isinstance(value, str) or not value:
        raise ValueError("query scope id must be a non-empty string")
    return f"{kind}:{value}"


def _job_scope_key(job: DurableJob) -> str:
    if job.scope_kind == "RUN" and job.run_id is not None and job.deployment_id is None:
        return f"RUN:{job.run_id}"
    if (
        job.scope_kind in {"GLOBAL", "DEPLOYMENT"}
        and job.run_id is None
        and job.deployment_id is not None
    ):
        return f"{job.scope_kind}:{job.deployment_id}"
    raise RuntimeError("persisted job has an invalid scope")


def _optional(target: dict[str, Any], name: str, value: str | None) -> None:
    if value is not None:
        target[name] = value


__all__ = [
    "ProductObjectNotFound",
    "ProductQueryScopeMismatch",
    "ReceiptJobQuery",
]