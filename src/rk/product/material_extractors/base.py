"""Material extractor contracts and deterministic layout/formula projections."""

from __future__ import annotations

import re
from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class ExtractedMaterial:
    text: str
    layout: tuple[dict[str, object], ...]
    formulas: tuple[dict[str, object], ...]


class ExtractionFailure(RuntimeError):
    pass


_DISPLAY_TEX = re.compile(
    r"\\\[(?P<bracket>.*?)\\\]|\\begin\{(?:equation\*?|align\*?)\}(?P<env>.*?)"
    r"\\end\{(?:equation\*?|align\*?)\}|(?<!\$)\$(?P<dollar>[^$\n]+)\$",
    re.DOTALL,
)


def project_text(text: str, *, formula_origin: str) -> ExtractedMaterial:
    layout: list[dict[str, object]] = []
    formulas: list[dict[str, object]] = []
    pages = text.split("\f")
    absolute = 0
    for page_number, page in enumerate(pages, start=1):
        for line_number, line in enumerate(page.splitlines(), start=1):
            stripped = line.strip()
            if stripped:
                layout.append(
                    {
                        "page": page_number,
                        "line": line_number,
                        "start": absolute,
                        "end": absolute + len(line),
                        "text": stripped,
                    }
                )
                if _looks_mathematical(stripped):
                    formulas.append(
                        {
                            "page": page_number,
                            "line": line_number,
                            "source": stripped,
                            "origin": formula_origin,
                        }
                    )
            absolute += len(line) + 1
        absolute += 1
    return ExtractedMaterial(text, tuple(layout), tuple(formulas))


def project_tex(text: str) -> ExtractedMaterial:
    projected = project_text(text, formula_origin="TEX_LINE_CANDIDATE")
    formulas = list(projected.formulas)
    for ordinal, match in enumerate(_DISPLAY_TEX.finditer(text), start=1):
        source = next(value for value in match.groupdict().values() if value is not None)
        formulas.append(
            {
                "ordinal": ordinal,
                "start": match.start(),
                "end": match.end(),
                "source": source.strip(),
                "origin": "TEX_EXACT",
            }
        )
    return ExtractedMaterial(text, projected.layout, tuple(formulas))


def _looks_mathematical(value: str) -> bool:
    return any(
        marker in value for marker in ("=", "≤", "≥", "∑", "∫", "√", "^", "_", "\\frac", "→")
    )


__all__ = [
    "ExtractedMaterial",
    "ExtractionFailure",
    "project_tex",
    "project_text",
]
