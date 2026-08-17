from __future__ import annotations

import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from rk.cas import ContentAddressedStore
from rk.product.api import (
    ProductCommand,
    ProductDecision,
    ProductReceipt,
    ProductSession,
)
from rk.product.artifact_read import ArtifactReadService, ExactArtifactRef
from rk.product.arxiv_batch import ArxivBatchError, ArxivBatchPipeline
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
    HttpResponse,
    MatlasConnector,
    TransportFailure,
    UrllibTransport,
)
from rk.product.operations import OperationStore
from rk.product.problem_pool import ProblemPoolError, ProblemPoolStore
from rk.product.source_snapshots import SourceSnapshotStore
from rk.product.tool_runs import ToolCatalogStore, ToolRunStore
from rk.product_migrations import ProductMigrationAssembler, ProductMigrationRegistry
from rk.wire import canonical_json_bytes

NOW = "2026-08-14T00:00:00Z"


class Ids:
    def __init__(self) -> None:
        self.value = 0

    def new(self) -> str:
        self.value += 1
        return f"artifact-{self.value}"


class Publisher:
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
            now=datetime(2026, 8, 14, tzinfo=UTC),
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
        return {"query": artifact_ref.artifact_id}


class StaticTransport:
    def __init__(self, response: HttpResponse | TransportFailure) -> None:
        self.response = response

    def request(self, **kwargs: Any) -> HttpResponse:
        if isinstance(self.response, TransportFailure):
            raise self.response
        return self.response


class FormalCommands:
    def __init__(self) -> None:
        self.receipts: dict[str, ProductReceipt] = {}
        self.calls: list[ProductCommand] = []

    def command(self, session: ProductSession, request: ProductCommand) -> ProductReceipt:
        assert session.principal_subject_id == "researcher-1"
        self.calls.append(request)
        prior = self.receipts.get(request.request_id)
        if prior is not None:
            return prior
        if request.command_type == "BATCH_CREATE_RESEARCH":
            receipt = ProductReceipt(
                f"receipt-{request.request_id}",
                1,
                request.request_id,
                request.scope,
                "PENDING",
                NOW,
                job_id=f"job-{request.request_id}",
            )
        else:
            assert request.command_type == "CREATE_RESEARCH"
            receipt = ProductReceipt(
                f"receipt-{request.request_id}",
                1,
                request.request_id,
                request.scope,
                "DECIDED",
                NOW,
                decision=ProductDecision(
                    True,
                    10,
                    11,
                    1,
                    20,
                    created_run_id=f"run-{request.request_id}",
                ),
                decided_at=NOW,
            )
        self.receipts[request.request_id] = receipt
        return receipt


def _declaration() -> ToolFunctionSpec:
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


def _db(tmp_path: Path) -> Path:
    result = tmp_path / "product.sqlite"
    with sqlite3.connect(result) as connection:
        ProductMigrationAssembler(ProductMigrationRegistry(Path("schema_fragments"))).apply(
            connection
        )
    return result


def _add_run(db: Path, runs: ToolRunStore, jobs: JobStore, number: int) -> tuple[str, str]:
    request_id = f"request-{number}"
    job_id = f"job-{number}"
    receipt_id = f"receipt-{number}"
    body = {
        "schema_version": "rk.product.receipt.v1",
        "request_id": request_id,
        "scope": {
            "kind": "RUN",
            "run_id": "run-source",
            "expected_revision": 1,
            "expected_contract_version": 1,
        },
        "updated_at": NOW,
        "state": "PENDING",
        "job_id": job_id,
    }
    OperationStore(db, iter([receipt_id]).__next__).reserve(
        scope_key="RUN:run-source",
        request_id=request_id,
        request_digest=hashlib.sha256(request_id.encode()).hexdigest(),
        pending_receipt=body,
        now=NOW,
    )
    jobs.enqueue(
        job_id=job_id,
        receipt_id=receipt_id,
        scope_kind="RUN",
        run_id="run-source",
        deployment_id=None,
        kind="RUN_TOOL",
        requested_by="literature-reviewer",
        request_id=request_id,
        retry_safety=RetrySafety.IDEMPOTENT,
        idempotency_key=None,
        now=NOW,
    )
    ref = ExactArtifactRef(f"arguments-{number}", "a" * 64, 2, "application/json")
    invocation = prepare_tool_invocation(
        spec=_declaration(),
        arguments_artifact=ref,
        input_artifact_ids=(),
        resources=ResourceRequest(500, 64 * 1024 * 1024, 30_000),
        authority_ceiling=AuthorityCeiling.NO_FACT_GRAPH_WRITE,
        artifacts=ArgumentReader(),
    )
    runs.create(
        tool_run_id=f"tool-run-{number}",
        run_id="run-source",
        research_revision=1,
        contract_version=1,
        request_id=request_id,
        requested_by="literature-reviewer",
        invocation=invocation,
        attempt_id=f"attempt-{number}",
        job_id=job_id,
        now=NOW,
    )
    return f"tool-run-{number}", f"attempt-{number}"


