"""Release composition root for the single RK-PRODUCT-1.1 HTTP application."""

from __future__ import annotations

import os
import secrets
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path

from rk.http.route_registry import PublishedRouteFactories
from rk.http_shell import HttpErrorClass, HttpResponse, ProductHttpError, RouteSpec, SessionRequest
from rk.migrations import MigrationRunner
from rk.product.identity import IdentityStore, ProductRole
from rk.product.published_app import (
    PublishedAppConfig,
    PublishedHttpApplication,
    PublishedSessionMiddleware,
)
from rk.product.sessions import SessionStore
from rk.product_migrations import ProductMigrationAssembler, ProductMigrationRegistry
from rk.resources import resource_root
from rk.sqlite import open_sqlite


@dataclass(frozen=True, slots=True)
class BootstrapAdmin:
    identity_id: str
    subject_id: str
    display_name: str
    login_secret: str

    def __post_init__(self) -> None:
        if not self.identity_id or not self.subject_id or not self.display_name:
            raise ValueError("bootstrap administrator identity is incomplete")
        if len(self.login_secret) < 16:
            raise ValueError("bootstrap administrator secret must contain at least 16 characters")


@dataclass(frozen=True, slots=True)
class BootstrappedDataRoot:
    config: PublishedAppConfig
    sessions: SessionStore
    session_id: str
    session_cookie: str


class _MetaRouter:
    def __init__(self, config: PublishedAppConfig) -> None:
        self._config = config

    def routes(self) -> Sequence[RouteSpec]:
        return (RouteSpec("GET", "/v1/meta", self.meta, "product-meta"),)

    async def meta(self, request: SessionRequest) -> HttpResponse:
        if request.request.body:
            raise ProductHttpError(
                code="REQUEST_BODY_NOT_ALLOWED",
                error_class=HttpErrorClass.SCHEMA,
                path="$",
            )
        return HttpResponse(
            200,
            {
                "schema_version": self._config.schema_version,
                "product_version": self._config.product_version,
                "deployment_id": self._config.deployment_id,
                "limits": dict(self._config.limits),
            },
        )


def bootstrap_admin_session(
    *,
    data_root: Path,
    schema_fragments: Path,
    deployment_id: str,
    organization_id: str,
    limits: Mapping[str, int],
    admin: BootstrapAdmin,
    now: str,
    expires_at: str,
    product_version: str = "RK-PRODUCT-1.1",
) -> BootstrappedDataRoot:
    """Create the initial ADMIN identity and opaque session in a genuinely empty root."""

    root = Path(data_root)
    if root.exists() and (not root.is_dir() or any(root.iterdir())):
        raise ValueError("bootstrap requires an empty data root")
    root.mkdir(parents=True, exist_ok=True)
    os.chmod(root, 0o700)
    cas_root = root / "cas"
    spool_root = root / "spool"
    cas_root.mkdir()
    spool_root.mkdir()
    os.chmod(cas_root, 0o700)
    os.chmod(spool_root, 0o700)
    db_path = root / "product.sqlite"
    migration_root = resource_root() / "migrations"
    MigrationRunner(db_path, migration_root, 5_000).migrate()
    with open_sqlite(db_path) as connection:
        ProductMigrationAssembler(ProductMigrationRegistry(schema_fragments)).apply(connection)
    os.chmod(db_path, 0o600)

    identities = IdentityStore(db_path, lambda: secrets.token_bytes(16))
    identities.register(
        identity_id=admin.identity_id,
        subject_id=admin.subject_id,
        display_name=admin.display_name,
        role=ProductRole.ADMIN,
        capability_id=f"cap-admin-{secrets.token_urlsafe(24)}",
        login_secret=admin.login_secret,
        now=now,
    )
    sessions = SessionStore(
        db_path,
        identities,
        lambda: f"session-{secrets.token_urlsafe(32)}",
        organization_id,
    )
    session = sessions.login(
        identity_id=admin.identity_id,
        login_secret=admin.login_secret,
        now=now,
        expires_at=expires_at,
    )
    config = PublishedAppConfig(
        db_path=db_path,
        cas_root=cas_root,
        spool_root=spool_root,
        deployment_id=deployment_id,
        limits=limits,
        product_version=product_version,
    )
    return BootstrappedDataRoot(
        config=config,
        sessions=sessions,
        session_id=session.session_id,
        session_cookie=f"{config.cookie_name}={session.session_id}",
    )


def build_application(
    *,
    config: PublishedAppConfig,
    sessions: SessionStore,
    clock: Callable[[], str],
    factories: PublishedRouteFactories,
) -> PublishedHttpApplication:
    """Materialize every production router once and freeze one unambiguous app graph."""

    middleware = PublishedSessionMiddleware(
        sessions,
        clock=clock,
        cookie_name=config.cookie_name,
        anonymous_routes=frozenset(
            {
                ("GET", "/v1/meta"),
                ("GET", "/v1/session/options"),
                ("POST", "/v1/session/enter"),
                ("POST", "/v1/session/login"),
            }
        ),
    )
    return PublishedHttpApplication(
        routers=(*factories.materialize(), _MetaRouter(config)),
        middleware=middleware,
    )


__all__ = [
    "BootstrapAdmin",
    "BootstrappedDataRoot",
    "PublishedRouteFactories",
    "bootstrap_admin_session",
    "build_application",
]
