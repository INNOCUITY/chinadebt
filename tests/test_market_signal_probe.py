"""Causality, training-only imputation, and label purge for the bounded signal probe."""

import importlib
from pathlib import Path

import numpy as np
import pandas as pd
import pytest


@pytest.fixture
def probe(monkeypatch):
    pytest.importorskip("torch")
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    return importlib.import_module("probe_market_signal")


def test_features_do_not_change_with_future_prices(probe):
    dates = pd.bdate_range("2020-01-01", periods=50)
    panel = pd.DataFrame(
        {2.0: np.arange(50.0) * 0.01, 5.0: np.arange(50.0) * 0.02, 10.0: np.arange(50.0) * 0.03},
        index=dates,
    )
    original = probe.extra_features(panel)
    changed = panel.copy()
    changed.loc[dates[31] :] += 1000
    pd.testing.assert_frame_equal(
        original.loc[: dates[30]], probe.extra_features(changed).loc[: dates[30]]
    )
    np.testing.assert_allclose(original.loc[dates[30]], [0.05, 0.10, 0.15, 0.4])


def test_imputation_and_purge_do_not_use_holdout(probe):
    fit = np.array([[1.0, np.nan], [3.0, 4.0]])
    mean = np.nanmean(fit, axis=0)
    held = np.array([[np.nan, 9000.0]])
    result = probe.augment(np.zeros((1, 2)), held, mean)
    np.testing.assert_array_equal(result, [[0.0, 0.0, 2.0, 9000.0, 1.0, 0.0]])
    valid = [dict(cutoff="2020-01-01", signal="2020-01-02", horizon="2020-01-08")]
    probe.require_purged(valid, [dict(signal="2020-01-09")])
    with pytest.raises(ValueError, match="boundary"):
        probe.require_purged(valid, [dict(signal="2020-01-08")])
    with pytest.raises(ValueError, match="clock"):
        probe.require_purged([{**valid[0], "cutoff": "2020-01-02"}], [dict(signal="2020-01-09")])
    with pytest.raises(ValueError, match="calibration"):
        probe.augment(np.zeros((1, 2)), held, np.array([np.nan, 0.0]))
