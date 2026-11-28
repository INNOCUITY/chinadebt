"""A frozen four-feature futures activity/spread block; no PPO training or forward access."""

import argparse
from pathlib import Path

import pandas as pd
from evaluate_rl import sha
from probe_market_signal import run
from run_critic_budget import read
from train_rl import ROOT, dump

from ficc.data.futures import asof_features, daily_features

PLAN = ROOT / "config/experiments/futures_signal_probe.json"


def evaluate(archive, holdout, dataset):
    plan = read(PLAN)
    calendar = ROOT / "config/baselines/futures_sessions.json"
    for file in ["contracts.parquet", "features.parquet"]:
        if sha(dataset / file) != plan["data_sha256"][file]:
            raise ValueError("frozen futures snapshot differs")
    if sha(calendar) != plan["calendar_sha256"]:
        raise ValueError("frozen futures calendar differs")
    contracts = pd.read_parquet(dataset / "contracts.parquet")
    frame = daily_features(contracts, read(calendar)["sessions"])
    pd.testing.assert_frame_equal(
        frame, pd.read_parquet(dataset / "features.parquet"), check_freq=False
    )
    results = {}
    for lag in plan["publication_lag_sensitivity_sessions"]:
        dates = {}

        def reader(cutoffs, lag=lag, dates=dates):
            values, used = asof_features(frame, cutoffs, lag_sessions=lag)
            dates.update(
                {str(pd.Timestamp(c).date()): u for c, u in zip(cutoffs, used, strict=True)}
            )
            return values

        result = run(archive, holdout, plan_path=PLAN, feature_reader=reader)
        for row in result["rows"]:
            for decision in row["selections"]:
                decision["futures_report_date"] = dates[decision["cutoff"]]
                if decision["futures_report_date"] > decision["cutoff"]:
                    raise ValueError("future daily report")
        result["extra_session_lag"] = lag
        results[str(lag)] = result
    sensitivity = results["1"]["median_bp"]
    passes = results["0"]["consider_continuous_replay"] and all(
        sensitivity[b]["gain_vs_original_bp"] >= 0 for b in (2, 1)
    )
    return dict(
        design_sha256=sha(PLAN),
        primary=results["0"],
        delay_sensitivity=results["1"],
        consider_continuous_replay=passes,
        scope="Historical development label diagnostic only. EOD publication time is modeled conservatively; no exact historical release or revision-vintage archive. No futures execution or bid-ask spread claims.",
    )


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--archive", type=Path, required=True)
    p.add_argument("--holdout", type=Path, default=Path("reports/action_value_holdout.json"))
    p.add_argument("--dataset", type=Path, default=Path("datasets/cffex-eod-v1"))
    p.add_argument("--output", type=Path, default=Path("reports/futures_signal_probe.json"))
    a = p.parse_args()
    result = evaluate(a.archive, a.holdout, a.dataset)
    dump(a.output, result)
    print(
        {
            "eligible": result["consider_continuous_replay"],
            "primary_medians": result["primary"]["median_bp"],
            "prediction_wins": result["primary"]["prediction_wins_at_2_lot"],
        }
    )
