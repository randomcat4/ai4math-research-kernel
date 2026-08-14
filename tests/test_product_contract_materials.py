from __future__ import annotations

import hashlib
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
from rk.product.contract_materials import (
    ContractMaterialConflict,
    ContractMaterialError,
    ContractMaterialService,
)
from rk.product.contracts import AmbiguitySpec, ContractContent, ContractError, ContractStore
from rk.product.invalidation import AuthorityInvalidationEngine, AuthorityObjectKind
from rk.product.jobs import JobStore, RetrySafety
from rk.product.materials import MaterialAnchor, MaterialStore
from rk.product.operations import OperationStore
from rk.product.tool_runs import ToolCatalogStore, ToolRunStore
from rk.product_migrations import ProductMigrationAssembler, ProductMigrationRegistry
from rk.wire import canonical_json_bytes

ROOT = Path(__file__).parents[1]
NOW = "2026-08-14T00:00:00Z"


class Ids:
    def __init__(self) -> None:
        self.value = 0

    def new(self) -> str:
        self.value += 1
        return f"artifact-{self.value}"


class Publisher:
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


def database(tmp_path: Path) -> Path:
    db = tmp_path / "product.sqlite"
    with sqlite3.connect(db) as connection:
        ProductMigrationAssembler(
            ProductMigrationRegistry(ROOT / "schema_fragments")
        ).apply(connection)
    return db


def add_run(
    db: Path,
    jobs: JobStore,
    runs: ToolRunStore,
    original: ExactArtifactRef,
) -> tuple[str, str]:
    body = {
        "schema_version": "rk.product.receipt.v1",
        "request_id": "request-1",
        "scope": {
            "kind": "RUN",
            "run_id": "run-1",
            "expected_revision": 4,
            "expected_contract_version": 1,
        },
        "updated_at": NOW,
        "state": "PENDING",
        "job_id": "job-1",
    }
    OperationStore(db, iter(["receipt-1"]).__next__).reserve(
        scope_key="RUN:run-1",
        request_id="request-1",
        request_digest="a" * 64,
        pending_receipt=body,
        now=NOW,
    )
    jobs.enqueue(
        job_id="job-1",
        receipt_id="receipt-1",
        scope_kind="RUN",
        run_id="run-1",
        deployment_id=None,
        kind="RUN_TOOL",
        requested_by="reviewer",
        request_id="request-1",
        retry_safety=RetrySafety.IDEMPOTENT,
        idempotency_key=None,
        now=NOW,
    )
    arguments = ExactArtifactRef("arguments-1", "b" * 64, 2, "application/json")
    invocation = prepare_tool_invocation(
        spec=declaration(),
        arguments_artifact=arguments,
        input_artifact_ids=(original.artifact_id,),
        resources=ResourceRequest(1_000, 256 * 1024 * 1024, 60_000),
        authority_ceiling=AuthorityCeiling.NO_FACT_GRAPH_WRITE,
        artifacts=ArgumentReader(),
    )
    runs.create(
        tool_run_id="tool-run-1",
        run_id="run-1",
        research_revision=4,
        contract_version=1,
        request_id="request-1",
        requested_by="reviewer",
        invocation=invocation,
        attempt_id="attempt-1",
        job_id="job-1",
        now=NOW,
    )
    return "tool-run-1", "attempt-1"


