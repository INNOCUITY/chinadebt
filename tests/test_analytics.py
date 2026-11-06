"""风险指标测试：memo 自算算例 A/B/D 回归 + 解析式与有限差分交叉校验。

黄金数字来自 ``docs/00_market_conventions.md`` §3.2；收益率百分数，金额元/百元面值。

注意（memo 自算算例 D 的结算日）：memo 只给了「30Y、票面 2.38%、半年付、y≈2.04% 时修正久期
≈ 21.18、DV01 ≈ 0.2291」的量级值，未给该券的起息日与结算日。本测试采用一组合理
假设——起息 2026-01-15、到期 2056-01-15、结算 2026-11-04——实测 modD=21.1758、
DV01=0.229068、凸性=555.40，与 memo 数字在 1e-2 / 1e-4 / 1.0 容差内。若换成别的
结算日，结果会按实际天数小幅漂移，故容差按 memo 明示的 1e-2 / 1e-4 设置。
"""

from __future__ import annotations

from datetime import date

import pytest

from ficc.data.schema import BondStatic
from ficc.pricing.analytics import (
    convexity,
    dv01,
    dv01_finite_diff,
    key_rate_durations,
    macaulay_duration,
    mod_duration,
)
from ficc.pricing.bond import dirty_price_from_ytm
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
# KRD：明确未实现
# ---------------------------------------------------------------------------
def test_key_rate_durations_not_implemented() -> None:
    """KRD 依赖 ficc.curve 的即期曲线 bootstrap，定价层显式留白（不糊弄）。"""
    with pytest.raises(NotImplementedError, match="bootstrap"):
        key_rate_durations(
            make_bond(),
            date(2026, 5, 28),
            curve_tenors=[1.0, 2.0, 10.0],
            curve_ytm_pct=[1.2, 1.3, 1.72],
        )
