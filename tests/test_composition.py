from __future__ import annotations

import unicodedata
from copy import deepcopy
from typing import Any, cast

import pytest
from hypothesis import given
from hypothesis import strategies as st

from rk.composition import (
    CanonicalizationError,
    canonical_json_bytes,
    selected_subgraph_digest,
    validate_closure_witness,
)
from rk.domain import RunSnapshot

HASH_PARENT = "1" * 64
HASH_CHILD = "2" * 64


def _part(status: str) -> dict[str, str]:
    return {"ref": f"ref-{status.lower()}", "status": status}


def _case(
    *,
    part_status: str = "MACHINE_CHECKED",
    rule: str = "LEAN_DECLARATION",
    mode: str = "MACHINE",
) -> tuple[RunSnapshot, dict[str, object], dict[str, object], dict[str, object]]:
    claims = [
        {
            "claim_id": "child",
            "run_id": "run-1",
            "contract_version": 1,
            "claim_kind": "LEMMA",
            "statement_revision": 1,
            "statement_hash": HASH_CHILD,
            "status": "ACTIVE",
        },
        {
            "claim_id": "parent",
            "run_id": "run-1",
            "contract_version": 1,
            "claim_kind": "ROOT",
            "statement_revision": 1,
            "statement_hash": HASH_PARENT,
            "status": "ACTIVE",
        },
    ]
    edge = {
        "edge_id": "edge-1",
        "run_id": "run-1",
        "contract_version": 1,
        "from_claim_id": "child",
        "to_claim_id": "parent",
        "edge_kind": "IMPLIES",
        "direction": "FORWARD",
        "justification_kind": rule,
        "justification_ref": "machine-1" if mode != "PEER" else "review-1",
        "status": "ACTIVE",
    }
    obligation = {
        "obligation_id": "obligation-1",
        "run_id": "run-1",
        "contract_version": 1,
        "parent_claim_id": "parent",
        "composition_rule": rule,
        "closure_theorem_ref": "closure-ref",
        "status": "OPEN",
        **{
            f"{name}_ref": f"ref-{name}"
            for name in (
                "coverage",
                "compatibility",
                "invariant",
                "progress",
                "boundary",
                "simultaneous_choice",
            )
        },
        **{
            f"{name}_status": part_status
            for name in (
                "coverage",
                "compatibility",
                "invariant",
                "progress",
                "boundary",
                "simultaneous_choice",
            )
        },
    }
    selected = {
        "schema": "rk.cgraph.v1",
        "run_id": "run-1",
        "contract_version": 1,
        "parent": {
            "claim_id": "parent",
            "statement_revision": 1,
            "statement_hash": HASH_PARENT,
        },
        "claims": [
            {
                "claim_id": claim["claim_id"],
                "statement_revision": 1,
                "statement_hash": claim["statement_hash"],
                "contract_version": 1,
            }
            for claim in reversed(claims)
        ],
        "edges": [
            {
                "edge_id": "edge-1",
                "from": "child",
                "to": "parent",
                "edge_kind": "IMPLIES",
                "direction": "FORWARD",
                "justification_kind": rule,
                "justification_ref": edge["justification_ref"],
            }
        ],
        "obligations": [
            {
                "obligation_id": "obligation-1",
                "composition_rule": rule,
                "closure_theorem_ref": "closure-ref",
                "parts": {
                    name: _part(part_status)
                    for name in (
                        "coverage",
                        "compatibility",
                        "invariant",
                        "progress",
                        "boundary",
                        "simultaneous_choice",
                    )
                },
            }
        ],
        "bridges": [],
        "cuts": [],
    }
    digest = selected_subgraph_digest(selected)
    payload: dict[str, object] = {
        "parent_claim_id": "parent",
        "contract_version": 1,
        "selected_subgraph": selected,
        "selected_subgraph_digest": digest,
        "discharged_obligation_ids": ["obligation-1"],
        "open_obligation_ids": [],
        "edge_justifications": [
            {"edge_id": "edge-1", "justification_ref": edge["justification_ref"]}
        ],
        "bridge_dependency_ids": [],
        "composition_mode": mode,
        "verification_refs": ["machine-1"] if mode != "PEER" else [],
        "human_attestation_review_ids": ["review-1"] if mode == "PEER" else [],
    }
    evidence: dict[str, object] = {
        "evidence": [
            {
                "evidence_id": "machine-1",
                "evidence_strength": "HARD_MACHINE",
                "evidence_type": "LEAN_REPLAY",
                "status": "ACTIVE",
                "replay_pass": True,
                "sorry_count": 0,
                "axiom_violations": [],
                "native_decide": False,
            }
        ],
        "reviews": [
            {
                "review_id": "review-1",
                "reviewer_capability_id": "human-root-1",
                "claim_id": "parent",
                "contract_version": 1,
                "selected_subgraph_digest": digest,
                "verdict": "ACCEPT",
                "independence_profile": {
                    "idea_independence": "INDEPENDENT",
                    "derivation_independence": "INDEPENDENT",
                    "verification_independence": "INDEPENDENT",
                    "implementation_independence": "INDEPENDENT",
                    "retrieval_independence": "INDEPENDENT",
                    "reasons": ["host-derived test fixture"],
                    "shared_ancestors": [],
                },
            }
        ],
    }
    snapshot = RunSnapshot(
        run_id="run-1",
        status="RUNNING",
        revision=3,
        current_contract_version=1,
        last_cursor=4,
        projection={
            "claims": claims,
            "edges": [edge],
            "obligations": [obligation],
            "bridges": [],
        },
    )
    return snapshot, payload, evidence, {"peer_review_threshold": 1}


