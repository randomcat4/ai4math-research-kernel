from __future__ import annotations

import hashlib
import uuid
from pathlib import Path

from rk.composition import canonical_json_bytes as composition_json_bytes
from rk.config import KernelConfig
from rk.domain import (
    ApplyRequest,
    ArtifactInput,
    CreateRequest,
    ExportRequest,
    RunSnapshot,
    TypedCommand,
    VerifiedCapability,
    frozen_mapping,
)
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

    normalized_statement = {"statement": "For every x, a witness exists.", "atomic": True}
    statement_hash = hashlib.sha256(
        composition_json_bytes(normalized_statement)
    ).hexdigest()
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
            "statement_artifact_id": support_id,
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
