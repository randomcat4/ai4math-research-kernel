"""Pinned clean Lean replay adapter."""

from __future__ import annotations

import hashlib
import re
import time
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from rk.adapters.base import (
    AdapterProfile,
    AdapterRequestError,
    ProcessRunner,
    SafeSubprocessRunner,
    confined_path,
    require_exact_keys,
)

_FORBIDDEN = re.compile(
    r"(?<![A-Za-z0-9_'])(?:sorry|admit|sorryAx|axiom|unsafe|native_decide)(?![A-Za-z0-9_'])"
)
_DECLARATION = re.compile(r"^[A-Za-z_][A-Za-z0-9_'.]*$")
_AXIOM_LIST = re.compile(r"depends on axioms:\s*\[([^\]]*)\]")


class LeanReplayAdapter:
    """Compile one source in a pinned project without trusting caller-supplied commands."""

    trust_limit = "LEAN_KERNEL_REPLAY"

    def __init__(
        self,
        profile: AdapterProfile,
        *,
        runner: ProcessRunner | None = None,
    ) -> None:
        profile.require(
            "argv_prefix",
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
            required=frozenset({"source_relpath", "output_relpath", "declarations", "environment"}),
            label="Lean replay request",
        )
        environment = request["environment"]
        declarations = request["declarations"]
        if not isinstance(environment, Mapping):
            raise AdapterRequestError("environment must be an object")
        if not isinstance(declarations, Sequence) or isinstance(declarations, (str, bytes)):
            raise AdapterRequestError("declarations must be an array")
        declaration_names = [str(item) for item in declarations]
        if (
            not 1 <= len(declaration_names) <= 64
            or len(set(declaration_names)) != len(declaration_names)
            or any(not _DECLARATION.fullmatch(item) for item in declaration_names)
        ):
            raise AdapterRequestError("declarations contains an invalid or duplicate name")
        project_root = self.profile.workspace_root
        output_root = self.profile.output_root
        binary_path = self.profile.binary_path
        expected_toolchain = self.profile.expected_toolchain
        assert project_root is not None
        assert output_root is not None
        assert binary_path is not None
        assert expected_toolchain is not None
        source = confined_path(project_root, str(request["source_relpath"]), label="source_relpath")
        output = confined_path(output_root, str(request["output_relpath"]), label="output_relpath")
        env = self.profile.select_environment(environment)
        common = {
            **self.profile.provenance(),
            "trust_limit": self.trust_limit,
            "environment_names": sorted(env),
            "toolchain": expected_toolchain,
            "binary_sha256": self.profile.binary_sha256,
        }
        if not source.is_file():
            raise AdapterRequestError("source_relpath does not name a regular file")
        if self._read_toolchain(project_root / "lean-toolchain") != expected_toolchain:
            return {**common, "status": "ENVIRONMENT_DRIFT", "kernel_verdict": "NOT_RUN"}
        if (
            not binary_path.is_file()
            or self._sha256_file(binary_path) != self.profile.binary_sha256
        ):
            return {**common, "status": "ENVIRONMENT_DRIFT", "kernel_verdict": "NOT_RUN"}
        try:
            source_text = source.read_text(encoding="utf-8")
        except (OSError, UnicodeDecodeError):
            return {**common, "status": "ADAPTER_SCHEMA_MISMATCH", "kernel_verdict": "NOT_RUN"}
        forbidden = sorted(set(_FORBIDDEN.findall(source_text)))
        if forbidden:
            return {
                **common,
                "status": "POLICY_VIOLATION",
                "kernel_verdict": "REJECTED",
                "forbidden_constructs": forbidden,
            }
        audit_source = output.with_suffix(output.suffix + ".axioms.lean")
        if output.exists() or audit_source.exists():
            return {
                **common,
                "status": "OUTPUT_COLLISION",
                "kernel_verdict": "NOT_RUN",
            }
        output.parent.mkdir(parents=True, exist_ok=True)
        argv = [*self.profile.argv_prefix, "-o", str(output), str(source.relative_to(project_root))]
        compile_started = time.monotonic_ns()
        completed = self.runner.run(
            argv,
            cwd=project_root,
            env=env,
            timeout=self.profile.timeout_seconds,
        )
        compile_wall_time_ms = (time.monotonic_ns() - compile_started) // 1_000_000
        transient = {"stdout": completed.stdout, "stderr": completed.stderr}
        if completed.returncode != 0:
            return {
                **common,
                "status": "LEAN_FEEDBACK",
                "kernel_verdict": "REJECTED",
                "exit_code": completed.returncode,
                "source_sha256": self._sha256_file(source),
                "transient_execution_output": transient,
            }
        if not output.is_file() or output.is_symlink():
            return {
                **common,
                "status": "ADAPTER_SCHEMA_MISMATCH",
                "kernel_verdict": "NOT_PROVIDED",
                "exit_code": completed.returncode,
                "transient_execution_output": transient,
            }
        audit_source.write_text(
            source_text
            + "\n"
            + "\n".join(f"#print axioms {name}" for name in declaration_names)
            + "\n",
            encoding="utf-8",
        )
        audit_started = time.monotonic_ns()
        audit = self.runner.run(
            [*self.profile.argv_prefix, str(audit_source)],
            cwd=project_root,
            env=env,
            timeout=self.profile.timeout_seconds,
        )
        audit_wall_time_ms = (time.monotonic_ns() - audit_started) // 1_000_000
        audit_output = f"{audit.stdout}\n{audit.stderr}"
        if audit.returncode != 0 or "sorryAx" in audit_output:
            return {
                **common,
                "status": "POLICY_VIOLATION",
                "kernel_verdict": "REJECTED",
                "exit_code": audit.returncode,
                "source_sha256": self._sha256_file(source),
                "transient_execution_output": {
                    **transient,
                    "axiom_stdout": audit.stdout,
                    "axiom_stderr": audit.stderr,
                },
            }
        reported = [name for name in declaration_names if name in audit_output]
        if len(reported) != len(declaration_names):
            return {
                **common,
                "status": "ADAPTER_SCHEMA_MISMATCH",
                "kernel_verdict": "NOT_PROVIDED",
                "exit_code": audit.returncode,
                "transient_execution_output": {
                    **transient,
                    "axiom_stdout": audit.stdout,
                    "axiom_stderr": audit.stderr,
                },
            }
        used_axioms = {
            axiom.strip()
            for match in _AXIOM_LIST.findall(audit_output)
            for axiom in match.split(",")
            if axiom.strip()
        }
        unexpected_axioms = sorted(used_axioms - set(self.profile.allowed_axioms))
        if unexpected_axioms:
            return {
                **common,
                "status": "POLICY_VIOLATION",
                "kernel_verdict": "REJECTED",
                "unexpected_axioms": unexpected_axioms,
                "exit_code": audit.returncode,
                "transient_execution_output": {
                    **transient,
                    "axiom_stdout": audit.stdout,
                    "axiom_stderr": audit.stderr,
                },
            }
        return {
            **common,
            "status": "COMPLETED",
            "kernel_verdict": "REPLAY_PASS",
            "exit_code": completed.returncode,
            "source_sha256": self._sha256_file(source),
            "output_sha256": self._sha256_file(output),
            "output_byte_count": output.stat().st_size,
            "axiom_dependencies": sorted(used_axioms),
            "phase_wall_time_ms": {
                "compile": compile_wall_time_ms,
                "axiom_audit": audit_wall_time_ms,
            },
            "transient_execution_output": {
                **transient,
                "axiom_stdout": audit.stdout,
                "axiom_stderr": audit.stderr,
            },
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
