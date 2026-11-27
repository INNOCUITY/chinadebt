"""Measure a two-signal confirmation rule on already reviewed inner training ledgers."""

import argparse
from datetime import date
from pathlib import Path

from audit_action_values import matched, read
from evaluate_rl import replay, sha
from run_action_value_probe import DESIGN, probe_window, training_split
from train_rl import dump


def confirmed_targets(indices, zero_index):
    confirmed = zero_index
    previous = zero_index
    for idx in indices:
        if idx == previous:
            confirmed = idx
        yield confirmed
        previous = idx


def run(paths, output, fold, band):
    design = read(DESIGN)
    if band not in design["replay_bands"]:
        raise ValueError("unregistered band")
    market, anchor, grid, _ = probe_window(fold, design)
    _, start, end = training_split(grid)
    ref = read(paths / f"fold-{fold}/band-{band}/continuous.json")
    if (ref["fold"], ref["band"], ref["design_sha256"]) != (fold, band, sha(DESIGN)):
        raise ValueError("source mismatch")
    ds = ref["decisions"]
    dates = [date.fromisoformat(r["signal"]) for r in ds]
    if dates != [d for d in grid if start <= d < end]:
        raise ValueError("decision dates changed")
    acts = design["actions"]
    raw = [r["selected_index"] for r in ds]
    indices = list(confirmed_targets(raw, acts.index(0.0)))
    schedule = dict(zip(dates, [acts[i] for i in indices], strict=True))

    def simulate(lookup):
        faces = {}

        def trace(roll, proposals, book):
            faces.update(
                {r.date: dict(r.post_trade_faces) for r in roll.records if start < r.date <= end}
            )

        book, _ = replay(
            market, anchor, end, band, lambda ctx, prev: lookup.get(ctx.as_of, 0.0), audit=trace
        )
        return book, faces

    zero, zfaces = simulate({})
    book, faces = simulate(schedule)
    row = matched(book, zero, faces, zfaces, start, end)
    ridge = ref["comparisons"]["zero"]["net_difference_bp"]
    gain = row["net_difference_bp"] - ridge
    dest = output / f"fold-{fold}/band-{band}"
    dest.mkdir(parents=True, exist_ok=True)
    dump(
        dest / "confirmation.json",
        dict(
            fold=fold,
            band=band,
            controls=ref["comparisons"],
            vs_zero=row,
            raw_indices=raw,
            confirmed_indices=indices,
            gain_vs_ridge_bp=gain,
            gain_vs_label_fixed_bp=gain + ref["comparisons"]["label_fixed"]["net_difference_bp"],
            gain_vs_continuous_fixed_bp=gain
            + ref["comparisons"]["continuous_fixed"]["net_difference_bp"],
        ),
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--paths", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--fold", type=int, required=True)
    p.add_argument("--band", choices=[1, 2], type=int, required=True)
    a = p.parse_args()
    run(a.paths, a.output, a.fold, a.band)
