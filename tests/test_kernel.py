from __future__ import annotations

import hashlib
import json
import sqlite3
import uuid
from pathlib import Path

import pytest

from rk.composition import canonical_json_bytes as composition_json_bytes
from rk.config import KernelConfig
from rk.domain import (
    ApplyRequest,
    ArtifactInput,
    CreateRequest,
    ExportRequest,
    RequestValidationError,
    RunSnapshot,
    TypedCommand,
    VerifiedCapability,
    frozen_mapping,
)
from rk.extensions import ExtensionRegistry, ProductActivity
from rk.kernel import ResearchKernel

ROOT = Path(__file__).resolve().parents[1]


def _contract() -> dict[str, object]:
    return {
        "stable_project_id": "TEST_MATH_1",
        "statement": "For every test object, a test witness exists.",
        "source_refs": [],
        "objects": [{"name": "test object"}],
        "definitions": [],
        "quantifiers": [{"kind": "forall", "variable": "x"}],
        "exact_negation": "There is a test object with no test witness.",
        "allowed_dependencies": [],
        "forbidden_information": [],
        "boundary_rules": {},
        "randomness_rules": {},
        "tie_rules": {},
        "success_certificate_types": ["NATURAL_LANGUAGE_PROOF"],
        "non_claims": [],
        "literature_scope": {
            "families": ["exact", "equivalent", "stronger", "weaker", "counterexample"]
        },
        "literature_cutoff_date": "2026-08-11",
        "budget_policy": {"global": {"CPU_SECOND": 1000}},
        "stop_rules": [{"kind": "manual"}],
        "semantic_review_policy": {},
        "amendment_policy": {},
    }


def _capability() -> VerifiedCapability:
    return VerifiedCapability(
        capability_id=str(uuid.uuid4()),
        subject_id="kernel-test",
        issuer="kernel-test-host",
        allowed_actions=frozenset({"*"}),
        run_scope=frozenset({"*"}),
        issued_at="2020-01-01T00:00:00.000Z",
        expires_at="2100-01-01T00:00:00.000Z",
    )


def _artifact(path: Path, name: str) -> ArtifactInput:
    data = path.read_bytes()
    return ArtifactInput(
        name=name,
        path=str(path),
        sha256=hashlib.sha256(data).hexdigest(),
        byte_count=len(data),
        media_type="text/plain",
    )


def _apply(
    kernel: ResearchKernel,
    capability: VerifiedCapability,
    run_id: str,
    revision: int,
    command_type: str,
    payload: dict[str, object],
    artifact_inputs: tuple[ArtifactInput, ...] = (),
):
    return kernel.apply(
        ApplyRequest(
            request_id=str(uuid.uuid4()),
            run_id=run_id,
            expected_revision=revision,
            command=TypedCommand(command_type, frozen_mapping(payload)),
            artifact_inputs=artifact_inputs,
        ),
        capability,
    )


