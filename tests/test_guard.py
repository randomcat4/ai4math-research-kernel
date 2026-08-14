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
            {"artifact_id": ARTIFACT_ID, "ingest_state": "COMMITTED", "sha256": "b" * 64},
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


def test_command_set_is_the_closed_31_command_union() -> None:
    assert len(COMMAND_TYPES) == 31
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


def _peer_review_payload() -> dict[str, object]:
    return {
        "claim_id": "claim-1",
        "contract_version": 1,
        "statement_hash": "a" * 64,
        "review_artifact_id": ARTIFACT_ID,
        "verdict": "ACCEPT",
        "checklist": {
            "proof_checked": {
                "passed": True,
                "status": "HUMAN_ATTESTED",
                "conclusion": "proof checked",
                "evidence_refs": [ARTIFACT_ID],
            },
            "scope_checked": {
                "passed": True,
                "status": "HUMAN_ATTESTED",
                "conclusion": "scope checked",
                "evidence_refs": [ARTIFACT_ID],
            },
            "blind_review": False,
        },
        "source_graph": {"review_artifact_id": ARTIFACT_ID},
        "verifier_attestation": {
            "artifact_sha256": "b" * 64,
            "verifier_identity_id": "cap-1",
            "verifier_subject_id": "tester",
            "promotion_eligible": True,
            "authority": "HUMAN_ATTESTED",
            "claim_id": "claim-1",
            "contract_version": 1,
            "statement_hash": "a" * 64,
            "verdict": "ACCEPT",
            "selected_subgraph_digest": None,
        },
    }


def test_peer_review_rejects_caller_asserted_independent_boolean() -> None:
    snapshot = _snapshot(
        "RUNNING",
        extra={"claims": [{"claim_id": "claim-1", "statement_hash": "a" * 64}]},
    )

    payload = _peer_review_payload()
    payload["independence_profile"] = {"independent": True}
    decision = _decide(snapshot, "RecordPeerReview", payload)

    assert not decision.accepted
    assert decision.rejection_code == "INDEPENDENCE_UNKNOWN"


def test_peer_review_overwrites_unverified_dimensions_with_host_unknown() -> None:
    snapshot = _snapshot(
        "RUNNING",
        extra={"claims": [{"claim_id": "claim-1", "statement_hash": "a" * 64}]},
    )
    decision = _decide(snapshot, "RecordPeerReview", _peer_review_payload())

    assert decision.accepted
    derived = decision.projection_mutations[0]["independence_profile"]
    assert derived["idea_independence"] == "UNKNOWN"
    assert derived["reasons"] == ["HUMAN_REVIEW_CHECKLIST_INCOMPLETE"]


def test_peer_review_requires_bound_managed_verifier_attestation() -> None:
    snapshot = _snapshot(
        "RUNNING",
        extra={"claims": [{"claim_id": "claim-1", "statement_hash": "a" * 64}]},
    )
    payload = _peer_review_payload()
    del payload["verifier_attestation"]
    missing = _decide(snapshot, "RecordPeerReview", payload)
    assert not missing.accepted
    assert missing.rejection_code == "INDEPENDENCE_UNKNOWN"

    payload = _peer_review_payload()
    payload["verifier_attestation"]["authority"] = "NONE"
    untrusted = _decide(snapshot, "RecordPeerReview", payload)
    assert not untrusted.accepted
    assert untrusted.rejection_code == "INDEPENDENCE_UNKNOWN"


@pytest.mark.parametrize("name", ["proof_checked", "scope_checked"])
def test_peer_review_rejects_unsigned_boolean_checklist_flags(name: str) -> None:
    snapshot = _snapshot(
        "RUNNING",
        extra={"claims": [{"claim_id": "claim-1", "statement_hash": "a" * 64}]},
    )
    payload = _peer_review_payload()
    payload["checklist"][name] = True
    decision = _decide(snapshot, "RecordPeerReview", payload)
    assert not decision.accepted
    assert decision.rejection_code == "INDEPENDENCE_UNKNOWN"
    assert decision.missing_conditions[0].code == "SIGNED_REVIEW_CHECKS"


