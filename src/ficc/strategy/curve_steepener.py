"""2s10s 期限利差基线策略（Phase 2 第 4 步）。

信号：``slope = 10Y − 2Y`` 的 z-score（只用 ``ctx.history`` 截至 as_of 的
``cfg.strategy.steepener.zscore_window`` 个交易日，绝不使用未来数据）。

状态机（带 hysteresis）::

    z > entry_z           → 预期平坦化：10Y +α、2Y −α
    z < −entry_z          → 预期陡峭化：10Y −α、2Y +α
    |z| <= exit_z         → 回基准（零 tilt）
    exit_z < |z| <= entry_z → 维持上一次方向（避免来回抖动）

``α = cfg.strategy.steepener.target_dv01``（元/bp，默认 10 万）。输出经
:func:`ficc.strategy.base.relative_tilt` 截断负桶并保持总 DV01 中性。
"""

from __future__ import annotations

import numpy as np

from ficc.strategy.base import BaseStrategy

__all__ = ["SteepenerStrategy"]


class SteepenerStrategy(BaseStrategy):
    """2s10s 利差 z-score 的平坦化/陡峭化策略。"""

    name = "2s10s"

    def __init__(self) -> None:
        self._direction: int = 0  # +1=平坦化（多10Y/空2Y），-1=陡峭化

    def reset(self) -> None:
        """清空持仓状态（回测换段/单测用）。"""
        self._direction = 0

    def _target_krd(self, ctx):
        cfg = ctx.cfg
        scfg = cfg.strategy.steepener
        short = float(scfg.short_tenor)
        long = float(scfg.long_tenor)
        if short not in ctx.benchmark.krd or long not in ctx.benchmark.krd:
            return self.baseline(
                ctx,
                notes=f"2s10s: 基准缺少 {short:g}Y/{long:g}Y KRD 桶，保持基准",
            )
        history = self.history_until(ctx)
        window = int(scfg.zscore_window)
        if window < 2 or len(history) < window:
            return self.baseline(
                ctx,
                notes=f"2s10s: 历史 {len(history)} < 窗口 {window}，保持基准",
            )
        short_series = self.tenor_column(history, short)
        long_series = self.tenor_column(history, long)
        if short_series is None or long_series is None:
            return self.baseline(ctx, notes="2s10s: 历史缺少 2Y/10Y 列，保持基准")
        slope = (long_series - short_series).astype(float)
        sample = slope.iloc[-window:].dropna()
        if len(sample) < window:
            return self.baseline(
                ctx, notes=f"2s10s: 有效历史 {len(sample)} < 窗口 {window}，保持基准"
            )
        std = float(sample.std(ddof=1))
        if not np.isfinite(std) or std <= 0:
            return self.baseline(ctx, notes="2s10s: 利差方差为 0，保持基准")
        current = float(sample.iloc[-1])
        z = (current - float(sample.mean())) / std

        entry = float(scfg.entry_z)
        exit_z = float(scfg.exit_z)
        previous = self._direction
        if z > entry:
            direction = 1
        elif z < -entry:
            direction = -1
        elif abs(z) <= exit_z:
            direction = 0
        else:
            direction = previous
        self._direction = direction
        if direction == 0:
            return self.baseline(
                ctx,
                notes=(f"2s10s z={z:+.2f}（slope={current:.4f}%）在 ±{entry:g} 内，回基准零 tilt"),
            )

        alpha = float(scfg.target_dv01)
        if not np.isfinite(alpha) or alpha <= 0:
            alpha = 0.1 * float(ctx.benchmark.total_dv01)
        # 风险预算：单次 tilt 不超过基准总 DV01 的 10%（否则久期带必然越界）
        alpha = min(alpha, 0.1 * float(ctx.benchmark.total_dv01))
        if direction > 0:
            deltas = {long: +alpha, short: -alpha}
            action = f"平坦化：{long:g}Y +{alpha:.0f}、{short:g}Y −{alpha:.0f} 元/bp"
        else:
            deltas = {long: -alpha, short: +alpha}
            action = f"陡峭化：{long:g}Y −{alpha:.0f}、{short:g}Y +{alpha:.0f} 元/bp"
        notes = f"2s10s z={z:+.2f}（slope={current:.4f}%，窗口{window}d）→ {action}"
        return self.tilt(ctx, deltas, notes=notes)
