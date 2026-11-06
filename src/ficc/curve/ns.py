"""Nelson-Siegel（纳尔逊-西格尔）曲线拟合。

模型
----
::

    y(t) = b0 + b1 · (1 - e^{-t/τ})/(t/τ) + b2 · [(1 - e^{-t/τ})/(t/τ) - e^{-t/τ}]

- ``b0``：水平因子（level），长端渐近值；
- ``b1``：斜率因子（slope），短端相对长端的偏离，``b0 + b1`` 是 ``t → 0`` 的极限；
- ``b2``：曲度因子（curvature），中期驼峰；
- ``τ``：衰减参数（年），决定驼峰位置；NS 对 τ 高度非线性。

在本项目里 NS 的定位
--------------------
中债曲线本身已是 Hermite 三次插值成品，NS **不是用来插值**，而是
① 给出可解析求导的平滑曲线；② 提取 level/slope/curvature 三因子；
③ 用 4 个参数给 11 个期限点降噪。详见 `docs/01_project_plan.md` 决策 1c。

求解策略（为什么不是"纯"多起点）
--------------------------------
模型对 ``(b0, b1, b2)`` 是线性的、只对 ``τ`` 非线性。因此实现分两步：

1. 在 ``τ`` 的对数网格上（**显式包含任务口径要求的 ``{0.5, 1, 2, 4, 8}``**，
   并额外加入配置 ``curve.ns_tau0`` 或调用方传入的 ``tau0``）用线性最小二乘
   **解析**求出最优 b —— 这一步向量化，没有 4 维非线性优化的局部极小问题；
2. 以网格最优点为初值，用 :func:`scipy.optimize.least_squares`
   （``method="trf"``；边界 ``b0∈[0,20]``、``b1/b2∈[-20,20]``、``τ∈[0.05,30]``）
   在完整参数空间做一次有界精修，保证参数精度与边界约束。

这是"多起点取最优"的更稳更快版本：网格粗扫覆盖整个 τ 区间，精修负责收敛精度。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any

import numpy as np

from ficc.config import get_config
from ficc.curve._common import (
    TAU_MAX,
    TAU_MIN,
    params_dict,
    polish,
    prepare_inputs,
    project_grid,
    residual_metrics,
    tau_grid_ns,
)
from ficc.curve._common import ns_rate as _ns_rate

__all__ = ["NSResult", "fit_ns", "ns_rate"]


@dataclass(frozen=True)
class NSResult:
    """单次 Nelson-Siegel 拟合结果。

    Parameters
    ----------
    params : dict[str, float]
        参数 ``b0, b1, b2, tau``（``tau1`` 是同值别名）；``b*`` 单位百分数、``tau`` 单位年。
    fitted : np.ndarray
        各期限点上的拟合收益率（百分数），与输入 ``tenors`` 等长同序。
    residuals : np.ndarray
        残差，**单位 bp**，``residuals = (fitted - observed) × 100``，带符号。
    rmse_bp : float
        均方根残差（bp）。
    mae_bp : float
        平均绝对残差（bp）。
    success : bool
        优化器是否报告成功。
    message : str
        优化器消息（或本模块构造的说明）。
    tenors : np.ndarray | None
        拟合所用期限（年，升序）；``residual_summary`` 依赖它做分期限统计。
    date : datetime.date | None
        该曲线对应的交易日（可选）。
    weights : np.ndarray | None
        拟合使用的权重（与 ``tenors`` 等长），便于复核。
    """

    params: dict[str, float]
    fitted: np.ndarray
    residuals: np.ndarray
    rmse_bp: float
    mae_bp: float
    success: bool
    message: str
    tenors: np.ndarray | None = None
    date: date | None = None
    weights: np.ndarray | None = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "fitted", np.asarray(self.fitted, dtype=float))
        object.__setattr__(self, "residuals", np.asarray(self.residuals, dtype=float))
        if self.tenors is not None:
            object.__setattr__(self, "tenors", np.asarray(self.tenors, dtype=float))
        if self.weights is not None:
            object.__setattr__(self, "weights", np.asarray(self.weights, dtype=float))


def ns_rate(params: np.ndarray, t: float | np.ndarray) -> np.ndarray:
    """按 NS 参数 ``[b0, b1, b2, tau]`` 在 ``t``（年）处求值（百分数）。"""
    return _ns_rate(np.asarray(params, dtype=float), t)


def fit_ns(
    tenors: Any,
    ytm_pct: Any,
    *,
    tau0: float | None = None,
    weights: Any = None,
    max_nfev: int = 40,
    date: date | None = None,
) -> NSResult:
    """拟合 Nelson-Siegel 曲线。

    Parameters
    ----------
    tenors : array-like
        期限（年），一维；会被排序，必须无重复、无 NaN、至少 4 个点。
    ytm_pct : array-like
        对应的到期收益率，**百分数**，与 ``tenors`` 等长。
    tau0 : float | None
        额外指定的 τ 起点（年）。``None`` 时用配置 ``curve.ns_tau0``；
        无论是否指定，网格里都包含任务口径要求的 ``{0.5, 1, 2, 4, 8}``。
    weights : None | "inverse_maturity_bucket" | array-like
        拟合权重；``None`` 等权，字符串见 :mod:`ficc.curve._common`，
        也可直接传与期限点等长的正权重数组。权重不改变残差口径（bp，未加权）。
    max_nfev : int
        有界精修的求值次数上限（默认 40，足够 4 参数问题收敛）。
    date : datetime.date | None
        该曲线对应的交易日，写入结果便于面板汇总。

    Returns
    -------
    NSResult
        含参数、拟合值、bp 残差、RMSE/MAE、成功标记与消息。

    Raises
    ------
    ValueError
        输入为空、长度不一致、含 NaN/inf、期限重复、有效期限点少于
        ``curve.min_valid_tenors``（4）、``tau0`` 越界、权重非法时。

    Examples
    --------
    >>> import numpy as np
    >>> from ficc.curve.ns import fit_ns
    >>> tenors = np.array([0.25, 0.5, 1, 2, 3, 5, 7, 10, 15, 20, 30], dtype=float)
    >>> ytm = 3.0 - 1.5 * (1 - np.exp(-tenors / 2.0)) / (tenors / 2.0)  # b0=3, b1=-1.5, b2=0
    >>> result = fit_ns(tenors, ytm)
    >>> result.rmse_bp < 1e-6
    True
    """
    t, y, w = prepare_inputs(tenors, ytm_pct, weights)

    if tau0 is not None:
        tau0 = float(tau0)
        if not np.isfinite(tau0) or not (TAU_MIN <= tau0 <= TAU_MAX):
            raise ValueError(f"tau0 必须在 [{TAU_MIN}, {TAU_MAX}] 区间内（年），实际 {tau0}")
        extra = (tau0,)
    else:
        extra = (float(get_config().curve.ns_tau0),)

    grid = tau_grid_ns(extra=extra)
    coef, tau_best, _, _ = project_grid("ns", t, y[:, None], w, tau_starts=grid)
    x0 = np.concatenate([coef[0], tau_best[0]])
    params, success, message = polish("ns", t, y, w, x0, max_nfev=max_nfev)

    fitted = _ns_rate(params, t)
    residuals, rmse_bp, mae_bp = residual_metrics(fitted, y)
    return NSResult(
        params=params_dict("ns", params),
        fitted=fitted,
        residuals=residuals,
        rmse_bp=rmse_bp,
        mae_bp=mae_bp,
        success=success,
        message=message,
        tenors=t,
        date=date,
        weights=w,
    )