def _stores(tmp_path: Path):
    db = _db(tmp_path)
    ToolCatalogStore(db).register(_declaration(), now=NOW)
    jobs = JobStore(db, iter(["unused"]).__next__)
    runs = ToolRunStore(db, jobs)
    publisher = Publisher(tmp_path / "cas")
    reader = ArtifactReadService(metadata=publisher, cas_root=tmp_path / "cas")
    snapshots = SourceSnapshotStore(
        db_path=db,
        artifacts=reader,
        publisher=publisher,
        tool_runs=runs,
    )
    return db, jobs, runs, publisher, reader, snapshots


ATOM = b"""<?xml version="1.0" encoding="UTF-8"?>
<feed xmlns="http://www.w3.org/2005/Atom"
      xmlns:arxiv="http://arxiv.org/schemas/atom">
  <entry>
    <id>http://arxiv.org/abs/2501.00001v1</id>
    <updated>2025-01-02T00:00:00Z</updated>
    <published>2025-01-01T00:00:00Z</published>
    <title>A spectral expansion conjecture, first version</title>
    <summary>We pose the conjecture that every bounded-degree family with uniform
    local expansion has a logarithmic global mixing bound.</summary>
    <category term="math.CO"/>
  </entry>
  <entry>
    <id>http://arxiv.org/abs/2501.00001v2</id>
    <updated>2025-01-04T00:00:00Z</updated>
    <published>2025-01-01T00:00:00Z</published>
    <title>A spectral expansion conjecture, corrected version</title>
    <summary>We pose the conjecture that every bounded-degree family with uniform
    local expansion has a logarithmic global mixing bound under connectedness.</summary>
    <category term="math.CO"/>
  </entry>
  <entry>
    <id>http://arxiv.org/abs/2501.00002v1</id>
    <updated>2025-01-03T00:00:00Z</updated>
    <published>2025-01-02T00:00:00Z</published>
    <title>A withdrawn extremal problem</title>
    <summary>The problem asks whether the extremal density is attained.</summary>
    <arxiv:comment>Withdrawn by the authors after an error.</arxiv:comment>
    <category term="math.CO"/>
  </entry>
  <entry>
    <id>http://arxiv.org/abs/2501.00003v1</id>
    <updated>2025-01-04T00:00:00Z</updated>
    <published>2025-01-03T00:00:00Z</published>
    <title>A rigidity question for measurable actions</title>
    <summary>The question is whether every ergodic action satisfying property P
    admits a finite rigidity witness.</summary>
    <category term="math.DS"/>
  </entry>
  <entry>
    <id>http://arxiv.org/abs/2501.00004v1</id>
    <updated>2025-01-05T00:00:00Z</updated>
    <published>2025-01-04T00:00:00Z</published>
    <title>A theorem without an open marker</title>
    <summary>We prove a complete classification in the compact case.</summary>
    <category term="math.DS"/>
  </entry>
  <entry>
    <id>http://arxiv.org/abs/2501.00005v1</id>
    <updated>2025-01-06T00:00:00Z</updated>
    <published>2025-01-05T00:00:00Z</published>
    <title>A rigidity question for measurable actions</title>
    <summary>The question is whether every ergodic action satisfying property P
    admits a finite rigidity witness.</summary>
    <category term="math.DS"/>
  </entry>
</feed>"""


