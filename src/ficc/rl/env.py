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
- **as-of / 信息滞后（R51/R57/R59）**：**曲线/历史/基准/NAV/现金/持仓估值**都只能
  看到 ``available_through = t − information_lag_days``（默认 1 个交易日，中债 t 日
  收盘估值当日不可用）；持仓数量/条款与成交仍按 t 日已知状态披露，但 t 日曲线估值
  字段置 NaN。``Observation.as_of`` 仍是 episode 当前交易日，另用
  ``metadata["available_through"]``/``nav_as_of`` 披露信息截止日。预热不足时
  ``reset/rollout`` 直接拒绝，绝不把截止日向未来夹紧。执行/估值/闸门仍用当日价格。
- **决策网格（R51）**：episode 起始交易日为索引 0；``decision_step_days > 1`` 时只有
  ``索引 % step == 0`` 的交易日允许策略给出目标，其余交易日强制 hold（不调用 policy、
  不产生主动订单），记录 ``notes`` 含 ``off_grid_hold``。step 与 rollout 共用该网格，
  避免调用方各自 ``days[::5]`` 造成错位。滚动换券/风控熔断是引擎外生事件，不受网格约束。

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
from dataclasses import dataclass, field, replace
from datetime import date, datetime
from typing import Any, Literal, NamedTuple

import numpy as np
import pandas as pd

from ficc.backtest.engine import BacktestResult, run_backtest
from ficc.config import Config, get_config
from ficc.curve.curve import Curve
from ficc.data.schema import CANONICAL_TENORS, BondStatic
from ficc.ml.asof import _coerce_date
from ficc.portfolio.benchmark import build_benchmark, mark_faces
from ficc.portfolio.constraints import portfolio_risk
from ficc.portfolio.rolling import RollingUniverse
from ficc.portfolio.types import BenchmarkPortfolio, MarketContext, TargetFaces, TargetKRD
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
    "Rollout",
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
    "replaced_by_risk_off",
    "no_trade_deadband",
    "hold",
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
      （``cfg.costs.fee_bp`` + 每笔结算费 + ``TradeRecord.fee``）；**已发生未支付**，
      单列披露、不计入 ``nav_mark_to_market``，净终值见 :attr:`net_terminal_value`；
    - ``liquidation_assumption``：默认按市价估值、**不强制平仓**。reward 只取账本
      NAV 的逐日变化（含持仓按市价重估的未实现变动），不因终止变现追加收益/成本；
    - **价差不重复扣**：成交日半价差已由账本计入 NAV/成本表，终止估值不再扣一次；
    - 未结算成交、待结算现金与费用作为义务单独披露。
    """

    nav_mark_to_market: float
    cash: float
    positions: pd.DataFrame
    unsettled_trades: tuple[dict[str, Any], ...]
    unsettled_cash: float
    accrued_fees: float
    liquidation_assumption: str
    notes: str

    @property
    def net_terminal_value(self) -> float:
        """净终值 = NAV − 已发生未支付费用（未结算现金义务已包含在 NAV 内）。

        默认不强平，因此不含任何变现价差/资本利得调整；价差已在成交日按账本扣一次。
        """
        return float(self.nav_mark_to_market) - float(self.accrued_fees)


@dataclass(frozen=True)
class Observation:
    """``as_of`` 时点的观测（**信息截止早于 as_of，R51**）。

    - ``as_of``：episode 当前交易日（信号日/成交日语义；不是信息截止日）；
    - ``curve``：``available_through = as_of − information_lag_days`` 的曲线，
      因此 ``curve.as_of == available_through ≤ as_of``；
    - ``benchmark_krd``：episode 起点固定身份的真实基准在 ``available_through``
      的 KRD（元/bp；R58，不逐日重新发行）；
    - ``positions``：``as_of`` 已知持仓数量/条款（债券级长表；reset 首日为空表）；
      ``remaining_years/ytm_pct/clean_price/dirty_price/market_value/mod_duration/dv01``
      置 NaN（R59），不得据此推断 t 日曲线；
    - ``cash`` / ``nav``：同一账本在 ``available_through``（不是 ``as_of``）的已结算
      现金与 NAV；``metadata["nav_as_of"]`` 给出对应日期；
    - ``history``：截至 ``available_through``（含）的曲线面板切片，
      ``index.max() == available_through``；
    - ``metadata``：episode 名、``as_of`` / ``available_through`` / ``nav_as_of`` /
      ``valuation_available=False`` / ``information_lag_days`` / 网格索引等。
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
    滚动日的机械换券不写入 ``projected_target``（滚动日恒为 ``None``）：它单独记在
    ``policy_actions``；当日机械换券实际提交的面值另记在 ``projected_faces``（仅作
    披露，不属于原动作），原动作若被取消则 ``action_outcome="cancelled_by_policy"``。

    **三阶段面值（R55）**——不能再用原请求冒充最终提交/成交：

    - ``requested_faces``：原动作的**目标持仓面值**（仅 ``target_faces`` 动作；
      ``target_krd``/hold 为空）；
    - ``projected_faces``：**最终接受订单的带符号面值**（``(code, +buy/−sell)``，
      来自引擎 ``gate_decisions.projected_faces``）。无成交/拒单/被风控替代时为空；
      滚动政策日保留机械换券订单披露，并由 ``substitution_detail.projected_faces_source``
      标注 ``policy_roll``，不得混入原动作；
    - ``accepted_orders``：最终接受订单明细（``bond_code/side/face_value/clean_price/
      spread_cost``，来自 ``gate_decisions.accepted_orders``）；
    - ``post_trade_faces``：该交易日 ``result.positions`` 的**期末多头面值**（不是请求值，
      也不是订单增量）。

    ``executed_trades`` 是账本实际提交的成交（可能为空），每条带当日闸门尝试次数
    ``attempted`` 与动作规模比 ``scale``（与 ``action_scale`` 相同）。``substitution_detail``
    记录原始动作、接受订单与实际成交的 face/dv01/逐桶差异。
    ``action_scale`` 是提交目标相对原始动作的**保守**规模比：KRD 取总 DV01 比例，
    但若逐桶形状有改动（``max_bucket_deviation > bucket_tolerance``，R53）则取
    ``min(total_ratio, 1 − max_bucket_deviation / max(1, Σ|orig|))``，保证形状缩放不会
    被记为 ``executed``；faces 用实际买入面值 / 请求买入面值。

    ``reward = nav_after - nav_before``：``nav_before`` 是**该交易日的前一交易日 NAV**
    （step 中即上一记录 NAV），与订单 ``signal_date`` 无关，因此 hold/无订单日也有
    有限 reward；同一账本、同一成本口径，价差不重复扣。
    动作结果：cancelled_by_policy > rejected > hold（持仓不动）> no_trade_deadband
    （非 hold 且无成交）> scaled（``action_scale`` 明显小于 1 或 KRD 逐桶形状改变）>
    executed。非决策网格日强制 hold，``notes`` 含 ``off_grid_hold``（R51）。
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
    # R55：原始请求面值 / 接受订单 / 交易后期末面值（三者独立）
    requested_faces: tuple[tuple[str, float], ...] = ()
    accepted_orders: tuple[dict[str, Any], ...] = ()
    post_trade_faces: tuple[tuple[str, float], ...] = ()
    # 性能披露：本步从 episode 起点重放的交易日数与耗时（O(n²) 接口原型的证据）
    replay_days: int = 0
    replay_cost_seconds: float = 0.0
    # 引擎逐档 candidate（R45 后续）：level/scale、order_faces、feasible、violation_codes
    attempts_detail: tuple[dict[str, Any], ...] = ()

    @property
    def attempted(self) -> int:
        """闸门尝试次数（``gate_attempts`` 的显式别名）。"""
        return self.gate_attempts


