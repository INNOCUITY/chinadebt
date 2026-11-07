"""债券风险指标：麦考利久期 / 修正久期 / 凸性 / DV01 / KRD。

口径（``docs/00_market_conventions.md`` §3.1）
---------------------------------------------
=================== =================================================== =============
指标                 定义                                                单位
=================== =================================================== =============
麦考利久期 D_mac      ``Σ t_i × PV_i / PV``（t_i 为年化现金流时间）        年
修正久期 D_mod        ``D_mac / (1 + y/f)``（价格弹性）                    年
凸性 C                ``Σ t_i(t_i+1/f) × PV_i / [PV × (1+y/f)²]``         年²
DV01                  ``全价 × D_mod × 1e-4``                             元/面值/bp
=================== =================================================== =============

⚠️ **久期与 DV01 一律用全价（dirty price）计算**。净价口径会系统性低估
（付息前 AI 可达全价 0.8%，净价 100.97 vs 全价 101.78），见 memo §3.3 陷阱 1。
DV01 单位是「元/报价面值/bp」（默认元/百元面值），与中债「估价基点价值」同口径。

配置口径
--------
报价面值、计息基准从 ``config.pricing`` 读取；需要自定义配置时把 ``cfg``
显式传给各函数（``cfg=None`` 时才读全局配置，不回落硬编码默认值）。

KRD（关键期限久期）用**即期（零息）曲线**实现：只依赖鸭子类型接口
``spot.times`` / ``spot.zero_rate(t)`` / ``spot.discount(t)``（曲线层
``ficc.curve.bootstrap`` 的产物；本模块不 import 它，避免循环依赖）。关键期限
单点 +1bp、其余关键点不动、关键点之间零息利率线性插值、首尾平推；各 KRD 之和
与平行 +1bp 的即期 DV01 按构造成立（浮点误差级）。**禁止**用
「总 DV01 × 期限占比」这类近似。
"""

from __future__ import annotations

import warnings
from collections.abc import Sequence
from datetime import date, datetime
from typing import Any

import numpy as np

from ficc.config import Config, get_config
from ficc.data.schema import BondStatic
from ficc.pricing.bond import cashflows, dirty_price_from_ytm
from ficc.pricing.conventions import (
    coupon_dates,
    coupon_period,
    day_count,
    quote_face_value,
    validate_settlement,
)
from ficc.utils.calendar import is_business_day

__all__ = [
    "convexity",
    "dv01",
    "dv01_finite_diff",
    "key_rate_durations",
    "macaulay_duration",
    "mod_duration",
    "spot_dv01",
]


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------
def _coupon_terms(
    bond: BondStatic, settlement: date, ytm_pct: float, *, cfg: Config | None = None
) -> tuple[float, list[tuple[float, float]]]:
    """规则附息券的 ``(全价, [(t_i, PV_i), ...])``，单位元/报价面值。"""
    freq = int(bond.freq)
    y = ytm_pct / 100.0
    base = 1.0 + y / freq
    period = coupon_period(bond, settlement, cfg=cfg)
    face = quote_face_value(cfg=cfg)
    annual_coupon = bond.coupon_pct * face / 100.0  # 元/面值/年
    per_coupon = annual_coupon / freq
    maturity = bond.maturity_date
    pv_total = 0.0
    terms: list[tuple[float, float]] = []
    index = 0
    for cf_date in coupon_dates(bond):
        if cf_date <= settlement:
            continue
        index += 1
        amount = per_coupon + (face if cf_date == maturity else 0.0)
        pv_i = amount * base ** (-(index - 1 + period.w))
        t_years = (index - 1 + period.w) / freq
        pv_total += pv_i
        terms.append((t_years, pv_i))
    return pv_total, terms


def _time_to_maturity_years(bond: BondStatic, settlement: date) -> float:
    """结算日 → 到期日的年化时间（利随本清/贴现券单笔现金流用）。"""
    return day_count(settlement, bond.maturity_date) / 365.0


