"""Shared internal seam for external execution adapters.

Deployment-specific values live in :class:`AdapterProfile`.  The adapters in this package
interpret tool output, but never promote a mathematical verdict.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType
from typing import Any, Protocol


class AdapterConfigurationError(ValueError):
    """A registered adapter profile is incomplete or unsafe."""


class AdapterRequestError(ValueError):
    """A caller supplied an invalid adapter request."""


class DuplicateJsonKey(ValueError):
    """JSON contained an ambiguous duplicate object key."""


def _pairs_without_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise DuplicateJsonKey(key)
        result[key] = value
    return result


def load_json(data: str | bytes) -> Any:
    """Decode strict UTF-8 JSON while rejecting duplicate object keys and extra data."""

    text = data.decode("utf-8", errors="strict") if isinstance(data, bytes) else data
    return json.loads(text, object_pairs_hook=_pairs_without_duplicates)


def canonical_json_sha256(value: Any) -> str:
    encoded = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        allow_nan=False,
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def require_exact_keys(
    value: Mapping[str, Any],
    *,
    required: frozenset[str],
    optional: frozenset[str] = frozenset(),
    label: str,
) -> None:
    keys = frozenset(value)
    missing = required - keys
    unknown = keys - required - optional
    if missing or unknown:
        details: list[str] = []
        if missing:
            details.append(f"missing={sorted(missing)!r}")
        if unknown:
            details.append(f"unknown={sorted(unknown)!r}")
        raise AdapterRequestError(f"{label} has invalid fields ({', '.join(details)})")


def confined_path(root: Path, relative: str, *, label: str) -> Path:
    """Resolve a caller-provided relative path without allowing root escape."""

    candidate = Path(relative)
    if candidate.is_absolute() or not relative or "\x00" in relative:
        raise AdapterRequestError(f"{label} must be a non-empty relative path")
    resolved_root = root.resolve()
    resolved = (resolved_root / candidate).resolve()
    try:
        resolved.relative_to(resolved_root)
    except ValueError as exc:
        raise AdapterRequestError(f"{label} escapes its registered root") from exc
    return resolved


@dataclass(frozen=True, slots=True)
class AdapterProfile:
    """Host-registered configuration consumed by every external adapter.

    The constructor intentionally has no machine-specific defaults.  Profiles can be loaded
    from ``KernelConfig.adapter_profiles`` with :meth:`from_mapping`; unknown fields fail closed.
    """

    name: str
    version: str
    source_commit: str
    timeout_seconds: float
    max_response_bytes: int
    env_whitelist: frozenset[str]
    argv_prefix: tuple[str, ...] = ()
    preflight_argv_prefix: tuple[str, ...] = ()
    repo_path: Path | None = None
    workspace_root: Path | None = None
    output_root: Path | None = None
    endpoint: str | None = None
    expected_toolchain: str | None = None
    binary_path: Path | None = None
    binary_sha256: str | None = None
    max_retries: int = 0
    retry_statuses: frozenset[int] = frozenset()
    backoff_seconds: float = 0.0
    allowed_axioms: tuple[str, ...] = ()
    max_tool_calls: int = 0
    require_deny_all_tools: bool = False
    run_as_user: str | None = None
    credential_env: str | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> AdapterProfile:
        allowed = {
            "name",
            "version",
            "source_commit",
            "timeout_seconds",
            "max_response_bytes",
            "env_whitelist",
            "argv_prefix",
            "preflight_argv_prefix",
            "repo_path",
            "workspace_root",
            "output_root",
            "endpoint",
            "expected_toolchain",
            "binary_path",
            "binary_sha256",
            "max_retries",
            "retry_statuses",
            "backoff_seconds",
            "allowed_axioms",
            "max_tool_calls",
            "require_deny_all_tools",
            "run_as_user",
            "credential_env",
        }
        required = {
            "name",
            "version",
            "source_commit",
            "timeout_seconds",
            "max_response_bytes",
            "env_whitelist",
        }
        missing = required - set(value)
        unknown = set(value) - allowed
        if missing or unknown:
            raise AdapterConfigurationError(
                f"invalid adapter profile fields: missing={sorted(missing)!r}, "
                f"unknown={sorted(unknown)!r}"
            )

        def optional_path(key: str) -> Path | None:
            raw = value.get(key)
            if raw is None:
                return None
            path = Path(str(raw)).expanduser()
            if not path.is_absolute():
                raise AdapterConfigurationError(f"{key} must be an absolute path")
            return path.resolve()

        argv_raw = value.get("argv_prefix", ())
        preflight_argv_raw = value.get("preflight_argv_prefix", ())
        env_raw = value["env_whitelist"]
        retry_raw = value.get("retry_statuses", ())
        axioms_raw = value.get("allowed_axioms", ())
        if not isinstance(argv_raw, Sequence) or isinstance(argv_raw, (str, bytes)):
            raise AdapterConfigurationError("argv_prefix must be a sequence of strings")
        if not isinstance(preflight_argv_raw, Sequence) or isinstance(
            preflight_argv_raw, (str, bytes)
        ):
            raise AdapterConfigurationError("preflight_argv_prefix must be a sequence of strings")
        if not isinstance(env_raw, Sequence) or isinstance(env_raw, (str, bytes)):
            raise AdapterConfigurationError("env_whitelist must be a sequence of names")
        if not isinstance(retry_raw, Sequence) or isinstance(retry_raw, (str, bytes)):
            raise AdapterConfigurationError("retry_statuses must be a sequence of integers")
        if not isinstance(axioms_raw, Sequence) or isinstance(axioms_raw, (str, bytes)):
            raise AdapterConfigurationError("allowed_axioms must be a sequence of strings")

        profile = cls(
            name=str(value["name"]),
            version=str(value["version"]),
            source_commit=str(value["source_commit"]),
            timeout_seconds=float(value["timeout_seconds"]),
            max_response_bytes=int(value["max_response_bytes"]),
            env_whitelist=frozenset(str(item) for item in env_raw),
            argv_prefix=tuple(str(item) for item in argv_raw),
            preflight_argv_prefix=tuple(str(item) for item in preflight_argv_raw),
            repo_path=optional_path("repo_path"),
            workspace_root=optional_path("workspace_root"),
            output_root=optional_path("output_root"),
            endpoint=str(value["endpoint"]) if value.get("endpoint") is not None else None,
            expected_toolchain=(
                str(value["expected_toolchain"])
                if value.get("expected_toolchain") is not None
                else None
            ),
            binary_path=optional_path("binary_path"),
            binary_sha256=(
                str(value["binary_sha256"]).lower()
                if value.get("binary_sha256") is not None
                else None
            ),
            max_retries=int(value.get("max_retries", 0)),
            retry_statuses=frozenset(int(item) for item in retry_raw),
            backoff_seconds=float(value.get("backoff_seconds", 0.0)),
            allowed_axioms=tuple(str(item) for item in axioms_raw),
            max_tool_calls=int(value.get("max_tool_calls", 0)),
            require_deny_all_tools=value.get("require_deny_all_tools", False),
            run_as_user=(str(value["run_as_user"]) if value.get("run_as_user") else None),
            credential_env=(
                str(value["credential_env"]) if value.get("credential_env") else None
            ),
        )
        profile._validate()
        return profile

    def _validate(self) -> None:
        if not self.name or not self.version or not self.source_commit:
            raise AdapterConfigurationError("name, version, and source_commit must be non-empty")
        commit_is_pinned = len(self.source_commit) in {40, 64} and not any(
            char not in "0123456789abcdef" for char in self.source_commit
        )
        if self.source_commit != "UNATTESTED" and not commit_is_pinned:
            raise AdapterConfigurationError("source_commit must be a full lowercase Git object ID")
        if self.timeout_seconds <= 0 or self.max_response_bytes <= 0:
            raise AdapterConfigurationError("timeout and response limit must be positive")
        if self.max_retries < 0 or self.backoff_seconds < 0 or self.max_tool_calls < 0:
            raise AdapterConfigurationError("retry values must be non-negative")
        if not isinstance(self.require_deny_all_tools, bool):
            raise AdapterConfigurationError("require_deny_all_tools must be boolean")
        if self.run_as_user is not None and (
            os.name != "posix"
            or not self.run_as_user
            or any(
                char not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_-"
                for char in self.run_as_user
            )
        ):
            raise AdapterConfigurationError("run_as_user must name a POSIX account")
        if any(not name or "=" in name or "\x00" in name for name in self.env_whitelist):
            raise AdapterConfigurationError("env_whitelist contains an invalid variable name")
        if self.credential_env is not None and self.credential_env not in self.env_whitelist:
            raise AdapterConfigurationError("credential_env must be in env_whitelist")
        if any(not item or "\x00" in item for item in self.argv_prefix):
            raise AdapterConfigurationError("argv_prefix contains an invalid argument")
        if any(not item or "\x00" in item for item in self.preflight_argv_prefix):
            raise AdapterConfigurationError("preflight_argv_prefix contains an invalid argument")
        if any(not item or "\x00" in item for item in self.allowed_axioms):
            raise AdapterConfigurationError("allowed_axioms contains an invalid name")
        forbidden_argument_fragments = ("--api-key=", "--token=", "--secret=")
        if any(
            fragment in item.lower()
            for item in self.argv_prefix
            for fragment in forbidden_argument_fragments
        ):
            raise AdapterConfigurationError("secrets must be supplied through whitelisted env")
        if self.endpoint is not None:
            parsed = urllib.parse.urlsplit(self.endpoint)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
                or parsed.query
                or parsed.fragment
            ):
                raise AdapterConfigurationError(
                    "endpoint must be an http(s) URL without credentials, query, or fragment"
                )
            if parsed.scheme != "https" and parsed.hostname not in {
                "localhost",
                "127.0.0.1",
                "::1",
            }:
                raise AdapterConfigurationError("non-local adapter endpoints must use https")
        if self.max_retries > 2 or not self.retry_statuses <= {429, 502, 503, 504}:
            raise AdapterConfigurationError("retry policy exceeds the v1 safe retry set")
        if self.binary_sha256 is not None and (
            len(self.binary_sha256) != 64
            or any(char not in "0123456789abcdef" for char in self.binary_sha256)
        ):
            raise AdapterConfigurationError("binary_sha256 must be lowercase hexadecimal")

    def require(self, *fields: str) -> None:
        absent = [field_name for field_name in fields if not getattr(self, field_name)]
        if absent:
            raise AdapterConfigurationError(
                f"profile {self.name!r} is missing required fields {absent!r}"
            )

    def select_environment(self, supplied: Mapping[str, Any] | None) -> dict[str, str]:
        values = supplied or {}
        unknown = set(values) - self.env_whitelist
        if unknown:
            raise AdapterRequestError(
                f"environment contains unregistered names: {sorted(unknown)!r}"
            )
        result: dict[str, str] = {}
        for key, value in values.items():
            rendered = str(value)
            if "\x00" in rendered:
                raise AdapterRequestError(f"environment variable {key!r} contains NUL")
            result[key] = rendered
        return result

    def select_credential(self, supplied: Mapping[str, Any] | None) -> str:
        selected = self.select_environment(supplied)
        name = self.credential_env
        if name is None:
            if len(self.env_whitelist) != 1:
                raise AdapterConfigurationError(
                    "network adapter profile must declare credential_env"
                )
            name = next(iter(self.env_whitelist))
        value = selected.get(name)
        if not value:
            raise AdapterRequestError(f"registered credential environment {name} is missing")
        return value

    def provenance(self) -> dict[str, Any]:
        """Return non-secret stable provenance; paths and environment values stay private."""

        return {
            "adapter_name": self.name,
            "adapter_version": self.version,
            "source_commit": self.source_commit,
            "endpoint": self.endpoint,
        }


@dataclass(frozen=True, slots=True)
class ProcessResult:
    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str
    protocol_completed: bool = False
    forced_termination: bool = False


class ProcessRunner(Protocol):
    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None,
        env: Mapping[str, str],
        timeout: float,
    ) -> ProcessResult: ...


@dataclass(slots=True)
class SafeSubprocessRunner:
    """Production subprocess adapter: argv only, no shell, no inherited environment."""

    def run(
        self,
        argv: Sequence[str],
        *,
        cwd: Path | None,
        env: Mapping[str, str],
        timeout: float,
    ) -> ProcessResult:
        command = tuple(str(item) for item in argv)
        completed = subprocess.run(
            list(command),
            cwd=str(cwd) if cwd is not None else None,
            env=dict(env),
            timeout=timeout,
            check=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="strict",
            shell=False,
        )
        return ProcessResult(command, completed.returncode, completed.stdout, completed.stderr)


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status_code: int
    body: bytes
    headers: Mapping[str, str] = field(default_factory=lambda: MappingProxyType({}))


class HttpClient(Protocol):
    def post_json(
        self,
        url: str,
        payload: Mapping[str, Any],
        *,
        timeout: float,
        max_response_bytes: int,
    ) -> HttpResponse: ...


@dataclass(slots=True)
class UrlLibHttpClient:
    """Small standard-library HTTP adapter used when a host does not inject another client."""

    user_agent: str = "ai4math-rk/1"

    def post_json(
        self,
        url: str,
        payload: Mapping[str, Any],
        *,
        timeout: float,
        max_response_bytes: int,
    ) -> HttpResponse:
        wire_payload = dict(payload)
        bearer = wire_payload.pop("_rk_authorization_bearer", None)
        body = json.dumps(
            wire_payload,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
            allow_nan=False,
        ).encode("utf-8")
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "User-Agent": self.user_agent,
        }
        if isinstance(bearer, str) and bearer:
            headers["Authorization"] = f"Bearer {bearer}"
        request = urllib.request.Request(
            url,
            data=body,
            headers=headers,
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout) as response:
                data = response.read(max_response_bytes + 1)
                if len(data) > max_response_bytes:
                    raise ValueError("HTTP response exceeds registered byte limit")
                return HttpResponse(
                    status_code=int(response.status),
                    body=data,
                    headers=MappingProxyType(dict(response.headers.items())),
                )
        except urllib.error.HTTPError as exc:
            data = exc.read(max_response_bytes + 1)
            if len(data) > max_response_bytes:
                data = data[:max_response_bytes]
            return HttpResponse(
                status_code=exc.code,
                body=data,
                headers=MappingProxyType(dict(exc.headers.items()) if exc.headers else {}),
            )


@dataclass(slots=True)
class CurlHttpClient:
    """curl-backed HTTP adapter for endpoints that reject Python's urllib TLS fingerprint."""

    executable: str = "curl"
    user_agent: str = "ai4math-rk/1"

    def post_json(
        self,
        url: str,
        payload: Mapping[str, Any],
        *,
        timeout: float,
        max_response_bytes: int,
    ) -> HttpResponse:
        body = json.dumps(
            payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
        )
        completed = subprocess.run(
            [
                self.executable,
                "--disable",
                "--silent",
                "--show-error",
                "--max-time",
                str(timeout),
                "--max-filesize",
                str(max_response_bytes),
                "--user-agent",
                self.user_agent,
                "--header",
                "Accept: application/json",
                "--header",
                "Content-Type: application/json",
                "--data-binary",
                "@-",
                "--write-out",
                "\n%{http_code}",
                url,
            ],
            input=body.encode(),
            capture_output=True,
            timeout=timeout + 1,
            check=False,
            shell=False,
            env={
                key: value
                for key, value in os.environ.items()
                if key in {"PATH", "SYSTEMROOT", "SSL_CERT_FILE", "SSL_CERT_DIR"}
            },
        )
        if completed.returncode != 0:
            raise OSError(completed.stderr.decode("utf-8", errors="replace"))
        response_body, separator, status = completed.stdout.rpartition(b"\n")
        if not separator or len(response_body) > max_response_bytes:
            raise ValueError("curl response is malformed or exceeds registered byte limit")
        return HttpResponse(status_code=int(status), body=response_body)


Sleeper = Callable[[float], None]
DEFAULT_SLEEPER: Sleeper = time.sleep