def test_machine_composition_is_unavailable_until_every_bearing_ref_is_bound() -> None:
    snapshot, payload, evidence, policy = _case()

    result = validate_closure_witness(snapshot, payload, evidence, policy)

    assert not result.accepted
    assert result.closure_state is None
    assert result.rejection_code == "REPLAY_FAILED"


def test_legacy_independence_profile_cannot_close_as_human() -> None:
    snapshot, payload, evidence, policy = _case(
        part_status="HUMAN_ATTESTED", rule="HUMAN_ARGUMENT", mode="PEER"
    )
    snapshot = RunSnapshot(
        run_id=snapshot.run_id,
        status=snapshot.status,
        revision=snapshot.revision,
        current_contract_version=snapshot.current_contract_version,
        last_cursor=snapshot.last_cursor,
        projection={**dict(snapshot.projection), "peer_reviews": evidence["reviews"]},
    )
    persisted_only = {"evidence": evidence["evidence"]}

    result = validate_closure_witness(snapshot, payload, persisted_only, policy)

    assert not result.accepted
    assert result.closure_state is None
    assert result.rejection_code == "INDEPENDENCE_UNKNOWN"


def test_ephemeral_summary_review_cannot_close_as_human() -> None:
    snapshot, payload, evidence, policy = _case(
        part_status="HUMAN_ATTESTED", rule="HUMAN_ARGUMENT", mode="PEER"
    )

    result = validate_closure_witness(snapshot, payload, evidence, policy)

    assert not result.accepted
    assert result.rejection_code == "INDEPENDENCE_UNKNOWN"


def test_human_parts_cannot_be_submitted_as_machine() -> None:
    snapshot, payload, evidence, policy = _case(
        part_status="HUMAN_ATTESTED", rule="HUMAN_ARGUMENT", mode="MACHINE"
    )

    result = validate_closure_witness(snapshot, payload, evidence, policy)

    assert not result.accepted
    assert result.closure_state is None
    assert result.rejection_code == "EVIDENCE_INSUFFICIENT"


def test_cycle_is_rejected_before_mathematical_mode() -> None:
    snapshot, payload, evidence, policy = _case()
    selected = cast(dict[str, Any], deepcopy(payload["selected_subgraph"]))
    selected_edges = cast(list[dict[str, Any]], selected["edges"])
    selected_edges.append(
        {
            "edge_id": "edge-2",
            "from": "parent",
            "to": "child",
            "edge_kind": "IMPLIES",
            "direction": "FORWARD",
            "justification_kind": "LEAN_DECLARATION",
            "justification_ref": "machine-1",
        }
    )
    projection = dict(snapshot.projection)
    projection["edges"] = [
        *projection["edges"],
        {
            "edge_id": "edge-2",
            "run_id": "run-1",
            "contract_version": 1,
            "from_claim_id": "parent",
            "to_claim_id": "child",
            "edge_kind": "IMPLIES",
            "direction": "FORWARD",
            "justification_kind": "LEAN_DECLARATION",
            "justification_ref": "machine-1",
            "status": "ACTIVE",
        },
    ]
    snapshot = RunSnapshot(
        run_id=snapshot.run_id,
        status=snapshot.status,
        revision=snapshot.revision,
        current_contract_version=snapshot.current_contract_version,
        last_cursor=snapshot.last_cursor,
        projection=projection,
    )
    payload["selected_subgraph"] = selected
    payload["selected_subgraph_digest"] = selected_subgraph_digest(selected)
    edge_justifications = cast(list[dict[str, Any]], payload["edge_justifications"])
    edge_justifications.append({"edge_id": "edge-2", "justification_ref": "machine-1"})

    result = validate_closure_witness(snapshot, payload, evidence, policy)

    assert not result.accepted
    assert result.rejection_code == "COMPOSITION_OPEN"
    assert result.missing_conditions[0].params["reason"] == "cycle"


