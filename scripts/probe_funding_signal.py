"""冻结资金公告增量：跨年、单目标、单期限的线性预测筛选，不训练 PPO。"""

import argparse
import hashlib
import json
from pathlib import Path

import numpy as np
import pandas as pd
from audit_funding_announcements import audit
from fetch_funding_announcements import dump
from run_ml_baseline import fit_ridge
from run_strategies import load_market

from ficc.config import get_config
from ficc.data.funding_announcements import calendar_sessions, funding_features
from ficc.ml.market_features import feature_specs

PLAN = Path("config/experiments/funding_signal_probe.json")


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_market_inputs(panel, funding, calendar, plan):
    """固定独立日历；资金利率只向前取已知值，限制最长陈旧期。"""
    start, end = plan["history"]
    market_start = plan.get("market_history_start", start)
    days = pd.to_datetime([d for d in calendar_sessions(calendar) if market_start <= d <= end])
    if (
        not days.isin(panel.index).all()
        or panel.index.has_duplicates
        or funding.index.has_duplicates
    ):
        raise ValueError("market does not cover independent audited sessions")
    frame = panel.loc[days].copy()
    frame.columns = frame.columns.astype(str)
    if not np.isfinite(frame[["2.0", "5.0", "10.0"]]).all().all():
        raise ValueError("missing required curve tenor")
    known = funding.loc[funding.index <= days[-1]].dropna()
    positions = known.index.get_indexer(days, method="ffill")
    if np.any(positions < 0):
        raise ValueError("funding warmup missing")
    ages = (days - known.index[positions]).days
    if np.any(ages > plan["funding_ffill_max_calendar_days"]):
        raise ValueError("funding observations too stale")
    frame["FDR007"] = known.iloc[positions].to_numpy()
    features = pd.DataFrame({s.name: s.transform(frame) for s in feature_specs()}, index=days)
    positions = np.flatnonzero(days >= pd.Timestamp(plan["decisions_start"]))[
        :: plan["decision_step_sessions"]
    ]
    rows = []
    for i in positions:
        h = i + plan["horizon_sessions"]
        if i < 1 or h >= len(days):
            continue
        # 一个留出年只包含完整标签，不能把下一年收益归入上一年。
        if days[i].year != days[h].year:
            continue
        rows.append(
            dict(
                decision=str(days[i].date()),
                cutoff=str(days[i - 1].date()),
                horizon=str(days[h].date()),
                target_bp=float((frame["10.0"].iloc[h] - frame["10.0"].iloc[i]) * 100),
                features=features.iloc[i - 1].to_numpy().tolist(),
            )
        )
    if not rows or not np.isfinite([r["features"] for r in rows]).all():
        raise ValueError("incomplete feature warmup")
    h = hashlib.sha256()
    h.update(pd.util.hash_pandas_object(frame, index=True).to_numpy().tobytes())
    return rows, dict(
        market_sha256=h.hexdigest(),
        sessions=len(days),
        funding_carried_sessions=int(sum(ages > 0)),
        funding_max_age_calendar_days=int(max(ages)),
    )


def annual_compare(rows, added, plan):
    """每年仅拟合一次；禁止跨边界标签和留出期标准化。"""
    if added.shape != (len(rows), len(plan["added_features"])) or not np.isfinite(added).all():
        raise ValueError("incomplete added features")
    bx = np.asarray([r["features"] for r in rows])
    ex = np.c_[bx, added]
    y = np.asarray([r["target_bp"] for r in rows])
    summaries = []
    predictions = []
    for year in plan["evaluation_years"]:
        held = np.array([i for i, r in enumerate(rows) if int(r["decision"][:4]) == year])
        if len(held) < 5:
            raise ValueError("insufficient heldout year")
        first = rows[held[0]]["cutoff"]
        fit = np.array([i for i, r in enumerate(rows) if r["horizon"] < first])
        if len(fit) < 60 or np.intersect1d(fit, held).size:
            raise ValueError("insufficient or overlapping fit")
        forecasts = {
            "original_ridge": fit_ridge(bx[fit], y[fit], plan["ridge_alpha"]).predict(bx[held]),
            "funding_ridge": fit_ridge(ex[fit], y[fit], plan["ridge_alpha"]).predict(ex[held]),
            "fit_mean": np.full(len(held), y[fit].mean()),
            "zero_change": np.zeros(len(held)),
        }
        metrics = {
            k: dict(
                rmse_bp=float(np.sqrt(np.mean((v - y[held]) ** 2))),
                mae_bp=float(np.mean(abs(v - y[held]))),
            )
            for k, v in forecasts.items()
        }
        gain = metrics["original_ridge"]["rmse_bp"] - metrics["funding_ridge"]["rmse_bp"]
        wins = all(
            metrics["funding_ridge"]["rmse_bp"] < metrics[k]["rmse_bp"]
            for k in plan["gate"]["comparators"]
        )
        summaries.append(
            dict(
                year=year,
                fit_rows=len(fit),
                heldout_rows=len(held),
                fit_last_horizon=max(rows[i]["horizon"] for i in fit),
                heldout_first_cutoff=first,
                metrics=metrics,
                rmse_gain_vs_original_bp=gain,
                beats_all_controls=wins,
            )
        )
        for j, i in enumerate(held):
            predictions.append(
                dict(
                    year=year,
                    decision=rows[i]["decision"],
                    horizon=rows[i]["horizon"],
                    target_bp=float(y[i]),
                    **{k: float(v[j]) for k, v in forecasts.items()},
                )
            )
    mse = {
        k: float(np.mean([(r[k] - r["target_bp"]) ** 2 for r in predictions])) for k in forecasts
    }
    return dict(
        years=summaries,
        pooled_mse_bp2=mse,
        median_rmse_gain_bp=float(np.median([r["rmse_gain_vs_original_bp"] for r in summaries])),
        wins=sum(r["beats_all_controls"] for r in summaries),
    ), predictions


