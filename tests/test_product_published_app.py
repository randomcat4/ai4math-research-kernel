from __future__ import annotations

import asyncio
import json
import sqlite3
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any, cast

import pytest

from rk.http_shell import DuplicateRouteError, HttpRequest, HttpResponse
from rk.product.activity_routes import activity_router_factory
from rk.product.activity_store import ActivityStore
from rk.product.adapters import CommandJsonAdapter, ProductHttpCommandAdapter
from rk.product.api import (
    ProductCommand,
    ProductDecision,
    ProductReceipt,
    ProductSession,
    ResearchProduct,
)
from rk.product.artifact_read import ArtifactReadService
from rk.product.artifact_routes import ArtifactAccessAuthorizer
from rk.product.artifact_upload import ArtifactUploadStore
from rk.product.artifact_upload_routes import artifact_upload_router
from rk.product.attestation_import import ReviewAttestationImporter
from rk.product.command_routes import command_router_factory
from rk.product.command_service import CommandPlan, ExecutionClass, ProductCommandService
from rk.product.identity import IdentityStore, ProductRole
from rk.product.identity_routes import identity_router
from rk.product.jobs import JobStore, RetrySafety
from rk.product.log_tail import PublicLogStore
from rk.product.operations import OperationStore
from rk.product.published_app import (
    PublishedAppConfig,
    PublishedAppRoutes,
    PublishedHttpApplication,
    PublishedSessionMiddleware,
    build_published_app,
)
from rk.product.query_routes import query_router_factory
from rk.product.query_service import ProductQueryService
from rk.product.review_routes import ReviewInboxIndex, review_router
from rk.product.reviews import ReviewTaskStore
from rk.product.sessions import SessionStore
from rk.product.supervisor import RuntimeSupervisor
from rk.product_migrations import ProductMigrationAssembler, ProductMigrationRegistry

ROOT = Path(__file__).parents[1]
NOW = "2026-08-13T18:00:00Z"
EXPIRES = "2026-08-14T18:00:00Z"


class Ids:
    def __init__(self, prefix: str) -> None:
        self._prefix = prefix
        self._value = 0

    def __call__(self) -> str:
        self._value += 1
        return f"{self._prefix}-{self._value}"


class UnusedAuthority:
    def apply(self, session: ProductSession, request: ProductCommand) -> ProductDecision:
        raise AssertionError("durable command must not call mathematical authority")


class CommandProduct:
    def __init__(self, service: ProductCommandService) -> None:
        self._service = service

    def command(self, session: ProductSession, request: ProductCommand) -> ProductReceipt:
        return self._service.execute(session, request)


class EmptyMetadata:
    def get_artifact(self, artifact_id: str) -> Mapping[str, Any] | None:
        return None


class AllowArtifacts:
    def authorize_artifact(self, principal: Any, descriptor: Any) -> None:
        return None

    def authorize_log(self, principal: Any, log: Any) -> None:
        return None


class AllowActivity:
    def authorize_subscription(self, principal: Any, run_id: str) -> None:
        return None


class EmptyInbox:
    def task_ids_for_assignee(self, assignee_identity_id: str) -> tuple[str, ...]:
        return ()


def _database(tmp_path: Path) -> Path:
    db = tmp_path / "product.sqlite"
    with sqlite3.connect(db) as connection:
        ProductMigrationAssembler(ProductMigrationRegistry(ROOT / "schema_fragments")).apply(
            connection
        )
    return db


def _app(tmp_path: Path) -> tuple[PublishedHttpApplication, Path]:
    db = _database(tmp_path)
    identities = IdentityStore(db, lambda: b"0" * 16)
    identities.register(
        identity_id="identity-main",
        subject_id="main:one",
        display_name="Main",
        role=ProductRole.MAIN,
        capability_id="cap:main",
        login_secret="main-login-secret",
        now=NOW,
    )
    sessions = SessionStore(db, identities, Ids("session"), "organization-one")
    identity = identity_router(
        sessions=sessions,
        clock=lambda: NOW,
        expires_at=lambda _now: EXPIRES,
        secure_cookie=False,
    )
    jobs = JobStore(db, Ids("lease"))
    supervisor = RuntimeSupervisor(
        store=jobs,
        holder_id="published-app",
        clock=lambda: NOW,
        retry_policy={"RUN_TOOL": RetrySafety.IDEMPOTENT},
    )
    service = ProductCommandService(
        operations=OperationStore(db, Ids("receipt")),
        authority=UnusedAuthority(),
        jobs=supervisor,
        plans={"RUN_TOOL": CommandPlan(ExecutionClass.DURABLE_JOB, "TOOL_RUN")},
        id_generator=Ids("job"),
        clock=lambda: NOW,
    )
    command = command_router_factory(
        adapter=ProductHttpCommandAdapter(
            CommandJsonAdapter(cast(ResearchProduct, CommandProduct(service)))
        ),
        deployment_id="deployment-one",
    )
    query = query_router_factory(
        service=cast(ProductQueryService, object()), deployment_id="deployment-one"
    )
    upload = artifact_upload_router(
        uploads=cast(ArtifactUploadStore, object()),
        authorize=lambda _principal, _operation: None,
    )
    activity = activity_router_factory(
        db_path=db,
        store=ActivityStore(db),
        authorizer=AllowActivity(),
        clock=lambda: NOW,
    )
    review = review_router(
        sessions=sessions,
        tasks=cast(ReviewTaskStore, object()),
        importer=cast(ReviewAttestationImporter, object()),
        inbox=cast(ReviewInboxIndex, EmptyInbox()),
        clock=lambda: NOW,
    )
    config = PublishedAppConfig(
        db_path=db,
        cas_root=tmp_path / "cas",
        spool_root=tmp_path / "spool",
        deployment_id="deployment-one",
        limits={"upload_bytes": 1024, "graph_nodes": 200},
    )
    routes = PublishedAppRoutes(
        command=command,
        query=query,
        upload=upload,
        activity=activity,
        identity=identity,
        review=review,
        artifacts=ArtifactReadService(metadata=EmptyMetadata(), cas_root=config.cas_root),
        logs=cast(PublicLogStore, object()),
        artifact_authorizer=cast(ArtifactAccessAuthorizer, AllowArtifacts()),
    )
    return build_published_app(
        config=config, sessions=sessions, clock=lambda: NOW, routes=routes
    ), db


