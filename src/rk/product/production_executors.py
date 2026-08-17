"""Production adapters from deployed B services to the 13 durable executor ports."""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, cast

from rk.product.ablation import AblationStore, FrozenAblationConfig
from rk.product.api import ProductCommand, ProductSession, RunScope, frozen_json
from rk.product.artifact_read import ExactArtifactRef
from rk.product.arxiv_batch import ArxivBatchPipeline
from rk.product.authority import ProductAuthority
from rk.product.backup import BackupService
from rk.product.case_import import HistoricalCaseImporter
from rk.product.durable_executors import (
    DurableExecutorPorts,
    domain_success,
    kernel_execution,
    rejected_execution,
    unknown_external_outcome,
)
from rk.product.durable_runtime import DurableExecutor, TypedExecution
from rk.product.jobs import DurableJob
from rk.product.literature_connectors import LiteratureConnector
from rk.product.managed_python import ManagedPythonExecutor, ManagedPythonRequest
from rk.product.production_managed_python import ManagedBindingRejected
from rk.product.publication import PublicationArtifactService
from rk.product.research_lineage import LineageMode, ResearchLineageStore
from rk.product.restore import RestoreRunner
from rk.product.source_snapshots import SourceSnapshotStore
from rk.product.upgrade import UpgradeRunner
from rk.sqlite import open_sqlite

type SessionResolver = Callable[[str], ProductSession]
type ExactArtifactResolver = Callable[[Mapping[str, object]], ExactArtifactRef]
type ManagedRequestResolver = Callable[
    [DurableJob, Mapping[str, object], Any], ManagedPythonRequest
]
type LeaseResolver = Callable[[DurableJob], Any]
type LineageFenceResolver = Callable[[DurableJob, Mapping[str, object]], Mapping[str, object]]
type FrozenAblationResolver = Callable[[DurableJob, Mapping[str, object]], FrozenAblationConfig]
type LifecyclePayloadResolver = Callable[[DurableJob, Mapping[str, object]], Mapping[str, object]]
type HistoricalPlanResolver = Callable[[DurableJob, Mapping[str, object]], Mapping[str, object]]
type FinalizedSnapshotResolver = Callable[[str], Mapping[str, Any]]


@dataclass(frozen=True, slots=True)
class ProductionExecutorDependencies:
    """Production services and deployment-profile resolvers used by real adapters."""

    db_path: Path
    clock: Callable[[], str]
    ids: Callable[[], str]
    snapshots: SourceSnapshotStore | None = None
    literature_connectors: Mapping[str, LiteratureConnector] | None = None
    publication: PublicationArtifactService | None = None
    authority: ProductAuthority | None = None
    sessions: SessionResolver | None = None
    lifecycle_payload: LifecyclePayloadResolver | None = None
    batch_pipeline: ArxivBatchPipeline | None = None
    artifact_ref: ExactArtifactResolver | None = None
    ablation_config: FrozenAblationResolver | None = None
    lineage: ResearchLineageStore | None = None
    lineage_fence: LineageFenceResolver | None = None
    historical_importer: HistoricalCaseImporter | None = None
    historical_plan: HistoricalPlanResolver | None = None
    managed_python: ManagedPythonExecutor | None = None
    managed_request: ManagedRequestResolver | None = None
    active_lease: LeaseResolver | None = None
    backup: BackupService | None = None
    restore: RestoreRunner | None = None
    upgrade: UpgradeRunner | None = None
    configuration_files: Mapping[str, Path] | None = None
    finalized_snapshot: FinalizedSnapshotResolver | None = None
    abstract_resolver: Callable[[DurableJob, Mapping[str, object]], str] | None = None
    unknown_upstream_resolver: DurableExecutor | None = None
    batch_create_port: DurableExecutor | None = None
    assign_ablation_port: DurableExecutor | None = None
    import_lineage_port: DurableExecutor | None = None


