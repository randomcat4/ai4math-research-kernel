"""Durable product operation idempotency and receipt state transitions."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any


class OperationConflict(RuntimeError):
    """A request ID was reused for a different command digest."""


class ReceiptTransitionError(RuntimeError):
    """A receipt transition did not follow its persisted state machine."""


@dataclass(frozen=True, slots=True)
class StoredReceipt:
    receipt_id: str
    receipt_version: int
    scope_key: str
    request_id: str
    request_digest: str
    state: str
    value: Mapping[str, Any]
    created_at: str
    updated_at: str


@dataclass(frozen=True, slots=True)
class Reservation:
    created: bool
    receipt: StoredReceipt


class OperationStore:
    def __init__(
        self,
        db_path: Path,
        id_generator: Callable[[], str],
        *,
        busy_timeout_ms: int = 5_000,
    ) -> None:
        self._db_path = Path(db_path)
        self._ids = id_generator
        self._busy_timeout_ms = busy_timeout_ms

    @staticmethod
    def request_digest(request: Mapping[str, Any]) -> str:
        return hashlib.sha256(_json(request).encode("utf-8")).hexdigest()

    def reserve(
        self,
        *,
        scope_key: str,
        request_id: str,
        request_digest: str,
        pending_receipt: Mapping[str, Any],
        now: str,
        after_insert: Callable[[sqlite3.Connection, str], None] | None = None,
    ) -> Reservation:
        _state_payload("PENDING", pending_receipt)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = self._find(connection, scope_key, request_id)
            if existing is not None:
                connection.commit()
                if existing.request_digest != request_digest:
                    raise OperationConflict("request id was reused with different content")
                return Reservation(False, existing)
            receipt_id = self._ids()
            value = dict(pending_receipt)
            value["receipt_id"] = receipt_id
            value["receipt_version"] = 1
            connection.execute(
                "INSERT INTO product_receipts("
                "receipt_id,receipt_version,scope_key,request_id,request_digest,state,"
                "receipt_json,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    receipt_id,
                    1,
                    scope_key,
                    request_id,
                    request_digest,
                    "PENDING",
                    _json(value),
                    now,
                    now,
                ),
            )
            if after_insert is not None:
                after_insert(connection, receipt_id)
            connection.commit()
            stored = self.get(receipt_id)
            return Reservation(True, stored)

    def decide(
        self,
        receipt_id: str,
        *,
        decision_receipt: Mapping[str, Any],
        now: str,
    ) -> StoredReceipt:
        return self._transition(receipt_id, "PENDING", "DECIDED", decision_receipt, now)

    def outcome_unknown(
        self,
        receipt_id: str,
        *,
        unknown_receipt: Mapping[str, Any],
        now: str,
    ) -> StoredReceipt:
        return self._transition(receipt_id, "PENDING", "OUTCOME_UNKNOWN", unknown_receipt, now)

    def get(self, receipt_id: str) -> StoredReceipt:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT receipt_id,receipt_version,scope_key,request_id,request_digest,"
                "state,receipt_json,created_at,updated_at FROM product_receipts "
                "WHERE receipt_id=?",
                (receipt_id,),
            ).fetchone()
        if row is None:
            raise KeyError(receipt_id)
        return self._row(row)

    def _transition(
        self,
        receipt_id: str,
        expected_state: str,
        state: str,
        value: Mapping[str, Any],
        now: str,
    ) -> StoredReceipt:
        _state_payload(state, value)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = self._get_in_transaction(connection, receipt_id)
            if current.state != expected_state:
                raise ReceiptTransitionError(
                    f"receipt {receipt_id} is {current.state}, expected {expected_state}"
                )
            version = current.receipt_version + 1
            body = dict(value)
            body["receipt_id"] = receipt_id
            body["receipt_version"] = version
            changed = connection.execute(
                "UPDATE product_receipts SET receipt_version=?,state=?,receipt_json=?,"
                "updated_at=? WHERE receipt_id=? AND receipt_version=? AND state=?",
                (
                    version,
                    state,
                    _json(body),
                    now,
                    receipt_id,
                    current.receipt_version,
                    expected_state,
                ),
            ).rowcount
            if changed != 1:
                raise ReceiptTransitionError("receipt changed concurrently")
            connection.commit()
        return self.get(receipt_id)

    def _get_in_transaction(self, connection: sqlite3.Connection, receipt_id: str) -> StoredReceipt:
        row = connection.execute(
            "SELECT receipt_id,receipt_version,scope_key,request_id,request_digest,"
            "state,receipt_json,created_at,updated_at FROM product_receipts WHERE receipt_id=?",
            (receipt_id,),
        ).fetchone()
        if row is None:
            raise KeyError(receipt_id)
        return self._row(row)

    def _find(
        self, connection: sqlite3.Connection, scope_key: str, request_id: str
    ) -> StoredReceipt | None:
        row = connection.execute(
            "SELECT receipt_id,receipt_version,scope_key,request_id,request_digest,"
            "state,receipt_json,created_at,updated_at FROM product_receipts "
            "WHERE scope_key=? AND request_id=?",
            (scope_key, request_id),
        ).fetchone()
        return None if row is None else self._row(row)

    @staticmethod
    def _row(row: tuple[object, ...]) -> StoredReceipt:
        value = json.loads(str(row[6]))
        if not isinstance(value, dict):
            raise ReceiptTransitionError("stored receipt JSON is not an object")
        return StoredReceipt(
            str(row[0]),
            int(str(row[1])),
            str(row[2]),
            str(row[3]),
            str(row[4]),
            str(row[5]),
            value,
            str(row[7]),
            str(row[8]),
        )

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._db_path, timeout=self._busy_timeout_ms / 1_000)
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(f"PRAGMA busy_timeout = {self._busy_timeout_ms}")
        return connection


def _state_payload(state: str, value: Mapping[str, Any]) -> None:
    if value.get("state") != state:
        raise ReceiptTransitionError("receipt body state does not match transition")
    if state == "PENDING" and not value.get("job_id"):
        raise ReceiptTransitionError("PENDING receipt requires job_id")
    if state == "DECIDED" and not isinstance(value.get("decision"), Mapping):
        raise ReceiptTransitionError("DECIDED receipt requires decision")
    if state == "OUTCOME_UNKNOWN" and not value.get("unknown_external_call_ref"):
        raise ReceiptTransitionError("OUTCOME_UNKNOWN requires external call reference")


def _json(value: Mapping[str, Any]) -> str:
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
