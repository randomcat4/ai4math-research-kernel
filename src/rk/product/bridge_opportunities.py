"""Traceable far-domain opportunities that can only bind an existing BridgeSpec."""

# ruff: noqa: E501

from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rk.sqlite import open_sqlite


class BridgeOpportunityError(RuntimeError):
    pass


@dataclass(frozen=True, slots=True)
class OpportunityMetrics:
    domain_distance: int
    source_method_maturity: int
    target_domain_absence: int
    native_tool_advantage: int
    expected_certificate_compression: int
    mapping_loss: int
    assumption_loss: int
    backtranslation_cost: int

    def __post_init__(self) -> None:
        if any(
            isinstance(x, bool) or not isinstance(x, int) or not 0 <= x <= 1_000_000
            for x in self.values()
        ):
            raise ValueError("opportunity metrics must be integer micropoints")

    def values(self) -> tuple[int, ...]:
        return (
            self.domain_distance,
            self.source_method_maturity,
            self.target_domain_absence,
            self.native_tool_advantage,
            self.expected_certificate_compression,
            self.mapping_loss,
            self.assumption_loss,
            self.backtranslation_cost,
        )

    @property
    def ranking_score(self) -> int:
        return sum(self.values()[:5]) - sum(self.values()[5:])


@dataclass(frozen=True, slots=True)
class BridgeOpportunity:
    opportunity_id: str
    run_id: str
    target_domain: str
    metrics: OpportunityMetrics
    ranking_score: int
    state: str
    rejection_reason: str | None


@dataclass(frozen=True, slots=True)
class DeathTest:
    death_test_id: str
    test_rank: int
    test_kind: str
    status: str
    receipt_artifact_id: str
    elapsed_ms: int
    cost_microunits: int
    failure_code: str | None


