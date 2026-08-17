"""Deterministic paper assembly and compilation from a verified fact closure."""

from __future__ import annotations

import re
import tempfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rk.factgraph import VerifiedFactGraph
from rk.latex_compile import compile_latex


def _tex(value: object) -> str:
    text = str(value)
    replacements = {
        "&": r"\&",
        "%": r"\%",
        "#": r"\#",
        "_": r"\_",
        "{": r"\{",
        "}": r"\}",
    }
    return "".join(replacements.get(char, char) for char in text)


_EXPLICIT_MATH = re.compile(r"(\$[^$]+\$|\\\([^)]*\\\)|\\\[[^]]*\\\])")
_IMPLICIT_POWER_OR_SUBSCRIPT = re.compile(
    r"\(?[A-Za-z0-9][A-Za-z0-9()+\-]*(?:[\^_](?:\{[^{}]+\}|[A-Za-z0-9+\-]+))+"
)


def _tex_body(value: str) -> str:
    """Preserve explicit math and make small inline algebra safe in prose."""

    def prose(segment: str) -> str:
        chunks: list[str] = []
        cursor = 0
        for match in _IMPLICIT_POWER_OR_SUBSCRIPT.finditer(segment):
            chunks.append(_tex(segment[cursor : match.start()]))
            chunks.append(f"${match.group(0)}$")
            cursor = match.end()
        chunks.append(_tex(segment[cursor:]))
        return "".join(chunks)

    parts = _EXPLICIT_MATH.split(value)
    return "".join(part if index % 2 else prose(part) for index, part in enumerate(parts))


def _body(statement: Mapping[str, Any], key: str) -> str:
    value = statement.get(key)
    if isinstance(value, str):
        return value.strip()
    return ""


@dataclass(frozen=True, slots=True)
class PaperArtifact:
    tex: bytes
    fact_ids: tuple[str, ...]


