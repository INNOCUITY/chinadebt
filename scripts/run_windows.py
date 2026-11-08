#!/usr/bin/env python3
"""多窗口 + 区间估计研究（计划 §12.12-B；只做研究，不改引擎/配置）。

研究问题
--------
1. 把历史样本切成**多个独立窗口**（默认 2 年/窗），逐窗报告策略/基准收益、超额、
   成本分项、换手、成交、主动 DV01/久期偏离、违规与拒单；
2. 对日频主动收益 ``active_t = r_strategy,t − r_benchmark,t`` 做**考虑时间相关性**
   的区间估计：Newey-West HAC（lag=5）t 统计量/95% CI + 固定块 bootstrap
   （block=20、2000 次、固定 seed）年化超额 95% CI 与双侧 p 值。

**预注册统计参数（写死在 ``ficc.research.inference``，不得按结果调整）**：
``HAC_LAG=5``、``BOOTSTRAP_BLOCK=20``、``BOOTSTRAP_REPS=2000``、
``BOOTSTRAP_SEED=20261108``、年化 250 交易日。``--quick`` 只把 bootstrap 次数降到
200 以便测试（输出中记录 ``boot_reps``），其余统计口径不变。

**窗口是否独立/每窗重置（必须一起披露）**：
- 当前买持基准（buyhold）的合成券宇宙在回测起点一次性生成、固定面值持有到期末；
  若把一条长回测切成子区间，后段基准会因短券到期变成现金、失去可比性。因此
  buyhold 默认 ``--mode reset``：**每个窗口独立跑一次引擎**（窗口起点重建宇宙与
  基准），不把窗口拼成连续净值（计划 §12.12-B "每窗重置"要求）；并按最短期限券
  的 T+1 结算日晚于到期日的问题，把每窗右端收缩到最后一个可估值交易日
  （被裁掉的交易日顺延为下一窗起点，仍不重不漏）。
- 滚动基准（``--benchmark-version rolling``，引擎已支持 ``portfolio.rolling`` 与
  ``backtest.benchmark_version``）默认 ``--mode continuous``：一条长回测按日期切窗，
  滚动政策保证短券到期/换券连续可比；脚本会自动在内存里开启滚动政策并记录
  ``rolling_version``（不修改 config.yaml）。
- 非重叠窗口之间也是**不同起点的独立回测**，不是同一路径的连续样本；窗口数少时
  CI 只作描述，不能当显著性检验。
- ``--overlap`` 追加**重叠窗口**（step=1 年）：重叠窗口不是独立样本，逐窗 CI/p
  仅描述，禁止据此做显著性判断；``pooled`` 行只是把各窗主动收益按日拼接的展示。

产物
----
``reports/windows_summary.csv``   逐窗口描述统计；
``reports/windows_inference.csv`` 策略×窗口集合的 HAC/块 bootstrap 区间估计
（逐窗 + ``pooled`` 描述行）。

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
    resolve_benchmark_version,
    window_active_returns,
    window_metrics,
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
    "n_obs",
    "initial_nav",
    "final_nav",
    "strategy_total_return",
    "benchmark_total_return",
    "excess_bp",
    "ann_excess_bp",
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
    "n_obs",
    "excess_bp",
    "ann_excess_bp",
    "tracking_error_bp",
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

NOTE_NONOVERLAP = (
    "非重叠窗口（每窗独立重置的回测）；窗口数少时 HAC/bootstrap 区间只作描述，不能当显著性检验"
)
NOTE_OVERLAP = "重叠窗口不是独立样本：逐窗 CI/p 仅描述，禁止据此做显著性判断"
NOTE_POOLED = (
    "pooled=按日拼接各窗主动收益（窗口独立重置，不构成连续净值）；仅描述，不把窗口当 iid 样本"
)


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
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "strategy": strategy,
        "window_set": window_set,
        "window": window.label,
        "mode": mode,
        "benchmark_version": benchmark_version,
        "rolling_version": rolling_version,
        "config_hash": config_hash,
    }
    row.update(metrics)
    return row


def _inference_row(
    strategy: str,
    window_set: str,
    *,
    label: str,
    start: date,
    end: date,
    n_windows: int,
    excess_bp: float,
    tracking_error: float,
    fields: dict[str, float],
    mode: str,
    benchmark_version: str,
    note: str,
) -> dict[str, Any]:
    return {
        "strategy": strategy,
        "window_set": window_set,
        "window": label,
        "mode": mode,
        "benchmark_version": benchmark_version,
        "start": str(start),
        "end": str(end),
        "n_windows": n_windows,
        "excess_bp": excess_bp,
        "tracking_error_bp": tracking_error,
        **fields,
        "note": note,
    }


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
        actives: list[pd.Series] = []
        window_excess: list[float] = []
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
                )
            )
            active = window_active_returns(result, window)
            actives.append(active)
            window_excess.append(float(metrics["excess_bp"]))
            note = NOTE_OVERLAP if window_set == "overlap" else NOTE_NONOVERLAP
            inference_rows.append(
                _inference_row(
                    strategy_name,
                    window_set,
                    label=window.label,
                    start=window.start,
                    end=window.end,
                    n_windows=len(windows),
                    excess_bp=float(metrics["excess_bp"]),
                    tracking_error=float(metrics["tracking_error_bp"]),
                    fields=_inference_fields(active, reps=bootstrap_reps, annual_days=annual_days),
                    mode=mode,
                    benchmark_version=resolution.effective,
                    note=note,
                )
            )
        pooled = pd.concat(actives) if actives else pd.Series(dtype=float)
        pooled = pooled.sort_index()
        inference_rows.append(
            _inference_row(
                strategy_name,
                window_set,
                label="pooled",
                start=windows[0].start if windows else sample_days[0],
                end=windows[-1].end if windows else sample_days[-1],
                n_windows=len(windows),
                excess_bp=float(np.nanmean(window_excess)) if window_excess else float("nan"),
                tracking_error=float("nan"),
                fields=_inference_fields(pooled, reps=bootstrap_reps, annual_days=annual_days),
                mode=mode,
                benchmark_version=resolution.effective,
                note=NOTE_OVERLAP if window_set == "overlap" else NOTE_POOLED,
            )
        )
    return summary_rows, inference_rows


def _buyhold_end_cap(cfg: Any, calendar: Any) -> Any:
    """买持基准的窗口右端上限：最后一天 T+1 结算必须早于最早到期券。

    固定面值基准在窗口起点发行合成券，最短期限券（如 2Y）会在「窗口起点 + 期限」
    到期；引擎按 T+1 结算日估值，因此窗口最后一天的结算日必须严格早于到期日，
    否则 ``mark_faces`` 会因"已到期无法估值"报错。返回 ``cap(start) -> date``。
    """
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


def _print_tables(summary: pd.DataFrame, inference: pd.DataFrame) -> None:
    if len(summary):
        print("\n=== 逐窗口描述统计 ===")
        columns = [
            "strategy",
            "window_set",
            "window",
            "n_days",
            "excess_bp",
            "ann_excess_bp",
            "tracking_error_bp",
            "total_cost",
            "total_turnover",
            "n_trades",
            "avg_abs_dv01_dev",
            "max_abs_dv01_dev",
            "avg_abs_duration_dev",
            "n_violation_days",
        ]
        with pd.option_context("display.width", 300):
            print(
                summary[columns].to_string(index=False, float_format=lambda value: f"{value:.4f}")
            )
    if len(inference):
        print("\n=== 区间估计（HAC lag=5；块 bootstrap block=20）===")
        columns = [
            "strategy",
            "window_set",
            "window",
            "n_obs",
            "ann_excess_bp",
            "hac_se_ann_bp",
            "hac_t",
            "hac_ci_low_bp",
            "hac_ci_high_bp",
            "boot_ci_low_bp",
            "boot_ci_high_bp",
            "boot_p_value",
            "boot_reps",
        ]
        with pd.option_context("display.width", 300):
            print(
                inference[columns].to_string(index=False, float_format=lambda value: f"{value:.4f}")
            )


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
        "--window-years", type=int, default=None, help="每窗年数（默认 2；quick 默认 1）"
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

    summary_rows: list[dict[str, Any]] = []
    inference_rows: list[dict[str, Any]] = []
    config_hash = resolution.cfg.hash()
    for strategy_name, factory in factories:
        n_windows = sum(len(windows) for _set, windows in window_sets)
        print(
            f"\n[windows] 策略 {strategy_name}: {len(window_sets)} 个窗口集 / {n_windows} 个窗口 "
            f"（{start} → {end}，每窗 {window_years} 年，mode={mode}，"
            f"benchmark={resolution.effective}）"
        )
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
            bootstrap_reps=bootstrap_reps,
            annual_days=annual_days,
            config_hash=config_hash,
        )
        summary_rows.extend(strategy_summary)
        inference_rows.extend(strategy_inference)

    summary = pd.DataFrame(summary_rows, columns=list(WINDOWS_SUMMARY_COLUMNS))
    inference = pd.DataFrame(inference_rows, columns=list(WINDOWS_INFERENCE_COLUMNS))
    out = cfg.paths.abs("reports")
    out.mkdir(parents=True, exist_ok=True)
    summary.to_csv(out / "windows_summary.csv", index=False, encoding="utf-8-sig")
    inference.to_csv(out / "windows_inference.csv", index=False, encoding="utf-8-sig")

    _print_tables(summary, inference)
    print(
        "\n口径提醒：非重叠窗口也是不同起点的独立回测；窗口数少时 CI 只作描述。"
        "重叠窗口不是独立样本，禁止据此做显著性判断。"
    )
    print(f"\n产物目录: {out}")
    print(f"  - {out / 'windows_summary.csv'}")
    print(f"  - {out / 'windows_inference.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
