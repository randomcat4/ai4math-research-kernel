"""Durable command coordination behind ``ResearchProduct.command``."""

from __future__ import annotations

import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol

from rk.product.api import (
    JsonObject,
    ProductCommand,
    ProductDecision,
    ProductReceipt,
    ProductSession,
    RunScope,
)
from rk.product.operations import OperationStore, StoredReceipt


class ExecutionClass(StrEnum):
    SYNCHRONOUS_AUTHORITY = "SYNCHRONOUS_AUTHORITY"
    DURABLE_JOB = "DURABLE_JOB"


class AuthorityPort(Protocol):
    def apply(self, session: ProductSession, request: ProductCommand) -> ProductDecision: ...


class JobQueuePort(Protocol):
    def enqueue_in_transaction(
        self,
        connection: sqlite3.Connection,
        *,
        job_id: str,
        session: ProductSession,
        request: ProductCommand,
        receipt_id: str,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class CommandPlan:
    execution_class: ExecutionClass
    job_type: str | None = None

    def __post_init__(self) -> None:
        if self.execution_class is ExecutionClass.DURABLE_JOB and not self.job_type:
            raise ValueError("durable command plan requires job_type")
        if self.execution_class is ExecutionClass.SYNCHRONOUS_AUTHORITY and self.job_type:
            raise ValueError("synchronous command plan cannot declare job_type")


class ProductCommandService:
    """Reserve idempotency before authority work or durable job submission."""

    def __init__(
        self,
        *,
        operations: OperationStore,
        authority: AuthorityPort,
        jobs: JobQueuePort,
        plans: Mapping[str, CommandPlan],
        id_generator: Callable[[], str],
        clock: Callable[[], str],
    ) -> None:
        self._operations = operations
        self._authority = authority
        self._jobs = jobs
        self._plans = dict(plans)
        self._ids = id_generator
        self._clock = clock

    def execute(self, session: ProductSession, request: ProductCommand) -> ProductReceipt:
        try:
            plan = self._plans[request.command_type]
        except KeyError as error:
            raise ValueError(f"command has no execution plan: {request.command_type}") from error
        now = self._clock()
        job_id = self._ids()
        request_value = _request_value(request)
        pending = {
            "schema_version": "rk.product.receipt.v1",
            "request_id": request.request_id,
            "scope": _scope_value(request),
            "updated_at": now,
            "state": "PENDING",
            "job_id": job_id,
        }
        after_insert: Callable[[sqlite3.Connection, str], None] | None = None
        if plan.execution_class is ExecutionClass.DURABLE_JOB:

            def bind_job(connection: sqlite3.Connection, receipt_id: str) -> None:
                self._jobs.enqueue_in_transaction(
                    connection,
                    job_id=job_id,
                    session=session,
                    request=request,
                    receipt_id=receipt_id,
                )

            after_insert = bind_job
        reservation = self._operations.reserve(
            scope_key=_scope_key(request),
            request_id=request.request_id,
            request_digest=self._operations.request_digest(request_value),
            pending_receipt=pending,
            now=now,
            after_insert=after_insert,
        )
        if not reservation.created:
            return _receipt(reservation.receipt, request)
        if plan.execution_class is ExecutionClass.DURABLE_JOB:
            return _receipt(reservation.receipt, request)

        decision = self._authority.apply(session, request)
        decided_at = self._clock()
        decided = {
            "schema_version": "rk.product.receipt.v1",
            "request_id": request.request_id,
            "scope": _scope_value(request),
            "updated_at": decided_at,
            "state": "DECIDED",
            "decision": _decision_value(decision),
            "decided_at": decided_at,
        }
        stored = self._operations.decide(
            reservation.receipt.receipt_id,
            decision_receipt=decided,
            now=decided_at,
        )
        return _receipt(stored, request)


def _scope_key(request: ProductCommand) -> str:
    if isinstance(request.scope, RunScope):
        return f"RUN:{request.scope.run_id}"
    return f"{request.scope.kind}:{request.scope.deployment_id}"


def _scope_value(request: ProductCommand) -> dict[str, object]:
    scope = request.scope
    if isinstance(scope, RunScope):
        return {
            "kind": scope.kind,
            "run_id": scope.run_id,
            "expected_revision": scope.expected_revision,
            "expected_contract_version": scope.expected_contract_version,
        }
    return {
        "kind": scope.kind,
        "deployment_id": scope.deployment_id,
        "expected_deployment_revision": scope.expected_deployment_revision,
    }


def _request_value(request: ProductCommand) -> dict[str, object]:
    return {
        "schema_version": "rk.product.command.v1",
        "request_id": request.request_id,
        "scope": _scope_value(request),
        "command": {"type": request.command_type, "payload": dict(request.payload)},
    }


def _decision_value(decision: ProductDecision) -> dict[str, object]:
    return {
        "accepted": decision.accepted,
        "rejection_code": decision.rejection_code,
        "missing_conditions": [dict(item) for item in decision.missing_conditions],
        "revision_before": decision.revision_before,
        "revision_after": decision.revision_after,
        "contract_version": decision.contract_version,
        "event_cursor_after": decision.event_cursor_after,
        "affected_entity_ids": list(decision.affected_entity_ids),
        "created_artifact_refs": [dict(item) for item in decision.created_artifact_refs],
        "created_run_id": decision.created_run_id,
        "kernel_receipts": [dict(item) for item in decision.kernel_receipts],
        "available_actions": [dict(item) for item in decision.available_actions],
    }


def _receipt(stored: StoredReceipt, request: ProductCommand) -> ProductReceipt:
    value = stored.value
    raw_decision = value.get("decision")
    decision: ProductDecision | None = None
    if isinstance(raw_decision, Mapping):
        decision = ProductDecision(
            accepted=bool(raw_decision["accepted"]),
            rejection_code=_optional_string(raw_decision.get("rejection_code")),
            revision_before=int(raw_decision["revision_before"]),
            revision_after=int(raw_decision["revision_after"]),
            contract_version=int(raw_decision["contract_version"]),
            event_cursor_after=int(raw_decision["event_cursor_after"]),
            missing_conditions=_objects(raw_decision.get("missing_conditions", [])),
            affected_entity_ids=_strings(raw_decision.get("affected_entity_ids", [])),
            created_artifact_refs=_objects(raw_decision.get("created_artifact_refs", [])),
            created_run_id=_optional_string(raw_decision.get("created_run_id")),
            kernel_receipts=_objects(raw_decision.get("kernel_receipts", [])),
            available_actions=_objects(raw_decision.get("available_actions", [])),
        )
    return ProductReceipt(
        receipt_id=stored.receipt_id,
        receipt_version=stored.receipt_version,
        request_id=stored.request_id,
        scope=request.scope,
        state=stored.state,
        updated_at=stored.updated_at,
        decision=decision,
        job_id=_optional_string(value.get("job_id")),
        unknown_external_call_ref=_optional_string(value.get("unknown_external_call_ref")),
        supersedes_or_resolves_receipt_id=_optional_string(
            value.get("supersedes_or_resolves_receipt_id")
        ),
        decided_at=_optional_string(value.get("decided_at")),
    )


def _optional_string(value: object) -> str | None:
    return None if value is None else str(value)


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, list):
        raise ValueError("stored receipt string list is invalid")
    return tuple(str(item) for item in value)


def _objects(value: object) -> tuple[JsonObject, ...]:
    if not isinstance(value, list) or any(not isinstance(item, dict) for item in value):
        raise ValueError("stored receipt object list is invalid")
    return tuple(item for item in value if isinstance(item, dict))
