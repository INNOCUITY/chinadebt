"""lot=5M 主动暴露解释的测试（``scripts/run_lot_exposure.py``）。"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pandas as pd
import pytest

from ficc.attribution import AttributionResult
from ficc.research import (
    EXCESS_SOURCE_NO,
    EXCESS_SOURCE_NONE,
    EXCESS_SOURCE_PARTIAL,
    EXCESS_SOURCE_YES,
    active_exposure_metrics,
    attribution_bp,
    attribution_components,
    classify_excess_source,
    excess_attribution_bp,
)

_ROOT = Path(__file__).resolve().parents[1]
_SCRIPTS = _ROOT / "scripts"


# ---------------------------------------------------------------------------
# 主动暴露指标（合成 exposure 帧，不跑引擎）
# ---------------------------------------------------------------------------
def _synthetic_exposure() -> pd.DataFrame:
    return pd.DataFrame(
        {
            "date": pd.bdate_range("2019-01-01", periods=3),
            "dv01_ratio": [0.0, 1.1, 0.9],
            "duration": [0.0, 8.2, 7.8],
            "bench_duration": [8.0, 8.0, 8.0],
            "leverage": [0.0, 1.0, 1.0],
            "market_value": [0.0, 1e8, 1e8],
            "nav": [1e8, 1e8, 1e8],
            "bench_total_dv01": [40000.0, 40000.0, 40000.0],
            "krd_10": [0.0, 44000.0, 36000.0],
            "bench_krd_10": [0.0, 40000.0, 40000.0],
            "krd_2": [0.0, 40000.0, 40000.0],
            "bench_krd_2": [0.0, 39200.0, 39200.0],
        }
    )


def test_active_exposure_metrics_warmup_and_krd() -> None:
    metrics = active_exposure_metrics(_synthetic_exposure())
    # 首日无持仓（market_value=0）被 warmup 剔除
    assert metrics["n_exposure_days"] == 2
    assert metrics["avg_abs_dv01_dev"] == pytest.approx(0.1)
    assert metrics["max_abs_dv01_dev"] == pytest.approx(0.1)
    assert metrics["avg_dv01_ratio"] == pytest.approx(1.0)
    assert metrics["avg_duration_dev"] == pytest.approx(0.0)
    assert metrics["avg_abs_duration_dev"] == pytest.approx(0.025)
    assert metrics["avg_leverage"] == pytest.approx(1.0)
    # 10Y 桶有符号均值 0（+0.1 / −0.1），2Y 桶均值 +0.02
    assert metrics["krd_dev_10"] == pytest.approx(0.0)
    assert metrics["krd_dev_2"] == pytest.approx(0.02)
    assert metrics["krd_dev_max_bucket"] == "2Y"
    assert metrics["krd_dev_max_abs"] == pytest.approx(0.02)
    assert metrics["krd_abs_dev_max"] == pytest.approx(0.1)
    assert metrics["krd_abs_dev_max_bucket"] == "10Y"


def test_active_exposure_metrics_rejects_bad_frames() -> None:
    with pytest.raises(ValueError, match="缺少必需列"):
        active_exposure_metrics(pd.DataFrame({"dv01_ratio": [1.0]}))
    with pytest.raises(TypeError):
        active_exposure_metrics([1, 2, 3])  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# 归因分解（合成 AttributionResult，验证可加恒等式与 bp 换算）
# ---------------------------------------------------------------------------
def _synthetic_attribution(strategy: str = "synthetic") -> AttributionResult:
    factors = pd.DataFrame(
        {
            "carry_roll": [60.0, 40.0],
            "level": [30.0, 20.0],
            "slope": [-10.0, -10.0],
            "curvature": [6.0, 4.0],
            "other_curve": [2.0, 3.0],
            "price_pnl": [88.0, 57.0],
        }
    )
    daily = pd.DataFrame(
        {
            "nav_delta": [170.0, 165.0],
            "coupon_cash": [100.0, 100.0],
            "principal_cash": [0.0, 0.0],
            "principal_redemption": [0.0, 0.0],
            "funding": [5.0, 5.0],
            "fees": [1.0, 1.0],
            "slippage": [-2.0, -1.0],
            "trade_settlement": [50.0, -50.0],
            "pending_delta": [30.0, -30.0],
            "trade_entry": [-80.0, 80.0],
            "bridge_residual": [0.0, 0.0],
            "valuation_basis": [2.0, 3.0],
        }
    )
    return AttributionResult(
        strategy=strategy,
        daily=daily,
        factors=factors,
        bonds=pd.DataFrame(),
        residual_bp=pd.Series(dtype=float),
    )


def test_attribution_components_additive() -> None:
    components = attribution_components(_synthetic_attribution())
    assert components["carry_roll"] == pytest.approx(100.0)
    assert components["level"] == pytest.approx(50.0)
    assert components["coupon"] == pytest.approx(200.0)
    assert components["funding"] == pytest.approx(-10.0)
    assert components["fees"] == pytest.approx(-2.0)
    assert components["slippage"] == pytest.approx(-3.0)
    assert components["trade_net"] == pytest.approx(0.0)
    assert components["nav_delta"] == pytest.approx(335.0)
    # 恒等式：nav_delta == Σ 分项（gap 只留数值噪声）
    assert components["gap"] == pytest.approx(0.0, abs=1e-9)


def test_attribution_bp_and_excess() -> None:
    strategy = _synthetic_attribution("strategy")
    benchmark = _synthetic_attribution("benchmark")
    # 基准归因清零 → 相对归因 == 策略归因
    for frame in (benchmark.factors, benchmark.daily):
        for column in frame.columns:
            frame[column] = 0.0
    bp = attribution_bp(strategy, initial_nav=1_000_000.0)
    assert bp["nav_delta_bp"] == pytest.approx(335.0 / 1_000_000.0 * 1e4)
    assert bp["carry_roll"] == pytest.approx(1.0)  # 100 元 / 1e6 × 1e4 bp
    assert bp["gap_bp"] == pytest.approx(0.0, abs=1e-9)
    excess = excess_attribution_bp(strategy, benchmark, initial_nav=1_000_000.0)
    assert excess["nav_delta_bp"] == pytest.approx(bp["nav_delta_bp"], abs=1e-9)
    for key in ("carry_roll", "level", "funding", "fees", "valuation_basis"):
        assert key in excess


def test_classify_excess_source_rules() -> None:
    assert (
        classify_excess_source(delta_excess_bp=20.0, market_component_bp=15.0, exposure_delta=0.01)
        == EXCESS_SOURCE_YES
    )
    assert (
        classify_excess_source(delta_excess_bp=20.0, market_component_bp=15.0, exposure_delta=0.001)
        == EXCESS_SOURCE_PARTIAL
    )
    assert (
        classify_excess_source(delta_excess_bp=20.0, market_component_bp=5.0, exposure_delta=0.01)
        == EXCESS_SOURCE_NO
    )
    assert (
        classify_excess_source(delta_excess_bp=0.3, market_component_bp=0.3, exposure_delta=0.01)
        == EXCESS_SOURCE_NONE
    )
    assert (
        classify_excess_source(
            delta_excess_bp=float("nan"), market_component_bp=1.0, exposure_delta=0.01
        )
        == EXCESS_SOURCE_NONE
    )


# ---------------------------------------------------------------------------
# 脚本 --quick 端到端（离线、隔离产物目录）
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def lot_module():
    if str(_SCRIPTS) not in sys.path:
        sys.path.insert(0, str(_SCRIPTS))
    import run_lot_exposure

    return run_lot_exposure


@pytest.fixture(scope="module")
def lot_report_dir(tmp_path_factory) -> Path:
    out = tmp_path_factory.mktemp("lot_exposure")
    cfg = __import__("ficc.config", fromlist=["get_config"]).get_config()
    raw = cfg.model_dump(mode="json")
    raw["paths"]["reports"] = str(out)
    (out / "config.json").write_text(json.dumps(raw, ensure_ascii=False), encoding="utf-8")
    return out


@pytest.fixture(scope="module")
def lot_quick_run(lot_report_dir: Path) -> Path:
    config_path = lot_report_dir / "config.json"
    env = {
        **os.environ,
        "PYTHONPATH": str(_ROOT / "src"),
        "MPLCONFIGDIR": str(_ROOT / ".mplconfig"),
    }
    completed = subprocess.run(
        [
            sys.executable,
            str(_SCRIPTS / "run_lot_exposure.py"),
            "--quick",
            "--strategies",
            "2s10s",
            "--start",
            "2019-01-28",
            "--end",
            "2019-06-28",
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
        f"run_lot_exposure.py --quick 失败（{completed.returncode}）\n"
        f"--- stdout ---\n{completed.stdout[-4000:]}\n--- stderr ---\n{completed.stderr[-4000:]}"
    )
    return lot_report_dir


def test_lot_quick_outputs_and_columns(lot_quick_run: Path, lot_module) -> None:
    summary = pd.read_csv(lot_quick_run / "lot_exposure_summary.csv")
    attribution = pd.read_csv(lot_quick_run / "lot_exposure_attribution.csv")
    diff = pd.read_csv(lot_quick_run / "lot_exposure_diff.csv")
    markdown = (lot_quick_run / "lot_exposure.md").read_text(encoding="utf-8")
    assert set(summary.columns) == set(lot_module.LOT_SUMMARY_COLUMNS)
    assert set(attribution.columns) == set(lot_module.LOT_ATTRIBUTION_COLUMNS)
    assert set(diff.columns) == set(lot_module.LOT_DIFF_COLUMNS)
    assert set(summary["lot"]) == {1_000_000.0, 5_000_000.0}
    assert set(summary["strategy"]) == {"2s10s"}
    assert len(diff) == 1
    # 归因恒等式：分项可加，gap 只是数值噪声
    assert diff["identity_gap_bp"].abs().max() < 1e-6
    for column in ("carry_roll", "level", "slope", "curvature", "other_curve"):
        assert column in attribution.columns
    assert diff["excess_source"].iloc[0] in {
        EXCESS_SOURCE_YES,
        EXCESS_SOURCE_PARTIAL,
        EXCESS_SOURCE_NO,
        EXCESS_SOURCE_NONE,
    }
    assert diff["delta_excess_bp"].iloc[0] == pytest.approx(
        diff["excess_bp_alt"].iloc[0] - diff["excess_bp_base"].iloc[0]
    )
    assert diff["market_share"].iloc[0] == pytest.approx(
        diff["delta_market_bp"].iloc[0] / diff["delta_excess_bp"].iloc[0]
    )
    # Markdown 必须包含明确结论与"lot 不是可调参数"的声明
    assert "是否来自不同的主动风险暴露" in markdown
    assert "lot 不是可调参数" in markdown
    assert "预注册" in markdown
    assert "identity_gap_bp" in markdown or "gap" in markdown
