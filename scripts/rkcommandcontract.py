"""Generate the closed ProductCommand contract and SDK command metadata."""
# ruff: noqa: E501

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

Schema = dict[str, Any]


def ref(name: str) -> Schema:
    return {"$ref": f"#/$defs/{name}"}


def arr(item: Schema, minimum: int = 0) -> Schema:
    return {"type": "array", "items": item, "minItems": minimum, "uniqueItems": True}


def obj(required: dict[str, Schema], optional: dict[str, Schema] | None = None) -> Schema:
    props = dict(required)
    props.update(optional or {})
    return {
        "type": "object",
        "additionalProperties": False,
        "required": list(required),
        "properties": props,
    }


TEXT = {"type": "string", "minLength": 1}
UUID = ref("uuid")
HASH = ref("hash")
NAT = ref("safeNatural")
POS = ref("safePositive")
BOOL = {"type": "boolean"}
IDS = arr(UUID)
TEXTS = arr(TEXT)
ART = ref("artifactBinding")
ARTS = arr(ART)
BUDGET = ref("budget")
CONTRACT = ref("contractDraft")
ENUMS: dict[str, Schema] = {
    "claim_kind": {"enum": ["ROOT", "LEMMA", "DEFINITION", "COUNTEREXAMPLE", "COMPUTATION"]},
    "review_type": {
        "enum": ["ATOMIC", "COMPOSITION", "PAPER", "MATERIAL", "LITERATURE", "SEMANTIC_FREEZE"]
    },
    "final_outcome": {"enum": ["PROVED", "DISPROVED", "UNRESOLVED"]},
    "authority_ceiling": {
        "enum": ["NO_FACT_GRAPH_WRITE", "SOFT_TOOL_RESULT", "CERTIFICATE_REQUIRES_VALIDATION"]
    },
    "applicability_verdict": {
        "enum": ["APPLICABLE", "INAPPLICABLE", "CONDITIONAL", "INSUFFICIENT_CONTEXT"]
    },
    "ablation_group": {"enum": ["direct", "near", "far-random", "far-retrieval", "full-RK"]},
}


def fields(spec: str) -> dict[str, Schema]:
    result: dict[str, Schema] = {}
    kinds = {
        "t": TEXT,
        "u": UUID,
        "h": HASH,
        "n": NAT,
        "p": POS,
        "b": BOOL,
        "U": IDS,
        "T": TEXTS,
        "a": ART,
        "A": ARTS,
        "g": BUDGET,
        "c": CONTRACT,
        "y": {"const": True},
        "z": {"const": False},
    }
    for token in spec.split():
        name, kind = token.rsplit(":", 1)
        result[name] = ENUMS[kind[1:]] if kind.startswith("e") else kinds[kind]
    return result


def payload(required: str, optional: str = "") -> Schema:
    return obj(fields(required), fields(optional))


def tagged(field: str, variants: dict[str, str]) -> Schema:
    return {
        "oneOf": [obj({field: {"const": tag}, **fields(spec)}) for tag, spec in variants.items()]
    }


