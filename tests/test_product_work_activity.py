from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from rk.extensions import ProductActivity
from rk.product.activity_store import ActivityStore
from rk.product.orchestrator_activity import OrchestratorActivityAdapter
from rk.product.work_activity import WorkActivityError, WorkActivityStore
from rk.product_migrations import ProductMigrationAssembler, ProductMigrationRegistry


class Ids:
    def __init__(self) -> None:
        self.value = 0

    def __call__(self) -> str:
        self.value += 1
        return f"id-{self.value:04d}"


class Clock:
    def __init__(self) -> None:
        self.value = 0

    def __call__(self) -> str:
        self.value += 1
        return f"2026-08-13T00:00:{self.value:02d}Z"


def database(tmp_path: Path) -> Path:
    path = tmp_path / "product.sqlite"
    with sqlite3.connect(path, isolation_level=None) as connection:
        ProductMigrationAssembler(ProductMigrationRegistry(Path("schema_fragments"))).apply(
            connection
        )
    return path


def service(
    path: Path, *, ids: Ids | None = None, clock: Clock | None = None
) -> tuple[WorkActivityStore, ActivityStore, Ids, Clock]:
    generated = ids or Ids()
    times = clock or Clock()
    activities = ActivityStore(path)
    return (
        WorkActivityStore(
            db_path=path,
            activities=activities,
            id_generator=generated,
            clock=times,
        ),
        activities,
        generated,
        times,
    )


def create_work(store: WorkActivityStore) -> str:
    return store.create_work_item(
        run_id="run-1",
        logical_key="route-1:prove-main-lemma",
        work_kind="DEVELOP_ROUTE",
        route_id="route-1",
        assignment_summary="证明主引理并提交可验证工件",
        assignment_artifact_ids=("assignment-1",),
        input_artifact_ids=("contract-1", "facts-1"),
        research_revision=7,
    ).work_item_id


def assign_and_start(
    store: WorkActivityStore,
    work_item_id: str,
    *,
    role: str,
    token: str,
) -> str:
    worker = store.assign_worker(
        work_item_id,
        worker_kind="ROLE_EXECUTION",
        role_id=role,
        process_token=token,
        budget_plan={"wall_seconds": 120, "output_tokens": 4000},
        research_revision=7,
    )
    return store.set_worker_state(
        worker.worker_run_id, state="RUNNING", research_revision=7
    ).worker_run_id


def run_attempt(
    store: WorkActivityStore,
    worker_run_id: str,
    *,
    token: str,
    outcome: str,
    exit_code: int | None,
) -> str:
    attempt = store.begin_attempt(worker_run_id, process_token=token, research_revision=7)
    return store.finish_attempt(
        attempt.attempt_id,
        state=outcome,
        exit_code=exit_code,
        diagnostic_code=None if outcome == "SUCCEEDED" else f"{outcome}_DIAGNOSTIC",
        output_artifact_ids=(f"artifact-{token}",) if outcome == "SUCCEEDED" else (),
        research_revision=7,
    ).attempt_id


def test_stable_work_item_replay_returns_same_identity_and_rejects_drift(
    tmp_path: Path,
) -> None:
    path = database(tmp_path)
    store, activities, _ids, _clock = service(path)
    first = create_work(store)
    second = create_work(store)

    assert first == second
    assert len(activities.snapshot(run_id="run-1").records) == 1
    with pytest.raises(WorkActivityError, match="logical key"):
        store.create_work_item(
            run_id="run-1",
            logical_key="route-1:prove-main-lemma",
            work_kind="FALSIFY_ROUTE",
            route_id="route-1",
            assignment_summary="changed assignment",
            research_revision=7,
        )


