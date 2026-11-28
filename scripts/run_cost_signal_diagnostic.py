"""Separate realized execution costs from prediction on frozen, matched action paths."""

import argparse
from datetime import date
from itertools import pairwise
from pathlib import Path
from statistics import median

import numpy as np
from evaluate_rl import replay, sha
from inventory_forward import runtime_digest
from run_action_value_probe import DESIGN, active_bp, fit_selector, probe_window, training_split
from run_critic_budget import read
from train_rl import ROOT, dump

PLAN = ROOT / "config/experiments/cost_signal_diagnostic.json"


def costs(book, raw, signal, horizon):
    """Accrue unpaid fees consistently with net NAV; spread is included only once."""
    days = book.loc[signal:horizon].index[1:]
    raw = raw.set_index("date").loc[days]
    spread, funding = float(raw.spread.sum()), float(raw.funding.sum())
    total = float(book.loc[days, "cost"].sum())
    return dict(spread=spread, funding=funding, fee=total - spread - funding, total=total)


def restore(net, delta, opening):
    if opening <= 0 or not np.isfinite([net, opening, *delta.values()]).all():
        raise ValueError("invalid cost decomposition")
    if abs(delta["total"] - sum(delta[k] for k in ("spread", "fee", "funding"))) > 1e-6:
        raise ValueError("cost components do not close")
    return dict(
        net=float(net),
        transaction_cost_restored=float(net + (delta["spread"] + delta["fee"]) * 1e4 / opening),
        all_cost_restored=float(net + delta["total"] * 1e4 / opening),
    )


def source(fold, band, archive):
    plan, design = read(PLAN), read(DESIGN)
    if fold not in plan["folds"] or band not in plan["bands"]:
        raise ValueError("unregistered fold/band")
    fit_path = archive / f"fold-{fold}/fit_labels.json"
    held_path = ROOT / "reports/action_value_holdout.json"
    if (
        sha(DESIGN) != plan["original_design_sha256"]
        or sha(fit_path) != plan["fit_sha256"][str(fold)]
        or sha(held_path) != plan["holdout_sha256"]
    ):
        raise ValueError("frozen label source differs")
    fit = read(fit_path)
    (held,) = [r for r in read(held_path)["pairs"] if r["fold"] == fold and r["band"] == band]
    market, anchor, grid, features = probe_window(fold, design)
    intervals, start, end = training_split(grid)
    if market.training_hash({"train": ["", str(end)]}) != plan["market_sha256"][str(fold)]:
        raise ValueError("historical market source differs")
    expected_fit = [(str(d), str(h)) for d, h in intervals]
    expected_held = [(str(d), str(h)) for d, h in pairwise(d for d in grid if d >= start)]
    if [(r["signal"], r["horizon"]) for r in fit] != expected_fit or [
        (r["signal"], r["horizon"]) for r in held["decisions"]
    ] != expected_held:
        raise ValueError("frozen fit/holdout dates differ")
    for r in fit:
        cutoff = market.calendar.prev_trading_day(date.fromisoformat(r["signal"]))
        if r["cutoff"] != str(cutoff):
            raise ValueError("fit clock differs")
        np.testing.assert_allclose(r["features"], features(cutoff), atol=1e-10, rtol=0)
    rows = (
        [dict(r, segment="fit", expected_net_bp=r["net_bp"]) for r in fit]
        if band == plan["fit_band"]
        else []
    )
    rows += [dict(r, segment="held", expected_net_bp=r["actual_bp"]) for r in held["decisions"]]
    fingerprint = dict(
        plan_sha256=sha(PLAN),
        runtime_sha256=runtime_digest(ROOT),
        market_sha256=plan["market_sha256"][str(fold)],
        fit_sha256=sha(fit_path),
        holdout_sha256=sha(held_path),
    )
    return plan, market, anchor, end, features, rows, fingerprint


def traced(market, anchor, end, band, policy):
    evidence = {}

    def audit(roll, proposals, book):
        evidence["costs"] = roll.result.costs.copy()
        evidence["faces"] = {r.date: dict(r.post_trade_faces) for r in roll.records}

    book, error = replay(market, anchor, end, band, policy, audit=audit)
    return book, evidence, error


