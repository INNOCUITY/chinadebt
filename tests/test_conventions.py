"""计息与日历惯例测试。

覆盖：ACT/ACT vs ACT/365（含 2024-02-29）、付息日生成（半年付/年付/利随本清/
月末锚定）、应计利息三分支（memo 自算算例 A/C）、结算日校验与 T+N 工作日推算、
配置显式透传（R07 定价侧）。

黄金数字来自 ``docs/00_market_conventions.md`` §2.2 / §2.3 / §2.5（memo 自算
算例，非独立外部对账；外部公告配对见 ``tests/test_pricing_official.py``）。
金额单位：元/报价面值（默认元/百元面值）。
"""

from __future__ import annotations

import itertools
from datetime import date

import pytest

from ficc.config import Config, get_config
from ficc.data.schema import BondStatic
from ficc.pricing.conventions import (
    ConventionError,
    accrued_interest,
    annual_anniversaries,
    coupon_dates,
    coupon_period,
    day_count,
    days_in_year,
    is_leap_year,
    is_on_coupon_date,
    next_coupon_date,
    pricing_config,
    quote_face_value,
    settlement_date,
    validate_settlement,
    year_fraction,
)


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
    """构造债券静态条款（默认即 memo 自算算例 A 的 26 附息国债 10）。"""
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
# 闰年与年化天数
# ---------------------------------------------------------------------------
def test_is_leap_year_and_days_in_year() -> None:
    assert is_leap_year(2024) is True
    assert is_leap_year(2023) is False
    assert is_leap_year(2000) is True
    assert is_leap_year(1900) is False  # 整百年需能被 400 整除
    assert days_in_year(2024) == 366
    assert days_in_year(2023) == 365


def test_year_fraction_act_act_includes_feb29() -> None:
    """2024-01-01 → 2024-03-01 共 60 天：ACT/ACT 含 2/29，ACT/365 不含。"""
    start, end = date(2024, 1, 1), date(2024, 3, 1)
    assert day_count(start, end) == 60
    assert year_fraction(start, end, "ACT/ACT") == pytest.approx(60 / 366, rel=1e-12)
    assert year_fraction(start, end, "ACT/365") == pytest.approx(59 / 365, rel=1e-12)
    assert year_fraction(start, end, "ACT/ACT") > year_fraction(start, end, "ACT/365")


def test_year_fraction_act_act_splits_by_calendar_year() -> None:
    """跨年时按自然年分段：2023-12-31 → 2024-03-01。"""
    start, end = date(2023, 12, 31), date(2024, 3, 1)
    assert day_count(start, end) == 61
    expected_act_act = 1 / 365 + 60 / 366  # 2023 段 1 天 + 2024 段 60 天
    assert year_fraction(start, end, "ACT/ACT") == pytest.approx(expected_act_act, rel=1e-12)
    assert year_fraction(start, end, "ACT/365") == pytest.approx(60 / 365, rel=1e-12)


def test_year_fraction_zero_and_reversed() -> None:
    d = date(2024, 2, 29)
    assert year_fraction(d, d) == 0.0
    with pytest.raises(ConventionError):
        year_fraction(date(2024, 3, 1), date(2024, 1, 1), "ACT/ACT")
    with pytest.raises(ConventionError):
        year_fraction(date(2024, 1, 1), date(2024, 3, 1), "30/360")  # type: ignore[arg-type]


def test_act365_excludes_feb29_in_day_count() -> None:
    """2024-02-28 → 2024-03-01 实际 2 天，其中 1 天是 2/29。"""
    start, end = date(2024, 2, 28), date(2024, 3, 1)
    assert day_count(start, end) == 2
    assert year_fraction(start, end, "ACT/365") == pytest.approx(1 / 365, rel=1e-12)


