"""Compare a seed-matched, untrained categorical policy with two fixed slow-actor checkpoints."""

import argparse
from datetime import date
from pathlib import Path

import numpy as np
import torch
from evaluate_rl import Market, inference_actor, load_policy, replay, score, sha
from run_critic_budget import read
from train_rl import ACTOR_STEP_SPEC, EVAL_SPEC, GRID_SPEC, dump, training_spec

from ficc.rl.ppo import ActorCritic


def recover_initial(spec, decisions):
    samples = [r for r in decisions if r["actor_sample"]]
    obs = torch.tensor([r["obs"] for r in samples], dtype=torch.float32)
    with torch.random.fork_rng(devices=[]):
        torch.random.default_generator.manual_seed(spec["seed"])
        model = ActorCritic(obs.shape[1], spec["hidden"], actions=spec["actions"]).eval()
    with torch.no_grad():
        indices = torch.tensor([int(r["latent"]) for r in samples])
        logs = model.distribution(obs).log_prob(indices)
        error = (logs - torch.tensor([r["log_prob"] for r in samples])).abs().max().item()
    if not error <= 2e-6 or any(
        spec["actions"][i] != r["z"] for i, r in zip(indices, samples, strict=True)
    ):
        raise ValueError("reconstructed initial policy differs from first on-policy trajectory")
    return model, error


def audit(root, fold, seed):
    spec = training_spec(
        fold,
        seed,
        action_grid=True,
        grid_active=True,
        zero_critic=True,
        critic_budget=True,
        invested_start=True,
        actor_step=True,
    )
    start, end = map(date.fromisoformat, spec["validation"])
    anchor = date.fromisoformat(read(EVAL_SPEC)["anchor"])
    market = Market(end)
    folder = root / f"fold-{fold}/seed-{seed}"
    ck = folder / "training"
    reference = read(folder / "validation.json")
    if (reference["fold"], reference["seed"], reference["actor_step_sha256"]) != (
        fold,
        seed,
        sha(ACTOR_STEP_SPEC),
    ):
        raise ValueError("validation reference mismatch")
    models = {}
    encoder = None
    for step in (2, 16):
        models[step], current = load_policy(
            ck / f"update-{step}.pt", spec, step, market, market.cfg
        )
        if encoder is not None and not (
            np.array_equal(encoder.mean, current.mean)
            and np.array_equal(encoder.scale, current.scale)
        ):
            raise ValueError("checkpoint normalization differs across updates")
        encoder = current
    models[0], error = recover_initial(spec, read(ck / "trajectory-1.json")["decisions"])
    rows = []
    for band in read(GRID_SPEC)["bands"]:
        fixed = []

        def neutral(ctx, prev, fixed=fixed):
            if ctx.as_of >= start:
                fixed.append(encoder.encode(ctx, prev))
            return 0.0

        zero, _ = replay(market, anchor, end, band, neutral)
        obs = torch.tensor(fixed, dtype=torch.float32)
        with torch.inference_mode():
            probs = {k: m.distribution(obs).probs for k, m in models.items()}
        for step in (0, 2, 16):
            actor = inference_actor(models[step], encoder)
            book, residual = replay(
                market,
                anchor,
                end,
                band,
                lambda ctx, prev, act=actor: act(ctx, prev) if ctx.as_of >= start else 0.0,
            )
            metrics = score(
                book, zero, start, end, market.calendar, market.cfg.backtest.annual_days
            )
            if step == 16:
                (prior,) = [r for r in reference["rows"] if r["band"] == band and r["update"] == 16]
                if prior["checkpoint_sha256"] != sha(ck / "update-16.pt") or any(
                    abs(metrics[k] - prior[k]) > 1e-5 for k in metrics
                ):
                    raise ValueError("update 16 failed archived ledger replication")
            picks = probs[step].argmax(1)
            rows.append(
                dict(
                    band=band,
                    update=step,
                    **metrics,
                    ledger_error_yuan=residual,
                    common_observations=len(obs),
                    changed_argmax_vs_initial=int((picks != probs[0].argmax(1)).sum()),
                    mean_tv_vs_initial=float((probs[step] - probs[0]).abs().sum(1).mean() / 2),
                    fixed_action_counts=torch.bincount(
                        picks, minlength=len(spec["actions"])
                    ).tolist(),
                )
            )
    return dict(fold=fold, seed=seed, initial_log_prob_error=error, rows=rows)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--fold", type=int, required=True)
    parser.add_argument("--seed", type=int, required=True)
    args = parser.parse_args()
    torch.set_num_threads(1)
    dump(args.output, audit(args.root, args.fold, args.seed))
