"""Measure deterministic training plateaus and evaluate a fixed categorical PPO update budget."""

import argparse
import json
import subprocess
import sys
from datetime import date
from itertools import product
from pathlib import Path
from statistics import median

import torch
from evaluate_rl import Market, load_actor, replay, sha, validation_rows
from train_rl import (
    CRITIC_BUDGET,
    CRITIC_SPEC,
    EVAL_SPEC,
    GRID_SPEC,
    INVESTED_SPEC,
    ROOT,
    dump,
    state_equal,
    training_spec,
)

from ficc.rl.ppo import ActorCritic


def read(path):
    return json.loads(path.read_text())


def plateau(history, design):
    tail = history[-3:]
    if len(tail) < 3:
        return False
    values = [r["train_active_bp"] for r in tail]
    return max(values) - min(values) <= design["max_training_range_bp"] and all(
        r["mean_action_tv"] <= design["max_mean_action_tv"] for r in tail[1:]
    )


def run(root, reference, fold, seed, device, cash_reference=None):
    spec = training_spec(
        fold,
        seed,
        action_grid=True,
        grid_active=True,
        zero_critic=True,
        critic_budget=True,
        invested_start=cash_reference is not None,
    )
    design = read(CRITIC_BUDGET)
    out = root / f"fold-{fold}/seed-{seed}"
    prior = reference / "active" / out.parent.name / out.name
    control = read(prior / "validation.json")
    if (
        control["critic_init_sha256"],
        control["grid_sha256"],
        control["fold"],
        control["seed"],
        control["active"],
    ) != (sha(CRITIC_SPEC), sha(GRID_SPEC), fold, seed, True):
        raise ValueError("reference mismatch")
    out.mkdir(parents=True, exist_ok=False)
    ck = out / "training"
    subprocess.run(
        [
            sys.executable,
            str(ROOT / "scripts/train_rl.py"),
            "train",
            "--action-grid",
            "--grid-active",
            "--zero-critic",
            "--critic-budget",
            *(["--invested-start"] if cash_reference is not None else []),
            "--fold",
            str(fold),
            "--seed",
            str(seed),
            "--device",
            device,
            "--output",
            str(ck),
        ],
        check=True,
        cwd=ROOT,
    )

    def saved(path):
        return torch.load(path, map_location="cpu", weights_only=False)

    old = saved(prior / "training/update-2.pt")
    new = saved(ck / "update-2.pt")
    if old["identity"]["data"] != new["identity"]["data"]:
        raise ValueError("training data changed")
    if cash_reference is None:
        if any(not state_equal(old[k], new[k]) for k in ("model", "optimizer", "rng")):
            raise ValueError("first two updates did not reproduce archived boundary")
    else:
        source = cash_reference / out.parent.name / out.name
        cash_result = read(source / "validation.json")
        if (cash_result["fold"], cash_result["seed"], cash_result["design_sha256"]) != (
            fold,
            seed,
            sha(CRITIC_BUDGET),
        ) or "opening_sha256" in cash_result:
            raise ValueError("cash-start control mismatch")
        cash_hash = sha(source / "training/update-16.pt")
    start, end = map(date.fromisoformat, new["identity"]["episode"])
    market = Market(end)
    begin = date.fromisoformat(read(EVAL_SPEC)["anchor"]) if cash_reference is not None else start
    zero, _ = replay(market, begin, end, 2, lambda ctx, prev: 0.0)
    observations = torch.tensor(
        [r["obs"] for r in read(ck / "trajectory-1.json")["decisions"] if r["actor_sample"]],
        dtype=torch.float32,
    )
    history = []
    prev = None

    model = ActorCritic(observations.shape[1], spec["hidden"], actions=spec["actions"])
    for step in design["checkpoints"]:
        path = ck / f"update-{step}.pt"
        act = load_actor(path, spec, step, market, market.cfg)
        book, error = replay(
            market,
            begin,
            end,
            2,
            lambda ctx, prev, act=act: act(ctx, prev) if ctx.as_of >= start else 0.0,
        )
        if abs(book.at[start, "net_nav"] - zero.at[start, "net_nav"]) > 1e-6:
            raise ValueError("training opening balance mismatch")
        model.load_state_dict(saved(path)["model"])
        with torch.no_grad():
            probs = model.distribution(observations).probs
        tv = None if prev is None else float((probs - prev).abs().sum(dim=1).mean() / 2)
        prev = probs
        history.append(
            dict(
                update=step,
                train_active_bp=float(
                    (book.net_nav.iloc[-1] - zero.net_nav.iloc[-1])
                    / zero.at[start, "net_nav"]
                    * 1e4
                ),
                mean_action_tv=tv,
                trades=int(book.trades.loc[book.index > start].sum()),
                ledger_error_yuan=error,
            )
        )
    dump(out / "training_curve.json", history)
    rows = []
    for band in read(GRID_SPEC)["bands"]:
        (row,) = validation_rows(read(EVAL_SPEC), spec, ck, [spec["updates"]], band)
        (old_row,) = [r for r in control["rows"] if r["band_lots"] == band and r["update"] == 2]
        if old_row["checkpoint_sha256"] != sha(prior / "training/update-2.pt"):
            raise ValueError("reference validation checkpoint differs")
        gain = row["net_excess_vs_zero_bp"] - old_row["net_excess_vs_zero_bp"]
        rows.append(
            dict(
                **row,
                band=band,
                gain_vs_2=gain,
                gain_vs_continuous=gain + old_row["gain_vs_continuous_bp"],
            )
        )
        if cash_reference is not None:
            (cash,) = [r for r in cash_result["rows"] if r["band"] == band and r["update"] == 16]
            if cash["checkpoint_sha256"] != cash_hash:
                raise ValueError("cash-start checkpoint mismatch")
            rows[-1]["gain_vs_cash_16"] = (
                row["net_excess_vs_zero_bp"] - cash["net_excess_vs_zero_bp"]
            )
    dump(
        out / "validation.json",
        dict(
            fold=fold,
            seed=seed,
            design_sha256=sha(CRITIC_BUDGET),
            plateau=plateau(history, design),
            rows=rows,
            **({"opening_sha256": sha(INVESTED_SPEC)} if cash_reference is not None else {}),
        ),
    )


