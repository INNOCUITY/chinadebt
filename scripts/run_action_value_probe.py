"""Fit one-decision net action values, then replay the frozen selector on a later training segment."""

import argparse
import json
from datetime import date
from itertools import pairwise
from pathlib import Path
from statistics import median

import numpy as np
import pandas as pd
from evaluate_rl import Market, replay, sha
from run_ml_baseline import fit_ridge
from train_rl import EVAL_SPEC, ROOT, SPEC, dump

from ficc.research.protocol import ExperimentProtocol

DESIGN = ROOT / "config/experiments/action_value_probe.json"


def training_split(grid):
    intervals = list(pairwise(grid))
    if len(intervals) < 6:
        raise ValueError("too few complete training intervals")
    start = intervals[len(intervals) * 2 // 3][0]
    return [(d, e) for d, e in intervals if e < start], start, grid[-1]


def active_bp(book, zero, start, end):
    opening = zero.at[start, "net_nav"]
    if abs(book.at[start, "net_nav"] - opening) > 1e-5 or opening <= 0:
        raise ValueError("unmatched intervention opening")
    return float((book.at[end, "net_nav"] - zero.at[end, "net_nav"]) * 1e4 / opening)


def probe_labels(market, zero, anchor, signal, end, design):
    for z in design["actions"][1:]:

        def action(ctx, prev, z=z):
            if ctx.as_of == signal:
                return z
            return 0.0

        book, _ = replay(market, anchor, end, design["label_band"], action)
        if not book.at[signal, "decision"]:
            raise ValueError("missing intervention")
        yield active_bp(book, zero, signal, end)


def probe_window(fold, design):
    if fold not in design["folds"]:
        raise ValueError("unregistered fold")
    plan = json.loads(EVAL_SPEC.read_text())
    first, last = map(date.fromisoformat, plan["folds"][fold]["train"])
    anchor = date.fromisoformat(plan["anchor"])
    market = Market(last)
    calendar = market.calendar
    days = calendar.trading_days_between(first, last)
    n = json.loads(SPEC.read_text())["calibration_days"]
    grid = [d for d in ExperimentProtocol().decision_dates(calendar, anchor, last) if d >= days[n]]
    calibration_mean = market.features.loc[str(first) : str(days[n - 1])].mean().to_numpy()
    if not np.isfinite(calibration_mean).all():
        raise ValueError("invalid training calibration")

    def features(cutoff):
        raw = market.features.loc[pd.Timestamp(cutoff)].to_numpy(dtype=float)
        missing = np.isnan(raw)
        return np.r_[np.where(missing, calibration_mean, raw), missing]

    return market, anchor, grid, features


def fit_selector(samples, design):
    x = np.array([r["features"] for r in samples])
    y = np.array([r["net_bp"] for r in samples])
    models = [fit_ridge(x, col, design["ridge_alpha"]) for col in y.T[1:]]

    def predict(x):
        return np.array([0.0, *(m.predict(x) for m in models)])

    return predict, int(y.mean(axis=0).argmax())


def run(output, fold):
    design = json.loads(DESIGN.read_text())
    market, anchor, grid, features = probe_window(fold, design)
    calendar = market.calendar
    intervals, inner_start, end = training_split(grid)
    dest = output / f"fold-{fold}"
    dest.mkdir(parents=True, exist_ok=False)
    band = design["label_band"]
    zero, _ = replay(market, anchor, end, band, lambda ctx, prev: 0.0)
    samples = []
    for signal, horizon in intervals:
        y = [0.0, *probe_labels(market, zero, anchor, signal, horizon, design)]
        cutoff = calendar.prev_trading_day(signal)
        samples.append(
            dict(
                signal=str(signal),
                cutoff=str(cutoff),
                horizon=str(horizon),
                features=features(cutoff).tolist(),
                net_bp=y,
            )
        )
        dump(dest / "fit_labels.json", samples)
    y = np.array([r["net_bp"] for r in samples])
    predict, fixed_index = fit_selector(samples, design)
    actions = design["actions"]
    fixed = actions[fixed_index]

    def choose(ctx, prev):
        if ctx.curve.as_of != calendar.prev_trading_day(ctx.as_of):
            raise ValueError("feature cutoff mismatch")
        return actions[int(predict(features(ctx.curve.as_of)).argmax())]

    rows = []
    for b in design["replay_bands"]:
        if b != band:
            zero, _ = replay(market, anchor, end, b, lambda ctx, prev: 0.0)
        results = {}
        for name, policy in (("fixed", lambda ctx, prev: fixed), ("selector", choose)):
            book, err = replay(
                market,
                anchor,
                end,
                b,
                lambda ctx, prev, policy=policy: (
                    policy(ctx, prev) if ctx.as_of >= inner_start else 0.0
                ),
            )
            active = book.loc[inner_start:end]
            results[name] = dict(
                net_bp=active_bp(book, zero, inner_start, end),
                trades=int(active.loc[active.index > inner_start, "trades"].sum()),
                ledger_error_yuan=err,
            )
        rows.append(
            dict(
                band=b,
                **results,
                gain_vs_fixed_bp=results["selector"]["net_bp"] - results["fixed"]["net_bp"],
            )
        )
    dump(
        dest / "result.json",
        dict(
            design_sha256=sha(DESIGN),
            fold=fold,
            inner_start=str(inner_start),
            end=str(end),
            fit_intervals=len(samples),
            last_fit_horizon=samples[-1]["horizon"],
            fixed_action=fixed,
            mean_best_observed_net_bp=float(y.max(1).mean()),
            positive_opportunity_intervals=int((y.max(1) > 1e-8).sum()),
            rows=rows,
        ),
    )


def summarize(output):
    design = json.loads(DESIGN.read_text())
    gains = {b: [] for b in design["replay_bands"]}
    for f in design["folds"]:
        result = json.loads((output / f"fold-{f}/result.json").read_text())
        if (result["fold"], result["design_sha256"]) != (f, sha(DESIGN)):
            raise ValueError("probe identity mismatch")
        for band in gains:
            (r,) = [v for v in result["rows"] if v["band"] == band]
            gains[band].append([r["selector"]["net_bp"], r["gain_vs_fixed_bp"]])
    medians = {b: [median(v) for v in zip(*rows, strict=True)] for b, rows in gains.items()}
    return dict(
        median_vs_zero_and_fixed_bp=medians,
        consider_development_replay=min(medians[2]) > 0 and min(medians[1]) >= 0,
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--output", type=Path, required=True)
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--fold", type=int)
    g.add_argument("--summary", action="store_true")
    a = p.parse_args()
    if a.summary:
        dump(a.output / "summary.json", summarize(a.output))
    else:
        run(a.output, a.fold)
