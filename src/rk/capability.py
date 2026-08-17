"""Host-issued HMAC capability credentials.

Credentials are authenticated before request idempotency lookup so an unauthenticated caller
cannot retrieve a previous receipt or probe run existence.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import stat
import unicodedata
from collections.abc import Callable, Mapping
from pathlib import Path
from typing import Any

from rk.domain import CapabilityError, VerifiedCapability
from rk.ports import Clock
from rk.runtime import parse_utc

KeyResolver = Callable[[str], bytes]


def _canonical(value: Mapping[str, Any]) -> bytes:
    normalized: dict[str, Any] = {}
    for key, item in value.items():
        normalized[unicodedata.normalize("NFC", key)] = item
    text = json.dumps(normalized, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return text.encode("utf-8")


def _decode_signature(value: str) -> bytes:
    padding = "=" * (-len(value) % 4)
    try:
        return base64.urlsafe_b64decode(value + padding)
    except (ValueError, TypeError) as exc:
        raise CapabilityError("CAPABILITY_DENIED") from exc


def sign_credential(payload: Mapping[str, Any], key: bytes) -> dict[str, Any]:
    """Test/host helper. The returned mapping contains no signing key."""

    body = dict(payload)
    body.pop("signature", None)
    signature = hmac.new(key, b"rk.cap.v1\n" + _canonical(body), hashlib.sha256).digest()
    body["signature"] = base64.urlsafe_b64encode(signature).rstrip(b"=").decode("ascii")
    return body


class FileKeyResolver:
    """Resolve one host key file whose key id is supplied by configuration."""

    def __init__(self, path: Path, key_id: str) -> None:
        self._path = path
        self._key_id = key_id

    def __call__(self, key_id: str) -> bytes:
        if key_id != self._key_id:
            raise CapabilityError("CAPABILITY_DENIED")
        data = self._path.read_bytes()
        if len(data) < 32:
            raise CapabilityError("CAPABILITY_DENIED")
        return data


class HmacCapabilityVerifier:
    def __init__(
        self,
        key_resolver: KeyResolver,
        clock: Clock,
        *,
        require_private_file: bool = True,
        revoked_ids: frozenset[str] = frozenset(),
    ) -> None:
        self._key_resolver = key_resolver
        self._clock = clock
        self._require_private_file = require_private_file
        self._revoked_ids = revoked_ids

    def verify(self, credential_path: Path, action: str, run_id: str | None) -> VerifiedCapability:
        try:
            if self._require_private_file:
                self._check_file_permissions(credential_path)
            raw = json.loads(credential_path.read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise CapabilityError("CAPABILITY_DENIED")
            required = {
                "schema_version",
                "capability_id",
                "subject_id",
                "issuer",
                "key_id",
                "allowed_actions",
                "run_scope",
                "issued_at",
                "expires_at",
                "nonce",
                "signature",
            }
            if set(raw) != required or raw["schema_version"] != "rk.cap.v1":
                raise CapabilityError("CAPABILITY_DENIED")
            signature = _decode_signature(str(raw["signature"]))
            body = dict(raw)
            body.pop("signature")
            key = self._key_resolver(str(body["key_id"]))
            expected = hmac.new(key, b"rk.cap.v1\n" + _canonical(body), hashlib.sha256).digest()
            if not hmac.compare_digest(signature, expected):
                raise CapabilityError("CAPABILITY_DENIED")
            capability = VerifiedCapability(
                capability_id=str(body["capability_id"]),
                subject_id=str(body["subject_id"]),
                issuer=str(body["issuer"]),
                allowed_actions=frozenset(str(item) for item in body["allowed_actions"]),
                run_scope=frozenset(str(item) for item in body["run_scope"]),
                issued_at=str(body["issued_at"]),
                expires_at=str(body["expires_at"]),
            )
            now = self._clock.now()
            if not parse_utc(capability.issued_at) <= now < parse_utc(capability.expires_at):
                raise CapabilityError("CAPABILITY_DENIED")
            if capability.capability_id in self._revoked_ids:
                raise CapabilityError("CAPABILITY_DENIED")
            if not capability.allows(action, run_id):
                raise CapabilityError("CAPABILITY_DENIED")
            return capability
        except CapabilityError:
            raise
        except (OSError, ValueError, TypeError, KeyError, json.JSONDecodeError) as exc:
            raise CapabilityError("CAPABILITY_DENIED") from exc

    @staticmethod
    def _check_file_permissions(path: Path) -> None:
        if os.name == "nt":
            # Windows ACL validation is performed by the host launcher. A writable directory
            # or symlink is still rejected here; the verifier never follows credential links.
            if path.is_symlink() or not path.is_file():
                raise CapabilityError("CAPABILITY_DENIED")
            return
        mode = stat.S_IMODE(path.stat().st_mode)
        if mode & (stat.S_IRWXG | stat.S_IRWXO):
            raise CapabilityError("CAPABILITY_DENIED")


class StaticCapabilityVerifier:
    """In-memory adapter for tests; never used implicitly in production."""

    def __init__(self, capability: VerifiedCapability) -> None:
        self._capability = capability

    def verify(self, credential_path: Path, action: str, run_id: str | None) -> VerifiedCapability:
        del credential_path
        if not self._capability.allows(action, run_id):
            raise CapabilityError("CAPABILITY_DENIED")
        return self._capability
