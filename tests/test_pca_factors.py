"""PCA、交叉校验与正交化（R14/R15/R16/R21）的单元测试。

覆盖：
1. 合成三因子曲线 → PCA 解析恢复；样本是 Δy(bp)，重建/均值/处理矩阵契约；
2. R15 显式验收映射：0.94→False、0.96→True、未知因子→None；
3. R16 共享预处理：clip(mean) != mean(clip) 反例，交叉校验必须走 W·clip(Δy)；
4. R21 训练窗拟合/冻结 transform/inverse、前视不变性；
5. 真实数据交叉校验数值（评审复核值，作为口径回归）。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from ficc.curve.factors import (
    FACTOR_COLUMNS,
    FACTOR_TARGETS,
    factor_changes_from_processed,
    factor_correlation,
    factor_crosscheck,
    factor_weight_matrix,
    fit_orthogonalizer,
    loading_stability,
    observable_factors,
    orthogonalize,
    pca_factors,
    preprocess_changes,
)

TENORS = np.asarray([0.25, 0.5, 1.0, 2.0, 3.0, 5.0, 7.0, 10.0, 15.0, 20.0, 30.0])


# ---------------------------------------------------------------------------
# 合成数据：直接指定 level_all / slope / curvature 的变化
# ---------------------------------------------------------------------------
def _synthetic_panel(n_days: int = 400, seed: int = 42) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    weights = factor_weight_matrix()
    a = weights.loc[["level_all", "slope", "curvature"]].to_numpy(dtype=float)  # 3×11
    v = a.T @ np.linalg.inv(a @ a.T)  # 11×3，A V = I
    factor_steps = np.column_stack(
        [
            rng.normal(0.0, 0.003, n_days),  # level_all 0.3bp
            rng.normal(0.0, 0.0008, n_days),  # slope 0.08bp
            rng.normal(0.0, 0.0003, n_days),  # curvature 0.03bp
        ]
    )
    changes = factor_steps @ v.T + rng.normal(0.0, 1e-5, (n_days, TENORS.size))
    base = 1.5 + 0.03 * TENORS
    levels = base + np.cumsum(changes, axis=0)
    dates = pd.bdate_range("2020-01-01", periods=n_days)
    return pd.DataFrame(levels, index=dates, columns=pd.Index(TENORS, name="tenor"))


def _real_panel(path) -> pd.DataFrame:
    raw = pd.read_csv(path)
    frame = raw[raw["term_years"].isin(TENORS)][["date", "term_years", "yield_pct"]]
    frame = frame.drop_duplicates(subset=["date", "term_years"])
    panel = frame.pivot(index="date", columns="term_years", values="yield_pct").sort_index()
    panel.index = pd.to_datetime(panel.index)
    panel.columns = pd.Index(panel.columns.to_numpy(dtype=float), name="tenor")
    return panel.reindex(columns=list(TENORS)).sort_index()


# ---------------------------------------------------------------------------
# 1. 合成三因子：PCA 恢复 + 样本口径
# ---------------------------------------------------------------------------
def test_pca_recovers_three_known_factors() -> None:
    panel = _synthetic_panel()
    pca = pca_factors(panel)
    assert pca.loadings.shape == (TENORS.size, 3)
    assert pca.explained_variance_ratio[0] > 0.95
    assert pca.explained_variance_ratio.sum() > 0.9999
    assert pca.processed_changes is not None
    factors = observable_factors(panel)
    crosscheck = factor_crosscheck(factors, pca)
    assert crosscheck.loc["level_all", "corr_pc1"] > 0.99
    assert crosscheck.loc["slope", "corr_pc2"] > 0.98
    assert crosscheck.loc["curvature", "corr_pc3"] > 0.97
    for name in FACTOR_COLUMNS:
        target_pc, threshold, meets = (
            FACTOR_TARGETS[name][0],
            FACTOR_TARGETS[name][1],
            crosscheck.loc[name, "meets_target"],
        )
        assert crosscheck.loc[name, "target_pc"] == target_pc
        assert crosscheck.loc[name, "target_threshold"] == threshold
        assert meets is not None
        assert bool(meets) == (float(crosscheck.loc[name, "corr_target"]) >= threshold)


def test_pca_sample_is_delta_y_and_contract() -> None:
    panel = _synthetic_panel()
    pca = pca_factors(panel)
    changes_bp = (panel.diff() * 100.0).dropna(how="any")
    assert pca.n_obs == changes_bp.shape[0]
    np.testing.assert_allclose(pca.mean.to_numpy(), changes_bp.mean().to_numpy(), atol=1e-12)
    reconstruction = pca.scores.to_numpy() @ pca.loadings.to_numpy().T + pca.mean.to_numpy()
    np.testing.assert_allclose(reconstruction, changes_bp.to_numpy(), atol=0.01)
    assert pca.center is True and pca.window is None
    assert pca.processed_changes is not None
    assert pca.processed_changes.attrs["unit"] == "bp"


def test_pca_winsor_is_per_tenor_and_shared() -> None:
    panel = _synthetic_panel(n_days=200)
    panel.iloc[50, TENORS.tolist().index(30.0)] += 1.0  # 30Y 单日 +100bp 异常
    pca = pca_factors(panel, winsor_bp=10.0)
    processed = pca.processed_changes
    assert processed is not None
    assert pca.winsor_bp == 10.0
    assert processed.abs().to_numpy().max() <= 10.0 + 1e-12
    raw = preprocess_changes(panel, winsor_bp=None)
    assert np.allclose(processed.to_numpy(), raw.clip(-10.0, 10.0).to_numpy())


def test_pca_rolling_is_out_of_sample_and_stable() -> None:
    panel = _synthetic_panel(n_days=400)
    window = 80
    pca = pca_factors(panel, window=window)
    changes = (panel.diff() * 100.0).dropna(how="any")
    assert pca.window == window
    assert pca.n_obs == changes.shape[0] - window
    assert pca.scores.index[0] == changes.index[window]
    assert pca.loadings_history is not None and pca.rolling_evr is not None
    stability = loading_stability(pca)
    # 余弦相似度对"水平型"PC 稳定；Pearson 在载荷跨期限方差极小时不可用
    assert (stability["cosine_with_fullsample_mean"] > 0.99).all()


# ---------------------------------------------------------------------------
# 2. R15 验收映射（含 unknown）
# ---------------------------------------------------------------------------
def test_crosscheck_unknown_factor_has_no_verdict() -> None:
    panel = _synthetic_panel(n_days=200)
    pca = pca_factors(panel)
    factors = observable_factors(panel)
    extended = factors.copy()
    extended["my_unknown"] = extended["slope"] * 0.5 + 1.0
    crosscheck = factor_crosscheck(extended, pca)
    row = crosscheck.loc["my_unknown"]
    assert row["target_pc"] is None or (
        isinstance(row["target_pc"], float) and np.isnan(row["target_pc"])
    )
    assert row["meets_target"] is None
    assert np.isfinite(row["exploratory_corr"])
    assert row["exploratory_pc"] in {"PC1", "PC2", "PC3"}
    # 已知因子不得是 exploratory 行
    assert crosscheck.loc["level_all", "exploratory_pc"] is None
    for name in FACTOR_COLUMNS:
        assert crosscheck.loc[name, "meets_target"] is not None


def test_crosscheck_explicit_verdicts() -> None:
    panel = _synthetic_panel(n_days=200)
    pca = pca_factors(panel)
    factors = observable_factors(panel)
    crosscheck = factor_crosscheck(factors, pca)
    for name in FACTOR_COLUMNS:
        target_pc, threshold = FACTOR_TARGETS[name]
        corr = float(crosscheck.loc[name, f"corr_{target_pc.lower()}"])
        assert crosscheck.loc[name, "target_pc"] == target_pc
        assert bool(crosscheck.loc[name, "meets_target"]) == (corr >= threshold)


# ---------------------------------------------------------------------------
# 3. R16 共享 winsor 路径（clip 与聚合不可交换）
# ---------------------------------------------------------------------------
def test_crosscheck_uses_shared_processed_changes() -> None:
    panel = _synthetic_panel(n_days=200)
    panel.iloc[30, TENORS.tolist().index(30.0)] += 0.8  # 异常
    pca = pca_factors(panel, winsor_bp=10.0)
    factors = observable_factors(panel)
    crosscheck = factor_crosscheck(factors, pca)
    assert pca.processed_changes is not None
    processed = pca.processed_changes
    # 手工按同一路径复算，必须与 crosscheck 完全一致
    manual_changes = factor_changes_from_processed(processed)
    scores = pca.scores
    idx = manual_changes.index.intersection(scores.index)
    for name in FACTOR_COLUMNS:
        target_pc = FACTOR_TARGETS[name][0]
        expected = float(
            np.corrcoef(manual_changes.loc[idx, name], scores.loc[idx, target_pc])[0, 1]
        )
        assert crosscheck.loc[name, "corr_target"] == pytest.approx(expected, abs=1e-12)
    # 原始因子变化对照是另一种口径：必须被显式标注且可以不同
    raw_crosscheck = factor_crosscheck(factors, pca, use_raw_factor_changes=True)
    assert "raw factor changes" in str(raw_crosscheck.attrs["sample_note"])
    manual_raw = factors.diff() * 100.0
    expected_raw = float(
        np.corrcoef(manual_raw.loc[idx, "level_all"], scores.loc[idx, "PC1"])[0, 1]
    )
    assert raw_crosscheck.loc["level_all", "corr_target"] == pytest.approx(expected_raw, abs=1e-12)
    assert raw_crosscheck.loc["level_all", "corr_target"] != pytest.approx(
        crosscheck.loc["level_all", "corr_target"], abs=1e-9
    )


# ---------------------------------------------------------------------------
# 4. R21 正交化：训练窗拟合 / 冻结应用 / inverse
# ---------------------------------------------------------------------------
def test_orthogonalizer_forward_invariance_and_inverse() -> None:
    factors = observable_factors(_synthetic_panel(n_days=400))
    window = 250
    orth = fit_orthogonalizer(factors, window=window)
    assert orth.fit_scope == "window" and orth.fit_window == window
    assert orth.n_fit <= window
    # 前视不变性：只改训练窗之后的数据，系数完全不变
    modified = factors.copy()
    modified.iloc[window:] = modified.iloc[window:] * 1.7
    orth_modified = fit_orthogonalizer(modified, window=window)
    np.testing.assert_allclose(orth.slope_coef, orth_modified.slope_coef, atol=1e-12)
    np.testing.assert_allclose(orth.curvature_coef, orth_modified.curvature_coef, atol=1e-12)
    # 单行 transform 与整表 transform 一致（只看冻结系数）
    transformed = orth.transform(factors)
    single = orth.transform(factors.iloc[[window + 10]])
    np.testing.assert_allclose(
        single.to_numpy(dtype=float),
        transformed.iloc[[window + 10]].to_numpy(dtype=float),
        atol=1e-12,
    )
    # inverse 还原
    inverse = orth.inverse(transformed)
    for column in FACTOR_COLUMNS:
        np.testing.assert_allclose(
            inverse[column].to_numpy(), factors[column].to_numpy(), atol=1e-10
        )
    assert "fit_scope" in orth.describe()


def test_orthogonalizer_default_scope_guard_and_spaces() -> None:
    factors = observable_factors(_synthetic_panel(n_days=300))
    with pytest.raises(ValueError, match="fit_scope"):
        fit_orthogonalizer(factors)
    full = fit_orthogonalizer(factors, fit_scope="full")
    assert full.fit_scope == "full"
    orth = fit_orthogonalizer(factors, window=200)
    transformed = orth.transform(factors)
    # 训练窗内**目标对**的水平相关≈0（代数恒等）；level_all 与 level_long
    # 天然高度相关，不属于被正交化的目标对。
    in_sample = transformed.iloc[:200]
    corr = factor_correlation(in_sample, space="levels")
    labels = list(corr.columns)
    i_level = labels.index("level_all")
    i_slope = labels.index("slope_orth")
    i_curv = labels.index("curvature_orth")
    target_off = [
        abs(float(corr.iloc[i_level, i_slope])),
        abs(float(corr.iloc[i_level, i_curv])),
        abs(float(corr.iloc[i_slope, i_curv])),
    ]
    assert max(target_off) < 1e-10
    # 日变化空间不保证为 0 —— 报告必须同时给出这组数字（R21）
    corr_changes = factor_correlation(transformed, space="changes").to_numpy()
    changes_off = np.max(np.abs(corr_changes - np.eye(corr_changes.shape[0])))
    assert changes_off > 0.0
    # 旧接口保留（默认全样本，需在报告中标注样本内诊断）
    legacy = orthogonalize(factors)
    assert legacy.attrs["orthogonalizer"]["fit_scope"] == "full"


def test_orthogonalizer_validation() -> None:
    factors = observable_factors(_synthetic_panel(n_days=100))
    with pytest.raises(ValueError, match="为空"):
        fit_orthogonalizer(pd.DataFrame())
    with pytest.raises(ValueError, match="window"):
        fit_orthogonalizer(factors, window=10)
    with pytest.raises(ValueError, match="缺少 level"):
        fit_orthogonalizer(pd.DataFrame({"slope": [1.0], "curvature": [2.0]}), fit_scope="full")
    orth = fit_orthogonalizer(factors, window=60)
    with pytest.raises(ValueError, match="缺少列"):
        orth.inverse(pd.DataFrame({"level_all": [1.0]}))


# ---------------------------------------------------------------------------
# 5. 输入校验 + 真实数据口径回归
# ---------------------------------------------------------------------------
def test_pca_validation_errors() -> None:
    with pytest.raises(TypeError, match="DataFrame"):
        pca_factors([1, 2, 3])  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="样本不足"):
        pca_factors(_synthetic_panel(n_days=20))
    panel = _synthetic_panel(n_days=200)
    with pytest.raises(ValueError, match="window"):
        pca_factors(panel, window=10)
    with pytest.raises(ValueError, match="winsor_bp"):
        pca_factors(panel, winsor_bp=0.0)
    with pytest.raises(ValueError, match="滚动窗口"):
        loading_stability(pca_factors(panel))
    with pytest.raises(ValueError, match="为空"):
        factor_crosscheck(pd.DataFrame(), pca_factors(panel))


def test_crosscheck_insufficient_overlap() -> None:
    panel = _synthetic_panel(n_days=200)
    # 滚动窗口留很短打分区间 → 与因子变化的共同样本不足
    pca = pca_factors(panel, window=190)
    factors = observable_factors(panel)
    with pytest.raises(ValueError, match="共同样本"):
        factor_crosscheck(factors, pca)


def test_real_data_unified_winsor_numbers(real_spike_csv) -> None:
    """评审复核值（R16 统一口径后）：winsor 10bp。

    level_all↔PC1=0.952613（达标）、level_long↔PC1=0.361593（不达标）、
    slope↔PC2=0.798903（达标）、curvature↔PC3=0.328500（不达标）；
    原始口径 level_all=0.928516（不达标）。
    """
    panel = _real_panel(real_spike_csv)
    factors = observable_factors(panel)
    raw = pca_factors(panel)
    winsor = pca_factors(panel, winsor_bp=10.0)
    raw_cross = factor_crosscheck(factors, raw)
    winsor_cross = factor_crosscheck(factors, winsor)
    assert float(raw_cross.loc["level_all", "corr_pc1"]) == pytest.approx(0.928516, abs=1e-4)
    assert float(winsor_cross.loc["level_all", "corr_pc1"]) == pytest.approx(0.952613, abs=1e-4)
    assert float(winsor_cross.loc["level_long", "corr_pc1"]) == pytest.approx(0.361593, abs=1e-4)
    assert float(winsor_cross.loc["slope", "corr_pc2"]) == pytest.approx(0.798903, abs=1e-4)
    assert float(winsor_cross.loc["curvature", "corr_pc3"]) == pytest.approx(0.328500, abs=1e-4)
    assert bool(winsor_cross.loc["level_all", "meets_target"]) is True
    assert bool(winsor_cross.loc["level_long", "meets_target"]) is False
    assert bool(winsor_cross.loc["slope", "meets_target"]) is True
    assert bool(winsor_cross.loc["curvature", "meets_target"]) is False
    assert bool(raw_cross.loc["level_all", "meets_target"]) is False
