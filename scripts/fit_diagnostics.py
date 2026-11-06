#!/usr/bin/env python3
"""全样本 NS/NSS 拟合诊断：自由拟合 vs 锚定拟合分别落盘 + 残差/参数/因子诊断。

用法
----
    PYTHONPATH=src python3 scripts/fit_diagnostics.py

口径（R01/R02/R07 评审后的硬约定）
----------------------------------
1. **自由拟合与锚定拟合必须分开验收**，不能用一个数字背书另一个：

   - **自由拟合（free）**：τ 与系数全放开、任务口径边界、**精确有界最小二乘**。
     它的残差衡量 NS/NSS 的**表达能力**（全样本 NSS RMSE 中位约 3.3bp）。
   - **锚定拟合（anchored）**：``b0`` 用长端观测锚定、``b1/b2/b3∈[-15,15]``。
     参数连续可解释，但再拟合误差明显更大（NSS RMSE 中位约 4.76bp）。

   两套参数、拟合值、残差、误差指标**分别写出**（``*_free.csv`` / ``*_anchored.csv``
   与 ``curve_fit_summary.csv`` 的 ``fit_mode`` 列）。
2. **权重显式传配置口径** ``cfg.curve.weights``（R07），实际使用的权重模式写进
   所有 CSV 的 ``weights_mode`` 列与图标题，避免"配置说短端加权、产物其实是等权"。
3. **最差拟合日不剔除**：R01 修复前，最差日残差里混入了"系数裁剪不是有界最小二乘"
   的求解误差（2002-04-26 156.0 → 93.8bp、2004-08-17 76.7 → 52.9bp）。修复后仍
   偏大的日子要逐案判断（数据异常/样本券/模型局限），**不能笼统归因于"数学上
   无法表达"**。清单见 ``reports/curve_fit_bad_dates.csv``。
4. **参数因子不能用**（R02）：锚定只固定 b0，b1/b2/b3/τ 仍会换解（2023-10-12 各
   节点收益率只动 0.04pp，b1 却跳 147bp）。本脚本额外输出**可观测因子**
   （level/slope/curvature）及其跳变敏感性，作为推荐口径。

产物
----
``reports/``：

- ``curve_fit_summary.csv``：method × fit_mode × scope(overall/tenor) 的指标长表；
- ``curve_fit_params_{method}_{mode}.csv``：参数面板（含两套误差与 unstable 标记）；
- ``curve_fit_residuals_{method}_{mode}.csv``：残差面板（bp）；
- ``curve_fit_fitted_{method}_{mode}.csv``：拟合收益率面板（百分数）；
- ``curve_fit_bad_dates.csv``：自由 NSS ``rmse_bp > 5`` 的最差拟合日清单；
- ``curve_factors.csv`` / ``curve_factor_jump_sensitivity.csv``：可观测因子与敏感性。

``reports/figures/``：

- ``curve_residual_hist.png``：自由拟合残差分布（NS vs NSS）；
- ``curve_residual_hist_anchored.png``：锚定拟合残差分布（NS vs NSS）；
- ``curve_residual_by_tenor.png``：自由拟合分期限残差箱线图；
- ``curve_residual_heatmap.png``：自由 NSS 残差时间×期限热力图；
- ``curve_params_timeseries.png``：锚定 NSS 参数（主因子产品）；
- ``curve_params_timeseries_free.png``：自由 NSS 参数（展示识别性问题导致的跳变）；
- ``curve_rmse_free_vs_anchored.png``：两种口径的逐日 RMSE 对比。
"""

from __future__ import annotations

import os
import sys
import tempfile
import time
from pathlib import Path

# matplotlib 需要可写的缓存目录（沙箱/服务器上 ~/.matplotlib 可能不可写）
os.environ.setdefault("MPLCONFIGDIR", tempfile.mkdtemp(prefix="mpl-"))
import matplotlib

matplotlib.use("Agg")

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from ficc.config import get_config  # noqa: E402
from ficc.curve.factors import factor_jump_sensitivity, observable_factors  # noqa: E402
from ficc.curve.fitting import fit_panel  # noqa: E402
from ficc.data.schema import CANONICAL_TENORS, TENOR_LABELS  # noqa: E402
from ficc.logging import get_logger  # noqa: E402

log = get_logger("fit_diagnostics")

RMSE_BAD_THRESHOLD_BP: float = 5.0
_METHODS: tuple[str, ...] = ("ns", "nss")
# (fit_mode, param_mode, residuals_mode, 说明)
_FIT_MODES: tuple[tuple[str, str, str], ...] = (
    ("free", "free", "free"),
    ("anchored", "stable", "anchored"),
)