# ---------------------------------------------------------------------------
# 久期与凸性
# ---------------------------------------------------------------------------
def macaulay_duration(
    bond: BondStatic, settlement: date, ytm_pct: float, *, cfg: Config | None = None
) -> float:
    """麦考利久期（年）：以现值为权重的现金流时间加权平均。

    规则附息：``Σ t_i × PV_i / PV``；
    利随本清/贴现（单笔现金流）：等于现金流年化时间 ``τ/365``。
    ``cfg`` 显式传入时用于报价面值/计息基准，不回落默认值。
    """
    validate_settlement(bond, settlement)
    if bond.accrual == "coupon":
        pv_total, terms = _coupon_terms(bond, settlement, ytm_pct, cfg=cfg)
        return sum(t * pv for t, pv in terms) / pv_total
    return _time_to_maturity_years(bond, settlement)


def mod_duration(
    bond: BondStatic, settlement: date, ytm_pct: float, *, cfg: Config | None = None
) -> float:
    """修正久期（年）：``D_mac / (1 + y/f)``，即 ``-(1/P)·dP/dy``。

    - 规则附息：``D_mac / (1 + y/f)``；
    - 利随本清（年复利：``P=A/(1+y)^τ``）：``τ / (1+y)``；
    - 贴现/零息（货币市场单利：``P=F/(1+yτ)``）：``τ / (1+yτ)``。
    """
    validate_settlement(bond, settlement)
    y = ytm_pct / 100.0
    if bond.accrual == "coupon":
        freq = int(bond.freq)
        return macaulay_duration(bond, settlement, ytm_pct, cfg=cfg) / (1.0 + y / freq)
    tau = _time_to_maturity_years(bond, settlement)
    if bond.accrual == "lump_sum":
        return tau / (1.0 + y)
    return tau / (1.0 + y * tau)


def convexity(
    bond: BondStatic, settlement: date, ytm_pct: float, *, cfg: Config | None = None
) -> float:
    """凸性（年²）：``C = (1/P)·d²P/dy²``。

    - 规则附息：``Σ t_i(t_i+1/f)·PV_i / [PV·(1+y/f)²]``（memo §3.1 口径）；
    - 利随本清（``P=A/(1+y)^τ``）：``τ(τ+1)/(1+y)²``；
    - 贴现/零息（``P=F/(1+yτ)``）：``2τ²/(1+yτ)²``。
    """
    validate_settlement(bond, settlement)
    y = ytm_pct / 100.0
    if bond.accrual == "coupon":
        freq = int(bond.freq)
        pv_total, terms = _coupon_terms(bond, settlement, ytm_pct, cfg=cfg)
        weighted = sum(t * (t + 1.0 / freq) * pv for t, pv in terms)
        return weighted / (pv_total * (1.0 + y / freq) ** 2)
    tau = _time_to_maturity_years(bond, settlement)
    if bond.accrual == "lump_sum":
        return tau * (tau + 1.0) / (1.0 + y) ** 2
    return 2.0 * tau**2 / (1.0 + y * tau) ** 2


# ---------------------------------------------------------------------------
# DV01
# ---------------------------------------------------------------------------
def dv01(bond: BondStatic, settlement: date, ytm_pct: float, *, cfg: Config | None = None) -> float:
    """DV01（基点价值），单位 **元/报价面值/bp**。

    ``DV01 = 全价 × 修正久期 × 1e-4``

    ⚠️ **必须用全价**（中债「估价基点价值」同口径）。若误用净价，付息前会低估
    0.8% 量级（memo §3.3 陷阱 1、决策 2）。
    """
    full_price = dirty_price_from_ytm(bond, settlement, ytm_pct, cfg=cfg)
    return full_price * mod_duration(bond, settlement, ytm_pct, cfg=cfg) * 1e-4


