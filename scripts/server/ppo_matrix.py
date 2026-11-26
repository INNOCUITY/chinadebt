"""第 0 折通过后，按 seed 分片训练、按验证窗选模，再逐 seed 连续测试。"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import subprocess
import sys
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "scripts"))
from evaluate_rl import EVAL_SPEC, dump, sha  # noqa: E402


def check_gate(root, plan):
    gate = json.loads((root / "gate/test.json").read_text())
    seed = plan["seeds"][0]
    selected = json.loads((root / f"seed-{seed}/fold-0/selection.json").read_text())
    expect = {(name, band) for name in plan["models"] for band in plan["bands"]}
    rows = [r for r in gate["metrics"] if r["window"] == "pooled"]
    if (
        gate["plan_sha256"] != sha(EVAL_SPEC)
        or gate["seed"] != seed
        or gate["selections"]
        != [{k: selected[k] for k in ("fold", "seed", "update", "checkpoint_sha256")}]
        or {(r["model"], r["band_lots"]) for r in rows} != expect
        or len(rows) != len(expect)
        or any(not (0 <= r["ledger_error_yuan"] <= 1e-5) for r in rows)
    ):
        raise ValueError("第 0 折冻结评估或对账未通过")


def run_worker(root, shard, shards):
    plan = json.loads(EVAL_SPEC.read_text())
    check_gate(root, plan)
    status = root / f"worker-{shard}.json"
    state = dict(
        shard=shard,
        shards=shards,
        pid=os.getpid(),
        plan_sha256=sha(EVAL_SPEC),
        commit=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        visible_device=os.environ.get("CUDA_VISIBLE_DEVICES"),
        state="running",
    )

    def run(script, *args):
        cmd = [sys.executable, str(ROOT / "scripts" / script), *map(str, args)]
        subprocess.run(cmd, cwd=ROOT, check=True)

    try:
        for seed in plan["seeds"][shard::shards]:
            for spec in plan["folds"]:
                fold = spec["fold"]
                folder = root / f"seed-{seed}/fold-{fold}"
                folder.mkdir(parents=True, exist_ok=True)
                state.update(seed=seed, fold=fold, stage="train")
                dump(status, state)
                ck = folder / "training"
                if not ck.exists():
                    # 完整训练成功才发布；失败尝试保留各自的日志和权重。
                    attempt = folder / f"training-{uuid.uuid4().hex[:8]}"
                    run(
                        "train_rl.py",
                        "train",
                        "--fold",
                        fold,
                        "--seed",
                        seed,
                        "--device",
                        "cuda",
                        "--output",
                        attempt,
                    )
                    attempt.rename(ck)
                state["stage"] = "select"
                dump(status, state)
                sel = folder / "selection.json"
                if not sel.exists():
                    run("evaluate_rl.py", "select", "--root", root, "--fold", fold, "--seed", seed)
                picked = json.loads(sel.read_text())
                if (
                    picked["plan_sha256"] != sha(EVAL_SPEC)
                    or (picked["seed"], picked["fold"]) != (seed, fold)
                    or sha(ck / f"update-{picked['update']}.pt") != picked["checkpoint_sha256"]
                ):
                    raise ValueError("已存在的选择与任务不匹配")
            dest = root / f"seed-{seed}/continuous"
            state["stage"] = "test"
            dump(status, state)
            if not dest.exists():
                stage = dest.with_name("test-" + uuid.uuid4().hex[:8])
                fold_args = [arg for f in plan["folds"] for arg in ("--fold", str(f["fold"]))]
                run(
                    "evaluate_rl.py",
                    "test",
                    "--root",
                    root,
                    "--seed",
                    seed,
                    "--output",
                    stage,
                    *fold_args,
                )
                stage.rename(dest)
        state.update(state="succeeded", stage="done")
    except BaseException as exc:
        state.update(state="failed", error=str(exc))
        raise
    finally:
        dump(status, state)


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", required=True, type=Path)
    p.add_argument("--shard", required=True, type=int, choices=[0, 1])
    a = p.parse_args()
    root = a.root.resolve()
    with (root / f".worker-{a.shard}.lock").open("a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        run_worker(root, a.shard, 2)


if __name__ == "__main__":
    main()
