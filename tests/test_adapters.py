from __future__ import annotations

import hashlib
import json
import sys
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from rk.adapters import (
    AdapterConfigurationError,
    AdapterProfile,
    AdapterRequestError,
    ArchonAdapter,
    HttpResponse,
    JixiaAdapter,
    LeanReplayAdapter,
    LeanSearchAdapter,
    LocalProofModelAdapter,
    OpenAICompatibleAdapter,
    OpenCodeAdapter,
    ProcessResult,
    RegisteredFileToolAdapter,
    RethlasAdapter,
    SafeSubprocessRunner,
)
from rk.adapters.opencode import OpenCodeJsonlRunner


def profile_mapping(**overrides: Any) -> dict[str, Any]:
    result: dict[str, Any] = {
        "name": "fixture",
        "version": "test-v1",
        "source_commit": "a" * 40,
        "timeout_seconds": 5,
        "max_response_bytes": 1024 * 1024,
        "env_whitelist": ["ALLOWED"],
    }
    result.update(overrides)
    return result


class FakeRunner:
    def __init__(self, result: ProcessResult, callback: Any = None) -> None:
        self.result = result
        self.callback = callback
        self.calls: list[dict[str, Any]] = []

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None,
        env: Mapping[str, str],
        timeout: float,
    ) -> ProcessResult:
        self.calls.append({"argv": tuple(argv), "cwd": cwd, "env": dict(env), "timeout": timeout})
        if self.callback is not None:
            self.callback()
        return ProcessResult(
            tuple(argv),
            self.result.returncode,
            self.result.stdout,
            self.result.stderr,
            self.result.protocol_completed,
            self.result.forced_termination,
        )


def test_opencode_runner_stops_after_protocol_completion() -> None:
    event = json.dumps(
        {
            "type": "step_finish",
            "part": {"reason": "stop", "tokens": {"input": 1, "output": 1, "total": 2}},
        }
    )
    program = f"import time; print({event!r}, flush=True); time.sleep(30)"
    started = time.monotonic()
    result = OpenCodeJsonlRunner(exit_grace_seconds=0.1).run(
        [sys.executable, "-c", program], cwd=None, env={}, timeout=5
    )

    assert time.monotonic() - started < 3
    assert result.protocol_completed is True
    assert result.forced_termination is True
    assert "step_finish" in result.stdout


def test_opencode_runner_waits_past_tool_call_finish() -> None:
    tool_finish = json.dumps(
        {"type": "step_finish", "part": {"reason": "tool-calls", "tokens": {"output": 1}}}
    )
    final_text = json.dumps({"type": "text", "part": {"text": "final"}})
    final_finish = json.dumps(
        {"type": "step_finish", "part": {"reason": "stop", "tokens": {"output": 1}}}
    )
    program = (
        f"import time; print({tool_finish!r}, flush=True); time.sleep(.2); "
        f"print({final_text!r}, flush=True); print({final_finish!r}, flush=True)"
    )

    result = OpenCodeJsonlRunner(exit_grace_seconds=0.1).run(
        [sys.executable, "-c", program], cwd=None, env={}, timeout=5
    )

    assert result.protocol_completed is True
    assert '"text": "final"' in result.stdout


class FakeHttpClient:
    def __init__(self, responses: Sequence[HttpResponse | Exception]) -> None:
        self.responses = list(responses)
        self.calls: list[dict[str, Any]] = []

    def post_json(
        self,
        url: str,
        payload: Mapping[str, Any],
        *,
        timeout: float,
        max_response_bytes: int,
    ) -> HttpResponse:
        self.calls.append({"url": url, "payload": dict(payload), "timeout": timeout})
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


def test_profile_rejects_unknown_fields_and_environment_names() -> None:
    with pytest.raises(AdapterConfigurationError):
        AdapterProfile.from_mapping(profile_mapping(machine_address="not-allowed"))
    profile = AdapterProfile.from_mapping(profile_mapping())
    with pytest.raises(AdapterRequestError):
        profile.select_environment({"SECRET": "must-not-pass"})
    with pytest.raises(AdapterConfigurationError):
        AdapterProfile.from_mapping(
            profile_mapping(endpoint="https://user:password@registered.invalid/search")
        )


