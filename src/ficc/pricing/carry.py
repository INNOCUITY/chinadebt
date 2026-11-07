"""Carry / roll-down 分解（**只做分解，不做信号与仓位**）。

本模块把「持有债券一段时间的收益」拆成可解释的几块，供后续回测归因使用；
本轮需求明确：在 KRD 与 10Y 曲线偏差的持续性查清之前**不产生任何交易信号或仓位**。

主要接口
--------
- :func:`carry_roll_down`：**事前**（ex-ante）一阶代理。``roll_annual =
  −[y(τ−r) − y(τ)]·D/r``（弦长年化，r=``roll_years``），``roll = roll_annual·h/365``；
  滚动目标锁定 **T − r**（10Y → 9Y），**不是** 7Y。**不是**任意持有期的重估收益。
- :func:`roll_down_aging`：**原券老化重估**。同一张券（票息/频率/到期不变）按剩余
  期限缩短 r 年重估；2Y→1Y、8Y→7Y、15Y→14Y 均支持，不因剩余 ≤1Y 拒绝。
- :func:`decompose_realized`：**事后**（ex-post）归因，R14 冻结的**可观测因子**
  （level_all / level_long / slope / curvature）经显式基向量最小二乘投影后重估，
  拆成 ``carry / roll / curve_level / curve_slope / curve_curvature / residual /
  total``；``total`` 为全价口径、含应计与实际票息、扣融资成本的实际持有期总收益。
- :func:`ns_scenario_projection`：旧的 NS 形状情景投影，**保留但单列**——它是另一种
  情景口径，**不是可观测因子归因**，不应用于研究结论。

口径与约定
----------
- 收益率单位：**百分数**（2.35 = 2.35%）；carry/roll 也以**百分数**表示收益率。
- 修正久期一律用**全价**口径（``dv01 = 全价 × D_mod × 1bp``，禁止净价）：
  由「合成平价债」（coupon = 该期限曲线收益率）经 :mod:`ficc.pricing.bond`
  的全价对收益率中心差分得到，不硬编码久期。
- 融资成本：``funding_rate_pct=None`` 时从 ``cfg.costs`` 解析；若配置的
  ``repo_rate_source`` 是需要行情序列的 ``FDR007/FR007``，定价层没有该序列，
  **抛 :class:`FundingRateUnavailableError`，绝不默认 0 成本**。只有显式把
  ``repo_rate_source`` 设为 ``"const"`` 才使用 ``repo_rate_fallback_const``。
- 付息频率：按 memo 惯例由期限决定（>7Y 视为 10Y/30Y 档半年付，2–7Y 年付）；
  1Y 及以下为利随本清/贴现，无期间票息，不支持本分解。

参考：``docs/00_market_conventions.md`` §6.2（carry & roll-down 量级与「滚到 9Y
不是 7Y」）、§3.3（久期/DV01 口径陷阱）。
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date, timedelta

import numpy as np

from ficc.config import Config, get_config
from ficc.curve.curve import Curve
from ficc.data.schema import BondStatic
from ficc.pricing.bond import cashflows, dirty_price_from_ytm
from ficc.pricing.conventions import day_count, pricing_config

__all__ = [
    "FACTOR_TENORS",
    "FundingRateUnavailableError",
    "carry_roll_down",
    "decompose_realized",
    "factor_basis_value",
    "ns_scenario_projection",
    "par_modified_duration",
    "resolve_funding_rate_pct",
    "roll_down_aging",
]

# 1bp 的价格敏感度差分步长（百分点）：0.01 个百分点 = 1bp。
_ONE_BP_PCT: float = 0.01


class FundingRateUnavailableError(RuntimeError):
    """融资利率缺失时抛出：**不允许**静默按 0 成本处理。"""


def _resolve_config(cfg: Config | None) -> Config:
    """显式 ``cfg`` 优先；``None`` 时读全局配置（不回落硬编码默认值）。"""
    return cfg if cfg is not None else get_config()


def resolve_funding_rate_pct(funding_rate_pct: float | None, *, cfg: Config | None = None) -> float:
    """解析融资（回购）成本，单位与曲线一致：**百分数**。

    Parameters
    ----------
    funding_rate_pct : float | None
        显式的融资利率（百分数，如 2.0 表示 2%）。**推荐由数据层逐日传入**
        （DR007 / FDR007 / R007，见 memo §4.4）。
    cfg : Config | None

    Returns
    -------
    float
        融资利率（百分数）。

    Raises
    ------
    FundingRateUnavailableError
        ``funding_rate_pct is None`` 且配置无法给出可用常量利率时。定价层没有
        回购利率时间序列；默认 0 会把 carry 系统性高估（成本不是 0）。
    """
    if funding_rate_pct is not None:
        rate = float(funding_rate_pct)
        if not np.isfinite(rate):
            raise FundingRateUnavailableError(f"funding_rate_pct 非有限值: {funding_rate_pct!r}")
        return rate

    costs = _resolve_config(cfg).costs
    source = str(costs.repo_rate_source)
    fallback_pct = float(costs.repo_rate_fallback_const) * 100.0  # 配置是小数(0.01318→1.318%)
    if source == "const":
        if not np.isfinite(fallback_pct):
            raise FundingRateUnavailableError(
                "cfg.costs.repo_rate_fallback_const 非有限值，无法作为常量融资成本"
            )
        return fallback_pct
    raise FundingRateUnavailableError(
        f"定价层没有回购利率时间序列，不能默认 0 成本；当前 cfg.costs.repo_rate_source={source!r}。"
        "请由数据层取当日回购利率并显式传 funding_rate_pct，或把 repo_rate_source 设为 'const' "
        "以显式使用 repo_rate_fallback_const（历史快照，非当日值）"
    )


def _require_curve(curve: Curve, *, name: str = "curve") -> None:
    """校验曲线对象具备本模块依赖的 API（缺方法时报清楚，而不是静默算错）。"""
    for attr in ("as_of", "rate", "shift"):
        if not hasattr(curve, attr):
            raise TypeError(
                f"{name} 缺少必需成员 {attr!r}：本模块需要 ficc.curve.Curve 的 "
                "as_of / rate(t) / shift(bp)（decompose 另需 shock(kind,bp)）"
            )


def _parse_tenor_bucket(label: str) -> tuple[float, float] | None:
    """解析 ``config.pricing.frequency_by_tenor`` 的区间键，如 ``"2Y-7Y"`` / ``"10Y"``。"""
    text = str(label).strip().upper()
    try:
        if "-" in text:
            left, right = text.split("-", 1)
            return float(left.rstrip("Y")), float(right.rstrip("Y"))
        value = float(text.rstrip("Y"))
    except ValueError:
        return None
    return value, value


def _coupon_freq_for_tenor(tenor: float, *, cfg: Config | None = None) -> int:
    """按 ``config.pricing.frequency_by_tenor`` 解析合成债的付息频率。

    memo §0 结论 5：10Y/30Y 半年付、2/3/5/7Y 年付、1Y 到期一次还本付息。
    配置的区间键（``"2Y-7Y"``、``"10Y-30Y"``）不覆盖的滚动后期限（如 9Y）
    取**最近的高一档**（9Y 属 10Y 券滚动后，仍是半年付），并在 docstring 说明。
    """
    if not np.isfinite(tenor) or tenor <= 1.0:
        raise ValueError(
            f"tenor={tenor} 不适用 carry/roll 分解：1Y 及以下为利随本清/贴现券，无期间票息"
        )
    mapping = pricing_config(cfg).frequency_by_tenor
    buckets: list[tuple[float, float, int]] = []
    for label, value in mapping.items():
        if isinstance(value, str):  # "lump_sum" 等非数值频率不参与
            continue
        parsed = _parse_tenor_bucket(label)
        if parsed is not None and int(value) in (1, 2):
            buckets.append((parsed[0], parsed[1], int(value)))
    if not buckets:
        raise ValueError("config.pricing.frequency_by_tenor 未给出可用的数值付息频率")
    for low, high, freq in buckets:
        if low <= tenor <= high:
            return freq
    # 区间未覆盖：取最近的高一档；若已超过全部区间，取最高档
    above = [b for b in buckets if b[0] > tenor]
    if above:
        return min(above, key=lambda item: item[0])[2]
    return max(buckets, key=lambda item: item[1])[2]


def _shift_years(anchor: date, years: float) -> date:
    """日历口径加 ``years`` 年；整数年精确对齐，小数部分按 365 天折算。"""
    whole = int(np.floor(years))
    frac = years - whole
    target_year = anchor.year + whole
    day = min(anchor.day, calendar.monthrange(target_year, anchor.month)[1])
    result = date(target_year, anchor.month, day)
    if frac > 1e-12:
        result += timedelta(days=round(frac * 365.0))
    return result


def _synthetic_par_bond(curve: Curve, tenor: float, *, cfg: Config | None = None) -> BondStatic:
    """构造「平价债」：起息=曲线日，到期=曲线日+tenor 年，票面=该期限曲线收益率。

    在整数年期限上，该券以曲线收益率为 YTM 时全价恰好 100（平价）；小数期限
    会有末期短残段，价格接近平价（误差 <1e-3），只用于取修正久期。
    """
    rate = float(curve.rate(tenor))
    if not np.isfinite(rate) or rate < 0:
        raise ValueError(f"曲线在 {tenor}Y 的收益率为 {rate!r}，无法构造平价债")
    return BondStatic(
        code=f"SYN-PAR-{tenor:.6g}",
        issue_date=curve.as_of,
        maturity_date=_shift_years(curve.as_of, tenor),
        coupon_pct=rate,
        freq=_coupon_freq_for_tenor(tenor, cfg=cfg),
    )


def par_modified_duration(curve: Curve, tenor: float, *, cfg: Config | None = None) -> float:
    """合成平价债的修正久期（年），**全价口径**、由全价对收益率中心差分得到。

    ``D_mod = −(1/P)·dP/dy ≈ −[P(y+1bp) − P(y−1bp)] / [2·(1bp)·P]``

    其中 ``P`` 由 :func:`ficc.pricing.bond.dirty_price_from_ytm` 按全价公式计算
    （不是净价、不是硬编码久期）。1bp = 0.01 个百分点。
    """
    _require_curve(curve)
    bond = _synthetic_par_bond(curve, tenor, cfg=cfg)
    y0 = float(curve.rate(tenor))
    price0 = dirty_price_from_ytm(bond, curve.as_of, y0, cfg=cfg)
    if price0 <= 0:
        raise ValueError(f"合成平价债价格非正（{price0}），检查曲线/条款")
    price_up = dirty_price_from_ytm(bond, curve.as_of, y0 + _ONE_BP_PCT, cfg=cfg)
    price_down = dirty_price_from_ytm(bond, curve.as_of, y0 - _ONE_BP_PCT, cfg=cfg)
    return -(price_up - price_down) / (2.0 * (_ONE_BP_PCT / 100.0) * price0)


def carry_roll_down(
    tenor: float,
    curve: Curve,
    *,
    horizon_days: int = 1,
    funding_rate_pct: float | None = None,
    roll_years: float | None = None,
    mod_duration: float | None = None,
    cfg: Config | None = None,
) -> dict[str, float]:
    """事前 carry / roll-down 估算（收益率百分数，持有 ``horizon_days`` 天）。

    契约（R18：步长与持有期必须自洽）
    --------------------------------
    - ``carry_annual_pct = y_τ − funding``；
    - ``roll_annual_pct  = −[y(τ−r) − y(τ)] · D_mod / r``（**弦长年化代理**，
      r = ``roll_years``）；
    - ``roll_pct = roll_annual_pct · h/365``，``carry_pct = carry_annual · h/365``；
    - ``total_pct = carry_pct + roll_pct``。

    ⚠️ 这是 **固定步长 r 的弦长一阶代理**，**不是**任意持有期的重估收益：它假设
    收益率随剩余期限线性滑动、久期不变。需要严格重估时请用
    :func:`roll_down_aging`（原券老化）或 :func:`decompose_realized`（事后）。
    此前实现漏了 ``/r``，会把 ``roll_years=0.5`` 的年化滚降减半（评审 R18 反例：
    线性 5bp/年、D=5、h=365 时 r=0.5 会错误得到 0.125%，正确 0.25%）。

    **滚动目标是 T − r（10Y → 9Y），不是 7Y**（memo §6.2：用 7Y 会把骑乘收益
    高估约 0.9pct）。``r`` 默认读 ``cfg.backtest.roll_down_step_years``（1.0）。

    ``D_mod`` 默认由曲线解析算：合成平价债在 **τ−r**（如 9Y）处的全价修正久期
    （经中心差分，见 :func:`par_modified_duration`），不是硬编码、也不是净价口径。

    Parameters
    ----------
    tenor : float
        持有券的期限（年），如 10.0 表示 10Y。
    curve : Curve
        t0 的 YTM 曲线（``ficc.curve.Curve``，需支持 ``rate`` / ``shift``）。
    horizon_days : int
        持有期（自然日）；0 时 carry/roll/total 全为 0。
    funding_rate_pct : float | None
        融资利率（百分数）；``None`` 时按 :func:`resolve_funding_rate_pct`
        从 ``cfg.costs`` 解析，缺数据抛 :class:`FundingRateUnavailableError`。
    roll_years : float | None
        滚动年限 r，默认 ``cfg.backtest.roll_down_step_years``。
    mod_duration : float | None
        显式指定修正久期（年）；``None`` 时按 τ−r 的合成平价债解析算。
    cfg : Config | None

    Returns
    -------
    dict[str, float]
        含 ``carry_pct`` / ``roll_pct`` / ``total_pct`` /
        ``carry_annual_pct`` / ``roll_annual_pct`` / ``total_annual_pct`` /
        ``y_tenor_pct`` / ``roll_target_tenor`` / ``roll_target_y_pct`` /
        ``funding_pct`` / ``mod_duration`` / ``roll_years`` / ``horizon_years``。
    """
    _require_curve(curve)
    tenor_f = float(tenor)
    if not np.isfinite(tenor_f) or tenor_f <= 1.0:
        raise ValueError(f"tenor 必须 >1Y（1Y 及以下为利随本清/贴现券），实际 {tenor!r}")
    if not isinstance(horizon_days, int) or horizon_days < 0:
        raise ValueError(f"horizon_days 必须为非负整数，实际 {horizon_days!r}")

    config = _resolve_config(cfg)
    roll_years_f = (
        float(config.backtest.roll_down_step_years) if roll_years is None else float(roll_years)
    )
    if not np.isfinite(roll_years_f) or roll_years_f <= 0:
        raise ValueError(f"roll_years 必须为正数，实际 {roll_years!r}")
    target_tenor = tenor_f - roll_years_f
    if target_tenor <= 0:
        raise ValueError(
            f"roll_years={roll_years_f} 大于 tenor={tenor_f}，滚动目标期限为 {target_tenor}"
        )

    y_tenor = float(curve.rate(tenor_f))
    y_target = float(curve.rate(target_tenor))
    funding_pct = resolve_funding_rate_pct(funding_rate_pct, cfg=cfg)
    duration = (
        par_modified_duration(curve, target_tenor, cfg=cfg)
        if mod_duration is None
        else float(mod_duration)
    )
    if not np.isfinite(duration) or duration <= 0:
        raise ValueError(f"mod_duration 必须为正数，实际 {mod_duration!r}")

    horizon_years = horizon_days / 365.0
    carry_annual_pct = y_tenor - funding_pct
    roll_annual_pct = -(y_target - y_tenor) * duration / roll_years_f
    carry_pct = carry_annual_pct * horizon_years
    roll_pct = roll_annual_pct * horizon_years
    total_pct = carry_pct + roll_pct
    total_annual_pct = total_pct * 365.0 / horizon_days if horizon_days > 0 else 0.0

    return {
        "carry_pct": carry_pct,
        "roll_pct": roll_pct,
        "total_pct": total_pct,
        "carry_annual_pct": carry_annual_pct if horizon_days > 0 else 0.0,
        "roll_annual_pct": roll_annual_pct if horizon_days > 0 else 0.0,
        "total_annual_pct": total_annual_pct,
        "y_tenor_pct": y_tenor,
        "roll_target_tenor": target_tenor,
        "roll_target_y_pct": y_target,
        "funding_pct": funding_pct,
        "mod_duration": duration,
        "roll_years": roll_years_f,
        "horizon_years": horizon_years,
    }


def roll_down_aging(
    bond: BondStatic,
    settlement: date,
    curve: Curve,
    *,
    roll_years: float | None = None,
    cfg: Config | None = None,
) -> dict[str, float]:
    """**原券老化重估**：同一张券（票息/频率/到期日不变）按剩余期限缩短 ``r`` 年重估。

    与 :func:`carry_roll_down` 的弦长代理不同，这里**不重建平价券**：用 ``bond``
    的原始现金流，在 ``settlement + r 年`` 的结算日、按缩短后的剩余期限查 t0 曲线
    收益率重新贴现，得到价格变化。因此：

    - 票息与付息频率始终是原券条款（8Y 年付→7Y 仍年付；15Y 半年付→14Y 仍半年付）；
    - 2Y→1Y 也允许（剩余期限 ≤1Y 不构成拒绝理由——券的条款没有变，仍是付息债）。

    返回字段（金额：元/报价面值；收益：百分数）

    - ``p0_dirty`` / ``p1_dirty``：老化前/后全价；
    - ``roll_price_change``：``p1_dirty − p0_dirty``（含应计变化）；
    - ``coupon_paid``：持有期内实际收到的票息；``accrued_income``：票息应计；
    - ``roll_pct``：剔除票息应计后的**滚降收益** =
      ``(ΔP + coupon_paid − accrued_income)/P0``；
    - ``roll_annual_pct``：``roll_pct × 365/h``（实际持有期年化，非弦长代理）；
    - ``price_return_pct``：``(ΔP + coupon_paid)/P0``（不含融资成本的价格收益）；
    - 另有 ``coupon_freq`` / ``coupon_pct`` / ``initial_tenor_years`` /
      ``aged_tenor_years`` / ``y_initial_pct`` / ``y_aged_pct`` / ``horizon_years``。

    Notes
    -----
    这是**用 t0 曲线做的滚降重估**，不含 t0→t1 的真实曲线变动与融资成本；
    完整持有期归因请用 :func:`decompose_realized`。
    """
    _require_curve(curve)
    if not isinstance(bond, BondStatic):
        raise TypeError(f"bond 必须是 BondStatic，实际 {type(bond).__name__}")
    if not isinstance(settlement, date):
        raise TypeError(f"settlement 必须是 date，实际 {type(settlement).__name__}")
    if settlement < bond.issue_date or settlement >= bond.maturity_date:
        raise ValueError(
            f"{bond.code}: 结算日 {settlement} 必须在 [起息日 {bond.issue_date}, "
            f"到期日 {bond.maturity_date}) 内"
        )
    config = _resolve_config(cfg)
    roll_years_f = (
        float(config.backtest.roll_down_step_years) if roll_years is None else float(roll_years)
    )
    if not np.isfinite(roll_years_f) or roll_years_f <= 0:
        raise ValueError(f"roll_years 必须为正数，实际 {roll_years!r}")

    t1_date = _shift_years(settlement, roll_years_f)
    if t1_date >= bond.maturity_date:
        raise ValueError(
            f"{bond.code}: 老化 roll_years={roll_years_f} 后（{t1_date}）已到期，无法重估"
        )
    horizon_years = day_count(settlement, t1_date) / 365.0
    initial_tenor = day_count(settlement, bond.maturity_date) / 365.0
    aged_tenor = day_count(t1_date, bond.maturity_date) / 365.0
    y_initial = float(curve.rate(initial_tenor))
    y_aged = float(curve.rate(aged_tenor))

    p0 = dirty_price_from_ytm(bond, settlement, y_initial, cfg=cfg)
    p1 = dirty_price_from_ytm(bond, t1_date, y_aged, cfg=cfg)
    frame = cashflows(bond, settlement, cfg=cfg)
    coupon_paid = float(
        sum(
            amount
            for cf_date, amount in zip(frame["date"], frame["amount"], strict=True)
            if settlement < cf_date <= t1_date
        )
    )
    accrued_income = float(bond.coupon_pct) * horizon_years
    roll_price_change = p1 - p0
    roll_pct = (roll_price_change + coupon_paid - accrued_income) / p0 * 100.0
    roll_annual_pct = roll_pct / horizon_years if horizon_years > 0 else 0.0
    price_return_pct = (roll_price_change + coupon_paid) / p0 * 100.0

    return {
        "p0_dirty": p0,
        "p1_dirty": p1,
        "roll_price_change": roll_price_change,
        "coupon_paid": coupon_paid,
        "accrued_income": accrued_income,
        "roll_pct": roll_pct,
        "roll_annual_pct": roll_annual_pct,
        "price_return_pct": price_return_pct,
        "coupon_freq": float(bond.freq),
        "coupon_pct": float(bond.coupon_pct),
        "initial_tenor_years": initial_tenor,
        "aged_tenor_years": aged_tenor,
        "y_initial_pct": y_initial,
        "y_aged_pct": y_aged,
        "horizon_years": horizon_years,
    }


# ---------------------------------------------------------------------------
# 事后分解：可观测因子（R14 冻结口径）与公共中间量
# ---------------------------------------------------------------------------
# 冻结的可观测因子定义（R14；与曲线线 ``FACTOR_COLUMNS`` 目标契约一致）。
FACTOR_TENORS: tuple[float, ...] = (
    0.25,
    0.5,
    1.0,
    2.0,
    3.0,
    5.0,
    7.0,
    10.0,
    15.0,
    20.0,
    30.0,
)
LEVEL_LONG_TENORS: tuple[float, ...] = (10.0, 15.0, 20.0, 30.0)
SLOPE_SHORT_TENOR: float = 2.0
SLOPE_LONG_TENOR: float = 10.0
CURVATURE_TENORS: tuple[float, float, float] = (5.0, 2.0, 10.0)  # 2×5Y − 2Y − 10Y

# 显式基向量节点（bp / 单位因子）；节点间线性插值，范围外平推：
# level_all: 全部期限 +1bp；slope: 10Y +0.5 / 2Y −0.5；curvature: 5Y +1 / 2Y −0.5 / 10Y −0.5。
_SLOPE_KNOTS: tuple[tuple[float, float], ...] = ((2.0, -0.5), (10.0, 0.5))
_CURVATURE_KNOTS: tuple[tuple[float, float], ...] = ((2.0, -0.5), (5.0, 1.0), (10.0, -0.5))


def _knots_value(knots: tuple[tuple[float, float], ...], t: float) -> float:
    """分段线性插值（范围外由 ``np.interp`` 平推）。"""
    xs = np.asarray([knot[0] for knot in knots], dtype=float)
    ys = np.asarray([knot[1] for knot in knots], dtype=float)
    return float(np.interp(float(t), xs, ys))


def factor_basis_value(tenor: float) -> tuple[float, float, float]:
    """``tenor`` 处三条可观测因子基向量的取值（bp / 单位因子）。

    返回 ``(level_all, slope, curvature)``，定义见 R14：

    - ``level_all``：所有期限 +1bp → 基值恒 1；
    - ``slope``：10Y +0.5bp、2Y −0.5bp，中间线性插值、范围外平推；
    - ``curvature``：5Y +1bp、2Y −0.5bp、10Y −0.5bp，中间线性插值、范围外平推。
    """
    t = float(tenor)
    return 1.0, _knots_value(_SLOPE_KNOTS, t), _knots_value(_CURVATURE_KNOTS, t)


def _observable_factor_projection(
    curve_t0: Curve, curve_t1: Curve
) -> tuple[np.ndarray, dict[str, float], float]:
    """把 ``Δy``（bp）投影到 R14 冻结的三条可观测因子基（最小二乘）。

    返回 ``(系数, 可观测因子变化, 基无法解释的节点残差 RMS)``：

    - 系数 ``[level_all, slope, curvature]``（单位因子，bp），用于后文
      「系数 × 基向量重估」；
    - 可观测因子变化（bp）：``level_all_bp``、``level_long_bp``、``slope_bp``、
      ``curvature_bp`` 按 R14 定义直接由观测收益率算出；
    - 残差 RMS：三条基张成空间之外的 Δy（进入价格 residual，不丢弃）。
    """
    tenors = np.asarray(FACTOR_TENORS, dtype=float)
    dy_bp = (
        np.asarray(curve_t1.rate(tenors), dtype=float)
        - np.asarray(curve_t0.rate(tenors), dtype=float)
    ) * 100.0
    # 设计矩阵：行=期限，列=[level_all, slope, curvature] 三条基
    design = np.asarray([factor_basis_value(t) for t in tenors], dtype=float)
    coefficients, *_ = np.linalg.lstsq(design, dy_bp, rcond=None)
    fitted = design @ coefficients
    rate_residual_bp = float(np.sqrt(np.mean((dy_bp - fitted) ** 2)))

    index = {float(t): i for i, t in enumerate(tenors)}
    long_indices = [index[t] for t in LEVEL_LONG_TENORS]
    observed = {
        "level_all_bp": float(np.mean(dy_bp)),
        "level_long_bp": float(np.mean(dy_bp[long_indices])),
        "slope_bp": float(dy_bp[index[SLOPE_LONG_TENOR]] - dy_bp[index[SLOPE_SHORT_TENOR]]),
        "curvature_bp": float(
            2.0 * dy_bp[index[CURVATURE_TENORS[0]]]
            - dy_bp[index[CURVATURE_TENORS[1]]]
            - dy_bp[index[CURVATURE_TENORS[2]]]
        ),
    }
    return coefficients, observed, rate_residual_bp


def _require_curve_rate(curve: Curve, *, name: str = "curve") -> None:
    """事后分解只依赖曲线的 ``as_of`` / ``rate``（可观测因子归因不用 ``shock``）。"""
    for attr in ("as_of", "rate"):
        if not hasattr(curve, attr):
            raise TypeError(
                f"{name} 缺少必需成员 {attr!r}：需要 ficc.curve.Curve 的 as_of / rate(t)"
            )


@dataclass(frozen=True)
class _RealizedBase:
    """事后分解的公共中间量（t0/t1 两条曲线 + 同一只债）。"""

    bond: BondStatic
    t0_date: date
    t1_date: date
    horizon_years: float
    remaining_tenor: float
    p0: float
    p1: float
    p_roll: float
    y_tenor_pct: float
    y_roll_pct: float
    funding_pct: float
    coupon_paid: float
    accrued_income: float
    carry_pct: float
    roll_pct: float
    total_pct: float


def _realized_base(
    tenor: float,
    curve_t0: Curve,
    curve_t1: Curve,
    *,
    horizon_days: int,
    funding_rate_pct: float | None,
    bond: BondStatic | None,
    cfg: Config | None,
) -> _RealizedBase:
    """校验输入并计算 t0/t1 重估的公共量（carry / roll / total）。"""
    _require_curve_rate(curve_t0, name="curve_t0")
    _require_curve_rate(curve_t1, name="curve_t1")
    tenor_f = float(tenor)
    if not np.isfinite(tenor_f) or tenor_f <= 0:
        raise ValueError(f"tenor 必须为正数，实际 {tenor!r}")
    if not isinstance(horizon_days, int) or horizon_days < 0:
        raise ValueError(f"horizon_days 必须为非负整数，实际 {horizon_days!r}")

    t0_date = curve_t0.as_of
    t1_date = curve_t1.as_of
    actual_days = day_count(t0_date, t1_date)
    if actual_days != horizon_days:
        raise ValueError(
            f"horizon_days={horizon_days} 与两条曲线日期差 {t0_date}→{t1_date}"
            f"（{actual_days} 天）不一致"
        )
    horizon_years = horizon_days / 365.0

    if bond is None:
        if tenor_f <= 1.0:
            raise ValueError(f"tenor 必须 >1Y（1Y 及以下无期间票息），实际 {tenor!r}")
        working_bond = _synthetic_par_bond(curve_t0, tenor_f, cfg=cfg)
        initial_tenor = tenor_f
    else:
        if not isinstance(bond, BondStatic):
            raise TypeError(f"bond 必须是 BondStatic 或 None，实际 {type(bond).__name__}")
        if t0_date < bond.issue_date or t0_date >= bond.maturity_date:
            raise ValueError(
                f"{bond.code}: t0={t0_date} 不在 [起息日 {bond.issue_date}, 到期日 "
                f"{bond.maturity_date}) 内"
            )
        working_bond = bond
        initial_tenor = day_count(t0_date, bond.maturity_date) / 365.0

    y_tenor = float(curve_t0.rate(initial_tenor))
    p0 = dirty_price_from_ytm(working_bond, t0_date, y_tenor, cfg=cfg)
    frame = cashflows(working_bond, t0_date, cfg=cfg)
    coupon_paid = float(
        sum(
            amount
            for cf_date, amount in zip(frame["date"], frame["amount"], strict=True)
            if t0_date < cf_date <= t1_date
        )
    )

    if bond is None:
        # 平价券重建：残余期限用「tenor − h/365」保证 h=0 时连续回到 tenor
        remaining_tenor = tenor_f - horizon_years
    else:
        # 固定券：按剩余期限自然老化（到期日固定），与 roll_down_aging 一致
        remaining_tenor = day_count(t1_date, working_bond.maturity_date) / 365.0
    if remaining_tenor <= 0:
        raise ValueError(
            f"t1={t1_date} 时债券已到期（残余期限 {remaining_tenor:.4f} 年），无法分解"
        )

    y_roll = float(curve_t0.rate(remaining_tenor))
    y_t1 = float(curve_t1.rate(remaining_tenor))
    p_roll = dirty_price_from_ytm(working_bond, t1_date, y_roll, cfg=cfg)
    p1 = dirty_price_from_ytm(working_bond, t1_date, y_t1, cfg=cfg)

    funding_pct = resolve_funding_rate_pct(funding_rate_pct, cfg=cfg)
    accrued_income = float(working_bond.coupon_pct) * horizon_years
    funding_frac = funding_pct / 100.0 * horizon_years
    pct = 100.0 / p0
    carry_pct = (accrued_income - funding_frac * p0) * pct
    roll_pct = ((p_roll - p0) + coupon_paid - accrued_income) * pct
    total_pct = ((p1 - p0) + coupon_paid) * pct - funding_frac * 100.0
    return _RealizedBase(
        bond=working_bond,
        t0_date=t0_date,
        t1_date=t1_date,
        horizon_years=horizon_years,
        remaining_tenor=remaining_tenor,
        p0=p0,
        p1=p1,
        p_roll=p_roll,
        y_tenor_pct=y_tenor,
        y_roll_pct=y_roll,
        funding_pct=funding_pct,
        coupon_paid=coupon_paid,
        accrued_income=accrued_income,
        carry_pct=carry_pct,
        roll_pct=roll_pct,
        total_pct=total_pct,
    )


def _observable_attribution(
    base: _RealizedBase, coefficients: np.ndarray, *, cfg: Config | None
) -> tuple[float, float, float]:
    """「系数 × 显式基向量」序贯重估，返回 level/slope/curvature 的价格贡献（%）。

    在残余期限处取基值，把因子冲击换算成该期限的收益率变化（bp），再用
    :func:`ficc.pricing.bond.dirty_price_from_ytm` 依次重估（level → slope →
    curvature）。三项之和 = 三因子联合冲击对价格的一阶效应；二阶项进 residual。
    """
    level_basis, slope_basis, curvature_basis = factor_basis_value(base.remaining_tenor)
    delta_level = float(coefficients[0]) * level_basis
    delta_slope = float(coefficients[1]) * slope_basis
    delta_curvature = float(coefficients[2]) * curvature_basis

    def price_with(delta_bp: float) -> float:
        return dirty_price_from_ytm(
            base.bond, base.t1_date, base.y_roll_pct + delta_bp / 100.0, cfg=cfg
        )

    price_level = price_with(delta_level)
    price_level_slope = price_with(delta_level + delta_slope)
    price_all = price_with(delta_level + delta_slope + delta_curvature)
    scale = 100.0 / base.p0
    return (
        (price_level - base.p_roll) * scale,
        (price_level_slope - price_level) * scale,
        (price_all - price_level_slope) * scale,
    )


def decompose_realized(
    tenor: float,
    curve_t0: Curve,
    curve_t1: Curve,
    *,
    horizon_days: int,
    funding_rate_pct: float | None = None,
    bond: BondStatic | None = None,
    cfg: Config | None = None,
) -> dict[str, float]:
    """事后收益分解：可观测因子归因（R14）+ carry / roll / residual / total。

    口径
    ----
    用同一只债在 t0/t1 两条 YTM 曲线上重估（``bond=None`` 时按 ``tenor`` 新建
    t0 平价券；给定 ``bond`` 时按**原券条款**重估，t1 的剩余期限自然缩短）：

    - ``total``：全价口径实际持有期总收益（含持有期内实际收到的票息、扣融资成本），
      以 t0 全价的百分比表示；
    - ``carry``：票息应计 − 融资成本；
    - ``roll``：曲线**不动**时沿曲线下滑（t1 残余期限用 t0 曲线重估）带来、
      剔除票息应计后的价格变化；
    - ``curve_level`` / ``curve_slope`` / ``curve_curvature``：**R14 冻结的可观测
      因子**归因（见下）；
    - ``residual``：``total − (carry + roll + level + slope + curvature)``，即一阶
      因子近似的误差（凸性、因子基张成空间之外的 Δy、交叉项），**不是数据缺失**。

    可观测因子（R14 冻结）
    ----------------------
    - ``level_all``：11 个标准期限（``FACTOR_TENORS``）等权均值；
    - ``level_long``：10/15/20/30Y 均值（只报告，不单独归因）；
    - ``slope``：10Y − 2Y；
    - ``curvature``：2×5Y − 2Y − 10Y。

    把 ``Δy = y_t1 − y_t0``（bp）对三条**显式基向量**做线性最小二乘投影：

    - level_all → 11 期限平行 +1bp；
    - slope → 10Y +0.5bp / 2Y −0.5bp（中间线性插值、范围外平推）；
    - curvature → 5Y +1bp / 2Y −0.5bp / 10Y −0.5bp（同样插值/平推）。

    系数 × 基向量在 t1 残余期限处重估，得到 ``curve_level / curve_slope /
    curve_curvature``。另返回直接由观测收益率算出的因子变化
    ``level_all_bp / level_long_bp / slope_bp / curvature_bp`` 与
    ``factor_fit_residual_bp``（三条基无法解释的 Δy RMS）。

    注意：这是**一阶因子近似**，不是官方即期对账；NS/情景形状投影是另一套口径，
    已单列为 :func:`ns_scenario_projection`，两者不可混用语义。

    恒等式：``carry + roll + curve_level + curve_slope + curve_curvature +
    residual == total``（浮点误差级）。

    Parameters
    ----------
    tenor : float
        t0 时的期限（年）。``bond`` 给定时仅作兼容参数，实际期限由债券到期日决定。
    curve_t0, curve_t1 : Curve
        t0 / t1 的 YTM 曲线（各自带 ``as_of``）。
    horizon_days : int
        持有期（自然日），必须与 ``curve_t0.as_of → curve_t1.as_of`` 的天数一致。
    funding_rate_pct : float | None
        融资利率（百分数），规则同 :func:`carry_roll_down`。
    bond : BondStatic | None
        给定则按该券（票息/频率/到期固定）重估——为账本接入预留：
        连续两日重估时，剩余期限自然缩短，而不是每天新建平价券。
    cfg : Config | None

    Returns
    -------
    dict[str, float]
        ``carry / roll / curve_level / curve_slope / curve_curvature / residual /
        total``，另附因子变化与拟合残差等诊断字段。
    """
    base = _realized_base(
        tenor,
        curve_t0,
        curve_t1,
        horizon_days=horizon_days,
        funding_rate_pct=funding_rate_pct,
        bond=bond,
        cfg=cfg,
    )
    coefficients, observed, rate_residual_bp = _observable_factor_projection(curve_t0, curve_t1)
    curve_level, curve_slope, curve_curvature = _observable_attribution(base, coefficients, cfg=cfg)
    residual = base.total_pct - (
        base.carry_pct + base.roll_pct + curve_level + curve_slope + curve_curvature
    )
    return {
        "carry": base.carry_pct,
        "roll": base.roll_pct,
        "curve_level": curve_level,
        "curve_slope": curve_slope,
        "curve_curvature": curve_curvature,
        "residual": residual,
        "total": base.total_pct,
        "level_all_bp": observed["level_all_bp"],
        "level_long_bp": observed["level_long_bp"],
        "slope_bp": observed["slope_bp"],
        "curvature_bp": observed["curvature_bp"],
        "factor_fit_residual_bp": rate_residual_bp,
        "funding_pct": base.funding_pct,
        "y_tenor_pct": base.y_tenor_pct,
        "horizon_years": base.horizon_years,
    }


# ---------------------------------------------------------------------------
# NS 情景投影（另一种口径，明确不是可观测因子归因）
# ---------------------------------------------------------------------------
def _ns_factor_changes(curve_t0: Curve, curve_t1: Curve) -> tuple[float, float, float, float]:
    """把 Δy 投影到 ``Curve.shock`` 的 NS 形状（steepen / butterfly）。

    这是 **NS 情景投影**，不是可观测因子归因：基向量由 ``Curve.shock`` 生成，
    依赖曲线 τ，与 R14 冻结的 level_all/slope/curvature 语义不同。
    """
    nodes = np.asarray(curve_t0.tenors, dtype=float)
    dy_bp = (
        np.asarray(curve_t1.rate(nodes), dtype=float)
        - np.asarray(curve_t0.rate(nodes), dtype=float)
    ) * 100.0
    base_bp = np.asarray(curve_t0.rate(nodes), dtype=float) * 100.0
    steepen_bp = (
        np.asarray(curve_t0.shock("steepen", 1.0).rate(nodes), dtype=float) * 100.0 - base_bp
    )
    butterfly_bp = (
        np.asarray(curve_t0.shock("butterfly", 1.0).rate(nodes), dtype=float) * 100.0 - base_bp
    )
    design = np.column_stack([np.ones_like(nodes), steepen_bp, butterfly_bp])
    coefficients, *_ = np.linalg.lstsq(design, dy_bp, rcond=None)
    fitted = design @ coefficients
    rate_residual_bp = float(np.sqrt(np.mean((dy_bp - fitted) ** 2)))
    level_bp, slope_bp, curvature_bp = (float(v) for v in coefficients)
    return level_bp, slope_bp, curvature_bp, rate_residual_bp


def _ns_factor_sensitivities(
    bond: BondStatic,
    curve_t0: Curve,
    settlement_t1: date,
    remaining_tenor: float,
    p_roll: float,
    p0: float,
    *,
    cfg: Config | None = None,
) -> tuple[float, float, float]:
    """NS 形状冲击（1bp 幅度）的价格敏感度（%收益 / 因子 bp）。"""
    for attr in ("shift", "shock"):
        if not hasattr(curve_t0, attr):
            raise TypeError(
                f"curve_t0 缺少必需成员 {attr!r}：ns_scenario_projection 需要 "
                "ficc.curve.Curve 的 shift(bp) 与 shock(kind,bp)"
            )

    def repriced(curve_bumped: Curve) -> float:
        y = float(curve_bumped.rate(remaining_tenor))
        return dirty_price_from_ytm(bond, settlement_t1, y, cfg=cfg)

    s_level = (repriced(curve_t0.shift(1.0)) - p_roll) / p0 * 100.0
    s_slope = (repriced(curve_t0.shock("steepen", 1.0)) - p_roll) / p0 * 100.0
    s_curv = (repriced(curve_t0.shock("butterfly", 1.0)) - p_roll) / p0 * 100.0
    return s_level, s_slope, s_curv


def ns_scenario_projection(
    tenor: float,
    curve_t0: Curve,
    curve_t1: Curve,
    *,
    horizon_days: int,
    funding_rate_pct: float | None = None,
    bond: BondStatic | None = None,
    cfg: Config | None = None,
) -> dict[str, float]:
    """NS 情景投影：把 Δy 投影到 ``Curve.shock`` 的 NS 形状（steepen/butterfly）。

    ⚠️ **这是另一种情景投影，不是可观测因子归因**（R14）。基向量依赖曲线 τ，
    与 :func:`decompose_realized` 冻结的 ``level_all / slope / curvature`` 语义
    不同，两者不可混用；保留它是为了披露/对照，不应作为研究结论的因子归因。

    返回 ``carry / roll / curve_level / curve_shape / residual / total``，
    另附 NS 投影幅度 ``level_bp / slope_bp / curvature_bp`` 与
    ``factor_fit_residual_bp``。``carry / roll / total`` 与
    :func:`decompose_realized` 共用 :func:`_realized_base`，因此两者可直接对比。
    """
    base = _realized_base(
        tenor,
        curve_t0,
        curve_t1,
        horizon_days=horizon_days,
        funding_rate_pct=funding_rate_pct,
        bond=bond,
        cfg=cfg,
    )
    level_bp, slope_bp, curvature_bp, rate_residual_bp = _ns_factor_changes(curve_t0, curve_t1)
    s_level, s_slope, s_curv = _ns_factor_sensitivities(
        base.bond, curve_t0, base.t1_date, base.remaining_tenor, base.p_roll, base.p0, cfg=cfg
    )
    curve_level_pct = s_level * level_bp
    curve_shape_pct = s_slope * slope_bp + s_curv * curvature_bp
    residual_pct = base.total_pct - (
        base.carry_pct + base.roll_pct + curve_level_pct + curve_shape_pct
    )
    return {
        "carry": base.carry_pct,
        "roll": base.roll_pct,
        "curve_level": curve_level_pct,
        "curve_shape": curve_shape_pct,
        "residual": residual_pct,
        "total": base.total_pct,
        "level_bp": level_bp,
        "slope_bp": slope_bp,
        "curvature_bp": curvature_bp,
        "factor_fit_residual_bp": rate_residual_bp,
        "funding_pct": base.funding_pct,
        "y_tenor_pct": base.y_tenor_pct,
    }
