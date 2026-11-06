"""Carry / roll-down 分解（**只做分解，不做信号与仓位**）。

本模块把「持有债券一段时间的收益」拆成可解释的几块，供后续回测归因使用；
本轮需求明确：在 KRD 与 10Y 曲线偏差的持续性查清之前**不产生任何交易信号或仓位**。

两个函数
--------
- :func:`carry_roll_down`：**事前**（ex-ante）估算，给定一条 YTM 曲线与持有期，
  返回 carry、roll-down 及其年化值。滚动目标锁定 **T − roll_years**（10Y → 9Y），
  **不是** 7Y；用 7Y 会把 roll 高估约 0.5–0.9pct。
- :func:`decompose_realized`：**事后**（ex-post）归因，给定 t0/t1 两条曲线，
  用同一只合成债重估，拆成 carry / roll / curve_level / curve_shape / residual，
  并保证 ``carry + roll + curve_level + curve_shape + residual == total``
  （``total`` 是全价口径、含应计与实际票息、扣融资成本的实际持有期总收益）。

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
from datetime import date, timedelta

import numpy as np

from ficc.config import Config, get_config
from ficc.curve.curve import Curve
from ficc.data.schema import BondStatic
from ficc.pricing.bond import cashflows, dirty_price_from_ytm
from ficc.pricing.conventions import day_count, pricing_config

__all__ = [
    "FundingRateUnavailableError",
    "carry_roll_down",
    "decompose_realized",
    "par_modified_duration",
    "resolve_funding_rate_pct",
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

    公式
    ----
    - ``carry_pct = (y_τ − funding) × h/365``（票息收入减融资成本）；
    - ``roll_pct  = −[y(τ−roll_years) − y(τ)] × D_mod × h/365``
      （持有期滚动使收益率沿曲线下滑，价格获得资本利得/损失）；
    - ``total_pct = carry_pct + roll_pct``；年化 = 除 ``h/365``。

    **滚动目标是 T − roll_years（10Y → 9Y），不是 7Y**（memo §6.2：用 7Y 会把
    骑乘收益高估约 0.9pct）。``roll_years`` 默认读
    ``cfg.backtest.roll_down_step_years``（1.0）。

    ``D_mod`` 默认由曲线解析算：合成平价债在 **τ−roll_years**（如 9Y）处的
    全价修正久期（经中心差分，见 :func:`par_modified_duration`），不是硬编码、
    也不是净价口径。

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
        滚动年限，默认 ``cfg.backtest.roll_down_step_years``。
    mod_duration : float | None
        显式指定修正久期（年）；``None`` 时按 τ−roll_years 的合成平价债解析算。
    cfg : Config | None

    Returns
    -------
    dict[str, float]
        至少含：``carry_pct`` / ``roll_pct`` / ``total_pct`` /
        ``carry_annual_pct`` / ``roll_annual_pct`` / ``total_annual_pct`` /
        ``y_tenor_pct`` / ``roll_target_tenor`` / ``roll_target_y_pct`` /
        ``funding_pct``；另附 ``mod_duration`` / ``horizon_years``。
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
    carry_pct = (y_tenor - funding_pct) * horizon_years
    roll_pct = -(y_target - y_tenor) * duration * horizon_years
    total_pct = carry_pct + roll_pct
    if horizon_days > 0:
        scale = 365.0 / horizon_days
        carry_annual, roll_annual, total_annual = (
            carry_pct * scale,
            roll_pct * scale,
            total_pct * scale,
        )
    else:
        carry_annual, roll_annual, total_annual = 0.0, 0.0, 0.0

    return {
        "carry_pct": carry_pct,
        "roll_pct": roll_pct,
        "total_pct": total_pct,
        "carry_annual_pct": carry_annual,
        "roll_annual_pct": roll_annual,
        "total_annual_pct": total_annual,
        "y_tenor_pct": y_tenor,
        "roll_target_tenor": target_tenor,
        "roll_target_y_pct": y_target,
        "funding_pct": funding_pct,
        "mod_duration": duration,
        "horizon_years": horizon_years,
    }


# ---------------------------------------------------------------------------
# 事后分解
# ---------------------------------------------------------------------------
def _factor_changes(curve_t0: Curve, curve_t1: Curve) -> tuple[float, float, float, float]:
    """把可观测曲线变化投影到 level / slope / curvature 三因子（单位 bp）。

    因子基由 ``Curve.shock`` 的三种形状定义（与曲线层 R10 语义一致），在
    ``curve_t0.tenors`` 节点上取值：

    - level：常数 1（平行移动）；
    - slope：``curve.shock("steepen", 1)`` 的节点变化（长端上、短端下，幅度 1bp）；
    - curvature：``curve.shock("butterfly", 1)`` 的节点变化（腹部上、两端下，幅度 1bp）。

    对实际节点变化 ``Δy`` 做**线性最小二乘**得到三因子幅度
    ``(level_bp, slope_bp, curvature_bp)``；返回的第 4 项是因子基无法解释的
    节点残差的均方根（bp），它进入价格残差而不是被丢掉。

    用 NS 基函数作因子基的原因：``Curve.shock`` 的四种形状都落在
    ``{1, L1, L2}`` 张成的空间里（见 ``ficc.curve.curve`` 的 R10 约定），
    因此对纯平移/陡平/蝶式冲击，本分解的一阶项与重估一致。
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


