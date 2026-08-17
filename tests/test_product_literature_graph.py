from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pytest

from rk.product.literature_connectors import (
    ArxivConnector,
    MatlasConnector,
    OpenAlexConnector,
    UrllibTransport,
)
from rk.product.literature_graph import LiteratureGraphError, LiteratureGraphStore
from rk.product.novelty import NoveltyBoundaryError, NoveltyStore
from rk.product.theorem_applicability import TheoremApplicabilityStore

NOW = "2026-08-14T00:00:00Z"


@dataclass(frozen=True, slots=True)
class SnapshotFixture:
    snapshot_id: str
    connector_version: str


def fixture_database(tmp_path: Path) -> Path:
    db = tmp_path / "literature-graph.sqlite"
    with sqlite3.connect(db, isolation_level=None) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.executescript(
            "CREATE TABLE product_tool_runs(tool_run_id TEXT PRIMARY KEY) STRICT;"
            "CREATE TABLE product_tool_attempts(attempt_id TEXT PRIMARY KEY) STRICT;"
            "CREATE TABLE product_materials("
            "material_id TEXT PRIMARY KEY,run_id TEXT NOT NULL,material_kind TEXT NOT NULL,"
            "original_artifact_id TEXT NOT NULL,original_artifact_sha256 TEXT NOT NULL,"
            "original_artifact_byte_count INTEGER NOT NULL,"
            "original_artifact_media_type TEXT NOT NULL,created_at TEXT NOT NULL) STRICT;"
        )
        connection.executescript(
            Path("schema_fragments/B08a/literature.sql").read_text(encoding="utf-8")
        )
        connection.executescript(
            Path("schema_fragments/B08b/literature_graph.sql").read_text(encoding="utf-8")
        )
        for number in range(1, 7):
            connection.execute("INSERT INTO product_tool_runs VALUES(?)", (f"run-{number}",))
            connection.execute(
                "INSERT INTO product_tool_attempts VALUES(?)", (f"attempt-{number}",)
            )
    return db


def insert_snapshot(
    db: Path,
    *,
    snapshot_id: str,
    number: int,
    connector: str,
    status: str,
    normalized: dict[str, object],
    mode: str = "LIVE_QUERY",
    parent_snapshot_id: str | None = None,
) -> SnapshotFixture:
    connector_version = f"{connector.lower()}-connector-v1"
    request = {"query": "finite graph property"}
    request_json = json.dumps(request, sort_keys=True, separators=(",", ":"))
    with sqlite3.connect(db) as connection:
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(
            "INSERT INTO product_source_snapshots VALUES("
            "?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                snapshot_id,
                f"run-{number}",
                f"attempt-{number}",
                connector,
                connector_version,
                mode,
                parent_snapshot_id,
                f"https://source.example/{connector.lower()}",
                NOW,
                request_json,
                hashlib.sha256(request_json.encode()).hexdigest(),
                200,
                "WIRE_RESPONSE",
                f"artifact-{snapshot_id}",
                hashlib.sha256(snapshot_id.encode()).hexdigest(),
                100,
                "application/json",
                "v1",
                '{"complete":false,"returned":1}',
                json.dumps(normalized, sort_keys=True, separators=(",", ":")),
                status,
                None,
                None,
                NOW,
            ),
        )
    return SnapshotFixture(snapshot_id, connector_version)


