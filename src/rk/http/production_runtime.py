"""Production-owned composition for the single RK product daemon."""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import signal
import sqlite3
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, cast

from rk.cas import ContentAddressedStore
from rk.config import KernelConfig
from rk.domain import RunSnapshot
from rk.http.app import build_application
from rk.http.daemon_main import ProductHttpDaemon
from rk.http.route_registry import PublishedRouteFactories
from rk.http_shell import (
    SessionPrincipal,
)
from rk.kernel import ResearchKernel
from rk.product.activity_routes import activity_router_factory
from rk.product.activity_store import ActivityStore
from rk.product.adapters import CommandJsonAdapter, ProductHttpCommandAdapter
from rk.product.api import ProductCommand, ProductReceipt, ProductSession, RunScope
from rk.product.artifact_read import ArtifactReadService, ExactArtifactRef
from rk.product.artifact_routes import artifact_router_factory
from rk.product.artifact_upload import ArtifactUploadStore, SQLiteArtifactRegistry
from rk.product.artifact_upload_routes import artifact_upload_router
from rk.product.attestation_import import (
    ArtifactReadContentReader,
    AuthorityEffect,
    HmacAttestationKey,
    HmacKeyringVerifier,
    ReviewAttestationImporter,
    TrustClass,
)
from rk.product.authority import (
    ProductAuthority,
    ResearchKernelRevocationAuthority,
    product_kernel_bindings,
)
from rk.product.backup import BackupService, CasBackupArtifactReader
from rk.product.bridge_opportunities import BridgeOpportunityStore
from rk.product.case_import import HistoricalCaseImporter
from rk.product.claims import ClaimStore
from rk.product.command_routes import command_router_factory
from rk.product.command_service import CommandPlan, ExecutionClass, ProductCommandService
from rk.product.contract_materials import ContractMaterialService
from rk.product.contracts import ContractStore
from rk.product.domain_command_handlers import (
    DomainHandlerServices,
    build_domain_command_authority,
)
from rk.product.domain_commands import IMMEDIATE_COMMAND_SPECS, CommandFence
from rk.product.domain_queries import (
    DomainObjectNotFound,
    DomainQueries,
    DomainQueryScopeMismatch,
    DomainQueryStale,
    FenceSource,
)
from rk.product.durable_executors import (
    DURABLE_COMMAND_TYPES,
    build_durable_executors,
)
from rk.product.durable_runtime import DurableJobPump, DurableJobResolver
from rk.product.graph_index import GraphIndex
from rk.product.graph_query import GraphQueryService
from rk.product.guidance import GuidanceStore
from rk.product.identity import IdentityStore
from rk.product.identity_routes import identity_router
from rk.product.invalidation import AuthorityInvalidationEngine
from rk.product.jobs import DurableJob, JobStore, RetrySafety
from rk.product.listing import ResearchCatalog
from rk.product.literature_connectors import (
    ArxivConnector,
    CrossrefConnector,
    MatlasConnector,
    OpenAlexConnector,
    UrllibTransport,
)
from rk.product.log_tail import PublicLogStore
from rk.product.managed_python import ManagedPythonExecutor, ManagedPythonProfileStore
from rk.product.materials import MaterialStore
from rk.product.operational_queries import OperationalFenceSource, OperationalQueries
from rk.product.operations import OperationStore
from rk.product.problem_pool import ProblemPoolStore
from rk.product.production_executors import (
    ProductionExecutorDependencies,
    ProductionUnknownUpstreamReconciler,
    build_production_executor_ports,
)
from rk.product.production_managed_python import (
    ActiveLeaseResolver,
    PersistentManagedRequestResolver,
)
from rk.product.production_research_ports import build_research_production_ports
from rk.product.publication import PublicationArtifactService
from rk.product.published_app import PublishedAppConfig, PublishedHttpApplication
from rk.product.query_routes import query_router_factory
from rk.product.query_service import (
    ProductQueryService,
    SessionQueryAuthorizer,
    SQLiteQueryFenceSource,
)
from rk.product.receipt_query import ReceiptJobQuery
from rk.product.research_lineage import ResearchLineageStore
from rk.product.research_queries import ResearchQueries
from rk.product.restore import RestoreRunner
from rk.product.review_routes import ReviewInboxIndex, review_router
from rk.product.reviews import ReviewTaskStore
from rk.product.revocation import RevocationService
from rk.product.route_plan import RoutePlanStore
from rk.product.sessions import SessionCapabilitySource, SessionStore
from rk.product.source_snapshots import SourceSnapshotStore
from rk.product.supervisor import RuntimeSupervisor
from rk.product.theorem_applicability import TheoremApplicabilityStore
from rk.product.tool_runs import ToolCatalogStore, ToolRunStore
from rk.product.upgrade import UpgradeRunner
from rk.product.work_activity import WorkActivityStore
from rk.product_release_migrations import ProductReleaseMigrationAssembler
from rk.runtime import SystemClock, Uuid7Generator, format_utc
from rk.storage import SQLiteStorage

