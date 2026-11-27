"""Held-out action labels and matched continuous ledgers for the frozen action-value probe."""

import argparse
import json
from itertools import pairwise
from pathlib import Path

import numpy as np
from evaluate_rl import replay, sha
from run_action_value_probe import (
    DESIGN,
    active_bp,
    fit_selector,
    probe_labels,
    probe_window,
    training_split,
)
from train_rl import dump


def read(path):
    return json.loads(path.read_text())


def one_step(root, fold, band, shard, shards):
    design = read(DESIGN)
    if band not in design["replay_bands"] or not 0 <= shard < shards:
        raise ValueError("unregistered band or invalid partition")
    market, anchor, grid, _ = probe_window(fold, design)
    _, start, end = training_split(grid)
    intervals = list(pairwise(d for d in grid if d >= start))[shard::shards]
    dest = root / f"fold-{fold}/band-{band}/labels"
    dest.mkdir(parents=True, exist_ok=True)
    zero, _ = replay(market, anchor, end, band, lambda ctx, prev: 0.0)
    for signal, horizon in intervals:
        y = [
            0.0,
            *probe_labels(market, zero, anchor, signal, horizon, dict(design, label_band=band)),
        ]
        dump(
            dest / f"{signal}.json",
            dict(
                fold=fold,
                band=band,
                signal=str(signal),
                horizon=str(horizon),
                design_sha256=sha(DESIGN),
                net_bp=y,
            ),
        )


def matched(book, ref, faces, ref_faces, start, end):
    days = [d for d in book.loc[start:end].index if d > start]
    if set(days) != faces.keys() or set(days) != ref_faces.keys():
        raise ValueError("unmatched holding dates")
    return dict(
        days=len(days),
        different_faces_days=sum(faces[d] != ref_faces[d] for d in days),
        trades=int(book.loc[days, "trades"].sum()),
        reference_trades=int(ref.loc[days, "trades"].sum()),
        cost_difference_yuan=float((book.loc[days, "cost"] - ref.loc[days, "cost"]).sum()),
        net_difference_bp=active_bp(book, ref, start, end),
    )


def continuous(root, archive, fold, band):
    design = read(DESIGN)
    if band not in design["replay_bands"]:
        raise ValueError("unregistered band")
    market, anchor, grid, features = probe_window(fold, design)
    intervals, start, end = training_split(grid)
    src = archive / f"fold-{fold}"
    fit_path = src / "fit_labels.json"
    samples = read(fit_path)
    if len(samples) != len(intervals):
        raise ValueError("fit archive incomplete")
    for r, (d, h) in zip(samples, intervals, strict=True):
        cutoff = market.calendar.prev_trading_day(d)
        if (
            (r["signal"], r["horizon"], r["cutoff"]) != (str(d), str(h), str(cutoff))
            or not np.allclose(r["features"], features(cutoff), atol=1e-10, rtol=0)
            or len(r["net_bp"]) != len(design["actions"])
            or r["net_bp"][0] != 0
            or not np.isfinite(r["net_bp"]).all()
        ):
            raise ValueError("fit sample provenance mismatch")
    predict, fixed = fit_selector(samples, design)
    signals = [d for d in grid if start <= d < end]
    forecasts = {d: predict(features(market.calendar.prev_trading_day(d))) for d in signals}
    picks = {d: int(p.argmax()) for d, p in forecasts.items()}
    acts = design["actions"]

    def full(action, first, last, b):
        faces = {}

        def audit(roll, proposals, book):
            faces.update(
                {r.date: dict(r.post_trade_faces) for r in roll.records if first < r.date <= last}
            )

        book, _ = replay(
            market,
            anchor,
            last,
            b,
            lambda ctx, prev: action(ctx.as_of) if ctx.as_of >= first else 0.0,
            audit=audit,
        )
        return book, faces

    dest = root / f"fold-{fold}/band-{band}"
    dest.mkdir(parents=True, exist_ok=True)
    # The extra continuous control is always selected at 2 lots on the purged fit segment.
    fit_first, fit_last = intervals[0][0], intervals[-1][1]
    fit_zero, _ = full(lambda d: 0.0, fit_first, fit_last, design["label_band"])
    fit_gains = [0.0]
    for action in acts[1:]:
        book, _ = full(lambda d, z=action: z, fit_first, fit_last, design["label_band"])
        fit_gains.append(active_bp(book, fit_zero, fit_first, fit_last))
    cf = int(np.argmax(fit_gains))
    controls = {}
    for idx in dict.fromkeys([0, fixed, cf]):
        controls[idx] = full(lambda d, i=idx: acts[i], start, end, band)
    book, faces = full(lambda d: acts[picks[d]], start, end, band)
    if list(book.loc[start:end].loc[lambda x: x.decision].index) != signals:
        raise ValueError("selection schedule differs from ledger decisions")
    comp = {
        name: matched(book, controls[i][0], faces, controls[i][1], start, end)
        for name, i in (("zero", 0), ("label_fixed", fixed), ("continuous_fixed", cf))
    }
    zero = controls[0][0]
    daily = []

    def path_change(path, d, h):
        return float(path.at[h, "net_nav"] - path.at[d, "net_nav"])

    for d, h in pairwise([*signals, end]):
        daily.append(
            dict(
                signal=str(d),
                horizon=str(h),
                selected_index=picks[d],
                predicted_bp=forecasts[d].tolist(),
                weight=float(zero.at[d, "net_nav"] / zero.at[start, "net_nav"]),
                path_vs_zero_bp=(path_change(book, d, h) - path_change(zero, d, h))
                * 1e4
                / zero.at[start, "net_nav"],
                path_vs_fixed_bp=(path_change(book, d, h) - path_change(controls[fixed][0], d, h))
                * 1e4
                / zero.at[start, "net_nav"],
            )
        )
    for base, field in [("zero", "path_vs_zero_bp"), ("label_fixed", "path_vs_fixed_bp")]:
        if abs(sum(r[field] for r in daily) - comp[base]["net_difference_bp"]) > 1e-7:
            raise ValueError("interval attribution does not close")
    dump(
        dest / "continuous.json",
        dict(
            fold=fold,
            band=band,
            design_sha256=sha(DESIGN),
            fit_sha256=sha(fit_path),
            fixed_index=fixed,
            continuous_fixed_index=cf,
            fit_continuous_net_bp=fit_gains,
            fit_mean_bp=np.mean([r["net_bp"] for r in samples], axis=0).tolist(),
            comparisons=comp,
            decisions=daily,
        ),
    )


