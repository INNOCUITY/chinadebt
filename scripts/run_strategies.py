#!/usr/bin/env python3
"""三个基线策略的回测研究脚本（Phase 2 第 4 步）。

产出
----
- `reports/strategy_summary.csv`：三策略 + 买入持有基准的绩效/成本/换手/暴露汇总
- `reports/strategy_<name>_{nav,exposure,trades,costs,turnover,constraints}.csv`
- `reports/benchmark_buyhold_nav.csv`

窗口默认 `2019-01-28 → 2020-12-31`：**刻意限制在 2 年**，因为当前投资宇宙是
在起点一次性生成的合成券（2/5/10/30Y），更长的窗口会让 2Y 券到期而没有"新券发行"
机制（债券滚动/新发是下一阶段工作）。脚本会打印该限制。

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
from ficc.curve.curve import Curve  # noqa: E402
from ficc.data.loader import load_curve_long, load_funding_series  # noqa: E402
from ficc.data.schema import CANONICAL_TENORS  # noqa: E402
from ficc.logging import get_logger  # noqa: E402
from ficc.portfolio.benchmark import build_benchmark, build_universe_bonds  # noqa: E402
from ficc.portfolio.ledger import Ledger, TradeRecord  # noqa: E402
from ficc.pricing.bond import dirty_price_from_ytm  # noqa: E402
from ficc.pricing.conventions import accrued_interest  # noqa: E402
from ficc.strategy.carry_rolldown import CarryRollStrategy  # noqa: E402
from ficc.strategy.curve_steepener import SteepenerStrategy  # noqa: E402
from ficc.strategy.relative_value import RelativeValueStrategy  # noqa: E402
from ficc.utils.calendar import TradingCalendar  # noqa: E402

log = get_logger("run_strategies")

DEFAULT_START = date(2019, 1, 28)
DEFAULT_END = date(2020, 12, 31)


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


def curve_at(panel: pd.DataFrame, d: date) -> Curve:
    row = panel.loc[pd.Timestamp(d)].reindex(CANONICAL_TENORS)
    return Curve(
        as_of=d, tenors=np.asarray(CANONICAL_TENORS), rates=tuple(row.values), method="hermite"
    )


# ---------------------------------------------------------------------------
# 买入持有基准（与策略同账本、同成本假设，但不调仓）
# ---------------------------------------------------------------------------
def run_buy_and_hold(
    panel: pd.DataFrame,
    funding: pd.Series,
    calendar: TradingCalendar,
    cfg: Config,
    start: date,
    end: date,
) -> pd.Series:
    days = list(calendar.trading_days_between(start, end))
    curve0 = curve_at(panel, days[0])
    universe = build_universe_bonds(curve0, days[0], cfg=cfg)
    bench = build_benchmark(curve0, days[0], bonds=universe, cfg=cfg)

    fallback = cfg.costs.repo_rate_fallback_const * 100.0
    fmap = {}
    for d in days:
        prior = funding.loc[:d]
        fmap[d] = float(prior.iloc[-1]) if len(prior) else fallback

    ledger = Ledger(
        calendar,
        initial_cash=cfg.backtest.init_cash,
        funding_rate_pct=fmap[days[0]],
        funding_rate_by_date=fmap,
        cfg=cfg,
    )
    lag = int(cfg.pricing.settlement_lag_days)
    curve_exec = curve_at(panel, days[1])

    for leg in bench.legs:
        settle = calendar.settlement_date(days[1], lag)
        remaining = (leg.bond.maturity_date - settle).days / 365.25
        ytm = float(curve_exec.rate(remaining))
        dirty = dirty_price_from_ytm(leg.bond, settle, ytm)
        clean = dirty - accrued_interest(leg.bond, settle)
        ledger.submit(
            TradeRecord(
                trade_id=f"bench-{leg.bond.code}",
                bond=leg.bond,
                side="buy",
                face_value=leg.face_value,
                clean_price=clean,
                signal_date=days[0],
                trade_date=days[1],
                settlement_date=settle,
            )
        )

    nav: dict[date, float] = {}
    for d in days:
        snap = ledger.process_day(d, curve_at(panel, d))
        nav[d] = snap.nav
    return pd.Series(nav, name="nav").sort_index()


# ---------------------------------------------------------------------------
# 绩效指标
# ---------------------------------------------------------------------------
def metrics(result: BacktestResult, benchmark_nav: pd.Series, cfg: Config) -> dict[str, float]:
    nav = result.nav
    rets = nav.pct_change().dropna()
    n = len(nav)
    ann_factor = cfg.backtest.annual_days
    total_ret = nav.iloc[-1] / nav.iloc[0] - 1.0
    ann_ret = (1.0 + total_ret) ** (ann_factor / max(n - 1, 1)) - 1.0
    vol = float(rets.std(ddof=0) * np.sqrt(ann_factor))
    rf = cfg.benchmark.risk_free_fallback_const * 100.0 / 100.0
    sharpe = (ann_ret - rf) / vol if vol > 0 else float("nan")
    dd = nav / nav.cummax() - 1.0
    bench_rets = benchmark_nav.pct_change().dropna().reindex(rets.index).dropna()
    common = rets.index.intersection(bench_rets.index)
    active = rets.loc[common] - bench_rets.loc[common]
    tracking_error = (
        float(active.std(ddof=0) * np.sqrt(ann_factor)) if len(active) > 1 else float("nan")
    )
    bench_total = benchmark_nav.iloc[-1] / benchmark_nav.iloc[0] - 1.0
    return {
        "total_return": total_ret,
        "ann_return": ann_ret,
        "ann_vol": vol,
        "sharpe": sharpe,
        "max_drawdown": float(dd.min()),
        "bench_total_return": float(bench_total),
        "excess_total_return": float(total_ret - bench_total),
        "tracking_error": tracking_error,
        "n_trades": len(result.trades),
        "total_fee": float(result.costs["fee"].sum()),
        "total_funding": float(result.costs["funding"].sum()),
        "total_cost": float(result.costs["total"].sum()),
        "total_turnover": float(result.turnover.sum()),
        "avg_daily_turnover": float(result.turnover.mean()),
        "n_constraint_violations": int((~result.constraints["ok"]).sum()),
        "avg_abs_dv01_ratio_dev": float((result.exposure["dv01_ratio"] - 1.0).abs().mean()),
        "avg_duration": float(result.exposure["duration"].mean()),
        "avg_total_dv01": float(result.exposure["total_dv01"].mean()),
    }


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="三个基线策略回测")
    parser.add_argument("--start", type=date.fromisoformat, default=DEFAULT_START)
    parser.add_argument("--end", type=date.fromisoformat, default=DEFAULT_END)
    parser.add_argument("--config", default=None)
    args = parser.parse_args(argv)

    cfg = get_config(args.config)
    out = cfg.paths.abs("reports")
    out.mkdir(parents=True, exist_ok=True)

    panel, funding, calendar = load_market(cfg)
    log.warning(
        "回测窗口 %s → %s（刻意限制：投资宇宙在起点一次性生成，2Y 券到期后无新发机制）",
        args.start,
        args.end,
    )

    log.info("跑买入持有基准账本…")
    bench_nav = run_buy_and_hold(panel, funding, calendar, cfg, args.start, args.end)
    bench_nav.to_csv(out / "benchmark_buyhold_nav.csv", encoding="utf-8-sig")

    # RV 需要"逐日残差面板"：一次性拟合（每日本身独立），避免回测中每天现场拟合 window 天
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
        CarryRollStrategy(),
        SteepenerStrategy(),
        RelativeValueStrategy(residuals=observed_minus_fitted),
    ]
    summary_rows = []
    for strategy in strategies:
        log.info("回测策略: %s", strategy.name)
        result = run_backtest(
            strategy,
            panel=panel,
            calendar=calendar,
            funding=funding,
            cfg=cfg,
            start=args.start,
            end=args.end,
        )
        frames = {
            "nav": result.nav.rename("nav").to_frame(),
            "exposure": result.exposure,
            "trades": result.trades,
            "costs": result.costs,
            "turnover": result.turnover.rename("turnover").to_frame(),
            "constraints": result.constraints,
        }
        for name, frame in frames.items():
            frame.to_csv(
                out / f"strategy_{strategy.name}_{name}.csv",
                index=(name != "trades"),
                encoding="utf-8-sig",
            )
        row = {"strategy": strategy.name, **metrics(result, bench_nav, cfg)}
        summary_rows.append(row)

    summary = pd.DataFrame(summary_rows)
    summary.to_csv(out / "strategy_summary.csv", index=False, encoding="utf-8-sig")

    print("\n=== 策略汇总（对比买入持有基准）===")
    cols = [
        "strategy",
        "total_return",
        "ann_return",
        "ann_vol",
        "sharpe",
        "max_drawdown",
        "excess_total_return",
        "tracking_error",
        "n_trades",
        "total_cost",
        "total_turnover",
        "n_constraint_violations",
        "avg_abs_dv01_ratio_dev",
    ]
    with pd.option_context("display.width", 220):
        print(summary[cols].to_string(index=False, float_format=lambda x: f"{x:.4f}"))
    print(f"\n买入持有基准总收益: {summary['bench_total_return'].iloc[0]:.4f}")
    log.info("产物: %s", out / "strategy_summary.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
