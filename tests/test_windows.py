"""多窗口 + HAC/块 bootstrap 区间估计的测试（``scripts/run_windows.py``）。"""

from __future__ import annotations

import itertools
import json
import os
import subprocess
import sys
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from ficc.research import add_years, resolve_benchmark_version
from ficc.research.inference import (
    BOOTSTRAP_BLOCK,
    BOOTSTRAP_REPS,
    BOOTSTRAP_SEED,
    HAC_LAG,
    annualized_inference,
    block_bootstrap,
    newey_west,
)
from ficc.research.windows import (
    WindowSpec,
    build_windows,
    window_active_returns,
    window_metrics,
    window_total_returns,
)

_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS = _ROOT / "scripts"


# ---------------------------------------------------------------------------
# 预注册参数与窗口切分
# ---------------------------------------------------------------------------
def test_preregistered_inference_parameters_are_frozen() -> None:
    """预注册参数写死在模块常量里，防止事后按结果调整。"""
    assert HAC_LAG == 5
    assert BOOTSTRAP_BLOCK == 20
    assert BOOTSTRAP_REPS == 2000
    assert BOOTSTRAP_SEED == 20261108
    import ficc.research.inference as inference_mod

    assert inference_mod.ANNUAL_DAYS == 250
    assert pytest.approx(1.959963984540054) == inference_mod.Z_95


@pytest.fixture(scope="module")
def business_days() -> list[date]:
    return [timestamp.date() for timestamp in pd.bdate_range("2019-01-01", "2023-06-30")]


def test_nonoverlap_windows_partition_days(business_days: list[date]) -> None:
    windows = build_windows(business_days, window_years=2, overlap=False)
    flattened = [day for window in windows for day in window.days]
    assert flattened == business_days  # 不重不漏且顺序一致
    assert windows[0].start == business_days[0]
    assert windows[-1].end == business_days[-1]
    for left, right in itertools.pairwise(windows):
        assert left.end < right.start
    for window in windows:
        assert window.n_days == len(window.days)
        assert window.days[0] == window.start and window.days[-1] == window.end


def test_nonoverlap_window_counts_and_truncation(business_days: list[date]) -> None:
    one_year = build_windows(business_days, window_years=1, overlap=False)
    assert len(one_year) == 5  # 2019..2023 年末 + 2023 半截
    two_year = build_windows(business_days, window_years=2, overlap=False)
    assert len(two_year) == 3
    # 最后一个窗口是截断的（不足 2 年）
    assert two_year[-1].n_days < two_year[0].n_days
    assert two_year[-1].end == business_days[-1]


def test_overlap_windows_count_and_membership(business_days: list[date]) -> None:
    windows = build_windows(business_days, window_years=2, overlap=True, step_years=1, min_days=20)
    # 起点 2019/2020/2021/2022/2023 各一窗（末尾窗被截断但 >= 20 天）
    assert len(windows) == 5
    assert [window.start.year for window in windows] == [2019, 2020, 2021, 2022, 2023]
    # 相邻窗口在时间上确实重叠（首尾相接不算重叠）
    for left, right in itertools.pairwise(windows):
        assert right.start <= left.end
    total_days = sum(window.n_days for window in windows)
    assert total_days > len(business_days)  # 重叠导致重复覆盖
    # 非重叠模式与重叠模式在窗口数上不同
    nonoverlap = build_windows(business_days, window_years=2, overlap=False)
    assert len(nonoverlap) == 3


def test_nonoverlap_end_cap_keeps_partition(business_days: list[date]) -> None:
    """end_cap 收缩右端后仍不重不漏，且每窗末日不超过 cap。"""
    cap = lambda start: add_years(start, 2) - timedelta(days=10)  # noqa: E731
    windows = build_windows(business_days, window_years=2, overlap=False, end_cap=cap)
    flattened = [day for window in windows for day in window.days]
    assert flattened == business_days
    for window in windows:
        assert window.end <= cap(window.start)


def test_end_cap_moves_trimmed_days_to_next_window(business_days: list[date]) -> None:
    """被 end_cap 裁掉的交易日顺延为下一窗起点，窗口数可能增加但不丢日。"""
    capped = build_windows(
        business_days, window_years=2, overlap=False, end_cap=lambda start: add_years(start, 1)
    )
    uncapped = build_windows(business_days, window_years=2, overlap=False)
    assert len(capped) >= len(uncapped)
    assert [day for window in capped for day in window.days] == business_days
    for left, right in itertools.pairwise(capped):
        assert left.end < right.start


