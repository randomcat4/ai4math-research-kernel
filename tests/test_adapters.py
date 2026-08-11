from __future__ import annotations

import hashlib
import json
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
    LeanSearchAdapter,
    ProcessResult,
    RethlasAdapter,
    SafeSubprocessRunner,
)


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
            tuple(argv), self.result.returncode, self.result.stdout, self.result.stderr
        )


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
            profile_mapping(name="rethlas", endpoint="http://registered.invalid/verify")
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
        AdapterProfile.from_mapping(profile_mapping(endpoint="http://registered.invalid/verify")),
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
        directory = output_root / "attempt-1"
        for name in ("decl", "sym", "elab", "lines"):
            (directory / f"{name}.json").write_text(json.dumps({"fixture": name}), encoding="utf-8")

    runner = FakeRunner(ProcessResult((), 0, "", ""), callback=create_outputs)
    adapter = JixiaAdapter(
        AdapterProfile.from_mapping(
            profile_mapping(
                name="jixia",
                argv_prefix=[str(binary)],
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
