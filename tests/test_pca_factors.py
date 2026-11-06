"""PCA 与因子正交性的单元测试。

覆盖：
1. 合成三因子曲线（level/slope/curvature 为已知方向）→ PCA 能解析恢复、
   可观测因子与 PC1–3 的交叉校验接近 1（验证机制本身正确）；
2. PC 符号约定（PC1 载荷和为正、PC2 短端为负、PC3 腹部为正）；
3. **PCA 样本是 Δy（bp）而非水平值**：分数 @ 载荷ᵀ + 均值 能重建 Δy；
4. 去均值（默认）与"含均值"对照版的差异；
5. 滚动窗口：走前打分（不含当日）、载荷时间稳定性；
6. 正交化后水平/斜率/曲率相关≈0；
7. 真实数据：机制跑通、如实报告 level↔PC1 未达 0.95 及原因（长端 level 与
   短端主导的 PC1 定义错位；诊断性 level_all 相关更高）。
"""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from ficc.curve.factors import (
    factor_correlation,
    factor_crosscheck,
    loading_stability,
    observable_factors,
    orthogonalize,
    pca_factors,
)

TENORS = np.asarray([0.25, 0.5, 1.0, 2.0, 3.0, 5.0, 7.0, 10.0, 15.0, 20.0, 30.0])


# ---------------------------------------------------------------------------
# 合成数据：观测因子方向由最小范数基构造，保证 A·Δy = 已知因子变化
# ---------------------------------------------------------------------------
def _observable_direction_basis(tenors: np.ndarray = TENORS) -> np.ndarray:
    """构造 (n,3) 基矩阵 V，使 ``A·(V f) = f``（A 为 level/slope/curvature 定义矩阵）。"""
    n = tenors.size

    def idx(t: float) -> int:
        return int(np.argmin(np.abs(tenors - t)))

    a = np.zeros((3, n))
    a[0, [idx(t) for t in (10.0, 15.0, 20.0, 30.0)]] = 0.25  # level
    a[1, idx(10.0)] = 1.0  # slope = 10Y - 2Y
    a[1, idx(2.0)] = -1.0
    a[2, idx(5.0)] = 2.0  # curvature = 2*5Y - 2Y - 10Y
    a[2, idx(2.0)] = -1.0
    a[2, idx(10.0)] = -1.0
    return a.T @ np.linalg.inv(a @ a.T)


def _synthetic_panel(n_days: int = 400, seed: int = 42) -> pd.DataFrame:
    """合成收益率面板：Δy = V·f + 小噪声，三因子方差严格分层（25:4:1）。"""
    rng = np.random.default_rng(seed)
    v = _observable_direction_basis()
    # 因子步长以"百分点"计：level 0.003pp = 0.3bp，slope 0.08bp，curvature 0.03bp；
    # 方差比 25:4:1 保证 PCA 主成分顺序与可观测因子一一对应。
    factor_steps = np.column_stack(
        [
            rng.normal(0.0, 0.003, n_days),  # level（bp: 0.3）
            rng.normal(0.0, 0.0008, n_days),  # slope（bp: 0.08）
            rng.normal(0.0, 0.0003, n_days),  # curvature（bp: 0.03）
        ]
    )
    changes = factor_steps @ v.T + rng.normal(0.0, 1e-5, (n_days, TENORS.size))
    base = 1.5 + 0.03 * TENORS
    levels = base + np.cumsum(changes, axis=0)
    dates = pd.bdate_range("2020-01-01", periods=n_days)
    panel = pd.DataFrame(levels, index=dates, columns=pd.Index(TENORS, name="tenor"))
    return panel


def _real_panel(path) -> pd.DataFrame:
    raw = pd.read_csv(path)
    frame = raw[raw["term_years"].isin(TENORS)][["date", "term_years", "yield_pct"]]
    frame = frame.drop_duplicates(subset=["date", "term_years"])
    panel = frame.pivot(index="date", columns="term_years", values="yield_pct").sort_index()
    panel.index = pd.to_datetime(panel.index)
    panel.columns = pd.Index(panel.columns.to_numpy(dtype=float), name="tenor")
    return panel.reindex(columns=list(TENORS)).sort_index()


# ---------------------------------------------------------------------------
# 1. 合成三因子：解析恢复 + 交叉校验
# ---------------------------------------------------------------------------
def test_pca_recovers_three_known_factors() -> None:
    panel = _synthetic_panel()
    pca = pca_factors(panel)
    assert pca.loadings.shape == (TENORS.size, 3)
    assert pca.scores.shape[1] == 3
    assert pca.explained_variance_ratio[0] > 0.95
    assert pca.explained_variance_ratio.sum() > 0.9999

    factors = observable_factors(panel)
    crosscheck = factor_crosscheck(factors, pca)
    assert crosscheck.loc["level", "corr_pc1"] > 0.99
    assert crosscheck.loc["slope", "corr_pc2"] > 0.98
    assert crosscheck.loc["curvature", "corr_pc3"] > 0.97
    # 方向一致 + R² 接近 1 + 目标全达成
    assert crosscheck["sign_consistent"].all()
    assert (crosscheck["r2_pc1_3"] > 0.99).all()
    assert crosscheck["meets_target"].all()


