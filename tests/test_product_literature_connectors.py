from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from rk.cas import ContentAddressedStore
from rk.product.artifact_read import ArtifactReadService, ExactArtifactRef
from rk.product.compute import (
    AuthorityCeiling,
    ResourceRequest,
    ToolAvailability,
    ToolFunctionSpec,
    prepare_tool_invocation,
)
from rk.product.jobs import JobStore, RetrySafety
from rk.product.literature_connectors import (
    ArxivConnector,
    ConnectorStatus,
    CrossrefConnector,
    HttpResponse,
    MatlasConnector,
    OpenAlexConnector,
    TransportFailure,
    UrllibTransport,
)
from rk.product.operations import OperationStore
from rk.product.source_snapshots import SourceSnapshotStore
from rk.product.tool_runs import ToolCatalogStore, ToolRunStore
from rk.product_migrations import ProductMigrationAssembler, ProductMigrationRegistry
from rk.wire import canonical_json_bytes

NOW = "2026-08-13T00:00:00Z"


class Ids:
    def __init__(self) -> None:
        self.value = 0

    def new(self) -> str:
        self.value += 1
        return f"artifact-{self.value}"


class RegistryPublisher:
    def __init__(self, root: Path) -> None:
        self.records: dict[str, dict[str, object]] = {}
        self.cas = ContentAddressedStore(
            root,
            max_bytes=50 * 1024 * 1024,
            inbox_roots=(),
            orphan_grace_seconds=60,
            id_generator=Ids(),
        )

    def publish(self, *, data: bytes, logical_name: str, media_type: str) -> ExactArtifactRef:
        committed = self.cas.commit(
            self.cas.stage_bytes(data, media_type=media_type, source_name=logical_name),
            now=datetime(2026, 8, 13, tzinfo=UTC),
        )
        self.records[committed.artifact_id] = committed.to_record()
        return ExactArtifactRef(
            committed.artifact_id,
            committed.sha256,
            committed.byte_count,
            committed.media_type,
        )

    def get_artifact(self, artifact_id: str) -> dict[str, object] | None:
        return self.records.get(artifact_id)


class ArgumentReader:
    def read_json(self, artifact_ref: ExactArtifactRef) -> dict[str, str]:
        return {"query": "graph theory"}


class StaticTransport:
    def __init__(self, response: HttpResponse | TransportFailure) -> None:
        self.response = response
        self.calls = 0

    def request(self, **kwargs: Any) -> HttpResponse:
        self.calls += 1
        if isinstance(self.response, TransportFailure):
            raise self.response
        return self.response


class NoNetwork:
    def request(self, **kwargs: Any) -> HttpResponse:
        raise AssertionError("snapshot replay attempted a network request")


def migrated_db(tmp_path: Path) -> Path:
    db = tmp_path / "product.sqlite"
    import sqlite3

    with sqlite3.connect(db) as connection:
        ProductMigrationAssembler(ProductMigrationRegistry(Path("schema_fragments"))).apply(
            connection
        )
    return db


def declaration() -> ToolFunctionSpec:
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["query"],
        "properties": {"query": {"type": "string"}},
    }
    return ToolFunctionSpec(
        tool_id="literature",
        tool_version="1",
        function_name="query",
        provider="rk",
        build_version="build-1",
        profile_id="literature",
        function_schema=schema,
        function_schema_digest=hashlib.sha256(canonical_json_bytes(schema)).hexdigest(),
        availability=ToolAvailability.AVAILABLE,
        authority_ceiling=AuthorityCeiling.NO_FACT_GRAPH_WRITE,
    )


def add_run(db: Path, runs: ToolRunStore, jobs: JobStore, number: int) -> tuple[str, str]:
    request_id = f"request-{number}"
    job_id = f"job-{number}"
    receipt_id = f"receipt-{number}"
    tool_run_id = f"tool-run-{number}"
    attempt_id = f"attempt-{number}"
    body = {
        "schema_version": "rk.product.receipt.v1",
        "request_id": request_id,
        "scope": {
            "kind": "RUN",
            "run_id": "run-1",
            "expected_revision": 3,
            "expected_contract_version": 1,
        },
        "updated_at": NOW,
        "state": "PENDING",
        "job_id": job_id,
    }
    OperationStore(db, iter([receipt_id]).__next__).reserve(
        scope_key="RUN:run-1",
        request_id=request_id,
        request_digest=hashlib.sha256(request_id.encode()).hexdigest(),
        pending_receipt=body,
        now=NOW,
    )
    jobs.enqueue(
        job_id=job_id,
        receipt_id=receipt_id,
        scope_kind="RUN",
        run_id="run-1",
        deployment_id=None,
        kind="RUN_TOOL",
        requested_by="literature-reviewer",
        request_id=request_id,
        retry_safety=RetrySafety.IDEMPOTENT,
        idempotency_key=None,
        now=NOW,
    )
    ref = ExactArtifactRef(f"arguments-{number}", "a" * 64, 2, "application/json")
    prepared = prepare_tool_invocation(
        spec=declaration(),
        arguments_artifact=ref,
        input_artifact_ids=(),
        resources=ResourceRequest(500, 64 * 1024 * 1024, 30_000),
        authority_ceiling=AuthorityCeiling.NO_FACT_GRAPH_WRITE,
        artifacts=ArgumentReader(),
    )
    runs.create(
        tool_run_id=tool_run_id,
        run_id="run-1",
        research_revision=3,
        contract_version=1,
        request_id=request_id,
        requested_by="literature-reviewer",
        invocation=prepared,
        attempt_id=attempt_id,
        job_id=job_id,
        now=NOW,
    )
    return tool_run_id, attempt_id


