"""第 0 折的固定回放、单卡 PPO 和更新边界恢复。"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import platform
import random
import resource
import subprocess
import sys
from collections import Counter
from dataclasses import asdict
from datetime import date
from pathlib import Path
from time import perf_counter

import numpy as np
import pandas as pd
import torch
from run_strategies import load_market

from ficc.research.protocol import ExperimentProtocol, protocol_config, protocol_fingerprint
from ficc.rl.collector import Draw, Encoder, apply_training_reward, collect
from ficc.rl.env import RebalanceEnv
from ficc.rl.ppo import ActorCritic, update

ROOT = Path(__file__).resolve().parents[1]
SPEC = ROOT / "config/experiments/ppo_smoke.json"


EVAL_SPEC = ROOT / "config/experiments/ppo_evaluation.json"


PILOT_SPEC = ROOT / "config/experiments/ppo_budget_pilot.json"


FACTOR_SPEC = ROOT / "config/experiments/ppo_factorial.json"


GRID_SPEC = ROOT / "config/experiments/ppo_action_grid.json"


CRITIC_SPEC = ROOT / "config/experiments/ppo_critic_init.json"


def training_spec(
    fold=None,
    seed=None,
    *,
    budget_pilot=False,
    arm=None,
    action_grid=False,
    grid_active=False,
    zero_critic=False,
):
    spec = json.loads(SPEC.read_text())
    if fold is not None:
        plan = json.loads(EVAL_SPEC.read_text())
        if fold < 0 or fold >= len(plan["folds"]) or seed not in plan["seeds"]:
            raise ValueError("fold 或 seed 超出冻结实验清单")
        spec.update(plan["folds"][fold], seed=seed)
    if budget_pilot:
        pilot = json.loads(PILOT_SPEC.read_text())
        if fold not in pilot["folds"] or seed not in pilot["seeds"]:
            raise ValueError("fold/seed outside budget pilot")
        spec.update(updates=max(pilot["updates"]), budget_protocol=pilot["version"])
    if arm is not None:
        design = json.loads(FACTOR_SPEC.read_text())
        if budget_pilot or fold not in design["folds"] or seed not in design["seeds"]:
            raise ValueError("invalid factorial experiment pair")
        active, scale, clip = design["arms"][arm]
        spec.update(
            arm=arm,
            active_reward=active,
            reward_scale=scale,
            market_clip=clip,
            updates=design["updates"],
            design_sha256=hashlib.sha256(FACTOR_SPEC.read_bytes()).hexdigest(),
        )
    if grid_active and not action_grid:
        raise ValueError("grid reward requires categorical actions")
    if action_grid:
        grid = json.loads(GRID_SPEC.read_text())
        if arm or budget_pilot or fold not in grid["folds"] or seed not in grid["seeds"]:
            raise ValueError("invalid action-grid experiment pair")
        spec.update(
            actions=grid["actions"],
            updates=grid["updates"],
            grid_sha256=hashlib.sha256(GRID_SPEC.read_bytes()).hexdigest(),
        )
    if grid_active:
        spec.update(active_reward=True, reward_scale=1)
    if zero_critic:
        if not action_grid:
            raise ValueError("critic pilot requires registered categorical policy")
        spec["critic_init_sha256"] = hashlib.sha256(CRITIC_SPEC.read_bytes()).hexdigest()
    return spec


def market_digest(panel, funding):
    digest = hashlib.sha256()
    for frame in (panel, funding):
        digest.update(pd.util.hash_pandas_object(frame, index=True).to_numpy().tobytes())
    return digest.hexdigest()


def dump(path, obj):
    data = json.dumps(obj, default=str, ensure_ascii=False, allow_nan=False)
    tmp = path.with_suffix(".tmp")
    tmp.write_text(data + "\n")
    tmp.replace(path)


def rng_state(device):
    return dict(
        python=random.getstate(),
        numpy=np.random.get_state(),  # noqa: NPY002 - checkpoint includes process-global NumPy RNG
        cpu=torch.get_rng_state(),
        cuda=torch.cuda.get_rng_state(device) if device.type == "cuda" else None,
    )


def restore_rng(state, device):
    random.setstate(state["python"])
    np.random.set_state(state["numpy"])  # noqa: NPY002 - checkpoint includes process-global NumPy RNG
    torch.set_rng_state(state["cpu"])
    if device.type == "cuda":
        torch.cuda.set_rng_state(state["cuda"], device)


def state_equal(a, b):
    if isinstance(a, torch.Tensor):
        return torch.equal(a.cpu(), b.cpu())
    if isinstance(a, np.ndarray):
        return np.array_equal(a, b)
    if isinstance(a, dict):
        return a.keys() == b.keys() and all(state_equal(a[k], b[k]) for k in a)
    if isinstance(a, (tuple, list)):
        return len(a) == len(b) and all(state_equal(x, y) for x, y in zip(a, b, strict=True))
    return a == b


def load_boundary(path, fingerprint, model, optimizer, device):
    # 仅加载本项目生成的可信 checkpoint，NumPy/Python RNG 需要完整反序列化。
    saved = torch.load(path, map_location="cpu", weights_only=False)
    if saved["fingerprint"] != fingerprint:
        raise ValueError("checkpoint 的源码、环境、数据或训练协议不匹配")
    model.load_state_dict(saved["model"])
    optimizer.load_state_dict(saved["optimizer"])
    restore_rng(saved["rng"], device)
    return saved["completed"]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("mode", choices=["profile", "train", "resume"])
    parser.add_argument("--device", choices=["cpu", "cuda"], required=True)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--arm", choices=json.loads(FACTOR_SPEC.read_text())["arms"])
    parser.add_argument("--grid-active", action="store_true")
    parser.add_argument("--zero-critic", action="store_true")
    parser.add_argument("--action-grid", action="store_true")
    parser.add_argument("--budget-pilot", action="store_true")
    parser.add_argument("--fold", type=int)
    parser.add_argument("--seed", type=int)
    parser.add_argument("--checkpoint", type=Path)
    parser.add_argument("--verify-resume", action="store_true")
    parser.add_argument(
        "--stop-after", type=int, help="完成指定次数的更新后正常退出并保留 checkpoint"
    )
    args = parser.parse_args(argv)
    if (args.mode == "resume") != bool(args.checkpoint):
        parser.error("resume 需要指定 --checkpoint")
    if (args.fold is None) != (args.seed is None):
        parser.error("--fold 和 --seed 必须配对")
    spec = training_spec(
        args.fold,
        args.seed,
        budget_pilot=args.budget_pilot,
        arm=args.arm,
        action_grid=args.action_grid,
        grid_active=args.grid_active,
        zero_critic=args.zero_critic,
    )
    if args.stop_after is not None and not 1 <= args.stop_after <= spec["updates"]:
        parser.error("stop-after 超出更新预算")
    out = args.output
    out.mkdir(parents=True, exist_ok=False)
    os.environ.setdefault("CUBLAS_WORKSPACE_CONFIG", ":4096:8")
    torch.use_deterministic_algorithms(True)
    torch.set_num_threads(1)
    device = torch.device(args.device)
    if device.type == "cuda" and torch.cuda.device_count() != 1:
        raise RuntimeError("单卡训练需要恰好一张可见 GPU")
    seed = spec["seed"]
    random.seed(seed)
    np.random.seed(seed)  # noqa: NPY002 - checkpoint includes process-global NumPy RNG
    torch.manual_seed(seed)
    load_start = perf_counter()
    protocol = ExperimentProtocol()
    cfg = protocol_config(protocol=protocol)
    if cfg.constraints.rebalance_band_lots != spec["primary_band_lots"]:
        raise ValueError("配置与冻结的主执行带不同")
    panel, funding, calendar = load_market(cfg)
    start, end = map(date.fromisoformat, spec["train"])
    days = calendar.trading_days_between(start, end)
    calibration = spec["calibration_days"]
    if calibration % protocol.decision_step_days or end >= date.fromisoformat(
        spec["validation"][0]
    ):
        raise ValueError("第 0 折日期或网格未对齐")
    start = days[calibration]
    if args.fold is not None:
        anchor = date.fromisoformat(json.loads(EVAL_SPEC.read_text())["anchor"])
        grid = protocol.decision_dates(calendar, anchor, end)
        start = next(d for d in grid if d >= start)
    if args.mode == "profile":
        end = days[calibration + 20]
    panel = panel.loc[: pd.Timestamp(end)]
    funding = funding.loc[: pd.Timestamp(end)]
    encoder = Encoder.fit(panel, funding, days[0], days[calibration - 1], cfg.portfolio.universe)
    encoder.market_clip = spec.get("market_clip")
    env = RebalanceEnv(
        panel,
        calendar,
        funding,
        cfg,
        information_lag_days=protocol.information_lag_days,
        decision_step_days=protocol.decision_step_days,
    )
    model = ActorCritic(len(encoder.names), spec["hidden"], actions=spec.get("actions")).to(device)
    if args.zero_critic:
        torch.nn.init.zeros_(model.critic[-1].weight)
        torch.nn.init.zeros_(model.critic[-1].bias)
    optimizer = torch.optim.Adam(model.parameters(), lr=spec["learning_rate"])
    identity = dict(
        spec=spec,
        protocol=protocol_fingerprint(cfg, protocol),
        data=market_digest(panel, funding),
        curve_tenors=list(panel.columns),
        commit=subprocess.check_output(["git", "rev-parse", "HEAD"], cwd=ROOT, text=True).strip(),
        config_sha256=hashlib.sha256(cfg.model_dump_json().encode()).hexdigest(),
        torch=str(torch.__version__),
        cuda=torch.version.cuda,
        device=args.device,
        gpu=torch.cuda.get_device_name() if device.type == "cuda" else None,
        python=platform.python_version(),
        numpy=np.__version__,
        pandas=pd.__version__,
        schema=encoder.names,
        mean=encoder.mean.tolist(),
        scale=encoder.scale.tolist(),
        episode=[str(start), str(end)],
    )
    digest = hashlib.sha256(json.dumps(identity, sort_keys=True).encode()).hexdigest()
    dump(
        out / "spec.json",
        dict(
            identity=identity,
            fingerprint=digest,
            parameters=sum(p.numel() for p in model.parameters()),
        ),
    )
    load_seconds = perf_counter() - load_start
    first = 0
    if args.checkpoint:
        first = load_boundary(args.checkpoint, digest, model, optimizer, device)
    limit = 1 if args.mode == "profile" else (args.stop_after or spec["updates"])
    if not 0 <= first < limit:
        raise ValueError("checkpoint 已达到或超出目标更新次数")
    boundary = args.checkpoint
    profile = args.mode == "profile"

    reference = None
    if spec.get("active_reward"):
        reference = collect(env, encoder, lambda _: Draw(0, 0, 0, 0), start, end)

    def run_episode():
        model.eval()
        # 固定回放的预设动作与网络参数无关，同时测量相同网络的推理路径。
        fixed = (
            iter(range(4))
            if args.action_grid
            else iter([math.atanh(z) for z in (0.0, 0.75, -0.75, 0.5)])
        )

        def draw(obs):
            return model.draw(obs, fixed_latent=next(fixed) if profile else None)

        episode = collect(env, encoder, draw, start, end)
        if "reward_scale" in spec:
            apply_training_reward(episode, reference, spec["reward_scale"])
        return episode

    for i in range(first + 1, limit + 1):
        episode = run_episode()
        started = perf_counter()
        model.train()
        metrics = {} if profile else update(model, optimizer, episode, spec)
        update_seconds = perf_counter() - started if not profile else 0.0
        tick = perf_counter()
        state = dict(
            fingerprint=digest,
            identity=identity,
            completed=i,
            model=model.state_dict(),
            optimizer=optimizer.state_dict(),
            rng=rng_state(device),
        )
        path = out / f"update-{i}.pt"
        if not profile:
            tmp = path.with_suffix(".tmp")
            torch.save(state, tmp)
            tmp.replace(path)
        trace = asdict(episode)
        for key in ("environment_seconds", "inference_seconds"):
            trace.pop(key)
        dump(out / f"trajectory-{i}.json", trace)
        io_seconds = perf_counter() - tick
        verified = False
        if args.verify_resume and boundary is not None:
            completed = load_boundary(boundary, digest, model, optimizer, device)
            if completed != i - 1:
                raise ValueError("恢复位置不匹配")
            replay = run_episode()
            replay_metrics = update(model, optimizer, replay, spec)
            saved = torch.load(path, map_location="cpu", weights_only=False)
            if not (
                episode.decisions == replay.decisions
                and episode.ledger == replay.ledger
                and metrics == replay_metrics
                and state_equal(model.state_dict(), saved["model"])
                and state_equal(optimizer.state_dict(), saved["optimizer"])
                and state_equal(rng_state(device), saved["rng"])
            ):
                raise ValueError("中断恢复与连续训练不一致")
            verified = True
        boundary = path
        rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
        summary = dict(
            update=i,
            **metrics,
            device=args.device,
            valuation_days=len(episode.ledger) + 1,
            actor_samples=len(episode.samples),
            episode_seconds=episode.environment_seconds + episode.inference_seconds,
            environment_seconds=episode.environment_seconds,
            inference_seconds=episode.inference_seconds,
            update_seconds=update_seconds,
            io_seconds=io_seconds,
            load_seconds=load_seconds,
            rss_bytes=rss if sys.platform == "darwin" else rss * 1024,
            peak_cuda_bytes=torch.cuda.max_memory_allocated(device) if device.type == "cuda" else 0,
            closure_error_yuan=episode.closure_error_yuan,
            terminal_fees_yuan=episode.terminal_fees,
            actual_trades=sum(len(row["executed_trades"]) for row in episode.ledger),
            decision_outcomes=dict(Counter(row["outcome"] for row in episode.decisions)),
            resume_verified=verified,
        )
        summary["decisions_per_second"] = summary["actor_samples"] / summary["episode_seconds"]
        summary["valuation_days_per_second"] = (
            summary["valuation_days"] / summary["episode_seconds"]
        )
        dump(out / f"metrics-{i}.json", summary)
        print(json.dumps(summary), flush=True)


if __name__ == "__main__":
    main()
