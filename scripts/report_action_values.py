"""Collect all six held-out action diagnostics and plot additive net gains against zero tilt."""

import argparse
import json
from pathlib import Path

import matplotlib
import numpy as np
from run_action_value_probe import DESIGN, summarize

matplotlib.use("Agg")
from matplotlib import pyplot as plt


def report(audit, archive, output, figure):
    cfg = json.loads(DESIGN.read_text())
    gate = summarize(archive)
    pairs = []
    fig, axes = plt.subplots(2, 3, figsize=(9, 5), sharex=True, layout="constrained")
    for col, fold in enumerate(cfg["folds"]):
        for row, band in enumerate(cfg["replay_bands"]):
            obj = json.loads((audit / f"fold-{fold}/band-{band}/audit.json").read_text())
            if (obj["fold"], obj["band"]) != (fold, band):
                raise ValueError("diagnostic pair mismatch")
            pairs.append(obj)
            decisions = obj["decisions"]
            ax = axes[row, col]
            for values, label, style in [
                ([r["path_vs_zero_bp"] for r in decisions], "Continuous portfolio", "-"),
                (
                    [r["weight"] * r["selected_bp"] for r in decisions],
                    "Reset to zero-tilt holdings",
                    "--",
                ),
            ]:
                ax.plot(
                    np.arange(len(values) + 1),
                    np.r_[0, np.cumsum(values)],
                    style,
                    label=label,
                    lw=1.8,
                )
            ax.set_title(f"Fold {fold} · {band} lot", loc="left")
            ax.axhline(0, color="0.5", lw=0.6)
            ax.spines[["top", "right"]].set_visible(False)
            ax.grid(axis="y", alpha=0.2)
    fig.supylabel("Cumulative net difference vs zero tilt (bp)")
    fig.supxlabel("Completed holdout decision intervals")
    fig.legend(
        *axes[0, 0].get_legend_handles_labels(), loc="outside upper center", ncol=2, frameon=False
    )
    fig.savefig(figure, dpi=170)
    plt.close(fig)
    output.write_text(
        json.dumps(
            dict(frozen_gate=gate, action_order=cfg["actions"], pairs=pairs),
            allow_nan=False,
            indent=2,
        )
        + "\n"
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    for arg in ("audit", "archive", "output", "figure"):
        p.add_argument(arg, type=Path)
    a = p.parse_args()
    report(a.audit, a.archive, a.output, a.figure)
