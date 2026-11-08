"""RL 环境接口骨架（**复用现有回测引擎的账本、闸门、滚动与成本口径**）。

设计约束（与 ``docs/01_project_plan.md`` §12.15/§12.16 的 C3 一致）
------------------------------------------------------------------
- **不另写交易模拟**：``RebalanceEnv`` 的每一步都调用
  :func:`ficc.backtest.engine.run_backtest`，因此成交价、半价差、手续费、结算、
  融资、滚动换券和成交前 9 项约束闸门与规则回测**完全同一套代码**；
- **动作投影**：策略提出的是 ``ActionSpec``（hold / target_krd / target_faces）；
  引擎的 ``project_and_validate`` 会缩放/削峰/拒单。``StepRecord`` 同时记录
  ``original_action`` 与 ``projected_target`` / 实际成交 / 拒单原因，不能把
  "原始动作"当成"已执行的动作"；
- **动作替代审计**：滚动日机械换券是独立政策动作，记入 ``policy_actions``；
  原 pending 被取消时 ``projected_target`` 不混入政策目标，而是显式记录
  ``action_outcome="cancelled_by_policy"``；
- **reward** = 同一账本 NAV 的逐日变化（含融资/费用/价差；价差只在账本里扣一次，
  本模块不重复扣）；
- **终止义务**：``terminate()`` 按市价估值、不强制平仓，单独披露未结算成交、
  待结算现金（买入负/卖出正）与未结算费用；reward 只取账本 NAV 的逐日变化，
  不额外计入终止清算收益；
- **as-of**：观测只包含 ``≤ 当前交易日`` 的曲线/持仓/NAV，step 日之后的数据
  不进入 ``Observation``。

实现方式与代价（**必须披露**）
------------------------------
当前引擎没有暴露"单步 pending → execution"的公共接口，因此本模块采用
**逐日重放**：每次 ``reset`` 后，``step`` 把动作记入 schedule，然后从 episode 起点
重新跑 ``run_backtest`` 到下一交易日。这样状态与直接跑一次同区间**逐位一致**，
但总代价是 O(n²)（n = episode 交易日数）。这是**接口原型**，性能/训练吞吐
**不作为结论**；``StepRecord.replay_days/replay_cost_seconds`` 与
``RebalanceEnv.performance`` 披露每一步的重放天数与耗时。正式训练需要引擎暴露
单步执行接口（不改本文件即可升级）。

决策频率：为了让每个 ``step`` 都对应一个可执行信号，环境内部把
``cfg.backtest.rebalance`` 覆写为 ``daily``（其余配置不变）。做对照实验时必须使用
``env.cfg``，而不是外部原始 cfg。
"""

from __future__ import annotations

import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any, Literal, NamedTuple

import numpy as np
import pandas as pd

from ficc.backtest.engine import BacktestResult, run_backtest
from ficc.config import Config, get_config
from ficc.curve.curve import Curve
from ficc.data.schema import CANONICAL_TENORS, BondStatic
from ficc.ml.asof import _coerce_date
from ficc.portfolio.benchmark import build_benchmark
from ficc.portfolio.constraints import portfolio_risk
from ficc.portfolio.rolling import RollingUniverse
from ficc.portfolio.types import MarketContext, TargetFaces, TargetKRD
from ficc.pricing.conventions import accrued_interest
from ficc.utils.calendar import TradingCalendar

__all__ = [
    "ActionOutcome",
    "ActionSpec",
    "ActionType",
    "Event",
    "Observation",
    "PolicyAction",
    "RebalanceEnv",
    "StepRecord",
    "TerminalState",
    "hold_target_krd",
]

