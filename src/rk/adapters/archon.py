"""Thin Archon-Horizon execution adapter."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from rk.adapters.base import (
    AdapterProfile,
    AdapterRequestError,
    ProcessRunner,
    SafeSubprocessRunner,
    confined_path,
    load_json,
    require_exact_keys,
)


class ArchonAdapter:
    """Run one JSON-mode Horizon command without interpreting mathematical success."""

    trust_limit = "EXECUTION_ORCHESTRATOR"

    def __init__(
        self,
        profile: AdapterProfile,
        *,
        runner: ProcessRunner | None = None,
    ) -> None:
        profile.require("argv_prefix", "repo_path", "workspace_root")
        self.profile = profile
        self.runner = runner or SafeSubprocessRunner()
        self.name = profile.name
        self.version = profile.version

    def run(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        require_exact_keys(
            request,
            required=frozenset(
                {
                    "target",
                    "rounds",
                    "resume_external_run_id",
                    "backend",
                    "workspace_relpath",
                    "environment",
                }
            ),
            label="Archon request",
        )
        target = request["target"]
        rounds = request["rounds"]
        resume_id = request["resume_external_run_id"]
        backend = request["backend"]
        environment = request["environment"]
        if not isinstance(target, str) or not target or target.startswith("-"):
            raise AdapterRequestError("target must be a non-option string")
        if isinstance(rounds, bool) or not isinstance(rounds, int) or rounds <= 0:
            raise AdapterRequestError("rounds must be a positive integer")
        if resume_id is not None and (
            not isinstance(resume_id, str) or not resume_id or resume_id.startswith("-")
        ):
            raise AdapterRequestError("resume_external_run_id must be null or a safe string")
        if not isinstance(backend, str) or not backend or backend in {"interactive", "bare"}:
            raise AdapterRequestError("backend is not permitted")
        if not isinstance(environment, Mapping):
            raise AdapterRequestError("environment must be an object")

        workspace_root = self.profile.workspace_root
        repo_path = self.profile.repo_path
        assert workspace_root is not None and repo_path is not None
        workspace = confined_path(
            workspace_root, str(request["workspace_relpath"]), label="workspace_relpath"
        )
        env = self.profile.select_environment(environment)
        argv = [*self.profile.argv_prefix, "--root", str(workspace), "run"]
        if resume_id is not None:
            argv.extend(("--resume", resume_id))
        else:
            argv.append(target)
        argv.extend(("--rounds", str(rounds), "--backend", backend, "--no-dashboard", "--json"))
        completed = self.runner.run(
            argv,
            cwd=repo_path,
            env=env,
            timeout=self.profile.timeout_seconds,
        )
        transient = {"stdout": completed.stdout, "stderr": completed.stderr}
        common: dict[str, Any] = {
            **self.profile.provenance(),
            "trust_limit": self.trust_limit,
            "exit_code": completed.returncode,
            "environment_names": sorted(env),
            "transient_execution_output": transient,
            "mathematical_axis_effect": "UNCHANGED",
        }
        if completed.returncode == 3:
            return {
                **common,
                "status": "PAUSED",
                "external_run_id": resume_id,
                "payload": None,
            }
        if completed.returncode != 0:
            return {
                **common,
                "status": "ENVIRONMENT_ERROR" if completed.returncode not in {1} else "FAILED",
                "external_run_id": resume_id,
                "payload": None,
            }
        if len(completed.stdout.encode("utf-8")) > self.profile.max_response_bytes:
            return {**common, "status": "ADAPTER_SCHEMA_MISMATCH", "payload": None}
        try:
            payload = load_json(completed.stdout)
            self._validate_payload(payload)
        except (ValueError, TypeError):
            return {**common, "status": "ADAPTER_SCHEMA_MISMATCH", "payload": None}
        return {
            **common,
            "status": "COMPLETED",
            "external_run_id": resume_id,
            "payload": payload,
        }

    @staticmethod
    def _validate_payload(payload: Any) -> None:
        if not isinstance(payload, Mapping):
            raise ValueError("Archon JSON must be an object")
        require_exact_keys(
            payload,
            required=frozenset({"dry_run", "rounds"}),
            label="Archon response",
        )
        if not isinstance(payload["dry_run"], bool):
            raise ValueError("dry_run must be boolean")
        rounds = payload["rounds"]
        if not isinstance(rounds, Sequence) or isinstance(rounds, (str, bytes)):
            raise ValueError("rounds must be an array")
        for index, report in enumerate(rounds):
            if not isinstance(report, Mapping):
                raise ValueError(f"rounds[{index}] must be an object")
            if payload["dry_run"]:
                required = frozenset({"round", "planned"})
            else:
                required = frozenset({"round", "tasks_run", "tasks_blocked", "tasks_unrunnable"})
            require_exact_keys(report, required=required, label=f"rounds[{index}]")
            if isinstance(report["round"], bool) or not isinstance(report["round"], int):
                raise ValueError("round must be an integer")
            for field_name in required - {"round"}:
                items = report[field_name]
                if not isinstance(items, Sequence) or isinstance(items, (str, bytes)):
                    raise ValueError(f"{field_name} must be an array")
                if any(not isinstance(item, (str, Mapping)) for item in items):
                    raise ValueError(f"{field_name} contains an invalid item")
