"""RL 接口层（**接口骨架，不做训练/绩效比较**）。

- :class:`ActionSpec`：策略提出的原始动作（目标 KRD 或零动作）；
- :class:`Observation` / :class:`StepRecord` / :class:`Event`：as-of 观测、单步审计记录、
  时间序事件；
- :class:`RebalanceEnv`：逐日重放环境，账本/闸门/滚动/成本**复用** ``run_backtest``；
- :func:`hold_target_krd`：零动作目标（保持当前 KRD）。

研究边界：2019–2025 已被研究；本包不产生任何绩效结论。``RebalanceEnv`` 每次 step 从
episode 起点重跑引擎（O(n²)），是接口原型，性能不作为结论。
"""

from __future__ import annotations

from ficc.rl.env import (
    ActionSpec,
    Event,
    Observation,
    RebalanceEnv,
    StepRecord,
    hold_target_krd,
)

__all__ = [
    "ActionSpec",
    "Event",
    "Observation",
    "RebalanceEnv",
    "StepRecord",
    "hold_target_krd",
]
