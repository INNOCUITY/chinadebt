"""Pair registered grid validations with zero-tilt daily holdings and first-update advantages."""

import argparse
import json
from itertools import product
from pathlib import Path

import numpy as np
from run_ppo_action_grid import summarize
from train_rl import GRID_SPEC

from ficc.rl.ppo import advantages


def read(path):
    return json.loads(path.read_text())


def review(root):
    design = read(GRID_SPEC)
    gate = summarize(root)
    bands = {b: [] for b in design["bands"]}
    signals = []
    for fold, seed in product(design["folds"], design["seeds"]):
        trace = {}
        for label in ("total", "active"):
            p = root / label / f"fold-{fold}" / f"seed-{seed}"
            trace[label] = [
                r for r in read(p / "training/trajectory-1.json")["decisions"] if r["actor_sample"]
            ]
        for x, y in zip(trace["total"], trace["active"], strict=True):
            if any(x[k] != y[k] for k in ("date", "obs", "value", "latent", "reward")):
                raise ValueError("initial trajectory mismatch")
        values = np.array([r["value"] for r in trace["total"]])
        a = {}
        for key, v in (("archived", values), ("zero_head", np.zeros_like(values))):
            pair = [
                advantages(
                    [r.get("training_reward", r["reward"]) for r in trace[label]],
                    v,
                    gae_lambda=0.95,
                    terminated=True,
                )
                for label in trace
            ]
            a[key] = float(np.corrcoef(*pair)[0, 1])
        signals.append(dict(fold=fold, seed=seed, **a))
        for b in bands:
            control = read(root / f"execution-audit/fold-{fold}/band-{b}/hold-days.json")
            data = {}
            for label in trace:
                p = root / label / f"fold-{fold}" / f"seed-{seed}"
                daily = read(p / f"days-{b}.json")
                (row,) = [r for r in read(p / "validation.json")["rows"] if r["band_lots"] == b]
                if [r["date"] for r in daily] != [r["date"] for r in control]:
                    raise ValueError("valuation dates differ")
                pnl = sum(r["net_pnl_yuan"] for r in daily)
                if abs(row["net_return_bp"]) < 1e-9:
                    raise ValueError("opening NAV not identifiable from archived return")
                opening = pnl / (row["net_return_bp"] / 1e4)
                zero_pnl = sum(r["net_pnl_yuan"] - r["pnl_vs_zero_yuan"] for r in control)
                zero_cost = sum(r["cost_yuan"] - r["cost_vs_zero_yuan"] for r in control)
                delta = (pnl - zero_pnl) / opening * 1e4
                if opening <= 0 or abs(delta - row["net_excess_vs_zero_bp"]) > 1e-7:
                    raise ValueError("net P&L discrepancy")
                dv = [
                    r["active_dv01"] - (z["active_dv01"] - z["dv01_vs_zero"])
                    for r, z in zip(daily, control, strict=True)
                ]
                data[label] = dict(
                    net_bp=delta,
                    cost_delta_bp=(row["net_cost_yuan"] - zero_cost) / opening * 1e4,
                    trades=row["trades"],
                    actions=row["actions"],
                    days=len(daily),
                    days_different=sum(
                        r["post_trade_faces"] != z["zero_faces"]
                        for r, z in zip(daily, control, strict=True)
                    ),
                    mean_abs_dv01_vs_zero=float(np.mean(np.abs(dv))),
                )
                if label == "total":
                    total_days = daily
                else:
                    different = sum(
                        r["post_trade_faces"] != s["post_trade_faces"]
                        for r, s in zip(total_days, daily, strict=True)
                    )
            bands[b].append(
                dict(fold=fold, seed=seed, between_reward_different_days=different, **data)
            )
    return dict(gate=gate, pairs=bands, initial_advantage_corr=signals)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("root", type=Path)
    print(json.dumps(review(p.parse_args().root), indent=2, allow_nan=False))
