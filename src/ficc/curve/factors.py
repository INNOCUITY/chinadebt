"""可观测曲线因子、PCA 与正交化（第二轮评审 R14/R15/R16/R21 后的契约）。

因子契约（R14，**对外固定**）
----------------------------
:func:`observable_factors` 固定返回 4 列（顺序固定，见 :data:`FACTOR_COLUMNS`）：

- ``level_all``：**固定 11 个标准期限**（3M/6M/1Y/2Y/3Y/5Y/7Y/10Y/15Y/20Y/30Y）的等权均值。
  任一期缺失 → 该日 NaN，**不得**按可用期限重新归一化（否则定义随数据漂移）。
- ``level_long``：10Y/15Y/20Y/30Y 等权均值；
- ``slope``：10Y − 2Y；
- ``curvature``：2×5Y − 2Y − 10Y。

单位均为**百分点**（日变动 ×100 即 bp）。``level`` 是 ``level_long`` 的**弃用别名**
（需显式 ``include_deprecated_level=True`` 才返回，并触发 DeprecationWarning）。

为什么用可观测因子而不是 NS/NSS 参数（R02 结论）
--------------------------------------------------
NS/NSS 参数存在严重识别性问题（同一组收益率可对应多组等价参数；2023-10-12 案例：
收益率只动 0.04pp，锚定 b1 却跳 147bp）。可观测因子直接由收益率线性组合得到，
无拟合、无识别、日间连续；需要参数化因子时必须走因果方案（冻结训练窗 τ 或状态
空间模型），见 :mod:`ficc.curve.fitting` 模块 docstring。

PCA 与 winsor 统一口径（R16，**关键**）
---------------------------------------
``clip``（截尾）与线性聚合**不可交换**：``clip(mean(Δy)) ≠ mean(clip(Δy))``。
因此本模块把预处理收敛成**单一入口** :func:`preprocess_changes`：

1. 先算逐期限 Δy（bp，完整样本、固定 11 期限）；
2. 若设 ``winsor_bp``，对矩阵**逐期限、逐观测**截尾；
3. PCA 用该截尾矩阵估载荷/打分；
4. 因子变化用**同一个**截尾矩阵经 :func:`factor_changes_from_processed`
   （固定权重矩阵 :func:`factor_weight_matrix`）线性聚合。

:class:`PCAResult` 携带 ``processed_changes``，调用方拿不到"各算各的"的中间量；
若要做"原始因子变化 vs 清洗后 PC"的对照，必须显式
``factor_crosscheck(..., use_raw_factor_changes=True)`` 并在报告里标注为另一种诊断。

验收映射（R15）
---------------
:data:`FACTOR_TARGETS` 显式给出 ``因子 → (目标 PC, 相关系数门槛)``；未知因子只输出
``exploratory_corr``（探索性相关）与 ``target_pc=None / meets_target=None``，
**不给达标判词**（避免事后挑 PC 冒充事前假设检验）。

正交化（R21，因果边界）
-----------------------
:func:`fit_orthogonalizer` 在**训练窗**上拟合线性投影并保存系数；
:meth:`Orthogonalizer.transform` / :meth:`Orthogonalizer.inverse` 冻结系数后应用。
无 ``window`` 时必须显式 ``fit_scope="full"``，且报告须标注"样本内诊断"——
样本内水平正交不等于日频风险正交（变化空间相关可能显著非零）。
"""

from __future__ import annotations

import warnings
from dataclasses import dataclass, field
from typing import Literal

import numpy as np
import pandas as pd

from ficc.curve.fitting import _normalize_panel
from ficc.data.schema import CANONICAL_TENORS

__all__ = [
    "CANONICAL_TENOR_GRID",
    "DEPRECATED_FACTOR_ALIASES",
    "FACTOR_COLUMNS",
    "FACTOR_TARGETS",
    "Orthogonalizer",
    "PCAResult",
    "evaluate_factor_target",
    "factor_changes_from_processed",
    "factor_correlation",
    "factor_crosscheck",
    "factor_definition_table",
    "factor_jump_sensitivity",
    "factor_weight_matrix",
    "fit_orthogonalizer",
    "loading_stability",
    "observable_factors",
    "orthogonalize",
    "pca_factors",
    "preprocess_changes",
]

