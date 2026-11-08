#!/usr/bin/env python3
"""固定持仓账本归因脚本（计划 §12.10 第 3 条 / Phase 2 第 5 步）。

对**策略账本**与**基准账本**分别做账本归因：

1. 精确桥接：``ΔNAV = 价格 + 票息 + 本金 − 融资 − 费用 + 成交结算 + 未结算变动 + 交易日内平衡项``；
   残差单独报告（金额与 NAV bp），**不回填**任何分项；
2. 逐券价格拆分：``carry_roll``（同一曲线、时间前进重估）与 ``curve_pnl``（曲线变动）；
3. 曲线因子拆分：level / slope / curvature / other（顺序重估 telescoping，other 显式披露）。

注意：价差已经进入执行价，归因只从 NAV 出发，**不会再次扣减价差**；成本表里的
``spread`` 仅供对照。

用法::

    PYTHONPATH=src .venv/bin/python scripts/run_attribution.py
    PYTHONPATH=src .venv/bin/python scripts/run_attribution.py --quick
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
_SCRIPTS = _ROOT / "scripts"
for _path in (_SRC, _SCRIPTS):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import pandas as pd  # noqa: E402
from run_strategies import (  # noqa: E402
    CarryRollStrategy,
    RelativeValueStrategy,
    SteepenerStrategy,
    ZeroTiltStrategy,
    load_market,
)

from ficc.attribution import AttributionResult, attribute_fixed_holdings  # noqa: E402
from ficc.backtest.engine import BacktestResult, run_backtest  # noqa: E402
from ficc.config import Config, get_config  # noqa: E402
from ficc.logging import get_logger  # noqa: E402

log = get_logger("run_attribution")

DEFAULT_START = date(2019, 1, 28)
DEFAULT_END = date(2020, 12, 31)
QUICK_START = date(2019, 1, 28)
QUICK_END = date(2019, 3, 29)


def _attribute(
    name: str,
    *,
    nav: pd.Series,
    snapshots: pd.DataFrame,
    cashflows: pd.DataFrame,
    trades: pd.DataFrame,
    panel: pd.DataFrame,
    calendar: object,
    cfg: Config,
    benchmark: AttributionResult | None = None,
) -> AttributionResult:
    return attribute_fixed_holdings(
        strategy=name,
        nav=nav,
        snapshots=snapshots,
        cashflows=cashflows,
        trades=trades,
        panel=panel,
        calendar=calendar,
        cfg=cfg,
        benchmark=benchmark,
    )


def _save(result: AttributionResult, out: Path) -> None:
    result.daily.to_csv(
        out / f"attribution_{result.strategy}_daily.csv", index=False, encoding="utf-8-sig"
    )
    result.factors.to_csv(
        out / f"attribution_{result.strategy}_factors.csv", index=False, encoding="utf-8-sig"
    )
    result.bonds.to_csv(
        out / f"attribution_{result.strategy}_bonds.csv", index=False, encoding="utf-8-sig"
    )


def _summary_line(result: AttributionResult) -> dict[str, float | str]:
    daily = result.daily
    factors = result.factors
    residual = daily["bridge_residual"]
    nav_scale = daily["nav"].abs().max()
    return {
        "strategy": result.strategy,
        "n_days": float(len(daily)),
        "max_abs_bridge_residual": float(residual.abs().max()),
        "max_residual_bp": float(result.residual_bp.abs().max()),
        "residual_rel_nav": float(residual.abs().max() / max(nav_scale, 1.0)),
        "price_pnl_sum": float(daily["price_pnl"].sum()),
        "coupon_cash_sum": float(daily["coupon_cash"].sum()),
        "principal_cash_sum": float(daily["principal_cash"].sum()),
        "funding_sum": float(daily["funding"].sum()),
        "fees_sum": float(daily["fees"].sum()),
        "trade_settlement_sum": float(daily["trade_settlement"].sum()),
        "pending_delta_sum": float(daily["pending_delta"].sum()),
        "slippage_sum": float(daily["slippage"].sum()),
        "trade_entry_sum": float(daily.get("trade_entry", pd.Series(dtype=float)).sum()),
        "carry_roll_sum": float(factors["carry_roll"].sum()),
        "curve_pnl_sum": float(factors["curve_pnl"].sum()),
        "level_sum": float(factors["level"].sum()),
        "slope_sum": float(factors["slope"].sum()),
        "curvature_sum": float(factors["curvature"].sum()),
        "other_curve_sum": float(factors["other_curve"].sum()),
        "other_curve_nav_bp_sum": float(factors["other_curve_nav_bp"].sum()),
        "max_abs_valuation_basis": float(daily["valuation_basis"].abs().max()),
        "valuation_breach_days": int(daily["valuation_basis_breach"].sum()),
        "principal_redemption_sum": float(daily["principal_redemption"].sum()),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="固定持仓账本归因")
    parser.add_argument("--start", type=date.fromisoformat, default=None)
    parser.add_argument("--end", type=date.fromisoformat, default=None)
    parser.add_argument("--config", default=None)
    parser.add_argument("--quick", action="store_true", help="短窗口 + 只跑 zero_tilt")
    args = parser.parse_args(argv)

    cfg = get_config(args.config)
    out = cfg.paths.abs("reports")
    out.mkdir(parents=True, exist_ok=True)
    start = args.start or (QUICK_START if args.quick else DEFAULT_START)
    end = args.end or (QUICK_END if args.quick else DEFAULT_END)

    panel, funding, calendar = load_market(cfg)
    _, residual_bp = _fit_residuals(panel, cfg)
    strategies = (
        [ZeroTiltStrategy()]
        if args.quick
        else [
            ZeroTiltStrategy(),
            CarryRollStrategy(),
            SteepenerStrategy(),
            RelativeValueStrategy(residuals=-residual_bp),
        ]
    )

    rows: list[dict[str, float | str]] = []
    for strategy in strategies:
        log.info("回测并归因: %s", strategy.name)
        result: BacktestResult = run_backtest(
            strategy,
            panel=panel,
            calendar=calendar,
            funding=funding,
            cfg=cfg,
            start=start,
            end=end,
        )
        bench_attr = _attribute(
            "benchmark",
            nav=result.benchmark_nav,
            snapshots=result.benchmark_positions,
            cashflows=result.benchmark_cashflows,
            trades=result.benchmark_trades,
            panel=panel,
            calendar=calendar,
            cfg=cfg,
        )
        strategy_attr = _attribute(
            strategy.name,
            nav=result.nav,
            snapshots=result.positions,
            cashflows=result.cashflows,
            trades=result.trades,
            panel=panel,
            calendar=calendar,
            cfg=cfg,
            benchmark=bench_attr,
        )
        _save(strategy_attr, out)
        _save(bench_attr, out)
        rows.append(_summary_line(strategy_attr))
        rows.append(_summary_line(bench_attr))

        # 验收：桥接残差必须为"噪声级"，不是用残差补平
        max_residual = float(strategy_attr.daily["bridge_residual"].abs().max())
        scale = max(float(result.nav.abs().max()), 1.0)
        if int(strategy_attr.daily["valuation_basis_breach"].sum()) > 0:
            log.error(
                "%s 有 %d 天独立估值基差超容差（max=%.6g 元）",
                strategy.name,
                int(strategy_attr.daily["valuation_basis_breach"].sum()),
                float(strategy_attr.daily["valuation_basis"].abs().max()),
            )
            return 1
        if max_residual > 1e-6 + 1e-9 * scale:
            log.error(
                "%s 桥接残差 %.6g 元超过容差（NAV 量级 %.3g）",
                strategy.name,
                max_residual,
                scale,
            )
            return 1
        if strategy_attr.factors["other_curve"].abs().sum() > 0:
            log.info(
                "%s: other_curve 合计 %.2f 元（占 NAV %.3f bp，显式披露）",
                strategy.name,
                strategy_attr.factors["other_curve"].sum(),
                strategy_attr.factors["other_curve_nav_bp"].sum(),
            )

    summary = pd.DataFrame(rows)
    summary.to_csv(out / "attribution_summary.csv", index=False, encoding="utf-8-sig")

    print("\n=== 固定持仓归因（账本独立重估）===")
    cols = [
        "strategy",
        "n_days",
        "max_abs_bridge_residual",
        "residual_rel_nav",
        "price_pnl_sum",
        "carry_roll_sum",
        "level_sum",
        "slope_sum",
        "curvature_sum",
        "other_curve_sum",
        "other_curve_nav_bp_sum",
        "max_abs_valuation_basis",
        "valuation_breach_days",
    ]
    with pd.option_context("display.width", 240):
        print(summary[cols].to_string(index=False, float_format=lambda x: f"{x:.6g}"))
    log.info("产物: %s", out / "attribution_summary.csv")
    return 0


def _fit_residuals(panel: pd.DataFrame, cfg: Config) -> tuple[object, pd.DataFrame]:
    from ficc.curve.fitting import fit_panel

    params, residual_bp = fit_panel(
        panel,
        method=cfg.curve.fit_method,
        weights=cfg.curve.weights,
        param_mode="free",
        residuals_mode="free",
    )
    residual_bp.columns = [float(c) for c in residual_bp.columns]
    return params, residual_bp


if __name__ == "__main__":
    raise SystemExit(main())
