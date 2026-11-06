"""债券现金流与价格（中债估值口径）。

口径
----
- **全价（dirty）** = 未来现金流按到期收益率贴现；**净价 = 全价 − 应计利息**；
- 报价用净价、结算与 PnL 用全价（``config.pricing.price_convention``）；
- 金额单位：**元/报价面值**（默认元/百元面值，面值取
  ``config.pricing.face_value``）；
- 贴现指数中的 ``w`` 取结算日到**下一个**付息日的占比（见
  :func:`dirty_price_from_ytm` 的注释，这是最容易写错的一行）。

配置口径
--------
本模块不硬编码金融口径默认值：报价面值、计息基准都从 ``config.pricing``
读取；需要自定义配置时把 ``cfg`` 显式传给各函数（``cfg=None`` 时才读全局
配置，不会回落硬编码默认值）。

参考：``docs/00_market_conventions.md`` §2.1（中债估值公式）、§2.4（财政部
公告全价—收益率互校，外部配对案例见 ``tests/test_pricing_official.py``）、
§2.5（memo 自算算例 C：1Y 利随本清与附息公式等价）。
"""

from __future__ import annotations

from datetime import date

import pandas as pd
from scipy.optimize import brentq

from ficc.config import Config
from ficc.data.schema import BondStatic
from ficc.pricing.conventions import (
    accrued_interest,
    annual_anniversaries,
    coupon_dates,
    coupon_period,
    day_count,
    quote_face_value,
    validate_settlement,
)

__all__ = [
    "CF_COLUMNS",
    "cashflows",
    "clean_price_from_ytm",
    "dirty_price_from_ytm",
    "ytm_from_clean_price",
    "ytm_from_dirty_price",
]

# 现金流表列名（date=现金流日, amount=元/报价面值, t_years=年化现金流时间,
# period_index=从 1 开始的付息序号）
CF_COLUMNS: tuple[str, ...] = ("date", "amount", "t_years", "period_index")

# YTM 反解的收益率扫描区间（百分数）：等价于小数 [-0.05, 0.30]。
_YTM_LOW_PCT: float = -5.0
_YTM_HIGH_PCT: float = 30.0
_YTM_GRID_STEP_PCT: float = 0.5


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------
def _future_coupon_dates(bond: BondStatic, settlement: date) -> list[date]:
    """结算日之后（严格晚于）的全部付息日。"""
    return [d for d in coupon_dates(bond) if d > settlement]


def _lump_sum_total_coupon(bond: BondStatic, *, face: float) -> float:
    """利随本清券到期时的累计票息（元/报价面值）= 完整年数 × 年票息。

    按周年序列数的完整年数，故月末/闰年锚定（如 2/29 → 2/28）也按整年计。
    """
    annual_coupon = bond.coupon_pct * face / 100.0  # 元/面值/年
    return annual_coupon * (len(annual_anniversaries(bond)) - 1)


def _remaining_days(bond: BondStatic, settlement: date) -> int:
    """结算日 → 到期日的实际天数。"""
    return day_count(settlement, bond.maturity_date)


def _solve_ytm(
    bond: BondStatic, settlement: date, target_dirty: float, *, cfg: Config | None = None
) -> float:
    """反解使全价等于 ``target_dirty`` 的到期收益率（返回百分数）。

    先在 ``[-5%, 30%]`` 上以 0.5% 步长扫描找变号区间，再用 ``brentq`` 精解，
    这样既覆盖负利率情形，又避免对非单调区间直接调用 brentq 的失败。
    """
    target = float(target_dirty)
    low = _YTM_LOW_PCT
    high = _YTM_HIGH_PCT

    def gap(ytm_pct: float) -> float:
        return dirty_price_from_ytm(bond, settlement, ytm_pct, cfg=cfg) - target

    steps = round((high - low) / _YTM_GRID_STEP_PCT)
    prev_y = low
    prev_gap = gap(prev_y)
    if abs(prev_gap) < 1e-12:
        return prev_y
    for i in range(1, steps + 1):
        y = low + _YTM_GRID_STEP_PCT * i
        cur_gap = gap(y)
        if abs(cur_gap) < 1e-12:
            return y
        if prev_gap * cur_gap < 0.0:
            return float(brentq(gap, prev_y, y, xtol=1e-12, rtol=1e-14, maxiter=200))
        prev_y, prev_gap = y, cur_gap
    raise ValueError(
        f"{bond.code}@{settlement}: 无法在 [{low}%, {high}%] 内反解收益率，"
        f"目标全价={target_dirty:.6f}（元/报价面值）"
    )


