"""10Y 系统偏差的时间持续性诊断（R13/R19 后的统计与判词分离口径）。

背景
----
自由拟合 NSS 曲线在 10Y 的残差（``fitted - observed``，bp）均值约 **+4.6bp**、MAE
约 5.2bp。本轮只回答**统计问题**，不把它翻译成经济溢价或交易结论：

- 滚动均值（20/60/250 日）、ACF(1..20)、AR(1) 系数与 *代数半衰期*；
- ADF（H0=单位根）与 KPSS（H0=常数均值平稳）；
- 普通 t 与 **HAC 稳健 t**（Newey-West，lag=20/60/250）；
- 2002–2007 / 2008–2013 / 2014–2019 / 2020–2026 分段统计；
- 2Y/5Y/10Y/30Y 横向对照（10Y 是否特别）。

判词边界（R19）
---------------
:func:`bias_verdict` 只输出**描述性字段**：

- ``persistent_model_relative_residual``：残差是否持续偏离 0（模型相对量，**不是**
  经济溢价）；
- ``economic_interpretation = "unidentified"``：经济解释未识别；
- ``tradability = "untested"``：可交易性未检验（未做组合、成本、融资、样本外）；
- ``mean_reverting_or_drifting``：平稳性检验能支持的最弱描述。

**不输出**"持续性溢价""可交易/不可交易""负 carry"等结论性判词；自动生成的
``tenor_bias_conclusion.md`` 采用"证据 / 限制 / 未检验项"三段式，并明确：
残差是模型相对量、不能从实际 KRD 中扣除；AR(1) 半衰期只是单阶模型代数量，
不是可执行持有期。

入口
----
::

    from ficc.curve.bias import run_bias
    run_bias(cfg=cfg, residuals_csv=path, out_dir=..., start=..., end=...)

或 CLI（仅包装）：``PYTHONPATH=src .venv/bin/python -m ficc.curve.bias ...``

R12 契约：``residuals_csv`` 必须显式给出（或给 ``panel`` 当次生成）；下游会用
``curve_fit_*_meta.json`` 校验 config_hash / weights / 日期范围 / 行数，不一致**报错**，
绝不静默消费旧残差。
"""

from __future__ import annotations

import argparse
import os
import tempfile
import warnings
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from ficc.config import Config, get_config, load_config
from ficc.curve.products import build_fit_products, validate_fit_meta
from ficc.logging import get_logger

os.environ.setdefault("MPLCONFIGDIR", tempfile.mkdtemp(prefix="mpl-"))

__all__ = [
    "DEFAULT_SEGMENTS",
    "ROLLING_WINDOWS",
    "autocorrelation_structure",
    "bias_verdict",
    "hac_t_statistics",
    "load_residual_panel",
    "main",
    "mean_test",
    "residual_series",
    "rolling_means",
    "rolling_regime",
    "run_bias",
    "segment_table",
    "stationarity_tests",
    "tenor_persistence_table",
]

log = get_logger("bias_persistence")

DEFAULT_SEGMENTS: tuple[tuple[str, str], ...] = (
    ("2002", "2007"),
    ("2008", "2013"),
    ("2014", "2019"),
    ("2020", "2026"),
)
ROLLING_WINDOWS: tuple[int, ...] = (20, 60, 250)
HAC_LAGS: tuple[int, ...] = (20, 60, 250)


# ---------------------------------------------------------------------------
# 数据
# ---------------------------------------------------------------------------
def load_residual_panel(path: str | Path) -> pd.DataFrame:
    """读取残差面板（``index=date``，``columns=tenor``，values 为 bp 残差）。"""
    frame = pd.read_csv(path, index_col=0, parse_dates=True)
    if frame.empty:
        raise ValueError(f"残差面板为空: {path}")
    frame.columns = pd.Index([float(c) for c in frame.columns], name="tenor")
    frame = frame.sort_index()
    frame.index = pd.DatetimeIndex(frame.index, name="date")
    return frame


