"""策略层基类与相对基准 KRD 中性工具（Phase 2 第 4 步）。

策略只输出 :class:`TargetKRD`（目标关键期限 DV01 向量），**不下单**；
下单/约束/账本由 construction、constraints 与引擎负责。

核心约定
--------
- 以基准组合的 KRD 为起点（``ctx.benchmark.krd``，元/bp，多头为正）；
- 策略只做**相对基准的桶 tilt**；最终必须满足：
  1. 不得出现负 KRD 桶（现券禁裸空）：负值截断到 0，被截掉的部分按比例
     重新分配到其余正桶；
  2. ``Σ target.krd == ctx.benchmark.total_dv01``（相对基准 DV01 中性）；
  3. ``notes`` 记录调仓理由与任何截断/缩放。
- 所有历史取值必须来自 ``ctx.history``（引擎已截断到 as_of）；本模块再做一层
  ``index <= as_of`` 防御，禁止未来数据。
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import Mapping
from typing import Protocol, runtime_checkable

import numpy as np
import pandas as pd

from ficc.portfolio.types import MarketContext, TargetKRD

__all__ = ["BaseStrategy", "Strategy", "relative_tilt"]


# ---------------------------------------------------------------------------
# 相对基准 KRD tilt
# ---------------------------------------------------------------------------
def relative_tilt(
    ctx: MarketContext,
    bucket_deltas: Mapping[float, float],
    *,
    max_bucket_fraction: float = 0.5,
    notes: str = "",
) -> TargetKRD:
    """在基准 KRD 上加桶 tilt，并保持非负 + 总 DV01 中性。

    Parameters
    ----------
    ctx : MarketContext
        决策日上下文；以 ``ctx.benchmark.krd`` 为起点，目标总 DV01 =
        ``ctx.benchmark.total_dv01``。
    bucket_deltas : Mapping[float, float]
        各关键期限的 tilt（元/bp，正=超配、负=低配）；键应是
        ``cfg.risk.key_tenors_krd`` 中的期限（允许新增桶，会以 0 为基准加入）。
    notes : str
        原始调仓理由；截断/缩放说明会追加在后面。

    Returns
    -------
    TargetKRD
        ``krd`` 全为非负；``Σkrd`` 与基准总 DV01 的误差 < 1e-9 相对量级。

    Notes
    -----
    - **单桶 tilt 上限**：每桶的 delta 先被截断到 ``±max_bucket_fraction × base``
      （默认 50%）。原因：固定金额 tilt（如 10% 总 DV01）在**小桶**上会超过桶本身
      （例如 2Y 桶只占 6%），若按"截掉后回填正桶"处理，会把 tilt 全部堆到长端，
      导致久期漂移（实测组合久期 10.9Y vs 基准 8.2Y）；
    - 负桶截断：截断后仍为负的值置 0，**不再回填**（避免放大其它桶）；
    - 最后按比例缩放，使 ``Σkrd`` 精确等于基准总 DV01（相对基准 DV01 中性）。
    """
    base = {float(tenor): float(value) for tenor, value in ctx.benchmark.krd.items()}
    desired_total = float(ctx.benchmark.total_dv01)
    if not np.isfinite(desired_total) or desired_total <= 0:
        raise ValueError(f"benchmark.total_dv01 必须为正数，实际 {desired_total!r}")
    if not (0.0 < max_bucket_fraction <= 1.0):
        raise ValueError(f"max_bucket_fraction 必须在 (0, 1]，实际 {max_bucket_fraction!r}")
    target = dict(base)
    extra_notes: list[str] = []
    capped_total = 0.0
    for tenor, delta in bucket_deltas.items():
        tenor_f = float(tenor)
        delta_f = float(delta)
        if not np.isfinite(tenor_f) or tenor_f <= 0:
            raise ValueError(f"tilt 的期限必须为正数，实际 {tenor!r}")
        if not np.isfinite(delta_f):
            raise ValueError(f"tilt[{tenor_f}] 不是有限数值: {delta!r}")
        base_value = float(base.get(tenor_f, 0.0))
        cap = max_bucket_fraction * base_value
        delta_eff = float(np.clip(delta_f, -cap, cap))
        capped_total += abs(delta_f - delta_eff)
        target[tenor_f] = target.get(tenor_f, 0.0) + delta_eff
    if capped_total > 0:
        extra_notes.append(
            f"单桶 tilt 超过 {max_bucket_fraction:.0%}×桶值，已裁剪 {capped_total:.2f} 元/bp"
        )

    # 1) 负桶截断（不再回填，避免把 tilt 堆到长端）
    clipped = 0.0
    for tenor in list(target):
        if target[tenor] < 0:
            clipped += -target[tenor]
            target[tenor] = 0.0
    if clipped > 0:
        extra_notes.append(f"负桶截断 {clipped:.2f} 元/bp（不回填）")

    # 2) 总量对齐基准总 DV01（deltas 之和不必然为 0；截断/均摊后可能偏移）
    current_total = float(sum(target.values()))
    if current_total <= 0:  # pragma: no cover - 防御式
        share = desired_total / len(target)
        for tenor in target:
            target[tenor] = share
        extra_notes.append("目标 KRD 合计为 0 → 按桶均摊")
    elif abs(current_total - desired_total) > 1e-9 * max(desired_total, 1.0):
        scale = desired_total / current_total
        for tenor in target:
            target[tenor] *= scale
        extra_notes.append(
            f"总量 {current_total:.2f} → {desired_total:.2f} 元/bp，按比例缩放 {scale:.8f}"
        )
    # 浮点误差清扫：不引入负桶
    target = {tenor: max(float(value), 0.0) for tenor, value in target.items()}
    total_after = float(sum(target.values()))
    if abs(total_after - desired_total) > 1e-6 * max(desired_total, 1.0):  # pragma: no cover
        raise RuntimeError(f"relative_tilt 总量对齐失败: {total_after} vs {desired_total}")
    note_parts = [part for part in (notes.strip(), *extra_notes) if part]
    return TargetKRD(
        as_of=ctx.as_of,
        krd={tenor: target[tenor] for tenor in sorted(target)},
        notes="；".join(note_parts),
    )


# ---------------------------------------------------------------------------
# 策略协议与基类
# ---------------------------------------------------------------------------
@runtime_checkable
class Strategy(Protocol):
    """策略协议：只输出目标 KRD。"""

    name: str

    def target_krd(self, ctx: MarketContext) -> TargetKRD:  # pragma: no cover - 协议声明
        ...


class BaseStrategy(ABC):
    """策略基类：提供历史截断、基准返回与相对 tilt 工具。"""

    name: str = "base"

    def target_krd(self, ctx: MarketContext) -> TargetKRD:
        """计算目标 KRD（具体策略实现 :meth:`_target_krd`）。"""
        return self._target_krd(ctx)

    @abstractmethod
    def _target_krd(self, ctx: MarketContext) -> TargetKRD:
        """具体策略的目标 KRD 实现。"""

    # ---- 工具 ----
    def baseline(self, ctx: MarketContext, *, notes: str = "") -> TargetKRD:
        """返回基准 KRD（零 tilt），用于无信号/窗口不足。"""
        krd = {float(tenor): float(value) for tenor, value in ctx.benchmark.krd.items()}
        total = float(sum(krd.values()))
        desired = float(ctx.benchmark.total_dv01)
        if total > 0 and abs(total - desired) > 1e-9 * max(desired, 1.0):
            scale = desired / total
            krd = {tenor: value * scale for tenor, value in krd.items()}
            notes = "；".join(part for part in (notes, "基准KRD按总DV01缩放") if part)
        return TargetKRD(as_of=ctx.as_of, krd=dict(sorted(krd.items())), notes=notes)

    def tilt(
        self, ctx: MarketContext, bucket_deltas: Mapping[float, float], *, notes: str = ""
    ) -> TargetKRD:
        """在基准上施加相对 tilt（自动截断负桶并保持总 DV01）。"""
        return relative_tilt(ctx, bucket_deltas, notes=notes)

    @staticmethod
    def history_until(ctx: MarketContext) -> pd.DataFrame:
        """只保留 ``index <= ctx.as_of`` 的历史（防御未来数据），按日期排序。"""
        history = ctx.history
        if not isinstance(history, pd.DataFrame):
            raise TypeError(f"ctx.history 必须是 DataFrame，实际 {type(history).__name__}")
        frame = history.copy()
        if not isinstance(frame.index, pd.DatetimeIndex):
            frame.index = pd.to_datetime(frame.index, errors="raise")
        cutoff = pd.Timestamp(ctx.as_of)
        frame = frame.loc[frame.index <= cutoff].sort_index()
        return frame

    @staticmethod
    def tenor_column(history: pd.DataFrame, tenor: float) -> pd.Series | None:
        """取历史面板中某期限列（浮点列名容差匹配）；缺列返回 None。"""
        for column in history.columns:
            try:
                column_f = float(column)
            except (TypeError, ValueError):
                continue
            if np.isclose(column_f, float(tenor), atol=1e-9):
                return history[column]
        return None
