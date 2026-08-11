"""Rethlas natural-language verifier adapter."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from typing import Any

from rk.adapters.base import (
    AdapterProfile,
    AdapterRequestError,
    HttpClient,
    UrlLibHttpClient,
    canonical_json_sha256,
    load_json,
    require_exact_keys,
)


class RethlasAdapter:
    """Call the verifier while permanently capping its evidence at ``SOFT_MODEL``."""

    trust_limit = "SOFT_MODEL"

    def __init__(
        self,
        profile: AdapterProfile,
        *,
        client: HttpClient | None = None,
    ) -> None:
        profile.require("endpoint")
        self.profile = profile
        self.client = client or UrlLibHttpClient()
        self.name = profile.name
        self.version = profile.version

    def run(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        require_exact_keys(
            request,
            required=frozenset({"statement", "proof"}),
            label="Rethlas request",
        )
        statement = request["statement"]
        proof = request["proof"]
        if not isinstance(statement, str) or not statement.strip():
            raise AdapterRequestError("statement must be a non-empty string")
        if not isinstance(proof, str) or not proof.strip():
            raise AdapterRequestError("proof must be a non-empty string")
        endpoint = self.profile.endpoint
        assert endpoint is not None
        common = {
            **self.profile.provenance(),
            "trust_limit": self.trust_limit,
            "evidence_type": "MODEL_JUDGE",
            "evidence_strength": "SOFT_MODEL",
            "machine_axis_effect": "UNCHANGED",
            "request_hash": canonical_json_sha256({"statement": statement, "proof": proof}),
        }
        try:
            response = self.client.post_json(
                endpoint,
                {"statement": statement, "proof": proof},
                timeout=self.profile.timeout_seconds,
                max_response_bytes=self.profile.max_response_bytes,
            )
        except (OSError, TimeoutError, ValueError):
            return {**common, "status": "ENVIRONMENT_ERROR", "payload": None}
        if response.status_code == 504:
            return {**common, "status": "PAUSED", "http_status": 504, "payload": None}
        if response.status_code >= 500:
            return {
                **common,
                "status": "ADAPTER_SCHEMA_MISMATCH",
                "http_status": response.status_code,
                "payload": None,
            }
        if not 200 <= response.status_code < 300:
            return {
                **common,
                "status": "FAILED",
                "http_status": response.status_code,
                "payload": None,
            }
        try:
            payload = load_json(response.body)
            self._validate_payload(payload)
        except (UnicodeDecodeError, ValueError, TypeError):
            return {
                **common,
                "status": "ADAPTER_SCHEMA_MISMATCH",
                "http_status": response.status_code,
                "payload": None,
            }
        return {
            **common,
            "status": "COMPLETED",
            "http_status": response.status_code,
            "payload": payload,
        }

    @staticmethod
    def _validate_payload(payload: Any) -> None:
        if not isinstance(payload, Mapping):
            raise ValueError("Rethlas response must be an object")
        require_exact_keys(
            payload,
            required=frozenset({"verification_report", "verdict", "repair_hints"}),
            label="Rethlas response",
        )
        report = payload["verification_report"]
        if not isinstance(report, Mapping):
            raise ValueError("verification_report must be an object")
        require_exact_keys(
            report,
            required=frozenset({"summary", "critical_errors", "gaps"}),
            label="verification_report",
        )
        if not isinstance(report["summary"], str):
            raise ValueError("summary must be a string")
        for field_name in ("critical_errors", "gaps"):
            entries = report[field_name]
            if not isinstance(entries, Sequence) or isinstance(entries, (str, bytes)):
                raise ValueError(f"{field_name} must be an array")
            for entry in entries:
                if not isinstance(entry, Mapping):
                    raise ValueError(f"{field_name} entries must be objects")
                require_exact_keys(
                    entry,
                    required=frozenset({"location", "issue"}),
                    label=f"{field_name} entry",
                )
                if not isinstance(entry["location"], str) or not isinstance(entry["issue"], str):
                    raise ValueError(f"{field_name} fields must be strings")
        verdict = payload["verdict"]
        repair_hints = payload["repair_hints"]
        if verdict not in {"correct", "wrong"} or not isinstance(repair_hints, str):
            raise ValueError("invalid verdict or repair_hints")
        has_errors = bool(report["critical_errors"] or report["gaps"])
        if verdict == "correct" and (has_errors or repair_hints):
            raise ValueError("correct verdict conflicts with errors, gaps, or repair hints")
        if verdict == "wrong" and not repair_hints.strip():
            raise ValueError("wrong verdict requires repair hints")
