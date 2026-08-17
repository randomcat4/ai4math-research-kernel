"""Immutable material originals, extraction artifacts, revisions, and math anchors."""

from __future__ import annotations

import difflib
import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from rk.product.artifact_read import ArtifactReadService, ExactArtifactRef
from rk.product.material_extractors import (
    ExtractedMaterial,
    ExtractionFailure,
    ImageExtractor,
    PdfExtractor,
    TexExtractor,
    TextExtractor,
)
from rk.product.material_extractors.base import project_tex, project_text
from rk.product.tool_runs import ToolRunStore
from rk.sqlite import open_sqlite
from rk.wire import canonical_json_bytes


class MaterialError(RuntimeError):
    pass


class MaterialConflict(MaterialError):
    pass


class MaterialArtifactPublisher(Protocol):
    def publish(self, *, data: bytes, logical_name: str, media_type: str) -> ExactArtifactRef: ...


class MaterialExtractor(Protocol):
    profile_id: str
    material_kind: str
    parser_name: str
    parser_build: str

    def extract(self, data: bytes) -> ExtractedMaterial: ...


@dataclass(frozen=True, slots=True)
class ExtractionProfile:
    profile_id: str
    material_kind: str
    parser_name: str
    parser_build: str
    availability: str
    unavailable_reason: str | None
    registered_at: str


@dataclass(frozen=True, slots=True)
class Material:
    material_id: str
    run_id: str
    material_kind: str
    original: ExactArtifactRef
    created_at: str


@dataclass(frozen=True, slots=True)
class MaterialExtraction:
    extraction_id: str
    material_id: str
    profile_id: str
    mode: str
    supersedes_extraction_id: str | None
    tool_run_id: str | None
    attempt_id: str | None
    status: str
    parser_build: str
    text_artifact: ExactArtifactRef | None
    layout_artifact_id: str | None
    formula_artifact_id: str | None
    difference_artifact_id: str | None
    revision_reason: str | None
    revised_by: str | None
    error_code: str | None
    error_detail: str | None
    created_at: str

    @property
    def establishes_mathematical_fact(self) -> bool:
        return False


@dataclass(frozen=True, slots=True)
class MaterialAnchor:
    anchor_id: str
    extraction_id: str
    anchor_kind: str
    locator: dict[str, object]
    excerpt: str
    excerpt_digest: str
    created_at: str


