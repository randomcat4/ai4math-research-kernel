"""Five-arm frozen ablation ledger; execution and verdict authority stay external."""

# ruff: noqa: E501

from __future__ import annotations

import hashlib
import json
import math
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rk.sqlite import open_sqlite
from rk.wire import canonical_json_bytes

GROUPS = ("direct", "near", "far-random", "far-retrieval", "full-RK")


class AblationError(RuntimeError):
    pass


class ConfigurationDrift(AblationError):
    pass


@dataclass(frozen=True, slots=True)
class FrozenAblationConfig:
    problem_pool_digest: str
    problem_ids: tuple[str, ...]
    model_identity: dict[str, object]
    tool_builds: dict[str, object]
    candidate_count: int
    budget: dict[str, object]
    verifier_identity: dict[str, object]
    verifier_profile_receipt_id: str

    def __post_init__(self) -> None:
        if (
            len(self.problem_pool_digest) != 64
            or not self.problem_ids
            or len(set(self.problem_ids)) != len(self.problem_ids)
            or not self.model_identity
            or not self.tool_builds
            or self.candidate_count <= 0
            or not self.budget
            or not self.verifier_identity
            or not self.verifier_profile_receipt_id
        ):
            raise ValueError("complete frozen ablation configuration is required")

    @property
    def digest(self) -> str:
        return hashlib.sha256(
            canonical_json_bytes(
                {
                    "problem_pool_digest": self.problem_pool_digest,
                    "problem_ids": list(self.problem_ids),
                    "model_identity": self.model_identity,
                    "tool_builds": self.tool_builds,
                    "candidate_count": self.candidate_count,
                    "budget": self.budget,
                    "verifier_identity": self.verifier_identity,
                    "verifier_profile_receipt_id": self.verifier_profile_receipt_id,
                }
            )
        ).hexdigest()


@dataclass(frozen=True, slots=True)
class GroupReport:
    group_name: str
    denominator: int
    verified: int
    verification_rate_ppm: int
    wilson_low_ppm: int
    wilson_high_ppm: int
    rejected: int
    inconclusive: int
    execution_failed: int
    rejected_bridges: int
    total_cost_microunits: int
    certificate_lengths: tuple[int, ...]


