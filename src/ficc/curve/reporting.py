"""本轮报告生成：即期曲线、PCA、因子交叉校验、正交化诊断（R13/R16/R20/R21）。

统一入口（产物写入 ``out_dir``，默认 ``cfg.paths.abs("reports")``）::

    from ficc.curve.reporting import build_reports
    build_reports(cfg=cfg, panel=panel, start=..., end=..., out_dir=...)

命令行（仅做 CLI 包装）::

    PYTHONPATH=src .venv/bin/python -m ficc.curve.reporting --end 2020-12-31 --out-dir /tmp/r

口径（R14/R15/R16/R20/R21）
-----------------------------
- 因子契约：``level_all / level_long / slope / curvature``（固定 11 期限，缺失→NaN）；
- PCA 样本：收益率变化 Δy（bp）；winsor 与因子聚合共享同一
  :func:`ficc.curve.factors.preprocess_changes`（R16：先逐期限截尾再线性聚合）；
- 验收映射：:data:`ficc.curve.factors.FACTOR_TARGETS` 显式给出，未知因子只报探索性
  相关、``meets_target=None``；
- 重复记录：走数据层 :func:`ficc.data.clean.dedupe_long_frame`（一致可去重、冲突报错）；
- 正交化：:func:`ficc.curve.factors.fit_orthogonalizer` 只在训练窗拟合，
  ``transform`` 冻结应用；报告同时给出"水平空间/日变化"两组相关并标注
  "样本内诊断，不等于风险正交"。
"""

from __future__ import annotations

import argparse
import os
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

os.environ.setdefault("MPLCONFIGDIR", tempfile.mkdtemp(prefix="mpl-"))
import matplotlib

matplotlib.use("Agg")

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))

import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from ficc.config import Config, get_config, load_config  # noqa: E402
from ficc.curve.bootstrap import bootstrap_spot  # noqa: E402
from ficc.curve.curve import Curve  # noqa: E402
from ficc.curve.factors import (  # noqa: E402
    FACTOR_COLUMNS,
    factor_correlation,
    factor_crosscheck,
    factor_definition_table,
    fit_orthogonalizer,
    loading_stability,
    observable_factors,
    pca_factors,
)
from ficc.data.clean import dedupe_long_frame  # noqa: E402
from ficc.data.schema import CANONICAL_TENORS, LONG_COLUMNS, TENOR_LABELS  # noqa: E402
from ficc.logging import get_logger  # noqa: E402

__all__ = ["build_reports", "load_panel", "long_frame_to_panel", "main"]

log = get_logger("curve_reporting")

SPOT_DATES: tuple[str, ...] = ("2024-06-28", "2013-06-20", "2026-11-05")
ROLLING_WINDOW: int = 250
WINSOR_BP: float = 10.0
ORTH_FIT_WINDOW: int = 250


# ---------------------------------------------------------------------------
# 数据加载（R20：重复校验走数据层）
# ---------------------------------------------------------------------------
def long_frame_to_panel(long_df: pd.DataFrame) -> pd.DataFrame:
    """长表 → 宽表；**先**复用数据层重复校验（一致去重、冲突抛异常）。"""
    if not isinstance(long_df, pd.DataFrame):
        raise TypeError(f"long_df 必须是 DataFrame，实际 {type(long_df).__name__}")
    missing = [col for col in LONG_COLUMNS if col not in long_df.columns]
    if missing:
        raise ValueError(f"长表缺少必需列 {missing}；实际列 {list(long_df.columns)}")
    dedupe = dedupe_long_frame(long_df.loc[:, list(LONG_COLUMNS)], context="报告输入曲线数据")
    panel = dedupe.frame.pivot(index="date", columns="tenor", values="ytm_pct").sort_index()
    panel = panel.reindex(columns=list(CANONICAL_TENORS))
    panel.columns = pd.Index(panel.columns.to_numpy(dtype=float), name="tenor")
    panel.index = pd.DatetimeIndex(panel.index, name="date")
    panel.attrs["rows_removed_duplicates"] = dedupe.rows_removed
    return panel.sort_index()


