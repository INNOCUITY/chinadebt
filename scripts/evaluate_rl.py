"""冻结第 0 折或跨折 PPO 选择与连续账本测试。运行产物只写到指定目录。"""

from __future__ import annotations

import argparse
import hashlib
import json
from collections import Counter, defaultdict
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import torch
from run_strategies import load_market
from train_rl import EVAL_SPEC, ROOT, dump, market_digest, training_spec

from ficc.research.protocol import (
    prediction_to_target,
    prediction_to_z,
    protocol_config,
    protocol_fingerprint,
)
from ficc.rl.collector import Encoder
from ficc.rl.env import RebalanceEnv, _estimated_trade_fee
from ficc.rl.ppo import ActorCritic


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def load_actor(path, spec, update, market, cfg):
    """只接受已登记 fold/seed 的完整训练产物及原始标准化统计。"""
    saved = torch.load(path, map_location="cpu", weights_only=False)
    ident = saved["identity"]
    digest = hashlib.sha256(json.dumps(ident, sort_keys=True).encode()).hexdigest()
    if (
        saved["completed"] != update
        or saved["fingerprint"] != digest
        or ident["spec"] != spec
        or ident["protocol"] != protocol_fingerprint(cfg)
        or ident["data"] != market.training_hash(spec)
        or ident["config_sha256"] != hashlib.sha256(cfg.model_dump_json().encode()).hexdigest()
    ):
        raise ValueError("checkpoint 与冻结训练窗口、输入数据或配置不匹配")
    encoder = Encoder(
        market.features,
        np.array(ident["mean"]),
        np.array(ident["scale"]),
        tuple(cfg.portfolio.universe),
    )
    if (
        encoder.names != ident["schema"]
        or encoder.mean.shape != (len(market.features.columns),)
        or encoder.scale.shape != encoder.mean.shape
        or not np.isfinite(encoder.mean).all()
        or not np.isfinite(encoder.scale).all()
        or np.any(encoder.scale <= 0)
    ):
        raise ValueError("checkpoint 观测或归一化状态无效")
    model = ActorCritic(len(encoder.names), spec["hidden"])
    model.load_state_dict(saved["model"])
    model.eval()

    @torch.inference_mode()
    def act(ctx, previous):
        obs = torch.tensor(encoder.encode(ctx, previous), dtype=torch.float32)
        value = model.actor(obs).tanh().item()
        if not np.isfinite(value):
            raise ValueError("非有限策略动作")
        return value

    return act


class Market:
    def __init__(self, end):
        self.cfg = protocol_config()
        panel, funding, self.calendar = load_market(self.cfg)
        self.panel = panel.loc[: pd.Timestamp(end)]
        self.funding = funding.loc[: pd.Timestamp(end)]
        self.features = Encoder.market_features(self.panel, self.funding)

    def training_hash(self, spec):
        end = pd.Timestamp(spec["train"][1])
        return market_digest(self.panel.loc[:end], self.funding.loc[:end])


def daily_book(rollout, cfg):
    """交易日计提费用、结算日释放计提。用日末净值和独立账本恒等式对账。"""
    result = rollout.result
    daily = result.costs.set_index("date")[["total"]].rename(columns={"total": "cost"}).copy()
    daily["nav"] = result.nav
    daily["turnover"] = result.turnover
    deltas = defaultdict(float)
    counts = Counter()
    for trade in result.trades.itertuples():
        fee = _estimated_trade_fee(
            cfg=cfg, face=trade.face_value, clean_price=trade.clean_price, contract_fee=trade.fee
        )
        deltas[trade.trade_date] += fee
        deltas[trade.settlement_date] -= fee
        counts[trade.trade_date] += 1
    change = np.array([deltas[d] for d in daily.index])
    daily["net_nav"] = daily.nav - change.cumsum()
    daily["cost"] += change
    daily["trades"] = [counts[d] for d in daily.index]
    exposure = result.exposure.set_index("date")
    balance = (exposure.cash + exposure.pending_cash + exposure.market_value).reindex(daily.index)
    reward_sum = sum(r.reward for r in rollout.records) - rollout.terminal.accrued_fees
    errors = [
        float((daily.nav - balance).abs().max()),
        abs(daily.net_nav.iloc[-1] - rollout.terminal.net_terminal_value),
        abs(daily.net_nav.iloc[-1] - daily.nav.iloc[0] - reward_sum),
    ]
    if (
        not np.isfinite(daily.to_numpy()).all()
        or balance.isna().any()
        or max(errors) > 1e-5
        or daily.trades.sum() != len(result.trades)
    ):
        raise ValueError(f"持仓/现金、终值、奖励或成交对账失败 {errors}")
    return daily, max(errors)