def test_invalid_window_parameters(business_days: list[date]) -> None:
    with pytest.raises(ValueError):
        build_windows(business_days, window_years=0)
    with pytest.raises(ValueError):
        build_windows(business_days, overlap=True, step_years=0)


# ---------------------------------------------------------------------------
# HAC / bootstrap 统计行为（不断言精确 p 值，只断言方向与覆盖）
# ---------------------------------------------------------------------------
def _ar1(phi: float, n: int, *, seed: int) -> np.ndarray:
    """平稳 AR(1)：单位新息方差缩放到平稳方差 1（便于跨 phi 比较区间宽度）。"""
    rng = np.random.default_rng(seed)
    innovations = rng.normal(size=n) * np.sqrt(max(1.0 - phi * phi, 1e-12))
    values = np.empty(n)
    values[0] = innovations[0]
    for index in range(1, n):
        values[index] = phi * values[index - 1] + innovations[index]
    return values


def test_hac_se_increases_with_autocorrelation() -> None:
    iid = _ar1(0.0, 2000, seed=11)
    autocorrelated = _ar1(0.8, 2000, seed=11)
    _, se_iid, _ = newey_west(iid)
    _, se_ar, _ = newey_west(autocorrelated)
    assert np.isfinite(se_iid) and np.isfinite(se_ar)
    assert se_ar > 2.0 * se_iid, f"AR(1) 下 HAC SE 未变大：{se_ar:.6g} vs {se_iid:.6g}"


def test_block_bootstrap_ci_widens_with_autocorrelation() -> None:
    iid = annualized_inference(1e-3 + _ar1(0.0, 800, seed=7), reps=500)
    autocorrelated = annualized_inference(1e-3 + _ar1(0.8, 800, seed=7), reps=500)
    width_iid = iid.boot_ci_high_bp - iid.boot_ci_low_bp
    width_ar = autocorrelated.boot_ci_high_bp - autocorrelated.boot_ci_low_bp
    assert width_iid > 0 and width_ar > 0
    assert width_ar > 1.5 * width_iid, (
        f"块 bootstrap 区间未随自相关变宽：{width_ar:.4g} vs {width_iid:.4g}"
    )


def test_hac_ci_coverage_for_iid_zero_mean() -> None:
    """iid 零均值序列：HAC 95% 区间应多数覆盖 0（只要求宽松覆盖，不检验精确 p 值）。"""
    covered = 0
    seeds = range(20)
    for seed in seeds:
        rng = np.random.default_rng(seed)
        values = rng.normal(scale=1e-4, size=500)
        summary = annualized_inference(values, reps=100)
        if summary.hac_ci_low_bp <= 0.0 <= summary.hac_ci_high_bp:
            covered += 1
    assert covered >= 16, f"HAC 区间覆盖率过低：{covered}/20"


def test_annualized_mean_and_constant_series() -> None:
    """常数序列：年化均值 = mean×250（×1e4 转 bp），se=0、CI 退化为点。"""
    values = np.full(100, 0.001)
    summary = annualized_inference(values, reps=50)
    assert summary.mean_ann_bp == pytest.approx(0.001 * 250 * 1e4, rel=0.0, abs=1e-9)
    assert summary.hac_se_ann_bp == pytest.approx(0.0, abs=1e-6)
    assert summary.hac_ci_low_bp == pytest.approx(summary.mean_ann_bp, abs=1e-6)
    assert summary.hac_ci_high_bp == pytest.approx(summary.mean_ann_bp, abs=1e-6)


def test_bootstrap_deterministic_and_directional() -> None:
    rng = np.random.default_rng(3)
    values = rng.normal(loc=3e-4, scale=1e-4, size=400)
    first = block_bootstrap(values, reps=300, seed=BOOTSTRAP_SEED)
    second = block_bootstrap(values, reps=300, seed=BOOTSTRAP_SEED)
    assert first == second
    assert first["boot_ci_low_bp"] < first["boot_ci_high_bp"]
    # 明显正均值：双侧 p 值应很小、区间整体在 0 以上
    assert first["boot_p_value"] < 0.05
    assert first["boot_ci_low_bp"] > 0.0