# 固定 11 个标准期限（level_all 的母集；缺一不可，不做可用列重归一化）
CANONICAL_TENOR_GRID: tuple[float, ...] = tuple(float(t) for t in CANONICAL_TENORS)
# 对外因子契约：顺序固定
FACTOR_COLUMNS: tuple[str, ...] = ("level_all", "level_long", "slope", "curvature")
# 弃用别名（旧代码里的 level = 长端 level）
DEPRECATED_FACTOR_ALIASES: dict[str, str] = {"level": "level_long"}
# 验收映射（R15）：因子 → (目标 PC, 相关系数门槛)。未知因子不给门槛。
FACTOR_TARGETS: dict[str, tuple[str, float]] = {
    "level_all": ("PC1", 0.95),
    "level_long": ("PC1", 0.95),
    "slope": ("PC2", 0.50),
    "curvature": ("PC3", 0.50),
}
LEVEL_ALL_TENORS: tuple[float, ...] = CANONICAL_TENOR_GRID
LEVEL_LONG_TENORS: tuple[float, ...] = (10.0, 15.0, 20.0, 30.0)
SLOPE_TENORS: tuple[float, float] = (10.0, 2.0)  # 10Y - 2Y
CURVATURE_TENORS: tuple[float, float, float] = (5.0, 2.0, 10.0)  # 2*5Y - 2Y - 10Y

# PCA 最小样本量
PCA_MIN_OBS: int = 60


# ---------------------------------------------------------------------------
# 因子定义与权重
# ---------------------------------------------------------------------------
def factor_definition_table() -> pd.DataFrame:
    """因子定义表（供文档自动生成 / 报告附录）。

    Returns
    -------
    pandas.DataFrame
        列：``factor`` / ``definition`` / ``weights`` / ``unit`` /
        ``target_pc`` / ``target_threshold`` / ``deprecated``。
    """
    rows = [
        {
            "factor": "level_all",
            "definition": "固定 11 个标准期限的等权均值；任一缺失→NaN，不重归一化",
            "weights": "每个标准期限 1/11",
            "unit": "%",
            "target_pc": FACTOR_TARGETS["level_all"][0],
            "target_threshold": FACTOR_TARGETS["level_all"][1],
            "deprecated": False,
        },
        {
            "factor": "level_long",
            "definition": "10/15/20/30Y 等权均值（长端水平）",
            "weights": "10Y/15Y/20Y/30Y 各 1/4",
            "unit": "%",
            "target_pc": FACTOR_TARGETS["level_long"][0],
            "target_threshold": FACTOR_TARGETS["level_long"][1],
            "deprecated": False,
        },
        {
            "factor": "slope",
            "definition": "10Y − 2Y 期限利差",
            "weights": "10Y +1；2Y −1",
            "unit": "%",
            "target_pc": FACTOR_TARGETS["slope"][0],
            "target_threshold": FACTOR_TARGETS["slope"][1],
            "deprecated": False,
        },
        {
            "factor": "curvature",
            "definition": "2×5Y − 2Y − 10Y（蝶式/曲度）",
            "weights": "5Y +2；2Y −1；10Y −1",
            "unit": "%",
            "target_pc": FACTOR_TARGETS["curvature"][0],
            "target_threshold": FACTOR_TARGETS["curvature"][1],
            "deprecated": False,
        },
        {
            "factor": "level",
            "definition": "DEPRECATED：level_long 的别名（旧接口）",
            "weights": "同 level_long",
            "unit": "%",
            "target_pc": None,
            "target_threshold": None,
            "deprecated": True,
        },
    ]
    table = pd.DataFrame(rows)
    table.attrs["note"] = "level_all 使用固定 11 期限；不得按可用期限重新归一化"
    return table


def factor_weight_matrix(tenors: tuple[float, ...] | None = None) -> pd.DataFrame:
    """固定因子权重矩阵（行=因子，列=标准期限）：``Δfactor(bp) = W · Δy(bp)``。"""
    grid = tuple(float(t) for t in (tenors if tenors is not None else CANONICAL_TENOR_GRID))
    weights = np.zeros((len(FACTOR_COLUMNS), len(grid)))
    for j, tenor in enumerate(grid):
        weights[0, j] = 1.0 / len(grid)  # level_all
        if tenor in LEVEL_LONG_TENORS:
            weights[1, j] = 1.0 / len(LEVEL_LONG_TENORS)
        if tenor == SLOPE_TENORS[0]:
            weights[2, j] = 1.0
        if tenor == SLOPE_TENORS[1]:
            weights[2, j] = -1.0
        if tenor == CURVATURE_TENORS[0]:
            weights[3, j] = 2.0
        if tenor in (CURVATURE_TENORS[1], CURVATURE_TENORS[2]):
            weights[3, j] = -1.0
    matrix = pd.DataFrame(weights, index=list(FACTOR_COLUMNS), columns=list(grid))
    matrix.columns.name = "tenor"
    matrix.attrs["unit"] = "per 1bp of Δy"
    return matrix


def _column(frame: pd.DataFrame, tenor: float) -> pd.Series:
    """取某期限列；缺失该期限时返回全 NaN（不插值、不臆造）。"""
    if tenor in frame.columns:
        return frame[tenor].astype(float)
    return pd.Series(np.nan, index=frame.index, dtype=float)


