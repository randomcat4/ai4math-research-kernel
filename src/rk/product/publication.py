"""Deterministic publication artifacts over B15a authority and B04/B05 stores."""

from __future__ import annotations

import hashlib
import os
import sqlite3
import subprocess
import tempfile
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

from rk.cas import ContentAddressedStore
from rk.domain import ArtifactRef
from rk.paper import VerifiedPaper
from rk.product.artifact_read import ArtifactRangeResult, ArtifactReadService, ExactArtifactRef
from rk.product.artifact_upload import ArtifactRegistry
from rk.product.log_tail import PublicLogStore
from rk.product.reviews import (
    ReviewBinding,
    ReviewTask,
    ReviewTaskStatus,
    ReviewTaskStore,
    ReviewType,
)
from rk.runtime import format_utc
from rk.wire import canonical_json_bytes


class PublicationArtifactError(RuntimeError):
    """A rendered, reviewed, compiled, or visible publication binding is invalid."""


class CandidateAccessDenied(PermissionError):
    """Candidate TeX is restricted to its exact PAPER_REVIEWER task."""


@dataclass(frozen=True, slots=True)
class CandidateRender:
    render_request_id: str
    run_id: str
    finalized_revision: int
    abstract_digest: str
    candidate_tex_ref: ExactArtifactRef


@dataclass(frozen=True, slots=True)
class CompilationResult:
    compilation_attempt_id: str
    candidate_tex_sha256: str
    pdf_ref: ExactArtifactRef
    stdout_log_artifact_id: str
    stderr_log_artifact_id: str


@dataclass(frozen=True, slots=True)
class PublishedPaperSummary:
    run_id: str
    final_outcome: str | None
    finalized_revision: int | None
    final_pdf_ref: ExactArtifactRef | None