class BridgeOpportunityStore:
    REQUIRED_DEATH_TESTS = frozenset({"COUNTEREXAMPLE", "ROUNDTRIP", "ASSUMPTION_LOSS"})

    def __init__(self, db_path: Path) -> None:
        self._db = Path(db_path)

    def propose(
        self,
        *,
        opportunity_id: str,
        run_id: str,
        route_id: str | None,
        source_problem: dict[str, object],
        target_domain: str,
        metrics: OpportunityMetrics,
        mapping_definition: dict[str, object],
        assumption_audit: dict[str, object],
        backtranslation_plan: dict[str, object],
        selection_reason: str,
        created_at: str,
    ) -> BridgeOpportunity:
        if (
            not source_problem
            or not target_domain
            or not mapping_definition
            or not assumption_audit
            or not backtranslation_plan
            or not selection_reason
        ):
            raise ValueError("complete opportunity definition is required")
        with self._connect() as c:
            c.execute(
                "INSERT INTO product_bridge_opportunities VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,'EVALUATING',NULL,?,?)",
                (
                    opportunity_id,
                    run_id,
                    route_id,
                    _j(source_problem),
                    target_domain,
                    *metrics.values(),
                    metrics.ranking_score,
                    _j(mapping_definition),
                    _j(assumption_audit),
                    _j(backtranslation_plan),
                    selection_reason,
                    created_at,
                    created_at,
                ),
            )
        return self.get(opportunity_id)

    def record_death_test(
        self,
        *,
        opportunity_id: str,
        death_test_id: str,
        test_rank: int,
        test_kind: str,
        specification: dict[str, object],
        status: str,
        receipt_artifact_id: str,
        elapsed_ms: int,
        cost_microunits: int,
        failure_code: str | None,
        recorded_at: str,
    ) -> DeathTest:
        if not specification or not receipt_artifact_id:
            raise ValueError("death test requires specification and receipt")
        with self._connect() as c:
            state = c.execute(
                "SELECT state FROM product_bridge_opportunities WHERE opportunity_id=?",
                (opportunity_id,),
            ).fetchone()
            if state != ("EVALUATING",):
                raise BridgeOpportunityError("death tests are closed")
            c.execute(
                "INSERT INTO product_bridge_death_tests VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                (
                    death_test_id,
                    opportunity_id,
                    test_rank,
                    test_kind,
                    _j(specification),
                    status,
                    receipt_artifact_id,
                    elapsed_ms,
                    cost_microunits,
                    failure_code,
                    recorded_at,
                ),
            )
        return DeathTest(
            death_test_id,
            test_rank,
            test_kind,
            status,
            receipt_artifact_id,
            elapsed_ms,
            cost_microunits,
            failure_code,
        )

    def finalize(self, opportunity_id: str, *, updated_at: str) -> BridgeOpportunity:
        with self._connect() as c:
            c.execute("BEGIN IMMEDIATE")
            rows = c.execute(
                "SELECT test_rank,test_kind,status,failure_code FROM product_bridge_death_tests WHERE opportunity_id=? ORDER BY test_rank",
                (opportunity_id,),
            ).fetchall()
            if not rows or [int(r[0]) for r in rows] != list(range(1, len(rows) + 1)):
                raise BridgeOpportunityError("death tests must have contiguous fastest-first ranks")
            kinds = {str(r[1]) for r in rows}
            if not self.REQUIRED_DEATH_TESTS.issubset(kinds):
                raise BridgeOpportunityError("required death tests are incomplete")
            failures = [r for r in rows if str(r[2]) != "PASSED"]
            if failures:
                reason = ";".join(f"{r[1]}:{r[3]}" for r in failures)
                state = "REJECTED"
            else:
                reason = None
                state = "ELIGIBLE"
            changed = c.execute(
                "UPDATE product_bridge_opportunities SET state=?,rejection_reason=?,updated_at=? WHERE opportunity_id=? AND state='EVALUATING'",
                (state, reason, updated_at, opportunity_id),
            ).rowcount
            if changed != 1:
                raise BridgeOpportunityError("opportunity is not evaluating")
            c.commit()
        return self.get(opportunity_id)

    def bind_existing_bridge(
        self, opportunity_id: str, *, bridge_spec_id: str, bound_at: str
    ) -> BridgeOpportunity:
        with self._connect() as c:
            c.execute("BEGIN IMMEDIATE")
            row = c.execute(
                "SELECT o.run_id,o.state,b.run_id FROM product_bridge_opportunities o LEFT JOIN bridges b ON b.bridge_id=? WHERE o.opportunity_id=?",
                (bridge_spec_id, opportunity_id),
            ).fetchone()
            if row is None or str(row[1]) != "ELIGIBLE":
                raise BridgeOpportunityError("only an eligible opportunity can bind BridgeSpec")
            if row[2] is None or str(row[0]) != str(row[2]):
                raise BridgeOpportunityError("BridgeSpec must already exist in the same run")
            c.execute(
                "INSERT INTO product_bridge_opportunity_bindings VALUES(?,?,?)",
                (opportunity_id, bridge_spec_id, bound_at),
            )
            c.execute(
                "UPDATE product_bridge_opportunities SET state='BRIDGE_REGISTERED',updated_at=? WHERE opportunity_id=?",
                (bound_at, opportunity_id),
            )
            c.commit()
        return self.get(opportunity_id)

    def get(self, opportunity_id: str) -> BridgeOpportunity:
        with self._connect() as c:
            r = c.execute(
                "SELECT opportunity_id,run_id,target_domain,domain_distance,source_method_maturity,target_domain_absence,native_tool_advantage,expected_certificate_compression,mapping_loss,assumption_loss,backtranslation_cost,ranking_score,state,rejection_reason FROM product_bridge_opportunities WHERE opportunity_id=?",
                (opportunity_id,),
            ).fetchone()
        if r is None:
            raise KeyError(opportunity_id)
        metrics = OpportunityMetrics(*map(int, r[3:11]))
        return BridgeOpportunity(
            str(r[0]),
            str(r[1]),
            str(r[2]),
            metrics,
            int(r[11]),
            str(r[12]),
            str(r[13]) if r[13] is not None else None,
        )

    def for_run(self, run_id: str) -> tuple[BridgeOpportunity, ...]:
        with self._connect() as c:
            rows = c.execute(
                "SELECT opportunity_id FROM product_bridge_opportunities WHERE run_id=? "
                "ORDER BY ranking_score DESC,opportunity_id",
                (run_id,),
            ).fetchall()
        return tuple(self.get(str(row[0])) for row in rows)

    def death_tests(self, opportunity_id: str) -> tuple[DeathTest, ...]:
        with self._connect() as c:
            rows = c.execute(
                "SELECT death_test_id,test_rank,test_kind,status,receipt_artifact_id,"
                "elapsed_ms,cost_microunits,failure_code "
                "FROM product_bridge_death_tests WHERE opportunity_id=? ORDER BY test_rank",
                (opportunity_id,),
            ).fetchall()
        return tuple(
            DeathTest(
                str(row[0]),
                int(row[1]),
                str(row[2]),
                str(row[3]),
                str(row[4]),
                int(row[5]),
                int(row[6]),
                str(row[7]) if row[7] is not None else None,
            )
            for row in rows
        )

    def _connect(self) -> sqlite3.Connection:
        c = open_sqlite(self._db, isolation_level=None)
        c.execute("PRAGMA foreign_keys=ON")
        return c


def _j(v: Any) -> str:
    return json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


__all__ = [
    "BridgeOpportunity",
    "BridgeOpportunityError",
    "BridgeOpportunityStore",
    "DeathTest",
    "OpportunityMetrics",
]
