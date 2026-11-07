"""策略层：只输出目标 KRD，不直接下单（Phase 2 第 4 步）。

- :mod:`ficc.strategy.base`：``Strategy`` 协议、``BaseStrategy`` 与
  ``relative_tilt``（相对基准 KRD 中性、负桶截断重分配）；
- :mod:`ficc.strategy.carry_rolldown`：carry+roll 排名策略；
- :mod:`ficc.strategy.curve_steepener`：2s10s z-score 状态机；
- :mod:`ficc.strategy.relative_value`：NSS 残差 z-score 多桶策略。
"""

from __future__ import annotations

from ficc.strategy.base import BaseStrategy, Strategy, relative_tilt
from ficc.strategy.carry_rolldown import CarryRollStrategy
from ficc.strategy.curve_steepener import SteepenerStrategy
from ficc.strategy.relative_value import RelativeValueStrategy

__all__ = [
    "BaseStrategy",
    "CarryRollStrategy",
    "RelativeValueStrategy",
    "SteepenerStrategy",
    "Strategy",
    "relative_tilt",
]