def _truncate(
    panel: pd.DataFrame,
    start: str | pd.Timestamp | None,
    end: str | pd.Timestamp | None,
) -> pd.DataFrame:
    frame = panel.copy()
    if not isinstance(frame.index, pd.DatetimeIndex):
        frame.index = pd.to_datetime(frame.index, errors="raise")
    if start is not None:
        frame = frame.loc[frame.index >= pd.Timestamp(start)]
    if end is not None:
        frame = frame.loc[frame.index <= pd.Timestamp(end)]
    if frame.empty:
        raise ValueError(f"按 start={start!r} / end={end!r} 截断后 panel 为空")
    return frame.sort_index()


def load_panel(
    cfg: Config | None = None,
    *,
    start: str | pd.Timestamp | None = None,
    end: str | pd.Timestamp | None = None,
) -> pd.DataFrame:
    """读取收益率宽表（parquet 优先，Phase 0 CSV 回退），截断到 [start, end]。"""
    resolved = cfg if cfg is not None else get_config()
    processed = resolved.paths.abs("processed") / "curve_long.parquet"
    if processed.exists():
        long_df = pd.read_parquet(processed)
        long_df = long_df.rename(columns={"tenor_years": "tenor", "yield_pct": "ytm_pct"})
        source = str(processed)
    else:
        raw = _ROOT / "data" / "raw_spike" / "cb_gzqx_standard_terms_2002_2026.csv"
        if not raw.exists():
            raise FileNotFoundError(f"找不到曲线数据：{processed} 与 {raw} 均不存在")
        long_df = pd.read_csv(raw).rename(columns={"term_years": "tenor", "yield_pct": "ytm_pct"})
        source = str(raw)
    long_df = long_df.loc[:, ["date", "tenor", "ytm_pct"]].copy()
    long_df["date"] = pd.to_datetime(long_df["date"], errors="raise")
    long_df["tenor"] = pd.to_numeric(long_df["tenor"], errors="raise")
    long_df["ytm_pct"] = pd.to_numeric(long_df["ytm_pct"], errors="raise")
    long_df = long_df[long_df["tenor"].isin(CANONICAL_TENORS)]
    panel = long_frame_to_panel(long_df)
    panel = _truncate(panel, start, end)
    log.info(
        "数据来源: %s；面板 %d 天 × %d 期限（%s→%s）",
        source,
        *panel.shape,
        panel.index.min().date(),
        panel.index.max().date(),
    )
    return panel


# ---------------------------------------------------------------------------
# 即期曲线对比
# ---------------------------------------------------------------------------
def _tenor_label(tenor: float) -> str:
    return TENOR_LABELS.get(float(tenor), f"{float(tenor):g}Y")


def build_spot_comparison(panel: pd.DataFrame) -> pd.DataFrame:
    """对代表性日期 bootstrap，返回 YTM vs 即期对比长表（日期受 panel 边界约束）。"""
    rows: list[pd.DataFrame] = []
    for day in SPOT_DATES:
        stamp = pd.Timestamp(day)
        if stamp < panel.index.min() or stamp > panel.index.max():
            log.warning("固定代表日 %s 不在样本区间内，跳过（R13 边界约束）", day)
            continue
        row = panel.loc[stamp].dropna()
        if row.empty:
            continue
        curve = Curve(
            as_of=stamp.date(),
            tenors=row.index.to_numpy(dtype=float),
            rates=row.to_numpy(dtype=float),
            method="linear",
        )
        spot = bootstrap_spot(curve)
        frame = pd.DataFrame(
            {
                "date": stamp,
                "tenor": row.index.to_numpy(dtype=float),
                "tenor_label": [_tenor_label(t) for t in row.index],
                "ytm_pct": row.to_numpy(dtype=float),
                "zero_pct": np.asarray(spot.zero_rate(row.index.to_numpy(dtype=float))),
            }
        )
        frame["discount"] = np.asarray(spot.discount(frame["tenor"].to_numpy(dtype=float)))
        frame["zero_minus_ytm_bp"] = (frame["zero_pct"] - frame["ytm_pct"]) * 100.0
        frame["observation_date"] = spot.observation_date.isoformat()
        frame["settlement_date"] = spot.settlement_date.isoformat()
        frame["source_curve_method"] = "linear"
        frame["synthetic_assumption"] = "par-bond bootstrap (not official spot curve)"
        rows.append(frame)
    if not rows:
        raise ValueError("没有任何代表性日期落在样本区间内（检查 start/end）")
    return pd.concat(rows, ignore_index=True)