EventKind = Literal["signal", "project", "execute", "reject", "roll", "settle"]
ActionType = Literal["hold", "target_krd", "target_faces"]
ActionOutcome = Literal[
    "executed",
    "scaled",
    "rejected",
    "cancelled_by_policy",
    "no_trade_deadband",
]
PolicySource = Literal["none", "signal", "policy_roll"]
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

    动作类型显式区分（R44/R45 后续）：

    - ``hold``：**持仓不动**。不生成 pending、不提交主动订单；滚动政策/结算等
      外生事件仍按引擎规则发生。注意它与"保持当前 KRD"（``target_krd=当前KRD``）
      不同：后者仍会重新求解、在整手/约束下可能交易；
    - ``target_krd``：目标 KRD 向量（元/bp，按关键期限），由引擎求解面值；
    - ``target_faces``：逐券目标面值（元），绕过 KRD 求解，仍走同一成交前闸门。

    向后兼容：不传 ``action_type`` 时按字段推断——两者都空 → ``hold``；提供
    ``target_krd`` → ``target_krd``；提供 ``target_faces`` → ``target_faces``。
    显式传入 ``action_type`` 时校验互斥：hold 不得带目标；krd/faces 必须且只能
    有一个。``target_krd={}`` 与历史行为一致，按零动作（hold）处理。

    它只是"意图"，实际能否成交由引擎闸门决定，差异记录在 :class:`StepRecord`。
    """

    # 保持 target_krd 为第一个位置参数（历史调用 ActionSpec(mapping) 兼容）。
    target_krd: Mapping[float, float] | None = None
    target_faces: Mapping[str, float] | None = None
    action_type: ActionType | None = None
    notes: str = ""

    def __post_init__(self) -> None:
        if not isinstance(self.notes, str):
            raise TypeError("ActionSpec.notes 必须是字符串")

        krd: dict[float, float] | None = None
        if self.target_krd is not None:
            try:
                # 接受 Mapping，但立即转成普通 dict，避免外部修改
                krd = {float(k): float(v) for k, v in dict(self.target_krd).items()}
            except (TypeError, ValueError) as exc:
                raise TypeError("ActionSpec.target_krd 必须是 {tenor: krd} 映射") from exc
            for tenor, value in krd.items():
                if tenor <= 0:
                    raise ValueError(f"关键期限必须为正数，实际 {tenor}")
                if not np.isfinite(value):
                    raise ValueError(f"tenor={tenor} 的 KRD 不是有限数：{value!r}")
            # 空映射在引擎里会被解释为"回到基准"（缺失 tenor 默认取基准），
            # 这里统一按零动作处理，避免动作语义被误读。
            krd = krd or None

        faces: dict[str, float] | None = None
        if self.target_faces is not None:
            try:
                faces = {str(k): float(v) for k, v in dict(self.target_faces).items()}
            except (TypeError, ValueError) as exc:
                raise TypeError("ActionSpec.target_faces 必须是 {bond_code: face} 映射") from exc
            for code, face in faces.items():
                if not code:
                    raise ValueError("ActionSpec.target_faces 的券代码不能为空")
                if not np.isfinite(face) or face < 0:
                    raise ValueError(f"券 {code} 的目标面值必须是非负有限数：{face!r}")
            faces = faces or None

        action_type = self.action_type
        if action_type is None:
            if krd is not None and faces is not None:
                raise ValueError(
                    "ActionSpec 不能同时提供 target_krd 与 target_faces；"
                    "无法推断动作类型，请显式指定 action_type"
                )
            if faces is not None:
                action_type = "target_faces"
            elif krd is not None:
                action_type = "target_krd"
            else:
                action_type = "hold"
        elif action_type not in ("hold", "target_krd", "target_faces"):
            raise ValueError(f"未知 action_type：{action_type!r}")

        if action_type == "hold":
            if krd is not None or faces is not None:
                raise ValueError("hold 动作不得携带 target_krd 或 target_faces")
        elif action_type == "target_krd":
            if krd is None:
                raise ValueError("target_krd 动作必须提供非空 target_krd")
            if faces is not None:
                raise ValueError("target_krd 动作不得同时提供 target_faces")
        else:  # target_faces
            if faces is None:
                raise ValueError("target_faces 动作必须提供非空 target_faces")
            if krd is not None:
                raise ValueError("target_faces 动作不得同时提供 target_krd")

        object.__setattr__(self, "target_krd", krd)
        object.__setattr__(self, "target_faces", faces)
        object.__setattr__(self, "action_type", action_type)

    @property
    def is_hold(self) -> bool:
        return self.action_type == "hold"

    @property
    def target_total(self) -> float:
        """动作规模：KRD 用 ``total_dv01``；faces 用面值合计；hold 为 0。"""
        if self.action_type == "target_krd":
            return float(sum((self.target_krd or {}).values()))
        if self.action_type == "target_faces":
            return float(sum((self.target_faces or {}).values()))
        return 0.0


@dataclass(frozen=True)
class PolicyAction:
    """滚动政策（机械换券）产生的**独立**动作记录。

    它不属于策略的原始动作；滚动日的机械换券必须单独披露，不能挂到
    ``StepRecord.projected_target`` 下。字段与 ``BacktestResult.roll_events``
    一一对应（``day`` 为成交/换券日）。
    """

    day: date
    tenor: float
    retired_code: str
    issued_code: str
    pending_status: str
    strategy_trade_ids: tuple[str, ...]
    status: str
    reason: str = ""
    gate_attempts: int = 0
    bench_trade_ids: tuple[str, ...] = ()

    @property
    def source(self) -> str:
        return "rolling_policy"


@dataclass(frozen=True)
class TerminalState:
    """episode 终止时的**估值与义务**快照（不强制平仓）。

    - ``nav_mark_to_market``：与 ``BacktestResult.nav`` 末值相同（trade-date 会计，
      ``nav = cash + ΣMV + pending_cash``）；
    - ``unsettled_trades``：``settlement_date > episode end`` 的成交（T+1 结算；
      末日成交通常在此）；
    - ``unsettled_cash``：待结算现金义务，**买入为负、卖出为正**，按结算日全价
      （净价 + 应计利息）× 面值/100 计算；
    - ``accrued_fees``：上述未结算成交在结算日将扣除但尚未进入 NAV 的费用
      （``cfg.costs.fee_bp`` + 每笔结算费 + ``TradeRecord.fee``）；
    - ``liquidation_assumption``：默认按市价估值、**不强制平仓**。reward 只取账本
      NAV 的逐日变化（含持仓按市价重估的未实现变动），不因终止变现追加收益/成本；
      未结算成交、待结算现金与费用作为义务单独披露。
    """

    nav_mark_to_market: float
    cash: float
    positions: pd.DataFrame
    unsettled_trades: tuple[dict[str, Any], ...]
    unsettled_cash: float
    accrued_fees: float
    liquidation_assumption: str
    notes: str


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

    ``original_action`` 是策略提出的目标；``projected_target`` 是**原动作提交给
    闸门的目标**（引擎内部还会按档位缩放/重试，本接口记录提交值/最后尝试）。
    滚动日的机械换券不写入 ``projected_target``：它单独记在 ``policy_actions``，
    原动作若被取消则 ``action_outcome="cancelled_by_policy"``。

    ``executed_trades`` 是账本实际提交的成交（可能为空）。``substitution_detail``
    记录原始动作、提交目标与实际成交的 face/dv01 差异；``action_scale`` 是提交
    目标相对原始动作的规模比（KRD 用 total_dv01；faces 用执行买入/请求买入）。
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
    # R44/R45 后续：动作结果与政策/替代审计
    action_outcome: ActionOutcome = "no_trade_deadband"
    action_scale: float = 1.0
    policy_actions: tuple[PolicyAction, ...] = ()
    substitution_detail: dict[str, Any] = field(default_factory=dict)
    # 性能披露：本步从 episode 起点重放的交易日数与耗时（O(n²) 接口原型的证据）
    replay_days: int = 0
    replay_cost_seconds: float = 0.0

    @property
    def attempted(self) -> int:
        """闸门尝试次数（``gate_attempts`` 的显式别名）。"""
        return self.gate_attempts


@dataclass(frozen=True)
class Rollout:
    """一次 ``run_backtest`` 生成的整段轨迹（**O(n)** 训练路径，替代逐步 O(n²) 重放）。

    ``records`` 与 ``step`` 的 :class:`StepRecord` 同契约；``result`` 是完整
    :class:`BacktestResult`（NAV/成交/约束/归因输入）。policy 在引擎的每个信号日
    （收盘后）被调用，只看到该日及之前的 ``MarketContext``；执行仍按 t+1 成交与同一闸门。
    """

    records: tuple[StepRecord, ...]
    result: BacktestResult


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
        self.emitted: dict[date, TargetKRD | TargetFaces | None] = {}

    def target_krd(self, ctx: MarketContext) -> TargetKRD | TargetFaces | None:
        action = self._schedule.get(ctx.as_of)
        if action is None or action.action_type == "hold":
            # hold：返回 None，引擎不生成 pending（不主动交易）；不是"保持 KRD"求解。
            self.emitted[ctx.as_of] = None
            return None
        if action.action_type == "target_faces":
            faces_target = TargetFaces(
                as_of=ctx.as_of,
                faces=dict(action.target_faces or {}),
                notes=action.notes or "RL 环境动作（目标面值）",
            )
            self.emitted[ctx.as_of] = faces_target
            return faces_target
        target = TargetKRD(
            as_of=ctx.as_of,
            krd=dict(action.target_krd or {}),
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
        # 性能披露：累计重放交易日数与耗时（O(n²) 接口原型；正式训练需单步接口）
        self._replay_days = 0
        self._replay_cost_seconds = 0.0

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
    def replay_days(self) -> int:
        """本 episode 累计重放的交易日数（每次 step 从起点重跑到成交日）。"""
        return self._replay_days

    @property
    def replay_cost_seconds(self) -> float:
        """本 episode 累计 ``run_backtest`` 重放耗时（秒，仅统计引擎调用）。"""
        return self._replay_cost_seconds

    @property
    def performance(self) -> dict[str, Any]:
        """性能披露（O(n²) 接口原型；正式训练需引擎单步状态转移接口）。"""
        n_steps = len(self._records)
        return {
            "n_steps": n_steps,
            "replay_days": self._replay_days,
            "replay_cost_seconds": self._replay_cost_seconds,
            "replay_days_per_step": (self._replay_days / n_steps) if n_steps else 0.0,
            "replay_seconds_per_step": (self._replay_cost_seconds / n_steps if n_steps else 0.0),
            "complexity": "O(n^2)：每次 step 从 episode 起点重放 run_backtest 到成交日",
            "recommendation": "正式训练需引擎暴露单步 pending→execution 状态转移接口",
        }

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
        self._replay_days = 0
        self._replay_cost_seconds = 0.0
        return self._observation(self._cursor)

    def step(self, action: ActionSpec) -> tuple[Observation, StepRecord]:
        """提交一个动作，推进 **1 个交易日**；返回 ``(下一日观测, 审计记录)``。

        动作在 ``t`` 日（signal_date）生成，引擎按既有纪律在 ``t+1`` 成交；成交前
        仍走 ``project_and_validate`` 的完整 9 项闸门。若被拒单，``executed_trades``
        为空且 ``rejected=True``；若闸门缩放后成交，``action_scale < 1`` 且
        ``gate_attempts`` 记录尝试次数。

        滚动日：原 action 的 pending 被机械换券取消时，``projected_target`` 为
        ``None``、``action_outcome="cancelled_by_policy"``；机械换券只出现在
        ``policy_actions``。风控熔断替代原 action 时同样记 ``cancelled_by_policy``。
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
        previous_faces = self._positions_at(signal_date)

        self._schedule[signal_date] = action
        strategy = _ScriptedStrategy(
            self._strategy_name, self._schedule, calendar=self._calendar, cfg=self._cfg
        )
        replay_start = time.perf_counter()
        result = run_backtest(
            strategy,
            panel=self._panel,
            calendar=self._calendar,
            funding=self._funding,
            cfg=self._cfg,
            start=self._start,
            end=trade_date,
        )
        replay_seconds = time.perf_counter() - replay_start
        step_replay_days = len(self._calendar.trading_days_between(self._start, trade_date))
        # 重放一致性：历史 NAV 必须与上一步记录相同（同引擎、同输入 → 逐位一致）
        replay_nav = float(result.nav.loc[trade_date])
        nav_at_signal = float(result.nav.loc[signal_date])
        if not np.isclose(nav_at_signal, self._last_nav, rtol=0.0, atol=1e-9):
            raise RuntimeError(
                f"重放状态漂移：{signal_date} NAV={nav_at_signal}，"
                f"上一步记录={self._last_nav}（逐日重放必须确定性）"
            )

        decision = self._gate_decision(result, trade_date)
        if decision is not None:
            decision_pending = str(decision.get("pending_status", "none"))
            gate_attempts = int(decision.get("gate_attempts", 0))
            policy_source = str(decision.get("source", "none"))
            decision_risk_off = bool(decision.get("risk_off", False))
        else:  # 兼容旧结果（无决策日志）
            decision_pending = "none"
            gate_attempts = 0
            policy_source = "none"
            decision_risk_off = False

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
        gate_rejected = len(rejected_on) > 0
        gate_reject_reason = str(rejected_on["reason"].iloc[0]) if gate_rejected else ""

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
        policy_actions = self._policy_actions(result, trade_date)

        # 政策替代：滚动日机械换券优先（旧 pending 被取消）或风控熔断强制目标。
        cancelled_by_policy = False
        if not action.is_hold and policy_source == "policy_roll":
            cancelled_by_policy = True
        if decision_risk_off:
            # 引擎在 risk_off 信号日直接以基准中性目标替代策略动作；原动作未提交。
            cancelled_by_policy = True

        projected_target, projected_faces = self._projected_action_target(
            action,
            decision=decision,
            cancelled_by_policy=cancelled_by_policy,
            strategy=strategy,
            signal_date=signal_date,
        )
        rejected = bool(
            gate_rejected
            and not cancelled_by_policy
            and action.action_type in ("target_krd", "target_faces")
        )
        reject_reason = gate_reject_reason if rejected else ""

        action_scale = self._action_scale(
            action,
            projected_target=projected_target,
            projected_faces=projected_faces,
            executed_trades=executed_trades,
            previous_faces=previous_faces,
        )
        # 每笔成交同时带上当日闸门尝试次数与动作规模比（审计可独立读取）。
        executed_trades = tuple(
            {
                **item,
                "attempted": gate_attempts,
                "scale": action_scale,
            }
            for item in executed_trades
        )
        if action.action_type == "hold":
            action_outcome: ActionOutcome = (
                "cancelled_by_policy" if cancelled_by_policy else "no_trade_deadband"
            )
        elif cancelled_by_policy:
            action_outcome = "cancelled_by_policy"
        elif rejected:
            action_outcome = "rejected"
        elif not executed_trades:
            action_outcome = "no_trade_deadband"
        elif abs(action_scale - 1.0) > 1e-9:
            action_outcome = "scaled"
        else:
            action_outcome = "executed"

        notes = self._step_notes(
            action=action,
            action_outcome=action_outcome,
            cancelled_by_policy=cancelled_by_policy,
            policy_source=policy_source,
            decision_pending=decision_pending,
            roll_on_day=roll_on_day,
            rejected=rejected,
            reject_reason=reject_reason,
            executed_trades=executed_trades,
            gate_attempts=gate_attempts,
            action_scale=action_scale,
            risk_off=risk_off,
        )
        substitution_detail = self._substitution_detail(
            action=action,
            action_outcome=action_outcome,
            policy_source=policy_source,
            projected_target=projected_target,
            projected_faces=projected_faces,
            executed_trades=executed_trades,
            previous_faces=previous_faces,
            action_scale=action_scale,
            gate_attempts=gate_attempts,
            reject_reason=reject_reason,
            cancelled_by_policy=cancelled_by_policy,
        )

        record = StepRecord(
            date=trade_date,
            original_action=action,
            projected_target=projected_target,
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
            action_outcome=action_outcome,
            action_scale=action_scale,
            policy_actions=policy_actions,
            substitution_detail=substitution_detail,
            replay_days=step_replay_days,
            replay_cost_seconds=replay_seconds,
        )

        self._records.append(record)
        self._cursor = trade_date
        self._result = result
        self._last_nav = replay_nav
        self._events = _build_events(result)
        self._replay_days += step_replay_days
        self._replay_cost_seconds += replay_seconds
        return self._observation(trade_date), record

    # ---- 终止义务 ------------------------------------------------------
    def rollout(
        self,
        policy: Any,
        start: date,
        end: date,
        *,
        name: str = "rl_rollout",
    ) -> Rollout:
        """用**一次** ``run_backtest`` 跑完整 episode（O(n)，训练用快速路径）。

        ``policy``：``Callable[[MarketContext], ActionSpec | TargetKRD | TargetFaces | None]``，
        在引擎每个信号日收盘后调用；返回值语义与 :class:`ActionSpec` 相同（None=hold）。
        与逐步 ``step`` 使用同一引擎/闸门/滚动/成本；返回的记录按执行日构造，便于与
        ``step`` 对照。交互式调试仍可用 ``step``（O(n²)）。
        """
        start = _coerce_date(start, name="start")
        end = _coerce_date(end, name="end")
        days = self._calendar.trading_days_between(start, end)
        if len(days) < 2:
            raise ValueError(f"rollout [{start}, {end}] 内交易日不足 2 天")
        actions: dict[date, ActionSpec] = {}

        rollout_name = name

        class _PolicyStrategy:
            @property
            def name(self) -> str:  # engine 读取 strategy.name
                return rollout_name

            def target_krd(self, ctx: MarketContext) -> TargetKRD | TargetFaces | None:
                raw = policy(ctx)
                if isinstance(raw, ActionSpec):
                    actions[ctx.as_of] = raw
                    if raw.action_type == "hold":
                        return None
                    if raw.action_type == "target_faces":
                        return TargetFaces(
                            as_of=ctx.as_of,
                            faces=dict(raw.target_faces or {}),
                            notes=raw.notes or "RL rollout faces",
                        )
                    return TargetKRD(
                        as_of=ctx.as_of,
                        krd=dict(raw.target_krd or {}),
                        notes=raw.notes or "RL rollout KRD",
                    )
                if isinstance(raw, (TargetKRD, TargetFaces)):
                    actions[ctx.as_of] = ActionSpec()
                    return raw
                if raw is None:
                    actions[ctx.as_of] = ActionSpec()
                    return None
                raise TypeError(f"policy 返回类型非法：{type(raw).__name__}")

        strategy = _PolicyStrategy()
        result = run_backtest(
            strategy,
            panel=self._panel,
            calendar=self._calendar,
            funding=self._funding,
            cfg=self._cfg,
            start=start,
            end=end,
        )
        records: list[StepRecord] = []
        decisions = {item["date"]: item for item in getattr(result, "gate_decisions", [])}
        trades = result.trades
        for day in days[1:]:
            decision = decisions.get(day)
            if decision is None:
                continue
            signal_date = decision.get("signal_date")
            action = actions.get(signal_date) if signal_date is not None else None
            if action is None:
                action = ActionSpec()
            trades_on = (
                trades.loc[_dates_of(trades["trade_date"]) == day] if len(trades) else trades
            )
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
                    "attempted": int(decision.get("gate_attempts", 0)),
                    "scale": 1.0,
                }
                for _, item in trades_on.iterrows()
            )
            nav_before = (
                float(result.nav.loc[signal_date])
                if signal_date in result.nav.index
                else float("nan")
            )
            nav_after = float(result.nav.loc[day])
            status = str(decision.get("status", "no_action"))
            source = str(decision.get("source", "none"))
            if source == "policy_roll" and action.action_type != "hold":
                outcome = "cancelled_by_policy"
            elif status == "rejected":
                outcome = "rejected"
            elif status == "partial_de_risk":
                outcome = "scaled"
            elif not executed_trades and action.action_type != "hold":
                outcome = "no_trade_deadband"
            else:
                outcome = "executed"
            rolls = result.roll_events
            policy_rows = rolls.loc[_dates_of(rolls["date"]) == day] if len(rolls) else rolls
            policy_actions = tuple(
                PolicyAction(
                    day=day,
                    tenor=float(row["tenor"]),
                    retired_code=str(row["retired_code"]),
                    issued_code=str(row["issued_code"]),
                    pending_status=str(row.get("pending_status", "")),
                    strategy_trade_ids=str(row.get("strategy_trade_ids", "")),
                    status=str(row.get("status", "")),
                )
                for _, row in policy_rows.iterrows()
            )
            records.append(
                StepRecord(
                    date=day,
                    original_action=action,
                    projected_target=decision.get("projected_target"),
                    projected_faces=tuple(decision.get("projected_faces") or ()),
                    executed_trades=executed_trades,
                    rejected=bool(decision.get("rejected", False)),
                    reject_reason=str(decision.get("reject_reason", "")),
                    gate_level=float(decision.get("gate_level", 1.0)),
                    risk_off=bool(decision.get("risk_off", False)),
                    nav_before=nav_before,
                    nav_after=nav_after,
                    reward=nav_after - nav_before,
                    executed_face=float(sum(t["face_value"] for t in executed_trades)),
                    gate_attempts=int(decision.get("gate_attempts", 0)),
                    policy_source=source,
                    pending_status=str(decision.get("pending_status", "none")),
                    action_outcome=outcome,
                    policy_actions=policy_actions,
                    replay_days=len(days),
                    replay_cost_seconds=0.0,
                    notes="rollout：单次引擎运行（O(n) 训练路径）",
                )
            )
        return Rollout(records=tuple(records), result=result)

    def terminate(self) -> TerminalState:
        """返回 episode 终止时的估值与未结算义务（**不强制平仓**）。

        只有在 episode 结束（``cursor >= end``）且已有回放结果时可调用。NAV 与
        ``result.nav`` 末值逐位一致；reward 只取账本 NAV 的逐日变化（含市价重估），
        不通过"强制平仓"制造期末收益；未结算成交/现金/费用单独披露。
        """
        if self._end is None or self._result is None:
            raise RuntimeError("必须先 reset/step 后再 terminate")
        if not self.done:
            raise RuntimeError(
                f"episode 尚未结束（cursor={self._cursor}, end={self._end}），不能 terminate"
            )
        result = self._result
        end = self._end
        nav = float(result.nav.loc[end])
        exposure = result.exposure
        exposure_row = exposure.loc[_dates_of(exposure["date"]) == end]
        cash = float(exposure_row["cash"].iloc[0]) if len(exposure_row) else float("nan")
        pending_cash = (
            float(exposure_row["pending_cash"].iloc[0]) if len(exposure_row) else float("nan")
        )
        positions = (
            result.positions.loc[_dates_of(result.positions["as_of"]) == end].copy()
            if len(result.positions)
            else result.positions.copy()
        )

        unsettled: list[dict[str, Any]] = []
        unsettled_cash = 0.0
        accrued_fees = 0.0
        for _, trade in result.trades.iterrows():
            settlement_date = _to_date(trade["settlement_date"])
            if settlement_date <= end:
                continue
            bond = _bond_from_trade_row(trade)
            ai = float(accrued_interest(bond, settlement_date, cfg=self._cfg))
            face = float(trade["face_value"])
            clean = float(trade["clean_price"])
            gross = face / 100.0 * (clean + ai)
            signed_cash = -gross if str(trade["side"]) == "buy" else gross
            fee = _estimated_trade_fee(
                cfg=self._cfg, face=face, clean_price=clean, contract_fee=float(trade["fee"])
            )
            unsettled_cash += signed_cash
            accrued_fees += fee
            unsettled.append(
                {
                    "trade_id": str(trade["trade_id"]),
                    "bond_code": str(trade["bond_code"]),
                    "side": str(trade["side"]),
                    "face_value": face,
                    "clean_price": clean,
                    "trade_date": _to_date(trade["trade_date"]),
                    "settlement_date": settlement_date,
                    "accrued_interest": ai,
                    "settlement_amount": gross,
                    "signed_cash": signed_cash,
                    "fee": fee,
                }
            )

        market_value = (
            float(positions["market_value"].sum())
            if len(positions) and "market_value" in positions
            else 0.0
        )
        reconciliation = cash + market_value + unsettled_cash
        pending_gap = unsettled_cash - pending_cash
        note_parts = [
            "终止口径：按市价 mark-to-market，不强制平仓；reward 取账本 NAV 逐日变化，"
            "不追加终止变现收益/成本",
            (
                f"未结算成交 {len(unsettled)} 笔，待结算现金 {unsettled_cash:,.2f} 元"
                "（买入为负、卖出为正）"
            ),
            (
                f"未结算费用 {accrued_fees:,.2f} 元按 cfg.costs.fee_bp + 每笔结算费 + "
                "TradeRecord.fee 估算；引擎在结算日才扣费，故未计入期末 NAV"
            ),
            (
                f"NAV 对账：cash({cash:,.2f}) + ΣMV({market_value:,.2f}) + "
                f"pending_cash({unsettled_cash:,.2f}) = {reconciliation:,.2f}，"
                f"result.nav 末值 {nav:,.2f}"
            ),
        ]
        if np.isfinite(pending_gap) and abs(pending_gap) > 1e-6:
            note_parts.append(f"待结算现金与 exposure.pending_cash 差额 {pending_gap:,.6f} 元")
        return TerminalState(
            nav_mark_to_market=nav,
            cash=cash,
            positions=positions,
            unsettled_trades=tuple(unsettled),
            unsettled_cash=unsettled_cash,
            accrued_fees=accrued_fees,
            liquidation_assumption="mark_to_market_no_forced_liquidation",
            notes="；".join(note_parts),
        )

    # ---- 内部 ----------------------------------------------------------
    def _gate_decision(self, result: BacktestResult, day: date) -> dict[str, Any] | None:
        for item in getattr(result, "gate_decisions", []):
            if item.get("date") == day:
                return item
        return None

    def _positions_at(self, day: date) -> dict[str, float]:
        """上一个已回放结果在 ``day`` 的持仓面值（用于 faces 动作的执行 scale）。"""
        result = self._result
        if result is None or len(result.positions) == 0:
            return {}
        frame = result.positions
        rows = frame.loc[_dates_of(frame["as_of"]) == day]
        return {str(item["bond_code"]): float(item["face_value"]) for _, item in rows.iterrows()}

    def _projected_action_target(
        self,
        action: ActionSpec,
        *,
        decision: dict[str, Any] | None,
        cancelled_by_policy: bool,
        strategy: _ScriptedStrategy,
        signal_date: date,
    ) -> tuple[TargetKRD | None, tuple[tuple[str, float], ...]]:
        """只返回**原动作**经闸门后的提交目标；政策换券不混入。"""
        if cancelled_by_policy or action.action_type == "hold":
            return None, ()
        if action.action_type == "target_faces":
            # 面值动作绕过 KRD 求解；提交的“目标”就是逐券面值，而不是引擎内部
            # 为复用闸门而构造的基准 KRD。引擎决策日志不保存 signal 分支的 faces，
            # 这里用原动作面值（它正是 _orders_from_faces 的输入）。
            faces = tuple((str(k), float(v)) for k, v in (action.target_faces or {}).items())
            if decision is None:
                emitted = strategy.emitted.get(signal_date)
                if isinstance(emitted, TargetFaces):
                    faces = tuple((str(k), float(v)) for k, v in emitted.faces.items())
            return None, faces
        if decision is not None:
            raw = decision.get("projected_target")
            target = raw if isinstance(raw, TargetKRD) else None
            faces = tuple(
                (str(code), float(face)) for code, face in (decision.get("projected_faces") or ())
            )
            return target, faces
        emitted = strategy.emitted.get(signal_date)
        if isinstance(emitted, TargetKRD):
            return emitted, ()
        return None, ()

    def _action_scale(
        self,
        action: ActionSpec,
        *,
        projected_target: TargetKRD | None,
        projected_faces: tuple[tuple[str, float], ...],
        executed_trades: tuple[dict[str, Any], ...],
        previous_faces: Mapping[str, float],
    ) -> float:
        """提交/执行规模相对原始动作的比例（>1 表示向基准回撤时被放大）。"""
        if action.action_type == "hold":
            return 1.0
        if action.action_type == "target_krd":
            original = float(sum((action.target_krd or {}).values()))
            if projected_target is None or abs(original) <= 1e-12:
                return 1.0
            return float(projected_target.total_dv01) / original
        # target_faces：机械闸门只缩放买入腿；用"实际买入面值/请求买入面值"度量。
        requested_buy = 0.0
        for code, target_face in (action.target_faces or {}).items():
            delta = float(target_face) - float(previous_faces.get(code, 0.0))
            if delta > 0.0:
                requested_buy += delta
        executed_buy = float(
            sum(item["face_value"] for item in executed_trades if item["side"] == "buy")
        )
        if requested_buy <= 1e-9 or not executed_trades:
            return 1.0
        return min(1.0, executed_buy / requested_buy)

    def _policy_actions(self, result: BacktestResult, day: date) -> tuple[PolicyAction, ...]:
        """把 ``roll_events`` 当日行映射成独立的机械换券动作记录（一一对应）。"""
        rows = self._roll_rows(result, day)
        actions: list[PolicyAction] = []
        for _, row in rows.iterrows():
            ids = tuple(
                part.strip()
                for part in str(row.get("strategy_trade_ids", "")).split(";")
                if part.strip()
            )
            bench_ids = tuple(
                str(row[column])
                for column in ("bench_sell_trade_id", "bench_buy_trade_id")
                if str(row.get(column, "") or "")
            )
            actions.append(
                PolicyAction(
                    day=_to_date(row["date"]),
                    tenor=float(row["tenor"]),
                    retired_code=str(row["retired_code"]),
                    issued_code=str(row["issued_code"]),
                    pending_status=str(row.get("pending_status", "") or ""),
                    strategy_trade_ids=ids,
                    status=str(row.get("status", "") or "pending"),
                    reason=str(row.get("reason", "") or ""),
                    gate_attempts=int(row.get("gate_attempts", 0) or 0),
                    bench_trade_ids=bench_ids,
                )
            )
        return tuple(actions)

    def _step_notes(
        self,
        *,
        action: ActionSpec,
        action_outcome: ActionOutcome,
        cancelled_by_policy: bool,
        policy_source: str,
        decision_pending: str,
        roll_on_day: pd.DataFrame,
        rejected: bool,
        reject_reason: str,
        executed_trades: tuple[dict[str, Any], ...],
        gate_attempts: int,
        action_scale: float,
        risk_off: bool,
    ) -> str:
        parts: list[str] = []
        if cancelled_by_policy:
            if policy_source == "policy_roll" or decision_pending == "cancelled_by_roll":
                parts.append(
                    "滚动日：原动作被机械换券取消/替代（cancelled_by_policy）；"
                    "机械换券单独记入 policy_actions，不写入 projected_target"
                )
            else:
                parts.append("风控熔断：引擎以基准中性目标替代原动作（原动作未提交）")
            parts.append("原始动作未提交；当日成交（如有）属于政策/风控动作")
        elif not roll_on_day.empty:
            parts.append(f"滚动日：{len(roll_on_day)} 个期限机械换券，见 policy_actions")
        if action.is_hold:
            parts.append("零动作（hold）：持仓不动，无主动成交")
        elif cancelled_by_policy:
            pass
        elif rejected:
            parts.append(f"闸门拒单：{reject_reason}")
        elif executed_trades:
            if action_outcome == "scaled":
                parts.append(
                    f"闸门缩放：提交/原始规模={action_scale:.6g}，gate_attempts={gate_attempts}"
                )
            else:
                parts.append(f"原动作经闸门执行（gate_attempts={gate_attempts}）")
        else:
            parts.append("原始动作未产生成交（低于死区/约束，或 face 目标与当前持仓一致）")
        if risk_off and not cancelled_by_policy:
            parts.append("当日约束记录含风控熔断状态")
        return "；".join(parts)

    def _substitution_detail(
        self,
        *,
        action: ActionSpec,
        action_outcome: ActionOutcome,
        policy_source: str,
        projected_target: TargetKRD | None,
        projected_faces: tuple[tuple[str, float], ...],
        executed_trades: tuple[dict[str, Any], ...],
        previous_faces: Mapping[str, float],
        action_scale: float,
        gate_attempts: int,
        reject_reason: str,
        cancelled_by_policy: bool,
    ) -> dict[str, Any]:
        """原始动作 vs 提交目标 vs 实际成交（face/dv01 差）。"""
        executed_faces: dict[str, float] = {}
        for item in executed_trades:
            code = str(item["bond_code"])
            signed = float(item["face_value"]) * (1.0 if item["side"] == "buy" else -1.0)
            executed_faces[code] = executed_faces.get(code, 0.0) + signed
        original_faces = (
            {str(k): float(v) for k, v in (action.target_faces or {}).items()}
            if action.action_type == "target_faces"
            else None
        )
        submitted_faces = {str(code): float(face) for code, face in projected_faces}
        original_total = action.target_total
        projected_total = (
            float(projected_target.total_dv01)
            if projected_target is not None
            else (float(sum(submitted_faces.values())) if submitted_faces else None)
        )
        executed_gross = float(sum(abs(item["face_value"]) for item in executed_trades))
        projected_delta_faces: dict[str, float] | None = None
        if original_faces is not None:
            projected_delta_faces = {
                code: float(target) - float(previous_faces.get(code, 0.0))
                for code, target in original_faces.items()
            }
        face_diff: float | None = None
        if projected_delta_faces is not None:
            executed_net = float(sum(executed_faces.values()))
            face_diff = executed_net - float(sum(projected_delta_faces.values()))
        elif submitted_faces:
            face_diff = executed_gross - float(sum(submitted_faces.values()))
        dv01_diff = (
            projected_total - original_total
            if projected_total is not None and action.action_type == "target_krd"
            else None
        )
        return {
            "action_type": action.action_type,
            "action_outcome": action_outcome,
            "policy_source": policy_source,
            "cancelled_by_policy": cancelled_by_policy,
            "original_total": original_total,
            "original_faces": original_faces,
            "projected_total": projected_total,
            "submitted_faces": submitted_faces,
            "projected_delta_faces": projected_delta_faces,
            "executed_faces": executed_faces,
            "executed_gross_face": executed_gross,
            "face_diff": face_diff,
            "dv01_diff": dv01_diff,
            "action_scale": action_scale,
            "gate_attempts": gate_attempts,
            "executed_trade_ids": tuple(str(item["trade_id"]) for item in executed_trades),
            "reject_reason": reject_reason,
            "previous_faces": dict(previous_faces),
        }

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
                "performance_note": "每次 step 从 episode 起点重放 run_backtest（O(n²) 原型）",
                "replay_days": self._replay_days,
                "replay_cost_seconds": self._replay_cost_seconds,
            },
        )


