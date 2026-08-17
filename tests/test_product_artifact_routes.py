from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from rk.cas import ContentAddressedStore
from rk.http_shell import (
    HttpErrorClass,
    HttpRequest,
    HttpResponse,
    HttpResult,
    HttpStreamResponse,
    ProductHttpError,
    RouteRegistry,
    SessionPrincipal,
    SessionRequest,
)
from rk.migrations import MigrationRunner
from rk.product.artifact_read import ArtifactDescriptor, ArtifactReadService
from rk.product.artifact_routes import ArtifactRouter, artifact_router_factory
from rk.product.artifact_upload import SQLiteArtifactRegistry
from rk.product.log_tail import PublicLog, PublicLogStore
from rk.product_migrations import ProductMigrationAssembler, ProductMigrationRegistry
from rk.storage import SQLiteStorage

NOW = datetime(2026, 8, 13, 12, tzinfo=UTC)


class Ids:
    def __init__(self, prefix: str) -> None:
        self.prefix = prefix
        self.value = 0

    def __call__(self) -> str:
        self.value += 1
        return f"{self.prefix}-{self.value}"

    def new(self) -> str:
        return self()


class Authorizer:
    def __init__(self, *, deny: bool = False) -> None:
        self.deny = deny
        self.artifacts: list[tuple[str, str]] = []
        self.logs: list[tuple[str, str]] = []

    def authorize_artifact(
        self, principal: SessionPrincipal, descriptor: ArtifactDescriptor
    ) -> None:
        if self.deny:
            raise ProductHttpError(
                "ARTIFACT_FORBIDDEN", HttpErrorClass.AUTHORIZATION, "$.artifact_id"
            )
        self.artifacts.append((principal.subject_id, descriptor.ref.artifact_id))

    def authorize_log(self, principal: SessionPrincipal, log: PublicLog) -> None:
        if self.deny:
            raise ProductHttpError(
                "LOG_FORBIDDEN", HttpErrorClass.AUTHORIZATION, "$.operation.payload.log_id"
            )
        self.logs.append((principal.subject_id, log.log_id))


class Services:
    def __init__(self, tmp_path: Path) -> None:
        self.db = tmp_path / "rk.sqlite"
        MigrationRunner(self.db, Path("migrations"), 5_000, minimum_sqlite=(3, 0, 0)).migrate()
        with sqlite3.connect(self.db) as connection:
            ProductMigrationAssembler(ProductMigrationRegistry(Path("schema_fragments"))).apply(
                connection
            )
        self.spool = tmp_path / "spool"
        self.spool.mkdir()
        self.cas_root = tmp_path / "cas"
        self.storage = SQLiteStorage(self.db, 5_000)
        self.registry = SQLiteArtifactRegistry(self.storage)
        self.cas = ContentAddressedStore(
            self.cas_root,
            max_bytes=20 * 1024 * 1024,
            inbox_roots=(self.spool,),
            orphan_grace_seconds=60,
            id_generator=Ids("artifact"),
        )
        self.read = ArtifactReadService(
            metadata=self.storage,
            cas_root=self.cas_root,
            stream_chunk_bytes=1024,
        )
        self.log_ids = Ids("log")
        self.logs = self.new_log_store()

    def new_log_store(self) -> PublicLogStore:
        return PublicLogStore(
            db_path=self.db,
            cas=self.cas,
            registry=self.registry,
            spool_root=self.spool,
            id_generator=self.log_ids,
            clock=lambda: NOW,
            max_chunk_bytes=1024 * 1024,
            max_tail_bytes=64 * 1024,
        )

    def artifact(self, data: bytes, *, name: str, media_type: str) -> str:
        committed = self.cas.commit(
            self.cas.stage_bytes(data, media_type=media_type, source_name=name), now=NOW
        )
        return self.registry.register(committed).artifact_id


def principal() -> SessionPrincipal:
    return SessionPrincipal("session-1", "subject-1", ("capability-1",))


def invoke(router: ArtifactRouter, name: str, request: HttpRequest) -> HttpResult:
    route = next(item for item in router.routes() if item.name == name)

    async def call() -> HttpResult:
        return await route.handler(SessionRequest(request, principal()))

    return asyncio.run(call())


def tail_body(log_id: str, cursor: int, limit: int | None = None) -> bytes:
    payload: dict[str, object] = {"log_id": log_id, "cursor": cursor}
    if limit is not None:
        payload["limit"] = limit
    return json.dumps(
        {
            "schema_version": "rk.product.artifact.v1",
            "request_id": "request-1",
            "operation": {"type": "TAIL_LOG", "payload": payload},
        }
    ).encode()


