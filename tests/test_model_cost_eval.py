"""冻结 OOS 预测的成本后评估脚本冒烟（不重新训练/不比较模型优劣）。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

import pandas as pd


def test_model_cost_eval_quick(tmp_path) -> None:
    out = tmp_path / "cost"
    proc = subprocess.run(
        [
            sys.executable,
            "scripts/run_model_cost_eval.py",
            "--max-splits",
            "1",
            "--models",
            "ridge,tree",
            "--out-dir",
            str(out),
        ],
        cwd=str(Path(__file__).resolve().parents[1]),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr[-1500:]
    summary = pd.read_csv(out / "model_cost_eval_summary.csv")
    assert set(summary["model"]) == {"ridge", "tree"}
    assert (summary["n_splits"] == 1).all()
    detail = pd.read_csv(out / "model_cost_eval.csv")
    assert {"model", "split_id", "active_excess_bp", "total_cost", "n_rejected"} <= set(
        detail.columns
    )
