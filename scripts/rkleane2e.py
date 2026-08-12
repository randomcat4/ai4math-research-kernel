from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from rk.adapters import (
    AdapterProfile,
    CurlHttpClient,
    JixiaAdapter,
    LeanReplayAdapter,
    LeanSearchAdapter,
    OpenCodeAdapter,
)
from rk.composition import canonical_json_bytes as composition_json_bytes
from rk.config import KernelConfig
from rk.domain import (
    ApplyRequest,
    ArtifactInput,
    CreateRequest,
    RunSnapshot,
    TypedCommand,
    VerifiedCapability,
    frozen_mapping,
)
from rk.kernel import ResearchKernel
from rk.ports import ExecutionAdapter
from rk.strategy import StrategyRunner, ToolInvocation


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def git_output(repo: Path, *args: str) -> str:
    completed = subprocess.run(
        ["git", "-C", str(repo), *args],
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="strict",
        timeout=60,
    )
    if completed.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} failed: {completed.stderr.strip()}")
    return completed.stdout.strip()


def artifact(path: Path, name: str, media_type: str) -> ArtifactInput:
    return ArtifactInput(name, str(path), sha256_file(path), path.stat().st_size, media_type)


def write_json(path: Path, value: Any) -> None:
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )


def apply(
    kernel: ResearchKernel,
    capability: VerifiedCapability,
    run_id: str,
    command_type: str,
    payload: Mapping[str, Any],
    inputs: Sequence[ArtifactInput] = (),
) -> Any:
    snapshot = kernel.inspect(run_id)
    assert isinstance(snapshot, RunSnapshot)
    receipt = kernel.apply(
        ApplyRequest(
            request_id=str(uuid.uuid4()),
            run_id=run_id,
            expected_revision=snapshot.revision,
            command=TypedCommand(command_type, frozen_mapping(payload)),
            artifact_inputs=tuple(inputs),
        ),
        capability,
    )
    if not receipt.accepted:
        raise RuntimeError(
            f"{command_type} rejected: {receipt.rejection_code} "
            f"{[item.to_dict() for item in receipt.missing_conditions]}"
        )
    return receipt


def artifact_id_for_sha(snapshot: RunSnapshot, digest: str) -> str:
    for item in snapshot.projection["artifacts"]:
        if item["sha256"] == digest:
            return str(item["artifact_id"])
    raise RuntimeError(f"artifact not projected: {digest}")


def newest(snapshot: RunSnapshot, collection: str, identifier: str) -> str:
    values = snapshot.projection[collection]
    if not values:
        raise RuntimeError(f"{collection} is empty")
    return str(values[-1][identifier])


def capability(identifier: str, subject: str, actions: set[str]) -> VerifiedCapability:
    return VerifiedCapability(
        capability_id=identifier,
        subject_id=subject,
        issuer="rk-lean-e2e",
        allowed_actions=frozenset(actions),
        run_scope=frozenset({"*"}),
        issued_at="2026-01-01T00:00:00.000Z",
        expires_at="2027-01-01T00:00:00.000Z",
    )


def contract() -> dict[str, Any]:
    return {
        "stable_project_id": "RK_LEAN_E2E_ADD_ZERO",
        "statement": "For every natural number n, n + 0 = n.",
        "source_refs": [],
        "objects": [{"name": "natural number n"}],
        "definitions": [],
        "quantifiers": [{"kind": "forall", "variable": "n : Nat"}],
        "exact_negation": "There exists a natural number n such that n + 0 != n.",
        "allowed_dependencies": ["Mathlib v4.28.0-rc1"],
        "forbidden_information": [],
        "boundary_rules": {},
        "randomness_rules": {},
        "tie_rules": {},
        "success_certificate_types": ["LEAN_REPLAY"],
        "non_claims": ["A LeanSearch hit or model-generated source is not a proof."],
        "literature_scope": {"families": ["elementary arithmetic"]},
        "literature_cutoff_date": "2026-08-11",
        "budget_policy": {"global": {"INPUT_TOKEN": 100000, "OUTPUT_TOKEN": 20000}},
        "stop_rules": [{"kind": "kernel_replay", "count": 1}],
        "semantic_review_policy": {"requires_independent_human": True},
        "amendment_policy": {},
    }


