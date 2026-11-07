"""固定持仓归因（Phase 2 第 5 步）。

对外只暴露一个入口 :func:`attribute_fixed_holdings` 与结果对象
:class:`AttributionResult`：输入账本四件套（trades / cashflows / snapshots /
nav）与曲线面板，输出精确桥接 + 逐券 carry/roll + 曲线因子
（level/slope/curvature/other）的逐日账本。口径见
:mod:`ficc.attribution.ledger_attribution` 模块 docstring。
"""

from __future__ import annotations

from ficc.attribution.ledger_attribution import (
    BONDS_COLUMNS,
    DAILY_COLUMNS,
    FACTORS_COLUMNS,
    AttributionResult,
    attribute_fixed_holdings,
)

__all__ = [
    "BONDS_COLUMNS",
    "DAILY_COLUMNS",
    "FACTORS_COLUMNS",
    "AttributionResult",
    "attribute_fixed_holdings",
]
