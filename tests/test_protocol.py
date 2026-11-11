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
    ProtocolError,
    evaluate_predictions,
    prediction_to_target,
    prediction_to_z,
    protocol_config,
    protocol_fingerprint,
    training_objective,
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
    # R68/R70 报告契约：决策数=实际产生的信号数，输入预测数单独报告
    assert ev.metrics["n_input_predictions"] == len(preds)
    assert ev.metrics["n_decisions"] == len(ev.result.signals)
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


def test_protocol_decision_grid_enforced(market) -> None:
    cfg, panel, funding, calendar = market
    proto = ExperimentProtocol()
    days = list(calendar.trading_days_between(START, END))
    grid = proto.decision_dates(calendar, START, END)
    assert len(grid) == len(days[::5])
    # 连续四天预测（不在 5 日网格上）必须被公共入口拒绝，而不是各自产生信号
    bad = {d: 0.0 for d in days[:4]}
    with pytest.raises(ProtocolError, match="决策网格"):
        evaluate_predictions(bad, panel, funding, calendar, cfg, start=START, end=END)


def test_protocol_frozen_version_rejects_custom_economics() -> None:
    with pytest.raises(ProtocolError, match="冻结字段"):
        ExperimentProtocol(holding_days_from_decision=20)
    with pytest.raises(ProtocolError, match="冻结字段"):
        ExperimentProtocol(label_target="active_excess_vs_benchmark")
    # 自定义版本可显式声明，但不得再称为 v1
    custom = ExperimentProtocol(version="protocol_custom", holding_days_from_decision=20)
    assert custom.version == "protocol_custom"


def test_protocol_fingerprint_binds_config(market) -> None:
    cfg, _panel, _funding, _calendar = market
    proto = ExperimentProtocol()
    fp1 = protocol_fingerprint(cfg, proto)
    changed = cfg.model_copy(
        update={"constraints": cfg.constraints.model_copy(update={"min_trade_lot": 2e6})}
    )
    assert protocol_fingerprint(changed, proto) != fp1
    trigger_changed = cfg.model_copy(
        update={
            "portfolio": cfg.portfolio.model_copy(
                update={
                    "rolling": cfg.portfolio.rolling.model_copy(update={"roll_trigger_years": 1.99})
                }
            )
        }
    )
    assert protocol_fingerprint(trigger_changed, proto) != fp1


def test_z_scale_and_training_objective() -> None:
    proto = ExperimentProtocol()
    assert prediction_to_z(0.0, proto) == 0.0
    assert prediction_to_z(0.005, proto) == pytest.approx(0.5)
    assert prediction_to_z(0.05, proto) == pytest.approx(1.0)  # 裁剪
    assert prediction_to_z(-0.05, proto) == pytest.approx(-1.0)
    assert training_objective(100.0, 2.5, proto) == pytest.approx(97.5)
    gross = ExperimentProtocol(version="protocol_custom", terminal_fee_treatment="gross_reward")
    assert training_objective(100.0, 2.5, gross) == pytest.approx(100.0)