def dv01_finite_diff(
    bond: BondStatic,
    settlement: date,
    ytm_pct: float,
    *,
    bp: float = 1.0,
    cfg: Config | None = None,
) -> float:
    """用 ±``bp`` 中心差分估算 DV01（元/报价面值/bp），用于交叉校验解析式。

    ``DV01 ≈ [P(y − bp) − P(y + bp)] / (2 × bp)``

    注意：分子是「±bp 的价格差」，不是「每 1bp 的敏感度」，因此**必须除以步长
    ``bp``**（评审 R08）。``bp`` 以 bp 为单位（1bp = 0.01 个百分点）。
    与 :func:`dv01` 的解析值相对偏差应 < 1e-6（``bp`` 越大，凸性/三阶项略增）。
    """
    if bp <= 0.0:
        raise ValueError(f"bp 必须为正数，实际 {bp}")
    step = bp / 100.0  # 百分数单位下的步长（1bp = 0.01 个百分点）
    price_down = dirty_price_from_ytm(bond, settlement, ytm_pct - step, cfg=cfg)
    price_up = dirty_price_from_ytm(bond, settlement, ytm_pct + step, cfg=cfg)
    return (price_down - price_up) / (2.0 * bp)


# ---------------------------------------------------------------------------
# KRD：即期（零息）曲线关键期限久期
# ---------------------------------------------------------------------------
# 1bp 用小数表示（1bp = 0.0001 = 0.01 个百分点）；KRD 内部固定 1bp，不暴露步长参数。
_BP_DECIMAL: float = 1e-4


def _resolve_config(cfg: Config | None) -> Config:
    """显式 cfg 优先；``None`` 时读全局配置（不回落硬编码默认值）。"""
    return cfg if cfg is not None else get_config()


def _resolve_key_tenors(key_tenors: Sequence[float] | None, cfg: Config | None) -> np.ndarray:
    """关键期限网格：显式参数优先，否则读 ``cfg.risk.key_tenors_krd``。"""
    raw = (
        list(key_tenors)
        if key_tenors is not None
        else list(_resolve_config(cfg).risk.key_tenors_krd)
    )
    keys = np.asarray(raw, dtype=float)
    if keys.ndim != 1 or keys.size < 2:
        raise ValueError(f"key_tenors 至少需要 2 个期限点，实际 {raw!r}")
    if not np.all(np.isfinite(keys)):
        raise ValueError(f"key_tenors 含 NaN/inf: {raw!r}")
    if np.any(keys <= 0):
        raise ValueError(f"key_tenors 必须为正数（单位：年）: {raw!r}")
    if np.any(np.diff(keys) <= 0):
        raise ValueError(f"key_tenors 必须严格递增: {raw!r}")
    return keys


def _validate_spot(spot: Any) -> None:
    """校验即期曲线鸭子类型接口存在且可用（不 import 曲线层实现）。"""
    for attr in ("times", "zero_rate", "discount"):
        if not hasattr(spot, attr):
            raise TypeError(
                f"spot 缺少必需成员 {attr!r}：需要鸭子类型接口 "
                "times / zero_rate(t) / discount(t)（见 key_rate_durations docstring）"
            )
    times = np.asarray(spot.times, dtype=float).ravel()
    if times.size < 2 or np.any(np.diff(times) <= 0):
        raise ValueError("spot.times 必须是一维、严格递增、至少 2 个点的期限网格")


def _coerce_spot_date(value: Any, *, field: str) -> date | None:
    """把 spot 上的日期字段规范成 ``date``（None 原样返回）。"""
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    raise TypeError(f"spot.{field} 必须是 date/datetime，实际 {type(value).__name__}")


def _read_spot_dates(spot: Any) -> tuple[date | None, date | None, bool]:
    """读取即期曲线的日期契约（鸭子类型）。

    返回 ``(结算日, 参考日, 是否回退)``：

    - 结算日：优先 ``spot.settlement_date``；缺失时回退 ``observation_date``/
      ``as_of`` 并标记 ``used_fallback=True``；
    - 参考日：``observation_date``（曲线观察日），缺失回退 ``as_of``。

    曲线线（``spot.bootstrap``）正在给 ``SpotCurve`` 增加
    ``observation_date`` / ``settlement_date``；本函数不 import 曲线层实现。
    """
    expected = _coerce_spot_date(getattr(spot, "settlement_date", None), field="settlement_date")
    observed = _coerce_spot_date(getattr(spot, "observation_date", None), field="observation_date")
    as_of = _coerce_spot_date(getattr(spot, "as_of", None), field="as_of")
    reference = observed if observed is not None else as_of
    if expected is None:
        expected = reference
        return expected, reference, True
    return expected, reference, False


