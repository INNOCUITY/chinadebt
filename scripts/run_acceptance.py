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
    ExactReplayStrategy,
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
ROLLING_START = date(2019, 1, 28)
ROLLING_END = date(2021, 12, 31)

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


def _recon_detail(rows: list[dict], name: str, reconciliation) -> None:
    """把 7 项对账逐项写成独立行（现金/持仓/票息权益/归因闭合等显式可审计）。"""
    for check in reconciliation.checks:
        if not check.executed:
            continue
        _add(rows, name, f"recon_{check.name}", bool(check.passed), check.detail[:240])


def _risk_small_shock_check(rows: list[dict], name: str, panel, calendar, cfg) -> None:
    """固定组合蝶式 0.001bp：全重估 vs 同源 KRD 的一阶收敛（<1e-4）。"""
    try:
        from ficc.curve.curve import Curve
        from ficc.data.schema import CANONICAL_TENORS
        from ficc.portfolio.benchmark import build_benchmark, build_universe_bonds
        from ficc.risk.stress import stress_test

        day = END  # 用验收窗口末日，避免 T+1 超出日历末尾
        rates = panel.loc[pd.Timestamp(day)].to_numpy(dtype=float)
        curve = Curve(
            as_of=day,
            tenors=np.asarray(sorted(CANONICAL_TENORS), dtype=float).copy(),
            rates=rates,
            method="linear",
        )
        bonds = build_universe_bonds(curve, day, cfg=cfg)
        bench = build_benchmark(curve, day, bonds=bonds, calendar=calendar, cfg=cfg)
        scenarios = [{"type": "butterfly", "name": "[收敛]蝶式0.001bp", "bp": 0.001}]
        results = stress_test(
            bench.faces, list(bonds), curve, scenarios=scenarios, calendar=calendar, cfg=cfg
        )
        error_raw = getattr(results[0], "error_ratio", None)
        error = float("nan") if error_raw is None else float(error_raw)
        _add(
            rows,
            name,
            "risk_small_shock_convergence",
            bool(error == error and error < 1e-4),
            f"蝶式0.001bp 相对误差={error:.3e}（阈值 1e-4；全重估与 KRD 同源即期）",
        )
    except Exception as exc:
        _add(rows, name, "risk_small_shock_convergence", False, f"检查失败: {exc}")


def _run_one(
    name,
    strategy,
    panel,
    funding,
    calendar,
    cfg,
    *,
    causality: bool = False,
    do_risk: bool = False,
) -> None:
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
    _recon_detail(rows, name, reconciliation)
    if do_risk:
        _risk_small_shock_check(rows, name, panel, calendar, cfg)

    held = result.constraints[result.constraints["n_positions"] > 0]
    cv_fail = int((~held["ok"]).sum())
    _add(
        rows,
        name,
        "constraints_held_days",
        cv_fail == 0,
        f"有持仓日 {len(held)} 天，违规 {cv_fail} 天",
    )

    if getattr(result, "roll_events", None) is not None and len(result.roll_events):
        events = result.roll_events
        missing = int((events["status"] == "missing_strategy_trade").sum())
        rejected = events[events["status"] == "rejected_constraint"]
        _add(
            rows,
            name,
            "roll_event_same_day",
            missing == 0,
            f"滚动事件 {len(events)} 个，缺策略同日成交 {missing} 个，约束拒绝 {len(rejected)} 个",
        )
        unexplained = rejected[rejected["reason"].fillna("") == ""]
        _add(
            rows,
            name,
            "roll_event_rejection_audited",
            len(unexplained) == 0,
            f"约束拒绝 {len(rejected)} 个，缺原因 {len(unexplained)} 个",
        )
        bench_dates = set(pd.to_datetime(events["date"]).dt.date)
        strat_dates = (
            set(pd.to_datetime(result.trades["trade_date"]).dt.date)
            if len(result.trades)
            else set()
        )
        _add(
            rows,
            name,
            "roll_event_dates",
            bench_dates <= strat_dates,
            f"基准换券日 {sorted(bench_dates)}，策略缺 {sorted(bench_dates - strat_dates)}",
        )
        # 跨换券归因：归因逐日索引必须覆盖换券日，且换券日桥接残差仍在容差内
        attr_dates = set(pd.to_datetime(attr.daily["date"]).dt.date)
        uncovered = sorted(bench_dates - attr_dates)
        daily_dates = pd.to_datetime(attr.daily["date"]).dt.date
        roll_residual = float(
            attr.daily.loc[daily_dates.isin(bench_dates), "bridge_residual"].abs().max()
        )
        _add(
            rows,
            name,
            "attribution_across_rolls",
            not uncovered and roll_residual <= BRIDGE_ABS_TOL,
            f"换券日 {len(bench_dates)} 个，归因缺 {uncovered}，换券日残差={roll_residual:.3e} 元",
        )

    total_ret = result.nav.iloc[-1] / result.nav.iloc[0] - 1.0
    bench_ret = result.benchmark_nav.iloc[-1] / result.benchmark_nav.iloc[0] - 1.0
    excess_bp = (total_ret - bench_ret) * 1e4
    if name.startswith("zero_tilt"):
        tol = (
            2.0
            if getattr(result, "benchmark_version", "") == "rolling"
            else ZERO_TILT_EXCESS_TOL_BP
        )
        _add(
            rows,
            name,
            "zero_tilt_control",
            abs(excess_bp) <= tol,
            f"excess={excess_bp:.4f}bp（阈值 ±{tol:g}bp），成交 {len(result.trades)} 笔",
        )
    if causality:
        cutoff = START + (END - START) // 2
        panel2 = panel.copy()
        future = panel2.index > pd.Timestamp(cutoff)
        panel2.loc[future, :] = panel2.loc[future, :] * 1.5
        funding2 = funding.copy()
        funding2.loc[funding2.index > pd.Timestamp(cutoff)] = (
            funding2.loc[funding2.index > pd.Timestamp(cutoff)] + 3.0
        )
        perturbed = run_backtest(
            strategy,
            panel=panel2,
            calendar=calendar,
            funding=funding2,
            cfg=cfg,
            start=START,
            end=END,
        )
        nav_base = result.nav.loc[result.nav.index <= cutoff]
        nav_pert = perturbed.nav.loc[perturbed.nav.index <= cutoff]
        ok_nav = nav_base.index.equals(nav_pert.index) and np.allclose(
            nav_base.to_numpy(), nav_pert.to_numpy(), rtol=0, atol=1e-9
        )
        tb = result.trades[result.trades["trade_date"] <= cutoff].reset_index(drop=True)
        tp = perturbed.trades[perturbed.trades["trade_date"] <= cutoff].reset_index(drop=True)
        ok_trades = len(tb) == len(tp) and all(
            tb[col].tolist() == tp[col].tolist()
            for col in ("trade_id", "side", "face_value", "clean_price", "settlement_date")
        )
        _add(
            rows,
            name,
            "future_perturbation_prefix",
            bool(ok_nav and ok_trades),
            f"cutoff={cutoff}，nav_prefix_equal={ok_nav}，trade_prefix_equal={ok_trades}",
        )

    return rows, result, attr