def residual_series(residuals: pd.DataFrame, tenor: float = 10.0) -> pd.Series:
    """取某期限的残差序列（bp，去掉 NaN）。"""
    columns = [float(c) for c in residuals.columns]
    if tenor not in columns:
        raise KeyError(f"残差面板没有 {tenor}Y 期限；可用 {columns}")
    series = residuals[tenor].astype(float).dropna()
    series.name = f"{tenor:g}Y_residual_bp"
    return series


def _truncate(series: pd.Series, start: str | None, end: str | None) -> pd.Series:
    out = series
    if start is not None:
        out = out.loc[out.index >= pd.Timestamp(start)]
    if end is not None:
        out = out.loc[out.index <= pd.Timestamp(end)]
    if out.empty:
        raise ValueError(f"按 start={start!r} / end={end!r} 截断后无残差样本")
    return out


# ---------------------------------------------------------------------------
# 滚动统计与自相关
# ---------------------------------------------------------------------------
def rolling_means(series: pd.Series, windows: Sequence[int] = ROLLING_WINDOWS) -> pd.DataFrame:
    """残差的 20/60/250 日滚动均值（bp）。"""
    values = series.astype(float)
    out = pd.DataFrame({f"ma{int(w)}": values.rolling(int(w)).mean() for w in windows})
    out.index.name = values.index.name or "date"
    return out


def autocorrelation_structure(series: pd.Series, nlags: int = 20) -> dict[str, Any]:
    """ACF(1..20) + AR(1) 系数 φ + **代数半衰期** ``ln(0.5)/ln(φ)``（天）。

    半衰期只是 AR(1) 单阶模型的代数量，**不是**可执行持有期；只在
    ``0 < φ < 1`` 时有定义。
    """
    from statsmodels.tsa.stattools import acf

    values = series.astype(float).dropna().to_numpy()
    n = values.size
    if n < 30:
        raise ValueError(f"样本太少（{n}）无法估计自相关")
    max_lag = int(min(nlags, n - 2))
    acf_values = acf(values, nlags=max_lag, fft=True)
    lag1 = float(acf_values[1]) if max_lag >= 1 else float("nan")
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
        "half_life_note": "AR(1) 单阶模型代数半衰期，不是可执行持有期",
    }


def stationarity_tests(series: pd.Series) -> dict[str, float]:
    """ADF（H0=单位根）与 KPSS（H0=常数均值平稳）。"""
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
    """残差均值是否显著异于 0（**普通** t 检验）+ 描述统计。"""
    from scipy import stats

    values = series.astype(float).dropna().to_numpy()
    if values.size < 2:
        raise ValueError("样本太少，无法做 t 检验")
    t_stat, t_p = stats.ttest_1samp(values, 0.0)
    mae = float(np.mean(np.abs(values)))
    return {
        "n": float(values.size),
        "mean_bp": float(np.mean(values)),
        "std_bp": float(np.std(values, ddof=1)),
        "mae_bp": mae,
        "positive_share_pct": float(np.mean(values > 0) * 100.0),
        "t_stat": float(t_stat),
        "t_p": float(t_p),
        "abs_mean_over_mae": float(abs(np.mean(values)) / mae) if mae > 0 else float("nan"),
    }


def hac_t_statistics(series: pd.Series, lags: Sequence[int] = HAC_LAGS) -> dict[str, float]:
    """Newey-West HAC 稳健 t 统计（均值 = 0 的 H0），三档 lag 并列。

    普通单样本 t 忽略残差强自相关（ACF(20)≈0.72），会把 |t| 严重高估；
    HAC t 才是与自相关相容的推断。返回 ``hac_t_lag{L}`` / ``hac_p_lag{L}``。
    """
    import statsmodels.api as sm

    values = series.astype(float).dropna().to_numpy()
    if values.size < 30:
        raise ValueError("样本太少，无法计算 HAC t")
    results: dict[str, float] = {}
    model = sm.OLS(values, np.ones((values.size, 1)))
    for lag in lags:
        lag = int(lag)
        if lag <= 0:
            raise ValueError(f"HAC lag 必须为正整数，实际 {lag}")
        fit = model.fit(cov_type="HAC", cov_kwds={"maxlags": lag})
        results[f"hac_t_lag{lag}"] = float(fit.tvalues[0])
        results[f"hac_p_lag{lag}"] = float(fit.pvalues[0])
    return results


