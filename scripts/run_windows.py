#!/usr/bin/env python3
"""多窗口 + 区间估计研究（计划 §12.12-B；只做研究，不改引擎/配置）。

研究问题
--------
1. 把历史样本切成多个窗口（默认标称 2 年/窗），逐窗报告策略/基准收益、超额、
   成本分项、换手、成交、主动 DV01/久期偏离、违规与拒单；
2. 对日频主动收益 ``active_t = r_strategy,t − r_benchmark,t`` 做**考虑时间相关性**
   的区间估计：Newey-West HAC（lag=5）t 统计量/95% CI + 固定块 bootstrap
   （block=20、2000 次、固定 seed）年化超额 95% CI 与双侧 p 值。

**预注册统计参数（写死在 ``ficc.research.inference``，不得按结果调整）**：
``HAC_LAG=5``、``BOOTSTRAP_BLOCK=20``、``BOOTSTRAP_REPS=2000``、
``BOOTSTRAP_SEED=20261108``、年化 250 交易日。``--quick`` 只把 bootstrap 次数降到
200 以便测试（输出中记录 ``boot_reps``），其余统计口径不变。
"固定常量"只表示不再按结果调参；若称"预注册"，须另行记录冻结提交/时间（R41）。

**窗口结构与模式（必须一起披露，R41）**
- 实际窗口由 ``n_days`` + ``partial`` 两列披露：默认样本
  2019-01-28→2025-12-31 为 **W1–W3 约两年整窗 + W4 229 日尾窗（partial=True）**，
  不是"四个完整两年窗"。
- ``--mode continuous``（rolling 基准默认）：**一条长回测按日期切窗，净值连续**；
  后窗累计收益包含"前一交易日收盘→该窗首日"的边界收益，因此收益基期是
  **该窗前收 NAV**（首个窗口为样本首日 NAV），CSV 用 ``return_base_date`` /
  ``return_base_nav`` 显式导出；``start_close_nav`` 只是首日收盘参考值。
- ``--mode reset``（buyhold 基准默认）：每窗独立跑一次引擎（窗口起点重建宇宙与
  基准），收益基期 = **该窗重置 NAV**（=首日收盘 NAV）；窗口之间不是连续净值。
  买持基准下还会按最短期限券的 T+1 到期日收缩窗口右端（被裁日顺延下一窗起点）。
- ``--overlap`` 追加重叠窗口：**重叠窗口不是独立样本**，逐窗 CI/p 仅描述；
  pooled 行不拼接重复日期、不做区间推断（R41）。

**pooled 语义（R41）**
- ``pooled_cumulative_excess_bp``：全段累计超额 = 策略日收益复利累计 − 基准日收益
  复利累计（continuous 为真实连续路径；reset 为各独立窗日收益按日拼接的示意）
- ``pooled_ann_excess_bp``：全段日频主动收益均值 × 250（bp，年化）
- ``mean_window_excess_bp``：各窗累计超额（``excess_bp``）的**算术均值**（旧语义，
  保留但明确命名，不再叫 pooled）
- HAC / bootstrap 列统一标注 ``*_ann_bp``（年化口径）；窗口数少时区间只作描述。

产物
----
``reports/windows_summary.csv``   逐窗口描述统计（含 ``n_days``/``partial``/收益基期）；
``reports/windows_inference.csv`` 策略×窗口集合的 HAC/块 bootstrap 区间估计
（逐窗 + ``pooled`` 行；rolling 基准时 ``rolling_version`` 必须非空）。

用法::

    PYTHONPATH=src .venv/bin/python scripts/run_windows.py
    PYTHONPATH=src .venv/bin/python scripts/run_windows.py --quick
    PYTHONPATH=src .venv/bin/python scripts/run_windows.py --overlap --strategies carry_roll,2s10s
    PYTHONPATH=src .venv/bin/python scripts/run_windows.py --benchmark-version rolling
"""

from __future__ import annotations

import argparse
import sys
from datetime import date, timedelta
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
_SCRIPTS = _ROOT / "scripts"
for _path in (_SRC, _SCRIPTS):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from run_strategies import load_market  # noqa: E402

