"""Internal seams used by the deep ResearchKernel module.

These protocols are not public caller interfaces. They exist because production and in-memory
test adapters both vary at the same seam.
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import datetime
from pathlib import Path
from typing import Any, Protocol

from rk.domain import ArtifactInput, ArtifactRef, VerifiedCapability


class Clock(Protocol):
    def now(self) -> datetime: ...


class IdGenerator(Protocol):
    def new(self) -> str: ...


class CapabilityVerifier(Protocol):
    def verify(
        self, credential_path: Path, action: str, run_id: str | None
    ) -> VerifiedCapability: ...


class ArtifactStore(Protocol):
    def ingest(self, value: ArtifactInput, *, now: datetime) -> ArtifactRef: ...

    def put_bytes(
        self, data: bytes, *, media_type: str, now: datetime, at_revision: int
    ) -> ArtifactRef: ...

    def read_bytes(self, artifact_id: str) -> bytes: ...


class ExecutionAdapter(Protocol):
    name: str
    version: str

    def run(self, request: Mapping[str, Any]) -> Mapping[str, Any]: ...
