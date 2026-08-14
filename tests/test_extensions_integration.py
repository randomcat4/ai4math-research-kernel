from __future__ import annotations

import hashlib
import sqlite3
import uuid
from collections.abc import Mapping
from pathlib import Path
from types import MappingProxyType
from typing import Any

import pytest

from rk.composition import canonical_json_bytes as composition_json_bytes
from rk.config import KernelConfig
from rk.domain import (
    ApplyRequest,
    ArtifactInput,
    CreateRequest,
    RequestValidationError,
    RunSnapshot,
    TypedCommand,
    VerifiedCapability,
    frozen_mapping,
)
from rk.extensions import (
    AuthorityInvalidation,
    ExtensionConflict,
    ExtensionNotRegistered,
    ExtensionRegistry,
)
from rk.kernel import ResearchKernel
from rk.migrations import MigrationRunner
from rk.orchestrator import ComponentRequest, ResearchOrchestrator
from rk.product.invalidation import (
    AuthorityInvalidationEngine,
    AuthorityObjectKind,
    register_invalidation_engine,
)
from rk.product.placement import (
    ExecutionTarget,
    ExecutorKind,
    HardwareProfile,
    PlacementPlanner,
    TargetAvailability,
    WorkRequirement,
)
from rk.product_migrations import ProductMigrationAssembler, ProductMigrationRegistry
from rk.scheduler import UnschedulablePlan, place_registered_work
from rk.wire import WireValidator

ROOT = Path(__file__).parents[1]
SPEC = ROOT / "docs/spec/json"


class UnusedRuntime:
    def execute(self, request: ComponentRequest) -> dict[str, Any]:
        raise AssertionError(f"runtime must not execute {request.request_id}")


def capability() -> VerifiedCapability:
    return VerifiedCapability(
        capability_id=str(uuid.uuid4()),
        subject_id="integration-main",
        issuer="integration-host",
        allowed_actions=frozenset({"*"}),
        run_scope=frozenset({"*"}),
        issued_at="2020-01-01T00:00:00.000Z",
        expires_at="2100-01-01T00:00:00.000Z",
    )


def contract() -> dict[str, object]:
    return {
        "stable_project_id": "S00_INTEGRATION",
        "statement": "For every integration object, a witness exists.",
        "source_refs": [],
        "objects": [{"name": "integration object"}],
        "definitions": [],
        "quantifiers": [{"kind": "forall", "variable": "x"}],
        "exact_negation": "Some integration object has no witness.",
        "allowed_dependencies": [],
        "forbidden_information": [],
        "boundary_rules": {},
        "randomness_rules": {},
        "tie_rules": {},
        "success_certificate_types": ["NATURAL_LANGUAGE_PROOF"],
        "non_claims": [],
        "literature_scope": {
            "families": ["exact", "equivalent", "stronger", "weaker", "counterexample"]
        },
        "literature_cutoff_date": "2026-08-14",
        "budget_policy": {"global": {"CPU_SECOND": 1000}},
        "stop_rules": [{"kind": "manual"}],
        "semantic_review_policy": {},
        "amendment_policy": {},
    }


def test_legacy_wire_dispatches_before_old_schema_rejection_and_conflicts() -> None:
    observed: list[str] = []

    def dispatch(value: Mapping[str, Any]) -> TypedCommand:
        observed.append(str(value["variant"]))
        return TypedCommand("ImportedLegacy", frozen_mapping({"source": "legacy"}))

    registry = ExtensionRegistry().register_legacy_wire_dispatch("legacy-import-v2", dispatch)
    validator = WireValidator(SPEC / "command.schema.json", SPEC / "receipt.schema.json", registry)
    legacy = {
        "variant": "legacy-import-v2",
        "legacy_payload": {"operation": "import"},
    }

    translated = validator.validate_request(legacy)

    assert translated == TypedCommand("ImportedLegacy", frozen_mapping({"source": "legacy"}))
    assert observed == ["legacy-import-v2"]
    with pytest.raises(RequestValidationError):
        validator.validate_request({"variant": "unregistered", "legacy_payload": {}})
    with pytest.raises(ExtensionConflict, match="legacy-import-v2"):
        registry.register_legacy_wire_dispatch("legacy-import-v2", dispatch)


def available_profile() -> HardwareProfile:
    return HardwareProfile(
        "s00-server",
        "linux",
        64 * 1024**3,
        (
            ExecutionTarget(
                "cpu-exact",
                ExecutorKind.CPU,
                "cpu-workers",
                4,
                32 * 1024**3,
                availability=TargetAvailability.AVAILABLE,
                probe_receipt_id="probe-current",
                availability_fault=None,
                assets=frozenset({"verifier", "reranker"}),
            ),
        ),
    )


