"""Plot six registered training curves and paired validation medians from a budget review JSON."""

import argparse
import json
from pathlib import Path
from statistics import median

import matplotlib

matplotlib.use("Agg")
import numpy as np
from matplotlib import pyplot as plt


def plot(report, output):
    fig, (net, tv, validation) = plt.subplots(3, 1, figsize=(8.3, 9.4), layout="constrained")
    colors = ["#17619c", "#c05e23", "#26815c"]
    steps = report["checkpoints"]
    for i, row in enumerate(report["pairs"]):
        style = dict(
            color=colors[i // 2],
            linestyle="-" if i % 2 == 0 else "--",
            marker="o" if i % 2 == 0 else "s",
            ms=4,
            lw=1.8,
        )
        net.plot(steps, row["net"], label=f"Fold {row['fold']} / {row['seed']}", **style)
        tv.plot(steps[1:], row["tv"][1:], **style)
    net.set_title("Deterministic training excess vs zero tilt", loc="left")
    net.set_ylabel("Net excess (bp)")
    net.axhline(0, color="0.5", linewidth=0.7)
    net.legend(ncol=3, fontsize=8, loc="upper center", bbox_to_anchor=(0.5, 1.29), frameon=False)
    tv.set_title("Action probabilities on fixed training observations", loc="left")
    tv.set_ylabel("Mean total variation")
    tv.axhline(0.02, ls=":", color="black", label="Plateau limit 0.02")
    tv.legend(loc="upper left", frameon=False, fontsize=9)
    for ax, ticks in [(net, steps), (tv, steps[1:])]:
        ax.set_xticks(ticks)
        ax.set_xlabel("Completed PPO updates")
    validation.set_title("Update 16: median paired net validation excess", loc="left")
    keys = ["gain_vs_cash_16", "gain_vs_2", "gain_vs_continuous", "net_excess_vs_zero_bp"]
    labels = ["Cash start, update 16", "Original update 2", "Continuous PPO", "Zero tilt"]
    if "gain_vs_invested_16" in report["summary"]["paired"]["2"]:
        keys.insert(0, "gain_vs_invested_16")
        labels.insert(0, "Invested start, standard step (16)")
    positions = np.arange(len(keys))
    for band, offset, color in [("2", -0.16, "#17619c"), ("1", 0.16, "#a54d27")]:
        vals = [median(report["summary"]["paired"][band][key]) for key in keys]
        bars = validation.barh(positions + offset, vals, 0.29, label=f"{band} lot", color=color)
        validation.bar_label(bars, fmt="%.2f", padding=3, fontsize=8)
    validation.set_yticks(positions, labels)
    validation.invert_yaxis()
    low, high = validation.get_xlim()
    padding = max((high - low) * 0.13, 0.5)
    validation.set_xlim(low - padding, high + padding)
    validation.set_xlabel("Median difference (bp), six pairs")
    validation.axvline(0, lw=0.8, color="0.4")
    validation.legend(
        loc="upper right", bbox_to_anchor=(1.0, 1.18), ncol=2, frameon=False, fontsize=9
    )
    for ax in (net, tv, validation):
        ax.spines[["top", "right"]].set_visible(False)
        ax.grid(axis="x" if ax is validation else "y", alpha=0.16)
    fig.savefig(output, dpi=170)
    plt.close(fig)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("report", type=Path)
    p.add_argument("output", type=Path)
    args = p.parse_args()
    plot(json.loads(args.report.read_text()), args.output)
