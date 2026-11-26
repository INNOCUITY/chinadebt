#!/usr/bin/env bash
# 显式可选；CPU 基线无需 torch。版本由手册选择并锁定，不碰系统驱动。
set -euo pipefail
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/../.." && pwd)"
PY="${FICC_PYTHON:-${FICC_VENV:-$ROOT/.venv-server}/bin/python}"
: "${TORCH_VERSION:?请设置 TORCH_VERSION，见服务器手册}"
: "${TORCH_INDEX_URL:?请设置官方 CUDA wheel 索引，例如 https://download.pytorch.org/whl/cu126}"
"$PY" -m pip install "torch==$TORCH_VERSION" --index-url "$TORCH_INDEX_URL"
"$PY" -m pip check
mkdir -p "$ROOT/exports"
"$PY" -m pip freeze --exclude-editable > "$ROOT/exports/server-requirements.freeze.txt"
"$PY" -c 'import torch; print("torch",torch.__version__,"wheel CUDA",torch.version.cuda,"CUDA available",torch.cuda.is_available(),"visible devices",torch.cuda.device_count())'