def fixture_snapshots(tmp_path: Path):
    db = fixture_database(tmp_path)
    matlas = insert_snapshot(
        db,
        snapshot_id="matlas-live",
        number=1,
        connector="MATLAS",
        status="SUCCESS",
        normalized={
            "candidate_kind": "THEOREM_CANDIDATE",
            "results": [
                {
                    "title": "Exact theorem",
                    "theorem": "For every finite G, P(G).",
                    "arxiv_id": "2106.14834",
                    "theorem_id": "thm.7",
                }
            ],
        },
    )
    openalex = insert_snapshot(
        db,
        snapshot_id="openalex-live",
        number=2,
        connector="OPENALEX",
        status="SUCCESS",
        normalized={"results": [{"id": "W1", "title": "Exact theorem paper"}]},
    )
    arxiv = insert_snapshot(
        db,
        snapshot_id="arxiv-context",
        number=3,
        connector="ARXIV",
        status="SUCCESS",
        normalized={"arxiv_id": "2106.14834v1", "context": "Theorem 7"},
    )
    replay = insert_snapshot(
        db,
        snapshot_id="matlas-replay",
        number=4,
        connector="MATLAS",
        status="SUCCESS",
        normalized={"candidate_kind": "THEOREM_CANDIDATE", "results": []},
        mode="REPLAYED_SNAPSHOT",
        parent_snapshot_id=matlas.snapshot_id,
    )
    no_hit = insert_snapshot(
        db,
        snapshot_id="matlas-no-hit",
        number=5,
        connector="MATLAS",
        status="NO_HIT",
        normalized={"candidate_kind": "THEOREM_CANDIDATE", "results": []},
    )
    return db, matlas, openalex, arxiv, replay, no_hit