_DURABLE_COMMANDS = frozenset(DURABLE_COMMAND_TYPES)

_CORE_QUERY_TYPES = frozenset(
    {
        "LIST_RESEARCH",
        "ACTION_ITEMS",
        "PRODUCT_RECEIPT",
        "JOB",
        "GRAPH_SLICE",
        "GRAPH_SEARCH",
        "DEPENDENCY_CLOSURE",
        "REVERSE_CLOSURE",
    }
)


@dataclass(frozen=True, slots=True)
class ProductionRuntimeConfig:
    data_root: Path
    deployment_id: str
    organization_id: str
    host: str = "127.0.0.1"
    port: int = 8080
    max_upload_bytes: int = 256 * 1024 * 1024
    max_chunk_bytes: int = 4 * 1024 * 1024
    max_log_tail_bytes: int = 1024 * 1024
    busy_timeout_ms: int = 5_000
    review_keys: Mapping[str, HmacAttestationKey] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if not self.deployment_id or not self.organization_id or not self.host:
            raise ValueError("runtime identity and listener values are required")
        if not 0 <= self.port <= 65_535:
            raise ValueError("runtime port is invalid")
        if min(self.max_upload_bytes, self.max_chunk_bytes, self.max_log_tail_bytes) <= 0:
            raise ValueError("runtime byte limits must be positive")
        if not self.review_keys:
            raise ValueError("production runtime requires a named review attestation key")


@dataclass(frozen=True, slots=True)
class ProductionRuntime:
    app: PublishedHttpApplication
    daemon: ProductHttpDaemon
    sessions: SessionStore
    config: PublishedAppConfig
    job_pump: _ManagedReceiptPump | None


@dataclass(frozen=True, slots=True)
class _ManagedReceiptPump:
    inner: DurableJobPump
    managed_python: ManagedPythonExecutor
    clock: Callable[[], str]

    @property
    def kinds(self) -> frozenset[str]:
        return self.inner.kinds

    def run_once(self) -> bool:
        worked = self.inner.run_once()
        self.managed_python.recover_receipts(now=self.clock())
        return worked


class _CommandProduct:
    def __init__(self, service: ProductCommandService) -> None:
        self._service = service

    def command(self, session: ProductSession, request: ProductCommand) -> ProductReceipt:
        return self._service.execute(session, request)


class _CasPublisher:
    def __init__(
        self,
        cas: ContentAddressedStore,
        registry: SQLiteArtifactRegistry,
        clock: SystemClock,
    ) -> None:
        self._cas = cas
        self._registry = registry
        self._clock = clock

    def publish(self, *, data: bytes, logical_name: str, media_type: str) -> ExactArtifactRef:
        del logical_name
        committed = self._cas.commit(
            self._cas.stage_bytes(data, media_type=media_type), now=self._clock.now()
        )
        ref = self._registry.register(committed)
        return ExactArtifactRef(ref.artifact_id, ref.sha256, ref.byte_count, ref.media_type)


def _session_resolver(
    db_path: Path,
    sessions: SessionStore,
    clock: Callable[[], str],
    ids: Callable[[], str],
    organization_id: str,
) -> Callable[[str], ProductSession]:
    def resolve(subject_id: str) -> ProductSession:
        now = clock()
        with sqlite3.connect(db_path) as connection:
            connection.execute("PRAGMA foreign_keys = ON")
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT identity_id FROM product_identities WHERE subject_id=? AND enabled=1",
                (subject_id,),
            ).fetchone()
            if row is None:
                raise ValueError("durable requester identity is not enabled")
            identity_id = str(row[0])
            managed = connection.execute(
                "SELECT session.session_id FROM product_managed_sessions AS managed "
                "JOIN product_sessions AS session ON session.session_id=managed.session_id "
                "WHERE managed.identity_id=? AND session.revoked_at IS NULL",
                (identity_id,),
            ).fetchone()
            if managed is None:
                session_id = f"managed-{ids()}"
                connection.execute(
                    "INSERT INTO product_sessions("
                    "session_id,organization_id,active_identity_id,session_version,"
                    "issued_at,expires_at) VALUES(?,?,?,?,?,?)",
                    (
                        session_id,
                        organization_id,
                        identity_id,
                        1,
                        now,
                        "9999-12-31T23:59:59Z",
                    ),
                )
                connection.execute(
                    "INSERT INTO product_session_identities VALUES(?,?,?)",
                    (session_id, identity_id, now),
                )
                connection.execute(
                    "INSERT INTO product_managed_sessions VALUES(?,?,?)",
                    (session_id, identity_id, now),
                )
            else:
                session_id = str(managed[0])
            connection.commit()
        resolved = sessions.derive(session_id, now=now)
        if resolved.principal_subject_id != subject_id:
            raise ValueError("durable requester session principal changed")
        return resolved

    return resolve


