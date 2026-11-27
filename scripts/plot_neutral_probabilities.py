"""Plot four categorical action probabilities on each pair's fixed first-rollout observations."""

import argparse
from itertools import product
from pathlib import Path

import matplotlib
import numpy as np
import torch
from evaluate_rl import sha
from run_critic_budget import read
from train_rl import CRITIC_BUDGET, GRID_SPEC, training_spec

from ficc.rl.ppo import ActorCritic

matplotlib.use("Agg")
from matplotlib import pyplot as plt


@torch.inference_mode()
def curves(root, fold, seed):
    folder = root / f"fold-{fold}/seed-{seed}"
    spec = training_spec(
        fold,
        seed,
        action_grid=True,
        grid_active=True,
        zero_critic=True,
        critic_budget=True,
        invested_start=True,
        actor_step=True,
        neutral_actor=True,
    )
    ck = folder / "training"
    checks = read(folder / "training_curve.json")
    if [r["update"] for r in checks] != read(CRITIC_BUDGET)["checkpoints"]:
        raise ValueError("checkpoint schedule mismatch")
    if {r["checkpoint_sha256"] for r in read(folder / "validation.json")["rows"]} != {
        sha(ck / "update-16.pt")
    }:
        raise ValueError("validation checkpoint mismatch")
    samples = [r for r in read(ck / "trajectory-1.json")["decisions"] if r["actor_sample"]]
    obs = torch.tensor([r["obs"] for r in samples], dtype=torch.float32)
    model = ActorCritic(obs.shape[1], spec["hidden"], actions=spec["actions"])
    initial = torch.zeros(len(spec["actions"]))
    initial[spec["actions"].index(0.0)] = spec["zero_tilt_logit"]
    means = [initial.softmax(0).tolist()]
    prev = None
    for row in checks:
        step = row["update"]
        state = torch.load(ck / f"update-{step}.pt", map_location="cpu", weights_only=False)
        if state["completed"] != step or state["identity"]["spec"] != spec:
            raise ValueError("checkpoint identity mismatch")
        model.load_state_dict(state["model"])
        probs = model.distribution(obs).probs
        if prev is not None:
            tv = float((probs - prev).abs().sum(1).mean() / 2)
            if abs(tv - row["mean_action_tv"]) > 1e-6:
                raise ValueError("fixed-observation TV differs from archive")
        means.append(probs.mean(0).tolist())
        prev = probs
    return [0, *[r["update"] for r in checks]], np.array(means) * 100


def plot(root, output):
    design = read(GRID_SPEC)
    fig, axes = plt.subplots(
        3, 2, figsize=(8.8, 6.5), sharex=True, sharey=True, layout="constrained"
    )
    styles = {
        None: ("Hold", "#765082"),
        0.0: ("Zero tilt", "#4c5760"),
        -1.0: ("Short duration", "#c45525"),
        1.0: ("Long duration", "#147f85"),
    }
    for ax, (fold, seed) in zip(axes.flat, product(design["folds"], design["seeds"]), strict=True):
        x, means = curves(root, fold, seed)
        for z, ys in zip(design["actions"], means.T, strict=True):
            label, color = styles[z]
            ax.plot(x, ys, color=color, marker="o", ms=3, lw=1.6, label=label)
        ax.set_title(f"Fold {fold} / seed {seed}", loc="left", fontsize=10)
        ax.set_xticks(x)
        ax.spines[["top", "right"]].set_visible(False)
        ax.grid(axis="y", alpha=0.2)
    fig.supxlabel("Completed PPO updates (0 = initialization)")
    fig.supylabel("Mean probability on fixed training observations (%)")
    fig.legend(
        *axes[0, 0].get_legend_handles_labels(), loc="outside upper center", ncol=4, frameon=False
    )
    fig.savefig(output, dpi=170)
    plt.close(fig)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    parser.add_argument("output", type=Path)
    args = parser.parse_args()
    plot(args.root, args.output)
