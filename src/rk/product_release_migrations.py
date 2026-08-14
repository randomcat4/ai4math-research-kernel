"""Frozen release ordering over D00a-validated product schema fragments."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import cast

from rk.product_migrations import (
    AppliedFragment,
    AssemblyStep,
    ProductMigrationAssembler,
    ProductMigrationError,
    ProductMigrationRegistry,
    SchemaFragment,
)

_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class ProductReleaseMigrationError(ProductMigrationError):
    """The release manifest, lock, registry, or installed release has drifted."""


@dataclass(frozen=True, slots=True)
class ProductReleaseFragment:
    release_position: int
    fragment_id: str
    sha256: str


@dataclass(frozen=True, slots=True)
class ProductReleaseManifest:
    schema_version: str
    release_id: str
    product_version: str
    release_status: str
    sealed: bool
    source_registry: str
    fragments: tuple[ProductReleaseFragment, ...]
    manifest_sha256: str

    @classmethod
    def load(cls, manifest_path: Path, lock_path: Path) -> ProductReleaseManifest:
        raw = Path(manifest_path).read_bytes()
        actual_digest = hashlib.sha256(raw).hexdigest()
        try:
            locked_digest = Path(lock_path).read_text(encoding="ascii").strip()
        except UnicodeDecodeError as error:
            raise ProductReleaseMigrationError("release lock must be ASCII") from error
        if not _SHA256.fullmatch(locked_digest) or locked_digest != actual_digest:
            raise ProductReleaseMigrationError("release manifest lock digest differs")
        try:
            document = json.loads(raw)
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ProductReleaseMigrationError(
                "release manifest is not valid UTF-8 JSON"
            ) from error
        if not isinstance(document, dict) or set(document) != {
            "schema_version",
            "release_id",
            "product_version",
            "release_status",
            "sealed",
            "source_registry",
            "fragments",
        }:
            raise ProductReleaseMigrationError("release manifest fields are not exact")
        scalar_names = (
            "schema_version",
            "release_id",
            "product_version",
            "release_status",
            "source_registry",
        )
        if any(not isinstance(document[name], str) or not document[name] for name in scalar_names):
            raise ProductReleaseMigrationError("release manifest scalar is invalid")
        if document["schema_version"] != "rk.product.migration-manifest.v1":
            raise ProductReleaseMigrationError("release manifest schema version is unsupported")
        if document["source_registry"] != "schema_fragments":
            raise ProductReleaseMigrationError("release manifest source registry is invalid")
        if document["release_status"] not in {"BACKEND_ONLY", "R00_SEALED"}:
            raise ProductReleaseMigrationError("release manifest status is invalid")
        sealed = document["sealed"]
        if not isinstance(sealed, bool) or sealed != (document["release_status"] == "R00_SEALED"):
            raise ProductReleaseMigrationError("release manifest seal and status differ")
        raw_fragments = document["fragments"]
        if not isinstance(raw_fragments, list) or not raw_fragments:
            raise ProductReleaseMigrationError("release manifest has no fragments")
        fragments: list[ProductReleaseFragment] = []
        for raw_fragment in raw_fragments:
            if not isinstance(raw_fragment, dict) or set(raw_fragment) != {
                "release_position",
                "fragment_id",
                "sha256",
            }:
                raise ProductReleaseMigrationError("release fragment fields are not exact")
            position = raw_fragment["release_position"]
            fragment_id = raw_fragment["fragment_id"]
            sha256 = raw_fragment["sha256"]
            if (
                not isinstance(position, int)
                or isinstance(position, bool)
                or position < 1
                or not isinstance(fragment_id, str)
                or "/" not in fragment_id
                or not isinstance(sha256, str)
                or not _SHA256.fullmatch(sha256)
            ):
                raise ProductReleaseMigrationError("release fragment binding is invalid")
            fragments.append(ProductReleaseFragment(position, fragment_id, sha256))
        positions = tuple(item.release_position for item in fragments)
        identities = tuple(item.fragment_id for item in fragments)
        if positions != tuple(range(1, len(fragments) + 1)):
            raise ProductReleaseMigrationError("release fragment positions are not contiguous")
        if len(set(identities)) != len(identities):
            raise ProductReleaseMigrationError("release fragment identity is duplicated")
        return cls(
            cast(str, document["schema_version"]),
            cast(str, document["release_id"]),
            cast(str, document["product_version"]),
            cast(str, document["release_status"]),
            sealed,
            cast(str, document["source_registry"]),
            tuple(fragments),
            actual_digest,
        )


class _FrozenReleaseRegistry(ProductMigrationRegistry):
    def __init__(self, plan: tuple[AssemblyStep, ...]) -> None:
        self._frozen_plan = plan

    def plan(self) -> tuple[AssemblyStep, ...]:
        return self._frozen_plan


class ProductReleaseMigrationAssembler:
    """Apply the lock-verified release plan through the D00a assembler."""

    def __init__(
        self,
        *,
        fragment_root: Path,
        manifest_path: Path,
        lock_path: Path,
    ) -> None:
        self._fragment_root = Path(fragment_root)
        self._manifest_path = Path(manifest_path)
        self._lock_path = Path(lock_path)

    def manifest(self) -> ProductReleaseManifest:
        return ProductReleaseManifest.load(self._manifest_path, self._lock_path)

    def plan(self) -> tuple[AssemblyStep, ...]:
        manifest = self.manifest()
        discovered = ProductMigrationRegistry(self._fragment_root).discover()
        by_id = {fragment.fragment_id: fragment for fragment in discovered}
        manifest_ids = {item.fragment_id for item in manifest.fragments}
        registry_ids = set(by_id)
        if manifest_ids != registry_ids:
            missing = sorted(registry_ids - manifest_ids)
            unknown = sorted(manifest_ids - registry_ids)
            raise ProductReleaseMigrationError(
                f"release manifest registry set differs; missing={missing}, unknown={unknown}"
            )
        plan: list[AssemblyStep] = []
        for item in manifest.fragments:
            fragment: SchemaFragment = by_id[item.fragment_id]
            if fragment.sha256 != item.sha256:
                raise ProductReleaseMigrationError(
                    f"release fragment digest has drifted: {item.fragment_id}"
                )
            plan.append(AssemblyStep(item.release_position, fragment))
        return tuple(plan)

    def apply(self, connection: sqlite3.Connection) -> tuple[AppliedFragment, ...]:
        plan = self.plan()
        return ProductMigrationAssembler(_FrozenReleaseRegistry(plan)).apply(connection)


__all__ = [
    "ProductReleaseFragment",
    "ProductReleaseManifest",
    "ProductReleaseMigrationAssembler",
    "ProductReleaseMigrationError",
]
