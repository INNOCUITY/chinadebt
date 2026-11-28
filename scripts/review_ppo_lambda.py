"""Verify lambda-one archives, compare matched controls, and plot registered checkpoints."""

import argparse
from collections import Counter
from itertools import product
from pathlib import Path

import matplotlib
import numpy as np
import torch
from audit_ppo_value import audit
from evaluate_rl import sha
from run_critic_budget import plateau, read, summarize
from train_rl import CRITIC_BUDGET, dump

from ficc.rl.ppo import ActorCritic

matplotlib.use("Agg")
from matplotlib import pyplot as plt

STEPS = [2, 4, 8, 12, 16]
PAIRS = list(product([0, 10, 21], [20261111, 20261112]))


def close_tree(a, b):
    """CPU archive replay permits small floating-point differences, never identity changes."""
    if isinstance(a, dict):
        assert a.keys() == b.keys()
        for k in a:
            close_tree(a[k], b[k])
    elif isinstance(a, list):
        assert len(a) == len(b)
        for x, y in zip(a, b, strict=True):
            close_tree(x, y)
    elif isinstance(a, float):
        assert np.isclose(a, b, atol=2e-6, rtol=2e-5), (a, b)
    else:
        assert a == b, (a, b)


def probabilities(folder):
    ck = folder / "training"
    identity = torch.load(ck / "update-16.pt", map_location="cpu", weights_only=False)["identity"]
    spec = identity["spec"]
    samples = [r for r in read(ck / "trajectory-1.json")["decisions"] if r["actor_sample"]]
    obs = torch.tensor([r["obs"] for r in samples], dtype=torch.float32)
    model = ActorCritic(obs.shape[1], spec["hidden"], actions=spec["actions"])
    history = read(folder / "training_curve.json")
    assert [r["update"] for r in history] == STEPS
    previous = None
    for row in history:
        saved = torch.load(
            ck / f"update-{row['update']}.pt", map_location="cpu", weights_only=False
        )
        assert saved["identity"] == identity and saved["completed"] == row["update"]
        model.load_state_dict(saved["model"])
        with torch.inference_mode():
            p = model.distribution(obs).probs
        if previous is not None:
            tv = float((p - previous).abs().sum(1).mean() / 2)
            assert abs(tv - row["mean_action_tv"]) < 1e-6
            switches = int((p.argmax(1) != previous.argmax(1)).sum())
            if "argmax_switches" in row:
                assert row["argmax_switches"] == switches
            row["argmax_switches"] = switches
        means = p.mean(0).tolist()
        if "mean_action_probabilities" in row:
            np.testing.assert_allclose(means, row["mean_action_probabilities"], atol=1e-7, rtol=0)
        row["mean_action_probabilities"] = means
        row["fixed_observations"] = len(obs)
        previous = p
    return history


