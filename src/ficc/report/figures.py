"""统一绘图模块。

设计约定
--------
1. **图中文字统一用英文**：matplotlib 在 macOS/Linux 上对中文字体的依赖不稳定，
   缺字体会渲染成方块（豆腐块）。报告正文用中文，图表用英文标签，是工程上更稳的选择。
2. **所有图必须可复现**：不依赖随机数，颜色与坐标范围显式指定。
3. 图统一输出到 `reports/figures/`，文件名稳定（便于报告引用与 diff）。
"""

from __future__ import annotations

import inspect
import logging
import os
from collections.abc import Iterable, Mapping
from pathlib import Path

# matplotlib 需要在 import 之前确定缓存目录；默认 ~/.matplotlib 在沙箱/CI 里可能不可写
os.environ.setdefault("MPLCONFIGDIR", str(Path(__file__).resolve().parents[3] / ".mplconfig"))

import matplotlib

matplotlib.use("Agg")  # 无 GUI 后端，服务器/CI 可用
# matplotlib 启动时会扫描系统字体并打印大量噪声日志，压到 ERROR
logging.getLogger("matplotlib.font_manager").setLevel(logging.ERROR)
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

# 统一色板（与 plotly 默认色接近，报告与 Dashboard 视觉一致）
COLORS = ["#636EFA", "#EF553B", "#00CC96", "#AB63FA", "#FFA15A", "#19D3F3", "#FF6692", "#B6E880"]
CURVE_CMAP = "RdYlBu_r"


def setup_style() -> None:
    """设置全局绘图风格（幂等）。"""
    plt.rcParams.update(
        {
            "figure.dpi": 120,
            "savefig.dpi": 150,
            "savefig.bbox": "tight",
            "font.size": 10,
            "axes.grid": True,
            "grid.alpha": 0.25,
            "grid.linestyle": "--",
            "axes.spines.top": False,
            "axes.spines.right": False,
            "axes.titlesize": 12,
            "axes.titleweight": "bold",
            "legend.frameon": False,
            "figure.autolayout": False,
        }
    )