def build_production_executor_ports(
    dependencies: ProductionExecutorDependencies,
) -> DurableExecutorPorts:
    """Bind every durable command to a real adapter or a command-specific rejection."""

    live: DurableExecutor = (
        _LiteratureLive(dependencies)
        if dependencies.snapshots is not None and dependencies.literature_connectors
        else _Rejected("LITERATURE_CONNECTOR_CAPABILITY_NOT_DEPLOYED")
    )
    replay: DurableExecutor = (
        _LiteratureReplay(dependencies)
        if dependencies.snapshots is not None
        else _Rejected("SOURCE_SNAPSHOT_REPLAY_NOT_DEPLOYED")
    )
    compile_pdf: DurableExecutor = (
        _CompilePdf(dependencies)
        if dependencies.publication is not None
        else _Rejected("PDF_COMPILER_CAPABILITY_NOT_DEPLOYED")
    )
    return DurableExecutorPorts(
        start_research=_KernelLifecycle(dependencies, "START_RESEARCH"),
        resume_research=_KernelLifecycle(dependencies, "RESUME_RESEARCH"),
        run_literature_query=live,
        replay_source_snapshot=replay,
        batch_create_research=dependencies.batch_create_port or _BatchCreate(dependencies),
        assign_ablation=dependencies.assign_ablation_port or _AssignAblation(dependencies),
        import_research_lineage=(dependencies.import_lineage_port or _ImportLineage(dependencies)),
        create_compute_task=_ManagedCompute(dependencies, "CREATE_COMPUTE_TASK"),
        run_tool=_ManagedCompute(dependencies, "RUN_TOOL"),
        generate_candidate_tex=_GenerateTex(dependencies),
        compile_final_pdf=compile_pdf,
        retry_unknown_outcome=_RetryUnknown(dependencies),
        deployment_operation=_DeploymentOperation(dependencies),
    )


@dataclass(frozen=True, slots=True)
class _KernelLifecycle:
    dependencies: ProductionExecutorDependencies
    command_type: str

    def __call__(self, job: DurableJob, request: Mapping[str, Any]) -> TypedExecution:
        authority = self.dependencies.authority
        sessions = self.dependencies.sessions
        resolver = self.dependencies.lifecycle_payload
        if authority is None or sessions is None or resolver is None:
            return rejected_execution(request=request, code="DURABLE_MANAGED_SESSION_NOT_DEPLOYED")
        command = _product_command(request, payload=resolver(job, _payload(request)))
        if command.command_type != self.command_type:
            raise ValueError("kernel lifecycle command binding differs")
        decision = authority.apply(sessions(job.requested_by), command)
        return kernel_execution(decision=decision)


@dataclass(frozen=True, slots=True)
class _BatchCreate:
    dependencies: ProductionExecutorDependencies

    def __call__(self, job: DurableJob, request: Mapping[str, Any]) -> TypedExecution:
        pipeline = self.dependencies.batch_pipeline
        sessions = self.dependencies.sessions
        resolve_ref = self.dependencies.artifact_ref
        if pipeline is None or sessions is None or resolve_ref is None:
            return rejected_execution(request=request, code="BATCH_EXECUTION_PROFILE_NOT_DEPLOYED")
        payload = _payload(request)
        scope = _scope_object(request)
        template = resolve_ref(_object(payload, "contract_template_artifact"))
        result = pipeline.execute_batch(
            batch_id=job.job_id,
            contract_template_artifact=template,
            owner=job.requested_by,
            labels=_strings(payload, "labels"),
            session=sessions(job.requested_by),
            expected_deployment_revision=_integer(scope, "expected_deployment_revision"),
            now=self.dependencies.clock(),
        )
        return domain_success(
            request=request,
            affected_entity_ids=(result.batch_id, *(run for _, run in result.created_runs)),
            result_refs=(
                frozen_json(
                    {
                        "batch_id": result.batch_id,
                        "state": result.state,
                        "created_runs": [list(item) for item in result.created_runs],
                    }
                ),
            ),
        )


