"""Replay saved validation actors and fixed hold/duration probes on matched economic ledgers."""

import argparse
import json
from collections import Counter
from dataclasses import asdict
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import torch
from evaluate_rl import Market, load_actor, replay, score, sha
from train_rl import EVAL_SPEC, FACTOR_SPEC, NEUTRAL_SPEC, dump, training_spec


def decision_ledger(rollout, proposals, book, start):
    exposure = rollout.result.exposure.set_index("date")
    dates = list(book.index)
    if [r.date for r in rollout.records] != dates[1:]:
        raise ValueError("ledger date alignment failed")
    rows = {}
    for signal, r in zip(dates[:-1], rollout.records, strict=True):
        if r.date < start:
            continue
        x = exposure.loc[r.date]
        row = dict(
            signal_date=signal,
            date=r.date,
            decision=signal in proposals and signal >= start,
            proposal_z=proposals.get(signal),
            target_krd=r.original_action.target_krd,
            projected_krd=r.projected_target.krd if r.projected_target else None,
            accepted_orders=r.accepted_orders,
            trades=r.executed_trades,
            post_trade_faces=dict(r.post_trade_faces),
            outcome=r.action_outcome,
            source=r.policy_source,
            risk_off=r.risk_off,
            policy_events=[asdict(p) for p in r.policy_actions],
            reject_reason=r.reject_reason,
            attempts=r.gate_attempts,
            cost_yuan=float(book.at[r.date, "cost"]),
            active_dv01=float(x.total_dv01 - x.bench_total_dv01),
            net_pnl_yuan=float(book.at[r.date, "net_nav"] - book.at[signal, "net_nav"]),
        )
        rows[r.date] = row
    return rows


def compare(rows, zero):
    if rows.keys() != zero.keys():
        raise ValueError("matched book dates differ")
    for d, row in rows.items():
        ref = zero[d]
        row.update(
            zero_faces=ref["post_trade_faces"],
            zero_trades=ref["trades"],
            dv01_vs_zero=row["active_dv01"] - ref["active_dv01"],
            cost_vs_zero_yuan=row["cost_yuan"] - ref["cost_yuan"],
            pnl_vs_zero_yuan=row["net_pnl_yuan"] - ref["net_pnl_yuan"],
            same_faces=row["post_trade_faces"] == ref["post_trade_faces"],
        )
    decisions = [r for r in rows.values() if r["decision"]]
    return dict(
        days=len(rows),
        same_faces_days=sum(r["same_faces"] for r in rows.values()),
        decision_count=len(decisions),
        outcomes=dict(Counter(r["outcome"] for r in decisions)),
        mean_abs_dv01_vs_zero=float(np.mean([abs(r["dv01_vs_zero"]) for r in rows.values()])),
        pnl_vs_zero_yuan=sum(r["pnl_vs_zero_yuan"] for r in rows.values()),
    )


