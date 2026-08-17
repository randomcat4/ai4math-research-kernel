"""Revision-bound revoke previews and kernel-recomputed confirmation."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from rk.extensions import AuthorityInvalidation
from rk.product.graph_query import ClosureRequest, GraphQueryService
from rk.product.invalidation import (
    AuthorityInvalidationEngine,
    AuthorityObjectKind,
    InvalidatedObject,
)
from rk.sqlite import open_sqlite


class RevocationError(RuntimeError):
    """A revoke preview, confirmation, or replacement recovery invariant failed."""


class RevocationPreviewStale(RevocationError):
    code = "REVOCATION_PREVIEW_STALE"


class RevocationConflict(RevocationError):
    """An immutable revoke or replacement identity was reused inconsistently."""


@dataclass(frozen=True, slots=True)
class RevocationClosure:
    run_id: str
    research_revision: int
    contract_version: int
    target_fact_id: str
    target_fact_digest: str
    affected_fact_ids: tuple[str, ...]
    preserved_sibling_ids: tuple[str, ...]
    reopened_obligation_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if (
            not self.run_id
            or self.research_revision < 0
            or self.contract_version < 1
            or not self.target_fact_id
            or not _is_digest(self.target_fact_digest)
            or self.target_fact_id not in self.affected_fact_ids
        ):
            raise ValueError("revocation closure scope or target binding is invalid")
        for values in (
            self.affected_fact_ids,
            self.preserved_sibling_ids,
            self.reopened_obligation_ids,
        ):
            if any(not value for value in values) or len(set(values)) != len(values):
                raise ValueError("revocation closure identities must be non-empty and unique")
        if set(self.affected_fact_ids) & set(self.preserved_sibling_ids):
            raise ValueError("affected facts and preserved siblings must be disjoint")


@dataclass(frozen=True, slots=True)
class RevocationPreview:
    preview_id: str
    closure: RevocationClosure
    preview_digest: str
    state: str
    created_at: str


@dataclass(frozen=True, slots=True)
class ConfirmedRevocation:
    preview_id: str
    kernel_event_id: str
    kernel_revision: int
    invalidation: AuthorityInvalidation


@dataclass(frozen=True, slots=True)
class ReplacementObject:
    object_kind: AuthorityObjectKind
    object_id: str


@dataclass(frozen=True, slots=True)
class KernelReplacementReceipt:
    authority_source: str
    command_type: str
    run_id: str
    revoked_target_fact_id: str
    replacement_fact_id: str
    replacement_fact_digest: str
    restored_objects: tuple[ReplacementObject, ...]
    kernel_revision: int
    kernel_receipt_id: str
    kernel_event_id: str


@dataclass(frozen=True, slots=True)
class RevocationGraphFence:
    run_id: str
    at_cursor: int
    at_revision: int
    contract_version: int
    target_fact_id: str
    target_fact_digest: str
    preserved_sibling_ids: tuple[str, ...]
    reopened_obligation_ids: tuple[str, ...]


class RevocationPreviewMetadata(Protocol):
    """Kernel projection metadata not present in the rebuildable B06 graph index."""

    def current(self, run_id: str, target_fact_id: str) -> RevocationGraphFence: ...


class RevocationPreviewSource(Protocol):
    def preview(self, run_id: str, target_fact_id: str) -> RevocationClosure: ...


class B06RevocationPreviewReader:
    """Read the initial reverse closure from B06b at one exact kernel-provided fence."""

    def __init__(
        self,
        graph: GraphQueryService,
        metadata: RevocationPreviewMetadata,
        *,
        node_limit: int = 200,
    ) -> None:
        if not 1 <= node_limit <= 200:
            raise ValueError("revoke preview node_limit must be between 1 and 200")
        self._graph = graph
        self._metadata = metadata
        self._node_limit = node_limit

    def preview(self, run_id: str, target_fact_id: str) -> RevocationClosure:
        fence = self._metadata.current(run_id, target_fact_id)
        if fence.run_id != run_id or fence.target_fact_id != target_fact_id:
            raise RevocationConflict("revocation graph fence crossed requested target")
        graph = self._graph.reverse_closure(
            ClosureRequest(
                run_id,
                fence.at_cursor,
                fence.at_revision,
                fence.contract_version,
                target_fact_id,
                self._node_limit,
            )
        )
        if graph.truncated:
            raise RevocationConflict("revoke closure exceeds the configured graph boundary")
        affected = tuple(sorted(node.claim_id for node in graph.nodes))
        return RevocationClosure(
            run_id=run_id,
            research_revision=fence.at_revision,
            contract_version=fence.contract_version,
            target_fact_id=target_fact_id,
            target_fact_digest=fence.target_fact_digest,
            affected_fact_ids=affected,
            preserved_sibling_ids=fence.preserved_sibling_ids,
            reopened_obligation_ids=fence.reopened_obligation_ids,
        )


class RevocationKernelAuthority(Protocol):
    """ResearchKernel-owned closure algorithm and replacement proof gate."""

    def preview(self, run_id: str, target_fact_id: str) -> RevocationClosure: ...

    def recompute_in_transaction(
        self,
        connection: sqlite3.Connection,
        preview: RevocationPreview,
    ) -> RevocationClosure: ...

    def validate_replacement_in_transaction(
        self,
        connection: sqlite3.Connection,
        receipt: KernelReplacementReceipt,
    ) -> tuple[ReplacementObject, ...]: ...


class RevocationService:
    """Persist preview bindings; mathematical closure remains owned by ResearchKernel."""

    def __init__(
        self,
        db_path: Path,
        authority: RevocationKernelAuthority,
        invalidations: AuthorityInvalidationEngine,
        id_generator: Callable[[], str],
        clock: Callable[[], str],
        *,
        preview_source: RevocationPreviewSource | None = None,
        busy_timeout_ms: int = 5_000,
    ) -> None:
        self._db_path = Path(db_path)
        self._authority = authority
        self._preview_source = preview_source or authority
        self._invalidations = invalidations
        self._ids = id_generator
        self._clock = clock
        self._busy_timeout_ms = busy_timeout_ms

    def register_dependency(
        self,
        *,
        object_kind: AuthorityObjectKind,
        object_id: str,
        fact_id: str,
        run_id: str,
    ) -> None:
        binding = self._invalidations.get_binding(object_kind, object_id)
        if binding.run_id != run_id or not fact_id:
            raise RevocationConflict("revocation dependency crossed authority binding scope")
        try:
            with self._connect() as connection:
                connection.execute(
                    "INSERT INTO product_revocation_dependencies("
                    "object_kind,object_id,fact_id,run_id,created_at) VALUES(?,?,?,?,?)",
                    (object_kind.value, object_id, fact_id, run_id, self._clock()),
                )
        except sqlite3.IntegrityError as error:
            with self._connect() as connection:
                existing = connection.execute(
                    "SELECT run_id FROM product_revocation_dependencies WHERE "
                    "object_kind=? AND object_id=? AND fact_id=?",
                    (object_kind.value, object_id, fact_id),
                ).fetchone()
            if existing != (run_id,):
                raise RevocationConflict(
                    "dependency identity has another run binding"
                ) from error

    def preview(self, *, run_id: str, target_fact_id: str) -> RevocationPreview:
        closure = self._preview_source.preview(run_id, target_fact_id)
        if closure.run_id != run_id or closure.target_fact_id != target_fact_id:
            raise RevocationConflict("kernel preview crossed requested target scope")
        preview_id = self._ids()
        digest = _preview_digest(closure)
        now = self._clock()
        with self._connect() as connection:
            connection.execute(
                "INSERT INTO product_revocation_previews("
                "preview_id,run_id,target_fact_id,target_fact_digest,preview_revision,"
                "contract_version,affected_fact_ids_json,preserved_sibling_ids_json,"
                "reopened_obligation_ids_json,preview_digest,state,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,'ACTIVE',?)",
                (
                    preview_id,
                    closure.run_id,
                    closure.target_fact_id,
                    closure.target_fact_digest,
                    closure.research_revision,
                    closure.contract_version,
                    _json(list(closure.affected_fact_ids)),
                    _json(list(closure.preserved_sibling_ids)),
                    _json(list(closure.reopened_obligation_ids)),
                    digest,
                    now,
                ),
            )
        return RevocationPreview(preview_id, closure, digest, "ACTIVE", now)

    def confirm(
        self,
        *,
        preview_id: str,
        preview_digest: str,
        kernel_event_id: str,
        kernel_revision: int,
    ) -> ConfirmedRevocation:
        if not preview_id or not _is_digest(preview_digest) or not kernel_event_id:
            raise ValueError("confirm requires preview ID/digest and kernel event ID")
        stale = False
        confirmed: ConfirmedRevocation | None = None
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            preview = self._get_preview(connection, preview_id)
            if preview.state != "ACTIVE" or preview.preview_digest != preview_digest:
                connection.rollback()
                raise RevocationPreviewStale(preview_id)
            recomputed = self._authority.recompute_in_transaction(connection, preview)
            if recomputed != preview.closure or kernel_revision <= recomputed.research_revision:
                connection.execute(
                    "UPDATE product_revocation_previews SET state='STALE',stale_at=? "
                    "WHERE preview_id=? AND state='ACTIVE'",
                    (self._clock(), preview_id),
                )
                connection.commit()
                stale = True
            else:
                invalidation = self._invalidation(
                    connection,
                    closure=recomputed,
                    kernel_event_id=kernel_event_id,
                    kernel_revision=kernel_revision,
                )
                changed = connection.execute(
                    "UPDATE product_revocation_previews SET state='CONSUMED',consumed_at=?,"
                    "kernel_event_id=? WHERE preview_id=? AND state='ACTIVE'",
                    (self._clock(), kernel_event_id, preview_id),
                )
                if changed.rowcount != 1:
                    raise RevocationConflict("preview was concurrently consumed")
                connection.commit()
                confirmed = ConfirmedRevocation(
                    preview_id, kernel_event_id, kernel_revision, invalidation
                )
        if stale:
            raise RevocationPreviewStale(preview_id)
        if confirmed is None:
            raise AssertionError("confirm transaction produced no result")
        return confirmed

    def recover_with_replacement(self, receipt: KernelReplacementReceipt) -> None:
        if (
            receipt.authority_source != "RESEARCH_KERNEL"
            or receipt.command_type != "PROVE_REPLACEMENT"
            or receipt.kernel_revision < 1
            or not _is_digest(receipt.replacement_fact_digest)
            or not receipt.kernel_receipt_id
            or not receipt.kernel_event_id
            or not receipt.restored_objects
        ):
            raise RevocationConflict("replacement is not an authority-bearing kernel receipt")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            verified = self._authority.validate_replacement_in_transaction(
                connection, receipt
            )
            if verified != receipt.restored_objects:
                raise RevocationConflict("replacement restored closure differs from kernel proof")
            now = self._clock()
            for item in verified:
                binding = connection.execute(
                    "SELECT run_id,state FROM product_authority_bindings WHERE "
                    "object_kind=? AND object_id=?",
                    (item.object_kind.value, item.object_id),
                ).fetchone()
                dependency = connection.execute(
                    "SELECT 1 FROM product_revocation_dependencies d "
                    "JOIN product_revocation_previews p ON p.run_id=d.run_id "
                    "JOIN json_each(p.affected_fact_ids_json) a ON a.value=d.fact_id "
                    "WHERE d.object_kind=? AND d.object_id=? AND d.run_id=? "
                    "AND p.target_fact_id=? AND p.state='CONSUMED' LIMIT 1",
                    (
                        item.object_kind.value,
                        item.object_id,
                        receipt.run_id,
                        receipt.revoked_target_fact_id,
                    ),
                ).fetchone()
                if binding != (receipt.run_id, "INVALIDATED") or dependency is None:
                    raise RevocationConflict(
                        "replacement can restore only invalidated target-bound objects"
                    )
                connection.execute(
                    "INSERT INTO product_revocation_recoveries("
                    "run_id,revoked_target_fact_id,replacement_fact_id,"
                    "replacement_fact_digest,restored_object_kind,restored_object_id,"
                    "kernel_revision,kernel_receipt_id,kernel_event_id,created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (
                        receipt.run_id,
                        receipt.revoked_target_fact_id,
                        receipt.replacement_fact_id,
                        receipt.replacement_fact_digest,
                        item.object_kind.value,
                        item.object_id,
                        receipt.kernel_revision,
                        receipt.kernel_receipt_id,
                        receipt.kernel_event_id,
                        now,
                    ),
                )
            connection.commit()

    def effective_state(
        self, *, run_id: str, object_kind: AuthorityObjectKind, object_id: str
    ) -> str:
        binding = self._invalidations.get_binding(object_kind, object_id)
        if binding.run_id != run_id:
            raise RevocationConflict("effective state crossed run scope")
        if binding.state == "VALID":
            return "VALID"
        with self._connect() as connection:
            invalidated = connection.execute(
                "SELECT COALESCE(MAX(l.research_revision),0) FROM "
                "product_authority_invalidation_materializations m JOIN "
                "product_authority_invalidation_ledger l "
                "ON l.kernel_event_id=m.kernel_event_id WHERE l.run_id=? "
                "AND m.object_kind=? AND m.object_id=?",
                (run_id, object_kind.value, object_id),
            ).fetchone()
            recovered = connection.execute(
                "SELECT COALESCE(MAX(kernel_revision),0) FROM product_revocation_recoveries "
                "WHERE run_id=? AND restored_object_kind=? AND restored_object_id=?",
                (run_id, object_kind.value, object_id),
            ).fetchone()
        invalidated_revision = int(invalidated[0]) if invalidated is not None else 0
        recovered_revision = int(recovered[0]) if recovered is not None else 0
        return "RESTORED" if recovered_revision > invalidated_revision else "INVALIDATED"

    def get_preview(self, preview_id: str) -> RevocationPreview:
        with self._connect() as connection:
            return self._get_preview(connection, preview_id)

    def _invalidation(
        self,
        connection: sqlite3.Connection,
        *,
        closure: RevocationClosure,
        kernel_event_id: str,
        kernel_revision: int,
    ) -> AuthorityInvalidation:
        affected = self._objects_for_facts(
            connection, closure.run_id, closure.affected_fact_ids
        )
        if not affected:
            raise RevocationConflict("revoked fact closure has no registered consumers")
        affected_ids = {item.object_id for item in affected}
        preserved = tuple(
            item.object_id
            for item in self._objects_for_facts(
                connection, closure.run_id, closure.preserved_sibling_ids
            )
            if item.object_id not in affected_ids
        )
        intent: dict[str, Any] = {
            "schema_version": "rk.authority_invalidation.v1",
            "reason": "FACT_CLOSURE_REVOKED",
            "affected_objects": [item.to_dict() for item in affected],
            "preserved_sibling_ids": sorted(set(preserved)),
            "reopened_obligation_ids": list(closure.reopened_obligation_ids),
        }
        return AuthorityInvalidation(
            kernel_event_id,
            closure.run_id,
            kernel_revision,
            intent,
        )

    @staticmethod
    def _objects_for_facts(
        connection: sqlite3.Connection, run_id: str, fact_ids: Sequence[str]
    ) -> tuple[InvalidatedObject, ...]:
        if not fact_ids:
            return ()
        placeholders = ",".join("?" for _ in fact_ids)
        rows = connection.execute(
            "SELECT DISTINCT b.object_kind,b.object_id,b.stable_label,b.object_digest "
            "FROM product_revocation_dependencies d JOIN product_authority_bindings b "
            "ON b.object_kind=d.object_kind AND b.object_id=d.object_id "
            f"WHERE d.run_id=? AND d.fact_id IN ({placeholders}) "
            "ORDER BY b.object_kind,b.object_id",
            (run_id, *fact_ids),
        ).fetchall()
        return tuple(
            InvalidatedObject(
                AuthorityObjectKind(str(row[0])),
                str(row[1]),
                str(row[2]),
                str(row[3]),
            )
            for row in rows
        )

    @staticmethod
    def _get_preview(
        connection: sqlite3.Connection, preview_id: str
    ) -> RevocationPreview:
        row = connection.execute(
            "SELECT preview_id,run_id,target_fact_id,target_fact_digest,preview_revision,"
            "contract_version,affected_fact_ids_json,preserved_sibling_ids_json,"
            "reopened_obligation_ids_json,preview_digest,state,created_at "
            "FROM product_revocation_previews WHERE preview_id=?",
            (preview_id,),
        ).fetchone()
        if row is None:
            raise KeyError(preview_id)
        closure = RevocationClosure(
            run_id=str(row[1]),
            target_fact_id=str(row[2]),
            target_fact_digest=str(row[3]),
            research_revision=int(row[4]),
            contract_version=int(row[5]),
            affected_fact_ids=_strings(row[6]),
            preserved_sibling_ids=_strings(row[7]),
            reopened_obligation_ids=_strings(row[8]),
        )
        return RevocationPreview(str(row[0]), closure, str(row[9]), str(row[10]), str(row[11]))

    def _connect(self) -> sqlite3.Connection:
        connection = open_sqlite(self._db_path, timeout=self._busy_timeout_ms / 1_000)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
        return connection


def _preview_digest(closure: RevocationClosure) -> str:
    value = {
        "run_id": closure.run_id,
        "preview_revision": closure.research_revision,
        "contract_version": closure.contract_version,
        "target_fact_id": closure.target_fact_id,
        "target_fact_digest": closure.target_fact_digest,
        "affected_fact_ids": list(closure.affected_fact_ids),
        "preserved_sibling_ids": list(closure.preserved_sibling_ids),
        "reopened_obligation_ids": list(closure.reopened_obligation_ids),
    }
    return hashlib.sha256(_json(value).encode("utf-8")).hexdigest()


def _strings(value: object) -> tuple[str, ...]:
    decoded = json.loads(str(value))
    if not isinstance(decoded, list) or any(
        not isinstance(item, str) or not item for item in decoded
    ):
        raise RevocationError("stored revoke preview identity list is invalid")
    return tuple(decoded)


def _is_digest(value: str) -> bool:
    return len(value) == 64 and all(character in "0123456789abcdef" for character in value)


def _json(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


__all__ = [
    "B06RevocationPreviewReader",
    "ConfirmedRevocation",
    "KernelReplacementReceipt",
    "ReplacementObject",
    "RevocationClosure",
    "RevocationConflict",
    "RevocationError",
    "RevocationGraphFence",
    "RevocationKernelAuthority",
    "RevocationPreview",
    "RevocationPreviewMetadata",
    "RevocationPreviewSource",
    "RevocationPreviewStale",
    "RevocationService",
]
