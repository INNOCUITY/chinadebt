"""回测引擎（`backtest/engine.py`）的集成测试：真实曲线、短窗口、真实账本。"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from ficc.backtest.engine import run_backtest
from ficc.config import get_config
from ficc.data.loader import load_curve_long, load_funding_series
from ficc.data.schema import CANONICAL_TENORS
from ficc.portfolio.types import MarketContext, TargetKRD
from ficc.utils.calendar import TradingCalendar

START = date(2019, 1, 28)
END = date(2019, 2, 28)


@pytest.fixture(scope="module")
def market():
    cfg = get_config()
    long = load_curve_long(cfg=cfg)
    panel = (
        long.pivot(index="date", columns="tenor", values="ytm_pct")
        .sort_index()
        .reindex(columns=list(CANONICAL_TENORS))
    )
    funding = load_funding_series(cfg=cfg).set_index("date")["FDR007"].sort_index()
    calendar = TradingCalendar.from_dates(panel.index)
    return cfg, panel, funding, calendar


class _BenchmarkStrategy:
    """始终要求"回到基准"的策略：用于验证引擎的管道而非 alpha。"""

    name = "bench"

    def target_krd(self, ctx: MarketContext) -> TargetKRD:
        return TargetKRD(as_of=ctx.as_of, krd=dict(ctx.benchmark.krd), notes="bench")


def test_engine_end_to_end(market) -> None:
    cfg, panel, funding, calendar = market
    result = run_backtest(
        _BenchmarkStrategy(),
        panel=panel,
        calendar=calendar,
        funding=funding,
        cfg=cfg,
        start=START,
        end=END,
    )

    # 1) 日期与三层日期契约：成交日晚于信号日，结算日 = 日历 T+1
    days = list(calendar.trading_days_between(START, END))
    assert result.nav.index[0] == days[0]
    assert result.nav.index[-1] == days[-1]
    trades = result.trades
    assert len(trades) > 0
    lag = int(cfg.pricing.settlement_lag_days)
    for row in trades.itertuples():
        assert row.signal_date < row.trade_date
        assert row.settlement_date == calendar.settlement_date(row.trade_date, lag)

    # 2) NAV 连续：不应因未结算成交而出现数量级跳变
    daily = result.nav.pct_change().abs().dropna()
    assert daily.max() < 0.02, f"单日 NAV 跳变 {daily.max():.4f} 过大（疑似未结算双计）"

    # 3) 记录完整性
    for column in ("date", "nav", "cash", "pending_cash", "total_dv01", "duration", "dv01_ratio"):
        assert column in result.exposure.columns
    assert {"fee", "funding", "total"} <= set(result.costs.columns)
    assert result.costs["total"].sum() > 0  # 费用与融资都应真实发生
    assert len(result.constraints) == len(days)
    assert len(result.turnover) == len(days)

    # 4) 约束：有持仓的交易日应基本满足（首日空仓是预热期）
    held = result.constraints[result.constraints["n_positions"] > 0]
    assert len(held) > 0
    assert held["ok"].mean() >= 0.95, (
        f"有持仓日约束通过率只有 {held['ok'].mean():.2f}："
        f"{held.loc[~held['ok'], 'violation_codes'].value_counts().to_dict()}"
    )

    # 5) 无 NaN 污染关键列
    assert np.isfinite(result.exposure["total_dv01"]).all()
    assert np.isfinite(result.exposure["duration"]).all()


def test_engine_requires_two_trading_days(market) -> None:
    cfg, panel, funding, calendar = market
    with pytest.raises(ValueError, match="交易日不足"):
        run_backtest(
            _BenchmarkStrategy(),
            panel=panel,
            calendar=calendar,
            funding=funding,
            cfg=cfg,
            start=date(2019, 1, 26),  # 周六
            end=date(2019, 1, 28),
        )


def test_strategy_sees_only_past_history(market) -> None:
    """引擎传给策略的 history 必须以 as_of 结尾（不得含未来）。"""
    cfg, panel, funding, calendar = market
    seen: list[tuple[date, date]] = []

    class _Spy:
        name = "spy"

        def target_krd(self, ctx: MarketContext) -> TargetKRD:
            seen.append((ctx.as_of, ctx.history.index[-1].date()))
            return TargetKRD(as_of=ctx.as_of, krd=dict(ctx.benchmark.krd))

    run_backtest(
        _Spy(), panel=panel, calendar=calendar, funding=funding, cfg=cfg, start=START, end=END
    )
    assert seen
    for as_of, last in seen:
        assert last <= as_of
    assert isinstance(panel.index, pd.DatetimeIndex)
