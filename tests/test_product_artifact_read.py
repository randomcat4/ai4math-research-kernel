from __future__ import annotations

import hashlib
import sqlite3
from pathlib import Path

import pytest

from rk.product.artifact_read import (
    ArtifactBindingMismatch,
    ArtifactReadError,
    ArtifactReadService,
    ExactArtifactRef,
    InvalidByteRange,
)
from rk.storage import SQLiteStorage


def stored_artifact(
    tmp_path: Path,
    data: bytes,
    *,
    media_type: str = "application/octet-stream",
    logical_name: str = "data.bin",
) -> tuple[ArtifactReadService, str]:
    database = tmp_path / "rk.sqlite"
    cas_root = tmp_path / "cas"
    digest = hashlib.sha256(data).hexdigest()
    relpath = f"{digest[:2]}/{digest[2:4]}/{digest}"
    target = cas_root / relpath
    target.parent.mkdir(parents=True)
    target.write_bytes(data)
    with sqlite3.connect(database) as connection:
        connection.execute(
            "CREATE TABLE artifacts(artifact_id TEXT PRIMARY KEY,sha256 TEXT,byte_count "
            "INTEGER,media_type TEXT,cas_relpath TEXT,ingest_state TEXT,source_name TEXT,"
            "line_count INTEGER)"
        )
        connection.execute(
            "INSERT INTO artifacts VALUES(?,?,?,?,?,'COMMITTED',?,?)",
            (
                "artifact-1",
                digest,
                len(data),
                media_type,
                relpath,
                logical_name,
                data.count(b"\n") if media_type.startswith("text/") else None,
            ),
        )
    return (
        ArtifactReadService(
            metadata=SQLiteStorage(database, 5_000),
            cas_root=cas_root,
            stream_chunk_bytes=7,
        ),
        digest,
    )


def test_describe_returns_exact_ref_logical_name_media_and_typed_viewer(tmp_path: Path) -> None:
    data = b"theorem rk : True := by trivial\n"
    service, digest = stored_artifact(
        tmp_path, data, media_type="text/x-lean; charset=utf-8", logical_name="定理.lean"
    )

    descriptor = service.describe("artifact-1")

    assert descriptor.ref == ExactArtifactRef(
        "artifact-1", digest, len(data), "text/x-lean; charset=utf-8"
    )
    assert descriptor.logical_name == "定理.lean"
    assert descriptor.viewer.viewer == "LEAN"
    assert descriptor.viewer.syntax == "lean"
    assert descriptor.line_count == 1
    assert "filename*=UTF-8''%E5%AE%9A%E7%90%86.lean" in descriptor.content_disposition


@pytest.mark.parametrize(
    ("media_type", "name", "viewer"),
    [
        ("application/pdf", "paper.pdf", "PDF"),
        ("image/png", "plot.png", "IMAGE"),
        ("application/json", "result.json", "JSON"),
        ("application/x-tex", "paper.tex", "TEX"),
        ("text/plain; charset=utf-8", "notes.txt", "TEXT"),
        ("application/octet-stream", "archive.bin", "BINARY"),
    ],
)
def test_viewer_metadata_is_derived_from_registered_media_and_logical_name(
    tmp_path: Path, media_type: str, name: str, viewer: str
) -> None:
    service, _digest = stored_artifact(
        tmp_path, b"content", media_type=media_type, logical_name=name
    )
    assert service.describe("artifact-1").viewer.viewer == viewer


@pytest.mark.parametrize(
    ("header", "expected", "content_range"),
    [
        ("bytes=0-3", b"0123", "bytes 0-3/16"),
        ("bytes=5-", b"56789abcdef", "bytes 5-15/16"),
        ("bytes=-4", b"cdef", "bytes 12-15/16"),
        ("bytes=13-99", b"def", "bytes 13-15/16"),
    ],
)
def test_single_http_range_streams_exact_bytes_and_headers(
    tmp_path: Path, header: str, expected: bytes, content_range: str
) -> None:
    service, _digest = stored_artifact(tmp_path, b"0123456789abcdef")

    result = service.open_range("artifact-1", range_header=header)

    assert b"".join(result.stream) == expected
    assert result.partial is True
    assert result.headers["content-range"] == content_range
    assert result.headers["content-length"] == str(len(expected))
    assert result.headers["accept-ranges"] == "bytes"


def test_large_artifact_is_emitted_in_bounded_segments_not_one_buffer(tmp_path: Path) -> None:
    data = bytes(range(256)) * 16_384
    service, _digest = stored_artifact(tmp_path, data)

    chunks = list(service.open_range("artifact-1", range_header="bytes=1-1048576").stream)

    assert b"".join(chunks) == data[1 : 1024 * 1024 + 1]
    assert len(chunks) > 1
    assert max(map(len, chunks)) == 7


@pytest.mark.parametrize(
    "header",
    ["bytes=", "bytes=8-7", "bytes=16-20", "bytes=0-1,4-5", "items=0-1", "bytes=-0"],
)
def test_malformed_multipart_and_unsatisfiable_ranges_are_rejected(
    tmp_path: Path, header: str
) -> None:
    service, _digest = stored_artifact(tmp_path, b"0123456789abcdef")
    with pytest.raises(InvalidByteRange):
        service.open_range("artifact-1", range_header=header)


def test_exact_artifact_ref_binding_cannot_be_silently_retargeted(tmp_path: Path) -> None:
    service, digest = stored_artifact(tmp_path, b"bound")
    correct = ExactArtifactRef("artifact-1", digest, 5, "application/octet-stream")
    assert service.describe("artifact-1", expected_ref=correct).ref == correct
    with pytest.raises(ArtifactBindingMismatch):
        service.open_range(
            "artifact-1",
            expected_ref=ExactArtifactRef("artifact-1", "0" * 64, 5, "application/octet-stream"),
        )


def test_registry_length_or_cas_address_mismatch_is_not_served(tmp_path: Path) -> None:
    service, _digest = stored_artifact(tmp_path, b"canonical")
    with sqlite3.connect(tmp_path / "rk.sqlite") as connection:
        connection.execute("UPDATE artifacts SET byte_count=999 WHERE artifact_id='artifact-1'")
    with pytest.raises(ArtifactReadError, match="length"):
        service.open_range("artifact-1")


def test_symbolic_link_inside_cas_address_is_never_served(tmp_path: Path) -> None:
    service, digest = stored_artifact(tmp_path, b"canonical")
    target = tmp_path / "cas" / digest[:2] / digest[2:4] / digest
    outside = tmp_path / "outside"
    outside.write_bytes(b"canonical")
    target.unlink()
    try:
        target.symlink_to(outside)
    except OSError:
        pytest.skip("symbolic links are unavailable")
    with pytest.raises(ArtifactReadError, match="symbolic link"):
        service.open_range("artifact-1")