@dataclass(frozen=True, slots=True)
class _AssignAblation:
    dependencies: ProductionExecutorDependencies

    def __call__(self, job: DurableJob, request: Mapping[str, Any]) -> TypedExecution:
        resolver = self.dependencies.ablation_config
        if resolver is None:
            return rejected_execution(request=request, code="ABLATION_PROFILE_NOT_DEPLOYED")
        payload = _payload(request)
        plan_id = _text(payload, "ablation_plan_id")
        config = resolver(job, payload)
        store = AblationStore(self.dependencies.db_path)
        with open_sqlite(self.dependencies.db_path) as connection:
            row = connection.execute(
                "SELECT frozen_digest FROM product_ablation_plans WHERE ablation_plan_id=?",
                (plan_id,),
            ).fetchone()
        if row is None:
            digest = store.freeze(
                ablation_plan_id=plan_id,
                run_id=_run_id(request),
                config=config,
                created_at=self.dependencies.clock(),
            )
        elif str(row[0]) == config.digest:
            digest = config.digest
        else:
            raise ValueError("ablation plan replay drifted from its frozen configuration")
        return domain_success(
            request=request,
            affected_entity_ids=(plan_id,),
            result_refs=(
                frozen_json(
                    {
                        "ablation_plan_id": plan_id,
                        "frozen_digest": digest,
                        "group": _text(payload, "group"),
                    }
                ),
            ),
        )


@dataclass(frozen=True, slots=True)
class _ImportLineage:
    dependencies: ProductionExecutorDependencies

    def __call__(self, job: DurableJob, request: Mapping[str, Any]) -> TypedExecution:
        lineage = self.dependencies.lineage
        fence_resolver = self.dependencies.lineage_fence
        artifact_resolver = self.dependencies.artifact_ref
        sessions = self.dependencies.sessions
        if (
            lineage is None
            or fence_resolver is None
            or artifact_resolver is None
            or sessions is None
        ):
            return rejected_execution(request=request, code="LINEAGE_STORE_NOT_DEPLOYED")
        payload = _payload(request)
        mode = _text(payload, "mode")
        fence = fence_resolver(job, payload)
        if mode == "HISTORICAL_CANDIDATE_MIGRATION":
            return self._historical(job, request, payload, fence)
        manifest_key = (
            "clean_room_input_manifest"
            if mode == "CLEAN_ROOM_REDISCOVERY"
            else "certificate_import_report"
        )
        result = lineage.start_zhao(
            lineage_id=job.job_id,
            mode=LineageMode(mode),
            run_id=_run_id(request),
            contract_version=_contract_version(request),
            frozen_tree_digest=_text(fence, "frozen_tree_digest"),
            data_root_id=_text(fence, "data_root_id"),
            input_manifest=artifact_resolver(_object(payload, manifest_key)),
            created_by_subject_id=sessions(job.requested_by).principal_subject_id,
            now=self.dependencies.clock(),
        )
        return domain_success(
            request=request,
            affected_entity_ids=(result.lineage_id,),
            result_refs=(
                frozen_json(
                    {
                        "lineage_id": result.lineage_id,
                        "mode": result.mode.value,
                        "status": result.status,
                    }
                ),
            ),
        )

    def _historical(
        self,
        job: DurableJob,
        request: Mapping[str, Any],
        payload: Mapping[str, object],
        fence: Mapping[str, object],
    ) -> TypedExecution:
        importer = self.dependencies.historical_importer
        plan_resolver = self.dependencies.historical_plan
        artifact_resolver = self.dependencies.artifact_ref
        sessions = self.dependencies.sessions
        if (
            importer is None
            or plan_resolver is None
            or artifact_resolver is None
            or sessions is None
        ):
            return rejected_execution(request=request, code="N2_AJT5_IMPORT_PROFILE_NOT_DEPLOYED")
        if _text(payload, "source_project_id") != "N2_AJT5":
            return rejected_execution(request=request, code="HISTORICAL_PROJECT_ALIAS_REJECTED")
        plan = plan_resolver(job, payload)
        result = importer.migrate_n2_ajt5(
            lineage_id=job.job_id,
            run_id=_run_id(request),
            contract_version=_contract_version(request),
            kernel_revision=_revision(request),
            frozen_tree_digest=_text(fence, "frozen_tree_digest"),
            data_root_id=_text(fence, "data_root_id"),
            input_manifest=artifact_resolver(_object(plan, "input_manifest")),
            material_inputs=cast(Any, plan["material_inputs"]),
            claim_candidates=cast(Any, plan["claim_candidates"]),
            subject_id=sessions(job.requested_by).principal_subject_id,
            now=self.dependencies.clock(),
        )
        return domain_success(
            request=request,
            affected_entity_ids=(result.lineage_id,),
            result_refs=(
                frozen_json(
                    {
                        "lineage_id": result.lineage_id,
                        "mode": result.mode.value,
                        "status": result.status,
                    }
                ),
            ),
        )


