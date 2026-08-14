"""Minimal OpenAI-compatible model adapter with no tool surface."""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from rk.adapters.base import (
    AdapterProfile,
    AdapterRequestError,
    HttpClient,
    UrlLibHttpClient,
    load_json,
    require_exact_keys,
)


class OpenAICompatibleAdapter:
    """Call a registered chat-completions endpoint without exposing tools or a shell."""

    trust_limit = "SOFT_MODEL"

    def __init__(self, profile: AdapterProfile, *, client: HttpClient | None = None) -> None:
        profile.require("endpoint")
        self.profile = profile
        self.client = client or UrlLibHttpClient()
        self.name = profile.name
        self.version = profile.version

    def run(self, request: Mapping[str, Any]) -> Mapping[str, Any]:
        require_exact_keys(
            request,
            required=frozenset({"prompt", "model", "max_tokens", "environment"}),
            label="OpenAI-compatible request",
        )
        prompt, model = request["prompt"], request["model"]
        environment = request["environment"]
        max_tokens = request["max_tokens"]
        if not isinstance(prompt, str) or not prompt or len(prompt.encode("utf-8")) > 64 * 1024:
            raise AdapterRequestError("prompt must be non-empty and at most 64 KiB")
        if not isinstance(model, str) or not model:
            raise AdapterRequestError("model must be non-empty")
        if not isinstance(environment, Mapping):
            raise AdapterRequestError("environment must be an object")
        env = self.profile.select_environment(environment)
        api_key = env.get("DEEPSEEK_API_KEY")
        if not api_key:
            raise AdapterRequestError("registered DEEPSEEK_API_KEY is missing")
        if (
            not isinstance(max_tokens, int)
            or isinstance(max_tokens, bool)
            or not 1 <= max_tokens <= 8192
        ):
            raise AdapterRequestError("max_tokens is outside the registered limit")
        endpoint = self.profile.endpoint
        assert endpoint is not None
        response = self.client.post_json(
            endpoint,
            {
                "model": model,
                "messages": [{"role": "user", "content": prompt}],
                "max_tokens": max_tokens,
                "thinking": {"type": "disabled"},
                "response_format": {"type": "json_object"},
                "_rk_authorization_bearer": api_key,
            },
            timeout=self.profile.timeout_seconds,
            max_response_bytes=self.profile.max_response_bytes,
        )
        common = {
            **self.profile.provenance(),
            "trust_limit": self.trust_limit,
            "evidence_type": "MODEL_JUDGE",
            "evidence_strength": "SOFT_MODEL",
            "machine_axis_effect": "UNCHANGED",
            "http_status": response.status_code,
            "tool_surface": "NONE",
        }
        if response.status_code != 200:
            return {**common, "status": "FAILED", "payload": None}
        try:
            value = load_json(response.body)
            if not isinstance(value, Mapping):
                raise ValueError("response is not an object")
            choices = value.get("choices")
            if not isinstance(choices, list) or not choices:
                raise ValueError("choices missing")
            message = choices[0].get("message")
            if not isinstance(message, Mapping):
                raise ValueError("message missing")
            text = message.get("content")
            finish_reason = choices[0].get("finish_reason")
            if not isinstance(text, str) or not text.strip():
                if finish_reason == "length":
                    return {
                        **common,
                        "status": "TOKEN_LIMIT",
                        "payload": None,
                        "usage": {
                            "input_tokens": int(value.get("usage", {}).get("prompt_tokens", 0)),
                            "output_tokens": int(
                                value.get("usage", {}).get("completion_tokens", 0)
                            ),
                            "reasoning_tokens": int(
                                value.get("usage", {})
                                .get("completion_tokens_details", {})
                                .get("reasoning_tokens", 0)
                            ),
                            "total_tokens": int(value.get("usage", {}).get("total_tokens", 0)),
                        },
                    }
                raise ValueError("content missing")
            usage_raw = value.get("usage")
            usage_raw = usage_raw if isinstance(usage_raw, Mapping) else {}
            completion_details = usage_raw.get("completion_tokens_details")
            completion_details = (
                completion_details if isinstance(completion_details, Mapping) else {}
            )
            usage = {
                "input_tokens": int(usage_raw.get("prompt_tokens", 0)),
                "output_tokens": int(usage_raw.get("completion_tokens", 0)),
                "reasoning_tokens": int(completion_details.get("reasoning_tokens", 0)),
                "total_tokens": int(usage_raw.get("total_tokens", 0)),
            }
        except (UnicodeDecodeError, ValueError, TypeError, KeyError):
            return {**common, "status": "ADAPTER_SCHEMA_MISMATCH", "payload": None}
        return {
            **common,
            "status": "COMPLETED",
            "payload": {"text": text.strip()},
            "usage": usage,
            "provider_request": {
                "model": model,
                "max_tokens": max_tokens,
                "thinking": "disabled",
                "response_format": "json_object",
                "tools": [],
            },
        }
