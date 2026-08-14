"""Crossref works search connector with explicit coverage and drift receipts."""

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


class CrossrefConnector:
    name = "CROSSREF"
    version = "crossref-works-v1"

    def __init__(
        self,
        transport: HttpTransport,
        *,
        endpoint: str = "https://api.crossref.org/works",
    ) -> None:
        self._transport = transport
        self._base_endpoint = endpoint

    def query(
        self, request: dict[str, object], *, timeout_seconds: float
    ) -> ConnectorFetch:
        if set(request) != {"query", "rows"}:
            raise ValueError("Crossref request fields are not exact")
        query, rows = request["query"], request["rows"]
        if (
            not isinstance(query, str)
            or not query.strip()
            or not isinstance(rows, int)
            or isinstance(rows, bool)
            or rows < 1
            or rows > 100
        ):
            raise ValueError("Crossref query is invalid")
        exact = {"query": query.strip(), "rows": rows}
        endpoint = self._base_endpoint + "?" + urlencode(
            {"query": exact["query"], "rows": rows}
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
        if response.status != 200:
            return _failure(self, exact, endpoint, response.status, response.body, "HTTP_ERROR")
        try:
            value = json.loads(response.body.decode())
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            return _failure(
                self,
                exact,
                endpoint,
                response.status,
                response.body,
                "SCHEMA_DRIFT",
                str(error),
            )
        message = value.get("message") if isinstance(value, dict) else None
        if not isinstance(message, dict) or not isinstance(message.get("items"), list):
            return _failure(
                self,
                exact,
                endpoint,
                response.status,
                response.body,
                "SCHEMA_DRIFT",
                "message.items missing",
            )
        results = []
        for item in message["items"]:
            if not isinstance(item, dict) or not isinstance(item.get("DOI"), str):
                return _failure(
                    self,
                    exact,
                    endpoint,
                    response.status,
                    response.body,
                    "SCHEMA_DRIFT",
                    "Crossref DOI drift",
                )
            authors = item.get("author", [])
            if not isinstance(authors, list):
                authors = []
            results.append(
                {
                    "doi": item["DOI"],
                    "title": item.get("title", []),
                    "authors": [
                        {"given": author.get("given"), "family": author.get("family")}
                        for author in authors
                        if isinstance(author, dict)
                    ],
                    "type": item.get("type"),
                    "published": item.get("published"),
                }
            )
        state = ConnectorStatus.SUCCESS if results else ConnectorStatus.NO_HIT
        return ConnectorFetch(
            self.name,
            self.version,
            endpoint,
            exact,
            response.status,
            "application/json",
            response.body,
            "WIRE_RESPONSE",
            response.header("etag"),
            {
                "returned": len(results),
                "total": message.get("total-results"),
                "complete": False,
            },
            {"results": results, "candidate_kind": "BIBLIOGRAPHIC_CANDIDATE"},
            state,
            None,
            None,
        )


def _failure(
    connector: CrossrefConnector,
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


__all__ = ["CrossrefConnector"]
