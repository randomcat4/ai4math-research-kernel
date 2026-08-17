from __future__ import annotations

import pytest

from rk.domain import RequestValidationError, RunSnapshot
from rk.factgraph import VerifiedFactGraph
from rk.kernel import ResearchKernel


def _claim(claim_id: str, label: str, *, verified: bool = True) -> dict[str, object]:
    return {
        "claim_id": claim_id,
        "stable_label": label,
        "claim_kind": "LEMMA",
        "contract_version": 1,
        "statement_hash": claim_id * 64,
        "normalized_statement": {"text": label, "atomic": True},
        "lifecycle": "ACTIVE",
        "machine": "KERNEL_VERIFIED" if verified else "UNVERIFIED",
        "semantic": "TESTED",
        "peer": "UNREVIEWED",
    }


def _graph() -> VerifiedFactGraph:
    return VerifiedFactGraph(
        {
            "claims": [
                _claim("a", "奇数求和基础"),
                _claim("b", "odd recurrence"),
                _claim("c", "奇数求和定理"),
                _claim("s", "independent sibling"),
                _claim("x", "unverified", verified=False),
            ],
            "edges": [
                {
                    "from_claim_id": "a",
                    "to_claim_id": "b",
                    "edge_kind": "DEPENDS_ON",
                    "status": "ACTIVE",
                },
                {
                    "from_claim_id": "b",
                    "to_claim_id": "c",
                    "edge_kind": "IMPLIES",
                    "status": "ACTIVE",
                },
                {
                    "from_claim_id": "x",
                    "to_claim_id": "c",
                    "edge_kind": "DEPENDS_ON",
                    "status": "ACTIVE",
                },
            ],
        }
    )


def test_search_and_graph_views_only_include_verified_facts() -> None:
    graph = _graph()
    assert graph.fact_ids == ("a", "b", "c", "s")
    assert [item["fact_id"] for item in graph.search("奇数求和")] == ["a", "c"]
    assert [item["fact_id"] for item in graph.predecessors("c")] == ["b"]
    assert [item["fact_id"] for item in graph.successors("a")] == ["b"]


def test_dependency_and_reverse_closure_preserve_sibling() -> None:
    graph = _graph()
    assert [item["fact_id"] for item in graph.dependency_closure(["c"])] == ["a", "b", "c"]
    assert [item["fact_id"] for item in graph.reverse_closure(["b"])] == ["b", "c"]
    assert graph.get("s") is not None


def test_reverse_edge_direction_and_hybrid_closure_are_effective_facts() -> None:
    hybrid = _claim("h", "hybrid conclusion", verified=False)
    hybrid["closure"] = "CLOSED_HYBRID"
    projection = {
        "current_contract_version": 2,
        "claims": [
            {**_claim("old", "superseded truth"), "contract_version": 1},
            {**_claim("a", "current premise"), "contract_version": 2},
            {**hybrid, "contract_version": 2},
        ],
        "edges": [
            {
                # Stored endpoints are intentionally opposite to logical flow.
                "from_claim_id": "h",
                "to_claim_id": "a",
                "edge_kind": "DEPENDS_ON",
                "direction": "REVERSE",
                "contract_version": 2,
                "status": "ACTIVE",
            },
            {
                "from_claim_id": "old",
                "to_claim_id": "h",
                "edge_kind": "DEPENDS_ON",
                "direction": "FORWARD",
                "contract_version": 1,
                "status": "ACTIVE",
            },
        ],
    }
    graph = VerifiedFactGraph(projection)
    assert graph.fact_ids == ("a", "h")
    assert [item["fact_id"] for item in graph.dependency_closure(["h"])] == ["a", "h"]
    assert [item["fact_id"] for item in graph.reverse_closure(["a"])] == ["a", "h"]


def test_unknown_or_unverified_fact_is_rejected() -> None:
    graph = _graph()
    with pytest.raises(KeyError):
        graph.dependency_closure(["x"])


def test_rejected_and_revoked_claims_are_searchable_only_as_negative_knowledge() -> None:
    projection = {
        "claims": [
            _claim("a", "base"),
            _claim("b", "revoked recurrence"),
            _claim("x", "failed induction", verified=False),
        ]
    }
    projection["claims"][1]["lifecycle"] = "REVOKED"
    projection["atomic_verifications"] = [
        {"claim_id": "x", "verdict": "REJECTED", "repair_feedback": "缺少归纳计算"}
    ]
    graph = VerifiedFactGraph(projection)
    assert graph.get("b") is None
    found = graph.search_negative("归纳计算")
    assert found[0]["claim_id"] == "x"


class _Storage:
    def inspect_snapshot(self, run_id: str) -> dict[str, object]:
        projection = {
            "claims": [
                _claim("a", "奇数求和基础"),
                _claim("b", "odd recurrence"),
            ],
            "edges": [
                {
                    "from_claim_id": "a",
                    "to_claim_id": "b",
                    "edge_kind": "DEPENDS_ON",
                    "status": "ACTIVE",
                }
            ],
        }
        return {
            "run_id": run_id,
            "status": "RUNNING",
            "revision": 3,
            "current_contract_version": 1,
            "last_cursor": 3,
            **projection,
        }


def test_fact_query_is_available_through_public_inspect() -> None:
    kernel = object.__new__(ResearchKernel)
    kernel._storage = _Storage()  # type: ignore[attr-defined]
    result = kernel.inspect(
        "run-1", fact_query={"operation": "dependency_closure", "fact_ids": ["b"]}
    )
    assert isinstance(result, RunSnapshot)
    assert [item["fact_id"] for item in result.projection["fact_graph"]] == ["a", "b"]


def test_fact_query_rejects_ambiguous_event_pagination() -> None:
    kernel = object.__new__(ResearchKernel)
    kernel._storage = _Storage()  # type: ignore[attr-defined]
    with pytest.raises(RequestValidationError):
        kernel.inspect("run-1", after_cursor=0, fact_query={"operation": "summary"})
