"""冻结模型选择、时钟、净费用和分片门槛的专项验证。"""

import importlib
import json
from datetime import date
from pathlib import Path

import numpy as np
import pytest


@pytest.fixture
def ev(monkeypatch):
    pytest.importorskip("torch")
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[1] / "scripts"))
    return importlib.import_module("evaluate_rl")


def test_frozen_folds_and_selection(ev):
    from run_ml_baseline import END, START, InformationClock, make_splits

    plan = json.loads(ev.EVAL_SPEC.read_text())
    market = ev.Market(END)
    splits, _ = make_splits(market.calendar, START, END, InformationClock(), "rolling")
    assert len(plan["folds"]) == len(splits) == 22
    for spec, split in zip(plan["folds"], splits, strict=True):
        for key, dates in (
            ("train", split.train_dates),
            ("validation", split.valid_dates),
            ("test", split.test_dates),
        ):
            assert spec[key] == [str(dates[0]), str(dates[-1])]
    rows = [
        dict(update=2, net_excess_vs_zero_bp=1 + 1e-10),
        dict(update=1, net_excess_vs_zero_bp=1),
    ]
    assert ev.winner(rows)["update"] == 1
    rows[0]["net_excess_vs_zero_bp"] = 1.001
    assert ev.winner(rows)["update"] == 2


def test_common_grid_and_net_nav(ev):
    market = ev.Market(date(2019, 3, 29))
    start = date(2019, 2, 1)
    days = market.calendar.trading_days_between(start, date(2019, 3, 29))
    end = days[16]  # 最后一个主动决策的执行日，T+1 费用尚未结算
    zero, _ = ev.replay(market, start, end, 1, lambda ctx, prev: 0.0)
    actions = iter([0.0, 0.0, 1.0, -1.0])
    active, error = ev.replay(market, start, end, 1, lambda ctx, prev: next(actions))
    assert list(active.proposal_z.dropna().index) == list(days[:16:5])
    assert error < 1e-5
    np.testing.assert_allclose(active.net_nav.iloc[:11], zero.net_nav.iloc[:11], atol=1e-7, rtol=0)
    assert active.nav.iloc[-1] > active.net_nav.iloc[-1]
    row = ev.score(active, zero, days[10], end, market.calendar, 250)
    assert row["nonzero_proposals"] == 2 and row["trades"] > 0
    relative = (
        active.net_nav.iloc[-1] / active.net_nav.iloc[9]
        - zero.net_nav.iloc[-1] / zero.net_nav.iloc[9]
    ) * 1e4
    assert row["net_excess_vs_zero_bp"] == pytest.approx(relative)
    assert row["te_vs_zero_bp"] == pytest.approx(
        (active.net_nav.iloc[9:].pct_change() - zero.net_nav.iloc[9:].pct_change()).std(ddof=0)
        * 250**0.5
        * 1e4
    )
    assert row["net_cost_yuan"] == pytest.approx(active.cost.loc[days[10] : end].sum())


def test_matrix_rejects_invalid_gate(ev, tmp_path, monkeypatch):
    monkeypatch.syspath_prepend(str(Path(ev.ROOT) / "scripts/server"))
    from ppo_matrix import check_gate

    plan = json.loads(ev.EVAL_SPEC.read_text())
    (tmp_path / "gate").mkdir()
    (tmp_path / "gate/test.json").write_text('{"plan_sha256":"wrong","metrics":[]}')
    folder = tmp_path / f"seed-{plan['seeds'][0]}/fold-0"
    folder.mkdir(parents=True)
    (folder / "selection.json").write_text("{}")
    with pytest.raises(ValueError, match="第 0 折"):
        check_gate(tmp_path, plan)


def test_budget_pilot_fixed_pairs_and_reference(ev):
    from run_ppo_budget_pilot import verify_reference
    from train_rl import PILOT_SPEC, training_spec

    pilot = json.loads(PILOT_SPEC.read_text())
    assert pilot["folds"] == [0, 10, 21] and pilot["updates"] == [2, 4, 8]
    for fold in pilot["folds"]:
        for seed in pilot["seeds"]:
            spec = training_spec(fold, seed, budget_pilot=True)
            original = training_spec(fold, seed)
            assert spec == dict(original, updates=8, budget_protocol=pilot["version"])
            assert original["updates"] == 2 and spec["validation"][1] < spec["test"][0]
    with pytest.raises(ValueError):
        training_spec(1, pilot["seeds"][0], budget_pilot=True)
    row = dict(
        update=2, net_excess_vs_zero_bp=-1, te_vs_zero_bp=1, turnover=1, trades=2, net_cost_yuan=12
    )
    ref = dict(
        fold=fold,
        seed=seed,
        candidates=[row],
        validation=spec["validation"],
        plan_sha256=ev.sha(ev.EVAL_SPEC),
    )
    verify_reference(ref, fold, seed, row, spec)
    with pytest.raises(ValueError, match="reproduce"):
        verify_reference(ref, fold, seed, dict(row, trades=3), spec)


