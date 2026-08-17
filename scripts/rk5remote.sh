#!/usr/bin/env bash
set -Eeuo pipefail

deployment_root=${1:?usage: rk5remote.sh DEPLOYMENT_ROOT}
: "${RK_DEMO_OPENCODE_BIN:?set RK_DEMO_OPENCODE_BIN}"
: "${RK_DEMO_OPENCODE_CONFIG:?set RK_DEMO_OPENCODE_CONFIG}"
: "${RK_DEMO_MODEL:?set RK_DEMO_MODEL}"
receipt="${deployment_root}/rk5run.receipt"
log="${deployment_root}/rk5run.log"

started_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
rm -f "${receipt}"
set +e
"${deployment_root}/venv/bin/python" \
  "${deployment_root}/app/scripts/rk5demo.py" "${deployment_root}" >"${log}" 2>&1
exit_code=$?
set -e
ended_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
printf 'exit_code=%s\nstarted_at=%s\nended_at=%s\nlog=%s\n' \
  "${exit_code}" "${started_at}" "${ended_at}" "${log}" >"${receipt}"
exit "${exit_code}"
