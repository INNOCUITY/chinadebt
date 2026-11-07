"""成本对照实验脚本（``scripts/run_cost_study.py``）的验收测试。

覆盖
----
1. ``--quick`` 能离线跑通，产出三张列名稳定的 CSV；
2. 固定路径分解可加性：关掉价差 / 全部关掉的 NAV 改善 ≈ 对应成本（元）；
3. 决策变化实验给出引擎重跑结果（含换手/成交笔数/违规天数）；
4. 参数敏感性包含 lot/死区/频率/价差四组变体；
5. 基准账本按同一开关重放的 NAV == 引擎 ``benchmark_nav``（固定路径分解可信）；
6. 回放==引擎的验收守卫会在不一致时抛错（不静默通过）。
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from ficc.backtest.engine import run_backtest
from ficc.backtest.replay import CostSwitches, ReplayResult, replay_engine_result
from ficc.config import get_config
from ficc.data.loader import load_curve_long, load_funding_series
from ficc.data.schema import CANONICAL_TENORS
from ficc.strategy.carry_rolldown import CarryRollStrategy
from ficc.utils.calendar import TradingCalendar

_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS = _ROOT / "scripts"
_SCRIPT = _SCRIPTS / "run_cost_study.py"
_START = "2019-01-28"
_END = "2019-02-28"

FIXED_PATH_COLUMNS = {
    "strategy",
    "scenario",
    "switches",
    "start",
    "end",
    "config_hash",
    "final_nav",
    "total_return",
    "benchmark_final_nav",
    "excess_bp",
    "total_fee",
    "total_funding",
    "total_spread",
    "total_cost",
    "nav_impact_cny",
    "nav_impact_bp",
    "excess_change_bp",
    "n_trades",
}
DECISION_COLUMNS = {
    "strategy",
    "scenario",
    "start",
    "end",
    "config_hash",
    "final_nav",
    "total_return",
    "benchmark_final_nav",
    "excess_bp",
    "total_fee",
    "total_funding",
    "total_spread",
    "total_cost",
    "nav_impact_cny",
    "nav_impact_bp",
    "excess_change_bp",
    "n_trades",
    "n_rejected",
    "total_turnover",
    "n_violation_days",
}
SENSITIVITY_COLUMNS = {
    "strategy",
    "variant",
    "value",
    "path",
    "start",
    "end",
    "config_hash",
    "total_return",
    "excess_bp",
    "total_fee",
    "total_funding",
    "total_spread",
    "total_cost",
    "n_trades",
    "total_turnover",
    "n_violation_days",
    "status",
}


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def study_module():
    """导入脚本模块（复用其配置变体/基准补齐/回放守卫等函数做单元级校验）。"""
    if str(_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(_SCRIPTS))
    import run_cost_study

    return run_cost_study


@pytest.fixture(scope="module")
def report_dir(tmp_path_factory) -> Path:
    """把脚本产物写到隔离目录，避免测试覆盖真实 reports/。

    脚本的产物目录来自 ``cfg.paths.abs("reports")``；这里通过临时 config 文件
    覆盖 ``paths.reports`` 指向 pytest 临时目录。
    """
    out = tmp_path_factory.mktemp("cost_study")
    cfg = get_config()
    raw = cfg.model_dump(mode="json")
    raw["paths"]["reports"] = str(out)
    config_path = out / "config.json"
    config_path.write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    return out


@pytest.fixture(scope="module")
def quick_run(report_dir) -> Path:
    """执行 ``--quick``（短窗口、单策略），返回产物目录。"""
    config_path = report_dir / "config.json"
    env = {
        **os.environ,
        "PYTHONPATH": str(_ROOT / "src"),
        "MPLCONFIGDIR": str(_ROOT / ".mplconfig"),
    }
    completed = subprocess.run(
        [
            sys.executable,
            str(_SCRIPT),
            "--quick",
            "--start",
            _START,
            "--end",
            _END,
            "--config",
            str(config_path),
        ],
        cwd=_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    assert completed.returncode == 0, (
        f"run_cost_study.py --quick 失败（returncode={completed.returncode}）\n"
        f"--- stdout ---\n{completed.stdout[-4000:]}\n--- stderr ---\n{completed.stderr[-4000:]}"
    )
    return report_dir


@pytest.fixture(scope="module")
def frames(quick_run) -> dict[str, pd.DataFrame]:
    paths = {
        "fixed_path": quick_run / "cost_study_fixed_path.csv",
        "decision_change": quick_run / "cost_study_decision_change.csv",
        "sensitivity": quick_run / "cost_sensitivity.csv",
    }
    for path in paths.values():
        assert path.exists(), f"缺少产物 {path}"
    return {name: pd.read_csv(path) for name, path in paths.items()}


@pytest.fixture(scope="module")
def market():
    cfg = get_config()
    long = load_curve_long(cfg=cfg)
    panel = (
        long.pivot(index="date", columns="tenor", values="ytm_pct")
        .sort_index()
        .reindex(columns=list(CANONICAL_TENORS))
    )
    funding = load_funding_series(cfg=cfg).set_index("date")["FDR007"].sort_index()
    calendar = TradingCalendar.from_dates(panel.index)
    return cfg, panel, funding, calendar


@pytest.fixture(scope="module")
def baseline(market):
    cfg, panel, funding, calendar = market
    return run_backtest(
        CarryRollStrategy(),
        panel=panel,
        calendar=calendar,
        funding=funding,
        cfg=cfg,
        start=pd.Timestamp(_START).date(),
        end=pd.Timestamp(_END).date(),
    )


# ---------------------------------------------------------------------------
# 1) 产物与列契约
# ---------------------------------------------------------------------------
def test_quick_produces_three_csv_with_stable_columns(frames) -> None:
    assert set(frames) == {"fixed_path", "decision_change", "sensitivity"}
    assert set(frames["fixed_path"].columns) == FIXED_PATH_COLUMNS
    assert set(frames["decision_change"].columns) == DECISION_COLUMNS
    assert set(frames["sensitivity"].columns) == SENSITIVITY_COLUMNS
    for name, frame in frames.items():
        assert len(frame) > 0, f"{name} 产物为空"


def test_quick_runs_single_strategy(frames) -> None:
    for frame in frames.values():
        assert set(frame["strategy"].unique()) == {"carry_roll"}


# ---------------------------------------------------------------------------
# 2) 固定路径分解
# ---------------------------------------------------------------------------
def test_fixed_path_scenarios_and_additivity(frames) -> None:
    fixed = frames["fixed_path"].set_index("scenario")
    assert {"baseline", "no_spread", "no_fee", "no_funding", "all_off"} <= set(fixed.index)
    baseline = fixed.loc["baseline"]
    # 价差开关的 NAV 改善 ≈ 基线价差成本
    assert float(fixed.loc["no_spread", "nav_impact_cny"]) == pytest.approx(
        float(baseline["total_spread"]), rel=1e-6
    )
    # 费用开关的 NAV 改善 ≈ 基线费用
    assert float(fixed.loc["no_fee", "nav_impact_cny"]) == pytest.approx(
        float(baseline["total_fee"]), rel=1e-6
    )
    # 三开关全关 ≈ 三项成本合计
    assert float(fixed.loc["all_off", "nav_impact_cny"]) == pytest.approx(
        float(baseline["total_cost"]), rel=1e-6
    )
    # 基准同样重放：baseline 相对基准超额应有限
    assert np.isfinite(float(baseline["excess_bp"]))
    # 开关全关时相对基准超额变化 = 省下的成本（bp 口径）
    assert float(fixed.loc["all_off", "excess_change_bp"]) > 0.0


# ---------------------------------------------------------------------------
# 3) 决策变化
# ---------------------------------------------------------------------------
def test_decision_change_reruns_engine(frames) -> None:
    decision = frames["decision_change"].set_index("scenario")
    assert {
        "baseline",
        "engine_no_spread",
        "engine_no_fee",
        "engine_no_funding",
        "engine_all_off",
    } <= set(decision.index)
    baseline = decision.loc["baseline"]
    for column in ("total_return", "excess_bp", "n_trades", "total_turnover", "n_violation_days"):
        assert np.isfinite(float(baseline[column])), f"决策变化基线列 {column} 非有限"
    assert int(baseline["n_trades"]) > 0
    # 关掉价差后总价差成本必为 0，且 NAV 不低于基线
    no_spread = decision.loc["engine_no_spread"]
    assert float(no_spread["total_spread"]) == 0.0
    assert float(no_spread["final_nav"]) >= float(baseline["final_nav"]) - 1e-6
    # 全部关闭后总成本为 0
    assert float(decision.loc["engine_all_off", "total_cost"]) == 0.0


# ---------------------------------------------------------------------------
# 4) 参数敏感性
# ---------------------------------------------------------------------------
def test_sensitivity_grid_covers_required_variants(frames) -> None:
    sensitivity = frames["sensitivity"]
    variants = set(sensitivity["variant"].unique())
    assert {"baseline", "min_trade_lot", "rebalance_band_lots", "backtest.rebalance"} <= variants
    assert "spread_multiplier" in variants
    assert {"engine", "fixed_replay"} <= set(sensitivity["path"].unique())
    # 价差乘数 0/1 同时出现在引擎重跑与固定路径回放
    multiplier = sensitivity[sensitivity["variant"] == "spread_multiplier"]
    values = multiplier["value"].astype(str)
    for multiplier_value in ("0.0", "1.0"):
        rows = multiplier[values == multiplier_value]
        assert len(rows) >= 2, f"价差乘数 {multiplier_value} 缺少 engine/fixed_replay 对照"
    # 至少大部分变体成功跑通；引擎缺 weekly 时允许 skipped 行
    ok = sensitivity[sensitivity["status"].astype(str).str.startswith("ok")]
    assert len(ok) >= len(sensitivity) - 1


# ---------------------------------------------------------------------------
# 5) 基准账本回放 == 引擎 benchmark_nav
# ---------------------------------------------------------------------------
def test_benchmark_replay_matches_engine_benchmark_nav(market, baseline, study_module) -> None:
    cfg, panel, funding, calendar = market
    benchmark_trades = study_module._benchmark_trades(baseline, panel, cfg)
    assert len(benchmark_trades) > 0
    _replay, benchmark = study_module._replay_pair(
        baseline,
        panel=panel,
        calendar=calendar,
        funding=funding,
        cfg=cfg,
        switches=CostSwitches(),
        benchmark_trades=benchmark_trades,
    )
    assert benchmark is not None
    max_error = float(np.max(np.abs(benchmark.nav.to_numpy() - baseline.benchmark_nav.to_numpy())))
    assert max_error < 1e-6, f"基准回放 NAV 误差 {max_error:.3e} 元"


# ---------------------------------------------------------------------------
# 6) 回放守卫：不一致必须报错
# ---------------------------------------------------------------------------
def test_replay_guard_raises_on_mismatch(market, baseline, study_module) -> None:
    cfg, panel, funding, calendar = market
    replay = replay_engine_result(
        baseline,
        panel=panel,
        calendar=calendar,
        funding=funding,
        cfg=cfg,
        switches=CostSwitches(),
    )
    tampered = ReplayResult(nav=replay.nav + 1.0, cashflows=replay.cashflows, costs=replay.costs)
    with pytest.raises(RuntimeError, match="回放"):
        study_module._check_replay_matches_engine(baseline, tampered, None)