def one_interval(market, anchor, band, row, zero, zero_audit, features, plan):
    d, h = map(date.fromisoformat, (row["signal"], row["horizon"]))
    opening = float(zero.at[d, "net_nav"])
    baseline_cost = costs(zero, zero_audit["costs"], d, h)
    labels = []
    errors = []
    for i, action in enumerate(plan["actions"]):
        if i == 0:
            book, audit, error = zero, zero_audit, 0.0
        else:
            book, audit, error = traced(
                market, anchor, h, band, lambda ctx, prev, z=action: z if ctx.as_of == d else 0.0
            )
        net = active_bp(book, zero, d, h)
        components = costs(book, audit["costs"], d, h)
        delta = {k: components[k] - baseline_cost[k] for k in components}
        days = book.loc[d:h].index[1:]
        labels.append(
            dict(
                **restore(net, delta, opening),
                delta_cost_yuan=delta,
                trades=int(book.loc[days, "trades"].sum()),
                different_faces_days=sum(audit["faces"][t] != zero_audit["faces"][t] for t in days),
            )
        )
        errors.append(error)
    np.testing.assert_allclose(
        [v["net"] for v in labels], row["expected_net_bp"], atol=plan["tolerance_bp"], rtol=0
    )
    cutoff = market.calendar.prev_trading_day(d)
    return dict(
        signal=str(d),
        horizon=str(h),
        cutoff=str(cutoff),
        segment=row["segment"],
        features=features(cutoff).tolist(),
        opening_net_nav=opening,
        labels=labels,
        ledger_error_yuan=max(errors),
    )


def collect(root, archive, fold, band, shard, shards, *, limit=None):
    if not 0 <= shard < shards:
        raise ValueError("invalid shard")
    plan, market, anchor, end, features, rows, fingerprint = source(fold, band, archive)
    rows = rows[shard::shards]
    if limit is not None:
        if limit <= 0:
            raise ValueError("positive smoke limit required")
        rows = rows[:limit]
    dest = root / f"fold-{fold}/band-{band}"
    dest.mkdir(parents=True, exist_ok=True)
    zero, audit, error = traced(market, anchor, end, band, lambda ctx, prev: 0.0)
    for row in rows:
        path = dest / (row["signal"] + ".json")
        expected = dict(
            **fingerprint,
            fold=fold,
            band=band,
            signal=row["signal"],
            horizon=row["horizon"],
            segment=row["segment"],
        )
        if path.exists():
            if any(read(path).get(k) != v for k, v in expected.items()):
                raise ValueError("existing diagnostic identity differs")
            continue
        result = one_interval(market, anchor, band, row, zero, audit, features, plan)
        result["ledger_error_yuan"] = max(error, result["ledger_error_yuan"])
        dump(path, dict(**result, **{k: v for k, v in expected.items() if k not in result}))
        print(
            f"completed fold={fold} band={band} segment={row['segment']} signal={row['signal']}",
            flush=True,
        )


def compare(fit, held, alpha):
    """Fit both targets on the same rows. Future costs never enter predictors."""
    if max(r["horizon"] for r in fit) >= min(r["signal"] for r in held):
        raise ValueError("label crosses prediction boundary")
    actions = ["net", "all_cost_restored"]
    predictors, fixed, forecasts = {}, {}, {}
    for target in actions:
        samples = [
            dict(features=r["features"], net_bp=[a[target] for a in r["labels"]]) for r in fit
        ]
        predictors[target], fixed[target] = fit_selector(samples, {"ridge_alpha": alpha})
        forecasts[target] = np.array([predictors[target](np.array(r["features"])) for r in held])
    n = np.array([a["net"] for r in held for a in r["labels"]]).reshape(-1, 4)
    g = np.array([a["all_cost_restored"] for r in held for a in r["labels"]]).reshape(-1, 4)
    gn = forecasts["all_cost_restored"].argmax(1)
    nn = forecasts["net"].argmax(1)
    idx = np.arange(len(held))
    mean = np.mean([[a["all_cost_restored"] for a in r["labels"]] for r in fit], axis=0)
    return dict(
        restored_rmse_bp=float(
            np.sqrt(np.mean((forecasts["all_cost_restored"][:, 1:] - g[:, 1:]) ** 2))
        ),
        restored_fit_mean_rmse_bp=float(np.sqrt(np.mean((mean[1:] - g[:, 1:]) ** 2))),
        restored_selected_bp=float(g[idx, gn].mean()),
        restored_vs_fixed_bp=float((g[idx, gn] - g[:, fixed["all_cost_restored"]]).mean()),
        actual_selected_net_bp=float(n[idx, gn].mean()),
        actual_vs_net_selector_bp=float((n[idx, gn] - n[idx, nn]).mean()),
        actual_vs_restored_fixed_bp=float((n[idx, gn] - n[:, fixed["all_cost_restored"]]).mean()),
        actual_vs_net_fixed_bp=float((n[idx, gn] - n[:, fixed["net"]]).mean()),
        net_fixed_index=fixed["net"],
        restored_fixed_index=fixed["all_cost_restored"],
        decisions=[
            dict(
                signal=r["signal"],
                net_pick=int(a),
                restored_pick=int(b),
                net_predictions=p.tolist(),
                restored_predictions=q.tolist(),
                net_values=n[i].tolist(),
                restored_values=g[i].tolist(),
                restored_choice_different_faces_days=r["labels"][b]["different_faces_days"],
            )
            for i, (r, a, b, p, q) in enumerate(
                zip(held, nn, gn, forecasts["net"], forecasts["all_cost_restored"], strict=True)
            )
        ],
    )