def _save(fig: plt.Figure, out_path: str | Path) -> Path:
    """保存图片并关闭 figure（避免内存泄漏）。"""
    path = Path(out_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fig.savefig(path, dpi=plt.rcParams["savefig.dpi"])
    plt.close(fig)
    return path


# ---------------------------------------------------------------------------
# 曲线相关
# ---------------------------------------------------------------------------
def plot_curve_heatmap(
    panel: pd.DataFrame,
    out_path: str | Path,
    *,
    title: str = "China Government Bond Yield Curve (2002-2026)",
    vmin: float | None = None,
    vmax: float | None = None,
) -> Path:
    """收益率曲线热力图：横轴期限、纵轴时间、颜色为收益率(%)。

    Parameters
    ----------
    panel : DataFrame
        index 为日期，columns 为期限（年），values 为收益率(%)。
    """
    setup_style()
    if panel.empty:
        raise ValueError("热力图输入为空")

    fig, ax = plt.subplots(figsize=(11, 5.2))
    # 用 imshow 更快，但时间轴不等距；这里用 pcolormesh 保证时间轴正确
    x = np.asarray(panel.columns, dtype=float)
    y = matplotlib.dates.date2num(panel.index.to_pydatetime())
    z = panel.to_numpy(
        dtype=float
    )  # shape (n_dates, n_tenors)，与 pcolormesh(X=tenor, Y=date) 匹配

    im = ax.pcolormesh(x, y, z, cmap=CURVE_CMAP, shading="nearest", vmin=vmin, vmax=vmax)
    ax.yaxis.set_major_locator(mdates.YearLocator(2))
    ax.yaxis.set_major_formatter(mdates.DateFormatter("%Y"))
    ax.set_xlim(float(x.min()) - 1.0, float(x.max()) + 1.0)
    ax.set_xlabel("Tenor (years)")
    ax.set_ylabel("Date")
    ax.set_title(title)
    ax.grid(False)
    cbar = fig.colorbar(im, ax=ax, pad=0.015)
    cbar.set_label("Yield (%)")
    return _save(fig, out_path)


def plot_curve_snapshot(
    curves: Mapping[str, tuple[Iterable[float], Iterable[float]]],
    out_path: str | Path,
    *,
    title: str = "Yield Curve Snapshots",
) -> Path:
    """多时点曲线对比。

    Parameters
    ----------
    curves : dict[str, (tenors, rates)]
        例如 ``{"2020-01-02": (tenors, rates), "2026-11-05": (...)}``。
    """
    setup_style()
    fig, ax = plt.subplots(figsize=(8, 4.5))
    for i, (label, (tenors, rates)) in enumerate(curves.items()):
        ax.plot(
            list(tenors),
            list(rates),
            marker="o",
            ms=3.5,
            lw=1.6,
            color=COLORS[i % len(COLORS)],
            label=label,
        )
    ax.set_xlabel("Tenor (years)")
    ax.set_ylabel("Yield (%)")
    ax.set_title(title)
    ax.legend()
    return _save(fig, out_path)


def plot_residual_hist(
    residuals_bp: np.ndarray | pd.Series,
    out_path: str | Path,
    *,
    title: str = "Curve Fit Residuals",
    bins: int = 60,
) -> Path:
    """拟合残差分布（单位 bp）：直方图 + 累计分布，标注均值/标准差/95 分位。"""
    setup_style()
    arr = np.asarray(residuals_bp, dtype=float)
    arr = arr[np.isfinite(arr)]
    if arr.size == 0:
        raise ValueError("残差为空，无法绘图")

    # 用稳健的横轴范围（1%~99% 分位），否则极端畸变日（如 156bp）会把主体压成一根线
    lo = float(np.percentile(arr, 1))
    hi = float(np.percentile(arr, 99))
    span = max(hi - lo, 1.0)
    xlim = (lo - 0.15 * span, hi + 0.15 * span)
    n_out = int(((arr < xlim[0]) | (arr > xlim[1])).sum())

    fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(11, 3.8))

    ax1.hist(
        np.clip(arr, *xlim), bins=bins, color=COLORS[0], alpha=0.8, edgecolor="white", linewidth=0.3
    )
    ax1.axvline(0, color="black", lw=0.8, ls=":")
    ax1.axvline(arr.mean(), color=COLORS[1], lw=1.4, label=f"mean={arr.mean():.2f}bp")
    ax1.set_xlabel("Residual (bp)")
    ax1.set_ylabel("Count")
    ax1.set_title(title)
    if n_out:
        ax1.text(
            0.02,
            0.95,
            f"{n_out} obs outside [{xlim[0]:.0f}, {xlim[1]:.0f}] bp (clipped)",
            transform=ax1.transAxes,
            fontsize=8,
            va="top",
            color="0.35",
        )
    ax1.legend()

    # 累计分布（同样限制在稳健区间）
    ax2.plot(np.sort(arr), np.linspace(0, 1, arr.size), color=COLORS[2], lw=1.6)
    p95 = float(np.percentile(np.abs(arr), 95))
    ax2.axvline(p95, color=COLORS[3], ls="--", lw=1, label=f"|resid| p95={p95:.2f}bp")
    ax2.axvline(-p95, color=COLORS[3], ls="--", lw=1)
    ax2.set_xlim(*xlim)
    ax2.set_xlabel("Residual (bp)")
    ax2.set_ylabel("Cumulative probability")
    ax2.set_title("Empirical CDF")
    ax2.legend(loc="lower right")

    fig.suptitle(
        f"n={arr.size:,}  std={arr.std():.2f}bp  MAE={np.abs(arr).mean():.2f}bp  "
        f"max|r|={np.abs(arr).max():.1f}bp",
        fontsize=9,
        y=1.02,
    )
    return _save(fig, out_path)


