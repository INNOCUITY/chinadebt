"""风险包（Phase 3）：VaR/ES 与压力测试（全重估 vs KRD 近似）。

只消费「持仓 + 曲线」，不消费归因分项；归因（尚未作为风险解释真值）与本包
解耦。口径见 :mod:`ficc.risk.var` 与 :mod:`ficc.risk.stress` 模块 docstring。

对外接口
--------
- :class:`VarResult` / :func:`historical_var_es` / :func:`parametric_var_es`
  （历史模拟、参数正态、Cornish-Fisher；ES 为尾部条件均值）；
- :func:`portfolio_pnl_fixed_revaluation` / :func:`portfolio_pnl_krd_approx`
  （固定组合逐日 PnL：全重估 vs KRD 近似，同窗口对照）；
- :class:`StressResult` / :func:`stress_test`（平行/陡平/蝶式/历史情景）；
- :func:`portfolio_market_value` / :func:`pnl_pct_from_cny` 辅助函数。
"""

from __future__ import annotations

from ficc.risk.stress import StressResult, stress_test
from ficc.risk.var import (
    VarResult,
    historical_var_es,
    parametric_var_es,
    pnl_pct_from_cny,
    portfolio_market_value,
    portfolio_pnl_fixed_revaluation,
    portfolio_pnl_krd_approx,
)

__all__ = [
    "StressResult",
    "VarResult",
    "historical_var_es",
    "parametric_var_es",
    "pnl_pct_from_cny",
    "portfolio_market_value",
    "portfolio_pnl_fixed_revaluation",
    "portfolio_pnl_krd_approx",
    "stress_test",
]