@dataclass(frozen=True, slots=True)
class _ManagedCompute:
    dependencies: ProductionExecutorDependencies
    command_type: str

    def __call__(self, job: DurableJob, request: Mapping[str, Any]) -> TypedExecution:
        executor = self.dependencies.managed_python
        resolver = self.dependencies.managed_request
        lease_resolver = self.dependencies.active_lease
        if executor is None or resolver is None or lease_resolver is None:
            return rejected_execution(request=request, code="MANAGED_PYTHON_PROFILE_NOT_DEPLOYED")
        lease = lease_resolver(job)
        try:
            managed_request = resolver(job, request, lease)
        except ManagedBindingRejected as error:
            return rejected_execution(request=request, code=error.code)
        result = executor.execute(managed_request, lease)
        refs = tuple(
            frozen_json(
                {
                    "artifact_id": item.artifact_id,
                    "sha256": item.sha256,
                    "byte_count": item.byte_count,
                    "media_type": item.media_type,
                }
            )
            for item in result.output_artifacts
        )
        if result.outcome.value != "SUCCEEDED":
            code = (
                result.failure_adjustment.failure_code
                if result.failure_adjustment is not None
                else "MANAGED_EXECUTION_FAILED"
            )
            return rejected_execution(request=request, code=code, result_refs=refs)
        return domain_success(
            request=request,
            affected_entity_ids=(result.execution_id,),
            result_refs=(
                *refs,
                frozen_json(
                    {
                        "public_log_artifact_id": result.public_log_artifact.artifact_id,
                        "authority": "SOFT_TOOL_RESULT_ONLY",
                    }
                ),
            ),
            created_artifact_refs=refs,
        )


@dataclass(frozen=True, slots=True)
class _GenerateTex:
    dependencies: ProductionExecutorDependencies

    def __call__(self, job: DurableJob, request: Mapping[str, Any]) -> TypedExecution:
        publication = self.dependencies.publication
        snapshot_resolver = self.dependencies.finalized_snapshot
        abstract_resolver = self.dependencies.abstract_resolver
        if publication is None or snapshot_resolver is None or abstract_resolver is None:
            return rejected_execution(request=request, code="TEX_RENDER_PROFILE_NOT_DEPLOYED")
        payload = _payload(request)
        run_id = _run_id(request)
        result = publication.render_candidate(
            render_request_id=job.request_id,
            run_id=run_id,
            finalized_snapshot=snapshot_resolver(run_id),
            abstract=abstract_resolver(job, payload),
        )
        candidate = result.candidate_tex_ref
        ref = frozen_json(
            {
                "artifact_id": candidate.artifact_id,
                "sha256": candidate.sha256,
                "byte_count": candidate.byte_count,
                "media_type": candidate.media_type,
            }
        )
        return domain_success(
            request=request,
            affected_entity_ids=(result.render_request_id,),
            result_refs=(ref,),
            created_artifact_refs=(ref,),
        )


