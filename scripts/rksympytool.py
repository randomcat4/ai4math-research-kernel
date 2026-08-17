from __future__ import annotations

import json
import sys
from importlib import import_module
from pathlib import Path

sympy = import_module("sympy")


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: rksympytool.py INPUT_JSON")
    value = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    if not isinstance(value, dict) or set(value) != {"operation", "expression", "symbol"}:
        raise RuntimeError("invalid CAS request")
    if value["operation"] != "expand" or value["symbol"] != "x":
        raise RuntimeError("operation is not registered")
    x = sympy.Symbol("x")
    expression = sympy.sympify(value["expression"], locals={"x": x}, evaluate=False)
    print(json.dumps({"expanded": str(sympy.expand(expression))}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
