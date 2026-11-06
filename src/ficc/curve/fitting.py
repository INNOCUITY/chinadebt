"""曲线拟合的高层接口：单日 :func:`fit_curve`、面板 :func:`fit_panel`、残差汇总。

面板返回"两个产品"，必须分开验收（R01/R02 评审后的口径）
-------------------------------------------------------------
NS/NSS 在 11 个期限点上只有 4–6 个参数，存在**严重的参数不可识别**：对形态平滑
的曲线，"b0 很大 + b1 很负"与"b0 很小 + b1 很正"可以给出几乎相同的拟合。因此
:func:`fit_panel` 同时计算两套拟合，并**分别**输出参数、拟合值、残差与误差指标：

1. **自由拟合（free）**：τ 与全部系数按任务口径边界 ``b0∈[0,20]``、``b∈[-20,20]``
   用**精确箱式约束最小二乘**求解。它的残差是"NS/NSS 能不能表达这条曲线"的
   **模型表达能力**度量（`docs/01_project_plan.md` 决策 1c 的"再拟合误差就是结论"）。
   实测全样本 NSS RMSE 中位约 3.3bp —— 这是**自由拟合**的数字，不能拿来背书
   锚定曲线。
2. **锚定拟合（anchored/stable）**：``b0`` 用长端观测锚定、``b1/b2/b3∈[-15,15]``，
   同样用精确有界最小二乘求解。它的参数连续、可解释，但再拟合误差明显更大
   （实测 NSS RMSE 中位约 4.76bp）。用途是"把曲线写成一组稳定参数"，**不是**
   拟合精度指标。

两套结果都落在面板列里：``rmse_free_bp`` / ``rmse_anchored_bp`` 等分别给出，
``rmse_bp`` 只是"当前 ``residuals_mode`` 选中的那套"的别名，便于 pipeline 直接取用。
``residuals_mode`` 决定 ``fit_panel`` 返回的残差矩阵属于哪一套；诊断脚本会把
两套残差/拟合值/参数/指标**分别落盘**。

关于"用模型参数做因子"（R02 的结论）
----------------------------------
**不要把 b1/b2/b3/τ 的原始序列直接当因子**。锚定只固定了 b0，b1 仍会在近似
等价的解之间跳变（评审复现：2023-10-11→10-12 各节点收益率最大只动 0.04pp，
b1 却从 -2.396 跳到 -0.923，约 147bp）。因此：

- 需要**当期可交易因子**时，用 :func:`ficc.curve.factors.observable_factors`
  （level/slope/curvature 直接由观测期限计算，无识别性问题）；
- 要用参数化因子，必须走**因果**方案（本轮不实现）：① 固定训练窗口估计
  ``(τ1, τ2)`` 后**冻结**，逐日只做线性系数回归；或 ② 状态空间/卡尔曼滤波，
  把参数当隐状态做平滑与先验约束。两者都避免"每天在多个等价解里挑一个"。

参数连续性校验（``unstable`` / ``unstable_reason``）覆盖**所有实际使用的参数**
（b0/b1/b2/b3/τ1/τ2），阈值取"长期尺度的 k 倍"（见 :func:`_parameter_jump_flags`），
被标记的日期**保留数据、不静默剔除**。
"""

from __future__ import annotations

from collections.abc import Iterable
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from ficc.config import get_config
from ficc.curve._common import (
    coef_names,
    design_matrix,
    long_end_b0_anchor,
    model_rate,
    polish,
    prepare_inputs,
    project_grid,
    project_grid_anchored,
    resolve_weights,
)
from ficc.curve.curve import Curve, CurveMethod
from ficc.curve.ns import NSResult, fit_ns
from ficc.curve.nss import NSSResult, fit_nss
from ficc.logging import get_logger

__all__ = ["fit_curve", "fit_panel", "residual_summary"]

logger = get_logger(__name__)

_FITTABLE_METHODS: tuple[str, ...] = ("ns", "nss")
_INTERP_METHODS: tuple[str, ...] = ("linear", "hermite")
_RESIDUAL_MODES: tuple[str, ...] = ("free", "anchored")

