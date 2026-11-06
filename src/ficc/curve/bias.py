"""10Y 系统偏差的时间持续性诊断（本轮关键结论）。

背景
----
自由拟合的 NSS 曲线在 10Y 的残差（``fitted - observed``，bp）均值约 **+4.6bp**、
MAE 约 5.2bp：10Y 观测收益率系统性**低于**曲线拟合值，即 10Y 相对整条曲线"偏贵"。
问题：这是**持续的结构性溢价**（例如 10Y 作为最活跃基准券的流动性/配置溢价），
还是**随时间变化的模型误差**？这决定能否把残差当作"可交易错价"。

本模块给出
----------
- :func:`rolling_means`：20/60/250 日滚动均值；
- :func:`autocorrelation_structure`：ACF(1..20)、AR(1) 系数 φ、半衰期 ``ln(0.5)/ln(φ)``；
- :func:`stationarity_tests`：ADF（原假设=单位根）与 KPSS（原假设=平稳）；
- :func:`mean_test`：残差均值是否显著异于 0（t 检验）；
- :func:`segment_table`：2002–2007 / 2008–2013 / 2014–2019 / 2020–2026 四段统计；
- :func:`tenor_persistence_table`：2Y/5Y/10Y/30Y 横向对照（10Y 是否特别）；
- :func:`rolling_regime`：250 日滚动均值的区间、符号切换与"哪段为正"；
- :func:`bias_verdict`：把上述证据落到"持续性溢价 vs 时变误差 + 是否可交易"的判定。

运行（生成本轮报告产物）::

    PYTHONPATH=src .venv/bin/python -m ficc.curve.bias

产物：``reports/tenor_bias_persistence.csv``、``reports/tenor_bias_segments.csv``、
``reports/tenor_bias_conclusion.md``、``reports/figures/bias_10y_rolling.png``。

结论（数据见表；由 :func:`bias_verdict` 自动生成并写入结论 md）
--------------------------------------------------------------
10Y 偏差是**慢变的持续性溢价**（ADF 拒绝单位根、88.9% 交易日为正、250 日滚动均
值几乎全程为正、AR(1) 半衰期约 5.6 日但 ACF 衰减很慢），**不是白噪声式的时变误差**；
但它的**幅度随制度漂移**（四段均值约 +2.2 → +3.3 → +7.2 → +5.7bp），且 NSS 只是
低维平滑、不是无套利公允价值，残差均值部分是被拟合吸收的"基准溢价"。因此**不满足
"可作错价交易"的前提**（可预期到稳定锚 + 可重复 + 幅度覆盖对冲与交易成本），
**不可直接当作独立错价信号**；只能作为估值/对冲的中性化调整项。
"""

from __future__ import annotations

import os
import sys
import tempfile
import warnings
from collections.abc import Sequence
from pathlib import Path

import numpy as np
import pandas as pd

__all__ = [
    "DEFAULT_SEGMENTS",
    "ROLLING_WINDOWS",
    "autocorrelation_structure",
    "bias_verdict",
    "load_residual_panel",
    "main",
    "mean_test",
    "residual_series",
    "rolling_means",
    "rolling_regime",
    "segment_table",
    "stationarity_tests",
    "tenor_persistence_table",
]

# 四段样本（需求方指定）
DEFAULT_SEGMENTS: tuple[tuple[str, str], ...] = (
    ("2002", "2007"),
    ("2008", "2013"),
    ("2014", "2019"),
    ("2020", "2026"),
)
ROLLING_WINDOWS: tuple[int, ...] = (20, 60, 250)

# 交易成本参考（config.costs：活跃券买卖价差 0.35bp、结算费约 0.014bp/亿）
ROUND_TRIP_COST_BP: float = 0.35 * 2 + 0.02


# ---------------------------------------------------------------------------
# 数据
# ---------------------------------------------------------------------------
def load_residual_panel(path: str | Path) -> pd.DataFrame:
    """读取残差面板（``reports/curve_fit_residuals_nss_free.csv``）。

    Returns
    -------
    pandas.DataFrame
        ``index = date``，``columns = tenor``（float，年），values 为 bp 残差。
    """
    frame = pd.read_csv(path, index_col=0, parse_dates=True)
    if frame.empty:
        raise ValueError(f"残差面板为空: {path}")
    frame.columns = pd.Index([float(c) for c in frame.columns], name="tenor")
    frame = frame.sort_index()
    frame.index.name = "date"
    return frame


