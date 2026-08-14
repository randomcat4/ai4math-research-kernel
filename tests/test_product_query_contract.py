from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parents[1]
CATALOG = json.loads((ROOT / "docs/spec/product/catalog.json").read_text(encoding="utf-8"))
SCHEMA = json.loads((ROOT / "docs/spec/product/query.schema.json").read_text(encoding="utf-8"))
Draft202012Validator.check_schema(SCHEMA)
VALIDATOR = Draft202012Validator(SCHEMA, format_checker=FormatChecker())
DEPLOYMENT_ID = "f1d10eee-4da4-49cf-ae78-870dff1c08ba"
RUN_ID = "c73f6387-2ea0-487a-aebf-dd2b8dad8ec2"
ENTITY_ID = "76e89cf5-2d2e-461b-b03d-c4ed076fd6c1"
CURSOR = "rkq1." + "A" * 32

GLOBAL = {
    "LIST_RESEARCH",
    "PROBLEM_POOL",
    "PROBLEM_CANDIDATE",
    "SOURCE_VERSION_HISTORY",
    "BATCH_RESEARCH_JOB",
    "ACTION_ITEMS",
}
DEPLOYMENT = {
    "TOOL_CATALOG",
    "DEPLOYMENT_STATUS",
    "DEPLOYMENT_JOB",
    "BACKUP_STATUS",
    "ADMIN_HEALTH",
    "USAGE",
}
POLYMORPHIC = {"PRODUCT_RECEIPT", "JOB"}
RUN_OR_DEPLOYMENT = {"LITERATURE_QUERY", "SOURCE_SNAPSHOT", "LITERATURE_SOURCE", "LITERATURE_GRAPH"}
RUN = set(CATALOG["query_types"]) - GLOBAL - DEPLOYMENT - POLYMORPHIC
LIST = {
    "LIST_RESEARCH",
    "SOURCE_VERSION_HISTORY",
    "BRIDGE_OPPORTUNITIES",
    "WORK_ITEM",
    "WORKER_RUN",
    "CLAIM_HISTORY",
    "GRAPH_SEARCH",
    "DEPENDENCY_CLOSURE",
    "REVERSE_CLOSURE",
    "ABLATION_RESULTS",
    "TOOL_CATALOG",
    "GUIDANCE_INBOX",
    "REVIEW_INBOX",
    "ARTIFACT_INDEX",
    "ACTION_ITEMS",
    "LITERATURE_GRAPH",
    "USAGE",
}
WORKFLOW = {
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
    "PROBLEM_POOL",
    "PROBLEM_CANDIDATE",
    "SOURCE_VERSION_HISTORY",
    "BATCH_RESEARCH_JOB",
    "BRIDGE_OPPORTUNITIES",
    "ABLATION_PLAN",
    "ABLATION_RESULTS",
    "RESEARCH_CASE_LINEAGE",
    "CLEAN_ROOM_INPUT_MANIFEST",
    "CERTIFICATE_IMPORT_REPORT",
}
IDS = {
    "CONTRACT": "contract_id",
    "MATERIAL": "material_id",
    "MATERIAL_EXTRACTION": "extraction_id",
    "CITATION_ANCHOR": "anchor_id",
    "LITERATURE_QUERY": "literature_query_id",
    "SOURCE_SNAPSHOT": "source_snapshot_id",
    "LITERATURE_SOURCE": "literature_source_id",
    "THEOREM_APPLICABILITY": "applicability_review_id",
    "PRIOR_ART_COMPARISON": "comparison_id",
    "NOVELTY_REVIEW": "novelty_review_id",
    "PROBLEM_POOL": "problem_pool_id",
    "PROBLEM_CANDIDATE": "problem_candidate_id",
    "BATCH_RESEARCH_JOB": "batch_job_id",
    "ROUTE_PLAN": "route_plan_id",
    "ABLATION_PLAN": "ablation_plan_id",
    "WORK_ITEM": "work_item_id",
    "WORKER_RUN": "worker_run_id",
    "CHECKPOINT": "checkpoint_id",
    "CLAIM": "claim_id",
    "COMPUTE_TASK": "compute_task_id",
    "TOOL_RUN": "tool_run_id",
    "HINT": "hint_id",
    "REVIEW_TASK": "review_task_id",
    "DOSSIER": "dossier_id",
    "RESEARCH_CASE_LINEAGE": "lineage_id",
    "CLEAN_ROOM_INPUT_MANIFEST": "manifest_id",
    "CERTIFICATE_IMPORT_REPORT": "report_id",
    "PRODUCT_RECEIPT": "receipt_id",
    "JOB": "job_id",
    "DEPLOYMENT_JOB": "deployment_job_id",
    "BACKUP_STATUS": "backup_id",
}


