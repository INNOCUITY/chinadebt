"""R51：RL 策略信息时钟（lag=1）与决策网格（5 日）的独立验收。

与 ``test_rl.py`` / ``test_rl_contract.py`` 一样只断言**接口纪律**：

- ``RebalanceEnv.reset`` 的观测：持仓/现金/NAV 是 as_of 当日收盘状态，而
  ``curve/history/benchmark`` 截止 ``available_through = as_of − lag``；
- 策略调用上下文（rollout wrapper；step 的 scripted wrapper 共用 ``_LaggedView``）
  也只看到滞后信息；修改 ``available_through`` 之后的曲线不得改变决策日 t 的
  policy 特征或原始动作；
- ``decision_step_days=5`` 时只有索引 % 5 == 0 的交易日产生目标，其余日强制
  ``off_grid_hold``（无主动成交），且 step 与 rollout 逐字段一致；
- ``information_lag_days=0`` 时保持旧 as-of 行为（回归兼容）。

2019–2025 是已研究样本，本测试不评价策略好坏。
"""

from __future__ import annotations

from datetime import date

import pandas as pd
import pytest

from ficc.config import Config, get_config
from ficc.data.loader import load_curve_long, load_funding_series
from ficc.data.schema import CANONICAL_TENORS
from ficc.portfolio.types import MarketContext, TargetKRD
from ficc.rl import ActionSpec, RebalanceEnv, StepRecord
from ficc.rl.env import _benchmark_portfolio, _curve_at, _LaggedView
from ficc.utils.calendar import TradingCalendar

START = date(2019, 1, 28)
END = date(2019, 2, 15)
AVAILABLE = date(2019, 1, 25)


@pytest.fixture(scope="module")
def market() -> tuple[Config, pd.DataFrame, pd.Series, TradingCalendar]:
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


def _recording_policy(sink: dict[date, dict[str, object]], notes: str = "clock"):
    """记录 policy 实际看到的特征与原始动作，并返回基于滞后基准 KRD 的目标。"""

    def policy(ctx: MarketContext) -> TargetKRD:
        sink[ctx.as_of] = {
            "curve_as_of": ctx.curve.as_of,
            "history_max": ctx.history.index.max().date(),
            "history": ctx.history.copy(),
            "benchmark_krd": dict(ctx.benchmark.krd),
            "benchmark_as_of": ctx.benchmark.as_of,
            "rate_10y": float(ctx.curve.rate(10.0)),
            "action": ActionSpec(target_krd=dict(ctx.benchmark.krd), notes=notes),
        }
        return TargetKRD(as_of=ctx.as_of, krd=dict(ctx.benchmark.krd), notes=notes)

    return policy


# ---------------------------------------------------------------------------
# R51：观测信息截止
# ---------------------------------------------------------------------------
def test_reset_observation_cutoff_and_metadata(market) -> None:
    """2019-01-28 reset：曲线/历史只到 01-25；持仓/NAV 仍是 01-28 状态。"""
    cfg, panel, funding, calendar = market
    env = RebalanceEnv(panel, calendar, funding, cfg)
    obs = env.reset(START, END)

    assert obs.as_of == START
    assert obs.metadata["as_of"] == START.isoformat()
    assert obs.metadata["available_through"] == AVAILABLE.isoformat()
    assert obs.metadata["information_lag_days"] == 1
    assert obs.metadata["decision_step_days"] == 1
    assert obs.metadata["lag_clamped"] is False

    # R51 验收：history.max() <= 01-25，curve.as_of == available_through
    assert obs.history.index.max() <= pd.Timestamp(AVAILABLE)
    assert obs.history.index.max() == pd.Timestamp(AVAILABLE)
    assert obs.curve.as_of == AVAILABLE
    assert obs.history.equals(panel.loc[: pd.Timestamp(AVAILABLE)])

    # 持仓/现金/NAV 仍可用 t 日收盘状态（reset 首日为空仓、初始现金）
    assert obs.nav == pytest.approx(float(cfg.backtest.init_cash))
    assert obs.cash == pytest.approx(float(cfg.backtest.init_cash))
    assert obs.positions.empty

    # 滞后基准 KRD 由 01-25 曲线/券池重建，不是 01-28 的
    tenors = tuple(float(t) for t in CANONICAL_TENORS)
    lagged_benchmark = _benchmark_portfolio(panel, tenors, calendar, cfg, AVAILABLE)
    assert env.available_through(START) == AVAILABLE
    assert obs.benchmark_krd == pytest.approx(dict(lagged_benchmark.krd))

    # lag=0 回归：与旧实现一致（曲线/历史/基准都用当日）
    legacy = RebalanceEnv(panel, calendar, funding, cfg, information_lag_days=0)
    obs0 = legacy.reset(START, END)
    assert obs0.curve.as_of == START
    assert obs0.history.index.max() == pd.Timestamp(START)
    assert obs0.history.equals(panel.loc[: pd.Timestamp(START)])
    assert obs0.metadata["available_through"] == START.isoformat()