# ---------------------------------------------------------------------------
# 分段与横向对照
# ---------------------------------------------------------------------------
def segment_table(
    series: pd.Series, segments: Sequence[tuple[str, str]] = DEFAULT_SEGMENTS
) -> pd.DataFrame:
    """按指定年份区间分段的均值/标准差/正残差占比/t 检验。"""
    rows: list[dict[str, Any]] = []
    for start, end in segments:
        block = series.loc[str(start) : str(end)].dropna()
        if block.size < 2:
            rows.append({"segment": f"{start}-{end}", "n": int(block.size)})
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
    residuals: pd.DataFrame, tenors: Sequence[float] = (2.0, 5.0, 10.0, 30.0)
) -> pd.DataFrame:
    """多期限残差持续性对照表（含普通 t 与 HAC t 三档）。"""
    rows: list[dict[str, Any]] = []
    for tenor in tenors:
        series = residual_series(residuals, float(tenor))
        mean_stats = mean_test(series)
        acf_stats = autocorrelation_structure(series)
        stationarity = stationarity_tests(series)
        hac_stats = hac_t_statistics(series)
        row: dict[str, Any] = {
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
            "hac_t_lag20": hac_stats["hac_t_lag20"],
            "hac_t_lag60": hac_stats["hac_t_lag60"],
            "hac_t_lag250": hac_stats["hac_t_lag250"],
        }
        rows.append(row)
    table = pd.DataFrame(rows).set_index("tenor")
    table.attrs["unit"] = "bp"
    table.attrs["half_life_note"] = "AR(1) 单阶模型代数半衰期，不是可执行持有期"
    return table


def rolling_regime(series: pd.Series, window: int = 250) -> dict[str, Any]:
    """250 日滚动均值的区间、符号切换与"哪段为正"的粗描述。"""
    rolling = series.astype(float).rolling(window).mean().dropna()
    if rolling.empty:
        raise ValueError("样本不足以计算滚动均值")
    sign = np.sign(rolling)
    sign_changes = sign.diff().abs() > 0
    change_dates = [index for index in sign.index[sign_changes] if index != rolling.index[0]]
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
    }


# ---------------------------------------------------------------------------
# 描述性判定（R19：不出经济/交易结论）
# ---------------------------------------------------------------------------
def _mean_reverting_or_drifting(adf_p: float, kpss_p: float) -> str:
    """只用 ADF/KPSS 能支持的最弱描述（不推断经济含义）。"""
    unit_root_rejected = adf_p < 0.10
    constant_mean_rejected = kpss_p <= 0.10
    if unit_root_rejected and constant_mean_rejected:
        return "mean_reverting_around_time_varying_mean"
    if unit_root_rejected:
        return "mean_reverting_around_constant_mean"
    return "cannot_reject_unit_root"


