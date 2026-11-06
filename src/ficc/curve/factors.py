"""可观测曲线因子（推荐口径）——没有 NS/NSS 参数识别性问题。

为什么需要这个模块（R02 的结论）
---------------------------------
NS/NSS 的参数在 11 个期限点上存在严重的**识别性问题**：同一组收益率可以有
多组几乎等价的参数。评审复现：2023-10-11 → 10-12 各节点收益率最大只变动
**0.04 个百分点**，但锚定拟合的 ``b1`` 从 ``-2.3959`` 跳到 ``-0.9227``
（约 147bp），``τ1`` 从 0.222 跳到 1.5、``τ2`` 从 0.481 跳到 30。也就是说
**模型参数不能直接当因子用**（锚定只固定了 b0，b1/b2/b3/τ 仍会换解）。

因此项目的**推荐因子口径**是直接由观测收益率构造的三个量（单位：百分点，
日变动 ×100 即 bp）：

- ``level``：10Y/15Y/20Y/30Y 的均值（与锚定 ``b0`` 的定义一致，长端水平）；
- ``slope``：10Y − 2Y 期限利差（曲线陡平）；
- ``curvature``：2×5Y − 2Y − 10Y 蝶式/曲度（中间相对两端的凸起）。

它们只依赖当天的可观测收益率、**没有任何拟合或识别环节**，因此日间连续、
可复现、也不会因为优化器换解而跳变。需要参数化因子时请走因果方案
（冻结训练窗口的 τ，或状态空间/卡尔曼平滑），见 :mod:`ficc.curve.fitting` 的
模块 docstring。

用法
----
>>> from ficc.curve.factors import observable_factors, factor_jump_sensitivity
>>> factors = observable_factors(panel)          # index=date, columns=level/slope/curvature
>>> factor_jump_sensitivity(factors)             # 每个因子的日变动分布与最大跳变日

PCA 与因子正交性
----------------
- :func:`pca_factors`：对**收益率变化 Δy（bp）**做 PCA（``window=None`` 全样本；
  ``window=250`` 走前滚动，避免前视）。默认**去均值**（只保留变化），
  ``center=False`` 提供"含均值"对照版本。
- :func:`factor_crosscheck`：用可观测因子的 Δ 与 PC1–3 分数做相关与回归 R²，
  检验"level↔PC1 / slope↔PC2 / curvature↔PC3"是否成立（不达标如实报告）。
- :func:`orthogonalize`：把 slope 对 level、curvature 对 level+slope 回归取残差，
  得到正交化因子（用于检查"水平/斜率/曲率"是否真的互相独立）。
- :func:`factor_correlation`：因子相关矩阵（levels 或 changes 两种口径）。
- :class:`PCAResult`：loadings（tenor×k）、explained_variance_ratio、scores（date×k）、
  mean、window；滚动口径另带 ``loadings_history`` / ``rolling_evr``。

PC 符号约定（**不是为了好看，而是让方向可比较**）：PC1 载荷和为正；PC2 用
**斜率坐标**（10Y 载荷 − 2Y 载荷）为正定方向；PC3 用**曲率坐标**
（2×5Y − 2Y − 10Y 载荷）为正定方向。``factor_crosscheck`` 仍会如实报告实际
相关系数——若经验数据不符合该方向，结论就是"不符合"。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal

import numpy as np
import pandas as pd

from ficc.curve.fitting import _normalize_panel

__all__ = [
    "PCAResult",
    "factor_correlation",
    "factor_crosscheck",
    "factor_jump_sensitivity",
    "loading_stability",
    "observable_factors",
    "orthogonalize",
    "pca_factors",
]

# PCA 最小样本量（太少时 SVD 不稳定）
PCA_MIN_OBS: int = 60
# 各可观测因子"理论上"对应的主成分（用于交叉校验方向）
PCA_EXPECTED_COMPONENT: dict[str, str] = {"level": "PC1", "slope": "PC2", "curvature": "PC3"}

# 因子定义（写死期限，便于复核与写进报告）
LEVEL_TENORS: tuple[float, ...] = (10.0, 15.0, 20.0, 30.0)
SLOPE_TENORS: tuple[float, float] = (10.0, 2.0)  # 10Y - 2Y
CURVATURE_TENORS: tuple[float, float, float] = (5.0, 2.0, 10.0)  # 2*5Y - 2Y - 10Y


def _column(frame: pd.DataFrame, tenor: float) -> pd.Series:
    """取某期限列；缺失该期限时返回全 NaN（不插值、不臆造）。"""
    if tenor in frame.columns:
        return frame[tenor].astype(float)
    return pd.Series(np.nan, index=frame.index, dtype=float)


def observable_factors(panel: pd.DataFrame) -> pd.DataFrame:
    """由观测收益率直接构造 level / slope / curvature 三因子。

    Parameters
    ----------
    panel : pandas.DataFrame
        宽表：``index = 日期``，``columns = 期限（年，float）``，
        ``values = 到期收益率（百分数）``。缺失期限保持 NaN（不填充）。

    Returns
    -------
    pandas.DataFrame
        ``index`` 与输入相同（日期），``columns = ["level", "slope", "curvature"]``，
        单位均为**百分点**（日变动 ×100 即 bp）。

        - ``level`` = mean(y10, y15, y20, y30)
        - ``slope`` = y10 − y2
        - ``curvature`` = 2·y5 − y2 − y10

        任一所需期限缺失时对应因子为 NaN（例如 2016-01 至 2019-01 缺 2Y，
        这段区间 ``slope``/``curvature`` 为空，不做插值）。

    Raises
    ------
    TypeError
        ``panel`` 不是 DataFrame。
    ValueError
        ``panel`` 为空或列不是数值期限。
    """
    frame = _normalize_panel(panel)
    level = (
        _column(frame, LEVEL_TENORS[0])
        + _column(frame, LEVEL_TENORS[1])
        + _column(frame, LEVEL_TENORS[2])
        + _column(frame, LEVEL_TENORS[3])
    ) / float(len(LEVEL_TENORS))
    slope = _column(frame, SLOPE_TENORS[0]) - _column(frame, SLOPE_TENORS[1])
    curvature = (
        2.0 * _column(frame, CURVATURE_TENORS[0])
        - _column(frame, CURVATURE_TENORS[1])
        - _column(frame, CURVATURE_TENORS[2])
    )
    factors = pd.DataFrame({"level": level, "slope": slope, "curvature": curvature})
    factors.index.name = "date"
    factors.attrs["unit"] = "percentage points"
    factors.attrs["definitions"] = {
        "level": f"mean of {LEVEL_TENORS}",
        "slope": "y10 - y2",
        "curvature": "2*y5 - y2 - y10",
    }
    return factors


def factor_jump_sensitivity(
    factors: pd.DataFrame,
    *,
    threshold_bp: float = 25.0,
) -> pd.DataFrame:
    """对每个因子做日变动敏感性检验（相邻日变化分布 / p99 / 最大跳变日）。

    Parameters
    ----------
    factors : pandas.DataFrame
        :func:`observable_factors` 的输出（或同构的因子宽表，单位百分点）。
    threshold_bp : float
        "大跳变"阈值（bp），用于计数列 ``n_above_threshold``。

    Returns
    -------
    pandas.DataFrame
        ``index = 因子名``，列：

        - ``n``：有效日变动样本数；
        - ``mean_abs_bp`` / ``std_bp``：日变动的均值绝对值 / 标准差（bp）；
        - ``p95_abs_bp`` / ``p99_abs_bp`` / ``max_abs_bp``：分位与最大值（bp）；
        - ``max_date``：最大跳变发生的日期；
        - ``n_above_threshold``：超过 ``threshold_bp`` 的天数。

    Raises
    ------
    ValueError
        ``factors`` 为空或没有数值列。
    """
    if not isinstance(factors, pd.DataFrame):
        raise TypeError(f"factors 必须是 DataFrame，实际 {type(factors).__name__}")
    if factors.empty:
        raise ValueError("factors 为空：没有可检验的因子")
    numeric = factors.select_dtypes(include=[np.number])
    if numeric.shape[1] == 0:
        raise ValueError("factors 中没有数值列")
    rows: list[dict[str, object]] = []
    for name in numeric.columns:
        series = numeric[name].astype(float)
        changes = series.diff() * 100.0  # 百分点 → bp
        changes = changes[np.isfinite(changes)]
        if changes.empty:
            rows.append(
                {
                    "factor": name,
                    "n": 0,
                    "mean_abs_bp": np.nan,
                    "std_bp": np.nan,
                    "p95_abs_bp": np.nan,
                    "p99_abs_bp": np.nan,
                    "max_abs_bp": np.nan,
                    "max_date": pd.NaT,
                    "n_above_threshold": 0,
                }
            )
            continue
        abs_changes = changes.abs()
        rows.append(
            {
                "factor": name,
                "n": int(changes.size),
                "mean_abs_bp": float(abs_changes.mean()),
                "std_bp": float(changes.std(ddof=0)),
                "p95_abs_bp": float(np.percentile(abs_changes, 95)),
                "p99_abs_bp": float(np.percentile(abs_changes, 99)),
                "max_abs_bp": float(abs_changes.max()),
                "max_date": abs_changes.idxmax(),
                "n_above_threshold": int((abs_changes > threshold_bp).sum()),
            }
        )
    sensitivity = pd.DataFrame(rows).set_index("factor")
    sensitivity.attrs["unit"] = "bp"
    sensitivity.attrs["threshold_bp"] = threshold_bp
    return sensitivity


# ---------------------------------------------------------------------------
# PCA：样本 = 收益率变化 Δy（bp）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PCAResult:
    """PCA 结果容器。

    Parameters
    ----------
    loadings : pandas.DataFrame
        载荷，``index = tenor``（年），``columns = PC1..PCk``。滚动口径下为
        各窗口载荷的均值。
    explained_variance_ratio : np.ndarray
        各主成分解释方差占比，长度 k。滚动口径下为各窗口的均值。
    scores : pandas.DataFrame
        主成分分数，``index = date``，``columns = PC1..PCk``。
        滚动口径下为**走前**（用前 ``window`` 个观测估载荷、再投影当日 Δy）的分数。
    mean : pandas.Series
        样本 Δy 的逐期限均值（bp），``index = tenor``。
    window : int | None
        滚动窗口长度；``None`` 表示全样本。
    n_obs : int
        参与 PCA 的观测数。
    center : bool
        是否对 Δy 去均值（``True`` = 只保留变化；``False`` = 含均值对照版）。
    loadings_history : pandas.DataFrame | None
        滚动口径下每个估计窗口的载荷，``index = 打分日``，
        ``columns = MultiIndex(component, tenor)``；全样本为 ``None``。
    rolling_evr : pandas.DataFrame | None
        滚动口径下每个窗口的解释方差占比；全样本为 ``None``。
    tenors : np.ndarray
        参与 PCA 的期限（年）。

    Notes
    -----
    本模块的 PCA **永远以收益率变化 Δy（bp）为样本**，不是收益率水平：
    收益率水平高度共线且非平稳，对水平做 PCA 得到的"第一主成分"更多是趋势，
    不能当作日频风险因子。去均值版本（默认）对应"只解释日变动"；含均值版本
    用于对照（第一主成分会吸收样本期漂移）。
    """

    loadings: pd.DataFrame
    explained_variance_ratio: np.ndarray
    scores: pd.DataFrame
    mean: pd.Series
    window: int | None = None
    n_obs: int = 0
    center: bool = True
    loadings_history: pd.DataFrame | None = None
    rolling_evr: pd.DataFrame | None = None
    tenors: np.ndarray = field(default_factory=lambda: np.asarray([], dtype=float))
    winsor_bp: float | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "explained_variance_ratio", np.asarray(self.explained_variance_ratio, dtype=float)
        )
        object.__setattr__(self, "tenors", np.asarray(self.tenors, dtype=float))
        object.__setattr__(self, "n_obs", int(self.n_obs))


def _delta_y_bp(panel: pd.DataFrame) -> tuple[pd.DataFrame, pd.DataFrame]:
    """计算 PCA 样本：``Δy = diff(panel)·100``（bp），并剔除不完整/跨缺失段的差分。"""
    frame = _normalize_panel(panel)
    if frame.shape[0] < 2:
        raise ValueError("panel 至少需要 2 个交易日才能计算收益率变化")
    changes = frame.diff() * 100.0
    changes = changes.dropna(axis=0, how="any")
    if changes.shape[0] < PCA_MIN_OBS:
        raise ValueError(
            f"PCA 样本不足：只有 {changes.shape[0]} 条完整 Δy 观测，至少需要 {PCA_MIN_OBS}"
        )
    return frame, changes


def _component_names(k: int) -> list[str]:
    return [f"PC{i + 1}" for i in range(k)]


def _align_pca_signs(
    loadings: np.ndarray, scores: np.ndarray, tenors: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """固定 PCA 符号约定，使因子方向可跨窗口比较（PC 符号本身是不可识别的）。"""
    tenors = np.asarray(tenors, dtype=float)
    loadings = loadings.copy()
    scores = scores.copy()
    k = loadings.shape[1]

    def index_of(tenor: float) -> int:
        return int(np.argmin(np.abs(tenors - tenor)))

    i2, i5, i10 = index_of(2.0), index_of(5.0), index_of(10.0)
    # PC1：载荷和为正（"水平"方向）。
    if k >= 1 and float(np.sum(loadings[:, 0])) < 0:
        loadings[:, 0] *= -1.0
        scores[:, 0] *= -1.0
    # PC2：用**斜率坐标**（10Y−2Y）定方向，而不是单个最短期限——最短期限的
    # PC2 载荷可能接近 0，用它会因噪声随机翻符号（滚动窗口里尤其明显）。
    if k >= 2 and (loadings[i10, 1] - loadings[i2, 1]) < 0:
        loadings[:, 1] *= -1.0
        scores[:, 1] *= -1.0
    # PC3：用**曲率坐标**（2×5Y−2Y−10Y）定方向。
    if k >= 3 and (2.0 * loadings[i5, 2] - loadings[i2, 2] - loadings[i10, 2]) < 0:
        loadings[:, 2] *= -1.0
        scores[:, 2] *= -1.0
    return loadings, scores


def _fit_pca(
    x: np.ndarray, *, center: bool, n_components: int, tenors: np.ndarray
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """对 ``x``（n×p，bp）做 SVD PCA，返回 ``(loadings p×k, scores n×k, evr k)``。"""
    x = np.asarray(x, dtype=float)
    if x.ndim != 2:
        raise ValueError(f"PCA 输入必须是二维矩阵，实际 shape={x.shape}")
    mu = x.mean(axis=0) if center else np.zeros(x.shape[1])
    xc = x - mu
    u, s, vt = np.linalg.svd(xc, full_matrices=False)
    k = int(min(n_components, vt.shape[0], x.shape[1]))
    if k < 1:
        raise ValueError("PCA 没有可用的主成分")
    loadings = vt[:k].T.copy()
    scores = u[:, :k] * s[:k]
    total = float(np.sum(s**2))
    evr = (s[:k] ** 2) / total if total > 0 else np.zeros(k)
    loadings, scores = _align_pca_signs(loadings, scores, tenors)
    return loadings, scores, evr


def pca_factors(
    panel: pd.DataFrame,
    *,
    window: int | None = None,
    n_components: int = 3,
    center: bool = True,
    winsor_bp: float | None = None,
) -> PCAResult:
    """对**收益率变化 Δy（bp）**做 PCA（全样本或走前滚动）。

    Parameters
    ----------
    panel : pandas.DataFrame
        收益率宽表（``index = date``，``columns = 期限（年）``，values 百分数）。
    window : int | None
        ``None``：全样本 PCA，样本为全部完整 Δy 观测；整数：滚动窗口（观测数）。
        滚动口径下每个打分日只用**之前** ``window`` 个观测估载荷，再投影当日 Δy，
        因此分数不含前视信息。
    n_components : int
        主成分个数（默认 3）。
    center : bool
        是否对 Δy 去均值。默认 ``True``（只保留变化）；``False`` 为"含均值"对照版。
    winsor_bp : float | None
        对 Δy 做**对称截尾**（winsorize）的阈值（bp）；``None`` 不处理。
        中国 2002–2009 年数据存在单日数百 bp 的异常跳变（如 2004-08-17），
        不处理会让 PCA 第一主成分被 30Y/短端异常日主导。稳健性对照用
        ``winsor_bp=10`` 等；这是**数据清洗**，不是调整因子定义。

    Returns
    -------
    PCAResult
        见 :class:`PCAResult`。

    Raises
    ------
    ValueError
        panel 为空/非法、样本不足、``window`` 非法。
    """
    if not isinstance(panel, pd.DataFrame):
        raise TypeError(f"panel 必须是 DataFrame，实际 {type(panel).__name__}")
    n_components = int(n_components)
    if n_components < 1:
        raise ValueError("n_components 必须 ≥1")
    _, changes = _delta_y_bp(panel)
    tenors = changes.columns.to_numpy(dtype=float)
    dates = changes.index
    x = changes.to_numpy(dtype=float)
    if winsor_bp is not None:
        limit = float(winsor_bp)
        if not np.isfinite(limit) or limit <= 0:
            raise ValueError(f"winsor_bp 必须是正数，实际 {winsor_bp!r}")
        x = np.clip(x, -limit, limit)
    component_names = _component_names(n_components)

    if window is None:
        loadings, scores, evr = _fit_pca(x, center=center, n_components=n_components, tenors=tenors)
        return PCAResult(
            loadings=pd.DataFrame(
                loadings, index=pd.Index(tenors, name="tenor"), columns=component_names
            ),
            explained_variance_ratio=evr,
            scores=pd.DataFrame(scores, index=dates, columns=component_names),
            mean=pd.Series(x.mean(axis=0), index=pd.Index(tenors, name="tenor")),
            window=None,
            n_obs=x.shape[0],
            center=center,
            tenors=tenors,
            winsor_bp=winsor_bp,
        )

    window = int(window)
    if window < max(30, n_components + 10) or window >= x.shape[0]:
        raise ValueError(
            f"window 必须在 [{max(30, n_components + 10)}, {x.shape[0] - 1}] 区间内，实际 {window}"
        )
    n_scores = x.shape[0] - window
    k = min(n_components, x.shape[1], window)
    scores_arr = np.empty((n_scores, k))
    evr_arr = np.empty((n_scores, k))
    load_hist = np.empty((n_scores, k, x.shape[1]))
    out_dates: list[pd.Timestamp] = []
    for idx in range(window, x.shape[0]):
        window_x = x[idx - window : idx]
        lw, _, evrw = _fit_pca(window_x, center=center, n_components=k, tenors=tenors)
        mu = window_x.mean(axis=0) if center else np.zeros(x.shape[1])
        scores_arr[idx - window] = (x[idx] - mu) @ lw
        evr_arr[idx - window] = evrw
        load_hist[idx - window] = lw.T
        out_dates.append(dates[idx])

    score_index = pd.DatetimeIndex(out_dates)
    rolling_evr = pd.DataFrame(evr_arr, index=score_index, columns=component_names[:k])
    history_columns = pd.MultiIndex.from_product(
        [component_names[:k], pd.Index(tenors, name="tenor")], names=["component", "tenor"]
    )
    loadings_history = pd.DataFrame(
        load_hist.reshape(n_scores, -1), index=score_index, columns=history_columns
    )
    mean_loadings = load_hist.mean(axis=0)  # (k, p)
    return PCAResult(
        loadings=pd.DataFrame(
            mean_loadings.T, index=pd.Index(tenors, name="tenor"), columns=component_names[:k]
        ),
        explained_variance_ratio=evr_arr.mean(axis=0),
        scores=pd.DataFrame(scores_arr, index=score_index, columns=component_names[:k]),
        mean=pd.Series(x.mean(axis=0), index=pd.Index(tenors, name="tenor")),
        window=window,
        n_obs=n_scores,
        center=center,
        loadings_history=loadings_history,
        rolling_evr=rolling_evr,
        tenors=tenors,
        winsor_bp=winsor_bp,
    )


def loading_stability(pca: PCAResult) -> pd.DataFrame:
    """滚动口径下载荷的时序稳定性（载荷与全样本向量的相关、标准差、EVR 波动）。

    Returns
    -------
    pandas.DataFrame
        ``index = PC1..PCk``，列：``evr_mean`` / ``evr_std`` /
        ``loading_std_mean``（各期限载荷时间序列标准差的均值）/
        ``corr_with_fullsample_mean``（每个窗口载荷向量与全样本载荷向量的
        横截面相关系数的均值）/ ``corr_with_fullsample_min`` / ``n_windows``。

    Raises
    ------
    ValueError
        传入的是全样本 PCA（没有 ``loadings_history``）。
    """
    if pca.loadings_history is None or pca.rolling_evr is None:
        raise ValueError("loading_stability 需要滚动窗口 PCA 结果（window 不为 None）")
    rows: list[dict[str, float]] = []
    for component in pca.loadings.columns:
        series = pca.loadings_history[component]  # index=date, columns=tenor
        full = pca.loadings[component].to_numpy(dtype=float)
        corrs = []
        for _, row in series.iterrows():
            values = row.to_numpy(dtype=float)
            if np.std(values) == 0 or np.std(full) == 0:
                continue
            corrs.append(float(np.corrcoef(values, full)[0, 1]))
        evr_series = pca.rolling_evr[component]
        rows.append(
            {
                "component": component,
                "evr_mean": float(evr_series.mean()),
                "evr_std": float(evr_series.std(ddof=0)),
                "loading_std_mean": float(series.std(axis=0, ddof=0).mean()),
                "corr_with_fullsample_mean": float(np.mean(corrs)) if corrs else float("nan"),
                "corr_with_fullsample_min": float(np.min(corrs)) if corrs else float("nan"),
                "n_windows": int(series.shape[0]),
            }
        )
    return pd.DataFrame(rows).set_index("component")


# ---------------------------------------------------------------------------
# 因子交叉校验与正交化
# ---------------------------------------------------------------------------
def _regression_r2(y: np.ndarray, scores: np.ndarray) -> float:
    """``y`` 对 ``[const, scores]`` 回归的 R²（用于"因子能被前 k 个 PC 解释多少"）。"""
    mask = np.isfinite(y)
    if mask.sum() < scores.shape[1] + 2:
        return float("nan")
    y_valid = y[mask]
    if np.allclose(np.var(y_valid), 0.0):
        return float("nan")
    design = np.column_stack([np.ones(mask.sum()), scores[mask]])
    beta, *_ = np.linalg.lstsq(design, y_valid, rcond=None)
    residual = y_valid - design @ beta
    ss_res = float(np.sum(residual**2))
    ss_tot = float(np.sum((y_valid - y_valid.mean()) ** 2))
    return 1.0 - ss_res / ss_tot if ss_tot > 0 else float("nan")


def factor_crosscheck(
    factors: pd.DataFrame,
    pca: PCAResult,
    *,
    winsor_bp: float | None = None,
) -> pd.DataFrame:
    """用可观测因子（变化口径）与 PC1–PC3 分数交叉校验。

    因子的相关性一律用**日变化**（``Δfactor × 100``，bp）与 PC 分数（也是 Δy 的
    线性组合，bp）计算，避免"水平对变化"的量纲错配。

    Parameters
    ----------
    factors : pandas.DataFrame
        :func:`observable_factors` 的输出（或同构因子表，单位百分点）。
    pca : PCAResult
        :func:`pca_factors` 的结果。
    winsor_bp : float | None
        对因子日变化做同样的对称截尾（bp）。``None`` 时**自动沿用
        ``pca.winsor_bp``**，保证"样本处理"与 PCA 一致（否则比较不公平）；
        显式传 0 或 False 可关闭。

    Returns
    -------
    pandas.DataFrame
        ``index = 因子名``，列：

        - ``corr_pc1`` / ``corr_pc2`` / ``corr_pc3``：Δfactor 与对应 PC 分数的相关；
        - ``r2_pc1_3``：Δfactor 对 PC1–PC3 回归的 R²；
        - ``expected_pc``：该因子理论上对应的主成分（level→PC1…）；
        - ``corr_expected``：与 expected PC 的相关系数；
        - ``sign_consistent``：``corr_expected > 0``（在 PC 符号约定下方向一致）；
        - ``meets_target``：level 要求 ``corr_pc1 > 0.95``；其余要求方向一致且
          ``|corr_expected| > 0.5``（不达标如实标记，不调整定义凑数）。

    Raises
    ------
    ValueError
        因子或 PCA 结果为空、样本交集不足。
    """
    if factors.empty:
        raise ValueError("factors 为空")
    if pca.scores.empty:
        raise ValueError("pca.scores 为空")
    changes = factors.diff() * 100.0
    effective_winsor = pca.winsor_bp if winsor_bp is None else winsor_bp
    if effective_winsor:
        limit = float(effective_winsor)
        if not np.isfinite(limit) or limit <= 0:
            raise ValueError(f"winsor_bp 必须是正数，实际 {winsor_bp!r}")
        changes = changes.clip(lower=-limit, upper=limit)
    common_index = changes.index.intersection(pca.scores.index)
    if common_index.size < PCA_MIN_OBS:
        raise ValueError(
            f"因子变化与 PC 分数的共同样本只有 {common_index.size} 个，至少需要 {PCA_MIN_OBS}"
        )
    changes = changes.loc[common_index]
    scores = pca.scores.loc[common_index]
    component_names = list(scores.columns)
    if "PC1" not in component_names:
        raise ValueError("pca.scores 至少需要包含 PC1")

    rows: list[dict[str, object]] = []
    score_matrix = scores.to_numpy(dtype=float)
    for name in changes.columns:
        y = changes[name].to_numpy(dtype=float)
        mask = np.isfinite(y) & np.isfinite(score_matrix).all(axis=1)
        row: dict[str, object] = {"factor": name}
        corrs: dict[str, float] = {}
        for j, component in enumerate(component_names):
            if mask.sum() < 3:
                corrs[component] = float("nan")
                continue
            corrs[component] = float(np.corrcoef(y[mask], score_matrix[mask, j])[0, 1])
        for j in range(3):
            component = f"PC{j + 1}"
            row[f"corr_pc{j + 1}"] = corrs.get(component, float("nan"))
        row["r2_pc1_3"] = _regression_r2(y, score_matrix[:, : min(3, score_matrix.shape[1])])
        expected = PCA_EXPECTED_COMPONENT.get(str(name).lower())
        if expected is None:
            expected = max(corrs, key=lambda comp: abs(corrs[comp])) if corrs else None
        row["expected_pc"] = expected
        corr_expected = float(corrs.get(expected, float("nan"))) if expected else float("nan")
        row["corr_expected"] = corr_expected
        sign_consistent = bool(np.isfinite(corr_expected) and corr_expected > 0)
        row["sign_consistent"] = sign_consistent
        if str(name).lower() == "level":
            row["meets_target"] = bool(
                np.isfinite(corrs.get("PC1", np.nan)) and corrs["PC1"] > 0.95
            )
        else:
            row["meets_target"] = bool(sign_consistent and abs(corr_expected) > 0.5)
        rows.append(row)
    return pd.DataFrame(rows).set_index("factor")


def _ols_residual(y: np.ndarray, regressors: list[np.ndarray]) -> np.ndarray:
    """``y`` 对 ``[const, *regressors]`` 回归后的残差（NaN 保持 NaN）。"""
    mask = np.isfinite(y)
    for regressor in regressors:
        mask &= np.isfinite(regressor)
    residual = np.full_like(y, np.nan, dtype=float)
    n_valid = int(mask.sum())
    if n_valid < len(regressors) + 2:
        return residual
    design = np.column_stack([np.ones(n_valid), *[reg[mask] for reg in regressors]])
    beta, *_ = np.linalg.lstsq(design, y[mask], rcond=None)
    residual[mask] = y[mask] - design @ beta
    return residual


def orthogonalize(
    factors: pd.DataFrame,
    *,
    space: Literal["levels", "changes"] = "levels",
) -> pd.DataFrame:
    """把 slope 对 level、curvature 对 level+slope 回归取残差，得到正交化因子。

    Parameters
    ----------
    factors : pandas.DataFrame
        至少包含 ``level`` / ``slope`` / ``curvature`` 三列（单位百分点）。
    space : {"levels", "changes"}
        ``"levels"``（默认）在因子**水平**上正交化（指标间的静态独立性）；
        ``"changes"`` 在日变化（bp）上正交化（更适合做日频信号）。

    Returns
    -------
    pandas.DataFrame
        ``index`` 与输入一致，列 ``["level", "slope_orth", "curvature_orth"]``
        （``level`` 原样保留；后两列为回归残差）。

    Notes
    -----
    正交化是**对样本**做的线性投影：残差与回归变量在该样本内严格正交
    （相关系数 ≈ 0，见 ``tests/test_pca_factors.py``），但样本内正交不代表
    经济上独立，也不保证样本外仍然正交；报告里需说明这一点。

    Raises
    ------
    ValueError
        缺少必要列或 ``space`` 非法。
    """
    required = ["level", "slope", "curvature"]
    missing = [name for name in required if name not in factors.columns]
    if missing:
        raise ValueError(f"factors 缺少必要列 {missing}；需要 {required}")
    if space not in ("levels", "changes"):
        raise ValueError(f"space 必须是 'levels' 或 'changes'，实际 {space!r}")
    data = factors[required].astype(float)
    if space == "changes":
        data = data.diff() * 100.0
    level = data["level"].to_numpy(dtype=float)
    slope = data["slope"].to_numpy(dtype=float)
    curvature = data["curvature"].to_numpy(dtype=float)
    slope_orth = _ols_residual(slope, [level])
    curvature_orth = _ols_residual(curvature, [level, slope])
    result = pd.DataFrame(
        {"level": level, "slope_orth": slope_orth, "curvature_orth": curvature_orth},
        index=data.index,
    )
    result.index.name = data.index.name
    result.attrs["space"] = space
    result.attrs["regression"] = {
        "slope_orth": "slope ~ 1 + level",
        "curvature_orth": "curvature ~ 1 + level + slope",
    }
    return result


def factor_correlation(
    factors: pd.DataFrame,
    *,
    space: Literal["levels", "changes"] = "levels",
) -> pd.DataFrame:
    """因子相关矩阵（``space="levels"`` 用水平值，``"changes"`` 用日变化 bp）。"""
    if factors.empty:
        raise ValueError("factors 为空")
    if space not in ("levels", "changes"):
        raise ValueError(f"space 必须是 'levels' 或 'changes'，实际 {space!r}")
    data = factors.astype(float)
    if space == "changes":
        data = data.diff() * 100.0
    data = data.dropna(how="any")
    if data.shape[0] < 3:
        raise ValueError("样本太少，无法计算相关矩阵")
    correlation = data.corr()
    correlation.attrs["space"] = space
    return correlation
