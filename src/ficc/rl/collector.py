"""从一次真实账本回放采集有执行机会的决策，并逐日对齐净收益。"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import asdict, dataclass
from datetime import date
from time import perf_counter

import numpy as np
import pandas as pd

from ficc.ml.dataset import attach_funding_column
from ficc.ml.market_features import feature_specs
from ficc.portfolio.rolling import _tenor_of
from ficc.portfolio.types import MarketContext
from ficc.research.protocol import prediction_to_target
from ficc.rl.env import RebalanceEnv


@dataclass
class Encoder:
    market: pd.DataFrame
    mean: np.ndarray
    scale: np.ndarray
    tenors: tuple[float, ...]

    @staticmethod
    def market_features(panel, funding):
        frame = attach_funding_column(panel, funding).rename(columns=str)
        return pd.DataFrame(
            {s.name: s.transform(frame.loc[:, list(s.inputs)]) for s in feature_specs()}
        )

    @classmethod
    def fit(cls, panel, funding, calibration_start, calibration_end, tenors):
        """九个向后看的特征。只用首段训练日期拟合，后续交易使用固定尺度。"""
        market = cls.market_features(panel, funding)
        calibration = market.loc[str(calibration_start) : str(calibration_end)]
        if calibration.count().min() < 2:
            raise ValueError("训练校准期的有效观测不足")
        scale = calibration.std(ddof=0).replace(0, 1)
        return cls(market, calibration.mean().to_numpy(), scale.to_numpy(), tuple(tenors))

    @property
    def names(self):
        names = list(self.market.columns)
        return (
            names
            + [f"{n}_missing" for n in names]
            + [f"face_{t:g}y" for t in self.tenors]
            + ["lagged_cash", "lagged_nav", "lagged_benchmark_duration", "previous_proposal"]
        )

    def encode(self, ctx: MarketContext, previous: float) -> list[float]:
        cutoff = ctx.curve.as_of
        if not cutoff == ctx.benchmark.as_of < ctx.as_of:
            raise ValueError("曲线和基准超过可用时间或时点不一致")
        rates = self.market.loc[pd.Timestamp(cutoff)].to_numpy(dtype=float)
        missing = np.isnan(rates)
        values = np.where(missing, 0, (rates - self.mean) / self.scale)
        notional = ctx.cfg.benchmark.notional
        faces = dict.fromkeys(self.tenors, 0.0)
        for position in ctx.positions:
            faces[_tenor_of(position.bond)] += position.face_value / notional
        obs = [
            *values,
            *missing.astype(float),
            *faces.values(),
            ctx.cash / notional,
            ctx.nav / notional,
            ctx.benchmark.duration,
            previous,
        ]
        if not np.isfinite(obs).all():
            raise ValueError("非有限策略观测")
        return list(map(float, obs))


@dataclass(frozen=True)
class Draw:
    latent: float
    z: float
    log_prob: float
    value: float


@dataclass
class Episode:
    decisions: list[dict]
    ledger: list[dict]
    initial_nav: float
    net_terminal: float
    terminal_fees: float
    prefix_reward: float
    closure_error_yuan: float
    inference_seconds: float
    environment_seconds: float

    @property
    def samples(self):
        return [row for row in self.decisions if row["actor_sample"]]


def collect(env: RebalanceEnv, encoder: Encoder, draw: Callable, start: date, end: date) -> Episode:
    """采样时保持模型冻结。最后估值日不给出无法执行的动作。"""
    decisions = {}
    inference = 0.0
    previous = 0.0
    started = perf_counter()

    def policy(ctx):
        nonlocal previous, inference
        if ctx.as_of == end:
            return None
        obs = encoder.encode(ctx, previous)
        tick = perf_counter()
        action = draw(obs)
        inference += perf_counter() - tick
        target = prediction_to_target(action.z, ctx.benchmark, as_of=ctx.as_of)
        previous = action.z
        decisions[ctx.as_of] = dict(
            date=ctx.as_of,
            available_through=ctx.curve.as_of,
            obs=obs,
            **asdict(action),
            target_krd=dict(target.krd),
        )
        return target

    rollout = env.rollout(policy, start, end, name="ppo")
    signals = rollout.result.signals.set_index("date")
    initial = float(rollout.result.nav.iloc[0])
    terminal = rollout.terminal
    ledger = []
    prior = start
    active = None
    prefix_reward = 0.0
    for rec in rollout.records:
        if prior in decisions:
            row = decisions[prior]
            # 当日风控可能替代已提出的动作。保留提议审计，但不训练该动作。
            row["actor_sample"] = not bool(signals.loc[prior, "risk_off"])
            row["outcome"] = rec.action_outcome
            if row["actor_sample"]:
                active = row
                active["reward"] = 0.0
        fee = terminal.accrued_fees if rec.date == end else 0.0
        net = (rec.reward - fee) / initial
        if active is None:
            prefix_reward += net
        else:
            active["reward"] += net
        ledger.append(
            dict(
                date=rec.date,
                decision_day=prior,
                reward_owner=active["date"] if active else None,
                nav_before=rec.nav_before,
                nav_after=rec.nav_after,
                reward_yuan=rec.reward,
                terminal_fee_yuan=fee,
                outcome=rec.action_outcome,
                projected_faces=rec.projected_faces,
                projected_krd=dict(rec.projected_target.krd) if rec.projected_target else None,
                policy_source=rec.policy_source,
                policy_actions=[asdict(x) for x in rec.policy_actions],
                executed_trades=rec.executed_trades,
                reject_reason=rec.reject_reason,
            )
        )
        prior = rec.date
    if prior != end or not decisions or active is None:
        raise ValueError("完整训练 episode 缺少终值或可训练动作")
    total = prefix_reward + sum(row.get("reward", 0) for row in decisions.values())
    error = total * initial - (terminal.net_terminal_value - initial)
    if not np.isfinite(error) or abs(error) > 1e-5:
        raise ValueError(f"账本奖励对账失败 {error} 元")
    return Episode(
        list(decisions.values()),
        ledger,
        initial,
        terminal.net_terminal_value,
        terminal.accrued_fees,
        prefix_reward,
        error,
        inference,
        perf_counter() - started - inference,
    )