def summarize(root, invested=False):
    grid = read(GRID_SPEC)
    design = read(CRITIC_BUDGET)
    records = []
    for f, s in product(grid["folds"], grid["seeds"]):
        d = read(root / f"fold-{f}/seed-{s}/validation.json")
        if (d["design_sha256"], d["fold"], d["seed"]) != (sha(CRITIC_BUDGET), f, s):
            raise ValueError("budget result mismatch")
        if d.get("opening_sha256") != (sha(INVESTED_SPEC) if invested else None):
            raise ValueError("opening protocol mismatch")
        records.append(d)
    controls = ["gain_vs_2", "gain_vs_continuous"]
    if invested:
        controls.append("gain_vs_cash_16")
    stats = {}
    for b in grid["bands"]:
        rows = []
        for d in records:
            (row,) = [
                r for r in d["rows"] if r["band"] == b and r["update"] == max(design["checkpoints"])
            ]
            rows.append(row)
        stats[b] = {k: [r[k] for r in rows] for k in [*controls, "net_excess_vs_zero_bp"]}
    stable = sum(d["plateau"] for d in records)
    passed = (
        stable == len(records)
        and median(stats[2]["net_excess_vs_zero_bp"]) > 0
        and all(
            sum(round(v, 8) > 0 for v in stats[2][k]) >= 4
            and median(stats[2][k]) > 0
            and median(stats[1][k]) >= 0
            for k in controls
        )
    )
    return dict(plateaus=stable, paired=stats, expand_validation=passed)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    sub = p.add_subparsers(dest="cmd", required=True)
    job = sub.add_parser("run")
    job.add_argument("--cash-reference", type=Path)
    job.add_argument("--fold", type=int, required=True)
    job.add_argument("--seed", type=int, required=True)
    job.add_argument("--reference", type=Path, required=True)
    job.add_argument("--device", choices=["cpu", "cuda"], required=True)
    summary = sub.add_parser("summary")
    summary.add_argument("--invested", action="store_true")
    for c in (summary, job):
        c.add_argument("--root", type=Path, required=True)
    a = p.parse_args()
    if a.cmd == "run":
        run(a.root.resolve(), a.reference.resolve(), a.fold, a.seed, a.device, a.cash_reference)
    else:
        print(json.dumps(summarize(a.root, a.invested), indent=2, allow_nan=False))