# 参数跳变判定：|Δparam| > k × 长期尺度即标记。
# "长期尺度"用一阶差分的中位绝对偏差（MAD）稳健估计，避免被跳变本身污染。
PARAM_JUMP_SCALE_FACTOR: float = 8.0
COEF_JUMP_FLOOR: float = 0.05  # 百分点；系数日变动小于 5bp 不标记
TAU_JUMP_FLOOR: float = float(np.log(1.5))  # τ 相对变动 50% 才可能标记


# ---------------------------------------------------------------------------
# 单日拟合
# ---------------------------------------------------------------------------
def fit_curve(
    tenors: Any,
    ytm_pct: Any,
    *,
    method: CurveMethod = "nss",
    as_of: date | None = None,
    **kwargs: Any,
) -> Curve:
    """对单条曲线拟合/构造 :class:`Curve`。

    Parameters
    ----------
    tenors : array-like
        期限（年），一维；排序后不得重复，至少 ``curve.min_valid_tenors``（4）个点。
    ytm_pct : array-like
        到期收益率，**百分数**，与 ``tenors`` 等长。
    method : {"linear", "hermite", "ns", "nss"}
        ``ns``/``nss`` 走参数化拟合（返回的 ``Curve.params`` 带参数）；
        ``linear``/``hermite`` 只做输入校验并保存节点，求值时插值。
    as_of : datetime.date | None
        曲线日期；``None`` 时用当天（``date.today()``）。建议显式传入以保证可复现。
    **kwargs
        透传给 :func:`ficc.curve.ns.fit_ns` / :func:`ficc.curve.nss.fit_nss`
        （如 ``weights``、``tau0`` / ``tau1`` / ``tau2``、``max_nfev``、``date``）。

    Returns
    -------
    Curve
        ``method="ns"/"nss"`` 时 ``Curve.rates`` 是**自由拟合值**（不是原始观测值），
        原始观测的残差请用 ``NSResult.residuals`` / :func:`residual_summary` 查看。
        单日 API 是自由估计（残差最优、但 τ/b 可能不唯一）；需要连续参数序列做因子
        时请用 :func:`fit_panel` 的锚定参数，或直接用
        :func:`ficc.curve.factors.observable_factors`。

    Raises
    ------
    ValueError
        method 非法，或底层拟合的输入校验失败。
    TypeError
        给插值方法传了不支持的 ``kwargs``。
    """
    method_str = str(method).lower()
    if method_str not in (*_FITTABLE_METHODS, *_INTERP_METHODS):
        raise ValueError(
            f"method 必须是 {(*_FITTABLE_METHODS, *_INTERP_METHODS)} 之一，实际 {method!r}"
        )
    as_of_date = date.today() if as_of is None else as_of

    params: dict[str, float] = {}
    if method_str == "ns":
        result_ns = fit_ns(tenors, ytm_pct, **kwargs)
        tenors_used, rates, params = result_ns.tenors, result_ns.fitted, dict(result_ns.params)
    elif method_str == "nss":
        result_nss = fit_nss(tenors, ytm_pct, **kwargs)
        tenors_used, rates, params = result_nss.tenors, result_nss.fitted, dict(result_nss.params)
    else:
        weights = kwargs.pop("weights", None)
        if kwargs:
            raise TypeError(f"method={method_str!r} 是插值方法，不支持的参数: {sorted(kwargs)}")
        tenors_used, rates, _ = prepare_inputs(tenors, ytm_pct, weights)

    assert tenors_used is not None  # 拟合结果一定带期限
    return Curve(
        as_of=as_of_date,
        tenors=np.asarray(tenors_used, dtype=float),
        rates=np.asarray(rates, dtype=float),
        method=method_str,  # type: ignore[arg-type]
        params=params,
    )


