"""Operational QueryResult adapters over persisted product and kernel records."""

from __future__ import annotations

import base64
import hmac
import json
import sqlite3
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol, cast

from rk.product.api import JsonObject, ProductSession, QueryResult, QuerySpec
from rk.product.domain_queries import (
    DomainObjectNotFound,
    DomainQueryScopeMismatch,
    DomainQueryStale,
    FenceSource,
    RunFence,
)


class CatalogFence(Protocol):
    deployment_id: str
    catalog_revision: int
    last_cursor: int


class OperationalFenceSource(FenceSource, Protocol):
    def catalog(self) -> CatalogFence: ...


class OperationalQueries:
    _TYPES = frozenset(
        {
            "RESEARCH_OVERVIEW",
            "SOURCE_VERSION_HISTORY",
            "BRIDGE_OPPORTUNITIES",
            "ABLATION_PLAN",
            "ABLATION_RESULTS",
            "CHECKPOINT",
            "ARTIFACT_INDEX",
            "DEPLOYMENT_STATUS",
            "DEPLOYMENT_JOB",
            "BACKUP_STATUS",
            "ADMIN_HEALTH",
            "USAGE",
        }
    )

    def __init__(
        self,
        *,
        db_path: Path,
        deployment_id: str,
        fences: OperationalFenceSource,
        cursor_secret: bytes,
        busy_timeout_ms: int = 5_000,
    ) -> None:
        if not deployment_id:
            raise ValueError("deployment_id must be non-empty")
        self._db_path = Path(db_path)
        self._deployment_id = deployment_id
        self._fences = fences
        self._cursors = _CursorCodec(cursor_secret)
        self._busy_timeout_ms = busy_timeout_ms

    def supports(self, query_type: str) -> bool:
        return query_type in self._TYPES

    def execute(self, _session: ProductSession, spec: QuerySpec) -> QueryResult:
        handlers = {
            "RESEARCH_OVERVIEW": self._overview,
            "SOURCE_VERSION_HISTORY": self._source_history,
            "BRIDGE_OPPORTUNITIES": self._bridges,
            "ABLATION_PLAN": self._ablation_plan,
            "ABLATION_RESULTS": self._ablation_results,
            "CHECKPOINT": self._checkpoint,
            "ARTIFACT_INDEX": self._artifact_index,
            "DEPLOYMENT_STATUS": self._deployment_status,
            "DEPLOYMENT_JOB": self._deployment_job,
            "BACKUP_STATUS": self._backup_status,
            "ADMIN_HEALTH": self._admin_health,
            "USAGE": self._usage,
        }
        handler = handlers.get(spec.query_type)
        if handler is None:
            raise ValueError(f"unsupported operational query: {spec.query_type}")
        return handler(spec)

    def _overview(self, spec: QuerySpec) -> QueryResult:
        run_id, fence = self._run_scope(spec)
        _empty(spec.payload)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT c.*,s.* FROM research_catalog c JOIN research_summary_projection s "
                "ON s.run_id=c.run_id WHERE c.run_id=?",
                (run_id,),
            ).fetchone()
        if row is None:
            raise DomainObjectNotFound(run_id)
        return self._run_result(spec.query_type, run_id, fence, _row(row))

    def _source_history(self, spec: QuerySpec) -> QueryResult:
        deployment_id, revision, last_cursor = self._global_scope(spec)
        source_id, limit, token = _id_and_page(spec.payload, "source_stable_id")
        after = self._cursors.decode(token, spec.query_type, deployment_id) if token else None
        values: list[object] = [source_id]
        boundary = ""
        if after is not None:
            boundary = " AND (s.observed_at,s.entity_id,s.source_version)>(?,?,?)"
            values.extend(after)
        values.append(limit + 1)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT s.*,e.entity_kind,e.canonical_key,e.title,e.arxiv_id,e.arxiv_version,"
                "e.theorem_id FROM product_literature_entity_sources s "
                "JOIN product_literature_entities e ON e.entity_id=s.entity_id "
                "WHERE s.source_record_key=?"
                + boundary
                + " ORDER BY s.observed_at,s.entity_id,s.source_version LIMIT ?",
                tuple(values),
            ).fetchall()
        return self._deployment_page(
            spec.query_type,
            f"source-history:{source_id}",
            deployment_id,
            revision,
            last_cursor,
            rows,
            limit,
            ("observed_at", "entity_id", "source_version"),
        )

    def _bridges(self, spec: QuerySpec) -> QueryResult:
        run_id, fence = self._run_scope(spec)
        limit, token = _page(spec.payload)
        after = self._cursors.decode(token, spec.query_type, run_id) if token else None
        values: list[object] = [run_id]
        boundary = ""
        if after is not None:
            boundary = " AND (ranking_score,opportunity_id)<(?,?)"
            values.extend((int(after[0]), after[1]))
        values.append(limit + 1)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT o.*,b.bridge_spec_id,b.bound_at FROM product_bridge_opportunities o "
                "LEFT JOIN product_bridge_opportunity_bindings b USING(opportunity_id) "
                "WHERE o.run_id=?"
                + boundary
                + " ORDER BY o.ranking_score DESC,o.opportunity_id LIMIT ?",
                tuple(values),
            ).fetchall()
        return self._run_page(
            spec.query_type,
            f"bridge-opportunities:{run_id}",
            run_id,
            fence,
            rows,
            limit,
            lambda item: (str(item["ranking_score"]), cast(str, item["opportunity_id"])),
        )

    def _ablation_plan(self, spec: QuerySpec) -> QueryResult:
        run_id, fence = self._run_scope(spec)
        entity_id = _only_id(spec.payload, "ablation_plan_id")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM product_ablation_plans WHERE ablation_plan_id=?", (entity_id,)
            ).fetchone()
            groups = connection.execute(
                "SELECT * FROM product_ablation_groups WHERE ablation_plan_id=? "
                "ORDER BY group_name",
                (entity_id,),
            ).fetchall()
        data = _owned(row, run_id, entity_id)
        data["groups"] = [_row(item) for item in groups]
        return self._run_result(spec.query_type, entity_id, fence, data)

    def _ablation_results(self, spec: QuerySpec) -> QueryResult:
        run_id, fence = self._run_scope(spec)
        entity_id, limit, token = _id_and_page(spec.payload, "ablation_plan_id")
        with self._connect() as connection:
            plan = connection.execute(
                "SELECT run_id FROM product_ablation_plans WHERE ablation_plan_id=?",
                (entity_id,),
            ).fetchone()
        _owned(plan, run_id, entity_id)
        after = self._cursors.decode(token, spec.query_type, run_id) if token else None
        values: list[object] = [entity_id]
        boundary = ""
        if after is not None:
            boundary = " AND (a.group_name,a.problem_id,a.assignment_id)>(?,?,?)"
            values.extend(after)
        values.append(limit + 1)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT a.*,r.frozen_digest,r.outcome,r.cost_microunits,"
                "r.certificate_length,r.verifier_profile_receipt_id,"
                "r.verifier_receipt_artifact_id,r.execution_receipt_artifact_id,"
                "r.failure_code,r.finished_at FROM product_ablation_assignments a "
                "LEFT JOIN product_ablation_results r USING(assignment_id) "
                "WHERE a.ablation_plan_id=?"
                + boundary
                + " ORDER BY a.group_name,a.problem_id,a.assignment_id LIMIT ?",
                tuple(values),
            ).fetchall()
        return self._run_page(
            spec.query_type,
            f"ablation-results:{entity_id}",
            run_id,
            fence,
            rows,
            limit,
            lambda item: (
                cast(str, item["group_name"]),
                cast(str, item["problem_id"]),
                cast(str, item["assignment_id"]),
            ),
        )

    def _checkpoint(self, spec: QuerySpec) -> QueryResult:
        run_id, fence = self._run_scope(spec)
        entity_id = _only_id(spec.payload, "checkpoint_id")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT c.*,j.run_id,j.kind AS job_kind,j.state AS job_state "
                "FROM product_job_checkpoints c JOIN product_jobs j ON j.job_id=c.job_id "
                "WHERE c.checkpoint_id=?",
                (entity_id,),
            ).fetchone()
        return self._run_result(spec.query_type, entity_id, fence, _owned(row, run_id, entity_id))

    def _artifact_index(self, spec: QuerySpec) -> QueryResult:
        run_id, fence = self._run_scope(spec)
        limit, token = _page(spec.payload)
        after = self._cursors.decode(token, spec.query_type, run_id) if token else None
        values: list[object] = [run_id]
        boundary = ""
        if after is not None:
            boundary = " AND (ra.role,ra.logical_name,a.artifact_id)>(?,?,?)"
            values.extend(after)
        values.append(limit + 1)
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT a.artifact_id,a.sha256,a.byte_count,a.media_type,a.ingest_state,"
                "a.source_name,a.created_at,a.committed_at,ra.logical_name,ra.role "
                "FROM run_artifacts ra JOIN artifacts a ON a.artifact_id=ra.artifact_id "
                "WHERE ra.run_id=?"
                + boundary
                + " ORDER BY ra.role,ra.logical_name,a.artifact_id LIMIT ?",
                tuple(values),
            ).fetchall()
        return self._run_page(
            spec.query_type,
            f"artifact-index:{run_id}",
            run_id,
            fence,
            rows,
            limit,
            lambda item: (
                cast(str, item["role"]),
                cast(str, item["logical_name"]),
                cast(str, item["artifact_id"]),
            ),
        )

    def _deployment_status(self, spec: QuerySpec) -> QueryResult:
        deployment_id, revision, last_cursor = self._deployment_scope(spec)
        _empty(spec.payload)
        data = self._latest_probe(deployment_id)
        return self._deployment_result(
            spec.query_type,
            f"deployment-status:{deployment_id}",
            deployment_id,
            revision,
            last_cursor,
            data,
        )

    def _deployment_job(self, spec: QuerySpec) -> QueryResult:
        deployment_id, revision, last_cursor = self._deployment_scope(spec)
        entity_id = _only_id(spec.payload, "deployment_job_id")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM product_jobs WHERE job_id=? AND scope_kind='DEPLOYMENT'",
                (entity_id,),
            ).fetchone()
        data = _owned_deployment(row, deployment_id, entity_id)
        return self._deployment_result(
            spec.query_type, entity_id, deployment_id, revision, last_cursor, data
        )

    def _backup_status(self, spec: QuerySpec) -> QueryResult:
        deployment_id, revision, last_cursor = self._deployment_scope(spec)
        entity_id = _only_id(spec.payload, "backup_id")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM product_backups WHERE backup_id=?", (entity_id,)
            ).fetchone()
        data = _owned_deployment(row, deployment_id, entity_id)
        return self._deployment_result(
            spec.query_type, entity_id, deployment_id, revision, last_cursor, data
        )

    def _admin_health(self, spec: QuerySpec) -> QueryResult:
        deployment_id, revision, last_cursor = self._deployment_scope(spec)
        _empty(spec.payload)
        data = self._latest_probe(deployment_id)
        return self._deployment_result(
            spec.query_type,
            f"admin-health:{deployment_id}",
            deployment_id,
            revision,
            last_cursor,
            data,
        )

    def _usage(self, spec: QuerySpec) -> QueryResult:
        deployment_id, revision, last_cursor = self._deployment_scope(spec)
        start, end, granularity, limit, token = _usage_payload(spec.payload)
        after = self._cursors.decode(token, spec.query_type, deployment_id) if token else None
        bucket = "substr(recorded_at,1,13)" if granularity == "HOUR" else "substr(recorded_at,1,10)"
        values: list[object] = [start, end]
        boundary = ""
        if after is not None:
            boundary = f" HAVING ({bucket},resource_kind,event_kind)>(?,?,?)"
            values.extend(after)
        values.append(limit + 1)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT {bucket} AS bucket,resource_kind,event_kind,unit,currency,"
                "SUM(amount_microunits) AS amount_microunits,COUNT(*) AS event_count "
                "FROM budget_events WHERE recorded_at>=? AND recorded_at<? "
                "GROUP BY bucket,resource_kind,event_kind,unit,currency"
                + boundary
                + " ORDER BY bucket,resource_kind,event_kind LIMIT ?",
                tuple(values),
            ).fetchall()
        return self._deployment_page(
            spec.query_type,
            f"usage:{deployment_id}",
            deployment_id,
            revision,
            last_cursor,
            rows,
            limit,
            ("bucket", "resource_kind", "event_kind"),
        )

    def _latest_probe(self, deployment_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM product_deployment_probe_runs WHERE deployment_id=? "
                "ORDER BY finished_at DESC,probe_run_id DESC LIMIT 1",
                (deployment_id,),
            ).fetchone()
            results = (
                []
                if row is None
                else connection.execute(
                    "SELECT * FROM product_deployment_probe_results WHERE probe_run_id=? "
                    "ORDER BY ordinal",
                    (row["probe_run_id"],),
                ).fetchall()
            )
        if row is None:
            raise DomainObjectNotFound(f"deployment-probe:{deployment_id}")
        data = _row(row)
        data["capabilities"] = [_row(item) for item in results]
        return data

    def _run_scope(self, spec: QuerySpec) -> tuple[str, RunFence]:
        if spec.scope.get("kind") != "RUN" or not isinstance(spec.scope.get("run_id"), str):
            raise DomainQueryScopeMismatch("RUN scope required")
        run_id = cast(str, spec.scope["run_id"])
        fence = self._fences.run(run_id)
        if spec.scope.get("at_revision") not in (None, fence.research_revision):
            raise DomainQueryStale("research revision changed")
        if spec.scope.get("at_contract_version") not in (None, fence.contract_version):
            raise DomainQueryStale("contract version changed")
        return run_id, fence

    def _global_scope(self, spec: QuerySpec) -> tuple[str, int, int]:
        if (
            spec.scope.get("kind") != "GLOBAL"
            or spec.scope.get("deployment_id") != self._deployment_id
        ):
            raise DomainQueryScopeMismatch("GLOBAL deployment scope required")
        fence = self._fences.catalog()
        if spec.scope.get("at_catalog_revision") not in (None, fence.catalog_revision):
            raise DomainQueryStale("catalog revision changed")
        return self._deployment_id, fence.catalog_revision, fence.last_cursor

    def _deployment_scope(self, spec: QuerySpec) -> tuple[str, int, int]:
        if (
            spec.scope.get("kind") != "DEPLOYMENT"
            or spec.scope.get("deployment_id") != self._deployment_id
        ):
            raise DomainQueryScopeMismatch("DEPLOYMENT scope required")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT COALESCE(MAX(cursor),0) FROM product_activity_events "
                "WHERE scope_kind='DEPLOYMENT' AND deployment_id=?",
                (self._deployment_id,),
            ).fetchone()
        revision = int(row[0]) if row is not None else 0
        if spec.scope.get("at_deployment_revision") not in (None, revision):
            raise DomainQueryStale("deployment revision changed")
        return self._deployment_id, revision, revision

    def _run_page(
        self,
        result_type: str,
        stable_id: str,
        run_id: str,
        fence: RunFence,
        rows: Sequence[sqlite3.Row],
        limit: int,
        boundary: Any,
    ) -> QueryResult:
        if not rows:
            raise DomainObjectNotFound(stable_id)
        items = [_row(row) for row in rows[:limit]]
        token = (
            self._cursors.encode(result_type, run_id, boundary(items[-1]))
            if len(rows) > limit
            else None
        )
        return self._run_result(
            result_type, stable_id, fence, {"items": items, "page": _page_data(items, token)}
        )

    def _deployment_page(
        self,
        result_type: str,
        stable_id: str,
        deployment_id: str,
        revision: int,
        last_cursor: int,
        rows: Sequence[sqlite3.Row],
        limit: int,
        boundary_fields: tuple[str, ...],
    ) -> QueryResult:
        if not rows:
            raise DomainObjectNotFound(stable_id)
        items = [_row(row) for row in rows[:limit]]
        boundary = tuple(str(items[-1][field] or "") for field in boundary_fields)
        token = (
            self._cursors.encode(result_type, deployment_id, boundary)
            if len(rows) > limit
            else None
        )
        return self._deployment_result(
            result_type,
            stable_id,
            deployment_id,
            revision,
            last_cursor,
            {"items": items, "page": _page_data(items, token)},
        )

    @staticmethod
    def _run_result(
        result_type: str, stable_id: str, fence: RunFence, data: Mapping[str, Any]
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
        result_type: str,
        stable_id: str,
        deployment_id: str,
        revision: int,
        last_cursor: int,
        data: Mapping[str, Any],
    ) -> QueryResult:
        return QueryResult(
            result_type,
            stable_id,
            _frozen(
                {
                    "scope_kind": "DEPLOYMENT",
                    "deployment_id": deployment_id,
                    "deployment_revision": revision,
                    "last_cursor": last_cursor,
                }
            ),
            _frozen(data),
        )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._db_path, timeout=self._busy_timeout_ms / 1_000)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection


class _CursorCodec:
    def __init__(self, secret: bytes) -> None:
        if len(secret) < 32:
            raise ValueError("operational query cursor secret must contain at least 32 bytes")
        self._secret = secret

    def encode(self, query_type: str, scope_id: str, after: Sequence[str]) -> str:
        body = json.dumps(
            {"v": 1, "query_type": query_type, "scope_id": scope_id, "after": list(after)},
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        return (
            "rkq1."
            + base64.urlsafe_b64encode(body + hmac.digest(self._secret, body, "sha256"))
            .rstrip(b"=")
            .decode()
        )

    def decode(self, token: str, query_type: str, scope_id: str) -> tuple[str, ...]:
        if not token.startswith("rkq1."):
            raise ValueError("operational cursor format is invalid")
        try:
            raw = base64.urlsafe_b64decode(token[5:] + "=" * (-len(token[5:]) % 4))
        except ValueError as error:
            raise ValueError("operational cursor encoding is invalid") from error
        if len(raw) <= 32:
            raise ValueError("operational cursor is truncated")
        body, signature = raw[:-32], raw[-32:]
        if not hmac.compare_digest(signature, hmac.digest(self._secret, body, "sha256")):
            raise ValueError("operational cursor signature is invalid")
        value = json.loads(body)
        if not isinstance(value, dict) or value.get("v") != 1:
            raise ValueError("operational cursor body is invalid")
        if value.get("query_type") != query_type or value.get("scope_id") != scope_id:
            raise DomainQueryStale("operational cursor binding changed")
        after = value.get("after")
        if (
            not isinstance(after, list)
            or not after
            or any(not isinstance(item, str) for item in after)
        ):
            raise ValueError("operational cursor boundary is invalid")
        return tuple(after)


def _only_id(payload: JsonObject, name: str) -> str:
    if set(payload) != {name} or not isinstance(payload.get(name), str) or not payload[name]:
        raise ValueError(f"query payload must contain only non-empty {name}")
    return cast(str, payload[name])


def _page(payload: JsonObject) -> tuple[int, str | None]:
    if set(payload) != {"page"} or not isinstance(payload.get("page"), Mapping):
        raise ValueError("query payload requires page")
    page = cast(Mapping[str, object], payload["page"])
    if set(page) not in ({"limit"}, {"limit", "cursor"}):
        raise ValueError("page accepts limit and optional cursor")
    limit, token = page.get("limit"), page.get("cursor")
    if isinstance(limit, bool) or not isinstance(limit, int) or not 1 <= limit <= 200:
        raise ValueError("page limit must be between 1 and 200")
    if token is not None and (not isinstance(token, str) or not token):
        raise ValueError("page cursor must be non-empty")
    return limit, token


def _id_and_page(payload: JsonObject, name: str) -> tuple[str, int, str | None]:
    if set(payload) != {name, "page"}:
        raise ValueError(f"query payload requires {name} and page")
    entity_id = payload.get(name)
    if not isinstance(entity_id, str) or not entity_id:
        raise ValueError(f"{name} must be non-empty")
    limit, token = _page({"page": payload["page"]})
    return entity_id, limit, token


def _usage_payload(payload: JsonObject) -> tuple[str, str, str, int, str | None]:
    if set(payload) != {"from", "to", "granularity", "page"}:
        raise ValueError("usage payload is incomplete")
    start, end, granularity = payload.get("from"), payload.get("to"), payload.get("granularity")
    if not isinstance(start, str) or not start or not isinstance(end, str) or start >= end:
        raise ValueError("usage interval is invalid")
    if granularity not in {"HOUR", "DAY"}:
        raise ValueError("usage granularity is invalid")
    limit, token = _page({"page": payload["page"]})
    return start, end, cast(str, granularity), limit, token


def _empty(payload: JsonObject) -> None:
    if payload:
        raise ValueError("query payload must be empty")


def _owned(row: sqlite3.Row | None, run_id: str, entity_id: str) -> dict[str, Any]:
    if row is None:
        raise DomainObjectNotFound(entity_id)
    data = _row(row)
    if data.get("run_id") != run_id:
        raise DomainQueryScopeMismatch(entity_id)
    return data


def _owned_deployment(
    row: sqlite3.Row | None, deployment_id: str, entity_id: str
) -> dict[str, Any]:
    if row is None:
        raise DomainObjectNotFound(entity_id)
    data = _row(row)
    if data.get("deployment_id") != deployment_id:
        raise DomainQueryScopeMismatch(entity_id)
    return data


def _row(row: sqlite3.Row) -> dict[str, Any]:
    return {key: _decode(key, value) for key, value in zip(row.keys(), row, strict=True)}


def _decode(name: str, value: Any) -> Any:
    return json.loads(value) if name.endswith("_json") and isinstance(value, str) else value


def _page_data(items: Sequence[object], token: str | None) -> dict[str, Any]:
    return {"returned": len(items), "next_cursor": token}


def _frozen(value: Mapping[str, Any]) -> JsonObject:
    return cast(JsonObject, MappingProxyType({key: _freeze(item) for key, item in value.items()}))


def _freeze(value: Any) -> Any:
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(_freeze(item) for item in value)
    return value


__all__ = ["OperationalFenceSource", "OperationalQueries"]
