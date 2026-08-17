"""Production-owned durable ports for batches, ablations, and research lineage."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol, cast

from rk.product.ablation import AblationStore, FrozenAblationConfig
from rk.product.api import ProductCommand, ProductReceipt, ProductSession, frozen_json
from rk.product.artifact_read import (
    ArtifactReadError,
    ArtifactReadService,
    ExactArtifactRef,
)
from rk.product.arxiv_batch import ArxivBatchError, ArxivBatchPipeline
from rk.product.case_import import (
    HistoricalCaseImporter,
    HistoricalClaimCandidate,
    HistoricalMaterialInput,
)
from rk.product.claims import ClaimKind
from rk.product.durable_executors import domain_success, rejected_execution
from rk.product.durable_runtime import DurableExecutor, TypedExecution
from rk.product.jobs import DurableJob
from rk.product.problem_pool import ProblemPoolStore
from rk.product.research_lineage import (
    LineageMode,
    ResearchLineageError,
    ResearchLineageStore,
)
from rk.product.source_snapshots import SourceSnapshotStore
from rk.sqlite import open_sqlite
from rk.wire import canonical_json_bytes


class _CommandService(Protocol):
    def execute(self, session: ProductSession, request: ProductCommand) -> ProductReceipt: ...


class CommandProductPort:
    """Late binding that breaks the command-service/batch-executor construction cycle."""

    def __init__(self) -> None:
        self._delegate: _CommandService | None = None

    def bind(self, delegate: _CommandService) -> None:
        if self._delegate is not None:
            raise RuntimeError("durable command product is already bound")
        self._delegate = delegate

    def command(self, session: ProductSession, request: ProductCommand) -> ProductReceipt:
        delegate = self._delegate
        if delegate is None:
            raise RuntimeError("durable command product is not bound")
        return delegate.execute(session, request)


@dataclass(frozen=True, slots=True)
class ResearchProductionPorts:
    batch_create: DurableExecutor
    assign_ablation: DurableExecutor
    import_lineage: DurableExecutor
    commands: CommandProductPort


def build_research_production_ports(
    *,
    db_path: Path,
    clock: Callable[[], str],
    artifacts: ArtifactReadService,
    problem_pools: ProblemPoolStore,
    sessions: Callable[[str], ProductSession],
    lineage: ResearchLineageStore,
    historical_importer: HistoricalCaseImporter,
    snapshots: SourceSnapshotStore,
) -> ResearchProductionPorts:
    commands = CommandProductPort()
    pipeline = ArxivBatchPipeline(
        db_path=db_path,
        pools=problem_pools,
        snapshots=snapshots,
        artifacts=artifacts,
        commands=commands,
    )
    common = _Dependencies(
        Path(db_path), clock, artifacts, problem_pools, sessions, lineage, historical_importer
    )
    return ResearchProductionPorts(
        _BatchPort(common, pipeline),
        _AblationPort(common),
        _LineagePort(common),
        commands,
    )


@dataclass(frozen=True, slots=True)
class _Dependencies:
    db_path: Path
    clock: Callable[[], str]
    artifacts: ArtifactReadService
    problem_pools: ProblemPoolStore
    sessions: Callable[[str], ProductSession]
    lineage: ResearchLineageStore
    historical_importer: HistoricalCaseImporter


class _EvidenceRejected(RuntimeError):
    def __init__(self, code: str) -> None:
        super().__init__(code)
        self.code = code


@dataclass(frozen=True, slots=True)
class _BatchPort:
    d: _Dependencies
    pipeline: ArxivBatchPipeline

    def __call__(self, job: DurableJob, request: Mapping[str, Any]) -> TypedExecution:
        try:
            payload = _payload(request)
            scope = _scope(request, "GLOBAL")
            pool_id = _text(payload, "problem_pool_id")
            candidate_ids = _strings(payload, "problem_candidate_ids")
            template = _artifact(self.d.artifacts, _object(payload, "contract_template_artifact"))
            pool = self.d.problem_pools.get(pool_id)
            if pool.state != "FROZEN":
                raise _EvidenceRejected("BATCH_PROBLEM_POOL_NOT_FROZEN")
            bindings = {
                item.binding_kind: item for item in self.d.problem_pools.artifact_bindings(pool_id)
            }
            if "SEMANTIC_AUDIT" not in bindings:
                raise _EvidenceRejected("BATCH_SEMANTIC_AUDIT_ARTIFACT_MISSING")
            template_binding = bindings.get("CONTRACT_TEMPLATE")
            if template_binding is None:
                raise _EvidenceRejected("BATCH_CONTRACT_TEMPLATE_NOT_FROZEN")
            if template_binding.artifact != template:
                raise _EvidenceRejected("BATCH_CONTRACT_TEMPLATE_BINDING_MISMATCH")
            candidates = tuple(self.d.problem_pools.get_candidate(item) for item in candidate_ids)
            if not candidates or any(
                item.problem_pool_id != pool_id
                or item.audit_status != "HUMAN_INCLUDED"
                or item.recommendation_status != "RECOMMENDED"
                or item.created_run_id is not None
                for item in candidates
            ):
                raise _EvidenceRejected("BATCH_CANDIDATE_SELECTION_NOT_ELIGIBLE")
            self._record_accepted(job, scope, payload, template, candidate_ids, pool.deployment_id)
            result = self.pipeline.execute_batch(
                batch_id=job.job_id,
                contract_template_artifact=template,
                owner=job.requested_by,
                labels=_strings(payload, "labels"),
                session=self.d.sessions(job.requested_by),
                expected_deployment_revision=_integer(scope, "expected_deployment_revision"),
                now=self.d.clock(),
            )
        except _EvidenceRejected as error:
            return rejected_execution(request=request, code=error.code)
        except KeyError:
            return rejected_execution(request=request, code="BATCH_FROZEN_OBJECT_MISSING")
        except ArxivBatchError:
            return rejected_execution(request=request, code="BATCH_EXECUTION_EVIDENCE_REJECTED")
        return domain_success(
            request=request,
            affected_entity_ids=(result.batch_id, *(item[1] for item in result.created_runs)),
            result_refs=(
                frozen_json(
                    {
                        "batch_id": result.batch_id,
                        "state": result.state,
                        "created_runs": [list(item) for item in result.created_runs],
                        "authority_effect": "NO_FACT",
                    }
                ),
            ),
        )

    def _record_accepted(
        self,
        job: DurableJob,
        scope: Mapping[str, object],
        payload: Mapping[str, object],
        template: ExactArtifactRef,
        candidate_ids: tuple[str, ...],
        pool_deployment_id: str,
    ) -> None:
        deployment_id = _text(scope, "deployment_id")
        if deployment_id != pool_deployment_id or deployment_id != job.deployment_id:
            raise _EvidenceRejected("BATCH_DEPLOYMENT_FENCE_MISMATCH")
        values = (
            _text(payload, "problem_pool_id"),
            job.request_id,
            deployment_id,
            _json(list(candidate_ids)),
            template.artifact_id,
            template.sha256,
            _json(dict(_object(payload, "per_run_budget"))),
            _json(list(_strings(payload, "labels"))),
            job.receipt_id,
            "PENDING",
        )
        with open_sqlite(self.d.db_path, isolation_level=None) as connection:
            connection.execute("PRAGMA foreign_keys=ON")
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT problem_pool_id,request_id,deployment_id,candidate_ids_json,"
                "contract_template_artifact_id,contract_template_sha256,per_run_budget_json,"
                "labels_json,batch_receipt_id,batch_receipt_state FROM "
                "product_problem_batch_commands WHERE batch_id=?",
                (job.job_id,),
            ).fetchone()
            if row is None:
                now = self.d.clock()
                connection.execute(
                    "INSERT INTO product_problem_batch_commands("
                    "batch_id,problem_pool_id,request_id,deployment_id,candidate_ids_json,"
                    "contract_template_artifact_id,contract_template_sha256,per_run_budget_json,"
                    "labels_json,batch_receipt_id,batch_receipt_state,state,created_at,updated_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?,?,'DISPATCHED',?,?)",
                    (job.job_id, *values, now, now),
                )
            elif tuple(row) != values:
                raise _EvidenceRejected("BATCH_ACCEPTED_COMMAND_BINDING_MISMATCH")
            connection.commit()


@dataclass(frozen=True, slots=True)
class _AblationPort:
    d: _Dependencies

    def __call__(self, job: DurableJob, request: Mapping[str, Any]) -> TypedExecution:
        try:
            payload = _payload(request)
            scope = _scope(request, "RUN")
            run_id, revision, contract = _run_fence(self.d.db_path, scope)
            problem_ids = _strings(payload, "problem_candidate_ids")
            if _integer(payload, "candidate_count") != len(problem_ids) or not problem_ids:
                raise _EvidenceRejected("ABLATION_CANDIDATE_DENOMINATOR_MISMATCH")
            pool_digest = _frozen_pool_digest(self.d, problem_ids)
            model = _profile(self.d.db_path, _text(payload, "model_profile"))
            tools = _profile(self.d.db_path, _text(payload, "tool_profile"))
            verifier_profile = _text(payload, "final_verifier_profile")
            verifier = _profile(self.d.db_path, verifier_profile)
            verifier_receipt = _verifier_receipt(
                self.d.db_path, run_id, revision, contract, verifier_profile
            )
            config = FrozenAblationConfig(
                problem_pool_digest=pool_digest,
                problem_ids=problem_ids,
                model_identity=model,
                tool_builds=tools,
                candidate_count=len(problem_ids),
                budget=dict(_object(payload, "budget")),
                verifier_identity=verifier,
                verifier_profile_receipt_id=verifier_receipt,
            )
            plan_id = _text(payload, "ablation_plan_id")
            store = AblationStore(self.d.db_path)
            with open_sqlite(self.d.db_path) as connection:
                row = connection.execute(
                    "SELECT run_id,frozen_digest FROM product_ablation_plans "
                    "WHERE ablation_plan_id=?",
                    (plan_id,),
                ).fetchone()
            if row is None:
                digest = store.freeze(
                    ablation_plan_id=plan_id,
                    run_id=run_id,
                    config=config,
                    created_at=self.d.clock(),
                )
            elif tuple(map(str, row)) == (run_id, config.digest):
                digest = config.digest
            else:
                raise _EvidenceRejected("ABLATION_FROZEN_CONFIGURATION_DRIFT")
        except _EvidenceRejected as error:
            return rejected_execution(request=request, code=error.code)
        except KeyError:
            return rejected_execution(request=request, code="ABLATION_FROZEN_OBJECT_MISSING")
        return domain_success(
            request=request,
            affected_entity_ids=(plan_id,),
            result_refs=(
                frozen_json(
                    {
                        "ablation_plan_id": plan_id,
                        "frozen_digest": digest,
                        "group": _text(payload, "group"),
                        "authority_effect": "RESEARCH_HYPOTHESIS_ONLY",
                    }
                ),
            ),
        )


@dataclass(frozen=True, slots=True)
class _LineagePort:
    d: _Dependencies

    def __call__(self, job: DurableJob, request: Mapping[str, Any]) -> TypedExecution:
        try:
            payload = _payload(request)
            scope = _scope(request, "RUN")
            run_id, revision, contract = _run_fence(self.d.db_path, scope)
            mode = _text(payload, "mode")
            if mode == "HISTORICAL_CANDIDATE_MIGRATION":
                result = self._historical(job, payload, run_id, revision, contract)
            else:
                result = self._zhao(job, payload, run_id, contract, mode)
        except _EvidenceRejected as error:
            return rejected_execution(request=request, code=error.code)
        except (KeyError, ResearchLineageError):
            return rejected_execution(request=request, code="LINEAGE_EVIDENCE_REJECTED")
        return domain_success(
            request=request,
            affected_entity_ids=(result.lineage_id,),
            result_refs=(
                frozen_json(
                    {
                        "lineage_id": result.lineage_id,
                        "mode": result.mode.value,
                        "status": result.status,
                        "authority_effect": "CANDIDATE_ONLY_NO_FACT_GRAPH_WRITE",
                    }
                ),
            ),
        )

    def _zhao(
        self,
        job: DurableJob,
        payload: Mapping[str, object],
        run_id: str,
        contract: int,
        mode: str,
    ) -> Any:
        if _text(payload, "source_project_id") != "ZHAO_C61":
            raise _EvidenceRejected("LINEAGE_SOURCE_PROJECT_MISMATCH")
        if mode == "CLEAN_ROOM_REDISCOVERY":
            if payload.get("historical_conclusions_injected") is not False:
                raise _EvidenceRejected("CLEAN_ROOM_HISTORICAL_EVIDENCE_FORBIDDEN")
            manifest_ref = _artifact(
                self.d.artifacts, _object(payload, "clean_room_input_manifest")
            )
        elif mode == "IMPORTED_CERTIFICATE_VERIFICATION":
            if not _strings(payload, "verifier_receipt_ids"):
                raise _EvidenceRejected("CERTIFICATE_VERIFIER_RECEIPTS_MISSING")
            if not _objects(payload, "imported_artifacts"):
                raise _EvidenceRejected("CERTIFICATE_ARTIFACTS_MISSING")
            manifest_ref = _artifact(
                self.d.artifacts, _object(payload, "certificate_import_report")
            )
        else:
            raise _EvidenceRejected("LINEAGE_MODE_UNSUPPORTED")
        manifest = _artifact_json(self.d.artifacts, manifest_ref)
        if manifest.get("schema_version") != "rk.zhao_input_manifest.v1":
            raise _EvidenceRejected("LINEAGE_INPUT_MANIFEST_MISSING")
        _source_versions(self.d.db_path, manifest, _strings(payload, "source_versions"))
        return self.d.lineage.start_zhao(
            lineage_id=job.job_id,
            mode=LineageMode(mode),
            run_id=run_id,
            contract_version=contract,
            frozen_tree_digest=_text(manifest, "frozen_tree_digest"),
            data_root_id=_text(manifest, "data_root_id"),
            input_manifest=manifest_ref,
            created_by_subject_id=self.d.sessions(job.requested_by).principal_subject_id,
            now=self.d.clock(),
        )

    def _historical(
        self,
        job: DurableJob,
        payload: Mapping[str, object],
        run_id: str,
        revision: int,
        contract: int,
    ) -> Any:
        if _text(payload, "source_project_id") != "N2_AJT5":
            raise _EvidenceRejected("HISTORICAL_PROJECT_ALIAS_REJECTED")
        if payload.get("promote_as_verified") is not False:
            raise _EvidenceRejected("HISTORICAL_PROMOTION_FORBIDDEN")
        documents = [
            (
                _artifact(self.d.artifacts, item),
                _artifact_json(self.d.artifacts, _artifact(self.d.artifacts, item)),
            )
            for item in _objects(payload, "candidate_artifacts")
        ]
        plans = [
            (ref, value)
            for ref, value in documents
            if value.get("schema_version") == "rk.n2_production_plan.v1"
        ]
        if len(plans) != 1:
            raise _EvidenceRejected("N2_AJT5_PRODUCTION_PLAN_MISSING")
        _, plan = plans[0]
        manifest_ref = _artifact(self.d.artifacts, _object(plan, "input_manifest"))
        manifest = _artifact_json(self.d.artifacts, manifest_ref)
        if manifest.get("schema_version") != "rk.n2_history_manifest.v1":
            raise _EvidenceRejected("N2_AJT5_INPUT_MANIFEST_MISSING")
        _source_versions(self.d.db_path, manifest, _strings(payload, "source_versions"))
        materials = tuple(
            HistoricalMaterialInput(
                _text(item, "lineage_artifact_id"),
                _text(item, "material_id"),
                _text(item, "material_kind"),
            )
            for item in _objects(plan, "material_inputs")
        )
        candidates = tuple(
            HistoricalClaimCandidate(
                _text(item, "historical_input_id"),
                _text(item, "source_lineage_artifact_id"),
                _text(item, "statement"),
                ClaimKind(_text(item, "claim_kind")),
                _text(item, "stable_label"),
                _text(item, "worker_run_id"),
                _text(item, "attempt_id"),
            )
            for item in _objects(plan, "claim_candidates")
        )
        return self.d.historical_importer.migrate_n2_ajt5(
            lineage_id=job.job_id,
            run_id=run_id,
            contract_version=contract,
            kernel_revision=revision,
            frozen_tree_digest=_text(manifest, "frozen_tree_digest"),
            data_root_id=_text(manifest, "data_root_id"),
            input_manifest=manifest_ref,
            material_inputs=materials,
            claim_candidates=candidates,
            subject_id=self.d.sessions(job.requested_by).principal_subject_id,
            now=self.d.clock(),
        )


def _frozen_pool_digest(d: _Dependencies, problem_ids: tuple[str, ...]) -> str:
    candidates = tuple(d.problem_pools.get_candidate(item) for item in problem_ids)
    pool_ids = {item.problem_pool_id for item in candidates}
    if len(pool_ids) != 1:
        raise _EvidenceRejected("ABLATION_PROBLEM_POOL_MISMATCH")
    pool_id = next(iter(pool_ids))
    pool = d.problem_pools.get(pool_id)
    if pool.state != "FROZEN" or any(
        item.audit_status != "HUMAN_INCLUDED" or item.recommendation_status != "RECOMMENDED"
        for item in candidates
    ):
        raise _EvidenceRejected("ABLATION_PROBLEM_POOL_NOT_FROZEN")
    bindings = {item.binding_kind: item for item in d.problem_pools.artifact_bindings(pool_id)}
    semantic = bindings.get("SEMANTIC_AUDIT")
    if semantic is None:
        raise _EvidenceRejected("ABLATION_SEMANTIC_AUDIT_ARTIFACT_MISSING")
    summary = {
        "problem_pool": {
            "problem_pool_id": pool.problem_pool_id,
            "date_from": pool.date_from,
            "date_to": pool.date_to,
            "subjects": list(pool.subjects),
            "version_rule": pool.version_rule,
            "withdrawal_rule": pool.withdrawal_rule,
            "exclusion_rules": list(pool.exclusion_rules),
            "frozen_by": pool.frozen_by,
            "frozen_at": pool.frozen_at,
        },
        "semantic_audit_artifact": {
            "artifact_id": semantic.artifact.artifact_id,
            "sha256": semantic.artifact.sha256,
            "byte_count": semantic.artifact.byte_count,
            "media_type": semantic.artifact.media_type,
        },
        "problem_candidates": [
            {
                "problem_candidate_id": item.problem_candidate_id,
                "arxiv_id": item.arxiv_id,
                "version": item.version,
                "normalized_statement": item.normalized_statement,
                "recommendation_score": item.recommendation_score,
            }
            for item in candidates
        ],
    }
    return hashlib.sha256(canonical_json_bytes(summary)).hexdigest()


def _profile(db_path: Path, profile_id: str) -> dict[str, object]:
    with open_sqlite(db_path) as connection:
        rows = connection.execute(
            "SELECT tool_id,tool_version,function_name,provider,build_version,"
            "function_schema_digest,availability,authority_ceiling FROM "
            "product_tool_catalog WHERE profile_id=? "
            "ORDER BY tool_id,tool_version,function_name",
            (profile_id,),
        ).fetchall()
    if not rows:
        raise _EvidenceRejected("ABLATION_PROFILE_NOT_REGISTERED")
    research_available = {"AVAILABLE", "PRODUCT_RECEIPT_AVAILABLE"}
    if any(str(row[6]) not in research_available for row in rows):
        raise _EvidenceRejected("ABLATION_PROFILE_NOT_AVAILABLE")
    return {
        "profile_id": profile_id,
        "functions": [
            {
                "tool_id": str(row[0]),
                "tool_version": str(row[1]),
                "function_name": str(row[2]),
                "provider": str(row[3]),
                "build_version": str(row[4]),
                "function_schema_digest": str(row[5]),
                "availability": str(row[6]),
                "authority_ceiling": str(row[7]),
            }
            for row in rows
        ],
    }


def _verifier_receipt(
    db_path: Path, run_id: str, revision: int, contract: int, profile_id: str
) -> str:
    with open_sqlite(db_path) as connection:
        rows = connection.execute(
            "SELECT DISTINCT r.validation_receipt_id FROM product_tool_runs r "
            "JOIN product_tool_catalog c ON c.tool_id=r.tool_id "
            "AND c.tool_version=r.tool_version AND c.function_name=r.function_name "
            "WHERE r.run_id=? AND r.research_revision=? AND r.contract_version=? "
            "AND c.profile_id=? AND r.validation_status='VALIDATION_ACCEPTED' "
            "AND r.validation_receipt_id IS NOT NULL",
            (run_id, revision, contract, profile_id),
        ).fetchall()
    if len(rows) != 1:
        raise _EvidenceRejected(
            "ABLATION_VERIFIER_RECEIPT_MISSING"
            if not rows
            else "ABLATION_VERIFIER_RECEIPT_AMBIGUOUS"
        )
    return str(rows[0][0])


def _run_fence(db_path: Path, scope: Mapping[str, object]) -> tuple[str, int, int]:
    run_id = _text(scope, "run_id")
    expected_revision = _integer(scope, "expected_revision")
    expected_contract = _integer(scope, "expected_contract_version")
    with open_sqlite(db_path) as connection:
        row = connection.execute(
            "SELECT revision,current_contract_version FROM runs WHERE run_id=?", (run_id,)
        ).fetchone()
    if row is None or tuple(map(int, row)) != (expected_revision, expected_contract):
        raise _EvidenceRejected("DURABLE_RUN_FENCE_STALE")
    return run_id, expected_revision, expected_contract


def _source_versions(
    db_path: Path, manifest: Mapping[str, object], declared: tuple[str, ...]
) -> None:
    keys = (
        "worker_input_lineage_artifact_ids",
        "imported_certificate_lineage_artifact_ids",
        "source_lineage_artifact_ids",
    )
    ids: list[str] = []
    for key in keys:
        value = manifest.get(key, [])
        if isinstance(value, Sequence) and not isinstance(value, (str, bytes)):
            ids.extend(str(item) for item in value)
    if not ids:
        raise _EvidenceRejected("LINEAGE_SOURCE_ARTIFACTS_MISSING")
    placeholders = ",".join("?" for _ in ids)
    with open_sqlite(db_path) as connection:
        rows = connection.execute(
            "SELECT lineage_artifact_id,source_version FROM product_research_lineage_artifacts "
            f"WHERE lineage_artifact_id IN ({placeholders}) ORDER BY lineage_artifact_id",
            tuple(ids),
        ).fetchall()
    if len(rows) != len(set(ids)):
        raise _EvidenceRejected("LINEAGE_SOURCE_ARTIFACT_NOT_REGISTERED")
    actual = tuple(sorted(str(row[1]) for row in rows))
    if tuple(sorted(declared)) != actual:
        raise _EvidenceRejected("LINEAGE_SOURCE_VERSION_MISMATCH")


def _artifact(service: ArtifactReadService, value: Mapping[str, object]) -> ExactArtifactRef:
    if set(value) != {"artifact_id", "sha256", "byte_count", "media_type"}:
        raise _EvidenceRejected("EXACT_ARTIFACT_REF_REQUIRED")
    ref = ExactArtifactRef(
        _text(value, "artifact_id"),
        _text(value, "sha256"),
        _integer(value, "byte_count"),
        _text(value, "media_type"),
    )
    try:
        service.describe(ref.artifact_id, expected_ref=ref)
    except (ArtifactReadError, OSError) as error:
        raise _EvidenceRejected("ARTIFACT_EVIDENCE_UNAVAILABLE") from error
    return ref


def _artifact_json(service: ArtifactReadService, ref: ExactArtifactRef) -> dict[str, object]:
    if ref.media_type != "application/json":
        raise _EvidenceRejected("JSON_EVIDENCE_REQUIRED")
    body = b"".join(service.open_range(ref.artifact_id, expected_ref=ref).stream)
    if len(body) != ref.byte_count or hashlib.sha256(body).hexdigest() != ref.sha256:
        raise _EvidenceRejected("ARTIFACT_EVIDENCE_DIGEST_MISMATCH")
    try:
        value = json.loads(body)
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise _EvidenceRejected("JSON_EVIDENCE_INVALID") from error
    if not isinstance(value, dict):
        raise _EvidenceRejected("JSON_EVIDENCE_OBJECT_REQUIRED")
    return {str(key): item for key, item in value.items()}


def _payload(request: Mapping[str, Any]) -> Mapping[str, object]:
    command = _object(request, "command")
    return _object(command, "payload")


def _scope(request: Mapping[str, Any], kind: str) -> Mapping[str, object]:
    scope = _object(request, "scope")
    if scope.get("kind") != kind:
        raise ValueError(f"{kind} scope required")
    return scope


def _object(value: Mapping[str, object], key: str) -> Mapping[str, object]:
    item = value.get(key)
    if not isinstance(item, Mapping):
        raise ValueError(f"{key} must be an object")
    return cast(Mapping[str, object], item)


def _objects(value: Mapping[str, object], key: str) -> tuple[Mapping[str, object], ...]:
    item = value.get(key)
    if not isinstance(item, Sequence) or isinstance(item, (str, bytes)):
        raise ValueError(f"{key} must be an object array")
    if any(not isinstance(entry, Mapping) for entry in item):
        raise ValueError(f"{key} must contain objects")
    return tuple(cast(Mapping[str, object], entry) for entry in item)


def _strings(value: Mapping[str, object], key: str) -> tuple[str, ...]:
    item = value.get(key)
    if not isinstance(item, Sequence) or isinstance(item, (str, bytes)):
        raise ValueError(f"{key} must be a string array")
    result = tuple(item)
    if any(not isinstance(entry, str) or not entry for entry in result):
        raise ValueError(f"{key} contains an invalid string")
    return cast(tuple[str, ...], result)


def _text(value: Mapping[str, object], key: str) -> str:
    item = value.get(key)
    if not isinstance(item, str) or not item:
        raise ValueError(f"{key} must be a non-empty string")
    return item


def _integer(value: Mapping[str, object], key: str) -> int:
    item = value.get(key)
    if isinstance(item, bool) or not isinstance(item, int):
        raise ValueError(f"{key} must be an integer")
    return item


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


__all__ = [
    "CommandProductPort",
    "ResearchProductionPorts",
    "build_research_production_ports",
]
