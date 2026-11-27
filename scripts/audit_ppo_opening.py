"""Paired deterministic training replay from cash and an existing zero-tilt ledger."""

import argparse
import json
from datetime import date
from pathlib import Path

import torch
from evaluate_rl import Market, load_actor, replay, sha
from train_rl import CRITIC_BUDGET, EVAL_SPEC, GRID_SPEC, dump, training_spec


def audit(root, output, fold):
    grid = json.loads(GRID_SPEC.read_text())
    if fold not in grid["folds"]:
        raise ValueError("unregistered fold")
    end = date.fromisoformat(json.loads(EVAL_SPEC.read_text())["folds"][fold]["train"][1])
    anchor = date.fromisoformat(json.loads(EVAL_SPEC.read_text())["anchor"])
    market = Market(end)
    results = []
    for seed in grid["seeds"]:
        folder = root / f"fold-{fold}/seed-{seed}"
        checkpoint = folder / "training/update-16.pt"
        valid = json.loads((folder / "validation.json").read_text())
        spec = training_spec(
            fold, seed, action_grid=True, grid_active=True, zero_critic=True, critic_budget=True
        )
        if (
            valid["design_sha256"] != sha(CRITIC_BUDGET)
            or (valid["fold"], valid["seed"]) != (fold, seed)
            or any(row["checkpoint_sha256"] != sha(checkpoint) for row in valid["rows"])
        ):
            raise ValueError("archive mismatch")
        actor = load_actor(checkpoint, spec, 16, market, market.cfg)
        ident = torch.load(checkpoint, map_location="cpu", weights_only=False)["identity"]
        start, last = map(date.fromisoformat, ident["episode"])
        if last != end or not anchor < start:
            raise ValueError("invalid training window")
        row = dict(seed=seed)
        for name, begin in (("cash", start), ("invested", anchor)):
            zero, _ = replay(market, begin, end, 2, lambda ctx, prev: 0.0)
            entry = {}

            def action(ctx, prev, start=start, entry=entry, actor=actor):
                if ctx.as_of < start:
                    return 0.0
                if not entry:
                    entry.update(
                        first_decision=str(ctx.as_of),
                        opening_face=sum(p.face_value for p in ctx.positions),
                    )
                return actor(ctx, prev)

            book, error = replay(market, begin, end, 2, action)
            opening = float(zero.at[start, "net_nav"])
            if abs(book.at[start, "net_nav"] - opening) > 1e-5 or entry["first_decision"] != str(
                start
            ):
                raise ValueError("opening ledger or decision grid mismatch")
            if (name == "invested") != (entry["opening_face"] > 0):
                raise ValueError("opening holdings mismatch")
            gain = float((book.net_nav.iloc[-1] - zero.net_nav.iloc[-1]) / opening * 1e4)
            row[name] = dict(
                **entry,
                net_excess_bp=gain,
                trades_after_open=int(book.trades.loc[book.index > start].sum()),
                ledger_error_yuan=error,
            )
        archived = json.loads((folder / "training_curve.json").read_text())[-1]
        if (
            archived["update"] != 16
            or abs(row["cash"]["net_excess_bp"] - archived["train_active_bp"]) > 1e-6
        ):
            raise ValueError("cash start did not reproduce archived training score")
        results.append(row)
    dump(output, dict(fold=fold, results=results))


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--fold", type=int, required=True)
    a = p.parse_args()
    audit(a.root, a.output, a.fold)
