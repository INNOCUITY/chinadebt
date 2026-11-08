"""即期曲线定价与关键点冲击基（R40：全重估与 KRD 近似同源）。

R40 的根因：全重估用 YTM 曲线 ``Curve.shock`` + :func:`dirty_price_from_ytm`，
KRD 却来自 bootstrap 即期曲线的单点冲击；两张价格函数、两套冲击基，即使把幅度
降到 0.001bp 仍留下约 9.4% 的**一阶口径差**，不能被称为凸性/曲率。

本模块提供唯一的一条链路：

- :func:`price_from_spot`：用 ``SpotCurve.discount(t)`` 对债券未来现金流贴现，
  返回**全价**（元/百元面值）。它与 :func:`ficc.pricing.analytics.key_rate_durations`
  内部使用的 ``PV0`` 完全一致（同一 ``cashflows`` + ``ACT/365`` 时间 + 同一贴现），
  因此 KRD 恰好是该价格函数对关键点零息利率的一阶差分。
- :func:`spot_shock`：按关键期限对零息利率做**线性插值、两端平推**的分桶冲击，
  返回新的 ``SpotCurve``（保留 ``as_of`` / ``observation_date`` /
  ``settlement_date`` / ``calendar`` 等字段）。该冲击基与
  ``key_rate_durations`` 的权重矩阵完全相同：单点 +1bp 冲击后的价格变化
  **逐位等于**该关键点的 KRD。

于是 ``全重估 = Σ face/100 × [price_from_spot(spot_shock(...)) − price_from_spot(spot)]``
与 ``KRD ≈ −Σ KRD_k × Δz_k`` 共用同一基准价、同一贴现函数、同一冲击方向；
两者之差只剩真正的二阶/高阶项（并随冲击幅度平方收敛）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import replace
from typing import Any

import numpy as np

from ficc.config import Config
from ficc.curve.bootstrap import SpotCurve
from ficc.data.schema import BondStatic
from ficc.pricing.bond import cashflows
from ficc.pricing.conventions import day_count, validate_settlement

__all__ = ["key_rate_weights", "portfolio_krd_from_spot", "price_from_spot", "spot_shock"]


def key_rate_weights(key_tenors: Sequence[float], times: np.ndarray) -> np.ndarray:
    """关键期限线性插值权重矩阵 ``(n_keys, n_times)``（与 KRD 实现同一定义）。

    每个现金流时间 ``t`` 的零息利率写成关键点利率的线性组合
    ``z(t) = Σ_k w_k(t)·z_k``：关键点之间线性插值，首尾**平推（flat）**。
    每列权重之和恒为 1，因此「所有关键点同向 +1bp」= 整条零息曲线平行 +1bp。
    """
    keys = np.asarray(key_tenors, dtype=float).ravel()
    if keys.size < 2:
        raise ValueError(f"key_tenors 至少需要 2 个点，实际 {keys.size}")
    if not np.all(np.isfinite(keys)) or np.any(keys <= 0.0):
        raise ValueError(f"key_tenors 必须是正有限数，实际 {keys.tolist()}")
    if np.any(np.diff(keys) <= 0.0):
        raise ValueError("key_tenors 必须严格递增且不重复")
    t = np.atleast_1d(np.asarray(times, dtype=float))
    if not np.all(np.isfinite(t)) or np.any(t < 0.0):
        raise ValueError(f"times 必须是有限非负数，实际 {t!r}")
    n_keys = keys.size
    weights = np.zeros((n_keys, t.size), dtype=float)
    idx = np.searchsorted(keys, t, side="right") - 1
    idx = np.clip(idx, 0, n_keys - 2)
    left = keys[idx]
    right = keys[idx + 1]
    frac = np.clip((t - left) / (right - left), 0.0, 1.0)  # 端点外平推
    cols = np.arange(t.size)
    weights[idx, cols] = 1.0 - frac
    weights[idx + 1, cols] = frac
    return weights


def price_from_spot(
    bond: BondStatic,
    settlement: Any,
    spot: SpotCurve,
    *,
    cfg: Config | None = None,
) -> float:
    """用即期（零息）曲线贴现债券全部未来现金流，返回**全价**（元/百元面值）。

    时间口径与 KRD 一致：``t = (现金流日 − settlement).days / 365``（ACT/365 名义
    年限）；贴现因子取 ``spot.discount(t) = exp(-z(t)/100·t)``（连续复利）。
    因此 ``price_from_spot`` 就是 ``key_rate_durations`` 的基准 ``PV0``。
    """
    validate_settlement(bond, settlement)
    frame = cashflows(bond, settlement, cfg=cfg)
    times = np.asarray([day_count(settlement, day) / 365.0 for day in frame["date"]], dtype=float)
    amounts = np.asarray(frame["amount"], dtype=float)
    if times.size == 0:
        raise ValueError(f"{bond.code}@{settlement}: 无未来现金流，无法定价")
    discounts = np.asarray(spot.discount(times), dtype=float).ravel()
    if discounts.size != times.size or not np.all(np.isfinite(discounts)):
        raise ValueError(f"{bond.code}: spot.discount 返回非法贴现因子")
    return float(np.dot(amounts, discounts))


def spot_shock(
    spot: SpotCurve,
    key_tenors: Sequence[float],
    shifts_bp: Sequence[float],
) -> SpotCurve:
    """对即期曲线关键点做分桶冲击，返回新的 ``SpotCurve``（不修改原对象）。

    冲击后零息曲线在节点 ``t`` 的变化为 ``Σ_k w_k(t)·shift_k``（``w`` 与 KRD
    权重相同：关键点间线性、两端平推），单位 bp（``shifts_bp/100`` 转为百分数）。
    保留原曲线的 ``as_of`` / ``observation_date`` / ``settlement_date`` /
    ``calendar`` / ``source_curve_method``，并在 ``assumptions`` 里留痕。

    ``key_tenors`` 必须与调用方计算 KRD 时的关键期限一致，否则全重估与 KRD
    又回到不同冲击基。
    """
    keys = np.asarray(key_tenors, dtype=float).ravel()
    shifts = np.asarray(shifts_bp, dtype=float).ravel()
    if keys.size != shifts.size:
        raise ValueError(f"key_tenors 与 shifts_bp 长度不一致：{keys.size} vs {shifts.size}")
    if not np.all(np.isfinite(shifts)):
        raise ValueError(f"shifts_bp 含 NaN/inf：{shifts!r}")
    if not _is_sorted_unique_positive(keys):
        raise ValueError(f"key_tenors 必须是严格递增正数，实际 {keys.tolist()}")
    times = np.asarray(spot.times, dtype=float)
    zeros = np.asarray(spot.zero_rates_pct, dtype=float)
    weights = key_rate_weights(keys, times)  # (n_keys, n_nodes)
    node_shift_pct = (weights.T @ shifts) / 100.0
    # 关键点之外 KRD 权重是**平推**的，而 SpotCurve 的区间外是端点斜率外推。
    # 为了让 price_from_spot 在任意现金流期限（包括略超曲线末节点，如 T+N 后
    # 剩余 30.02Y）都与 KRD 的“两端平推”一致，追加两个外推缓冲节点：它们落在
    # 原曲线的同一条外推直线上（不改变基准价），但把冲击在外推段的斜率钉成 0。
    lower_time = max(float(times[0]) * 0.5, 1e-6)
    upper_time = max(float(times[-1]), float(keys[-1])) + 1000.0
    extra_times = np.asarray([lower_time, upper_time], dtype=float)
    extra_shifts_pct = (key_rate_weights(keys, extra_times).T @ shifts) / 100.0
    extra_zeros = (
        np.asarray([float(spot.zero_rate(t)) for t in extra_times], dtype=float) + extra_shifts_pct
    )
    new_times = np.concatenate([extra_times[:1], times, extra_times[1:]])
    new_zero_rates = np.concatenate([extra_zeros[:1], zeros + node_shift_pct, extra_zeros[1:]])
    assumptions = dict(spot.assumptions or {})
    assumptions["shock_basis"] = "spot_key_linear_flat"
    assumptions["shock_shifts_bp"] = {
        float(tenor): float(shift) for tenor, shift in zip(keys, shifts, strict=True)
    }
    assumptions["extrapolation_buffer_nodes"] = [float(lower_time), float(upper_time)]
    return replace(
        spot,
        times=new_times,
        zero_rates_pct=new_zero_rates,
        assumptions=assumptions,
    )


def portfolio_krd_from_spot(
    faces: Mapping[str, float],
    bonds: Sequence[BondStatic],
    settlement: Any,
    spot: SpotCurve,
    key_tenors: Sequence[float],
    *,
    cfg: Config | None = None,
) -> dict[float, float]:
    """组合即期关键点 KRD 的**解析一阶导**（元/bp，多头为正）。

    与 :func:`price_from_spot` 严格同源：对零息曲线关键点 ``z_k`` 求偏导

    ``∂P/∂z_k = Σ_i A_i·D(t_i)·(−t_i/100)·w_k(t_i)``，

    再乘 ``0.01``（1bp = 0.01 个百分点）并汇总面值。它等价于“零步长中心差分”
    的线性化，避免了 :func:`ficc.pricing.analytics.key_rate_durations` 的
    **1bp 单边差分**有限差分偏差（30Y 约 0.14%，会把小冲击下的相对误差抬到
    ~1e-3，掩盖真正的二阶项）。两者在同一冲击基下应满足

    ``price_from_spot(spot_shock(spot, keys, e_k × 1bp)) − price_from_spot(spot)``
    ``≈ KRD_k × 1bp``（差异即 1bp 单边差分误差；测试中校验）。
    """
    keys = np.asarray(key_tenors, dtype=float).ravel()
    if keys.size < 2:
        raise ValueError(f"key_tenors 至少需要 2 个点，实际 {keys.size}")
    bond_by_code = {bond.code: bond for bond in bonds}
    totals: dict[float, float] = {float(tenor): 0.0 for tenor in keys}
    for code, face in dict(faces).items():
        value = float(face)
        if value <= 0.0:
            continue
        bond = bond_by_code.get(str(code))
        if bond is None:
            raise ValueError(f"faces 中的 {code!r} 不在 bonds 里")
        validate_settlement(bond, settlement)
        frame = cashflows(bond, settlement, cfg=cfg)
        times = np.asarray(
            [day_count(settlement, day) / 365.0 for day in frame["date"]], dtype=float
        )
        amounts = np.asarray(frame["amount"], dtype=float)
        discounts = np.asarray(spot.discount(times), dtype=float).ravel()
        weights = key_rate_weights(keys, times)  # (n_keys, n_cashflows)
        # dP/dz_k[元/100面值/bp] = -Σ A·D·t·w_k / 10000（z 以百分点计，1bp=0.01%）
        sensitivities = -np.sum(
            amounts[np.newaxis, :]
            * discounts[np.newaxis, :]
            * times[np.newaxis, :]
            * weights
            / 10000.0,
            axis=1,
        )
        for index, tenor in enumerate(keys):
            totals[float(tenor)] += float(-sensitivities[index]) * value / 100.0
    return totals


def _is_sorted_unique_positive(values: np.ndarray) -> bool:
    return bool(
        values.size
        and np.all(np.isfinite(values))
        and np.all(values > 0.0)
        and np.all(np.diff(values) > 0.0)
    )