# ---------------------------------------------------------------------------
# 付息日序列
# ---------------------------------------------------------------------------
def test_coupon_dates_semi_annual_10y() -> None:
    """memo 自算算例 A：10Y 半年付，含起息日共 21 个节点、20 次付息。"""
    dates = coupon_dates(make_bond())
    assert len(dates) == 21
    assert len(dates) - 1 == 20  # memo 算例「付息日数 20」
    assert dates[0] == date(2026, 5, 15)
    assert dates[1] == date(2026, 11, 15)
    assert dates[-1] == date(2036, 5, 15)
    assert all(a < b for a, b in itertools.pairwise(dates))
    assert all(d.day == 15 for d in dates)


def test_coupon_dates_annual() -> None:
    """7Y 年付：起息 2026-05-15、到期 2033-05-15 → 7 次付息。"""
    bond = make_bond(
        code="260007.IB",
        maturity_date=date(2033, 5, 15),
        coupon_pct=1.66,
        freq=1,
    )
    dates = coupon_dates(bond)
    assert len(dates) == 8
    assert dates[1] == date(2027, 5, 15)
    assert dates[-1] == date(2033, 5, 15)


def test_coupon_dates_lump_sum_only_two_nodes() -> None:
    """利随本清（1Y）：唯一一次现金流在到期日，付息日序列只有起息日与到期日。"""
    bond = make_bond(
        code="260001.IB",
        issue_date=date(2026, 1, 15),
        maturity_date=date(2027, 1, 15),
        coupon_pct=1.22,
        freq=1,
        accrual="lump_sum",
    )
    assert coupon_dates(bond) == [date(2026, 1, 15), date(2027, 1, 15)]


def test_coupon_dates_month_end_anchor() -> None:
    """起息日为月末（2/29）时，半年付付息日锚定到对应月份最后一天。"""
    bond = make_bond(
        code="SYN-EOM.IB",
        issue_date=date(2024, 2, 29),
        maturity_date=date(2026, 2, 28),
        coupon_pct=2.0,
        freq=2,
    )
    assert coupon_dates(bond) == [
        date(2024, 2, 29),
        date(2024, 8, 31),
        date(2025, 2, 28),
        date(2025, 8, 31),
        date(2026, 2, 28),
    ]


# ---------------------------------------------------------------------------
# 计息周期（memo 自算算例 A）
# ---------------------------------------------------------------------------
def test_coupon_period_official_A() -> None:
    """TS=184、T=171、w=0.929348（结算日 2026-05-28）。"""
    period = coupon_period(make_bond(), date(2026, 5, 28))
    assert period.prev_coupon == date(2026, 5, 15)
    assert period.next_coupon == date(2026, 11, 15)
    assert period.ts_days == 184
    assert period.t_days == 13  # 2026-05-15 → 2026-05-28（算头不算尾）
    assert period.t_remaining_days == 171  # 结算日 → **下一个**付息日
    assert period.w == pytest.approx(171 / 184, rel=1e-12)
    assert period.w == pytest.approx(0.929348, abs=1e-6)


# ---------------------------------------------------------------------------
# 应计利息（三分支）
# ---------------------------------------------------------------------------
def test_accrued_interest_official_A() -> None:
    """memo 自算算例 A：AI = 1.72%/2 × 13/184 = 0.060761 元。"""
    bond = make_bond()
    ai = accrued_interest(bond, date(2026, 5, 28))
    assert ai == pytest.approx(0.060761, abs=1e-4)
    assert ai == pytest.approx(1.72 / 2 * 13 / 184, rel=1e-12)


def test_accrued_interest_official_B() -> None:
    """memo 自算算例 B：2026-11-04 的 AI = 1.72%/2 × 173/184 = 0.8086 元。"""
    bond = make_bond()
    ai = accrued_interest(bond, date(2026, 11, 4))
    assert ai == pytest.approx(0.8086, abs=1e-4)
    assert ai == pytest.approx(0.86 * 173 / 184, rel=1e-12)


