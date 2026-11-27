"""Estimate action-versus-hold net values on matched portfolios; screen frozen continuous policies."""

import argparse
import os
from datetime import date
from pathlib import Path
from statistics import median
from tempfile import TemporaryDirectory

import numpy as np
from audit_action_values import matched, read
from evaluate_rl import Market, replay, sha
from inventory_forward import checked_calendar, runtime_digest, scheduled_window
from run_action_value_probe import active_bp, probe_window, training_split
from run_ml_baseline import RidgeModel, fit_ridge
from train_rl import EVAL_SPEC, ROOT, SPEC, dump

from ficc.rl.collector import Encoder

DESIGN = ROOT / "config/experiments/inventory_value.json"


CALENDAR = ROOT / "config/baselines/inventory_forward_calendar.json"
SEAL = ROOT / "config/baselines/inventory_forward_seal.json"


def window(fold):
    cfg = read(DESIGN)
    market, anchor, grid, _ = probe_window(fold, cfg)
    intervals, start, end = training_split(grid)
    first, last = map(date.fromisoformat, read(EVAL_SPEC)["folds"][fold]["train"])
    days = market.calendar.trading_days_between(first, last)
    encoder = Encoder.fit(
        market.panel,
        market.funding,
        first,
        days[read(SPEC)["calibration_days"] - 1],
        market.cfg.portfolio.universe,
    )
    return cfg, market, anchor, intervals, start, end, encoder


def inputs(encoder, ctx):
    width = 2 * len(encoder.market.columns) + len(encoder.tenors) + 1
    return encoder.encode(ctx, 0.0)[:width]


def make_label(market, anchor, d, h, schedule, encoder, cfg, *, observe=None):
    states = []
    nets = []
    costs = []
    trades = []
    openings = []
    for action in cfg["actions"]:

        def policy(ctx, prev, action=action):
            if ctx.as_of == d:
                if ctx.curve.as_of != market.calendar.prev_trading_day(d):
                    raise ValueError("decision feature cutoff differs")
                if observe is not None:
                    observe(ctx)
                states.append(inputs(encoder, ctx))
                return action
            return schedule.get(ctx.as_of, 0.0)

        book, _ = replay(market, anchor, h, cfg["fit_band"], policy)
        openings.append(float(book.at[d, "net_nav"]))
        nets.append(float(book.at[h, "net_nav"]))
        section = book.loc[d:h].iloc[1:]
        costs.append(float(section.cost.sum()))
        trades.append(int(section.trades.sum()))
    if (
        len(states) != len(cfg["actions"])
        or not all(x == states[0] for x in states)
        or np.ptp(openings) > 1e-6
        or openings[0] <= 0
    ):
        raise ValueError("counterfactual branches do not have the same opening")
    j = cfg["actions"].index(None)
    return dict(
        signal=str(d),
        cutoff=str(market.calendar.prev_trading_day(d)),
        horizon=str(h),
        x=states[0],
        opening_net_nav=openings[0],
        net_bp=((np.array(nets) - nets[j]) * 1e4 / openings[0]).tolist(),
        delta_cost_yuan=(np.array(costs) - costs[j]).tolist(),
        trades=trades,
    )


def labels(root, fold, shard, shards):
    cfg, m, anchor, intervals, _, _, encoder = window(fold)
    if not 0 <= shard < shards:
        raise ValueError("invalid shard")
    dest = root / f"fold-{fold}/labels"
    dest.mkdir(parents=True, exist_ok=True)
    for path, cycle in enumerate(cfg["prefix_paths"]):
        schedule = {d: cycle[i % len(cycle)] for i, (d, _) in enumerate(intervals)}
        for i, (d, h) in enumerate(intervals):
            if (i * len(cfg["prefix_paths"]) + path) % shards != shard:
                continue
            row = make_label(m, anchor, d, h, schedule, encoder, cfg)
            dump(
                dest / f"{d}-{path}.json",
                dict(**row, fold=fold, path=path, design_sha256=sha(DESIGN)),
            )


