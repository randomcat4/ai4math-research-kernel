#!/usr/bin/env bash
set -eu

base=/home/ai4mathpod/rk_opencode_live
rm -rf "$base"
cp -a /home/ai4mathpod/rk_opencode_probe "$base"
rm -rf "$base/data" "$base/cache" "$base/state"
mkdir -p "$base/data" "$base/cache" "$base/state" "$base/work"
chown -R ai4mathpod:ai4mathpod "$base"
chmod 711 /root
cleanup() {
  if [ -n "${runuser_pid:-}" ]; then
    kill "$runuser_pid" 2>/dev/null || true
  fi
  pkill -u ai4mathpod -f 'opencode.*rk_opencode_live' 2>/dev/null || true
  chmod 700 /root
}
trap cleanup EXIT

export HOME="$base/home"
export XDG_CONFIG_HOME="$base/config"
export XDG_DATA_HOME="$base/data"
export XDG_CACHE_HOME="$base/cache"
export XDG_STATE_HOME="$base/state"
export OPENCODE_CONFIG="$base/opencode.json"

runuser -u ai4mathpod -p -- \
  /root/ai4math_repro_20260811/env/opencode/node_modules/opencode-linux-x64/bin/opencode \
  run --pure --print-logs --log-level DEBUG --format json \
  --model deepseek-v4/deepseek-v4-pro --dir "$base/work" \
  'Reply with exactly: OK. Do not use tools.' \
  >"$base/stdout.jsonl" 2>"$base/stderr.log" &
runuser_pid=$!
sleep 5

echo "RUNUSER_PID=$runuser_pid"
ps -eo pid,ppid,user,etime,stat,pcpu,pmem,args --forest \
  | grep -E "PID|${runuser_pid}|opencode-linux-x64/bin/opencode" \
  | grep -v grep || true
opencode_pid=$(pgrep -P "$runuser_pid" -f 'opencode.*run' | head -1 || true)
if [ -z "$opencode_pid" ]; then
  opencode_pid=$(pgrep -u ai4mathpod -f 'opencode.*run' | tail -1 || true)
fi
echo "OPENCODE_PID=$opencode_pid"
if [ -n "$opencode_pid" ]; then
  echo "WCHAN=$(cat "/proc/$opencode_pid/wchan" 2>/dev/null || true)"
  echo STATUS
  grep -E 'State|Threads|voluntary|nonvoluntary' "/proc/$opencode_pid/status" 2>/dev/null || true
  echo FDS
  ls -l "/proc/$opencode_pid/fd" 2>/dev/null | tail -40 || true
  echo SOCKETS
  ss -tpn 2>/dev/null | grep "pid=$opencode_pid" || true
  echo IO
  cat "/proc/$opencode_pid/io" 2>/dev/null || true
fi
echo LOG
tail -40 "$base/stderr.log" || true
