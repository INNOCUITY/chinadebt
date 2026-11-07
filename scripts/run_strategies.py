#!/usr/bin/env python3
"""三个基线策略 + 零 tilt 控制组的回测研究（Phase 2 第 4 步，R22–R27 修复后）。

关键口径（见 docs/03 §10.7–10.10）
----------------------------------
- **基准**：买入持有固定面值，风险基准与绩效基准同一组合；引擎内置同成本口径的
  基准账本（`benchmark_nav`），不再由本脚本单独构造；
- **零 tilt 控制**：每天返回 `TargetKRD(krd=ctx.benchmark.krd)`，用于度量
  "整手取整/成交前投影/日期口径"本身带来的基线差异（必须接近 0，单独报告）；
- **成本**：手续费/结算费 + 融资 + 买卖价差（半价差进入执行价并单独记账）；
- 窗口默认 2 年：投资宇宙在起点一次性生成，无新发/到期滚动机制。

用法::

    PYTHONPATH=src .venv/bin/python scripts/run_strategies.py
    PYTHONPATH=src .venv/bin/python scripts/run_strategies.py --end 2020-06-30
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from ficc.backtest.engine import BacktestResult, run_backtest  # noqa: E402
from ficc.config import Config, get_config  # noqa: E402
from ficc.data.loader import load_curve_long, load_funding_series  # noqa: E402
from ficc.data.schema import CANONICAL_TENORS  # noqa: E402
from ficc.logging import get_logger  # noqa: E402
from ficc.portfolio.types import MarketContext, TargetKRD  # noqa: E402
from ficc.strategy.carry_rolldown import CarryRollStrategy  # noqa: E402
from ficc.strategy.curve_steepener import SteepenerStrategy  # noqa: E402
from ficc.strategy.relative_value import RelativeValueStrategy  # noqa: E402
from ficc.utils.calendar import TradingCalendar  # noqa: E402

log = get_logger("run_strategies")

DEFAULT_START = date(2019, 1, 28)
DEFAULT_END = date(2020, 12, 31)


class ZeroTiltStrategy:
    """控制组：每天要求"完全回到基准 KRD"（无观点）。"""

    name = "zero_tilt"

    def target_krd(self, ctx: MarketContext) -> TargetKRD:
        return TargetKRD(as_of=ctx.as_of, krd=dict(ctx.benchmark.krd), notes="控制组：零 tilt")


# ---------------------------------------------------------------------------
# 数据
# ---------------------------------------------------------------------------
def load_market(cfg: Config) -> tuple[pd.DataFrame, pd.Series, TradingCalendar]:
    long = load_curve_long(cfg=cfg)
    panel = (
        long.pivot(index="date", columns="tenor", values="ytm_pct")
        .sort_index()
        .reindex(columns=list(CANONICAL_TENORS))
    )
    funding = load_funding_series(cfg=cfg).set_index("date")["FDR007"].sort_index()
    return panel, funding, TradingCalendar.from_dates(panel.index)


# ---------------------------------------------------------------------------
# 绩效指标（基准来自引擎内部同口径账本）
# ---------------------------------------------------------------------------
def metrics(result: BacktestResult, cfg: Config) -> dict[str, float]:
    nav = result.nav
    bench_nav = result.benchmark_nav
    rets = nav.pct_change().dropna()
    n = len(nav)
    ann_factor = cfg.backtest.annual_days
    total_ret = nav.iloc[-1] / nav.iloc[0] - 1.0
    ann_ret = (1.0 + total_ret) ** (ann_factor / max(n - 1, 1)) - 1.0
    vol = float(rets.std(ddof=0) * np.sqrt(ann_factor))
    rf = cfg.benchmark.risk_free_fallback_const
    sharpe = (ann_ret - rf) / vol if vol > 0 else float("nan")
    dd = nav / nav.cummax() - 1.0
    bench_rets = bench_nav.pct_change().dropna()
    common = rets.index.intersection(bench_rets.index)
    active = rets.loc[common] - bench_rets.loc[common]
    tracking_error = (
        float(active.std(ddof=0) * np.sqrt(ann_factor)) if len(active) > 1 else float("nan")
    )
    bench_total = bench_nav.iloc[-1] / bench_nav.iloc[0] - 1.0
    return {
        "total_return": float(total_ret),
        "ann_return": float(ann_ret),
        "ann_vol": vol,
        "sharpe": float(sharpe),
        "max_drawdown": float(dd.min()),
        "bench_total_return": float(bench_total),
        "excess_total_return": float(total_ret - bench_total),
        "excess_bp": float((total_ret - bench_total) * 1e4),
        "tracking_error": tracking_error,
        "n_trades": len(result.trades),
        "n_rejected": len(result.rejected),
        "total_fee": float(result.costs["fee"].sum()),
        "total_funding": float(result.costs["funding"].sum()),
        "total_spread": float(result.costs["spread"].sum()),
        "total_cost": float(result.costs["total"].sum()),
        "bench_spread": float(result.costs["bench_spread"].sum()),
        "total_turnover": float(result.turnover.sum()),
        "avg_daily_turnover": float(result.turnover.mean()),
        "n_constraint_violations": int((~result.constraints["ok"]).sum()),
        "avg_abs_dv01_ratio_dev": float((result.exposure["dv01_ratio"] - 1.0).abs().mean()),
        "avg_duration": float(result.exposure["duration"].mean()),
        "avg_total_dv01": float(result.exposure["total_dv01"].mean()),
    }


def _save(result: BacktestResult, out: Path) -> None:
    frames = {
        "nav": result.nav.rename("nav").to_frame(),
        "benchmark_nav": result.benchmark_nav.rename("benchmark_nav").to_frame(),
        "exposure": result.exposure,
        "trades": result.trades,
        "cashflows": result.cashflows,
        "positions": result.positions,
        "benchmark_positions": result.benchmark_positions,
        "benchmark_trades": result.benchmark_trades,
        "benchmark_cashflows": result.benchmark_cashflows,
        "costs": result.costs,
        "turnover": result.turnover.rename("turnover").to_frame(),
        "constraints": result.constraints,
        "signals": result.signals,
        "rejected": result.rejected,
    }
    for name, frame in frames.items():
        frame.to_csv(
            out / f"strategy_{result.strategy}_{name}.csv",
            index=(name not in {"trades", "rejected", "benchmark_trades"}),
            encoding="utf-8-sig",
        )


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="三个基线策略回测（含零 tilt 控制组）")
    parser.add_argument("--start", type=date.fromisoformat, default=DEFAULT_START)
    parser.add_argument("--end", type=date.fromisoformat, default=DEFAULT_END)
    parser.add_argument("--config", default=None)
    args = parser.parse_args(argv)

    cfg = get_config(args.config)
    out = cfg.paths.abs("reports")
    out.mkdir(parents=True, exist_ok=True)

    panel, funding, calendar = load_market(cfg)
    log.warning(
        "回测窗口 %s → %s（投资宇宙固定 4 只合成券、无新发/滚动，限制为 2 年）",
        args.start,
        args.end,
    )

    log.info("预计算 NSS 残差面板（供 RV 策略使用）…")
    from ficc.curve.fitting import fit_panel

    _params, residual_bp = fit_panel(
        panel,
        method=cfg.curve.fit_method,
        weights=cfg.curve.weights,
        param_mode="free",
        residuals_mode="free",
    )
    residual_bp.columns = [float(c) for c in residual_bp.columns]
    observed_minus_fitted = -residual_bp  # fit_panel 约定残差 = 拟合 − 观测

    strategies = [
        ZeroTiltStrategy(),
        CarryRollStrategy(),
        SteepenerStrategy(),
        RelativeValueStrategy(residuals=observed_minus_fitted),
    ]
    summary_rows = []
    for strategy in strategies:
        log.info("回测: %s", strategy.name)
        result = run_backtest(
            strategy,
            panel=panel,
            calendar=calendar,
            funding=funding,
            cfg=cfg,
            start=args.start,
            end=args.end,
        )
        _save(result, out)
        summary_rows.append({"strategy": strategy.name, **metrics(result, cfg)})

    summary = pd.DataFrame(summary_rows)
    summary["config_hash"] = cfg.hash()
    summary["start"] = str(args.start)
    summary["end"] = str(args.end)
    summary.to_csv(out / "strategy_summary.csv", index=False, encoding="utf-8-sig")

    zero = summary.loc[summary["strategy"] == "zero_tilt"].iloc[0]
    log.info(
        "零 tilt 控制: 超额 %.4f bp, 成交 %d 笔, 成本 %.2f 元（基准价差 %.2f）",
        zero["excess_bp"],
        zero["n_trades"],
        zero["total_cost"],
        zero["bench_spread"],
    )
    if abs(zero["excess_bp"]) > 1.0:
        log.error("零 tilt 控制超额 %.4f bp 超过 1bp：基准/日期口径可能仍不一致", zero["excess_bp"])
        return 1

    print("\n=== 策略汇总（基准 = 引擎内同口径买入持有）===")
    cols = [
        "strategy",
        "total_return",
        "ann_return",
        "ann_vol",
        "sharpe",
        "max_drawdown",
        "excess_bp",
        "tracking_error",
        "n_trades",
        "n_rejected",
        "total_fee",
        "total_funding",
        "total_spread",
        "total_cost",
        "total_turnover",
        "n_constraint_violations",
        "avg_abs_dv01_ratio_dev",
    ]
    with pd.option_context("display.width", 260):
        print(summary[cols].to_string(index=False, float_format=lambda x: f"{x:.4f}"))
    print(f"\n零 tilt 控制：超额 {zero['excess_bp']:.4f} bp（验收阈值 ±1bp）")
    print(f"买入持有基准总收益: {summary['bench_total_return'].iloc[0]:.4f}")
    log.info("产物: %s", out / "strategy_summary.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
