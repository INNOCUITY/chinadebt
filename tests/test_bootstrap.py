"""即期曲线 bootstrap 的验收测试。

对照任务要求逐条覆盖：
1. **往返一致性**：bootstrap 得到的即期曲线给每个合成平价债重估必须 = 100（±1e-8）；
2. 向上倾斜曲线：即期 ≥ 同期限 YTM（同复利口径换算后）、远期 ≥ 即期；
3. 无套利单调性：discount 严格递减且 > 0；远期有限（倒挂允许负远期）；
4. 已知解析解：平坦曲线 → 零息利率可解析复现；
5. 真实数据冒烟：2024-06-28 / 2013-06-20（极端畸变日）/ 2026-11-05；
另加：合成假设必须在 ``assumptions`` 里显式留痕、频率覆盖、短端锚点校验等。
"""

from __future__ import annotations

from datetime import date, timedelta

import numpy as np
import pandas as pd
import pytest

from ficc.config import get_config
from ficc.curve.bootstrap import (
    SpotCurve,
    bootstrap_spot,
    default_freq_for_tenor,
    frequency_for_tenor,
    price_synthetic_par_bond,
)
from ficc.curve.curve import Curve
from ficc.data.schema import TENOR_LABELS

TENORS = np.asarray([0.25, 0.5, 1.0, 2.0, 3.0, 5.0, 7.0, 10.0, 15.0, 20.0, 30.0])
# 2024-06-28 实测中债国债到期收益率（百分数）
RATES_UP = np.asarray(
    [1.4411, 1.4489, 1.5390, 1.6335, 1.7960, 1.9782, 2.1030, 2.2058, 2.3138, 2.3629, 2.4282]
)
AS_OF = date(2024, 6, 28)


def _curve(rates: np.ndarray, tenors: np.ndarray = TENORS, as_of: date = AS_OF) -> Curve:
    return Curve(as_of=as_of, tenors=np.asarray(tenors, float), rates=np.asarray(rates, float))


def _real_panel(path) -> pd.DataFrame:
    raw = pd.read_csv(path)
    frame = raw[raw["term_years"].isin(TENORS)][["date", "term_years", "yield_pct"]]
    frame = frame.drop_duplicates(subset=["date", "term_years"])
    panel = frame.pivot(index="date", columns="term_years", values="yield_pct").sort_index()
    panel.index = pd.to_datetime(panel.index)
    panel.columns = pd.Index(panel.columns.to_numpy(dtype=float), name="tenor")
    return panel.reindex(columns=list(TENORS)).sort_index()


def _ytm_equivalent_pct(z_pct: float, tenor: float, freq: int) -> float:
    """把连续复利零息利率换算成**同复利口径**的 YTM（百分数），用于凸性比较。"""
    z = float(z_pct)
    t = float(tenor)
    f = int(freq)
    if f == 0:  # 单利：df = 1/(1+y t) = exp(-z t/100) → y = (e^{z t/100}-1)/t
        return 100.0 * (np.exp(z * t / 100.0) - 1.0) / t
    # 分频复利：df = (1+y/(100 f))^{-f t} = exp(-z t/100) → y = 100 f (e^{z/(100 f)} - 1)
    return 100.0 * f * (np.exp(z / (100.0 * f)) - 1.0)


# ---------------------------------------------------------------------------
# 1. 往返一致性（核心验收）
# ---------------------------------------------------------------------------
def test_roundtrip_par_prices_are_exactly_100() -> None:
    spot = bootstrap_spot(_curve(RATES_UP))
    errors = [abs(price_synthetic_par_bond(spot, float(t)) - 100.0) for t in TENORS]
    assert max(errors) < 1e-8, f"往返误差 {max(errors):.3e} 超过 1e-8"


def test_roundtrip_random_upward_and_inverted_curves() -> None:
    """随机曲线（含倒挂）也要往返一致：bootstrap 逻辑不能依赖曲线形态。"""
    rng = np.random.default_rng(20240628)
    for trial in range(5):
        base = rng.uniform(1.5, 4.5)
        slope = rng.uniform(-2.5, 2.5)
        rates = base + slope * (TENORS / TENORS[-1]) ** 0.7
        spot = bootstrap_spot(_curve(rates))
        errors = [abs(price_synthetic_par_bond(spot, float(t)) - 100.0) for t in TENORS]
        assert max(errors) < 1e-8, f"trial={trial} 往返误差 {max(errors):.3e}"