def test_frozen_problem_pool_complete_denominator_and_formal_batch_restart(
    tmp_path: Path,
) -> None:
    db, jobs, runs, publisher, reader, snapshots = _stores(tmp_path)
    fixture_run, fixture_attempt = _add_run(db, runs, jobs, 1)
    fixture = snapshots.capture_live(
        snapshot_id="snapshot-fixture",
        tool_run_id=fixture_run,
        attempt_id=fixture_attempt,
        connector=ArxivConnector(StaticTransport(HttpResponse(200, (), ATOM))),
        request={"kind": "SEARCH", "query": "open problems", "max_results": 6},
        queried_at=NOW,
        timeout_seconds=1,
    )
    failed_run, failed_attempt = _add_run(db, runs, jobs, 2)
    failed = snapshots.capture_live(
        snapshot_id="snapshot-timeout",
        tool_run_id=failed_run,
        attempt_id=failed_attempt,
        connector=ArxivConnector(
            StaticTransport(TransportFailure(ConnectorStatus.TIMEOUT, "deadline"))
        ),
        request={"kind": "SEARCH", "query": "open problems", "max_results": 6},
        queried_at=NOW,
        timeout_seconds=1,
    )
    blocked_run, blocked_attempt = _add_run(db, runs, jobs, 3)
    blocked = snapshots.capture_live(
        snapshot_id="snapshot-matlas",
        tool_run_id=blocked_run,
        attempt_id=blocked_attempt,
        connector=MatlasConnector(StaticTransport(HttpResponse(200, (), b"[]"))),
        request={"query": "open problems", "num_results": 1},
        queried_at=NOW,
        timeout_seconds=1,
    )

    pools = ProblemPoolStore(db)
    pools.create(
        problem_pool_id="pool-2025-math",
        deployment_id="deployment-1",
        date_from="2025-01-01",
        date_to="2025-01-31",
        subjects=("math.CO", "math.DS"),
        version_rule="LATEST_VISIBLE",
        withdrawal_rule="EXCLUDE_WITHDRAWN",
        exclusion_rules=("REQUIRE_EXPLICIT_MARKER",),
        now=NOW,
    )
    commands = FormalCommands()
    pipeline = ArxivBatchPipeline(
        db_path=db,
        pools=pools,
        snapshots=snapshots,
        artifacts=reader,
        commands=commands,
    )
    pipeline.ingest_snapshots(
        problem_pool_id="pool-2025-math",
        snapshot_ids=(fixture.snapshot_id, failed.snapshot_id, blocked.snapshot_id),
        now=NOW,
    )
    pipeline.ingest_snapshots(
        problem_pool_id="pool-2025-math",
        snapshot_ids=(fixture.snapshot_id, failed.snapshot_id, blocked.snapshot_id),
        now="2026-08-14T00:30:00Z",
    )
    denominator = pools.denominator("pool-2025-math")
    assert denominator.total == 8
    assert (
        denominator.included,
        denominator.excluded,
        denominator.failed,
        denominator.blocked,
    ) == (
        2,
        4,
        1,
        1,
    )
    assert {
        "SUPERSEDED_VERSION",
        "WITHDRAWN",
        "NO_EXPLICIT_OPEN_MARKER",
        "DUPLICATE_STATEMENT",
        "TIMEOUT",
        "NON_ARXIV_SNAPSHOT",
    } <= {reason for reason, _ in denominator.reasons}

    candidates = pools.candidates("pool-2025-math")
    assert len(candidates) == 2
    with pytest.raises(ProblemPoolError, match="human"):
        pools.audit_candidate(
            candidates[0].problem_candidate_id,
            decision="INCLUDE",
            normalized_statement="A model-selected statement",
            definitions=("A graph family is a sequence of finite graphs.",),
            quantifiers=("For every family.",),
            hypotheses=("The degrees are uniformly bounded.",),
            audit_note="model output",
            audited_by="model",
            actor_kind="MODEL",
            now=NOW,
        )
    normalized = {
        "2501.00001": (
            "For every connected bounded-degree graph family with uniform local expansion, "
            "the global mixing time is logarithmic in graph size.",
            ("A graph family is a sequence of finite connected graphs.",),
            ("For every graph in the family and every vertex subset.",),
            ("Degrees are uniformly bounded and local expansion is uniform.",),
        ),
        "2501.00003": (
            "Every ergodic measurable action satisfying property P admits a finite rigidity "
            "witness.",
            ("A measurable action is measure preserving on a standard probability space.",),
            ("For every ergodic action satisfying property P.",),
            ("Property P and ergodicity hold.",),
        ),
    }
    for candidate in candidates:
        semantic = normalized[candidate.arxiv_id]
        pools.audit_candidate(
            candidate.problem_candidate_id,
            decision="INCLUDE",
            normalized_statement=semantic[0],
            definitions=semantic[1],
            quantifiers=semantic[2],
            hypotheses=semantic[3],
            audit_note="Human checked the source sentence and restored its scope.",
            audited_by="expert-auditor",
            actor_kind="USER",
            now=NOW,
        )
    semantic_ref = publisher.publish(
        data=b'{"audit":"human-reviewed"}',
        logical_name="semantic-audit.json",
        media_type="application/json",
    )
    pools.freeze(
        "pool-2025-math",
        frozen_by="expert-auditor",
        actor_kind="USER",
        semantic_audit_artifact=semantic_ref,
        now=NOW,
    )
    scored = tuple(
        pools.score_candidate(
            candidate.problem_candidate_id,
            importance=90,
            verifiability=80,
            bridge_potential=75,
            estimated_cost=20,
            recommend_threshold=50,
            now=NOW,
        )
        for candidate in candidates
    )
    assert all(candidate.recommendation_status == "RECOMMENDED" for candidate in scored)
    assert all(
        candidate.expert_confirmation_status == "EXTERNAL_CONFIRMATION_PENDING"
        and candidate.author_confirmation_status == "EXTERNAL_CONFIRMATION_PENDING"
        and candidate.machine_certificate_status == "NOT_ATTEMPTED"
        and candidate.heterogeneous_review_status == "NOT_REQUESTED"
        for candidate in scored
    )

    template = {
        "objects": ["source paper"],
        "domain": "research mathematics",
        "quantifiers": ["For every object in the stated domain."],
        "boundary_conditions": ["Use the frozen arXiv version."],
        "exact_negation": "There exists a counterexample satisfying all hypotheses.",
        "allowed_tools": ["proof-kernel", "managed-python"],
        "success_conditions": ["A kernel-accepted proof or counterexample."],
    }
    template_ref = publisher.publish(
        data=json.dumps(template).encode(),
        logical_name="contract-template.json",
        media_type="application/json",
    )
    pools.bind_artifact(
        "pool-2025-math",
        binding_kind="CONTRACT_TEMPLATE",
        artifact=template_ref,
        bound_by="researcher-1",
        now=NOW,
    )
    bindings = pools.artifact_bindings("pool-2025-math")
    assert {item.binding_kind for item in bindings} == {
        "SEMANTIC_AUDIT",
        "CONTRACT_TEMPLATE",
    }
    assert all(item.authority_effect == "NO_FACT" for item in bindings)
    session = ProductSession("session-1", "researcher-1", ("cap-create",))
    candidate_ids = tuple(candidate.problem_candidate_id for candidate in scored)
    receipt = pipeline.dispatch_batch(
        batch_id="batch-1",
        request_id="batch-request-1",
        problem_pool_id="pool-2025-math",
        candidate_ids=candidate_ids,
        contract_template_artifact=template_ref,
        per_run_budget={"microunits": 200_000, "wall_seconds": 3600},
        labels=("arxiv-open-problem",),
        session=session,
        expected_deployment_revision=7,
        now=NOW,
    )
    assert receipt.state == "PENDING"
    replayed_receipt = pipeline.dispatch_batch(
        batch_id="batch-1",
        request_id="batch-request-1",
        problem_pool_id="pool-2025-math",
        candidate_ids=candidate_ids,
        contract_template_artifact=template_ref,
        per_run_budget={"microunits": 200_000, "wall_seconds": 3600},
        labels=("arxiv-open-problem",),
        session=session,
        expected_deployment_revision=7,
        now="2026-08-14T01:00:00Z",
    )
    assert replayed_receipt.receipt_id == receipt.receipt_id

    first = pipeline.execute_batch(
        batch_id="batch-1",
        contract_template_artifact=template_ref,
        owner="researcher-1",
        labels=("arxiv-open-problem",),
        session=session,
        expected_deployment_revision=7,
        now=NOW,
    )
    assert first.state == "COMPLETED"
    assert len(first.created_runs) == 2

    restarted = ArxivBatchPipeline(
        db_path=db,
        pools=ProblemPoolStore(db),
        snapshots=snapshots,
        artifacts=reader,
        commands=commands,
    )
    second = restarted.execute_batch(
        batch_id="batch-1",
        contract_template_artifact=template_ref,
        owner="researcher-1",
        labels=("arxiv-open-problem",),
        session=session,
        expected_deployment_revision=7,
        now="2026-08-14T02:00:00Z",
    )
    assert second == first
    completed_replay = restarted.dispatch_batch(
        batch_id="batch-1",
        request_id="batch-request-1",
        problem_pool_id="pool-2025-math",
        candidate_ids=candidate_ids,
        contract_template_artifact=template_ref,
        per_run_budget={"microunits": 200_000, "wall_seconds": 3600},
        labels=("arxiv-open-problem",),
        session=session,
        expected_deployment_revision=7,
        now="2026-08-14T03:00:00Z",
    )
    assert completed_replay.receipt_id == receipt.receipt_id
    with sqlite3.connect(db) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM product_problem_batch_runs WHERE batch_id='batch-1'"
        ).fetchone() == (2,)
    create_calls = [item for item in commands.calls if item.command_type == "CREATE_RESEARCH"]
    assert len({item.request_id for item in create_calls}) == 2
    assert all(item.scope.kind == "GLOBAL" for item in commands.calls)
    assert all(
        "graph family" in json.dumps(dict(item.payload))
        or "measurable action" in json.dumps(dict(item.payload))
        for item in create_calls
    )
    with pytest.raises(ArxivBatchError, match="labels differ"):
        restarted.execute_batch(
            batch_id="batch-1",
            contract_template_artifact=template_ref,
            owner="researcher-1",
            labels=("changed-label",),
            session=session,
            expected_deployment_revision=7,
            now=NOW,
        )