def residual_series(residuals: pd.DataFrame, tenor: float = 10.0) -> pd.Series:
    """取某期限的残差序列（bp，去掉 NaN）。"""
    columns = [float(c) for c in residuals.columns]
    if tenor not in columns:
        raise KeyError(f"残差面板没有 {tenor}Y 期限；可用 {columns}")
    series = residuals[tenor].astype(float).dropna()
    series.name = f"{tenor:g}Y_residual_bp"
    return series


# ---------------------------------------------------------------------------
# 滚动统计与自相关
# ---------------------------------------------------------------------------
def rolling_means(series: pd.Series, windows: Sequence[int] = ROLLING_WINDOWS) -> pd.DataFrame:
    """残差的 20/60/250 日滚动均值（bp）。"""
    values = series.astype(float)
    out = pd.DataFrame({f"ma{int(w)}": values.rolling(int(w)).mean() for w in windows})
    out.index.name = values.index.name or "date"
    return out


def autocorrelation_structure(series: pd.Series, nlags: int = 20) -> dict[str, float | list[float]]:
    """ACF(1..20) + AR(1) 系数 φ + 半衰期 ``ln(0.5)/ln(φ)``（天）。

    半衰期只在 ``0 < φ < 1`` 时有定义；否则返回 ``nan``（例如 φ≥1 表示不衰减）。
    """
    from statsmodels.tsa.stattools import acf

    values = series.astype(float).dropna().to_numpy()
    n = values.size
    if n < 30:
        raise ValueError(f"样本太少（{n}）无法估计自相关")
    max_lag = int(min(nlags, n - 2))
    acf_values = acf(values, nlags=max_lag, fft=True)
    lag1 = float(acf_values[1]) if max_lag >= 1 else float("nan")
    # AR(1) 系数（对水平值回归，与 ADF 的回归式一致）
    phi = float(np.polyfit(values[:-1], values[1:], 1)[0])
    half_life = float(np.log(0.5) / np.log(phi)) if 0.0 < phi < 1.0 else float("nan")
    return {
        "n": float(n),
        "acf": [float(v) for v in acf_values[1:]],
        "acf_1": lag1,
        "acf_5": float(acf_values[5]) if max_lag >= 5 else float("nan"),
        "acf_10": float(acf_values[10]) if max_lag >= 10 else float("nan"),
        "acf_20": float(acf_values[20]) if max_lag >= 20 else float("nan"),
        "ar1_phi": phi,
        "half_life_days": half_life,
    }


def stationarity_tests(series: pd.Series) -> dict[str, float]:
    """ADF（H0=单位根）与 KPSS（H0=水平平稳）检验。

    两者结论**相反**时（ADF 拒绝单位根、KPSS 拒绝平稳）通常意味着序列是
    "慢变/带结构断点的平稳过程"——不是白噪声，也不是纯随机游走。
    """
    from statsmodels.tsa.stattools import adfuller, kpss

    values = series.astype(float).dropna().to_numpy()
    with warnings.catch_warnings():
        warnings.simplefilter("ignore")
        try:
            adf = adfuller(values, regression="c", autolag="AIC", result_object=False)
        except TypeError:  # pragma: no cover - 兼容旧版 statsmodels
            adf = adfuller(values, regression="c", autolag="AIC")
        try:
            kp = kpss(values, regression="c", nlags="auto", store=False, result_object=False)
        except TypeError:  # pragma: no cover
            kp = kpss(values, regression="c", nlags="auto", store=False)
    return {
        "adf_stat": float(adf[0]),
        "adf_p": float(adf[1]),
        "kpss_stat": float(kp[0]),
        "kpss_p": float(kp[1]),
    }


def mean_test(series: pd.Series) -> dict[str, float]:
    """残差均值是否显著异于 0（单样本 t 检验）+ 常用描述统计。"""
    from scipy import stats

    values = series.astype(float).dropna().to_numpy()
    if values.size < 2:
        raise ValueError("样本太少，无法做 t 检验")
    t_stat, t_p = stats.ttest_1samp(values, 0.0)
    return {
        "n": float(values.size),
        "mean_bp": float(np.mean(values)),
        "std_bp": float(np.std(values, ddof=1)),
        "mae_bp": float(np.mean(np.abs(values))),
        "positive_share_pct": float(np.mean(values > 0) * 100.0),
        "t_stat": float(t_stat),
        "t_p": float(t_p),
        "abs_mean_over_mae": float(abs(np.mean(values)) / np.mean(np.abs(values)))
        if np.mean(np.abs(values)) > 0
        else float("nan"),
    }