# ---------------------------------------------------------------------------
# 窗口指标（用合成 BacktestResult 帧，不跑引擎）
# ---------------------------------------------------------------------------
def _fake_result(days: list[date]) -> object:
    from types import SimpleNamespace

    nav = pd.Series(np.full(len(days), 1e8), index=days, name="nav")
    benchmark = pd.Series(np.full(len(days), 1e8), index=days, name="benchmark_nav")
    costs = pd.DataFrame(
        {
            "date": days,
            "fee": [1.0] * len(days),
            "funding": [2.0] * len(days),
            "spread": [4.0] * len(days),
            "total": [7.0] * len(days),
        }
    )
    turnover = pd.Series(0.01, index=days, name="turnover")
    trades = pd.DataFrame(
        {
            "trade_date": [days[1], days[2], days[5]],
            "face_value": [1e6, 2e6, 3e6],
        }
    )
    rejected = pd.DataFrame({"date": [days[1], days[6]]})
    constraints = pd.DataFrame(
        {
            "date": days,
            "ok": [True, True, False, True, True, True, True, True, True, True][: len(days)],
            "n_positions": [0] + [4] * (len(days) - 1),
        }
    )
    exposure = pd.DataFrame(
        {
            "date": days,
            "dv01_ratio": [0.0] + [1.02] * 3 + [0.98] * (len(days) - 4),
            "duration": [0.0] + [8.2] * 3 + [7.8] * (len(days) - 4),
            "bench_duration": [8.0] * len(days),
            "leverage": [0.0] + [1.0] * (len(days) - 1),
            "market_value": [0.0] + [1e8] * (len(days) - 1),
            "nav": [1e8] * len(days),
            "bench_total_dv01": [40000.0] * len(days),
            "krd_10": [0.0] + [40800.0] * 3 + [39200.0] * (len(days) - 4),
            "bench_krd_10": [0.0] + [40000.0] * (len(days) - 1),
        }
    )
    return SimpleNamespace(
        nav=nav,
        benchmark_nav=benchmark,
        costs=costs,
        turnover=turnover,
        trades=trades,
        rejected=rejected,
        constraints=constraints,
        exposure=exposure,
    )


def test_window_metrics_slices_costs_trades_and_exposure() -> None:
    days = [timestamp.date() for timestamp in pd.bdate_range("2019-01-01", periods=10)]
    result = _fake_result(days)
    window = WindowSpec(label="W1", start=days[0], end=days[4], days=tuple(days[:5]))
    metrics = window_metrics(result, window, annual_days=250)
    assert metrics["n_days"] == 5
    assert metrics["n_obs"] == 4  # 首日无前收，主动收益从第 2 天开始
    assert metrics["strategy_total_return"] == pytest.approx(0.0)
    assert metrics["excess_bp"] == pytest.approx(0.0)
    assert metrics["total_fee"] == pytest.approx(5.0)  # 只累计窗口内 5 天
    assert metrics["total_cost"] == pytest.approx(35.0)
    assert metrics["n_trades"] == 2  # days[1]、days[2] 在窗口内
    assert metrics["n_rejected"] == 1
    assert metrics["n_violation_days"] == 1
    # warmup（首日无持仓）被剔除：|DV01 ratio−1| = 0.02
    assert metrics["avg_abs_dv01_dev"] == pytest.approx(0.02)
    assert metrics["avg_abs_duration_dev"] == pytest.approx(0.025)
    active = window_active_returns(result, window)
    assert len(active) == 4
    strategy_total, benchmark_total, excess_bp = window_total_returns(result, window)
    assert strategy_total == pytest.approx(0.0)
    assert benchmark_total == pytest.approx(0.0)
    assert excess_bp == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# 基准版本兼容层（rolling 接口就绪后自动开启滚动政策；不改 config.yaml）
# ---------------------------------------------------------------------------
def test_resolve_benchmark_version_rolling_enables_policy(cfg) -> None:
    resolution = resolve_benchmark_version(cfg, "rolling")
    assert resolution.effective == "rolling"
    assert resolution.fell_back is False
    assert resolution.cfg.backtest.benchmark_version == "rolling"
    assert resolution.cfg.portfolio.rolling.enabled is True
    assert resolution.rolling_version  # 滚动政策版本号必须非空
    # 请求 buyhold 时必须关闭滚动政策（避免配置残留导致引擎校验失败）
    buyhold = resolve_benchmark_version(cfg, "buyhold")
    assert buyhold.cfg.backtest.benchmark_version == "buyhold"
    assert buyhold.cfg.portfolio.rolling.enabled is False
    with pytest.raises(ValueError):
        resolve_benchmark_version(cfg, "unknown")


# ---------------------------------------------------------------------------
# 脚本 --quick 端到端（离线、隔离产物目录）
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def windows_module():
    if str(_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(_SCRIPTS))
    import run_windows

    return run_windows