def _json_request(method: str, path: str, value: object, cookie: str | None = None) -> HttpRequest:
    headers = {"content-type": "application/json"}
    if cookie is not None:
        headers["cookie"] = cookie
    return HttpRequest(
        method,
        path,
        headers,
        json.dumps(value, separators=(",", ":")).encode(),
    )


def _response(app: PublishedHttpApplication, request: HttpRequest) -> HttpResponse:
    result = asyncio.run(app(request))
    assert isinstance(result, HttpResponse)
    return result


def test_published_app_mounts_one_complete_route_graph(tmp_path: Path) -> None:
    app, _db = _app(tmp_path)

    mounted = {(route.method, route.path) for route in app.routes}

    assert len(mounted) == 19
    assert ("POST", "/v1/research/{run_id}/commands") in mounted
    assert ("POST", "/v1/research/{run_id}/queries") in mounted
    assert ("POST", "/v1/artifacts/operations") in mounted
    assert ("GET", "/v1/artifacts/{artifact_id}") in mounted
    assert ("GET", "/v1/research/{run_id}/events") in mounted
    assert ("GET", "/v1/reviews/inbox") in mounted
    assert ("GET", "/v1/meta") in mounted
    assert ("GET", "/v1/session/options") in mounted
    assert ("POST", "/v1/session/enter") in mounted


def test_in_process_login_and_command_create_real_receipt_and_job(tmp_path: Path) -> None:
    app, db = _app(tmp_path)
    login = _response(
        app,
        _json_request(
            "POST",
            "/v1/session/login",
            {"identity_id": "identity-main", "login_secret": "main-login-secret"},
        ),
    )
    cookie = login.headers["set-cookie"].split(";", 1)[0]

    command = _response(
        app,
        _json_request(
            "POST",
            "/v1/research/run-one/commands",
            {
                "schema_version": "rk.product.command.v1",
                "request_id": "request-one",
                "scope": {
                    "kind": "RUN",
                    "run_id": "run-one",
                    "expected_revision": 3,
                    "expected_contract_version": 1,
                },
                "command": {"type": "RUN_TOOL", "payload": {}},
            },
            cookie,
        ),
    )

    assert login.status == 200
    assert command.status == 200
    assert command.body["state"] == "PENDING"
    with sqlite3.connect(db) as connection:
        assert connection.execute("SELECT COUNT(*) FROM product_receipts").fetchone() == (1,)
        assert connection.execute("SELECT COUNT(*) FROM product_jobs").fetchone() == (1,)


def test_unauthed_business_route_is_rejected_before_router(tmp_path: Path) -> None:
    app, _db = _app(tmp_path)

    response = _response(app, HttpRequest("GET", "/v1/research?limit=20"))

    assert response.status == 401
    assert response.body["code"] == "SESSION_REQUIRED"


def test_duplicate_router_registration_aborts_publication(tmp_path: Path) -> None:
    app, db = _app(tmp_path)
    identities = IdentityStore(db, lambda: b"1" * 16)
    sessions = SessionStore(db, identities, Ids("unused"), "organization-one")
    middleware = PublishedSessionMiddleware(
        sessions,
        clock=lambda: NOW,
        cookie_name="rk_session",
        anonymous_routes=frozenset(),
    )

    with pytest.raises(DuplicateRouteError):
        PublishedHttpApplication(
            routers=(_RouteRouter(app.routes), _RouteRouter(app.routes)),
            middleware=middleware,
        )


class _RouteRouter:
    def __init__(self, routes: Sequence[Any]) -> None:
        self._routes = routes

    def routes(self) -> Sequence[Any]:
        return self._routes
