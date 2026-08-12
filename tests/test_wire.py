import hashlib
import json
from pathlib import Path

import pytest

from rk.domain import RequestValidationError
from rk.wire import WireValidator, request_digest

SPEC = Path(__file__).parents[1] / "docs" / "spec" / "json"


def test_spec_manifest_matches_every_listed_final_byte() -> None:
    spec_root = Path(__file__).parents[1] / "docs" / "spec"
    manifest = json.loads((spec_root / "manifest.json").read_text(encoding="utf-8"))
    entries = manifest["files"]

    assert manifest["manifest_self_excluded"] is True
    assert manifest["file_count"] == len(entries)
    assert len({entry["path"] for entry in entries}) == len(entries)
    total = 0
    for entry in entries:
        data = (spec_root / entry["path"]).read_bytes()
        total += len(data)
        assert entry["byte_count"] == len(data)
        assert entry["sha256"] == hashlib.sha256(data).hexdigest()
    assert manifest["total_bytes_excluding_manifest"] == total


def _request(path: str) -> dict[str, object]:
    return {
        "schema_version": "rk.command.v1",
        "operation": "apply",
        "request_id": "123e4567-e89b-42d3-a456-426614174000",
        "run_id": "018f0c3a-7b8e-7abc-8def-1234567890ab",
        "expected_revision": 0,
        "command": {
            "type": "FreezeContract",
            "payload": {
                "contract_version": 1,
                "completeness_check_artifact_id": "018f0c3a-7b8e-7abc-8def-1234567890ac",
            },
        },
        "artifact_inputs": [
            {
                "name": "proof.md",
                "path": path,
                "sha256": "a" * 64,
                "byte_count": 3,
                "media_type": "text/markdown",
            }
        ],
    }


def test_request_digest_does_not_bind_retry_to_staging_path() -> None:
    assert request_digest(_request("C:/inbox/a")) == request_digest(_request("C:/inbox/b"))


def test_wire_validator_accepts_valid_and_rejects_missing_field() -> None:
    validator = WireValidator(SPEC / "command.schema.json", SPEC / "receipt.schema.json")
    value = _request("C:/inbox/a")
    validator.validate_request(value)
    invalid = json.loads(json.dumps(value))
    del invalid["command"]["payload"]["contract_version"]
    with pytest.raises(RequestValidationError):
        validator.validate_request(invalid)


def test_wire_rejects_caller_supplied_peer_independence_profile() -> None:
    validator = WireValidator(SPEC / "command.schema.json", SPEC / "receipt.schema.json")
    value = _request("C:/inbox/a")
    value["command"] = {
        "type": "RecordPeerReview",
        "payload": {
            "claim_id": "018f0c3a-7b8e-7abc-8def-1234567890ac",
            "contract_version": 1,
            "statement_hash": "a" * 64,
            "review_artifact_id": "018f0c3a-7b8e-7abc-8def-1234567890ad",
            "verdict": "ACCEPT",
            "checklist": {"proof_checked": True},
            "source_graph": {"review": "018f0c3a-7b8e-7abc-8def-1234567890ad"},
            "independence_profile": {"independent": True},
        },
    }

    with pytest.raises(RequestValidationError):
        validator.validate_request(value)
