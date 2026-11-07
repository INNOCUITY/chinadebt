"""计息与日历惯例（中债口径）。

本模块锁定定价层的**计息基准**，是 PnL / 久期 / DV01 的地基：

- 银行间（IB）债券：实际/实际（``ACT/ACT``），闰年 2/29 计息，算头不算尾；
- 交易所（EX）债券：实际/365（``ACT/365``），2/29 不计息；
- 应计利息三分支：规则附息 / 利随本清 / 贴现（见 :func:`accrued_interest`）。

金额单位
--------
除特别说明外，本模块所有金额一律为「**元/报价面值**」：报价面值取
``config.pricing.face_value``（默认 100，即「元/百元面值」，与
``src/ficc/data/schema.py`` 的价格契约一致）。``coupon_pct`` 是百分数
（``1.72`` 表示 1.72%），故每百元面值的**年票息恰为** ``coupon_pct`` 元
（1.72 元），付息周期票息为 ``coupon_pct / freq`` 元。

配置口径
--------
本模块不硬编码金融口径默认值：计息基准、报价面值、T+N 滞后都从
``config.pricing`` 读取；需要自定义配置时把 ``cfg`` 显式传给各函数
（``cfg=None`` 时才读全局配置，**不会回落到硬编码默认值**）。

参考
----
``docs/00_market_conventions.md`` §2.2（应计利息三分支与日计数沿革）、
§2.3（memo 自算算例 A：26 附息国债 10）、§2.5（memo 自算算例 C：1Y 利随本清）。
"""

from __future__ import annotations

import calendar
from dataclasses import dataclass
from datetime import date
from typing import Literal

from ficc.config import Config, PricingConfig, get_config
from ficc.data.schema import BondStatic
from ficc.utils.calendar import settlement_date as _calendar_settlement_date

__all__ = [
    "Basis",
    "ConventionError",
    "CouponPeriod",
    "accrual_basis",
    "accrual_days",
    "accrued_interest",
    "annual_anniversaries",
    "coupon_dates",
    "coupon_period",
    "day_count",
    "days_in_year",
    "is_leap_year",
    "is_on_coupon_date",
    "next_coupon_date",
    "pricing_config",
    "quote_face_value",
    "settlement_date",
    "validate_settlement",
    "year_fraction",
]

# 日计数基准：银行间 ACT/ACT（含 2/29）、交易所 ACT/365（不含 2/29）。
Basis = Literal["ACT/ACT", "ACT/365"]


class ConventionError(ValueError):
    """计息/日历参数不合法（日期越界、未知基准等）时抛出。"""


def pricing_config(cfg: Config | None = None) -> PricingConfig:
    """解析定价配置：显式传入的 ``cfg`` 优先，否则读取全局配置。

    刻意**不**提供硬编码回退值——自定义配置必须显式传入，被调用的底层函数
    不会悄悄回落到默认口径。
    """
    return (cfg if cfg is not None else get_config()).pricing


def quote_face_value(*, cfg: Config | None = None) -> float:
    """报价面值（元），取自 ``config.pricing.face_value``（默认 100）。

    所有价格/金额/应计利息都以该面值为基准；默认值 100 即「元/百元面值」。
    """
    face = float(pricing_config(cfg).face_value)
    if face <= 0.0:
        raise ConventionError(f"配置的报价面值 face_value={face} 必须为正数")
    return face


# ---------------------------------------------------------------------------
# 闰年与日历基础
# ---------------------------------------------------------------------------
def is_leap_year(year: int) -> bool:
    """该年是否为闰年（2/29 是否存在）。"""
    return calendar.isleap(year)


def days_in_year(year: int) -> int:
    """该年的实际天数（365 或 366）。"""
    return 366 if calendar.isleap(year) else 365


def _is_month_end(d: date) -> bool:
    """是否为当月最后一天（用于月末付息日锚定）。"""
    return d.day == calendar.monthrange(d.year, d.month)[1]


