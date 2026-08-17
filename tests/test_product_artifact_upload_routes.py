from __future__ import annotations

import asyncio
import hashlib
import json
import sqlite3
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from rk.cas import CommittedArtifact, ContentAddressedStore
from rk.domain import ArtifactRef
from rk.http_shell import (
    HttpErrorClass,
    HttpRequest,
    HttpResponse,
    ProductHttpError,
    RouteRegistry,
    SessionPrincipal,
    SessionRequest,
)
from rk.product.artifact_upload import ArtifactRegistry, ArtifactUploadStore
from rk.product.artifact_upload_routes import (
    ArtifactUploadOperation,
    ArtifactUploadRouter,
    artifact_upload_router,
)

ROOT = Path(__file__).parents[1]
UPLOAD_SQL = ROOT / "schema_fragments/B04a/upload.sql"
NOW = datetime(2026, 8, 13, 12, tzinfo=UTC)
PRINCIPAL = SessionPrincipal(
    session_id="session-1",
    subject_id="worker:one",
    capability_ids=("cap:worker:one",),
)


class Registry(ArtifactRegistry):
    def __init__(self) -> None:
        self.values: dict[str, ArtifactRef] = {}

    def find_by_sha256(self, sha256: str) -> ArtifactRef | None:
        return self.values.get(sha256)

    def register(self, artifact: CommittedArtifact) -> ArtifactRef:
        ref = artifact.to_ref(at_revision=0)
        return self.values.setdefault(ref.sha256, ref)


class SequentialIds:
    def __init__(self, prefix: str) -> None:
        self.prefix = prefix
        self.value = 0

    def new(self) -> str:
        return self()

    def __call__(self) -> str:
        self.value += 1
        return f"{self.prefix}-{self.value}"


class RecordingAuthorizer:
    def __init__(self, *, denied: bool = False) -> None:
        self.denied = denied
        self.calls: list[tuple[SessionPrincipal, ArtifactUploadOperation]] = []

    def __call__(
        self, principal: SessionPrincipal, operation: ArtifactUploadOperation
    ) -> None:
        self.calls.append((principal, operation))
        if self.denied:
            raise PermissionError("capability denied")


def _setup(
    tmp_path: Path, *, authorizer: RecordingAuthorizer | None = None
) -> tuple[ArtifactUploadRouter, ArtifactUploadStore, Registry, Path, Path]:
    db_path = tmp_path / "product.sqlite"
    with sqlite3.connect(db_path) as connection:
        connection.executescript(UPLOAD_SQL.read_text(encoding="utf-8"))
    spool = tmp_path / "spool"
    cas_root = tmp_path / "cas"
    registry = Registry()
    store = ArtifactUploadStore(
        db_path=db_path,
        spool_root=spool,
        cas=ContentAddressedStore(
            cas_root,
            max_bytes=2 * 1024 * 1024,
            inbox_roots=(spool,),
            orphan_grace_seconds=60,
            id_generator=SequentialIds("artifact"),
        ),
        registry=registry,
        id_generator=SequentialIds("upload"),
        clock=lambda: NOW,
        max_upload_bytes=1024 * 1024,
        max_chunk_bytes=64 * 1024,
    )
    router = artifact_upload_router(
        uploads=store, authorize=authorizer or RecordingAuthorizer()
    )
    return router, store, registry, cas_root, db_path


def _json_request(value: dict[str, Any]) -> HttpRequest:
    return HttpRequest(
        method="POST",
        path="/v1/artifacts/operations",
        headers={"content-type": "application/json; charset=utf-8"},
        body=json.dumps(value, sort_keys=True, separators=(",", ":")).encode(),
    )


def _invoke(
    router: ArtifactUploadRouter,
    request: HttpRequest,
    *,
    principal: SessionPrincipal = PRINCIPAL,
) -> HttpResponse:
    result = asyncio.run(router.handle(SessionRequest(request=request, principal=principal)))
    assert isinstance(result, HttpResponse)
    return result