def test_accrued_interest_zero_on_issue_and_coupon_date() -> None:
    bond = make_bond()
    assert accrued_interest(bond, date(2026, 5, 15)) == pytest.approx(0.0, abs=1e-15)
    # 付息日当天 AI = 0（算头不算尾，新一轮计息从付息日开始）
    assert accrued_interest(bond, date(2026, 11, 15)) == pytest.approx(0.0, abs=1e-15)


def test_accrued_interest_at_maturity_is_final_coupon() -> None:
    """到期日仍有最后一期票息未兑付：AI = C/f = 0.86 元。"""
    assert accrued_interest(make_bond(), date(2036, 5, 15)) == pytest.approx(0.86, rel=1e-12)


def test_accrued_interest_lump_sum_official_C() -> None:
    """memo 自算算例 C：26 附息国债 1，AI = 1.22% × 294/365 = 0.9827 元。"""
    bond = make_bond(
        code="260001.IB",
        issue_date=date(2026, 1, 15),
        maturity_date=date(2027, 1, 15),
        coupon_pct=1.22,
        freq=1,
        accrual="lump_sum",
    )
    ai = accrued_interest(bond, date(2026, 11, 5))
    assert ai == pytest.approx(0.9827, abs=1e-4)
    assert ai == pytest.approx(1.22 * 294 / 365, rel=1e-12)


def test_accrued_interest_lump_sum_multi_year_crosses_leap() -> None:
    """利随本清跨年：K×C + C×t/TY，K=整年数、TY=当前计息年度实际天数。"""
    bond = make_bond(
        code="SYN-LS-2Y.IB",
        issue_date=date(2024, 2, 29),
        maturity_date=date(2026, 2, 28),
        coupon_pct=1.5,
        freq=1,
        accrual="lump_sum",
    )
    assert annual_anniversaries(bond) == [
        date(2024, 2, 29),
        date(2025, 2, 28),
        date(2026, 2, 28),
    ]
    settle = date(2025, 6, 30)
    t_days = day_count(date(2025, 2, 28), settle)  # 122 天
    ty_days = day_count(date(2025, 2, 28), date(2026, 2, 28))  # 365 天
    expected = 1.0 * 1.5 + 1.5 * t_days / ty_days
    assert accrued_interest(bond, settle) == pytest.approx(expected, rel=1e-12)
    # 到期日：累计整年票息（1Y 券即 C，2Y 券即 2C）
    assert accrued_interest(bond, date(2026, 2, 28)) == pytest.approx(3.0, rel=1e-12)


def test_accrued_interest_discount_branch() -> None:
    """贴现分支：AI = (100 − Pd) × t/T（折价线性摊销）。"""
    bond = make_bond(
        code="SYN-DISC.IB",
        issue_date=date(2026, 1, 15),
        maturity_date=date(2026, 4, 16),
        coupon_pct=0.0,
        freq=1,
        accrual="discount",
    )
    # 91 天存续期，结算 2026-02-14 已过 30 天，发行价 99.0 → 折价 1 元按天摊销
    ai = accrued_interest(bond, date(2026, 2, 14), issue_price=99.0)
    assert ai == pytest.approx((100.0 - 99.0) * 30 / 91, rel=1e-12)
    assert accrued_interest(bond, bond.issue_date, issue_price=99.0) == pytest.approx(0.0)
    assert accrued_interest(bond, bond.maturity_date, issue_price=99.0) == pytest.approx(1.0)


def test_accrued_interest_discount_requires_issue_price() -> None:
    bond = make_bond(
        code="SYN-DISC.IB",
        issue_date=date(2026, 1, 15),
        maturity_date=date(2026, 4, 16),
        coupon_pct=0.0,
        freq=1,
        accrual="discount",
    )
    with pytest.raises(ConventionError):
        accrued_interest(bond, date(2026, 2, 14))
    with pytest.raises(ConventionError):
        accrued_interest(bond, date(2026, 2, 14), issue_price=101.0)


