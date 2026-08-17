from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from rk.config import KernelConfig
from rk.domain import ArtifactInput, CreateRequest, RunSnapshot, VerifiedCapability, frozen_mapping
from rk.host_execution import HostExecutionNotAuthoritative, HostExecutionReceiptService
from rk.kernel import ResearchKernel
from rk.migrations import MigrationRunner
from rk.runtime import SystemClock, Uuid7Generator
from rk.storage import SQLiteStorage, StorageConflict
from rk.strategy import StrategyRunner
from rk.wire import canonical_json_bytes
from tests.test_kernel import (
    ROOT,
    _apply,
    _artifact,
    _capability,
    _contract,
)


class ReceiptFixtureAdapter:
    name = "fixture"
    version = "v1"

    def run(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        return {
            "status": "COMPLETED",
            "exit_code": 0,
            "echo": dict(request),
            "usage": {"input_tokens": 2, "output_tokens": 1},
        }


class UnknownCostFixtureAdapter:
    name = "fixture"
    version = "v1"

    def run(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        return {"status": "COMPLETED", "exit_code": 0, "usage": {"cost_unknown": True}}


class CountingFixtureAdapter(ReceiptFixtureAdapter):
    def __init__(self) -> None:
        self.calls = 0

    def run(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        self.calls += 1
        return super().run(request)


class RaisingFixtureAdapter:
    name = "fixture"
    version = "v1"

    def run(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        raise RuntimeError("provider crashed after the host claim")


class FailedFixtureAdapter:
    name = "fixture"
    version = "v1"

    def run(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        return {
            "status": "FAILED",
            "exit_code": 1,
            "error": "upstream rejected the request",
            "usage": {"input_tokens": 2, "output_tokens": 0},
        }


class LeanPassFixtureAdapter:
    name = "lean-replay"
    version = "v1"

    def __init__(self, artifact_sha256: str) -> None:
        self._artifact_sha256 = artifact_sha256

    def run(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        declaration_audit = {
            "target_theorem": {"owner": "target_module", "type": "True"}
        }
        return {
            "status": "COMPLETED",
            "exit_code": 0,
            "kernel_verdict": "REPLAY_PASS",
            "execution_mode": "reproducible/authoritative",
            "source_sha256": self._artifact_sha256,
            "output_sha256": self._artifact_sha256,
            "axiom_dependencies": [],
            "declaration_audit": declaration_audit,
            "declaration_module": "Main",
            "declaration_type_digest": hashlib.sha256(
                canonical_json_bytes(declaration_audit)
            ).hexdigest(),
            "usage": {},
        }


class CheckerPassFixtureAdapter:
    name = "fixture"
    version = "v1"

    def __init__(self, output_sha256: str, expected: object) -> None:
        self._output_sha256 = output_sha256
        self._expected = expected

    def run(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        return {
            "status": "COMPLETED",
            "exit_code": 0,
            "payload": self._expected,
            "output_sha256": self._output_sha256,
            "trust_limit": "HOST_CHECKED_CERTIFICATE",
            "usage": {},
        }


class BlockingFixtureAdapter(ReceiptFixtureAdapter):
    def __init__(self) -> None:
        self.started = threading.Event()
        self.release = threading.Event()

    def run(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        self.started.set()
        if not self.release.wait(5):
            raise RuntimeError("blocking fixture timed out")
        return super().run(request)


class FrozenClock:
    def __init__(self, value: datetime) -> None:
        self.value = value

    def now(self) -> datetime:
        return self.value


def _host_capability() -> VerifiedCapability:
    return VerifiedCapability(
        capability_id="00000000-0000-4000-8000-000000000099",
        subject_id="host-execution-service",
        issuer="test-host",
        allowed_actions=frozenset({"HostExecute"}),
        run_scope=frozenset({"*"}),
        issued_at="2020-01-01T00:00:00.000Z",
        expires_at="2100-01-01T00:00:00.000Z",
    )


def _write_host_key(path: Path) -> None:
    path.write_bytes(b"k" * 32)
    if os.name == "posix":
        path.chmod(0o600)


def _dependency_profile(tmp_path: Path) -> dict[str, str]:
    root = tmp_path / "dependency"
    root.mkdir(parents=True, exist_ok=True)
    olean = root / "Mathlib.olean"
    olean.write_bytes(b"pinned fixture olean")
    relative = olean.relative_to(root).as_posix().encode("utf-8")
    digest = hashlib.sha256()
    digest.update(len(relative).to_bytes(8, "big"))
    digest.update(relative)
    digest.update(hashlib.sha256(olean.read_bytes()).digest())
    manifest = tmp_path / "closure.json"
    manifest.write_text(
        json.dumps(
            {
                "schema_version": "rk.mathlib_closure_anchor.v1",
                "dependency_root_relpath": ".lake",
                "dependency_closure_sha256": digest.hexdigest(),
                "mathlib_commit": "a" * 40,
                "toolchain": "fixture-toolchain",
                "olean_files": {
                    olean.relative_to(root).as_posix(): hashlib.sha256(
                        olean.read_bytes()
                    ).hexdigest()
                },
            }
        ),
        encoding="utf-8",
    )
    return {
        "dependency_closure_root": str(root),
        "dependency_closure_sha256": digest.hexdigest(),
        "dependency_closure_manifest_path": str(manifest),
        "dependency_closure_manifest_sha256": hashlib.sha256(
            manifest.read_bytes()
        ).hexdigest(),
        "source_commit": "a" * 40,
        "toolchain": "fixture-toolchain",
    }


def test_dependency_anchor_rejects_tampering_and_unlisted_objects(tmp_path: Path) -> None:
    profile = _dependency_profile(tmp_path)
    root = Path(profile["dependency_closure_root"])
    assert HostExecutionReceiptService._dependency_closure_digest(profile) == profile[
        "dependency_closure_sha256"
    ]
    (root / "Mathlib.olean").write_bytes(b"tampered")
    with pytest.raises(StorageConflict, match="digest drifted"):
        HostExecutionReceiptService._dependency_closure_digest(profile)

    profile = _dependency_profile(tmp_path / "extra")
    root = Path(profile["dependency_closure_root"])
    extra = root / "RKLeanE2E/old/Main.olean"
    extra.parent.mkdir(parents=True)
    extra.write_bytes(b"stale generated object")
    with pytest.raises(StorageConflict, match="untrusted dependency object"):
        HostExecutionReceiptService._dependency_closure_digest(profile)

    profile = _dependency_profile(tmp_path / "manifest")
    Path(profile["dependency_closure_manifest_path"]).write_text("{}", encoding="utf-8")
    with pytest.raises(StorageConflict, match="manifest digest drifted"):
        HostExecutionReceiptService._dependency_closure_digest(profile)


def test_lean_receipt_consumption_rechecks_the_live_dependency_closure(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    profile = _dependency_profile(tmp_path)
    service = object.__new__(HostExecutionReceiptService)
    service._profiles = {"lean-replay": profile}
    service._clock = SystemClock()
    service._capability = _host_capability()
    service._revoked_capability_ids = frozenset()
    monkeypatch.setattr(
        service,
        "verify_receipt",
        lambda _receipt_id: {
            "adapter_name": "lean-replay",
            "dependency_closure_digest": profile["dependency_closure_sha256"],
            "payload": {"run_id": "00000000-0000-4000-8000-000000000001"},
            "signature": "fixture-signature",
        },
    )
    root = Path(profile["dependency_closure_root"])
    (root / "Mathlib.olean").write_bytes(b"changed after execution")

    with pytest.raises(StorageConflict, match="dependency object digest drifted"):
        service.consume_lean_replay(receipt_id="fixture-receipt")


def test_authority_environment_is_host_pinned_before_provider(tmp_path: Path) -> None:
    profile = {
        "execution_environment": {
            "PATH": "/trusted/toolchain/bin",
            "LEAN_PATH": "/trusted/mathlib/.lake/build/lib/lean",
        }
    }
    request = {"environment": dict(profile["execution_environment"]), "source_relpath": "X.lean"}
    assert HostExecutionReceiptService._host_owned_request(
        "lean-replay", profile, request
    )["environment"] == profile["execution_environment"]
    with pytest.raises(StorageConflict, match="does not match"):
        HostExecutionReceiptService._host_owned_request(
            "lean-replay",
            profile,
            {**request, "environment": {"PATH": "/evil", "LEAN_PATH": "/evil"}},
        )


def test_lean_profile_is_pinned_to_the_exact_claim_statement() -> None:
    scope = {
        "adapter_name": "lean-replay",
        "adapter_version": "v1",
        "environment_profile_id": "lean-clean",
        "source_commit": "a" * 40,
        "statement_hash": "b" * 64,
    }
    profile = {
        "adapter_version": "v1",
        "environment_profile_id": "lean-clean",
        "source_commit": "a" * 40,
        "expected_statement_hash": "c" * 64,
    }
    with pytest.raises(StorageConflict, match="not pinned"):
        HostExecutionReceiptService._validate_profile(scope, profile)


@pytest.mark.parametrize(
    ("result", "message"),
    [
        ({"status": "COMPLETED", "exit_code": 0}, "kernel pass verdict"),
        (
            {
                "kernel_verdict": "REJECTED",
                "status": "COMPLETED",
                "exit_code": 0,
                "source_sha256": "a" * 64,
                "output_sha256": "b" * 64,
                "axiom_dependencies": [],
            },
            "kernel pass verdict",
        ),
        (
            {
                "kernel_verdict": "REPLAY_PASS",
                "status": "COMPLETED",
                "exit_code": 0,
                "source_sha256": "a" * 64,
                "output_sha256": "b" * 64,
                "axiom_dependencies": ["evilAxiom"],
                "declaration_audit": {
                    "t": {"owner": "target_module", "type": "True"}
                },
                "declaration_module": "Main",
                "declaration_type_digest": hashlib.sha256(
                    b'{"t":{"owner":"target_module","type":"True"}}'
                ).hexdigest(),
            },
            "untrusted axiom",
        ),
        (
            {
                "kernel_verdict": "REPLAY_PASS",
                "status": "COMPLETED",
                "exit_code": 0,
                "source_sha256": "a" * 64,
                "output_sha256": "b" * 64,
                "axiom_dependencies": [],
                "declaration_audit": {
                    "t": {"owner": "target_module", "type": "False"}
                },
                "declaration_module": "Main",
                "declaration_type_digest": hashlib.sha256(
                    b'{"t":{"owner":"target_module","type":"False"}}'
                ).hexdigest(),
            },
            "types do not match",
        ),
    ],
)
def test_lean_authority_requires_kernel_verdict_and_allowed_axioms(
    result: Mapping[str, Any], message: str
) -> None:
    with pytest.raises(StorageConflict, match=message):
        HostExecutionReceiptService._authority_result(
            "lean-replay",
            {
                "allowed_axioms": ["propext"],
                "expected_declaration_types": {"t": "True"},
                "expected_declaration_module": "Main",
            },
            {**result, "execution_mode": "reproducible/authoritative"},
        )


def test_exploratory_lean_result_cannot_issue_authority() -> None:
    with pytest.raises(StorageConflict, match="exploratory Lean replay"):
        HostExecutionReceiptService._authority_result(
            "lean-replay",
            {
                "allowed_axioms": [],
                "expected_declaration_types": {"t": "True"},
                "expected_declaration_module": "Main",
            },
            {
                "status": "COMPLETED",
                "exit_code": 0,
                "execution_mode": "bootstrap/exploratory",
                "kernel_verdict": "REPLAY_PASS",
            },
        )


def test_token_meter_requires_both_input_and_output_reservations(tmp_path: Path) -> None:
    _kernel, config, run_id, attempt_id = _ready(tmp_path, reserve_output=False)
    key = tmp_path / "host.key"
    _write_host_key(key)
    adapter = CountingFixtureAdapter()
    service = HostExecutionReceiptService(
        storage=SQLiteStorage(config.db_path, 5_000),
        strategy=StrategyRunner({"fixture": adapter}), signing_key_path=key,
        capability=_host_capability(), id_generator=Uuid7Generator(), clock=SystemClock(),
        host_profiles={"fixture": {
            "adapter_version": "v1", "environment_profile_id": "fixture-clean",
            "source_commit": "a" * 40, "token_meter_applicable": True,
        }}, budget_limits={
            "WALL_SECOND": 100_000_000,
            "INPUT_TOKEN": 100_000_000,
            "OUTPUT_TOKEN": 100_000_000,
        },
    )
    with pytest.raises(StorageConflict, match="OUTPUT_TOKEN reservation"):
        service.execute(run_id=run_id, attempt_id=attempt_id, request={"x": 1})
    assert adapter.calls == 0


def _ready(
    tmp_path: Path,
    *,
    reserve_output: bool = True,
    operator: VerifiedCapability | None = None,
) -> tuple[ResearchKernel, KernelConfig, str, str]:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    support = inbox / "support.txt"
    support.write_text("contract and invocation\n", encoding="utf-8")
    operator = operator or _capability()
    config = KernelConfig.from_mapping(
        {
            "workspace_root": str(tmp_path / "state"),
            "inbox_roots": [str(inbox)],
            "command_schema_path": str(ROOT / "docs/spec/json/command.schema.json"),
            "receipt_schema_path": str(ROOT / "docs/spec/json/receipt.schema.json"),
            "adapter_profiles": {
                "fixture": {
                    "versions": {
                        "v1": {
                            "environment_profile_ids": ["fixture-clean"],
                            "source_commits": ["a" * 40],
                        }
                    }
                },
                "lean-replay": {
                    "versions": {
                        "v1": {
                            "environment_profile_ids": ["lean-clean"],
                            "source_commits": ["a" * 40],
                        }
                    }
                },
            },
            "budget_policy": {
                "budget_controller_capability_ids": [operator.capability_id],
                "global_budget_limits": {
                    "WALL_SECOND": 1_000_000_000,
                    "INPUT_TOKEN": 1_000_000_000,
                    "OUTPUT_TOKEN": 1_000_000_000,
                },
            },
        },
        base=ROOT,
    )
    kernel = ResearchKernel.from_config(config, migrations_dir=ROOT / "migrations")
    handle = kernel.create(
        CreateRequest(
            request_id="00000000-0000-4000-8000-000000000001",
            contract=frozen_mapping(_contract()),
            artifact_inputs=(_artifact(support, "support.txt"),),
        ),
        operator,
    )
    snapshot = kernel.inspect(handle.run_id)
    assert isinstance(snapshot, RunSnapshot)
    support_id = next(
        item["artifact_id"] for item in snapshot.projection["artifacts"]
        if item["logical_name"] == "support.txt"
    )
    contract_id = next(
        item["artifact_id"] for item in snapshot.projection["artifacts"]
        if item["role"] == "CONTRACT"
    )
    normalized = _contract()
    statement_hash = hashlib.sha256(canonical_json_bytes(normalized)).hexdigest()
    assert _apply(kernel, operator, handle.run_id, 0, "RegisterClaim", {
        "contract_version": 1, "claim_kind": "ROOT", "stable_label": "root",
        "statement_artifact_id": contract_id, "statement_hash": statement_hash,
        "normalized_statement": normalized,
    }).accepted
    snapshot = kernel.inspect(handle.run_id)
    claim_id = str(snapshot.projection["root_claim_id"])
    assert _apply(kernel, operator, handle.run_id, 1, "FreezeContract", {
        "contract_version": 1, "completeness_check_artifact_id": support_id,
    }).accepted
    assert _apply(kernel, operator, handle.run_id, 2, "StartRun", {
        "contract_version": 1, "literature_plan_artifact_id": support_id,
        "budget_policy": {"global": {"WALL_SECOND": 100}},
    }).accepted
    assert _apply(kernel, operator, handle.run_id, 3, "RegisterRoute", {
        "contract_version": 1, "target_claim_id": claim_id, "label": "fixture-route",
        "representation": "test", "tool_family": "fixture",
        "approach_root": {"kind": "fixture"}, "budget_policy": {"attempts": 1},
    }).accepted
    snapshot = kernel.inspect(handle.run_id)
    route_id = str(snapshot.projection["routes"][-1]["route_id"])
    assert _apply(kernel, operator, handle.run_id, 4, "RegisterAttempt", {
        "route_id": route_id, "ordinal": 1, "isolation_epoch": 1,
        "work_relpath": "attempts/1/work", "allowed_write_set": ["attempt-1"],
        "input_snapshot_digest": "b" * 64,
    }).accepted
    snapshot = kernel.inspect(handle.run_id)
    attempt_id = str(snapshot.projection["active_attempts"][-1]["attempt_id"])
    assert _apply(kernel, operator, handle.run_id, 5, "BindExecution", {
        "route_id": route_id, "attempt_id": attempt_id, "adapter_name": "fixture",
        "adapter_version": "v1", "source_commit": "a" * 40,
        "environment_profile_id": "fixture-clean", "invocation_artifact_id": support_id,
        "external_ids": {},
    }).accepted
    assert _apply(kernel, operator, handle.run_id, 6, "AcquireLease", {
        "attempt_id": attempt_id, "holder_id": "fixture-holder", "ttl_seconds": 900,
    }).accepted
    reservations = [
        (7, "WALL_SECOND", 100_000_000),
        (8, "INPUT_TOKEN", 1_000_000),
    ]
    if reserve_output:
        reservations.append((9, "OUTPUT_TOKEN", 1_000_000))
    for revision, resource, amount in reservations:
        assert _apply(kernel, operator, handle.run_id, revision, "RecordBudget", {
            "route_id": route_id, "attempt_id": attempt_id,
            "event_kind": "RESERVATION", "resource_kind": resource,
            "amount_microunits": amount, "unit": "microunit",
            "provider_usage": {"component": "fixture", "preflight": True},
        }).accepted
    return kernel, config, handle.run_id, attempt_id


def test_host_service_derives_scope_signs_and_records_actual_usage(tmp_path: Path) -> None:
    kernel, config, run_id, attempt_id = _ready(tmp_path)
    key = tmp_path / "host.key"
    _write_host_key(key)
    service = HostExecutionReceiptService(
        storage=SQLiteStorage(config.db_path, 5_000),
        strategy=StrategyRunner({"fixture": ReceiptFixtureAdapter()}),
        signing_key_path=key,
        capability=_host_capability(),
        id_generator=Uuid7Generator(), clock=SystemClock(),
        host_profiles={"fixture": {
            "adapter_version": "v1", "environment_profile_id": "fixture-clean",
            "source_commit": "a" * 40, "component": "fixture-model",
            "token_meter_applicable": True,
            "currency_meter_applicable": True,
            "mounts": {}, "process": {},
        }},
        budget_limits={
            "WALL_SECOND": 100_000_000, "INPUT_TOKEN": 10_000_000,
            "OUTPUT_TOKEN": 10_000_000,
        },
    )
    executed = service.execute(run_id=run_id, attempt_id=attempt_id, request={"x": 1})
    snapshot = kernel.inspect(run_id)
    assert isinstance(snapshot, RunSnapshot)
    receipt = snapshot.projection["host_execution_receipts"][0]
    assert receipt["receipt_id"] == executed.receipt_id
    assert receipt["claim_id"] == snapshot.projection["root_claim_id"]
    assert snapshot.projection["component_usage"]["fixture-model"]["input_tokens"] == 2
    assert snapshot.projection["component_usage"]["fixture-model"]["output_tokens"] == 1
    assert snapshot.projection["component_usage"]["fixture-model"]["unknown_count"] == 1
    with sqlite3.connect(config.db_path) as connection:
        trust = connection.execute(
            "SELECT DISTINCT json_extract(provider_usage_json,'$._rk_trust') "
            "FROM budget_events WHERE event_kind IN ('ACTUAL','UNKNOWN_COST')"
        ).fetchall()
    assert trust == [("HOST_VERIFIED",)]


def test_host_lean_promotes_a_non_root_claim_in_the_same_truth_graph(tmp_path: Path) -> None:
    operator = _capability()
    kernel, config, run_id, _root_attempt_id = _ready(tmp_path, operator=operator)
    snapshot = kernel.inspect(run_id)
    statement = {"atomic": True, "statement": "0 + n = n", "proof": "by simp"}
    statement_path = config.inbox_roots[0] / "lemma.json"
    statement_path.write_bytes(canonical_json_bytes(statement))
    data = statement_path.read_bytes()
    statement_input = ArtifactInput(
        "lemma.json",
        str(statement_path),
        hashlib.sha256(data).hexdigest(),
        len(data),
        "application/json",
    )
    statement_artifact = kernel.import_artifact(
        run_id,
        statement_input,
        operator,
        logical_name="lean-lemma",
        role="CLAIM_STATEMENT",
    )
    revision = snapshot.revision

    def apply(kind: str, payload: dict[str, object]):
        nonlocal revision
        receipt = _apply(kernel, operator, run_id, revision, kind, payload)
        assert receipt.accepted, (kind, receipt.rejection_code, receipt.missing_conditions)
        revision = receipt.revision_after

    apply(
        "RegisterClaim",
        {
            "contract_version": 1,
            "claim_kind": "LEMMA",
            "stable_label": "lean-subclaim",
            "statement_artifact_id": statement_artifact.artifact_id,
            "statement_hash": hashlib.sha256(data).hexdigest(),
            "normalized_statement": statement,
        },
    )
    lemma_id = str(
        next(
            item["claim_id"]
            for item in kernel.inspect(run_id).projection["claims"]
            if item["stable_label"] == "lean-subclaim"
        )
    )
    apply(
        "RegisterRoute",
        {
            "contract_version": 1,
            "target_claim_id": lemma_id,
            "label": "lean-subclaim-route",
            "representation": "Lean theorem",
            "tool_family": "lean-replay",
            "approach_root": {"kind": "formalization"},
            "budget_policy": {"attempts": 1},
        },
    )
    route_id = str(kernel.inspect(run_id).projection["routes"][-1]["route_id"])
    apply(
        "RegisterAttempt",
        {
            "route_id": route_id,
            "ordinal": 1,
            "isolation_epoch": 2,
            "work_relpath": "attempts/lean-subclaim/work",
            "allowed_write_set": ["lean-subclaim"],
            "input_snapshot_digest": "c" * 64,
        },
    )
    attempt_id = str(kernel.inspect(run_id).projection["active_attempts"][-1]["attempt_id"])
    apply(
        "BindExecution",
        {
            "route_id": route_id,
            "attempt_id": attempt_id,
            "adapter_name": "lean-replay",
            "adapter_version": "v1",
            "source_commit": "a" * 40,
            "environment_profile_id": "lean-clean",
            "invocation_artifact_id": statement_artifact.artifact_id,
            "external_ids": {},
        },
    )
    apply(
        "AcquireLease",
        {"attempt_id": attempt_id, "holder_id": "lean-host", "ttl_seconds": 900},
    )
    apply(
        "RecordBudget",
        {
            "route_id": route_id,
            "attempt_id": attempt_id,
            "event_kind": "RESERVATION",
            "resource_kind": "WALL_SECOND",
            "amount_microunits": 100_000_000,
            "unit": "microunit",
            "provider_usage": {"component": "lean-replay", "preflight": True},
        },
    )
    key = tmp_path / "lean-host.key"
    _write_host_key(key)
    dependency = _dependency_profile(tmp_path / "lean-dependency")
    profile = {
        **dependency,
        "adapter_version": "v1",
        "environment_profile_id": "lean-clean",
        "component": "lean-replay",
        "mounts": {},
        "process": {},
        "execution_environment": {"PATH": "fixture", "LEAN_PATH": "fixture"},
        "expected_statement_hash": hashlib.sha256(data).hexdigest(),
        "allowed_axioms": [],
        "expected_declaration_types": {"target_theorem": "True"},
        "expected_declaration_module": "Main",
    }
    service = HostExecutionReceiptService(
        storage=SQLiteStorage(config.db_path, 5_000),
        strategy=StrategyRunner(
            {"lean-replay": LeanPassFixtureAdapter(statement_artifact.sha256)}
        ),
        signing_key_path=key,
        capability=_host_capability(),
        id_generator=Uuid7Generator(),
        clock=SystemClock(),
        host_profiles={"lean-replay": profile},
        budget_limits={"WALL_SECOND": 1_000_000_000},
    )
    executed = service.execute(
        run_id=run_id,
        attempt_id=attempt_id,
        request={"environment": profile["execution_environment"]},
    )
    service.consume_lean_replay(receipt_id=executed.receipt_id)
    result = kernel.inspect(run_id).projection
    lemma = next(item for item in result["claims"] if item["claim_id"] == lemma_id)
    assert lemma["machine"] == "KERNEL_VERIFIED"
    evidence = next(item for item in result["evidence"] if item["claim_id"] == lemma_id)
    assert evidence["evidence_type"] == "LEAN_REPLAY"
    assert evidence["trust_class"] == "HOST_VERIFIED_EVIDENCE"


@pytest.mark.parametrize("capability_kind", ["SMT", "EXACT_ENUMERATION"])
def test_host_checker_promotes_smt_and_enumeration_but_not_cas(
    tmp_path: Path, capability_kind: str
) -> None:
    kernel, config, run_id, attempt_id = _ready(tmp_path)
    snapshot = kernel.inspect(run_id)
    root_id = str(snapshot.projection["root_claim_id"])
    root = next(item for item in snapshot.projection["claims"] if item["claim_id"] == root_id)
    invocation = next(
        item for item in snapshot.projection["artifacts"] if item["logical_name"] == "support.txt"
    )
    key = tmp_path / "checker-host.key"
    _write_host_key(key)
    profile = {
        "adapter_version": "v1",
        "environment_profile_id": "fixture-clean",
        "source_commit": "a" * 40,
        "component": capability_kind.lower(),
        "mounts": {},
        "process": {},
        "authority_mode": "CHECKER_CERTIFICATE",
        "capability_kind": capability_kind,
        "expected_result": "unsat" if capability_kind == "SMT" else {"checked": 256},
        "expected_statement_hash": root["statement_hash"],
    }
    service = HostExecutionReceiptService(
        storage=SQLiteStorage(config.db_path, 5_000),
        strategy=StrategyRunner(
            {
                "fixture": CheckerPassFixtureAdapter(
                    str(invocation["sha256"]), profile["expected_result"]
                )
            }
        ),
        signing_key_path=key,
        capability=_host_capability(),
        id_generator=Uuid7Generator(),
        clock=SystemClock(),
        host_profiles={"fixture": profile},
        budget_limits={"WALL_SECOND": 1_000_000_000},
    )
    executed = service.execute(run_id=run_id, attempt_id=attempt_id, request={"x": 1})
    service.consume_checker_result(receipt_id=executed.receipt_id)
    result = kernel.inspect(run_id).projection
    root = next(item for item in result["claims"] if item["claim_id"] == root_id)
    assert root["machine"] == "CERTIFICATE_VERIFIED"
    assert root["semantic"] == "TESTED"
    verification = result["atomic_verifications"][-1]
    assert verification["backend"] == "DETERMINISTIC_CHECKER"


def test_cas_result_is_observable_but_cannot_be_consumed_as_certificate(tmp_path: Path) -> None:
    _kernel, config, run_id, attempt_id = _ready(tmp_path)
    key = tmp_path / "cas-host.key"
    _write_host_key(key)
    profile = {
        "adapter_version": "v1",
        "environment_profile_id": "fixture-clean",
        "source_commit": "a" * 40,
        "component": "cas",
        "mounts": {},
        "process": {},
        "authority_mode": "CHECKER_CERTIFICATE",
        "capability_kind": "CAS",
        "expected_result": {"simplified": "x^2"},
        "expected_statement_hash": "unused",
    }
    service = HostExecutionReceiptService(
        storage=SQLiteStorage(config.db_path, 5_000),
        strategy=StrategyRunner(
            {
                "fixture": CheckerPassFixtureAdapter(
                    "a" * 64, profile["expected_result"]
                )
            }
        ),
        signing_key_path=key,
        capability=_host_capability(),
        id_generator=Uuid7Generator(),
        clock=SystemClock(),
        host_profiles={"fixture": profile},
        budget_limits={"WALL_SECOND": 1_000_000_000},
    )
    with pytest.raises(StorageConflict, match=r"not pinned|only SMT"):
        service.execute(run_id=run_id, attempt_id=attempt_id, request={"x": 1})


def test_host_service_rejects_fake_profile_and_scope_drift(tmp_path: Path) -> None:
    _kernel, config, run_id, attempt_id = _ready(tmp_path)
    key = tmp_path / "host.key"
    _write_host_key(key)
    service = HostExecutionReceiptService(
        storage=SQLiteStorage(config.db_path, 5_000),
        strategy=StrategyRunner({"fixture": ReceiptFixtureAdapter()}),
        signing_key_path=key, capability=_host_capability(), id_generator=Uuid7Generator(),
        clock=SystemClock(), host_profiles={"fixture": {
            "adapter_version": "v1", "environment_profile_id": "fake-environment",
            "source_commit": "a" * 40,
        }}, budget_limits={},
    )
    with pytest.raises(StorageConflict, match="does not match"):
        service.execute(run_id=run_id, attempt_id=attempt_id, request={"x": 1})
    with sqlite3.connect(config.db_path) as connection:
        connection.execute(
            "UPDATE claims SET statement_hash=? WHERE claim_kind='ROOT'", ("9" * 64,)
        )
    with pytest.raises(StorageConflict):
        service.execute(run_id=run_id, attempt_id=attempt_id, request={"x": 1})


def test_host_receipt_nonce_and_consumption_are_one_shot(tmp_path: Path) -> None:
    migrations = tmp_path / "migrations"
    migrations.mkdir()
    for source in (ROOT / "migrations").glob("*.sql"):
        (migrations / source.name).write_bytes(source.read_bytes())
    db = tmp_path / "empty.sqlite"
    MigrationRunner(db, migrations, 5_000).migrate()
    with sqlite3.connect(db) as connection:
        indexes = {
            row[1]
            for row in connection.execute("PRAGMA index_list(host_execution_receipts)")
        }
    assert any(name.startswith("sqlite_autoindex_host_execution_receipts_") for name in indexes)


def test_same_attempt_cannot_execute_twice(tmp_path: Path) -> None:
    _kernel, config, run_id, attempt_id = _ready(tmp_path)
    key = tmp_path / "host.key"
    _write_host_key(key)
    adapter = CountingFixtureAdapter()
    service = HostExecutionReceiptService(
        storage=SQLiteStorage(config.db_path, 5_000),
        strategy=StrategyRunner({"fixture": adapter}),
        signing_key_path=key, capability=_host_capability(), id_generator=Uuid7Generator(),
        clock=SystemClock(), host_profiles={"fixture": {
            "adapter_version": "v1", "environment_profile_id": "fixture-clean",
            "source_commit": "a" * 40,
        }}, budget_limits={"WALL_SECOND": 100_000_000},
    )
    service.execute(run_id=run_id, attempt_id=attempt_id, request={"x": 1})
    with pytest.raises(StorageConflict):
        service.execute(run_id=run_id, attempt_id=attempt_id, request={"x": 1})
    assert adapter.calls == 1


def test_incomplete_claim_recovery_is_idempotent_and_reports_unknown(tmp_path: Path) -> None:
    kernel, config, run_id, attempt_id = _ready(tmp_path)
    storage = SQLiteStorage(config.db_path, 5_000)
    scope = storage.host_execution_scope(
        run_id=run_id, attempt_id=attempt_id, now="2026-08-12T00:00:00.000Z"
    )
    storage.claim_host_execution(
        scope=scope, capability=_host_capability(), claim_token="f" * 64,
        request_hash="e" * 64, component="fixture", service_instance_id="host-a",
        now="2026-08-12T00:00:00.000Z", recover_after="2026-08-12T00:00:10.000Z",
    )
    assert storage.recover_incomplete_host_claims(
        capability=_host_capability(), now="2026-08-12T00:00:01.000Z"
    ) == ()
    assert storage.recover_incomplete_host_claims(
        capability=_host_capability(), now="2026-08-12T00:00:10.000Z"
    ) == (attempt_id,)
    assert storage.recover_incomplete_host_claims(
        capability=_host_capability(), now="2026-08-12T00:00:02.000Z"
    ) == ()
    snapshot = kernel.inspect(run_id)
    assert snapshot.projection["component_usage"]["fixture"]["unknown_count"] == 1
    assert snapshot.revision == 11
    with sqlite3.connect(config.db_path) as connection:
        assert connection.execute(
            "SELECT status FROM attempts WHERE attempt_id=?", (attempt_id,)
        ).fetchone() == ("ENVIRONMENT_ERROR",)
        assert connection.execute(
            "SELECT status FROM leases WHERE attempt_id=?", (attempt_id,)
        ).fetchone() == ("REVOKED",)
        command = connection.execute(
            "SELECT command_type,revision_before,revision_after,receipt_json FROM commands "
            "WHERE command_type='HostRecoverExecution'"
        ).fetchone()
        assert command is not None
        assert command[0:3] == ("HostRecoverExecution", 10, 11)
        receipt = json.loads(command[3])
        assert receipt["accepted"] is True
        assert receipt["revision_after"] == 11


def test_service_start_recovers_only_expired_claims(tmp_path: Path) -> None:
    kernel, config, run_id, attempt_id = _ready(tmp_path)
    storage = SQLiteStorage(config.db_path, 5_000)
    scope = storage.host_execution_scope(
        run_id=run_id, attempt_id=attempt_id, now="2026-08-12T00:00:00.000Z"
    )
    storage.claim_host_execution(
        scope=scope, capability=_host_capability(), claim_token="d" * 64,
        request_hash="c" * 64, component="fixture", service_instance_id="dead-host",
        now="2026-08-12T00:00:00.000Z", recover_after="2026-08-12T00:01:00.000Z",
    )
    key = tmp_path / "host.key"
    _write_host_key(key)
    HostExecutionReceiptService(
        storage=storage, strategy=StrategyRunner({"fixture": ReceiptFixtureAdapter()}),
        signing_key_path=key, capability=_host_capability(), id_generator=Uuid7Generator(),
        clock=FrozenClock(datetime(2026, 8, 12, 0, 0, 30, tzinfo=UTC)),
        host_profiles={"fixture": {
            "adapter_version": "v1", "environment_profile_id": "fixture-clean",
            "source_commit": "a" * 40,
        }}, budget_limits={"WALL_SECOND": 100_000_000},
    )
    assert kernel.inspect(run_id).projection["component_usage"] == {}
    HostExecutionReceiptService(
        storage=storage, strategy=StrategyRunner({"fixture": ReceiptFixtureAdapter()}),
        signing_key_path=key, capability=_host_capability(), id_generator=Uuid7Generator(),
        clock=FrozenClock(datetime(2026, 8, 12, 0, 1, tzinfo=UTC)),
        host_profiles={"fixture": {
            "adapter_version": "v1", "environment_profile_id": "fixture-clean",
            "source_commit": "a" * 40,
        }}, budget_limits={"WALL_SECOND": 100_000_000},
    )
    assert kernel.inspect(run_id).projection["component_usage"]["fixture"][
        "unknown_count"
    ] == 1


def test_live_heartbeat_prevents_another_host_from_recovering(tmp_path: Path) -> None:
    kernel, config, run_id, attempt_id = _ready(tmp_path)
    key = tmp_path / "host.key"
    _write_host_key(key)
    adapter = BlockingFixtureAdapter()
    profile = {
        "adapter_version": "v1", "environment_profile_id": "fixture-clean",
        "source_commit": "a" * 40, "recovery_timeout_seconds": 0.15,
    }
    service = HostExecutionReceiptService(
        storage=SQLiteStorage(config.db_path, 5_000),
        strategy=StrategyRunner({"fixture": adapter}), signing_key_path=key,
        capability=_host_capability(), id_generator=Uuid7Generator(), clock=SystemClock(),
        host_profiles={"fixture": profile}, budget_limits={"WALL_SECOND": 100_000_000},
    )
    errors: list[BaseException] = []

    def execute() -> None:
        try:
            service.execute(run_id=run_id, attempt_id=attempt_id, request={"x": 1})
        except BaseException as error:
            errors.append(error)

    worker = threading.Thread(target=execute)
    worker.start()
    assert adapter.started.wait(2)
    time.sleep(0.3)
    HostExecutionReceiptService(
        storage=SQLiteStorage(config.db_path, 5_000),
        strategy=StrategyRunner({"fixture": ReceiptFixtureAdapter()}), signing_key_path=key,
        capability=_host_capability(), id_generator=Uuid7Generator(), clock=SystemClock(),
        host_profiles={"fixture": profile}, budget_limits={"WALL_SECOND": 100_000_000},
    )
    assert kernel.inspect(run_id).projection["active_attempts"][0]["status"] == "RUNNING"
    adapter.release.set()
    worker.join(5)
    assert not worker.is_alive()
    assert errors == []
    assert len(kernel.inspect(run_id).projection["host_execution_receipts"]) == 1


def test_adapter_exception_is_recovered_without_silent_retry(tmp_path: Path) -> None:
    kernel, config, run_id, attempt_id = _ready(tmp_path)
    key = tmp_path / "host.key"
    _write_host_key(key)
    service = HostExecutionReceiptService(
        storage=SQLiteStorage(config.db_path, 5_000),
        strategy=StrategyRunner({"fixture": RaisingFixtureAdapter()}),
        signing_key_path=key, capability=_host_capability(), id_generator=Uuid7Generator(),
        clock=SystemClock(), host_profiles={"fixture": {
            "adapter_version": "v1", "environment_profile_id": "fixture-clean",
            "source_commit": "a" * 40,
        }}, budget_limits={"WALL_SECOND": 100_000_000},
    )
    with pytest.raises(RuntimeError, match="provider crashed"):
        service.execute(run_id=run_id, attempt_id=attempt_id, request={"x": 1})
    assert kernel.inspect(run_id).projection["component_usage"]["fixture"][
        "unknown_count"
    ] == 1
    with pytest.raises(StorageConflict, match="active canonical-root"):
        service.execute(run_id=run_id, attempt_id=attempt_id, request={"x": 1})


def test_post_call_budget_overrun_is_recorded_and_blocks_authority(tmp_path: Path) -> None:
    kernel, config, run_id, attempt_id = _ready(tmp_path)
    key = tmp_path / "host.key"
    _write_host_key(key)
    service = HostExecutionReceiptService(
        storage=SQLiteStorage(config.db_path, 5_000),
        strategy=StrategyRunner({"fixture": ReceiptFixtureAdapter()}),
        signing_key_path=key, capability=_host_capability(), id_generator=Uuid7Generator(),
        clock=SystemClock(), host_profiles={"fixture": {
            "adapter_version": "v1", "environment_profile_id": "fixture-clean",
            "source_commit": "a" * 40, "component": "fixture-model",
            "token_meter_applicable": True,
        }}, budget_limits={
            "WALL_SECOND": 100_000_000,
            "INPUT_TOKEN": 1_000_000,
            "OUTPUT_TOKEN": 1_000_000,
        },
    )
    with pytest.raises(HostExecutionNotAuthoritative) as error:
        service.execute(run_id=run_id, attempt_id=attempt_id, request={"x": 1})
    snapshot = kernel.inspect(run_id)
    assert len(snapshot.projection["host_execution_receipts"]) == 1
    receipt = snapshot.projection["host_execution_receipts"][0]
    assert receipt["authority_eligible"] == 0
    assert "BUDGET_OVERRUN:INPUT_TOKEN" in receipt["block_reasons"]
    assert snapshot.projection["component_usage"]["fixture-model"]["input_tokens"] == 2
    with sqlite3.connect(config.db_path) as connection:
        assert connection.execute(
            "SELECT COUNT(*) FROM budget_events WHERE event_kind='FUSE_TRIP'"
        ).fetchone() == (1,)
    assert error.value.receipt_id == receipt["receipt_id"]


def test_failed_provider_receipt_is_recorded_but_never_authoritative(
    tmp_path: Path,
) -> None:
    kernel, config, run_id, attempt_id = _ready(tmp_path)
    key = tmp_path / "host.key"
    _write_host_key(key)
    service = HostExecutionReceiptService(
        storage=SQLiteStorage(config.db_path, 5_000),
        strategy=StrategyRunner({"fixture": FailedFixtureAdapter()}),
        signing_key_path=key,
        capability=_host_capability(),
        id_generator=Uuid7Generator(),
        clock=SystemClock(),
        host_profiles={"fixture": {
            "adapter_version": "v1",
            "environment_profile_id": "fixture-clean",
            "source_commit": "a" * 40,
            "component": "fixture-model",
            "token_meter_applicable": True,
        }},
        budget_limits={
            "WALL_SECOND": 100_000_000,
            "INPUT_TOKEN": 1_000_000,
            "OUTPUT_TOKEN": 1_000_000,
        },
    )
    with pytest.raises(HostExecutionNotAuthoritative) as error:
        service.execute(run_id=run_id, attempt_id=attempt_id, request={"x": 1})
    snapshot = kernel.inspect(run_id)
    receipt = snapshot.projection["host_execution_receipts"][0]
    assert receipt["authority_eligible"] == 0
    assert receipt["status"] == "FAILED"
    assert "EXECUTION_NOT_SUCCESSFUL:FAILED:1" in receipt["block_reasons"]
    assert snapshot.projection["component_usage"]["fixture-model"][
        "input_tokens"
    ] == 2
    assert error.value.invocation.status == "FAILED"
    assert error.value.invocation.result_hash == receipt["result_hash"]
    assert error.value.invocation.result["error"] == "upstream rejected the request"
    assert error.value.receipt_id == receipt["receipt_id"]


def test_host_capability_scope_and_expired_lease_fail_before_adapter(tmp_path: Path) -> None:
    _kernel, config, run_id, attempt_id = _ready(tmp_path)
    key = tmp_path / "host.key"
    _write_host_key(key)
    scoped_out = VerifiedCapability(
        capability_id="00000000-0000-4000-8000-000000000098",
        subject_id="host", issuer="test-host", allowed_actions=frozenset({"HostExecute"}),
        run_scope=frozenset({"00000000-0000-4000-8000-000000000001"}),
        issued_at="2020-01-01T00:00:00.000Z", expires_at="2100-01-01T00:00:00.000Z",
    )
    service = HostExecutionReceiptService(
        storage=SQLiteStorage(config.db_path, 5_000),
        strategy=StrategyRunner({"fixture": ReceiptFixtureAdapter()}),
        signing_key_path=key, capability=scoped_out, id_generator=Uuid7Generator(),
        clock=SystemClock(), host_profiles={"fixture": {
            "adapter_version": "v1", "environment_profile_id": "fixture-clean",
            "source_commit": "a" * 40,
        }}, budget_limits={"WALL_SECOND": 100_000_000},
    )
    with pytest.raises(StorageConflict, match="outside this run scope"):
        service.execute(run_id=run_id, attempt_id=attempt_id, request={"x": 1})
    with sqlite3.connect(config.db_path) as connection:
        connection.execute(
            "UPDATE leases SET acquired_at='2019-01-01T00:00:00Z',"
            "heartbeat_at='2019-01-01T00:00:00Z',expires_at='2020-01-01T00:00:01Z'"
        )
    valid = HostExecutionReceiptService(
        storage=SQLiteStorage(config.db_path, 5_000),
        strategy=StrategyRunner({"fixture": ReceiptFixtureAdapter()}),
        signing_key_path=key, capability=_host_capability(), id_generator=Uuid7Generator(),
        clock=SystemClock(), host_profiles={"fixture": {
            "adapter_version": "v1", "environment_profile_id": "fixture-clean",
            "source_commit": "a" * 40,
        }}, budget_limits={"WALL_SECOND": 100_000_000},
    )
    with pytest.raises(StorageConflict, match="active canonical-root"):
        valid.execute(run_id=run_id, attempt_id=attempt_id, request={"x": 1})


@pytest.mark.parametrize("mode", ["expired", "not-yet-valid", "revoked"])
def test_invalid_host_capability_fails_before_provider(
    tmp_path: Path, mode: str
) -> None:
    _kernel, config, run_id, _attempt_id = _ready(tmp_path)
    key = tmp_path / "host.key"
    _write_host_key(key)
    adapter = CountingFixtureAdapter()
    issued = "2020-01-01T00:00:00.000Z"
    expires = "2100-01-01T00:00:00.000Z"
    if mode == "expired":
        expires = "2026-08-11T00:00:00.000Z"
    elif mode == "not-yet-valid":
        issued = "2026-08-13T00:00:00.000Z"
    capability = VerifiedCapability(
        capability_id="00000000-0000-4000-8000-000000000097",
        subject_id="host", issuer="test-host", allowed_actions=frozenset({"HostExecute"}),
        run_scope=frozenset({run_id}), issued_at=issued, expires_at=expires,
    )
    with pytest.raises(StorageConflict, match="outside this run scope"):
        HostExecutionReceiptService(
            storage=SQLiteStorage(config.db_path, 5_000),
            strategy=StrategyRunner({"fixture": adapter}), signing_key_path=key,
            capability=capability, id_generator=Uuid7Generator(),
            clock=FrozenClock(datetime(2026, 8, 12, tzinfo=UTC)),
            host_profiles={"fixture": {
                "adapter_version": "v1", "environment_profile_id": "fixture-clean",
                "source_commit": "a" * 40,
            }}, budget_limits={"WALL_SECOND": 100_000_000},
            revoked_capability_ids=(
                frozenset({capability.capability_id})
                if mode == "revoked" else frozenset()
            ),
        )
    assert adapter.calls == 0


def test_host_unknown_count_is_per_resource_and_not_row_order_dependent(tmp_path: Path) -> None:
    kernel, config, run_id, attempt_id = _ready(tmp_path)
    key = tmp_path / "host.key"
    _write_host_key(key)
    service = HostExecutionReceiptService(
        storage=SQLiteStorage(config.db_path, 5_000),
        strategy=StrategyRunner({"fixture": UnknownCostFixtureAdapter()}),
        signing_key_path=key, capability=_host_capability(), id_generator=Uuid7Generator(),
        clock=SystemClock(), host_profiles={"fixture": {
            "adapter_version": "v1", "environment_profile_id": "fixture-clean",
            "source_commit": "a" * 40, "component": "fixture-model",
            "token_meter_applicable": True,
        }}, budget_limits={
            "WALL_SECOND": 100_000_000,
            "INPUT_TOKEN": 10_000_000,
            "OUTPUT_TOKEN": 10_000_000,
        },
    )
    service.execute(run_id=run_id, attempt_id=attempt_id, request={"x": 1})
    usage = kernel.inspect(run_id).projection["component_usage"]["fixture-model"]
    assert usage["unknown_count"] == 3
    assert usage["input_tokens"] == 0


@pytest.mark.parametrize("field", ["signature", "payload_json", "statement_hash"])
def test_lean_receipt_tampering_is_rejected_before_consumption(
    tmp_path: Path, field: str
) -> None:
    _kernel, config, run_id, attempt_id = _ready(tmp_path)
    key = tmp_path / "host.key"
    _write_host_key(key)
    service = HostExecutionReceiptService(
        storage=SQLiteStorage(config.db_path, 5_000),
        strategy=StrategyRunner({"fixture": ReceiptFixtureAdapter()}),
        signing_key_path=key,
        capability=_host_capability(), id_generator=Uuid7Generator(), clock=SystemClock(),
        host_profiles={"fixture": {
            "adapter_version": "v1", "environment_profile_id": "fixture-clean",
            "source_commit": "a" * 40,
        }},
        budget_limits={"WALL_SECOND": 100_000_000},
    )
    receipt = service.execute(run_id=run_id, attempt_id=attempt_id, request={"x": 1})
    with sqlite3.connect(config.db_path) as connection:
        if field == "signature":
            connection.execute(
                "UPDATE host_execution_receipts SET signature=? WHERE receipt_id=?",
                ("0" * 64, receipt.receipt_id),
            )
        elif field == "payload_json":
            connection.execute(
                "UPDATE host_execution_receipts SET "
                "payload_json=json_set(payload_json,'$.status','FAILED') "
                "WHERE receipt_id=?", (receipt.receipt_id,)
            )
        else:
            connection.execute(
                "UPDATE host_execution_receipts SET statement_hash=? WHERE receipt_id=?",
                ("9" * 64, receipt.receipt_id),
            )
    with pytest.raises(StorageConflict):
        service.verify_receipt(receipt.receipt_id)
