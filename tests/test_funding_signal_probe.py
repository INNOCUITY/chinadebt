"""冻结资金预测：跨年 purge、留出处理与数据版本边界。"""

import copy
import json
from datetime import timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from probe_funding_signal import annual_compare, assess_gate, build_market_inputs


def toy_rows():
    rng = np.random.default_rng(7)
    days = list(pd.bdate_range("2019-01-01", periods=90)) + list(
        pd.bdate_range("2021-01-04", periods=10)
    )
    return [
        dict(
            decision=str(d.date()),
            cutoff=str((d.to_pydatetime() - timedelta(days=1)).date()),
            horizon=str((d.to_pydatetime() + timedelta(days=5)).date()),
            features=rng.normal(size=3).tolist(),
            target_bp=float(rng.normal()),
        )
        for d in days
    ]


def test_heldout_values_never_fit_standardization_or_coefficients():
    rows = toy_rows()
    added = np.arange(len(rows) * 3, dtype=float).reshape(-1, 3)
    plan = dict(
        evaluation_years=[2021],
        added_features=["a", "b", "c"],
        ridge_alpha=10,
        gate=dict(comparators=["original_ridge", "fit_mean", "zero_change"]),
    )
    report, pred = annual_compare(rows, added, plan)
    altered = copy.deepcopy(rows)
    for r in altered[90:]:
        r["target_bp"] += 10000
    _, after = annual_compare(altered, added, plan)
    for a, b in zip(pred, after, strict=True):
        for k in ["original_ridge", "funding_ridge", "fit_mean", "zero_change"]:
            assert a[k] == b[k]
    assert report["years"][0]["fit_last_horizon"] < report["years"][0]["heldout_first_cutoff"]
    with pytest.raises(ValueError, match="incomplete added"):
        annual_compare(rows, added * np.nan, plan)


def test_future_curves_do_not_change_prior_features_and_missing_session_fails():
    days = pd.bdate_range("2018-12-03", "2019-06-28")
    calendar = dict(
        start=str(days[0].date()),
        end=str(days[-1].date()),
        sessions=days.strftime("%Y-%m-%d").tolist(),
        annual_notices=[],
        amendments=[],
    )
    plan = dict(
        history=[calendar["start"], calendar["end"]],
        decisions_start="2019-04-01",
        decision_step_sessions=5,
        horizon_sessions=5,
        funding_ffill_max_calendar_days=7,
    )
    p = pd.DataFrame(
        {
            2.0: np.arange(len(days)) * 0.001 + 2,
            5.0: np.arange(len(days)) * 0.002 + 3,
            10.0: np.arange(len(days)) * 0.003 + 4,
        },
        index=days,
    )
    funding = pd.Series(2.0, index=days)
    rows, _ = build_market_inputs(p, funding, calendar, plan)
    changed = p.copy()
    changed.loc["2019-05-01":] += 100
    after, _ = build_market_inputs(changed, funding, calendar, plan)
    for a, b in zip(rows, after, strict=True):
        if a["cutoff"] < "2019-05-01":
            assert a["features"] == b["features"]
    with pytest.raises(ValueError, match="independent"):
        build_market_inputs(p.drop(days[90]), funding, calendar, plan)
    with pytest.raises(ValueError, match="stale"):
        build_market_inputs(p, funding.iloc[:20], calendar, plan)


def test_gate_requires_both_absolute_prediction_and_incremental_improvement():
    plan = json.loads(Path("config/experiments/funding_signal_probe.json").read_text())
    good = dict(
        wins=3, median_rmse_gain_bp=0.1, pooled_mse_bp2=dict(funding_ridge=2.0, original_ridge=3.0)
    )
    assert assess_gate(good, good, plan)["passed"]
    assert not assess_gate(dict(good, wins=2), good, plan)["passed"]
    assert not assess_gate(good, dict(good, median_rmse_gain_bp=-0.1), plan)["passed"]
    assert not assess_gate(good, good, plan)["economic_value_tested"]
