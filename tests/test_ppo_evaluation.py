"""冻结模型选择、时钟、净费用和分片门槛的专项验证。"""

import importlib
import json
from datetime import date
from itertools import pairwise
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
    import audit_action_switches as switches

    monkeypatch.setattr(switches, "DESIGN", path)
    switches.run(out, tmp_path / "switches", 0, 2)
    result = json.loads((tmp_path / "switches/fold-0/band-2/switches-0.json").read_text())
    schedule = [0, *[r["selected_index"] for r in ref["decisions"]]]
    assert (
        result["total_switches"]
        == len(result["rows"])
        == sum(a != b for a, b in pairwise(schedule))
    )
    assert all(np.isfinite(r["selected_minus_hold_bp"]) for r in result["rows"])
    labels[0]["features"][0] += 50
    (tmp_path / "fold-0/fit_labels.json").write_text(json.dumps(labels))
    with pytest.raises(ValueError, match="identity"):
        audit.join(out, tmp_path, 0, 2)


def test_confirmation_uses_only_current_and_previous_suggestions(ev):
    from probe_action_confirmation import confirmed_targets

    original = [3, 0, 3, 3, 0, 1, 1, 2, 2]
    expected = [0, 0, 0, 3, 3, 3, 1, 1, 2]
    assert list(confirmed_targets(original, 0)) == expected
    assert list(confirmed_targets(original[:4], 0)) == expected[:4]


def test_inventory_value_forks_and_forward_gate(ev, tmp_path, monkeypatch):
    import run_action_value_probe as probe
    import run_inventory_value as inventory
    from inventory_forward import digest_lines, scheduled_window

    from ficc.utils.calendar import TradingCalendar

    plan_path = tmp_path / "plan.json"
    plan_path.write_text(
        json.dumps({"anchor": "2019-02-01", "folds": [{"train": ["2019-02-01", "2019-04-30"]}]})
    )
    cfg = json.loads(inventory.DESIGN.read_text())
    cfg.update(
        folds=[0], confirmation_fold=0, confirmation_start="2019-05-01", confirmation_intervals=2
    )
    path = tmp_path / "design.json"
    path.write_text(json.dumps(cfg))
    monkeypatch.setattr(probe, "EVAL_SPEC", plan_path)
    monkeypatch.setattr(inventory, "EVAL_SPEC", plan_path)
    monkeypatch.setattr(inventory, "DESIGN", path)
    for shard in (0, 1):
        inventory.labels(tmp_path, 0, shard, 2)
    rows = [json.loads(p.read_text()) for p in sorted((tmp_path / "fold-0/labels").glob("*.json"))]
    assert all(r["net_bp"][0] == r["delta_cost_yuan"][0] == 0 for r in rows)
    assert all(r["cutoff"] < r["signal"] < r["horizon"] for r in rows)
    assert rows[0]["x"] == rows[1]["x"] and rows[0]["net_bp"] == rows[1]["net_bp"]
    assert any(a["x"] != b["x"] for a, b in zip(rows[::2], rows[1::2], strict=True))
    inventory.fit(tmp_path, 0)
    model = json.loads((tmp_path / "fold-0/model.json").read_text())
    inner = json.loads((tmp_path / "fold-0/inner.json").read_text())
    assert model["fit_rows"] == len(rows) == 2 * model["fit_intervals"]
    assert max(r["horizon"] for r in rows) < inner["start"]
    assert (
        len(model["market"][0]["coef"]) < len(model["inventory"][0]["coef"]) == len(model["names"])
    )
    assert set(inventory.gates(tmp_path)["median_bp"][2]) == {"zero", "hold", "fixed", "market"}
    monkeypatch.setattr(inventory, "gates", lambda root: {"eligible": False})
    with pytest.raises(ValueError, match="screen failed"):
        inventory.confirm(tmp_path)
    monkeypatch.setattr(inventory, "gates", lambda root: {"eligible": True})
    anchor, cutoff = date(2019, 2, 1), date(2019, 4, 30)
    full = ev.Market(date(2019, 6, 1))
    history = full.calendar.trading_days_between(anchor, cutoff)
    # Static synthetic source, independent of every damaged input below; includes a makeup Sunday.
    suffix = [
        f"2019-05-{d:02d}"
        for d in (5, 6, 7, 8, 9, 10, 13, 14, 15, 16, 17, 20, 21, 22, 23, 24, 27, 28, 29, 30, 31)
    ]
    plan = dict(
        anchor=str(anchor),
        history_end=str(cutoff),
        verified_through=suffix[-1],
        history_sessions=len(history),
        history_sha256=digest_lines(history),
        sessions=suffix,
    )
    calendar_file = tmp_path / "calendar.json"
    seal_file = tmp_path / "seal.json"
    calendar_file.write_text(json.dumps(plan))
    seal = dict(
        calendar_sha256=ev.sha(calendar_file),
        design_sha256=ev.sha(path),
        model_sha256=inner["model_sha256"],
        fixed_action=model["fixed_action"],
        inner_sha256={"0": ev.sha(tmp_path / "fold-0/inner.json")},
        runtime_sha256=inventory.runtime_digest(inventory.ROOT),
    )
    seal_file.write_text(json.dumps(seal))
    monkeypatch.setattr(inventory, "CALENDAR", calendar_file)
    monkeypatch.setattr(inventory, "SEAL", seal_file)
    start, end, dates = scheduled_window(plan, cfg, anchor)
    with pytest.raises(ValueError, match="verified calendar"):
        scheduled_window({**plan, "sessions": suffix[:5]}, cfg, anchor)
    # The would-be extra session remains available even after deleting a full curve.
    for omitted in (dates[dates.index(start) + 1], end):
        damaged = ev.Market(dates[-1])
        damaged.panel = damaged.panel.drop(ev.pd.Timestamp(omitted))
        damaged.calendar = TradingCalendar.from_dates(damaged.panel.index)
        monkeypatch.setattr(inventory, "Market", lambda through, m=damaged: m)
        with pytest.raises(ValueError, match=str(omitted)):
            inventory.confirm(tmp_path)
        assert not (tmp_path / "confirmation.json").exists()
    damaged = ev.Market(end)
    damaged.funding = damaged.funding.drop(ev.pd.Timestamp(start))
    monkeypatch.setattr(inventory, "Market", lambda through: damaged)
    with pytest.raises(ValueError, match="funding observations incomplete"):
        inventory.confirm(tmp_path)
    monkeypatch.setattr(inventory, "Market", ev.Market)
    seal_file.write_text(json.dumps({**seal, "model_sha256": "changed"}))
    with pytest.raises(ValueError, match="artifacts differ"):
        inventory.confirm(tmp_path)
    seal_file.write_text(json.dumps(seal))
    inventory.confirm(tmp_path)
    confirmation = json.loads((tmp_path / "confirmation.json").read_text())
    assert [confirmation[k] for k in ("start", "end")] == [str(start), str(end)]
    assert confirmation["start"] > inner["end"] and len(confirmation["bands"]) == 2
    with pytest.raises(ValueError, match="already recorded"):
        inventory.confirm(tmp_path)


