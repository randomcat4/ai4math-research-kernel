from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import time
import uuid
from pathlib import Path
from typing import Any

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


def artifact(path: Path, name: str, media_type: str = "text/plain") -> ArtifactInput:
    data = path.read_bytes()
    return ArtifactInput(
        name=name,
        path=str(path),
        sha256=hashlib.sha256(data).hexdigest(),
        byte_count=len(data),
        media_type=media_type,
    )


def apply(
    kernel: ResearchKernel,
    capability: VerifiedCapability,
    run_id: str,
    command_type: str,
    payload: dict[str, Any],
    inputs: tuple[ArtifactInput, ...] = (),
) -> Any:
    snapshot = kernel.inspect(run_id)
    assert isinstance(snapshot, RunSnapshot)
    receipt = kernel.apply(
        ApplyRequest(
            request_id=str(uuid.uuid4()),
            run_id=run_id,
            expected_revision=snapshot.revision,
            command=TypedCommand(command_type, frozen_mapping(payload)),
            artifact_inputs=inputs,
        ),
        capability,
    )
    if not receipt.accepted:
        raise RuntimeError(
            f"{command_type} rejected: {receipt.rejection_code} "
            f"{[item.to_dict() for item in receipt.missing_conditions]}"
        )
    return receipt


def newest_id(snapshot: RunSnapshot, collection: str, identifier: str) -> str:
    values = snapshot.projection[collection]
    if not values:
        raise RuntimeError(f"{collection} is empty")
    return str(values[-1][identifier])


def model_usage(log_path: Path) -> tuple[str, dict[str, int]]:
    texts: list[str] = []
    usage = {
        "input_tokens": 0,
        "output_tokens": 0,
        "reasoning_tokens": 0,
        "cache_read_tokens": 0,
        "cache_write_tokens": 0,
        "total_tokens": 0,
    }
    with log_path.open(encoding="utf-8", errors="strict") as stream:
        for line in stream:
            try:
                event = json.loads(line)
            except json.JSONDecodeError:
                continue
            if not isinstance(event, dict):
                continue
            part = event.get("part") or {}
            if event.get("type") == "text" and isinstance(part.get("text"), str):
                texts.append(part["text"])
            if event.get("type") == "step_finish":
                tokens = part.get("tokens") or {}
                cache = tokens.get("cache") or {}
                usage["input_tokens"] += int(tokens.get("input", 0))
                usage["output_tokens"] += int(tokens.get("output", 0))
                usage["reasoning_tokens"] += int(tokens.get("reasoning", 0))
                usage["cache_read_tokens"] += int(cache.get("read", 0))
                usage["cache_write_tokens"] += int(cache.get("write", 0))
                usage["total_tokens"] += int(tokens.get("total", 0))
    return "\n".join(texts).strip(), usage


def parsed_answer(text: str) -> dict[str, Any] | None:
    matches = re.findall(r"FINAL_JSON\s*=\s*(\{[^\n]*\})", text)
    if not matches:
        return None
    try:
        value = json.loads(matches[-1])
    except json.JSONDecodeError:
        return None
    return value if isinstance(value, dict) else None


def enumerate_problem() -> dict[str, Any]:
    maximizers: list[list[int]] = []
    maximum = -1
    for mask in range(1 << 12):
        subset = [index + 1 for index in range(12) if mask & (1 << index)]
        valid = all(a + b != 13 for i, a in enumerate(subset) for b in subset[i + 1 :])
        if not valid:
            continue
        if len(subset) > maximum:
            maximum = len(subset)
            maximizers = [subset]
        elif len(subset) == maximum:
            maximizers.append(subset)
    return {
        "status": "PASS",
        "maximum": maximum,
        "number_of_maximizers": len(maximizers),
        "first_maximizer": maximizers[0],
        "checked_subsets": 1 << 12,
    }