# ---------------------------------------------------------------------------
# 分段与横向对照
# ---------------------------------------------------------------------------
def segment_table(
    series: pd.Series,
    segments: Sequence[tuple[str, str]] = DEFAULT_SEGMENTS,
) -> pd.DataFrame:
    """按指定年份区间分段的均值/标准差/正残差占比/t 检验。"""
    rows: list[dict[str, object]] = []
    for start, end in segments:
        block = series.loc[str(start) : str(end)].dropna()
        if block.size < 2:
            rows.append(
                {
                    "segment": f"{start}-{end}",
                    "n": int(block.size),
                    "mean_bp": np.nan,
                    "std_bp": np.nan,
                    "mae_bp": np.nan,
                    "positive_share_pct": np.nan,
                    "t_stat": np.nan,
                    "t_p": np.nan,
                }
            )
            continue
        stats_ = mean_test(block)
        rows.append(
            {
                "segment": f"{start}-{end}",
                "n": int(stats_["n"]),
                "mean_bp": stats_["mean_bp"],
                "std_bp": stats_["std_bp"],
                "mae_bp": stats_["mae_bp"],
                "positive_share_pct": stats_["positive_share_pct"],
                "t_stat": stats_["t_stat"],
                "t_p": stats_["t_p"],
            }
        )
    return pd.DataFrame(rows)


def tenor_persistence_table(
    residuals: pd.DataFrame,
    tenors: Sequence[float] = (2.0, 5.0, 10.0, 30.0),
) -> pd.DataFrame:
    """多期限的残差持续性对照表（10Y 是否特别）。"""
    rows: list[dict[str, object]] = []
    for tenor in tenors:
        series = residual_series(residuals, float(tenor))
        mean_stats = mean_test(series)
        acf_stats = autocorrelation_structure(series)
        stationarity = stationarity_tests(series)
        rows.append(
            {
                "tenor": float(tenor),
                **{
                    key: mean_stats[key]
                    for key in (
                        "n",
                        "mean_bp",
                        "std_bp",
                        "mae_bp",
                        "positive_share_pct",
                        "t_stat",
                        "t_p",
                        "abs_mean_over_mae",
                    )
                },
                "acf_1": acf_stats["acf_1"],
                "acf_5": acf_stats["acf_5"],
                "acf_10": acf_stats["acf_10"],
                "acf_20": acf_stats["acf_20"],
                "ar1_phi": acf_stats["ar1_phi"],
                "half_life_days": acf_stats["half_life_days"],
                "adf_stat": stationarity["adf_stat"],
                "adf_p": stationarity["adf_p"],
                "kpss_stat": stationarity["kpss_stat"],
                "kpss_p": stationarity["kpss_p"],
            }
        )
    table = pd.DataFrame(rows).set_index("tenor")
    table.attrs["unit"] = "bp"
    return table


def rolling_regime(series: pd.Series, window: int = 250) -> dict[str, object]:
    """250 日滚动均值的区间、符号切换与"哪段为正"的粗判断。"""
    rolling = series.astype(float).rolling(window).mean().dropna()
    if rolling.empty:
        raise ValueError("样本不足以计算滚动均值")
    sign = np.sign(rolling)
    sign_changes = sign.diff().abs() > 0
    change_dates = [index for index in sign.index[sign_changes] if index != rolling.index[0]]
    total = float(sign.sum())
    return {
        "window": int(window),
        "min_bp": float(rolling.min()),
        "min_date": rolling.idxmin(),
        "max_bp": float(rolling.max()),
        "max_date": rolling.idxmax(),
        "positive_share_pct": float((rolling > 0).mean() * 100.0),
        "sign_change_dates": change_dates,
        "sign_change_count": len(change_dates),
        "final_bp": float(rolling.iloc[-1]),
        "_total": total,
    }


