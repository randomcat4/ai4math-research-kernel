"""Pure deterministic transition guard for all v1 ResearchKernel commands."""

from __future__ import annotations

from collections.abc import Callable, Iterable, Mapping, Sequence
from datetime import UTC, datetime
from hashlib import sha256
from pathlib import PurePosixPath
from typing import Any, cast

from rk.composition import (
    CanonicalizationError,
    canonical_json_bytes,
    validate_closure_witness,
)
from rk.domain import (
    Decision,
    MissingCondition,
    RejectionCode,
    RunSnapshot,
    TypedCommand,
    VerifiedCapability,
    frozen_mapping,
)

COMMAND_TYPES = frozenset(
    {
        "FreezeContract",
        "StartRun",
        "AmendContract",
        "Interrupt",
        "Resume",
        "Finalize",
        "SubmitEvidence",
        "RecordFailure",
        "RequestExpansion",
        "ProposeContractDefect",
        "RecordPeerReview",
        "RecordQualityReview",
        "RecordLiterature",
        "RegisterBridge",
        "RecordLeanFeedback",
        "RegisterClaim",
        "RegisterClaimEdge",
        "RegisterRoute",
        "RegisterCompositionObligation",
        "SubmitClosureWitness",
        "PromoteClaim",
        "RegisterAttempt",
        "AcquireLease",
        "HeartbeatLease",
        "ReleaseLease",
        "RecordBudget",
        "BindExecution",
    }
)

_EVENT_TYPES = {
    "FreezeContract": "CONTRACT_FROZEN",
    "StartRun": "RUN_STARTED",
    "AmendContract": "CONTRACT_AMENDED",
    "Interrupt": "RUN_INTERRUPTED",
    "Resume": "RUN_RESUMED",
    "Finalize": "RUN_FINALIZED",
    "SubmitEvidence": "EVIDENCE_SUBMITTED",
    "RecordFailure": "FAILURE_RECORDED",
    "RequestExpansion": "EXPANSION_APPROVED",
    "ProposeContractDefect": "CONTRACT_DEFECT_PROPOSED",
    "RecordPeerReview": "PEER_REVIEW_RECORDED",
    "RecordQualityReview": "QUALITY_REVIEW_RECORDED",
    "RecordLiterature": "LITERATURE_RECORDED",
    "RegisterBridge": "BRIDGE_REGISTERED",
    "RecordLeanFeedback": "LEAN_FEEDBACK_RECORDED",
    "RegisterClaim": "CLAIM_REGISTERED",
    "RegisterClaimEdge": "CLAIM_EDGE_REGISTERED",
    "RegisterRoute": "ROUTE_REGISTERED",
    "RegisterCompositionObligation": "COMPOSITION_OBLIGATION_REGISTERED",
    "SubmitClosureWitness": "CLOSURE_WITNESS_ACCEPTED",
    "PromoteClaim": "CLAIM_PROMOTED",
    "RegisterAttempt": "ATTEMPT_REGISTERED",
    "AcquireLease": "LEASE_ACQUIRED",
    "HeartbeatLease": "LEASE_HEARTBEAT",
    "ReleaseLease": "LEASE_RELEASED",
    "RecordBudget": "BUDGET_RECORDED",
    "BindExecution": "EXECUTION_BOUND",
}

_HARD_MACHINE_TYPES = {"LEAN_REPLAY", "CHECKER_CERTIFICATE"}
_EVIDENCE_STRENGTHS = {
    "LEAN_REPLAY": {"HARD_MACHINE"},
    "CHECKER_CERTIFICATE": {"HARD_MACHINE"},
    "EXACT_ENUMERATION": {"HARD_MACHINE", "PROVENANCE_ONLY"},
    "COUNTEREXAMPLE": {"HARD_MACHINE", "HUMAN_ATTESTED"},
    "NATURAL_LANGUAGE_PROOF": {"HUMAN_ATTESTED"},
    "MODEL_JUDGE": {"SOFT_MODEL"},
    "PEER_SIGNATURE": {"HUMAN_ATTESTED"},
    "SEMANTIC_AUDIT": {"HUMAN_ATTESTED"},
    "LITERATURE_SOURCE": {"PROVENANCE_ONLY"},
    "EXECUTION_LOG": {"PROVENANCE_ONLY"},
}
_LOGICAL_EDGE_KINDS = {"IMPLIES", "DEPENDS_ON", "SPECIALIZES", "GENERALIZES"}
_PART_NAMES = (
    "coverage",
    "compatibility",
    "invariant",
    "progress",
    "boundary",
    "simultaneous_choice",
)


def _projection(snapshot: RunSnapshot | Mapping[str, Any]) -> Mapping[str, Any]:
    if isinstance(snapshot, RunSnapshot):
        return snapshot.projection
    nested = snapshot.get("projection")
    return nested if isinstance(nested, Mapping) else snapshot


def _snapshot_value(
    snapshot: RunSnapshot | Mapping[str, Any], key: str, default: Any = None
) -> Any:
    if isinstance(snapshot, RunSnapshot):
        return getattr(snapshot, key, snapshot.projection.get(key, default))
    return snapshot.get(key, _projection(snapshot).get(key, default))


def _records(value: Any, *id_keys: str) -> dict[str, Mapping[str, Any]]:
    source = value.values() if isinstance(value, Mapping) else value
    if not isinstance(source, Iterable) or isinstance(source, (str, bytes, bytearray)):
        return {}
    result: dict[str, Mapping[str, Any]] = {}
    for item in source:
        if not isinstance(item, Mapping):
            continue
        identifier = next((item.get(key) for key in id_keys if item.get(key) is not None), None)
        if isinstance(identifier, (str, int)) and not isinstance(identifier, bool):
            result[str(identifier)] = item
    return result


def _status(record: Mapping[str, Any]) -> str:
    return str(record.get("lifecycle_status", record.get("lifecycle", record.get("status", ""))))


def _condition(code: str, path: str, **params: Any) -> MissingCondition:
    return MissingCondition(code=code, path=path, params=frozen_mapping(params))


def _reject(
    code: str | RejectionCode,
    conditions: MissingCondition | Iterable[MissingCondition],
) -> Decision:
    items = (conditions,) if isinstance(conditions, MissingCondition) else tuple(conditions)
    return Decision(accepted=False, rejection_code=str(code), missing_conditions=items)


def _accept(command: TypedCommand, *mutations: Mapping[str, Any]) -> Decision:
    event = frozen_mapping({"type": _EVENT_TYPES[command.type], "command_type": command.type})
    return Decision(
        accepted=True,
        projection_mutations=tuple(frozen_mapping(value) for value in mutations),
        event_intents=(event,),
    )


def _parse_time(value: datetime | str) -> datetime | None:
    if isinstance(value, datetime):
        parsed = value
    elif isinstance(value, str):
        try:
            parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    else:
        return None
    if parsed.tzinfo is None:
        return None
    return parsed.astimezone(UTC)


def _current_contract(projection: Mapping[str, Any], version: int) -> Mapping[str, Any]:
    current = projection.get("contract")
    if isinstance(current, Mapping) and current.get("version", version) == version:
        return current
    return _records(projection.get("contracts"), "version", "contract_version").get(
        str(version), {}
    )


def _object_scope(
    record: Mapping[str, Any] | None,
    *,
    run_id: str,
    version: int | None = None,
    active: bool = False,
) -> bool:
    if not record:
        return False
    if record.get("run_id", run_id) != run_id:
        return False
    if version is not None and record.get("contract_version") != version:
        return False
    return not active or _status(record) == "ACTIVE"


