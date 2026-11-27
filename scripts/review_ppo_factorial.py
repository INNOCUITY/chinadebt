"""Read the complete frozen matrix and its paired first-update trajectories."""

import argparse
import json
from itertools import combinations, product
from pathlib import Path
from statistics import mean, median

import numpy as np
from summarize_ppo_factorial import PLAN, summarize

from ficc.rl.ppo import advantages


def review(root):
    gate = summarize(root)
    design = json.loads(PLAN.read_text())
    metrics = {b: {a: [] for a in design["arms"]} for b in design["bands"]}
    interactions = {b: {} for b in metrics}
    training = {a: [] for a in design["arms"]}
    for fold, seed in product(design["folds"], design["seeds"]):
        base = root / f"fold-{fold}/seed-{seed}"
        for arm in design["arms"]:
            p = base / arm
            rows = json.loads((p / "validation.json").read_text())["results"]
            for row in rows:
                metrics[row["band_lots"]][arm].append(row)
            trace = json.loads((p / "training/trajectory-1.json").read_text())
            s = [r for r in trace["decisions"] if r["actor_sample"]]
            dates = [r["date"] for r in s]
            adv = advantages(
                [r["training_reward"] for r in s],
                [r["value"] for r in s],
                gae_lambda=0.95,
                terminated=True,
            )
            if arm == "total":
                ref_dates = dates
                ref = adv
            if dates != ref_dates or min(adv.std(), ref.std()) <= 0:
                raise ValueError("cannot compare first-update advantage vectors")
            m = json.loads((p / "training/metrics-2.json").read_text())
            training[arm].append(
                dict(
                    first_gae_sd=float(adv.std()),
                    first_gae_corr_vs_total=float(np.corrcoef(ref, adv)[0, 1]),
                    update2_value_loss_native=m["value_loss"],
                )
            )

    def effects(band):
        factor_names = ["active", "scale", "clip"]
        for size in [2, 3]:
            for axes in combinations(range(3), size):
                gains = []
                for j in range(len(design["folds"]) * len(design["seeds"])):
                    contrast = 0
                    for arm, settings in design["arms"].items():
                        signs = [
                            1 if x != off else -1
                            for x, off in zip(settings, [False, 1, None], strict=True)
                        ]
                        contrast += (
                            np.prod([signs[i] for i in axes])
                            * metrics[band][arm][j]["net_excess_vs_zero_bp"]
                        )
                    gains.append(float(contrast / 2 ** (3 - size)))
                interactions[band][" x ".join(factor_names[i] for i in axes)] = median(gains)

    for b in metrics:
        effects(b)
    summary = {}
    for b, arms in metrics.items():
        summary[b] = {}
        for arm, rows in arms.items():
            net = [r["net_excess_vs_zero_bp"] for r in rows]
            summary[b][arm] = dict(
                net_bp=net,
                median_bp=median(net),
                positive=sum(round(v, 8) > 0 for v in net),
                no_trades=sum(r["trades"] == 0 for r in rows),
                trades=sum(r["trades"] for r in rows),
                mean_abs_action=mean(r["mean_abs_action"] for r in rows),
            )
    return dict(
        **gate,
        absolute_vs_zero=summary,
        median_interaction_bp=interactions,
        training={
            a: {k: median([r[k] for r in rows]) for k in rows[0]} for a, rows in training.items()
        },
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("root", type=Path)
    print(json.dumps(review(p.parse_args().root), indent=2, allow_nan=False))