def test_safe_runner_always_uses_argv_and_shell_false(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: dict[str, Any] = {}

    def fake_run(argv: list[str], **kwargs: Any) -> SimpleNamespace:
        observed["argv"] = argv
        observed.update(kwargs)
        return SimpleNamespace(returncode=0, stdout="{}", stderr="")

    monkeypatch.setattr("rk.adapters.base.subprocess.run", fake_run)
    result = SafeSubprocessRunner().run(
        ["registered-executable", "--flag"], cwd=None, env={}, timeout=1
    )
    assert result.returncode == 0
    assert observed["argv"] == ["registered-executable", "--flag"]
    assert observed["shell"] is False


def test_registered_smt_adapter_uses_fixed_command_and_matches_status(tmp_path: Path) -> None:
    source = tmp_path / "check.smt2"
    source.write_text("(check-sat)\n", encoding="utf-8")
    binary = tmp_path / "z3"
    binary.write_bytes(b"fixture binary")
    runner = FakeRunner(ProcessResult((), 0, "unsat\n", ""))
    adapter = RegisteredFileToolAdapter(
        AdapterProfile.from_mapping(
            profile_mapping(
                name="z3-smt2",
                argv_prefix=[str(binary), "-smt2"],
                workspace_root=str(tmp_path),
                binary_path=str(binary),
                binary_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),
            )
        ),
        capability_kind="SMT",
        trust_limit="HEURISTIC_EMPIRICAL_UNLESS_CERTIFICATE_REPLAYED",
        output_mode="smt-status",
        runner=runner,
    )

    result = adapter.run(
        {"input_relpath": "check.smt2", "expected": "unsat", "environment": {}}
    )

    assert result["status"] == "COMPLETED"
    assert result["machine_axis_effect"] == "UNCHANGED"
    assert runner.calls[0]["argv"][-2:] == ("-smt2", "check.smt2")


def test_registered_json_tool_does_not_promote_truth(tmp_path: Path) -> None:
    source = tmp_path / "input.json"
    source.write_text("{}\n", encoding="utf-8")
    binary = tmp_path / "tool"
    binary.write_bytes(b"fixture binary")
    adapter = RegisteredFileToolAdapter(
        AdapterProfile.from_mapping(
            profile_mapping(
                name="sympy-cas",
                argv_prefix=[str(binary)],
                workspace_root=str(tmp_path),
                binary_path=str(binary),
                binary_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),
            )
        ),
        capability_kind="CAS",
        trust_limit="HEURISTIC_EMPIRICAL",
        output_mode="json",
        runner=FakeRunner(ProcessResult((), 0, '{"expanded":"x**2 + 2*x + 1"}', "")),
    )

    result = adapter.run(
        {
            "input_relpath": "input.json",
            "expected": {"expanded": "x**2 + 2*x + 1"},
            "environment": {},
        }
    )

    assert result["status"] == "COMPLETED"
    assert result["trust_limit"] == "HEURISTIC_EMPIRICAL"
    assert result["machine_axis_effect"] == "UNCHANGED"