def _canonical_frame(panel: pd.DataFrame) -> pd.DataFrame:
    """规范化面板并固定到 11 个标准期限（缺失列补 NaN）。"""
    frame = _normalize_panel(panel)
    return frame.reindex(columns=list(CANONICAL_TENOR_GRID))


# ---------------------------------------------------------------------------
# 可观测因子（固定契约）
# ---------------------------------------------------------------------------
def observable_factors(
    panel: pd.DataFrame,
    *,
    include_deprecated_level: bool = False,
) -> pd.DataFrame:
    """由观测收益率构造 4 个固定因子。

    Parameters
    ----------
    panel : pandas.DataFrame
        收益率宽表：``index=日期``，``columns=期限（年）``，values 为**百分数**。
    include_deprecated_level : bool
        是否额外返回弃用别名 ``level``（= ``level_long``，触发 DeprecationWarning）。
        默认 ``False``，保证输出列严格等于 :data:`FACTOR_COLUMNS`。

    Returns
    -------
    pandas.DataFrame
        ``index`` 与输入相同；列固定为 ``level_all, level_long, slope, curvature``
        （单位：百分点）。任一所需期限缺失时对应因子为 NaN：
        ``level_all`` 要求**全部 11 个标准期限**存在（缺一即 NaN，不重新归一化）。

    Raises
    ------
    TypeError
        ``panel`` 不是 DataFrame。
    ValueError
        ``panel`` 为空或列不是数值期限。

    Examples
    --------
    >>> import pandas as pd
    >>> idx = pd.to_datetime(["2024-06-28"])
    >>> cols = [0.25, 0.5, 1, 2, 3, 5, 7, 10, 15, 20, 30]
    >>> panel = pd.DataFrame([[1.5] * 11], index=idx, columns=cols)
    >>> observable_factors(panel).columns.tolist()
    ['level_all', 'level_long', 'slope', 'curvature']
    """
    frame = _canonical_frame(panel)
    level_all = frame.mean(axis=1, skipna=False)  # 任一 NaN → 整行 NaN
    level_long = frame.loc[:, list(LEVEL_LONG_TENORS)].mean(axis=1, skipna=False)
    slope = _column(frame, SLOPE_TENORS[0]) - _column(frame, SLOPE_TENORS[1])
    curvature = (
        2.0 * _column(frame, CURVATURE_TENORS[0])
        - _column(frame, CURVATURE_TENORS[1])
        - _column(frame, CURVATURE_TENORS[2])
    )
    factors = pd.DataFrame(
        {
            "level_all": level_all,
            "level_long": level_long,
            "slope": slope,
            "curvature": curvature,
        },
        index=frame.index,
    )
    factors.index.name = "date"
    factors.attrs["unit"] = "percentage points"
    factors.attrs["definitions"] = (
        factor_definition_table().set_index("factor")["definition"].to_dict()
    )
    if include_deprecated_level:
        warnings.warn(
            "observable_factors(include_deprecated_level=True) 返回的 'level' 是 "
            "level_long 的弃用别名；新代码请直接用 level_long。",
            DeprecationWarning,
            stacklevel=2,
        )
        factors["level"] = factors["level_long"]
    return factors