class RecordedRun:
    def __init__(
        self,
        kernel: ResearchKernel,
        run_id: str,
        claim_id: str,
        route_id: str,
        statement_hash: str,
        inbox: Path,
        candidate: VerifiedCapability,
        verifier: VerifiedCapability,
        operator: VerifiedCapability,
    ) -> None:
        self.kernel = kernel
        self.run_id = run_id
        self.claim_id = claim_id
        self.route_id = route_id
        self.statement_hash = statement_hash
        self.inbox = inbox
        self.candidate = candidate
        self.verifier = verifier
        self.operator = operator
        self.ordinal = 0

    def evidence(
        self,
        cap: VerifiedCapability,
        evidence_type: str,
        strength: str,
        paths: Sequence[tuple[Path, str]],
        actor: str,
        root_kind: str,
        provenance: Mapping[str, Any],
        verifier_profile_id: str | None = None,
    ) -> Any:
        names = [path.name for path, _ in paths]
        root: dict[str, Any] = {"root_kind": root_kind, "source_graph": {}}
        if verifier_profile_id is not None:
            root["verifier_profile_id"] = verifier_profile_id
        return apply(
            self.kernel,
            cap,
            self.run_id,
            "SubmitEvidence",
            {
                "claim_id": self.claim_id,
                "contract_version": 1,
                "statement_hash": self.statement_hash,
                "evidence_type": evidence_type,
                "evidence_strength": strength,
                "artifact_input_names": names,
                "scope": {
                    "claim_id": self.claim_id,
                    "contract_version": 1,
                    "statement_hash": self.statement_hash,
                },
                "provenance": {"actor": actor, **dict(provenance)},
                "evidence_root": root,
            },
            [artifact(path, path.name, media) for path, media in paths],
        )

    def invoke(
        self,
        strategy: StrategyRunner,
        adapter_name: str,
        request: Mapping[str, Any],
        request_path: Path,
        cap: VerifiedCapability,
        source_commit: str,
        environment_profile_id: str,
    ) -> tuple[ToolInvocation, str]:
        self.ordinal += 1
        persisted_request = dict(request)
        request_environment = persisted_request.get("environment")
        if isinstance(request_environment, Mapping):
            persisted_request["environment"] = {
                str(key): "<redacted>" for key in request_environment
            }
        write_json(request_path, persisted_request)
        self.evidence(
            cap,
            "EXECUTION_LOG",
            "PROVENANCE_ONLY",
            [(request_path, "application/json")],
            f"{adapter_name}:request",
            "EXTERNAL_SOURCE",
            {"phase": "invocation"},
        )
        snapshot = self.kernel.inspect(self.run_id)
        assert isinstance(snapshot, RunSnapshot)
        invocation_artifact_id = artifact_id_for_sha(snapshot, sha256_file(request_path))
        apply(
            self.kernel,
            cap,
            self.run_id,
            "RegisterAttempt",
            {
                "route_id": self.route_id,
                "ordinal": self.ordinal,
                "isolation_epoch": self.ordinal,
                "work_relpath": f"attempts/{self.ordinal}/work",
                "allowed_write_set": [f"attempt-{self.ordinal}"],
                "input_snapshot_digest": sha256_file(request_path),
            },
        )
        snapshot = self.kernel.inspect(self.run_id)
        assert isinstance(snapshot, RunSnapshot)
        attempt_id = newest(snapshot, "active_attempts", "attempt_id")
        _, adapter_version = strategy.adapter_identity(adapter_name)
        apply(
            self.kernel,
            cap,
            self.run_id,
            "BindExecution",
            {
                "route_id": self.route_id,
                "attempt_id": attempt_id,
                "adapter_name": adapter_name,
                "adapter_version": adapter_version,
                "source_commit": source_commit,
                "environment_profile_id": environment_profile_id,
                "invocation_artifact_id": invocation_artifact_id,
                "external_ids": {},
            },
        )
        snapshot = self.kernel.inspect(self.run_id)
        assert isinstance(snapshot, RunSnapshot)
        binding = next(
            item
            for item in snapshot.projection["bindings"]
            if item["attempt_id"] == attempt_id
        )
        receipt_context = {
            "run_id": self.run_id,
            "attempt_id": attempt_id,
            "binding_id": binding["binding_id"],
            "environment_profile_id": environment_profile_id,
            "source_commit": source_commit,
            "invocation_nonce": binding["invocation_nonce"],
        }
        reservations: dict[str, int] = {
            "WALL_SECOND": {
                "leansearch": 60,
                "opencode": 600,
                "jixia": 300,
                "lean-replay": 300,
            }[adapter_name]
            * 1_000_000,
        }
        if adapter_name == "opencode":
            reservations.update(
                {
                    "INPUT_TOKEN": 50_000 * 1_000_000,
                    "OUTPUT_TOKEN": int(request.get("max_tokens", 0)) * 1_000_000,
                }
            )
        component = f"{adapter_name}:{environment_profile_id}"
        for resource, amount in reservations.items():
            apply(
                self.kernel,
                self.operator,
                self.run_id,
                "RecordBudget",
                {
                    "route_id": self.route_id,
                    "attempt_id": attempt_id,
                    "event_kind": "RESERVATION",
                    "resource_kind": resource,
                    "amount_microunits": amount,
                    "unit": "microunit",
                    "provider_usage": {"component": component, "preflight": True},
                },
            )
        holder = f"{adapter_name}-{self.ordinal}"
        apply(
            self.kernel,
            cap,
            self.run_id,
            "AcquireLease",
            {"attempt_id": attempt_id, "holder_id": holder, "ttl_seconds": 900},
        )
        snapshot = self.kernel.inspect(self.run_id)
        assert isinstance(snapshot, RunSnapshot)
        lease_id = str(snapshot.projection["active_attempts"][-1]["lease_id"])
        invocation: ToolInvocation | None = None
        released = False
        try:
            invocation = strategy.invoke(
                adapter_name, request, receipt_context=receipt_context
            )
            raw_result_path = request_path.with_name(
                request_path.name.replace(".request.json", ".rawresult.json")
            )
            write_json(raw_result_path, invocation.to_dict())
            self.evidence(
                cap,
                "EXECUTION_LOG",
                "PROVENANCE_ONLY",
                [(raw_result_path, "application/json")],
                f"{adapter_name}:raw-result",
                "EXTERNAL_SOURCE",
                {"phase": "post-execution-pre-budget", "status": invocation.status},
            )
            usage = dict(invocation.usage)
            for resource, amount in reservations.items():
                apply(
                    self.kernel,
                    self.operator,
                    self.run_id,
                    "RecordBudget",
                    {
                        "route_id": self.route_id,
                        "attempt_id": attempt_id,
                        "event_kind": "REFUND",
                        "resource_kind": resource,
                        "amount_microunits": amount,
                        "unit": "microunit",
                        "provider_usage": {"component": component, "settled": True},
                    },
                )
            if usage.get("input_tokens") is not None:
                apply(
                    self.kernel,
                    self.operator,
                    self.run_id,
                    "RecordBudget",
                    {
                        "route_id": self.route_id,
                        "attempt_id": attempt_id,
                        "event_kind": "ACTUAL",
                        "resource_kind": "INPUT_TOKEN",
                        "amount_microunits": int(usage.get("input_tokens", 0)) * 1_000_000,
                        "unit": "microtoken",
                        "provider_usage": {
                            "component": component,
                            **{
                                key: int(value)
                                for key, value in usage.items()
                                if key != "wall_time_ms"
                                and isinstance(value, int)
                                and not isinstance(value, bool)
                            },
                        },
                    },
                )
                apply(
                    self.kernel,
                    self.operator,
                    self.run_id,
                    "RecordBudget",
                    {
                        "route_id": self.route_id,
                        "attempt_id": attempt_id,
                        "event_kind": "ACTUAL",
                        "resource_kind": "OUTPUT_TOKEN",
                        "amount_microunits": int(usage.get("output_tokens", 0)) * 1_000_000,
                        "unit": "microtoken",
                        "provider_usage": {"component": component},
                    },
                )
            elif adapter_name == "leansearch":
                for resource in ("INPUT_TOKEN", "API_MICRO_CURRENCY"):
                    apply(
                        self.kernel,
                        self.operator,
                        self.run_id,
                        "RecordBudget",
                        {
                            "route_id": self.route_id,
                            "attempt_id": attempt_id,
                            "event_kind": "UNKNOWN_COST",
                            "resource_kind": resource,
                            "unit": "unknown",
                            "provider_usage": {
                                "component": component,
                                "reason": "provider_omitted",
                                "cost_unknown": True,
                            },
                        },
                    )
            if adapter_name == "opencode" and usage.get("cost_unknown") is not True:
                apply(
                    self.kernel,
                    self.operator,
                    self.run_id,
                    "RecordBudget",
                    {
                        "route_id": self.route_id,
                        "attempt_id": attempt_id,
                        "event_kind": "UNKNOWN_COST",
                        "resource_kind": "API_MICRO_CURRENCY",
                        "unit": "unknown",
                        "provider_usage": {
                            "component": component,
                            "reason": "provider_price_not_attested",
                            "cost_unknown": True,
                        },
                    },
                )
            if adapter_name in {"jixia", "lean-replay"}:
                apply(
                    self.kernel,
                    self.operator,
                    self.run_id,
                    "RecordBudget",
                    {
                        "route_id": self.route_id,
                        "attempt_id": attempt_id,
                        "event_kind": "UNKNOWN_COST",
                        "resource_kind": "CPU_SECOND",
                        "unit": "unknown",
                        "provider_usage": {
                            "component": component,
                            "reason": "process_cpu_not_sampled",
                            "cost_unknown": True,
                        },
                    },
                )
            if usage.get("cost_unknown") is True:
                unknown_resources = {
                    "opencode": ("INPUT_TOKEN", "OUTPUT_TOKEN", "API_MICRO_CURRENCY"),
                    "jixia": ("CPU_SECOND",),
                    "lean-replay": ("CPU_SECOND",),
                }.get(adapter_name, ())
                for resource in unknown_resources:
                    apply(
                        self.kernel,
                        self.operator,
                        self.run_id,
                        "RecordBudget",
                        {
                            "route_id": self.route_id,
                            "attempt_id": attempt_id,
                            "event_kind": "UNKNOWN_COST",
                            "resource_kind": resource,
                            "unit": "unknown",
                            "provider_usage": {
                                "component": component,
                                "reason": "execution_cost_unknown",
                                "cost_unknown": True,
                            },
                        },
                    )
            apply(
                self.kernel,
                self.operator,
                self.run_id,
                "RecordBudget",
                {
                    "route_id": self.route_id,
                    "attempt_id": attempt_id,
                    "event_kind": "ACTUAL",
                    "resource_kind": "WALL_SECOND",
                    "amount_microunits": invocation.wall_time_ms * 1_000,
                    "unit": "microsecond",
                    "provider_usage": {
                        "component": component,
                        "wall_time_ms": invocation.wall_time_ms,
                    },
                },
            )
            terminal = "SUCCEEDED" if invocation.status == "COMPLETED" else "FAILED"
            apply(
                self.kernel,
                cap,
                self.run_id,
                "ReleaseLease",
                {
                    "lease_id": lease_id,
                    "holder_id": holder,
                    "terminal_attempt_status": terminal,
                },
            )
            released = True
        finally:
            if not released:
                apply(
                    self.kernel,
                    cap,
                    self.run_id,
                    "ReleaseLease",
                    {
                        "lease_id": lease_id,
                        "holder_id": holder,
                        "terminal_attempt_status": "ENVIRONMENT_ERROR",
                    },
                )
        assert invocation is not None
        return invocation, attempt_id


