"""Wire-schema validation and stable request digests."""

from __future__ import annotations

import hashlib
import json
import unicodedata
from collections.abc import Mapping
from copy import deepcopy
from pathlib import Path
from typing import Any

from jsonschema import Draft202012Validator, FormatChecker

from rk.domain import RequestValidationError, TypedCommand
from rk.extensions import ExtensionRegistry


def _normalize(value: Any) -> Any:
    if value is None or isinstance(value, bool | int):
        return value
    if isinstance(value, float):
        raise RequestValidationError("floating-point values are not allowed in request digests")
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value.replace("\r\n", "\n").replace("\r", "\n"))
    if isinstance(value, list | tuple):
        return [_normalize(item) for item in value]
    if isinstance(value, Mapping):
        result: dict[str, Any] = {}
        for key, item in value.items():
            normalized_key = unicodedata.normalize("NFC", str(key))
            if normalized_key in result:
                raise RequestValidationError("normalization produced duplicate object keys")
            result[normalized_key] = _normalize(item)
        return result
    raise RequestValidationError(f"unsupported request value type: {type(value).__name__}")


def canonical_json_bytes(value: Mapping[str, Any]) -> bytes:
    normalized = _normalize(value)
    return json.dumps(
        normalized,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def request_digest(value: Mapping[str, Any]) -> str:
    """Digest semantic request content without host-local artifact paths."""

    semantic = deepcopy(dict(value))
    for artifact in semantic.get("artifact_inputs", []):
        if isinstance(artifact, dict):
            artifact.pop("path", None)
    return hashlib.sha256(b"rk.command.v1\n" + canonical_json_bytes(semantic)).hexdigest()


class WireValidator:
    def __init__(
        self,
        command_schema_path: Path,
        receipt_schema_path: Path,
        extensions: ExtensionRegistry | None = None,
    ) -> None:
        self._command_schema = json.loads(command_schema_path.read_text(encoding="utf-8"))
        self._receipt_schema = json.loads(receipt_schema_path.read_text(encoding="utf-8"))
        Draft202012Validator.check_schema(self._command_schema)
        Draft202012Validator.check_schema(self._receipt_schema)
        checker = FormatChecker()
        self._command = Draft202012Validator(self._command_schema, format_checker=checker)
        self._receipt = Draft202012Validator(self._receipt_schema, format_checker=checker)
        self._extensions = extensions or ExtensionRegistry()

    def validate_request(self, value: Mapping[str, Any]) -> TypedCommand | None:
        errors = sorted(self._command.iter_errors(dict(value)), key=lambda item: list(item.path))
        if not errors:
            return None
        variant = _legacy_variant(value)
        if variant is not None and variant in self._extensions.legacy_wire_dispatches:
            command = self._extensions.dispatch_legacy_wire(variant, value)
            if not isinstance(command, TypedCommand) or not command.type:
                raise RequestValidationError("legacy wire dispatch did not return a typed command")
            return command
        details = "; ".join(
            f"/{'/'.join(str(part) for part in error.path)}: {error.message}"
            for error in errors[:8]
        )
        raise RequestValidationError(details)

    def validate_receipt(self, value: Mapping[str, Any]) -> None:
        errors = sorted(self._receipt.iter_errors(dict(value)), key=lambda item: list(item.path))
        if errors:
            raise RequestValidationError(
                "; ".join(
                    f"/{'/'.join(str(part) for part in error.path)}: {error.message}"
                    for error in errors[:8]
                )
            )


def _legacy_variant(value: Mapping[str, Any]) -> str | None:
    explicit = value.get("variant")
    if isinstance(explicit, str) and explicit:
        return explicit
    command = value.get("command")
    if isinstance(command, Mapping):
        command_type = command.get("type")
        if isinstance(command_type, str) and command_type:
            return command_type
    return None