def review(root, controls, execution, neutral_execution):
    neutral = controls / "ppo-neutral-2fac5ad"
    summary = summarize(root, True, True, True, True)
    assert read(root / "summary.json") == {
        **summary,
        "paired": {str(k): v for k, v in summary["paired"].items()},
    }
    diagnostics = read(root / "value_diagnostics.json")["results"]
    baseline = read(Path("reports/ppo_value_diagnostics.json"))["results"]

    def keyed(rows):
        return {(r["fold"], r["seed"], r["update"]): r for r in rows}

    new_values, old_values = keyed(diagnostics), keyed(baseline)
    assert len(new_values) == len(old_values) == 30
    pairs, holdings, errors = [], [], []
    for fold, seed in PAIRS:
        sub = Path(f"fold-{fold}/seed-{seed}")
        folder = root / sub
        ck = folder / "training"
        assert read(ck / "trajectory-1.json") == read(neutral / sub / "training/trajectory-1.json")
        current, old = probabilities(folder), probabilities(neutral / sub)
        validation = read(folder / "validation.json")
        assert plateau(current, read(CRITIC_BUDGET)) == validation["plateau"]
        assert len(list(ck.glob("metrics-*.json"))) == 16
        for step in range(1, 17):
            m = read(ck / f"metrics-{step}.json")
            assert m["update"] == step and m["device"] == "cuda"
            errors.append(abs(m["closure_error_yuan"]))
        values = []
        for step in STEPS:
            audited = audit(ck, step, "cpu")
            close_tree(audited, new_values[fold, seed, step])
            values.append(
                {"update": step, "lambda_one": audited, "lambda_095": old_values[fold, seed, step]}
            )
        for r in validation["rows"]:
            assert r["checkpoint_sha256"] == sha(ck / "update-16.pt")
            errors.append(abs(r["ledger_error_yuan"]))
            refs = [
                ("gain_vs_neutral_16", "ppo-neutral-2fac5ad", "rows", "band", 16),
                ("gain_vs_slow_16", "ppo-actor-step-95ddf19", "rows", "band", 16),
                ("gain_vs_invested_16", "ppo-invested-0164e07", "rows", "band", 16),
                ("gain_vs_cash_16", "ppo-convergence-eb9d861", "rows", "band", 16),
                ("gain_vs_2", "ppo-critic-8a8f752/active", "rows", "band_lots", 2),
                ("gain_vs_continuous", "ppo-factorial-0a529c7", "results", "band_lots", 2),
            ]
            for key, name, field, band_key, update in refs:
                refdir = controls / name / sub
                if name == "ppo-factorial-0a529c7":
                    refdir /= "total"
                (ref,) = [
                    v
                    for v in read(refdir / "validation.json")[field]
                    if v[band_key] == r["band"] and v["update"] == update
                ]
                assert (
                    abs(r[key] - r["net_excess_vs_zero_bp"] + ref["net_excess_vs_zero_bp"]) < 1e-8
                )
            band = r["band"]
            new_dir = execution / f"fold-{fold}/band-{band}"
            old_dir = neutral_execution / f"fold-{fold}/band-{band}"
            n, o = read(new_dir / f"lambda-{seed}.json"), read(old_dir / f"neutral-{seed}.json")
            assert n["checkpoint_sha256"] == sha(ck / "update-16.pt")
            assert o["checkpoint_sha256"] == sha(neutral / sub / "training/update-16.pt")
            nd, od = (
                read(new_dir / f"lambda-{seed}-days.json"),
                read(old_dir / f"neutral-{seed}-days.json"),
            )
            assert [v["date"] for v in nd] == [v["date"] for v in od]
            assert all(x["zero_faces"] == y["zero_faces"] for x, y in zip(nd, od, strict=True))
            assert abs(n["metrics"]["net_excess_vs_zero_bp"] - r["net_excess_vs_zero_bp"]) < 1e-7
            assert (
                abs(
                    n["metrics"]["net_excess_vs_zero_bp"]
                    - o["metrics"]["net_excess_vs_zero_bp"]
                    - r["gain_vs_neutral_16"]
                )
                < 1e-7
            )
            action_names = {None: "hold", 0.0: "zero", -1.0: "short", 1.0: "long"}
            holdings.append(
                dict(
                    fold=fold,
                    seed=seed,
                    band=band,
                    days=len(nd),
                    different_faces_vs_zero=sum(not v["same_faces"] for v in nd),
                    different_faces_vs_lambda095=sum(
                        x["post_trade_faces"] != y["post_trade_faces"]
                        for x, y in zip(nd, od, strict=True)
                    ),
                    actions=dict(
                        Counter(action_names[v["proposal_z"]] for v in nd if v["decision"])
                    ),
                    trades=n["metrics"]["trades"],
                    lambda095_trades=o["metrics"]["trades"],
                    net_excess_bp=r["net_excess_vs_zero_bp"],
                    gain_vs_lambda095_bp=r["gain_vs_neutral_16"],
                    gain_vs_continuous_bp=r["gain_vs_continuous"],
                    net_pnl_gain_vs_lambda095_yuan=n["execution"]["pnl_vs_zero_yuan"]
                    - o["execution"]["pnl_vs_zero_yuan"],
                    cost_gain_vs_lambda095_yuan=sum(
                        x["cost_yuan"] - y["cost_yuan"] for x, y in zip(nd, od, strict=True)
                    ),
                    mean_abs_dv01_vs_zero=n["execution"]["mean_abs_dv01_vs_zero"],
                )
            )
        errors.extend(abs(r["ledger_error_yuan"]) for r in current)
        pairs.append(
            dict(
                fold=fold,
                seed=seed,
                plateau=validation["plateau"],
                lambda_one=current,
                lambda_095=old,
                values=values,
            )
        )
    assert len(holdings) == 12 and max(errors) < 1e-5
    return dict(
        checkpoints=STEPS,
        summary=summary,
        pairs=pairs,
        holdings=holdings,
        max_ledger_residual_yuan=max(errors),
        value_scope="Pre-update and same-batch post-update MC errors on each policy's own sampled trajectory. Different trajectories prevent interpreting cross-run RMSE differences as a controlled critic-only effect.",
    )


