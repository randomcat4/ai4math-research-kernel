from __future__ import annotations

import subprocess
from collections.abc import Mapping
from typing import Any

import pytest

from rk.strategy import StrategyRunner


class FixtureAdapter:
    name = "fixture"
    version = "v1"

    def run(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        return {"status": "COMPLETED", "echo": dict(request), "usage": {"input_tokens": 2}}


def test_strategy_runner_records_hashes_status_usage_and_time() -> None:
    invocation = StrategyRunner({"fixture": FixtureAdapter()}).invoke("fixture", {"x": 1})
    assert invocation.adapter_name == "fixture"
    assert invocation.status == "COMPLETED"
    assert invocation.usage["input_tokens"] == 2
    assert invocation.usage["wall_time_ms"] >= 0
    assert len(invocation.request_hash) == 64
    assert len(invocation.result_hash) == 64


def test_strategy_runner_fails_closed_for_unregistered_adapter() -> None:
    with pytest.raises(ValueError, match="not registered"):
        StrategyRunner({}).invoke("missing", {})


class TimeoutAdapter:
    name = "timeout"
    version = "v1"

    def run(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        del request
        raise subprocess.TimeoutExpired(["tool"], 2)


def test_strategy_runner_normalizes_timeout_for_recording_and_cleanup() -> None:
    invocation = StrategyRunner({"timeout": TimeoutAdapter()}).invoke("timeout", {})

    assert invocation.status == "TIMEOUT"
    assert invocation.usage["cost_unknown"] is True
    assert invocation.result["error_type"] == "TimeoutExpired"


def test_strategy_runner_exposes_no_receipt_signing_interface() -> None:
    with pytest.raises(TypeError):
        StrategyRunner({"fixture": FixtureAdapter()}, receipt_signing_keys={"fixture": b"k" * 32})
