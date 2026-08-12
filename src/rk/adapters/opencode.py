"""OpenCode JSONL model-execution adapter."""

from __future__ import annotations

import hashlib
import os
import queue
import signal
import subprocess
import threading
import time
from collections.abc import Mapping
from pathlib import Path
from typing import Any

from rk.adapters.base import (
    AdapterProfile,
    AdapterRequestError,
    ProcessResult,
    ProcessRunner,
    confined_path,
    load_json,
    require_exact_keys,
)


class OpenCodeJsonlRunner:
    """Stop a headless OpenCode process after its protocol says the turn is complete.

    Some OpenCode releases keep their background event loop alive after emitting the final
    ``step_finish`` event.  Waiting only for the OS process therefore deadlocks automation.
    This runner treats the JSONL completion event as authoritative for process lifecycle,
    gives OpenCode a short cleanup grace period, and then terminates the whole process group.
    """

    def __init__(
        self, *, exit_grace_seconds: float = 2.0, run_as_user: str | None = None
    ) -> None:
        self.exit_grace_seconds = exit_grace_seconds
        self.run_as_user = run_as_user

    @staticmethod
    def _stop(process: subprocess.Popen[str], *, force: bool) -> None:
        if process.poll() is not None:
            return
        if os.name == "posix":
            os.killpg(process.pid, signal.SIGKILL if force else signal.SIGTERM)
        elif force:
            process.kill()
        else:
            process.terminate()

    def run(
        self,
        argv: list[str] | tuple[str, ...],
        *,
        cwd: Path | None,
        env: Mapping[str, str],
        timeout: float,
    ) -> ProcessResult:
        command = tuple(str(item) for item in argv)
        identity: dict[str, Any] = {}
        if self.run_as_user is not None:
            if os.name != "posix":
                raise OSError("run_as_user requires POSIX")
            import pwd

            account = pwd.getpwnam(self.run_as_user)
            identity = {"user": account.pw_uid, "group": account.pw_gid, "extra_groups": []}
        process = subprocess.Popen(
            list(command),
            cwd=str(cwd) if cwd is not None else None,
            env=dict(env),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            encoding="utf-8",
            errors="strict",
            shell=False,
            start_new_session=True,
            bufsize=1,
            **identity,
        )
        assert process.stdout is not None
        assert process.stderr is not None
        events: queue.Queue[tuple[str, str | BaseException | None]] = queue.Queue()

        def read_stream(name: str, stream: Any) -> None:
            try:
                for line in stream:
                    events.put((name, line))
            except BaseException as exc:  # surfaced in the owning thread below
                events.put(("error", exc))
            finally:
                events.put((f"{name}_eof", None))

        threads = [
            threading.Thread(target=read_stream, args=("stdout", process.stdout), daemon=True),
            threading.Thread(target=read_stream, args=("stderr", process.stderr), daemon=True),
        ]
        for thread in threads:
            thread.start()

        stdout: list[str] = []
        stderr: list[str] = []
        protocol_completed = False
        forced_termination = False
        deadline = time.monotonic() + timeout
        completion_deadline: float | None = None
        reader_error: BaseException | None = None
        while True:
            now = time.monotonic()
            active_deadline = min(
                deadline,
                completion_deadline if completion_deadline is not None else deadline,
            )
            if process.poll() is not None and events.empty():
                break
            if now >= active_deadline:
                if protocol_completed:
                    forced_termination = process.poll() is None
                    self._stop(process, force=False)
                else:
                    self._stop(process, force=False)
                try:
                    process.wait(timeout=2)
                except subprocess.TimeoutExpired:
                    self._stop(process, force=True)
                    process.wait(timeout=2)
                break
            try:
                stream_name, item = events.get(timeout=min(0.1, active_deadline - now))
            except queue.Empty:
                continue
            if stream_name == "error":
                assert isinstance(item, BaseException)
                reader_error = item
                self._stop(process, force=True)
                process.wait(timeout=2)
                break
            if not isinstance(item, str):
                continue
            if stream_name == "stdout":
                stdout.append(item)
                try:
                    event = load_json(item)
                except (UnicodeDecodeError, ValueError):
                    event = None
                part = event.get("part") if isinstance(event, Mapping) else None
                if (
                    isinstance(event, Mapping)
                    and event.get("type") == "step_finish"
                    and isinstance(part, Mapping)
                    and part.get("reason") == "stop"
                ):
                    protocol_completed = True
                    completion_deadline = time.monotonic() + self.exit_grace_seconds
            elif stream_name == "stderr":
                stderr.append(item)

        for thread in threads:
            thread.join(timeout=1)
        if reader_error is not None:
            raise UnicodeError("OpenCode emitted invalid UTF-8") from reader_error
        returncode = process.poll()
        if returncode is None:
            self._stop(process, force=True)
            returncode = process.wait(timeout=2)
        if not protocol_completed and time.monotonic() >= deadline:
            returncode = 124
        while not events.empty():
            stream_name, item = events.get_nowait()
            if isinstance(item, str) and stream_name == "stdout":
                stdout.append(item)
            elif isinstance(item, str) and stream_name == "stderr":
                stderr.append(item)
        return ProcessResult(
            command,
            int(returncode),
            "".join(stdout),
            "".join(stderr),
            protocol_completed=protocol_completed,
            forced_termination=forced_termination,
        )


