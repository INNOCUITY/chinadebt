"""NSS 残差相对价值（RV）基线策略（Phase 2 第 4 步）。

信号：对 ``ctx.history`` 中截至 as_of 的每个交易日，用
:func:`ficc.curve.fitting.fit_curve`（method=``cfg.curve.fit_method``）拟合当日
曲线，残差 ``residual = 观测 − 拟合``（bp）。对每个可交易期限桶，用过去
``cfg.strategy.relative_value.zscore_window`` 天的残差序列算 z-score：

- ``z > entry_z``（便宜）→ 该桶 ``+α``；
- ``z < −entry_z``（贵）→ 该桶 ``−α``；
- 多桶同时触发时按 ``|z|`` 加权、总 tilt 归一化。

**无未来信息**：只读 ``ctx.history`` 且再做 ``index <= as_of`` 截断；窗口不足
（或有效残差样本不足）时返回基准零 tilt，不偷偷改用全样本。
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import numpy as np
import pandas as pd

from ficc.curve.fitting import fit_curve
from ficc.strategy.base import BaseStrategy

if TYPE_CHECKING:
    from ficc.portfolio.types import MarketContext, TargetKRD

__all__ = ["RelativeValueStrategy"]


class RelativeValueStrategy(BaseStrategy):
    """NSS 残差 z-score 的多桶相对价值策略。"""

    name = "rv"

    def __init__(
        self,
        *,
        window: int | None = None,
        entry_z: float | None = None,
        alpha_fraction: float = 0.05,
        residuals: pd.DataFrame | None = None,
    ) -> None:
        if window is not None and int(window) < 5:
            raise ValueError(f"window 必须 ≥5，实际 {window!r}")
        if entry_z is not None and (not np.isfinite(entry_z) or entry_z <= 0):
            raise ValueError(f"entry_z 必须为正数，实际 {entry_z!r}")
        if not np.isfinite(alpha_fraction) or alpha_fraction <= 0:
            raise ValueError(f"alpha_fraction 必须为正数，实际 {alpha_fraction!r}")
        self.window = None if window is None else int(window)
        self.entry_z = None if entry_z is None else float(entry_z)
        self.alpha_fraction = float(alpha_fraction)
        # 预计算的残差面板（date × tenor，单位 bp，观测−拟合，正值=便宜）。
        # 强烈建议传入：逐日回测若每天现场拟合 window 天曲线会慢 2–3 个数量级。
        self.residuals = residuals

    def _residual_series(self, ctx, buckets: list[float], window: int):
        """从预计算残差面板取截至 as_of 的窗口数据（无未来信息）。"""
        panel = self.residuals
        usable = [t for t in buckets if t in panel.columns]
        if not usable:
            return {}
        end = panel.index.searchsorted(pd.Timestamp(ctx.as_of), side="right")
        if end <= 0:
            return {}
        start = max(0, end - window)
        sample = panel.iloc[start:end][usable]
        return {t: sample[t].dropna().to_numpy(dtype=float) for t in usable}

    def _fit_residuals(
        self, history, *, buckets: list[float], method: str, weights, min_tenors: int
    ) -> dict[float, list[float]]:
        """对历史逐日拟合 NSS，返回 ``bucket → 残差序列(bp)``。"""
        residuals: dict[float, list[float]] = {tenor: [] for tenor in buckets}
        for timestamp, row in history.iterrows():
            values = row.astype(float)
            valid = values[np.isfinite(values.to_numpy())]
            if len(valid) < min_tenors:
                continue
            tenors = [float(column) for column in valid.index]
            ytm = valid.to_numpy(dtype=float)
            try:
                fitted = fit_curve(
                    tenors,
                    ytm,
                    method=method,
                    as_of=timestamp.date(),
                    weights=weights,
                )
            except (ValueError, TypeError, RuntimeError):
                continue
            for tenor in buckets:
                if tenor not in row.index:
                    continue
                observed = float(row[tenor])
                if not np.isfinite(observed):
                    continue
                residual = (observed - float(fitted.rate(tenor))) * 100.0
                if np.isfinite(residual):
                    residuals[tenor].append(residual)
        return residuals

    def _target_krd(self, ctx: MarketContext) -> TargetKRD:
        cfg = ctx.cfg
        params = cfg.strategy.relative_value
        window = int(self.window if self.window is not None else params.zscore_window)
        entry = float(self.entry_z if self.entry_z is not None else params.entry_z)
        buckets = [
            float(tenor) for tenor in cfg.portfolio.universe if float(tenor) in ctx.benchmark.krd
        ]
        if not buckets:
            return self.baseline(ctx, notes="rv: 基准缺少可交易桶，保持基准")
        history = self.history_until(ctx)
        if len(history) < window:
            return self.baseline(ctx, notes=f"rv: 历史 {len(history)} < 窗口 {window}，保持基准")
        if self.residuals is not None:
            residuals = self._residual_series(ctx, buckets, window)
        else:  # fallback：现场逐日拟合（慢，仅用于单元测试/小样本）
            sample = history.iloc[-window:]
            min_tenors = int(getattr(cfg.curve, "min_valid_tenors", 4))
            residuals = self._fit_residuals(
                sample,
                buckets=buckets,
                method=str(cfg.curve.fit_method),
                weights=cfg.curve.weights,
                min_tenors=min_tenors,
            )
        min_obs = max(30, window // 2)
        zscores: dict[float, float] = {}
        for tenor, series in residuals.items():
            values = np.asarray(series, dtype=float)
            values = values[np.isfinite(values)]
            if values.size < min_obs:
                continue
            std = float(values.std(ddof=1))
            if not np.isfinite(std) or std <= 0:
                continue
            z = (float(values[-1]) - float(values.mean())) / std
            if np.isfinite(z):
                zscores[tenor] = z
        triggered = {tenor: z for tenor, z in zscores.items() if abs(z) > entry}
        if not triggered:
            detail = ", ".join(f"{tenor:g}Y={z:+.2f}" for tenor, z in sorted(zscores.items()))
            return self.baseline(
                ctx,
                notes=f"rv: 无 |z|>{entry:g}（{detail or '无有效残差'}），保持基准",
            )
        score_sum = float(sum(abs(z) for z in triggered.values()))
        budget = self.alpha_fraction * float(ctx.benchmark.total_dv01)
        cap = float(getattr(params, "max_position_per_tenor", 1.0))
        raw_shares = {tenor: abs(z) / score_sum for tenor, z in triggered.items()}
        if cap > 0:
            capped = {tenor: min(share, cap) for tenor, share in raw_shares.items()}
            capped_sum = float(sum(capped.values()))
            shares = {tenor: share / capped_sum for tenor, share in capped.items()}
        else:  # pragma: no cover - 防御式
            shares = raw_shares
        deltas = {tenor: np.sign(triggered[tenor]) * budget * shares[tenor] for tenor in triggered}
        detail = ", ".join(
            f"{tenor:g}Y z={triggered[tenor]:+.2f}→{'+' if triggered[tenor] > 0 else '−'}"
            f"{budget * shares[tenor]:.0f}"
            for tenor in sorted(triggered)
        )
        notes = f"rv: NSS残差 z 触发（窗口{window}d，entry={entry:g}）：{detail} 元/bp"
        return self.tilt(ctx, deltas, notes=notes)
