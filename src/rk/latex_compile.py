"""Fail-closed LaTeX subprocess boundary used by every paper compiler."""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True, slots=True)
class LatexResult:
    returncode: int
    stdout: bytes
    stderr: bytes
    pdf: bytes | None
    timed_out: bool = False


def compile_latex(
    root: Path,
    *,
    executable: str,
    timeout_seconds: float,
    max_log_bytes: int = 1 << 20,
    max_pdf_bytes: int = 64 << 20,
) -> LatexResult:
    resolved = shutil.which(executable)
    if resolved is None:
        raise FileNotFoundError(executable)
    stdout_path, stderr_path = root / "stdout.log", root / "stderr.log"
    environment = {
        "PATH": str(Path(resolved).parent),
        "HOME": str(root),
        "TEXMFOUTPUT": str(root),
        "openin_any": "p",
        "openout_any": "p",
    }
    if os.name == "nt" and "SYSTEMROOT" in os.environ:
        environment["SYSTEMROOT"] = os.environ["SYSTEMROOT"]
    with stdout_path.open("xb") as stdout, stderr_path.open("xb") as stderr:
        process = subprocess.Popen(
            [
                resolved,
                "-no-shell-escape",
                "-interaction=nonstopmode",
                "-halt-on-error",
                "main.tex",
            ],
            cwd=root,
            stdin=subprocess.DEVNULL,
            stdout=stdout,
            stderr=stderr,
            env=environment,
            shell=False,
        )
        try:
            returncode = process.wait(timeout=timeout_seconds)
            timed_out = False
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
            returncode = -1
            timed_out = True
    stdout_data = _bounded_read(stdout_path, max_log_bytes)
    stderr_data = _bounded_read(stderr_path, max_log_bytes)
    pdf_path = root / "main.pdf"
    pdf = _bounded_read(pdf_path, max_pdf_bytes) if pdf_path.is_file() else None
    return LatexResult(returncode, stdout_data, stderr_data, pdf, timed_out)


def _bounded_read(path: Path, limit: int) -> bytes:
    with path.open("rb") as stream:
        data = stream.read(limit + 1)
    if len(data) > limit:
        raise RuntimeError(f"compiler output exceeded {limit} bytes")
    return data


__all__ = ["LatexResult", "compile_latex"]
