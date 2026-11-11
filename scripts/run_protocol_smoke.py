#!/usr/bin/env python3
"""共同实验协议冒烟评估（**不是模型研究**）：零/常数预测 → 成本后组合评估。

验证 ``src/ficc/research/protocol.py`` 的冻结映射与同一引擎成本口径：
- z=0（零预测）回到基准，组合净收益应≈基准（含建仓成本）；
- 常数 z=±0.5/±1 只用于验证映射边界与成本/暴露计算，不构成 alpha 结论；
- 输出 ``reports/protocol_smoke.csv`` 与协议 manifest。

模型研究（Ridge/树/RL 多 seed）在协议冻结后另行开展，不在本脚本。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
for _p in (_ROOT / "src", _ROOT / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import pandas as pd  # noqa: E402
from run_strategies import load_market  # noqa: E402

from ficc.config import get_config  # noqa: E402
from ficc.logging import get_logger  # noqa: E402
from ficc.research.protocol import (  # noqa: E402
    ExperimentProtocol,
    evaluate_predictions,
)

log = get_logger("protocol_smoke")

DEFAULT_START = date(2019, 2, 1)
DEFAULT_END = date(2021, 12, 31)
QUICK_END = date(2019, 6, 28)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="共同实验协议冒烟评估")
    parser.add_argument("--start", type=date.fromisoformat, default=DEFAULT_START)
    parser.add_argument("--end", type=date.fromisoformat, default=None)
    parser.add_argument("--step", type=int, default=5)
    parser.add_argument("--quick", action="store_true")
    parser.add_argument(
        "--out-dir", type=Path, default=None, help="输出目录（默认 reports；测试用临时目录）"
    )
    args = parser.parse_args(argv)

    cfg = get_config()
    panel, funding, calendar = load_market(cfg)
    end = args.end or (QUICK_END if args.quick else DEFAULT_END)
    days = list(calendar.trading_days_between(args.start, end))
    if len(days) < args.step + 2:
        raise ValueError("样本太短")
    decision_days = days[:: args.step]
    protocol = ExperimentProtocol(
        decision_step_days=int(args.step),
        metadata={"script": "scripts/run_protocol_smoke.py", "quick": args.quick},
    )

    # 输入是**原始预期收益**（小数），协议用 z_scale=0.01 转 z：±0.005→±0.5，±0.01→±1
    scenarios = {
        "zero": 0.0,
        "const_plus_half": 0.005,
        "const_minus_half": -0.005,
        "const_plus_one": 0.01,
    }
    rows: list[dict] = []
    for name, value in scenarios.items():
        predictions = {day: value for day in decision_days}
        evaluation = evaluate_predictions(
            predictions,
            panel,
            funding,
            calendar,
            cfg,
            protocol=protocol,
            start=args.start,
            end=end,
        )
        rows.append({"scenario": name, "prediction": value, **evaluation.metrics})
        log.info(
            "%s: active=%.2fbp TE=%.2fbp cost=%.0f turnover=%.3f trades=%d rejected=%d",
            name,
            evaluation.metrics["active_excess_bp"],
            evaluation.metrics["tracking_error_bp"],
            evaluation.metrics["total_cost"],
            evaluation.metrics["total_turnover"],
            evaluation.metrics["n_trades"],
            evaluation.metrics["n_rejected"],
        )

    table = pd.DataFrame(rows)
    out = Path(args.out_dir) if args.out_dir is not None else cfg.paths.abs("reports")
    out.mkdir(parents=True, exist_ok=True)
    table.to_csv(out / "protocol_smoke.csv", index=False, encoding="utf-8-sig")
    from ficc.research.protocol import protocol_fingerprint

    manifest = {
        "protocol": protocol.describe(),
        "fingerprint": protocol_fingerprint(cfg, protocol),
        "start": args.start.isoformat(),
        "end": end.isoformat(),
        "n_decision_days": len(decision_days),
        "scenarios": list(scenarios),
        "note": "冒烟评估只验证协议映射/成本口径，不是模型研究或 alpha 结论",
    }
    (out / "protocol_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print("\n=== 协议冒烟（零/常数预测，非模型研究）===")
    cols = [
        "scenario",
        "prediction",
        "total_return",
        "active_excess_bp",
        "tracking_error_bp",
        "total_cost",
        "total_turnover",
        "n_trades",
        "n_rejected",
    ]
    print(table[cols].to_string(index=False, float_format=lambda x: f"{x:.6f}"))
    print(f"\n协议版本: {protocol.version}；产物: {out / 'protocol_smoke.csv'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
