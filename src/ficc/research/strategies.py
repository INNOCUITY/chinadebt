"""研究脚本共用的策略工厂与 RV 残差面板（避免两个脚本各写一套构造）。

**每次引擎运行必须新建策略实例**：``SteepenerStrategy`` 等带有持仓方向状态
（``_direction``），复用同一实例会让后一次回测继承上一次的方向、污染对照实验。
因此本模块返回"工厂"而不是策略实例。

RV 残差面板只拟合到回测末日 ``end``：``fit_panel`` 逐日横截面独立拟合，截断未来
数据不会改变 ``≤end`` 的残差，同时更符合"不读未来"的纪律。
"""

from __future__ import annotations

from collections.abc import Callable
from datetime import date
from typing import Any

import pandas as pd

from ficc.config import Config
from ficc.curve.fitting import fit_panel
from ficc.portfolio.types import MarketContext, TargetKRD
from ficc.strategy.carry_rolldown import CarryRollStrategy
from ficc.strategy.curve_steepener import SteepenerStrategy
from ficc.strategy.relative_value import RelativeValueStrategy

__all__ = [
    "STRATEGY_NAMES",
    "ZeroTiltStrategy",
    "build_strategy_factories",
    "fit_residual_panel",
]

#: 默认策略顺序（zero_tilt 为控制组，必须与三策略同口径报告）
STRATEGY_NAMES: tuple[str, ...] = ("zero_tilt", "carry_roll", "2s10s", "rv")


class ZeroTiltStrategy:
    """控制组：每天要求"完全回到基准 KRD"（无观点），用于度量执行/取整基线偏差。

    与 ``scripts/run_strategies.py::ZeroTiltStrategy`` 同一实现（研究包不反向依赖脚本）。
    """

    name = "zero_tilt"

    def target_krd(self, ctx: MarketContext) -> TargetKRD:
        return TargetKRD(as_of=ctx.as_of, krd=dict(ctx.benchmark.krd), notes="控制组：零 tilt")


def fit_residual_panel(panel: pd.DataFrame, cfg: Config, end: date | None = None) -> pd.DataFrame:
    """RV 策略用的 NSS 残差面板（观测 − 拟合），只拟合到 ``end``（含）。"""
    fitting_panel = panel.loc[: pd.Timestamp(end)] if end is not None else panel
    _params, residual_bp = fit_panel(
        fitting_panel,
        method=cfg.curve.fit_method,
        weights=cfg.curve.weights,
        param_mode="free",
        residuals_mode="free",
    )
    residual_bp.columns = [float(column) for column in residual_bp.columns]
    return -residual_bp


def build_strategy_factories(
    names: list[str] | tuple[str, ...],
    panel: pd.DataFrame,
    cfg: Config,
    end: date | None = None,
) -> list[tuple[str, Callable[[], Any]]]:
    """按名称返回 ``(name, factory)`` 列表；工厂每次调用创建新的策略实例。"""
    requested = list(names)
    unknown = [name for name in requested if name not in STRATEGY_NAMES]
    if unknown:
        raise ValueError(f"未知策略 {unknown}；可用：{list(STRATEGY_NAMES)}")
    residuals: pd.DataFrame | None = None
    if "rv" in requested:
        residuals = fit_residual_panel(panel, cfg, end)
    factories: dict[str, Callable[[], Any]] = {
        "zero_tilt": ZeroTiltStrategy,
        "carry_roll": CarryRollStrategy,
        "2s10s": SteepenerStrategy,
        "rv": lambda: RelativeValueStrategy(residuals=residuals),
    }
    return [(name, factories[name]) for name in requested]
