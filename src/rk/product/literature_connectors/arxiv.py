"""arXiv search, exact-version document, and metadata-context connector."""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from urllib.parse import urlencode

from rk.product.literature_connectors.base import (
    ConnectorFetch,
    ConnectorStatus,
    HttpTransport,
    TransportFailure,
    transport_failure_fetch,
)

_ARXIV_ID = re.compile(r"^(?:[a-z-]+(?:\.[A-Z]{2})?/\d{7}|\d{4}\.\d{4,5})$")
_ATOM = {"atom": "http://www.w3.org/2005/Atom"}


class ArxivConnector:
    name = "ARXIV"
    version = "arxiv-api-v1"

    def __init__(
        self,
        transport: HttpTransport,
        *,
        api_endpoint: str = "https://export.arxiv.org/api/query",
        document_endpoint: str = "https://arxiv.org/pdf",
    ) -> None:
        self._transport = transport
        self._api = api_endpoint
        self._documents = document_endpoint

    def query(
        self, request: dict[str, object], *, timeout_seconds: float
    ) -> ConnectorFetch:
        kind = request.get("kind")
        if kind == "SEARCH":
            return self._search(request, timeout_seconds)
        if kind == "CONTEXT":
            return self._context(request, timeout_seconds)
        if kind == "DOCUMENT":
            return self._document(request, timeout_seconds)
        raise ValueError("arXiv request kind is unsupported")

    def _search(
        self, request: dict[str, object], timeout: float
    ) -> ConnectorFetch:
        if set(request) != {"kind", "query", "max_results"}:
            raise ValueError("arXiv search fields are not exact")
        query, count = request["query"], request["max_results"]
        if (
            not isinstance(query, str)
            or not query.strip()
            or not isinstance(count, int)
            or isinstance(count, bool)
            or count < 1
            or count > 100
        ):
            raise ValueError("arXiv search is invalid")
        exact = {"kind": "SEARCH", "query": query.strip(), "max_results": count}
        endpoint = self._api + "?" + urlencode(
            {"search_query": f"all:{query.strip()}", "start": 0, "max_results": count}
        )
        return self._fetch_atom(exact, endpoint, timeout)

    def _context(
        self, request: dict[str, object], timeout: float
    ) -> ConnectorFetch:
        arxiv_id, version = _exact_version_request(request, "CONTEXT")
        exact = {"kind": "CONTEXT", "arxiv_id": arxiv_id, "version": version}
        endpoint = self._api + "?" + urlencode({"id_list": f"{arxiv_id}v{version}"})
        return self._fetch_atom(exact, endpoint, timeout)

    def _document(
        self, request: dict[str, object], timeout: float
    ) -> ConnectorFetch:
        arxiv_id, version = _exact_version_request(request, "DOCUMENT")
        exact = {"kind": "DOCUMENT", "arxiv_id": arxiv_id, "version": version}
        endpoint = f"{self._documents}/{arxiv_id}v{version}"
        try:
            response = self._transport.request(
                method="GET",
                endpoint=endpoint,
                headers=(("Accept", "application/pdf"), ("User-Agent", "rk-product/1.1")),
                body=None,
                timeout_seconds=timeout,
            )
        except TransportFailure as failure:
            return transport_failure_fetch(
                connector=self.name,
                version=self.version,
                endpoint=endpoint,
                request=exact,
                failure=failure,
            )
        media = response.header("content-type") or "application/octet-stream"
        if response.status != 200:
            return _failure(self, exact, endpoint, response.status, media, response.body)
        is_pdf = media.partition(";")[0].strip().lower() == "application/pdf"
        if not is_pdf or not response.body.startswith(b"%PDF-"):
            return _failure(
                self,
                exact,
                endpoint,
                response.status,
                media,
                response.body,
                "SCHEMA_DRIFT",
                "arXiv document is not PDF",
            )
        return ConnectorFetch(
            self.name,
            self.version,
            endpoint,
            exact,
            response.status,
            "application/pdf",
            response.body,
            "WIRE_RESPONSE",
            f"{arxiv_id}v{version}",
            {"returned": 1, "complete": True, "document_bytes": len(response.body)},
            {
                "results": [
                    {
                        "arxiv_id": arxiv_id,
                        "version": version,
                        "candidate_kind": "SOURCE_DOCUMENT",
                    }
                ]
            },
            ConnectorStatus.SUCCESS,
            None,
            None,
        )

    def _fetch_atom(
        self, request: dict[str, object], endpoint: str, timeout: float
    ) -> ConnectorFetch:
        try:
            response = self._transport.request(
                method="GET",
                endpoint=endpoint,
                headers=(
                    ("Accept", "application/atom+xml"),
                    ("User-Agent", "rk-product/1.1 (mailto:research@example.invalid)"),
                ),
                body=None,
                timeout_seconds=timeout,
            )
        except TransportFailure as failure:
            return transport_failure_fetch(
                connector=self.name,
                version=self.version,
                endpoint=endpoint,
                request=request,
                failure=failure,
            )
        if response.status != 200:
            return _failure(
                self,
                request,
                endpoint,
                response.status,
                "application/atom+xml",
                response.body,
            )
        try:
            root = ET.fromstring(response.body)
        except ET.ParseError as error:
            return _failure(
                self,
                request,
                endpoint,
                response.status,
                "application/atom+xml",
                response.body,
                "SCHEMA_DRIFT",
                str(error),
            )
        results = []
        for entry in root.findall("atom:entry", _ATOM):
            identifier = _text(entry, "atom:id")
            title = _text(entry, "atom:title")
            summary = _text(entry, "atom:summary")
            if not identifier or not title or summary is None:
                return _failure(
                    self,
                    request,
                    endpoint,
                    response.status,
                    "application/atom+xml",
                    response.body,
                    "SCHEMA_DRIFT",
                    "arXiv entry fields drifted",
                )
            authors = [
                _text(author, "atom:name") or ""
                for author in entry.findall("atom:author", _ATOM)
            ]
            results.append(
                {
                    "versioned_id": identifier.rsplit("/", 1)[-1],
                    "title": " ".join(title.split()),
                    "summary": " ".join(summary.split()),
                    "authors": authors,
                    "published": _text(entry, "atom:published"),
                    "updated": _text(entry, "atom:updated"),
                }
            )
        state = ConnectorStatus.SUCCESS if results else ConnectorStatus.NO_HIT
        visible_version = str(results[0]["versioned_id"]) if len(results) == 1 else None
        return ConnectorFetch(
            self.name,
            self.version,
            endpoint,
            request,
            response.status,
            "application/atom+xml",
            response.body,
            "WIRE_RESPONSE",
            visible_version,
            {"returned": len(results), "complete": request["kind"] == "CONTEXT"},
            {"results": results, "candidate_kind": "SOURCE_CONTEXT"},
            state,
            None,
            None,
        )


