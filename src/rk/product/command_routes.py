"""HTTP routes for the single product command operation family."""

from __future__ import annotations

import asyncio
import json
import re
from collections.abc import Mapping, Sequence
from typing import Any, cast
from urllib.parse import urlsplit

from rk.domain import CapabilityError
from rk.http_shell import (
    HttpErrorClass,
    HttpResponse,
    JsonValue,
    ProductHttpError,
    RouteSpec,
    SessionPrincipal,
    SessionRequest,
    error_response,
)
from rk.product.adapters import ProductHttpCommandAdapter, ProductWireError
from rk.product.api import ProductSession
from rk.product.operations import OperationConflict

_RUN_COMMAND_PATH = re.compile(r"^/v1/research/([A-Za-z0-9_.:-]+)/commands$")


class CommandRouter:
    """Decode fixed HTTP paths into the one shared command adapter."""

    def __init__(self, adapter: ProductHttpCommandAdapter, *, deployment_id: str) -> None:
        if not deployment_id:
            raise ValueError("deployment_id must be non-empty")
        self._adapter = adapter
        self._deployment_id = deployment_id
        self._routes = (
            RouteSpec("POST", "/v1/research", self.create_research, "research-create-command"),
            RouteSpec(
                "POST",
                "/v1/research/{run_id}/commands",
                self.run_command,
                "run-product-command",
            ),
            RouteSpec(
                "POST",
                "/v1/deployment/operations",
                self.deployment_command,
                "deployment-product-command",
            ),
        )

    def routes(self) -> Sequence[RouteSpec]:
        return self._routes

    async def create_research(self, request: SessionRequest) -> HttpResponse:
        try:
            body = _command_body(request)
        except ProductWireError:
            return _problem("COMMAND_ENVELOPE_INVALID", HttpErrorClass.SCHEMA, "$")
        command = body["command"]
        scope = body["scope"]
        if (
            not isinstance(command, dict)
            or command.get("type") != "CREATE_RESEARCH"
            or not isinstance(scope, dict)
            or scope.get("kind") != "GLOBAL"
            or scope.get("deployment_id") != self._deployment_id
        ):
            return _problem("COMMAND_PATH_SCOPE_MISMATCH", HttpErrorClass.AUTHORIZATION, "$.scope")
        return await self._execute(request.principal, body)

    async def run_command(self, request: SessionRequest) -> HttpResponse:
        split = urlsplit(request.request.path)
        match = _RUN_COMMAND_PATH.fullmatch(split.path)
        if match is None or split.query or split.fragment:
            return _problem("RUN_COMMAND_PATH_INVALID", HttpErrorClass.SCHEMA, "$.path")
        try:
            body = _command_body(request)
        except ProductWireError:
            return _problem("COMMAND_ENVELOPE_INVALID", HttpErrorClass.SCHEMA, "$")
        scope = body["scope"]
        if (
            not isinstance(scope, dict)
            or scope.get("kind") != "RUN"
            or scope.get("run_id") != match.group(1)
        ):
            return _problem("COMMAND_PATH_SCOPE_MISMATCH", HttpErrorClass.AUTHORIZATION, "$.scope")
        return await self._execute(request.principal, body)

    async def deployment_command(self, request: SessionRequest) -> HttpResponse:
        split = urlsplit(request.request.path)
        if split.path != "/v1/deployment/operations" or split.query or split.fragment:
            return _problem("DEPLOYMENT_COMMAND_PATH_INVALID", HttpErrorClass.SCHEMA, "$.path")
        try:
            body = _command_body(request)
        except ProductWireError:
            return _problem("COMMAND_ENVELOPE_INVALID", HttpErrorClass.SCHEMA, "$")
        scope = body["scope"]
        if (
            not isinstance(scope, dict)
            or scope.get("kind") not in {"GLOBAL", "DEPLOYMENT"}
            or scope.get("deployment_id") != self._deployment_id
        ):
            return _problem("COMMAND_PATH_SCOPE_MISMATCH", HttpErrorClass.AUTHORIZATION, "$.scope")
        return await self._execute(request.principal, body)

    async def _execute(
        self, principal: SessionPrincipal, body: Mapping[str, JsonValue]
    ) -> HttpResponse:
        if not principal.session_id or not principal.subject_id or not principal.capability_ids:
            return _problem("COMMAND_SESSION_INVALID", HttpErrorClass.AUTHENTICATION, "$.session")
        session = ProductSession(
            principal.session_id, principal.subject_id, principal.capability_ids
        )
        try:
            result = await asyncio.to_thread(self._adapter.command, session, body)
        except ProductWireError:
            return _problem("COMMAND_ENVELOPE_INVALID", HttpErrorClass.SCHEMA, "$")
        except (CapabilityError, PermissionError):
            return _problem("COMMAND_FORBIDDEN", HttpErrorClass.AUTHORIZATION, "$.session")
        except OperationConflict:
            return _problem("COMMAND_CONFLICT", HttpErrorClass.CONFLICT, "$.request_id")
        except ValueError:
            return _problem(
                "COMMAND_VARIANT_UNAVAILABLE",
                HttpErrorClass.UNAVAILABLE,
                "$.command.type",
            )
        return HttpResponse(200, result)


def command_router_factory(
    *, adapter: ProductHttpCommandAdapter, deployment_id: str
) -> CommandRouter:
    return CommandRouter(adapter, deployment_id=deployment_id)


class _DuplicateJsonKey(ValueError):
    pass


def _command_body(request: SessionRequest) -> dict[str, JsonValue]:
    content_type = _header(request.request.headers, "content-type")
    if (
        content_type is None
        or content_type.partition(";")[0].strip().casefold() != "application/json"
    ):
        raise ProductWireError("JSON content type is required")

    def reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise _DuplicateJsonKey(key)
            result[key] = value
        return result

    try:
        value = json.loads(
            request.request.body.decode("utf-8"), object_pairs_hook=reject_duplicates
        )
    except (UnicodeDecodeError, json.JSONDecodeError, _DuplicateJsonKey) as error:
        raise ProductWireError("command body is invalid JSON") from error
    if not isinstance(value, dict):
        raise ProductWireError("command body must be an object")
    return cast(dict[str, JsonValue], value)


def _header(headers: Mapping[str, str], name: str) -> str | None:
    lowered = name.casefold()
    return next((value for key, value in headers.items() if key.casefold() == lowered), None)


def _problem(code: str, error_class: HttpErrorClass, path: str) -> HttpResponse:
    return error_response(ProductHttpError(code=code, error_class=error_class, path=path))


__all__ = ["CommandRouter", "command_router_factory"]
