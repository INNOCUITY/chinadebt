"""Check the cost-only explanation against the already archived matched PPO books."""

import hashlib
import json
from pathlib import Path
from statistics import median

ROOT = Path(__file__).resolve().parents[1]


def review(matrix):
    result = {}
    for band, data in matrix["bands"].items():
        rows = [
            dict(
                seed=r["seed"],
                net_excess_bp=r["excess_bp"],
                realized_cost_difference_bp=r["cost_delta_bp"],
                fixed_path_cost_restored_bp=r["excess_bp"] + r["cost_delta_bp"],
            )
            for r in data["seeds"]
        ]
        result[band] = dict(
            rows=rows,
            net_median_bp=median(r["net_excess_bp"] for r in rows),
            restored_median_bp=median(r["fixed_path_cost_restored_bp"] for r in rows),
            positive_restored_seeds=sum(r["fixed_path_cost_restored_bp"] > 1e-8 for r in rows),
            turnover_range=[
                min(r["turnover"] for r in data["seeds"]),
                max(r["turnover"] for r in data["seeds"]),
            ],
            cost_delta_range_bp=[
                min(r["cost_delta_bp"] for r in data["seeds"]),
                max(r["cost_delta_bp"] for r in data["seeds"]),
            ],
        )
    return dict(
        test=matrix["test"],
        bands=result,
        conclusion="The fixed-path accounting result rejects a costs-only explanation for these PPO policies; it does not prove signal absence or zero-cost training failure.",
        scope="Restore realized incremental spread, fees (including terminal accrual) and funding to matched net excess. Original decisions, holdings and funding events stay fixed; no reoptimized or zero-cost-trained strategy is simulated.",
    )


if __name__ == "__main__":
    path = ROOT / "reports/ppo_matrix_review.json"
    report = review(json.loads(path.read_text()))
    report["source_sha256"] = hashlib.sha256(path.read_bytes()).hexdigest()
    (ROOT / "reports/research_pivot_cost_audit.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n"
    )
    print({b: v["restored_median_bp"] for b, v in report["bands"].items()})