def errors(value: object) -> list[Any]:
    return list(VALIDATOR.iter_errors(value))


def scope_for(query_type: str, kind: str | None = None) -> dict[str, Any]:
    selected = kind
    if selected is None:
        selected = (
            "GLOBAL"
            if query_type in GLOBAL
            else "DEPLOYMENT"
            if query_type in DEPLOYMENT
            else "RUN"
        )
    if selected == "RUN":
        return {"kind": "RUN", "run_id": RUN_ID}
    return {"kind": selected, "deployment_id": DEPLOYMENT_ID}


def payload_for(query_type: str) -> dict[str, Any]:
    if query_type in IDS:
        value: dict[str, Any] = {IDS[query_type]: ENTITY_ID}
    elif query_type in LIST:
        value = {"page": {"limit": 50}}
    else:
        value = {}
    if query_type == "CONTRACT_IMPACT":
        value = {"impact_preview_id": ENTITY_ID}
    elif query_type == "EXTRACTION_DIFF":
        value = {"before_extraction_id": ENTITY_ID, "after_extraction_id": RUN_ID}
    elif query_type == "LITERATURE_GRAPH":
        value = {"literature_graph_id": ENTITY_ID, "page": {"limit": 50}}
    elif query_type == "SOURCE_VERSION_HISTORY":
        value = {"source_stable_id": "arxiv:2401.00001v2", "page": {"limit": 50}}
    elif query_type == "ABLATION_RESULTS":
        value = {"ablation_plan_id": ENTITY_ID, "page": {"limit": 50}}
    elif query_type == "CLAIM_HISTORY":
        value = {"claim_id": ENTITY_ID, "page": {"limit": 50}}
    elif query_type == "GRAPH_SEARCH":
        value = {"text": "compactness", "mode": "VERIFIED", "at_revision": 8, "page": {"limit": 50}}
    elif query_type in {"DEPENDENCY_CLOSURE", "REVERSE_CLOSURE"}:
        value = {"claim_id": ENTITY_ID, "at_revision": 8, "node_limit": 200}
    elif query_type == "REVOKE_PREVIEW":
        value = {"claim_id": ENTITY_ID, "target_digest": "a" * 64, "at_revision": 8}
    elif query_type == "USAGE":
        value = {
            "from": "2026-08-01T00:00:00Z",
            "to": "2026-08-13T00:00:00Z",
            "granularity": "DAY",
            "page": {"limit": 50},
        }
    elif query_type == "GRAPH_SLICE":
        value = {
            "mode": "VERIFIED",
            "seed_ids": [ENTITY_ID],
            "direction": "BOTH",
            "depth": 2,
            "filters": {},
            "node_limit": 200,
            "at_revision": 8,
        }
    return value


def query_for(query_type: str, kind: str | None = None) -> dict[str, Any]:
    return {
        "schema_version": "rk.product.query.v1",
        "scope": scope_for(query_type, kind),
        "query": {"type": query_type, "payload": payload_for(query_type)},
    }


def projection(query_type: str) -> dict[str, Any]:
    if query_type == "ACTION_ITEMS":
        return {
            "stable_entity_id": ENTITY_ID,
            "run_id": RUN_ID,
            "command_type": "SUBMIT_CLAIM",
            "target_ids": [],
            "required_inputs": ["statement"],
            "blocked_by": [],
            "research_revision": 8,
            "contract_version": 2,
        }
    base: dict[str, Any] = {
        "schema_version": "rk.product.projection.v1",
        "stable_entity_id": ENTITY_ID,
        "projection_type": query_type,
        "status": "CURRENT",
        "artifact_ids": [],
    }
    if query_type == "LIST_RESEARCH":
        base.update(
            {
                "run_id": RUN_ID,
                "title": "群论问题",
                "question_summary": "证明有限群结论",
                "owner": "math:one",
                "labels": ["开放题"],
                "outcome_state": "OPEN",
                "execution_state": "RUNNING",
                "authority_state": "UNVERIFIED",
                "publication_state": "NONE",
                "phase": "探索",
                "blockers": ["等待验证"],
                "next_actions": ["SUBMIT_CLAIM"],
                "budget": {
                    "reserved_microunits": 100,
                    "actual_microunits": 20,
                    "refunded_microunits": 0,
                    "unknown_cost_count": 0,
                },
                "recent_activity_at": "2026-08-13T18:00:00Z",
                "recent_activity_summary": "候选已提交",
                "research_revision": 8,
                "contract_version": 2,
                "last_cursor": 12,
            }
        )
    if query_type in WORKFLOW:
        base.update(
            {
                "evidence_class": "ENGINEERING_REQUIRED",
                "authority_effect": "NO_FACT_GRAPH_WRITE",
                "source_artifact_ids": [],
            }
        )
    return base