@dataclass(frozen=True)
class Rollout:
    """一次 ``run_backtest`` 生成的整段轨迹（**O(n)** 训练路径，替代逐步 O(n²) 重放）。

    ``records`` 与 ``step`` 的 :class:`StepRecord` 同契约（共用同一轨迹构造器）；
    ``result`` 是完整 :class:`BacktestResult`（NAV/成交/约束/归因输入）；``terminal``
    是本次 episode 的 :class:`TerminalState`（与随后 ``env.terminate()`` 返回同一快照）。
    policy 只在该 episode 的决策网格日（索引 % ``decision_step_days`` == 0，R51）被
    调用；收到的 ``MarketContext`` 已按 ``information_lag_days`` 截断
    （``curve/history/benchmark``/``nav/cash`` 截止 ``available_through``；
    ``positions`` 只有数量/条款，估值字段 NaN，R59）。执行仍按 t+1 成交与同一闸门。
    """

    records: tuple[StepRecord, ...]
    result: BacktestResult
    terminal: TerminalState


class Event(NamedTuple):
    """时间序事件：``(date, kind, detail)``，kind ∈ signal/project/execute/reject/roll/settle。"""

    date: date
    kind: EventKind
    detail: dict[str, Any]


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def _curve_at(
    panel: pd.DataFrame,
    tenors: tuple[float, ...],
    day: date,
    *,
    allow_missing: bool = False,
) -> Curve:
    """取 ``day`` 的曲线；``allow_missing`` 时只用当日有报价的期限点。

    R51 的滞后视图会遇到真实数据缺口（例如 2019-01-25 缺 2Y 报价，2Y 自 01-28
    才有）。此时不能退回更早的完整曲线（那会破坏 lag=1 语义），而是用当日可得
    期限点的 Hermite 插值求缺失期限——这是信息截止日内可计算的，不是未来数据。
    非滞后路径保持严格校验（``allow_missing=False``），缺期限仍报错。
    """
    row = panel.loc[pd.Timestamp(day)].reindex(list(tenors))
    if row.isna().any():
        if not allow_missing:
            raise ValueError(f"{day} 曲线存在缺失期限：{row[row.isna()].index.tolist()}")
        row = row.dropna()
        if row.empty:
            raise ValueError(f"{day} 曲线没有任何期限报价")
    return Curve(
        as_of=day,
        tenors=np.asarray([float(t) for t in row.index]),
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


# 非决策网格日强制 hold 的机器可读前缀（R51，测试/审计按子串检索）
_OFF_GRID_NOTE = "off_grid_hold"


_VALUATION_COLUMNS: tuple[str, ...] = (
    "remaining_years",
    "ytm_pct",
    "clean_price",
    "dirty_price",
    "market_value",
    "mod_duration",
    "dv01",
)


@dataclass(frozen=True)
class _PolicyState:
    """策略在某个已观测交易日的账本状态（现金/NAV/持仓数量均为该日已知）。"""

    nav: float
    cash: float
    positions: tuple[Any, ...]


@dataclass(frozen=True)
class _BenchmarkBook:
    """episode 内的真实基准身份：固定条款/面值，只有估值日随信息截止移动。"""

    bonds: tuple[BondStatic, ...]
    faces: Mapping[str, float]
    weights: Mapping[float, float]
    notional: float


def _sanitize_position(position: Any) -> Any:
    """隐藏持仓的估值字段（R59）；只保留债券条款、面值与结算已知字段。"""
    return replace(
        position,
        remaining_years=float("nan"),
        ytm_pct=float("nan"),
        clean_price=float("nan"),
        dirty_price=float("nan"),
        market_value=float("nan"),
        mod_duration=float("nan"),
        dv01=float("nan"),
    )


def _sanitize_positions_frame(
    frame: pd.DataFrame,
    *,
    nav: float,
    cash: float,
    pending_cash: float = float("nan"),
) -> pd.DataFrame:
    """把持仓长表的估值列置 NaN，并统一 NAV/现金为信息截止日的账本状态（R59）。

    数量（``face_value``/``settled_face``）与债券条款保留：成交/结算后即已知，不是
    t 日曲线估值。``accrued_interest`` 由条款与日期决定，也保留。
    """
    out = frame.copy()
    for column in _VALUATION_COLUMNS:
        if column in out.columns:
            out[column] = np.nan
    for column, value in (("nav", nav), ("cash", cash), ("pending_cash", pending_cash)):
        if column in out.columns:
            out[column] = value
    return out


def _lagged_day(
    day: date,
    *,
    calendar: TradingCalendar,
    lag: int,
    panel_first: date,
    panel_dates: frozenset[date],
) -> date:
    """返回 ``day − lag`` 个交易日的信息截止日；**绝不向未来移动**（R57）。

    ``lag <= 0`` 时等于 ``day``（仅用于 lag=0 对照）。若回退越过 panel 起点或落到
    非报价日，抛出 :class:`ValueError`，由调用方拒绝该 episode/决策日，而不是把
    截止日夹到面板首日（那会静默泄漏未来信息）。
    """
    if lag <= 0:
        return day
    out = day
    try:
        for _ in range(lag):
            out = calendar.prev_trading_day(out, 1)
    except ValueError as exc:
        raise ValueError(
            f"{day} 的信息截止日需要回退 {lag} 个交易日，但已早于交易日历起点：{exc}；"
            "需要预热数据或推迟首个决策日"
        ) from exc
    if out < panel_first or out not in panel_dates:
        raise ValueError(
            f"{day} 的信息截止日 {out} 早于/不在 panel 起点 {panel_first}："
            f"需要至少 {lag + 1} 个交易日预热（含截止日本身）；"
            "请提供预热数据或推迟首个决策日"
        )
    return out


def _benchmark_portfolio(
    panel: pd.DataFrame,
    tenors: tuple[float, ...],
    calendar: TradingCalendar,
    cfg: Config,
    day: date,
) -> BenchmarkPortfolio:
    """单点构造基准（**仅测试/审计辅助**；环境滞后视图不再逐日重建）。

    R58 后，:class:`_LaggedView` 使用 episode 起点构造的同一组券/面值，只在
    ``available_through`` 重估；本函数保留给需要独立计算期望值的测试。
    """
    curve = _curve_at(panel, tenors, day, allow_missing=True)
    universe = RollingUniverse.build(curve, day, cfg=cfg).active()
    return build_benchmark(curve, day, bonds=universe, calendar=calendar, cfg=cfg)


def _episode_benchmark_books(
    panel: pd.DataFrame,
    tenors: tuple[float, ...],
    calendar: TradingCalendar,
    cfg: Config,
    start: date,
    end: date,
) -> tuple[_BenchmarkBook, dict[date, _BenchmarkBook]]:
    """在 episode 起点构造一次基准，并逐日推进**同一组券**的滚动状态（R58）。

    返回 ``(initial_book, books)``：``initial_book`` 是引擎在 ``start`` 构造的初始
    基准（起息日 = start），用于 ``available_through < start`` 的估值；``books[d]``
    是引擎在 ``d`` 日实际持有的基准身份/面值（滚动日替换为真实新券、面值沿用旧腿）。
    """
    curve0 = _curve_at(panel, tenors, start)
    rolling = RollingUniverse.build(curve0, start, cfg=cfg)
    initial_bonds = rolling.active()
    benchmark0 = build_benchmark(curve0, start, bonds=initial_bonds, calendar=calendar, cfg=cfg)
    weights = {float(k): float(v) for k, v in benchmark0.weights.items()}
    notional = float(benchmark0.notional or benchmark0.market_value)
    faces: dict[str, float] = {str(k): float(v) for k, v in benchmark0.faces.items()}
    initial = _BenchmarkBook(
        bonds=tuple(initial_bonds),
        faces=dict(faces),
        weights=dict(weights),
        notional=notional,
    )
    books: dict[date, _BenchmarkBook] = {}
    for day in calendar.trading_days_between(start, end):
        curve_d = _curve_at(panel, tenors, day)
        for event in rolling.roll(curve_d, day):
            old_face = float(faces.pop(event.retired.code, 0.0))
            if old_face > 0.0:
                faces[event.issued.code] = old_face
        books[day] = _BenchmarkBook(
            bonds=tuple(rolling.active()),
            faces=dict(faces),
            weights=dict(weights),
            notional=notional,
        )
    return initial, books


class _LaggedView:
    """把 policy 可见信息截到 ``available_through``（R51/R57/R58/R59）。

    - **R57**：截止日只向过去回退；预热不足直接抛错，不再 clamp 到面板首日。
    - **R58**：基准身份在 episode 起点固定（与引擎同一配置/同一组券），滚动日按
      真实滚动事件替换券与面值；``benchmark(available)`` 只重估，不重新发行。
    - **R59**：policy 上下文中的持仓估值置 NaN；``nav/cash`` 取 ``available`` 日
      的账本状态（该日曾在引擎回调中出现，按交易日缓存）。
    """

    def __init__(
        self,
        *,
        panel: pd.DataFrame,
        tenors: tuple[float, ...],
        calendar: TradingCalendar,
        cfg: Config,
        information_lag_days: int,
    ) -> None:
        self._panel = panel
        self._tenors = tenors
        self._calendar = calendar
        self._cfg = cfg
        self._lag = int(information_lag_days)
        self._panel_first = _to_date(panel.index[0])
        self._panel_dates = frozenset(_to_date(ts) for ts in panel.index)
        self._init_cash = float(cfg.backtest.init_cash)
        self._benchmarks: dict[date, BenchmarkPortfolio] = {}
        self._initial_book: _BenchmarkBook | None = None
        self._books: dict[date, _BenchmarkBook] = {}
        self._actual_books: dict[date, _BenchmarkBook] = {}
        self._states: dict[date, _PolicyState] = {}
        self._episode_start: date | None = None
        self._episode_end: date | None = None

    # ---- episode ------------------------------------------------------
    def begin_episode(self, start: date, end: date) -> None:
        """重置 episode 身份/状态缓存；基准只在 ``start`` 构造一次（R58）。"""
        start = _to_date(start)
        end = _to_date(end)
        self._episode_start = start
        self._episode_end = end
        self._states = {}
        self._benchmarks = {}
        self._actual_books = {}
        self._initial_book, self._books = _episode_benchmark_books(
            self._panel, self._tenors, self._calendar, self._cfg, start, end
        )

    def _ensure_episode(self, day: date) -> None:
        if self._initial_book is None:
            self.begin_episode(day, day)

    # ---- 信息截止 ------------------------------------------------------
    def available_through(self, day: date) -> tuple[date, bool]:
        """``day`` 的信息截止日；不足预热时抛错（第二返回值仅为兼容旧签名）。"""
        day = _to_date(day)
        out = _lagged_day(
            day,
            calendar=self._calendar,
            lag=self._lag,
            panel_first=self._panel_first,
            panel_dates=self._panel_dates,
        )
        return out, False

    # ---- policy 状态 ---------------------------------------------------
    def observe(self, ctx: MarketContext) -> None:
        """记录引擎在 ``ctx.as_of`` 的账本状态，供之后 lag 日回看。"""
        self._states[_to_date(ctx.as_of)] = _PolicyState(
            nav=float(ctx.nav),
            cash=float(ctx.cash),
            positions=tuple(ctx.positions),
        )

    def _state_at(self, day: date) -> _PolicyState:
        """``day`` 的已观测账本状态；早于 episode 起点时为空仓/初始现金。"""
        day = _to_date(day)
        if self._episode_start is not None and day < self._episode_start:
            return _PolicyState(
                nav=self._init_cash,
                cash=self._init_cash,
                positions=(),
            )
        state = self._states.get(day)
        if state is not None:
            return state
        earlier = [item for item in self._states if item <= day]
        if earlier:
            return self._states[max(earlier)]
        return _PolicyState(nav=self._init_cash, cash=self._init_cash, positions=())

    # ---- 基准 ----------------------------------------------------------
    def _book_at(self, day: date) -> _BenchmarkBook:
        day = _to_date(day)
        if day in self._actual_books:
            return self._actual_books[day]
        if day in self._books:
            return self._books[day]
        self._ensure_episode(day)
        if self._episode_start is not None and day < self._episode_start:
            assert self._initial_book is not None
            return self._initial_book
        candidates = [item for item in self._books if item <= day]
        if candidates:
            return self._books[max(candidates)]
        assert self._initial_book is not None
        return self._initial_book

    def benchmark(self, day: date) -> BenchmarkPortfolio:
        """``day``（信息截止日）的真实基准估值；同一组券只重估、不重新发行。"""
        day = _to_date(day)
        cached = self._benchmarks.get(day)
        if cached is not None:
            return cached
        book = self._book_at(day)
        bench = mark_faces(
            dict(book.faces),
            bonds=book.bonds,
            curve=_curve_at(self._panel, self._tenors, day, allow_missing=True),
            as_of=day,
            calendar=self._calendar,
            cfg=self._cfg,
            weights=dict(book.weights),
            notional=book.notional,
        )
        self._benchmarks[day] = bench
        return bench

    def sync_from_result(self, result: BacktestResult) -> None:
        """用引擎真实基准持仓同步身份/面值（R58 滚动政策）。

        ``result.benchmark_positions`` 给出每个估值日的**实际基准持仓**（含滚动后的
        新券与面值）；债券条款从 ``result.benchmark_trades`` 的成交行恢复。同步只
        更新 ``actual_books``（之后的估值仍用 ``mark_faces`` 在 available 日重估），
        不会用 ``RollingUniverse.build(available_day)`` 重新发行。
        """
        if self._initial_book is None:
            return
        bonds_by_code: dict[str, BondStatic] = {
            bond.code: bond for bond in self._initial_book.bonds
        }
        trades = getattr(result, "benchmark_trades", None)
        if trades is not None and len(trades):
            for _, row in trades.iterrows():
                bonds_by_code[str(row["bond_code"])] = _bond_from_trade_row(row)
        positions = getattr(result, "benchmark_positions", None)
        if positions is None or len(positions) == 0:
            return
        frame = positions.copy()
        frame["_day"] = _dates_of(frame["as_of"])
        for day, rows in frame.groupby("_day", sort=True):
            faces: dict[str, float] = {}
            for _, row in rows.iterrows():
                code = row["bond_code"]
                if code is None or (isinstance(code, float) and np.isnan(code)):
                    continue
                face = float(row["face_value"])
                if not np.isfinite(face) or face <= 0.0:
                    continue
                faces[str(code)] = face
            if not faces:
                continue
            missing = [code for code in faces if code not in bonds_by_code]
            if missing:
                continue
            self._actual_books[_to_date(day)] = _BenchmarkBook(
                bonds=tuple(bonds_by_code[code] for code in faces),
                faces=faces,
                weights=dict(self._initial_book.weights),
                notional=self._initial_book.notional,
            )
            self._benchmarks.pop(_to_date(day), None)

    # ---- policy 上下文 -------------------------------------------------
    def context(self, ctx: MarketContext) -> MarketContext:
        """返回 policy 可见的滞后上下文（R51/R57/R59）。

        ``curve/history/benchmark`` 截到 ``available_through``；``nav/cash`` 取该日
        账本状态；``positions`` 只保留数量/条款，估值字段全部 NaN。``as_of`` 仍是
        决策日 t（信号日/成交日语义不变）。
        """
        as_of = _to_date(ctx.as_of)
        self.observe(ctx)
        available = self.available_through(as_of)[0]
        state = self._state_at(available)
        return MarketContext(
            as_of=as_of,
            curve=_curve_at(self._panel, self._tenors, available, allow_missing=True),
            history=self._panel.loc[: pd.Timestamp(available)],
            benchmark=self.benchmark(available),
            nav=state.nav,
            cash=state.cash,
            positions=tuple(_sanitize_position(position) for position in state.positions),
            cfg=ctx.cfg,
        )


def _normalise_action(raw: Any) -> ActionSpec:
    """把策略返回值**无损**规范化为 :class:`ActionSpec`（R48）。

    原生 ``TargetKRD`` / ``TargetFaces`` 必须保留完整的目标向量/面值，不能退化成
    hold；``None`` 按 hold（引擎的"零动作"）。``TargetKRD.krd`` 为空时引擎语义是
    "缺失 tenor 取基准"，``ActionSpec`` 无法无损表达（空映射会被当成 hold），因此
    显式报错而不是静默改变动作含义。StepRecord 的 ``original_action`` 与 ``step``
    路径使用同一动作语义。
    """
    if isinstance(raw, ActionSpec):
        return raw
    if isinstance(raw, TargetKRD):
        if not raw.krd:
            raise ValueError("TargetKRD.krd 为空，无法无损规范化为 ActionSpec")
        return ActionSpec(action_type="target_krd", target_krd=dict(raw.krd), notes=raw.notes)
    if isinstance(raw, TargetFaces):
        return ActionSpec(action_type="target_faces", target_faces=dict(raw.faces), notes=raw.notes)
    if raw is None:
        return ActionSpec()
    raise TypeError(f"policy 返回类型非法：{type(raw).__name__}")


def _trade_dict(item: Any) -> dict[str, Any]:
    """成交行 → StepRecord 的成交字典（step 与 rollout 共用，保证逐字段一致）。"""
    return {
        "trade_id": str(item["trade_id"]),
        "bond_code": str(item["bond_code"]),
        "side": str(item["side"]),
        "face_value": float(item["face_value"]),
        "clean_price": float(item["clean_price"]),
        "signal_date": _to_date(item["signal_date"]),
        "trade_date": _to_date(item["trade_date"]),
        "settlement_date": _to_date(item["settlement_date"]),
    }


def hold_target_krd(ctx: MarketContext, *, calendar: TradingCalendar, cfg: Config) -> TargetKRD:
    """零动作目标：**保持当前组合 KRD 不变**（不主动买卖）。

    通过 ``portfolio_risk`` 从 ``ctx.positions`` 计算当前 KRD（与闸门/报告同一风险函数），
    因此零动作只表达"什么都不做"，不是"回到基准"。
    """
    risk = portfolio_risk(ctx.positions, ctx.curve, calendar=calendar, cfg=cfg)
    krd = {float(tenor): float(value) for tenor, value in risk.krd.items()}
    return TargetKRD(as_of=ctx.as_of, krd=krd, notes="零动作：保持当前 KRD 不变")


class _ScriptedStrategy:
    """按 signal_date 查表输出目标的策略（环境内部使用；不对外承诺 API）。

    ``lagged_view`` 非空时，策略实际收到的是 R51 的滞后 ``MarketContext``
    （``curve/history/benchmark`` 截止 ``available_through``）；schedule 仍按决策日
    ``ctx.as_of`` 查表，因此信号日/成交日语义不变。
    """

    def __init__(
        self,
        name: str,
        schedule: dict[date, ActionSpec],
        *,
        calendar: TradingCalendar,
        cfg: Config,
        lagged_view: _LaggedView | None = None,
    ) -> None:
        self.name = name
        self._schedule = schedule
        self._calendar = calendar
        self._cfg = cfg
        self._lagged_view = lagged_view
        self.emitted: dict[date, TargetKRD | TargetFaces | None] = {}

    def target_krd(self, ctx: MarketContext) -> TargetKRD | TargetFaces | None:
        if self._lagged_view is not None:
            ctx = self._lagged_view.context(ctx)
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
    information_lag_days : int, default 1
        信息滞后（交易日数，R51）。观测/策略上下文中的 ``curve/history/benchmark``
        只到 ``t − information_lag_days``；持仓估值/NAV 也截到该日（R59），执行仍用
        当日价格。``0`` = 旧行为（策略可见当日曲线），仅用于对照/回归测试。
    warmup_days : int, default 0
        显式预热要求（R57）。实际要求为 ``max(lag, warmup_days)`` 且至少 ``lag+1``
        个交易日（含信息截止日本身），不足时 ``reset/rollout`` 直接抛
        :class:`ValueError`，不会把截止日夹到未来。
    decision_step_days : int, default 1
        决策网格步长（R51）。episode 起始交易日为索引 0，只有
        ``索引 % decision_step_days == 0`` 的交易日允许策略给出目标；其余日强制
        hold 且记录 ``off_grid_hold``。``1`` = 每个交易日都可决策（旧行为）。
    """

    def __init__(
        self,
        panel: pd.DataFrame,
        calendar: TradingCalendar,
        funding: pd.Series | None = None,
        cfg: Config | None = None,
        *,
        force_daily_rebalance: bool = True,
        information_lag_days: int = 1,
        warmup_days: int = 0,
        decision_step_days: int = 1,
    ) -> None:
        if not isinstance(panel, pd.DataFrame) or panel.empty:
            raise ValueError("panel 必须是非空 DataFrame")
        if not isinstance(calendar, TradingCalendar):
            raise TypeError("calendar 必须是 TradingCalendar")
        if funding is not None and not isinstance(funding, pd.Series):
            raise TypeError("funding 必须是 pandas.Series 或 None")
        if isinstance(information_lag_days, bool) or not isinstance(information_lag_days, int):
            raise TypeError("information_lag_days 必须是整数")
        if information_lag_days < 0:
            raise ValueError("information_lag_days 不能为负")
        if isinstance(warmup_days, bool) or not isinstance(warmup_days, int):
            raise TypeError("warmup_days 必须是整数")
        if warmup_days < 0:
            raise ValueError("warmup_days 不能为负")
        if isinstance(decision_step_days, bool) or not isinstance(decision_step_days, int):
            raise TypeError("decision_step_days 必须是整数")
        if decision_step_days < 1:
            raise ValueError("decision_step_days 必须 >= 1")

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
        self._information_lag_days = information_lag_days
        self._warmup_days = warmup_days
        # 截止日本身必须已有历史（lag>0 时至少回退 lag+1 个交易日），避免把
        # available_through 落在 panel 首日而“看似”有滞后。
        self._required_warmup = max(
            max(information_lag_days, warmup_days),
            information_lag_days + 1 if information_lag_days > 0 else 0,
        )
        self._decision_step_days = decision_step_days
        self._lagged_view = _LaggedView(
            panel=self._panel,
            tenors=self._tenors,
            calendar=self._calendar,
            cfg=self._cfg,
            information_lag_days=information_lag_days,
        )
        self._day_index: dict[date, int] = {}

        self._start: date | None = None
        self._end: date | None = None
        self._days: tuple[date, ...] = ()
        self._cursor: date | None = None
        self._strategy_name = "rl_env"
        self._schedule: dict[date, ActionSpec] = {}
        self._records: list[StepRecord] = []
        self._result: BacktestResult | None = None
        self._terminal: TerminalState | None = None
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
    def information_lag_days(self) -> int:
        """信息滞后交易日数（R51；默认 1）。"""
        return self._information_lag_days

    @property
    def warmup_days(self) -> int:
        """显式预热天数参数（R57；实际要求见 :attr:`episode_required_warmup`）。"""
        return self._warmup_days

    @property
    def episode_required_warmup(self) -> int:
        """episode 起点之前至少需要的交易日数（R57；不足时 reset/rollout 抛错）。"""
        return self._required_warmup

    @property
    def decision_step_days(self) -> int:
        """决策网格步长（R51；默认 1 = 每日可决策）。"""
        return self._decision_step_days

    def available_through(self, day: date) -> date:
        """``day`` 决策可见的信息截止日（``day − lag`` 个交易日；越界抛错）。"""
        return self._lagged_view.available_through(day)[0]

    def is_decision_day(self, day: date) -> bool:
        """``day`` 是否落在 episode 决策网格上（以 reset/rollout 起点为索引 0）。"""
        if self._decision_step_days <= 1:
            return True
        index = self._day_index.get(day)
        if index is not None:
            return index % self._decision_step_days == 0
        if self._start is None:
            return True
        offset = len(self._calendar.trading_days_between(self._start, day)) - 1
        return offset >= 0 and offset % self._decision_step_days == 0

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
    def _validate_episode_window(self, start: date, end: date) -> tuple[date, ...]:
        """校验 episode 窗口与预热（R57）：不足时拒绝，绝不把截止日移到未来。"""
        days = self._calendar.trading_days_between(start, end)
        if len(days) < 2:
            raise ValueError(f"episode [{start}, {end}] 内交易日不足 2 天")
        first = days[0]
        if pd.Timestamp(first) not in self._panel.index:
            raise ValueError(f"起始日 {first} 不在 panel 内")
        prior = sum(1 for timestamp in self._panel.index if _to_date(timestamp) < first)
        required = self._required_warmup
        if prior < required:
            raise ValueError(
                f"episode 起点 {first} 之前只有 {prior} 个交易日历史，"
                f"需要至少 {required} 个交易日预热"
                f"（information_lag_days={self._information_lag_days}, "
                f"warmup_days={self._warmup_days}, "
                f"episode_required_warmup={required}）；"
                "请提供预热数据或推迟首个决策日"
            )
        # 预热足够后仍需确认截止日落在 panel 内（防止日历与报价日缺口）。
        self._lagged_view.available_through(first)
        return days

    def reset(self, start: date, end: date, strategy_name: str = "rl_env") -> Observation:
        """开始一个新 episode，返回**首日 as-of 观测**（曲线/基准 KRD/持仓/现金/NAV）。

        R57：``start`` 之前没有足够预热交易日时抛 :class:`ValueError`，不用面板
        首日冒充截止日；基准身份在 ``start`` 构造一次（R58）。
        """
        start = _coerce_date(start, name="start")
        end = _coerce_date(end, name="end")
        if not isinstance(strategy_name, str) or not strategy_name:
            raise ValueError("strategy_name 必须是非空字符串")
        days = self._validate_episode_window(start, end)

        self._start = days[0]
        self._end = days[-1]
        self._days = tuple(days)
        self._day_index = {day: index for index, day in enumerate(days)}
        self._cursor = days[0]
        self._strategy_name = strategy_name
        self._schedule = {}
        self._records = []
        self._result = None
        self._terminal = None
        self._events = ()
        self._last_nav = float(self._cfg.backtest.init_cash)
        self._replay_days = 0
        self._replay_cost_seconds = 0.0
        self._lagged_view.begin_episode(days[0], days[-1])
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

        R51：``decision_step_days > 1`` 且当前信号日不在网格上时，传入的动作被强制
        替换为 hold（不进入 schedule、不产生主动订单），记录 ``notes`` 含
        ``off_grid_hold``；与 :meth:`rollout` 的网格判定完全一致。
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

        off_grid = not self.is_decision_day(signal_date)
        if off_grid:
            # 非决策日：忽略调用方动作，强制 hold；不进入 schedule → 引擎无 pending。
            action = ActionSpec(notes=_OFF_GRID_NOTE)

        self._schedule[signal_date] = action
        strategy = _ScriptedStrategy(
            self._strategy_name,
            self._schedule,
            calendar=self._calendar,
            cfg=self._cfg,
            lagged_view=self._lagged_view,
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

        record = self._build_step_record(
            result=result,
            day=trade_date,
            action=action,
            nav_before=self._last_nav,
            nav_after=replay_nav,
            previous_faces=previous_faces,
            emitted=strategy.emitted.get(signal_date),
            replay_days=step_replay_days,
            replay_cost_seconds=replay_seconds,
            off_grid=off_grid,
        )

        self._lagged_view.sync_from_result(result)
        self._records.append(record)
        self._cursor = trade_date
        self._result = result
        self._terminal = None  # 新的 step 结果使上一终止快照失效
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
        原生 ``TargetKRD`` / ``TargetFaces`` 会**无损规范化**为 :class:`ActionSpec` 并记入
        ``original_action``（不再退化成 hold）。与逐步 ``step`` 使用同一引擎/闸门/滚动/
        成本，并共用 :meth:`_build_step_record` 构造记录，故逐字段等价（仅性能字段不同）。

        完成后同步环境生命周期（``_result/_cursor/_start/_end/_records``），因此随后
        ``terminate()`` 可调用并返回与 ``Rollout.terminal`` 相同的 :class:`TerminalState`。
        交互式调试仍可用 ``step``（O(n²)）；rollout 始终只运行一次引擎。

        限制：引擎在风控熔断日**不调用策略**（直接以基准中性目标替代），rollout 因此
        无法观测被替代的原始动作，该日 ``original_action`` 回落为 hold；要逐字段对齐
        该类日期，需要引擎暴露被替代动作或在熔断日仍调用策略（与逐档 candidate 一并）。

        R51/R59：policy 收到的是滞后 ``MarketContext``（``curve/history/benchmark``
        截止 ``available_through``；``nav/cash`` 取该日账本状态；``positions`` 只有
        数量/条款、估值 NaN）；非决策网格日不调用 policy，直接返回 None 并记录
        ``off_grid_hold``。网格以本次 ``start`` 交易日为索引 0，与 :meth:`step`
        （reset 起点）同一规则。
        """
        start = _coerce_date(start, name="start")
        end = _coerce_date(end, name="end")
        days = self._validate_episode_window(start, end)
        self._lagged_view.begin_episode(days[0], days[-1])
        actions: dict[date, ActionSpec] = {}
        day_index = {day: index for index, day in enumerate(days)}
        grid_days = frozenset(
            days[index] for index in range(0, len(days), self._decision_step_days)
        )

        rollout_name = name
        lagged_view = self._lagged_view

        class _PolicyStrategy:
            @property
            def name(self) -> str:  # engine 读取 strategy.name
                return rollout_name

            def target_krd(self, ctx: MarketContext) -> TargetKRD | TargetFaces | None:
                # R59：无论是否网格日都记录引擎 t 日账本状态，供之后回看 available 日。
                lagged_view.observe(ctx)
                if ctx.as_of not in grid_days:
                    # 非决策日：不调用 policy，强制 hold（无 pending → 无主动订单）。
                    actions[ctx.as_of] = ActionSpec(notes=_OFF_GRID_NOTE)
                    return None
                decision_ctx = lagged_view.context(ctx)
                action = _normalise_action(policy(decision_ctx))
                actions[ctx.as_of] = action
                if action.is_hold:
                    return None
                if action.action_type == "target_faces":
                    return TargetFaces(
                        as_of=ctx.as_of,
                        faces=dict(action.target_faces or {}),
                        notes=action.notes or "RL 环境动作（目标面值）",
                    )
                return TargetKRD(
                    as_of=ctx.as_of,
                    krd=dict(action.target_krd or {}),
                    notes=action.notes or "RL 环境动作",
                )

        strategy = _PolicyStrategy()
        replay_start = time.perf_counter()
        result = run_backtest(
            strategy,
            panel=self._panel,
            calendar=self._calendar,
            funding=self._funding,
            cfg=self._cfg,
            start=start,
            end=end,
        )
        replay_seconds = time.perf_counter() - replay_start
        self._lagged_view.sync_from_result(result)
        decisions = {item["date"]: item for item in getattr(result, "gate_decisions", [])}
        records: list[StepRecord] = []
        for index, day in enumerate(days[1:], start=1):
            decision = decisions.get(day)
            signal_date = decision.get("signal_date") if decision is not None else None
            prior_day = days[index - 1]
            if decision is not None and signal_date is not None:
                decision_day = _to_date(signal_date)
            else:
                # 滚动政策目标以 pending.as_of 为信号日；无 pending 时引擎回落到前一交易日。
                decision_day = prior_day
            action = actions.get(decision_day)
            if action is None:
                action = ActionSpec()
            off_grid = day_index.get(decision_day, 0) % self._decision_step_days != 0
            previous_faces = self._positions_in(result, decision_day)
            # R47：reward 一律取相邻估值日 NAV 差，与订单 signal_date 无关。
            records.append(
                self._build_step_record(
                    result=result,
                    day=day,
                    action=action,
                    nav_before=float(result.nav.loc[prior_day]),
                    nav_after=float(result.nav.loc[day]),
                    previous_faces=previous_faces,
                    decision=decision,
                    replay_days=len(days),
                    replay_cost_seconds=0.0,
                    off_grid=off_grid,
                )
            )
        terminal = self._terminal_state(result, days[-1])
        # R49：同步环境生命周期，使 rollout 后可直接 terminate()/读取观测。
        self._start = days[0]
        self._end = days[-1]
        self._days = tuple(days)
        self._day_index = dict(day_index)
        self._cursor = days[-1]
        self._strategy_name = name
        self._schedule = dict(actions)
        self._records = list(records)
        self._result = result
        self._terminal = terminal
        self._events = _build_events(result)
        self._last_nav = float(result.nav.loc[days[-1]])
        self._replay_days = len(days)
        self._replay_cost_seconds = replay_seconds
        return Rollout(records=tuple(records), result=result, terminal=terminal)

    def terminate(self) -> TerminalState:
        """返回 episode 终止时的估值与未结算义务（**不强制平仓**）。

        ``reset/step`` 与 ``rollout`` 完成后都可调用；``rollout`` 已同步环境生命周期，
        因此新 env 直接 rollout 后无需再 step。返回的 :class:`TerminalState` 与
        ``Rollout.terminal`` 相同（同一估值/义务口径）。NAV 与 ``result.nav`` 末值
        逐位一致；reward 只取账本 NAV 的逐日变化（含市价重估），不通过"强制平仓"
        制造期末收益；未结算成交/现金/费用单独披露，价差不重复扣。
        """
        if self._end is None or self._result is None:
            raise RuntimeError("必须先 reset/step/rollout 后再 terminate")
        if not self.done:
            raise RuntimeError(
                f"episode 尚未结束（cursor={self._cursor}, end={self._end}），不能 terminate"
            )
        if self._terminal is None:
            self._terminal = self._terminal_state(self._result, self._end)
        return self._terminal

    def _terminal_state(self, result: BacktestResult, end: date) -> TerminalState:
        """由 ``BacktestResult`` 构造终止义务快照（step/rollout 共用同一口径）。"""
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
            "不追加终止变现收益/成本；半价差已在成交日按账本扣一次，终止时不重复扣",
            (
                f"未结算成交 {len(unsettled)} 笔，待结算现金 {unsettled_cash:,.2f} 元"
                "（买入为负、卖出为正；已含在期末 NAV 的 pending_cash 内）"
            ),
            (
                f"已发生未支付费用 {accrued_fees:,.2f} 元按 cfg.costs.fee_bp + 每笔结算费 + "
                "TradeRecord.fee 估算；引擎在结算日才扣费，故未计入期末 NAV；"
                f"净终值 = NAV − 未支付费用 = {nav - accrued_fees:,.2f} 元"
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

    def _build_step_record(
        self,
        *,
        result: BacktestResult,
        day: date,
        action: ActionSpec,
        nav_before: float,
        nav_after: float,
        previous_faces: Mapping[str, float],
        emitted: TargetKRD | TargetFaces | None = None,
        decision: dict[str, Any] | None = None,
        replay_days: int = 0,
        replay_cost_seconds: float = 0.0,
        off_grid: bool = False,
    ) -> StepRecord:
        """共用轨迹构造器（R47/R48/R51/R53/R55）：decision + trades + rolls + nav → record。

        ``step``（逐日重放）与 ``rollout``（单次回放）**都调用本函数**，因此 record 的
        语义逐项相同；唯一允许不同的是 ``replay_days/replay_cost_seconds``（性能披露）
        以及成交 ``trade_id``（含策略名）。

        - R51：``off_grid`` 为真时在 ``notes`` 追加 ``off_grid_hold``，且调用方已把
          动作替换为 hold；
        - R53：KRD 动作同时比较**逐桶**投影偏差，形状改变即使总 DV01 不变也记 ``scaled``；
        - R55：``requested_faces``（原请求）与 ``projected_faces``（最终接受订单的带符号
          面值）/``accepted_orders``/``post_trade_faces``（期末持仓）分开记录。
        """
        decision = self._gate_decision(result, day) if decision is None else decision
        constraints = result.constraints
        constraint_row = constraints.loc[_dates_of(constraints["date"]) == day]
        rejected_frame = result.rejected
        rejected_on = (
            rejected_frame.loc[_dates_of(rejected_frame["date"]) == day]
            if len(rejected_frame)
            else rejected_frame
        )
        if decision is not None:
            pending_status = str(decision.get("pending_status", "none"))
            gate_attempts = int(decision.get("gate_attempts", 0))
            policy_source = str(decision.get("source", "none"))
            risk_off = bool(decision.get("risk_off", False))
            gate_level = float(decision.get("gate_level", 1.0))
            gate_rejected = bool(decision.get("rejected", False))
            gate_reject_reason = str(decision.get("reject_reason", "") or "")
        else:  # 兼容旧结果（无决策日志）
            pending_status = "none"
            gate_attempts = 0
            policy_source = "none"
            risk_off = bool(constraint_row["risk_off"].iloc[0]) if len(constraint_row) else False
            gate_level = float(constraint_row["gate_level"].iloc[0]) if len(constraint_row) else 1.0
            gate_rejected = len(rejected_on) > 0
            gate_reject_reason = str(rejected_on["reason"].iloc[0]) if gate_rejected else ""

        trades = result.trades
        trades_on = trades.loc[_dates_of(trades["trade_date"]) == day] if len(trades) else trades
        executed_trades = tuple(_trade_dict(item) for _, item in trades_on.iterrows())
        executed_face = float(sum(item["face_value"] for item in executed_trades))
        roll_on_day = self._roll_rows(result, day)
        policy_actions = self._policy_actions(result, day)

        # 政策替代：滚动日机械换券优先（旧 pending 被取消）或风控熔断强制目标。
        cancelled_by_policy = bool(
            (not action.is_hold and policy_source == "policy_roll") or risk_off
        )
        projected_target, requested_faces, projected_faces, accepted_orders = self._action_views(
            action,
            decision=decision,
            cancelled_by_policy=cancelled_by_policy,
            emitted=emitted,
        )
        krd_shape = self._krd_shape_deviation(
            result=result,
            day=day,
            action=action,
            projected_target=projected_target,
        )
        rejected = bool(gate_rejected and not cancelled_by_policy and not action.is_hold)
        reject_reason = gate_reject_reason if rejected else ""
        action_scale = (
            1.0
            if cancelled_by_policy
            else self._action_scale(
                action,
                projected_target=projected_target,
                projected_faces=projected_faces,
                executed_trades=executed_trades,
                previous_faces=previous_faces,
                krd_shape=krd_shape,
            )
        )
        # 每笔成交同时带上当日闸门尝试次数与动作规模比（审计可独立读取）。
        executed_trades = tuple(
            {**item, "attempted": gate_attempts, "scale": action_scale} for item in executed_trades
        )
        if risk_off and policy_source != "policy_roll":
            action_outcome: ActionOutcome = "replaced_by_risk_off"
        elif cancelled_by_policy:
            action_outcome = "cancelled_by_policy"
        elif rejected:
            action_outcome = "rejected"
        elif action.is_hold:
            action_outcome = "hold"
        elif not executed_trades:
            action_outcome = "no_trade_deadband"
        elif (
            action_scale < 0.999
            or action_scale > 1.001
            or (
                krd_shape is not None
                and krd_shape["max_bucket_deviation"] > krd_shape["bucket_tolerance"]
            )
        ):
            action_outcome = "scaled"
        else:
            action_outcome = "executed"

        notes = self._step_notes(
            action=action,
            action_outcome=action_outcome,
            cancelled_by_policy=cancelled_by_policy,
            policy_source=policy_source,
            decision_pending=pending_status,
            roll_on_day=roll_on_day,
            rejected=rejected,
            reject_reason=reject_reason,
            executed_trades=executed_trades,
            gate_attempts=gate_attempts,
            action_scale=action_scale,
            risk_off=risk_off,
            off_grid=off_grid,
            krd_shape=krd_shape,
        )
        post_trade_faces = tuple(sorted(self._positions_in(result, day).items()))
        substitution_detail = self._substitution_detail(
            action=action,
            action_outcome=action_outcome,
            policy_source=policy_source,
            projected_target=projected_target,
            requested_faces=requested_faces,
            projected_faces=projected_faces,
            accepted_orders=accepted_orders,
            post_trade_faces=post_trade_faces,
            executed_trades=executed_trades,
            previous_faces=previous_faces,
            action_scale=action_scale,
            gate_attempts=gate_attempts,
            reject_reason=reject_reason,
            cancelled_by_policy=cancelled_by_policy,
            krd_shape=krd_shape,
        )
        return StepRecord(
            date=day,
            original_action=action,
            projected_target=projected_target,
            projected_faces=projected_faces,
            executed_trades=executed_trades,
            rejected=rejected,
            reject_reason=reject_reason,
            gate_level=gate_level,
            risk_off=risk_off,
            nav_before=float(nav_before),
            nav_after=float(nav_after),
            reward=float(nav_after) - float(nav_before),
            executed_face=executed_face,
            gate_attempts=gate_attempts,
            policy_source=policy_source,
            pending_status=pending_status,
            notes=notes,
            action_outcome=action_outcome,
            action_scale=action_scale,
            policy_actions=policy_actions,
            substitution_detail=substitution_detail,
            requested_faces=requested_faces,
            accepted_orders=accepted_orders,
            post_trade_faces=post_trade_faces,
            replay_days=replay_days,
            replay_cost_seconds=replay_cost_seconds,
            attempts_detail=tuple(decision.get("attempts_detail") or ()) if decision else (),
        )

    def _positions_at(self, day: date) -> dict[str, float]:
        """上一个已回放结果在 ``day`` 的持仓面值（用于 faces 动作的执行 scale）。"""
        result = self._result
        if result is None:
            return {}
        return self._positions_in(result, day)

    @staticmethod
    def _positions_in(result: BacktestResult, day: date) -> dict[str, float]:
        """``result`` 在 ``day`` 的持仓面值（step 与 rollout 共用）。

        空仓日的快照是占位行（``bond_code=None``、面值 NaN），必须过滤掉，否则两路径
        的 ``previous_faces``/faces scale 会出现差异。
        """
        frame = result.positions
        if frame is None or len(frame) == 0:
            return {}
        rows = frame.loc[_dates_of(frame["as_of"]) == day]
        out: dict[str, float] = {}
        for _, item in rows.iterrows():
            code = item["bond_code"]
            if code is None or (isinstance(code, float) and np.isnan(code)):
                continue
            face = float(item["face_value"])
            if not np.isfinite(face):
                continue
            out[str(code)] = face
        return out

    def _action_views(
        self,
        action: ActionSpec,
        *,
        decision: dict[str, Any] | None,
        cancelled_by_policy: bool,
        emitted: TargetKRD | TargetFaces | None = None,
    ) -> tuple[
        TargetKRD | None,
        tuple[tuple[str, float], ...],
        tuple[tuple[str, float], ...],
        tuple[dict[str, Any], ...],
    ]:
        """返回 ``(projected_target, requested_faces, projected_faces, accepted_orders)``。

        R55：``requested_faces`` 只来自原动作（``target_faces`` 的目标持仓面值）；
        ``projected_faces`` 是**最终接受订单的带符号面值**（``+buy/−sell``），来自引擎
        ``gate_decisions.projected_faces``，不再用原请求冒充。拒单/无成交为空；滚动政策
        日保留机械换券订单披露（与 ``policy_actions`` 一致），由 substitution_detail 标注
        ``projected_faces_source="policy_roll"``，不混入原动作。

        ``projected_target`` 只表示原动作经闸门后的 KRD 目标：``target_faces`` 动作绕过
        KRD 求解，恒为 ``None``（不把引擎内部为复用闸门而构造的基准 KRD 当成用户目标）；
        滚动日/风控替代日也为 ``None``。
        """
        source = str(decision.get("source", "none")) if decision is not None else "none"
        accepted_orders = tuple(
            dict(item) for item in ((decision.get("accepted_orders") if decision else None) or ())
        )
        decision_faces = tuple(
            (str(code), float(face))
            for code, face in ((decision.get("projected_faces") if decision else None) or ())
        )
        requested_faces = (
            tuple((str(code), float(face)) for code, face in (action.target_faces or {}).items())
            if action.action_type == "target_faces"
            else ()
        )
        if cancelled_by_policy and source != "policy_roll":
            # 风控熔断替代：当天的成交是引擎的基准中性目标，不属于原动作。
            return None, requested_faces, (), ()
        if source == "policy_roll":
            # 机械换券目标不是原动作的提交目标：只披露政策订单（projected_faces/
            # accepted_orders），绝不再返回 decision["projected_target"]（政策目标本身）。
            return None, requested_faces, decision_faces, accepted_orders
        if action.is_hold:
            return None, (), (), ()
        if action.action_type == "target_faces":
            return None, requested_faces, decision_faces, accepted_orders
        if decision is not None:
            raw = decision.get("projected_target")
            return (
                raw if isinstance(raw, TargetKRD) else None,
                requested_faces,
                decision_faces,
                accepted_orders,
            )
        if isinstance(emitted, TargetKRD):
            return emitted, requested_faces, (), ()
        return None, requested_faces, (), ()

    def _krd_shape_deviation(
        self,
        *,
        result: BacktestResult,
        day: date,
        action: ActionSpec,
        projected_target: TargetKRD | None,
    ) -> dict[str, Any] | None:
        """KRD 动作的总量/逐桶偏差（R53）；非 KRD 动作返回 None。

        原动作缺失的 tenor 仍按引擎语义取**当日基准**（不是 0），否则部分键动作会被
        误判为巨大形状偏离。``max_bucket_deviation = max_t |proj[t] − orig[t]|``
        （t 为原始/投影键并集）；``bucket_tolerance = 1e-6 × max(1, Σ|orig[t]|)``。
        即使总 DV01 比例 = 1，只要单桶偏差超容差就必须记 ``scaled``。
        """
        if action.action_type != "target_krd" or projected_target is None:
            return None
        requested = {float(k): float(v) for k, v in (action.target_krd or {}).items()}
        projected = {float(k): float(v) for k, v in projected_target.krd.items()}
        benchmark = _bench_krd_from_result(result, day)
        tenors = sorted(set(requested) | set(projected))
        if not tenors:
            return None
        effective = {
            tenor: requested.get(tenor, float(benchmark.get(tenor, 0.0))) for tenor in tenors
        }
        deviations = {
            tenor: float(projected.get(tenor, 0.0)) - float(effective[tenor]) for tenor in tenors
        }
        max_deviation = max(abs(value) for value in deviations.values())
        magnitude = float(sum(abs(value) for value in effective.values()))
        tolerance = 1e-6 * max(1.0, magnitude)
        return {
            "requested_krd": requested,
            "effective_requested_krd": effective,
            "projected_krd": projected,
            "bucket_deviation": deviations,
            "max_bucket_deviation": float(max_deviation),
            "bucket_tolerance": float(tolerance),
            "requested_magnitude": magnitude,
        }

    def _action_scale(
        self,
        action: ActionSpec,
        *,
        projected_target: TargetKRD | None,
        projected_faces: tuple[tuple[str, float], ...],
        executed_trades: tuple[dict[str, Any], ...],
        previous_faces: Mapping[str, float],
        krd_shape: Mapping[str, Any] | None = None,
    ) -> float:
        """提交/执行规模相对原始动作的比例（>1 表示向基准回撤时被放大）。

        KRD（R53）：先算总 DV01 比例 ``total_ratio = proj.total_dv01 / orig.total``；
        若逐桶形状有超容差改动（``max_bucket_deviation > bucket_tolerance``），返回
        ``min(total_ratio, 1 − max_bucket_deviation / max(1, Σ|orig|))``。选择该式而非
        "保留 total_ratio 只改 outcome"，是因为缩放率本身也应反映形状替代程度（与
        faces 分支"实际/请求"的保守口径一致），且数学上保证 < 1，不会与
        ``action_outcome="executed"`` 矛盾。总 DV01 不变时该值约为 1 − 形状替代率。

        faces：机械闸门只缩放买入腿，用实际成交买入面值 / 请求买入面值（闸门取整后
        提交，成交即提交值）。
        """
        if action.is_hold:
            return 1.0
        if action.action_type == "target_krd":
            original = float(sum((action.target_krd or {}).values()))
            if projected_target is None or abs(original) <= 1e-12:
                return 1.0
            total_ratio = float(projected_target.total_dv01) / original
            if krd_shape is not None:
                max_deviation = float(krd_shape["max_bucket_deviation"])
                tolerance = float(krd_shape["bucket_tolerance"])
                if max_deviation > tolerance:
                    magnitude = max(1.0, float(krd_shape["requested_magnitude"]))
                    shape_cap = max(0.0, 1.0 - max_deviation / magnitude)
                    return min(total_ratio, shape_cap)
            return total_ratio
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
        off_grid: bool = False,
        krd_shape: Mapping[str, Any] | None = None,
    ) -> str:
        parts: list[str] = []
        if off_grid:
            parts.append(
                f"{_OFF_GRID_NOTE}：非决策日（决策步长={self._decision_step_days}）"
                "强制 hold，忽略调用方动作；无主动订单"
            )
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
                if krd_shape is not None and float(krd_shape["max_bucket_deviation"]) > float(
                    krd_shape["bucket_tolerance"]
                ):
                    parts.append(
                        "KRD 逐桶形状改变：max_bucket_deviation="
                        f"{float(krd_shape['max_bucket_deviation']):,.2f} 元/bp"
                        f"（容差 {float(krd_shape['bucket_tolerance']):.6g}）"
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
        requested_faces: tuple[tuple[str, float], ...],
        projected_faces: tuple[tuple[str, float], ...],
        accepted_orders: tuple[dict[str, Any], ...],
        post_trade_faces: tuple[tuple[str, float], ...],
        executed_trades: tuple[dict[str, Any], ...],
        previous_faces: Mapping[str, float],
        action_scale: float,
        gate_attempts: int,
        reject_reason: str,
        cancelled_by_policy: bool,
        krd_shape: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        """原始请求 vs 接受订单 vs 交易后期末持仓（face/dv01/逐桶差，R53/R55）。

        三阶段面值不再混用：``requested_faces`` 是原动作的目标持仓面值；
        ``projected_faces`` 是最终接受订单的带符号面值（``+buy/−sell``）；
        ``post_trade_faces`` 是账本期末多头面值。``projected_faces_source`` 标明
        “接受订单”还是“滚动政策披露”，避免把政策换券当成原动作提交值。
        """
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
        requested = {str(code): float(face) for code, face in requested_faces}
        accepted_faces = {str(code): float(face) for code, face in projected_faces}
        post_trade = {str(code): float(face) for code, face in post_trade_faces}
        original_total = action.target_total
        projected_total: float | None
        if action.action_type == "target_krd":
            projected_total = (
                float(projected_target.total_dv01) if projected_target is not None else None
            )
        elif accepted_faces:
            projected_total = float(sum(abs(face) for face in accepted_faces.values()))
        elif requested:
            projected_total = float(sum(abs(face) for face in requested.values()))
        else:
            projected_total = None
        executed_gross = float(sum(abs(item["face_value"]) for item in executed_trades))
        requested_delta_faces: dict[str, float] | None = None
        if original_faces is not None:
            requested_delta_faces = {
                code: float(target) - float(previous_faces.get(code, 0.0))
                for code, target in original_faces.items()
            }
        face_diff: float | None = None
        if requested_delta_faces is not None:
            executed_net = float(sum(executed_faces.values()))
            face_diff = executed_net - float(sum(requested_delta_faces.values()))
        dv01_diff = (
            projected_total - original_total
            if projected_total is not None and action.action_type == "target_krd"
            else None
        )
        total_ratio = (
            projected_total / original_total
            if projected_total is not None
            and action.action_type == "target_krd"
            and abs(original_total) > 1e-12
            else None
        )
        detail: dict[str, Any] = {
            "action_type": action.action_type,
            "action_outcome": action_outcome,
            "policy_source": policy_source,
            "cancelled_by_policy": cancelled_by_policy,
            "original_total": original_total,
            "original_faces": original_faces,
            "projected_total": projected_total,
            "requested_faces": requested,
            "projected_faces": accepted_faces,
            "projected_faces_source": (
                "policy_roll"
                if policy_source == "policy_roll"
                else ("accepted_orders" if accepted_faces else "none")
            ),
            "accepted_orders": accepted_orders,
            "post_trade_faces": post_trade,
            "requested_delta_faces": requested_delta_faces,
            "executed_faces": executed_faces,
            "executed_gross_face": executed_gross,
            "face_diff": face_diff,
            "dv01_diff": dv01_diff,
            "total_dv01_ratio": total_ratio,
            "action_scale": action_scale,
            "gate_attempts": gate_attempts,
            "executed_trade_ids": tuple(str(item["trade_id"]) for item in executed_trades),
            "reject_reason": reject_reason,
            "previous_faces": dict(previous_faces),
            # R53：逐桶形状偏差（非 KRD 动作为 None）
            "max_bucket_deviation": (
                float(krd_shape["max_bucket_deviation"]) if krd_shape is not None else None
            ),
            "bucket_tolerance": (
                float(krd_shape["bucket_tolerance"]) if krd_shape is not None else None
            ),
            "requested_krd": dict(krd_shape["requested_krd"]) if krd_shape is not None else None,
            "projected_krd": dict(krd_shape["projected_krd"]) if krd_shape is not None else None,
            "krd_bucket_deviation": (
                dict(krd_shape["bucket_deviation"]) if krd_shape is not None else None
            ),
        }
        return detail

    def _roll_rows(self, result: BacktestResult, day: date) -> pd.DataFrame:
        rolls = result.roll_events
        if len(rolls) == 0:
            return rolls
        return rolls.loc[pd.to_datetime(rolls["date"]).dt.date == day]

    def _benchmark_krd(self, day: date) -> dict[float, float]:
        benchmark = self._lagged_view.benchmark(day)
        return {float(tenor): float(value) for tenor, value in benchmark.krd.items()}

    def _available_ledger_state(
        self, result: BacktestResult, available: date
    ) -> tuple[float, float, float]:
        """``available`` 日的 ``(nav, cash, pending_cash)``；早于 episode 起点用初始现金。"""
        init_cash = float(self._cfg.backtest.init_cash)
        if self._start is not None and available < self._start:
            return init_cash, init_cash, float("nan")
        nav_series = result.nav
        nav = float(nav_series.loc[available]) if available in nav_series.index else float("nan")
        exposure = result.exposure
        row = exposure.loc[_dates_of(exposure["date"]) == available]
        if len(row) == 0:
            return nav, float("nan"), float("nan")
        return (
            nav,
            float(row["cash"].iloc[0]),
            float(row["pending_cash"].iloc[0]) if "pending_cash" in row else float("nan"),
        )

    def _observation(self, day: date) -> Observation:
        """``as_of=day`` 的观测：市场信息/持仓估值/NAV 截到 ``available_through``。

        R57：截止日不向前夹紧（预热不足时由 reset/rollout 提前抛错）。
        R58：``benchmark_krd`` 来自 episode 起点固定身份的真实基准在 ``available``
        日的估值。
        R59：持仓只保留数量/条款（估值列 NaN）；``nav/cash`` 取 ``available`` 日
        账本状态，metadata 显式记录 ``valuation_available=False`` / ``nav_as_of``。
        """
        available, _ = self._lagged_view.available_through(day)
        curve = _curve_at(self._panel, self._tenors, available, allow_missing=True)
        history = self._panel.loc[: pd.Timestamp(available)]
        if history.index.max() > pd.Timestamp(available):
            raise AssertionError(f"as-of 观测包含 {history.index.max()} 之后的未来数据")

        if self._result is None:
            positions = _empty_frame()
            cash = float(self._cfg.backtest.init_cash)
            nav = float(self._cfg.backtest.init_cash)
            pending_cash = float("nan")
        else:
            result = self._result
            nav, cash, pending_cash = self._available_ledger_state(result, available)
            positions = result.positions
            positions = (
                positions.loc[_dates_of(positions["as_of"]) == day].copy()
                if len(positions)
                else positions.copy()
            )
        positions = _sanitize_positions_frame(
            positions, nav=nav, cash=cash, pending_cash=pending_cash
        )
        benchmark_krd = self._benchmark_krd(available)

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
                "as_of": day.isoformat(),
                "available_through": available.isoformat(),
                "information_lag_days": self._information_lag_days,
                "warmup_days": self._warmup_days,
                "episode_required_warmup": self._required_warmup,
                "decision_step_days": self._decision_step_days,
                "lag_clamped": False,  # R57：不再 clamp；保留字段供旧审计读取
                "valuation_available": False,
                "nav_as_of": available.isoformat(),
                "positions_quantities_as_of": day.isoformat(),
                "curve_partial": len(curve.tenors) < len(self._tenors),
                "curve_tenors": tuple(float(t) for t in curve.tenors),
                "decision_index": self._day_index.get(day),
                "on_decision_grid": self.is_decision_day(day),
                "asof_note": (
                    "观测只含 ≤ available_through 的曲线/历史/基准；持仓数量为 as_of 已知，"
                    "估值字段置 NaN；NAV/现金取 available_through 账本状态；"
                    "执行/估值/闸门仍用当日价格（引擎内部）"
                ),
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
