"""RL 环境接口骨架（**复用现有回测引擎的账本、闸门、滚动与成本口径**）。

设计约束（与 ``docs/01_project_plan.md`` §12.15/§12.16 的 C3 一致）
------------------------------------------------------------------
- **不另写交易模拟**：``RebalanceEnv`` 的每一步都调用
  :func:`ficc.backtest.engine.run_backtest`，因此成交价、半价差、手续费、结算、
  融资、滚动换券和成交前 9 项约束闸门与规则回测**完全同一套代码**；
- **动作投影**：策略提出的是 ``ActionSpec.target_krd``（原始动作）；引擎的
  ``project_and_validate`` 会缩放/削峰/拒单。``StepRecord`` 同时记录
  ``original_action`` 与 ``projected_target`` / 实际成交 / 拒单原因，
  不能把"原始动作"当成"已执行的动作"；
- **reward** = 同一账本 NAV 的逐日变化（含融资/费用/价差；价差只在账本里扣一次，
  本模块不重复扣）；
- **as-of**：观测只包含 ``≤ 当前交易日`` 的曲线/持仓/NAV，step 日之后的数据
  不进入 ``Observation``。

实现方式与代价（**必须披露**）
------------------------------
当前引擎没有暴露"单步 pending → execution"的公共接口，因此本模块采用
**逐日重放**：每次 ``reset`` 后，``step`` 把动作记入 schedule，然后从 episode 起点
重新跑 ``run_backtest`` 到下一交易日。这样状态与直接跑一次同区间**逐位一致**，
但总代价是 O(n²)（n = episode 交易日数）。这是**接口原型**，
性能/训练吞吐**不作为结论**；正式训练需要引擎暴露单步执行接口（不改本文件即可升级）。

决策频率：为了让每个 ``step`` 都对应一个可执行信号，环境内部把
``cfg.backtest.rebalance`` 覆写为 ``daily``（其余配置不变）。做对照实验时必须使用
``env.cfg``，而不是外部原始 cfg。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Literal, NamedTuple

import numpy as np
import pandas as pd

from ficc.backtest.engine import BacktestResult, run_backtest
from ficc.config import Config, get_config
from ficc.curve.curve import Curve
from ficc.data.schema import CANONICAL_TENORS
from ficc.ml.asof import _coerce_date
from ficc.portfolio.benchmark import build_benchmark
from ficc.portfolio.constraints import portfolio_risk
from ficc.portfolio.rolling import RollingUniverse
from ficc.portfolio.types import MarketContext, TargetKRD
from ficc.utils.calendar import TradingCalendar

__all__ = [
    "ActionSpec",
    "Event",
    "Observation",
    "RebalanceEnv",
    "StepRecord",
    "hold_target_krd",
]

EventKind = Literal["signal", "project", "execute", "reject", "roll", "settle"]
_EVENT_ORDER: dict[str, int] = {
    "signal": 0,
    "project": 1,
    "execute": 2,
    "reject": 3,
    "roll": 4,
    "settle": 5,
}


# ---------------------------------------------------------------------------
# 动作 / 观测 / 记录
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ActionSpec:
    """策略提出的**原始动作**（尚未过闸门）。

    ``target_krd`` 为目标 KRD 向量（元/bp，按关键期限）；``None``（或空映射）表示
    **零动作 = 持仓不动（hold）**：不生成 pending、不提交主动订单；滚动政策/结算等
    外生事件仍按引擎规则发生。注意"保持 KRD"是另一动作（``target_krd=当前KRD``），
    在整手/约束下仍可能重新求解并交易，不能与 hold 混用。
    它只是"意图"，实际能否成交由引擎闸门决定，差异记录在 :class:`StepRecord`。
    """

    target_krd: Mapping[float, float] | None = None
    notes: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.notes, str):
            raise TypeError("ActionSpec.notes 必须是字符串")
        if self.target_krd is None:
            return
        try:
            # 接受 Mapping，但立即转成普通 dict，避免外部修改
            target = {float(k): float(v) for k, v in dict(self.target_krd).items()}
        except (TypeError, ValueError) as exc:
            raise TypeError("ActionSpec.target_krd 必须是 {tenor: krd} 映射") from exc
        for tenor, value in target.items():
            if tenor <= 0:
                raise ValueError(f"关键期限必须为正数，实际 {tenor}")
            if not np.isfinite(value):
                raise ValueError(f"tenor={tenor} 的 KRD 不是有限数：{value!r}")
        # 空映射在引擎里会被解释为"回到基准"（缺失 tenor 默认取基准），
        # 这里统一按零动作处理，避免动作语义被误读。
        object.__setattr__(self, "target_krd", target or None)


@dataclass(frozen=True)
class Observation:
    """``as_of`` 时点的观测（**不含任何未来数据**）。

    - ``curve``：当日曲线；
    - ``benchmark_krd``：基准的 KRD 向量（元/bp）；
    - ``positions``：当日持仓快照（债券级长表；reset 首日为空表）；
    - ``cash`` / ``nav``：同一账本的已结算现金与 NAV；
    - ``history``：截至当日（含）的曲线面板切片，index.max() ≤ as_of；
    - ``metadata``：episode 名、剩余步数、来源说明等。
    """

    as_of: date
    curve: Curve
    benchmark_krd: dict[float, float]
    positions: pd.DataFrame
    cash: float
    nav: float
    history: pd.DataFrame
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class StepRecord:
    """一步决策的完整审计记录。

    ``original_action`` 是策略提出的目标；``projected_target`` 是**提交给闸门的目标**
    （引擎内部还会按档位缩放/重试，本接口拿不到中间尝试，只记录提交值与
    ``gate_level``）。``executed_trades`` 是账本实际提交的成交（可能为空）。
    ``reward = nav_after - nav_before``，同一账本、同一成本口径，价差不重复扣。
    """

    date: date
    original_action: ActionSpec
    projected_target: TargetKRD | None
    projected_faces: tuple[tuple[str, float], ...]
    executed_trades: tuple[dict[str, Any], ...]
    rejected: bool
    reject_reason: str
    gate_level: float
    risk_off: bool
    nav_before: float
    nav_after: float
    reward: float
    executed_face: float = 0.0
    gate_attempts: int = 0
    policy_source: str = "none"
    pending_status: str = "none"
    notes: str = ""


class Event(NamedTuple):
    """时间序事件：``(date, kind, detail)``，kind ∈ signal/project/execute/reject/roll/settle。"""

    date: date
    kind: EventKind
    detail: dict[str, Any]


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def _curve_at(panel: pd.DataFrame, tenors: tuple[float, ...], day: date) -> Curve:
    row = panel.loc[pd.Timestamp(day)].reindex(list(tenors))
    if row.isna().any():
        raise ValueError(f"{day} 曲线存在缺失期限：{row[row.isna()].index.tolist()}")
    return Curve(
        as_of=day,
        tenors=np.asarray(tenors),
        rates=tuple(row.values),
        method="hermite",
    )


def _dates_of(series: pd.Series) -> pd.Series:
    """把一列日期（date/Timestamp/字符串）统一成 datetime.date 的 object Series。"""
    if series.empty:
        return pd.Series([], dtype=object, index=series.index)
    return pd.to_datetime(series).dt.date


def _to_date(value: Any) -> date:
    if isinstance(value, pd.Timestamp):
        return value.date()
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return pd.Timestamp(value).date()


def _empty_frame() -> pd.DataFrame:
    return pd.DataFrame()


def hold_target_krd(ctx: MarketContext, *, calendar: TradingCalendar, cfg: Config) -> TargetKRD:
    """零动作目标：**保持当前组合 KRD 不变**（不主动买卖）。

    通过 ``portfolio_risk`` 从 ``ctx.positions`` 计算当前 KRD（与闸门/报告同一风险函数），
    因此零动作只表达"什么都不做"，不是"回到基准"。
    """
    risk = portfolio_risk(ctx.positions, ctx.curve, calendar=calendar, cfg=cfg)
    krd = {float(tenor): float(value) for tenor, value in risk.krd.items()}
    return TargetKRD(as_of=ctx.as_of, krd=krd, notes="零动作：保持当前 KRD 不变")


class _ScriptedStrategy:
    """按 signal_date 查表输出目标的策略（环境内部使用；不对外承诺 API）。"""

    def __init__(
        self,
        name: str,
        schedule: dict[date, ActionSpec],
        *,
        calendar: TradingCalendar,
        cfg: Config,
    ) -> None:
        self.name = name
        self._schedule = schedule
        self._calendar = calendar
        self._cfg = cfg
        self.emitted: dict[date, TargetKRD | None] = {}

    def target_krd(self, ctx: MarketContext) -> TargetKRD | None:
        action = self._schedule.get(ctx.as_of)
        if action is None or action.target_krd is None:
            # hold：返回 None，引擎不生成 pending（不主动交易）；不是"保持 KRD"求解。
            self.emitted[ctx.as_of] = None
            return None
        target = TargetKRD(
            as_of=ctx.as_of,
            krd=dict(action.target_krd),
            notes=action.notes or "RL 环境动作",
        )
        self.emitted[ctx.as_of] = target
        return target


# ---------------------------------------------------------------------------
# 环境
# ---------------------------------------------------------------------------
class RebalanceEnv:
    """逐日重放的 RL 环境（接口原型；账本/闸门/滚动/成本复用回测引擎）。

    Parameters
    ----------
    panel, calendar, funding, cfg
        与 :func:`run_backtest` 完全相同的输入；``cfg=None`` 时读全局配置。
    force_daily_rebalance : bool, default True
        把决策频率覆写为 daily，使每个 ``step`` 都能生成一个待执行信号。
    """

    def __init__(
        self,
        panel: pd.DataFrame,
        calendar: TradingCalendar,
        funding: pd.Series | None = None,
        cfg: Config | None = None,
        *,
        force_daily_rebalance: bool = True,
    ) -> None:
        if not isinstance(panel, pd.DataFrame) or panel.empty:
            raise ValueError("panel 必须是非空 DataFrame")
        if not isinstance(calendar, TradingCalendar):
            raise TypeError("calendar 必须是 TradingCalendar")
        if funding is not None and not isinstance(funding, pd.Series):
            raise TypeError("funding 必须是 pandas.Series 或 None")

        resolved = get_config() if cfg is None else cfg
        if force_daily_rebalance and resolved.backtest.rebalance != "daily":
            resolved = resolved.model_copy(
                update={"backtest": resolved.backtest.model_copy(update={"rebalance": "daily"})}
            )
        self._panel = panel.sort_index()
        self._calendar = calendar
        self._funding = funding
        self._cfg = resolved
        self._tenors = tuple(float(t) for t in CANONICAL_TENORS)

        self._start: date | None = None
        self._end: date | None = None
        self._days: tuple[date, ...] = ()
        self._cursor: date | None = None
        self._strategy_name = "rl_env"
        self._schedule: dict[date, ActionSpec] = {}
        self._records: list[StepRecord] = []
        self._result: BacktestResult | None = None
        self._events: tuple[Event, ...] = ()
        self._last_nav = float(self._cfg.backtest.init_cash)

    # ---- 只读属性 ------------------------------------------------------
    @property
    def cfg(self) -> Config:
        """环境实际使用的配置（已把 rebalance 覆写为 daily；对照实验必须用它）。"""
        return self._cfg

    @property
    def result(self) -> BacktestResult | None:
        """最近一次重放的完整回测结果（未 reset/step 前为 None）。"""
        return self._result

    @property
    def records(self) -> tuple[StepRecord, ...]:
        return tuple(self._records)

    @property
    def events(self) -> tuple[Event, ...]:
        """按时间序的 ``(date, kind, detail)`` 事件（来自最近一次重放）。"""
        return self._events

    @property
    def current_date(self) -> date | None:
        return self._cursor

    @property
    def done(self) -> bool:
        return self._cursor is not None and self._end is not None and self._cursor >= self._end

    @property
    def observation(self) -> Observation:
        if self._cursor is None:
            raise RuntimeError("环境尚未 reset")
        return self._observation(self._cursor)

    # ---- episode -------------------------------------------------------
    def reset(self, start: date, end: date, strategy_name: str = "rl_env") -> Observation:
        """开始一个新 episode，返回**首日 as-of 观测**（曲线/基准 KRD/持仓/现金/NAV）。"""
        start = _coerce_date(start, name="start")
        end = _coerce_date(end, name="end")
        if not isinstance(strategy_name, str) or not strategy_name:
            raise ValueError("strategy_name 必须是非空字符串")
        days = self._calendar.trading_days_between(start, end)
        if len(days) < 2:
            raise ValueError(f"episode [{start}, {end}] 内交易日不足 2 天")
        if pd.Timestamp(days[0]) not in self._panel.index:
            raise ValueError(f"起始日 {days[0]} 不在 panel 内")

        self._start = days[0]
        self._end = days[-1]
        self._days = tuple(days)
        self._cursor = days[0]
        self._strategy_name = strategy_name
        self._schedule = {}
        self._records = []
        self._result = None
        self._events = ()
        self._last_nav = float(self._cfg.backtest.init_cash)
        return self._observation(self._cursor)

    def step(self, action: ActionSpec) -> tuple[Observation, StepRecord]:
        """提交一个动作，推进 **1 个交易日**；返回 ``(下一日观测, 审计记录)``。

        动作在 ``t`` 日（signal_date）生成，引擎按既有纪律在 ``t+1`` 成交；成交前
        仍走 ``project_and_validate`` 的完整 9 项闸门。若被拒单，``executed_trades``
        为空且 ``rejected=True``；若闸门缩放后成交，成交会小于原始动作（记录差异）。
        """
        if self._cursor is None or self._start is None or self._end is None:
            raise RuntimeError("必须先调用 reset 再 step")
        if not isinstance(action, ActionSpec):
            raise TypeError(f"action 必须是 ActionSpec，实际 {type(action).__name__}")
        if self.done:
            raise RuntimeError(f"episode 已结束（cursor={self._cursor}, end={self._end}）")

        signal_date = self._cursor
        trade_date = self._calendar.next_trading_day(signal_date, 1)
        if trade_date > self._end:
            raise RuntimeError(f"下一交易日 {trade_date} 超出 episode 终点 {self._end}")

        self._schedule[signal_date] = action
        strategy = _ScriptedStrategy(
            self._strategy_name, self._schedule, calendar=self._calendar, cfg=self._cfg
        )
        result = run_backtest(
            strategy,
            panel=self._panel,
            calendar=self._calendar,
            funding=self._funding,
            cfg=self._cfg,
            start=self._start,
            end=trade_date,
        )
        # 重放一致性：历史 NAV 必须与上一步记录相同（同引擎、同输入 → 逐位一致）
        replay_nav = float(result.nav.loc[trade_date])
        nav_at_signal = float(result.nav.loc[signal_date])
        if not np.isclose(nav_at_signal, self._last_nav, rtol=0.0, atol=1e-9):
            raise RuntimeError(
                f"重放状态漂移：{signal_date} NAV={nav_at_signal}，"
                f"上一步记录={self._last_nav}（逐日重放必须确定性）"
            )
        decision = None
        for item in getattr(result, "gate_decisions", []):
            if item.get("date") == trade_date:
                decision = item
                break
        if decision is not None:
            projected = decision.get("projected_target")
            projected_faces = tuple(decision.get("projected_faces") or ())
            decision_pending = str(decision.get("pending_status", "none"))
            gate_attempts = int(decision.get("gate_attempts", 0))
            policy_source = str(decision.get("source", "none"))
        else:  # 兼容旧结果（无决策日志）
            projected = strategy.emitted.get(signal_date)
            projected_faces = ()
            decision_pending = "none"
            gate_attempts = 0
            policy_source = "none"

        trades = result.trades
        trades_on = (
            trades.loc[_dates_of(trades["trade_date"]) == trade_date] if len(trades) else trades
        )
        rejected_frame = result.rejected
        rejected_on = (
            rejected_frame.loc[_dates_of(rejected_frame["date"]) == trade_date]
            if len(rejected_frame)
            else rejected_frame
        )
        constraints = result.constraints
        row = constraints.loc[_dates_of(constraints["date"]) == trade_date]
        gate_level = float(row["gate_level"].iloc[0]) if len(row) else 1.0
        risk_off = bool(row["risk_off"].iloc[0]) if len(row) else False
        rejected = len(rejected_on) > 0
        reject_reason = str(rejected_on["reason"].iloc[0]) if rejected else ""

        executed_trades = tuple(
            {
                "trade_id": str(item["trade_id"]),
                "bond_code": str(item["bond_code"]),
                "side": str(item["side"]),
                "face_value": float(item["face_value"]),
                "clean_price": float(item["clean_price"]),
                "signal_date": _to_date(item["signal_date"]),
                "trade_date": _to_date(item["trade_date"]),
                "settlement_date": _to_date(item["settlement_date"]),
            }
            for _, item in trades_on.iterrows()
        )
        executed_face = float(sum(item["face_value"] for item in executed_trades))

        roll_on_day = self._roll_rows(result, trade_date)
        note_parts: list[str] = []
        if decision_pending == "cancelled_by_roll":
            note_parts.append("滚动日：旧 pending 被取消（无延期队列），机械换券优先执行")
        elif not roll_on_day.empty:
            note_parts.append(f"滚动日：{len(roll_on_day)} 个期限换券")
        if risk_off:
            note_parts.append("风控熔断：引擎以基准中性目标替代原动作")
        if action.target_krd is None and not executed_trades:
            note_parts.append("零动作（hold）：持仓不动，无主动成交")
        elif action.target_krd is not None and not executed_trades and not rejected:
            note_parts.append("原始动作未产生成交（低于死区/约束或滚动政策替代）")
        notes = "；".join(note_parts)

        record = StepRecord(
            date=trade_date,
            original_action=action,
            projected_target=projected,
            projected_faces=projected_faces,
            executed_trades=executed_trades,
            rejected=rejected,
            reject_reason=reject_reason,
            gate_level=gate_level,
            risk_off=risk_off,
            nav_before=self._last_nav,
            nav_after=replay_nav,
            reward=replay_nav - self._last_nav,
            executed_face=executed_face,
            gate_attempts=gate_attempts,
            policy_source=policy_source,
            pending_status=decision_pending,
            notes=notes,
        )

        self._records.append(record)
        self._cursor = trade_date
        self._result = result
        self._last_nav = replay_nav
        self._events = _build_events(result)
        return self._observation(trade_date), record

    # ---- 内部 ----------------------------------------------------------
    def _roll_rows(self, result: BacktestResult, day: date) -> pd.DataFrame:
        rolls = result.roll_events
        if len(rolls) == 0:
            return rolls
        return rolls.loc[pd.to_datetime(rolls["date"]).dt.date == day]

    def _benchmark_krd(self, day: date) -> dict[float, float]:
        curve = _curve_at(self._panel, self._tenors, day)
        universe = RollingUniverse.build(curve, day, cfg=self._cfg).active()
        benchmark = build_benchmark(
            curve, day, bonds=universe, calendar=self._calendar, cfg=self._cfg
        )
        return {float(tenor): float(value) for tenor, value in benchmark.krd.items()}

    def _observation(self, day: date) -> Observation:
        curve = _curve_at(self._panel, self._tenors, day)
        history = self._panel.loc[: pd.Timestamp(day)]
        if history.index.max() > pd.Timestamp(day):
            raise AssertionError(f"as-of 观测包含 {history.index.max()} 之后的未来数据")

        if self._result is None:
            positions = _empty_frame()
            cash = float(self._cfg.backtest.init_cash)
            nav = float(self._cfg.backtest.init_cash)
            benchmark_krd = self._benchmark_krd(day)
        else:
            result = self._result
            positions = result.positions
            positions = (
                positions.loc[_dates_of(positions["as_of"]) == day].copy()
                if len(positions)
                else positions.copy()
            )
            exposure = result.exposure
            exposure_row = exposure.loc[_dates_of(exposure["date"]) == day]
            nav = float(result.nav.loc[day])
            cash = float(exposure_row["cash"].iloc[0]) if len(exposure_row) else float("nan")
            benchmark_krd = _bench_krd_from_result(result, day)
            if not benchmark_krd:
                benchmark_krd = self._benchmark_krd(day)

        return Observation(
            as_of=day,
            curve=curve,
            benchmark_krd=benchmark_krd,
            positions=positions,
            cash=cash,
            nav=nav,
            history=history,
            metadata={
                "strategy": self._strategy_name,
                "start": self._start.isoformat() if self._start else None,
                "end": self._end.isoformat() if self._end else None,
                "n_steps": len(self._records),
                "done": self.done,
                "asof_note": "观测只含 ≤ as_of 的数据；step 日之后的数据不可见",
                "roll_note": "滚动换券来自同一回测引擎的 roll_events",
            },
        )


def _bench_krd_from_result(result: BacktestResult, day: date) -> dict[float, float]:
    """从 ``result.exposure`` 的 ``bench_krd_*`` 列还原当日基准 KRD。"""
    exposure = result.exposure
    row = exposure.loc[_dates_of(exposure["date"]) == day]
    if len(row) == 0:
        return {}
    out: dict[float, float] = {}
    for column in exposure.columns:
        if isinstance(column, str) and column.startswith("bench_krd_"):
            out[float(column[len("bench_krd_") :])] = float(row[column].iloc[0])
    return out


def _build_events(result: BacktestResult) -> tuple[Event, ...]:
    """把引擎各类结果表映射成时间序事件（roll 来自 ``result.roll_events``）。"""
    events: list[Event] = []

    signals = result.signals
    for _, row in signals.iterrows():
        signal_date = row.get("signal_date")
        if signal_date is None or (isinstance(signal_date, float) and np.isnan(signal_date)):
            continue
        events.append(
            Event(
                date=_to_date(row["date"]),
                kind="signal",
                detail={
                    "signal_date": _to_date(signal_date),
                    "target_total_dv01": float(row.get("target_total_dv01", 0.0)),
                    "notes": str(row.get("notes", "")),
                    "risk_off": bool(row.get("risk_off", False)),
                },
            )
        )

    # 真实闸门决策（R45）：每行是当日实际 project 结果（含 policy_roll/取消/拒单）
    decisions = getattr(result, "gate_decisions", [])
    if decisions:
        for item in decisions:
            events.append(
                Event(
                    date=_to_date(item["date"]),
                    kind="project",
                    detail={
                        "source": str(item.get("source", "none")),
                        "status": str(item.get("status", "")),
                        "pending_status": str(item.get("pending_status", "none")),
                        "gate_level": float(item.get("gate_level", 1.0)),
                        "gate_attempts": int(item.get("gate_attempts", 0)),
                        "n_trades": len(item.get("executed_trade_ids") or ()),
                        "rejected": bool(item.get("rejected", False)),
                        "risk_off": bool(item.get("risk_off", False)),
                        "reject_reason": str(item.get("reject_reason", "")),
                    },
                )
            )
    else:  # 兼容旧结果
        constraints = result.constraints
        for _, row in constraints.iterrows():
            if int(row.get("n_trades", 0)) > 0 or bool(row.get("rejected", False)):
                events.append(
                    Event(
                        date=_to_date(row["date"]),
                        kind="project",
                        detail={
                            "gate_level": float(row.get("gate_level", 1.0)),
                            "gate_mode": str(row.get("gate_mode", "normal")),
                            "n_trades": int(row.get("n_trades", 0)),
                            "rejected": bool(row.get("rejected", False)),
                            "risk_off": bool(row.get("risk_off", False)),
                            "reject_reason": str(row.get("reject_reason", "")),
                        },
                    )
                )

    trades = result.trades
    for _, row in trades.iterrows():
        events.append(
            Event(
                date=_to_date(row["trade_date"]),
                kind="execute",
                detail={
                    "trade_id": str(row["trade_id"]),
                    "bond_code": str(row["bond_code"]),
                    "side": str(row["side"]),
                    "face_value": float(row["face_value"]),
                    "clean_price": float(row["clean_price"]),
                    "signal_date": _to_date(row["signal_date"]),
                    "settlement_date": _to_date(row["settlement_date"]),
                },
            )
        )

    rejected = result.rejected
    for _, row in rejected.iterrows():
        events.append(
            Event(
                date=_to_date(row["date"]),
                kind="reject",
                detail={
                    "signal_date": _to_date(row["signal_date"]),
                    "reason": str(row["reason"]),
                    "target_total_dv01": float(row.get("target_total_dv01", 0.0)),
                    "gate_level": float(row.get("gate_level", 1.0)),
                    "risk_off": bool(row.get("risk_off", False)),
                },
            )
        )

    rolls = result.roll_events
    for _, row in rolls.iterrows():
        events.append(
            Event(
                date=_to_date(row["date"]),
                kind="roll",
                detail={
                    "tenor": float(row["tenor"]),
                    "retired_code": str(row["retired_code"]),
                    "issued_code": str(row["issued_code"]),
                    "status": str(row.get("status", "")),
                    "pending_status": str(row.get("pending_status", "")),
                    "reason": str(row.get("reason", "")),
                    "strategy_trade_ids": str(row.get("strategy_trade_ids", "")),
                },
            )
        )

    cashflows = result.cashflows
    for _, row in cashflows.iterrows():
        kind = str(row["kind"])
        if kind not in ("trade_buy", "trade_sell"):
            continue
        events.append(
            Event(
                date=_to_date(row["date"]),
                kind="settle",
                detail={
                    "flow_kind": kind,
                    "amount": float(row["amount"]),
                    "bond_code": str(row.get("bond_code", "")),
                    "trade_id": str(row.get("trade_id", "")),
                    "note": str(row.get("note", "")),
                },
            )
        )

    events.sort(key=lambda event: (event.date, _EVENT_ORDER[event.kind]))
    return tuple(events)