def audit_pair(root, output, fold, band, probes_only=False, neutral=False):
    design = json.loads(FACTOR_SPEC.read_text())
    plan = json.loads(EVAL_SPEC.read_text())
    if fold not in design["folds"] or band not in design["bands"]:
        raise ValueError("unregistered validation window or band")
    start, end = map(date.fromisoformat, plan["folds"][fold]["validation"])
    anchor = date.fromisoformat(plan["anchor"])
    market = Market(end)
    prior = market.calendar.prev_trading_day(start)

    def run(action):
        rows = {}

        def trace(rollout, proposals, book):
            rows.update(decision_ledger(rollout, proposals, book, start))

        book, error = replay(
            market,
            anchor,
            end,
            band,
            lambda ctx, prev: action(ctx, prev) if ctx.as_of >= start else 0.0,
            audit=trace,
        )
        return book, rows, error

    zero, zero_rows, _ = run(lambda ctx, prev: 0.0)
    dest = output / f"fold-{fold}" / f"band-{band}"
    dest.mkdir(exist_ok=False, parents=True)

    def record(name, action, archived=None, path=None):
        book, rows, error = run(action)
        stats = score(book, zero, start, end, market.calendar, market.cfg.backtest.annual_days)
        paired = compare(rows, zero_rows)
        opening = zero.at[prior, "net_nav"]
        if (
            abs(opening - book.at[prior, "net_nav"]) > 1e-5
            or abs(paired["pnl_vs_zero_yuan"] - stats["net_excess_vs_zero_bp"] * opening / 1e4)
            > 1e-5
        ):
            raise ValueError("paired daily net P&L failed reconciliation")
        if archived:
            for k, v in stats.items():
                if not np.isclose(v, archived[k], atol=1e-7, rtol=0):
                    raise ValueError(f"archived metric differs: {name}/{k}")
        report = dict(
            fold=fold,
            band_lots=band,
            policy=name,
            metrics=stats,
            execution=paired,
            ledger_error_yuan=error,
        )
        if path:
            ident = torch.load(path, map_location="cpu", weights_only=False)["identity"]
            decisions = [r["signal_date"] for r in rows.values() if r["decision"]]
            cutoffs = [pd.Timestamp(market.calendar.prev_trading_day(d)) for d in decisions]
            raw = (market.features.loc[cutoffs] - np.asarray(ident["mean"])) / np.asarray(
                ident["scale"]
            )
            report["validation_feature_counts"] = {
                k: dict(valid=int(v.count()), outside_5=int((v.abs() > 5).sum()))
                for k, v in raw.items()
            }
            report["checkpoint_sha256"] = sha(path)
        dump(dest / f"{name}-days.json", list(rows.values()))
        dump(dest / f"{name}.json", report)

    if neutral:
        for seed in design["seeds"]:
            spec = training_spec(
                fold,
                seed,
                action_grid=True,
                grid_active=True,
                zero_critic=True,
                critic_budget=True,
                invested_start=True,
                actor_step=True,
                neutral_actor=True,
            )
            src = root / f"fold-{fold}/seed-{seed}"
            archived = json.loads((src / "validation.json").read_text())
            (row,) = [r for r in archived["rows"] if r["band"] == band and r["update"] == 16]
            ck = src / "training/update-16.pt"
            if (
                archived["fold"],
                archived["seed"],
                archived["neutral_actor_sha256"],
                row["checkpoint_sha256"],
            ) != (fold, seed, sha(NEUTRAL_SPEC), sha(ck)):
                raise ValueError("neutral actor archive mismatch")
            record(f"neutral-{seed}", load_actor(ck, spec, 16, market, market.cfg), row, ck)
        return
    for name, z in (("hold", None), ("short_duration", -1.0), ("long_duration", 1.0)):
        record(name, lambda ctx, prev, z=z: z)
    if probes_only:
        return
    for seed in design["seeds"]:
        for arm in design["arms"]:
            spec = training_spec(fold, seed, arm=arm)
            src = root / f"fold-{fold}" / f"seed-{seed}" / arm
            archived = json.loads((src / "validation.json").read_text())
            (row,) = [r for r in archived["results"] if r["band_lots"] == band]
            ck = src / "training/update-2.pt"
            if (
                archived["design_sha256"] != sha(FACTOR_SPEC)
                or row["checkpoint_sha256"] != sha(ck)
                or (archived["fold"], archived["seed"], archived["arm"]) != (fold, seed, arm)
            ):
                raise ValueError("reference identity differs")
            record(f"{arm}-{seed}", load_actor(ck, spec, 2, market, market.cfg), row, ck)


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, required=True)
    p.add_argument("--output", type=Path, required=True)
    p.add_argument("--fold", type=int, required=True)
    p.add_argument("--band", type=int, required=True)
    mode = p.add_mutually_exclusive_group()
    mode.add_argument("--neutral", action="store_true")
    mode.add_argument("--probes-only", action="store_true")
    a = p.parse_args()
    audit_pair(a.root, a.output, a.fold, a.band, a.probes_only, a.neutral)
