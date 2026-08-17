"""Internal strategy runner that normalizes calls across registered external adapters."""

from __future__ import annotations

import subprocess
import time
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from rk.adapters.base import canonical_json_sha256
from rk.ports import ExecutionAdapter


@dataclass(frozen=True, slots=True)
class ToolInvocation:
    adapter_name: str
    adapter_version: str
    request_hash: str
    result_hash: str
    status: str
    wall_time_ms: int
    usage: Mapping[str, Any]
    result: Mapping[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "adapter_name": self.adapter_name,
            "adapter_version": self.adapter_version,
            "request_hash": self.request_hash,
            "result_hash": self.result_hash,
            "status": self.status,
            "wall_time_ms": self.wall_time_ms,
            "usage": dict(self.usage),
            "result": dict(self.result),
        }


class StrategyRunner:
    """One small seam for every external tool call; adapters remain internal and injectable."""

    def __init__(
        self,
        adapters: Mapping[str, ExecutionAdapter],
    ) -> None:
        self._adapters = dict(adapters)

    def adapter_identity(self, adapter_name: str) -> tuple[str, str]:
        try:
            adapter = self._adapters[adapter_name]
        except KeyError as exc:
            raise ValueError(f"adapter is not registered: {adapter_name}") from exc
        return str(adapter.name), str(adapter.version)

    def invoke(
        self,
        adapter_name: str,
        request: Mapping[str, Any],
    ) -> ToolInvocation:
        self.adapter_identity(adapter_name)
        adapter = self._adapters[adapter_name]
        started = time.monotonic_ns()
        try:
            result = adapter.run(request)
        except subprocess.TimeoutExpired as exc:
            result = {
                "status": "TIMEOUT",
                "error_type": type(exc).__name__,
                "timeout_seconds": exc.timeout,
                "usage": {"cost_unknown": True},
            }
        except (OSError, UnicodeError) as exc:
            result = {
                "status": "EXECUTION_ERROR",
                "error_type": type(exc).__name__,
                "usage": {"cost_unknown": True},
            }
        wall_time_ms = (time.monotonic_ns() - started) // 1_000_000
        stable_result = dict(result)
        usage = stable_result.get("usage")
        normalized_usage = dict(usage) if isinstance(usage, Mapping) else {}
        normalized_usage["wall_time_ms"] = int(wall_time_ms)
        request_hash = canonical_json_sha256(request)
        result_hash = canonical_json_sha256(stable_result)
        return ToolInvocation(
            adapter_name=str(adapter.name),
            adapter_version=str(adapter.version),
            request_hash=request_hash,
            result_hash=result_hash,
            status=str(stable_result.get("status", "UNKNOWN")),
            wall_time_ms=int(wall_time_ms),
            usage=normalized_usage,
            result=stable_result,
        )
