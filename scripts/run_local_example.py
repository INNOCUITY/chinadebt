#!/usr/bin/env python3
"""Mac 本地 example（不连 A800）：带预热区的短 episode + 极小 Ridge 训练/保存/恢复。

只验证**工程链路**：RL rollout 轨迹、协议网格、ML 数据集、模型文件保存与恢复；
不做模型研究、不报告 alpha。示例窗口自带预热区（panel 起点 2019-01-28，示例 2019-05-06 起）。
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
for _p in (_ROOT / "src", _ROOT / "scripts"):
    if str(_p) not in sys.path:
        sys.path.insert(0, str(_p))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from run_ml_baseline import fit_ridge  # noqa: E402
from run_strategies import load_market  # noqa: E402

from ficc.config import get_config  # noqa: E402
from ficc.curve.curve import Curve  # noqa: E402
from ficc.data.schema import CANONICAL_TENORS  # noqa: E402
from ficc.ml.asof import FeatureSpec  # noqa: E402
from ficc.ml.dataset import (  # noqa: E402
    InformationClock,
    attach_funding_column,
    build_supervised_dataset,
)
from ficc.portfolio.benchmark import (  # noqa: E402
    build_benchmark,
    build_universe_bonds,
)
from ficc.research.protocol import ExperimentProtocol, protocol_config  # noqa: E402
from ficc.rl import ActionSpec, RebalanceEnv  # noqa: E402

EXAMPLE_START = date(2019, 5, 6)  # 自带 >60 个交易日预热（panel 从 2019-01-28 起）
EXAMPLE_END = date(2019, 6, 28)
FUNDING_COLUMN = "_fdr007"


def _simple_features() -> list[FeatureSpec]:
    return [
        FeatureSpec(name="level_5y", transform=lambda f: f["5.0"], inputs=("5.0",)),
        FeatureSpec(
            name="slope_2s10s",
            transform=lambda f: f["10.0"] - f["2.0"],
            inputs=("2.0", "10.0"),
        ),
    ]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Mac 本地 example")
    parser.add_argument("--start", type=date.fromisoformat, default=EXAMPLE_START)
    parser.add_argument("--end", type=date.fromisoformat, default=EXAMPLE_END)
    parser.add_argument("--out-dir", type=Path, default=None)
    args = parser.parse_args(argv)

    cfg = get_config()
    panel, funding, calendar = load_market(cfg)
    protocol = ExperimentProtocol()
    aligned = protocol_config(cfg, protocol)
    out = (
        Path(args.out_dir)
        if args.out_dir is not None
        else cfg.paths.abs("reports") / "local_example"
    )
    out.mkdir(parents=True, exist_ok=True)

    # 1) RL rollout：带预热区的短 episode；策略固定持仓面值（仅链路验证）
    env = RebalanceEnv(
        panel,
        calendar,
        funding,
        aligned,
        information_lag_days=1,
        decision_step_days=protocol.decision_step_days,
        warmup_days=5,
    )
    env.reset(args.start, args.end)
    # 用真实基准身份的一次性面值目标（不依赖 lagged 重发）
    curve = Curve(
        as_of=args.start,
        tenors=np.asarray(sorted(CANONICAL_TENORS), dtype=float),
        rates=panel.loc[pd.Timestamp(args.start)]
        .reindex(list(CANONICAL_TENORS))
        .to_numpy(dtype=float),
        method="linear",
    )
    bonds = build_universe_bonds(curve, args.start, cfg=aligned)
    bench = build_benchmark(curve, args.start, bonds=bonds, calendar=calendar, cfg=aligned)
    target_faces = {code: float(face) for code, face in bench.faces.items()}

    def policy(ctx):
        return ActionSpec(action_type="target_faces", target_faces=dict(target_faces))

    rollout = env.rollout(policy, args.start, args.end, name="local_example")
    traj_path = out / "trajectory.npz"
    np.savez_compressed(
        traj_path,
        nav=rollout.result.nav.to_numpy(),
        nav_dates=np.array([str(d) for d in rollout.result.nav.index]),
        n_trades=len(rollout.result.trades),
        terminal_nav=rollout.terminal.nav_mark_to_market,
        terminal_unsettled_cash=rollout.terminal.unsettled_cash,
    )
    loaded = np.load(traj_path)
    assert np.allclose(loaded["nav"], rollout.result.nav.to_numpy())
    print(
        f"[example] rollout NAV 末值={float(rollout.result.nav.iloc[-1]):,.2f}，"
        f"trades={len(rollout.result.trades)}，轨迹已保存 {traj_path}"
    )

    # 2) ML 数据集 + 极小 Ridge 训练、保存、恢复（只验证链路）
    clock = InformationClock(observation_lag_days=1, holding_days=5, label_lag_days=1)
    decisions = protocol.decision_dates(calendar, args.start, args.end)
    feature_panel = attach_funding_column(panel, funding, FUNDING_COLUMN)
    dataset = build_supervised_dataset(
        feature_panel, funding, calendar, decisions, _simple_features(), aligned, clock
    )
    feature_names = [str(n) for n in dataset.attrs["feature_names"]]
    x = dataset[feature_names].to_numpy(dtype=float)
    y = dataset["label"].to_numpy(dtype=float)
    model = fit_ridge(x, y, alpha=100.0)
    model_path = out / "local_ridge.npz"
    np.savez_compressed(
        model_path,
        coef=model.coef,
        mean=model.mean,
        scale=model.scale,
        intercept=np.array([model.intercept]),
        feature_names=np.array(feature_names),
    )
    loaded_model = np.load(model_path)
    pred = ((x - loaded_model["mean"]) / loaded_model["scale"]) @ loaded_model["coef"] + float(
        loaded_model["intercept"][0]
    )
    assert np.allclose(pred, model.predict(x))
    (out / "example_manifest.json").write_text(
        json.dumps(
            {
                "start": str(args.start),
                "end": str(args.end),
                "protocol": protocol.version,
                "n_ml_rows": len(dataset),
                "features": feature_names,
                "note": "本地 example：链路验证，不是模型研究/alpha 结论",
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"[example] ML 行数={len(dataset)}，Ridge 训练完成并恢复验证；产物目录 {out}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
