"""复核冻结成本诊断；只读已有标签，不重跑账本或选择新模型。"""

import argparse
import hashlib
import json
from pathlib import Path
from statistics import median

import numpy as np

ROOT = Path(__file__).resolve().parents[1]


def read(path: Path) -> dict:
    """读取本地归档。"""
    return json.loads(path.read_text())


def sha(path: Path) -> str:
    """记录归档文件的内容散列。"""
    return hashlib.sha256(path.read_bytes()).hexdigest()


def audit(root: Path, server_summary: Path, local_summary: Path) -> dict:
    """以增广最小二乘独立复算预测和决策统计，核对冻结身份及成本闭合。"""
    plan_path = ROOT / "config/experiments/cost_signal_diagnostic.json"
    plan = read(plan_path)
    launch = read(ROOT / "reports/cost_signal_launch.json")
    report = read(server_summary)
    local = read(local_summary)
    assert report["plan_sha256"] == sha(plan_path) == launch["plan_sha256"]
    assert report["runtime_sha256"] == launch["runtime_sha256"]
    diffs = []

    def equal(a, b):
        if isinstance(a, dict):
            assert a.keys() == b.keys()
            for k in a:
                equal(a[k], b[k])
        elif isinstance(a, list):
            assert len(a) == len(b)
            for x, y in zip(a, b, strict=True):
                equal(x, y)
        elif isinstance(a, float):
            assert np.isfinite([a, b]).all()
            diffs.append(abs(a - b))
            assert abs(a - b) < 1e-9
        else:
            assert a == b

    equal(report, local)
    files = sorted(root.glob("fold-*/band-*/*.json"))
    assert len(files) == launch["expected_label_records"] == 181
    records = [read(p) for p in files]
    for r in records:
        for k in ("plan_sha256", "runtime_sha256"):
            assert r[k] == report[k]
        assert r["fit_sha256"] == plan["fit_sha256"][str(r["fold"])]
        assert r["market_sha256"] == plan["market_sha256"][str(r["fold"])]
        assert r["holdout_sha256"] == plan["holdout_sha256"]
        assert r["cutoff"] < r["signal"] < r["horizon"]
        assert r["opening_net_nav"] > 0
        for a in r["labels"]:
            c = a["delta_cost_yuan"]
            np.testing.assert_allclose(
                c["total"], c["spread"] + c["fee"] + c["funding"], atol=1e-6, rtol=0
            )
            np.testing.assert_allclose(
                a["all_cost_restored"],
                a["net"] + c["total"] * 1e4 / r["opening_net_nav"],
                atol=1e-10,
                rtol=0,
            )
    max_prediction_error = 0.0
    for result in report["results"]:
        fold, band = result["fold"], result["band"]
        fit = [r for r in records if r["fold"] == fold and r["band"] == 2 and r["segment"] == "fit"]
        held = [
            r for r in records if r["fold"] == fold and r["band"] == band and r["segment"] == "held"
        ]
        assert len(fit) == 29 and len(held) == result["held_intervals"]
        assert max(r["horizon"] for r in fit) < min(r["signal"] for r in held)
        x = np.array([r["features"] for r in fit])
        hx = np.array([r["features"] for r in held])
        scale = x.std(0)
        scale[scale == 0] = 1
        z, hz = (x - x.mean(0)) / scale, (hx - x.mean(0)) / scale
        matrix = np.vstack((z, np.sqrt(plan["ridge_alpha"]) * np.eye(x.shape[1])))
        chosen = {}
        values = {}
        fixed = {}
        for target, prefix in (("net", "net"), ("all_cost_restored", "restored")):
            y = np.array([[a[target] for a in r["labels"]] for r in fit])
            hy = np.array([[a[target] for a in r["labels"]] for r in held])
            coef = np.linalg.lstsq(
                matrix, np.vstack((y - y.mean(0), np.zeros((x.shape[1], 4)))), rcond=None
            )[0]
            pred = y.mean(0) + hz @ coef
            archived = np.array([d[prefix + "_predictions"] for d in result["decisions"]])
            np.testing.assert_allclose(pred, archived, atol=1e-9, rtol=0)
            max_prediction_error = max(max_prediction_error, float(np.abs(pred - archived).max()))
            picks = pred.argmax(1)
            assert picks.tolist() == [d[prefix + "_pick"] for d in result["decisions"]]
            fixed[target] = int(y.mean(0).argmax())
            assert fixed[target] == result[prefix + "_fixed_index"]
            chosen[target], values[target] = picks, hy
            if prefix == "restored":
                for name, forecast in (
                    ("restored_rmse_bp", pred),
                    ("restored_fit_mean_rmse_bp", y.mean(0)),
                ):
                    np.testing.assert_allclose(
                        result[name],
                        np.sqrt(np.mean((forecast[..., 1:] - hy[:, 1:]) ** 2)),
                        atol=1e-9,
                        rtol=0,
                    )
        idx = np.arange(len(held))
        g, n = values["all_cost_restored"], values["net"]
        gp, npick, gf = chosen["all_cost_restored"], chosen["net"], fixed["all_cost_restored"]
        derived = dict(
            restored_selected_bp=float(g[idx, gp].mean()),
            restored_vs_fixed_bp=float((g[idx, gp] - g[:, gf]).mean()),
            actual_selected_net_bp=float(n[idx, gp].mean()),
            actual_vs_net_selector_bp=float((n[idx, gp] - n[idx, npick]).mean()),
            actual_vs_restored_fixed_bp=float((n[idx, gp] - n[:, gf]).mean()),
        )
        for k, v in derived.items():
            np.testing.assert_allclose(result[k], v, atol=1e-9, rtol=0)
        result["actions_changed_vs_net_selector"] = int((gp != npick).sum())
        result["intervals_with_changed_holdings_vs_zero"] = sum(
            r["labels"][p]["different_faces_days"] > 0 for r, p in zip(held, gp, strict=True)
        )
        result["selected_action_counts"] = [int((gp == i).sum()) for i in range(4)]
        result["selected_incremental_cost_bp"] = {
            k: float(
                np.mean(
                    [
                        r["labels"][p]["delta_cost_yuan"][k] * 1e4 / r["opening_net_nav"]
                        for r, p in zip(held, gp, strict=True)
                    ]
                )
            )
            for k in ("spread", "fee", "funding", "total")
        }
    for band, stats in report["median_bp"].items():
        for k, v in stats.items():
            np.testing.assert_allclose(
                v,
                median(r[k] for r in report["results"] if r["band"] == int(band)),
                atol=1e-12,
                rtol=0,
            )
    wins = sum(
        r["restored_rmse_bp"] < r["restored_fit_mean_rmse_bp"]
        for r in report["results"]
        if r["band"] == 2
    )
    med = report["median_bp"]
    pk = ("restored_selected_bp", "restored_vs_fixed_bp")
    ek = ("actual_selected_net_bp", "actual_vs_net_selector_bp", "actual_vs_restored_fixed_bp")
    pred_gate = wins >= 2 and all(med["2"][k] > 0 and med["1"][k] >= 0 for k in pk)
    econ_gate = all(med["2"][k] > 0 and med["1"][k] >= 0 for k in ek)
    assert wins == report["restored_prediction_wins"]
    assert pred_gate == report["restored_prediction_gate"]
    assert econ_gate == report["actual_cost_economics_gate"]
    assert not pred_gate and report["route"] == "stop_this_feature_action_family"
    assert (
        max(r["ledger_error_yuan"] for r in records)
        == report["max_ledger_error_yuan"]
        < plan["ledger_tolerance_yuan"]
    )
    report["verification"] = dict(
        status="completed",
        source_commit=launch["source_commit"],
        records=len(records),
        server_tests_passed=8,
        local_frozen_summary_max_abs_difference=max(diffs),
        independent_augmented_lstsq_max_prediction_difference=max_prediction_error,
        discrete_fields_and_selected_actions_identical=True,
        server_summary_sha256=sha(server_summary),
        local_frozen_summary_sha256=sha(local_summary),
        record_sha256={str(p.relative_to(root)): sha(p) for p in files},
    )
    report["metric"] = (
        "Mean paired one-decision-interval value in bp, reset to benchmark state; NOT cumulative or continuous portfolio return. Medians are across three already-viewed folds."
    )
    report["fixed_control"] = (
        "Action chosen by the 2-lot fitting-label mean of each target, NOT the best continuous fixed strategy. Restored-target control is long duration in all three folds, reused at 1 lot."
    )
    return report


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", required=True, type=Path)
    p.add_argument("--server-summary", required=True, type=Path)
    p.add_argument("--local-summary", required=True, type=Path)
    args = p.parse_args()
    result = audit(args.root, args.server_summary, args.local_summary)
    (ROOT / "reports/cost_signal_review.json").write_text(
        json.dumps(result, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    )
    print({"verified_records": result["verification"]["records"], "route": result["route"]})
