from __future__ import annotations

from datetime import UTC, datetime

import pytest
from hypothesis import given
from hypothesis import strategies as st

from rk.domain import Decision, RunSnapshot, TypedCommand, VerifiedCapability
from rk.guard import COMMAND_TYPES, TransitionGuard

NOW = datetime(2026, 8, 11, 12, tzinfo=UTC)
RUN_ID = "run-1"
ARTIFACT_ID = "artifact-1"


def _capability(actions: frozenset[str] = frozenset({"*"})) -> VerifiedCapability:
    return VerifiedCapability(
        capability_id="cap-1",
        subject_id="tester",
        issuer="test-host",
        allowed_actions=actions,
        run_scope=frozenset({RUN_ID}),
        issued_at="2026-08-11T11:00:00Z",
        expires_at="2026-08-11T13:00:00Z",
    )


def _snapshot(
    status: str,
    *,
    revision: int = 0,
    contract_status: str = "FROZEN",
    extra: dict[str, object] | None = None,
) -> RunSnapshot:
    projection: dict[str, object] = {
        "contract": {
            "version": 1,
            "status": contract_status,
            "fields_complete": True,
        },
        "claims": [],
        "routes": [],
        "attempts": [],
        "leases": [],
        "artifacts": [
            {"artifact_id": ARTIFACT_ID, "ingest_state": "COMMITTED"},
        ],
        "open_obligation_ids": [],
    }
    projection.update(extra or {})
    return RunSnapshot(
        run_id=RUN_ID,
        status=status,
        revision=revision,
        current_contract_version=1,
        last_cursor=0,
        projection=projection,
    )


def _decide(
    snapshot: RunSnapshot,
    command_type: str,
    payload: dict[str, object],
    *,
    evidence: dict[str, object] | None = None,
    policy: dict[str, object] | None = None,
    expected_revision: int | None = None,
) -> Decision:
    return TransitionGuard().decide(
        now_utc=NOW,
        snapshot=snapshot,
        command=TypedCommand(command_type, payload),
        evidence_summary=evidence or {},
        capability=_capability(),
        policy_snapshot=policy or {},
        expected_revision=snapshot.revision if expected_revision is None else expected_revision,
    )


def test_command_set_is_the_closed_27_command_union() -> None:
    assert len(COMMAND_TYPES) == 27
    assert "Create" not in COMMAND_TYPES
    assert "PauseRun" not in COMMAND_TYPES


@pytest.mark.parametrize("terminal_status", ["CLOSED", "CONTRACT_DEFECTIVE"])
@pytest.mark.parametrize("command_type", sorted(COMMAND_TYPES))
def test_every_apply_command_rejects_a_terminal_run(
    command_type: str, terminal_status: str
) -> None:
    decision = _decide(_snapshot(terminal_status), command_type, {})

    assert not decision.accepted
    assert decision.rejection_code == "RUN_CLOSED"
    assert not decision.projection_mutations
    assert not decision.event_intents


def test_freeze_contract_emits_declarative_intents() -> None:
    snapshot = _snapshot("OPEN", contract_status="DRAFT")

    decision = _decide(
        snapshot,
        "FreezeContract",
        {"contract_version": 1, "completeness_check_artifact_id": ARTIFACT_ID},
        evidence={"contract_complete": True},
    )

    assert decision.accepted
    assert dict(decision.projection_mutations[0]) == {
        "op": "SET_CONTRACT_STATUS",
        "version": 1,
        "status": "FROZEN",
    }
    assert dict(decision.event_intents[0]) == {
        "type": "CONTRACT_FROZEN",
        "command_type": "FreezeContract",
    }


def test_start_interrupt_resume_and_unresolved_finalize() -> None:
    started = _decide(
        _snapshot("OPEN"),
        "StartRun",
        {
            "contract_version": 1,
            "literature_plan_artifact_id": ARTIFACT_ID,
            "budget_policy": {"global": {"CPU_SECOND": 1000}},
        },
    )
    assert started.accepted
    assert dict(started.projection_mutations[0]) == {
        "op": "SET_RUN_STATUS",
        "status": "RUNNING",
    }

    interrupted = _decide(
        _snapshot("RUNNING"),
        "Interrupt",
        {"reason_code": "USER_REQUEST", "checkpoint_artifact_id": ARTIFACT_ID},
    )
    assert interrupted.accepted
    assert [item["op"] for item in interrupted.projection_mutations] == [
        "EXPIRE_ACTIVE_LEASES",
        "SET_RUN_STATUS",
        "PAUSE_ACTIVE_ATTEMPTS",
    ]

    resumed = _decide(
        _snapshot(
            "PAUSED",
            extra={
                "last_interrupt": {"checkpoint_artifact_id": ARTIFACT_ID},
                "budget_fuse_tripped": False,
            },
        ),
        "Resume",
        {
            "checkpoint_artifact_id": ARTIFACT_ID,
            "lease_preflight": True,
            "budget_preflight": True,
        },
    )
    assert resumed.accepted

    finalized = _decide(
        _snapshot("PAUSED", extra={"open_obligation_ids": ["open-1"]}),
        "Finalize",
        {
            "outcome": "UNRESOLVED",
            "terminal_claim_ids": [],
            "open_obligation_ids": ["open-1"],
            "dossier_spec": {"format": "json"},
        },
    )
    assert finalized.accepted
    assert dict(finalized.projection_mutations[0])["status"] == "CLOSED"