def plot(report, output):
    output.mkdir(parents=True, exist_ok=True)
    for kind in ("value", "training", "probabilities"):
        fig, axes = plt.subplots(3, 2, figsize=(11, 9), sharex=True, layout="constrained")
        for ax, pair in zip(axes.flat, report["pairs"], strict=True):
            ax.set_title(
                f"Fold {pair['fold']} / {pair['seed']}  |  plateau: {pair['plateau']}",
                loc="left",
                fontsize=10,
            )
            for arm, style, label, color in [
                ("lambda_one", "-", "lambda=1", "#24649a"),
                ("lambda_095", "--", "lambda=0.95", "#c4712c"),
            ]:
                rows = pair[arm]
                if kind == "training":
                    ax.plot(
                        STEPS,
                        [r["train_active_bp"] for r in rows],
                        style,
                        color=color,
                        marker="o",
                        label=label,
                    )
                elif kind == "value":
                    for timing, marker in [("before", "o"), ("after", "s")]:
                        vals = [
                            v[arm][f"monte_carlo_{timing}"]["rmse"] * 1e4 / v[arm]["reward_scale"]
                            for v in pair["values"]
                        ]
                        ax.plot(
                            STEPS,
                            vals,
                            style,
                            color=color,
                            marker=marker,
                            alpha=1 if timing == "before" else 0.5,
                            label=f"{label}, {timing}",
                        )
                else:
                    for k, (name, col) in enumerate(
                        zip(
                            ["Hold", "Zero", "Short", "Long"],
                            ["#75528b", "#53616b", "#bd5933", "#138474"],
                            strict=True,
                        )
                    ):
                        ax.plot(
                            STEPS,
                            [r["mean_action_probabilities"][k] * 100 for r in rows],
                            style,
                            color=col,
                            marker="o" if arm == "lambda_one" else None,
                            ms=3,
                            label=name if arm == "lambda_one" else None,
                        )
            ax.set_xticks(STEPS)
            ax.spines[["top", "right"]].set_visible(False)
            ax.grid(alpha=0.18)
            if kind == "training":
                ax.axhline(0, color="0.5", lw=0.7)
        titles = {
            "value": "Critic Monte Carlo RMSE (bp): before / same-batch after update",
            "training": "Deterministic training net excess vs zero tilt (bp)",
            "probabilities": "Fixed observations: mean action probability (%) | solid: lambda=1; dashed: 0.95",
        }
        fig.supxlabel("Completed updates")
        fig.legend(
            *axes[0, 0].get_legend_handles_labels(),
            title=titles[kind],
            title_fontsize=11,
            fontsize=9,
            loc="outside upper center",
            ncol=4,
            frameon=False,
        )
        fig.savefig(output / f"ppo_lambda_{kind}.png", dpi=160)
        plt.close(fig)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--controls", type=Path, required=True)
    p.add_argument("--execution", type=Path, required=True)
    p.add_argument("--neutral-execution", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    a = p.parse_args()
    torch.set_num_threads(1)
    report = review(a.root, a.controls, a.execution, a.neutral_execution)
    dump(a.output, report)
    plot(report, a.output.parent)
    print(
        {
            "plateaus": report["summary"]["plateaus"],
            "expand": report["summary"]["expand_validation"],
        }
    )