def stores(tmp_path: Path):
    db = migrated_db(tmp_path)
    ToolCatalogStore(db).register(declaration(), now=NOW)
    jobs = JobStore(db, iter(["unused"]).__next__)
    runs = ToolRunStore(db, jobs)
    publisher = RegistryPublisher(tmp_path / "cas")
    reader = ArtifactReadService(metadata=publisher, cas_root=tmp_path / "cas")
    snapshots = SourceSnapshotStore(
        db_path=db,
        artifacts=reader,
        publisher=publisher,
        tool_runs=runs,
    )
    return db, jobs, runs, publisher, snapshots


def test_matlas_no_hit_schema_drift_and_timeout_are_explicit_receipts() -> None:
    no_hit = MatlasConnector(
        StaticTransport(HttpResponse(200, (("Content-Type", "application/json"),), b"[]"))
    ).query({"query": "impossible query", "num_results": 2}, timeout_seconds=1)
    assert no_hit.status == ConnectorStatus.NO_HIT
    assert no_hit.normalized == {
        "results": [],
        "candidate_kind": "THEOREM_CANDIDATE",
    }
    assert "novel" not in json.dumps(no_hit.normalized).lower()

    drift = MatlasConnector(StaticTransport(HttpResponse(200, (), b'{"results":[]}'))).query(
        {"query": "x", "num_results": 2}, timeout_seconds=1
    )
    assert drift.status == ConnectorStatus.SCHEMA_DRIFT
    assert drift.error_code == "SCHEMA_DRIFT"

    timed_out = MatlasConnector(
        StaticTransport(TransportFailure(ConnectorStatus.TIMEOUT, "deadline"))
    ).query({"query": "x", "num_results": 2}, timeout_seconds=1)
    assert timed_out.status == ConnectorStatus.TIMEOUT
    assert timed_out.raw_kind == "TRANSPORT_RECEIPT"
    assert json.loads(timed_out.raw_body)["error_code"] == "TIMEOUT"


@pytest.mark.parametrize(
    ("response", "expected_status", "expected_kind"),
    [
        (HttpResponse(200, (), b"[]"), "NO_HIT", "WIRE_RESPONSE"),
        (HttpResponse(200, (), b'{"results":[]}'), "SCHEMA_DRIFT", "WIRE_RESPONSE"),
        (
            TransportFailure(ConnectorStatus.TIMEOUT, "deadline"),
            "TIMEOUT",
            "TRANSPORT_RECEIPT",
        ),
    ],
)
def test_no_hit_drift_and_timeout_are_persisted_with_raw_receipts(
    tmp_path: Path,
    response: HttpResponse | TransportFailure,
    expected_status: str,
    expected_kind: str,
) -> None:
    db, jobs, runs, publisher, snapshots = stores(tmp_path)
    tool_run_id, attempt_id = add_run(db, runs, jobs, 1)
    snapshot = snapshots.capture_live(
        snapshot_id="snapshot-1",
        tool_run_id=tool_run_id,
        attempt_id=attempt_id,
        connector=MatlasConnector(StaticTransport(response)),
        request={"query": "candidate", "num_results": 2},
        queried_at=NOW,
        timeout_seconds=1,
    )
    assert snapshot.result_status == expected_status
    assert snapshot.raw_kind == expected_kind
    assert snapshot.raw_response.artifact_id in publisher.records
    assert snapshot.establishes_novelty is False


def test_snapshot_replay_is_offline_and_preserves_raw_and_normalized(tmp_path: Path) -> None:
    db, jobs, runs, _, snapshots = stores(tmp_path)
    first_run, first_attempt = add_run(db, runs, jobs, 1)
    response = HttpResponse(
        200,
        (("Content-Type", "application/json"),),
        json.dumps(
            [
                {
                    "title": "T",
                    "theorem": "Statement",
                    "arxiv_id": "2106.14834",
                    "theorem_id": "thm.1",
                }
            ]
        ).encode(),
    )
    live = snapshots.capture_live(
        snapshot_id="snapshot-live",
        tool_run_id=first_run,
        attempt_id=first_attempt,
        connector=MatlasConnector(StaticTransport(response)),
        request={"query": "rank decomposition", "num_results": 1},
        queried_at=NOW,
        timeout_seconds=1,
    )
    second_run, second_attempt = add_run(db, runs, jobs, 2)
    replayed = snapshots.replay(
        source_snapshot_id=live.snapshot_id,
        snapshot_id="snapshot-replay",
        tool_run_id=second_run,
        attempt_id=second_attempt,
        replayed_at="2026-08-13T01:00:00Z",
    )
    assert live.mode == "LIVE_QUERY"
    assert replayed.mode == "REPLAYED_SNAPSHOT"
    assert replayed.parent_snapshot_id == live.snapshot_id
    assert replayed.raw_response == live.raw_response
    assert replayed.normalized == live.normalized
    assert replayed.queried_at == live.queried_at
    assert replayed.establishes_novelty is False