def test_peer_promotion_ignores_ephemeral_summary_reviews() -> None:
    claim = {
        "claim_id": "claim-1",
        "statement_hash": "a" * 64,
        "status": "ACTIVE",
        "run_id": RUN_ID,
        "contract_version": 1,
    }
    injected = {
        "review_id": "review-1",
        "claim_id": "claim-1",
        "contract_version": 1,
        "statement_hash": "a" * 64,
        "reviewer_capability_id": "cap-injected",
        "verdict": "ACCEPT",
        "independence_profile": {
            "idea_independence": "INDEPENDENT",
            "derivation_independence": "INDEPENDENT",
            "verification_independence": "INDEPENDENT",
            "implementation_independence": "INDEPENDENT",
            "retrieval_independence": "INDEPENDENT",
            "shared_ancestors": [],
        },
    }

    decision = _decide(
        _snapshot("RUNNING", extra={"claims": [claim]}),
        "PromoteClaim",
        {
            "claim_id": "claim-1",
            "target_axis": "PEER",
            "target_value": "ACCEPTED",
            "evidence_ids": ["review-1"],
        },
        evidence={"reviews": [injected], "peer_reviews": [injected]},
    )

    assert not decision.accepted
    assert decision.rejection_code == "EVIDENCE_INSUFFICIENT"


def test_legacy_human_states_cannot_support_proved_finalize() -> None:
    claim = {
        "claim_id": "claim-1",
        "statement_hash": "a" * 64,
        "route": "ROUTE_PROVED",
        "machine": "UNVERIFIED",
        "semantic": "HUMAN_ATTESTED",
        "peer": "ACCEPTED",
        "quality": "UNREVIEWED",
        "closure": "CLOSED_HUMAN",
        "status": "ACTIVE",
    }
    decision = _decide(
        _snapshot("RUNNING", extra={"claims": [claim]}),
        "Finalize",
        {
            "outcome": "PROVED",
            "terminal_claim_ids": ["claim-1"],
            "open_obligation_ids": [],
            "dossier_spec": {"format": "JSON", "include_raw_artifacts": False},
        },
    )

    assert not decision.accepted
    assert decision.rejection_code == "TERMINAL_CLAIM_UNSUPPORTED"


def test_self_reported_hard_counterexample_cannot_finalize_disproved() -> None:
    claim = {
        "claim_id": "claim-1",
        "statement_hash": "a" * 64,
        "status": "ACTIVE",
        "run_id": RUN_ID,
        "contract_version": 1,
    }
    evidence = {
        "evidence_id": "fake-counterexample",
        "claim_id": "claim-1",
        "evidence_type": "COUNTEREXAMPLE",
        "evidence_strength": "HARD_MACHINE",
        "status": "ACTIVE",
    }
    decision = _decide(
        _snapshot("RUNNING", extra={"claims": [claim], "evidence": [evidence]}),
        "Finalize",
        {
            "outcome": "DISPROVED",
            "terminal_claim_ids": ["claim-1"],
            "open_obligation_ids": [],
            "dossier_spec": {"format": "JSON", "include_raw_artifacts": False},
        },
    )

    assert not decision.accepted
    assert decision.rejection_code == "TERMINAL_CLAIM_UNSUPPORTED"