def test_range_route_is_206_raw_segmented_bytes_with_exact_download_headers(
    tmp_path: Path,
) -> None:
    services = Services(tmp_path)
    data = bytes(range(256)) * 40
    artifact_id = services.artifact(data, name="结果.bin", media_type="application/octet-stream")
    authorizer = Authorizer()
    router = artifact_router_factory(
        artifacts=services.read, logs=services.logs, authorizer=authorizer
    )

    response = invoke(
        router,
        "artifact-range-read",
        HttpRequest("GET", f"/v1/artifacts/{artifact_id}", headers={"Range": "bytes=3-4098"}),
    )

    assert isinstance(response, HttpStreamResponse)
    chunks = list(response.body)
    assert response.status == 206
    assert b"".join(chunks) == data[3:4099]
    assert len(chunks) == 4
    assert max(map(len, chunks)) == 1024
    assert response.headers["content-range"] == f"bytes 3-4098/{len(data)}"
    assert response.headers["content-length"] == "4096"
    assert response.headers["content-type"] == "application/octet-stream"
    assert "filename*=UTF-8''%E7%BB%93%E6%9E%9C.bin" in response.headers["content-disposition"]
    assert response.headers["x-rk-artifact-id"] == artifact_id
    assert authorizer.artifacts == [("subject-1", artifact_id)]


def test_download_without_range_is_200_and_still_streamed(tmp_path: Path) -> None:
    services = Services(tmp_path)
    artifact_id = services.artifact(b"whole body", name="note.txt", media_type="text/plain")
    router = artifact_router_factory(
        artifacts=services.read, logs=services.logs, authorizer=Authorizer()
    )

    response = invoke(
        router,
        "artifact-range-read",
        HttpRequest("GET", f"/v1/artifacts/{artifact_id}"),
    )

    assert isinstance(response, HttpStreamResponse)
    assert response.status == 200
    assert b"".join(response.body) == b"whole body"
    assert "content-range" not in response.headers


def test_unsatisfiable_range_is_416_with_rfc_content_range(tmp_path: Path) -> None:
    services = Services(tmp_path)
    artifact_id = services.artifact(
        b"short", name="short.bin", media_type="application/octet-stream"
    )
    router = artifact_router_factory(
        artifacts=services.read, logs=services.logs, authorizer=Authorizer()
    )

    response = invoke(
        router,
        "artifact-range-read",
        HttpRequest("GET", f"/v1/artifacts/{artifact_id}", headers={"range": "bytes=99-100"}),
    )

    assert isinstance(response, HttpResponse)
    assert response.status == 416
    assert response.headers["content-range"] == "bytes */5"
    assert response.headers["accept-ranges"] == "bytes"
    assert response.body["code"] == "RANGE_NOT_SATISFIABLE"


def test_missing_and_unauthorized_artifacts_do_not_open_a_stream(tmp_path: Path) -> None:
    services = Services(tmp_path)
    missing = artifact_router_factory(
        artifacts=services.read, logs=services.logs, authorizer=Authorizer()
    )
    not_found = invoke(
        missing,
        "artifact-range-read",
        HttpRequest("GET", "/v1/artifacts/absent"),
    )
    assert isinstance(not_found, HttpResponse)
    assert not_found.status == 404

    artifact_id = services.artifact(b"secret", name="bound.tex", media_type="application/x-tex")
    denied = artifact_router_factory(
        artifacts=services.read, logs=services.logs, authorizer=Authorizer(deny=True)
    )
    forbidden = invoke(
        denied,
        "artifact-range-read",
        HttpRequest("GET", f"/v1/artifacts/{artifact_id}"),
    )
    assert isinstance(forbidden, HttpResponse)
    assert forbidden.status == 403
    assert forbidden.body["code"] == "ARTIFACT_FORBIDDEN"


