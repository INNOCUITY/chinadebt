"""回测引擎（`backtest/engine.py`）的集成测试：真实曲线、短窗口、真实账本。"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from ficc.backtest.engine import allows_partial_de_risk, run_backtest
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


def _make_report(codes_and_values: list[tuple[str, float, float]]):
    from ficc.portfolio.constraints import ConstraintReport, ConstraintViolation

    violations = tuple(
        ConstraintViolation(code=code, detail=code, value=value, limit=limit)
        for code, value, limit in codes_and_values
    )
    return ConstraintReport(as_of=START, violations=violations, metrics={})


def test_r31_partial_de_risk_policy() -> None:
    """R31 降风险偏序：允许"历史回撤未消除但可行动项改善"，禁止引入新违规。"""
    current = _make_report([("cash_floor", -110.0, -100.0), ("drawdown_stop", -0.06, -0.05)])
    better = _make_report([("cash_floor", -105.0, -100.0), ("drawdown_stop", -0.06, -0.05)])
    worse = _make_report(
        [
            ("cash_floor", -105.0, -100.0),
            ("leverage", 2.1, 2.0),
            ("drawdown_stop", -0.06, -0.05),
        ]
    )
    unchanged = _make_report([("cash_floor", -110.0, -100.0), ("drawdown_stop", -0.06, -0.05)])
    assert allows_partial_de_risk(current, better)
    assert not allows_partial_de_risk(current, worse)  # 引入新违规 code
    assert not allows_partial_de_risk(current, unchanged)  # 可行动项未改善


def test_r31_risk_ladder_selects_lower_level(market) -> None:
    """R31：基准档不可行时，熔断必须实际降档执行（gate_level<1 且有卖出）。"""
    cfg, panel, funding, calendar = market

    class _Overweight85:
        name = "over85"

        def target_krd(self, ctx):
            from ficc.portfolio.types import TargetKRD

            mult = 0.85 if ctx.as_of <= START else 3.0
            return TargetKRD(
                as_of=ctx.as_of, krd={t: mult * v for t, v in ctx.benchmark.krd.items()}
            )

    limits = cfg.risk.limits.model_copy(update={"max_drawdown_stop": 2e-3, "max_dv01": 74_400.0})
    tight = cfg.model_copy(update={"risk": cfg.risk.model_copy(update={"limits": limits})})
    result = run_backtest(
        _Overweight85(),
        panel=panel,
        calendar=calendar,
        funding=funding,
        cfg=tight,
        start=START,
        end=date(2019, 3, 29),
    )
    assert int(result.signals["risk_off"].sum()) > 0
    levels = result.constraints["gate_level"]
    assert (levels < 1.0).any(), "熔断阶梯没有被实际选择"
    assert (result.trades["side"] == "sell").sum() >= 1
    ratio = result.exposure["total_dv01"] / result.exposure["bench_total_dv01"]
    assert ratio.iloc[-1] < 0.86  # 降档执行后明显低于初始 0.85×基准


def test_r32_long_holiday_cash_budget(market) -> None:
    """R32 反例：平 3% 曲线、5% 融资、零费用/价差、紧现金带跨春节不得违规。

    旧实现在闸门只扣一天融资，02-03 建仓后 02-11 出现 cash_floor；修复后
    闸门用账本状态推进到"结算日后的下一交易日"，把长假逐日融资计入投影。
    """
    cfg, panel, _funding, calendar = market
    dates = panel.index[
        (panel.index >= pd.Timestamp(date(2019, 1, 28)))
        & (panel.index <= pd.Timestamp(date(2019, 2, 15)))
    ]
    flat = pd.DataFrame(3.0, index=dates, columns=list(panel.columns))
    funding_5 = pd.Series(5.0, index=dates)
    costs = cfg.costs.model_copy(
        update={
            "fee_bp": 0.0,
            "settlement_fee_cny_per_trade": 0.0,
            "spread_bp_active": 0.0,
            "spread_bp_illiquid": 0.0,
            "spread_bp_ultra_long": 0.0,
        }
    )
    cons = cfg.constraints.model_copy(update={"min_cash_fraction": -0.0802})
    tight = cfg.model_copy(update={"costs": costs, "constraints": cons})

    class _Over108:
        name = "over108"

        def target_krd(self, ctx):
            from ficc.portfolio.types import TargetKRD

            return TargetKRD(
                as_of=ctx.as_of, krd={t: 1.08 * v for t, v in ctx.benchmark.krd.items()}
            )

    result = run_backtest(
        _Over108(),
        panel=flat,
        calendar=calendar,
        funding=funding_5,
        cfg=tight,
        start=date(2019, 1, 28),
        end=date(2019, 2, 12),
    )
    held = result.constraints[result.constraints["n_positions"] > 0]
    assert len(held) > 0
    # 闸门允许建仓时，结算日现金必须在下限内（R32：不能先成交、等结算后才报违规）
    traded_days = set(pd.to_datetime(result.trades["trade_date"]).dt.date)
    check = result.constraints.copy()
    check["date"] = pd.to_datetime(check["date"]).dt.date
    executed_days = check[check["date"].isin(traded_days)]
    assert "cash_floor" not in executed_days["violation_codes"].str.cat(sep=";")
    # 之后由 5% 融资在长假期间产生的被动现金消耗可以出现 cash_floor，但必须是
    # **无成交日**的被动违规（引擎不会为了"修复被动违规"而违规成交）；如实报告。
    passive = check[(check["n_trades"] == 0) & ~check["ok"]]
    if len(passive):
        assert set(passive["violation_codes"]) == {"cash_floor"}
    floor = float(tight.constraints.min_cash_fraction) * result.exposure["nav"]
    assert (result.exposure["cash"] >= floor - 1e-6).min() or bool(len(passive))
    assert result.exposure["cash"].min() < -8_000_000  # 确实跨过了长假的负现金区间


def test_r32_settlement_lag_0_1_2(market) -> None:
    """R32：T+0/T+1/T+2 下成交结算日与账本一致，且闸门投影覆盖结算日。"""
    cfg, panel, funding, calendar = market
    for lag in (0, 1, 2):
        lag_cfg = cfg.model_copy(
            update={"pricing": cfg.pricing.model_copy(update={"settlement_lag_days": lag})}
        )
        result = run_backtest(
            _ZeroTilt(),
            panel=panel,
            calendar=calendar,
            funding=funding,
            cfg=lag_cfg,
            start=START,
            end=date(2019, 2, 15),
        )
        assert len(result.trades) > 0
        for trade in result.trades.itertuples():
            assert trade.settlement_date == calendar.settlement_date(trade.trade_date, lag)
        assert result.constraints["ok"].all()


# ---------------------------------------------------------------------------
# R37：成交前闸门必须覆盖 T+lag 结算时点，且逐时点检查（不只看最后快照）
# ---------------------------------------------------------------------------
def test_r37_gate_checks_settlement_and_intermediate_snapshots(market) -> None:
    """T+2 + cash_floor=0：结算日现金为负的建仓必须在成交前被拒（不能事后违规）。"""
    cfg, panel, funding, calendar = market
    lag_cfg = cfg.model_copy(
        update={
            "pricing": cfg.pricing.model_copy(update={"settlement_lag_days": 2}),
            "constraints": cfg.constraints.model_copy(update={"min_cash_fraction": 0.0}),
        }
    )
    result = run_backtest(
        _ZeroTilt(),
        panel=panel,
        calendar=calendar,
        funding=funding,
        cfg=lag_cfg,
        start=START,
        end=date(2019, 2, 5),
    )
    # 不允许"先成交、结算日再报违规"：持仓日现金不得低于下限，且无 cash_floor 违规
    floor = float(lag_cfg.constraints.min_cash_fraction) * result.exposure["nav"]
    assert (result.exposure["cash"] >= floor - 1e-6).all()
    assert "cash_floor" not in result.constraints["violation_codes"].str.cat(sep=";")
    # 该配置下全量建仓必然违反 0 现金下限 → 闸门应拒单（可接受的替代是缩量，但当前策略无缩量路径）
    assert len(result.trades) == 0
    # 若仍有拒单记录，必须落在成交日前（不是结算日才拒）
    if len(result.rejected):
        assert (result.rejected["date"] <= date(2019, 2, 5)).all()


def test_r37_gate_uses_execution_day_single_bond_weight(market) -> None:
    """执行日单券权重（而非次日预测值）超限必须被闸门拦下。"""
    cfg, panel, funding, calendar = market

    class _Concentrated:
        name = "concentrated"

        def target_krd(self, ctx: MarketContext) -> TargetKRD:
            # 把全部 DV01 压到 10Y 桶 → 单券权重极可能 >30%
            total = float(ctx.benchmark.total_dv01)
            krd = {t: 0.0 for t in ctx.benchmark.krd}
            krd[10.0] = total
            return TargetKRD(as_of=ctx.as_of, krd=krd)

    result = run_backtest(
        _Concentrated(),
        panel=panel,
        calendar=calendar,
        funding=funding,
        cfg=cfg,
        start=START,
        end=date(2019, 2, 28),
    )
    held = result.constraints[result.constraints["n_positions"] > 0]
    assert "single_bond_weight" not in held["violation_codes"].str.cat(sep=";")


# ---------------------------------------------------------------------------
# R38：滚动触发日策略与基准必须同日显式换券（weekly 也不能延后一天）
# ---------------------------------------------------------------------------
def test_r38_weekly_strategy_rolls_same_day_as_benchmark(market) -> None:
    cfg, panel, funding, calendar = market
    rolling = cfg.portfolio.rolling.model_copy(update={"enabled": True})
    rolling_cfg = cfg.model_copy(
        update={
            "portfolio": cfg.portfolio.model_copy(update={"rolling": rolling}),
            "backtest": cfg.backtest.model_copy(
                update={"benchmark_version": "rolling", "rebalance": "weekly"}
            ),
        }
    )
    result = run_backtest(
        _ZeroTilt(),
        panel=panel,
        calendar=calendar,
        funding=funding,
        cfg=rolling_cfg,
        start=START,
        end=date(2021, 12, 31),
    )
    roll_trades = result.benchmark_trades[
        result.benchmark_trades["trade_id"].str.startswith("bench-roll")
    ]
    assert len(roll_trades) >= 2
    bench_dates = set(pd.to_datetime(roll_trades["trade_date"]).dt.date)
    # 每个基准换券日，策略都必须有成交（卖旧/买新），不能延后到下一交易日
    strategy_dates = set(pd.to_datetime(result.trades["trade_date"]).dt.date)
    assert bench_dates <= strategy_dates
    for d in bench_dates:
        codes = set(result.trades.loc[result.trades["trade_date"] == d, "bond_code"])
        renamed = set(roll_trades.loc[roll_trades["trade_date"] == d, "bond_code"])
        assert renamed <= codes
    # 换券当日不应出现"基准已换、策略未换"的拒单
    rejected_dates = (
        set(pd.to_datetime(result.rejected["date"]).dt.date) if len(result.rejected) else set()
    )
    assert not (bench_dates & rejected_dates)