def test_two_failed_worker_runs_then_success_preserve_all_attempt_history(
    tmp_path: Path,
) -> None:
    path = database(tmp_path)
    store, activities, _ids, _clock = service(path)
    work_item_id = create_work(store)

    worker_one = assign_and_start(
        store, work_item_id, role="PROOF_COUNTEREXAMPLE", token="worker-process-1"
    )
    run_attempt(
        store,
        worker_one,
        token="host-1",
        outcome="FAILED",
        exit_code=1,
    )
    store.set_worker_state(
        worker_one, state="FAILED", stop_reason="ROLE_RUNTIME_FAILED", research_revision=7
    )

    worker_two = assign_and_start(
        store, work_item_id, role="TARGETED_REVISER", token="worker-process-2"
    )
    run_attempt(
        store,
        worker_two,
        token="host-2a",
        outcome="OUTCOME_UNKNOWN",
        exit_code=None,
    )
    run_attempt(
        store,
        worker_two,
        token="host-2b",
        outcome="FAILED",
        exit_code=2,
    )
    store.set_worker_state(
        worker_two, state="FAILED", stop_reason="NO_VALID_CANDIDATE", research_revision=7
    )

    worker_three = assign_and_start(
        store, work_item_id, role="LEAN_FORMALIZER", token="worker-process-3"
    )
    run_attempt(
        store,
        worker_three,
        token="host-3a",
        outcome="FAILED",
        exit_code=1,
    )
    successful_attempt = run_attempt(
        store,
        worker_three,
        token="host-3b",
        outcome="SUCCEEDED",
        exit_code=0,
    )
    store.set_worker_state(worker_three, state="COMPLETED", research_revision=7)

    view = store.get_work_item(work_item_id)

    assert view.aggregate_state == "COMPLETED"
    assert [worker.worker_run_id for worker in view.worker_runs] == [
        worker_one,
        worker_two,
        worker_three,
    ]
    assert [worker.ordinal for worker in view.worker_runs] == [1, 2, 3]
    assert [worker.state for worker in view.worker_runs] == [
        "FAILED",
        "FAILED",
        "COMPLETED",
    ]
    assert [[attempt.state for attempt in worker.attempts] for worker in view.worker_runs] == [
        ["FAILED"],
        ["OUTCOME_UNKNOWN", "FAILED"],
        ["FAILED", "SUCCEEDED"],
    ]
    assert view.worker_runs[2].attempts[1].attempt_id == successful_attempt
    assert view.worker_runs[0].stop_reason == "ROLE_RUNTIME_FAILED"
    assert view.worker_runs[1].stop_reason == "NO_VALID_CANDIDATE"
    with pytest.raises(WorkActivityError, match="cannot be reassigned"):
        store.assign_worker(
            work_item_id,
            worker_kind="ROLE_EXECUTION",
            role_id="ANOTHER_ROLE",
            process_token="worker-process-4",
            budget_plan={},
            research_revision=7,
        )

    records = activities.snapshot(run_id="run-1", limit=1000).records
    assert [record.cursor for record in records] == list(range(1, len(records) + 1))
    assert [record.payload["type"] for record in records].count("WORKER_FAILED") == 2
    assert [record.payload["type"] for record in records].count("WORKER_COMPLETED") == 1
    assert [record.payload["type"] for record in records].count("ATTEMPT_STARTED") == 5
    assert records[-1].entity_refs == {
        "work_item_id": work_item_id,
        "worker_run_id": worker_three,
    }


def test_restart_recovers_exact_pending_worker_and_attempt_but_new_execution_gets_new_attempt(
    tmp_path: Path,
) -> None:
    path = database(tmp_path)
    first, _activities, ids, clock = service(path)
    work_item_id = create_work(first)
    worker_run_id = assign_and_start(
        first, work_item_id, role="LEAN_FORMALIZER", token="pending-worker"
    )
    pending = first.begin_attempt(worker_run_id, process_token="pending-host", research_revision=7)

    restarted, activities, _ids, _clock = service(path, ids=ids, clock=clock)
    recovered_worker = restarted.recover_pending_worker(
        worker_run_id, process_token="pending-worker", research_revision=7
    )
    recovered_attempt = restarted.recover_pending_attempt(
        pending.attempt_id, process_token="pending-host", research_revision=7
    )

    assert recovered_worker.worker_run_id == worker_run_id
    assert recovered_attempt.attempt_id == pending.attempt_id
    assert recovered_attempt.ordinal == 1
    with pytest.raises(WorkActivityError, match="exact pending"):
        restarted.recover_pending_attempt(
            pending.attempt_id, process_token="wrong-host", research_revision=7
        )
    restarted.finish_attempt(
        pending.attempt_id,
        state="FAILED",
        exit_code=1,
        diagnostic_code="PROCESS_FAILED",
        research_revision=7,
    )
    next_attempt = restarted.begin_attempt(
        worker_run_id, process_token="new-host-execution", research_revision=7
    )
    assert next_attempt.attempt_id != pending.attempt_id
    assert next_attempt.ordinal == 2
    view = restarted.get_work_item(work_item_id)
    assert len(view.worker_runs) == 1
    assert [attempt.attempt_id for attempt in view.worker_runs[0].attempts] == [
        pending.attempt_id,
        next_attempt.attempt_id,
    ]
    event_types = [
        record.payload["type"] for record in activities.snapshot(run_id="run-1", limit=1000).records
    ]
    assert "WORKER_RECOVERED" in event_types
    assert "ATTEMPT_RECOVERED" in event_types