def test_stale_revision_has_no_mutation_or_event_intents() -> None:
    decision = _decide(
        _snapshot("OPEN", revision=4),
        "StartRun",
        {
            "contract_version": 1,
            "literature_plan_artifact_id": ARTIFACT_ID,
            "budget_policy": {"global": {"CPU_SECOND": 1000}},
        },
        expected_revision=3,
    )

    assert not decision.accepted
    assert decision.rejection_code == "REVISION_CONFLICT"
    assert decision.projection_mutations == ()
    assert decision.event_intents == ()


@given(st.integers(min_value=1, max_value=20))
def test_soft_model_evidence_never_promotes_machine_axis(count: int) -> None:
    claim = {
        "claim_id": "claim-1",
        "run_id": RUN_ID,
        "contract_version": 1,
        "statement_hash": "1" * 64,
        "status": "ACTIVE",
    }
    model_evidence = [
        {
            "evidence_id": f"model-{index}",
            "claim_id": "claim-1",
            "evidence_type": "MODEL_JUDGE",
            "evidence_strength": "SOFT_MODEL",
            "status": "ACTIVE",
            "replay_pass": True,
        }
        for index in range(count)
    ]
    decision = _decide(
        _snapshot("RUNNING", extra={"claims": [claim], "evidence": model_evidence}),
        "PromoteClaim",
        {
            "claim_id": "claim-1",
            "target_axis": "MACHINE",
            "target_value": "KERNEL_VERIFIED",
            "evidence_ids": [item["evidence_id"] for item in model_evidence],
        },
    )

    assert not decision.accepted
    assert decision.rejection_code == "REPLAY_FAILED"
    assert decision.projection_mutations == ()


def test_self_reported_replay_never_promotes_machine_axis() -> None:
    claim = {
        "claim_id": "claim-1",
        "run_id": RUN_ID,
        "contract_version": 1,
        "statement_hash": "1" * 64,
        "status": "ACTIVE",
    }
    evidence = {
        "evidence_id": "fake-replay",
        "claim_id": "claim-1",
        "artifact_id": "artifact-output",
        "evidence_type": "LEAN_REPLAY",
        "evidence_strength": "HARD_MACHINE",
        "root_kind": "LEAN_KERNEL",
        "verifier_profile_id": "lean-clean",
        "status": "ACTIVE",
        "replay": {"passed": True, "sorry_count": 0, "axiom_violations": []},
    }
    decision = _decide(
        _snapshot("RUNNING", extra={"claims": [claim], "evidence": [evidence]}),
        "PromoteClaim",
        {
            "claim_id": "claim-1",
            "target_axis": "MACHINE",
            "target_value": "KERNEL_VERIFIED",
            "evidence_ids": ["fake-replay"],
        },
        policy={"verifier_profiles": {"lean-clean": {"kind": "LEAN"}}},
    )

    assert not decision.accepted
    assert decision.rejection_code == "REPLAY_FAILED"


def test_route_proved_checks_other_axes_without_circular_dependency() -> None:
    claim = {
        "claim_id": "claim-1",
        "run_id": RUN_ID,
        "contract_version": 1,
        "statement_hash": "1" * 64,
        "status": "ACTIVE",
        "route": "UNASSESSED",
        "machine": "CERTIFICATE_VERIFIED",
        "semantic": "HUMAN_ATTESTED",
        "peer": "UNREVIEWED",
        "closure": "NOT_REQUIRED",
    }
    support = {
        "evidence_id": "support-1",
        "claim_id": "claim-1",
        "evidence_type": "EXACT_ENUMERATION",
        "evidence_strength": "HARD_MACHINE",
        "status": "ACTIVE",
    }
    decision = _decide(
        _snapshot("RUNNING", extra={"claims": [claim], "evidence": [support]}),
        "PromoteClaim",
        {
            "claim_id": "claim-1",
            "target_axis": "ROUTE",
            "target_value": "ROUTE_PROVED",
            "evidence_ids": ["support-1"],
        },
    )

    assert decision.accepted