def result_for(query_type: str, kind: str | None = None) -> dict[str, Any]:
    selected = kind
    if selected is None:
        selected = (
            "GLOBAL"
            if query_type in GLOBAL
            else "DEPLOYMENT"
            if query_type in DEPLOYMENT
            else "RUN"
        )
    result: dict[str, Any]
    if query_type in LIST:
        result = {
            "items": [projection(query_type)],
            "page": {"returned": 1, "total": 1, "truncated": False},
        }
    else:
        result = {"entity": projection(query_type)}
    value: dict[str, Any] = {
        "schema_version": "rk.product.query_result.v1",
        "result_type": query_type,
        "stable_entity_id": f"query:{query_type.lower()}",
        "scope_kind": selected,
        "last_cursor": 12,
        "result": result,
    }
    if selected == "RUN":
        value.update({"run_id": RUN_ID, "research_revision": 8, "contract_version": 2})
    elif selected == "GLOBAL":
        value.update({"deployment_id": DEPLOYMENT_ID, "catalog_revision": 5})
    else:
        value.update({"deployment_id": DEPLOYMENT_ID, "deployment_revision": 5})
    return value


def graph_node(dependable: bool) -> dict[str, Any]:
    return {
        "claim_id": ENTITY_ID,
        "stable_label": "C1",
        "statement": "P",
        "lifecycle": "ACTIVE",
        "dependable": dependable,
        "claim_type": "LEMMA",
        "authority_axes": {"machine": "VERIFIED"},
        "contract_version": 2,
        "verification_method": "LEAN",
        "route_id": RUN_ID,
    }


def graph_result(mode: str, dependable: bool) -> dict[str, Any]:
    value = result_for("GRAPH_SLICE")
    value["result"] = {
        "mode": mode,
        "nodes": [graph_node(dependable)],
        "edges": [],
        "groups": [],
        "cross_route_boundary": [],
        "total_matches": 1,
        "returned_nodes": 1,
        "returned_edges": 0,
        "truncated": False,
        "query_digest": "a" * 64,
        "boundary_digest": "b" * 64,
    }
    return value


def test_schema_covers_catalog_exactly() -> None:
    found: set[str] = set()
    for branch in SCHEMA["$defs"]["querySpec"]["oneOf"]:
        ref = branch["properties"]["query"]["$ref"]
        found.add(SCHEMA["$defs"][ref.rsplit("/", 1)[1]]["properties"]["type"]["const"])
    assert found == set(CATALOG["query_types"])
    assert len(found) == len(CATALOG["query_types"]) == 56


@pytest.mark.parametrize("query_type", CATALOG["query_types"])
def test_every_query_has_a_strict_valid_sample(query_type: str) -> None:
    assert not errors(query_for(query_type))
    injected = deepcopy(query_for(query_type))
    injected["query"]["payload"]["principal_subject_id"] = "forged"
    assert errors(injected)


@pytest.mark.parametrize("query_type", sorted(RUN))
def test_run_queries_reject_global_scope(query_type: str) -> None:
    assert errors(query_for(query_type, "GLOBAL"))


@pytest.mark.parametrize("query_type", sorted(RUN_OR_DEPLOYMENT))
def test_literature_workspace_queries_allow_deployment_snapshots(query_type: str) -> None:
    assert not errors(query_for(query_type, "DEPLOYMENT"))


@pytest.mark.parametrize("query_type", sorted(GLOBAL))
def test_global_queries_reject_run_scope(query_type: str) -> None:
    assert errors(query_for(query_type, "RUN"))


@pytest.mark.parametrize("query_type", sorted(DEPLOYMENT))
def test_deployment_queries_reject_global_scope(query_type: str) -> None:
    assert errors(query_for(query_type, "GLOBAL"))


