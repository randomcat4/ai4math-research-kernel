"""OpenAlex works search connector with strict response projection."""

from __future__ import annotations

import json
from urllib.parse import urlencode

from rk.product.literature_connectors.base import (
    ConnectorFetch,
    ConnectorStatus,
    HttpTransport,
    TransportFailure,
    transport_failure_fetch,
)


class OpenAlexConnector:
    name = "OPENALEX"
    version = "openalex-works-v1"

    def __init__(
        self,
        transport: HttpTransport,
        *,
        endpoint: str = "https://api.openalex.org/works",
    ) -> None:
        self._transport = transport
        self._base_endpoint = endpoint

    def query(
        self, request: dict[str, object], *, timeout_seconds: float
    ) -> ConnectorFetch:
        if set(request) != {"query", "per_page"}:
            raise ValueError("OpenAlex request fields are not exact")
        query, count = request["query"], request["per_page"]
        if (
            not isinstance(query, str)
            or not query.strip()
            or not isinstance(count, int)
            or isinstance(count, bool)
            or count < 1
            or count > 100
        ):
            raise ValueError("OpenAlex query is invalid")
        exact = {"query": query.strip(), "per_page": count}
        endpoint = self._base_endpoint + "?" + urlencode(
            {"search": exact["query"], "per-page": count}
        )
        try:
            response = self._transport.request(
                method="GET",
                endpoint=endpoint,
                headers=(
                    ("Accept", "application/json"),
                    ("User-Agent", "rk-product/1.1 (mailto:research@example.invalid)"),
                ),
                body=None,
                timeout_seconds=timeout_seconds,
            )
        except TransportFailure as failure:
            return transport_failure_fetch(
                connector=self.name,
                version=self.version,
                endpoint=endpoint,
                request=exact,
                failure=failure,
            )
        return self._parse(exact, endpoint, response.status, response.body)

    def _parse(
        self, request: dict[str, object], endpoint: str, status: int, body: bytes
    ) -> ConnectorFetch:
        if status != 200:
            return _failure(self, request, endpoint, status, body, "HTTP_ERROR")
        try:
            value = json.loads(body.decode())
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            return _failure(
                self, request, endpoint, status, body, "SCHEMA_DRIFT", str(error)
            )
        if not isinstance(value, dict) or not isinstance(value.get("results"), list):
            return _failure(
                self, request, endpoint, status, body, "SCHEMA_DRIFT", "results missing"
            )
        results: list[dict[str, object]] = []
        for item in value["results"]:
            if not isinstance(item, dict) or not isinstance(item.get("id"), str):
                return _failure(
                    self, request, endpoint, status, body, "SCHEMA_DRIFT", "work ID drift"
                )
            authorships = item.get("authorships", [])
            if not isinstance(authorships, list):
                return _failure(
                    self, request, endpoint, status, body, "SCHEMA_DRIFT", "authorship drift"
                )
            authors = []
            for authorship in authorships:
                if isinstance(authorship, dict) and isinstance(authorship.get("author"), dict):
                    name = authorship["author"].get("display_name")
                    if isinstance(name, str):
                        authors.append(name)
            results.append(
                {
                    "openalex_id": item["id"],
                    "doi": item.get("doi"),
                    "title": item.get("title"),
                    "publication_year": item.get("publication_year"),
                    "authors": authors,
                    "cited_by_count": item.get("cited_by_count"),
                }
            )
        meta_value = value.get("meta")
        meta: dict[str, object] = meta_value if isinstance(meta_value, dict) else {}
        state = ConnectorStatus.SUCCESS if results else ConnectorStatus.NO_HIT
        return ConnectorFetch(
            self.name,
            self.version,
            endpoint,
            request,
            status,
            "application/json",
            body,
            "WIRE_RESPONSE",
            None,
            {"returned": len(results), "total": meta.get("count"), "complete": False},
            {"results": results, "candidate_kind": "BIBLIOGRAPHIC_CANDIDATE"},
            state,
            None,
            None,
        )


def _failure(
    connector: OpenAlexConnector,
    request: dict[str, object],
    endpoint: str,
    status: int,
    body: bytes,
    code: str,
    detail: str | None = None,
) -> ConnectorFetch:
    return ConnectorFetch(
        connector.name,
        connector.version,
        endpoint,
        request,
        status,
        "application/json",
        body,
        "WIRE_RESPONSE",
        None,
        {"returned": 0, "complete": False},
        {"results": []},
        ConnectorStatus(code),
        code,
        detail or f"HTTP {status}",
    )


__all__ = ["OpenAlexConnector"]