def _add_months(anchor: date, months: int, *, end_of_month: bool) -> date:
    """从锚点日加 ``months`` 个月。

    若 ``end_of_month`` 为真（锚点在月末），结果取目标月最后一天；
    否则取同号日，目标月没有该日时钳制到月末（如 1/31 → 2/28）。
    """
    year = anchor.year + (anchor.month - 1 + months) // 12
    month = (anchor.month - 1 + months) % 12 + 1
    last_day = calendar.monthrange(year, month)[1]
    day = last_day if end_of_month else min(anchor.day, last_day)
    return date(year, month, day)


def _add_years(anchor: date, years: int, *, end_of_month: bool) -> date:
    """从锚点日加整数年（处理 2/29 与月末锚定）。"""
    if years <= 0:
        raise ConventionError(f"years 必须为正整数，实际 {years}")
    last_day = calendar.monthrange(anchor.year + years, anchor.month)[1]
    day = last_day if end_of_month else min(anchor.day, last_day)
    return date(anchor.year + years, anchor.month, day)


def day_count(start: date, end: date) -> int:
    """实际日历天数 ``end - start``（含 2/29，算头不算尾）。"""
    return (end - start).days


def _leap_days_between(start: date, end: date) -> int:
    """``[start, end)`` 区间内 2/29 的个数（算头不算尾）。"""
    if end <= start:
        return 0
    count = 0
    for year in range(start.year, end.year + 1):
        if calendar.isleap(year):
            feb29 = date(year, 2, 29)
            if start <= feb29 < end:
                count += 1
    return count


def accrual_days(start: date, end: date, basis: Basis = "ACT/ACT") -> int:
    """**计息用**实际天数（算头不算尾）。

    - ``ACT/ACT``：实际日历天数，含 2/29（银行间）；
    - ``ACT/365``：剔除 2/29 后的实际天数（交易所，2/29 不计息）。

    ``basis`` 的默认值 ``ACT/ACT`` 只是纯函数的便捷默认；债券定价路径一律经
    :func:`accrual_basis` 从 ``config.pricing`` 显式取基准，不会依赖此默认值。
    """
    if end < start:
        raise ConventionError(f"结束日 {end} 不能早于起始日 {start}")
    raw = (end - start).days
    if basis == "ACT/ACT":
        return raw
    if basis == "ACT/365":
        return raw - _leap_days_between(start, end)
    raise ConventionError(f"不支持的日计数基准: {basis!r}（可选 'ACT/ACT' / 'ACT/365'）")


def year_fraction(start: date, end: date, basis: Basis = "ACT/ACT") -> float:
    """起止日之间的年化期限（单位：年）。

    - ``ACT/ACT``：按自然年分段求和，每段 = 该段实际天数 / 该年实际天数
      （闰年 2/29 计息，跨年时各年分母不同）；
    - ``ACT/365``：累计实际天数（剔除 2/29） / 365。

    ``basis`` 的默认值 ``ACT/ACT`` 只是纯函数便捷值（见 :func:`accrual_days`）；
    债券定价路径的基准由 :func:`accrual_basis` 从 ``config.pricing`` 决定。

    Examples
    --------
    >>> year_fraction(date(2024, 1, 1), date(2024, 3, 1), "ACT/ACT")
    0.16393442622950818          # 60 / 366
    >>> year_fraction(date(2024, 1, 1), date(2024, 3, 1), "ACT/365")
    0.16164383561643836          # 59 / 365（2/29 不计息）
    """
    if end < start:
        raise ConventionError(f"结束日 {end} 不能早于起始日 {start}")
    if end == start:
        return 0.0
    if basis == "ACT/365":
        return accrual_days(start, end, basis) / 365.0
    if basis != "ACT/ACT":
        raise ConventionError(f"不支持的日计数基准: {basis!r}（可选 'ACT/ACT' / 'ACT/365'）")
    total = 0.0
    cursor = start
    while cursor < end:
        next_year = date(cursor.year + 1, 1, 1)
        segment_end = min(next_year, end)
        total += (segment_end - cursor).days / days_in_year(cursor.year)
        cursor = segment_end
    return total


