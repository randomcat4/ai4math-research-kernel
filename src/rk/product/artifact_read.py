"""Range-safe reads of immutable artifacts from the canonical CAS."""

from __future__ import annotations

import os
import re
from collections.abc import Iterator, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from urllib.parse import quote

_CAS_RELPATH = re.compile(r"^[0-9a-f]{2}/[0-9a-f]{2}/[0-9a-f]{64}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")
_RANGE = re.compile(r"^bytes=(\d*)-(\d*)$")


class ArtifactReadError(RuntimeError):
    """An artifact cannot be described or read through the public byte seam."""


class ArtifactNotFound(ArtifactReadError):
    """The canonical registry has no artifact with this identity."""


class ArtifactNotCommitted(ArtifactReadError):
    """Only committed immutable artifacts are readable."""


class ArtifactBindingMismatch(ArtifactReadError):
    """The caller's exact ArtifactRef does not match canonical metadata."""


class InvalidByteRange(ArtifactReadError):
    """A Range header is malformed, multipart, or outside the artifact."""


class ArtifactMetadataSource(Protocol):
    def get_artifact(self, artifact_id: str) -> Mapping[str, Any] | None: ...


@dataclass(frozen=True, slots=True)
class ExactArtifactRef:
    artifact_id: str
    sha256: str
    byte_count: int
    media_type: str
    at_revision: int = 0


@dataclass(frozen=True, slots=True)
class ViewerMetadata:
    viewer: str
    textual: bool
    syntax: str | None


@dataclass(frozen=True, slots=True)
class ArtifactDescriptor:
    ref: ExactArtifactRef
    logical_name: str
    line_count: int | None
    viewer: ViewerMetadata
    content_disposition: str


@dataclass(frozen=True, slots=True)
class ResolvedByteRange:
    start: int
    end_exclusive: int
    total: int

    @property
    def byte_count(self) -> int:
        return self.end_exclusive - self.start

    @property
    def content_range(self) -> str:
        return f"bytes {self.start}-{self.end_exclusive - 1}/{self.total}"


class ArtifactByteStream:
    """A reopenable bounded iterator; no request buffers the full artifact."""

    def __init__(self, path: Path, byte_range: ResolvedByteRange, chunk_bytes: int) -> None:
        self._path = path
        self.byte_range = byte_range
        self._chunk_bytes = chunk_bytes

    def __iter__(self) -> Iterator[bytes]:
        remaining = self.byte_range.byte_count
        with self._path.open("rb", buffering=0) as reader:
            reader.seek(self.byte_range.start)
            while remaining:
                chunk = reader.read(min(remaining, self._chunk_bytes))
                if not chunk:
                    raise ArtifactReadError("CAS object ended before its registered byte count")
                remaining -= len(chunk)
                yield chunk


@dataclass(frozen=True, slots=True)
class ArtifactRangeResult:
    descriptor: ArtifactDescriptor
    byte_range: ResolvedByteRange
    stream: ArtifactByteStream
    partial: bool

    @property
    def headers(self) -> Mapping[str, str]:
        headers = {
            "accept-ranges": "bytes",
            "content-type": self.descriptor.ref.media_type,
            "content-length": str(self.byte_range.byte_count),
            "content-disposition": self.descriptor.content_disposition,
            "etag": f'"sha256:{self.descriptor.ref.sha256}"',
            "x-rk-artifact-id": self.descriptor.ref.artifact_id,
        }
        if self.partial:
            headers["content-range"] = self.byte_range.content_range
        return headers


class ArtifactReadService:
    """Resolve registry metadata once, then stream the named immutable CAS object."""

    def __init__(
        self,
        *,
        metadata: ArtifactMetadataSource,
        cas_root: Path,
        stream_chunk_bytes: int = 1024 * 1024,
    ) -> None:
        if stream_chunk_bytes <= 0:
            raise ValueError("stream_chunk_bytes must be positive")
        self._metadata = metadata
        self._cas_root = Path(cas_root).resolve()
        self._chunk_bytes = stream_chunk_bytes

    def describe(
        self, artifact_id: str, *, expected_ref: ExactArtifactRef | None = None
    ) -> ArtifactDescriptor:
        row = self._metadata.get_artifact(artifact_id)
        if row is None:
            raise ArtifactNotFound(artifact_id)
        if row.get("ingest_state") != "COMMITTED":
            raise ArtifactNotCommitted(artifact_id)
        descriptor = _descriptor(row)
        if expected_ref is not None and descriptor.ref != expected_ref:
            raise ArtifactBindingMismatch("requested ArtifactRef does not match canonical metadata")
        self._artifact_path(row, descriptor.ref)
        return descriptor

    def open_range(
        self,
        artifact_id: str,
        *,
        range_header: str | None = None,
        expected_ref: ExactArtifactRef | None = None,
    ) -> ArtifactRangeResult:
        row = self._metadata.get_artifact(artifact_id)
        if row is None:
            raise ArtifactNotFound(artifact_id)
        if row.get("ingest_state") != "COMMITTED":
            raise ArtifactNotCommitted(artifact_id)
        descriptor = _descriptor(row)
        if expected_ref is not None and descriptor.ref != expected_ref:
            raise ArtifactBindingMismatch("requested ArtifactRef does not match canonical metadata")
        path = self._artifact_path(row, descriptor.ref)
        byte_range = resolve_range(range_header, descriptor.ref.byte_count)
        return ArtifactRangeResult(
            descriptor=descriptor,
            byte_range=byte_range,
            stream=ArtifactByteStream(path, byte_range, self._chunk_bytes),
            partial=range_header is not None,
        )

    def _artifact_path(self, row: Mapping[str, Any], ref: ExactArtifactRef) -> Path:
        relpath = str(row.get("cas_relpath", ""))
        if (
            not _CAS_RELPATH.fullmatch(relpath)
            or relpath != f"{ref.sha256[:2]}/{ref.sha256[2:4]}/{ref.sha256}"
        ):
            raise ArtifactReadError("canonical artifact path does not match its digest")
        candidate = self._cas_root / relpath
        if any(
            item.is_symlink() for item in (candidate.parent.parent, candidate.parent, candidate)
        ):
            raise ArtifactReadError("canonical artifact path contains a symbolic link")
        path = candidate.resolve(strict=True)
        if not path.is_relative_to(self._cas_root) or not path.is_file():
            raise ArtifactReadError("canonical artifact path is not a regular CAS object")
        if os.stat(path).st_size != ref.byte_count:
            raise ArtifactReadError("canonical artifact length does not match registry metadata")
        return path


