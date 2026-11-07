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
    # 基准 NAV 与策略同引擎、同成本口径（R22）
    assert len(result.benchmark_nav) == len(result.nav)
    assert result.benchmark_nav.index.equals(result.nav.index)

    # 3) 记录完整性
    for column in (
        "date",
        "nav",
        "cash",
        "pending_cash",
        "total_dv01",
        "duration",
        "dv01_ratio",
        "bench_total_dv01",
        "bench_duration",
        "bench_nav",
    ):
        assert column in result.exposure.columns
    assert {"fee", "funding", "spread", "total"} <= set(result.costs.columns)
    assert result.costs["total"].sum() > 0  # 费用/融资/价差都应真实发生
    # R26：价差进入执行价并单独记账；成交明细的价差合计 = 成本表价差合计
    assert result.costs["spread"].sum() > 0
    if "spread_cost" in result.trades.columns:
        assert result.trades["spread_cost"].sum() == pytest.approx(
            result.costs["spread"].sum(), rel=1e-9
        )
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


class _OverweightThenRisky:
    """前段 1.1× 基准（可执行），后段 3× 基准（不可行）→ 验证熔断降风险。"""

    name = "overweight_then_risky"

    def target_krd(self, ctx: MarketContext) -> TargetKRD:
        mult = 1.1 if ctx.as_of <= START else 3.0
        return TargetKRD(as_of=ctx.as_of, krd={t: mult * v for t, v in ctx.benchmark.krd.items()})


class _OverweightBenchmark:
    """目标 = 基准 KRD 的 1.2 倍（主动加久期），用于验证成交前约束闸门。"""

    name = "overweight"

    def target_krd(self, ctx: MarketContext) -> TargetKRD:
        return TargetKRD(as_of=ctx.as_of, krd={t: 1.2 * v for t, v in ctx.benchmark.krd.items()})


class _ZeroTilt:
    name = "zero_tilt"

    def target_krd(self, ctx: MarketContext) -> TargetKRD:
        return TargetKRD(as_of=ctx.as_of, krd=dict(ctx.benchmark.krd))


def test_r22_zero_tilt_control_excess_near_zero(market) -> None:
    """R22：同引擎零 tilt 控制组，超额必须接近 0（±1bp），且成交仅来自整手/死区。"""
    cfg, panel, funding, calendar = market
    result = run_backtest(
        _ZeroTilt(), panel=panel, calendar=calendar, funding=funding, cfg=cfg, start=START, end=END
    )
    bench_ret = result.benchmark_nav.iloc[-1] / result.benchmark_nav.iloc[0] - 1.0
    ret = result.nav.iloc[-1] / result.nav.iloc[0] - 1.0
    assert abs((ret - bench_ret) * 1e4) <= 1.0
    assert len(result.trades) <= 12  # 4 笔建仓 + 少量整手/死区抖单
    # 基准价差与策略价差同量级（策略只多出少量抖单成本）
    assert result.costs["spread"].sum() == pytest.approx(
        result.costs["bench_spread"].sum(), rel=0.2
    )


def test_r23_pretrade_gate_rejects_infeasible_target(market) -> None:
    """R23：目标不可行时必须在成交前拒单，不能先成交再记录违规。"""
    cfg, panel, funding, calendar = market
    # 把绝对 DV01 上限压到基准的 90%：零 tilt（=基准）不可行
    from ficc.portfolio.benchmark import build_benchmark, build_universe_bonds

    curve0 = _curve(panel, START)
    bonds = build_universe_bonds(curve0, START, cfg=cfg)
    bench0 = build_benchmark(curve0, START, bonds=bonds, calendar=calendar, cfg=cfg)
    limit = 0.90 * bench0.total_dv01
    risk = cfg.risk.model_copy(
        update={"limits": cfg.risk.limits.model_copy(update={"max_dv01": limit})}
    )
    tight = cfg.model_copy(update={"risk": risk})
    result = run_backtest(
        _ZeroTilt(),
        panel=panel,
        calendar=calendar,
        funding=funding,
        cfg=tight,
        start=START,
        end=END,
    )
    held = result.constraints[result.constraints["n_positions"] > 0]
    if len(held):
        assert "absolute_dv01" not in held["violation_codes"].str.cat(sep=";")
        assert (held["m_absolute_dv01"] <= limit + 1e-6).all()
    # 要么拒单，要么缩到可行域；不允许"先成交再报违规"
    assert len(result.rejected) > 0 or len(held) == 0


