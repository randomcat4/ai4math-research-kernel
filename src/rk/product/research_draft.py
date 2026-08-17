"""Deterministic normalization of public research drafts into atomic Claim candidates."""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from rk.product.artifact_read import ExactArtifactRef
from rk.product.claims import ClaimArtifactBinding, ClaimKind
from rk.sqlite import open_sqlite
from rk.wire import canonical_json_bytes


class ResearchDraftError(RuntimeError):
    """The draft bytes, grammar, scope, or candidate graph is invalid."""


class ResearchDraftConflict(ResearchDraftError):
    """A stable draft or candidate identity was rebound."""


class ArtifactReader(Protocol):
    def open_range(
        self, artifact_id: str, *, expected_ref: ExactArtifactRef | None = None
    ) -> object: ...


@dataclass(frozen=True, slots=True)
class DraftCandidate:
    candidate_id: str
    draft_id: str
    ordinal: int
    stable_label: str
    statement: str
    statement_digest: str
    claim_kind: ClaimKind
    predecessor_labels: tuple[str, ...]
    predecessor_fact_ids: tuple[str, ...]
    declared_symbols: tuple[str, ...]
    undefined_symbols: tuple[str, ...]
    proof_text: str
    proof_digest: str
    lifecycle: str
    submitted_claim_id: str | None


@dataclass(frozen=True, slots=True)
class NormalizedDraft:
    draft_id: str
    run_id: str
    contract_version: int
    kernel_revision: int
    source_artifact: ClaimArtifactBinding
    normalized_digest: str
    defined_symbols: tuple[str, ...]
    candidates: tuple[DraftCandidate, ...]
    created_at: str


@dataclass(frozen=True, slots=True)
class _ParsedClaim:
    stable_label: str
    statement: str
    claim_kind: ClaimKind
    predecessor_labels: tuple[str, ...]
    predecessor_fact_ids: tuple[str, ...]
    symbols: tuple[str, ...]
    proof: str


_LABEL = re.compile(r"^[a-z][a-z0-9_]{2,63}$")
_KEYS = (
    "Label",
    "Type",
    "Statement",
    "Predecessors",
    "Fact-Predecessors",
    "Symbols",
    "Proof",
)