# ---------------------------------------------------------------------------
# 面板拟合
# ---------------------------------------------------------------------------
def fit_panel(
    panel: pd.DataFrame,
    method: str = "nss",
    *,
    return_residuals: bool = True,
    weights: Any = None,
    param_mode: str = "stable",
    residuals_mode: str = "free",
    refine: bool = False,
    max_nfev: int = 40,
    progress: bool = False,
) -> pd.DataFrame | tuple[pd.DataFrame, pd.DataFrame]:
    """逐日拟合整条面板，返回 ``(参数面板, 残差面板)``（两个产品口径见模块 docstring）。

    Parameters
    ----------
    panel : pandas.DataFrame
        **宽表**：``index = DatetimeIndex(交易日)``，``columns = 期限（年，float）``，
        ``values = 到期收益率（百分数）``。缺失期限保持 ``NaN``（不插值、不填充），
        每个交易日只要有效期限点不少于 ``curve.min_valid_tenors``（4）就会拟合。
        也接受 ``date`` 列代替 DatetimeIndex。
    method : {"ns", "nss"}
        拟合模型。插值方法（linear/hermite）没有参数面板，故不支持。
    return_residuals : bool
        ``True``（默认，pipeline 口径）返回二元组 ``(params_df, residuals_df)``；
        ``False`` 只返回 ``params_df``。
    weights : None | "equal" | "inverse_maturity_bucket" | array-like
        拟合权重；``None``（默认）表示**使用配置** ``cfg.curve.weights``
        （当前为 ``inverse_maturity_bucket``），保证产物与 config 口径一致。
        需要等权请显式传 ``"equal"``。权重按每个交易日的有效期限点解析。
    param_mode : {"stable", "free"}
        ``"stable"``（默认）：参数面板取**锚定拟合**（``b0`` 用长端观测锚定、
        ``b1/b2/b3∈[-15,15]``、τ 网格搜索），参数连续可解释、但再拟合误差更大；
        ``"free"``：参数面板取自由拟合（与自由残差同一组参数），拟合最优但参数
        不可识别、日间会跳。两套结果的关键指标都会同时出现在面板列里。
    residuals_mode : {"free", "anchored"}
        ``fit_panel`` **返回的残差矩阵**（以及 ``rmse_bp/mae_bp/max_abs_bp``）
        取自哪一套拟合。默认 ``"free"``（模型表达能力口径，pipeline 用这套）。
        要验收锚定曲线请显式传 ``"anchored"``，不要用自由拟合的 3.3bp 去背书它。
    refine : bool
        是否在自由拟合的 τ 网格粗搜之后逐日做有界非线性精修。默认 ``False``：
        τ 网格已足够密（NS 约 140 点、NSS 约 1200 对），且每个格点上的系数都是
        **精确有界最小二乘**；精修只影响 τ 的连续性（不改变"触界要重解"这一约束
        最优性，但会微调 τ）。
    max_nfev : int
        ``refine=True`` 时逐日精修的求值次数上限（默认 40）。
    progress : bool
        是否每完成一个缺失模式打一条 INFO 日志（诊断脚本用）。

    Returns
    -------
    params_df : pandas.DataFrame
        ``index`` 为日期（``DatetimeIndex``，名字 ``date``）。列：

        - ``ns``：``b0, b1, b2, tau1, b0_free``
        - ``nss``：``b0, b1, b2, b3, tau1, tau2, b0_free``
        - 误差指标（全部 bp）：``rmse_bp/mae_bp/max_abs_bp``（当前
          ``residuals_mode`` 选中那套）、``rmse_free_bp/mae_free_bp/max_abs_free_bp``、
          ``rmse_anchored_bp/mae_anchored_bp/max_abs_anchored_bp``、
          ``rmse_stable_bp``（= 锚定口径的别名，向后兼容）
        - 诊断：``n_points, success, unstable, unstable_reason, message``

        ``b*`` 单位百分数，``tau*`` 单位年；``b0..tau`` 来自 ``param_mode`` 选中的
        那套，``b0_free`` 恒为自由估计的水平因子。``unstable`` 按**所有实际使用的
        参数**做相对跳变判定，``unstable_reason`` 列出触发参数（如 ``"b1+tau1"``），
        被标记的日期**保留、不剔除**。
    residuals_df : pandas.DataFrame
        ``index`` 与 ``params_df`` 相同，``columns`` 与输入 ``panel.columns`` 逐位一致
        （期限，年），数值为 ``(拟合值 - 观测值) × 100``，**单位 bp**；
        该交易日缺失的期限保持 ``NaN``。来自 ``residuals_mode`` 指定的那套拟合。

    Notes
    -----
    用模型参数做因子的**因果方案**（本轮不实现，供下一轮参考）：① 固定训练窗口
    估计 ``(τ1, τ2)`` 后**冻结**，逐日只做线性系数回归（参数不再换解）；
    ② 状态空间/卡尔曼：把参数当隐状态，用先验 + 观测做平滑（可给出不确定性）。
    两者都需要避免"每天在多个等价解里挑一个"，否则因子会像 b1 一样跳变。

    **最差拟合日**：R01 修复前，最差日残差里混入了"系数裁剪不是有界最小二乘"
    的求解误差（评审复现 2002-04-26 156.0 → 93.8bp、2004-08-17 76.7 → 52.9bp）。
    修复后仍有偏大的日子（曲线剧烈畸变、极端形态），但**不得笼统归因于"数学上
    无法表达"**，需逐案判断是数据异常、样本券问题还是模型局限。这些日期保留在
    ``reports/curve_fit_bad_dates.csv`` 里供报告讨论。

    Raises
    ------
    TypeError
        ``panel`` 不是 DataFrame。
    ValueError
        ``panel`` 为空、列不是数值期限、存在 inf、method 非法、``param_mode`` /
        ``residuals_mode`` 非法，或没有任何交易日满足最少期限点要求。
    """
    method_str = str(method).lower()
    if method_str not in _FITTABLE_METHODS:
        raise ValueError(f"fit_panel 仅支持 {_FITTABLE_METHODS}，实际 {method!r}")
    mode = str(param_mode).lower()
    if mode not in ("stable", "free"):
        raise ValueError(f"param_mode 必须是 'stable' 或 'free'，实际 {param_mode!r}")
    rmode = str(residuals_mode).lower()
    if rmode not in _RESIDUAL_MODES:
        raise ValueError(f"residuals_mode 必须是 {_RESIDUAL_MODES}，实际 {residuals_mode!r}")

    frame = _normalize_panel(panel)
    n_tenors = frame.shape[1]
    n_dates = frame.shape[0]
    tenors_all = frame.columns.to_numpy(dtype=float)
    values = frame.to_numpy(dtype=float)

    cfg = get_config()
    min_valid = int(cfg.curve.min_valid_tenors)
    weights_spec = cfg.curve.weights if weights is None else weights
    finite = np.isfinite(values)
    counts = finite.sum(axis=1)

    n_linear = len(coef_names(method_str))
    n_tau = 1 if method_str == "ns" else 2
    coef = np.full((n_dates, n_linear), np.nan)
    taus = np.full((n_dates, n_tau), np.nan)
    success = np.zeros(n_dates, dtype=bool)
    messages = np.full(n_dates, "", dtype=object)
    residuals_free = np.full((n_dates, n_tenors), np.nan)
    residuals_anchored = np.full((n_dates, n_tenors), np.nan)

    stable_b0 = np.full(n_dates, np.nan)
    stable_coef = np.full((n_dates, n_linear - 1), np.nan)
    stable_tau = np.full((n_dates, n_tau), np.nan)

    valid_rows = np.flatnonzero(counts >= min_valid)
    too_few = counts < min_valid
    messages[too_few] = f"有效期限点不足 {min_valid} 个"
    if valid_rows.size == 0:
        raise ValueError(
            f"panel 中没有任何交易日满足最少 {min_valid} 个有效期限点；"
            f"有效点数的最大值只有 {int(counts.max())}"
        )

    # 按缺失模式分组：同一模式下所有交易日共用同一套因子载荷，可向量化粗搜
    patterns, inverse = np.unique(finite[valid_rows], axis=0, return_inverse=True)
    for group in range(patterns.shape[0]):
        rows = valid_rows[inverse == group]
        cols = np.flatnonzero(patterns[group])
        tenors = tenors_all[cols]
        y_matrix = values[np.ix_(rows, cols)].T  # (n_tenors, n_group)
        weights_arr = resolve_weights(tenors, weights_spec)

        # --- 产品 1：自由拟合（τ 与系数全放开；残差=模型表达能力口径） ---
        coef_g, tau_g, _, _ = project_grid(method_str, tenors, y_matrix, weights_arr)
        for j, row in enumerate(rows):
            x0 = np.concatenate([coef_g[j], tau_g[j]])
            if refine:
                x, ok, msg = polish(
                    method_str, tenors, y_matrix[:, j], weights_arr, x0, max_nfev=max_nfev
                )
            else:
                x, ok, msg = x0, True, "bounded-τ-grid"
            fitted_j = np.asarray(model_rate(method_str, x, tenors), dtype=float)
            residuals_free[row, cols] = (fitted_j - y_matrix[:, j]) * 100.0
            coef[row] = x[:n_linear]
            taus[row] = x[n_linear:]
            success[row] = ok
            messages[row] = msg

        # --- 产品 2：锚定拟合（b0 长端锚定、系数精确有界最小二乘） ---
        anchors = np.array(
            [long_end_b0_anchor(tenors, y_matrix[:, j]) for j in range(y_matrix.shape[1])],
            dtype=float,
        )
        coef_a, tau_a, _, _ = project_grid_anchored(
            method_str, tenors, y_matrix, weights_arr, anchors
        )
        stable_b0[rows] = anchors
        stable_coef[rows] = coef_a
        stable_tau[rows] = tau_a
        for j, row in enumerate(rows):
            fitted_a = anchors[j] + np.asarray(
                model_rate_anchored(method_str, coef_a[j], tau_a[j], tenors), dtype=float
            )
            residuals_anchored[row, cols] = (fitted_a - y_matrix[:, j]) * 100.0

        if progress:
            logger.info(
                "面板拟合 %s: 缺失模式 %d/%d 完成（本组 %d 个交易日）",
                method_str,
                group + 1,
                patterns.shape[0],
                rows.size,
            )
    if progress:
        logger.info(
            "面板拟合 %s 完成: %d 个交易日, %d 种缺失模式", method_str, n_dates, patterns.shape[0]
        )

    params_df = _build_params_frame(
        frame.index,
        method_str,
        mode,
        rmode,
        coef,
        taus,
        residuals_free,
        residuals_anchored,
        stable_b0,
        stable_coef,
        stable_tau,
        success,
        messages,
        counts,
    )
    params_df.attrs["method"] = method_str
    params_df.attrs["param_mode"] = mode
    params_df.attrs["residuals_mode"] = rmode
    params_df.attrs["weights"] = str(weights_spec)
    if not return_residuals:
        return params_df

    residuals = residuals_free if rmode == "free" else residuals_anchored
    residuals_df = pd.DataFrame(
        residuals,
        index=frame.index,
        columns=pd.Index(tenors_all, name="tenor"),
    )
    residuals_df.index.name = "date"
    residuals_df.attrs["method"] = method_str
    residuals_df.attrs["residuals_mode"] = rmode
    residuals_df.attrs["weights"] = str(weights_spec)
    residuals_df.attrs["unit"] = "bp"
    return params_df, residuals_df