def test_lagged_view_keeps_current_positions_and_nav(market) -> None:
    """滞后上下文只替换 curve/history/benchmark；nav/cash/positions 保持 t 日状态。"""
    cfg, panel, _funding, calendar = market
    tenors = tuple(float(t) for t in CANONICAL_TENORS)
    view = _LaggedView(
        panel=panel,
        tenors=tenors,
        calendar=calendar,
        cfg=cfg,
        information_lag_days=1,
    )
    day = date(2019, 1, 29)
    ctx = MarketContext(
        as_of=day,
        curve=_curve_at(panel, tenors, day),
        history=panel.loc[: pd.Timestamp(day)],
        benchmark=_benchmark_portfolio(panel, tenors, calendar, cfg, day),
        nav=123.0,
        cash=45.0,
        positions=(),
        cfg=cfg,
    )
    lagged = view.context(ctx)
    assert lagged.as_of == day  # 决策日/信号日语义不变
    assert lagged.curve.as_of == date(2019, 1, 28)
    assert lagged.history.index.max() == pd.Timestamp(date(2019, 1, 28))
    assert lagged.benchmark.as_of == date(2019, 1, 28)
    assert lagged.nav == 123.0
    assert lagged.cash == 45.0
    assert lagged.positions is ctx.positions
    # lag=0 时原样返回同一对象（旧行为逐位不变）
    same = _LaggedView(
        panel=panel, tenors=tenors, calendar=calendar, cfg=cfg, information_lag_days=0
    )
    assert same.context(ctx) is ctx


def test_policy_context_is_lagged_and_future_data_does_not_leak(market) -> None:
    """01-28 决策看不到 01-28 曲线：改动 01-28 的 10Y 不改变 policy 特征/原始动作。"""
    cfg, panel, funding, calendar = market
    perturbed = panel.copy()
    # 只改决策截止日（available_through=01-25）之后的数据
    perturbed.loc[pd.Timestamp(START), 10.0] = float(panel.loc[pd.Timestamp(START), 10.0]) + 1.0

    sink_a: dict[date, dict[str, object]] = {}
    sink_b: dict[date, dict[str, object]] = {}
    env_a = RebalanceEnv(panel, calendar, funding, cfg)
    env_b = RebalanceEnv(perturbed, calendar, funding, cfg)
    rollout_a = env_a.rollout(_recording_policy(sink_a), START, END)
    rollout_b = env_b.rollout(_recording_policy(sink_b), START, END)
    assert len(rollout_a.records) == len(rollout_b.records)

    first_a = sink_a[START]
    first_b = sink_b[START]
    assert first_a["curve_as_of"] == AVAILABLE
    assert first_a["history_max"] == AVAILABLE
    assert first_a["benchmark_as_of"] == AVAILABLE
    # 决策日 t 的 policy 特征与原始动作在截止日之后被改动时保持不变
    assert first_a["history"].equals(first_b["history"])
    assert first_a["benchmark_krd"] == first_b["benchmark_krd"]
    assert first_a["rate_10y"] == first_b["rate_10y"]
    assert first_a["action"] == first_b["action"]
    assert rollout_a.records[0].original_action == rollout_b.records[0].original_action

    # 边界恰为 lag=1：01-28 的扰动在 01-29 的决策视图（curve.as_of=01-28）中可见
    second_day = date(2019, 1, 29)
    assert sink_a[second_day]["curve_as_of"] == START
    assert sink_a[second_day]["rate_10y"] != sink_b[second_day]["rate_10y"]

    # 01-28 的 policy 看到的是 01-25 的 10Y，而非被改动的 01-28 值
    assert sink_a[START]["rate_10y"] == pytest.approx(
        float(panel.loc[pd.Timestamp(AVAILABLE), 10.0])
    )


