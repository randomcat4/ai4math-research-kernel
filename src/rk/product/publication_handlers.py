"""S00 closure finalization and CLOSED-run publication projection handlers."""

from __future__ import annotations

import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Any

from rk.domain import Decision, MissingCondition, RejectionCode, frozen_mapping
from rk.extensions import ClosedRunPermission, ExtensionRegistry, ProductCommandContext
from rk.projector import ProjectionContext
from rk.sqlite import open_sqlite


class PublicationBindingError(ValueError):
    """A publication command is not bound to one exact authority chain."""


_FINALIZE_FIELDS = {"outcome", "terminal_claim_ids", "open_obligation_ids", "dossier_spec"}
_CHAIN_FIELDS = {
    "finalized_revision",
    "terminal_root_id",
    "terminal_root_digest",
    "closure_witness_id",
    "dependency_closure_digest",
    "candidate_tex_artifact",
}
_REVIEW_FIELDS = _CHAIN_FIELDS | {
    "generation_command_id",
    "paper_review_id",
    "signed_review_artifact",
    "paper_review_schema_version",
    "reviewer_subject_id",
    "verdict",
}
_COMPILE_FIELDS = _CHAIN_FIELDS | {
    "generation_command_id",
    "paper_review_id",
    "final_pdf_artifact",
    "compiler_profile_id",
    "compiler_profile_version",
}