def test_invalid_evidence_root_kind_is_rejected_before_projection() -> None:
    claim = {
        "claim_id": "claim-1",
        "run_id": RUN_ID,
        "contract_version": 1,
        "statement_hash": "1" * 64,
        "status": "ACTIVE",
    }
    decision = _decide(
        _snapshot("RUNNING", extra={"claims": [claim]}),
        "SubmitEvidence",
        {
            "claim_id": "claim-1",
            "contract_version": 1,
            "statement_hash": "1" * 64,
            "evidence_type": "MODEL_JUDGE",
            "evidence_strength": "SOFT_MODEL",
            "artifact_input_names": ["candidate.md"],
            "scope": {
                "claim_id": "claim-1",
                "contract_version": 1,
                "statement_hash": "1" * 64,
            },
            "provenance": {"actor": "model"},
            "evidence_root": {"root_kind": "MACHINE"},
        },
        evidence={"artifact_input_names": ["candidate.md"]},
    )

    assert not decision.accepted
    assert decision.rejection_code == "EVIDENCE_INSUFFICIENT"


def test_lean_feedback_requires_profile_bound_verifier_writer() -> None:
    claim = {
        "claim_id": "claim-1",
        "run_id": RUN_ID,
        "contract_version": 1,
        "statement_hash": "1" * 64,
        "status": "ACTIVE",
    }
    decision = _decide(
        _snapshot("RUNNING", extra={"claims": [claim]}),
        "RecordLeanFeedback",
        {
            "claim_id": "claim-1",
            "contract_version": 1,
            "environment_profile_id": "lean-clean",
            "toolchain": "lean-4.32",
            "source_artifact_id": ARTIFACT_ID,
            "output_artifact_id": ARTIFACT_ID,
            "feedback_kind": "REPLAY_PASS",
            "diagnostic": {},
        },
        policy={
            "verifier_profiles": {
                "lean-clean": {
                    "toolchain": "lean-4.32",
                    "verifier_writer_capability_ids": ["some-other-capability"],
                }
            }
        },
    )

    assert not decision.accepted
    assert decision.rejection_code == "CAPABILITY_DENIED"


def test_lean_feedback_unknown_profile_rejects_instead_of_crashing() -> None:
    claim = {
        "claim_id": "claim-1",
        "run_id": RUN_ID,
        "contract_version": 1,
        "statement_hash": "1" * 64,
        "status": "ACTIVE",
    }
    decision = _decide(
        _snapshot("RUNNING", extra={"claims": [claim]}),
        "RecordLeanFeedback",
        {
            "claim_id": "claim-1",
            "contract_version": 1,
            "environment_profile_id": "missing-profile",
            "toolchain": "lean-4.32",
            "source_artifact_id": ARTIFACT_ID,
            "output_artifact_id": ARTIFACT_ID,
            "feedback_kind": "REPLAY_PASS",
            "diagnostic": {},
        },
        policy={"verifier_profiles": {}},
    )

    assert not decision.accepted
    assert decision.rejection_code == "ENVIRONMENT_DRIFT"


def test_replay_pass_requires_succeeded_bound_attempt_and_host_receipt_fields() -> None:
    claim = {
        "claim_id": "claim-1",
        "run_id": RUN_ID,
        "contract_version": 1,
        "statement_hash": "1" * 64,
        "status": "ACTIVE",
    }
    decision = _decide(
        _snapshot("RUNNING", extra={"claims": [claim]}),
        "RecordLeanFeedback",
        {
            "claim_id": "claim-1",
            "contract_version": 1,
            "environment_profile_id": "lean-clean",
            "toolchain": "lean-4.32",
            "mathlib_commit": "a" * 40,
            "source_artifact_id": ARTIFACT_ID,
            "output_artifact_id": ARTIFACT_ID,
            "feedback_kind": "REPLAY_PASS",
            "diagnostic": {},
        },
        policy={
            "verifier_profiles": {
                "lean-clean": {
                    "toolchain": "lean-4.32",
                    "mathlib_commit": "a" * 40,
                    "adapter_name": "lean-replay",
                    "binary_sha256": "b" * 64,
                    "verifier_writer_capability_ids": ["cap-1"],
                }
            }
        },
    )

    assert decision.rejection_code == "REPLAY_FAILED"