def replay(market, anchor, end, band, action):
    cfg = market.cfg
    cfg = cfg.model_copy(
        update={"constraints": cfg.constraints.model_copy(update={"rebalance_band_lots": band})}
    )
    env = RebalanceEnv(
        market.panel,
        market.calendar,
        market.funding,
        cfg,
        decision_step_days=5,
        information_lag_days=1,
    )
    previous = 0.0

    def policy(ctx):
        nonlocal previous
        if ctx.as_of == end:
            return None
        previous = action(ctx, previous)
        return prediction_to_target(previous, ctx.benchmark, as_of=ctx.as_of)

    return daily_book(env.rollout(policy, anchor, end), cfg)


def score(book, zero, start, end, calendar, annual_days):
    prior = calendar.prev_trading_day(start)
    path = book.loc[prior:end, "net_nav"]
    control = zero.loc[prior:end, "net_nav"]
    if (
        not path.index.equals(control.index)
        or len(path) < 2
        or path.index[0] != prior
        or path.index[-1] != end
    ):
        raise ValueError("比较日期不一致")
    window = book.loc[start:end]
    ret = path.iloc[-1] / path.iloc[0] - 1
    excess = ret - (control.iloc[-1] / control.iloc[0] - 1)
    te = (
        (path.pct_change() - control.pct_change()).dropna().std(ddof=0) * np.sqrt(annual_days) * 1e4
    )
    return dict(
        net_return_bp=ret * 1e4,
        net_excess_vs_zero_bp=excess * 1e4,
        te_vs_zero_bp=te,
        net_cost_yuan=window.cost.sum(),
        turnover=window.turnover.sum(),
        trades=int(window.trades.sum()),
    )


def winner(rows):
    return max(rows, key=lambda r: (round(r["net_excess_vs_zero_bp"], 8), -r["update"]))


def select(plan, fold, seed, checkpoints, out):
    spec = training_spec(fold, seed)
    start, end = map(date.fromisoformat, spec["validation"])
    anchor = date.fromisoformat(plan["anchor"])
    market = Market(end)
    band = plan["bands"][0]
    zero, _ = replay(market, anchor, end, band, lambda ctx, prev: 0.0)
    rows = []
    for update in plan["updates"]:
        p = checkpoints / f"update-{update}.pt"
        act = load_actor(p, spec, update, market, market.cfg)
        book, error = replay(
            market,
            anchor,
            end,
            band,
            lambda ctx, prev, act=act: act(ctx, prev) if ctx.as_of >= start else 0.0,
        )
        rows.append(
            dict(
                update=update,
                checkpoint_sha256=sha(p),
                ledger_error_yuan=error,
                **score(book, zero, start, end, market.calendar, market.cfg.backtest.annual_days),
            )
        )
    best = winner(rows)
    result = dict(
        fold=fold,
        seed=seed,
        plan_sha256=sha(EVAL_SPEC),
        validation=spec["validation"],
        candidates=rows,
        update=best["update"],
        checkpoint_sha256=best["checkpoint_sha256"],
    )
    dump(out / "selection.json", result)


