"""可观测曲线因子（推荐口径）的单元测试。

R02 的结论：NS/NSS 模型参数存在识别性问题（b1/τ 会换解），不能直接当因子；
:func:`ficc.curve.factors.observable_factors` 用观测收益率构造 level/slope/curvature，
无识别性问题。本文件验证：① 定义数值正确；② 缺失期限不臆造；③ 跳变敏感性
统计能定位最大跳变日；④ 真实数据上 2023-10-12 这类"参数换解日"因子本身是平稳的。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from ficc.curve.factors import factor_jump_sensitivity, observable_factors

TENORS = [0.25, 0.5, 1.0, 2.0, 3.0, 5.0, 7.0, 10.0, 15.0, 20.0, 30.0]


def _panel(rows: list[dict[float, float]], dates: list[str]) -> pd.DataFrame:
    frame = pd.DataFrame(rows, index=pd.to_datetime(dates))
    frame.columns = pd.Index([float(c) for c in frame.columns], name="tenor")
    return frame.sort_index(axis=1)


def _flat_row(**overrides: float) -> dict[float, float]:
    """构造一条给定 level 的平坦曲线（默认所有期限同一收益率）。"""
    base = overrides.pop("base", 2.50)
    row = {float(t): float(base) for t in TENORS}
    row.update({float(k.replace("y", "")): v for k, v in overrides.items()})
    return row


def test_observable_factors_known_values() -> None:
    """已知曲线的 level/slope/curvature 数值必须可手算复现。"""
    row = _flat_row(base=2.0)
    row.update({10.0: 3.0, 15.0: 3.2, 20.0: 3.4, 30.0: 3.6, 2.0: 2.2, 5.0: 2.6})
    panel = _panel([row], ["2024-06-28"])
    factors = observable_factors(panel)
    assert list(factors.columns) == ["level", "slope", "curvature"]
    assert factors.index.equals(panel.index)
    # level = mean(3.0, 3.2, 3.4, 3.6) = 3.3
    assert factors.loc["2024-06-28", "level"] == pytest.approx(3.3, abs=1e-12)
    # slope = 10Y - 2Y = 3.0 - 2.2 = 0.8
    assert factors.loc["2024-06-28", "slope"] == pytest.approx(0.8, abs=1e-12)
    # curvature = 2*5Y - 2Y - 10Y = 2*2.6 - 2.2 - 3.0 = 0.0
    assert factors.loc["2024-06-28", "curvature"] == pytest.approx(0.0, abs=1e-12)


def test_observable_factors_missing_tenor_is_nan() -> None:
    """缺 2Y 时 slope/curvature 必须是 NaN（不插值、不臆造）；level 不受影响。"""
    row = _flat_row(base=2.5)
    row.pop(2.0)
    panel = _panel([row], ["2017-06-01"])
    factors = observable_factors(panel)
    assert np.isnan(factors.loc["2017-06-01", "slope"])
    assert np.isnan(factors.loc["2017-06-01", "curvature"])
    assert factors.loc["2017-06-01", "level"] == pytest.approx(2.5, abs=1e-12)


def test_factor_jump_sensitivity_locates_jump() -> None:
    """跳变敏感性：构造一天 level +100bp 的跳变，必须被统计定位。"""
    rows = [_flat_row(base=2.5) for _ in range(4)]
    rows[2][10.0] = 3.5  # 第 3 天 10Y 跳 100bp
    panel = _panel(rows, ["2024-06-03", "2024-06-04", "2024-06-05", "2024-06-06"])
    factors = observable_factors(panel)
    sensitivity = factor_jump_sensitivity(factors, threshold_bp=25.0)
    assert set(sensitivity.index) == {"level", "slope", "curvature"}
    for col in (
        "n",
        "mean_abs_bp",
        "std_bp",
        "p95_abs_bp",
        "p99_abs_bp",
        "max_abs_bp",
        "max_date",
        "n_above_threshold",
    ):
        assert col in sensitivity.columns
    level = sensitivity.loc["level"]
    assert level["max_abs_bp"] == pytest.approx(25.0, abs=1e-9)  # 100bp 的 1/4
    assert pd.Timestamp(level["max_date"]) == pd.Timestamp("2024-06-05")
    assert level["n_above_threshold"] == 0
    slope = sensitivity.loc["slope"]
    assert slope["max_abs_bp"] == pytest.approx(100.0, abs=1e-9)
    # 第 3 天跳上去、第 4 天又跳回来，故有 2 次 100bp 的日变动
    assert slope["n_above_threshold"] == 2


def test_factor_jump_sensitivity_errors() -> None:
    with pytest.raises(TypeError, match="DataFrame"):
        factor_jump_sensitivity([1, 2, 3])  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="为空"):
        factor_jump_sensitivity(pd.DataFrame())


def test_observable_factors_real_data_immune_to_parameter_switching(real_spike_csv) -> None:
    """真实数据：2023-10-11→12 参数换解（评审：b1 跳 147bp）时，可观测因子平稳。

    这两天各节点收益率最大变动仅约 0.04 个百分点，因此 level/slope/curvature 的
    日变动都应很小（< 5bp）——这正是推荐"可观测因子"而不是"模型参数"的原因。
    """
    raw = pd.read_csv(real_spike_csv)
    want = np.asarray(TENORS)
    frame = raw[raw["term_years"].isin(want)][["date", "term_years", "yield_pct"]]
    frame = frame.drop_duplicates(subset=["date", "term_years"])
    panel = frame.pivot(index="date", columns="term_years", values="yield_pct").sort_index()
    panel.index = pd.to_datetime(panel.index)
    panel.columns = pd.Index(panel.columns.to_numpy(dtype=float), name="tenor")
    panel = panel.reindex(columns=list(want)).sort_index()

    factors = observable_factors(panel)
    before = factors.loc[pd.Timestamp("2023-10-11")]
    after = factors.loc[pd.Timestamp("2023-10-12")]
    changes = (after - before).abs() * 100.0  # bp
    assert changes.max() < 5.0, f"可观测因子日变动应很小，实际 {changes.to_dict()}"

    sensitivity = factor_jump_sensitivity(factors)
    # 可观测因子的 p99 日变动在个位数到几十 bp 量级（远小于参数跳变的百 bp）
    assert sensitivity["p99_abs_bp"].max() < 120.0
