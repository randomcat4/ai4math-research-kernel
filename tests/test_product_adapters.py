from __future__ import annotations

import json

import pytest

from rk.product.adapters import (
    CommandJsonAdapter,
    ProductCliAdapter,
    ProductHttpCommandAdapter,
    ProductWireError,
)
from rk.product.api import (
    ArtifactOperation,
    ArtifactResult,
    EventStream,
    ProductCommand,
    ProductReceipt,
    ProductSession,
    QueryResult,
    QuerySpec,
    SubscriptionSpec,
)


class Product:
    def command(self, session: ProductSession, request: ProductCommand) -> ProductReceipt:
        assert session.principal_subject_id == "subject-1"
        return ProductReceipt(
            "receipt-1",
            1,
            request.request_id,
            request.scope,
            "PENDING",
            "2026-08-13T00:00:00Z",
            job_id="job-1",
        )

    def query(self, session: ProductSession, spec: QuerySpec) -> QueryResult:
        raise AssertionError

    def subscribe(self, session: ProductSession, spec: SubscriptionSpec) -> EventStream:
        raise AssertionError

    def artifact(self, session: ProductSession, request: ArtifactOperation) -> ArtifactResult:
        raise AssertionError


def envelope() -> dict[str, object]:
    return {
        "schema_version": "rk.product.command.v1",
        "request_id": "request-1",
        "scope": {
            "kind": "RUN",
            "run_id": "run-1",
            "expected_revision": 3,
            "expected_contract_version": 1,
        },
        "command": {"type": "START_RESEARCH", "payload": {}},
    }


def test_cli_and_http_share_exact_command_translation() -> None:
    shared = CommandJsonAdapter(Product())
    cli = ProductCliAdapter(shared)
    http = ProductHttpCommandAdapter(shared)
    session = ProductSession("session-1", "subject-1", ("cap-1",))
    value = envelope()

    cli_result = json.loads(cli.command(session, json.dumps(value)))
    http_result = http.command(session, value)  # type: ignore[arg-type]

    assert cli_result == http_result
    assert cli_result["receipt_id"] == "receipt-1"
    assert cli_result["scope"]["expected_revision"] == 3


@pytest.mark.parametrize("injected", ["actor", "role", "capability", "principal_subject_id"])
def test_raw_adapters_do_not_accept_identity_in_command_body(injected: str) -> None:
    shared = CommandJsonAdapter(Product())
    session = ProductSession("session-1", "subject-1", ("cap-1",))
    value = envelope()
    command = value["command"]
    assert isinstance(command, dict)
    payload = command["payload"]
    assert isinstance(payload, dict)
    payload[injected] = "forged"

    with pytest.raises((ProductWireError, ValueError)):
        ProductHttpCommandAdapter(shared).command(session, value)  # type: ignore[arg-type]
