#!/usr/bin/env python3
"""lot=5M 的 +33.45bp 解释（计划 §12.12-B 最后一条）。

背景
----
2 年窗口（2019-01-28→2020-12-31）的参数敏感性里，``2s10s`` 在
``min_trade_lot=5e6`` 时累计超额 +33.45bp（基线 lot=1e6 时为 −2.16bp）。本脚本
在**冻结主配置**（只覆盖 ``constraints.min_trade_lot``）下回答：

**这个差异是否来自"不同的主动风险暴露"？** 还是只来自交易时点/成本？

做法
----
1. 对 carry_roll / 2s10s / rv 分别跑 lot=1e6 与 5e6（其余配置不变，记录
   config_hash），输出主动风险暴露对比：平均/最大 |DV01 ratio−1|、久期相对偏离、
   各 KRD 桶相对基准偏离（最大桶）、杠杆、平均市值、换手、成交笔数、平均持仓券数、
   年化 tracking error；
2. 用 ``attribute_fixed_holdings`` 对两配置各自做账本归因，把超额变化拆成
   carry/roll、level/slope/curvature/other、估值基差、票息/本金、融资、费用、
   slippage、未结算/交易时点项（金额恒等式校验 gap）；
3. 结论写入 ``reports/lot_exposure.md``，并明确声明 **lot 不是可调参数**：
   它是市场可成交最小面值（整数手约束），由券种/市场规则决定；这里只做两个数值的
   敏感性/解释，**不能据此挑选"最优 lot"**。

**预注册判定规则（不按结果调整）**
- 市场因子解释 Δ超额 的占比 ≥ 50% 且主动 DV01 偏离变化 ≥ 0.5%（基准总 DV01 的倍数）
  → "是"（来自不同主动风险暴露）；
- 市场因子占比 ≥ 50% 但暴露变化 < 0.5% → "部分"；
- 市场因子占比 < 50% → "否"（主要来自费用/融资/交易时点/取整执行差异）；
- |Δ超额| < 1bp → "无实质差异"。
所有 bp 以**回测初始 NAV** 为分母，归因分项严格可加（gap 单独披露）。

产物：``reports/lot_exposure_summary.csv``、``reports/lot_exposure_attribution.csv``、
``reports/lot_exposure_diff.csv``、``reports/lot_exposure.md``。

用法::

    PYTHONPATH=src .venv/bin/python scripts/run_lot_exposure.py
    PYTHONPATH=src .venv/bin/python scripts/run_lot_exposure.py --quick
    PYTHONPATH=src .venv/bin/python scripts/run_lot_exposure.py --strategies 2s10s
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
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

from ficc.attribution import AttributionResult, attribute_fixed_holdings  # noqa: E402
from ficc.backtest.engine import BacktestResult, run_backtest  # noqa: E402
from ficc.config import Config, get_config  # noqa: E402
from ficc.research import (  # noqa: E402
    ATTRIBUTION_COMPONENTS,
    DEFAULT_EXPOSURE_MATERIAL,
    DEFAULT_MARKET_SHARE,
    DEFAULT_MIN_ABS_EXCESS_BP,
    EXCESS_SOURCE_NONE,
    EXCESS_SOURCE_PARTIAL,
    EXCESS_SOURCE_YES,
    active_exposure_metrics,
    build_strategy_factories,
    classify_excess_source,
    excess_attribution_bp,
    resolve_benchmark_version,
    tracking_error_bp,
)

DEFAULT_START = date(2019, 1, 28)
DEFAULT_END = date(2020, 12, 31)
QUICK_START = date(2019, 1, 28)
QUICK_END = date(2019, 6, 28)
BASE_LOT = 1_000_000.0
ALT_LOT = 5_000_000.0
DEFAULT_STRATEGIES = ("carry_roll", "2s10s", "rv")
QUICK_STRATEGIES = ("2s10s",)
KEY_TENORS: tuple[float, ...] = (1.0, 2.0, 3.0, 5.0, 7.0, 10.0, 15.0, 20.0, 30.0)

LOT_SUMMARY_COLUMNS: tuple[str, ...] = (
    "strategy",
    "lot",
    "start",
    "end",
    "benchmark_version",
    "config_hash",
    "initial_nav",
    "final_nav",
    "total_return",
    "benchmark_total_return",
    "excess_bp",
    "tracking_error_bp",
    "avg_nav",
    "avg_market_value",
    "avg_leverage",
    "avg_dv01_ratio",
    "avg_abs_dv01_dev",
    "max_abs_dv01_dev",
    "avg_duration",
    "avg_duration_dev",
    "avg_abs_duration_dev",
    "max_abs_duration_dev",
    "krd_dev_max_bucket",
    "krd_dev_max_abs",
    "krd_abs_dev_max",
    "krd_abs_dev_max_bucket",
    "total_turnover",
    "n_trades",
    "avg_n_positions",
    "n_rejected",
    "n_violation_days",
    *(f"krd_dev_{tenor:g}" for tenor in KEY_TENORS),
)

LOT_ATTRIBUTION_COLUMNS: tuple[str, ...] = (
    "strategy",
    "lot",
    "start",
    "end",
    "initial_nav",
    "avg_nav",
    "price_pnl_model",
    "price_pnl_snapshot",
    *ATTRIBUTION_COMPONENTS,
    "nav_delta_bp",
    "gap_bp",
)

LOT_DIFF_COLUMNS: tuple[str, ...] = (
    "strategy",
    "base_lot",
    "alt_lot",
    "start",
    "end",
    "benchmark_version",
    "config_hash",
    "excess_bp_base",
    "excess_bp_alt",
    "delta_excess_bp",
    "delta_market_bp",
    "market_share",
    "delta_carry_roll_bp",
    "delta_level_bp",
    "delta_slope_bp",
    "delta_curvature_bp",
    "delta_other_curve_bp",
    "delta_valuation_basis_bp",
    "delta_coupon_bp",
    "delta_principal_net_bp",
    "delta_funding_bp",
    "delta_fees_bp",
    "delta_slippage_bp",
    "delta_trade_net_bp",
    "delta_bridge_residual_bp",
    "identity_gap_bp",
    "avg_abs_dv01_dev_base",
    "avg_abs_dv01_dev_alt",
    "delta_avg_abs_dv01_dev",
    "max_abs_dv01_dev_base",
    "max_abs_dv01_dev_alt",
    "delta_max_abs_dv01_dev",
    "tracking_error_bp_base",
    "tracking_error_bp_alt",
    "delta_tracking_error_bp",
    "krd_abs_dev_max_base",
    "krd_abs_dev_max_alt",
    "delta_krd_abs_dev_max",
    "n_trades_base",
    "n_trades_alt",
    "total_turnover_base",
    "total_turnover_alt",
    "delta_turnover",
    "exposure_material",
    "market_share_threshold",
    "excess_source",
)


# ---------------------------------------------------------------------------
# 运行 + 归因
# ---------------------------------------------------------------------------
def _lot_config(cfg: Config, lot: float) -> Config:
    """冻结主配置，只覆盖 ``constraints.min_trade_lot``（唯一敏感性参数）。"""
    constraints = cfg.constraints.model_copy(update={"min_trade_lot": float(lot)})
    return cfg.model_copy(update={"constraints": constraints})


def _attribute(
    name: str,
    result: BacktestResult,
    *,
    panel: pd.DataFrame,
    calendar: Any,
    cfg: Config,
    strategy_side: bool,
    benchmark: AttributionResult | None = None,
) -> AttributionResult:
    return attribute_fixed_holdings(
        strategy=name,
        nav=result.nav if strategy_side else result.benchmark_nav,
        snapshots=result.positions if strategy_side else result.benchmark_positions,
        cashflows=result.cashflows if strategy_side else result.benchmark_cashflows,
        trades=result.trades if strategy_side else result.benchmark_trades,
        panel=panel,
        calendar=calendar,
        cfg=cfg,
        benchmark=benchmark,
    )


def _activity_metrics(result: BacktestResult) -> dict[str, float]:
    constraints = result.constraints
    n_violation_days = int((~constraints["ok"].astype(bool)).sum()) if len(constraints) else 0
    avg_positions = (
        float(pd.to_numeric(constraints["n_positions"], errors="coerce").mean())
        if len(constraints)
        else float("nan")
    )
    return {
        "total_turnover": float(result.turnover.sum()),
        "n_trades": float(len(result.trades)),
        "avg_n_positions": avg_positions,
        "n_rejected": float(len(result.rejected)),
        "n_violation_days": float(n_violation_days),
        "avg_nav": float(result.exposure["nav"].mean()) if len(result.exposure) else float("nan"),
    }


def _summary_row(
    strategy: str,
    lot: float,
    result: BacktestResult,
    cfg: Config,
    *,
    start: date,
    end: date,
    benchmark_version: str,
) -> dict[str, Any]:
    exposure = active_exposure_metrics(result.exposure)
    activity = _activity_metrics(result)
    initial_nav = float(result.nav.iloc[0])
    final_nav = float(result.nav.iloc[-1])
    total_return = final_nav / initial_nav - 1.0
    benchmark_return = float(result.benchmark_nav.iloc[-1] / result.benchmark_nav.iloc[0] - 1.0)
    active = result.nav.pct_change() - result.benchmark_nav.pct_change()
    row: dict[str, Any] = {
        "strategy": strategy,
        "lot": float(lot),
        "start": str(start),
        "end": str(end),
        "benchmark_version": benchmark_version,
        "config_hash": cfg.hash(),
        "initial_nav": initial_nav,
        "final_nav": final_nav,
        "total_return": total_return,
        "benchmark_total_return": benchmark_return,
        "excess_bp": (total_return - benchmark_return) * 1e4,
        "tracking_error_bp": tracking_error_bp(active, annual_days=int(cfg.backtest.annual_days)),
        "avg_nav": activity["avg_nav"],
        "total_turnover": activity["total_turnover"],
        "n_trades": activity["n_trades"],
        "avg_n_positions": activity["avg_n_positions"],
        "n_rejected": activity["n_rejected"],
        "n_violation_days": activity["n_violation_days"],
    }
    row.update(exposure)
    return row


def _attribution_row(
    strategy: str,
    lot: float,
    excess_bp: dict[str, float],
    *,
    start: date,
    end: date,
    initial_nav: float,
    avg_nav: float,
) -> dict[str, Any]:
    row: dict[str, Any] = {
        "strategy": strategy,
        "lot": float(lot),
        "start": str(start),
        "end": str(end),
        "initial_nav": initial_nav,
        "avg_nav": avg_nav,
    }
    row.update({key: excess_bp.get(key, float("nan")) for key in ATTRIBUTION_COMPONENTS})
    row["price_pnl_model"] = excess_bp.get("price_pnl_model", float("nan"))
    row["price_pnl_snapshot"] = excess_bp.get("price_pnl_snapshot", float("nan"))
    row["nav_delta_bp"] = excess_bp.get("nav_delta_bp", float("nan"))
    row["gap_bp"] = excess_bp.get("gap_bp", float("nan"))
    return row


def _diff_row(
    strategy: str,
    base_row: dict[str, Any],
    alt_row: dict[str, Any],
    base_attr: dict[str, float],
    alt_attr: dict[str, float],
    *,
    start: date,
    end: date,
    benchmark_version: str,
    config_hash: str,
) -> dict[str, Any]:
    deltas = {
        key: alt_attr.get(key, 0.0) - base_attr.get(key, 0.0) for key in ATTRIBUTION_COMPONENTS
    }
    delta_excess = float(alt_row["excess_bp"] - base_row["excess_bp"])
    delta_market = float(
        alt_attr.get("price_pnl_snapshot", 0.0) - base_attr.get("price_pnl_snapshot", 0.0)
    )
    market_share = delta_market / delta_excess if abs(delta_excess) > 1e-9 else float("nan")
    delta_sum = float(sum(deltas.values()))
    exposure_delta = float(alt_row["avg_abs_dv01_dev"] - base_row["avg_abs_dv01_dev"])
    exposure_material = bool(abs(exposure_delta) >= DEFAULT_EXPOSURE_MATERIAL)
    excess_source = classify_excess_source(
        delta_excess_bp=delta_excess,
        market_component_bp=delta_market,
        exposure_delta=exposure_delta,
        exposure_material=DEFAULT_EXPOSURE_MATERIAL,
        market_share_threshold=DEFAULT_MARKET_SHARE,
        min_abs_excess_bp=DEFAULT_MIN_ABS_EXCESS_BP,
    )
    row: dict[str, Any] = {
        "strategy": strategy,
        "base_lot": float(base_row["lot"]),
        "alt_lot": float(alt_row["lot"]),
        "start": str(start),
        "end": str(end),
        "benchmark_version": benchmark_version,
        "config_hash": config_hash,
        "excess_bp_base": float(base_row["excess_bp"]),
        "excess_bp_alt": float(alt_row["excess_bp"]),
        "delta_excess_bp": delta_excess,
        "delta_market_bp": delta_market,
        "market_share": market_share,
        "identity_gap_bp": delta_excess - delta_sum,
        "avg_abs_dv01_dev_base": float(base_row["avg_abs_dv01_dev"]),
        "avg_abs_dv01_dev_alt": float(alt_row["avg_abs_dv01_dev"]),
        "delta_avg_abs_dv01_dev": exposure_delta,
        "max_abs_dv01_dev_base": float(base_row["max_abs_dv01_dev"]),
        "max_abs_dv01_dev_alt": float(alt_row["max_abs_dv01_dev"]),
        "delta_max_abs_dv01_dev": float(alt_row["max_abs_dv01_dev"] - base_row["max_abs_dv01_dev"]),
        "tracking_error_bp_base": float(base_row["tracking_error_bp"]),
        "tracking_error_bp_alt": float(alt_row["tracking_error_bp"]),
        "delta_tracking_error_bp": float(
            alt_row["tracking_error_bp"] - base_row["tracking_error_bp"]
        ),
        "krd_abs_dev_max_base": float(base_row["krd_abs_dev_max"]),
        "krd_abs_dev_max_alt": float(alt_row["krd_abs_dev_max"]),
        "delta_krd_abs_dev_max": float(alt_row["krd_abs_dev_max"] - base_row["krd_abs_dev_max"]),
        "n_trades_base": float(base_row["n_trades"]),
        "n_trades_alt": float(alt_row["n_trades"]),
        "total_turnover_base": float(base_row["total_turnover"]),
        "total_turnover_alt": float(alt_row["total_turnover"]),
        "delta_turnover": float(alt_row["total_turnover"] - base_row["total_turnover"]),
        "exposure_material": exposure_material,
        "market_share_threshold": DEFAULT_MARKET_SHARE,
        "excess_source": excess_source,
    }
    for key in ("carry_roll", "level", "slope", "curvature", "other_curve"):
        row[f"delta_{key}_bp"] = float(deltas[key])
    for key in (
        "valuation_basis",
        "coupon",
        "principal_net",
        "funding",
        "fees",
        "slippage",
        "trade_net",
        "bridge_residual",
    ):
        row[f"delta_{key}_bp"] = float(deltas[key])
    return row


# ---------------------------------------------------------------------------
# Markdown 报告
# ---------------------------------------------------------------------------
def _md_table(headers: list[str], rows: list[list[str]]) -> str:
    lines = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    for row in rows:
        lines.append("| " + " | ".join(row) + " |")
    return "\n".join(lines)


def _fmt(value: Any, digits: int = 4) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if not np.isfinite(number):
        return "n/a"
    return f"{number:.{digits}f}"


def _pct(value: Any, digits: int = 2) -> str:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return str(value)
    if not np.isfinite(number):
        return "n/a"
    return f"{number * 100:.{digits}f}%"


def _dominant_factor(diff: dict[str, Any]) -> str:
    candidates = {
        "carry/roll": float(diff.get("delta_carry_roll_bp", 0.0)),
        "level(10Y)": float(diff.get("delta_level_bp", 0.0)),
        "slope(2s10s)": float(diff.get("delta_slope_bp", 0.0)),
        "curvature(2s5s10s)": float(diff.get("delta_curvature_bp", 0.0)),
        "other_curve": float(diff.get("delta_other_curve_bp", 0.0)),
    }
    name = max(candidates, key=lambda key: abs(candidates[key]))
    return f"{name}（{candidates[name]:+.2f}bp）"


def _market_share_phrase(diff: dict[str, Any]) -> str:
    """把 market_share 翻译成人类可读的"市场因子解释"措辞（含 >100% 抵消情形）。"""
    share = float(diff["market_share"])
    delta_market = float(diff["delta_market_bp"])
    if not np.isfinite(share):
        return "市场因子占不可计算（Δ超额≈0）"
    if abs(share) > 1.2:
        return (
            f"市场因子变化 {delta_market:+.2f}bp"
            f"（|占比| {abs(share) * 100:.0f}%，其余分项反向抵消）"
        )
    if share < 0:
        return f"市场因子变化 {delta_market:+.2f}bp（与 Δ超额 方向相反，占比 {share:.0%}）"
    return f"市场因子变化 {delta_market:+.2f}bp（占比 {share:.0%}）"


def _conclusion_paragraph(diff: dict[str, Any]) -> str:
    strategy = diff["strategy"]
    source = diff["excess_source"]
    delta_excess = float(diff["delta_excess_bp"])
    base_dev = float(diff["avg_abs_dv01_dev_base"])
    alt_dev = float(diff["avg_abs_dv01_dev_alt"])
    te_base = float(diff["tracking_error_bp_base"])
    te_alt = float(diff["tracking_error_bp_alt"])
    n_trades_base = int(diff["n_trades_base"])
    n_trades_alt = int(diff["n_trades_alt"])
    dominant = _dominant_factor(diff)
    share_phrase = _market_share_phrase(diff)
    exposure_phrase = (
        f"主动 DV01 偏离（平均 |DV01 ratio−1|）由 {_pct(base_dev)} 变为 {_pct(alt_dev)}"
        f"（变化 {_pct(alt_dev - base_dev)}，材料阈值 {_pct(DEFAULT_EXPOSURE_MATERIAL)}），"
        f"年化 TE 由 {te_base:.2f}bp 变为 {te_alt:.2f}bp，成交 {n_trades_base}→{n_trades_alt} 笔"
    )
    if source == EXCESS_SOURCE_NONE:
        return (
            f"- **{strategy}**：lot 1e6→5e6 的累计超额变化 {delta_excess:+.2f}bp，"
            f"低于 {DEFAULT_MIN_ABS_EXCESS_BP:g}bp 的实质差异阈值 → **无实质差异**。"
        )
    no_active_tilt = n_trades_alt <= 4
    if source == EXCESS_SOURCE_YES:
        extra = (
            "（lot=5M 实际只有 4 笔建仓、未建立主动 tilt，差异等价于"
            '"有无主动暴露"——这同样不是可交易的正向选择）'
            if no_active_tilt
            else ""
        )
        return (
            f"- **{strategy}**：**是，主要来自不同的主动风险暴露。** 累计超额变化 "
            f"{delta_excess:+.2f}bp，{share_phrase}；主导分项为 {dominant}。"
            f"{exposure_phrase}。即整手取整改变了实际持仓的 KRD/久期路径，"
            f"差异不是同一暴露下仅成本/时点造成的{extra}。"
        )
    if source == EXCESS_SOURCE_PARTIAL:
        return (
            f"- **{strategy}**：**部分来自主动暴露差异。** 累计超额变化 {delta_excess:+.2f}bp，"
            f"{share_phrase}（主导 {dominant}），但 {exposure_phrase}，"
            f"暴露变化幅度小于材料阈值 {_pct(DEFAULT_EXPOSURE_MATERIAL)}，"
            "不能把全部差异归因于暴露变化。"
        )
    return (
        f"- **{strategy}**：**否，不是系统性的主动暴露差异。** 累计超额变化 "
        f"{delta_excess:+.2f}bp，{share_phrase}；其余主要来自费用/融资/交易时点"
        f"（整手取整后的执行差异）。{exposure_phrase}。"
    )


def _write_markdown(
    *,
    path: Path,
    summary: pd.DataFrame,
    attribution: pd.DataFrame,
    diff: pd.DataFrame,
    start: date,
    end: date,
    benchmark_version: str,
    config_hash: str,
) -> None:
    lines: list[str] = []
    lines.append("# lot=5M 的 +33.45bp 解释（主动风险暴露复核）")
    lines.append("")
    lines.append(
        "> 研究问题：2 年窗口（2019-01-28→2020-12-31）参数敏感性中 `2s10s` 在 "
        "`min_trade_lot=5e6` 时累计超额 +33.45bp（基线 −2.16bp）；"
        "该差异是否来自**不同的主动风险暴露**，还是交易时点/成本？"
    )
    lines.append("")
    lines.append("## 口径与预注册判定规则")
    lines.append("")
    lines.append(
        f"- 样本：{start} → {end}；基准版本：`{benchmark_version}`；config_hash：`{config_hash}`。"
    )
    lines.append(
        "- 冻结主配置，只覆盖 `constraints.min_trade_lot` ∈ {1e6, 5e6}；"
        "其余参数、成本、约束、宇宙完全一致。"
    )
    lines.append(
        "- 主动暴露：`dv01_ratio = 组合总DV01/基准总DV01`；KRD 偏离以基准总 DV01 归一；"
        "统计剔除首日无持仓 warmup。"
    )
    lines.append(
        "- 归因：`attribute_fixed_holdings` 账本归因；bp 以回测初始 NAV 为分母；"
        "分项严格可加，`identity_gap_bp` 单独披露（应为数值噪声）。"
    )
    lines.append(
        f"- 判定规则（预注册）：市场因子解释 Δ超额 ≥ {DEFAULT_MARKET_SHARE:.0%} 且主动 "
        f"DV01 偏离变化 ≥ {DEFAULT_EXPOSURE_MATERIAL:.2%}（基准总 DV01 倍数）→ **是**；"
        f"市场因子 ≥ {DEFAULT_MARKET_SHARE:.0%} 但暴露变化不足 → **部分**；"
        f"市场因子 < {DEFAULT_MARKET_SHARE:.0%} → **否**；"
        f"|Δ超额| < {DEFAULT_MIN_ABS_EXCESS_BP:g}bp → **无实质差异**。"
    )
    lines.append("")

    lines.append("## 1. 各配置累计超额（先复现 +33.45bp）")
    lines.append("")
    headers = [
        "策略",
        "lot",
        "累计超额(bp)",
        "总收益",
        "基准收益",
        "年化TE(bp)",
        "成交笔数",
        "换手",
    ]
    rows = []
    for row in summary.itertuples(index=False):
        rows.append(
            [
                str(row.strategy),
                _fmt(row.lot, 0),
                f"{row.excess_bp:+.2f}",
                _pct(row.total_return),
                _pct(row.benchmark_total_return),
                _fmt(row.tracking_error_bp, 2),
                _fmt(row.n_trades, 0),
                _fmt(row.total_turnover, 3),
            ]
        )
    lines.append(_md_table(headers, rows))
    lines.append("")

    lines.append("## 2. 主动风险暴露对比")
    lines.append("")
    headers = [
        "策略",
        "lot",
        "平均|DV01r−1|",
        "最大|DV01r−1|",
        "久期偏离",
        "平均|久期偏离|",
        "KRD最大桶(有符号)",
        "各桶|偏离|最大",
        "杠杆",
        "平均市值(亿)",
        "平均持仓数",
    ]
    rows = []
    for row in summary.itertuples(index=False):
        rows.append(
            [
                str(row.strategy),
                _fmt(row.lot, 0),
                _pct(row.avg_abs_dv01_dev),
                _pct(row.max_abs_dv01_dev),
                _pct(row.avg_duration_dev),
                _pct(row.avg_abs_duration_dev),
                f"{row.krd_dev_max_bucket} {_pct(row.krd_dev_max_abs)}",
                f"{row.krd_abs_dev_max_bucket} {_pct(row.krd_abs_dev_max)}",
                _fmt(row.avg_leverage, 3),
                _fmt(row.avg_market_value / 1e8, 2),
                _fmt(row.avg_n_positions, 1),
            ]
        )
    lines.append(_md_table(headers, rows))
    lines.append("")
    lines.append("KRD 桶的逐桶有符号偏离（相对基准总 DV01 的均值）：")
    lines.append("")
    headers = ["策略", "lot"] + [f"{tenor:g}Y" for tenor in KEY_TENORS]
    rows = []
    for row in summary.itertuples(index=False):
        values = [
            f"{row._asdict().get(f'krd_dev_{tenor:g}', float('nan')):+.4%}" for tenor in KEY_TENORS
        ]
        rows.append([str(row.strategy), _fmt(row.lot, 0), *values])
    lines.append(_md_table(headers, rows))
    lines.append("")

    lines.append("## 3. 归因分解（相对基准，bp of 初始 NAV）")
    lines.append("")
    headers = [
        "策略",
        "lot",
        "carry/roll",
        "level",
        "slope",
        "curvature",
        "other",
        "估值基差",
        "票息",
        "融资",
        "费用",
        "slippage",
        "合计超额",
        "gap",
    ]
    rows = []
    for row in attribution.itertuples(index=False):
        rows.append(
            [
                str(row.strategy),
                _fmt(row.lot, 0),
                f"{row.carry_roll:+.2f}",
                f"{row.level:+.2f}",
                f"{row.slope:+.2f}",
                f"{row.curvature:+.2f}",
                f"{row.other_curve:+.2f}",
                f"{row.valuation_basis:+.2f}",
                f"{row.coupon:+.2f}",
                f"{row.funding:+.2f}",
                f"{row.fees:+.2f}",
                f"{row.slippage:+.2f}",
                f"{row.nav_delta_bp:+.2f}",
                f"{row.gap_bp:+.2e}",
            ]
        )
    lines.append(_md_table(headers, rows))
    lines.append("")
    lines.append("两配置之差（lot 5e6 − lot 1e6）：")
    lines.append("")
    headers = [
        "策略",
        "Δ超额",
        "Δ市场因子",
        "市场占比",
        "Δcarry/roll",
        "Δlevel",
        "Δslope",
        "Δcurvature",
        "Δother",
        "Δ融资+费用+slippage",
        "Δ票息",
        "Δgap",
    ]
    rows = []
    for row in diff.itertuples(index=False):
        cost_delta = row.delta_funding_bp + row.delta_fees_bp + row.delta_slippage_bp
        rows.append(
            [
                str(row.strategy),
                f"{row.delta_excess_bp:+.2f}",
                f"{row.delta_market_bp:+.2f}",
                _pct(row.market_share),
                f"{row.delta_carry_roll_bp:+.2f}",
                f"{row.delta_level_bp:+.2f}",
                f"{row.delta_slope_bp:+.2f}",
                f"{row.delta_curvature_bp:+.2f}",
                f"{row.delta_other_curve_bp:+.2f}",
                f"{cost_delta:+.2f}",
                f"{row.delta_coupon_bp:+.2f}",
                f"{row.identity_gap_bp:+.2e}",
            ]
        )
    lines.append(_md_table(headers, rows))
    lines.append("")
    lines.append(
        "> 注：`市场占比 = Δ市场因子 / Δ超额`；**|占比| > 100% 表示市场因子的变化大于 Δ超额，"
        "其余分项（成本/票息/时点）反向抵消；< 0 表示市场因子与 Δ超额方向相反**。"
    )
    lines.append("")

    lines.append("## 4. 结论：是否来自不同的主动风险暴露？")
    lines.append("")
    for row in diff.itertuples(index=False):
        lines.append(_conclusion_paragraph(row._asdict()))
    lines.append("")
    verdicts = {str(row.strategy): str(row.excess_source) for row in diff.itertuples(index=False)}
    yes_strategies = [name for name, verdict in verdicts.items() if verdict == EXCESS_SOURCE_YES]
    if yes_strategies:
        lines.append(
            f"**总体回答**：`{'、'.join(yes_strategies)}` 的 lot 差异**主要来自不同主动风险暴露**"
            "（整手取整改变了实际 KRD/久期暴露路径；部分策略的成本项与市场因子反向抵消，"
            '见上表 `市场占比`）；逐条结论见上。因此不能把 lot=5M 当作"更优参数"，'
            "它只是整数手约束下的另一条路径。"
        )
    else:
        lines.append(
            "**总体回答**：没有证据表明 lot 差异来自系统性的主动风险暴露差异；"
            "差异主要由费用/融资/交易时点与取整执行解释（见上表）。"
        )
    lines.append("")
    lines.append("## 5. 限制与声明")
    lines.append("")
    lines.append(
        "- **lot 不是可调参数**：它是市场可成交最小面值（整数手约束），由券种/市场规则决定，"
        "不是策略可以自由优化的参数。本报告只解释两个数值的敏感性，"
        '**不能据此挑选"最优 lot"**，也不把 lot=5M 或 1M 当作确认性结果。'
    )
    lines.append(
        "- 单窗口（2019-01-28→2020-12-31）结果；2s10s lot=5M 仅有极少成交，"
        "差异对单一路径/样本敏感，不能外推为稳健结论。"
    )
    lines.append(
        "- 归因把执行价差留在成交/NAV 桥接内（不重复扣）；`other_curve` 是因子未覆盖期限"
        "变化的部分，作为显式披露而非残差补平。"
    )
    lines.append(
        "- 前向/滚动基准（benchmark_version=rolling）尚在 Lead 侧开发；本报告写的是当前"
        "买持基准口径，滚动基准版本须独立版本化、不与本结果混比。"
    )
    lines.append("")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="lot=5M 的 +33.45bp 主动暴露解释")
    parser.add_argument("--start", type=date.fromisoformat, default=None)
    parser.add_argument("--end", type=date.fromisoformat, default=None)
    parser.add_argument("--config", default=None)
    parser.add_argument("--strategies", default=None, help="逗号分隔（默认 carry_roll,2s10s,rv）")
    parser.add_argument(
        "--benchmark-version",
        choices=("buyhold", "rolling"),
        default="buyhold",
        help="基准版本；引擎尚未支持 rolling 时明确告警并回退 buyhold",
    )
    parser.add_argument("--quick", action="store_true", help="短窗口 + 单策略（测试用）")
    args = parser.parse_args(argv)

    cfg = get_config(args.config)
    resolution = resolve_benchmark_version(cfg, args.benchmark_version)
    if resolution.fell_back:
        print(f"[warn] {resolution.note}")
    panel, funding, calendar = load_market(cfg)
    data_last = panel.index.max().date()
    start = args.start or (QUICK_START if args.quick else DEFAULT_START)
    end = args.end or (QUICK_END if args.quick else DEFAULT_END)
    if end > data_last:
        print(f"[warn] --end {end} 超过数据末日 {data_last}，按 {data_last} 处理")
        end = data_last
    if end <= start:
        parser.error(f"--end({end}) 必须晚于 --start({start})")

    if args.strategies:
        names = [name.strip() for name in args.strategies.split(",") if name.strip()]
    else:
        names = list(QUICK_STRATEGIES if args.quick else DEFAULT_STRATEGIES)
    if "zero_tilt" in names:
        parser.error("lot 暴露解释只针对有主动观点的策略（carry_roll/2s10s/rv）")
    factories = build_strategy_factories(names, panel, resolution.cfg, end)

    summary_rows: list[dict[str, Any]] = []
    attribution_rows: list[dict[str, Any]] = []
    diff_rows: list[dict[str, Any]] = []
    config_hash = resolution.cfg.hash()
    for strategy_name, factory in factories:
        print(
            f"\n[lot-exposure] 策略 {strategy_name}（{start} → {end}，benchmark={resolution.effective}）"
        )
        per_lot: dict[float, tuple[BacktestResult, dict[str, float], dict[str, Any]]] = {}
        for lot in (BASE_LOT, ALT_LOT):
            lot_cfg = _lot_config(resolution.cfg, lot)
            result = run_backtest(
                factory(),
                panel=panel,
                calendar=calendar,
                funding=funding,
                cfg=lot_cfg,
                start=start,
                end=end,
                **resolution.run_kwargs,
            )
            summary = _summary_row(
                strategy_name,
                lot,
                result,
                lot_cfg,
                start=start,
                end=end,
                benchmark_version=resolution.effective,
            )
            benchmark_attr = _attribute(
                "benchmark",
                result,
                panel=panel,
                calendar=calendar,
                cfg=lot_cfg,
                strategy_side=False,
            )
            strategy_attr = _attribute(
                strategy_name,
                result,
                panel=panel,
                calendar=calendar,
                cfg=lot_cfg,
                strategy_side=True,
                benchmark=benchmark_attr,
            )
            excess = excess_attribution_bp(
                strategy_attr, benchmark_attr, initial_nav=float(summary["initial_nav"])
            )
            attribution = _attribution_row(
                strategy_name,
                lot,
                excess,
                start=start,
                end=end,
                initial_nav=float(summary["initial_nav"]),
                avg_nav=float(summary["avg_nav"]),
            )
            summary_rows.append(summary)
            attribution_rows.append(attribution)
            per_lot[lot] = (result, excess, summary)
            gap = float(attribution["gap_bp"])
            if abs(gap) > 1e-6:
                raise RuntimeError(
                    f"{strategy_name} lot={lot:g} 归因恒等式 gap={gap:.3e}bp 超容差（实现错误）"
                )
            print(
                f"  lot={lot:g}: excess={summary['excess_bp']:+.4f}bp, "
                f"trades={int(summary['n_trades'])}, avg|DV01r−1|={summary['avg_abs_dv01_dev']:.4%}, "
                f"归因 gap={gap:.2e}bp"
            )
        _base_result, base_attr, base_summary = per_lot[BASE_LOT]
        _alt_result, alt_attr, alt_summary = per_lot[ALT_LOT]
        diff_rows.append(
            _diff_row(
                strategy_name,
                base_summary,
                alt_summary,
                base_attr,
                alt_attr,
                start=start,
                end=end,
                benchmark_version=resolution.effective,
                config_hash=config_hash,
            )
        )

    summary_frame = pd.DataFrame(summary_rows, columns=list(LOT_SUMMARY_COLUMNS))
    attribution_frame = pd.DataFrame(attribution_rows, columns=list(LOT_ATTRIBUTION_COLUMNS))
    diff_frame = pd.DataFrame(diff_rows, columns=list(LOT_DIFF_COLUMNS))
    out = cfg.paths.abs("reports")
    out.mkdir(parents=True, exist_ok=True)
    summary_frame.to_csv(out / "lot_exposure_summary.csv", index=False, encoding="utf-8-sig")
    attribution_frame.to_csv(
        out / "lot_exposure_attribution.csv", index=False, encoding="utf-8-sig"
    )
    diff_frame.to_csv(out / "lot_exposure_diff.csv", index=False, encoding="utf-8-sig")
    _write_markdown(
        path=out / "lot_exposure.md",
        summary=summary_frame,
        attribution=attribution_frame,
        diff=diff_frame,
        start=start,
        end=end,
        benchmark_version=resolution.effective,
        config_hash=config_hash,
    )

    print("\n=== 各配置累计超额与主动暴露 ===")
    columns = [
        "strategy",
        "lot",
        "excess_bp",
        "tracking_error_bp",
        "avg_abs_dv01_dev",
        "max_abs_dv01_dev",
        "avg_abs_duration_dev",
        "krd_abs_dev_max_bucket",
        "krd_abs_dev_max",
        "n_trades",
        "total_turnover",
    ]
    with pd.option_context("display.width", 260):
        print(
            summary_frame[columns].to_string(index=False, float_format=lambda value: f"{value:.4f}")
        )

    print("\n=== 归因差异（lot 5e6 − lot 1e6）===")
    columns = [
        "strategy",
        "delta_excess_bp",
        "delta_market_bp",
        "market_share",
        "delta_avg_abs_dv01_dev",
        "delta_tracking_error_bp",
        "delta_carry_roll_bp",
        "delta_level_bp",
        "identity_gap_bp",
        "excess_source",
    ]
    with pd.option_context("display.width", 260):
        print(diff_frame[columns].to_string(index=False, float_format=lambda value: f"{value:.4f}"))

    print(f"\n产物目录: {out}")
    for name in (
        "lot_exposure_summary.csv",
        "lot_exposure_attribution.csv",
        "lot_exposure_diff.csv",
        "lot_exposure.md",
    ):
        print(f"  - {out / name}")
    print("\n声明：lot 是整数手约束，不是可调参数；本报告不据此挑选最优 lot。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