def _factor_bump_sensitivities(
    bond: BondStatic,
    curve_t0: Curve,
    settlement_t1: date,
    remaining_tenor: float,
    p_roll: float,
    p0: float,
    *,
    cfg: Config | None = None,
) -> tuple[float, float, float]:
    """因子冲击的价格敏感度（%收益 / 因子 bp），由曲线 ``shift`` / ``shock`` 重估。

    三个因子与 :func:`_factor_changes` 的基一一对应（冲击 1bp 的因子幅度 = 1 单位）：

    - level：``curve.shift(1)``（整条曲线 +1bp）；
    - slope：``curve.shock("steepen", 1)``（幅度 1bp 的变陡形状）；
    - curvature：``curve.shock("butterfly", 1)``（幅度 1bp 的蝶形）。

    返回 ``(s_level, s_slope, s_curv)``，单位：%收益/bp（均以 t0 全价 ``p0``
    为分母，多头、收益率上行 → 负值）。这不是硬编码久期，而是对曲线冲击的
    中心重估（每次 ±1bp 用 ``shock`` 的幅度语义）。
    """
    for attr in ("shift", "shock"):
        if not hasattr(curve_t0, attr):
            raise TypeError(
                f"curve_t0 缺少必需成员 {attr!r}：decompose_realized 需要 "
                "ficc.curve.Curve 的 shift(bp) 与 shock(kind,bp) 来算因子敏感度"
            )

    def repriced(curve_bumped: Curve) -> float:
        y = float(curve_bumped.rate(remaining_tenor))
        return dirty_price_from_ytm(bond, settlement_t1, y, cfg=cfg)

    s_level = (repriced(curve_t0.shift(1.0)) - p_roll) / p0 * 100.0
    s_slope = (repriced(curve_t0.shock("steepen", 1.0)) - p_roll) / p0 * 100.0
    s_curv = (repriced(curve_t0.shock("butterfly", 1.0)) - p_roll) / p0 * 100.0
    return s_level, s_slope, s_curv