def test_r23_pretrade_gate_scales_reducible_target(market) -> None:
    """R23：可缩减的过度风险目标，引擎应缩到约束内成交（而非成交后违规）。"""
    cfg, panel, funding, calendar = market
    result = run_backtest(
        _OverweightBenchmark(),
        panel=panel,
        calendar=calendar,
        funding=funding,
        cfg=cfg,
        start=START,
        end=END,
    )
    held = result.constraints[result.constraints["n_positions"] > 0]
    assert len(held) > 0
    assert held["ok"].all(), held.loc[~held["ok"], "violation_codes"].value_counts().to_dict()
    # 超额加久期的目标被投影回 1.3 倍 DV01 带内
    assert (
        result.exposure.loc[result.exposure["total_dv01"] > 0, "dv01_ratio"] <= 1.30 + 1e-6
    ).all()


def test_r26_zero_spread_control(market) -> None:
    """R26 控制：把价差全部置零后，策略成本里的 spread 必须为 0。"""
    cfg, panel, funding, calendar = market
    costs = cfg.costs.model_copy(
        update={"spread_bp_active": 0.0, "spread_bp_illiquid": 0.0, "spread_bp_ultra_long": 0.0}
    )
    zero = cfg.model_copy(update={"costs": costs})
    result = run_backtest(
        _ZeroTilt(), panel=panel, calendar=calendar, funding=funding, cfg=zero, start=START, end=END
    )
    assert result.costs["spread"].sum() == pytest.approx(0.0, abs=1e-9)
    assert result.costs["bench_spread"].sum() == pytest.approx(0.0, abs=1e-9)


def _curve(panel, d):
    import numpy as np

    from ficc.curve.curve import Curve

    row = panel.loc[pd.Timestamp(d)].reindex(list(CANONICAL_TENORS))
    return Curve(
        as_of=d,
        tenors=np.asarray(list(CANONICAL_TENORS)),
        rates=tuple(row.values),
        method="hermite",
    )


def test_r23_cash_budget_includes_pending_shipments(market) -> None:
    """R23 补查：现金预算必须包含未结算付款/费用，不能等结算后才报违规。

    审查反例：min_cash_fraction=0 时，零 tilt 在 2019-01-29 仍获准成交，次日现金
    约 −29.58 万元。修复后应在成交前拒单（或缩到现金非负），且持仓日不得出现
    cash_floor 违规。
    """
    cfg, panel, funding, calendar = market
    tight = cfg.model_copy(
        update={"constraints": cfg.constraints.model_copy(update={"min_cash_fraction": 0.0})}
    )
    result = run_backtest(
        _ZeroTilt(),
        panel=panel,
        calendar=calendar,
        funding=funding,
        cfg=tight,
        start=START,
        end=date(2019, 1, 31),
    )
    assert len(result.rejected) > 0
    assert len(result.trades) == 0
    held = result.constraints[result.constraints["n_positions"] > 0]
    assert "cash_floor" not in held["violation_codes"].str.cat(sep=";")
    assert (result.exposure["cash"] >= -1e-6).all()


def test_drawdown_trigger_allows_derisking_execution(market) -> None:
    """回撤触发后，引擎必须能执行降低风险的订单（不能被同一回撤检查永久拒绝）。"""
    cfg, panel, funding, calendar = market
    tight = cfg.model_copy(
        update={
            "risk": cfg.risk.model_copy(
                update={"limits": cfg.risk.limits.model_copy(update={"max_drawdown_stop": 2e-3})}
            )
        }
    )
    result = run_backtest(
        _OverweightThenRisky(),
        panel=panel,
        calendar=calendar,
        funding=funding,
        cfg=tight,
        start=START,
        end=date(2019, 3, 29),
    )
    assert int(result.signals["risk_off"].sum()) > 0
    # 熔断后必须有卖出（降风险）成交
    sells = result.trades[result.trades["side"] == "sell"]
    assert len(sells) > 0
    # 降风险后组合 DV01 应低于 1.1× 基准的最高暴露
    dv01 = result.exposure["total_dv01"]
    bench = result.exposure["bench_total_dv01"]
    assert dv01.max() > 0
    assert (dv01 / bench).max() < 1.15  # 1.1× 目标经整手取整后允许少量超调
    assert len(result.rejected) > 0  # 3× 目标被拒