def test_kernel_mounts_activity_appender_in_the_command_transaction(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    support = inbox / "support.txt"
    support.write_text("contract completeness", encoding="utf-8")
    observed: list[ProductActivity] = []

    def append(connection: sqlite3.Connection, activity: ProductActivity) -> int:
        assert connection.in_transaction
        observed.append(activity)
        return len(observed)

    config = KernelConfig.from_mapping(
        {
            "workspace_root": str(tmp_path / "state"),
            "inbox_roots": [str(inbox)],
            "command_schema_path": str(ROOT / "docs/spec/json/command.schema.json"),
            "receipt_schema_path": str(ROOT / "docs/spec/json/receipt.schema.json"),
        },
        base=ROOT,
    )
    kernel = ResearchKernel.from_config(
        config,
        migrations_dir=ROOT / "migrations",
        extensions=ExtensionRegistry().register_product_activity_append(append),
    )
    capability = _capability()
    handle = kernel.create(
        CreateRequest(
            str(uuid.uuid4()),
            frozen_mapping(_contract()),
            (_artifact(support, "support.txt"),),
        ),
        capability,
    )
    snapshot = kernel.inspect(handle.run_id)
    assert isinstance(snapshot, RunSnapshot)
    support_id = snapshot.projection["artifacts"][0]["artifact_id"]

    accepted = _apply(
        kernel,
        capability,
        handle.run_id,
        0,
        "FreezeContract",
        {"contract_version": 1, "completeness_check_artifact_id": support_id},
    )
    rejected = _apply(
        kernel,
        capability,
        handle.run_id,
        0,
        "FreezeContract",
        {"contract_version": 1, "completeness_check_artifact_id": support_id},
    )

    assert accepted.accepted
    assert not rejected.accepted
    assert [(item.payload["command_type"], item.payload["accepted"]) for item in observed] == [
        ("FreezeContract", True),
        ("FreezeContract", False),
    ]
    assert observed[0].kernel_event_id == accepted.event_ids[0]
    assert observed[1].kernel_event_id is None
    assert observed[1].research_revision == accepted.revision_after


def test_component_runtime_usage_enters_authoritative_budget_ledger(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    support = inbox / "support.txt"
    support.write_text("contract support", encoding="utf-8")
    capability = _capability()
    config = KernelConfig.from_mapping(
        {
            "workspace_root": str(tmp_path / "state"),
            "inbox_roots": [str(inbox)],
            "command_schema_path": str(ROOT / "docs/spec/json/command.schema.json"),
            "receipt_schema_path": str(ROOT / "docs/spec/json/receipt.schema.json"),
            "budget_policy": {
                "global_budget_limits": {
                    "INPUT_TOKEN": 20_000_000,
                    "OUTPUT_TOKEN": 20_000_000,
                    "WALL_SECOND": 20_000_000,
                }
            },
        },
        base=ROOT,
    )
    kernel = ResearchKernel.from_config(config, migrations_dir=ROOT / "migrations")
    handle = kernel.create(
        CreateRequest(
            str(uuid.uuid4()),
            frozen_mapping(_contract()),
            (_artifact(support, "support.txt"),),
        ),
        capability,
    )

    assert kernel.record_component_usage(
        run_id=handle.run_id,
        request_id="component-request-1",
        component="research-model",
        usage={"input_tokens": 7, "output_tokens": 3, "wall_time_ms": 12},
        capability=capability,
    ) == ()
    # Retry is idempotent, while the persisted projection remains the single budget truth.
    assert kernel.record_component_usage(
        run_id=handle.run_id,
        request_id="component-request-1",
        component="research-model",
        usage={"input_tokens": 7, "output_tokens": 3, "wall_time_ms": 12},
        capability=capability,
    ) == ()
    snapshot = kernel.inspect(handle.run_id)
    assert isinstance(snapshot, RunSnapshot)
    events = snapshot.projection["budget_events"]
    assert {item["resource_kind"] for item in events} == {
        "INPUT_TOKEN",
        "OUTPUT_TOKEN",
        "WALL_SECOND",
    }
    assert snapshot.projection["component_usage"]["research-model"] == {
        "input_tokens": 7,
        "output_tokens": 3,
        "reasoning_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "total_tokens": 0,
        "wall_time_ms": 12,
        "unknown_count": 0,
    }


def test_public_lifecycle_is_executable_and_idempotent(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    support = inbox / "support.txt"
    support.write_text("complete contract\nliterature plan\ncheckpoint\n", encoding="utf-8")
    evidence_file = inbox / "evidence.md"
    evidence_file.write_text("A scoped human proof candidate.\n", encoding="utf-8")
    config = KernelConfig.from_mapping(
        {
            "workspace_root": str(tmp_path / "state"),
            "inbox_roots": [str(inbox)],
            "command_schema_path": str(ROOT / "docs/spec/json/command.schema.json"),
            "receipt_schema_path": str(ROOT / "docs/spec/json/receipt.schema.json"),
            "budget_policy": {"global_budget_limits": {"INPUT_TOKEN": 20_000_000}},
        },
        base=ROOT,
    )
    kernel = ResearchKernel.from_config(config, migrations_dir=ROOT / "migrations")
    capability = _capability()
    request = CreateRequest(
        request_id=str(uuid.uuid4()),
        contract=frozen_mapping(_contract()),
        artifact_inputs=(_artifact(support, "support.txt"),),
    )
    handle = kernel.create(request, capability)
    replay = kernel.create(request, capability)
    assert replay == handle

    snapshot = kernel.inspect(handle.run_id)
    assert isinstance(snapshot, RunSnapshot)
    assert snapshot.revision == 0
    create_artifacts = snapshot.projection["artifacts"]
    support_id = next(
        item["artifact_id"] for item in create_artifacts if item["logical_name"] == "support.txt"
    )
    contract_id = next(
        item["artifact_id"] for item in create_artifacts if item["role"] == "CONTRACT"
    )

    normalized_statement = _contract()
    statement_hash = hashlib.sha256(composition_json_bytes(normalized_statement)).hexdigest()
    register = _apply(
        kernel,
        capability,
        handle.run_id,
        0,
        "RegisterClaim",
        {
            "contract_version": 1,
            "claim_kind": "ROOT",
            "stable_label": "root",
            "statement_artifact_id": contract_id,
            "statement_hash": statement_hash,
            "normalized_statement": normalized_statement,
        },
    )
    assert register.accepted and register.revision_after == 1
    registered_snapshot = kernel.inspect(handle.run_id)
    assert isinstance(registered_snapshot, RunSnapshot)
    root_claim_id = registered_snapshot.projection["root_claim_id"]

    freeze = _apply(
        kernel,
        capability,
        handle.run_id,
        1,
        "FreezeContract",
        {"contract_version": 1, "completeness_check_artifact_id": support_id},
    )
    assert freeze.accepted and freeze.revision_after == 2

    start = _apply(
        kernel,
        capability,
        handle.run_id,
        2,
        "StartRun",
        {
            "contract_version": 1,
            "literature_plan_artifact_id": support_id,
            "budget_policy": {"global": {"CPU_SECOND": 1000}},
        },
    )
    assert start.accepted and start.revision_after == 3

    evidence = _apply(
        kernel,
        capability,
        handle.run_id,
        3,
        "SubmitEvidence",
        {
            "claim_id": root_claim_id,
            "contract_version": 1,
            "statement_hash": statement_hash,
            "evidence_type": "NATURAL_LANGUAGE_PROOF",
            "evidence_strength": "HUMAN_ATTESTED",
            "artifact_input_names": ["evidence.md"],
            "scope": {
                "claim_id": root_claim_id,
                "contract_version": 1,
                "statement_hash": statement_hash,
            },
            "provenance": {"actor": "independent-human-test"},
            "evidence_root": {"root_kind": "HUMAN", "source_graph": {}},
        },
        (_artifact(evidence_file, "evidence.md"),),
    )
    assert evidence.accepted and evidence.revision_after == 4
    evidence_snapshot = kernel.inspect(handle.run_id)
    assert isinstance(evidence_snapshot, RunSnapshot)
    assert evidence_snapshot.projection["evidence"][0]["evidence_strength"] == "HUMAN_ATTESTED"
    assert evidence_snapshot.projection["evidence"][0]["trust_class"] == "UNMANAGED_CANDIDATE"
    assert evidence_snapshot.projection["evidence"][0]["authority_effect"] == "NONE"
    assert evidence_snapshot.projection["evidence"][0]["promotion_eligible"] is False

    interrupt = _apply(
        kernel,
        capability,
        handle.run_id,
        4,
        "Interrupt",
        {"reason_code": "TEST_CHECKPOINT", "checkpoint_artifact_id": support_id},
    )
    assert interrupt.accepted and interrupt.revision_after == 5

    resume = _apply(
        kernel,
        capability,
        handle.run_id,
        5,
        "Resume",
        {
            "checkpoint_artifact_id": support_id,
            "lease_preflight": True,
            "budget_preflight": True,
        },
    )
    assert resume.accepted and resume.revision_after == 6

    finalize = _apply(
        kernel,
        capability,
        handle.run_id,
        6,
        "Finalize",
        {
            "outcome": "UNRESOLVED",
            "terminal_claim_ids": [],
            "open_obligation_ids": [],
            "dossier_spec": {
                "format": "JSON",
                "include_raw_artifacts": False,
                "language": "zh-CN",
            },
        },
    )
    assert finalize.accepted and finalize.revision_after == 7
    final_snapshot = kernel.inspect(handle.run_id)
    assert isinstance(final_snapshot, RunSnapshot)
    assert final_snapshot.status == "CLOSED"
    assert final_snapshot.revision == 7
    assert any(item["role"] == "DOSSIER" for item in final_snapshot.projection["artifacts"])

    dossier_spec = frozen_mapping(
        {"format": "JSON", "include_raw_artifacts": False, "language": "zh-CN"}
    )
    exported = kernel.export(
        ExportRequest(
            request_id=str(uuid.uuid4()),
            run_id=handle.run_id,
            at_revision=7,
            dossier_spec=dossier_spec,
        ),
        capability,
    )
    exported_again = kernel.export(
        ExportRequest(
            request_id=str(uuid.uuid4()),
            run_id=handle.run_id,
            at_revision=7,
            dossier_spec=dossier_spec,
        ),
        capability,
    )
    assert exported.artifact_id in finalize.artifact_ids
    assert exported.sha256 == exported_again.sha256
    assert exported.artifact_id == exported_again.artifact_id

    events = kernel.inspect(handle.run_id, after_cursor=0, limit=100)
    assert [item["type"] for item in events.events] == [
        "CLAIM_REGISTERED",
        "CONTRACT_FROZEN",
        "RUN_STARTED",
        "EVIDENCE_SUBMITTED",
        "RUN_INTERRUPTED",
        "RUN_RESUMED",
        "RUN_FINALIZED",
    ]


def test_public_peer_review_derives_unknown_and_rejects_caller_independence(
    tmp_path: Path,
) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    support = inbox / "support.txt"
    support.write_text("contract and review\n", encoding="utf-8")
    config = KernelConfig.from_mapping(
        {
            "workspace_root": str(tmp_path / "state"),
            "inbox_roots": [str(inbox)],
            "command_schema_path": str(ROOT / "docs/spec/json/command.schema.json"),
            "receipt_schema_path": str(ROOT / "docs/spec/json/receipt.schema.json"),
        },
        base=ROOT,
    )
    kernel = ResearchKernel.from_config(config, migrations_dir=ROOT / "migrations")
    capability = _capability()
    handle = kernel.create(
        CreateRequest(
            request_id=str(uuid.uuid4()),
            contract=frozen_mapping(_contract()),
            artifact_inputs=(_artifact(support, "support.txt"),),
        ),
        capability,
    )
    snapshot = kernel.inspect(handle.run_id)
    assert isinstance(snapshot, RunSnapshot)
    support_id = next(
        item["artifact_id"]
        for item in snapshot.projection["artifacts"]
        if item["logical_name"] == "support.txt"
    )
    contract_id = next(
        item["artifact_id"]
        for item in snapshot.projection["artifacts"]
        if item["role"] == "CONTRACT"
    )
    normalized = _contract()
    statement_hash = hashlib.sha256(composition_json_bytes(normalized)).hexdigest()
    registered = _apply(
        kernel,
        capability,
        handle.run_id,
        0,
        "RegisterClaim",
        {
            "contract_version": 1,
            "claim_kind": "ROOT",
            "stable_label": "root",
            "statement_artifact_id": contract_id,
            "statement_hash": statement_hash,
            "normalized_statement": normalized,
        },
    )
    assert registered.accepted
    snapshot = kernel.inspect(handle.run_id)
    assert isinstance(snapshot, RunSnapshot)
    claim_id = snapshot.projection["root_claim_id"]
    payload = {
        "claim_id": claim_id,
        "contract_version": 1,
        "statement_hash": statement_hash,
        "review_artifact_id": support_id,
        "verdict": "ACCEPT",
        "checklist": {
            "proof_checked": {
                "passed": True, "status": "HUMAN_ATTESTED",
                "conclusion": "proof checked", "evidence_refs": [support_id],
            },
            "scope_checked": {
                "passed": True, "status": "HUMAN_ATTESTED",
                "conclusion": "scope checked", "evidence_refs": [support_id],
            },
            "blind_review": False,
        },
        "source_graph": {"review_artifact_id": support_id},
        "verifier_attestation": {
            "artifact_sha256": hashlib.sha256(support.read_bytes()).hexdigest(),
            "verifier_identity_id": capability.capability_id,
            "verifier_subject_id": capability.subject_id,
            "promotion_eligible": True,
            "authority": "HUMAN_ATTESTED",
            "claim_id": claim_id,
            "contract_version": 1,
            "statement_hash": statement_hash,
            "verdict": "ACCEPT",
            "selected_subgraph_digest": None,
        },
    }
    recorded = _apply(
        kernel, capability, handle.run_id, 1, "RecordPeerReview", payload
    )
    assert recorded.accepted
    snapshot = kernel.inspect(handle.run_id)
    assert isinstance(snapshot, RunSnapshot)
    with sqlite3.connect(config.db_path) as connection:
        raw_profile = connection.execute(
            "SELECT independence_profile_json FROM peer_reviews WHERE run_id = ?",
            (handle.run_id,),
        ).fetchone()[0]
    profile = json.loads(str(raw_profile))
    assert profile["idea_independence"] == "UNKNOWN"
    assert snapshot.projection["claims"][0]["peer"] == "UNREVIEWED"

    payload["independence_profile"] = {"independent": True}
    with pytest.raises(RequestValidationError):
        _apply(kernel, capability, handle.run_id, 2, "RecordPeerReview", payload)


def test_registering_a_rebuilt_canonical_root_replaces_an_invalidated_pointer(
    tmp_path: Path,
) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    support = inbox / "support.txt"
    support.write_text("contract support\n", encoding="utf-8")
    config = KernelConfig.from_mapping(
        {
            "workspace_root": str(tmp_path / "state"),
            "inbox_roots": [str(inbox)],
            "command_schema_path": str(ROOT / "docs/spec/json/command.schema.json"),
            "receipt_schema_path": str(ROOT / "docs/spec/json/receipt.schema.json"),
        },
        base=ROOT,
    )
    kernel = ResearchKernel.from_config(config, migrations_dir=ROOT / "migrations")
    capability = _capability()
    handle = kernel.create(
        CreateRequest(
            request_id=str(uuid.uuid4()),
            contract=frozen_mapping(_contract()),
            artifact_inputs=(_artifact(support, "support.txt"),),
        ),
        capability,
    )
    before = kernel.inspect(handle.run_id)
    contract_id = next(
        item["artifact_id"] for item in before.projection["artifacts"] if item["role"] == "CONTRACT"
    )
    normalized = _contract()
    statement_hash = hashlib.sha256(composition_json_bytes(normalized)).hexdigest()
    first = _apply(
        kernel,
        capability,
        handle.run_id,
        0,
        "RegisterClaim",
        {
            "contract_version": 1,
            "claim_kind": "ROOT",
            "stable_label": "root-v1",
            "statement_artifact_id": contract_id,
            "statement_hash": statement_hash,
            "normalized_statement": normalized,
        },
    )
    assert first.accepted
    frozen = _apply(
        kernel,
        capability,
        handle.run_id,
        1,
        "FreezeContract",
        {"contract_version": 1, "completeness_check_artifact_id": contract_id},
    )
    assert frozen.accepted
    old_root = kernel.inspect(handle.run_id).projection["root_claim_id"]
    with sqlite3.connect(config.db_path) as connection:
        connection.execute(
            "UPDATE claims SET lifecycle_status='INVALIDATED' WHERE claim_id=?", (old_root,)
        )
    second = _apply(
        kernel,
        capability,
        handle.run_id,
        2,
        "RegisterClaim",
        {
            "contract_version": 1,
            "claim_kind": "ROOT",
            "stable_label": "root-v1",
            "statement_artifact_id": contract_id,
            "statement_hash": statement_hash,
            "normalized_statement": normalized,
        },
    )

    assert second.accepted
    rebuilt = kernel.inspect(handle.run_id).projection
    assert rebuilt["root_claim_id"] != old_root
    new_root = next(
        item for item in rebuilt["claims"] if item["claim_id"] == rebuilt["root_claim_id"]
    )
    assert new_root["stable_label"] == "root-v1"
    assert new_root["statement_revision"] == 2


def test_component_usage_is_aggregated_in_inspect(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    support = inbox / "support.txt"
    support.write_text("complete contract\nliterature plan\n", encoding="utf-8")
    config = KernelConfig.from_mapping(
        {
            "workspace_root": str(tmp_path / "state"),
            "inbox_roots": [str(inbox)],
            "command_schema_path": str(ROOT / "docs/spec/json/command.schema.json"),
            "receipt_schema_path": str(ROOT / "docs/spec/json/receipt.schema.json"),
            "budget_policy": {
                "global_budget_limits": {"INPUT_TOKEN": 20_000_000},
                "budget_controller_capability_ids": ["configured-after-capability"],
            },
        },
        base=ROOT,
    )
    kernel = ResearchKernel.from_config(config, migrations_dir=ROOT / "migrations")
    capability = _capability()
    handle = kernel.create(
        CreateRequest(
            request_id=str(uuid.uuid4()),
            contract=frozen_mapping(_contract()),
            artifact_inputs=(_artifact(support, "support.txt"),),
        ),
        capability,
    )
    receipt = _apply(
        kernel,
        capability,
        handle.run_id,
        0,
        "RecordBudget",
        {
            "event_kind": "ACTUAL",
            "resource_kind": "INPUT_TOKEN",
            "amount_microunits": 12_000_000,
            "unit": "microtoken",
            "provider_usage": {
                "component": "deepseek-v4-pro",
                "input_tokens": 12,
                "output_tokens": 3,
                "reasoning_tokens": 2,
                "cache_read_tokens": 7,
                "cache_write_tokens": 0,
                "total_tokens": 24,
                "wall_time_ms": 345,
            },
        },
    )

    assert not receipt.accepted
    assert receipt.rejection_code == "CAPABILITY_DENIED"
    assert "deepseek-v4-pro" not in kernel.inspect(handle.run_id).projection["component_usage"]


def test_contract_amendment_preserves_unaffected_sibling(tmp_path: Path) -> None:
    """A local contract defect must not erase independent verified research memory."""

    inbox = tmp_path / "inbox"
    inbox.mkdir()
    support = inbox / "support.txt"
    support.write_text("contract amendment evidence\n", encoding="utf-8")
    config = KernelConfig.from_mapping(
        {
            "workspace_root": str(tmp_path / "state"),
            "inbox_roots": [str(inbox)],
            "command_schema_path": str(ROOT / "docs/spec/json/command.schema.json"),
            "receipt_schema_path": str(ROOT / "docs/spec/json/receipt.schema.json"),
        },
        base=ROOT,
    )
    kernel = ResearchKernel.from_config(config, migrations_dir=ROOT / "migrations")
    capability = _capability()
    handle = kernel.create(
        CreateRequest(
            str(uuid.uuid4()),
            frozen_mapping(_contract()),
            (_artifact(support, "support.txt"),),
        ),
        capability,
    )
    initial = kernel.inspect(handle.run_id)
    contract_artifact = next(
        item["artifact_id"]
        for item in initial.projection["artifacts"]
        if item["role"] == "CONTRACT"
    )
    contract_hash = hashlib.sha256(composition_json_bytes(_contract())).hexdigest()
    revision = 0

    def apply(
        kind: str,
        payload: dict[str, object],
        artifacts: tuple[ArtifactInput, ...] = (),
    ):
        nonlocal revision
        receipt = _apply(
            kernel, capability, handle.run_id, revision, kind, payload, artifacts
        )
        assert receipt.accepted, (kind, receipt.rejection_code, receipt.missing_conditions)
        revision = receipt.revision_after
        return receipt

    apply(
        "RegisterClaim",
        {
            "contract_version": 1,
            "claim_kind": "ROOT",
            "stable_label": "root",
            "statement_artifact_id": contract_artifact,
            "statement_hash": contract_hash,
            "normalized_statement": _contract(),
        },
    )
    apply(
        "FreezeContract",
        {"contract_version": 1, "completeness_check_artifact_id": contract_artifact},
    )
    apply(
        "StartRun",
        {
            "contract_version": 1,
            "literature_plan_artifact_id": contract_artifact,
            "budget_policy": {"global": {"CPU_SECOND": 1000}},
        },
    )
    root_id = str(kernel.inspect(handle.run_id).projection["root_claim_id"])

    def add_claim(label: str) -> tuple[str, str]:
        normalized = {"atomic": True, "statement": label, "proof": f"proof of {label}"}
        path = inbox / f"{label}.json"
        path.write_bytes(composition_json_bytes(normalized))
        data = path.read_bytes()
        claim_input = ArtifactInput(
            name="atomic.json",
            path=str(path),
            sha256=hashlib.sha256(data).hexdigest(),
            byte_count=len(data),
            media_type="application/json",
        )
        imported = kernel.import_artifact(
            handle.run_id,
            claim_input,
            capability,
            logical_name=f"claim-{label}",
            role="CLAIM_STATEMENT",
        )
        apply(
            "RegisterClaim",
            {
                "contract_version": 1,
                "claim_kind": "LEMMA",
                "stable_label": label,
                "statement_artifact_id": imported.artifact_id,
                "statement_hash": hashlib.sha256(path.read_bytes()).hexdigest(),
                "normalized_statement": normalized,
            },
        )
        claims = kernel.inspect(handle.run_id).projection["claims"]
        claim_id = next(item["claim_id"] for item in claims if item["stable_label"] == label)
        return str(claim_id), imported.artifact_id

    dependent_id, dependent_artifact = add_claim("depends-on-root")
    sibling_id, _ = add_claim("independent-sibling")
    apply(
        "RegisterClaimEdge",
        {
            "contract_version": 1,
            # Store the endpoints opposite to the effective dependency direction.  Contract
            # amendment must follow direction, not raw column order.
            "from_claim_id": dependent_id,
            "to_claim_id": root_id,
            "edge_kind": "DEPENDS_ON",
            "direction": "REVERSE",
            "justification_kind": "DEFINITIONAL",
            "justification_ref": dependent_artifact,
        },
    )
    apply(
        "SubmitEvidence",
        {
            "claim_id": root_id,
            "contract_version": 1,
            "statement_hash": contract_hash,
            "evidence_type": "NATURAL_LANGUAGE_PROOF",
            "evidence_strength": "HUMAN_ATTESTED",
            "artifact_input_names": ["support.txt"],
            "scope": {"claim_id": root_id, "contract_version": 1, "statement_hash": contract_hash},
            "provenance": {"actor": "contract-auditor"},
            "evidence_root": {"root_kind": "HUMAN", "source_graph": {}},
        },
        (_artifact(support, "support.txt"),),
    )
    evidence_id = str(kernel.inspect(handle.run_id).projection["evidence"][-1]["evidence_id"])
    apply(
        "RecordPeerReview",
        {
            "claim_id": root_id,
            "contract_version": 1,
            "statement_hash": contract_hash,
            "review_artifact_id": contract_artifact,
            "verdict": "ACCEPT",
            "checklist": {
                "proof_checked": {
                    "passed": True, "status": "HUMAN_ATTESTED",
                    "conclusion": "proof checked", "evidence_refs": [contract_artifact],
                },
                "scope_checked": {
                    "passed": True, "status": "HUMAN_ATTESTED",
                    "conclusion": "scope checked", "evidence_refs": [contract_artifact],
                },
                "blind_review": True,
                "amendment_approved": True,
                "amendment_role": "CONTRACT_OWNER",
            },
            "source_graph": {"author_subject_ids": ["different-worker"]},
            "verifier_attestation": {
                "artifact_sha256": contract_hash,
                "verifier_identity_id": capability.capability_id,
                "verifier_subject_id": capability.subject_id,
                "promotion_eligible": True,
                "authority": "HUMAN_ATTESTED",
                "claim_id": root_id,
                "contract_version": 1,
                "statement_hash": contract_hash,
                "verdict": "ACCEPT",
                "selected_subgraph_digest": None,
            },
        },
    )
    review_id = str(kernel.inspect(handle.run_id).projection["peer_reviews"][-1]["review_id"])
    patch = kernel.import_artifact(
        handle.run_id,
        _artifact(support, "patch.txt"),
        capability,
        logical_name="contract-patch",
        role="RESEARCH_MATERIAL",
    )
    apply(
        "ProposeContractDefect",
        {
            "contract_version": 1,
            "defect_type": "BUDGET_SCOPE",
            "evidence_refs": [evidence_id],
            "affected_claim_ids": [root_id],
            "proposed_patch_artifact_id": patch.artifact_id,
        },
    )
    impact = kernel.import_artifact(
        handle.run_id,
        _artifact(support, "impact.txt"),
        capability,
        logical_name="contract-impact",
        role="RESEARCH_MATERIAL",
    )
    replacement = _contract()
    replacement["budget_policy"] = {"global": {"CPU_SECOND": 2000}}
    apply(
        "AmendContract",
        {
            "base_version": 1,
            "replacement_contract": replacement,
            "patch_artifact_id": patch.artifact_id,
            "approvals": [review_id],
            "impact_analysis_artifact_id": impact.artifact_id,
        },
    )
    result = kernel.inspect(handle.run_id).projection
    by_id = {item["claim_id"]: item for item in result["claims"]}
    assert by_id[root_id]["lifecycle"] == "SUPERSEDED"
    assert by_id[dependent_id]["lifecycle"] == "SUPERSEDED"
    assert by_id[sibling_id]["lifecycle"] == "ACTIVE"
    assert by_id[sibling_id]["contract_version"] == 2
    with sqlite3.connect(config.db_path) as connection:
        impact_summary = json.loads(
            connection.execute(
                "SELECT impact_analysis_json FROM contract_versions "
                "WHERE run_id=? AND version=2",
                (handle.run_id,),
            ).fetchone()[0]
        )
    assert impact_summary["invalidated_claim_ids"] == [root_id, dependent_id]
    assert impact_summary["carried_claim_ids"] == [sibling_id]


def test_bridge_spec_one_way_product_flow_rejects_reverse_use(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    support = inbox / "support.txt"
    support.write_text("bridge audit and backtranslation\n", encoding="utf-8")
    config = KernelConfig.from_mapping(
        {
            "workspace_root": str(tmp_path / "state"),
            "inbox_roots": [str(inbox)],
            "command_schema_path": str(ROOT / "docs/spec/json/command.schema.json"),
            "receipt_schema_path": str(ROOT / "docs/spec/json/receipt.schema.json"),
        },
        base=ROOT,
    )
    kernel = ResearchKernel.from_config(config, migrations_dir=ROOT / "migrations")
    capability = _capability()
    handle = kernel.create(
        CreateRequest(
            str(uuid.uuid4()),
            frozen_mapping(_contract()),
            (_artifact(support, "support.txt"),),
        ),
        capability,
    )
    snapshot = kernel.inspect(handle.run_id)
    contract_artifact = next(
        item["artifact_id"]
        for item in snapshot.projection["artifacts"]
        if item["role"] == "CONTRACT"
    )
    contract_hash = hashlib.sha256(composition_json_bytes(_contract())).hexdigest()
    revision = 0

    def apply(kind: str, payload: dict[str, object]):
        nonlocal revision
        receipt = _apply(kernel, capability, handle.run_id, revision, kind, payload)
        revision = receipt.revision_after
        return receipt

    assert apply(
        "RegisterClaim",
        {
            "contract_version": 1,
            "claim_kind": "ROOT",
            "stable_label": "source-domain-claim",
            "statement_artifact_id": contract_artifact,
            "statement_hash": contract_hash,
            "normalized_statement": _contract(),
        },
    ).accepted
    assert apply(
        "FreezeContract",
        {"contract_version": 1, "completeness_check_artifact_id": contract_artifact},
    ).accepted
    assert apply(
        "StartRun",
        {
            "contract_version": 1,
            "literature_plan_artifact_id": contract_artifact,
            "budget_policy": {"global": {"CPU_SECOND": 1000}},
        },
    ).accepted
    source_id = str(kernel.inspect(handle.run_id).projection["root_claim_id"])
    target_statement = {
        "atomic": True,
        "statement": "The translated graph has a perfect matching.",
        "proof": "target-domain certificate",
    }
    target_path = inbox / "target.json"
    target_path.write_bytes(composition_json_bytes(target_statement))
    target_data = target_path.read_bytes()
    target_artifact = kernel.import_artifact(
        handle.run_id,
        ArtifactInput(
            "target.json",
            str(target_path),
            hashlib.sha256(target_data).hexdigest(),
            len(target_data),
            "application/json",
        ),
        capability,
        logical_name="target-domain-claim",
        role="CLAIM_STATEMENT",
    )
    assert apply(
        "RegisterClaim",
        {
            "contract_version": 1,
            "claim_kind": "BRIDGE",
            "stable_label": "target-domain-claim",
            "statement_artifact_id": target_artifact.artifact_id,
            "statement_hash": hashlib.sha256(target_data).hexdigest(),
            "normalized_statement": target_statement,
        },
    ).accepted
    target_id = str(
        next(
            item["claim_id"]
            for item in kernel.inspect(handle.run_id).projection["claims"]
            if item["stable_label"] == "target-domain-claim"
        )
    )
    assert apply(
        "RecordPeerReview",
        {
            "claim_id": target_id,
            "contract_version": 1,
            "statement_hash": hashlib.sha256(target_data).hexdigest(),
            "review_artifact_id": contract_artifact,
            "verdict": "ACCEPT",
                "checklist": {
                    "proof_checked": {
                        "passed": True, "status": "HUMAN_ATTESTED",
                        "conclusion": "proof checked", "evidence_refs": [contract_artifact],
                    },
                    "scope_checked": {
                        "passed": True, "status": "HUMAN_ATTESTED",
                        "conclusion": "scope checked", "evidence_refs": [contract_artifact],
                    },
                    "blind_review": True,
                },
            "source_graph": {"author_subject_ids": ["bridge-constructor"]},
            "verifier_attestation": {
                "artifact_sha256": contract_hash,
                "verifier_identity_id": capability.capability_id,
                "verifier_subject_id": capability.subject_id,
                "promotion_eligible": True,
                "authority": "HUMAN_ATTESTED",
                "claim_id": target_id,
                "contract_version": 1,
                "statement_hash": hashlib.sha256(target_data).hexdigest(),
                "verdict": "ACCEPT",
                "selected_subgraph_digest": None,
            },
        },
    ).accepted
    audit_id = str(kernel.inspect(handle.run_id).projection["peer_reviews"][-1]["review_id"])
    backtranslation = kernel.import_artifact(
        handle.run_id,
        _artifact(support, "backtranslation.txt"),
        capability,
        logical_name="bridge-backtranslation",
        role="RESEARCH_MATERIAL",
    )
    bridge_id = str(uuid.uuid4())
    bridge_spec = {
        "source_domain": "finite set systems",
        "target_domain": "bipartite graphs",
        "source_objects": ["sets", "representatives"],
        "target_objects": ["left vertices", "right vertices", "edges"],
        "forward_map": "membership becomes adjacency",
        "backward_map": "matched edge becomes representative",
        "preserved_invariants": ["distinctness"],
        "imported_assumptions": [],
        "lost_assumptions": ["reverse Hall implication not checked"],
        "gained_assumptions": [],
        "target_domain_tools": ["matching checker"],
        "fastest_countertests": ["two sets sharing one element"],
        "roundtrip_tests": ["representatives -> matching -> representatives"],
        "source_to_target_dictionary": {"representative": "matched neighbor"},
        "target_to_source_dictionary": {"matched neighbor": "representative"},
        "source_domain_auditor": "set-system-reviewer",
        "target_domain_auditor": "graph-reviewer",
    }
    registered = apply(
        "RegisterBridge",
        {
            "bridge_id": bridge_id,
            "contract_version": 1,
            "source_claim_id": source_id,
            "target_claim_id": target_id,
            "directionality": "ONE_WAY_VALID",
            "term_mapping": {"membership": "adjacency"},
            "forward_obligations": [{"id": "map-total", "status": "CHECKED"}],
            "reverse_obligations": [{"id": "reverse", "status": "OPEN"}],
            "loss_accounting": {"reverse_direction": "not established"},
            "bridge_spec": bridge_spec,
            "target_audit_review_id": audit_id,
            "backtranslation_artifact_id": backtranslation.artifact_id,
        },
    )
    assert registered.accepted
    forward_edge = apply(
        "RegisterClaimEdge",
        {
            "contract_version": 1,
            "from_claim_id": target_id,
            "to_claim_id": source_id,
            "edge_kind": "IMPLIES",
            "direction": "REVERSE",
            "justification_kind": "BRIDGE",
            "justification_ref": bridge_id,
        },
    )
    assert forward_edge.accepted
    reverse_edge = apply(
        "RegisterClaimEdge",
        {
            "contract_version": 1,
            "from_claim_id": target_id,
            "to_claim_id": source_id,
            "edge_kind": "IMPLIES",
            "direction": "FORWARD",
            "justification_kind": "BRIDGE",
            "justification_ref": bridge_id,
        },
    )
    assert not reverse_edge.accepted
    assert reverse_edge.rejection_code == "COMPOSITION_OPEN"
    bridge = kernel.inspect(handle.run_id).projection["bridges"][0]
    assert bridge["forward_status"] == "CHECKED"
    assert bridge["reverse_status"] == "CANDIDATE"
    assert bridge["bridge_spec"]["target_domain"] == "bipartite graphs"