@dataclass(frozen=True, slots=True)
class _DeploymentOperation:
    dependencies: ProductionExecutorDependencies

    def __call__(self, job: DurableJob, request: Mapping[str, Any]) -> TypedExecution:
        payload = _payload(request)
        action = _text(payload, "action")
        deployment_id = _deployment_id(request)
        if action == "BACKUP":
            backup_service = self.dependencies.backup
            if backup_service is None:
                return rejected_execution(request=request, code="BACKUP_SERVICE_NOT_DEPLOYED")
            include_configuration = _boolean(payload, "include_configuration")
            receipt = backup_service.create(
                deployment_id=deployment_id,
                request_id=job.request_id,
                include_cas=_boolean(payload, "include_cas"),
                include_configuration=include_configuration,
                configuration_files=(self.dependencies.configuration_files or {})
                if include_configuration
                else {},
            )
            ref = frozen_json(
                {
                    "artifact_id": receipt.artifact.artifact_id,
                    "sha256": receipt.artifact.sha256,
                    "byte_count": receipt.artifact.byte_count,
                    "media_type": receipt.artifact.media_type,
                }
            )
            return domain_success(
                request=request,
                affected_entity_ids=(receipt.backup_id,),
                result_refs=(ref,),
                created_artifact_refs=(ref,),
            )
        if action == "RESTORE":
            restore_service = self.dependencies.restore
            resolver = self.dependencies.artifact_ref
            if restore_service is None or resolver is None:
                return rejected_execution(request=request, code="RESTORE_SERVICE_NOT_DEPLOYED")
            backup_ref = resolver(_object(payload, "backup_artifact"))
            restore_result = restore_service.restore(
                source_backup_id=_backup_id_for_artifact(
                    self.dependencies.db_path, backup_ref.artifact_id
                ),
                backup_artifact=cast(Any, backup_ref),
                deployment_id=deployment_id,
                request_id=job.request_id,
                new_data_root=Path(_text(payload, "new_data_root")),
            )
            return domain_success(
                request=request,
                affected_entity_ids=(restore_result.restore_id,),
                result_refs=(
                    frozen_json(
                        {
                            "restore_id": restore_result.restore_id,
                            "database_sha256": restore_result.restored_database_digest,
                        }
                    ),
                ),
            )
        if action == "UPGRADE_EXECUTE":
            upgrade_service = self.dependencies.upgrade
            if upgrade_service is None:
                return rejected_execution(request=request, code="UPGRADE_SERVICE_NOT_DEPLOYED")
            upgrade_result = upgrade_service.execute(
                deployment_id=deployment_id,
                request_id=job.request_id,
                backup_id=_text(payload, "backup_id"),
            )
            return domain_success(
                request=request,
                affected_entity_ids=(upgrade_result.upgrade_id,),
                result_refs=(
                    frozen_json(
                        {
                            "upgrade_id": upgrade_result.upgrade_id,
                            "manifest_sha256": upgrade_result.release_manifest_digest,
                        }
                    ),
                ),
            )
        if action == "UPGRADE_PREFLIGHT":
            preflight_service = self.dependencies.upgrade
            if preflight_service is None:
                return rejected_execution(request=request, code="UPGRADE_SERVICE_NOT_DEPLOYED")
            preflight = preflight_service.preflight()
            return domain_success(
                request=request,
                affected_entity_ids=(deployment_id,),
                result_refs=(
                    frozen_json(
                        {
                            "release_id": preflight.release_id,
                            "manifest_sha256": preflight.release_manifest_digest,
                            "pending_fragments": list(preflight.pending_fragment_ids),
                        }
                    ),
                ),
            )
        return rejected_execution(request=request, code=f"DEPLOYMENT_{action}_PROFILE_NOT_DEPLOYED")


@dataclass(frozen=True, slots=True)
class _Rejected:
    code: str

    def __call__(self, job: DurableJob, request: Mapping[str, Any]) -> TypedExecution:
        del job
        return rejected_execution(request=request, code=self.code)


@dataclass(frozen=True, slots=True)
class ProductionUnknownUpstreamReconciler:
    """Execute the four formal UNKNOWN dispositions without inventing an outcome."""

    def __call__(self, job: DurableJob, request: Mapping[str, Any]) -> TypedExecution:
        del job
        payload = _payload(request)
        strategy = _text(payload, "resolution_strategy")
        receipt_id = _text(payload, "outcome_unknown_receipt_id")
        external_ref = _text(payload, "unknown_external_call_ref")
        evidence = _strings(payload, "evidence_artifact_ids")
        record = frozen_json(
            {
                "outcome_unknown_receipt_id": receipt_id,
                "unknown_external_call_ref": external_ref,
                "resolution_strategy": strategy,
                "evidence_artifact_ids": list(evidence),
                "authority_effect": "NO_IMPLICIT_MATH_DECISION",
            }
        )
        if strategy == "MARK_ABANDONED":
            return domain_success(
                request=request,
                affected_entity_ids=(receipt_id,),
                result_refs=(record,),
            )
        if strategy == "QUERY_REMOTE":
            return unknown_external_outcome(
                external_call_ref=external_ref,
                result_refs=(record,),
            )
        if strategy == "ACCEPT_RECEIPT":
            return rejected_execution(
                request=request,
                code="UPSTREAM_RECEIPT_VERIFIER_NOT_DEPLOYED",
                result_refs=(record,),
            )
        if strategy == "RETRY":
            return rejected_execution(
                request=request,
                code="UPSTREAM_RETRY_ADAPTER_NOT_DEPLOYED",
                result_refs=(record,),
            )
        return rejected_execution(
            request=request,
            code="UNKNOWN_RESOLUTION_STRATEGY_INVALID",
            result_refs=(record,),
        )