# ---------------------------------------------------------------------------
# 因子跳变敏感性
# ---------------------------------------------------------------------------
def factor_jump_sensitivity(
    factors: pd.DataFrame,
    *,
    threshold_bp: float = 25.0,
) -> pd.DataFrame:
    """对每个因子做日变动敏感性检验（相邻日变化分布 / p99 / 最大跳变日）。

    Parameters
    ----------
    factors : pandas.DataFrame
        :func:`observable_factors` 的输出（或同构因子宽表，单位百分点）。
    threshold_bp : float
        "大跳变"阈值（bp），用于计数列 ``n_above_threshold``。

    Returns
    -------
    pandas.DataFrame
        ``index = 因子名``，列：``n`` / ``mean_abs_bp`` / ``std_bp`` /
        ``p95_abs_bp`` / ``p99_abs_bp`` / ``max_abs_bp`` / ``max_date`` /
        ``n_above_threshold``。
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
# R16：共享预处理（clip 逐期限 Δy）与固定权重聚合
# ---------------------------------------------------------------------------
def preprocess_changes(panel: pd.DataFrame, *, winsor_bp: float | None = None) -> pd.DataFrame:
    """PCA 与因子的**唯一**预处理入口：逐期限 Δy（bp），可选对称截尾。

    ``clip`` 与线性聚合不可交换（``clip(mean) != mean(clip)``），因此必须先得到
    逐期限、逐观测的截尾变动矩阵，再同时喂给 PCA 与因子聚合。

    Parameters
    ----------
    panel : pandas.DataFrame
        收益率宽表（百分点）。缺任一标准期限的日期会被剔除（complete-case），
        且跨缺失段的差分也会被剔除（不做跨缺口差分）。
    winsor_bp : float | None
        对称截尾阈值（bp）。``None`` 不截尾。

    Returns
    -------
    pandas.DataFrame
        ``index=date``，``columns=11 个标准期限``，values 为 Δy（**bp**）——
        已按需截尾。``attrs`` 记录 ``winsor_bp``、``unit="bp"``、样本区间。

    Raises
    ------
    ValueError
        panel 缺标准期限、样本不足、``winsor_bp`` 非法。
    TypeError
        panel 不是 DataFrame。
    """
    if not isinstance(panel, pd.DataFrame):
        raise TypeError(f"panel 必须是 DataFrame，实际 {type(panel).__name__}")
    frame = _normalize_panel(panel)
    missing_tenors = [t for t in CANONICAL_TENOR_GRID if t not in frame.columns]
    if missing_tenors:
        raise ValueError(f"panel 缺少标准期限 {missing_tenors}；level_all 需要固定 11 期限")
    frame = frame.reindex(columns=list(CANONICAL_TENOR_GRID))
    changes = frame.diff() * 100.0  # 百分点 → bp
    changes = changes.dropna(axis=0, how="any")
    if changes.shape[0] < PCA_MIN_OBS:
        raise ValueError(
            f"样本不足：只有 {changes.shape[0]} 条完整 Δy 观测，至少需要 {PCA_MIN_OBS}"
        )
    if winsor_bp is not None:
        limit = float(winsor_bp)
        if not np.isfinite(limit) or limit <= 0:
            raise ValueError(f"winsor_bp 必须是正数，实际 {winsor_bp!r}")
        changes = changes.clip(lower=-limit, upper=limit)
    changes.index.name = "date"
    changes.attrs["winsor_bp"] = winsor_bp
    changes.attrs["unit"] = "bp"
    changes.attrs["sample_start"] = changes.index.min()
    changes.attrs["sample_end"] = changes.index.max()
    return changes


def factor_changes_from_processed(processed_changes: pd.DataFrame) -> pd.DataFrame:
    """用固定权重把**已预处理**的逐期限 Δy（bp）聚合为因子变化（bp）。

    ``Δfactor = W · Δy``，其中 ``W`` 来自 :func:`factor_weight_matrix`。
    这是 R16 要求的"同一路径"聚合：不得对因子聚合值再做截尾。
    """
    if not isinstance(processed_changes, pd.DataFrame):
        raise TypeError(
            f"processed_changes 必须是 DataFrame，实际 {type(processed_changes).__name__}"
        )
    missing = [t for t in CANONICAL_TENOR_GRID if t not in processed_changes.columns]
    if missing:
        raise ValueError(f"processed_changes 缺少标准期限 {missing}")
    weights = factor_weight_matrix().to_numpy(dtype=float)
    values = processed_changes.loc[:, list(CANONICAL_TENOR_GRID)].to_numpy(dtype=float)
    result = pd.DataFrame(
        values @ weights.T, index=processed_changes.index, columns=list(FACTOR_COLUMNS)
    )
    result.index.name = processed_changes.index.name
    result.attrs["unit"] = "bp"
    result.attrs["winsor_bp"] = processed_changes.attrs.get("winsor_bp")
    result.attrs["note"] = "由同一 processed_changes 经固定权重线性聚合得到（R16）"
    return result


# ---------------------------------------------------------------------------
# PCA
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class PCAResult:
    """PCA 结果容器（样本永远是收益率变化 Δy，单位 bp）。

    Parameters
    ----------
    loadings : pandas.DataFrame
        载荷 ``index=tenor``，``columns=PC1..``。滚动口径为各窗口载荷的均值。
    explained_variance_ratio : np.ndarray
        各主成分解释方差占比。
    scores : pandas.DataFrame
        主成分分数 ``index=date``。滚动口径下为**走前**打分（用前 window 个观测
        估载荷、再投影当日 Δy）。
    mean : pandas.Series
        ``processed_changes`` 的逐期限均值（bp）。
    window, n_obs, center : 同 :func:`pca_factors`。
    loadings_history / rolling_evr : 滚动口径的逐窗载荷与 EVR。
    tenors : 参与 PCA 的期限（年）。
    winsor_bp : 截尾阈值（bp）或 None。
    processed_changes : pandas.DataFrame
        **共享预处理后的逐期限 Δy（bp）**；PCA 与因子聚合都只能用它（R16）。
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
    processed_changes: pd.DataFrame | None = None

    def __post_init__(self) -> None:
        object.__setattr__(
            self, "explained_variance_ratio", np.asarray(self.explained_variance_ratio, dtype=float)
        )
        object.__setattr__(self, "tenors", np.asarray(self.tenors, dtype=float))
        object.__setattr__(self, "n_obs", int(self.n_obs))