def test_scheduler_and_orchestrator_use_s00_b13_without_quality_fallback() -> None:
    requirement = WorkRequirement(
        "work-exact",
        "exact-verification",
        (ExecutorKind.CPU,),
        1024,
        None,
        None,
        137,
        True,
        True,
    )
    planner = PlacementPlanner(available_profile())
    registry = planner.register(ExtensionRegistry())
    expected = planner.place(requirement).to_dict()

    assert place_registered_work(registry, requirement.to_dict()) == expected
    orchestrator = ResearchOrchestrator(UnusedRuntime(), extensions=registry)
    assert orchestrator.place_work(requirement.to_dict()) == expected

    def lossy(request: Mapping[str, Any]) -> Mapping[str, Any]:
        result = dict(expected)
        result["retrieval_top_k"] = int(request["retrieval_top_k"]) - 1
        return result

    lossy_registry = ExtensionRegistry().register_placement_provider("b13-research", lossy)
    with pytest.raises(UnschedulablePlan, match="exact B13 quality"):
        place_registered_work(lossy_registry, requirement.to_dict())
    with pytest.raises(ExtensionNotRegistered, match="b13-research"):
        place_registered_work(ExtensionRegistry(), requirement.to_dict())


def test_real_kernel_commit_precedes_single_b11a_registry_consumption(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    support = inbox / "support.txt"
    support.write_text("contract completeness", encoding="utf-8")
    config = KernelConfig.from_mapping(
        {
            "workspace_root": str(tmp_path / "state"),
            "inbox_roots": [str(inbox)],
            "command_schema_path": str(SPEC / "command.schema.json"),
            "receipt_schema_path": str(SPEC / "receipt.schema.json"),
        },
        base=ROOT,
    )
    config.prepare_local_directories()
    MigrationRunner(config.db_path, ROOT / "migrations", 5_000).migrate()
    with sqlite3.connect(config.db_path) as connection:
        ProductMigrationAssembler(ProductMigrationRegistry(ROOT / "schema_fragments")).apply(
            connection
        )
    engine = AuthorityInvalidationEngine(config.db_path, lambda: "2026-08-14T12:00:00Z")
    registry = register_invalidation_engine(ExtensionRegistry(), engine)
    kernel = ResearchKernel.from_config(
        config, migrations_dir=ROOT / "migrations", extensions=registry
    )
    cap = capability()
    data = support.read_bytes()
    handle = kernel.create(
        CreateRequest(
            str(uuid.uuid4()),
            frozen_mapping(contract()),
            (
                ArtifactInput(
                    "support.txt",
                    str(support),
                    hashlib.sha256(data).hexdigest(),
                    len(data),
                    "text/plain",
                ),
            ),
        ),
        cap,
    )
    snapshot = kernel.inspect(handle.run_id)
    assert isinstance(snapshot, RunSnapshot)
    create_artifacts = snapshot.projection["artifacts"]
    support_id = next(
        item["artifact_id"] for item in create_artifacts if item["logical_name"] == "support.txt"
    )
    contract_id = next(
        item["artifact_id"] for item in create_artifacts if item["role"] == "CONTRACT"
    )
    normalized = contract()
    receipt = kernel.apply(
        ApplyRequest(
            str(uuid.uuid4()),
            handle.run_id,
            0,
            TypedCommand(
                "RegisterClaim",
                frozen_mapping(
                    {
                        "contract_version": 1,
                        "claim_kind": "ROOT",
                        "stable_label": "root",
                        "statement_artifact_id": contract_id,
                        "statement_hash": hashlib.sha256(
                            composition_json_bytes(normalized)
                        ).hexdigest(),
                        "normalized_statement": normalized,
                    }
                ),
            ),
        ),
        cap,
    )
    assert receipt.accepted and receipt.revision_after == 1
    receipt = kernel.apply(
        ApplyRequest(
            str(uuid.uuid4()),
            handle.run_id,
            1,
            TypedCommand(
                "FreezeContract",
                frozen_mapping(
                    {
                        "contract_version": 1,
                        "completeness_check_artifact_id": support_id,
                    }
                ),
            ),
        ),
        cap,
    )
    assert receipt.accepted and receipt.revision_after == 2 and len(receipt.event_ids) == 1
    engine.register_binding(
        object_kind=AuthorityObjectKind.CHECKPOINT,
        object_id="checkpoint-one",
        run_id=handle.run_id,
        contract_version=1,
        bound_revision=1,
        stable_label="checkpoint-current",
        object_digest="a" * 64,
    )
    intent = MappingProxyType(
        {
            "schema_version": "rk.authority_invalidation.v1",
            "reason": "KERNEL_AUTHORITY_CHANGED",
            "affected_objects": [
                {
                    "object_kind": "CHECKPOINT",
                    "object_id": "checkpoint-one",
                    "stable_label": "checkpoint-current",
                    "object_digest": "a" * 64,
                }
            ],
            "preserved_sibling_ids": [],
            "reopened_obligation_ids": [],
        }
    )
    orchestrator = ResearchOrchestrator(
        UnusedRuntime(), kernel=kernel, capability=cap, extensions=registry
    )
    uncommitted = AuthorityInvalidation(str(uuid.uuid4()), handle.run_id, 2, intent)
    with pytest.raises(RuntimeError, match="durably visible"):
        orchestrator.consume_kernel_invalidation(uncommitted)
    assert engine.watermark(handle.run_id).recorded_sequence == 0

    committed = AuthorityInvalidation(receipt.event_ids[0], handle.run_id, 2, intent)
    orchestrator.consume_kernel_invalidation(committed)

    assert engine.watermark(handle.run_id).caught_up
    assert (
        engine.get_binding(AuthorityObjectKind.CHECKPOINT, "checkpoint-one").state == "INVALIDATED"
    )
    orchestrator.consume_kernel_invalidation(committed)
    assert engine.watermark(handle.run_id).recorded_sequence == 1