def fit_heads(x, y, alpha):
    heads = []
    for col in y.T[1:]:
        m = fit_ridge(x, col, alpha)
        heads.append(
            dict(
                mean=m.mean.tolist(),
                scale=m.scale.tolist(),
                coef=m.coef.tolist(),
                intercept=m.intercept,
            )
        )
    return heads


def trained_policy(heads, encoder, actions):
    models = [
        RidgeModel(
            mean=np.array(m["mean"]),
            scale=np.array(m["scale"]),
            coef=np.array(m["coef"]),
            intercept=m["intercept"],
        )
        for m in heads
    ]

    def act(ctx, prev):
        x = np.array(inputs(encoder, ctx))[: len(models[0].mean)]
        return actions[int(np.argmax([0.0, *(m.predict(x) for m in models)]))]

    return act


def continuous(m, anchor, start, end, band, act):
    faces = {}

    def trace(roll, proposals, book):
        faces.update(
            {r.date: dict(r.post_trade_faces) for r in roll.records if start < r.date <= end}
        )

    book, _ = replay(
        m,
        anchor,
        end,
        band,
        lambda ctx, prev: act(ctx, prev) if ctx.as_of >= start else 0.0,
        audit=trace,
    )
    return book, faces


def evaluate(m, anchor, start, end, encoder, cfg, model):
    by_band = []
    for band in cfg["bands"]:
        policies = dict(
            zero=lambda ctx, prev: 0.0,
            hold=lambda ctx, prev: None,
            fixed=lambda ctx, prev: model["fixed_action"],
            market=trained_policy(model["market"], encoder, cfg["actions"]),
            inventory=trained_policy(model["inventory"], encoder, cfg["actions"]),
        )
        book, faces = continuous(m, anchor, start, end, band, policies.pop("inventory"))
        controls = {}
        for name, policy in policies.items():
            ref, ref_faces = continuous(m, anchor, start, end, band, policy)
            controls[name] = matched(book, ref, faces, ref_faces, start, end)
        by_band.append(dict(band=band, controls=controls))
    return by_band


def fit(root, fold):
    cfg, m, anchor, intervals, start, end, enc = window(fold)
    dest = root / f"fold-{fold}"
    rows = []
    market_width = 2 * len(enc.market.columns)
    names = enc.names[: market_width + len(enc.tenors) + 1]
    for d, h in intervals:
        for path in range(len(cfg["prefix_paths"])):
            r = read(dest / f"labels/{d}-{path}.json")
            if (
                r["fold"],
                r["path"],
                r["signal"],
                r["horizon"],
                r["cutoff"],
                r["design_sha256"],
            ) != (
                fold,
                path,
                str(d),
                str(h),
                str(m.calendar.prev_trading_day(d)),
                sha(DESIGN),
            ) or h >= start:
                raise ValueError("label identity or time boundary differs")
            if (
                len(r["net_bp"]) != len(cfg["actions"])
                or r["net_bp"][0] != 0
                or len(r["x"]) != len(names)
                or not np.isfinite([*r["x"], *r["net_bp"]]).all()
            ):
                raise ValueError("invalid label or features")
            rows.append(r)
    x = np.array([r["x"] for r in rows])
    y = np.array([r["net_bp"] for r in rows])
    first, last = intervals[0][0], intervals[-1][1]
    fit_paths = [
        continuous(m, anchor, first, last, cfg["fit_band"], lambda ctx, prev, z=z: z)[0]
        for z in cfg["actions"]
    ]
    ref = fit_paths[cfg["actions"].index(0.0)]
    gains = [active_bp(b, ref, first, last) for b in fit_paths]
    model = dict(
        fold=fold,
        design_sha256=sha(DESIGN),
        names=names,
        tenors=list(enc.tenors),
        encoder_mean=enc.mean.tolist(),
        encoder_scale=enc.scale.tolist(),
        fit_intervals=len(intervals),
        fit_rows=len(rows),
        distinct_face_dates=int(
            np.any(
                np.ptp(
                    x[:, market_width:-1].reshape(
                        len(intervals), len(cfg["prefix_paths"]), len(enc.tenors)
                    ),
                    axis=1,
                )
                > 0,
                axis=1,
            ).sum()
        ),
        mean_fit_delta_vs_hold_bp=y.mean(axis=0).tolist(),
        fit_continuous_bp=gains,
        fixed_action=cfg["actions"][int(np.argmax(gains))],
        inventory=fit_heads(x, y, cfg["ridge_alpha"]),
        market=fit_heads(x[:, :market_width], y, cfg["ridge_alpha"]),
    )
    dump(dest / "model.json", model)
    result = evaluate(m, anchor, start, end, enc, cfg, model)
    dump(
        dest / "inner.json",
        dict(
            fold=fold,
            model_sha256=sha(dest / "model.json"),
            start=str(start),
            end=str(end),
            bands=result,
        ),
    )


