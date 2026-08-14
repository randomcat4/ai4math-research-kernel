"""Build a deterministic, hash-indexed service-contract release archive."""

from __future__ import annotations

import argparse
import hashlib
import json
import zipfile
from pathlib import Path


def build(output: Path) -> Path:
    root = Path(__file__).resolve().parent
    manifest_path = root / "release-manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    payloads = ("release-manifest.json", *manifest["payloads"])
    digests: dict[str, str] = {}
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for relative in payloads:
            path = root / relative
            data = path.read_bytes()
            digests[relative] = hashlib.sha256(data).hexdigest()
            info = zipfile.ZipInfo(relative, date_time=(2026, 8, 14, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o644 << 16
            archive.writestr(info, data)
        digest_data = json.dumps(
            {"schema_version": "rk.packaging.sha256.v1", "files": digests},
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        info = zipfile.ZipInfo("SHA256SUMS.json", date_time=(2026, 8, 14, 0, 0, 0))
        info.compress_type = zipfile.ZIP_DEFLATED
        info.external_attr = 0o644 << 16
        archive.writestr(info, digest_data)
    return output


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    build(arguments.output)


if __name__ == "__main__":
    main()
