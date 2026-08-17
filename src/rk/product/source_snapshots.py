"""Immutable raw-response snapshots for live literature calls and offline replay."""

from __future__ import annotations

import hashlib
import json
import sqlite3
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

from rk.product.artifact_read import ArtifactReadService, ExactArtifactRef
from rk.product.literature_connectors import ConnectorFetch, LiteratureConnector
from rk.product.tool_runs import ToolRunStore
from rk.sqlite import open_sqlite
from rk.wire import canonical_json_bytes


class SourceSnapshotError(RuntimeError):
    """A source snapshot identity, binding, or raw artifact is invalid."""


class SourceSnapshotConflict(SourceSnapshotError):
    """A stable snapshot identity was rebound to another call."""


class RawResponsePublisher(Protocol):
    def publish(
        self, *, data: bytes, logical_name: str, media_type: str
    ) -> ExactArtifactRef: ...


@dataclass(frozen=True, slots=True)
class SourceSnapshot:
    snapshot_id: str
    tool_run_id: str
    attempt_id: str
    connector: str
    connector_version: str
    mode: str
    parent_snapshot_id: str | None
    endpoint: str
    queried_at: str
    request: dict[str, object]
    request_digest: str
    http_status: int | None
    raw_kind: str
    raw_response: ExactArtifactRef
    source_visible_version: str | None
    coverage: dict[str, object]
    normalized: dict[str, object]
    result_status: str
    error_code: str | None
    error_detail: str | None
    created_at: str

    @property
    def establishes_novelty(self) -> bool:
        return False


class SourceSnapshotStore:
    """Capture every connector outcome; replay never contacts an external endpoint."""

    def __init__(
        self,
        *,
        db_path: Path,
        artifacts: ArtifactReadService,
        publisher: RawResponsePublisher,
        tool_runs: ToolRunStore,
        busy_timeout_ms: int = 5_000,
    ) -> None:
        self._db_path = Path(db_path)
        self._artifacts = artifacts
        self._publisher = publisher
        self._tool_runs = tool_runs
        self._busy_timeout_ms = busy_timeout_ms

    def capture_live(
        self,
        *,
        snapshot_id: str,
        tool_run_id: str,
        attempt_id: str,
        connector: LiteratureConnector,
        request: dict[str, object],
        queried_at: str,
        timeout_seconds: float,
    ) -> SourceSnapshot:
        self._require_attempt(tool_run_id, attempt_id)
        if timeout_seconds <= 0:
            raise ValueError("connector timeout must be positive")
        fetch = connector.query(request, timeout_seconds=timeout_seconds)
        if fetch.connector != connector.name or fetch.connector_version != connector.version:
            raise SourceSnapshotError("connector response identity drifted")
        _reject_verdict_fields(fetch.normalized)
        suffix = "transport.json" if fetch.raw_kind == "TRANSPORT_RECEIPT" else "response"
        raw_ref = self._publisher.publish(
            data=fetch.raw_body,
            logical_name=f"{fetch.connector.lower()}-{suffix}",
            media_type=fetch.response_media_type,
        )
        _require_raw_binding(raw_ref, fetch.raw_body, fetch.response_media_type)
        self._insert(
            snapshot_id=snapshot_id,
            tool_run_id=tool_run_id,
            attempt_id=attempt_id,
            mode="LIVE_QUERY",
            parent_snapshot_id=None,
            fetch=fetch,
            raw_ref=raw_ref,
            queried_at=queried_at,
            created_at=queried_at,
        )
        return self.get(snapshot_id)

    def replay(
        self,
        *,
        source_snapshot_id: str,
        snapshot_id: str,
        tool_run_id: str,
        attempt_id: str,
        replayed_at: str,
    ) -> SourceSnapshot:
        self._require_attempt(tool_run_id, attempt_id)
        source = self.get(source_snapshot_id)
        raw = b"".join(
            self._artifacts.open_range(
                source.raw_response.artifact_id,
                expected_ref=source.raw_response,
            ).stream
        )
        _require_raw_binding(
            source.raw_response, raw, source.raw_response.media_type
        )
        fetch = ConnectorFetch(
            connector=source.connector,
            connector_version=source.connector_version,
            endpoint=source.endpoint,
            request=source.request,
            http_status=source.http_status,
            response_media_type=source.raw_response.media_type,
            raw_body=raw,
            raw_kind=source.raw_kind,
            source_visible_version=source.source_visible_version,
            coverage=source.coverage,
            normalized=source.normalized,
            status=source.result_status,  # type: ignore[arg-type]
            error_code=source.error_code,
            error_detail=source.error_detail,
        )
        self._insert(
            snapshot_id=snapshot_id,
            tool_run_id=tool_run_id,
            attempt_id=attempt_id,
            mode="REPLAYED_SNAPSHOT",
            parent_snapshot_id=source_snapshot_id,
            fetch=fetch,
            raw_ref=source.raw_response,
            queried_at=source.queried_at,
            created_at=replayed_at,
        )
        return self.get(snapshot_id)

    def get(self, snapshot_id: str) -> SourceSnapshot:
        with self._connect() as connection:
            row = connection.execute(
                _SELECT + " WHERE snapshot_id=?", (snapshot_id,)
            ).fetchone()
        if row is None:
            raise KeyError(snapshot_id)
        return _snapshot(row)

    def _require_attempt(self, tool_run_id: str, attempt_id: str) -> None:
        run = self._tool_runs.get(tool_run_id)
        if run.current_attempt_id != attempt_id:
            raise SourceSnapshotError("snapshot must bind the current ToolRun attempt")
        if not any(
            attempt.attempt_id == attempt_id
            for attempt in self._tool_runs.attempts(tool_run_id)
        ):
            raise SourceSnapshotError("snapshot attempt is absent")

    def _insert(
        self,
        *,
        snapshot_id: str,
        tool_run_id: str,
        attempt_id: str,
        mode: str,
        parent_snapshot_id: str | None,
        fetch: ConnectorFetch,
        raw_ref: ExactArtifactRef,
        queried_at: str,
        created_at: str,
    ) -> None:
        request_json = _json(fetch.request)
        request_digest = hashlib.sha256(canonical_json_bytes(fetch.request)).hexdigest()
        values = (
            tool_run_id,
            attempt_id,
            fetch.connector,
            fetch.connector_version,
            mode,
            parent_snapshot_id,
            fetch.endpoint,
            queried_at,
            request_json,
            request_digest,
            fetch.http_status,
            fetch.raw_kind,
            raw_ref.artifact_id,
            raw_ref.sha256,
            raw_ref.byte_count,
            raw_ref.media_type,
            fetch.source_visible_version,
            _json(fetch.coverage),
            _json(fetch.normalized),
            str(fetch.status),
            fetch.error_code,
            fetch.error_detail,
            created_at,
        )
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT tool_run_id,attempt_id,connector,connector_version,mode,"
                "parent_snapshot_id,endpoint,queried_at,request_json,request_digest,http_status,"
                "raw_kind,raw_artifact_id,raw_artifact_sha256,raw_artifact_byte_count,"
                "raw_artifact_media_type,source_visible_version,coverage_json,normalized_json,"
                "result_status,error_code,error_detail,created_at "
                "FROM product_source_snapshots WHERE snapshot_id=?",
                (snapshot_id,),
            ).fetchone()
            if row is None:
                connection.execute(
                    "INSERT INTO product_source_snapshots("
                    "snapshot_id,tool_run_id,attempt_id,connector,connector_version,mode,"
                    "parent_snapshot_id,endpoint,queried_at,request_json,request_digest,"
                    "http_status,raw_kind,raw_artifact_id,raw_artifact_sha256,"
                    "raw_artifact_byte_count,raw_artifact_media_type,source_visible_version,"
                    "coverage_json,normalized_json,result_status,error_code,error_detail,"
                    "created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (snapshot_id, *values),
                )
            elif tuple(row) != values:
                raise SourceSnapshotConflict("snapshot ID is bound to another response")
            connection.commit()

    def _connect(self) -> sqlite3.Connection:
        connection = open_sqlite(self._db_path, isolation_level=None)
        connection.execute(f"PRAGMA busy_timeout={self._busy_timeout_ms}")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection


_SELECT = (
    "SELECT snapshot_id,tool_run_id,attempt_id,connector,connector_version,mode,"
    "parent_snapshot_id,endpoint,queried_at,request_json,request_digest,http_status,raw_kind,"
    "raw_artifact_id,raw_artifact_sha256,raw_artifact_byte_count,raw_artifact_media_type,"
    "source_visible_version,coverage_json,normalized_json,result_status,error_code,error_detail,"
    "created_at FROM product_source_snapshots"
)


def _snapshot(row: tuple[Any, ...]) -> SourceSnapshot:
    return SourceSnapshot(
        snapshot_id=str(row[0]),
        tool_run_id=str(row[1]),
        attempt_id=str(row[2]),
        connector=str(row[3]),
        connector_version=str(row[4]),
        mode=str(row[5]),
        parent_snapshot_id=str(row[6]) if row[6] is not None else None,
        endpoint=str(row[7]),
        queried_at=str(row[8]),
        request=_object(row[9]),
        request_digest=str(row[10]),
        http_status=int(row[11]) if row[11] is not None else None,
        raw_kind=str(row[12]),
        raw_response=ExactArtifactRef(
            str(row[13]), str(row[14]), int(row[15]), str(row[16])
        ),
        source_visible_version=str(row[17]) if row[17] is not None else None,
        coverage=_object(row[18]),
        normalized=_object(row[19]),
        result_status=str(row[20]),
        error_code=str(row[21]) if row[21] is not None else None,
        error_detail=str(row[22]) if row[22] is not None else None,
        created_at=str(row[23]),
    )


def _object(value: object) -> dict[str, object]:
    decoded = json.loads(str(value))
    if not isinstance(decoded, dict):
        raise SourceSnapshotError("persisted snapshot JSON is invalid")
    return decoded


def _require_raw_binding(ref: ExactArtifactRef, raw: bytes, media_type: str) -> None:
    if (
        ref.sha256 != hashlib.sha256(raw).hexdigest()
        or ref.byte_count != len(raw)
        or ref.media_type != media_type
    ):
        raise SourceSnapshotError("raw response ArtifactRef does not match response bytes")


def _reject_verdict_fields(value: object) -> None:
    if isinstance(value, dict):
        forbidden = {"novel", "novelty", "mathematical_fact", "valid", "true"}
        if forbidden.intersection(key.casefold() for key in value):
            raise SourceSnapshotError("connector output attempted to contain a verdict")
        for item in value.values():
            _reject_verdict_fields(item)
    elif isinstance(value, list):
        for item in value:
            _reject_verdict_fields(item)


def _json(value: object) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


__all__ = [
    "RawResponsePublisher",
    "SourceSnapshot",
    "SourceSnapshotConflict",
    "SourceSnapshotError",
    "SourceSnapshotStore",
]