def test_accepted_managed_human_closure_witness_can_be_repromoted() -> None:
    claim = {
        "claim_id": "claim-1",
        "statement_hash": "a" * 64,
        "status": "ACTIVE",
        "run_id": RUN_ID,
        "contract_version": 1,
    }
    witness = {
        "witness_id": "witness-1",
        "parent_claim_id": "claim-1",
        "composition_mode": "PEER",
        "status": "ACCEPTED",
    }
    decision = _decide(
        _snapshot(
            "RUNNING",
            extra={
                "claims": [claim],
                "closure_witnesses": [witness],
                    "evidence": [{
                        "evidence_id": "evidence-1", "status": "ACCEPTED",
                        "claim_id": "claim-1", "contract_version": 1,
                        "statement_hash": "a" * 64,
                    }],
            },
        ),
        "PromoteClaim",
        {
            "claim_id": "claim-1",
            "target_axis": "CLOSURE",
            "target_value": "CLOSED_HUMAN",
            "evidence_ids": ["evidence-1"],
            "closure_witness_id": "witness-1",
        },
    )

    assert decision.accepted


def test_human_argument_edge_is_unavailable_without_managed_human_receipt() -> None:
    claims = [
        {
            "claim_id": "a",
            "statement_hash": "a" * 64,
            "status": "ACTIVE",
            "run_id": RUN_ID,
            "contract_version": 1,
        },
        {
            "claim_id": "b",
            "statement_hash": "b" * 64,
            "status": "ACTIVE",
            "run_id": RUN_ID,
            "contract_version": 1,
        },
    ]
    review = {"review_id": "review-1", "verdict": "ACCEPT"}
    decision = _decide(
        _snapshot("RUNNING", extra={"claims": claims, "peer_reviews": [review]}),
        "RegisterClaimEdge",
        {
            "contract_version": 1,
            "from_claim_id": "a",
            "to_claim_id": "b",
            "edge_kind": "IMPLIES",
            "direction": "FORWARD",
            "justification_kind": "HUMAN_ARGUMENT",
            "justification_ref": "review-1",
        },
    )

    assert not decision.accepted
    assert decision.rejection_code == "COMPOSITION_OPEN"


def test_revoke_fact_requires_an_active_scoped_fact() -> None:
    active = {
        "claim_id": "fact-1",
        "run_id": RUN_ID,
        "contract_version": 1,
        "status": "ACTIVE",
    }
    accepted = _decide(
        _snapshot("RUNNING", extra={"claims": [active]}),
        "RevokeFact",
        {"contract_version": 1, "fact_id": "fact-1", "reason": "proof gap"},
    )
    missing = _decide(
        _snapshot("RUNNING"),
        "RevokeFact",
        {"contract_version": 1, "fact_id": "fact-1", "reason": "proof gap"},
    )
    assert accepted.accepted
    assert accepted.projection_mutations[0]["op"] == "REVOKE_FACT_CLOSURE"
    assert not missing.accepted


def test_research_hint_is_context_only() -> None:
    decision = _decide(
        _snapshot("RUNNING"),
        "RecordResearchHint",
        {
            "contract_version": 1,
            "hint_kind": "CHANGE_REPRESENTATION",
            "hint": "改用生成函数表示",
            "checkpoint_label": "before-route-2",
        },
    )
    assert decision.accepted
    assert decision.projection_mutations == ({"op": "RECORD_RESEARCH_HINT"},)


def test_main_cannot_submit_worker_claim_when_role_ids_are_enforced() -> None:
    decision = TransitionGuard().decide(
        now_utc=NOW,
        snapshot=_snapshot("RUNNING"),
        command=TypedCommand("RegisterClaim", {}),
        evidence_summary={},
        capability=_capability(),
        policy_snapshot={"candidate_writer_capability_ids": ["worker-cap"]},
        expected_revision=0,
    )
    assert not decision.accepted
    assert decision.rejection_code == "CAPABILITY_DENIED"
    assert decision.missing_conditions[0].params["role"] == "WORKER"


def test_worker_cannot_revoke_fact_when_main_role_ids_are_enforced() -> None:
    active = {
        "claim_id": "fact-1",
        "run_id": RUN_ID,
        "contract_version": 1,
        "status": "ACTIVE",
    }
    decision = TransitionGuard().decide(
        now_utc=NOW,
        snapshot=_snapshot("RUNNING", extra={"claims": [active]}),
        command=TypedCommand(
            "RevokeFact", {"contract_version": 1, "fact_id": "fact-1", "reason": "gap"}
        ),
        evidence_summary={},
        capability=_capability(),
        policy_snapshot={"main_capability_ids": ["main-cap"]},
        expected_revision=0,
    )
    assert not decision.accepted
    assert decision.rejection_code == "CAPABILITY_DENIED"


