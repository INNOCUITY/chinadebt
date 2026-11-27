"""Run a registered categorical-policy pair, then compare with frozen continuous and zero controls."""

import argparse
import json
import subprocess
import sys
from collections import Counter
from datetime import date
from itertools import product
from pathlib import Path
from statistics import median

from audit_ppo_execution import decision_ledger
from evaluate_rl import sha, validation_rows
from train_rl import EVAL_SPEC, FACTOR_SPEC, GRID_SPEC, ROOT, dump, training_spec


def run(root, reference, fold, seed, device, active=False):
    spec = training_spec(fold, seed, action_grid=True, grid_active=active)
    plan = json.loads(EVAL_SPEC.read_text())
    grid = json.loads(GRID_SPEC.read_text())
    out = root / ("active" if active else "total") / f"fold-{fold}" / f"seed-{seed}"
    baseline = json.loads(
        (reference / out.parent.name / out.name / "total/validation.json").read_text()
    )
    if (
        baseline["fold"],
        baseline["seed"],
        baseline["arm"],
        baseline["design_sha256"],
        baseline["validation"],
    ) != (fold, seed, "total", sha(FACTOR_SPEC), spec["validation"]):
        raise ValueError("reference mismatch")
    out.mkdir(parents=True, exist_ok=False)
    ck = out / "training"
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/train_rl.py"),
            "train",
            "--action-grid",
            *(["--grid-active"] if active else []),
            "--device",
            device,
            "--fold",
            str(fold),
            "--seed",
            str(seed),
            "--output",
            str(ck),
        ],
        check=True,
        cwd=ROOT,
    )
    rows = []
    for band in grid["bands"]:
        days = []

        def trace(rollout, proposals, book, days=days):
            days.extend(
                decision_ledger(
                    rollout, proposals, book, date.fromisoformat(spec["validation"][0])
                ).values()
            )

        (row,) = validation_rows(plan, spec, ck, [spec["updates"]], band, audit=trace)
        (control,) = [
            r
            for r in baseline["results"]
            if r["band_lots"] == band and r["update"] == spec["updates"]
        ]
        action_counts = Counter(
            "hold" if r["proposal_z"] is None else str(r["proposal_z"])
            for r in days
            if r["decision"]
        )
        if sum(action_counts.values()) != row["decisions"]:
            raise ValueError("decision count mismatch")
        rows.append(
            dict(
                **row,
                band_lots=band,
                actions=dict(action_counts),
                gain_vs_continuous_bp=row["net_excess_vs_zero_bp"]
                - control["net_excess_vs_zero_bp"],
            )
        )
        dump(out / f"days-{band}.json", days)
    dump(
        out / "validation.json",
        dict(grid_sha256=sha(GRID_SPEC), fold=fold, seed=seed, active=active, rows=rows),
    )


def summarize(root):
    grid = json.loads(GRID_SPEC.read_text())
    bands = {band: [] for band in grid["bands"]}
    reward = {band: [] for band in bands}
    for fold, seed in product(grid["folds"], grid["seeds"]):
        pair = {}
        for active, label in [(False, "total"), (True, "active")]:
            d = json.loads((root / label / f"fold-{fold}/seed-{seed}/validation.json").read_text())
            if (d["grid_sha256"], d["fold"], d["seed"], d["active"]) != (
                sha(GRID_SPEC),
                fold,
                seed,
                active,
            ):
                raise ValueError("inconsistent grid result")
            pair[label] = {}
            for band in bands:
                (row,) = [
                    r
                    for r in d["rows"]
                    if r["band_lots"] == band and r["update"] == grid["updates"]
                ]
                pair[label][band] = row["net_excess_vs_zero_bp"]
                if not active:
                    bands[band].append(row)
        for band in bands:
            reward[band].append(pair["active"][band] - pair["total"][band])
    result = {}
    for b, rows in bands.items():
        gain = [r["gain_vs_continuous_bp"] for r in rows]
        net = [r["net_excess_vs_zero_bp"] for r in rows]
        result[b] = dict(
            gain_bp=gain,
            net_vs_zero_bp=net,
            positive=sum(round(x, 8) > 0 for x in gain),
            median_gain=median(gain),
            median_net=median(net),
        )
    passed = (
        result[2]["positive"] >= 4
        and result[2]["median_gain"] > 0
        and result[1]["median_gain"] >= 0
        and result[2]["median_net"] > 0
    )
    return dict(bands=result, expand_validation=passed, active_minus_total_grid_bp=reward)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    job = sub.add_parser("run")
    job.add_argument("--active", action="store_true")
    job.add_argument("--reference", type=Path, required=True)
    job.add_argument("--fold", type=int, required=True)
    job.add_argument("--seed", type=int, required=True)
    job.add_argument("--device", choices=["cpu", "cuda"], required=True)
    for cmd in (job, sub.add_parser("summary")):
        cmd.add_argument("--root", type=Path, required=True)
    a = p.parse_args()
    if a.command == "run":
        run(a.root.resolve(), a.reference, a.fold, a.seed, a.device, a.active)
    else:
        print(json.dumps(summarize(a.root), indent=2, allow_nan=False))
