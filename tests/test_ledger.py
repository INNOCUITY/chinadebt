"""固定券账本测试（Phase 2 第 1 步，task-15）。

覆盖 10 类场景：无交易持有、跨付息、闰年/节假日、部分减仓、到期兑付、
融资利率变动、零成本对照、三层日期报错、未结算不计现金、条款锁定与老化。

所有测试用实测中债估值日构造的真实 CN_IB 日历（data/processed/curve_long.parquet）
与合成 YTM 曲线；金额单位元，价格元/百元面值。
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from ficc.config import Config, get_config
from ficc.curve.curve import Curve
from ficc.data.schema import BondStatic
from ficc.portfolio import (
    CASHFLOWS_COLUMNS,
    SNAPSHOTS_COLUMNS,
    TRADES_COLUMNS,
    Ledger,
    TradeRecord,
)
from ficc.pricing.bond import dirty_price_from_ytm
from ficc.pricing.conventions import accrued_interest, day_count
from ficc.utils.calendar import TradingCalendar

TENORS = np.array([0.25, 0.5, 1.0, 2.0, 3.0, 5.0, 7.0, 10.0, 15.0, 20.0, 30.0])


# ---------------------------------------------------------------------------
# 夹具与工具
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def calendar() -> TradingCalendar:
    path = Path(__file__).resolve().parents[1] / "data" / "processed" / "curve_long.parquet"
    if not path.exists():
        pytest.skip(f"缺少中债实测数据 {path}，无法构造 CN_IB 日历")
    frame = pd.read_parquet(path)
    return TradingCalendar.from_dates(pd.to_datetime(frame["date"]).dt.date)


def flat_curve(as_of: date, level: float = 2.0) -> Curve:
    """平坦 YTM 曲线（2% 附近），用于可手算的估值。"""
    return Curve(as_of=as_of, tenors=TENORS, rates=np.full_like(TENORS, level), method="linear")


def zero_cost_cfg() -> Config:
    """零成本配置：手续费/结算费/融资均为 0（零成本对照用）。"""
    cfg = get_config()
    costs = cfg.costs.model_copy(update={"fee_bp": 0.0, "settlement_fee_cny_per_trade": 0.0})
    return cfg.model_copy(update={"costs": costs})


def cfg_with_lag(lag_days: int, *, zero_cost: bool = False) -> Config:
    cfg = zero_cost_cfg() if zero_cost else get_config()
    pricing = cfg.pricing.model_copy(update={"settlement_lag_days": lag_days})
    return cfg.model_copy(update={"pricing": pricing})


def make_bond(
    code: str, issue: date, maturity: date, coupon_pct: float = 2.0, freq: int = 2
) -> BondStatic:
    return BondStatic(
        code=code, issue_date=issue, maturity_date=maturity, coupon_pct=coupon_pct, freq=freq
    )


def make_trade(
    calendar: TradingCalendar,
    cfg: Config,
    *,
    trade_id: str,
    bond: BondStatic,
    trade_date: date,
    signal_date: date | None = None,
    side: str = "buy",
    face_value: float = 1_000_000.0,
    clean_price: float = 100.0,
    fee: float = 0.0,
    settlement_date: date | None = None,
) -> TradeRecord:
    signal = signal_date if signal_date is not None else trade_date - timedelta(days=3)
    settlement = (
        settlement_date
        if settlement_date is not None
        else calendar.settlement_date(trade_date, int(cfg.pricing.settlement_lag_days))
    )
    return TradeRecord(
        trade_id=trade_id,
        bond=bond,
        side=side,  # type: ignore[arg-type]
        face_value=face_value,
        clean_price=clean_price,
        signal_date=signal,
        trade_date=trade_date,
        settlement_date=settlement,
        fee=fee,
    )


def manual_dirty(bond: BondStatic, as_of: date, level: float = 2.0) -> float:
    return dirty_price_from_ytm(bond, as_of, level)


def process(ledger: Ledger, dates: list[date], level: float = 2.0) -> list:
    return [ledger.process_day(day, flat_curve(day, level)) for day in dates]


# ---------------------------------------------------------------------------
# 1. 无交易持有：NAV 变化 = 全价变动（票息应计）
# ---------------------------------------------------------------------------
def test_hold_nav_change_equals_dirty_price_change(calendar: TradingCalendar) -> None:
    cfg = zero_cost_cfg()
    bond = make_bond("T26", date(2024, 6, 15), date(2026, 6, 15))
    trade = make_trade(calendar, cfg, trade_id="t1", bond=bond, trade_date=date(2024, 6, 17))
    ledger = Ledger(calendar, initial_cash=2_000_000.0, funding_rate_pct=0.0, cfg=cfg)
    ledger.submit(trade)
    snap18, snap20 = process(ledger, [date(2024, 6, 17), date(2024, 6, 18), date(2024, 6, 20)])[-2:]

    # 结算后现金 = 期初 − 全价成交金额（零费用）
    gross = 1_000_000.0 / 100.0 * (100.0 + accrued_interest(bond, date(2024, 6, 18)))
    assert snap18.cash == pytest.approx(2_000_000.0 - gross, abs=1e-6)
    # 现金不变，NAV 变化 = 固定面值 × 全价变动（票息应计）
    expected = (
        1_000_000.0
        / 100.0
        * (manual_dirty(bond, date(2024, 6, 20)) - manual_dirty(bond, date(2024, 6, 18)))
    )
    assert snap20.cash == pytest.approx(snap18.cash, abs=1e-9)
    assert snap20.nav - snap18.nav == pytest.approx(expected, abs=1e-6)
    # NAV 恒等式（trade-date 会计：含未结算应付/应收）
    for snap in (snap18, snap20):
        assert snap.nav == pytest.approx(
            snap.cash
            + sum(position.market_value for position in snap.positions)
            + snap.pending_cash,
            abs=1e-9,
        )
    # 应计利息与定价层一致
    assert snap20.positions[0].accrued_interest == pytest.approx(
        accrued_interest(bond, date(2024, 6, 20)), abs=1e-12
    )


# ---------------------------------------------------------------------------
# 2. 跨付息日：票息到账、应计归零、净值不跳空
# ---------------------------------------------------------------------------
def test_cross_coupon_no_nav_jump(calendar: TradingCalendar) -> None:
    cfg = zero_cost_cfg()
    bond = make_bond("T26", date(2024, 6, 15), date(2026, 6, 15))
    trade = make_trade(calendar, cfg, trade_id="t1", bond=bond, trade_date=date(2024, 6, 17))
    ledger = Ledger(calendar, initial_cash=2_000_000.0, funding_rate_pct=0.0, cfg=cfg)
    ledger.submit(trade)
    snap13, snap16 = process(
        ledger, [date(2024, 6, 17), date(2024, 6, 18), date(2024, 12, 13), date(2024, 12, 16)]
    )[-2:]

    # 2024-12-15 为周日：付息顺延到 12-16；没有 12-15 的到账记录
    events = ledger.cashflows_frame()
    coupons = events[events["kind"] == "coupon"]
    assert list(coupons["date"]) == [date(2024, 12, 16)]
    assert float(coupons["amount"].iloc[0]) == pytest.approx(10_000.0)  # 2%/2 × 100 万
    assert not (events["date"] == date(2024, 12, 15)).any()

    # 应计归零：付息日后一日应计远小于付息前
    assert snap16.positions[0].accrued_interest < snap13.positions[0].accrued_interest
    assert snap16.positions[0].accrued_interest == pytest.approx(
        accrued_interest(bond, date(2024, 12, 16)), abs=1e-12
    )
    # 净值连续：变化 = 票息现金 + 固定面值 × 全价变动（没有票息跳空）
    expected = 10_000.0 + 1_000_000.0 / 100.0 * (
        manual_dirty(bond, date(2024, 12, 16)) - manual_dirty(bond, date(2024, 12, 13))
    )
    assert snap16.nav - snap13.nav == pytest.approx(expected, abs=1e-6)
    assert abs(snap16.nav - snap13.nav) < 1_000.0  # 远小于一个付息额，确认未跳空


# ---------------------------------------------------------------------------
# 3. 闰年 2024-02-29 与国庆 2024-10-01 的现金流日期
# ---------------------------------------------------------------------------
def test_leap_day_coupon(calendar: TradingCalendar) -> None:
    cfg = zero_cost_cfg()
    bond = make_bond("LEAP", date(2023, 8, 29), date(2024, 8, 29))
    trade = make_trade(calendar, cfg, trade_id="t1", bond=bond, trade_date=date(2024, 1, 15))
    ledger = Ledger(calendar, initial_cash=2_000_000.0, funding_rate_pct=0.0, cfg=cfg)
    ledger.submit(trade)
    snap28, snap29 = process(
        ledger, [date(2024, 1, 15), date(2024, 1, 16), date(2024, 2, 28), date(2024, 2, 29)]
    )[-2:]

    assert calendar.is_trading_day(date(2024, 2, 29))
    coupons = ledger.cashflows_frame()
    coupons = coupons[coupons["kind"] == "coupon"]
    assert list(coupons["date"]) == [date(2024, 2, 29)]  # 闰日付息
    assert snap28.positions[0].accrued_interest > 0.0
    assert snap29.positions[0].accrued_interest == pytest.approx(0.0, abs=1e-12)  # 付息后归零


def test_national_day_coupon_rolls_to_next_trading_day(calendar: TradingCalendar) -> None:
    cfg = zero_cost_cfg()
    bond = make_bond("HOL", date(2024, 4, 1), date(2025, 4, 1))
    trade = make_trade(calendar, cfg, trade_id="t1", bond=bond, trade_date=date(2024, 4, 2))
    ledger = Ledger(calendar, initial_cash=2_000_000.0, funding_rate_pct=0.0, cfg=cfg)
    ledger.submit(trade)
    snap30, snap08 = process(
        ledger, [date(2024, 4, 2), date(2024, 4, 3), date(2024, 9, 30), date(2024, 10, 8)]
    )[-2:]

    assert not calendar.is_trading_day(date(2024, 10, 1))  # 国庆休市
    assert calendar.settlement_date(date(2024, 9, 30), 1) == date(2024, 10, 8)
    coupons = ledger.cashflows_frame()
    coupons = coupons[coupons["kind"] == "coupon"]
    assert list(coupons["date"]) == [date(2024, 10, 8)]  # 顺延到 10-08
    assert float(coupons["amount"].iloc[0]) == pytest.approx(10_000.0)
    assert snap30.positions[0].accrued_interest > 0.0
    # 9/30 → 10/8 只有票息到账（无其他现金流、无融资）
    assert snap08.cash - snap30.cash == pytest.approx(10_000.0, abs=1e-9)


# ---------------------------------------------------------------------------
# 4. 部分减仓
# ---------------------------------------------------------------------------
def test_partial_sell_cash_and_position(calendar: TradingCalendar) -> None:
    cfg = zero_cost_cfg()
    bond = make_bond("T26", date(2024, 6, 15), date(2026, 6, 15))
    buy = make_trade(calendar, cfg, trade_id="b1", bond=bond, trade_date=date(2024, 6, 17))
    sell = make_trade(
        calendar,
        cfg,
        trade_id="s1",
        bond=bond,
        side="sell",
        face_value=400_000.0,
        trade_date=date(2024, 6, 24),
    )
    ledger = Ledger(calendar, initial_cash=3_000_000.0, funding_rate_pct=0.0, cfg=cfg)
    ledger.submit(buy)
    ledger.submit(sell)
    snap = process(
        ledger,
        [date(2024, 6, 17), date(2024, 6, 18), date(2024, 6, 24), date(2024, 6, 25)],
    )[-1]

    ai_buy = accrued_interest(bond, date(2024, 6, 18))
    ai_sell = accrued_interest(bond, date(2024, 6, 25))
    expected_cash = 3_000_000.0 - 10_000.0 * (100.0 + ai_buy) + 4_000.0 * (100.0 + ai_sell)
    assert snap.cash == pytest.approx(expected_cash, abs=1e-6)
    assert len(snap.positions) == 1
    assert snap.positions[0].face_value == pytest.approx(600_000.0)
    assert snap.positions[0].market_value == pytest.approx(
        6_000.0 * manual_dirty(bond, date(2024, 6, 25)), abs=1e-6
    )
    assert snap.nav == pytest.approx(snap.cash + snap.positions[0].market_value, abs=1e-9)


# ---------------------------------------------------------------------------
# 5. 到期兑付：本金 + 末期票息入账、持仓归零
# ---------------------------------------------------------------------------
def test_maturity_redemption(calendar: TradingCalendar) -> None:
    cfg = zero_cost_cfg()
    bond = make_bond("MAT", date(2024, 6, 15), date(2025, 6, 15))
    trade = make_trade(calendar, cfg, trade_id="t1", bond=bond, trade_date=date(2024, 6, 17))
    ledger = Ledger(calendar, initial_cash=2_000_000.0, funding_rate_pct=0.0, cfg=cfg)
    ledger.submit(trade)
    process(ledger, [date(2024, 6, 17), date(2024, 6, 18), date(2025, 6, 13)])
    snapshot = ledger.process_day(date(2025, 6, 16), flat_curve(date(2025, 6, 16)))

    assert not calendar.is_trading_day(date(2025, 6, 15))  # 到期日为周日 → 顺延
    assert snapshot.positions == ()  # 到期后持仓归零
    events = ledger.cashflows_frame()
    final = events[events["date"] == date(2025, 6, 16)]
    kinds = final["kind"].tolist()
    assert kinds == ["coupon", "principal"]
    assert float(final.loc[final["kind"] == "coupon", "amount"].iloc[0]) == pytest.approx(10_000.0)
    assert float(final.loc[final["kind"] == "principal", "amount"].iloc[0]) == pytest.approx(
        1_000_000.0
    )
    gross_buy = 10_000.0 * (100.0 + accrued_interest(bond, date(2024, 6, 18)))
    expected_cash = 2_000_000.0 - gross_buy + 10_000.0 + 10_000.0 + 1_000_000.0
    assert snapshot.cash == pytest.approx(expected_cash, abs=1e-6)
    assert snapshot.nav == pytest.approx(snapshot.cash, abs=1e-9)


# ---------------------------------------------------------------------------
# 6. 融资利率变动（含逐日设置与周末计提）
# ---------------------------------------------------------------------------
def test_funding_aggregated_per_processing_day(calendar: TradingCalendar) -> None:
    """融资成本按「处理日」合并入账：事件日期=as_of，金额=区间逐自然日利息之和。"""
    cfg = zero_cost_cfg()
    bond = make_bond("T26", date(2024, 6, 15), date(2026, 6, 15))
    trade = make_trade(calendar, cfg, trade_id="t1", bond=bond, trade_date=date(2024, 6, 17))
    ledger = Ledger(
        calendar,
        initial_cash=100_000.0,
        funding_rate_pct=1.0,
        funding_rate_by_date={date(2024, 6, 19): 2.0, date(2024, 6, 20): 4.0},
        cfg=cfg,
    )
    ledger.set_funding_rate(date(2024, 6, 21), 3.0)  # 逐日设值覆盖
    ledger.submit(trade)
    process_days = [
        date(2024, 6, 17),
        date(2024, 6, 18),
        date(2024, 6, 19),
        date(2024, 6, 20),
        date(2024, 6, 21),
        date(2024, 6, 24),
    ]
    snapshots = process(ledger, process_days)

    gross_buy = 10_000.0 * (100.0 + accrued_interest(bond, date(2024, 6, 18)))
    rate_by_day = {
        date(2024, 6, 18): 1.0,
        date(2024, 6, 19): 2.0,
        date(2024, 6, 20): 4.0,
        date(2024, 6, 21): 3.0,  # set_funding_rate 覆盖
        date(2024, 6, 22): 1.0,  # 周六：回落基础利率
        date(2024, 6, 23): 1.0,  # 周日
        date(2024, 6, 24): 1.0,
    }
    cash = 100_000.0
    expected: dict[date, float] = {}
    for index, as_of in enumerate(process_days):
        start = process_days[index - 1] + timedelta(days=1) if index > 0 else as_of
        total = 0.0
        day = start
        while day <= as_of:
            if day == date(2024, 6, 18):  # 买入结算使现金转负
                cash -= gross_buy
            rate = rate_by_day.get(day, 1.0)
            if cash < 0.0:
                interest = cash * rate / 100.0 / 365.0
                cash += interest
                total += interest
            day += timedelta(days=1)
        if total != 0.0:
            expected[as_of] = total
        assert snapshots[index].cash == pytest.approx(cash, abs=1e-6)

    funding = ledger.cashflows_frame()
    funding = funding[funding["kind"] == "funding"].sort_values("date")
    assert funding["date"].tolist() == sorted(expected)
    for _date, amount in zip(funding["date"], funding["amount"], strict=True):
        assert float(amount) == pytest.approx(expected[_date], rel=1e-12)
    # 周末不单独产生事件（合并进 6-24 的处理日）
    assert date(2024, 6, 22) not in set(funding["date"])
    assert date(2024, 6, 23) not in set(funding["date"])
    assert float(funding["amount"].sum()) < 0.0


def test_funding_over_spring_festival_single_event(calendar: TradingCalendar) -> None:
    """春节跨期融资合并为一笔、日期=节后首个 NAV 日 2019-02-11。

    2019-02-01（周五）之后 02-04~02-10 为春节休市，02-11（周一）恢复。
    区间 02-02 → 02-11 共 10 个自然日（周末 02-02/03/09/10 仍按 365 计息，
    以保持「总成本不变」；其中春节法定假期 02-04~02-10 为 7 天）。
    """
    cfg = zero_cost_cfg()
    bond = make_bond("CNY19", date(2019, 1, 28), date(2029, 1, 28), coupon_pct=3.2)
    trade = make_trade(calendar, cfg, trade_id="t1", bond=bond, trade_date=date(2019, 1, 29))
    ledger = Ledger(calendar, initial_cash=100_000.0, funding_rate_pct=2.0, cfg=cfg)
    ledger.submit(trade)
    process_days = [
        date(2019, 1, 29),
        date(2019, 1, 30),
        date(2019, 1, 31),
        date(2019, 2, 1),
        date(2019, 2, 11),
    ]
    snapshots = process(ledger, process_days)

    assert not calendar.is_trading_day(date(2019, 2, 4))
    assert calendar.is_trading_day(date(2019, 2, 11))
    funding = ledger.cashflows_frame()
    funding = funding[funding["kind"] == "funding"].sort_values("date")
    assert funding["date"].tolist() == [
        date(2019, 1, 30),
        date(2019, 1, 31),
        date(2019, 2, 1),
        date(2019, 2, 11),
    ]
    # 02-11 只有一笔，金额覆盖 02-02..02-11 共 10 个自然日（复利路径）
    span = funding[funding["date"] == date(2019, 2, 11)]
    assert len(span) == 1
    gross_buy = 10_000.0 * (100.0 + accrued_interest(bond, date(2019, 1, 30)))
    cash = 100_000.0 - gross_buy
    for _day in (date(2019, 1, 30), date(2019, 1, 31), date(2019, 2, 1)):
        cash += cash * 2.0 / 100.0 / 365.0
    expected_span = 0.0
    for _ in range(10):  # 02-02 .. 02-11
        interest = cash * 2.0 / 100.0 / 365.0
        cash += interest
        expected_span += interest
    assert float(span["amount"].iloc[0]) == pytest.approx(expected_span, rel=1e-12)
    assert "10 个自然日" in str(span["note"].iloc[0])
    assert snapshots[-1].cash == pytest.approx(cash, abs=1e-6)


def test_weekend_gap_funding_is_three_days(calendar: TradingCalendar) -> None:
    """周五 → 周一跨周末：融资事件 1 笔、金额 = 3 个自然日利息（含复利路径）。"""
    cfg = zero_cost_cfg()
    bond = make_bond("T26", date(2024, 6, 15), date(2026, 6, 15))
    trade = make_trade(calendar, cfg, trade_id="t1", bond=bond, trade_date=date(2024, 6, 17))
    ledger = Ledger(calendar, initial_cash=100_000.0, funding_rate_pct=2.0, cfg=cfg)
    ledger.submit(trade)
    process(ledger, [date(2024, 6, 17), date(2024, 6, 18), date(2024, 6, 21), date(2024, 6, 24)])
    funding = ledger.cashflows_frame()
    funding = funding[funding["kind"] == "funding"].sort_values("date")
    monday = funding[funding["date"] == date(2024, 6, 24)]
    assert len(monday) == 1

    gross_buy = 10_000.0 * (100.0 + accrued_interest(bond, date(2024, 6, 18)))
    cash = 100_000.0 - gross_buy
    for _day in range(4):  # 6/18, 6/19, 6/20, 6/21 的日融资
        interest = cash * 2.0 / 100.0 / 365.0
        cash += interest
    simple_three_days = cash * 2.0 / 100.0 / 365.0 * 3.0
    expected = 0.0
    for _ in range(3):  # 6/22, 6/23, 6/24
        interest = cash * 2.0 / 100.0 / 365.0
        cash += interest
        expected += interest
    assert float(monday["amount"].iloc[0]) == pytest.approx(expected, rel=1e-12)
    assert float(monday["amount"].iloc[0]) == pytest.approx(simple_three_days, rel=1e-4)


def test_all_cashflow_event_dates_are_trading_days(calendar: TradingCalendar) -> None:
    """窗口内全部现金流事件日期都落在交易日历（含融资/票息/本金/费用/交易）。"""
    cfg = get_config()  # 默认成本：产生 fee；初始现金低 → 产生融资
    bond = make_bond("T26", date(2024, 6, 15), date(2026, 6, 15))
    trade = make_trade(calendar, cfg, trade_id="t1", bond=bond, trade_date=date(2024, 6, 17))
    sell = make_trade(
        calendar,
        cfg,
        trade_id="s1",
        bond=bond,
        side="sell",
        face_value=400_000.0,
        trade_date=date(2024, 6, 24),
    )
    ledger = Ledger(calendar, initial_cash=100_000.0, funding_rate_pct=1.0, cfg=cfg)
    ledger.submit(trade)
    ledger.submit(sell)
    days = [
        date(2024, 6, 17),
        date(2024, 6, 18),
        date(2024, 6, 24),
        date(2024, 6, 25),
        date(2024, 12, 13),
        date(2024, 12, 16),
    ]
    process(ledger, days)
    events = ledger.cashflows_frame()
    assert len(events) > 0
    for value in events["date"]:
        assert calendar.is_trading_day(value), f"事件日期 {value} 不是交易日"
    kinds = set(events["kind"])
    assert {"initial", "trade_buy", "trade_sell", "fee", "coupon", "funding"} <= kinds


# ---------------------------------------------------------------------------
# 7. 零成本对照：NAV 变化只剩价格 + 票息应计
# ---------------------------------------------------------------------------
def test_zero_cost_only_price_and_accrual(calendar: TradingCalendar) -> None:
    cfg = zero_cost_cfg()
    bond = make_bond("T26", date(2024, 6, 15), date(2026, 6, 15))
    trade = make_trade(calendar, cfg, trade_id="t1", bond=bond, trade_date=date(2024, 6, 17))
    ledger = Ledger(calendar, initial_cash=2_000_000.0, funding_rate_pct=0.0, cfg=cfg)
    ledger.submit(trade)
    snap18, snap20 = process(ledger, [date(2024, 6, 17), date(2024, 6, 18), date(2024, 6, 20)])[-2:]

    kinds = set(ledger.cashflows_frame()["kind"])
    assert "fee" not in kinds and "funding" not in kinds
    expected = 10_000.0 * (
        manual_dirty(bond, date(2024, 6, 20)) - manual_dirty(bond, date(2024, 6, 18))
    )
    assert snap20.nav - snap18.nav == pytest.approx(expected, abs=1e-6)

    # 默认成本配置下费用事件确实存在，且 = fee_bp 净价成交额 + 每笔结算费
    default_cfg = get_config()
    bond2 = make_bond("T27", date(2024, 6, 15), date(2026, 6, 15))
    trade2 = make_trade(
        calendar, default_cfg, trade_id="d1", bond=bond2, trade_date=date(2024, 6, 17)
    )
    ledger2 = Ledger(calendar, initial_cash=2_000_000.0, funding_rate_pct=0.0, cfg=default_cfg)
    ledger2.submit(trade2)
    process(ledger2, [date(2024, 6, 17), date(2024, 6, 18)])
    fees = ledger2.cashflows_frame()
    fees = fees[fees["kind"] == "fee"]
    expected_fee = (
        default_cfg.costs.fee_bp / 1e4 * 1_000_000.0
        + default_cfg.costs.settlement_fee_cny_per_trade
    )
    assert float(fees["amount"].iloc[0]) == pytest.approx(-expected_fee, rel=1e-12)


# ---------------------------------------------------------------------------
# 8. 三层日期校验
# ---------------------------------------------------------------------------
def test_three_layer_date_validation(calendar: TradingCalendar) -> None:
    cfg = get_config()
    bond = make_bond("T26", date(2024, 6, 15), date(2026, 6, 15))
    ledger = Ledger(calendar, initial_cash=2_000_000.0, funding_rate_pct=0.0, cfg=cfg)

    # signal >= trade
    bad_signal = make_trade(
        calendar,
        cfg,
        trade_id="x1",
        bond=bond,
        trade_date=date(2024, 6, 17),
        signal_date=date(2024, 6, 17),
    )
    with pytest.raises(ValueError, match="signal_date < trade_date"):
        ledger.submit(bad_signal)

    # 结算日与 calendar.settlement_date(trade_date, lag) 不一致
    wrong_settle = make_trade(
        calendar,
        cfg,
        trade_id="x2",
        bond=bond,
        trade_date=date(2024, 6, 17),
        settlement_date=date(2024, 6, 18) + timedelta(days=1),
    )
    with pytest.raises(ValueError, match="不一致"):
        ledger.submit(wrong_settle)

    # 非交易日成交（2024-10-01 国庆）
    holiday = make_trade(
        calendar,
        cfg,
        trade_id="x3",
        bond=bond,
        trade_date=date(2024, 10, 1),
        signal_date=date(2024, 9, 27),
    )
    with pytest.raises(ValueError, match="不是交易日"):
        ledger.submit(holiday)

    # settlement < trade
    backdated = make_trade(
        calendar,
        cfg,
        trade_id="x4",
        bond=bond,
        trade_date=date(2024, 6, 17),
        settlement_date=date(2024, 6, 14),
    )
    with pytest.raises(ValueError, match="早于"):
        ledger.submit(backdated)

    # 同一 trade_id 重复
    ok = make_trade(calendar, cfg, trade_id="ok", bond=bond, trade_date=date(2024, 6, 17))
    ledger.submit(ok)
    with pytest.raises(ValueError, match="重复"):
        ledger.submit(ok)

    # 处理后不能回溯建仓
    ledger.process_day(date(2024, 6, 17), flat_curve(date(2024, 6, 17)))
    late = make_trade(calendar, cfg, trade_id="late", bond=bond, trade_date=date(2024, 6, 17))
    with pytest.raises(ValueError, match="不能回溯"):
        ledger.submit(late)


def test_curve_date_contract(calendar: TradingCalendar) -> None:
    cfg = zero_cost_cfg()
    ledger = Ledger(calendar, initial_cash=2_000_000.0, funding_rate_pct=0.0, cfg=cfg)
    with pytest.raises(ValueError, match="日期契约"):
        ledger.process_day(date(2024, 6, 17), flat_curve(date(2024, 6, 18)))


# ---------------------------------------------------------------------------
# 9. 未结算成交不计现金（T+2）
# ---------------------------------------------------------------------------
def test_unsettled_trade_not_in_cash(calendar: TradingCalendar) -> None:
    cfg = cfg_with_lag(2, zero_cost=True)
    bond = make_bond("T26", date(2024, 6, 15), date(2026, 6, 15))
    trade = make_trade(calendar, cfg, trade_id="t2", bond=bond, trade_date=date(2024, 6, 28))
    assert trade.settlement_date == date(2024, 7, 2)  # 周五 T+2 → 下周二
    ledger = Ledger(calendar, initial_cash=2_000_000.0, funding_rate_pct=0.0, cfg=cfg)
    ledger.submit(trade)

    snap_trade_day = ledger.process_day(date(2024, 6, 28), flat_curve(date(2024, 6, 28)))
    assert snap_trade_day.cash == pytest.approx(2_000_000.0)  # 现金未划转
    assert [t.trade_id for t in snap_trade_day.pending_settlements] == ["t2"]
    assert len(snap_trade_day.positions) == 1  # 经济敞口已在 trade_date 生效
    gross_expected = 10_000.0 * (100.0 + accrued_interest(bond, date(2024, 7, 2)))
    assert snap_trade_day.pending_cash == pytest.approx(-gross_expected, abs=1e-6)
    assert set(ledger.cashflows_frame()["kind"]) == {"initial"}

    snap_settled = ledger.process_day(date(2024, 7, 2), flat_curve(date(2024, 7, 2)))
    gross = 10_000.0 * (100.0 + accrued_interest(bond, date(2024, 7, 2)))
    assert snap_settled.cash == pytest.approx(2_000_000.0 - gross, abs=1e-6)
    assert snap_settled.pending_cash == pytest.approx(0.0, abs=1e-9)
    assert snap_settled.pending_settlements == ()
    settled_events = ledger.cashflows_frame()
    assert settled_events[settled_events["kind"] == "trade_buy"]["date"].tolist() == [
        date(2024, 7, 2)
    ]


# ---------------------------------------------------------------------------
# 10. 条款锁定与老化（持有 1 年）
# ---------------------------------------------------------------------------
def test_term_lock_and_aging(calendar: TradingCalendar) -> None:
    cfg = zero_cost_cfg()
    bond = make_bond("AGING10Y", date(2024, 6, 15), date(2034, 6, 15), coupon_pct=2.2, freq=2)
    trade = make_trade(calendar, cfg, trade_id="t1", bond=bond, trade_date=date(2024, 6, 17))
    ledger = Ledger(calendar, initial_cash=2_000_000.0, funding_rate_pct=0.0, cfg=cfg)
    ledger.submit(trade)
    snap0, snap1 = process(ledger, [date(2024, 6, 17), date(2024, 6, 18), date(2025, 6, 18)])[-2:]

    pos0, pos1 = snap0.positions[0], snap1.positions[0]
    # 条款锁定：同一对象、票息/频率/到期日不变
    assert pos0.bond is trade.bond and pos1.bond is trade.bond
    assert pos1.bond.coupon_pct == pytest.approx(2.2)
    assert pos1.bond.freq == 2
    assert pos1.bond.maturity_date == date(2034, 6, 15)
    # 老化：剩余期限减少 1 年（2024-06-18 → 2025-06-18 = 365 天）
    assert pos0.remaining_years - pos1.remaining_years == pytest.approx(1.0, abs=1e-9)
    assert pos1.remaining_years == pytest.approx(
        day_count(date(2025, 6, 18), bond.maturity_date) / 365.0, abs=1e-12
    )
    # 没有重建为同期限平价券：票面 2.2% ≠ 曲线 2.0%，净价显著高于 100
    assert pos1.clean_price > 100.5
    assert pos1.ytm_pct == pytest.approx(2.0, abs=1e-12)


def test_bond_switch_is_explicit_two_trades(calendar: TradingCalendar) -> None:
    cfg = zero_cost_cfg()
    bond1 = make_bond("OLD", date(2024, 6, 15), date(2026, 6, 15))
    bond2 = make_bond("NEW", date(2024, 5, 15), date(2027, 5, 15), coupon_pct=2.1)
    buy1 = make_trade(calendar, cfg, trade_id="b1", bond=bond1, trade_date=date(2024, 6, 17))
    sell1 = make_trade(
        calendar, cfg, trade_id="s1", bond=bond1, side="sell", trade_date=date(2024, 6, 24)
    )
    buy2 = make_trade(calendar, cfg, trade_id="b2", bond=bond2, trade_date=date(2024, 6, 24))
    ledger = Ledger(calendar, initial_cash=5_000_000.0, funding_rate_pct=0.0, cfg=cfg)
    for trade in (buy1, sell1, buy2):
        ledger.submit(trade)
    snapshot = process(
        ledger, [date(2024, 6, 17), date(2024, 6, 18), date(2024, 6, 24), date(2024, 6, 25)]
    )[-1]
    assert [position.bond.code for position in snapshot.positions] == ["NEW"]
    assert len(ledger.trades_frame()) == 3  # 显式两笔成交换券
    # 条款锁定冲突：同 code 不同条款的成交必须报错
    conflicting = make_bond("OLD", date(2024, 6, 15), date(2026, 6, 15), coupon_pct=2.5)
    bad = make_trade(calendar, cfg, trade_id="b3", bond=conflicting, trade_date=date(2024, 6, 26))
    with pytest.raises(ValueError, match="条款"):
        ledger.submit(bad)


def test_coupon_belongs_to_holder_on_coupon_date(calendar: TradingCalendar) -> None:
    """票息只付给付息日持有者：付息日前买入的账本收到票息，付息日后买入的没有。"""
    cfg = zero_cost_cfg()
    bond = make_bond("T26", date(2024, 6, 15), date(2026, 6, 15))
    coupon_day = date(2025, 12, 15)  # 周一，交易日

    before = make_trade(calendar, cfg, trade_id="before", bond=bond, trade_date=date(2025, 12, 12))
    ledger_before = Ledger(calendar, initial_cash=2_000_000.0, funding_rate_pct=0.0, cfg=cfg)
    ledger_before.submit(before)
    process(ledger_before, [date(2025, 12, 12), coupon_day])
    coupons_before = ledger_before.cashflows_frame()
    coupons_before = coupons_before[coupons_before["kind"] == "coupon"]
    assert coupons_before["date"].tolist() == [coupon_day]
    assert float(coupons_before["amount"].iloc[0]) == pytest.approx(10_000.0)

    after = make_trade(calendar, cfg, trade_id="after", bond=bond, trade_date=date(2025, 12, 16))
    ledger_after = Ledger(calendar, initial_cash=2_000_000.0, funding_rate_pct=0.0, cfg=cfg)
    ledger_after.submit(after)
    process(ledger_after, [date(2025, 12, 16)])
    kind_after = ledger_after.cashflows_frame()[ledger_after.cashflows_frame()["kind"] == "coupon"]
    assert kind_after.empty


def test_coupon_on_trading_day_resets_accrual(calendar: TradingCalendar) -> None:
    """交易日付息：票息当日到账、应计利息归零。"""
    cfg = zero_cost_cfg()
    bond = make_bond("TRADECOUPON", date(2025, 4, 15), date(2026, 4, 15))
    trade = make_trade(calendar, cfg, trade_id="t1", bond=bond, trade_date=date(2025, 5, 15))
    ledger = Ledger(calendar, initial_cash=2_000_000.0, funding_rate_pct=0.0, cfg=cfg)
    ledger.submit(trade)
    coupon_day = date(2025, 10, 15)
    assert calendar.is_trading_day(coupon_day)
    snap_before, snap_coupon = process(ledger, [date(2025, 5, 15), date(2025, 10, 14), coupon_day])[
        -2:
    ]
    assert snap_before.positions[0].accrued_interest > 0.0
    assert snap_coupon.positions[0].accrued_interest == pytest.approx(0.0, abs=1e-12)
    coupons = ledger.cashflows_frame()
    coupons = coupons[coupons["kind"] == "coupon"]
    assert coupons["date"].tolist() == [coupon_day]
    assert float(coupons["amount"].iloc[0]) == pytest.approx(10_000.0)


def test_trade_record_fee_is_stacked(calendar: TradingCalendar) -> None:
    """``TradeRecord.fee`` 非 0 时叠加到费用事件（零费率配置下仍收该笔附加费）。"""
    cfg = zero_cost_cfg()
    bond = make_bond("T26", date(2024, 6, 15), date(2026, 6, 15))
    trade = make_trade(
        calendar, cfg, trade_id="t1", bond=bond, trade_date=date(2024, 6, 17), fee=50.0
    )
    ledger = Ledger(calendar, initial_cash=2_000_000.0, funding_rate_pct=0.0, cfg=cfg)
    ledger.submit(trade)
    process(ledger, [date(2024, 6, 17), date(2024, 6, 18)])
    fees = ledger.cashflows_frame()
    fees = fees[fees["kind"] == "fee"]
    assert float(fees["amount"].iloc[0]) == pytest.approx(-50.0, rel=1e-12)


# ---------------------------------------------------------------------------
# trade-date 会计：未结算应付/应收计入 NAV（C-013）
# ---------------------------------------------------------------------------
def quoted_clean_price(bond: BondStatic, curve: Curve, settle: date) -> float:
    """按引擎口径报价：clean = dirty(settle, 曲线 YTM) − AI(settle)。"""
    remaining = day_count(settle, bond.maturity_date) / 365.0
    ytm = float(curve.rate(remaining))
    return dirty_price_from_ytm(bond, settle, ytm) - accrued_interest(bond, settle)


def test_trade_date_nav_reflects_price_only_not_buy_amount(
    calendar: TradingCalendar,
) -> None:
    """成交日 NAV 只反映价格变动（价格不变 ≈0），绝不等于买入金额。"""
    cfg = zero_cost_cfg()
    bond = make_bond("T26", date(2024, 6, 15), date(2026, 6, 15))
    trade_date = date(2024, 6, 17)
    settle = calendar.settlement_date(trade_date, 1)
    curve_trade = flat_curve(trade_date)
    # 构造「成交日全价 = 结算日应付」的报价，隔离出价格不变情形（应计差为 0）
    remaining = day_count(trade_date, bond.maturity_date) / 365.0
    clean_zero = dirty_price_from_ytm(
        bond, trade_date, float(curve_trade.rate(remaining))
    ) - accrued_interest(bond, settle)
    trade = make_trade(
        calendar, cfg, trade_id="t1", bond=bond, trade_date=trade_date, clean_price=clean_zero
    )
    ledger = Ledger(calendar, initial_cash=100_000_000.0, funding_rate_pct=0.0, cfg=cfg)
    ledger.submit(trade)
    snap_before = ledger.process_day(date(2024, 6, 14), flat_curve(date(2024, 6, 14)))
    snap_trade = ledger.process_day(trade_date, curve_trade)

    gross = 10_000.0 * (clean_zero + accrued_interest(bond, settle))
    assert snap_trade.cash == pytest.approx(snap_before.cash, abs=1e-9)  # 现金未动
    assert snap_trade.pending_cash == pytest.approx(-gross, abs=1e-6)  # 应付为负
    # 价格不变 → NAV 不变（只可能有应计 carry 的当日口径差，此处构造为严格 0）
    assert snap_trade.nav - snap_before.nav == pytest.approx(0.0, abs=1e-6)
    # 绝不能把未付买入款再计一次：NAV 变化远不等于 -gross
    assert snap_trade.nav - snap_before.nav != pytest.approx(-gross, abs=1.0)


def test_trade_date_nav_double_count_regression(calendar: TradingCalendar) -> None:
    """回归：平价券（clean=100）成交日 NAV 只差一日应计，不得虚增买入金额。"""
    cfg = zero_cost_cfg()
    bond = make_bond("T26", date(2024, 6, 15), date(2026, 6, 15))
    trade_date = date(2024, 6, 17)
    settle = calendar.settlement_date(trade_date, 1)
    trade = make_trade(
        calendar, cfg, trade_id="t1", bond=bond, trade_date=trade_date, clean_price=100.0
    )
    ledger = Ledger(calendar, initial_cash=100_000_000.0, funding_rate_pct=0.0, cfg=cfg)
    ledger.submit(trade)
    snap_before = ledger.process_day(date(2024, 6, 14), flat_curve(date(2024, 6, 14)))
    snap_trade = ledger.process_day(trade_date, flat_curve(trade_date))

    gross = 10_000.0 * (100.0 + accrued_interest(bond, settle))
    # trade-date 全价 − 结算应付 = 仅价格（含复利微差的一日 carry），不是买入金额
    expected_carry = 10_000.0 * (
        manual_dirty(bond, trade_date) - (100.0 + accrued_interest(bond, settle))
    )
    assert snap_trade.pending_cash == pytest.approx(-gross, abs=1e-6)
    assert snap_trade.nav - snap_before.nav == pytest.approx(expected_carry, abs=1e-6)
    assert abs(snap_trade.nav - snap_before.nav) < 1_000.0  # 约 1 日应计，远小于 100 万买入
    assert snap_trade.nav - snap_before.nav != pytest.approx(-gross, abs=1.0)


def test_settlement_day_pending_zero_and_nav_continuous(
    calendar: TradingCalendar,
) -> None:
    """结算日：pending_cash 归零、现金划转、NAV 连续（日变化 <1bp）。"""
    cfg = zero_cost_cfg()
    bond = make_bond("T26", date(2024, 6, 15), date(2026, 6, 15))
    trade = make_trade(
        calendar, cfg, trade_id="t1", bond=bond, trade_date=date(2024, 6, 17), clean_price=100.0
    )
    settle = trade.settlement_date
    ledger = Ledger(calendar, initial_cash=100_000_000.0, funding_rate_pct=0.0, cfg=cfg)
    ledger.submit(trade)
    snap_trade = ledger.process_day(date(2024, 6, 17), flat_curve(date(2024, 6, 17)))
    snap_settled = ledger.process_day(settle, flat_curve(settle))

    gross = 10_000.0 * (100.0 + accrued_interest(bond, settle))
    assert snap_settled.pending_cash == pytest.approx(0.0, abs=1e-9)
    assert snap_settled.cash == pytest.approx(snap_trade.cash - gross, abs=1e-6)
    change = snap_settled.nav - snap_trade.nav
    assert abs(change) / snap_trade.nav < 1e-4  # < 1bp
    assert snap_settled.nav == pytest.approx(
        snap_settled.cash
        + sum(p.market_value for p in snap_settled.positions)
        + snap_settled.pending_cash,
        abs=1e-9,
    )


def test_sell_pending_cash_is_positive(calendar: TradingCalendar) -> None:
    """卖出未结算：pending_cash 为正（未来收现），结算日归零、现金增加。"""
    cfg = zero_cost_cfg()
    bond = make_bond("T26", date(2024, 6, 15), date(2026, 6, 15))
    buy = make_trade(
        calendar, cfg, trade_id="b1", bond=bond, trade_date=date(2024, 6, 17), clean_price=100.0
    )
    sell = make_trade(
        calendar,
        cfg,
        trade_id="s1",
        bond=bond,
        side="sell",
        face_value=400_000.0,
        trade_date=date(2024, 6, 24),
        clean_price=100.0,
    )
    ledger = Ledger(calendar, initial_cash=100_000_000.0, funding_rate_pct=0.0, cfg=cfg)
    ledger.submit(buy)
    ledger.submit(sell)
    process(ledger, [date(2024, 6, 17), date(2024, 6, 18)])
    sell_settle = sell.settlement_date
    snap_sell = ledger.process_day(date(2024, 6, 24), flat_curve(date(2024, 6, 24)))
    cash_before = snap_sell.cash
    gross = 4_000.0 * (100.0 + accrued_interest(bond, sell_settle))
    assert snap_sell.pending_cash == pytest.approx(gross, abs=1e-6)  # 应收为正
    assert snap_sell.cash == pytest.approx(cash_before, abs=1e-9)
    assert snap_sell.positions[0].face_value == pytest.approx(600_000.0)

    snap_settled = ledger.process_day(sell_settle, flat_curve(sell_settle))
    assert snap_settled.pending_cash == pytest.approx(0.0, abs=1e-9)
    assert snap_settled.cash == pytest.approx(cash_before + gross, abs=1e-6)
    assert abs(snap_settled.nav - snap_sell.nav) / snap_sell.nav < 1e-4  # 连续


def test_t2_pending_cash_persists_until_settlement(calendar: TradingCalendar) -> None:
    """T+2 多日：pending_cash 在成交日与中间维持不变，结算日归零。"""
    cfg = cfg_with_lag(2, zero_cost=True)
    bond = make_bond("T26", date(2024, 6, 15), date(2026, 6, 15))
    trade = make_trade(
        calendar, cfg, trade_id="t2", bond=bond, trade_date=date(2024, 6, 28), clean_price=100.0
    )
    assert trade.settlement_date == date(2024, 7, 2)
    gross = 10_000.0 * (100.0 + accrued_interest(bond, date(2024, 7, 2)))
    ledger = Ledger(calendar, initial_cash=100_000_000.0, funding_rate_pct=0.0, cfg=cfg)
    ledger.submit(trade)

    for day in (date(2024, 6, 28), date(2024, 7, 1)):
        snapshot = ledger.process_day(day, flat_curve(day))
        assert snapshot.pending_cash == pytest.approx(-gross, abs=1e-6)
        assert snapshot.cash == pytest.approx(100_000_000.0, abs=1e-9)
        assert snapshot.nav == pytest.approx(
            snapshot.cash + sum(p.market_value for p in snapshot.positions) + snapshot.pending_cash,
            abs=1e-9,
        )
    settled = ledger.process_day(date(2024, 7, 2), flat_curve(date(2024, 7, 2)))
    assert settled.pending_cash == pytest.approx(0.0, abs=1e-9)
    assert settled.cash == pytest.approx(100_000_000.0 - gross, abs=1e-6)


# ---------------------------------------------------------------------------
# 导出契约、NAV 独立重算、可复现
# ---------------------------------------------------------------------------
def test_export_contract_and_reproducibility(calendar: TradingCalendar) -> None:
    cfg = zero_cost_cfg()
    bond = make_bond("T26", date(2024, 6, 15), date(2026, 6, 15))
    trade = make_trade(calendar, cfg, trade_id="t1", bond=bond, trade_date=date(2024, 6, 17))

    def run() -> Ledger:
        ledger = Ledger(calendar, initial_cash=2_000_000.0, funding_rate_pct=0.0, cfg=cfg)
        ledger.submit(trade)
        process(ledger, [date(2024, 6, 17), date(2024, 6, 18), date(2024, 6, 20)])
        return ledger

    ledger = run()
    assets = (
        ledger.trades_frame(),
        ledger.cashflows_frame(),
        ledger.snapshots_frame(),
        ledger.nav_series(),
    )
    assert list(assets[0].columns) == list(TRADES_COLUMNS)
    assert list(assets[1].columns) == list(CASHFLOWS_COLUMNS)
    assert list(assets[2].columns) == list(SNAPSHOTS_COLUMNS)
    nav_series = assets[3]
    assert nav_series.name == "nav" and nav_series.index.name == "as_of"

    # 现金流闭环：期末现金 = Σ 全部现金事件
    assert ledger.cash == pytest.approx(float(ledger.cashflows_frame()["amount"].sum()), abs=1e-6)

    # snapshots_frame 独立重算 NAV 恒等式
    frame = assets[2]
    assert "pending_cash" in frame.columns
    for _as_of, group in frame.groupby("as_of", sort=True):
        cash = float(group["cash"].iloc[0])
        nav = float(group["nav"].iloc[0])
        pending_cash = float(group["pending_cash"].iloc[0])
        recomputed = cash + float(group["market_value"].fillna(0.0).sum()) + pending_cash
        assert nav == pytest.approx(recomputed, abs=1e-8)

    # 可复现：同输入两次运行逐位相同
    rerun = run()
    pd.testing.assert_series_equal(nav_series, rerun.nav_series(), check_exact=True)
    pd.testing.assert_frame_equal(assets[1], rerun.cashflows_frame(), check_exact=True)


def test_snapshot_with_no_positions_exports_row(calendar: TradingCalendar) -> None:
    cfg = zero_cost_cfg()
    ledger = Ledger(calendar, initial_cash=1_000_000.0, funding_rate_pct=0.0, cfg=cfg)
    snapshot = ledger.process_day(date(2024, 6, 17), flat_curve(date(2024, 6, 17)))
    assert snapshot.positions == () and snapshot.nav == pytest.approx(1_000_000.0)
    frame = ledger.snapshots_frame()
    assert len(frame) == 1
    assert int(frame["n_positions"].iloc[0]) == 0
    assert pd.isna(frame["bond_code"].iloc[0])
    assert float(frame["market_value"].iloc[0]) == 0.0
    assert float(frame["pending_cash"].iloc[0]) == 0.0
    assert float(frame["nav"].iloc[0]) == pytest.approx(1_000_000.0)