def test_accrued_interest_exchange_basis_differs_from_ib() -> None:
    """同一只券跨市场：交易所 ACT/365 剔除 2/29，AI 比银行间少一天利息。"""
    issue, maturity = date(2023, 11, 15), date(2025, 11, 15)
    ib = make_bond(code="X.IB", issue_date=issue, maturity_date=maturity, market="IB")
    ex = make_bond(code="X.EX", issue_date=issue, maturity_date=maturity, market="EX")
    settle = date(2024, 3, 1)  # 计息周期 2023-11-15 → 2024-05-15 跨 2/29

    prev, nxt = date(2023, 11, 15), date(2024, 5, 15)
    t_days, ts_days = day_count(prev, settle), day_count(prev, nxt)
    assert accrued_interest(ib, settle) == pytest.approx(0.86 * t_days / ts_days, rel=1e-12)
    # 交易所：t 与 TS 同时剔除 2/29
    assert accrued_interest(ex, settle) == pytest.approx(
        0.86 * (t_days - 1) / (ts_days - 1), rel=1e-12
    )
    assert accrued_interest(ex, settle) < accrued_interest(ib, settle)


# ---------------------------------------------------------------------------
# 付息日查询与结算日校验
# ---------------------------------------------------------------------------
def test_next_coupon_date_and_is_on_coupon_date() -> None:
    bond = make_bond()
    assert next_coupon_date(bond, date(2026, 5, 15)) == date(2026, 11, 15)
    assert next_coupon_date(bond, date(2026, 5, 28)) == date(2026, 11, 15)
    # 恰在付息日：返回**下一个**付息日（不含当日）
    assert next_coupon_date(bond, date(2026, 11, 15)) == date(2027, 5, 15)
    assert is_on_coupon_date(bond, date(2026, 11, 15)) is True
    assert is_on_coupon_date(bond, date(2026, 5, 15)) is False  # 起息日不算付息日
    assert is_on_coupon_date(bond, date(2026, 5, 28)) is False


def test_validate_settlement_range() -> None:
    bond = make_bond()
    with pytest.raises(ConventionError):
        validate_settlement(bond, date(2026, 5, 14))  # 早于起息日
    with pytest.raises(ConventionError):
        validate_settlement(bond, bond.maturity_date)  # 定价不允许等于到期日
    validate_settlement(bond, bond.maturity_date, allow_maturity=True)  # 应计利息允许
    with pytest.raises(ConventionError):
        validate_settlement(bond, date(2036, 5, 16))  # 晚于到期日


def pricing_cfg_with(**pricing_updates: object) -> Config:
    """复制全局配置并覆盖 ``pricing`` 下的若干字段（用于验证 cfg 显式透传）。"""
    cfg = get_config()
    pricing = cfg.pricing.model_copy(update=pricing_updates)
    return cfg.model_copy(update={"pricing": pricing})


