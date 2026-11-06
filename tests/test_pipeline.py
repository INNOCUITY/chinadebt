"""编排脚本 `scripts/run_pipeline.py` 的单元测试。

脚本不是"跑一次看看"，它的数据加载与清洗逻辑必须有测试，否则整条链路
（数据层 → 曲线层 → 图）只能靠肉眼保证。

用 importlib 直接按路径加载脚本，避免把 scripts 变成 package。
"""

from __future__ import annotations

import importlib.util
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from ficc.data.schema import CANONICAL_TENORS

_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run_pipeline.py"


def _load_module():
    spec = importlib.util.spec_from_file_location("run_pipeline", _SCRIPT)
    if spec is None or spec.loader is None:  # pragma: no cover
        pytest.skip(f"无法加载脚本: {_SCRIPT}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def pipeline():
    return _load_module()


# ---------------------------------------------------------------------------
# _finalize：日期过滤 / 去重 / 冲突检测 / 契约校验
# ---------------------------------------------------------------------------
def test_finalize_filters_date_range(pipeline) -> None:
    df = pd.DataFrame(
        {
            "date": ["2020-01-02", "2020-01-03", "2020-01-06"],
            "tenor": [1.0, 1.0, 1.0],
            "ytm_pct": [2.5, 2.6, 2.7],
        }
    )
    out = pipeline._finalize(df, start=date(2020, 1, 3), end=date(2020, 1, 6))
    assert len(out) == 2
    assert out["date"].min() == pd.Timestamp("2020-01-03")


def test_finalize_accepts_exact_duplicates(pipeline) -> None:
    """跨年文件重叠产生完全相同的重复行：允许，自动去重。"""
    df = pd.DataFrame(
        {
            "date": ["2024-12-31", "2024-12-31"],
            "tenor": [1.0, 1.0],
            "ytm_pct": [1.05, 1.05],
        }
    )
    out = pipeline._finalize(df, start=None, end=None)
    assert len(out) == 1


def test_finalize_rejects_conflicting_duplicates(pipeline) -> None:
    """同一 (date, tenor) 出现不同收益率 = 真数据冲突，必须报错。"""
    df = pd.DataFrame(
        {
            "date": ["2024-12-31", "2024-12-31"],
            "tenor": [1.0, 1.0],
            "ytm_pct": [1.05, 1.10],
        }
    )
    with pytest.raises(ValueError, match="数值冲突"):
        pipeline._finalize(df, start=None, end=None)


def test_finalize_requires_columns(pipeline) -> None:
    with pytest.raises(ValueError, match="缺少必要列"):
        pipeline._finalize(pd.DataFrame({"date": ["2024-01-02"]}), None, None)


# ---------------------------------------------------------------------------
# to_panel
# ---------------------------------------------------------------------------
def test_to_panel_has_canonical_columns(pipeline) -> None:
    df = pd.DataFrame(
        {
            "date": [pd.Timestamp("2024-01-02")] * 3,
            "tenor": [1.0, 5.0, 10.0],
            "ytm_pct": [2.0, 2.4, 2.6],
        }
    )
    panel = pipeline.to_panel(df)
    assert list(panel.columns) == list(CANONICAL_TENORS)
    assert panel.shape == (1, len(CANONICAL_TENORS))
    assert panel.loc[pd.Timestamp("2024-01-02"), 5.0] == pytest.approx(2.4)
    # 未提供的期限应为 NaN，而不是被静默填充
    assert pd.isna(panel.loc[pd.Timestamp("2024-01-02"), 30.0])


# ---------------------------------------------------------------------------
# 兜底加载：数据层产物缺失时才读 raw_spike
# ---------------------------------------------------------------------------
def test_load_fallback_csv_reads_spike_data(pipeline) -> None:
    spike = (
        Path(__file__).resolve().parents[1]
        / "data"
        / "raw_spike"
        / "cb_gzqx_standard_terms_2002_2026.csv"
    )
    if not spike.exists():
        pytest.skip("缺少 Phase 0 实测数据")
    df = pipeline._load_fallback_csv()
    assert len(df) > 10_000
    assert set(df["tenor"]).issubset(set(CANONICAL_TENORS))


def test_bad_stage_rejected(pipeline) -> None:
    with pytest.raises(SystemExit):
        pipeline.main(["--stage", "not_a_stage"])
