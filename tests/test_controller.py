from __future__ import annotations

import json
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import pytest

from rk.component_runtime import (
    ComponentRegistration,
    ComponentRuntime,
    RuntimeComponentKind,
    without_environment,
)
from rk.controller import (
    ControllerError,
    ControllerJournal,
    ControllerStatus,
    ResearchController,
)


class QueueIntentAdapter:
    name = "intent-fixture"
    version = "1"

    def __init__(self, results: list[Mapping[str, Any]]) -> None:
        self.results = list(results)
        self.requests: list[Mapping[str, Any]] = []

    def run(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        self.requests.append(dict(request))
        return self.results.pop(0)


class ToolAdapter:
    name = "tool-fixture"
    version = "1"

    def __init__(self) -> None:
        self.calls = 0

    def run(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        self.calls += 1
        return {
            "status": "COMPLETED",
            "payload": {"marker": request["query"]},
            "usage": {"input_tokens": 2, "output_tokens": 1, "total_tokens": 3},
        }


def intent(
    *, directives: list[dict[str, Any]] | None = None, text: str = "", status: str = "COMPLETED"
) -> Mapping[str, Any]:
    return {
        "status": status,
        "payload": None
        if status != "COMPLETED"
        else {
            "directives": directives or [],
            "text": text,
            "execution_claimed": False,
        },
        "usage": {"input_tokens": 5, "output_tokens": 2, "reasoning_tokens": 1, "total_tokens": 7},
    }


def make_controller(
    tmp_path: Path, results: list[Mapping[str, Any]], *, max_turns: int = 8
) -> tuple[ResearchController, QueueIntentAdapter, ToolAdapter]:
    tool = ToolAdapter()
    runtime = ComponentRuntime(
        [
            ComponentRegistration(
                component_id="leansearch",
                kind=RuntimeComponentKind.LEANSEARCH,
                adapter=tool,
                function_name="search_lean",
                description="Search Lean declarations",
                function_schema={
                    "type": "object",
                    "properties": {"query": {"type": "string"}},
                    "required": ["query"],
                    "additionalProperties": False,
                },
                request_builder=without_environment,
            )
        ]
    )
    model = QueueIntentAdapter(results)
    controller = ResearchController(
        ControllerJournal(tmp_path), model, runtime, max_turns=max_turns
    )
    return controller, model, tool


def test_complete_multistep_loop_persists_real_receipt_and_consumes_it_once(tmp_path: Path) -> None:
    controller, model, tool = make_controller(
        tmp_path,
        [
            intent(
                directives=[
                    {"call_id": "call-1", "name": "search_lean", "arguments": {"query": "add"}}
                ]
            ),
            intent(text="final theorem candidate"),
        ],
    )
    started = controller.start(
        prompt="solve", model="deepseek-v4-pro", max_output_tokens=1024
    )
    assert started.status is ControllerStatus.RUNNING

    waiting = controller.advance(environment={"DEEPSEEK_API_KEY": "secret"})
    assert waiting.status is ControllerStatus.WAITING_TOOL
    ready = controller.execute_pending(environment={})
    assert ready.status is ControllerStatus.RECEIPT_READY
    assert tool.calls == 1

    finished = controller.advance(environment={"DEEPSEEK_API_KEY": "secret"})
    assert finished.status is ControllerStatus.COMPLETED
    assert finished.final_text == "final theorem candidate"
    assert finished.usage["input_tokens"] == 12
    assert tool.calls == 1
    assert "TOOL_RECEIPT" in model.requests[1]["prompt"]
    state = json.loads((tmp_path / "controller-state.json").read_text(encoding="utf-8"))
    assert len(state["consumed_receipt_ids"]) == 1
    assert state["pending"] == []


def test_pause_and_resume_preserve_waiting_tool_state_across_instances(tmp_path: Path) -> None:
    controller, model, tool = make_controller(
        tmp_path,
        [intent(directives=[{"call_id": "c", "name": "search_lean", "arguments": {"query": "q"}}])],
    )
    controller.start(prompt="solve", model="m", max_output_tokens=10)
    controller.advance(environment={})
    assert controller.pause().status is ControllerStatus.PAUSED
    with pytest.raises(ControllerError, match="resume"):
        controller.advance(environment={})

    restored = ResearchController(controller.journal, model, controller.runtime)
    assert restored.resume().status is ControllerStatus.WAITING_TOOL
    assert restored.execute_pending(environment={}).status is ControllerStatus.RECEIPT_READY
    assert tool.calls == 1


def test_unknown_model_function_fails_without_tool_execution(tmp_path: Path) -> None:
    controller, _, tool = make_controller(
        tmp_path,
        [intent(directives=[{"call_id": "c", "name": "shell", "arguments": {}}])],
    )
    controller.start(prompt="solve", model="m", max_output_tokens=10)
    failed = controller.advance(environment={})
    assert failed.status is ControllerStatus.FAILED
    assert failed.failure == "INVALID_OR_DUPLICATE_DIRECTIVE"
    assert tool.calls == 0


def test_interrupted_executing_call_is_not_replayed(tmp_path: Path) -> None:
    controller, _, tool = make_controller(
        tmp_path,
        [intent(directives=[{"call_id": "c", "name": "search_lean", "arguments": {"query": "q"}}])],
    )
    controller.start(prompt="solve", model="m", max_output_tokens=10)
    controller.advance(environment={})
    state_path = tmp_path / "controller-state.json"
    state = json.loads(state_path.read_text(encoding="utf-8"))
    state["pending"][0]["execution_state"] = "EXECUTING"
    state_path.write_text(json.dumps(state), encoding="utf-8")

    failed = controller.execute_pending(environment={})
    assert failed.status is ControllerStatus.FAILED
    assert failed.failure == "TOOL_EXECUTION_OUTCOME_UNCERTAIN"
    assert tool.calls == 0


def test_turn_limit_and_adapter_failure_are_terminal_and_metered(tmp_path: Path) -> None:
    controller, _, _ = make_controller(tmp_path, [intent(status="FAILED")], max_turns=1)
    controller.start(prompt="solve", model="m", max_output_tokens=10)
    failed = controller.advance(environment={})
    assert failed.status is ControllerStatus.FAILED
    assert failed.failure == "INTENT_FAILED"
    assert failed.usage["input_tokens"] == 5


def test_receipt_consumption_is_committed_before_next_model_call(tmp_path: Path) -> None:
    class CrashingIntent(QueueIntentAdapter):
        def run(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
            if self.requests:
                raise ValueError("simulated model transport crash")
            return super().run(request)

    controller, _, tool = make_controller(
        tmp_path,
        [intent(directives=[{"call_id": "c", "name": "search_lean", "arguments": {"query": "q"}}])],
    )
    crashing = CrashingIntent([])
    crashing.requests = []
    controller.start(prompt="solve", model="m", max_output_tokens=10)
    controller.advance(environment={})
    controller.execute_pending(environment={})
    # A replacement instance simulates a process restart immediately before model continuation.
    crashing.requests.append({"prior": True})
    restarted = ResearchController(controller.journal, crashing, controller.runtime)
    failed = restarted.advance(environment={})
    assert failed.status is ControllerStatus.FAILED
    state = json.loads((tmp_path / "controller-state.json").read_text(encoding="utf-8"))
    assert len(state["consumed_receipt_ids"]) == 1
    assert len([x for x in state["transcript"] if x["kind"] == "TOOL_RECEIPT"]) == 1
    assert tool.calls == 1