def contract() -> dict[str, Any]:
    return {
        "stable_project_id": "RK5_PAIR_SUM_13",
        "statement": (
            "If A is a subset of {1,...,12} and no two distinct elements of A sum to 13, "
            "then |A| <= 6, and equality is attainable."
        ),
        "source_refs": [],
        "objects": [{"name": "subset A of {1,...,12}"}],
        "definitions": [{"name": "valid", "meaning": "no distinct a,b in A have a+b=13"}],
        "quantifiers": [{"kind": "forall", "variable": "A"}],
        "exact_negation": "There exists a valid A with at least 7 elements.",
        "allowed_dependencies": ["finite enumeration", "pairing argument"],
        "forbidden_information": [],
        "boundary_rules": {},
        "randomness_rules": {},
        "tie_rules": {},
        "success_certificate_types": ["CHECKER_CERTIFICATE"],
        "non_claims": ["A model answer alone is not a proof certificate."],
        "literature_scope": {"families": ["elementary finite combinatorics"]},
        "literature_cutoff_date": "2026-08-11",
        "budget_policy": {"global": {"INPUT_TOKEN": 100000, "OUTPUT_TOKEN": 20000}},
        "stop_rules": [{"kind": "fixed_attempts", "count": 5}],
        "semantic_review_policy": {"requires_independent_human": True},
        "amendment_policy": {},
    }


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: rk5demo.py DEPLOYMENT_ROOT")
    root = Path(sys.argv[1]).resolve()
    run_root = root / "demo"
    state = run_root / "state"
    inbox = state / "inbox"
    outputs = run_root / "outputs"
    inbox.mkdir(parents=True, exist_ok=True)
    outputs.mkdir(parents=True, exist_ok=True)

    opencode_bin = Path(os.environ["RK_DEMO_OPENCODE_BIN"])
    opencode_config = Path(os.environ["RK_DEMO_OPENCODE_CONFIG"])
    model = os.environ.get("RK_DEMO_MODEL", "deepseek-v4/deepseek-v4-pro")
    verifier_capability_id = str(uuid.uuid4())
    config = KernelConfig.from_mapping(
        {
            "workspace_root": str(state),
            "inbox_roots": [str(inbox)],
            "command_schema_path": str(root / "app/docs/spec/json/command.schema.json"),
            "receipt_schema_path": str(root / "app/docs/spec/json/receipt.schema.json"),
            "adapter_profiles": {
                "opencode": {
                    "versions": {
                        "1": {
                            "environment_profile_ids": ["deepseek-v4-pro-remote"],
                            "source_commits": [],
                        }
                    }
                }
            },
            "verifier_profiles": {
                "python-enumerator-v1": {
                    "toolchain": "python3-enumerator-v1",
                    "verifier_writer_capability_ids": [verifier_capability_id],
                }
            },
        },
        base=root,
    )
    kernel = ResearchKernel.from_config(config, migrations_dir=root / "app/migrations")
    capability = VerifiedCapability(
        capability_id=verifier_capability_id,
        subject_id="rk5demo-host",
        issuer="rk5demo-remote",
        allowed_actions=frozenset({"*"}),
        run_scope=frozenset({"*"}),
        issued_at="2026-01-01T00:00:00.000Z",
        expires_at="2027-01-01T00:00:00.000Z",
    )

    support = inbox / "problem.md"
    support.write_text(contract()["statement"] + "\n", encoding="utf-8")
    handle = kernel.create(
        CreateRequest(
            request_id=str(uuid.uuid4()),
            contract=frozen_mapping(contract()),
            artifact_inputs=(artifact(support, "problem.md"),),
        ),
        capability,
    )
    snapshot = kernel.inspect(handle.run_id)
    assert isinstance(snapshot, RunSnapshot)
    support_id = next(
        str(item["artifact_id"])
        for item in snapshot.projection["artifacts"]
        if item["logical_name"] == "problem.md"
    )
    normalized = contract()
    statement_hash = hashlib.sha256(composition_json_bytes(normalized)).hexdigest()
    apply(
        kernel,
        capability,
        handle.run_id,
        "RegisterClaim",
        {
            "contract_version": 1,
            "claim_kind": "ROOT",
            "stable_label": "pair-sum-13",
            "statement_artifact_id": next(
                str(item["artifact_id"])
                for item in snapshot.projection["artifacts"]
                if item["role"] == "CONTRACT"
            ),
            "statement_hash": statement_hash,
            "normalized_statement": normalized,
        },
    )
    snapshot = kernel.inspect(handle.run_id)
    assert isinstance(snapshot, RunSnapshot)
    claim_id = str(snapshot.projection["root_claim_id"])
    apply(
        kernel,
        capability,
        handle.run_id,
        "FreezeContract",
        {"contract_version": 1, "completeness_check_artifact_id": support_id},
    )
    apply(
        kernel,
        capability,
        handle.run_id,
        "StartRun",
        {
            "contract_version": 1,
            "literature_plan_artifact_id": support_id,
            "budget_policy": {"global": {"INPUT_TOKEN": 100000, "OUTPUT_TOKEN": 20000}},
        },
    )
    apply(
        kernel,
        capability,
        handle.run_id,
        "RegisterRoute",
        {
            "contract_version": 1,
            "target_claim_id": claim_id,
            "label": "five-independent-model-attempts",
            "representation": "elementary pairing proof",
            "tool_family": "opencode-deepseek",
            "approach_root": {"label": "direct-model-candidates"},
            "budget_policy": {"attempts": 5},
        },
    )
    snapshot = kernel.inspect(handle.run_id)
    assert isinstance(snapshot, RunSnapshot)
    route_id = newest_id(snapshot, "routes", "route_id")

    prompt = (
        "Solve this problem rigorously and concisely: Let A be a subset of {1,2,...,12}. "
        "No two distinct elements of A sum to 13. Determine the exact maximum possible "
        "size of A, prove the upper bound, and give a construction. End with exactly one "
        'line FINAL_JSON={"maximum":6,"construction":[1,2,3,4,5,6]}. '
        "Do not call tools and do not create files."
    )
    attempt_results: list[dict[str, Any]] = []
    for ordinal in range(1, 6):
        input_digest = hashlib.sha256(f"{statement_hash}:{ordinal}".encode()).hexdigest()
        apply(
            kernel,
            capability,
            handle.run_id,
            "RegisterAttempt",
            {
                "route_id": route_id,
                "ordinal": ordinal,
                "isolation_epoch": ordinal,
                "work_relpath": f"runs/{handle.run_id}/{route_id}/{ordinal}/work",
                "allowed_write_set": [f"attempt-{ordinal}"],
                "input_snapshot_digest": input_digest,
            },
        )
        snapshot = kernel.inspect(handle.run_id)
        assert isinstance(snapshot, RunSnapshot)
        attempt_id = newest_id(snapshot, "active_attempts", "attempt_id")
        apply(
            kernel,
            capability,
            handle.run_id,
            "BindExecution",
            {
                "route_id": route_id,
                "attempt_id": attempt_id,
                "adapter_name": "opencode",
                "adapter_version": "1",
                "environment_profile_id": "deepseek-v4-pro-remote",
                "invocation_artifact_id": support_id,
                "external_ids": {"session_ids": []},
            },
        )
        holder = f"opencode-{ordinal}"
        apply(
            kernel,
            capability,
            handle.run_id,
            "AcquireLease",
            {"attempt_id": attempt_id, "holder_id": holder, "ttl_seconds": 900},
        )
        snapshot = kernel.inspect(handle.run_id)
        assert isinstance(snapshot, RunSnapshot)
        lease_id = str(snapshot.projection["active_attempts"][-1]["lease_id"])

        work = outputs / f"attempt{ordinal}"
        work.mkdir(parents=True, exist_ok=True)
        log_path = inbox / f"attempt{ordinal}.jsonl"
        environment = dict(os.environ)
        environment["OPENCODE_CONFIG"] = str(opencode_config)
        started = time.monotonic_ns()
        completed = subprocess.run(
            [
                str(opencode_bin),
                "run",
                "--pure",
                "--format",
                "json",
                "--model",
                model,
                "--dir",
                str(work),
                prompt,
            ],
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            timeout=600,
            check=False,
            env=environment,
        )
        wall_ms = (time.monotonic_ns() - started) // 1_000_000
        log_path.write_bytes(completed.stdout)
        response_text, usage = model_usage(log_path)
        answer = parsed_answer(response_text)
        answer_ok = bool(
            answer
            and answer.get("maximum") == 6
            and answer.get("construction") == [1, 2, 3, 4, 5, 6]
        )
        usage["wall_time_ms"] = int(wall_ms)
        component = f"opencode:{model}:attempt{ordinal}"
        apply(
            kernel,
            capability,
            handle.run_id,
            "SubmitEvidence",
            {
                "claim_id": claim_id,
                "contract_version": 1,
                "statement_hash": statement_hash,
                "evidence_type": "MODEL_JUDGE",
                "evidence_strength": "SOFT_MODEL",
                "artifact_input_names": [log_path.name],
                "scope": {
                    "claim_id": claim_id,
                    "contract_version": 1,
                    "statement_hash": statement_hash,
                },
                "provenance": {
                    "actor": component,
                    "model": model,
                    "attempt": ordinal,
                    "exit_code": completed.returncode,
                },
                "evidence_root": {"root_kind": "MODEL", "source_graph": {}},
            },
            (artifact(log_path, log_path.name, "application/x-ndjson"),),
        )
        for resource, amount, fields in (
            (
                "INPUT_TOKEN",
                usage["input_tokens"] * 1_000_000,
                {
                    key: usage[key]
                    for key in (
                        "input_tokens",
                        "reasoning_tokens",
                        "cache_read_tokens",
                        "cache_write_tokens",
                        "total_tokens",
                    )
                },
            ),
            (
                "OUTPUT_TOKEN",
                usage["output_tokens"] * 1_000_000,
                {"output_tokens": usage["output_tokens"]},
            ),
            ("WALL_SECOND", int(wall_ms) * 1_000, {"wall_time_ms": int(wall_ms)}),
        ):
            apply(
                kernel,
                capability,
                handle.run_id,
                "RecordBudget",
                {
                    "route_id": route_id,
                    "attempt_id": attempt_id,
                    "event_kind": "ACTUAL",
                    "resource_kind": resource,
                    "amount_microunits": amount,
                    "unit": "microtoken" if resource.endswith("TOKEN") else "microsecond",
                    "provider_usage": {"component": component, **fields},
                },
            )
        terminal = "SUCCEEDED" if completed.returncode == 0 else "FAILED"
        apply(
            kernel,
            capability,
            handle.run_id,
            "ReleaseLease",
            {
                "lease_id": lease_id,
                "holder_id": holder,
                "terminal_attempt_status": terminal,
            },
        )
        attempt_results.append(
            {
                "ordinal": ordinal,
                "attempt_id": attempt_id,
                "exit_code": completed.returncode,
                "answer_ok": answer_ok,
                "answer": answer,
                "usage": usage,
            }
        )

    checker_started = time.monotonic_ns()
    enumeration = enumerate_problem()
    checker_ms = (time.monotonic_ns() - checker_started) // 1_000_000
    certificate = inbox / "enumeration.json"
    certificate.write_text(
        json.dumps(enumeration, ensure_ascii=False, sort_keys=True, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    evidence_receipt = apply(
        kernel,
        capability,
        handle.run_id,
        "SubmitEvidence",
        {
            "claim_id": claim_id,
            "contract_version": 1,
            "statement_hash": statement_hash,
            "evidence_type": "CHECKER_CERTIFICATE",
            "evidence_strength": "HARD_MACHINE",
            "artifact_input_names": [certificate.name],
            "scope": {
                "claim_id": claim_id,
                "contract_version": 1,
                "statement_hash": statement_hash,
            },
            "provenance": {
                "actor": "python-enumerator-v1",
                "checked_subsets": enumeration["checked_subsets"],
            },
            "evidence_root": {
                "root_kind": "CHECKER",
                "verifier_profile_id": "python-enumerator-v1",
                "source_graph": {},
            },
        },
        (artifact(certificate, certificate.name, "application/json"),),
    )
    checker_evidence_id: str | None = None
    checker_artifact_id: str | None = None
    snapshot = kernel.inspect(handle.run_id)
    assert isinstance(snapshot, RunSnapshot)
    for item in snapshot.projection["evidence"]:
        if item["artifact_id"] in evidence_receipt.artifact_ids:
            checker_evidence_id = str(item["evidence_id"])
            checker_artifact_id = str(item["artifact_id"])
            break
    if checker_evidence_id is None or checker_artifact_id is None:
        raise RuntimeError("checker evidence was not projected")
    apply(
        kernel,
        capability,
        handle.run_id,
        "RecordLeanFeedback",
        {
            "claim_id": claim_id,
            "contract_version": 1,
            "environment_profile_id": "python-enumerator-v1",
            "toolchain": "python3-enumerator-v1",
            "source_artifact_id": support_id,
            "output_artifact_id": checker_artifact_id,
            "feedback_kind": "REPLAY_PASS",
            "diagnostic": enumeration,
        },
    )
    apply(
        kernel,
        capability,
        handle.run_id,
        "PromoteClaim",
        {
            "claim_id": claim_id,
            "target_axis": "MACHINE",
            "target_value": "CERTIFICATE_VERIFIED",
            "evidence_ids": [checker_evidence_id],
        },
    )
    apply(
        kernel,
        capability,
        handle.run_id,
        "RecordBudget",
        {
            "route_id": route_id,
            "event_kind": "ACTUAL",
            "resource_kind": "WALL_SECOND",
            "amount_microunits": int(checker_ms) * 1_000,
            "unit": "microsecond",
            "provider_usage": {
                "component": "python-enumerator-v1",
                "wall_time_ms": int(checker_ms),
            },
        },
    )
    apply(
        kernel,
        capability,
        handle.run_id,
        "Finalize",
        {
            "outcome": "UNRESOLVED",
            "terminal_claim_ids": [],
            "open_obligation_ids": [],
            "dossier_spec": {
                "format": "JSON",
                "include_raw_artifacts": False,
                "language": "zh-CN",
            },
        },
    )
    final = kernel.inspect(handle.run_id)
    assert isinstance(final, RunSnapshot)
    result = {
        "status": "SUCCESS",
        "run_id": handle.run_id,
        "run_status": final.status,
        "revision": final.revision,
        "attempts": attempt_results,
        "enumeration": {**enumeration, "wall_time_ms": int(checker_ms)},
        "claim": final.projection["claims"][0],
        "component_usage": final.projection["component_usage"],
        "budget_summary": final.projection["budget_summary"],
        "expected_final_outcome": "UNRESOLVED_WITH_CERTIFICATE_VERIFIED",
    }
    result_path = outputs / "rk5result.json"
    result_path.write_text(
        json.dumps(result, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(result, ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
