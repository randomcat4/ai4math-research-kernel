"""B08 snapshot ingestion and formal GLOBAL command execution for arXiv batches."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
import uuid
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from types import MappingProxyType
from typing import Protocol, cast

from rk.product.api import (
    GlobalScope,
    JsonObject,
    JsonValue,
    ProductCommand,
    ProductReceipt,
    ProductSession,
)
from rk.product.artifact_read import ExactArtifactRef
from rk.product.problem_pool import ProblemPool, ProblemPoolStore, SourceEntry
from rk.product.source_snapshots import SourceSnapshot


class ArxivBatchError(RuntimeError):
    pass


class SnapshotSource(Protocol):
    def get(self, snapshot_id: str) -> SourceSnapshot: ...


class ArtifactReader(Protocol):
    def open_range(
        self, artifact_id: str, *, expected_ref: ExactArtifactRef | None = None
    ) -> object: ...


class ProductCommandClient(Protocol):
    def command(self, session: ProductSession, request: ProductCommand) -> ProductReceipt: ...


@dataclass(frozen=True, slots=True)
class BatchExecution:
    batch_id: str
    state: str
    created_runs: tuple[tuple[str, str], ...]


_ATOM = {"atom": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
_VERSIONED_ID = re.compile(r"^(?P<id>.+?)v(?P<version>[1-9][0-9]*)$")
_MARKER = re.compile(r"\b(conjecture|problem|question)\b", re.IGNORECASE)
_SENTENCE = re.compile(r"[^.!?]*(?:conjecture|problem|question)[^.!?]*(?:[.!?]|$)", re.IGNORECASE)


class ArxivBatchPipeline:
    def __init__(
        self,
        *,
        db_path: Path,
        pools: ProblemPoolStore,
        snapshots: SnapshotSource,
        artifacts: ArtifactReader,
        commands: ProductCommandClient,
        busy_timeout_ms: int = 5_000,
    ) -> None:
        self._db_path = Path(db_path)
        self._pools = pools
        self._snapshots = snapshots
        self._artifacts = artifacts
        self._commands = commands
        self._busy_timeout_ms = busy_timeout_ms

    def ingest_snapshots(
        self,
        *,
        problem_pool_id: str,
        snapshot_ids: tuple[str, ...],
        now: str,
    ) -> None:
        if not snapshot_ids or len(set(snapshot_ids)) != len(snapshot_ids):
            raise ValueError("arXiv snapshot IDs must be non-empty and unique")
        pool = self._pools.get(problem_pool_id)
        parsed: dict[str, tuple[_ArxivEntry, ...]] = {}
        failures: dict[str, tuple[str, str]] = {}
        for snapshot_id in snapshot_ids:
            snapshot = self._snapshots.get(snapshot_id)
            if snapshot.connector != "ARXIV":
                failures[snapshot_id] = ("BLOCKED", "NON_ARXIV_SNAPSHOT")
                continue
            if snapshot.result_status not in {"SUCCESS", "NO_HIT"}:
                failures[snapshot_id] = (
                    "FAILED",
                    snapshot.error_code or f"ARXIV_{snapshot.result_status}",
                )
                continue
            raw = self._read(snapshot.raw_response)
            try:
                parsed[snapshot_id] = _parse_atom(raw)
            except (ET.ParseError, ValueError):
                failures[snapshot_id] = ("FAILED", "ARXIV_ATOM_SCHEMA_DRIFT")
        versions: dict[str, int] = {}
        for parsed_entries in parsed.values():
            for entry in parsed_entries:
                versions[entry.arxiv_id] = max(versions.get(entry.arxiv_id, 0), entry.version)
        for ordinal, snapshot_id in enumerate(snapshot_ids):
            if snapshot_id in failures:
                status, code = failures[snapshot_id]
                self._pools.record_snapshot(
                    problem_pool_id=problem_pool_id,
                    snapshot_id=snapshot_id,
                    ordinal=ordinal,
                    ingest_status=status,
                    failure_code=code,
                    entries=(),
                    now=now,
                )
                continue
            source_entries: tuple[SourceEntry, ...] = tuple(
                self._classify(pool, entry, versions[entry.arxiv_id], snapshot_id)
                for entry in parsed[snapshot_id]
            )
            if not source_entries:
                source_entries = (
                    SourceEntry(
                        f"source-no-hit-{snapshot_id}",
                        None,
                        None,
                        "No arXiv entries returned",
                        "No result in the frozen source snapshot.",
                        pool.date_from + "T00:00:00Z",
                        pool.date_from + "T00:00:00Z",
                        pool.subjects,
                        False,
                        "EXCLUDED",
                        "NO_HIT",
                    ),
                )
            self._pools.record_snapshot(
                problem_pool_id=problem_pool_id,
                snapshot_id=snapshot_id,
                ordinal=ordinal,
                ingest_status="INGESTED",
                failure_code=None,
                entries=source_entries,
                now=now,
            )

    def dispatch_batch(
        self,
        *,
        batch_id: str,
        request_id: str,
        problem_pool_id: str,
        candidate_ids: tuple[str, ...],
        contract_template_artifact: ExactArtifactRef,
        per_run_budget: Mapping[str, int],
        labels: tuple[str, ...],
        session: ProductSession,
        expected_deployment_revision: int,
        now: str,
    ) -> ProductReceipt:
        pool = self._pools.get(problem_pool_id)
        if pool.state != "FROZEN":
            raise ArxivBatchError("batch creation requires a frozen problem pool")
        if not candidate_ids or len(set(candidate_ids)) != len(candidate_ids):
            raise ValueError("batch candidate IDs must be non-empty and unique")
        with self._connect() as connection:
            existing_batch = connection.execute(
                "SELECT 1 FROM product_problem_batch_commands WHERE batch_id=?",
                (batch_id,),
            ).fetchone()
        selected = [self._pools.get_candidate(item) for item in candidate_ids]
        if any(
            item.problem_pool_id != problem_pool_id
            or item.audit_status != "HUMAN_INCLUDED"
            or item.recommendation_status != "RECOMMENDED"
            or (item.created_run_id is not None and existing_batch is None)
            for item in selected
        ):
            raise ArxivBatchError("batch selection contains an ineligible candidate")
        budget = _budget(per_run_budget)
        template = self._read_json(contract_template_artifact)
        _contract_template(template)
        payload: JsonObject = MappingProxyType(
            {
                "problem_pool_id": problem_pool_id,
                "problem_candidate_ids": list(candidate_ids),
                "contract_template_artifact": {
                    "artifact_id": contract_template_artifact.artifact_id,
                    "sha256": contract_template_artifact.sha256,
                },
                "per_run_budget": dict(budget),
                "labels": list(labels),
            }
        )
        request = ProductCommand(
            request_id,
            GlobalScope(pool.deployment_id, expected_deployment_revision),
            "BATCH_CREATE_RESEARCH",
            payload,
        )
        receipt = self._commands.command(session, request)
        if receipt.state not in {"PENDING", "DECIDED"}:
            raise ArxivBatchError("formal batch command returned an invalid receipt state")
        immutable_values = (
            problem_pool_id,
            request_id,
            pool.deployment_id,
            _json(list(candidate_ids)),
            contract_template_artifact.artifact_id,
            contract_template_artifact.sha256,
            _json(dict(budget)),
            _json(list(labels)),
            receipt.receipt_id,
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT problem_pool_id,request_id,deployment_id,candidate_ids_json,"
                "contract_template_artifact_id,contract_template_sha256,per_run_budget_json,"
                "labels_json,batch_receipt_id,batch_receipt_state FROM "
                "product_problem_batch_commands WHERE batch_id=?",
                (batch_id,),
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO product_problem_batch_commands("
                    "batch_id,problem_pool_id,request_id,deployment_id,candidate_ids_json,"
                    "contract_template_artifact_id,contract_template_sha256,"
                    "per_run_budget_json,labels_json,batch_receipt_id,batch_receipt_state,state,"
                    "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (batch_id, *immutable_values, receipt.state, "DISPATCHED", now, now),
                )
            elif tuple(row[:9]) != immutable_values:
                raise ArxivBatchError("batch identity is bound differently")
            elif row[9] == "PENDING" and receipt.state == "DECIDED":
                connection.execute(
                    "UPDATE product_problem_batch_commands SET batch_receipt_state=?,"
                    "updated_at=? WHERE batch_id=?",
                    (receipt.state, now, batch_id),
                )
            elif row[9] != receipt.state:
                raise ArxivBatchError("batch receipt state regressed")
            connection.commit()
        return receipt

    def execute_batch(
        self,
        *,
        batch_id: str,
        contract_template_artifact: ExactArtifactRef,
        owner: str,
        labels: tuple[str, ...],
        session: ProductSession,
        expected_deployment_revision: int,
        now: str,
    ) -> BatchExecution:
        template = self._read_json(contract_template_artifact)
        _contract_template(template)
        with self._connect() as connection:
            row = connection.execute(
                "SELECT problem_pool_id,deployment_id,candidate_ids_json,"
                "contract_template_artifact_id,contract_template_sha256,per_run_budget_json,"
                "labels_json,state FROM product_problem_batch_commands WHERE batch_id=?",
                (batch_id,),
            ).fetchone()
        if row is None:
            raise KeyError(batch_id)
        if (
            row[3] != contract_template_artifact.artifact_id
            or row[4] != contract_template_artifact.sha256
            or row[7] not in {"DISPATCHED", "RUNNING", "COMPLETED"}
        ):
            raise ArxivBatchError("batch execution binding differs from dispatch")
        candidate_ids = _strings(row[2])
        budget = _object(row[5])
        stored_labels = _strings(row[6])
        if stored_labels != labels:
            raise ArxivBatchError("batch execution labels differ from dispatch")
        with self._connect() as connection:
            connection.execute(
                "UPDATE product_problem_batch_commands SET state='RUNNING',updated_at=? "
                "WHERE batch_id=? AND state='DISPATCHED'",
                (now, batch_id),
            )
        for candidate_id in candidate_ids:
            candidate = self._pools.get_candidate(candidate_id)
            request_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"rk:batch:{batch_id}:{candidate_id}"))
            contract = dict(template)
            contract["objects"] = list(
                dict.fromkeys(
                    [
                        *cast(list[str], contract["objects"]),
                        *candidate.definitions,
                        candidate.arxiv_id,
                    ]
                )
            )
            contract["quantifiers"] = list(
                dict.fromkeys([*cast(list[str], contract["quantifiers"]), *candidate.quantifiers])
            )
            contract["boundary_conditions"] = list(
                dict.fromkeys(
                    [
                        *cast(list[str], contract["boundary_conditions"]),
                        *candidate.hypotheses,
                    ]
                )
            )
            payload: JsonObject = MappingProxyType(
                {
                    "question": str(candidate.normalized_statement),
                    "contract_draft": cast(JsonValue, contract),
                    "owner": owner,
                    "labels": list(dict.fromkeys([*labels, "arxiv-batch", candidate.arxiv_id])),
                    "initial_budget": cast(JsonValue, budget),
                    "title": f"arXiv {candidate.arxiv_id}v{candidate.version} open problem",
                    "material_artifacts": [],
                }
            )
            receipt = self._commands.command(
                session,
                ProductCommand(
                    request_id,
                    GlobalScope(str(row[1]), expected_deployment_revision),
                    "CREATE_RESEARCH",
                    payload,
                ),
            )
            if (
                receipt.state != "DECIDED"
                or receipt.decision is None
                or not receipt.decision.accepted
                or receipt.decision.created_run_id is None
            ):
                raise ArxivBatchError("formal CREATE_RESEARCH command did not create a run")
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                existing = connection.execute(
                    "SELECT create_request_id,create_receipt_id,created_run_id FROM "
                    "product_problem_batch_runs WHERE batch_id=? AND problem_candidate_id=?",
                    (batch_id, candidate_id),
                ).fetchone()
                values = (request_id, receipt.receipt_id, receipt.decision.created_run_id)
                if existing is None:
                    connection.execute(
                        "INSERT INTO product_problem_batch_runs("
                        "batch_id,problem_candidate_id,create_request_id,create_receipt_id,"
                        "created_run_id,created_at) VALUES(?,?,?,?,?,?)",
                        (batch_id, candidate_id, *values, now),
                    )
                    connection.execute(
                        "UPDATE product_problem_candidates SET created_run_id=?,updated_at=? "
                        "WHERE problem_candidate_id=? AND created_run_id IS NULL",
                        (receipt.decision.created_run_id, now, candidate_id),
                    )
                elif tuple(existing) != values:
                    raise ArxivBatchError("candidate run was created differently")
                connection.commit()
        with self._connect() as connection:
            connection.execute(
                "UPDATE product_problem_batch_commands SET state='COMPLETED',updated_at=? "
                "WHERE batch_id=?",
                (now, batch_id),
            )
        return self.get_batch(batch_id)

    def get_batch(self, batch_id: str) -> BatchExecution:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT state FROM product_problem_batch_commands WHERE batch_id=?",
                (batch_id,),
            ).fetchone()
            runs = connection.execute(
                "SELECT problem_candidate_id,created_run_id FROM product_problem_batch_runs "
                "WHERE batch_id=? ORDER BY problem_candidate_id",
                (batch_id,),
            ).fetchall()
        if row is None:
            raise KeyError(batch_id)
        return BatchExecution(
            batch_id,
            str(row[0]),
            tuple((str(item[0]), str(item[1])) for item in runs),
        )

    def _classify(
        self,
        pool: ProblemPool,
        entry: _ArxivEntry,
        latest_version: int,
        snapshot_id: str,
    ) -> SourceEntry:
        status, reason = "INCLUDED", "EXPLICIT_OPEN_MARKER"
        published_date = entry.published_at[:10]
        if pool.version_rule == "LATEST_VISIBLE" and entry.version < latest_version:
            status, reason = "EXCLUDED", "SUPERSEDED_VERSION"
        elif not pool.date_from <= published_date <= pool.date_to:
            status, reason = "EXCLUDED", "OUTSIDE_DATE_WINDOW"
        elif not set(entry.subjects).intersection(pool.subjects):
            status, reason = "EXCLUDED", "OUTSIDE_SUBJECT_WINDOW"
        elif entry.withdrawn and pool.withdrawal_rule == "EXCLUDE_WITHDRAWN":
            status, reason = "EXCLUDED", "WITHDRAWN"
        elif "EXCLUDE_SURVEY" in pool.exclusion_rules and "survey" in entry.title.casefold():
            status, reason = "EXCLUDED", "EXCLUSION_RULE_SURVEY"
        marker, statement = _extract_marker(entry.title, entry.summary)
        if status == "INCLUDED" and marker is None:
            status, reason = "EXCLUDED", "NO_EXPLICIT_OPEN_MARKER"
        source_id = (
            "arxiv-source-"
            + hashlib.sha256(
                f"{pool.problem_pool_id}:{snapshot_id}:{entry.arxiv_id}:v{entry.version}".encode()
            ).hexdigest()[:32]
        )
        return SourceEntry(
            source_id,
            entry.arxiv_id,
            entry.version,
            entry.title,
            entry.summary,
            entry.published_at,
            entry.updated_at,
            entry.subjects,
            entry.withdrawn,
            status,
            reason,
            marker if status == "INCLUDED" else None,
            statement if status == "INCLUDED" else None,
        )

    def _read_json(self, artifact: ExactArtifactRef) -> dict[str, JsonValue]:
        try:
            value = json.loads(self._read(artifact))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ArxivBatchError("contract template artifact is invalid JSON") from error
        if not isinstance(value, dict):
            raise ArxivBatchError("contract template must be a JSON object")
        return cast(dict[str, JsonValue], value)

    def _read(self, artifact: ExactArtifactRef) -> bytes:
        result = self._artifacts.open_range(artifact.artifact_id, expected_ref=artifact)
        stream = getattr(result, "stream", None)
        if stream is None:
            raise ArxivBatchError("artifact reader exposed no bytes")
        body = b"".join(stream)
        if len(body) != artifact.byte_count or hashlib.sha256(body).hexdigest() != artifact.sha256:
            raise ArxivBatchError("ArtifactRef digest differs from bytes")
        return body

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self._db_path, isolation_level=None)
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms}")
        return connection


@dataclass(frozen=True, slots=True)
class _ArxivEntry:
    arxiv_id: str
    version: int
    title: str
    summary: str
    published_at: str
    updated_at: str
    subjects: tuple[str, ...]
    withdrawn: bool


def _parse_atom(raw: bytes) -> tuple[_ArxivEntry, ...]:
    root = ET.fromstring(raw)
    result = []
    for node in root.findall("atom:entry", _ATOM):
        identifier = _text(node, "atom:id")
        title = _text(node, "atom:title")
        summary = _text(node, "atom:summary")
        published = _text(node, "atom:published")
        updated = _text(node, "atom:updated")
        if None in {identifier, title, summary, published, updated}:
            raise ValueError("arXiv entry fields drifted")
        versioned = str(identifier).rsplit("/", 1)[-1]
        match = _VERSIONED_ID.fullmatch(versioned)
        if match is None:
            raise ValueError("arXiv entry lacks an exact version")
        subjects = tuple(
            item.attrib["term"]
            for item in node.findall("atom:category", _ATOM)
            if item.attrib.get("term")
        )
        if not subjects:
            raise ValueError("arXiv entry has no subject category")
        comment = _text(node, "arxiv:comment") or ""
        combined = f"{title} {summary} {comment}".casefold()
        result.append(
            _ArxivEntry(
                match.group("id"),
                int(match.group("version")),
                " ".join(str(title).split()),
                " ".join(str(summary).split()),
                str(published),
                str(updated),
                subjects,
                "withdrawn" in combined or "withdrawal" in combined,
            )
        )
    return tuple(result)


def _extract_marker(title: str, summary: str) -> tuple[str | None, str | None]:
    text = f"{title}. {summary}"
    match = _SENTENCE.search(text)
    if match is None:
        return None, None
    statement = " ".join(match.group(0).strip().split())
    marker = _MARKER.search(statement)
    if marker is None or len(statement) < 30:
        return None, None
    return marker.group(1).upper(), statement


def _text(node: ET.Element, path: str) -> str | None:
    child = node.find(path, _ATOM)
    return child.text if child is not None else None


def _budget(value: Mapping[str, int]) -> dict[str, int]:
    if set(value) != {"microunits", "wall_seconds"}:
        raise ValueError("budget fields are not exact")
    if any(isinstance(item, bool) or not isinstance(item, int) for item in value.values()):
        raise ValueError("budget values must be integers")
    result = dict(value)
    if result["microunits"] < 0 or result["wall_seconds"] < 1:
        raise ValueError("budget values are invalid")
    return result


def _contract_template(value: Mapping[str, JsonValue]) -> None:
    required = {
        "objects",
        "domain",
        "quantifiers",
        "boundary_conditions",
        "exact_negation",
        "allowed_tools",
        "success_conditions",
    }
    if set(value) != required:
        raise ArxivBatchError("contract template fields are not exact")
    if not isinstance(value["domain"], str) or not value["domain"]:
        raise ArxivBatchError("contract template domain is missing")
    for name in required - {"domain", "exact_negation"}:
        items = value[name]
        if not isinstance(items, list) or any(
            not isinstance(item, str) or not item for item in items
        ):
            raise ArxivBatchError("contract template array field is invalid")
    if not isinstance(value["exact_negation"], str) or not value["exact_negation"]:
        raise ArxivBatchError("contract template exact negation is missing")


def _strings(value: object) -> tuple[str, ...]:
    decoded = json.loads(str(value))
    if not isinstance(decoded, list) or any(not isinstance(item, str) for item in decoded):
        raise ArxivBatchError("stored candidate list is invalid")
    return tuple(decoded)


def _object(value: object) -> dict[str, JsonValue]:
    decoded = json.loads(str(value))
    if not isinstance(decoded, dict):
        raise ArxivBatchError("stored batch JSON is invalid")
    return cast(dict[str, JsonValue], decoded)


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


__all__ = ["ArxivBatchError", "ArxivBatchPipeline", "BatchExecution"]