def _exact_version_request(
    request: dict[str, object], kind: str
) -> tuple[str, int]:
    if set(request) != {"kind", "arxiv_id", "version"} or request.get("kind") != kind:
        raise ValueError(f"arXiv {kind.lower()} fields are not exact")
    arxiv_id, version = request["arxiv_id"], request["version"]
    if (
        not isinstance(arxiv_id, str)
        or _ARXIV_ID.fullmatch(arxiv_id) is None
        or not isinstance(version, int)
        or isinstance(version, bool)
        or version < 1
    ):
        raise ValueError("arXiv ID/version is invalid")
    return arxiv_id, version


def _text(element: ET.Element, path: str) -> str | None:
    child = element.find(path, _ATOM)
    return child.text if child is not None else None


def _failure(
    connector: ArxivConnector,
    request: dict[str, object],
    endpoint: str,
    status: int,
    media: str,
    body: bytes,
    code: str = "HTTP_ERROR",
    detail: str | None = None,
) -> ConnectorFetch:
    return ConnectorFetch(
        connector.name,
        connector.version,
        endpoint,
        request,
        status,
        media,
        body,
        "WIRE_RESPONSE",
        None,
        {"returned": 0, "complete": False},
        {"results": []},
        ConnectorStatus(code),
        code,
        detail or f"HTTP {status}",
    )


__all__ = ["ArxivConnector"]
