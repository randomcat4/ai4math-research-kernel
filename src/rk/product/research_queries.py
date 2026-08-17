"""Strict research-workspace projections over persisted product authority records."""

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
from typing import Any, cast

from rk.product.api import JsonObject, ProductSession, QueryResult, QuerySpec
from rk.product.domain_queries import (
    DomainObjectNotFound,
    DomainQueryScopeMismatch,
    DomainQueryStale,
    FenceSource,
    RunFence,
)
from rk.sqlite import open_sqlite


class ResearchQueries:
    """Expose only records that exist under the requested run fence."""

    _TYPES = frozenset(
        {
            "CONTRACT",
            "CONTRACT_IMPACT",
            "MATERIAL",
            "MATERIAL_EXTRACTION",
            "CITATION_ANCHOR",
            "EXTRACTION_DIFF",
            "LITERATURE_QUERY",
            "SOURCE_SNAPSHOT",
            "LITERATURE_SOURCE",
            "LITERATURE_GRAPH",
            "THEOREM_APPLICABILITY",
            "PRIOR_ART_COMPARISON",
            "NOVELTY_REVIEW",
            "CLAIM",
            "CLAIM_HISTORY",
            "AVAILABLE_ACTIONS",
            "REVOKE_PREVIEW",
            "GUIDANCE_INBOX",
            "HINT",
            "REVIEW_INBOX",
            "REVIEW_TASK",
        }
    )

    def __init__(
        self,
        *,
        db_path: Path,
        fences: FenceSource,
        cursor_secret: bytes,
        busy_timeout_ms: int = 5_000,
    ) -> None:
        self._db_path = Path(db_path)
        self._fences = fences
        self._cursors = _ResearchCursorCodec(cursor_secret)
        self._busy_timeout_ms = busy_timeout_ms

    def supports(self, query_type: str) -> bool:
        return query_type in self._TYPES

    def execute(self, session: ProductSession, spec: QuerySpec) -> QueryResult:
        handlers = {
            "CONTRACT": self._contract,
            "CONTRACT_IMPACT": self._contract_impact,
            "MATERIAL": self._material,
            "MATERIAL_EXTRACTION": self._material_extraction,
            "CITATION_ANCHOR": self._citation_anchor,
            "EXTRACTION_DIFF": self._extraction_diff,
            "LITERATURE_QUERY": self._missing_literature_query,
            "SOURCE_SNAPSHOT": self._source_snapshot,
            "LITERATURE_SOURCE": self._missing_literature_source,
            "LITERATURE_GRAPH": self._missing_literature_graph,
            "THEOREM_APPLICABILITY": self._theorem_applicability,
            "PRIOR_ART_COMPARISON": self._prior_art,
            "NOVELTY_REVIEW": self._novelty_review,
            "CLAIM": self._claim,
            "CLAIM_HISTORY": self._claim_history,
            "AVAILABLE_ACTIONS": self._available_actions,
            "REVOKE_PREVIEW": self._revoke_preview,
            "GUIDANCE_INBOX": self._guidance_inbox,
            "HINT": self._hint,
            "REVIEW_INBOX": self._review_inbox,
            "REVIEW_TASK": self._review_task,
        }
        handler = handlers.get(spec.query_type)
        if handler is None:
            raise ValueError(f"unsupported research query: {spec.query_type}")
        return handler(session, spec)

    def _contract(self, _session: ProductSession, spec: QuerySpec) -> QueryResult:
        run_id, fence = self._run_scope(spec)
        entity_id = _only_id(spec.payload, "contract_id")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT c.*,v.state,v.content_json,v.content_digest,v.supersedes_version,"
                "v.confirmed_by,v.confirmed_at,v.created_at AS version_created_at "
                "FROM product_contracts c JOIN product_contract_versions v "
                "ON v.contract_id=c.contract_id AND v.version=c.current_version "
                "WHERE c.contract_id=?",
                (entity_id,),
            ).fetchone()
            ambiguities = connection.execute(
                "SELECT * FROM product_contract_ambiguities WHERE contract_id=? "
                "AND contract_version=(SELECT current_version FROM product_contracts "
                "WHERE contract_id=?) ORDER BY field_path,ambiguity_id",
                (entity_id, entity_id),
            ).fetchall()
        data = self._owned_row(row, run_id, entity_id)
        data["ambiguities"] = [_row(item) for item in ambiguities]
        return self._result(spec.query_type, entity_id, fence, data)

    def _contract_impact(self, _session: ProductSession, spec: QuerySpec) -> QueryResult:
        run_id, fence = self._run_scope(spec)
        entity_id = _only_id(spec.payload, "impact_preview_id")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT p.*,c.run_id FROM product_contract_revision_previews p "
                "JOIN product_contracts c ON c.contract_id=p.contract_id WHERE p.preview_id=?",
                (entity_id,),
            ).fetchone()
        return self._result(
            spec.query_type, entity_id, fence, self._owned_row(row, run_id, entity_id)
        )

    def _material(self, _session: ProductSession, spec: QuerySpec) -> QueryResult:
        run_id, fence = self._run_scope(spec)
        entity_id = _only_id(spec.payload, "material_id")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM product_materials WHERE material_id=?", (entity_id,)
            ).fetchone()
            extractions = connection.execute(
                "SELECT * FROM product_material_extractions WHERE material_id=? "
                "ORDER BY created_at,extraction_id",
                (entity_id,),
            ).fetchall()
        data = self._owned_row(row, run_id, entity_id)
        data["extractions"] = [_row(item) for item in extractions]
        return self._result(spec.query_type, entity_id, fence, data)

    def _material_extraction(
        self, _session: ProductSession, spec: QuerySpec
    ) -> QueryResult:
        run_id, fence = self._run_scope(spec)
        entity_id = _only_id(spec.payload, "extraction_id")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT e.*,m.run_id FROM product_material_extractions e "
                "JOIN product_materials m ON m.material_id=e.material_id "
                "WHERE e.extraction_id=?",
                (entity_id,),
            ).fetchone()
            anchors = connection.execute(
                "SELECT * FROM product_material_anchors WHERE extraction_id=? "
                "ORDER BY anchor_kind,anchor_id",
                (entity_id,),
            ).fetchall()
        data = self._owned_row(row, run_id, entity_id)
        data["anchors"] = [_row(item) for item in anchors]
        data["establishes_mathematical_fact"] = False
        return self._result(spec.query_type, entity_id, fence, data)

    def _citation_anchor(self, _session: ProductSession, spec: QuerySpec) -> QueryResult:
        run_id, fence = self._run_scope(spec)
        entity_id = _only_id(spec.payload, "anchor_id")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT a.*,e.material_id,m.run_id FROM product_material_anchors a "
                "JOIN product_material_extractions e ON e.extraction_id=a.extraction_id "
                "JOIN product_materials m ON m.material_id=e.material_id WHERE a.anchor_id=?",
                (entity_id,),
            ).fetchone()
        return self._result(
            spec.query_type, entity_id, fence, self._owned_row(row, run_id, entity_id)
        )

    def _extraction_diff(self, _session: ProductSession, spec: QuerySpec) -> QueryResult:
        run_id, fence = self._run_scope(spec)
        before_id, after_id = _two_ids(
            spec.payload, "before_extraction_id", "after_extraction_id"
        )
        with self._connect() as connection:
            rows = []
            for entity_id in (before_id, after_id):
                row = connection.execute(
                    "SELECT e.*,m.run_id FROM product_material_extractions e "
                    "JOIN product_materials m ON m.material_id=e.material_id "
                    "WHERE e.extraction_id=?",
                    (entity_id,),
                ).fetchone()
                rows.append(self._owned_row(row, run_id, entity_id))
        if rows[0]["material_id"] != rows[1]["material_id"]:
            raise DomainQueryScopeMismatch("extractions belong to different materials")
        if rows[1]["supersedes_extraction_id"] != before_id:
            raise DomainQueryScopeMismatch("after extraction does not supersede before extraction")
        stable_id = f"{before_id}:{after_id}"
        return self._result(
            spec.query_type,
            stable_id,
            fence,
            {
                "before": rows[0],
                "after": rows[1],
                "difference_artifact_id": rows[1]["difference_artifact_id"],
            },
        )

    def _missing_literature_query(
        self, _session: ProductSession, spec: QuerySpec
    ) -> QueryResult:
        self._run_scope(spec)
        raise DomainObjectNotFound(_only_id(spec.payload, "literature_query_id"))

    def _missing_literature_source(
        self, _session: ProductSession, spec: QuerySpec
    ) -> QueryResult:
        self._run_scope(spec)
        raise DomainObjectNotFound(_only_id(spec.payload, "literature_source_id"))

    def _missing_literature_graph(
        self, _session: ProductSession, spec: QuerySpec
    ) -> QueryResult:
        self._run_scope(spec)
        entity_id = _id_with_page(spec.payload, "literature_graph_id")
        raise DomainObjectNotFound(entity_id)

    def _source_snapshot(self, _session: ProductSession, spec: QuerySpec) -> QueryResult:
        run_id, fence = self._run_scope(spec)
        entity_id = _only_id(spec.payload, "source_snapshot_id")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT s.*,t.run_id FROM product_source_snapshots s "
                "JOIN product_tool_runs t ON t.tool_run_id=s.tool_run_id "
                "WHERE s.snapshot_id=?",
                (entity_id,),
            ).fetchone()
        data = self._owned_row(row, run_id, entity_id)
        data["establishes_novelty"] = False
        return self._result(spec.query_type, entity_id, fence, data)

    def _theorem_applicability(
        self, _session: ProductSession, spec: QuerySpec
    ) -> QueryResult:
        run_id, fence = self._run_scope(spec)
        entity_id = _only_id(spec.payload, "applicability_review_id")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT a.*,l.run_id FROM product_theorem_applicability_reviews a "
                "JOIN product_literature_links l ON l.link_id=a.target_link_id "
                "WHERE a.applicability_id=?",
                (entity_id,),
            ).fetchone()
        return self._result(
            spec.query_type, entity_id, fence, self._owned_row(row, run_id, entity_id)
        )

    def _prior_art(self, _session: ProductSession, spec: QuerySpec) -> QueryResult:
        return self._direct_run_record(
            spec, "comparison_id", "product_prior_art_comparisons", "comparison_id"
        )

    def _novelty_review(self, _session: ProductSession, spec: QuerySpec) -> QueryResult:
        return self._direct_run_record(
            spec, "novelty_review_id", "product_novelty_reviews", "novelty_review_id"
        )

    def _claim(self, _session: ProductSession, spec: QuerySpec) -> QueryResult:
        run_id, fence = self._run_scope(spec)
        entity_id = _only_id(spec.payload, "claim_id")
        data = self._claim_record(run_id, entity_id)
        return self._result(spec.query_type, entity_id, fence, data)

    def _claim_history(self, _session: ProductSession, spec: QuerySpec) -> QueryResult:
        run_id, fence = self._run_scope(spec)
        claim_id, limit, token = _id_and_page(spec.payload, "claim_id")
        target = self._claim_record(run_id, claim_id)
        after = self._cursors.decode(token, spec.query_type, run_id) if token else None
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM product_claims WHERE run_id=? ORDER BY created_at,claim_id",
                (run_id,),
            ).fetchall()
        projected = [_row(row) for row in rows]
        lineage_ids = {claim_id}
        changed = True
        while changed:
            changed = False
            for item in projected:
                parent = item["supersedes_claim_id"]
                if item["claim_id"] in lineage_ids or parent in lineage_ids:
                    if item["claim_id"] not in lineage_ids:
                        lineage_ids.add(cast(str, item["claim_id"]))
                        changed = True
                    if isinstance(parent, str) and parent not in lineage_ids:
                        lineage_ids.add(parent)
                        changed = True
        lineage = [item for item in projected if item["claim_id"] in lineage_ids]
        if after is not None:
            lineage = [
                item
                for item in lineage
                if (cast(str, item["created_at"]), cast(str, item["claim_id"])) > after
            ]
        page = lineage[:limit]
        if not page:
            raise DomainObjectNotFound(f"claim-history:{claim_id}")
        next_cursor = None
        if len(lineage) > limit:
            last = page[-1]
            next_cursor = self._cursors.encode(
                spec.query_type,
                run_id,
                (cast(str, last["created_at"]), cast(str, last["claim_id"])),
            )
        return self._result(
            spec.query_type,
            f"claim-history:{claim_id}",
            fence,
            {"target": target, "items": page, "page": _page_data(page, next_cursor)},
        )

    def _available_actions(self, _session: ProductSession, spec: QuerySpec) -> QueryResult:
        run_id, fence = self._run_scope(spec)
        _empty(spec.payload)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT available_actions_json FROM research_summary_projection WHERE run_id=?",
                (run_id,),
            ).fetchone()
        if row is None:
            raise DomainObjectNotFound(f"available-actions:{run_id}")
        actions = _json(str(row[0]))
        if not isinstance(actions, list) or not actions:
            raise DomainObjectNotFound(f"available-actions:{run_id}")
        return self._result(
            spec.query_type,
            f"available-actions:{run_id}",
            fence,
            {"items": actions},
        )

    def _revoke_preview(self, _session: ProductSession, spec: QuerySpec) -> QueryResult:
        run_id, fence = self._run_scope(spec)
        claim_id, digest, revision = _revoke_key(spec.payload)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM product_revocation_previews WHERE run_id=? "
                "AND target_fact_id=? AND target_fact_digest=? AND preview_revision=? "
                "ORDER BY created_at DESC,preview_id DESC LIMIT 1",
                (run_id, claim_id, digest, revision),
            ).fetchone()
        if row is None:
            raise DomainObjectNotFound(f"revoke-preview:{claim_id}")
        data = _row(row)
        return self._result(spec.query_type, cast(str, data["preview_id"]), fence, data)

    def _guidance_inbox(self, _session: ProductSession, spec: QuerySpec) -> QueryResult:
        return self._paged_table(
            spec,
            table="product_guidance",
            id_column="guidance_id",
            where="run_id=? AND state='QUEUED'",
            params=(),
        )

    def _hint(self, _session: ProductSession, spec: QuerySpec) -> QueryResult:
        return self._direct_run_record(spec, "hint_id", "product_route_hints", "hint_id")

    def _review_inbox(self, session: ProductSession, spec: QuerySpec) -> QueryResult:
        return self._paged_table(
            spec,
            table="product_review_tasks",
            id_column="review_task_id",
            where=(
                "json_extract(binding_json,'$.run_id')=? AND assignee_identity_id IN "
                "(SELECT identity_id FROM product_identities WHERE subject_id=? AND enabled=1) "
                "AND status IN ('OPEN','CLAIMED','REASSIGNED')"
            ),
            params=(session.principal_subject_id,),
        )

    def _review_task(self, session: ProductSession, spec: QuerySpec) -> QueryResult:
        run_id, fence = self._run_scope(spec)
        entity_id = _only_id(spec.payload, "review_task_id")
        with self._connect() as connection:
            row = connection.execute(
                "SELECT task.*,identity.subject_id AS assignee_subject_id "
                "FROM product_review_tasks task JOIN product_identities identity "
                "ON identity.identity_id=task.assignee_identity_id "
                "WHERE task.review_task_id=?",
                (entity_id,),
            ).fetchone()
        if row is None:
            raise DomainObjectNotFound(entity_id)
        data = _row(row)
        binding = data["binding_json"]
        if not isinstance(binding, dict) or binding.get("run_id") != run_id:
            raise DomainQueryScopeMismatch(entity_id)
        if data["assignee_subject_id"] != session.principal_subject_id:
            raise DomainQueryScopeMismatch(entity_id)
        return self._result(spec.query_type, entity_id, fence, data)

    def _direct_run_record(
        self, spec: QuerySpec, payload_key: str, table: str, id_column: str
    ) -> QueryResult:
        run_id, fence = self._run_scope(spec)
        entity_id = _only_id(spec.payload, payload_key)
        with self._connect() as connection:
            row = connection.execute(
                f"SELECT * FROM {table} WHERE {id_column}=?", (entity_id,)
            ).fetchone()
        data = self._owned_row(row, run_id, entity_id)
        return self._result(spec.query_type, entity_id, fence, data)

    def _paged_table(
        self,
        spec: QuerySpec,
        *,
        table: str,
        id_column: str,
        where: str,
        params: tuple[object, ...],
    ) -> QueryResult:
        run_id, fence = self._run_scope(spec)
        limit, token = _page(spec.payload)
        after = self._cursors.decode(token, spec.query_type, run_id) if token else None
        boundary = ""
        values: list[object] = [run_id, *params]
        if after is not None:
            boundary = f" AND (created_at,{id_column})>(?,?)"
            values.extend(after)
        values.append(limit + 1)
        with self._connect() as connection:
            rows = connection.execute(
                f"SELECT * FROM {table} WHERE {where}{boundary} "
                f"ORDER BY created_at,{id_column} LIMIT ?",
                tuple(values),
            ).fetchall()
        if not rows:
            raise DomainObjectNotFound(f"{spec.query_type.lower()}:{run_id}")
        more = len(rows) > limit
        page = [_row(row) for row in rows[:limit]]
        next_cursor = None
        if more:
            last = page[-1]
            next_cursor = self._cursors.encode(
                spec.query_type,
                run_id,
                (cast(str, last["created_at"]), cast(str, last[id_column])),
            )
        return self._result(
            spec.query_type,
            f"{spec.query_type.lower()}:{run_id}",
            fence,
            {"items": page, "page": _page_data(page, next_cursor)},
        )

    def _claim_record(self, run_id: str, claim_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT * FROM product_claims WHERE claim_id=?", (claim_id,)
            ).fetchone()
            predecessors = connection.execute(
                "SELECT fact_id,ordinal FROM product_claim_predecessors WHERE claim_id=? "
                "ORDER BY ordinal",
                (claim_id,),
            ).fetchall()
            evidence = connection.execute(
                "SELECT ordinal,binding_json FROM product_claim_evidence WHERE claim_id=? "
                "ORDER BY ordinal",
                (claim_id,),
            ).fetchall()
            validations = connection.execute(
                "SELECT * FROM product_claim_validations WHERE claim_id=? "
                "ORDER BY created_at,validation_id",
                (claim_id,),
            ).fetchall()
        data = self._owned_row(row, run_id, claim_id)
        data["predecessors"] = [_row(item) for item in predecessors]
        data["evidence"] = [_row(item) for item in evidence]
        data["validations"] = [_row(item) for item in validations]
        return data

    @staticmethod
    def _owned_row(
        row: sqlite3.Row | None, run_id: str, entity_id: str
    ) -> dict[str, Any]:
        if row is None:
            raise DomainObjectNotFound(entity_id)
        data = _row(row)
        if data.get("run_id") != run_id:
            raise DomainQueryScopeMismatch(entity_id)
        return data

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

    @staticmethod
    def _result(
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

    def _connect(self) -> sqlite3.Connection:
        connection = open_sqlite(self._db_path, timeout=self._busy_timeout_ms / 1_000)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection


class _ResearchCursorCodec:
    def __init__(self, secret: bytes) -> None:
        if len(secret) < 32:
            raise ValueError("research query cursor secret must contain at least 32 bytes")
        self._secret = secret

    def encode(self, query_type: str, run_id: str, after: tuple[str, str]) -> str:
        body = json.dumps(
            {"v": 1, "query_type": query_type, "run_id": run_id, "after": list(after)},
            separators=(",", ":"),
            sort_keys=True,
        ).encode()
        signature = hmac.digest(self._secret, body, "sha256")
        return "rkq1." + base64.urlsafe_b64encode(body + signature).rstrip(b"=").decode()

    def decode(self, token: str, query_type: str, run_id: str) -> tuple[str, str]:
        if not token.startswith("rkq1."):
            raise ValueError("research query cursor format is invalid")
        try:
            raw = base64.urlsafe_b64decode(token[5:] + "=" * (-len(token[5:]) % 4))
        except ValueError as error:
            raise ValueError("research query cursor encoding is invalid") from error
        if len(raw) <= 32:
            raise ValueError("research query cursor is truncated")
        body, signature = raw[:-32], raw[-32:]
        if not hmac.compare_digest(signature, hmac.digest(self._secret, body, "sha256")):
            raise ValueError("research query cursor signature is invalid")
        value = json.loads(body)
        if not isinstance(value, dict) or value.get("v") != 1:
            raise ValueError("research query cursor body is invalid")
        if value.get("query_type") != query_type or value.get("run_id") != run_id:
            raise DomainQueryStale("research query cursor binding changed")
        after = value.get("after")
        if (
            not isinstance(after, list)
            or len(after) != 2
            or any(not isinstance(item, str) or not item for item in after)
        ):
            raise ValueError("research query cursor boundary is invalid")
        return after[0], after[1]


def _only_id(payload: JsonObject, name: str) -> str:
    if set(payload) != {name} or not isinstance(payload.get(name), str) or not payload[name]:
        raise ValueError(f"query payload must contain only non-empty {name}")
    return cast(str, payload[name])


def _two_ids(payload: JsonObject, first: str, second: str) -> tuple[str, str]:
    if set(payload) != {first, second}:
        raise ValueError(f"query payload requires {first} and {second}")
    one, two = payload.get(first), payload.get(second)
    if not isinstance(one, str) or not one or not isinstance(two, str) or not two:
        raise ValueError("extraction identifiers must be non-empty")
    return one, two


def _id_with_page(payload: JsonObject, name: str) -> str:
    if set(payload) != {name, "page"}:
        raise ValueError(f"query payload requires {name} and page")
    _page({"page": payload["page"]})
    value = payload.get(name)
    if not isinstance(value, str) or not value:
        raise ValueError(f"{name} must be non-empty")
    return value


def _id_and_page(payload: JsonObject, name: str) -> tuple[str, int, str | None]:
    entity_id = _id_with_page(payload, name)
    limit, token = _page({"page": payload["page"]})
    return entity_id, limit, token


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


def _revoke_key(payload: JsonObject) -> tuple[str, str, int]:
    expected = {"claim_id", "target_digest", "at_revision"}
    if set(payload) != expected:
        raise ValueError("revoke preview payload is incomplete")
    claim_id, digest, revision = (
        payload.get("claim_id"),
        payload.get("target_digest"),
        payload.get("at_revision"),
    )
    if (
        not isinstance(claim_id, str)
        or not claim_id
        or not isinstance(digest, str)
        or len(digest) != 64
        or isinstance(revision, bool)
        or not isinstance(revision, int)
        or revision < 0
    ):
        raise ValueError("revoke preview key is invalid")
    return claim_id, digest, revision


def _empty(payload: JsonObject) -> None:
    if payload:
        raise ValueError("query payload must be empty")


def _row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        key: _decode(key, value)
        for key, value in zip(row.keys(), row, strict=True)
    }


def _decode(name: str, value: Any) -> Any:
    return _json(value) if name.endswith("_json") and isinstance(value, str) else value


def _json(value: str) -> Any:
    return json.loads(value)


def _page_data(items: Sequence[object], next_cursor: str | None) -> dict[str, Any]:
    return {"returned": len(items), "next_cursor": next_cursor}


def _frozen(value: Mapping[str, Any]) -> JsonObject:
    return cast(JsonObject, MappingProxyType({key: _freeze(item) for key, item in value.items()}))


def _freeze(value: Any) -> Any:
    if isinstance(value, Enum):
        return value.value
    if is_dataclass(value) and not isinstance(value, type):
        return _freeze(asdict(value))
    if isinstance(value, Mapping):
        return MappingProxyType({str(key): _freeze(item) for key, item in value.items()})
    if isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        return tuple(_freeze(item) for item in value)
    return value


__all__ = ["ResearchQueries"]
