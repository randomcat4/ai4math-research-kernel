import hashlib
import os
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

from rk.cas import (
    CasCorruptionError,
    CasValidationError,
    ContentAddressedStore,
)
from rk.domain import ArtifactInput

NOW = datetime(2026, 8, 11, 12, tzinfo=UTC)


class CounterIds:
    def __init__(self) -> None:
        self.value = 0

    def new(self) -> str:
        self.value += 1
        return f"id-{self.value}"


def _store(
    tmp_path: Path,
    *,
    hook=None,
    lookup=None,
    grace: int = 60,
) -> tuple[ContentAddressedStore, Path, Path]:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    root = tmp_path / "cas"
    store = ContentAddressedStore(
        root,
        max_bytes=1024,
        inbox_roots=(inbox,),
        orphan_grace_seconds=grace,
        id_generator=CounterIds(),
        artifact_lookup=lookup,
        fault_hook=hook,
    )
    return store, inbox, root


def _input(path: Path, data: bytes) -> ArtifactInput:
    path.write_bytes(data)
    return ArtifactInput(
        name=path.name,
        path=str(path.resolve()),
        sha256=hashlib.sha256(data).hexdigest(),
        byte_count=len(data),
        media_type="text/plain",
    )


def test_stage_commit_read_and_same_content_deduplicate(tmp_path: Path) -> None:
    records: dict[str, object] = {}
    store, inbox, root = _store(tmp_path, lookup=lambda artifact_id: records.get(artifact_id))
    value = _input(inbox / "proof.txt", b"line one\nline two\n")

    first = store.commit(store.stage_input(value), now=NOW)
    records[first.artifact_id] = first.to_record()
    second = store.commit(store.stage_input(value), now=NOW)

    assert first.sha256 == second.sha256
    assert first.cas_relpath == second.cas_relpath
    assert first.line_count == 2
    assert store.read_bytes(first.artifact_id) == b"line one\nline two\n"
    assert len(list(root.glob("[0-9a-f][0-9a-f]/[0-9a-f][0-9a-f]/*"))) == 1
    assert list((root / ".stage").iterdir()) == []


def test_declared_mismatch_and_unsafe_paths_never_commit(tmp_path: Path) -> None:
    store, inbox, root = _store(tmp_path)
    source = inbox / "input.txt"
    value = _input(source, b"actual")
    wrong = ArtifactInput(
        name=value.name,
        path=value.path,
        sha256="0" * 64,
        byte_count=value.byte_count,
        media_type=value.media_type,
    )
    with pytest.raises(CasValidationError, match="does not match"):
        store.stage_input(wrong)
    outside = tmp_path / "outside.txt"
    outside.write_bytes(b"outside")
    with pytest.raises(CasValidationError, match="outside configured inbox"):
        store.stage_input(_input(outside, b"outside"))
    with pytest.raises(CasValidationError, match="absolute"):
        store.stage_input(
            ArtifactInput(
                name="relative",
                path="relative.txt",
                sha256=hashlib.sha256(b"x").hexdigest(),
                byte_count=1,
                media_type="text/plain",
            )
        )
    assert list((root / ".stage").iterdir()) == []


def test_replace_then_crash_leaves_discoverable_final_orphan(tmp_path: Path) -> None:
    def hook(point: str, _staged: object) -> None:
        if point == "after_final_replace":
            raise RuntimeError("simulated crash")

    store, _inbox, root = _store(tmp_path, hook=hook, grace=1)
    staged = store.stage_bytes(b"durable", media_type="application/octet-stream")
    with pytest.raises(RuntimeError, match="simulated"):
        store.commit(staged, now=NOW)

    finals = list(root.glob("[0-9a-f][0-9a-f]/[0-9a-f][0-9a-f]/*"))
    assert len(finals) == 1
    old = (NOW - timedelta(seconds=10)).timestamp()
    os.utime(finals[0], (old, old))
    candidates = store.scan_orphans((), now=NOW)
    assert [(item.kind, item.eligible_for_collection) for item in candidates] == [
        ("FINAL", True)
    ]


def test_crash_before_replace_leaves_staged_orphan(tmp_path: Path) -> None:
    def hook(point: str, _staged: object) -> None:
        if point == "before_final_replace":
            raise RuntimeError("simulated crash")

    store, _inbox, root = _store(tmp_path, hook=hook, grace=1)
    staged = store.stage_bytes(b"not-final", media_type="application/octet-stream")
    with pytest.raises(RuntimeError):
        store.commit(staged, now=NOW)
    old = (NOW - timedelta(seconds=10)).timestamp()
    os.utime(staged.stage_path, (old, old))
    candidates = store.scan_orphans((), now=NOW)
    assert [(item.kind, item.relpath) for item in candidates] == [
        ("STAGED", f".stage/{staged.stage_path.name}")
    ]
    assert list(root.glob("[0-9a-f][0-9a-f]/[0-9a-f][0-9a-f]/*")) == []


def test_existing_corrupt_final_is_rejected(tmp_path: Path) -> None:
    store, _inbox, root = _store(tmp_path)
    staged = store.stage_bytes(b"expected", media_type="application/octet-stream")
    final = root / staged.final_relpath
    final.parent.mkdir(parents=True)
    final.write_bytes(b"corrupt")

    with pytest.raises(CasCorruptionError, match="does not match"):
        store.commit(staged, now=NOW)
    assert staged.stage_path.exists()