def test_protocol_smoke_script_isolated(tmp_path) -> None:
    """冒烟脚本写入 --out-dir，不得覆盖仓库 reports（R56）。"""
    import subprocess
    import sys

    out = tmp_path / "protocol_out"
    proc = subprocess.run(
        [
            sys.executable,
            "scripts/run_protocol_smoke.py",
            "--quick",
            "--out-dir",
            str(out),
        ],
        cwd=str(Path(__file__).resolve().parents[1]),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr[-1200:]
    assert (out / "protocol_smoke.csv").exists()
    assert (out / "protocol_manifest.json").exists()
    table = pd.read_csv(out / "protocol_smoke.csv")
    assert {"zero", "const_plus_one"} <= set(table["scenario"])
    zero = table.loc[table["scenario"] == "zero"].iloc[0]
    assert abs(float(zero["active_excess_bp"])) < 1.0


def test_r60_fingerprint_covers_full_economics(market) -> None:
    cfg, _panel, _funding, _calendar = market
    proto = ExperimentProtocol()
    base = protocol_fingerprint(cfg, proto)
    variants = [
        cfg.model_copy(
            update={"costs": cfg.costs.model_copy(update={"settlement_fee_cny_per_trade": 1350.0})}
        ),
        cfg.model_copy(
            update={"costs": cfg.costs.model_copy(update={"spread_bp_ultra_long": 9.0})}
        ),
        cfg.model_copy(
            update={"backtest": cfg.backtest.model_copy(update={"init_cash": 200_000_000.0})}
        ),
        cfg.model_copy(update={"backtest": cfg.backtest.model_copy(update={"max_leverage": 1.5})}),
        cfg.model_copy(
            update={"constraints": cfg.constraints.model_copy(update={"relative_krd_tol": 0.25})}
        ),
    ]
    fingerprints = {protocol_fingerprint(item, proto) for item in variants}
    assert len(fingerprints) == len(variants)
    assert base not in fingerprints


def test_r60_lag_is_frozen_in_v1() -> None:
    with pytest.raises(ProtocolError, match="冻结字段"):
        ExperimentProtocol(information_lag_days=2)
    custom = ExperimentProtocol(version="protocol_custom", information_lag_days=2)
    assert custom.information_lag_days == 2


def test_r60_pricing_in_fingerprint(market) -> None:
    """结算周期 T+1→T+2 必须改变协议指纹（R60 补查）。"""
    cfg, _panel, _funding, _calendar = market
    proto = ExperimentProtocol()
    base = protocol_fingerprint(cfg, proto)
    t2 = cfg.model_copy(
        update={
            "pricing": cfg.pricing.model_copy(update={"settlement_lag_days": 2}),
        }
    )
    assert protocol_fingerprint(t2, proto) != base


def test_r68_missing_predictions_only_on_grid_and_distinct_fingerprint(market) -> None:
    """R68：缺预测只在公共网格日置零；非网格日 hold，不能每日脉冲回基准。"""
    cfg, panel, funding, calendar = market
    proto = ExperimentProtocol(
        version="protocol_v1_continuous_eval",
        missing_prediction_policy="benchmark_on_grid",
    )
    standard = ExperimentProtocol()
    assert protocol_fingerprint(cfg, proto) != protocol_fingerprint(cfg, standard)
    grid = proto.decision_dates(calendar, date(2019, 2, 1), date(2019, 3, 8))
    start, last = date(2019, 2, 1), grid[1]
    end = calendar.next_trading_day(last, 1)
    predictions = {grid[0]: 0.01}  # 只有首日正预测；其余网格日缺预测→零目标
    evaluation = evaluate_predictions(
        predictions, panel, funding, calendar, cfg, protocol=proto, start=start, end=end
    )
    trades = evaluation.result.trades
    grid_set = set(grid)
    assert not trades.empty
    # 成交日必须是信号日的下一交易日；非网格日不允许出现新交易
    for _, trade in trades.iterrows():
        trade_day = pd.Timestamp(trade["trade_date"]).date()
        signal_day = calendar.prev_trading_day(trade_day, 1)
        assert signal_day in grid_set, (trade_day, signal_day)
    # 02-02 建仓后，02-03 等非网格日不得因缺预测而卖出：上面已逐笔验证
    # 每个成交的 signal_day 都属于公共网格，非网格日没有新信号/成交。


def test_r69_net_active_deducts_both_terminal_fees(market) -> None:
    """R69：双方都有正终止费用时净超额仍为 0；基准费用 +1 元必须减少净终值。"""
    from dataclasses import replace

    from ficc.rl import benchmark_terminal_state_from_result

    cfg, panel, funding, calendar = market
    proto = ExperimentProtocol(
        version="protocol_v1_continuous_eval",
        missing_prediction_policy="benchmark_on_grid",
    )
    grid = proto.decision_dates(calendar, date(2019, 2, 1), date(2019, 3, 8))
    start = grid[0]
    end = calendar.next_trading_day(start, 1)  # 首笔建仓当日终止→双方均有未付费用
    evaluation = evaluate_predictions(
        {start: 0.0}, panel, funding, calendar, cfg, protocol=proto, start=start, end=end
    )
    m = evaluation.metrics
    assert m["terminal_accrued_fees"] > 0.0
    assert m["terminal_accrued_fees"] == pytest.approx(
        m["benchmark_terminal_accrued_fees"], abs=1e-9
    )
    assert m["active_excess_bp"] == pytest.approx(0.0, abs=1e-9)
    assert m["net_active_excess_bp"] == pytest.approx(0.0, abs=1e-9)
    # 基准费用 +1 元：基准净终值准确减少 1 元，净收益相应变化
    bench_terminal = benchmark_terminal_state_from_result(
        evaluation.result, calendar=calendar, cfg=cfg
    )
    tampered = replace(bench_terminal, accrued_fees=bench_terminal.accrued_fees + 1.0)
    assert tampered.net_terminal_value == pytest.approx(
        bench_terminal.net_terminal_value - 1.0, abs=1e-12
    )


def test_r72_prediction_mapping_uses_lagged_benchmark(market) -> None:
    """R72：预测→KRD 必须使用 t−1 可得基准；当日基准被扰动不得改变目标。"""
    from types import SimpleNamespace

    from ficc.research.protocol import _anchor_benchmark_krd_history

    cfg, panel, funding, calendar = market
    proto = ExperimentProtocol()
    start = date(2019, 2, 1)
    end = date(2019, 3, 8)
    history = _anchor_benchmark_krd_history(
        panel, funding, calendar, cfg, start=start, end=end, initial_curve=None
    )
    t = proto.decision_dates(calendar, start, end)[1]
    available = calendar.prev_trading_day(t, proto.information_lag_days)
    assert available in history
    base_krd = history[available]
    strategy = PredictionStrategy(
        {t: 0.01},
        protocol=proto,
        decision_grid={t},
        benchmark_krd_history=history,
        calendar=calendar,
    )
    base_ctx = SimpleNamespace(
        as_of=t,
        benchmark=SimpleNamespace(krd=dict(base_krd), as_of=t),
    )
    perturbed_ctx = SimpleNamespace(
        as_of=t,
        benchmark=SimpleNamespace(
            krd={float(k): float(v) + 1000.0 for k, v in base_krd.items()}, as_of=t
        ),
    )
    a = strategy.target_krd(base_ctx)
    b = strategy.target_krd(perturbed_ctx)
    assert a is not None and b is not None
    np.testing.assert_allclose(a.total_dv01, b.total_dv01, rtol=0.0, atol=1e-9)
    # 目标基于 available 日基准，而不是当日 ctx.benchmark
    expected = prediction_to_target(0.01 / 0.01, SimpleNamespace(krd=base_krd, as_of=available))
    np.testing.assert_allclose(a.total_dv01, expected.total_dv01, rtol=1e-12, atol=0.0)
