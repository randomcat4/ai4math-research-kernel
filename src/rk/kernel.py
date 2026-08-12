"""The only caller-facing deep module: create, apply, inspect, and export."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from rk.cas import CommittedArtifact, ContentAddressedStore, StagedArtifact
from rk.config import KernelConfig
from rk.domain import (
    ApplyRequest,
    ArtifactRef,
    CapabilityError,
    CommandReceipt,
    CreateRequest,
    Decision,
    EventPage,
    ExportRequest,
    MissingCondition,
    RequestValidationError,
    RunHandle,
    RunSnapshot,
    VerifiedCapability,
    frozen_mapping,
)
from rk.dossier import DossierBuilder
from rk.guard import TransitionGuard
from rk.ingest import EvidenceIngest, IngestDisposition, IngestPolicy
from rk.migrations import MigrationRunner
from rk.ports import Clock, IdGenerator
from rk.projector import ProjectionContext, ProjectionWriter
from rk.runtime import SystemClock, Uuid7Generator, format_utc
from rk.storage import RunNotFound, SQLiteStorage, StorageConflict
from rk.wire import WireValidator, canonical_json_bytes, request_digest


def _artifact_input_mapping(value: Any) -> dict[str, Any]:
    return {
        "name": value.name,
        "path": value.path,
        "sha256": value.sha256,
        "byte_count": value.byte_count,
        "media_type": value.media_type,
    }


def _create_wire(request: CreateRequest) -> dict[str, Any]:
    return {
        "schema_version": "rk.command.v1",
        "operation": "create",
        "request_id": request.request_id,
        "contract": dict(request.contract),
        "artifact_inputs": [_artifact_input_mapping(item) for item in request.artifact_inputs],
    }


def _apply_wire(request: ApplyRequest) -> dict[str, Any]:
    return {
        "schema_version": "rk.command.v1",
        "operation": "apply",
        "request_id": request.request_id,
        "run_id": request.run_id,
        "expected_revision": request.expected_revision,
        "command": {"type": request.command.type, "payload": dict(request.command.payload)},
        "artifact_inputs": [_artifact_input_mapping(item) for item in request.artifact_inputs],
    }


def _export_wire(request: ExportRequest) -> dict[str, Any]:
    return {
        "schema_version": "rk.command.v1",
        "operation": "export",
        "request_id": request.request_id,
        "run_id": request.run_id,
        "at_revision": request.at_revision,
        "dossier_spec": dict(request.dossier_spec),
    }


def _receipt(value: Mapping[str, Any]) -> CommandReceipt:
    return CommandReceipt(
        request_id=str(value["request_id"]),
        command_id=str(value["command_id"]),
        run_id=str(value["run_id"]),
        accepted=bool(value["accepted"]),
        revision_before=int(value["revision_before"]),
        revision_after=int(value["revision_after"]),
        event_ids=tuple(str(item) for item in value.get("event_ids", ())),
        artifact_ids=tuple(str(item) for item in value.get("artifact_ids", ())),
        rejection_code=(
            str(value["rejection_code"]) if value.get("rejection_code") is not None else None
        ),
        missing_conditions=tuple(
            MissingCondition(
                code=str(item["code"]),
                path=str(item["path"]),
                params=frozen_mapping(item.get("params", {})),
            )
            for item in value.get("missing_conditions", ())
        ),
        decided_at=str(value["decided_at"]),
    )


class ResearchKernel:
    """Own all state transitions while hiding storage, guards, and adapters."""

    def __init__(
        self,
        *,
        storage: SQLiteStorage,
        cas: ContentAddressedStore,
        guard: TransitionGuard,
        projector: ProjectionWriter,
        ingest: EvidenceIngest,
        wire: WireValidator,
        clock: Clock,
        id_generator: IdGenerator,
        dossier: DossierBuilder,
        policy_snapshot: Mapping[str, Any] | None = None,
    ) -> None:
        self._storage = storage
        self._cas = cas
        self._guard = guard
        self._projector = projector
        self._ingest = ingest
        self._wire = wire
        self._clock = clock
        self._ids = id_generator
        self._dossier = dossier
        self._policy = dict(policy_snapshot or {})

    @classmethod
    def from_config(
        cls,
        config: KernelConfig,
        *,
        migrations_dir: Path | None = None,
    ) -> ResearchKernel:
        config.prepare_local_directories()
        root = Path(__file__).resolve().parents[2]
        migration_root = (migrations_dir or root / "migrations").resolve()
        MigrationRunner(config.db_path, migration_root, config.busy_timeout_ms).migrate()
        ids = Uuid7Generator()
        clock = SystemClock()
        storage = SQLiteStorage(config.db_path, config.busy_timeout_ms)
        cas = ContentAddressedStore(
            config.cas_root,
            config.max_artifact_bytes,
            config.inbox_roots,
            config.orphan_grace_seconds,
            ids,
            artifact_lookup=storage.get_artifact,
        )
        ingest = EvidenceIngest(
            IngestPolicy(
                inbox_roots=config.inbox_roots,
                max_artifact_bytes=config.max_artifact_bytes,
                max_archive_expanded_bytes=config.max_artifact_bytes * 4,
                max_archive_files=2_048,
                max_archive_ratio=100.0,
            )
        )
        policy = {
            "adapter_profiles": dict(config.adapter_profiles),
            "verifier_profiles": dict(config.verifier_profiles),
            **dict(config.budget_policy),
        }
        return cls(
            storage=storage,
            cas=cas,
            guard=TransitionGuard(),
            projector=ProjectionWriter(ids),
            ingest=ingest,
            wire=WireValidator(config.command_schema_path, config.receipt_schema_path),
            clock=clock,
            id_generator=ids,
            dossier=DossierBuilder(),
            policy_snapshot=policy,
        )

    def create(self, request: CreateRequest, capability: VerifiedCapability) -> RunHandle:
        if not capability.allows("create"):
            raise CapabilityError("CAPABILITY_DENIED")
        wire_value = _create_wire(request)
        self._wire.validate_request(wire_value)
        digest = request_digest(wire_value)
        existing = self._storage.find_create_request(capability.issuer, request.request_id)
        if existing is not None:
            if existing["create_request_digest"] != digest:
                raise StorageConflict("create request id was reused with different content")
            return RunHandle(**existing["handle"])

        inspected = [self._ingest.inspect(item) for item in request.artifact_inputs]
        if any(not item.accepted for item in inspected):
            raise RequestValidationError("create artifact input was rejected by ingest policy")
        stages: list[StagedArtifact] = []
        now = self._clock.now()
        try:
            contract_bytes = canonical_json_bytes(request.contract)
            stages.append(
                self._cas.stage_bytes(
                    contract_bytes,
                    media_type="application/json",
                    source_name="contract.v1.json",
                )
            )
            stages.extend(self._cas.stage_input(item) for item in request.artifact_inputs)
            committed = [self._cas.commit(item, now=now) for item in stages]
        except BaseException:
            for item in stages:
                self._cas.discard(item)
            raise

        self._storage.ensure_capability(capability)
        handle = self._storage.create_run_atomic(
            run_id=self._ids.new(),
            stable_project_id=str(request.contract["stable_project_id"]),
            create_issuer=capability.issuer,
            create_request_id=request.request_id,
            create_request_digest=digest,
            capability_id=capability.capability_id,
            contract_artifact=committed[0].to_record(),
            additional_artifacts=tuple(item.to_record() for item in committed[1:]),
            contract_json=dict(request.contract),
            statement_hash=hashlib.sha256(contract_bytes).hexdigest(),
            created_at=format_utc(now),
        )
        return RunHandle(**handle)

    def apply(self, request: ApplyRequest, capability: VerifiedCapability) -> CommandReceipt:
        if not capability.allows(request.command.type, request.run_id):
            raise CapabilityError("CAPABILITY_DENIED")
        wire_value = _apply_wire(request)
        self._wire.validate_request(wire_value)
        digest = request_digest(wire_value)
        now = self._clock.now()
        decided_at = format_utc(now)
        inspections = [self._ingest.inspect(item) for item in request.artifact_inputs]
        stages: list[StagedArtifact] = []
        if all(item.accepted for item in inspections):
            stages = [self._cas.stage_input(item) for item in request.artifact_inputs]

        self._storage.ensure_capability(capability)
        with self._storage.transaction() as connection:
            previous = self._storage.find_command(
                request.run_id, request.request_id, connection=connection
            )
            if previous is not None:
                for stage in stages:
                    self._cas.discard(stage)
                if previous["request_digest"] != digest:
                    raise StorageConflict("apply request id was reused with different content")
                return _receipt(previous["receipt"])

            snapshot = self._storage.guard_snapshot(request.run_id, connection=connection)
            evidence_summary = {
                # The create wire schema already required every contract field.  Recompute this
                # host fact here instead of asking callers for an impossible extra wire field.
                "contract_complete": bool(snapshot["projection"].get("contract")),
                "artifact_inputs": [
                    {
                        "name": item.name,
                        "sha256": item.sha256,
                        "byte_count": item.byte_count,
                        "media_type": item.media_type,
                        "status": "COMMITTED",
                    }
                    for item in request.artifact_inputs
                ],
            }
            if any(not item.accepted for item in inspections):
                quarantined = any(
                    item.disposition is IngestDisposition.QUARANTINE for item in inspections
                )
                decision = Decision(
                    accepted=False,
                    rejection_code=(
                        "SECRET_QUARANTINED" if quarantined else "INGEST_SCHEMA_INVALID"
                    ),
                    missing_conditions=(
                        MissingCondition(
                            code="INGEST_POLICY",
                            path="/artifact_inputs",
                            params=frozen_mapping(
                                {
                                    "findings": [
                                        finding.code
                                        for result in inspections
                                        for finding in result.findings
                                    ]
                                }
                            ),
                        ),
                    ),
                )
            else:
                decision = self._guard.decide(
                    now_utc=now,
                    snapshot=snapshot,
                    command=request.command,
                    evidence_summary=evidence_summary,
                    capability=capability,
                    policy_snapshot=self._policy,
                    expected_revision=request.expected_revision,
                )
                if decision.accepted and not self._projector.supports(
                    decision.projection_mutations
                ):
                    decision = Decision(
                        accepted=False,
                        rejection_code="TEMPORARILY_UNAVAILABLE",
                        missing_conditions=(
                            MissingCondition(
                                code="IMPLEMENTATION_SLICE",
                                path="/command/type",
                                params=frozen_mapping({"command": request.command.type}),
                            ),
                        ),
                    )
                if (
                    decision.accepted
                    and request.command.type == "Finalize"
                    and bool(
                        request.command.payload.get("dossier_spec", {}).get(
                            "include_raw_artifacts", False
                        )
                    )
                ):
                    decision = Decision(
                        accepted=False,
                        rejection_code="TEMPORARILY_UNAVAILABLE",
                        missing_conditions=(
                            MissingCondition(
                                code="IMPLEMENTATION_SLICE",
                                path="/command/payload/dossier_spec/include_raw_artifacts",
                                params=frozen_mapping({"supported": False}),
                            ),
                        ),
                    )

            command_id = self._ids.new()
            trace_id = self._ids.new()
            event_ids: tuple[str, ...] = (self._ids.new(),) if decision.accepted else ()
            revision_before = int(snapshot["revision"])
            revision_after = revision_before + 1 if decision.accepted else revision_before
            committed: list[CommittedArtifact] = []
            generated: dict[str, str] = {}
            canonical_by_name: dict[str, Mapping[str, Any]] = {}
            if decision.accepted:
                committed = [self._cas.commit(item, now=now) for item in stages]
                for item in committed:
                    canonical = self._storage.insert_artifact(connection, item.to_record())
                    if item.source_name is not None:
                        canonical_by_name[item.source_name] = canonical
                        self._storage.link_artifact(
                            connection,
                            run_id=request.run_id,
                            artifact_id=str(canonical["artifact_id"]),
                            logical_name=f"{item.source_name}@r{revision_after}",
                            role="APPLY_INPUT",
                            linked_at=decided_at,
                        )
                if request.command.type == "SubmitClosureWitness":
                    graph_stage = self._cas.stage_bytes(
                        canonical_json_bytes(request.command.payload["selected_subgraph"]),
                        media_type="application/json",
                        source_name="selected_subgraph.json",
                    )
                    graph = self._cas.commit(graph_stage, now=now)
                    canonical = self._storage.insert_artifact(connection, graph.to_record())
                    generated["selected_subgraph"] = str(canonical["artifact_id"])
                    self._storage.link_artifact(
                        connection,
                        run_id=request.run_id,
                        artifact_id=str(canonical["artifact_id"]),
                        logical_name=f"selected_subgraph@r{revision_after}",
                        role="CLOSURE_GRAPH",
                        linked_at=decided_at,
                    )
                    committed.append(graph)
                if request.command.type == "Finalize":
                    final_snapshot = RunSnapshot(
                        run_id=request.run_id,
                        status="CLOSED",
                        revision=revision_after,
                        current_contract_version=int(snapshot["current_contract_version"]),
                        last_cursor=0,
                        projection=frozen_mapping(snapshot["projection"]),
                    )
                    data, media_type = self._dossier.build(
                        final_snapshot, request.command.payload["dossier_spec"]
                    )
                    dossier_stage = self._cas.stage_bytes(
                        data, media_type=media_type, source_name="final_dossier"
                    )
                    dossier = self._cas.commit(dossier_stage, now=now)
                    canonical = self._storage.insert_artifact(connection, dossier.to_record())
                    generated["dossier"] = str(canonical["artifact_id"])
                    self._storage.link_artifact(
                        connection,
                        run_id=request.run_id,
                        artifact_id=str(canonical["artifact_id"]),
                        logical_name=f"final_dossier@r{revision_after}",
                        role="DOSSIER",
                        linked_at=decided_at,
                    )
                    committed.append(dossier)
            else:
                for stage in stages:
                    self._cas.discard(stage)

            artifact_ids = tuple(
                sorted(
                    {str(value["artifact_id"]) for value in canonical_by_name.values()}
                    | set(generated.values())
                )
            )
            receipt = CommandReceipt(
                request_id=request.request_id,
                command_id=command_id,
                run_id=request.run_id,
                accepted=decision.accepted,
                revision_before=revision_before,
                revision_after=revision_after,
                event_ids=event_ids,
                artifact_ids=artifact_ids,
                rejection_code=decision.rejection_code,
                missing_conditions=decision.missing_conditions,
                decided_at=decided_at,
            )
            receipt_json = receipt.to_dict()
            self._wire.validate_receipt(receipt_json)
            self._storage.record_command(
                connection,
                command_id=command_id,
                run_id=request.run_id,
                request_id=request.request_id,
                command_type=request.command.type,
                request_digest=digest,
                expected_revision=request.expected_revision,
                capability_id=capability.capability_id,
                accepted=decision.accepted,
                revision_before=revision_before,
                revision_after=revision_after,
                rejection_code=decision.rejection_code,
                missing_conditions=[item.to_dict() for item in decision.missing_conditions],
                receipt=receipt_json,
                trace_id=trace_id,
                decided_at=decided_at,
            )
            if decision.accepted:
                event = decision.event_intents[0]
                payload = dict(request.command.payload)
                payload.pop("selected_subgraph", None)
                payload["command_type"] = request.command.type
                payload["generated_artifact_ids"] = generated
                self._storage.append_event(
                    connection,
                    event_id=event_ids[0],
                    run_id=request.run_id,
                    command_id=command_id,
                    revision=revision_after,
                    event_type=str(event["type"]),
                    payload=payload,
                    recorded_at=decided_at,
                    contract_version=int(
                        request.command.payload.get(
                            "contract_version", snapshot["current_contract_version"]
                        )
                    ),
                    route_id=request.command.payload.get("route_id"),
                    claim_id=request.command.payload.get(
                        "claim_id", request.command.payload.get("parent_claim_id")
                    ),
                    attempt_id=request.command.payload.get("attempt_id"),
                )
                self._projector.apply(
                    connection,
                    ProjectionContext(
                        run_id=request.run_id,
                        command_id=command_id,
                        event_id=event_ids[0],
                        revision=revision_after,
                        contract_version=int(snapshot["current_contract_version"]),
                        command=request.command,
                        capability_id=capability.capability_id,
                        recorded_at=decided_at,
                        artifacts_by_name=canonical_by_name,
                        generated_artifact_ids=generated,
                    ),
                    decision.projection_mutations,
                )
                self._storage.advance_revision(
                    connection,
                    request.run_id,
                    request.expected_revision,
                    updated_at=decided_at,
                )
            return receipt

    def inspect(
        self,
        run_id: str,
        after_cursor: int | None = None,
        limit: int = 100,
    ) -> RunSnapshot | EventPage:
        if after_cursor is not None:
            page = self._storage.event_page(run_id, after_cursor, limit)
            return EventPage(
                run_id=page["run_id"],
                after_cursor=page["after_cursor"],
                events=tuple(frozen_mapping(item) for item in page["events"]),
                next_cursor=page["next_cursor"],
                has_more=page["has_more"],
            )
        value = self._storage.inspect_snapshot(run_id)
        projection = {
            key: item
            for key, item in value.items()
            if key
            not in {
                "run_id",
                "status",
                "revision",
                "current_contract_version",
                "last_cursor",
            }
        }
        return RunSnapshot(
            run_id=str(value["run_id"]),
            status=str(value["status"]),
            revision=int(value["revision"]),
            current_contract_version=int(value["current_contract_version"]),
            last_cursor=int(value["last_cursor"]),
            projection=frozen_mapping(projection),
        )

    def export(self, request: ExportRequest, capability: VerifiedCapability) -> ArtifactRef:
        if not capability.allows("export", request.run_id):
            raise CapabilityError("CAPABILITY_DENIED")
        self._wire.validate_request(_export_wire(request))
        snapshot = self.inspect(request.run_id)
        if not isinstance(snapshot, RunSnapshot) or snapshot.revision != request.at_revision:
            raise RequestValidationError("export revision is not the current persisted revision")
        data, media_type = self._dossier.build(snapshot, request.dossier_spec)
        now = self._clock.now()
        staged = self._cas.stage_bytes(data, media_type=media_type, source_name="dossier")
        committed = self._cas.commit(staged, now=now)
        with self._storage.transaction() as connection:
            canonical = self._storage.insert_artifact(connection, committed.to_record())
            self._storage.link_artifact(
                connection,
                run_id=request.run_id,
                artifact_id=str(canonical["artifact_id"]),
                logical_name=f"export@r{request.at_revision}@{committed.sha256[:12]}",
                role="DOSSIER",
                linked_at=format_utc(now),
            )
        return ArtifactRef(
            artifact_id=str(canonical["artifact_id"]),
            sha256=str(canonical["sha256"]),
            byte_count=int(canonical["byte_count"]),
            media_type=str(canonical["media_type"]),
            at_revision=request.at_revision,
        )


__all__ = ["ResearchKernel", "RunNotFound"]