def test_archon_parses_pure_json_and_exit_three_is_pause(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    workspaces = tmp_path / "workspaces"
    repo.mkdir()
    workspaces.mkdir()
    payload = {"dry_run": True, "rounds": [{"round": 0, "planned": ["task-1"]}]}
    runner = FakeRunner(ProcessResult((), 0, json.dumps(payload), "banner"))
    adapter = ArchonAdapter(
        AdapterProfile.from_mapping(
            profile_mapping(
                name="archon",
                argv_prefix=["registered-python", "-m", "registered-module"],
                repo_path=str(repo),
                workspace_root=str(workspaces),
            )
        ),
        runner=runner,
    )
    request = {
        "target": ".",
        "rounds": 1,
        "resume_external_run_id": None,
        "backend": "default",
        "workspace_relpath": "attempt/work",
        "environment": {"ALLOWED": "yes"},
    }
    result = adapter.run(request)
    assert result["status"] == "COMPLETED"
    assert result["mathematical_axis_effect"] == "UNCHANGED"
    assert "--json" in runner.calls[0]["argv"]
    assert runner.calls[0]["env"] == {"ALLOWED": "yes"}

    paused = ArchonAdapter(
        adapter.profile, runner=FakeRunner(ProcessResult((), 3, "", "budget pause"))
    ).run(request)
    assert paused["status"] == "PAUSED"


def test_rethlas_correct_remains_soft_model() -> None:
    response = {
        "verification_report": {"summary": "ok", "critical_errors": [], "gaps": []},
        "verdict": "correct",
        "repair_hints": "",
    }
    client = FakeHttpClient([HttpResponse(200, json.dumps(response).encode())])
    adapter = RethlasAdapter(
        AdapterProfile.from_mapping(
            profile_mapping(name="rethlas", endpoint="https://registered.invalid/verify")
        ),
        client=client,
    )
    result = adapter.run({"statement": "P", "proof": "Proof."})
    assert result["status"] == "COMPLETED"
    assert result["evidence_strength"] == "SOFT_MODEL"
    assert result["machine_axis_effect"] == "UNCHANGED"


def test_rethlas_rejects_schema_that_claims_correct_with_gaps() -> None:
    response = {
        "verification_report": {
            "summary": "not ok",
            "critical_errors": [],
            "gaps": [{"location": "1", "issue": "missing"}],
        },
        "verdict": "correct",
        "repair_hints": "",
    }
    adapter = RethlasAdapter(
        AdapterProfile.from_mapping(profile_mapping(endpoint="https://registered.invalid/verify")),
        client=FakeHttpClient([HttpResponse(200, json.dumps(response).encode())]),
    )
    assert adapter.run({"statement": "P", "proof": "Proof."})["status"] == (
        "ADAPTER_SCHEMA_MISMATCH"
    )


def leansearch_hit() -> dict[str, Any]:
    return {
        "result": {
            "module_name": ["Mathlib"],
            "kind": "theorem",
            "name": ["Nat", "add_comm"],
            "signature": "Nat.add_comm",
            "type": "a + b = b + a",
            "value": None,
            "docstring": None,
            "informal_name": None,
            "informal_description": None,
        },
        "distance": 0.1,
    }


def test_leansearch_returns_only_premise_candidates_and_retries() -> None:
    client = FakeHttpClient(
        [HttpResponse(503, b"busy"), HttpResponse(200, json.dumps([leansearch_hit()]).encode())]
    )
    sleeps: list[float] = []
    adapter = LeanSearchAdapter(
        AdapterProfile.from_mapping(
            profile_mapping(
                name="leansearch",
                endpoint="https://registered.invalid/search",
                max_retries=1,
                retry_statuses=[503],
                backoff_seconds=0.25,
            )
        ),
        client=client,
        sleeper=sleeps.append,
    )
    result = adapter.run(
        {"query": ["commutativity"], "num_results": 5, "rerank": True, "retrieve_k": 20}
    )
    assert result["status"] == "COMPLETED"
    assert result["trust_limit"] == "PREMISE_CANDIDATE"
    assert result["machine_axis_effect"] == "UNCHANGED"
    assert sleeps == [0.25]


def test_openai_compatible_model_adapter_has_no_tool_surface() -> None:
    response = {
        "choices": [{"message": {"content": "```lean\ntheorem t : True := by trivial\n```"}}],
        "usage": {
            "prompt_tokens": 10,
            "completion_tokens": 7,
            "total_tokens": 17,
            "completion_tokens_details": {"reasoning_tokens": 3},
        },
    }
    client = FakeHttpClient([HttpResponse(200, json.dumps(response).encode())])
    adapter = OpenAICompatibleAdapter(
        AdapterProfile.from_mapping(
            profile_mapping(
                name="deepseek-http",
                endpoint="https://registered.invalid/v1/chat/completions",
                env_whitelist=["DEEPSEEK_API_KEY"],
            )
        ),
        client=client,
    )

    result = adapter.run(
        {
            "prompt": "prove",
            "model": "provider/model",
            "max_tokens": 100,
            "environment": {"DEEPSEEK_API_KEY": "secret"},
        }
    )

    assert result["status"] == "COMPLETED"
    assert result["tool_surface"] == "NONE"
    assert result["provider_request"]["tools"] == []
    assert result["usage"]["reasoning_tokens"] == 3
    assert client.calls[0]["payload"]["_rk_authorization_bearer"] == "secret"


def test_leansearch_empty_is_incomplete_not_no_theorem() -> None:
    adapter = LeanSearchAdapter(
        AdapterProfile.from_mapping(profile_mapping(endpoint="https://registered.invalid/search")),
        client=FakeHttpClient([HttpResponse(200, b"[]")]),
    )
    result = adapter.run(
        {"query": ["missing"], "num_results": 5, "rerank": False, "retrieve_k": None}
    )
    assert result["status"] == "SEARCH_INCOMPLETE"


def test_jixia_refuses_toolchain_drift_without_execution(tmp_path: Path) -> None:
    repo = tmp_path / "jixia"
    project = tmp_path / "project"
    output = tmp_path / "output"
    repo.mkdir()
    project.mkdir()
    output.mkdir()
    (repo / "lean-toolchain").write_text("lean-a\n", encoding="utf-8")
    (project / "lean-toolchain").write_text("lean-b\n", encoding="utf-8")
    (project / "Main.lean").write_text("theorem t : True := by trivial\n", encoding="utf-8")
    binary = repo / "jixia-bin"
    binary.write_bytes(b"fixture binary")
    runner = FakeRunner(ProcessResult((), 0, "", ""))
    adapter = JixiaAdapter(
        AdapterProfile.from_mapping(
            profile_mapping(
                name="jixia",
                argv_prefix=[str(binary)],
                preflight_argv_prefix=["lean"],
                repo_path=str(repo),
                workspace_root=str(project),
                output_root=str(output),
                expected_toolchain="lean-a",
                binary_path=str(binary),
                binary_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),
            )
        ),
        runner=runner,
    )
    result = adapter.run(
        {
            "source_relpath": "Main.lean",
            "output_relpath": "attempt-1",
            "include_initializers": True,
            "environment": {},
        }
    )
    assert result["status"] == "ENVIRONMENT_DRIFT"
    assert runner.calls == []


