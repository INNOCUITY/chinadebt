#!/usr/bin/env bash
# 每项任务创建独立 clone + 数据副本；任何一步失败即非零退出。
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PY="${FICC_PYTHON:-${FICC_VENV:-$ROOT/.venv-server}/bin/python}"
if [[ ! -x "$PY" ]]; then
  echo "找不到 $PY；先运行 bash scripts/server/bootstrap.sh 或设置 FICC_PYTHON。" >&2
  exit 2
fi
if [[ "${1:-}" == full ]]; then
  shift
  for mode in smoke check models acceptance-buyhold acceptance-rolling acceptance-extended research; do
    "$PY" "$ROOT/scripts/server/run.py" "$mode" "$@"
  done
else
  exec "$PY" "$ROOT/scripts/server/run.py" "$@"
fi
