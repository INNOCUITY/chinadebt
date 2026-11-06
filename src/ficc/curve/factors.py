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
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from ficc.curve.fitting import _normalize_panel

__all__ = ["factor_jump_sensitivity", "observable_factors"]

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
