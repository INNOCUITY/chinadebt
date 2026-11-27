"""Reconcile frozen validation choices and summarize paired, continuous test books."""

import argparse
import hashlib
import json
from pathlib import Path
from statistics import median

import numpy as np
import pandas as pd

from ficc.research.inference import annualized_inference

PLAN = Path(__file__).resolve().parents[1] / "config/experiments/ppo_evaluation.json"


def analyze(root):
    plan = json.loads(PLAN.read_text())
    digest = hashlib.sha256(PLAN.read_bytes()).hexdigest()
    folds = plan["folds"]
    start, end = folds[0]["test"][0], folds[-1]["test"][1]
    choices = dict(pairs=0, ties=0, both_zero=0, update_2_wins=0)
    bands = {band: {"seeds": [], "baselines": {}} for band in plan["bands"]}
    active = {b: [] for b in bands}
    fold_scores = {b: [[] for _ in folds] for b in bands}
    max_error = 0
    for seed in plan["seeds"]:
        folder = root / f"seed-{seed}"
        report = json.loads((folder / "continuous/test.json").read_text())
        assert report["seed"] == seed and report["plan_sha256"] == digest
        assert len(report["selections"]) == len(folds)
        for fold, selection in zip(folds, report["selections"], strict=True):
            result = json.loads((folder / f"fold-{fold['fold']}/selection.json").read_text())
            assert selection == {k: result[k] for k in selection}
            assert (result["fold"], result["seed"]) == (fold["fold"], seed)
            assert result["plan_sha256"] == digest and result["validation"] == fold["validation"]
            rows = result["candidates"]
            assert [r["update"] for r in rows] == plan["updates"]
            a, b = [round(r["net_excess_vs_zero_bp"], 8) for r in rows]
            assert result["update"] == (2 if b > a else 1)
            assert result["checkpoint_sha256"] == rows[result["update"] - 1]["checkpoint_sha256"]
            choices["pairs"] += 1
            choices["ties"] += a == b
            choices["both_zero"] += a == b == 0
            choices["update_2_wins"] += b > a
        for band, summary in bands.items():
            books = {}
            for model in ("ppo", "zero"):
                path = folder / f"continuous/{model}-{band}lot-daily.csv"
                book = pd.read_csv(path, index_col="date")
                assert book.index.is_unique and book.index.is_monotonic_increasing
                books[model] = book
            p, z = books["ppo"], books["zero"]
            assert p.index.equals(z.index) and start in p.index and end in p.index
            prior = p.index[p.index < start][-1]
            nav = p.loc[prior, "net_nav"]
            assert np.isclose(nav, z.loc[prior, "net_nav"], rtol=0, atol=1e-5)
            daily = (p.net_nav.pct_change() - z.net_nav.pct_change()).loc[start:end]
            assert daily.notna().all()
            active[band].append(daily)
            metrics = [r for r in report["metrics"] if r["band_lots"] == band]
            pooled = {r["model"]: r for r in metrics if r["window"] == "pooled"}
            max_error = max(max_error, *(r["ledger_error_yuan"] for r in metrics))
            assert max_error <= 1e-5
            r = pooled["ppo"]
            excess = (p.loc[end, "net_nav"] - z.loc[end, "net_nav"]) / nav * 1e4
            assert np.isclose(excess, r["net_excess_vs_zero_bp"], rtol=0, atol=1e-7)
            cost = p.loc[start:end, "cost"].sum() - z.loc[start:end, "cost"].sum()
            assert np.isclose(cost, r["net_cost_yuan"] - pooled["zero"]["net_cost_yuan"])
            summary["seeds"].append(
                dict(
                    seed=seed,
                    excess_bp=excess,
                    cost_delta_bp=cost / nav * 1e4,
                    te_bp=r["te_vs_zero_bp"],
                    turnover=r["turnover"],
                    trades=r["trades"],
                )
            )
            baselines = {
                m: dict(
                    excess_bp=row["net_excess_vs_zero_bp"],
                    trades=row["trades"],
                    turnover=row["turnover"],
                )
                for m, row in pooled.items()
                if m != "ppo"
            }
            if summary["baselines"]:
                assert summary["baselines"] == baselines
            summary["baselines"] = baselines
            for row in metrics:
                if row["window"] != "pooled" and row["model"] == "ppo":
                    fold_scores[band][row["window"]].append(row["net_excess_vs_zero_bp"])
    for band, summary in bands.items():
        assert all(len(v) == len(plan["seeds"]) for v in fold_scores[band])
        summary["fold_median_excess_bp"] = [median(v) for v in fold_scores[band]]
        daily = pd.concat(active[band], axis=1)
        assert daily.notna().all().all()
        summary["daily_seed_mean_inference"] = annualized_inference(daily.mean(axis=1)).as_dict()
    return dict(test=[start, end], selection=choices, ledger_error_yuan=max_error, bands=bands)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    args = parser.parse_args()
    print(json.dumps(analyze(args.root), indent=2, allow_nan=False))