@dataclass(frozen=True, slots=True)
class _LiteratureLive:
    dependencies: ProductionExecutorDependencies

    def __call__(self, job: DurableJob, request: Mapping[str, Any]) -> TypedExecution:
        store = self.dependencies.snapshots
        connectors = self.dependencies.literature_connectors
        if store is None or not connectors:
            return rejected_execution(
                request=request, code="LITERATURE_CONNECTOR_CAPABILITY_NOT_DEPLOYED"
            )
        payload = _payload(request)
        profiles = _strings(payload, "connector_profile_ids")
        query = _text(payload, "query_text")
        tool_run_id, attempt_id = _tool_attempt(self.dependencies.db_path, job.job_id)
        snapshots = []
        refs: list[Mapping[str, Any]] = []
        for profile in profiles:
            try:
                connector = connectors[profile]
            except KeyError:
                return rejected_execution(
                    request=request,
                    code="LITERATURE_CONNECTOR_PROFILE_NOT_DEPLOYED",
                    result_refs=(frozen_json({"connector_profile_id": profile}),),
                )
            snapshot = store.capture_live(
                snapshot_id=self.dependencies.ids(),
                tool_run_id=tool_run_id,
                attempt_id=attempt_id,
                connector=connector,
                request=_connector_request(connector.name, query),
                queried_at=self.dependencies.clock(),
                timeout_seconds=30.0,
            )
            snapshots.append(snapshot.snapshot_id)
            refs.append(
                frozen_json(
                    {
                        "snapshot_id": snapshot.snapshot_id,
                        "mode": snapshot.mode,
                        "connector": snapshot.connector,
                        "result_status": snapshot.result_status,
                        "raw_response": {
                            "artifact_id": snapshot.raw_response.artifact_id,
                            "sha256": snapshot.raw_response.sha256,
                        },
                    }
                )
            )
        return domain_success(
            request=request,
            affected_entity_ids=tuple(snapshots),
            result_refs=tuple(refs),
        )


@dataclass(frozen=True, slots=True)
class _LiteratureReplay:
    dependencies: ProductionExecutorDependencies

    def __call__(self, job: DurableJob, request: Mapping[str, Any]) -> TypedExecution:
        store = self.dependencies.snapshots
        if store is None:
            return rejected_execution(request=request, code="SOURCE_SNAPSHOT_REPLAY_NOT_DEPLOYED")
        payload = _payload(request)
        source_id = _text(payload, "source_snapshot_id")
        source = store.get(source_id)
        if source.raw_response.sha256 != _text(payload, "expected_response_sha256"):
            return rejected_execution(request=request, code="SOURCE_SNAPSHOT_DIGEST_MISMATCH")
        if payload.get("reconfirm_external_index") is True:
            return rejected_execution(
                request=request, code="REPLAY_CANNOT_RECONFIRM_EXTERNAL_INDEX"
            )
        tool_run_id, attempt_id = _tool_attempt(self.dependencies.db_path, job.job_id)
        snapshot = store.replay(
            source_snapshot_id=source_id,
            snapshot_id=self.dependencies.ids(),
            tool_run_id=tool_run_id,
            attempt_id=attempt_id,
            replayed_at=self.dependencies.clock(),
        )
        return domain_success(
            request=request,
            affected_entity_ids=(snapshot.snapshot_id,),
            result_refs=(
                frozen_json(
                    {
                        "snapshot_id": snapshot.snapshot_id,
                        "source_snapshot_id": source_id,
                        "mode": snapshot.mode,
                        "raw_response_sha256": snapshot.raw_response.sha256,
                    }
                ),
            ),
        )


