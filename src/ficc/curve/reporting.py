"""本轮报告生成：即期曲线对比、PCA/因子交叉校验、因子相关矩阵。

统一入口（产物写入 ``reports/`` / ``reports/figures/``）::

    PYTHONPATH=src .venv/bin/python -m ficc.curve.reporting

产物
----
- ``reports/spot_curve_compare.csv``：三个代表性日期的 YTM vs bootstrap 即期曲线；
- ``reports/pca_loadings.csv``：全样本/稳健截尾/滚动均值/滚动的标准差/含均值版载荷；
- ``reports/pca_explained.csv``：各口径解释方差占比与滚动稳定性；
- ``reports/factor_crosscheck.csv``：可观测因子 Δ 与 PC1–3 的相关系数、R²、方向一致性；
- ``reports/factor_correlation.csv``：水平/变化、正交化前后因子相关矩阵（长表）；
- ``reports/figures/spot_vs_ytm.png``、``pca_loadings.png``、``factor_corr_heatmap.png``。

口径说明
--------
- 即期曲线：平价债 bootstrap，合成假设见 :mod:`ficc.curve.bootstrap`（**不是**官方即期曲线）；
- PCA 样本：收益率**变化** Δy（bp）；``center=True`` 为默认口径（只解释日变动），
  ``center=False`` 作为"含均值"对照；``winsor_bp=10`` 为剔除 2002–2009 异常跳变后的
  稳健性对照（属于数据清洗，不改变因子定义）；
- 滚动 PCA：``window=250`` 个观测、走前（不含当日）估载荷再投影当日 Δy；
- 可观测因子定义不变（level=10/15/20/30Y 均值、slope=10Y−2Y、
  curvature=2×5Y−2Y−10Y）；交叉校验里额外加入 ``level_all`` 仅作为**诊断性对照**
  （全期限均值），用于解释"为什么 level↔PC1 不达标"。
"""

from __future__ import annotations

import os
import sys
import tempfile
from pathlib import Path

os.environ.setdefault("MPLCONFIGDIR", tempfile.mkdtemp(prefix="mpl-"))
import matplotlib

matplotlib.use("Agg")

_ROOT = Path(__file__).resolve().parents[2]
if str(_ROOT / "src") not in sys.path:
    sys.path.insert(0, str(_ROOT / "src"))

import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from ficc.config import get_config  # noqa: E402
from ficc.curve.bootstrap import bootstrap_spot  # noqa: E402
from ficc.curve.curve import Curve  # noqa: E402
from ficc.curve.factors import (  # noqa: E402
    PCAResult,
    factor_correlation,
    factor_crosscheck,
    loading_stability,
    observable_factors,
    orthogonalize,
    pca_factors,
)
from ficc.data.schema import CANONICAL_TENORS, TENOR_LABELS  # noqa: E402
from ficc.logging import get_logger  # noqa: E402

__all__ = ["build_spot_comparison", "load_panel", "main"]

log = get_logger("curve_reporting")

SPOT_DATES: tuple[str, ...] = ("2024-06-28", "2013-06-20", "2026-11-05")
ROLLING_WINDOW: int = 250
WINSOR_BP: float = 10.0


# ---------------------------------------------------------------------------
# 数据
# ---------------------------------------------------------------------------
def load_panel() -> pd.DataFrame:
    """读取收益率宽表（优先 parquet，回退 Phase 0 实测 CSV），只保留标准期限。"""
    cfg = get_config()
    processed = cfg.paths.abs("processed") / "curve_long.parquet"
    if processed.exists():
        long_df = pd.read_parquet(processed)
        long_df = long_df.rename(columns={"tenor_years": "tenor"})
        source = str(processed)
    else:
        raw = _ROOT / "data" / "raw_spike" / "cb_gzqx_standard_terms_2002_2026.csv"
        if not raw.exists():
            raise FileNotFoundError(f"找不到曲线数据：{processed} 与 {raw} 均不存在")
        long_df = pd.read_csv(raw).rename(columns={"term_years": "tenor", "yield_pct": "ytm_pct"})
        source = str(raw)
    long_df = long_df[["date", "tenor", "ytm_pct"]].copy()
    long_df["date"] = pd.to_datetime(long_df["date"], errors="raise")
    long_df["tenor"] = pd.to_numeric(long_df["tenor"], errors="raise")
    long_df["ytm_pct"] = pd.to_numeric(long_df["ytm_pct"], errors="raise")
    long_df = long_df[long_df["tenor"].isin(CANONICAL_TENORS)]
    long_df = long_df.drop_duplicates(subset=["date", "tenor"], keep="first")
    panel = long_df.pivot(index="date", columns="tenor", values="ytm_pct").sort_index()
    panel = panel.reindex(columns=list(CANONICAL_TENORS))
    panel.columns = pd.Index(panel.columns.to_numpy(dtype=float), name="tenor")
    log.info("数据来源: %s；面板 %d 天 × %d 期限", source, *panel.shape)
    return panel


