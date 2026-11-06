"""绘图模块测试。

目标不是"验证图好不好看"，而是保证：
1. 每个绘图函数在合法输入下**真的产出非空文件**（CI 里能跑、不会因字体/后端报错）；
2. 非法输入给出**明确异常**而不是画出一张空白图；
3. 极端输入（如 400bp 的离群残差）不会把图压成一条线（有稳健裁剪）。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from ficc.report.figures import (
    plot_curve_heatmap,
    plot_curve_snapshot,
    plot_nav,
    plot_param_series,
    plot_residual_by_tenor,
    plot_residual_hist,
    setup_style,
)

MIN_PNG_BYTES = 5_000  # 小于此值基本是一张空图


def _assert_png(path, min_bytes: int = MIN_PNG_BYTES) -> None:
    assert path.exists(), f"未生成文件: {path}"
    size = path.stat().st_size
    assert size > min_bytes, f"文件过小({size}B)，可能画成了空图: {path}"
    with path.open("rb") as fh:
        assert fh.read(8) == b"\x89PNG\r\n\x1a\n", "不是合法 PNG"


@pytest.fixture
def panel() -> pd.DataFrame:
    idx = pd.bdate_range("2020-01-01", periods=120)
    tenors = [0.25, 0.5, 1.0, 2.0, 3.0, 5.0, 7.0, 10.0, 15.0, 20.0, 30.0]
    base = np.array([2.0, 2.05, 2.1, 2.2, 2.3, 2.5, 2.65, 2.8, 2.95, 3.0, 3.1])
    data = base[None, :] + np.linspace(-0.2, 0.2, len(idx))[:, None]
    return pd.DataFrame(data, index=idx, columns=tenors)


def test_setup_style_is_idempotent() -> None:
    setup_style()
    setup_style()  # 不应抛异常


def test_curve_heatmap(tmp_path, panel) -> None:
    _assert_png(plot_curve_heatmap(panel, tmp_path / "hm.png"))


def test_curve_heatmap_rejects_empty(tmp_path) -> None:
    with pytest.raises(ValueError, match="为空"):
        plot_curve_heatmap(pd.DataFrame(), tmp_path / "x.png")


def test_curve_snapshot(tmp_path, panel) -> None:
    curves = {
        str(panel.index[0].date()): (panel.columns.to_numpy(), panel.iloc[0].to_numpy()),
        str(panel.index[-1].date()): (panel.columns.to_numpy(), panel.iloc[-1].to_numpy()),
    }
    _assert_png(plot_curve_snapshot(curves, tmp_path / "snap.png"))


def test_residual_hist_handles_outliers(tmp_path) -> None:
    """含 400bp 离群值时应裁剪横轴，而不是把主体压成一根线。"""
    rng = np.random.default_rng(0)
    resid = rng.normal(0, 3, 5000)
    resid = np.concatenate([resid, [400.0, -380.0]])
    _assert_png(plot_residual_hist(resid, tmp_path / "hist.png"))


def test_residual_hist_rejects_empty(tmp_path) -> None:
    with pytest.raises(ValueError, match="残差为空"):
        plot_residual_hist(np.array([np.nan, np.nan]), tmp_path / "x.png")


def test_residual_by_tenor_numeric(tmp_path) -> None:
    df = pd.DataFrame(
        {
            "tenor": np.repeat([1.0, 5.0, 10.0], 50),
            "residual_bp": np.random.default_rng(1).normal(0, 2, 150),
        }
    )
    _assert_png(plot_residual_by_tenor(df, tmp_path / "bt.png"))


def test_residual_by_tenor_string_labels(tmp_path) -> None:
    """tenor 用字符串标签（"3M"/"10Y"）时也不能崩。"""
    df = pd.DataFrame(
        {
            "tenor": np.repeat(["3M", "10Y"], 40),
            "residual_bp": np.random.default_rng(2).normal(0, 2, 80),
        }
    )
    _assert_png(plot_residual_by_tenor(df, tmp_path / "bt2.png"))


def test_param_series(tmp_path, panel) -> None:
    params = pd.DataFrame(
        {
            "b0": 2.5 + np.linspace(0, 0.3, len(panel)),
            "b1": -1.5 + np.linspace(0, 0.2, len(panel)),
            "b2": -0.8 + np.linspace(0, 0.1, len(panel)),
            "tau1": np.full(len(panel), 1.5),
            "rmse_bp": np.full(len(panel), 2.0),
        },
        index=panel.index,
    )
    _assert_png(plot_param_series(params, tmp_path / "params.png"))


def test_param_series_rejects_missing_columns(tmp_path) -> None:
    with pytest.raises(ValueError, match="缺少可用列"):
        plot_param_series(pd.DataFrame({"foo": [1, 2]}), tmp_path / "x.png")


def test_plot_nav_with_drawdown(tmp_path, panel) -> None:
    nav = pd.Series(np.linspace(1.0, 1.25, len(panel)), index=panel.index)
    bench = pd.Series(np.linspace(1.0, 1.10, len(panel)), index=panel.index)
    _assert_png(plot_nav(nav, tmp_path / "nav.png", benchmark=bench))
