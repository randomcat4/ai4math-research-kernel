from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest

from rk.product.action_items import RunActions, aggregate_action_items
from rk.product.listing import (
    CatalogCursorError,
    CatalogFenceChanged,
    ResearchCatalog,
    ResearchListQuery,
)
from rk.product.summary import BudgetSummary, ResearchSummaryProjection


def _db(tmp_path: Path) -> Path:
    path = tmp_path / "catalog.sqlite"
    with sqlite3.connect(path) as c:
        c.executescript(
            (Path(__file__).parents[1] / "schema_fragments/B01b/catalog.sql").read_text()
        )
    return path


def _summary(recent: str, revision: int = 1) -> ResearchSummaryProjection:
    return ResearchSummaryProjection(
        "OPEN",
        "RUNNING",
        "UNVERIFIED",
        "NONE",
        "探索",
        ("等待验证",),
        ("SUBMIT_CLAIM",),
        (
            {
                "command_type": "SUBMIT_CLAIM",
                "principal_subject_id": "math:one",
                "target_ids": [],
                "required_inputs": ["statement"],
                "blocked_by": [],
            },
        ),
        BudgetSummary(100, 20, 0, 0),
        recent,
        "Worker 已提交候选",
        revision,
        1,
        revision,
        "a" * 64,
    )


def test_real_sqlite_catalog_lists_orthogonal_summary_and_keyset(tmp_path: Path) -> None:
    catalog = ResearchCatalog(_db(tmp_path))
    for run, recent in (("r1", "2026-08-13T12:00:00Z"), ("r2", "2026-08-13T11:00:00Z")):
        catalog.register(
            run_id=run,
            title=run,
            question_summary=run,
            owner="math:one",
            labels=("开放题",),
            created_at=recent,
        )
        catalog.project(run, _summary(recent))
    first = catalog.list(ResearchListQuery(limit=1, owners=("math:one",)))
    assert first.items[0]["run_id"] == "r1"
    assert tuple(
        first.items[0][key]
        for key in ("outcome_state", "execution_state", "authority_state", "publication_state")
    ) == ("OPEN", "RUNNING", "UNVERIFIED", "NONE")
    assert first.next_cursor
    second = catalog.list(
        ResearchListQuery(limit=1, owners=("math:one",), cursor=first.next_cursor)
    )
    assert [item["run_id"] for item in second.items] == ["r2"]


def test_cursor_binds_query_digest_and_catalog_fence(tmp_path: Path) -> None:
    catalog = ResearchCatalog(_db(tmp_path))
    for run, recent in (("r1", "2026-08-13T12:00:00Z"), ("r2", "2026-08-13T11:00:00Z")):
        catalog.register(
            run_id=run, title=run, question_summary=run, owner="one", labels=(), created_at=recent
        )
        catalog.project(run, _summary(recent))
    cursor = catalog.list(ResearchListQuery(limit=1)).next_cursor
    assert cursor
    with pytest.raises(CatalogCursorError, match="query mismatch"):
        catalog.list(ResearchListQuery(limit=1, text="different", cursor=cursor))
    catalog.project("r2", _summary("2026-08-13T13:00:00Z", 2))
    with pytest.raises(CatalogFenceChanged):
        catalog.list(ResearchListQuery(limit=1, cursor=cursor))


def test_action_items_only_use_authoritative_actions_for_principal() -> None:
    actions = aggregate_action_items(
        (
            RunActions(
                "r1",
                4,
                2,
                (
                    {
                        "command_type": "SUBMIT_CLAIM",
                        "principal_subject_id": "math:one",
                        "target_ids": ["c1"],
                        "required_inputs": ["proof"],
                        "blocked_by": [],
                    },
                    {
                        "command_type": "FINALIZE_RESEARCH",
                        "principal_subject_id": "math:two",
                        "target_ids": ["root"],
                    },
                ),
            ),
        ),
        "math:one",
    )
    assert len(actions) == 1
    assert actions[0]["command_type"] == "SUBMIT_CLAIM"
    assert actions[0]["research_revision"] == 4