class VerifiedPaper:
    """Build one paper from exactly one final fact's effective dependency closure."""

    def build(
        self, projection: Mapping[str, Any], final_fact_id: str, *, title: str | None = None
    ) -> PaperArtifact:
        root_claim_id = projection.get("root_claim_id")
        if (
            projection.get("status") != "CLOSED"
            or not projection.get("final_outcome")
            or final_fact_id != root_claim_id
            or list(projection.get("terminal_claim_ids", ())) != [root_claim_id]
        ):
            raise ValueError("paper requires a finalized run with the unique ROOT terminal")
        return self._assemble(projection, final_fact_id, title=title)

    def build_candidate(
        self, projection: Mapping[str, Any], final_fact_id: str, *, title: str | None = None
    ) -> PaperArtifact:
        """Build the exact pre-finalization TeX that an independent reviewer must sign.

        This does not deliver a paper: it requires the active ROOT to have passed the truth and
        closure gates, and only exposes deterministic TeX for a digest-bound whole-paper review.
        Final PDF export remains unavailable until Finalize records this ROOT as the unique
        terminal and the signed review of these exact bytes has been persisted.
        """

        root_claim_id = projection.get("root_claim_id")
        claims = projection.get("claims", ())
        root = next(
            (
                item
                for item in claims
                if isinstance(item, Mapping) and item.get("claim_id") == root_claim_id
            ),
            None,
        )
        if (
            final_fact_id != root_claim_id
            or not isinstance(root, Mapping)
            or root.get("claim_kind") != "ROOT"
            or root.get("lifecycle") != "ACTIVE"
            or root.get("closure") not in {"CLOSED_MACHINE", "CLOSED_HUMAN", "CLOSED_HYBRID"}
        ):
            raise ValueError("candidate paper requires an active, truth-closed ROOT claim")
        return self._assemble(projection, final_fact_id, title=title)

    def _assemble(
        self, projection: Mapping[str, Any], final_fact_id: str, *, title: str | None = None
    ) -> PaperArtifact:
        graph = VerifiedFactGraph(projection)
        facts = graph.dependency_closure([final_fact_id])
        if not facts:
            raise ValueError("paper closure is empty")
        blocks: list[str] = []
        citations: set[str] = set()
        for index, fact in enumerate(facts, 1):
            statement = fact["statement"]
            if not isinstance(statement, Mapping):
                raise ValueError(f"fact {fact['fact_id']} has no structured statement")
            statement_text = _body(statement, "statement") or _body(statement, "text")
            proof = _body(statement, "proof") or _body(statement, "evidence")
            if fact["fact_id"] == final_fact_id and not proof:
                signed_conclusions = [
                    str(check.get("conclusion"))
                    for review in projection.get("peer_reviews", ())
                    if isinstance(review, Mapping)
                    and review.get("claim_id") == final_fact_id
                    and review.get("trust_class") == "MANAGED_PEER_REVIEW"
                    and review.get("promotion_eligible") is True
                    and isinstance((checklist := review.get("checklist")), Mapping)
                    and isinstance((check := checklist.get("proof_checked")), Mapping)
                    and check.get("passed") is True
                    and str(check.get("conclusion", "")).strip()
                ]
                proof = "\n".join(signed_conclusions)
            if not statement_text or not proof:
                raise ValueError(f"fact {fact['fact_id']} lacks statement or proof text")
            raw_citations = statement.get("citations", ())
            if isinstance(raw_citations, Sequence) and not isinstance(raw_citations, (str, bytes)):
                citations.update(str(item) for item in raw_citations if str(item).strip())
            kind = "theorem" if fact["fact_id"] == final_fact_id else "lemma"
            blocks.append(
                f"\\begin{{{kind}}}[{_tex(fact['stable_label'])}]\\label{{fact:{index}}}\n"
                f"{_tex_body(statement_text)}\n\\end{{{kind}}}\n"
                f"\\begin{{proof}}\n{_tex_body(proof)}\n\\end{{proof}}"
            )
        bibliography = "\n".join(
            f"\\bibitem{{ref{i}}} {_tex(reference)}"
            for i, reference in enumerate(sorted(citations), 1)
        )
        bibliography_block = (
            "\\begin{thebibliography}{99}\n" + bibliography + "\n\\end{thebibliography}"
            if bibliography
            else ""
        )
        paper_title = title or str(facts[-1]["stable_label"])
        tex = (
            "\\documentclass[11pt]{article}\n"
            "\\usepackage{iftex}\n"
            "\\ifPDFTeX\\usepackage[T1]{fontenc}\\else"
            "\\usepackage{fontspec}\\setmainfont{Noto Sans CJK SC}\\fi\n"
            "\\usepackage{amsmath,amsthm,amssymb}\n"
            "\\newtheorem{theorem}{Theorem}\n\\newtheorem{lemma}{Lemma}\n"
            f"\\title{{{_tex(paper_title)}}}\n\\author{{ResearchKernel}}\n"
            "\\begin{document}\n\\maketitle\n\n"
            + "\n\n".join(blocks)
            + ("\n\n" + bibliography_block if bibliography_block else "")
            + "\n\\end{document}\n"
        )
        return PaperArtifact(tex.encode("utf-8"), tuple(str(item["fact_id"]) for item in facts))

    def compile_pdf(self, tex: bytes, executable: str = "pdflatex") -> tuple[bytes, str]:
        with tempfile.TemporaryDirectory(prefix="rk-paper-") as directory:
            root = Path(directory)
            source = root / "main.tex"
            source.write_bytes(tex)
            completed = compile_latex(root, executable=executable, timeout_seconds=120)
            log = (completed.stdout + b"\n" + completed.stderr).decode(
                "utf-8", errors="replace"
            ).strip()
            if completed.returncode != 0 or completed.pdf is None:
                tail = "\n".join(log.splitlines()[-40:])
                raise RuntimeError(f"LaTeX compilation failed:\n{tail}")
            return completed.pdf, log


def paper_math_review_status(
    projection: Mapping[str, Any], final_fact_id: str, paper_sha256: str
) -> str:
    """Return the persisted whole-paper review status; absence is an honest blocker."""

    reviews = projection.get("paper_reviews", ())
    for review in reversed(list(reviews) if isinstance(reviews, Sequence) else []):
        if (
            isinstance(review, Mapping)
            and review.get("final_fact_id") == final_fact_id
            and review.get("paper_sha256") == paper_sha256
        ):
            status = str(review.get("status", "PENDING"))
            if re.fullmatch(r"CORRECT|WRONG|PENDING|OVERRIDDEN", status):
                return status
    return "PENDING"


__all__ = ["PaperArtifact", "VerifiedPaper", "paper_math_review_status"]