def plot_spot_vs_ytm(comparison: pd.DataFrame, path: Path, dpi: int) -> None:
    dates = list(comparison["date"].drop_duplicates())
    fig, axes = plt.subplots(1, len(dates), figsize=(5.2 * len(dates), 4.4), squeeze=False)
    for ax, day in zip(axes.ravel(), dates, strict=False):
        block = comparison[comparison["date"] == day]
        ax.plot(block["tenor"], block["ytm_pct"], "o-", ms=3, lw=1.2, label="YTM (observed)")
        ax.plot(block["tenor"], block["zero_pct"], "s--", ms=3, lw=1.2, label="zero (bootstrap)")
        ax.set_xscale("log")
        ax.set_xticks([0.25, 1, 2, 5, 10, 30])
        ax.set_xticklabels(["3M", "1Y", "2Y", "5Y", "10Y", "30Y"])
        ax.set_title(pd.Timestamp(day).strftime("%Y-%m-%d"))
        ax.set_xlabel("Tenor")
        ax.set_ylabel("Rate (%)")
        ax.grid(alpha=0.25)
        ax.legend(fontsize=8)
    fig.suptitle("Synthetic spot curve (par-bond bootstrap) vs observed YTM")
    fig.tight_layout()
    fig.savefig(path, dpi=dpi)
    plt.close(fig)


def plot_pca_loadings(
    pca_full: Any,
    pca_winsor: Any,
    pca_roll: Any,
    stability: pd.DataFrame,
    path: Path,
    dpi: int,
) -> None:
    components = list(pca_full.loadings.columns)
    fig, axes = plt.subplots(
        1, len(components), figsize=(5.0 * len(components), 4.4), squeeze=False
    )
    tenors = pca_full.loadings.index.to_numpy(dtype=float)
    history = pca_roll.loadings_history
    for ax, component in zip(axes.ravel(), components, strict=False):
        ax.plot(tenors, pca_full.loadings[component], "o-", ms=3, lw=1.3, label="full (centered)")
        ax.plot(
            tenors,
            pca_winsor.loadings[component],
            "^--",
            ms=3,
            lw=1.1,
            label=f"full winsor ±{WINSOR_BP:g}bp",
        )
        ax.plot(tenors, pca_roll.loadings[component], "s-", ms=3, lw=1.1, label="rolling mean")
        if history is not None:
            std = history[component].std(axis=0, ddof=0).to_numpy(dtype=float)
        else:  # pragma: no cover - 防御式
            std = np.full(tenors.size, float(stability.loc[component, "loading_std_mean"]))
        ax.fill_between(
            tenors,
            pca_roll.loadings[component].to_numpy(dtype=float) - std,
            pca_roll.loadings[component].to_numpy(dtype=float) + std,
            color="0.7",
            alpha=0.3,
            label="rolling ± std",
        )
        ax.axhline(0.0, color="black", lw=0.7, ls=":")
        ax.set_xscale("log")
        ax.set_xticks([0.25, 1, 2, 5, 10, 30])
        ax.set_xticklabels(["3M", "1Y", "2Y", "5Y", "10Y", "30Y"])
        ax.set_title(f"{component} loadings (Δy sample)")
        ax.set_xlabel("Tenor")
        ax.grid(alpha=0.25)
        ax.legend(fontsize=7)
    axes.ravel()[0].set_ylabel("Loading")
    fig.tight_layout()
    fig.savefig(path, dpi=dpi)
    plt.close(fig)


