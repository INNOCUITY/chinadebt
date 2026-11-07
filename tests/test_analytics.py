"""风险指标测试：memo 自算算例 A/B/D 回归 + 解析式与有限差分交叉校验。

黄金数字来自 ``docs/00_market_conventions.md`` §3.2；收益率百分数，金额元/百元面值。

注意（memo 自算算例 D 的结算日）：memo 只给了「30Y、票面 2.38%、半年付、y≈2.04% 时修正久期
≈ 21.18、DV01 ≈ 0.2291」的量级值，未给该券的起息日与结算日。本测试采用一组合理
假设——起息 2026-01-15、到期 2056-01-15、结算 2026-11-04——实测 modD=21.1758、
DV01=0.229068、凸性=555.40，与 memo 数字在 1e-2 / 1e-4 / 1.0 容差内。若换成别的
结算日，结果会按实际天数小幅漂移，故容差按 memo 明示的 1e-2 / 1e-4 设置。
"""

from __future__ import annotations

import inspect
from datetime import date, timedelta

import numpy as np
import pytest

from ficc.config import get_config
from ficc.data.schema import BondStatic
from ficc.pricing.analytics import (
    convexity,
    dv01,
    dv01_finite_diff,
    key_rate_durations,
    macaulay_duration,
    mod_duration,
    spot_dv01,
)
from ficc.pricing.bond import cashflows, dirty_price_from_ytm, ytm_from_dirty_price
from ficc.pricing.conventions import accrued_interest, day_count


def make_bond(
    *,
    code: str = "260010.IB",
    issue_date: date = date(2026, 5, 15),
    maturity_date: date = date(2036, 5, 15),
    coupon_pct: float = 1.72,
    freq: int = 2,
    accrual: str = "coupon",
    market: str = "IB",
) -> BondStatic:
    return BondStatic(
        code=code,
        issue_date=issue_date,
        maturity_date=maturity_date,
        coupon_pct=coupon_pct,
        freq=freq,
        accrual=accrual,
        market=market,
    )


# 30Y 量级感算例 D 的构造券（起息日/结算日为文档允许的自选值，见模块 docstring）
BOND_30Y = make_bond(
    code="260002.IB",
    issue_date=date(2026, 1, 15),
    maturity_date=date(2056, 1, 15),
    coupon_pct=2.38,
    freq=2,
)


# ---------------------------------------------------------------------------
# memo 自算算例 A / B
# ---------------------------------------------------------------------------
def test_official_A_duration_and_convexity(near_par_bond: BondStatic) -> None:
    settle, ytm = date(2026, 5, 28), 1.72
    assert macaulay_duration(near_par_bond, settle, ytm) == pytest.approx(9.1946, abs=1e-3)
    assert mod_duration(near_par_bond, settle, ytm) == pytest.approx(9.1162, abs=1e-3)
    assert convexity(near_par_bond, settle, ytm) == pytest.approx(92.03, abs=0.5)


def test_official_A_dv01(near_par_bond: BondStatic) -> None:
    settle, ytm = date(2026, 5, 28), 1.72
    assert dv01(near_par_bond, settle, ytm) == pytest.approx(0.091217, abs=1e-5)
    # 单位换算核对：1 亿元面值 = 1e6 个「百元面值」单位 → 9.12 万元/bp
    assert dv01(near_par_bond, settle, ytm) * 1e6 == pytest.approx(91217.4, rel=1e-4)


def test_official_B_analytics(near_par_bond: BondStatic) -> None:
    settle, ytm = date(2026, 11, 4), 1.61
    assert macaulay_duration(near_par_bond, settle, ytm) == pytest.approx(8.7647, abs=1e-3)
    assert mod_duration(near_par_bond, settle, ytm) == pytest.approx(8.6947, abs=1e-3)
    assert convexity(near_par_bond, settle, ytm) == pytest.approx(84.29, abs=0.5)
    assert dv01(near_par_bond, settle, ytm) == pytest.approx(0.088492, abs=1e-5)


