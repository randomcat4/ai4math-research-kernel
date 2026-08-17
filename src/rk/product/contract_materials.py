"""Exact material references and B11a-backed local contract revision."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from rk.extensions import AuthorityInvalidation
from rk.product.contracts import ContractContent, ContractStore, ContractVersion
from rk.product.invalidation import (
    AuthorityInvalidationEngine,
    AuthorityObjectKind,
)
from rk.product.materials import MaterialStore
from rk.sqlite import open_sqlite
from rk.wire import canonical_json_bytes


class ContractMaterialError(RuntimeError):
    pass


class ContractMaterialConflict(ContractMaterialError):
    pass


@dataclass(frozen=True, slots=True)
class ContractMaterialReference:
    reference_id: str
    contract_id: str
    contract_version: int
    field_path: str
    anchor_id: str
    anchor_kind: str
    excerpt_digest: str
    acceptance_state: str
    accepted_by: str | None
    accepted_at: str | None
    created_at: str


@dataclass(frozen=True, slots=True)
class ImpactPreview:
    preview_id: str
    contract_id: str
    base_version: int
    proposed_content: ContractContent
    proposed_content_digest: str
    changed_fields: tuple[str, ...]
    affected_objects: tuple[dict[str, str], ...]
    preserved_sibling_ids: tuple[str, ...]
    reopened_obligation_ids: tuple[str, ...]
    preview_digest: str
    state: str
    created_at: str
    applied_at: str | None


class ContractMaterialService:
    def __init__(
        self,
        *,
        db_path: Path,
        contracts: ContractStore,
        materials: MaterialStore,
        invalidation: AuthorityInvalidationEngine,
        busy_timeout_ms: int = 5_000,
    ) -> None:
        self._db_path = Path(db_path)
        self._contracts = contracts
        self._materials = materials
        self._invalidation = invalidation
        self._busy_timeout_ms = busy_timeout_ms

    def add_reference(
        self,
        *,
        reference_id: str,
        contract_id: str,
        field_path: str,
        anchor_id: str,
        now: str,
    ) -> ContractMaterialReference:
        current = self._contracts.get(contract_id)
        if current.state not in {"DRAFT", "AMBIGUOUS"}:
            raise ContractMaterialError("material references can only be proposed on a draft")
        _field(field_path)
        anchor = self._materials.get_anchor(anchor_id)
        extraction = self._materials.get_extraction(anchor.extraction_id)
        material = self._materials.get_material(extraction.material_id)
        if material.run_id != current.run_id:
            raise ContractMaterialError("material anchor crossed contract run scope")
        values = (
            contract_id,
            current.version,
            field_path,
            anchor.anchor_id,
            anchor.anchor_kind,
            anchor.excerpt_digest,
            "PROPOSED",
            None,
            None,
            now,
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT contract_id,contract_version,field_path,anchor_id,anchor_kind,"
                "excerpt_digest,acceptance_state,accepted_by,accepted_at,created_at "
                "FROM product_contract_material_references WHERE reference_id=?",
                (reference_id,),
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO product_contract_material_references("
                    "reference_id,contract_id,contract_version,field_path,anchor_id,anchor_kind,"
                    "excerpt_digest,acceptance_state,created_at)"
                    " VALUES(?,?,?,?,?,?,?,'PROPOSED',?)",
                    (
                        reference_id,
                        contract_id,
                        current.version,
                        field_path,
                        anchor.anchor_id,
                        anchor.anchor_kind,
                        anchor.excerpt_digest,
                        now,
                    ),
                )
            elif tuple(row) != values:
                raise ContractMaterialConflict("reference ID is bound differently")
            connection.commit()
        return self.get_reference(reference_id)

    def accept_reference_by_user(
        self,
        reference_id: str,
        *,
        accepted_by: str,
        actor_kind: str,
        now: str,
    ) -> ContractMaterialReference:
        if actor_kind != "USER":
            raise ContractMaterialError("only a user may accept extracted material text")
        if not accepted_by.strip():
            raise ValueError("accepted_by is required")
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT acceptance_state,accepted_by FROM "
                "product_contract_material_references WHERE reference_id=?",
                (reference_id,),
            ).fetchone()
            if row is None:
                raise KeyError(reference_id)
            if row[0] == "USER_ACCEPTED":
                if row[1] == accepted_by:
                    connection.commit()
                    return self.get_reference(reference_id)
                raise ContractMaterialConflict("reference was accepted by another identity")
            connection.execute(
                "UPDATE product_contract_material_references SET "
                "acceptance_state='USER_ACCEPTED',accepted_by=?,accepted_at=? "
                "WHERE reference_id=? AND acceptance_state='PROPOSED'",
                (accepted_by, now, reference_id),
            )
            connection.commit()
        return self.get_reference(reference_id)

    def get_reference(self, reference_id: str) -> ContractMaterialReference:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT reference_id,contract_id,contract_version,field_path,anchor_id,"
                "anchor_kind,excerpt_digest,acceptance_state,accepted_by,accepted_at,created_at "
                "FROM product_contract_material_references WHERE reference_id=?",
                (reference_id,),
            ).fetchone()
        if row is None:
            raise KeyError(reference_id)
        return ContractMaterialReference(
            str(row[0]),
            str(row[1]),
            int(row[2]),
            str(row[3]),
            str(row[4]),
            str(row[5]),
            str(row[6]),
            str(row[7]),
            str(row[8]) if row[8] is not None else None,
            str(row[9]) if row[9] is not None else None,
            str(row[10]),
        )

    def register_dependency(
        self,
        *,
        contract_id: str,
        field_path: str,
        object_kind: AuthorityObjectKind,
        object_id: str,
        reopened_obligation_id: str,
    ) -> None:
        current = self._contracts.get(contract_id)
        if current.state != "CONFIRMED":
            raise ContractMaterialError("dependencies require a confirmed contract")
        _field(field_path)
        binding = self._invalidation.get_binding(object_kind, object_id)
        if (
            binding.run_id != current.run_id
            or binding.contract_version != current.version
            or binding.state != "VALID"
        ):
            raise ContractMaterialError("authority dependency binding does not match contract")
        values = (
            contract_id,
            current.version,
            field_path,
            object_kind.value,
            object_id,
            binding.stable_label,
            binding.object_digest,
            reopened_obligation_id,
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT contract_id,contract_version,field_path,object_kind,object_id,"
                "stable_label,object_digest,reopened_obligation_id "
                "FROM product_contract_authority_dependencies WHERE contract_id=? "
                "AND contract_version=? AND field_path=? AND object_kind=? AND object_id=?",
                (contract_id, current.version, field_path, object_kind.value, object_id),
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO product_contract_authority_dependencies("
                    "contract_id,contract_version,field_path,object_kind,object_id,"
                    "stable_label,object_digest,reopened_obligation_id)"
                    " VALUES(?,?,?,?,?,?,?,?)",
                    values,
                )
            elif tuple(row) != values:
                raise ContractMaterialConflict("authority dependency differs")
            connection.commit()

    def preview_revision(
        self,
        *,
        preview_id: str,
        contract_id: str,
        proposed_content: ContractContent,
        now: str,
    ) -> ImpactPreview:
        current = self._contracts.get(contract_id)
        if current.state != "CONFIRMED":
            raise ContractMaterialError("local revision requires a confirmed contract")
        old = current.content.to_dict()
        proposed = proposed_content.to_dict()
        changed = tuple(f"$.{field}" for field in sorted(old) if old[field] != proposed[field])
        if not changed:
            raise ContractMaterialError("revision preview requires a content change")
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT field_path,object_kind,object_id,stable_label,object_digest,"
                "reopened_obligation_id FROM product_contract_authority_dependencies "
                "WHERE contract_id=? AND contract_version=? ORDER BY object_kind,object_id",
                (contract_id, current.version),
            ).fetchall()
        affected_rows = [row for row in rows if str(row[0]) in changed]
        if not affected_rows:
            raise ContractMaterialError("changed fields have no B11a authority dependencies")
        affected = tuple(
            {
                "object_kind": str(row[1]),
                "object_id": str(row[2]),
                "stable_label": str(row[3]),
                "object_digest": str(row[4]),
            }
            for row in affected_rows
        )
        affected_ids = {item["object_id"] for item in affected}
        siblings = tuple(sorted({str(row[2]) for row in rows if str(row[2]) not in affected_ids}))
        obligations = tuple(sorted({str(row[5]) for row in affected_rows}))
        content_digest = hashlib.sha256(canonical_json_bytes(proposed)).hexdigest()
        payload = {
            "contract_id": contract_id,
            "base_version": current.version,
            "proposed_content_digest": content_digest,
            "changed_fields": list(changed),
            "affected_objects": list(affected),
            "preserved_sibling_ids": list(siblings),
            "reopened_obligation_ids": list(obligations),
        }
        preview_digest = hashlib.sha256(canonical_json_bytes(payload)).hexdigest()
        values = (
            contract_id,
            current.version,
            _json(proposed),
            content_digest,
            _json(list(changed)),
            _json(list(affected)),
            _json(list(siblings)),
            _json(list(obligations)),
            preview_digest,
            "ACTIVE",
            now,
            None,
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT contract_id,base_version,proposed_content_json,"
                "proposed_content_digest,changed_fields_json,affected_objects_json,"
                "preserved_sibling_ids_json,reopened_obligation_ids_json,preview_digest,"
                "state,created_at,applied_at FROM product_contract_revision_previews "
                "WHERE preview_id=?",
                (preview_id,),
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO product_contract_revision_previews("
                    "preview_id,contract_id,base_version,proposed_content_json,"
                    "proposed_content_digest,changed_fields_json,affected_objects_json,"
                    "preserved_sibling_ids_json,reopened_obligation_ids_json,preview_digest,"
                    "state,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,'ACTIVE',?)",
                    (
                        preview_id,
                        contract_id,
                        current.version,
                        _json(proposed),
                        content_digest,
                        _json(list(changed)),
                        _json(list(affected)),
                        _json(list(siblings)),
                        _json(list(obligations)),
                        preview_digest,
                        now,
                    ),
                )
            elif tuple(row) != values:
                raise ContractMaterialConflict("preview ID is bound differently")
            connection.commit()
        return self.get_preview(preview_id)

    def get_preview(self, preview_id: str) -> ImpactPreview:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT preview_id,contract_id,base_version,proposed_content_json,"
                "proposed_content_digest,changed_fields_json,affected_objects_json,"
                "preserved_sibling_ids_json,reopened_obligation_ids_json,preview_digest,"
                "state,created_at,applied_at FROM product_contract_revision_previews "
                "WHERE preview_id=?",
                (preview_id,),
            ).fetchone()
        if row is None:
            raise KeyError(preview_id)
        content_value = _object(row[3])
        affected_value = _array(row[6])
        if any(not isinstance(item, dict) for item in affected_value):
            raise ContractMaterialError("persisted affected object array is invalid")
        return ImpactPreview(
            preview_id=str(row[0]),
            contract_id=str(row[1]),
            base_version=int(row[2]),
            proposed_content=ContractContent.from_mapping(content_value),
            proposed_content_digest=str(row[4]),
            changed_fields=tuple(_string_array(row[5])),
            affected_objects=tuple(
                {str(key): str(value) for key, value in item.items()}
                for item in affected_value
                if isinstance(item, dict)
            ),
            preserved_sibling_ids=tuple(_string_array(row[7])),
            reopened_obligation_ids=tuple(_string_array(row[8])),
            preview_digest=str(row[9]),
            state=str(row[10]),
            created_at=str(row[11]),
            applied_at=str(row[12]) if row[12] is not None else None,
        )

    def apply_revision(
        self,
        *,
        preview_id: str,
        preview_digest: str,
        kernel_event_id: str,
        research_revision: int,
        now: str,
        actor_kind: str,
        revised_by: str,
    ) -> ContractVersion:
        if actor_kind != "USER":
            raise ContractMaterialError("only a user may apply a local contract revision")
        if not revised_by.strip():
            raise ValueError("revised_by is required")
        preview = self.get_preview(preview_id)
        if preview.preview_digest != preview_digest:
            raise ContractMaterialConflict("preview digest does not match")
        if preview.state == "APPLIED":
            return self._contracts.get(preview.contract_id)
        current = self._contracts.get(preview.contract_id)
        if (
            preview.state != "ACTIVE"
            or current.version != preview.base_version
            or current.state != "CONFIRMED"
        ):
            self._mark_preview_stale(preview_id)
            raise ContractMaterialConflict("revision preview is stale")
        new_version = preview.base_version + 1
        intent = {
            "schema_version": "rk.authority_invalidation.v1",
            "reason": "CONTRACT_LOCAL_REVISION",
            "affected_objects": list(preview.affected_objects),
            "preserved_sibling_ids": list(preview.preserved_sibling_ids),
            "reopened_obligation_ids": list(preview.reopened_obligation_ids),
        }
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            state = connection.execute(
                "SELECT current_version FROM product_contracts WHERE contract_id=?",
                (preview.contract_id,),
            ).fetchone()
            preview_state = connection.execute(
                "SELECT state FROM product_contract_revision_previews WHERE preview_id=?",
                (preview_id,),
            ).fetchone()
            if state != (preview.base_version,) or preview_state != ("ACTIVE",):
                raise ContractMaterialConflict("revision state changed before prepare")
            connection.execute(
                "INSERT INTO product_contract_versions("
                "contract_id,version,state,content_json,content_digest,supersedes_version,"
                "created_at) VALUES(?,?,'PENDING_INVALIDATION',?,?,?,?)",
                (
                    preview.contract_id,
                    new_version,
                    _json(preview.proposed_content.to_dict()),
                    preview.proposed_content_digest,
                    preview.base_version,
                    now,
                ),
            )
            connection.execute(
                "UPDATE product_contract_revision_previews SET state='APPLYING' "
                "WHERE preview_id=? AND state='ACTIVE'",
                (preview_id,),
            )
            connection.execute(
                "INSERT INTO product_contract_revision_invalidations("
                "preview_id,contract_id,base_version,new_version,kernel_event_id,"
                "research_revision,invalidation_intent_json,state,created_at)"
                " VALUES(?,?,?,?,?,?,?,'PENDING',?)",
                (
                    preview_id,
                    preview.contract_id,
                    preview.base_version,
                    new_version,
                    kernel_event_id,
                    research_revision,
                    _json(intent),
                    now,
                ),
            )
            connection.commit()
        self._consume_and_finalize(preview_id, now=now)
        return self._contracts.get(preview.contract_id, new_version)

    def resume_pending(self, *, now: str) -> tuple[str, ...]:
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT preview_id FROM product_contract_revision_invalidations "
                "WHERE state='PENDING' ORDER BY created_at,preview_id"
            ).fetchall()
        completed = []
        for row in rows:
            preview_id = str(row[0])
            self._consume_and_finalize(preview_id, now=now)
            completed.append(preview_id)
        return tuple(completed)

    def _consume_and_finalize(self, preview_id: str, *, now: str) -> None:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT i.contract_id,c.run_id,i.base_version,i.new_version,"
                "i.kernel_event_id,i.research_revision,i.invalidation_intent_json "
                "FROM product_contract_revision_invalidations i "
                "JOIN product_contracts c ON c.contract_id=i.contract_id "
                "WHERE i.preview_id=? AND i.state='PENDING'",
                (preview_id,),
            ).fetchone()
        if row is None:
            return
        intent = _object(row[6])
        event = AuthorityInvalidation(
            kernel_event_id=str(row[4]),
            run_id=str(row[1]),
            research_revision=int(row[5]),
            intent=intent,
        )
        self._invalidation.consume(event)
        affected = _array(intent["affected_objects"])
        siblings = _string_values(intent["preserved_sibling_ids"])
        for value in affected:
            if not isinstance(value, dict):
                raise ContractMaterialError("pending affected object is invalid")
            binding = self._invalidation.get_binding(
                AuthorityObjectKind(str(value["object_kind"])),
                str(value["object_id"]),
            )
            if binding.state != "INVALIDATED":
                raise ContractMaterialError("B11a did not invalidate an affected object")
        for sibling_id in siblings:
            with self._connect() as connection:
                dependency = connection.execute(
                    "SELECT object_kind FROM product_contract_authority_dependencies "
                    "WHERE contract_id=? AND contract_version=? AND object_id=?",
                    (row[0], row[2], sibling_id),
                ).fetchone()
            if dependency is None:
                raise ContractMaterialError("preserved sibling dependency is missing")
            binding = self._invalidation.get_binding(
                AuthorityObjectKind(str(dependency[0])), sibling_id
            )
            if binding.state != "VALID":
                raise ContractMaterialError("B11a invalidated a preserved sibling")
        self._finalize(preview_id, now=now)

    def _finalize(self, preview_id: str, *, now: str) -> None:
        preview = self.get_preview(preview_id)
        changed = set(preview.changed_fields)
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT contract_id,base_version,new_version,state FROM "
                "product_contract_revision_invalidations WHERE preview_id=?",
                (preview_id,),
            ).fetchone()
            if row is None:
                raise KeyError(preview_id)
            if row[3] == "APPLIED":
                connection.commit()
                return
            contract_id, base_version, new_version = str(row[0]), int(row[1]), int(row[2])
            connection.execute(
                "UPDATE product_contract_versions SET state='SUPERSEDED' "
                "WHERE contract_id=? AND version=? AND state='CONFIRMED'",
                (contract_id, base_version),
            )
            connection.execute(
                "UPDATE product_contract_versions SET state='DRAFT' "
                "WHERE contract_id=? AND version=? AND state='PENDING_INVALIDATION'",
                (contract_id, new_version),
            )
            references = connection.execute(
                "SELECT reference_id,field_path,anchor_id,anchor_kind,excerpt_digest,"
                "acceptance_state,accepted_by,accepted_at,created_at "
                "FROM product_contract_material_references WHERE contract_id=? "
                "AND contract_version=? ORDER BY reference_id",
                (contract_id, base_version),
            ).fetchall()
            for reference in references:
                field_path = str(reference[1])
                if field_path in changed:
                    continue
                identity = {
                    "contract_id": contract_id,
                    "version": new_version,
                    "source_reference_id": str(reference[0]),
                }
                reference_id = (
                    "reference-" + hashlib.sha256(canonical_json_bytes(identity)).hexdigest()[:32]
                )
                connection.execute(
                    "INSERT INTO product_contract_material_references("
                    "reference_id,contract_id,contract_version,field_path,anchor_id,"
                    "anchor_kind,excerpt_digest,acceptance_state,accepted_by,accepted_at,"
                    "created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
                    (
                        reference_id,
                        contract_id,
                        new_version,
                        field_path,
                        reference[2],
                        reference[3],
                        reference[4],
                        reference[5],
                        reference[6],
                        reference[7],
                        reference[8],
                    ),
                )
            connection.execute(
                "UPDATE product_contracts SET current_version=?,updated_at=? "
                "WHERE contract_id=? AND current_version=?",
                (new_version, now, contract_id, base_version),
            )
            connection.execute(
                "UPDATE product_contract_revision_previews SET state='APPLIED',applied_at=? "
                "WHERE preview_id=? AND state='APPLYING'",
                (now, preview_id),
            )
            connection.execute(
                "UPDATE product_contract_revision_invalidations SET state='APPLIED',"
                "applied_at=? WHERE preview_id=? AND state='PENDING'",
                (now, preview_id),
            )
            connection.commit()

    def _mark_preview_stale(self, preview_id: str) -> None:
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "UPDATE product_contract_revision_previews SET state='STALE' "
                "WHERE preview_id=? AND state='ACTIVE'",
                (preview_id,),
            )
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        connection = open_sqlite(self._db_path, isolation_level=None)
        connection.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms}")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection


def _field(value: str) -> str:
    allowed = {
        "$.objective",
        "$.domain",
        "$.quantifiers",
        "$.boundary_conditions",
        "$.exact_negation",
        "$.allowed_tools",
        "$.success_criteria",
    }
    if value not in allowed:
        raise ContractMaterialError("material reference field path is invalid")
    return value


def _object(value: object) -> dict[str, object]:
    if isinstance(value, dict):
        return value
    decoded = json.loads(str(value))
    if not isinstance(decoded, dict):
        raise ContractMaterialError("persisted JSON object is invalid")
    return decoded


def _array(value: object) -> list[object]:
    if isinstance(value, list):
        return value
    decoded = json.loads(str(value))
    if not isinstance(decoded, list):
        raise ContractMaterialError("persisted JSON array is invalid")
    return decoded


def _string_values(value: object) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise ContractMaterialError("invalidation string array is invalid")
    return tuple(value)


def _string_array(value: object) -> list[str]:
    decoded = _array(value)
    if any(not isinstance(item, str) for item in decoded):
        raise ContractMaterialError("persisted string array is invalid")
    return [str(item) for item in decoded]


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


__all__ = [
    "ContractMaterialConflict",
    "ContractMaterialError",
    "ContractMaterialReference",
    "ContractMaterialService",
    "ImpactPreview",
]
