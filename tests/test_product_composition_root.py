from __future__ import annotations

import json
import sqlite3
import urllib.request
from collections.abc import Mapping
from pathlib import Path
from typing import Any, cast

import pytest

from rk.http.app import (
    BootstrapAdmin,
    PublishedRouteFactories,
    bootstrap_admin_session,
    build_application,
)
from rk.http.daemon_main import ProductHttpDaemon
from rk.http_shell import DuplicateRouteError
from rk.product.activity_routes import activity_router_factory
from rk.product.activity_store import ActivityStore
from rk.product.adapters import ProductHttpCommandAdapter
from rk.product.artifact_read import ArtifactReadService
from rk.product.artifact_routes import ArtifactAccessAuthorizer, artifact_router_factory
from rk.product.artifact_upload import ArtifactUploadStore
from rk.product.artifact_upload_routes import artifact_upload_router
from rk.product.attestation_import import ReviewAttestationImporter
from rk.product.command_routes import command_router_factory
from rk.product.identity_routes import identity_router
from rk.product.log_tail import PublicLogStore
from rk.product.query_routes import query_router_factory
from rk.product.query_service import ProductQueryService
from rk.product.review_routes import ReviewInboxIndex, review_router
from rk.product.reviews import ReviewTaskStore

ROOT = Path(__file__).parents[1]
NOW = "2026-08-14T12:00:00Z"
EXPIRES = "2026-08-15T12:00:00Z"


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


def _root(tmp_path: Path) -> Any:
    return bootstrap_admin_session(
        data_root=tmp_path / "data",
        schema_fragments=ROOT / "schema_fragments",
        deployment_id="deployment-one",
        organization_id="organization-one",
        limits={"upload_bytes": 1024, "graph_nodes": 200},
        admin=BootstrapAdmin(
            identity_id="identity-admin",
            subject_id="admin:one",
            display_name="Administrator",
            login_secret="administrator-login-secret",
        ),
        now=NOW,
        expires_at=EXPIRES,
    )


def _factories(root: Any, admin_bindings: list[str]) -> PublishedRouteFactories:
    config = root.config
    session = identity_router(
        sessions=root.sessions,
        clock=lambda: NOW,
        expires_at=lambda _now: EXPIRES,
        secure_cookie=False,
    )
    command = command_router_factory(
        adapter=cast(ProductHttpCommandAdapter, object()),
        deployment_id=config.deployment_id,
    )
    query = query_router_factory(
        service=cast(ProductQueryService, object()),
        deployment_id=config.deployment_id,
    )
    activity = activity_router_factory(
        db_path=config.db_path,
        store=ActivityStore(config.db_path),
        authorizer=AllowActivity(),
        clock=lambda: NOW,
    )
    upload = artifact_upload_router(
        uploads=cast(ArtifactUploadStore, object()),
        authorize=lambda _principal, _operation: None,
    )
    artifact = artifact_router_factory(
        artifacts=ArtifactReadService(metadata=EmptyMetadata(), cas_root=config.cas_root),
        logs=cast(PublicLogStore, object()),
        authorizer=cast(ArtifactAccessAuthorizer, AllowArtifacts()),
        other_operations=upload.handle,
    )
    review = review_router(
        sessions=root.sessions,
        tasks=cast(ReviewTaskStore, object()),
        importer=cast(ReviewAttestationImporter, object()),
        inbox=cast(ReviewInboxIndex, EmptyInbox()),
        clock=lambda: NOW,
    )

    def bind_admin_variants() -> None:
        admin_bindings.append("DEPLOYMENT_OPERATION+DEPLOYMENT_STATUS")

    return PublishedRouteFactories(
        command=lambda: command,
        query=lambda: query,
        activity=lambda: activity,
        artifact=lambda: artifact,
        session=lambda: session,
        admin=bind_admin_variants,
        review=lambda: review,
    )


def test_empty_root_bootstraps_real_admin_session_without_browser_capability(
    tmp_path: Path,
) -> None:
    root = _root(tmp_path)

    view = root.sessions.view(root.session_id, now=NOW)
    principal = root.sessions.derive(root.session_id, now=NOW)
    with sqlite3.connect(root.config.db_path) as connection:
        role = connection.execute(
            "SELECT role FROM product_identities WHERE identity_id='identity-admin'"
        ).fetchone()

    assert view.role.value == "ADMIN"
    assert role == ("ADMIN",)
    assert root.session_cookie == f"rk_session={root.session_id}"
    assert principal.capability_ids and "capability" not in root.__dataclass_fields__
    with pytest.raises(ValueError, match="empty data root"):
        _root(tmp_path)


def test_one_app_mounts_all_generic_production_route_families_once(tmp_path: Path) -> None:
    root = _root(tmp_path)
    admin_bindings: list[str] = []
    factories = _factories(root, admin_bindings)
    app = build_application(
        config=root.config,
        sessions=root.sessions,
        clock=lambda: NOW,
        factories=factories,
    )
    routes = {(item.method, item.path) for item in app.routes}

    assert admin_bindings == ["DEPLOYMENT_OPERATION+DEPLOYMENT_STATUS"]
    assert len(routes) == len(app.routes) == 19
    assert ("GET", "/v1/session/options") in routes
    assert ("POST", "/v1/session/enter") in routes
    assert ("POST", "/v1/research/{run_id}/commands") in routes
    assert ("POST", "/v1/deployment/operations") in routes
    assert ("POST", "/v1/deployment/queries") in routes
    assert ("GET", "/v1/research/{run_id}/events") in routes
    assert ("POST", "/v1/artifacts/operations") in routes
    assert ("POST", "/v1/session/login") in routes
    assert ("GET", "/v1/reviews/inbox") in routes
    assert ("GET", "/v1/meta") in routes
    assert all(not path.startswith("/pages/") for _method, path in routes)

    with pytest.raises(DuplicateRouteError):
        build_application(
            config=root.config,
            sessions=root.sessions,
            clock=lambda: NOW,
            factories=PublishedRouteFactories(
                command=lambda: factories.command(),
                query=lambda: factories.command(),
                activity=factories.activity,
                artifact=factories.artifact,
                session=factories.session,
                admin=lambda: None,
                review=factories.review,
            ),
        )


def test_daemon_real_listener_health_session_and_restart(tmp_path: Path) -> None:
    root = _root(tmp_path)
    app = build_application(
        config=root.config,
        sessions=root.sessions,
        clock=lambda: NOW,
        factories=_factories(root, []),
    )
    daemon = ProductHttpDaemon(
        app=app,
        deployment_id=root.config.deployment_id,
        host="127.0.0.1",
        port=0,
    )

    daemon.start()
    host, port = daemon.address
    try:
        health = json.load(urllib.request.urlopen(f"http://{host}:{port}/healthz"))
        request = urllib.request.Request(
            f"http://{host}:{port}/v1/session/me",
            headers={"Cookie": root.session_cookie},
        )
        session = json.load(urllib.request.urlopen(request))
    finally:
        daemon.stop()

    assert health == {
        "deployment_id": "deployment-one",
        "schema_version": "rk.product.daemon_health.v1",
        "status": "AVAILABLE",
    }
    assert session["role"] == "ADMIN"
    assert "capability_id" not in session and "capability_ids" not in session

    daemon.start()
    try:
        host, port = daemon.address
        meta = json.load(urllib.request.urlopen(f"http://{host}:{port}/v1/meta"))
    finally:
        daemon.stop()
    assert meta["product_version"] == "RK-PRODUCT-1.1"