# ---------------------------------------------------------------------------
# 数据加载
# ---------------------------------------------------------------------------
def load_panel(cfg) -> tuple[pd.DataFrame, str]:
    """加载宽表面板（index=date, columns=tenor, values=ytm_pct）并说明来源。"""
    processed = cfg.paths.abs("processed") / "curve_long.parquet"
    if processed.exists():
        long_df = pd.read_parquet(processed)
        source = f"data/processed/curve_long.parquet ({len(long_df)} 行)"
        long_df = long_df.rename(columns={"tenor_years": "tenor"})
    else:
        raw = cfg.paths.abs("raw") / "raw_spike" / "cb_gzqx_standard_terms_2002_2026.csv"
        if not raw.exists():
            raw = _ROOT / "data" / "raw_spike" / "cb_gzqx_standard_terms_2002_2026.csv"
        if not raw.exists():
            raise FileNotFoundError(
                "找不到曲线数据：请先跑 scripts/fetch_data.py，或确认 "
                "data/raw_spike/cb_gzqx_standard_terms_2002_2026.csv 存在"
            )
        raw_df = pd.read_csv(raw)
        long_df = raw_df.rename(columns={"term_years": "tenor", "yield_pct": "ytm_pct"})
        source = "data/raw_spike/cb_gzqx_standard_terms_2002_2026.csv（回退，Phase 0 实测）"
    log.info("数据来源: %s", source)

    long_df = long_df[["date", "tenor", "ytm_pct"]].copy()
    long_df["date"] = pd.to_datetime(long_df["date"], errors="raise")
    long_df["tenor"] = pd.to_numeric(long_df["tenor"], errors="raise")
    long_df["ytm_pct"] = pd.to_numeric(long_df["ytm_pct"], errors="raise")
    long_df = long_df[long_df["tenor"].isin(CANONICAL_TENORS)]
    # 官方年度文件跨年重叠：数值相同则去重，数值不同是真冲突必须报错
    dup = long_df.duplicated(subset=["date", "tenor"], keep=False)
    if dup.any():
        conflict = long_df[dup].groupby(["date", "tenor"])["ytm_pct"].nunique()
        conflict = conflict[conflict > 1]
        if len(conflict) > 0:
            raise ValueError(f"发现 {len(conflict)} 组数值冲突的重复记录，需人工核查")
        long_df = long_df.drop_duplicates(subset=["date", "tenor"], keep="first")
        log.info("已去重 %d 行跨年重叠记录（数值一致）", int(dup.sum()))

    panel = long_df.pivot(index="date", columns="tenor", values="ytm_pct").sort_index()
    panel = panel.reindex(columns=list(CANONICAL_TENORS))
    panel.columns = pd.Index(panel.columns.to_numpy(dtype=float), name="tenor")
    panel = panel.sort_index()
    log.info(
        "面板就绪: %d 个交易日, %s → %s, %d 个期限, 缺失 %.2f%%",
        len(panel),
        panel.index.min().date(),
        panel.index.max().date(),
        panel.shape[1],
        float(panel.isna().to_numpy().mean() * 100.0),
    )
    return panel, source


def _fit_input(panel: pd.DataFrame) -> pd.DataFrame:
    return panel.dropna(thresh=int(get_config().curve.min_valid_tenors))


# ---------------------------------------------------------------------------
# 统计
# ---------------------------------------------------------------------------
def residual_table(residuals: pd.DataFrame) -> pd.DataFrame:
    """残差按期限汇总（单位 bp）。"""
    long = residuals.stack().rename("residual_bp").reset_index()
    long.columns = ["date", "tenor", "residual_bp"]
    long = long.dropna(subset=["residual_bp"])
    grouped = long.groupby("tenor")["residual_bp"]
    out = pd.DataFrame(
        {
            "n": grouped.size().astype(int),
            "mean_bp": grouped.mean(),
            "std_bp": grouped.std(ddof=0),
            "median_bp": grouped.median(),
            "mae_bp": grouped.apply(lambda s: float(np.abs(s).mean())),
            "p95_abs_bp": grouped.apply(lambda s: float(np.abs(s).quantile(0.95))),
            "max_abs_bp": grouped.apply(lambda s: float(np.abs(s).max())),
        }
    )
    out.index.name = "tenor"
    return out


