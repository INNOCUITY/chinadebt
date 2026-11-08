"""主动风险暴露指标与归因分解辅助（研究脚本共用）。

两个使用场景
------------
1. **多窗口**（``scripts/run_windows.py``）：逐窗口报告主动 DV01 / 久期偏离与
   主动收益波动（tracking error），作为"风险暴露是否随窗口变化"的描述；
2. **lot 敏感性解释**（``scripts/run_lot_exposure.py``）：比较 ``min_trade_lot=1e6``
   与 ``5e6`` 的主动暴露差异，并用账本归因把超额变化拆成
   "市场因子（carry/roll、level/slope/curvature/other）+ 估值基差 + 现金/成本项"，
   从而回答"收益差异是否来自不同主动风险暴露"。

口径（必须明示）
----------------
- 暴露指标全部来自引擎 ``exposure`` 帧（逐日一行）：``dv01_ratio = 组合总 DV01 /
  基准总 DV01``；``duration / bench_duration`` 为久期相对基准；
  ``krd_<t>`` 与 ``bench_krd_<t>`` 是各关键期限桶 DV01（元/bp）。
- KRD 偏离的归一化分母是**基准总 DV01 的日均值**（``bench_total_dv01``），
  这样不同窗口/不同券的桶偏离可横向比较（单位 = 基准总 DV01 的倍数）。
- 首日无持仓（``market_value == 0``）会给出 ``dv01_ratio = 0``（|ratio−1| = 1），
  会污染"最大偏离"，因此平均/最大偏离默认**只统计有持仓的交易日**（warmup 剔除）。
- 归因金额口径与 ``ficc.attribution`` 的账本桥接完全一致：
  ``ΔNAV = carry_roll + level + slope + curvature + other_curve + valuation_basis
  + coupon + principal_net − funding − fees + slippage + trade_net + bridge_residual``；
  bp 一律以**回测初始 NAV**为分母（不是当日 NAV），保证不同配置/窗口可加、可比。
"""

from __future__ import annotations

from typing import Any

import numpy as np
import pandas as pd

__all__ = [
    "ATTRIBUTION_COMPONENTS",
    "EXCESS_SOURCE_NO",
    "EXCESS_SOURCE_NONE",
    "EXCESS_SOURCE_PARTIAL",
    "EXCESS_SOURCE_YES",
    "active_exposure_metrics",
    "attribution_bp",
    "attribution_components",
    "classify_excess_source",
    "excess_attribution_bp",
    "tracking_error_bp",
]

#: 归因 bp 分项（顺序即报告展示顺序）；``nav_delta``/``gap`` 是恒等式校验项
ATTRIBUTION_COMPONENTS: tuple[str, ...] = (
    "carry_roll",
    "level",
    "slope",
    "curvature",
    "other_curve",
    "valuation_basis",
    "coupon",
    "principal_net",
    "funding",
    "fees",
    "slippage",
    "trade_net",
    "bridge_residual",
)

EXCESS_SOURCE_YES = "是"
EXCESS_SOURCE_PARTIAL = "部分"
EXCESS_SOURCE_NO = "否"
EXCESS_SOURCE_NONE = "无实质差异"

#: 判定"主动暴露有材料差异"的阈值：|Δ 平均 DV01 偏离| ≥ 0.5% 基准总 DV01
DEFAULT_EXPOSURE_MATERIAL = 0.005
#: 市场因子解释 Δ超额 的占比阈值
DEFAULT_MARKET_SHARE = 0.5
#: |Δ超额| 低于该值视为"无实质差异"（bp）
DEFAULT_MIN_ABS_EXCESS_BP = 1.0


def _finite_frame(exposure: pd.DataFrame) -> pd.DataFrame:
    if not isinstance(exposure, pd.DataFrame):
        raise TypeError(f"exposure 必须是 DataFrame，实际 {type(exposure).__name__}")
    required = {"dv01_ratio", "duration", "bench_duration", "leverage", "market_value"}
    missing = sorted(required - set(exposure.columns))
    if missing:
        raise ValueError(f"exposure 缺少必需列：{missing}")
    return exposure