def test_roundtrip_with_overridden_frequency_rule() -> None:
    """覆盖频率规则（10Y+ 改年付）后，往返一致性依然成立。"""
    rule = {"10Y": 1, "15Y": 1, "20Y": 1, "30Y": 1}
    spot = bootstrap_spot(_curve(RATES_UP), freq_rule=rule)
    assert spot.assumptions["freq_rule"]["10Y"] == 1
    errors = [abs(price_synthetic_par_bond(spot, float(t)) - 100.0) for t in TENORS]
    assert max(errors) < 1e-8


# ---------------------------------------------------------------------------
# 2. 向上倾斜曲线：即期 ≥ YTM、远期 ≥ 即期
# ---------------------------------------------------------------------------
def test_zero_rate_above_ytm_for_upward_curve() -> None:
    spot = bootstrap_spot(_curve(RATES_UP))
    for tenor, ytm in zip(TENORS, RATES_UP, strict=True):
        freq = default_freq_for_tenor(float(tenor))
        z = float(spot.zero_rate(float(tenor)))
        y_equiv = _ytm_equivalent_pct(z, float(tenor), freq)
        assert y_equiv >= ytm - 1e-6, (
            f"t={tenor}: 即期等效 YTM {y_equiv:.6f}% 低于 YTM {ytm:.6f}%（凸性/息票效应方向错误）"
        )


def test_forward_above_zero_for_upward_curve() -> None:
    spot = bootstrap_spot(_curve(RATES_UP))
    fwd_5_10 = spot.forward(5.0, 10.0)
    assert fwd_5_10 > float(spot.zero_rate(10.0))
    assert fwd_5_10 > float(spot.zero_rate(5.0))
    assert spot.forward(0.25, 1.0) > float(spot.zero_rate(0.25))


# ---------------------------------------------------------------------------
# 3. 无套利单调性
# ---------------------------------------------------------------------------
def test_discount_is_strictly_decreasing_and_positive() -> None:
    spot = bootstrap_spot(_curve(RATES_UP))
    dense = np.linspace(0.0, 30.0, 601)
    dfs = np.asarray(spot.discount(dense))
    assert np.all(dfs > 0.0)
    assert np.all(np.diff(dfs) < 0.0), "discount 必须严格递减（无套利）"
    assert spot.discount(0.0) == pytest.approx(1.0)
    # 节点上的零息利率必须精确等于输入
    np.testing.assert_allclose(np.asarray(spot.zero_rate(TENORS)), spot.zero_rates_pct, atol=1e-12)
    # discount 与零息定义一致
    np.testing.assert_allclose(
        np.asarray(spot.discount(TENORS)),
        np.exp(-spot.zero_rates_pct / 100.0 * spot.times),
        rtol=1e-14,
    )


def test_inverted_curve_forwards_are_finite(real_spike_csv) -> None:
    """倒挂日（2013-06-20 钱荒）允许负远期，但必须有限、且 discount 仍单调。"""
    panel = _real_panel(real_spike_csv)
    row = panel.loc["2013-06-20"].dropna()
    spot = bootstrap_spot(
        Curve(
            as_of=date(2013, 6, 20),
            tenors=row.index.to_numpy(dtype=float),
            rates=row.to_numpy(dtype=float),
        )
    )
    forwards = [
        spot.forward(float(a), float(b))
        for a, b in [(0.25, 0.5), (1.0, 2.0), (5.0, 10.0), (10.0, 30.0)]
    ]
    assert np.all(np.isfinite(forwards))
    dfs = np.asarray(spot.discount(np.linspace(0.0, 30.0, 301)))
    assert np.all(dfs > 0) and np.all(np.diff(dfs) < 0)
    # 该日短端远高于长端：零息曲线应明显向下（至少 0.25Y > 10Y）
    assert float(spot.zero_rate(0.25)) > float(spot.zero_rate(10.0))


