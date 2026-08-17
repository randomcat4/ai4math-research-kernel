"""Narrow adapter from orchestration telemetry to the public activity allowlist."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from rk.product.work_activity import WorkActivityStore


class OrchestratorActivityAdapter:
    def __init__(self, store: WorkActivityStore) -> None:
        self._store = store

    def ingest(
        self,
        *,
        worker_run_id: str,
        event_type: str,
        payload: Mapping[str, Any],
        research_revision: int,
    ) -> int:
        return self._store.record_public_activity(
            worker_run_id,
            event_type=event_type,
            raw_payload=payload,
            research_revision=research_revision,
        )


__all__ = ["OrchestratorActivityAdapter"]
