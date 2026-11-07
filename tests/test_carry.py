"""carry / roll-down 与事后分解测试（R14/R18）。

口径：收益率百分数；修正久期用全价、由合成平价债解析算；滚动目标锁定 T−1Y
（10Y→9Y，**不是 7Y**）。融资利率缺失必须报错（默认 0 成本是错的）。

R18：``roll_annual = −[y(τ−r)−y(τ)]·D/r`` 是**弦长年化代理**（r=``roll_years``），
``roll = roll_annual·h/365``；严格滚降用 ``roll_down_aging``（原券老化，条款不变）。
R14：``decompose_realized`` 用**冻结的可观测因子基**（level_all / slope /
curvature）做最小二乘投影；NS 情景投影单列为 ``ns_scenario_projection``。

曲线用 ``ficc.curve.Curve`` 合成，测试不依赖真实数据。
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pytest

from ficc.config import Config, get_config
from ficc.curve.curve import Curve
from ficc.data.schema import BondStatic
from ficc.pricing.carry import (
    FACTOR_TENORS,
    FundingRateUnavailableError,
    carry_roll_down,
    decompose_realized,
    factor_basis_value,
    ns_scenario_projection,
    par_modified_duration,
    resolve_funding_rate_pct,
    roll_down_aging,
)

TENORS = np.array([1.0, 2.0, 3.0, 5.0, 7.0, 10.0, 15.0, 20.0, 30.0])
AS_OF = date(2026, 11, 4)
T1_AS_OF = AS_OF + timedelta(days=365)


# ---------------------------------------------------------------------------
# 合成曲线与手算工具
# ---------------------------------------------------------------------------
def make_curve(rates: np.ndarray, *, as_of: date = AS_OF, tenors: np.ndarray = TENORS) -> Curve:
    return Curve(
        as_of=as_of, tenors=np.asarray(tenors, dtype=float), rates=np.asarray(rates, float)
    )


def flat_curve(rate_pct: float = 3.0, *, as_of: date = AS_OF) -> Curve:
    return make_curve(np.full_like(TENORS, rate_pct), as_of=as_of)


def steep_curve(base_pct: float = 1.5, slope_bp_per_year: float = 5.0) -> Curve:
    """向上倾斜曲线：y(t) = base + (slope_bp/100)·t（每年 +5bp）。"""
    return make_curve(base_pct + slope_bp_per_year / 100.0 * TENORS)


def curved_curve() -> Curve:
    """先陡后平的弯曲曲线（用于老化 vs 弦长代理对比）：2.0 + 0.02t − 0.0008t²。"""
    return make_curve(2.0 + 0.02 * TENORS - 0.0008 * TENORS**2)


def make_bond(
    *, code: str, maturity: date, coupon_pct: float, freq: int = 1, issue: date = AS_OF
) -> BondStatic:
    return BondStatic(
        code=code, issue_date=issue, maturity_date=maturity, coupon_pct=coupon_pct, freq=freq
    )


def cfg_with_costs(**updates: object) -> Config:
    """复制全局配置并覆盖 costs 下的字段。"""
    cfg = get_config()
    return cfg.model_copy(update={"costs": cfg.costs.model_copy(update=updates)})


def par_price_and_mod_duration(
    par_yield_pct: float, years: float, freq: int
) -> tuple[float, float]:
    """独立手算：平价债全价与修正久期（年付/半年付，整数年）。"""
    n_periods = round(years * freq)
    coupon = par_yield_pct / freq  # 元/百元面值/期（年票息 = coupon_pct 元）

    def price(y_pct: float) -> float:
        v = 1.0 / (1.0 + y_pct / 100.0 / freq)
        return sum(coupon * v**i for i in range(1, n_periods + 1)) + 100.0 * v**n_periods

    p0 = price(par_yield_pct)
    p_up = price(par_yield_pct + 0.01)  # +1bp
    p_dn = price(par_yield_pct - 0.01)  # -1bp
    return p0, -(p_up - p_dn) / (2.0 * 1e-4 * p0)


# ---------------------------------------------------------------------------
# 事前：carry / roll-down（弦长代理，R18）
# ---------------------------------------------------------------------------
def test_flat_curve_carry_hand_calc() -> None:
    """平坦曲线 3%、融资 2%、持有 1 年 → carry = 1.0pct、roll = 0。"""
    result = carry_roll_down(10.0, flat_curve(3.0), horizon_days=365, funding_rate_pct=2.0)
    assert result["carry_pct"] == pytest.approx(1.0, abs=1e-12)
    assert result["carry_annual_pct"] == pytest.approx(1.0, abs=1e-12)
    assert result["roll_pct"] == pytest.approx(0.0, abs=1e-12)
    assert result["total_pct"] == pytest.approx(1.0, abs=1e-12)
    assert result["y_tenor_pct"] == pytest.approx(3.0, abs=1e-12)
    assert result["funding_pct"] == pytest.approx(2.0, abs=1e-12)


def test_roll_target_locked_to_t_minus_1y() -> None:
    """滚动目标锁定 T−1Y：10Y→9Y；用 7Y 会显著高估（memo §6.2）。"""
    curve = steep_curve()
    result = carry_roll_down(10.0, curve, horizon_days=365, funding_rate_pct=2.0)

    y_10 = float(curve.rate(10.0))
    y_9 = float(curve.rate(9.0))
    y_7 = float(curve.rate(7.0))
    assert result["roll_target_tenor"] == pytest.approx(9.0, abs=1e-12)
    assert result["roll_target_y_pct"] == pytest.approx(y_9, abs=1e-12)

    # 9Y 平价债（10Y 券一年后，仍是半年付）的修正久期
    p0, duration_9y = par_price_and_mod_duration(y_9, years=9.0, freq=2)
    assert p0 == pytest.approx(100.0, abs=1e-9)  # 平价
    assert result["mod_duration"] == pytest.approx(duration_9y, rel=1e-10)
    expected_annual = -(y_9 - y_10) * duration_9y  # r=1Y 时弦长年化 = 该式
    assert result["roll_annual_pct"] == pytest.approx(expected_annual, rel=1e-10)
    assert result["roll_pct"] == pytest.approx(expected_annual, rel=1e-10)
    assert expected_annual > 0.0  # 向上倾斜曲线持有一年 → 正 roll

    # 错误地滚到 7Y（memo 警告的经典错误）会高估多少
    _p7, duration_7y = par_price_and_mod_duration(y_7, years=7.0, freq=1)
    wrong_roll = -(y_7 - y_10) * duration_7y
    assert wrong_roll - expected_annual > 0.5  # memo：约 0.5–0.9pct 的高估


def test_par_modified_duration_matches_closed_form() -> None:
    """曲线解析算出的 9Y 平价债修正久期与独立手算一致。"""
    curve = steep_curve()
    y_9 = float(curve.rate(9.0))
    _p0, duration_manual = par_price_and_mod_duration(y_9, years=9.0, freq=2)
    assert par_modified_duration(curve, 9.0) == pytest.approx(duration_manual, rel=1e-10)


def test_roll_annual_is_chord_annualized_not_halved() -> None:
    """R18 反例回归：线性 5bp/年、固定 D=5、h=365 时 r=0.5 与 r=1.0 的年化滚降相等。

    旧实现漏了 ``/r``：r=0.5 会得到 0.125%（把年化滚降减半）；修复后两者都应为
    0.25%。用显式 ``mod_duration=5.0`` 隔离久期随目标期限的变化。
    """
    curve = steep_curve()
    results = {
        r: carry_roll_down(
            10.0,
            curve,
            horizon_days=365,
            funding_rate_pct=2.0,
            roll_years=r,
            mod_duration=5.0,
        )
        for r in (0.5, 1.0)
    }
    for r, result in results.items():
        assert result["roll_annual_pct"] == pytest.approx(0.25, abs=1e-12), r
        assert result["roll_pct"] == pytest.approx(0.25, abs=1e-12), r
        assert result["roll_target_tenor"] == pytest.approx(10.0 - r, abs=1e-12)
    assert results[0.5]["roll_annual_pct"] == pytest.approx(
        results[1.0]["roll_annual_pct"], rel=1e-12
    )


def test_default_duration_chord_consistency() -> None:
    """默认久期按目标期限算（9.5Y vs 9Y），年化弦长近似相差 <5%。"""
    curve = steep_curve()
    annual_half = carry_roll_down(
        10.0, curve, horizon_days=365, funding_rate_pct=2.0, roll_years=0.5
    )["roll_annual_pct"]
    annual_one = carry_roll_down(
        10.0, curve, horizon_days=365, funding_rate_pct=2.0, roll_years=1.0
    )["roll_annual_pct"]
    assert annual_half == pytest.approx(annual_one, rel=0.05)
    assert annual_half > annual_one  # 9.5Y 久期 > 9Y 久期（正斜率+正凸性下）


def test_roll_scales_linearly_with_horizon() -> None:
    curve = steep_curve()
    full = carry_roll_down(10.0, curve, horizon_days=365, funding_rate_pct=2.0)
    partial = carry_roll_down(10.0, curve, horizon_days=73, funding_rate_pct=2.0)
    assert partial["roll_pct"] == pytest.approx(full["roll_pct"] * 73 / 365, rel=1e-12)
    assert partial["carry_pct"] == pytest.approx(full["carry_pct"] * 73 / 365, rel=1e-12)
    assert partial["roll_annual_pct"] == pytest.approx(full["roll_annual_pct"], rel=1e-12)


def test_horizon_zero_gives_zero_carry_and_roll() -> None:
    result = carry_roll_down(10.0, steep_curve(), horizon_days=0, funding_rate_pct=2.0)
    for key in ("carry_pct", "roll_pct", "total_pct"):
        assert result[key] == pytest.approx(0.0, abs=1e-15)
    for key in ("carry_annual_pct", "roll_annual_pct", "total_annual_pct"):
        assert result[key] == pytest.approx(0.0, abs=1e-15)
    assert result["mod_duration"] > 0.0  # 久期仍给出，便于检查


def test_mod_duration_override() -> None:
    curve = steep_curve()
    result = carry_roll_down(10.0, curve, horizon_days=365, funding_rate_pct=2.0, mod_duration=5.0)
    assert result["mod_duration"] == pytest.approx(5.0)
    assert result["roll_pct"] == pytest.approx(
        -(float(curve.rate(9.0)) - float(curve.rate(10.0))) * 5.0
    )


def test_funding_rate_missing_raises() -> None:
    """融资利率缺失时必须报错：定价层没有回购序列，默认 0 成本会把 carry 高估。"""
    with pytest.raises(FundingRateUnavailableError, match="不能默认 0 成本"):
        carry_roll_down(10.0, flat_curve(3.0), horizon_days=365)  # 默认 cfg 是 FDR007
    with pytest.raises(FundingRateUnavailableError, match="不能默认 0 成本"):
        resolve_funding_rate_pct(None, cfg=get_config())


def test_funding_rate_from_const_config() -> None:
    """只有显式把 repo_rate_source 设为 'const' 才使用 fallback 常量（1.318%）。"""
    cfg = cfg_with_costs(repo_rate_source="const")
    funding = resolve_funding_rate_pct(None, cfg=cfg)
    assert funding == pytest.approx(1.318, rel=1e-12)
    result = carry_roll_down(10.0, flat_curve(3.0), horizon_days=365, cfg=cfg)
    assert result["funding_pct"] == pytest.approx(1.318, rel=1e-12)
    assert result["carry_pct"] == pytest.approx(3.0 - 1.318, rel=1e-12)


def test_explicit_funding_overrides_config() -> None:
    assert resolve_funding_rate_pct(2.5, cfg=cfg_with_costs(repo_rate_source="const")) == 2.5


def test_carry_roll_down_validates_inputs() -> None:
    curve = steep_curve()
    with pytest.raises(ValueError, match="1Y 及以下"):
        carry_roll_down(1.0, curve, horizon_days=365, funding_rate_pct=2.0)
    with pytest.raises(ValueError, match="roll_years"):
        carry_roll_down(10.0, curve, horizon_days=365, funding_rate_pct=2.0, roll_years=12.0)
    with pytest.raises(ValueError, match="horizon_days"):
        carry_roll_down(10.0, curve, horizon_days=-1, funding_rate_pct=2.0)
    with pytest.raises(TypeError, match="缺少必需成员"):
        carry_roll_down(10.0, object(), horizon_days=365, funding_rate_pct=2.0)  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# R18：原券老化重估（条款不变）
# ---------------------------------------------------------------------------
def test_roll_down_aging_2y_to_1y_keeps_annual_frequency() -> None:
    """2Y→1Y：原券（年付）老化一年，剩余期限缩短但频率不变；不被拒。"""
    curve = steep_curve()
    bond = make_bond(code="SYN-2Y", maturity=date(2028, 11, 4), coupon_pct=2.0, freq=1)
    result = roll_down_aging(bond, AS_OF, curve, roll_years=1.0)

    assert result["coupon_freq"] == pytest.approx(1.0)  # 年付不变
    assert result["initial_tenor_years"] == pytest.approx(2.00274, abs=1e-5)
    assert result["aged_tenor_years"] == pytest.approx(1.00274, abs=1e-5)
    assert result["aged_tenor_years"] == pytest.approx(
        result["initial_tenor_years"] - 1.0, abs=1e-9
    )
    assert result["y_aged_pct"] < result["y_initial_pct"]  # 向上曲线 → 老化后收益率更低
    assert result["coupon_paid"] == pytest.approx(2.0, rel=1e-12)  # 中途收到一次票息
    assert result["roll_pct"] == pytest.approx(-0.335318, abs=1e-5)


def test_roll_down_aging_8y_and_15y_keep_frequency() -> None:
    """8Y（年付）→7Y 仍年付；15Y（半年付）→14Y 仍半年付。"""
    curve = steep_curve()
    bond_8y = make_bond(code="SYN-8Y", maturity=date(2034, 11, 4), coupon_pct=2.2, freq=1)
    aging_8y = roll_down_aging(bond_8y, AS_OF, curve, roll_years=1.0)
    assert aging_8y["coupon_freq"] == pytest.approx(1.0)
    assert aging_8y["initial_tenor_years"] == pytest.approx(8.005479, abs=1e-5)
    assert aging_8y["aged_tenor_years"] == pytest.approx(7.005479, abs=1e-5)
    assert aging_8y["roll_pct"] == pytest.approx(0.069862, abs=1e-5)

    bond_15y = make_bond(code="SYN-15Y", maturity=date(2041, 11, 4), coupon_pct=2.6, freq=2)
    aging_15y = roll_down_aging(bond_15y, AS_OF, curve, roll_years=1.0)
    assert aging_15y["coupon_freq"] == pytest.approx(2.0)  # 半年付不变
    assert aging_15y["initial_tenor_years"] == pytest.approx(15.010959, abs=1e-5)
    assert aging_15y["aged_tenor_years"] == pytest.approx(14.010959, abs=1e-5)
    assert aging_15y["roll_pct"] == pytest.approx(0.347299, abs=1e-5)


def test_roll_down_aging_validates() -> None:
    curve = steep_curve()
    bond = make_bond(code="SYN-2Y", maturity=date(2028, 11, 4), coupon_pct=2.0, freq=1)
    with pytest.raises(ValueError, match="必须为正数"):
        roll_down_aging(bond, AS_OF, curve, roll_years=0.0)
    with pytest.raises(ValueError, match="已到期"):
        roll_down_aging(bond, AS_OF, curve, roll_years=3.0)
    with pytest.raises(ValueError, match="结算日"):
        roll_down_aging(bond, date(2025, 1, 1), curve, roll_years=1.0)


def test_aging_vs_chord_proxy_on_curved_curve() -> None:
    """弯曲曲线 + 溢价券：原券老化的滚降与弦长代理不同（pull-to-par 效应）。

    弦长代理（carry_roll_down）只乘久期，忽略溢价债券向面值收敛；老化重估含该效应。
    """
    curve = curved_curve()
    y10 = float(curve.rate(10.0))
    premium = make_bond(code="SYN-10Y-PREM", maturity=date(2036, 11, 4), coupon_pct=2.6, freq=2)
    aging = roll_down_aging(premium, AS_OF, curve, roll_years=1.0)
    chord = carry_roll_down(10.0, curve, horizon_days=365, funding_rate_pct=2.0, roll_years=1.0)
    assert aging["p0_dirty"] == pytest.approx(104.3049, abs=1e-4)  # 溢价券
    assert aging["roll_annual_pct"] == pytest.approx(-0.323944, abs=1e-5)
    assert chord["roll_annual_pct"] == pytest.approx(0.052203, abs=1e-5)
    difference = aging["roll_annual_pct"] - chord["roll_annual_pct"]
    assert difference == pytest.approx(-0.376148, abs=1e-4)  # 量级 ~0.38pct
    assert abs(difference) > 0.2

    # 平价券上两者接近（曲率效应小），差异 <1bp
    par = make_bond(code="SYN-10Y-PAR", maturity=date(2036, 11, 4), coupon_pct=y10, freq=2)
    aging_par = roll_down_aging(par, AS_OF, curve, roll_years=1.0)
    assert abs(aging_par["roll_annual_pct"] - chord["roll_annual_pct"]) < 0.005


# ---------------------------------------------------------------------------
# 事后分解：可观测因子归因（R14）
# ---------------------------------------------------------------------------
def shocked_curve(base: Curve, kind: str, bp: float) -> Curve:
    return make_curve(base.shock(kind, bp).rates, as_of=T1_AS_OF)  # type: ignore[arg-type]


def test_decompose_identity_closed_loop() -> None:
    """平移/陡峭/蝶式冲击后：五项因子+carry+roll+residual = total（<1bp）。"""
    base = steep_curve()
    scenarios = {
        "+25bp level": make_curve(base.rates + 0.25, as_of=T1_AS_OF),
        "-25bp level": make_curve(base.rates - 0.25, as_of=T1_AS_OF),
        "steepen 25bp": shocked_curve(base, "steepen", 25.0),
        "butterfly 25bp": shocked_curve(base, "butterfly", 25.0),
    }
    for name, curve_t1 in scenarios.items():
        result = decompose_realized(10.0, base, curve_t1, horizon_days=365, funding_rate_pct=2.0)
        decomposed = (
            result["carry"]
            + result["roll"]
            + result["curve_level"]
            + result["curve_slope"]
            + result["curve_curvature"]
            + result["residual"]
        )
        assert decomposed == pytest.approx(result["total"], abs=1e-10), name


def test_decompose_no_curve_change() -> None:
    """曲线不变（仅时间前进 1 年）：carry=0（票息=融资 2%），收益全部来自 roll。"""
    base = steep_curve()
    curve_t1 = make_curve(base.rates, as_of=T1_AS_OF)
    result = decompose_realized(10.0, base, curve_t1, horizon_days=365, funding_rate_pct=2.0)

    assert result["carry"] == pytest.approx(0.0, abs=1e-12)  # y_10 = 2% = 融资
    assert result["curve_level"] == pytest.approx(0.0, abs=1e-12)
    assert result["curve_slope"] == pytest.approx(0.0, abs=1e-12)
    assert result["curve_curvature"] == pytest.approx(0.0, abs=1e-12)
    assert result["residual"] == pytest.approx(0.0, abs=1e-12)
    assert result["roll"] > 0.0
    assert result["roll"] == pytest.approx(result["total"], rel=1e-12)


def test_decompose_level_shock_uses_observable_level() -> None:
    """平行上移 25bp：level_all 系数 = 25bp，curve_level 显著为负且残差≈0。"""
    base = steep_curve()
    curve_t1 = make_curve(base.rates + 0.25, as_of=T1_AS_OF)
    result = decompose_realized(10.0, base, curve_t1, horizon_days=365, funding_rate_pct=2.0)

    assert result["level_all_bp"] == pytest.approx(25.0, abs=1e-9)
    assert result["slope_bp"] == pytest.approx(0.0, abs=1e-9)
    assert result["curvature_bp"] == pytest.approx(0.0, abs=1e-9)
    assert result["curve_level"] == pytest.approx(-2.0358, abs=0.02)
    assert result["curve_slope"] == pytest.approx(0.0, abs=1e-9)
    assert result["curve_curvature"] == pytest.approx(0.0, abs=1e-9)
    assert result["total"] < 0.0
    assert abs(result["residual"]) < 1e-6  # 纯平行冲击可被 level 基精确表示


def test_decompose_r14_counterexample_level_all_is_zero() -> None:
    """R14 反例：Δy = 0.01·(tenor−mean) 时全期限均值变化为 0。

    可观测 level_all 必须 ≈0（|level|<0.5bp）；可观测 slope = 8bp（10Y−2Y）。
    旧 NS 投影会给出 level=1.328862bp、slope=20.878240bp、curvature=−10.226884bp
    ——这些数字只能出现在 ``ns_scenario_projection``，不得冒充可观测归因。
    """
    ten = np.asarray(FACTOR_TENORS, dtype=float)
    base_rates = 1.5 + 0.05 * ten
    base = make_curve(base_rates, tenors=ten)
    dy = 0.01 * (ten - ten.mean())
    curve_t1 = make_curve(base_rates + dy, as_of=T1_AS_OF, tenors=ten)

    result = decompose_realized(10.0, base, curve_t1, horizon_days=365, funding_rate_pct=2.0)
    assert abs(result["level_all_bp"]) < 0.5  # 实测 0.0bp
    assert result["slope_bp"] == pytest.approx(8.0, abs=1e-9)  # 10Y−2Y
    assert result["curvature_bp"] == pytest.approx(-2.0, abs=1e-9)  # 2×5Y−2Y−10Y
    assert abs(result["curve_level"]) < 0.01  # level 的价格贡献也≈0

    # NS 情景投影（另一套口径）：复现评审的 1.33/20.88/−10.23bp，仅供披露对照
    ns = ns_scenario_projection(10.0, base, curve_t1, horizon_days=365, funding_rate_pct=2.0)
    assert ns["level_bp"] == pytest.approx(1.328862, abs=1e-5)
    assert ns["slope_bp"] == pytest.approx(20.878240, abs=1e-5)
    assert ns["curvature_bp"] == pytest.approx(-10.226884, abs=1e-5)
    # 可观测 slope 不得出现 NS 的 20.88bp 量级
    assert abs(result["slope_bp"] - ns["slope_bp"]) > 10.0


def test_decompose_shape_terms_signs() -> None:
    """陡峭/蝶式冲击：可观测因子项符号与量级合理，NS 投影幅度单独可查。"""
    base = steep_curve()

    steepened = decompose_realized(
        10.0, base, shocked_curve(base, "steepen", 25.0), horizon_days=365, funding_rate_pct=2.0
    )
    assert steepened["slope_bp"] > 0.0
    assert steepened["curve_slope"] < -0.3  # 长端上行 → 10Y 券价格下跌
    assert abs(steepened["residual"]) < 0.2  # 基向量张成空间外的冲击部分

    ns = ns_scenario_projection(
        10.0, base, shocked_curve(base, "steepen", 25.0), horizon_days=365, funding_rate_pct=2.0
    )
    assert ns["slope_bp"] == pytest.approx(25.0, abs=0.1)  # NS 形状幅度

    butterfly = decompose_realized(
        10.0,
        base,
        shocked_curve(base, "butterfly", 25.0),
        horizon_days=365,
        funding_rate_pct=2.0,
    )
    assert butterfly["curvature_bp"] > 0.0
    assert butterfly["curve_curvature"] > 0.0
    assert abs(butterfly["residual"]) < 0.5


def test_decompose_with_fixed_bond_vs_daily_par_rebuild() -> None:
    """``bond=`` 固定券重估 vs 每日新建平价券：两日持有差异 <1bp，且剩余期限自然缩短。"""
    base = steep_curve()
    two_days = AS_OF + timedelta(days=2)
    curve_t1 = make_curve(base.rates + 0.02, as_of=two_days)  # +2bp level
    fixed10 = make_bond(code="SYN-FIX10", maturity=date(2036, 11, 4), coupon_pct=2.0, freq=2)

    synthetic = decompose_realized(10.0, base, curve_t1, horizon_days=2, funding_rate_pct=2.0)
    held = decompose_realized(
        10.0, base, curve_t1, horizon_days=2, funding_rate_pct=2.0, bond=fixed10
    )
    for key in ("carry", "roll", "curve_level", "curve_slope", "curve_curvature", "total"):
        assert held[key] == pytest.approx(synthetic[key], abs=0.01)  # 1bp 内
    # 固定券的 t0 期限是债券剩余期限（10.0082Y），不是参数给出的名义 10Y
    assert held["y_tenor_pct"] != pytest.approx(synthetic["y_tenor_pct"], abs=1e-6)
    assert held["level_all_bp"] == pytest.approx(synthetic["level_all_bp"], abs=1e-9)


def test_decompose_horizon_zero() -> None:
    base = steep_curve()
    same_day = make_curve(base.rates, as_of=AS_OF)
    result = decompose_realized(10.0, base, same_day, horizon_days=0, funding_rate_pct=2.0)
    for key in (
        "carry",
        "roll",
        "curve_level",
        "curve_slope",
        "curve_curvature",
        "residual",
        "total",
    ):
        assert result[key] == pytest.approx(0.0, abs=1e-12)


def test_decompose_validates_dates_and_funding() -> None:
    base = steep_curve()
    wrong_date = make_curve(base.rates, as_of=AS_OF + timedelta(days=300))
    with pytest.raises(ValueError, match="不一致"):
        decompose_realized(10.0, base, wrong_date, horizon_days=365, funding_rate_pct=2.0)
    with pytest.raises(FundingRateUnavailableError):
        decompose_realized(10.0, base, make_curve(base.rates, as_of=T1_AS_OF), horizon_days=365)
    with pytest.raises(ValueError, match="1Y 及以下"):
        decompose_realized(
            1.0,
            base,
            make_curve(base.rates, as_of=T1_AS_OF),
            horizon_days=365,
            funding_rate_pct=2.0,
        )


def test_observable_factor_contract_matches_curve_layer() -> None:
    """R14：定价层的冻结因子定义必须与曲线线 ``FACTOR_COLUMNS`` 契约一致。"""
    curve_factors = pytest.importorskip("ficc.curve.factors")
    assert tuple(curve_factors.FACTOR_COLUMNS) == (
        "level_all",
        "level_long",
        "slope",
        "curvature",
    )
    assert tuple(float(t) for t in curve_factors.LEVEL_ALL_TENORS) == FACTOR_TENORS


def test_factor_basis_vectors_are_frozen_r14() -> None:
    """可观测因子基向量冻结：level_all 恒 1、slope 10Y+0.5/2Y−0.5、curvature 5Y+1/2Y−0.5/10Y−0.5。"""
    level, slope, curvature = factor_basis_value(5.0)
    assert level == 1.0
    assert slope == pytest.approx(-0.125)  # (5−6)/8
    assert curvature == pytest.approx(1.0)

    _level_2, slope_2, curvature_2 = factor_basis_value(2.0)
    assert slope_2 == pytest.approx(-0.5)
    assert curvature_2 == pytest.approx(-0.5)

    _level_10, slope_10, curvature_10 = factor_basis_value(10.0)
    assert slope_10 == pytest.approx(0.5)
    assert curvature_10 == pytest.approx(-0.5)

    # 范围外平推
    assert factor_basis_value(0.25)[1] == pytest.approx(-0.5)
    assert factor_basis_value(30.0)[1] == pytest.approx(0.5)