def lean_source(text: str) -> str:
    matches = re.findall(
        r"```(?:lean|lean4)?\s*\n(.*?)```", text, flags=re.DOTALL | re.IGNORECASE
    )
    source = matches[-1].strip() if matches else text.strip()
    if "theorem rk_add_zero" not in source:
        raise RuntimeError("LeanWorker response omitted theorem rk_add_zero")
    return source + "\n"


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: rkleane2e.py DEPLOYMENT_ROOT")
    root = Path(sys.argv[1]).resolve()
    run_name = os.environ.get("RK_E2E_RUN_NAME", "leane2e")
    if not re.fullmatch(r"[a-z0-9_]{1,40}", run_name):
        raise RuntimeError("RK_E2E_RUN_NAME must be a short lowercase alphanumeric name")
    run_root = root / run_name
    state = run_root / "state"
    inbox = state / "inbox"
    outputs = run_root / "outputs"
    attempts_root = run_root / "attempts"
    for directory in (inbox, outputs, attempts_root):
        directory.mkdir(parents=True, exist_ok=True)

    mathlib = Path(os.environ["RK_E2E_MATHLIB_ROOT"]).resolve()
    project = run_root / "project"
    # The shared Mathlib Lake cache maps module names to object paths.  Include the run name so
    # a new run cannot consume or collide with another run's jixia preflight object.
    source_relpath = f"RKLeanE2E/{run_name}/Main.lean"
    project_source = project / source_relpath
    toolchain = Path(os.environ["RK_E2E_TOOLCHAIN_ROOT"]).resolve()
    jixia_repo = Path(os.environ["RK_E2E_JIXIA_ROOT"]).resolve()
    opencode = Path(os.environ["RK_E2E_OPENCODE_BIN"]).resolve()
    opencode_source_config = Path(os.environ["RK_E2E_OPENCODE_CONFIG"]).resolve()
    opencode_config = inbox / "opencode.policy.json"
    subprocess.run(
        [
            sys.executable,
            str(Path(__file__).with_name("rkopencodepolicy.py")),
            str(opencode_source_config),
            str(opencode_config),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    opencode_workspace_root = Path(
        os.environ["RK_E2E_OPENCODE_WORKSPACE_ROOT"]
    ).resolve()
    opencode_run_user = os.environ["RK_E2E_OPENCODE_USER"]
    opencode_workspace_root.mkdir(parents=True, exist_ok=True)
    if opencode_run_user:
        access_probe = subprocess.run(
            ["runuser", "-u", opencode_run_user, "--", "test", "-x", str(opencode)],
            check=False,
        )
        if access_probe.returncode != 0:
            raise RuntimeError(
                "RK_E2E_OPENCODE_BIN must be executable from the non-root runner path"
            )
    deepseek_key_path = Path(os.environ["RK_E2E_DEEPSEEK_KEY"]).resolve()
    if deepseek_key_path.stat().st_mode & 0o077:
        raise RuntimeError("RK_E2E_DEEPSEEK_KEY must not be accessible by group or others")
    deepseek_key = deepseek_key_path.read_text(encoding="utf-8").strip()
    model = os.environ.get("RK_E2E_MODEL", "deepseek-v4/deepseek-v4-pro")
    search_commit = "94f4888cbaf9f4322535755f86cbac690ec18080"
    mathlib_commit = subprocess.check_output(
        ["git", "-C", str(mathlib), "rev-parse", "HEAD"], text=True
    ).strip()
    jixia_commit = subprocess.check_output(
        ["git", "-C", str(jixia_repo), "rev-parse", "HEAD"], text=True
    ).strip()
    expected_mathlib_commit = "5352afccd6866369be9de43f5b7ec47203555f44"
    if mathlib_commit != expected_mathlib_commit:
        raise RuntimeError(f"Mathlib commit drift: {mathlib_commit}")
    toolchain_name = "leanprover/lean4:v4.28.0-rc1"
    if not (project / ".git").exists():
        subprocess.run(
            [
                "git",
                "-C",
                str(mathlib),
                "worktree",
                "add",
                "--detach",
                str(project),
                mathlib_commit,
            ],
            check=True,
            timeout=300,
        )
        os.symlink(mathlib / ".lake", project / ".lake", target_is_directory=True)
    project_source.parent.mkdir(parents=True, exist_ok=True)
    if (project / "lean-toolchain").read_text(encoding="utf-8").strip() != toolchain_name:
        raise RuntimeError("Mathlib lean-toolchain drift")
    if git_output(project, "rev-parse", "HEAD") != mathlib_commit:
        raise RuntimeError("Mathlib worktree HEAD drift")
    baseline_dirty = git_output(project, "status", "--short", "--untracked-files=no")
    if baseline_dirty:
        raise RuntimeError(f"Mathlib tracked worktree is dirty before execution: {baseline_dirty}")
    dependency_inputs = [project / "lake-manifest.json", project / "lean-toolchain"]
    dependency_closure_sha256 = hashlib.sha256(
        b"".join(path.read_bytes() for path in dependency_inputs)
    ).hexdigest()

    verifier_id = str(uuid.uuid4())
    operator_id = str(uuid.uuid4())
    candidate_id = str(uuid.uuid4())
    receipt_key_path = Path(os.environ["RK_E2E_RECEIPT_KEY"]).resolve()
    replay_receipt_key = receipt_key_path.read_bytes()
    if len(replay_receipt_key) < 32:
        raise RuntimeError("RK_E2E_RECEIPT_KEY must contain at least 32 bytes")
    if receipt_key_path.stat().st_mode & 0o077:
        raise RuntimeError("RK_E2E_RECEIPT_KEY must not be accessible by group or others")
    adapter_policy = {
        "leansearch": {
            "versions": {
                "v1": {"environment_profile_ids": ["public-v1"], "source_commits": [search_commit]}
            }
        },
        "opencode": {
            "versions": {
                "v1": {
                    "environment_profile_ids": ["deepseek-v4-pro"],
                    "source_commits": [sha256_file(opencode)],
                }
            }
        },
        "jixia": {
            "versions": {
                "v1": {"environment_profile_ids": ["jixia-4.28"], "source_commits": [jixia_commit]}
            }
        },
        "lean-replay": {
            "versions": {
                "v1": {
                    "environment_profile_ids": ["lean-clean-4.28"],
                    "source_commits": [mathlib_commit],
                }
            }
        },
    }
    config = KernelConfig.from_mapping(
        {
            "workspace_root": str(state),
            "inbox_roots": [str(inbox)],
            "command_schema_path": str(root / "app/docs/spec/json/command.schema.json"),
            "receipt_schema_path": str(root / "app/docs/spec/json/receipt.schema.json"),
            "adapter_profiles": adapter_policy,
            "verifier_profiles": {
                "lean-clean-4.28": {
                    "toolchain": toolchain_name,
                    "mathlib_commit": mathlib_commit,
                    "adapter_name": "lean-replay",
                    "binary_sha256": sha256_file(toolchain / "bin/lean"),
                    "receipt_hmac_key_hex": replay_receipt_key.hex(),
                    "verifier_writer_capability_ids": [verifier_id],
                    "forbidden_submitter_subject_ids": ["candidate-writer"],
                }
            },
            "budget_controller_capability_ids": [operator_id],
            "budget_policy": {
                "budget_controller_capability_ids": [operator_id],
                "global_budget_limits": {
                    "INPUT_TOKEN": 100_000_000_000,
                    "OUTPUT_TOKEN": 20_000_000_000,
                    "WALL_SECOND": 3_600_000_000,
                    "CPU_SECOND": 3_600_000_000,
                    "API_MICRO_CURRENCY": 100_000_000_000,
                },
                "route_share_denominator": 5,
            },
        },
        base=root,
    )
    kernel = ResearchKernel.from_config(config, migrations_dir=root / "app/migrations")
    operator = capability(operator_id, "host-operator", {"*"})
    candidate = capability(
        candidate_id,
        "candidate-writer",
        {"SubmitEvidence", "RegisterAttempt", "BindExecution", "AcquireLease", "ReleaseLease"},
    )
    verifier = capability(
        verifier_id,
        "verifier-writer",
        {
            "SubmitEvidence",
            "RegisterAttempt",
            "BindExecution",
            "AcquireLease",
            "ReleaseLease",
            "RecordLeanFeedback",
        },
    )

    problem = inbox / "problem.md"
    problem.write_text(contract()["statement"] + "\n", encoding="utf-8")
    handle = kernel.create(
        CreateRequest(
            str(uuid.uuid4()),
            frozen_mapping(contract()),
            (artifact(problem, problem.name, "text/markdown"),),
        ),
        operator,
    )
    snapshot = kernel.inspect(handle.run_id)
    assert isinstance(snapshot, RunSnapshot)
    problem_id = artifact_id_for_sha(snapshot, sha256_file(problem))
    normalized = {"statement": contract()["statement"], "atomic": True}
    statement_hash = hashlib.sha256(composition_json_bytes(normalized)).hexdigest()
    apply(
        kernel,
        operator,
        handle.run_id,
        "RegisterClaim",
        {
            "contract_version": 1,
            "claim_kind": "ROOT",
            "stable_label": "rk-add-zero",
            "statement_artifact_id": problem_id,
            "statement_hash": statement_hash,
            "normalized_statement": normalized,
        },
    )
    snapshot = kernel.inspect(handle.run_id)
    assert isinstance(snapshot, RunSnapshot)
    claim_id = str(snapshot.projection["root_claim_id"])
    apply(
        kernel,
        operator,
        handle.run_id,
        "FreezeContract",
        {"contract_version": 1, "completeness_check_artifact_id": problem_id},
    )
    apply(
        kernel,
        operator,
        handle.run_id,
        "StartRun",
        {
            "contract_version": 1,
            "literature_plan_artifact_id": problem_id,
            "budget_policy": contract()["budget_policy"],
        },
    )
    apply(
        kernel,
        operator,
        handle.run_id,
        "RegisterRoute",
        {
            "contract_version": 1,
            "target_claim_id": claim_id,
            "label": "leansearch-deepseek-jixia-clean-replay",
            "representation": "Lean theorem",
            "tool_family": "formal-proof-pipeline",
            "approach_root": {"label": "mathlib-premise-to-kernel"},
            "budget_policy": {"attempts": 4},
        },
    )
    snapshot = kernel.inspect(handle.run_id)
    assert isinstance(snapshot, RunSnapshot)
    route_id = newest(snapshot, "routes", "route_id")
    recorded = RecordedRun(
        kernel,
        handle.run_id,
        claim_id,
        route_id,
        statement_hash,
        inbox,
        candidate,
        verifier,
        operator,
    )

    path_value = f"{toolchain / 'bin'}:/usr/local/bin:/usr/bin:/bin"
    jixia_bin = jixia_repo / ".lake/build/bin/jixia"
    adapters: dict[str, ExecutionAdapter] = {
        "leansearch": LeanSearchAdapter(
            AdapterProfile.from_mapping(
                {
                    "name": "leansearch",
                    "version": "v1",
                    "source_commit": search_commit,
                    "timeout_seconds": 60,
                    "max_response_bytes": 8 * 1024 * 1024,
                    "env_whitelist": [],
                    "endpoint": "https://leansearch.net/search",
                    "max_retries": 1,
                    "retry_statuses": [429, 502, 503, 504],
                    "backoff_seconds": 0.25,
                }
            ),
            client=CurlHttpClient(),
        ),
        "opencode": OpenCodeAdapter(
            AdapterProfile.from_mapping(
                {
                    "name": "opencode",
                    "version": "v1",
                    "source_commit": sha256_file(opencode),
                    "timeout_seconds": 600,
                    "max_response_bytes": 16 * 1024 * 1024,
                    "env_whitelist": ["DEEPSEEK_API_KEY", "OPENCODE_CONFIG"],
                    "argv_prefix": [str(opencode)],
                    "workspace_root": str(opencode_workspace_root),
                    "max_tool_calls": 0,
                    "require_deny_all_tools": True,
                    "run_as_user": opencode_run_user,
                }
            )
        ),
        "jixia": JixiaAdapter(
            AdapterProfile.from_mapping(
                {
                    "name": "jixia",
                    "version": "v1",
                    "source_commit": jixia_commit,
                    "timeout_seconds": 300,
                    "max_response_bytes": 32 * 1024 * 1024,
                    "env_whitelist": ["PATH"],
                    "argv_prefix": [str(toolchain / "bin/lake"), "env", str(jixia_bin)],
                    "preflight_argv_prefix": [
                        str(toolchain / "bin/lake"),
                        "env",
                        str(toolchain / "bin/lean"),
                    ],
                    "repo_path": str(jixia_repo),
                    "workspace_root": str(project),
                    "output_root": str(inbox / "jixia"),
                    "expected_toolchain": toolchain_name,
                    "binary_path": str(jixia_bin),
                    "binary_sha256": sha256_file(jixia_bin),
                }
            )
        ),
        "lean-replay": LeanReplayAdapter(
            AdapterProfile.from_mapping(
                {
                    "name": "lean-replay",
                    "version": "v1",
                    "source_commit": mathlib_commit,
                    "timeout_seconds": 300,
                    "max_response_bytes": 8 * 1024 * 1024,
                    "env_whitelist": ["PATH"],
                    "argv_prefix": [
                        str(toolchain / "bin/lake"),
                        "env",
                        str(toolchain / "bin/lean"),
                    ],
                    "workspace_root": str(project),
                    "output_root": str(inbox / "lean"),
                    "expected_toolchain": toolchain_name,
                    "binary_path": str(toolchain / "bin/lean"),
                    "binary_sha256": sha256_file(toolchain / "bin/lean"),
                    "allowed_axioms": ["propext", "Classical.choice", "Quot.sound"],
                }
            )
        ),
    }
    strategy = StrategyRunner(
        adapters, receipt_signing_keys={"lean-replay": replay_receipt_key}
    )
    invocations: list[dict[str, Any]] = []

    search_request = {
        "query": ["natural number addition with zero equals itself"],
        "num_results": 8,
        "rerank": True,
        "retrieve_k": 50,
    }
    search, _ = recorded.invoke(
        strategy,
        "leansearch",
        search_request,
        inbox / "leansearch.request.json",
        candidate,
        search_commit,
        "public-v1",
    )
    invocations.append(search.to_dict())
    search_result_path = inbox / "leansearch.result.json"
    write_json(search_result_path, search.to_dict())
    recorded.evidence(
        candidate,
        "EXECUTION_LOG",
        "PROVENANCE_ONLY",
        [(search_result_path, "application/json")],
        "leansearch",
        "EXTERNAL_SOURCE",
        {"trust_limit": "PREMISE_CANDIDATE"},
    )
    if search.status != "COMPLETED":
        raise RuntimeError(f"LeanSearch did not complete: {search.status}")

    hits = search.result.get("payload", {}).get("batches", [[]])[0]
    prompt = (
        "You are the LeanWorker. Produce one complete Lean 4.28.0-rc1 source file using Mathlib. "
        "Prove exactly `theorem rk_add_zero (n : Nat) : n + 0 = n`. "
        "No sorry, admit, axiom, unsafe, native_decide, tools, or file writes. "
        "Return only one fenced lean code block. LeanSearch premise candidates follow:\n"
        + json.dumps(hits[:8], ensure_ascii=False)
    )
    model_request = {
        "prompt": prompt,
        "model": model,
        "workspace_relpath": run_name,
        "environment": {
            "DEEPSEEK_API_KEY": deepseek_key,
            "OPENCODE_CONFIG": str(opencode_config),
        },
    }
    model_call, _ = recorded.invoke(
        strategy,
        "opencode",
        model_request,
        inbox / "opencode.request.json",
        candidate,
        sha256_file(opencode),
        "deepseek-v4-pro",
    )
    invocations.append(model_call.to_dict())
    model_log = inbox / "opencode.result.json"
    write_json(model_log, model_call.to_dict())
    recorded.evidence(
        candidate,
        "EXECUTION_LOG",
        "PROVENANCE_ONLY",
        [(model_log, "application/json")],
        "opencode-deepseek-leanworker:execution",
        "MODEL",
        {"model": model, "status": model_call.status},
    )
    if model_call.status != "COMPLETED":
        raise RuntimeError(f"OpenCode did not complete: {model_call.status}")
    source_text = lean_source(str(model_call.result.get("payload", {}).get("text", "")))
    project_source.write_text(source_text, encoding="utf-8")
    source_copy = inbox / "Main.lean"
    shutil.copyfile(project_source, source_copy)
    recorded.evidence(
        candidate,
        "MODEL_JUDGE",
        "SOFT_MODEL",
        [(source_copy, "text/x-lean")],
        "opencode-deepseek-leanworker",
        "MODEL",
        {"model": model},
    )
    snapshot = kernel.inspect(handle.run_id)
    assert isinstance(snapshot, RunSnapshot)
    source_artifact_id = artifact_id_for_sha(snapshot, sha256_file(source_copy))

    jixia_request = {
        "source_relpath": source_relpath,
        "output_relpath": "run1",
        "include_initializers": True,
        "environment": {"PATH": path_value},
    }
    jixia_call, _ = recorded.invoke(
        strategy,
        "jixia",
        jixia_request,
        inbox / "jixia.request.json",
        verifier,
        jixia_commit,
        "jixia-4.28",
    )
    invocations.append(jixia_call.to_dict())
    jixia_result = inbox / "jixia.result.json"
    write_json(jixia_result, jixia_call.to_dict())
    jixia_outputs = sorted((inbox / "jixia/run1").glob("*.json"))
    recorded.evidence(
        verifier,
        "EXECUTION_LOG",
        "PROVENANCE_ONLY",
        [
            (jixia_result, "application/json"),
            *((path, "application/json") for path in jixia_outputs),
        ],
        "jixia",
        "EXTERNAL_SOURCE",
        {"trust_limit": "STATIC_STRUCTURE_AND_PROOF_STATE"},
    )
    if jixia_call.status != "COMPLETED":
        raise RuntimeError(f"jixia did not complete: {jixia_call.status}")
    declarations = jixia_call.result.get("payload", {}).get("declarations", [])
    declaration_names = {
        ".".join(str(part) for part in item.get("name", []))
        for item in declarations
        if isinstance(item, Mapping) and isinstance(item.get("name"), list)
    }
    if "rk_add_zero" not in declaration_names:
        raise RuntimeError("jixia did not extract the required rk_add_zero declaration")

    lean_request = {
        "source_relpath": source_relpath,
        "output_relpath": "Main.olean",
        "declarations": sorted(declaration_names & {"rk_add_zero"}),
        "environment": {"PATH": path_value},
    }
    lean_call, lean_attempt_id = recorded.invoke(
        strategy,
        "lean-replay",
        lean_request,
        inbox / "lean.request.json",
        verifier,
        mathlib_commit,
        "lean-clean-4.28",
    )
    invocations.append(lean_call.to_dict())
    lean_result = inbox / "lean.result.json"
    write_json(lean_result, lean_call.to_dict())
    recorded.evidence(
        verifier,
        "EXECUTION_LOG",
        "PROVENANCE_ONLY",
        [(lean_result, "application/json")],
        "lean-replay",
        "LEAN_KERNEL",
        {"kernel_verdict": lean_call.result.get("kernel_verdict")},
        "lean-clean-4.28",
    )
    if lean_call.status != "COMPLETED" or lean_call.result.get("kernel_verdict") != "REPLAY_PASS":
        raise RuntimeError(f"Lean replay did not pass: {lean_call.status}")
    olean = inbox / "lean/Main.olean"
    replay_receipt = recorded.evidence(
        verifier,
        "LEAN_REPLAY",
        "HARD_MACHINE",
        [(olean, "application/x-lean-olean")],
        "lean-kernel-clean-replay",
        "LEAN_KERNEL",
        {
            "toolchain": toolchain_name,
            "mathlib_commit": mathlib_commit,
            "binary_sha256": sha256_file(toolchain / "bin/lean"),
        },
        "lean-clean-4.28",
    )
    snapshot = kernel.inspect(handle.run_id)
    assert isinstance(snapshot, RunSnapshot)
    output_artifact_id = artifact_id_for_sha(snapshot, sha256_file(olean))
    evidence_id = next(
        str(item["evidence_id"])
        for item in snapshot.projection["evidence"]
        if item["artifact_id"] == output_artifact_id and item["evidence_type"] == "LEAN_REPLAY"
    )
    apply(
        kernel,
        verifier,
        handle.run_id,
        "RecordLeanFeedback",
        {
            "claim_id": claim_id,
            "attempt_id": lean_attempt_id,
            "contract_version": 1,
            "environment_profile_id": "lean-clean-4.28",
            "toolchain": toolchain_name,
            "mathlib_commit": mathlib_commit,
            "source_artifact_id": source_artifact_id,
            "output_artifact_id": output_artifact_id,
            "feedback_kind": "REPLAY_PASS",
            "diagnostic": {
                "evidence_receipt": replay_receipt.command_id,
                "axiom_dependencies": lean_call.result.get("axiom_dependencies", []),
                "request_hash": lean_call.request_hash,
                "result_hash": lean_call.result_hash,
                "source_sha256": lean_call.result.get("source_sha256"),
                "output_sha256": lean_call.result.get("output_sha256"),
                "binary_sha256": sha256_file(toolchain / "bin/lean"),
                "exit_code": lean_call.result.get("exit_code"),
                "host_receipt": lean_call.host_receipt,
            },
        },
    )
    apply(
        kernel,
        operator,
        handle.run_id,
        "PromoteClaim",
        {
            "claim_id": claim_id,
            "target_axis": "MACHINE",
            "target_value": "KERNEL_VERIFIED",
            "evidence_ids": [evidence_id],
        },
    )
    apply(
        kernel,
        operator,
        handle.run_id,
        "Finalize",
        {
            "outcome": "UNRESOLVED",
            "terminal_claim_ids": [],
            "open_obligation_ids": [],
            "dossier_spec": {"format": "JSON", "include_raw_artifacts": False, "language": "zh-CN"},
        },
    )
    final = kernel.inspect(handle.run_id)
    assert isinstance(final, RunSnapshot)
    result = {
        "status": "SUCCESS",
        "run_id": handle.run_id,
        "run_status": final.status,
        "revision": final.revision,
        "claim": final.projection["claims"][0],
        "component_usage": final.projection["component_usage"],
        "budget_summary": final.projection["budget_summary"],
        "invocations": invocations,
        "assets": {
            "mathlib_commit": mathlib_commit,
            "mathlib_worktree_head": git_output(project, "rev-parse", "HEAD"),
            "mathlib_tracked_dirty": git_output(
                project, "status", "--short", "--untracked-files=no"
            ),
            "dependency_closure_sha256": dependency_closure_sha256,
            "lean_binary_sha256": sha256_file(toolchain / "bin/lean"),
            "jixia_commit": jixia_commit,
            "jixia_binary_sha256": sha256_file(jixia_bin),
            "leansearch_commit": search_commit,
            "opencode_binary_sha256": sha256_file(opencode),
            "opencode_config_sha256": sha256_file(opencode_config),
            "model_transport": "opencode-jsonl-headless-no-tools",
            "model": model,
        },
        "isolation": {
            "network": "UNENFORCED",
            "readonly_mathlib_cache": "UNENFORCED",
            "per_run_worktree": "ENFORCED",
            "per_invocation_output_collision_check": "ENFORCED",
            "model_tool_surface": "NONE_OPENCODE_POLICY_AND_TOOL_REGISTRY",
        },
    }
    write_json(outputs / "rkleane2e.json", result)
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
