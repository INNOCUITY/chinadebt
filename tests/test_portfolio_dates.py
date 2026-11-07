"""R27：组合层日期契约（CN_IB 日历贯通 bootstrap / 基准 / 报价）。

审查证据：2019-09-30 的 spot 参考结算日是 10-01，而账本为 10-08；2020-12-31 分别
为 2021-01-01 / 01-04。本文件锁定"组合层必须用真实交易日历"的契约。
"""

from __future__ import annotations

from datetime import date, datetime

import numpy as np
import pytest

from ficc.config import get_config
from ficc.curve.bootstrap import bootstrap_spot
from ficc.curve.curve import Curve
from ficc.portfolio.benchmark import build_benchmark, build_universe_bonds
from ficc.portfolio.construction import build_orders, execution_price
from ficc.portfolio.dates import remaining_years, settlement_for
from ficc.portfolio.types import TargetKRD
from ficc.utils.calendar import TradingCalendar

TENORS = np.array([0.25, 0.5, 1.0, 2.0, 3.0, 5.0, 7.0, 10.0, 15.0, 20.0, 30.0])

# 2019 国庆前后 + 2019 春节调休（02-02 周六是交易日）
_DAYS = [
    date(2019, 1, 28),
    date(2019, 1, 29),
    date(2019, 1, 30),
    date(2019, 1, 31),
    date(2019, 2, 1),
    date(2019, 2, 2),  # 周六调休交易日
    date(2019, 2, 3),  # 周日调休交易日
    date(2019, 2, 11),
    date(2019, 9, 27),
    date(2019, 9, 30),
    date(2019, 10, 8),
    date(2019, 12, 31),
    date(2020, 1, 2),
    date(2020, 12, 30),
    date(2020, 12, 31),
    date(2021, 1, 4),
]


def _calendar() -> TradingCalendar:
    return TradingCalendar.from_dates(_DAYS)


def _curve(d: date, as_of: date | None = None) -> Curve:
    rates = np.linspace(1.5, 3.0, TENORS.size)
    return Curve(as_of=as_of or d, tenors=TENORS, rates=rates, method="linear")


@pytest.mark.parametrize(
    ("observation", "expected"),
    [
        (date(2019, 9, 30), date(2019, 10, 8)),  # 跨国庆
        (date(2019, 12, 31), date(2020, 1, 2)),  # 跨元旦
        (date(2020, 12, 31), date(2021, 1, 4)),  # 跨元旦 + 周末
        (date(2019, 2, 1), date(2019, 2, 2)),  # 周六调休交易日
    ],
)
def test_settlement_uses_real_calendar(observation: date, expected: date) -> None:
    cal = _calendar()
    assert settlement_for(cal, observation) == expected
    # 未传日历时退回简单工作日规则，与真实日历不同（防止隐式回退）
    assert settlement_for(None, observation) != expected or observation.weekday() >= 5


def test_bootstrap_settlement_matches_calendar() -> None:
    cal = _calendar()
    curve = _curve(date(2019, 9, 30))
    spot = bootstrap_spot(curve, calendar=cal)
    assert spot.settlement_date == date(2019, 10, 8)
    # 不带日历 → 工作日规则给 10-01（历史 bug 的复现点，现在只在显式不传时发生）
    legacy = bootstrap_spot(curve)
    assert legacy.settlement_date == date(2019, 10, 1)


def test_bootstrap_accepts_weekend_trading_day_from_calendar() -> None:
    cal = _calendar()
    curve = _curve(date(2019, 2, 1))
    spot = bootstrap_spot(curve, calendar=cal)
    assert spot.settlement_date == date(2019, 2, 2)  # 周六调休
    with pytest.raises(ValueError, match=r"工作日|交易日"):
        bootstrap_spot(curve, settlement=date(2019, 2, 2))  # 无日历时拒绝周末


def test_benchmark_and_orders_priced_on_settlement_date() -> None:
    cal = _calendar()
    as_of = date(2019, 9, 30)
    curve = _curve(as_of)
    cfg = get_config()
    bonds = build_universe_bonds(curve, as_of, cfg=cfg)
    benchmark = build_benchmark(curve, as_of, bonds=bonds, calendar=cal, cfg=cfg)
    assert benchmark.faces  # 固定面值
    # 基准面值按结算日（10-08）全价折算
    settlement = settlement_for(cal, as_of, cfg=cfg)
    for leg in benchmark.legs:
        assert leg.market_value > 0
        expected_remaining = remaining_years(leg.bond, settlement)
        assert expected_remaining > 0

    target = TargetKRD(as_of=as_of, krd=dict(benchmark.krd))
    orders = build_orders(target, [], bonds, curve, calendar=cal, cfg=cfg)
    for order in orders:
        exec_price, mid_price, spread_bp, _ = execution_price(
            order.bond, curve, side=order.side, calendar=cal, cfg=cfg
        )
        assert order.clean_price == pytest.approx(exec_price, rel=1e-12)
        assert order.mid_clean_price == pytest.approx(mid_price, rel=1e-12)
        assert spread_bp >= 0.0


def test_remaining_years_contract() -> None:
    cal = _calendar()
    as_of = date(2019, 9, 30)
    curve = _curve(as_of)
    cfg = get_config()
    bonds = build_universe_bonds(curve, as_of, cfg=cfg)
    settlement = settlement_for(cal, as_of, cfg=cfg)
    for bond in bonds:
        expected = (bond.maturity_date - settlement).days / 365.0
        assert remaining_years(bond, settlement) == pytest.approx(expected, rel=1e-12)


def test_settlement_rejects_datetime_and_bad_calendar() -> None:
    with pytest.raises(TypeError):
        settlement_for(None, datetime(2019, 9, 30))  # type: ignore[arg-type]
    with pytest.raises(TypeError, match="settlement_date"):
        settlement_for(object(), date(2019, 9, 30))