def plot_residual_by_tenor(
    residuals: pd.DataFrame,
    out_path: str | Path,
    *,
    tenor_col: str = "tenor",
    resid_col: str = "residual_bp",
    title: str = "Residuals by Tenor",
) -> Path:
    """按期限分组的残差箱线图 + 均值折线。"""
    setup_style()
    fig, ax = plt.subplots(figsize=(8.5, 4.2))
    df = residuals.copy()
    # 允许 tenor 是数值（年）或标签字符串；数值按大小排序，字符串按首次出现顺序
    unique = list(dict.fromkeys(df[tenor_col].tolist()))
    try:
        unique = sorted(unique, key=float)
        labels = [f"{float(t):g}Y" for t in unique]
    except (TypeError, ValueError):
        labels = [str(t) for t in unique]
    groups = [df.loc[df[tenor_col] == t, resid_col].to_numpy() for t in unique]
    # matplotlib 3.9 起把 labels= 改名为 tick_labels=，同时兼容两个版本
    label_kw = (
        "tick_labels" if "tick_labels" in inspect.signature(ax.boxplot).parameters else "labels"
    )
    bp = ax.boxplot(groups, showfliers=False, patch_artist=True, widths=0.55, **{label_kw: labels})
    for patch in bp["boxes"]:
        patch.set_facecolor(COLORS[0])
        patch.set_alpha(0.55)
    ax.axhline(0, color="black", lw=0.8, ls=":")
    means = [np.mean(g) for g in groups]
    ax.plot(range(1, len(means) + 1), means, "o-", color=COLORS[1], ms=4, lw=1.4, label="mean")
    ax.set_xlabel("Tenor")
    ax.set_ylabel("Residual (bp)")
    ax.set_title(title)
    ax.legend()
    return _save(fig, out_path)


def plot_param_series(
    params: pd.DataFrame,
    out_path: str | Path,
    *,
    title: str = "Nelson-Siegel / Svensson Parameters",
) -> Path:
    """拟合参数的时序（每参数一个子图）。"""
    setup_style()
    cols = [c for c in ("b0", "b1", "b2", "tau1", "tau2", "rmse_bp") if c in params.columns]
    if not cols:
        raise ValueError(f"参数面板缺少可用列，实际列: {list(params.columns)}")

    n = len(cols)
    fig, axes = plt.subplots(n, 1, figsize=(9, 1.6 * n + 0.6), sharex=True)
    axes = np.atleast_1d(axes)
    for ax, col in zip(axes, cols, strict=True):
        ax.plot(params.index, params[col], lw=1.2, color=COLORS[0])
        ax.set_ylabel(col)
    axes[0].set_title(title)
    axes[-1].set_xlabel("Date")
    return _save(fig, out_path)


def plot_nav(
    nav: pd.Series,
    out_path: str | Path,
    *,
    benchmark: pd.Series | None = None,
    title: str = "Strategy Net Asset Value",
) -> Path:
    """净值曲线（可选叠加基准）+ 下方回撤子图。"""
    setup_style()
    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(9.5, 5.6), sharex=True, gridspec_kw={"height_ratios": [3, 1]}
    )
    ax1.plot(nav.index, nav / nav.iloc[0], lw=1.5, color=COLORS[0], label="Strategy")
    if benchmark is not None:
        ax1.plot(
            benchmark.index,
            benchmark / benchmark.iloc[0],
            lw=1.2,
            color=COLORS[1],
            label="Benchmark",
        )
    ax1.set_ylabel("NAV (rebased to 1)")
    ax1.set_title(title)
    ax1.legend()

    dd = nav / nav.cummax() - 1.0
    ax2.fill_between(dd.index, dd, 0, color=COLORS[1], alpha=0.45)
    ax2.set_ylabel("Drawdown")
    ax2.set_xlabel("Date")
    ax2.yaxis.set_major_formatter(matplotlib.ticker.PercentFormatter(1.0, decimals=1))
    return _save(fig, out_path)
