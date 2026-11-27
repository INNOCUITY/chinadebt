"""Compute preregistered paired contrasts only for a complete validation design."""

import argparse
import hashlib
import json
from itertools import product
from pathlib import Path
from statistics import mean, median

PLAN = Path(__file__).resolve().parents[1] / "config/experiments/ppo_factorial.json"


def summarize(root):
    design = json.loads(PLAN.read_text())
    digest = hashlib.sha256(PLAN.read_bytes()).hexdigest()
    margins = {}
    paired = {}
    for band in design["bands"]:
        pairs = []
        effects = [[] for _ in range(3)]
        for fold, seed in product(design["folds"], design["seeds"]):
            values = {}
            for arm in design["arms"]:
                path = root / f"fold-{fold}/seed-{seed}/{arm}/validation.json"
                d = json.loads(path.read_text())
                if (d["design_sha256"], d["fold"], d["seed"], d["arm"]) != (
                    digest,
                    fold,
                    seed,
                    arm,
                ):
                    raise ValueError("design or pair mismatch")
                (row,) = [r for r in d["results"] if r["band_lots"] == band]
                if row["update"] != design["updates"] or not 0 <= row["ledger_error_yuan"] <= 1e-5:
                    raise ValueError("invalid validation result")
                values[arm] = row["net_excess_vs_zero_bp"]
            pairs.append(values["active_pct_clip"] - values["total"])
            for i, off in enumerate((False, 1, None)):
                on_values = [v for a, v in values.items() if design["arms"][a][i] != off]
                off_values = [v for a, v in values.items() if design["arms"][a][i] == off]
                effects[i].append(mean(on_values) - mean(off_values))
        paired[band] = dict(
            gains_bp=pairs, median_bp=median(pairs), positive=sum(round(x, 8) > 0 for x in pairs)
        )
        margins[band] = dict(
            zip(("active", "reward_scale", "market_clip"), map(median, effects), strict=True)
        )
    passed = (
        paired[2]["positive"] >= 4 and paired[2]["median_bp"] > 0 and paired[1]["median_bp"] >= 0
    )
    return dict(primary_paired=paired, median_marginal_effect_bp=margins, expand_validation=passed)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("root", type=Path)
    print(json.dumps(summarize(parser.parse_args().root), indent=2, allow_nan=False))
