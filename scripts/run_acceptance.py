#!/usr/bin/env python3
"""统一验收入口（计划 §12.12）：策略/基准/现金流/独立估值/归因在同一入口实跑。

对每个策略（含零 tilt 控制组）执行：

1. 引擎回测（T+1、成本、约束闸门）；
2. 归因（精确桥接 + carry/roll + 因子 + **独立估值检查**）；
3. 对账（现金/持仓/NAV/PnL 双路径/全价/票息权益/**归因闭合**，7 项实际执行）；
4. 约束与零 tilt 控制验收；
5. 汇总为 ``reports/acceptance_summary.csv``；任一 FAIL → exit 1，不再标"全部通过"。

用法::

    PYTHONPATH=src .venv/bin/python scripts/run_acceptance.py
    PYTHONPATH=src .venv/bin/python scripts/run_acceptance.py --quick
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

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from run_strategies import (  # noqa: E402
    CarryRollStrategy,
    RelativeValueStrategy,
    SteepenerStrategy,
    ZeroTiltStrategy,
    load_market,
)

from ficc.attribution import attribute_fixed_holdings  # noqa: E402
from ficc.attribution.ledger_attribution import (  # noqa: E402
    BASIS_MATRIX,
    FACTOR_MATRIX,
)
from ficc.backtest.engine import run_backtest  # noqa: E402
from ficc.config import get_config  # noqa: E402
from ficc.logging import get_logger  # noqa: E402
from ficc.portfolio.reconcile import reconcile_all  # noqa: E402

log = get_logger("acceptance")

DEFAULT_START = date(2019, 1, 28)
DEFAULT_END = date(2020, 12, 31)
QUICK_START = date(2019, 1, 28)
QUICK_END = date(2019, 3, 29)

BRIDGE_ABS_TOL = 1e-5  # 元（真实 480 天实测 ≤3.6e-8）
ZERO_TILT_EXCESS_TOL_BP = 1.0


def _attribution_components(attr) -> pd.Series:
    daily = attr.daily
    return pd.Series(
        {
            "price": float(daily["price_pnl"].sum()),
            "coupon": float(daily["coupon_cash"].sum()),
            "principal_net": float((daily["principal_cash"] - daily["principal_redemption"]).sum()),
            "funding": -float(daily["funding"].sum()),
            "fees": -float(daily["fees"].sum()),
            "trade_settlement": float(daily["trade_settlement"].sum()),
            "pending_delta": float(daily["pending_delta"].sum()),
            "slippage": float(daily["slippage"].sum()),
            "trade_entry": float(daily["trade_entry"].sum()),
        }
    )


def _add(rows: list[dict], strategy: str, check: str, passed: bool, detail: str) -> None:
    rows.append(
        {
            "strategy": strategy,
            "check": check,
            "status": "PASS" if passed else "FAIL",
            "detail": detail,
        }
    )


def _run_one(name, strategy, panel, funding, calendar, cfg) -> None:
    log.info("验收: %s", name)
    result = run_backtest(
        strategy, panel=panel, calendar=calendar, funding=funding, cfg=cfg, start=START, end=END
    )
    bench = attribute_fixed_holdings(
        strategy="benchmark",
        nav=result.benchmark_nav,
        snapshots=result.benchmark_positions,
        cashflows=result.benchmark_cashflows,
        trades=result.benchmark_trades,
        panel=panel,
        calendar=calendar,
        cfg=cfg,
    )
    attr = attribute_fixed_holdings(
        strategy=name,
        nav=result.nav,
        snapshots=result.positions,
        cashflows=result.cashflows,
        trades=result.trades,
        panel=panel,
        calendar=calendar,
        cfg=cfg,
        benchmark=bench,
    )

    rows: list[dict] = []
    max_residual = float(attr.daily["bridge_residual"].abs().max())
    _add(
        rows,
        name,
        "attribution_bridge",
        max_residual <= BRIDGE_ABS_TOL,
        f"max|bridge_residual|={max_residual:.3e} 元（容差 {BRIDGE_ABS_TOL:g}）",
    )
    max_basis = float(attr.daily["valuation_basis"].abs().max())
    breaches = int(attr.daily["valuation_basis_breach"].sum())
    _add(
        rows,
        name,
        "independent_valuation",
        breaches == 0,
        f"max|valuation_basis|={max_basis:.6g} 元, breach_days={breaches}",
    )
    identity_error = float(np.abs(FACTOR_MATRIX @ BASIS_MATRIX - np.eye(3)).max())
    _add(
        rows,
        name,
        "factor_identity",
        identity_error <= 1e-12,
        f"max|F@B−I|={identity_error:.3e}",
    )
    factor_sum_error = float(
        (
            attr.factors[["level", "slope", "curvature", "other_curve"]].sum(axis=1)
            - attr.factors["curve_pnl"]
        )
        .abs()
        .max()
    )
    _add(
        rows,
        name,
        "factor_telescoping",
        factor_sum_error <= 1e-6,
        f"max|四因子和−curve_pnl|={factor_sum_error:.3e} 元",
    )
    bench_residual = float(bench.daily["bridge_residual"].abs().max())
    _add(
        rows,
        name,
        "benchmark_bridge",
        bench_residual <= BRIDGE_ABS_TOL,
        f"benchmark max|bridge_residual|={bench_residual:.3e} 元",
    )

    reconciliation = reconcile_all(
        cashflows=result.cashflows,
        trades=result.trades,
        snapshots=result.positions,
        nav=result.nav,
        attribution=_attribution_components(attr),
        calendar=calendar,
    )
    executed = [check for check in reconciliation.checks if check.executed]
    failed = [check.name for check in executed if not check.passed]
    _add(
        rows,
        name,
        "ledger_reconciliation",
        not failed,
        f"执行 {len(executed)} 项；失败: {failed or '无'}",
    )
    not_run = [check.name for check in reconciliation.checks if not check.executed]
    _add(
        rows,
        name,
        "reconciliation_coverage",
        not not_run,
        f"未执行: {not_run or '无'}",
    )

    held = result.constraints[result.constraints["n_positions"] > 0]
    cv_fail = int((~held["ok"]).sum())
    _add(
        rows,
        name,
        "constraints_held_days",
        cv_fail == 0,
        f"有持仓日 {len(held)} 天，违规 {cv_fail} 天",
    )

    total_ret = result.nav.iloc[-1] / result.nav.iloc[0] - 1.0
    bench_ret = result.benchmark_nav.iloc[-1] / result.benchmark_nav.iloc[0] - 1.0
    excess_bp = (total_ret - bench_ret) * 1e4
    if name == "zero_tilt":
        _add(
            rows,
            name,
            "zero_tilt_control",
            abs(excess_bp) <= ZERO_TILT_EXCESS_TOL_BP,
            f"excess={excess_bp:.4f}bp（阈值 ±{ZERO_TILT_EXCESS_TOL_BP:g}bp），"
            f"成交 {len(result.trades)} 笔",
        )
    return rows, result, attr


def main(argv: list[str] | None = None) -> int:
    global START, END
    parser = argparse.ArgumentParser(description="统一验收（策略+归因+独立估值+对账）")
    parser.add_argument("--start", type=date.fromisoformat, default=None)
    parser.add_argument("--end", type=date.fromisoformat, default=None)
    parser.add_argument("--config", default=None)
    parser.add_argument("--quick", action="store_true")
    args = parser.parse_args(argv)

    cfg = get_config(args.config)
    START = args.start or (QUICK_START if args.quick else DEFAULT_START)
    END = args.end or (QUICK_END if args.quick else DEFAULT_END)

    panel, funding, calendar = load_market(cfg)
    from ficc.curve.fitting import fit_panel

    _, residual_bp = fit_panel(
        panel,
        method=cfg.curve.fit_method,
        weights=cfg.curve.weights,
        param_mode="free",
        residuals_mode="free",
    )
    residual_bp.columns = [float(c) for c in residual_bp.columns]

    strategies = [("zero_tilt", ZeroTiltStrategy())]
    if not args.quick:
        strategies += [
            ("carry_roll", CarryRollStrategy()),
            ("2s10s", SteepenerStrategy()),
            ("rv", RelativeValueStrategy(residuals=-residual_bp)),
        ]

    rows: list[dict] = []
    for name, strategy in strategies:
        strategy_rows, _result, _attr = _run_one(name, strategy, panel, funding, calendar, cfg)
        rows.extend(strategy_rows)

    summary = pd.DataFrame(rows)
    out = cfg.paths.abs("reports")
    out.mkdir(parents=True, exist_ok=True)
    summary.to_csv(out / "acceptance_summary.csv", index=False, encoding="utf-8-sig")

    print("\n=== 统一验收（策略 + 归因 + 独立估值 + 对账）===")
    with pd.option_context("display.width", 220):
        print(summary.to_string(index=False))
    failed = summary["status"] != "PASS"
    print(f"\n总计 {len(summary)} 项：PASS {int((~failed).sum())} / FAIL {int(failed.sum())}")
    if failed.any():
        log.error("验收未通过：%s", summary.loc[failed, "check"].tolist())
        return 1
    log.info("验收全部通过（归因已实际执行，不再是 not run）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