def _lifecycle_payload(job: DurableJob, payload: Mapping[str, object]) -> Mapping[str, object]:
    required = {
        "START_RESEARCH": {"contract_version", "literature_plan_artifact_id", "budget_policy"},
        "RESUME_RESEARCH": {"checkpoint_artifact_id", "lease_preflight", "budget_preflight"},
    }
    expected = required.get(job.kind)
    if expected is None or set(payload) != expected:
        raise ValueError("durable lifecycle payload differs from its frozen kernel binding")
    return dict(payload)


def _artifact_resolver(
    artifacts: ArtifactReadService,
) -> Callable[[Mapping[str, object]], ExactArtifactRef]:
    def resolve(value: Mapping[str, object]) -> ExactArtifactRef:
        required = {"artifact_id", "sha256", "byte_count", "media_type"}
        if set(value) != required:
            raise ValueError("ArtifactRef fields are not exact")
        artifact_id = value["artifact_id"]
        if not isinstance(artifact_id, str):
            raise ValueError("ArtifactRef identity is invalid")
        actual = artifacts.describe(artifact_id).ref
        if (
            actual.sha256 != value["sha256"]
            or actual.byte_count != value["byte_count"]
            or actual.media_type != value["media_type"]
        ):
            raise ValueError("ArtifactRef metadata differs from CAS authority")
        return actual

    return resolve


def _finalized_snapshot(kernel: ResearchKernel) -> Callable[[str], Mapping[str, Any]]:
    def resolve(run_id: str) -> Mapping[str, Any]:
        snapshot = kernel.inspect(run_id)
        if not isinstance(snapshot, RunSnapshot):
            raise ValueError("finalized snapshot lookup returned an event page")
        return snapshot.to_dict()

    return resolve


def _publication_abstract(
    db_path: Path,
) -> Callable[[DurableJob, Mapping[str, object]], str]:
    def resolve(job: DurableJob, payload: Mapping[str, object]) -> str:
        del payload
        if job.run_id is None:
            raise ValueError("publication job requires RUN scope")
        with sqlite3.connect(db_path) as connection:
            row = connection.execute(
                "SELECT question_summary FROM research_catalog WHERE run_id=?",
                (job.run_id,),
            ).fetchone()
        if row is None or not str(row[0]).strip():
            raise ValueError("publication abstract source is unavailable")
        return str(row[0])

    return resolve


class _ProductionCommandFences:
    def __init__(self, source: SQLiteQueryFenceSource, deployment_id: str) -> None:
        self._source = source
        self._deployment_id = deployment_id

    def current(self, request: ProductCommand) -> CommandFence:
        scope = request.scope
        if isinstance(scope, RunScope):
            run_id = scope.run_id
            fence = self._source.run(run_id)
            return CommandFence("RUN", run_id, fence.research_revision, fence.contract_version)
        catalog = self._source.catalog()
        return CommandFence(scope.kind, self._deployment_id, catalog.catalog_revision, 0)


