"""Verified mathematical working memory derived from the RK projection.

The SQLite projection remains the only truth source.  This module owns no fact
files and no durable verdicts: it provides a deep, rebuildable read model over
ACTIVE claims that have passed RK's machine or managed peer truth gate.

The BM25 calculation is adapted from FrenzyMath Danus (Apache-2.0),
``danus/core/bm25.py``.  RK adds CJK tokenisation and graph selectors.
"""

from __future__ import annotations

import json
import math
import re
from collections import Counter, deque
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from typing import Any

_TOKEN_RE = re.compile(r"[A-Za-z0-9_]+|[\u3400-\u9fff]")
_LOGICAL_EDGE_KINDS = frozenset({"IMPLIES", "DEPENDS_ON", "SPECIALIZES", "GENERALIZES"})
_MACHINE_ACCEPTED = frozenset({"KERNEL_VERIFIED", "CERTIFICATE_VERIFIED"})
_SEMANTIC_ACCEPTED = frozenset({"TESTED", "HUMAN_ATTESTED"})


def _tokens(text: str) -> list[str]:
    return _TOKEN_RE.findall(text.lower())


def _bm25(query: str, documents: list[list[str]]) -> list[float]:
    query_counts = Counter(_tokens(query))
    if not query_counts or not documents:
        return [0.0 for _ in documents]
    counts = [Counter(document) for document in documents]
    lengths = [len(document) for document in documents]
    average = sum(lengths) / len(lengths) if lengths else 0.0
    frequencies: Counter[str] = Counter()
    for document in documents:
        frequencies.update(set(document))
    scores: list[float] = []
    for terms, length in zip(counts, lengths, strict=True):
        norm = 1.5 * (0.25 + 0.75 * length / average) if average else 1.5
        score = 0.0
        for token, query_frequency in query_counts.items():
            frequency = terms.get(token, 0)
            if not frequency:
                continue
            document_frequency = frequencies[token]
            inverse = math.log(
                1.0 + (len(documents) - document_frequency + 0.5) / (document_frequency + 0.5)
            )
            score += query_frequency * inverse * frequency * 2.5 / (frequency + norm)
        scores.append(score)
    return scores


def _truth_gate_passed(claim: Mapping[str, Any]) -> bool:
    if claim.get("lifecycle") != "ACTIVE":
        return False
    semantic = str(claim.get("semantic", "UNREVIEWED"))
    machine = str(claim.get("machine", "UNVERIFIED"))
    peer = str(claim.get("peer", "UNREVIEWED"))
    closed = str(claim.get("closure", "OPEN")) in {
        "CLOSED_MACHINE",
        "CLOSED_HUMAN",
        "CLOSED_HYBRID",
    }
    return closed or (
        semantic in _SEMANTIC_ACCEPTED
        and (machine in _MACHINE_ACCEPTED or peer == "ACCEPTED")
    )


@dataclass(frozen=True, slots=True)
class FactView:
    fact_id: str
    stable_label: str
    claim_kind: str
    statement: Mapping[str, Any]
    statement_hash: str
    contract_version: int
    machine: str
    semantic: str
    peer: str

    def search_text(self) -> str:
        return " ".join(
            (
                self.stable_label,
                self.claim_kind,
                json.dumps(self.statement, ensure_ascii=False, sort_keys=True),
            )
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "fact_id": self.fact_id,
            "stable_label": self.stable_label,
            "claim_kind": self.claim_kind,
            "statement": dict(self.statement),
            "statement_hash": self.statement_hash,
            "contract_version": self.contract_version,
            "verification": {
                "machine": self.machine,
                "semantic": self.semantic,
                "peer": self.peer,
            },
        }