# ---------------------------------------------------------------------------
# memo 自算算例 D：30Y 量级感（起息/结算日期为假设值，见模块 docstring）
# ---------------------------------------------------------------------------
def test_30y_magnitude_matches_memo() -> None:
    settle, ytm = date(2026, 11, 4), 2.04
    assert mod_duration(BOND_30Y, settle, ytm) == pytest.approx(21.18, abs=1e-2)
    assert dv01(BOND_30Y, settle, ytm) == pytest.approx(0.2291, abs=1e-4)
    assert convexity(BOND_30Y, settle, ytm) == pytest.approx(555.0, abs=1.0)
    # 30Y 的利率风险量级约为 10Y 的 2.5 倍（memo §3.2）
    ratio = mod_duration(BOND_30Y, settle, ytm) / mod_duration(make_bond(), date(2026, 5, 28), 1.72)
    assert ratio == pytest.approx(2.3, abs=0.1)


# ---------------------------------------------------------------------------
# 解析式 vs 有限差分
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    ("settle", "ytm"),
    [(date(2026, 5, 28), 1.72), (date(2026, 11, 4), 1.61)],
)
def test_dv01_analytic_matches_finite_diff(settle: date, ytm: float) -> None:
    bond = make_bond()
    assert dv01(bond, settle, ytm) == pytest.approx(dv01_finite_diff(bond, settle, ytm), abs=1e-5)


def test_30y_dv01_matches_finite_diff() -> None:
    settle, ytm = date(2026, 11, 4), 2.04
    assert dv01(BOND_30Y, settle, ytm) == pytest.approx(
        dv01_finite_diff(BOND_30Y, settle, ytm), abs=1e-5
    )


# ---------------------------------------------------------------------------
# R08：非 1bp 步长下的 DV01 单位不变性
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("bp", [0.1, 0.25, 0.5, 1.0, 2.0, 5.0])
def test_dv01_finite_diff_unit_invariance(near_par_bond: BondStatic, bp: float) -> None:
    """DV01 是「每 1bp」的敏感度，必须与差分步长无关（R08）。

    中心差分除以 ``2*bp`` 后：所有步长的绝对偏差 < 1e-6 元；bp ≤ 2 时相对偏差
    也 < 1e-6。bp = 5 的相对偏差约 4.5e-6，是中心差分截断误差 O(bp²) 的固有
    量级（不是单位错误），绝对偏差仍 < 1e-6。
    """
    settle, ytm = date(2026, 5, 28), 1.72
    analytic = dv01(near_par_bond, settle, ytm)
    fd = dv01_finite_diff(near_par_bond, settle, ytm, bp=bp)
    assert fd == pytest.approx(analytic, abs=1e-6)
    if bp <= 2.0:
        assert abs(fd - analytic) / analytic < 1e-6


def test_dv01_finite_diff_r08_regression(near_par_bond: BondStatic) -> None:
    """R08 反例回归：修复前 bp=0.5 返回 0.04560868948（少一半）、
    bp=2 返回 0.18243488133（多一倍）；修复后两者都应≈解析 DV01=0.09121737484。
    """
    settle, ytm = date(2026, 5, 28), 1.72
    analytic = 0.09121737484
    assert dv01(near_par_bond, settle, ytm) == pytest.approx(analytic, abs=1e-9)
    # 修复后实测：bp=0.5 → 0.091217378954；bp=2 → 0.091217440666
    assert dv01_finite_diff(near_par_bond, settle, ytm, bp=0.5) == pytest.approx(analytic, rel=1e-6)
    assert dv01_finite_diff(near_par_bond, settle, ytm, bp=2.0) == pytest.approx(analytic, rel=1e-6)


@pytest.mark.parametrize("bad_bp", [0.0, -0.5])
def test_dv01_finite_diff_rejects_nonpositive_bp(near_par_bond: BondStatic, bad_bp: float) -> None:
    with pytest.raises(ValueError, match="bp 必须为正数"):
        dv01_finite_diff(near_par_bond, date(2026, 5, 28), 1.72, bp=bad_bp)


def test_dv01_matches_one_bp_price_change(near_par_bond: BondStatic) -> None:
    """±1bp 的价格变化应等于 DV01（差值为凸性二阶项，量级 ~5e-5 元）。"""
    settle, ytm = date(2026, 5, 28), 1.72
    dv = dv01(near_par_bond, settle, ytm)
    p0 = dirty_price_from_ytm(near_par_bond, settle, ytm)
    p_down = dirty_price_from_ytm(near_par_bond, settle, ytm - 0.01)  # -1bp
    p_up = dirty_price_from_ytm(near_par_bond, settle, ytm + 0.01)  # +1bp
    # 单边 ±1bp 与 DV01 差 0.5×凸性×P×(1bp)² ≈ 4.6e-5，故用 1e-4 容差
    assert p_down - p0 == pytest.approx(dv, abs=1e-4)
    assert p0 - p_up == pytest.approx(dv, abs=1e-4)
    # 中心差分则与解析 DV01 高度一致（< 1e-7）
    assert (p_down - p_up) / 2.0 == pytest.approx(dv, abs=1e-7)