# ---------------------------------------------------------------------------
# R51：5 日决策网格（step / rollout 一致）
# ---------------------------------------------------------------------------
def test_five_day_grid_forces_hold_off_grid_and_matches_rollout(market) -> None:
    """decision_step_days=5：只有索引 0/5/10 产生目标，其余日 off_grid_hold 无成交。"""
    cfg, panel, funding, calendar = market
    days = calendar.trading_days_between(START, END)
    expected = [index for index in range(len(days) - 1) if index % 5 == 0]
    assert expected == [0, 5, 10]

    step_env = RebalanceEnv(panel, calendar, funding, cfg, decision_step_days=5)
    obs0 = step_env.reset(START, END)
    target = dict(obs0.benchmark_krd)
    for index, day in enumerate(days[:-1]):
        assert step_env.is_decision_day(day) == (index in expected)
    step_records: list[StepRecord] = []
    step_env.reset(START, END)
    for _day in days[:-1]:
        # 每个交易日都提交目标：非网格日必须被强制改为 hold
        _obs, record = step_env.step(ActionSpec(target_krd=target, notes="网格信号"))
        step_records.append(record)

    calls: list[date] = []

    def policy(ctx: MarketContext) -> TargetKRD:
        calls.append(ctx.as_of)
        return TargetKRD(as_of=ctx.as_of, krd=target, notes="网格信号")

    roll_env = RebalanceEnv(panel, calendar, funding, cfg, decision_step_days=5)
    rollout = roll_env.rollout(policy, START, END)

    for label, records in (("step", step_records), ("rollout", list(rollout.records))):
        non_hold = [
            index for index, record in enumerate(records) if not record.original_action.is_hold
        ]
        assert non_hold == expected, label
        assert len(records) == len(days) - 1
        for index, record in enumerate(records):
            assert record.date == days[index + 1]
            if index in expected:
                continue
            assert record.original_action.is_hold, label
            assert "off_grid_hold" in record.notes, label
            assert record.executed_trades == (), label
            assert record.projected_target is None, label
            assert record.projected_faces == (), label
    # policy 只在网格日被调用（不调用 → 不可能产生目标）
    assert calls == [days[index] for index in expected]

    # step 与 rollout 逐字段一致（含 R55 三阶段面值与 off-grid notes）
    for step_record, roll_record in zip(step_records, rollout.records, strict=True):
        assert step_record.date == roll_record.date
        assert step_record.original_action == roll_record.original_action
        assert step_record.action_outcome == roll_record.action_outcome
        assert step_record.notes == roll_record.notes
        assert step_record.gate_attempts == roll_record.gate_attempts
        assert step_record.executed_face == pytest.approx(roll_record.executed_face)
        assert step_record.requested_faces == roll_record.requested_faces
        assert step_record.projected_faces == roll_record.projected_faces
        assert step_record.accepted_orders == roll_record.accepted_orders
        assert step_record.post_trade_faces == roll_record.post_trade_faces
        assert step_record.action_scale == pytest.approx(roll_record.action_scale, abs=1e-12)


def test_lag_aligns_with_decision_grid_indices(market) -> None:
    """网格索引以 episode 起始交易日为 0；lag 与网格互不干扰。"""
    cfg, panel, funding, calendar = market
    env = RebalanceEnv(panel, calendar, funding, cfg, information_lag_days=1, decision_step_days=5)
    obs = env.reset(START, END)
    days = calendar.trading_days_between(START, END)
    assert obs.metadata["decision_index"] == 0
    assert obs.metadata["on_decision_grid"] is True
    assert obs.metadata["available_through"] == AVAILABLE.isoformat()

    # 索引 0 是网格日：目标被接受（成交发生在下一交易日）
    obs1, record1 = env.step(ActionSpec(target_krd=dict(obs.benchmark_krd), notes="网格信号"))
    assert obs1.as_of == days[1]
    assert record1.original_action.action_type == "target_krd"
    assert "off_grid_hold" not in record1.notes

    # 索引 1 非网格日：调用方目标被忽略 → hold；观测信息截止 01-28（lag=1 对 01-30）
    obs2, record2 = env.step(ActionSpec(target_krd=dict(obs.benchmark_krd), notes="应被忽略"))
    assert obs2.as_of == days[2]
    assert obs2.metadata["decision_index"] == 2
    assert obs2.metadata["on_decision_grid"] is False
    assert obs2.metadata["available_through"] == days[1].isoformat()
    assert record2.original_action.is_hold
    assert record2.original_action.notes == "off_grid_hold"
    assert "off_grid_hold" in record2.notes
    assert record2.executed_trades == ()