def test_pca_sign_conventions() -> None:
    """PC 符号约定：PC1 载荷和 >0；PC2 斜率坐标 >0；PC3 曲率坐标 >0。

    用"斜率/曲率坐标"而不是单个期限载荷定方向：单期限载荷可能接近 0，
    会因噪声在滚动窗口中随机翻符号（这正是本约定要避免的）。
    """
    pca = pca_factors(_synthetic_panel())
    tenors = pca.loadings.index.to_numpy(dtype=float)

    def loading_at(tenor: float, component: str) -> float:
        index = int(np.argmin(np.abs(tenors - tenor)))
        return float(pca.loadings[component].iloc[index])

    assert pca.loadings["PC1"].sum() > 0
    assert loading_at(10.0, "PC2") - loading_at(2.0, "PC2") > 0
    assert 2.0 * loading_at(5.0, "PC3") - loading_at(2.0, "PC3") - loading_at(10.0, "PC3") > 0


# ---------------------------------------------------------------------------
# 2. 样本是 Δy（bp）：重建 + 含均值对照
# ---------------------------------------------------------------------------
def test_pca_sample_is_delta_y_in_bp() -> None:
    panel = _synthetic_panel()
    pca = pca_factors(panel)
    changes = (panel.diff() * 100.0).dropna(how="any")
    assert pca.n_obs == changes.shape[0]
    np.testing.assert_allclose(pca.mean.to_numpy(), changes.mean().to_numpy(), atol=1e-12)
    reconstruction = pca.scores.to_numpy(dtype=float) @ pca.loadings.to_numpy(dtype=float).T
    reconstruction = reconstruction + pca.mean.to_numpy()
    # 前 3 个 PC 解释 99.99%+ 方差，重建误差应在 0.01bp 量级
    np.testing.assert_allclose(reconstruction, changes.to_numpy(), atol=0.01)
    assert pca.center is True
    assert pca.window is None


def test_pca_centering_control_version() -> None:
    """含均值版本（center=False）的 PC1 会吸收样本期漂移。"""
    panel = _synthetic_panel(n_days=300)
    # 人为加大同向漂移（所有期限每天 +1bp）
    drifted = panel.copy()
    drift = np.arange(len(panel), dtype=float)[:, None] * 1.0 / 100.0
    drifted = drifted + drift
    centered = pca_factors(drifted, center=True)
    uncentered = pca_factors(drifted, center=False)
    assert centered.center is True and uncentered.center is False
    ones = np.ones(TENORS.size)
    ones /= np.linalg.norm(ones)
    pc1_uncentered = uncentered.loadings["PC1"].to_numpy(dtype=float)
    pc1_uncentered /= np.linalg.norm(pc1_uncentered)
    # 含均值版本 PC1 与"全期限同向"方向高度一致
    assert abs(float(pc1_uncentered @ ones)) > 0.99


# ---------------------------------------------------------------------------
# 3. 滚动窗口与载荷稳定性
# ---------------------------------------------------------------------------
def test_rolling_pca_is_out_of_sample_and_stable() -> None:
    panel = _synthetic_panel(n_days=400)
    window = 80
    pca = pca_factors(panel, window=window)
    changes = (panel.diff() * 100.0).dropna(how="any")
    assert pca.window == window
    assert pca.n_obs == changes.shape[0] - window
    # 走前打分：第一个分数日 = 样本第 window 个观测日
    assert pca.scores.index[0] == changes.index[window]
    assert pca.loadings_history is not None
    assert pca.rolling_evr is not None
    assert pca.loadings_history.shape[0] == pca.n_obs
    assert isinstance(pca.loadings_history.columns, pd.MultiIndex)
    stability = loading_stability(pca)
    for column in (
        "evr_mean",
        "evr_std",
        "loading_std_mean",
        "corr_with_fullsample_mean",
        "corr_with_fullsample_min",
        "n_windows",
    ):
        assert column in stability.columns
    # 合成数据的因子结构稳定：滚动载荷与全样本载荷高度相关
    assert (stability["corr_with_fullsample_mean"] > 0.95).all()
    # 滚动 EVR 与全样本口径接近
    np.testing.assert_allclose(
        pca.explained_variance_ratio, pca.rolling_evr.mean().to_numpy(), atol=1e-8
    )


