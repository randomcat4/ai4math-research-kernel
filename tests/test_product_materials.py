from __future__ import annotations

import hashlib
import shutil
import sqlite3
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

from rk.cas import ContentAddressedStore
from rk.product.artifact_read import ArtifactReadService, ExactArtifactRef
from rk.product.compute import (
    AuthorityCeiling,
    ResourceRequest,
    ToolAvailability,
    ToolFunctionSpec,
    prepare_tool_invocation,
)
from rk.product.jobs import JobStore, RetrySafety
from rk.product.materials import MaterialStore
from rk.product.operations import OperationStore
from rk.product.tool_runs import ToolCatalogStore, ToolRunStore
from rk.product_migrations import ProductMigrationAssembler, ProductMigrationRegistry
from rk.wire import canonical_json_bytes

NOW = "2026-08-14T00:00:00Z"


class Ids:
    def __init__(self) -> None:
        self.value = 0

    def new(self) -> str:
        self.value += 1
        return f"artifact-{self.value}"


class RegistryPublisher:
    def __init__(self, root: Path) -> None:
        self.records: dict[str, dict[str, object]] = {}
        self.cas = ContentAddressedStore(
            root,
            max_bytes=20 * 1024 * 1024,
            inbox_roots=(),
            orphan_grace_seconds=60,
            id_generator=Ids(),
        )

    def publish(self, *, data: bytes, logical_name: str, media_type: str) -> ExactArtifactRef:
        committed = self.cas.commit(
            self.cas.stage_bytes(data, media_type=media_type, source_name=logical_name),
            now=datetime(2026, 8, 14, tzinfo=UTC),
        )
        self.records[committed.artifact_id] = committed.to_record()
        return ExactArtifactRef(
            committed.artifact_id,
            committed.sha256,
            committed.byte_count,
            committed.media_type,
        )

    def get_artifact(self, artifact_id: str) -> dict[str, object] | None:
        return self.records.get(artifact_id)


class ArgumentReader:
    def read_json(self, artifact_ref: ExactArtifactRef) -> dict[str, str]:
        return {"material_id": artifact_ref.artifact_id}


def migrated_db(tmp_path: Path) -> Path:
    db = tmp_path / "product.sqlite"
    with sqlite3.connect(db) as connection:
        ProductMigrationAssembler(ProductMigrationRegistry(Path("schema_fragments"))).apply(
            connection
        )
    return db


def declaration() -> ToolFunctionSpec:
    schema = {
        "type": "object",
        "additionalProperties": False,
        "required": ["material_id"],
        "properties": {"material_id": {"type": "string"}},
    }
    return ToolFunctionSpec(
        tool_id="material-extractor",
        tool_version="1",
        function_name="extract",
        provider="rk",
        build_version="build-1",
        profile_id="materials",
        function_schema=schema,
        function_schema_digest=hashlib.sha256(canonical_json_bytes(schema)).hexdigest(),
        availability=ToolAvailability.AVAILABLE,
        authority_ceiling=AuthorityCeiling.NO_FACT_GRAPH_WRITE,
    )


def add_run(
    db: Path,
    jobs: JobStore,
    runs: ToolRunStore,
    number: int,
    original: ExactArtifactRef,
) -> tuple[str, str]:
    request_id = f"request-{number}"
    job_id = f"job-{number}"
    receipt_id = f"receipt-{number}"
    body = {
        "schema_version": "rk.product.receipt.v1",
        "request_id": request_id,
        "scope": {
            "kind": "RUN",
            "run_id": "run-1",
            "expected_revision": 3,
            "expected_contract_version": 1,
        },
        "updated_at": NOW,
        "state": "PENDING",
        "job_id": job_id,
    }
    OperationStore(db, iter([receipt_id]).__next__).reserve(
        scope_key="RUN:run-1",
        request_id=request_id,
        request_digest=hashlib.sha256(request_id.encode()).hexdigest(),
        pending_receipt=body,
        now=NOW,
    )
    jobs.enqueue(
        job_id=job_id,
        receipt_id=receipt_id,
        scope_kind="RUN",
        run_id="run-1",
        deployment_id=None,
        kind="RUN_TOOL",
        requested_by="literature-reviewer",
        request_id=request_id,
        retry_safety=RetrySafety.IDEMPOTENT,
        idempotency_key=None,
        now=NOW,
    )
    arguments = ExactArtifactRef(f"arguments-{number}", "a" * 64, 2, "application/json")
    prepared = prepare_tool_invocation(
        spec=declaration(),
        arguments_artifact=arguments,
        input_artifact_ids=(original.artifact_id,),
        resources=ResourceRequest(1_000, 256 * 1024 * 1024, 60_000),
        authority_ceiling=AuthorityCeiling.NO_FACT_GRAPH_WRITE,
        artifacts=ArgumentReader(),
    )
    tool_run_id, attempt_id = f"tool-run-{number}", f"attempt-{number}"
    runs.create(
        tool_run_id=tool_run_id,
        run_id="run-1",
        research_revision=3,
        contract_version=1,
        request_id=request_id,
        requested_by="literature-reviewer",
        invocation=prepared,
        attempt_id=attempt_id,
        job_id=job_id,
        now=NOW,
    )
    return tool_run_id, attempt_id