def test_execution_trace_hold_and_date_alignment(ev):
    from audit_ppo_execution import compare, decision_ledger

    end = date(2019, 3, 8)
    market = ev.Market(end)
    start = date(2019, 2, 1)
    rows = {}

    def trace(rollout, proposals, book):
        rows.update(decision_ledger(rollout, proposals, book, start))

    book, _ = ev.replay(market, start, end, 2, lambda ctx, prev: None, audit=trace)
    paired = compare(rows, rows)
    assert paired["pnl_vs_zero_yuan"] == 0
    assert paired["same_faces_days"] == len(book) - 1
    assert paired["decision_count"] == int(book.decision.sum()) > 0
    assert all(r["target_krd"] is None for r in rows.values() if r["decision"])
    assert sum(r["net_pnl_yuan"] for r in rows.values()) == pytest.approx(
        book.net_nav.iloc[-1] - book.net_nav.iloc[0]
    )


def test_critic_budget_requires_stable_profit_and_policy(ev):
    from run_critic_budget import plateau
    from train_rl import CRITIC_BUDGET, training_spec

    design = json.loads(CRITIC_BUDGET.read_text())
    spec = training_spec(
        0, 20261111, zero_critic=True, grid_active=True, action_grid=True, critic_budget=True
    )
    assert spec["updates"] == 16
    with pytest.raises(ValueError):
        training_spec(0, 20261111, action_grid=True, critic_budget=True)
    curve = [
        dict(train_active_bp=x, mean_action_tv=y) for x, y in [(8, None), (8.8, 0.02), (8.4, 0.01)]
    ]
    assert not plateau(curve[:2], design)
    assert plateau(curve, design)
    assert not plateau([*curve[:-1], dict(train_active_bp=10, mean_action_tv=0.01)], design)
    assert not plateau([*curve[:-1], dict(train_active_bp=8, mean_action_tv=0.1)], design)


def test_actor_step_only_changes_actor_adam_rate(ev):
    import copy

    import torch
    from train_rl import make_optimizer, training_spec

    from ficc.rl.ppo import ActorCritic

    flags = dict(
        action_grid=True,
        grid_active=True,
        zero_critic=True,
        critic_budget=True,
        invested_start=True,
    )
    original = training_spec(0, 20261111, **flags)
    new = training_spec(0, 20261111, actor_step=True, **flags)
    assert {
        k: v for k, v in new.items() if k not in ("actor_learning_rate", "actor_step_sha256")
    } == original
    with pytest.raises(ValueError, match="invested"):
        training_spec(0, 20261111, actor_step=True)
    base = ActorCritic(4, 8, actions=original["actions"])
    a, b = copy.deepcopy(base), copy.deepcopy(base)
    for model, spec in [(a, original), (b, new)]:
        optim = make_optimizer(model, spec)
        for param in model.parameters():
            param.grad = torch.ones_like(param)
        optim.step()
    for x, y in zip(a.critic.parameters(), b.critic.parameters(), strict=True):
        torch.testing.assert_close(x, y, rtol=0, atol=0)
    for x, y, z in zip(
        a.actor.parameters(), b.actor.parameters(), base.actor.parameters(), strict=True
    ):
        torch.testing.assert_close((x - z) / 4, y - z, rtol=0, atol=4e-8)


def test_neutral_actor_starts_at_benchmark_and_preserves_exploration(ev):
    import torch
    from audit_ppo_initial_policy import recover_initial
    from train_rl import initialize_neutral_actor, training_spec

    flags = dict(
        action_grid=True,
        grid_active=True,
        zero_critic=True,
        critic_budget=True,
        invested_start=True,
        actor_step=True,
    )
    spec = training_spec(0, 20261111, neutral_actor=True, **flags)
    torch.manual_seed(spec["seed"])
    model = ev.ActorCritic(3, spec["hidden"], actions=spec["actions"])
    obs = [[0, 1, 0], [1, -3, 0.25]]
    first = []
    for x in obs:
        d = model.draw(x)
        first.append(dict(obs=x, actor_sample=True, latent=d.latent, z=d.z, log_prob=d.log_prob))
    original, error = recover_initial(spec, first)
    assert error < 2e-6 and torch.equal(original.actor[-1].weight, model.actor[-1].weight)
    first[0]["log_prob"] += 0.1
    with pytest.raises(ValueError, match="initial policy"):
        recover_initial(spec, first)
    with pytest.raises(ValueError, match="slow actor"):
        training_spec(0, 20261111, neutral_actor=True)
    original = {k: v.clone() for k, v in model.state_dict().items()}
    initialize_neutral_actor(model, spec)
    x = torch.tensor(obs, dtype=torch.float32)
    assert [model.deterministic(v) for v in x] == [0.0, 0.0]
    assert torch.all((model.distribution(x).probs - 0.25).abs() < 0.001)
    assert {k for k, v in model.state_dict().items() if not torch.equal(v, original[k])} == {
        "actor.4.weight",
        "actor.4.bias",
    }


