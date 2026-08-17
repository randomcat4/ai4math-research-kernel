"""SQLite persistence hidden behind the ResearchKernel seam."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import sqlite3
import uuid
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

    def host_execution_scope(self, *, run_id: str, attempt_id: str, now: str) -> dict[str, Any]:
        """Resolve the only authority-bearing execution scope from current database state."""

        with self._reader(None) as connection:
            row = connection.execute(
                "SELECT r.run_id,r.current_contract_version AS contract_version,"
                "r.root_claim_id,rt.route_id,rt.target_claim_id AS claim_id,c.statement_hash,"
                "a.attempt_id,a.input_snapshot_digest,b.binding_id,b.adapter_name,"
                "b.adapter_version,b.source_commit,b.environment_profile_id,"
                "b.invocation_nonce,art.sha256 AS invocation_artifact_sha256 "
                "FROM runs r JOIN routes rt ON rt.run_id=r.run_id "
                "JOIN claims c ON c.claim_id=rt.target_claim_id "
                "JOIN attempts a ON a.route_id=rt.route_id "
                "JOIN execution_bindings b ON b.attempt_id=a.attempt_id "
                "JOIN artifacts art ON art.artifact_id=b.invocation_artifact_id "
                "JOIN leases l ON l.attempt_id=a.attempt_id "
                "WHERE r.run_id=? AND a.attempt_id=? AND r.status='RUNNING' "
                "AND rt.status='ACTIVE' AND c.lifecycle_status='ACTIVE' "
                "AND c.contract_version=r.current_contract_version "
                "AND rt.contract_version=r.current_contract_version "
                "AND a.status='RUNNING' AND l.status='ACTIVE' AND l.expires_at>?",
                (run_id, attempt_id, now),
            ).fetchone()
        if row is None:
            raise StorageConflict(
                "attempt is not an active canonical-root/current-contract host execution"
            )
        return dict(row)

    def preflight_host_budget(
        self,
        *,
        run_id: str,
        attempt_id: str,
        required_resources: Sequence[str],
        budget_limits: Mapping[str, int],
    ) -> None:
        """Require a live reservation and available hard cap before external execution."""

        with self._reader(None) as connection:
            for resource in required_resources:
                limit = budget_limits.get(resource)
                if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
                    raise StorageConflict(f"host budget hard limit is missing for {resource}")
                row = connection.execute(
                    "SELECT "
                    "COALESCE(SUM(CASE WHEN event_kind IN ('ACTUAL','RESERVATION') "
                    "THEN amount_microunits WHEN event_kind='REFUND' "
                    "THEN -amount_microunits ELSE 0 END),0),"
                    "COALESCE(SUM(CASE WHEN attempt_id=? AND event_kind='RESERVATION' "
                    "THEN amount_microunits WHEN attempt_id=? AND event_kind='REFUND' "
                    "THEN -amount_microunits ELSE 0 END),0) "
                    "FROM budget_events WHERE run_id=? AND resource_kind=? "
                    "AND COALESCE(json_extract(provider_usage_json,'$._rk_trust'),'CURRENT') "
                    "<> 'LEGACY_UNTRUSTED'",
                    (attempt_id, attempt_id, run_id, resource),
                ).fetchone()
                assert row is not None
                consumed, attempt_reserved = int(row[0]), int(row[1])
                if attempt_reserved <= 0:
                    raise StorageConflict(
                        f"host execution requires a preflight {resource} reservation"
                    )
                if consumed > limit:
                    raise StorageConflict(f"host execution preflight exceeds {resource} budget")

    def record_component_usage_atomic(
        self,
        *,
        run_id: str,
        request_id: str,
        component: str,
        usage: Mapping[str, Any],
        capability: VerifiedCapability,
        command_id: str,
        event_id: str,
        trace_id: str,
        now: str,
        budget_limits: Mapping[str, int],
    ) -> tuple[str, ...]:
        """Append host-observed orchestration usage without opening a public forged-actual path."""

        if not capability.allows("RecordComponentUsage", run_id):
            raise StorageConflict("component accounting capability is outside this run scope")
        self.ensure_capability(capability)
        normalized = dict(usage)
        digest = hashlib.sha256(
            _json({"component": component, "usage": normalized}).encode("utf-8")
        ).hexdigest()
        accounting_request_id = f"component-usage:{request_id}"
        with self.transaction() as connection:
            previous = connection.execute(
                "SELECT request_digest FROM commands WHERE run_id=? AND request_id=?",
                (run_id, accounting_request_id),
            ).fetchone()
            if previous is not None:
                if str(previous["request_digest"]) != digest:
                    raise StorageConflict("component usage request was reused with different facts")
                return ()
            run = connection.execute(
                "SELECT revision,current_contract_version,status FROM runs WHERE run_id=?",
                (run_id,),
            ).fetchone()
            if run is None:
                raise RunNotFound(run_id)
            if str(run["status"]) not in {"OPEN", "RUNNING", "PAUSED"}:
                raise StorageConflict("component usage cannot be appended to a closed run")
            rows: list[tuple[str, str, int | None, str]] = []
            for name, resource, multiplier, unit in (
                ("input_tokens", "INPUT_TOKEN", 1_000_000, "microtoken"),
                ("output_tokens", "OUTPUT_TOKEN", 1_000_000, "microtoken"),
                ("wall_time_ms", "WALL_SECOND", 1_000, "microsecond"),
            ):
                value = normalized.get(name)
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    rows.append((resource, "ACTUAL", value * multiplier, unit))
                elif normalized.get(f"{name}_applicable", True) is True:
                    rows.append((resource, "UNKNOWN_COST", None, "unknown"))
            if normalized.get("cost_unknown") is True:
                rows.append(("API_MICRO_CURRENCY", "UNKNOWN_COST", None, "unknown"))
            revision_before = int(run["revision"])
            revision_after = revision_before + 1
            trusted_usage = {
                **normalized,
                "component": component,
                "_rk_trust": "ORCHESTRATOR_OBSERVED",
                "_rk_request_id": request_id,
            }
            overruns: list[str] = []
            for resource, kind, amount, _unit in rows:
                if kind != "ACTUAL" or amount is None:
                    continue
                limit = budget_limits.get(resource)
                consumed = connection.execute(
                    "SELECT COALESCE(SUM(amount_microunits),0) FROM budget_events "
                    "WHERE run_id=? AND resource_kind=? AND event_kind='ACTUAL'",
                    (run_id, resource),
                ).fetchone()
                if (
                    not isinstance(limit, int)
                    or isinstance(limit, bool)
                    or limit <= 0
                    or int(consumed[0]) + amount > limit
                ):
                    overruns.append(resource)
            receipt = {
                "schema_version": "rk.receipt.v1",
                "request_id": accounting_request_id,
                "command_id": command_id,
                "run_id": run_id,
                "accepted": True,
                "revision_before": revision_before,
                "revision_after": revision_after,
                "event_ids": [event_id],
                "artifact_ids": [],
                "rejection_code": None,
                "missing_conditions": [],
                "decided_at": now,
            }
            self.record_command(
                connection,
                command_id=command_id,
                run_id=run_id,
                request_id=accounting_request_id,
                command_type="RecordComponentUsage",
                request_digest=digest,
                expected_revision=revision_before,
                capability_id=capability.capability_id,
                accepted=True,
                revision_before=revision_before,
                revision_after=revision_after,
                rejection_code=None,
                missing_conditions=[],
                receipt=receipt,
                trace_id=trace_id,
                decided_at=now,
            )
            self.append_event(
                connection,
                event_id=event_id,
                run_id=run_id,
                command_id=command_id,
                revision=revision_after,
                event_type="COMPONENT_USAGE_RECORDED",
                payload={"component": component, "resources": [row[0] for row in rows]},
                recorded_at=now,
                contract_version=int(run["current_contract_version"]),
            )
            for resource, kind, amount, unit in rows:
                connection.execute(
                    "INSERT INTO budget_events(budget_event_id,run_id,command_id,revision,"
                    "event_kind,resource_kind,amount_microunits,unit,currency,"
                    "provider_usage_json,recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        self._host_id(accounting_request_id, resource),
                        run_id,
                        command_id,
                        revision_after,
                        kind,
                        resource,
                        amount,
                        unit,
                        None,
                        _json(trusted_usage),
                        now,
                    ),
                )
            for resource in overruns:
                connection.execute(
                    "INSERT INTO budget_events(budget_event_id,run_id,command_id,revision,"
                    "event_kind,resource_kind,amount_microunits,unit,currency,"
                    "provider_usage_json,recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        self._host_id(accounting_request_id, f"fuse:{resource}"),
                        run_id,
                        command_id,
                        revision_after,
                        "FUSE_TRIP",
                        resource,
                        0,
                        "microunit",
                        None,
                        _json({**trusted_usage, "budget_overrun": resource}),
                        now,
                    ),
                )
            status = "PAUSED" if overruns else str(run["status"])
            result = connection.execute(
                "UPDATE runs SET revision=?,status=?,updated_at=? WHERE run_id=? AND revision=?",
                (revision_after, status, now, run_id, revision_before),
            )
            if result.rowcount != 1:
                raise RevisionConflict("component accounting lost the revision compare-and-set")
        return tuple(overruns)

    def claim_host_execution(
        self,
        *,
        scope: Mapping[str, Any],
        capability: VerifiedCapability,
        claim_token: str,
        request_hash: str,
        component: str,
        service_instance_id: str,
        now: str,
        recover_after: str,
    ) -> None:
        """Atomically burn the one execution right before any provider side effect."""

        if not capability.allows("HostExecute", str(scope["run_id"])):
            raise StorageConflict("host capability is outside this run scope")
        self.ensure_capability(capability)
        try:
            with self.transaction() as connection:
                current = connection.execute(
                    "SELECT a.status,r.status,rt.status,c.lifecycle_status,"
                    "EXISTS(SELECT 1 FROM leases l WHERE l.attempt_id=a.attempt_id "
                    "AND l.status='ACTIVE' AND l.expires_at>?) "
                    "FROM attempts a JOIN routes rt ON rt.route_id=a.route_id "
                    "JOIN runs r ON r.run_id=a.run_id "
                    "JOIN claims c ON c.claim_id=rt.target_claim_id "
                    "WHERE a.attempt_id=? AND r.run_id=? AND rt.route_id=?",
                    (now, scope["attempt_id"], scope["run_id"], scope["route_id"]),
                ).fetchone()
                if current is None or tuple(current) != (
                    "RUNNING", "RUNNING", "ACTIVE", "ACTIVE", 1
                ):
                    raise StorageConflict("host execution state changed before provider call")
                connection.execute(
                    "INSERT INTO host_execution_claims("
                    "attempt_id,binding_id,claim_token,run_id,route_id,request_hash,component,"
                    "service_instance_id,claimed_at,heartbeat_at,recover_after) "
                    "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (scope["attempt_id"], scope["binding_id"], claim_token,scope["run_id"],
                     scope["route_id"],request_hash,component,service_instance_id,now,now,
                     recover_after),
                )
        except sqlite3.IntegrityError as error:
            raise StorageConflict("attempt already has a host execution claim") from error

    def record_host_execution_atomic(
        self,
        *,
        scope: Mapping[str, Any],
        expected_scope: Mapping[str, Any],
        receipt: Mapping[str, Any],
        capability: VerifiedCapability,
        command_id: str,
        event_id: str,
        trace_id: str,
        now: str,
        budget_limits: Mapping[str, int],
        claim_token: str,
        authority_block_reasons: tuple[str, ...] = (),
    ) -> tuple[str, ...]:
        """Persist one host result and its accounting in a single serializable transaction."""

        payload = receipt.get("payload")
        signature = receipt.get("signature")
        if not isinstance(payload, Mapping) or not isinstance(signature, str):
            raise StorageConflict("host receipt is malformed")
        if not capability.allows("HostExecute", str(scope["run_id"])):
            raise StorageConflict("host capability is outside this run scope")
        self.ensure_capability(capability)
        with self.transaction() as connection:
            current = connection.execute(
                "SELECT r.run_id,r.revision,r.status,r.current_contract_version,r.root_claim_id,"
                "rt.route_id,rt.status AS route_status,rt.target_claim_id,c.lifecycle_status,"
                "c.claim_kind,c.statement_hash,a.attempt_id,a.status AS attempt_status,"
                "a.input_snapshot_digest,"
                "b.binding_id,b.adapter_name,b.adapter_version,b.source_commit,"
                "b.environment_profile_id,b.invocation_nonce,"
                "art.sha256 AS invocation_artifact_sha256,"
                "EXISTS(SELECT 1 FROM leases l WHERE l.attempt_id=a.attempt_id "
                "AND l.status='ACTIVE' AND l.expires_at>?) AS has_lease "
                "FROM runs r JOIN routes rt ON rt.run_id=r.run_id "
                "JOIN claims c ON c.claim_id=rt.target_claim_id "
                "JOIN attempts a ON a.route_id=rt.route_id "
                "JOIN execution_bindings b ON b.attempt_id=a.attempt_id "
                "JOIN artifacts art ON art.artifact_id=b.invocation_artifact_id "
                "WHERE r.run_id=? AND a.attempt_id=?",
                (now, scope["run_id"], scope["attempt_id"]),
            ).fetchone()
            if current is None:
                raise StorageConflict("host execution scope disappeared")
            claimed = connection.execute(
                "SELECT 1 FROM host_execution_claims WHERE attempt_id=? AND binding_id=? "
                "AND claim_token=? AND completed_at IS NULL",
                (scope["attempt_id"], scope["binding_id"], claim_token),
            ).fetchone()
            if claimed is None:
                raise StorageConflict("host execution claim is missing or already completed")
            comparisons = {
                "run_id": current["run_id"],
                "route_id": current["route_id"],
                "attempt_id": current["attempt_id"],
                "binding_id": current["binding_id"],
                "claim_id": current["target_claim_id"],
                "contract_version": current["current_contract_version"],
                "statement_hash": current["statement_hash"],
                "adapter_name": current["adapter_name"],
                "adapter_version": current["adapter_version"],
                "source_commit": current["source_commit"],
                "environment_profile_id": current["environment_profile_id"],
                "invocation_nonce": current["invocation_nonce"],
                "input_snapshot_digest": current["input_snapshot_digest"],
                "invocation_artifact_sha256": current["invocation_artifact_sha256"],
            }
            block_reasons = list(authority_block_reasons)
            if any(expected_scope.get(key) != value for key, value in comparisons.items()):
                block_reasons.append("SCOPE_DRIFT")
            if (
                current["status"] != "RUNNING"
                or current["route_status"] != "ACTIVE"
                or current["lifecycle_status"] != "ACTIVE"
                or current["attempt_status"] != "RUNNING"
                or not current["has_lease"]
            ):
                block_reasons.append("EXECUTION_STATE_DRIFT")
            revision_before = int(current["revision"])
            revision_after = revision_before + 1
            usage = payload.get("provider_usage")
            if not isinstance(usage, Mapping):
                raise StorageConflict("host receipt provider usage is malformed")
            budget_rows = self._host_budget_rows(payload, usage)
            overruns = self._host_budget_overruns(
                connection,
                scope["run_id"],
                scope["attempt_id"],
                budget_rows,
                budget_limits,
            )
            block_reasons.extend(f"BUDGET_OVERRUN:{resource}" for resource in overruns)
            command_receipt = {
                "schema_version": "rk.receipt.v1",
                "request_id": str(payload["receipt_id"]),
                "command_id": command_id,
                "run_id": scope["run_id"],
                "accepted": True,
                "revision_before": revision_before,
                "revision_after": revision_after,
                "event_ids": [event_id],
                "artifact_ids": [],
                "rejection_code": None,
                "missing_conditions": [],
                "decided_at": now,
            }
            self.record_command(
                connection,
                command_id=command_id,
                run_id=str(scope["run_id"]),
                request_id=str(payload["receipt_id"]),
                command_type="HostExecute",
                request_digest=str(payload["request_hash"]),
                expected_revision=revision_before,
                capability_id=capability.capability_id,
                accepted=True,
                revision_before=revision_before,
                revision_after=revision_after,
                rejection_code=None,
                missing_conditions=[],
                receipt=command_receipt,
                trace_id=trace_id,
                decided_at=now,
            )
            event_payload = {
                "receipt_id": payload["receipt_id"],
                "receipt_nonce": payload["receipt_nonce"],
                "adapter_name": payload["adapter_name"],
                "status": payload["status"],
                "request_hash": payload["request_hash"],
                "result_hash": payload["result_hash"],
            }
            self.append_event(
                connection,
                event_id=event_id,
                run_id=str(scope["run_id"]),
                command_id=command_id,
                revision=revision_after,
                event_type="HOST_EXECUTION_RECORDED",
                payload=event_payload,
                recorded_at=now,
                contract_version=int(scope["contract_version"]),
                route_id=str(scope["route_id"]),
                claim_id=str(scope["claim_id"]),
                attempt_id=str(scope["attempt_id"]),
            )
            columns = (
                "receipt_id", "receipt_nonce", "service_instance_id", "run_id", "route_id",
                "attempt_id", "binding_id", "claim_id", "contract_version", "statement_hash",
                "environment_profile_id", "adapter_name", "adapter_version", "source_commit",
                "toolchain", "binary_sha256", "request_hash", "result_hash", "source_sha256",
                "output_sha256", "input_snapshot_digest", "environment_digest", "mount_digest",
                "dependency_closure_digest", "process_digest", "tool_digest", "status",
                "exit_code", "wall_time_ms",
                "provider_usage_json", "payload_json", "signature", "authority_eligible",
                "block_reasons_json", "recorded_by_command_id", "created_at",
            )
            connection.execute(
                f"INSERT INTO host_execution_receipts({','.join(columns)}) "
                f"VALUES ({','.join('?' for _ in columns)})",
                (
                    payload["receipt_id"],payload["receipt_nonce"],payload["service_instance_id"],
                    scope["run_id"],scope["route_id"],scope["attempt_id"],scope["binding_id"],
                    scope["claim_id"],scope["contract_version"],scope["statement_hash"],
                    scope["environment_profile_id"],scope["adapter_name"],scope["adapter_version"],
                    scope.get("source_commit"),payload.get("toolchain"),payload.get("binary_sha256"),
                    payload["request_hash"],payload["result_hash"],payload.get("source_sha256"),
                    payload.get("output_sha256"),payload["input_snapshot_digest"],
                    payload["environment_digest"],payload["mount_digest"],
                    payload.get("dependency_closure_digest"),payload["process_digest"],
                    payload["tool_digest"],payload["status"],payload.get("exit_code"),
                    payload["wall_time_ms"],_json(usage),_json(payload),signature,
                    int(not block_reasons),_json(block_reasons),command_id,now,
                ),
            )
            trusted_usage = {
                **dict(usage),
                "_rk_trust": "HOST_VERIFIED",
                "_rk_receipt_id": payload["receipt_id"],
            }
            for resource_kind, event_kind, amount, unit in budget_rows:
                connection.execute(
                    "INSERT INTO budget_events(budget_event_id,run_id,route_id,attempt_id,"
                    "command_id,"
                    "revision,event_kind,resource_kind,amount_microunits,unit,currency,"
                    "provider_usage_json,recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (str(self._host_id(payload["receipt_id"], resource_kind)),scope["run_id"],
                     scope["route_id"],scope["attempt_id"],command_id,revision_after,event_kind,
                     resource_kind,amount,unit,None,_json(trusted_usage),now),
                )
            for resource in overruns:
                fused_usage = {
                    **trusted_usage,
                    "budget_overrun": resource,
                    "authority_blocked": True,
                }
                connection.execute(
                    "INSERT INTO budget_events(budget_event_id,run_id,route_id,attempt_id,"
                    "command_id,revision,event_kind,resource_kind,amount_microunits,unit,"
                    "currency,provider_usage_json,recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (str(self._host_id(payload["receipt_id"], f"fuse:{resource}")),scope["run_id"],
                     scope["route_id"],scope["attempt_id"],command_id,revision_after,"FUSE_TRIP",
                     resource,0,"microunit",None,_json(fused_usage),now),
                )
            result = connection.execute(
                "UPDATE runs SET revision=?,updated_at=? WHERE run_id=? AND revision=?",
                (revision_after,now,scope["run_id"],revision_before),
            )
            if result.rowcount != 1:
                raise StorageConflict("host execution lost the revision compare-and-set")
            connection.execute(
                "UPDATE host_execution_claims SET completed_at=?,recovery_state='COMPLETED' "
                "WHERE attempt_id=? "
                "AND claim_token=? AND completed_at IS NULL",
                (now, scope["attempt_id"], claim_token),
            )
        return tuple(block_reasons)

    def heartbeat_host_execution(
        self,
        *,
        claim_token: str,
        service_instance_id: str,
        now: str,
        recover_after: str,
    ) -> bool:
        """Extend a live execution claim without changing the run revision."""

        with self.transaction() as connection:
            changed = connection.execute(
                "UPDATE host_execution_claims SET heartbeat_at=?,recover_after=? "
                "WHERE claim_token=? AND service_instance_id=? "
                "AND recovery_state='PENDING' AND completed_at IS NULL",
                (now,recover_after,claim_token,service_instance_id),
            )
        return changed.rowcount == 1

    def host_receipt_profile(self, receipt_id: str) -> dict[str, Any]:
        with self._reader(None) as connection:
            row = connection.execute(
                "SELECT * "
                "FROM host_execution_receipts WHERE receipt_id=?",
                (receipt_id,),
            ).fetchone()
        if row is None:
            raise StorageConflict("host execution receipt does not exist")
        result = dict(row)
        result["payload"] = json.loads(str(result.pop("payload_json")))
        result["provider_usage"] = json.loads(str(result.pop("provider_usage_json")))
        return result

    def recover_incomplete_host_claims(
        self,
        *,
        capability: VerifiedCapability,
        now: str,
        claim_token: str | None = None,
        force: bool = False,
    ) -> tuple[str, ...]:
        """Fail closed after a host crash; never retry an ambiguous provider call."""

        self.ensure_capability(capability)
        recovered: list[str] = []
        with self.transaction() as connection:
            conditions = ["recovery_state='PENDING'"]
            parameters: list[Any] = []
            if claim_token is not None:
                conditions.append("claim_token=?")
                parameters.append(claim_token)
            if not force:
                conditions.append("recover_after<=?")
                parameters.append(now)
            rows = connection.execute(
                "SELECT * FROM host_execution_claims WHERE "
                + " AND ".join(conditions)
                + " ORDER BY attempt_id",
                tuple(parameters),
            ).fetchall()
            for row in rows:
                if not capability.allows("HostExecute", str(row["run_id"])):
                    continue
                run = connection.execute(
                    "SELECT revision FROM runs WHERE run_id=?", (row["run_id"],)
                ).fetchone()
                if run is None:
                    raise StorageConflict("host recovery run disappeared")
                revision_before = int(run["revision"])
                revision_after = revision_before + 1
                command_id = self._host_recovery_uuid(row["claim_token"], "command")
                request_id = self._host_recovery_uuid(row["claim_token"], "request")
                event_id = self._host_recovery_uuid(row["claim_token"], "event")
                receipt = {
                    "schema_version": "rk.receipt.v1",
                    "request_id": request_id,
                    "command_id": command_id,
                    "run_id": row["run_id"],
                    "accepted": True,
                    "revision_before": revision_before,
                    "revision_after": revision_after,
                    "event_ids": [event_id],
                    "artifact_ids": [],
                    "rejection_code": None,
                    "missing_conditions": [],
                    "decided_at": now,
                }
                self.record_command(
                    connection,
                    command_id=command_id,
                    run_id=str(row["run_id"]),
                    request_id=request_id,
                    command_type="HostRecoverExecution",
                    request_digest=str(row["request_hash"]),
                    expected_revision=revision_before,
                    capability_id=capability.capability_id,
                    accepted=True,
                    revision_before=revision_before,
                    revision_after=revision_after,
                    rejection_code=None,
                    missing_conditions=[],
                    receipt=receipt,
                    trace_id=self._host_recovery_uuid(row["claim_token"], "trace"),
                    decided_at=now,
                )
                self.append_event(
                    connection,
                    event_id=event_id,
                    run_id=str(row["run_id"]),
                    command_id=command_id,
                    revision=revision_after,
                    event_type="HOST_EXECUTION_RECOVERED_UNKNOWN",
                    payload={
                        "attempt_id": row["attempt_id"],
                        "binding_id": row["binding_id"],
                        "recovery_state": "UNKNOWN_FUSED",
                    },
                    recorded_at=now,
                    route_id=str(row["route_id"]),
                    attempt_id=str(row["attempt_id"]),
                )
                provider_usage = _json({
                    "component": row["component"],
                    "_rk_trust": "HOST_VERIFIED",
                    "_rk_receipt_id": f"recovery:{row['claim_token']}",
                    "_rk_claim_token": row["claim_token"],
                    "cost_unknown": True,
                    "recovery_reason": "HOST_CRASH_WINDOW",
                })
                connection.execute(
                    "INSERT OR IGNORE INTO budget_events("
                    "budget_event_id,run_id,route_id,attempt_id,command_id,revision,event_kind,"
                    "resource_kind,amount_microunits,unit,provider_usage_json,recorded_at) "
                    "VALUES (?,?,?,?,?,?,'UNKNOWN_COST','API_MICRO_CURRENCY',NULL,"
                    "'unknown',?,?)",
                    (self._host_id(row["claim_token"], "recovery:unknown"),row["run_id"],
                     row["route_id"],row["attempt_id"],command_id,revision_after,
                     provider_usage,now),
                )
                connection.execute(
                    "INSERT OR IGNORE INTO budget_events("
                    "budget_event_id,run_id,route_id,attempt_id,command_id,revision,event_kind,"
                    "resource_kind,amount_microunits,unit,provider_usage_json,recorded_at) "
                    "VALUES (?,?,?,?,?,?,'FUSE_TRIP','API_MICRO_CURRENCY',0,'unknown',?,?)",
                    (self._host_id(row["claim_token"], "recovery:fuse"),row["run_id"],
                     row["route_id"],row["attempt_id"],command_id,revision_after,
                     provider_usage,now),
                )
                connection.execute(
                    "UPDATE attempts SET status='ENVIRONMENT_ERROR',ended_at=? "
                    "WHERE attempt_id=? AND status IN ('QUEUED','RUNNING','PAUSED')",
                    (now, row["attempt_id"]),
                )
                connection.execute(
                    "UPDATE leases SET status='REVOKED',released_at=? "
                    "WHERE attempt_id=? AND status='ACTIVE'",
                    (now, row["attempt_id"]),
                )
                connection.execute(
                    "UPDATE host_execution_claims SET completed_at=?,"
                    "recovery_state='UNKNOWN_FUSED' WHERE attempt_id=?",
                    (now, row["attempt_id"]),
                )
                changed = connection.execute(
                    "UPDATE runs SET revision=?,updated_at=? WHERE run_id=? AND revision=?",
                    (revision_after,now,row["run_id"],revision_before),
                )
                if changed.rowcount != 1:
                    raise StorageConflict("host recovery lost revision compare-and-set")
                recovered.append(str(row["attempt_id"]))
        return tuple(recovered)

    @staticmethod
    def _host_recovery_uuid(claim_token: Any, purpose: str) -> str:
        return str(uuid.uuid5(uuid.NAMESPACE_URL, f"rk:host-recovery:{claim_token}:{purpose}"))

    def consume_host_lean_receipt_atomic(
        self,
        *,
        receipt_id: str,
        feedback_id: str,
        capability: VerifiedCapability,
        command_id: str,
        event_id: str,
        trace_id: str,
        now: str,
        toolchain: str,
        expected_signature: str,
        expected_payload_json: str,
    ) -> None:
        """Bind a successful host Lean execution to CAS artifacts exactly once."""

        self.ensure_capability(capability)
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT h.*,r.revision,r.status AS run_status,r.current_contract_version,"
                "r.root_claim_id,rt.status AS route_status,c.lifecycle_status,c.claim_kind,"
                "c.statement_hash AS current_statement_hash,c.machine_verdict,"
                "a.status AS attempt_status "
                "FROM host_execution_receipts h JOIN runs r ON r.run_id=h.run_id "
                "JOIN routes rt ON rt.route_id=h.route_id JOIN claims c ON c.claim_id=h.claim_id "
                "JOIN attempts a ON a.attempt_id=h.attempt_id WHERE h.receipt_id=?",
                (receipt_id,),
            ).fetchone()
            if row is None:
                raise StorageConflict("host execution receipt does not exist")
            if (
                not hmac.compare_digest(str(row["signature"]), expected_signature)
                or str(row["payload_json"]) != expected_payload_json
            ):
                raise StorageConflict("host execution receipt changed after verification")
            if row["consumed_by_feedback_id"] is not None:
                raise StorageConflict("host execution receipt was already consumed")
            if (
                row["adapter_name"] != "lean-replay"
                or row["authority_eligible"] != 1
                or row["status"] != "COMPLETED"
                or row["exit_code"] != 0
                or row["run_status"] != "RUNNING"
                or row["route_status"] != "ACTIVE"
                or row["lifecycle_status"] != "ACTIVE"
                or row["contract_version"] != row["current_contract_version"]
                or row["statement_hash"] != row["current_statement_hash"]
                or row["attempt_status"] not in {"RUNNING", "SUCCEEDED"}
                or not row["source_sha256"]
                or not row["output_sha256"]
                or not toolchain
                or row["toolchain"] != toolchain
            ):
                raise StorageConflict("host Lean receipt is not authority eligible")
            artifacts = connection.execute(
                "SELECT a.artifact_id,a.sha256 FROM artifacts a JOIN run_artifacts ra "
                "ON ra.artifact_id=a.artifact_id WHERE ra.run_id=? "
                "AND a.sha256 IN (?,?) AND a.ingest_state='COMMITTED'",
                (row["run_id"], row["source_sha256"], row["output_sha256"]),
            ).fetchall()
            by_sha = {str(item["sha256"]): str(item["artifact_id"]) for item in artifacts}
            source_id = by_sha.get(str(row["source_sha256"]))
            output_id = by_sha.get(str(row["output_sha256"]))
            if source_id is None or output_id is None:
                raise StorageConflict("host Lean source/output is not committed in this run")
            revision_before = int(row["revision"])
            revision_after = revision_before + 1
            command_receipt = {
                "schema_version": "rk.receipt.v1", "request_id": receipt_id,
                "command_id": command_id, "run_id": row["run_id"], "accepted": True,
                "revision_before": revision_before, "revision_after": revision_after,
                "event_ids": [event_id], "artifact_ids": [], "rejection_code": None,
                "missing_conditions": [], "decided_at": now,
            }
            self.record_command(
                connection, command_id=command_id, run_id=str(row["run_id"]),
                request_id=f"consume:{receipt_id}", command_type="HostConsumeLeanReceipt",
                request_digest=str(row["result_hash"]), expected_revision=revision_before,
                capability_id=capability.capability_id, accepted=True,
                revision_before=revision_before, revision_after=revision_after,
                rejection_code=None, missing_conditions=[], receipt=command_receipt,
                trace_id=trace_id, decided_at=now,
            )
            diagnostic = {
                "host_receipt_id": receipt_id,
                "request_hash": row["request_hash"], "result_hash": row["result_hash"],
                "source_sha256": row["source_sha256"], "output_sha256": row["output_sha256"],
                "binary_sha256": row["binary_sha256"], "exit_code": row["exit_code"],
            }
            self.append_event(
                connection, event_id=event_id, run_id=str(row["run_id"]), command_id=command_id,
                revision=revision_after, event_type="HOST_LEAN_RECEIPT_CONSUMED",
                payload={"receipt_id": receipt_id, "feedback_id": feedback_id}, recorded_at=now,
                contract_version=int(row["contract_version"]), route_id=str(row["route_id"]),
                claim_id=str(row["claim_id"]), attempt_id=str(row["attempt_id"]),
            )
            connection.execute(
                "INSERT INTO lean_feedback_events(lean_feedback_id,run_id,claim_id,attempt_id,"
                "contract_version,environment_profile_id,toolchain,mathlib_commit,source_artifact_id,"
                "output_artifact_id,feedback_kind,first_failed_obligation_id,diagnostic_json,"
                "created_by_event_id,created_at,receipt_nonce) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (feedback_id,row["run_id"],row["claim_id"],row["attempt_id"],
                 row["contract_version"],row["environment_profile_id"],toolchain,
                 row["source_commit"],source_id,output_id,"REPLAY_PASS",None,_json(diagnostic),
                 event_id,now,row["receipt_nonce"]),
            )
            evidence_root_id = self._host_recovery_uuid(receipt_id, "lean-evidence-root")
            evidence_id = self._host_recovery_uuid(receipt_id, "lean-evidence")
            connection.execute(
                "INSERT INTO evidence_roots(evidence_root_id,run_id,root_kind,origin_artifact_id,"
                "verifier_profile_id,ancestor_root_ids_json,source_graph_json,created_by_event_id,"
                "created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    evidence_root_id,
                    row["run_id"],
                    "LEAN_KERNEL",
                    source_id,
                    row["environment_profile_id"],
                    "[]",
                    _json({"host_receipt_id": receipt_id, "feedback_id": feedback_id}),
                    event_id,
                    now,
                ),
            )
            connection.execute(
                "INSERT INTO evidence(evidence_id,run_id,claim_id,contract_version,"
                "statement_hash,artifact_id,evidence_type,evidence_strength,evidence_root_id,"
                "scope_json,provenance_json,ingest_schema_version,ingest_status,"
                "submitted_by_command_id,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    evidence_id,
                    row["run_id"],
                    row["claim_id"],
                    row["contract_version"],
                    row["statement_hash"],
                    output_id,
                    "LEAN_REPLAY",
                    "HARD_MACHINE",
                    evidence_root_id,
                    _json(
                        {
                            "claim_id": row["claim_id"],
                            "contract_version": row["contract_version"],
                            "statement_hash": row["statement_hash"],
                        }
                    ),
                    _json(
                        {
                            "actor": "host-lean-replay",
                            "host_receipt_id": receipt_id,
                            "feedback_id": feedback_id,
                        }
                    ),
                    1,
                    "ACCEPTED",
                    command_id,
                    now,
                ),
            )
            before = str(row["machine_verdict"])
            if before != "KERNEL_VERIFIED":
                connection.execute(
                    "INSERT INTO verdict_events(verdict_event_id,run_id,claim_id,command_id,"
                    "revision,axis,value_before,value_after,evidence_ids_json,closure_witness_id,"
                    "capability_id,reason_code,recorded_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        self._host_recovery_uuid(receipt_id, "lean-verdict"),
                        row["run_id"],
                        row["claim_id"],
                        command_id,
                        revision_after,
                        "MACHINE",
                        before,
                        "KERNEL_VERIFIED",
                        _json([evidence_id]),
                        None,
                        capability.capability_id,
                        "HOST_LEAN_RECEIPT_CONSUMED",
                        now,
                    ),
                )
                connection.execute(
                    "UPDATE claims SET machine_verdict='KERNEL_VERIFIED',updated_at=? "
                    "WHERE claim_id=?",
                    (now, row["claim_id"]),
                )
            changed = connection.execute(
                "UPDATE host_execution_receipts SET consumed_by_feedback_id=?,consumed_at=? "
                "WHERE receipt_id=? AND consumed_by_feedback_id IS NULL",
                (feedback_id,now,receipt_id),
            )
            if changed.rowcount != 1:
                raise StorageConflict("host execution receipt was concurrently consumed")
            result = connection.execute(
                "UPDATE runs SET revision=?,updated_at=? WHERE run_id=? AND revision=?",
                (revision_after,now,row["run_id"],revision_before),
            )
            if result.rowcount != 1:
                raise StorageConflict("host receipt consumption lost revision compare-and-set")

    def consume_host_checker_receipt_atomic(
        self,
        *,
        receipt_id: str,
        verification_id: str,
        capability: VerifiedCapability,
        command_id: str,
        event_id: str,
        trace_id: str,
        now: str,
        expected_signature: str,
        expected_payload_json: str,
        root_kind: str,
    ) -> None:
        """Atomically turn one host-pinned checker receipt into Claim authority."""

        self.ensure_capability(capability)
        with self.transaction() as connection:
            row = connection.execute(
                "SELECT h.*,r.revision,r.status AS run_status,r.current_contract_version,"
                "rt.status AS route_status,c.lifecycle_status,c.statement_hash AS "
                "current_statement_hash,c.machine_verdict,a.status AS attempt_status "
                "FROM host_execution_receipts h JOIN runs r ON r.run_id=h.run_id "
                "JOIN routes rt ON rt.route_id=h.route_id JOIN claims c ON c.claim_id=h.claim_id "
                "JOIN attempts a ON a.attempt_id=h.attempt_id WHERE h.receipt_id=?",
                (receipt_id,),
            ).fetchone()
            if row is None:
                raise StorageConflict("host checker receipt does not exist")
            if (
                not hmac.compare_digest(str(row["signature"]), expected_signature)
                or str(row["payload_json"]) != expected_payload_json
                or row["consumed_by_feedback_id"] is not None
                or row["consumed_by_verification_id"] is not None
                or row["authority_eligible"] != 1
                or row["status"] != "COMPLETED"
                or row["exit_code"] != 0
                or row["run_status"] != "RUNNING"
                or row["route_status"] != "ACTIVE"
                or row["lifecycle_status"] != "ACTIVE"
                or row["contract_version"] != row["current_contract_version"]
                or row["statement_hash"] != row["current_statement_hash"]
                or row["attempt_status"] not in {"RUNNING", "SUCCEEDED"}
            ):
                raise StorageConflict("host checker receipt is not authority eligible")
            artifact = connection.execute(
                "SELECT a.artifact_id FROM artifacts a JOIN run_artifacts ra "
                "ON ra.artifact_id=a.artifact_id WHERE ra.run_id=? AND a.sha256=? "
                "AND a.ingest_state='COMMITTED' LIMIT 1",
                (row["run_id"], row["output_sha256"]),
            ).fetchone()
            if artifact is None:
                raise StorageConflict("host checker output is not committed in this run")
            artifact_id = str(artifact[0])
            revision_before = int(row["revision"])
            revision_after = revision_before + 1
            receipt = {
                "schema_version": "rk.receipt.v1",
                "request_id": receipt_id,
                "command_id": command_id,
                "run_id": row["run_id"],
                "accepted": True,
                "revision_before": revision_before,
                "revision_after": revision_after,
                "event_ids": [event_id],
                "artifact_ids": [],
                "rejection_code": None,
                "missing_conditions": [],
                "decided_at": now,
            }
            self.record_command(
                connection,
                command_id=command_id,
                run_id=str(row["run_id"]),
                request_id=f"consume-checker:{receipt_id}",
                command_type="HostConsumeCheckerReceipt",
                request_digest=str(row["result_hash"]),
                expected_revision=revision_before,
                capability_id=capability.capability_id,
                accepted=True,
                revision_before=revision_before,
                revision_after=revision_after,
                rejection_code=None,
                missing_conditions=[],
                receipt=receipt,
                trace_id=trace_id,
                decided_at=now,
            )
            self.append_event(
                connection,
                event_id=event_id,
                run_id=str(row["run_id"]),
                command_id=command_id,
                revision=revision_after,
                event_type="HOST_CHECKER_RECEIPT_CONSUMED",
                payload={"receipt_id": receipt_id, "verification_id": verification_id},
                recorded_at=now,
                contract_version=int(row["contract_version"]),
                route_id=str(row["route_id"]),
                claim_id=str(row["claim_id"]),
                attempt_id=str(row["attempt_id"]),
            )
            root_id = self._host_recovery_uuid(receipt_id, "checker-evidence-root")
            evidence_id = self._host_recovery_uuid(receipt_id, "checker-evidence")
            connection.execute(
                "INSERT INTO evidence_roots(evidence_root_id,run_id,root_kind,origin_artifact_id,"
                "verifier_profile_id,ancestor_root_ids_json,source_graph_json,created_by_event_id,"
                "created_at) VALUES (?,?,?,?,?,?,?,?,?)",
                (
                    root_id,
                    row["run_id"],
                    root_kind,
                    artifact_id,
                    row["environment_profile_id"],
                    "[]",
                    _json({"host_receipt_id": receipt_id}),
                    event_id,
                    now,
                ),
            )
            connection.execute(
                "INSERT INTO evidence(evidence_id,run_id,claim_id,contract_version,"
                "statement_hash,artifact_id,evidence_type,evidence_strength,evidence_root_id,"
                "scope_json,provenance_json,ingest_schema_version,ingest_status,"
                "submitted_by_command_id,created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    evidence_id,
                    row["run_id"],
                    row["claim_id"],
                    row["contract_version"],
                    row["statement_hash"],
                    artifact_id,
                    "CHECKER_CERTIFICATE",
                    "HARD_MACHINE",
                    root_id,
                    _json(
                        {
                            "claim_id": row["claim_id"],
                            "contract_version": row["contract_version"],
                        }
                    ),
                    _json({"actor": "host-deterministic-checker", "host_receipt_id": receipt_id}),
                    1,
                    "ACCEPTED",
                    command_id,
                    now,
                ),
            )
            connection.execute(
                "INSERT INTO atomic_verifications(verification_id,run_id,contract_version,"
                "claim_id,backend,verdict,verification_ref,repair_feedback,"
                "verifier_capability_id,created_by_event_id,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (
                    verification_id,
                    row["run_id"],
                    row["contract_version"],
                    row["claim_id"],
                    "DETERMINISTIC_CHECKER",
                    "ACCEPTED",
                    evidence_id,
                    "",
                    capability.capability_id,
                    event_id,
                    now,
                ),
            )
            connection.execute(
                "UPDATE claims SET machine_verdict='CERTIFICATE_VERIFIED',"
                "semantic_verdict='TESTED',updated_at=? WHERE claim_id=?",
                (now, row["claim_id"]),
            )
            changed = connection.execute(
                "UPDATE host_execution_receipts SET consumed_by_verification_id=?,"
                "checker_consumed_at=? WHERE receipt_id=? AND consumed_by_feedback_id IS NULL "
                "AND consumed_by_verification_id IS NULL",
                (verification_id, now, receipt_id),
            )
            if changed.rowcount != 1:
                raise StorageConflict("host checker receipt was concurrently consumed")
            result = connection.execute(
                "UPDATE runs SET revision=?,updated_at=? WHERE run_id=? AND revision=?",
                (revision_after, now, row["run_id"], revision_before),
            )
            if result.rowcount != 1:
                raise StorageConflict("host checker receipt consumption lost revision CAS")

    @staticmethod
    def _host_id(receipt_id: Any, resource: str) -> str:
        digest = hashlib.sha256(f"{receipt_id}\n{resource}".encode()).hexdigest()
        return f"host-{digest[:32]}"

    @staticmethod
    def _host_budget_rows(
        payload: Mapping[str, Any], usage: Mapping[str, Any]
    ) -> list[tuple[str, str, int | None, str]]:
        rows: list[tuple[str, str, int | None, str]] = [
            ("WALL_SECOND", "ACTUAL", int(payload["wall_time_ms"]) * 1_000, "microsecond")
        ]
        # Token meters apply to the model transport, not to deterministic local tools.
        # Absence of an inapplicable meter is not an unknown cost.  API currency remains
        # independently unknown whenever the provider did not return it.
        if usage.get("token_meter_applicable") is True:
            for name, resource in (
                ("input_tokens", "INPUT_TOKEN"),
                ("output_tokens", "OUTPUT_TOKEN"),
            ):
                value = usage.get(name)
                if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                    rows.append((resource, "ACTUAL", value * 1_000_000, "microtoken"))
                else:
                    rows.append((resource, "UNKNOWN_COST", None, "unknown"))
        if usage.get("cost_unknown") is True or usage.get("currency_meter_applicable") is True:
            rows.append(("API_MICRO_CURRENCY", "UNKNOWN_COST", None, "unknown"))
        return rows

    @staticmethod
    def _host_budget_overruns(
        connection: sqlite3.Connection,
        run_id: Any,
        attempt_id: Any,
        rows: Sequence[tuple[str, str, int | None, str]],
        limits: Mapping[str, int],
    ) -> list[str]:
        overruns: list[str] = []
        for resource, kind, amount, _unit in rows:
            if kind != "ACTUAL" or amount is None:
                continue
            limit = limits.get(resource)
            if not isinstance(limit, int) or isinstance(limit, bool) or limit <= 0:
                overruns.append(resource)
                continue
            consumed, attempt_reserved = connection.execute(
                "SELECT COALESCE(SUM(CASE WHEN event_kind IN ('ACTUAL','RESERVATION') "
                "THEN amount_microunits WHEN event_kind='REFUND' "
                "THEN -amount_microunits ELSE 0 END),0),"
                "COALESCE(SUM(CASE WHEN attempt_id=? AND event_kind='RESERVATION' "
                "THEN amount_microunits WHEN attempt_id=? AND event_kind='REFUND' "
                "THEN -amount_microunits ELSE 0 END),0) "
                "FROM budget_events WHERE run_id=? AND resource_kind=? "
                "AND COALESCE(json_extract(provider_usage_json,'$._rk_trust'),'CURRENT') "
                "<> 'LEGACY_UNTRUSTED'",
                (attempt_id, attempt_id, run_id, resource),
            ).fetchone()
            projected = int(consumed) - int(attempt_reserved) + max(
                int(attempt_reserved), amount
            )
            if projected > limit:
                overruns.append(resource)
        return overruns

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

        existing = connection.execute(
            "SELECT artifact_id,role FROM run_artifacts WHERE run_id=? AND logical_name=?",
            (run_id, logical_name),
        ).fetchone()
        if existing is None:
            connection.execute(
                "INSERT INTO run_artifacts(run_id,artifact_id,logical_name,role,linked_at) "
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
            research_hints = rows("research_hints", "created_at, hint_id")
            paper_reviews = rows("paper_reviews", "created_at, paper_review_id")
            atomic_verifications = rows(
                "atomic_verifications", "created_at, verification_id"
            )
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
            host_receipts = rows("host_execution_receipts", "receipt_id")
            interrupt = conn.execute(
                "SELECT payload_json FROM events WHERE run_id = ? "
                "AND event_type = 'RUN_INTERRUPTED' ORDER BY event_seq DESC LIMIT 1",
                (run_id,),
            ).fetchone()
            fuse = conn.execute(
                "SELECT 1 FROM budget_events WHERE run_id = ? AND event_kind = 'FUSE_TRIP' "
                "AND COALESCE(json_extract(provider_usage_json,'$._rk_trust'),'CURRENT') "
                "<> 'LEGACY_UNTRUSTED' LIMIT 1",
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
            "research_hints": (),
            "paper_reviews": (),
            "atomic_verifications": (),
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
            "host_execution_receipts": (
                "provider_usage_json", "payload_json", "block_reasons_json"
            ),
        }
        collections: dict[str, list[dict[str, Any]]] = {
            "contracts": contracts,
            "claims": claims,
            "routes": routes,
            "attempts": attempts,
            "leases": leases,
            "artifacts": artifacts,
            "literature": literature,
            "research_hints": research_hints,
            "paper_reviews": paper_reviews,
            "atomic_verifications": atomic_verifications,
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
            "host_execution_receipts": host_receipts,
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

        independence_dimensions = (
            "idea_independence",
            "derivation_independence",
            "verification_independence",
            "implementation_independence",
            "retrieval_independence",
        )
        for review in peer_reviews:
            profile = review.get("independence_profile")
            managed = bool(
                isinstance(profile, Mapping)
                and profile.get("managed_human") is True
                and all(profile.get(name) == "INDEPENDENT" for name in independence_dimensions)
                and not profile.get("shared_ancestors")
            )
            review["trust_class"] = "MANAGED_PEER_REVIEW" if managed else "UNMANAGED_REVIEW"
            review["authority_effect"] = (
                "PEER_PROMOTION_ELIGIBLE" if managed else "NONE"
            )
            review["promotion_eligible"] = managed and review.get("verdict") == "ACCEPT"

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

    def inspect_snapshot(
        self, run_id: str, *, connection: sqlite3.Connection | None = None
    ) -> dict[str, Any]:
        with self._reader(connection) as connection:
            run = connection.execute("SELECT * FROM runs WHERE run_id = ?", (run_id,)).fetchone()
            if run is None:
                raise RunNotFound("run not found")
            contract = connection.execute(
                "SELECT version,status,contract_artifact_id,statement_hash,contract_json "
                "FROM contract_versions "
                "WHERE run_id = ? AND version = ?",
                (run_id, run["current_contract_version"]),
            ).fetchone()
            claim_rows = connection.execute(
                "SELECT claim_id,contract_version,claim_kind,stable_label,statement_revision,"
                "statement_artifact_id,statement_hash,normalized_statement_json,"
                "lifecycle_status,route_result, "
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
                "FROM budget_events WHERE run_id = ? "
                "AND COALESCE(json_extract(provider_usage_json,'$._rk_trust'),'TRUSTED') "
                "<> 'LEGACY_UNTRUSTED' GROUP BY resource_kind, event_kind",
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
                "SELECT e.evidence_id,e.claim_id,e.contract_version,e.statement_hash,"
                "e.evidence_type,e.evidence_strength,e.ingest_status,e.artifact_id,"
                "r.root_kind,r.verifier_profile_id FROM evidence e JOIN evidence_roots r "
                "ON r.evidence_root_id=e.evidence_root_id WHERE e.run_id = ? "
                "ORDER BY e.evidence_id",
                (run_id,),
            ).fetchall()
            review_rows = connection.execute(
                "SELECT review_id,claim_id,contract_version,statement_hash,verdict,"
                "review_artifact_id,reviewer_capability_id,independence_profile_json,"
                "checklist_json,source_graph_json,selected_subgraph_digest "
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
                "SELECT edge_id,contract_version,from_claim_id,to_claim_id,edge_kind,direction,"
                "justification_kind,justification_ref,status "
                "FROM claim_edges WHERE run_id = ? ORDER BY edge_id",
                (run_id,),
            ).fetchall()
            obligation_rows = connection.execute(
                "SELECT obligation_id,contract_version,parent_claim_id,child_claim_ids_json,"
                "local_domain_json,coverage_ref,coverage_status,compatibility_ref,"
                "compatibility_status,invariant_ref,invariant_status,progress_ref,"
                "progress_status,boundary_ref,boundary_status,simultaneous_choice_ref,"
                "simultaneous_choice_status,composition_rule,closure_theorem_ref,"
                "missing_conditions_json,status,displacement_status "
                "FROM composition_obligations WHERE run_id = ? ORDER BY obligation_id",
                (run_id,),
            ).fetchall()
            bridge_rows = connection.execute(
                "SELECT bridge_id,contract_version,source_claim_id,target_claim_id,directionality,"
                "term_mapping_json,forward_obligations_json,reverse_obligations_json,"
                "loss_accounting_json,bridge_spec_json,forward_status,reverse_status,"
                "target_audit_review_id,backtranslation_artifact_id "
                "FROM bridges WHERE run_id = ? ORDER BY bridge_id",
                (run_id,),
            ).fetchall()
            literature_rows = connection.execute(
                "SELECT literature_record_id,claim_id,status,relation,cutoff_date "
                "FROM literature_records WHERE run_id = ? ORDER BY literature_record_id",
                (run_id,),
            ).fetchall()
            research_hint_rows = connection.execute(
                "SELECT hint_id,contract_version,hint_kind,hint_text,target_route_id,"
                "target_claim_id,checkpoint_label,created_at FROM research_hints "
                "WHERE run_id=? ORDER BY created_at,hint_id",
                (run_id,),
            ).fetchall()
            paper_review_rows = connection.execute(
                "SELECT paper_review_id,contract_version,final_fact_id,paper_sha256,status,"
                "review_artifact_id,created_at FROM paper_reviews WHERE run_id=? "
                "ORDER BY created_at,paper_review_id",
                (run_id,),
            ).fetchall()
            atomic_verification_rows = connection.execute(
                "SELECT verification_id,contract_version,claim_id,backend,verdict,"
                "verification_ref,repair_feedback,created_at FROM atomic_verifications "
                "WHERE run_id=? ORDER BY created_at,verification_id",
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
            host_receipts = [
                dict(row)
                for row in connection.execute(
                    "SELECT receipt_id,receipt_nonce,service_instance_id,run_id,route_id,"
                    "attempt_id,binding_id,claim_id,contract_version,statement_hash,"
                    "environment_profile_id,adapter_name,adapter_version,source_commit,"
                    "toolchain,binary_sha256,request_hash,result_hash,source_sha256,output_sha256,"
                    "input_snapshot_digest,environment_digest,mount_digest,"
                    "dependency_closure_digest,process_digest,tool_digest,status,exit_code,"
                    "wall_time_ms,authority_eligible,"
                    "block_reasons_json,consumed_by_feedback_id,"
                    "created_at,consumed_at FROM host_execution_receipts WHERE run_id=? "
                    "ORDER BY receipt_id",
                    (run_id,),
                ).fetchall()
            ]
            for receipt in host_receipts:
                receipt["block_reasons"] = json.loads(
                    str(receipt.pop("block_reasons_json"))
                )
            cursor_row = connection.execute(
                "SELECT COALESCE(MAX(event_seq), 0) FROM events WHERE run_id = ?", (run_id,)
            ).fetchone()
        claims = [
            {
                "claim_id": str(row["claim_id"]),
                "contract_version": int(row["contract_version"]),
                "claim_kind": str(row["claim_kind"]),
                "stable_label": str(row["stable_label"]),
                "statement_revision": int(row["statement_revision"]),
                "statement_artifact_id": str(row["statement_artifact_id"]),
                "statement_hash": str(row["statement_hash"]),
                "normalized_statement": json.loads(str(row["normalized_statement_json"])),
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
        public_evidence = [
            {
                **dict(row),
                "trust_class": "UNMANAGED_CANDIDATE",
                "authority_effect": "NONE",
                "promotion_eligible": False,
            }
            for row in evidence_rows
        ]
        artifact_sha_by_id = {
            str(row["artifact_id"]): str(row["sha256"]) for row in artifact_rows
        }
        verified_evidence_keys = {
            (
                str(item["claim_id"]),
                int(item["contract_version"]),
                str(item["statement_hash"]),
                str(item["output_sha256"]),
            )
            for item in host_receipts
            if item.get("adapter_name") == "lean-replay"
            and item.get("status") == "COMPLETED"
            and item.get("exit_code") == 0
            and item.get("output_sha256") is not None
            and item.get("consumed_by_feedback_id") is not None
            and item.get("consumed_at") is not None
            and item.get("authority_eligible") == 1
            and item.get("block_reasons") in ([], ())
            and item.get("dependency_closure_digest") is not None
        }
        for item in public_evidence:
            artifact_sha = artifact_sha_by_id.get(str(item.get("artifact_id")))
            evidence_key = (
                str(item.get("claim_id")),
                int(item.get("contract_version", 0)),
                str(item.get("statement_hash")),
                str(artifact_sha),
            )
            if (
                item.get("evidence_type") == "LEAN_REPLAY"
                and item.get("evidence_strength") == "HARD_MACHINE"
                and item.get("ingest_status") in {"ACTIVE", "COMMITTED", "ACCEPTED"}
                and evidence_key in verified_evidence_keys
            ):
                item["trust_class"] = "HOST_VERIFIED_EVIDENCE"
                item["authority_effect"] = "MACHINE_PROMOTION_ELIGIBLE"
                item["promotion_eligible"] = True
        public_peer_reviews = [
            {
                **dict(row),
                "independence_profile": json.loads(str(row["independence_profile_json"])),
                "checklist": json.loads(str(row["checklist_json"])),
                "source_graph": json.loads(str(row["source_graph_json"])),
                "trust_class": "UNMANAGED_REVIEW",
                "authority_effect": "NONE",
                "promotion_eligible": False,
            }
            for row in review_rows
        ]
        independence_dimensions = (
            "idea_independence",
            "derivation_independence",
            "verification_independence",
            "implementation_independence",
            "retrieval_independence",
        )
        for item in public_peer_reviews:
            profile = item.get("independence_profile")
            managed = bool(
                isinstance(profile, Mapping)
                and profile.get("managed_human") is True
                and all(profile.get(name) == "INDEPENDENT" for name in independence_dimensions)
                and not profile.get("shared_ancestors")
            )
            if managed:
                item["trust_class"] = "MANAGED_PEER_REVIEW"
                item["authority_effect"] = "PEER_PROMOTION_ELIGIBLE"
                item["promotion_eligible"] = True
        public_quality_reviews = [
            {
                **dict(row),
                "trust_class": "UNMANAGED_REVIEW",
                "authority_effect": "NONE",
                "promotion_eligible": False,
            }
            for row in quality_review_rows
        ]
        public_bindings = [
            {
                **row,
                "trust_class": "UNMANAGED_BINDING",
                "authority_effect": "NONE",
                "promotion_eligible": False,
            }
            for row in bindings
        ]
        public_lean_feedback = [
            {
                **row,
                "trust_class": "V01_UNSCOPED_FEEDBACK",
                "authority_effect": "NONE",
                "promotion_eligible": False,
            }
            for row in lean_feedback
        ]
        consumed_feedback_ids = {
            str(item["consumed_by_feedback_id"])
            for item in host_receipts
            if item.get("consumed_by_feedback_id") is not None
            and item.get("authority_eligible") == 1
            and item.get("block_reasons") in ([], ())
            and item.get("dependency_closure_digest") is not None
        }
        for item in public_lean_feedback:
            if str(item.get("lean_feedback_id")) in consumed_feedback_ids:
                item["trust_class"] = "HOST_VERIFIED_FEEDBACK"
                item["authority_effect"] = "MACHINE_PROMOTION_ELIGIBLE"
                item["promotion_eligible"] = True
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
        legacy_untrusted_component_usage: dict[str, dict[str, int]] = {}
        seen_host_receipts: set[str] = set()
        host_unknowns: dict[tuple[str, str], int] = {}
        for row in component_rows:
            event_kind = str(row["event_kind"])
            if event_kind not in {"ACTUAL", "UNKNOWN_COST"}:
                continue
            try:
                usage = json.loads(str(row["provider_usage_json"]))
            except (json.JSONDecodeError, TypeError):
                continue
            if not isinstance(usage, Mapping):
                continue
            component = usage.get("component")
            if not isinstance(component, str) or not component:
                continue
            trust = usage.get("_rk_trust")
            if trust == "LEGACY_UNTRUSTED":
                target = legacy_untrusted_component_usage
            elif trust in {"HOST_VERIFIED", "ORCHESTRATOR_OBSERVED"}:
                target = component_usage
                receipt_key = usage.get("_rk_receipt_id") or usage.get("_rk_request_id")
                if isinstance(receipt_key, str) and event_kind == "UNKNOWN_COST":
                    host_unknowns[(component, receipt_key)] = (
                        host_unknowns.get((component, receipt_key), 0) + 1
                    )
                if isinstance(receipt_key, str) and receipt_key in seen_host_receipts:
                    # Provider token/wall totals describe the whole invocation and are copied
                    # onto each resource row for traceability. Aggregate them once per receipt.
                    continue
                if isinstance(receipt_key, str):
                    seen_host_receipts.add(receipt_key)
            else:
                # No public command may manufacture host trust in provider JSON.
                continue
            item = target.setdefault(
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
            if event_kind == "UNKNOWN_COST" and (
                trust not in {"HOST_VERIFIED", "ORCHESTRATOR_OBSERVED"}
                or not isinstance(
                    usage.get("_rk_receipt_id") or usage.get("_rk_request_id"), str
                )
            ):
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
        for (component, _receipt_id), count in host_unknowns.items():
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
            item["unknown_count"] += count
        terminal_values = {"ROUTE_LOCAL", "ROUTE_PROVED", "REFUTED", "PREVIOUSLY_KNOWN"}
        return {
            "run_id": str(run["run_id"]),
            "stable_project_id": str(run["stable_project_id"]),
            "status": str(run["status"]),
            "final_outcome": run["final_outcome"],
            "revision": int(run["revision"]),
            "current_contract_version": int(run["current_contract_version"]),
            "contract": (
                {
                    **{
                        key: value
                        for key, value in dict(contract).items()
                        if key != "contract_json"
                    },
                    "contract": json.loads(str(contract["contract_json"])),
                }
                if contract is not None
                else None
            ),
            "root_claim_id": run["root_claim_id"],
            "artifacts": [dict(row) for row in artifact_rows],
            "evidence": public_evidence,
            "peer_reviews": public_peer_reviews,
            "quality_reviews": public_quality_reviews,
            "closure_witnesses": [dict(row) for row in witness_rows],
            "edges": [dict(row) for row in edge_rows],
            "obligations": [dict(row) for row in obligation_rows],
            "bridges": [
                {
                    **dict(row),
                    "term_mapping": json.loads(str(row["term_mapping_json"])),
                    "forward_obligations": json.loads(str(row["forward_obligations_json"])),
                    "reverse_obligations": json.loads(str(row["reverse_obligations_json"])),
                    "loss_accounting": json.loads(str(row["loss_accounting_json"])),
                    "bridge_spec": json.loads(str(row["bridge_spec_json"])),
                }
                for row in bridge_rows
            ],
            "literature": [dict(row) for row in literature_rows],
            "research_hints": [dict(row) for row in research_hint_rows],
            "paper_reviews": [dict(row) for row in paper_review_rows],
            "atomic_verifications": [dict(row) for row in atomic_verification_rows],
            "claims": claims,
            "routes": [dict(row) for row in route_rows],
            "open_obligation_ids": [str(row[0]) for row in obligations],
            "active_attempts": [dict(row) for row in attempts],
            "bindings": public_bindings,
            "lean_feedback": public_lean_feedback,
            "host_execution_receipts": host_receipts,
            "budget_events": budget_events,
            "budget_summary": budget,
            "component_usage": component_usage,
            "legacy_untrusted_component_usage": legacy_untrusted_component_usage,
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
