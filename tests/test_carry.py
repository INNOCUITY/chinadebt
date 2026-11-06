"""carry / roll-down 事前估算与事后分解测试。

口径：收益率百分数；修正久期用全价、由合成平价债解析算；滚动目标锁定 T−1Y
（10Y→9Y，**不是 7Y**）。融资利率缺失必须报错（默认 0 成本是错的）。

曲线用 ``ficc.curve.Curve`` 合成，测试不依赖真实数据。
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pytest

from ficc.config import Config, get_config
from ficc.curve.curve import Curve
from ficc.pricing.carry import (
    FundingRateUnavailableError,
    carry_roll_down,
    decompose_realized,
    par_modified_duration,
    resolve_funding_rate_pct,
)

TENORS = np.array([1.0, 2.0, 3.0, 5.0, 7.0, 10.0, 15.0, 20.0, 30.0])
AS_OF = date(2026, 11, 4)


def make_curve(rates: np.ndarray, *, as_of: date = AS_OF) -> Curve:
    return Curve(as_of=as_of, tenors=TENORS.copy(), rates=np.asarray(rates, dtype=float))


def flat_curve(rate_pct: float = 3.0, *, as_of: date = AS_OF) -> Curve:
    return make_curve(np.full_like(TENORS, rate_pct), as_of=as_of)


def steep_curve(base_pct: float = 1.5, slope_bp_per_year: float = 5.0) -> Curve:
    """向上倾斜曲线：y(t) = base + (slope_bp/100)·t（每年 +5bp）。"""
    return make_curve(base_pct + slope_bp_per_year / 100.0 * TENORS)


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
# 事前：carry / roll-down
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
    expected_roll = -(y_9 - y_10) * duration_9y
    assert result["roll_pct"] == pytest.approx(expected_roll, rel=1e-10)
    assert expected_roll > 0.0  # 向上倾斜曲线持有一年 → 正 roll

    # 错误地滚到 7Y（memo 警告的经典错误）会高估多少
    _p7, duration_7y = par_price_and_mod_duration(y_7, years=7.0, freq=1)
    wrong_roll = -(y_7 - y_10) * duration_7y
    assert wrong_roll - expected_roll > 0.5  # memo：约 0.5–0.9pct 的高估


def test_par_modified_duration_matches_closed_form() -> None:
    """曲线解析算出的 9Y 平价债修正久期与独立手算一致。"""
    curve = steep_curve()
    y_9 = float(curve.rate(9.0))
    _p0, duration_manual = par_price_and_mod_duration(y_9, years=9.0, freq=2)
    assert par_modified_duration(curve, 9.0) == pytest.approx(duration_manual, rel=1e-10)


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
# 事后：decompose_realized
# ---------------------------------------------------------------------------
T1_AS_OF = AS_OF + timedelta(days=365)


def shocked_curve(base: Curve, kind: str, bp: float) -> Curve:
    return make_curve(base.shock(kind, bp).rates, as_of=T1_AS_OF)  # type: ignore[arg-type]


def test_decompose_identity_closed_loop() -> None:
    """平移/陡峭/蝶式冲击后：五项之和 == total（<1bp），残差本身也只有几个 bp。"""
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
            + result["curve_shape"]
            + result["residual"]
        )
        assert decomposed == pytest.approx(result["total"], abs=1e-10), name
        assert abs(result["residual"]) < 0.03, f"{name}: 残差 {result['residual']:.4f}% 超 3bp"


def test_decompose_no_curve_change() -> None:
    """曲线不变（仅时间前进 1 年）：carry=0（票息=融资 2%），收益全部来自 roll。"""
    base = steep_curve()
    curve_t1 = make_curve(base.rates, as_of=T1_AS_OF)
    result = decompose_realized(10.0, base, curve_t1, horizon_days=365, funding_rate_pct=2.0)

    assert result["carry"] == pytest.approx(0.0, abs=1e-12)  # y_10 = 2% = 融资
    assert result["curve_level"] == pytest.approx(0.0, abs=1e-12)
    assert result["curve_shape"] == pytest.approx(0.0, abs=1e-12)
    assert result["residual"] == pytest.approx(0.0, abs=1e-12)
    assert result["roll"] > 0.0  # 沿曲线下滑的资本利得
    assert result["roll"] == pytest.approx(result["total"], rel=1e-12)


def test_decompose_level_shock_sign_and_magnitude() -> None:
    """平行上移 25bp：level 项显著为负，占主导；total = level + roll。"""
    base = steep_curve()
    curve_t1 = make_curve(base.rates + 0.25, as_of=T1_AS_OF)
    result = decompose_realized(10.0, base, curve_t1, horizon_days=365, funding_rate_pct=2.0)

    assert result["curve_level"] == pytest.approx(-2.06, abs=0.05)
    assert result["curve_level"] < -1.5
    assert abs(result["curve_shape"]) < 0.01
    assert result["total"] < 0.0
    assert result["total"] == pytest.approx(result["roll"] + result["curve_level"], abs=0.05)


def test_decompose_steepen_and_butterfly_shape_terms() -> None:
    base = steep_curve()
    steepened = decompose_realized(
        10.0,
        base,
        shocked_curve(base, "steepen", 25.0),
        horizon_days=365,
        funding_rate_pct=2.0,
    )
    assert steepened["curve_shape"] < -0.5  # 长端上行对 10Y 券不利
    assert steepened["level_bp"] == pytest.approx(0.0, abs=0.1)
    assert steepened["slope_bp"] == pytest.approx(25.0, abs=0.1)

    butterfly = decompose_realized(
        10.0,
        base,
        shocked_curve(base, "butterfly", 25.0),
        horizon_days=365,
        funding_rate_pct=2.0,
    )
    assert butterfly["curve_shape"] < -0.5
    assert butterfly["level_bp"] == pytest.approx(0.0, abs=0.1)
    assert butterfly["curvature_bp"] == pytest.approx(25.0, abs=0.1)


def test_decompose_horizon_zero() -> None:
    base = steep_curve()
    same_day = make_curve(base.rates, as_of=AS_OF)
    result = decompose_realized(10.0, base, same_day, horizon_days=0, funding_rate_pct=2.0)
    for key in ("carry", "roll", "curve_level", "curve_shape", "residual", "total"):
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