def _validate_krd_date_contract(
    settlement: date, spot: Any, *, allow_mismatch: bool = False
) -> None:
    """R17：校验 KRD 的结算日/工作日/曲线参考日契约。

    1. ``settlement`` 必须是工作日（周末不可结算）；
    2. ``settlement >= 曲线参考日``（观察日 / ``as_of``）；
    3. 若 ``spot`` 暴露 ``settlement_date``，要求 ``settlement == spot.settlement_date``；
       否则视为日期错位，默认抛 :class:`ValueError`；显式
       ``allow_mismatch=True`` 时只 warning（留出曲线线未升级的过渡期）。
       若 ``spot`` 只有 ``observation_date``/``as_of``（``settlement_date`` 缺失），
       回退用其做校验并 warning，提示应升级曲线对象。

    说明：定价层不隐式假设 ``curve.as_of == 结算日``——这是 R17 的核心错位。
    """
    calendar = getattr(spot, "calendar", None)
    if calendar is not None and hasattr(calendar, "is_trading_day"):
        if not calendar.is_trading_day(settlement):
            raise ValueError(
                f"KRD 结算日 {settlement}({settlement:%a}) 不是 CN_IB 交易日；"
                "请传入经 TradingCalendar 推算的结算日"
            )
    elif not is_business_day(settlement):
        raise ValueError(
            f"KRD 结算日 {settlement}({settlement:%a}) 不是工作日：结算不应发生在周末；"
            "请传入经 ficc.utils.calendar 推算的结算日"
        )

    expected, reference, used_fallback = _read_spot_dates(spot)
    if used_fallback:
        warnings.warn(
            "spot 未暴露 settlement_date，KRD 日期契约回退用 observation_date/as_of 校验；"
            "请升级曲线对象（SpotCurve 应提供 observation_date 与 settlement_date）",
            UserWarning,
            stacklevel=3,
        )
    if reference is not None and settlement < reference:
        raise ValueError(
            f"KRD 结算日 {settlement} 早于曲线参考日 {reference}：现金流起点不能早于曲线观察日"
        )
    if expected is not None and settlement != expected:
        message = (
            f"KRD 结算日 {settlement} 与曲线结算日 {expected} 不一致（R17 日期契约）；"
            "应以 spot.settlement_date 为现金流/贴现起点"
        )
        if allow_mismatch:
            warnings.warn(
                message + "；allow_mismatch=True，已放行但结果可能错位", UserWarning, stacklevel=3
            )
        else:
            raise ValueError(message + "；如确认要用其它结算日，请显式传 allow_mismatch=True")


def _spot_discount_vector(spot: Any, times: np.ndarray) -> np.ndarray:
    """把 ``spot.discount`` 的返回值统一成与 ``times`` 同形的一维数组。

    契约允许 ``discount`` 支持数组；若实现只接受标量（抛 ``TypeError``），
    这里自动退化为逐点求值，保证鸭子类型接口的最小可用性。
    """
    try:
        raw = spot.discount(times)
    except TypeError:  # 只支持标量的实现
        raw = np.asarray([spot.discount(float(t)) for t in times], dtype=float)
    disc = np.asarray(raw, dtype=float)
    if disc.ndim == 0:  # 允许只接受标量的实现
        disc = np.full(times.shape, float(disc))
    disc = disc.ravel()
    if disc.size != times.size:
        raise ValueError(f"spot.discount 返回长度 {disc.size} 与时间点数 {times.size} 不符")
    if not np.all(np.isfinite(disc)) or np.any(disc <= 0):
        raise ValueError("spot.discount 返回非有限值或非正折算因子")
    return disc


def _cashflow_times_and_amounts(
    bond: BondStatic, settlement: date, *, cfg: Config | None
) -> tuple[np.ndarray, np.ndarray]:
    """债券未来现金流的（年化时间, 金额）数组；时间口径 ACT/365。"""
    frame = cashflows(bond, settlement, cfg=cfg)
    times = np.asarray([day_count(settlement, d) / 365.0 for d in frame["date"]], dtype=float)
    amounts = np.asarray(frame["amount"], dtype=float)
    if times.size == 0:
        raise ValueError(f"{bond.code}@{settlement}: 无未来现金流，无法计算 KRD")
    return times, amounts