def plot_factor_corr_matrix(
    factors: pd.DataFrame,
    orth_window: pd.DataFrame,
    orth_full: pd.DataFrame,
    path: Path,
    dpi: int,
) -> None:
    """正交化前后（水平/变化）的因子相关热力图（R21 诊断）。"""
    matrices = [
        ("levels before", factor_correlation(factors, space="levels")),
        ("levels, window-fit transform", factor_correlation(orth_window, space="levels")),
        ("changes before", factor_correlation(factors, space="changes")),
        (
            "changes, window-fit transform",
            factor_correlation(orth_window, space="changes"),
        ),
        ("levels, full-fit (in-sample)", factor_correlation(orth_full, space="levels")),
        ("changes, full-fit (in-sample)", factor_correlation(orth_full, space="changes")),
    ]
    fig, axes = plt.subplots(2, 3, figsize=(13.5, 8.0), squeeze=False)
    for ax, (title, matrix) in zip(axes.ravel(), matrices, strict=False):
        values = matrix.to_numpy(dtype=float)
        im = ax.imshow(values, cmap="RdBu_r", vmin=-1.0, vmax=1.0)
        labels = [str(c) for c in matrix.columns]
        ax.set_xticks(range(len(labels)))
        ax.set_xticklabels(labels, rotation=30, ha="right", fontsize=7)
        ax.set_yticks(range(len(labels)))
        ax.set_yticklabels(labels, fontsize=7)
        for i in range(values.shape[0]):
            for j in range(values.shape[1]):
                ax.text(j, i, f"{values[i, j]:.2f}", ha="center", va="center", fontsize=7)
        ax.set_title(title, fontsize=9)
    fig.colorbar(im, ax=axes.ravel().tolist(), shrink=0.8, label="Correlation")
    fig.suptitle("Factor correlation before/after orthogonalization (in-sample diagnostic)")
    fig.savefig(path, dpi=dpi, bbox_inches="tight")
    plt.close(fig)


# ---------------------------------------------------------------------------
# 表格构建
# ---------------------------------------------------------------------------
def _build_loadings_table(
    pca_full: Any, pca_winsor: Any, pca_roll: Any, pca_uncentered: Any
) -> pd.DataFrame:
    table = pd.DataFrame(index=pca_full.loadings.index)
    history = pca_roll.loadings_history
    for component in pca_full.loadings.columns:
        table[f"{component}_full"] = pca_full.loadings[component]
        table[f"{component}_winsor{WINSOR_BP:g}"] = pca_winsor.loadings[component]
        table[f"{component}_rolling_mean"] = pca_roll.loadings[component]
        if history is not None:
            table[f"{component}_rolling_std"] = history[component].std(axis=0, ddof=0)
        table[f"{component}_uncentered"] = pca_uncentered.loadings[component]
    table.index.name = "tenor"
    table.attrs["note"] = (
        f"rolling_std=载荷时序逐期限标准差；winsor 为逐期限 Δy 截尾 ±{WINSOR_BP:g}bp；"
        "uncentered 为含均值对照口径"
    )
    return table


def _build_explained_table(
    pca_full: Any, pca_winsor: Any, pca_roll: Any, pca_uncentered: Any
) -> pd.DataFrame:
    n = len(pca_full.explained_variance_ratio)
    rolling_std = (
        pca_roll.rolling_evr.std(ddof=0).to_numpy()
        if pca_roll.rolling_evr is not None
        else np.full(n, np.nan)
    )
    table = pd.DataFrame(
        {
            "component": [f"PC{i + 1}" for i in range(n)],
            "full_centered": pca_full.explained_variance_ratio,
            "full_uncentered": pca_uncentered.explained_variance_ratio,
            f"full_winsor{WINSOR_BP:g}": pca_winsor.explained_variance_ratio,
            "rolling_mean": pca_roll.explained_variance_ratio,
            "rolling_std": rolling_std,
            "winsor_bp": WINSOR_BP,
        }
    )
    table["full_centered_cumulative"] = table["full_centered"].cumsum()
    table["rolling_mean_cumulative"] = table["rolling_mean"].cumsum()
    return table


def _crosscheck_all(
    factors: pd.DataFrame, pca_full: Any, pca_winsor: Any, pca_roll: Any
) -> pd.DataFrame:
    tables = []
    for label, pca in (
        ("full_centered", pca_full),
        (f"full_winsor{WINSOR_BP:g}", pca_winsor),
        (f"rolling{ROLLING_WINDOW}", pca_roll),
    ):
        block = factor_crosscheck(factors, pca)
        block.insert(0, "pc_sample", label)
        tables.append(block)
    # R16：另一口径诊断（原始因子变化 vs 清洗后 PC），单独命名
    diagnostic = factor_crosscheck(factors, pca_winsor, use_raw_factor_changes=True)
    diagnostic.insert(0, "pc_sample", f"DIAGNOSTIC_raw_factor_changes_vs_winsor{WINSOR_BP:g}_PC")
    tables.append(diagnostic)
    combined = pd.concat(tables)
    combined.index.name = "factor"
    return combined