def _krd_column_pairs(columns: Any) -> list[tuple[float, str, str]]:
    pairs: list[tuple[float, str, str]] = []
    for column in columns:
        if not str(column).startswith("krd_"):
            continue
        tenor_text = str(column)[len("krd_") :]
        try:
            tenor = float(tenor_text)
        except ValueError:
            continue
        bench_column = f"bench_{column}"
        if bench_column in columns:
            pairs.append((tenor, str(column), bench_column))
    pairs.sort(key=lambda item: item[0])
    return pairs


def active_exposure_metrics(
    exposure: pd.DataFrame, *, exclude_warmup: bool = True
) -> dict[str, float | str]:
    """主动风险暴露指标（均值/最大值均为"有持仓交易日"上的统计）。

    Returns
    -------
    dict
        ``avg_abs_dv01_dev`` / ``max_abs_dv01_dev``：|DV01 ratio−1| 的均值/最大值；
        ``avg_dv01_ratio``；``avg_duration`` / ``avg_duration_dev``（有符号均值）/
        ``avg_abs_duration_dev`` / ``max_abs_duration_dev``；
        ``avg_leverage`` / ``avg_market_value``；
        ``krd_dev_<t>``（各桶相对基准总 DV01 的有符号均值偏离）、
        ``krd_dev_max_bucket`` / ``krd_dev_max_abs``（绝对值最大的桶）、
        ``krd_abs_dev_max`` / ``krd_abs_dev_max_bucket``（各桶 |偏离| 均值最大者）。
        无有效交易日时全部为 ``nan``。
    """
    frame = _finite_frame(exposure)
    if exclude_warmup and "market_value" in frame.columns:
        active = frame.loc[pd.to_numeric(frame["market_value"], errors="coerce").fillna(0.0) > 0.0]
    else:
        active = frame
    active = active.loc[
        np.isfinite(pd.to_numeric(active["dv01_ratio"], errors="coerce"))
        & np.isfinite(pd.to_numeric(active["duration"], errors="coerce"))
        & np.isfinite(pd.to_numeric(active["bench_duration"], errors="coerce"))
    ]
    nan = float("nan")
    result: dict[str, float | str] = {
        "n_exposure_days": float(len(active)),
        "avg_abs_dv01_dev": nan,
        "max_abs_dv01_dev": nan,
        "avg_dv01_ratio": nan,
        "max_dv01_ratio": nan,
        "avg_duration": nan,
        "avg_duration_dev": nan,
        "avg_abs_duration_dev": nan,
        "max_abs_duration_dev": nan,
        "avg_leverage": nan,
        "avg_market_value": nan,
        "krd_dev_max_bucket": "",
        "krd_dev_max_abs": nan,
        "krd_abs_dev_max": nan,
        "krd_abs_dev_max_bucket": "",
    }
    if active.empty:
        return result

    ratio = pd.to_numeric(active["dv01_ratio"], errors="coerce").to_numpy(dtype=float)
    duration = pd.to_numeric(active["duration"], errors="coerce").to_numpy(dtype=float)
    bench_duration = pd.to_numeric(active["bench_duration"], errors="coerce").to_numpy(dtype=float)
    benchmark_positive = bench_duration > 0.0
    duration_dev = np.full_like(duration, np.nan)
    duration_dev[benchmark_positive] = (
        duration[benchmark_positive] / bench_duration[benchmark_positive] - 1.0
    )
    dv01_dev = np.abs(ratio - 1.0)
    result.update(
        {
            "avg_abs_dv01_dev": float(np.nanmean(dv01_dev)),
            "max_abs_dv01_dev": float(np.nanmax(dv01_dev)),
            "avg_dv01_ratio": float(np.nanmean(ratio)),
            "max_dv01_ratio": float(np.nanmax(ratio)),
            "avg_duration": float(np.nanmean(duration)),
            "avg_duration_dev": float(np.nanmean(duration_dev)),
            "avg_abs_duration_dev": float(np.nanmean(np.abs(duration_dev))),
            "max_abs_duration_dev": float(np.nanmax(np.abs(duration_dev))),
            "avg_leverage": float(
                np.nanmean(pd.to_numeric(active["leverage"], errors="coerce").to_numpy(float))
            ),
            "avg_market_value": float(
                np.nanmean(pd.to_numeric(active["market_value"], errors="coerce").to_numpy(float))
            ),
        }
    )

    pairs = _krd_column_pairs(active.columns)
    if pairs and "bench_total_dv01" in active.columns:
        benchmark_total = pd.to_numeric(active["bench_total_dv01"], errors="coerce").to_numpy(
            dtype=float
        )
        positive = benchmark_total > 0.0
        signed: dict[float, float] = {}
        absolute: dict[float, float] = {}
        for tenor, krd_column, bench_krd_column in pairs:
            krd = pd.to_numeric(active[krd_column], errors="coerce").to_numpy(dtype=float)
            bench_krd = pd.to_numeric(active[bench_krd_column], errors="coerce").to_numpy(
                dtype=float
            )
            with np.errstate(invalid="ignore", divide="ignore"):
                deviation = np.where(positive, (krd - bench_krd) / benchmark_total, np.nan)
            if np.all(np.isnan(deviation)):
                continue
            signed[tenor] = float(np.nanmean(deviation))
            absolute[tenor] = float(np.nanmean(np.abs(deviation)))
            label = f"{tenor:g}"
            result[f"krd_dev_{label}"] = signed[tenor]
        if signed:
            worst_tenor = max(signed, key=lambda t: abs(signed[t]))
            result["krd_dev_max_bucket"] = f"{worst_tenor:g}Y"
            result["krd_dev_max_abs"] = abs(signed[worst_tenor])
        if absolute:
            worst_abs_tenor = max(absolute, key=lambda t: absolute[t])
            result["krd_abs_dev_max"] = absolute[worst_abs_tenor]
            result["krd_abs_dev_max_bucket"] = f"{worst_abs_tenor:g}Y"
    return result


