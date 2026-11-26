#!/usr/bin/env bash
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
# pack/unpack/verify 只需 Python 标准库，Mac 无需安装服务器环境。
PY="${PYTHON_BIN:-python3}"
VENV="${FICC_VENV:-$ROOT/.venv-server}"
if [[ -z "${PYTHON_BIN:-}" && -x "$VENV/bin/python" ]]; then
  PY="$VENV/bin/python"
fi
exec "$PY" "$ROOT/scripts/server/ops.py" "$@"