def accrual_basis(bond: BondStatic, *, cfg: Config | None = None) -> Basis:
    """按托管场所返回计息基准：银行间 ACT/ACT、交易所 ACT/365。

    基准值来自 ``config.pricing.day_count`` / ``day_count_exchange``（不硬编码）；
    ``cfg`` 显式传入时优先使用，不回落默认值。
    """
    pricing = pricing_config(cfg)
    raw = pricing.day_count if bond.market == "IB" else pricing.day_count_exchange
    if raw == "ACT/ACT":
        return "ACT/ACT"
    if raw == "ACT/365":
        return "ACT/365"
    raise ConventionError(
        f"配置的日计数基准 {raw!r} 不受支持（bond.market={bond.market!r}）；"
        "可选 'ACT/ACT' / 'ACT/365'"
    )


# ---------------------------------------------------------------------------
# 结算日校验与推断
# ---------------------------------------------------------------------------
def validate_settlement(
    bond: BondStatic, settlement: date, *, allow_maturity: bool = False
) -> None:
    """校验结算日。

    定价（现金流/贴现）要求结算日落在 ``[起息日, 到期日)``；应计利息允许等于
    到期日（``allow_maturity=True``，到期日仍有最后一期票息/本息未兑付）。
    """
    if settlement < bond.issue_date:
        raise ConventionError(f"{bond.code}: 结算日 {settlement} 早于起息日 {bond.issue_date}")
    if settlement > bond.maturity_date:
        raise ConventionError(f"{bond.code}: 结算日 {settlement} 晚于到期日 {bond.maturity_date}")
    if settlement == bond.maturity_date and not allow_maturity:
        raise ConventionError(
            f"{bond.code}: 结算日 {settlement} 等于到期日，无未来现金流可定价"
            "（应计利息场景请传 allow_maturity=True）"
        )


def settlement_date(
    trade_date: date, lag_days: int | None = None, *, cfg: Config | None = None
) -> date:
    """按 T+``lag_days`` 推算现券结算日（**唯一的日历实现在** :mod:`ficc.utils.calendar`）。

    规则
    ----
    1. 若成交日落在周六/周日，先顺延到下一个工作日（结算不可能发生在周末）；
    2. 再从顺延后的日期起，**逐个工作日**推进 ``lag_days`` 个工作日
       （例如周五 T+1 → 下周一，周五 T+2 → 下周二，周五 T+3 → 下周三）；
    3. ``lag_days=None`` 时取 ``config.pricing.settlement_lag_days``（默认 T+1），
       自定义配置请显式传 ``cfg``。

    ``pricing`` 层不重复实现日历：本函数只解析配置里的默认滞后，然后委托给
    :func:`ficc.utils.calendar.settlement_date`（R17：bootstrap / KRD / 结算
    三处必须共用同一套工作日规则）。

    TODO: 尚未接入银行间节假日表（``config.data.trading_calendar = "CN_IB"``），
    法定节假日目前会被当作工作日；接入点在 ``ficc.utils.calendar``。
    """
    effective_lag = pricing_config(cfg).settlement_lag_days if lag_days is None else lag_days
    if effective_lag < 0:
        raise ConventionError(f"lag_days 不能为负，实际 {effective_lag}")
    return _calendar_settlement_date(trade_date, effective_lag)


# ---------------------------------------------------------------------------
# 付息日序列
# ---------------------------------------------------------------------------
def coupon_dates(bond: BondStatic) -> list[date]:
    """完整付息日序列（升序，含起息日与到期日）。

    - 规则附息：按 ``bond.freq``（1=年付、2=半年付）生成；起息日落在月末时，
      后续付息日统一锚定到对应月份最后一天（处理月末/闰年）；
    - 利随本清 / 贴现（零息）：只有起息日与到期日两个节点
      （唯一一次现金流发生在到期日）。

    Examples
    --------
    >>> coupon_dates(bond_10y_semi)  # doctest: +SKIP
    [date(2026, 5, 15), date(2026, 11, 15), ..., date(2036, 5, 15)]  # 21 个，20 次付息
    """
    issue = bond.issue_date
    maturity = bond.maturity_date
    if bond.accrual != "coupon":
        return [issue, maturity]

    freq = int(bond.freq)
    if freq not in (1, 2):
        raise ConventionError(f"{bond.code}: 不支持的付息频率 freq={freq}（仅支持 1/2）")
    step_months = 12 // freq
    end_of_month = _is_month_end(issue)

    dates: list[date] = [issue]
    index = 1
    while True:
        nxt = _add_months(issue, step_months * index, end_of_month=end_of_month)
        if nxt >= maturity:
            break
        dates.append(nxt)
        index += 1
    if dates[-1] != maturity:
        dates.append(maturity)
    return dates


