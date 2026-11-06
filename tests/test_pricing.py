"""现金流与价格测试：memo 自算算例 A/B/C 回归 + 公式等价性 + 边界。

黄金数字来自 ``docs/00_market_conventions.md`` §2.1 / §2.3 / §2.4 / §2.5；
收益率一律为百分数，金额单位：元/百元面值。
"""

from __future__ import annotations

from datetime import date

import pytest

from ficc.config import get_config
from ficc.data.schema import BondStatic
from ficc.pricing.bond import (
    CF_COLUMNS,
    cashflows,
    clean_price_from_ytm,
    dirty_price_from_ytm,
    ytm_from_clean_price,
    ytm_from_dirty_price,
)
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


# ---------------------------------------------------------------------------
# 现金流表
# ---------------------------------------------------------------------------
def test_cashflows_official_A_schedule() -> None:
    """memo 自算算例 A：20 次付息，末期含本金，t_years 与折现指数一致。"""
    bond = make_bond()
    cf = cashflows(bond, date(2026, 5, 28))

    assert list(cf.columns) == list(CF_COLUMNS)
    assert len(cf) == 20
    assert list(cf["period_index"]) == list(range(1, 21))
    assert cf["date"].iloc[0] == date(2026, 11, 15)
    assert cf["date"].iloc[-1] == date(2036, 5, 15)
    # 每期票息 = C/f = 0.86；末期加本金 100
    assert cf["amount"].iloc[0] == pytest.approx(0.86, rel=1e-12)
    assert cf["amount"].iloc[-1] == pytest.approx(100.86, rel=1e-12)
    assert cf["t_years"].is_monotonic_increasing
    assert cf["t_years"].iloc[0] == pytest.approx((0 + 171 / 184) / 2, rel=1e-12)
    assert cf["t_years"].iloc[-1] == pytest.approx((19 + 171 / 184) / 2, rel=1e-12)


# ---------------------------------------------------------------------------
# memo 自算算例 A：26 附息国债 10（2026-05-28，y=1.72%）
# ---------------------------------------------------------------------------
def test_official_A_dirty_price(near_par_bond: BondStatic) -> None:
    assert dirty_price_from_ytm(near_par_bond, date(2026, 5, 28), 1.72) == pytest.approx(
        100.060519, abs=1e-4
    )


def test_official_A_clean_price(near_par_bond: BondStatic) -> None:
    settle = date(2026, 5, 28)
    clean = clean_price_from_ytm(near_par_bond, settle, 1.72)
    assert clean == pytest.approx(99.999759, abs=1e-4)
    # 净价 = 全价 − AI（三者关系必须严格成立）
    ai = accrued_interest(near_par_bond, settle)
    assert ai == pytest.approx(0.060761, abs=1e-4)
    assert clean == pytest.approx(dirty_price_from_ytm(near_par_bond, settle, 1.72) - ai, rel=1e-12)


def test_official_A_reverse_ytm_from_clean_100(near_par_bond: BondStatic) -> None:
    """给定净价 100.00 反解 y = 1.719974%。

    与 1.72% 的差（约 0.0026bp < 0.01bp）来自**净价从 99.999759 取整到 100**，
    不是求根算法的数值误差（brentq 收敛误差远小于 1e-9）。
    """
    settle = date(2026, 5, 28)
    ytm = ytm_from_clean_price(near_par_bond, settle, 100.0)
    assert ytm == pytest.approx(1.719974, abs=1e-5)
    assert abs(ytm - 1.72) < 0.0001  # 0.01bp = 0.0001 个百分点


# ---------------------------------------------------------------------------
# memo 自算算例 B：同一只券在 2026-11-04，y=1.61%
# ---------------------------------------------------------------------------
def test_official_B_prices(near_par_bond: BondStatic) -> None:
    settle = date(2026, 11, 4)
    ai = accrued_interest(near_par_bond, settle)
    dirty = dirty_price_from_ytm(near_par_bond, settle, 1.61)
    clean = clean_price_from_ytm(near_par_bond, settle, 1.61)

    assert ai == pytest.approx(0.8086, abs=1e-4)
    assert dirty == pytest.approx(101.7766, abs=1e-4)
    assert clean == pytest.approx(100.9680, abs=1e-4)
    assert clean == pytest.approx(dirty - ai, rel=1e-12)


def test_dirty_and_clean_ytm_round_trip(near_par_bond: BondStatic) -> None:
    settle = date(2026, 11, 4)
    dirty = dirty_price_from_ytm(near_par_bond, settle, 1.61)
    clean = clean_price_from_ytm(near_par_bond, settle, 1.61)
    assert ytm_from_dirty_price(near_par_bond, settle, dirty) == pytest.approx(1.61, abs=1e-9)
    assert ytm_from_clean_price(near_par_bond, settle, clean) == pytest.approx(1.61, abs=1e-9)