def test_jixia_outputs_are_static_only(tmp_path: Path) -> None:
    repo = tmp_path / "jixia"
    project = tmp_path / "project"
    output_root = tmp_path / "output"
    repo.mkdir()
    project.mkdir()
    output_root.mkdir()
    for root in (repo, project):
        (root / "lean-toolchain").write_text("lean-exact\n", encoding="utf-8")
    source = project / "Main.lean"
    source.write_text("theorem t : True := by trivial\n", encoding="utf-8")
    binary = repo / "jixia-bin"
    binary.write_bytes(b"fixture binary")

    def create_outputs() -> None:
        object_path = project / ".lake/build/lib/lean/Main.olean"
        object_path.parent.mkdir(parents=True, exist_ok=True)
        object_path.write_bytes(b"fixture object")
        directory = output_root / "attempt-1"
        for name in ("decl", "sym", "elab", "lines"):
            (directory / f"{name}.json").write_text(json.dumps({"fixture": name}), encoding="utf-8")

    runner = FakeRunner(ProcessResult((), 0, "", ""), callback=create_outputs)
    adapter = JixiaAdapter(
        AdapterProfile.from_mapping(
            profile_mapping(
                name="jixia",
                argv_prefix=[str(binary)],
                preflight_argv_prefix=["lean"],
                repo_path=str(repo),
                workspace_root=str(project),
                output_root=str(output_root),
                expected_toolchain="lean-exact",
                binary_path=str(binary),
                binary_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),
            )
        ),
        runner=runner,
    )
    result = adapter.run(
        {
            "source_relpath": "Main.lean",
            "output_relpath": "attempt-1",
            "include_initializers": False,
            "environment": {},
        }
    )
    assert result["status"] == "COMPLETED"
    assert result["trust_limit"] == "STATIC_STRUCTURE_AND_PROOF_STATE"
    assert result["kernel_verdict"] == "NOT_PROVIDED"
    assert result["machine_axis_effect"] == "UNCHANGED"
    assert set(result["phase_wall_time_ms"]) == {"preflight_compile", "analysis"}