# ---------------------------------------------------------------------------
# 4. 正交化
# ---------------------------------------------------------------------------
def test_orthogonalize_removes_cross_correlation() -> None:
    panel = _synthetic_panel(n_days=250)
    factors = observable_factors(panel)
    orthogonal = orthogonalize(factors)
    assert list(orthogonal.columns) == ["level", "slope_orth", "curvature_orth"]
    correlation = factor_correlation(orthogonal, space="levels")
    off_diagonal = correlation.to_numpy(dtype=float) - np.eye(3)
    assert np.max(np.abs(off_diagonal)) < 1e-10
    # 残差定义核对：slope_orth 与 slope 对 level 的 OLS 残差一致
    level = factors["level"].to_numpy(dtype=float)
    slope = factors["slope"].to_numpy(dtype=float)
    design = np.column_stack([np.ones(level.size), level])
    beta, *_ = np.linalg.lstsq(design, slope, rcond=None)
    expected = slope - design @ beta
    np.testing.assert_allclose(orthogonal["slope_orth"].to_numpy(), expected, atol=1e-10)
    # 注：在水平上正交化**不保证**日变化也正交（变化口径需 space="changes" 单独处理，
    # 见 test_orthogonalize_changes_space）；这里只要求水平口径的相关系数≈0。


def test_orthogonalize_changes_space() -> None:
    factors = observable_factors(_synthetic_panel(n_days=200))
    orthogonal = orthogonalize(factors, space="changes")
    assert orthogonal.attrs["space"] == "changes"
    # space="changes" 返回的残差**已经是变化口径**，所以算相关矩阵时用 levels
    # （若再 diff 一次会变成"变化的变化"）。
    correlation = factor_correlation(orthogonal, space="levels")
    off = correlation.to_numpy(dtype=float) - np.eye(3)
    assert np.max(np.abs(off)) < 1e-10


def test_factor_correlation_matrix_properties() -> None:
    factors = observable_factors(_synthetic_panel(n_days=200))
    correlation = factor_correlation(factors, space="levels")
    values = correlation.to_numpy(dtype=float)
    assert np.allclose(values, values.T)
    assert np.allclose(np.diag(values), 1.0)
    assert np.all(np.abs(values) <= 1.0 + 1e-12)
    with pytest.raises(ValueError, match="space"):
        factor_correlation(factors, space="level")
    with pytest.raises(ValueError, match="为空"):
        factor_correlation(pd.DataFrame())


# ---------------------------------------------------------------------------
# 5. 输入校验
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
    with pytest.raises(ValueError, match="缺少必要列"):
        orthogonalize(pd.DataFrame({"level": [1.0, 2.0]}))
    with pytest.raises(ValueError, match="为空"):
        factor_crosscheck(pd.DataFrame(), pca_factors(panel))


def test_crosscheck_insufficient_overlap() -> None:
    panel = _synthetic_panel(n_days=200)
    pca = pca_factors(panel)
    factors = observable_factors(panel)
    shallow = factors.iloc[:30]
    with pytest.raises(ValueError, match="共同样本"):
        factor_crosscheck(shallow, pca)


# ---------------------------------------------------------------------------
# 6. 真实数据：机制跑通 + 如实报告"目标未达成"及原因
# ---------------------------------------------------------------------------
def test_real_data_crosscheck_reports_honestly(real_spike_csv) -> None:
    panel = _real_panel(real_spike_csv)
    pca = pca_factors(panel)
    factors = observable_factors(panel)
    crosscheck = factor_crosscheck(factors, pca)
    for column in (
        "corr_pc1",
        "corr_pc2",
        "corr_pc3",
        "r2_pc1_3",
        "expected_pc",
        "corr_expected",
        "sign_consistent",
        "meets_target",
    ):
        assert column in crosscheck.columns
    assert np.isfinite(crosscheck["corr_pc1"].to_numpy()).all()
    # meets_target 必须与实际相关系数一致（不允许硬编码"达标"）
    assert bool(crosscheck.loc["level", "meets_target"]) == (
        float(crosscheck.loc["level", "corr_pc1"]) > 0.95
    )
    # 真实数据里 level↔PC1 未达 0.95：长端 level 与短端主导的 PC1 存在定义错位。
    # 诊断性对照（全期限均值）与 PC1 的相关应更高，用来解释这一点。
    level_all = factor_crosscheck(factors.assign(level_all=panel.mean(axis=1)), pca)
    assert abs(level_all.loc["level_all", "corr_pc1"]) > abs(crosscheck.loc["level", "corr_pc1"])
    # winsor 稳健性对照：样本处理与因子变化一致（自动沿用 pca.winsor_bp）
    pca_winsor = pca_factors(panel, winsor_bp=10.0)
    crosscheck_winsor = factor_crosscheck(factors, pca_winsor)
    assert np.isfinite(crosscheck_winsor["corr_pc1"].to_numpy()).all()
    assert pca_winsor.winsor_bp == 10.0