# ---------------------------------------------------------------------------
# 现金流
# ---------------------------------------------------------------------------
def cashflows(bond: BondStatic, settlement: date, *, cfg: Config | None = None) -> pd.DataFrame:
    """未来现金流表（单位：元/报价面值，默认元/百元面值）。

    Parameters
    ----------
    bond : BondStatic
        债券静态条款。
    settlement : date
        结算日，必须落在 ``[起息日, 到期日)``。
    cfg : Config | None
        显式配置；``None`` 时读取全局配置。

    Returns
    -------
    pandas.DataFrame
        列 ``date`` / ``amount`` / ``t_years`` / ``period_index``：

        - ``amount``：该次现金流金额（元/报价面值；末期含本金）；
        - ``t_years``：**年化现金流时间**，与贴现指数严格一致
          （规则附息为 ``(i-1+w)/f``；利随本清/贴现为 ``τ/365``）；
        - ``period_index``：从 1 开始的付息序号。
    """
    validate_settlement(bond, settlement)
    maturity = bond.maturity_date
    face = quote_face_value(cfg=cfg)
    annual_coupon = bond.coupon_pct * face / 100.0  # 元/面值/年
    rows: list[dict[str, object]] = []

    if bond.accrual == "coupon":
        freq = int(bond.freq)
        per_coupon = annual_coupon / freq
        period = coupon_period(bond, settlement, cfg=cfg)
        future = _future_coupon_dates(bond, settlement)
        for index, cf_date in enumerate(future, start=1):
            amount = per_coupon + (face if cf_date == maturity else 0.0)
            t_years = (index - 1 + period.w) / freq
            rows.append(
                {"date": cf_date, "amount": amount, "t_years": t_years, "period_index": index}
            )
    elif bond.accrual == "lump_sum":
        amount = face + _lump_sum_total_coupon(bond, face=face)
        t_years = _remaining_days(bond, settlement) / 365.0
        rows.append({"date": maturity, "amount": amount, "t_years": t_years, "period_index": 1})
    else:  # discount / 零息：到期一次性还本
        t_years = _remaining_days(bond, settlement) / 365.0
        rows.append({"date": maturity, "amount": face, "t_years": t_years, "period_index": 1})

    return pd.DataFrame(rows, columns=list(CF_COLUMNS))


# ---------------------------------------------------------------------------
# 价格
# ---------------------------------------------------------------------------
def dirty_price_from_ytm(
    bond: BondStatic, settlement: date, ytm_pct: float, *, cfg: Config | None = None
) -> float:
    """按到期收益率计算**全价**（中债估值口径，元/报价面值）。

    规则附息（中债估值公式）
    ------------------------
    ``PV = Σ_{i=1..n} (C/f)/(1+y/f)^(i-1+w) + F/(1+y/f)^(n-1+w)``

    其中 ``w = T / TS``：

    - ``T`` = 结算日 → **下一个**付息日的实际天数；
    - ``TS`` = 上一付息日 → 下一付息日的实际天数（整周期）。

    ⚠️ ``T`` **不是**「结算日距上一付息日」的天数。若误用距上一付息日的占比，
    刚起息时价格会错 0.5 元以上（见 memo §2.1）。

    利随本清 / 贴现
    ---------------
    - 利随本清：``PV = (F + n×C) / (1+y)^(τ/365)``，τ=结算日→到期日天数；
    - 贴现/零息：``PV = F / (1 + y×τ/365)``（货币市场单利）。

    Parameters
    ----------
    bond : BondStatic
    settlement : date
    ytm_pct : float
        到期收益率，**百分数**（1.72 表示 1.72%）。
    cfg : Config | None
        显式配置；``None`` 时读取全局配置。

    Returns
    -------
    float
        全价，元/报价面值（默认元/百元面值）。
    """
    validate_settlement(bond, settlement)
    y = ytm_pct / 100.0
    maturity = bond.maturity_date
    face = quote_face_value(cfg=cfg)
    annual_coupon = bond.coupon_pct * face / 100.0  # 元/面值/年

    if bond.accrual == "coupon":
        freq = int(bond.freq)
        base = 1.0 + y / freq
        if base <= 0.0:
            raise ValueError(f"{bond.code}: 收益率 {ytm_pct}% 导致折现因子非正（1+y/f={base}）")
        period = coupon_period(bond, settlement, cfg=cfg)
        per_coupon = annual_coupon / freq
        pv = 0.0
        for index, cf_date in enumerate(_future_coupon_dates(bond, settlement), start=1):
            amount = per_coupon + (face if cf_date == maturity else 0.0)
            pv += amount * base ** (-(index - 1 + period.w))
        return pv

    tau_days = _remaining_days(bond, settlement)
    if bond.accrual == "lump_sum":
        amount = face + _lump_sum_total_coupon(bond, face=face)
        return amount / (1.0 + y) ** (tau_days / 365.0)

    # discount / 零息：货币市场单利贴现
    denominator = 1.0 + y * tau_days / 365.0
    if denominator <= 0.0:
        raise ValueError(f"{bond.code}: 收益率 {ytm_pct}% 导致贴现分母非正（{denominator}）")
    return face / denominator


def clean_price_from_ytm(
    bond: BondStatic,
    settlement: date,
    ytm_pct: float,
    *,
    issue_price: float | None = None,
    cfg: Config | None = None,
) -> float:
    """按到期收益率计算**净价**（报价口径，元/报价面值）。

    ``净价 = 全价 − 应计利息``。``issue_price`` 仅贴现/零息债需要（见
    :func:`ficc.pricing.conventions.accrued_interest`）。
    """
    dirty = dirty_price_from_ytm(bond, settlement, ytm_pct, cfg=cfg)
    return dirty - accrued_interest(bond, settlement, issue_price=issue_price, cfg=cfg)


def ytm_from_dirty_price(
    bond: BondStatic, settlement: date, dirty_price: float, *, cfg: Config | None = None
) -> float:
    """由**全价**反解到期收益率（百分数）。

    全价是官方结算口径（财政部续发行公告的「续发行价格」即全价，见 memo §2.4），
    因此从公告价格读数时应走本函数，而不是净价口径。
    """
    return _solve_ytm(bond, settlement, dirty_price, cfg=cfg)


def ytm_from_clean_price(
    bond: BondStatic,
    settlement: date,
    clean_price: float,
    *,
    issue_price: float | None = None,
    cfg: Config | None = None,
) -> float:
    """由**净价**反解到期收益率（百分数）。

    先换算成 ``全价 = 净价 + 应计利息`` 再反解；``issue_price`` 仅贴现/零息债需要。
    """
    dirty = clean_price + accrued_interest(bond, settlement, issue_price=issue_price, cfg=cfg)
    return _solve_ytm(bond, settlement, dirty, cfg=cfg)
