from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import time
from pathlib import Path

LEAN_BLOCK = re.compile(r"```(?:lean4|lean)\s*\n(.*?)```", re.DOTALL | re.IGNORECASE)
THEOREM = re.compile(r"\btheorem\s+([A-Za-z_][A-Za-z0-9_'.]*)")
FORBIDDEN = re.compile(
    r"(?<![A-Za-z0-9_'])(?:sorry|admit|sorryAx|axiom|unsafe|native_decide)"
    r"(?![A-Za-z0-9_'])"
)


def invoke(argv: list[str], *, cwd: Path, timeout: int = 180) -> dict[str, object]:
    started = time.monotonic()
    try:
        completed = subprocess.run(
            argv,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=timeout,
            check=False,
        )
        stdout = completed.stdout
        stderr = completed.stderr
        return {
            "returncode": completed.returncode,
            "wall_seconds": time.monotonic() - started,
            "stdout_sha256": hashlib.sha256(stdout.encode()).hexdigest(),
            "stderr_sha256": hashlib.sha256(stderr.encode()).hexdigest(),
            "stdout_tail": stdout[-4096:],
            "stderr_tail": stderr[-4096:],
            "_stdout": stdout,
            "_stderr": stderr,
            "timed_out": False,
        }
    except subprocess.TimeoutExpired as exc:
        return {
            "returncode": None,
            "wall_seconds": time.monotonic() - started,
            "stdout_sha256": None,
            "stderr_sha256": None,
            "stdout_tail": (exc.stdout or "")[-4096:],
            "stderr_tail": (exc.stderr or "")[-4096:],
            "_stdout": exc.stdout or "",
            "_stderr": exc.stderr or "",
            "timed_out": True,
        }


def main() -> int:
    if len(sys.argv) != 4:
        raise SystemExit("usage: rkleanverifybench.py BENCHMARK_OUTPUT PROJECT_ROOT LAKE_BINARY")
    root = Path(sys.argv[1]).resolve()
    project = Path(sys.argv[2]).resolve()
    lake = Path(sys.argv[3]).resolve()
    results: list[dict[str, object]] = []
    for response in sorted(root.glob("*.txt")):
        blocks = LEAN_BLOCK.findall(response.read_text(encoding="utf-8"))
        result: dict[str, object] = {"id": response.stem, "extracted": bool(blocks)}
        if not blocks:
            results.append(result)
            continue
        source = max(blocks, key=len).strip() + "\n"
        statement_path = root / f"{response.stem}.statement.lean"
        if statement_path.is_file() and not source.lstrip().startswith("import "):
            statement = statement_path.read_text(encoding="utf-8")
            theorem_at = statement.find("theorem ")
            if theorem_at < 0:
                raise RuntimeError(f"statement has no theorem: {statement_path}")
            source = statement[:theorem_at] + source
        target = root / f"{response.stem}.lean"
        target.write_text(source, encoding="utf-8")
        names = THEOREM.findall(source)
        forbidden = sorted(set(FORBIDDEN.findall(source)))
        result.update({"declarations": names, "forbidden": forbidden})
        if forbidden or not names:
            results.append(result)
            continue
        compile_result = invoke([str(lake), "env", "lean", str(target)], cwd=project)
        audit = root / f"{response.stem}.axioms.lean"
        audit.write_text(
            source + "\n" + "\n".join(f"#print axioms {name}" for name in names) + "\n",
            encoding="utf-8",
        )
        audit_result = invoke([str(lake), "env", "lean", str(audit)], cwd=project)
        audit_text = f"{audit_result['_stdout']}\n{audit_result['_stderr']}"
        compile_result.pop("_stdout", None)
        compile_result.pop("_stderr", None)
        audit_result.pop("_stdout", None)
        audit_result.pop("_stderr", None)
        result.update(
            {
                "compile": compile_result,
                "axiom_audit": audit_result,
                "sorry_ax": "sorryAx" in audit_text,
                "kernel_verified": (
                    compile_result["returncode"] == 0
                    and audit_result["returncode"] == 0
                    and "sorryAx" not in audit_text
                ),
            }
        )
        results.append(result)
    output = {"project_root": str(project), "lake_binary": str(lake), "results": results}
    (root / "lean_verification.json").write_text(
        json.dumps(output, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    print(json.dumps(output, ensure_ascii=False, sort_keys=True))
    return 0 if all(item.get("kernel_verified") is True for item in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
