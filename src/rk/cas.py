"""Atomic, content-addressed storage for immutable research artifacts."""

from __future__ import annotations

import hashlib
import os
import re
import stat
from collections.abc import Callable, Collection, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

from rk.domain import ArtifactInput, ArtifactRef
from rk.runtime import format_utc

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_FINAL_RE = re.compile(r"^[0-9a-f]{2}/[0-9a-f]{2}/[0-9a-f]{64}$")


class CasError(RuntimeError):
    """Base class for artifact store failures."""


class CasValidationError(CasError):
    """Input path, size, or digest violates the ingest contract."""


class CasCorruptionError(CasError):
    """A content-addressed final path does not contain the named bytes."""


class CasLookupError(CasError):
    """An artifact id cannot be resolved without a database mapping."""


class _IdGenerator(Protocol):
    def new(self) -> str: ...


@dataclass(frozen=True, slots=True)
class StagedArtifact:
    artifact_id: str
    sha256: str
    byte_count: int
    media_type: str
    stage_path: Path
    final_relpath: str
    source_name: str | None = None
    line_count: int | None = None


@dataclass(frozen=True, slots=True)
class CommittedArtifact:
    artifact_id: str
    sha256: str
    byte_count: int
    media_type: str
    cas_relpath: str
    source_name: str | None
    line_count: int | None
    created_at: str

    def to_record(self) -> dict[str, Any]:
        return {
            "artifact_id": self.artifact_id,
            "sha256": self.sha256,
            "byte_count": self.byte_count,
            "media_type": self.media_type,
            "cas_relpath": self.cas_relpath,
            "ingest_state": "COMMITTED",
            "quarantine_code": None,
            "source_name": self.source_name,
            "original_path": None,
            "line_count": self.line_count,
            "created_at": self.created_at,
            "committed_at": self.created_at,
        }

    def to_ref(self, *, at_revision: int) -> ArtifactRef:
        return ArtifactRef(
            artifact_id=self.artifact_id,
            sha256=self.sha256,
            byte_count=self.byte_count,
            media_type=self.media_type,
            at_revision=at_revision,
        )


@dataclass(frozen=True, slots=True)
class OrphanCandidate:
    relpath: str
    byte_count: int
    modified_at: str
    kind: str
    eligible_for_collection: bool


FaultHook = Callable[[str, StagedArtifact], None]
ArtifactLookup = Callable[[str], Mapping[str, Any] | None]