def test_non_verifier_cannot_record_whole_paper_verdict() -> None:
    decision = TransitionGuard().decide(
        now_utc=NOW,
        snapshot=_snapshot("RUNNING"),
        command=TypedCommand("RecordPaperReview", {}),
        evidence_summary={},
        capability=_capability(),
        policy_snapshot={"verifier_capability_ids": ["verifier-cap"]},
        expected_revision=0,
    )
    assert not decision.accepted
    assert decision.rejection_code == "CAPABILITY_DENIED"
    assert decision.missing_conditions[0].params["role"] == "VERIFIER"


def test_soft_verifier_can_reject_with_feedback_but_cannot_accept_fact() -> None:
    atomic = {
        "claim_id": "claim-1",
        "run_id": RUN_ID,
        "contract_version": 1,
        "status": "ACTIVE",
        "normalized_statement": {"atomic": True},
    }
    snapshot = _snapshot("RUNNING", extra={"claims": [atomic]})
    rejected = _decide(
        snapshot,
        "VerifyAtomicClaim",
        {
            "contract_version": 1,
            "claim_id": "claim-1",
            "backend": "SOFT_VERIFIER",
            "verdict": "REJECTED",
            "repair_feedback": "归纳步缺少边界条件",
        },
    )
    accepted = _decide(
        snapshot,
        "VerifyAtomicClaim",
        {
            "contract_version": 1,
            "claim_id": "claim-1",
            "backend": "SOFT_VERIFIER",
            "verdict": "ACCEPTED",
        },
    )
    assert rejected.accepted
    assert rejected.projection_mutations[0]["accepted"] is False
    assert not accepted.accepted
    assert accepted.rejection_code == "EVIDENCE_INSUFFICIENT"


