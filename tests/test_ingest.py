from __future__ import annotations

import hashlib
import json
from pathlib import Path

from rk.domain import ArtifactInput
from rk.ingest import (
    EvidenceIngest,
    IngestDisposition,
    IngestExpectation,
    IngestPolicy,
)


def policy(inbox: Path) -> IngestPolicy:
    return IngestPolicy(
        inbox_roots=(inbox,),
        max_artifact_bytes=4096,
        max_archive_expanded_bytes=8192,
        max_archive_files=16,
        max_archive_ratio=20.0,
        known_secret_values=(b"known-secret-value",),
    )


def declared(path: Path, *, media_type: str = "text/markdown") -> ArtifactInput:
    data = path.read_bytes()
    return ArtifactInput(
        name="proof.md" if media_type != "application/json" else "proof.json",
        path=str(path.resolve()),
        sha256=hashlib.sha256(data).hexdigest(),
        byte_count=len(data),
        media_type=media_type,
    )


def codes(result: object) -> set[str]:
    return {item.code for item in result.findings}  # type: ignore[attr-defined]


def test_accepts_matching_text_headers_without_granting_a_verdict(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    source = inbox / "proof.md"
    source.write_text("STATUS: PROVED\nTARGET_SCOPE: R03-M5\n\nArgument.\n", encoding="utf-8")
    result = EvidenceIngest(policy(inbox)).inspect(
        declared(source),
        expectation=IngestExpectation(
            scope="R03-M5",
            status_values=frozenset({"PROVED"}),
            provenance={"provider": "fixture", "source_commit": "abc"},
            required_provenance_fields=frozenset({"provider", "source_commit"}),
        ),
    )
    assert result.accepted
    assert result.disposition is IngestDisposition.ACCEPT
    assert result.status == "PROVED"
    assert result.scope == "R03-M5"
    assert result.provenance_status == "DECLARED"
    assert "verdict" not in result.to_dict()


def test_hash_and_size_mismatch_reject_before_cas(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    source = inbox / "proof.md"
    source.write_text("STATUS: INCOMPLETE\n", encoding="utf-8")
    artifact = ArtifactInput(
        name="proof.md",
        path=str(source.resolve()),
        sha256="0" * 64,
        byte_count=999,
        media_type="text/markdown",
    )
    result = EvidenceIngest(policy(inbox)).inspect(artifact)
    assert result.disposition is IngestDisposition.REJECT
    assert {"HASH_MISMATCH", "BYTE_COUNT_MISMATCH"} <= codes(result)


def test_secret_pattern_quarantines_without_echoing_secret(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    source = inbox / "proof.md"
    secret = "sk-" + ("0" * 32)
    source.write_text(f"STATUS: PROVED\n{secret}\n", encoding="utf-8")
    result = EvidenceIngest(policy(inbox)).inspect(declared(source))
    assert result.disposition is IngestDisposition.QUARANTINE
    assert "SECRET_QUARANTINED" in codes(result)
    assert secret not in json.dumps(result.to_dict())


def test_rejects_mixed_status_or_scope_blocks(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    source = inbox / "proof.md"
    source.write_text(
        "STATUS: PROVED\nTARGET_SCOPE: A\ntext\nSTATUS: WRONG\nTARGET_SCOPE: B\n",
        encoding="utf-8",
    )
    result = EvidenceIngest(policy(inbox)).inspect(declared(source))
    assert result.disposition is IngestDisposition.REJECT
    assert "MIXED_OUTPUT" in codes(result)


def test_json_schema_scope_and_embedded_provenance_are_checked(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    source = inbox / "proof.json"
    source.write_text(
        json.dumps(
            {
                "status": "correct",
                "scope": "claim-1",
                "proof": "ok",
                "provenance": {"commit": "good"},
            }
        ),
        encoding="utf-8",
    )
    result = EvidenceIngest(policy(inbox)).inspect(
        declared(source, media_type="application/json"),
        expectation=IngestExpectation(
            scope="claim-1",
            status_values=frozenset({"correct"}),
            provenance={"commit": "bad"},
            required_provenance_fields=frozenset({"commit"}),
            json_schema={
                "type": "object",
                "required": ["proof"],
                "properties": {"proof": {"type": "string"}},
            },
        ),
    )
    assert result.disposition is IngestDisposition.REJECT
    assert "PROVENANCE_MISMATCH" in codes(result)


def test_rejects_paths_outside_registered_inbox(tmp_path: Path) -> None:
    inbox = tmp_path / "inbox"
    inbox.mkdir()
    outside = tmp_path / "outside.md"
    outside.write_text("STATUS: PROVED\n", encoding="utf-8")
    result = EvidenceIngest(policy(inbox)).inspect(declared(outside))
    assert result.disposition is IngestDisposition.REJECT
    assert "PATH_OUTSIDE_INBOX" in codes(result)