def test_settlement_date_business_day_matrix() -> None:
    """T+0/1/2/3 × {周一, 周三, 周五, 周六, 周日} 的工作日计数矩阵。

    规则：周末成交先顺延到下一工作日，再逐个工作日推进。修复前实现
    （先加自然日、只修终点）在周五 T+2 会错返回周一 11-09，正确为周二 11-10。
    """
    matrix = {
        # 2026-11-02 周一
        (date(2026, 11, 2), 0): date(2026, 11, 2),
        (date(2026, 11, 2), 1): date(2026, 11, 3),
        (date(2026, 11, 2), 2): date(2026, 11, 4),
        (date(2026, 11, 2), 3): date(2026, 11, 5),
        # 2026-11-04 周三
        (date(2026, 11, 4), 0): date(2026, 11, 4),
        (date(2026, 11, 4), 1): date(2026, 11, 5),
        (date(2026, 11, 4), 2): date(2026, 11, 6),
        (date(2026, 11, 4), 3): date(2026, 11, 9),
        # 2026-11-06 周五（R11 反例：T+2 必须是 11-10 周二）
        (date(2026, 11, 6), 0): date(2026, 11, 6),
        (date(2026, 11, 6), 1): date(2026, 11, 9),
        (date(2026, 11, 6), 2): date(2026, 11, 10),
        (date(2026, 11, 6), 3): date(2026, 11, 11),
        # 2026-11-07 周六：先顺延到周一 11-09 再计数
        (date(2026, 11, 7), 0): date(2026, 11, 9),
        (date(2026, 11, 7), 1): date(2026, 11, 10),
        (date(2026, 11, 7), 2): date(2026, 11, 11),
        (date(2026, 11, 7), 3): date(2026, 11, 12),
        # 2026-11-08 周日：先顺延到周一 11-09 再计数
        (date(2026, 11, 8), 0): date(2026, 11, 9),
        (date(2026, 11, 8), 1): date(2026, 11, 10),
        (date(2026, 11, 8), 2): date(2026, 11, 11),
        (date(2026, 11, 8), 3): date(2026, 11, 12),
    }
    for (trade_date, lag_days), expected in matrix.items():
        assert settlement_date(trade_date, lag_days) == expected, (
            f"{trade_date}({trade_date:%a}) T+{lag_days}"
        )


def test_settlement_date_r11_regression() -> None:
    """R11 反例：周五 2026-11-06 的 T+2/T+3 必须分别是 11-10/11-11（不是 11-09）。"""
    friday = date(2026, 11, 6)
    assert settlement_date(friday, 2) == date(2026, 11, 10)
    assert settlement_date(friday, 3) == date(2026, 11, 11)
    with pytest.raises(ConventionError):
        settlement_date(friday, -1)


def test_settlement_lag_from_config_and_cfg_override() -> None:
    """``lag_days=None`` 时从 ``config.pricing.settlement_lag_days`` 取默认值；
    显式 ``cfg`` 优先，不回落硬编码默认（R07 定价侧）。"""
    friday = date(2026, 11, 6)
    assert pricing_config().settlement_lag_days == 1
    assert settlement_date(friday) == date(2026, 11, 9)  # 配置默认 T+1
    # 显式 lag 覆盖配置
    assert settlement_date(friday, 2) == date(2026, 11, 10)
    # 自定义 cfg：T+2 必须生效，不能回落默认 1
    cfg_t2 = pricing_cfg_with(settlement_lag_days=2)
    assert settlement_date(friday, cfg=cfg_t2) == date(2026, 11, 10)
    assert settlement_date(friday, 0, cfg=cfg_t2) == date(2026, 11, 6)


def test_cfg_threading_day_count_not_fallback() -> None:
    """自定义 cfg 把银行间基准改成 ACT/365 时，应计利息必须跟随（不回落默认）。"""
    issue, maturity = date(2023, 11, 15), date(2025, 11, 15)
    ib = make_bond(code="X.IB", issue_date=issue, maturity_date=maturity, market="IB")
    ex = make_bond(code="X.EX", issue_date=issue, maturity_date=maturity, market="EX")
    settle = date(2024, 3, 1)  # 周期跨 2/29

    cfg_act365 = pricing_cfg_with(day_count="ACT/365")
    assert accrued_interest(ib, settle, cfg=cfg_act365) == pytest.approx(
        accrued_interest(ex, settle), rel=1e-12
    )
    assert accrued_interest(ib, settle, cfg=cfg_act365) != pytest.approx(
        accrued_interest(ib, settle), rel=1e-6
    )


def test_quote_face_value_from_config() -> None:
    """报价面值来自 ``config.pricing.face_value``（默认 100），可显式覆盖。"""
    assert quote_face_value() == pytest.approx(get_config().pricing.face_value)
    cfg_200 = pricing_cfg_with(face_value=200.0)
    assert quote_face_value(cfg=cfg_200) == pytest.approx(200.0)