def _bond_from_trade_row(row: Any) -> BondStatic:
    """从 ``result.trades`` 的一行恢复债券条款（用于结算日应计利息）。"""
    return BondStatic.model_validate(
        {
            "code": str(row["bond_code"]),
            "issue_date": _to_date(row["issue_date"]),
            "maturity_date": _to_date(row["maturity_date"]),
            "coupon_pct": float(row["coupon_pct"]),
            "freq": int(row["freq"]),
            "accrual": str(row["accrual"]),
            "market": str(row["market"]),
        }
    )


def _estimated_trade_fee(
    *, cfg: Config, face: float, clean_price: float, contract_fee: float
) -> float:
    """估算一笔成交在结算日将扣的费用（与账本费用口径一致，见 ledger 文档）。

    账本 ``_trade_fee`` = ``fee_bp/1e4 × 净价成交金额 + settlement_fee_cny_per_trade
    + TradeRecord.fee``；该费用在**结算日**才写 ``fee`` 现金事件，因此期末未结算
    成交对应的费用尚未进入 NAV。环境不调用账本私有方法，按同一公开参数重建。
    """
    notional = face / 100.0 * clean_price
    return (
        float(cfg.costs.fee_bp) / 1e4 * notional
        + float(cfg.costs.settlement_fee_cny_per_trade)
        + float(contract_fee)
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