def _begin(
    router: ArtifactUploadRouter,
    data: bytes,
    *,
    request_id: str = "request-1",
) -> str:
    response = _invoke(
        router,
        _json_request(
            {
                "type": "BEGIN_UPLOAD",
                "payload": {
                    "request_id": request_id,
                    "logical_name": "binary-proof.dat",
                    "media_type": "application/octet-stream",
                    "byte_count": len(data),
                    "sha256": hashlib.sha256(data).hexdigest(),
                },
            }
        ),
    )
    upload = response.body["upload"]
    assert isinstance(upload, dict)
    return str(upload["upload_id"])


def _append_request(
    upload_id: str,
    data: bytes,
    *,
    offset: int,
    digest: str | None = None,
    content_type: str = "application/octet-stream",
) -> HttpRequest:
    return HttpRequest(
        method="POST",
        path="/v1/artifacts/operations",
        headers={
            "content-type": content_type,
            "x-rk-artifact-operation": "APPEND_CHUNK",
            "x-rk-upload-id": upload_id,
            "x-rk-upload-offset": str(offset),
            "x-rk-chunk-sha256": digest or hashlib.sha256(data).hexdigest(),
        },
        body=data,
    )


def test_route_registry_and_real_raw_bytes_begin_append_commit_chain(
    tmp_path: Path,
) -> None:
    authorizer = RecordingAuthorizer()
    router, store, registry, cas_root, _db = _setup(
        tmp_path, authorizer=authorizer
    )
    routes = RouteRegistry()
    routes.register(router)
    assert [(item.method, item.path) for item in routes.routes] == [
        ("POST", "/v1/artifacts/operations")
    ]

    data = b"\x00not-json-or-base64\xff\x10proof-bytes"
    upload_id = _begin(router, data)
    split = 9
    first = _invoke(router, _append_request(upload_id, data[:split], offset=0))
    first_upload = first.body["upload"]
    assert isinstance(first_upload, dict)
    assert first_upload["received_byte_count"] == split
    _invoke(
        router,
        _append_request(upload_id, data[split:], offset=split),
    )
    committed = _invoke(
        router,
        _json_request(
            {"type": "COMMIT_UPLOAD", "payload": {"upload_id": upload_id}}
        ),
    )

    artifact = committed.body["artifact_ref"]
    assert isinstance(artifact, dict)
    digest = hashlib.sha256(data).hexdigest()
    assert artifact == {
        "artifact_id": registry.values[digest].artifact_id,
        "sha256": digest,
        "byte_count": len(data),
        "media_type": "application/octet-stream",
    }
    final = cas_root / digest[:2] / digest[2:4] / digest
    assert final.read_bytes() == data
    assert store.get(upload_id).state == "COMMITTED"
    assert [operation for _principal, operation in authorizer.calls] == [
        ArtifactUploadOperation.BEGIN_UPLOAD,
        ArtifactUploadOperation.APPEND_CHUNK,
        ArtifactUploadOperation.APPEND_CHUNK,
        ArtifactUploadOperation.COMMIT_UPLOAD,
    ]
    assert all(principal is PRINCIPAL for principal, _operation in authorizer.calls)


@pytest.mark.parametrize(
    ("location", "field"),
    [
        ("payload", "path"),
        ("payload", "host_path"),
        ("payload", "principal_subject_id"),
        ("envelope", "capability_id"),
        ("envelope", "actor"),
    ],
)
def test_begin_rejects_host_path_and_identity_or_capability_injection(
    tmp_path: Path, location: str, field: str
) -> None:
    router, _store, _registry, _cas, db_path = _setup(tmp_path)
    payload: dict[str, Any] = {
        "request_id": "request-injection",
        "logical_name": "paper.pdf",
        "media_type": "application/pdf",
        "byte_count": 0,
        "sha256": hashlib.sha256(b"").hexdigest(),
    }
    envelope: dict[str, Any] = {"type": "BEGIN_UPLOAD", "payload": payload}
    target = payload if location == "payload" else envelope
    target[field] = "C:/server/private/paper.pdf"

    with pytest.raises(ProductHttpError) as caught:
        _invoke(router, _json_request(envelope))

    assert caught.value.error_class is HttpErrorClass.SCHEMA
    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM product_uploads").fetchone() == (0,)