def test_jixia_refuses_stale_output_directory(tmp_path: Path) -> None:
    repo = tmp_path / "jixia"
    project = tmp_path / "project"
    output_root = tmp_path / "output"
    repo.mkdir()
    project.mkdir()
    output_root.mkdir()
    for root in (repo, project):
        (root / "lean-toolchain").write_text("lean-exact\n", encoding="utf-8")
    (project / "Main.lean").write_text("theorem t : True := by trivial\n", encoding="utf-8")
    (output_root / "attempt-1").mkdir()
    binary = repo / "jixia-bin"
    binary.write_bytes(b"fixture binary")
    runner = FakeRunner(ProcessResult((), 0, "", ""))
    adapter = JixiaAdapter(
        AdapterProfile.from_mapping(
            profile_mapping(
                name="jixia",
                argv_prefix=[str(binary)],
                preflight_argv_prefix=["lean"],
                repo_path=str(repo),
                workspace_root=str(project),
                output_root=str(output_root),
                expected_toolchain="lean-exact",
                binary_path=str(binary),
                binary_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),
            )
        ),
        runner=runner,
    )

    result = adapter.run(
        {
            "source_relpath": "Main.lean",
            "output_relpath": "attempt-1",
            "include_initializers": False,
            "environment": {},
        }
    )

    assert result["status"] == "OUTPUT_COLLISION"
    assert runner.calls == []


def test_jixia_preflight_writes_object_to_lake_module_directory(tmp_path: Path) -> None:
    repo = tmp_path / "jixia"
    project = tmp_path / "project"
    output_root = tmp_path / "output"
    repo.mkdir()
    project.mkdir()
    output_root.mkdir()
    for root in (repo, project):
        (root / "lean-toolchain").write_text("lean-exact\n", encoding="utf-8")
    source = project / "Nested" / "Main.lean"
    source.parent.mkdir()
    source.write_text("theorem t : True := by trivial\n", encoding="utf-8")
    binary = repo / "jixia-bin"
    binary.write_bytes(b"fixture binary")

    def create_outputs() -> None:
        object_path = project / ".lake/build/lib/lean/Nested/Main.olean"
        object_path.parent.mkdir(parents=True, exist_ok=True)
        object_path.write_bytes(b"fixture object")
        directory = output_root / "attempt-1"
        for name in ("decl", "sym", "elab", "lines"):
            (directory / f"{name}.json").write_text("{}", encoding="utf-8")

    runner = FakeRunner(ProcessResult((), 0, "", ""), callback=create_outputs)
    adapter = JixiaAdapter(
        AdapterProfile.from_mapping(
            profile_mapping(
                name="jixia",
                argv_prefix=[str(binary)],
                preflight_argv_prefix=["lean"],
                repo_path=str(repo),
                workspace_root=str(project),
                output_root=str(output_root),
                expected_toolchain="lean-exact",
                binary_path=str(binary),
                binary_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),
            )
        ),
        runner=runner,
    )

    result = adapter.run(
        {
            "source_relpath": "Nested/Main.lean",
            "output_relpath": "attempt-1",
            "include_initializers": False,
            "environment": {},
        }
    )

    assert result["status"] == "COMPLETED"
    expected = project / ".lake/build/lib/lean/Nested/Main.olean"
    assert str(expected) in runner.calls[0]["argv"]


