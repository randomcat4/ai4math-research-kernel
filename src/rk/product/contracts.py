"""Versioned research contracts with explicit ambiguity and human confirmation."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from rk.sqlite import open_sqlite
from rk.wire import canonical_json_bytes


class ContractError(RuntimeError):
    pass


class ContractConflict(ContractError):
    pass


_CONTRACT_FIELDS = (
    "objective",
    "domain",
    "quantifiers",
    "boundary_conditions",
    "exact_negation",
    "allowed_tools",
    "success_criteria",
)


@dataclass(frozen=True, slots=True)
class ContractContent:
    objective: str
    domain: str
    quantifiers: tuple[str, ...]
    boundary_conditions: tuple[str, ...]
    exact_negation: str
    allowed_tools: tuple[str, ...]
    success_criteria: tuple[str, ...]

    @classmethod
    def from_mapping(cls, value: dict[str, object]) -> ContractContent:
        if set(value) != set(_CONTRACT_FIELDS):
            raise ContractError("contract content fields are not exact")
        objective = _text(value["objective"], "objective")
        domain = _text(value["domain"], "domain")
        exact_negation = _text(value["exact_negation"], "exact_negation")
        return cls(
            objective,
            domain,
            _text_array(value["quantifiers"], "quantifiers"),
            _text_array(value["boundary_conditions"], "boundary_conditions"),
            exact_negation,
            _text_array(value["allowed_tools"], "allowed_tools"),
            _text_array(value["success_criteria"], "success_criteria"),
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "objective": self.objective,
            "domain": self.domain,
            "quantifiers": list(self.quantifiers),
            "boundary_conditions": list(self.boundary_conditions),
            "exact_negation": self.exact_negation,
            "allowed_tools": list(self.allowed_tools),
            "success_criteria": list(self.success_criteria),
        }


@dataclass(frozen=True, slots=True)
class AmbiguitySpec:
    ambiguity_id: str
    field_path: str
    description: str
    options: tuple[str, ...]

    def __post_init__(self) -> None:
        _field(self.field_path)
        _text(self.ambiguity_id, "ambiguity_id")
        _text(self.description, "description")
        if len(self.options) < 2 or len(set(self.options)) != len(self.options):
            raise ContractError("ambiguity must have at least two distinct options")
        if any(not option.strip() for option in self.options):
            raise ContractError("ambiguity options must be non-empty")


@dataclass(frozen=True, slots=True)
class ContractVersion:
    contract_id: str
    run_id: str
    version: int
    state: str
    content: ContractContent
    content_digest: str
    supersedes_version: int | None
    confirmed_by: str | None
    confirmed_at: str | None
    created_at: str


@dataclass(frozen=True, slots=True)
class ContractAmbiguity:
    ambiguity_id: str
    contract_id: str
    contract_version: int
    field_path: str
    description: str
    options: tuple[str, ...]
    state: str
    selected_option: str | None
    resolved_by: str | None
    resolved_at: str | None


class ContractStore:
    def __init__(self, db_path: Path, *, busy_timeout_ms: int = 5_000) -> None:
        self._db_path = Path(db_path)
        self._busy_timeout_ms = busy_timeout_ms

    def create_draft(
        self,
        *,
        contract_id: str,
        run_id: str,
        content: ContractContent,
        ambiguities: tuple[AmbiguitySpec, ...],
        now: str,
    ) -> ContractVersion:
        if len({item.ambiguity_id for item in ambiguities}) != len(ambiguities):
            raise ContractError("ambiguity IDs must be unique")
        if len({item.field_path for item in ambiguities}) != len(ambiguities):
            raise ContractError("one unresolved ambiguity is allowed per field")
        encoded = _json(content.to_dict())
        digest = hashlib.sha256(canonical_json_bytes(content.to_dict())).hexdigest()
        state = "AMBIGUOUS" if ambiguities else "DRAFT"
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            root = connection.execute(
                "SELECT run_id,current_version,created_at FROM product_contracts "
                "WHERE contract_id=?",
                (contract_id,),
            ).fetchone()
            if root is not None:
                existing = self.get(contract_id, 1)
                if (
                    str(root[0]) == run_id
                    and int(root[1]) == 1
                    and existing.content_digest == digest
                    and existing.state == state
                    and self.ambiguities(contract_id, 1)
                    == tuple(_ambiguity_from_spec(contract_id, item) for item in ambiguities)
                ):
                    connection.commit()
                    return existing
                raise ContractConflict("contract ID is already bound")
            connection.execute(
                "INSERT INTO product_contracts("
                "contract_id,run_id,current_version,created_at,updated_at)"
                " VALUES(?,?,1,?,?)",
                (contract_id, run_id, now, now),
            )
            connection.execute(
                "INSERT INTO product_contract_versions("
                "contract_id,version,state,content_json,content_digest,created_at)"
                " VALUES(?,1,?,?,?,?)",
                (contract_id, state, encoded, digest, now),
            )
            for item in ambiguities:
                connection.execute(
                    "INSERT INTO product_contract_ambiguities("
                    "ambiguity_id,contract_id,contract_version,field_path,description,"
                    "options_json,state) VALUES(?,?,1,?,?,?,'OPEN')",
                    (
                        item.ambiguity_id,
                        contract_id,
                        item.field_path,
                        item.description,
                        _json(list(item.options)),
                    ),
                )
            connection.commit()
        return self.get(contract_id, 1)

    def get(self, contract_id: str, version: int | None = None) -> ContractVersion:
        with self._connect() as connection:
            selected = version
            if selected is None:
                root = connection.execute(
                    "SELECT current_version FROM product_contracts WHERE contract_id=?",
                    (contract_id,),
                ).fetchone()
                if root is None:
                    raise KeyError(contract_id)
                selected = int(root[0])
            row = connection.execute(
                "SELECT v.contract_id,c.run_id,v.version,v.state,v.content_json,"
                "v.content_digest,v.supersedes_version,v.confirmed_by,v.confirmed_at,"
                "v.created_at FROM product_contract_versions v JOIN product_contracts c "
                "ON c.contract_id=v.contract_id WHERE v.contract_id=? AND v.version=?",
                (contract_id, selected),
            ).fetchone()
        if row is None:
            raise KeyError((contract_id, selected))
        return _version(row)

    def ambiguities(
        self, contract_id: str, version: int | None = None
    ) -> tuple[ContractAmbiguity, ...]:
        selected = self.get(contract_id, version).version
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT ambiguity_id,contract_id,contract_version,field_path,description,"
                "options_json,state,selected_option,resolved_by,resolved_at "
                "FROM product_contract_ambiguities WHERE contract_id=? AND contract_version=? "
                "ORDER BY field_path,ambiguity_id",
                (contract_id, selected),
            ).fetchall()
        return tuple(_ambiguity(row) for row in rows)

    def resolve_ambiguity_by_user(
        self,
        ambiguity_id: str,
        *,
        selected_option: str,
        resolved_by: str,
        actor_kind: str,
        now: str,
    ) -> ContractVersion:
        if actor_kind != "USER":
            raise ContractError("only a user may resolve contract ambiguity")
        _text(resolved_by, "resolved_by")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT contract_id,contract_version,options_json,state,selected_option,"
                "resolved_by FROM product_contract_ambiguities WHERE ambiguity_id=?",
                (ambiguity_id,),
            ).fetchone()
            if row is None:
                raise KeyError(ambiguity_id)
            options = _decode_array(row[2])
            if selected_option not in options:
                raise ContractError("selected option is not one of the frozen alternatives")
            if row[3] == "RESOLVED":
                if row[4] == selected_option and row[5] == resolved_by:
                    connection.commit()
                    return self.get(str(row[0]), int(row[1]))
                raise ContractConflict("ambiguity was already resolved differently")
            connection.execute(
                "UPDATE product_contract_ambiguities SET state='RESOLVED',selected_option=?,"
                "resolved_by=?,resolved_at=? WHERE ambiguity_id=? AND state='OPEN'",
                (selected_option, resolved_by, now, ambiguity_id),
            )
            remaining = connection.execute(
                "SELECT COUNT(*) FROM product_contract_ambiguities WHERE contract_id=? "
                "AND contract_version=? AND state='OPEN'",
                (row[0], row[1]),
            ).fetchone()
            if remaining == (0,):
                connection.execute(
                    "UPDATE product_contract_versions SET state='DRAFT' WHERE contract_id=? "
                    "AND version=? AND state='AMBIGUOUS'",
                    (row[0], row[1]),
                )
            connection.commit()
        return self.get(str(row[0]), int(row[1]))

    def confirm_by_user(
        self,
        contract_id: str,
        *,
        confirmed_by: str,
        actor_kind: str,
        now: str,
    ) -> ContractVersion:
        if actor_kind != "USER":
            raise ContractError("only a user may confirm a contract")
        _text(confirmed_by, "confirmed_by")
        current = self.get(contract_id)
        if current.state == "CONFIRMED":
            if current.confirmed_by == confirmed_by:
                return current
            raise ContractConflict("contract is already confirmed by another user")
        if current.state != "DRAFT":
            raise ContractError("only an unambiguous draft can be confirmed")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            unresolved = connection.execute(
                "SELECT COUNT(*) FROM product_contract_ambiguities WHERE contract_id=? "
                "AND contract_version=? AND state='OPEN'",
                (contract_id, current.version),
            ).fetchone()
            proposed = connection.execute(
                "SELECT COUNT(*) FROM product_contract_material_references WHERE contract_id=? "
                "AND contract_version=? AND acceptance_state<>'USER_ACCEPTED'",
                (contract_id, current.version),
            ).fetchone()
            if unresolved != (0,):
                raise ContractError("contract still has unresolved ambiguity")
            if proposed != (0,):
                raise ContractError("contract requires explicit user-accepted material anchors")
            revised_fields = connection.execute(
                "SELECT p.changed_fields_json FROM "
                "product_contract_revision_invalidations i "
                "JOIN product_contract_revision_previews p ON p.preview_id=i.preview_id "
                "WHERE i.contract_id=? AND i.new_version=? AND i.state='APPLIED'",
                (contract_id, current.version),
            ).fetchone()
            if revised_fields is not None:
                for field_path in _decode_array(revised_fields[0]):
                    accepted_for_field = connection.execute(
                        "SELECT COUNT(*) FROM product_contract_material_references "
                        "WHERE contract_id=? AND contract_version=? AND field_path=? "
                        "AND acceptance_state='USER_ACCEPTED'",
                        (contract_id, current.version, field_path),
                    ).fetchone()
                    if accepted_for_field == (0,):
                        raise ContractError(
                            "every revised field requires a newly user-accepted material anchor"
                        )
            result = connection.execute(
                "UPDATE product_contract_versions SET state='CONFIRMED',confirmed_by=?,"
                "confirmed_at=? WHERE contract_id=? AND version=? AND state='DRAFT'",
                (confirmed_by, now, contract_id, current.version),
            )
            if result.rowcount != 1:
                raise ContractConflict("contract state changed before confirmation")
            connection.execute(
                "UPDATE product_contracts SET updated_at=? WHERE contract_id=?",
                (now, contract_id),
            )
            connection.commit()
        return self.get(contract_id, current.version)

    def _connect(self) -> sqlite3.Connection:
        connection = open_sqlite(self._db_path, isolation_level=None)
        connection.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms}")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection


def _version(row: tuple[Any, ...]) -> ContractVersion:
    value = json.loads(str(row[4]))
    if not isinstance(value, dict):
        raise ContractError("persisted contract content is invalid")
    return ContractVersion(
        str(row[0]),
        str(row[1]),
        int(row[2]),
        str(row[3]),
        ContractContent.from_mapping(value),
        str(row[5]),
        int(row[6]) if row[6] is not None else None,
        str(row[7]) if row[7] is not None else None,
        str(row[8]) if row[8] is not None else None,
        str(row[9]),
    )


def _ambiguity(row: tuple[Any, ...]) -> ContractAmbiguity:
    return ContractAmbiguity(
        str(row[0]),
        str(row[1]),
        int(row[2]),
        str(row[3]),
        str(row[4]),
        tuple(_decode_array(row[5])),
        str(row[6]),
        str(row[7]) if row[7] is not None else None,
        str(row[8]) if row[8] is not None else None,
        str(row[9]) if row[9] is not None else None,
    )


def _ambiguity_from_spec(contract_id: str, value: AmbiguitySpec) -> ContractAmbiguity:
    return ContractAmbiguity(
        value.ambiguity_id,
        contract_id,
        1,
        value.field_path,
        value.description,
        value.options,
        "OPEN",
        None,
        None,
        None,
    )


def _field(value: str) -> str:
    if value not in {f"$.{field}" for field in _CONTRACT_FIELDS}:
        raise ContractError("contract field path is not a top-level contract field")
    return value


def _text(value: object, label: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ContractError(f"{label} must be a non-empty string")
    return value.strip()


def _text_array(value: object, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or not value:
        raise ContractError(f"{label} must be a non-empty array")
    result = tuple(_text(item, label) for item in value)
    if len(set(result)) != len(result):
        raise ContractError(f"{label} contains duplicates")
    return result


def _decode_array(value: object) -> list[str]:
    decoded = json.loads(str(value))
    if not isinstance(decoded, list) or any(not isinstance(item, str) for item in decoded):
        raise ContractError("persisted string array is invalid")
    return decoded


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


__all__ = [
    "AmbiguitySpec",
    "ContractAmbiguity",
    "ContractConflict",
    "ContractContent",
    "ContractError",
    "ContractStore",
    "ContractVersion",
]
