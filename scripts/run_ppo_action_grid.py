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
from train_rl import CRITIC_SPEC, EVAL_SPEC, FACTOR_SPEC, GRID_SPEC, ROOT, dump, training_spec


def run(root, reference, fold, seed, device, active=False, critic_reference=None):
    spec = training_spec(
        fold, seed, action_grid=True, grid_active=active, zero_critic=critic_reference is not None
    )
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
            *(["--zero-critic"] if critic_reference is not None else []),
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
    if critic_reference is not None:
        prior = critic_reference / out.relative_to(root)
        old = json.loads((prior / "validation.json").read_text())
        if (old["fold"], old["seed"], old["active"], old["grid_sha256"]) != (
            fold,
            seed,
            active,
            sha(GRID_SPEC),
        ) or "critic_init_sha256" in old:
            raise ValueError("ordinary critic reference mismatch")

        def first_trace(folder):
            return json.loads((folder / "trajectory-1.json").read_text())["decisions"]

        new_rows = first_trace(ck)
        for new, orig in zip(new_rows, first_trace(prior / "training"), strict=True):
            if new["value"] != 0 or {k: v for k, v in new.items() if k != "value"} != {
                k: v for k, v in orig.items() if k != "value"
            }:
                raise ValueError("first rollout changed beyond critic predictions")
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
        if critic_reference is not None:
            (old_row,) = [
                r for r in old["rows"] if r["band_lots"] == band and r["update"] == spec["updates"]
            ]
            rows[-1]["gain_vs_ordinary_critic_bp"] = (
                row["net_excess_vs_zero_bp"] - old_row["net_excess_vs_zero_bp"]
            )
        dump(out / f"days-{band}.json", days)
    dump(
        out / "validation.json",
        dict(
            grid_sha256=sha(GRID_SPEC),
            fold=fold,
            seed=seed,
            active=active,
            rows=rows,
            **({"critic_init_sha256": sha(CRITIC_SPEC)} if critic_reference is not None else {}),
        ),
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
            if "critic_init_sha256" in d:
                raise ValueError("use critic-summary for critic pilot")
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


def summarize_critic(root):
    grid = json.loads(GRID_SPEC.read_text())
    results = {}
    for b in grid["bands"]:
        base = []
        active = []
        vs_cont = []
        net = []
        for f, s in product(grid["folds"], grid["seeds"]):
            for label in ("active", "total"):
                d = json.loads((root / label / f"fold-{f}/seed-{s}/validation.json").read_text())
                if (
                    d["critic_init_sha256"],
                    d["grid_sha256"],
                    d["fold"],
                    d["seed"],
                    d["active"],
                ) != (sha(CRITIC_SPEC), sha(GRID_SPEC), f, s, label == "active"):
                    raise ValueError("critic result identity mismatch")
                (r,) = [
                    x for x in d["rows"] if x["band_lots"] == b and x["update"] == grid["updates"]
                ]
                if label == "active":
                    active.append(r["gain_vs_ordinary_critic_bp"])
                    vs_cont.append(r["gain_vs_continuous_bp"])
                    net.append(r["net_excess_vs_zero_bp"])
                else:
                    base.append(r["gain_vs_ordinary_critic_bp"])
        results[b] = dict(
            active_gain=active,
            total_gain=base,
            active_vs_continuous=vs_cont,
            active_net=net,
            interaction=[x - y for x, y in zip(active, base, strict=True)],
        )
    passed = median(results[2]["active_net"]) > 0 and all(
        sum(round(v, 8) > 0 for v in results[2][k]) >= 4
        and median(results[2][k]) > 0
        and median(results[1][k]) >= 0
        for k in ("active_gain", "active_vs_continuous")
    )
    return dict(bands=results, expand_validation=passed)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="command", required=True)
    job = sub.add_parser("run")
    job.add_argument("--critic-reference", type=Path)
    job.add_argument("--active", action="store_true")
    job.add_argument("--reference", type=Path, required=True)
    job.add_argument("--fold", type=int, required=True)
    job.add_argument("--seed", type=int, required=True)
    job.add_argument("--device", choices=["cpu", "cuda"], required=True)
    for cmd in (job, sub.add_parser("summary"), sub.add_parser("critic-summary")):
        cmd.add_argument("--root", type=Path, required=True)
    a = p.parse_args()
    if a.command == "run":
        run(a.root.resolve(), a.reference, a.fold, a.seed, a.device, a.active, a.critic_reference)
    elif a.command == "critic-summary":
        print(json.dumps(summarize_critic(a.root), indent=2, allow_nan=False))
    else:
        print(json.dumps(summarize(a.root), indent=2, allow_nan=False))