def assess_gate(primary, delay, plan):
    """按事前门槛输出下一步，不将预测准确率称为成本后收益。"""
    rules = plan["gate"]
    checks = dict(
        primary_wins=primary["wins"] >= rules["primary_wins_required"],
        primary_median=primary["median_rmse_gain_bp"] > 0,
        pooled_improvement=primary["pooled_mse_bp2"]["funding_ridge"]
        < primary["pooled_mse_bp2"]["original_ridge"],
        delay_median=delay["median_rmse_gain_bp"] >= 0,
    )
    passed = all(checks.values())
    return dict(
        checks=checks,
        passed=passed,
        next_step="freeze_cost_assessment"
        if passed
        else "stop_this_funding_feature_target_specification",
        economic_value_tested=False,
        ppo_authorized=False,
    )


def run(root, plan_path=PLAN):
    plan = json.loads(plan_path.read_text())
    cal_path = Path("config/baselines/funding_sessions.json")
    audited = audit(root, cal_path)
    if (
        not audited["passed_gross_announcements"]
        or digest(root / "audit.json") != plan["audit_report_sha256"]
        or digest(cal_path) != plan["calendar_sha256"]
        or audited["raw_manifest_sha256"] != plan["raw_manifest_sha256"]
    ):
        raise ValueError("audited data identity differs from frozen predictive design")
    calendar = json.loads(cal_path.read_text())
    records = json.loads((root / "audited_records.json").read_text())
    panel, funding, _ = load_market(get_config())
    rows, market_meta = build_market_inputs(panel, funding, calendar, plan)
    if market_meta["market_sha256"] != plan["market_sha256"]:
        raise ValueError("historical market inputs differ from the frozen prediction design")
    reports = []
    details = []
    for lag in plan["publication_extra_lags"]:
        added = funding_features(
            records, [r["cutoff"] for r in rows], calendar, *plan["history"], extra_lag=lag
        )
        if list(added.columns) != plan["added_features"]:
            raise ValueError("registered feature names differ")
        report, prediction = annual_compare(rows, added.to_numpy(), plan)
        reports.append(dict(extra_lag=lag, **report))
        details.append(prediction)
    result = dict(
        version=plan["version"],
        plan_sha256=digest(plan_path),
        audit_sha256=plan["audit_report_sha256"],
        calendar_sha256=plan["calendar_sha256"],
        market=market_meta,
        target=plan["target"],
        scope=plan["scope"],
        primary=reports[0],
        delay_sensitivity=reports[1],
        gate=assess_gate(reports[0], reports[1], plan),
        economic_limit="Yield forecast RMSE only; no cost or cumulative portfolio excess claimed. Forward period untouched.",
    )
    dump(root / "prediction_detail.json", dict(rows=rows, predictions=details))
    return result


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, default=Path("datasets/pbc-omo-v1"))
    p.add_argument("--report", type=Path, default=Path("reports/funding_signal_probe.json"))
    args = p.parse_args()
    result = run(args.root)
    dump(args.report, result)
    print(
        json.dumps(
            dict(
                gate=result["gate"],
                primary_wins=result["primary"]["wins"],
                primary_median_gain=result["primary"]["median_rmse_gain_bp"],
                delayed_median_gain=result["delay_sensitivity"]["median_rmse_gain_bp"],
            ),
            ensure_ascii=False,
        )
    )