def test_replay_pass_with_missing_artifact_fails_closed() -> None:
    decision = _decide(
        _snapshot(
            "RUNNING",
            extra={
                "claims": [
                    {
                        "claim_id": "claim-1",
                        "run_id": RUN_ID,
                        "contract_version": 1,
                        "statement_hash": "1" * 64,
                        "status": "ACTIVE",
                    }
                ]
            },
        ),
        "RecordLeanFeedback",
        {
            "claim_id": "claim-1",
            "attempt_id": "attempt-1",
            "contract_version": 1,
            "environment_profile_id": "lean-clean",
            "toolchain": "lean-4.32",
            "mathlib_commit": "a" * 40,
            "source_artifact_id": "missing-source",
            "output_artifact_id": "missing-output",
            "feedback_kind": "REPLAY_PASS",
            "diagnostic": {},
        },
        policy={
            "verifier_profiles": {
                "lean-clean": {
                    "toolchain": "lean-4.32",
                    "mathlib_commit": "a" * 40,
                    "adapter_name": "lean-replay",
                    "verifier_writer_capability_ids": ["cap-1"],
                }
            }
        },
    )

    assert decision.rejection_code == "ARTIFACT_MISSING"


def test_component_usage_requires_named_component_and_nonnegative_counters() -> None:
    missing = _decide(
        _snapshot("RUNNING"),
        "RecordBudget",
        {
            "event_kind": "ACTUAL",
            "resource_kind": "WALL_SECOND",
            "amount_microunits": 1,
            "unit": "microsecond",
            "provider_usage": {},
        },
    )
    invalid = _decide(
        _snapshot("RUNNING"),
        "RecordBudget",
        {
            "event_kind": "ACTUAL",
            "resource_kind": "INPUT_TOKEN",
            "amount_microunits": 1,
            "unit": "microtoken",
            "provider_usage": {"component": "model", "input_tokens": -1},
        },
    )

    assert missing.rejection_code == "INGEST_SCHEMA_INVALID"
    assert invalid.rejection_code == "INGEST_SCHEMA_INVALID"


def test_budget_actual_is_denied_when_hard_limit_is_missing_or_exceeded() -> None:
    payload = {
        "event_kind": "ACTUAL",
        "resource_kind": "INPUT_TOKEN",
        "amount_microunits": 6,
        "unit": "microtoken",
        "provider_usage": {"component": "model", "input_tokens": 6},
    }
    missing = _decide(_snapshot("RUNNING"), "RecordBudget", payload)
    exceeded = _decide(
        _snapshot(
            "RUNNING",
            extra={
                "budget_events": [
                    {
                        "budget_event_id": "budget-1",
                        "resource_kind": "INPUT_TOKEN",
                        "event_kind": "ACTUAL",
                        "amount_microunits": 5,
                    }
                ]
            },
        ),
        "RecordBudget",
        payload,
        policy={"global_budget_limits": {"INPUT_TOKEN": 10}},
    )

    assert missing.rejection_code == "BUDGET_DENIED"
    assert exceeded.rejection_code == "BUDGET_DENIED"


def test_route_attempt_limit_is_enforced() -> None:
    route = {
        "route_id": "route-1",
        "run_id": RUN_ID,
        "status": "ACTIVE",
        "budget_policy": {"attempts": 1},
    }
    attempt = {
        "attempt_id": "attempt-1",
        "route_id": "route-1",
        "ordinal": 1,
        "status": "SUCCEEDED",
        "allowed_write_set": ["attempt-1"],
    }
    decision = _decide(
        _snapshot("RUNNING", extra={"routes": [route], "attempts": [attempt]}),
        "RegisterAttempt",
        {
            "route_id": "route-1",
            "ordinal": 2,
            "isolation_epoch": 2,
            "work_relpath": "attempts/2/work",
            "allowed_write_set": ["attempt-2"],
            "input_snapshot_digest": "a" * 64,
        },
    )

    assert decision.rejection_code == "BUDGET_DENIED"


def test_same_inputs_produce_equal_decisions() -> None:
    snapshot = _snapshot("OPEN", contract_status="DRAFT")
    command = {
        "contract_version": 1,
        "completeness_check_artifact_id": ARTIFACT_ID,
    }

    first = _decide(snapshot, "FreezeContract", command, evidence={"contract_complete": True})
    second = _decide(snapshot, "FreezeContract", command, evidence={"contract_complete": True})

    assert first == second