def _tenor_label(tenor: float) -> str:
    return TENOR_LABELS.get(float(tenor), f"{float(tenor):g}Y")


# ---------------------------------------------------------------------------
# 即期曲线对比
# ---------------------------------------------------------------------------
def build_spot_comparison(panel: pd.DataFrame) -> pd.DataFrame:
    """对 ``SPOT_DATES`` 逐日 bootstrap，返回 YTM vs 即期对比长表。"""
    rows: list[pd.DataFrame] = []
    for day in SPOT_DATES:
        stamp = pd.Timestamp(day)
        if stamp not in panel.index:
            log.warning("面板缺少 %s，跳过", day)
            continue
        row = panel.loc[stamp].dropna()
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
        frame["source_curve_method"] = "linear"
        frame["synthetic_assumption"] = "par-bond bootstrap (not official spot curve)"
        rows.append(frame)
    if not rows:
        raise ValueError("没有任何代表性日期可用于即期曲线对比")
    return pd.concat(rows, ignore_index=True)


def plot_spot_vs_ytm(comparison: pd.DataFrame, path: Path, dpi: int) -> None:
    """三个日期的 YTM vs 即期曲线（英文标签）。"""
    dates = list(comparison["date"].drop_duplicates())
    fig, axes = plt.subplots(1, len(dates), figsize=(5.2 * len(dates), 4.4), squeeze=False)
    axes_flat = axes.ravel()
    for ax, day in zip(axes_flat, dates, strict=False):
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


# ---------------------------------------------------------------------------
# PCA 与因子
# ---------------------------------------------------------------------------
def _build_loadings_table(
    pca_full: PCAResult,
    pca_winsor: PCAResult,
    pca_roll: PCAResult,
    pca_uncentered: PCAResult,
    stability: pd.DataFrame,
) -> pd.DataFrame:
    """把各口径载荷拼成一张表（index=tenor）。"""
    table = pd.DataFrame(index=pca_full.loadings.index)
    history = pca_roll.loadings_history
    for component in pca_full.loadings.columns:
        table[f"{component}_full"] = pca_full.loadings[component]
        table[f"{component}_winsor{WINSOR_BP:g}"] = pca_winsor.loadings[component]
        table[f"{component}_rolling_mean"] = pca_roll.loadings[component]
        if history is not None:
            table[f"{component}_rolling_std"] = history[component].std(axis=0, ddof=0)
        table[f"{component}_uncentered"] = pca_uncentered.loadings[component]
    table.attrs["note"] = (
        f"rolling_std = 载荷时间序列的逐期限标准差；stability_evr 见 pca_explained.csv；"
        f"full_winsor 为 Δy 对称截尾 ±{WINSOR_BP:g}bp 的稳健性对照"
    )
    table.index.name = "tenor"
    return table


def _build_explained_table(
    pca_full: PCAResult,
    pca_winsor: PCAResult,
    pca_roll: PCAResult,
    pca_uncentered: PCAResult,
) -> pd.DataFrame:
    """各口径的解释方差占比（含滚动均值/标准差、累计）。"""
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
        }
    )
    table["full_centered_cumulative"] = table["full_centered"].cumsum()
    table["rolling_mean_cumulative"] = table["rolling_mean"].cumsum()
    return table