def bias_verdict(
    persistence: pd.DataFrame,
    segments: pd.DataFrame,
    regime: dict[str, Any],
) -> dict[str, Any]:
    """把统计证据整理成**描述性**字段（R19：不做经济/交易判词）。

    Returns
    -------
    dict
        ``persistent_model_relative_residual``（bool）、
        ``economic_interpretation``（固定 ``"unidentified"``）、
        ``tradability``（固定 ``"untested"``）、
        ``mean_reverting_or_drifting``（ADF/KPSS 能支持的最弱描述）、
        以及 ``evidence`` / ``limitations`` / ``untested`` 三段文字。
    """
    row = persistence.loc[10.0]
    segment_means = [float(value) for value in segments["mean_bp"] if np.isfinite(value)]
    persistent = bool(row["adf_p"] < 0.10 and row["positive_share_pct"] > 75.0)
    evidence = [
        f"10Y 残差均值 {row['mean_bp']:+.3f}bp，MAE {row['mae_bp']:.3f}bp，"
        f"正残差占比 {row['positive_share_pct']:.1f}%，样本 {int(row['n'])} 天",
        f"普通 t={row['t_stat']:.2f}（忽略自相关，仅作对照）；"
        f"HAC t（lag 20/60/250）= {row['hac_t_lag20']:.2f} / "
        f"{row['hac_t_lag60']:.2f} / {row['hac_t_lag250']:.2f}",
        f"ADF p={row['adf_p']:.5f}（H0=单位根）、KPSS p={row['kpss_p']:.4f}"
        f"（H0=常数均值平稳）；AR(1) φ={row['ar1_phi']:.4f}，"
        f"代数半衰期 {row['half_life_days']:.2f} 日，ACF(20)={row['acf_20']:.3f}",
        "分段均值(bp): "
        + " / ".join(
            f"{seg!s}:{float(value):+.2f}"
            for seg, value in zip(segments["segment"], segments["mean_bp"], strict=True)
        ),
        f"250 日滚动均值区间 [{float(regime['min_bp']):.2f}, {float(regime['max_bp']):.2f}]bp，"
        f"为正占比 {float(regime['positive_share_pct']):.1f}%，"
        f"符号切换 {int(regime['sign_change_count'])} 次",
    ]
    limitations = [
        "残差是**模型相对量**（NSS 自由拟合 vs 观测），不是可观测错价；其均值部分会被"
        "曲线拟合吸收，不能从实际 KRD 中扣除全样本残差均值",
        "AR(1) 半衰期是单阶模型的代数量，不是可执行持有期；ACF 衰减很慢说明单阶模型"
        "不足以描述自相关结构",
        "ADF 拒绝单位根 ≠ 常数均值平稳；KPSS 结果需与 ADF 并列解读",
        "分段均值随制度变化（见 evidence），无法用一个常数刻画",
    ]
    untested = [
        "可执行组合（多空腿、DV01 中性、券种/流动性约束）未构建",
        "交易成本、融资成本、冲击成本未计入",
        "样本外稳定性、真实前向评估、已知-at（known_at）时间戳未检验",
        "残差的经济解释（如流动性/基准溢价）未识别，本报告不作归因结论",
    ]
    return {
        "persistent_model_relative_residual": persistent,
        "economic_interpretation": "unidentified",
        "tradability": "untested",
        "mean_reverting_or_drifting": _mean_reverting_or_drifting(
            float(row["adf_p"]), float(row["kpss_p"])
        ),
        "unit_root_rejected": bool(row["adf_p"] < 0.10),
        "constant_mean_rejected": bool(row["kpss_p"] <= 0.10),
        "segment_means_bp": segment_means,
        "evidence": evidence,
        "limitations": limitations,
        "untested": untested,
    }


# ---------------------------------------------------------------------------
# 报告生成
# ---------------------------------------------------------------------------
def _plot_bias_rolling(series: pd.Series, path: Path, dpi: int) -> None:
    import matplotlib.pyplot as plt

    rolling = rolling_means(series, ROLLING_WINDOWS)
    fig, ax = plt.subplots(figsize=(13, 5.2))
    ax.plot(series.index, series.to_numpy(), color="0.75", lw=0.5, label="10Y residual (bp)")
    for window in ROLLING_WINDOWS:
        ax.plot(
            rolling.index,
            rolling[f"ma{window}"].to_numpy(),
            lw=2.2 if window == 250 else 1.6,
            label=f"{window}d rolling mean",
        )
    ax.axhline(0.0, color="black", lw=0.8, ls="--")
    for _start, end in DEFAULT_SEGMENTS[:-1]:
        ax.axvline(pd.Timestamp(f"{end}-12-31"), color="0.6", lw=0.8, ls=":")
    ax.set_title("10Y NSS residual: rolling means (bp)")
    ax.set_ylabel("Residual (bp): fitted - observed")
    ax.legend(loc="upper left", ncol=2, fontsize=8)
    ax.grid(alpha=0.25)
    fig.tight_layout()
    fig.savefig(path, dpi=dpi)
    plt.close(fig)