def _key_rate_weights(key_tenors: np.ndarray, times: np.ndarray) -> np.ndarray:
    """关键期限线性插值权重矩阵 ``(n_keys, n_times)``。

    每个现金流时间 ``t`` 的零息利率写成关键点利率的线性组合
    ``r(t) = Σ_k w_k(t) · r(τ_k)``：区间内按相邻关键点线性插值，首尾
    **平推（flat，权重落在最近端点）**。所有权重每列之和恒为 1，因此
    「所有关键点同时 +1bp」等价于曲线整体平行 +1bp（KRD 求和精确闭合）。
    """
    keys = np.asarray(key_tenors, dtype=float)
    t = np.asarray(times, dtype=float)
    n_keys = keys.size
    weights = np.zeros((n_keys, t.size), dtype=float)

    idx = np.searchsorted(keys, t, side="right") - 1
    idx = np.clip(idx, 0, n_keys - 2)
    left = keys[idx]
    right = keys[idx + 1]
    frac = (t - left) / (right - left)
    frac = np.clip(frac, 0.0, 1.0)  # t 在首端点左侧/末端点右侧时 → 平推
    cols = np.arange(t.size)
    weights[idx, cols] = 1.0 - frac
    weights[idx + 1, cols] = frac
    return weights


def _spot_cashflows(
    bond: BondStatic, settlement: date, spot: Any, *, cfg: Config | None
) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """返回 ``(现金流时间, 现金流金额, 基准折算因子)``，只是 spot 求值一次。"""
    times, amounts = _cashflow_times_and_amounts(bond, settlement, cfg=cfg)
    disc0 = _spot_discount_vector(spot, times)
    return times, amounts, disc0


def spot_dv01(bond: BondStatic, settlement: date, spot: Any, *, cfg: Config | None = None) -> float:
    """即期曲线平行 +1bp 的 DV01（**带符号**：多头为负值），元/报价面值/bp。

    零息利率为连续复利（百分数），平行 +1bp 使 ``discount(t)`` 乘以
    ``exp(-1e-4·t)``，故

    ``DV01_spot = Σ_i A_i · D(t_i) · (exp(-1e-4·t_i) − 1)``

    与 :func:`dv01` 的 YTM 口径不同：这里直接对**零息曲线**做平行移动，不做
    「把曲线压成单一到期收益率」的假设。KRD 之和应与此值在浮点误差内相等。
    """
    validate_settlement(bond, settlement)
    _validate_spot(spot)
    times, amounts, disc0 = _spot_cashflows(bond, settlement, spot, cfg=cfg)
    pv0 = float(np.dot(amounts, disc0))
    pv_parallel = float(np.dot(amounts, disc0 * np.exp(-_BP_DECIMAL * times)))
    return pv_parallel - pv0


