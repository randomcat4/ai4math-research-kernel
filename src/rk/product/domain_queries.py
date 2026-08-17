"""Strict QueryResult projections over existing product stores and authoritative tables."""

from __future__ import annotations

import base64
import hmac
import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import asdict, is_dataclass
from enum import Enum
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol, cast

from rk.product.api import JsonObject, QueryResult, QuerySpec
from rk.product.problem_pool import ProblemPoolStore
from rk.product.research_lineage import ResearchLineageStore
from rk.product.route_plan import RoutePlanStore
from rk.product.tool_runs import ToolCatalogStore, ToolRunStore
from rk.product.work_activity import WorkActivityStore


class DomainObjectNotFound(KeyError):
    """The requested persisted domain object does not exist in the requested scope."""


class DomainQueryScopeMismatch(PermissionError):
    """A persisted object belongs to another run or deployment."""


class DomainQueryStale(RuntimeError):
    """The requested read fence is no longer current."""

    code = "STALE_QUERY"


class RunFence(Protocol):
    run_id: str
    research_revision: int
    contract_version: int
    last_cursor: int


class FenceSource(Protocol):
    def run(self, run_id: str) -> RunFence: ...


class DomainQueries:
    """Adapt mature domain stores to the generic query contract without a second truth store."""

    _RUN_TYPES = frozenset(
        {
            "ROUTE_PLAN",
            "WORKFLOW",
            "WORK_ITEM",
            "WORKER_RUN",
            "COMPUTE_TASK",
            "TOOL_RUN",
            "DOSSIER",
            "PUBLICATION_STATUS",
            "RESEARCH_CASE_LINEAGE",
            "CLEAN_ROOM_INPUT_MANIFEST",
            "CERTIFICATE_IMPORT_REPORT",
        }
    )
    _GLOBAL_TYPES = frozenset({"PROBLEM_POOL", "PROBLEM_CANDIDATE", "BATCH_RESEARCH_JOB"})

    def __init__(
        self,
        *,
        db_path: Path,
        deployment_id: str,
        fences: FenceSource,
        route_plans: RoutePlanStore,
        work: WorkActivityStore,
        tool_catalog: ToolCatalogStore,
        tool_runs: ToolRunStore,
        problem_pools: ProblemPoolStore,
        lineages: ResearchLineageStore,
        cursor_secret: bytes,
        busy_timeout_ms: int = 5_000,
    ) -> None:
        if not deployment_id:
            raise ValueError("deployment_id must be non-empty")
        self._db_path = Path(db_path)
        self._deployment_id = deployment_id
        self._fences = fences
        self._route_plans = route_plans
        self._work = work
        self._tool_catalog = tool_catalog
        self._tool_runs = tool_runs
        self._problem_pools = problem_pools
        self._lineages = lineages
        self._tool_cursors = _ToolCursorCodec(cursor_secret, deployment_id)
        self._busy_timeout_ms = busy_timeout_ms

    def supports(self, query_type: str) -> bool:
        return (
            query_type in self._RUN_TYPES
            or query_type in self._GLOBAL_TYPES
            or query_type == "TOOL_CATALOG"
        )

    def execute(self, spec: QuerySpec) -> QueryResult:
        handlers = {
            "ROUTE_PLAN": self._route_plan,
            "WORKFLOW": self._workflow,
            "WORK_ITEM": self._work_item,
            "WORKER_RUN": self._worker_run,
            "COMPUTE_TASK": self._compute_task,
            "TOOL_CATALOG": self._tool_catalog_query,
            "TOOL_RUN": self._tool_run,
            "DOSSIER": self._dossier,
            "PUBLICATION_STATUS": self._publication_status,
            "PROBLEM_POOL": self._problem_pool,
            "PROBLEM_CANDIDATE": self._problem_candidate,
            "BATCH_RESEARCH_JOB": self._batch_job,
            "RESEARCH_CASE_LINEAGE": self._lineage,
            "CLEAN_ROOM_INPUT_MANIFEST": self._manifest,
            "CERTIFICATE_IMPORT_REPORT": self._certificate_report,
        }
        handler = handlers.get(spec.query_type)
        if handler is None:
            raise ValueError(f"unsupported domain query: {spec.query_type}")
        return handler(spec)

    def _route_plan(self, spec: QuerySpec) -> QueryResult:
        run_id, fence = self._run_scope(spec)
        entity_id = _only_id(spec.payload, "route_plan_id")
        plan = _get(self._route_plans.get, entity_id)
        if plan.run_id != run_id:
            raise DomainQueryScopeMismatch(entity_id)
        return self._run_result(spec.query_type, entity_id, fence, _value(plan))

    def _workflow(self, spec: QuerySpec) -> QueryResult:
        run_id, fence = self._run_scope(spec)
        _empty_payload(spec.payload)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT work_item_id FROM product_work_items WHERE run_id=? "
                "ORDER BY created_at,work_item_id",
                (run_id,),
            ).fetchall()
        if not rows:
            raise DomainObjectNotFound(f"workflow:{run_id}")
        items = [_value(_get(self._work.get_work_item, str(row[0]))) for row in rows]
        states: dict[str, int] = {}
        for item in items:
            state = cast(str, item["aggregate_state"])
            states[state] = states.get(state, 0) + 1
        return self._run_result(
            spec.query_type,
            f"workflow:{run_id}",
            fence,
            {"run_id": run_id, "work_items": items, "state_counts": states},
        )

    def _work_item(self, spec: QuerySpec) -> QueryResult:
        run_id, fence = self._run_scope(spec)
        entity_id = _id_with_optional_page(spec.payload, "work_item_id")
        item = _get(self._work.get_work_item, entity_id)
        if item.run_id != run_id:
            raise DomainQueryScopeMismatch(entity_id)
        return self._run_result(spec.query_type, entity_id, fence, _value(item))

    def _worker_run(self, spec: QuerySpec) -> QueryResult:
        run_id, fence = self._run_scope(spec)
        entity_id = _id_with_optional_page(spec.payload, "worker_run_id")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT wi.work_item_id,wi.run_id FROM product_worker_runs wr "
                "JOIN product_work_items wi ON wi.work_item_id=wr.work_item_id "
                "WHERE wr.worker_run_id=?",
                (entity_id,),
            ).fetchone()
        if row is None:
            raise DomainObjectNotFound(entity_id)
        if str(row[1]) != run_id:
            raise DomainQueryScopeMismatch(entity_id)
        item = _get(self._work.get_work_item, str(row[0]))
        worker = next(
            (candidate for candidate in item.worker_runs if candidate.worker_run_id == entity_id),
            None,
        )
        if worker is None:
            raise DomainObjectNotFound(entity_id)
        return self._run_result(spec.query_type, entity_id, fence, _value(worker))

    def _compute_task(self, spec: QuerySpec) -> QueryResult:
        self._run_scope(spec)
        entity_id = _only_id(spec.payload, "compute_task_id")
        # No compute-task authority table/store exists yet.
        # A Job or ToolRun is not silently relabelled.
        raise DomainObjectNotFound(entity_id)

    def _tool_catalog_query(self, spec: QuerySpec) -> QueryResult:
        deployment_id = self._deployment_scope(spec)
        limit, cursor = _page(spec.payload)
        after = self._tool_cursors.decode(cursor) if cursor is not None else None
        items = self._tool_catalog.list(after=after, limit=limit)
        if not items:
            raise DomainObjectNotFound("tool-catalog")
        data = [_value(item) for item in items]
        last = items[-1].key
        return QueryResult(
            spec.query_type,
            f"tool-catalog:{deployment_id}",
            _frozen({"scope_kind": "DEPLOYMENT", "deployment_id": deployment_id}),
            _frozen(
                {
                    "items": data,
                    "page": {
                        "returned": len(data),
                        "next_cursor": self._tool_cursors.encode(last)
                        if len(data) == limit
                        else None,
                    },
                }
            ),
        )

    def _tool_run(self, spec: QuerySpec) -> QueryResult:
        run_id, fence = self._run_scope(spec)
        entity_id = _only_id(spec.payload, "tool_run_id")
        run = _get(self._tool_runs.get, entity_id)
        if run.run_id != run_id:
            raise DomainQueryScopeMismatch(entity_id)
        data = _value(run)
        data["attempts"] = [_value(item) for item in self._tool_runs.attempts(entity_id)]
        return self._run_result(spec.query_type, entity_id, fence, data)

    def _dossier(self, spec: QuerySpec) -> QueryResult:
        run_id, fence = self._run_scope(spec)
        entity_id = _only_id(spec.payload, "dossier_id")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT dossier_request_id,run_id,observed_revision,observed_status,"
                "snapshot_digest,dossier_artifact_id,dossier_sha256,created_at "
                "FROM product_dossier_artifacts "
                "WHERE dossier_request_id=?",
                (entity_id,),
            ).fetchone()
        if row is None:
            raise DomainObjectNotFound(entity_id)
        if str(row[1]) != run_id:
            raise DomainQueryScopeMismatch(entity_id)
        return self._run_result(spec.query_type, entity_id, fence, _row(row))

    def _publication_status(self, spec: QuerySpec) -> QueryResult:
        run_id, fence = self._run_scope(spec)
        _empty_payload(spec.payload)
        with self._connect() as connection:
            finalization = _one_dict(
                connection, "product_publication_finalizations", "run_id", run_id
            )
            candidate = _latest(
                connection, "product_publication_candidates", run_id, "publication_revision"
            )
            review = _latest(
                connection, "product_publication_reviews", run_id, "publication_revision"
            )
            compilation = _latest(
                connection, "product_publication_compilations", run_id, "publication_revision"
            )
            attempts = _all_dicts(
                connection,
                "product_compilation_attempts",
                "run_id",
                run_id,
                "created_at,compilation_attempt_id",
            )
        if (
            finalization is None
            and candidate is None
            and review is None
            and compilation is None
            and not attempts
        ):
            raise DomainObjectNotFound(f"publication:{run_id}")
        return self._run_result(
            spec.query_type,
            f"publication:{run_id}",
            fence,
            {
                "run_id": run_id,
                "finalization": finalization,
                "candidate": candidate,
                "paper_review": review,
                "compilation": compilation,
                "compilation_attempts": attempts,
            },
        )

    def _problem_pool(self, spec: QuerySpec) -> QueryResult:
        deployment_id = self._global_scope(spec)
        entity_id = _only_id(spec.payload, "problem_pool_id")
        pool = _get(self._problem_pools.get, entity_id)
        if pool.deployment_id != deployment_id:
            raise DomainQueryScopeMismatch(entity_id)
        data = _value(pool)
        bindings = {
            binding.binding_kind: binding
            for binding in self._problem_pools.artifact_bindings(entity_id)
        }
        if "SEMANTIC_AUDIT" in bindings:
            data["semantic_audit_artifact"] = _value(bindings["SEMANTIC_AUDIT"].artifact)
        if "CONTRACT_TEMPLATE" in bindings:
            data["contract_template_artifact"] = _value(
                bindings["CONTRACT_TEMPLATE"].artifact
            )
        data["denominator"] = _value(self._problem_pools.denominator(entity_id))
        data["candidates"] = [_value(item) for item in self._problem_pools.candidates(entity_id)]
        data["authority_effect"] = "NO_FACT"
        return self._deployment_result(spec.query_type, entity_id, deployment_id, data)

    def _problem_candidate(self, spec: QuerySpec) -> QueryResult:
        deployment_id = self._global_scope(spec)
        entity_id = _only_id(spec.payload, "problem_candidate_id")
        candidate = _get(self._problem_pools.get_candidate, entity_id)
        pool = _get(self._problem_pools.get, candidate.problem_pool_id)
        if pool.deployment_id != deployment_id:
            raise DomainQueryScopeMismatch(entity_id)
        return self._deployment_result(spec.query_type, entity_id, deployment_id, _value(candidate))

    def _batch_job(self, spec: QuerySpec) -> QueryResult:
        deployment_id = self._global_scope(spec)
        entity_id = _only_id(spec.payload, "batch_job_id")
        with self._connect() as connection:
            command = _one_dict(connection, "product_problem_batch_commands", "batch_id", entity_id)
            runs = _all_dicts(
                connection,
                "product_problem_batch_runs",
                "batch_id",
                entity_id,
                "problem_candidate_id",
            )
        if command is None:
            raise DomainObjectNotFound(entity_id)
        if command.get("deployment_id") != deployment_id:
            raise DomainQueryScopeMismatch(entity_id)
        command["runs"] = runs
        return self._deployment_result(spec.query_type, entity_id, deployment_id, command)

    def _lineage(self, spec: QuerySpec) -> QueryResult:
        run_id, fence = self._run_scope(spec)
        entity_id = _only_id(spec.payload, "lineage_id")
        lineage = _get(self._lineages.get, entity_id)
        if lineage.run_id != run_id:
            raise DomainQueryScopeMismatch(entity_id)
        data = _value(lineage)
        with self._connect() as connection:
            data["candidate_bindings"] = _all_dicts(
                connection,
                "product_research_lineage_candidates",
                "lineage_id",
                entity_id,
                "claim_id",
            )
            data["outcome"] = _one_dict(
                connection, "product_research_lineage_outcomes", "lineage_id", entity_id
            )
        return self._run_result(spec.query_type, entity_id, fence, data)

    def _manifest(self, spec: QuerySpec) -> QueryResult:
        run_id, fence = self._run_scope(spec)
        entity_id = _only_id(spec.payload, "manifest_id")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT lineage_id,run_id,contract_version,frozen_tree_digest,"
                "data_root_id,input_manifest_artifact_id,input_manifest_sha256,"
                "input_manifest_json,candidate_authority "
                "FROM product_research_case_lineages WHERE input_manifest_artifact_id=?",
                (entity_id,),
            ).fetchone()
            inputs = (
                _all_dicts(
                    connection,
                    "product_research_lineage_inputs",
                    "lineage_id",
                    str(row[0]),
                    "ordinal",
                )
                if row is not None
                else []
            )
        if row is None:
            raise DomainObjectNotFound(entity_id)
        if str(row[1]) != run_id:
            raise DomainQueryScopeMismatch(entity_id)
        data = _row(row)
        data["input_manifest"] = _json(data.pop("input_manifest_json"))
        data["inputs"] = inputs
        return self._run_result(spec.query_type, entity_id, fence, data)

    def _certificate_report(self, spec: QuerySpec) -> QueryResult:
        run_id, fence = self._run_scope(spec)
        entity_id = _only_id(spec.payload, "report_id")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT r.lineage_id,l.run_id,r.report_artifact_id,r.report_sha256,"
                "r.report_json,r.created_at "
                "FROM product_research_lineage_reports r JOIN product_research_case_lineages l "
                "ON l.lineage_id=r.lineage_id WHERE r.report_artifact_id=?",
                (entity_id,),
            ).fetchone()
            receipts = (
                _all_dicts(
                    connection,
                    "product_research_certificate_verifications",
                    "lineage_id",
                    str(row[0]),
                    "certificate_artifact_id",
                )
                if row is not None
                else []
            )
        if row is None:
            raise DomainObjectNotFound(entity_id)
        if str(row[1]) != run_id:
            raise DomainQueryScopeMismatch(entity_id)
        data = _row(row)
        data["report"] = _json(data.pop("report_json"))
        for receipt in receipts:
            receipt["verifier_receipt"] = _json(receipt.pop("verifier_receipt_json"))
        data["certificate_verifications"] = receipts
        data["authority_effect"] = "VERIFICATION_EVIDENCE_ONLY"
        return self._run_result(spec.query_type, entity_id, fence, data)

    def _run_scope(self, spec: QuerySpec) -> tuple[str, RunFence]:
        if spec.scope.get("kind") != "RUN" or not isinstance(spec.scope.get("run_id"), str):
            raise DomainQueryScopeMismatch("RUN scope required")
        run_id = cast(str, spec.scope["run_id"])
        fence = self._fences.run(run_id)
        expected_revision = spec.scope.get("at_revision")
        expected_contract = spec.scope.get("at_contract_version")
        if expected_revision is not None and expected_revision != fence.research_revision:
            raise DomainQueryStale("research revision changed")
        if expected_contract is not None and expected_contract != fence.contract_version:
            raise DomainQueryStale("contract version changed")
        return run_id, fence

    def _global_scope(self, spec: QuerySpec) -> str:
        if (
            spec.scope.get("kind") != "GLOBAL"
            or spec.scope.get("deployment_id") != self._deployment_id
        ):
            raise DomainQueryScopeMismatch("GLOBAL deployment scope required")
        return self._deployment_id

    def _deployment_scope(self, spec: QuerySpec) -> str:
        if (
            spec.scope.get("kind") != "DEPLOYMENT"
            or spec.scope.get("deployment_id") != self._deployment_id
        ):
            raise DomainQueryScopeMismatch("DEPLOYMENT scope required")
        return self._deployment_id

    @staticmethod
    def _run_result(
        result_type: str, stable_id: str, fence: RunFence, data: dict[str, Any]
    ) -> QueryResult:
        return QueryResult(
            result_type,
            stable_id,
            _frozen(
                {
                    "scope_kind": "RUN",
                    "run_id": fence.run_id,
                    "research_revision": fence.research_revision,
                    "contract_version": fence.contract_version,
                    "last_cursor": fence.last_cursor,
                }
            ),
            _frozen(data),
        )

    @staticmethod
    def _deployment_result(
        result_type: str, stable_id: str, deployment_id: str, data: dict[str, Any]
    ) -> QueryResult:
        return QueryResult(
            result_type,
            stable_id,
            _frozen({"scope_kind": "GLOBAL", "deployment_id": deployment_id}),
            _frozen(data),
        )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._db_path, timeout=self._busy_timeout_ms / 1_000)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection


def _get(function: Any, entity_id: str) -> Any:
    try:
        return function(entity_id)
    except KeyError as error:
        raise DomainObjectNotFound(entity_id) from error


def _only_id(payload: JsonObject, name: str) -> str:
    if set(payload) != {name} or not isinstance(payload.get(name), str) or not payload[name]:
        raise ValueError(f"query payload must contain only non-empty {name}")
    return cast(str, payload[name])


def _id_with_optional_page(payload: JsonObject, name: str) -> str:
    if set(payload) not in ({name}, {name, "page"}):
        raise ValueError(f"query payload must contain {name} and optional page")
    if "page" in payload:
        _page({"page": payload["page"]})
    value = payload.get(name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be non-empty")
    return value


def _empty_payload(payload: JsonObject) -> None:
    if payload:
        raise ValueError("query payload must be empty")


def _page(payload: JsonObject) -> tuple[int, str | None]:
    if set(payload) != {"page"} or not isinstance(payload["page"], Mapping):
        raise ValueError("query payload requires page")
    page = cast(Mapping[str, object], payload["page"])
    if set(page) not in ({"limit"}, {"limit", "cursor"}):
        raise ValueError("page accepts limit and optional cursor")
    limit = page.get("limit")
    cursor = page.get("cursor")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
        raise ValueError("page limit must be between 1 and 200")
    if cursor is not None and (not isinstance(cursor, str) or not cursor):
        raise ValueError("page cursor must be non-empty")
    return limit, cursor


class _ToolCursorCodec:
    def __init__(self, secret: bytes, deployment_id: str) -> None:
        if len(secret) < 32:
            raise ValueError("domain query cursor secret must contain at least 32 bytes")
        self._secret = secret
        self._deployment_id = deployment_id

    def encode(self, after: tuple[str, str, str]) -> str:
        body = json.dumps(
            {
                "v": 1,
                "query_type": "TOOL_CATALOG",
                "deployment_id": self._deployment_id,
                "after": list(after),
            },
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        signature = hmac.digest(self._secret, body, "sha256")
        encoded = base64.urlsafe_b64encode(body + signature).rstrip(b"=")
        return "rkq1." + encoded.decode()

    def decode(self, token: str) -> tuple[str, str, str]:
        if not token.startswith("rkq1."):
            raise ValueError("tool catalog cursor format is invalid")
        encoded = token[5:]
        try:
            raw = base64.urlsafe_b64decode(encoded + "=" * (-len(encoded) % 4))
        except ValueError as error:
            raise ValueError("tool catalog cursor encoding is invalid") from error
        if len(raw) <= 32:
            raise ValueError("tool catalog cursor is truncated")
        body, signature = raw[:-32], raw[-32:]
        expected = hmac.digest(self._secret, body, "sha256")
        if not hmac.compare_digest(signature, expected):
            raise ValueError("tool catalog cursor signature is invalid")
        value = json.loads(body)
        if not isinstance(value, dict) or value.get("v") != 1:
            raise ValueError("tool catalog cursor body is invalid")
        if (
            value.get("query_type") != "TOOL_CATALOG"
            or value.get("deployment_id") != self._deployment_id
        ):
            raise DomainQueryStale("tool catalog cursor binding changed")
        after = value.get("after")
        if (
            not isinstance(after, list)
            or len(after) != 3
            or any(not isinstance(item, str) or not item for item in after)
        ):
            raise ValueError("tool catalog cursor boundary is invalid")
        return after[0], after[1], after[2]


def _value(value: Any) -> dict[str, Any]:
    if is_dataclass(value) and not isinstance(value, type):
        converted = _convert(asdict(value))
    else:
        converted = _convert(value)
    if not isinstance(converted, dict):
        raise TypeError("domain object projection must be an object")
    return converted


def _convert(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return _convert(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): _convert(item) for key, item in value.items()}
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return [_convert(item) for item in value]
    return value


def _frozen(value: Mapping[str, Any]) -> JsonObject:
    return cast(JsonObject, MappingProxyType({key: _freeze(item) for key, item in value.items()}))


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, list):
        return tuple(_freeze(item) for item in value)
    return value


def _row(row: sqlite3.Row) -> dict[str, Any]:
    return {key: _decode_column(key, row[key]) for key in row}


def _decode_column(name: str, value: Any) -> Any:
    if name.endswith("_json") and isinstance(value, str):
        return _json(value)
    return value


def _json(value: Any) -> Any:
    if not isinstance(value, str):
        raise ValueError("persisted JSON column is not text")
    return json.loads(value)


def _one_dict(
    connection: sqlite3.Connection, table: str, key: str, value: str
) -> dict[str, Any] | None:
    row = connection.execute(f"SELECT * FROM {table} WHERE {key}=?", (value,)).fetchone()
    return _row(row) if row is not None else None


def _all_dicts(
    connection: sqlite3.Connection, table: str, key: str, value: str, order: str
) -> list[dict[str, Any]]:
    rows = connection.execute(
        f"SELECT * FROM {table} WHERE {key}=? ORDER BY {order}", (value,)
    ).fetchall()
    return [_row(row) for row in rows]


def _latest(
    connection: sqlite3.Connection, table: str, run_id: str, revision: str
) -> dict[str, Any] | None:
    row = connection.execute(
        f"SELECT * FROM {table} WHERE run_id=? ORDER BY {revision} DESC LIMIT 1", (run_id,)
    ).fetchone()
    return _row(row) if row is not None else None


__all__ = [
    "DomainObjectNotFound",
    "DomainQueries",
    "DomainQueryScopeMismatch",
    "DomainQueryStale",
    "FenceSource",
]
