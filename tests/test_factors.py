"""可观测因子契约与 winsor 预处理（R14/R15/R16）的单元测试。

覆盖：
1. `observable_factors` 固定 4 列（level_all/level_long/slope/curvature）、数值定义；
2. `level_all` 用固定 11 期限，**缺一即 NaN、不按可用列重新归一化**；
3. `level` 是 level_long 的弃用别名（显式开关 + DeprecationWarning）；
4. 定义表/权重矩阵/验收映射（FACTOR_TARGETS）；
5. 跳变敏感性；6. `preprocess_changes` 逐期限截尾 + 固定权重聚合（R16）。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from ficc.curve.factors import (
    DEPRECATED_FACTOR_ALIASES,
    FACTOR_COLUMNS,
    FACTOR_TARGETS,
    evaluate_factor_target,
    factor_changes_from_processed,
    factor_definition_table,
    factor_jump_sensitivity,
    factor_weight_matrix,
    observable_factors,
    preprocess_changes,
)

TENORS = [0.25, 0.5, 1.0, 2.0, 3.0, 5.0, 7.0, 10.0, 15.0, 20.0, 30.0]


def _panel(rows: list[list[float]], dates: list[str]) -> pd.DataFrame:
    frame = pd.DataFrame(rows, index=pd.to_datetime(dates), columns=TENORS)
    frame.columns = pd.Index([float(c) for c in frame.columns], name="tenor")
    return frame


def _row(**overrides: float) -> list[float]:
    base = 2.5
    values = {float(t): base for t in TENORS}
    values.update({float(k.replace("y", "")): v for k, v in overrides.items() if k.startswith("y")})
    return [values[float(t)] for t in TENORS]


def test_factor_columns_are_fixed() -> None:
    frame = observable_factors(_panel([_row()], ["2024-06-28"]))
    assert tuple(frame.columns) == FACTOR_COLUMNS
    assert FACTOR_COLUMNS == ("level_all", "level_long", "slope", "curvature")
    assert frame.attrs["unit"] == "percentage points"


def test_observable_factor_values() -> None:
    row = _row(y10=3.0, y15=3.2, y20=3.4, y30=3.6, y2=2.2, y5=2.6)
    factors = observable_factors(_panel([row], ["2024-06-28"]))
    at = factors.loc["2024-06-28"]
    assert at["level_long"] == pytest.approx((3.0 + 3.2 + 3.4 + 3.6) / 4.0, abs=1e-12)
    assert at["level_all"] == pytest.approx(np.mean(row), abs=1e-12)
    assert at["slope"] == pytest.approx(3.0 - 2.2, abs=1e-12)
    assert at["curvature"] == pytest.approx(2 * 2.6 - 2.2 - 3.0, abs=1e-12)


def test_level_all_requires_all_eleven_tenors_no_renormalization() -> None:
    """缺任一标准期限 → level_all 必须 NaN，而不是按可用列重新归一化。"""
    frame = _panel([_row()], ["2017-06-01"])
    frame[2.0] = np.nan  # 缺 2Y
    factors = observable_factors(frame)
    assert np.isnan(factors.loc["2017-06-01", "level_all"])
    assert np.isnan(factors.loc["2017-06-01", "slope"])
    assert np.isnan(factors.loc["2017-06-01", "curvature"])
    # 长端仍完整 → level_long 可计算
    assert np.isfinite(factors.loc["2017-06-01", "level_long"])
    # 缺 30Y（在长端集合里）→ level_long 也 NaN
    frame2 = _panel([_row()], ["2017-06-01"])
    frame2[30.0] = np.nan
    factors2 = observable_factors(frame2)
    assert np.isnan(factors2.loc["2017-06-01", "level_all"])
    assert np.isnan(factors2.loc["2017-06-01", "level_long"])


def test_level_is_deprecated_alias() -> None:
    assert DEPRECATED_FACTOR_ALIASES == {"level": "level_long"}
    with pytest.warns(DeprecationWarning):
        frame = observable_factors(_panel([_row()], ["2024-06-28"]), include_deprecated_level=True)
    assert "level" in frame.columns
    np.testing.assert_allclose(
        frame["level"].to_numpy(), frame["level_long"].to_numpy(), atol=1e-12
    )


def test_definition_table_and_targets() -> None:
    table = factor_definition_table()
    assert set(FACTOR_COLUMNS).issubset(set(table["factor"]))
    assert bool(table.loc[table["factor"] == "level", "deprecated"].iloc[0]) is True
    for name in FACTOR_COLUMNS:
        assert name in FACTOR_TARGETS
    assert FACTOR_TARGETS["level_all"] == ("PC1", 0.95)
    assert FACTOR_TARGETS["slope"] == ("PC2", 0.50)
    weights = factor_weight_matrix()
    assert tuple(weights.index) == FACTOR_COLUMNS
    # level_all 权重和 = 1；slope/curvature 权重和 = 0
    assert weights.loc["level_all"].sum() == pytest.approx(1.0)
    assert weights.loc["slope"].sum() == pytest.approx(0.0)
    assert weights.loc["curvature"].sum() == pytest.approx(0.0)


def test_evaluate_factor_target_thresholds() -> None:
    assert evaluate_factor_target("level_all", 0.94)[2] is False
    assert evaluate_factor_target("level_all", 0.96)[2] is True
    assert evaluate_factor_target("slope", 0.49)[2] is False
    assert evaluate_factor_target("slope", 0.51)[2] is True
    assert evaluate_factor_target("some_unknown", 0.99) == (None, None, None)
    assert evaluate_factor_target("level_all", float("nan"))[2] is None


def test_factor_jump_sensitivity_locates_jump() -> None:
    rows = [_row() for _ in range(4)]
    rows[2][TENORS.index(10.0)] = 3.5  # 第 3 天 10Y 跳 100bp
    factors = observable_factors(
        _panel(rows, ["2024-06-03", "2024-06-04", "2024-06-05", "2024-06-06"])
    )
    sensitivity = factor_jump_sensitivity(factors, threshold_bp=25.0)
    assert set(FACTOR_COLUMNS).issubset(set(sensitivity.index))
    assert sensitivity.loc["slope", "max_abs_bp"] == pytest.approx(100.0, abs=1e-9)
    assert pd.Timestamp(sensitivity.loc["slope", "max_date"]) == pd.Timestamp("2024-06-05")
    assert sensitivity.loc["slope", "n_above_threshold"] == 2
    with pytest.raises(TypeError):
        factor_jump_sensitivity([1, 2, 3])  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="为空"):
        factor_jump_sensitivity(pd.DataFrame())


def test_preprocess_changes_clips_per_tenor_before_aggregation() -> None:
    """R16 反例：clip(mean) != mean(clip)；聚合必须走同一截尾矩阵。"""
    rng = np.random.default_rng(7)
    rows = [(_row())]
    for _ in range(120):
        step = rng.normal(0.0, 0.05, len(TENORS))
        step[TENORS.index(30.0)] = 0.9  # 30Y 单日 +90bp（异常）
        rows.append([rows[-1][i] + step[i] for i in range(len(TENORS))])
    dates = pd.bdate_range("2020-01-01", periods=len(rows)).strftime("%Y-%m-%d").tolist()
    panel = _panel(rows, dates)
    processed = preprocess_changes(panel, winsor_bp=10.0)
    raw = preprocess_changes(panel, winsor_bp=None)
    # 逐期限截尾：单期异常被压到 10bp
    assert processed[30.0].abs().max() == pytest.approx(10.0)
    # clip(mean) != mean(clip)：用 level_all 做反例
    raw_level_all = raw.mean(axis=1)
    clipped_mean = raw_level_all.clip(-10.0, 10.0)
    mean_after_clip = processed.mean(axis=1)
    differ = (clipped_mean - mean_after_clip).abs().max()
    assert differ > 1e-9, "构造的样本应满足 clip(mean) != mean(clip)"
    # 固定权重聚合
    factor_changes = factor_changes_from_processed(processed)
    manual = processed.to_numpy() @ factor_weight_matrix().to_numpy().T
    np.testing.assert_allclose(factor_changes.to_numpy(), manual, atol=1e-12)
    with pytest.raises(ValueError, match="缺少标准期限"):
        factor_changes_from_processed(processed.drop(columns=[10.0]))
    with pytest.raises(ValueError, match="winsor_bp"):
        preprocess_changes(panel, winsor_bp=-1.0)
    with pytest.raises(ValueError, match="样本不足"):
        preprocess_changes(panel.iloc[:5])


def test_real_data_factor_definitions_and_immunity(real_spike_csv) -> None:
    """真实数据：2023-10-12（参数换解日）可观测因子日变动应很小（<5bp）。"""
    raw = pd.read_csv(real_spike_csv)
    frame = raw[raw["term_years"].isin(TENORS)][["date", "term_years", "yield_pct"]]
    frame = frame.drop_duplicates(subset=["date", "term_years"])
    panel = frame.pivot(index="date", columns="term_years", values="yield_pct").sort_index()
    panel.index = pd.to_datetime(panel.index)
    panel.columns = pd.Index(panel.columns.to_numpy(dtype=float), name="tenor")
    factors = observable_factors(panel)
    changes = (
        factors.loc[pd.Timestamp("2023-10-12")] - factors.loc[pd.Timestamp("2023-10-11")]
    ).abs() * 100.0
    assert changes.max() < 5.0, f"可观测因子日变动应很小，实际 {changes.to_dict()}"
    sensitivity = factor_jump_sensitivity(factors)
    assert sensitivity["p99_abs_bp"].max() < 120.0