def _build_conclusion_md(
    verdict: dict[str, Any],
    persistence: pd.DataFrame,
    segments: pd.DataFrame,
    *,
    meta: dict[str, Any],
    residuals_csv: Path,
    panel_start: pd.Timestamp,
    panel_end: pd.Timestamp,
) -> str:
    """生成"证据 / 限制 / 未检验项"三段式结论（R19）。"""
    lines = [
        "# 10Y 系统偏差：统计证据、限制与未检验项（自动生成）",
        "",
        "## 口径",
        "",
        f"- 残差文件：`{residuals_csv}`（NSS 自由拟合，单位 bp）",
        f"- config_hash：`{meta.get('config_hash')}`；weights：`{meta.get('weights')}`；"
        f"method：`{meta.get('method')}`",
        f"- 样本区间：{panel_start.date()} → {panel_end.date()}（n={int(persistence.loc[10.0, 'n'])}）",
        f"- 生成时间：{datetime.now(UTC).isoformat()}",
        "",
        "## 描述性字段",
        "",
        f"- persistent_model_relative_residual = {verdict['persistent_model_relative_residual']}",
        f"- economic_interpretation = {verdict['economic_interpretation']}",
        f"- tradability = {verdict['tradability']}",
        f"- mean_reverting_or_drifting = {verdict['mean_reverting_or_drifting']}",
        "",
        "## 证据",
        "",
    ]
    lines.extend(f"- {item}" for item in verdict["evidence"])
    lines.extend(["", "## 限制", ""])
    lines.extend(f"- {item}" for item in verdict["limitations"])
    lines.extend(["", "## 未检验项", ""])
    lines.extend(f"- {item}" for item in verdict["untested"])
    lines.extend(
        [
            "",
            "## 多期限对照",
            "",
            persistence.round(4).to_markdown(),
            "",
            "## 10Y 分段统计",
            "",
            segments.round(4).to_markdown(index=False),
            "",
        ]
    )
    return "\n".join(lines)