def test_coverage_dedup_and_common_optimum(ev):
    from run_inventory_coverage import date_summary

    def row(faces, cash, labels):
        return dict(
            state=dict(faces=faces, lagged_cash=cash, lagged_nav=100.0),
            opening_net_nav=100.0,
            net_bp=labels,
        )

    a = row([["bond-a", 100]], 0, [0, 2, 2, 0])
    b = row([["bond-b", 100]], 0, [0, 2, 0, 0])
    result = date_summary([a, a, b], 1e-8)
    assert result["distinct_faces"] == result["distinct_states"] == 2
    assert not result["no_common_best"] and result["hindsight_gain_bp"] == 0
    c = row([["bond-a", 100]], 1, [0, 0, 4, 0])
    result = date_summary([b, c], 1e-8)
    assert result["no_common_best"] and result["hindsight_gain_bp"] == 1
    assert date_summary([a, c], 1e-8)["distinct_states"] == 2
    with pytest.raises(ValueError, match="inconsistent"):
        date_summary([a, row([["bond-a", 100]], 0, [0, 3, 2, 0])], 1e-8)


def test_coverage_paths_reuse_matched_ledger(ev, tmp_path, monkeypatch):
    import run_inventory_coverage as coverage
    import run_inventory_value as inventory

    plan = coverage.read(coverage.PLAN)
    plan.update(folds=[0], bands=[2])
    plan_path = tmp_path / "coverage-plan.json"
    plan_path.write_text(json.dumps(plan))
    monkeypatch.setattr(coverage, "PLAN", plan_path)
    cfg, market, anchor, intervals, start, end, enc = inventory.window(0)
    intervals = intervals[:2]
    monkeypatch.setattr(
        coverage, "window", lambda fold: (cfg, market, anchor, intervals, start, end, enc)
    )
    for shard in (0, 1):
        coverage.collect(tmp_path, 0, 2, shard, 2)
    rows = [json.loads(p.read_text()) for p in (tmp_path / "fold-0/band-2").glob("*.json")]
    assert len(rows) == 12
    first = [r for r in rows if r["signal"] == str(intervals[0][0])]
    assert coverage.date_summary(first, 1e-8)["distinct_states"] == 1
    old = inventory.make_label(
        market,
        anchor,
        *intervals[1],
        {d: cfg["prefix_paths"][0][j] for j, (d, _) in enumerate(intervals)},
        enc,
        cfg,
    )
    saved = next(r for r in rows if r["path"] == 0 and r["signal"] == str(intervals[1][0]))
    assert saved["net_bp"] == old["net_bp"]
    coverage.collect(tmp_path, 0, 2, 0, 2)
    coverage.summarize(tmp_path)
    summary = json.loads((tmp_path / "summary.json").read_text())
    assert summary["results"][0]["intervals"] == 2
    assert summary["results"][0]["dates"][0]["distinct_states"] == 1
    next((tmp_path / "fold-0/band-2").glob("*.json")).unlink()
    with pytest.raises(FileNotFoundError):
        coverage.summarize(tmp_path)