def test_action_value_labels_use_matched_opening_and_future_only_as_target(ev):
    from run_action_value_probe import active_bp, probe_labels, training_split

    market = ev.Market(date(2019, 5, 20))
    anchor = date(2019, 2, 1)
    grid = market.calendar.trading_days_between(anchor, date(2019, 5, 20))[::5]
    intervals, inner, end = training_split(grid)
    assert intervals[-1][1] < inner and end == grid[-1]
    assert all(grid.index(b) == grid.index(a) + 1 for a, b in intervals)
    day, horizon = intervals[-1]
    zero, _ = ev.replay(market, anchor, end, 2, lambda ctx, prev: 0.0)
    shorter, _ = ev.replay(market, anchor, horizon, 2, lambda ctx, prev: 0.0)
    assert active_bp(shorter, zero, day, horizon) == pytest.approx(0, abs=1e-8)
    labels = list(
        probe_labels(
            market, zero, anchor, day, horizon, {"label_band": 2, "actions": [0.0, None, -1.0, 1.0]}
        )
    )
    assert len(labels) == 3 and np.isfinite(labels).all()
    book, _ = ev.replay(market, anchor, end, 2, lambda ctx, prev: -1.0 if ctx.as_of == day else 0.0)
    assert labels[1] == pytest.approx(active_bp(book, zero, day, horizon), abs=1e-8)
    bad = zero.copy()
    bad.at[day, "net_nav"] += 1.0
    with pytest.raises(ValueError, match="opening"):
        active_bp(book, bad, day, horizon)


def test_action_value_fit_and_sequential_holdout(ev, tmp_path, monkeypatch):
    import run_action_value_probe as probe

    plan = {"anchor": "2019-02-01", "folds": [{"train": ["2019-02-01", "2019-05-20"]}]}
    plan_path = tmp_path / "windows.json"
    plan_path.write_text(json.dumps(plan))
    monkeypatch.setattr(probe, "EVAL_SPEC", plan_path)
    cfg = json.loads(probe.DESIGN.read_text())
    cfg["folds"] = [0]
    path = tmp_path / "design.json"
    path.write_text(json.dumps(cfg))
    monkeypatch.setattr(probe, "DESIGN", path)
    probe.run(tmp_path, 0)
    result = json.loads((tmp_path / "fold-0/result.json").read_text())
    labels = json.loads((tmp_path / "fold-0/fit_labels.json").read_text())
    assert all(r["cutoff"] < r["signal"] < r["horizon"] < result["inner_start"] for r in labels)
    assert result["fit_intervals"] == len(labels) and result["end"] <= plan["folds"][0]["train"][1]
    assert all(r["net_bp"][0] == 0.0 and len(r["net_bp"]) == 4 for r in labels)
    assert {r["band"] for r in result["rows"]} == {1, 2}
    assert type(probe.summarize(tmp_path)["consider_development_replay"]) is bool
    import audit_action_values as audit

    monkeypatch.setattr(audit, "DESIGN", path)
    out = tmp_path / "audit"
    audit.continuous(out, tmp_path, 0, 2)
    for i in (0, 1):
        audit.one_step(out, 0, 2, i, 2)
    audit.join(out, tmp_path, 0, 2)
    ref = json.loads((out / "fold-0/band-2/audit.json").read_text())
    assert len(ref["decisions"]) > 1
    for row in ref["decisions"]:
        assert row["selected_bp"] == row["actual_bp"][row["selected_index"]]
    assert ref["comparisons"]["zero"]["net_difference_bp"] == pytest.approx(
        result["rows"][0]["selector"]["net_bp"]
    )
    labels[0]["features"][0] += 50
    (tmp_path / "fold-0/fit_labels.json").write_text(json.dumps(labels))
    with pytest.raises(ValueError, match="identity"):
        audit.join(out, tmp_path, 0, 2)
