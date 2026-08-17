"""Resolve immutable schemas and migrations in source trees and installed wheels."""

from __future__ import annotations

import os
import sysconfig
from pathlib import Path


def resource_root() -> Path:
    override = os.environ.get("RK_RESOURCE_ROOT")
    if override:
        return Path(override).expanduser().resolve()
    source_root = Path(__file__).resolve().parents[2]
    if (source_root / "migrations").is_dir():
        return source_root
    installed = Path(sysconfig.get_path("data")) / "share" / "ai4math-research-kernel"
    if (installed / "migrations").is_dir():
        return installed
    raise RuntimeError("RK immutable resources are missing from this installation")


__all__ = ["resource_root"]
