#!/usr/bin/env python3
"""冻结 OOS 预测的**连续基准成本后评估**（R64–R67）。

- 基准/标签严格同源：一条从 2019-01-28 起持续滚动（roll_v1）的基准；标签
  `ml_dataset.csv` 的 label 来自同一 `benchmark_nav` 口径。
- 每条预测在**下一交易日**执行：模型预测日之外按零预测（目标=基准 KRD）持续跟踪，
  因此不再按 split 重新发行基准，也不丢末端预测。
- 每个模型单独跑一条连续 episode（同一账本/成本/约束/滚动），报告相对连续基准的
  active excess、TE、成本、换手、拒单、终止义务；零预测控制（全部日零目标）作为基线。
- 模型/split/alpha/预测均冻结；只做成本后诊断，不是模型研究/alpha 结论。

输入：`reports/ml_oos_predictions.csv`（run_ml_baseline 导出）与 `reports/ml_dataset.csv`。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
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
    protocol_config,
    protocol_fingerprint,
)

log = get_logger("model_cost_eval")
MODELS = {
    "ridge": "pred_ridge",
    "tree": "pred_tree",
    "train_mean": "pred_train_mean",
    "literal_zero": "pred_literal_zero",
}
START = date(2019, 2, 1)
END = date(2025, 12, 31)


def _run(
    name: str,
    predictions: dict | None,
    panel,
    funding,
    calendar,
    cfg,
    protocol,
    initial_curve,
):
    evaluation = evaluate_predictions(
        predictions or {},
        panel,
        funding,
        calendar,
        cfg,
        protocol=protocol,
        start=START,
        end=END,
        initial_curve=initial_curve,
    )
    m = evaluation.metrics
    requested = float(cfg.constraints.rebalance_band_lots)
    row = {
        "model": name,
        "requested_band_lots": requested,
        "effective_min_trade_face": float(
            max(requested, 1.0) * float(cfg.constraints.min_trade_lot)
        ),
        **m,
    }
    log.info(
        "%s: active=%.2fbp net_active=%.2fbp TE=%.2fbp cost=%.0f trades=%d rejected=%d",
        name,
        m["active_excess_bp"],
        m["net_active_excess_bp"],
        m["tracking_error_bp"],
        m["total_cost"],
        m["n_trades"],
        m["n_rejected"],
    )
    return row


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="冻结 OOS 预测的连续基准成本后评估")
    parser.add_argument("--predictions", default="reports/ml_oos_predictions.csv")
    parser.add_argument("--models", default=",".join(MODELS))
    parser.add_argument("--band-lots", type=float, default=1.0)
    parser.add_argument("--out-dir", type=Path, default=None)
    args = parser.parse_args(argv)

    cfg = get_config()
    # 评估执行带（R70）：生产主配置 2 lot；冻结弱预测在 2 lot 下大多被最小整手吸收。
    # 探索配置显式取 1 lot；注意 build_orders 的有效最小成交面值 = max(band, 1 lot)，
    # 因此 requested=1 lot 与 effective 都是 1 lot（请求 0.25 会静默变成 1 lot）。
    requested_band_lots = float(args.band_lots)
    if requested_band_lots <= 0:
        raise ValueError("--band-lots 必须为正")
    eval_constraints = cfg.constraints.model_copy(
        update={"rebalance_band_lots": requested_band_lots}
    )
    cfg = cfg.model_copy(update={"constraints": eval_constraints})
    panel, funding, calendar = load_market(cfg)
    # R68：连续复盘的缺失预测策略是"仅公共网格日零目标，非网格日 hold"；
    # 这是独立协议版本，指纹与标准 hold 协议不同。
    protocol = ExperimentProtocol(
        version="protocol_v1_continuous_eval",
        missing_prediction_policy="benchmark_on_grid",
    )
    initial_curve = None  # 默认入口：start−lag 完整曲线（R72）

    oos = pd.read_csv(args.predictions)
    oos["decision_as_of"] = pd.to_datetime(oos["decision_as_of"]).dt.date
    if oos["decision_as_of"].duplicated().any():
        raise ValueError("OOS 预测日期重复：同一日期只能属于一个测试窗")

    rows = []
    # 零预测控制：全部决策日零目标（跟踪连续基准），作为模型增量基线
    control = _run("zero_control", None, panel, funding, calendar, cfg, protocol, initial_curve)
    rows.append(control)
    for model_name in args.models.split(","):
        model_name = model_name.strip()
        if model_name not in MODELS:
            raise ValueError(f"未知模型 {model_name}")
        column = MODELS[model_name]
        predictions = {row["decision_as_of"]: float(row[column]) for _, row in oos.iterrows()}
        rows.append(
            _run(
                model_name,
                predictions,
                panel,
                funding,
                calendar,
                cfg,
                protocol,
                initial_curve,
            )
        )

    table = pd.DataFrame(rows)
    control_active = float(control["active_excess_bp"])
    control_cost = float(control["total_cost"])
    table["incremental_active_vs_zero_bp"] = table["active_excess_bp"] - control_active
    table["incremental_net_active_vs_zero_bp"] = table["net_active_excess_bp"] - float(
        control["net_active_excess_bp"]
    )
    table["incremental_cost_vs_zero"] = table["total_cost"] - control_cost

    out = Path(args.out_dir) if args.out_dir is not None else cfg.paths.abs("reports")
    out.mkdir(parents=True, exist_ok=True)
    suffix = "" if abs(requested_band_lots - 1.0) < 1e-12 else f"_{requested_band_lots:g}lot"
    detail_path = out / f"model_cost_eval{suffix}.csv"
    table.to_csv(detail_path, index=False, encoding="utf-8-sig")
    summary_cols = [
        "model",
        "n_days",
        "active_excess_bp",
        "net_active_excess_bp",
        "incremental_active_vs_zero_bp",
        "incremental_net_active_vs_zero_bp",
        "incremental_cost_vs_zero",
        "tracking_error_bp",
        "benchmark_terminal_accrued_fees",
        "benchmark_terminal_unsettled_cash",
        "net_benchmark_return",
        "total_cost",
        "total_turnover",
        "n_trades",
        "n_rejected",
        "terminal_accrued_fees",
        "terminal_unsettled_cash",
    ]
    summary_path = out / f"model_cost_eval_summary{suffix}.csv"
    table[summary_cols].to_csv(summary_path, index=False, encoding="utf-8-sig")

    def _sha256(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    try:
        git_head = subprocess.check_output(
            ["git", "rev-parse", "HEAD"], cwd=_ROOT, text=True, stderr=subprocess.DEVNULL
        ).strip()
    except (OSError, subprocess.CalledProcessError):
        git_head = "unknown"
    resolved_cfg = protocol_config(cfg, protocol)
    source_files = [
        "src/ficc/research/protocol.py",
        "src/ficc/rl/env.py",
        "scripts/run_model_cost_eval.py",
    ]
    dirty = subprocess.check_output(["git", "status", "--porcelain"], cwd=_ROOT, text=True).strip()
    manifest = {
        "protocol_version": protocol.version,
        "protocol_fingerprint": protocol_fingerprint(resolved_cfg, protocol),
        "resolved_benchmark_version": resolved_cfg.backtest.benchmark_version,
        "resolved_rolling": resolved_cfg.portfolio.rolling.model_dump(mode="json"),
        "resolved_costs": resolved_cfg.costs.model_dump(mode="json"),
        "resolved_pricing": resolved_cfg.pricing.model_dump(mode="json"),
        "initialization": {
            "policy": "default_available_through_start_minus_lag",
            "initial_curve_as_of": str(
                calendar.prev_trading_day(START, protocol.information_lag_days)
            ),
            "information_lag_days": protocol.information_lag_days,
        },
        "missing_prediction_policy": protocol.missing_prediction_policy,
        "start": str(START),
        "end": str(END),
        "requested_band_lots": requested_band_lots,
        "effective_min_trade_face": float(
            max(requested_band_lots, 1.0) * float(cfg.constraints.min_trade_lot)
        ),
        "predictions_path": str(args.predictions),
        "predictions_sha256": _sha256(Path(args.predictions)),
        "dataset_sha256": _sha256(Path("reports/ml_dataset.csv")),
        "n_prediction_rows": len(oos),
        "n_models": len(table),
        "models": table["model"].tolist(),
        "n_decisions": {
            str(k): float(v) for k, v in zip(table["model"], table["n_decisions"], strict=True)
        },
        "git_head": git_head,
        "git_dirty": bool(dirty),
        "source_sha256": {path: _sha256(_ROOT / path) for path in source_files},
        "detail_csv": str(detail_path),
        "summary_csv": str(summary_path),
    }
    manifest_path = out / f"model_cost_eval_manifest{suffix}.json"
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8")
    cols = [
        "model",
        "n_days",
        "active_excess_bp",
        "net_active_excess_bp",
        "incremental_active_vs_zero_bp",
        "tracking_error_bp",
        "benchmark_terminal_accrued_fees",
        "benchmark_terminal_unsettled_cash",
        "net_benchmark_return",
        "total_cost",
        "total_turnover",
        "n_trades",
        "n_rejected",
        "terminal_accrued_fees",
        "terminal_unsettled_cash",
    ]
    print("\n=== 冻结 OOS 预测的连续基准成本后评估 ===")
    print(table[cols].to_string(index=False, float_format=lambda x: f"{x:.4f}"))
    print(
        "\n说明：所有收益相对**同一条 2019-02-01 起持续滚动基准**；模型预测日之外为零预测"
        "（跟踪基准 KRD）。模型/split/alpha/预测已冻结；探索执行带 requested=1 lot，"
        "effective=1 lot（生产主配置 2 lot 不变），仅用于成本后诊断，不是 alpha 结论。"
        "缺失预测只在公共网格日置零，非网格日 hold（protocol_v1_continuous_eval）。"
    )
    log.info("产物: %s, %s, %s", detail_path, summary_path, manifest_path)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