def run_bias(
    *,
    cfg: Config | None = None,
    residuals_csv: str | Path | None = None,
    panel: pd.DataFrame | None = None,
    out_dir: str | Path | None = None,
    start: str | pd.Timestamp | None = None,
    end: str | pd.Timestamp | None = None,
) -> dict[str, Path]:
    """生成 10Y 偏差持续性报告，返回 ``{artifact: path}``。

    Parameters
    ----------
    cfg : Config | None
        配置；``None`` 时才读默认配置。传入后不再调用 ``get_config()``。
    residuals_csv : str | Path | None
        残差面板路径。**必须显式给出**（R12）；为 ``None`` 时若给了 ``panel``
        则当次生成拟合产品（``build_fit_products``）再使用其免费残差；
        两者都未给 → 抛 ``ValueError``，绝不默认读旧文件。
    panel : DataFrame | None
        可选收益率宽表（用于当次生成残差）。
    out_dir : str | Path | None
        输出目录；默认 ``cfg.paths.abs("reports")``。
    start, end : 可选日期边界
        截断残差样本；超出残差文件范围时由 meta 校验报错。

    Returns
    -------
    dict[str, Path]
        ``persistence`` / ``segments`` / ``conclusion`` / ``figure``（+ ``meta``）。
    """
    resolved = cfg if cfg is not None else get_config()
    directory = Path(out_dir) if out_dir is not None else resolved.paths.abs("reports")
    figures = directory / "figures"
    figures.mkdir(parents=True, exist_ok=True)

    if residuals_csv is None:
        if panel is None:
            raise ValueError(
                "bias.run_bias 必须显式提供 residuals_csv 或 panel（R12：不得默认读取旧残差）。"
                "请先生成 build_fit_products() 并传残差路径"
            )
        products = build_fit_products(
            resolved,
            panel,
            method="nss",
            out_dir=directory,
            include_summary=False,
            start=start,
            end=end,
        )
        residuals_csv = products["residuals_nss_free"]

    residuals_path = Path(residuals_csv)
    meta = validate_fit_meta(resolved, residuals_path, start=start, end=end)
    residuals = load_residual_panel(residuals_path)
    residuals = residuals.loc[
        (residuals.index >= pd.Timestamp(meta["start"]))
        & (residuals.index <= pd.Timestamp(meta["end"]))
    ]
    series = _truncate(residual_series(residuals, 10.0), start, end)
    log.info(
        "偏差分析样本: %s→%s（%d 天）；残差=%s；config_hash=%s",
        series.index.min().date(),
        series.index.max().date(),
        len(series),
        residuals_path,
        meta["config_hash"],
    )

    persistence = tenor_persistence_table(residuals, (2.0, 5.0, 10.0, 30.0))
    segments = segment_table(series)
    regime = rolling_regime(series, window=250)
    verdict = bias_verdict(persistence, segments, regime)

    persistence_csv = directory / "tenor_bias_persistence.csv"
    persistence.to_csv(persistence_csv, encoding="utf-8-sig")
    segments_out = segments.copy()
    segments_out.insert(0, "tenor", 10.0)
    segments_csv = directory / "tenor_bias_segments.csv"
    segments_out.to_csv(segments_csv, index=False, encoding="utf-8-sig")

    figure_path = figures / "bias_10y_rolling.png"
    _plot_bias_rolling(series, figure_path, dpi=int(resolved.report.dpi))

    md_path = directory / "tenor_bias_conclusion.md"
    md_path.write_text(
        _build_conclusion_md(
            verdict,
            persistence,
            segments,
            meta=meta,
            residuals_csv=residuals_path,
            panel_start=series.index.min(),
            panel_end=series.index.max(),
        ),
        encoding="utf-8",
    )
    log.info("偏差持续性报告: %s / %s / %s", persistence_csv, segments_csv, md_path)

    print("\n===== 10Y 系统偏差：统计证据（R19 描述性口径）=====")
    print(f"persistent_model_relative_residual={verdict['persistent_model_relative_residual']}")
    print(f"economic_interpretation={verdict['economic_interpretation']}")
    print(f"tradability={verdict['tradability']}")
    print(f"mean_reverting_or_drifting={verdict['mean_reverting_or_drifting']}")
    print("\n多期限对照:")
    print(persistence.round(3).to_string())
    print("\n10Y 分段:")
    print(segments.round(3).to_string(index=False))
    print(f"\n产物: {persistence_csv}\n      {segments_csv}\n      {md_path}\n      {figure_path}")
    return {
        "persistence": persistence_csv,
        "segments": segments_csv,
        "conclusion": md_path,
        "figure": figure_path,
        "meta": Path(residuals_csv).parent / f"curve_fit_{meta['method']}_meta.json",
    }


def main(argv: list[str] | None = None) -> int:
    """CLI 包装：解析参数后调用 :func:`run_bias`。"""
    parser = argparse.ArgumentParser(description="10Y 偏差持续性诊断（描述性统计）")
    parser.add_argument("--config", default=None, help="配置文件路径")
    parser.add_argument("--residuals", default=None, help="残差面板 CSV（必须与 meta 一致）")
    parser.add_argument("--start", default=None, help="样本起始日（含）")
    parser.add_argument("--end", default=None, help="样本截止日（含）")
    parser.add_argument("--out-dir", default=None, help="输出根目录（默认 cfg.paths.reports）")
    args = parser.parse_args(argv)
    cfg = load_config(args.config) if args.config else get_config()
    run_bias(
        cfg=cfg, residuals_csv=args.residuals, start=args.start, end=args.end, out_dir=args.out_dir
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
