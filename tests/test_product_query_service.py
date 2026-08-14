from __future__ import annotations

import sqlite3
from pathlib import Path
from types import MappingProxyType

import pytest

from rk.extensions import ProductActivity
from rk.product.activity_store import ActivityStore
from rk.product.api import ProductSession, QuerySpec
from rk.product.graph_index import (
    GraphAuthoritySnapshot,
    GraphIndex,
    IndexedGraphEdge,
    IndexedGraphNode,
)
from rk.product.graph_query import GraphQueryService, StaleQuery
from rk.product.identity import IdentityStore, ProductRole
from rk.product.jobs import JobStore, RetrySafety
from rk.product.listing import ResearchCatalog
from rk.product.operations import OperationStore
from rk.product.query_service import (
    ProductQueryService,
    QueryAuthenticationError,
    QueryScopeDenied,
    QueryVariantUnavailable,
    SessionQueryAuthorizer,
    SQLiteQueryFenceSource,
)
from rk.product.receipt_query import ReceiptJobQuery
from rk.product.sessions import SessionStore
from rk.product.summary import BudgetSummary, ResearchSummaryProjection
from rk.product_migrations import ProductMigrationAssembler, ProductMigrationRegistry


class Services:
    def __init__(self, tmp_path: Path) -> None:
        self.path = tmp_path / "product.sqlite"
        with sqlite3.connect(self.path, isolation_level=None) as connection:
            ProductMigrationAssembler(ProductMigrationRegistry(Path("schema_fragments"))).apply(
                connection
            )
        self.activity = ActivityStore(self.path)
        self.cursor = self.activity.append(
            ProductActivity(
                event_id="event-1",
                scope_kind="RUN",
                run_id="run-1",
                source="KERNEL",
                research_revision=1,
                kernel_event_id="kernel-1",
                entity_refs={"run_id": "run-1"},
                payload={"type": "SNAPSHOT_FENCE"},
                recorded_at="2026-08-13T00:00:00Z",
            )
        )
        self.catalog = ResearchCatalog(self.path)
        self.catalog.register(
            run_id="run-1",
            title="Spectral theorem",
            question_summary="Prove the spectral statement",
            owner="subject-main",
            labels=("analysis",),
            created_at="2026-08-13T00:00:00Z",
        )
        self.catalog.project(
            "run-1",
            ResearchSummaryProjection(
                "OPEN",
                "RUNNING",
                "VERIFIED",
                "NONE",
                "PROVING",
                (),
                ("ReviseClaim",),
                (
                    {
                        "command_type": "ReviseClaim",
                        "principal_subject_id": "subject-main",
                        "target_ids": ["claim-a"],
                        "required_inputs": ["proof"],
                        "blocked_by": [],
                    },
                    {
                        "command_type": "RunTool",
                        "principal_subject_id": "subject-main",
                        "target_ids": ["claim-b"],
                        "required_inputs": [],
                        "blocked_by": [],
                    },
                    {
                        "command_type": "SubmitAtomicReview",
                        "principal_subject_id": "subject-other",
                        "target_ids": ["claim-a"],
                        "required_inputs": [],
                        "blocked_by": [],
                    },
                ),
                BudgetSummary(100, 10, 0, 0),
                "2026-08-13T00:00:01Z",
                "Claim verified",
                1,
                1,
                self.cursor,
                "a" * 64,
            ),
        )
        self.operations = OperationStore(self.path, iter(("receipt-1",)).__next__)
        self.jobs = JobStore(self.path, iter(("lease-1",)).__next__)
        receipt = self.operations.reserve(
            scope_key="RUN:run-1",
            request_id="request-1",
            request_digest="b" * 64,
            pending_receipt={
                "schema_version": "rk.product.receipt.v1",
                "request_id": "request-1",
                "scope": {"kind": "RUN", "run_id": "run-1"},
                "updated_at": "2026-08-13T00:00:00Z",
                "state": "PENDING",
                "job_id": "job-1",
            },
            now="2026-08-13T00:00:00Z",
        ).receipt
        self.jobs.enqueue(
            job_id="job-1",
            receipt_id=receipt.receipt_id,
            scope_kind="RUN",
            run_id="run-1",
            deployment_id=None,
            kind="MATLAS_QUERY",
            requested_by="subject-main",
            request_id="request-1",
            retry_safety=RetrySafety.READ_ONLY,
            idempotency_key=None,
            now="2026-08-13T00:00:00Z",
        )
        self.graph_index = GraphIndex(self.path, clock=lambda: "2026-08-13T00:00:02Z")
        self.graph_index.rebuild(
            GraphAuthoritySnapshot(
                "run-1",
                self.cursor,
                1,
                "kernel-graph-1",
                "2026-08-13T00:00:01Z",
                (
                    IndexedGraphNode(
                        "claim-a",
                        "A",
                        "spectral premise",
                        "VERIFIED",
                        True,
                        "LEMMA",
                        {"mathematical": "KERNEL"},
                        1,
                        "LEAN",
                        route_id="route-1",
                    ),
                    IndexedGraphNode(
                        "claim-b",
                        "B",
                        "spectral conclusion",
                        "VERIFIED",
                        True,
                        "THEOREM",
                        {"mathematical": "KERNEL"},
                        1,
                        "LEAN",
                        route_id="route-1",
                    ),
                ),
                (IndexedGraphEdge("edge-1", "claim-a", "claim-b", "FORWARD", "DONE"),),
            )
        )
        identities = IdentityStore(self.path, lambda: b"s" * 16)
        identities.register(
            identity_id="identity-main",
            subject_id="subject-main",
            display_name="Main",
            role=ProductRole.MAIN,
            capability_id="cap-main",
            login_secret="correct horse battery staple",
            now="2026-08-13T00:00:00Z",
        )
        self.sessions = SessionStore(
            self.path,
            identities,
            iter(("session-1",)).__next__,
            "organization-1",
        )
        view = self.sessions.login(
            identity_id="identity-main",
            login_secret="correct horse battery staple",
            now="2026-08-13T00:00:00Z",
            expires_at="2026-08-14T00:00:00Z",
        )
        self.session = ProductSession(view.session_id, view.principal_subject_id, ("cap-main",))
        self.service = ProductQueryService(
            catalog=self.catalog,
            receipt_jobs=ReceiptJobQuery(self.operations, self.jobs),
            graph=GraphQueryService(self.path, self.graph_index, cursor_secret=b"g" * 32),
            fences=SQLiteQueryFenceSource(self.path, "deployment-1"),
            authorizer=SessionQueryAuthorizer(
                self.sessions,
                deployment_id="deployment-1",
                clock=lambda: "2026-08-13T00:01:00Z",
            ),
            cursor_secret=b"q" * 32,
        )


