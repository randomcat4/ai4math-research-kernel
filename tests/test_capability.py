import json
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path

import pytest

from rk.capability import HmacCapabilityVerifier, sign_credential
from rk.domain import CapabilityError


@dataclass(frozen=True)
class FrozenClock:
    value: datetime

    def now(self) -> datetime:
        return self.value


def _payload() -> dict[str, object]:
    return {
        "schema_version": "rk.cap.v1",
        "capability_id": "018f0c3a-7b8e-7abc-8def-1234567890ad",
        "subject_id": "tester",
        "issuer": "test-host",
        "key_id": "test-key",
        "allowed_actions": ["FreezeContract"],
        "run_scope": ["run-1"],
        "issued_at": "2026-08-11T11:00:00.000Z",
        "expires_at": "2026-08-11T13:00:00.000Z",
        "nonce": "test-nonce",
    }


def test_signed_capability_is_scoped_by_action_and_run(tmp_path: Path) -> None:
    key = b"x" * 32
    path = tmp_path / "cap.json"
    path.write_text(json.dumps(sign_credential(_payload(), key)), encoding="utf-8")
    verifier = HmacCapabilityVerifier(
        lambda key_id: key if key_id == "test-key" else b"",
        FrozenClock(datetime(2026, 8, 11, 12, tzinfo=UTC)),
        require_private_file=False,
    )

    capability = verifier.verify(path, "FreezeContract", "run-1")
    assert capability.subject_id == "tester"
    with pytest.raises(CapabilityError):
        verifier.verify(path, "Finalize", "run-1")
    with pytest.raises(CapabilityError):
        verifier.verify(path, "FreezeContract", "run-2")


def test_tampering_is_rejected_without_detail(tmp_path: Path) -> None:
    key = b"y" * 32
    signed = sign_credential(_payload(), key)
    signed["allowed_actions"] = ["*"]
    path = tmp_path / "cap.json"
    path.write_text(json.dumps(signed), encoding="utf-8")
    verifier = HmacCapabilityVerifier(
        lambda _key_id: key,
        FrozenClock(datetime(2026, 8, 11, 12, tzinfo=UTC)),
        require_private_file=False,
    )

    with pytest.raises(CapabilityError, match="CAPABILITY_DENIED"):
        verifier.verify(path, "FreezeContract", "run-1")