class _Context:
    def __init__(
        self,
        snapshot: RunSnapshot | Mapping[str, Any],
        evidence: Mapping[str, Any],
        policy: Mapping[str, Any],
        capability: VerifiedCapability,
        now: datetime,
    ) -> None:
        self.snapshot = snapshot
        self.projection = _projection(snapshot)
        self.evidence_summary = evidence
        self.policy = policy
        self.capability = capability
        self.now = now
        self.run_id = str(_snapshot_value(snapshot, "run_id"))
        self.version = int(_snapshot_value(snapshot, "current_contract_version", 0))
        self.contract = _current_contract(self.projection, self.version)
        self.claims = _records(self.projection.get("claims"), "claim_id")
        self.routes = _records(self.projection.get("routes"), "route_id")
        self.attempts = _records(
            self.projection.get("attempts", self.projection.get("active_attempts")), "attempt_id"
        )
        self.leases = _records(self.projection.get("leases"), "lease_id")
        self.edges = _records(self.projection.get("edges"), "edge_id")
        self.obligations = _records(self.projection.get("obligations"), "obligation_id")
        self.bridges = _records(self.projection.get("bridges"), "bridge_id")
        self.witnesses = _records(self.projection.get("closure_witnesses"), "witness_id")
        self.reviews = {
            **_records(self.projection.get("reviews"), "review_id"),
            **_records(self.projection.get("peer_reviews"), "review_id"),
            **_records(evidence.get("reviews"), "review_id"),
            **_records(evidence.get("peer_reviews"), "review_id"),
        }
        self.evidence = {
            **_records(self.projection.get("evidence"), "evidence_id"),
            **_records(evidence.get("evidence"), "evidence_id"),
        }
        self.artifacts = {
            **_records(self.projection.get("artifacts"), "artifact_id"),
            **_records(evidence.get("artifacts"), "artifact_id"),
        }

    def artifact_committed(self, artifact_id: Any) -> bool:
        record = self.artifacts.get(str(artifact_id))
        return bool(record and record.get("ingest_state", record.get("status")) == "COMMITTED")

    def artifact_names_available(self, names: Sequence[Any]) -> bool:
        staged = self.evidence_summary.get("artifact_inputs", ())
        available = {
            str(item.get("name"))
            for item in staged
            if isinstance(item, Mapping)
            and item.get("ingest_state", item.get("status", "COMMITTED")) == "COMMITTED"
        }
        available.update(
            str(item) for item in self.evidence_summary.get("artifact_input_names", ())
        )
        return bool(names) and all(str(name) in available for name in names)

    def active_leases(self) -> list[Mapping[str, Any]]:
        return [
            lease
            for lease in self.leases.values()
            if _status(lease) == "ACTIVE"
            and (_parse_time(str(lease.get("expires_at"))) or datetime.min.replace(tzinfo=UTC))
            > self.now
        ]


Handler = Callable[[_Context, TypedCommand], Decision | None]