# ---------------------------------------------------------------------------
# 判定
# ---------------------------------------------------------------------------
def bias_verdict(
    persistence: pd.DataFrame,
    segments: pd.DataFrame,
    regime: dict[str, object],
    *,
    costs_bp: float = ROUND_TRIP_COST_BP,
) -> dict[str, object]:
    """把证据落到一句话判定：持续性溢价 vs 时变误差；是否可作错价交易。

    Returns
    -------
    dict
        含 ``conclusion``（一句话）、``is_persistent``、``is_tradable``、
        ``segment_means``、``evidence``（列表）等字段。
    """
    row = persistence.loc[10.0]
    segment_means = [float(v) for v in segments["mean_bp"]]
    same_sign = all(value > 0 for value in segment_means) or all(
        value < 0 for value in segment_means
    )
    range_bp = max(segment_means) - min(segment_means)
    persistent = bool(row["adf_p"] < 0.10 and row["positive_share_pct"] > 75.0)
    magnitude_covers_cost = bool(abs(row["mean_bp"]) > 3.0 * costs_bp)
    # "稳定锚"要求分段均值漂移远小于均值本身（否则没有可锚定的公允价值）
    stable_anchor = bool(same_sign and range_bp < 0.5 * abs(row["mean_bp"]))
    # 常数均值平稳（KPSS 不拒绝）= 残差围绕一个不随时间变化的公允价值波动；
    # 否则只是"慢变溢价"，静态水平交易没有回归锚。
    constant_mean_ok = bool(row["kpss_p"] > 0.05)
    tradable = bool(persistent and magnitude_covers_cost and stable_anchor and constant_mean_ok)
    positive_share_pct = float(regime["positive_share_pct"])
    evidence = [
        f"10Y 残差均值 {row['mean_bp']:+.3f}bp，MAE {row['mae_bp']:.3f}bp，正残差占比 {row['positive_share_pct']:.1f}%",
        f"ADF p={row['adf_p']:.5f}，KPSS p={row['kpss_p']:.4f}；AR(1) φ={row['ar1_phi']:.4f}，半衰期 {row['half_life_days']:.1f} 日，ACF(20)={row['acf_20']:.3f}",
        "分段均值(bp): "
        + " / ".join(
            f"{seg!s}:{float(value):+.2f}"
            for seg, value in zip(segments["segment"], segment_means, strict=True)
        ),
        f"250 日滚动均值区间 [{float(regime['min_bp']):.2f}, {float(regime['max_bp']):.2f}]bp，"
        f"为正占比 {positive_share_pct:.1f}%，符号切换 {int(regime['sign_change_count'])} 次",
        f"|mean|/MAE={row['abs_mean_over_mae']:.3f}；参考往返成本约 {costs_bp:.2f}bp",
        f"稳定锚={stable_anchor}（分段区间 {range_bp:.2f}bp vs |mean| {abs(row['mean_bp']):.2f}bp）；"
        f"KPSS 常数均值检验={'不拒绝' if constant_mean_ok else '拒绝'}（p={row['kpss_p']:.4f}）",
    ]
    if persistent and not tradable:
        conclusion = (
            f"10Y 偏差是**持续性溢价**（10Y 结构性偏贵：均值 {row['mean_bp']:+.2f}bp、"
            f"{row['positive_share_pct']:.1f}% 交易日为正、ADF p={row['adf_p']:.4f} 拒绝单位根），"
            f"且**可预期/可重复**，但**不满足'可作错价交易'**：分段均值 {segment_means[0]:+.1f}→"
            f"{segment_means[-1]:+.1f}bp（区间宽 {range_bp:.1f}bp，KPSS 拒绝常数均值），没有可锚定的"
            "公允价值；静态水平交易还承担负 carry。**结论：不可直接作为独立错价信号**，只能作为估值/"
            "对冲的中性化调整项；可交易对象只能是残差相对滚动均值的短期偏离（需另做 z-score 检验）。"
        )
    elif persistent and tradable:
        conclusion = (
            f"10Y 偏差是持续性溢价且幅度({row['mean_bp']:+.2f}bp)覆盖成本、符号稳定，"
            "**满足错价交易的前提，可进一步做滚动 z-score 的事后检验**。"
        )
    else:
        conclusion = (
            f"10Y 偏差不是稳定的持续性溢价（正占比 {row['positive_share_pct']:.1f}%、分段符号/幅度不稳），"
            "更接近**时变模型误差**，**不可作为错价交易信号**。"
        )
    return {
        "conclusion": conclusion,
        "is_persistent": persistent,
        "is_tradable": tradable,
        "stable_anchor": stable_anchor,
        "magnitude_covers_cost": magnitude_covers_cost,
        "segment_means": segment_means,
        "segment_range_bp": range_bp,
        "evidence": evidence,
        "costs_bp": costs_bp,
    }


