"""Derive SDK metadata and the transport envelope from strict query.schema.json."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

Schema = dict[str, Any]


RESULT_DOMAIN_FIELDS: dict[str, tuple[str, ...]] = {
    "RESEARCH_OVERVIEW": (
        "run_id",
        "outcome_state",
        "execution_state",
        "authority_state",
        "publication_state",
    ),
    "CONTRACT": ("contract_id", "contract_version", "contract_state", "content_digest"),
    "CONTRACT_IMPACT": (
        "impact_preview_id",
        "base_contract_version",
        "affected_object_ids",
        "impact_digest",
    ),
    "MATERIAL": ("material_id", "original_artifact_id", "media_type", "ingest_state", "ocr_state"),
    "MATERIAL_EXTRACTION": (
        "extraction_id",
        "material_id",
        "extractor_profile_id",
        "output_artifact_id",
        "extraction_digest",
    ),
    "CITATION_ANCHOR": (
        "anchor_id",
        "material_id",
        "source_artifact_id",
        "locator",
        "extraction_digest",
    ),
    "EXTRACTION_DIFF": (
        "diff_id",
        "before_extraction_id",
        "after_extraction_id",
        "changed_regions",
        "diff_digest",
    ),
    "LITERATURE_QUERY": (
        "literature_query_id",
        "source_mode",
        "snapshot_id",
        "query_digest",
        "coverage_boundary",
    ),
    "SOURCE_SNAPSHOT": (
        "snapshot_id",
        "source_mode",
        "corpus_digest",
        "retrieved_at",
        "coverage_boundary",
    ),
    "LITERATURE_SOURCE": (
        "literature_source_id",
        "stable_source_id",
        "source_version",
        "source_kind",
        "content_digest",
    ),
    "LITERATURE_GRAPH": (
        "literature_graph_id",
        "node_count",
        "edge_count",
        "source_kinds",
        "graph_digest",
    ),
    "THEOREM_APPLICABILITY": (
        "applicability_review_id",
        "theorem_id",
        "claim_id",
        "verdict",
        "review_artifact_id",
    ),
    "PRIOR_ART_COMPARISON": (
        "comparison_id",
        "claim_id",
        "literature_source_id",
        "relationship",
        "comparison_digest",
    ),
    "NOVELTY_REVIEW": (
        "novelty_review_id",
        "claim_id",
        "verdict",
        "coverage_snapshot_ids",
        "review_artifact_id",
    ),
    "PROBLEM_POOL": (
        "problem_pool_id",
        "pool_revision",
        "candidate_count",
        "source_snapshot_ids",
        "selection_digest",
    ),
    "PROBLEM_CANDIDATE": (
        "problem_candidate_id",
        "problem_pool_id",
        "statement_digest",
        "candidate_state",
        "source_anchor_ids",
    ),
    "SOURCE_VERSION_HISTORY": (
        "stable_source_id",
        "source_version",
        "content_digest",
        "supersedes_version",
        "snapshot_id",
    ),
    "BATCH_RESEARCH_JOB": (
        "batch_job_id",
        "job_state",
        "total_count",
        "succeeded_count",
        "failed_count",
    ),
    "ROUTE_PLAN": ("route_plan_id", "plan_revision", "plan_state", "route_ids", "contract_version"),
    "BRIDGE_OPPORTUNITIES": (
        "opportunity_id",
        "source_domain",
        "target_domain",
        "distance_microunits",
        "death_test_state",
    ),
    "ABLATION_PLAN": (
        "ablation_plan_id",
        "opportunity_id",
        "baseline_digest",
        "variant_digests",
        "plan_state",
    ),
    "ABLATION_RESULTS": (
        "ablation_result_id",
        "ablation_plan_id",
        "baseline_metric_microunits",
        "variant_metric_microunits",
        "result_artifact_id",
    ),
    "WORKFLOW": (
        "workflow_id",
        "phase",
        "workflow_state",
        "active_work_item_ids",
        "workflow_digest",
    ),
    "WORK_ITEM": ("work_item_id", "work_kind", "work_state", "route_id", "input_digest"),
    "WORKER_RUN": (
        "worker_run_id",
        "work_item_id",
        "worker_subject_id",
        "run_state",
        "input_digest",
    ),
    "CHECKPOINT": (
        "checkpoint_id",
        "route_id",
        "checkpoint_revision",
        "checkpoint_state",
        "checkpoint_digest",
    ),
    "CLAIM": (
        "claim_id",
        "stable_label",
        "lifecycle",
        "machine_state",
        "semantic_state",
        "statement_digest",
    ),
    "CLAIM_HISTORY": (
        "claim_id",
        "claim_revision",
        "lifecycle",
        "statement_digest",
        "supersedes_claim_id",
    ),
    "AVAILABLE_ACTIONS": (
        "action_set_id",
        "allowed_command_types",
        "expected_revision",
        "expected_contract_version",
        "guard_digest",
    ),
    "GRAPH_SEARCH": ("claim_id", "graph_mode", "dependable", "distance", "statement_digest"),
    "DEPENDENCY_CLOSURE": ("claim_id", "graph_mode", "dependable", "distance", "boundary_digest"),
    "REVERSE_CLOSURE": ("claim_id", "graph_mode", "dependable", "distance", "boundary_digest"),
    "REVOKE_PREVIEW": (
        "revoke_preview_id",
        "target_claim_id",
        "target_digest",
        "closure_digest",
        "affected_claim_ids",
    ),
    "COMPUTE_TASK": (
        "compute_task_id",
        "task_type",
        "task_state",
        "input_digest",
        "output_artifact_ids",
    ),
    "TOOL_CATALOG": (
        "tool_profile_id",
        "profile_version",
        "tool_state",
        "capabilities",
        "authority_ceiling",
    ),
    "TOOL_RUN": (
        "tool_run_id",
        "tool_profile_id",
        "run_state",
        "execution_receipt_id",
        "output_artifact_ids",
    ),
    "GUIDANCE_INBOX": ("hint_id", "hint_kind", "hint_state", "checkpoint_id", "target_digest"),
    "HINT": ("hint_id", "hint_kind", "hint_state", "checkpoint_id", "target_digest"),
    "REVIEW_INBOX": ("review_task_id", "review_type", "review_state", "target_id", "target_digest"),
    "REVIEW_TASK": (
        "review_task_id",
        "review_type",
        "review_state",
        "target_id",
        "signed_review_artifact_id",
    ),
    "DOSSIER": (
        "dossier_id",
        "dossier_state",
        "observed_revision",
        "dossier_artifact_id",
        "dossier_digest",
    ),
    "PUBLICATION_STATUS": (
        "publication_id",
        "finalized_revision",
        "final_outcome",
        "review_state",
        "final_pdf_artifact_id",
    ),
    "ARTIFACT_INDEX": (
        "artifact_id",
        "artifact_digest",
        "byte_count",
        "media_type",
        "ingest_state",
    ),
    "RESEARCH_CASE_LINEAGE": (
        "lineage_id",
        "lineage_mode",
        "source_version",
        "lineage_state",
        "lineage_digest",
    ),
    "CLEAN_ROOM_INPUT_MANIFEST": (
        "manifest_id",
        "lineage_id",
        "input_artifact_ids",
        "manifest_digest",
        "lineage_mode",
    ),
    "CERTIFICATE_IMPORT_REPORT": (
        "report_id",
        "lineage_id",
        "certificate_artifact_id",
        "verifier_profile_id",
        "verification_outcome",
    ),
    "PRODUCT_RECEIPT": (
        "receipt_id",
        "receipt_state",
        "request_id",
        "receipt_version",
        "revision_after",
    ),
    "JOB": ("job_id", "job_type", "job_state", "retry_safety", "result_receipt_id"),
    "DEPLOYMENT_STATUS": (
        "deployment_id",
        "deployment_state",
        "probe_run_id",
        "capability_keys",
        "fault_codes",
    ),
    "DEPLOYMENT_JOB": (
        "deployment_job_id",
        "deployment_id",
        "job_type",
        "job_state",
        "execution_receipt_id",
    ),
    "BACKUP_STATUS": (
        "backup_id",
        "deployment_id",
        "backup_state",
        "backup_artifact_id",
        "backup_digest",
    ),
    "ADMIN_HEALTH": (
        "health_report_id",
        "deployment_id",
        "overall_state",
        "probe_run_id",
        "fault_codes",
    ),
    "USAGE": (
        "usage_bucket_id",
        "period_start",
        "period_end",
        "resource_type",
        "quantity_microunits",
    ),
}

_STRING_IDS = {
    "stable_source_id",
    "tool_profile_id",
    "worker_subject_id",
    "verifier_profile_id",
    "usage_bucket_id",
}
_STRING_ARRAYS = {
    "changed_regions",
    "source_kinds",
    "allowed_command_types",
    "capabilities",
    "capability_keys",
    "fault_codes",
}


def _domain_field_schema(field: str) -> Schema:
    if field == "source_mode":
        return {"enum": ["LIVE_QUERY", "REPLAYED_SNAPSHOT"]}
    if field == "lineage_mode":
        return {"enum": ["CLEAN_ROOM_REDISCOVERY", "CERTIFICATE_IMPORT", "N2_HISTORICAL_MIGRATION"]}
    if field == "receipt_state":
        return {"enum": ["PENDING", "DECIDED", "OUTCOME_UNKNOWN"]}
    if field == "graph_mode":
        return {"enum": ["VERIFIED", "RESEARCH_HISTORY"]}
    if field == "dependable":
        return {"type": "boolean"}
    if field in _STRING_ARRAYS:
        return {"type": "array", "uniqueItems": True, "items": {"type": "string", "minLength": 1}}
    if field.endswith("_ids"):
        return {"type": "array", "uniqueItems": True, "items": {"$ref": "#/$defs/uuid"}}
    if field.endswith("_digest") or field.endswith("_digests"):
        if field.endswith("_digests"):
            return {"type": "array", "uniqueItems": True, "items": {"$ref": "#/$defs/hash"}}
        return {"$ref": "#/$defs/hash"}
    if field.endswith(("_count", "_revision", "_version", "_microunits")) or field in {
        "distance",
        "byte_count",
        "revision_after",
        "observed_revision",
        "finalized_revision",
        "expected_revision",
        "expected_contract_version",
    }:
        return {"$ref": "#/$defs/nat"}
    if field.endswith("_id") and field not in _STRING_IDS:
        return {"$ref": "#/$defs/uuid"}
    return {"type": "string", "minLength": 1}


def strengthen_query_results(schema: Schema) -> None:
    defs = schema["$defs"]
    for branch in defs["queryResult"]["oneOf"]:
        query_type = branch["properties"]["result_type"]["const"]
        fields = RESULT_DOMAIN_FIELDS.get(query_type)
        if fields is None:
            continue
        name = "resultDomain" + "".join(part.title() for part in query_type.lower().split("_"))
        defs[name] = {
            "type": "object",
            "additionalProperties": False,
            "required": list(fields),
            "properties": {field: _domain_field_schema(field) for field in fields},
        }
        result = branch["properties"]["result"]
        projection = result["properties"].get("entity")
        if projection is None:
            projection = result["properties"]["items"]["items"]
        projection["properties"]["domain"] = {"$ref": f"#/$defs/{name}"}
        if "domain" not in projection["required"]:
            projection["required"].append("domain")


def _rewrite_refs(value: Any) -> Any:
    if isinstance(value, list):
        return [_rewrite_refs(item) for item in value]
    if not isinstance(value, dict):
        return value
    rewritten = {key: _rewrite_refs(item) for key, item in value.items()}
    ref = rewritten.get("$ref")
    if isinstance(ref, str) and ref.startswith("#/$defs/"):
        rewritten["$ref"] = "#/$defs/query_" + ref.removeprefix("#/$defs/")
    return rewritten


def _query_type(branch: Schema, defs: dict[str, Schema]) -> tuple[str, str, str]:
    scope_ref = branch["properties"]["scope"]["$ref"]
    scope_kind = {
        "#/$defs/runScope": "RUN",
        "#/$defs/globalScope": "GLOBAL",
        "#/$defs/deploymentScope": "DEPLOYMENT",
    }[scope_ref]
    query_ref = branch["properties"]["query"]["$ref"].removeprefix("#/$defs/")
    query_type = defs[query_ref]["properties"]["type"]["const"]
    return query_type, scope_kind, query_ref


def _payload_fields(payload: Schema) -> tuple[list[str], list[str]]:
    variants = payload.get("oneOf", [payload])
    names: set[str] = set()
    required_sets: list[set[str]] = []
    for variant in variants:
        names.update(variant["properties"])
        required_sets.append(set(variant.get("required", [])))
    required = set.intersection(*required_sets)
    return sorted(required), sorted(names - required)


def metadata(root: Path) -> dict[str, dict[str, Any]]:
    schema = json.loads((root / "docs/spec/product/query.schema.json").read_text(encoding="utf-8"))
    defs = schema["$defs"]
    result: dict[str, dict[str, Any]] = {}
    for branch in defs["querySpec"]["oneOf"]:
        query_type, scope_kind, query_ref = _query_type(branch, defs)
        payload = defs[query_ref]["properties"]["payload"]
        required, optional = _payload_fields(payload)
        entry = result.setdefault(
            query_type,
            {
                "scope_kinds": [],
                "required_payload_fields": required,
                "optional_payload_fields": optional,
            },
        )
        if scope_kind not in entry["scope_kinds"]:
            entry["scope_kinds"].append(scope_kind)
    for entry in result.values():
        entry["scope_kinds"].sort()
    return dict(sorted(result.items()))


def result_metadata(root: Path) -> dict[str, dict[str, Any]]:
    schema = json.loads((root / "docs/spec/product/query.schema.json").read_text(encoding="utf-8"))
    defs = schema["$defs"]
    result: dict[str, dict[str, Any]] = {}
    for branch in defs["queryResult"]["oneOf"]:
        query_type = branch["properties"]["result_type"]["const"]
        scope_kind = branch["properties"]["scope_kind"]["const"]
        container = branch["properties"]["result"]
        if "$ref" in container:
            graph = defs[container["$ref"].rsplit("/", 1)[1]]
            kind = "graph"
            projection_required = list(graph.get("required", []))
            projection_fields = list(graph.get("properties", {}))
            domain_required: list[str] = []
            domain_fields: list[str] = []
        else:
            kind = "entity" if "entity" in container["properties"] else "list"
            projection = container["properties"].get("entity")
            if projection is None:
                projection = container["properties"]["items"]["items"]
            projection_required = list(projection.get("required", []))
            projection_fields = list(projection.get("properties", {}))
            domain = projection.get("properties", {}).get("domain")
            if isinstance(domain, dict) and "$ref" in domain:
                domain_schema = defs[domain["$ref"].rsplit("/", 1)[1]]
                domain_required = list(domain_schema.get("required", []))
                domain_fields = list(domain_schema.get("properties", {}))
            else:
                domain_required = []
                domain_fields = []
        entry = result.setdefault(
            query_type,
            {
                "scope_kinds": [],
                "result_kind": kind,
                "required_projection_fields": projection_required,
                "projection_fields": projection_fields,
                "required_domain_fields": domain_required,
                "domain_fields": domain_fields,
            },
        )
        if (
            entry["result_kind"] != kind
            or entry["required_projection_fields"] != projection_required
            or entry["projection_fields"] != projection_fields
            or entry["required_domain_fields"] != domain_required
            or entry["domain_fields"] != domain_fields
        ):
            raise ValueError(f"query result contract drifts across scopes: {query_type}")
        entry["scope_kinds"].append(scope_kind)
    for entry in result.values():
        entry["scope_kinds"].sort()
    return dict(sorted(result.items()))


def generate(root: Path) -> None:
    query_path = root / "docs/spec/product/query.schema.json"
    query_schema = json.loads(query_path.read_text(encoding="utf-8"))
    strengthen_query_results(query_schema)
    query_path.write_text(
        json.dumps(query_schema, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    envelope_path = root / "docs/spec/product/envelope.schema.json"
    envelope = json.loads(envelope_path.read_text(encoding="utf-8"))
    envelope["$defs"].pop("queryPayload", None)
    for name in [key for key in envelope["$defs"] if key.startswith("query_")]:
        del envelope["$defs"][name]
    for name, definition in query_schema["$defs"].items():
        envelope["$defs"]["query_" + name] = _rewrite_refs(copy.deepcopy(definition))
    envelope["$defs"]["querySpec"] = envelope["$defs"].pop("query_querySpec")
    envelope["$defs"]["queryResult"] = envelope["$defs"].pop("query_queryResult")
    result_ref = {"$ref": "#/$defs/queryResult"}
    if result_ref not in envelope["oneOf"]:
        envelope["oneOf"].append(result_ref)
    envelope_path.write_text(
        json.dumps(envelope, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
