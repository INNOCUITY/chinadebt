#!/usr/bin/env python3
"""跨入口控制实验（R57–R60 后的训练基线冒烟）：同一网格/固定目标面值比较

- ML 评估器：`evaluate_target_faces`（同一 `run_backtest` 引擎）
- RL rollout：`RebalanceEnv(..., decision_step_days=5).rollout(policy)`
- RL step：同一调度逐步 `step`（O(n²) 调试路径）

三者应产生相同订单、NAV 与终止净值（策略名/trade_id 前缀除外）。这不是模型研究。
"""

from __future__ import annotations

import argparse
import dataclasses
import sys
from datetime import date
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
for _p in (_ROOT / "src", _ROOT / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from run_strategies import load_market  # noqa: E402

from ficc.config import get_config  # noqa: E402
from ficc.curve.curve import Curve  # noqa: E402
from ficc.data.schema import CANONICAL_TENORS  # noqa: E402
from ficc.logging import get_logger  # noqa: E402
from ficc.portfolio.benchmark import build_benchmark, build_universe_bonds  # noqa: E402
from ficc.portfolio.types import TargetFaces  # noqa: E402
from ficc.research.protocol import (  # noqa: E402
    ExperimentProtocol,
    evaluate_target_faces,
    protocol_config,
)
from ficc.rl import (  # noqa: E402
    ActionSpec,
    RebalanceEnv,
    TerminalState,
    terminal_state_from_result,
)
from ficc.utils.calendar import TradingCalendar  # noqa: E402

log = get_logger("cross_entry")

DEFAULT_START = date(2019, 2, 1)  # 前面至少 4 个交易日预热（panel 从 01-28 起）
DEFAULT_END = date(2019, 3, 29)


def _terms_curve(
    panel: pd.DataFrame, calendar: TradingCalendar, start: date, lag: int = 1
) -> Curve:
    """R62：初始券条款/基准身份使用 start−lag 的曲线（引擎与 policy 同源）。"""
    day = calendar.prev_trading_day(start, lag)
    rates = panel.loc[pd.Timestamp(day)].reindex(list(CANONICAL_TENORS)).to_numpy(dtype=float)
    return Curve(
        as_of=day,
        tenors=np.asarray(sorted(CANONICAL_TENORS), dtype=float),
        rates=rates,
        method="linear",
    )


def _initial_faces(
    panel: pd.DataFrame, calendar: TradingCalendar, cfg, day: date
) -> dict[str, float]:
    curve = _terms_curve(panel, calendar, day)
    bonds = build_universe_bonds(curve, curve.as_of, cfg=cfg)
    bench = build_benchmark(curve, curve.as_of, bonds=bonds, calendar=calendar, cfg=cfg)
    return {code: float(face) for code, face in bench.faces.items()}


def _compare(a, b, label: str) -> dict[str, float]:
    ta = a.trades.reset_index(drop=True)
    tb = b.trades.reset_index(drop=True)
    same_cols = ("side", "face_value", "clean_price", "trade_date", "settlement_date")
    order_diff = 0.0
    if len(ta) != len(tb):
        order_diff = float(abs(len(ta) - len(tb)))
    else:
        for col in same_cols:
            if col in ("side", "trade_date", "settlement_date"):
                if ta[col].tolist() != tb[col].tolist():
                    order_diff += 1.0
            else:
                order_diff = max(
                    order_diff,
                    float(np.abs(ta[col].to_numpy() - tb[col].to_numpy()).max(initial=0)),
                )
    nav_common = a.nav.index.intersection(b.nav.index)
    nav_diff = float(
        np.abs(a.nav.loc[nav_common].to_numpy() - b.nav.loc[nav_common].to_numpy()).max()
    )
    log.info("%s: order_diff=%s nav_diff=%.6g", label, order_diff, nav_diff)
    return {"order_diff": order_diff, "nav_max_abs_diff": nav_diff}


def _terminal_diff(a: TerminalState, b: TerminalState) -> dict[str, float]:
    return {
        "terminal_nav_diff": abs(float(a.nav_mark_to_market) - float(b.nav_mark_to_market)),
        "terminal_cash_diff": abs(float(a.cash) - float(b.cash)),
        "terminal_unsettled_cash_diff": abs(float(a.unsettled_cash) - float(b.unsettled_cash)),
        "terminal_accrued_fees_diff": abs(float(a.accrued_fees) - float(b.accrued_fees)),
        "terminal_net_diff": abs(float(a.net_terminal_value) - float(b.net_terminal_value)),
        "terminal_unsettled_count_diff": float(
            abs(len(a.unsettled_trades) - len(b.unsettled_trades))
        ),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="跨入口控制实验（ML/RL/step）")
    parser.add_argument("--start", type=date.fromisoformat, default=DEFAULT_START)
    parser.add_argument("--end", type=date.fromisoformat, default=DEFAULT_END)
    parser.add_argument("--out-dir", type=Path, default=None)
    parser.add_argument(
        "--tamper-fee",
        type=float,
        default=0.0,
        help="测试用：给 rollout 终止费用加一个偏移，验证独立终止对照会失败",
    )
    args = parser.parse_args(argv)

    cfg = get_config()
    panel, funding, calendar = load_market(cfg)
    protocol = ExperimentProtocol()
    aligned = protocol_config(cfg, protocol)
    start, end = args.start, args.end
    days = list(calendar.trading_days_between(start, end))
    grid = protocol.decision_dates(calendar, start, end)
    terms_curve = _terms_curve(panel, calendar, start)
    faces = _initial_faces(panel, calendar, aligned, start)
    targets = {day: faces for day in grid}

    out = Path(args.out_dir) if args.out_dir is not None else cfg.paths.abs("reports")
    out.mkdir(parents=True, exist_ok=True)

    # 1) ML/协议评估器（固定目标面值）
    ml = evaluate_target_faces(
        targets,
        panel,
        funding,
        calendar,
        cfg,
        protocol=protocol,
        start=start,
        end=end,
        initial_curve=terms_curve,
    )

    # 2) RL rollout（同一网格、同一目标面值）
    env = RebalanceEnv(
        panel,
        calendar,
        funding,
        aligned,
        information_lag_days=1,
        decision_step_days=protocol.decision_step_days,
    )
    env.reset(start, end)

    def policy(ctx):
        return TargetFaces(as_of=ctx.as_of, faces=dict(faces), notes="cross_entry")

    rollout = env.rollout(policy, start, end, name="cross_entry")
    terminal = rollout.terminal

    # 3) step 逐日路径（同调度）
    env_step = RebalanceEnv(
        panel,
        calendar,
        funding,
        aligned,
        information_lag_days=1,
        decision_step_days=protocol.decision_step_days,
    )
    env_step.reset(start, end)
    grid_set = set(grid)
    for day in days[:-1]:
        action = (
            ActionSpec(action_type="target_faces", target_faces=dict(faces))
            if day in grid_set
            else ActionSpec()
        )
        env_step.step(action)
    step_result = env_step.result
    assert step_result is not None

    ml_terminal = terminal_state_from_result(ml.result, calendar=calendar, cfg=aligned)
    rollout_terminal = terminal
    if args.tamper_fee:
        rollout_terminal = dataclasses.replace(
            rollout_terminal,
            accrued_fees=float(rollout_terminal.accrued_fees) + float(args.tamper_fee),
        )
    step_terminal = terminal_state_from_result(step_result, calendar=calendar, cfg=aligned)

    m1 = {
        **_compare(ml.result, rollout.result, "ml_vs_rollout"),
        **_terminal_diff(ml_terminal, rollout_terminal),
    }
    m2 = {
        **_compare(ml.result, step_result, "ml_vs_step"),
        **_terminal_diff(ml_terminal, step_terminal),
    }
    m3 = {
        **_compare(rollout.result, step_result, "rollout_vs_step"),
        **_terminal_diff(rollout_terminal, step_terminal),
    }
    summary = pd.DataFrame(
        [
            {"check": "ml_vs_rollout", **m1},
            {"check": "ml_vs_step", **m2},
            {"check": "rollout_vs_step", **m3},
        ]
    )
    summary["start"] = str(start)
    summary["end"] = str(end)
    summary["protocol_version"] = protocol.version
    summary["decision_step_days"] = protocol.decision_step_days
    summary["terminal_nav"] = rollout_terminal.nav_mark_to_market
    summary["terminal_unsettled_cash"] = rollout_terminal.unsettled_cash
    summary["terminal_accrued_fees"] = rollout_terminal.accrued_fees
    summary["terminal_net"] = rollout_terminal.net_terminal_value
    summary.to_csv(out / "cross_entry_check.csv", index=False, encoding="utf-8-sig")
    print(summary.to_string(index=False, float_format=lambda x: f"{x:.6g}"))
    # ML 与 RL 的策略名不同，trade_id 前缀允许不同；订单字段/NAV/终止义务必须一致。
    terminal_cols = [
        column
        for column in summary.columns
        if column.startswith("terminal_") and column.endswith("_diff")
    ]
    ok = bool(
        summary["nav_max_abs_diff"].max() <= 1e-6
        and summary["order_diff"].max() <= 1e-6
        and summary[terminal_cols].max().max() <= 1e-6
    )
    if not ok:
        log.error("跨入口控制实验未通过：%s", summary.to_dict("records"))
        return 1
    log.info("跨入口控制实验通过（NAV/订单/终止净值一致）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