def test_lambda_one_changes_only_target_and_preserves_first_rollout(ev, tmp_path, monkeypatch):
    import torch
    import train_rl as cli

    from ficc.rl.ppo import advantages

    flags = dict(
        action_grid=True,
        grid_active=True,
        zero_critic=True,
        critic_budget=True,
        invested_start=True,
        actor_step=True,
        neutral_actor=True,
    )
    old = cli.training_spec(0, 20261111, **flags)
    new = cli.training_spec(0, 20261111, lambda_one=True, **flags)
    assert {k for k in new if new[k] != old.get(k)} == {"gae_lambda", "lambda_sha256"}
    assert new["gae_lambda"] == 1
    with pytest.raises(ValueError, match="registered neutral"):
        cli.training_spec(0, 20261111, lambda_one=True)
    rewards = np.array([0.01, -0.02, 0.005, -0.001])
    values = np.array([0.3, -0.7, 0.2, 0.4])
    target = values + advantages(rewards, values, gae_lambda=1, terminated=True)
    np.testing.assert_allclose(target, rewards[::-1].cumsum()[::-1], atol=1e-15, rtol=0)

    plan = json.loads(cli.EVAL_SPEC.read_text())
    plan["folds"][0]["train"][1] = "2019-04-02"
    path = tmp_path / "folds.json"
    path.write_text(json.dumps(plan))
    monkeypatch.setattr(cli, "EVAL_SPEC", path)
    options = [
        "--action-grid",
        "--grid-active",
        "--zero-critic",
        "--critic-budget",
        "--invested-start",
        "--actor-step",
        "--neutral-actor",
        "--fold",
        "0",
        "--seed",
        "20261111",
        "--device",
        "cpu",
        "--stop-after",
        "2",
    ]
    cli.main(["train", "--output", str(tmp_path / "control"), *options])
    cli.main(
        [
            "train",
            "--output",
            str(tmp_path / "candidate"),
            *options,
            "--lambda-one",
            "--paired-reference",
            str(tmp_path / "control"),
            "--verify-resume",
        ]
    )
    assert (tmp_path / "control/trajectory-1.json").read_bytes() == (
        tmp_path / "candidate/trajectory-1.json"
    ).read_bytes()
    metrics = json.loads((tmp_path / "candidate/metrics-2.json").read_text())
    assert metrics["resume_verified"] and metrics["closure_error_yuan"] < 1e-5
    candidate = tmp_path / "candidate/update-2.pt"
    spec = cli.training_spec(0, 20261111, lambda_one=True, **flags)
    market = ev.Market(date(2019, 4, 2))
    ev.load_policy(candidate, spec, 2, market, market.cfg)
    with pytest.raises(ValueError, match="checkpoint"):
        ev.load_policy(candidate, cli.training_spec(0, 20261111, **flags), 2, market, market.cfg)
    saved = torch.load(candidate, map_location="cpu", weights_only=False)
    assert saved["identity"]["spec"]["gae_lambda"] == 1.0
    reference = tmp_path / "control"
    original = (reference / "spec.json").read_text()
    mismatched = json.loads(original)
    mismatched["identity"]["data"] = "different-data"
    (reference / "spec.json").write_text(json.dumps(mismatched))
    with pytest.raises(ValueError, match="pre-update identity"):
        cli.main(
            [
                "train",
                "--output",
                str(tmp_path / "bad-data"),
                *options,
                "--lambda-one",
                "--paired-reference",
                str(reference),
            ]
        )
    assert not list((tmp_path / "bad-data").glob("update-*.pt"))
    (reference / "spec.json").write_text(original)
    (reference / "trajectory-1.json").write_text("{}")
    with pytest.raises(ValueError, match="pre-update trajectory"):
        cli.main(
            [
                "train",
                "--output",
                str(tmp_path / "bad-trace"),
                *options,
                "--lambda-one",
                "--paired-reference",
                str(reference),
            ]
        )
    assert not list((tmp_path / "bad-trace").glob("update-*.pt"))