from ficc.backtest.engine import run_backtest  # noqa: E402
from ficc.config import get_config  # noqa: E402
from ficc.research import (  # noqa: E402
    BOOTSTRAP_REPS,
    STRATEGY_NAMES,
    BenchmarkResolution,
    WindowSpec,
    add_years,
    annualized_inference,
    build_strategy_factories,
    build_windows,
    daily_returns,
    resolve_benchmark_version,
    tracking_error_bp,
    window_active_returns,
    window_metrics,
    window_return_basis,
)

DEFAULT_START = date(2019, 1, 28)
DEFAULT_END = date(2025, 12, 31)
QUICK_START = date(2019, 1, 28)
QUICK_END = date(2020, 6, 30)
QUICK_BOOTSTRAP_REPS = 200

WINDOWS_SUMMARY_COLUMNS: tuple[str, ...] = (
    "strategy",
    "window_set",
    "window",
    "mode",
    "benchmark_version",
    "rolling_version",
    "start",
    "end",
    "n_days",
    "partial",
    "n_obs",
    "return_base_date",
    "return_base_nav",
    "start_close_nav",
    "final_nav",
    "strategy_total_return",
    "benchmark_total_return",
    "excess_bp",
    "ann_excess_bp",
    "ann_return",
    "max_drawdown",
    "tracking_error_bp",
    "total_fee",
    "total_funding",
    "total_spread",
    "total_cost",
    "total_turnover",
    "n_trades",
    "n_rejected",
    "n_violation_days",
    "avg_n_positions",
    "avg_dv01_ratio",
    "max_dv01_ratio",
    "avg_abs_dv01_dev",
    "max_abs_dv01_dev",
    "avg_duration",
    "avg_duration_dev",
    "avg_abs_duration_dev",
    "max_abs_duration_dev",
    "avg_leverage",
    "avg_market_value",
    "krd_dev_max_bucket",
    "krd_dev_max_abs",
    "krd_abs_dev_max",
    "krd_abs_dev_max_bucket",
    "config_hash",
    "window_structure",
)

WINDOWS_INFERENCE_COLUMNS: tuple[str, ...] = (
    "strategy",
    "window_set",
    "window",
    "mode",
    "benchmark_version",
    "rolling_version",
    "start",
    "end",
    "n_windows",
    "n_days",
    "partial",
    "return_base_date",
    "return_base_nav",
    "start_close_nav",
    "n_obs",
    "excess_bp",
    "ann_excess_bp",
    "tracking_error_bp",
    "pooled_cumulative_excess_bp",
    "pooled_ann_excess_bp",
    "mean_window_excess_bp",
    "pooled_strategy_total_return",
    "pooled_benchmark_total_return",
    "ann_return",
    "max_drawdown",
    "hac_lag",
    "hac_se_ann_bp",
    "hac_t",
    "hac_ci_low_bp",
    "hac_ci_high_bp",
    "boot_block",
    "boot_reps",
    "boot_seed",
    "boot_ci_low_bp",
    "boot_ci_high_bp",
    "boot_p_value",
    "note",
)


# ---------------------------------------------------------------------------
# 窗口/说明
# ---------------------------------------------------------------------------
def _parse_strategies(text: str | None, *, quick: bool) -> list[str]:
    if text:
        names = [name.strip() for name in text.split(",") if name.strip()]
    elif quick:
        names = ["carry_roll"]
    else:
        names = list(STRATEGY_NAMES)
    unknown = [name for name in names if name not in STRATEGY_NAMES]
    if unknown:
        raise SystemExit(f"未知策略 {unknown}；可用：{list(STRATEGY_NAMES)}")
    return names


def _window_structure(windows: list[WindowSpec], window_years: int) -> str:
    """人类可读的窗口结构（R41：必须体现"约两年整窗 + 尾窗"）。"""
    full = [window for window in windows if not window.partial]
    partial = [window for window in windows if window.partial]
    parts: list[str] = []
    if full:
        if len(full) == 1:
            parts.append(f"{full[0].label} 为约 {window_years} 年整窗（{full[0].n_days} 日）")
        else:
            sizes = "/".join(str(window.n_days) for window in full)
            parts.append(
                f"{full[0].label}–{full[-1].label} 为约 {window_years} 年整窗（{sizes} 日）"
            )
    for window in partial:
        parts.append(f"{window.label} 为 {window.n_days} 日尾窗（partial=True）")
    return "；".join(parts)