# ---------------------------------------------------------------------------
# 面板内部工具
# ---------------------------------------------------------------------------
def model_rate_anchored(
    method: str, coef: np.ndarray, tau_vec: np.ndarray, tenors: np.ndarray
) -> np.ndarray:
    """按"锚定 b0 + 其余系数"求值（不含 b0，调用方需自行加回锚定 b0）。"""
    x = design_matrix(method, tenors, tau_vec)
    return x[:, 1:] @ np.asarray(coef, dtype=float)


def _normalize_panel(panel: pd.DataFrame) -> pd.DataFrame:
    """校验并把面板规范成 ``DatetimeIndex × 升序 float 期限`` 的宽表。"""
    if not isinstance(panel, pd.DataFrame):
        raise TypeError(f"panel 必须是 pandas.DataFrame，实际 {type(panel).__name__}")
    if panel.empty:
        raise ValueError("panel 为空：没有可拟合的曲线")
    frame = panel.copy()

    if isinstance(frame.index, pd.DatetimeIndex):
        pass
    elif "date" in frame.columns:
        frame = frame.set_index("date")
        frame.index = pd.to_datetime(frame.index, errors="raise")
    else:
        try:
            frame.index = pd.to_datetime(frame.index, errors="raise")
        except (ValueError, TypeError) as exc:
            raise ValueError(f"panel 的 index 无法解析为日期: {exc}") from exc

    try:
        cols = pd.Index([float(c) for c in frame.columns], dtype=float, name="tenor")
    except (TypeError, ValueError) as exc:
        raise ValueError(f"panel 的列必须是数值期限（年）: {exc}") from exc
    if not np.all(np.isfinite(cols.to_numpy(dtype=float))):
        raise ValueError("panel 的期限列含 NaN/inf")
    if cols.duplicated().any():
        raise ValueError("panel 的期限列存在重复")
    frame.columns = cols
    frame = frame.sort_index(axis=1)

    values = frame.to_numpy(dtype=float)
    if np.isinf(values).any():
        raise ValueError("panel 含 inf，收益率不可能是无穷大；请先清洗数据")
    frame.index = pd.DatetimeIndex(frame.index, name="date")
    return frame.sort_index()


