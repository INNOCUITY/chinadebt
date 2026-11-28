"""Rebuild public summaries and bilingual reports; never train or replay a strategy."""

import argparse
import json
import tempfile
from pathlib import Path

from ficc.reporting.delivery import collect, render_report, sha

ROOT = Path(__file__).resolve().parents[1]
TEXT_OUTPUTS = [
    "reports/delivery_facts.json",
    "reports/acceptance_status.json",
    "reports/unified_baselines.csv",
    "reports/rl_ablation.csv",
    "reports/zh/final_report.md",
    "reports/en/final_report.md",
    "reports/delivery_manifest.json",
]
FIGURES = ["reports/delivery_baselines.png", "reports/delivery_futures.png"]


def plots(root, out, frame):
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    import numpy as np

    from ficc.reporting.delivery import read

    plt.rcParams.update({"font.family": "DejaVu Sans", "font.size": 10})
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.2), sharey=True)
    names = ["zero", "train_mean", "ridge", "tree", "ppo"]
    for band, ax in zip((2, 1), axes, strict=True):
        data = frame.loc[frame.band_lot.eq(band)]
        values = [data.loc[data.model.eq(name), "excess_bp"].median() for name in names]
        ax.bar(names, values, color=["#9ca3af", "#64748b", "#3b82f6", "#14b8a6", "#e8793a"])
        seeds = data.loc[data.model.eq("ppo"), "excess_bp"].to_numpy()
        ax.scatter(
            np.linspace(3.8, 4.2, len(seeds)),
            seeds,
            color="#1f2937",
            s=20,
            zorder=3,
            label="PPO seeds",
        )
        ax.axhline(0, color="#374151", lw=0.8)
        ax.set_title(f"{band} lot | matched 22-fold window")
        ax.set_ylabel("Cumulative net excess vs zero tilt (bp)")
        ax.legend(frameon=False)
    fig.suptitle("PPO: bars show seed median; dots show all five seeds")
    fig.tight_layout()
    fig.savefig(out / FIGURES[0], dpi=160, metadata={"Software": "ficc"})
    plt.close(fig)
    result = read(root, "reports/futures_signal_probe.json")
    fig, axes = plt.subplots(1, 2, figsize=(11, 4.2), sharey=True)
    for key, title, ax in zip(
        ("primary", "delay_sensitivity"),
        ("Previous report", "One extra session delay"),
        axes,
        strict=True,
    ):
        rows = [r for r in result[key]["rows"] if r["band"] == 2]
        x = np.arange(len(rows))
        for offset, field, label, color in [
            (-0.24, "rmse_bp", "New futures features", "#e8793a"),
            (0, "original_rmse_bp", "Original Ridge", "#3b82f6"),
            (0.24, "mean_rmse_bp", "Fit-mean forecast", "#64748b"),
        ]:
            ax.bar(x + offset, [r[field] for r in rows], width=0.24, label=label, color=color)
        ax.set_xticks(x, [f"Fold {r['fold']}" for r in rows])
        ax.set_title(title)
        ax.set_ylabel("Held-out nonzero-action RMSE (bp), 2 lot")
    axes[1].legend(frameon=False, fontsize=8)
    fig.suptitle("Futures feature probe: no fold beats both controls; development labels only")
    fig.tight_layout()
    fig.savefig(out / FIGURES[1], dpi=160, metadata={"Software": "ficc"})
    plt.close(fig)


def build(root, out, make_plots=True):
    facts, plan, baseline, ablation = collect(root)
    for name in TEXT_OUTPUTS + FIGURES:
        (out / name).parent.mkdir(parents=True, exist_ok=True)
    for name, value in [
        (TEXT_OUTPUTS[0], facts),
        (
            TEXT_OUTPUTS[1],
            {**plan, "full_original_plan_accepted": facts["full_original_plan_accepted"]},
        ),
    ]:
        (out / name).write_text(
            json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
        )
    baseline.to_csv(out / TEXT_OUTPUTS[2], index=False, float_format="%.12g")
    ablation.to_csv(out / TEXT_OUTPUTS[3], index=False, float_format="%.12g")
    for english, language in [(False, "zh"), (True, "en")]:
        (out / f"reports/{language}/final_report.md").write_text(
            render_report(facts, plan, english)
        )
    sources = {
        "config/acceptance/delivery.json",
        "reports/futures_signal_probe.json",
        "reports/futures_data_coverage.json",
        "scripts/build_report.py",
        "src/ficc/reporting/delivery.py",
        "docs/09_reproduction.md",
        "docs/05_defense_qa.md",
        "reports/curve_summary.csv",
        "reports/curve_fit_summary.csv",
    }
    sources.update(ablation.evidence.dropna())
    for item in plan["items"]:
        sources.update(item["evidence"])
    # Generated evidence is validated after generation, not hashed as its own input.
    for name in sorted(sources):
        path = out / name if name in TEXT_OUTPUTS else root / name
        if not path.is_file() or path.stat().st_size == 0:
            raise FileNotFoundError(f"missing acceptance evidence: {name}")
    sources.difference_update(TEXT_OUTPUTS)
    manifest = dict(
        schema=1,
        as_of=facts["as_of"],
        scope="Public evidence rebuild only; no raw-data replay, training or new acceptance computation.",
        inputs={s: sha(root / s) for s in sorted(sources)},
        text_outputs={s: sha(out / s) for s in TEXT_OUTPUTS[:-1]},
        figures=FIGURES,
        figure_check="Figures regenerate from the same facts; PNG bytes may vary by renderer/OS.",
    )
    (out / TEXT_OUTPUTS[-1]).write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
    if make_plots:
        plots(root, out, baseline)
    return facts


def check(root):
    with tempfile.TemporaryDirectory() as temporary:
        out = Path(temporary)
        build(root, out, make_plots=False)
        changed = [
            p
            for p in TEXT_OUTPUTS
            if not (root / p).exists() or (root / p).read_bytes() != (out / p).read_bytes()
        ]
        missing_figures = [p for p in FIGURES if not (root / p).is_file()]
        if changed or missing_figures:
            raise ValueError(
                f"delivery drift or missing figures: {changed + missing_figures}; run make reproduce"
            )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--check", action="store_true")
    parser.add_argument("--output-root", type=Path)
    args = parser.parse_args()
    if args.check:
        check(ROOT)
        print("Public delivery text, source hashes and figure presence verified.")
    else:
        facts = build(ROOT, args.output_root or ROOT)
        print(
            {
                "delivery_built": True,
                "full_original_plan_accepted": facts["full_original_plan_accepted"],
            }
        )