class ResearchDraftStore:
    """Reads immutable CAS bytes and persists a lossless, author-declared Claim split."""

    def __init__(
        self,
        *,
        db_path: Path,
        artifacts: ArtifactReader,
        busy_timeout_ms: int = 5_000,
    ) -> None:
        self._db_path = Path(db_path)
        self._artifacts = artifacts
        self._busy_timeout_ms = busy_timeout_ms

    def normalize(
        self,
        *,
        draft_id: str,
        run_id: str,
        contract_version: int,
        kernel_revision: int,
        source_artifact: ExactArtifactRef,
        now: str,
    ) -> NormalizedDraft:
        if not draft_id or not run_id or contract_version < 1 or kernel_revision < 0:
            raise ValueError("draft identity and scope are required")
        if source_artifact.media_type not in {"text/markdown", "text/plain"}:
            raise ResearchDraftError("research draft must be a public textual artifact")
        result = self._artifacts.open_range(
            source_artifact.artifact_id, expected_ref=source_artifact
        )
        stream = getattr(result, "stream", None)
        if stream is None:
            raise ResearchDraftError("artifact reader did not expose public bytes")
        body = b"".join(stream)
        if len(body) != source_artifact.byte_count:
            raise ResearchDraftError("draft byte count changed while reading")
        if hashlib.sha256(body).hexdigest() != source_artifact.sha256:
            raise ResearchDraftError("draft bytes do not match the ArtifactRef digest")
        try:
            text = body.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ResearchDraftError("research draft is not UTF-8") from error
        defined, parsed = _parse(text)
        normalized = {
            "defined_symbols": list(defined),
            "claims": [
                {
                    "label": item.stable_label,
                    "statement": item.statement,
                    "claim_kind": item.claim_kind.value,
                    "predecessor_labels": list(item.predecessor_labels),
                    "predecessor_fact_ids": list(item.predecessor_fact_ids),
                    "symbols": list(item.symbols),
                    "proof": item.proof,
                }
                for item in parsed
            ],
        }
        normalized_digest = hashlib.sha256(canonical_json_bytes(normalized)).hexdigest()
        binding = ClaimArtifactBinding(
            source_artifact.artifact_id,
            source_artifact.sha256,
            source_artifact.byte_count,
            source_artifact.media_type,
        )
        draft_values = (
            run_id,
            contract_version,
            kernel_revision,
            _json(binding.to_dict()),
            source_artifact.sha256,
            normalized_digest,
            _json(list(defined)),
            len(parsed),
            now,
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT run_id,contract_version,kernel_revision,source_artifact_json,"
                "source_sha256,normalized_digest,defined_symbols_json,candidate_count,created_at "
                "FROM product_research_drafts WHERE draft_id=?",
                (draft_id,),
            ).fetchone()
            if existing is None:
                connection.execute(
                    "INSERT INTO product_research_drafts("
                    "draft_id,run_id,contract_version,kernel_revision,source_artifact_json,"
                    "source_sha256,normalized_digest,defined_symbols_json,candidate_count,"
                    "created_at) "
                    "VALUES(?,?,?,?,?,?,?,?,?,?)",
                    (draft_id, *draft_values),
                )
            elif tuple(existing[:-1]) != draft_values[:-1]:
                raise ResearchDraftConflict("draft ID is bound to different immutable bytes")
            for ordinal, item in enumerate(parsed):
                candidate_id = _candidate_id(source_artifact.sha256, item.stable_label)
                statement_digest = hashlib.sha256(item.statement.encode()).hexdigest()
                proof_digest = hashlib.sha256(item.proof.encode()).hexdigest()
                undefined = tuple(symbol for symbol in item.symbols if symbol not in defined)
                values = (
                    draft_id,
                    ordinal,
                    item.stable_label,
                    item.statement,
                    statement_digest,
                    item.claim_kind.value,
                    _json(list(item.predecessor_labels)),
                    _json(list(item.predecessor_fact_ids)),
                    _json(list(item.symbols)),
                    _json(list(undefined)),
                    item.proof,
                    proof_digest,
                    now,
                )
                row = connection.execute(
                    "SELECT draft_id,ordinal,stable_label,statement,statement_digest,claim_kind,"
                    "predecessor_labels_json,predecessor_fact_ids_json,declared_symbols_json,"
                    "undefined_symbols_json,proof_text,proof_digest,created_at "
                    "FROM product_research_claim_candidates WHERE candidate_id=?",
                    (candidate_id,),
                ).fetchone()
                if row is None:
                    connection.execute(
                        "INSERT INTO product_research_claim_candidates("
                        "candidate_id,draft_id,ordinal,stable_label,statement,statement_digest,"
                        "claim_kind,predecessor_labels_json,predecessor_fact_ids_json,"
                        "declared_symbols_json,undefined_symbols_json,proof_text,proof_digest,"
                        "lifecycle,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,'CANDIDATE',?)",
                        (candidate_id, *values),
                    )
                elif tuple(row[:-1]) != values[:-1]:
                    raise ResearchDraftConflict("candidate identity is bound differently")
            connection.commit()
        return self.get(draft_id)

    def get(self, draft_id: str) -> NormalizedDraft:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT draft_id,run_id,contract_version,kernel_revision,source_artifact_json,"
                "normalized_digest,defined_symbols_json,created_at FROM "
                "product_research_drafts WHERE draft_id=?",
                (draft_id,),
            ).fetchone()
            if row is None:
                raise KeyError(draft_id)
            candidates = connection.execute(
                "SELECT candidate_id,draft_id,ordinal,stable_label,statement,statement_digest,"
                "claim_kind,predecessor_labels_json,predecessor_fact_ids_json,"
                "declared_symbols_json,undefined_symbols_json,proof_text,proof_digest,lifecycle,"
                "submitted_claim_id FROM product_research_claim_candidates WHERE draft_id=? "
                "ORDER BY ordinal",
                (draft_id,),
            ).fetchall()
        artifact = _artifact(row[4])
        return NormalizedDraft(
            str(row[0]),
            str(row[1]),
            int(row[2]),
            int(row[3]),
            artifact,
            str(row[5]),
            _strings(row[6]),
            tuple(_candidate(item) for item in candidates),
            str(row[7]),
        )

    def get_candidate(self, candidate_id: str) -> DraftCandidate:
        with self._connect() as connection:
            row = connection.execute(
                "SELECT candidate_id,draft_id,ordinal,stable_label,statement,statement_digest,"
                "claim_kind,predecessor_labels_json,predecessor_fact_ids_json,"
                "declared_symbols_json,undefined_symbols_json,proof_text,proof_digest,lifecycle,"
                "submitted_claim_id FROM product_research_claim_candidates WHERE candidate_id=?",
                (candidate_id,),
            ).fetchone()
        if row is None:
            raise KeyError(candidate_id)
        return _candidate(row)

    def source_binding(self, draft_id: str) -> ClaimArtifactBinding:
        return self.get(draft_id).source_artifact

    def _connect(self) -> sqlite3.Connection:
        connection = open_sqlite(self._db_path, isolation_level=None)
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms}")
        return connection