# scope kinds, execution class, authority boundary, exact payload schema.
COMMANDS: dict[str, tuple[tuple[str, ...], str, str, Schema]] = {
    "CREATE_RESEARCH": (
        ("GLOBAL",),
        "IMMEDIATE",
        "KERNEL_GUARDED_CREATE",
        payload(
            "question:t contract_draft:c owner:t labels:T initial_budget:g",
            "title:t material_artifacts:A",
        ),
    ),
    "CONFIRM_CONTRACT": (
        ("RUN",),
        "IMMEDIATE",
        "KERNEL_GUARDED_MATH_STATE",
        payload("contract_digest:h material_anchor_ids:U confirmation_note:t"),
    ),
    "AMEND_CONTRACT": (
        ("RUN",),
        "IMMEDIATE",
        "KERNEL_GUARDED_INVALIDATION",
        payload("base_contract_version:p amendment_artifact:a impact_acknowledgement:t"),
    ),
    "APPLY_ROUTE_PLAN": (
        ("RUN",),
        "IMMEDIATE",
        "ORCHESTRATION_ONLY",
        tagged(
            "action",
            {
                "APPROVE": "route_plan_id:u plan_digest:h",
                "START": "route_plan_id:u",
                "PAUSE": "route_plan_id:u reason:t",
                "STOP": "route_plan_id:u reason:t",
                "SET_PRIORITY": "route_plan_id:u priority:p",
                "SET_BUDGET": "route_plan_id:u budget:g",
            },
        ),
    ),
    "START_RESEARCH": (
        ("RUN",),
        "DURABLE_JOB",
        "ORCHESTRATION_ONLY",
        payload("route_plan_id:u checkpoint_id:u"),
    ),
    "PAUSE_RESEARCH": (
        ("RUN",),
        "IMMEDIATE",
        "ORCHESTRATION_ONLY",
        payload("reason:t checkpoint_required:y"),
    ),
    "RESUME_RESEARCH": (
        ("RUN",),
        "DURABLE_JOB",
        "ORCHESTRATION_ONLY",
        payload("checkpoint_id:u resume_invalidated_items:z"),
    ),
    "CANCEL_RESEARCH": (
        ("RUN",),
        "IMMEDIATE",
        "ORCHESTRATION_ONLY",
        payload("reason:t cancel_pending_external_attempts:b"),
    ),
    "SUBMIT_GUIDANCE": (
        ("RUN",),
        "IMMEDIATE",
        "NO_FACT_GRAPH_WRITE",
        payload("target_checkpoint_id:u goal:t guidance_text:t affected_route_ids:U"),
    ),
    "WITHDRAW_GUIDANCE": (
        ("RUN",),
        "IMMEDIATE",
        "NO_FACT_GRAPH_WRITE",
        payload("guidance_id:u reason:t"),
    ),
    "SUBMIT_CLAIM": (
        ("RUN",),
        "IMMEDIATE",
        "KERNEL_VALIDATION_GATE_REQUIRED",
        payload(
            "statement:t claim_kind:eclaim_kind proof_or_evidence_artifacts:A predecessor_fact_ids:U source_binding_artifact:a work_item_id:u worker_run_id:u attempt_id:u",
            "route_id:u supersedes_claim_id:u public_summary:t",
        ),
    ),
    "IMPORT_VERIFICATION": (
        ("RUN",),
        "IMMEDIATE",
        "KERNEL_VALIDATION_GATE_REQUIRED",
        payload("review_task_id:u signed_review_artifact:a target_digest:h verifier_receipt_ids:U"),
    ),
    "CONFIRM_REVOKE": (
        ("RUN",),
        "IMMEDIATE",
        "KERNEL_GUARDED_INVALIDATION",
        payload(
            "fact_id:u preview_revision:n contract_version:p target_fact_digest:h affected_fact_ids:U reopened_obligation_ids:U preserved_sibling_ids:U reason_artifact:a"
        ),
    ),
    "REGISTER_BRIDGE_SPEC": (
        ("RUN",),
        "IMMEDIATE",
        "KERNEL_VALIDATION_GATE_REQUIRED",
        payload(
            "bridge_opportunity_id:u direction:t source_domain:t target_domain:t mapping_artifact:a assumption_loss_artifact:a translation_artifact:a target_review_artifact:a composition_obligation_ids:U"
        ),
    ),
    "SUBMIT_CLOSURE_WITNESS": (
        ("RUN",),
        "IMMEDIATE",
        "KERNEL_CLOSURE_GATE_REQUIRED",
        payload(
            "terminal_root_id:u selected_subgraph_digest:h graph_revision:n edge_justification_artifacts:A bridge_spec_ids:U composition_obligation_ids:U verification_review_ids:U"
        ),
    ),
    "CREATE_REVIEW_TASK": (
        ("RUN",),
        "IMMEDIATE",
        "NO_FACT_GRAPH_WRITE",
        payload(
            "review_type:ereview_type target_entity_ids:U target_digest:h author_subject_ids:T assignee_subject_id:t independence_constraints:T expires_at:t"
        ),
    ),
    "CLAIM_REVIEW_TASK": (
        ("RUN",),
        "IMMEDIATE",
        "NO_FACT_GRAPH_WRITE",
        payload("review_task_id:u task_binding_digest:h"),
    ),
    "SUBMIT_REVIEW": (
        ("RUN",),
        "IMMEDIATE",
        "SIGNED_REVIEW_ONLY",
        payload(
            "review_task_id:u signed_review_artifact:a review_schema_version:t task_binding_digest:h"
        ),
    ),
    "GENERATE_CANDIDATE_TEX": (
        ("RUN",),
        "DURABLE_JOB",
        "CLOSED_RUN_PUBLICATION_ONLY",
        payload(
            "finalized_revision:n terminal_root_id:u dependency_closure_digest:h template_artifact:a"
        ),
    ),
    "SUBMIT_PAPER_REVIEW": (
        ("RUN",),
        "IMMEDIATE",
        "SIGNED_PAPER_REVIEW_ONLY",
        payload(
            "review_task_id:u signed_paper_review_artifact:a candidate_tex_artifact:a paper_review_schema_version:t"
        ),
    ),
    "FINALIZE_RESEARCH": (
        ("RUN",),
        "IMMEDIATE",
        "KERNEL_FINALIZATION_GATE_REQUIRED",
        payload("final_outcome:efinal_outcome terminal_root_id:u closure_witness_id:u"),
    ),
    "COMPILE_FINAL_PDF": (
        ("RUN",),
        "DURABLE_JOB",
        "CLOSED_RUN_PUBLICATION_ONLY",
        payload(
            "candidate_tex_artifact:a paper_review_id:u compiler_profile_id:t compiler_profile_version:t"
        ),
    ),
    "CONFIRM_MATERIAL_EXTRACTION": (
        ("RUN",),
        "IMMEDIATE",
        "NO_FACT_GRAPH_WRITE",
        payload(
            "material_extraction_id:u source_artifact:a extraction_digest:h reviewed_page_numbers:T corrections_artifact:a"
        ),
    ),
    "RUN_LITERATURE_QUERY": (
        ("RUN",),
        "EXTERNAL_SIDE_EFFECT",
        "NO_MATH_OR_NOVELTY_WRITE",
        payload(
            "research_question:t connector_profile_ids:T query_text:t coverage_boundary:t target_entity_ids:U capture_raw_response:y"
        ),
    ),
    "REPLAY_SOURCE_SNAPSHOT": (
        ("RUN",),
        "DURABLE_JOB",
        "NO_MATH_OR_NOVELTY_WRITE",
        payload("source_snapshot_id:u expected_response_sha256:h reconfirm_external_index:z"),
    ),
    "REVIEW_THEOREM_APPLICABILITY": (
        ("RUN",),
        "IMMEDIATE",
        "NO_FACT_GRAPH_WRITE",
        payload(
            "theorem_source_binding:a target_claim_id:u verdict:eapplicability_verdict assumption_mapping_artifact:a signed_review_artifact:a"
        ),
    ),
    "FREEZE_PROBLEM_POOL": (
        ("GLOBAL",),
        "IMMEDIATE",
        "NO_MATH_OR_NOVELTY_WRITE",
        payload(
            "pool_name:t frozen_at:t subject_classes:T version_rule:t inclusion_rules:T exclusion_rules:T source_snapshot_ids:U candidate_denominator:p semantic_audit_artifact:a"
        ),
    ),
    "BATCH_CREATE_RESEARCH": (
        ("GLOBAL",),
        "DURABLE_JOB",
        "KERNEL_GUARDED_CREATE",
        payload(
            "problem_pool_id:u problem_candidate_ids:U contract_template_artifact:a per_run_budget:g labels:T"
        ),
    ),
    "REGISTER_BRIDGE_OPPORTUNITY": (
        ("RUN",),
        "IMMEDIATE",
        "NO_FACT_GRAPH_WRITE",
        payload(
            "normalized_source_problem:t candidate_target_domain:t domain_distance_basis:t source_method_maturity:t target_domain_absence_evidence:t native_tool_advantage:t expected_certificate_compression:t mapping_and_assumption_losses:T round_trip_and_source_review_cost:t fastest_death_test:t selection_rationale:t source_snapshot_ids:U"
        ),
    ),
    "ASSIGN_ABLATION": (
        ("RUN",),
        "DURABLE_JOB",
        "RESEARCH_HYPOTHESIS_ONLY",
        payload(
            "ablation_plan_id:u group:eablation_group problem_candidate_ids:U model_profile:t tool_profile:t candidate_count:p budget:g final_verifier_profile:t"
        ),
    ),
    "IMPORT_RESEARCH_LINEAGE": (
        ("RUN",),
        "DURABLE_JOB",
        "CANDIDATE_ONLY_NO_FACT_GRAPH_WRITE",
        tagged(
            "mode",
            {
                "CLEAN_ROOM_REDISCOVERY": "source_project_id:t source_versions:T clean_room_input_manifest:a historical_conclusions_injected:z",
                "IMPORTED_CERTIFICATE_VERIFICATION": "source_project_id:t source_versions:T imported_artifacts:A certificate_import_report:a verifier_receipt_ids:U",
                "HISTORICAL_CANDIDATE_MIGRATION": "source_project_id:t source_versions:T candidate_artifacts:A promote_as_verified:z",
            },
        ),
    ),
    "CREATE_COMPUTE_TASK": (
        ("RUN",),
        "DURABLE_JOB",
        "SOFT_TOOL_RESULT_ONLY",
        payload(
            "script_artifact:a input_artifacts:A environment_profile_id:t environment_profile_version:t parameters_artifact:a limits_artifact:a expected_output_names:T"
        ),
    ),
    "RUN_TOOL": (
        ("RUN",),
        "EXTERNAL_SIDE_EFFECT",
        "TOOL_RECEIPT_NOT_AUTHORITY",
        payload(
            "tool_id:t tool_version:t function_name:t function_schema_digest:h input_artifact_ids:U arguments_artifact:a authority_ceiling:eauthority_ceiling"
        ),
    ),
    "CANCEL_JOB": (
        ("RUN", "DEPLOYMENT"),
        "IMMEDIATE",
        "EXECUTION_STATE_ONLY",
        payload("job_id:u reason:t"),
    ),
    "RETRY_UNKNOWN_OUTCOME": (
        ("RUN", "DEPLOYMENT"),
        "EXTERNAL_SIDE_EFFECT",
        "NO_IMPLICIT_MATH_DECISION",
        payload(
            "outcome_unknown_receipt_id:u unknown_external_call_ref:t resolution_strategy:t evidence_artifact_ids:U"
        ),
    ),
    "DEPLOYMENT_OPERATION": (
        ("DEPLOYMENT",),
        "DURABLE_JOB",
        "DEPLOYMENT_STATE_ONLY",
        tagged(
            "action",
            {
                "BOOTSTRAP": "data_root:t configuration_artifact:a",
                "UPDATE_CONFIG": "configuration_artifact:a expected_config_digest:h",
                "PROBE_CAPABILITY": "capability_profile_id:t",
                "INVENTORY_HARDWARE": "inventory_profile_id:t",
                "REGISTER_COMPONENT": "component_manifest:a",
                "START_DAEMON": "daemon_id:t",
                "STOP_DAEMON": "daemon_id:t drain_timeout_seconds:p",
                "BACKUP": "backup_target:t include_cas:b include_configuration:b",
                "RESTORE": "backup_artifact:a new_data_root:t",
                "UPGRADE_PREFLIGHT": "release_manifest:a",
                "MIGRATE_SCHEMA": "release_manifest:a backup_id:u",
                "EXPORT_DIAGNOSTICS": "redact_credentials:b",
            },
        ),
    ),
}