@pytest.mark.parametrize("query_type", sorted(POLYMORPHIC))
@pytest.mark.parametrize("kind", ["RUN", "GLOBAL", "DEPLOYMENT"])
def test_receipt_and_job_queries_preserve_object_scope(query_type: str, kind: str) -> None:
    assert not errors(query_for(query_type, kind))


@pytest.mark.parametrize("query_type,id_field", sorted(IDS.items()))
def test_detail_queries_require_domain_stable_id(query_type: str, id_field: str) -> None:
    value = query_for(query_type)
    del value["query"]["payload"][id_field]
    assert errors(value)


@pytest.mark.parametrize(
    "query_type",
    sorted(LIST - {"WORK_ITEM", "WORKER_RUN", "DEPENDENCY_CLOSURE", "REVERSE_CLOSURE"}),
)
def test_list_queries_require_bounded_page(query_type: str) -> None:
    value = query_for(query_type)
    del value["query"]["payload"]["page"]
    assert errors(value)
    value = query_for(query_type)
    value["query"]["payload"]["page"]["limit"] = 201
    assert errors(value)


@pytest.mark.parametrize("query_type", CATALOG["query_types"])
def test_every_query_result_has_scope_fence_and_stable_id(query_type: str) -> None:
    value = (
        graph_result("VERIFIED", True) if query_type == "GRAPH_SLICE" else result_for(query_type)
    )
    assert not errors(value)
    del value["last_cursor"]
    assert errors(value)


def test_result_fence_is_scope_specific() -> None:
    run_value = result_for("CLAIM")
    run_value["deployment_revision"] = 3
    assert errors(run_value)
    global_value = result_for("LIST_RESEARCH")
    del global_value["catalog_revision"]
    assert errors(global_value)
    deployment_value = result_for("ADMIN_HEALTH")
    del deployment_value["deployment_revision"]
    assert errors(deployment_value)


@pytest.mark.parametrize("query_type", CATALOG["query_types"])
def test_result_projection_rejects_unknown_fields(query_type: str) -> None:
    if query_type == "GRAPH_SLICE":
        return
    value = result_for(query_type)
    key = "items" if query_type in LIST else "entity"
    projection_value = value["result"][key][0] if key == "items" else value["result"][key]
    projection_value["private_page_state"] = True
    assert errors(value)


def test_workflow_results_cannot_claim_mathematical_authority() -> None:
    value = result_for("SOURCE_SNAPSHOT")
    value["result"]["entity"]["authority_effect"] = "FACT_GRAPH_WRITE"
    assert errors(value)
    del value["result"]["entity"]["evidence_class"]
    assert errors(value)


def test_graph_slice_separates_verified_and_history_views() -> None:
    assert not errors(graph_result("VERIFIED", True))
    assert errors(graph_result("VERIFIED", False))
    assert not errors(graph_result("RESEARCH_HISTORY", False))

    verified = query_for("GRAPH_SLICE")
    verified["query"]["payload"]["filters"]["lifecycles"] = ["REVOKED"]
    assert errors(verified)
    history = query_for("GRAPH_SLICE")
    history["query"]["payload"]["mode"] = "RESEARCH_HISTORY"
    history["query"]["payload"]["filters"]["lifecycles"] = ["REVOKED"]
    assert not errors(history)


def test_graph_slice_limit_and_opaque_cursor_are_strict() -> None:
    value = query_for("GRAPH_SLICE")
    value["query"]["payload"]["node_limit"] = 201
    assert errors(value)
    value = query_for("GRAPH_SLICE")
    value["query"]["payload"]["continuation_cursor"] = "page-2"
    assert errors(value)
    value["query"]["payload"]["continuation_cursor"] = CURSOR
    assert not errors(value)


def test_cursor_contract_freezes_stale_semantics() -> None:
    comment = SCHEMA["$defs"]["cursor"]["$comment"]
    for term in (
        "run_id",
        "at_revision",
        "query digest",
        "boundary",
        "STALE_QUERY",
        "never rebased",
    ):
        assert term in comment


@pytest.mark.parametrize(
    ("query_type", "field"),
    [
        ("LIST_RESEARCH", "authority_state"),
        ("ACTION_ITEMS", "research_revision"),
    ],
)
def test_b01b_result_rejects_missing_authoritative_field(query_type: str, field: str) -> None:
    value = result_for(query_type)
    del value["result"]["items"][0][field]
    assert errors(value)