def setup(tmp_path: Path):
    db = migrated_db(tmp_path)
    ToolCatalogStore(db).register(declaration(), now=NOW)
    jobs = JobStore(db, iter(["unused"]).__next__)
    runs = ToolRunStore(db, jobs)
    publisher = RegistryPublisher(tmp_path / "cas")
    reader = ArtifactReadService(metadata=publisher, cas_root=tmp_path / "cas")
    store = MaterialStore(
        db_path=db,
        artifacts=reader,
        publisher=publisher,
        tool_runs=runs,
    )
    store.register_builtin_profiles(now=NOW)
    return db, jobs, runs, publisher, reader, store


def render_formula_assets(tmp_path: Path) -> tuple[bytes, bytes]:
    tex = r"""\documentclass{article}
\pagestyle{empty}
\begin{document}
For every $\varepsilon>0$ there exists $\delta>0$ and
\[ \int_0^1 x^2\,dx = \frac{1}{3}. \]
\end{document}
"""
    source = tmp_path / "formula.tex"
    source.write_text(tex, encoding="utf-8")
    subprocess.run(
        ("pdflatex", "-interaction=nonstopmode", "-halt-on-error", source.name),
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    pdf = (tmp_path / "formula.pdf").read_bytes()
    subprocess.run(
        ("pdftoppm", "-png", "-singlefile", "-r", "300", "formula.pdf", "formula"),
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    return pdf, (tmp_path / "formula.png").read_bytes()


def test_real_pdf_tex_image_and_text_profiles_create_artifacts_and_anchors(
    tmp_path: Path,
) -> None:
    db, jobs, runs, publisher, reader, store = setup(tmp_path)
    pdf, image = render_formula_assets(tmp_path)
    inputs = [
        ("PDF", "pdf_poppler_v1", pdf, "application/pdf"),
        (
            "TEX",
            "tex_source_v1",
            b"Let $x^2+y^2=z^2$.\\[\\sum_{i=1}^n i=n(n+1)/2\\]",
            "text/x-tex",
        ),
        ("IMAGE", "image_tesseract_v1", image, "image/png"),
        ("TEXT", "text_utf8_v1", "Energy E = mc².\n".encode(), "text/plain"),
    ]
    anchor_sets = []
    for number, (kind, profile, data, media) in enumerate(inputs, start=1):
        original = publisher.publish(data=data, logical_name=f"source-{number}", media_type=media)
        store.ingest(
            material_id=f"material-{number}",
            run_id="run-1",
            material_kind=kind,
            original=original,
            now=NOW,
        )
        tool_run_id, attempt_id = add_run(db, jobs, runs, number, original)
        extraction = store.extract(
            extraction_id=f"extraction-{number}",
            material_id=f"material-{number}",
            profile_id=profile,
            tool_run_id=tool_run_id,
            attempt_id=attempt_id,
            now=NOW,
        )
        assert extraction.status == "SUCCEEDED"
        assert extraction.text_artifact is not None
        assert extraction.layout_artifact_id in publisher.records
        assert extraction.formula_artifact_id in publisher.records
        assert extraction.difference_artifact_id in publisher.records
        assert extraction.establishes_mathematical_fact is False
        anchors = store.anchors(extraction.extraction_id)
        assert any(anchor.anchor_kind == "PAGE_SEGMENT" for anchor in anchors)
        assert any(anchor.anchor_kind == "FORMULA" for anchor in anchors)
        anchor_sets.append(tuple(anchor.anchor_id for anchor in anchors))

    restarted = MaterialStore(db_path=db, artifacts=reader, publisher=publisher, tool_runs=runs)
    restarted.register_builtin_profiles(now=NOW)
    for number, expected in enumerate(anchor_sets, start=1):
        assert (
            tuple(anchor.anchor_id for anchor in restarted.anchors(f"extraction-{number}"))
            == expected
        )


def test_ocr_symbol_error_is_revised_without_mutating_original_extraction(
    tmp_path: Path,
) -> None:
    db, jobs, runs, publisher, reader, store = setup(tmp_path)
    _, image = render_formula_assets(tmp_path)
    original = publisher.publish(data=image, logical_name="formula.png", media_type="image/png")
    store.ingest(
        material_id="material-image",
        run_id="run-1",
        material_kind="IMAGE",
        original=original,
        now=NOW,
    )
    tool_run_id, attempt_id = add_run(db, jobs, runs, 1, original)
    machine = store.extract(
        extraction_id="ocr-1",
        material_id="material-image",
        profile_id="image_tesseract_v1",
        tool_run_id=tool_run_id,
        attempt_id=attempt_id,
        now=NOW,
    )
    assert machine.text_artifact is not None
    ocr_text = b"".join(
        reader.open_range(
            machine.text_artifact.artifact_id,
            expected_ref=machine.text_artifact,
        ).stream
    ).decode()
    corrected = "∀ ε > 0, ∃ δ > 0 with δ ≤ ε; ∫₀¹ x² dx = 1/3.\n"
    assert ocr_text != corrected

    revised = store.revise(
        extraction_id="ocr-2",
        supersedes_extraction_id="ocr-1",
        corrected_text=corrected,
        reason="OCR lost quantifiers, superscripts, integral bounds, and ≤",
        revised_by="literature-reviewer",
        now="2026-08-14T00:10:00Z",
    )
    assert revised.mode == "HUMAN_REVISION"
    assert revised.supersedes_extraction_id == "ocr-1"
    assert revised.text_artifact is not None
    assert revised.text_artifact != machine.text_artifact
    assert store.get_extraction("ocr-1") == machine
    corrected_formula = next(
        anchor
        for anchor in store.anchors("ocr-2")
        if anchor.anchor_kind == "FORMULA" and "≤" in anchor.excerpt
    )
    assert store.get_anchor(corrected_formula.anchor_id) == corrected_formula
    restarted = MaterialStore(db_path=db, artifacts=reader, publisher=publisher, tool_runs=runs)
    assert restarted.get_anchor(corrected_formula.anchor_id) == corrected_formula
    diff_record = publisher.records[revised.difference_artifact_id]
    diff_ref = ExactArtifactRef(
        str(diff_record["artifact_id"]),
        str(diff_record["sha256"]),
        int(diff_record["byte_count"]),
        str(diff_record["media_type"]),
    )
    diff = b"".join(reader.open_range(diff_ref.artifact_id, expected_ref=diff_ref).stream).decode()
    assert "OCR lost" not in diff
    assert corrected.rstrip() in diff


def test_machine_extraction_and_revision_replays_are_exactly_idempotent(
    tmp_path: Path,
) -> None:
    db, jobs, runs, publisher, _, store = setup(tmp_path)
    original = publisher.publish(
        data=b"x = 1\n", logical_name="source.txt", media_type="text/plain"
    )
    store.ingest(
        material_id="material-1",
        run_id="run-1",
        material_kind="TEXT",
        original=original,
        now=NOW,
    )
    tool_run_id, attempt_id = add_run(db, jobs, runs, 1, original)
    first = store.extract(
        extraction_id="extract-1",
        material_id="material-1",
        profile_id="text_utf8_v1",
        tool_run_id=tool_run_id,
        attempt_id=attempt_id,
        now=NOW,
    )
    assert (
        store.extract(
            extraction_id="extract-1",
            material_id="material-1",
            profile_id="text_utf8_v1",
            tool_run_id=tool_run_id,
            attempt_id=attempt_id,
            now="later",
        )
        == first
    )
    revision = store.revise(
        extraction_id="extract-2",
        supersedes_extraction_id="extract-1",
        corrected_text="x = 2\n",
        reason="correct numeral",
        revised_by="reviewer",
        now=NOW,
    )
    assert (
        store.revise(
            extraction_id="extract-2",
            supersedes_extraction_id="extract-1",
            corrected_text="x = 2\n",
            reason="correct numeral",
            revised_by="reviewer",
            now="later",
        )
        == revision
    )


def test_missing_ocr_engine_is_persisted_as_unavailable_profile(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    original_which = shutil.which
    monkeypatch.setattr(
        shutil,
        "which",
        lambda command: None if command == "tesseract" else original_which(command),
    )
    db = migrated_db(tmp_path)
    ToolCatalogStore(db).register(declaration(), now=NOW)
    jobs = JobStore(db, iter(["unused"]).__next__)
    runs = ToolRunStore(db, jobs)
    publisher = RegistryPublisher(tmp_path / "cas")
    reader = ArtifactReadService(metadata=publisher, cas_root=tmp_path / "cas")
    store = MaterialStore(db_path=db, artifacts=reader, publisher=publisher, tool_runs=runs)
    store.register_builtin_profiles(now=NOW)
    assert store.get_profile("image_tesseract_v1").availability == "UNAVAILABLE"
    image = publisher.publish(
        data=b"not-read-because-profile-unavailable",
        logical_name="formula.png",
        media_type="image/png",
    )
    store.ingest(
        material_id="material-1",
        run_id="run-1",
        material_kind="IMAGE",
        original=image,
        now=NOW,
    )
    tool_run_id, attempt_id = add_run(db, jobs, runs, 1, image)
    result = store.extract(
        extraction_id="extract-1",
        material_id="material-1",
        profile_id="image_tesseract_v1",
        tool_run_id=tool_run_id,
        attempt_id=attempt_id,
        now=NOW,
    )
    assert result.status == "PROFILE_UNAVAILABLE"
    assert result.error_code == "PROFILE_UNAVAILABLE"
    assert result.text_artifact is None