def _crosscheck_all(
    factors: pd.DataFrame, pca_full: PCAResult, pca_winsor: PCAResult, pca_roll: PCAResult
) -> pd.DataFrame:
    """三个口径的交叉校验拼成一张长表（含诊断性 level_all）。"""
    tables = []
    for label, pca in (
        ("full_centered", pca_full),
        (f"full_winsor{WINSOR_BP:g}", pca_winsor),
        (f"rolling{ROLLING_WINDOW}", pca_roll),
    ):
        table = factor_crosscheck(factors, pca)
        table.insert(0, "pc_sample", label)
        tables.append(table)
    combined = pd.concat(tables)
    combined.index.name = "factor"
    return combined


def _correlation_long(factors: pd.DataFrame, factors_orth: pd.DataFrame) -> pd.DataFrame:
    """水平/变化、正交化前后的相关矩阵（长表）。"""
    scopes = [
        ("levels_raw", factor_correlation(factors, space="levels")),
        ("changes_raw", factor_correlation(factors, space="changes")),
        ("levels_orthogonalized", factor_correlation(factors_orth, space="levels")),
        ("changes_orthogonalized", factor_correlation(factors_orth, space="changes")),
    ]
    rows: list[pd.DataFrame] = []
    for scope, matrix in scopes:
        block = matrix.stack().rename("corr").reset_index()
        block.columns = ["factor_a", "factor_b", "corr"]
        block.insert(0, "scope", scope)
        rows.append(block)
    return pd.concat(rows, ignore_index=True)


def plot_pca_loadings(
    pca_full: PCAResult,
    pca_winsor: PCAResult,
    pca_roll: PCAResult,
    stability: pd.DataFrame,
    path: Path,
    dpi: int,
) -> None:
    """各主成分载荷：全样本 vs 稳健截尾 vs 滚动均值 ± 标准差。"""
    components = list(pca_full.loadings.columns)
    fig, axes = plt.subplots(
        1, len(components), figsize=(5.0 * len(components), 4.4), squeeze=False
    )
    axes_flat = axes.ravel()
    tenors = pca_full.loadings.index.to_numpy(dtype=float)
    for ax, component in zip(axes_flat, components, strict=False):
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
        history = pca_roll.loadings_history
        std = (
            history[component].std(axis=0, ddof=0).to_numpy(dtype=float)
            if history is not None
            else np.full(tenors.size, float(stability.loc[component, "loading_std_mean"]))
        )
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
    axes_flat[0].set_ylabel("Loading")
    fig.tight_layout()
    fig.savefig(path, dpi=dpi)
    plt.close(fig)


