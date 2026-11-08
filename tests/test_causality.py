"""因果性（无未来信息）端到端测试（计划 §12.10 第 1 条扩展）。

只检查"传给策略的历史切片"不足以证明无泄漏：引擎还消费缓存残差面板、融资序列、
质量标记等。这里做**未来数据扰动**实验：把 t 之后的所有输入改成完全不同的值，
要求截止 t 的信号、成交、净值前缀逐字节不变。
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from ficc.backtest.engine import run_backtest
from ficc.config import get_config
from ficc.data.loader import load_curve_long, load_funding_series
from ficc.data.schema import CANONICAL_TENORS
from ficc.utils.calendar import TradingCalendar

START = date(2019, 1, 28)
CUTOFF = date(2019, 3, 1)  # 扰动该日之后的未来数据
END = date(2019, 3, 29)


class _ZeroTilt:
    name = "zero"

    def target_krd(self, ctx):
        from ficc.portfolio.types import TargetKRD

        return TargetKRD(as_of=ctx.as_of, krd=dict(ctx.benchmark.krd))


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


def _perturb_future(panel: pd.DataFrame, funding: pd.Series) -> tuple[pd.DataFrame, pd.Series]:
    """把 CUTOFF 之后的曲线整体上移 150bp、资金利率 +300bp（彻底改变未来路径）。"""
    panel2 = panel.copy()
    future = panel2.index > pd.Timestamp(CUTOFF)
    panel2.loc[future, :] = panel2.loc[future, :] * 1.5
    funding2 = funding.copy()
    funding2.loc[funding2.index > pd.Timestamp(CUTOFF)] = (
        funding2.loc[funding2.index > pd.Timestamp(CUTOFF)] + 3.0
    )
    return panel2, funding2


def test_future_perturbation_does_not_change_past(market) -> None:
    """扰动未来曲线/融资：截止 CUTOFF 的信号、成交、净值必须完全相同。"""
    cfg, panel, funding, calendar = market
    base = run_backtest(
        _ZeroTilt(), panel=panel, calendar=calendar, funding=funding, cfg=cfg, start=START, end=END
    )
    panel2, funding2 = _perturb_future(panel, funding)
    pert = run_backtest(
        _ZeroTilt(),
        panel=panel2,
        calendar=calendar,
        funding=funding2,
        cfg=cfg,
        start=START,
        end=END,
    )

    # 1) 净值前缀
    nav_base = base.nav.loc[base.nav.index <= CUTOFF]
    nav_pert = pert.nav.loc[pert.nav.index <= CUTOFF]
    assert nav_base.index.equals(nav_pert.index)
    np.testing.assert_allclose(nav_base.to_numpy(), nav_pert.to_numpy(), rtol=0, atol=1e-9)

    # 2) 成交前缀（含价格与面值）
    trades_base = base.trades[base.trades["trade_date"] <= CUTOFF].reset_index(drop=True)
    trades_pert = pert.trades[pert.trades["trade_date"] <= CUTOFF].reset_index(drop=True)
    assert len(trades_base) == len(trades_pert)
    for column in ("trade_id", "side", "face_value", "clean_price", "settlement_date"):
        assert trades_base[column].tolist() == trades_pert[column].tolist(), column

    # 3) 信号前缀（目标 DV01 与调仓理由）
    sig_base = base.signals[base.signals["date"] <= CUTOFF].reset_index(drop=True)
    sig_pert = pert.signals[pert.signals["date"] <= CUTOFF].reset_index(drop=True)
    assert len(sig_base) == len(sig_pert)
    np.testing.assert_allclose(
        sig_base["target_total_dv01"].to_numpy(),
        sig_pert["target_total_dv01"].to_numpy(),
        rtol=1e-12,
        atol=1e-9,
    )
    assert sig_base["notes"].tolist() == sig_pert["notes"].tolist()

    # 4) 敞口前缀
    exp_base = base.exposure[base.exposure["date"] <= CUTOFF].reset_index(drop=True)
    exp_pert = pert.exposure[pert.exposure["date"] <= CUTOFF].reset_index(drop=True)
    for column in ("nav", "cash", "total_dv01", "duration"):
        np.testing.assert_allclose(
            exp_base[column].to_numpy(), exp_pert[column].to_numpy(), rtol=0, atol=1e-9
        )


def test_future_residual_cache_does_not_change_past_rv(market) -> None:
    """RV 的残差面板是"缓存输入"：扰动未来残差不得改变历史信号/成交。"""
    from ficc.curve.fitting import fit_panel
    from ficc.strategy.relative_value import RelativeValueStrategy

    cfg, panel, funding, calendar = market
    _, residual_bp = fit_panel(
        panel,
        method=cfg.curve.fit_method,
        weights=cfg.curve.weights,
        param_mode="free",
        residuals_mode="free",
    )
    residual_bp.columns = [float(c) for c in residual_bp.columns]
    observed_minus_fitted = -residual_bp

    base = run_backtest(
        RelativeValueStrategy(residuals=observed_minus_fitted),
        panel=panel,
        calendar=calendar,
        funding=funding,
        cfg=cfg,
        start=START,
        end=END,
    )

    tampered = observed_minus_fitted.copy()
    tampered.loc[tampered.index > pd.Timestamp(CUTOFF), :] = 999.0
    pert = run_backtest(
        RelativeValueStrategy(residuals=tampered),
        panel=panel,
        calendar=calendar,
        funding=funding,
        cfg=cfg,
        start=START,
        end=END,
    )

    sig_base = base.signals[base.signals["date"] <= CUTOFF].reset_index(drop=True)
    sig_pert = pert.signals[pert.signals["date"] <= CUTOFF].reset_index(drop=True)
    assert len(sig_base) == len(sig_pert)
    assert sig_base["notes"].tolist() == sig_pert["notes"].tolist()
    trades_base = base.trades[base.trades["trade_date"] <= CUTOFF].reset_index(drop=True)
    trades_pert = pert.trades[pert.trades["trade_date"] <= CUTOFF].reset_index(drop=True)
    assert len(trades_base) == len(trades_pert)
    assert trades_base["trade_id"].tolist() == trades_pert["trade_id"].tolist()


def test_weekly_rebalance_only_trades_on_weekly_dates(market) -> None:
    """weekly 调仓：信号只出现在每周第一个交易日，成交笔数应少于 daily。"""
    cfg, panel, funding, calendar = market
    daily = run_backtest(
        _ZeroTilt(), panel=panel, calendar=calendar, funding=funding, cfg=cfg, start=START, end=END
    )
    weekly_cfg = cfg.model_copy(
        update={"backtest": cfg.backtest.model_copy(update={"rebalance": "weekly"})}
    )
    weekly = run_backtest(
        _ZeroTilt(),
        panel=panel,
        calendar=calendar,
        funding=funding,
        cfg=weekly_cfg,
        start=START,
        end=END,
    )
    assert len(weekly.signals) < len(daily.signals)
    # 每个信号日必须是该 ISO 周的第一个交易日
    traded_weeks = {(d.isocalendar()[0], d.isocalendar()[1]) for d in weekly.signals["date"]}
    first_trading_days = set()
    for week in sorted(traded_weeks):
        days = [
            d for d in weekly.signals["date"] if (d.isocalendar()[0], d.isocalendar()[1]) == week
        ]
        if days:
            first_trading_days.add(min(days))
    assert set(weekly.signals["date"]) == first_trading_days


def test_rolling_future_perturbation_does_not_change_past(market) -> None:
    """R37/R38：滚动模式下扰动未来曲线/融资，截止 CUTOFF 的净值与成交前缀不变。"""
    cfg, panel, funding, calendar = market
    rolling = cfg.portfolio.rolling.model_copy(update={"enabled": True})
    rolling_cfg = cfg.model_copy(
        update={
            "portfolio": cfg.portfolio.model_copy(update={"rolling": rolling}),
            "backtest": cfg.backtest.model_copy(update={"benchmark_version": "rolling"}),
        }
    )
    end = date(2020, 3, 31)
    base = run_backtest(
        _ZeroTilt(),
        panel=panel,
        calendar=calendar,
        funding=funding,
        cfg=rolling_cfg,
        start=START,
        end=end,
    )
    # 扰动必须发生在比较截止日之后（首次换券前：2020-01-31）
    cutoff = date(2020, 1, 31)
    panel2 = panel.copy()
    future = panel2.index > pd.Timestamp(cutoff)
    panel2.loc[future, :] = panel2.loc[future, :] * 1.5
    funding2 = funding.copy()
    funding2.loc[funding2.index > pd.Timestamp(cutoff)] = (
        funding2.loc[funding2.index > pd.Timestamp(cutoff)] + 3.0
    )
    pert = run_backtest(
        _ZeroTilt(),
        panel=panel2,
        calendar=calendar,
        funding=funding2,
        cfg=rolling_cfg,
        start=START,
        end=end,
    )
    nav_base = base.nav.loc[base.nav.index <= cutoff]
    nav_pert = pert.nav.loc[pert.nav.index <= cutoff]
    assert nav_base.index.equals(nav_pert.index)
    np.testing.assert_allclose(nav_base.to_numpy(), nav_pert.to_numpy(), rtol=0, atol=1e-9)
    trades_base = base.trades[base.trades["trade_date"] <= cutoff].reset_index(drop=True)
    trades_pert = pert.trades[pert.trades["trade_date"] <= cutoff].reset_index(drop=True)
    assert len(trades_base) == len(trades_pert)
    for column in ("trade_id", "side", "face_value", "clean_price", "settlement_date"):
        assert trades_base[column].tolist() == trades_pert[column].tolist(), column