def resolve_range(header: str | None, total: int) -> ResolvedByteRange:
    if total < 0:
        raise ValueError("total byte count cannot be negative")
    if header is None:
        return ResolvedByteRange(0, total, total)
    match = _RANGE.fullmatch(header)
    if match is None or "," in header:
        raise InvalidByteRange("only one RFC 9110 byte range is accepted")
    first, last = match.groups()
    if not first and not last:
        raise InvalidByteRange("byte range has no boundary")
    if total == 0:
        raise InvalidByteRange("empty artifacts have no satisfiable byte range")
    if first:
        start = int(first)
        end_exclusive = min(int(last) + 1, total) if last else total
        if start >= total or end_exclusive <= start:
            raise InvalidByteRange("byte range is outside the artifact")
        return ResolvedByteRange(start, end_exclusive, total)
    suffix = int(last)
    if suffix <= 0:
        raise InvalidByteRange("suffix byte range must be positive")
    return ResolvedByteRange(max(total - suffix, 0), total, total)


def _descriptor(row: Mapping[str, Any]) -> ArtifactDescriptor:
    artifact_id = str(row.get("artifact_id", ""))
    sha256 = str(row.get("sha256", ""))
    media_type = str(row.get("media_type", ""))
    byte_count = row.get("byte_count")
    if (
        not artifact_id
        or not _SHA256.fullmatch(sha256)
        or not isinstance(byte_count, int)
        or isinstance(byte_count, bool)
        or byte_count < 0
        or not media_type
    ):
        raise ArtifactReadError("canonical artifact metadata is invalid")
    source_name = row.get("source_name")
    logical_name = str(source_name) if source_name else artifact_id
    line_count_value = row.get("line_count")
    line_count = int(line_count_value) if line_count_value is not None else None
    ref = ExactArtifactRef(artifact_id, sha256, byte_count, media_type)
    return ArtifactDescriptor(
        ref=ref,
        logical_name=logical_name,
        line_count=line_count,
        viewer=_viewer(media_type, logical_name),
        content_disposition=_content_disposition(logical_name),
    )


def _viewer(media_type: str, logical_name: str) -> ViewerMetadata:
    normalized = media_type.partition(";")[0].strip().lower()
    suffix = Path(logical_name).suffix.lower()
    if normalized == "application/pdf":
        return ViewerMetadata("PDF", False, None)
    if normalized.startswith("image/"):
        return ViewerMetadata("IMAGE", False, None)
    if normalized in {"application/json", "application/ld+json"}:
        return ViewerMetadata("JSON", True, "json")
    if normalized in {"application/x-tex", "text/x-tex"} or suffix in {".tex", ".sty"}:
        return ViewerMetadata("TEX", True, "latex")
    if normalized in {"application/x-lean", "text/x-lean"} or suffix == ".lean":
        return ViewerMetadata("LEAN", True, "lean")
    if normalized.startswith("text/"):
        return ViewerMetadata("TEXT", True, None)
    return ViewerMetadata("BINARY", False, None)


def _content_disposition(logical_name: str) -> str:
    ascii_name = "".join(
        character if 0x20 <= ord(character) < 0x7F and character not in {'"', "\\"} else "_"
        for character in logical_name
    ).strip()
    if not ascii_name:
        ascii_name = "artifact"
    return f"attachment; filename=\"{ascii_name}\"; filename*=UTF-8''{quote(logical_name)}"


__all__ = [
    "ArtifactBindingMismatch",
    "ArtifactByteStream",
    "ArtifactDescriptor",
    "ArtifactNotCommitted",
    "ArtifactNotFound",
    "ArtifactRangeResult",
    "ArtifactReadError",
    "ArtifactReadService",
    "ExactArtifactRef",
    "InvalidByteRange",
    "ResolvedByteRange",
    "ViewerMetadata",
    "resolve_range",
]