class _ProductionQueries(ProductQueryService):
    """Add mature domain projections without weakening the existing query fences."""

    def __init__(
        self,
        *,
        domain: DomainQueries,
        research: ResearchQueries,
        operational: OperationalQueries,
        authorizer: SessionQueryAuthorizer,
        **kwargs: Any,
    ) -> None:
        super().__init__(authorizer=authorizer, **kwargs)
        self._domain = domain
        self._research = research
        self._operational = operational
        self._domain_authorizer = authorizer

    def execute(self, session: ProductSession, spec: Any) -> Any:
        self._domain_authorizer.authorize(session, spec.scope)
        if self._research.supports(spec.query_type):
            try:
                return self._research.execute(session, spec)
            except DomainObjectNotFound as error:
                from rk.product.query_service import QueryObjectNotFound

                raise QueryObjectNotFound(str(error)) from error
            except DomainQueryScopeMismatch as error:
                from rk.product.query_service import QueryScopeDenied

                raise QueryScopeDenied(str(error)) from error
            except DomainQueryStale as error:
                from rk.product.graph_query import StaleQuery

                raise StaleQuery(str(error)) from error
        if not self._domain.supports(spec.query_type):
            if self._operational.supports(spec.query_type):
                try:
                    return self._operational.execute(session, spec)
                except DomainObjectNotFound as error:
                    from rk.product.query_service import QueryObjectNotFound

                    raise QueryObjectNotFound(str(error)) from error
                except DomainQueryScopeMismatch as error:
                    from rk.product.query_service import QueryScopeDenied

                    raise QueryScopeDenied(str(error)) from error
                except DomainQueryStale as error:
                    from rk.product.graph_query import StaleQuery

                    raise StaleQuery(str(error)) from error
            return super().execute(session, spec)
        try:
            return self._domain.execute(spec)
        except DomainObjectNotFound as error:
            from rk.product.query_service import QueryObjectNotFound

            raise QueryObjectNotFound(str(error)) from error
        except DomainQueryScopeMismatch as error:
            from rk.product.query_service import QueryScopeDenied

            raise QueryScopeDenied(str(error)) from error
        except DomainQueryStale as error:
            from rk.product.graph_query import StaleQuery

            raise StaleQuery(str(error)) from error


class _AuthenticatedAccess:
    @staticmethod
    def _require(principal: SessionPrincipal) -> None:
        if not principal.session_id or not principal.subject_id or not principal.capability_ids:
            raise PermissionError("authenticated session is required")

    def authorize_subscription(self, principal: SessionPrincipal, run_id: str) -> None:
        self._require(principal)
        if not run_id:
            raise PermissionError("run scope is required")

    def authorize_artifact(self, principal: SessionPrincipal, descriptor: Any) -> None:
        self._require(principal)

    def authorize_log(self, principal: SessionPrincipal, log: Any) -> None:
        self._require(principal)


class _SQLiteReviewInbox(ReviewInboxIndex):
    def __init__(self, db_path: Path) -> None:
        self._db_path = db_path

    def task_ids_for_assignee(self, assignee_identity_id: str) -> Sequence[str]:
        with sqlite3.connect(self._db_path) as connection:
            rows = connection.execute(
                "SELECT review_task_id FROM product_review_tasks "
                "WHERE assignee_identity_id=? "
                "AND status IN ('OPEN','CLAIMED','REASSIGNED') "
                "ORDER BY expires_at,review_task_id",
                (assignee_identity_id,),
            ).fetchall()
        return tuple(str(row[0]) for row in rows)


def _assert_query_catalog(
    schema_path: Path,
    domain: DomainQueries,
    research: ResearchQueries,
    operational: OperationalQueries,
) -> None:
    value = json.loads(schema_path.read_text(encoding="utf-8"))
    definitions = value.get("$defs")
    if not isinstance(definitions, dict):
        raise ValueError("query schema has no definitions")
    query_spec = definitions.get("querySpec")
    if not isinstance(query_spec, dict) or not isinstance(query_spec.get("oneOf"), list):
        raise ValueError("query schema has no querySpec variants")
    variants: set[str] = set()
    for branch in query_spec["oneOf"]:
        if not isinstance(branch, dict):
            raise ValueError("query schema branch is invalid")
        query = branch.get("properties", {}).get("query", {})
        reference = query.get("$ref") if isinstance(query, dict) else None
        definition = definitions.get(str(reference).rsplit("/", 1)[-1])
        if not isinstance(definition, dict):
            raise ValueError("query schema branch reference is invalid")
        properties = definition.get("properties")
        query_type = (
            properties.get("type", {}).get("const") if isinstance(properties, dict) else None
        )
        if not isinstance(query_type, str):
            raise ValueError("query schema has an invalid variant")
        variants.add(query_type)
    for query_type in variants:
        owners = sum(
            (
                query_type in _CORE_QUERY_TYPES,
                domain.supports(query_type),
                research.supports(query_type),
                operational.supports(query_type),
            )
        )
        if owners != 1:
            raise ValueError(f"query variant has {owners} production owners: {query_type}")
    if len(variants) != 56:
        raise ValueError(f"query contract contains {len(variants)} variants, expected 56")


