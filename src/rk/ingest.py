"""Pre-CAS evidence inspection.

``EvidenceIngest`` is deliberately a pure receiving module: it reads a host-selected input,
checks that the bytes match the declaration and policy, and returns an immutable report.  It
does not write the CAS or database and has no operation capable of granting a verdict.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import stat
import zipfile
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from enum import StrEnum
from pathlib import Path, PurePosixPath
from types import MappingProxyType
from typing import Any

from jsonschema import Draft202012Validator

from rk.adapters.base import DuplicateJsonKey, load_json
from rk.domain import ArtifactInput

_ARTIFACT_NAME = re.compile(r"^[A-Za-z0-9_.]{1,64}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_HEADER = re.compile(r"^([A-Z][A-Z0-9_]*)\s*:\s*(.*?)\s*$")
_SECRET_PATTERNS: tuple[tuple[str, re.Pattern[bytes]], ...] = (
    ("API_KEY_SK", re.compile(rb"\bsk-(?:proj-|ant-)?[A-Za-z0-9_-]{16,}\b")),
    ("GITHUB_TOKEN", re.compile(rb"\bgh[pousr]_[A-Za-z0-9]{30,}\b")),
    ("AWS_ACCESS_KEY", re.compile(rb"\bAKIA[0-9A-Z]{16}\b")),
    ("GOOGLE_API_KEY", re.compile(rb"\bAIza[0-9A-Za-z_-]{35}\b")),
    (
        "PRIVATE_KEY",
        re.compile(rb"-----BEGIN (?:RSA |EC |OPENSSH )?PRIVATE KEY-----"),
    ),
)


class IngestDisposition(StrEnum):
    ACCEPT = "ACCEPT"
    REJECT = "REJECT"
    QUARANTINE = "QUARANTINE"


@dataclass(frozen=True, slots=True)
class IngestFinding:
    code: str
    path: str
    detail: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))

    def to_dict(self) -> dict[str, Any]:
        return {"code": self.code, "path": self.path, "detail": dict(self.detail)}


@dataclass(frozen=True, slots=True)
class IngestExpectation:
    """Per-artifact expectations supplied by the typed command handler."""

    scope: str | None = None
    status_values: frozenset[str] = frozenset()
    provenance: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))
    required_provenance_fields: frozenset[str] = frozenset()
    json_schema: Mapping[str, Any] | None = None

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> IngestExpectation:
        allowed = {
            "scope",
            "status_values",
            "provenance",
            "required_provenance_fields",
            "json_schema",
        }
        unknown = set(value) - allowed
        if unknown:
            raise ValueError(f"unknown ingest expectation fields: {sorted(unknown)!r}")
        statuses = value.get("status_values", ())
        required = value.get("required_provenance_fields", ())
        provenance = value.get("provenance", {})
        if not isinstance(statuses, Sequence) or isinstance(statuses, (str, bytes)):
            raise ValueError("status_values must be a sequence")
        if not isinstance(required, Sequence) or isinstance(required, (str, bytes)):
            raise ValueError("required_provenance_fields must be a sequence")
        if not isinstance(provenance, Mapping):
            raise ValueError("provenance must be an object")
        schema = value.get("json_schema")
        if schema is not None and not isinstance(schema, Mapping):
            raise ValueError("json_schema must be an object")
        return cls(
            scope=str(value["scope"]) if value.get("scope") is not None else None,
            status_values=frozenset(str(item) for item in statuses),
            provenance=MappingProxyType(dict(provenance)),
            required_provenance_fields=frozenset(str(item) for item in required),
            json_schema=MappingProxyType(dict(schema)) if schema is not None else None,
        )


@dataclass(frozen=True, slots=True)
class IngestPolicy:
    inbox_roots: tuple[Path, ...]
    max_artifact_bytes: int
    max_archive_expanded_bytes: int
    max_archive_files: int
    max_archive_ratio: float
    known_secret_values: tuple[bytes, ...] = ()

    def __post_init__(self) -> None:
        if not self.inbox_roots:
            raise ValueError("at least one inbox root is required")
        limits = (
            self.max_artifact_bytes,
            self.max_archive_expanded_bytes,
            self.max_archive_files,
        )
        if any(value <= 0 for value in limits) or self.max_archive_ratio <= 0:
            raise ValueError("ingest limits must be positive")
        for secret in self.known_secret_values:
            if len(secret) < 8:
                raise ValueError("known secret values shorter than eight bytes are unsafe to scan")

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> IngestPolicy:
        allowed = {
            "inbox_roots",
            "max_artifact_bytes",
            "max_archive_expanded_bytes",
            "max_archive_files",
            "max_archive_ratio",
            "known_secret_values",
        }
        required = allowed - {"known_secret_values"}
        missing = required - set(value)
        unknown = set(value) - allowed
        if missing or unknown:
            raise ValueError(
                f"invalid ingest policy fields: missing={sorted(missing)!r}, "
                f"unknown={sorted(unknown)!r}"
            )
        roots = value["inbox_roots"]
        secrets = value.get("known_secret_values", ())
        if not isinstance(roots, Sequence) or isinstance(roots, (str, bytes)):
            raise ValueError("inbox_roots must be a sequence")
        if not isinstance(secrets, Sequence) or isinstance(secrets, (str, bytes)):
            raise ValueError("known_secret_values must be a sequence")
        return cls(
            inbox_roots=tuple(Path(str(item)).expanduser().resolve() for item in roots),
            max_artifact_bytes=int(value["max_artifact_bytes"]),
            max_archive_expanded_bytes=int(value["max_archive_expanded_bytes"]),
            max_archive_files=int(value["max_archive_files"]),
            max_archive_ratio=float(value["max_archive_ratio"]),
            known_secret_values=tuple(
                item if isinstance(item, bytes) else str(item).encode("utf-8") for item in secrets
            ),
        )


@dataclass(frozen=True, slots=True)
class IngestResult:
    disposition: IngestDisposition
    logical_name: str
    sha256: str | None
    byte_count: int | None
    media_type: str
    status: str | None
    scope: str | None
    provenance: Mapping[str, Any]
    provenance_status: str
    findings: tuple[IngestFinding, ...]
    structured_data: Any = None

    @property
    def accepted(self) -> bool:
        return self.disposition is IngestDisposition.ACCEPT

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "rk.ingest_result.v1",
            "disposition": self.disposition.value,
            "accepted": self.accepted,
            "logical_name": self.logical_name,
            "sha256": self.sha256,
            "byte_count": self.byte_count,
            "media_type": self.media_type,
            "status": self.status,
            "scope": self.scope,
            "provenance": dict(self.provenance),
            "provenance_status": self.provenance_status,
            "findings": [finding.to_dict() for finding in self.findings],
            "structured_data": self.structured_data,
        }


@dataclass(slots=True)
class EvidenceIngest:
    policy: IngestPolicy

    def inspect(
        self,
        value: ArtifactInput,
        *,
        expectation: IngestExpectation | None = None,
    ) -> IngestResult:
        expected = expectation or IngestExpectation()
        findings: list[IngestFinding] = []
        self._validate_declaration(value, findings)
        source = self._resolve_source(value.path, findings)
        if source is None:
            return self._result(value, findings=findings)

        try:
            stat = source.stat()
        except OSError:
            findings.append(self._finding("ARTIFACT_MISSING", "artifact.path"))
            return self._result(value, findings=findings)
        if not source.is_file():
            findings.append(self._finding("ARTIFACT_NOT_REGULAR_FILE", "artifact.path"))
            return self._result(value, findings=findings)
        if stat.st_size > self.policy.max_artifact_bytes:
            findings.append(
                self._finding(
                    "ARTIFACT_TOO_LARGE",
                    "artifact.byte_count",
                    observed=stat.st_size,
                    limit=self.policy.max_artifact_bytes,
                )
            )
            return self._result(value, byte_count=stat.st_size, findings=findings)

        try:
            data = source.read_bytes()
        except OSError:
            findings.append(self._finding("ARTIFACT_READ_FAILED", "artifact.path"))
            return self._result(value, byte_count=stat.st_size, findings=findings)

        digest = hashlib.sha256(data).hexdigest()
        if len(data) != value.byte_count:
            findings.append(
                self._finding(
                    "BYTE_COUNT_MISMATCH",
                    "artifact.byte_count",
                    declared=value.byte_count,
                    observed=len(data),
                )
            )
        if digest != value.sha256:
            findings.append(self._finding("HASH_MISMATCH", "artifact.sha256"))

        self._scan_secrets(data, findings, path="artifact")
        if zipfile.is_zipfile(source):
            self._inspect_zip(source, findings)

        structured: Any = None
        status: str | None = None
        scope: str | None = None
        embedded_provenance: Mapping[str, Any] | None = None
        if self._is_json_media_type(value.media_type):
            try:
                structured = load_json(data)
            except (UnicodeDecodeError, json.JSONDecodeError, DuplicateJsonKey):
                findings.append(self._finding("INGEST_SCHEMA_INVALID", "artifact.content"))
            else:
                if expected.json_schema is not None:
                    self._validate_json_schema(structured, expected.json_schema, findings)
                if isinstance(structured, Mapping):
                    status = self._mapping_text(structured, "status", "verdict")
                    scope = self._mapping_text(structured, "scope", "target_scope")
                    candidate = structured.get("provenance")
                    if isinstance(candidate, Mapping):
                        embedded_provenance = candidate
        elif self._is_text_media_type(value.media_type):
            try:
                text = data.decode("utf-8", errors="strict")
            except UnicodeDecodeError:
                findings.append(self._finding("TEXT_NOT_UTF8", "artifact.content"))
            else:
                headers = self._headers_outside_fences(text)
                status_values = headers.get("STATUS", ())
                scope_values = headers.get("TARGET_SCOPE", headers.get("SCOPE", ()))
                probe_values = headers.get("PROBE_ID", ())
                mixed = (
                    len(status_values) > 1
                    or len(set(scope_values)) > 1
                    or len(set(probe_values)) > 1
                )
                if mixed:
                    findings.append(self._finding("MIXED_OUTPUT", "artifact.content"))
                status = status_values[0] if len(status_values) == 1 else None
                scope = scope_values[0] if len(set(scope_values)) == 1 and scope_values else None

        self._check_status(status, expected, findings)
        self._check_scope(scope, expected, findings)
        provenance_status = self._check_provenance(
            expected, embedded_provenance=embedded_provenance, findings=findings
        )
        return self._result(
            value,
            digest=digest,
            byte_count=len(data),
            status=status,
            scope=scope,
            provenance=expected.provenance,
            provenance_status=provenance_status,
            findings=findings,
            structured_data=structured,
        )

    def _validate_declaration(self, value: ArtifactInput, findings: list[IngestFinding]) -> None:
        if not _ARTIFACT_NAME.fullmatch(value.name):
            findings.append(self._finding("INGEST_SCHEMA_INVALID", "artifact.name"))
        if not _SHA256.fullmatch(value.sha256):
            findings.append(self._finding("INGEST_SCHEMA_INVALID", "artifact.sha256"))
        if value.byte_count < 0 or value.byte_count > self.policy.max_artifact_bytes:
            findings.append(self._finding("INGEST_SCHEMA_INVALID", "artifact.byte_count"))
        if not value.media_type or any(char in value.media_type for char in "\r\n\x00"):
            findings.append(self._finding("INGEST_SCHEMA_INVALID", "artifact.media_type"))

    def _resolve_source(self, raw_path: str, findings: list[IngestFinding]) -> Path | None:
        if not raw_path or "\x00" in raw_path:
            findings.append(self._finding("UNSAFE_PATH", "artifact.path"))
            return None
        normalized = raw_path.replace("/", "\\")
        if normalized.startswith(("\\\\", "\\?\\", "\\.\\")):
            findings.append(self._finding("UNSAFE_PATH", "artifact.path"))
            return None
        if ":" in raw_path[2:]:
            findings.append(self._finding("UNSAFE_PATH", "artifact.path"))
            return None
        source = Path(raw_path)
        if not source.is_absolute() or ".." in source.parts:
            findings.append(self._finding("UNSAFE_PATH", "artifact.path"))
            return None
        try:
            resolved = source.resolve(strict=True)
        except OSError:
            findings.append(self._finding("ARTIFACT_MISSING", "artifact.path"))
            return None
        registered_root: Path | None = None
        for root in self.policy.inbox_roots:
            resolved_root = root.resolve()
            try:
                resolved.relative_to(resolved_root)
            except ValueError:
                continue
            registered_root = resolved_root
            break
        if registered_root is None:
            findings.append(self._finding("PATH_OUTSIDE_INBOX", "artifact.path"))
            return None
        if self._contains_link_or_junction(source, registered_root):
            findings.append(self._finding("UNSAFE_PATH", "artifact.path"))
            return None
        return resolved

    @staticmethod
    def _contains_link_or_junction(source: Path, root: Path) -> bool:
        try:
            relative = source.absolute().relative_to(root.absolute())
        except ValueError:
            return True
        candidates = (
            root,
            *(root / Path(*relative.parts[:index]) for index in range(1, len(relative.parts) + 1)),
        )
        for current in candidates:
            try:
                if current.is_symlink():
                    return True
                is_junction = getattr(os.path, "isjunction", None)
                if is_junction is not None and bool(is_junction(current)):
                    return True
            except OSError:
                return True
        return False

    def _inspect_zip(self, source: Path, findings: list[IngestFinding]) -> None:
        expanded = 0
        compressed = 0
        try:
            with zipfile.ZipFile(source) as archive:
                entries = archive.infolist()
                if len(entries) > self.policy.max_archive_files:
                    findings.append(
                        self._finding(
                            "ARCHIVE_LIMIT_EXCEEDED",
                            "artifact.archive",
                            kind="file_count",
                            limit=self.policy.max_archive_files,
                        )
                    )
                    return
                for entry in entries:
                    member = PurePosixPath(entry.filename.replace("\\", "/"))
                    member_mode = entry.external_attr >> 16
                    unsafe_member = (
                        member.is_absolute()
                        or ".." in member.parts
                        or ":" in entry.filename
                        or bool(entry.flag_bits & 0x1)
                        or stat.S_ISLNK(member_mode)
                    )
                    if unsafe_member:
                        findings.append(self._finding("UNSAFE_ARCHIVE_MEMBER", "artifact.archive"))
                        return
                    expanded += entry.file_size
                    compressed += entry.compress_size
                    if expanded > self.policy.max_archive_expanded_bytes:
                        findings.append(
                            self._finding(
                                "ARCHIVE_LIMIT_EXCEEDED",
                                "artifact.archive",
                                kind="expanded_bytes",
                                limit=self.policy.max_archive_expanded_bytes,
                            )
                        )
                        return
                ratio = expanded / max(compressed, 1)
                if ratio > self.policy.max_archive_ratio:
                    findings.append(
                        self._finding(
                            "ARCHIVE_LIMIT_EXCEEDED",
                            "artifact.archive",
                            kind="compression_ratio",
                        )
                    )
                    return
                for entry in entries:
                    if entry.is_dir():
                        continue
                    with archive.open(entry) as stream:
                        member_data = stream.read(self.policy.max_archive_expanded_bytes + 1)
                    self._scan_secrets(member_data, findings, path="artifact.archive")
        except (OSError, zipfile.BadZipFile, RuntimeError):
            findings.append(self._finding("ARCHIVE_INVALID", "artifact.archive"))

    def _scan_secrets(self, data: bytes, findings: list[IngestFinding], *, path: str) -> None:
        found: set[str] = set()
        for pattern_id, pattern in _SECRET_PATTERNS:
            if pattern.search(data):
                found.add(pattern_id)
        for index, secret in enumerate(self.policy.known_secret_values):
            if secret in data:
                found.add(f"KNOWN_SECRET_{index}")
        for pattern_id in sorted(found):
            findings.append(self._finding("SECRET_QUARANTINED", path, pattern_id=pattern_id))

    @staticmethod
    def _headers_outside_fences(text: str) -> dict[str, tuple[str, ...]]:
        values: dict[str, list[str]] = {}
        fence: str | None = None
        for line in text.splitlines():
            stripped = line.strip()
            marker = stripped[:3]
            if marker in {"```", "~~~"}:
                fence = None if fence == marker else marker
                continue
            if fence is not None:
                continue
            match = _HEADER.fullmatch(stripped)
            if match:
                values.setdefault(match.group(1), []).append(match.group(2))
        return {key: tuple(items) for key, items in values.items()}

    @staticmethod
    def _mapping_text(value: Mapping[str, Any], *keys: str) -> str | None:
        for key in keys:
            candidate = value.get(key)
            if isinstance(candidate, str):
                return candidate
        return None

    def _validate_json_schema(
        self,
        structured: Any,
        schema: Mapping[str, Any],
        findings: list[IngestFinding],
    ) -> None:
        try:
            validator = Draft202012Validator(dict(schema))
            errors = sorted(validator.iter_errors(structured), key=lambda error: list(error.path))
        except Exception:  # schema itself is host configuration, but still fail closed
            findings.append(self._finding("INGEST_SCHEMA_INVALID", "expectation.json_schema"))
            return
        for error in errors:
            pointer = "/".join(str(item) for item in error.absolute_path)
            findings.append(self._finding("INGEST_SCHEMA_INVALID", f"artifact.content/{pointer}"))

    def _check_status(
        self,
        status: str | None,
        expected: IngestExpectation,
        findings: list[IngestFinding],
    ) -> None:
        if expected.status_values and status not in expected.status_values:
            findings.append(
                self._finding(
                    "STATUS_HEADER_MISMATCH",
                    "artifact.content.status",
                    observed=status,
                    allowed=sorted(expected.status_values),
                )
            )

    def _check_scope(
        self,
        scope: str | None,
        expected: IngestExpectation,
        findings: list[IngestFinding],
    ) -> None:
        if expected.scope is not None and scope != expected.scope:
            findings.append(
                self._finding(
                    "EVIDENCE_SCOPE_MISMATCH",
                    "artifact.content.scope",
                    observed=scope,
                    expected=expected.scope,
                )
            )

    def _check_provenance(
        self,
        expected: IngestExpectation,
        *,
        embedded_provenance: Mapping[str, Any] | None,
        findings: list[IngestFinding],
    ) -> str:
        missing = expected.required_provenance_fields - set(expected.provenance)
        if missing:
            findings.append(
                self._finding(
                    "PROVENANCE_INCOMPLETE",
                    "expectation.provenance",
                    missing=sorted(missing),
                )
            )
        try:
            encoded = json.dumps(
                dict(expected.provenance),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
                allow_nan=False,
            ).encode("utf-8")
        except (TypeError, ValueError):
            findings.append(self._finding("PROVENANCE_INVALID", "expectation.provenance"))
            return "INVALID"
        self._scan_secrets(encoded, findings, path="expectation.provenance")
        if embedded_provenance is None:
            return "DECLARED"
        for key, expected_value in expected.provenance.items():
            if embedded_provenance.get(key) != expected_value:
                findings.append(
                    self._finding(
                        "PROVENANCE_MISMATCH",
                        f"artifact.content.provenance.{key}",
                    )
                )
        return (
            "EMBEDDED_MATCH"
            if not any(item.code == "PROVENANCE_MISMATCH" for item in findings)
            else "MISMATCH"
        )

    @staticmethod
    def _is_json_media_type(media_type: str) -> bool:
        normalized = media_type.split(";", 1)[0].strip().lower()
        return normalized == "application/json" or normalized.endswith("+json")

    @staticmethod
    def _is_text_media_type(media_type: str) -> bool:
        normalized = media_type.split(";", 1)[0].strip().lower()
        return normalized.startswith("text/") or normalized in {
            "application/markdown",
            "application/x-lean",
        }

    @staticmethod
    def _finding(code: str, path: str, **detail: Any) -> IngestFinding:
        return IngestFinding(code=code, path=path, detail=MappingProxyType(detail))

    @staticmethod
    def _result(
        value: ArtifactInput,
        *,
        digest: str | None = None,
        byte_count: int | None = None,
        status: str | None = None,
        scope: str | None = None,
        provenance: Mapping[str, Any] | None = None,
        provenance_status: str = "NOT_CHECKED",
        findings: list[IngestFinding],
        structured_data: Any = None,
    ) -> IngestResult:
        quarantine = any(item.code == "SECRET_QUARANTINED" for item in findings)
        disposition = (
            IngestDisposition.QUARANTINE
            if quarantine
            else IngestDisposition.REJECT
            if findings
            else IngestDisposition.ACCEPT
        )
        return IngestResult(
            disposition=disposition,
            logical_name=value.name,
            sha256=digest,
            byte_count=byte_count,
            media_type=value.media_type,
            status=status,
            scope=scope,
            provenance=MappingProxyType(dict(provenance or {})),
            provenance_status=provenance_status,
            findings=tuple(findings),
            structured_data=structured_data,
        )