def overall_stats(params: pd.DataFrame, residuals: pd.DataFrame) -> dict[str, float]:
    """总体统计：RMSE 分布、残差分位、MAE、失败/超阈天数、参数跳变。"""
    rmse = params["rmse_bp"].dropna().to_numpy()
    flat = residuals.to_numpy(dtype=float).ravel()
    flat = flat[np.isfinite(flat)]
    b0 = params["b0"].to_numpy(dtype=float)
    jump = np.abs(np.diff(b0))
    jump = jump[np.isfinite(jump)]
    return {
        "n_days": float(len(params)),
        "n_obs": float(flat.size),
        "rmse_mean_bp": float(np.mean(rmse)),
        "rmse_median_bp": float(np.median(rmse)),
        "rmse_p95_bp": float(np.percentile(rmse, 95)),
        "rmse_max_bp": float(np.max(rmse)),
        "resid_mean_bp": float(np.mean(flat)),
        "resid_std_bp": float(np.std(flat)),
        "mae_bp": float(np.mean(np.abs(flat))),
        "p95_abs_bp": float(np.percentile(np.abs(flat), 95)),
        "n_over_5bp": float(np.sum(rmse > RMSE_BAD_THRESHOLD_BP)),
        "n_fail": float((~params["success"].astype(bool)).sum()),
        "n_unstable": float(params["unstable"].sum()),
        "b0_in_range_pct": float(np.mean((b0 >= 0.5) & (b0 <= 8.0)) * 100.0),
        "b0_jump_p99_pp": float(np.percentile(jump, 99)) if jump.size else float("nan"),
        "b0_jump_max_pp": float(np.max(jump)) if jump.size else float("nan"),
    }


def bad_dates(params: pd.DataFrame) -> pd.DataFrame:
    """自由拟合 rmse 超阈的日期清单（含参数与标记）。"""
    cols = [
        "rmse_bp",
        "mae_bp",
        "max_abs_bp",
        "n_points",
        "b0",
        "b1",
        "b2",
        "b0_free",
        "success",
        "unstable",
        "unstable_reason",
    ]
    cols = [c for c in cols if c in params.columns]
    out = params.loc[params["rmse_bp"] > RMSE_BAD_THRESHOLD_BP, cols].copy()
    out = out.sort_values("rmse_bp", ascending=False)
    out.index.name = "date"
    return out


# ---------------------------------------------------------------------------
# 图
# ---------------------------------------------------------------------------
def _tenor_labels(tenors) -> list[str]:
    return [TENOR_LABELS.get(float(t), f"{float(t):g}Y") for t in tenors]


def plot_residual_hist(
    residuals: dict[str, pd.DataFrame], path: Path, dpi: int, *, title: str
) -> None:
    """残差分布直方图（NS vs NSS 叠加）。"""
    fig, ax = plt.subplots(figsize=(8, 5))
    colors = {"ns": "#1f77b4", "nss": "#d62728"}
    bins = np.linspace(-30, 30, 61)
    for method, frame in residuals.items():
        flat = frame.to_numpy(dtype=float).ravel()
        flat = flat[np.isfinite(flat)]
        ax.hist(
            np.clip(flat, -29.9, 29.9),
            bins=bins,
            density=True,
            alpha=0.55,
            label=f"{method.upper()} (n={flat.size:,})",
            color=colors[method],
        )
    ax.axvline(0.0, color="black", lw=0.8, ls="--")
    ax.set_xlabel("Residual (bp): fitted - observed")
    ax.set_ylabel("Density")
    ax.set_title(title)
    ax.legend(loc="upper right")
    ax.grid(alpha=0.25)
    fig.tight_layout()
    fig.savefig(path, dpi=dpi)
    plt.close(fig)


def plot_residual_by_tenor(
    residuals: dict[str, pd.DataFrame], path: Path, dpi: int, *, title: str
) -> None:
    """残差随期限的箱线图（NS / NSS 两栏）。"""
    methods = list(residuals)
    fig, axes = plt.subplots(1, len(methods), figsize=(6.5 * len(methods), 5), sharey=True)
    axes = np.atleast_1d(axes)
    for ax, method in zip(axes, methods, strict=True):
        frame = residuals[method]
        data = [frame[c].dropna().to_numpy() for c in frame.columns]
        positions = np.arange(len(data))
        bp = ax.boxplot(
            data,
            positions=positions,
            widths=0.6,
            showfliers=False,
            patch_artist=True,
            medianprops={"color": "black", "lw": 1.2},
        )
        for patch in bp["boxes"]:
            patch.set_facecolor("#9ecae1" if method == "ns" else "#fc9272")
            patch.set_alpha(0.85)
        ax.axhline(0.0, color="black", lw=0.8, ls="--")
        ax.set_xticks(positions)
        ax.set_xticklabels(_tenor_labels(frame.columns))
        ax.set_title(f"{method.upper()} residuals by tenor")
        ax.set_xlabel("Tenor")
        ax.grid(alpha=0.25, axis="y")
    axes[0].set_ylabel("Residual (bp)")
    fig.suptitle(title, fontsize=10)
    fig.tight_layout()
    fig.savefig(path, dpi=dpi)
    plt.close(fig)