class MaterialStore:
    def __init__(
        self,
        *,
        db_path: Path,
        artifacts: ArtifactReadService,
        publisher: MaterialArtifactPublisher,
        tool_runs: ToolRunStore,
        busy_timeout_ms: int = 5_000,
    ) -> None:
        self._db_path = Path(db_path)
        self._artifacts = artifacts
        self._publisher = publisher
        self._tool_runs = tool_runs
        self._busy_timeout_ms = busy_timeout_ms
        builtins: tuple[MaterialExtractor, ...] = (
            PdfExtractor(),
            TexExtractor(),
            ImageExtractor(),
            TextExtractor(),
        )
        self._extractors = {item.profile_id: item for item in builtins}

    def register_builtin_profiles(self, *, now: str) -> tuple[ExtractionProfile, ...]:
        profiles = []
        for extractor in self._extractors.values():
            availability = str(getattr(extractor, "availability", "AVAILABLE"))
            reason_value = getattr(extractor, "unavailable_reason", None)
            reason = str(reason_value) if reason_value is not None else None
            immutable = (
                extractor.material_kind,
                extractor.parser_name,
                extractor.parser_build,
                availability,
                reason,
            )
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    "SELECT material_kind,parser_name,parser_build,availability,"
                    "unavailable_reason FROM product_material_profiles "
                    "WHERE profile_id=?",
                    (extractor.profile_id,),
                ).fetchone()
                if row is None:
                    connection.execute(
                        "INSERT INTO product_material_profiles("
                        "profile_id,material_kind,parser_name,parser_build,availability,"
                        "unavailable_reason,registered_at) VALUES(?,?,?,?,?,?,?)",
                        (extractor.profile_id, *immutable, now),
                    )
                elif tuple(row) != immutable:
                    raise MaterialConflict("extraction profile build changed under stable ID")
                connection.commit()
            profiles.append(self.get_profile(extractor.profile_id))
        return tuple(profiles)

    def get_profile(self, profile_id: str) -> ExtractionProfile:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT profile_id,material_kind,parser_name,parser_build,availability,"
                "unavailable_reason,registered_at FROM product_material_profiles "
                "WHERE profile_id=?",
                (profile_id,),
            ).fetchone()
        if row is None:
            raise KeyError(profile_id)
        return ExtractionProfile(
            str(row[0]),
            str(row[1]),
            str(row[2]),
            str(row[3]),
            str(row[4]),
            str(row[5]) if row[5] is not None else None,
            str(row[6]),
        )

    def ingest(
        self,
        *,
        material_id: str,
        run_id: str,
        material_kind: str,
        original: ExactArtifactRef,
        now: str,
    ) -> Material:
        if material_kind not in {"PDF", "TEX", "IMAGE", "TEXT"}:
            raise ValueError("material kind is unsupported")
        _require_material_media(material_kind, original.media_type)
        self._artifacts.describe(original.artifact_id, expected_ref=original)
        values = (
            run_id,
            material_kind,
            original.artifact_id,
            original.sha256,
            original.byte_count,
            original.media_type,
            now,
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT run_id,material_kind,original_artifact_id,original_artifact_sha256,"
                "original_artifact_byte_count,original_artifact_media_type,created_at "
                "FROM product_materials WHERE material_id=?",
                (material_id,),
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO product_materials("
                    "material_id,run_id,material_kind,original_artifact_id,"
                    "original_artifact_sha256,original_artifact_byte_count,"
                    "original_artifact_media_type,created_at) VALUES(?,?,?,?,?,?,?,?)",
                    (material_id, *values),
                )
            elif tuple(row) != values:
                raise MaterialConflict("material ID is bound to another immutable original")
            connection.commit()
        return self.get_material(material_id)

    def get_material(self, material_id: str) -> Material:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT material_id,run_id,material_kind,original_artifact_id,"
                "original_artifact_sha256,original_artifact_byte_count,"
                "original_artifact_media_type,created_at FROM product_materials "
                "WHERE material_id=?",
                (material_id,),
            ).fetchone()
        if row is None:
            raise KeyError(material_id)
        return Material(
            str(row[0]),
            str(row[1]),
            str(row[2]),
            ExactArtifactRef(str(row[3]), str(row[4]), int(row[5]), str(row[6])),
            str(row[7]),
        )

    def extract(
        self,
        *,
        extraction_id: str,
        material_id: str,
        profile_id: str,
        tool_run_id: str,
        attempt_id: str,
        now: str,
    ) -> MaterialExtraction:
        try:
            existing = self.get_extraction(extraction_id)
        except KeyError:
            existing = None
        if existing is not None:
            if (
                existing.material_id == material_id
                and existing.profile_id == profile_id
                and existing.mode == "MACHINE"
                and existing.tool_run_id == tool_run_id
                and existing.attempt_id == attempt_id
            ):
                return existing
            raise MaterialConflict("extraction ID is bound to another request")
        material = self.get_material(material_id)
        profile = self.get_profile(profile_id)
        if profile.material_kind != material.material_kind:
            raise MaterialError("profile does not accept this material kind")
        run = self._tool_runs.get(tool_run_id)
        if run.run_id != material.run_id or run.current_attempt_id != attempt_id:
            raise MaterialError("extraction ToolRun binding is not current")
        if profile.availability != "AVAILABLE":
            self._insert_extraction(
                extraction_id=extraction_id,
                material=material,
                profile=profile,
                mode="MACHINE",
                supersedes=None,
                tool_run_id=tool_run_id,
                attempt_id=attempt_id,
                status="PROFILE_UNAVAILABLE",
                artifacts=None,
                reason=None,
                revised_by=None,
                error=("PROFILE_UNAVAILABLE", profile.unavailable_reason or "unavailable"),
                now=now,
            )
            return self.get_extraction(extraction_id)
        raw = b"".join(
            self._artifacts.open_range(
                material.original.artifact_id, expected_ref=material.original
            ).stream
        )
        try:
            extracted = self._extractors[profile_id].extract(raw)
        except ExtractionFailure as error:
            self._insert_extraction(
                extraction_id=extraction_id,
                material=material,
                profile=profile,
                mode="MACHINE",
                supersedes=None,
                tool_run_id=tool_run_id,
                attempt_id=attempt_id,
                status="FAILED",
                artifacts=None,
                reason=None,
                revised_by=None,
                error=("EXTRACTION_FAILED", str(error)),
                now=now,
            )
            return self.get_extraction(extraction_id)
        artifacts = self._publish_projection(
            extraction_id, material.original, extracted, difference=None
        )
        self._insert_extraction(
            extraction_id=extraction_id,
            material=material,
            profile=profile,
            mode="MACHINE",
            supersedes=None,
            tool_run_id=tool_run_id,
            attempt_id=attempt_id,
            status="SUCCEEDED",
            artifacts=artifacts,
            reason=None,
            revised_by=None,
            error=None,
            now=now,
        )
        self._insert_anchors(extraction_id, extracted, now)
        return self.get_extraction(extraction_id)

    def revise(
        self,
        *,
        extraction_id: str,
        supersedes_extraction_id: str,
        corrected_text: str,
        reason: str,
        revised_by: str,
        now: str,
    ) -> MaterialExtraction:
        try:
            existing = self.get_extraction(extraction_id)
        except KeyError:
            existing = None
        if existing is not None:
            if (
                existing.mode == "HUMAN_REVISION"
                and existing.supersedes_extraction_id == supersedes_extraction_id
                and existing.revision_reason == reason.strip()
                and existing.revised_by == revised_by.strip()
            ):
                if existing.text_artifact is None:
                    raise MaterialConflict("revision artifact is missing")
                persisted = b"".join(
                    self._artifacts.open_range(
                        existing.text_artifact.artifact_id,
                        expected_ref=existing.text_artifact,
                    ).stream
                ).decode("utf-8")
                if persisted == corrected_text:
                    return existing
            raise MaterialConflict("revision ID is bound to another correction")
        previous = self.get_extraction(supersedes_extraction_id)
        if previous.status != "SUCCEEDED" or previous.text_artifact is None:
            raise MaterialError("only a successful extraction can be revised")
        if not corrected_text or not reason.strip() or not revised_by.strip():
            raise ValueError("revision text, reason, and author are required")
        old_text = b"".join(
            self._artifacts.open_range(
                previous.text_artifact.artifact_id,
                expected_ref=previous.text_artifact,
            ).stream
        ).decode("utf-8")
        if old_text == corrected_text:
            raise MaterialError("revision must change extracted text")
        material = self.get_material(previous.material_id)
        profile = self.get_profile(previous.profile_id)
        projected = (
            project_tex(corrected_text)
            if material.material_kind == "TEX"
            else project_text(corrected_text, formula_origin="HUMAN_CORRECTED_FORMULA")
        )
        difference = "".join(
            difflib.unified_diff(
                old_text.splitlines(keepends=True),
                corrected_text.splitlines(keepends=True),
                fromfile=supersedes_extraction_id,
                tofile=extraction_id,
            )
        ).encode()
        artifacts = self._publish_projection(
            extraction_id, material.original, projected, difference=difference
        )
        self._insert_extraction(
            extraction_id=extraction_id,
            material=material,
            profile=profile,
            mode="HUMAN_REVISION",
            supersedes=supersedes_extraction_id,
            tool_run_id=None,
            attempt_id=None,
            status="SUCCEEDED",
            artifacts=artifacts,
            reason=reason.strip(),
            revised_by=revised_by.strip(),
            error=None,
            now=now,
            parser_build="HUMAN_REVISION_V1",
        )
        self._insert_anchors(extraction_id, projected, now)
        return self.get_extraction(extraction_id)

    def get_extraction(self, extraction_id: str) -> MaterialExtraction:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT extraction_id,material_id,profile_id,mode,supersedes_extraction_id,"
                "tool_run_id,attempt_id,status,parser_build,text_artifact_id,"
                "text_artifact_sha256,text_artifact_byte_count,text_artifact_media_type,"
                "layout_artifact_id,formula_artifact_id,difference_artifact_id,"
                "revision_reason,revised_by,error_code,error_detail,created_at "
                "FROM product_material_extractions WHERE extraction_id=?",
                (extraction_id,),
            ).fetchone()
        if row is None:
            raise KeyError(extraction_id)
        text_ref = (
            ExactArtifactRef(str(row[9]), str(row[10]), int(row[11]), str(row[12]))
            if row[9] is not None
            else None
        )
        return MaterialExtraction(
            extraction_id=str(row[0]),
            material_id=str(row[1]),
            profile_id=str(row[2]),
            mode=str(row[3]),
            supersedes_extraction_id=str(row[4]) if row[4] is not None else None,
            tool_run_id=str(row[5]) if row[5] is not None else None,
            attempt_id=str(row[6]) if row[6] is not None else None,
            status=str(row[7]),
            parser_build=str(row[8]),
            text_artifact=text_ref,
            layout_artifact_id=str(row[13]) if row[13] is not None else None,
            formula_artifact_id=str(row[14]) if row[14] is not None else None,
            difference_artifact_id=str(row[15]) if row[15] is not None else None,
            revision_reason=str(row[16]) if row[16] is not None else None,
            revised_by=str(row[17]) if row[17] is not None else None,
            error_code=str(row[18]) if row[18] is not None else None,
            error_detail=str(row[19]) if row[19] is not None else None,
            created_at=str(row[20]),
        )

    def anchors(self, extraction_id: str) -> tuple[MaterialAnchor, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT anchor_id,extraction_id,anchor_kind,locator_json,excerpt,"
                "excerpt_digest,created_at FROM product_material_anchors "
                "WHERE extraction_id=? ORDER BY anchor_kind,anchor_id",
                (extraction_id,),
            ).fetchall()
        return tuple(_anchor(row) for row in rows)

    def get_anchor(self, anchor_id: str) -> MaterialAnchor:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT anchor_id,extraction_id,anchor_kind,locator_json,excerpt,"
                "excerpt_digest,created_at FROM product_material_anchors WHERE anchor_id=?",
                (anchor_id,),
            ).fetchone()
        if row is None:
            raise KeyError(anchor_id)
        return _anchor(row)

    def _publish_projection(
        self,
        extraction_id: str,
        original: ExactArtifactRef,
        extracted: ExtractedMaterial,
        *,
        difference: bytes | None,
    ) -> tuple[ExactArtifactRef, ExactArtifactRef, ExactArtifactRef, ExactArtifactRef]:
        text_ref = self._publisher.publish(
            data=extracted.text.encode(),
            logical_name=f"{extraction_id}.txt",
            media_type="text/plain",
        )
        layout_ref = self._publisher.publish(
            data=canonical_json_bytes({"objects": list(extracted.layout)}),
            logical_name=f"{extraction_id}.layout.json",
            media_type="application/json",
        )
        formula_ref = self._publisher.publish(
            data=canonical_json_bytes({"objects": list(extracted.formulas)}),
            logical_name=f"{extraction_id}.formulas.json",
            media_type="application/json",
        )
        difference_bytes = difference or canonical_json_bytes(
            {
                "schema_version": "rk.material-original-difference.v1",
                "original_artifact_id": original.artifact_id,
                "original_sha256": original.sha256,
                "extraction_artifact_id": text_ref.artifact_id,
                "extraction_sha256": text_ref.sha256,
                "relation": "DERIVED_NOT_IDENTICAL",
            }
        )
        difference_ref = self._publisher.publish(
            data=difference_bytes,
            logical_name=f"{extraction_id}.diff",
            media_type=("text/x-diff" if difference is not None else "application/json"),
        )
        return text_ref, layout_ref, formula_ref, difference_ref

    def _insert_extraction(
        self,
        *,
        extraction_id: str,
        material: Material,
        profile: ExtractionProfile,
        mode: str,
        supersedes: str | None,
        tool_run_id: str | None,
        attempt_id: str | None,
        status: str,
        artifacts: tuple[ExactArtifactRef, ExactArtifactRef, ExactArtifactRef, ExactArtifactRef]
        | None,
        reason: str | None,
        revised_by: str | None,
        error: tuple[str, str] | None,
        now: str,
        parser_build: str | None = None,
    ) -> None:
        if artifacts is None:
            artifact_values: tuple[object, ...] = (None,) * 7
        else:
            text_ref, layout_ref, formula_ref, difference_ref = artifacts
            artifact_values = (
                text_ref.artifact_id,
                text_ref.sha256,
                text_ref.byte_count,
                text_ref.media_type,
                layout_ref.artifact_id,
                formula_ref.artifact_id,
                difference_ref.artifact_id,
            )
        error_code, error_detail = error if error is not None else (None, None)
        values = (
            material.material_id,
            profile.profile_id,
            mode,
            supersedes,
            tool_run_id,
            attempt_id,
            status,
            parser_build or profile.parser_build,
            *artifact_values,
            reason,
            revised_by,
            error_code,
            error_detail,
            now,
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT material_id,profile_id,mode,supersedes_extraction_id,tool_run_id,"
                "attempt_id,status,parser_build,text_artifact_id,text_artifact_sha256,"
                "text_artifact_byte_count,text_artifact_media_type,layout_artifact_id,"
                "formula_artifact_id,difference_artifact_id,revision_reason,revised_by,"
                "error_code,error_detail,created_at FROM product_material_extractions "
                "WHERE extraction_id=?",
                (extraction_id,),
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO product_material_extractions("
                    "extraction_id,material_id,profile_id,mode,supersedes_extraction_id,"
                    "tool_run_id,attempt_id,status,parser_build,text_artifact_id,"
                    "text_artifact_sha256,text_artifact_byte_count,text_artifact_media_type,"
                    "layout_artifact_id,formula_artifact_id,difference_artifact_id,"
                    "revision_reason,revised_by,error_code,error_detail,created_at)"
                    " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (extraction_id, *values),
                )
            elif tuple(row) != values:
                raise MaterialConflict("extraction ID is bound to another projection")
            connection.commit()

    def _insert_anchors(self, extraction_id: str, extracted: ExtractedMaterial, now: str) -> None:
        records: list[tuple[str, str, str, str, str, str]] = []
        for kind, objects in (
            ("PAGE_SEGMENT", extracted.layout),
            ("FORMULA", extracted.formulas),
        ):
            for value in objects:
                excerpt = str(value.get("source", value.get("text", "")))
                locator = {
                    key: item for key, item in value.items() if key not in {"source", "text"}
                }
                locator_json = _json(locator)
                digest = hashlib.sha256(excerpt.encode()).hexdigest()
                anchor_value = {
                    "extraction_id": extraction_id,
                    "kind": kind,
                    "locator": locator,
                    "excerpt_digest": digest,
                }
                anchor_id = (
                    "anchor-" + hashlib.sha256(canonical_json_bytes(anchor_value)).hexdigest()[:32]
                )
                records.append((anchor_id, extraction_id, kind, locator_json, excerpt, digest))
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            for record in records:
                connection.execute(
                    "INSERT INTO product_material_anchors("
                    "anchor_id,extraction_id,anchor_kind,locator_json,excerpt,excerpt_digest,"
                    "created_at) VALUES(?,?,?,?,?,?,?)",
                    (*record, now),
                )
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        connection = open_sqlite(self._db_path, isolation_level=None)
        connection.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms}")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection


def _anchor(row: tuple[Any, ...]) -> MaterialAnchor:
    locator = json.loads(str(row[3]))
    if not isinstance(locator, dict):
        raise MaterialError("persisted anchor locator is invalid")
    return MaterialAnchor(
        str(row[0]),
        str(row[1]),
        str(row[2]),
        locator,
        str(row[4]),
        str(row[5]),
        str(row[6]),
    )


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _require_material_media(material_kind: str, media_type: str) -> None:
    normalized = media_type.partition(";")[0].strip().lower()
    accepted = {
        "PDF": normalized == "application/pdf",
        "TEX": normalized in {"application/x-tex", "text/x-tex"},
        "IMAGE": normalized.startswith("image/"),
        "TEXT": normalized in {"text/plain", "text/markdown"},
    }
    if not accepted[material_kind]:
        raise MaterialError("original media type does not match material kind")


__all__ = [
    "ExtractionProfile",
    "Material",
    "MaterialAnchor",
    "MaterialArtifactPublisher",
    "MaterialConflict",
    "MaterialError",
    "MaterialExtraction",
    "MaterialStore",
]