def test_plus_100bp_second_order_approximation(near_par_bond: BondStatic) -> None:
    """久期+凸性预测 vs 实际 +100bp 价格：误差应很小（memo 实测 0.016 元）。"""
    settle, ytm = date(2026, 5, 28), 1.72
    p0 = dirty_price_from_ytm(near_par_bond, settle, ytm)
    mod = mod_duration(near_par_bond, settle, ytm)
    conv = convexity(near_par_bond, settle, ytm)
    dy = 0.01  # +100bp，小数单位
    predicted = p0 * (1.0 - mod * dy + 0.5 * conv * dy**2)
    actual = dirty_price_from_ytm(near_par_bond, settle, ytm + 1.0)
    assert actual == pytest.approx(predicted, abs=0.05)
    # 一阶项（仅久期）会明显高估跌幅，凸性修正不可忽略
    assert predicted > p0 * (1.0 - mod * dy)


# ---------------------------------------------------------------------------
# 口径与量纲检查
# ---------------------------------------------------------------------------
def test_dv01_uses_full_price_not_clean_price(near_par_bond: BondStatic) -> None:
    """DV01 必须用全价乘修正久期，用净价会系统性低估 AI×D_mod 那一块。"""
    settle, ytm = date(2026, 5, 28), 1.72
    dirty = dirty_price_from_ytm(near_par_bond, settle, ytm)
    ai = accrued_interest(near_par_bond, settle)
    clean = dirty - ai
    mod = mod_duration(near_par_bond, settle, ytm)
    dv = dv01(near_par_bond, settle, ytm)

    assert dv == pytest.approx(dirty * mod * 1e-4, rel=1e-12)
    assert dv == pytest.approx(clean * mod * 1e-4 + ai * mod * 1e-4, rel=1e-12)
    assert dv > clean * mod * 1e-4  # 净价口径偏低


def test_mod_duration_equals_macaulay_divided_by_compounding(near_par_bond: BondStatic) -> None:
    settle, ytm = date(2026, 5, 28), 1.72
    mac = macaulay_duration(near_par_bond, settle, ytm)
    assert mod_duration(near_par_bond, settle, ytm) == pytest.approx(
        mac / (1.0 + ytm / 100.0 / 2), rel=1e-12
    )
    assert mod_duration(near_par_bond, settle, ytm) < mac


def test_duration_ordering_30y_10y_2y() -> None:
    """期限越长久期越大：30Y > 10Y > 2Y。"""
    settle = date(2026, 11, 4)
    bond_2y = make_bond(
        code="SYN-2Y.IB",
        issue_date=date(2026, 3, 15),
        maturity_date=date(2028, 3, 15),
        coupon_pct=1.20,
        freq=1,
    )
    mod_30 = mod_duration(BOND_30Y, settle, 2.04)
    mod_10 = mod_duration(make_bond(), date(2026, 5, 28), 1.72)
    mod_2 = mod_duration(bond_2y, settle, 1.25)
    assert mod_30 > mod_10 > mod_2 > 0.0


def test_convexity_positive(near_par_bond: BondStatic) -> None:
    settle, ytm = date(2026, 5, 28), 1.72
    assert convexity(near_par_bond, settle, ytm) > 0.0


# ---------------------------------------------------------------------------
# 利随本清 / 贴现分支的解析式交叉校验
# ---------------------------------------------------------------------------
def test_lump_sum_analytics_match_finite_diff() -> None:
    bond = make_bond(
        code="260001.IB",
        issue_date=date(2026, 1, 15),
        maturity_date=date(2027, 1, 15),
        coupon_pct=1.22,
        freq=1,
        accrual="lump_sum",
    )
    settle, ytm = date(2026, 11, 5), 1.5
    tau_years = day_count(settle, bond.maturity_date) / 365.0
    assert macaulay_duration(bond, settle, ytm) == pytest.approx(tau_years, rel=1e-12)
    assert dv01(bond, settle, ytm) == pytest.approx(dv01_finite_diff(bond, settle, ytm), abs=1e-9)
    assert convexity(bond, settle, ytm) > 0.0