def evaluate(plan, folds, seed, root, out):
    """每个 seed/执行带/模型一条全期账本；换折只切换策略参数。"""
    anchor = date.fromisoformat(plan["anchor"])
    specs = [training_spec(f, seed) for f in folds]
    first, last = map(date.fromisoformat, [specs[0]["test"][0], specs[-1]["test"][1]])
    market = Market(last)
    predictions = ROOT / "reports/ml_oos_predictions.csv"
    if sha(predictions) != plan["prediction_sha256"]:
        raise ValueError("ML 预测文件散列不一致")
    oos = pd.read_csv(
        predictions,
        usecols=["split_id", "decision_as_of", "pred_ridge", "pred_tree", "pred_train_mean"],
    )
    grid = market.calendar.trading_days_between(anchor, last)[::5]
    signed = {}
    ml = {}
    selections = []
    for fold, spec in zip(folds, specs, strict=True):
        folder = root / f"seed-{seed}/fold-{fold}"
        picked = json.loads((folder / "selection.json").read_text())
        if (
            picked["plan_sha256"] != sha(EVAL_SPEC)
            or (picked["fold"], picked["seed"]) != (fold, seed)
            or picked["update"] != winner(picked["candidates"])["update"]
        ):
            raise ValueError("验证选择与冻结方案不匹配")
        ckpt = folder / "training" / f"update-{picked['update']}.pt"
        if sha(ckpt) != picked["checkpoint_sha256"]:
            raise ValueError("被选 checkpoint 已改变")
        act = load_actor(ckpt, spec, picked["update"], market, market.cfg)
        start, end = map(date.fromisoformat, spec["test"])
        dates = {d for d in grid if start <= d <= end}
        rows = oos[oos.split_id == fold].copy()
        rows.index = pd.to_datetime(rows.decision_as_of).dt.date
        if rows.index.has_duplicates or set(rows.index) != dates or dates.intersection(signed):
            raise ValueError("模型的公共测试网格不一致")
        signed.update(dict.fromkeys(dates, act))
        ml.update(rows.to_dict("index"))
        selections.append({k: picked[k] for k in ("fold", "seed", "update", "checkpoint_sha256")})
    summary = []
    for band in plan["bands"]:
        zero, error = replay(market, anchor, last, band, lambda ctx, prev: 0.0)
        for model in plan["models"]:
            if model == "zero":
                book, err = zero, error
            elif model == "ppo":

                def action(ctx, previous):
                    return signed[ctx.as_of](ctx, previous) if ctx.as_of in signed else 0.0

                book, err = replay(market, anchor, last, band, action)
            else:

                def action(ctx, previous, column="pred_" + model):
                    return prediction_to_z(ml[ctx.as_of][column]) if ctx.as_of in ml else 0.0

                book, err = replay(market, anchor, last, band, action)
            if not np.allclose(
                book.loc[: market.calendar.prev_trading_day(first), "net_nav"],
                zero.loc[: market.calendar.prev_trading_day(first), "net_nav"],
                atol=1e-6,
                rtol=0,
            ):
                raise ValueError("各模型测试前账本不一致")
            book.to_csv(out / f"{model}-{band}lot-daily.csv")
            for label, begin, finish in [
                ("pooled", first, last),
                *[(s["fold"], *map(date.fromisoformat, s["test"])) for s in specs],
            ]:
                summary.append(
                    dict(
                        model=model,
                        band_lots=band,
                        window=label,
                        ledger_error_yuan=err,
                        **score(
                            book,
                            zero,
                            begin,
                            finish,
                            market.calendar,
                            market.cfg.backtest.annual_days,
                        ),
                    )
                )
    dump(
        out / "test.json",
        dict(plan_sha256=sha(EVAL_SPEC), seed=seed, selections=selections, metrics=summary),
    )


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["select", "test"])
    parser.add_argument("--root", required=True, type=Path)
    parser.add_argument("--fold", type=int, action="append", required=True)
    parser.add_argument("--seed", type=int, required=True)
    parser.add_argument("--output", type=Path)
    a = parser.parse_args(argv)
    plan = json.loads(EVAL_SPEC.read_text())
    if a.fold != sorted(set(a.fold)) or (a.mode == "select" and len(a.fold) != 1):
        parser.error("按顺序选择折，select 每次只选一折")
    torch.set_num_threads(1)
    folder = a.root / f"seed-{a.seed}/fold-{a.fold[0]}"
    if a.mode == "select":
        select(plan, a.fold[0], a.seed, folder / "training", folder)
    else:
        if a.output is None:
            parser.error("test 需要 --output")
        a.output.mkdir(parents=True, exist_ok=False)
        evaluate(plan, a.fold, a.seed, a.root, a.output)


if __name__ == "__main__":
    main()