def _residual_metrics(residuals: np.ndarray) -> dict[str, np.ndarray]:
    """由残差矩阵（bp）算逐日 RMSE / MAE / 最大绝对残差。"""
    has_resid = np.isfinite(residuals).any(axis=1)
    rmse = np.full(residuals.shape[0], np.nan)
    mae = np.full(residuals.shape[0], np.nan)
    max_abs = np.full(residuals.shape[0], np.nan)
    if np.any(has_resid):
        block = residuals[has_resid]
        rmse[has_resid] = np.sqrt(np.nanmean(np.square(block), axis=1))
        mae[has_resid] = np.nanmean(np.abs(block), axis=1)
        max_abs[has_resid] = np.nanmax(np.abs(block), axis=1)
    return {"rmse": rmse, "mae": mae, "max_abs": max_abs}


def _parameter_jump_flags(
    values: dict[str, np.ndarray | list[str]],
    params: list[str],
    *,
    scale_factor: float = PARAM_JUMP_SCALE_FACTOR,
) -> tuple[np.ndarray, np.ndarray]:
    """对**所有实际使用的参数**做相对跳变判定。

    对每个参数 p，取日变动 ``|Δp|``；用其**中位绝对偏差**（MAD）估计长期尺度
    ``σ ≈ 1.4826·MAD(|Δp - median(Δp)|)``，超过 ``scale_factor·σ`` 即标记。
    τ 在**对数**尺度上判定（相对变动）；系数另设 5bp 的下限，避免平稳期把
    微小变动误标成跳变。

    注意：尺度由样本内差分估计，只用于**诊断标记**（不参与拟合、不作交易信号），
    因此不构成前视；被标记的日期保留在面板里，不静默剔除。
    """
    n = len(np.asarray(values["b0"], dtype=float))
    unstable = np.zeros(n, dtype=bool)
    reasons = np.full(n, "", dtype=object)
    for name in params:
        series = np.asarray(values[name], dtype=float)
        if name.startswith("tau"):
            transformed = np.log(np.clip(series, 1e-6, None))
            diffs = np.abs(np.diff(transformed))
            floor = TAU_JUMP_FLOOR
        else:
            diffs = np.abs(np.diff(series))
            floor = COEF_JUMP_FLOOR
        finite = np.isfinite(diffs)
        if not np.any(finite):
            continue
        med = float(np.median(diffs[finite]))
        mad = float(np.median(np.abs(diffs[finite] - med)))
        scale = 1.4826 * mad
        threshold = max(scale_factor * scale, floor)
        flags = np.zeros(n, dtype=bool)
        flags[1:] = finite & (diffs > threshold)
        for i in np.flatnonzero(flags):
            unstable[i] = True
            reasons[i] = f"{reasons[i]}+{name}" if reasons[i] else name
    return unstable, reasons