def test_append_requires_raw_octet_stream_and_never_decodes_json_base64(
    tmp_path: Path,
) -> None:
    router, store, _registry, _cas, _db = _setup(tmp_path)
    raw = b'{"base64":"YWJj"}'
    upload_id = _begin(router, raw)

    with pytest.raises(ProductHttpError) as caught:
        _invoke(
            router,
            _append_request(
                upload_id,
                raw,
                offset=0,
                content_type="application/json",
            ),
        )

    assert caught.value.code == "RAW_CHUNK_CONTENT_TYPE_REQUIRED"
    assert caught.value.error_class is HttpErrorClass.SCHEMA
    assert store.get(upload_id).received_byte_count == 0

    _invoke(router, _append_request(upload_id, raw, offset=0))
    assert store.get(upload_id).received_byte_count == len(raw)


@pytest.mark.parametrize(
    ("offset", "digest"),
    [
        (1, None),
        (0, "0" * 64),
    ],
)
def test_chunk_offset_and_digest_conflicts_keep_durable_offset(
    tmp_path: Path, offset: int, digest: str | None
) -> None:
    router, store, _registry, _cas, _db = _setup(tmp_path)
    data = b"chunk"
    upload_id = _begin(router, data)

    with pytest.raises(ProductHttpError) as caught:
        _invoke(
            router,
            _append_request(upload_id, data, offset=offset, digest=digest),
        )

    assert caught.value.error_class is HttpErrorClass.CONFLICT
    assert caught.value.code == "ARTIFACT_UPLOAD_CONFLICT"
    assert store.get(upload_id).received_byte_count == 0


def test_session_principal_is_middleware_derived_and_authorization_is_mandatory(
    tmp_path: Path,
) -> None:
    authorizer = RecordingAuthorizer(denied=True)
    router, _store, _registry, _cas, db_path = _setup(
        tmp_path, authorizer=authorizer
    )
    request = _json_request(
        {
            "type": "BEGIN_UPLOAD",
            "payload": {
                "request_id": "request-denied",
                "logical_name": "paper.pdf",
                "media_type": "application/pdf",
                "byte_count": 0,
                "sha256": hashlib.sha256(b"").hexdigest(),
            },
        }
    )

    with pytest.raises(ProductHttpError) as caught:
        _invoke(router, request)
    assert caught.value.error_class is HttpErrorClass.AUTHORIZATION
    assert authorizer.calls == [(PRINCIPAL, ArtifactUploadOperation.BEGIN_UPLOAD)]

    empty = SessionPrincipal(session_id="", subject_id="", capability_ids=())
    with pytest.raises(ProductHttpError) as unauthenticated:
        _invoke(router, request, principal=empty)
    assert unauthenticated.value.error_class is HttpErrorClass.AUTHENTICATION

    with sqlite3.connect(db_path) as connection:
        assert connection.execute("SELECT COUNT(*) FROM product_uploads").fetchone() == (0,)


def test_begin_is_idempotent_and_commit_rejects_incomplete_upload(
    tmp_path: Path,
) -> None:
    router, store, _registry, _cas, _db = _setup(tmp_path)
    data = b"incomplete"
    first = _begin(router, data)
    second = _begin(router, data)
    assert first == second

    with pytest.raises(ProductHttpError) as caught:
        _invoke(
            router,
            _json_request(
                {"type": "COMMIT_UPLOAD", "payload": {"upload_id": first}}
            ),
        )
    assert caught.value.error_class is HttpErrorClass.CONFLICT
    assert store.get(first).state == "OPEN"