def summarize(root, archive):
    plan = read(PLAN)
    results, max_error = [], 0.0
    old = read(ROOT / "reports/action_value_holdout.json")
    for fold in plan["folds"]:
        fit = None
        for band in plan["bands"]:
            _, _, _, _, features, expected, fingerprint = source(fold, band, archive)
            rows = []
            for row in expected:
                r = read(root / f"fold-{fold}/band-{band}/{row['signal']}.json")
                identity = dict(
                    **fingerprint,
                    fold=fold,
                    band=band,
                    signal=row["signal"],
                    horizon=row["horizon"],
                    segment=row["segment"],
                )
                if any(r.get(k) != v for k, v in identity.items()):
                    raise ValueError("summary source identity differs")
                if not r["cutoff"] < r["signal"] < r["horizon"] or r["opening_net_nav"] <= 0:
                    raise ValueError("invalid diagnostic clock/opening")
                np.testing.assert_allclose(
                    r["features"],
                    features(date.fromisoformat(r["cutoff"])),
                    atol=1e-10,
                    rtol=0,
                )
                for a in r["labels"]:
                    expected_value = restore(a["net"], a["delta_cost_yuan"], r["opening_net_nav"])
                    np.testing.assert_allclose(
                        [a[t] for t in expected_value],
                        list(expected_value.values()),
                        atol=1e-10,
                        rtol=0,
                    )
                np.testing.assert_allclose(
                    [a["net"] for a in r["labels"]],
                    row["expected_net_bp"],
                    atol=plan["tolerance_bp"],
                    rtol=0,
                )
                max_error = max(max_error, r["ledger_error_yuan"])
                rows.append(r)
            if band == plan["fit_band"]:
                fit = [r for r in rows if r["segment"] == "fit"]
            if fit is None:
                raise ValueError("fit band must precede sensitivity")
            held = [r for r in rows if r["segment"] == "held"]
            result = compare(fit, held, plan["ridge_alpha"])
            (prior,) = [r for r in old["pairs"] if r["fold"] == fold and r["band"] == band]
            np.testing.assert_allclose(
                [r["net_predictions"] for r in result["decisions"]],
                [r["predicted_bp"] for r in prior["decisions"]],
                atol=1e-9,
                rtol=0,
            )
            results.append(
                dict(
                    fold=fold, band=band, fit_intervals=len(fit), held_intervals=len(held), **result
                )
            )
        if [d["restored_pick"] for d in results[-1]["decisions"]] != [
            d["restored_pick"] for d in results[-2]["decisions"]
        ]:
            raise ValueError("execution bands must share predictions")
    if max_error > plan["ledger_tolerance_yuan"]:
        raise ValueError("ledger residual exceeds limit")
    keys = [
        "restored_selected_bp",
        "restored_vs_fixed_bp",
        "actual_selected_net_bp",
        "actual_vs_net_selector_bp",
        "actual_vs_restored_fixed_bp",
    ]
    med = {
        str(b): {k: median(r[k] for r in results if r["band"] == b) for k in keys}
        for b in plan["bands"]
    }
    wins = sum(
        r["restored_rmse_bp"] < r["restored_fit_mean_rmse_bp"] for r in results if r["band"] == 2
    )
    pred = (
        wins >= 2
        and min(med["2"][k] for k in keys[:2]) > 0
        and min(med["1"][k] for k in keys[:2]) >= 0
    )
    econ = min(med["2"][k] for k in keys[2:]) > 0 and min(med["1"][k] for k in keys[2:]) >= 0
    result = dict(
        plan_sha256=sha(PLAN),
        runtime_sha256=runtime_digest(ROOT),
        results=results,
        median_bp=med,
        restored_prediction_wins=wins,
        restored_prediction_gate=pred,
        actual_cost_economics_gate=econ,
        route="old_development_continuous_only"
        if pred and econ
        else "single_execution_design_candidate"
        if pred
        else "stop_this_feature_action_family",
        max_ledger_error_yuan=max_error,
        rl_training_authorized=False,
        scope="Fixed original execution paths with realized costs restored. Not zero-cost reoptimized trading, not zero-cost PPO training, not an upper bound over all policies.",
    )
    dump(root / "summary.json", result)
    return result


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("mode", choices=["collect", "summary"])
    p.add_argument("--root", required=True, type=Path)
    p.add_argument("--archive", required=True, type=Path)
    p.add_argument("--fold", type=int)
    p.add_argument("--band", type=int)
    p.add_argument("--shard", type=int, default=0)
    p.add_argument("--shards", type=int, default=1)
    p.add_argument("--limit", type=int)
    a = p.parse_args()
    if a.mode == "collect":
        collect(a.root, a.archive, a.fold, a.band, a.shard, a.shards, limit=a.limit)
    else:
        print(summarize(a.root, a.archive)["route"])