# ---------------------------------------------------------------------------
# 4. 解析解
# ---------------------------------------------------------------------------
def test_flat_curve_annual_has_analytic_zero_rates() -> None:
    """平坦曲线（全部年付）的解析解：z = 100·ln(1 + y/100)，与 YTM 有凸性差。"""
    y = 3.0
    tenors = np.asarray([1.0, 2.0, 3.0, 5.0, 7.0])
    spot = bootstrap_spot(_curve(np.full(tenors.size, y), tenors=tenors))
    expected = 100.0 * np.log(1.0 + y / 100.0)
    np.testing.assert_allclose(spot.zero_rates_pct, expected, atol=1e-10)
    # 连续复利与年复利的换算差（bp）
    assert (y - expected) * 100 > 0
    # 往返仍然精确
    for tenor in tenors:
        assert price_synthetic_par_bond(spot, float(tenor)) == pytest.approx(100.0, abs=1e-10)


def test_flat_curve_short_end_simple_interest_analytic() -> None:
    """<1Y 单利解析解：df = 1/(1+y t) → z = 100·ln(1+y t)/t。"""
    y = 2.0
    spot = bootstrap_spot(_curve(np.array([y, y]), tenors=np.array([0.25, 0.5])))
    expected = 100.0 * np.log(1.0 + y / 100.0 * 0.5) / 0.5
    assert spot.zero_rates_pct[1] == pytest.approx(expected, rel=1e-13)
    assert price_synthetic_par_bond(spot, 0.5) == pytest.approx(100.0, abs=1e-10)


def test_flat_curve_mixed_frequencies() -> None:
    """平坦曲线混合频率：1–7Y 年付节点解析；10Y+ 半年付与 200·ln(1+y/200) 差 <1bp。"""
    y = 3.3
    spot = bootstrap_spot(_curve(np.full(TENORS.size, y)))
    annual_expected = 100.0 * np.log(1.0 + y / 100.0)
    for tenor in [1.0, 2.0, 3.0, 5.0, 7.0]:
        z = float(spot.zero_rate(tenor))
        assert z == pytest.approx(annual_expected, abs=1e-10)
    semi_expected = 200.0 * np.log(1.0 + y / 200.0)
    for tenor in [10.0, 15.0, 20.0, 30.0]:
        z = float(spot.zero_rate(tenor))
        assert abs(z - semi_expected) < 0.01, f"t={tenor}: z={z:.5f} vs {semi_expected:.5f}"


# ---------------------------------------------------------------------------
# 5. 真实数据冒烟（覆盖正常日 / 极端畸变日 / 最新日）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("day", ["2024-06-28", "2013-06-20", "2026-11-05"])
def test_real_data_spot_smoke(real_spike_csv, day: str) -> None:
    panel = _real_panel(real_spike_csv)
    row = panel.loc[day].dropna()
    curve = Curve(
        as_of=pd.Timestamp(day).date(),
        tenors=row.index.to_numpy(dtype=float),
        rates=row.to_numpy(dtype=float),
    )
    spot = bootstrap_spot(curve)
    assert spot.times.size == row.size
    assert spot.method == "par_bootstrap"
    assert spot.source_curve_method == "linear"
    errors = [abs(price_synthetic_par_bond(spot, float(t)) - 100.0) for t in row.index]
    assert max(errors) < 1e-8, f"{day} 往返误差 {max(errors):.3e}"
    dfs = np.asarray(spot.discount(row.index.to_numpy(dtype=float)))
    assert np.all(dfs > 0) and np.all(np.diff(dfs) < 0)


# ---------------------------------------------------------------------------
# 合成假设必须显式留痕
# ---------------------------------------------------------------------------
def test_assumptions_are_explicit() -> None:
    spot = bootstrap_spot(_curve(RATES_UP))
    assumptions = spot.assumptions
    assert assumptions["synthetic_not_official"] is True
    lowered = " ".join(str(v) for v in assumptions.values()).lower()
    for keyword in ("平价", "par", "coupon", "插值", "单利", "结算"):
        assert keyword in lowered, f"assumptions 缺少 {keyword!r} 的声明"
    # 频率规则：<1Y 零息、1–7Y 年付、10Y+ 半年付
    assert assumptions["freq_rule"] == {
        "3M": 0,
        "6M": 0,
        "1Y": 1,
        "2Y": 1,
        "3Y": 1,
        "5Y": 1,
        "7Y": 1,
        "10Y": 2,
        "15Y": 2,
        "20Y": 2,
        "30Y": 2,
    }
    # 票面 = 该期限 YTM，价格 = 100
    for tenor, rate in zip(TENORS, RATES_UP, strict=True):
        label = TENOR_LABELS[float(tenor)]
        assert assumptions["coupon_pct_by_tenor"][label] == pytest.approx(float(rate))
    # T+1 结算
    assert assumptions["settlement_lag_days"] == int(get_config().pricing.settlement_lag_days)
    expected_settlement = AS_OF + timedelta(days=int(get_config().pricing.settlement_lag_days))
    assert assumptions["settlement_date"] == expected_settlement.isoformat()
    assert assumptions["source_curve_method"] == "linear"
    assert "线性插值" in assumptions["interpolation"]


