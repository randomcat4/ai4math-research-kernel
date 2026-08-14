"""Strict HTTP transport and connector result contracts for literature sources."""

from __future__ import annotations

import urllib.error
import urllib.request
from dataclasses import dataclass
from enum import StrEnum
from typing import Protocol


class ConnectorStatus(StrEnum):
    SUCCESS = "SUCCESS"
    NO_HIT = "NO_HIT"
    HTTP_ERROR = "HTTP_ERROR"
    TIMEOUT = "TIMEOUT"
    NETWORK_ERROR = "NETWORK_ERROR"
    SCHEMA_DRIFT = "SCHEMA_DRIFT"


@dataclass(frozen=True, slots=True)
class HttpResponse:
    status: int
    headers: tuple[tuple[str, str], ...]
    body: bytes

    def header(self, name: str) -> str | None:
        target = name.casefold()
        for key, value in self.headers:
            if key.casefold() == target:
                return value
        return None


class TransportFailure(RuntimeError):
    def __init__(self, code: ConnectorStatus, detail: str) -> None:
        super().__init__(detail)
        self.code = code


class HttpTransport(Protocol):
    def request(
        self,
        *,
        method: str,
        endpoint: str,
        headers: tuple[tuple[str, str], ...],
        body: bytes | None,
        timeout_seconds: float,
    ) -> HttpResponse: ...


class UrllibTransport:
    def request(
        self,
        *,
        method: str,
        endpoint: str,
        headers: tuple[tuple[str, str], ...],
        body: bytes | None,
        timeout_seconds: float,
    ) -> HttpResponse:
        request = urllib.request.Request(
            endpoint, data=body, method=method, headers=dict(headers)
        )
        try:
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                return HttpResponse(
                    status=int(response.status),
                    headers=tuple((key, value) for key, value in response.headers.items()),
                    body=response.read(),
                )
        except urllib.error.HTTPError as error:
            return HttpResponse(
                status=int(error.code),
                headers=tuple((key, value) for key, value in error.headers.items()),
                body=error.read(),
            )
        except TimeoutError as error:
            raise TransportFailure(ConnectorStatus.TIMEOUT, str(error)) from error
        except urllib.error.URLError as error:
            raise TransportFailure(ConnectorStatus.NETWORK_ERROR, str(error.reason)) from error


@dataclass(frozen=True, slots=True)
class ConnectorFetch:
    connector: str
    connector_version: str
    endpoint: str
    request: dict[str, object]
    http_status: int | None
    response_media_type: str
    raw_body: bytes
    raw_kind: str
    source_visible_version: str | None
    coverage: dict[str, object]
    normalized: dict[str, object]
    status: ConnectorStatus
    error_code: str | None
    error_detail: str | None


class LiteratureConnector(Protocol):
    name: str
    version: str

    def query(
        self, request: dict[str, object], *, timeout_seconds: float
    ) -> ConnectorFetch: ...


def transport_failure_fetch(
    *,
    connector: str,
    version: str,
    endpoint: str,
    request: dict[str, object],
    failure: TransportFailure,
) -> ConnectorFetch:
    receipt = (
        '{"schema_version":"rk.transport-receipt.v1","error_code":"'
        + str(failure.code)
        + '","detail":'
        + _json_string(str(failure))
        + "}"
    ).encode()
    return ConnectorFetch(
        connector=connector,
        connector_version=version,
        endpoint=endpoint,
        request=request,
        http_status=None,
        response_media_type="application/vnd.rk.transport-receipt+json",
        raw_body=receipt,
        raw_kind="TRANSPORT_RECEIPT",
        source_visible_version=None,
        coverage={"returned": 0, "complete": False},
        normalized={"results": []},
        status=failure.code,
        error_code=str(failure.code),
        error_detail=str(failure),
    )


def _json_string(value: str) -> str:
    import json

    return json.dumps(value, ensure_ascii=False)


__all__ = [
    "ConnectorFetch",
    "ConnectorStatus",
    "HttpResponse",
    "HttpTransport",
    "LiteratureConnector",
    "TransportFailure",
    "UrllibTransport",
    "transport_failure_fetch",
]
