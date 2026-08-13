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