def plot_residual_heatmap(residuals: pd.DataFrame, path: Path, dpi: int, *, title: str) -> None:
    """自由 NSS 残差的时间 × 期限热力图。"""
    frame = residuals.reindex(columns=list(CANONICAL_TENORS))
    values = frame.to_numpy(dtype=float).T  # (tenor, date)
    vmax = float(np.nanpercentile(np.abs(values), 98))
    vmax = max(vmax, 1.0)
    fig, ax = plt.subplots(figsize=(13, 4.6))
    im = ax.imshow(
        values,
        aspect="auto",
        cmap="RdBu_r",
        vmin=-vmax,
        vmax=vmax,
        interpolation="nearest",
        extent=[0, len(frame), len(frame.columns), 0],
    )
    ax.set_yticks(np.arange(len(frame.columns)) + 0.5)
    ax.set_yticklabels(_tenor_labels(frame.columns))
    ticks = np.linspace(0, len(frame) - 1, 8).astype(int)
    ax.set_xticks(ticks)
    ax.set_xticklabels([frame.index[i].strftime("%Y-%m") for i in ticks], rotation=30, ha="right")
    ax.set_title(title)
    fig.colorbar(im, ax=ax, label="Residual (bp)")
    fig.tight_layout()
    fig.savefig(path, dpi=dpi)
    plt.close(fig)


def plot_param_series(frame: pd.DataFrame, path: Path, dpi: int, *, title: str) -> None:
    """参数时间序列 + RMSE（按可用列自动布局）。"""
    panels = [
        ("b0", "Level b0 (%)"),
        ("b1", "Slope b1 (%)"),
        ("b2", "Curvature b2 (%)"),
        ("b3", "Curvature b3 (%)"),
        ("tau1", "tau1 (years)"),
        ("tau2", "tau2 (years)"),
        ("rmse_bp", "RMSE (bp)"),
    ]
    panels = [item for item in panels if item[0] in frame.columns]
    ncols = 3
    nrows = int(np.ceil(len(panels) / ncols))
    fig, axes = plt.subplots(nrows, ncols, figsize=(4.6 * ncols, 2.9 * nrows), squeeze=False)
    axes_flat = axes.ravel()
    for ax, (col, label) in zip(axes_flat, panels, strict=False):
        ax.plot(frame.index, frame[col].to_numpy(dtype=float), lw=0.7)
        ax.set_title(label)
        ax.grid(alpha=0.25)
        ax.tick_params(axis="x", rotation=30, labelsize=8)
    for ax in axes_flat[len(panels) :]:
        ax.axis("off")
    if "unstable" in frame.columns and bool(frame["unstable"].any()):
        title = f"{title} — flagged unstable days: {int(frame['unstable'].sum())}"
    fig.suptitle(title, fontsize=10)
    fig.tight_layout()
    fig.savefig(path, dpi=dpi)
    plt.close(fig)


