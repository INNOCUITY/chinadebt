#!/usr/bin/env python3
"""长样本滚动回测（§12.12-B 第 1 条）：冻结滚动政策 + 滚动基准单独版本化。

政策（`roll_v1_trigger1y_parissue`，见 `docs/03_methodology.md` §10.14）
- 任一腿剩余期限 < 1.0 年时，在交易日**显式卖旧买新**，新券平价发行（票面 = 当日该期限 YTM）；
- 基准为**滚动阶梯**（`benchmark_version="rolling"`）：每次滚动把该腿换成新券并再平衡回目标市值权重；
- 原买入持有基准（`buyhold`）保留用于 2019–2020 短窗口复现，二者**不可混比**。

样本：2019-01-28 → 2025-12-31。
起点不能更早：2Y 期限在 2016-01-04→2019-01-25 缺失（中债未发布/数据缺口），
更早的连续样本需要插值 2Y，属未验收假设，因此不在此脚本内悄悄补。

用法::

    PYTHONPATH=src .venv/bin/python scripts/run_long_sample.py
    PYTHONPATH=src .venv/bin/python scripts/run_long_sample.py --quick
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

from ficc.backtest.engine import run_backtest  # noqa: E402
from ficc.config import Config, get_config  # noqa: E402
from ficc.logging import get_logger  # noqa: E402
from ficc.portfolio.rolling import ROLL_POLICY_VERSION  # noqa: E402

log = get_logger("long_sample")

START = date(2019, 1, 28)
END = date(2025, 12, 31)
QUICK_END = date(2021, 12, 31)


def rolling_config(cfg: Config) -> Config:
    """开启冻结滚动政策，并强制使用滚动基准（版本化）。"""
    rolling = cfg.portfolio.rolling.model_copy(update={"enabled": True})
    return cfg.model_copy(
        update={
            "portfolio": cfg.portfolio.model_copy(update={"rolling": rolling}),
            "backtest": cfg.backtest.model_copy(update={"benchmark_version": "rolling"}),
        }
    )


def metrics(result, cfg: Config) -> dict[str, float | str]:
    nav = result.nav
    bench = result.benchmark_nav
    rets = nav.pct_change().dropna()
    bench_rets = bench.pct_change().dropna()
    n = len(nav)
    ann = cfg.backtest.annual_days
    total = float(nav.iloc[-1] / nav.iloc[0] - 1.0)
    ann_ret = float((1.0 + total) ** (ann / max(n - 1, 1)) - 1.0)
    vol = float(rets.std(ddof=0) * np.sqrt(ann))
    dd = nav / nav.cummax() - 1.0
    bench_total = float(bench.iloc[-1] / bench.iloc[0] - 1.0)
    active = rets.loc[rets.index.intersection(bench_rets.index)] - bench_rets
    te = float(active.std(ddof=0) * np.sqrt(ann)) if len(active) > 1 else float("nan")
    exp = result.exposure
    held = result.constraints[result.constraints["n_positions"] > 0]
    return {
        "strategy": result.strategy,
        "benchmark_version": result.benchmark_version,
        "rolling_version": result.rolling_version,
        "n_days": float(n),
        "total_return": total,
        "ann_return": ann_ret,
        "ann_vol": vol,
        "sharpe": float((ann_ret - cfg.benchmark.risk_free_fallback_const) / vol)
        if vol > 0
        else float("nan"),
        "max_drawdown": float(dd.min()),
        "bench_total_return": bench_total,
        "excess_bp": float((total - bench_total) * 1e4),
        "tracking_error": te,
        "n_trades": float(len(result.trades)),
        "n_rejected": float(len(result.rejected)),
        "total_fee": float(result.costs["fee"].sum()),
        "total_funding": float(result.costs["funding"].sum()),
        "total_spread": float(result.costs["spread"].sum()),
        "total_cost": float(result.costs["total"].sum()),
        "bench_spread": float(result.costs["bench_spread"].sum()),
        "total_turnover": float(result.turnover.sum()),
        "avg_abs_dv01_ratio_dev": float((exp["dv01_ratio"] - 1.0).abs().mean()),
        "avg_duration_dev": float((exp["duration"] - exp["bench_duration"]).abs().mean()),
        "n_constraint_violations": float((~held["ok"]).sum()),
        "n_days_held": float(len(held)),
    }


def _save(result, out: Path) -> None:
    frames = {
        "nav": result.nav.rename("nav").to_frame(),
        "benchmark_nav": result.benchmark_nav.rename("benchmark_nav").to_frame(),
        "costs": result.costs,
        "trades": result.trades,
        "benchmark_trades": result.benchmark_trades,
        "constraints": result.constraints,
    }
    for name, frame in frames.items():
        frame.to_csv(
            out / f"long_sample_{result.strategy}_{name}.csv",
            index=(name not in {"trades", "benchmark_trades"}),
            encoding="utf-8-sig",
        )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="长样本滚动回测（滚动政策 roll_v1）")
    parser.add_argument("--start", type=date.fromisoformat, default=START)
    parser.add_argument("--end", type=date.fromisoformat, default=None)
    parser.add_argument("--config", default=None)
    parser.add_argument("--quick", action="store_true")
    args = parser.parse_args(argv)

    cfg = rolling_config(get_config(args.config))
    end = args.end or (QUICK_END if args.quick else END)
    out = cfg.paths.abs("reports")
    out.mkdir(parents=True, exist_ok=True)
    if cfg.backtest.benchmark_version != "rolling":  # pragma: no cover - 防御式
        raise RuntimeError("长样本脚本要求滚动基准版本")

    panel, funding, calendar = load_market(cfg)
    log.info(
        "长样本: %s → %s，滚动政策 %s，基准版本 %s",
        args.start,
        end,
        ROLL_POLICY_VERSION,
        cfg.backtest.benchmark_version,
    )

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
        ZeroTiltStrategy(),
        CarryRollStrategy(),
        SteepenerStrategy(),
        RelativeValueStrategy(residuals=-residual_bp),
    ]

    rows: list[dict[str, float | str]] = []
    for strategy in strategies:
        log.info("回测: %s", strategy.name)
        result = run_backtest(
            strategy,
            panel=panel,
            calendar=calendar,
            funding=funding,
            cfg=cfg,
            start=args.start,
            end=end,
        )
        _save(result, out)
        rows.append(metrics(result, cfg))

    summary = pd.DataFrame(rows)
    summary["start"] = str(args.start)
    summary["end"] = str(end)
    summary["config_hash"] = cfg.hash()
    summary.to_csv(out / "long_sample_summary.csv", index=False, encoding="utf-8-sig")

    zero = summary.loc[summary["strategy"] == "zero_tilt"].iloc[0]
    # 滚动政策在换券日会产生结构性 KRD 跳变（信号 t 与执行 t+1 之间发生滚动），
    # 零 tilt 控制组允许 ≤2bp 基线漂移；超过说明基准/执行口径不一致。
    if abs(float(zero["excess_bp"])) > 2.0:
        log.error(
            "零 tilt 控制超额 %.4f bp 超过 2bp：滚动基准与策略执行口径可能不一致",
            zero["excess_bp"],
        )
        return 1

    print("\n=== 长样本滚动回测（基准版本 rolling）===")
    cols = [
        "strategy",
        "n_days",
        "total_return",
        "ann_return",
        "sharpe",
        "max_drawdown",
        "excess_bp",
        "tracking_error",
        "n_trades",
        "total_fee",
        "total_funding",
        "total_spread",
        "total_cost",
        "total_turnover",
        "avg_abs_dv01_ratio_dev",
        "n_constraint_violations",
    ]
    with pd.option_context("display.width", 260):
        print(summary[cols].to_string(index=False, float_format=lambda x: f"{x:.4f}"))
    print(
        f"\n零 tilt 控制：超额 {float(zero['excess_bp']):.4f} bp；"
        f"滚动政策 {ROLL_POLICY_VERSION}；基准版本 {cfg.backtest.benchmark_version}"
    )
    log.info("产物: %s", out / "long_sample_summary.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