# ---------------------------------------------------------------------------
# 报告生成
# ---------------------------------------------------------------------------
def main() -> int:
    """生成 10Y 偏差持续性报告（CSV / Markdown / 图）。"""
    os.environ.setdefault("MPLCONFIGDIR", tempfile.mkdtemp(prefix="mpl-"))
    import matplotlib

    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    try:
        from ficc.config import get_config
        from ficc.logging import get_logger
    except ImportError:  # pragma: no cover - 允许直接运行脚本
        sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
        from ficc.config import get_config
        from ficc.logging import get_logger

    log = get_logger("bias_persistence")
    cfg = get_config()
    reports = cfg.paths.abs("reports")
    figures = reports / "figures"
    figures.mkdir(parents=True, exist_ok=True)
    residuals_path = reports / "curve_fit_residuals_nss_free.csv"
    if not residuals_path.exists():
        raise FileNotFoundError(
            f"缺少残差面板 {residuals_path}；请先运行 scripts/fit_diagnostics.py 或 fit_panel 生成"
        )

    residuals = load_residual_panel(residuals_path)
    log.info("残差面板: %s（%d 天 × %d 期限）", residuals_path, *residuals.shape)

    series = residual_series(residuals, 10.0)
    persistence = tenor_persistence_table(residuals, (2.0, 5.0, 10.0, 30.0))
    segments = segment_table(series)
    regime = rolling_regime(series, window=250)
    verdict = bias_verdict(persistence, segments, regime)

    persistence_csv = reports / "tenor_bias_persistence.csv"
    persistence.to_csv(persistence_csv, encoding="utf-8-sig")
    segments_out = segments.copy()
    segments_out.insert(0, "tenor", 10.0)
    segments_csv = reports / "tenor_bias_segments.csv"
    segments_out.to_csv(segments_csv, index=False, encoding="utf-8-sig")
    log.info("持续性统计: %s；分段统计: %s", persistence_csv, segments_csv)

    # ---- 图：10Y 残差 + 20/60/250 日滚动均值 ----
    rolling = rolling_means(series, ROLLING_WINDOWS)
    fig, ax = plt.subplots(figsize=(13, 5.2))
    ax.plot(series.index, series.to_numpy(), color="0.75", lw=0.5, label="10Y residual (bp)")
    for window in ROLLING_WINDOWS:
        ax.plot(
            rolling.index,
            rolling[f"ma{window}"].to_numpy(),
            lw=1.6 if window != 250 else 2.2,
            label=f"{window}d rolling mean",
        )
    ax.axhline(0.0, color="black", lw=0.8, ls="--")
    for _start, end in DEFAULT_SEGMENTS[:-1]:
        ax.axvline(pd.Timestamp(f"{end}-12-31"), color="0.6", lw=0.8, ls=":")
    ax.set_title("10Y NSS residual persistence: rolling means (bp)")
    ax.set_ylabel("Residual (bp): fitted - observed")
    ax.legend(loc="upper left", ncol=2, fontsize=8)
    ax.grid(alpha=0.25)
    fig.tight_layout()
    figure_path = figures / "bias_10y_rolling.png"
    fig.savefig(figure_path, dpi=int(cfg.report.dpi))
    plt.close(fig)
    log.info("图: %s", figure_path)

    # ---- 结论 md ----
    md_lines = [
        "# 10Y 系统偏差的时间持续性（自动生成）",
        "",
        "- 数据：`reports/curve_fit_residuals_nss_free.csv`（NSS 自由拟合残差，单位 bp）",
        f"- 10Y 样本：{int(persistence.loc[10.0, 'n'])} 个交易日",
        "",
        "## 一句话结论",
        "",
        verdict["conclusion"],
        "",
        "## 证据",
        "",
    ]
    md_lines.extend(f"- {item}" for item in verdict["evidence"])
    md_lines.extend(
        [
            "",
            "## 多期限对照（残差持续性）",
            "",
            persistence.round(4).to_markdown(),
            "",
            "## 10Y 分段统计",
            "",
            segments.round(4).to_markdown(index=False),
            "",
            "## 判定字段",
            "",
            f"- is_persistent={verdict['is_persistent']}",
            f"- stable_anchor={verdict['stable_anchor']}",
            f"- magnitude_covers_cost={verdict['magnitude_covers_cost']}",
            f"- is_tradable={verdict['is_tradable']}",
            "",
            "## 交易含义",
            "",
            "静态的 10Y 偏贵（残差均值为正）不能直接交易：NSS 残差是**模型相对量**，",
            "均值本身会被曲线拟合并入；可交易的只有「相对该慢变溢价的短期偏离」（滚动 z-score），",
            "而溢价水平随制度漂移（见分段与 250 日滚动均值），需要状态空间/滚动均值估计，",
            "本轮证据不足以支持把它当独立错价信号。",
            "",
        ]
    )
    md_path = reports / "tenor_bias_conclusion.md"
    md_path.write_text("\n".join(md_lines), encoding="utf-8")
    log.info("结论: %s", md_path)

    print("\n===== 10Y 系统偏差持续性 =====")
    print(verdict["conclusion"])
    print("\n多期限对照:")
    print(persistence.round(3).to_string())
    print("\n10Y 分段:")
    print(segments.round(3).to_string(index=False))
    print(f"\n产物: {persistence_csv}\n      {segments_csv}\n      {md_path}\n      {figure_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
