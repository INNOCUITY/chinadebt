"""Carry + roll-down 基线策略（Phase 2 第 4 步）。

对 ``cfg.portfolio.universe`` 中每个可交易期限桶计算
:func:`ficc.pricing.carry.carry_roll_down` 的年化 ``total_annual_pct``：
carry（票息−融资） + roll-down（沿曲线下滑的资本利得代理）。
按排名做相对基准 tilt：最优桶 ``+α``、最差桶 ``−α``，其余不动。

融资利率：``MarketContext`` 不含实时回购数据，按任务约定用配置常量
``cfg.costs.repo_rate_fallback_const × 100`` 代理，并在 ``notes`` 注明。

**不依赖历史窗口**（信号只来自当前曲线），因此对"未来数据"天然免疫；
无未来信息的验收仍由 :class:`MarketContext` 的截断契约保证。
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import TYPE_CHECKING

import numpy as np

from ficc.pricing.carry import carry_roll_down, par_modified_duration
from ficc.strategy.base import BaseStrategy

if TYPE_CHECKING:
    from ficc.portfolio.types import MarketContext, TargetKRD

__all__ = ["CarryRollStrategy"]


class CarryRollStrategy(BaseStrategy):
    """按 carry+roll 年化排名做多最优桶、低配最差桶。"""

    name = "carry_roll"

    def __init__(
        self,
        *,
        alpha_fraction: float = 0.05,
        horizon_days: int = 1,
        funding_rate_pct: float | None = None,
    ) -> None:
        if not np.isfinite(alpha_fraction) or alpha_fraction <= 0:
            raise ValueError(f"alpha_fraction 必须为正数，实际 {alpha_fraction!r}")
        if int(horizon_days) <= 0:
            raise ValueError(f"horizon_days 必须为正整数，实际 {horizon_days!r}")
        self.alpha_fraction = float(alpha_fraction)
        self.horizon_days = int(horizon_days)
        self.funding_rate_pct = funding_rate_pct

    def _target_krd(self, ctx: MarketContext) -> TargetKRD:
        cfg = ctx.cfg
        universe = [float(tenor) for tenor in cfg.portfolio.universe]
        buckets = [tenor for tenor in universe if float(tenor) in ctx.benchmark.krd]
        if len(buckets) < 2:
            return self.baseline(ctx, notes="carry_roll: 可交易桶不足 2 个，保持基准")

        funding = (
            float(self.funding_rate_pct)
            if self.funding_rate_pct is not None
            else float(cfg.costs.repo_rate_fallback_const) * 100.0
        )
        metrics: dict[float, float] = {}
        for tenor in buckets:
            try:
                result = carry_roll_down(
                    tenor,
                    ctx.curve,
                    horizon_days=self.horizon_days,
                    funding_rate_pct=funding,
                    cfg=cfg,
                )
            except (ValueError, RuntimeError):
                # 2Y 的 roll 目标 1Y 是利随本清/贴现券，carry_roll_down 默认拒绝；
                # 用该期限本身的平价债修正久期作代理（只影响 roll 的久期乘数）。
                try:
                    proxy_duration = par_modified_duration(ctx.curve, tenor, cfg=cfg)
                    result = carry_roll_down(
                        tenor,
                        ctx.curve,
                        horizon_days=self.horizon_days,
                        funding_rate_pct=funding,
                        mod_duration=proxy_duration,
                        cfg=cfg,
                    )
                except (ValueError, RuntimeError):
                    continue
            value = float(result["total_annual_pct"])
            if np.isfinite(value):
                metrics[tenor] = value
        if len(metrics) < 2:
            return self.baseline(ctx, notes="carry_roll: 有效桶不足 2 个，保持基准")

        ranked = sorted(metrics.items(), key=lambda item: (-item[1], item[0]))
        best_tenor = ranked[0][0]
        worst_tenor = ranked[-1][0]
        if best_tenor == worst_tenor:  # pragma: no cover - 防御式
            return self.baseline(ctx, notes="carry_roll: 排名退化，保持基准")
        alpha = self.alpha_fraction * float(ctx.benchmark.total_dv01)
        deltas: Mapping[float, float] = {best_tenor: +alpha, worst_tenor: -alpha}
        distribution = ", ".join(f"{tenor:g}Y={value:+.4f}%" for tenor, value in ranked)
        notes = (
            f"carry_roll（融资用配置常量代理 {funding:.3f}%，h={self.horizon_days}d）："
            f"{distribution} → 多 {best_tenor:g}Y +{alpha:.0f}，低配 {worst_tenor:g}Y "
            f"−{alpha:.0f} 元/bp"
        )
        return self.tilt(ctx, deltas, notes=notes)
