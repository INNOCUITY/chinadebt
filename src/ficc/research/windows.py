"""回测样本的窗口切分与逐窗口指标（研究脚本共用）。

窗口设计
--------
- **非重叠窗口**（默认）：从样本首日按 ``window_years`` 年切分，最后一段截断到样本
  末日；交易日在窗口间**不重不漏**地划分（本模块构造后做覆盖校验）。可用
  ``end_cap(start)`` 收缩每窗右端（买持基准下短券 T+1 到期估值越界的场景），
  被裁掉的交易日顺延为下一窗起点。
- **重叠窗口**：``overlap=True`` 时按 ``step_years`` 年滑动、每窗 ``window_years`` 年，
  截止到样本末日；末尾不足 ``min_days`` 个交易日的窗丢弃。
- **重叠窗口不是独立样本**：本模块只负责切分与逐窗描述统计，区间估计由
  :mod:`ficc.research.inference` 给出并须注明"仅描述"。

关于"每窗重置"
--------------
当前买持基准（``benchmark_version=buyhold``）的投资宇宙在回测起点一次性生成，
固定面值持有到期末；若把一条长回测切成多个子区间，后段基准会因短券到期变成
现金、失去可比性。因此本研究的默认做法是**每个窗口独立跑一次引擎**（窗口起点
重建合成券宇宙与基准），不把窗口拼成连续净值——这与计划 §12.12-B
"如暂用多个独立两年窗口，须声明每窗重置"一致。滚动基准就绪后可用连续模式
（``scripts/run_windows.py --mode continuous``），两者在输出中用 ``mode`` 列区分。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import TYPE_CHECKING, Any

import numpy as np
import pandas as pd

if TYPE_CHECKING:  # 仅类型标注，避免研究包反向依赖回测引擎
    from ficc.backtest.engine import BacktestResult

__all__ = [
    "WindowSpec",
    "add_years",
    "build_windows",
    "window_active_returns",
    "window_metrics",
    "window_total_returns",
]


def _as_date(value: Any) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, (pd.Timestamp, np.datetime64)):
        return pd.Timestamp(value).date()
    raise TypeError(f"不是日期类型：{type(value).__name__}")


def add_years(day: date, years: int | float) -> date:
    """加年数（2/29 → 2/28；非整数年按 365.25 天近似，与基准券到期日口径一致）。"""
    years_f = float(years)
    if abs(years_f - round(years_f)) < 1e-12:
        whole = round(years_f)
        try:
            return day.replace(year=day.year + whole)
        except ValueError:
            return day.replace(year=day.year + whole, day=28, month=2)
    return day + timedelta(days=round(365.25 * years_f))


@dataclass(frozen=True)
class WindowSpec:
    """一个回测窗口（交易日集合连续，含首尾）。"""

    label: str
    start: date
    end: date
    days: tuple[date, ...]

    def __post_init__(self) -> None:
        if not self.days:
            raise ValueError(f"窗口 {self.label} 没有交易日")
        if self.start != self.days[0] or self.end != self.days[-1]:
            raise ValueError(f"窗口 {self.label} 的 start/end 与交易日首尾不一致")
        if any(day < self.start or day > self.end for day in self.days):
            raise ValueError(f"窗口 {self.label} 的交易日越界")

    @property
    def n_days(self) -> int:
        return len(self.days)


def _normalise_days(days: Any) -> list[date]:
    values = sorted({_as_date(day) for day in days})
    if len(values) < 2:
        raise ValueError(f"交易日不足 2 天：{values}")
    return values


def build_windows(
    days: Any,
    *,
    window_years: int = 2,
    overlap: bool = False,
    step_years: int = 1,
    min_days: int = 20,
    end_cap: Callable[[date], date] | None = None,
) -> list[WindowSpec]:
    """把连续交易日切分为非重叠或重叠窗口。

    Parameters
    ----------
    days : iterable of date
        回测样本内的全部交易日（会排序去重）。
    window_years : int
        每个窗口的标称年数（默认 2）。
    overlap : bool
        ``False``：非重叠切分，窗口覆盖 ``[首日, 末日]`` 且不重不漏；
        ``True``：按 ``step_years`` 滑动，允许重叠，末尾不足 ``min_days`` 的窗丢弃。
    step_years : int
        重叠窗口的滑动步长（年），仅 ``overlap=True`` 时使用。
    min_days : int
        重叠窗口的最少交易日数（过滤样本末尾过短窗口）。
    end_cap : callable | None
        可选``cap = end_cap(window_start)``：窗口右端不得超过 ``cap``（买持基准下
        短券到期日 T+1 估值会越界，需按每窗起点收缩右端）。非重叠模式下被裁掉的
        交易日**顺延为下一窗口的起点**，仍保持不重不漏；重叠模式只截断、不移动起点。

    Returns
    -------
    list[WindowSpec]
    """
    if window_years <= 0:
        raise ValueError(f"window_years 必须为正整数，实际 {window_years!r}")
    if step_years <= 0:
        raise ValueError(f"step_years 必须为正整数，实际 {step_years!r}")
    if min_days < 1:
        raise ValueError(f"min_days 必须为正整数，实际 {min_days!r}")

    trading_days = _normalise_days(days)
    first, last = trading_days[0], trading_days[-1]
    windows: list[WindowSpec] = []

    if not overlap:
        cursor = 0
        while cursor < len(trading_days):
            start = trading_days[cursor]
            boundary = add_years(start, window_years)
            stop = cursor
            while stop < len(trading_days) and trading_days[stop] < boundary:
                stop += 1
            window_days = list(trading_days[cursor:stop])
            if end_cap is not None:
                cap = end_cap(start)
                window_days = [day for day in window_days if day <= cap]
            if not window_days:
                raise ValueError(
                    f"窗口起点 {start} 被 end_cap 裁空（cap={end_cap(start) if end_cap else None}）"
                )
            windows.append(
                WindowSpec(
                    label=f"W{len(windows) + 1}",
                    start=window_days[0],
                    end=window_days[-1],
                    days=tuple(window_days),
                )
            )
            cursor += len(window_days)
        coverage = [day for window in windows for day in window.days]
        if coverage != trading_days:
            raise RuntimeError("非重叠窗口切分未做到不重不漏（实现错误）")
        return windows

    index = 0
    while True:
        start = add_years(first, index * step_years)
        if start > last:
            break
        stop = min(add_years(start, window_years), last)
        window_days = [day for day in trading_days if start <= day <= stop]
        if end_cap is not None:
            cap = end_cap(start)
            window_days = [day for day in window_days if day <= cap]
        if len(window_days) >= min_days:
            windows.append(
                WindowSpec(
                    label=f"W{index + 1}",
                    start=window_days[0],
                    end=window_days[-1],
                    days=tuple(window_days),
                )
            )
        index += 1
    return windows


def window_total_returns(result: BacktestResult, window: WindowSpec) -> tuple[float, float, float]:
    """窗口内策略/基准累计收益（小数）与超额（bp）。

    用窗口内逐日收益连乘（窗口首日无前收，故首日收益不计）；样本起点重置的回测
    首日 NAV = 初始现金，因此该口径与"期末/期初 NAV − 1"一致。
    """
    nav = result.nav
    benchmark = result.benchmark_nav
    days = list(window.days)
    strategy_returns = nav.pct_change().reindex(days)
    benchmark_returns = benchmark.pct_change().reindex(days)
    mask = strategy_returns.notna() & benchmark_returns.notna()
    strategy_total = (
        float((1.0 + strategy_returns[mask]).prod() - 1.0) if mask.any() else float("nan")
    )
    benchmark_total = (
        float((1.0 + benchmark_returns[mask]).prod() - 1.0) if mask.any() else float("nan")
    )
    excess_bp = (strategy_total - benchmark_total) * 1e4
    return strategy_total, benchmark_total, excess_bp


def window_active_returns(result: BacktestResult, window: WindowSpec) -> pd.Series:
    """窗口内日频主动收益 ``r_strategy,t − r_benchmark,t``（小数）。"""
    days = list(window.days)
    strategy_returns = result.nav.pct_change().reindex(days)
    benchmark_returns = result.benchmark_nav.pct_change().reindex(days)
    mask = strategy_returns.notna() & benchmark_returns.notna()
    active = strategy_returns[mask] - benchmark_returns[mask]
    active.name = "active_return"
    return active


def _date_column(frame: pd.DataFrame, column: str) -> pd.Series:
    if not isinstance(frame, pd.DataFrame) or column not in frame.columns:
        raise ValueError(f"缺少日期列 {column!r} 或帧非法")
    if pd.api.types.is_datetime64_any_dtype(frame[column]):
        return frame[column].dt.date
    return frame[column].map(_as_date)


def window_metrics(
    result: BacktestResult,
    window: WindowSpec,
    *,
    annual_days: int = 250,
) -> dict[str, Any]:
    """窗口描述统计：收益/超额、成本、换手、成交、暴露、违规。

    暴露指标见 :func:`ficc.research.exposure.active_exposure_metrics`
    （warmup 日剔除：只统计有持仓交易日）。
    """
    from ficc.research.exposure import active_exposure_metrics, tracking_error_bp

    days = list(window.days)
    strategy_total, benchmark_total, excess_bp = window_total_returns(result, window)
    active = window_active_returns(result, window)
    ann_excess_bp = float(active.mean()) * float(annual_days) * 1e4 if len(active) else float("nan")

    costs = result.costs
    cost_dates = _date_column(costs, "date")
    cost_slice = costs.loc[cost_dates.isin(days)]
    turnover_slice = result.turnover.reindex(days).dropna()

    trades = result.trades
    trade_dates = _date_column(trades, "trade_date") if len(trades) else pd.Series(dtype=object)
    rejects = result.rejected
    reject_dates = _date_column(rejects, "date") if len(rejects) else pd.Series(dtype=object)

    constraints = result.constraints
    constraint_dates = _date_column(constraints, "date")
    constraint_slice = constraints.loc[constraint_dates.isin(days)]
    violation_days = (
        int((~constraint_slice["ok"].astype(bool)).sum()) if len(constraint_slice) else 0
    )
    avg_positions = (
        float(pd.to_numeric(constraint_slice["n_positions"], errors="coerce").mean())
        if len(constraint_slice)
        else float("nan")
    )

    exposure = result.exposure
    exposure_dates = _date_column(exposure, "date")
    exposure_slice = exposure.loc[exposure_dates.isin(days)].reset_index(drop=True)
    exposure_metrics = active_exposure_metrics(exposure_slice)

    nav_slice = result.nav.reindex(days)
    metrics: dict[str, Any] = {
        "start": window.start,
        "end": window.end,
        "n_days": int(window.n_days),
        "n_obs": len(active),
        "initial_nav": float(nav_slice.iloc[0]) if len(nav_slice) else float("nan"),
        "final_nav": float(nav_slice.iloc[-1]) if len(nav_slice) else float("nan"),
        "strategy_total_return": strategy_total,
        "benchmark_total_return": benchmark_total,
        "excess_bp": excess_bp,
        "ann_excess_bp": ann_excess_bp,
        "tracking_error_bp": tracking_error_bp(active, annual_days=annual_days),
        "total_fee": float(cost_slice["fee"].sum()) if len(cost_slice) else 0.0,
        "total_funding": float(cost_slice["funding"].sum()) if len(cost_slice) else 0.0,
        "total_spread": float(cost_slice["spread"].sum()) if len(cost_slice) else 0.0,
        "total_cost": float(cost_slice["total"].sum()) if len(cost_slice) else 0.0,
        "total_turnover": float(turnover_slice.sum()) if len(turnover_slice) else 0.0,
        "n_trades": int(trade_dates.isin(days).sum()) if len(trade_dates) else 0,
        "n_rejected": int(reject_dates.isin(days).sum()) if len(reject_dates) else 0,
        "n_violation_days": violation_days,
        "avg_n_positions": avg_positions,
        **exposure_metrics,
    }
    return metrics