def _offdiag_max(matrix: pd.DataFrame) -> float:
    """所有非对角元素的最大绝对值（含 level_all–level_long 这类天然相关的对）。"""
    values = matrix.to_numpy(dtype=float)
    off = values - np.eye(values.shape[0])
    return float(np.max(np.abs(off))) if off.size else float("nan")


def _target_pair_max(
    matrix: pd.DataFrame, level_column: str, slope_column: str, curvature_column: str
) -> float:
    """只看被正交化的**目标对**（level vs slope_orth、level vs curvature_orth、
    slope_orth vs curvature_orth）的最大相关；level_all 与 level_long 天然相关，
    不属于正交化目标。"""
    labels = list(matrix.columns)
    try:
        i_level = labels.index(level_column)
        i_slope = labels.index(slope_column)
        i_curv = labels.index(curvature_column)
    except ValueError:
        return _offdiag_max(matrix)
    values = matrix.to_numpy(dtype=float)
    pairs = [(i_level, i_slope), (i_level, i_curv), (i_slope, i_curv)]
    return float(max(abs(float(values[i, j])) for i, j in pairs))


def _orthogonalization_diagnostics(
    factors: pd.DataFrame,
) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """R21：训练窗拟合 + 冻结应用，输出水平/变化两组相关诊断。"""
    orth_window = fit_orthogonalizer(factors, window=ORTH_FIT_WINDOW)
    transformed_window = orth_window.transform(factors)
    orth_full = fit_orthogonalizer(factors, window=None, fit_scope="full")
    transformed_full = orth_full.transform(factors)
    inverse = orth_window.inverse(transformed_window)
    inverse_error = float(
        np.nanmax(
            np.abs(
                inverse.loc[:, list(FACTOR_COLUMNS)].to_numpy(dtype=float)
                - factors.loc[:, list(FACTOR_COLUMNS)].to_numpy(dtype=float)
            )
        )
    )
    in_sample = transformed_window.iloc[:ORTH_FIT_WINDOW]
    out_of_sample = transformed_window.iloc[ORTH_FIT_WINDOW:]
    slope_orth = f"{orth_window.slope_column}_orth"
    curvature_orth = f"{orth_window.curvature_column}_orth"
    level_column = orth_window.level_column

    def _pair_stats(frame: pd.DataFrame) -> tuple[float, float]:
        levels = factor_correlation(frame, space="levels")
        changes = factor_correlation(frame, space="changes")
        return (
            _target_pair_max(levels, level_column, slope_orth, curvature_orth),
            _target_pair_max(changes, level_column, slope_orth, curvature_orth),
        )

    in_levels, in_changes = _pair_stats(in_sample)
    oos_levels, oos_changes = _pair_stats(out_of_sample)
    full_levels, full_changes = _pair_stats(transformed_full)
    rows = [
        {
            "scope": "in_sample_window",
            "n_obs": len(in_sample),
            "levels_max_abs_corr": in_levels,
            "changes_max_abs_corr": in_changes,
            "inverse_max_abs_error": inverse_error,
            "note": "训练窗内水平正交（代数恒等）；变化空间不保证为 0",
        },
        {
            "scope": "out_of_sample_frozen",
            "n_obs": len(out_of_sample),
            "levels_max_abs_corr": oos_levels,
            "changes_max_abs_corr": oos_changes,
            "inverse_max_abs_error": inverse_error,
            "note": "冻结训练窗系数后应用到样本外；水平相关不再为 0",
        },
        {
            "scope": "full_sample_in_sample_diagnostic",
            "n_obs": len(transformed_full),
            "levels_max_abs_corr": full_levels,
            "changes_max_abs_corr": full_changes,
            "inverse_max_abs_error": inverse_error,
            "note": "全样本拟合仅作样本内诊断，不等于风险正交",
        },
    ]
    diagnostics = pd.DataFrame(rows)
    diagnostics.attrs["fit_window"] = ORTH_FIT_WINDOW
    diagnostics.attrs["orthogonalizer"] = orth_window.describe()
    return diagnostics, transformed_window, transformed_full


