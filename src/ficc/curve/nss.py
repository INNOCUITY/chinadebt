"""Svensson（Svensson / NSS）曲线拟合：在 NS 基础上加第二驼峰。

模型
----
::

    y(t) = b0 + b1·(1-e^{-t/τ1})/(t/τ1) + b2·[(1-e^{-t/τ1})/(t/τ1) - e^{-t/τ1}]
             + b3·[(1-e^{-t/τ2})/(t/τ2) - e^{-t/τ2}]

相比 NS 多了 ``(b3, τ2)`` 一组参数，可以刻画"双驼峰 / 中段凹陷"这类 NS 表达
不了的形态（中国国债曲线在 2011、2013、2020 年都出现过）。

``τ2 > τ1`` 的约束
------------------
``τ1 = τ2`` 时两组基函数共线，参数不可识别，所以 Svensson 要求
``τ2 > τ1``。本实现：

- 粗搜网格只保留 ``τ2 > 1.15·τ1`` 的组合；
- 有界精修把 ``max(0, τ1 - τ2 + 1e-3)`` 以惩罚项形式拼进残差（系数
  ``TAU_ORDER_PENALTY = 1e3``），等价于硬约束；
- 若极端情况下精修结果仍违反约束，``NSSResult.params`` 会原样保留并在
  ``message`` 里追加说明，由调用方决定是否丢弃（面板拟合会记录 success）。

求解策略与 NS 相同：τ 二维网格 + 线性最小二乘解析求 b + 有界
:func:`scipy.optimize.least_squares` 精修。详见 :mod:`ficc.curve.ns`。
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
    tau_pairs_nss,
)
from ficc.curve._common import nss_rate as _nss_rate

__all__ = ["NSSResult", "fit_nss", "nss_rate"]


@dataclass(frozen=True)
class NSSResult:
    """单次 Svensson 拟合结果（字段口径与 :class:`ficc.curve.ns.NSResult` 一致）。

    Parameters
    ----------
    params : dict[str, float]
        参数 ``b0, b1, b2, b3, tau1, tau2``；``b*`` 单位百分数、``tau*`` 单位年。
    fitted, residuals, rmse_bp, mae_bp, success, message, tenors, date, weights
        语义同 :class:`ficc.curve.ns.NSResult`；``residuals`` 单位 **bp**。
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


def nss_rate(params: np.ndarray, t: float | np.ndarray) -> np.ndarray:
    """按 NSS 参数 ``[b0, b1, b2, b3, tau1, tau2]`` 在 ``t``（年）处求值（百分数）。"""
    return _nss_rate(np.asarray(params, dtype=float), t)


def fit_nss(
    tenors: Any,
    ytm_pct: Any,
    *,
    tau1: float | None = None,
    tau2: float | None = None,
    weights: Any = None,
    max_nfev: int = 60,
    date: date | None = None,
) -> NSSResult:
    """拟合 Svensson（NSS）曲线。

    Parameters
    ----------
    tenors : array-like
        期限（年），一维；排序后不得有重复，至少 ``curve.min_valid_tenors``（4）个点。
    ytm_pct : array-like
        到期收益率，**百分数**，与 ``tenors`` 等长。
    tau1, tau2 : float | None
        可选的 τ 起点（年）。两者都不给时使用内置二维网格（已覆盖典型组合）；
        给了一个时另一个按 1.5（τ1）/ 6.0（τ2）补齐。要求 ``tau2 > tau1``。
    weights : None | "inverse_maturity_bucket" | array-like
        拟合权重，口径同 :func:`ficc.curve.ns.fit_ns`。
    max_nfev : int
        有界精修的求值次数上限（默认 60，6 参数问题比 NS 稍难收敛）。
    date : datetime.date | None
        该曲线对应的交易日。

    Returns
    -------
    NSSResult
        含参数、拟合值、bp 残差、RMSE/MAE、成功标记与消息。

    Raises
    ------
    ValueError
        输入非法（空/长度不一致/NaN/重复期限/点太少）、``tau1/tau2`` 越界，
        或给定的 ``tau2 <= tau1`` 时。
    """
    t, y, w = prepare_inputs(tenors, ytm_pct, weights)

    extra: tuple[tuple[float, float], ...] = ()
    if tau1 is not None or tau2 is not None:
        a = float(1.5 if tau1 is None else tau1)
        b = float(6.0 if tau2 is None else tau2)
        for name, value in (("tau1", a), ("tau2", b)):
            if not np.isfinite(value) or not (TAU_MIN <= value <= TAU_MAX):
                raise ValueError(f"{name} 必须在 [{TAU_MIN}, {TAU_MAX}] 区间内（年），实际 {value}")
        if b <= a:
            raise ValueError(f"要求 tau2({b}) > tau1({a})")
        extra = ((a, b),)
    else:
        # 用配置里的 nss_tau1（默认 6.0）作为一个显式起点的第二 τ，第一 τ 取 NS 默认
        cfg_tau = float(get_config().curve.nss_tau1)
        if TAU_MIN <= cfg_tau <= TAU_MAX:
            extra = ((float(get_config().curve.ns_tau0), cfg_tau),)

    grid = tau_pairs_nss(extra=extra)
    coef, tau_best, _, _ = project_grid("nss", t, y[:, None], w, tau_starts=grid)
    x0 = np.concatenate([coef[0], tau_best[0]])
    params, success, message = polish("nss", t, y, w, x0, max_nfev=max_nfev)

    fitted = _nss_rate(params, t)
    residuals, rmse_bp, mae_bp = residual_metrics(fitted, y)
    if params[5] <= params[4]:
        message = f"{message} | 警告: 精修后 tau2({params[5]:.4f}) <= tau1({params[4]:.4f})"
    return NSSResult(
        params=params_dict("nss", params),
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
