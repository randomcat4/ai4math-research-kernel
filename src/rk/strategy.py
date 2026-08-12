"""Internal strategy runner that normalizes calls across registered external adapters."""

from __future__ import annotations

import hashlib
import hmac
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
    host_receipt: Mapping[str, Any]

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
            "host_receipt": dict(self.host_receipt),
        }


class StrategyRunner:
    """One small seam for every external tool call; adapters remain internal and injectable."""

    def __init__(
        self,
        adapters: Mapping[str, ExecutionAdapter],
        *,
        receipt_signing_keys: Mapping[str, bytes] | None = None,
    ) -> None:
        self._adapters = dict(adapters)
        self._receipt_signing_keys = dict(receipt_signing_keys or {})

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
        *,
        receipt_context: Mapping[str, Any] | None = None,
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
        receipt: dict[str, Any] = {}
        key = self._receipt_signing_keys.get(adapter_name)
        if key is not None:
            if len(key) < 32:
                raise ValueError("host receipt signing keys must contain at least 32 bytes")
            required_context = {
                "run_id",
                "attempt_id",
                "binding_id",
                "environment_profile_id",
                "source_commit",
                "invocation_nonce",
            }
            if not isinstance(receipt_context, Mapping) or not required_context.issubset(
                receipt_context
            ):
                raise ValueError("signed host receipts require complete invocation context")
            payload = {
                "adapter_name": str(adapter.name),
                "adapter_version": str(adapter.version),
                **{name: receipt_context[name] for name in sorted(required_context)},
                "request_hash": request_hash,
                "result_hash": result_hash,
                "status": str(stable_result.get("status", "UNKNOWN")),
                "source_sha256": stable_result.get("source_sha256"),
                "output_sha256": stable_result.get("output_sha256"),
                "binary_sha256": stable_result.get("binary_sha256"),
                "exit_code": stable_result.get("exit_code"),
            }
            signature = hmac.new(
                key,
                canonical_json_sha256(payload).encode("ascii"),
                hashlib.sha256,
            ).hexdigest()
            receipt = {"payload": payload, "signature": signature}
        return ToolInvocation(
            adapter_name=str(adapter.name),
            adapter_version=str(adapter.version),
            request_hash=request_hash,
            result_hash=result_hash,
            status=str(stable_result.get("status", "UNKNOWN")),
            wall_time_ms=int(wall_time_ms),
            usage=normalized_usage,
            result=stable_result,
            host_receipt=receipt,
        )