def _build_params_frame(
    index: pd.DatetimeIndex,
    method: str,
    param_mode: str,
    residuals_mode: str,
    coef: np.ndarray,
    taus: np.ndarray,
    residuals_free: np.ndarray,
    residuals_anchored: np.ndarray,
    stable_b0: np.ndarray,
    stable_coef: np.ndarray,
    stable_tau: np.ndarray,
    success: np.ndarray,
    messages: np.ndarray,
    counts: np.ndarray,
) -> pd.DataFrame:
    """拼参数面板：主参数按 ``param_mode`` 选，两套误差指标并列，跳变标记全覆盖。"""
    data: dict[str, Any] = {}
    if param_mode == "stable":
        data["b0"] = stable_b0
        coef_mat = stable_coef
        tau_mat = stable_tau
    else:
        data["b0"] = coef[:, 0]
        coef_mat = coef[:, 1:]
        tau_mat = taus
    for i, name in enumerate(coef_names(method)[1:]):
        data[name] = coef_mat[:, i]
    tau_labels = ["tau1"] if method == "ns" else ["tau1", "tau2"]
    for i, name in enumerate(tau_labels):
        data[name] = tau_mat[:, i]
    data["b0_free"] = coef[:, 0]

    free_metrics = _residual_metrics(residuals_free)
    anchored_metrics = _residual_metrics(residuals_anchored)
    selected = free_metrics if residuals_mode == "free" else anchored_metrics
    data["rmse_bp"] = selected["rmse"]
    data["mae_bp"] = selected["mae"]
    data["max_abs_bp"] = selected["max_abs"]
    data["rmse_free_bp"] = free_metrics["rmse"]
    data["mae_free_bp"] = free_metrics["mae"]
    data["max_abs_free_bp"] = free_metrics["max_abs"]
    data["rmse_anchored_bp"] = anchored_metrics["rmse"]
    data["mae_anchored_bp"] = anchored_metrics["mae"]
    data["max_abs_anchored_bp"] = anchored_metrics["max_abs"]
    data["rmse_stable_bp"] = anchored_metrics["rmse"]  # 向后兼容别名
    data["n_points"] = counts.astype(int)

    used_params = list(coef_names(method)) + list(tau_labels)
    unstable, reasons = _parameter_jump_flags(data, used_params)
    data["success"] = success
    data["unstable"] = unstable
    data["unstable_reason"] = [(f"参数跳变: {reason}" if reason else "") for reason in reasons]
    msgs: list[str] = []
    for i, message in enumerate(messages):
        text = str(message)
        if reasons[i]:
            text = f"{text} | 参数跳变: {reasons[i]}"
        msgs.append(text)
    data["message"] = msgs

    frame = pd.DataFrame(data, index=index)
    frame.index.name = "date"
    return frame


