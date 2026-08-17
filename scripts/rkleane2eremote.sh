#!/usr/bin/env bash
set -Eeuo pipefail

deployment_root=${1:?usage: rkleane2eremote.sh DEPLOYMENT_ROOT}
: "${RK_E2E_MATHLIB_ROOT:?set RK_E2E_MATHLIB_ROOT}"
: "${RK_E2E_TOOLCHAIN_ROOT:?set RK_E2E_TOOLCHAIN_ROOT}"
: "${RK_E2E_JIXIA_ROOT:?set RK_E2E_JIXIA_ROOT}"
: "${RK_E2E_OPENCODE_BIN:?set RK_E2E_OPENCODE_BIN}"
: "${RK_E2E_OPENCODE_CONFIG:?set RK_E2E_OPENCODE_CONFIG}"
: "${RK_E2E_OPENCODE_WORKSPACE_ROOT:?set RK_E2E_OPENCODE_WORKSPACE_ROOT}"
: "${RK_E2E_OPENCODE_USER:?set RK_E2E_OPENCODE_USER}"
: "${RK_E2E_DEEPSEEK_KEY:?set RK_E2E_DEEPSEEK_KEY}"
: "${RK_E2E_MODEL:?set RK_E2E_MODEL}"
: "${RK_E2E_RECEIPT_KEY:?set RK_E2E_RECEIPT_KEY}"
run_name=${RK_E2E_RUN_NAME:-leane2e}
if [[ ! "${run_name}" =~ ^[a-z0-9_]{1,40}$ ]]; then
  printf 'invalid RK_E2E_RUN_NAME\n' >&2
  exit 2
fi
receipt="${deployment_root}/${run_name}.receipt"
log="${deployment_root}/${run_name}.log"

started_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
rm -f "${receipt}"
set +e
export PYTHONPATH="${deployment_root}/app/src:${deployment_root}/app${PYTHONPATH:+:${PYTHONPATH}}"
"${deployment_root}/venv/bin/python" \
  "${deployment_root}/app/scripts/rkleane2e.py" "${deployment_root}" >"${log}" 2>&1
exit_code=$?
set -e
ended_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
printf 'exit_code=%s\nstarted_at=%s\nended_at=%s\nlog=%s\n' \
  "${exit_code}" "${started_at}" "${ended_at}" "${log}" >"${receipt}"
exit "${exit_code}"
