"""RL 接口层（**接口骨架，不做训练/绩效比较**）。

- :class:`ActionSpec`：策略提出的原始动作（hold / target_krd / target_faces）；
- :class:`PolicyAction`：滚动政策的机械换券动作（独立于原动作审计）；
- :class:`TerminalState`：episode 终止时的 NAV/现金/未结算成交/费用义务；
- :class:`Observation` / :class:`StepRecord` / :class:`Event`：as-of 观测、单步审计记录、
  时间序事件；:class:`Rollout` 是单次 ``run_backtest`` 的整段轨迹 + 终止状态；
- :class:`RebalanceEnv`：逐日重放环境，账本/闸门/滚动/成本**复用** ``run_backtest``；
- :func:`hold_target_krd`：零动作目标（保持当前 KRD）。

研究边界：2019–2025 已被研究；本包不产生任何绩效结论。``RebalanceEnv`` 每次 step 从
episode 起点重跑引擎（O(n²)），是接口原型，性能不作为结论；``rollout`` 只运行一次
引擎（O(n)），与 ``step`` 共用同一 ``StepRecord`` 构造器（R47–R49）。环境披露
``replay_days`` / ``replay_cost_seconds``，正式训练需要引擎单步状态转移接口。
"""

from __future__ import annotations

from ficc.rl.env import (
    ActionOutcome,
    ActionSpec,
    ActionType,
    Event,
    Observation,
    PolicyAction,
    RebalanceEnv,
    Rollout,
    StepRecord,
    TerminalState,
    hold_target_krd,
)

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
