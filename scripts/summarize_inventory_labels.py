"""Quantify how often paired inventory states require different actions on training labels."""

import argparse
import json
from pathlib import Path
from statistics import mean


def paired_opportunity(pairs):
    # Best separate choices versus best common choice, with equal weight per path and date.
    gaps = []
    disjoint = 0
    for left, right in pairs:
        common = [mean((a, b)) for a, b in zip(left, right, strict=True)]
        gap = mean((max(left), max(right))) - max(common)
        gaps.append(gap)
        disjoint += not any(
            max(left) - a <= 1e-8 and max(right) - b <= 1e-8
            for a, b in zip(left, right, strict=True)
        )
    return dict(dates=len(gaps), disjoint_best_dates=disjoint, mean_hindsight_gain_bp=mean(gaps))


def summarize(root):
    results = {}
    for fold in (0, 10, 21):
        dest = root / f"fold-{fold}"
        model = json.loads((dest / "model.json").read_text())
        pairs = {}
        for file in (dest / "labels").glob("*.json"):
            row = json.loads(file.read_text())
            key = (row["signal"], row["horizon"])
            states = pairs.setdefault(key, {})
            if (
                row["fold"] != fold
                or row["design_sha256"] != model["design_sha256"]
                or row["path"] not in (0, 1)
                or row["path"] in states
                or len(row["net_bp"]) != 4
            ):
                raise ValueError("unmatched training label")
            states[row["path"]] = row["net_bp"]
        if len(pairs) != model["fit_intervals"] or any(len(v) != 2 for v in pairs.values()):
            raise ValueError("incomplete training pairs")
        results[fold] = paired_opportunity((v[0], v[1]) for _, v in sorted(pairs.items()))
    return results


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", required=True, type=Path)
    print(
        json.dumps(summarize(parser.parse_args().root), sort_keys=True, indent=2, allow_nan=False)
    )