def test_lean_replay_rejects_forbidden_construct_before_execution(tmp_path: Path) -> None:
    project = tmp_path / "project"
    output = tmp_path / "output"
    project.mkdir()
    output.mkdir()
    (project / "lean-toolchain").write_text("lean-exact\n", encoding="utf-8")
    (project / "Main.lean").write_text("theorem t : True := by sorry\n", encoding="utf-8")
    binary = project / "lean"
    binary.write_bytes(b"fixture binary")
    runner = FakeRunner(ProcessResult((), 0, "", ""))
    adapter = LeanReplayAdapter(
        AdapterProfile.from_mapping(
            profile_mapping(
                name="lean-replay",
                argv_prefix=[str(binary)],
                workspace_root=str(project),
                output_root=str(output),
                expected_toolchain="lean-exact",
                binary_path=str(binary),
                binary_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),
            )
        ),
        runner=runner,
    )
    result = adapter.run(
        {
            "source_relpath": "Main.lean",
            "output_relpath": "Main.olean",
            "declarations": ["t"],
            "environment": {},
        }
    )
    assert result["status"] == "POLICY_VIOLATION"
    assert result["kernel_verdict"] == "REJECTED"
    assert runner.calls == []


@pytest.mark.parametrize(
    "body",
    ["by exact (by native_decide)", "by exact (by sorry)", "by admit"],
)
def test_lean_replay_rejects_forbidden_construct_next_to_punctuation(
    tmp_path: Path, body: str
) -> None:
    project = tmp_path / "project"
    output = tmp_path / "output"
    project.mkdir()
    output.mkdir()
    (project / "lean-toolchain").write_text("lean-exact\n", encoding="utf-8")
    (project / "Main.lean").write_text(f"theorem t : True := {body}\n", encoding="utf-8")
    binary = project / "lean"
    binary.write_bytes(b"fixture binary")
    runner = FakeRunner(ProcessResult((), 0, "", ""))
    adapter = LeanReplayAdapter(
        AdapterProfile.from_mapping(
            profile_mapping(
                name="lean-replay",
                argv_prefix=[str(binary)],
                workspace_root=str(project),
                output_root=str(output),
                expected_toolchain="lean-exact",
                binary_path=str(binary),
                binary_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),
            )
        ),
        runner=runner,
    )

    result = adapter.run(
        {
            "source_relpath": "Main.lean",
            "output_relpath": "Main.olean",
            "declarations": ["t"],
            "environment": {},
        }
    )

    assert result["status"] == "POLICY_VIOLATION"
    assert runner.calls == []


def test_lean_replay_refuses_stale_output(tmp_path: Path) -> None:
    project = tmp_path / "project"
    output = tmp_path / "output"
    project.mkdir()
    output.mkdir()
    (project / "lean-toolchain").write_text("lean-exact\n", encoding="utf-8")
    (project / "Main.lean").write_text("theorem t : True := by trivial\n", encoding="utf-8")
    (output / "Main.olean").write_bytes(b"stale")
    binary = project / "lean"
    binary.write_bytes(b"fixture binary")
    runner = FakeRunner(ProcessResult((), 0, "", ""))
    adapter = LeanReplayAdapter(
        AdapterProfile.from_mapping(
            profile_mapping(
                name="lean-replay",
                argv_prefix=[str(binary)],
                workspace_root=str(project),
                output_root=str(output),
                expected_toolchain="lean-exact",
                binary_path=str(binary),
                binary_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),
            )
        ),
        runner=runner,
    )

    result = adapter.run(
        {
            "source_relpath": "Main.lean",
            "output_relpath": "Main.olean",
            "declarations": ["t"],
            "environment": {},
        }
    )

    assert result["status"] == "OUTPUT_COLLISION"
    assert runner.calls == []