def _metadata(schema: Schema) -> tuple[list[str], list[str]]:
    variants = schema.get("oneOf", [schema])
    names: set[str] = set()
    reqs: list[set[str]] = []
    for variant in variants:
        names.update(variant["properties"])
        reqs.append(set(variant["required"]))
    required = set.intersection(*reqs)
    return sorted(required), sorted(names - required)


def build_schema() -> Schema:
    defs: dict[str, Schema] = {
        "uuid": {"type": "string", "format": "uuid"},
        "hash": {"type": "string", "pattern": "^[0-9a-f]{64}$"},
        "safeNatural": {"type": "integer", "minimum": 0, "maximum": 9007199254740991},
        "safePositive": {"type": "integer", "minimum": 1, "maximum": 9007199254740991},
        "artifactBinding": obj({"artifact_id": UUID, "sha256": HASH}),
        "budget": obj({"microunits": NAT, "wall_seconds": POS}),
        "contractDraft": obj(
            {
                "objects": TEXTS,
                "domain": TEXT,
                "quantifiers": TEXTS,
                "boundary_conditions": TEXTS,
                "exact_negation": TEXT,
                "allowed_tools": TEXTS,
                "success_conditions": TEXTS,
            }
        ),
        "artifactRef": obj(
            {"artifact_id": UUID, "sha256": HASH, "byte_count": NAT, "media_type": TEXT},
            {"logical_name": TEXT},
        ),
        "globalScope": obj({"kind": {"const": "GLOBAL"}, "deployment_id": UUID}),
        "runScope": obj(
            {
                "kind": {"const": "RUN"},
                "run_id": UUID,
                "expected_revision": NAT,
                "expected_contract_version": POS,
            }
        ),
        "deploymentScope": obj(
            {
                "kind": {"const": "DEPLOYMENT"},
                "deployment_id": UUID,
                "expected_deployment_revision": NAT,
            }
        ),
    }
    envelopes: list[Schema] = []
    scope_refs = {"GLOBAL": "globalScope", "RUN": "runScope", "DEPLOYMENT": "deploymentScope"}
    for name, (scopes, execution, authority, body) in COMMANDS.items():
        suffix = name.title().replace("_", "")
        command_name = f"productCommand{suffix}Body"
        envelope_name = f"productCommand{suffix}"
        defs[command_name] = {
            **obj({"type": {"const": name}, "payload": body}),
            "x-rk-scope-kinds": list(scopes),
            "x-rk-execution-class": execution,
            "x-rk-authority-boundary": authority,
        }
        scope: Schema = (
            ref(scope_refs[scopes[0]])
            if len(scopes) == 1
            else {"oneOf": [ref(scope_refs[x]) for x in scopes]}
        )
        defs[envelope_name] = obj(
            {
                "schema_version": {"const": "rk.product.command.v1"},
                "request_id": UUID,
                "scope": scope,
                "command": ref(command_name),
                "artifact_inputs": arr(ref("artifactRef")),
            }
        )
        envelopes.append(ref(envelope_name))
    defs["productCommand"] = {"oneOf": envelopes}
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": "https://local.ai4math/rk/product/command.schema.json",
        "title": "RK ProductCommand strict tagged union v1",
        "$ref": "#/$defs/productCommand",
        "$defs": defs,
    }


def metadata() -> dict[str, dict[str, Any]]:
    result = {}
    for name, (scopes, execution, authority, body) in COMMANDS.items():
        required, optional = _metadata(body)
        result[name] = {
            "scope_kinds": list(scopes),
            "execution_class": execution,
            "authority_boundary": authority,
            "required_payload_fields": required,
            "optional_payload_fields": optional,
        }
    return result


def generate(root: Path) -> None:
    schema = build_schema()
    path = root / "docs/spec/product/command.schema.json"
    path.write_text(json.dumps(schema, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    envelope_path = root / "docs/spec/product/envelope.schema.json"
    envelope = json.loads(envelope_path.read_text(encoding="utf-8"))
    envelope["$defs"].pop("commandPayload", None)
    envelope["$defs"].update(schema["$defs"])
    envelope_path.write_text(
        json.dumps(envelope, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
