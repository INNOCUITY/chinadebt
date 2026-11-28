"""Cost accrual, predictor/label separation and purged diagnostic boundaries."""

import importlib
from copy import deepcopy
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest


@pytest.fixture
def diagnostic(monkeypatch):
    pytest.importorskip("torch")
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    return importlib.import_module("run_cost_signal_diagnostic")


def test_terminal_fee_accrual_and_single_spread_count(diagnostic):
    dates = [date(2020, 1, d) for d in (2, 3, 6)]
    # Last-day fee 7 is still unpaid in raw cash accounting, but accrued in net NAV.
    raw = pd.DataFrame(
        dict(date=dates, spread=[0.0, 5.0, 0.0], funding=[0.0, 2.0, 1.0], fee=[0.0, 0.0, 0.0])
    )
    book = pd.DataFrame(dict(cost=[0.0, 7.0, 8.0]), index=dates)
    c = diagnostic.costs(book, raw, dates[0], dates[-1])
    assert c == dict(spread=5.0, funding=3.0, fee=7.0, total=15.0)
    y = diagnostic.restore(-2.0, c, 100_000.0)
    assert y["transaction_cost_restored"] == pytest.approx(-0.8)
    assert y["all_cost_restored"] == pytest.approx(-0.5)
    with pytest.raises(ValueError, match="close"):
        diagnostic.restore(-2.0, dict(c, total=16.0), 100_000.0)
    with pytest.raises(ValueError, match="invalid"):
        diagnostic.restore(-2.0, c, 0.0)


def rows():
    dates = pd.bdate_range("2020-01-01", periods=25)
    result = []
    for i in range(12):
        x = float(i)
        result.append(
            dict(
                signal=str(dates[2 * i].date()),
                horizon=str(dates[2 * i + 1].date()),
                features=[x, x / 2],
                labels=[
                    dict(net=0.0, all_cost_restored=0.0, different_faces_days=0),
                    dict(net=0.0, all_cost_restored=0.0, different_faces_days=0),
                    dict(net=2 - x / 5, all_cost_restored=3 - x / 5, different_faces_days=3),
                    dict(net=x / 5 - 2, all_cost_restored=x / 5 - 1, different_faces_days=3),
                ],
            )
        )
    return result[:8], result[8:]


def test_future_cost_labels_never_enter_predictions(diagnostic):
    fit, held = rows()
    original = diagnostic.compare(fit, held, 10.0)
    changed = deepcopy(held)
    for row in changed:
        for label in row["labels"][1:]:
            label["all_cost_restored"] += 10_000
            label["net"] -= 20_000
    perturbed = diagnostic.compare(fit, changed, 10.0)
    for a, b in zip(original["decisions"], perturbed["decisions"], strict=True):
        assert a["net_predictions"] == b["net_predictions"]
        assert a["restored_predictions"] == b["restored_predictions"]
        assert a["net_pick"] == b["net_pick"] and a["restored_pick"] == b["restored_pick"]
    fit[-1]["horizon"] = held[0]["signal"]
    with pytest.raises(ValueError, match="boundary"):
        diagnostic.compare(fit, held, 10.0)


def test_accounting_restoration_is_paired_not_median_addition(monkeypatch):
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    module = importlib.import_module("review_research_pivot")
    samples = [
        dict(seed=i, excess_bp=n, cost_delta_bp=c, turnover=1.0)
        for i, (n, c) in enumerate([(0, 100), (10, 0), (20, 1)])
    ]
    report = module.review(dict(test=["a", "b"], bands={"2": dict(seeds=samples)}))
    assert report["bands"]["2"]["restored_median_bp"] == 21
    assert np.median([0, 10, 20]) + np.median([100, 0, 1]) == 11