def tracking_error_bp(active_returns: Any, *, annual_days: int = 250) -> float:
    """主动收益的年化跟踪误差（bp）：``std(active, ddof=0) × sqrt(250) × 1e4``。"""
    arr = np.asarray(active_returns, dtype=float).reshape(-1)
    arr = arr[np.isfinite(arr)]
    if arr.size < 2:
        return float("nan")
    return float(np.std(arr, ddof=0) * np.sqrt(float(annual_days)) * 1e4)


def attribution_components(attr: Any) -> dict[str, float]:
    """把一次账本归因结果压成**金额**分项（元），并给出恒等式校验。

    ``nav_delta`` = 归因区间内 NAV 变动合计；``gap`` = ``nav_delta − Σ分项``，
    必须为数值噪声级（真实数据实测 ~1e-8 元）。
    """
    daily = attr.daily
    factors = attr.factors
    required_daily = {
        "nav_delta",
        "coupon_cash",
        "principal_cash",
        "principal_redemption",
        "funding",
        "fees",
        "slippage",
        "trade_settlement",
        "pending_delta",
        "trade_entry",
        "bridge_residual",
        "valuation_basis",
    }
    missing = sorted(required_daily - set(daily.columns))
    if missing:
        raise ValueError(f"归因 daily 帧缺少列：{missing}")
    required_factors = {"carry_roll", "level", "slope", "curvature", "other_curve"}
    missing = sorted(required_factors - set(factors.columns))
    if missing:
        raise ValueError(f"归因 factors 帧缺少列：{missing}")

    components: dict[str, float] = {
        "carry_roll": float(factors["carry_roll"].sum()),
        "level": float(factors["level"].sum()),
        "slope": float(factors["slope"].sum()),
        "curvature": float(factors["curvature"].sum()),
        "other_curve": float(factors["other_curve"].sum()),
        "valuation_basis": float(daily["valuation_basis"].sum()),
        "coupon": float(daily["coupon_cash"].sum()),
        "principal_net": float(daily["principal_cash"].sum() - daily["principal_redemption"].sum()),
        "funding": -float(daily["funding"].sum()),
        "fees": -float(daily["fees"].sum()),
        "slippage": float(daily["slippage"].sum()),
        "trade_net": float(
            daily["trade_settlement"].sum()
            + daily["pending_delta"].sum()
            + daily["trade_entry"].sum()
        ),
        "bridge_residual": float(daily["bridge_residual"].sum()),
    }
    nav_delta = float(daily["nav_delta"].sum())
    components["nav_delta"] = nav_delta
    components["gap"] = nav_delta - float(sum(components[key] for key in ATTRIBUTION_COMPONENTS))
    components["price_pnl_model"] = float(
        sum(components[key] for key in ("carry_roll", "level", "slope", "curvature", "other_curve"))
    )
    components["price_pnl_snapshot"] = components["price_pnl_model"] + components["valuation_basis"]
    return components


