"""Replay archived PPO updates and measure value calibration and policy changes on their samples."""

import argparse
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace

import numpy as np
import torch
from evaluate_rl import sha
from train_rl import dump, make_optimizer

from ficc.rl.ppo import ActorCritic, advantages, update


def value_metrics(prediction, target):
    error = prediction - target
    variance = float(np.var(target))
    return dict(
        target_mean=float(np.mean(target)),
        target_sd=float(np.std(target)),
        prediction_mean=float(np.mean(prediction)),
        prediction_sd=float(np.std(prediction)),
        bias=float(np.mean(error)),
        rmse=float(np.sqrt(np.mean(error**2))),
        explained_variance=None if variance < 1e-20 else float(1 - np.var(error) / variance),
        mse_over_constant=None if variance < 1e-20 else float(np.mean(error**2) / variance),
    )


def audit(directory, number, device):
    import json

    before_path = directory / f"update-{number - 1}.pt"
    after_path = directory / f"update-{number}.pt"
    saved = torch.load(before_path, map_location=device, weights_only=False)
    after = torch.load(after_path, map_location=device, weights_only=False)
    if (
        saved["fingerprint"] != after["fingerprint"]
        or saved["completed"] != number - 1
        or after["completed"] != number
    ):
        raise ValueError("checkpoint identity differs")
    spec = saved["identity"]["spec"]
    rows = json.loads((directory / f"trajectory-{number}.json").read_text())["decisions"]
    samples = [r for r in rows if r["actor_sample"]]
    model = ActorCritic(len(samples[0]["obs"]), spec["hidden"], actions=spec.get("actions")).to(
        device
    )
    model.load_state_dict(saved["model"])
    optimizer = make_optimizer(model, spec)
    optimizer.load_state_dict(deepcopy(saved["optimizer"]))
    obs = torch.tensor([r["obs"] for r in samples], dtype=torch.float32, device=device)
    rewards = np.array(
        [r["training_reward" if "reward_scale" in spec else "reward"] for r in samples]
    )
    old_values = np.array([r["value"] for r in samples])
    target = old_values + advantages(
        rewards, old_values, gae_lambda=spec["gae_lambda"], terminated=True
    )
    mc = rewards[::-1].cumsum()[::-1].copy()
    with torch.no_grad():
        old_dist = model.distribution(obs)
        pre = model.critic(obs).squeeze(-1).cpu().numpy()
    if not np.allclose(pre, old_values, atol=2e-6, rtol=2e-5):
        raise ValueError("archived critic prediction mismatch")
    gradients = []
    update(model, optimizer, SimpleNamespace(samples=samples), spec, diagnostics=gradients)
    parameter_error = max(
        float((p.detach() - after["model"][k]).abs().max()) for k, p in model.state_dict().items()
    )
    if parameter_error > 2e-6:
        raise ValueError(f"archived update failed to reproduce: {parameter_error}")
    with torch.no_grad():
        new_dist = model.distribution(obs)
        post = model.critic(obs).squeeze(-1).cpu().numpy()
        kl = torch.distributions.kl_divergence(old_dist, new_dist)
        old_p, new_p = old_dist.probs, new_dist.probs
        tv = 0.5 * (old_p - new_p).abs().sum(-1)
        switches = (old_p.argmax(-1) != new_p.argmax(-1)).sum().item()
    # A fixed training-only counterfactual: remove bootstrapping from the value target.
    model.load_state_dict(saved["model"])
    optimizer = make_optimizer(model, spec)
    optimizer.load_state_dict(deepcopy(saved["optimizer"]))
    update(model, optimizer, SimpleNamespace(samples=samples), {**spec, "gae_lambda": 1.0})
    with torch.no_grad():
        mc_fit = model.critic(obs).squeeze(-1).cpu().numpy()
        mc_kl = torch.distributions.kl_divergence(old_dist, model.distribution(obs)).mean()
    alternative = dict(monte_carlo_after=value_metrics(mc_fit, mc), exact_kl_mean=float(mc_kl))
    for g in gradients:
        total = (g["actor_gradient_norm"] ** 2 + g["critic_gradient_norm"] ** 2) ** 0.5
        g["global_clip_multiplier"] = min(1.0, spec["max_grad_norm"] / (total + 1e-6))
    return dict(
        lambda_one_training_probe=alternative,
        fold=spec["fold"],
        seed=spec["seed"],
        update=number,
        samples=len(samples),
        checkpoint_sha256=sha(after_path),
        parameter_error=parameter_error,
        gae_before=value_metrics(pre, target),
        gae_after=value_metrics(post, target),
        monte_carlo_before=value_metrics(pre, mc),
        monte_carlo_after=value_metrics(post, mc),
        exact_kl_mean=float(kl.mean()),
        exact_kl_max=float(kl.max()),
        tv_mean=float(tv.mean()),
        argmax_switches=int(switches),
        gradients=gradients,
        reward_scale=spec.get("reward_scale", 1.0),
        unique_market_dates=len({r["date"] for r in samples}),
    )


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--device", default="cpu")
    args = parser.parse_args()
    torch.set_num_threads(1)
    results = []
    for folder in sorted(args.root.glob("fold-*/seed-*/training")):
        for number in (2, 4, 8, 12, 16):
            results.append(audit(folder, number, args.device))
    if len(results) != 30:
        raise ValueError("expected six paired runs and five updates")
    dump(
        args.output,
        dict(
            scope="Training rollout diagnostics; MC targets are realized on-policy returns.",
            results=results,
        ),
    )
