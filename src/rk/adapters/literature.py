"""Crossref bibliographic search adapter."""

from __future__ import annotations

import urllib.parse
import urllib.request
from collections.abc import Mapping
from typing import Any

from rk.adapters.base import AdapterProfile, AdapterRequestError, load_json, require_exact_keys


class CrossrefLiteratureAdapter:
    """Retrieve bibliographic candidates; never interpret absence as a proof."""

    trust_limit = "BIBLIOGRAPHIC_CANDIDATE"

    def __init__(self, profile: AdapterProfile) -> None:
        profile.require("endpoint")
        self.profile = profile
        self.name = profile.name
        self.version = profile.version

    def run(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        require_exact_keys(
            request,
            required=frozenset({"query", "rows"}),
            label="Crossref literature request",
        )
        query, rows = request["query"], request["rows"]
        if not isinstance(query, str) or not query.strip():
            raise AdapterRequestError("query must be non-empty")
        if not isinstance(rows, int) or isinstance(rows, bool) or not 1 <= rows <= 20:
            raise AdapterRequestError("rows must be between 1 and 20")
        endpoint = self.profile.endpoint
        assert endpoint is not None
        url = endpoint + "?" + urllib.parse.urlencode(
            {"query.bibliographic": query, "rows": rows, "select": "DOI,title,author,published"}
        )
        req = urllib.request.Request(url, headers={"User-Agent": "ai4math-rk/1"})
        try:
            with urllib.request.urlopen(req, timeout=self.profile.timeout_seconds) as response:
                raw = response.read(self.profile.max_response_bytes + 1)
                status = int(response.status)
        except (OSError, TimeoutError):
            return {**self.profile.provenance(), "status": "ENVIRONMENT_ERROR"}
        if status != 200 or len(raw) > self.profile.max_response_bytes:
            return {**self.profile.provenance(), "status": "FAILED", "http_status": status}
        try:
            value = load_json(raw)
            message = value.get("message") if isinstance(value, Mapping) else None
            items = message.get("items") if isinstance(message, Mapping) else None
            if not isinstance(items, list):
                raise ValueError("items missing")
            candidates = []
            for item in items:
                if not isinstance(item, Mapping):
                    raise ValueError("item is not an object")
                title = item.get("title")
                candidates.append(
                    {
                        "doi": item.get("DOI"),
                        "title": title[0] if isinstance(title, list) and title else None,
                        "authors": item.get("author", []),
                        "published": item.get("published"),
                    }
                )
        except (UnicodeDecodeError, ValueError):
            return {**self.profile.provenance(), "status": "ADAPTER_SCHEMA_MISMATCH"}
        return {
            **self.profile.provenance(),
            "status": "COMPLETED" if candidates else "SEARCH_INCOMPLETE",
            "payload": {"candidates": candidates},
            "trust_limit": self.trust_limit,
            "machine_axis_effect": "UNCHANGED",
            "no_hit_is_proof": False,
        }