def test_lean_replay_returns_kernel_pass_only_with_pinned_output(tmp_path: Path) -> None:
    project = tmp_path / "project"
    output = tmp_path / "output"
    project.mkdir()
    output.mkdir()
    (project / "lean-toolchain").write_text("lean-exact\n", encoding="utf-8")
    source = project / "Main.lean"
    source.write_text("theorem t : True := by trivial\n", encoding="utf-8")
    binary = project / "lean"
    binary.write_bytes(b"fixture binary")

    def create_output() -> None:
        (output / "Main.olean").write_bytes(b"olean fixture")

    adapter = LeanReplayAdapter(
        AdapterProfile.from_mapping(
            profile_mapping(
                name="lean-replay",
                argv_prefix=[str(binary)],
                workspace_root=str(project),
                output_root=str(output),
                expected_toolchain="lean-exact",
                binary_path=str(binary),
                binary_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),
            )
        ),
        runner=FakeRunner(
            ProcessResult((), 0, "'t' depends on axioms: []", ""), callback=create_output
        ),
    )
    result = adapter.run(
        {
            "source_relpath": "Main.lean",
            "output_relpath": "Main.olean",
            "declarations": ["t"],
            "environment": {},
        }
    )
    assert result["status"] == "COMPLETED"
    assert result["kernel_verdict"] == "REPLAY_PASS"
    assert result["output_byte_count"] == len(b"olean fixture")
    assert set(result["phase_wall_time_ms"]) == {"compile", "axiom_audit"}


def test_local_proof_model_is_soft_only_and_records_usage(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    output_root = tmp_path / "output"
    workspace.mkdir()
    output_root.mkdir()
    request_path = workspace / "request.json"
    request_path.write_text('{"formal":"theorem t : True := by sorry"}\n', encoding="utf-8")
    binary = workspace / "python"
    binary.write_bytes(b"fixture binary")

    def create_output() -> None:
        (output_root / "attempt-1.json").write_text(
            json.dumps(
                {
                    "text": "```lean\ntheorem t : True := by trivial\n```",
                    "usage": {"input_tokens": 12, "output_tokens": 9, "gpu_peak_bytes": 1024},
                }
            ),
            encoding="utf-8",
        )

    adapter = LocalProofModelAdapter(
        AdapterProfile.from_mapping(
            profile_mapping(
                name="deepseek-prover-v2-7b",
                argv_prefix=[str(binary), "invoke", "deepseek-prover"],
                workspace_root=str(workspace),
                output_root=str(output_root),
                binary_path=str(binary),
                binary_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),
            )
        ),
        runner=FakeRunner(ProcessResult((), 0, "", ""), callback=create_output),
    )
    result = adapter.run(
        {
            "input_relpath": "request.json",
            "output_relpath": "attempt-1.json",
            "environment": {},
        }
    )

    assert result["status"] == "COMPLETED"
    assert result["trust_limit"] == "SOFT_CANDIDATE_ONLY"
    assert result["machine_axis_effect"] == "UNCHANGED"
    assert result["usage"]["output_tokens"] == 9


def test_local_proof_model_does_not_complete_at_generation_limit(tmp_path: Path) -> None:
    workspace = tmp_path / "workspace"
    output_root = tmp_path / "output"
    workspace.mkdir()
    output_root.mkdir()
    (workspace / "request.json").write_text('{"formal":"theorem t : True"}', encoding="utf-8")
    binary = workspace / "python"
    binary.write_bytes(b"fixture binary")

    def create_output() -> None:
        (output_root / "attempt-1.json").write_text(
            json.dumps(
                {
                    "text": "incomplete candidate",
                    "usage": {"output_tokens": 8192, "hit_token_limit": True},
                }
            ),
            encoding="utf-8",
        )

    adapter = LocalProofModelAdapter(
        AdapterProfile.from_mapping(
            profile_mapping(
                name="deepseek-prover-v2-7b",
                argv_prefix=[str(binary)],
                workspace_root=str(workspace),
                output_root=str(output_root),
                binary_path=str(binary),
                binary_sha256=hashlib.sha256(binary.read_bytes()).hexdigest(),
            )
        ),
        runner=FakeRunner(ProcessResult((), 0, "", ""), callback=create_output),
    )

    result = adapter.run(
        {
            "input_relpath": "request.json",
            "output_relpath": "attempt-1.json",
            "environment": {},
        }
    )

    assert result["status"] == "GENERATION_LIMIT"
    assert result["machine_axis_effect"] == "UNCHANGED"


