#!/usr/bin/env bash
set -Eeuo pipefail

project=${1:?usage: rkleanbench.sh PROJECT_ROOT TOOLCHAIN_ROOT JIXIA_BIN OUTPUT_ROOT}
toolchain=${2:?usage: rkleanbench.sh PROJECT_ROOT TOOLCHAIN_ROOT JIXIA_BIN OUTPUT_ROOT}
jixia=${3:?usage: rkleanbench.sh PROJECT_ROOT TOOLCHAIN_ROOT JIXIA_BIN OUTPUT_ROOT}
output=${4:?usage: rkleanbench.sh PROJECT_ROOT TOOLCHAIN_ROOT JIXIA_BIN OUTPUT_ROOT}
mkdir -p "$output"
source_file=RKLeanE2E/Main.lean

measure() {
  local label=$1
  shift
  local timing="$output/$label.time"
  local started ended elapsed
  started=$(date +%s%N)
  set +e
  "$@" >"$output/$label.stdout" 2>"$output/$label.stderr"
  local rc=$?
  set -e
  ended=$(date +%s%N)
  elapsed=$((ended-started))
  printf '{"wall_ms":%s,"exit":%s}\n' "$((elapsed/1000000))" "$rc" >"$timing"
  printf '%s rc=%s %s\n' "$label" "$rc" "$(cat "$timing")"
}

cd "$project"
for round in 1 2 3; do
  measure "compile_$round" "$toolchain/bin/lake" env "$toolchain/bin/lean" \
    -o "$output/Main.$round.olean" "$source_file"

  audit="$output/Audit.$round.lean"
  cp "$source_file" "$audit"
  printf '\n#print axioms rk_add_zero\n' >>"$audit"
  measure "axioms_$round" "$toolchain/bin/lake" env "$toolchain/bin/lean" "$audit"

  jroot="$output/jixia_$round"
  mkdir -p "$jroot"
  measure "jixia_$round" "$toolchain/bin/lake" env "$jixia" -i \
    -d "$jroot/decl.json" -s "$jroot/sym.json" -e "$jroot/elab.json" \
    -l "$jroot/lines.json" "$source_file"
done

cat >"$output/Minimal.lean" <<'EOF'
import Mathlib.Data.Nat.Basic
theorem rk_minimal_add_zero (n : Nat) : n + 0 = n := Nat.add_zero n
#print axioms rk_minimal_add_zero
EOF
for round in 1 2 3; do
  measure "minimal_$round" "$toolchain/bin/lake" env "$toolchain/bin/lean" \
    "$output/Minimal.lean"
done
