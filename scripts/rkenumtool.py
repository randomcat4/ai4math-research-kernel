from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: rkenumtool.py INPUT_JSON")
    value = json.loads(Path(sys.argv[1]).read_text(encoding="utf-8"))
    if not isinstance(value, dict) or set(value) != {"limit", "target"}:
        raise RuntimeError("invalid enumeration request")
    limit, target = value["limit"], value["target"]
    if not isinstance(limit, int) or not isinstance(target, int) or not 0 <= limit <= 100_000:
        raise RuntimeError("enumeration bounds are invalid")
    witnesses = [[a, target - a] for a in range(limit + 1) if 0 <= target - a <= limit]
    print(json.dumps({"count": len(witnesses), "witnesses": witnesses}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