def plot_rmse_comparison(
    free: pd.DataFrame, anchored: pd.DataFrame, path: Path, dpi: int, *, title: str
) -> None:
    """两种口径的逐日 RMSE 对比（自由 vs 锚定）。"""
    fig, ax = plt.subplots(figsize=(12, 4.2))
    ax.plot(free.index, free["rmse_bp"], lw=0.6, label="free fit (model capability)")
    ax.plot(
        anchored.index, anchored["rmse_bp"], lw=0.6, alpha=0.8, label="anchored fit (stable params)"
    )
    ax.set_yscale("symlog", linthresh=10.0)
    ax.set_ylabel("Daily RMSE (bp)")
    ax.set_title(title)
    ax.legend(loc="upper left")
    ax.grid(alpha=0.25)
    fig.tight_layout()
    fig.savefig(path, dpi=dpi)
    plt.close(fig)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main() -> int:
    cfg = get_config()
    fig_dir = cfg.paths.abs("reports") / "figures"
    report_dir = cfg.paths.abs("reports")
    fig_dir.mkdir(parents=True, exist_ok=True)
    report_dir.mkdir(parents=True, exist_ok=True)
    dpi = int(cfg.report.dpi)
    weights_mode = str(cfg.curve.weights)

    panel, source = load_panel(cfg)
    fit_input = _fit_input(panel)
    log.info("参与拟合: %d 个交易日；权重口径=%s", len(fit_input), weights_mode)

    # ---- 两套拟合 × 两种模型分别落盘 ----
    products: dict[tuple[str, str], tuple[pd.DataFrame, pd.DataFrame]] = {}
    timings: dict[tuple[str, str], float] = {}
    for method in _METHODS:
        for fit_mode, param_mode, residuals_mode in _FIT_MODES:
            t0 = time.perf_counter()
            params, residuals = fit_panel(
                fit_input,
                method=method,
                weights=cfg.curve.weights,  # R07：显式传配置口径，不用默认值
                param_mode=param_mode,
                residuals_mode=residuals_mode,
                progress=True,
            )
            timings[(method, fit_mode)] = time.perf_counter() - t0
            products[(method, fit_mode)] = (params, residuals)
            log.info(
                "全样本 %s %s 拟合完成，用时 %.2fs",
                method.upper(),
                fit_mode,
                timings[(method, fit_mode)],
            )
            params.to_csv(
                report_dir / f"curve_fit_params_{method}_{fit_mode}.csv", encoding="utf-8-sig"
            )
            residuals.to_csv(
                report_dir / f"curve_fit_residuals_{method}_{fit_mode}.csv", encoding="utf-8-sig"
            )
            fitted = fit_input + residuals / 100.0
            fitted.to_csv(
                report_dir / f"curve_fit_fitted_{method}_{fit_mode}.csv", encoding="utf-8-sig"
            )

    # ---- 统计长表 ----
    rows: list[dict[str, object]] = []
    for method in _METHODS:
        for fit_mode, _, _ in _FIT_MODES:
            params, residuals = products[(method, fit_mode)]
            stats = overall_stats(params, residuals)
            stats.update(
                {
                    "method": method.upper(),
                    "fit_mode": fit_mode,
                    "weights_mode": weights_mode,
                    "source": source,
                    "scope": "overall",
                    "tenor": np.nan,
                    "fit_seconds": timings[(method, fit_mode)],
                }
            )
            rows.append(stats)
            by_tenor = residual_table(residuals)
            for tenor, row in by_tenor.iterrows():
                rows.append(
                    {
                        "method": method.upper(),
                        "fit_mode": fit_mode,
                        "weights_mode": weights_mode,
                        "source": source,
                        "scope": "tenor",
                        "tenor": float(tenor),
                        "n_days": float(len(params)),
                        "n_obs": float(row["n"]),
                        "rmse_mean_bp": np.nan,
                        "rmse_median_bp": np.nan,
                        "rmse_p95_bp": np.nan,
                        "rmse_max_bp": np.nan,
                        "resid_mean_bp": row["mean_bp"],
                        "resid_std_bp": row["std_bp"],
                        "mae_bp": row["mae_bp"],
                        "p95_abs_bp": row["p95_abs_bp"],
                        "n_over_5bp": np.nan,
                        "n_fail": np.nan,
                        "n_unstable": np.nan,
                        "b0_in_range_pct": np.nan,
                        "b0_jump_p99_pp": np.nan,
                        "b0_jump_max_pp": np.nan,
                        "fit_seconds": np.nan,
                    }
                )
    summary = pd.DataFrame(rows)
    summary_csv = report_dir / "curve_fit_summary.csv"
    summary.to_csv(summary_csv, index=False, encoding="utf-8-sig")
    log.info("统计表: %s", summary_csv)

    # ---- 最差日清单（自由口径的可表达能力诊断；不剔除）----
    free_nss = products[("nss", "free")][0]
    bad = bad_dates(free_nss)
    bad.insert(0, "weights_mode", weights_mode)
    bad_csv = report_dir / "curve_fit_bad_dates.csv"
    bad.to_csv(bad_csv, encoding="utf-8-sig")
    log.info(
        "最差拟合日（自由 NSS rmse > %.0fbp）: %d 天 → %s", RMSE_BAD_THRESHOLD_BP, len(bad), bad_csv
    )

    # ---- 可观测因子（推荐口径，无识别性问题）----
    factors = observable_factors(panel)
    factors.to_csv(report_dir / "curve_factors.csv", encoding="utf-8-sig")
    sensitivity = factor_jump_sensitivity(factors)
    sensitivity.to_csv(report_dir / "curve_factor_jump_sensitivity.csv", encoding="utf-8-sig")
    log.info(
        "可观测因子: %s；敏感性: %s",
        report_dir / "curve_factors.csv",
        report_dir / "curve_factor_jump_sensitivity.csv",
    )

    # ---- 图 ----
    free_resid = {m: products[(m, "free")][1] for m in _METHODS}
    anchored_resid = {m: products[(m, "anchored")][1] for m in _METHODS}
    plot_residual_hist(
        free_resid,
        fig_dir / "curve_residual_hist.png",
        dpi,
        title=f"FREE fit residual distribution (weights={weights_mode})",
    )
    plot_residual_hist(
        anchored_resid,
        fig_dir / "curve_residual_hist_anchored.png",
        dpi,
        title=f"ANCHORED fit residual distribution (weights={weights_mode})",
    )
    plot_residual_by_tenor(
        free_resid,
        fig_dir / "curve_residual_by_tenor.png",
        dpi,
        title=f"FREE fit residuals by tenor (weights={weights_mode})",
    )
    plot_residual_heatmap(
        products[("nss", "free")][1],
        fig_dir / "curve_residual_heatmap.png",
        dpi,
        title=f"FREE NSS residual heatmap (bp, weights={weights_mode})",
    )
    plot_param_series(
        products[("nss", "anchored")][0],
        fig_dir / "curve_params_timeseries.png",
        dpi,
        title=f"ANCHORED NSS parameters (weights={weights_mode})",
    )
    plot_param_series(
        products[("nss", "free")][0],
        fig_dir / "curve_params_timeseries_free.png",
        dpi,
        title=f"FREE NSS parameters — identification makes them unstable (weights={weights_mode})",
    )
    plot_rmse_comparison(
        products[("nss", "free")][0],
        products[("nss", "anchored")][0],
        fig_dir / "curve_rmse_free_vs_anchored.png",
        dpi,
        title=f"NSS daily RMSE: free vs anchored (bp, weights={weights_mode})",
    )
    for name in (
        "curve_residual_hist",
        "curve_residual_hist_anchored",
        "curve_residual_by_tenor",
        "curve_residual_heatmap",
        "curve_params_timeseries",
        "curve_params_timeseries_free",
        "curve_rmse_free_vs_anchored",
    ):
        log.info("图 %-30s %s", name, fig_dir / f"{name}.png")

    # ---- 控制台摘要 ----
    print(f"\n===== 全样本拟合摘要（残差单位 bp；权重={weights_mode}）=====")
    for method in _METHODS:
        for fit_mode, _, _ in _FIT_MODES:
            params, residuals = products[(method, fit_mode)]
            s = overall_stats(params, residuals)
            print(
                f"{method.upper():>3} {fit_mode:<8}: RMSE mean={s['rmse_mean_bp']:.4f} "
                f"median={s['rmse_median_bp']:.4f} p95={s['rmse_p95_bp']:.4f} "
                f"max={s['rmse_max_bp']:.2f} | MAE={s['mae_bp']:.4f} "
                f"p95|r|={s['p95_abs_bp']:.2f} | unstable={int(s['n_unstable'])} "
                f"| b0∈[0.5,8]={s['b0_in_range_pct']:.1f}% "
                f"|Δb0|p99={s['b0_jump_p99_pp']:.3f}pp | {timings[(method, fit_mode)]:.2f}s"
            )
    free_median = float(products[("nss", "free")][0]["rmse_bp"].median())
    anchored_median = float(products[("nss", "anchored")][0]["rmse_bp"].median())
    print(
        f"\n自由 vs 锚定（NSS）：RMSE 中位 {free_median:.4f} vs {anchored_median:.4f} bp"
        "（锚定更差是预期取舍）"
    )
    print("\n自由 NSS 最差 10 个拟合日（逐案判断，不笼统归因于模型局限）:")
    top = free_nss["rmse_bp"].nlargest(10)
    for day, value in top.items():
        print(f"  {day.date()}  rmse={value:7.2f}bp")
    print("\n可观测因子跳变敏感性（推荐因子口径）:")
    print(sensitivity.round(3).to_string())
    print(f"\n统计表: {summary_csv}\n最差日清单: {bad_csv}\n图目录: {fig_dir}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
