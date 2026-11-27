"""单窗口 PPO-Clip。概率始终按原始策略采样计算。"""

from __future__ import annotations

import math

import numpy as np
import torch
from torch import nn
from torch.distributions import Categorical, Normal
from torch.nn import functional as F

from ficc.rl.collector import Draw, Episode


class ActorCritic(nn.Module):
    def __init__(self, width: int, hidden: int, *, actions=None):
        super().__init__()

        def network(outputs):
            return nn.Sequential(
                nn.Linear(width, hidden),
                nn.Tanh(),
                nn.Linear(hidden, hidden),
                nn.Tanh(),
                nn.Linear(hidden, outputs),
            )

        self.actions = actions
        self.actor = network(len(actions) if actions else 1)
        self.critic = network(1)
        if not actions:
            self.log_std = nn.Parameter(torch.tensor(-0.5))

    def distribution(self, obs):
        if self.actions:
            return Categorical(logits=self.actor(obs))
        return Normal(self.actor(obs).squeeze(-1), self.log_std.exp())

    @staticmethod
    def log_prob(distribution, latent):
        if isinstance(distribution, Categorical):
            return distribution.log_prob(latent)
        # log(1-tanh(u)^2) 的稳定形式，在饱和区仍使用原始 u。
        return distribution.log_prob(latent) - 2 * (math.log(2) - latent - F.softplus(-2 * latent))

    def deterministic(self, obs):
        scores = self.actor(obs)
        if not torch.isfinite(scores).all():
            raise ValueError("non-finite policy output")
        return self.actions[scores.argmax().item()] if self.actions else scores.tanh().item()

    @torch.no_grad()
    def draw(self, obs, *, fixed_latent=None):
        x = torch.tensor(obs, dtype=torch.float32, device=next(self.parameters()).device)
        dist = self.distribution(x)
        u = dist.sample() if fixed_latent is None else x.new_tensor(fixed_latent)
        if self.actions:
            i, prob, value = (
                torch.stack((u, dist.log_prob(u), self.critic(x).squeeze(-1))).cpu().tolist()
            )
            return Draw(int(i), self.actions[int(i)], prob, value)
        values = torch.stack((u, u.tanh(), self.log_prob(dist, u), self.critic(x).squeeze(-1)))
        return Draw(*values.cpu().tolist())


def advantages(rewards, values, *, gae_lambda, terminated, next_value=None):
    """未折现 GAE；计算截断点必须提供同一账本的后续价值。"""
    if terminated == (next_value is not None):
        raise ValueError("终止使用零尾值，截断必须显式传入 next_value")
    tail = 0.0 if terminated else float(next_value)
    adv = np.empty(len(rewards), dtype=np.float64)
    carry = 0.0
    for i in reversed(range(len(rewards))):
        carry = rewards[i] + tail - values[i] + gae_lambda * carry
        adv[i] = carry
        tail = values[i]
    return adv


def update(
    model: ActorCritic, optimizer, episode: Episode, spec: dict, *, diagnostics=None
) -> dict:
    samples = episode.samples
    device = next(model.parameters()).device

    def tensor(field):
        return torch.tensor([row[field] for row in samples], device=device, dtype=torch.float32)

    obs, latent, old_prob = tensor("obs"), tensor("latent"), tensor("log_prob")
    values = np.array([row["value"] for row in samples])
    rewards = [row["training_reward" if "reward_scale" in spec else "reward"] for row in samples]
    adv = advantages(rewards, values, gae_lambda=spec["gae_lambda"], terminated=True)
    if len(adv) < 2:
        raise ValueError("PPO 至少需要两个决策")
    targets = torch.tensor(values + adv, dtype=torch.float32, device=device)
    weights = torch.tensor(
        (adv - adv.mean()) / max(adv.std(), 1e-8), dtype=torch.float32, device=device
    )
    before = [p.detach().clone() for p in model.parameters()]
    for epoch in range(spec["epochs"]):
        logp = model.log_prob(model.distribution(obs), latent)
        if epoch == 0 and not torch.allclose(logp, old_prob, atol=2e-5, rtol=2e-5):
            raise ValueError("行为策略与更新前原始动作概率不一致")
        ratio = (logp - old_prob).exp()
        clipped = ratio.clamp(1 - spec["clip_ratio"], 1 + spec["clip_ratio"])
        policy_loss = -torch.minimum(weights * ratio, weights * clipped).mean()
        value_loss = F.mse_loss(model.critic(obs).squeeze(-1), targets)
        loss = policy_loss + spec["value_coef"] * value_loss
        if not loss.isfinite():
            raise FloatingPointError("PPO loss 非有限")
        optimizer.zero_grad(set_to_none=True)
        loss.backward()
        if diagnostics is not None:

            def gradient_norm(parameters):
                return float(
                    torch.stack(
                        [p.grad.detach().norm().square() for p in parameters if p.grad is not None]
                    )
                    .sum()
                    .sqrt()
                )

            diagnostics.append(
                dict(
                    epoch=epoch,
                    actor_gradient_norm=gradient_norm(model.actor.parameters()),
                    critic_gradient_norm=gradient_norm(model.critic.parameters()),
                    sampled_approx_kl=float(((ratio - 1) - (logp - old_prob)).mean().detach()),
                    clip_fraction=float(((ratio - 1).abs() > spec["clip_ratio"]).float().mean()),
                )
            )
        norm = nn.utils.clip_grad_norm_(
            model.parameters(), spec["max_grad_norm"], error_if_nonfinite=True
        )
        optimizer.step()
    delta = max(
        (p.detach() - old).abs().max().item()
        for p, old in zip(model.parameters(), before, strict=True)
    )
    if not all(p.isfinite().all() for p in model.parameters()) or delta <= 0:
        raise FloatingPointError("参数非有限或没有更新")
    return dict(
        loss=loss.item(),
        policy_loss=policy_loss.item(),
        value_loss=value_loss.item(),
        gradient_norm=norm.item(),
        reward_sd=float(np.std(rewards)),
        value_sd=float(values.std()),
        advantage_sd=float(adv.std()),
        max_parameter_change=delta,
    )
