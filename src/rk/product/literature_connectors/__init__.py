"""Strict literature connector clients; connector hits are source candidates only."""

from rk.product.literature_connectors.arxiv import ArxivConnector
from rk.product.literature_connectors.base import (
    ConnectorFetch,
    ConnectorStatus,
    HttpResponse,
    HttpTransport,
    LiteratureConnector,
    TransportFailure,
    UrllibTransport,
)
from rk.product.literature_connectors.crossref import CrossrefConnector
from rk.product.literature_connectors.matlas import MatlasConnector
from rk.product.literature_connectors.openalex import OpenAlexConnector

__all__ = [
    "ArxivConnector",
    "ConnectorFetch",
    "ConnectorStatus",
    "CrossrefConnector",
    "HttpResponse",
    "HttpTransport",
    "LiteratureConnector",
    "MatlasConnector",
    "OpenAlexConnector",
    "TransportFailure",
    "UrllibTransport",
]