# ---------------------------------------------------------------------------
# 残差汇总
# ---------------------------------------------------------------------------
def residual_summary(results: Iterable[NSResult | NSSResult]) -> pd.DataFrame:
    """把多次拟合的残差按期限汇总成统计表（**单位 bp**）。

    Parameters
    ----------
    results : Iterable[NSResult | NSSResult]
        单日拟合结果；必须带 ``tenors`` 字段（``fit_ns``/``fit_nss`` 的输出都有）。

    Returns
    -------
    pandas.DataFrame
        ``index = tenor``（年），列：

        - ``n``：该期限的残差样本数；
        - ``mean_bp`` / ``std_bp`` / ``median_bp``：均值、总体标准差、中位数；
        - ``p05_bp`` / ``p95_bp``：5% / 95% 分位；
        - ``rmse_bp``：该期限的均方根残差；
        - ``max_abs_bp``：最大绝对残差。

    Raises
    ------
    ValueError
        输入为空，或某个结果缺少 ``tenors`` 且长度与残差对不上。
    """
    tenors_list: list[np.ndarray] = []
    resid_list: list[np.ndarray] = []
    for result in results:
        if result is None:
            continue
        resid = np.asarray(result.residuals, dtype=float).ravel()
        if result.tenors is not None:
            tenors = np.asarray(result.tenors, dtype=float).ravel()
        else:  # pragma: no cover - fit_* 一定带 tenors
            default = np.asarray(get_config().curve.pca_tenors, dtype=float)
            if default.size != resid.size:
                raise ValueError(
                    "某个拟合结果缺少 tenors，且残差长度与配置的标准期限数不一致，"
                    "无法判断残差对应期限"
                )
            tenors = default
        if tenors.size != resid.size:  # pragma: no cover - 防御式检查
            raise ValueError(f"tenors 长度 {tenors.size} 与残差长度 {resid.size} 不一致")
        tenors_list.append(tenors)
        resid_list.append(resid)

    if not tenors_list:
        raise ValueError("residual_summary 输入为空：没有任何拟合结果")

    frame = pd.DataFrame(
        {
            "tenor": np.concatenate(tenors_list),
            "residual_bp": np.concatenate(resid_list),
        }
    )
    grouped = frame.groupby("tenor")["residual_bp"]
    summary = pd.DataFrame(
        {
            "n": grouped.size().astype(int),
            "mean_bp": grouped.mean(),
            "std_bp": grouped.std(ddof=0),
            "median_bp": grouped.median(),
            "p05_bp": grouped.quantile(0.05),
            "p95_bp": grouped.quantile(0.95),
            "rmse_bp": np.sqrt(grouped.apply(lambda s: float(np.mean(np.square(s))))),
            "max_abs_bp": grouped.apply(lambda s: float(np.max(np.abs(s)))),
        }
    )
    summary.index.name = "tenor"
    return summary
