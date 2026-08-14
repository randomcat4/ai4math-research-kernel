"""Narrow DeepSeek Responses controller adapter.

Only host-registered standard functions are exposed. The adapter parses call intent but never
executes tools, infers execution from prose, or grants mathematical authority.
"""

from __future__ import annotations

import re
from collections.abc import Mapping, Sequence
from types import MappingProxyType
from typing import Any

from jsonschema import Draft202012Validator
from jsonschema.exceptions import SchemaError, ValidationError

from rk.adapters.base import (
    AdapterProfile,
    AdapterRequestError,
    HttpClient,
    UrlLibHttpClient,
    load_json,
    require_exact_keys,
)

_FUNCTION_NAME = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]{0,63}$")


class DeepSeekResponsesControllerAdapter:
    """Return standard-function directives as soft, unexecuted controller intent."""

    trust_limit = "SOFT_CONTROLLER_INTENT_ONLY"

    def __init__(
        self,
        profile: AdapterProfile,
        *,
        functions: Mapping[str, Mapping[str, Any]],
        client: HttpClient | None = None,
    ) -> None:
        profile.require("endpoint")
        if not functions:
            raise AdapterRequestError("at least one host-registered function is required")
        normalized: dict[str, Mapping[str, Any]] = {}
        for name, definition in functions.items():
            if not isinstance(name, str) or _FUNCTION_NAME.fullmatch(name) is None:
                raise AdapterRequestError(f"invalid registered function name: {name!r}")
            require_exact_keys(
                definition,
                required=frozenset({"description", "parameters"}),
                label=f"registered function {name!r}",
            )
            description, parameters = definition["description"], definition["parameters"]
            if not isinstance(description, str) or not description.strip():
                raise AdapterRequestError(f"registered function {name!r} needs a description")
            if not isinstance(parameters, Mapping) or parameters.get("type") != "object":
                raise AdapterRequestError(
                    f"registered function {name!r} parameters must be an object JSON schema"
                )
            try:
                Draft202012Validator.check_schema(dict(parameters))
            except SchemaError as error:
                raise AdapterRequestError(
                    f"registered function {name!r} has an invalid JSON schema"
                ) from error
            normalized[name] = MappingProxyType(
                {"description": description.strip(), "parameters": dict(parameters)}
            )
        self.profile = profile
        self.functions = MappingProxyType(normalized)
        self.client = client or UrlLibHttpClient()
        self.name = profile.name
        self.version = profile.version

    def run(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        require_exact_keys(
            request,
            required=frozenset({"prompt", "model", "max_output_tokens", "environment"}),
            label="DeepSeek Responses controller request",
        )
        prompt, model = request["prompt"], request["model"]
        max_output_tokens = request["max_output_tokens"]
        if (
            not isinstance(prompt, str)
            or not prompt
            or len(prompt.encode("utf-8")) > 64 * 1024
        ):
            raise AdapterRequestError("prompt must be non-empty and at most 64 KiB")
        if not isinstance(model, str) or not model:
            raise AdapterRequestError("model must be non-empty")
        if (
            not isinstance(max_output_tokens, int)
            or isinstance(max_output_tokens, bool)
            or not 1 <= max_output_tokens <= 32768
        ):
            raise AdapterRequestError("max_output_tokens is outside the registered limit")
        environment = request["environment"]
        if not isinstance(environment, Mapping):
            raise AdapterRequestError("environment must be an object")
        env = self.profile.select_environment(environment)
        api_key = env.get("DEEPSEEK_API_KEY")
        if not api_key:
            raise AdapterRequestError("registered DEEPSEEK_API_KEY is missing")

        tools = [
            {
                "type": "function",
                "name": name,
                "description": definition["description"],
                "parameters": definition["parameters"],
            }
            for name, definition in self.functions.items()
        ]
        endpoint = self.profile.endpoint
        assert endpoint is not None
        provider_payload: dict[str, Any] = {
            "model": model,
            "input": prompt,
            "max_output_tokens": max_output_tokens,
            "tools": tools,
            "tool_choice": "auto",
            "_rk_authorization_bearer": api_key,
        }
        response = self.client.post_json(
            endpoint,
            provider_payload,
            timeout=self.profile.timeout_seconds,
            max_response_bytes=self.profile.max_response_bytes,
        )
        common = {
            **self.profile.provenance(),
            "trust_limit": self.trust_limit,
            "evidence_type": "CONTROLLER_INTENT",
            "evidence_strength": "SOFT_MODEL",
            "machine_axis_effect": "UNCHANGED",
            "http_status": response.status_code,
            "tool_surface": "REGISTERED_STANDARD_FUNCTIONS_ONLY",
        }
        if response.status_code != 200:
            return {**common, "status": "FAILED", "payload": None}
        try:
            value = load_json(response.body)
            if not isinstance(value, Mapping):
                raise ValueError("response is not an object")
            if value.get("status") != "completed" or value.get("incomplete_details") is not None:
                raise ValueError("response is incomplete")
            response_id, output = value.get("id"), value.get("output")
            if not isinstance(response_id, str) or not response_id:
                raise ValueError("response id missing")
            if not isinstance(output, Sequence) or isinstance(output, (str, bytes)):
                raise ValueError("output missing")
            directives: list[dict[str, Any]] = []
            messages: list[str] = []
            call_ids: set[str] = set()
            for item in output:
                if not isinstance(item, Mapping):
                    raise ValueError("output item is not an object")
                item_type = item.get("type")
                if item_type == "function_call":
                    if item.get("status") != "completed":
                        raise ValueError("function call is incomplete")
                    call_id = item.get("call_id")
                    function_name = item.get("name")
                    arguments_raw = item.get("arguments")
                    if (
                        not isinstance(call_id, str)
                        or not call_id
                        or call_id in call_ids
                        or function_name not in self.functions
                        or not isinstance(arguments_raw, str)
                    ):
                        raise ValueError("invalid or unregistered function call")
                    arguments = load_json(arguments_raw)
                    if not isinstance(arguments, Mapping):
                        raise ValueError("function arguments must be an object")
                    try:
                        Draft202012Validator(
                            self.functions[str(function_name)]["parameters"]
                        ).validate(arguments)
                    except ValidationError as error:
                        raise ValueError("function arguments do not match schema") from error
                    call_ids.add(call_id)
                    directives.append(
                        {"call_id": call_id, "name": function_name, "arguments": dict(arguments)}
                    )
                elif item_type == "message":
                    if item.get("status") != "completed":
                        raise ValueError("message is incomplete")
                    content = item.get("content")
                    if not isinstance(content, Sequence) or isinstance(content, (str, bytes)):
                        raise ValueError("message content missing")
                    for part in content:
                        if isinstance(part, Mapping) and part.get("type") == "output_text":
                            text = part.get("text")
                            if isinstance(text, str) and text.strip():
                                messages.append(text.strip())
                elif item_type != "reasoning":
                    raise ValueError("unsupported output item type")
            usage_raw = value.get("usage")
            usage_raw = usage_raw if isinstance(usage_raw, Mapping) else {}
            output_details = usage_raw.get("output_tokens_details")
            output_details = output_details if isinstance(output_details, Mapping) else {}
            usage = {
                "input_tokens": int(usage_raw.get("input_tokens", 0)),
                "output_tokens": int(usage_raw.get("output_tokens", 0)),
                "reasoning_tokens": int(output_details.get("reasoning_tokens", 0)),
                "total_tokens": int(usage_raw.get("total_tokens", 0)),
            }
            if not directives and not messages:
                raise ValueError("response contains neither function calls nor text")
        except (UnicodeDecodeError, ValueError, TypeError, KeyError):
            return {**common, "status": "ADAPTER_SCHEMA_MISMATCH", "payload": None}

        return {
            **common,
            "status": "COMPLETED",
            "payload": {
                "response_id": response_id,
                "response_model": (
                    value.get("model") if isinstance(value.get("model"), str) else None
                ),
                "directives": directives,
                "text": "\n".join(messages),
                "execution_claimed": False,
            },
            "usage": usage,
            "provider_request": {
                "model": model,
                "max_output_tokens": max_output_tokens,
                "function_names": list(self.functions),
                "continuation": False,
            },
        }