def _component_names(k: int) -> list[str]:
    return [f"PC{i + 1}" for i in range(k)]


def _align_pca_signs(
    loadings: np.ndarray, scores: np.ndarray, tenors: np.ndarray
) -> tuple[np.ndarray, np.ndarray]:
    """固定 PCA 符号约定（PC 符号不可识别；约定只为方向可比较）。

    PC1：载荷和为正；PC2：斜率坐标（10Y−2Y 载荷）为正；PC3：曲率坐标
    （2×5Y−2Y−10Y 载荷）为正。用坐标而非单个期限，避免近零载荷处的噪声翻符号。
    """
    tenors = np.asarray(tenors, dtype=float)
    loadings = loadings.copy()
    scores = scores.copy()
    k = loadings.shape[1]

    def index_of(tenor: float) -> int:
        return int(np.argmin(np.abs(tenors - tenor)))

    i2, i5, i10 = index_of(2.0), index_of(5.0), index_of(10.0)
    if k >= 1 and float(np.sum(loadings[:, 0])) < 0:
        loadings[:, 0] *= -1.0
        scores[:, 0] *= -1.0
    if k >= 2 and (loadings[i10, 1] - loadings[i2, 1]) < 0:
        loadings[:, 1] *= -1.0
        scores[:, 1] *= -1.0
    if k >= 3 and (2.0 * loadings[i5, 2] - loadings[i2, 2] - loadings[i10, 2]) < 0:
        loadings[:, 2] *= -1.0
        scores[:, 2] *= -1.0
    return loadings, scores


