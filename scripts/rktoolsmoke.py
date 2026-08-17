from __future__ import annotations

import hashlib
import json
import sys
from pathlib import Path
from typing import Any

from rk.adapters import AdapterProfile, RegisteredFileToolAdapter
from rk.strategy import StrategyRunner


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def profile(
    name: str, binary: Path, workspace: Path, argv_prefix: list[str]
) -> AdapterProfile:
    return AdapterProfile.from_mapping(
        {
            "name": name,
            "version": "v1",
            "source_commit": sha256_file(binary),
            "timeout_seconds": 60,
            "max_response_bytes": 8 * 1024 * 1024,
            "env_whitelist": ["PATH"],
            "argv_prefix": argv_prefix,
            "workspace_root": str(workspace),
            "binary_path": str(binary),
            "binary_sha256": sha256_file(binary),
        }
    )


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: rktoolsmoke.py DEPLOYMENT_ROOT")
    root = Path(sys.argv[1]).resolve()
    workspace = root / "toolsmoke"
    workspace.mkdir(parents=True, exist_ok=True)
    python = root / "venv/bin/python"
    z3 = root / "venv/bin/z3"
    sympy_tool = root / "app/scripts/rksympytool.py"
    enum_tool = root / "app/scripts/rkenumtool.py"
    (workspace / "check.smt2").write_text(
        "(declare-const n Int)\n(assert (not (= (+ n 0) n)))\n(check-sat)\n",
        encoding="utf-8",
    )
    (workspace / "cas.json").write_text(
        json.dumps({"operation": "expand", "expression": "(x + 1)**3", "symbol": "x"}),
        encoding="utf-8",
    )
    (workspace / "enum.json").write_text(
        json.dumps({"limit": 10, "target": 10}), encoding="utf-8"
    )
    adapters = {
        "smt": RegisteredFileToolAdapter(
            profile("z3-smt2", z3, workspace, [str(z3), "-smt2"]),
            capability_kind="SMT",
            trust_limit="HEURISTIC_EMPIRICAL_UNLESS_CERTIFICATE_REPLAYED",
            output_mode="smt-status",
        ),
        "cas": RegisteredFileToolAdapter(
            profile("sympy-cas", python, workspace, [str(python), str(sympy_tool)]),
            capability_kind="CAS",
            trust_limit="HEURISTIC_EMPIRICAL",
            output_mode="json",
        ),
        "enumeration": RegisteredFileToolAdapter(
            profile("python-enumeration", python, workspace, [str(python), str(enum_tool)]),
            capability_kind="EXACT_ENUMERATION",
            trust_limit="HARD_ONLY_AFTER_CHECKER_REPLAY",
            output_mode="json",
        ),
        "code": RegisteredFileToolAdapter(
            profile("registered-code", python, workspace, [str(python), str(enum_tool)]),
            capability_kind="CODE_EXECUTION",
            trust_limit="HEURISTIC_EMPIRICAL",
            output_mode="json",
        ),
    }
    runner = StrategyRunner(adapters)
    calls: list[dict[str, Any]] = []
    env = {"PATH": "/usr/bin:/bin"}
    enumeration_expected = {
        "count": 11,
        "witnesses": [[i, 10 - i] for i in range(11)],
    }
    for name, request in (
        (
            "smt",
            {"input_relpath": "check.smt2", "expected": "unsat", "environment": env},
        ),
        (
            "cas",
            {
                "input_relpath": "cas.json",
                "expected": {"expanded": "x**3 + 3*x**2 + 3*x + 1"},
                "environment": env,
            },
        ),
        (
            "enumeration",
            {
                "input_relpath": "enum.json",
                "expected": enumeration_expected,
                "environment": env,
            },
        ),
        (
            "code",
            {
                "input_relpath": "enum.json",
                "expected": enumeration_expected,
                "environment": env,
            },
        ),
    ):
        calls.append(runner.invoke(name, request).to_dict())
    result = {"status": "SUCCESS", "invocations": calls}
    (workspace / "rktoolsmoke.json").write_text(
        json.dumps(result, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    print(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
