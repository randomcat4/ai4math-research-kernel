"""Tesseract image OCR with text and formula-candidate anchors."""

from __future__ import annotations

import shutil
import subprocess
import tempfile
from pathlib import Path

from rk.product.material_extractors.base import ExtractedMaterial, ExtractionFailure, project_text


class ImageExtractor:
    profile_id = "image_tesseract_v1"
    material_kind = "IMAGE"
    parser_name = "tesseract"

    def __init__(self) -> None:
        executable = shutil.which("tesseract")
        self.executable: str | None = executable
        self.unavailable_reason: str | None
        if executable is None:
            self.parser_build = "tesseract:MISSING"
            self.availability = "UNAVAILABLE"
            self.unavailable_reason = "tesseract executable is not installed"
        else:
            completed = subprocess.run(
                (executable, "--version"), capture_output=True, text=True, check=False, timeout=10
            )
            self.parser_build = completed.stdout.splitlines()[0].strip()
            self.availability = "AVAILABLE"
            self.unavailable_reason = None

    def extract(self, data: bytes) -> ExtractedMaterial:
        if self.executable is None:
            raise ExtractionFailure("image OCR profile is unavailable")
        with tempfile.TemporaryDirectory(prefix="rk-image-") as directory:
            source = Path(directory) / "source.png"
            source.write_bytes(data)
            completed = subprocess.run(
                (self.executable, str(source), "stdout", "--psm", "6"),
                stdin=subprocess.DEVNULL,
                capture_output=True,
                check=False,
                timeout=60,
            )
        if completed.returncode != 0:
            raise ExtractionFailure(
                "tesseract failed: " + completed.stderr.decode("utf-8", errors="replace")
            )
        try:
            text = completed.stdout.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ExtractionFailure("tesseract output is not UTF-8") from error
        projected = project_text(text, formula_origin="OCR_FORMULA_CANDIDATE")
        if not projected.formulas and text.strip():
            formula = {
                "page": 1,
                "line": 1,
                "source": " ".join(text.split()),
                "origin": "OCR_FORMULA_CANDIDATE",
            }
            return ExtractedMaterial(projected.text, projected.layout, (formula,))
        return projected


__all__ = ["ImageExtractor"]
