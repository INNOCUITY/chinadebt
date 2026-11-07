"""pipeline 的产品指纹守卫测试（评审 R12）。

评审问题：`bias` 以前直接读一个"不知道哪次生成、也不知道用了什么配置"的残差 CSV；
空报告目录会失败，预置旧文件又可能被静默消费。

修复后的契约：
1. `_ensure_products()` 用 meta 指纹（config hash / method / weights / 起止日 / 天数）
   判断现有产物是否属于"本次运行"；
2. 指纹不匹配 → **重新生成**，绝不消费旧文件；
3. 指纹匹配 → 复用；
4. 空目录 → 当次生成。

本文件只测这几个契约，不测拟合计本身（那在 test_curve_fit.py）。
"""

from __future__ import annotations

import importlib.util
import json
from pathlib import Path

import pandas as pd
import pytest

from ficc.data.schema import CANONICAL_TENORS


def _load_pipeline():
    script = Path(__file__).resolve().parents[1] / "scripts" / "run_pipeline.py"
    spec = importlib.util.spec_from_file_location("run_pipeline_products", script)
    if spec is None or spec.loader is None:  # pragma: no cover
        pytest.skip(f"无法加载脚本: {script}")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def pipeline():
    return _load_pipeline()


@pytest.fixture
def small_panel() -> pd.DataFrame:
    """30 个交易日 × 11 期限的小面板（让拟合计在测试里足够快）。"""
    idx = pd.bdate_range("2024-01-02", periods=30)
    base = pd.Series(
        {
            0.25: 1.9,
            0.5: 1.95,
            1.0: 2.0,
            2.0: 2.1,
            3.0: 2.2,
            5.0: 2.35,
            7.0: 2.45,
            10.0: 2.55,
            15.0: 2.7,
            20.0: 2.75,
            30.0: 2.8,
        }
    )
    import numpy as np

    frame = pd.DataFrame([base.values] * len(idx), index=idx, columns=list(base.index))
    # 加一点确定性的日内变化，避免拟合退化
    drift = np.arange(len(idx), dtype=float)[:, None] * 0.001
    frame = pd.DataFrame(frame.to_numpy() + drift, index=idx, columns=frame.columns)
    assert list(frame.columns) == list(CANONICAL_TENORS)
    return frame


def test_empty_out_dir_builds_products(pipeline, cfg, small_panel, tmp_path) -> None:
    """空目录必须当次生成自由/锚定产品（R12 的失败场景）。"""
    out = tmp_path / "reports"
    files = pipeline._ensure_products(cfg, small_panel, "nss", out, force=True)

    meta_path = out / pipeline.PRODUCTS_META_NAME
    assert meta_path.exists()
    for key in ("residuals_nss_free", "residuals_nss_anchored", "params_nss_free"):
        assert key in files and Path(files[key]).exists(), f"缺少产物 {key}"
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    assert meta["n_days"] == len(small_panel)
    assert meta["method"] == "nss"
    assert meta["weights"] == str(cfg.curve.weights)


def test_reuses_products_when_fingerprint_matches(pipeline, cfg, small_panel, tmp_path) -> None:
    out = tmp_path / "reports"
    first = pipeline._ensure_products(cfg, small_panel, "nss", out, force=True)
    mtime_before = Path(first["residuals_nss_free"]).stat().st_mtime_ns

    second = pipeline._ensure_products(cfg, small_panel, "nss", out, force=False)
    assert {k: Path(v) for k, v in second.items()} == {k: Path(v) for k, v in first.items()}
    assert Path(second["residuals_nss_free"]).stat().st_mtime_ns == mtime_before


def test_stale_products_are_regenerated(pipeline, cfg, small_panel, tmp_path) -> None:
    """预置"错误配置/日期"的旧产物不得被消费（R12 的关键反例）。"""
    out = tmp_path / "reports"
    files = pipeline._ensure_products(cfg, small_panel, "nss", out, force=True)

    # 人为篡改指纹：假装这批产物来自另一个区间/权重
    meta_path = out / pipeline.PRODUCTS_META_NAME
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    meta["n_days"] = 9999
    meta["weights"] = "equal"
    meta["start"] = "1900-01-01"
    meta_path.write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")

    # 同时把残差文件内容改成一眼可辨的哨兵值
    sentinel = Path(files["residuals_nss_free"])
    sentinel.write_text("date,0.25\n1900-01-01,999.0\n", encoding="utf-8")

    rebuilt = pipeline._ensure_products(cfg, small_panel, "nss", out, force=False)
    new_meta = json.loads(meta_path.read_text(encoding="utf-8"))

    assert new_meta["n_days"] == len(small_panel)
    assert new_meta["weights"] == str(cfg.curve.weights)
    rebuilt_text = Path(rebuilt["residuals_nss_free"]).read_text(encoding="utf-8")
    assert "1900-01-01" not in rebuilt_text, "哨兵旧内容仍在，说明旧产物被消费了"
    # 重建后的残差必须能被正常解析（维度/列名正确）
    frame = pd.read_csv(rebuilt["residuals_nss_free"], index_col=0, parse_dates=True)
    assert len(frame) == len(small_panel)
    assert len(frame.columns) == len(CANONICAL_TENORS)


def test_meta_counts_match_fingerprint(pipeline, cfg, small_panel, tmp_path) -> None:
    """指纹里的 start/end 必须等于面板的真实首尾日期。"""
    out = tmp_path / "reports"
    pipeline._ensure_products(cfg, small_panel, "nss", out, force=True)
    meta = json.loads((out / pipeline.PRODUCTS_META_NAME).read_text(encoding="utf-8"))
    assert meta["start"] == str(small_panel.index.min().date())
    assert meta["end"] == str(small_panel.index.max().date())
    assert meta["config_hash"] == cfg.hash()