class ContentAddressedStore:
    """Stage bytes on the CAS volume, then atomically expose a SHA-256 final path.

    ``fault_hook`` is an internal seam for deterministic crash-boundary tests. Production
    leaves it unset. Exceptions raised after ``after_final_replace`` intentionally leave an
    unreferenced final object for :meth:`scan_orphans` to discover.
    """

    def __init__(
        self,
        root: Path,
        max_bytes: int,
        inbox_roots: Collection[Path],
        orphan_grace_seconds: int,
        id_generator: _IdGenerator,
        *,
        artifact_lookup: ArtifactLookup | None = None,
        fault_hook: FaultHook | None = None,
    ) -> None:
        if max_bytes <= 0:
            raise ValueError("max_bytes must be positive")
        if orphan_grace_seconds <= 0:
            raise ValueError("orphan_grace_seconds must be positive")
        self._root = Path(root).resolve()
        self._stage_root = self._root / ".stage"
        self._max_bytes = max_bytes
        self._inbox_roots = tuple(Path(item).resolve() for item in inbox_roots)
        self._orphan_grace_seconds = orphan_grace_seconds
        self._id_generator = id_generator
        self._artifact_lookup = artifact_lookup
        self._fault_hook = fault_hook
        self._stage_root.mkdir(parents=True, exist_ok=True)

    def stage_input(self, value: ArtifactInput) -> StagedArtifact:
        source = self._validate_source_path(Path(value.path))
        if value.byte_count < 0 or value.byte_count > self._max_bytes:
            raise CasValidationError("declared byte_count exceeds the configured limit")
        if not _SHA256_RE.fullmatch(value.sha256):
            raise CasValidationError("declared sha256 must be lowercase hexadecimal")
        stage_path = self._new_stage_path()
        hasher = hashlib.sha256()
        byte_count = 0
        newline_count = 0
        last_byte: bytes | None = None
        try:
            with source.open("rb") as reader, stage_path.open("xb") as writer:
                descriptor = reader.fileno()
                if not stat.S_ISREG(os.fstat(descriptor).st_mode):
                    raise CasValidationError("artifact input must be a regular file")
                while chunk := reader.read(1024 * 1024):
                    byte_count += len(chunk)
                    if byte_count > self._max_bytes:
                        raise CasValidationError("artifact exceeds the configured byte limit")
                    hasher.update(chunk)
                    newline_count += chunk.count(b"\n")
                    last_byte = chunk[-1:]
                    writer.write(chunk)
                writer.flush()
                os.fsync(writer.fileno())
            digest = hasher.hexdigest()
            if byte_count != value.byte_count or digest != value.sha256:
                raise CasValidationError("artifact size or digest does not match its declaration")
            line_count = self._line_count(
                value.media_type, byte_count, newline_count, last_byte
            )
            return StagedArtifact(
                artifact_id=self._id_generator.new(),
                sha256=digest,
                byte_count=byte_count,
                media_type=value.media_type,
                stage_path=stage_path,
                final_relpath=self._relpath_for_digest(digest),
                source_name=value.name,
                line_count=line_count,
            )
        except BaseException:
            stage_path.unlink(missing_ok=True)
            raise

    def stage_bytes(
        self,
        data: bytes,
        *,
        media_type: str,
        source_name: str | None = None,
    ) -> StagedArtifact:
        if len(data) > self._max_bytes:
            raise CasValidationError("artifact exceeds the configured byte limit")
        digest = hashlib.sha256(data).hexdigest()
        stage_path = self._new_stage_path()
        try:
            with stage_path.open("xb") as writer:
                writer.write(data)
                writer.flush()
                os.fsync(writer.fileno())
        except BaseException:
            stage_path.unlink(missing_ok=True)
            raise
        return StagedArtifact(
            artifact_id=self._id_generator.new(),
            sha256=digest,
            byte_count=len(data),
            media_type=media_type,
            stage_path=stage_path,
            final_relpath=self._relpath_for_digest(digest),
            source_name=source_name,
            line_count=self._line_count(
                media_type, len(data), data.count(b"\n"), data[-1:] if data else None
            ),
        )

    def commit(self, staged: StagedArtifact, *, now: datetime) -> CommittedArtifact:
        self._validate_stage(staged)
        final_path = self._safe_final_path(staged.final_relpath)
        final_path.parent.mkdir(parents=True, exist_ok=True)
        if final_path.exists():
            self._verify_final(final_path, staged.sha256, staged.byte_count)
            staged.stage_path.unlink(missing_ok=True)
        else:
            self._call_fault_hook("before_final_replace", staged)
            os.replace(staged.stage_path, final_path)
            self._fsync_directory(final_path.parent)
            self._call_fault_hook("after_final_replace", staged)
            self._verify_final(final_path, staged.sha256, staged.byte_count)
        return CommittedArtifact(
            artifact_id=staged.artifact_id,
            sha256=staged.sha256,
            byte_count=staged.byte_count,
            media_type=staged.media_type,
            cas_relpath=staged.final_relpath,
            source_name=staged.source_name,
            line_count=staged.line_count,
            created_at=format_utc(now),
        )

    def discard(self, staged: StagedArtifact) -> None:
        self._validate_stage(staged, require_exists=False)
        staged.stage_path.unlink(missing_ok=True)

    def ingest(self, value: ArtifactInput, *, now: datetime) -> ArtifactRef:
        return self.commit(self.stage_input(value), now=now).to_ref(at_revision=0)

    def put_bytes(
        self,
        data: bytes,
        *,
        media_type: str,
        now: datetime,
        at_revision: int,
    ) -> ArtifactRef:
        return self.commit(
            self.stage_bytes(data, media_type=media_type), now=now
        ).to_ref(at_revision=at_revision)

    def read_bytes(self, artifact_id: str) -> bytes:
        if self._artifact_lookup is None:
            raise CasLookupError("artifact lookup is not configured")
        record = self._artifact_lookup(artifact_id)
        if record is None or record.get("ingest_state") != "COMMITTED":
            raise CasLookupError("artifact is not committed")
        relpath = str(record["cas_relpath"])
        path = self._safe_final_path(relpath)
        self._verify_final(path, str(record["sha256"]), int(record["byte_count"]))
        return path.read_bytes()

    def scan_orphans(
        self,
        referenced_relpaths: Collection[str],
        *,
        now: datetime,
    ) -> tuple[OrphanCandidate, ...]:
        referenced = set(referenced_relpaths)
        current = now.astimezone(UTC).timestamp()
        candidates: list[OrphanCandidate] = []
        for path in self._root.glob("[0-9a-f][0-9a-f]/[0-9a-f][0-9a-f]/*"):
            if not path.is_file() or path.is_symlink():
                continue
            relpath = path.relative_to(self._root).as_posix()
            if not _FINAL_RE.fullmatch(relpath) or relpath in referenced:
                continue
            candidates.append(self._orphan(path, relpath, "FINAL", current))
        for path in self._stage_root.glob("*.part"):
            if path.is_file() and not path.is_symlink():
                relpath = path.relative_to(self._root).as_posix()
                candidates.append(self._orphan(path, relpath, "STAGED", current))
        return tuple(sorted(candidates, key=lambda item: (item.kind, item.relpath)))

    def _validate_source_path(self, path: Path) -> Path:
        if not path.is_absolute():
            raise CasValidationError("artifact path must be absolute")
        raw = str(path)
        if raw.startswith(("\\\\", "//", "\\\\?\\", "\\\\.\\")):
            raise CasValidationError("network and device paths are not allowed")
        if ".." in path.parts:
            raise CasValidationError("parent path traversal is not allowed")
        for part in path.parts[1:]:
            if ":" in part:
                raise CasValidationError("alternate data streams are not allowed")
        try:
            resolved = path.resolve(strict=True)
        except OSError as exc:
            raise CasValidationError("artifact path does not exist") from exc
        allowed_root = next(
            (root for root in self._inbox_roots if resolved.is_relative_to(root)), None
        )
        if allowed_root is None:
            raise CasValidationError("artifact path is outside configured inbox roots")
        cursor = allowed_root
        self._reject_link(cursor)
        for part in resolved.relative_to(allowed_root).parts:
            cursor = cursor / part
            self._reject_link(cursor)
        if not resolved.is_file():
            raise CasValidationError("artifact input must be a regular file")
        return resolved

    @staticmethod
    def _reject_link(path: Path) -> None:
        is_junction = getattr(path, "is_junction", None)
        if path.is_symlink() or (is_junction is not None and is_junction()):
            raise CasValidationError("links and junctions are not allowed")

    def _new_stage_path(self) -> Path:
        for _ in range(32):
            path = self._stage_root / f"{self._id_generator.new()}.part"
            if not path.exists():
                return path
        raise CasError("could not allocate a unique stage path")

    def _validate_stage(self, staged: StagedArtifact, *, require_exists: bool = True) -> None:
        stage_path = staged.stage_path.resolve(strict=require_exists)
        if not stage_path.is_relative_to(self._stage_root):
            raise CasValidationError("stage path escaped the CAS staging root")
        if stage_path.suffix != ".part" or stage_path.is_symlink():
            raise CasValidationError("invalid staged artifact path")
        if not _SHA256_RE.fullmatch(staged.sha256):
            raise CasValidationError("invalid staged artifact digest")
        if staged.final_relpath != self._relpath_for_digest(staged.sha256):
            raise CasValidationError("staged final path does not match its digest")

    @staticmethod
    def _relpath_for_digest(digest: str) -> str:
        if not _SHA256_RE.fullmatch(digest):
            raise CasValidationError("invalid content digest")
        return f"{digest[:2]}/{digest[2:4]}/{digest}"

    def _safe_final_path(self, relpath: str) -> Path:
        if not _FINAL_RE.fullmatch(relpath):
            raise CasValidationError("invalid CAS relative path")
        path = (self._root / Path(relpath)).resolve()
        if not path.is_relative_to(self._root) or path.is_relative_to(self._stage_root):
            raise CasValidationError("CAS relative path escaped its root")
        return path

    @staticmethod
    def _hash_file(path: Path) -> tuple[str, int]:
        hasher = hashlib.sha256()
        size = 0
        with path.open("rb") as stream:
            while chunk := stream.read(1024 * 1024):
                size += len(chunk)
                hasher.update(chunk)
        return hasher.hexdigest(), size

    def _verify_final(self, path: Path, digest: str, byte_count: int) -> None:
        if not path.is_file() or path.is_symlink():
            raise CasCorruptionError("CAS final object is missing or not a regular file")
        actual_digest, actual_size = self._hash_file(path)
        if actual_digest != digest or actual_size != byte_count:
            raise CasCorruptionError("CAS final object does not match its address")

    def _call_fault_hook(self, point: str, staged: StagedArtifact) -> None:
        if self._fault_hook is not None:
            self._fault_hook(point, staged)

    @staticmethod
    def _fsync_directory(path: Path) -> None:
        if os.name == "nt":
            return
        descriptor = os.open(path, os.O_RDONLY)
        try:
            os.fsync(descriptor)
        finally:
            os.close(descriptor)

    @staticmethod
    def _line_count(
        media_type: str,
        byte_count: int,
        newline_count: int,
        last_byte: bytes | None,
    ) -> int | None:
        normalized = media_type.lower().split(";", 1)[0].strip()
        textual = normalized.startswith("text/") or normalized in {
            "application/json",
            "application/ld+json",
            "application/xml",
            "application/yaml",
        }
        if not textual:
            return None
        if byte_count == 0:
            return 0
        return newline_count if last_byte == b"\n" else newline_count + 1

    def _orphan(
        self,
        path: Path,
        relpath: str,
        kind: str,
        current_timestamp: float,
    ) -> OrphanCandidate:
        info = path.stat()
        modified = datetime.fromtimestamp(info.st_mtime, UTC)
        return OrphanCandidate(
            relpath=relpath,
            byte_count=info.st_size,
            modified_at=format_utc(modified),
            kind=kind,
            eligible_for_collection=(current_timestamp - info.st_mtime)
            >= self._orphan_grace_seconds,
        )