class AblationStore:
    def __init__(self, db_path: Path) -> None:
        self._db = Path(db_path)

    def freeze(
        self, *, ablation_plan_id: str, run_id: str, config: FrozenAblationConfig, created_at: str
    ) -> str:
        with self._connect() as c:
            c.execute("BEGIN IMMEDIATE")
            c.execute(
                "INSERT INTO product_ablation_plans VALUES(?,?,?,?,?,?,?,?,?,?,?,'FROZEN',?,?)",
                (
                    ablation_plan_id,
                    run_id,
                    config.problem_pool_digest,
                    _j(list(config.problem_ids)),
                    _j(config.model_identity),
                    _j(config.tool_builds),
                    config.candidate_count,
                    _j(config.budget),
                    _j(config.verifier_identity),
                    config.verifier_profile_receipt_id,
                    config.digest,
                    created_at,
                    created_at,
                ),
            )
            c.executemany(
                "INSERT INTO product_ablation_groups VALUES(?,?,?,'PENDING',NULL,NULL,NULL)",
                [(ablation_plan_id, g, config.digest) for g in GROUPS],
            )
            c.executemany(
                "INSERT INTO product_ablation_assignments VALUES(?,?,?,?,NULL,NULL,'PENDING')",
                [
                    (f"{ablation_plan_id}:{g}:{p}", ablation_plan_id, g, p)
                    for g in GROUPS
                    for p in config.problem_ids
                ],
            )
            c.commit()
        return config.digest

    def attach_opportunity(
        self,
        *,
        ablation_plan_id: str,
        group_name: str,
        problem_id: str,
        opportunity_id: str | None,
        rejected_bridge_reason: str | None = None,
    ) -> None:
        if group_name not in {"far-random", "far-retrieval", "full-RK"}:
            raise AblationError("bridge opportunities belong only to far-domain arms")
        with self._connect() as c:
            if rejected_bridge_reason:
                changed = c.execute(
                    "UPDATE product_ablation_assignments SET opportunity_id=?,rejected_bridge_reason=?,state='REJECTED_BRIDGE' WHERE ablation_plan_id=? AND group_name=? AND problem_id=? AND state='PENDING'",
                    (
                        opportunity_id,
                        rejected_bridge_reason,
                        ablation_plan_id,
                        group_name,
                        problem_id,
                    ),
                ).rowcount
            else:
                changed = c.execute(
                    "UPDATE product_ablation_assignments SET opportunity_id=? WHERE ablation_plan_id=? AND group_name=? AND problem_id=? AND state='PENDING'",
                    (opportunity_id, ablation_plan_id, group_name, problem_id),
                ).rowcount
            if changed != 1:
                raise AblationError("assignment cannot be updated")

    def start_group(
        self,
        *,
        ablation_plan_id: str,
        group_name: str,
        frozen_digest: str,
        run_receipt_artifact_id: str,
        started_at: str,
    ) -> None:
        if not run_receipt_artifact_id:
            raise ValueError("real group run receipt is required")
        with self._connect() as c:
            expected = self._expected(c, ablation_plan_id)
            if frozen_digest != expected:
                raise ConfigurationDrift("ablation group configuration drifted")
            changed = c.execute(
                "UPDATE product_ablation_groups SET state='RUNNING',run_receipt_artifact_id=?,started_at=? WHERE ablation_plan_id=? AND group_name=? AND frozen_digest=? AND state='PENDING'",
                (run_receipt_artifact_id, started_at, ablation_plan_id, group_name, frozen_digest),
            ).rowcount
            if changed != 1:
                raise AblationError("group cannot start")
            c.execute(
                "UPDATE product_ablation_plans SET state='RUNNING',updated_at=? WHERE ablation_plan_id=? AND state='FROZEN'",
                (started_at, ablation_plan_id),
            )
            c.execute(
                "UPDATE product_ablation_assignments SET state='RUNNING' WHERE ablation_plan_id=? AND group_name=? AND state='PENDING'",
                (ablation_plan_id, group_name),
            )

    def record_result(
        self,
        *,
        assignment_id: str,
        frozen_digest: str,
        outcome: str,
        cost_microunits: int,
        certificate_length: int | None,
        verifier_profile_receipt_id: str,
        verifier_receipt_artifact_id: str,
        execution_receipt_artifact_id: str,
        failure_code: str | None,
        finished_at: str,
    ) -> None:
        with self._connect() as c:
            row = c.execute(
                "SELECT a.ablation_plan_id,a.state,p.frozen_digest,p.verifier_profile_receipt_id FROM product_ablation_assignments a JOIN product_ablation_plans p ON p.ablation_plan_id=a.ablation_plan_id WHERE a.assignment_id=?",
                (assignment_id,),
            ).fetchone()
            if row is None or str(row[1]) != "RUNNING":
                raise AblationError("only running assignments accept results")
            if frozen_digest != str(row[2]) or verifier_profile_receipt_id != str(row[3]):
                raise ConfigurationDrift("result configuration or final verifier drifted")
            c.execute(
                "INSERT INTO product_ablation_results VALUES(?,?,?,?,?,?,?,?,?,?)",
                (
                    assignment_id,
                    frozen_digest,
                    outcome,
                    cost_microunits,
                    certificate_length,
                    verifier_profile_receipt_id,
                    verifier_receipt_artifact_id,
                    execution_receipt_artifact_id,
                    failure_code,
                    finished_at,
                ),
            )
            state = "SUCCEEDED" if outcome == "VERIFIED" else "FAILED"
            c.execute(
                "UPDATE product_ablation_assignments SET state=? WHERE assignment_id=?",
                (state, assignment_id),
            )

    def complete_group(self, *, ablation_plan_id: str, group_name: str, completed_at: str) -> None:
        with self._connect() as c:
            open_count = c.execute(
                "SELECT COUNT(*) FROM product_ablation_assignments WHERE ablation_plan_id=? AND group_name=? AND state IN ('PENDING','RUNNING')",
                (ablation_plan_id, group_name),
            ).fetchone()
            if int(open_count[0]) != 0:
                raise AblationError("complete group cannot omit denominator members")
            changed = c.execute(
                "UPDATE product_ablation_groups SET state='COMPLETED',completed_at=? WHERE ablation_plan_id=? AND group_name=? AND state='RUNNING'",
                (completed_at, ablation_plan_id, group_name),
            ).rowcount
            if changed != 1:
                raise AblationError("group is not running")
            remaining = c.execute(
                "SELECT COUNT(*) FROM product_ablation_groups WHERE ablation_plan_id=? AND state<>'COMPLETED'",
                (ablation_plan_id,),
            ).fetchone()
            if int(remaining[0]) == 0:
                c.execute(
                    "UPDATE product_ablation_plans SET state='COMPLETED',updated_at=? WHERE ablation_plan_id=?",
                    (completed_at, ablation_plan_id),
                )

    def report(self, ablation_plan_id: str) -> tuple[GroupReport, ...]:
        reports = []
        with self._connect() as c:
            for group in GROUPS:
                rows = c.execute(
                    "SELECT a.state,r.outcome,r.cost_microunits,r.certificate_length FROM product_ablation_assignments a LEFT JOIN product_ablation_results r ON r.assignment_id=a.assignment_id WHERE a.ablation_plan_id=? AND a.group_name=? ORDER BY a.problem_id",
                    (ablation_plan_id, group),
                ).fetchall()
                outcomes = [str(r[1]) for r in rows if r[1] is not None]
                verified = outcomes.count("VERIFIED")
                low, high = _wilson_interval(verified, len(rows))
                reports.append(
                    GroupReport(
                        group,
                        len(rows),
                        verified,
                        round(verified * 1_000_000 / len(rows)),
                        low,
                        high,
                        outcomes.count("REJECTED"),
                        outcomes.count("INCONCLUSIVE"),
                        outcomes.count("EXECUTION_FAILED"),
                        sum(str(r[0]) == "REJECTED_BRIDGE" for r in rows),
                        sum(int(r[2] or 0) for r in rows),
                        tuple(int(r[3]) for r in rows if r[3] is not None),
                    )
                )
        return tuple(reports)

    def _expected(self, c: sqlite3.Connection, plan: str) -> str:
        row = c.execute(
            "SELECT frozen_digest FROM product_ablation_plans WHERE ablation_plan_id=?", (plan,)
        ).fetchone()
        if row is None:
            raise KeyError(plan)
        return str(row[0])

    def _connect(self) -> sqlite3.Connection:
        c = open_sqlite(self._db, isolation_level=None)
        c.execute("PRAGMA foreign_keys=ON")
        return c


def _j(v: Any) -> str:
    return json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _wilson_interval(successes: int, denominator: int) -> tuple[int, int]:
    z = 1.959963984540054
    rate = successes / denominator
    z2 = z * z
    center = (rate + z2 / (2 * denominator)) / (1 + z2 / denominator)
    radius = (
        z
        * math.sqrt(rate * (1 - rate) / denominator + z2 / (4 * denominator * denominator))
        / (1 + z2 / denominator)
    )
    return round(max(0.0, center - radius) * 1_000_000), round(
        min(1.0, center + radius) * 1_000_000
    )


__all__ = [
    "GROUPS",
    "AblationError",
    "AblationStore",
    "ConfigurationDrift",
    "FrozenAblationConfig",
    "GroupReport",
]
