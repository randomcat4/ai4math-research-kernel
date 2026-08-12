"""Pinned deterministic tool adapters for SMT, CAS, enumeration, and code execution."""

from __future__ import annotations

import hashlib
from collections.abc import Mapping
from pathlib import Path
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


class RegisteredFileToolAdapter:
    """Run a host-registered argv template over one immutable input file.

    The caller can select input bytes and declared expectation, but never a command line.  The
    adapter's trust class is supplied by the host-owned capability profile, not by tool output.
    """

    def __init__(
        self,
        profile: AdapterProfile,
        *,
        capability_kind: str,
        trust_limit: str,
        output_mode: str,
        runner: ProcessRunner | None = None,
    ) -> None:
        profile.require("argv_prefix", "workspace_root", "binary_path", "binary_sha256")
        if capability_kind not in {"SMT", "CAS", "EXACT_ENUMERATION", "CODE_EXECUTION"}:
            raise ValueError("unsupported deterministic capability kind")
        if output_mode not in {"smt-status", "json"}:
            raise ValueError("unsupported deterministic output mode")
        self.profile = profile
        self.capability_kind = capability_kind
        self.trust_limit = trust_limit
        self.output_mode = output_mode
        self.runner = runner or SafeSubprocessRunner()
        self.name = profile.name
        self.version = profile.version

    def run(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        require_exact_keys(
            request,
            required=frozenset({"input_relpath", "expected", "environment"}),
            label=f"{self.capability_kind} request",
        )
        environment = request["environment"]
        if not isinstance(environment, Mapping):
            raise AdapterRequestError("environment must be an object")
        workspace = self.profile.workspace_root
        binary = self.profile.binary_path
        assert workspace is not None
        assert binary is not None
        input_path = confined_path(
            workspace, str(request["input_relpath"]), label="input_relpath"
        )
        if not input_path.is_file() or input_path.is_symlink():
            raise AdapterRequestError("input_relpath does not name a regular file")
        if not binary.is_file() or self._sha256_file(binary) != self.profile.binary_sha256:
            return {**self.profile.provenance(), "status": "ENVIRONMENT_DRIFT"}
        env = self.profile.select_environment(environment)
        completed = self.runner.run(
            [*self.profile.argv_prefix, str(input_path.relative_to(workspace))],
            cwd=workspace,
            env=env,
            timeout=self.profile.timeout_seconds,
        )
        common = {
            **self.profile.provenance(),
            "capability_kind": self.capability_kind,
            "trust_limit": self.trust_limit,
            "binary_sha256": self.profile.binary_sha256,
            "input_sha256": self._sha256_file(input_path),
            "exit_code": completed.returncode,
            "environment_names": sorted(env),
            "transient_execution_output": {
                "stdout": completed.stdout,
                "stderr": completed.stderr,
            },
            "machine_axis_effect": "UNCHANGED",
        }
        if completed.returncode != 0:
            return {**common, "status": "FAILED", "payload": None}
        if len(completed.stdout.encode("utf-8")) > self.profile.max_response_bytes:
            return {**common, "status": "ADAPTER_SCHEMA_MISMATCH", "payload": None}
        try:
            if self.output_mode == "smt-status":
                value: Any = completed.stdout.strip()
                if value not in {"sat", "unsat", "unknown"}:
                    raise ValueError("unexpected SMT status")
            else:
                value = load_json(completed.stdout)
        except (UnicodeDecodeError, ValueError):
            return {**common, "status": "ADAPTER_SCHEMA_MISMATCH", "payload": None}
        if value != request["expected"]:
            return {**common, "status": "EXPECTATION_MISMATCH", "payload": value}
        return {**common, "status": "COMPLETED", "payload": value}

    @staticmethod
    def _sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
