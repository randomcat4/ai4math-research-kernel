"""Single durable AuthorityInvalidation ledger and materialization engine."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
from typing import Any

from rk.extensions import AuthorityInvalidation, ExtensionRegistry


class InvalidationError(RuntimeError):
    """An authority invalidation ledger or binding invariant failed."""


class InvalidationConflict(InvalidationError):
    """An immutable event, binding, revision, or stable label was reused inconsistently."""


class AuthorityProjectionLag(InvalidationError):
    code = "AUTHORITY_PROJECTION_LAG"

    def __init__(self, watermark: InvalidationWatermark, required_revision: int) -> None:
        self.watermark = watermark
        self.required_revision = required_revision
        super().__init__(
            "authority invalidation projection is behind required kernel revision "
            f"{required_revision}"
        )


class AuthorityObjectInvalidated(InvalidationError):
    code = "AUTHORITY_OBJECT_INVALIDATED"


class AuthorityObjectKind(StrEnum):
    CHECKPOINT = "CHECKPOINT"
    QUEUE = "QUEUE"
    TOOL_FEEDBACK = "TOOL_FEEDBACK"
    COMPOSITION = "COMPOSITION"
    REVIEW = "REVIEW"
    WITNESS = "WITNESS"
    PUBLICATION = "PUBLICATION"


@dataclass(frozen=True, slots=True)
class InvalidatedObject:
    object_kind: AuthorityObjectKind
    object_id: str
    stable_label: str
    object_digest: str

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> InvalidatedObject:
        if set(value) != {"object_kind", "object_id", "stable_label", "object_digest"}:
            raise InvalidationError("affected object fields are not exact")
        try:
            return cls(
                AuthorityObjectKind(value["object_kind"]),
                _nonempty(value["object_id"]),
                _nonempty(value["stable_label"]),
                _digest(value["object_digest"]),
            )
        except (TypeError, ValueError) as error:
            raise InvalidationError("affected object values are invalid") from error

    def to_dict(self) -> dict[str, str]:
        return {
            "object_kind": self.object_kind.value,
            "object_id": self.object_id,
            "stable_label": self.stable_label,
            "object_digest": self.object_digest,
        }


@dataclass(frozen=True, slots=True)
class InvalidationIntent:
    reason: str
    affected_objects: tuple[InvalidatedObject, ...]
    preserved_sibling_ids: tuple[str, ...]
    reopened_obligation_ids: tuple[str, ...]

    @classmethod
    def from_mapping(cls, value: Mapping[str, Any]) -> InvalidationIntent:
        if set(value) != {
            "schema_version",
            "reason",
            "affected_objects",
            "preserved_sibling_ids",
            "reopened_obligation_ids",
        } or value.get("schema_version") != "rk.authority_invalidation.v1":
            raise InvalidationError("authority invalidation intent fields are not exact")
        affected_raw = _array(value["affected_objects"], "affected_objects")
        siblings_raw = _array(value["preserved_sibling_ids"], "preserved_sibling_ids")
        reopened_raw = _array(value["reopened_obligation_ids"], "reopened_obligation_ids")
        affected = tuple(
            InvalidatedObject.from_mapping(item)
            for item in affected_raw
            if isinstance(item, Mapping)
        )
        if len(affected) != len(affected_raw) or not affected:
            raise InvalidationError("affected_objects must contain objects")
        keys = [(item.object_kind, item.object_id) for item in affected]
        labels = [(item.object_kind, item.stable_label) for item in affected]
        siblings = tuple(_nonempty(item) for item in siblings_raw)
        reopened = tuple(_nonempty(item) for item in reopened_raw)
        if (
            len(set(keys)) != len(keys)
            or len(set(labels)) != len(labels)
            or len(set(siblings)) != len(siblings)
            or len(set(reopened)) != len(reopened)
            or set(siblings) & {item.object_id for item in affected}
        ):
            raise InvalidationError("invalidation closure contains duplicates or sibling overlap")
        return cls(_nonempty(value["reason"]), affected, siblings, reopened)

    def to_dict(self) -> dict[str, Any]:
        return {
            "schema_version": "rk.authority_invalidation.v1",
            "reason": self.reason,
            "affected_objects": [item.to_dict() for item in self.affected_objects],
            "preserved_sibling_ids": list(self.preserved_sibling_ids),
            "reopened_obligation_ids": list(self.reopened_obligation_ids),
        }


@dataclass(frozen=True, slots=True)
class AuthorityBinding:
    object_kind: AuthorityObjectKind
    object_id: str
    run_id: str
    contract_version: int
    bound_revision: int
    stable_label: str
    object_digest: str
    state: str
    invalidated_by_event_id: str | None


@dataclass(frozen=True, slots=True)
class InvalidationWatermark:
    run_id: str
    recorded_sequence: int
    recorded_revision: int
    processed_sequence: int
    processed_revision: int

    @property
    def caught_up(self) -> bool:
        return (
            self.processed_sequence == self.recorded_sequence
            and self.processed_revision == self.recorded_revision
        )


@dataclass(frozen=True, slots=True)
class CatchUpResult:
    watermark: InvalidationWatermark
    processed_event_ids: tuple[str, ...]


FaultHook = Callable[[str, AuthorityInvalidation], None]


class AuthorityInvalidationEngine:
    """Persist kernel closure intents, then idempotently materialize every consumer view."""

    def __init__(
        self,
        db_path: Path,
        clock: Callable[[], str],
        *,
        busy_timeout_ms: int = 5_000,
        fault_hook: FaultHook | None = None,
    ) -> None:
        self._db_path = Path(db_path)
        self._clock = clock
        self._busy_timeout_ms = busy_timeout_ms
        self._fault_hook = fault_hook

    def register_binding(
        self,
        *,
        object_kind: AuthorityObjectKind,
        object_id: str,
        run_id: str,
        contract_version: int,
        bound_revision: int,
        stable_label: str,
        object_digest: str,
    ) -> AuthorityBinding:
        values = (
            _nonempty(object_id),
            _nonempty(run_id),
            _nonempty(stable_label),
            _digest(object_digest),
        )
        if contract_version < 1 or bound_revision < 0:
            raise ValueError("binding contract version or revision is invalid")
        now = self._clock()
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                connection.execute(
                    "INSERT INTO product_authority_bindings("
                    "object_kind,object_id,run_id,contract_version,bound_revision,stable_label,"
                    "object_digest,state,created_at) VALUES(?,?,?,?,?,?,?,'VALID',?)",
                    (
                        object_kind.value,
                        values[0],
                        values[1],
                        contract_version,
                        bound_revision,
                        values[2],
                        values[3],
                        now,
                    ),
                )
                watermark = self._watermark(connection, run_id, missing=True)
                if watermark.caught_up and bound_revision > watermark.recorded_revision:
                    connection.execute(
                        "INSERT INTO product_authority_invalidation_watermarks("
                        "run_id,recorded_sequence,recorded_revision,processed_sequence,"
                        "processed_revision,updated_at) VALUES(?,0,?,0,?,?) "
                        "ON CONFLICT(run_id) DO UPDATE SET "
                        "recorded_revision=excluded.recorded_revision,"
                        "processed_revision=excluded.processed_revision,"
                        "updated_at=excluded.updated_at",
                        (run_id, bound_revision, bound_revision, now),
                    )
                connection.commit()
        except sqlite3.IntegrityError as error:
            try:
                existing = self.get_binding(object_kind, object_id)
            except KeyError as missing:
                raise InvalidationConflict(
                    "stable label is already bound to another object or digest"
                ) from missing
            if (
                existing.run_id != run_id
                or existing.contract_version != contract_version
                or existing.bound_revision != bound_revision
                or existing.stable_label != stable_label
                or existing.object_digest != object_digest
            ):
                raise InvalidationConflict(
                    "stable label or object identity has a different digest binding"
                ) from error
            return existing
        return self.get_binding(object_kind, object_id)

    def record(self, invalidation: AuthorityInvalidation) -> InvalidationWatermark:
        intent = InvalidationIntent.from_mapping(invalidation.intent)
        value = intent.to_dict()
        encoded = _json(value)
        digest = hashlib.sha256(encoded.encode("utf-8")).hexdigest()
        now = self._clock()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT run_id,research_revision,intent_digest FROM "
                "product_authority_invalidation_ledger WHERE kernel_event_id=?",
                (invalidation.kernel_event_id,),
            ).fetchone()
            if existing is not None:
                if tuple(existing) != (
                    invalidation.run_id,
                    invalidation.research_revision,
                    digest,
                ):
                    raise InvalidationConflict(
                        "kernel event ID is already bound to another invalidation"
                    )
                connection.commit()
                return self._watermark(connection, invalidation.run_id)
            current = self._watermark(connection, invalidation.run_id, missing=True)
            if invalidation.research_revision <= current.recorded_revision:
                raise InvalidationConflict("invalidation revision must advance monotonically")
            self._validate_bindings(connection, invalidation.run_id, intent)
            cursor = connection.execute(
                "INSERT INTO product_authority_invalidation_ledger("
                "kernel_event_id,run_id,research_revision,intent_digest,intent_json,recorded_at) "
                "VALUES(?,?,?,?,?,?) RETURNING sequence",
                (
                    invalidation.kernel_event_id,
                    invalidation.run_id,
                    invalidation.research_revision,
                    digest,
                    encoded,
                    now,
                ),
            ).fetchone()
            if cursor is None:
                raise InvalidationError("invalidation ledger did not allocate a sequence")
            connection.execute(
                "INSERT INTO product_authority_invalidation_watermarks("
                "run_id,recorded_sequence,recorded_revision,processed_sequence,"
                "processed_revision,updated_at) VALUES(?,?,?,0,0,?) "
                "ON CONFLICT(run_id) DO UPDATE SET recorded_sequence=excluded.recorded_sequence,"
                "recorded_revision=excluded.recorded_revision,updated_at=excluded.updated_at",
                (
                    invalidation.run_id,
                    int(cursor[0]),
                    invalidation.research_revision,
                    now,
                ),
            )
            connection.commit()
        if self._fault_hook is not None:
            self._fault_hook("AFTER_LEDGER_COMMIT", invalidation)
        return self.watermark(invalidation.run_id)

    def consume(self, invalidation: AuthorityInvalidation) -> None:
        """S00 consumer entrypoint; recording is durable and materialization is resumable."""

        self.record(invalidation)
        self.catch_up(invalidation.run_id)

    def catch_up(self, run_id: str, *, max_events: int | None = None) -> CatchUpResult:
        if not run_id or (max_events is not None and max_events <= 0):
            raise ValueError("run_id and max_events are invalid")
        processed: list[str] = []
        while max_events is None or len(processed) < max_events:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                watermark = self._watermark(connection, run_id)
                row = connection.execute(
                    "SELECT sequence,kernel_event_id,research_revision,intent_json "
                    "FROM product_authority_invalidation_ledger WHERE run_id=? AND sequence>? "
                    "ORDER BY sequence LIMIT 1",
                    (run_id, watermark.processed_sequence),
                ).fetchone()
                if row is None:
                    connection.commit()
                    return CatchUpResult(watermark, tuple(processed))
                intent_value = json.loads(str(row[3]))
                if not isinstance(intent_value, dict):
                    raise InvalidationError("stored invalidation intent is not an object")
                intent = InvalidationIntent.from_mapping(intent_value)
                self._materialize(
                    connection,
                    kernel_event_id=str(row[1]),
                    run_id=run_id,
                    intent=intent,
                )
                connection.execute(
                    "UPDATE product_authority_invalidation_watermarks SET "
                    "processed_sequence=?,processed_revision=?,updated_at=? WHERE run_id=?",
                    (int(row[0]), int(row[2]), self._clock(), run_id),
                )
                connection.commit()
            processed.append(str(row[1]))
        return CatchUpResult(self.watermark(run_id), tuple(processed))

    def assert_consumable(
        self,
        *,
        run_id: str,
        object_kind: AuthorityObjectKind,
        object_id: str,
        required_kernel_revision: int,
    ) -> AuthorityBinding:
        watermark = self.watermark(run_id)
        if (
            watermark.processed_revision < required_kernel_revision
            or watermark.processed_sequence < watermark.recorded_sequence
        ):
            raise AuthorityProjectionLag(watermark, required_kernel_revision)
        binding = self.get_binding(object_kind, object_id)
        if binding.run_id != run_id:
            raise InvalidationConflict("authority binding crossed run scope")
        if binding.state != "VALID":
            raise AuthorityObjectInvalidated(object_id)
        return binding

    def watermark(self, run_id: str) -> InvalidationWatermark:
        with self._connect() as connection:
            return self._watermark(connection, run_id)

    def get_binding(
        self, object_kind: AuthorityObjectKind, object_id: str
    ) -> AuthorityBinding:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT object_kind,object_id,run_id,contract_version,bound_revision,"
                "stable_label,object_digest,state,invalidated_by_event_id "
                "FROM product_authority_bindings WHERE object_kind=? AND object_id=?",
                (object_kind.value, object_id),
            ).fetchone()
        if row is None:
            raise KeyError((object_kind, object_id))
        return _binding(row)

    def _validate_bindings(
        self,
        connection: sqlite3.Connection,
        run_id: str,
        intent: InvalidationIntent,
    ) -> None:
        for affected in intent.affected_objects:
            row = connection.execute(
                "SELECT run_id,stable_label,object_digest FROM product_authority_bindings "
                "WHERE object_kind=? AND object_id=?",
                (affected.object_kind.value, affected.object_id),
            ).fetchone()
            if row is None or tuple(row) != (
                run_id,
                affected.stable_label,
                affected.object_digest,
            ):
                raise InvalidationConflict(
                    "affected object does not match its stable label and digest binding"
                )
        affected_ids = {item.object_id for item in intent.affected_objects}
        for sibling_id in intent.preserved_sibling_ids:
            if sibling_id in affected_ids:
                raise InvalidationConflict("preserved sibling appears in affected closure")
            sibling = connection.execute(
                "SELECT 1 FROM product_authority_bindings WHERE run_id=? AND object_id=?",
                (run_id, sibling_id),
            ).fetchone()
            if sibling is None:
                raise InvalidationConflict("preserved sibling is not registered in run")

    def _materialize(
        self,
        connection: sqlite3.Connection,
        *,
        kernel_event_id: str,
        run_id: str,
        intent: InvalidationIntent,
    ) -> None:
        now = self._clock()
        for affected in intent.affected_objects:
            row = connection.execute(
                "SELECT state FROM product_authority_bindings WHERE object_kind=? "
                "AND object_id=? AND run_id=?",
                (affected.object_kind.value, affected.object_id, run_id),
            ).fetchone()
            if row is None:
                raise InvalidationConflict("affected object disappeared during catch-up")
            previous = str(row[0])
            connection.execute(
                "INSERT INTO product_authority_invalidation_materializations("
                "kernel_event_id,object_kind,object_id,previous_state,resulting_state,"
                "materialized_at) VALUES(?,?,?,?, 'INVALIDATED',?) "
                "ON CONFLICT(kernel_event_id,object_kind,object_id) DO NOTHING",
                (
                    kernel_event_id,
                    affected.object_kind.value,
                    affected.object_id,
                    previous,
                    now,
                ),
            )
            if previous == "VALID":
                connection.execute(
                    "UPDATE product_authority_bindings SET state='INVALIDATED',"
                    "invalidated_by_event_id=?,invalidated_at=?,invalidation_reason=? "
                    "WHERE object_kind=? AND object_id=? AND state='VALID'",
                    (
                        kernel_event_id,
                        now,
                        intent.reason,
                        affected.object_kind.value,
                        affected.object_id,
                    ),
                )
                self._materialize_existing_table(connection, affected, intent.reason, now)

    @staticmethod
    def _materialize_existing_table(
        connection: sqlite3.Connection,
        affected: InvalidatedObject,
        reason: str,
        now: str,
    ) -> None:
        kind = affected.object_kind
        object_id = affected.object_id
        if kind is AuthorityObjectKind.CHECKPOINT:
            row = connection.execute(
                "SELECT job_id FROM product_job_checkpoints "
                "WHERE checkpoint_id=? AND state='ACTIVE'",
                (object_id,),
            ).fetchone()
            if row is not None:
                job_id = str(row[0])
                connection.execute(
                    "UPDATE product_job_checkpoints SET state='INVALIDATED',"
                    "invalidation_reason=?,invalidated_at=? WHERE checkpoint_id=?",
                    (reason, now, object_id),
                )
                connection.execute(
                    "UPDATE product_job_leases SET state='EXPIRED',released_at=? "
                    "WHERE job_id=? AND state='ACTIVE'",
                    (now, job_id),
                )
                connection.execute(
                    "UPDATE product_jobs SET state='INVALIDATED',failure_code=?,finished_at=? "
                    "WHERE job_id=? AND state NOT IN "
                    "('CANCELLED','SUCCEEDED','FAILED','OUTCOME_UNKNOWN','STALE','INVALIDATED')",
                    (reason, now, job_id),
                )
        elif kind is AuthorityObjectKind.QUEUE:
            connection.execute(
                "UPDATE product_jobs SET state='INVALIDATED',failure_code=?,finished_at=? "
                "WHERE job_id=? AND state NOT IN "
                "('CANCELLED','SUCCEEDED','FAILED','OUTCOME_UNKNOWN','STALE','INVALIDATED')",
                (reason, now, object_id),
            )
        elif kind is AuthorityObjectKind.TOOL_FEEDBACK:
            connection.execute(
                "UPDATE product_tool_runs SET validation_status='STALE',updated_at=? "
                "WHERE tool_run_id=? AND validation_status IN "
                "('VALIDATION_ACCEPTED','VALIDATION_REJECTED')",
                (now, object_id),
            )
        elif kind is AuthorityObjectKind.REVIEW:
            connection.execute(
                "UPDATE product_review_tasks SET status='INVALIDATED' "
                "WHERE review_task_id=? AND status IN ('OPEN','CLAIMED','EXPIRED','REASSIGNED')",
                (object_id,),
            )

    @staticmethod
    def _watermark(
        connection: sqlite3.Connection,
        run_id: str,
        *,
        missing: bool = False,
    ) -> InvalidationWatermark:
        row = connection.execute(
            "SELECT recorded_sequence,recorded_revision,processed_sequence,processed_revision "
            "FROM product_authority_invalidation_watermarks WHERE run_id=?",
            (run_id,),
        ).fetchone()
        if row is None:
            if missing:
                return InvalidationWatermark(run_id, 0, 0, 0, 0)
            raise KeyError(run_id)
        return InvalidationWatermark(run_id, *(int(item) for item in row))

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._db_path, timeout=self._busy_timeout_ms / 1_000)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
        return connection


def register_invalidation_engine(
    registry: ExtensionRegistry,
    engine: AuthorityInvalidationEngine,
    *,
    consumer_id: str = "B11A_AUTHORITY_INVALIDATION",
) -> ExtensionRegistry:
    return registry.register_invalidation_consumer(consumer_id, engine.consume)


def _binding(row: Sequence[object]) -> AuthorityBinding:
    return AuthorityBinding(
        AuthorityObjectKind(str(row[0])),
        str(row[1]),
        str(row[2]),
        int(str(row[3])),
        int(str(row[4])),
        str(row[5]),
        str(row[6]),
        str(row[7]),
        str(row[8]) if row[8] is not None else None,
    )


def _array(value: object, label: str) -> Sequence[Any]:
    if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
        raise InvalidationError(f"{label} must be an array")
    return value


def _nonempty(value: object) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValueError("value must be a non-empty string")
    return value


def _digest(value: object) -> str:
    text = _nonempty(value)
    if len(text) != 64 or any(character not in "0123456789abcdef" for character in text):
        raise ValueError("value must be a lowercase sha256")
    return text


def _json(value: object) -> str:
    return json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False
    )


__all__ = [
    "AuthorityBinding",
    "AuthorityInvalidationEngine",
    "AuthorityObjectInvalidated",
    "AuthorityObjectKind",
    "AuthorityProjectionLag",
    "CatchUpResult",
    "InvalidatedObject",
    "InvalidationConflict",
    "InvalidationError",
    "InvalidationIntent",
    "InvalidationWatermark",
    "register_invalidation_engine",
]