@dataclass(frozen=True, slots=True)
class _CompilePdf:
    dependencies: ProductionExecutorDependencies

    def __call__(self, job: DurableJob, request: Mapping[str, Any]) -> TypedExecution:
        del job
        service = self.dependencies.publication
        if service is None:
            return rejected_execution(request=request, code="PDF_COMPILER_CAPABILITY_NOT_DEPLOYED")
        payload = _payload(request)
        candidate = _object(payload, "candidate_tex_artifact")
        generation_id = _generation_for_candidate(
            self.dependencies.db_path,
            _text(candidate, "artifact_id"),
            _text(candidate, "sha256"),
        )
        result = service.compile_reviewed(
            generation_command_id=generation_id,
            paper_review_id=_text(payload, "paper_review_id"),
        )
        return domain_success(
            request=request,
            affected_entity_ids=(result.compilation_attempt_id,),
            result_refs=(
                frozen_json(
                    {
                        "attempt_id": result.compilation_attempt_id,
                        "pdf_artifact": {
                            "artifact_id": result.pdf_ref.artifact_id,
                            "sha256": result.pdf_ref.sha256,
                            "byte_count": result.pdf_ref.byte_count,
                            "media_type": result.pdf_ref.media_type,
                        },
                        "stdout_log_artifact_id": result.stdout_log_artifact_id,
                        "stderr_log_artifact_id": result.stderr_log_artifact_id,
                    }
                ),
            ),
            created_artifact_refs=(
                frozen_json(
                    {
                        "artifact_id": result.pdf_ref.artifact_id,
                        "sha256": result.pdf_ref.sha256,
                        "byte_count": result.pdf_ref.byte_count,
                        "media_type": result.pdf_ref.media_type,
                    }
                ),
            ),
        )


@dataclass(frozen=True, slots=True)
class _RetryUnknown:
    dependencies: ProductionExecutorDependencies

    def __call__(self, job: DurableJob, request: Mapping[str, Any]) -> TypedExecution:
        payload = _payload(request)
        receipt_id = _text(payload, "outcome_unknown_receipt_id")
        external_ref = _text(payload, "unknown_external_call_ref")
        with open_sqlite(self.dependencies.db_path) as connection:
            row = connection.execute(
                "SELECT state,unknown_external_call_ref,receipt_json "
                "FROM product_receipts WHERE receipt_id=?",
                (receipt_id,),
            ).fetchone()
        if row is None:
            return rejected_execution(request=request, code="OUTCOME_UNKNOWN_RECEIPT_NOT_FOUND")
        if str(row[0]) != "OUTCOME_UNKNOWN" or str(row[1]) != external_ref:
            return rejected_execution(request=request, code="OUTCOME_UNKNOWN_BINDING_MISMATCH")
        upstream = self.dependencies.unknown_upstream_resolver
        if upstream is None:
            return rejected_execution(
                request=request,
                code="UNKNOWN_UPSTREAM_RESOLVER_NOT_DEPLOYED",
                result_refs=(
                    frozen_json(
                        {
                            "outcome_unknown_receipt_id": receipt_id,
                            "unknown_external_call_ref": external_ref,
                            "resolution_strategy": _text(payload, "resolution_strategy"),
                        }
                    ),
                ),
            )
        return upstream(job, request)


def _tool_attempt(path: Path, job_id: str) -> tuple[str, str]:
    with open_sqlite(path) as connection:
        rows = connection.execute(
            "SELECT tool_run_id,attempt_id FROM product_tool_attempts WHERE job_id=?",
            (job_id,),
        ).fetchall()
    if len(rows) != 1:
        raise ValueError("literature job has no exact ToolRun attempt binding")
    return str(rows[0][0]), str(rows[0][1])


