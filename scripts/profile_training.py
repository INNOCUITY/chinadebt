"""固定动作、同一冻结窗口，测量单进程与 2/4 进程 CPU 回放。"""

from __future__ import annotations

import hashlib
import json
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from time import perf_counter


def main():
    root = Path("reports/ppo-cpu-profile")
    root.mkdir(exist_ok=False, parents=True)
    profiles = []
    reference = None
    for workers in (1, 2, 4):
        folders = [root / f"workers-{workers}-{i}" for i in range(workers)]
        tick = perf_counter()

        def replay(folder):
            with folder.with_suffix(".log").open("w") as log:
                subprocess.run(
                    [
                        sys.executable,
                        "scripts/train_rl.py",
                        "profile",
                        "--device",
                        "cpu",
                        "--output",
                        str(folder),
                    ],
                    stdout=log,
                    stderr=subprocess.STDOUT,
                    check=True,
                )

        with ThreadPoolExecutor(workers) as pool:
            list(pool.map(replay, folders))
        wall = perf_counter() - tick
        group = []
        for folder in folders:
            digest = hashlib.sha256((folder / "trajectory-1.json").read_bytes()).hexdigest()
            if reference is None:
                reference = digest
            if reference != digest:
                raise ValueError("并发固定回放与单任务轨迹不一致")
            group.append(json.loads((folder / "metrics-1.json").read_text()))
        profiles.append(
            dict(
                workers=workers,
                wall_seconds=wall,
                trajectory_sha256=reference,
                aggregate_days_per_second=sum(x["valuation_days"] for x in group) / wall,
                tasks=group,
            )
        )
    (root / "comparison.json").write_text(json.dumps(profiles, indent=2) + "\n")


if __name__ == "__main__":
    main()