def _scope() -> MappingProxyType[str, object]:
    return MappingProxyType(
        {
            "kind": "RUN",
            "run_id": "run-1",
            "at_revision": 1,
            "at_contract_version": 1,
        }
    )


def _global() -> MappingProxyType[str, object]:
    return MappingProxyType(
        {"kind": "GLOBAL", "deployment_id": "deployment-1", "at_catalog_revision": 2}
    )


def test_list_research_and_action_items_preserve_catalog_fence_and_stable_ids(
    tmp_path: Path,
) -> None:
    services = Services(tmp_path)
    listed = services.service.execute(
        services.session,
        QuerySpec(
            _global(),
            "LIST_RESEARCH",
            MappingProxyType({"page": {"limit": 10}, "owners": ["subject-main"]}),
        ),
    )
    assert listed.stable_entity_id == "research-catalog:deployment-1"
    assert listed.fence == {
        "scope_kind": "GLOBAL",
        "deployment_id": "deployment-1",
        "catalog_revision": 2,
        "last_cursor": 1,
    }
    item = listed.data["items"][0]
    assert item["stable_entity_id"] == "run-1"
    assert item["projection_type"] == "LIST_RESEARCH"

    first = services.service.execute(
        services.session,
        QuerySpec(_global(), "ACTION_ITEMS", MappingProxyType({"page": {"limit": 1}})),
    )
    assert first.data["page"]["total"] == 2
    cursor = first.data["page"]["next_cursor"]
    second = services.service.execute(
        services.session,
        QuerySpec(
            _global(),
            "ACTION_ITEMS",
            MappingProxyType({"page": {"limit": 1, "cursor": cursor}}),
        ),
    )
    assert first.data["items"][0]["stable_entity_id"] != second.data["items"][0]["stable_entity_id"]
    assert all(item["run_id"] == "run-1" for item in (*first.data["items"], *second.data["items"]))


