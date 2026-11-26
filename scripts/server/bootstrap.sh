#!/usr/bin/env bash
# 不需要 conda/sudo；只安装到项目专属虚拟环境。
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PY="${PYTHON_BIN:-python3}"
VENV="${FICC_VENV:-$ROOT/.venv-server}"
"$PY" -c 'import sys; assert sys.version_info >= (3, 11), "需要 Python >=3.11，见 docs/04_server_guide.md"'
if [[ ! -e "$VENV" ]]; then
  "$PY" -m venv "$VENV"
fi
"$VENV/bin/python" -c 'import sys; from pathlib import Path; p=Path(sys.prefix)/"pyvenv.cfg"; assert sys.prefix != sys.base_prefix and "include-system-site-packages = true" not in p.read_text().lower(), "必须使用独立 venv"'
"$VENV/bin/python" -m pip install --upgrade pip
"$VENV/bin/python" -m pip install -e "$ROOT[dev]"
"$VENV/bin/python" -m pip check
mkdir -p "$ROOT/exports"
"$VENV/bin/python" -m pip freeze --exclude-editable > "$ROOT/exports/server-requirements.freeze.txt"
echo "环境就绪：$VENV/bin/python"
echo "下一步：bash scripts/server/server.sh doctor"