@pytest.fixture(scope="module")
def windows_report_dir(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("windows")
    cfg = __import__("ficc.config", fromlist=["get_config"]).get_config()
    raw = cfg.model_dump(mode="json")
    raw["paths"]["reports"] = str(out)
    (out / "config.json").write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    return out


@pytest.fixture(scope="module")
def windows_quick_run(windows_report_dir: Path) -> Path:
    config_path = windows_report_dir / "config.json"
    env = {
        **os.environ,
        "PYTHONPATH": str(_ROOT / "src"),
        "MPLCONFIGDIR": str(_ROOT / ".mplconfig"),
    }
    completed = subprocess.run(
        [
            sys.executable,
            str(_SCRIPTS / "run_windows.py"),
            "--quick",
            "--start",
            "2019-01-28",
            "--end",
            "2020-06-30",
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
        f"run_windows.py --quick 失败（{completed.returncode}）\n"
        f"--- stdout ---\n{completed.stdout[-4000:]}\n--- stderr ---\n{completed.stderr[-4000:]}"
    )
    return windows_report_dir


def test_windows_quick_outputs_stable_columns(windows_quick_run: Path, windows_module) -> None:
    summary_path = windows_quick_run / "windows_summary.csv"
    inference_path = windows_quick_run / "windows_inference.csv"
    assert summary_path.exists() and inference_path.exists()
    summary = pd.read_csv(summary_path)
    inference = pd.read_csv(inference_path)
    assert set(summary.columns) == set(windows_module.WINDOWS_SUMMARY_COLUMNS)
    assert set(inference.columns) == set(windows_module.WINDOWS_INFERENCE_COLUMNS)
    assert set(summary["strategy"]) == {"carry_roll"}  # quick 只跑 1 个策略
    assert set(summary["window_set"]) == {"nonoverlap"}
    assert set(summary["mode"]) == {"reset"}
    assert list(summary["window"]) == ["W1", "W2"]
    pooled = inference[inference["window"] == "pooled"]
    assert len(pooled) == 1
    assert int(pooled["n_windows"].iloc[0]) == 2
    for column in (
        "ann_excess_bp",
        "hac_se_ann_bp",
        "hac_ci_low_bp",
        "hac_ci_high_bp",
        "boot_ci_low_bp",
        "boot_ci_high_bp",
        "boot_p_value",
    ):
        values = pd.to_numeric(inference[column], errors="coerce")
        assert values.notna().all(), f"区间估计列 {column} 含 NaN"
    assert int(inference["hac_lag"].iloc[0]) == HAC_LAG
    assert int(inference["boot_block"].iloc[0]) == BOOTSTRAP_BLOCK
    assert (inference["boot_ci_low_bp"] <= inference["boot_ci_high_bp"]).all()
    assert (inference["hac_ci_low_bp"] <= inference["hac_ci_high_bp"]).all()
    assert ((inference["boot_p_value"] >= 0) & (inference["boot_p_value"] <= 1)).all()
    assert (pd.to_numeric(inference["boot_reps"], errors="coerce") > 0).all()


@pytest.fixture(scope="module")
def windows_rolling_quick_run(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("windows_rolling")
    cfg = __import__("ficc.config", fromlist=["get_config"]).get_config()
    raw = cfg.model_dump(mode="json")
    raw["paths"]["reports"] = str(out)
    (out / "config.json").write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    env = {
        **os.environ,
        "PYTHONPATH": str(_ROOT / "src"),
        "MPLCONFIGDIR": str(_ROOT / ".mplconfig"),
    }
    completed = subprocess.run(
        [
            sys.executable,
            str(_SCRIPTS / "run_windows.py"),
            "--quick",
            "--benchmark-version",
            "rolling",
            "--start",
            "2019-01-28",
            "--end",
            "2020-06-30",
            "--config",
            str(out / "config.json"),
        ],
        cwd=_ROOT,
        env=env,
        capture_output=True,
        text=True,
        timeout=600,
        check=False,
    )
    assert completed.returncode == 0, (
        f"run_windows.py --quick --benchmark-version rolling 失败（{completed.returncode}）\n"
        f"--- stdout ---\n{completed.stdout[-4000:]}\n--- stderr ---\n{completed.stderr[-4000:]}"
    )
    return out


def test_windows_rolling_branch_end_to_end(windows_rolling_quick_run: Path) -> None:
    summary = pd.read_csv(windows_rolling_quick_run / "windows_summary.csv")
    inference = pd.read_csv(windows_rolling_quick_run / "windows_inference.csv")
    assert set(summary["benchmark_version"]) == {"rolling"}
    assert set(inference["benchmark_version"]) == {"rolling"}
    # rolling 基准默认按连续长回测切窗
    assert set(summary["mode"]) == {"continuous"}
    # 滚动政策版本号必须记录（新旧基准不可混比）
    assert summary["rolling_version"].astype(str).str.len().gt(0).all()
    assert (summary["n_days"] > 0).all()
    assert set(summary["strategy"]) == {"carry_roll"}