class TransitionGuard:
    """A deep, deterministic decision module over immutable command context."""

    def decide(
        self,
        *,
        now_utc: datetime,
        snapshot: RunSnapshot | Mapping[str, Any] | None,
        command: TypedCommand,
        evidence_summary: Mapping[str, Any],
        capability: VerifiedCapability,
        policy_snapshot: Mapping[str, Any],
        expected_revision: int,
    ) -> Decision:
        now = _parse_time(now_utc)
        issued = _parse_time(capability.issued_at)
        expires = _parse_time(capability.expires_at)
        revoked = set(policy_snapshot.get("revoked_capability_ids", ()))
        run_id = None if snapshot is None else str(_snapshot_value(snapshot, "run_id"))
        if (
            command.type not in COMMAND_TYPES
            or now is None
            or issued is None
            or expires is None
            or not issued <= now < expires
            or capability.capability_id in revoked
            or not capability.allows(command.type, run_id)
        ):
            return _reject(
                RejectionCode.CAPABILITY_DENIED,
                _condition("REQUIRED_ACTION", "/command/type", action=command.type),
            )
        if snapshot is None or not run_id or run_id == "None":
            return _reject(
                RejectionCode.INVALID_TRANSITION,
                _condition("RUN_NOT_FOUND", "/run_id"),
            )
        revision = _snapshot_value(snapshot, "revision")
        if expected_revision != revision:
            return _reject(
                RejectionCode.REVISION_CONFLICT,
                _condition("RUN_STATE", "/expected_revision", expected=revision),
            )
        if _snapshot_value(snapshot, "status") in {"CLOSED", "CONTRACT_DEFECTIVE"}:
            return _reject(
                RejectionCode.RUN_CLOSED,
                _condition("RUN_STATE", "/run_id", required="NOT_CLOSED"),
            )
        context = _Context(snapshot, evidence_summary, policy_snapshot, capability, now)
        generic = self._scope_check(context, command)
        if generic is not None:
            return generic
        handler = cast(Handler, getattr(self, f"_handle_{command.type}"))
        result = handler(context, command)
        if result is not None:
            return result
        return _accept(command, {"op": "APPLY_COMMAND", "command_type": command.type})

    def _scope_check(self, context: _Context, command: TypedCommand) -> Decision | None:
        payload = command.payload
        if (
            "contract_version" in payload
            and command.type != "RecordLiterature"
            and payload.get("contract_version") != context.version
        ):
            return _reject(
                RejectionCode.EVIDENCE_SCOPE_MISMATCH,
                _condition(
                    "CONTRACT_VERSION",
                    "/command/payload/contract_version",
                    current=context.version,
                ),
            )
        claim_id = payload.get("claim_id")
        if claim_id is not None and command.type not in {"RegisterClaim"}:
            claim = context.claims.get(str(claim_id))
            if not _object_scope(claim, run_id=context.run_id):
                return _reject(
                    RejectionCode.EVIDENCE_SCOPE_MISMATCH,
                    _condition("OBJECT_SCOPE", "/command/payload/claim_id", claim_id=claim_id),
                )
        return None

    def _handle_FreezeContract(self, context: _Context, command: TypedCommand) -> Decision | None:
        payload = command.payload
        if (
            _snapshot_value(context.snapshot, "status") != "OPEN"
            or _status(context.contract) != "DRAFT"
        ):
            return _reject(
                RejectionCode.INVALID_TRANSITION,
                _condition("CONTRACT_STATE", "/command/type", required="DRAFT"),
            )
        if payload.get("contract_version") != context.version:
            return _reject(
                RejectionCode.EVIDENCE_SCOPE_MISMATCH,
                _condition("CONTRACT_VERSION", "/command/payload/contract_version"),
            )
        if not context.artifact_committed(payload.get("completeness_check_artifact_id")):
            return _reject(
                RejectionCode.ARTIFACT_MISSING,
                _condition(
                    "ARTIFACT_STATE",
                    "/command/payload/completeness_check_artifact_id",
                    required="COMMITTED",
                ),
            )
        complete = (
            context.contract.get("fields_complete") is True
            or context.evidence_summary.get("contract_complete") is True
        )
        if not complete or context.projection.get("defect_proposals"):
            return _reject(
                RejectionCode.INVALID_TRANSITION,
                _condition("CONTRACT_STATE", "/contract", required="COMPLETE_WITHOUT_DEFECT"),
            )
        return _accept(
            command, {"op": "SET_CONTRACT_STATUS", "version": context.version, "status": "FROZEN"}
        )

    def _handle_StartRun(self, context: _Context, command: TypedCommand) -> Decision | None:
        payload = command.payload
        if (
            _snapshot_value(context.snapshot, "status") != "OPEN"
            or _status(context.contract) != "FROZEN"
        ):
            return _reject(
                RejectionCode.CONTRACT_NOT_FROZEN,
                _condition("CONTRACT_STATE", "/contract", required="FROZEN"),
            )
        if not context.artifact_committed(payload.get("literature_plan_artifact_id")):
            return _reject(
                RejectionCode.ARTIFACT_MISSING,
                _condition("LITERATURE_PLAN", "/command/payload/literature_plan_artifact_id"),
            )
        if not isinstance(payload.get("budget_policy"), Mapping) or not payload.get(
            "budget_policy"
        ):
            return _reject(
                RejectionCode.BUDGET_DENIED,
                _condition("BUDGET_POLICY", "/command/payload/budget_policy"),
            )
        return _accept(command, {"op": "SET_RUN_STATUS", "status": "RUNNING"})

    def _handle_AmendContract(self, context: _Context, command: TypedCommand) -> Decision | None:
        payload = command.payload
        if (
            _status(context.contract) != "DEFECT_PROPOSED"
            or payload.get("base_version") != context.version
        ):
            return _reject(
                RejectionCode.CONTRACT_DEFECTIVE,
                _condition(
                    "CONTRACT_STATE",
                    "/command/payload/base_version",
                    required="DEFECT_PROPOSED_CURRENT",
                ),
            )
        paths = ("patch_artifact_id", "impact_analysis_artifact_id")
        missing = [
            _condition("ARTIFACT_STATE", f"/command/payload/{name}")
            for name in paths
            if not context.artifact_committed(payload.get(name))
        ]
        if missing:
            return _reject(RejectionCode.ARTIFACT_MISSING, missing)
        approval_ids = tuple(payload.get("approvals", ()))
        approvals = _records(context.evidence_summary.get("approvals"), "approval_id")
        valid = [
            approvals[item]
            for item in approval_ids
            if item in approvals
            and approvals[item].get("base_version") == context.version
            and approvals[item].get("accepted") is True
        ]
        roles = {item.get("role") for item in valid}
        required = set(context.policy.get("amendment_required_roles", ("CONTRACT_OWNER",)))
        if not required.issubset(roles):
            return _reject(
                RejectionCode.CAPABILITY_DENIED,
                _condition(
                    "CONTRACT_OWNER_APPROVAL",
                    "/command/payload/approvals",
                    required=sorted(required),
                ),
            )
        return _accept(
            command,
            {"op": "SUPERSEDE_CONTRACT", "version": context.version},
            {"op": "CREATE_CONTRACT_VERSION", "version": context.version + 1, "status": "FROZEN"},
            {"op": "INVALIDATE_DEPENDENCY_CLOSURE"},
            {"op": "SET_RUN_STATUS", "status": "PAUSED"},
        )

    def _handle_Interrupt(self, context: _Context, command: TypedCommand) -> Decision | None:
        if _snapshot_value(context.snapshot, "status") != "RUNNING":
            return _reject(
                RejectionCode.INVALID_TRANSITION,
                _condition("RUN_STATE", "/command/type", required="RUNNING"),
            )
        if not context.artifact_committed(command.payload.get("checkpoint_artifact_id")):
            return _reject(
                RejectionCode.ARTIFACT_MISSING,
                _condition("CHECKPOINT", "/command/payload/checkpoint_artifact_id"),
            )
        return _accept(
            command, {"op": "SET_RUN_STATUS", "status": "PAUSED"}, {"op": "PAUSE_ACTIVE_ATTEMPTS"}
        )

    def _handle_Resume(self, context: _Context, command: TypedCommand) -> Decision | None:
        payload = command.payload
        if (
            _snapshot_value(context.snapshot, "status") != "PAUSED"
            or _status(context.contract) != "FROZEN"
        ):
            return _reject(
                RejectionCode.INVALID_TRANSITION,
                _condition("RUN_STATE", "/command/type", required="PAUSED_WITH_FROZEN_CONTRACT"),
            )
        last = context.projection.get("last_interrupt", {})
        if not context.artifact_committed(payload.get("checkpoint_artifact_id")) or payload.get(
            "checkpoint_artifact_id"
        ) != last.get("checkpoint_artifact_id"):
            return _reject(
                RejectionCode.ENVIRONMENT_DRIFT,
                _condition("CHECKPOINT", "/command/payload/checkpoint_artifact_id"),
            )
        if payload.get("lease_preflight") is not True or context.active_leases():
            return _reject(
                RejectionCode.LEASE_CONFLICT,
                _condition("ACTIVE_LEASE", "/command/payload/lease_preflight"),
            )
        if (
            payload.get("budget_preflight") is not True
            or context.projection.get("budget_fuse_tripped") is True
        ):
            return _reject(
                RejectionCode.BUDGET_DENIED,
                _condition("BUDGET_POLICY", "/command/payload/budget_preflight"),
            )
        return _accept(command, {"op": "SET_RUN_STATUS", "status": "RUNNING"})

    def _handle_Finalize(self, context: _Context, command: TypedCommand) -> Decision | None:
        payload = command.payload
        if context.active_leases():
            return _reject(
                RejectionCode.LEASE_CONFLICT, _condition("ACTIVE_LEASE", "/command/type")
            )
        declared_open = set(payload.get("open_obligation_ids", ()))
        actual_open = set(context.projection.get("open_obligation_ids", ()))
        if declared_open != actual_open:
            return _reject(
                RejectionCode.TERMINAL_CLAIM_UNSUPPORTED,
                _condition(
                    "OPEN_OBLIGATION",
                    "/command/payload/open_obligation_ids",
                    expected=sorted(actual_open),
                ),
            )
        outcome = payload.get("outcome")
        terminals = [
            context.claims.get(str(item)) for item in payload.get("terminal_claim_ids", ())
        ]
        if any(item is None for item in terminals):
            return _reject(
                RejectionCode.TERMINAL_CLAIM_UNSUPPORTED,
                _condition("TERMINAL_SUPPORT", "/command/payload/terminal_claim_ids"),
            )
        proved_is_unsupported = outcome == "PROVED" and (
            not terminals or any(not self._claim_proved(item) for item in terminals if item)
        )
        disproved_is_unsupported = outcome == "DISPROVED" and not self._disproof_exists(
            context, terminals
        )
        route_local_is_unsupported = outcome == "ROUTE_LOCAL" and (
            not terminals
            or any(
                item and item.get("route") not in {"ROUTE_LOCAL", "LOCAL_LEMMAS_VERIFIED"}
                for item in terminals
            )
        )
        if proved_is_unsupported or disproved_is_unsupported or route_local_is_unsupported:
            return _reject(
                RejectionCode.TERMINAL_CLAIM_UNSUPPORTED,
                _condition(
                    "TERMINAL_SUPPORT", "/command/payload/terminal_claim_ids", outcome=outcome
                ),
            )
        elif outcome == "PREVIOUSLY_KNOWN" and not any(
            record.get("status") == "LITERATURE_HIT"
            and record.get("relation") == "EQUIVALENT"
            and record.get("assumptions_exact") is True
            for record in _records(
                context.projection.get("literature"), "literature_record_id"
            ).values()
        ):
            return _reject(
                RejectionCode.TERMINAL_CLAIM_UNSUPPORTED,
                _condition("TERMINAL_SUPPORT", "/command/payload/outcome", outcome=outcome),
            )
        elif outcome == "CONTRACT_DEFECTIVE" and _status(context.contract) != "DEFECT_PROPOSED":
            return _reject(
                RejectionCode.TERMINAL_CLAIM_UNSUPPORTED,
                _condition(
                    "CONTRACT_STATE", "/command/payload/outcome", required="DEFECT_PROPOSED"
                ),
            )
        return _accept(
            command,
            {"op": "SET_RUN_STATUS", "status": "CLOSED", "outcome": outcome},
            {"op": "CREATE_DOSSIER", "spec": dict(payload.get("dossier_spec", {}))},
        )

    @staticmethod
    def _claim_proved(claim: Mapping[str, Any]) -> bool:
        return bool(
            claim.get("route") == "ROUTE_PROVED"
            and claim.get("semantic") == "HUMAN_ATTESTED"
            and claim.get("closure")
            in {"CLOSED_MACHINE", "CLOSED_HUMAN", "CLOSED_HYBRID", "NOT_REQUIRED"}
            and (
                claim.get("machine") in {"KERNEL_VERIFIED", "CERTIFICATE_VERIFIED"}
                or claim.get("peer") == "ACCEPTED"
            )
        )

    @staticmethod
    def _disproof_exists(context: _Context, claims: Sequence[Mapping[str, Any] | None]) -> bool:
        ids = {str(claim.get("claim_id")) for claim in claims if claim}
        return any(
            evidence.get("claim_id") in ids
            and evidence.get("evidence_type") == "COUNTEREXAMPLE"
            and evidence.get("evidence_strength") == "HARD_MACHINE"
            and _status(evidence) in {"ACTIVE", "COMMITTED", "ACCEPTED"}
            for evidence in context.evidence.values()
        )

    def _handle_SubmitEvidence(self, context: _Context, command: TypedCommand) -> Decision | None:
        payload = command.payload
        claim = context.claims.get(str(payload.get("claim_id")))
        if (
            claim is None
            or not _object_scope(claim, run_id=context.run_id, version=context.version, active=True)
            or payload.get("statement_hash") != claim.get("statement_hash")
        ):
            return _reject(
                RejectionCode.EVIDENCE_SCOPE_MISMATCH,
                _condition("EVIDENCE_SCOPE", "/command/payload/claim_id"),
            )
        scope = payload.get("scope")
        if not isinstance(scope, Mapping) or any(
            scope.get(key) != expected
            for key, expected in (
                ("claim_id", payload.get("claim_id")),
                ("contract_version", context.version),
                ("statement_hash", payload.get("statement_hash")),
            )
        ):
            return _reject(
                RejectionCode.EVIDENCE_SCOPE_MISMATCH,
                _condition("EVIDENCE_SCOPE", "/command/payload/scope"),
            )
        evidence_type = payload.get("evidence_type")
        strength = payload.get("evidence_strength")
        if strength not in _EVIDENCE_STRENGTHS.get(str(evidence_type), set()):
            return _reject(
                RejectionCode.EVIDENCE_INSUFFICIENT,
                _condition(
                    "EVIDENCE_TYPE",
                    "/command/payload/evidence_strength",
                    evidence_type=evidence_type,
                ),
            )
        if not context.artifact_names_available(payload.get("artifact_input_names", ())):
            return _reject(
                RejectionCode.ARTIFACT_MISSING,
                _condition("ARTIFACT_STATE", "/command/payload/artifact_input_names"),
            )
        root = payload.get("evidence_root")
        provenance = payload.get("provenance")
        if (
            not isinstance(root, Mapping)
            or not root.get("root_kind")
            or not isinstance(provenance, Mapping)
            or not provenance.get("actor")
        ):
            return _reject(
                RejectionCode.EVIDENCE_INSUFFICIENT,
                _condition("EVIDENCE_ROOT", "/command/payload/evidence_root"),
            )
        return _accept(
            command,
            {"op": "APPEND_EVIDENCE", "claim_id": payload.get("claim_id"), "strength": strength},
        )

    def _handle_RecordFailure(self, context: _Context, command: TypedCommand) -> Decision | None:
        payload = command.payload
        route_id = payload.get("route_id")
        if route_id is not None and not _object_scope(
            context.routes.get(str(route_id)), run_id=context.run_id
        ):
            return _reject(
                RejectionCode.EVIDENCE_SCOPE_MISMATCH,
                _condition("OBJECT_SCOPE", "/command/payload/route_id"),
            )
        if payload.get("evidence_artifact_id") and not context.artifact_committed(
            payload.get("evidence_artifact_id")
        ):
            return _reject(
                RejectionCode.ARTIFACT_MISSING,
                _condition("ARTIFACT_STATE", "/command/payload/evidence_artifact_id"),
            )
        if payload.get("failure_kind") in {
            "FALSE_LEMMA",
            "BRIDGE_FAILURE",
            "COMPOSITION_GAP",
        } and not payload.get("first_failed_obligation_id"):
            return _reject(
                RejectionCode.EVIDENCE_INSUFFICIENT,
                _condition("OPEN_OBLIGATION", "/command/payload/first_failed_obligation_id"),
            )
        return _accept(
            command,
            {"op": "APPEND_FAILURE"},
            {
                "op": "INVALIDATE_DEPENDENCY_CLOSURE",
                "first_failed_obligation_id": payload.get("first_failed_obligation_id"),
            },
        )

    def _handle_RequestExpansion(self, context: _Context, command: TypedCommand) -> Decision | None:
        payload = command.payload
        route = context.routes.get(str(payload.get("route_id")))
        if (
            _snapshot_value(context.snapshot, "status") != "RUNNING"
            or not route
            or _status(route) not in {"ACTIVE", "BLOCKED"}
        ):
            return _reject(
                RejectionCode.INVALID_TRANSITION,
                _condition("RUN_STATE", "/command/type", required="RUNNING_ACTIVE_ROUTE"),
            )
        literature = list(
            _records(context.projection.get("literature"), "literature_record_id").values()
        )
        if not any(
            item.get("contract_version") == context.version
            and item.get("status") in {"LITERATURE_HIT", "NO_HIT_AFTER_SEARCH"}
            for item in literature
        ):
            return _reject(
                RejectionCode.EVIDENCE_INSUFFICIENT,
                _condition("LITERATURE_PLAN", "/command/payload/route_id"),
            )
        if payload.get("expected_information_gain", 0) <= 0 or not payload.get("decision_ids"):
            return _reject(
                RejectionCode.EVIDENCE_INSUFFICIENT,
                _condition("DECISION_TARGET", "/command/payload/decision_ids"),
            )
        delta = payload.get("novelty_delta")
        if (
            not isinstance(delta, Mapping)
            or not delta.get("objects")
            or route.get("novelty_zero_streak", 0) >= 2
        ):
            return _reject(
                RejectionCode.EVIDENCE_INSUFFICIENT,
                _condition("NOVELTY_DELTA", "/command/payload/novelty_delta"),
            )
        reservation = payload.get("reservation")
        limits = context.policy.get("global_budget_limits")
        denominator = context.policy.get("route_share_denominator")
        if (
            not isinstance(reservation, Mapping)
            or not isinstance(limits, Mapping)
            or not isinstance(denominator, int)
            or denominator <= 0
        ):
            return _reject(
                RejectionCode.BUDGET_DENIED,
                _condition("BUDGET_POLICY", "/command/payload/reservation"),
            )
        for resource, amount in reservation.items():
            limit = limits.get(resource)
            if (
                not isinstance(amount, int)
                or not isinstance(limit, int)
                or amount > limit // denominator
            ):
                return _reject(
                    RejectionCode.BUDGET_DENIED,
                    _condition("BUDGET_POLICY", "/command/payload/reservation", resource=resource),
                )
        return _accept(
            command,
            {"op": "APPEND_BUDGET_RESERVATION", "route_id": payload.get("route_id")},
            {"op": "APPROVE_EXPANSION_BATCH"},
        )

    def _handle_ProposeContractDefect(
        self, context: _Context, command: TypedCommand
    ) -> Decision | None:
        payload = command.payload
        if _status(context.contract) != "FROZEN":
            return _reject(
                RejectionCode.INVALID_TRANSITION,
                _condition("CONTRACT_STATE", "/contract", required="FROZEN"),
            )
        if (
            not payload.get("evidence_refs")
            or not payload.get("affected_claim_ids")
            or not context.artifact_committed(payload.get("proposed_patch_artifact_id"))
        ):
            return _reject(
                RejectionCode.EVIDENCE_INSUFFICIENT,
                _condition(
                    "CONTRACT_STATE", "/command/payload", required="EVIDENCE_AFFECTED_CLAIMS_PATCH"
                ),
            )
        if any(str(item) not in context.claims for item in payload.get("affected_claim_ids", ())):
            return _reject(
                RejectionCode.EVIDENCE_SCOPE_MISMATCH,
                _condition("OBJECT_SCOPE", "/command/payload/affected_claim_ids"),
            )
        return _accept(
            command,
            {"op": "SET_CONTRACT_STATUS", "version": context.version, "status": "DEFECT_PROPOSED"},
            {"op": "SET_RUN_STATUS", "status": "PAUSED"},
        )

    def _handle_RecordPeerReview(self, context: _Context, command: TypedCommand) -> Decision | None:
        payload = command.payload
        claim = context.claims[str(payload.get("claim_id"))]
        if payload.get("statement_hash") != claim.get(
            "statement_hash"
        ) or not context.artifact_committed(payload.get("review_artifact_id")):
            return _reject(
                RejectionCode.EVIDENCE_SCOPE_MISMATCH,
                _condition("EVIDENCE_SCOPE", "/command/payload/statement_hash"),
            )
        if (
            not isinstance(payload.get("source_graph"), Mapping)
            or not payload.get("source_graph")
            or not isinstance(payload.get("independence_profile"), Mapping)
            or "independent" not in payload.get("independence_profile", {})
        ):
            return _reject(
                RejectionCode.INDEPENDENCE_UNKNOWN,
                _condition("SOURCE_GRAPH", "/command/payload/source_graph"),
            )
        return _accept(command, {"op": "APPEND_PEER_REVIEW", "claim_id": payload.get("claim_id")})

    def _handle_RecordQualityReview(
        self, context: _Context, command: TypedCommand
    ) -> Decision | None:
        payload = command.payload
        if (
            not context.artifact_committed(payload.get("review_artifact_id"))
            or not isinstance(payload.get("dimensions"), Mapping)
            or not payload.get("dimensions")
            or payload.get("training_pool") not in {"HUMAN_SOFT_LABELS", "EXCLUDED"}
        ):
            return _reject(
                RejectionCode.EVIDENCE_SCOPE_MISMATCH,
                _condition("EVIDENCE_SCOPE", "/command/payload/dimensions"),
            )
        return _accept(
            command, {"op": "APPEND_QUALITY_REVIEW", "claim_id": payload.get("claim_id")}
        )

    def _handle_RecordLiterature(self, context: _Context, command: TypedCommand) -> Decision | None:
        payload = command.payload
        versions = {context.version}
        for item in _records(
            context.projection.get("contracts"), "version", "contract_version"
        ).values():
            item_version = item.get("version")
            if isinstance(item_version, int) and _status(item) == "SUPERSEDED":
                versions.add(item_version)
        if payload.get("contract_version") not in versions:
            return _reject(
                RejectionCode.EVIDENCE_SCOPE_MISMATCH,
                _condition("CONTRACT_VERSION", "/command/payload/contract_version"),
            )
        families = payload.get("query_families", ())
        family_ids = {
            str(item.get("family", item.get("id", "")))
            for item in families
            if isinstance(item, Mapping)
        }
        family_ids.discard("")
        if len(family_ids) < 5:
            return _reject(
                RejectionCode.INGEST_SCHEMA_INVALID,
                _condition("LITERATURE_PLAN", "/command/payload/query_families", minimum=5),
            )
        required_artifacts = [
            payload.get("query_log_artifact_id"),
            payload.get("assessment_artifact_id"),
        ]
        if payload.get("status") == "LITERATURE_HIT":
            required_artifacts.append(payload.get("reference_artifact_id"))
            if not payload.get("relation"):
                return _reject(
                    RejectionCode.EVIDENCE_INSUFFICIENT,
                    _condition("LITERATURE_PLAN", "/command/payload/relation"),
                )
        if any(not context.artifact_committed(item) for item in required_artifacts):
            return _reject(
                RejectionCode.ARTIFACT_MISSING, _condition("ARTIFACT_STATE", "/command/payload")
            )
        return _accept(command, {"op": "APPEND_LITERATURE_RECORD"})

    def _handle_RegisterBridge(self, context: _Context, command: TypedCommand) -> Decision | None:
        payload = command.payload
        source = context.claims.get(str(payload.get("source_claim_id")))
        target = context.claims.get(str(payload.get("target_claim_id")))
        if (
            not _object_scope(source, run_id=context.run_id, version=context.version, active=True)
            or not _object_scope(
                target, run_id=context.run_id, version=context.version, active=True
            )
            or source is target
        ):
            return _reject(
                RejectionCode.EVIDENCE_SCOPE_MISMATCH,
                _condition("OBJECT_SCOPE", "/command/payload/source_claim_id"),
            )
        directionality = payload.get("directionality")
        forward = payload.get("forward_obligations", ())
        reverse = payload.get("reverse_obligations", ())
        if (
            not isinstance(payload.get("term_mapping"), Mapping)
            or not isinstance(payload.get("loss_accounting"), Mapping)
            or not forward
        ):
            return _reject(
                RejectionCode.EVIDENCE_INSUFFICIENT,
                _condition("BRIDGE_DIRECTION", "/command/payload"),
            )
        if directionality in {"ONE_WAY_VALID", "EQUIVALENT_VALID"} and not payload.get(
            "target_audit_review_id"
        ):
            return _reject(
                RejectionCode.EVIDENCE_INSUFFICIENT,
                _condition("PEER_APPROVAL", "/command/payload/target_audit_review_id"),
            )
        if directionality == "EQUIVALENT_VALID" and (
            not reverse
            or any(
                item.get("status") == "OPEN"
                for item in (*forward, *reverse)
                if isinstance(item, Mapping)
            )
        ):
            return _reject(
                RejectionCode.COMPOSITION_OPEN,
                _condition("BRIDGE_DIRECTION", "/command/payload/reverse_obligations"),
            )
        return _accept(command, {"op": "REGISTER_BRIDGE", "directionality": directionality})

    def _handle_RecordLeanFeedback(
        self, context: _Context, command: TypedCommand
    ) -> Decision | None:
        payload = command.payload
        profiles = context.policy.get("verifier_profiles", {})
        profile = (
            profiles.get(payload.get("environment_profile_id"))
            if isinstance(profiles, Mapping)
            else None
        )
        if not isinstance(profile, Mapping) or profile.get("toolchain") not in {
            None,
            payload.get("toolchain"),
        }:
            return _reject(
                RejectionCode.ENVIRONMENT_DRIFT,
                _condition("MACHINE_REPLAY", "/command/payload/environment_profile_id"),
            )
        if any(
            not context.artifact_committed(payload.get(name))
            for name in ("source_artifact_id", "output_artifact_id")
        ):
            return _reject(
                RejectionCode.ARTIFACT_MISSING, _condition("ARTIFACT_STATE", "/command/payload")
            )
        return _accept(
            command,
            {"op": "APPEND_LEAN_FEEDBACK", "feedback_kind": payload.get("feedback_kind")},
            {
                "op": "INVALIDATE_FROM_FIRST_FAILURE",
                "obligation_id": payload.get("first_failed_obligation_id"),
            },
        )

    def _handle_RegisterClaim(self, context: _Context, command: TypedCommand) -> Decision | None:
        payload = command.payload
        is_first_root = payload.get("claim_kind") == "ROOT" and not any(
            item.get("claim_kind") == "ROOT" for item in context.claims.values()
        )
        if _status(context.contract) != "FROZEN" and not (
            _status(context.contract) == "DRAFT" and is_first_root
        ):
            return _reject(
                RejectionCode.CONTRACT_NOT_FROZEN,
                _condition("CONTRACT_STATE", "/contract", required="FROZEN"),
            )
        if not context.artifact_committed(payload.get("statement_artifact_id")):
            return _reject(
                RejectionCode.ARTIFACT_MISSING,
                _condition("ARTIFACT_STATE", "/command/payload/statement_artifact_id"),
            )
        try:
            digest = sha256(canonical_json_bytes(payload.get("normalized_statement"))).hexdigest()
        except CanonicalizationError:
            digest = ""
        if digest != payload.get("statement_hash") or any(
            item.get("stable_label") == payload.get("stable_label") and _status(item) == "ACTIVE"
            for item in context.claims.values()
        ):
            return _reject(
                RejectionCode.INGEST_SCHEMA_INVALID,
                _condition("EVIDENCE_SCOPE", "/command/payload/normalized_statement"),
            )
        return _accept(
            command,
            {
                "op": "REGISTER_CLAIM",
                "closure": "NOT_REQUIRED"
                if payload.get("normalized_statement", {}).get("atomic") is True
                else "OPEN",
            },
        )

    def _handle_RegisterClaimEdge(
        self, context: _Context, command: TypedCommand
    ) -> Decision | None:
        payload = command.payload
        source = context.claims.get(str(payload.get("from_claim_id")))
        target = context.claims.get(str(payload.get("to_claim_id")))
        if (
            not _object_scope(source, run_id=context.run_id, version=context.version, active=True)
            or not _object_scope(
                target, run_id=context.run_id, version=context.version, active=True
            )
            or source is target
        ):
            return _reject(
                RejectionCode.EVIDENCE_SCOPE_MISMATCH,
                _condition("OBJECT_SCOPE", "/command/payload/from_claim_id"),
            )
        if not payload.get("justification_ref") or not self._justification_exists(context, payload):
            return _reject(
                RejectionCode.COMPOSITION_OPEN,
                _condition("EDGE_JUSTIFICATION", "/command/payload/justification_ref"),
            )
        if payload.get("edge_kind") in _LOGICAL_EDGE_KINDS and self._edge_would_cycle(
            context, payload
        ):
            return _reject(
                RejectionCode.INVALID_TRANSITION,
                _condition("SOURCE_GRAPH", "/command/payload", reason="cycle"),
            )
        return _accept(
            command,
            {"op": "REGISTER_CLAIM_EDGE"},
            {"op": "INVALIDATE_PARENT_CLOSURE", "claim_id": payload.get("to_claim_id")},
        )

    @staticmethod
    def _justification_exists(context: _Context, payload: Mapping[str, Any]) -> bool:
        kind = payload.get("justification_kind")
        ref = str(payload.get("justification_ref"))
        if kind == "BRIDGE":
            bridge = context.bridges.get(ref)
            if not bridge or bridge.get("directionality") not in {
                "ONE_WAY_VALID",
                "EQUIVALENT_VALID",
            }:
                return False
            forward = (payload.get("from_claim_id"), payload.get("to_claim_id")) == (
                bridge.get("source_claim_id"),
                bridge.get("target_claim_id"),
            )
            reverse = (payload.get("from_claim_id"), payload.get("to_claim_id")) == (
                bridge.get("target_claim_id"),
                bridge.get("source_claim_id"),
            )
            return forward or (reverse and bridge.get("directionality") == "EQUIVALENT_VALID")
        if kind in {"LEAN_DECLARATION", "CHECKER_PROFILE"}:
            evidence = context.evidence.get(ref)
            return bool(
                evidence
                and evidence.get("evidence_strength") == "HARD_MACHINE"
                and evidence.get("evidence_type") in _HARD_MACHINE_TYPES
            )
        if kind == "HUMAN_ARGUMENT":
            return ref in context.reviews and context.reviews[ref].get("verdict") == "ACCEPT"
        return kind == "DEFINITIONAL" and (
            ref in context.artifacts or ref in context.evidence_summary.get("definition_refs", ())
        )

    @staticmethod
    def _edge_would_cycle(context: _Context, payload: Mapping[str, Any]) -> bool:
        adjacency: dict[str, set[str]] = {}
        for edge in (*context.edges.values(), payload):
            if edge.get("edge_kind") not in _LOGICAL_EDGE_KINDS or _status(edge) not in {
                "",
                "ACTIVE",
            }:
                continue
            source, target = edge.get("from_claim_id"), edge.get("to_claim_id")
            if edge.get("direction") == "REVERSE":
                source, target = target, source
            if isinstance(source, str) and isinstance(target, str):
                adjacency.setdefault(source, set()).add(target)
        start = str(
            payload.get("to_claim_id")
            if payload.get("direction") != "REVERSE"
            else payload.get("from_claim_id")
        )
        goal = str(
            payload.get("from_claim_id")
            if payload.get("direction") != "REVERSE"
            else payload.get("to_claim_id")
        )
        pending, seen = [start], set()
        while pending:
            node = pending.pop()
            if node == goal:
                return True
            if node not in seen:
                seen.add(node)
                pending.extend(adjacency.get(node, ()))
        return False

    def _handle_RegisterRoute(self, context: _Context, command: TypedCommand) -> Decision | None:
        payload = command.payload
        target = context.claims.get(str(payload.get("target_claim_id")))
        if _status(context.contract) != "FROZEN" or not _object_scope(
            target, run_id=context.run_id, version=context.version, active=True
        ):
            return _reject(
                RejectionCode.EVIDENCE_SCOPE_MISMATCH,
                _condition("OBJECT_SCOPE", "/command/payload/target_claim_id"),
            )
        if (
            not all(payload.get(key) for key in ("label", "representation", "tool_family"))
            or not isinstance(payload.get("approach_root"), Mapping)
            or not payload.get("approach_root")
            or not isinstance(payload.get("budget_policy"), Mapping)
            or not payload.get("budget_policy")
        ):
            return _reject(
                RejectionCode.BUDGET_DENIED, _condition("BUDGET_POLICY", "/command/payload")
            )
        if any(route.get("label") == payload.get("label") for route in context.routes.values()):
            return _reject(
                RejectionCode.INGEST_SCHEMA_INVALID,
                _condition("OBJECT_SCOPE", "/command/payload/label"),
            )
        # The v1 wire has no separate ActivateRoute command or requested-status field.  A
        # permanently SCOUT route would make RequestExpansion/RegisterAttempt unreachable, so a
        # fully validated registration enters ACTIVE directly (allowed by the frozen transition
        # outcome "SCOUT/ACTIVE").
        return _accept(command, {"op": "REGISTER_ROUTE", "status": "ACTIVE"})

    def _handle_RegisterCompositionObligation(
        self, context: _Context, command: TypedCommand
    ) -> Decision | None:
        payload = command.payload
        ids = (payload.get("parent_claim_id"), *payload.get("child_claim_ids", ()))
        if not ids or any(
            not _object_scope(
                context.claims.get(str(item)),
                run_id=context.run_id,
                version=context.version,
                active=True,
            )
            for item in ids
        ):
            return _reject(
                RejectionCode.EVIDENCE_SCOPE_MISMATCH,
                _condition("OBJECT_SCOPE", "/command/payload/child_claim_ids"),
            )
        invalid_parts = [
            name
            for name in _PART_NAMES
            if not isinstance(payload.get(name), Mapping)
            or not payload.get(name, {}).get("ref")
            or payload.get(name, {}).get("status")
            not in {"MACHINE_CHECKED", "HUMAN_ATTESTED", "OPEN", "NOT_APPLICABLE"}
        ]
        if invalid_parts or not payload.get("closure_theorem_ref"):
            return _reject(
                RejectionCode.INGEST_SCHEMA_INVALID,
                _condition("OPEN_OBLIGATION", "/command/payload", parts=invalid_parts),
            )
        return _accept(
            command,
            {"op": "REGISTER_COMPOSITION_OBLIGATION"},
            {"op": "SET_CLOSURE", "claim_id": payload.get("parent_claim_id"), "value": "OPEN"},
        )

    def _handle_SubmitClosureWitness(
        self, context: _Context, command: TypedCommand
    ) -> Decision | None:
        result = validate_closure_witness(
            context.snapshot, command.payload, context.evidence_summary, context.policy
        )
        if not result.accepted:
            return Decision(
                accepted=False,
                rejection_code=result.rejection_code,
                missing_conditions=result.missing_conditions,
            )
        return _accept(
            command,
            {
                "op": "ACCEPT_CLOSURE_WITNESS",
                "claim_id": command.payload.get("parent_claim_id"),
                "closure_state": result.closure_state,
            },
        )

    def _handle_PromoteClaim(self, context: _Context, command: TypedCommand) -> Decision | None:
        payload = command.payload
        claim = context.claims[str(payload.get("claim_id"))]
        if _status(claim) != "ACTIVE" or _status(context.contract) != "FROZEN":
            return _reject(
                RejectionCode.CONTRACT_NOT_FROZEN, _condition("CONTRACT_STATE", "/contract")
            )
        evidence = [context.evidence.get(str(item)) for item in payload.get("evidence_ids", ())]
        if not evidence or any(
            item is None or _status(item) not in {"ACTIVE", "COMMITTED", "ACCEPTED"}
            for item in evidence
        ):
            return _reject(
                RejectionCode.EVIDENCE_INSUFFICIENT,
                _condition("EVIDENCE_TYPE", "/command/payload/evidence_ids"),
            )
        axis, value = payload.get("target_axis"), payload.get("target_value")
        if axis == "MACHINE" and not self._machine_promotion(value, evidence):
            return _reject(
                RejectionCode.REPLAY_FAILED,
                _condition("MACHINE_REPLAY", "/command/payload/evidence_ids"),
            )
        if axis == "CLOSURE":
            witness = context.witnesses.get(str(payload.get("closure_witness_id")))
            expected = {
                "CLOSED_MACHINE": "MACHINE",
                "CLOSED_HUMAN": "PEER",
                "CLOSED_HYBRID": "HYBRID",
            }.get(str(value))
            if (
                not witness
                or _status(witness) != "ACCEPTED"
                or witness.get("composition_mode") != expected
                or witness.get("parent_claim_id") != payload.get("claim_id")
            ):
                return _reject(
                    RejectionCode.COMPOSITION_OPEN,
                    _condition("CLOSURE_WITNESS", "/command/payload/closure_witness_id"),
                )
        if axis == "SEMANTIC" and not self._semantic_promotion(value, evidence, context):
            return _reject(
                RejectionCode.EVIDENCE_INSUFFICIENT,
                _condition("SEMANTIC_REVIEW", "/command/payload/evidence_ids"),
            )
        if axis == "PEER" and not self._peer_promotion(evidence, context):
            return _reject(
                RejectionCode.INDEPENDENCE_UNKNOWN,
                _condition("INDEPENDENT_REVIEW", "/command/payload/evidence_ids"),
            )
        if axis == "QUALITY" and not all(
            item
            and item.get("evidence_type") in {"PEER_SIGNATURE", "NATURAL_LANGUAGE_PROOF"}
            and item.get("evidence_strength") == "HUMAN_ATTESTED"
            for item in evidence
        ):
            return _reject(
                RejectionCode.EVIDENCE_INSUFFICIENT,
                _condition("EVIDENCE_TYPE", "/command/payload/evidence_ids"),
            )
        if axis == "ROUTE" and value == "ROUTE_PROVED" and not self._claim_proved(claim):
            return _reject(
                RejectionCode.COMPOSITION_OPEN,
                _condition("TERMINAL_SUPPORT", "/command/payload/target_value"),
            )
        if (
            axis == "ROUTE"
            and value == "PREVIOUSLY_KNOWN"
            and not any(
                item.get("status") == "LITERATURE_HIT"
                and item.get("relation") == "EQUIVALENT"
                and item.get("assumptions_exact") is True
                for item in _records(
                    context.projection.get("literature"), "literature_record_id"
                ).values()
            )
        ):
            return _reject(
                RejectionCode.EVIDENCE_INSUFFICIENT,
                _condition("LITERATURE_PLAN", "/command/payload/target_value"),
            )
        return _accept(
            command,
            {
                "op": "SET_CLAIM_AXIS",
                "claim_id": payload.get("claim_id"),
                "axis": axis,
                "value": value,
            },
        )

    @staticmethod
    def _machine_promotion(value: Any, evidence: Sequence[Mapping[str, Any] | None]) -> bool:
        required_type = {
            "KERNEL_VERIFIED": "LEAN_REPLAY",
            "CERTIFICATE_VERIFIED": "CHECKER_CERTIFICATE",
        }.get(str(value))
        if required_type is None:
            return False
        for item in evidence:
            if (
                not item
                or item.get("evidence_strength") != "HARD_MACHINE"
                or item.get("evidence_type") != required_type
            ):
                return False
            replay = item.get("replay", item)
            if (
                replay.get("passed", replay.get("replay_pass")) is not True
                or replay.get("sorry_count", 0) != 0
                or replay.get("axiom_violations", ())
                or replay.get("native_decide", False)
                or replay.get("environment_drift", False)
            ):
                return False
        return True

    @staticmethod
    def _semantic_promotion(
        value: Any, evidence: Sequence[Mapping[str, Any] | None], context: _Context
    ) -> bool:
        required = {"satisfying_witness", "negation_test", "mutation_test", "backtranslation"}
        tests = context.evidence_summary.get("semantic_checks", {})
        if value == "TESTED":
            return isinstance(tests, Mapping) and required.issubset(
                key for key, passed in tests.items() if passed is True
            )
        return (
            value == "HUMAN_ATTESTED"
            and isinstance(tests, Mapping)
            and required.issubset(key for key, passed in tests.items() if passed is True)
            and all(
                item
                and item.get("evidence_type") == "SEMANTIC_AUDIT"
                and item.get("evidence_strength") == "HUMAN_ATTESTED"
                for item in evidence
            )
        )

    @staticmethod
    def _peer_promotion(evidence: Sequence[Mapping[str, Any] | None], context: _Context) -> bool:
        threshold = int(context.policy.get("peer_review_threshold", 1))
        roots = {
            str(item.get("evidence_root_id"))
            for item in evidence
            if item
            and item.get("evidence_type") == "PEER_SIGNATURE"
            and item.get("evidence_strength") == "HUMAN_ATTESTED"
            and item.get("verdict", "ACCEPT") == "ACCEPT"
            and item.get("independent") is True
        }
        return len(roots) >= threshold

    def _handle_RegisterAttempt(self, context: _Context, command: TypedCommand) -> Decision | None:
        payload = command.payload
        route = context.routes.get(str(payload.get("route_id")))
        if (
            _snapshot_value(context.snapshot, "status") != "RUNNING"
            or not route
            or _status(route) not in {"ACTIVE", "BLOCKED"}
        ):
            return _reject(
                RejectionCode.INVALID_TRANSITION,
                _condition("RUN_STATE", "/command/type", required="RUNNING_ACTIVE_ROUTE"),
            )
        ordinals: list[int] = [
            int(item["ordinal"])
            for item in context.attempts.values()
            if item.get("route_id") == payload.get("route_id")
            and isinstance(item.get("ordinal"), int)
        ]
        if payload.get("ordinal") != (max(ordinals, default=0) + 1):
            return _reject(
                RejectionCode.INVALID_TRANSITION,
                _condition(
                    "OBJECT_SCOPE",
                    "/command/payload/ordinal",
                    expected=max(ordinals, default=0) + 1,
                ),
            )
        raw_path = str(payload.get("work_relpath", ""))
        path = PurePosixPath(raw_path)
        if (
            not raw_path
            or "\\" in raw_path
            or path.is_absolute()
            or ".." in path.parts
            or not payload.get("allowed_write_set")
        ):
            return _reject(
                RejectionCode.INGEST_SCHEMA_INVALID,
                _condition("OBJECT_SCOPE", "/command/payload/work_relpath"),
            )
        requested = set(payload.get("allowed_write_set", ()))
        for attempt in context.attempts.values():
            if _status(attempt) in {"QUEUED", "RUNNING", "PAUSED"} and requested.intersection(
                attempt.get("allowed_write_set", ())
            ):
                return _reject(
                    RejectionCode.LEASE_CONFLICT,
                    _condition("ACTIVE_LEASE", "/command/payload/allowed_write_set"),
                )
        return _accept(command, {"op": "REGISTER_ATTEMPT", "status": "QUEUED"})

    def _handle_AcquireLease(self, context: _Context, command: TypedCommand) -> Decision | None:
        payload = command.payload
        attempt = context.attempts.get(str(payload.get("attempt_id")))
        if (
            _snapshot_value(context.snapshot, "status") != "RUNNING"
            or not attempt
            or _status(attempt) not in {"QUEUED", "PAUSED"}
        ):
            return _reject(
                RejectionCode.INVALID_TRANSITION,
                _condition("RUN_STATE", "/command/payload/attempt_id"),
            )
        if any(
            lease.get("attempt_id") == payload.get("attempt_id")
            for lease in context.active_leases()
        ):
            return _reject(
                RejectionCode.LEASE_CONFLICT,
                _condition("ACTIVE_LEASE", "/command/payload/attempt_id"),
            )
        binding = next(
            (
                item
                for item in _records(context.projection.get("bindings"), "binding_id").values()
                if item.get("attempt_id") == payload.get("attempt_id")
            ),
            None,
        )
        if not binding or binding.get("holder_id", payload.get("holder_id")) != payload.get(
            "holder_id"
        ):
            return _reject(
                RejectionCode.CAPABILITY_DENIED,
                _condition("LEASE_HOLDER", "/command/payload/holder_id"),
            )
        return _accept(
            command,
            {"op": "ACQUIRE_LEASE", "attempt_id": payload.get("attempt_id")},
            {"op": "SET_ATTEMPT_STATUS", "status": "RUNNING"},
        )

    def _handle_HeartbeatLease(self, context: _Context, command: TypedCommand) -> Decision | None:
        payload = command.payload
        lease = context.leases.get(str(payload.get("lease_id")))
        if (
            not lease
            or lease not in context.active_leases()
            or lease.get("holder_id") != payload.get("holder_id")
        ):
            return _reject(
                RejectionCode.LEASE_CONFLICT,
                _condition("LEASE_HOLDER", "/command/payload/holder_id"),
            )
        return _accept(
            command,
            {
                "op": "EXTEND_LEASE",
                "lease_id": payload.get("lease_id"),
                "seconds": payload.get("extend_seconds"),
            },
        )

    def _handle_ReleaseLease(self, context: _Context, command: TypedCommand) -> Decision | None:
        payload = command.payload
        lease = context.leases.get(str(payload.get("lease_id")))
        attempt = context.attempts.get(str(lease.get("attempt_id"))) if lease else None
        if (
            not lease
            or lease not in context.active_leases()
            or lease.get("holder_id") != payload.get("holder_id")
            or not attempt
            or _status(attempt) != "RUNNING"
        ):
            return _reject(
                RejectionCode.LEASE_CONFLICT,
                _condition("LEASE_HOLDER", "/command/payload/holder_id"),
            )
        requirements = (
            context.policy.get("release_artifact_requirements", {}).get(
                payload.get("terminal_attempt_status"), ()
            )
            if isinstance(context.policy.get("release_artifact_requirements", {}), Mapping)
            else ()
        )
        if any(not context.artifact_committed(item) for item in requirements):
            return _reject(
                RejectionCode.ARTIFACT_MISSING,
                _condition("ARTIFACT_STATE", "/command/payload/terminal_attempt_status"),
            )
        return _accept(
            command,
            {"op": "RELEASE_LEASE", "lease_id": payload.get("lease_id")},
            {"op": "SET_ATTEMPT_STATUS", "status": payload.get("terminal_attempt_status")},
        )

    def _handle_RecordBudget(self, context: _Context, command: TypedCommand) -> Decision | None:
        payload = command.payload
        budget_controllers = context.policy.get("budget_controller_capability_ids")
        if budget_controllers is not None and context.capability.capability_id not in set(
            budget_controllers
        ):
            return _reject(
                RejectionCode.CAPABILITY_DENIED,
                _condition("REQUIRED_ACTION", "/command/type", role="BUDGET_CONTROLLER"),
            )
        amount = payload.get("amount_microunits")
        if payload.get("event_kind") != "UNKNOWN_COST" and (
            not isinstance(amount, int) or isinstance(amount, bool) or amount < 0
        ):
            return _reject(
                RejectionCode.BUDGET_DENIED,
                _condition("BUDGET_POLICY", "/command/payload/amount_microunits"),
            )
        if payload.get("event_kind") == "REFUND":
            available = context.evidence_summary.get("refundable_microunits", {}).get(
                payload.get("resource_kind"), -1
            )
            if not isinstance(amount, int) or amount > available:
                return _reject(
                    RejectionCode.BUDGET_DENIED,
                    _condition(
                        "BUDGET_POLICY", "/command/payload/amount_microunits", available=available
                    ),
                )
        mutations: list[Mapping[str, Any]] = [{"op": "APPEND_BUDGET_EVENT"}]
        if payload.get("event_kind") == "FUSE_TRIP":
            mutations.extend(
                (
                    {"op": "BLOCK_ROUTE", "route_id": payload.get("route_id")},
                    {"op": "SET_RUN_STATUS", "status": "PAUSED"},
                )
            )
        return _accept(command, *mutations)

    def _handle_BindExecution(self, context: _Context, command: TypedCommand) -> Decision | None:
        payload = command.payload
        attempt = context.attempts.get(str(payload.get("attempt_id")))
        route = context.routes.get(str(payload.get("route_id")))
        if (
            not attempt
            or _status(attempt) != "QUEUED"
            or not route
            or attempt.get("route_id") != payload.get("route_id")
        ):
            return _reject(
                RejectionCode.INVALID_TRANSITION,
                _condition("OBJECT_SCOPE", "/command/payload/attempt_id"),
            )
        if any(
            item.get("attempt_id") == payload.get("attempt_id")
            for item in _records(context.projection.get("bindings"), "binding_id").values()
        ):
            return _reject(
                RejectionCode.INVALID_TRANSITION,
                _condition("OBJECT_SCOPE", "/command/payload/attempt_id", reason="already_bound"),
            )
        adapters = context.policy.get("adapter_profiles", {})
        adapter = (
            adapters.get(payload.get("adapter_name"), {}) if isinstance(adapters, Mapping) else {}
        )
        versions = adapter.get("versions", {}) if isinstance(adapter, Mapping) else {}
        version_profile = (
            versions.get(payload.get("adapter_version")) if isinstance(versions, Mapping) else None
        )
        if (
            not isinstance(version_profile, Mapping)
            or payload.get("environment_profile_id")
            not in version_profile.get("environment_profile_ids", ())
            or (
                payload.get("source_commit")
                and payload.get("source_commit") not in version_profile.get("source_commits", ())
            )
        ):
            return _reject(
                RejectionCode.ENVIRONMENT_DRIFT,
                _condition("MACHINE_REPLAY", "/command/payload/adapter_version"),
            )
        if not context.artifact_committed(payload.get("invocation_artifact_id")):
            return _reject(
                RejectionCode.ARTIFACT_MISSING,
                _condition("ARTIFACT_STATE", "/command/payload/invocation_artifact_id"),
            )
        return _accept(command, {"op": "BIND_EXECUTION", "attempt_id": payload.get("attempt_id")})


def decide(
    now_utc: datetime,
    snapshot: RunSnapshot | Mapping[str, Any] | None,
    command: TypedCommand,
    evidence_summary: Mapping[str, Any],
    capability: VerifiedCapability,
    policy_snapshot: Mapping[str, Any],
    expected_revision: int,
) -> Decision:
    """Functional facade matching the normative TransitionGuard signature."""

    return TransitionGuard().decide(
        now_utc=now_utc,
        snapshot=snapshot,
        command=command,
        evidence_summary=evidence_summary,
        capability=capability,
        policy_snapshot=policy_snapshot,
        expected_revision=expected_revision,
    )