def test_tail_log_operation_streams_exact_byte_cursor_after_restart(tmp_path: Path) -> None:
    services = Services(tmp_path)
    log = services.logs.create(
        scope_kind="RUN",
        scope_id="run-1",
        producer_run_id="worker-1",
        stream="STDOUT",
        logical_name="worker.stdout.log",
    )
    first = b"alpha\n"
    services.logs.append(
        log.log_id,
        offset=0,
        data=first,
        transfer_sha256=hashlib.sha256(first).hexdigest(),
    )
    restarted = services.new_log_store()
    second = b"beta\n"
    restarted.append(
        log.log_id,
        offset=len(first),
        data=second,
        transfer_sha256=hashlib.sha256(second).hexdigest(),
    )
    authorizer = Authorizer()
    router = artifact_router_factory(artifacts=services.read, logs=restarted, authorizer=authorizer)

    response = invoke(
        router,
        "artifact-operation",
        HttpRequest("POST", "/v1/artifacts/operations", body=tail_body(log.log_id, 2, 7)),
    )

    assert isinstance(response, HttpStreamResponse)
    assert response.status == 200
    assert b"".join(response.body) == b"pha\nbet"
    assert response.headers["content-length"] == "7"
    assert response.headers["x-rk-log-cursor"] == "2"
    assert response.headers["x-rk-log-next-cursor"] == "9"
    assert response.headers["x-rk-log-durable-byte-count"] == "11"
    assert response.headers["x-rk-log-caught-up"] == "false"
    assert response.headers["x-rk-log-end-of-log"] == "false"
    assert authorizer.logs == [("subject-1", log.log_id)]


def test_sealed_log_tail_identifies_final_artifact_and_end(tmp_path: Path) -> None:
    services = Services(tmp_path)
    log = services.logs.create(
        scope_kind="RUN",
        scope_id="run-1",
        producer_run_id="worker-1",
        stream="STDERR",
        logical_name="worker.stderr.log",
    )
    data = b"public error\n"
    services.logs.append(
        log.log_id,
        offset=0,
        data=data,
        transfer_sha256=hashlib.sha256(data).hexdigest(),
    )
    ref = services.logs.seal(log.log_id)
    router = artifact_router_factory(
        artifacts=services.read, logs=services.logs, authorizer=Authorizer()
    )

    response = invoke(
        router,
        "artifact-operation",
        HttpRequest("POST", "/v1/artifacts/operations", body=tail_body(log.log_id, 0)),
    )

    assert isinstance(response, HttpStreamResponse)
    assert b"".join(response.body) == data
    assert response.headers["x-rk-log-end-of-log"] == "true"
    assert response.headers["x-rk-artifact-id"] == ref.artifact_id


def test_tail_payload_cannot_inject_raw_completion_or_identity(tmp_path: Path) -> None:
    services = Services(tmp_path)
    router = artifact_router_factory(
        artifacts=services.read, logs=services.logs, authorizer=Authorizer()
    )
    body = json.dumps(
        {
            "schema_version": "rk.product.artifact.v1",
            "request_id": "request-1",
            "operation": {
                "type": "TAIL_LOG",
                "payload": {
                    "log_id": "raw-model-completion",
                    "cursor": 0,
                    "raw_completion": "hidden",
                    "principal_subject_id": "forged",
                },
            },
        }
    ).encode()

    response = invoke(
        router,
        "artifact-operation",
        HttpRequest("POST", "/v1/artifacts/operations", body=body),
    )

    assert isinstance(response, HttpResponse)
    assert response.status == 400
    assert response.body["code"] == "LOG_TAIL_INVALID"
    assert b"hidden" not in json.dumps(dict(response.body)).encode()


def test_non_tail_artifact_operation_delegates_without_changing_json_response(
    tmp_path: Path,
) -> None:
    services = Services(tmp_path)

    async def upload(_request: SessionRequest) -> HttpResult:
        return HttpResponse(
            200,
            {
                "schema_version": "rk.product.artifact_result.v1",
                "result_type": "UPLOAD_OPEN",
            },
        )

    router = artifact_router_factory(
        artifacts=services.read,
        logs=services.logs,
        authorizer=Authorizer(),
        other_operations=upload,
    )
    body = json.dumps(
        {
            "schema_version": "rk.product.artifact.v1",
            "request_id": "request-1",
            "operation": {"type": "BEGIN_UPLOAD", "payload": {}},
        }
    ).encode()

    response = invoke(
        router,
        "artifact-operation",
        HttpRequest("POST", "/v1/artifacts/operations", body=body),
    )

    assert isinstance(response, HttpResponse)
    assert response.status == 200
    assert response.body["result_type"] == "UPLOAD_OPEN"
    assert response.headers["content-type"] == "application/json"


def test_router_factory_contributes_the_two_contract_routes(tmp_path: Path) -> None:
    services = Services(tmp_path)
    router = artifact_router_factory(
        artifacts=services.read, logs=services.logs, authorizer=Authorizer()
    )
    registry = RouteRegistry()
    registry.register(router)
    assert [(route.method, route.path) for route in registry.routes] == [
        ("GET", "/v1/artifacts/{artifact_id}"),
        ("POST", "/v1/artifacts/operations"),
    ]
