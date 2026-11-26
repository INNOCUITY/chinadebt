"""PPO 专项验收：信息边界、概率、净奖励与真实更新边界恢复。"""

import importlib
import json
from datetime import date

import numpy as np
import pandas as pd
import pytest

from ficc.data.loader import load_curve_long, load_funding_series
from ficc.data.schema import CANONICAL_TENORS
from ficc.research.protocol import protocol_config
from ficc.rl.collector import Draw, Encoder, collect
from ficc.rl.env import RebalanceEnv
from ficc.utils.calendar import TradingCalendar


@pytest.fixture(scope="module")
def market():
    cfg = protocol_config()
    frame = load_curve_long(cfg=cfg)
    panel = frame.pivot(index="date", columns="tenor", values="ytm_pct").reindex(
        columns=CANONICAL_TENORS
    )
    funding = load_funding_series(cfg=cfg).set_index("date")["FDR007"]
    return cfg, panel, funding, TradingCalendar.from_dates(panel.index)


def test_collector_clock_terminal_and_future_perturbation(market):
    cfg, panel, funding, calendar = market
    dates = calendar.trading_days_between(date(2019, 2, 1), date(2019, 4, 1))
    start, end = dates[20], dates[31]

    def replay(p):
        encoder = Encoder.fit(p, funding, dates[0], dates[19], cfg.portfolio.universe)
        env = RebalanceEnv(p, calendar, funding, cfg, decision_step_days=5)
        return collect(env, encoder, lambda _obs: Draw(0.5, float(np.tanh(0.5)), -1, 0), start, end)

    ep = replay(panel)
    assert [r["date"] for r in ep.samples] == list(dates[20:31:5])
    assert [r["available_through"] for r in ep.samples] == list(dates[19:30:5])
    expected = sum(r["reward"] for r in ep.samples) * ep.initial_nav + ep.initial_nav
    assert expected == pytest.approx(ep.net_terminal, abs=1e-6, rel=0)
    for row in ep.samples:
        owned = [r for r in ep.ledger if r["reward_owner"] == row["date"]]
        assert row["reward"] * ep.initial_nav == pytest.approx(
            sum(r["reward_yuan"] - r["terminal_fee_yuan"] for r in owned), abs=1e-6
        )
    assert sum(r["terminal_fee_yuan"] for r in ep.ledger) == ep.terminal_fees
    assert all(r["terminal_fee_yuan"] == 0 for r in ep.ledger[:-1])
    short = collect(
        RebalanceEnv(panel, calendar, funding, cfg, decision_step_days=5),
        Encoder.fit(panel, funding, dates[0], dates[19], cfg.portfolio.universe),
        lambda obs: Draw(0.0, 0.0, -1.0, 0.0),
        start,
        dates[21],
    )
    assert short.terminal_fees > 0
    assert short.samples[0]["reward"] * short.initial_nav == pytest.approx(
        short.ledger[0]["reward_yuan"] - short.terminal_fees, abs=1e-7
    )
    perturbed = panel.copy()
    perturbed.loc[pd.Timestamp(dates[25]) :] += 0.1
    other = replay(perturbed)
    for a, b in zip(ep.samples[:2], other.samples[:2], strict=True):
        assert a["obs"] == b["obs"]
        assert a["target_krd"] == b["target_krd"]


def test_gae_and_bounded_density():
    torch = pytest.importorskip("torch")
    from ficc.rl.ppo import ActorCritic, advantages

    r = [1.0, 2.0]
    v = [0.2, 0.3]
    for terminal, tail, expected in [(True, None, [2.8, 1.7]), (False, 0.4, [3.2, 2.1])]:
        assert advantages(
            r, v, gae_lambda=1, terminated=terminal, next_value=tail
        ) == pytest.approx(expected)
    with pytest.raises(ValueError):
        advantages(r, v, gae_lambda=1, terminated=False)
    latent = torch.tensor([-2.1, 0.0, 1.2])
    dist = torch.distributions.Normal(0.0, 1.0)
    bounded = torch.distributions.TransformedDistribution(
        dist, [torch.distributions.TanhTransform()]
    )
    torch.testing.assert_close(ActorCritic.log_prob(dist, latent), bounded.log_prob(latent.tanh()))


def test_cli_resume_and_audit(tmp_path, monkeypatch):
    torch = pytest.importorskip("torch")
    from pathlib import Path

    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    cli = importlib.import_module("train_rl")
    spec = json.loads(cli.SPEC.read_text())
    spec["train"][1] = "2019-04-02"
    short = tmp_path / "short.json"
    short.write_text(json.dumps(spec))
    monkeypatch.setattr(cli, "SPEC", short)

    def run(mode, dest, *opts):
        cli.main([mode, "--device", "cpu", "--output", str(tmp_path / dest), *map(str, opts)])

    run("train", "continuous", "--verify-resume")
    run("train", "first", "--stop-after", "1")
    run("resume", "resumed", "--checkpoint", tmp_path / "first/update-1.pt")
    results = [
        torch.load(tmp_path / d / "update-2.pt", weights_only=False)
        for d in ("continuous", "resumed")
    ]
    assert cli.state_equal(*results)
    assert (tmp_path / "continuous/trajectory-2.json").read_bytes() == (
        tmp_path / "resumed/trajectory-2.json"
    ).read_bytes()
    m = json.loads((tmp_path / "continuous/metrics-2.json").read_text())
    assert m["resume_verified"] and m["max_parameter_change"] > 0 and m["actual_trades"] > 0
    assert abs(m["closure_error_yuan"]) < 1e-5
    spec["seed"] += 1
    short.write_text(json.dumps(spec))
    with pytest.raises(ValueError, match="checkpoint"):
        run("resume", "mismatch", "--checkpoint", tmp_path / "first/update-1.pt")
