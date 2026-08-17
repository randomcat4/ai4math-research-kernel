from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
from jsonschema import Draft202012Validator, FormatChecker

ROOT = Path(__file__).resolve().parents[1]
PRODUCT_SPEC = ROOT / "docs" / "spec" / "product"


def _validator(name: str) -> Draft202012Validator:
    schema = json.loads((PRODUCT_SPEC / name).read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(schema)
    return Draft202012Validator(schema, format_checker=FormatChecker())


def test_product_command_rejects_identity_injection() -> None:
    validator = _validator("envelope.schema.json")
    command = {
        "schema_version": "rk.product.command.v1",
        "request_id": "7f857a15-bddb-4238-aa88-6dbeaec50f7a",
        "scope": {"kind": "GLOBAL", "deployment_id": "f1d10eee-4da4-49cf-ae78-870dff1c08ba"},
        "command": {"type": "CREATE_RESEARCH", "payload": {}},
        "artifact_inputs": [],
        "actor": "forged-admin",
    }
    assert list(validator.iter_errors(command))


@pytest.mark.parametrize(
    "payload",
    [
        {"actor": "forged"},
        {"nested": {"role": "ADMIN"}},
        {"items": [{"capability": "forged"}]},
        {"principal_subject_id": "forged"},
    ],
)
def test_raw_wire_rejects_nested_identity_injection(payload: dict[str, object]) -> None:
    validator = _validator("envelope.schema.json")
    command = {
        "schema_version": "rk.product.command.v1",
        "request_id": "7f857a15-bddb-4238-aa88-6dbeaec50f7a",
        "scope": {"kind": "GLOBAL", "deployment_id": "f1d10eee-4da4-49cf-ae78-870dff1c08ba"},
        "command": {"type": "CREATE_RESEARCH", "payload": payload},
        "artifact_inputs": [],
    }
    assert list(validator.iter_errors(command))


def test_raw_wire_rejects_integer_that_typescript_cannot_preserve() -> None:
    validator = _validator("envelope.schema.json")
    command = {
        "schema_version": "rk.product.command.v1",
        "request_id": "7f857a15-bddb-4238-aa88-6dbeaec50f7a",
        "scope": {"kind": "GLOBAL", "deployment_id": "f1d10eee-4da4-49cf-ae78-870dff1c08ba"},
        "command": {"type": "CREATE_RESEARCH", "payload": {"budget": 9_007_199_254_740_992}},
        "artifact_inputs": [],
    }
    assert list(validator.iter_errors(command))


def test_product_command_rejects_unknown_variant() -> None:
    validator = _validator("envelope.schema.json")
    command = {
        "schema_version": "rk.product.command.v1",
        "request_id": "7f857a15-bddb-4238-aa88-6dbeaec50f7a",
        "scope": {"kind": "GLOBAL", "deployment_id": "f1d10eee-4da4-49cf-ae78-870dff1c08ba"},
        "command": {"type": "FORCE_SUCCESS", "payload": {}},
        "artifact_inputs": [],
    }
    assert list(validator.iter_errors(command))


@pytest.mark.parametrize("state", ["PENDING", "DECIDED", "OUTCOME_UNKNOWN"])
def test_receipt_state_has_exact_required_evidence(state: str) -> None:
    validator = _validator("envelope.schema.json")
    receipt = {
        "schema_version": "rk.product.receipt.v1",
        "receipt_id": "f019c6e5-c86f-410f-ac72-9a0ad1afcb32",
        "receipt_version": 1,
        "request_id": "7f857a15-bddb-4238-aa88-6dbeaec50f7a",
        "scope": {"kind": "GLOBAL", "deployment_id": "f1d10eee-4da4-49cf-ae78-870dff1c08ba"},
        "updated_at": "2026-08-13T18:00:00Z",
        "state": state,
    }
    assert list(validator.iter_errors(receipt))


def test_pending_receipt_requires_job_and_forbids_decision() -> None:
    validator = _validator("envelope.schema.json")
    receipt = {
        "schema_version": "rk.product.receipt.v1",
        "receipt_id": "f019c6e5-c86f-410f-ac72-9a0ad1afcb32",
        "receipt_version": 1,
        "request_id": "7f857a15-bddb-4238-aa88-6dbeaec50f7a",
        "scope": {"kind": "GLOBAL", "deployment_id": "f1d10eee-4da4-49cf-ae78-870dff1c08ba"},
        "updated_at": "2026-08-13T18:00:00Z",
        "state": "PENDING",
        "job_id": "1d6adcee-55d5-459d-a66d-d199b3d61b95",
    }
    assert not list(validator.iter_errors(receipt))
    receipt["decision"] = {}
    assert list(validator.iter_errors(receipt))


def test_outcome_unknown_cannot_carry_mathematical_decision() -> None:
    validator = _validator("envelope.schema.json")
    receipt = {
        "schema_version": "rk.product.receipt.v1",
        "receipt_id": "f019c6e5-c86f-410f-ac72-9a0ad1afcb32",
        "receipt_version": 2,
        "request_id": "7f857a15-bddb-4238-aa88-6dbeaec50f7a",
        "scope": {"kind": "GLOBAL", "deployment_id": "f1d10eee-4da4-49cf-ae78-870dff1c08ba"},
        "updated_at": "2026-08-13T18:00:00Z",
        "state": "OUTCOME_UNKNOWN",
        "unknown_external_call_ref": "provider-call-17",
    }
    assert not list(validator.iter_errors(receipt))
    receipt["decided_at"] = "2026-08-13T18:00:01Z"
    assert list(validator.iter_errors(receipt))


def test_generated_sdk_surfaces_are_current() -> None:
    targets = [
        ROOT / "sdk" / "python" / "rk_product" / "types.py",
        ROOT / "sdk" / "typescript" / "src" / "types.ts",
    ]
    before = [target.read_bytes() for target in targets]
    subprocess.run([sys.executable, str(ROOT / "scripts" / "rkgenerateproduct.py")], check=True)
    assert [target.read_bytes() for target in targets] == before


def test_composition_review_requires_all_six_parts() -> None:
    validator = _validator("review.schema.json")
    review = {
        "schema_version": "rk.product.review.v1",
        "review_type": "COMPOSITION",
        "review_task_id": "76e89cf5-2d2e-461b-b03d-c4ed076fd6c1",
        "reviewer_subject_id": "reviewer:one",
        "author_subject_ids": ["worker:one"],
        "run_id": "c73f6387-2ea0-487a-aebf-dd2b8dad8ec2",
        "contract_version": 1,
        "research_revision": 8,
        "target_digest": "a" * 64,
        "verdict": "ACCEPT",
        "signed_at": "2026-08-13T18:00:00Z",
        "closure_witness_id": "2999d48e-d478-4094-b212-d33e061a448a",
        "proof_scope_valid": True,
        "coverage": True,
        "compatibility": True,
        "invariant": True,
        "progress": True,
        "boundary": True,
    }
    assert list(validator.iter_errors(review))


@pytest.mark.parametrize(
    "schema_name",
    [
        "activity.schema.json",
        "envelope.schema.json",
        "graph.schema.json",
        "review.schema.json",
        "support.schema.json",
        "workflow.schema.json",
    ],
)
def test_every_product_schema_is_valid_draft_2020_12(schema_name: str) -> None:
    _validator(schema_name)


def test_literature_graph_preserves_edge_provenance() -> None:
    validator = _validator("workflow.schema.json")
    graph = {
        "schema_version": "rk.product.literature_graph.v1",
        "object_id": "d730b48b-7f4c-4e8b-809a-417f20c4e6db",
        "created_by_subject_id": "literature-reviewer:one",
        "scope": {"run_id": "c73f6387-2ea0-487a-aebf-dd2b8dad8ec2"},
        "created_cursor": 3,
        "created_at": "2026-08-13T18:00:00Z",
        "evidence_class": "EXTERNAL_DEPENDENCY",
        "authority_effect": "NO_FACT_GRAPH_WRITE",
        "nodes": [],
        "edges": [],
    }
    assert not list(validator.iter_errors(graph))


def test_ablation_plan_requires_exact_five_frozen_groups() -> None:
    validator = _validator("workflow.schema.json")
    plan = {
        "schema_version": "rk.product.ablation_plan.v1",
        "object_id": "d730b48b-7f4c-4e8b-809a-417f20c4e6db",
        "created_by_subject_id": "main:one",
        "scope": {"deployment_id": "f1d10eee-4da4-49cf-ae78-870dff1c08ba"},
        "created_cursor": 4,
        "created_at": "2026-08-13T18:00:00Z",
        "evidence_class": "RESEARCH_HYPOTHESIS",
        "authority_effect": "NO_FACT_GRAPH_WRITE",
        "problem_pool_id": "a89e7cf5-2d2e-461b-b03d-c4ed076fd6c1",
        "model_profile": {"id": "frozen-model"},
        "tool_profile": {"id": "frozen-tools"},
        "candidate_count": 10,
        "budget": {"microunits": 100},
        "final_verifier": {"id": "source-verifier"},
        "groups": ["direct", "near", "far-random", "far-retrieval"],
    }
    assert list(validator.iter_errors(plan))


def _artifact(seed: str = "a") -> dict[str, object]:
    return {
        "artifact_id": "77e89cf5-2d2e-461b-b03d-c4ed076fd6c1",
        "sha256": seed * 64,
        "byte_count": 17,
        "media_type": "application/json",
    }


def _attested(passed: bool = True) -> dict[str, object]:
    return {
        "passed": passed,
        "status": "HUMAN_ATTESTED",
        "conclusion": "independently checked",
        "evidence_refs": ["artifact:review-evidence"],
    }


def _signed_review(review_type: str) -> dict[str, object]:
    checks = {
        "ATOMIC": [
            "statement_correct",
            "proof_valid",
            "dependency_scope_valid",
            "evidence_sufficient",
        ],
        "COMPOSITION": [
            "proof_checked",
            "scope_checked",
            "coverage",
            "compatibility",
            "invariant",
            "progress",
            "boundary",
            "simultaneous_choice",
        ],
        "PAPER": [
            "statement_alignment",
            "proof_completeness",
            "citation_accuracy",
            "novelty_boundary",
            "artifact_binding",
            "outcome_alignment",
        ],
    }
    binding: dict[str, object] = {
        "run_id": "c73f6387-2ea0-487a-aebf-dd2b8dad8ec2",
        "kernel_revision": 8,
        "contract_version": 2,
        "target_id": "2999d48e-d478-4094-b212-d33e061a448a",
        "target_digest": "a" * 64,
    }
    if review_type == "COMPOSITION":
        binding["selected_subgraph_digest"] = "b" * 64
    review: dict[str, object] = {
        "schema_version": "rk.product.review.v1",
        "review_id": "66e89cf5-2d2e-461b-b03d-c4ed076fd6c1",
        "review_type": review_type,
        "review_task_id": "76e89cf5-2d2e-461b-b03d-c4ed076fd6c1",
        "verifier_identity_id": "verifier:managed:one",
        "reviewer_subject_id": "reviewer:one",
        "binding": binding,
        "independence": {
            "blind_review": True,
            "author_subject_ids": ["worker:one"],
            "saw_other_verdicts": False,
        },
        "verdict": "ACCEPT",
        "issued_at": "2026-08-13T18:00:00Z",
        "signature": {
            "algorithm": "HMAC_SHA256",
            "key_id": "reviewer-key-one",
            "signed_payload_sha256": "c" * 64,
            "value": "signed-value",
        },
        "checks": {name: _attested() for name in checks[review_type]},
    }
    if review_type == "COMPOSITION":
        review["closure_witness_id"] = "1999d48e-d478-4094-b212-d33e061a448a"
    if review_type == "PAPER":
        review.update(
            candidate_tex_artifact_id="3999d48e-d478-4094-b212-d33e061a448a",
            terminal_root_digest="d" * 64,
            dependency_closure_digest="e" * 64,
        )
    return review


@pytest.mark.parametrize("review_type", ["ATOMIC", "COMPOSITION", "PAPER"])
def test_signed_review_variants_require_exact_binding_and_attested_checks(review_type: str) -> None:
    validator = _validator("review.schema.json")
    review = _signed_review(review_type)
    assert not list(validator.iter_errors(review))

    review.pop("signature")
    assert list(validator.iter_errors(review))


def test_accept_review_cannot_hide_a_failed_check() -> None:
    validator = _validator("review.schema.json")
    review = _signed_review("COMPOSITION")
    checks = review["checks"]
    assert isinstance(checks, dict)
    checks["boundary"] = _attested(False)
    assert list(validator.iter_errors(review))


def test_composition_review_binds_selected_subgraph() -> None:
    validator = _validator("review.schema.json")
    review = _signed_review("COMPOSITION")
    binding = review["binding"]
    assert isinstance(binding, dict)
    binding.pop("selected_subgraph_digest")
    assert list(validator.iter_errors(review))


def _source_snapshot(mode: str) -> dict[str, object]:
    return {
        "schema_version": "rk.product.source_snapshot.v1",
        "object_id": "d730b48b-7f4c-4e8b-809a-417f20c4e6db",
        "created_by_subject_id": "literature-reviewer:one",
        "scope": {"run_id": "c73f6387-2ea0-487a-aebf-dd2b8dad8ec2"},
        "created_cursor": 3,
        "created_at": "2026-08-13T18:00:00Z",
        "evidence_class": "EXTERNAL_DEPENDENCY",
        "authority_effect": "NO_FACT_GRAPH_WRITE",
        "connector": "MATLAS",
        "endpoint": "https://leansearch.net/thm/search",
        "request_digest": "1" * 64,
        "response_artifact": _artifact("2"),
        "response_digest": "2" * 64,
        "queried_at": "2026-08-13T18:00:00Z",
        "visible_service_version": "visible-2026-08-13",
        "coverage": {"query": "finite group"},
        "mode": mode,
        "status": "SUCCEEDED",
    }


def test_live_query_and_snapshot_replay_have_disjoint_provenance() -> None:
    validator = _validator("workflow.schema.json")
    live = _source_snapshot("LIVE_QUERY")
    assert not list(validator.iter_errors(live))
    live["replayed_from_snapshot_id"] = "b730b48b-7f4c-4e8b-809a-417f20c4e6db"
    assert list(validator.iter_errors(live))

    replay = _source_snapshot("REPLAYED_SNAPSHOT")
    assert list(validator.iter_errors(replay))
    replay["replayed_from_snapshot_id"] = "b730b48b-7f4c-4e8b-809a-417f20c4e6db"
    replay["replayed_at"] = "2026-08-13T19:00:00Z"
    assert not list(validator.iter_errors(replay))


def _lineage(mode: str, project: str) -> dict[str, object]:
    return {
        "schema_version": "rk.product.research_case_lineage.v1",
        "object_id": "d730b48b-7f4c-4e8b-809a-417f20c4e6db",
        "created_by_subject_id": "main:one",
        "scope": {"run_id": "c73f6387-2ea0-487a-aebf-dd2b8dad8ec2"},
        "created_cursor": 4,
        "created_at": "2026-08-13T18:00:00Z",
        "evidence_class": "REUSABLE_BASELINE",
        "authority_effect": "NO_FACT_GRAPH_WRITE",
        "stable_project_id": project,
        "mode": mode,
        "source_artifacts": [],
        "input_manifest_artifact": _artifact("3"),
        "candidate_claim_ids": [],
        "verifier_receipt_ids": [],
        "candidate_authority": "CANDIDATE_ONLY",
    }


def test_zhao_clean_room_excludes_historical_conclusions_and_certificates() -> None:
    validator = _validator("workflow.schema.json")
    clean = _lineage("CLEAN_ROOM_REDISCOVERY", "ZHAO_C61")
    clean["historical_conclusion_input_ids"] = []
    clean["imported_certificate_artifacts"] = []
    assert not list(validator.iter_errors(clean))
    clean["historical_conclusion_input_ids"] = ["old-proof"]
    assert list(validator.iter_errors(clean))


def test_zhao_import_requires_certificate_report_and_current_verifier_receipt() -> None:
    validator = _validator("workflow.schema.json")
    imported = _lineage("IMPORTED_CERTIFICATE_VERIFICATION", "ZHAO_C61")
    imported["imported_certificate_artifacts"] = [_artifact("4")]
    assert list(validator.iter_errors(imported))
    imported["certificate_import_report_artifact"] = _artifact("5")
    imported["verifier_receipt_ids"] = ["8999d48e-d478-4094-b212-d33e061a448a"]
    assert not list(validator.iter_errors(imported))


def test_n2_history_is_candidate_only_and_cannot_import_verifier_success() -> None:
    validator = _validator("workflow.schema.json")
    history = _lineage("HISTORICAL_CANDIDATE_MIGRATION", "N2_AJT5")
    history["historical_conclusion_input_ids"] = ["n2-manual-note"]
    history["imported_certificate_artifacts"] = []
    history["source_artifacts"] = [_artifact("6")]
    history["candidate_claim_ids"] = ["9999d48e-d478-4094-b212-d33e061a448a"]
    assert not list(validator.iter_errors(history))
    history["verifier_receipt_ids"] = ["8999d48e-d478-4094-b212-d33e061a448a"]
    assert list(validator.iter_errors(history))


def test_submitted_review_task_requires_verified_independence_and_signed_artifact() -> None:
    validator = _validator("support.schema.json")
    task = {
        "schema_version": "rk.product.review_task.v1",
        "review_task_id": "76e89cf5-2d2e-461b-b03d-c4ed076fd6c1",
        "review_type": "ATOMIC",
        "run_id": "c73f6387-2ea0-487a-aebf-dd2b8dad8ec2",
        "assignee_subject_id": "reviewer:one",
        "author_subject_ids": ["worker:one"],
        "target_digest": "a" * 64,
        "contract_version": 2,
        "research_revision": 8,
        "independence_required": True,
        "independence_status": "VERIFIED",
        "state": "SUBMITTED",
        "created_at": "2026-08-13T18:00:00Z",
        "expires_at": "2026-08-14T18:00:00Z",
    }
    assert list(validator.iter_errors(task))
    task["signed_artifact_ref"] = _artifact("7")
    assert not list(validator.iter_errors(task))


def test_workflow_evidence_level_cannot_grant_fact_graph_authority() -> None:
    validator = _validator("workflow.schema.json")
    snapshot = _source_snapshot("LIVE_QUERY")
    snapshot["authority_effect"] = "FACT_GRAPH_WRITE"
    assert list(validator.iter_errors(snapshot))
    snapshot["authority_effect"] = "NO_FACT_GRAPH_WRITE"
    snapshot["evidence_class"] = "MATHEMATICAL_FACT"
    assert list(validator.iter_errors(snapshot))
