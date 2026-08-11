"""Pure selected-subgraph canonicalization and composition closure checks.

This module is deliberately free of I/O.  It checks only immutable projections and evidence
summaries supplied by the kernel.  In particular, a structurally complete human argument is
never re-labelled as machine verification.
"""

from __future__ import annotations

import json
import unicodedata
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from hashlib import sha256
from types import MappingProxyType
from typing import Any

from rk.domain import MissingCondition, RejectionCode, frozen_mapping

_PREFIX = b"rk.cgraph.v1\n"
_COLLECTION_KEYS = {
    "claims": "claim_id",
    "edges": "edge_id",
    "obligations": "obligation_id",
    "bridges": "bridge_id",
    "cuts": "cut_id",
}
_SET_ARRAY_KEYS = {
    "bridge_dependency_ids",
    "child_claim_ids",
    "discharged_obligation_ids",
    "evidence_ids",
    "from_claim_ids",
    "human_attestation_review_ids",
    "human_review_ids",
    "open_obligation_ids",
    "verification_refs",
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
_MACHINE_RULES = {"LEAN_DECLARATION", "CHECKER_PROFILE"}
_MACHINE_EVIDENCE_TYPES = {"LEAN_REPLAY", "CHECKER_CERTIFICATE"}


class CanonicalizationError(ValueError):
    """The value is outside the canonical JSON subset used by composition digests."""


@dataclass(frozen=True, slots=True)
class CompositionResult:
    accepted: bool
    closure_state: str | None = None
    rejection_code: str | None = None
    missing_conditions: tuple[MissingCondition, ...] = ()

    def __post_init__(self) -> None:
        if self.accepted and (self.closure_state is None or self.rejection_code is not None):
            raise ValueError("accepted composition result requires only a closure state")
        if not self.accepted and (self.rejection_code is None or not self.missing_conditions):
            raise ValueError("rejected composition result requires rejection details")


def _contains_surrogate(value: str) -> bool:
    return any(0xD800 <= ord(character) <= 0xDFFF for character in value)


def _normal_string(value: str) -> str:
    if _contains_surrogate(value):
        raise CanonicalizationError("unpaired Unicode surrogate is forbidden")
    return unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n"))


def _normalize(value: Any, *, parent_key: str | None = None) -> Any:
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        raise CanonicalizationError("floating-point values are forbidden")
    if isinstance(value, str):
        return _normal_string(value)
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for raw_key, raw_value in value.items():
            if not isinstance(raw_key, str):
                raise CanonicalizationError("object keys must be strings")
            key = _normal_string(raw_key)
            if key in result:
                raise CanonicalizationError("object keys collide after NFC normalization")
            result[key] = _normalize(raw_value, parent_key=key)
        return result
    if isinstance(value, Sequence) and not isinstance(value, (bytes, bytearray, memoryview)):
        items = [_normalize(item) for item in value]
        if parent_key in _COLLECTION_KEYS:
            id_key = _COLLECTION_KEYS[parent_key]
            if any(
                not isinstance(item, Mapping) or not isinstance(item.get(id_key), str)
                for item in items
            ):
                raise CanonicalizationError(f"{parent_key} entries require {id_key}")
            identifiers = [str(item[id_key]) for item in items]
            if len(identifiers) != len(set(identifiers)):
                raise CanonicalizationError(f"duplicate {id_key} in {parent_key}")
            items.sort(key=lambda item: str(item[id_key]))
        elif (parent_key in _SET_ARRAY_KEYS or (parent_key or "").endswith("_ids")) and all(
            isinstance(item, str) for item in items
        ):
            items = sorted(set(items))
        return items
    raise CanonicalizationError(f"unsupported canonical JSON type: {type(value).__name__}")


def canonicalize_json(value: Any) -> Any:
    """Return the NFC/LF/integer-only canonical JSON value without mutating ``value``."""

    return _normalize(value)


def canonical_json_bytes(value: Any) -> bytes:
    """Serialize a value using the exact deterministic JSON subset from the v1 spec."""

    normalized = canonicalize_json(value)
    return json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _validate_subgraph_shape(value: Any) -> Mapping[str, Any]:
    if not isinstance(value, Mapping):
        raise CanonicalizationError("selected subgraph must be an object")
    required = {
        "schema",
        "run_id",
        "contract_version",
        "parent",
        "claims",
        "edges",
        "obligations",
        "bridges",
        "cuts",
    }
    missing = sorted(required.difference(value))
    if missing:
        raise CanonicalizationError(f"selected subgraph missing fields: {','.join(missing)}")
    if value.get("schema") != "rk.cgraph.v1":
        raise CanonicalizationError("selected subgraph schema must be rk.cgraph.v1")
    if not isinstance(value.get("contract_version"), int) or isinstance(
        value.get("contract_version"), bool
    ):
        raise CanonicalizationError("contract_version must be an integer")
    if not isinstance(value.get("parent"), Mapping):
        raise CanonicalizationError("parent must be an object")
    for key in _COLLECTION_KEYS:
        if not isinstance(value.get(key), Sequence) or isinstance(
            value.get(key), (str, bytes, bytearray)
        ):
            raise CanonicalizationError(f"{key} must be an array")
    return value


def selected_subgraph_digest(value: Mapping[str, Any]) -> str:
    """Return lowercase SHA-256 over the versioned selected-subgraph canonical bytes."""

    _validate_subgraph_shape(value)
    return sha256(_PREFIX + canonical_json_bytes(value)).hexdigest()


canonical_subgraph_digest = selected_subgraph_digest


def _records(value: Any, *id_keys: str) -> dict[str, Mapping[str, Any]]:
    records: Iterable[Any]
    if isinstance(value, Mapping):
        records = value.values()
    elif isinstance(value, Sequence) and not isinstance(value, (str, bytes, bytearray)):
        records = value
    else:
        return {}
    result: dict[str, Mapping[str, Any]] = {}
    for item in records:
        if not isinstance(item, Mapping):
            continue
        identifier = next((item.get(key) for key in id_keys if item.get(key) is not None), None)
        if isinstance(identifier, str):
            result[identifier] = item
    return result


def _projection(snapshot: Any) -> Mapping[str, Any]:
    projection = getattr(snapshot, "projection", None)
    if isinstance(projection, Mapping):
        return projection
    return snapshot if isinstance(snapshot, Mapping) else {}


def _snapshot_value(snapshot: Any, key: str, default: Any = None) -> Any:
    direct = getattr(snapshot, key, None)
    if direct is not None:
        return direct
    if isinstance(snapshot, Mapping) and key in snapshot:
        return snapshot[key]
    return _projection(snapshot).get(key, default)


def _condition(code: str, path: str, **params: Any) -> MissingCondition:
    return MissingCondition(code=code, path=path, params=frozen_mapping(params))


def _failure(
    rejection_code: str | RejectionCode,
    conditions: MissingCondition | Iterable[MissingCondition],
) -> CompositionResult:
    items = (conditions,) if isinstance(conditions, MissingCondition) else tuple(conditions)
    return CompositionResult(
        accepted=False,
        rejection_code=str(rejection_code),
        missing_conditions=items,
    )


def _status(record: Mapping[str, Any]) -> str:
    return str(record.get("lifecycle_status", record.get("lifecycle", record.get("status", ""))))


def _directed_arcs(edge: Mapping[str, Any]) -> tuple[tuple[str, str], ...]:
    source = edge.get("from", edge.get("from_claim_id"))
    target = edge.get("to", edge.get("to_claim_id"))
    if not isinstance(source, str) or not isinstance(target, str):
        return ()
    direction = edge.get("direction")
    if direction == "FORWARD":
        return ((source, target),)
    if direction == "REVERSE":
        return ((target, source),)
    if direction == "BIDIRECTIONAL":
        return ((source, target), (target, source))
    return ()


def _has_cycle(nodes: set[str], arcs: Sequence[tuple[str, str]]) -> bool:
    outgoing: dict[str, list[str]] = {node: [] for node in nodes}
    indegree = {node: 0 for node in nodes}
    for source, target in arcs:
        if source not in nodes or target not in nodes:
            continue
        outgoing[source].append(target)
        indegree[target] += 1
    ready = sorted(node for node, degree in indegree.items() if degree == 0)
    visited = 0
    while ready:
        node = ready.pop(0)
        visited += 1
        for target in sorted(outgoing[node]):
            indegree[target] -= 1
            if indegree[target] == 0:
                ready.append(target)
                ready.sort()
    return visited != len(nodes)


def _can_reach_parent(nodes: set[str], arcs: Sequence[tuple[str, str]], parent_id: str) -> set[str]:
    reverse: dict[str, list[str]] = {node: [] for node in nodes}
    for source, target in arcs:
        if source in nodes and target in nodes:
            reverse[target].append(source)
    reached = {parent_id}
    pending = [parent_id]
    while pending:
        target = pending.pop()
        for source in reverse.get(target, ()):
            if source not in reached:
                reached.add(source)
                pending.append(source)
    return reached


def _is_hard_machine(record: Mapping[str, Any]) -> bool:
    strength = record.get("evidence_strength", record.get("strength"))
    evidence_type = record.get("evidence_type", record.get("type"))
    status = record.get("ingest_status", record.get("status", "ACTIVE"))
    replay = record.get("replay", record)
    return bool(
        strength == "HARD_MACHINE"
        and evidence_type in _MACHINE_EVIDENCE_TYPES
        and status in {"ACTIVE", "COMMITTED", "ACCEPTED", "PASS"}
        and isinstance(replay, Mapping)
        and replay.get("passed", replay.get("replay_pass", False)) is True
        and replay.get("sorry_count", 0) == 0
        and not replay.get("axiom_violations", ())
        and not replay.get("native_decide", False)
        and not replay.get("environment_drift", False)
    )


def _review_is_acceptable(
    review: Mapping[str, Any], *, parent_id: str, version: int, digest: str
) -> bool:
    profile = review.get("independence_profile", {})
    independent = review.get("independent") is True or (
        isinstance(profile, Mapping) and profile.get("independent") is True
    )
    return bool(
        review.get("verdict") == "ACCEPT"
        and review.get("claim_id") == parent_id
        and review.get("contract_version") == version
        and review.get("selected_subgraph_digest") == digest
        and independent
    )


def _verification_id(value: Any) -> str | None:
    if isinstance(value, str):
        return value
    if isinstance(value, Mapping):
        for key in ("evidence_id", "verification_ref", "ref"):
            if isinstance(value.get(key), str):
                return str(value[key])
    return None


def _all_machine_refs_valid(refs: Sequence[Any], evidence: Mapping[str, Mapping[str, Any]]) -> bool:
    identifiers = [_verification_id(value) for value in refs]
    return bool(identifiers) and all(
        identifier is not None and identifier in evidence and _is_hard_machine(evidence[identifier])
        for identifier in identifiers
    )


def _obligation_parts(
    selected: Mapping[str, Any], registered: Mapping[str, Any]
) -> dict[str, Mapping[str, Any]]:
    result: dict[str, Mapping[str, Any]] = {}
    selected_parts = selected.get("parts", {})
    for name in _PART_NAMES:
        value = selected_parts.get(name) if isinstance(selected_parts, Mapping) else None
        if not isinstance(value, Mapping):
            value = selected.get(name)
        if not isinstance(value, Mapping):
            ref = registered.get(f"{name}_ref")
            status = registered.get(f"{name}_status")
            if ref is not None or status is not None:
                value = {"ref": ref, "status": status}
        if isinstance(value, Mapping):
            result[name] = value
    return result


def _bridge_direction_valid(bridge: Mapping[str, Any], arc: tuple[str, str]) -> bool:
    directionality = bridge.get("directionality")
    source = bridge.get("source_claim_id")
    target = bridge.get("target_claim_id")
    if directionality == "EQUIVALENT_VALID":
        return arc in {(source, target), (target, source)}
    return directionality == "ONE_WAY_VALID" and arc == (source, target)


def _cut_covers_edge(cut: Mapping[str, Any], edge_id: str, edge: Mapping[str, Any]) -> bool:
    if cut.get("edge_id", cut.get("boundary_edge_id")) == edge_id:
        return True
    cut_sources = set(cut.get("from_claim_ids", ()))
    cut_target = cut.get("to_claim_id")
    return any(
        source in cut_sources and target == cut_target for source, target in _directed_arcs(edge)
    )


def validate_closure_witness(
    snapshot: Any,
    payload: Mapping[str, Any],
    evidence_summary: Mapping[str, Any],
    policy_snapshot: Mapping[str, Any],
) -> CompositionResult:
    """Validate one ``SubmitClosureWitness`` payload against immutable supplied state."""

    raw_subgraph = payload.get("selected_subgraph")
    try:
        subgraph = _validate_subgraph_shape(raw_subgraph)
        computed_digest = selected_subgraph_digest(subgraph)
    except CanonicalizationError as exc:
        return _failure(
            RejectionCode.EVIDENCE_SCOPE_MISMATCH,
            _condition("SOURCE_GRAPH", "/command/payload/selected_subgraph", reason=str(exc)),
        )
    submitted_digest = payload.get("selected_subgraph_digest")
    if submitted_digest != computed_digest:
        return _failure(
            RejectionCode.EVIDENCE_SCOPE_MISMATCH,
            _condition(
                "SUBGRAPH_DIGEST_MISMATCH",
                "/command/payload/selected_subgraph_digest",
                expected=computed_digest,
            ),
        )

    projection = _projection(snapshot)
    current_version = _snapshot_value(snapshot, "current_contract_version")
    parent_id = payload.get("parent_claim_id")
    version = payload.get("contract_version")
    subgraph_parent = subgraph.get("parent", {})
    if (
        version != current_version
        or subgraph.get("contract_version") != version
        or subgraph_parent.get("claim_id") != parent_id
        or subgraph.get("run_id") != _snapshot_value(snapshot, "run_id")
    ):
        return _failure(
            RejectionCode.EVIDENCE_SCOPE_MISMATCH,
            _condition("CONTRACT_VERSION", "/command/payload/contract_version"),
        )

    claims = _records(projection.get("claims"), "claim_id")
    edges = _records(projection.get("edges"), "edge_id")
    obligations = _records(projection.get("obligations"), "obligation_id")
    bridges = _records(projection.get("bridges"), "bridge_id")
    selected_claims = _records(subgraph.get("claims"), "claim_id")
    selected_edges = _records(subgraph.get("edges"), "edge_id")
    selected_obligations = _records(subgraph.get("obligations"), "obligation_id")
    selected_bridges = _records(subgraph.get("bridges"), "bridge_id")

    if parent_id not in selected_claims or parent_id not in claims:
        return _failure(
            RejectionCode.EVIDENCE_SCOPE_MISMATCH,
            _condition("OBJECT_NOT_FOUND", "/command/payload/parent_claim_id", claim_id=parent_id),
        )
    parent = claims[parent_id]
    if (
        _status(parent) != "ACTIVE"
        or parent.get("contract_version") != version
        or subgraph_parent.get("statement_hash") != parent.get("statement_hash")
        or subgraph_parent.get("statement_revision", 1) != parent.get("statement_revision", 1)
    ):
        return _failure(
            RejectionCode.EVIDENCE_SCOPE_MISMATCH,
            _condition("EVIDENCE_SCOPE", "/command/payload/selected_subgraph/parent"),
        )

    for claim_id, selected in selected_claims.items():
        registered = claims.get(claim_id)
        if (
            registered is None
            or _status(registered) != "ACTIVE"
            or registered.get("contract_version") != version
            or selected.get("contract_version") != version
            or selected.get("statement_hash") != registered.get("statement_hash")
            or selected.get("statement_revision", 1) != registered.get("statement_revision", 1)
        ):
            return _failure(
                RejectionCode.EVIDENCE_SCOPE_MISMATCH,
                _condition(
                    "OBJECT_SCOPE", "/command/payload/selected_subgraph/claims", claim_id=claim_id
                ),
            )

    arcs: list[tuple[str, str]] = []
    edge_justifications = _records(payload.get("edge_justifications"), "edge_id")
    for edge_id, selected in selected_edges.items():
        registered = edges.get(edge_id)
        if (
            registered is None
            or _status(registered) != "ACTIVE"
            or registered.get("contract_version") != version
        ):
            return _failure(
                RejectionCode.EVIDENCE_SCOPE_MISMATCH,
                _condition(
                    "OBJECT_SCOPE", "/command/payload/selected_subgraph/edges", edge_id=edge_id
                ),
            )
        selected_arc = _directed_arcs(selected)
        registered_arc = _directed_arcs(registered)
        if selected_arc != registered_arc or selected.get("edge_kind") != registered.get(
            "edge_kind"
        ):
            return _failure(
                RejectionCode.EVIDENCE_SCOPE_MISMATCH,
                _condition(
                    "EVIDENCE_SCOPE", "/command/payload/selected_subgraph/edges", edge_id=edge_id
                ),
            )
        if selected.get("edge_kind") in _LOGICAL_EDGE_KINDS:
            arcs.extend(selected_arc)
            justification = edge_justifications.get(edge_id)
            if (
                justification is None
                or not selected.get("justification_kind")
                or not selected.get("justification_ref")
                or justification.get("justification_ref") != selected.get("justification_ref")
            ):
                return _failure(
                    RejectionCode.COMPOSITION_OPEN,
                    _condition(
                        "EDGE_JUSTIFICATION",
                        "/command/payload/edge_justifications",
                        edge_id=edge_id,
                    ),
                )

    claim_ids = set(selected_claims)
    if _has_cycle(claim_ids, arcs):
        return _failure(
            RejectionCode.COMPOSITION_OPEN,
            _condition("SOURCE_GRAPH", "/command/payload/selected_subgraph/edges", reason="cycle"),
        )
    unreachable = sorted(claim_ids.difference(_can_reach_parent(claim_ids, arcs, str(parent_id))))
    if unreachable:
        return _failure(
            RejectionCode.COMPOSITION_OPEN,
            _condition(
                "SOURCE_GRAPH", "/command/payload/selected_subgraph/claims", unreachable=unreachable
            ),
        )

    selected_cuts = [cut for cut in subgraph.get("cuts", ()) if isinstance(cut, Mapping)]
    boundary_edges = {
        edge_id: edge
        for edge_id, edge in edges.items()
        if _status(edge) == "ACTIVE"
        and edge.get("contract_version") == version
        and edge.get("edge_kind") in _LOGICAL_EDGE_KINDS
        and any(
            (source in claim_ids) != (target in claim_ids)
            for source, target in _directed_arcs(edge)
        )
    }
    undeclared_boundary = sorted(
        edge_id
        for edge_id, edge in boundary_edges.items()
        if not any(_cut_covers_edge(cut, edge_id, edge) for cut in selected_cuts)
    )
    if undeclared_boundary:
        return _failure(
            RejectionCode.COMPOSITION_OPEN,
            _condition(
                "OPEN_OBLIGATION",
                "/command/payload/selected_subgraph/cuts",
                undeclared_boundary_edges=undeclared_boundary,
            ),
        )

    declared_bridge_ids = set(payload.get("bridge_dependency_ids", ()))
    if declared_bridge_ids != set(selected_bridges):
        return _failure(
            RejectionCode.COMPOSITION_OPEN,
            _condition("BRIDGE_DIRECTION", "/command/payload/bridge_dependency_ids"),
        )
    for bridge_id, selected in selected_bridges.items():
        registered = bridges.get(bridge_id)
        if (
            registered is None
            or registered.get("contract_version") != version
            or registered.get("directionality") != selected.get("directionality")
        ):
            return _failure(
                RejectionCode.EVIDENCE_SCOPE_MISMATCH,
                _condition(
                    "OBJECT_SCOPE",
                    "/command/payload/selected_subgraph/bridges",
                    bridge_id=bridge_id,
                ),
            )
        traversals = [
            arc
            for edge in selected_edges.values()
            if edge.get("justification_kind") == "BRIDGE"
            and edge.get("justification_ref") == bridge_id
            for arc in _directed_arcs(edge)
        ]
        if not traversals or not all(
            _bridge_direction_valid(registered, arc) for arc in traversals
        ):
            return _failure(
                RejectionCode.COMPOSITION_OPEN,
                _condition(
                    "BRIDGE_DIRECTION",
                    "/command/payload/selected_subgraph/bridges",
                    bridge_id=bridge_id,
                ),
            )

    required_obligation_ids = {
        obligation_id
        for obligation_id, obligation in obligations.items()
        if obligation.get("parent_claim_id") == parent_id
        and obligation.get("contract_version") == version
        and _status(obligation) != "INVALIDATED"
    }
    if set(selected_obligations) != required_obligation_ids:
        return _failure(
            RejectionCode.COMPOSITION_OPEN,
            _condition(
                "OPEN_OBLIGATION",
                "/command/payload/selected_subgraph/obligations",
                missing=sorted(required_obligation_ids.difference(selected_obligations)),
                extra=sorted(set(selected_obligations).difference(required_obligation_ids)),
            ),
        )

    discharged = set(payload.get("discharged_obligation_ids", ()))
    open_ids = set(payload.get("open_obligation_ids", ()))
    if discharged.intersection(open_ids) or discharged.union(open_ids) != required_obligation_ids:
        return _failure(
            RejectionCode.COMPOSITION_OPEN,
            _condition("OPEN_OBLIGATION", "/command/payload/open_obligation_ids"),
        )
    if open_ids:
        return _failure(
            RejectionCode.COMPOSITION_OPEN,
            _condition(
                "OPEN_OBLIGATION", "/command/payload/open_obligation_ids", ids=sorted(open_ids)
            ),
        )

    all_part_statuses: list[str] = []
    all_rules: list[str] = []
    for obligation_id, selected in selected_obligations.items():
        registered = obligations[obligation_id]
        if selected.get("composition_rule") != registered.get("composition_rule"):
            return _failure(
                RejectionCode.EVIDENCE_SCOPE_MISMATCH,
                _condition(
                    "EVIDENCE_SCOPE",
                    "/command/payload/selected_subgraph/obligations",
                    obligation_id=obligation_id,
                ),
            )
        parts = _obligation_parts(selected, registered)
        if set(parts) != set(_PART_NAMES):
            return _failure(
                RejectionCode.COMPOSITION_OPEN,
                _condition(
                    "OPEN_OBLIGATION",
                    "/command/payload/selected_subgraph/obligations",
                    obligation_id=obligation_id,
                ),
            )
        for name, part in parts.items():
            if not part.get("ref") or part.get("status") not in {
                "MACHINE_CHECKED",
                "HUMAN_ATTESTED",
                "OPEN",
                "NOT_APPLICABLE",
            }:
                return _failure(
                    RejectionCode.COMPOSITION_OPEN,
                    _condition(
                        "OPEN_OBLIGATION",
                        f"/command/payload/selected_subgraph/obligations/{obligation_id}/{name}",
                    ),
                )
            all_part_statuses.append(str(part["status"]))
        if not selected.get("closure_theorem_ref"):
            return _failure(
                RejectionCode.COMPOSITION_OPEN,
                _condition(
                    "CLOSURE_WITNESS",
                    "/command/payload/selected_subgraph/obligations",
                    obligation_id=obligation_id,
                ),
            )
        all_rules.append(str(selected.get("composition_rule")))
    if "OPEN" in all_part_statuses:
        return _failure(
            RejectionCode.COMPOSITION_OPEN,
            _condition("OPEN_OBLIGATION", "/command/payload/selected_subgraph/obligations"),
        )

    evidence = {
        **_records(evidence_summary.get("evidence"), "evidence_id"),
        **_records(projection.get("evidence"), "evidence_id"),
    }
    reviews = {
        **_records(evidence_summary.get("reviews"), "review_id"),
        **_records(evidence_summary.get("peer_reviews"), "review_id"),
        **_records(projection.get("reviews"), "review_id"),
        **_records(projection.get("peer_reviews"), "review_id"),
    }
    verification_refs = payload.get("verification_refs", ())
    review_ids = tuple(payload.get("human_attestation_review_ids", ()))
    mode = payload.get("composition_mode")

    if mode == "MACHINE":
        direct_edges_are_machine = all(
            edge.get("justification_kind") in _MACHINE_RULES
            for edge in selected_edges.values()
            if edge.get("edge_kind") in _LOGICAL_EDGE_KINDS
        )
        if any(status == "HUMAN_ATTESTED" for status in all_part_statuses) or any(
            rule not in _MACHINE_RULES and not (rule == "DIRECT_EDGE" and direct_edges_are_machine)
            for rule in all_rules
        ):
            return _failure(
                RejectionCode.EVIDENCE_INSUFFICIENT,
                _condition("MACHINE_REPLAY", "/command/payload/composition_mode"),
            )
        if review_ids or not _all_machine_refs_valid(verification_refs, evidence):
            return _failure(
                RejectionCode.REPLAY_FAILED,
                _condition("MACHINE_REPLAY", "/command/payload/verification_refs"),
            )
        return CompositionResult(accepted=True, closure_state="CLOSED_MACHINE")

    threshold = int(
        policy_snapshot.get(
            "peer_closure_reviewers",
            policy_snapshot.get(
                "peer_review_threshold", 2 if parent.get("claim_kind") == "ROOT" else 1
            ),
        )
    )
    accepted_reviews = [
        reviews[review_id]
        for review_id in review_ids
        if review_id in reviews
        and _review_is_acceptable(
            reviews[review_id],
            parent_id=str(parent_id),
            version=int(version),
            digest=computed_digest,
        )
    ]
    reviewer_roots = {
        str(review.get("source_root_id", review.get("reviewer_capability_id", "")))
        for review in accepted_reviews
    }
    reviews_satisfy = len(accepted_reviews) >= threshold and len(reviewer_roots) >= threshold

    if mode == "PEER":
        direct_edges_are_human = all(
            edge.get("justification_kind") == "HUMAN_ARGUMENT"
            for edge in selected_edges.values()
            if edge.get("edge_kind") in _LOGICAL_EDGE_KINDS
        )
        if any(status == "MACHINE_CHECKED" for status in all_part_statuses) or any(
            rule != "HUMAN_ARGUMENT" and not (rule == "DIRECT_EDGE" and direct_edges_are_human)
            for rule in all_rules
        ):
            return _failure(
                RejectionCode.EVIDENCE_INSUFFICIENT,
                _condition("PEER_APPROVAL", "/command/payload/composition_mode"),
            )
        if verification_refs or not reviews_satisfy:
            return _failure(
                RejectionCode.INDEPENDENCE_UNKNOWN,
                _condition(
                    "INDEPENDENT_REVIEW",
                    "/command/payload/human_attestation_review_ids",
                    required=threshold,
                ),
            )
        return CompositionResult(accepted=True, closure_state="CLOSED_HUMAN")

    if mode != "HYBRID":
        return _failure(
            RejectionCode.INGEST_SCHEMA_INVALID,
            _condition("CLOSURE_WITNESS", "/command/payload/composition_mode"),
        )
    cuts = tuple(subgraph.get("cuts", ()))
    if not cuts:
        return _failure(
            RejectionCode.COMPOSITION_OPEN,
            _condition("CLOSURE_WITNESS", "/command/payload/selected_subgraph/cuts"),
        )
    for index, cut in enumerate(cuts):
        if not isinstance(cut, Mapping):
            return _failure(
                RejectionCode.COMPOSITION_OPEN,
                _condition("CLOSURE_WITNESS", f"/command/payload/selected_subgraph/cuts/{index}"),
            )
        kind = cut.get("kind")
        if kind == "OPEN":
            return _failure(
                RejectionCode.COMPOSITION_OPEN,
                _condition("OPEN_OBLIGATION", f"/command/payload/selected_subgraph/cuts/{index}"),
            )
        if kind == "MACHINE_CHECKED":
            if cut.get("rule") not in _MACHINE_RULES or not _all_machine_refs_valid(
                cut.get("verification_refs", ()), evidence
            ):
                return _failure(
                    RejectionCode.REPLAY_FAILED,
                    _condition(
                        "MACHINE_REPLAY", f"/command/payload/selected_subgraph/cuts/{index}"
                    ),
                )
        elif kind == "HUMAN_ATTESTED":
            cut_review_ids = cut.get("human_review_ids", ())
            cut_reviews = [reviews.get(review_id, {}) for review_id in cut_review_ids]
            cut_roots = {
                str(review.get("source_root_id", review.get("reviewer_capability_id", "")))
                for review in cut_reviews
                if _review_is_acceptable(
                    review, parent_id=str(parent_id), version=int(version), digest=computed_digest
                )
            }
            if len(cut_roots) < threshold:
                return _failure(
                    RejectionCode.INDEPENDENCE_UNKNOWN,
                    _condition(
                        "INDEPENDENT_REVIEW",
                        f"/command/payload/selected_subgraph/cuts/{index}",
                        required=threshold,
                    ),
                )
        else:
            return _failure(
                RejectionCode.COMPOSITION_OPEN,
                _condition(
                    "CLOSURE_WITNESS", f"/command/payload/selected_subgraph/cuts/{index}/kind"
                ),
            )
    if not _all_machine_refs_valid(verification_refs, evidence) or not reviews_satisfy:
        return _failure(
            RejectionCode.EVIDENCE_INSUFFICIENT,
            (
                _condition("MACHINE_REPLAY", "/command/payload/verification_refs"),
                _condition(
                    "INDEPENDENT_REVIEW",
                    "/command/payload/human_attestation_review_ids",
                    required=threshold,
                ),
            ),
        )
    return CompositionResult(accepted=True, closure_state="CLOSED_HYBRID")


def immutable_subgraph(value: Mapping[str, Any]) -> Mapping[str, Any]:
    """Expose a shallow immutable normalized object for mutation-safety tests and callers."""

    normalized = canonicalize_json(value)
    if not isinstance(normalized, Mapping):
        raise CanonicalizationError("selected subgraph must normalize to an object")
    return MappingProxyType(dict(normalized))
