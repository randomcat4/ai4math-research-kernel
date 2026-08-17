from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
PYTHON_SDK = ROOT / "sdk" / "python"
TS_SDK = ROOT / "sdk" / "typescript"
sys.path.insert(0, str(PYTHON_SDK))

from rk_product import lossless_json_bytes, lossless_json_loads  # noqa: E402


def test_python_typescript_python_lossless_round_trip() -> None:
    subprocess.run(
        ["npm", "--prefix", str(TS_SDK), "run", "build"],
        capture_output=True,
        check=True,
    )
    value = {
        "schema_version": "rk.product.query.v1",
        "scope": {
            "kind": "RUN",
            "run_id": "c73f6387-2ea0-487a-aebf-dd2b8dad8ec2",
            "expected_revision": 17,
            "expected_contract_version": 3,
        },
        "query": {
            "type": "GRAPH_SLICE",
            "payload": {"seed_ids": ["希腊字母", "命题-1"], "depth": 2, "filters": {}},
        },
    }
    source = lossless_json_bytes(value)
    module_uri = (TS_SDK / "dist" / "roundtrip.js").as_uri()
    script = (
        f'import {{roundTrip}} from {json.dumps(module_uri)};'
        "let text='';"
        "for await (const chunk of process.stdin) text += chunk;"
        "process.stdout.write(JSON.stringify(roundTrip(JSON.parse(text))));"
    )
    completed = subprocess.run(
        ["node", "--input-type=module", "--eval", script],
        input=source,
        capture_output=True,
        check=True,
    )
    assert lossless_json_loads(completed.stdout) == value
