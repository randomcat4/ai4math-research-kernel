"""Matlas theorem-search adapter derived from Danus under Apache-2.0.

Source attribution: FrenzyMath/Danus ``danus/integrations/matlas.py`` (Apache-2.0).
Only its unauthenticated thin-client protocol is reused. No Matlas server, corpus,
dependency graph, embedding model, or index is included or claimed by this module.
"""

from __future__ import annotations

import json

from rk.product.literature_connectors.base import (
    ConnectorFetch,
    ConnectorStatus,
    HttpTransport,
    TransportFailure,
    transport_failure_fetch,
)

_TASK = (
    "Given a math statement, retrieve useful references, such as theorems, "
    "lemmas, and definitions, that are useful for solving the given problem."
)


class MatlasConnector:
    name = "MATLAS"
    version = "rk-danus-apache2-thin-client-v1"

    def __init__(
        self,
        transport: HttpTransport,
        *,
        endpoint: str = "https://leansearch.net/thm/search",
    ) -> None:
        self._transport = transport
        self._endpoint = endpoint

    def query(
        self, request: dict[str, object], *, timeout_seconds: float
    ) -> ConnectorFetch:
        if set(request) != {"query", "num_results"}:
            raise ValueError("Matlas request fields are not exact")
        query, count = request["query"], request["num_results"]
        if (
            not isinstance(query, str)
            or not query.strip()
            or not isinstance(count, int)
            or isinstance(count, bool)
            or count < 1
            or count > 100
        ):
            raise ValueError("Matlas query or result count is invalid")
        wire_request = {"query": query.strip(), "task": _TASK, "num_results": count}
        try:
            response = self._transport.request(
                method="POST",
                endpoint=self._endpoint,
                headers=(
                    ("Content-Type", "application/json"),
                    ("Accept", "application/json"),
                    ("User-Agent", "danus/1.0 (+https://frenzymath.com)"),
                ),
                body=json.dumps(wire_request).encode(),
                timeout_seconds=timeout_seconds,
            )
        except TransportFailure as failure:
            return transport_failure_fetch(
                connector=self.name,
                version=self.version,
                endpoint=self._endpoint,
                request=wire_request,
                failure=failure,
            )
        if response.status != 200:
            return _error(self, wire_request, response.status, response.body, "HTTP_ERROR")
        try:
            value = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            return _error(
                self, wire_request, response.status, response.body, "SCHEMA_DRIFT", str(error)
            )
        if not isinstance(value, list):
            return _error(
                self,
                wire_request,
                response.status,
                response.body,
                "SCHEMA_DRIFT",
                "Matlas response is not a list",
            )
        results: list[dict[str, str]] = []
        required = {"title", "theorem", "arxiv_id", "theorem_id"}
        for item in value:
            if (
                not isinstance(item, dict)
                or not required.issubset(item)
                or any(not isinstance(item[field], str) for field in required)
            ):
                return _error(
                    self,
                    wire_request,
                    response.status,
                    response.body,
                    "SCHEMA_DRIFT",
                    "Matlas result fields drifted",
                )
            results.append({field: item[field] for field in sorted(required)})
        status = ConnectorStatus.SUCCESS if results else ConnectorStatus.NO_HIT
        return ConnectorFetch(
            connector=self.name,
            connector_version=self.version,
            endpoint=self._endpoint,
            request=wire_request,
            http_status=response.status,
            response_media_type="application/json",
            raw_body=response.body,
            raw_kind="WIRE_RESPONSE",
            source_visible_version=response.header("etag"),
            coverage={"requested": count, "returned": len(results), "complete": False},
            normalized={"results": results, "candidate_kind": "THEOREM_CANDIDATE"},
            status=status,
            error_code=None,
            error_detail=None,
        )


def _error(
    connector: MatlasConnector,
    request: dict[str, object],
    status: int,
    body: bytes,
    code: str,
    detail: str | None = None,
) -> ConnectorFetch:
    state = ConnectorStatus(code)
    return ConnectorFetch(
        connector=connector.name,
        connector_version=connector.version,
        endpoint=connector._endpoint,
        request=request,
        http_status=status,
        response_media_type="application/json",
        raw_body=body,
        raw_kind="WIRE_RESPONSE",
        source_visible_version=None,
        coverage={"returned": 0, "complete": False},
        normalized={"results": []},
        status=state,
        error_code=code,
        error_detail=detail or f"HTTP {status}",
    )


__all__ = ["MatlasConnector"]
