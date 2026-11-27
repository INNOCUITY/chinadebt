"""Compare a frozen Ridge switch with a one-time hold from the same actual portfolio."""

import argparse
from datetime import date
from pathlib import Path

from audit_action_values import read
from evaluate_rl import replay, sha
from run_action_value_probe import DESIGN, active_bp, probe_window, training_split
from train_rl import dump


def run(paths, out, fold, band, shard=0, shards=1):
    design = read(DESIGN)
    if band not in design["replay_bands"] or not 0 <= shard < shards:
        raise ValueError("invalid switch diagnostic partition")
    market, anchor, grid, _ = probe_window(fold, design)
    _, start, end = training_split(grid)
    ref = read(paths / f"fold-{fold}/band-{band}/continuous.json")
    if (ref["fold"], ref["band"], ref["design_sha256"]) != (fold, band, sha(DESIGN)):
        raise ValueError("continuous policy identity mismatch")
    picks = {date.fromisoformat(r["signal"]): r["selected_index"] for r in ref["decisions"]}
    dates = [d for d in grid if start <= d < end]
    if list(picks) != dates:
        raise ValueError("incomplete Ridge schedule")
    acts = design["actions"]
    zero, _ = replay(market, anchor, end, band, lambda ctx, prev: 0.0)

    def action(ctx, prev):
        return acts[picks[ctx.as_of]] if ctx.as_of >= start else 0.0

    selected, _ = replay(market, anchor, end, band, action)
    if (
        abs(active_bp(selected, zero, start, end) - ref["comparisons"]["zero"]["net_difference_bp"])
        > 1e-6
    ):
        raise ValueError("frozen continuous ledger mismatch")
    prev = design["actions"].index(0.0)
    switches = []
    for r in ref["decisions"]:
        idx = r["selected_index"]
        if idx != prev:
            switches.append(r)
        prev = idx
    rows = []
    for r in switches[shard::shards]:
        signal, horizon = map(date.fromisoformat, (r["signal"], r["horizon"]))
        held, _ = replay(
            market,
            anchor,
            horizon,
            band,
            lambda ctx, prev, signal=signal: None if ctx.as_of == signal else action(ctx, prev),
        )
        # At the intervention the two books have the same state and the same entire prefix.
        net = active_bp(selected, held, signal, horizon)
        days = held.loc[signal:horizon].index[1:]
        rows.append(
            dict(
                signal=r["signal"],
                horizon=r["horizon"],
                selected_index=r["selected_index"],
                selected_minus_hold_bp=net,
                selected_trades=int(selected.loc[days, "trades"].sum()),
                hold_trades=int(held.loc[days, "trades"].sum()),
                incremental_cost_yuan=float(
                    (selected.loc[days, "cost"] - held.loc[days, "cost"]).sum()
                ),
            )
        )
    dest = out / f"fold-{fold}/band-{band}"
    dest.mkdir(parents=True, exist_ok=True)
    dump(
        dest / f"switches-{shard}.json",
        dict(
            fold=fold,
            band=band,
            shard=shard,
            shards=shards,
            total_switches=len(switches),
            fit_sha256=ref["fit_sha256"],
            rows=rows,
        ),
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--paths", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--fold", type=int, required=True)
    p.add_argument("--band", choices=[1, 2], type=int, required=True)
    p.add_argument("--shard", type=int, default=0)
    p.add_argument("--shards", type=int, default=1)
    a = p.parse_args()
    run(a.paths, a.output, a.fold, a.band, a.shard, a.shards)