def test_discount_analytics_match_finite_diff() -> None:
    bond = make_bond(
        code="SYN-DISC.IB",
        issue_date=date(2026, 1, 15),
        maturity_date=date(2026, 4, 16),
        coupon_pct=0.0,
        freq=1,
        accrual="discount",
    )
    settle, ytm = date(2026, 2, 14), 1.8
    assert dv01(bond, settle, ytm) == pytest.approx(dv01_finite_diff(bond, settle, ytm), abs=1e-9)
    assert convexity(bond, settle, ytm) > 0.0
    assert 0.0 < mod_duration(bond, settle, ytm) < 0.25


# ---------------------------------------------------------------------------
# 即期曲线 KRD（含 R17 日期契约）
# ---------------------------------------------------------------------------
class SyntheticSpotCurve:
    """合成即期（零息）曲线：节点间线性插值、首尾平推。

    只实现 KRD 依赖的鸭子类型接口 ``times`` / ``zero_rate(t)`` / ``discount(t)``，
    并可选暴露 ``observation_date`` / ``settlement_date`` / ``as_of`` 供 R17
    日期契约校验；刻意**不** import ``ficc.curve.bootstrap``（曲线层并行开发，
    避免循环依赖）。零息利率为连续复利、单位百分数。
    """

    def __init__(
        self,
        times: list[float],
        zero_rates_pct: list[float],
        *,
        observation_date: date | None = None,
        settlement_date: date | None = None,
    ) -> None:
        self.times = np.asarray(times, dtype=float)
        self._rates = np.asarray(zero_rates_pct, dtype=float)
        if self.times.size != self._rates.size or np.any(np.diff(self.times) <= 0):
            raise ValueError("times/rates 必须等长且 times 严格递增")
        self.observation_date = observation_date
        self.settlement_date = settlement_date

    def zero_rate(self, t: float | np.ndarray) -> float | np.ndarray:
        scalar = np.ndim(t) == 0
        values = np.interp(np.atleast_1d(np.asarray(t, dtype=float)), self.times, self._rates)
        return float(values[0]) if scalar else values

    def discount(self, t: float | np.ndarray) -> float | np.ndarray:
        t_arr = np.asarray(t, dtype=float)
        return np.exp(-self.zero_rate(t_arr) / 100.0 * t_arr)


def make_spot_curve(
    *,
    observation_date: date | None = None,
    settlement_date: date | None = None,
) -> SyntheticSpotCurve:
    """z(t) = 1.8 + 0.05·t（连续复利，百分数），节点取 KRD 关键期限。"""
    keys = [float(t) for t in get_config().risk.key_tenors_krd]
    return SyntheticSpotCurve(
        keys,
        [1.8 + 0.05 * t for t in keys],
        observation_date=observation_date,
        settlement_date=settlement_date,
    )


def make_spot(settlement: date | None = None) -> SyntheticSpotCurve:
    """观察日 = 结算日 = ``settlement`` 的合成即期曲线（None 时不带日期字段）。"""
    return make_spot_curve(observation_date=settlement, settlement_date=settlement)


def spot_implied_ytm(bond: BondStatic, settlement: date, spot: SyntheticSpotCurve) -> float:
    """即期曲线定价对应的 YTM（用于与 YTM 口径 dv01 交叉检验）。"""
    frame = cashflows(bond, settlement)
    pv = sum(
        amount * float(spot.discount(day_count(settlement, cf_date) / 365.0))
        for cf_date, amount in zip(frame["date"], frame["amount"], strict=True)
    )
    return ytm_from_dirty_price(bond, settlement, pv)


# ---- 基本数值与一致性 ------------------------------------------------
def test_key_rate_durations_10y_vector_and_sum() -> None:
    """10Y 国债的 KRD：关键期限齐全、多头为负、sum ≈ 即期 DV01（相对误差 <1e-3）。"""
    settle = date(2026, 5, 28)
    spot = make_spot(settle)
    bond = make_bond()
    krd = key_rate_durations(bond, settle, spot)

    expected_keys = {float(t) for t in get_config().risk.key_tenors_krd}
    assert set(krd) == expected_keys
    # 多头、收益率上行 → 负值
    assert all(value <= 1e-12 for value in krd.values())
    # 现金流全部 ≤10Y，故 15/20/30Y 关键期限没有敞口
    for tenor in (15.0, 20.0, 30.0):
        assert krd[tenor] == pytest.approx(0.0, abs=1e-12)
    # 10Y 现金流的敞口最大
    assert krd[10.0] == min(krd.values())

    dv01_spot = spot_dv01(bond, settle, spot)
    assert dv01_spot < 0
    assert sum(krd.values()) == pytest.approx(dv01_spot, rel=1e-3)  # 验收要求
    assert sum(krd.values()) == pytest.approx(dv01_spot, rel=1e-4)  # 实测 ~1.7e-5


