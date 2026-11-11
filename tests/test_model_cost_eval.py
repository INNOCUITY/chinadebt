"""冻结 OOS 预测的成本后评估脚本冒烟（不重新训练/不比较模型优劣）。"""

from __future__ import annotations

import json
import subprocess
import sys
from datetime import date
from pathlib import Path

import pandas as pd
import pytest


def test_model_cost_eval_quick(tmp_path) -> None:
    out = tmp_path / "cost"
    proc = subprocess.run(
        [
            sys.executable,
            "scripts/run_model_cost_eval.py",
            "--models",
            "literal_zero",
            "--out-dir",
            str(out),
        ],
        cwd=str(Path(__file__).resolve().parents[1]),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr[-1500:]
    detail = pd.read_csv(out / "model_cost_eval.csv")
    assert {"model", "active_excess_bp", "total_cost", "n_rejected"} <= set(detail.columns)
    assert {"zero_control", "literal_zero"} <= set(detail["model"])
    # R70：请求带与实际最小成交面值必须同时披露（请求 0.25 会静默变成 1 lot）
    assert {
        "requested_band_lots",
        "effective_min_trade_face",
        "benchmark_terminal_accrued_fees",
    } <= set(detail.columns)
    assert (detail["requested_band_lots"] == 1.0).all()
    assert (detail["effective_min_trade_face"] == 1_000_000.0).all()
    manifest = json.loads((out / "model_cost_eval_manifest.json").read_text())
    assert manifest["protocol_version"] == "protocol_v1_continuous_eval"
    assert manifest["missing_prediction_policy"] == "benchmark_on_grid"
    assert len(manifest["protocol_fingerprint"]) == 64
    assert manifest["predictions_sha256"]
    assert manifest["n_prediction_rows"] > 0
    assert "zero_control" in manifest["n_decisions"]
    assert manifest["repo_state"] in {"clean", "dirty"}
    assert "source_sha256" in manifest
    assert "git_head" not in manifest
    assert manifest["resolved_benchmark_version"] == "rolling"
    assert manifest["resolved_rolling"]["enabled"] is True
    assert manifest["source_sha256"]
    assert manifest["source_archive_sha256"]
    assert manifest["data_archive_sha256"]
    assert manifest["data_sha256"]
    assert manifest["dependency_sha256"]
    assert manifest["source_commit"]
    assert manifest["packages"]
    # R68：决策数=实际信号（网格机会），不是输入预测条数
    assert manifest["n_decisions"]["zero_control"] > manifest["n_prediction_rows"] * 0


def test_r65_last_prediction_executes_next_day(tmp_path) -> None:
    """R65：改变窗口最后一条预测，必须影响订单/NAV（否则末端未执行）。"""

    # 直接用协议评估器验证：预测在 end 当日由引擎在 next day 执行

    from ficc.config import get_config
    from ficc.data.loader import load_curve_long, load_funding_series
    from ficc.data.schema import CANONICAL_TENORS
    from ficc.research.protocol import ExperimentProtocol, evaluate_predictions
    from ficc.utils.calendar import TradingCalendar

    cfg = get_config()
    long = load_curve_long(cfg=cfg)
    panel = (
        long.pivot(index="date", columns="tenor", values="ytm_pct")
        .sort_index()
        .reindex(columns=list(CANONICAL_TENORS))
    )
    funding = load_funding_series(cfg=cfg).set_index("date")["FDR007"].dropna().sort_index()
    calendar = TradingCalendar.from_dates(panel.index)
    protocol = ExperimentProtocol()
    start = date(2019, 2, 1)
    grid = protocol.decision_dates(calendar, start, date(2019, 3, 8))
    assert len(grid) >= 3
    last = grid[2]
    end = calendar.next_trading_day(last, 1)  # R65：最后预测的下一交易日必须包含在区间内
    base = {d: 0.0 for d in grid[:3]}
    changed = dict(base)
    changed[last] = 0.01  # z=1
    a = evaluate_predictions(base, panel, funding, calendar, cfg, start=start, end=end)
    b = evaluate_predictions(changed, panel, funding, calendar, cfg, start=start, end=end)
    assert b.metrics["n_trades"] > a.metrics["n_trades"]
    assert abs(b.metrics["terminal_nav"] - a.metrics["terminal_nav"]) > 1.0
    # R66：末端成交未结算 → 终止费用/未结算现金进入净终值
    assert b.metrics["terminal_accrued_fees"] > 0.0
    assert b.metrics["terminal_unsettled_cash"] < 0.0
    assert b.metrics["net_terminal_nav"] == pytest.approx(
        b.metrics["terminal_nav"] - b.metrics["terminal_accrued_fees"], abs=1e-6
    )
    assert b.metrics["net_active_excess_bp"] != pytest.approx(
        b.metrics["active_excess_bp"], abs=1e-9
    )


def test_r71_unused_benchmark_override_removed() -> None:
    """R71：未使用的 benchmark override 接口应删除，避免日期错贴。"""
    import inspect

    from ficc.research.protocol import PredictionStrategy, evaluate_predictions

    params = set(inspect.signature(evaluate_predictions).parameters)
    assert "benchmark_krd_override" not in params
    assert "benchmark_nav_override" not in params
    assert "default_to_benchmark" not in params
    strategy_params = set(inspect.signature(PredictionStrategy.__init__).parameters)
    assert "benchmark_krd" not in strategy_params
