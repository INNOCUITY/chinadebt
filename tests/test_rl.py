"""RL 环境接口测试：零动作等价、动作/成交差异、reward 口径、roll 事件、as-of 无未来。

全部断言针对**接口纪律**，不评价策略好坏；2019–2025 是已研究样本，不称 holdout。
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from ficc.backtest.engine import run_backtest
from ficc.config import Config, get_config
from ficc.data.loader import load_curve_long, load_funding_series
from ficc.data.schema import CANONICAL_TENORS
from ficc.portfolio.types import MarketContext, TargetKRD
from ficc.rl import ActionSpec, Observation, RebalanceEnv, StepRecord, hold_target_krd
from ficc.utils.calendar import TradingCalendar

START = date(2019, 1, 28)
END = date(2019, 2, 15)


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


class _ScheduledStrategy:
    """测试用独立策略：按 signal_date 查表；无动作时保持当前 KRD（零动作）。"""

    name = "rl_env"

    def __init__(
        self,
        schedule: dict[date, ActionSpec],
        *,
        calendar: TradingCalendar,
        cfg: Config,
    ) -> None:
        self._schedule = schedule
        self._calendar = calendar
        self._cfg = cfg

    def target_krd(self, ctx: MarketContext) -> TargetKRD:
        action = self._schedule.get(ctx.as_of)
        if action is None or action.target_krd is None:
            return hold_target_krd(ctx, calendar=self._calendar, cfg=self._cfg)
        return TargetKRD(
            as_of=ctx.as_of, krd=dict(action.target_krd), notes=action.notes or "RL 环境动作"
        )


def test_zero_action_equivalence(market) -> None:
    cfg, panel, funding, calendar = market
    env = RebalanceEnv(panel, calendar, funding, cfg)
    obs0 = env.reset(START, END)
    assert obs0.nav == pytest.approx(cfg.backtest.init_cash)
    assert obs0.cash == pytest.approx(cfg.backtest.init_cash)
    assert obs0.history.index.max() <= pd.Timestamp(obs0.as_of)

    bench_target = dict(obs0.benchmark_krd)
    obs1, rec1 = env.step(ActionSpec(target_krd=bench_target, notes="回到基准"))
    assert rec1.original_action.target_krd is not None
    assert rec1.projected_target is not None
    assert rec1.projected_target.as_of == obs0.as_of
    assert rec1.executed_trades, "回到基准动作应有成交"
    assert rec1.executed_face > 0.0
    for trade in rec1.executed_trades:
        assert trade["signal_date"] < trade["trade_date"] == rec1.date

    obs2, rec2 = env.step(ActionSpec())  # 零动作
    assert rec2.original_action.target_krd is None
    assert rec2.reward == pytest.approx(rec2.nav_after - rec2.nav_before, abs=0.0)
    assert obs2.nav == pytest.approx(rec2.nav_after, abs=1e-12)
    assert isinstance(obs1, Observation) and isinstance(rec2, StepRecord)

    # 对照：直接跑一次同区间、同 schedule 的回测
    schedule = {obs0.as_of: ActionSpec(target_krd=bench_target), obs1.as_of: ActionSpec()}
    direct = run_backtest(
        _ScheduledStrategy(schedule, calendar=calendar, cfg=env.cfg),
        panel=panel,
        calendar=calendar,
        funding=funding,
        cfg=env.cfg,
        start=START,
        end=obs2.as_of,
    )
    assert env.result is not None
    np.testing.assert_allclose(
        env.result.nav.to_numpy(), direct.nav.to_numpy(), rtol=0.0, atol=1e-9
    )
    assert env.result.nav.index.equals(direct.nav.index)
    assert env.result.nav.index[-1] == obs2.as_of

    # as-of：观测只含 ≤ as_of 的数据
    assert obs2.history.index.max() == pd.Timestamp(obs2.as_of)
    assert obs2.curve.as_of == obs2.as_of
    assert obs2.history.equals(panel.loc[: pd.Timestamp(obs2.as_of)])
    assert env.done is False


def test_two_zero_steps_match_direct_backtest(market) -> None:
    """连续两次零动作：环境与直接 run_backtest 同区间 NAV 逐位一致（容差 1e-9）。"""
    cfg, panel, funding, calendar = market
    env = RebalanceEnv(panel, calendar, funding, cfg)
    obs0 = env.reset(START, END)
    assert ActionSpec(target_krd={}).target_krd is None  # 空映射按零动作处理
    obs1, _rec1 = env.step(ActionSpec())
    obs2, _rec2 = env.step(ActionSpec())

    schedule = {obs0.as_of: ActionSpec(), obs1.as_of: ActionSpec()}
    direct = run_backtest(
        _ScheduledStrategy(schedule, calendar=calendar, cfg=env.cfg),
        panel=panel,
        calendar=calendar,
        funding=funding,
        cfg=env.cfg,
        start=START,
        end=obs2.as_of,
    )
    assert env.result is not None
    np.testing.assert_allclose(
        env.result.nav.to_numpy(), direct.nav.to_numpy(), atol=1e-9, rtol=0.0
    )
    assert env.result.nav.index.equals(direct.nav.index)


def test_rejected_action_records_difference(market) -> None:
    cfg, panel, funding, calendar = market
    env = RebalanceEnv(panel, calendar, funding, cfg)
    obs0 = env.reset(START, END)
    huge = {tenor: 1000.0 * value for tenor, value in obs0.benchmark_krd.items()}
    obs1, record = env.step(ActionSpec(target_krd=huge, notes="1000x 基准（必被闸门拒绝）"))

    assert record.original_action.target_krd is not None
    assert record.projected_target is not None
    assert record.projected_target.total_dv01 == pytest.approx(float(sum(huge.values())))
    assert record.rejected is True
    assert record.reject_reason
    assert record.executed_trades == ()
    assert record.executed_face == 0.0
    # 动作与执行差异被显式记录：原始动作非零、实际成交为空、原因非空
    assert record.reward == pytest.approx(record.nav_after - record.nav_before, abs=0.0)
    assert obs1.nav == pytest.approx(record.nav_after, abs=1e-12)

    events = env.events
    assert any(event.kind == "reject" and event.date == record.date for event in events)
    assert any(event.kind == "project" and event.date == record.date for event in events)
    reject_event = next(event for event in events if event.kind == "reject")
    assert "投影" in reject_event.detail["reason"] or reject_event.detail["reason"]


def test_scaled_action_executes_less_than_original(market) -> None:
    """5x 基准动作：闸门缩放后成交，成交规模明显小于原始动作（差异可审计）。"""
    cfg, panel, funding, calendar = market
    env = RebalanceEnv(panel, calendar, funding, cfg)
    obs0 = env.reset(START, END)
    five_x = {tenor: 5.0 * value for tenor, value in obs0.benchmark_krd.items()}
    _obs1, record = env.step(ActionSpec(target_krd=five_x, notes="5x 基准"))

    assert record.projected_target is not None
    assert record.projected_target.total_dv01 == pytest.approx(float(sum(five_x.values())))
    if record.rejected:
        assert record.executed_trades == ()
    else:
        # 若闸门缩放后成交：成交面值远低于 5x 目标隐含的规模
        assert record.executed_trades
        assert record.executed_face < 2.0 * cfg.backtest.init_cash
    assert record.gate_level <= 1.0


def test_reward_matches_ledger_nav_and_roll_events(market) -> None:
    cfg, panel, funding, calendar = market
    # 强制在短窗口内触发 2Y 滚动：trigger 1.99 年 → 约 5 个交易日后剩余期限 < 1.99
    rolling = cfg.portfolio.rolling.model_copy(update={"enabled": True, "roll_trigger_years": 1.99})
    cfg_roll = cfg.model_copy(
        update={
            "portfolio": cfg.portfolio.model_copy(update={"rolling": rolling}),
            "backtest": cfg.backtest.model_copy(update={"benchmark_version": "rolling"}),
        }
    )
    env = RebalanceEnv(panel, calendar, funding, cfg_roll)
    env.reset(START, END)

    saw_roll = False
    for _ in range(8):
        if env.done:
            break
        obs, record = env.step(ActionSpec())  # 零动作，只让基准/政策滚动
        assert record.reward == pytest.approx(record.nav_after - record.nav_before, abs=0.0)
        assert obs.nav == pytest.approx(record.nav_after, abs=1e-12)
        assert obs.as_of == record.date
        assert obs.history.index.max() <= pd.Timestamp(obs.as_of)
        if any(event.kind == "roll" for event in env.events):
            saw_roll = True
            break

    assert saw_roll, "短窗口内应出现滚动事件（roll_trigger_years=1.99）"
    rolls = [event for event in env.events if event.kind == "roll"]
    assert rolls
    for event in rolls:
        assert event.detail["tenor"] > 0
        assert event.detail["retired_code"]
        assert event.detail["issued_code"]
        assert event.date <= env.current_date
    # 事件按时间序
    dates = [event.date for event in env.events]
    assert dates == sorted(dates)


def test_events_cover_signal_execute_settle(market) -> None:
    cfg, panel, funding, calendar = market
    env = RebalanceEnv(panel, calendar, funding, cfg)
    obs0 = env.reset(START, END)
    env.step(ActionSpec(target_krd=dict(obs0.benchmark_krd), notes="回到基准"))
    env.step(ActionSpec())  # 再推进一天，让 T+1 结算事件进入账本
    kinds = {event.kind for event in env.events}
    assert {"signal", "execute", "settle"} <= kinds
    for event in env.events:
        assert event.kind in {"signal", "project", "execute", "reject", "roll", "settle"}
        assert event.date >= START


def test_env_forces_daily_decisions(market) -> None:
    cfg, panel, funding, calendar = market
    weekly = cfg.model_copy(
        update={"backtest": cfg.backtest.model_copy(update={"rebalance": "weekly"})}
    )
    env = RebalanceEnv(panel, calendar, funding, weekly)
    assert env.cfg.backtest.rebalance == "daily"
    env.reset(START, END)
    _obs, record = env.step(ActionSpec())
    assert record.projected_target is not None  # 每个 step 都能生成信号


def test_step_requires_reset_and_respects_end(market) -> None:
    cfg, panel, funding, calendar = market
    env = RebalanceEnv(panel, calendar, funding, cfg)
    with pytest.raises(RuntimeError):
        env.step(ActionSpec())
    days = calendar.trading_days_between(START, date(2019, 1, 30))
    env.reset(START, date(2019, 1, 30))
    env.step(ActionSpec())
    env.step(ActionSpec())
    assert env.done is True
    with pytest.raises(RuntimeError):
        env.step(ActionSpec())
    assert env.records[-1].date == days[-1]
