from __future__ import annotations

import json
import sys
from pathlib import Path


def main() -> int:
    if len(sys.argv) != 3:
        raise SystemExit("usage: rkopencodepolicy.py SOURCE_CONFIG OUTPUT_CONFIG")
    source = Path(sys.argv[1]).resolve()
    output = Path(sys.argv[2]).resolve()
    value = json.loads(source.read_text(encoding="utf-8"))
    if not isinstance(value, dict):
        raise RuntimeError("OpenCode config must be an object")
    value["permission"] = {"*": "deny"}
    value["tools"] = {"*": False}
    agents = value.setdefault("agent", {})
    if not isinstance(agents, dict):
        raise RuntimeError("OpenCode agent config must be an object")
    build = agents.setdefault("build", {})
    if not isinstance(build, dict):
        raise RuntimeError("OpenCode build agent config must be an object")
    build["permission"] = {"*": "deny"}
    build["tools"] = {"*": False}
    title = agents.setdefault("title", {})
    if not isinstance(title, dict):
        raise RuntimeError("OpenCode title agent config must be an object")
    title["disable"] = True
    value["autoupdate"] = False
    provider = value.get("provider", {}).get("deepseek-v4", {})
    options = provider.get("options", {}) if isinstance(provider, dict) else {}
    if isinstance(options, dict) and "apiKey" in options:
        options["apiKey"] = "{env:DEEPSEEK_API_KEY}"
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        json.dumps(value, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    output.chmod(0o600)
    print(json.dumps({"status": "SUCCESS", "permission": "deny_all"}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
