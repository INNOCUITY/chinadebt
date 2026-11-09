"""跨入口控制实验：同一网格/固定目标面值下 ML 评估器、RL rollout、RL step 一致。"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from ficc.config import get_config
from ficc.curve.curve import Curve
from ficc.data.loader import load_curve_long, load_funding_series
from ficc.data.schema import CANONICAL_TENORS
from ficc.portfolio.benchmark import build_benchmark, build_universe_bonds
from ficc.research.protocol import (
    ExperimentProtocol,
    evaluate_target_faces,
    protocol_config,
)
from ficc.rl import ActionSpec, RebalanceEnv
from ficc.utils.calendar import TradingCalendar

START = date(2019, 2, 1)
END = date(2019, 2, 15)


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


def _terms_curve(panel, calendar, day):
    terms_day = calendar.prev_trading_day(day, 1)
    rates = panel.loc[pd.Timestamp(terms_day)].reindex(list(CANONICAL_TENORS)).to_numpy(dtype=float)
    return Curve(
        as_of=terms_day,
        tenors=np.asarray(sorted(CANONICAL_TENORS), dtype=float),
        rates=rates,
        method="linear",
    )


def _faces(cfg, panel, calendar, day):
    curve = _terms_curve(panel, calendar, day)
    bonds = build_universe_bonds(curve, curve.as_of, cfg=cfg)
    bench = build_benchmark(curve, curve.as_of, bonds=bonds, calendar=calendar, cfg=cfg)
    return {code: float(face) for code, face in bench.faces.items()}


def test_cross_entry_ml_rollout_step_agree(market) -> None:
    cfg, panel, funding, calendar = market
    protocol = ExperimentProtocol()
    aligned = protocol_config(cfg, protocol)
    grid = protocol.decision_dates(calendar, START, END)
    faces = _faces(aligned, panel, calendar, START)
    targets = {day: faces for day in grid}

    ml = evaluate_target_faces(
        targets,
        panel,
        funding,
        calendar,
        cfg,
        protocol=protocol,
        start=START,
        end=END,
        initial_curve=_terms_curve(panel, calendar, START),
    )

    env = RebalanceEnv(
        panel,
        calendar,
        funding,
        aligned,
        information_lag_days=1,
        decision_step_days=protocol.decision_step_days,
    )
    env.reset(START, END)
    rollout = env.rollout(
        lambda ctx: ActionSpec(action_type="target_faces", target_faces=dict(faces)),
        START,
        END,
    )
    np.testing.assert_allclose(
        rollout.result.nav.to_numpy(), ml.result.nav.to_numpy(), rtol=0.0, atol=1e-9
    )
    assert rollout.terminal.nav_mark_to_market == pytest.approx(
        float(ml.result.nav.iloc[-1]), abs=1e-9
    )

    env_step = RebalanceEnv(
        panel,
        calendar,
        funding,
        aligned,
        information_lag_days=1,
        decision_step_days=protocol.decision_step_days,
    )
    env_step.reset(START, END)
    grid_set = set(grid)
    days = list(calendar.trading_days_between(START, END))
    for day in days[:-1]:
        action = (
            ActionSpec(action_type="target_faces", target_faces=dict(faces))
            if day in grid_set
            else ActionSpec()
        )
        env_step.step(action)
    assert env_step.result is not None
    np.testing.assert_allclose(
        env_step.result.nav.to_numpy(), rollout.result.nav.to_numpy(), rtol=0.0, atol=1e-9
    )
    # 订单字段一致（trade_id 前缀允许不同）
    a = ml.result.trades.reset_index(drop=True)
    b = rollout.result.trades.reset_index(drop=True)
    assert len(a) == len(b)
    for col in ("side", "face_value", "clean_price", "trade_date", "settlement_date"):
        assert a[col].tolist() == b[col].tolist(), col


def test_cross_entry_script_runs(tmp_path) -> None:
    import subprocess
    import sys

    proc = subprocess.run(
        [
            sys.executable,
            "scripts/run_cross_entry_check.py",
            "--start",
            "2019-02-01",
            "--end",
            "2019-02-15",
            "--out-dir",
            str(tmp_path / "cross"),
        ],
        cwd=str(Path(__file__).resolve().parents[1]),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 0, proc.stderr[-1200:]
    out = pd.read_csv(tmp_path / "cross" / "cross_entry_check.csv")
    assert (out["nav_max_abs_diff"] <= 1e-6).all()
    assert (out["order_diff"] <= 1e-6).all()


def test_cross_entry_script_detects_terminal_fee_tamper(tmp_path) -> None:
    """R63：人为把 rollout 终止费用 +1 元，跨入口脚本必须 exit 1（独立终止对照）。"""
    import subprocess
    import sys

    proc = subprocess.run(
        [
            sys.executable,
            "scripts/run_cross_entry_check.py",
            "--start",
            "2019-02-01",
            "--end",
            "2019-02-15",
            "--out-dir",
            str(tmp_path / "tamper"),
            "--tamper-fee",
            "1.0",
        ],
        cwd=str(Path(__file__).resolve().parents[1]),
        capture_output=True,
        text=True,
        check=False,
    )
    assert proc.returncode == 1
    assert "未通过" in proc.stderr or "未通过" in proc.stdout