def plot_factor_corr_matrix(
    factors: pd.DataFrame, factors_orth: pd.DataFrame, path: Path, dpi: int
) -> None:
    """正交化前后（水平/变化）的因子相关热力图。"""
    matrices = [
        ("levels before", factor_correlation(factors, space="levels")),
        ("levels after orth.", factor_correlation(factors_orth, space="levels")),
        ("changes before", factor_correlation(factors, space="changes")),
        ("changes after orth.", factor_correlation(factors_orth, space="changes")),
    ]
    fig, axes = plt.subplots(1, len(matrices), figsize=(4.6 * len(matrices), 4.2), squeeze=False)
    for ax, (title, matrix) in zip(axes.ravel(), matrices, strict=False):
        values = matrix.to_numpy(dtype=float)
        im = ax.imshow(values, cmap="RdBu_r", vmin=-1.0, vmax=1.0)
        labels = [str(c) for c in matrix.columns]
        ax.set_xticks(range(len(labels)))
        ax.set_xticklabels(labels, rotation=30, ha="right", fontsize=8)
        ax.set_yticks(range(len(labels)))
        ax.set_yticklabels(labels, fontsize=8)
        for i in range(values.shape[0]):
            for j in range(values.shape[1]):
                ax.text(j, i, f"{values[i, j]:.2f}", ha="center", va="center", fontsize=8)
        ax.set_title(title, fontsize=9)
    fig.colorbar(im, ax=axes.ravel().tolist(), shrink=0.8, label="Correlation")
    fig.suptitle("Factor correlation before/after orthogonalization")
    fig.savefig(path, dpi=dpi, bbox_inches="tight")
    plt.close(fig)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main() -> int:
    cfg = get_config()
    reports = cfg.paths.abs("reports")
    figures = reports / "figures"
    figures.mkdir(parents=True, exist_ok=True)
    dpi = int(cfg.report.dpi)

    panel = load_panel()

    # ---- 1) 即期曲线对比 ----
    comparison = build_spot_comparison(panel)
    spot_csv = reports / "spot_curve_compare.csv"
    comparison.to_csv(spot_csv, index=False, encoding="utf-8-sig")
    plot_spot_vs_ytm(comparison, figures / "spot_vs_ytm.png", dpi)
    log.info("即期曲线对比: %s", spot_csv)

    # ---- 2) PCA（全样本 / 稳健截尾 / 滚动 / 含均值对照）----
    pca_full = pca_factors(panel)
    pca_winsor = pca_factors(panel, winsor_bp=WINSOR_BP)
    pca_roll = pca_factors(panel, window=ROLLING_WINDOW)
    pca_uncentered = pca_factors(panel, center=False)
    stability = loading_stability(pca_roll)

    loadings_table = _build_loadings_table(
        pca_full, pca_winsor, pca_roll, pca_uncentered, stability
    )
    loadings_csv = reports / "pca_loadings.csv"
    loadings_table.to_csv(loadings_csv, encoding="utf-8-sig")

    explained_table = _build_explained_table(pca_full, pca_winsor, pca_roll, pca_uncentered)
    explained_csv = reports / "pca_explained.csv"
    explained_table.to_csv(explained_csv, index=False, encoding="utf-8-sig")
    log.info("PCA 载荷/解释方差: %s / %s", loadings_csv, explained_csv)

    # ---- 3) 因子交叉校验与相关矩阵 ----
    factors = observable_factors(panel)
    factors_diag = factors.copy()
    factors_diag["level_all"] = panel.mean(axis=1)  # 诊断性对照（不替代 level 定义）
    factors_diag.attrs = dict(factors.attrs)
    crosscheck = _crosscheck_all(factors_diag, pca_full, pca_winsor, pca_roll)
    crosscheck["note"] = [
        "diagnostic: mean of all canonical tenors" if name == "level_all" else "primary definition"
        for name in crosscheck.index
    ]
    crosscheck_csv = reports / "factor_crosscheck.csv"
    crosscheck.to_csv(crosscheck_csv, encoding="utf-8-sig")

    factors_orth = orthogonalize(factors)
    correlation_long = _correlation_long(factors, factors_orth)
    correlation_csv = reports / "factor_correlation.csv"
    correlation_long.to_csv(correlation_csv, index=False, encoding="utf-8-sig")
    log.info("因子交叉校验/相关矩阵: %s / %s", crosscheck_csv, correlation_csv)

    # ---- 4) 图 ----
    plot_pca_loadings(pca_full, pca_winsor, pca_roll, stability, figures / "pca_loadings.png", dpi)
    plot_factor_corr_matrix(factors, factors_orth, figures / "factor_corr_heatmap.png", dpi)

    # ---- 5) 控制台摘要 ----
    print("\n===== 即期 vs 到期（代表性日期）=====")
    for day, block in comparison.groupby("date"):
        parts = ", ".join(
            f"{label}: ytm={ytm:.2f}%/zero={zero:.2f}%"
            for label, ytm, zero in zip(
                block["tenor_label"], block["ytm_pct"], block["zero_pct"], strict=True
            )
        )
        print(f"{pd.Timestamp(day).date()}: {parts}")
    print("\n===== PCA 解释方差（Δy bp）=====")
    print(explained_table.round(4).to_string(index=False))
    for label in ("full_centered", f"full_winsor{WINSOR_BP:g}", f"rolling{ROLLING_WINDOW}"):
        block = crosscheck[crosscheck["pc_sample"] == label].drop(columns=["pc_sample"])
        print(f"\n===== 交叉校验（{label}）=====")
        print(block.round(3).to_string())
    print("\n===== 因子相关（水平，正交化后）=====")
    print(factor_correlation(factors_orth, space="levels").round(4).to_string())
    print(
        f"\n产物: {spot_csv}\n      {loadings_csv}\n      {explained_csv}"
        f"\n      {crosscheck_csv}\n      {correlation_csv}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
