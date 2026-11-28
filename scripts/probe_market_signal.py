"""Frozen lagged-curve feature probe on archived one-decision net action labels."""

import argparse
from datetime import date
from itertools import pairwise
from pathlib import Path
from statistics import median

import numpy as np
import pandas as pd
from evaluate_rl import sha
from inventory_forward import runtime_digest
from run_action_value_probe import DESIGN, fit_selector, probe_window, training_split
from run_critic_budget import read
from run_ml_baseline import fit_ridge
from train_rl import ROOT, dump

PLAN = ROOT / "config/experiments/market_signal_probe.json"


def extra_features(panel):
    """All four inputs stop at the supplied observation date; no forward fill from the future."""
    return pd.DataFrame(
        {
            "change_5_2y": panel[2.0].diff(5),
            "change_5_5y": panel[5.0].diff(5),
            "change_5_10y": panel[10.0].diff(5),
            "slope_change_20": (panel[10.0] - panel[2.0]).diff(20),
        }
    )


def augment(base, extra, mean):
    missing = ~np.isfinite(extra)
    if not np.isfinite(mean).all():
        raise ValueError("fit segment has no valid added-feature calibration")
    return np.c_[base, np.where(missing, mean, extra), missing.astype(float)]


def require_purged(fit, held):
    if not fit or not held or max(r["horizon"] for r in fit) >= min(r["signal"] for r in held):
        raise ValueError("fit labels cross the held-out decision boundary")
    if any(not r["cutoff"] < r["signal"] < r["horizon"] for r in fit):
        raise ValueError("fit information clock differs")


