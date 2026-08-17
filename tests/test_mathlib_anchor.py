from __future__ import annotations

import ast
import json
from hashlib import sha256
from pathlib import Path

ROOT = Path(__file__).parents[1]


def test_versioned_mathlib_anchor_matches_the_e2e_pinned_digest() -> None:
    anchor = ROOT / "docs/evidence/mathlib-5352afc-closure.json"
    digest = sha256(anchor.read_bytes()).hexdigest()
    tree = ast.parse((ROOT / "scripts/rkleane2e.py").read_text(encoding="utf-8"))
    pinned = next(
        ast.literal_eval(node.value)
        for node in ast.walk(tree)
        if isinstance(node, ast.Assign)
        and any(
            isinstance(target, ast.Name)
            and target.id == "pinned_closure_manifest_sha256"
            for target in node.targets
        )
    )
    payload = json.loads(anchor.read_text(encoding="utf-8"))
    assert digest == pinned
    assert payload["provenance"] == "CLEAN_GIT_WORKTREE_OFFICIAL_MATHLIB_CACHE_GET"
    assert payload["mathlib_commit"] == "5352afccd6866369be9de43f5b7ec47203555f44"
    assert payload["toolchain"] == "leanprover/lean4:v4.28.0-rc1"
    assert payload["olean_count"] > 7_000
    assert payload["olean_count"] == len(payload["olean_files"])
    assert len(payload["dependency_closure_sha256"]) == 64
