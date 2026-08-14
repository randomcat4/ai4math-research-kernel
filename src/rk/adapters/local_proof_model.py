"""Adapter for host-registered local proof-model inference commands."""

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
    confined_path,
    load_json,
    require_exact_keys,
)


class LocalProofModelAdapter:
    """Run a pinned local generator while keeping every result soft-only."""

    trust_limit = "SOFT_CANDIDATE_ONLY"

    def __init__(
        self,
        profile: AdapterProfile,
        *,
        runner: ProcessRunner | None = None,
    ) -> None:
        profile.require(
            "argv_prefix", "workspace_root", "output_root", "binary_path", "binary_sha256"
        )
        self.profile = profile
        self.runner = runner or SafeSubprocessRunner()
        self.name = profile.name
        self.version = profile.version

    def run(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        structured = "model" in request or "prompt" in request or "max_new_tokens" in request
        require_exact_keys(
            request,
            required=(
                frozenset({"model", "prompt", "max_new_tokens", "output_relpath", "environment"})
                if structured
                else frozenset({"input_relpath", "output_relpath", "environment"})
            ),
            label="local proof model request",
        )
        environment = request["environment"]
        if not isinstance(environment, Mapping):
            raise AdapterRequestError("environment must be an object")
        workspace = self.profile.workspace_root
        output_root = self.profile.output_root
        binary = self.profile.binary_path
        assert workspace is not None
        assert output_root is not None
        assert binary is not None
        if structured:
            model, prompt, max_new_tokens = (
                request["model"], request["prompt"], request["max_new_tokens"]
            )
            if model not in {"qed-nano", "deepseek-prover"}:
                raise AdapterRequestError("model must be qed-nano or deepseek-prover")
            if (
                not isinstance(prompt, str)
                or not prompt.strip()
                or len(prompt.encode("utf-8")) > 64 * 1024
            ):
                raise AdapterRequestError("prompt must be non-empty and at most 64 KiB")
            if (
                not isinstance(max_new_tokens, int)
                or isinstance(max_new_tokens, bool)
                or not 1 <= max_new_tokens <= 1024
            ):
                raise AdapterRequestError("max_new_tokens must be between 1 and 1024")
            input_payload = {
                "model": model, "prompt": prompt, "max_new_tokens": max_new_tokens
            }
            input_sha256 = hashlib.sha256(
                json.dumps(
                    input_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
                ).encode("utf-8")
            ).hexdigest()
            input_path = confined_path(
                workspace,
                f".rk-local-proof/{input_sha256}.json",
                label="materialized_input_relpath",
            )
            input_path.parent.mkdir(parents=True, exist_ok=True)
            encoded = json.dumps(
                input_payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ).encode("utf-8") + b"\n"
            if input_path.exists() and input_path.read_bytes() != encoded:
                return {**self.profile.provenance(), "status": "INPUT_COLLISION"}
            if not input_path.exists():
                input_path.write_bytes(encoded)
        else:
            input_path = confined_path(
                workspace, str(request["input_relpath"]), label="input_relpath"
            )
        output_path = confined_path(
            output_root, str(request["output_relpath"]), label="output_relpath"
        )
        if not input_path.is_file() or input_path.is_symlink():
            raise AdapterRequestError("input_relpath does not name a regular file")
        if output_path.exists():
            return {**self.profile.provenance(), "status": "OUTPUT_COLLISION"}
        if not binary.is_file() or self._sha256_file(binary) != self.profile.binary_sha256:
            return {**self.profile.provenance(), "status": "ENVIRONMENT_DRIFT"}
        output_path.parent.mkdir(parents=True, exist_ok=True)
        env = self.profile.select_environment(environment)
        completed = self.runner.run(
            [*self.profile.argv_prefix, str(input_path), str(output_path)],
            cwd=workspace,
            env=env,
            timeout=self.profile.timeout_seconds,
        )
        common = {
            **self.profile.provenance(),
            "trust_limit": self.trust_limit,
            "machine_axis_effect": "UNCHANGED",
            "input_sha256": self._sha256_file(input_path),
            "binary_sha256": self.profile.binary_sha256,
            "environment_names": sorted(env),
            "exit_code": completed.returncode,
            "transient_execution_output": {
                "stdout": completed.stdout,
                "stderr": completed.stderr,
            },
        }
        if completed.returncode != 0:
            return {**common, "status": "FAILED", "usage": {"cost_unknown": True}}
        if not output_path.is_file() or output_path.is_symlink():
            return {**common, "status": "ADAPTER_SCHEMA_MISMATCH"}
        try:
            raw = output_path.read_bytes()
        except OSError:
            return {**common, "status": "ADAPTER_SCHEMA_MISMATCH"}
        if len(raw) > self.profile.max_response_bytes:
            return {**common, "status": "ADAPTER_SCHEMA_MISMATCH"}
        try:
            payload = load_json(raw)
        except (UnicodeDecodeError, ValueError):
            return {**common, "status": "ADAPTER_SCHEMA_MISMATCH"}
        if not isinstance(payload, Mapping):
            return {**common, "status": "ADAPTER_SCHEMA_MISMATCH"}
        text = payload.get("text")
        usage = payload.get("usage")
        if not isinstance(text, str) or not text.strip() or not isinstance(usage, Mapping):
            return {**common, "status": "ADAPTER_SCHEMA_MISMATCH"}
        if usage.get("hit_token_limit") is True:
            return {
                **common,
                "status": "GENERATION_LIMIT",
                "output_sha256": hashlib.sha256(raw).hexdigest(),
                "payload": dict(payload),
                "usage": dict(usage),
            }
        return {
            **common,
            "status": "COMPLETED",
            "output_sha256": hashlib.sha256(raw).hexdigest(),
            "payload": dict(payload),
            "usage": dict(usage),
        }

    @staticmethod
    def _sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()