def key_rate_durations(
    bond: BondStatic,
    settlement: date,
    spot: Any,
    *,
    key_tenors: Sequence[float] | None = None,
    allow_mismatch: bool = False,
    cfg: Config | None = None,
) -> dict[float, float]:
    """即期曲线口径的关键期限久期（KRD），单位 **元/报价面值/bp**。

    符号约定
    --------
    **多头、收益率上行 → KRD 为负值**（价格下跌）；``sum(KRD)`` 与
    :func:`spot_dv01` 同号。若分析空头，请自行取负。

    算法（memo §3.3 陷阱 3 的正确做法，**不是**「总 DV01 × 期限占比」）
    ------------------------------------------------------------------
    1. 用 ``spot.discount(t)`` 贴现本券全部现金流得基准全价 ``PV0``（现券多头）；
    2. 对第 k 个关键期限点把该点零息利率 **+1bp**，其余关键点不动；
       关键点之间零息利率**线性插值**，首尾**平推（flat）**（区间外权重落在
       最近端点）。等价于重估折算因子
       ``D_k(t) = D(t)·exp(-1e-4·w_k(t)·t)``，其中 ``w_k(t)`` 是插值权重；
    3. 用重估后的曲线重新贴现全部现金流，``KRD_k = PV_k − PV0``；
    4. 遍历全部关键期限返回 ``dict[tenor, krd]``。

    KRD 内部**固定 1bp 步长**，没有 ``bp`` 参数（不做外部步长差分）。

    一致性
    ------
    - ``sum(KRD) ≈ spot_dv01``（所有关键点同时 +1bp = 曲线整体平行 +1bp，
      因为线性插值/平推权重每列之和恒为 1）。注意 KRD 是逐点非线性重估、
      DV01_spot 是平行重估，两者差一个 O(bump·t) 的二阶项（10Y 约 2e-5 相对，
      30Y 仍 <1e-3），测试按相对误差 <1e-3 验收；
    - ``sum(KRD)`` 与 YTM 口径 :func:`dv01` 同量级：对 10Y 国债相对差 < 2%。
      差异来源：① YTM 口径把整条曲线压成单一到期收益率并假设它平行移动；
      ② 债券年复利 ``(1+y/f)^-i`` vs 即期连续复利 ``exp(-z·t)``（二者是非线性
      映射）；③ 曲线形状/息票效应（各期现金流对应不同零息利率、KRD 分布在
      整条曲线上，而 YTM DV01 用单一修正久期概括）。

    日期契约（R17）
    ----------------
    结算日是现金流/贴现的**起点**，与曲线观察日/结算日必须一致：

    - ``settlement`` 必须是**工作日**（周末不可结算）；
    - ``settlement >= 曲线参考日``（``spot.observation_date``，缺失回退 ``as_of``）；
    - 若 ``spot`` 暴露 ``settlement_date``，要求 ``settlement == spot.settlement_date``，
      否则抛 :class:`ValueError`；``allow_mismatch=True`` 时降级为 warning（过渡期用）。
    - 曲线对象若缺少 ``settlement_date``（当前 ``SpotCurve`` 正在补充），本函数回退
      用 ``observation_date``/``as_of`` 校验并 **warning**，不静默接受错位日期。

    参数
    ----
    bond : BondStatic
    settlement : date
    spot : Any
        鸭子类型即期曲线：``times``（严格递增，年）、``zero_rate(t)``（连续复利，
        百分数）、``discount(t) = exp(-zero_rate(t)/100·t)``；可选
        ``observation_date`` / ``settlement_date`` / ``as_of`` 用于日期契约。
    key_tenors : Sequence[float] | None
        关键期限（年）；``None`` 时读 ``cfg.risk.key_tenors_krd``。
    allow_mismatch : bool
        结算日与 ``spot.settlement_date`` 不一致时是否放行（只 warning 不报错）。
    cfg : Config | None

    返回
    ----
    dict[float, float]
        关键期限 → KRD（元/报价面值/bp，多头为负）。

    时间口径（必须单列，不是官方即期对账）
    --------------------------------------
    现金流时间按 ``(现金流日 − 结算日).days / 365`` 映射到曲线 ``times``
    （ACT/365 名义年限）；曲线 ``times`` 同样是名义年限。这是**内部一致的
    ACT/365 名义期限近似**，不是中债官方“即期对账”口径，跨闰年会有
    ≤1 天的名义期限误差；报告与对账时不得称其为官方即期估值。
    """
    validate_settlement(bond, settlement)
    _validate_spot(spot)
    _validate_krd_date_contract(settlement, spot, allow_mismatch=allow_mismatch)
    keys = _resolve_key_tenors(key_tenors, cfg)
    times, amounts, disc0 = _spot_cashflows(bond, settlement, spot, cfg=cfg)
    pv0 = float(np.dot(amounts, disc0))
    weights = _key_rate_weights(keys, times)

    result: dict[float, float] = {}
    for index, tenor in enumerate(keys):
        bumped = disc0 * np.exp(-_BP_DECIMAL * weights[index] * times)
        result[float(tenor)] = float(np.dot(amounts, bumped)) - pv0
    return result