def test_krd_sum_consistency_for_30y_bond() -> None:
    """30Y 长券：sum(KRD) 与即期 DV01 相对差仍 <1e-3，与 YTM DV01 差 <2%。"""
    settle = date(2026, 11, 4)
    spot = make_spot(settle)
    krd = key_rate_durations(BOND_30Y, settle, spot)
    assert sum(krd.values()) == pytest.approx(spot_dv01(BOND_30Y, settle, spot), rel=1e-3)
    ytm = spot_implied_ytm(BOND_30Y, settle, spot)
    assert abs(sum(krd.values())) == pytest.approx(dv01(BOND_30Y, settle, ytm), rel=0.02)


def test_krd_sum_vs_ytm_dv01_within_2pct() -> None:
    """即期口径 sum(KRD) 与 YTM 口径 dv01() 相差 <2%，差异来源见 docstring。"""
    settle = date(2026, 5, 28)
    spot = make_spot(settle)
    bond = make_bond()
    krd_total = abs(sum(key_rate_durations(bond, settle, spot).values()))
    ytm = spot_implied_ytm(bond, settle, spot)
    assert krd_total == pytest.approx(dv01(bond, settle, ytm), rel=0.02)


def test_key_rate_durations_flat_spot_curve() -> None:
    """平坦即期曲线：KRD 全部为负、sum 闭合，长端敞口大于短端。"""
    keys = [float(t) for t in get_config().risk.key_tenors_krd]
    settle = date(2026, 5, 28)
    spot = SyntheticSpotCurve(
        keys, [2.0] * len(keys), observation_date=settle, settlement_date=settle
    )
    bond = make_bond()
    krd = key_rate_durations(bond, settle, spot)

    assert sum(krd.values()) == pytest.approx(spot_dv01(bond, settle, spot), rel=1e-4)
    assert all(value <= 0.0 for value in krd.values())
    assert abs(krd[10.0]) > abs(krd[2.0])


def test_key_rate_durations_zero_coupon_single_point() -> None:
    """零息债：KRD 全部落在到期期限那一个关键点上，且等于即期 DV01。"""
    settle = date(2026, 5, 28)
    spot = make_spot(settle)
    # 存续期恰为 10.0 年（3650 天），对应 10Y 关键期限
    zero = make_bond(
        code="SYN-Z10.IB",
        issue_date=settle,
        maturity_date=settle + timedelta(days=3650),
        coupon_pct=0.0,
        freq=1,
        accrual="discount",
    )
    krd = key_rate_durations(zero, settle, spot)
    dv01_spot = spot_dv01(zero, settle, spot)
    assert krd[10.0] == pytest.approx(dv01_spot, rel=1e-12)
    others = [value for tenor, value in krd.items() if tenor != 10.0]
    assert sum(abs(value) for value in others) == pytest.approx(0.0, abs=1e-18)


def test_key_rate_weights_linear_between_keys_and_flat_extrapolation() -> None:
    """关键点之间按线性插值分配权重；首尾平推；每列权重和为 1。"""
    from ficc.pricing.analytics import _key_rate_weights

    keys = np.array([2.0, 5.0, 10.0, 30.0])
    times = np.array([1.0, 3.5, 8.5, 40.0])  # 首端左侧、2-5 之间、5-10 之间、末端右侧
    weights = _key_rate_weights(keys, times)

    assert weights.shape == (4, 4)
    assert np.allclose(weights.sum(axis=0), 1.0, rtol=0.0, atol=1e-15)
    assert weights[0, 0] == pytest.approx(1.0)  # t=1.0 < 2.0 → 平推到首节点
    assert weights[0, 1] == pytest.approx(0.5)  # t=3.5 在 2 与 5 之间
    assert weights[1, 1] == pytest.approx(0.5)
    assert weights[1, 2] == pytest.approx(0.3)  # t=8.5 在 5 与 10 之间
    assert weights[2, 2] == pytest.approx(0.7)
    assert weights[3, 3] == pytest.approx(1.0)  # t=40 > 30 → 平推到末节点