def test_current_arxiv_window_is_a_live_cas_snapshot_and_enters_denominator(
    tmp_path: Path,
) -> None:
    db, jobs, runs, publisher, reader, snapshots = _stores(tmp_path)
    tool_run_id, attempt_id = _add_run(db, runs, jobs, 1)
    live = snapshots.capture_live(
        snapshot_id="snapshot-live-window",
        tool_run_id=tool_run_id,
        attempt_id=attempt_id,
        connector=ArxivConnector(UrllibTransport()),
        request={"kind": "SEARCH", "query": "mathematics conjecture", "max_results": 3},
        queried_at=NOW,
        timeout_seconds=30,
    )
    assert live.mode == "LIVE_QUERY"
    assert live.result_status in {"SUCCESS", "NO_HIT"}
    assert live.raw_kind == "WIRE_RESPONSE"
    assert live.raw_response.artifact_id in publisher.records

    pools = ProblemPoolStore(db)
    math_subjects = tuple(
        f"math.{suffix}"
        for suffix in [
            "AG",
            "AT",
            "AP",
            "CA",
            "CO",
            "CT",
            "CV",
            "DG",
            "DS",
            "FA",
            "GM",
            "GN",
            "GR",
            "GT",
            "HO",
            "IT",
            "KT",
            "LO",
            "MG",
            "MP",
            "NA",
            "NT",
            "OA",
            "OC",
            "PR",
            "QA",
            "RA",
            "RT",
            "SG",
            "SP",
            "ST",
        ]
    )
    pools.create(
        problem_pool_id="live-window",
        deployment_id="deployment-1",
        date_from="1991-01-01",
        date_to="2026-08-14",
        subjects=math_subjects,
        version_rule="LATEST_VISIBLE",
        withdrawal_rule="EXCLUDE_WITHDRAWN",
        exclusion_rules=("REQUIRE_EXPLICIT_MARKER",),
        now=NOW,
    )
    ArxivBatchPipeline(
        db_path=db,
        pools=pools,
        snapshots=snapshots,
        artifacts=reader,
        commands=FormalCommands(),
    ).ingest_snapshots(
        problem_pool_id="live-window",
        snapshot_ids=(live.snapshot_id,),
        now=NOW,
    )
    assert pools.denominator("live-window").total >= 1
