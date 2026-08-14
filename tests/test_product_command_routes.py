from __future__ import annotations

import asyncio
import json
from typing import cast

from rk.http_shell import HttpRequest, HttpResponse, SessionPrincipal, SessionRequest
from rk.product.adapters import CommandJsonAdapter, ProductHttpCommandAdapter
from rk.product.api import ProductCommand, ProductReceipt, ProductSession, ResearchProduct
from rk.product.command_routes import CommandRouter, command_router_factory


class NeverCalledProduct:
    def command(
        self, session: ProductSession, request: ProductCommand
    ) -> ProductReceipt:
        raise AssertionError("invalid command must not reach the product service")


def _router() -> CommandRouter:
    product = cast(ResearchProduct, NeverCalledProduct())
    return command_router_factory(
        adapter=ProductHttpCommandAdapter(CommandJsonAdapter(product)),
        deployment_id="deployment-one",
    )


def _invoke(router: CommandRouter, path: str, body: object) -> HttpResponse:
    request = HttpRequest(
        "POST",
        path,
        {"content-type": "application/json"},
        json.dumps(body, separators=(",", ":")).encode(),
    )
    result = asyncio.run(
        router.run_command(
            SessionRequest(
                request,
                SessionPrincipal("session-one", "subject-one", ("cap-one",)),
            )
        )
    )
    assert isinstance(result, HttpResponse)
    return result


def test_command_router_declares_only_fixed_operation_family_routes() -> None:
    assert [(route.method, route.path) for route in _router().routes()] == [
        ("POST", "/v1/research"),
        ("POST", "/v1/research/{run_id}/commands"),
        ("POST", "/v1/deployment/operations"),
    ]


def test_run_path_scope_mismatch_is_rejected_before_command_service() -> None:
    response = _invoke(
        _router(),
        "/v1/research/run-one/commands",
        {
            "schema_version": "rk.product.command.v1",
            "request_id": "request-one",
            "scope": {
                "kind": "RUN",
                "run_id": "run-two",
                "expected_revision": 1,
                "expected_contract_version": 1,
            },
            "command": {"type": "RUN_TOOL", "payload": {}},
        },
    )

    assert response.status == 403
    assert response.body["code"] == "COMMAND_PATH_SCOPE_MISMATCH"


def test_forged_identity_in_payload_is_rejected_by_shared_adapter() -> None:
    response = _invoke(
        _router(),
        "/v1/research/run-one/commands",
        {
            "schema_version": "rk.product.command.v1",
            "request_id": "request-one",
            "scope": {
                "kind": "RUN",
                "run_id": "run-one",
                "expected_revision": 1,
                "expected_contract_version": 1,
            },
            "command": {
                "type": "RUN_TOOL",
                "payload": {"principal_subject_id": "forged"},
            },
        },
    )

    assert response.status == 400
    assert response.body["code"] == "COMMAND_ENVELOPE_INVALID"
