"""Poppler PDF text/layout extraction through fixed argv."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from rk.product.material_extractors.base import ExtractedMaterial, ExtractionFailure, project_text


class PdfExtractor:
    profile_id = "pdf_poppler_v1"
    material_kind = "PDF"
    parser_name = "pdftotext"

    def __init__(self) -> None:
        executable = shutil.which("pdftotext")
        self.executable: str | None = executable
        self.unavailable_reason: str | None
        if executable is None:
            self.parser_build = "pdftotext:MISSING"
            self.availability = "UNAVAILABLE"
            self.unavailable_reason = "pdftotext executable is not installed"
        else:
            completed = subprocess.run(
                (executable, "-v"), capture_output=True, text=True, check=False, timeout=10
            )
            version = (completed.stderr or completed.stdout).splitlines()[0].strip()
            self.parser_build = version
            self.availability = "AVAILABLE"
            self.unavailable_reason = None

    def extract(self, data: bytes) -> ExtractedMaterial:
        if self.executable is None:
            raise ExtractionFailure("PDF extraction profile is unavailable")
        with tempfile.TemporaryDirectory(prefix="rk-pdf-") as directory:
            source = Path(directory) / "source.pdf"
            source.write_bytes(data)
            completed = subprocess.run(
                (self.executable, "-layout", str(source), "-"),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                check=False,
                timeout=60,
            )
        if completed.returncode != 0:
            raise ExtractionFailure(
                "pdftotext failed: " + completed.stderr.decode("utf-8", errors="replace")
            )
        try:
            text = completed.stdout.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ExtractionFailure("pdftotext output is not UTF-8") from error
        return project_text(text, formula_origin="PDF_TEXT_FORMULA_CANDIDATE")


__all__ = ["PdfExtractor"]