def annual_anniversaries(bond: BondStatic) -> list[date]:
    """起息日起的**周年序列**（含起息日与到期日，升序）。

    利随本清的 ``K``（整年数）与 ``TY``（当前计息年度实际天数）按此序列计算；
    规则附息券的价格计算不使用该序列（用 :func:`coupon_dates`）。
    """
    issue = bond.issue_date
    maturity = bond.maturity_date
    end_of_month = _is_month_end(issue)
    dates: list[date] = [issue]
    index = 1
    while True:
        nxt = _add_years(issue, index, end_of_month=end_of_month)
        if nxt >= maturity:
            break
        dates.append(nxt)
        index += 1
    if dates[-1] != maturity:
        dates.append(maturity)
    return dates


@dataclass(frozen=True)
class CouponPeriod:
    """结算日所在的计息周期。

    Attributes
    ----------
    prev_coupon : date
        上一付息日（含；结算日恰为付息日时即该日，此时 ``t_days = 0``）。
    next_coupon : date
        下一付息日（严格晚于结算日）。
    t_days : int
        上一付息日 → 结算日的**计息**实际天数（算头不算尾）。
    ts_days : int
        上一付息日 → 下一付息日的实际天数，即整个计息周期 TS。
    """

    prev_coupon: date
    next_coupon: date
    t_days: int
    ts_days: int

    @property
    def t_remaining_days(self) -> int:
        """结算日 → **下一个**付息日的天数 T（不是距上一付息日）。"""
        return self.ts_days - self.t_days

    @property
    def w(self) -> float:
        """折现指数 ``w = T / TS``，T 为结算日到**下一个**付息日的天数。"""
        if self.ts_days <= 0:
            raise ConventionError(
                f"计息周期天数非正: prev={self.prev_coupon}, next={self.next_coupon}"
            )
        return self.t_remaining_days / self.ts_days


def coupon_period(bond: BondStatic, settlement: date, *, cfg: Config | None = None) -> CouponPeriod:
    """结算日所在计息周期 ``[上一付息日, 下一付息日)``。

    三分支口径：

    - 规则附息：周期由 :func:`coupon_dates` 决定；
    - 利随本清：周期为**当前计息年度**（周年区间）；
    - 贴现/零息：整个存续期视为一个周期（起息 → 到期）。

    ``cfg`` 显式传入时用于计息基准（ACT/ACT 或 ACT/365），不回落默认值。
    """
    validate_settlement(bond, settlement)
    basis = accrual_basis(bond, cfg=cfg)

    if bond.accrual == "coupon":
        schedule = coupon_dates(bond)
        prev_coupon = max(d for d in schedule if d <= settlement)
        next_coupon = min(d for d in schedule if d > settlement)
    elif bond.accrual == "lump_sum":
        anniversaries = annual_anniversaries(bond)
        prev_coupon = max(d for d in anniversaries if d <= settlement)
        next_coupon = min(d for d in anniversaries if d > settlement)
    else:  # discount / 零息
        prev_coupon = bond.issue_date
        next_coupon = bond.maturity_date

    return CouponPeriod(
        prev_coupon=prev_coupon,
        next_coupon=next_coupon,
        t_days=accrual_days(prev_coupon, settlement, basis),
        ts_days=accrual_days(prev_coupon, next_coupon, basis),
    )


def next_coupon_date(bond: BondStatic, settlement: date) -> date:
    """结算日**之后**的下一个付息日（严格晚于结算日）。

    利随本清与贴现/零息券的唯一现金流在到期日，故返回到期日。
    """
    validate_settlement(bond, settlement)
    if bond.accrual != "coupon":
        return bond.maturity_date
    return min(d for d in coupon_dates(bond) if d > settlement)


def is_on_coupon_date(bond: BondStatic, settlement: date) -> bool:
    """结算日是否为付息/兑付日（起息日不算付息日）。"""
    return settlement != bond.issue_date and settlement in coupon_dates(bond)