def test_openalex_crossref_and_arxiv_strict_fixture_projection() -> None:
    openalex = OpenAlexConnector(
        StaticTransport(
            HttpResponse(
                200,
                (),
                json.dumps(
                    {
                        "meta": {"count": 1},
                        "results": [
                            {
                                "id": "https://openalex.org/W1",
                                "doi": "https://doi.org/10.1/x",
                                "title": "A work",
                                "publication_year": 2024,
                                "authorships": [{"author": {"display_name": "Ada Author"}}],
                                "cited_by_count": 3,
                            }
                        ],
                    }
                ).encode(),
            )
        )
    ).query({"query": "graph", "per_page": 1}, timeout_seconds=1)
    assert openalex.status == ConnectorStatus.SUCCESS
    assert openalex.normalized["results"][0]["authors"] == ["Ada Author"]

    crossref = CrossrefConnector(
        StaticTransport(
            HttpResponse(
                200,
                (),
                json.dumps(
                    {
                        "message": {
                            "total-results": 1,
                            "items": [
                                {
                                    "DOI": "10.1/x",
                                    "title": ["A work"],
                                    "author": [{"given": "Ada", "family": "Author"}],
                                    "type": "journal-article",
                                    "published": {"date-parts": [[2024]]},
                                }
                            ],
                        }
                    }
                ).encode(),
            )
        )
    ).query({"query": "graph", "rows": 1}, timeout_seconds=1)
    assert crossref.status == ConnectorStatus.SUCCESS
    assert crossref.normalized["results"][0]["doi"] == "10.1/x"

    atom = b"""<?xml version="1.0"?>
<feed xmlns="http://www.w3.org/2005/Atom"><entry>
<id>http://arxiv.org/abs/2106.14834v1</id><updated>2021-06-29T00:00:00Z</updated>
<published>2021-06-29T00:00:00Z</published><title>LoRA</title>
<summary>Low rank adaptation.</summary><author><name>Edward Hu</name></author>
</entry></feed>"""
    arxiv = ArxivConnector(StaticTransport(HttpResponse(200, (), atom))).query(
        {"kind": "CONTEXT", "arxiv_id": "2106.14834", "version": 1},
        timeout_seconds=1,
    )
    assert arxiv.status == ConnectorStatus.SUCCESS
    assert arxiv.source_visible_version == "2106.14834v1"


@pytest.mark.parametrize(
    ("connector", "request_value"),
    [
        (MatlasConnector(UrllibTransport()), {"query": "nef canonical bundle", "num_results": 2}),
        (OpenAlexConnector(UrllibTransport()), {"query": "graph theory", "per_page": 1}),
        (CrossrefConnector(UrllibTransport()), {"query": "graph theory", "rows": 1}),
        (
            ArxivConnector(UrllibTransport()),
            {"kind": "SEARCH", "query": "graph theory", "max_results": 1},
        ),
        (
            ArxivConnector(UrllibTransport()),
            {"kind": "CONTEXT", "arxiv_id": "2106.14834", "version": 1},
        ),
        (
            ArxivConnector(UrllibTransport()),
            {"kind": "DOCUMENT", "arxiv_id": "2106.14834", "version": 1},
        ),
    ],
)
def test_current_endpoint_live_call_is_losslessly_captured_in_real_cas(
    tmp_path: Path, connector: Any, request_value: dict[str, object]
) -> None:
    db, jobs, runs, publisher, snapshots = stores(tmp_path)
    tool_run_id, attempt_id = add_run(db, runs, jobs, 1)
    snapshot = snapshots.capture_live(
        snapshot_id="snapshot-live",
        tool_run_id=tool_run_id,
        attempt_id=attempt_id,
        connector=connector,
        request=request_value,
        queried_at=NOW,
        timeout_seconds=30,
    )
    assert snapshot.mode == "LIVE_QUERY"
    assert snapshot.result_status in {"SUCCESS", "NO_HIT", "HTTP_ERROR"}
    if snapshot.result_status == "HTTP_ERROR":
        assert snapshot.error_code == "HTTP_ERROR"
        assert snapshot.error_detail
    assert snapshot.raw_kind == "WIRE_RESPONSE"
    assert snapshot.raw_response.artifact_id in publisher.records
    assert (
        snapshot.raw_response.sha256
        == publisher.records[snapshot.raw_response.artifact_id]["sha256"]
    )
    assert snapshot.establishes_novelty is False


def test_matlas_attribution_is_preserved() -> None:
    attribution = Path("src/rk/product/literature_connectors/ATTRIBUTION.md").read_text(
        encoding="utf-8"
    )
    assert "FrenzyMath/Danus/danus/integrations/matlas.py" in attribution
    assert "Apache-2.0" in attribution