def _correlation_long(
    factors: pd.DataFrame,
    orth_window: pd.DataFrame,
    orth_full: pd.DataFrame,
) -> pd.DataFrame:
    scopes = [
        ("levels_raw", factor_correlation(factors, space="levels")),
        ("changes_raw", factor_correlation(factors, space="changes")),
        (f"levels_window{ORTH_FIT_WINDOW}_fit", factor_correlation(orth_window, space="levels")),
        (f"changes_window{ORTH_FIT_WINDOW}_fit", factor_correlation(orth_window, space="changes")),
        ("levels_full_fit_in_sample", factor_correlation(orth_full, space="levels")),
        ("changes_full_fit_in_sample", factor_correlation(orth_full, space="changes")),
    ]
    rows: list[pd.DataFrame] = []
    for scope, matrix in scopes:
        block = matrix.stack().rename("corr").reset_index()
        block.columns = ["factor_a", "factor_b", "corr"]
        block.insert(0, "scope", scope)
        rows.append(block)
    return pd.concat(rows, ignore_index=True)


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def build_reports(
    *,
    cfg: Config | None = None,
    panel: pd.DataFrame | None = None,
    start: str | pd.Timestamp | None = None,
    end: str | pd.Timestamp | None = None,
    out_dir: str | Path | None = None,
) -> dict[str, Path]:
    """生成曲线层全部报告产物，返回 ``{artifact: path}``。

    Parameters
    ----------
    cfg : Config | None
        配置；``None`` 时才读默认配置。**传入后不再调用 get_config()**。
    panel : DataFrame | None
        收益率宽表；``None`` 时按 ``cfg`` 加载并截断到 [start, end]。
    start, end : 可选日期边界
        同时作用于即期代表日、PCA/因子/正交化样本（R13）。
    out_dir : str | Path | None
        输出根目录；默认 ``cfg.paths.abs("reports")``。所有产物只写这里。

    Returns
    -------
    dict[str, Path]
        产物键 → 绝对路径（含 ``manifest``）。
    """
    resolved = cfg if cfg is not None else get_config()
    directory = Path(out_dir) if out_dir is not None else resolved.paths.abs("reports")
    figures = directory / "figures"
    figures.mkdir(parents=True, exist_ok=True)
    dpi = int(resolved.report.dpi)
    frame = (
        load_panel(resolved, start=start, end=end)
        if panel is None
        else _truncate(panel, start, end)
    )
    panel_start = frame.index.min()
    panel_end = frame.index.max()
    log.info(
        "报告样本区间: %s→%s（%d 天）；out_dir=%s",
        panel_start.date(),
        panel_end.date(),
        len(frame),
        directory,
    )

    artifacts: dict[str, Path] = {}

    # ---- 1) 即期曲线对比 ----
    comparison = build_spot_comparison(frame)
    spot_csv = directory / "spot_curve_compare.csv"
    comparison.to_csv(spot_csv, index=False, encoding="utf-8-sig")
    plot_spot_vs_ytm(comparison, figures / "spot_vs_ytm.png", dpi)
    artifacts["spot_curve_compare"] = spot_csv

    # ---- 2) 因子与 PCA（共享 processed_changes，R16）----
    factors = observable_factors(frame)
    factor_definition = factor_definition_table()
    factor_definition["panel_start"] = panel_start.date().isoformat()
    factor_definition["panel_end"] = panel_end.date().isoformat()
    factor_definition_path = directory / "factor_definition_table.csv"
    factor_definition.to_csv(factor_definition_path, index=False, encoding="utf-8-sig")
    artifacts["factor_definition_table"] = factor_definition_path

    pca_full = pca_factors(frame)
    pca_winsor = pca_factors(frame, winsor_bp=WINSOR_BP)
    pca_roll = pca_factors(frame, window=ROLLING_WINDOW)
    pca_uncentered = pca_factors(frame, center=False)
    stability = loading_stability(pca_roll)

    loadings = _build_loadings_table(pca_full, pca_winsor, pca_roll, pca_uncentered)
    loadings_path = directory / "pca_loadings.csv"
    loadings.to_csv(loadings_path, encoding="utf-8-sig")
    explained = _build_explained_table(pca_full, pca_winsor, pca_roll, pca_uncentered)
    explained["panel_start"] = panel_start.date().isoformat()
    explained["panel_end"] = panel_end.date().isoformat()
    explained_path = directory / "pca_explained.csv"
    explained.to_csv(explained_path, index=False, encoding="utf-8-sig")
    artifacts["pca_loadings"] = loadings_path
    artifacts["pca_explained"] = explained_path

    crosscheck = _crosscheck_all(factors, pca_full, pca_winsor, pca_roll)
    crosscheck["panel_start"] = panel_start.date().isoformat()
    crosscheck["panel_end"] = panel_end.date().isoformat()
    crosscheck_path = directory / "factor_crosscheck.csv"
    crosscheck.to_csv(crosscheck_path, encoding="utf-8-sig")
    artifacts["factor_crosscheck"] = crosscheck_path

    # ---- 3) 正交化诊断（R21）----
    diagnostics, orth_window, orth_full = _orthogonalization_diagnostics(factors)
    diagnostics["panel_start"] = panel_start.date().isoformat()
    diagnostics["panel_end"] = panel_end.date().isoformat()
    diagnostics_path = directory / "orthogonalization_diagnostics.csv"
    diagnostics.to_csv(diagnostics_path, index=False, encoding="utf-8-sig")
    artifacts["orthogonalization_diagnostics"] = diagnostics_path

    correlation_long = _correlation_long(factors, orth_window, orth_full)
    correlation_path = directory / "factor_correlation.csv"
    correlation_long.to_csv(correlation_path, index=False, encoding="utf-8-sig")
    artifacts["factor_correlation"] = correlation_path

    plot_pca_loadings(pca_full, pca_winsor, pca_roll, stability, figures / "pca_loadings.png", dpi)
    plot_factor_corr_matrix(
        factors, orth_window, orth_full, figures / "factor_corr_heatmap.png", dpi
    )
    for key in ("spot_vs_ytm", "pca_loadings", "factor_corr_heatmap"):
        artifacts[key] = figures / f"{key}.png"

    # ---- 4) manifest（记录实际样本区间/配置指纹，供下游审计）----
    manifest = pd.DataFrame(
        [
            {
                "artifact": key,
                "path": str(path),
                "panel_start": panel_start.date().isoformat(),
                "panel_end": panel_end.date().isoformat(),
                "n_days": len(frame),
                "config_hash": resolved.hash(),
                "generated_at": datetime.now(UTC).isoformat(),
                "note": "fixed representative dates"
                if key == "spot_curve_compare"
                else "sample-bounded",
            }
            for key, path in artifacts.items()
        ]
    )
    manifest_path = directory / "curve_reports_manifest.csv"
    manifest.to_csv(manifest_path, index=False, encoding="utf-8-sig")
    artifacts["manifest"] = manifest_path
    log.info("报告产物 %d 个，manifest: %s", len(artifacts), manifest_path)

    print("\n===== 即期 vs 到期（代表性日期）=====")
    for day, block in comparison.groupby("date"):
        print(
            f"{pd.Timestamp(day).date()}: "
            + ", ".join(
                f"{label}: ytm={ytm:.2f}%/zero={zero:.2f}%"
                for label, ytm, zero in zip(
                    block["tenor_label"], block["ytm_pct"], block["zero_pct"], strict=True
                )
            )
        )
    print("\n===== PCA 解释方差（Δy bp）=====")
    print(explained.round(4).to_string(index=False))
    for label in ("full_centered", f"full_winsor{WINSOR_BP:g}", f"rolling{ROLLING_WINDOW}"):
        block = crosscheck[crosscheck["pc_sample"] == label].drop(columns=["pc_sample"])
        print(f"\n===== 因子交叉校验（{label}）=====")
        print(block.round(3).to_string())
    print("\n===== 正交化诊断（R21）=====")
    print(diagnostics.round(4).to_string(index=False))
    print(f"\n产物目录: {directory}")
    return artifacts


def main(argv: list[str] | None = None) -> int:
    """CLI 包装：只解析参数后调用 :func:`build_reports`。"""
    parser = argparse.ArgumentParser(description="曲线层报告：即期曲线 / PCA / 因子 / 正交化")
    parser.add_argument("--config", default=None, help="配置文件路径（默认 config/config.yaml）")
    parser.add_argument("--start", default=None, help="样本起始日（含），YYYY-MM-DD")
    parser.add_argument("--end", default=None, help="样本截止日（含），YYYY-MM-DD")
    parser.add_argument("--out-dir", default=None, help="输出根目录（默认 cfg.paths.reports）")
    args = parser.parse_args(argv)
    cfg = load_config(args.config) if args.config else get_config()
    artifacts = build_reports(cfg=cfg, start=args.start, end=args.end, out_dir=args.out_dir)
    for key, path in artifacts.items():
        log.info("产物 %-32s %s", key, path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