# ---------------------------------------------------------------------------
# 利随本清 / 贴现分支
# ---------------------------------------------------------------------------
def test_lump_sum_1y_matches_coupon_formula() -> None:
    """memo §2.5：1Y 利随本清与规则附息公式（f=1、TS=TY=365）数值等价。"""
    settle = date(2026, 11, 5)
    lump_sum = make_bond(
        code="260001.IB",
        issue_date=date(2026, 1, 15),
        maturity_date=date(2027, 1, 15),
        coupon_pct=1.22,
        freq=1,
        accrual="lump_sum",
    )
    coupon_form = make_bond(
        code="260001-C.IB",
        issue_date=date(2026, 1, 15),
        maturity_date=date(2027, 1, 15),
        coupon_pct=1.22,
        freq=1,
        accrual="coupon",
    )
    assert dirty_price_from_ytm(lump_sum, settle, 1.5) == pytest.approx(
        dirty_price_from_ytm(coupon_form, settle, 1.5), rel=1e-12
    )

    cf = cashflows(lump_sum, settle)
    assert len(cf) == 1
    assert cf["date"].iloc[0] == date(2027, 1, 15)
    assert cf["amount"].iloc[0] == pytest.approx(100.0 + 1.22, rel=1e-12)


def test_lump_sum_multi_year_coupon_amount() -> None:
    """2Y 利随本清：到期现金流 = 100 + 2×C（按完整年数计）。"""
    bond = make_bond(
        code="SYN-LS-2Y.IB",
        issue_date=date(2024, 2, 29),
        maturity_date=date(2026, 2, 28),
        coupon_pct=1.5,
        freq=1,
        accrual="lump_sum",
    )
    cf = cashflows(bond, date(2025, 6, 30))
    assert len(cf) == 1
    assert cf["amount"].iloc[0] == pytest.approx(100.0 + 3.0, rel=1e-12)


def test_discount_bond_uses_money_market_formula() -> None:
    """贴现/零息：PV = 100 / (1 + y×τ/365)（货币市场单利）。"""
    bond = make_bond(
        code="SYN-DISC.IB",
        issue_date=date(2026, 1, 15),
        maturity_date=date(2026, 4, 16),
        coupon_pct=0.0,
        freq=1,
        accrual="discount",
    )
    settle = date(2026, 2, 14)
    tau = day_count(settle, bond.maturity_date)
    assert dirty_price_from_ytm(bond, settle, 1.8) == pytest.approx(
        100.0 / (1.0 + 0.018 * tau / 365.0), rel=1e-12
    )


# ---------------------------------------------------------------------------
# 边界与单调性
# ---------------------------------------------------------------------------
def test_price_decreases_with_yield(near_par_bond: BondStatic) -> None:
    settle = date(2026, 5, 28)
    assert (
        dirty_price_from_ytm(near_par_bond, settle, 1.5)
        > dirty_price_from_ytm(near_par_bond, settle, 1.72)
        > dirty_price_from_ytm(near_par_bond, settle, 2.0)
    )


def test_price_on_coupon_date_clean_equals_dirty(near_par_bond: BondStatic) -> None:
    """恰在付息日结算：AI = 0，故净价 = 全价。"""
    settle = date(2026, 11, 15)
    dirty = dirty_price_from_ytm(near_par_bond, settle, 1.72)
    clean = clean_price_from_ytm(near_par_bond, settle, 1.72)
    assert accrued_interest(near_par_bond, settle) == pytest.approx(0.0, abs=1e-15)
    assert clean == pytest.approx(dirty, rel=1e-12)


def test_settlement_out_of_range_raises(near_par_bond: BondStatic) -> None:
    with pytest.raises(ValueError):
        dirty_price_from_ytm(near_par_bond, date(2026, 5, 14), 1.72)  # 早于起息日
    with pytest.raises(ValueError):
        cashflows(near_par_bond, near_par_bond.maturity_date)  # 等于到期日无未来现金流


def test_ytm_solver_raises_when_unreachable(near_par_bond: BondStatic) -> None:
    """目标全价 0.01 元不可能由 [−5%, 30%] 内的收益率得到。"""
    with pytest.raises(ValueError):
        ytm_from_dirty_price(near_par_bond, date(2026, 5, 28), 0.01)


# ---------------------------------------------------------------------------
# 配置显式透传（R07 定价侧）
# ---------------------------------------------------------------------------
def test_quote_face_value_from_config_threads_through_pricing(near_par_bond: BondStatic) -> None:
    """报价面值来自 config 且可显式覆盖：face=200 时全部金额按 200 元面值计价。

    默认 config.pricing.face_value=100，故不影响 memo 自算算例的黄金数字；
    这里只验证底层不回落硬编码 100（R07 定价侧）。
    """
    cfg = get_config()
    custom = cfg.model_copy(
        update={"pricing": cfg.pricing.model_copy(update={"face_value": 200.0})}
    )
    settle, ytm = date(2026, 5, 28), 1.72
    assert dirty_price_from_ytm(near_par_bond, settle, ytm, cfg=custom) == pytest.approx(
        2.0 * dirty_price_from_ytm(near_par_bond, settle, ytm), rel=1e-12
    )
    assert accrued_interest(near_par_bond, settle, cfg=custom) == pytest.approx(
        2.0 * accrued_interest(near_par_bond, settle), rel=1e-12
    )
    cf = cashflows(near_par_bond, settle, cfg=custom)
    assert cf["amount"].iloc[-1] == pytest.approx(200.0 + 1.72, rel=1e-12)