def _fit_pca(
    x: np.ndarray, *, center: bool, n_components: int, tenors: np.ndarray
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """SVD PCA：返回 ``(loadings p×k, scores n×k, evr k)``。"""
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
        收益率宽表（百分点）。
    window : int | None
        ``None`` 全样本；整数 = 走前滚动窗口（观测数）。
    n_components : int
        主成分数（默认 3）。
    center : bool
        是否对 Δy 去均值（默认 ``True``，即只解释日变动；``False`` 为对照口径）。
    winsor_bp : float | None
        逐期限 Δy 截尾阈值（bp）。与因子变化共享同一 ``processed_changes``（R16）。

    Returns
    -------
    PCAResult
        含 ``processed_changes``（共享预处理结果）。

    Raises
    ------
    ValueError / TypeError
        panel 非法、样本不足、``window`` 非法、``winsor_bp`` 非法。
    """
    if not isinstance(panel, pd.DataFrame):
        raise TypeError(f"panel 必须是 DataFrame，实际 {type(panel).__name__}")
    n_components = int(n_components)
    if n_components < 1:
        raise ValueError("n_components 必须 ≥1")
    processed = preprocess_changes(panel, winsor_bp=winsor_bp)
    tenors = processed.columns.to_numpy(dtype=float)
    dates = processed.index
    x = processed.to_numpy(dtype=float)
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
            processed_changes=processed,
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
    mean_loadings = load_hist.mean(axis=0)
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
        processed_changes=processed,
    )


def loading_stability(pca: PCAResult) -> pd.DataFrame:
    """滚动口径下载荷的时序稳定性（与全样本载荷相关、时序标准差、EVR 波动）。"""
    if pca.loadings_history is None or pca.rolling_evr is None:
        raise ValueError("loading_stability 需要滚动窗口 PCA 结果（window 不为 None）")
    rows: list[dict[str, float]] = []
    for component in pca.loadings.columns:
        series = pca.loadings_history[component]
        full = pca.loadings[component].to_numpy(dtype=float)
        corrs: list[float] = []
        cosines: list[float] = []
        for _, row in series.iterrows():
            values = row.to_numpy(dtype=float)
            # 余弦相似度：对"水平型"PC（载荷跨期限近乎相等）比 Pearson 稳定
            denom = float(np.linalg.norm(values) * np.linalg.norm(full))
            if denom > 0:
                cosines.append(float(values @ full / denom))
            # Pearson：跨期限方差极小时不稳定，仅作辅助
            if np.std(values) > 0 and np.std(full) > 0:
                corrs.append(float(np.corrcoef(values, full)[0, 1]))
        evr_series = pca.rolling_evr[component]
        rows.append(
            {
                "component": component,
                "evr_mean": float(evr_series.mean()),
                "evr_std": float(evr_series.std(ddof=0)),
                "loading_std_mean": float(series.std(axis=0, ddof=0).mean()),
                "cosine_with_fullsample_mean": float(np.mean(cosines)) if cosines else float("nan"),
                "cosine_with_fullsample_min": float(np.min(cosines)) if cosines else float("nan"),
                "corr_with_fullsample_mean": float(np.mean(corrs)) if corrs else float("nan"),
                "corr_with_fullsample_min": float(np.min(corrs)) if corrs else float("nan"),
                "n_windows": int(series.shape[0]),
            }
        )
    return pd.DataFrame(rows).set_index("component")


# ---------------------------------------------------------------------------
# R15：显式验收映射
# ---------------------------------------------------------------------------
def evaluate_factor_target(
    factor: str, correlation: float
) -> tuple[str | None, float | None, bool | None]:
    """按 :data:`FACTOR_TARGETS` 给出 ``(target_pc, threshold, meets_target)``。

    未知因子返回 ``(None, None, None)``（只允许探索性相关，不得判达标）。
    已知因子只有相关系数 **≥ 门槛** 才算达标（方向由 PC 符号约定统一）。
    """
    target = FACTOR_TARGETS.get(str(factor).lower())
    if target is None:
        return None, None, None
    pc, threshold = target
    if not np.isfinite(correlation):
        return pc, threshold, None
    return pc, threshold, bool(correlation >= threshold)


def _regression_r2(y: np.ndarray, scores: np.ndarray) -> float:
    """``y`` 对 ``[const, scores]`` 回归的 R²。"""
    mask = np.isfinite(y) & np.isfinite(scores).all(axis=1)
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
    use_raw_factor_changes: bool = False,
) -> pd.DataFrame:
    """可观测因子变化与 PC1–3 的交叉校验（R15/R16 契约）。

    **同口径（默认）**：因子变化由 ``pca.processed_changes`` 经固定权重聚合
    （:func:`factor_changes_from_processed`），与 PCA 共享同一截尾矩阵。
    ``use_raw_factor_changes=True`` 是另一种诊断口径（直接用传入 factors 的
    ``diff()*100``），调用方必须在报告中单独标注。

    Returns
    -------
    pandas.DataFrame
        ``index=factor``，列：``corr_pc1/2/3``、``r2_pc1_3``、
        ``target_pc`` / ``target_threshold`` / ``corr_target`` / ``meets_target``、
        ``exploratory_corr`` / ``exploratory_pc``、``sample_note``。

    Raises
    ------
    ValueError
        输入为空、样本交集不足、或缺少 ``processed_changes`` 却未声明原始口径。
    """
    if factors.empty:
        raise ValueError("factors 为空")
    if pca.scores.empty:
        raise ValueError("pca.scores 为空")
    factor_names = [str(name) for name in factors.columns]

    if use_raw_factor_changes:
        raw_changes = factors.astype(float).diff() * 100.0
        changes = raw_changes.loc[:, factor_names]
        sample_note = "raw factor changes (diagnostic; NOT same winsor pipeline as PCA)"
    else:
        if pca.processed_changes is None:
            raise ValueError(
                "pca 缺少 processed_changes；R16 要求 PCA 与因子共享同一预处理。"
                "若确实要做原始对照，请显式 use_raw_factor_changes=True 并在报告标注"
            )
        processed_changes = pca.processed_changes
        known_changes = factor_changes_from_processed(processed_changes)
        changes = pd.DataFrame(index=processed_changes.index)
        for name in factor_names:
            if name in known_changes.columns:
                changes[name] = known_changes[name]
            else:  # 未知因子：只能用原始变化（探索性）
                changes[name] = factors[name].astype(float).diff() * 100.0
        sample_note = "factor changes = W · processed Δy (shared winsor pipeline)"

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
    for name in factor_names:
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
            row[f"corr_pc{j + 1}"] = corrs.get(f"PC{j + 1}", float("nan"))
        row["r2_pc1_3"] = _regression_r2(y, score_matrix[:, : min(3, score_matrix.shape[1])])
        target = FACTOR_TARGETS.get(name.lower())
        if target is not None:
            target_pc, threshold, meets = evaluate_factor_target(
                name, float(corrs.get(target[0], float("nan")))
            )
            row["target_pc"] = target_pc
            row["target_threshold"] = threshold
            row["corr_target"] = (
                float(corrs.get(target_pc, float("nan"))) if target_pc else float("nan")
            )
            row["meets_target"] = meets
            row["exploratory_corr"] = float("nan")
            row["exploratory_pc"] = None
            row["sample_note"] = sample_note
        else:
            best_component = max(corrs, key=lambda comp: abs(corrs[comp])) if corrs else None
            row["target_pc"] = None
            row["target_threshold"] = None
            row["corr_target"] = float("nan")
            row["meets_target"] = None
            row["exploratory_corr"] = (
                float(abs(corrs[best_component])) if best_component else float("nan")
            )
            row["exploratory_pc"] = best_component
            row["sample_note"] = f"{sample_note}; unknown factor → exploratory only"
        rows.append(row)
    result = pd.DataFrame(rows).set_index("factor")
    result.index.name = "factor"
    result.attrs["sample_note"] = sample_note
    result.attrs["winsor_bp"] = pca.winsor_bp
    return result


# ---------------------------------------------------------------------------
# R21：训练窗正交化（fit / transform / inverse）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Orthogonalizer:
    """冻结的线性正交化器（slope⟂level、curvature⟂level+slope）。

    由 :func:`fit_orthogonalizer` 在**训练窗**上拟合；之后 :meth:`transform` /
    :meth:`inverse` 只使用冻结系数，**不**再看新数据（前视不变）。
    """

    level_column: str
    slope_column: str
    curvature_column: str
    columns: tuple[str, ...]
    slope_coef: np.ndarray  # [const, level]
    curvature_coef: np.ndarray  # [const, level, slope]
    fit_scope: str  # "window" | "full"
    fit_window: int | None
    fit_start: pd.Timestamp | object
    fit_end: pd.Timestamp | object
    n_fit: int

    def transform(self, factors: pd.DataFrame) -> pd.DataFrame:
        """对任意行数应用冻结系数，返回正交化因子（列名加 ``_orth``）。"""
        frame = self._normalize(factors)
        level = frame[self.level_column].to_numpy(dtype=float)
        slope = frame[self.slope_column].to_numpy(dtype=float)
        curvature = frame[self.curvature_column].to_numpy(dtype=float)
        slope_orth = slope - (self.slope_coef[0] + self.slope_coef[1] * level)
        curvature_orth = curvature - (
            self.curvature_coef[0] + self.curvature_coef[1] * level + self.curvature_coef[2] * slope
        )
        out = frame.copy()
        out[self.slope_column] = slope_orth
        out[self.curvature_column] = curvature_orth
        out = out.rename(
            columns={
                self.slope_column: f"{self.slope_column}_orth",
                self.curvature_column: f"{self.curvature_column}_orth",
            }
        )
        out.attrs["orthogonalizer"] = self.describe()
        return out

    def inverse(self, orthogonal_factors: pd.DataFrame) -> pd.DataFrame:
        """把正交化因子还原为原始因子（用于核对可逆性与归因）。"""
        frame = orthogonal_factors.copy()
        slope_orth_name = f"{self.slope_column}_orth"
        curvature_orth_name = f"{self.curvature_column}_orth"
        missing = [
            name for name in (slope_orth_name, curvature_orth_name) if name not in frame.columns
        ]
        if missing:
            raise ValueError(f"正交化因子缺少列 {missing}；无法 inverse")
        level = frame[self.level_column].to_numpy(dtype=float)
        slope = frame[slope_orth_name].to_numpy(dtype=float) + (
            self.slope_coef[0] + self.slope_coef[1] * level
        )
        curvature = frame[curvature_orth_name].to_numpy(dtype=float) + (
            self.curvature_coef[0] + self.curvature_coef[1] * level + self.curvature_coef[2] * slope
        )
        out = frame.drop(columns=[slope_orth_name, curvature_orth_name])
        out[self.slope_column] = slope
        out[self.curvature_column] = curvature
        out.attrs["inverse_of"] = self.describe()
        return out

    def describe(self) -> dict[str, object]:
        """简要描述（供报错/报告标注）。"""
        return {
            "level_column": self.level_column,
            "slope_column": self.slope_column,
            "curvature_column": self.curvature_column,
            "fit_scope": self.fit_scope,
            "fit_window": self.fit_window,
            "fit_start": self.fit_start.isoformat()
            if isinstance(self.fit_start, pd.Timestamp)
            else self.fit_start,
            "fit_end": self.fit_end.isoformat()
            if isinstance(self.fit_end, pd.Timestamp)
            else self.fit_end,
            "n_fit": self.n_fit,
        }

    def _normalize(self, factors: pd.DataFrame) -> pd.DataFrame:
        required = [self.level_column, self.slope_column, self.curvature_column]
        missing = [name for name in required if name not in factors.columns]
        if missing:
            raise ValueError(f"factors 缺少列 {missing}；训练时列为 {list(self.columns)}")
        return factors.astype(float)


def _resolve_factor_columns(factors: pd.DataFrame) -> tuple[str, str, str]:
    """找出 level/slope/curvature 三列（level 优先 level_all，其次 level_long / level）。"""
    level_column = None
    for candidate in ("level_all", "level_long", "level"):
        if candidate in factors.columns:
            level_column = candidate
            break
    if level_column is None:
        raise ValueError("factors 缺少 level 列（level_all / level_long / level）")
    missing = [name for name in ("slope", "curvature") if name not in factors.columns]
    if missing:
        raise ValueError(f"factors 缺少列 {missing}")
    return level_column, "slope", "curvature"


def _fit_ols(y: np.ndarray, exog: list[np.ndarray]) -> np.ndarray:
    """最小二乘（带截距），返回系数 ``[const, *exog]``；不足样本抛 ValueError。"""
    mask = np.isfinite(y)
    for column in exog:
        mask &= np.isfinite(column)
    if int(mask.sum()) < len(exog) + 2:
        raise ValueError(
            f"正交化拟合样本不足：需要 ≥{len(exog) + 2} 个有效观测，实际 {int(mask.sum())}"
        )
    design = np.column_stack([np.ones(int(mask.sum())), *[column[mask] for column in exog]])
    beta, *_ = np.linalg.lstsq(design, y[mask], rcond=None)
    return beta


def fit_orthogonalizer(
    factors: pd.DataFrame,
    *,
    window: int | None = None,
    fit_scope: str = "window",
) -> Orthogonalizer:
    """在**训练窗**上拟合线性正交化器（R21）。

    Parameters
    ----------
    factors : pandas.DataFrame
        至少包含 ``level_*`` / ``slope`` / ``curvature`` 列（百分点）。
    window : int | None
        训练窗长度（行数）。大于 0 时**只使用前 window 行**拟合（后续行不参与，
        保证前视不变性）。
    fit_scope : {"window", "full"}
        ``window is None`` 时必须显式传 ``"full"`` 才允许全样本拟合；该口径只能
        作**样本内诊断**，不能当作因果正交。

    Returns
    -------
    Orthogonalizer
        冻结系数的正交化器。

    Raises
    ------
    ValueError
        factors 非法、window 非法、未显式声明 full 拟合、样本不足。
    """
    if factors.empty:
        raise ValueError("factors 为空")
    level_column, slope_column, curvature_column = _resolve_factor_columns(factors)
    columns = tuple(str(name) for name in factors.columns)
    if window is None:
        if fit_scope != "full":
            raise ValueError(
                "未指定 window 时必须显式 fit_scope='full' 才允许全样本拟合"
                "（且只能在报告中标注为样本内诊断）"
            )
        training = factors
    else:
        window = int(window)
        if window < 30 or window > len(factors):
            raise ValueError(f"window 必须在 [30, {len(factors)}] 区间内，实际 {window}")
        training = factors.iloc[:window]
    finite_rows = training.loc[:, [level_column, slope_column, curvature_column]].dropna()
    if finite_rows.empty:
        raise ValueError("训练窗内没有完整观测，无法拟合正交化器")
    level = finite_rows[level_column].to_numpy(dtype=float)
    slope = finite_rows[slope_column].to_numpy(dtype=float)
    curvature = finite_rows[curvature_column].to_numpy(dtype=float)
    slope_coef = _fit_ols(slope, [level])
    curvature_coef = _fit_ols(curvature, [level, slope])
    return Orthogonalizer(
        level_column=level_column,
        slope_column=slope_column,
        curvature_column=curvature_column,
        columns=columns,
        slope_coef=np.asarray(slope_coef, dtype=float),
        curvature_coef=np.asarray(curvature_coef, dtype=float),
        fit_scope="full" if window is None else "window",
        fit_window=window,
        fit_start=training.index[0] if len(training) else pd.NaT,
        fit_end=training.index[-1] if len(training) else pd.NaT,
        n_fit=int(finite_rows.shape[0]),
    )


def orthogonalize(
    factors: pd.DataFrame,
    *,
    space: Literal["levels", "changes"] = "levels",
    window: int | None = None,
    fit_scope: str = "full",
) -> pd.DataFrame:
    """**旧接口（保留兼容）**：拟合 + 变换一步完成；新代码请用
    :func:`fit_orthogonalizer` + :meth:`Orthogonalizer.transform`。

    注意：不指定 ``window`` 时默认 ``fit_scope="full"``（样本内诊断，见 R21）。
    ``space="changes"`` 时在日变化上拟合与变换。
    """
    if space not in ("levels", "changes"):
        raise ValueError(f"space 必须是 'levels' 或 'changes'，实际 {space!r}")
    data = factors.astype(float)
    if space == "changes":
        data = data.diff() * 100.0
    orthogonalizer = fit_orthogonalizer(data, window=window, fit_scope=fit_scope)
    return orthogonalizer.transform(data)


def factor_correlation(
    factors: pd.DataFrame,
    *,
    space: Literal["levels", "changes"] = "levels",
) -> pd.DataFrame:
    """因子相关矩阵（``space="levels"`` 水平值；``"changes"`` 日变化 bp）。"""
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
