"""RL 动作/替代审计/终止义务契约测试（R44/R45 后续）。

覆盖：
- ActionSpec 三种动作类型、互斥校验与向后兼容推断；
- target_faces 精确面值执行、target_krd 正常执行、hold 不交易；
- 拒单/缩放的 attempted/scale/action_outcome；
- 滚动日原动作 cancelled_by_policy、policy_actions 与 roll_events 一一对应；
- terminate() 的未结算成交/待结算现金/未结算费用与 NAV 对账；
- replay_days/replay_cost_seconds 性能披露；
- R47/R48：rollout 与 step 共用轨迹构造器，reward 按相邻估值日 NAV 差闭合，
  三种动作/原生目标/拒单/缩放/滚动的记录逐字段等价；
- R49：rollout 返回 TerminalState 并同步环境生命周期（新 env/复用 env、末日 T+1）。

全部断言针对接口纪律，不评价策略好坏；2019–2025 是已研究样本，不称 holdout。
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from ficc.backtest.engine import run_backtest
from ficc.config import Config, get_config
from ficc.curve.curve import Curve
from ficc.data.loader import load_curve_long, load_funding_series
from ficc.data.schema import CANONICAL_TENORS
from ficc.portfolio.benchmark import build_benchmark
from ficc.portfolio.rolling import RollingUniverse
from ficc.portfolio.types import MarketContext, TargetFaces, TargetKRD
from ficc.rl import (
    ActionSpec,
    PolicyAction,
    RebalanceEnv,
    StepRecord,
    TerminalState,
)
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
    """测试用独立策略：按 signal_date 查表；无动作返回 None（hold）。"""

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

    def target_krd(self, ctx: MarketContext) -> TargetKRD | None:
        action = self._schedule.get(ctx.as_of)
        if action is None or action.action_type == "hold":
            return None
        return TargetKRD(
            as_of=ctx.as_of,
            krd=dict(action.target_krd or {}),
            notes=action.notes or "RL 环境动作",
        )


def _faces_of(env: RebalanceEnv) -> dict[str, float]:
    positions = env.observation.positions
    return {str(row["bond_code"]): float(row["face_value"]) for _, row in positions.iterrows()}


# ---------------------------------------------------------------------------
# 动作类型
# ---------------------------------------------------------------------------
def test_action_type_inference_and_exclusive_validation() -> None:
    assert ActionSpec().action_type == "hold"
    assert ActionSpec().is_hold is True
    assert ActionSpec({2.0: 100.0}).action_type == "target_krd"  # 位置参数向后兼容
    assert ActionSpec(target_krd={2.0: 100.0}).action_type == "target_krd"
    assert ActionSpec(target_faces={"BOND": 1_000_000.0}).action_type == "target_faces"
    assert ActionSpec(target_krd={}).action_type == "hold"

    with pytest.raises(ValueError, match="hold"):
        ActionSpec(action_type="hold", target_krd={2.0: 1.0})
    with pytest.raises(ValueError, match="hold"):
        ActionSpec(action_type="hold", target_faces={"BOND": 1.0})
    with pytest.raises(ValueError, match="必须提供"):
        ActionSpec(action_type="target_krd")
    with pytest.raises(ValueError, match="必须提供"):
        ActionSpec(action_type="target_faces")
    with pytest.raises(ValueError, match="同时"):
        ActionSpec(target_krd={2.0: 1.0}, target_faces={"BOND": 1.0})
    with pytest.raises(ValueError, match="不得同时"):
        ActionSpec(
            action_type="target_krd",
            target_krd={2.0: 1.0},
            target_faces={"BOND": 1.0},
        )
    with pytest.raises(ValueError, match="非负"):
        ActionSpec(action_type="target_faces", target_faces={"BOND": -1.0})
    with pytest.raises(ValueError, match="关键期限"):
        ActionSpec(target_krd={0.0: 1.0})


# ---------------------------------------------------------------------------
# hold / target_krd / target_faces
# ---------------------------------------------------------------------------
def test_hold_records_no_trade_outcome(market) -> None:
    cfg, panel, funding, calendar = market
    env = RebalanceEnv(panel, calendar, funding, cfg)
    obs0 = env.reset(START, END)
    env.step(ActionSpec(target_krd=dict(obs0.benchmark_krd)))

    _obs, record = env.step(ActionSpec(notes="hold"))
    assert record.original_action.action_type == "hold"
    assert record.executed_trades == ()
    assert record.executed_face == 0.0
    assert record.projected_target is None
    assert record.projected_faces == ()
    assert record.policy_actions == ()
    assert record.action_outcome == "hold"  # hold 是"持仓不动"，不是成交/死区
    assert record.action_scale == 1.0
    assert "零动作" in record.notes


def test_target_krd_normal_execution_and_substitution_detail(market) -> None:
    cfg, panel, funding, calendar = market
    env = RebalanceEnv(panel, calendar, funding, cfg)
    obs0 = env.reset(START, END)
    _obs, record = env.step(
        ActionSpec(action_type="target_krd", target_krd=dict(obs0.benchmark_krd))
    )

    assert isinstance(record, StepRecord)
    assert record.original_action.action_type == "target_krd"
    assert record.action_outcome == "executed"
    assert record.projected_target is not None
    assert record.projected_target.as_of == obs0.as_of
    assert record.action_scale == pytest.approx(1.0, abs=1e-12)
    assert record.attempted == record.gate_attempts >= 1
    detail = record.substitution_detail
    assert detail["action_type"] == "target_krd"
    assert detail["original_faces"] is None
    assert detail["dv01_diff"] == pytest.approx(0.0, abs=1e-6)
    assert detail["executed_faces"]

    # 与直接整段回放逐位一致
    schedule = {obs0.as_of: ActionSpec(target_krd=dict(obs0.benchmark_krd))}
    direct = run_backtest(
        _ScheduledStrategy(schedule, calendar=calendar, cfg=env.cfg),
        panel=panel,
        calendar=calendar,
        funding=funding,
        cfg=env.cfg,
        start=START,
        end=record.date,
    )
    assert env.result is not None
    np.testing.assert_allclose(
        env.result.nav.to_numpy(), direct.nav.to_numpy(), rtol=0.0, atol=1e-9
    )


def test_target_faces_executes_exact_faces(market) -> None:
    cfg, panel, funding, calendar = market
    env = RebalanceEnv(panel, calendar, funding, cfg)
    obs0 = env.reset(START, END)
    env.step(ActionSpec(target_krd=dict(obs0.benchmark_krd)))

    current = _faces_of(env)
    assert current
    code = sorted(current)[0]
    target = dict(current)
    target[code] += 1_000_000.0  # 一手整手增量，可执行
    _obs, record = env.step(ActionSpec(target_faces=target, notes="精确面值"))

    assert record.original_action.action_type == "target_faces"
    assert record.action_outcome == "executed"
    assert record.projected_target is None  # 面值动作不伪造内部基准 KRD 为提交目标
    assert dict(record.projected_faces) == target
    assert record.executed_face == pytest.approx(1_000_000.0)
    assert record.executed_trades[0]["bond_code"] == code
    assert record.executed_trades[0]["side"] == "buy"
    assert record.action_scale == pytest.approx(1.0, abs=1e-12)
    detail = record.substitution_detail
    assert detail["original_faces"] == target
    assert detail["submitted_faces"] == target
    assert detail["projected_delta_faces"][code] == pytest.approx(1_000_000.0)
    assert detail["executed_faces"][code] == pytest.approx(1_000_000.0)
    assert detail["face_diff"] == pytest.approx(0.0, abs=1e-6)

    # 精确面值执行：成交后持仓面值等于目标
    assert _faces_of(env) == pytest.approx(target)


def test_target_faces_noop_is_no_trade(market) -> None:
    cfg, panel, funding, calendar = market
    env = RebalanceEnv(panel, calendar, funding, cfg)
    obs0 = env.reset(START, END)
    env.step(ActionSpec(target_krd=dict(obs0.benchmark_krd)))
    current = _faces_of(env)
    _obs, record = env.step(ActionSpec(target_faces=dict(current)))
    assert record.executed_trades == ()
    assert record.action_outcome == "no_trade_deadband"
    assert record.projected_faces  # 提交目标仍被记录
    assert record.action_scale == 1.0


# ---------------------------------------------------------------------------
# 拒单 / 缩放审计
# ---------------------------------------------------------------------------
def test_rejected_action_records_attempted_and_reason(market) -> None:
    cfg, panel, funding, calendar = market
    env = RebalanceEnv(panel, calendar, funding, cfg)
    obs0 = env.reset(START, END)
    huge = {tenor: 1000.0 * value for tenor, value in obs0.benchmark_krd.items()}
    _obs, record = env.step(ActionSpec(target_krd=huge, notes="1000x 基准"))

    assert record.action_outcome == "rejected"
    assert record.rejected is True
    assert record.reject_reason
    assert record.executed_trades == ()
    assert record.gate_attempts >= 1
    assert record.projected_target is not None
    assert record.projected_target.total_dv01 < float(sum(huge.values()))
    assert record.action_scale < 1.0
    detail = record.substitution_detail
    assert detail["reject_reason"] == record.reject_reason
    assert detail["dv01_diff"] < 0.0
    assert detail["executed_faces"] == {}


def test_scaled_action_records_scale(market) -> None:
    cfg, panel, funding, calendar = market
    env = RebalanceEnv(panel, calendar, funding, cfg)
    obs0 = env.reset(START, END)
    five_x = {tenor: 5.0 * value for tenor, value in obs0.benchmark_krd.items()}
    _obs, record = env.step(ActionSpec(target_krd=five_x, notes="5x 基准"))

    assert record.gate_attempts >= 1
    assert record.projected_target is not None
    if record.rejected:
        assert record.action_outcome == "rejected"
        assert record.executed_trades == ()
    else:
        assert record.executed_trades
        assert record.action_outcome == "scaled"
        assert 0.0 < record.action_scale < 1.0
        assert record.substitution_detail["dv01_diff"] < 0.0


# ---------------------------------------------------------------------------
# 滚动日：原动作 vs 机械换券
# ---------------------------------------------------------------------------
def test_roll_day_cancels_original_action_and_records_policy_actions(market) -> None:
    cfg, panel, funding, calendar = market
    rolling = cfg.portfolio.rolling.model_copy(update={"enabled": True, "roll_trigger_years": 1.99})
    cfg_roll = cfg.model_copy(
        update={
            "portfolio": cfg.portfolio.model_copy(update={"rolling": rolling}),
            "backtest": cfg.backtest.model_copy(update={"benchmark_version": "rolling"}),
        }
    )
    env = RebalanceEnv(panel, calendar, funding, cfg_roll)
    obs0 = env.reset(START, END)

    roll_record = None
    for _ in range(10):
        if env.done:
            break
        _obs, record = env.step(
            ActionSpec(target_krd=dict(obs0.benchmark_krd), notes="每日回到期初基准")
        )
        if record.policy_actions:
            roll_record = record
            break
    assert roll_record is not None, "短窗口内应出现机械换券（roll_trigger_years=1.99）"

    record = roll_record
    assert record.policy_source == "policy_roll"
    assert record.pending_status == "cancelled_by_roll"
    assert record.action_outcome == "cancelled_by_policy"
    assert record.projected_target is None  # policy roll 不挂到原动作下
    # 机械换券实际提交的逐腿面值只出现在 projected_faces（披露用途），不伪造原动作目标
    assert record.projected_faces
    submitted_codes = {code for code, _face in record.projected_faces}
    policy_codes = {action.retired_code for action in record.policy_actions} | {
        action.issued_code for action in record.policy_actions
    }
    assert submitted_codes <= policy_codes
    assert all(isinstance(code, str) and face > 0.0 for code, face in record.projected_faces)
    assert "policy_actions" in record.notes

    # policy_actions 与 result.roll_events 一一对应
    assert env.result is not None
    rolls = env.result.roll_events
    roll_rows = rolls.loc[pd.to_datetime(rolls["date"]).dt.date == record.date]
    assert len(roll_rows) == len(record.policy_actions) > 0
    for action, (_, row) in zip(record.policy_actions, roll_rows.iterrows(), strict=True):
        assert isinstance(action, PolicyAction)
        assert action.day == record.date
        assert action.tenor == float(row["tenor"])
        assert action.retired_code == str(row["retired_code"])
        assert action.issued_code == str(row["issued_code"])
        assert action.pending_status == str(row["pending_status"])
        assert action.status == str(row["status"])
        expected_ids = tuple(
            part for part in str(row.get("strategy_trade_ids", "")).split(";") if part
        )
        assert action.strategy_trade_ids == expected_ids
        assert isinstance(action.strategy_trade_ids, tuple)
        assert all(isinstance(trade_id, str) for trade_id in action.strategy_trade_ids)

    # 当日成交属于机械换券（若执行），trade_id 出现在 policy_actions 中
    policy_ids = {
        trade_id for action in record.policy_actions for trade_id in action.strategy_trade_ids
    }
    if record.executed_trades:
        assert {trade["trade_id"] for trade in record.executed_trades} <= policy_ids


# ---------------------------------------------------------------------------
# 终止义务
# ---------------------------------------------------------------------------
def test_terminal_state_discloses_unsettled_obligations(market) -> None:
    cfg, panel, funding, calendar = market
    days = calendar.trading_days_between(START, date(2019, 1, 31))
    episode_end = days[1]  # 两个交易日：末日成交、T+1 结算落在 episode 之外
    env = RebalanceEnv(panel, calendar, funding, cfg)
    obs0 = env.reset(START, episode_end)

    # 未结束时 terminate 必须拒绝（先用三天 episode 推进一天）
    unfinished = RebalanceEnv(panel, calendar, funding, cfg)
    unfinished_obs = unfinished.reset(START, days[2])
    unfinished.step(ActionSpec(target_krd=dict(unfinished_obs.benchmark_krd)))
    assert unfinished.done is False
    with pytest.raises(RuntimeError, match="尚未结束"):
        unfinished.terminate()

    obs, record = env.step(ActionSpec(target_krd=dict(obs0.benchmark_krd)))
    assert env.done is True
    assert record.date == episode_end
    assert record.executed_trades, "末日应有成交以形成未结算义务"

    state = env.terminate()
    assert isinstance(state, TerminalState)
    assert state.liquidation_assumption == "mark_to_market_no_forced_liquidation"
    assert state.nav_mark_to_market == pytest.approx(float(env.result.nav.loc[obs.as_of]))
    assert state.nav_mark_to_market == pytest.approx(record.nav_after, abs=1e-12)
    assert state.positions is not None and len(state.positions) > 0

    assert state.unsettled_trades
    assert all(item["settlement_date"] > obs.as_of for item in state.unsettled_trades)
    expected_cash = 0.0
    expected_fees = 0.0
    for item in state.unsettled_trades:
        assert item["trade_date"] <= obs.as_of
        assert item["settlement_amount"] > 0.0
        expected_cash += item["signed_cash"]
        expected_cash_sign = -1.0 if item["side"] == "buy" else 1.0
        assert np.sign(item["signed_cash"]) == expected_cash_sign
        expected_fees += item["fee"]
    assert state.unsettled_cash == pytest.approx(expected_cash)
    assert state.unsettled_cash < 0.0  # 建仓买入：未来付现
    assert state.accrued_fees == pytest.approx(expected_fees)
    assert state.accrued_fees > 0.0
    assert "未结算" in state.notes and "NAV" in state.notes

    # 与 exposure.pending_cash 对账，且 NAV 恒等式成立
    exposure = env.result.exposure
    row = exposure.loc[pd.to_datetime(exposure["date"]).dt.date == obs.as_of]
    assert state.unsettled_cash == pytest.approx(float(row["pending_cash"].iloc[0]))
    nav_identity = state.cash + float(state.positions["market_value"].sum()) + state.unsettled_cash
    assert nav_identity == pytest.approx(state.nav_mark_to_market, abs=1e-5)


# ---------------------------------------------------------------------------
# 性能披露与整段回放一致性
# ---------------------------------------------------------------------------
def test_performance_stats_and_replay_consistency(market) -> None:
    cfg, panel, funding, calendar = market
    env = RebalanceEnv(panel, calendar, funding, cfg)
    obs0 = env.reset(START, END)
    obs1, rec1 = env.step(ActionSpec(target_krd=dict(obs0.benchmark_krd)))
    obs2, rec2 = env.step(ActionSpec())

    expected_1 = len(calendar.trading_days_between(START, obs1.as_of))
    expected_2 = len(calendar.trading_days_between(START, obs2.as_of))
    assert rec1.replay_days == expected_1
    assert rec2.replay_days == expected_2
    assert rec1.replay_cost_seconds >= 0.0
    assert rec2.replay_cost_seconds >= 0.0
    assert env.replay_days == expected_1 + expected_2
    assert env.replay_cost_seconds == pytest.approx(
        rec1.replay_cost_seconds + rec2.replay_cost_seconds
    )
    perf = env.performance
    assert perf["replay_days"] == env.replay_days
    assert perf["n_steps"] == 2
    assert "O(n^2)" in perf["complexity"]
    assert obs2.metadata["replay_days"] == env.replay_days
    assert obs2.metadata["replay_cost_seconds"] == pytest.approx(env.replay_cost_seconds)

    # 整段回放一致性：同一 schedule 的直接回测逐位一致
    schedule = {
        obs0.as_of: ActionSpec(target_krd=dict(obs0.benchmark_krd)),
        obs1.as_of: ActionSpec(),
    }
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


# ---------------------------------------------------------------------------
# R47/R48/R49：共用轨迹构造器、逐字段等价、缩放/滚动反例、终止状态
# ---------------------------------------------------------------------------
def _benchmark_faces(
    panel: pd.DataFrame, calendar: TradingCalendar, cfg: Config, day: date
) -> dict[str, float]:
    """起点基准的逐腿面值（构造精确面值/25M 反例用）。"""
    row = panel.loc[pd.Timestamp(day)].reindex(list(CANONICAL_TENORS))
    curve = Curve(
        as_of=day,
        tenors=np.asarray(CANONICAL_TENORS),
        rates=tuple(row.values),
        method="hermite",
    )
    universe = RollingUniverse.build(curve, day, cfg=cfg).active()
    benchmark = build_benchmark(curve, day, bonds=universe, calendar=calendar, cfg=cfg)
    return {leg.bond.code: float(leg.face_value) for leg in benchmark.legs}


def _native_policy(schedule: dict[date, ActionSpec]):
    """测试策略：把 ActionSpec 转成原生 TargetKRD/TargetFaces（hold 返回 None）。"""

    def policy(ctx: MarketContext) -> TargetKRD | TargetFaces | None:
        action = schedule.get(ctx.as_of, ActionSpec())
        if action.is_hold:
            return None
        if action.action_type == "target_faces":
            return TargetFaces(
                as_of=ctx.as_of,
                faces=dict(action.target_faces or {}),
                notes=action.notes,
            )
        return TargetKRD(
            as_of=ctx.as_of,
            krd=dict(action.target_krd or {}),
            notes=action.notes,
        )

    return policy


def _step_records(
    market: tuple[Config, pd.DataFrame, pd.Series, TradingCalendar],
    cfg: Config,
    schedule: dict[date, ActionSpec],
    start: date = START,
    end: date = END,
) -> tuple[RebalanceEnv, list[StepRecord]]:
    """按 schedule 逐步 step，返回 env 与逐日记录。"""
    _cfg, panel, funding, calendar = market
    env = RebalanceEnv(panel, calendar, funding, cfg)
    env.reset(start, end)
    days = calendar.trading_days_between(start, end)
    records = [env.step(schedule.get(day, ActionSpec()))[1] for day in days[:-1]]
    return env, records


def _policy_signature(
    actions: tuple[PolicyAction, ...], strategy_name: str
) -> tuple[tuple[object, ...], ...]:
    """策略名是成交 ID 的一部分；等价比较时把前缀归一化。"""
    prefix = f"{strategy_name}-"
    return tuple(
        (
            item.day,
            item.tenor,
            item.retired_code,
            item.issued_code,
            item.pending_status,
            tuple(trade_id.replace(prefix, "X-") for trade_id in item.strategy_trade_ids),
            item.status,
            item.reason,
            item.gate_attempts,
            item.bench_trade_ids,
        )
        for item in actions
    )


def _assert_records_equivalent(
    step_records: list[StepRecord], rollout_records: tuple[StepRecord, ...]
) -> None:
    """除策略名/耗时外逐字段等价（R48 验收）。"""
    assert len(step_records) == len(rollout_records)
    for step, rollout in zip(step_records, rollout_records, strict=True):
        assert step.date == rollout.date
        assert step.original_action == rollout.original_action
        assert step.projected_target == rollout.projected_target
        assert step.projected_faces == rollout.projected_faces
        assert step.rejected == rollout.rejected
        assert step.reject_reason == rollout.reject_reason
        assert step.gate_level == pytest.approx(rollout.gate_level)
        assert step.risk_off == rollout.risk_off
        assert step.nav_before == pytest.approx(rollout.nav_before, abs=1e-9)
        assert step.nav_after == pytest.approx(rollout.nav_after, abs=1e-9)
        assert step.reward == pytest.approx(rollout.reward, abs=1e-9)
        assert step.executed_face == pytest.approx(rollout.executed_face, abs=1e-9)
        assert step.gate_attempts == rollout.gate_attempts
        assert step.policy_source == rollout.policy_source
        assert step.pending_status == rollout.pending_status
        assert step.action_outcome == rollout.action_outcome
        assert step.action_scale == pytest.approx(rollout.action_scale, abs=1e-12)
        assert step.notes == rollout.notes
        assert _policy_signature(step.policy_actions, "rl_env") == _policy_signature(
            rollout.policy_actions, "rl_rollout"
        )
        step_trades = [
            {key: value for key, value in trade.items() if key != "trade_id"}
            for trade in step.executed_trades
        ]
        roll_trades = [
            {key: value for key, value in trade.items() if key != "trade_id"}
            for trade in rollout.executed_trades
        ]
        assert step_trades == roll_trades
        step_detail = {
            key: value
            for key, value in step.substitution_detail.items()
            if key != "executed_trade_ids"
        }
        roll_detail = {
            key: value
            for key, value in rollout.substitution_detail.items()
            if key != "executed_trade_ids"
        }
        assert step_detail == roll_detail


def test_rollout_reward_closes_and_matches_step(market) -> None:
    """R47：reward 一律是相邻估值日 NAV 差；空仓 hold/建仓后 hold/结算日均有有限值。"""
    cfg, panel, funding, calendar = market
    days = calendar.trading_days_between(START, END)
    probe = RebalanceEnv(panel, calendar, funding, cfg)
    obs0 = probe.reset(START, END)
    bench = dict(obs0.benchmark_krd)
    schedule = {days[0]: ActionSpec(), days[1]: ActionSpec(target_krd=bench, notes="建仓")}
    for day in days[2:]:
        schedule[day] = ActionSpec()

    _env_step, step_records = _step_records(market, cfg, schedule)
    env_roll = RebalanceEnv(panel, calendar, funding, cfg)
    rollout = env_roll.rollout(_native_policy(schedule), START, END)

    assert len(step_records) == len(rollout.records) == len(days) - 1
    rewards = [record.reward for record in rollout.records]
    assert all(np.isfinite(value) for value in rewards)
    nav = rollout.result.nav
    assert sum(rewards) == pytest.approx(float(nav.loc[days[-1]] - nav.loc[days[0]]), abs=1e-9)
    for step, rolled in zip(step_records, rollout.records, strict=True):
        assert step.reward == pytest.approx(rolled.reward, abs=1e-9)
        assert step.nav_before == pytest.approx(rolled.nav_before, abs=1e-9)
        assert step.nav_after == pytest.approx(rolled.nav_after, abs=1e-9)
        assert rolled.reward == pytest.approx(rolled.nav_after - rolled.nav_before, abs=0.0)

    # 空仓 hold：无成交、reward 仍有限；建仓后 hold；结算日也是 hold 记录
    assert step_records[0].original_action.is_hold
    assert step_records[0].executed_trades == ()
    assert step_records[0].action_outcome == "hold"
    build = step_records[1]
    assert build.original_action.action_type == "target_krd"
    assert build.executed_trades
    settlement_day = build.executed_trades[0]["settlement_date"]
    assert settlement_day == step_records[2].date
    assert step_records[2].original_action.is_hold
    assert any(
        record.original_action.is_hold and record.date > build.date for record in rollout.records
    )


def test_rollout_matches_step_across_action_types(market) -> None:
    """R48：hold/原生 TargetKRD/TargetFaces 逐字段等价；原生目标不得记成 hold。"""
    cfg, panel, funding, calendar = market
    days = calendar.trading_days_between(START, END)
    probe = RebalanceEnv(panel, calendar, funding, cfg)
    obs0 = probe.reset(START, END)
    bench = dict(obs0.benchmark_krd)
    bench_faces = _benchmark_faces(panel, calendar, cfg, days[0])
    face_target = dict(bench_faces)
    face_target[sorted(face_target)[0]] += 1_000_000.0
    huge = {tenor: 1000.0 * value for tenor, value in bench.items()}
    schedule = {
        days[0]: ActionSpec(),  # 空仓 hold
        days[1]: ActionSpec(target_krd=bench, notes="R48 KRD"),
        days[2]: ActionSpec(target_faces=face_target, notes="R48 FACES"),
        days[3]: ActionSpec(target_krd=huge, notes="R48 REJECT"),
        days[4]: ActionSpec(),  # 建仓后 hold
    }
    for day in days[5:]:
        schedule[day] = ActionSpec()

    _env_step, step_records = _step_records(market, cfg, schedule)
    env_roll = RebalanceEnv(panel, calendar, funding, cfg)
    rollout = env_roll.rollout(_native_policy(schedule), START, END)
    _assert_records_equivalent(step_records, rollout.records)

    kinds = [record.original_action.action_type for record in rollout.records[:5]]
    assert kinds == ["hold", "target_krd", "target_faces", "target_krd", "hold"]
    assert rollout.records[1].original_action.target_krd == bench
    assert rollout.records[2].original_action.target_faces == face_target
    assert rollout.records[1].action_outcome in {"executed", "scaled"}
    assert rollout.records[2].action_outcome in {"executed", "scaled"}
    assert rollout.records[2].projected_target is None
    assert dict(rollout.records[2].projected_faces) == face_target
    rejected = rollout.records[3]
    assert rejected.rejected is True
    assert rejected.action_outcome == "rejected"
    assert rejected.reject_reason
    assert rejected.executed_trades == ()
    assert rejected.projected_target is not None
    assert rejected.action_scale < 1.0
    assert rollout.records[4].action_outcome == "hold"


def test_rollout_scaled_faces_matches_step(market) -> None:
    """R48 缩放反例：min_cash_fraction=0，四腿各 25M → 各成交 22M、scale=0.88。"""
    cfg, panel, funding, calendar = market
    days = calendar.trading_days_between(START, date(2019, 1, 29))
    tight = cfg.model_copy(
        update={"constraints": cfg.constraints.model_copy(update={"min_cash_fraction": 0.0})}
    )
    bench_faces = _benchmark_faces(panel, calendar, cfg, days[0])
    requested = {code: 25_000_000.0 for code in sorted(bench_faces)}
    assert len(requested) == 4
    schedule = {days[0]: ActionSpec(target_faces=requested, notes="25M x4")}

    _env_step, step_records = _step_records(market, tight, schedule, START, days[-1])
    env_roll = RebalanceEnv(panel, calendar, funding, tight)
    rollout = env_roll.rollout(_native_policy(schedule), START, days[-1])
    _assert_records_equivalent(step_records, rollout.records)

    for record in (step_records[0], rollout.records[0]):
        assert record.action_outcome == "scaled"
        assert record.action_scale == pytest.approx(0.88, abs=1e-12)
        assert len(record.executed_trades) == 4
        assert {trade["face_value"] for trade in record.executed_trades} == {22_000_000.0}
        assert all(trade["attempted"] == 2 for trade in record.executed_trades)
        assert all(
            trade["scale"] == pytest.approx(0.88, abs=1e-12) for trade in record.executed_trades
        )
        detail = record.substitution_detail
        assert detail["projected_delta_faces"] == requested
        assert detail["executed_faces"] == pytest.approx(
            {code: 22_000_000.0 for code in requested}, abs=1e-9
        )


def test_rollout_roll_cancellation_matches_step(market) -> None:
    """R48 滚动反例：滚动日原动作 cancelled_by_policy、projected_target=None。"""
    cfg, panel, funding, calendar = market
    rolling = cfg.portfolio.rolling.model_copy(update={"enabled": True, "roll_trigger_years": 1.99})
    cfg_roll = cfg.model_copy(
        update={
            "portfolio": cfg.portfolio.model_copy(update={"rolling": rolling}),
            "backtest": cfg.backtest.model_copy(update={"benchmark_version": "rolling"}),
        }
    )
    days = calendar.trading_days_between(START, END)
    probe = RebalanceEnv(panel, calendar, funding, cfg_roll)
    bench = dict(probe.reset(START, END).benchmark_krd)
    schedule = {day: ActionSpec(target_krd=bench, notes="每日基准 KRD") for day in days[:-1]}

    _env_step, step_records = _step_records(market, cfg_roll, schedule)
    env_roll = RebalanceEnv(panel, calendar, funding, cfg_roll)
    rollout = env_roll.rollout(_native_policy(schedule), START, END)
    _assert_records_equivalent(step_records, rollout.records)

    roll_records = [record for record in rollout.records if record.policy_actions]
    assert roll_records, "短窗口内应出现机械换券（roll_trigger_years=1.99）"
    roll_dates = {record.date for record in roll_records}
    assert {date(2019, 2, 2), date(2019, 2, 11)} <= roll_dates
    for record in roll_records:
        assert record.policy_source == "policy_roll"
        assert record.pending_status == "cancelled_by_roll"
        assert record.action_outcome == "cancelled_by_policy"
        assert record.projected_target is None
        assert record.projected_faces  # 机械换券提交面值单独披露
        policy_codes = {action.retired_code for action in record.policy_actions} | {
            action.issued_code for action in record.policy_actions
        }
        assert {code for code, _face in record.projected_faces} <= policy_codes
        for action in record.policy_actions:
            assert isinstance(action, PolicyAction)
            assert isinstance(action.strategy_trade_ids, tuple)
            assert all(isinstance(trade_id, str) for trade_id in action.strategy_trade_ids)


def test_rollout_terminal_state_and_lifecycle(market) -> None:
    """R49：rollout 返回 TerminalState 并同步生命周期；新 env/复用 env 均可 terminate。"""
    cfg, panel, funding, calendar = market
    end = date(2019, 1, 31)
    days = calendar.trading_days_between(START, end)
    probe = RebalanceEnv(panel, calendar, funding, cfg)
    bench = dict(probe.reset(START, end).benchmark_krd)
    schedule = {day: ActionSpec() for day in days[:-1]}
    schedule[days[-2]] = ActionSpec(target_krd=bench, notes="末日建仓（T+1 未结算）")

    env_step, _step_records_unused = _step_records(market, cfg, schedule, START, end)
    assert env_step.done is True
    step_terminal = env_step.terminate()

    # 新 env：rollout 后无需 step 即可 terminate
    env_roll = RebalanceEnv(panel, calendar, funding, cfg)
    rollout = env_roll.rollout(_native_policy(schedule), START, end)
    assert env_roll.done is True
    assert env_roll.current_date == end
    assert env_roll.result is rollout.result
    assert env_roll.observation.nav == pytest.approx(float(rollout.result.nav.loc[end]))
    state = rollout.terminal
    assert isinstance(state, TerminalState)
    assert env_roll.terminate() is state  # 已缓存的本次 rollout 终止状态
    nav = float(rollout.result.nav.loc[end])
    assert state.nav_mark_to_market == pytest.approx(nav, abs=1e-12)
    assert state.nav_mark_to_market == pytest.approx(step_terminal.nav_mark_to_market, abs=1e-9)

    # 末日成交留在 unsettled_trades；买入的待结算现金为负、费用已发生未支付
    assert state.unsettled_trades
    assert all(item["settlement_date"] > end for item in state.unsettled_trades)
    assert all(item["trade_date"] <= end for item in state.unsettled_trades)
    assert state.unsettled_cash == pytest.approx(
        sum(item["signed_cash"] for item in state.unsettled_trades)
    )
    assert state.unsettled_cash < 0.0
    assert state.accrued_fees == pytest.approx(sum(item["fee"] for item in state.unsettled_trades))
    assert state.accrued_fees > 0.0
    assert state.net_terminal_value == pytest.approx(nav - state.accrued_fees)
    assert "已发生未支付费用" in state.notes
    assert "价差" in state.notes and "不重复扣" in state.notes
    assert state.liquidation_assumption == "mark_to_market_no_forced_liquidation"

    exposure = rollout.result.exposure
    exposure_row = exposure.loc[pd.to_datetime(exposure["date"]).dt.date == end]
    assert state.unsettled_cash == pytest.approx(float(exposure_row["pending_cash"].iloc[0]))
    nav_identity = state.cash + float(state.positions["market_value"].sum()) + state.unsettled_cash
    assert nav_identity == pytest.approx(nav, abs=1e-5)
    assert sum(record.reward for record in rollout.records) == pytest.approx(
        nav - float(rollout.result.nav.loc[days[0]]), abs=1e-9
    )

    # 复用 env：先 reset/step 再 rollout，生命周期与终止状态必须切到本次 rollout
    reused = RebalanceEnv(panel, calendar, funding, cfg)
    reused.reset(START, end)
    reused.step(ActionSpec())
    rollout2 = reused.rollout(_native_policy(schedule), START, end)
    assert reused.current_date == end
    assert reused.result is rollout2.result
    assert reused.terminate() is rollout2.terminal
    assert rollout2.terminal.nav_mark_to_market == pytest.approx(state.nav_mark_to_market, abs=1e-9)
