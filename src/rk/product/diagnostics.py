"""Public, typed diagnostics assembled from database projections only."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from rk.product.activity_store import ActivityRecord, ActivityStore
from rk.product.deployment import DeploymentHealthReport, DeploymentHealthService
from rk.sqlite import open_sqlite


@dataclass(frozen=True, slots=True)
class DiagnosticActivity:
    cursor: int
    event_id: str
    source: str
    recorded_at: str
    research_revision: int | None
    event_type: str | None
    public_summary: str | None


@dataclass(frozen=True, slots=True)
class ProjectionStateCount:
    projection: str
    state: str
    count: int


@dataclass(frozen=True, slots=True)
class DiagnosticSnapshot:
    deployment_id: str
    activity_fence: int
    activities: tuple[DiagnosticActivity, ...]
    projection_counts: tuple[ProjectionStateCount, ...]
    latest_health: DeploymentHealthReport | None


class TypedDiagnosticService:
    """Read allowlisted typed columns; arbitrary activity payload is never returned."""

    def __init__(
        self,
        db_path: Path,
        deployment_id: str,
        health: DeploymentHealthService,
    ) -> None:
        if not deployment_id.strip():
            raise ValueError("deployment ID is required")
        self._db_path = Path(db_path)
        self._deployment_id = deployment_id
        self._health = health

    def snapshot(self, *, after_cursor: int = 0, limit: int = 100) -> DiagnosticSnapshot:
        activity = ActivityStore(self._db_path).snapshot(
            after_cursor=after_cursor,
            limit=limit,
            deployment_id=self._deployment_id,
        )
        return DiagnosticSnapshot(
            deployment_id=self._deployment_id,
            activity_fence=activity.last_cursor,
            activities=tuple(_public_activity(record) for record in activity.records),
            projection_counts=self._projection_counts(),
            latest_health=self._health.latest(),
        )

    def _projection_counts(self) -> tuple[ProjectionStateCount, ...]:
        statements = (
            ("JOB", "SELECT state,COUNT(*) FROM product_jobs WHERE deployment_id=? GROUP BY state"),
            ("REVIEW", "SELECT status,COUNT(*) FROM product_review_tasks GROUP BY status"),
            (
                "TOOL",
                "SELECT availability,COUNT(*) FROM product_tool_catalog GROUP BY availability",
            ),
        )
        rows: list[ProjectionStateCount] = []
        with open_sqlite(self._db_path) as connection:
            for projection, statement in statements:
                params: tuple[str, ...] = (self._deployment_id,) if projection == "JOB" else ()
                rows.extend(
                    ProjectionStateCount(projection, str(state), int(count))
                    for state, count in connection.execute(statement, params)
                )
        return tuple(sorted(rows, key=lambda item: (item.projection, item.state)))


def _public_activity(record: ActivityRecord) -> DiagnosticActivity:
    event_type = record.payload.get("event_type")
    public_summary = record.payload.get("public_summary")
    return DiagnosticActivity(
        cursor=record.cursor,
        event_id=record.event_id,
        source=record.source,
        recorded_at=record.recorded_at,
        research_revision=record.research_revision,
        event_type=event_type if isinstance(event_type, str) else None,
        public_summary=public_summary if isinstance(public_summary, str) else None,
    )


__all__ = [
    "DiagnosticActivity",
    "DiagnosticSnapshot",
    "ProjectionStateCount",
    "TypedDiagnosticService",
]
