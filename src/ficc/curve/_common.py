"""NS / NSS 拟合的公共数值内核（模块私有，不属于公开 API）。

为什么单独抽一层
----------------
`ns.py`、`nss.py`、`fitting.py` 都需要同一套东西：因子载荷、输入校验、
权重口径、残差口径、τ 网格、带边界的局部精修。集中放在这里，保证
"单日拟合"与"面板拟合"用的是**同一个目标函数**，不会出现两条路径给出不同答案。

关键数值事实
------------
1. NS/NSS 对系数 ``b0/b1/b2(,b3)`` 是**线性**的，只对 τ 非线性。因此求最优的
   高效做法是"投影（variable projection）"：给定 τ，用线性最小二乘解析解出 b，
   再只在 1 维（NS）或 2 维（NSS）的 τ 空间上搜索。这样既没有 4–6 维非线性
   优化的局部极小问题，速度也快一到两个数量级。
2. 我们在 τ 空间用**对数网格粗搜**（其中显式包含任务口径要求的
   ``τ0 ∈ {0.5, 1, 2, 4, 8}``），最后再用 :func:`scipy.optimize.least_squares`
   在**完整参数空间 + 任务规定的边界**下做一次有界精修。粗搜保证全局性，
   精修保证参数精度与边界约束，二者取长补短。
3. ``t/τ → 0`` 时 ``(1-e^{-x})/x`` 是 0/0，用量级 ``x < 1e-6`` 的泰勒展开
   ``1 - x/2 + x²/6`` 处理（对应的 ``L2 ≈ x/2 - x²/3``），避免 NaN/除零。

单位约定（跨模块唯一口径）
--------------------------
- 收益率入参 ``ytm_pct`` 是**百分数**（``2.35`` 表示 2.35%）；
- 所有对外暴露的残差 / RMSE / MAE 一律换算成 **bp**（``×100``）；
- 权重 ``w`` 只改变拟合目标（``sqrt(w)`` 加权最小二乘），不改变残差口径。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from typing import Literal

import numpy as np
from scipy.optimize import least_squares

from ficc.config import get_config

# 参数边界：与任务口径严格一致（b0∈[0,20]，b1/b2/b3∈[-20,20]，τ∈[0.05,30] 年）。
# 这是**自由拟合**（用于测"模型表达能力"）的边界；面板因子提取另走"锚定 b0"的
# 稳定化估计（见 project_grid_anchored），因为自由拟合存在严重的参数不可识别。
NS_LOWER: np.ndarray = np.array([0.0, -20.0, -20.0, 0.05])
NS_UPPER: np.ndarray = np.array([20.0, 20.0, 20.0, 30.0])
NSS_LOWER: np.ndarray = np.array([0.0, -20.0, -20.0, -20.0, 0.05, 0.05])
NSS_UPPER: np.ndarray = np.array([20.0, 20.0, 20.0, 20.0, 30.0, 30.0])

# 稳定化估计（面板因子提取）用的系数边界：b1/b2/b3 限制在 [-15, 15]，
# b0 由**长端锚定**给定（见 long_end_b0_anchor）。收紧边界可排掉"b1/b2/b3 取极端值
# 硬凑拟合"的退化解，这是识别性问题最直接的止血手段。
STABLE_COEF_LOWER: float = -15.0
STABLE_COEF_UPPER: float = 15.0

# NSS 的 τ2 > τ1 约束：以惩罚项形式进入最小二乘残差（单位与残差一致，即 bp 量纲）
# 1e3 表示"τ 每违反 1 年 ≈ 1000bp 的惩罚"，在实际量级上等价于硬约束。
TAU_ORDER_PENALTY: float = 1e3

# τ 网格搜索的范围（年）：覆盖从季度到超长端的全部合理形态
TAU_MIN: float = 0.05
TAU_MAX: float = 30.0

# 权重模式
WeightSpec = Literal["inverse_maturity_bucket"] | None | Sequence[float] | np.ndarray

_METHOD_BOUNDS: dict[str, tuple[np.ndarray, np.ndarray]] = {
    "ns": (NS_LOWER, NS_UPPER),
    "nss": (NSS_LOWER, NSS_UPPER),
}
_METHOD_NPARAMS: dict[str, int] = {"ns": 4, "nss": 6}
_METHOD_NLINEAR: dict[str, int] = {"ns": 3, "nss": 4}


# ---------------------------------------------------------------------------
# 1. 因子载荷
# ---------------------------------------------------------------------------
def ns_loadings(t: np.ndarray, tau: float) -> tuple[np.ndarray, np.ndarray]:
    """Nelson-Siegel 的两个非线性载荷 ``L1``、``L2``。

    定义（``x = t/τ``）::

        L1(x) = (1 - e^{-x}) / x
        L2(x) = L1(x) - e^{-x}

    ``x → 0`` 时用泰勒展开 ``L1 ≈ 1 - x/2 + x²/6``、``L2 ≈ x/2 - x²/3``，
    避免 ``0/0``；``x`` 很大时 ``e^{-x}`` 自然下溢到 0，无溢出风险。

    Parameters
    ----------
    t : ndarray
        期限（年），一维。
    tau : float
        衰减参数 τ（年），必须为正。

    Returns
    -------
    (L1, L2) : tuple[ndarray, ndarray]
        与 ``t`` 同形状的两个载荷。
    """
    t = np.asarray(t, dtype=float)
    x = t / float(tau)
    small = np.abs(x) < 1e-6
    x_safe = np.where(small, 1.0, x)  # 把 0 替换成 1，仅为让下面的除法不报警
    l1 = np.where(small, 1.0 - x / 2.0 + x * x / 6.0, (1.0 - np.exp(-x_safe)) / x_safe)
    l2 = np.where(small, x / 2.0 - x * x / 3.0, l1 - np.exp(-x))
    return l1, l2


def ns_factor_matrix(t: np.ndarray, tau: float) -> np.ndarray:
    """NS 的线性设计矩阵 ``[1, L1(t;τ), L2(t;τ)]``，形状 ``(n, 3)``。"""
    l1, l2 = ns_loadings(t, tau)
    return np.column_stack([np.ones_like(l1), l1, l2])


def nss_factor_matrix(t: np.ndarray, tau1: float, tau2: float) -> np.ndarray:
    """Svensson 的线性设计矩阵 ``[1, L1(t;τ1), L2(t;τ1), L2(t;τ2)]``，形状 ``(n, 4)``。

    注意第二驼峰的载荷与 NS 的 ``L2`` **形式相同**，只是换成 ``τ2``。
    """
    l1, l2 = ns_loadings(t, tau1)
    l3 = ns_loadings(t, tau2)[1]
    return np.column_stack([np.ones_like(l1), l1, l2, l3])


def design_matrix(method: str, t: np.ndarray, tau_vec: np.ndarray) -> np.ndarray:
    """按方法给出线性设计矩阵；``tau_vec`` 长度 1（NS）或 2（NSS）。"""
    tau_vec = np.atleast_1d(np.asarray(tau_vec, dtype=float))
    if method == "ns":
        return ns_factor_matrix(t, tau_vec[0])
    if method == "nss":
        return nss_factor_matrix(t, tau_vec[0], tau_vec[1])
    raise ValueError(f"未知方法: {method!r}；仅支持 'ns' / 'nss'")


# ---------------------------------------------------------------------------
# 2. 曲线求值
# ---------------------------------------------------------------------------
def ns_rate(params: np.ndarray, t: float | np.ndarray) -> np.ndarray:
    """按 NS 参数 ``[b0, b1, b2, tau]`` 求 ``t`` 处的收益率（百分数）。"""
    p = np.asarray(params, dtype=float)
    l1, l2 = ns_loadings(np.asarray(t, dtype=float), p[3])
    return p[0] + p[1] * l1 + p[2] * l2


def nss_rate(params: np.ndarray, t: float | np.ndarray) -> np.ndarray:
    """按 NSS 参数 ``[b0, b1, b2, b3, tau1, tau2]`` 求 ``t`` 处的收益率（百分数）。"""
    p = np.asarray(params, dtype=float)
    l1, l2 = ns_loadings(np.asarray(t, dtype=float), p[4])
    l3 = ns_loadings(np.asarray(t, dtype=float), p[5])[1]
    return p[0] + p[1] * l1 + p[2] * l2 + p[3] * l3


def model_rate(method: str, params: np.ndarray, t: float | np.ndarray) -> np.ndarray:
    """按方法分派曲线求值，``params`` 为 ``[b..., τ...]`` 的扁平数组。"""
    if method == "ns":
        return ns_rate(params, t)
    if method == "nss":
        return nss_rate(params, t)
    raise ValueError(f"未知方法: {method!r}；仅支持 'ns' / 'nss'")


# ---------------------------------------------------------------------------
# 3. 权重
# ---------------------------------------------------------------------------
def inverse_maturity_bucket_weights(tenors: np.ndarray) -> np.ndarray:
    """``inverse_maturity_bucket`` 权重：短端权重高，长端权重低。

    中债曲线短端（≤1Y）的报价更活跃、对资金面更敏感，而 NS/NSS 在短端的
    载荷变化最快、最容易拟合不足；给短端更高权重可以让曲线在短端更贴近市场。
    分桶口径（**在本项目内固定**，改动需同步更新测试）::

        tenor ≤ 0.5  → 2.00        ≤ 1   → 1.50        ≤ 2   → 1.20
        tenor ≤ 5    → 1.00        ≤ 10  → 0.80        ≤ 20  → 0.65
        tenor > 20   → 0.50

    权重只用于加权最小二乘的目标函数，**不改变**输出的残差口径（残差仍是
    未加权的 bp 值），这样不同权重下的 rmse 可以直接横向比较。
    """
    edges = np.array([0.5, 1.0, 2.0, 5.0, 10.0, 20.0, np.inf])
    values = np.array([2.0, 1.5, 1.2, 1.0, 0.8, 0.65, 0.5])
    idx = np.searchsorted(edges, np.asarray(tenors, dtype=float), side="left")
    return values[np.clip(idx, 0, len(values) - 1)]


def resolve_weights(tenors: np.ndarray, weights: WeightSpec) -> np.ndarray:
    """把权重参数解析成与 ``tenors`` 等长的正数数组。

    ``None`` → 等权；字符串只支持 ``"inverse_maturity_bucket"``；
    也接受显式的权重数组/序列（长度必须与期限点数一致）。
    """
    n = tenors.size
    if weights is None:
        return np.ones(n, dtype=float)
    if isinstance(weights, str):
        if weights == "inverse_maturity_bucket":
            return inverse_maturity_bucket_weights(tenors)
        if weights == "equal":
            return np.ones(n, dtype=float)
        raise ValueError(
            f"未知权重模式: {weights!r}；可选 None / 'equal' / 'inverse_maturity_bucket'，"
            "也可直接传权重数组"
        )
    w = np.asarray(weights, dtype=float).ravel()
    if w.size != n:
        raise ValueError(f"权重长度 {w.size} 与期限点数 {n} 不一致")
    if not np.all(np.isfinite(w)) or np.any(w <= 0):
        raise ValueError("权重必须为有限正数")
    return w


# ---------------------------------------------------------------------------
# 4. 输入校验
# ---------------------------------------------------------------------------
def prepare_inputs(
    tenors: Sequence[float] | np.ndarray,
    ytm_pct: Sequence[float] | np.ndarray,
    weights: WeightSpec = None,
    *,
    min_tenors: int | None = None,
    sort: bool = True,
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """校验并规范化拟合输入，返回 ``(tenors, ytm_pct, weights)``。

    规则（违反则抛 ``ValueError``，消息里写明是哪一条）：

    - 空输入 → 报错；长度不一致 → 报错；
    - 含 NaN / inf → 报错（缺失必须在数据层显式处理，不能静默带入模型）；
    - 期限必须为正、且互不重复；数量不少于 ``min_tenors``
      （默认取配置 ``curve.min_valid_tenors``，即 4）；
    - ``sort=True`` 时按期限升序重排（同时重排权重），否则要求调用方已排好。

    ``sort=True`` 只是便利，不会改变观测点集合；重复期限仍然报错（同一期限
    两个价格属于数据错误，不能靠排序掩盖）。
    """
    t = np.asarray(tenors, dtype=float).ravel()
    y = np.asarray(ytm_pct, dtype=float).ravel()
    if t.size == 0 or y.size == 0:
        raise ValueError("拟合输入为空：至少需要 4 个有效期限点")
    if t.size != y.size:
        raise ValueError(f"tenors 与 ytm_pct 长度不一致: {t.size} vs {y.size}")
    if not np.all(np.isfinite(t)):
        raise ValueError("tenors 含 NaN/inf，无法拟合")
    if not np.all(np.isfinite(y)):
        raise ValueError("ytm_pct 含 NaN/inf，无法拟合；缺失值必须在数据清洗阶段显式处理")
    if np.any(t <= 0):
        raise ValueError("tenors 必须为正数（单位：年）")
    min_n = int(min_tenors) if min_tenors is not None else int(get_config().curve.min_valid_tenors)
    if t.size < min_n:
        raise ValueError(f"有效期限点太少：{t.size} 个，至少需要 {min_n} 个才能拟合该曲线")

    order = np.argsort(t, kind="stable") if sort else np.arange(t.size)
    t_sorted = t[order]
    y_sorted = y[order]
    if np.any(np.diff(t_sorted) <= 0):
        raise ValueError("tenors 存在重复值，无法拟合（同一期限只能有一个收益率）")
    if isinstance(weights, str) or weights is None:
        w = resolve_weights(t_sorted, weights)
    else:
        w_raw = np.asarray(weights, dtype=float).ravel()
        if w_raw.size != t.size:
            raise ValueError(f"权重长度 {w_raw.size} 与期限点数 {t.size} 不一致")
        w = resolve_weights(t_sorted, w_raw[order])
    return t_sorted, y_sorted, w


def residual_metrics(fitted: np.ndarray, ytm_pct: np.ndarray) -> tuple[np.ndarray, float, float]:
    """由拟合值算残差与误差指标，**统一以 bp 为单位**。

    Returns
    -------
    (residuals_bp, rmse_bp, mae_bp)
        ``residuals_bp = (fitted - observed) * 100``，带符号；RMSE/MAE 取绝对值口径。
    """
    resid_bp = (np.asarray(fitted, dtype=float) - np.asarray(ytm_pct, dtype=float)) * 100.0
    rmse_bp = float(np.sqrt(np.mean(np.square(resid_bp))))
    mae_bp = float(np.mean(np.abs(resid_bp)))
    return resid_bp, rmse_bp, mae_bp


def weighted_sse(residuals_bp: np.ndarray, weights: np.ndarray) -> float:
    """加权残差平方和（残差已是 bp，权重对齐后可跨起点比较优劣）。"""
    return float(np.sum(np.asarray(weights, dtype=float) * np.square(residuals_bp)))


# ---------------------------------------------------------------------------
# 5. τ 网格
# ---------------------------------------------------------------------------
def tau_grid_ns(*, extra: Iterable[float] = ()) -> np.ndarray:
    """NS 的 τ 候选网格（对数 + 任务要求的固定起点 + 调用方额外指定）。

    任务口径要求多起点 ``τ0 ∈ {0.5, 1, 2, 4, 8}``；这里在对数网格上把整个
    ``[0.05, 30]`` 年区间细分成约 140 个点，上述 5 个起点是其中一个子集。
    因此"网格粗搜"是"多起点"的严格超集：既覆盖了要求的起点，也不会漏掉
    中间的局部最优区域。

    面板拟合直接以该网格为最终精度（见 :func:`project_grid`），单日拟合则在
    网格最优解之上再用 ``least_squares`` 精修 τ。注意：网格解已经是**每个 τ 上
    的精确有界最小二乘**，精修只影响 τ 的连续性，不能用来给"裁剪近似"背书。
    """
    base = np.geomspace(TAU_MIN, TAU_MAX, 140)
    required = np.array([0.5, 1.0, 2.0, 4.0, 8.0])
    extra_arr = np.asarray(list(extra), dtype=float)
    vals = np.concatenate([base, required, extra_arr])
    vals = vals[np.isfinite(vals) & (vals >= TAU_MIN) & (vals <= TAU_MAX)]
    return np.unique(vals)


def tau_pairs_nss(*, extra: Iterable[tuple[float, float]] = ()) -> np.ndarray:
    """NSS 的 ``(τ1, τ2)`` 候选网格，形状 ``(m, 2)``，保证 ``τ2 > τ1``。

    ``τ1``、``τ2`` 各自取约 40 个对数点，笛卡尔积后过滤 ``τ2 > 1.15·τ1``
    （避免两个驼峰重合导致参数不可识别），最终约 1200 对。
    """
    tau1_base = np.unique(
        np.concatenate([np.geomspace(0.15, 25.0, 40), np.array([0.5, 1.0, 1.5, 2.0, 4.0, 8.0])])
    )
    tau2_base = np.unique(
        np.concatenate([np.geomspace(0.3, TAU_MAX, 40), np.array([2.0, 4.0, 6.0, 8.0, 15.0, 25.0])])
    )
    pairs = [(float(a), float(b)) for a in tau1_base for b in tau2_base if b > a * 1.15]
    pairs.extend((float(a), float(b)) for a, b in extra if b > a * 1.15)
    arr = np.array(pairs, dtype=float)
    ok = (
        np.isfinite(arr).all(axis=1)
        & (arr[:, 0] >= TAU_MIN)
        & (arr[:, 0] <= TAU_MAX)
        & (arr[:, 1] >= TAU_MIN)
        & (arr[:, 1] <= TAU_MAX)
    )
    return np.unique(arr[ok], axis=0)


def default_tau_starts(method: str) -> np.ndarray:
    """面板拟合用的默认 τ 起点网格。"""
    if method == "ns":
        return tau_grid_ns()
    if method == "nss":
        return tau_pairs_nss()
    raise ValueError(f"未知方法: {method!r}")


# ---------------------------------------------------------------------------
# 5b. 箱式约束线性最小二乘（精确，逐列向量化）
# ---------------------------------------------------------------------------
def bounded_linear_least_squares(
    x: np.ndarray,
    y_scaled: np.ndarray,
    lower: np.ndarray,
    upper: np.ndarray,
    *,
    kkt_tol: float = 1e-8,
) -> tuple[np.ndarray, np.ndarray]:
    """精确求解带箱式约束的线性最小二乘（逐列向量化）。

    求解
    ----
    ::

        min_b  ||x · b - y_scaled||_2   s.t.  lower <= b <= upper

    对每一列（一个交易日）是 ``k <= 4`` 维的箱式约束 QP。实现用**有效集法**：

    1. 先解无约束最小二乘；若系数全在界内，直接返回；
    2. 否则把越界变量固定到对应边界，对**其余自由变量重新解最小二乘**
       （这一步是 ``pinv + clip`` 缺失的关键：触界后必须重新优化其他系数）；
    3. 用 KKT 条件校验（自由变量梯度为 0、下界变量梯度 ≤ 0、上界变量梯度 ≥ 0，
       且自由变量可行）；对少数退化情形枚举全部 ``3^k`` 种"自由/下界/上界"
       状态，取满足 KKT 的最优解。

    返回的解在浮点精度内等于 ``scipy.optimize.lsq_linear(..., method="bvls")``
    （见 ``tests/test_curve_fit.py::test_bounded_lstsq_matches_lsq_linear``），
    但一次调用即可向量化处理上万条曲线。

    Parameters
    ----------
    x : ndarray
        设计矩阵，形状 ``(n, k)``，一般已按 ``sqrt(weight)`` 加权。
    y_scaled : ndarray
        加权响应，形状 ``(n, D)``（也接受一维 ``(n,)``）。
    lower, upper : ndarray
        形状 ``(k,)`` 的箱式边界。

    Returns
    -------
    (coef, sse)
        ``coef`` 形状 ``(k, D)``；``sse`` 形状 ``(D,)``，为加权残差平方和
        （单位与 ``y_scaled`` 相同，即 ``%²``；调用方自行换算 bp）。
    """
    x = np.asarray(x, dtype=float)
    lower = np.asarray(lower, dtype=float).ravel()
    upper = np.asarray(upper, dtype=float).ravel()
    y = np.asarray(y_scaled, dtype=float)
    squeeze = y.ndim == 1
    if squeeze:
        y = y[:, None]
    if x.ndim != 2 or x.shape[0] != y.shape[0]:
        raise ValueError(f"x 形状 {x.shape} 与 y 形状 {y.shape} 不匹配")
    k = x.shape[1]
    if lower.size != k or upper.size != k:
        raise ValueError(f"边界长度必须为 {k}，实际 lower={lower.size}, upper={upper.size}")
    if np.any(lower > upper):
        raise ValueError("lower 必须逐元素不大于 upper")

    n_dates = y.shape[1]
    unconstrained = np.linalg.pinv(x) @ y
    coef = np.clip(unconstrained, lower[:, None], upper[:, None])

    # 无约束解处于界内的列：无需任何处理（绝大多数交易日走这条快路径）
    status = np.zeros((k, n_dates), dtype=np.int8)  # 0=free, 1=lower, 2=upper
    status[unconstrained < lower[:, None]] = 1
    status[unconstrained > upper[:, None]] = 2
    if not np.any(status != 0):
        resid = x @ coef - y
        return (coef[:, 0] if squeeze else coef), np.sum(np.square(resid), axis=0)

    # 迭代式有效集（批量 BVLS）：触界→固定并重解自由变量；
    # 已是边界但 KKT 符号不对→释放回自由变量。一般 1–2 轮收敛。
    pending = np.flatnonzero(np.any(status != 0, axis=0))
    pinv_cache: dict[int, tuple[np.ndarray, np.ndarray]] = {}
    max_rounds = 2 * k + 2
    for _ in range(max_rounds):
        if pending.size == 0:
            break
        pattern, inverse = np.unique(status[:, pending].T, axis=0, return_inverse=True)
        next_pending: list[np.ndarray] = []
        for group in range(pattern.shape[0]):
            idx = pending[inverse == group]
            st = pattern[group]
            b_group = _solve_with_status(x, y[:, idx], st, lower, upper, pinv_cache)
            coef[:, idx] = b_group
            grad = x.T @ (x @ b_group - y[:, idx])  # (k, d)
            scale = max(1.0, float(np.max(np.abs(grad), initial=0.0)))
            tol = kkt_tol * scale
            free = (st == 0)[:, None]
            fix_low = free & (b_group < lower[:, None] - 1e-10)
            fix_up = free & (b_group > upper[:, None] + 1e-10)
            rel_lower = (st == 1)[:, None] & (grad > tol)
            rel_upper = (st == 2)[:, None] & (grad < -tol)
            changed = fix_low | fix_up | rel_lower | rel_upper
            changed_cols = np.any(changed, axis=0)
            if not np.any(changed_cols):
                continue
            new_status = status[:, idx].copy()
            for i in range(k):
                sel = changed[i]
                if not np.any(sel):
                    continue
                target = new_status[i, sel]
                target[fix_low[i, sel]] = 1
                target[fix_up[i, sel]] = 2
                target[rel_lower[i, sel]] = 0
                target[rel_upper[i, sel]] = 0
                new_status[i, sel] = target
            status[:, idx] = new_status
            next_pending.append(idx[changed_cols])
        pending = np.concatenate(next_pending) if next_pending else np.array([], dtype=int)

    # 极端退化/循环情形：枚举 3^k 种状态兜底（保证仍是精确约束最优）
    if pending.size:
        coef[:, pending] = _solve_by_enumeration(x, y[:, pending], lower, upper, kkt_tol=kkt_tol)

    resid = x @ coef - y
    sse = np.sum(np.square(resid), axis=0)
    return (coef[:, 0] if squeeze else coef), sse


def _solve_with_status(
    x: np.ndarray,
    y: np.ndarray,
    status: np.ndarray,
    lower: np.ndarray,
    upper: np.ndarray,
    pinv_cache: dict[int, tuple[np.ndarray, np.ndarray]] | None = None,
) -> np.ndarray:
    """给定"每个变量自由/下界/上界"状态，解自由变量上的最小二乘。

    ``pinv_cache`` 以"自由变量子集"的位掩码为键缓存 ``(自由列, 伪逆)``：
    一个 τ 下有数千条曲线、至多 ``2^k <= 16`` 种自由子集，缓存后不再重复 SVD。
    """
    k = x.shape[1]
    n_dates = y.shape[1]
    active = status != 0
    b = np.zeros((k, n_dates))
    if np.any(active):
        fixed = np.where(status[active] == 1, lower[active], upper[active])
        b[active] = fixed[:, None]
    if np.any(~active):
        free = ~active
        rhs = y - x @ b if np.any(active) else y
        key = int(np.dot(free.astype(np.int64), 1 << np.arange(k, dtype=np.int64)))
        if pinv_cache is None:
            b[free] = np.linalg.pinv(x[:, free]) @ rhs
        else:
            if key not in pinv_cache:
                pinv_cache[key] = (x[:, free], np.linalg.pinv(x[:, free]))
            b[free] = pinv_cache[key][1] @ rhs
    return b


def _solve_by_enumeration(
    x: np.ndarray,
    y: np.ndarray,
    lower: np.ndarray,
    upper: np.ndarray,
    *,
    kkt_tol: float = 1e-8,
) -> np.ndarray:
    """枚举全部 ``3^k`` 种触界状态，返回满足 KKT 的最优可行解（退化兜底）。"""
    k = x.shape[1]
    n_dates = y.shape[1]
    best = np.clip(np.linalg.pinv(x) @ y, lower[:, None], upper[:, None])
    best_sse = np.sum(np.square(x @ best - y), axis=0)
    best_feasible = np.zeros(n_dates, dtype=bool)
    for code in range(3**k):
        digits = np.zeros(k, dtype=np.int8)
        value = code
        for i in range(k - 1, -1, -1):
            digits[i] = value % 3
            value //= 3
        cand = _solve_with_status(x, y, digits, lower, upper)
        feasible_free = np.ones(n_dates, dtype=bool)
        for i in range(k):
            if digits[i] == 0:
                feasible_free &= (cand[i] >= lower[i] - 1e-10) & (cand[i] <= upper[i] + 1e-10)
        if not np.any(feasible_free):
            continue
        sse = np.sum(np.square(x @ cand - y), axis=0)
        improve = feasible_free & (~best_feasible | (sse < best_sse))
        if np.any(improve):
            best[:, improve] = cand[:, improve]
            best_sse[improve] = sse[improve]
            best_feasible[improve] = True
    return best


# ---------------------------------------------------------------------------
# 6. 向量化粗搜（投影）
# ---------------------------------------------------------------------------
def project_grid(
    method: str,
    tenors: np.ndarray,
    y_matrix: np.ndarray,
    weights: np.ndarray,
    *,
    tau_starts: np.ndarray | None = None,
    tie_rel_tol: float = 1e-2,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """对一批收益率列做"τ 网格 + 线性最小二乘投影"的向量化粗搜。

    Parameters
    ----------
    method : str
        ``"ns"`` 或 ``"nss"``。
    tenors : ndarray
        形状 ``(n,)``，升序、无重复的期限点。
    y_matrix : ndarray
        形状 ``(n, D)``：每一列是一个交易日/一条曲线的收益率（百分数）。
        调用方必须保证**无 NaN**（面板路径会先按缺失模式分组）。
    weights : ndarray
        形状 ``(n,)``，正权重。
    tau_starts : ndarray | None
        网格；``None`` 时用默认网格。
    tie_rel_tol : float
        "近优解"的相对容差。NSS 在曲线形态简单时存在严重的**参数不可识别**
        （``b3`` 很大 + ``τ2`` 很大会与"双驼峰"几乎等价，加权残差差异 < 1%），
        只按残差最小选格点会经常选到贴着 τ 边界的退化解，导致参数时间序列
        抖成噪声、无法解释。因此这里在"加权 SSE 与最优值相差在
        ``tie_rel_tol`` 以内"的格点中，再取**线性系数范数最小**（最简约）的解。
        代价是再拟合误差最多上升约 ``sqrt(1+tie_rel_tol)-1 ≈ 0.5%``
        （典型 3.7bp → +0.02bp），换来稳定的参数序列。

    Returns
    -------
    (coef, tau_best, weighted_sse_best, coef_norm)
        ``coef`` 形状 ``(D, k)``（NS k=3，NSS k=4，含常数项），
        ``tau_best`` 形状 ``(D, 1)`` 或 ``(D, 2)``，
        ``weighted_sse_best`` 形状 ``(D,)``，单位 bp²（已按权重加权），
        ``coef_norm`` 形状 ``(D,)``，为所选解的线性系数平方和。

    Notes
    -----
    NS/NSS 对系数线性，所以固定 τ 时系数是线性最小二乘问题；把线性系数
    "投影掉"后，全局搜索只剩 1–2 维的 τ，计算量可控且几乎不会落入局部极小。
    系数在**任务规定的边界内用精确箱式约束最小二乘**求解
    （:func:`bounded_linear_least_squares`，等价于 ``lsq_linear(method="bvls")``），
    而不是 ``pinv + clip``：后者只是把参数压回界内，触界时没有重新优化其余
    系数，残差会明显偏大（R01 已复现：极端日可达 156bp vs 93.8bp）。
    """
    y_matrix = np.asarray(y_matrix, dtype=float)
    if y_matrix.ndim != 2:
        raise ValueError(f"y_matrix 必须是二维 (n, D)，实际 shape={y_matrix.shape}")
    tenors = np.asarray(tenors, dtype=float)
    n, n_dates = y_matrix.shape
    if tenors.size != n:
        raise ValueError(f"tenors 长度 {tenors.size} 与 y_matrix 行数 {n} 不一致")
    if n_dates == 0:
        raise ValueError("y_matrix 的列数为 0，没有可拟合的曲线")

    if tau_starts is None:
        tau_starts = default_tau_starts(method)
    tau_starts = np.asarray(tau_starts, dtype=float)
    if method == "nss":
        tau_starts = np.atleast_2d(tau_starts)

    sw = np.sqrt(np.asarray(weights, dtype=float))
    lower, upper = _METHOD_BOUNDS[method]
    n_linear = _METHOD_NLINEAR[method]
    y_scaled = sw[:, None] * y_matrix

    best_coef = np.zeros((n_dates, n_linear))
    best_tau = np.zeros((n_dates, 1 if method == "ns" else 2))
    best_sse = np.full(n_dates, np.inf)
    best_norm = np.full(n_dates, np.inf)
    min_sse = np.full(n_dates, np.inf)

    for tau in tau_starts:
        x = sw[:, None] * design_matrix(method, tenors, np.atleast_1d(tau))
        # 固定 τ 时系数是**箱式约束**线性最小二乘：必须精确求解（触界后要
        # 重新优化其他系数），不能用 pinv + clip 近似（见 R01 回归测试）
        coef, sse = bounded_linear_least_squares(x, y_scaled, lower[:n_linear], upper[:n_linear])
        coef_norm = np.sum(np.square(coef), axis=0)
        # 先更新"真实最优"，再用"近优 + 系数更简约"做稳定化选择
        min_sse = np.minimum(min_sse, sse)
        near = (sse <= min_sse * (1.0 + tie_rel_tol)) & (coef_norm < best_norm)
        update = near | (sse < best_sse)
        if np.any(update):
            best_sse[update] = sse[update]
            best_coef[update] = coef[:, update].T
            best_tau[update] = np.atleast_1d(tau)
            best_norm[update] = coef_norm[update]
    return best_coef, best_tau, best_sse, best_norm


# ---------------------------------------------------------------------------
# 6b. 稳定化估计：b0 锚定 + 其余系数线性求解
# ---------------------------------------------------------------------------
def long_end_b0_anchor(
    tenors: np.ndarray, ytm_pct: np.ndarray, *, long_tenor: float = 10.0
) -> float:
    """用长端观测给 NS/NSS 的水平因子 ``b0`` 定锚（单位：百分数）。

    为什么需要锚定：NS/NSS 的 4–6 个参数在 11 个期限点上存在**严重的参数不可
    识别**——对形态平滑的曲线，"b0 很大 + b1 很负"与"b0 很小 + b1 很正"可以给出
    几乎相同的拟合（再拟合误差差异常 < 1bp）。自由估计 b0 会日间在多个等价解之间
    跳动（实测 p99 |Δb0| ≈ 4pp），level 因子因此无法使用。

    经济上看 ``b0`` 是长端渐近水平，中国国债 10Y–30Y 收益率 2002 年以来始终在
    1.7%–5.6% 区间，因此用 ``tenor ≥ long_tenor`` 的观测均值作为锚，既贴近数据又
    天然连续（收益率本身是平滑时间序列）。

    Parameters
    ----------
    tenors : ndarray
        期限（年）。
    ytm_pct : ndarray
        收益率（百分数），与 ``tenors`` 等长。
    long_tenor : float
        长端阈值（默认 10 年）；若数据里没有 ≥ 该阈值的点，退回最长期限的收益率。

    Returns
    -------
    float
        锚定的 b0（百分数）。
    """
    t = np.asarray(tenors, dtype=float)
    y = np.asarray(ytm_pct, dtype=float)
    mask = t >= float(long_tenor)
    if np.any(mask):
        return float(np.mean(y[mask]))
    return float(y[-1])


def project_grid_anchored(
    method: str,
    tenors: np.ndarray,
    y_matrix: np.ndarray,
    weights: np.ndarray,
    b0_anchor: np.ndarray,
    *,
    tau_starts: np.ndarray | None = None,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    """``b0`` 固定为锚定值的投影网格拟合（用于面板的**稳定化参数估计**）。

    目标函数与 :func:`project_grid` 相同（加权最小二乘），只是把常数项换成由
    :func:`long_end_b0_anchor` 给定的 ``b0``，从而消除 b0 的不可识别性：
    给定 τ 后剩余系数（NS: b1,b2；NSS: b1,b2,b3）仍是线性问题，解析求解。

    Returns
    -------
    (coef, tau_best, weighted_sse_best, coef_norm)
        ``coef`` 形状 ``(D, 2)``（NS）或 ``(D, 3)``（NSS），对应 ``b1,b2[,b3]``。
    """
    y_matrix = np.asarray(y_matrix, dtype=float)
    if y_matrix.ndim != 2:
        raise ValueError(f"y_matrix 必须是二维 (n, D)，实际 shape={y_matrix.shape}")
    tenors = np.asarray(tenors, dtype=float)
    n, n_dates = y_matrix.shape
    if tenors.size != n:
        raise ValueError(f"tenors 长度 {tenors.size} 与 y_matrix 行数 {n} 不一致")
    anchor = np.asarray(b0_anchor, dtype=float).ravel()
    if anchor.size != n_dates:
        raise ValueError(f"b0_anchor 长度 {anchor.size} 与曲线条数 {n_dates} 不一致")

    if tau_starts is None:
        tau_starts = default_tau_starts(method)
    tau_starts = np.asarray(tau_starts, dtype=float)
    if method == "nss":
        tau_starts = np.atleast_2d(tau_starts)

    sw = np.sqrt(np.asarray(weights, dtype=float))
    y_adj = y_matrix - anchor[None, :]
    y_scaled = sw[:, None] * y_adj
    n_free = 2 if method == "ns" else 3
    lo = np.full(n_free, STABLE_COEF_LOWER)
    hi = np.full(n_free, STABLE_COEF_UPPER)

    best_coef = np.zeros((n_dates, n_free))
    best_tau = np.zeros((n_dates, 1 if method == "ns" else 2))
    best_sse = np.full(n_dates, np.inf)
    best_norm = np.full(n_dates, np.inf)
    min_sse = np.full(n_dates, np.inf)

    for tau in tau_starts:
        tau_vec = np.atleast_1d(tau)
        l1, l2 = ns_loadings(tenors, tau_vec[0])
        if method == "ns":
            x = np.column_stack([l1, l2])
        else:
            x = np.column_stack([l1, l2, ns_loadings(tenors, tau_vec[1])[1]])
        x = sw[:, None] * x
        coef, sse = bounded_linear_least_squares(x, y_scaled, lo, hi)
        coef_norm = np.sum(np.square(coef), axis=0)
        min_sse = np.minimum(min_sse, sse)
        near = (sse <= min_sse * (1.0 + 1e-2)) & (coef_norm < best_norm) | (sse < best_sse)
        if np.any(near):
            best_sse[near] = sse[near]
            best_coef[near] = coef[:, near].T
            best_tau[near] = tau_vec
            best_norm[near] = coef_norm[near]
    return best_coef, best_tau, best_sse, best_norm


def params_dict_anchored(
    method: str, b0: float, coef: np.ndarray, tau_vec: np.ndarray
) -> dict[str, float]:
    """把"锚定 b0 + 其余系数"拼成有名字的参数 dict（单位：百分数 / 年）。"""
    coef = np.asarray(coef, dtype=float).ravel()
    tau_vec = np.atleast_1d(np.asarray(tau_vec, dtype=float))
    if method == "ns":
        return {
            "b0": float(b0),
            "b1": float(coef[0]),
            "b2": float(coef[1]),
            "tau": float(tau_vec[0]),
            "tau1": float(tau_vec[0]),
        }
    if method == "nss":
        return {
            "b0": float(b0),
            "b1": float(coef[0]),
            "b2": float(coef[1]),
            "b3": float(coef[2]),
            "tau1": float(tau_vec[0]),
            "tau2": float(tau_vec[1]),
        }
    raise ValueError(f"未知方法: {method!r}；仅支持 'ns' / 'nss'")


# ---------------------------------------------------------------------------
# 7. 带边界的局部精修
# ---------------------------------------------------------------------------
def polish(
    method: str,
    tenors: np.ndarray,
    ytm_pct: np.ndarray,
    weights: np.ndarray,
    x0: np.ndarray,
    *,
    max_nfev: int = 15,
) -> tuple[np.ndarray, bool, str]:
    """在**完整参数空间 + 任务规定边界**下做一次有界非线性精修。

    目标函数是 ``sqrt(w) · (拟合值 - 观测值) · 100``（即 bp 口径的加权残差），
    NSS 额外把 ``τ2 > τ1`` 约束作为惩罚项拼在残差末尾。使用
    :func:`scipy.optimize.least_squares`（``method="trf"``），初值先裁剪到边界内。

    Parameters
    ----------
    method, tenors, ytm_pct, weights :
        与 :func:`project_grid` 相同口径；``ytm_pct`` 形状 ``(n,)``。
    x0 : ndarray
        初值，长度 4（NS）或 6（NSS）；``[b..., τ...]``。
    max_nfev : int
        函数求值次数上限。面板拟合用较小的值（粗搜已经给出很好的初值），
        单日拟合用较大的值以保证参数精度。

    Returns
    -------
    (params, success, message)
        精修后的参数、``least_squares`` 的成功标记与消息。即使失败也返回
        精修得到的参数（而不是抛异常），由调用方决定如何记录。
    """
    lower, upper = _METHOD_BOUNDS[method]
    n_linear = _METHOD_NLINEAR[method]
    sw = np.sqrt(np.asarray(weights, dtype=float)) * 100.0
    x0 = np.clip(np.asarray(x0, dtype=float), lower, upper)

    def _residual(x: np.ndarray) -> np.ndarray:
        resid = sw * (model_rate(method, x, tenors) - ytm_pct)
        if method == "nss":
            gap = TAU_ORDER_PENALTY * max(0.0, x[n_linear] - x[n_linear + 1] + 1e-3)
            return np.concatenate([resid, [gap]])
        return resid

    try:
        result = least_squares(
            _residual,
            x0,
            bounds=(lower, upper),
            method="trf",
            max_nfev=int(max_nfev),
            xtol=1e-9,
            ftol=1e-10,
            gtol=1e-9,
        )
    except (ValueError, FloatingPointError) as exc:  # pragma: no cover - 数值极端情况
        return x0, False, f"精修失败: {exc}"
    return np.asarray(result.x, dtype=float), bool(result.success), str(result.message)


# ---------------------------------------------------------------------------
# 8. 参数名
# ---------------------------------------------------------------------------
def coef_names(method: str) -> list[str]:
    """线性系数名（``b0, b1, ...``）。"""
    n_linear = _METHOD_NLINEAR[method]
    return [f"b{i}" for i in range(n_linear)]


def tau_names(method: str) -> list[str]:
    """τ 参数名。NS 用 ``tau``（同时提供 ``tau1`` 别名便于与面板列对齐）。"""
    if method == "ns":
        return ["tau"]
    return ["tau1", "tau2"]


def params_dict(method: str, x: np.ndarray) -> dict[str, float]:
    """把扁平参数数组转成有名字的 dict，单位均为百分数 / 年。"""
    x = np.asarray(x, dtype=float)
    n_linear = _METHOD_NLINEAR[method]
    out = {name: float(value) for name, value in zip(coef_names(method), x[:n_linear], strict=True)}
    if method == "ns":
        out["tau"] = float(x[3])
        # 别名：与 NSS / 参数面板的 tau1 列对齐，方便上层统一按 tau1 取值
        out["tau1"] = float(x[3])
    else:
        out["tau1"] = float(x[4])
        out["tau2"] = float(x[5])
    return out


def method_nparams(method: str) -> int:
    """该方法的参数总数（NS=4，NSS=6）。"""
    if method not in _METHOD_NPARAMS:
        raise ValueError(f"未知方法: {method!r}；仅支持 'ns' / 'nss'")
    return _METHOD_NPARAMS[method]
