from __future__ import annotations

import hashlib

import pytest

from rk.paper import VerifiedPaper, paper_math_review_status


def _projection() -> dict[str, object]:
    def claim(fact_id: str, label: str, statement: str, proof: str) -> dict[str, object]:
        return {
            "claim_id": fact_id,
            "stable_label": label,
            "claim_kind": "LEMMA",
            "contract_version": 1,
            "statement_hash": fact_id * 64,
            "normalized_statement": {
                "statement": statement,
                "proof": proof,
                "atomic": True,
            },
            "lifecycle": "ACTIVE",
            "machine": "KERNEL_VERIFIED",
            "semantic": "TESTED",
            "peer": "UNREVIEWED",
        }

    claims = [
        claim("a", "Base", "For $n=0$ the sum is zero.", "This is immediate."),
        claim("b", "Step", "The recurrence preserves the formula.", "Apply the hypothesis."),
        claim("c", "Odd sum", "$1+3+\\cdots +(2n-1)=n^2$.", "Induct using the step."),
        claim("s", "Sibling", "$2+2=4$.", "By arithmetic."),
    ]
    claims[2]["claim_kind"] = "ROOT"
    return {
        "status": "CLOSED",
        "final_outcome": "PROVED",
        "root_claim_id": "c",
        "terminal_claim_ids": ["c"],
        "claims": [
            *claims,
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
                "edge_kind": "DEPENDS_ON",
                "status": "ACTIVE",
            },
        ],
    }


def test_paper_uses_only_topological_effective_closure() -> None:
    paper = VerifiedPaper().build(_projection(), "c")
    text = paper.tex.decode("utf-8")
    assert paper.fact_ids == ("a", "b", "c")
    assert text.index("label{fact:1}") < text.index("label{fact:2}") < text.index("label{fact:3}")
    assert "Sibling" not in text


def test_generated_paper_compiles() -> None:
    projection = _projection()
    projection["claims"][0]["normalized_statement"]["statement"] = (
        "For n=0 the sum is 0^2 & the formula holds."
    )
    paper = VerifiedPaper().build(projection, "c")
    assert "$0^2$ \\&" in paper.tex.decode("utf-8")
    pdf, log = VerifiedPaper().compile_pdf(paper.tex)
    assert pdf.startswith(b"%PDF")
    assert "Output written on main.pdf" in log


def test_pdf_delivery_requires_review_of_exact_tex() -> None:
    projection = _projection()
    paper = VerifiedPaper().build(projection, "c")
    projection["paper_reviews"] = [
        {
            "final_fact_id": "c",
            "paper_sha256": hashlib.sha256(paper.tex).hexdigest(),
            "status": "CORRECT",
        }
    ]
    digest = hashlib.sha256(paper.tex).hexdigest()
    assert paper_math_review_status(projection, "c", digest) == "CORRECT"
    assert paper_math_review_status(projection, "c", "0" * 64) == "PENDING"


def test_open_run_or_lemma_terminal_cannot_masquerade_as_final_paper() -> None:
    projection = _projection()
    projection["status"] = "RUNNING"
    with pytest.raises(ValueError, match="finalized run"):
        VerifiedPaper().build(projection, "c")


def test_candidate_tex_requires_truth_closed_root_and_matches_final_tex() -> None:
    projection = _projection()
    projection["status"] = "RUNNING"
    projection["final_outcome"] = None
    projection["terminal_claim_ids"] = []
    projection["claims"][2]["closure"] = "CLOSED_HUMAN"

    candidate = VerifiedPaper().build_candidate(projection, "c")

    finalized = _projection()
    finalized["claims"][2]["closure"] = "CLOSED_HUMAN"
    assert candidate.tex == VerifiedPaper().build(finalized, "c").tex

    projection["claims"][2]["closure"] = "OPEN"
    with pytest.raises(ValueError, match="truth-closed ROOT"):
        VerifiedPaper().build_candidate(projection, "c")

    projection = _projection()
    projection["root_claim_id"] = "a"
    projection["terminal_claim_ids"] = ["a"]
    with pytest.raises(ValueError, match="unique ROOT"):
        VerifiedPaper().build(projection, "c")