def _parse(text: str) -> tuple[tuple[str, ...], tuple[_ParsedClaim, ...]]:
    lines = [line.rstrip() for line in text.replace("\r\n", "\n").split("\n")]
    meaningful = [index for index, line in enumerate(lines) if line.strip()]
    if not meaningful or not lines[meaningful[0]].startswith("Defined-Symbols:"):
        raise ResearchDraftError("draft must declare Defined-Symbols before Claim blocks")
    defined = _csv(lines[meaningful[0]].split(":", 1)[1], "defined symbols")
    if not defined:
        raise ResearchDraftError("defined symbol registry cannot be empty")
    claims: list[_ParsedClaim] = []
    cursor = meaningful[0] + 1
    while cursor < len(lines):
        if not lines[cursor].strip():
            cursor += 1
            continue
        if lines[cursor].strip() != ":::claim":
            raise ResearchDraftError("text outside an explicit Claim block is not allowed")
        cursor += 1
        fields: dict[str, str] = {}
        while cursor < len(lines) and lines[cursor].strip() != ":::end":
            line = lines[cursor]
            if not line.strip():
                cursor += 1
                continue
            if ":" not in line:
                raise ResearchDraftError("Claim fields must use Key: value syntax")
            key, value = line.split(":", 1)
            if key not in _KEYS or key in fields:
                raise ResearchDraftError("Claim fields are duplicated or unsupported")
            fields[key] = value.strip()
            cursor += 1
        if cursor >= len(lines):
            raise ResearchDraftError("Claim block is not closed")
        if set(fields) != set(_KEYS) or any(not fields[key] for key in _KEYS):
            raise ResearchDraftError("Claim block fields are not exact and complete")
        label = fields["Label"]
        if not _LABEL.fullmatch(label):
            raise ResearchDraftError("Claim stable label is invalid")
        try:
            kind = ClaimKind(fields["Type"])
        except ValueError as error:
            raise ResearchDraftError("Claim Type is unsupported") from error
        predecessors = _csv_or_empty(fields["Predecessors"])
        fact_ids = _csv_or_empty(fields["Fact-Predecessors"])
        symbols = _csv(fields["Symbols"], "symbols")
        if len(set(predecessors)) != len(predecessors) or len(set(fact_ids)) != len(fact_ids):
            raise ResearchDraftError("Claim predecessors must be unique")
        earlier = {item.stable_label for item in claims}
        if any(label_value not in earlier for label_value in predecessors):
            raise ResearchDraftError("Claim predecessor labels must name earlier blocks")
        statement = fields["Statement"].strip()
        proof = fields["Proof"].strip()
        if len(statement) < 40 or len(proof) < 80:
            raise ResearchDraftError("Claim statement or derivation is not research-grade")
        claims.append(_ParsedClaim(label, statement, kind, predecessors, fact_ids, symbols, proof))
        cursor += 1
    if len(claims) < 2:
        raise ResearchDraftError("research draft must contain multiple atomic Claims")
    labels = [item.stable_label for item in claims]
    if len(set(labels)) != len(labels):
        raise ResearchDraftError("Claim stable labels must be unique")
    return defined, tuple(claims)


def _csv(value: str, label: str) -> tuple[str, ...]:
    result = tuple(item.strip() for item in value.split(",") if item.strip())
    if not result or len(set(result)) != len(result):
        raise ResearchDraftError(f"{label} must be a non-empty unique CSV list")
    return result


def _csv_or_empty(value: str) -> tuple[str, ...]:
    return () if value == "-" else _csv(value, "predecessors")


def _candidate_id(source_sha256: str, label: str) -> str:
    return "draft-claim-" + hashlib.sha256(f"{source_sha256}:{label}".encode()).hexdigest()[:32]


def _artifact(value: object) -> ClaimArtifactBinding:
    decoded = json.loads(str(value))
    if not isinstance(decoded, dict) or set(decoded) != {
        "artifact_id",
        "sha256",
        "byte_count",
        "media_type",
    }:
        raise ResearchDraftError("stored source ArtifactRef is invalid")
    return ClaimArtifactBinding(
        str(decoded["artifact_id"]),
        str(decoded["sha256"]),
        int(decoded["byte_count"]),
        str(decoded["media_type"]),
    )


def _candidate(row: tuple[object, ...]) -> DraftCandidate:
    return DraftCandidate(
        str(row[0]),
        str(row[1]),
        int(str(row[2])),
        str(row[3]),
        str(row[4]),
        str(row[5]),
        ClaimKind(str(row[6])),
        _strings(row[7]),
        _strings(row[8]),
        _strings(row[9]),
        _strings(row[10]),
        str(row[11]),
        str(row[12]),
        str(row[13]),
        str(row[14]) if row[14] is not None else None,
    )


def _strings(value: object) -> tuple[str, ...]:
    decoded = json.loads(str(value))
    if not isinstance(decoded, list) or any(not isinstance(item, str) for item in decoded):
        raise ResearchDraftError("stored string array is invalid")
    return tuple(decoded)


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


__all__ = [
    "DraftCandidate",
    "NormalizedDraft",
    "ResearchDraftConflict",
    "ResearchDraftError",
    "ResearchDraftStore",
]
