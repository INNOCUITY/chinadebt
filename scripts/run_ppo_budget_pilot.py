"""Run one seed of the frozen training-budget pilot, using validation only."""

import argparse
import json
import subprocess
import sys
from pathlib import Path

from evaluate_rl import sha, validation_rows
from train_rl import EVAL_SPEC, PILOT_SPEC, ROOT, dump, training_spec


def verify_reference(report, fold, seed, row, spec):
    if (
        report["fold"] != fold
        or report["seed"] != seed
        or report["plan_sha256"] != sha(EVAL_SPEC)
        or report["validation"] != spec["validation"]
    ):
        raise ValueError("validation reference mismatch")
    original = next(r for r in report["candidates"] if r["update"] == 2)
    for key in ("net_excess_vs_zero_bp", "te_vs_zero_bp", "turnover", "trades", "net_cost_yuan"):
        if abs(original[key] - row[key]) > 1e-7:
            raise ValueError(f"update 2 does not reproduce archived validation: {key}")


def run(seed, output, reference, device):
    pilot = json.loads(PILOT_SPEC.read_text())
    plan = json.loads(EVAL_SPEC.read_text())
    if seed not in pilot["seeds"] or pilot["band_lots"] != plan["bands"][0]:
        raise ValueError("seed or primary execution band outside frozen plan")
    out = output / f"seed-{seed}"
    out.mkdir(parents=True, exist_ok=False)
    state = dict(state="running", seed=seed, pilot_sha256=sha(PILOT_SPEC), completed=[])
    try:
        for fold in pilot["folds"]:
            folder = out / f"fold-{fold}"
            folder.mkdir()
            spec = training_spec(fold, seed, budget_pilot=True)
            ck = folder / "training"
            state.update(fold=fold, stage="train")
            dump(out / "status.json", state)
            subprocess.run(
                [
                    sys.executable,
                    str(ROOT / "scripts/train_rl.py"),
                    "train",
                    "--device",
                    device,
                    "--budget-pilot",
                    "--seed",
                    str(seed),
                    "--fold",
                    str(fold),
                    "--output",
                    str(ck),
                ],
                check=True,
                cwd=ROOT,
            )
            state["stage"] = "validation"
            dump(out / "status.json", state)
            rows = validation_rows(plan, spec, ck, pilot["updates"], pilot["band_lots"])
            report = json.loads((reference / out.name / folder.name / "selection.json").read_text())
            verify_reference(report, fold, seed, rows[0], spec)
            dump(
                folder / "validation.json",
                dict(
                    fold=fold,
                    seed=seed,
                    validation=spec["validation"],
                    pilot_sha256=sha(PILOT_SPEC),
                    reference_reproduced=True,
                    candidates=rows,
                ),
            )
            state["completed"].append(fold)
        state.update(state="succeeded", stage="done")
    except BaseException:
        state["state"] = "failed"
        raise
    finally:
        dump(out / "status.json", state)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--seed", type=int, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--reference", type=Path, required=True)
    p.add_argument("--device", choices=["cpu", "cuda"], required=True)
    args = p.parse_args()
    run(args.seed, args.output.resolve(), args.reference.resolve(), args.device)