def test_synthetic_bond_repricing_matches_assumption_coupons() -> None:
    """price_synthetic_par_bond 必须使用 assumptions 里的票面（价格 100 的平价债）。"""
    spot = bootstrap_spot(_curve(RATES_UP))
    by_tenor = spot.assumptions["coupon_pct_by_tenor"]
    assert by_tenor["10Y"] == pytest.approx(float(RATES_UP[7]))
    assert price_synthetic_par_bond(spot, 10.0, coupon_pct=float(RATES_UP[7])) == pytest.approx(
        100.0, abs=1e-8
    )


# ---------------------------------------------------------------------------
# 频率规则 / 校验
# ---------------------------------------------------------------------------
def test_frequency_rule_parsing_and_errors() -> None:
    assert frequency_for_tenor(0.25) == 0
    assert frequency_for_tenor(1.0) == 1
    assert frequency_for_tenor(7.0) == 1
    assert frequency_for_tenor(10.0) == 2
    assert frequency_for_tenor(30.0) == 2
    assert frequency_for_tenor(10.0, {"10Y": 1}) == 1
    assert frequency_for_tenor(10.0, {"10.0": 1}) == 1
    assert frequency_for_tenor(1.0, {"1y": "annual"}) == 1
    with pytest.raises(ValueError, match="频率"):
        frequency_for_tenor(1.0, {"1Y": "weekly"})
    with pytest.raises(ValueError, match="期限"):
        frequency_for_tenor(1.0, {"abc": 1})
    with pytest.raises(ValueError, match="不存在"):
        bootstrap_spot(_curve(RATES_UP), freq_rule={"50Y": 1})
    assert default_freq_for_tenor(0.5) == 0


def test_bootstrap_rejects_curve_without_short_anchor() -> None:
    """只有长端期限点时（现金流时点需要外推）应显式拒绝，而不是给不稳健结果。"""
    with pytest.raises(ValueError, match="短端锚点"):
        bootstrap_spot(_curve(np.array([3.0, 3.1, 3.2]), tenors=np.array([10.0, 15.0, 20.0])))


def test_spotcurve_validation_and_interpolation() -> None:
    spot = bootstrap_spot(_curve(RATES_UP))
    # 线性插值：中点应等于两端均值
    mid = (spot.times[1] + spot.times[2]) / 2.0
    expected = (spot.zero_rates_pct[1] + spot.zero_rates_pct[2]) / 2.0
    assert float(spot.zero_rate(mid)) == pytest.approx(expected, abs=1e-14)
    # 显式构造也走同一套校验
    with pytest.raises(ValueError, match="严格递增"):
        SpotCurve(
            as_of=AS_OF, times=np.array([1.0, 1.0]), zero_rates_pct=np.array([1.0, 2.0]), method="x"
        )
    with pytest.raises(ValueError, match="长度不一致"):
        SpotCurve(
            as_of=AS_OF, times=np.array([1.0, 2.0]), zero_rates_pct=np.array([1.0]), method="x"
        )
    with pytest.raises(ValueError, match="NaN"):
        SpotCurve(
            as_of=AS_OF,
            times=np.array([1.0, 2.0]),
            zero_rates_pct=np.array([1.0, np.nan]),
            method="x",
        )
    with pytest.raises(ValueError, match="不能为负"):
        spot.discount(-1.0)
    with pytest.raises(ValueError, match="t2"):
        spot.forward(5.0, 5.0)


def test_spotcurve_to_frame_columns() -> None:
    spot = bootstrap_spot(_curve(RATES_UP))
    frame = spot.to_frame()
    assert list(frame.columns) == ["tenor", "zero_pct", "discount"]
    np.testing.assert_allclose(frame["tenor"].to_numpy(), TENORS)
    np.testing.assert_allclose(frame["zero_pct"].to_numpy(), spot.zero_rates_pct)
    np.testing.assert_allclose(
        frame["discount"].to_numpy(), np.exp(-spot.zero_rates_pct / 100.0 * spot.times)
    )
