"""jixia static Lean-structure adapter."""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from rk.adapters.base import (
    AdapterProfile,
    AdapterRequestError,
    ProcessRunner,
    SafeSubprocessRunner,
    canonical_json_sha256,
    confined_path,
    load_json,
    require_exact_keys,
)


class JixiaAdapter:
    """Extract declarations and proof states without impersonating the Lean kernel."""

    trust_limit = "STATIC_STRUCTURE_AND_PROOF_STATE"

    def __init__(
        self,
        profile: AdapterProfile,
        *,
        runner: ProcessRunner | None = None,
    ) -> None:
        profile.require(
            "argv_prefix",
            "repo_path",
            "workspace_root",
            "output_root",
            "expected_toolchain",
            "binary_path",
            "binary_sha256",
        )
        self.profile = profile
        self.runner = runner or SafeSubprocessRunner()
        self.name = profile.name
        self.version = profile.version

    def run(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        require_exact_keys(
            request,
            required=frozenset(
                {"source_relpath", "output_relpath", "include_initializers", "environment"}
            ),
            label="jixia request",
        )
        include_initializers = request["include_initializers"]
        environment = request["environment"]
        if not isinstance(include_initializers, bool):
            raise AdapterRequestError("include_initializers must be boolean")
        if not isinstance(environment, Mapping):
            raise AdapterRequestError("environment must be an object")
        project_root = self.profile.workspace_root
        output_root = self.profile.output_root
        repo_path = self.profile.repo_path
        binary_path = self.profile.binary_path
        expected_toolchain = self.profile.expected_toolchain
        assert project_root is not None
        assert output_root is not None
        assert repo_path is not None
        assert binary_path is not None
        assert expected_toolchain is not None
        source = confined_path(project_root, str(request["source_relpath"]), label="source_relpath")
        output = confined_path(output_root, str(request["output_relpath"]), label="output_relpath")
        env = self.profile.select_environment(environment)
        common = {
            **self.profile.provenance(),
            "trust_limit": self.trust_limit,
            "machine_axis_effect": "UNCHANGED",
            "kernel_verdict": "NOT_PROVIDED",
            "environment_names": sorted(env),
        }
        if not source.is_file():
            raise AdapterRequestError("source_relpath does not name a regular file")
        actual_project_toolchain = self._read_toolchain(project_root / "lean-toolchain")
        actual_jixia_toolchain = self._read_toolchain(repo_path / "lean-toolchain")
        if (
            actual_project_toolchain != expected_toolchain
            or actual_jixia_toolchain != expected_toolchain
        ):
            return {
                **common,
                "status": "ENVIRONMENT_DRIFT",
                "payload": None,
                "toolchain_match": False,
            }
        if (
            not binary_path.is_file()
            or self._sha256_file(binary_path) != self.profile.binary_sha256
        ):
            return {
                **common,
                "status": "ENVIRONMENT_DRIFT",
                "payload": None,
                "toolchain_match": True,
                "binary_match": False,
            }
        output.mkdir(parents=True, exist_ok=True)
        paths = {
            "declarations": output / "decl.json",
            "symbols": output / "sym.json",
            "elaboration": output / "elab.json",
            "lines": output / "lines.json",
        }
        argv = list(self.profile.argv_prefix)
        if include_initializers:
            argv.append("-i")
        argv.extend(
            (
                "-d",
                str(paths["declarations"]),
                "-s",
                str(paths["symbols"]),
                "-e",
                str(paths["elaboration"]),
                "-l",
                str(paths["lines"]),
                str(source.relative_to(project_root)),
            )
        )
        completed = self.runner.run(
            argv,
            cwd=project_root,
            env=env,
            timeout=self.profile.timeout_seconds,
        )
        execution = {"stdout": completed.stdout, "stderr": completed.stderr}
        if completed.returncode != 0:
            drift_markers = ("invalid header", "missing constants", "version mismatch")
            diagnostic = f"{completed.stdout}\n{completed.stderr}".lower()
            status = (
                "ENVIRONMENT_DRIFT"
                if any(marker in diagnostic for marker in drift_markers)
                else "LEAN_FEEDBACK"
            )
            return {
                **common,
                "status": status,
                "exit_code": completed.returncode,
                "payload": None,
                "transient_execution_output": execution,
            }

        parsed: dict[str, Any] = {}
        hashes: dict[str, str] = {}
        for label, path in paths.items():
            try:
                resolved_output = path.resolve(strict=True)
                resolved_output.relative_to(output.resolve())
            except (OSError, ValueError):
                return {
                    **common,
                    "status": "ADAPTER_SCHEMA_MISMATCH",
                    "exit_code": completed.returncode,
                    "payload": None,
                    "transient_execution_output": execution,
                }
            if path.is_symlink() or not resolved_output.is_file():
                return {
                    **common,
                    "status": "ADAPTER_SCHEMA_MISMATCH",
                    "exit_code": completed.returncode,
                    "payload": None,
                    "transient_execution_output": execution,
                }
            try:
                raw = resolved_output.read_bytes()
            except OSError:
                return {
                    **common,
                    "status": "ADAPTER_SCHEMA_MISMATCH",
                    "exit_code": completed.returncode,
                    "payload": None,
                    "transient_execution_output": execution,
                }
            if len(raw) > self.profile.max_response_bytes:
                return {
                    **common,
                    "status": "ADAPTER_SCHEMA_MISMATCH",
                    "exit_code": completed.returncode,
                    "payload": None,
                    "transient_execution_output": execution,
                }
            try:
                parsed[label] = load_json(raw)
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError):
                return {
                    **common,
                    "status": "ADAPTER_SCHEMA_MISMATCH",
                    "exit_code": completed.returncode,
                    "payload": None,
                    "transient_execution_output": execution,
                }
            hashes[label] = hashlib.sha256(raw).hexdigest()
        return {
            **common,
            "status": "COMPLETED",
            "exit_code": completed.returncode,
            "toolchain_match": True,
            "binary_match": True,
            "source_sha256": self._sha256_file(source),
            "outputs_sha256": hashes,
            "output_summary_hash": canonical_json_sha256(parsed),
            "payload": parsed,
            "transient_execution_output": execution,
        }

    @staticmethod
    def _read_toolchain(path: Path) -> str | None:
        try:
            return path.read_text(encoding="utf-8").strip()
        except (OSError, UnicodeDecodeError):
            return None

    @staticmethod
    def _sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