def join(root, archive, fold, band):
    dest = root / f"fold-{fold}/band-{band}"
    c = read(dest / "continuous.json")
    src = archive / f"fold-{fold}"
    ref = read(src / "result.json")
    design = read(DESIGN)
    if (c["fold"], c["band"], c["design_sha256"], c["fit_sha256"]) != (
        fold,
        band,
        sha(DESIGN),
        sha(src / "fit_labels.json"),
    ) or (ref["design_sha256"], ref["fold"], ref["fixed_action"]) != (
        sha(DESIGN),
        fold,
        design["actions"][c["fixed_index"]],
    ):
        raise ValueError("frozen identity mismatch")
    (rr,) = [r for r in ref["rows"] if r["band"] == band]
    a, b = c["comparisons"]["zero"], c["comparisons"]["label_fixed"]
    if (a["trades"], b["reference_trades"]) != (
        rr["selector"]["trades"],
        rr["fixed"]["trades"],
    ) or not np.allclose(
        [a["net_difference_bp"], b["net_difference_bp"]],
        [rr["selector"]["net_bp"], rr["gain_vs_fixed_bp"]],
        atol=1e-6,
        rtol=0,
    ):
        raise ValueError("continuous control failed archived replication")
    reset = [[], []]
    sq_pred = []
    sq_mean = []
    for r in c["decisions"]:
        lab = read(dest / "labels" / (r["signal"] + ".json"))
        if (lab["fold"], lab["band"], lab["signal"], lab["horizon"], lab["design_sha256"]) != (
            fold,
            band,
            r["signal"],
            r["horizon"],
            sha(DESIGN),
        ):
            raise ValueError("held-out label date or band mismatch")
        y = np.array(lab["net_bp"])
        p = np.array(r["predicted_bp"])
        if y.shape != (4,) or y[0] != 0 or not np.isfinite(y).all():
            raise ValueError("invalid held-out values")
        selected = float(y[r["selected_index"]])
        fx = float(y[c["fixed_index"]])
        r.update(actual_bp=y.tolist(), selected_bp=selected, fixed_bp=fx)
        reset[0].append(selected)
        reset[1].append(selected - fx)
        sq_pred.extend((p[1:] - y[1:]) ** 2)
        sq_mean.extend((np.array(c["fit_mean_bp"][1:]) - y[1:]) ** 2)
    for name, vs in zip(("zero", "label_fixed"), reset, strict=True):
        reset_sum = sum(r["weight"] * x for r, x in zip(c["decisions"], vs, strict=True))
        c["comparisons"][name].update(
            mean_one_step_bp=float(np.mean(vs)),
            wins=sum(v > 1e-8 for v in vs),
            ties=sum(abs(v) <= 1e-8 for v in vs),
            spliced_bp=reset_sum,
            continuous_minus_spliced_bp=c["comparisons"][name]["net_difference_bp"] - reset_sum,
        )
    c.update(
        prediction_rmse_bp=float(np.sqrt(np.mean(sq_pred))),
        fit_mean_rmse_bp=float(np.sqrt(np.mean(sq_mean))),
    )
    dump(dest / "audit.json", c)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("mode", choices=["labels", "continuous", "join"])
    p.add_argument("--archive", type=Path)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--fold", type=int, required=True)
    p.add_argument("--band", type=int, choices=[1, 2], required=True)
    p.add_argument("--shard", type=int, default=0)
    p.add_argument("--shards", type=int, default=1)
    a = p.parse_args()
    if a.mode == "labels":
        one_step(a.output, a.fold, a.band, a.shard, a.shards)
    elif a.archive is None:
        p.error("archive is required")
    else:
        (continuous if a.mode == "continuous" else join)(a.output, a.archive, a.fold, a.band)