def main(argv: list[str] | None = None) -> int:
    global START, END
    parser = argparse.ArgumentParser(description="统一验收（策略+归因+独立估值+对账）")
    parser.add_argument("--start", type=date.fromisoformat, default=None)
    parser.add_argument("--end", type=date.fromisoformat, default=None)
    parser.add_argument("--config", default=None)
    parser.add_argument("--quick", action="store_true")
    parser.add_argument(
        "--rolling",
        action="store_true",
        help="滚动政策 roll_v1 + 滚动基准版本（2019–2021），覆盖长样本执行/滚动契约",
    )
    args = parser.parse_args(argv)

    cfg = get_config(args.config)
    if args.rolling:
        from run_long_sample import rolling_config

        cfg = rolling_config(cfg)
    START = args.start or (
        QUICK_START if args.quick else (ROLLING_START if args.rolling else DEFAULT_START)
    )
    END = args.end or (QUICK_END if args.quick else (ROLLING_END if args.rolling else DEFAULT_END))

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

    strategies = [
        ("zero_tilt_exact", ExactReplayStrategy())
        if args.rolling
        else ("zero_tilt", ZeroTiltStrategy())
    ]
    if not args.quick:
        strategies += [
            ("carry_roll", CarryRollStrategy()),
            ("2s10s", SteepenerStrategy()),
            ("rv", RelativeValueStrategy(residuals=-residual_bp)),
        ]

    rows: list[dict] = []
    for index, (name, strategy) in enumerate(strategies):
        strategy_rows, _result, _attr = _run_one(
            name,
            strategy,
            panel,
            funding,
            calendar,
            cfg,
            causality=bool(args.rolling and index == 0),
            do_risk=(index == 0),
        )
        rows.extend(strategy_rows)

    summary = pd.DataFrame(rows)
    summary["start"] = str(START)
    summary["end"] = str(END)
    summary["benchmark_version"] = str(cfg.backtest.benchmark_version)
    summary["rolling_version"] = (
        "roll_v1_trigger1y_parissue" if cfg.portfolio.rolling.enabled else ""
    )
    summary["config_hash"] = cfg.hash()
    out = cfg.paths.abs("reports")
    out.mkdir(parents=True, exist_ok=True)
    summary_path = out / (
        "acceptance_summary_rolling.csv" if args.rolling else "acceptance_summary.csv"
    )
    summary.to_csv(summary_path, index=False, encoding="utf-8-sig")

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
