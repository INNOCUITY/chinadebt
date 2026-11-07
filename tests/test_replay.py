"""固定成交路径回放（``backtest/replay.py``）的验收测试。

核心验收（任务 §12.10 任务 A 第一条）
------------------------------------
1. 三开关全开时，回放 NAV 与引擎 NAV **逐日一致**（< 1e-6 元）；
2. 关 ``spread`` / ``fee`` / ``funding`` 的 NAV 改善分别 ≈ 对应成本（价差/费用/融资）；
3. 篡改成交面值后回放与引擎**不再一致**——说明回放不是自证；
4. 回放只看窗口内成交与当日曲线（截断窗口的前半段 NAV 与全窗口一致，不读未来）。
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pytest

from ficc.backtest.engine import run_backtest
from ficc.backtest.replay import (
    CostSwitches,
    ReplayResult,
    replay_engine_result,
    replay_trades,
)
from ficc.config import get_config
from ficc.data.loader import load_curve_long, load_funding_series
from ficc.data.schema import CANONICAL_TENORS
from ficc.strategy.carry_rolldown import CarryRollStrategy
from ficc.utils.calendar import TradingCalendar

START = date(2019, 1, 28)
END = date(2019, 2, 28)
TOL = 1e-6  # 元


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------
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


@pytest.fixture(scope="module")
def baseline(market):
    """一个月窗口的引擎基线：成交路径固定，供各开关重放。"""
    cfg, panel, funding, calendar = market
    return run_backtest(
        CarryRollStrategy(),
        panel=panel,
        calendar=calendar,
        funding=funding,
        cfg=cfg,
        start=START,
        end=END,
    )


@pytest.fixture(scope="module")
def full_replay(market, baseline) -> ReplayResult:
    cfg, panel, funding, calendar = market
    return replay_engine_result(
        baseline,
        panel=panel,
        calendar=calendar,
        funding=funding,
        cfg=cfg,
        switches=CostSwitches(),
    )


@pytest.fixture(scope="module")
def small_cash_cfg(market):
    """小初始现金配置：让账本出现负现金 → 融资成本非零（可验证 funding 开关）。"""
    cfg, _panel, _funding, _calendar = market
    return cfg.model_copy(update={"backtest": cfg.backtest.model_copy(update={"init_cash": 1e5})})


# ---------------------------------------------------------------------------
# 1) 核心：全开开关回放 == 引擎
# ---------------------------------------------------------------------------
def test_full_switches_replay_matches_engine_nav(market, baseline, full_replay) -> None:
    _cfg, _panel, _funding, _calendar = market
    assert len(full_replay.nav) == len(baseline.nav)
    assert full_replay.nav.index.equals(baseline.nav.index)
    max_error = float(np.max(np.abs(full_replay.nav.to_numpy() - baseline.nav.to_numpy())))
    assert max_error < TOL, f"回放 NAV 与引擎逐日误差 {max_error:.3e} 元（阈值 {TOL}）"
    # 成本口径也应逐日一致（fee/funding 来自同一批现金事件，spread 为披露口径）
    engine_costs = baseline.costs.set_index("date")
    replay_costs = full_replay.costs.set_index("date")
    for column in ("fee", "funding", "spread", "total"):
        error = float(np.max(np.abs(replay_costs[column] - engine_costs[column])))
        assert error < TOL, f"成本列 {column} 逐日误差 {error:.3e} 元"
    # 现金流事件总数与期初现金一致
    assert len(full_replay.cashflows) == len(baseline.cashflows)
    assert float(full_replay.cashflows["amount"].sum()) == pytest.approx(
        float(baseline.cashflows["amount"].sum()), abs=TOL
    )


def test_replay_engine_result_equals_replay_trades(market, baseline, full_replay) -> None:
    cfg, panel, funding, calendar = market
    via_trades = replay_trades(
        baseline.trades,
        panel=panel,
        calendar=calendar,
        funding=funding,
        cfg=cfg,
        start=baseline.start,
        end=baseline.end,
        switches=CostSwitches(),
    )
    assert np.max(np.abs(via_trades.nav.to_numpy() - full_replay.nav.to_numpy())) < TOL


# ---------------------------------------------------------------------------
# 2) 逐项关闭成本：NAV 改善 ≈ 对应成本
# ---------------------------------------------------------------------------
def test_spread_off_raises_nav_by_spread_cost(market, baseline, full_replay) -> None:
    cfg, panel, funding, calendar = market
    off = replay_engine_result(
        baseline,
        panel=panel,
        calendar=calendar,
        funding=funding,
        cfg=cfg,
        switches=CostSwitches(spread=False),
    )
    difference = off.nav - full_replay.nav
    # 逐日单调上升：每笔成交少付（买入）或多收（卖出）正好一笔价差
    assert float(difference.diff().min()) >= -1e-3
    spread_total = float(baseline.trades["spread_cost"].sum())
    assert spread_total > 0.0
    assert float(difference.iloc[-1]) == pytest.approx(spread_total, rel=1e-6)
    assert float(off.costs["spread"].sum()) == 0.0
    # 价差只披露、不重复扣：全开回放 NAV 已等于引擎 NAV（见上一条），此处再确认
    # 价差披露合计 == 成交表价差合计
    assert float(full_replay.costs["spread"].sum()) == pytest.approx(spread_total, rel=1e-9)


def test_fee_off_raises_nav_by_fee(market, baseline, full_replay) -> None:
    cfg, panel, funding, calendar = market
    off = replay_engine_result(
        baseline,
        panel=panel,
        calendar=calendar,
        funding=funding,
        cfg=cfg,
        switches=CostSwitches(fee=False),
    )
    fee_total = float(full_replay.costs["fee"].sum())
    assert fee_total > 0.0
    difference = float(off.nav.iloc[-1] - full_replay.nav.iloc[-1])
    assert difference == pytest.approx(fee_total, rel=1e-9, abs=TOL)
    assert float(off.costs["fee"].sum()) == 0.0


def test_funding_off_raises_nav_by_funding_cost(market, baseline, small_cash_cfg) -> None:
    """构造负现金场景：小初始现金 → 融资成本非零，关掉后 NAV 改善 = 融资成本。"""
    _cfg, panel, funding, calendar = market
    base = replay_trades(
        baseline.trades,
        panel=panel,
        calendar=calendar,
        funding=funding,
        cfg=small_cash_cfg,
        start=baseline.start,
        end=baseline.end,
    )
    off = replay_trades(
        baseline.trades,
        panel=panel,
        calendar=calendar,
        funding=funding,
        cfg=small_cash_cfg,
        start=baseline.start,
        end=baseline.end,
        switches=CostSwitches(funding=False),
    )
    funding_total = float(base.costs["funding"].sum())
    assert funding_total > 0.0, "小初始现金场景应产生融资成本（否则测试不成立）"
    difference = float(off.nav.iloc[-1] - base.nav.iloc[-1])
    assert difference == pytest.approx(funding_total, rel=1e-9, abs=TOL)
    assert float(off.costs["funding"].sum()) == 0.0
    # 融资关闭不应改变成交集合/价差披露
    assert float(off.costs["spread"].sum()) == pytest.approx(
        float(base.costs["spread"].sum()), rel=1e-9
    )


def test_all_switches_off_saves_sum_of_costs(market, baseline, full_replay) -> None:
    """三项成本都关闭：NAV 改善 = 费用 + 融资 + 价差（本窗口融资为 0）。"""
    cfg, panel, funding, calendar = market
    off = replay_engine_result(
        baseline,
        panel=panel,
        calendar=calendar,
        funding=funding,
        cfg=cfg,
        switches=CostSwitches(spread=False, fee=False, funding=False),
    )
    saved = float(off.nav.iloc[-1] - full_replay.nav.iloc[-1])
    costs = full_replay.costs
    assert saved == pytest.approx(float(costs["total"].sum()), rel=1e-9, abs=TOL)


# ---------------------------------------------------------------------------
# 3) 篡改与兼容分支
# ---------------------------------------------------------------------------
def test_tampered_face_breaks_engine_equality(market, baseline) -> None:
    """把一笔买入面值放大 1%：回放 NAV 必须偏离引擎（回放不是自证）。"""
    cfg, panel, funding, calendar = market
    tampered = baseline.trades.copy()
    buy_index = tampered.index[tampered["side"] == "buy"][0]
    tampered.loc[buy_index, "face_value"] = float(tampered.loc[buy_index, "face_value"]) * 1.01
    replay = replay_trades(
        tampered,
        panel=panel,
        calendar=calendar,
        funding=funding,
        cfg=cfg,
        start=baseline.start,
        end=baseline.end,
    )
    max_error = float(np.max(np.abs(replay.nav.to_numpy() - baseline.nav.to_numpy())))
    assert max_error > 1.0, f"篡改面值后回放仍与引擎一致（{max_error:.3e}），回放可能自证"


def test_mid_price_fallback_without_exported_column(market, baseline, full_replay) -> None:
    """旧版导出没有 ``mid_clean_price`` 时，用 ``spread_cost`` 反推中间价的兼容分支。"""
    cfg, panel, funding, calendar = market
    trades = baseline.trades.drop(columns=["mid_clean_price"])
    off = replay_trades(
        trades,
        panel=panel,
        calendar=calendar,
        funding=funding,
        cfg=cfg,
        start=baseline.start,
        end=baseline.end,
        switches=CostSwitches(spread=False),
    )
    exact = replay_engine_result(
        baseline,
        panel=panel,
        calendar=calendar,
        funding=funding,
        cfg=cfg,
        switches=CostSwitches(spread=False),
    )
    assert float(np.max(np.abs(off.nav.to_numpy() - exact.nav.to_numpy()))) < 1e-4
    assert float(off.nav.iloc[-1]) > float(full_replay.nav.iloc[-1])


# ---------------------------------------------------------------------------
# 4) 不读未来：截断窗口的前半段与全窗口一致
# ---------------------------------------------------------------------------
def test_truncated_window_matches_full_window_until_cutoff(market, baseline) -> None:
    cfg, panel, funding, calendar = market
    days = list(calendar.trading_days_between(baseline.start, baseline.end))
    cutoff = days[len(days) // 2]
    partial = replay_engine_result(
        baseline,
        panel=panel,
        calendar=calendar,
        funding=funding,
        cfg=cfg,
        switches=CostSwitches(),
    )
    truncated = replay_trades(
        baseline.trades,
        panel=panel,
        calendar=calendar,
        funding=funding,
        cfg=cfg,
        start=baseline.start,
        end=cutoff,
        switches=CostSwitches(),
    )
    overlap = truncated.nav.index
    assert len(overlap) > 1
    aligned = partial.nav.reindex(overlap)
    assert aligned.notna().all()
    max_error = float(np.max(np.abs(truncated.nav.to_numpy() - aligned.to_numpy())))
    assert max_error < TOL, f"截断窗口前半段 NAV 偏离 {max_error:.3e} 元（疑似读未来）"