def test_receipt_and_job_use_real_stores_and_run_snapshot_fence(tmp_path: Path) -> None:
    services = Services(tmp_path)
    receipt = services.service.execute(
        services.session,
        QuerySpec(_scope(), "PRODUCT_RECEIPT", MappingProxyType({"receipt_id": "receipt-1"})),
    )
    job = services.service.execute(
        services.session,
        QuerySpec(_scope(), "JOB", MappingProxyType({"job_id": "job-1"})),
    )

    assert receipt.fence["research_revision"] == 1
    assert receipt.fence["last_cursor"] == 1
    assert receipt.data["entity"]["state"] == "PENDING"
    assert receipt.data["entity"]["stable_entity_id"] == "receipt-1"
    assert job.data["entity"]["kind"] == "MATLAS_QUERY"
    assert job.data["entity"]["authority_effect"] == "NONE"


def test_graph_slice_search_and_both_closures_dispatch_to_b06b(tmp_path: Path) -> None:
    services = Services(tmp_path)
    slice_result = services.service.execute(
        services.session,
        QuerySpec(
            _scope(),
            "GRAPH_SLICE",
            MappingProxyType(
                {
                    "mode": "VERIFIED",
                    "seed_ids": ["claim-a"],
                    "direction": "BOTH",
                    "depth": 2,
                    "filters": {},
                    "node_limit": 200,
                    "at_revision": 1,
                }
            ),
        ),
    )
    search = services.service.execute(
        services.session,
        QuerySpec(
            _scope(),
            "GRAPH_SEARCH",
            MappingProxyType(
                {
                    "page": {"limit": 20},
                    "text": "spectral",
                    "mode": "VERIFIED",
                    "at_revision": 1,
                }
            ),
        ),
    )
    dependency = services.service.execute(
        services.session,
        QuerySpec(
            _scope(),
            "DEPENDENCY_CLOSURE",
            MappingProxyType({"claim_id": "claim-b", "at_revision": 1, "node_limit": 200}),
        ),
    )
    reverse = services.service.execute(
        services.session,
        QuerySpec(
            _scope(),
            "REVERSE_CLOSURE",
            MappingProxyType({"claim_id": "claim-a", "at_revision": 1, "node_limit": 200}),
        ),
    )

    assert slice_result.fence["last_cursor"] == services.cursor
    assert [node["claim_id"] for node in slice_result.data["nodes"]] == [
        "claim-a",
        "claim-b",
    ]
    assert search.data["page"]["total"] == 2
    assert [node["claim_id"] for node in dependency.data["nodes"]] == [
        "claim-b",
        "claim-a",
    ]
    assert [node["claim_id"] for node in reverse.data["nodes"]] == [
        "claim-a",
        "claim-b",
    ]


def test_unimplemented_variant_and_sort_are_explicitly_unavailable(tmp_path: Path) -> None:
    services = Services(tmp_path)
    with pytest.raises(QueryVariantUnavailable) as unknown:
        services.service.execute(
            services.session, QuerySpec(_scope(), "CONTRACT", MappingProxyType({}))
        )
    assert unknown.value.http_status == 503
    with pytest.raises(QueryVariantUnavailable, match="TITLE_ASC"):
        services.service.execute(
            services.session,
            QuerySpec(
                _global(),
                "LIST_RESEARCH",
                MappingProxyType({"page": {"limit": 20}, "sort": "TITLE_ASC"}),
            ),
        )


def test_session_identity_and_deployment_scope_are_not_body_authority(tmp_path: Path) -> None:
    services = Services(tmp_path)
    forged = ProductSession("session-1", "subject-main", ("cap-admin",))
    with pytest.raises(QueryAuthenticationError):
        services.service.execute(
            forged,
            QuerySpec(_global(), "LIST_RESEARCH", MappingProxyType({"page": {"limit": 20}})),
        )
    with pytest.raises(QueryScopeDenied):
        services.service.execute(
            services.session,
            QuerySpec(
                MappingProxyType({"kind": "GLOBAL", "deployment_id": "deployment-2"}),
                "LIST_RESEARCH",
                MappingProxyType({"page": {"limit": 20}}),
            ),
        )
    with pytest.raises(ValueError, match="fields"):
        services.service.execute(
            services.session,
            QuerySpec(
                _global(),
                "ACTION_ITEMS",
                MappingProxyType({"page": {"limit": 20}, "role": "ADMIN"}),
            ),
        )


def test_revision_change_is_stale_instead_of_rebased(tmp_path: Path) -> None:
    services = Services(tmp_path)
    with pytest.raises(StaleQuery, match="revision"):
        services.service.execute(
            services.session,
            QuerySpec(
                MappingProxyType(
                    {
                        "kind": "RUN",
                        "run_id": "run-1",
                        "at_revision": 0,
                        "at_contract_version": 1,
                    }
                ),
                "JOB",
                MappingProxyType({"job_id": "job-1"}),
            ),
        )
