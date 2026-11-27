"""Replay six reachable portfolio paths on frozen fit intervals and compare action values."""

import argparse
import json
from pathlib import Path

import numpy as np
from evaluate_rl import sha
from inventory_forward import runtime_digest
from run_inventory_value import DESIGN, make_label, read, window
from train_rl import ROOT, dump

PLAN = ROOT / "config/experiments/inventory_coverage.json"


def context_state(ctx):
    return dict(
        faces=sorted((p.bond.code, float(p.face_value)) for p in ctx.positions if p.face_value),
        lagged_cash=float(ctx.cash),
        lagged_nav=float(ctx.nav),
    )


def identity(market, cfg):
    return dict(
        plan_sha256=sha(PLAN),
        design_sha256=sha(DESIGN),
        runtime_sha256=runtime_digest(ROOT),
        data_sha256=market.training_hash({"train": ["", cfg["end"]]}),
    )


def collect(root, fold, band, shard, shards):
    plan = read(PLAN)
    if fold not in plan["folds"] or band not in plan["bands"] or not 0 <= shard < shards:
        raise ValueError("unregistered fold, band or shard")
    cfg, market, anchor, intervals, start, _, encoder = window(fold)
    cfg = {**cfg, "fit_band": band}
    fingerprint = identity(market, {"end": str(intervals[-1][1])})
    dest = root / f"fold-{fold}/band-{band}"
    dest.mkdir(parents=True, exist_ok=True)
    for i, (signal, horizon) in enumerate(intervals):
        for path, cycle in enumerate(plan["paths"]):
            if (i * len(plan["paths"]) + path) % shards != shard:
                continue
            file = dest / f"{signal}-{path}.json"
            expected = dict(
                **fingerprint,
                fold=fold,
                band=band,
                path=path,
                signal=str(signal),
                horizon=str(horizon),
                fit_end=str(start),
            )
            if file.exists():
                saved = read(file)
                if any(saved[k] != v for k, v in expected.items()):
                    raise ValueError("existing coverage label identity differs")
                continue
            schedule = {d: cycle[j % len(cycle)] for j, (d, _) in enumerate(intervals)}
            states = []
            row = make_label(
                market,
                anchor,
                signal,
                horizon,
                schedule,
                encoder,
                cfg,
                observe=lambda ctx, states=states: states.append(context_state(ctx)),
            )
            if len(states) != 4 or any(s != states[0] for s in states):
                raise ValueError("counterfactual portfolio states differ")
            dump(file, {**row, **expected, "state": states[0]})
            print(f"completed fold={fold} band={band} signal={signal} path={path}", flush=True)


def date_summary(rows, tolerance):
    groups = {}
    faces = set()
    for row in rows:
        state = row["state"]
        faces.add(json.dumps(state["faces"]))
        key = json.dumps([state, row["opening_net_nav"]], sort_keys=True)
        if key in groups and not np.allclose(
            groups[key]["net_bp"], row["net_bp"], atol=tolerance, rtol=0
        ):
            raise ValueError("duplicate state has inconsistent action values")
        groups[key] = row
    labels = np.array([v["net_bp"] for v in groups.values()])
    best = labels.max(axis=1)
    common = labels.mean(axis=0)
    return dict(
        distinct_faces=len(faces),
        distinct_states=len(groups),
        no_common_best=not bool(np.any(np.all(best[:, None] - labels <= tolerance, axis=0))),
        hindsight_gain_bp=float(best.mean() - common.max()),
        best_indices=[
            np.flatnonzero(b - y <= tolerance).tolist() for b, y in zip(best, labels, strict=True)
        ],
        mean_action_net_bp=common.tolist(),
    )


def summarize(root):
    plan = read(PLAN)
    result = []
    for fold in plan["folds"]:
        _, market, _, intervals, _, _, _ = window(fold)
        fingerprint = identity(market, {"end": str(intervals[-1][1])})
        for band in plan["bands"]:
            dates = []
            for signal, horizon in intervals:
                rows = []
                for path in range(len(plan["paths"])):
                    row = read(root / f"fold-{fold}/band-{band}/{signal}-{path}.json")
                    expected = {
                        **fingerprint,
                        "fold": fold,
                        "band": band,
                        "path": path,
                        "signal": str(signal),
                        "horizon": str(horizon),
                    }
                    if any(row[k] != v for k, v in expected.items()):
                        raise ValueError("coverage identity mismatch")
                    if len(row["net_bp"]) != 4 or not np.isfinite(row["net_bp"]).all():
                        raise ValueError("invalid action labels")
                    rows.append(row)
                dates.append(
                    dict(signal=str(signal), **date_summary(rows, plan["tie_tolerance_bp"]))
                )
            gains = [d["hindsight_gain_bp"] for d in dates]
            result.append(
                dict(
                    fold=fold,
                    band=band,
                    intervals=len(dates),
                    no_common_best_dates=sum(d["no_common_best"] for d in dates),
                    mean_hindsight_gain_bp=float(np.mean(gains)),
                    quantiles_bp=np.quantile(gains, [0, 0.5, 0.9, 1]).tolist(),
                    dates=dates,
                )
            )
    dump(root / "summary.json", {"plan_sha256": sha(PLAN), "results": result})


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["collect", "summary"])
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--fold", type=int)
    parser.add_argument("--band", type=int)
    parser.add_argument("--shard", type=int, default=0)
    parser.add_argument("--shards", type=int, default=1)
    args = parser.parse_args()
    if args.mode == "collect":
        collect(args.root, args.fold, args.band, args.shard, args.shards)
    else:
        summarize(args.root)
