"""即期（零息）曲线 bootstrap：从 YTM 曲线反推合成即期曲线。

⚠️ 合成假设声明（需求方明确要求显式声明，代码、docstring、``SpotCurve.assumptions``
三处同步）
------------------------------------------------------------------------------
本模块产出的是**从估值曲线反推的合成即期曲线**，**不是**真实成交曲线、也**不是**
中债官方即期曲线。合成假设如下：

1. **平价债券假设**：把 YTM 曲线的每个期限点视为一只**平价债券**——
   票面利率 = 该期限的到期收益率（百分数），价格 = 100。
2. **付息频率（中债条款口径，与 ``config.pricing.frequency_by_tenor`` 一致）**：

   - ``t < 1Y``：零息、单现金流（``freq=0``）；
   - ``1Y–7Y``：年付（``freq=1``）；
   - ``10Y/15Y/20Y/30Y``：半年付（``freq=2``）。

   可用 ``freq_rule`` 覆盖（键支持 ``"10y"`` / ``"10Y"`` / ``"10.0"`` 等形式）。
3. **贴现约定**：

   - ``t < 1Y`` 的零息平价债用**单利**：``df = 1/(1 + y·t)``（``y`` 为小数）；
   - ``t ≥ 1Y`` 的附息平价债按分频复利约定付息与报价
     ``(1 + y/f)^(-f·t_i)``（与 ``ficc.pricing.bond`` 的 YTM 口径一致），
     但 bootstrap 时现金流一律用**零息曲线**贴现，解出使 PV=100 的零息利率。
   - 零息曲线本身用**连续复利**表示：``df(t) = exp(-z(t)/100 · t)``，``z`` 为百分数。
4. **插值**：相邻已知零息点之间对**零息利率做线性插值**（不是远期插值，也不是
   对贴现因子插值）；区间外按端点两点的斜率线性外推。
5. **结算（R17）**：观察日 ``observation_date = curve.as_of``；结算日
   ``settlement_date = 观察日 + T+N 个**工作日**``（统一走
   :mod:`ficc.utils.calendar`，当前 T+1；节假日表尚未接入，见该模块 TODO）。
   例：2024-06-28（周五）→ 2024-07-01（周一），**不是** 2024-06-29（周六）。
   ``times`` 为**自结算日**起的名义年限（直接采用期限标签，不做 ACT/ACT 日历
   分数调整——这是本合成曲线的一处已知简化，不是官方即期曲线的对账口径）。
   调用方可用 ``bootstrap_spot(..., settlement=...)`` 显式覆盖结算日。
6. 曲线外的期限靠**线性外推**，仅作定价占位，不应作为交易依据。

核心恒等式（bootstrap 正确性的硬证据）
--------------------------------------
对每个期限 ``T``，用 bootstrap 出的即期曲线给对应合成平价债重新估值，必须
精确等于 100（数值误差 ≤ 1e-10 量级）。见 ``tests/test_bootstrap.py`` 的往返测试。

为什么用平价债券 bootstrap
--------------------------
中债只公开发布"到期收益率曲线"，即期/远期需自行推导（见
`docs/00_market_conventions.md` §1.2）。平价债 bootstrap 是最小假设的做法：
它只要求"每个期限点的 YTM 是平价债收益率"，不需要个券现金流或成交数据，
因此结论可复现、可审计；代价是**它继承估值曲线的合成属性**，不能当作真实
可成交即期利率使用。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import numpy as np
import pandas as pd
from scipy.optimize import brentq

from ficc.config import get_config
from ficc.curve.curve import Curve
from ficc.data.schema import TENOR_LABELS
from ficc.utils.calendar import is_business_day
from ficc.utils.calendar import settlement_date as _calendar_settlement_date

__all__ = [
    "SpotCurve",
    "bootstrap_spot",
    "default_freq_for_tenor",
    "frequency_for_tenor",
    "price_synthetic_par_bond",
]

# 连续复利零息曲线的数值边界（bootstrap 求根区间；单位百分数）
_ZERO_BRACKET_LOW: float = -50.0
_ZERO_BRACKET_HIGH: float = 200.0

_FREQ_ALIASES: dict[str, int] = {
    "zero": 0,
    "single": 0,
    "discount": 0,
    "lump_sum": 1,  # 到期一次还本付息：与年付 1Y 单笔现金流等价
    "annual": 1,
    "yearly": 1,
    "semiannual": 2,
    "semi_annual": 2,
    "biannual": 2,
}


# ---------------------------------------------------------------------------
# 零息利率插值（模块级函数，保证 bootstrap 与 SpotCurve 用同一套口径）
# ---------------------------------------------------------------------------
def _zero_rate_interp(times: np.ndarray, zeros: np.ndarray, t: float | np.ndarray) -> np.ndarray:
    """在已知零息点上做**线性**插值；区间外按端点两点斜率线性外推。"""
    times = np.asarray(times, dtype=float)
    zeros = np.asarray(zeros, dtype=float)
    t_arr = np.atleast_1d(np.asarray(t, dtype=float))
    if times.size == 1:
        return np.full_like(t_arr, zeros[0], dtype=float)
    if times.size != zeros.size:
        raise ValueError(f"times 与 zeros 长度不一致: {times.size} vs {zeros.size}")
    out = np.empty_like(t_arr, dtype=float)
    inside = (t_arr >= times[0]) & (t_arr <= times[-1])
    below = t_arr < times[0]
    above = t_arr > times[-1]
    if np.any(inside):
        out[inside] = np.interp(t_arr[inside], times, zeros)
    if np.any(below):
        slope = (zeros[1] - zeros[0]) / (times[1] - times[0])
        out[below] = zeros[0] + slope * (t_arr[below] - times[0])
    if np.any(above):
        slope = (zeros[-1] - zeros[-2]) / (times[-1] - times[-2])
        out[above] = zeros[-1] + slope * (t_arr[above] - times[-1])
    return out


# ---------------------------------------------------------------------------
# 付息频率规则
# ---------------------------------------------------------------------------
def default_freq_for_tenor(tenor: float) -> int:
    """默认付息频率：``<1Y`` 零息（0）、``1Y–7Y`` 年付（1）、``≥10Y`` 半年付（2）。

    与 ``config.pricing.frequency_by_tenor`` 的语义一致（1Y 的"利随本清"与年付
    1Y 都是到期单笔现金流，故统一用 ``freq=1``）。
    """
    t = float(tenor)
    if t < 1.0:
        return 0
    if t < 8.0:
        return 1
    return 2


def _parse_freq_rule(freq_rule: Mapping[Any, Any]) -> dict[float, int]:
    """把用户传入的频率规则解析成 ``{tenor_years: freq}``。"""
    parsed: dict[float, int] = {}
    for key, value in dict(freq_rule).items():
        tenor = _tenor_from_key(key)
        freq = _freq_from_value(value)
        parsed[tenor] = freq
    return parsed


def _tenor_from_key(key: Any) -> float:
    """把 ``"3m"`` / ``"10Y"`` / ``"10.0"`` / ``10`` 统一解析成期限（年）。"""
    if isinstance(key, (int, float)) and not isinstance(key, bool):
        return float(key)
    text = str(key).strip()
    upper = text.upper()
    if upper in TENOR_TO_LABEL_SET:
        # 标签形式（"3M"/"10Y"）——通过 TENOR_LABELS 的逆映射解析
        for tenor, label in _LABEL_LOOKUP.items():
            if label.upper() == upper:
                return tenor
    try:
        return float(text)
    except ValueError as exc:
        raise ValueError(f"无法解析期限键 {key!r}；支持 '3m'/'10Y'/10/10.0 等形式") from exc


def _freq_from_value(value: Any) -> int:
    """把频率值解析成整数（0=零息单现金流，1=年付，2=半年付）。"""
    if isinstance(value, str):
        key = value.strip().lower()
        if key in _FREQ_ALIASES:
            return _FREQ_ALIASES[key]
        raise ValueError(f"未知付息频率 {value!r}；可用整数 0/1/2 或 {sorted(_FREQ_ALIASES)}")
    if isinstance(value, bool):
        raise ValueError(f"频率不能是 bool: {value!r}")
    as_float = float(value)
    if not np.isfinite(as_float) or abs(as_float - round(as_float)) > 1e-9:
        raise ValueError(f"频率必须是整数，实际 {value!r}")
    freq = round(as_float)
    if freq < 0:
        raise ValueError(f"频率必须 ≥0，实际 {freq}")
    return freq


# 标签反查表（避免依赖 dict 顺序）
_LABEL_LOOKUP: dict[float, str] = {float(k): v for k, v in TENOR_LABELS.items()}
TENOR_TO_LABEL_SET: frozenset[str] = frozenset(label.upper() for label in _LABEL_LOOKUP.values())


def frequency_for_tenor(tenor: float, freq_rule: Mapping[str, int] | None = None) -> int:
    """取某期限的付息频率（``freq_rule`` 覆盖 > 默认规则）。

    Parameters
    ----------
    tenor : float
        期限（年）。
    freq_rule : Mapping | None
        自定义规则，键为期限标签/数值；未覆盖的期限走 :func:`default_freq_for_tenor`。

    Raises
    ------
    ValueError
        ``freq_rule`` 里的键无法解析，或键指定了曲线上不存在的期限（不静默忽略）。
    """
    if freq_rule is None:
        return default_freq_for_tenor(tenor)
    for key, value in dict(freq_rule).items():
        if abs(_tenor_from_key(key) - float(tenor)) < 1e-9:
            return _freq_from_value(value)
    return default_freq_for_tenor(tenor)


# ---------------------------------------------------------------------------
# 合成平价债现金流
# ---------------------------------------------------------------------------
def _cashflow_times(tenor: float, freq: int) -> np.ndarray:
    """合成平价债的现金流时点（年）：``freq=0`` 为单笔到期现金流。"""
    t = float(tenor)
    if freq == 0:
        return np.array([t])
    n = round(t * freq)
    if n == 0 or abs(n / freq - t) > 1e-8:
        raise ValueError(f"期限 {t} 与付息频率 {freq} 不匹配（不能整除）")
    return np.arange(1, n + 1, dtype=float) / freq


def _par_bond_pv(
    tenor: float, ytm_pct: float, freq: int, knots_t: np.ndarray, knots_z: np.ndarray
) -> float:
    """按给定零息节点给"票面=ytm 的平价债"估值（价格，面值 100）。

    ``freq=0``（t<1Y）按单利平价的定义：期初价格 100、到期一次还本付息
    ``100·(1 + y·t)``（y 为小数），因此 ``df = 1/(1+y·t)`` 时 PV 恰为 100。
    """
    times = _cashflow_times(tenor, freq)
    zeros = _zero_rate_interp(knots_t, knots_z, times)
    dfs = np.exp(-zeros / 100.0 * times)
    if freq == 0:
        maturity_value = 100.0 * (1.0 + float(ytm_pct) / 100.0 * float(tenor))
        return maturity_value * float(dfs[0])
    coupon = float(ytm_pct) / freq  # 每期票息（元/百元面值）
    pv = coupon * float(np.sum(dfs[:-1]))
    pv += (100.0 + coupon) * float(dfs[-1])
    return pv


def price_synthetic_par_bond(
    spot: SpotCurve,
    tenor: float,
    coupon_pct: float | None = None,
    freq: int | None = None,
) -> float:
    """用 ``spot`` 给"票面=该期限 YTM、价格应为 100"的合成平价债重新估值。

    这是 bootstrap 往返一致性的验收工具：``coupon_pct`` 默认从
    ``spot.assumptions["coupon_pct_by_tenor"]`` 里取；``freq`` 默认从
    ``assumptions["freq_rule"]`` 里取。返回价格（面值 100）。
    """
    t = float(tenor)
    coupon = coupon_pct
    if coupon is None:
        by_tenor = spot.assumptions.get("coupon_pct_by_tenor", {})
        key = _label_for(t)
        if key not in by_tenor:
            raise ValueError(
                f"assumptions 里没有 {t} 的票面；请显式传 coupon_pct 或确认该期限在曲线内"
            )
        coupon = float(by_tenor[key])
    f = freq
    if f is None:
        rule = spot.assumptions.get("freq_rule", {})
        f = frequency_for_tenor(t, rule) if rule else default_freq_for_tenor(t)
    if f is None:  # pragma: no cover - 防御式
        f = default_freq_for_tenor(t)
    times = _cashflow_times(t, int(f))
    dfs = np.asarray(spot.discount(times), dtype=float)
    if int(f) == 0:
        # 单利零息平价债：到期还本付息 100·(1 + y·t)
        maturity_value = 100.0 * (1.0 + float(coupon) / 100.0 * t)
        return maturity_value * float(dfs[0])
    c = float(coupon) / int(f)
    return float(c * np.sum(dfs[:-1]) + (100.0 + c) * dfs[-1])


def _label_for(tenor: float) -> str:
    """期限 → 可读标签（用于 assumptions 的 JSON 友好键）。"""
    return TENOR_LABELS.get(float(tenor), f"{float(tenor):g}Y")


# ---------------------------------------------------------------------------
# SpotCurve
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class SpotCurve:
    """连续复利零息曲线（百分数）+ 合成假设留痕。

    Parameters
    ----------
    as_of : date
        曲线日期（= 观察日）。保留该字段是为了向后兼容；它与
        ``observation_date`` 必须一致（见 ``__post_init__``）。
    times : np.ndarray
        零息节点（年，自**结算日**起），严格递增、无 NaN。
    observation_date : date | None
        观察日（曲线估值日），默认等于 ``as_of``。
    settlement_date : date | None
        结算日（现金流/贴现起点，T+N 交易日），默认等于 ``observation_date``；
        必须 ≥ ``observation_date``，且是 ``calendar`` 的交易日（未传日历时按工作日规则）。
    zero_rates_pct : np.ndarray
        连续复利零息利率，**百分数**（``2.35`` 表示 2.35%）；与 ``times`` 等长。
    method : str
        构造方法，本模块为 ``"par_bootstrap"``。
    assumptions : Mapping[str, Any]
        合成假设留痕（平价债、票面、频率、插值、结算、数据来源等）。
    source_curve_method : str
        源 YTM 曲线的构造方法（``"linear"`` / ``"nss"`` 等）。

    Notes
    -----
    - 零息利率用**线性插值**，区间外按端点斜率线性外推（见模块 docstring 声明）。
    - ``discount`` 用连续复利 ``exp(-z/100·t)``；``forward`` 为连续复利区间远期。
    """

    as_of: date
    times: np.ndarray
    zero_rates_pct: np.ndarray
    method: str
    assumptions: Mapping[str, Any] = field(default_factory=dict)
    source_curve_method: str = ""
    observation_date: date | None = None
    settlement_date: date | None = None
    # R27：真实 CN_IB 日历；存在时用它校验 settlement（调休周六也是交易日）
    calendar: Any = None

    def __post_init__(self) -> None:
        as_of = self.as_of
        if isinstance(as_of, datetime):
            as_of = as_of.date()
        if not isinstance(as_of, date):
            raise TypeError(f"as_of 必须是 datetime.date，实际 {type(as_of).__name__}")
        times = np.asarray(self.times, dtype=float).ravel()
        zeros = np.asarray(self.zero_rates_pct, dtype=float).ravel()
        if times.size != zeros.size:
            raise ValueError(f"times 与 zero_rates_pct 长度不一致: {times.size} vs {zeros.size}")
        if times.size < 1:
            raise ValueError("即期曲线至少需要 1 个节点")
        if not np.all(np.isfinite(times)) or not np.all(np.isfinite(zeros)):
            raise ValueError("times / zero_rates_pct 含 NaN/inf")
        if np.any(times <= 0):
            raise ValueError("times 必须为正数（年）")
        if np.any(np.diff(times) <= 0):
            raise ValueError("times 必须严格递增且不重复")
        method = str(self.method)

        def _as_date(value: date | datetime | None, name: str) -> date | None:
            if value is None:
                return None
            if isinstance(value, datetime):
                value = value.date()
            if not isinstance(value, date):
                raise TypeError(f"{name} 必须是 datetime.date，实际 {type(value).__name__}")
            return value

        observation_date = _as_date(self.observation_date, "observation_date") or as_of
        if observation_date != as_of:
            raise ValueError(
                f"observation_date({observation_date}) 必须等于 as_of({as_of})；"
                "as_of 是曲线估值日，两者是同一概念"
            )
        settlement_date = _as_date(self.settlement_date, "settlement_date") or observation_date
        if settlement_date < observation_date:
            raise ValueError(
                f"settlement_date({settlement_date}) 不能早于 observation_date({observation_date})"
            )
        calendar = self.calendar
        if calendar is not None:
            if not hasattr(calendar, "is_trading_day"):
                raise TypeError("calendar 必须提供 is_trading_day(d)")
            if not calendar.is_trading_day(settlement_date):
                raise ValueError(
                    f"settlement_date({settlement_date}) 不是 CN_IB 交易日；"
                    "结算日必须按传入的 TradingCalendar 推算"
                )
        elif not is_business_day(settlement_date):
            raise ValueError(
                f"settlement_date({settlement_date}) 不是工作日；结算日必须按工作日规则推算"
                "（见 ficc.utils.calendar）"
            )
        assumptions = dict(self.assumptions or {})
        object.__setattr__(self, "as_of", as_of)
        object.__setattr__(self, "observation_date", observation_date)
        object.__setattr__(self, "settlement_date", settlement_date)
        object.__setattr__(self, "times", times)
        object.__setattr__(self, "zero_rates_pct", zeros)
        object.__setattr__(self, "method", method)
        object.__setattr__(self, "assumptions", assumptions)
        object.__setattr__(self, "source_curve_method", str(self.source_curve_method))

    # ------------------------------------------------------------------
    # 求值
    # ------------------------------------------------------------------
    def zero_rate(self, t: float | np.ndarray) -> float | np.ndarray:
        """``t``（年）处的连续复利零息利率（百分数，线性插值/外推）。"""
        scalar = np.ndim(t) == 0
        t_arr = np.atleast_1d(np.asarray(t, dtype=float))
        if not np.all(np.isfinite(t_arr)):
            raise ValueError("t 含 NaN/inf")
        if np.any(t_arr < 0):
            raise ValueError("t（期限）不能为负")
        values = _zero_rate_interp(self.times, self.zero_rates_pct, t_arr)
        return float(values[0]) if scalar else values

    def discount(self, t: float | np.ndarray) -> float | np.ndarray:
        """贴现因子 ``exp(-z/100·t)``（连续复利）。"""
        scalar = np.ndim(t) == 0
        t_arr = np.atleast_1d(np.asarray(t, dtype=float))
        if not np.all(np.isfinite(t_arr)):
            raise ValueError("t 含 NaN/inf")
        if np.any(t_arr < 0):
            raise ValueError("t（期限）不能为负")
        values = np.exp(-self.zero_rate(t_arr) / 100.0 * t_arr)
        return float(values[0]) if scalar else values

    def forward(self, t1: float, t2: float) -> float:
        """``[t1, t2]`` 的连续复利区间远期利率（百分数）。

        公式：``f = (z(t2)·t2 - z(t1)·t1) / (t2 - t1)``。
        """
        t1 = float(t1)
        t2 = float(t2)
        if not np.isfinite([t1, t2]).all():
            raise ValueError("t1/t2 必须为有限数")
        if t1 < 0:
            raise ValueError("t1 不能为负")
        if t2 <= t1:
            raise ValueError(f"t2({t2}) 必须大于 t1({t1})")
        z1 = float(self.zero_rate(t1))
        z2 = float(self.zero_rate(t2))
        return (z2 * t2 - z1 * t1) / (t2 - t1)

    def to_frame(self) -> pd.DataFrame:
        """导出为 DataFrame：``columns=["tenor", "zero_pct", "discount"]``。"""
        return pd.DataFrame(
            {
                "tenor": self.times.astype(float),
                "zero_pct": self.zero_rates_pct.astype(float),
                "discount": np.exp(-self.zero_rates_pct / 100.0 * self.times),
            }
        )

    def __repr__(self) -> str:
        return (
            f"SpotCurve(as_of={self.as_of!s}, method={self.method!r}, "
            f"n_nodes={self.times.size}, range=[{self.times[0]:g}, {self.times[-1]:g}]y)"
        )


# ---------------------------------------------------------------------------
# Bootstrap
# ---------------------------------------------------------------------------
def bootstrap_spot(
    curve: Curve,
    *,
    settlement: date | None = None,
    calendar: object | None = None,
    freq_rule: Mapping[str, int] | None = None,
) -> SpotCurve:
    """从 YTM 曲线 bootstrap 合成即期（零息）曲线。

    算法（逐期限、最小假设）
    ------------------------
    按期限从短到长，把每个期限点视为一只**平价债券**（票面=该期限 YTM、价格=100）：

    - ``t < 1Y``：零息单现金流，直接由单利约定反推
      ``df(t) = 1/(1 + y·t)``，再换算成连续复利零息利率；
    - ``t ≥ 1Y``：按频率 ``f`` 生成付息现金流（票息 ``y/f``、到期还本 100），
      用已知零息节点（+ 待求节点）做**零息利率线性插值**贴现，用 Brent 法解
      ``PV(z)=100`` 得到该期限零息利率；
    - ``t`` 之后（曲线上限之外）的期限由 :meth:`SpotCurve.zero_rate` 线性外推。

    Parameters
    ----------
    curve : Curve
        源曲线（通常为 ``ficc.curve.fit_curve`` 的产物）；用其 ``tenors``/``rates``。
    settlement : date | None
        结算日（现金流/贴现起点）。``None`` 时：传了 ``calendar`` 就按真实
        CN_IB 日历推算（含调休工作日，如 2019-02-02 周六是交易日）；未传
        ``calendar`` 时退回简单工作日规则（2024-06-28 周五 → 2024-07-01 周一）。
        显式传入时必须 ≥ ``curve.as_of`` 且是**该日历的交易日**，否则抛 ``ValueError``。
    calendar : object | None
        ``TradingCalendar``（或同接口对象）；用于结算日推算与校验。R27：调用方
        必须显式传入，避免"曲线层 10-01、账本 10-08"的日期契约割裂。
    freq_rule : Mapping[str, int] | None
        付息频率覆盖（键支持 ``"10Y"/"10.0"/10`` 等）；``None`` 用默认中债规则：
        ``<1Y`` 零息、``1Y–7Y`` 年付、``≥10Y`` 半年付。

    Returns
    -------
    SpotCurve
        连续复利零息曲线（百分数）；``observation_date``/``settlement_date`` 记录
        日期契约，``assumptions`` 内含全部合成假设留痕（含实际结算日与来源）。

    Raises
    ------
    ValueError
        曲线期限非法、频率规则无法解析/期限不匹配、结算日非法（早于观察日或
        非工作日）、求根失败等。

    Examples
    --------
    >>> from datetime import date
    >>> from ficc.curve.curve import Curve
    >>> c = Curve(as_of=date(2024, 6, 28), tenors=[1.0, 2.0, 3.0],
    ...           rates=[2.0, 2.2, 2.3], method="linear")
    >>> spot = bootstrap_spot(c)
    >>> round(price_synthetic_par_bond(spot, 3.0), 8)
    100.0
    """
    tenors = np.asarray(curve.tenors, dtype=float).ravel()
    rates = np.asarray(curve.rates, dtype=float).ravel()
    order = np.argsort(tenors, kind="stable")
    tenors = tenors[order]
    rates = rates[order]
    if tenors.size < 1:
        raise ValueError("曲线没有期限点")
    if np.any(np.diff(tenors) <= 0):
        raise ValueError("曲线 tenors 必须严格递增")
    if not np.all(np.isfinite(rates)):
        raise ValueError("曲线 rates 含 NaN/inf")
    # 附息债的第一笔现金流时点为 1/f（半年付=0.5Y）；若曲线首节点晚于它，
    # bootstrap 必须对短端做区间外线性外推，解会不稳健 → 直接拒绝并提示。
    default_freqs = [default_freq_for_tenor(t) for t in tenors]
    coupon_freqs = [f for f in default_freqs if f > 0]
    if freq_rule is not None:
        parsed_preview = _parse_freq_rule(freq_rule)
        coupon_freqs = [f for f in (_freq_for(t, parsed_preview) for t in tenors) if f > 0]
    if coupon_freqs:
        first_coupon_time = min(1.0 / f for f in coupon_freqs)
        if float(tenors[0]) > first_coupon_time + 1e-9:
            raise ValueError(
                f"曲线缺少短端锚点：首节点 {float(tenors[0])}Y 晚于第一笔票息时点 "
                f"{first_coupon_time:.4f}Y，bootstrap 需要在短端做区间外外推，结果不稳健；"
                "请补一个 ≤0.5Y 的短端期限点"
            )

    cfg = get_config()
    lag = int(cfg.pricing.settlement_lag_days)
    observation = curve.as_of
    if calendar is not None and not hasattr(calendar, "settlement_date"):
        raise TypeError("calendar 必须提供 settlement_date(as_of, lag) 与 is_trading_day(d)")
    if settlement is None:
        if calendar is not None:
            resolved_settlement = calendar.settlement_date(observation, lag)  # type: ignore[attr-defined]
            settlement_source = "calendar"
        else:
            resolved_settlement = _calendar_settlement_date(observation, lag)
            settlement_source = "config"
    else:
        resolved_settlement = settlement
        if isinstance(resolved_settlement, datetime):
            resolved_settlement = resolved_settlement.date()
        if not isinstance(resolved_settlement, date):
            raise TypeError(f"settlement 必须是 datetime.date，实际 {type(settlement).__name__}")
        settlement_source = "explicit"
    if resolved_settlement < observation:
        raise ValueError(
            f"settlement({resolved_settlement}) 不能早于 observation date({observation})"
        )
    if calendar is not None:
        if not calendar.is_trading_day(resolved_settlement):  # type: ignore[attr-defined]
            raise ValueError(
                f"settlement({resolved_settlement}) 不是 CN_IB 交易日；"
                "结算日必须按传入的 TradingCalendar 推算"
            )
    elif not is_business_day(resolved_settlement):
        raise ValueError(
            f"settlement({resolved_settlement}) 不是工作日；结算日必须按工作日规则推算"
            "（2024-06-28 周五的 T+1 是 2024-07-01 周一，不是 06-29 周六）"
        )
    settlement = resolved_settlement

    # 解析频率规则；若用户给了规则，检查键确实对应曲线期限（不静默忽略拼写错误）
    if freq_rule is None:
        parsed_rule: dict[float, int] = {}
        freqs = [default_freq_for_tenor(t) for t in tenors]
    else:
        parsed_rule = _parse_freq_rule(freq_rule)
        unknown = sorted(set(parsed_rule) - {round(float(t), 10) for t in tenors})
        if unknown:
            raise ValueError(
                f"freq_rule 指定了曲线上不存在的期限 {unknown}；"
                f"曲线期限为 {[round(float(t), 4) for t in tenors]}"
            )
        freqs = [_freq_for(t, parsed_rule) for t in tenors]

    knot_t: list[float] = []
    knot_z: list[float] = []
    for tenor, ytm, freq in zip(tenors, rates, freqs, strict=True):
        t = float(tenor)
        y = float(ytm)
        if freq == 0:
            # 零息单利：df = 1/(1+y·t)，y 为小数
            df = 1.0 / (1.0 + y / 100.0 * t)
            if df <= 0:
                raise ValueError(f"期限 {t} 的单利贴现因子非正（y={y}）")
            z = -100.0 * float(np.log(df)) / t
        else:

            def _pv(
                z_candidate: float,
                _t: float = t,
                _y: float = y,
                _freq: int = int(freq),
            ) -> float:
                return _par_bond_pv(
                    _t,
                    _y,
                    _freq,
                    np.asarray([*knot_t, _t], dtype=float),
                    np.asarray([*knot_z, z_candidate], dtype=float),
                )

            low = _ZERO_BRACKET_LOW
            high = _ZERO_BRACKET_HIGH
            if _pv(low) <= 100.0:
                raise ValueError(f"期限 {t} 在零息下界 {low}% 处仍估不到 100（曲线异常，y={y}）")
            if _pv(high) >= 100.0:
                raise ValueError(f"期限 {t} 在零息上界 {high}% 处仍估高于 100（曲线异常，y={y}）")
            z = float(brentq(lambda zz: _pv(zz) - 100.0, low, high, xtol=1e-14, rtol=1e-14))
        knot_t.append(t)
        knot_z.append(z)

    times_arr = np.asarray(knot_t, dtype=float)
    zeros_arr = np.asarray(knot_z, dtype=float)
    assumptions: dict[str, Any] = {
        "synthetic_not_official": True,
        "description": ("从估值曲线反推的合成即期曲线：不是真实成交曲线，也不是中债官方即期曲线"),
        "par_bond": "每个期限点视为平价债券（par bond）：coupon_pct = 该期限 ytm_pct，price = 100",
        "par_identity_convention": (
            "附息平价债的 YTM 报价与分频复利约定 (1+y/f)^(-f·t_i) 一致"
            "（同 ficc.pricing.bond 的 YTM 口径）；bootstrap 时现金流用零息曲线贴现"
        ),
        "freq_rule": {_label_for(t): int(f) for t, f in zip(tenors, freqs, strict=True)},
        "short_end": (
            "t < 1Y：零息平价债，期初 100、到期一次还本付息 100·(1+y·t)（单利），"
            "故 df = 1/(1 + y·t)"
        ),
        "coupon_bonds": "t ≥ 1Y：每年付息 f 次，每期票息 y/f，到期还本 100",
        "interpolation": "相邻已知零息点之间对零息利率做线性插值（未用远期插值）",
        "extrapolation": "区间外按端点两点斜率线性外推（仅占位，不作交易依据）",
        "compounding": "零息曲线为连续复利、百分数；df(t) = exp(-z/100 · t)",
        "date_basis": "times 为自结算日起的名义年限（直接采用期限标签，不做 ACT/ACT 日历分数调整）",
        "observation_date": observation.isoformat(),
        "settlement_date": settlement.isoformat(),
        "settlement_lag_days": lag,
        "settlement_source": settlement_source,
        "settlement_rule": (
            "observation + N 个工作日（ficc.utils.calendar；节假日表待接入）；"
            "times 为自结算日起的名义年限，未做 ACT/ACT 日历分数调整"
        ),
        "source_curve_as_of": curve.as_of.isoformat(),
        "source_curve_method": curve.method,
        "source_curve_tenors": [float(t) for t in tenors],
        "source_curve_rates_pct": [float(r) for r in rates],
        "coupon_pct_by_tenor": {
            _label_for(t): float(r) for t, r in zip(tenors, rates, strict=True)
        },
    }
    return SpotCurve(
        as_of=curve.as_of,
        times=times_arr,
        zero_rates_pct=zeros_arr,
        method="par_bootstrap",
        assumptions=assumptions,
        source_curve_method=str(curve.method),
        observation_date=observation,
        settlement_date=settlement,
        calendar=calendar,
    )


def _freq_for(tenor: float, parsed_rule: Mapping[float, int]) -> int:
    """按已解析的规则取频率（精确匹配期限，未覆盖则用默认规则）。"""
    key = round(float(tenor), 10)
    for rule_tenor, freq in parsed_rule.items():
        if abs(rule_tenor - key) < 1e-9:
            return int(freq)
    return default_freq_for_tenor(tenor)