def test_key_rate_durations_cashflow_before_first_key() -> None:
    """现金流早于首个关键期限（如 26 天后到期的票息）按平推归到首节点。"""
    settle = date(2026, 10, 20)  # 下一付息日 2026-11-15，t≈0.071 年 < 首个关键点 1Y
    spot = make_spot(settle)
    bond = make_bond()
    krd = key_rate_durations(bond, settle, spot)
    assert krd[1.0] < 0.0
    assert sum(krd.values()) == pytest.approx(spot_dv01(bond, settle, spot), rel=1e-3)


def test_key_rate_durations_cashflow_beyond_last_key() -> None:
    """现金流晚于最后一个关键期限（55Y 零息债）按平推归到末节点。"""
    settle = date(2026, 5, 28)
    spot = make_spot(settle)
    long_zero = make_bond(
        code="SYN-Z55.IB",
        issue_date=settle,
        maturity_date=settle + timedelta(days=55 * 365),
        coupon_pct=0.0,
        freq=1,
        accrual="discount",
    )
    krd = key_rate_durations(long_zero, settle, spot)
    last_key = max(float(t) for t in get_config().risk.key_tenors_krd)
    assert krd[last_key] == pytest.approx(sum(krd.values()), rel=1e-12)
    assert sum(krd.values()) == pytest.approx(spot_dv01(long_zero, settle, spot), rel=1e-12)


def test_key_rate_durations_uses_internal_fixed_1bp() -> None:
    """KRD 内部固定 1bp 步长：没有 bp 参数，重复调用结果一致（不做外部步长差分）。"""
    params = inspect.signature(key_rate_durations).parameters
    assert "bp" not in params and "bump_bp" not in params
    settle = date(2026, 5, 28)
    spot = make_spot(settle)
    bond = make_bond()
    assert key_rate_durations(bond, settle, spot) == key_rate_durations(bond, settle, spot)


def test_key_rate_durations_validates_inputs() -> None:
    settle = date(2026, 5, 28)
    spot = make_spot(settle)
    bond = make_bond()
    with pytest.raises(TypeError, match="spot 缺少必需成员"):
        key_rate_durations(bond, settle, object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="严格递增"):
        key_rate_durations(bond, settle, spot, key_tenors=[5.0, 2.0])
    with pytest.raises(ValueError, match="早于起息日"):
        key_rate_durations(bond, date(2026, 5, 14), spot)


def test_key_rate_durations_accepts_scalar_only_spot_discount() -> None:
    """鸭子类型最小可用性：只支持标量 discount 的实现也能算 KRD（内部逐点退化）。"""
    knots = np.array([1.0, 2.0, 3.0, 5.0, 7.0, 10.0, 15.0, 20.0, 30.0])
    levels = 1.8 + 0.05 * knots

    class ScalarOnlySpot:
        times = knots
        observation_date = date(2026, 5, 28)
        settlement_date = date(2026, 5, 28)

        def zero_rate(self, t: float) -> float:
            return float(np.interp(float(t), knots, levels))

        def discount(self, t: float) -> float:
            if np.ndim(t) != 0:
                raise TypeError("仅支持标量输入")
            return float(np.exp(-self.zero_rate(float(t)) / 100.0 * float(t)))

    bond = make_bond()
    settle = date(2026, 5, 28)
    krd = key_rate_durations(bond, settle, ScalarOnlySpot())
    assert sum(krd.values()) == pytest.approx(spot_dv01(bond, settle, make_spot(settle)), rel=1e-3)


def test_spot_dv01_matches_parallel_bump() -> None:
    """spot_dv01 与「所有现金流时间的折算因子乘以 exp(-1e-4·t)」一致。"""
    settle = date(2026, 5, 28)
    spot = make_spot(settle)
    bond = make_bond()
    frame = cashflows(bond, settle)
    times = np.asarray([day_count(settle, d) / 365.0 for d in frame["date"]])
    amounts = np.asarray(frame["amount"], dtype=float)
    disc = np.asarray(spot.discount(times), dtype=float)
    expected = float(np.dot(amounts, disc * (np.exp(-1e-4 * times) - 1.0)))
    assert spot_dv01(bond, settle, spot) == pytest.approx(expected, rel=1e-12)


