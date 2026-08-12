import json

import pytest

from rk.domain import RunSnapshot, frozen_mapping
from rk.dossier import DossierBuilder


def _claim(claim_id: str, *, machine: str) -> dict[str, str]:
    return {
        "claim_id": claim_id,
        "claim_kind": "ROOT",
        "stable_label": "根命题",
        "normalized_statement": {"statement": r"证明 $\\sum_{i=1}^n i=n(n+1)/2$"},
        "route": "UNASSESSED",
        "machine": machine,
        "semantic": "UNREVIEWED",
        "peer": "UNREVIEWED",
        "quality": "UNREVIEWED",
        "closure": "NOT_REQUIRED",
    }


def _snapshot(*claims: dict[str, str]) -> RunSnapshot:
    snapshot = RunSnapshot(
        run_id="run",
        status="CLOSED",
        revision=7,
        current_contract_version=2,
        last_cursor=10,
        projection=frozen_mapping(
            {
                "final_outcome": "UNRESOLVED",
                "contract": {
                    "statement_hash": "a" * 64,
                    "contract": {
                        "statement": r"证明 $\\sum_{i=1}^n i=n(n+1)/2$",
                        "exact_negation": "存在一个正整数 n 使等式不成立。",
                    },
                },
                "claims": list(claims),
                "evidence": [
                    {
                        "evidence_id": "candidate",
                        "ingest_status": "ACCEPTED",
                        "evidence_strength": "HARD_MACHINE",
                        "trust_class": "UNMANAGED_CANDIDATE",
                        "authority_effect": "NONE",
                        "promotion_eligible": False,
                    }
                ],
                "peer_reviews": [
                    {
                        "review_id": "review",
                        "verdict": "ACCEPT",
                        "trust_class": "UNMANAGED_REVIEW",
                        "authority_effect": "NONE",
                        "promotion_eligible": False,
                    }
                ],
                "bindings": [
                    {
                        "binding_id": "binding",
                        "trust_class": "UNMANAGED_BINDING",
                        "authority_effect": "NONE",
                        "promotion_eligible": False,
                    }
                ],
                "lean_feedback": [
                    {
                        "lean_feedback_id": "feedback",
                        "feedback_kind": "REPLAY_PASS",
                        "trust_class": "V01_UNSCOPED_FEEDBACK",
                        "authority_effect": "NONE",
                        "promotion_eligible": False,
                    }
                ],
                "open_obligation_ids": [],
            }
        ),
    )
    return snapshot


def test_dossier_is_byte_stable_and_sorts_claims() -> None:
    snapshot = _snapshot(_claim("b", machine="UNVERIFIED"), _claim("a", machine="KERNEL_VERIFIED"))
    spec = {"format": "JSON", "language": "zh-CN", "include_raw_artifacts": False}
    builder = DossierBuilder()

    first, media = builder.build(snapshot, spec)
    second, _ = builder.build(snapshot, spec)

    assert first == second
    assert first.index(b'"claim_id":"a"') < first.index(b'"claim_id":"b"')
    assert media == "application/json"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("route", "ROUTE_PROVED"),
        ("machine", "KERNEL_VERIFIED"),
        ("semantic", "HUMAN_ATTESTED"),
        ("peer", "ACCEPTED"),
        ("quality", "ACCEPTED"),
        ("closure", "CLOSED_MACHINE"),
    ],
)
def test_markdown_and_json_use_the_same_canonical_claim_states(field: str, value: str) -> None:
    claim = _claim("claim-1", machine="UNVERIFIED")
    claim[field] = value
    snapshot = _snapshot(claim)
    builder = DossierBuilder()

    json_bytes, _ = builder.build(
        snapshot, {"format": "JSON", "language": "zh-CN", "include_raw_artifacts": False}
    )
    markdown_bytes, _ = builder.build(
        snapshot,
        {"format": "MARKDOWN", "language": "zh-CN", "include_raw_artifacts": False},
    )

    exported_claim = json.loads(json_bytes)["projection"]["claims"][0]
    assert exported_claim[field] == value
    assert f"{field}={value}" in markdown_bytes.decode("utf-8")


def test_dossier_fails_closed_when_a_canonical_claim_state_is_missing() -> None:
    claim = _claim("claim-1", machine="KERNEL_VERIFIED")
    del claim["machine"]

    with pytest.raises(ValueError, match="missing canonical state machine"):
        DossierBuilder().build(
            _snapshot(claim),
            {"format": "MARKDOWN", "language": "zh-CN", "include_raw_artifacts": False},
        )


def test_dossier_fails_closed_on_conflicting_legacy_state() -> None:
    claim = _claim("claim-1", machine="KERNEL_VERIFIED")
    claim["machine_verdict"] = "UNVERIFIED"

    with pytest.raises(ValueError, match="conflicting machine and machine_verdict"):
        DossierBuilder().build(
            _snapshot(claim),
            {"format": "MARKDOWN", "language": "zh-CN", "include_raw_artifacts": False},
        )


def test_chinese_markdown_contains_problem_negation_and_outcome() -> None:
    data, media = DossierBuilder().build(
        _snapshot(_claim("root", machine="UNVERIFIED")),
        {"format": "MARKDOWN", "language": "zh-CN", "include_raw_artifacts": False},
    )

    text = data.decode("utf-8")
    assert media == "text/markdown; charset=utf-8"
    assert r"\\sum_{i=1}^n" in text
    assert "存在一个正整数 n" in text
    assert "最终结论: `UNRESOLVED`" in text
    assert "根命题" in text
    assert "信任分类=UNMANAGED_CANDIDATE" in text
    assert "权威作用=NONE" in text
    assert "可晋级=False" in text
    assert "原始意见=ACCEPT" in text
