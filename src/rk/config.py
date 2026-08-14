"""Deployment configuration with no business-rule dependency on one machine."""

from __future__ import annotations

import json
import os
import tomllib
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


def _as_path(value: str | os.PathLike[str], base: Path) -> Path:
    path = Path(value).expanduser()
    return path.resolve() if path.is_absolute() else (base / path).resolve()


def _positive_int(value: Any, label: str) -> int:
    result = int(value)
    if result <= 0:
        raise ValueError(f"{label} must be positive")
    return result


@dataclass(frozen=True, slots=True)
class KernelConfig:
    workspace_root: Path
    db_path: Path
    cas_root: Path
    schema_path: Path
    command_schema_path: Path
    receipt_schema_path: Path
    capability_key_path: Path | None = None
    capability_key_id: str | None = None
    inbox_roots: tuple[Path, ...] = ()
    busy_timeout_ms: int = 5_000
    max_artifact_bytes: int = 64 * 1024 * 1024
    orphan_grace_seconds: int = 86_400
    adapter_profiles: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    verifier_profiles: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    budget_policy: Mapping[str, Any] = field(default_factory=dict)
    product: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_mapping(
        cls,
        values: Mapping[str, Any],
        *,
        base: Path | None = None,
    ) -> KernelConfig:
        base_path = (base or Path.cwd()).resolve()
        workspace = _as_path(values.get("workspace_root", ".rk"), base_path)
        docs = _as_path(values.get("spec_root", "docs/spec"), base_path)
        key_value = values.get("capability_key_path")
        inbox = values.get("inbox_roots", [workspace / "inbox"])
        return cls(
            workspace_root=workspace,
            db_path=_as_path(values.get("db_path", workspace / "rk.sqlite"), base_path),
            cas_root=_as_path(values.get("cas_root", workspace / "cas"), base_path),
            schema_path=_as_path(values.get("schema_path", docs / "schema.sql"), base_path),
            command_schema_path=_as_path(
                values.get("command_schema_path", docs / "json/command.schema.json"), base_path
            ),
            receipt_schema_path=_as_path(
                values.get("receipt_schema_path", docs / "json/receipt.schema.json"), base_path
            ),
            capability_key_path=_as_path(key_value, base_path) if key_value else None,
            capability_key_id=(
                str(values["capability_key_id"])
                if values.get("capability_key_id") is not None
                else None
            ),
            inbox_roots=tuple(_as_path(item, base_path) for item in inbox),
            busy_timeout_ms=_positive_int(values.get("busy_timeout_ms", 5_000), "busy_timeout_ms"),
            max_artifact_bytes=_positive_int(
                values.get("max_artifact_bytes", 64 * 1024 * 1024), "max_artifact_bytes"
            ),
            orphan_grace_seconds=_positive_int(
                values.get("orphan_grace_seconds", 86_400), "orphan_grace_seconds"
            ),
            adapter_profiles=dict(values.get("adapter_profiles", {})),
            verifier_profiles=dict(values.get("verifier_profiles", {})),
            budget_policy=dict(values.get("budget_policy", {})),
            product=dict(values.get("product", {})),
        )

    @classmethod
    def load(
        cls,
        path: Path | None = None,
        *,
        environ: Mapping[str, str] | None = None,
        base: Path | None = None,
    ) -> KernelConfig:
        env = dict(environ or os.environ)
        values: dict[str, Any] = {}
        config_path = path or (Path(env["RK_CONFIG"]) if env.get("RK_CONFIG") else None)
        if config_path is not None:
            raw = config_path.read_bytes()
            if config_path.suffix.lower() == ".json":
                values.update(json.loads(raw.decode("utf-8")))
            else:
                values.update(tomllib.loads(raw.decode("utf-8")))
            base = base or config_path.resolve().parent

        env_map = {
            "RK_WORKSPACE_ROOT": "workspace_root",
            "RK_DB_PATH": "db_path",
            "RK_CAS_ROOT": "cas_root",
            "RK_SPEC_ROOT": "spec_root",
            "RK_SCHEMA_PATH": "schema_path",
            "RK_COMMAND_SCHEMA_PATH": "command_schema_path",
            "RK_RECEIPT_SCHEMA_PATH": "receipt_schema_path",
            "RK_CAPABILITY_KEY_PATH": "capability_key_path",
            "RK_CAPABILITY_KEY_ID": "capability_key_id",
            "RK_BUSY_TIMEOUT_MS": "busy_timeout_ms",
            "RK_MAX_ARTIFACT_BYTES": "max_artifact_bytes",
            "RK_ORPHAN_GRACE_SECONDS": "orphan_grace_seconds",
        }
        for env_name, key in env_map.items():
            if env_name in env:
                values[key] = env[env_name]
        if "RK_INBOX_ROOTS" in env:
            values["inbox_roots"] = [
                item for item in env["RK_INBOX_ROOTS"].split(os.pathsep) if item
            ]
        return cls.from_mapping(values, base=base)

    def prepare_local_directories(self) -> None:
        """Create only host-owned state directories, never external source paths."""

        self.workspace_root.mkdir(parents=True, exist_ok=True)
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.cas_root.mkdir(parents=True, exist_ok=True)
        for root in self.inbox_roots:
            root.mkdir(parents=True, exist_ok=True)