# ---- R17 日期/日历契约 ------------------------------------------------
def test_krd_contract_requires_business_day_settlement() -> None:
    """结算日必须是工作日：周六/周日结算直接报错。"""
    keys = [float(t) for t in get_config().risk.key_tenors_krd]
    for weekend in (date(2026, 5, 30), date(2026, 5, 31)):  # 周六 / 周日
        spot = SyntheticSpotCurve(
            keys, [1.8 + 0.05 * t for t in keys], observation_date=weekend, settlement_date=weekend
        )
        with pytest.raises(ValueError, match="不是工作日"):
            key_rate_durations(make_bond(), weekend, spot)


def test_krd_contract_settlement_mismatch_raises_or_allows_with_warning() -> None:
    """结算日与 spot.settlement_date 不一致：默认报错；allow_mismatch=True 降级 warning。"""
    bond = make_bond()
    curve_settle, settle = date(2026, 5, 28), date(2026, 5, 29)  # 5/29 周五，晚于参考日
    spot = make_spot(curve_settle)
    with pytest.raises(ValueError, match="不一致"):
        key_rate_durations(bond, settle, spot)
    with pytest.warns(UserWarning, match="allow_mismatch"):
        krd = key_rate_durations(bond, settle, spot, allow_mismatch=True)
    assert sum(krd.values()) == pytest.approx(spot_dv01(bond, settle, spot), rel=1e-3)


def test_krd_contract_settlement_before_observation_raises() -> None:
    """结算日不能早于曲线参考日（observation_date）。"""
    bond = make_bond()
    reference = date(2026, 5, 29)
    spot = SyntheticSpotCurve(
        [float(t) for t in get_config().risk.key_tenors_krd],
        [1.8 + 0.05 * float(t) for t in get_config().risk.key_tenors_krd],
        observation_date=reference,
        settlement_date=reference,
    )
    with pytest.raises(ValueError, match="早于曲线参考日"):
        key_rate_durations(bond, date(2026, 5, 28), spot)


def test_krd_contract_fallback_to_as_of_warns() -> None:
    """spot 缺少 settlement_date：回退 observation_date/as_of 校验并 warning（不静默）。"""
    settle = date(2026, 5, 28)
    spot = SyntheticSpotCurve(
        [float(t) for t in get_config().risk.key_tenors_krd],
        [1.8 + 0.05 * float(t) for t in get_config().risk.key_tenors_krd],
    )
    spot.as_of = settle
    with pytest.warns(UserWarning, match="settlement_date"):
        krd = key_rate_durations(make_bond(), settle, spot)
    assert sum(krd.values()) == pytest.approx(spot_dv01(make_bond(), settle, spot), rel=1e-3)


@pytest.mark.parametrize(
    "observation",
    [date(2026, 11, 6), date(2026, 11, 7), date(2026, 11, 8)],  # 周五 / 周六 / 周日
)
def test_krd_contract_weekend_observation_monday_settlement(observation: date) -> None:
    """周五/周六/周日观察日 + 周一结算：结算日工作日且 ≥ 参考日 → 契约通过。"""
    settle = date(2026, 11, 9)  # 周一
    keys = [float(t) for t in get_config().risk.key_tenors_krd]
    spot = SyntheticSpotCurve(
        keys,
        [1.8 + 0.05 * t for t in keys],
        observation_date=observation,
        settlement_date=settle,
    )
    bond = make_bond()
    krd = key_rate_durations(bond, settle, spot)
    assert sum(krd.values()) == pytest.approx(spot_dv01(bond, settle, spot), rel=1e-3)


def test_krd_contract_coupon_boundary_and_leap_day() -> None:
    """付息日前后（2026-05-15 周五、2026-11-16 周一）与闰年 2024-02-29 均可定价。"""
    bond = make_bond()
    for settle in (date(2026, 5, 15), date(2026, 11, 16)):
        spot = make_spot(settle)
        krd = key_rate_durations(bond, settle, spot)
        assert sum(krd.values()) == pytest.approx(spot_dv01(bond, settle, spot), rel=1e-3)

    leap_day = date(2024, 2, 29)  # 周四，工作日
    leap_bond = make_bond(
        code="SYN-LEAP.IB", issue_date=date(2023, 11, 15), maturity_date=date(2025, 11, 15)
    )
    leap_spot = make_spot(leap_day)
    krd = key_rate_durations(leap_bond, leap_day, leap_spot)
    assert sum(krd.values()) == pytest.approx(spot_dv01(leap_bond, leap_day, leap_spot), rel=1e-3)