def _generation_for_candidate(path: Path, artifact_id: str, digest: str) -> str:
    with open_sqlite(path) as connection:
        rows = connection.execute(
            "SELECT generation_command_id FROM product_publication_candidates "
            "WHERE candidate_tex_artifact_id=? AND candidate_tex_sha256=?",
            (artifact_id, digest),
        ).fetchall()
    if len(rows) != 1:
        raise ValueError("candidate TeX artifact has no unique generation binding")
    return str(rows[0][0])


def _connector_request(name: str, query: str) -> dict[str, object]:
    if name == "OPENALEX":
        return {"query": query, "per_page": 100}
    if name == "CROSSREF":
        return {"query": query, "rows": 100}
    if name == "ARXIV":
        return {"kind": "SEARCH", "query": query, "max_results": 100}
    if name == "MATLAS":
        return {"query": query, "num_results": 100}
    raise ValueError(f"connector has no deployed request adapter: {name}")


def _payload(request: Mapping[str, Any]) -> Mapping[str, object]:
    command = request.get("command")
    if not isinstance(command, Mapping) or not isinstance(command.get("payload"), Mapping):
        raise ValueError("durable command payload is missing")
    return cast(Mapping[str, object], command["payload"])


def _text(value: Mapping[str, object], name: str) -> str:
    item = value.get(name)
    if not isinstance(item, str) or not item.strip():
        raise ValueError(f"{name} must be non-empty text")
    return item


def _object(value: Mapping[str, object], name: str) -> Mapping[str, object]:
    item = value.get(name)
    if not isinstance(item, Mapping):
        raise ValueError(f"{name} must be an object")
    return cast(Mapping[str, object], item)


def _strings(value: Mapping[str, object], name: str) -> tuple[str, ...]:
    item = value.get(name)
    if not isinstance(item, list) or any(not isinstance(part, str) for part in item):
        raise ValueError(f"{name} must be a string array")
    return tuple(cast(list[str], item))


def _integer(value: Mapping[str, object], name: str) -> int:
    item = value.get(name)
    if not isinstance(item, int) or isinstance(item, bool):
        raise ValueError(f"{name} must be an integer")
    return item


def _boolean(value: Mapping[str, object], name: str) -> bool:
    item = value.get(name)
    if not isinstance(item, bool):
        raise ValueError(f"{name} must be a boolean")
    return item


def _scope_object(request: Mapping[str, Any]) -> Mapping[str, object]:
    scope = request.get("scope")
    if not isinstance(scope, Mapping):
        raise ValueError("durable request scope is missing")
    return cast(Mapping[str, object], scope)


def _run_id(request: Mapping[str, Any]) -> str:
    return _text(_scope_object(request), "run_id")


def _deployment_id(request: Mapping[str, Any]) -> str:
    return _text(_scope_object(request), "deployment_id")


def _revision(request: Mapping[str, Any]) -> int:
    return _integer(_scope_object(request), "expected_revision")


def _contract_version(request: Mapping[str, Any]) -> int:
    return _integer(_scope_object(request), "expected_contract_version")


def _product_command(
    request: Mapping[str, Any], *, payload: Mapping[str, object] | None = None
) -> ProductCommand:
    command = request.get("command")
    if not isinstance(command, Mapping):
        raise ValueError("durable command is missing")
    scope_value = _scope_object(request)
    scope = RunScope(
        _text(scope_value, "run_id"),
        _integer(scope_value, "expected_revision"),
        _integer(scope_value, "expected_contract_version"),
    )
    request_id = request.get("request_id")
    command_type = command.get("type")
    if not isinstance(request_id, str) or not isinstance(command_type, str):
        raise ValueError("durable request identity is missing")
    return ProductCommand(
        request_id,
        scope,
        command_type,
        cast(Any, payload if payload is not None else _payload(request)),
    )


def _backup_id_for_artifact(path: Path, artifact_id: str) -> str:
    with open_sqlite(path) as connection:
        rows = connection.execute(
            "SELECT backup_id FROM product_backups "
            "WHERE backup_artifact_id=? AND state='SUCCEEDED'",
            (artifact_id,),
        ).fetchall()
    if len(rows) != 1:
        raise ValueError("backup artifact has no unique successful backup binding")
    return str(rows[0][0])


__all__ = [
    "ProductionExecutorDependencies",
    "build_production_executor_ports",
]