def test_multisource_graph_import_replay_applicability_and_novelty_boundary(
    tmp_path: Path,
) -> None:
    db, matlas, openalex, arxiv, replay, no_hit = fixture_snapshots(tmp_path)
    graph = LiteratureGraphStore(db)
    paper = graph.add_entity(
        entity_id="paper-1",
        entity_kind="PAPER",
        canonical_key="arxiv:2106.14834v1",
        title="Exact theorem paper",
        arxiv_id="2106.14834",
        arxiv_version="v1",
        created_at=NOW,
    )
    duplicate = graph.add_entity(
        entity_id="paper-duplicate",
        entity_kind="PAPER",
        canonical_key="arxiv:2106.14834v1",
        title="Duplicate",
        created_at=NOW,
    )
    assert duplicate.entity_id == paper.entity_id
    theorem = graph.add_entity(
        entity_id="theorem-1",
        entity_kind="THEOREM",
        canonical_key="arxiv:2106.14834v1#thm.7",
        title="Exact theorem",
        statement="For every finite G, P(G).",
        arxiv_id="2106.14834",
        arxiv_version="v1",
        theorem_id="thm.7",
        created_at=NOW,
    )
    author = graph.add_entity(
        entity_id="author-1",
        entity_kind="AUTHOR",
        canonical_key="name:ada-author",
        title="Ada Author",
        created_at=NOW,
    )
    graph.add_source(
        entity_id=theorem.entity_id,
        source_kind="MATLAS",
        snapshot_id=matlas.snapshot_id,
        source_record_key="thm.7",
        source_version=matlas.connector_version,
        source_anchor={"result_index": 0},
        observed_at=NOW,
    )
    graph.add_source(
        entity_id=paper.entity_id,
        source_kind="OPENALEX",
        snapshot_id=openalex.snapshot_id,
        source_record_key="W1",
        source_anchor={"result_index": 0},
        observed_at=NOW,
    )
    graph.add_edge(
        edge_id="edge-theorem",
        from_entity_id=paper.entity_id,
        to_entity_id=theorem.entity_id,
        edge_kind="CONTAINS_THEOREM",
        source_kind="MATLAS",
        snapshot_id=matlas.snapshot_id,
        source_anchor={"theorem_id": "thm.7"},
        created_at=NOW,
    )
    graph.add_edge(
        edge_id="edge-author",
        from_entity_id=author.entity_id,
        to_entity_id=paper.entity_id,
        edge_kind="AUTHORED",
        source_kind="OPENALEX",
        snapshot_id=openalex.snapshot_id,
        source_anchor={"author_index": 0},
        created_at=NOW,
    )
    with pytest.raises(LiteratureGraphError, match="thin client"):
        graph.add_edge(
            edge_id="false-matlas-author",
            from_entity_id=author.entity_id,
            to_entity_id=paper.entity_id,
            edge_kind="AUTHORED",
            source_kind="MATLAS",
            snapshot_id=matlas.snapshot_id,
            source_anchor={},
            created_at=NOW,
        )
    context = graph.add_context(
        context_id="context-1",
        theorem_entity_id=theorem.entity_id,
        arxiv_snapshot_id=arxiv.snapshot_id,
        arxiv_id="2106.14834",
        arxiv_version="v1",
        anchor={"section": "Theorem 7", "offset": 81},
        excerpt="For every finite G, P(G).",
        created_at=NOW,
    )
    with sqlite3.connect(db) as connection:
        connection.execute(
            "INSERT INTO product_materials VALUES(?,?,?,?,?,?,?,?)",
            ("import-1", "run-1", "TEXT", "artifact-import", "a" * 64, 20, "text/plain", NOW),
        )
    graph.add_source(
        entity_id=theorem.entity_id,
        source_kind="HUMAN_IMPORT",
        import_material_id="import-1",
        source_record_key="bibliography-row-1",
        source_anchor={"line": 1},
        observed_at=NOW,
    )
    graph.link(
        link_id="claim-link",
        entity_id=theorem.entity_id,
        run_id="run-1",
        link_kind="CLAIM",
        claim_id="claim-1",
        created_by="literature-reviewer",
        created_at=NOW,
    )
    TheoremApplicabilityStore(db).review(
        applicability_id="app-1",
        context_id=context.context_id,
        target_link_id="claim-link",
        quantifiers={"source": "forall finite G", "target": "forall finite G"},
        assumptions={"source": ["finite"], "target": ["finite"], "missing": []},
        symbols={"G": "target_graph", "P": "target_property"},
        verdict="APPLICABLE",
        reviewed_by="reviewer-a",
        reviewed_at=NOW,
    )
    novelty = NoveltyStore(db)
    novelty.compare(
        comparison_id="comparison-1",
        run_id="run-1",
        target_link_id="claim-link",
        entity_id=paper.entity_id,
        overlap={"objects": ["finite graphs"]},
        difference={"target_strength": "strictly stronger"},
        assessed_by="reviewer-a",
        assessed_at=NOW,
    )
    result = novelty.review(
        novelty_review_id="novelty-1",
        run_id="run-1",
        target_link_id="claim-link",
        boundary={
            "snapshot_ids": [replay.snapshot_id, openalex.snapshot_id],
            "queries": ["finite graph property"],
            "limitations": ["Matlas thin client", "two indexed sources plus import"],
        },
        conclusion="NOVEL_WITHIN_REVIEWED_BOUNDARY",
        reviewed_by="reviewer-b",
        reviewed_at=NOW,
    )
    assert result.conclusion == "NOVEL_WITHIN_REVIEWED_BOUNDARY"
    with pytest.raises(NoveltyBoundaryError, match="NO_HIT"):
        novelty.review(
            novelty_review_id="false-no-hit",
            run_id="run-1",
            target_link_id="claim-link",
            boundary={
                "snapshot_ids": [no_hit.snapshot_id, openalex.snapshot_id],
                "queries": ["absent"],
                "limitations": ["limited"],
            },
            conclusion="NOVEL_WITHIN_REVIEWED_BOUNDARY",
            reviewed_by="reviewer-c",
            reviewed_at=NOW,
        )


def test_current_endpoints_return_sources_never_novelty_verdicts() -> None:
    calls = (
        MatlasConnector(UrllibTransport()).query(
            {"query": "nef canonical bundle", "num_results": 2}, timeout_seconds=30
        ),
        OpenAlexConnector(UrllibTransport()).query(
            {"query": "graph theory", "per_page": 1}, timeout_seconds=30
        ),
        ArxivConnector(UrllibTransport()).query(
            {"kind": "CONTEXT", "arxiv_id": "2106.14834", "version": 1},
            timeout_seconds=30,
        ),
    )
    allowed = {"SUCCESS", "NO_HIT", "HTTP_ERROR", "TIMEOUT", "NETWORK_ERROR", "SCHEMA_DRIFT"}
    assert all(str(call.status) in allowed for call in calls)
    assert all("novel" not in json.dumps(call.normalized).lower() for call in calls)
