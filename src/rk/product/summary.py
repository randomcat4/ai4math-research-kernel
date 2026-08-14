"""Orthogonal research summaries projected from authoritative product state."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class BudgetSummary:
    reserved_microunits: int
    actual_microunits: int
    refunded_microunits: int
    unknown_cost_count: int

    def as_dict(self) -> dict[str, int]:
        return {
            "reserved_microunits": self.reserved_microunits,
            "actual_microunits": self.actual_microunits,
            "refunded_microunits": self.refunded_microunits,
            "unknown_cost_count": self.unknown_cost_count,
        }


@dataclass(frozen=True, slots=True)
class ResearchSummaryProjection:
    outcome_state: str
    execution_state: str
    authority_state: str
    publication_state: str
    phase: str
    blockers: tuple[str, ...]
    next_actions: tuple[str, ...]
    available_actions: tuple[dict[str, Any], ...]
    budget: BudgetSummary
    recent_activity_at: str
    recent_activity_summary: str
    research_revision: int
    contract_version: int
    last_cursor: int
    projection_source_digest: str

    def __post_init__(self) -> None:
        if self.research_revision < 0 or self.contract_version < 1 or self.last_cursor < 0:
            raise ValueError("invalid authoritative fence")
        if len(self.projection_source_digest) != 64:
            raise ValueError("projection digest must be sha256")
