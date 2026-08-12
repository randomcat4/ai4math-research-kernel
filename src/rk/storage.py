"""SQLite persistence hidden behind the ResearchKernel seam."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections.abc import Iterator, Mapping, Sequence
from contextlib import contextmanager
from pathlib import Path
from typing import Any

from rk.domain import VerifiedCapability

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")


class StorageError(RuntimeError):
    """Base class for local persistence failures."""


class StorageConflict(StorageError):
    """A stable identity was reused with different immutable content."""


class RunNotFound(StorageError):
    """The requested run does not exist."""


class RevisionConflict(StorageConflict):
    """A compare-and-set revision update lost a race."""


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def _digest_label(value: str, *, prefix: bytes) -> str:
    if _SHA256_RE.fullmatch(value):
        return value
    return hashlib.sha256(prefix + value.encode("utf-8")).hexdigest()


class SQLiteStorage:
    """Own connections, transaction rules, idempotency reads, and inspect projections.

    Business projection SQL may be executed by the kernel on the connection yielded by
    :meth:`transaction`; this module deliberately does not learn TransitionGuard rules.
    """

    def __init__(self, db_path: Path, busy_timeout_ms: int) -> None:
        if busy_timeout_ms <= 0:
            raise ValueError("busy_timeout_ms must be positive")
        self._db_path = Path(db_path)
        self._busy_timeout_ms = busy_timeout_ms

    def connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(
            self._db_path,
            timeout=self._busy_timeout_ms / 1_000,
            isolation_level=None,
        )
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA synchronous = FULL")
        connection.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
        return connection

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Yield one single-writer transaction and roll it back on every exception class."""

        connection = self.connect()
        try:
            connection.execute("BEGIN IMMEDIATE")
            yield connection
            connection.commit()
        except BaseException:
            connection.rollback()
            raise
        finally:
            connection.close()

    @contextmanager
    def _reader(self, connection: sqlite3.Connection | None) -> Iterator[sqlite3.Connection]:
        if connection is not None:
            yield connection
            return
        owned = self.connect()
        try:
            yield owned
        finally:
            owned.close()

    def ensure_capability(
        self,
        capability: VerifiedCapability,
        credential_digest: str = "inprocess",
        *,
        key_id: str | None = None,
        nonce: str | None = None,
        connection: sqlite3.Connection | None = None,
    ) -> None:
        """Persist only verified capability facts; never persist a signature or secret."""

        digest_source = (
            credential_digest
            if _SHA256_RE.fullmatch(credential_digest)
            else f"{credential_digest}\n{capability.capability_id}"
        )
        digest = _digest_label(digest_source, prefix=b"rk.credential.v1\n")
        derived_nonce = nonce or _digest_label(
            capability.capability_id, prefix=b"rk.capability.nonce.v1\n"
        )
        values = (
            capability.capability_id,
            capability.subject_id,
            capability.issuer,
            key_id or f"verified:{capability.issuer}",
            _json(sorted(capability.allowed_actions)),
            _json(sorted(capability.run_scope)),
            derived_nonce,
            digest,
            capability.issued_at,
            capability.expires_at,
        )

        def write(conn: sqlite3.Connection) -> None:
            conn.execute(
                "INSERT OR IGNORE INTO capabilities("
                "capability_id, subject_id, issuer, key_id, allowed_actions_json, "
                "run_scope_json, nonce, credential_digest, issued_at, expires_at"
                ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                values,
            )
            row = conn.execute(
                "SELECT capability_id, subject_id, issuer, key_id, allowed_actions_json, "
                "run_scope_json, nonce, credential_digest, issued_at, expires_at "
                "FROM capabilities WHERE capability_id = ?",
                (capability.capability_id,),
            ).fetchone()
            if row is None or tuple(row) != values:
                raise StorageConflict("capability identity was reused with different facts")

        if connection is not None:
            write(connection)
        else:
            with self.transaction() as conn:
                write(conn)

    def find_create_request(
        self,
        issuer: str,
        request_id: str,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> dict[str, Any] | None:
        with self._reader(connection) as conn:
            row = conn.execute(
                "SELECT create_request_digest, run_id, revision, status, "
                "current_contract_version, created_at FROM runs "
                "WHERE create_issuer = ? AND create_request_id = ?",
                (issuer, request_id),
            ).fetchone()
        if row is None:
            return None
        return {
            "create_request_digest": str(row["create_request_digest"]),
            "handle": self._handle(row),
        }

    def create_run_atomic(
        self,
        *,
        run_id: str,
        stable_project_id: str,
        create_issuer: str,
        create_request_id: str,
        create_request_digest: str,
        capability_id: str,
        contract_artifact: Mapping[str, Any],
        additional_artifacts: Sequence[Mapping[str, Any]] = (),
        contract_json: Mapping[str, Any],
        statement_hash: str,
        created_at: str,
    ) -> dict[str, Any]:
        """Create the artifact, run, and draft contract v1 in one deferred-FK transaction."""

        with self.transaction() as connection:
            existing = self.find_create_request(
                create_issuer, create_request_id, connection=connection
            )
            if existing is not None:
                if existing["create_request_digest"] != create_request_digest:
                    raise StorageConflict("create request id was reused with different content")
                return dict(existing["handle"])
            connection.execute("PRAGMA defer_foreign_keys = ON")
            canonical_artifact = self.insert_artifact(connection, contract_artifact)
            canonical_additional = [
                self.insert_artifact(connection, artifact) for artifact in additional_artifacts
            ]
            connection.execute(
                "INSERT INTO runs("
                "run_id, stable_project_id, create_issuer, create_request_id, "
                "create_request_digest, created_by_capability_id, status, revision, "
                "current_contract_version, created_at, updated_at"
                ") VALUES (?, ?, ?, ?, ?, ?, 'OPEN', 0, 1, ?, ?)",
                (
                    run_id,
                    stable_project_id,
                    create_issuer,
                    create_request_id,
                    create_request_digest,
                    capability_id,
                    created_at,
                    created_at,
                ),
            )
            self.link_artifact(
                connection,
                run_id=run_id,
                artifact_id=str(canonical_artifact["artifact_id"]),
                logical_name="contract.v1.json",
                role="CONTRACT",
                linked_at=created_at,
            )
            for ordinal, (declared, artifact) in enumerate(
                zip(additional_artifacts, canonical_additional, strict=True), start=1
            ):
                logical_name = str(declared.get("source_name") or f"create_input_{ordinal}")
                self.link_artifact(
                    connection,
                    run_id=run_id,
                    artifact_id=str(artifact["artifact_id"]),
                    logical_name=logical_name,
                    role="CREATE_INPUT",
                    linked_at=created_at,
                )
            connection.execute(
                "INSERT INTO contract_versions("
                "run_id, version, status, contract_artifact_id, contract_json, "
                "statement_hash, supersedes_version, created_by_capability_id, "
                "approved_by_json, created_at, frozen_at"
                ") VALUES (?, 1, 'DRAFT', ?, ?, ?, NULL, ?, '[]', ?, NULL)",
                (
                    run_id,
                    str(canonical_artifact["artifact_id"]),
                    _json(contract_json),
                    statement_hash,
                    capability_id,
                    created_at,
                ),
            )
        return {
            "run_id": run_id,
            "revision": 0,
            "status": "OPEN",
            "current_contract_version": 1,
            "created_at": created_at,
        }

    def get_run(
        self,
        run_id: str,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> dict[str, Any] | None:
        with self._reader(connection) as conn:
            row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
        return dict(row) if row is not None else None

    def find_command(
        self,
        run_id: str,
        request_id: str,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> dict[str, Any] | None:
        with self._reader(connection) as conn:
            row = conn.execute(
                "SELECT request_digest, receipt_json FROM commands "
                "WHERE run_id = ? AND request_id = ?",
                (run_id, request_id),
            ).fetchone()
        if row is None:
            return None
        return {
            "request_digest": str(row["request_digest"]),
            "receipt": json.loads(str(row["receipt_json"])),
        }

    def record_command(
        self,
        connection: sqlite3.Connection,
        *,
        command_id: str,
        run_id: str,
        request_id: str,
        command_type: str,
        request_digest: str,
        expected_revision: int,
        capability_id: str,
        accepted: bool,
        revision_before: int,
        revision_after: int,
        rejection_code: str | None,
        missing_conditions: list[Mapping[str, Any]] | tuple[Mapping[str, Any], ...],
        receipt: Mapping[str, Any],
        trace_id: str,
        decided_at: str,
    ) -> None:
        connection.execute(
            "INSERT INTO commands("
            "command_id, run_id, request_id, command_type, request_digest, expected_revision, "
            "capability_id, accepted, revision_before, revision_after, rejection_code, "
            "missing_conditions_json, receipt_json, trace_id, decided_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                command_id,
                run_id,
                request_id,
                command_type,
                request_digest,
                expected_revision,
                capability_id,
                int(accepted),
                revision_before,
                revision_after,
                rejection_code,
                _json(list(missing_conditions)),
                _json(receipt),
                trace_id,
                decided_at,
            ),
        )

    def append_event(
        self,
        connection: sqlite3.Connection,
        *,
        event_id: str,
        run_id: str,
        command_id: str,
        revision: int,
        event_type: str,
        payload: Mapping[str, Any],
        recorded_at: str,
        contract_version: int | None = None,
        route_id: str | None = None,
        claim_id: str | None = None,
        attempt_id: str | None = None,
    ) -> int:
        cursor = connection.execute(
            "INSERT INTO events("
            "event_id, run_id, command_id, revision, event_type, payload_json, "
            "contract_version, route_id, claim_id, attempt_id, recorded_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                event_id,
                run_id,
                command_id,
                revision,
                event_type,
                _json(payload),
                contract_version,
                route_id,
                claim_id,
                attempt_id,
                recorded_at,
            ),
        ).lastrowid
        if cursor is None:
            raise StorageError("event insert did not return a cursor")
        return int(cursor)

    def advance_revision(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        expected_revision: int,
        *,
        updated_at: str,
        status: str | None = None,
    ) -> int:
        next_revision = expected_revision + 1
        if status is None:
            result = connection.execute(
                "UPDATE runs SET revision = ?, updated_at = ? WHERE run_id = ? AND revision = ?",
                (next_revision, updated_at, run_id, expected_revision),
            )
        else:
            result = connection.execute(
                "UPDATE runs SET revision = ?, status = ?, updated_at = ? "
                "WHERE run_id = ? AND revision = ?",
                (next_revision, status, updated_at, run_id, expected_revision),
            )
        if result.rowcount != 1:
            raise RevisionConflict("run revision compare-and-set failed")
        return next_revision

    def insert_artifact(
        self, connection: sqlite3.Connection, artifact: Mapping[str, Any]
    ) -> dict[str, Any]:
        values = (
            str(artifact["artifact_id"]),
            str(artifact["sha256"]),
            int(artifact["byte_count"]),
            str(artifact["media_type"]),
            str(artifact["cas_relpath"]),
            str(artifact.get("ingest_state", "COMMITTED")),
            artifact.get("quarantine_code"),
            artifact.get("source_name"),
            artifact.get("original_path"),
            artifact.get("line_count"),
            str(artifact["created_at"]),
            artifact.get("committed_at", artifact.get("created_at")),
        )
        connection.execute(
            "INSERT OR IGNORE INTO artifacts("
            "artifact_id, sha256, byte_count, media_type, cas_relpath, ingest_state, "
            "quarantine_code, source_name, original_path, line_count, created_at, committed_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            values,
        )
        row = connection.execute(
            "SELECT artifact_id, sha256, byte_count, media_type, cas_relpath, ingest_state, "
            "quarantine_code, source_name, original_path, line_count, created_at, committed_at "
            "FROM artifacts WHERE artifact_id = ? OR sha256 = ?",
            (values[0], values[1]),
        ).fetchone()
        if row is None:
            raise StorageError("artifact insert was not observable")
        if (
            str(row["sha256"]) != values[1]
            or int(row["byte_count"]) != values[2]
            or str(row["media_type"]) != values[3]
            or str(row["cas_relpath"]) != values[4]
        ):
            raise StorageConflict("artifact identity or digest was reused with different content")
        return dict(row)

    def get_artifact(
        self,
        artifact_id: str,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> dict[str, Any] | None:
        with self._reader(connection) as conn:
            row = conn.execute(
                "SELECT * FROM artifacts WHERE artifact_id = ?", (artifact_id,)
            ).fetchone()
        return dict(row) if row is not None else None

    def get_artifact_by_sha256(
        self,
        sha256: str,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> dict[str, Any] | None:
        with self._reader(connection) as conn:
            row = conn.execute("SELECT * FROM artifacts WHERE sha256 = ?", (sha256,)).fetchone()
        return dict(row) if row is not None else None

    def link_artifact(
        self,
        connection: sqlite3.Connection,
        *,
        run_id: str,
        artifact_id: str,
        logical_name: str,
        role: str,
        linked_at: str,
    ) -> None:
        """Link already committed immutable bytes to one run under a stable logical name."""

        connection.execute(
            "INSERT OR IGNORE INTO run_artifacts(run_id,artifact_id,logical_name,role,linked_at) "
            "VALUES (?,?,?,?,?)",
            (run_id, artifact_id, logical_name, role, linked_at),
        )
        row = connection.execute(
            "SELECT artifact_id, role FROM run_artifacts WHERE run_id = ? AND logical_name = ?",
            (run_id, logical_name),
        ).fetchone()
        if row is None or str(row["artifact_id"]) != artifact_id or str(row["role"]) != role:
            raise StorageConflict("run artifact logical name was reused with different content")

    def artifact_ids_by_relpath(self) -> dict[str, str]:
        with self._reader(None) as connection:
            rows = connection.execute(
                "SELECT cas_relpath, artifact_id FROM artifacts WHERE ingest_state = 'COMMITTED'"
            ).fetchall()
        return {str(row["cas_relpath"]): str(row["artifact_id"]) for row in rows}

    def guard_snapshot(
        self,
        run_id: str,
        *,
        connection: sqlite3.Connection | None = None,
    ) -> dict[str, Any]:
        """Return the complete typed context required by the pure TransitionGuard."""

        with self._reader(connection) as conn:
            run_row = conn.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if run_row is None:
                raise RunNotFound("run not found")

            def rows(table: str, order_by: str) -> list[dict[str, Any]]:
                # Table and ordering names are constants owned by this module, never user input.
                result = conn.execute(
                    f"SELECT * FROM {table} WHERE run_id = ? ORDER BY {order_by}",
                    (run_id,),
                ).fetchall()
                return [dict(item) for item in result]

            contracts = rows("contract_versions", "version")
            claims = rows("claims", "claim_id")
            routes = rows("routes", "route_id")
            attempts = rows("attempts", "attempt_id")
            leases = [
                dict(item)
                for item in conn.execute(
                    "SELECT l.* FROM leases l JOIN attempts a ON a.attempt_id = l.attempt_id "
                    "WHERE a.run_id = ? ORDER BY l.lease_id",
                    (run_id,),
                ).fetchall()
            ]
            artifacts = [
                dict(item)
                for item in conn.execute(
                    "SELECT a.*, ra.logical_name, ra.role FROM artifacts a "
                    "JOIN run_artifacts ra ON ra.artifact_id = a.artifact_id "
                    "WHERE ra.run_id = ? ORDER BY a.artifact_id",
                    (run_id,),
                ).fetchall()
            ]
            literature = rows("literature_records", "literature_record_id")
            peer_reviews = rows("peer_reviews", "review_id")
            quality_reviews = rows("quality_reviews", "quality_review_id")
            bridges = rows("bridges", "bridge_id")
            edges = rows("claim_edges", "edge_id")
            obligations = rows("composition_obligations", "obligation_id")
            witnesses = rows("closure_witnesses", "witness_id")
            bindings = rows("execution_bindings", "binding_id")
            failures = rows("failure_records", "failure_record_id")
            evidence = [
                dict(item)
                for item in conn.execute(
                    "SELECT e.*,r.root_kind,r.verifier_profile_id,c.capability_id AS "
                    "submitter_capability_id,c.subject_id AS submitter_subject_id "
                    "FROM evidence e JOIN evidence_roots r "
                    "ON r.evidence_root_id = e.evidence_root_id "
                    "JOIN commands cmd ON cmd.command_id = e.submitted_by_command_id "
                    "JOIN capabilities c ON c.capability_id = cmd.capability_id "
                    "WHERE e.run_id = ? ORDER BY e.evidence_id",
                    (run_id,),
                ).fetchall()
            ]
            lean_feedback = rows("lean_feedback_events", "lean_feedback_id")
            budget_events = rows("budget_events", "budget_event_id")
            interrupt = conn.execute(
                "SELECT payload_json FROM events WHERE run_id = ? "
                "AND event_type = 'RUN_INTERRUPTED' ORDER BY event_seq DESC LIMIT 1",
                (run_id,),
            ).fetchone()
            fuse = conn.execute(
                "SELECT 1 FROM budget_events WHERE run_id = ? AND event_kind = 'FUSE_TRIP' LIMIT 1",
                (run_id,),
            ).fetchone()

        json_fields: dict[str, tuple[str, ...]] = {
            "contracts": (
                "contract_json",
                "defect_evidence_json",
                "impact_analysis_json",
                "approved_by_json",
            ),
            "claims": ("normalized_statement_json",),
            "routes": ("budget_policy_json",),
            "attempts": ("allowed_write_set_json",),
            "artifacts": (),
            "literature": ("scope_json", "query_families_json"),
            "peer_reviews": (
                "independence_profile_json",
                "checklist_json",
                "source_graph_json",
            ),
            "quality_reviews": ("dimensions_json",),
            "bridges": (
                "term_mapping_json",
                "forward_obligations_json",
                "reverse_obligations_json",
                "loss_accounting_json",
            ),
            "edges": (),
            "obligations": ("child_claim_ids_json", "local_domain_json", "missing_conditions_json"),
            "closure_witnesses": (
                "discharged_obligations_json",
                "open_obligations_json",
                "edge_justifications_json",
                "bridge_dependencies_json",
                "verification_refs_json",
                "human_attestation_review_ids_json",
            ),
            "bindings": ("external_session_ids_json",),
            "failures": ("applicability_json", "novelty_delta_json"),
            "evidence": ("scope_json", "provenance_json"),
            "lean_feedback": ("diagnostic_json",),
            "budget_events": ("provider_usage_json",),
        }
        collections: dict[str, list[dict[str, Any]]] = {
            "contracts": contracts,
            "claims": claims,
            "routes": routes,
            "attempts": attempts,
            "leases": leases,
            "artifacts": artifacts,
            "literature": literature,
            "peer_reviews": peer_reviews,
            "quality_reviews": quality_reviews,
            "bridges": bridges,
            "edges": edges,
            "obligations": obligations,
            "closure_witnesses": witnesses,
            "bindings": bindings,
            "failures": failures,
            "evidence": evidence,
            "lean_feedback": lean_feedback,
            "budget_events": budget_events,
        }
        for key, fields in json_fields.items():
            for record in collections[key]:
                for field in fields:
                    raw = record.pop(field, None)
                    plain = field.removesuffix("_json")
                    record[plain] = json.loads(raw) if raw is not None else None

        for contract in contracts:
            contract["contract_version"] = contract["version"]
            contract.update(contract.get("contract") or {})
        for claim in claims:
            claim["lifecycle"] = claim["lifecycle_status"]
            claim["route"] = claim["route_result"]
            claim["machine"] = claim["machine_verdict"]
            claim["semantic"] = claim["semantic_verdict"]
            claim["peer"] = claim["peer_verdict"]
            claim["quality"] = claim["quality_verdict"]
            claim["closure"] = claim["closure_state"]
        for route in routes:
            route["budget_policy"] = route.pop("budget_policy", None)
        for attempt in attempts:
            attempt["allowed_write_set"] = attempt.pop("allowed_write_set", None)
        for artifact in artifacts:
            artifact["status"] = artifact["ingest_state"]
        for item in evidence:
            item["status"] = item["ingest_status"]
            provenance = item.get("provenance")
            if isinstance(provenance, Mapping) and "replay" in provenance:
                item["replay"] = provenance["replay"]

        current_version = int(run_row["current_contract_version"])
        current_contract = next(
            (item for item in contracts if int(item["version"]) == current_version), {}
        )
        projection: dict[str, Any] = {
            "contract": current_contract,
            **collections,
            "open_obligation_ids": [
                str(item["obligation_id"]) for item in obligations if item.get("status") == "OPEN"
            ],
            "reviews": peer_reviews,
            "last_interrupt": json.loads(str(interrupt[0])) if interrupt is not None else {},
            "budget_fuse_tripped": fuse is not None,
            "defect_proposals": [item for item in contracts if item["status"] == "DEFECT_PROPOSED"],
        }
        return {
            "run_id": str(run_row["run_id"]),
            "stable_project_id": str(run_row["stable_project_id"]),
            "status": str(run_row["status"]),
            "revision": int(run_row["revision"]),
            "current_contract_version": current_version,
            "root_claim_id": run_row["root_claim_id"],
            "projection": projection,
        }

    def inspect_snapshot(self, run_id: str) -> dict[str, Any]:
        with self._reader(None) as connection:
            run = connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if run is None:
                raise RunNotFound("run not found")
            contract = connection.execute(
                "SELECT statement_hash, status FROM contract_versions "
                "WHERE run_id = ? AND version = ?",
                (run_id, run["current_contract_version"]),
            ).fetchone()
            claim_rows = connection.execute(
                "SELECT claim_id, statement_hash, lifecycle_status, route_result, "
                "machine_verdict, semantic_verdict, peer_verdict, quality_verdict, closure_state "
                "FROM claims WHERE run_id = ? ORDER BY claim_id",
                (run_id,),
            ).fetchall()
            route_rows = connection.execute(
                "SELECT route_id, status, first_failed_obligation_id FROM routes "
                "WHERE run_id = ? ORDER BY route_id",
                (run_id,),
            ).fetchall()
            obligations = connection.execute(
                "SELECT obligation_id FROM composition_obligations "
                "WHERE run_id = ? AND status = 'OPEN' ORDER BY obligation_id",
                (run_id,),
            ).fetchall()
            attempts = connection.execute(
                "SELECT a.attempt_id, a.route_id, a.status, l.lease_id, "
                "l.expires_at AS lease_expires_at "
                "FROM attempts a LEFT JOIN leases l ON l.attempt_id = a.attempt_id "
                "AND l.status = 'ACTIVE' WHERE a.run_id = ? "
                "AND a.status IN ('QUEUED','RUNNING','PAUSED') ORDER BY a.attempt_id",
                (run_id,),
            ).fetchall()
            budget_rows = connection.execute(
                "SELECT resource_kind, event_kind, COALESCE(SUM(amount_microunits), 0) AS amount, "
                "SUM(CASE WHEN event_kind='UNKNOWN_COST' THEN 1 ELSE 0 END) AS unknown_count "
                "FROM budget_events WHERE run_id = ? GROUP BY resource_kind, event_kind",
                (run_id,),
            ).fetchall()
            component_rows = connection.execute(
                "SELECT resource_kind,event_kind,amount_microunits,provider_usage_json "
                "FROM budget_events WHERE run_id = ? ORDER BY budget_event_id",
                (run_id,),
            ).fetchall()
            artifact_rows = connection.execute(
                "SELECT a.artifact_id,a.sha256,a.byte_count,a.media_type,ra.logical_name,ra.role "
                "FROM run_artifacts ra JOIN artifacts a ON a.artifact_id = ra.artifact_id "
                "WHERE ra.run_id = ? ORDER BY ra.role,ra.logical_name,a.artifact_id",
                (run_id,),
            ).fetchall()
            evidence_rows = connection.execute(
                "SELECT evidence_id,claim_id,contract_version,evidence_type,evidence_strength,"
                "ingest_status,artifact_id FROM evidence WHERE run_id = ? ORDER BY evidence_id",
                (run_id,),
            ).fetchall()
            review_rows = connection.execute(
                "SELECT review_id,claim_id,contract_version,verdict,review_artifact_id "
                "FROM peer_reviews WHERE run_id = ? ORDER BY review_id",
                (run_id,),
            ).fetchall()
            quality_review_rows = connection.execute(
                "SELECT quality_review_id,claim_id,contract_version,verdict,review_artifact_id,"
                "training_pool FROM quality_reviews WHERE run_id = ? ORDER BY quality_review_id",
                (run_id,),
            ).fetchall()
            witness_rows = connection.execute(
                "SELECT witness_id,parent_claim_id,contract_version,selected_subgraph_digest,"
                "composition_mode,status FROM closure_witnesses "
                "WHERE run_id = ? ORDER BY witness_id",
                (run_id,),
            ).fetchall()
            edge_rows = connection.execute(
                "SELECT edge_id,from_claim_id,to_claim_id,edge_kind,direction,status "
                "FROM claim_edges WHERE run_id = ? ORDER BY edge_id",
                (run_id,),
            ).fetchall()
            obligation_rows = connection.execute(
                "SELECT obligation_id,parent_claim_id,status,displacement_status "
                "FROM composition_obligations WHERE run_id = ? ORDER BY obligation_id",
                (run_id,),
            ).fetchall()
            bridge_rows = connection.execute(
                "SELECT bridge_id,source_claim_id,target_claim_id,directionality "
                "FROM bridges WHERE run_id = ? ORDER BY bridge_id",
                (run_id,),
            ).fetchall()
            literature_rows = connection.execute(
                "SELECT literature_record_id,claim_id,status,relation,cutoff_date "
                "FROM literature_records WHERE run_id = ? ORDER BY literature_record_id",
                (run_id,),
            ).fetchall()
            bindings = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM execution_bindings WHERE run_id = ? ORDER BY binding_id",
                    (run_id,),
                ).fetchall()
            ]
            lean_feedback = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM lean_feedback_events WHERE run_id = ? "
                    "ORDER BY lean_feedback_id",
                    (run_id,),
                ).fetchall()
            ]
            budget_events = [
                dict(row)
                for row in connection.execute(
                    "SELECT * FROM budget_events WHERE run_id = ? ORDER BY budget_event_id",
                    (run_id,),
                ).fetchall()
            ]
            cursor_row = connection.execute(
                "SELECT COALESCE(MAX(event_seq), 0) FROM events WHERE run_id = ?", (run_id,)
            ).fetchone()
        claims = [
            {
                "claim_id": str(row["claim_id"]),
                "statement_hash": str(row["statement_hash"]),
                "lifecycle": str(row["lifecycle_status"]),
                "route": str(row["route_result"]),
                "machine": str(row["machine_verdict"]),
                "semantic": str(row["semantic_verdict"]),
                "peer": str(row["peer_verdict"]),
                "quality": str(row["quality_verdict"]),
                "closure": str(row["closure_state"]),
            }
            for row in claim_rows
        ]
        budget: dict[str, dict[str, int]] = {}
        key_by_kind = {"RESERVATION": "reserved", "ACTUAL": "actual", "REFUND": "refunded"}
        for row in budget_rows:
            item = budget.setdefault(
                str(row["resource_kind"]),
                {"reserved": 0, "actual": 0, "refunded": 0, "unknown_count": 0},
            )
            event_kind = str(row["event_kind"])
            if event_kind in key_by_kind:
                item[key_by_kind[event_kind]] += int(row["amount"])
            item["unknown_count"] += int(row["unknown_count"])
        component_usage: dict[str, dict[str, int]] = {}
        for row in component_rows:
            try:
                usage = json.loads(str(row["provider_usage_json"]))
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(usage, Mapping):
                continue
            component = usage.get("component")
            if not isinstance(component, str) or not component:
                continue
            item = component_usage.setdefault(
                component,
                {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "reasoning_tokens": 0,
                    "cache_read_tokens": 0,
                    "cache_write_tokens": 0,
                    "total_tokens": 0,
                    "wall_time_ms": 0,
                    "unknown_count": 0,
                },
            )
            if str(row["event_kind"]) == "UNKNOWN_COST":
                item["unknown_count"] += 1
            for name in (
                "input_tokens",
                "output_tokens",
                "reasoning_tokens",
                "cache_read_tokens",
                "cache_write_tokens",
                "total_tokens",
                "wall_time_ms",
            ):
                value = usage.get(name)
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    item[name] += value
        terminal_values = {"ROUTE_LOCAL", "ROUTE_PROVED", "REFUTED", "PREVIOUSLY_KNOWN"}
        return {
            "run_id": str(run["run_id"]),
            "stable_project_id": str(run["stable_project_id"]),
            "status": str(run["status"]),
            "revision": int(run["revision"]),
            "current_contract_version": int(run["current_contract_version"]),
            "contract": dict(contract) if contract is not None else None,
            "root_claim_id": run["root_claim_id"],
            "artifacts": [dict(row) for row in artifact_rows],
            "evidence": [dict(row) for row in evidence_rows],
            "peer_reviews": [dict(row) for row in review_rows],
            "quality_reviews": [dict(row) for row in quality_review_rows],
            "closure_witnesses": [dict(row) for row in witness_rows],
            "edges": [dict(row) for row in edge_rows],
            "obligations": [dict(row) for row in obligation_rows],
            "bridges": [dict(row) for row in bridge_rows],
            "literature": [dict(row) for row in literature_rows],
            "claims": claims,
            "routes": [dict(row) for row in route_rows],
            "open_obligation_ids": [str(row[0]) for row in obligations],
            "active_attempts": [dict(row) for row in attempts],
            "bindings": bindings,
            "lean_feedback": lean_feedback,
            "budget_events": budget_events,
            "budget_summary": budget,
            "component_usage": component_usage,
            "terminal_claim_ids": [
                item["claim_id"] for item in claims if item["route"] in terminal_values
            ],
            "last_cursor": int(cursor_row[0]) if cursor_row is not None else 0,
        }

    def event_page(self, run_id: str, after_cursor: int, limit: int) -> dict[str, Any]:
        if after_cursor < 0:
            raise ValueError("after_cursor must be nonnegative")
        if not 1 <= limit <= 500:
            raise ValueError("limit must be between 1 and 500")
        with self._reader(None) as connection:
            exists = connection.execute("SELECT 1 FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if exists is None:
                raise RunNotFound("run not found")
            rows = connection.execute(
                "SELECT event_seq, event_id, revision, event_type, contract_version, route_id, "
                "claim_id, attempt_id, payload_json, recorded_at FROM events "
                "WHERE run_id = ? AND event_seq > ? ORDER BY event_seq LIMIT ?",
                (run_id, after_cursor, limit + 1),
            ).fetchall()
        has_more = len(rows) > limit
        page_rows = rows[:limit]
        events = [
            {
                "cursor": int(row["event_seq"]),
                "event_id": str(row["event_id"]),
                "revision": int(row["revision"]),
                "type": str(row["event_type"]),
                "contract_version": row["contract_version"],
                "route_id": row["route_id"],
                "claim_id": row["claim_id"],
                "attempt_id": row["attempt_id"],
                "payload": json.loads(str(row["payload_json"])),
                "recorded_at": str(row["recorded_at"]),
            }
            for row in page_rows
        ]
        next_cursor = int(page_rows[-1]["event_seq"]) if page_rows else after_cursor
        return {
            "run_id": run_id,
            "after_cursor": after_cursor,
            "events": events,
            "next_cursor": next_cursor,
            "has_more": has_more,
        }

    @staticmethod
    def _handle(row: sqlite3.Row) -> dict[str, Any]:
        return {
            "run_id": str(row["run_id"]),
            "revision": int(row["revision"]),
            "status": str(row["status"]),
            "current_contract_version": int(row["current_contract_version"]),
            "created_at": str(row["created_at"]),
        }
