"""Persistent host-managed controller loop over registered standard functions.

The model may propose function intent.  Only :class:`ComponentRuntime` executes it.  Pending calls
and receipts are journalled before state transitions so a crash cannot turn prose into execution or
consume one tool result twice.
"""

from __future__ import annotations

import importlib
import json
import os
import time
import uuid
from collections.abc import Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any, Protocol

from rk.adapters.base import AdapterRequestError, canonical_json_sha256
from rk.component_runtime import ComponentRuntime, ComponentRuntimeError


class IntentAdapter(Protocol):
    name: str
    version: str

    def run(self, request: Mapping[str, Any]) -> Mapping[str, Any]: ...


class ControllerStatus(StrEnum):
    RUNNING = "RUNNING"
    WAITING_TOOL = "WAITING_TOOL"
    RECEIPT_READY = "RECEIPT_READY"
    PAUSED = "PAUSED"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"


class ControllerError(RuntimeError):
    """The persisted loop was misused or its journal failed validation."""


@dataclass(frozen=True, slots=True)
class ControllerSnapshot:
    run_id: str
    status: ControllerStatus
    turn: int
    pending_count: int
    receipt_count: int
    final_text: str
    failure: str | None
    usage: Mapping[str, int]
    wall_time_ms: int

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "rk.controller-snapshot.v1",
            "run_id": self.run_id,
            "status": self.status.value,
            "turn": self.turn,
            "pending_count": self.pending_count,
            "receipt_count": self.receipt_count,
            "final_text": self.final_text,
            "failure": self.failure,
            "usage": dict(self.usage),
            "wall_time_ms": self.wall_time_ms,
        }