class PublicationArtifactService:
    def __init__(
        self,
        *,
        db_path: Path,
        cas: ContentAddressedStore,
        registry: ArtifactRegistry,
        artifacts: ArtifactReadService,
        review_tasks: ReviewTaskStore,
        logs: PublicLogStore,
        id_generator: Callable[[], str],
        clock: Callable[[], datetime],
        compiler: str = "pdflatex",
        compile_timeout_seconds: int = 120,
        busy_timeout_ms: int = 5_000,
    ) -> None:
        if not compiler or compile_timeout_seconds <= 0:
            raise ValueError("compiler configuration is invalid")
        self._db_path = Path(db_path)
        self._cas = cas
        self._registry = registry
        self._artifacts = artifacts
        self._tasks = review_tasks
        self._logs = logs
        self._ids = id_generator
        self._clock = clock
        self._compiler = compiler
        self._timeout = compile_timeout_seconds
        self._busy_timeout_ms = busy_timeout_ms

    def render_candidate(
        self,
        *,
        render_request_id: str,
        run_id: str,
        finalized_snapshot: Mapping[str, Any],
        abstract: str,
    ) -> CandidateRender:
        if not render_request_id or not run_id or not abstract.strip():
            raise ValueError("render request, run, and abstract are required")
        finalization = self._finalization(run_id)
        self._assert_snapshot(finalized_snapshot, finalization)
        snapshot_digest = hashlib.sha256(canonical_json_bytes(finalized_snapshot)).hexdigest()
        abstract_text = " ".join(abstract.split())
        abstract_digest = hashlib.sha256(abstract_text.encode("utf-8")).hexdigest()
        existing = self._render(render_request_id, required=False)
        if existing is not None:
            if (
                existing["run_id"] != run_id
                or existing["finalized_snapshot_digest"] != snapshot_digest
                or existing["abstract_digest"] != abstract_digest
            ):
                raise PublicationArtifactError("render request identity was reused with drift")
            return CandidateRender(
                render_request_id,
                run_id,
                int(existing["finalized_revision"]),
                abstract_digest,
                self._exact_ref(existing, "candidate_tex"),
            )
        paper = VerifiedPaper().build(finalized_snapshot, str(finalization["terminal_root_id"]))
        tex = _insert_abstract(paper.tex, abstract_text)
        artifact = self._commit_bytes(
            tex,
            media_type="application/x-tex",
            source_name=f"{render_request_id}.tex",
        )
        now = format_utc(self._clock())
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO product_candidate_renders("
                "render_request_id,run_id,finalized_revision,terminal_root_id,"
                "terminal_root_digest,closure_witness_id,dependency_closure_digest,"
                "finalized_snapshot_digest,abstract_digest,candidate_tex_artifact_id,"
                "candidate_tex_sha256,candidate_tex_byte_count,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    render_request_id,
                    run_id,
                    finalization["finalized_revision"],
                    finalization["terminal_root_id"],
                    finalization["terminal_root_digest"],
                    finalization["closure_witness_id"],
                    finalization["dependency_closure_digest"],
                    snapshot_digest,
                    abstract_digest,
                    artifact.artifact_id,
                    artifact.sha256,
                    artifact.byte_count,
                    now,
                ),
            )
        return CandidateRender(
            render_request_id,
            run_id,
            int(finalization["finalized_revision"]),
            abstract_digest,
            ExactArtifactRef(
                artifact.artifact_id,
                artifact.sha256,
                artifact.byte_count,
                artifact.media_type,
            ),
        )

    def create_paper_review_task(
        self,
        *,
        generation_command_id: str,
        review_task_id: str,
        assignee_identity_id: str,
        author_subject_ids: tuple[str, ...],
        created_at: str,
        expires_at: str,
    ) -> ReviewTask:
        candidate = self._candidate(generation_command_id)
        render = self._render_for_candidate(candidate)
        task = self._tasks.create(
            review_task_id=review_task_id,
            review_type=ReviewType.PAPER,
            binding=ReviewBinding(
                run_id=str(candidate["run_id"]),
                kernel_revision=int(candidate["finalized_revision"]),
                contract_version=int(candidate["contract_version"]),
                target_id=generation_command_id,
                target_digest=str(candidate["candidate_tex_sha256"]),
                candidate_tex_artifact_id=str(candidate["candidate_tex_artifact_id"]),
                terminal_root_digest=str(candidate["terminal_root_digest"]),
                dependency_closure_digest=str(candidate["dependency_closure_digest"]),
            ),
            author_subject_ids=author_subject_ids,
            assignee_identity_id=assignee_identity_id,
            created_at=created_at,
            expires_at=expires_at,
        )
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO product_publication_review_bindings("
                "generation_command_id,review_task_id,render_request_id,"
                "candidate_tex_sha256,abstract_digest,created_at) VALUES(?,?,?,?,?,?)",
                (
                    generation_command_id,
                    review_task_id,
                    render["render_request_id"],
                    candidate["candidate_tex_sha256"],
                    render["abstract_digest"],
                    created_at,
                ),
            )
        return task

    def open_candidate_for_review(
        self,
        generation_command_id: str,
        *,
        identity_id: str,
        subject_role: str,
        range_header: str | None = None,
    ) -> ArtifactRangeResult:
        if subject_role != "PAPER_REVIEWER":
            raise CandidateAccessDenied("candidate TeX requires PAPER_REVIEWER")
        candidate = self._candidate(generation_command_id)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT review_task_id FROM product_publication_review_bindings "
                "WHERE generation_command_id=?",
                (generation_command_id,),
            ).fetchone()
        if row is None:
            raise CandidateAccessDenied("candidate has no exact review task")
        task = self._tasks.get(str(row[0]))
        if task.assignee_identity_id != identity_id or task.status not in {
            ReviewTaskStatus.CLAIMED,
            ReviewTaskStatus.SUBMITTED,
        }:
            raise CandidateAccessDenied("identity is not the claimed exact reviewer")
        ref = ExactArtifactRef(
            str(candidate["candidate_tex_artifact_id"]),
            str(candidate["candidate_tex_sha256"]),
            int(candidate["candidate_tex_byte_count"]),
            str(candidate["candidate_tex_media_type"]),
        )
        return self._artifacts.open_range(
            ref.artifact_id,
            range_header=range_header,
            expected_ref=ref,
        )

    def compile_reviewed(
        self,
        *,
        generation_command_id: str,
        paper_review_id: str,
    ) -> CompilationResult:
        candidate = self._candidate(generation_command_id)
        review, binding, render = self._accepted_review(generation_command_id, paper_review_id)
        tex_ref = self._exact_ref(candidate, "candidate_tex")
        if (
            str(review["candidate_tex_sha256"]) != tex_ref.sha256
            or str(binding["candidate_tex_sha256"]) != tex_ref.sha256
        ):
            raise PublicationArtifactError("review and task do not bind the candidate digest")
        raw = b"".join(self._artifacts.open_range(tex_ref.artifact_id, expected_ref=tex_ref).stream)
        attempt_id = self._ids()
        stdout = self._logs.create(
            scope_kind="RUN",
            scope_id=str(candidate["run_id"]),
            producer_run_id=attempt_id,
            stream="STDOUT",
            logical_name=f"{attempt_id}.stdout.log",
        )
        stderr = self._logs.create(
            scope_kind="RUN",
            scope_id=str(candidate["run_id"]),
            producer_run_id=attempt_id,
            stream="STDERR",
            logical_name=f"{attempt_id}.stderr.log",
        )
        with tempfile.TemporaryDirectory(prefix="rk-publication-") as directory:
            root = Path(directory)
            source = root / "main.tex"
            with source.open("xb") as writer:
                writer.write(raw)
                writer.flush()
                os.fsync(writer.fileno())
            completed = subprocess.run(
                [self._compiler, "-interaction=nonstopmode", "-halt-on-error", "main.tex"],
                cwd=root,
                capture_output=True,
                timeout=self._timeout,
                check=False,
            )
            self._append_log(stdout.log_id, completed.stdout)
            self._append_log(stderr.log_id, completed.stderr)
            stdout_ref = self._logs.seal(stdout.log_id)
            stderr_ref = self._logs.seal(stderr.log_id)
            pdf_path = root / "main.pdf"
            if completed.returncode != 0 or not pdf_path.is_file():
                self._record_attempt(
                    attempt_id,
                    candidate,
                    paper_review_id,
                    render,
                    stdout_ref,
                    stderr_ref,
                    outcome="FAILED",
                    failure_code=f"COMPILER_EXIT_{completed.returncode}",
                )
                raise PublicationArtifactError(f"compilation failed in attempt {attempt_id}")
            pdf = pdf_path.read_bytes()
        if not pdf.startswith(b"%PDF-"):
            raise PublicationArtifactError("compiler output is not a PDF")
        pdf_ref = self._commit_bytes(
            pdf,
            media_type="application/pdf",
            source_name=f"{attempt_id}.pdf",
        )
        self._record_attempt(
            attempt_id,
            candidate,
            paper_review_id,
            render,
            stdout_ref,
            stderr_ref,
            outcome="SUCCEEDED",
            pdf_ref=pdf_ref,
        )
        return CompilationResult(
            attempt_id,
            tex_ref.sha256,
            ExactArtifactRef(
                pdf_ref.artifact_id,
                pdf_ref.sha256,
                pdf_ref.byte_count,
                pdf_ref.media_type,
            ),
            stdout_ref.artifact_id,
            stderr_ref.artifact_id,
        )

    def homepage(self, run_id: str) -> PublishedPaperSummary:
        with self._connect() as connection:
            finalization = connection.execute(
                "SELECT final_outcome,finalized_revision FROM "
                "product_publication_finalizations WHERE run_id=?",
                (run_id,),
            ).fetchone()
            publication = connection.execute(
                "SELECT pc.final_pdf_artifact_id,pc.final_pdf_sha256,"
                "a.final_pdf_artifact_id FROM product_publication_compilations pc "
                "JOIN product_compilation_attempts a "
                "ON a.paper_review_id=pc.paper_review_id "
                "AND a.final_pdf_sha256=pc.final_pdf_sha256 "
                "WHERE pc.run_id=? AND a.outcome='SUCCEEDED' "
                "ORDER BY pc.publication_revision DESC LIMIT 1",
                (run_id,),
            ).fetchone()
        final_pdf = None
        if publication is not None:
            row = self._artifact_row(str(publication[0]))
            final_pdf = self._exact_ref(row, "")
        return PublishedPaperSummary(
            run_id,
            str(finalization[0]) if finalization is not None else None,
            int(finalization[1]) if finalization is not None else None,
            final_pdf,
        )

    def _accepted_review(
        self, generation_id: str, review_id: str
    ) -> tuple[dict[str, Any], dict[str, Any], dict[str, Any]]:
        with self._connect() as connection:
            review = connection.execute(
                "SELECT generation_command_id,candidate_tex_artifact_id,"
                "candidate_tex_sha256,signed_review_artifact_id,signed_review_sha256,"
                "verdict FROM product_publication_reviews WHERE paper_review_id=?",
                (review_id,),
            ).fetchone()
            binding = connection.execute(
                "SELECT review_task_id,candidate_tex_sha256,abstract_digest "
                "FROM product_publication_review_bindings WHERE generation_command_id=?",
                (generation_id,),
            ).fetchone()
        if (
            review is None
            or binding is None
            or str(review[0]) != generation_id
            or str(review[5]) != "ACCEPT"
        ):
            raise PublicationArtifactError("exact accepted paper review is unavailable")
        task = self._tasks.get(str(binding[0]))
        if (
            task.status is not ReviewTaskStatus.SUBMITTED
            or task.signed_artifact_ref is None
            or task.signed_artifact_ref.artifact_id != str(review[3])
            or task.signed_artifact_ref.sha256 != str(review[4])
        ):
            raise PublicationArtifactError("B05 signed task does not match B15 review")
        return (
            dict(
                zip(
                    (
                        "generation_command_id",
                        "candidate_tex_artifact_id",
                        "candidate_tex_sha256",
                        "signed_review_artifact_id",
                        "signed_review_sha256",
                        "verdict",
                    ),
                    tuple(review),
                    strict=True,
                )
            ),
            {
                "review_task_id": binding[0],
                "candidate_tex_sha256": binding[1],
                "abstract_digest": binding[2],
            },
            self._render_for_candidate(self._candidate(generation_id)),
        )

    def _append_log(self, log_id: str, data: bytes) -> None:
        offset = 0
        for start in range(0, len(data), 32 * 1024):
            chunk = data[start : start + 32 * 1024]
            self._logs.append(
                log_id,
                offset=offset,
                data=chunk,
                transfer_sha256=hashlib.sha256(chunk).hexdigest(),
            )
            offset += len(chunk)

    def _record_attempt(
        self,
        attempt_id: str,
        candidate: Mapping[str, Any],
        review_id: str,
        render: Mapping[str, Any],
        stdout: ArtifactRef,
        stderr: ArtifactRef,
        *,
        outcome: str,
        failure_code: str | None = None,
        pdf_ref: ArtifactRef | None = None,
    ) -> None:
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO product_compilation_attempts("
                "compilation_attempt_id,run_id,generation_command_id,paper_review_id,"
                "candidate_tex_sha256,abstract_digest,compiler_profile,outcome,"
                "stdout_log_id,stderr_log_id,stdout_log_artifact_id,"
                "stderr_log_artifact_id,final_pdf_artifact_id,final_pdf_sha256,"
                "failure_code,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    attempt_id,
                    candidate["run_id"],
                    candidate["generation_command_id"],
                    review_id,
                    candidate["candidate_tex_sha256"],
                    render["abstract_digest"],
                    self._compiler,
                    outcome,
                    self._log_id(stdout.artifact_id),
                    self._log_id(stderr.artifact_id),
                    stdout.artifact_id,
                    stderr.artifact_id,
                    pdf_ref.artifact_id if pdf_ref else None,
                    pdf_ref.sha256 if pdf_ref else None,
                    failure_code,
                    format_utc(self._clock()),
                ),
            )

    def _log_id(self, artifact_id: str) -> str:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT log_id FROM product_public_logs WHERE artifact_id=?", (artifact_id,)
            ).fetchone()
        if row is None:
            raise PublicationArtifactError("sealed compilation log is unavailable")
        return str(row[0])

    def _commit_bytes(self, data: bytes, *, media_type: str, source_name: str) -> ArtifactRef:
        staged = self._cas.stage_bytes(data, media_type=media_type, source_name=source_name)
        committed = self._cas.commit(staged, now=self._clock())
        return self._registry.register(committed)

    def _finalization(self, run_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT run_id,finalized_revision,contract_version,final_outcome,"
                "terminal_root_id,terminal_root_digest,closure_witness_id,"
                "dependency_closure_digest FROM product_publication_finalizations "
                "WHERE run_id=?",
                (run_id,),
            ).fetchone()
        if row is None or str(row[3]) not in {"PROVED", "DISPROVED"}:
            raise PublicationArtifactError("run is not authority-finalized")
        names = (
            "run_id",
            "finalized_revision",
            "contract_version",
            "final_outcome",
            "terminal_root_id",
            "terminal_root_digest",
            "closure_witness_id",
            "dependency_closure_digest",
        )
        return dict(zip(names, tuple(row), strict=True))

    def _candidate(self, generation_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT generation_command_id,run_id,finalized_revision,contract_version,"
                "terminal_root_id,terminal_root_digest,closure_witness_id,"
                "dependency_closure_digest,candidate_tex_artifact_id,candidate_tex_sha256,"
                "candidate_tex_byte_count,candidate_tex_media_type,generated_by_subject_id "
                "FROM product_publication_candidates WHERE generation_command_id=?",
                (generation_id,),
            ).fetchone()
        if row is None:
            raise PublicationArtifactError("candidate command is unavailable")
        names = (
            "generation_command_id",
            "run_id",
            "finalized_revision",
            "contract_version",
            "terminal_root_id",
            "terminal_root_digest",
            "closure_witness_id",
            "dependency_closure_digest",
            "candidate_tex_artifact_id",
            "candidate_tex_sha256",
            "candidate_tex_byte_count",
            "candidate_tex_media_type",
            "generated_by_subject_id",
        )
        return dict(zip(names, tuple(row), strict=True))

    def _render(self, request_id: str, *, required: bool = True) -> dict[str, Any] | None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT render_request_id,run_id,finalized_revision,"
                "finalized_snapshot_digest,abstract_digest,candidate_tex_artifact_id,"
                "candidate_tex_sha256,candidate_tex_byte_count "
                "FROM product_candidate_renders WHERE render_request_id=?",
                (request_id,),
            ).fetchone()
        if row is None:
            if required:
                raise PublicationArtifactError("candidate render is unavailable")
            return None
        names = (
            "render_request_id",
            "run_id",
            "finalized_revision",
            "finalized_snapshot_digest",
            "abstract_digest",
            "candidate_tex_artifact_id",
            "candidate_tex_sha256",
            "candidate_tex_byte_count",
        )
        return dict(zip(names, tuple(row), strict=True))

    def _render_for_candidate(self, candidate: Mapping[str, Any]) -> dict[str, Any]:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT render_request_id,abstract_digest,candidate_tex_sha256 "
                "FROM product_candidate_renders WHERE run_id=? "
                "AND candidate_tex_artifact_id=? AND candidate_tex_sha256=?",
                (
                    candidate["run_id"],
                    candidate["candidate_tex_artifact_id"],
                    candidate["candidate_tex_sha256"],
                ),
            ).fetchone()
        if row is None:
            raise PublicationArtifactError("candidate has no deterministic render receipt")
        return {
            "render_request_id": row[0],
            "abstract_digest": row[1],
            "candidate_tex_sha256": row[2],
        }

    def _assert_snapshot(
        self, snapshot: Mapping[str, Any], finalization: Mapping[str, Any]
    ) -> None:
        if (
            snapshot.get("status") != "CLOSED"
            or snapshot.get("revision") != finalization["finalized_revision"]
            or snapshot.get("final_outcome") != finalization["final_outcome"]
            or snapshot.get("root_claim_id") != finalization["terminal_root_id"]
            or list(snapshot.get("terminal_claim_ids", ())) != [finalization["terminal_root_id"]]
        ):
            raise PublicationArtifactError("snapshot does not match finalization")

    def _artifact_row(self, artifact_id: str) -> dict[str, Any]:
        descriptor = self._artifacts.describe(artifact_id)
        return {
            "artifact_id": descriptor.ref.artifact_id,
            "sha256": descriptor.ref.sha256,
            "byte_count": descriptor.ref.byte_count,
            "media_type": descriptor.ref.media_type,
        }

    @staticmethod
    def _exact_ref(value: Mapping[str, Any], prefix: str) -> ExactArtifactRef:
        stem = f"{prefix}_" if prefix else ""
        return ExactArtifactRef(
            str(value[f"{stem}artifact_id"]),
            str(value[f"{stem}sha256"]),
            int(value[f"{stem}byte_count"]),
            str(value.get(f"{stem}media_type", "application/x-tex")),
        )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._db_path, timeout=self._busy_timeout_ms / 1_000)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection


def _insert_abstract(tex: bytes, abstract: str) -> bytes:
    marker = b"\\maketitle\n"
    if tex.count(marker) != 1:
        raise PublicationArtifactError("deterministic TeX has no unique title marker")
    escaped = "".join(
        {"&": r"\&", "%": r"\%", "#": r"\#", "_": r"\_", "{": r"\{", "}": r"\}"}.get(
            character, character
        )
        for character in abstract
    )
    block = f"\\begin{{abstract}}\n{escaped}\n\\end{{abstract}}\n".encode()
    return tex.replace(marker, marker + block)


__all__ = [
    "CandidateAccessDenied",
    "CandidateRender",
    "CompilationResult",
    "PublicationArtifactError",
    "PublicationArtifactService",
    "PublishedPaperSummary",
]