def attribution_bp(attr: Any, *, initial_nav: float) -> dict[str, float]:
    """把归因金额分项换算为 **bp of 初始 NAV**（含 ``nav_delta_bp`` 与 ``gap_bp``）。"""
    if not np.isfinite(initial_nav) or initial_nav == 0.0:
        raise ValueError(f"initial_nav 必须是有限非零数，实际 {initial_nav!r}")
    components = attribution_components(attr)
    scale = 1e4 / float(initial_nav)
    result = {key: value * scale for key, value in components.items()}
    result["nav_delta_bp"] = components["nav_delta"] * scale
    result["gap_bp"] = components["gap"] * scale
    return result


def excess_attribution_bp(
    strategy_attr: Any, benchmark_attr: Any, *, initial_nav: float
) -> dict[str, float]:
    """相对基准的归因分项（bp of 初始 NAV）；``nav_delta_bp`` 即累计超额。"""
    strategy = attribution_bp(strategy_attr, initial_nav=initial_nav)
    benchmark = attribution_bp(benchmark_attr, initial_nav=initial_nav)
    result = {
        key: strategy.get(key, 0.0) - benchmark.get(key, 0.0)
        for key in (*ATTRIBUTION_COMPONENTS, "price_pnl_model", "price_pnl_snapshot")
    }
    result["nav_delta_bp"] = strategy["nav_delta_bp"] - benchmark["nav_delta_bp"]
    result["gap_bp"] = strategy["gap_bp"] - benchmark["gap_bp"]
    return result


def classify_excess_source(
    *,
    delta_excess_bp: float,
    market_component_bp: float,
    exposure_delta: float,
    exposure_material: float = DEFAULT_EXPOSURE_MATERIAL,
    market_share_threshold: float = DEFAULT_MARKET_SHARE,
    min_abs_excess_bp: float = DEFAULT_MIN_ABS_EXCESS_BP,
) -> str:
    """判定"超额差异是否来自不同主动风险暴露"。

    Parameters
    ----------
    delta_excess_bp : float
        两配置累计超额之差（bp，新配置 − 基准配置）。
    market_component_bp : float
        Δ超额中由**市场因子**（carry/roll + level/slope/curvature/other，含估值基差）
        解释的部分（bp）。占比 ``market_component_bp / delta_excess_bp``。
    exposure_delta : float
        主动 DV01 偏离的变化（如 ``avg_abs_dv01_dev`` 之差，单位 = 基准总 DV01 的倍数）。
    exposure_material : float
        判定"暴露有材料差异"的阈值，默认 0.5% 基准总 DV01。
    market_share_threshold : float
        市场因子占比阈值，默认 0.5。
    min_abs_excess_bp : float
        |Δ超额| 低于该值直接判"无实质差异"。

    Returns
    -------
    str
        :data:`EXCESS_SOURCE_YES` / :data:`EXCESS_SOURCE_PARTIAL` /
        :data:`EXCESS_SOURCE_NO` / :data:`EXCESS_SOURCE_NONE` 之一。
    """
    if not (np.isfinite(delta_excess_bp) and np.isfinite(market_component_bp)):
        return EXCESS_SOURCE_NONE
    if abs(delta_excess_bp) < min_abs_excess_bp:
        return EXCESS_SOURCE_NONE
    share = market_component_bp / delta_excess_bp
    material = np.isfinite(exposure_delta) and abs(exposure_delta) >= exposure_material
    if share >= market_share_threshold and material:
        return EXCESS_SOURCE_YES
    if share >= market_share_threshold:
        return EXCESS_SOURCE_PARTIAL
    return EXCESS_SOURCE_NO
