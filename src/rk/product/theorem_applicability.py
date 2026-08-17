"""Manual theorem applicability review; it does not validate mathematical truth."""

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class ApplicabilityReview:
    applicability_id: str
    context_id: str
    target_link_id: str
    verdict: str
    reviewed_by: str


class TheoremApplicabilityStore:
    def __init__(self, db_path: Path) -> None:
        self._db = Path(db_path)

    def review(
        self,
        *,
        applicability_id: str,
        context_id: str,
        target_link_id: str,
        quantifiers: dict[str, object],
        assumptions: dict[str, object],
        symbols: dict[str, object],
        verdict: str,
        reviewed_by: str,
        reviewed_at: str,
    ) -> ApplicabilityReview:
        if not reviewed_by or not all((quantifiers, assumptions, symbols)):
            raise ValueError("manual quantifier, assumption, and symbol reviews are required")
        with sqlite3.connect(self._db) as c:
            c.execute("PRAGMA foreign_keys=ON")
            c.execute(
                "INSERT INTO product_theorem_applicability_reviews VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    applicability_id,
                    context_id,
                    target_link_id,
                    _j(quantifiers),
                    _j(assumptions),
                    _j(symbols),
                    verdict,
                    reviewed_by,
                    reviewed_at,
                ),
            )
        return ApplicabilityReview(
            applicability_id, context_id, target_link_id, verdict, reviewed_by
        )


def _j(v: object) -> str:
    return json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


__all__ = ["ApplicabilityReview", "TheoremApplicabilityStore"]