def _mode_phrase(mode: str) -> str:
    if mode == "continuous":
        return "连续回测按日期切窗（净值连续；逐窗收益基期=该窗前收 NAV，首个窗口=样本首日 NAV）"
    return "非重叠窗口（每窗独立重置回测；逐窗收益基期=该窗重置 NAV）"


def _window_note(mode: str, window_set: str, structure: str) -> str:
    if window_set == "overlap":
        return (
            "重叠窗口不是独立样本：逐窗 CI/p 仅描述，禁止据此做显著性判断；"
            f"{_mode_phrase(mode)}；窗口结构：{structure}"
        )
    return (
        f"{_mode_phrase(mode)}；窗口结构：{structure}；"
        "窗口数少时 HAC/bootstrap 区间只作描述，不能当显著性检验"
    )


def _pooled_note(mode: str, window_set: str, structure: str, valid: bool) -> str:
    if window_set == "overlap" or not valid:
        return (
            "重叠/重复日期不拼接 pooled 序列，不对拼接序列做 HAC/bootstrap 推断"
            "（R41）；仅列 mean_window_excess_bp 作描述；"
            f"窗口结构：{structure}"
        )
    if mode == "continuous":
        return (
            "pooled=连续路径：策略/基准各自按日复利后相减得全段累计超额；"
            "HAC/bootstrap 基于全段日频主动收益（年化口径，列名 *_ann_bp）；"
            f"窗口结构：{structure}；仅描述，不把窗口当 iid 样本"
        )
    return (
        "pooled=各独立重置窗口的日收益按日期拼接后复利（示意，不构成连续净值；"
        "ann_return/max_drawdown 不可定义）；HAC/bootstrap 基于拼接日频主动收益；"
        f"窗口结构：{structure}；仅描述"
    )


# ---------------------------------------------------------------------------
# 引擎与统计
# ---------------------------------------------------------------------------
def _run_engine(
    factory: Any,
    resolution: BenchmarkResolution,
    *,
    panel: pd.DataFrame,
    calendar: Any,
    funding: pd.Series | None,
    start: date,
    end: date,
) -> Any:
    result = run_backtest(
        factory(),
        panel=panel,
        calendar=calendar,
        funding=funding,
        cfg=resolution.cfg,
        start=start,
        end=end,
        **resolution.run_kwargs,
    )
    actual_version = getattr(result, "benchmark_version", None)
    if actual_version is not None and str(actual_version) != resolution.effective:
        raise RuntimeError(
            f"引擎实际基准版本 {actual_version!r} 与解析结果 {resolution.effective!r} 不一致"
        )
    return result


def _inference_fields(active: pd.Series, *, reps: int, annual_days: int) -> dict[str, float]:
    summary = annualized_inference(active, annual_days=annual_days, reps=reps)
    return {
        "n_obs": float(summary.n_obs),
        "ann_excess_bp": summary.mean_ann_bp,
        "hac_lag": float(summary.hac_lag),
        "hac_se_ann_bp": summary.hac_se_ann_bp,
        "hac_t": summary.hac_t,
        "hac_ci_low_bp": summary.hac_ci_low_bp,
        "hac_ci_high_bp": summary.hac_ci_high_bp,
        "boot_block": float(summary.boot_block),
        "boot_reps": float(summary.boot_reps),
        "boot_seed": float(summary.boot_seed),
        "boot_ci_low_bp": summary.boot_ci_low_bp,
        "boot_ci_high_bp": summary.boot_ci_high_bp,
        "boot_p_value": summary.boot_p_value,
    }


def _max_drawdown(nav: pd.Series) -> float:
    values = nav.astype(float)
    if len(values) == 0:
        return float("nan")
    peak = values.cummax()
    return float((values / peak - 1.0).min())


def _summary_row(
    strategy: str,
    window_set: str,
    window: WindowSpec,
    metrics: dict[str, Any],
    *,
    mode: str,
    benchmark_version: str,
    rolling_version: str,
    config_hash: str,
    structure: str,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "strategy": strategy,
        "window_set": window_set,
        "window": window.label,
        "mode": mode,
        "benchmark_version": benchmark_version,
        "rolling_version": rolling_version,
        "config_hash": config_hash,
        "window_structure": structure,
    }
    row.update(metrics)
    row["return_base_date"] = str(row["return_base_date"])
    return row


