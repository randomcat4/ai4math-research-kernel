#!/usr/bin/env bash
set -Eeuo pipefail

deployment_root=${1:?usage: rkbootstrap.sh DEPLOYMENT_ROOT}
tools_root=${RK_BOOTSTRAP_TOOLS_ROOT:-${deployment_root}/tools}
mathlib_root="${tools_root}/mathlib4-v4.28.0-rc1"
receipt="${deployment_root}/bootstrap.receipt"
log="${deployment_root}/bootstrap.log"
toolchain_bin=${RK_BOOTSTRAP_TOOLCHAIN_BIN:-${deployment_root}/env/lean_toolchains/lean-4.28.0-rc1-linux/bin}

install -d -m 700 "${tools_root}"
started_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
rm -f "${receipt}"
set +e
(
  set -Eeuo pipefail
  if [[ ! -d "${mathlib_root}/.git" ]]; then
    git clone --branch v4.28.0-rc1 --depth 1 \
      https://github.com/leanprover-community/mathlib4.git "${mathlib_root}"
  fi
  git -C "${mathlib_root}" rev-parse HEAD
  cd "${mathlib_root}"
  PATH="${toolchain_bin}:${PATH}" "${toolchain_bin}/lake" exe cache get
  PATH="${toolchain_bin}:${PATH}" "${toolchain_bin}/lake" -d "${mathlib_root}" env lean --version
) >"${log}" 2>&1
exit_code=$?
set -e
ended_at=$(date -u +%Y-%m-%dT%H:%M:%SZ)
commit=$(git -C "${mathlib_root}" rev-parse HEAD 2>/dev/null || true)
printf 'exit_code=%s\nstarted_at=%s\nended_at=%s\nmathlib_commit=%s\nlog=%s\n' \
  "${exit_code}" "${started_at}" "${ended_at}" "${commit}" "${log}" >"${receipt}"
exit "${exit_code}"