def test_opencode_normalizes_text_and_usage_without_truth_authority(tmp_path: Path) -> None:
    event_log = "\n".join(
        (
            json.dumps({"type": "step_start", "part": {}}),
            json.dumps({"type": "text", "part": {"text": "proof candidate"}}),
            json.dumps(
                {
                    "type": "step_finish",
                    "part": {
                        "tokens": {
                            "input": 10,
                            "output": 3,
                            "reasoning": 2,
                            "total": 17,
                            "cache": {"read": 2, "write": 0},
                        }
                    },
                }
            ),
        )
    )
    adapter = OpenCodeAdapter(
        AdapterProfile.from_mapping(
            profile_mapping(
                name="opencode",
                argv_prefix=["opencode"],
                workspace_root=str(tmp_path),
            )
        ),
        runner=FakeRunner(ProcessResult((), 0, event_log, "")),
    )
    result = adapter.run(
        {
            "prompt": "prove it",
            "model": "provider/model",
            "workspace_relpath": "attempt-1",
            "environment": {},
        }
    )
    assert result["status"] == "COMPLETED"
    assert result["payload"]["text"] == "proof candidate"
    assert result["usage"]["total_tokens"] == 17
    assert result["machine_axis_effect"] == "UNCHANGED"


def test_opencode_fails_when_model_uses_unregistered_tool(tmp_path: Path) -> None:
    event_log = "\n".join(
        (
            json.dumps({"type": "tool_use", "part": {"tool": "bash"}}),
            json.dumps(
                {
                    "type": "step_finish",
                    "part": {
                        "tokens": {
                            "input": 1,
                            "output": 1,
                            "reasoning": 0,
                            "total": 2,
                            "cache": {"read": 0, "write": 0},
                        }
                    },
                }
            ),
        )
    )
    adapter = OpenCodeAdapter(
        AdapterProfile.from_mapping(
            profile_mapping(
                name="opencode",
                argv_prefix=["opencode"],
                workspace_root=str(tmp_path),
                max_tool_calls=0,
            )
        ),
        runner=FakeRunner(ProcessResult((), 0, event_log, "")),
    )
    result = adapter.run(
        {
            "prompt": "prove it",
            "model": "provider/model",
            "workspace_relpath": "attempt-1",
            "environment": {},
        }
    )
    assert result["status"] == "POLICY_VIOLATION"
    assert result["tool_calls"] == ["bash"]


@pytest.mark.parametrize("event_log", ["", json.dumps({"type": "text", "part": {"text": "x"}})])
def test_opencode_requires_text_and_finish_event(tmp_path: Path, event_log: str) -> None:
    adapter = OpenCodeAdapter(
        AdapterProfile.from_mapping(
            profile_mapping(name="opencode", argv_prefix=["opencode"], workspace_root=str(tmp_path))
        ),
        runner=FakeRunner(ProcessResult((), 0, event_log, "")),
    )

    result = adapter.run(
        {
            "prompt": "prove it",
            "model": "provider/model",
            "workspace_relpath": "attempt-1",
            "environment": {},
        }
    )

    assert result["status"] == "ADAPTER_SCHEMA_MISMATCH"


def test_opencode_requires_deny_all_config_before_execution(tmp_path: Path) -> None:
    config = tmp_path / "opencode.json"
    config.write_text(json.dumps({"permission": {"bash": "deny"}}), encoding="utf-8")
    runner = FakeRunner(ProcessResult((), 0, "", ""))
    adapter = OpenCodeAdapter(
        AdapterProfile.from_mapping(
            profile_mapping(
                name="opencode",
                argv_prefix=["opencode"],
                workspace_root=str(tmp_path),
                env_whitelist=["OPENCODE_CONFIG"],
                require_deny_all_tools=True,
            )
        ),
        runner=runner,
    )

    with pytest.raises(AdapterRequestError, match="does not deny every tool"):
        adapter.run(
            {
                "prompt": "prove it",
                "model": "provider/model",
                "workspace_relpath": "attempt-1",
                "environment": {"OPENCODE_CONFIG": str(config)},
            }
        )
    assert runner.calls == []
