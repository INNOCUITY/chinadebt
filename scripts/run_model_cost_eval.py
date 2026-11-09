#!/usr/bin/env python3
"""把 ML 冻结 OOS 预测接入同一账本做成本后评估（不重新训练、不调参）。

输入 `reports/ml_oos_predictions.csv`（run_ml_baseline 导出，含 ridge/tree/train_mean/
literal_zero 的测试窗预测）；对每个 split 的测试窗：
- 原始收益预测 → 协议 `z=clip(raw/z_scale,-1,1)` → KRD 倾斜（10% 最大）
- 同一 `run_backtest` 引擎（成本/票息/融资/滚动/约束）
输出 `reports/model_cost_eval.csv`，报告净超额、TE、成本、换手、拒单与终止义务。
不是模型研究：模型、split、alpha、预测均已冻结。
"""

from __future__ import annotations

import argparse
import sys
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
from ficc.research.protocol import (  # noqa: E402
    ExperimentProtocol,
    evaluate_predictions,
)

log = get_logger("model_cost_eval")
MODELS = {
    "ridge": "pred_ridge",
    "tree": "pred_tree",
    "train_mean": "pred_train_mean",
    "literal_zero": "pred_literal_zero",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="冻结 OOS 预测的成本后评估")
    parser.add_argument("--predictions", default="reports/ml_oos_predictions.csv")
    parser.add_argument("--models", default=",".join(MODELS))
    parser.add_argument("--max-splits", type=int, default=None)
    parser.add_argument("--out-dir", type=Path, default=None)
    args = parser.parse_args(argv)

    cfg = get_config()
    panel, funding, calendar = load_market(cfg)
    protocol = ExperimentProtocol()
    oos = pd.read_csv(args.predictions)
    oos["decision_as_of"] = pd.to_datetime(oos["decision_as_of"]).dt.date
    splits = sorted(oos["split_id"].unique().tolist())
    if args.max_splits is not None:
        splits = splits[: int(args.max_splits)]
    rows: list[dict] = []
    for model_name in args.models.split(","):
        model_name = model_name.strip()
        if model_name not in MODELS:
            raise ValueError(f"未知模型 {model_name}")
        column = MODELS[model_name]
        for split_id in splits:
            part = oos[oos["split_id"] == split_id].sort_values("decision_as_of")
            if len(part) < 2:
                continue
            predictions = {row["decision_as_of"]: float(row[column]) for _, row in part.iterrows()}
            start = min(predictions)
            end = max(predictions)
            terms_day = calendar.prev_trading_day(start, 1)
            terms_curve = Curve(
                as_of=terms_day,
                tenors=np.asarray(sorted(CANONICAL_TENORS), dtype=float),
                rates=panel.loc[pd.Timestamp(terms_day)]
                .reindex(list(CANONICAL_TENORS))
                .to_numpy(dtype=float),
                method="linear",
            )
            evaluation = evaluate_predictions(
                predictions,
                panel,
                funding,
                calendar,
                cfg,
                protocol=protocol,
                start=start,
                end=end,
                initial_curve=terms_curve,
            )
            metrics = evaluation.metrics
            rows.append(
                {
                    "model": model_name,
                    "split_id": int(split_id),
                    "start": str(start),
                    "end": str(end),
                    "n_decision_days": len(predictions),
                    **{
                        k: metrics[k]
                        for k in (
                            "n_days",
                            "active_excess_bp",
                            "tracking_error_bp",
                            "max_drawdown",
                            "total_cost",
                            "total_turnover",
                            "n_trades",
                            "n_rejected",
                            "avg_abs_dv01_ratio_dev",
                        )
                    },
                }
            )
    table = pd.DataFrame(rows)
    if table.empty:
        raise ValueError("没有可评估的 split")
    out = Path(args.out_dir) if args.out_dir is not None else cfg.paths.abs("reports")
    out.mkdir(parents=True, exist_ok=True)
    table.to_csv(out / "model_cost_eval.csv", index=False, encoding="utf-8-sig")
    summary = (
        table.groupby("model")
        .agg(
            n_splits=("split_id", "nunique"),
            n_days=("n_days", "sum"),
            mean_active_excess_bp=("active_excess_bp", "mean"),
            mean_tracking_error_bp=("tracking_error_bp", "mean"),
            total_cost=("total_cost", "sum"),
            total_turnover=("total_turnover", "sum"),
            total_trades=("n_trades", "sum"),
            total_rejected=("n_rejected", "sum"),
            mean_abs_dv01_dev=("avg_abs_dv01_ratio_dev", "mean"),
        )
        .reset_index()
    )
    summary.to_csv(out / "model_cost_eval_summary.csv", index=False, encoding="utf-8-sig")
    print("\n=== 冻结 OOS 预测的成本后评估（同一账本/协议）===")
    print(summary.to_string(index=False, float_format=lambda x: f"{x:.4f}"))
    print("\n注意：模型/预测/split 均已冻结；这里只增加成本后组合评估，不是模型研究或 alpha 结论。")
    log.info("产物: %s, %s", out / "model_cost_eval.csv", out / "model_cost_eval_summary.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