def _collect_results(
    windows: list[WindowSpec],
    factory: Any,
    resolution: BenchmarkResolution,
    *,
    panel: pd.DataFrame,
    calendar: Any,
    funding: pd.Series | None,
    mode: str,
    continuous_result: Any | None,
) -> dict[tuple[date, date], Any]:
    """按 (start,end) 去重后跑引擎；continuous 模式复用同一条长回测。"""
    results: dict[tuple[date, date], Any] = {}
    if mode == "continuous":
        if continuous_result is None:
            raise RuntimeError("continuous 模式缺少长回测结果")
        return {(window.start, window.end): continuous_result for window in windows}
    for window in windows:
        key = (window.start, window.end)
        if key in results:
            continue
        results[key] = _run_engine(
            factory,
            resolution,
            panel=panel,
            calendar=calendar,
            funding=funding,
            start=window.start,
            end=window.end,
        )
    return results


def _pooled_fields(
    *,
    strategy_returns: list[pd.Series],
    benchmark_returns: list[pd.Series],
    window_excess: list[float],
    windows: list[WindowSpec],
    mode: str,
    continuous_result: Any | None,
    sample_days: list[date],
    annual_days: int,
    bootstrap_reps: int,
    window_set: str,
    structure: str,
) -> tuple[dict[str, Any], str]:
    """计算 pooled 行字段（R41：全段累计超额 vs 窗口算术均值分开命名）。"""
    nan = float("nan")
    mean_window = float(np.nanmean(window_excess)) if window_excess else nan
    fields: dict[str, Any] = {
        "excess_bp": nan,
        "ann_excess_bp": nan,
        "tracking_error_bp": nan,
        "n_obs": 0.0,
        "pooled_cumulative_excess_bp": mean_window,
        "pooled_ann_excess_bp": nan,
        "mean_window_excess_bp": mean_window,
        "pooled_strategy_total_return": nan,
        "pooled_benchmark_total_return": nan,
        "ann_return": nan,
        "max_drawdown": nan,
        "hac_lag": nan,
        "hac_se_ann_bp": nan,
        "hac_t": nan,
        "hac_ci_low_bp": nan,
        "hac_ci_high_bp": nan,
        "boot_block": nan,
        "boot_reps": nan,
        "boot_seed": nan,
        "boot_ci_low_bp": nan,
        "boot_ci_high_bp": nan,
        "boot_p_value": nan,
    }
    if window_set == "overlap":
        fields["pooled_cumulative_excess_bp"] = nan
        return fields, _pooled_note(mode, window_set, structure, valid=False)
    if not strategy_returns or not benchmark_returns:
        return fields, _pooled_note(mode, "nonoverlap", structure, valid=False)

    strategy_rets = pd.concat(strategy_returns).sort_index()
    benchmark_rets = pd.concat(benchmark_returns).sort_index()
    if strategy_rets.index.has_duplicates or benchmark_rets.index.has_duplicates:
        fields["pooled_cumulative_excess_bp"] = nan
        return fields, _pooled_note(mode, "overlap", structure, valid=False)
    if not strategy_rets.index.equals(benchmark_rets.index):
        raise RuntimeError("pooled 策略/基准日收益索引不一致（实现错误）")

    active = (strategy_rets - benchmark_rets).rename("active_return")
    stats = _inference_fields(active, reps=bootstrap_reps, annual_days=annual_days)
    pooled_strategy_total = float((1.0 + strategy_rets).prod() - 1.0)
    pooled_benchmark_total = float((1.0 + benchmark_rets).prod() - 1.0)
    pooled_cumulative = (pooled_strategy_total - pooled_benchmark_total) * 1e4
    fields.update(
        {
            "pooled_cumulative_excess_bp": pooled_cumulative,
            "pooled_ann_excess_bp": stats["ann_excess_bp"],
            "mean_window_excess_bp": mean_window,
            "pooled_strategy_total_return": pooled_strategy_total,
            "pooled_benchmark_total_return": pooled_benchmark_total,
            "tracking_error_bp": tracking_error_bp(active, annual_days=annual_days),
            **stats,
        }
    )
    if mode == "continuous" and continuous_result is not None:
        full_nav = continuous_result.nav.reindex(sample_days)
        fields["ann_return"] = (
            (1.0 + pooled_strategy_total) ** (float(annual_days) / len(active)) - 1.0
            if len(active) > 0
            else nan
        )
        fields["max_drawdown"] = _max_drawdown(full_nav)
    return fields, _pooled_note(mode, window_set, structure, valid=True)


