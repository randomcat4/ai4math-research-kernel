from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from pathlib import Path
from typing import Any

import pytest
from jsonschema import Draft202012Validator

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "sdk/python"))

from rk_product import (  # noqa: E402
    GlobalScope,
    InvalidEnvelopeError,
    QueryDeploymentScope,
    QueryRunScope,
    ResearchProductClient,
    UnknownVariantError,
)
from rk_product.types import QUERY_CONTRACTS  # noqa: E402

UUID = "7f857a15-bddb-4238-aa88-6dbeaec50f7a"
PRODUCT = ROOT / "docs/spec/product"


def test_query_metadata_covers_all_56_types_and_64_scope_branches() -> None:
    catalog = json.loads((PRODUCT / "catalog.json").read_text(encoding="utf-8"))
    schema = json.loads((PRODUCT / "query.schema.json").read_text(encoding="utf-8"))
    assert set(QUERY_CONTRACTS) == set(catalog["query_types"])
    assert len(QUERY_CONTRACTS) == 56
    assert len(schema["$defs"]["querySpec"]["oneOf"]) == 64


def test_envelope_uses_strict_query_spec_and_accepts_query_result() -> None:
    envelope = json.loads((PRODUCT / "envelope.schema.json").read_text(encoding="utf-8"))
    Draft202012Validator.check_schema(envelope)
    assert "queryPayload" not in envelope["$defs"]
    assert {"$ref": "#/$defs/queryResult"} in envelope["oneOf"]
    assert len(envelope["$defs"]["querySpec"]["oneOf"]) == 64


def test_python_query_client_rejects_scope_missing_and_unknown_fields() -> None:
    client = ResearchProductClient(lambda operation, body: {"schema_version": "unused"})
    with pytest.raises(InvalidEnvelopeError, match="requires query scope"):
        client.query(
            scope=QueryRunScope(UUID), query_type="LIST_RESEARCH", payload={"page": {"limit": 20}}
        )
    with pytest.raises(InvalidEnvelopeError, match="missing fields"):
        client.query(scope=GlobalScope(UUID), query_type="LIST_RESEARCH", payload={})
    with pytest.raises(InvalidEnvelopeError, match="unknown fields"):
        client.query(
            scope=GlobalScope(UUID),
            query_type="LIST_RESEARCH",
            payload={"page": {"limit": 20}, "expected_revision": 4},
        )


def test_query_read_scopes_emit_only_read_fences() -> None:
    assert QueryRunScope(UUID, 7, 3).to_dict() == {
        "kind": "RUN",
        "run_id": UUID,
        "at_revision": 7,
        "at_contract_version": 3,
    }
    assert QueryDeploymentScope(UUID, 9).to_dict() == {
        "kind": "DEPLOYMENT",
        "deployment_id": UUID,
        "at_deployment_revision": 9,
    }


def test_unknown_query_result_schema_explicitly_requires_upgrade() -> None:
    def transport(_operation: str, _body: dict[str, Any]) -> dict[str, Any]:
        return {"schema_version": "rk.product.query_result.v2"}

    client = ResearchProductClient(transport)
    with pytest.raises(UnknownVariantError, match="upgrade the SDK"):
        client.query(
            scope=GlobalScope(UUID),
            query_type="LIST_RESEARCH",
            payload={"page": {"limit": 20}},
        )


def test_product_generator_is_diff_clean_for_query_outputs() -> None:
    paths = [
        PRODUCT / "query.schema.json",
        PRODUCT / "envelope.schema.json",
        ROOT / "sdk/python/rk_product/types.py",
        ROOT / "sdk/typescript/src/types.ts",
    ]

    def digest() -> str:
        value = hashlib.sha256()
        for path in paths:
            value.update(path.read_bytes())
        return value.hexdigest()

    before = digest()
    subprocess.run([sys.executable, str(ROOT / "scripts/rkgenerateproduct.py")], check=True)
    assert digest() == before


def material_result() -> dict[str, Any]:
    return {
        "schema_version": "rk.product.query_result.v1",
        "result_type": "MATERIAL",
        "stable_entity_id": UUID,
        "scope_kind": "RUN",
        "last_cursor": 7,
        "run_id": UUID,
        "research_revision": 4,
        "contract_version": 2,
        "result": {
            "entity": {
                "schema_version": "rk.product.projection.v1",
                "stable_entity_id": UUID,
                "projection_type": "MATERIAL",
                "status": "CURRENT",
                "artifact_ids": [],
                "evidence_class": "ENGINEERING_REQUIRED",
                "authority_effect": "NO_FACT_GRAPH_WRITE",
                "source_artifact_ids": [],
                "domain": {
                    "material_id": UUID,
                    "original_artifact_id": UUID,
                    "media_type": "application/pdf",
                    "ingest_state": "COMMITTED",
                    "ocr_state": "PENDING_REVIEW",
                },
            }
        },
    }


def test_python_sdk_runtime_validates_exact_result_variant_scope_and_domain() -> None:
    response = material_result()
    client = ResearchProductClient(lambda _operation, _body: response)
    assert (
        client.query(
            scope=QueryRunScope(UUID), query_type="MATERIAL", payload={"material_id": UUID}
        )
        == response
    )

    missing = material_result()
    del missing["result"]["entity"]["domain"]["ocr_state"]
    with pytest.raises(InvalidEnvelopeError, match="MATERIAL domain is missing"):
        ResearchProductClient(lambda _operation, _body: missing).query(
            scope=QueryRunScope(UUID), query_type="MATERIAL", payload={"material_id": UUID}
        )

    drift = material_result()
    drift["result"]["entity"]["private_page_state"] = True
    with pytest.raises(InvalidEnvelopeError, match="projection has unknown"):
        ResearchProductClient(lambda _operation, _body: drift).query(
            scope=QueryRunScope(UUID), query_type="MATERIAL", payload={"material_id": UUID}
        )

    unknown = material_result()
    unknown["result_type"] = "MATERIAL_V2"
    with pytest.raises(UnknownVariantError, match="upgrade the SDK"):
        ResearchProductClient(lambda _operation, _body: unknown).query(
            scope=QueryRunScope(UUID), query_type="MATERIAL", payload={"material_id": UUID}
        )


def test_typescript_runtime_rejects_unknown_query_result_variant() -> None:
    sdk = ROOT / "sdk/typescript"
    subprocess.run(["npm", "run", "build"], cwd=sdk, check=True)
    program = """
import {ResearchProductClient, UnknownVariantError} from './dist/client.js';
const transport = {
  request: async () => ({
    schema_version: 'rk.product.query_result.v1',
    result_type: 'PRIVATE_V2',
  }),
};
const client = new ResearchProductClient(transport);
try {
  await client.query({
    scope: {kind: 'RUN', run_id: '7f857a15-bddb-4238-aa88-6dbeaec50f7a'},
    type: 'MATERIAL',
    payload: {material_id: '7f857a15-bddb-4238-aa88-6dbeaec50f7a'},
  });
  process.exit(2);
} catch (error) {
  if (
    !(error instanceof UnknownVariantError)
    || !error.message.includes('upgrade the SDK')
  ) process.exit(3);
}
"""
    subprocess.run(["node", "--input-type=module", "--eval", program], cwd=sdk, check=True)
