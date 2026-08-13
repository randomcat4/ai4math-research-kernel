from __future__ import annotations

import sys
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "sdk" / "python"))

from rk_product import (  # noqa: E402
    GlobalScope,
    InvalidEnvelopeError,
    ResearchProductClient,
    UnknownVariantError,
    UnsafeJsonValueError,
    lossless_json_bytes,
    lossless_json_loads,
)


def test_lossless_codec_preserves_unicode_and_nested_values() -> None:
    value = {"题目": "∀ x, x = x", "nested": [None, True, 7, {"status": "开放"}]}
    assert lossless_json_loads(lossless_json_bytes(value)) == value


@pytest.mark.parametrize("value", [1.5, 9_007_199_254_740_992])
def test_lossless_codec_rejects_cross_sdk_value_drift(value: Any) -> None:
    with pytest.raises(UnsafeJsonValueError):
        lossless_json_bytes({"value": value})


def test_client_exposes_only_four_transport_operations() -> None:
    calls: list[tuple[str, dict[str, Any]]] = []

    def transport(operation: str, body: dict[str, Any]) -> dict[str, Any]:
        calls.append((operation, body))
        return {"schema_version": "rk.product.query_result.v1", "data": {}}

    client = ResearchProductClient(transport)
    client.query(
        scope=GlobalScope("f1d10eee-4da4-49cf-ae78-870dff1c08ba"),
        query_type="LIST_RESEARCH",
        payload={"limit": 20},
    )
    assert [item[0] for item in calls] == ["query"]
    assert calls[0][1]["query"]["type"] == "LIST_RESEARCH"


def test_unknown_variant_requires_sdk_upgrade() -> None:
    client = ResearchProductClient(lambda operation, body: {"schema_version": "unused"})
    with pytest.raises(UnknownVariantError, match="upgrade the SDK"):
        client.query(
            scope=GlobalScope("f1d10eee-4da4-49cf-ae78-870dff1c08ba"),
            query_type="PRIVATE_PAGE_QUERY",
            payload={},
        )


def test_transport_response_requires_versioned_envelope() -> None:
    client = ResearchProductClient(lambda operation, body: {"data": {}})
    with pytest.raises(InvalidEnvelopeError):
        client.subscribe(run_id="c73f6387-2ea0-487a-aebf-dd2b8dad8ec2", after_cursor=0)


@pytest.mark.parametrize(
    "payload",
    [
        {"actor": "forged"},
        {"nested": {"role": "ADMIN"}},
        {"items": [{"capability_id": "forged"}]},
        {"principal_subject_id": "forged"},
    ],
)
def test_identity_fields_cannot_hide_inside_payload(payload: dict[str, Any]) -> None:
    client = ResearchProductClient(lambda operation, body: {"schema_version": "unused"})
    with pytest.raises(InvalidEnvelopeError, match="identity fields come from Session"):
        client.command(
            request_id="7f857a15-bddb-4238-aa88-6dbeaec50f7a",
            scope=GlobalScope("f1d10eee-4da4-49cf-ae78-870dff1c08ba"),
            command_type="CREATE_RESEARCH",
            payload=payload,
        )
