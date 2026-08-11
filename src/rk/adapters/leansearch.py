"""LeanSearch premise-retrieval adapter."""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from typing import Any

from rk.adapters.base import (
    DEFAULT_SLEEPER,
    AdapterProfile,
    AdapterRequestError,
    HttpClient,
    Sleeper,
    UrlLibHttpClient,
    canonical_json_sha256,
    load_json,
    require_exact_keys,
)


class LeanSearchAdapter:
    """Retrieve candidates; similarity and non-results never become mathematical verdicts."""

    trust_limit = "PREMISE_CANDIDATE"

    def __init__(
        self,
        profile: AdapterProfile,
        *,
        client: HttpClient | None = None,
        sleeper: Sleeper = DEFAULT_SLEEPER,
    ) -> None:
        profile.require("endpoint")
        self.profile = profile
        self.client = client or UrlLibHttpClient()
        self.sleeper = sleeper
        self.name = profile.name
        self.version = profile.version

    def run(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        require_exact_keys(
            request,
            required=frozenset({"query", "num_results", "rerank", "retrieve_k"}),
            label="LeanSearch request",
        )
        query = request["query"]
        num_results = request["num_results"]
        rerank = request["rerank"]
        retrieve_k = request["retrieve_k"]
        if not isinstance(query, Sequence) or isinstance(query, (str, bytes)):
            raise AdapterRequestError("query must be an array")
        queries = list(query)
        if not 1 <= len(queries) <= 32 or any(
            not isinstance(item, str) or not item or len(item.encode("utf-8")) > 4096
            for item in queries
        ):
            raise AdapterRequestError("query violates count, type, or byte limits")
        if (
            isinstance(num_results, bool)
            or not isinstance(num_results, int)
            or not 1 <= num_results <= 50
        ):
            raise AdapterRequestError("num_results must be in [1, 50]")
        if not isinstance(rerank, bool):
            raise AdapterRequestError("rerank must be boolean")
        if retrieve_k is not None and (
            isinstance(retrieve_k, bool)
            or not isinstance(retrieve_k, int)
            or retrieve_k < num_results
        ):
            raise AdapterRequestError("retrieve_k must be null or at least num_results")
        payload = {
            "query": queries,
            "num_results": num_results,
            "rerank": rerank,
            "retrieve_k": retrieve_k,
        }
        endpoint = self.profile.endpoint
        assert endpoint is not None
        common = {
            **self.profile.provenance(),
            "trust_limit": self.trust_limit,
            "evidence_type": "PREMISE_CANDIDATE",
            "machine_axis_effect": "UNCHANGED",
            "semantic_axis_effect": "UNCHANGED",
            "closure_axis_effect": "UNCHANGED",
            "query_hash": canonical_json_sha256(payload),
        }
        response = None
        attempts = self.profile.max_retries + 1
        for attempt in range(attempts):
            try:
                candidate = self.client.post_json(
                    endpoint,
                    payload,
                    timeout=self.profile.timeout_seconds,
                    max_response_bytes=self.profile.max_response_bytes,
                )
            except (OSError, TimeoutError, ValueError):
                if attempt + 1 >= attempts:
                    return {**common, "status": "ENVIRONMENT_ERROR", "payload": None}
                self.sleeper(self.profile.backoff_seconds * (2**attempt))
                continue
            response = candidate
            if candidate.status_code in self.profile.retry_statuses and attempt + 1 < attempts:
                self.sleeper(self.profile.backoff_seconds * (2**attempt))
                continue
            break
        if response is None:
            return {**common, "status": "ENVIRONMENT_ERROR", "payload": None}
        if not 200 <= response.status_code < 300:
            return {
                **common,
                "status": "ENVIRONMENT_ERROR",
                "http_status": response.status_code,
                "payload": None,
            }
        try:
            decoded = load_json(response.body)
            batches = self._validate_and_normalize(decoded, expected_batches=len(queries))
        except (UnicodeDecodeError, ValueError, TypeError):
            return {
                **common,
                "status": "ADAPTER_SCHEMA_MISMATCH",
                "http_status": response.status_code,
                "payload": None,
            }
        response_hash = canonical_json_sha256(decoded)
        status = "SEARCH_INCOMPLETE" if not any(batches) else "COMPLETED"
        return {
            **common,
            "status": status,
            "http_status": response.status_code,
            "response_hash": response_hash,
            "payload": {"batches": batches},
        }

    @classmethod
    def _validate_and_normalize(cls, value: Any, *, expected_batches: int) -> list[list[Any]]:
        if not isinstance(value, Sequence) or isinstance(value, (str, bytes)):
            raise ValueError("LeanSearch response must be an array")
        raw = list(value)
        if not raw:
            return [[] for _ in range(expected_batches)]
        if all(isinstance(item, Mapping) for item in raw):
            if expected_batches != 1:
                raise ValueError("flat response is valid only for one query")
            batches: list[list[Any]] = [raw]
        else:
            if len(raw) != expected_batches:
                raise ValueError("batch count does not match query count")
            batches = []
            for item in raw:
                if not isinstance(item, Sequence) or isinstance(item, (str, bytes)):
                    raise ValueError("batch must be an array")
                batches.append(list(item))
        for batch in batches:
            for hit in batch:
                cls._validate_hit(hit)
        return batches

    @staticmethod
    def _validate_hit(value: Any) -> None:
        if not isinstance(value, Mapping):
            raise ValueError("search hit must be an object")
        require_exact_keys(
            value,
            required=frozenset({"result", "distance"}),
            label="LeanSearch hit",
        )
        distance = value["distance"]
        if isinstance(distance, bool) or not isinstance(distance, (int, float)):
            raise ValueError("distance must be numeric")
        if not math.isfinite(float(distance)):
            raise ValueError("distance must be finite")
        result = value["result"]
        if not isinstance(result, Mapping):
            raise ValueError("result must be an object")
        required = frozenset(
            {
                "module_name",
                "kind",
                "name",
                "signature",
                "type",
                "value",
                "docstring",
                "informal_name",
                "informal_description",
            }
        )
        require_exact_keys(result, required=required, label="LeanSearch result")
        for field_name in ("module_name", "name"):
            parts = result[field_name]
            if not isinstance(parts, Sequence) or isinstance(parts, (str, bytes)):
                raise ValueError(f"{field_name} must be an array")
            if any(not isinstance(part, str) for part in parts):
                raise ValueError(f"{field_name} must contain strings")
        for field_name in ("kind", "signature", "type"):
            if not isinstance(result[field_name], str):
                raise ValueError(f"{field_name} must be a string")
        for field_name in ("value", "docstring", "informal_name", "informal_description"):
            if result[field_name] is not None and not isinstance(result[field_name], str):
                raise ValueError(f"{field_name} must be null or a string")