def test_public_orchestrator_allowlist_drops_reasoning_completion_and_transcript(
    tmp_path: Path,
) -> None:
    path = database(tmp_path)
    store, activities, _ids, _clock = service(path)
    work_item_id = create_work(store)
    worker_run_id = assign_and_start(
        store, work_item_id, role="ROUTE_SCOUT", token="worker-process"
    )
    adapter = OrchestratorActivityAdapter(store)

    diagnostic_cursor = adapter.ingest(
        worker_run_id=worker_run_id,
        event_type="DIAGNOSTIC_RECORDED",
        payload={
            "diagnostic_code": "LEAN_EXIT_NONZERO",
            "diagnostic_summary": "Lean 编译失败, 详见正式日志工件",
            "severity": "ERROR",
            "exit_code": 1,
            "artifact_ids": ["stderr-artifact"],
            "reasoning": "hidden chain must disappear",
            "raw_completion": "private model completion must disappear",
            "transcript": {"thought": "nested hidden text"},
        },
        research_revision=7,
    )
    search_cursor = adapter.ingest(
        worker_run_id=worker_run_id,
        event_type="SEARCH_RECORDED",
        payload={
            "connector": "OpenAlex",
            "status": "SUCCEEDED",
            "result_count": 12,
            "query_artifact_id": "query-snapshot",
            "snapshot_artifact_id": "result-snapshot",
            "duration_ms": 83,
            "query": "private raw query",
            "raw_completion": {"provider": "hidden"},
        },
        research_revision=7,
    )

    assert search_cursor == diagnostic_cursor + 1
    records = activities.snapshot(after_cursor=diagnostic_cursor - 1, run_id="run-1").records
    diagnostic, search = records
    assert diagnostic.payload == {
        "type": "DIAGNOSTIC_RECORDED",
        "diagnostic_code": "LEAN_EXIT_NONZERO",
        "diagnostic_summary": "Lean 编译失败, 详见正式日志工件",
        "severity": "ERROR",
        "exit_code": 1,
        "artifact_ids": ["stderr-artifact"],
    }
    assert search.payload == {
        "type": "SEARCH_RECORDED",
        "connector": "OpenAlex",
        "status": "SUCCEEDED",
        "result_count": 12,
        "query_artifact_id": "query-snapshot",
        "snapshot_artifact_id": "result-snapshot",
        "duration_ms": 83,
    }
    with sqlite3.connect(path) as connection:
        persisted = "\n".join(
            str(row[0])
            for row in connection.execute(
                "SELECT payload_json FROM product_activity_events ORDER BY cursor"
            )
        )
    for forbidden in (
        "hidden chain",
        "private model completion",
        "nested hidden text",
        "private raw query",
        '"raw_completion"',
        '"reasoning"',
        '"transcript"',
    ):
        assert forbidden not in persisted


def test_nonpublic_or_incomplete_orchestrator_event_does_not_allocate_cursor(
    tmp_path: Path,
) -> None:
    path = database(tmp_path)
    store, activities, _ids, _clock = service(path)
    work_item_id = create_work(store)
    worker_run_id = assign_and_start(
        store, work_item_id, role="ROUTE_SCOUT", token="worker-process"
    )
    adapter = OrchestratorActivityAdapter(store)
    before = activities.snapshot(run_id="run-1").last_cursor

    with pytest.raises(WorkActivityError, match="not public"):
        adapter.ingest(
            worker_run_id=worker_run_id,
            event_type="MODEL_REASONING",
            payload={"reasoning": "hidden"},
            research_revision=7,
        )
    with pytest.raises(WorkActivityError, match="missing formal"):
        adapter.ingest(
            worker_run_id=worker_run_id,
            event_type="SEARCH_RECORDED",
            payload={"raw_completion": "hidden"},
            research_revision=7,
        )
    assert activities.snapshot(run_id="run-1").last_cursor == before


def test_work_host_and_other_activity_share_one_global_cursor(tmp_path: Path) -> None:
    path = database(tmp_path)
    store, activities, _ids, _clock = service(path)
    work_item_id = create_work(store)
    external_cursor = activities.append(
        ProductActivity(
            event_id="external-event",
            scope_kind="RUN",
            run_id="run-other",
            source="TOOL",
            research_revision=2,
            entity_refs={"tool_run_id": "tool-1"},
            payload={"type": "TOOL_FINISHED"},
            recorded_at="2026-08-13T00:01:00Z",
        )
    )
    worker_run_id = assign_and_start(
        store, work_item_id, role="LEAN_FORMALIZER", token="worker-process"
    )
    attempt = store.begin_attempt(worker_run_id, process_token="host-process", research_revision=7)

    snapshot = activities.snapshot(after_cursor=0, limit=1000)
    assert external_cursor == 2
    assert [record.cursor for record in snapshot.records] == [1, 2, 3, 4, 5]
    assert snapshot.records[-1].entity_refs["attempt_id"] == attempt.attempt_id
    assert snapshot.records[1].run_id == "run-other"


def test_b09a_fragment_assembles_atomically_with_foreign_keys(tmp_path: Path) -> None:
    path = database(tmp_path)
    with sqlite3.connect(path) as connection:
        assert connection.execute("PRAGMA integrity_check").fetchone() == ("ok",)
        assert connection.execute("PRAGMA foreign_key_check").fetchall() == []
        tables = {
            str(row[0])
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name LIKE 'product_work%'"
            )
        }
    assert tables == {
        "product_work_items",
        "product_worker_runs",
        "product_worker_attempts",
    }