def decompose_realized(
    tenor: float,
    curve_t0: Curve,
    curve_t1: Curve,
    *,
    horizon_days: int,
    funding_rate_pct: float | None = None,
    cfg: Config | None = None,
) -> dict[str, float]:
    """事后收益分解：carry / roll / curve_level / curve_shape / residual / total。

    口径
    ----
    用同一只**合成平价债**（t0 曲线在 ``tenor`` 的收益率作票面，起息 t0、
    到期 t0+tenor 年）在 t0/t1 两条曲线上重估：

    - ``total_pct``：全价口径实际持有期总收益（含持有期内实际收到的票息、扣
      融资成本），以 t0 全价的百分比表示；
    - ``carry_pct``：票息应计 − 融资成本；
    - ``roll_pct``：曲线**不动**时沿曲线下滑（残余期限 τ−h → 用 t0 曲线重估）
      带来的价格变化（剔除票息应计）；
    - ``curve_level_pct``：可观测 level 因子变化 × 久期近似；
    - ``curve_shape_pct``：slope + curvature 因子变化 × 对应敏感度；
    - ``residual_pct``：平衡项 = total − (carry+roll+level+shape)，即一阶因子
      近似的误差（凸性、交叉项、冲击形状与 NS 基不匹配等），**不是数据缺失**。

    恒等式：``carry + roll + curve_level + curve_shape + residual == total``
    （浮点误差级），测试用 <1bp 断言；residual 本身在温和冲击下应为几个 bp 内。

    Parameters
    ----------
    tenor : float
        t0 时的期限（年）。
    curve_t0, curve_t1 : Curve
        t0 / t1 的 YTM 曲线（各自带 ``as_of``）。
    horizon_days : int
        持有期（自然日），必须与 ``curve_t0.as_of → curve_t1.as_of`` 的实际天数一致。
    funding_rate_pct : float | None
        融资利率（百分数），规则同 :func:`carry_roll_down`。
    cfg : Config | None

    Returns
    -------
    dict[str, float]
        ``carry / roll / curve_level / curve_shape / residual / total``，
        另附 ``level_bp / slope_bp / curvature_bp / funding_pct / y_tenor_pct``。
    """
    _require_curve(curve_t0, name="curve_t0")
    _require_curve(curve_t1, name="curve_t1")
    tenor_f = float(tenor)
    if not np.isfinite(tenor_f) or tenor_f <= 1.0:
        raise ValueError(f"tenor 必须 >1Y（1Y 及以下无期间票息），实际 {tenor!r}")
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
    funding_pct = resolve_funding_rate_pct(funding_rate_pct, cfg=cfg)

    bond = _synthetic_par_bond(curve_t0, tenor_f, cfg=cfg)
    y_tenor = float(curve_t0.rate(tenor_f))
    p0 = dirty_price_from_ytm(bond, t0_date, y_tenor, cfg=cfg)

    # 持有期内实际收到的票息（不含本金；tenor 远大于持有期）
    frame = cashflows(bond, t0_date, cfg=cfg)
    coupon_paid = float(
        sum(
            amount
            for cf_date, amount in zip(frame["date"], frame["amount"], strict=True)
            if t0_date < cf_date <= t1_date
        )
    )

    maturity = bond.maturity_date
    # 残余期限用「tenor − h/365」而不是日历天数/365：分解必须在 t0 的期限网格上
    # 连续（h=0 时剩余期限必须精确回到 tenor，否则闰年天数会让 total 出现假残差）。
    remaining_tenor = tenor_f - horizon_years
    if remaining_tenor <= 0:
        raise ValueError(
            f"horizon_days={horizon_days} 超过 tenor={tenor_f}，残余期限 {remaining_tenor:.4f} 年"
        )
    if t1_date >= maturity:
        raise ValueError(f"结算日 {t1_date} 已不早于合成债到期日 {maturity}")
    p1 = dirty_price_from_ytm(bond, t1_date, float(curve_t1.rate(remaining_tenor)), cfg=cfg)
    y_roll = float(curve_t0.rate(remaining_tenor))
    p_roll = dirty_price_from_ytm(bond, t1_date, y_roll, cfg=cfg)

    # 票息应计（每 100 面值年票息 = coupon_pct 元）
    accrued_income = float(bond.coupon_pct) * horizon_years
    funding_frac = funding_pct / 100.0 * horizon_years
    pct = 100.0 / p0

    carry_pct = (accrued_income - funding_frac * p0) * pct
    roll_pct = ((p_roll - p0) + coupon_paid - accrued_income) * pct
    total_pct = ((p1 - p0) + coupon_paid) * pct - funding_frac * 100.0

    level_bp, slope_bp, curvature_bp, rate_residual_bp = _factor_changes(curve_t0, curve_t1)
    s_level, s_slope, s_curv = _factor_bump_sensitivities(
        bond, curve_t0, t1_date, remaining_tenor, p_roll, p0, cfg=cfg
    )
    curve_level_pct = s_level * level_bp
    curve_shape_pct = s_slope * slope_bp + s_curv * curvature_bp
    residual_pct = total_pct - (carry_pct + roll_pct + curve_level_pct + curve_shape_pct)

    return {
        "carry": carry_pct,
        "roll": roll_pct,
        "curve_level": curve_level_pct,
        "curve_shape": curve_shape_pct,
        "residual": residual_pct,
        "total": total_pct,
        "level_bp": level_bp,
        "slope_bp": slope_bp,
        "curvature_bp": curvature_bp,
        "factor_fit_residual_bp": rate_residual_bp,
        "funding_pct": funding_pct,
        "y_tenor_pct": y_tenor,
    }
