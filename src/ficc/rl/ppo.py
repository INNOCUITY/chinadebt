"""单窗口 PPO-Clip。概率始终按原始策略采样计算。"""

from __future__ import annotations

import math

import numpy as np
import torch
from torch import nn
from torch.distributions import Normal
from torch.nn import functional as F

from ficc.rl.collector import Draw, Episode


class ActorCritic(nn.Module):
    def __init__(self, width: int, hidden: int):
        super().__init__()

        def network():
            return nn.Sequential(
                nn.Linear(width, hidden),
                nn.Tanh(),
                nn.Linear(hidden, hidden),
                nn.Tanh(),
                nn.Linear(hidden, 1),
            )

        self.actor = network()
        self.critic = network()
        self.log_std = nn.Parameter(torch.tensor(-0.5))

    def distribution(self, obs):
        return Normal(self.actor(obs).squeeze(-1), self.log_std.exp())

    @staticmethod
    def log_prob(distribution, latent):
        # log(1-tanh(u)^2) 的稳定形式，在饱和区仍使用原始 u。
        return distribution.log_prob(latent) - 2 * (math.log(2) - latent - F.softplus(-2 * latent))

    @torch.no_grad()
    def draw(self, obs, *, fixed_latent=None):
        x = torch.tensor(obs, dtype=torch.float32, device=self.log_std.device)
        dist = self.distribution(x)
        u = dist.sample() if fixed_latent is None else x.new_tensor(fixed_latent)
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


def update(model: ActorCritic, optimizer, episode: Episode, spec: dict) -> dict:
    samples = episode.samples
    device = model.log_std.device

    def tensor(field):
        return torch.tensor([row[field] for row in samples], device=device, dtype=torch.float32)

    obs, latent, old_prob = tensor("obs"), tensor("latent"), tensor("log_prob")
    values = np.array([row["value"] for row in samples])
    rewards = [row["training_reward" if "arm" in spec else "reward"] for row in samples]
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
