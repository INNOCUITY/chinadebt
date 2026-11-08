"""共同实验协议测试：映射边界、零预测回基准、成本后评估与时钟对齐。"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from ficc.config import get_config
from ficc.data.loader import load_curve_long, load_funding_series
from ficc.data.schema import CANONICAL_TENORS
from ficc.research.protocol import (
    ExperimentProtocol,
    PredictionStrategy,
    evaluate_predictions,
    prediction_to_target,
    protocol_config,
)
from ficc.utils.calendar import TradingCalendar

START = date(2019, 1, 28)
END = date(2019, 3, 29)


@pytest.fixture(scope="module")
def market():
    cfg = get_config()
    long = load_curve_long(cfg=cfg)
    panel = (
        long.pivot(index="date", columns="tenor", values="ytm_pct")
        .sort_index()
        .reindex(columns=list(CANONICAL_TENORS))
    )
    funding = load_funding_series(cfg=cfg).set_index("date")["FDR007"].dropna().sort_index()
    calendar = TradingCalendar.from_dates(panel.index)
    return cfg, panel, funding, calendar


def test_protocol_defaults_and_clock() -> None:
    p = ExperimentProtocol()
    assert p.version.startswith("protocol_v1")
    assert p.return_window_trading_days == 4  # 决策+5、执行+1 → 4 个交易日
    d = p.describe()
    assert d["clocks"]["ml"]["observation_lag_days"] == 1
    assert d["clocks"]["rl"]["execution"] == "next_close"
    with pytest.raises(ValueError):
        ExperimentProtocol(execution="next_open")
    assert protocol_config(get_config(), p).backtest.benchmark_version == "rolling"


def test_prediction_mapping_bounds_and_zero(market) -> None:
    cfg, panel, _funding, calendar = market
    from ficc.curve.curve import Curve
    from ficc.portfolio.benchmark import build_benchmark, build_universe_bonds

    day = START
    curve = Curve(
        as_of=day,
        tenors=np.asarray(sorted(CANONICAL_TENORS), dtype=float),
        rates=panel.loc[pd.Timestamp(day)].to_numpy(dtype=float),
        method="linear",
    )
    bonds = build_universe_bonds(curve, day, cfg=cfg)
    bench = build_benchmark(curve, day, bonds=bonds, calendar=calendar, cfg=cfg)
    zero = prediction_to_target(0.0, bench, as_of=day)
    np.testing.assert_allclose(zero.total_dv01, bench.total_dv01, rtol=1e-12)
    plus = prediction_to_target(2.0, bench, as_of=day)  # 越界裁剪到 +1
    base = prediction_to_target(1.0, bench, as_of=day)
    np.testing.assert_allclose(plus.total_dv01, base.total_dv01, rtol=1e-12)
    assert plus.total_dv01 > bench.total_dv01


def test_zero_prediction_tracks_benchmark_and_costs(market) -> None:
    cfg, panel, funding, calendar = market
    days = list(calendar.trading_days_between(START, END))
    preds = {d: 0.0 for d in days[::5]}
    ev = evaluate_predictions(preds, panel, funding, calendar, cfg, start=START, end=END)
    assert ev.metrics["n_days"] > 30
    assert abs(ev.metrics["active_excess_bp"]) < 1.0  # 零映射≈基准（建仓成本内）
    assert ev.metrics["total_cost"] >= 0.0
    assert ev.metrics["n_decisions"] == len(preds)
    assert ev.result.benchmark_version == "rolling"
    # 预测序列只作用在决策日；非决策日策略返回 None（hold）
    strategy = PredictionStrategy(preds, protocol=ExperimentProtocol())
    assert strategy._predictions  # 内部决策日集合非空


def test_evaluate_rejects_nonfinite_prediction(market) -> None:
    cfg, panel, _funding, calendar = market
    from ficc.curve.curve import Curve
    from ficc.portfolio.benchmark import build_benchmark, build_universe_bonds

    curve = Curve(
        as_of=START,
        tenors=np.asarray(sorted(CANONICAL_TENORS), dtype=float),
        rates=panel.loc[pd.Timestamp(START)].to_numpy(dtype=float),
        method="linear",
    )
    bench = build_benchmark(
        curve, START, bonds=build_universe_bonds(curve, START, cfg=cfg), calendar=calendar, cfg=cfg
    )
    with pytest.raises(ValueError):
        prediction_to_target(float("nan"), bench, as_of=START)


def test_protocol_smoke_script_quick(tmp_path) -> None:
    """冒烟脚本可跑通并产出 CSV（短窗，验证协议链路而非结果）。"""
    import subprocess
    import sys

    proc = subprocess.run(
        [sys.executable, "scripts/run_protocol_smoke.py", "--quick"],
        cwd=str(Path(__file__).resolve().parents[1]),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr[-1000:]
    csv = Path(__file__).resolve().parents[1] / "reports" / "protocol_smoke.csv"
    assert csv.exists()
    table = pd.read_csv(csv)
    assert {"zero", "const_plus_one"} <= set(table["scenario"])
    zero = table.loc[table["scenario"] == "zero"].iloc[0]
    assert abs(float(zero["active_excess_bp"])) < 1.0