def run(archive, holdout_path):
    plan, design, holdout = read(PLAN), read(DESIGN), read(holdout_path)
    if sha(DESIGN) != plan["original_design_sha256"] or sha(holdout_path) != plan["holdout_sha256"]:
        raise ValueError("frozen label provenance differs")
    rows = []
    for fold in plan["folds"]:
        fit_path = archive / f"fold-{fold}/fit_labels.json"
        if sha(fit_path) != plan["fit_sha256"][str(fold)]:
            raise ValueError("frozen fit labels differ")
        fit = read(fit_path)
        market, _, grid, features = probe_window(fold, design)
        intervals, start, end = training_split(grid)
        if market.training_hash({"train": ["", str(end)]}) != plan["market_sha256"][str(fold)]:
            raise ValueError("frozen historical prices, funding or calendar differ")
        if [(r["signal"], r["horizon"]) for r in fit] != [(str(d), str(h)) for d, h in intervals]:
            raise ValueError("fit interval grid differs")
        if any(
            r["cutoff"] != str(market.calendar.prev_trading_day(date.fromisoformat(r["signal"])))
            for r in fit
        ):
            raise ValueError("fit feature cutoff differs")
        np.testing.assert_allclose(
            [r["features"] for r in fit],
            [features(date.fromisoformat(r["cutoff"])) for r in fit],
            atol=1e-10,
            rtol=0,
        )
        baseline, fixed = fit_selector(fit, design)
        x = np.array([r["features"] for r in fit])
        y = np.array([r["net_bp"] for r in fit])
        frame = extra_features(market.panel)
        extra = frame.loc[pd.to_datetime([r["cutoff"] for r in fit])].to_numpy()
        mean = np.nanmean(extra, axis=0)
        extended = augment(x, extra, mean)
        heads = [fit_ridge(extended, col, plan["ridge_alpha"]) for col in y.T[1:]]
        for band in plan["bands"]:
            (prior,) = [r for r in holdout["pairs"] if r["fold"] == fold and r["band"] == band]
            held = prior["decisions"]
            require_purged(fit, held)
            if (prior["fit_sha256"], prior["design_sha256"], prior["fixed_index"]) != (
                sha(fit_path),
                sha(DESIGN),
                fixed,
            ):
                raise ValueError("held-out fit identity differs")
            if [(r["signal"], r["horizon"]) for r in held] != [
                (str(d), str(h)) for d, h in pairwise(d for d in grid if start <= d <= end)
            ]:
                raise ValueError("held-out interval grid differs")
            cutoffs = [
                market.calendar.prev_trading_day(date.fromisoformat(r["signal"])) for r in held
            ]
            bx = np.array([features(c) for c in cutoffs])
            ex = augment(bx, frame.loc[pd.to_datetime(cutoffs)].to_numpy(), mean)
            original = np.array([baseline(v) for v in bx])
            np.testing.assert_allclose(
                original, [r["predicted_bp"] for r in held], atol=1e-9, rtol=0
            )
            forecast = np.c_[np.zeros(len(held)), *[m.predict(ex) for m in heads]]
            actual = np.array([r["actual_bp"] for r in held])
            if (
                actual.shape != (len(held), 4)
                or not np.isfinite(actual).all()
                or np.any(actual[:, 0])
            ):
                raise ValueError("invalid net action labels")
            pick = forecast.argmax(1)
            old_pick = original.argmax(1)
            idx = np.arange(len(held))
            selected = actual[idx, pick]
            constant = np.tile(y.mean(0), (len(held), 1))

            def rmse(p, actual=actual):
                return float(np.sqrt(np.mean((p[:, 1:] - actual[:, 1:]) ** 2)))

            rows.append(
                dict(
                    fold=fold,
                    band=band,
                    fit_intervals=len(fit),
                    heldout_intervals=len(held),
                    fit_last_horizon=max(r["horizon"] for r in fit),
                    heldout_first_signal=held[0]["signal"],
                    rmse_bp=rmse(forecast),
                    original_rmse_bp=rmse(original),
                    mean_rmse_bp=rmse(constant),
                    mean_selected_net_bp=float(selected.mean()),
                    gain_vs_original_bp=float(np.mean(selected - actual[idx, old_pick])),
                    gain_vs_label_fixed_bp=float(np.mean(selected - actual[:, fixed])),
                    gain_vs_continuous_fixed_bp=float(
                        np.mean(selected - actual[:, prior["continuous_fixed_index"]])
                    ),
                    selections=[
                        dict(
                            signal=r["signal"],
                            cutoff=str(c),
                            selected_index=int(a),
                            original_index=int(b),
                            predicted_bp=p.tolist(),
                            actual_bp=v.tolist(),
                        )
                        for r, c, a, b, p, v in zip(
                            held, cutoffs, pick, old_pick, forecast, actual, strict=True
                        )
                    ],
                )
            )
        # Both execution bands use identical forecasts and choices from the 2-lot fitted model.
        assert [r["selected_index"] for r in rows[-1]["selections"]] == [
            r["selected_index"] for r in rows[-2]["selections"]
        ]
    primary = [r for r in rows if r["band"] == 2]
    better = sum(r["rmse_bp"] < min(r["original_rmse_bp"], r["mean_rmse_bp"]) for r in primary)
    keys = [
        "mean_selected_net_bp",
        "gain_vs_original_bp",
        "gain_vs_label_fixed_bp",
        "gain_vs_continuous_fixed_bp",
    ]
    medians = {
        b: {k: median(r[k] for r in rows if r["band"] == b) for k in keys} for b in plan["bands"]
    }
    gate = better >= 2 and min(medians[2].values()) > 0 and min(medians[1].values()) >= 0
    return dict(
        design_sha256=sha(PLAN),
        runtime_sha256=runtime_digest(ROOT),
        rows=rows,
        prediction_wins_at_2_lot=better,
        median_bp=medians,
        consider_continuous_replay=gate,
        scope="Previously examined development periods. Paired next-interval labels from benchmark openings, not continuous strategy returns. This gate never authorizes PPO expansion or forward confirmation.",
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--archive", type=Path, required=True)
    p.add_argument("--holdout", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    report = run(a.archive, a.holdout)
    dump(a.output, report)
    print(
        {
            k: report[k]
            for k in ("prediction_wins_at_2_lot", "median_bp", "consider_continuous_replay")
        }
    )