class OpenCodeAdapter:
    """Run one isolated model turn and normalize provider usage without granting truth."""

    trust_limit = "SOFT_MODEL"

    def __init__(
        self,
        profile: AdapterProfile,
        *,
        runner: ProcessRunner | None = None,
    ) -> None:
        profile.require("argv_prefix", "workspace_root")
        self.profile = profile
        self.runner = runner or OpenCodeJsonlRunner(run_as_user=profile.run_as_user)
        self.name = profile.name
        self.version = profile.version

    def run(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        require_exact_keys(
            request,
            required=frozenset({"prompt", "model", "workspace_relpath", "environment"}),
            label="OpenCode request",
        )
        prompt = request["prompt"]
        model = request["model"]
        environment = request["environment"]
        if not isinstance(prompt, str) or not prompt or len(prompt.encode()) > 64 * 1024:
            raise AdapterRequestError("prompt must be non-empty and at most 64 KiB")
        if not isinstance(model, str) or not model or model.startswith("-"):
            raise AdapterRequestError("model must be a non-option string")
        if not isinstance(environment, Mapping):
            raise AdapterRequestError("environment must be an object")
        workspace_root = self.profile.workspace_root
        assert workspace_root is not None
        workspace = confined_path(
            workspace_root, str(request["workspace_relpath"]), label="workspace_relpath"
        )
        workspace.mkdir(parents=True, exist_ok=True)
        env = self.profile.select_environment(environment)
        # OpenCode persists sessions and background state under XDG directories.  A fresh,
        # attempt-confined state tree prevents a crashed invocation from poisoning the next one.
        runtime_root = workspace / ".opencode-runtime"
        if runtime_root.exists():
            raise AdapterRequestError("OpenCode runtime directory must be fresh per attempt")
        for relative in ("home", "config", "data", "cache", "state"):
            (runtime_root / relative).mkdir(parents=True)
        env.update(
            {
                "HOME": str(runtime_root / "home"),
                "XDG_CONFIG_HOME": str(runtime_root / "config"),
                "XDG_DATA_HOME": str(runtime_root / "data"),
                "XDG_CACHE_HOME": str(runtime_root / "cache"),
                "XDG_STATE_HOME": str(runtime_root / "state"),
            }
        )
        config_sha256: str | None = None
        if self.profile.require_deny_all_tools:
            config_value = env.get("OPENCODE_CONFIG")
            if not config_value:
                raise AdapterRequestError("OPENCODE_CONFIG is required by the registered profile")
            config_path = Path(config_value)
            try:
                config_raw = config_path.read_bytes()
                config = load_json(config_raw)
            except (OSError, UnicodeDecodeError, ValueError) as exc:
                raise AdapterRequestError(
                    "registered OpenCode config is unreadable or invalid"
                ) from exc
            agent = config.get("agent") if isinstance(config, Mapping) else None
            build = agent.get("build") if isinstance(agent, Mapping) else None
            if (
                not isinstance(config, Mapping)
                or config.get("permission") != {"*": "deny"}
                or not isinstance(build, Mapping)
                or build.get("permission") != {"*": "deny"}
                or config.get("tools") != {"*": False}
                or build.get("tools") != {"*": False}
            ):
                raise AdapterRequestError("registered OpenCode config does not deny every tool")
            config_sha256 = hashlib.sha256(config_raw).hexdigest()
            runtime_config = runtime_root / "config" / "opencode-policy.json"
            runtime_config.write_bytes(config_raw)
            env["OPENCODE_CONFIG"] = str(runtime_config)
        if self.profile.run_as_user is not None:
            import pwd

            account = pwd.getpwnam(self.profile.run_as_user)
            for path in (workspace, *workspace.rglob("*")):
                os.chown(path, account.pw_uid, account.pw_gid)
        argv = [
            *self.profile.argv_prefix,
            "run",
            "--pure",
            "--format",
            "json",
            "--model",
            model,
            "--dir",
            str(workspace),
            prompt,
        ]
        completed = self.runner.run(
            argv,
            cwd=workspace,
            env=env,
            timeout=self.profile.timeout_seconds,
        )
        raw = completed.stdout.encode()
        common = {
            **self.profile.provenance(),
            "trust_limit": self.trust_limit,
            "evidence_type": "MODEL_JUDGE",
            "evidence_strength": "SOFT_MODEL",
            "machine_axis_effect": "UNCHANGED",
            "exit_code": completed.returncode,
            "protocol_completed": completed.protocol_completed,
            "forced_termination_after_protocol_completion": completed.forced_termination,
            "environment_names": sorted(env),
            "tool_policy": {
                "deny_all_required": self.profile.require_deny_all_tools,
                "config_sha256": config_sha256,
            },
            "transient_execution_output": {
                "stdout": completed.stdout,
                "stderr": completed.stderr,
            },
        }
        if len(raw) > self.profile.max_response_bytes:
            return {**common, "status": "ADAPTER_SCHEMA_MISMATCH", "payload": None}
        if completed.returncode != 0 and not completed.protocol_completed:
            return {
                **common,
                "status": "TIMEOUT" if completed.returncode == 124 else "FAILED",
                "payload": None,
                "usage": {"cost_unknown": True},
            }
        texts: list[str] = []
        usage = {
            "input_tokens": 0,
            "output_tokens": 0,
            "reasoning_tokens": 0,
            "cache_read_tokens": 0,
            "cache_write_tokens": 0,
            "total_tokens": 0,
        }
        tool_calls: list[str] = []
        finish_events = 0
        try:
            for line in completed.stdout.splitlines():
                event = load_json(line)
                if not isinstance(event, Mapping):
                    raise ValueError("OpenCode event must be an object")
                part = event.get("part")
                part = part if isinstance(part, Mapping) else {}
                if event.get("type") == "tool_use":
                    tool_calls.append(str(part.get("tool", "unknown")))
                if event.get("type") == "text" and isinstance(part.get("text"), str):
                    texts.append(part["text"])
                if event.get("type") == "step_finish":
                    finish_events += 1
                    tokens = part.get("tokens")
                    tokens = tokens if isinstance(tokens, Mapping) else {}
                    cache = tokens.get("cache")
                    cache = cache if isinstance(cache, Mapping) else {}
                    for target, source in (
                        ("input_tokens", "input"),
                        ("output_tokens", "output"),
                        ("reasoning_tokens", "reasoning"),
                        ("total_tokens", "total"),
                    ):
                        usage[target] += int(tokens.get(source, 0))
                    usage["cache_read_tokens"] += int(cache.get("read", 0))
                    usage["cache_write_tokens"] += int(cache.get("write", 0))
        except (UnicodeDecodeError, ValueError, TypeError):
            return {**common, "status": "ADAPTER_SCHEMA_MISMATCH", "payload": None}
        if len(tool_calls) > self.profile.max_tool_calls:
            return {
                **common,
                "status": "POLICY_VIOLATION",
                "payload": None,
                "usage": usage,
                "tool_calls": tool_calls,
            }
        text = "\n".join(texts).strip()
        if not text or finish_events == 0:
            return {
                **common,
                "status": "ADAPTER_SCHEMA_MISMATCH",
                "payload": None,
                "usage": usage,
                "tool_calls": tool_calls,
            }
        return {
            **common,
            "status": "COMPLETED",
            "payload": {"text": text},
            "usage": usage,
            "tool_calls": tool_calls,
        }
