from pathlib import Path

from setuptools import setup

ROOT = Path(__file__).parent
RESOURCE_PATTERNS = (
    "migrations/**/*.sql",
    "migrations/**/*.json",
    "migrations/**/*.lock",
    "schema_fragments/**/*.json",
    "schema_fragments/**/*.sql",
    "docs/spec/**/*.json",
    "packaging/release-manifest.json",
)

grouped: dict[str, list[str]] = {}
for pattern in RESOURCE_PATTERNS:
    for path in ROOT.glob(pattern):
        if path.is_file():
            destination = Path("share/ai4math-research-kernel") / path.parent.relative_to(ROOT)
            grouped.setdefault(str(destination), []).append(path.relative_to(ROOT).as_posix())

setup(data_files=sorted((destination, paths) for destination, paths in grouped.items()))