def test_start_interrupt_resume_and_unresolved_finalize() -> None:
    contract = {
        "version": 1,
        "status": "FROZEN",
        "statement_hash": "a" * 64,
        "contract_artifact_id": ARTIFACT_ID,
        "contract": {"statement": "canonical"},
    }
    root = {
        "claim_id": "root",
        "run_id": RUN_ID,
        "contract_version": 1,
        "claim_kind": "ROOT",
        "statement_hash": "a" * 64,
        "statement_artifact_id": ARTIFACT_ID,
        "normalized_statement": {"statement": "canonical"},
        "status": "ACTIVE",
    }
    started = _decide(
        _snapshot(
            "OPEN",
            extra={"contract": contract, "claims": [root], "root_claim_id": "root"},
        ),
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


def test_start_run_requires_an_active_canonical_contract_root() -> None:
    decision = _decide(
        _snapshot("OPEN"),
        "StartRun",
        {
            "contract_version": 1,
            "literature_plan_artifact_id": ARTIFACT_ID,
            "budget_policy": {"global": {"CPU_SECOND": 1000}},
        },
    )

    assert not decision.accepted
    assert decision.rejection_code == "EVIDENCE_SCOPE_MISMATCH"


def test_same_active_label_with_different_statement_hash_is_rejected() -> None:
    import hashlib
    import json

    statement = {"atomic": True, "statement": "new statement"}
    digest = hashlib.sha256(
        json.dumps(statement, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()
    snapshot = _snapshot(
        "RUNNING",
        extra={
            "claims": [
                {
                    "claim_id": "old",
                    "run_id": RUN_ID,
                    "contract_version": 1,
                    "stable_label": "shared-label",
                    "statement_hash": "f" * 64,
                    "status": "ACTIVE",
                }
            ],
            "artifacts": [
                {
                    "artifact_id": ARTIFACT_ID,
                    "ingest_state": "COMMITTED",
                    "sha256": digest,
                }
            ],
        },
    )
    decision = _decide(
        snapshot,
        "RegisterClaim",
        {
            "contract_version": 1,
            "claim_kind": "LEMMA",
            "stable_label": "shared-label",
            "statement_artifact_id": ARTIFACT_ID,
            "statement_hash": digest,
            "normalized_statement": statement,
        },
    )
    assert not decision.accepted
    assert decision.rejection_code == "INGEST_SCHEMA_INVALID"


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
                "contract_version": 1,
                "statement_hash": "1" * 64,
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


def test_promote_unknown_claim_returns_stable_scope_error() -> None:
    decision = _decide(
        _snapshot("RUNNING"),
        "PromoteClaim",
        {
            "claim_id": "missing-claim",
            "target_axis": "MACHINE",
            "target_value": "KERNEL_VERIFIED",
            "evidence_ids": ["missing-evidence"],
        },
    )

    assert not decision.accepted
    assert decision.rejection_code == "EVIDENCE_SCOPE_MISMATCH"


@pytest.mark.parametrize("value", ["LOCAL_LEMMAS_VERIFIED", "ROUTE_LOCAL"])
def test_unscoped_local_route_promotion_is_unavailable(value: str) -> None:
    claim = {
        "claim_id": "claim-1",
        "statement_hash": "a" * 64,
        "status": "ACTIVE",
        "run_id": RUN_ID,
        "contract_version": 1,
    }
    evidence = {"evidence_id": "evidence-1", "status": "ACCEPTED"}
    decision = _decide(
        _snapshot("RUNNING", extra={"claims": [claim], "evidence": [evidence]}),
        "PromoteClaim",
        {
            "claim_id": "claim-1",
            "target_axis": "ROUTE",
            "target_value": value,
            "evidence_ids": ["evidence-1"],
        },
    )

    assert not decision.accepted
    assert decision.rejection_code == "COMPOSITION_OPEN"


def test_illegal_axis_value_is_rejected_before_projection() -> None:
    claim = {
        "claim_id": "claim-1",
        "statement_hash": "a" * 64,
        "status": "ACTIVE",
        "run_id": RUN_ID,
        "contract_version": 1,
    }
    evidence = {"evidence_id": "evidence-1", "status": "ACCEPTED"}
    decision = _decide(
        _snapshot("RUNNING", extra={"claims": [claim], "evidence": [evidence]}),
        "PromoteClaim",
        {
            "claim_id": "claim-1",
            "target_axis": "ROUTE",
            "target_value": "NOT_A_STATE",
            "evidence_ids": ["evidence-1"],
        },
    )

    assert not decision.accepted
    assert decision.rejection_code == "INGEST_SCHEMA_INVALID"


def test_route_local_finalize_is_unavailable_without_scoped_closure() -> None:
    claim = {
        "claim_id": "claim-1",
        "statement_hash": "a" * 64,
        "route": "ROUTE_LOCAL",
        "status": "ACTIVE",
    }
    decision = _decide(
        _snapshot("RUNNING", extra={"claims": [claim]}),
        "Finalize",
        {
            "outcome": "ROUTE_LOCAL",
            "terminal_claim_ids": ["claim-1"],
            "open_obligation_ids": [],
            "dossier_spec": {"format": "JSON", "include_raw_artifacts": False},
        },
    )

    assert not decision.accepted
    assert decision.rejection_code == "TERMINAL_CLAIM_UNSUPPORTED"


def test_nonroot_proved_terminal_cannot_close_the_run() -> None:
    root = {
        "claim_id": "root",
        "claim_kind": "ROOT",
        "status": "ACTIVE",
        "route": "UNASSESSED",
        "run_id": RUN_ID,
    }
    side = {
        "claim_id": "side",
        "claim_kind": "SIDE_FINDING",
        "status": "ACTIVE",
        "route": "ROUTE_PROVED",
        "machine": "KERNEL_VERIFIED",
        "semantic": "HUMAN_ATTESTED",
        "peer": "UNREVIEWED",
        "closure": "NOT_REQUIRED",
        "run_id": RUN_ID,
    }
    snapshot = _snapshot("RUNNING", extra={"claims": [root, side]})
    snapshot = RunSnapshot(
        run_id=snapshot.run_id,
        status=snapshot.status,
        revision=snapshot.revision,
        current_contract_version=snapshot.current_contract_version,
        last_cursor=snapshot.last_cursor,
        projection={**snapshot.projection, "root_claim_id": "root"},
    )
    decision = _decide(
        snapshot,
        "Finalize",
        {
            "outcome": "PROVED",
            "terminal_claim_ids": ["side"],
            "open_obligation_ids": [],
            "dossier_spec": {"format": "JSON", "include_raw_artifacts": False},
        },
    )

    assert not decision.accepted
    assert decision.rejection_code == "TERMINAL_CLAIM_UNSUPPORTED"


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
            "contract_version": 1,
            "statement_hash": "1" * 64,
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
        "contract_version": 1,
        "statement_hash": "1" * 64,
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
        policy={"budget_controller_capability_ids": ["cap-1"]},
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
        policy={"budget_controller_capability_ids": ["cap-1"]},
    )

    assert missing.rejection_code == "INGEST_SCHEMA_INVALID"
    assert invalid.rejection_code == "INGEST_SCHEMA_INVALID"


def test_budget_provider_usage_rejects_reserved_host_trust_namespace() -> None:
    decision = _decide(
        _snapshot("RUNNING"),
        "RecordBudget",
        {
            "event_kind": "RESERVATION",
            "resource_kind": "INPUT_TOKEN",
            "amount_microunits": 1,
            "unit": "microtoken",
            "provider_usage": {
                "component": "model",
                "_rk_trust": "HOST_VERIFIED",
                "input_tokens": 99,
            },
        },
        policy={
            "global_budget_limits": {"INPUT_TOKEN": 10},
            "budget_controller_capability_ids": ["cap-1"],
        },
    )

    assert decision.rejection_code == "INGEST_SCHEMA_INVALID"


def test_legacy_budget_generation_does_not_consume_new_reservation_limit() -> None:
    decision = _decide(
        _snapshot(
            "RUNNING",
            extra={
                "budget_events": [
                    {
                        "budget_event_id": "legacy-reservation",
                        "resource_kind": "INPUT_TOKEN",
                        "event_kind": "RESERVATION",
                        "amount_microunits": 10,
                        "provider_usage": {
                            "component": "legacy-model",
                            "_rk_trust": "LEGACY_UNTRUSTED",
                        },
                    }
                ]
            },
        ),
        "RecordBudget",
        {
            "event_kind": "RESERVATION",
            "resource_kind": "INPUT_TOKEN",
            "amount_microunits": 1,
            "unit": "microtoken",
            "provider_usage": {"component": "new-model"},
        },
        policy={
            "global_budget_limits": {"INPUT_TOKEN": 10},
            "budget_controller_capability_ids": ["cap-1"],
        },
    )

    assert decision.accepted


def test_budget_actual_is_denied_when_hard_limit_is_missing_or_exceeded() -> None:
    payload = {
        "event_kind": "ACTUAL",
        "resource_kind": "INPUT_TOKEN",
        "amount_microunits": 6,
        "unit": "microtoken",
        "provider_usage": {"component": "model", "input_tokens": 6},
    }
    missing = _decide(
        _snapshot("RUNNING"),
        "RecordBudget",
        payload,
        policy={"budget_controller_capability_ids": ["cap-1"]},
    )
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
        policy={
            "global_budget_limits": {"INPUT_TOKEN": 10},
            "budget_controller_capability_ids": ["cap-1"],
        },
    )

    assert missing.rejection_code == "EVIDENCE_INSUFFICIENT"
    assert exceeded.rejection_code == "EVIDENCE_INSUFFICIENT"


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