def test_undeclared_boundary_edge_is_an_open_cut() -> None:
    snapshot, payload, evidence, policy = _case()
    projection = dict(snapshot.projection)
    projection["claims"] = [
        *projection["claims"],
        {
            "claim_id": "omitted",
            "run_id": "run-1",
            "contract_version": 1,
            "statement_revision": 1,
            "statement_hash": "3" * 64,
            "status": "ACTIVE",
        },
    ]
    projection["edges"] = [
        *projection["edges"],
        {
            "edge_id": "boundary-edge",
            "run_id": "run-1",
            "contract_version": 1,
            "from_claim_id": "omitted",
            "to_claim_id": "parent",
            "edge_kind": "DEPENDS_ON",
            "direction": "FORWARD",
            "justification_kind": "LEAN_DECLARATION",
            "justification_ref": "machine-1",
            "status": "ACTIVE",
        },
    ]
    snapshot = RunSnapshot(
        run_id=snapshot.run_id,
        status=snapshot.status,
        revision=snapshot.revision,
        current_contract_version=snapshot.current_contract_version,
        last_cursor=snapshot.last_cursor,
        projection=projection,
    )

    result = validate_closure_witness(snapshot, payload, evidence, policy)

    assert not result.accepted
    assert result.rejection_code == "COMPOSITION_OPEN"
    assert result.missing_conditions[0].params["undeclared_boundary_edges"] == ["boundary-edge"]


def test_one_way_bridge_cannot_be_traversed_backwards() -> None:
    snapshot, payload, evidence, policy = _case()
    projection = dict(snapshot.projection)
    registered_edge = dict(projection["edges"][0])
    registered_edge.update({"justification_kind": "BRIDGE", "justification_ref": "bridge-1"})
    bridge = {
        "bridge_id": "bridge-1",
        "run_id": "run-1",
        "contract_version": 1,
        "source_claim_id": "parent",
        "target_claim_id": "child",
        "directionality": "ONE_WAY_VALID",
    }
    projection["edges"] = [registered_edge]
    projection["bridges"] = [bridge]
    snapshot = RunSnapshot(
        run_id=snapshot.run_id,
        status=snapshot.status,
        revision=snapshot.revision,
        current_contract_version=snapshot.current_contract_version,
        last_cursor=snapshot.last_cursor,
        projection=projection,
    )
    selected = cast(dict[str, Any], payload["selected_subgraph"])
    selected_edge = cast(list[dict[str, Any]], selected["edges"])[0]
    selected_edge.update({"justification_kind": "BRIDGE", "justification_ref": "bridge-1"})
    selected["bridges"] = [
        {
            "bridge_id": "bridge-1",
            "directionality": "ONE_WAY_VALID",
            "source_claim_id": "parent",
            "target_claim_id": "child",
            "version": 1,
        }
    ]
    payload["bridge_dependency_ids"] = ["bridge-1"]
    payload["edge_justifications"] = [{"edge_id": "edge-1", "justification_ref": "bridge-1"}]
    payload["selected_subgraph_digest"] = selected_subgraph_digest(selected)

    result = validate_closure_witness(snapshot, payload, evidence, policy)

    assert not result.accepted
    assert result.rejection_code == "COMPOSITION_OPEN"
    assert result.missing_conditions[0].code == "BRIDGE_DIRECTION"


def test_digest_normalizes_nfc_lf_sets_and_collection_order() -> None:
    _, payload, _, _ = _case()
    first = cast(dict[str, Any], deepcopy(payload["selected_subgraph"]))
    second = deepcopy(first)
    first["note"] = "e\u0301\r\nline"
    second["note"] = unicodedata.normalize("NFC", "e\u0301") + "\nline"
    cast(list[dict[str, Any]], first["claims"]).reverse()
    first["cuts"] = [{"cut_id": "z", "from_claim_ids": ["b", "a", "b"]}]
    second["cuts"] = [{"cut_id": "z", "from_claim_ids": ["a", "b"]}]

    assert selected_subgraph_digest(first) == selected_subgraph_digest(second)
    assert canonical_json_bytes(first) == canonical_json_bytes(second)


def test_digest_has_versioned_prefix_golden_value() -> None:
    selected = {
        "schema": "rk.cgraph.v1",
        "run_id": "r",
        "contract_version": 1,
        "parent": {
            "claim_id": "p",
            "statement_revision": 1,
            "statement_hash": "0" * 64,
        },
        "claims": [],
        "edges": [],
        "obligations": [],
        "bridges": [],
        "cuts": [],
    }

    assert selected_subgraph_digest(selected) == (
        "ee2e78412f6d60641e68f4a5e9072b35d7ace8c4c6da7d71e7dc159d53345375"
    )


@given(st.one_of(st.floats(allow_nan=True, allow_infinity=True), st.just({"x": 1.5})))
def test_floats_are_never_canonical(value: object) -> None:
    with pytest.raises(CanonicalizationError):
        canonical_json_bytes(value)


def test_canonicalization_does_not_mutate_caller_value() -> None:
    _, payload, _, _ = _case()
    selected = payload["selected_subgraph"]
    before = deepcopy(selected)

    canonical_json_bytes(selected)

    assert selected == before
