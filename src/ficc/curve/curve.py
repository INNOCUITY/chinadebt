"""曲线对象：把"某一交易日的收益率曲线"封装成可求值、可冲击的一等公民。

定位（为什么不是简单插值）
--------------------------
中债国债收益率曲线本身已经是 **Hermite 三次插值的成品**（见
`docs/00_market_conventions.md` §1 与 `docs/01_project_plan.md` 决策 1c），
所以本模块做 NS/NSS **不是**为了"插值出中债原本没有的点"，而是为了：

1. **可解析求导**：得到光滑、可导的曲线，才能干净地算远期利率、KRD、情景冲击；
2. **因子提取**：level / slope / curvature 三个可解释因子，供策略与归因使用；
3. **降噪**：用 4–6 个参数压缩 11 个期限点，避免单点报价噪声污染信号。

因此 :class:`Curve` 同时支持两类形态：

- ``method in {"linear", "hermite"}``：直接对市场节点做插值（linear=分段线性，
  hermite=分段单调三次 Hermite，等价于 Fritsch–Carlson / PCHIP）；
- ``method in {"ns", "nss"}``：``rate()`` 用参数化公式解析求值，``tenors/rates``
  保存的是拟合点/市场点（由构造方决定，``fit_curve`` 会存拟合值）。

外推策略（必须明示，否则会被追问）
----------------------------------
- 区间内：按 ``method`` 插值；
- 区间外：用**端点区间的斜率线性延伸**（而不是常数外推），再统一
  **clamp 到下界 0**——外推不允许产生负利率；上界不设限。
- 参数化曲线（ns/nss）由公式自然外推，同样 clamp 到 0。

参数键约定（R09 统一口径）
--------------------------
- **NS** 的衰减参数规范键是 ``tau``；同时接受别名 ``tau1``（NS 面板导出的列名就是
  ``tau1``），构造时会规范化成 ``tau``。
- **NSS** 用 ``tau1`` / ``tau2``。
- ``method in {"ns","nss"}`` 时参数**必须齐全**：缺参数直接抛 ``ValueError``，
  **禁止静默退化成线性插值**（旧实现的静默降级会把"参数错"伪装成"模型不同"）。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Literal

import numpy as np
import pandas as pd
from scipy.interpolate import PchipInterpolator

from ficc.config import get_config
from ficc.curve._common import (
    coef_names,
    design_matrix,
    model_rate,
    ns_loadings,
)

# 合法方法 / 冲击类型（用 Literal 表达，便于静态检查与 IDE 提示）
CurveMethod = Literal["linear", "hermite", "ns", "nss"]
ShockKind = Literal["parallel", "steepen", "flatten", "butterfly"]

_VALID_METHODS: tuple[str, ...] = ("linear", "hermite", "ns", "nss")
_VALID_SHOCKS: tuple[str, ...] = ("parallel", "steepen", "flatten", "butterfly")
_PARAMETRIC_METHODS: tuple[str, ...] = ("ns", "nss")

# L2(x) = (1-e^{-x})/x - e^{-x} 的全局最大值（在 x*=1.79328 处取到），
# 用于把蝶式冲击归一化到"腹部 +bp、两端 -bp/2"
_L2_MAX: float = 0.29842560752563906

# 参数规范：系数键 + τ 键 + 别名（NS 面板列名是 tau1，Curve 规范键是 tau）
_PARAM_SPECS: dict[str, dict[str, tuple[str, ...] | dict[str, str]]] = {
    "ns": {
        "coef": ("b0", "b1", "b2"),
        "tau": ("tau",),
        "aliases": {"tau1": "tau"},
    },
    "nss": {
        "coef": ("b0", "b1", "b2", "b3"),
        "tau": ("tau1", "tau2"),
        "aliases": {"tau": "tau1"},
    },
}


def _param_order_of(method: str) -> list[str]:
    """参数数组的键顺序（NS: b0,b1,b2,tau；NSS: b0,b1,b2,b3,tau1,tau2）。"""
    spec = _PARAM_SPECS[str(method).lower()]
    coef = spec["coef"]
    tau = spec["tau"]
    assert isinstance(coef, tuple) and isinstance(tau, tuple)
    return [*coef, *tau]


def normalize_params(method: str, params: Mapping[str, float]) -> dict[str, float]:
    """把参数 dict 规范化成该方法的**完整**规范键集合；不完整直接抛异常。

    - NS：规范键 ``b0,b1,b2,tau``，接受别名 ``tau1``；
    - NSS：规范键 ``b0,b1,b2,b3,tau1,tau2``，接受别名 ``tau``（映射到 ``tau1``）。

    Parameters
    ----------
    method : {"ns", "nss"}
        曲线类型。
    params : Mapping[str, float]
        参数（百分数 / 年）。

    Returns
    -------
    dict[str, float]
        只含规范键、已转 float 的参数。

    Raises
    ------
    ValueError
        method 非法、参数缺失、参数非有限。
    """
    method_str = str(method).lower()
    if method_str not in _PARAM_SPECS:
        raise ValueError(f"未知参数化方法: {method!r}；仅支持 'ns' / 'nss'")
    spec = _PARAM_SPECS[method_str]
    coef = spec["coef"]
    tau = spec["tau"]
    aliases = spec["aliases"]
    assert isinstance(coef, tuple) and isinstance(tau, tuple) and isinstance(aliases, dict)

    lookup: dict[str, float] = {}
    for key, value in dict(params or {}).items():
        if isinstance(value, bool):
            raise TypeError(f"params[{key!r}] 不应为 bool")
        fvalue = float(value)
        if not np.isfinite(fvalue):
            raise ValueError(f"params[{key!r}] 含 NaN/inf")
        lookup[str(key)] = fvalue
    required = [*coef, *tau]
    for alias, canonical in aliases.items():
        if canonical not in lookup and alias in lookup:
            lookup[canonical] = lookup[alias]
    missing = [name for name in required if name not in lookup]
    if missing:
        raise ValueError(
            f"method={method_str!r} 的参数不完整，缺少 {missing}；"
            f"需要 {required}（NS 的 τ 可用 tau 或 tau1）"
        )
    return {name: float(lookup[name]) for name in required}


@dataclass(frozen=True, eq=False)
class Curve:
    """一条收益率曲线（单位：期限=年，收益率=百分数）。

    ``eq=False``：字段里含 ``np.ndarray``，默认的逐字段 ``==`` 会返回数组而
    触发 "truth value is ambiguous"，因此显式关闭自动生成的 ``__eq__``。

    Parameters
    ----------
    as_of : date
        曲线对应的交易日。传 ``datetime`` 会被转成 ``date``。
    tenors : np.ndarray
        期限（年），一维、严格递增、无 NaN；至少 2 个点。
    rates : np.ndarray
        与 ``tenors`` 等长的到期收益率，**百分数**（``2.35`` 表示 2.35%）。
    method : {"linear", "hermite", "ns", "nss"}
        求值方式；``ns``/``nss`` 需要 ``params`` 里带齐参数。
    params : dict[str, float]
        参数化曲线的参数（``b0,b1,b2[,b3],tau1[,tau2]``）；插值曲线为空。

    Notes
    -----
    构造时做严格校验（期限递增、长度一致、无 NaN/inf、方法合法），
    校验失败抛 ``ValueError``/``TypeError``，不会静默产生坏曲线。
    """

    as_of: date
    tenors: np.ndarray
    rates: np.ndarray
    method: CurveMethod = "linear"
    params: dict[str, float] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # 构造校验
    # ------------------------------------------------------------------
    def __post_init__(self) -> None:
        as_of = self.as_of
        if isinstance(as_of, datetime):
            as_of = as_of.date()
        if not isinstance(as_of, date):
            raise TypeError(f"as_of 必须是 datetime.date，实际 {type(as_of).__name__}")

        tenors = np.asarray(self.tenors, dtype=float).ravel()
        rates = np.asarray(self.rates, dtype=float).ravel()
        if tenors.size != rates.size:
            raise ValueError(f"tenors 与 rates 长度不一致: {tenors.size} vs {rates.size}")
        if tenors.size < 2:
            raise ValueError(f"Curve 至少需要 2 个期限点，实际 {tenors.size} 个")
        if not np.all(np.isfinite(tenors)):
            raise ValueError("tenors 含 NaN/inf")
        if not np.all(np.isfinite(rates)):
            raise ValueError("rates 含 NaN/inf；缺失值必须在数据层显式处理")
        if np.any(tenors <= 0):
            raise ValueError("tenors 必须为正数（单位：年）")
        if np.any(np.diff(tenors) <= 0):
            raise ValueError("tenors 必须严格递增且不重复")

        method = str(self.method).lower()
        if method not in _VALID_METHODS:
            raise ValueError(f"method 必须是 {_VALID_METHODS} 之一，实际 {self.method!r}")

        if method in _PARAMETRIC_METHODS:
            # 参数化曲线：参数必须齐全（缺参数抛 ValueError，禁止静默退化插值）
            params = normalize_params(method, self.params or {})
        else:
            params = {}
            for key, value in dict(self.params or {}).items():
                if isinstance(value, bool):
                    raise TypeError(f"params[{key!r}] 不应为 bool")
                fvalue = float(value)
                if not np.isfinite(fvalue):
                    raise ValueError(f"params[{key!r}] 含 NaN/inf")
                params[str(key)] = fvalue

        object.__setattr__(self, "as_of", as_of)
        object.__setattr__(self, "tenors", tenors)
        object.__setattr__(self, "rates", rates)
        object.__setattr__(self, "method", method)  # type: ignore[arg-type]
        object.__setattr__(self, "params", params)

    # ------------------------------------------------------------------
    # 内部工具
    # ------------------------------------------------------------------
    @property
    def _param_array(self) -> np.ndarray | None:
        """把 ``params`` 拼成扁平参数数组；插值方法返回 ``None``。

        参数化曲线的 ``params`` 已在 ``__post_init__`` 里规范化并校验齐全，
        因此这里不会再出现"缺参数"分支（旧实现的静默退化已被 R09 移除）。
        """
        if self.method not in _PARAMETRIC_METHODS:
            return None
        return np.array([self.params[name] for name in self._param_order], dtype=float)

    @property
    def _tau_ref(self) -> float:
        """冲击形状使用的参考 τ：参数化曲线用自身 τ1，插值曲线用配置默认值。"""
        for key in ("tau1", "tau"):
            if key in self.params and self.params[key] > 0:
                return float(self.params[key])
        return float(get_config().curve.ns_tau0)

    def _linear_extension(self, t: np.ndarray) -> np.ndarray:
        """分段线性插值 + 两端线性延伸（未 clamp）。"""
        x, y = self.tenors, self.rates
        out = np.empty_like(t)
        inside = (t >= x[0]) & (t <= x[-1])
        below = t < x[0]
        above = t > x[-1]
        if np.any(inside):
            out[inside] = np.interp(t[inside], x, y)
        if np.any(below):
            slope = (y[1] - y[0]) / (x[1] - x[0])
            out[below] = y[0] + slope * (t[below] - x[0])
        if np.any(above):
            slope = (y[-1] - y[-2]) / (x[-1] - x[-2])
            out[above] = y[-1] + slope * (t[above] - x[-1])
        return out

    def _hermite_extension(self, t: np.ndarray) -> np.ndarray:
        """单调三次 Hermite（PCHIP）插值 + 两端线性延伸（未 clamp）。"""
        x, y = self.tenors, self.rates
        out = np.empty_like(t)
        inside = (t >= x[0]) & (t <= x[-1])
        below = t < x[0]
        above = t > x[-1]
        if np.any(inside):
            interp = PchipInterpolator(x, y, extrapolate=False)
            out[inside] = interp(t[inside])
        if np.any(below):
            slope = (y[1] - y[0]) / (x[1] - x[0])
            out[below] = y[0] + slope * (t[below] - x[0])
        if np.any(above):
            slope = (y[-1] - y[-2]) / (x[-1] - x[-2])
            out[above] = y[-1] + slope * (t[above] - x[-1])
        return out

    # ------------------------------------------------------------------
    # 求值
    # ------------------------------------------------------------------
    def rate(self, t: float | np.ndarray) -> float | np.ndarray:
        """求 ``t``（年）处的到期收益率（百分数）。

        Parameters
        ----------
        t : float | np.ndarray
            期限（年），可为标量或数组；不允许负值或 NaN（负期限没有金融含义）。

        Returns
        -------
        float | np.ndarray
            与输入同形状的收益率（百分数）；标量输入返回 ``float``。

        Notes
        -----
        外推由端点区间斜率线性延伸（参数化曲线由公式自然外推），并统一
        ``clamp`` 到 0 以上——外推不产生负利率。
        """
        scalar = np.ndim(t) == 0
        t_arr = np.atleast_1d(np.asarray(t, dtype=float))
        if not np.all(np.isfinite(t_arr)):
            raise ValueError("t 含 NaN/inf")
        if np.any(t_arr < 0):
            raise ValueError("t（期限）不能为负")

        param = self._param_array
        if param is not None:
            values = np.asarray(model_rate(self.method, param, t_arr), dtype=float)
        elif self.method == "hermite":
            values = self._hermite_extension(t_arr)
        else:
            values = self._linear_extension(t_arr)

        values = np.maximum(values, 0.0)  # 外推不得产生负利率
        return float(values[0]) if scalar else values

    def forward(self, t1: float, t2: float) -> float:
        """``[t1, t2]`` 区间的简单远期利率（连续复利，百分数）。

        用"到期收益率近似零息利率"的标准近似：``f = (r2·t2 - r1·t1)/(t2 - t1)``。
        精确的远期曲线应先把 YTM 曲线 bootstrap 成即期曲线（下一轮计划），
        这里给出的是与曲线求值口径一致的**一阶近似**，用于快速看形态。

        Raises
        ------
        ValueError
            期限非有限、``t1 < 0`` 或 ``t2 <= t1``。
        """
        t1 = float(t1)
        t2 = float(t2)
        if not np.isfinite([t1, t2]).all():
            raise ValueError("t1/t2 必须为有限数")
        if t1 < 0:
            raise ValueError("t1 不能为负")
        if t2 <= t1:
            raise ValueError(f"t2({t2}) 必须大于 t1({t1})")
        r1 = float(self.rate(t1))
        r2 = float(self.rate(t2))
        return (r2 * t2 - r1 * t1) / (t2 - t1)

    # ------------------------------------------------------------------
    # 场景冲击
    # ------------------------------------------------------------------
    def shift(self, bp: float) -> Curve:
        """平行移动 ``bp`` 个基点（**带符号**：``bp=100`` → +1.00%，``-25`` → -0.25%）。

        与 :meth:`shock` 的分工：``shift`` 用于"上下平移"这类本身有方向的幅度，
        因此接受负数；``shock(kind, bp)`` 的 ``bp`` 是**幅度**，必须非负，
        方向由 ``kind`` 表达。参数化曲线等价于 ``b0 += bp/100``（精确）；
        插值曲线直接加到节点上。
        """
        bp = float(bp)
        if not np.isfinite(bp):
            raise ValueError("bp 必须为有限数")
        amp = bp / 100.0
        param = self._param_array
        if param is None:
            return Curve(
                as_of=self.as_of,
                tenors=self.tenors.copy(),
                rates=self.rates + amp,
                method=self.method,
            )
        new_params = dict(self.params)
        new_params["b0"] = new_params["b0"] + amp
        new_rates = np.asarray(
            model_rate(
                self.method,
                np.array([new_params[name] for name in self._param_order], dtype=float),
                self.tenors,
            ),
            dtype=float,
        )
        return Curve(
            as_of=self.as_of,
            tenors=self.tenors.copy(),
            rates=new_rates,
            method=self.method,
            params=new_params,
        )

    def shock(self, kind: ShockKind, bp: float) -> Curve:
        """对曲线施加情景冲击，返回**新**曲线（原曲线不可变）。

        **语义约定（R10）**：``kind`` 表达**方向**，``bp`` 表达**幅度**。

        - ``bp`` 必须 **≥ 0**（传负值抛 ``ValueError``）；"反向冲击"用对应的
          ``kind`` 表达（``flatten`` 是 ``steepen`` 的反向），下游**不要**再取负号。
        - 需要带符号的平行移动请用 :meth:`shift`（它按"平移量"语义接受负数）。

        冲击形状定义（``amp = bp/100``，τ 取曲线自身的 τ1，插值曲线取配置
        ``curve.ns_tau0``）::

            parallel  : Δ(t) = +amp                                整条曲线上移
            steepen   : Δ(t) = +amp·(0.5 - L1(t;τ))                短端下、长端上（变陡）
            flatten   : Δ(t) = -amp·(0.5 - L1(t;τ))                短端上、长端下（变平）
            butterfly : Δ(t) = +amp·(1.5·L2(t;τ)/max L2 - 0.5)     腹部上、两端下

        这四种形状都落在 NS 基函数 ``{1, L1, L2}`` 张成的空间里，因此对 NS/NSS
        参数化曲线是**精确可表示**的：把 Δ 投影到基函数上（线性最小二乘）后直接
        更新 ``b`` 系数，``rate()`` 结果与插值曲线完全一致。

        Parameters
        ----------
        kind : {"parallel", "steepen", "flatten", "butterfly"}
            冲击方向。
        bp : float
            冲击幅度（基点），**必须 ≥ 0**。

        Raises
        ------
        ValueError
            ``kind`` 非法、``bp`` 非有限，或 ``bp < 0``。
        """
        kind = str(kind).lower()
        if kind not in _VALID_SHOCKS:
            raise ValueError(f"kind 必须是 {_VALID_SHOCKS} 之一，实际 {kind!r}")
        bp_value = float(bp)
        if not np.isfinite(bp_value):
            raise ValueError("bp 必须为有限数")
        if bp_value < 0:
            raise ValueError(
                "bp 是冲击幅度，必须非负；方向请用 kind 表达（steepen/flatten/butterfly），"
                "带符号的平移请用 shift(bp)"
            )
        amp = bp_value / 100.0

        delta = self._shock_delta_pct(kind, amp)
        param = self._param_array
        if param is None:
            new_rates = self.rates + delta
            new_params: dict[str, float] = {}
        else:
            tau_vec = param[3:] if self.method == "ns" else param[4:]
            basis = design_matrix(self.method, self.tenors, tau_vec)
            db, *_ = np.linalg.lstsq(basis, delta, rcond=None)
            new_params = dict(self.params)
            for name, value in zip(coef_names(self.method), db, strict=True):
                new_params[name] = new_params[name] + float(value)
            new_param_array = np.array(
                [new_params[name] for name in self._param_order], dtype=float
            )
            new_rates = np.asarray(
                model_rate(self.method, new_param_array, self.tenors), dtype=float
            )
        return Curve(
            as_of=self.as_of,
            tenors=self.tenors.copy(),
            rates=new_rates,
            method=self.method,
            params=new_params,
        )

    @property
    def _param_order(self) -> list[str]:
        """构造参数数组时的键顺序（NS: b0,b1,b2,tau；NSS: b0,b1,b2,b3,tau1,tau2）。"""
        if self.method == "ns":
            return ["b0", "b1", "b2", "tau"]
        return ["b0", "b1", "b2", "b3", "tau1", "tau2"]

    def _shock_delta_pct(self, kind: str, amp: float) -> np.ndarray:
        """按冲击类型给出节点上的**加性**变化（百分数，未 clamp）。"""
        if kind == "parallel":
            return np.full_like(self.rates, amp)
        tau = self._tau_ref
        l1, l2 = ns_loadings(self.tenors, tau)
        if kind == "steepen":
            return amp * (0.5 - l1)
        if kind == "flatten":
            return -amp * (0.5 - l1)
        # 蝶式：以 L2 载荷归一化为 [-0.5, +1]，腹部 +bp、两端 -bp/2
        return amp * (1.5 * l2 / _L2_MAX - 0.5)

    # ------------------------------------------------------------------
    # 输出
    # ------------------------------------------------------------------
    def to_frame(self) -> pd.DataFrame:
        """导出为长表：``columns=["tenor", "ytm_pct"]``（单位：年 / 百分数）。"""
        return pd.DataFrame(
            {
                "tenor": self.tenors.astype(float),
                "ytm_pct": self.rates.astype(float),
            }
        )

    def __repr__(self) -> str:
        return (
            f"Curve(as_of={self.as_of!s}, method={self.method!r}, "
            f"n_tenors={self.tenors.size}, range=[{self.tenors[0]:g}, {self.tenors[-1]:g}]y)"
        )

    # ------------------------------------------------------------------
    # 便捷构造
    # ------------------------------------------------------------------
    @classmethod
    def from_params(
        cls,
        as_of: date,
        params: Mapping[str, float],
        *,
        tenors: np.ndarray | None = None,
        method: str = "nss",
    ) -> Curve:
        """由参数直接构造曲线（``tenors`` 默认用配置的 11 个标准期限）。

        R09 口径：**参数键统一由 :func:`normalize_params` 规范化**——NS 接受
        ``tau`` 或别名 ``tau1``（面板导出的就是 ``tau1``），NSS 接受 ``tau1/tau2``。
        参数缺失/不完整直接抛 ``ValueError``，不会静默换模型。

        Parameters
        ----------
        as_of : datetime.date
            曲线日期。
        params : Mapping[str, float]
            参数（NS: b0,b1,b2,tau[/tau1]；NSS: b0,b1,b2,b3,tau1,tau2）。
        tenors : np.ndarray | None
            求值期限（年）；``None`` 时用配置的标准期限。
        method : {"ns", "nss"}
            曲线类型。

        Raises
        ------
        ValueError
            method 非法、参数缺失或非有限。
        """
        method_str = str(method).lower()
        normalized = normalize_params(method_str, params)
        tenors_arr = (
            np.asarray(tenors, dtype=float)
            if tenors is not None
            else np.asarray(get_config().curve.pca_tenors, dtype=float)
        )
        param_array = np.array([normalized[name] for name in _param_order_of(method_str)])
        rates = np.asarray(model_rate(method_str, param_array, tenors_arr), dtype=float)
        return cls(
            as_of=as_of,
            tenors=tenors_arr,
            rates=rates,
            method=method_str,  # type: ignore[arg-type]
            params=normalized,
        )