def render_formula_image(tmp_path: Path) -> bytes:
    source = tmp_path / "contract-formula.tex"
    source.write_text(
        r"""\documentclass{article}
\pagestyle{empty}
\begin{document}
For every $\varepsilon>0$ there exists $\delta>0$ and
\[ \int_0^1 x^2\,dx = \frac{1}{3}. \]
\end{document}
""",
        encoding="utf-8",
    )
    subprocess.run(
        ("pdflatex", "-interaction=nonstopmode", "-halt-on-error", source.name),
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    subprocess.run(
        (
            "pdftoppm",
            "-png",
            "-singlefile",
            "-r",
            "300",
            "contract-formula.pdf",
            "contract-formula",
        ),
        cwd=tmp_path,
        check=True,
        capture_output=True,
    )
    return (tmp_path / "contract-formula.png").read_bytes()


def setup_real_material(
    tmp_path: Path,
) -> tuple[Path, MaterialStore, MaterialAnchor, MaterialAnchor]:
    db = database(tmp_path)
    ToolCatalogStore(db).register(declaration(), now=NOW)
    jobs = JobStore(db, iter(["unused"]).__next__)
    runs = ToolRunStore(db, jobs)
    publisher = Publisher(tmp_path / "cas")
    reader = ArtifactReadService(metadata=publisher, cas_root=tmp_path / "cas")
    materials = MaterialStore(
        db_path=db,
        artifacts=reader,
        publisher=publisher,
        tool_runs=runs,
    )
    materials.register_builtin_profiles(now=NOW)
    original = publisher.publish(
        data=render_formula_image(tmp_path),
        logical_name="contract-formula.png",
        media_type="image/png",
    )
    materials.ingest(
        material_id="material-image",
        run_id="run-1",
        material_kind="IMAGE",
        original=original,
        now=NOW,
    )
    tool_run_id, attempt_id = add_run(db, jobs, runs, original)
    machine = materials.extract(
        extraction_id="ocr-machine",
        material_id="material-image",
        profile_id="image_tesseract_v1",
        tool_run_id=tool_run_id,
        attempt_id=attempt_id,
        now=NOW,
    )
    machine_anchor = next(
        anchor for anchor in materials.anchors(machine.extraction_id)
        if anchor.anchor_kind == "FORMULA"
    )
    revised = materials.revise(
        extraction_id="ocr-human-revision",
        supersedes_extraction_id=machine.extraction_id,
        corrected_text="∀ ε > 0, ∃ δ > 0; ∫₀¹ x² dx = 1/3.\n",
        reason="OCR lost quantifiers, powers, and integral bounds",
        revised_by="reviewer",
        now="2026-08-14T00:05:00Z",
    )
    revised_anchor = next(
        anchor for anchor in materials.anchors(revised.extraction_id)
        if anchor.anchor_kind == "FORMULA"
    )
    return db, materials, machine_anchor, revised_anchor


def content(*, domain: str = "real numbers") -> ContractContent:
    return ContractContent(
        objective="prove non-negativity of the square",
        domain=domain,
        quantifiers=("for every real x",),
        boundary_conditions=("x is finite",),
        exact_negation="there exists a real x with x squared below zero",
        allowed_tools=("symbolic algebra",),
        success_criteria=("a checked proof closes the exact negation",),
    )


def service_for(
    db: Path,
    materials: MaterialStore,
    engine: AuthorityInvalidationEngine,
) -> tuple[ContractStore, ContractMaterialService]:
    contracts = ContractStore(db)
    return contracts, ContractMaterialService(
        db_path=db,
        contracts=contracts,
        materials=materials,
        invalidation=engine,
    )


def add_and_accept(
    service: ContractMaterialService,
    *,
    reference_id: str,
    contract_id: str,
    field_path: str,
    anchor: MaterialAnchor,
) -> None:
    service.add_reference(
        reference_id=reference_id,
        contract_id=contract_id,
        field_path=field_path,
        anchor_id=anchor.anchor_id,
        now=NOW,
    )
    service.accept_reference_by_user(
        reference_id,
        accepted_by="researcher",
        actor_kind="USER",
        now=NOW,
    )


def create_confirmed(
    contracts: ContractStore,
    service: ContractMaterialService,
    machine_anchor: MaterialAnchor,
    revised_anchor: MaterialAnchor,
    *,
    contract_id: str,
) -> None:
    contracts.create_draft(
        contract_id=contract_id,
        run_id="run-1",
        content=content(),
        ambiguities=(),
        now=NOW,
    )
    add_and_accept(
        service,
        reference_id=f"{contract_id}-domain-ref",
        contract_id=contract_id,
        field_path="$.domain",
        anchor=machine_anchor,
    )
    add_and_accept(
        service,
        reference_id=f"{contract_id}-objective-ref",
        contract_id=contract_id,
        field_path="$.objective",
        anchor=revised_anchor,
    )
    contracts.confirm_by_user(
        contract_id,
        confirmed_by="researcher",
        actor_kind="USER",
        now=NOW,
    )


def register_dependencies(
    service: ContractMaterialService,
    engine: AuthorityInvalidationEngine,
    *,
    contract_id: str,
    prefix: str,
) -> tuple[str, str]:
    affected = f"{prefix}-domain-review"
    sibling = f"{prefix}-objective-review"
    engine.register_binding(
        object_kind=AuthorityObjectKind.REVIEW,
        object_id=affected,
        run_id="run-1",
        contract_version=1,
        bound_revision=4,
        stable_label=f"label-{affected}",
        object_digest="c" * 64,
    )
    engine.register_binding(
        object_kind=AuthorityObjectKind.REVIEW,
        object_id=sibling,
        run_id="run-1",
        contract_version=1,
        bound_revision=4,
        stable_label=f"label-{sibling}",
        object_digest="d" * 64,
    )
    service.register_dependency(
        contract_id=contract_id,
        field_path="$.domain",
        object_kind=AuthorityObjectKind.REVIEW,
        object_id=affected,
        reopened_obligation_id=f"{prefix}-domain-obligation",
    )
    service.register_dependency(
        contract_id=contract_id,
        field_path="$.objective",
        object_kind=AuthorityObjectKind.REVIEW,
        object_id=sibling,
        reopened_obligation_id=f"{prefix}-objective-obligation",
    )
    return affected, sibling


def test_ambiguity_and_ocr_anchor_require_explicit_user_decisions(tmp_path: Path) -> None:
    db, materials, machine_anchor, revised_anchor = setup_real_material(tmp_path)
    engine = AuthorityInvalidationEngine(db, lambda: NOW)
    contracts, service = service_for(db, materials, engine)
    draft = contracts.create_draft(
        contract_id="contract-ambiguous",
        run_id="run-1",
        content=content(),
        ambiguities=(
            AmbiguitySpec(
                "ambiguity-domain",
                "$.domain",
                "Does the statement quantify over reals or complexes?",
                ("real numbers", "complex numbers"),
            ),
        ),
        now=NOW,
    )
    assert contracts.create_draft(
        contract_id="contract-ambiguous",
        run_id="run-1",
        content=content(),
        ambiguities=(
            AmbiguitySpec(
                "ambiguity-domain",
                "$.domain",
                "Does the statement quantify over reals or complexes?",
                ("real numbers", "complex numbers"),
            ),
        ),
        now="retry-at-a-later-time",
    ) == draft
    machine_ref = service.add_reference(
        reference_id="machine-ocr-ref",
        contract_id="contract-ambiguous",
        field_path="$.domain",
        anchor_id=machine_anchor.anchor_id,
        now=NOW,
    )
    revised_ref = service.add_reference(
        reference_id="human-revision-ref",
        contract_id="contract-ambiguous",
        field_path="$.objective",
        anchor_id=revised_anchor.anchor_id,
        now=NOW,
    )
    assert machine_ref.anchor_kind == machine_anchor.anchor_kind
    assert machine_ref.excerpt_digest == machine_anchor.excerpt_digest
    assert revised_ref.excerpt_digest == revised_anchor.excerpt_digest

    with pytest.raises(ContractError, match="only a user"):
        contracts.resolve_ambiguity_by_user(
            "ambiguity-domain",
            selected_option="real numbers",
            resolved_by="model-agent",
            actor_kind="MODEL",
            now=NOW,
        )
    with pytest.raises(ContractError, match="frozen alternatives"):
        contracts.resolve_ambiguity_by_user(
            "ambiguity-domain",
            selected_option="rational numbers",
            resolved_by="researcher",
            actor_kind="USER",
            now=NOW,
        )
    assert contracts.get("contract-ambiguous").state == "AMBIGUOUS"
    contracts.resolve_ambiguity_by_user(
        "ambiguity-domain",
        selected_option="real numbers",
        resolved_by="researcher",
        actor_kind="USER",
        now=NOW,
    )
    with pytest.raises(ContractMaterialError, match="only a user"):
        service.accept_reference_by_user(
            machine_ref.reference_id,
            accepted_by="model-agent",
            actor_kind="MODEL",
            now=NOW,
        )
    with pytest.raises(ContractError, match="user-accepted material"):
        contracts.confirm_by_user(
            "contract-ambiguous",
            confirmed_by="researcher",
            actor_kind="USER",
            now=NOW,
        )
    for reference in (machine_ref, revised_ref):
        service.accept_reference_by_user(
            reference.reference_id,
            accepted_by="researcher",
            actor_kind="USER",
            now=NOW,
        )
    with pytest.raises(ContractError, match="only a user"):
        contracts.confirm_by_user(
            "contract-ambiguous",
            confirmed_by="model-agent",
            actor_kind="MODEL",
            now=NOW,
        )
    confirmed = contracts.confirm_by_user(
        "contract-ambiguous",
        confirmed_by="researcher",
        actor_kind="USER",
        now=NOW,
    )
    assert confirmed.state == "CONFIRMED"
    ambiguity = contracts.ambiguities("contract-ambiguous", 1)[0]
    assert ambiguity.selected_option == "real numbers"
    assert ambiguity.resolved_by == "researcher"


def test_local_revision_invalidates_only_affected_b11a_objects(tmp_path: Path) -> None:
    db, materials, machine_anchor, revised_anchor = setup_real_material(tmp_path)
    engine = AuthorityInvalidationEngine(db, lambda: NOW)
    contracts, service = service_for(db, materials, engine)
    create_confirmed(
        contracts,
        service,
        machine_anchor,
        revised_anchor,
        contract_id="contract-local",
    )
    affected, sibling = register_dependencies(
        service, engine, contract_id="contract-local", prefix="local"
    )
    preview = service.preview_revision(
        preview_id="preview-local",
        contract_id="contract-local",
        proposed_content=content(domain="nonzero real numbers"),
        now=NOW,
    )
    assert preview.changed_fields == ("$.domain",)
    assert [item["object_id"] for item in preview.affected_objects] == [affected]
    assert preview.preserved_sibling_ids == (sibling,)
    assert preview.reopened_obligation_ids == ("local-domain-obligation",)
    with pytest.raises(ContractMaterialConflict, match="digest"):
        service.apply_revision(
            preview_id=preview.preview_id,
            preview_digest="0" * 64,
            kernel_event_id="event-local",
            research_revision=5,
            now=NOW,
            actor_kind="USER",
            revised_by="researcher",
        )
    with pytest.raises(ContractMaterialError, match="only a user"):
        service.apply_revision(
            preview_id=preview.preview_id,
            preview_digest=preview.preview_digest,
            kernel_event_id="event-local",
            research_revision=5,
            now=NOW,
            actor_kind="MODEL",
            revised_by="model-agent",
        )
    revised = service.apply_revision(
        preview_id=preview.preview_id,
        preview_digest=preview.preview_digest,
        kernel_event_id="event-local",
        research_revision=5,
        now=NOW,
        actor_kind="USER",
        revised_by="researcher",
    )
    assert revised.version == 2
    assert revised.state == "DRAFT"
    assert revised.supersedes_version == 1
    assert contracts.get("contract-local", 1).state == "SUPERSEDED"
    assert engine.get_binding(AuthorityObjectKind.REVIEW, affected).state == "INVALIDATED"
    assert engine.get_binding(AuthorityObjectKind.REVIEW, sibling).state == "VALID"
    with sqlite3.connect(db) as connection:
        fields = connection.execute(
            "SELECT field_path FROM product_contract_material_references "
            "WHERE contract_id='contract-local' AND contract_version=2 ORDER BY field_path"
        ).fetchall()
    assert fields == [("$.objective",)]
    with pytest.raises(ContractError, match="revised field"):
        contracts.confirm_by_user(
            "contract-local",
            confirmed_by="researcher",
            actor_kind="USER",
            now=NOW,
        )
    add_and_accept(
        service,
        reference_id="contract-local-revised-domain-ref",
        contract_id="contract-local",
        field_path="$.domain",
        anchor=revised_anchor,
    )
    assert contracts.confirm_by_user(
        "contract-local",
        confirmed_by="researcher",
        actor_kind="USER",
        now=NOW,
    ).state == "CONFIRMED"


def test_commit_then_crash_resumes_real_b11a_invalidation(tmp_path: Path) -> None:
    db, materials, machine_anchor, revised_anchor = setup_real_material(tmp_path)

    class CrashAfterLedger(RuntimeError):
        pass

    def crash(phase: str, _event: object) -> None:
        assert phase == "AFTER_LEDGER_COMMIT"
        raise CrashAfterLedger

    crashing_engine = AuthorityInvalidationEngine(db, lambda: NOW, fault_hook=crash)
    contracts, service = service_for(db, materials, crashing_engine)
    create_confirmed(
        contracts,
        service,
        machine_anchor,
        revised_anchor,
        contract_id="contract-crash",
    )
    affected, sibling = register_dependencies(
        service, crashing_engine, contract_id="contract-crash", prefix="crash"
    )
    preview = service.preview_revision(
        preview_id="preview-crash",
        contract_id="contract-crash",
        proposed_content=content(domain="positive real numbers"),
        now=NOW,
    )
    with pytest.raises(CrashAfterLedger):
        service.apply_revision(
            preview_id=preview.preview_id,
            preview_digest=preview.preview_digest,
            kernel_event_id="event-crash",
            research_revision=5,
            now=NOW,
            actor_kind="USER",
            revised_by="researcher",
        )
    assert contracts.get("contract-crash").version == 1
    assert contracts.get("contract-crash", 1).state == "CONFIRMED"
    assert contracts.get("contract-crash", 2).state == "PENDING_INVALIDATION"
    assert service.get_preview(preview.preview_id).state == "APPLYING"
    assert crashing_engine.get_binding(AuthorityObjectKind.REVIEW, affected).state == "VALID"

    restarted_engine = AuthorityInvalidationEngine(db, lambda: "2026-08-14T00:10:00Z")
    restarted_contracts, restarted = service_for(db, materials, restarted_engine)
    assert restarted.resume_pending(now="2026-08-14T00:10:00Z") == (preview.preview_id,)
    assert restarted.resume_pending(now="2026-08-14T00:11:00Z") == ()
    assert restarted_contracts.get("contract-crash").version == 2
    assert restarted_contracts.get("contract-crash", 1).state == "SUPERSEDED"
    assert restarted_contracts.get("contract-crash", 2).state == "DRAFT"
    assert restarted_engine.get_binding(AuthorityObjectKind.REVIEW, affected).state == "INVALIDATED"
    assert restarted_engine.get_binding(AuthorityObjectKind.REVIEW, sibling).state == "VALID"
    assert restarted.get_preview(preview.preview_id).state == "APPLIED"