class VerifiedFactGraph:
    """Read-only verified fact graph built from one persisted run snapshot."""

    def __init__(self, projection: Mapping[str, Any]) -> None:
        current_version = projection.get("current_contract_version")
        scoped_version = (
            int(current_version)
            if isinstance(current_version, int) and not isinstance(current_version, bool)
            else None
        )
        facts: dict[str, FactView] = {}
        for value in projection.get("claims", ()):
            if not isinstance(value, Mapping) or not _truth_gate_passed(value):
                continue
            if scoped_version is not None and value.get("contract_version") != scoped_version:
                continue
            fact_id = str(value.get("claim_id", ""))
            statement = value.get("normalized_statement")
            if not fact_id or not isinstance(statement, Mapping):
                continue
            facts[fact_id] = FactView(
                fact_id=fact_id,
                stable_label=str(value.get("stable_label", fact_id)),
                claim_kind=str(value.get("claim_kind", "LEMMA")),
                statement=statement,
                statement_hash=str(value.get("statement_hash", "")),
                contract_version=int(value.get("contract_version", 0)),
                machine=str(value.get("machine", "UNVERIFIED")),
                semantic=str(value.get("semantic", "UNREVIEWED")),
                peer=str(value.get("peer", "UNREVIEWED")),
            )
        predecessors: dict[str, set[str]] = {fact_id: set() for fact_id in facts}
        successors: dict[str, set[str]] = {fact_id: set() for fact_id in facts}
        for edge in projection.get("edges", ()):
            if not isinstance(edge, Mapping):
                continue
            source = str(edge.get("from_claim_id", ""))
            target = str(edge.get("to_claim_id", ""))
            if (
                edge.get("status") == "ACTIVE"
                and edge.get("edge_kind") in _LOGICAL_EDGE_KINDS
                and (
                    scoped_version is None or edge.get("contract_version") == scoped_version
                )
                and source in facts
                and target in facts
            ):
                direction = edge.get("direction", "FORWARD")
                arcs = (
                    ((source, target), (target, source))
                    if direction == "BIDIRECTIONAL"
                    else ((target, source),)
                    if direction == "REVERSE"
                    else ((source, target),)
                )
                for predecessor, dependent in arcs:
                    predecessors[dependent].add(predecessor)
                    successors[predecessor].add(dependent)
        self._facts = facts
        self._predecessors = predecessors
        self._successors = successors

        negative: list[dict[str, Any]] = []
        feedback_by_claim: dict[str, list[str]] = {}
        for verification in projection.get("atomic_verifications", ()):
            if (
                isinstance(verification, Mapping)
                and verification.get("verdict") == "REJECTED"
                and (
                    scoped_version is None
                    or verification.get("contract_version") == scoped_version
                )
            ):
                claim_id = str(verification.get("claim_id", ""))
                feedback_by_claim.setdefault(claim_id, []).append(
                    str(verification.get("repair_feedback") or "verification rejected")
                )
        for value in projection.get("claims", ()):
            if not isinstance(value, Mapping):
                continue
            if scoped_version is not None and value.get("contract_version") != scoped_version:
                continue
            claim_id = str(value.get("claim_id", ""))
            lifecycle = str(value.get("lifecycle", "ACTIVE"))
            feedback = feedback_by_claim.get(claim_id, [])
            if lifecycle == "ACTIVE" and not feedback:
                continue
            negative.append(
                {
                    "claim_id": claim_id,
                    "stable_label": str(value.get("stable_label", claim_id)),
                    "lifecycle": lifecycle,
                    "statement": dict(value.get("normalized_statement", {})),
                    "repair_feedback": feedback,
                }
            )
        self._negative = negative

    @property
    def fact_ids(self) -> tuple[str, ...]:
        return tuple(sorted(self._facts))

    def get(self, fact_id: str) -> dict[str, Any] | None:
        fact = self._facts.get(fact_id)
        if fact is None:
            return None
        return self._with_links(fact)

    def search(self, query: str, *, limit: int = 10) -> list[dict[str, Any]]:
        if limit < 1 or limit > 100:
            raise ValueError("fact search limit must be between 1 and 100")
        ordered = [self._facts[fact_id] for fact_id in sorted(self._facts)]
        documents = [_tokens(fact.search_text()) for fact in ordered]
        scores = _bm25(query, documents)
        ranked = sorted(
            zip(ordered, scores, strict=True),
            key=lambda item: (-item[1], item[0].fact_id),
        )
        return [{**self._with_links(fact), "score": score} for fact, score in ranked if score > 0][
            :limit
        ]

    def search_negative(self, query: str, *, limit: int = 10) -> list[dict[str, Any]]:
        if limit < 1 or limit > 100:
            raise ValueError("negative search limit must be between 1 and 100")
        documents = [
            _tokens(json.dumps(item, ensure_ascii=False, sort_keys=True)) for item in self._negative
        ]
        scores = _bm25(query, documents)
        ranked = sorted(
            zip(self._negative, scores, strict=True),
            key=lambda item: (-item[1], item[0]["claim_id"]),
        )
        return [{**item, "score": score} for item, score in ranked if score > 0][:limit]

    def predecessors(self, fact_id: str) -> list[dict[str, Any]]:
        return self._linked(self._predecessors, fact_id)

    def successors(self, fact_id: str) -> list[dict[str, Any]]:
        return self._linked(self._successors, fact_id)

    def dependency_closure(self, fact_ids: Iterable[str]) -> list[dict[str, Any]]:
        return [self._with_links(self._facts[fact_id]) for fact_id in self.topological(fact_ids)]

    def reverse_closure(self, fact_ids: Iterable[str]) -> list[dict[str, Any]]:
        selected = self._closure(fact_ids, self._successors)
        return [self._with_links(self._facts[fact_id]) for fact_id in sorted(selected)]

    def topological(self, fact_ids: Iterable[str]) -> list[str]:
        selected = self._closure(fact_ids, self._predecessors)
        indegree = {fact_id: len(self._predecessors[fact_id] & selected) for fact_id in selected}
        ready = deque(sorted(fact_id for fact_id, degree in indegree.items() if degree == 0))
        result: list[str] = []
        while ready:
            fact_id = ready.popleft()
            result.append(fact_id)
            for successor in sorted(self._successors[fact_id] & selected):
                indegree[successor] -= 1
                if indegree[successor] == 0:
                    ready.append(successor)
        if len(result) != len(selected):
            raise ValueError("verified fact graph contains a cycle")
        return result

    def summary(self) -> dict[str, Any]:
        return {
            "fact_count": len(self._facts),
            "edge_count": sum(len(items) for items in self._successors.values()),
            "fact_ids": list(self.fact_ids),
        }

    def _linked(self, index: Mapping[str, set[str]], fact_id: str) -> list[dict[str, Any]]:
        if fact_id not in self._facts:
            raise KeyError(f"unknown or unverified fact: {fact_id}")
        return [self._with_links(self._facts[item]) for item in sorted(index[fact_id])]

    def _closure(self, seeds: Iterable[str], index: Mapping[str, set[str]]) -> set[str]:
        pending = list(dict.fromkeys(str(seed) for seed in seeds))
        if any(seed not in self._facts for seed in pending):
            missing = sorted(seed for seed in pending if seed not in self._facts)
            raise KeyError(f"unknown or unverified facts: {missing}")
        selected: set[str] = set()
        while pending:
            fact_id = pending.pop()
            if fact_id in selected:
                continue
            selected.add(fact_id)
            pending.extend(sorted(index[fact_id] - selected, reverse=True))
        return selected

    def _with_links(self, fact: FactView) -> dict[str, Any]:
        return {
            **fact.to_dict(),
            "predecessor_fact_ids": sorted(self._predecessors[fact.fact_id]),
            "successor_fact_ids": sorted(self._successors[fact.fact_id]),
        }


__all__ = ["VerifiedFactGraph"]