class ControllerJournal:
    """Atomic state plus append-only events inside one run workspace."""

    def __init__(self, workspace: Path) -> None:
        self.workspace = workspace.resolve()
        self.workspace.mkdir(parents=True, exist_ok=True)
        self.state_path = self.workspace / "controller-state.json"
        self.events_path = self.workspace / "controller-events.jsonl"
        self.lock_path = self.workspace / ".controller.lock"

    @contextmanager
    def locked(self) -> Any:
        descriptor = os.open(self.lock_path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            self._lock_descriptor(descriptor)
        except BaseException:
            os.close(descriptor)
            raise
        try:
            yield
        finally:
            self._unlock_descriptor(descriptor)
            os.close(descriptor)

    @staticmethod
    def _lock_descriptor(descriptor: int) -> None:
        try:
            if os.name == "nt":
                msvcrt: Any = importlib.import_module("msvcrt")

                if os.fstat(descriptor).st_size == 0:
                    os.write(descriptor, b"\0")
                os.lseek(descriptor, 0, os.SEEK_SET)
                msvcrt.locking(descriptor, msvcrt.LK_NBLCK, 1)
            else:
                fcntl: Any = importlib.import_module("fcntl")
                fcntl.flock(descriptor, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as error:
            raise ControllerError("controller workspace is locked by another process") from error

    @staticmethod
    def _unlock_descriptor(descriptor: int) -> None:
        if os.name == "nt":
            msvcrt: Any = importlib.import_module("msvcrt")

            os.lseek(descriptor, 0, os.SEEK_SET)
            msvcrt.locking(descriptor, msvcrt.LK_UNLCK, 1)
        else:
            fcntl: Any = importlib.import_module("fcntl")
            fcntl.flock(descriptor, fcntl.LOCK_UN)

    def load(self) -> dict[str, Any]:
        try:
            value = json.loads(self.state_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ControllerError("controller state is missing or invalid") from error
        if not isinstance(value, dict) or value.get("schema_version") != "rk.controller-state.v1":
            raise ControllerError("controller state has an unsupported schema")
        return value

    def commit(self, state: Mapping[str, Any], event: Mapping[str, Any]) -> None:
        event_line = json.dumps(event, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        with self.events_path.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(event_line + "\n")
            stream.flush()
            os.fsync(stream.fileno())
        temporary = self.state_path.with_suffix(".json.tmp")
        temporary.write_text(
            json.dumps(state, ensure_ascii=False, sort_keys=True, separators=(",", ":")),
            encoding="utf-8",
        )
        os.replace(temporary, self.state_path)


class ResearchController:
    """Advance a durable model/tool loop one crash-safe transition at a time."""

    def __init__(
        self,
        journal: ControllerJournal,
        intent_adapter: IntentAdapter,
        runtime: ComponentRuntime,
        *,
        max_turns: int = 24,
    ) -> None:
        if max_turns <= 0:
            raise ValueError("max_turns must be positive")
        self.journal = journal
        self.intent_adapter = intent_adapter
        self.runtime = runtime
        self.max_turns = max_turns

    def start(self, *, prompt: str, model: str, max_output_tokens: int) -> ControllerSnapshot:
        if not prompt or not model or not 1 <= max_output_tokens <= 32768:
            raise ControllerError("prompt, model, and max_output_tokens are required")
        with self.journal.locked():
            if self.journal.state_path.exists():
                raise ControllerError("controller workspace already contains a run")
            now = time.time_ns()
            state: dict[str, Any] = {
                "schema_version": "rk.controller-state.v1",
                "run_id": uuid.uuid4().hex,
                "status": ControllerStatus.RUNNING.value,
                "prompt": prompt,
                "model": model,
                "max_output_tokens": max_output_tokens,
                "turn": 0,
                "pending": [],
                "receipts": [],
                "consumed_receipt_ids": [],
                "transcript": [],
                "final_text": "",
                "failure": None,
                "usage": self._zero_usage(),
                "started_ns": now,
                "updated_ns": now,
            }
            self.journal.commit(state, self._event(state, "STARTED", {}))
            return self._snapshot(state)

    def advance(self, *, environment: Mapping[str, str]) -> ControllerSnapshot:
        """Consume ready receipts once, then ask the model for the next intent or final text."""

        with self.journal.locked():
            state = self.journal.load()
            status = ControllerStatus(state["status"])
            if status is ControllerStatus.PAUSED:
                raise ControllerError("resume the controller before advancing")
            if status in {ControllerStatus.COMPLETED, ControllerStatus.FAILED}:
                return self._snapshot(state)
            if status is ControllerStatus.WAITING_TOOL:
                raise ControllerError("execute pending calls before advancing")
            if status is ControllerStatus.RECEIPT_READY:
                self._consume_receipts(state)
                state["updated_ns"] = time.time_ns()
                self.journal.commit(
                    state,
                    self._event(
                        state,
                        "RECEIPTS_CONSUMED",
                        {"consumed_receipt_ids": state["consumed_receipt_ids"]},
                    ),
                )
            if state["turn"] >= self.max_turns:
                return self._fail(state, "TURN_LIMIT")
            request = {
                "prompt": self._render_prompt(state),
                "model": state["model"],
                "max_output_tokens": state["max_output_tokens"],
                "environment": dict(environment),
            }
            started = time.monotonic_ns()
            try:
                result = self.intent_adapter.run(request)
            except (AdapterRequestError, OSError, TimeoutError, ValueError, TypeError):
                return self._fail(state, "INTENT_ADAPTER_EXCEPTION")
            wall_time_ms = max(0, (time.monotonic_ns() - started) // 1_000_000)
            state["turn"] += 1
            self._add_usage(state, result.get("usage"), wall_time_ms)
            if result.get("status") != "COMPLETED" or not isinstance(
                result.get("payload"), Mapping
            ):
                return self._fail(state, f"INTENT_{result.get('status', 'INVALID')}")
            payload = result["payload"]
            if payload.get("execution_claimed") is not False:
                return self._fail(state, "MODEL_EXECUTION_CLAIM")
            directives = payload.get("directives")
            text = payload.get("text")
            if not isinstance(directives, list) or not isinstance(text, str):
                return self._fail(state, "INTENT_SCHEMA_MISMATCH")
            if directives:
                try:
                    pending = self._validate_pending(state, directives)
                except ControllerError:
                    return self._fail(state, "INVALID_OR_DUPLICATE_DIRECTIVE")
                if text.strip():
                    state["transcript"].append({"kind": "MODEL_NOTE", "text": text.strip()})
                state["pending"] = pending
                state["status"] = ControllerStatus.WAITING_TOOL.value
                state["updated_ns"] = time.time_ns()
                self.journal.commit(
                    state,
                    self._event(
                        state,
                        "DIRECTIVES_PERSISTED",
                        {"call_ids": [item["call_id"] for item in pending]},
                    ),
                )
                return self._snapshot(state)
            if not text.strip():
                return self._fail(state, "EMPTY_FINAL_RESPONSE")
            state["final_text"] = text.strip()
            state["status"] = ControllerStatus.COMPLETED.value
            state["updated_ns"] = time.time_ns()
            self.journal.commit(state, self._event(state, "COMPLETED", {}))
            return self._snapshot(state)

    def execute_pending(self, *, environment: Mapping[str, str]) -> ControllerSnapshot:
        """Execute persisted calls once and persist receipts before allowing model continuation."""

        with self.journal.locked():
            state = self.journal.load()
            if ControllerStatus(state["status"]) is not ControllerStatus.WAITING_TOOL:
                raise ControllerError("controller has no pending calls")
            pending = state.get("pending")
            if not isinstance(pending, list) or not pending:
                return self._fail(state, "PENDING_STATE_CORRUPT")
            receipts = list(state.get("receipts", []))
            for directive in pending:
                execution_state = directive.get("execution_state")
                if execution_state == "COMPLETED":
                    continue
                if execution_state == "EXECUTING":
                    return self._fail(state, "TOOL_EXECUTION_OUTCOME_UNCERTAIN")
                directive["execution_state"] = "EXECUTING"
                state["updated_ns"] = time.time_ns()
                self.journal.commit(
                    state,
                    self._event(
                        state,
                        "TOOL_EXECUTION_CLAIMED",
                        {"call_id": directive["call_id"], "function": directive["name"]},
                    ),
                )
                try:
                    receipt = self.runtime.execute_function(
                        call_id=directive["call_id"],
                        function_name=directive["name"],
                        arguments=directive["arguments"],
                        environment=environment,
                    )
                except ComponentRuntimeError:
                    return self._fail(state, "COMPONENT_RUNTIME_REJECTED")
                receipts.append(receipt.to_dict())
                self._add_usage(state, receipt.usage, 0)
                directive["execution_state"] = "COMPLETED"
                state["receipts"] = receipts
                state["updated_ns"] = time.time_ns()
                self.journal.commit(
                    state,
                    self._event(
                        state,
                        "TOOL_RECEIPT_PERSISTED",
                        {"receipt_id": receipt.receipt_id, "call_id": receipt.call_id},
                    ),
                )
            state["receipts"] = receipts
            state["status"] = ControllerStatus.RECEIPT_READY.value
            state["updated_ns"] = time.time_ns()
            self.journal.commit(
                state,
                self._event(
                    state,
                    "RECEIPTS_PERSISTED",
                    {"receipt_ids": [item["receipt_id"] for item in receipts]},
                ),
            )
            return self._snapshot(state)

    def pause(self) -> ControllerSnapshot:
        with self.journal.locked():
            state = self.journal.load()
            status = ControllerStatus(state["status"])
            if status in {ControllerStatus.COMPLETED, ControllerStatus.FAILED}:
                return self._snapshot(state)
            state["paused_from"] = status.value
            state["status"] = ControllerStatus.PAUSED.value
            state["updated_ns"] = time.time_ns()
            self.journal.commit(state, self._event(state, "PAUSED", {"from": status.value}))
            return self._snapshot(state)

    def resume(self) -> ControllerSnapshot:
        with self.journal.locked():
            state = self.journal.load()
            if ControllerStatus(state["status"]) is not ControllerStatus.PAUSED:
                raise ControllerError("controller is not paused")
            restored = ControllerStatus(state.pop("paused_from"))
            state["status"] = restored.value
            state["updated_ns"] = time.time_ns()
            self.journal.commit(state, self._event(state, "RESUMED", {"to": restored.value}))
            return self._snapshot(state)

    def inspect(self) -> ControllerSnapshot:
        with self.journal.locked():
            return self._snapshot(self.journal.load())

    def _consume_receipts(self, state: dict[str, Any]) -> None:
        consumed = set(state["consumed_receipt_ids"])
        receipts = state.get("receipts", [])
        pending_by_call = {item["call_id"]: item for item in state.get("pending", [])}
        if not receipts or len(receipts) != len(pending_by_call):
            raise ControllerError("receipt set does not match pending calls")
        for receipt in receipts:
            receipt_id = receipt.get("receipt_id")
            call_id = receipt.get("call_id")
            if receipt_id in consumed or call_id not in pending_by_call:
                raise ControllerError("receipt was already consumed or has no pending call")
            consumed.add(receipt_id)
            state["transcript"].append(
                {
                    "kind": "TOOL_RECEIPT",
                    "call_id": call_id,
                    "function": receipt.get("function_name"),
                    "status": receipt.get("status"),
                    "receipt_id": receipt_id,
                    "result": receipt.get("result"),
                }
            )
        state["consumed_receipt_ids"] = sorted(consumed)
        state["pending"] = []
        state["receipts"] = []
        state["status"] = ControllerStatus.RUNNING.value

    def _render_prompt(self, state: Mapping[str, Any]) -> str:
        transcript = state.get("transcript", [])
        return (
            "You are the RK research controller. Use only registered functions. Tool execution "
            "is performed by the host; never claim execution from prose. When the recorded "
            "receipts are sufficient, return the final answer without a function call.\n\n"
            f"Original task:\n{state['prompt']}\n\n"
            "Host transcript (canonical JSON):\n"
            + json.dumps(transcript, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
        )

    def _validate_pending(
        self, state: Mapping[str, Any], directives: list[Any]
    ) -> list[dict[str, Any]]:
        known = {
            item.get("call_id")
            for item in state.get("transcript", [])
            if isinstance(item, Mapping)
        }
        pending: list[dict[str, Any]] = []
        seen: set[str] = set()
        registered = self.runtime.function_definitions()
        for item in directives:
            if not isinstance(item, Mapping) or set(item) != {"call_id", "name", "arguments"}:
                raise ControllerError("directive shape is invalid")
            call_id, name, arguments = item["call_id"], item["name"], item["arguments"]
            if (
                not isinstance(call_id, str)
                or not call_id
                or call_id in seen
                or call_id in known
                or name not in registered
                or not isinstance(arguments, Mapping)
            ):
                raise ControllerError("directive identity or function is invalid")
            seen.add(call_id)
            pending.append(
                {
                    "call_id": call_id,
                    "name": name,
                    "arguments": dict(arguments),
                    "execution_state": "PENDING",
                }
            )
        return pending

    def _fail(self, state: dict[str, Any], reason: str) -> ControllerSnapshot:
        state["status"] = ControllerStatus.FAILED.value
        state["failure"] = reason
        state["updated_ns"] = time.time_ns()
        self.journal.commit(state, self._event(state, "FAILED", {"reason": reason}))
        return self._snapshot(state)

    @staticmethod
    def _zero_usage() -> dict[str, int]:
        return {
            "input_tokens": 0,
            "output_tokens": 0,
            "reasoning_tokens": 0,
            "total_tokens": 0,
            "wall_time_ms": 0,
        }

    def _add_usage(self, state: dict[str, Any], value: Any, wall_time_ms: int) -> None:
        usage = state["usage"]
        raw = value if isinstance(value, Mapping) else {}
        for key in ("input_tokens", "output_tokens", "reasoning_tokens", "total_tokens"):
            amount = raw.get(key, 0)
            if isinstance(amount, int) and not isinstance(amount, bool) and amount >= 0:
                usage[key] += amount
        usage["wall_time_ms"] += max(0, wall_time_ms)
        component_wall = raw.get("wall_time_ms", 0)
        if isinstance(component_wall, int) and not isinstance(component_wall, bool):
            usage["wall_time_ms"] += max(0, component_wall)

    @staticmethod
    def _event(state: Mapping[str, Any], kind: str, payload: Mapping[str, Any]) -> dict[str, Any]:
        return {
            "schema_version": "rk.controller-event.v1",
            "run_id": state["run_id"],
            "kind": kind,
            "turn": state["turn"],
            "timestamp_ns": time.time_ns(),
            "payload": dict(payload),
            "state_hash": canonical_json_sha256(state),
        }

    @staticmethod
    def _snapshot(state: Mapping[str, Any]) -> ControllerSnapshot:
        usage = state.get("usage")
        usage = usage if isinstance(usage, Mapping) else ResearchController._zero_usage()
        return ControllerSnapshot(
            run_id=str(state["run_id"]),
            status=ControllerStatus(state["status"]),
            turn=int(state["turn"]),
            pending_count=len(state.get("pending", [])),
            receipt_count=len(state.get("receipts", [])),
            final_text=str(state.get("final_text", "")),
            failure=str(state["failure"]) if state.get("failure") is not None else None,
            usage={key: int(value) for key, value in usage.items()},
            wall_time_ms=max(0, (int(state["updated_ns"]) - int(state["started_ns"])) // 1_000_000),
        )
