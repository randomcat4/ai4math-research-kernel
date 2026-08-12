#!/usr/bin/env bash
set -Eeuo pipefail

binary=${1:?usage: rkopencodeprobe.sh OPENCODE_BIN SOURCE_CONFIG OUTPUT_ROOT}
source_config=${2:?usage: rkopencodeprobe.sh OPENCODE_BIN SOURCE_CONFIG OUTPUT_ROOT}
output_root=${3:?usage: rkopencodeprobe.sh OPENCODE_BIN SOURCE_CONFIG OUTPUT_ROOT}
mkdir -p "${output_root}"

make_config() {
  local mode=$1
  local target=$2
  python3 - "${source_config}" "${target}" "${mode}" <<'PY'
import json
import sys
from pathlib import Path

source, target, mode = map(Path, sys.argv[1:])
value = json.loads(source.read_text(encoding="utf-8"))
if mode.name == "allow":
    value["permission"] = "allow"
elif mode.name == "wildcard":
    value["permission"] = {"*": "deny"}
elif mode.name == "scalar":
    value["permission"] = "deny"
elif mode.name == "explicit":
    denied = {
        "read": "deny", "edit": "deny", "glob": "deny", "grep": "deny",
        "list": "deny", "bash": "deny", "task": "deny", "skill": "deny",
        "lsp": "deny", "question": "deny", "webfetch": "deny",
        "websearch": "deny", "external_directory": "deny", "doom_loop": "deny",
        "todowrite": "deny", "todoread": "deny",
    }
    value["permission"] = denied
    value["agent"] = {"build": {"permission": denied}}
else:
    raise SystemExit(f"unknown mode: {mode.name}")
target.write_text(json.dumps(value, sort_keys=True) + "\n", encoding="utf-8")
target.chmod(0o600)
PY
}

for mode in allow wildcard scalar explicit; do
  case_root="${output_root}/${mode}"
  rm -rf "${case_root}"
  mkdir -p "${case_root}/home" "${case_root}/work"
  config="${case_root}/opencode.json"
  make_config "${mode}" "${config}"
  started=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  set +e
  timeout --signal=TERM --kill-after=5s 75s env \
    HOME="${case_root}/home" \
    XDG_CONFIG_HOME="${case_root}/home/.config" \
    XDG_DATA_HOME="${case_root}/home/.local/share" \
    XDG_CACHE_HOME="${case_root}/home/.cache" \
    OPENCODE_CONFIG="${config}" \
    "${binary}" run --pure --print-logs --log-level DEBUG --format json \
      --model deepseek-v4/deepseek-v4-pro --dir "${case_root}/work" \
      "Reply with exactly OK. Do not call any tool." \
      >"${case_root}/stdout.jsonl" 2>"${case_root}/stderr.log"
  exit_code=$?
  set -e
  ended=$(date -u +%Y-%m-%dT%H:%M:%SZ)
  python3 - "${case_root}" "${mode}" "${started}" "${ended}" "${exit_code}" <<'PY'
import hashlib
import json
import sys
from pathlib import Path

root = Path(sys.argv[1])
stdout = (root / "stdout.jsonl").read_bytes()
stderr = (root / "stderr.log").read_text(encoding="utf-8", errors="replace")
events = []
for line in stdout.splitlines():
    try:
        value = json.loads(line)
    except ValueError:
        continue
    if isinstance(value, dict):
        events.append(value.get("type"))
summary = {
    "mode": sys.argv[2],
    "started_at": sys.argv[3],
    "ended_at": sys.argv[4],
    "exit_code": int(sys.argv[5]),
    "stdout_sha256": hashlib.sha256(stdout).hexdigest(),
    "stdout_bytes": len(stdout),
    "event_types": events,
    "stderr_tail": stderr.splitlines()[-30:],
}
(root / "summary.json").write_text(
    json.dumps(summary, ensure_ascii=False, indent=2) + "\n", encoding="utf-8"
)
print(json.dumps(summary, ensure_ascii=False))
PY
done
