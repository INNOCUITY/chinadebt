"""研究辅助包：多窗口切分、区间估计与主动暴露指标（研究脚本专用）。

由 ``scripts/run_windows.py``（多窗口 + HAC/块 bootstrap）与
``scripts/run_lot_exposure.py``（lot=5M 主动暴露解释）共用，避免两套口径。
"""

from __future__ import annotations

from ficc.research.compat import (
    BENCHMARK_VERSIONS,
    BenchmarkResolution,
    resolve_benchmark_version,
)
from ficc.research.exposure import (
    ATTRIBUTION_COMPONENTS,
    DEFAULT_EXPOSURE_MATERIAL,
    DEFAULT_MARKET_SHARE,
    DEFAULT_MIN_ABS_EXCESS_BP,
    EXCESS_SOURCE_NO,
    EXCESS_SOURCE_NONE,
    EXCESS_SOURCE_PARTIAL,
    EXCESS_SOURCE_YES,
    active_exposure_metrics,
    attribution_bp,
    attribution_components,
    classify_excess_source,
    excess_attribution_bp,
    tracking_error_bp,
)
from ficc.research.inference import (
    ANNUAL_DAYS,
    BOOTSTRAP_BLOCK,
    BOOTSTRAP_REPS,
    BOOTSTRAP_SEED,
    HAC_LAG,
    Z_95,
    InferenceSummary,
    annualized_inference,
    block_bootstrap,
    newey_west,
)
from ficc.research.strategies import (
    STRATEGY_NAMES,
    ZeroTiltStrategy,
    build_strategy_factories,
    fit_residual_panel,
)
from ficc.research.windows import (
    WindowSpec,
    add_years,
    build_windows,
    window_active_returns,
    window_metrics,
    window_total_returns,
)

__all__ = [
    "ANNUAL_DAYS",
    "ATTRIBUTION_COMPONENTS",
    "BENCHMARK_VERSIONS",
    "BOOTSTRAP_BLOCK",
    "BOOTSTRAP_REPS",
    "BOOTSTRAP_SEED",
    "DEFAULT_EXPOSURE_MATERIAL",
    "DEFAULT_MARKET_SHARE",
    "DEFAULT_MIN_ABS_EXCESS_BP",
    "EXCESS_SOURCE_NO",
    "EXCESS_SOURCE_NONE",
    "EXCESS_SOURCE_PARTIAL",
    "EXCESS_SOURCE_YES",
    "HAC_LAG",
    "STRATEGY_NAMES",
    "Z_95",
    "BenchmarkResolution",
    "InferenceSummary",
    "WindowSpec",
    "ZeroTiltStrategy",
    "active_exposure_metrics",
    "add_years",
    "annualized_inference",
    "attribution_bp",
    "attribution_components",
    "block_bootstrap",
    "build_strategy_factories",
    "build_windows",
    "classify_excess_source",
    "excess_attribution_bp",
    "fit_residual_panel",
    "newey_west",
    "resolve_benchmark_version",
    "tracking_error_bp",
    "window_active_returns",
    "window_metrics",
    "window_total_returns",
]