# ---------------------------------------------------------------------------
# 应计利息（三分支）
# ---------------------------------------------------------------------------
def accrued_interest(
    bond: BondStatic,
    settlement: date,
    *,
    issue_price: float | None = None,
    cfg: Config | None = None,
) -> float:
    """应计利息（单位：**元/报价面值**，默认元/百元面值），三分支口径。

    ============ ===========================================================
    分支         公式
    ============ ===========================================================
    ``coupon``    ``AI = C / f × t / TS``：t = 上一付息日 → 结算日实际天数
                  （算头不算尾），TS = 当前付息周期实际天数；付息日当天 AI = 0。
    ``lump_sum``  ``AI = K × C + C × t / TY``：K = 起息日起整年数，
                  TY = 当前计息年度实际天数（闰年 366）。
    ``discount``  ``AI = (F − Pd) × t / T``：Pd = 发行价，t = 起息 → 结算天数，
                  T = 起息 → 到期天数，F = 报价面值（默认 100）。
    ============ ===========================================================

    Parameters
    ----------
    bond : BondStatic
        债券静态条款。
    settlement : date
        结算日，允许等于到期日（此时 AI 为尚未兑付的最后一期票息/本息）。
    issue_price : float | None
        贴现/零息债的**发行价**（元/报价面值）。``BondStatic`` 不含该字段，
        因此该分支必须显式传入（如 99.0）。
    cfg : Config | None
        显式配置；``None`` 时读取全局配置（不回落硬编码默认值）。

    Returns
    -------
    float
        应计利息，元/报价面值（默认元/百元面值）。

    Notes
    -----
    **贴现分支与文档字面的差异**：``docs/00_market_conventions.md`` §2.2 与
    ``config.pricing.accrual_formula.discount`` 现写作 ``100 − Pd/T×t``，
    这是**缺括号的笔误**：按字面代入会在起息日得到 ``AI ≈ 100``，与「应计利息
    从 0 单调累积到发行折价」矛盾。正确形式为 ``(100 − Pd) × t / T``——
    起息日 AI = 0，到期日 AI = 总折价 ``100 − Pd``，方向已验证正确
    （评审 C-007 / 证据边界条目）。文档由 Lead 统一修订，本函数实现不变。
    """
    validate_settlement(bond, settlement, allow_maturity=True)
    basis = accrual_basis(bond, cfg=cfg)
    face = quote_face_value(cfg=cfg)
    annual_coupon = bond.coupon_pct * face / 100.0  # 元/面值/年

    if bond.accrual == "coupon":
        freq = int(bond.freq)
        if settlement == bond.maturity_date:
            # 到期日仍有最后一期票息未兑付（算头不算尾，整期计息）。
            return annual_coupon / freq
        period = coupon_period(bond, settlement, cfg=cfg)
        return annual_coupon / freq * period.t_days / period.ts_days

    if bond.accrual == "lump_sum":
        anniversaries = annual_anniversaries(bond)
        complete_years = max(i for i, d in enumerate(anniversaries) if d <= settlement)
        result = complete_years * annual_coupon
        if complete_years + 1 < len(anniversaries):
            t_days = accrual_days(anniversaries[complete_years], settlement, basis)
            ty_days = accrual_days(
                anniversaries[complete_years], anniversaries[complete_years + 1], basis
            )
            result += annual_coupon * t_days / ty_days
        return result

    # discount / 零息
    if issue_price is None:
        raise ConventionError(
            f"{bond.code}: 贴现/零息债必须显式传入发行价 issue_price（元/报价面值）；"
            "BondStatic 不包含该字段"
        )
    if not 0.0 < issue_price <= face:
        raise ConventionError(
            f"{bond.code}: 发行价 issue_price={issue_price} 超出 (0, {face}] 范围"
        )
    total_days = day_count(bond.issue_date, bond.maturity_date)
    if total_days <= 0:
        raise ConventionError(f"{bond.code}: 起息日至到期日天数非正")
    elapsed_days = day_count(bond.issue_date, settlement)
    return (face - issue_price) * elapsed_days / total_days
