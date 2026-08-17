from __future__ import annotations

import json
import re
import sys
from pathlib import Path

LEAN_BLOCK = re.compile(r"```(?:lean4|lean)\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)


def main() -> int:
    if len(sys.argv) != 2:
        raise SystemExit("usage: rkverifyprover.py BENCHMARK_OUTPUT")
    root = Path(sys.argv[1]).resolve()
    extracted: list[dict[str, object]] = []
    for response in sorted(root.glob("*.txt")):
        blocks = LEAN_BLOCK.findall(response.read_text(encoding="utf-8"))
        target = root / f"{response.stem}.lean"
        if blocks:
            source = max(blocks, key=len).strip() + "\n"
            statement_path = root / f"{response.stem}.statement.lean"
            if statement_path.is_file() and not source.lstrip().startswith("import "):
                statement = statement_path.read_text(encoding="utf-8")
                theorem_at = statement.find("theorem ")
                if theorem_at < 0:
                    raise RuntimeError(f"statement has no theorem: {statement_path}")
                source = statement[:theorem_at] + source
            target.write_text(source, encoding="utf-8")
            pattern = (
                r"(?<![A-Za-z0-9_'])(?:sorry|admit|axiom|unsafe|native_decide)"
                r"(?![A-Za-z0-9_'])"
            )
            forbidden = sorted(set(re.findall(pattern, source)))
            extracted.append(
                {"id": response.stem, "extracted": True, "forbidden": forbidden}
            )
        else:
            extracted.append({"id": response.stem, "extracted": False, "forbidden": []})
    (root / "extraction.json").write_text(
        json.dumps(extracted, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
    )
    print(json.dumps(extracted, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
