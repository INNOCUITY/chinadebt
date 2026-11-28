"""Delivery must keep cohorts separate, fail on missing evidence and show all UI pages."""

import importlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from ficc.reporting.delivery import ablations, baselines, collect

ROOT = Path(__file__).resolve().parents[1]


def test_matched_summary_preserves_seed_results_and_missing_metrics():
    matrix = json.loads((ROOT / "reports/ppo_matrix_review.json").read_text())
    frame = baselines(matrix)
    assert len(frame) == 18 and frame.cohort.nunique() == 1
    assert set(frame.start) == {"2020-05-29"}
    for band in (2, 1):
        group = frame.loc[frame.band_lot.eq(band)]
        ppo = group.loc[group.model.eq("ppo")]
        assert ppo.seed.nunique() == 5
        np.testing.assert_allclose(
            ppo.excess_bp, [r["excess_bp"] for r in matrix["bands"][str(band)]["seeds"]]
        )
        assert group.loc[group.model.ne("ppo"), "te_bp"].isna().all()
        assert group.loc[group.model.eq("zero"), "excess_bp"].eq(0).all()


def test_missing_original_plan_experiments_are_not_passes():
    facts, plan, _, _ = collect(ROOT)
    assert facts["full_original_plan_accepted"] is False
    statuses = {r["id"]: r["status"] for r in plan["items"]}
    assert statuses["sac"] == statuses["risk_penalty"] == statuses["rl_no_cost"] == "deferred"
    frame = ablations(ROOT)
    for name in ("SAC", "risk_penalty_removal", "RL_cost_removal", "five_way_matched"):
        (row,) = frame.loc[frame.experiment.eq(name)].to_dict("records")
        assert row["status"] == "not_completed" and pd.isna(row["median_gain_bp"])
    completed = frame.loc[frame.status.eq("completed")]
    assert ((completed.wins + completed.ties + completed.losses) == completed.pairs).all()


def test_report_build_is_deterministic_and_missing_evidence_fails(tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(ROOT / "scripts"))
    builder = importlib.import_module("build_report")
    builder.check(ROOT)
    builder.build(ROOT, tmp_path, make_plots=False)
    for name in builder.TEXT_OUTPUTS:
        assert (tmp_path / name).read_bytes() == (ROOT / name).read_bytes()
    original = builder.collect

    def missing(root):
        facts, plan, baseline, ablation = original(root)
        plan["items"][0]["evidence"].append("reports/missing-acceptance-proof.csv")
        return facts, plan, baseline, ablation

    monkeypatch.setattr(builder, "collect", missing)
    with pytest.raises(FileNotFoundError, match="missing acceptance evidence"):
        builder.build(ROOT, tmp_path, make_plots=False)


def test_futures_gate_and_report_dates_from_archived_predictions():
    report = json.loads((ROOT / "reports/futures_signal_probe.json").read_text())
    for key in ("primary", "delay_sensitivity"):
        data = report[key]
        wins = sum(
            r["rmse_bp"] < min(r["original_rmse_bp"], r["mean_rmse_bp"])
            for r in data["rows"]
            if r["band"] == 2
        )
        assert wins == data["prediction_wins_at_2_lot"]
        for row in data["rows"]:
            values = []
            for decision in row["selections"]:
                assert decision["futures_report_date"] <= decision["cutoff"] < decision["signal"]
                assert int(np.argmax(decision["predicted_bp"])) == decision["selected_index"]
                values.append(
                    decision["actual_bp"][decision["selected_index"]]
                    - decision["actual_bp"][decision["original_index"]]
                )
            assert float(np.mean(values)) == pytest.approx(row["gain_vs_original_bp"], abs=1e-12)
    assert report["consider_continuous_replay"] is False


def test_dashboard_all_pages_are_read_only(monkeypatch):
    from streamlit.testing.v1 import AppTest

    # Replaying or loading private market snapshots from UI code is forbidden.
    import ficc.data.loader

    def forbidden(*args, **kwargs):
        raise AssertionError("dashboard must not access raw market snapshots")

    monkeypatch.setattr(ficc.data.loader, "load_curve_long", forbidden)
    app = AppTest.from_file(str(ROOT / "apps/dashboard/app.py"), default_timeout=20).run()
    assert not app.exception
    for page in app.sidebar.radio[0].options:
        app.sidebar.radio[0].set_value(page).run()
        assert not app.exception
        assert len(app.dataframe) >= 1
        if page.startswith("基线"):
            app.selectbox[0].set_value(1).run()
            assert not app.exception
        if page.startswith("RL"):
            for figure in app.selectbox[0].options:
                app.selectbox[0].set_value(figure).run()
                assert not app.exception
