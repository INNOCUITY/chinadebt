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

KRD（关键期限久期）**未实现**：正确算法依赖即期曲线 bootstrap 与曲线插值，
属于 ``ficc.curve`` 模块，见 :func:`key_rate_durations` 的说明。
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import date

from ficc.config import Config
from ficc.data.schema import BondStatic
from ficc.pricing.bond import dirty_price_from_ytm
from ficc.pricing.conventions import (
    coupon_dates,
    coupon_period,
    day_count,
    quote_face_value,
    validate_settlement,
)

__all__ = [
    "convexity",
    "dv01",
    "dv01_finite_diff",
    "key_rate_durations",
    "macaulay_duration",
    "mod_duration",
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
# KRD（可选，未实现）
# ---------------------------------------------------------------------------
def key_rate_durations(
    bond: BondStatic,
    settlement: date,
    curve_tenors: Sequence[float],
    curve_ytm_pct: Sequence[float],
    *,
    key_tenors: Sequence[float] | None = None,
    cfg: Config | None = None,
) -> dict[float, float]:
    """关键期限久期（KRD）：单点 +1bp、其余点插值不变，重估债券的 DV01 向量。

    返回 ``dict[tenor, dv01]``（元/报价面值/bp）；各 KRD 之和应 ≈ 总 DV01。

    未实现（明确留白，不糊弄）
    --------------------------
    正确做法（memo §3.3 陷阱 3）需要跨模块协作：

    1. 用 ``ficc.curve`` 把 YTM 曲线 bootstrap 成**即期（零息）曲线**；
    2. 对某个关键期限点 +1bp，其余关键点不动，按曲线插值规则重算即期曲线；
    3. 用重估后的即期曲线贴现本券全部现金流，得到该点的 DV01 贡献；
    4. 遍历全部关键期限，得到 KRD 向量。

    ``ficc.pricing`` 层只有单券 YTM，没有曲线对象，无法在不引入虚假假设的前提下
    计算 KRD。为拒绝「总 DV01 × 期限占比」这类糊弄式近似（memo 明确禁止），
    这里显式抛 :class:`NotImplementedError`。

    TODO: ``ficc.curve`` 提供 ``bootstrap_spot()`` + ``shift_key_tenor()`` 后，
    在本函数中实现上述 1–4 步，并用「各 KRD 之和 ≈ 总 DV01」做回归测试。
    """
    raise NotImplementedError(
        "key_rate_durations 需要即期曲线 bootstrap（ficc.curve 模块），当前定价层不实现；"
        "见本函数 docstring 的 TODO 与 docs/00_market_conventions.md §3.3"
    )