@dataclass(frozen=True, slots=True)
class PublicationHandlers:
    db_path: Path
    busy_timeout_ms: int = 5_000

    def finalize(self, context: ProductCommandContext) -> Decision:
        if context.capability.subject_role != "MAIN":
            return _role_rejection("MAIN")
        try:
            mutation = self._finalization_mutation(context)
        except (KeyError, PublicationBindingError, TypeError, ValueError):
            return _reject(
                RejectionCode.TERMINAL_CLAIM_UNSUPPORTED,
                "UNIQUE_ROOT_CLOSURE",
                "/command/payload",
            )
        outcome = str(mutation["final_outcome"])
        return Decision(
            accepted=True,
            projection_mutations=(
                frozen_mapping({"op": "SET_RUN_STATUS", "status": "CLOSED", "outcome": outcome}),
                frozen_mapping(
                    {"op": "CREATE_DOSSIER", "spec": dict(context.command.payload["dossier_spec"])}
                ),
                frozen_mapping({"op": "B15_RECORD_FINALIZATION", **mutation}),
            ),
            event_intents=(
                frozen_mapping(
                    {
                        "type": "RUN_FINALIZED",
                        "command_type": "Finalize",
                        "terminal_root_id": mutation["terminal_root_id"],
                    }
                ),
            ),
        )

    def generate_candidate_tex(self, context: ProductCommandContext) -> Decision:
        if context.capability.subject_role != "PUBLICATION_WORKER":
            return _role_rejection("PUBLICATION_WORKER")
        try:
            payload = _exact_payload(context, _CHAIN_FIELDS)
            _assert_chain(payload, self._finalization(context.run_id))
            artifact = _artifact(
                payload["candidate_tex_artifact"],
                context.evidence_summary,
                media_type="application/x-tex",
            )
        except (KeyError, PublicationBindingError, TypeError, ValueError):
            return _binding_rejection()
        return _publication_decision(
            "B15_RECORD_CANDIDATE",
            "CANDIDATE_TEX_GENERATED",
            {
                **_chain_value(payload),
                "candidate_tex_artifact": artifact,
                "generated_by_subject_id": context.capability.subject_id,
            },
        )

    def submit_paper_review(self, context: ProductCommandContext) -> Decision:
        if context.capability.subject_role != "PAPER_REVIEWER":
            return _role_rejection("PAPER_REVIEWER")
        try:
            payload = _exact_payload(context, _REVIEW_FIELDS)
            if payload["reviewer_subject_id"] != context.capability.subject_id:
                raise PublicationBindingError("review signature subject does not match capability")
            candidate = self._candidate(_string(payload["generation_command_id"]))
            _assert_candidate_chain(payload, candidate)
            if candidate["generated_by_subject_id"] == context.capability.subject_id:
                raise PublicationBindingError("reviewer is not independent of generation")
            signed = _artifact(payload["signed_review_artifact"], context.evidence_summary)
            candidate_artifact = _full_artifact(payload["candidate_tex_artifact"])
            if (
                candidate_artifact["artifact_id"] != candidate["candidate_tex_artifact_id"]
                or candidate_artifact["sha256"] != candidate["candidate_tex_sha256"]
            ):
                raise PublicationBindingError("review names another TeX artifact")
            verdict = _enum(payload["verdict"], {"ACCEPT", "REJECT"})
        except (KeyError, PublicationBindingError, TypeError, ValueError):
            return _binding_rejection()
        return _publication_decision(
            "B15_RECORD_PAPER_REVIEW",
            "PAPER_REVIEW_RECORDED",
            {
                **_chain_value(payload),
                "generation_command_id": str(payload["generation_command_id"]),
                "paper_review_id": _string(payload["paper_review_id"]),
                "signed_review_artifact": signed,
                "paper_review_schema_version": _string(payload["paper_review_schema_version"]),
                "reviewer_subject_id": context.capability.subject_id,
                "verdict": verdict,
            },
        )

    def compile_reviewed_paper(self, context: ProductCommandContext) -> Decision:
        if context.capability.subject_role != "PUBLICATION_WORKER":
            return _role_rejection("PUBLICATION_WORKER")
        try:
            payload = _exact_payload(context, _COMPILE_FIELDS)
            candidate = self._candidate(_string(payload["generation_command_id"]))
            _assert_candidate_chain(payload, candidate)
            review = self._review(_string(payload["paper_review_id"]))
            if (
                review["generation_command_id"] != payload["generation_command_id"]
                or review["verdict"] != "ACCEPT"
                or review["candidate_tex_artifact_id"] != candidate["candidate_tex_artifact_id"]
                or review["candidate_tex_sha256"] != candidate["candidate_tex_sha256"]
            ):
                raise PublicationBindingError("review does not accept exact candidate")
            candidate_artifact = _full_artifact(payload["candidate_tex_artifact"])
            if (
                candidate_artifact["artifact_id"] != candidate["candidate_tex_artifact_id"]
                or candidate_artifact["sha256"] != candidate["candidate_tex_sha256"]
            ):
                raise PublicationBindingError("compile names another TeX artifact")
            final_pdf = _artifact(
                payload["final_pdf_artifact"],
                context.evidence_summary,
                media_type="application/pdf",
            )
        except (KeyError, PublicationBindingError, TypeError, ValueError):
            return _binding_rejection()
        return _publication_decision(
            "B15_RECORD_COMPILATION",
            "REVIEWED_PAPER_COMPILED",
            {
                **_chain_value(payload),
                "generation_command_id": str(payload["generation_command_id"]),
                "paper_review_id": str(payload["paper_review_id"]),
                "final_pdf_artifact": final_pdf,
                "compiled_by_subject_id": context.capability.subject_id,
                "compiler_profile_id": _string(payload["compiler_profile_id"]),
                "compiler_profile_version": _string(payload["compiler_profile_version"]),
            },
        )

    def apply_finalization(
        self,
        connection: sqlite3.Connection,
        context: ProjectionContext,
        mutation: Mapping[str, Any],
    ) -> None:
        expected = {
            "op",
            "finalized_revision",
            "contract_version",
            "final_outcome",
            "terminal_root_id",
            "terminal_root_digest",
            "closure_witness_id",
            "dependency_closure_digest",
        }
        if set(mutation) != expected or mutation["finalized_revision"] != context.revision:
            raise PublicationBindingError("finalization mutation is not revision bound")
        connection.execute(
            "INSERT INTO product_publication_finalizations("
            "run_id,finalized_revision,contract_version,final_outcome,terminal_root_id,"
            "terminal_root_digest,closure_witness_id,dependency_closure_digest,"
            "finalize_command_id,finalize_event_id,finalized_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (
                context.run_id,
                mutation["finalized_revision"],
                mutation["contract_version"],
                mutation["final_outcome"],
                mutation["terminal_root_id"],
                mutation["terminal_root_digest"],
                mutation["closure_witness_id"],
                mutation["dependency_closure_digest"],
                context.command_id,
                context.event_id,
                context.recorded_at,
            ),
        )

    def apply_candidate(
        self,
        connection: sqlite3.Connection,
        context: ProjectionContext,
        mutation: Mapping[str, Any],
    ) -> None:
        value = _mutation_value(mutation, "B15_RECORD_CANDIDATE")
        if context.command.type != "GenerateCandidateTex":
            raise PublicationBindingError("candidate command type is invalid")
        _assert_chain(value, _finalization_in(connection, context.run_id))
        artifact = _full_artifact(value["candidate_tex_artifact"])
        connection.execute(
            "INSERT INTO product_publication_candidates("
            "generation_command_id,generation_event_id,run_id,publication_revision,"
            "finalized_revision,contract_version,terminal_root_id,terminal_root_digest,"
            "closure_witness_id,dependency_closure_digest,candidate_tex_artifact_id,"
            "candidate_tex_sha256,candidate_tex_byte_count,candidate_tex_media_type,"
            "generated_by_subject_id,generated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                context.command_id,
                context.event_id,
                context.run_id,
                context.revision,
                value["finalized_revision"],
                context.contract_version,
                value["terminal_root_id"],
                value["terminal_root_digest"],
                value["closure_witness_id"],
                value["dependency_closure_digest"],
                artifact["artifact_id"],
                artifact["sha256"],
                artifact["byte_count"],
                artifact["media_type"],
                value["generated_by_subject_id"],
                context.recorded_at,
            ),
        )

    def apply_review(
        self,
        connection: sqlite3.Connection,
        context: ProjectionContext,
        mutation: Mapping[str, Any],
    ) -> None:
        value = _mutation_value(mutation, "B15_RECORD_PAPER_REVIEW")
        if context.command.type != "SubmitPaperReview":
            raise PublicationBindingError("review command type is invalid")
        candidate = _candidate_in(connection, str(value["generation_command_id"]))
        _assert_candidate_chain(value, candidate)
        tex = _full_artifact(value["candidate_tex_artifact"])
        signed = _full_artifact(value["signed_review_artifact"])
        connection.execute(
            "INSERT INTO product_publication_reviews("
            "paper_review_id,review_command_id,review_event_id,run_id,"
            "publication_revision,generation_command_id,finalized_revision,"
            "terminal_root_id,terminal_root_digest,closure_witness_id,"
            "dependency_closure_digest,candidate_tex_artifact_id,candidate_tex_sha256,"
            "signed_review_artifact_id,signed_review_sha256,reviewer_subject_id,"
            "paper_review_schema_version,verdict,reviewed_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                value["paper_review_id"],
                context.command_id,
                context.event_id,
                context.run_id,
                context.revision,
                value["generation_command_id"],
                value["finalized_revision"],
                value["terminal_root_id"],
                value["terminal_root_digest"],
                value["closure_witness_id"],
                value["dependency_closure_digest"],
                tex["artifact_id"],
                tex["sha256"],
                signed["artifact_id"],
                signed["sha256"],
                value["reviewer_subject_id"],
                value["paper_review_schema_version"],
                value["verdict"],
                context.recorded_at,
            ),
        )

    def apply_compilation(
        self,
        connection: sqlite3.Connection,
        context: ProjectionContext,
        mutation: Mapping[str, Any],
    ) -> None:
        value = _mutation_value(mutation, "B15_RECORD_COMPILATION")
        if context.command.type != "CompileReviewedPaper":
            raise PublicationBindingError("compile command type is invalid")
        candidate = _candidate_in(connection, str(value["generation_command_id"]))
        review = _review_in(connection, str(value["paper_review_id"]))
        if (
            review["verdict"] != "ACCEPT"
            or review["generation_command_id"] != value["generation_command_id"]
            or review["candidate_tex_sha256"] != candidate["candidate_tex_sha256"]
        ):
            raise PublicationBindingError("compile lacks exact accepted review")
        tex = _full_artifact(value["candidate_tex_artifact"])
        final_pdf = _full_artifact(value["final_pdf_artifact"])
        connection.execute(
            "INSERT INTO product_publication_compilations("
            "compile_command_id,compile_event_id,run_id,publication_revision,"
            "generation_command_id,paper_review_id,candidate_tex_artifact_id,"
            "candidate_tex_sha256,final_pdf_artifact_id,final_pdf_sha256,"
            "compiled_by_subject_id,compiler_profile_id,compiler_profile_version,compiled_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                context.command_id,
                context.event_id,
                context.run_id,
                context.revision,
                value["generation_command_id"],
                value["paper_review_id"],
                tex["artifact_id"],
                tex["sha256"],
                final_pdf["artifact_id"],
                final_pdf["sha256"],
                value["compiled_by_subject_id"],
                value["compiler_profile_id"],
                value["compiler_profile_version"],
                context.recorded_at,
            ),
        )

    def _finalization_mutation(self, context: ProductCommandContext) -> dict[str, Any]:
        payload = context.command.payload
        if set(payload) != _FINALIZE_FIELDS:
            raise PublicationBindingError("Finalize payload fields are not exact")
        outcome = _enum(payload["outcome"], {"PROVED", "DISPROVED", "UNRESOLVED"})
        terminals = _strings(payload["terminal_claim_ids"])
        declared_open = _strings(payload["open_obligation_ids"])
        actual_open = _strings(context.snapshot.get("open_obligation_ids", ()))
        if set(declared_open) != set(actual_open):
            raise PublicationBindingError("declared open obligations are stale")
        if outcome == "UNRESOLVED":
            if terminals:
                raise PublicationBindingError("UNRESOLVED cannot claim a terminal ROOT")
            return {
                "finalized_revision": context.revision + 1,
                "contract_version": context.contract_version,
                "final_outcome": outcome,
                "terminal_root_id": None,
                "terminal_root_digest": None,
                "closure_witness_id": None,
                "dependency_closure_digest": None,
            }
        if actual_open:
            raise PublicationBindingError("authority outcome has open obligations")
        active_roots = [
            claim
            for claim in _mappings(context.snapshot.get("claims"))
            if claim.get("claim_kind") == "ROOT"
            and _field(claim, "lifecycle_status", "lifecycle") == "ACTIVE"
            and int(claim.get("contract_version", -1)) == context.contract_version
        ]
        if len(active_roots) != 1:
            raise PublicationBindingError("Finalize requires exactly one active ROOT")
        root = active_roots[0]
        root_id = _string(root["claim_id"])
        if terminals != (root_id,) or not _root_supports(root, outcome):
            raise PublicationBindingError("ROOT does not support final outcome")
        witnesses = [
            witness
            for witness in _mappings(context.snapshot.get("closure_witnesses"))
            if witness.get("parent_claim_id") == root_id
            and witness.get("status") == "ACCEPTED"
            and int(witness.get("contract_version", -1)) == context.contract_version
        ]
        if len(witnesses) != 1:
            raise PublicationBindingError("ROOT requires one accepted ClosureWitness")
        witness = witnesses[0]
        return {
            "finalized_revision": context.revision + 1,
            "contract_version": context.contract_version,
            "final_outcome": outcome,
            "terminal_root_id": root_id,
            "terminal_root_digest": _digest(_field(root, "statement_hash", "statement_digest")),
            "closure_witness_id": _string(witness["witness_id"]),
            "dependency_closure_digest": _digest(witness["selected_subgraph_digest"]),
        }

    def _finalization(self, run_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            return _finalization_in(connection, run_id)

    def _candidate(self, command_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            return _candidate_in(connection, command_id)

    def _review(self, review_id: str) -> dict[str, Any]:
        with self._connect() as connection:
            return _review_in(connection, review_id)

    def _connect(self) -> sqlite3.Connection:
        connection = open_sqlite(self.db_path, timeout=self.busy_timeout_ms / 1_000)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        return connection


def register_publication_handlers(
    registry: ExtensionRegistry, handlers: PublicationHandlers
) -> ExtensionRegistry:
    return (
        registry.register_command_handler("Finalize", handlers.finalize)
        .register_command_handler("GenerateCandidateTex", handlers.generate_candidate_tex)
        .register_command_handler("SubmitPaperReview", handlers.submit_paper_review)
        .register_command_handler("CompileReviewedPaper", handlers.compile_reviewed_paper)
        .register_projection_mutation("B15_RECORD_FINALIZATION", handlers.apply_finalization)
        .register_projection_mutation("B15_RECORD_CANDIDATE", handlers.apply_candidate)
        .register_projection_mutation("B15_RECORD_PAPER_REVIEW", handlers.apply_review)
        .register_projection_mutation("B15_RECORD_COMPILATION", handlers.apply_compilation)
        .register_closed_run_permission(
            ClosedRunPermission("GenerateCandidateTex", frozenset({"PUBLICATION_WORKER"}))
        )
        .register_closed_run_permission(
            ClosedRunPermission("SubmitPaperReview", frozenset({"PAPER_REVIEWER"}))
        )
        .register_closed_run_permission(
            ClosedRunPermission("CompileReviewedPaper", frozenset({"PUBLICATION_WORKER"}))
        )
    )


def _publication_decision(opcode: str, event_type: str, value: Mapping[str, Any]) -> Decision:
    return Decision(
        accepted=True,
        projection_mutations=(frozen_mapping({"op": opcode, "value": dict(value)}),),
        event_intents=(
            frozen_mapping(
                {
                    "type": event_type,
                    "command_type": event_type,
                    "authority_effect": "PUBLICATION_PROJECTION_ONLY",
                }
            ),
        ),
    )


def _root_supports(root: Mapping[str, Any], outcome: str) -> bool:
    route = _field(root, "route_result", "route")
    closure = _field(root, "closure_state", "closure")
    if closure not in {"CLOSED_MACHINE", "CLOSED_HUMAN", "CLOSED_HYBRID"}:
        return False
    if outcome == "DISPROVED":
        return bool(
            route == "REFUTED" and _field(root, "semantic_verdict", "semantic") == "REFUTED"
        )
    machine = (
        _field(root, "machine_verdict", "machine") in {"KERNEL_VERIFIED", "CERTIFICATE_VERIFIED"}
        and _field(root, "semantic_verdict", "semantic") in {"TESTED", "HUMAN_ATTESTED"}
        and closure in {"CLOSED_MACHINE", "CLOSED_HYBRID"}
    )
    peer = (
        _field(root, "peer_verdict", "peer") == "ACCEPTED"
        and _field(root, "semantic_verdict", "semantic") == "HUMAN_ATTESTED"
        and closure in {"CLOSED_HUMAN", "CLOSED_HYBRID"}
    )
    return route == "ROUTE_PROVED" and (machine or peer)


def _assert_chain(value: Mapping[str, Any], finalization: Mapping[str, Any]) -> None:
    expected = {
        "finalized_revision": finalization["finalized_revision"],
        "terminal_root_id": finalization["terminal_root_id"],
        "terminal_root_digest": finalization["terminal_root_digest"],
        "closure_witness_id": finalization["closure_witness_id"],
        "dependency_closure_digest": finalization["dependency_closure_digest"],
    }
    if finalization["final_outcome"] not in {"PROVED", "DISPROVED"} or any(
        value.get(name) != expected_value for name, expected_value in expected.items()
    ):
        raise PublicationBindingError("command is not bound to finalization")


def _assert_candidate_chain(value: Mapping[str, Any], candidate: Mapping[str, Any]) -> None:
    expected = {
        "finalized_revision": candidate["finalized_revision"],
        "terminal_root_id": candidate["terminal_root_id"],
        "terminal_root_digest": candidate["terminal_root_digest"],
        "closure_witness_id": candidate["closure_witness_id"],
        "dependency_closure_digest": candidate["dependency_closure_digest"],
    }
    if any(value.get(name) != expected_value for name, expected_value in expected.items()):
        raise PublicationBindingError("command is not bound to candidate chain")


def _chain_value(payload: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "finalized_revision": _positive(payload["finalized_revision"]),
        "terminal_root_id": _string(payload["terminal_root_id"]),
        "terminal_root_digest": _digest(payload["terminal_root_digest"]),
        "closure_witness_id": _string(payload["closure_witness_id"]),
        "dependency_closure_digest": _digest(payload["dependency_closure_digest"]),
        "candidate_tex_artifact": _full_artifact(payload["candidate_tex_artifact"]),
    }


def _artifact(
    value: object, evidence_summary: Mapping[str, Any], *, media_type: str | None = None
) -> dict[str, Any]:
    artifact = _full_artifact(value)
    catalog = evidence_summary.get("committed_artifacts")
    metadata = catalog.get(artifact["artifact_id"]) if isinstance(catalog, Mapping) else None
    if (
        not isinstance(metadata, Mapping)
        or metadata.get("sha256") != artifact["sha256"]
        or metadata.get("byte_count") != artifact["byte_count"]
        or metadata.get("media_type") != artifact["media_type"]
        or metadata.get("ingest_state") != "COMMITTED"
    ):
        raise PublicationBindingError("artifact is not committed with exact metadata")
    if media_type is not None and artifact["media_type"] != media_type:
        raise PublicationBindingError("artifact media type is invalid")
    return artifact


def _full_artifact(value: object) -> dict[str, Any]:
    if not isinstance(value, Mapping) or set(value) != {
        "artifact_id",
        "sha256",
        "byte_count",
        "media_type",
    }:
        raise PublicationBindingError("artifact binding fields are not exact")
    return {
        "artifact_id": _string(value["artifact_id"]),
        "sha256": _digest(value["sha256"]),
        "byte_count": _natural(value["byte_count"]),
        "media_type": _string(value["media_type"]),
    }


def _exact_payload(context: ProductCommandContext, fields: set[str]) -> Mapping[str, Any]:
    if set(context.command.payload) != fields:
        raise PublicationBindingError("publication payload fields are not exact")
    return context.command.payload


def _mutation_value(mutation: Mapping[str, Any], opcode: str) -> Mapping[str, Any]:
    if (
        set(mutation) != {"op", "value"}
        or mutation["op"] != opcode
        or not isinstance(mutation["value"], Mapping)
    ):
        raise PublicationBindingError("mutation envelope is invalid")
    return mutation["value"]


def _finalization_in(connection: sqlite3.Connection, run_id: str) -> dict[str, Any]:
    row = connection.execute(
        "SELECT run_id,finalized_revision,contract_version,final_outcome,"
        "terminal_root_id,terminal_root_digest,closure_witness_id,"
        "dependency_closure_digest,finalize_command_id "
        "FROM product_publication_finalizations WHERE run_id=?",
        (run_id,),
    ).fetchone()
    if row is None:
        raise PublicationBindingError("run has no finalization projection")
    names = (
        "run_id",
        "finalized_revision",
        "contract_version",
        "final_outcome",
        "terminal_root_id",
        "terminal_root_digest",
        "closure_witness_id",
        "dependency_closure_digest",
        "finalize_command_id",
    )
    return dict(zip(names, tuple(row), strict=True))


def _candidate_in(connection: sqlite3.Connection, command_id: str) -> dict[str, Any]:
    row = connection.execute(
        "SELECT generation_command_id,run_id,finalized_revision,contract_version,"
        "terminal_root_id,terminal_root_digest,closure_witness_id,"
        "dependency_closure_digest,candidate_tex_artifact_id,candidate_tex_sha256,"
        "candidate_tex_byte_count,candidate_tex_media_type,generated_by_subject_id "
        "FROM product_publication_candidates WHERE generation_command_id=?",
        (command_id,),
    ).fetchone()
    if row is None:
        raise PublicationBindingError("candidate generation command is unavailable")
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


def _review_in(connection: sqlite3.Connection, review_id: str) -> dict[str, Any]:
    row = connection.execute(
        "SELECT paper_review_id,generation_command_id,candidate_tex_artifact_id,"
        "candidate_tex_sha256,reviewer_subject_id,verdict "
        "FROM product_publication_reviews WHERE paper_review_id=?",
        (review_id,),
    ).fetchone()
    if row is None:
        raise PublicationBindingError("paper review is unavailable")
    names = (
        "paper_review_id",
        "generation_command_id",
        "candidate_tex_artifact_id",
        "candidate_tex_sha256",
        "reviewer_subject_id",
        "verdict",
    )
    return dict(zip(names, tuple(row), strict=True))


def _mappings(value: object) -> tuple[Mapping[str, Any], ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise PublicationBindingError("projection collection is invalid")
    if any(not isinstance(item, Mapping) for item in value):
        raise PublicationBindingError("projection item is invalid")
    return tuple(item for item in value if isinstance(item, Mapping))


def _strings(value: object) -> tuple[str, ...]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise PublicationBindingError("identity collection is invalid")
    result = tuple(_string(item) for item in value)
    if len(result) != len(set(result)):
        raise PublicationBindingError("identity collection contains duplicates")
    return result


def _field(value: Mapping[str, Any], primary: str, alternate: str) -> Any:
    return value.get(primary, value.get(alternate))


def _enum(value: object, allowed: set[str]) -> str:
    result = _string(value)
    if result not in allowed:
        raise PublicationBindingError("enumerated value is invalid")
    return result


def _string(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise PublicationBindingError("value must be a non-empty string")
    return value


def _digest(value: object) -> str:
    result = _string(value)
    if len(result) != 64 or any(character not in "0123456789abcdef" for character in result):
        raise PublicationBindingError("digest must be lowercase SHA-256")
    return result


def _positive(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise PublicationBindingError("value must be a positive integer")
    return value


def _natural(value: object) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise PublicationBindingError("value must be a natural integer")
    return value


def _role_rejection(role: str) -> Decision:
    return _reject(RejectionCode.CAPABILITY_DENIED, "REQUIRED_ACTION", "/command/type", role=role)


def _binding_rejection() -> Decision:
    return _reject(RejectionCode.EVIDENCE_SCOPE_MISMATCH, "PUBLICATION_BINDING", "/command/payload")


def _reject(code: RejectionCode, condition: str, path: str, **params: Any) -> Decision:
    return Decision(
        accepted=False,
        rejection_code=code.value,
        missing_conditions=(MissingCondition(condition, path, MappingProxyType(dict(params))),),
    )


__all__ = ["PublicationBindingError", "PublicationHandlers", "register_publication_handlers"]