def build_production_runtime(config: ProductionRuntimeConfig) -> ProductionRuntime:
    """Materialize one real route graph over one durable SQLite/CAS data root."""

    root = config.data_root.resolve()
    db_path = root / "product.sqlite"
    if not db_path.is_file():
        raise ValueError("production data root is not bootstrapped")
    cas_root, spool_root = root / "cas", root / "spool"
    cas_root.mkdir(parents=True, exist_ok=True)
    spool_root.mkdir(parents=True, exist_ok=True)
    ids = Uuid7Generator()
    clock = SystemClock()

    def now() -> str:
        return format_utc(clock.now())

    identities = IdentityStore(db_path, lambda: os.urandom(16))
    sessions = SessionStore(db_path, identities, ids.new, config.organization_id)
    storage = SQLiteStorage(db_path, config.busy_timeout_ms)
    registry = SQLiteArtifactRegistry(storage)
    cas = ContentAddressedStore(
        cas_root,
        config.max_upload_bytes,
        (spool_root,),
        86_400,
        ids,
        artifact_lookup=storage.get_artifact,
    )
    uploads = ArtifactUploadStore(
        db_path=db_path,
        spool_root=spool_root / "uploads",
        cas=cas,
        registry=registry,
        id_generator=ids.new,
        clock=clock.now,
        max_upload_bytes=config.max_upload_bytes,
        max_chunk_bytes=config.max_chunk_bytes,
        busy_timeout_ms=config.busy_timeout_ms,
    )
    logs = PublicLogStore(
        db_path=db_path,
        cas=cas,
        registry=registry,
        spool_root=spool_root / "logs",
        id_generator=ids.new,
        clock=clock.now,
        max_chunk_bytes=config.max_chunk_bytes,
        max_tail_bytes=config.max_log_tail_bytes,
        busy_timeout_ms=config.busy_timeout_ms,
    )
    access = _AuthenticatedAccess()
    upload_router = artifact_upload_router(
        uploads=uploads, authorize=lambda principal, operation: access._require(principal)
    )
    artifact_reader = ArtifactReadService(metadata=storage, cas_root=cas_root)
    artifact_router = artifact_router_factory(
        artifacts=artifact_reader,
        logs=logs,
        authorizer=access,
        other_operations=upload_router.handle,
    )
    review_tasks = ReviewTaskStore(db_path, identities, busy_timeout_ms=config.busy_timeout_ms)
    review_importer = ReviewAttestationImporter(
        tasks=review_tasks,
        artifacts=ArtifactReadContentReader(artifact_reader),
        signatures=HmacKeyringVerifier(config.review_keys),
        review_schema_path=Path(__file__).resolve().parents[3]
        / "docs/spec/product/review.schema.json",
    )
    reviews = review_router(
        sessions=sessions,
        tasks=review_tasks,
        importer=review_importer,
        inbox=_SQLiteReviewInbox(db_path),
        clock=now,
    )
    jobs = JobStore(db_path, ids.new, busy_timeout_ms=config.busy_timeout_ms)
    supervisor = RuntimeSupervisor(
        store=jobs,
        holder_id=f"daemon:{config.deployment_id}",
        clock=now,
        retry_policy={name: RetrySafety.MANUAL_ONLY for name in _DURABLE_COMMANDS},
    )
    repository_root = Path(__file__).resolve().parents[3]
    kernel = ResearchKernel.from_config(
        KernelConfig.from_mapping(
            {
                "workspace_root": root,
                "db_path": db_path,
                "cas_root": cas_root,
                "inbox_roots": [spool_root],
                "command_schema_path": repository_root / "docs/spec/json/command.schema.json",
                "receipt_schema_path": repository_root / "docs/spec/json/receipt.schema.json",
                "max_artifact_bytes": config.max_upload_bytes,
            },
            base=repository_root,
        )
    )
    activities = ActivityStore(db_path)
    route_store = RoutePlanStore(
        db_path=db_path,
        activities=activities,
        id_generator=ids.new,
        clock=now,
        busy_timeout_ms=config.busy_timeout_ms,
    )
    fences = SQLiteQueryFenceSource(db_path, config.deployment_id)
    capabilities = SessionCapabilitySource(sessions, now)
    kernel_authority = ProductAuthority(kernel, capabilities, product_kernel_bindings())
    publisher = _CasPublisher(cas, registry, clock)
    tool_catalog = ToolCatalogStore(db_path, busy_timeout_ms=config.busy_timeout_ms)
    tool_runs = ToolRunStore(db_path, jobs, busy_timeout_ms=config.busy_timeout_ms)
    managed_profiles = ManagedPythonProfileStore(
        db_path, artifact_reader, busy_timeout_ms=config.busy_timeout_ms
    )
    managed_python = ManagedPythonExecutor(
        db_path=db_path,
        workspace_root=root / "managed-python",
        artifacts=artifact_reader,
        publisher=publisher,
        profiles=managed_profiles,
        jobs=jobs,
        tool_runs=tool_runs,
        clock=now,
        busy_timeout_ms=config.busy_timeout_ms,
        defer_b03_resolution=True,
    )
    managed_python.recover_receipts(now=now())
    managed_python.abandon_orphaned_processes(now=now())
    managed_request = PersistentManagedRequestResolver(
        jobs=jobs,
        catalog=tool_catalog,
        tool_runs=tool_runs,
        profiles=managed_profiles,
        artifacts=artifact_reader,
        clock=now,
    )
    invalidations = AuthorityInvalidationEngine(
        db_path, now, busy_timeout_ms=config.busy_timeout_ms
    )
    contracts = ContractStore(db_path, busy_timeout_ms=config.busy_timeout_ms)
    materials = MaterialStore(
        db_path=db_path,
        artifacts=artifact_reader,
        publisher=publisher,
        tool_runs=tool_runs,
        busy_timeout_ms=config.busy_timeout_ms,
    )
    contract_materials = ContractMaterialService(
        db_path=db_path,
        contracts=contracts,
        materials=materials,
        invalidation=invalidations,
        busy_timeout_ms=config.busy_timeout_ms,
    )
    guidance = GuidanceStore(
        db_path=db_path,
        activities=activities,
        event_id_generator=ids.new,
        clock=now,
        busy_timeout_ms=config.busy_timeout_ms,
    )
    problem_pools = ProblemPoolStore(db_path, busy_timeout_ms=config.busy_timeout_ms)
    authority = build_domain_command_authority(
        capabilities=capabilities,
        fences=_ProductionCommandFences(fences, config.deployment_id),
        services=DomainHandlerServices(
            db_path=db_path,
            kernel=kernel_authority,
            artifacts=artifact_reader,
            publisher=publisher,
            routes=route_store,
            guidance=guidance,
            contracts=contracts,
            contract_materials=contract_materials,
            materials=materials,
            review_tasks=review_tasks,
            review_importer=review_importer,
            theorem_applicability=TheoremApplicabilityStore(db_path),
            problem_pool=problem_pools,
            bridge_opportunities=BridgeOpportunityStore(db_path),
            revocations=RevocationService(
                db_path,
                ResearchKernelRevocationAuthority(kernel),
                invalidations,
                ids.new,
                now,
                busy_timeout_ms=config.busy_timeout_ms,
            ),
            jobs=jobs,
            catalog=ResearchCatalog(db_path),
            clock=now,
            ids=ids.new,
        ),
    )
    snapshots = SourceSnapshotStore(
        db_path=db_path,
        artifacts=artifact_reader,
        publisher=publisher,
        tool_runs=tool_runs,
        busy_timeout_ms=config.busy_timeout_ms,
    )
    publication = PublicationArtifactService(
        db_path=db_path,
        cas=cas,
        registry=registry,
        artifacts=artifact_reader,
        review_tasks=review_tasks,
        logs=logs,
        id_generator=ids.new,
        clock=clock.now,
        busy_timeout_ms=config.busy_timeout_ms,
    )
    backup = BackupService(
        db_path=db_path,
        cas_root=cas_root,
        work_root=root / "backups",
        cas=cas,
        registry=registry,
        id_generator=ids.new,
        clock=clock.now,
        busy_timeout_ms=config.busy_timeout_ms,
    )
    release = ProductReleaseMigrationAssembler(
        fragment_root=repository_root / "schema_fragments",
        manifest_path=repository_root / "packaging/release-manifest.json",
        lock_path=repository_root / "migrations/release/current.lock",
    )
    restore = RestoreRunner(
        tracking_db_path=db_path,
        artifact_reader=CasBackupArtifactReader(cas),
        release=release,
        id_generator=ids.new,
        clock=clock.now,
        busy_timeout_ms=config.busy_timeout_ms,
    )
    upgrade = UpgradeRunner(
        db_path=db_path,
        release=release,
        id_generator=ids.new,
        clock=clock.now,
        busy_timeout_ms=config.busy_timeout_ms,
    )
    managed_sessions = _session_resolver(db_path, sessions, now, ids.new, config.organization_id)
    lineage = ResearchLineageStore(
        db_path=db_path,
        artifacts=artifact_reader,
        busy_timeout_ms=config.busy_timeout_ms,
    )
    research_ports = build_research_production_ports(
        db_path=db_path,
        clock=now,
        artifacts=artifact_reader,
        problem_pools=problem_pools,
        sessions=managed_sessions,
        lineage=lineage,
        historical_importer=HistoricalCaseImporter(
            db_path=db_path,
            artifacts=artifact_reader,
            lineage=lineage,
            materials=materials,
            contracts=contracts,
            claims=ClaimStore(
                db_path,
                ids.new,
                now,
                busy_timeout_ms=config.busy_timeout_ms,
            ),
            busy_timeout_ms=config.busy_timeout_ms,
        ),
        snapshots=snapshots,
    )
    durable_executors = build_durable_executors(
        build_production_executor_ports(
            ProductionExecutorDependencies(
                db_path=db_path,
                clock=now,
                ids=ids.new,
                snapshots=snapshots,
                literature_connectors={
                    "ARXIV": ArxivConnector(UrllibTransport()),
                    "CROSSREF": CrossrefConnector(UrllibTransport()),
                    "MATLAS": MatlasConnector(UrllibTransport()),
                    "OPENALEX": OpenAlexConnector(UrllibTransport()),
                },
                publication=publication,
                authority=kernel_authority,
                sessions=managed_sessions,
                lifecycle_payload=_lifecycle_payload,
                artifact_ref=_artifact_resolver(artifact_reader),
                managed_python=managed_python,
                managed_request=managed_request,
                active_lease=ActiveLeaseResolver(db_path),
                lineage=lineage,
                backup=backup,
                restore=restore,
                upgrade=upgrade,
                configuration_files={},
                finalized_snapshot=_finalized_snapshot(kernel),
                abstract_resolver=_publication_abstract(db_path),
                unknown_upstream_resolver=ProductionUnknownUpstreamReconciler(),
                batch_create_port=research_ports.batch_create,
                assign_ablation_port=research_ports.assign_ablation,
                import_lineage_port=research_ports.import_lineage,
            )
        )
    )
    plans = {name: CommandPlan(ExecutionClass.DURABLE_JOB, name) for name in durable_executors}
    plans.update(
        {
            name: CommandPlan(ExecutionClass.SYNCHRONOUS_AUTHORITY)
            for name in IMMEDIATE_COMMAND_SPECS
        }
    )
    commands = ProductCommandService(
        operations=OperationStore(db_path, ids.new, busy_timeout_ms=config.busy_timeout_ms),
        authority=authority,
        jobs=supervisor,
        plans=plans,
        id_generator=ids.new,
        clock=now,
        authorizer=capabilities,
    )
    research_ports.commands.bind(commands)
    job_pump = (
        _ManagedReceiptPump(
            DurableJobPump(
                supervisor=supervisor,
                jobs=jobs,
                resolver=DurableJobResolver(
                    db_path, ids.new, busy_timeout_ms=config.busy_timeout_ms
                ),
                executors=durable_executors,
                clock=now,
                process_tokens=ids.new,
            ),
            managed_python,
            now,
        )
        if durable_executors
        else None
    )
    graph = GraphQueryService(
        db_path,
        GraphIndex(db_path, clock=now),
        cursor_secret=hashlib.sha256((config.deployment_id + ":graph").encode()).digest(),
        busy_timeout_ms=config.busy_timeout_ms,
    )
    operations = OperationStore(db_path, ids.new, busy_timeout_ms=config.busy_timeout_ms)
    query_authorizer = SessionQueryAuthorizer(
        sessions, deployment_id=config.deployment_id, clock=now
    )
    domain_queries = DomainQueries(
        db_path=db_path,
        deployment_id=config.deployment_id,
        fences=cast(FenceSource, fences),
        route_plans=route_store,
        work=WorkActivityStore(
            db_path=db_path,
            activities=activities,
            id_generator=ids.new,
            clock=now,
            busy_timeout_ms=config.busy_timeout_ms,
        ),
        tool_catalog=tool_catalog,
        tool_runs=tool_runs,
        problem_pools=problem_pools,
        lineages=lineage,
        cursor_secret=hashlib.sha256((config.deployment_id + ":query").encode()).digest(),
        busy_timeout_ms=config.busy_timeout_ms,
    )
    research_queries = ResearchQueries(
        db_path=db_path,
        fences=cast(FenceSource, fences),
        cursor_secret=hashlib.sha256((config.deployment_id + ":research-query").encode()).digest(),
        busy_timeout_ms=config.busy_timeout_ms,
    )
    operational_queries = OperationalQueries(
        db_path=db_path,
        deployment_id=config.deployment_id,
        fences=cast(OperationalFenceSource, fences),
        cursor_secret=hashlib.sha256(
            (config.deployment_id + ":operational-query").encode()
        ).digest(),
        busy_timeout_ms=config.busy_timeout_ms,
    )
    _assert_query_catalog(
        repository_root / "docs/spec/product/query.schema.json",
        domain_queries,
        research_queries,
        operational_queries,
    )
    queries = _ProductionQueries(
        domain=domain_queries,
        research=research_queries,
        operational=operational_queries,
        catalog=ResearchCatalog(db_path),
        receipt_jobs=ReceiptJobQuery(operations, jobs),
        graph=graph,
        fences=fences,
        authorizer=query_authorizer,
        cursor_secret=hashlib.sha256((config.deployment_id + ":query").encode()).digest(),
    )
    published = PublishedAppConfig(
        db_path=db_path,
        cas_root=cas_root,
        spool_root=spool_root,
        deployment_id=config.deployment_id,
        limits={
            "upload_bytes": config.max_upload_bytes,
            "upload_chunk_bytes": config.max_chunk_bytes,
            "log_tail_bytes": config.max_log_tail_bytes,
            "graph_nodes": 200,
        },
    )

    def expires_at(value: str) -> str:
        current = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return format_utc(current + timedelta(hours=12))

    identity = identity_router(
        sessions=sessions, clock=now, expires_at=expires_at, secure_cookie=False
    )
    factories = PublishedRouteFactories(
        command=lambda: command_router_factory(
            adapter=ProductHttpCommandAdapter(CommandJsonAdapter(_CommandProduct(commands))),
            deployment_id=config.deployment_id,
        ),
        query=lambda: query_router_factory(service=queries, deployment_id=config.deployment_id),
        activity=lambda: activity_router_factory(
            db_path=db_path, store=ActivityStore(db_path), authorizer=access, clock=now
        ),
        artifact=lambda: artifact_router,
        session=lambda: identity,
        admin=lambda: None,
        review=lambda: reviews,
    )
    app = build_application(config=published, sessions=sessions, clock=now, factories=factories)
    daemon = ProductHttpDaemon(
        app=app, deployment_id=config.deployment_id, host=config.host, port=config.port
    )
    return ProductionRuntime(app, daemon, sessions, published, job_pump)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="rk-product-daemon")
    parser.add_argument("--data-root", type=Path, required=True)
    parser.add_argument("--deployment-id", required=True)
    parser.add_argument("--organization-id", required=True)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    parser.add_argument("--review-key-id", default="production-review")
    parser.add_argument("--reviewer-identity-id", required=True)
    args = parser.parse_args(argv)
    review_secret = os.environ.get("RK_REVIEW_HMAC_SECRET", "").encode()
    if len(review_secret) < 32:
        parser.error("RK_REVIEW_HMAC_SECRET must contain at least 32 bytes")
    runtime = build_production_runtime(
        ProductionRuntimeConfig(
            args.data_root,
            args.deployment_id,
            args.organization_id,
            args.host,
            args.port,
            review_keys={
                args.review_key_id: HmacAttestationKey(
                    secret=review_secret,
                    verifier_identity_id=args.reviewer_identity_id,
                    trust_class=TrustClass.MANAGED_PEER_REVIEW,
                    authority_effect=AuthorityEffect.PEER_PROMOTION_ELIGIBLE,
                    promotion_eligible=True,
                )
            },
        )
    )
    stopped = threading.Event()
    signal.signal(signal.SIGTERM, lambda *_: stopped.set())
    signal.signal(signal.SIGINT, lambda *_: stopped.set())
    runtime.daemon.start()
    host, port = runtime.daemon.address
    print(f"RK product daemon listening on http://{host}:{port}", flush=True)
    pump_errors: list[BaseException] = []
    pump_thread: threading.Thread | None = None
    pump = runtime.job_pump
    if pump is not None:

        def pump_jobs() -> None:
            try:
                while not stopped.is_set():
                    if not pump.run_once():
                        stopped.wait(0.2)
            except BaseException as error:
                pump_errors.append(error)
                stopped.set()

        pump_thread = threading.Thread(target=pump_jobs, name="rk-product-jobs")
        pump_thread.start()
    try:
        stopped.wait()
    finally:
        runtime.daemon.stop()
        if pump_thread is not None:
            pump_thread.join()
    if pump_errors:
        raise RuntimeError("durable job pump stopped") from pump_errors[0]
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