def gates(root):
    cfg = read(DESIGN)
    rows = []
    for fold in cfg["folds"]:
        p = root / f"fold-{fold}"
        m = read(p / "model.json")
        r = read(p / "inner.json")
        if (m["design_sha256"], m["fold"], r["fold"], r["model_sha256"]) != (
            sha(DESIGN),
            fold,
            fold,
            sha(p / "model.json"),
        ):
            raise ValueError("inner evaluation identity differs")
        rows.append(r["bands"])
    gains = {
        b: {
            k: median(
                next(v for v in bands if v["band"] == b)["controls"][k]["net_difference_bp"]
                for bands in rows
            )
            for k in ("zero", "hold", "fixed", "market")
        }
        for b in cfg["bands"]
    }
    return dict(
        median_bp=gains, eligible=min(gains[2].values()) > 0 and min(gains[1].values()) >= 0
    )


def confirm(root):
    cfg = read(DESIGN)
    if not gates(root)["eligible"]:
        raise ValueError("development screen failed")
    dest = root / "confirmation.json"
    if dest.exists():
        raise ValueError("confirmation already recorded")
    model_path = root / f"fold-{cfg['confirmation_fold']}/model.json"
    model = read(model_path)
    seal = read(SEAL)
    if (
        seal["design_sha256"] != sha(DESIGN)
        or model["design_sha256"] != seal["design_sha256"]
        or seal["model_sha256"] != sha(model_path)
        or model["fixed_action"] != seal["fixed_action"]
        or seal["calendar_sha256"] != sha(CALENDAR)
        or seal["runtime_sha256"] != runtime_digest(ROOT)
        or any(
            sha(root / f"fold-{f}/inner.json") != seal["inner_sha256"][str(f)] for f in cfg["folds"]
        )
    ):
        raise ValueError("frozen confirmation artifacts differ")
    plan = read(CALENDAR)
    anchor = date.fromisoformat(read(EVAL_SPEC)["anchor"])
    start, end, suffix = scheduled_window(plan, cfg, anchor)
    m = Market(end)
    m.calendar = checked_calendar(m, anchor, end, plan, suffix)
    enc = Encoder(
        m.features,
        np.array(model["encoder_mean"]),
        np.array(model["encoder_scale"]),
        tuple(model["tenors"]),
    )
    if enc.names[: len(model["names"])] != model["names"]:
        raise ValueError("forward feature schema changed")
    result = evaluate(m, anchor, start, end, enc, cfg, model)
    passed = all(
        all(
            v["net_difference_bp"] > 0 if row["band"] == 2 else v["net_difference_bp"] >= 0
            for v in row["controls"].values()
        )
        for row in result
    )
    with TemporaryDirectory(dir=root) as tmp:
        staged = Path(tmp) / dest.name
        dump(
            staged,
            dict(
                model_sha256=sha(model_path),
                seal_sha256=sha(SEAL),
                start=str(start),
                end=str(end),
                bands=result,
                passed=passed,
            ),
        )
        os.link(staged, dest)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("mode", choices=["labels", "fit", "summary", "confirm"])
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--fold", type=int)
    p.add_argument("--shard", type=int, default=0)
    p.add_argument("--shards", type=int, default=1)
    a = p.parse_args()
    if a.mode in ("labels", "fit") and a.fold is None:
        p.error("fold is required")
    if a.mode == "labels":
        labels(a.root, a.fold, a.shard, a.shards)
    elif a.mode == "fit":
        fit(a.root, a.fold)
    elif a.mode == "summary":
        dump(a.root / "summary.json", gates(a.root))
    else:
        confirm(a.root)
