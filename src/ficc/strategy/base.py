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
def _project_box_sum(
    base: Mapping[float, float],
    deltas: Mapping[float, float],
    *,
    cap_fraction: float,
    total: float,
    max_iter: int = 500,
    tol: float = 1e-12,
) -> tuple[dict[float, float], bool]:
    """把 ``base + deltas`` 投影到 {桶界 [base−c, base+c] ∩ 非负, 总和=total}。

    R28：先裁剪再归一化会重新突破单桶上限（审查反例最终偏离 79.6% > 50%）。
    这里用**交替投影**（裁剪到盒 → 按比例缩放到目标和），两个集合都是凸集且基点
    本身在交集内，因此必收敛到交集内一点，最终解**同时**满足逐桶上限与总量。
    """
    keys = sorted(base)
    lower = {t: max(0.0, float(base[t]) * (1.0 - cap_fraction)) for t in keys}
    upper = {t: float(base[t]) * (1.0 + cap_fraction) for t in keys}
    target = {
        t: min(max(float(base[t]) + float(deltas.get(t, 0.0)), lower[t]), upper[t]) for t in keys
    }
    clipped = any(abs(target[t] - (float(base[t]) + float(deltas.get(t, 0.0)))) > tol for t in keys)
    for _ in range(max_iter):
        current = float(sum(target.values()))
        if current <= 0.0:
            break
        target = {t: target[t] * total / current for t in keys}
        over = [t for t in keys if target[t] > upper[t] + 0.0 or target[t] < lower[t] - 0.0]
        if not over and abs(float(sum(target.values())) - total) <= tol * max(abs(total), 1.0):
            break
        target = {t: min(max(target[t], lower[t]), upper[t]) for t in keys}
    return target, clipped


def _project_simplex_cap(
    raw: Mapping[str, float], *, cap: float, max_iter: int = 500, tol: float = 1e-12
) -> tuple[dict[str, float], bool]:
    """把原始权重投影到 {0≤x≤cap, Σx≤1}（R28：单桶上限不能靠"裁剪后再归一化"）。

    若 ``cap × n < 1``，预算无法用完，返回全 cap（并标记为未用满）。
    """
    keys = sorted(raw)
    if not keys:
        return {}, False
    if cap <= 0:
        raise ValueError(f"cap 必须为正数，实际 {cap!r}")
    raw_sum = float(sum(raw.values()))
    if raw_sum <= 0:
        return {k: 0.0 for k in keys}, False
    shares = {k: float(raw[k]) / raw_sum for k in keys}
    if cap * len(keys) < 1.0 - tol:
        return {k: cap for k in keys}, True
    for _ in range(max_iter):
        current = float(sum(shares.values()))
        if current > 0:
            shares = {k: shares[k] / current for k in keys}
        if all(v <= cap + tol for v in shares.values()):
            break
        excess = 0.0
        for k in keys:
            if shares[k] > cap:
                excess += shares[k] - cap
                shares[k] = cap
        free = [k for k in keys if shares[k] < cap - tol]
        free_sum = float(sum(shares[k] for k in free))
        if free and free_sum > 0:
            for k in free:
                shares[k] += excess * shares[k] / free_sum
    return shares, False


def relative_tilt(
    ctx: MarketContext,
    bucket_deltas: Mapping[float, float],
    *,
    max_bucket_fraction: float = 0.5,
    notes: str = "",
) -> TargetKRD:
    """在基准 KRD 上加桶 tilt，并保持非负 + 总 DV01 守恒 + 单桶边界。

    最终解由 :func:`_project_box_sum` 给出，**同时**满足：

    - 每桶 ``|target − base| ≤ max_bucket_fraction × base``（默认 50%）；
    - ``target ≥ 0``（现券禁裸空）；
    - ``Σ target = benchmark.total_dv01``（相对基准 DV01 中性）。

    注意：不能"先裁剪 delta 再按比例缩放总量"——归一化会把桶推回界外（R28 反例
    最终偏离 79.6% > 声明 50%）。预算用不完时结果会落在可行域内，不强行再分配。
    """
    base = {float(tenor): float(value) for tenor, value in ctx.benchmark.krd.items()}
    desired_total = float(ctx.benchmark.total_dv01)
    if not np.isfinite(desired_total) or desired_total <= 0:
        raise ValueError(f"benchmark.total_dv01 必须为正数，实际 {desired_total!r}")
    if not (0.0 < max_bucket_fraction <= 1.0):
        raise ValueError(f"max_bucket_fraction 必须在 (0, 1]，实际 {max_bucket_fraction!r}")
    deltas: dict[float, float] = {}
    for tenor, delta in bucket_deltas.items():
        tenor_f = float(tenor)
        delta_f = float(delta)
        if not np.isfinite(tenor_f) or tenor_f <= 0:
            raise ValueError(f"tilt 的期限必须为正数，实际 {tenor!r}")
        if not np.isfinite(delta_f):
            raise ValueError(f"tilt[{tenor_f}] 不是有限数值: {delta!r}")
        deltas[tenor_f] = delta_f

    target, clipped = _project_box_sum(
        base, deltas, cap_fraction=max_bucket_fraction, total=desired_total
    )
    total_after = float(sum(target.values()))
    if abs(total_after - desired_total) > 1e-6 * max(desired_total, 1.0):
        raise RuntimeError(f"relative_tilt 总量对齐失败: {total_after} vs {desired_total}")
    # 最终边界复核：任何桶都不许越界（浮点容差内）
    for tenor in base:
        cap = max_bucket_fraction * base[tenor]
        if abs(target[tenor] - base[tenor]) > cap + 1e-6 * max(base[tenor], 1.0):
            raise RuntimeError(
                f"relative_tilt 单桶越界: {tenor}Y target={target[tenor]:.6f} "
                f"base={base[tenor]:.6f} cap={cap:.6f}"
            )
    extra_notes: list[str] = []
    if clipped:
        extra_notes.append(f"tilt 经投影到 {max_bucket_fraction:.0%}×桶值可行域（未越界）")
    note_parts = [part for part in (notes.strip(), *extra_notes) if part]
    return TargetKRD(
        as_of=ctx.as_of,
        krd={tenor: float(target[tenor]) for tenor in sorted(target)},
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
