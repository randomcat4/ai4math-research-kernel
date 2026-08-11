"""Transactional projection writer for accepted guard decisions.

The pure guard decides whether a command is legal.  This module is the only place that turns
its typed mutation intents into SQLite rows.  Unknown intents fail closed: an accepted receipt
is never committed without the corresponding projection update.
"""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from rk.domain import TypedCommand
from rk.ports import IdGenerator
from rk.runtime import format_utc


class ProjectionError(RuntimeError):
    """A guard mutation cannot be materialized without inventing semantics."""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


@dataclass(frozen=True, slots=True)
class ProjectionContext:
    run_id: str
    command_id: str
    event_id: str
    revision: int
    contract_version: int
    command: TypedCommand
    capability_id: str
    recorded_at: str
    artifacts_by_name: Mapping[str, Mapping[str, Any]]
    generated_artifact_ids: Mapping[str, str]


class ProjectionWriter:
    """Apply a frozen set of mutation opcodes inside the caller's transaction."""

    unsupported_ops = frozenset({"CREATE_CONTRACT_VERSION"})

    def __init__(self, id_generator: IdGenerator) -> None:
        self._ids = id_generator

    def supports(self, mutations: Sequence[Mapping[str, Any]]) -> bool:
        return all(str(item.get("op")) not in self.unsupported_ops for item in mutations)

    def apply(
        self,
        connection: sqlite3.Connection,
        context: ProjectionContext,
        mutations: Sequence[Mapping[str, Any]],
    ) -> None:
        for mutation in mutations:
            op = str(mutation.get("op"))
            handler = getattr(self, f"_op_{op}", None)
            if handler is None:
                raise ProjectionError(f"unsupported projection mutation: {op}")
            handler(connection, context, mutation)

    @staticmethod
    def _op_APPLY_COMMAND(
        connection: sqlite3.Connection,
        context: ProjectionContext,
        mutation: Mapping[str, Any],
    ) -> None:
        del connection, context, mutation

    @staticmethod
    def _op_SET_CONTRACT_STATUS(
        connection: sqlite3.Connection,
        context: ProjectionContext,
        mutation: Mapping[str, Any],
    ) -> None:
        status = str(mutation["status"])
        frozen_at = context.recorded_at if status == "FROZEN" else None
        connection.execute(
            "UPDATE contract_versions SET status = ?, frozen_at = COALESCE(?, frozen_at) "
            "WHERE run_id = ? AND version = ?",
            (status, frozen_at, context.run_id, int(mutation["version"])),
        )
        if context.command.type == "ProposeContractDefect":
            payload = context.command.payload
            connection.execute(
                "UPDATE contract_versions SET defect_type = ?, defect_evidence_json = ? "
                "WHERE run_id = ? AND version = ?",
                (
                    payload.get("defect_type"),
                    _json(
                        {
                            "evidence_refs": payload.get("evidence_refs", []),
                            "affected_claim_ids": payload.get("affected_claim_ids", []),
                            "proposed_patch_artifact_id": payload.get(
                                "proposed_patch_artifact_id"
                            ),
                        }
                    ),
                    context.run_id,
                    context.contract_version,
                ),
            )

    @staticmethod
    def _op_SET_RUN_STATUS(
        connection: sqlite3.Connection,
        context: ProjectionContext,
        mutation: Mapping[str, Any],
    ) -> None:
        status = str(mutation["status"])
        if status == "CLOSED":
            connection.execute(
                "UPDATE runs SET status = 'CLOSED', final_outcome = ?, closed_at = ?, "
                "updated_at = ? WHERE run_id = ?",
                (mutation.get("outcome"), context.recorded_at, context.recorded_at, context.run_id),
            )
        else:
            connection.execute(
                "UPDATE runs SET status = ?, updated_at = ? WHERE run_id = ?",
                (status, context.recorded_at, context.run_id),
            )

    @staticmethod
    def _op_PAUSE_ACTIVE_ATTEMPTS(
        connection: sqlite3.Connection,
        context: ProjectionContext,
        mutation: Mapping[str, Any],
    ) -> None:
        del mutation
        connection.execute(
            "UPDATE attempts SET status = 'PAUSED' WHERE run_id = ? AND status = 'RUNNING'",
            (context.run_id,),
        )

    @staticmethod
    def _op_SUPERSEDE_CONTRACT(
        connection: sqlite3.Connection,
        context: ProjectionContext,
        mutation: Mapping[str, Any],
    ) -> None:
        connection.execute(
            "UPDATE contract_versions SET status = 'SUPERSEDED' "
            "WHERE run_id = ? AND version = ?",
            (context.run_id, int(mutation["version"])),
        )

    @staticmethod
    def _op_CREATE_CONTRACT_VERSION(
        connection: sqlite3.Connection,
        context: ProjectionContext,
        mutation: Mapping[str, Any],
    ) -> None:
        del connection, context, mutation
        raise ProjectionError(
            "AmendContract is disabled until the patch artifact wire format is frozen"
        )

    @staticmethod
    def _op_INVALIDATE_DEPENDENCY_CLOSURE(
        connection: sqlite3.Connection,
        context: ProjectionContext,
        mutation: Mapping[str, Any],
    ) -> None:
        obligation_id = mutation.get("first_failed_obligation_id")
        if obligation_id:
            parent = connection.execute(
                "SELECT parent_claim_id FROM composition_obligations WHERE obligation_id = ?",
                (obligation_id,),
            ).fetchone()
            if parent is not None:
                connection.execute(
                    "UPDATE claims SET closure_state = 'INVALIDATED', "
                    "invalidated_by_event_id = ?, updated_at = ? WHERE claim_id = ?",
                    (context.event_id, context.recorded_at, parent[0]),
                )
        else:
            connection.execute(
                "UPDATE claims SET closure_state = 'INVALIDATED', "
                "invalidated_by_event_id = ?, updated_at = ? "
                "WHERE run_id = ? AND contract_version = ? AND closure_state <> 'NOT_REQUIRED'",
                (
                    context.event_id,
                    context.recorded_at,
                    context.run_id,
                    context.contract_version,
                ),
            )

    @staticmethod
    def _op_CREATE_DOSSIER(
        connection: sqlite3.Connection,
        context: ProjectionContext,
        mutation: Mapping[str, Any],
    ) -> None:
        del mutation
        artifact_id = context.generated_artifact_ids.get("dossier")
        if artifact_id is None:
            raise ProjectionError("Finalize did not stage a dossier artifact")
        connection.execute(
            "UPDATE runs SET parent_dossier_artifact_id = ? WHERE run_id = ?",
            (artifact_id, context.run_id),
        )

    def _op_APPEND_EVIDENCE(
        self,
        connection: sqlite3.Connection,
        context: ProjectionContext,
        mutation: Mapping[str, Any],
    ) -> None:
        del mutation
        payload = context.command.payload
        root = payload["evidence_root"]
        root_id = self._ids.new()
        origin_name = root.get("origin_artifact_input_name")
        origin = context.artifacts_by_name.get(str(origin_name)) if origin_name else None
        connection.execute(
            "INSERT INTO evidence_roots(evidence_root_id,run_id,root_kind,origin_artifact_id,"
            "verifier_profile_id,ancestor_root_ids_json,source_graph_json,created_by_event_id,"
            "created_at) VALUES (?,?,?,?,?,?,?,?,?)",
            (
                root_id,
                context.run_id,
                root["root_kind"],
                origin.get("artifact_id") if origin else root.get("origin_artifact_id"),
                root.get("verifier_profile_id"),
                _json(root.get("ancestor_root_ids", [])),
                _json(root.get("source_graph", {})),
                context.event_id,
                context.recorded_at,
            ),
        )
        names = [str(item) for item in payload["artifact_input_names"]]
        for name in names:
            artifact = context.artifacts_by_name.get(name)
            if artifact is None:
                raise ProjectionError(f"accepted evidence artifact missing: {name}")
            connection.execute(
                "INSERT INTO evidence(evidence_id,run_id,claim_id,contract_version,statement_hash,"
                "artifact_id,evidence_type,evidence_strength,evidence_root_id,scope_json,"
                "provenance_json,ingest_schema_version,ingest_status,submitted_by_command_id,"
                "created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    self._ids.new(),
                    context.run_id,
                    payload["claim_id"],
                    payload["contract_version"],
                    payload["statement_hash"],
                    artifact["artifact_id"],
                    payload["evidence_type"],
                    payload["evidence_strength"],
                    root_id,
                    _json(payload["scope"]),
                    _json(payload["provenance"]),
                    1,
                    "ACCEPTED",
                    context.command_id,
                    context.recorded_at,
                ),
            )

    def _op_APPEND_FAILURE(
        self,
        connection: sqlite3.Connection,
        context: ProjectionContext,
        mutation: Mapping[str, Any],
    ) -> None:
        del mutation
        payload = context.command.payload
        connection.execute(
            "INSERT INTO failure_records(failure_record_id,run_id,contract_version,route_id,"
            "claim_id,failure_kind,normalized_fingerprint,equivalence_key,"
            "first_failed_obligation_id,evidence_artifact_id,applicability_json,"
            "novelty_delta_json,status,created_by_event_id,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                self._ids.new(),
                context.run_id,
                context.contract_version,
                payload.get("route_id"),
                payload.get("claim_id"),
                payload["failure_kind"],
                payload["normalized_fingerprint"],
                payload["equivalence_key"],
                payload.get("first_failed_obligation_id"),
                payload.get("evidence_artifact_id"),
                _json(payload["applicability"]),
                _json(payload["novelty_delta"]),
                "ACTIVE",
                context.event_id,
                context.recorded_at,
            ),
        )

    @staticmethod
    def _op_APPROVE_EXPANSION_BATCH(
        connection: sqlite3.Connection,
        context: ProjectionContext,
        mutation: Mapping[str, Any],
    ) -> None:
        del connection, context, mutation

    def _op_APPEND_BUDGET_RESERVATION(
        self,
        connection: sqlite3.Connection,
        context: ProjectionContext,
        mutation: Mapping[str, Any],
    ) -> None:
        reservation = context.command.payload.get("reservation", {})
        if not isinstance(reservation, Mapping):
            raise ProjectionError("reservation is not a mapping")
        for resource_kind, value in sorted(reservation.items()):
            if isinstance(value, Mapping):
                amount = value.get("amount_microunits")
                unit = value.get("unit", "microunit")
                currency = value.get("currency")
            else:
                amount, unit, currency = value, "microunit", None
            connection.execute(
                "INSERT INTO budget_events(budget_event_id,run_id,route_id,attempt_id,command_id,"
                "revision,event_kind,resource_kind,amount_microunits,unit,currency,"
                "provider_usage_json,recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    self._ids.new(),
                    context.run_id,
                    mutation.get("route_id"),
                    None,
                    context.command_id,
                    context.revision,
                    "RESERVATION",
                    resource_kind,
                    amount,
                    unit,
                    currency,
                    "{}",
                    context.recorded_at,
                ),
            )

    def _op_APPEND_PEER_REVIEW(
        self,
        connection: sqlite3.Connection,
        context: ProjectionContext,
        mutation: Mapping[str, Any],
    ) -> None:
        del mutation
        p = context.command.payload
        connection.execute(
            "INSERT INTO peer_reviews(review_id,run_id,claim_id,contract_version,statement_hash,"
            "selected_subgraph_digest,reviewer_capability_id,independence_profile_json,verdict,"
            "checklist_json,review_artifact_id,source_graph_json,created_by_event_id,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                self._ids.new(), context.run_id, p["claim_id"], p["contract_version"],
                p["statement_hash"], p.get("selected_subgraph_digest"), context.capability_id,
                _json(p["independence_profile"]), p["verdict"], _json(p["checklist"]),
                p["review_artifact_id"], _json(p["source_graph"]), context.event_id,
                context.recorded_at,
            ),
        )

    def _op_APPEND_QUALITY_REVIEW(
        self,
        connection: sqlite3.Connection,
        context: ProjectionContext,
        mutation: Mapping[str, Any],
    ) -> None:
        del mutation
        p = context.command.payload
        connection.execute(
            "INSERT INTO quality_reviews(quality_review_id,run_id,claim_id,contract_version,"
            "reviewer_capability_id,verdict,dimensions_json,review_artifact_id,training_pool,"
            "created_by_event_id,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
            (
                self._ids.new(), context.run_id, p["claim_id"], p["contract_version"],
                context.capability_id, p["verdict"], _json(p["dimensions"]),
                p["review_artifact_id"], p["training_pool"], context.event_id,
                context.recorded_at,
            ),
        )

    def _op_APPEND_LITERATURE_RECORD(
        self,
        connection: sqlite3.Connection,
        context: ProjectionContext,
        mutation: Mapping[str, Any],
    ) -> None:
        del mutation
        p = context.command.payload
        connection.execute(
            "INSERT INTO literature_records(literature_record_id,run_id,contract_version,claim_id,"
            "status,relation,scope_json,cutoff_date,query_families_json,query_log_artifact_id,"
            "reference_artifact_id,assessment_artifact_id,created_by_event_id,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                self._ids.new(), context.run_id, p["contract_version"], p.get("claim_id"),
                p["status"], p.get("relation"), _json(p["scope"]), p["cutoff_date"],
                _json(p["query_families"]), p["query_log_artifact_id"],
                p.get("reference_artifact_id"), p["assessment_artifact_id"], context.event_id,
                context.recorded_at,
            ),
        )

    def _op_REGISTER_BRIDGE(
        self,
        connection: sqlite3.Connection,
        context: ProjectionContext,
        mutation: Mapping[str, Any],
    ) -> None:
        p = context.command.payload
        bridge_id = str(p.get("bridge_id") or self._ids.new())
        connection.execute(
            "INSERT INTO bridges(bridge_id,run_id,contract_version,source_claim_id,target_claim_id,"
            "directionality,term_mapping_json,forward_obligations_json,reverse_obligations_json,"
            "loss_accounting_json,target_audit_review_id,backtranslation_artifact_id,"
            "created_by_event_id,updated_by_event_id,created_at,updated_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                bridge_id, context.run_id, p["contract_version"], p["source_claim_id"],
                p["target_claim_id"], mutation["directionality"], _json(p["term_mapping"]),
                _json(p["forward_obligations"]), _json(p["reverse_obligations"]),
                _json(p["loss_accounting"]), p.get("target_audit_review_id"),
                p.get("backtranslation_artifact_id"), context.event_id, context.event_id,
                context.recorded_at, context.recorded_at,
            ),
        )

    def _op_APPEND_LEAN_FEEDBACK(
        self,
        connection: sqlite3.Connection,
        context: ProjectionContext,
        mutation: Mapping[str, Any],
    ) -> None:
        del mutation
        p = context.command.payload
        connection.execute(
            "INSERT INTO lean_feedback_events(lean_feedback_id,run_id,claim_id,attempt_id,"
            "contract_version,environment_profile_id,toolchain,mathlib_commit,source_artifact_id,"
            "output_artifact_id,feedback_kind,first_failed_obligation_id,diagnostic_json,"
            "created_by_event_id,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                self._ids.new(), context.run_id, p["claim_id"], p.get("attempt_id"),
                p["contract_version"], p["environment_profile_id"], p["toolchain"],
                p.get("mathlib_commit"), p["source_artifact_id"], p["output_artifact_id"],
                p["feedback_kind"], p.get("first_failed_obligation_id"), _json(p["diagnostic"]),
                context.event_id, context.recorded_at,
            ),
        )

    @staticmethod
    def _op_INVALIDATE_FROM_FIRST_FAILURE(
        connection: sqlite3.Connection,
        context: ProjectionContext,
        mutation: Mapping[str, Any],
    ) -> None:
        obligation_id = mutation.get("obligation_id")
        if obligation_id:
            ProjectionWriter._op_INVALIDATE_DEPENDENCY_CLOSURE(
                connection,
                context,
                {"first_failed_obligation_id": obligation_id},
            )

    def _op_REGISTER_CLAIM(
        self,
        connection: sqlite3.Connection,
        context: ProjectionContext,
        mutation: Mapping[str, Any],
    ) -> None:
        p = context.command.payload
        claim_id = self._ids.new()
        connection.execute(
            "INSERT INTO claims(claim_id,run_id,contract_version,claim_kind,stable_label,"
            "statement_revision,statement_artifact_id,statement_hash,normalized_statement_json,"
            "lifecycle_status,route_result,machine_verdict,semantic_verdict,peer_verdict,"
            "quality_verdict,closure_state,created_by_event_id,created_at,updated_at) "
            "VALUES (?,?,?,?,?,1,?,?,?,'ACTIVE','UNASSESSED','UNVERIFIED','UNREVIEWED',"
            "'UNREVIEWED','UNREVIEWED',?,?,?,?)",
            (
                claim_id, context.run_id, p["contract_version"], p["claim_kind"],
                p["stable_label"], p["statement_artifact_id"], p["statement_hash"],
                _json(p["normalized_statement"]), mutation["closure"], context.event_id,
                context.recorded_at, context.recorded_at,
            ),
        )
        if p["claim_kind"] == "ROOT":
            connection.execute(
                "UPDATE runs SET root_claim_id = COALESCE(root_claim_id, ?) WHERE run_id = ?",
                (claim_id, context.run_id),
            )

    def _op_REGISTER_CLAIM_EDGE(
        self,
        connection: sqlite3.Connection,
        context: ProjectionContext,
        mutation: Mapping[str, Any],
    ) -> None:
        del mutation
        p = context.command.payload
        connection.execute(
            "INSERT INTO claim_edges(edge_id,run_id,contract_version,from_claim_id,to_claim_id,"
            "edge_kind,direction,justification_kind,justification_ref,status,created_by_event_id,"
            "created_at) VALUES (?,?,?,?,?,?,?,?,?,'ACTIVE',?,?)",
            (
                self._ids.new(), context.run_id, p["contract_version"], p["from_claim_id"],
                p["to_claim_id"], p["edge_kind"], p["direction"], p["justification_kind"],
                p["justification_ref"], context.event_id, context.recorded_at,
            ),
        )

    @staticmethod
    def _op_INVALIDATE_PARENT_CLOSURE(
        connection: sqlite3.Connection,
        context: ProjectionContext,
        mutation: Mapping[str, Any],
    ) -> None:
        connection.execute(
            "UPDATE claims SET closure_state = 'INVALIDATED', updated_at = ?, "
            "invalidated_by_event_id = ? WHERE claim_id = ? AND closure_state <> 'NOT_REQUIRED'",
            (context.recorded_at, context.event_id, mutation.get("claim_id")),
        )

    def _op_REGISTER_ROUTE(
        self,
        connection: sqlite3.Connection,
        context: ProjectionContext,
        mutation: Mapping[str, Any],
    ) -> None:
        p = context.command.payload
        root = p["approach_root"]
        root_id = self._ids.new()
        connection.execute(
            "INSERT INTO approach_roots(approach_root_id,run_id,label,origin_artifact_id,"
            "origin_event_id,parent_root_ids_json,contact_epoch,contamination_json,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (
                root_id, context.run_id, root.get("label", p["label"]),
                root.get("origin_artifact_id"), context.event_id,
                _json(root.get("parent_root_ids", [])), int(root.get("contact_epoch", 0)),
                _json(root.get("contamination", {})), context.recorded_at,
            ),
        )
        connection.execute(
            "INSERT INTO routes(route_id,run_id,contract_version,target_claim_id,label,status,"
            "representation,tool_family,approach_root_id,budget_policy_json,created_by_event_id,"
            "created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                self._ids.new(), context.run_id, p["contract_version"], p["target_claim_id"],
                p["label"], mutation["status"], p["representation"], p["tool_family"], root_id,
                _json(p["budget_policy"]), context.event_id, context.recorded_at,
                context.recorded_at,
            ),
        )

    def _op_REGISTER_COMPOSITION_OBLIGATION(
        self,
        connection: sqlite3.Connection,
        context: ProjectionContext,
        mutation: Mapping[str, Any],
    ) -> None:
        del mutation
        p = context.command.payload
        parts: list[Any] = []
        for name in (
            "coverage", "compatibility", "invariant", "progress", "boundary",
            "simultaneous_choice",
        ):
            item = p[name]
            parts.extend((item["ref"], item["status"]))
        connection.execute(
            "INSERT INTO composition_obligations(obligation_id,run_id,contract_version,"
            "parent_claim_id,child_claim_ids_json,local_domain_json,coverage_ref,coverage_status,"
            "compatibility_ref,compatibility_status,invariant_ref,invariant_status,progress_ref,"
            "progress_status,boundary_ref,boundary_status,simultaneous_choice_ref,"
            "simultaneous_choice_status,composition_rule,closure_theorem_ref,"
            "missing_conditions_json,displacement_status,status,created_by_event_id,"
            "updated_by_event_id,created_at,updated_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,"
            "?,?,?,?,?,?,?,?,?,?,?)",
            (
                self._ids.new(), context.run_id, p["contract_version"], p["parent_claim_id"],
                _json(p["child_claim_ids"]), _json(p["local_domain"]), *parts,
                p["composition_rule"], p["closure_theorem_ref"], _json(p["missing_conditions"]),
                p["displacement_status"], "OPEN", context.event_id, context.event_id,
                context.recorded_at, context.recorded_at,
            ),
        )

    @staticmethod
    def _op_SET_CLOSURE(
        connection: sqlite3.Connection,
        context: ProjectionContext,
        mutation: Mapping[str, Any],
    ) -> None:
        connection.execute(
            "UPDATE claims SET closure_state = ?, updated_at = ? WHERE claim_id = ?",
            (mutation["value"], context.recorded_at, mutation["claim_id"]),
        )

    def _op_ACCEPT_CLOSURE_WITNESS(
        self,
        connection: sqlite3.Connection,
        context: ProjectionContext,
        mutation: Mapping[str, Any],
    ) -> None:
        p = context.command.payload
        artifact_id = context.generated_artifact_ids.get("selected_subgraph")
        if artifact_id is None:
            raise ProjectionError("closure witness subgraph artifact was not staged")
        witness_id = self._ids.new()
        connection.execute(
            "INSERT INTO closure_witnesses(witness_id,run_id,parent_claim_id,contract_version,"
            "selected_subgraph_digest,selected_subgraph_artifact_id,"
            "discharged_obligations_json,open_obligations_json,edge_justifications_json,"
            "bridge_dependencies_json,composition_mode,verification_refs_json,"
            "human_attestation_review_ids_json,status,created_by_event_id,accepted_by_event_id,"
            "created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,'ACCEPTED',?,?,?)",
            (
                witness_id, context.run_id, p["parent_claim_id"], p["contract_version"],
                p["selected_subgraph_digest"], artifact_id,
                _json(p["discharged_obligation_ids"]), _json(p["open_obligation_ids"]),
                _json(p["edge_justifications"]), _json(p["bridge_dependency_ids"]),
                p["composition_mode"], _json(p["verification_refs"]),
                _json(p["human_attestation_review_ids"]), context.event_id, context.event_id,
                context.recorded_at,
            ),
        )
        if p["discharged_obligation_ids"]:
            placeholders = ",".join("?" for _ in p["discharged_obligation_ids"])
            status = {
                "MACHINE": "DISCHARGED_MACHINE",
                "PEER": "DISCHARGED_HUMAN",
                "HYBRID": "DISCHARGED_HYBRID",
            }[p["composition_mode"]]
            connection.execute(
                f"UPDATE composition_obligations SET status = ?, updated_by_event_id = ?, "
                f"updated_at = ? WHERE obligation_id IN ({placeholders})",
                (status, context.event_id, context.recorded_at, *p["discharged_obligation_ids"]),
            )
        connection.execute(
            "UPDATE claims SET closure_state = ?, updated_at = ? WHERE claim_id = ?",
            (mutation["closure_state"], context.recorded_at, p["parent_claim_id"]),
        )

    def _op_SET_CLAIM_AXIS(
        self,
        connection: sqlite3.Connection,
        context: ProjectionContext,
        mutation: Mapping[str, Any],
    ) -> None:
        columns = {
            "ROUTE": "route_result", "MACHINE": "machine_verdict",
            "SEMANTIC": "semantic_verdict", "PEER": "peer_verdict",
            "QUALITY": "quality_verdict", "CLOSURE": "closure_state",
        }
        axis = str(mutation["axis"])
        column = columns.get(axis)
        if column is None:
            raise ProjectionError(f"unknown claim axis: {axis}")
        before = connection.execute(
            f"SELECT {column} FROM claims WHERE claim_id = ?",
            (mutation["claim_id"],),
        ).fetchone()
        if before is None:
            raise ProjectionError("claim disappeared during promotion")
        connection.execute(
            f"UPDATE claims SET {column} = ?, updated_at = ? WHERE claim_id = ?",
            (mutation["value"], context.recorded_at, mutation["claim_id"]),
        )
        p = context.command.payload
        connection.execute(
            "INSERT INTO verdict_events(verdict_event_id,run_id,claim_id,command_id,revision,"
            "axis,value_before,value_after,evidence_ids_json,closure_witness_id,capability_id,"
            "reason_code,recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                self._ids.new(), context.run_id, mutation["claim_id"], context.command_id,
                context.revision, axis, before[0], mutation["value"],
                _json(p.get("evidence_ids", [])), p.get("closure_witness_id"),
                context.capability_id, "PROMOTE_CLAIM", context.recorded_at,
            ),
        )

    def _op_REGISTER_ATTEMPT(
        self,
        connection: sqlite3.Connection,
        context: ProjectionContext,
        mutation: Mapping[str, Any],
    ) -> None:
        p = context.command.payload
        connection.execute(
            "INSERT INTO attempts(attempt_id,route_id,run_id,ordinal,status,isolation_epoch,"
            "work_relpath,allowed_write_set_json,input_snapshot_digest,created_by_event_id) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)",
            (
                self._ids.new(), p["route_id"], context.run_id, p["ordinal"], mutation["status"],
                p["isolation_epoch"], p["work_relpath"], _json(p["allowed_write_set"]),
                p["input_snapshot_digest"], context.event_id,
            ),
        )

    def _op_ACQUIRE_LEASE(
        self,
        connection: sqlite3.Connection,
        context: ProjectionContext,
        mutation: Mapping[str, Any],
    ) -> None:
        del mutation
        p = context.command.payload
        now = datetime.fromisoformat(context.recorded_at.replace("Z", "+00:00"))
        expires = format_utc(now + timedelta(seconds=int(p["ttl_seconds"])))
        connection.execute(
            "INSERT INTO leases(lease_id,attempt_id,holder_id,status,acquired_at,heartbeat_at,"
            "expires_at,created_by_event_id) VALUES (?,?,?,'ACTIVE',?,?,?,?)",
            (
                self._ids.new(), p["attempt_id"], p["holder_id"], context.recorded_at,
                context.recorded_at, expires, context.event_id,
            ),
        )

    @staticmethod
    def _op_SET_ATTEMPT_STATUS(
        connection: sqlite3.Connection,
        context: ProjectionContext,
        mutation: Mapping[str, Any],
    ) -> None:
        p = context.command.payload
        attempt_id = p.get("attempt_id")
        if attempt_id is None and p.get("lease_id"):
            row = connection.execute(
                "SELECT attempt_id FROM leases WHERE lease_id = ?", (p["lease_id"],)
            ).fetchone()
            attempt_id = row[0] if row else None
        status = str(mutation["status"])
        started = context.recorded_at if status in {
            "RUNNING", "PAUSED", "SUCCEEDED", "FAILED", "ABORTED", "ENVIRONMENT_ERROR"
        } else None
        ended = context.recorded_at if status in {
            "SUCCEEDED", "FAILED", "ABORTED", "ENVIRONMENT_ERROR"
        } else None
        connection.execute(
            "UPDATE attempts SET status = ?, started_at = COALESCE(started_at, ?), "
            "ended_at = COALESCE(?, ended_at) WHERE attempt_id = ?",
            (status, started, ended, attempt_id),
        )

    @staticmethod
    def _op_EXTEND_LEASE(
        connection: sqlite3.Connection,
        context: ProjectionContext,
        mutation: Mapping[str, Any],
    ) -> None:
        now = datetime.fromisoformat(context.recorded_at.replace("Z", "+00:00"))
        expires = format_utc(now + timedelta(seconds=int(mutation["seconds"])))
        connection.execute(
            "UPDATE leases SET heartbeat_at = ?, expires_at = ? WHERE lease_id = ?",
            (context.recorded_at, expires, mutation["lease_id"]),
        )

    @staticmethod
    def _op_RELEASE_LEASE(
        connection: sqlite3.Connection,
        context: ProjectionContext,
        mutation: Mapping[str, Any],
    ) -> None:
        connection.execute(
            "UPDATE leases SET status = 'RELEASED', released_at = ? WHERE lease_id = ?",
            (context.recorded_at, mutation["lease_id"]),
        )

    def _op_APPEND_BUDGET_EVENT(
        self,
        connection: sqlite3.Connection,
        context: ProjectionContext,
        mutation: Mapping[str, Any],
    ) -> None:
        del mutation
        p = context.command.payload
        connection.execute(
            "INSERT INTO budget_events(budget_event_id,run_id,route_id,attempt_id,command_id,"
            "revision,event_kind,resource_kind,amount_microunits,unit,currency,"
            "provider_usage_json,recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                self._ids.new(), context.run_id, p.get("route_id"), p.get("attempt_id"),
                context.command_id, context.revision, p["event_kind"], p["resource_kind"],
                p.get("amount_microunits"), p["unit"], p.get("currency"),
                _json(p["provider_usage"]), context.recorded_at,
            ),
        )

    @staticmethod
    def _op_BLOCK_ROUTE(
        connection: sqlite3.Connection,
        context: ProjectionContext,
        mutation: Mapping[str, Any],
    ) -> None:
        route_id = mutation.get("route_id")
        if route_id is not None:
            connection.execute(
                "UPDATE routes SET status = 'BLOCKED', updated_at = ? WHERE route_id = ?",
                (context.recorded_at, route_id),
            )

    def _op_BIND_EXECUTION(
        self,
        connection: sqlite3.Connection,
        context: ProjectionContext,
        mutation: Mapping[str, Any],
    ) -> None:
        del mutation
        p = context.command.payload
        external = p["external_ids"]
        connection.execute(
            "INSERT INTO execution_bindings(binding_id,run_id,route_id,attempt_id,adapter_name,"
            "adapter_version,source_commit,external_run_id,external_task_id,"
            "external_session_ids_json,workspace_commit,environment_profile_id,"
            "invocation_artifact_id,created_by_event_id,created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                self._ids.new(), context.run_id, p["route_id"], p["attempt_id"],
                p["adapter_name"], p["adapter_version"], p.get("source_commit"),
                external.get("run_id"), external.get("task_id"),
                _json(external.get("session_ids", [])), external.get("workspace_commit"),
                p["environment_profile_id"], p["invocation_artifact_id"], context.event_id,
                context.recorded_at,
            ),
        )
