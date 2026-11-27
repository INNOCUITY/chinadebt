"""One independent GPU worker for the frozen validation-only factorial experiment."""

import argparse
import json
import subprocess
import sys
from itertools import product
from pathlib import Path

from evaluate_rl import sha, validation_rows
from run_ppo_budget_pilot import verify_reference
from train_rl import EVAL_SPEC, FACTOR_SPEC, ROOT, dump, training_spec


def run(shard, out, reference):
    design = json.loads(FACTOR_SPEC.read_text())
    plan = json.loads(EVAL_SPEC.read_text())
    arms = list(design["arms"])
    if shard not in range(len(arms)):
        raise ValueError("unknown shard")
    for i, (fold, seed) in enumerate(product(design["folds"], design["seeds"])):
        arm = arms[(shard + i) % len(arms)]
        folder = out / f"fold-{fold}" / f"seed-{seed}" / arm
        folder.mkdir(parents=True, exist_ok=False)
        ck = folder / "training"
        print(f"training fold={fold} seed={seed} arm={arm}", flush=True)
        subprocess.run(
            [
                sys.executable,
                str(ROOT / "scripts/train_rl.py"),
                "train",
                "--device",
                "cuda",
                "--fold",
                str(fold),
                "--seed",
                str(seed),
                "--arm",
                arm,
                "--output",
                str(ck),
            ],
            cwd=ROOT,
            check=True,
        )
        spec = training_spec(fold, seed, arm=arm)
        results = []
        for band in design["bands"]:
            (row,) = validation_rows(plan, spec, ck, [design["updates"]], band)
            if arm == "total" and band == 2:
                original = reference / f"seed-{seed}/fold-{fold}/selection.json"
                verify_reference(json.loads(original.read_text()), fold, seed, row, spec)
            results.append(dict(band_lots=band, **row))
        dump(
            folder / "validation.json",
            dict(
                design_sha256=sha(FACTOR_SPEC),
                fold=fold,
                seed=seed,
                arm=arm,
                validation=spec["validation"],
                results=results,
            ),
        )
    print("succeeded", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--shard", type=int, required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--reference", type=Path, required=True)
    args = parser.parse_args()
    run(args.shard, args.output.resolve(), args.reference.resolve())
