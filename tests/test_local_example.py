"""本地 example 冒烟：带预热短 episode、RL 轨迹保存/恢复、极小 Ridge 保存/恢复。"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path


def test_local_example_runs(tmp_path) -> None:
    out = tmp_path / "example"
    proc = subprocess.run(
        [
            sys.executable,
            "scripts/run_local_example.py",
            "--out-dir",
            str(out),
        ],
        cwd=str(Path(__file__).resolve().parents[1]),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr[-1500:]
    assert (out / "trajectory.npz").exists()
    assert (out / "local_ridge.npz").exists()
    assert (out / "example_manifest.json").exists()
    assert "rollout NAV" in proc.stdout
    assert "Ridge 训练完成并恢复验证" in proc.stdout