def _inference_row(
    strategy: str,
    window_set: str,
    *,
    label: str,
    start: date,
    end: date,
    n_windows: int,
    mode: str,
    benchmark_version: str,
    rolling_version: str,
    partial: bool | None,
    return_base_date: str,
    return_base_nav: float,
    start_close_nav: float,
    n_days: int,
    details: dict[str, Any],
    note: str,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "strategy": strategy,
        "window_set": window_set,
        "window": label,
        "mode": mode,
        "benchmark_version": benchmark_version,
        "rolling_version": rolling_version,
        "start": str(start),
        "end": str(end),
        "n_windows": n_windows,
        "n_days": n_days,
        "partial": partial,
        "return_base_date": return_base_date,
        "return_base_nav": return_base_nav,
        "start_close_nav": start_close_nav,
        "note": note,
    }
    row.update(details)
    return row


def _run_strategy(
    strategy_name: str,
    factory: Any,
    resolution: BenchmarkResolution,
    *,
    panel: pd.DataFrame,
    calendar: Any,
    funding: pd.Series | None,
    sample_days: list[date],
    window_sets: list[tuple[str, list[WindowSpec]]],
    mode: str,
    window_years: int,
    bootstrap_reps: int,
    annual_days: int,
    config_hash: str,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """跑一个策略的所有窗口，返回 (summary_rows, inference_rows)。"""
    all_windows = [window for _name, windows in window_sets for window in windows]
    continuous_result = None
    if mode == "continuous":
        continuous_result = _run_engine(
            factory,
            resolution,
            panel=panel,
            calendar=calendar,
            funding=funding,
            start=sample_days[0],
            end=sample_days[-1],
        )
    results = _collect_results(
        all_windows,
        factory,
        resolution,
        panel=panel,
        calendar=calendar,
        funding=funding,
        mode=mode,
        continuous_result=continuous_result,
    )

    summary_rows: list[dict[str, Any]] = []
    inference_rows: list[dict[str, Any]] = []
    for window_set, windows in window_sets:
        structure = _window_structure(windows, window_years)
        strategy_returns: list[pd.Series] = []
        benchmark_returns: list[pd.Series] = []
        window_excess: list[float] = []
        first_window = windows[0] if windows else None
        first_result = results[(first_window.start, first_window.end)] if first_window else None
        if first_window is not None and first_result is not None:
            pooled_base_date, pooled_base_nav, pooled_start_close = window_return_basis(
                first_result, first_window
            )
        else:  # pragma: no cover - 窗口集不会为空
            pooled_base_date, pooled_base_nav, pooled_start_close = (
                sample_days[0],
                float("nan"),
                float("nan"),
            )
        for window in windows:
            result = results[(window.start, window.end)]
            metrics = window_metrics(result, window, annual_days=annual_days)
            summary_rows.append(
                _summary_row(
                    strategy_name,
                    window_set,
                    window,
                    metrics,
                    mode=mode,
                    benchmark_version=resolution.effective,
                    rolling_version=resolution.rolling_version,
                    config_hash=config_hash,
                    structure=structure,
                )
            )
            window_strategy_returns, window_benchmark_returns = daily_returns(result, window.days)
            strategy_returns.append(window_strategy_returns)
            benchmark_returns.append(window_benchmark_returns)
            window_excess.append(float(metrics["excess_bp"]))
            active = window_active_returns(result, window)
            window_note = _window_note(mode, window_set, structure)
            inference_rows.append(
                _inference_row(
                    strategy_name,
                    window_set,
                    label=window.label,
                    start=window.start,
                    end=window.end,
                    n_windows=len(windows),
                    mode=mode,
                    benchmark_version=resolution.effective,
                    rolling_version=resolution.rolling_version,
                    partial=bool(window.partial),
                    return_base_date=str(metrics["return_base_date"]),
                    return_base_nav=float(metrics["return_base_nav"]),
                    start_close_nav=float(metrics["start_close_nav"]),
                    n_days=int(metrics["n_days"]),
                    details={
                        "excess_bp": float(metrics["excess_bp"]),
                        "ann_excess_bp": float(metrics["ann_excess_bp"]),
                        "tracking_error_bp": float(metrics["tracking_error_bp"]),
                        **_inference_fields(active, reps=bootstrap_reps, annual_days=annual_days),
                    },
                    note=window_note,
                )
            )

        pooled_fields, pooled_note = _pooled_fields(
            strategy_returns=strategy_returns,
            benchmark_returns=benchmark_returns,
            window_excess=window_excess,
            windows=windows,
            mode=mode,
            continuous_result=continuous_result,
            sample_days=sample_days,
            annual_days=annual_days,
            bootstrap_reps=bootstrap_reps,
            window_set=window_set,
            structure=structure,
        )
        inference_rows.append(
            _inference_row(
                strategy_name,
                window_set,
                label="pooled",
                start=first_window.start if first_window else sample_days[0],
                end=windows[-1].end if windows else sample_days[-1],
                n_windows=len(windows),
                mode=mode,
                benchmark_version=resolution.effective,
                rolling_version=resolution.rolling_version,
                partial=None,
                return_base_date=str(pooled_base_date),
                return_base_nav=float(pooled_base_nav),
                start_close_nav=float(pooled_start_close),
                n_days=sum(window.n_days for window in windows),
                details=pooled_fields,
                note=pooled_note,
            )
        )
    return summary_rows, inference_rows


# ---------------------------------------------------------------------------
# 买持窗口右端上限 / long_sample 核对 / 输出
# ---------------------------------------------------------------------------
def _buyhold_end_cap(cfg: Any, calendar: Any) -> Any:
    """买持基准的窗口右端上限：最后一天 T+1 结算必须早于最早到期券。"""
    tenors = [float(tenor) for tenor in cfg.portfolio.universe]
    if not tenors:
        return None
    min_tenor = min(tenors)
    lag = int(cfg.pricing.settlement_lag_days)
    calendar_last = calendar.last_day

    def _prev_trading(day: date) -> date:
        cursor = day
        while True:
            cursor -= timedelta(days=1)
            if calendar.is_trading_day(cursor):
                return cursor

    def cap(start: date) -> date:
        maturity = add_years(start, min_tenor)
        if maturity > calendar_last:
            return date.max
        day = _prev_trading(maturity)
        while calendar.settlement_date(day, lag) >= maturity:
            day = _prev_trading(day)
        return day

    return cap


def _crosscheck_long_sample(
    out: Path,
    *,
    strategy: str,
    ann_return: float,
    max_drawdown: float,
    pooled_cumulative_excess_bp: float,
    benchmark_version: str,
    rolling_version: str,
) -> str | None:
    """与 ``reports/long_sample_summary.csv`` 核对 ann/MDD/全段超额（R41 数值一致性）。"""
    path = out / "long_sample_summary.csv"
    if not path.exists():
        return None
    try:
        frame = pd.read_csv(path)
    except (OSError, pd.errors.ParserError):  # pragma: no cover - 产物损坏时不阻断
        return None
    if "strategy" not in frame.columns or "ann_return" not in frame.columns:
        return None
    mask = frame["strategy"].astype(str) == strategy
    if "benchmark_version" in frame.columns:
        mask &= frame["benchmark_version"].astype(str) == benchmark_version
    if "rolling_version" in frame.columns and rolling_version:
        mask &= frame["rolling_version"].astype(str) == rolling_version
    rows = frame.loc[mask]
    if len(rows) != 1:
        return None
    row = rows.iloc[0]
    delta_ann = float(row["ann_return"]) - ann_return
    delta_mdd = float(row["max_drawdown"]) - max_drawdown
    message = f"  long_sample 核对 {strategy}: Δann={delta_ann:+.2e}, ΔMDD={delta_mdd:+.2e}"
    if "excess_bp" in frame.columns and np.isfinite(pooled_cumulative_excess_bp):
        delta_excess = float(row["excess_bp"]) - pooled_cumulative_excess_bp
        message += f", Δ全段超额={delta_excess:+.2e}bp"
    return message


def _print_tables(summary: pd.DataFrame, inference: pd.DataFrame) -> None:
    if len(summary):
        print("\n=== 逐窗口描述统计（收益基期见 return_base_*）===")
        columns = [
            "strategy",
            "window_set",
            "window",
            "n_days",
            "partial",
            "return_base_nav",
            "strategy_total_return",
            "excess_bp",
            "ann_return",
            "max_drawdown",
            "ann_excess_bp",
            "tracking_error_bp",
            "total_cost",
            "n_trades",
            "n_violation_days",
        ]
        with pd.option_context("display.width", 320):
            print(
                summary[columns].to_string(index=False, float_format=lambda value: f"{value:.4f}")
            )
    if len(inference):
        print("\n=== 区间估计（HAC lag=5；块 bootstrap block=20；*_ann_bp 为年化）===")
        columns = [
            "strategy",
            "window_set",
            "window",
            "n_obs",
            "mean_window_excess_bp",
            "pooled_cumulative_excess_bp",
            "pooled_ann_excess_bp",
            "ann_return",
            "max_drawdown",
            "hac_se_ann_bp",
            "hac_t",
            "hac_ci_low_bp",
            "hac_ci_high_bp",
            "boot_ci_low_bp",
            "boot_ci_high_bp",
            "boot_p_value",
            "boot_reps",
        ]
        with pd.option_context("display.width", 340):
            print(
                inference[columns].to_string(index=False, float_format=lambda value: f"{value:.4f}")
            )


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="多窗口 + HAC/块 bootstrap 区间估计")
    parser.add_argument("--start", type=date.fromisoformat, default=None)
    parser.add_argument("--end", type=date.fromisoformat, default=None)
    parser.add_argument("--config", default=None)
    parser.add_argument(
        "--strategies",
        default=None,
        help="逗号分隔策略名（zero_tilt/carry_roll/2s10s/rv）",
    )
    parser.add_argument(
        "--window-years", type=int, default=None, help="每窗标称年数（默认 2；quick 默认 1）"
    )
    parser.add_argument(
        "--mode",
        choices=("reset", "continuous"),
        default=None,
        help="reset=每窗独立重置回测（买持基准默认）；continuous=一条长回测按日切窗（滚动基准默认）",
    )
    parser.add_argument(
        "--overlap", action="store_true", help="额外报告重叠窗口（step=1 年，仅描述）"
    )
    parser.add_argument(
        "--benchmark-version",
        choices=("buyhold", "rolling"),
        default="buyhold",
        help="基准版本；引擎尚未支持 rolling 时明确告警并回退 buyhold",
    )
    parser.add_argument(
        "--quick", action="store_true", help="短样本 + 单策略 + 少 bootstrap（测试用）"
    )
    args = parser.parse_args(argv)

    cfg = get_config(args.config)
    resolution = resolve_benchmark_version(cfg, args.benchmark_version)
    if resolution.fell_back:
        print(f"[warn] {resolution.note}")
    if args.mode is not None:
        mode = args.mode
    else:
        mode = "continuous" if resolution.effective == "rolling" else "reset"
    if mode == "continuous" and resolution.effective == "buyhold":
        print(
            "[warn] continuous 模式搭配买持基准时，后段窗口的基准会因短券到期而失去可比性；"
            "正式结果请用 reset 模式或滚动基准"
        )
    if mode == "reset" and resolution.effective == "rolling":
        print(
            "[warn] reset 模式 + rolling 基准：每窗会重启滚动宇宙（独立窗口可比，但不要与连续长回测混比）"
        )

    panel, funding, calendar = load_market(cfg)
    data_last = panel.index.max().date()
    start = args.start or (QUICK_START if args.quick else cfg.backtest.start_date or DEFAULT_START)
    end = args.end or (QUICK_END if args.quick else (cfg.backtest.end_date or data_last))
    if end > data_last:
        print(f"[warn] --end {end} 超过数据末日 {data_last}，按 {data_last} 处理")
        end = data_last
    if end <= start:
        parser.error(f"--end({end}) 必须晚于 --start({start})")
    sample_days = list(calendar.trading_days_between(start, end))
    if len(sample_days) < 2:
        parser.error(f"样本 {start}~{end} 内交易日不足 2 天")

    window_years = args.window_years or (1 if args.quick else 2)
    strategy_names = _parse_strategies(args.strategies, quick=args.quick)
    end_cap = (
        _buyhold_end_cap(resolution.cfg, calendar)
        if (mode == "reset" and resolution.effective == "buyhold")
        else None
    )
    window_sets: list[tuple[str, list[WindowSpec]]] = [
        (
            "nonoverlap",
            build_windows(sample_days, window_years=window_years, overlap=False, end_cap=end_cap),
        )
    ]
    if args.overlap:
        window_sets.append(
            (
                "overlap",
                build_windows(
                    sample_days,
                    window_years=window_years,
                    overlap=True,
                    step_years=1,
                    end_cap=end_cap,
                ),
            )
        )
    bootstrap_reps = QUICK_BOOTSTRAP_REPS if args.quick else BOOTSTRAP_REPS
    annual_days = int(resolution.cfg.backtest.annual_days)
    factories = build_strategy_factories(strategy_names, panel, resolution.cfg, end)

    main_structure = _window_structure(window_sets[0][1], window_years)
    policy_text = f"（政策 {resolution.rolling_version}）" if resolution.rolling_version else ""
    print(
        f"\n[windows] 样本 {sample_days[0]} → {sample_days[-1]}（{len(sample_days)} 交易日）；"
        f"mode={mode}；benchmark={resolution.effective}{policy_text}"
    )
    print(f"[windows] 窗口结构：{main_structure}")

    summary_rows: list[dict[str, Any]] = []
    inference_rows: list[dict[str, Any]] = []
    config_hash = resolution.cfg.hash()
    crosscheck_messages: list[str] = []
    for strategy_name, factory in factories:
        n_windows = sum(len(windows) for _set, windows in window_sets)
        print(f"\n[windows] 策略 {strategy_name}: {len(window_sets)} 个窗口集 / {n_windows} 个窗口")
        strategy_summary, strategy_inference = _run_strategy(
            strategy_name,
            factory,
            resolution,
            panel=panel,
            calendar=calendar,
            funding=funding,
            sample_days=sample_days,
            window_sets=window_sets,
            mode=mode,
            window_years=window_years,
            bootstrap_reps=bootstrap_reps,
            annual_days=annual_days,
            config_hash=config_hash,
        )
        summary_rows.extend(strategy_summary)
        inference_rows.extend(strategy_inference)
        if mode == "continuous":
            pooled_rows = [
                row
                for row in strategy_inference
                if row["window_set"] == "nonoverlap" and row["window"] == "pooled"
            ]
            if pooled_rows:
                message = _crosscheck_long_sample(
                    cfg.paths.abs("reports"),
                    strategy=strategy_name,
                    ann_return=float(pooled_rows[0]["ann_return"]),
                    max_drawdown=float(pooled_rows[0]["max_drawdown"]),
                    pooled_cumulative_excess_bp=float(
                        pooled_rows[0]["pooled_cumulative_excess_bp"]
                    ),
                    benchmark_version=resolution.effective,
                    rolling_version=resolution.rolling_version,
                )
                if message:
                    crosscheck_messages.append(message)

    summary = pd.DataFrame(summary_rows, columns=list(WINDOWS_SUMMARY_COLUMNS))
    inference = pd.DataFrame(inference_rows, columns=list(WINDOWS_INFERENCE_COLUMNS))
    out = cfg.paths.abs("reports")
    out.mkdir(parents=True, exist_ok=True)
    summary.to_csv(out / "windows_summary.csv", index=False, encoding="utf-8-sig")
    inference.to_csv(out / "windows_inference.csv", index=False, encoding="utf-8-sig")

    _print_tables(summary, inference)
    if crosscheck_messages:
        print("\n=== 与 long_sample_summary.csv 的数值核对（R41）===")
        for message in crosscheck_messages:
            print(message)
    print(
        "\n口径提醒：逐窗收益基期见 return_base_nav（continuous=该窗前收 NAV、"
        "reset=该窗重置 NAV）；pooled_cumulative_excess_bp=全段复利累计超额、"
        "mean_window_excess_bp=各窗累计超额算术均值；窗口数少时 CI 仅描述；"
        "重叠窗口不做独立样本推断。"
    )
    print(f"\n产物目录: {out}")
    print(f"  - {out / 'windows_summary.csv'}")
    print(f"  - {out / 'windows_inference.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
