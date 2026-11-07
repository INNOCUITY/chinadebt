"""定价层：计息惯例、现金流贴现、久期/凸性/DV01。

对外 API
--------
- 计息与日历（:mod:`ficc.pricing.conventions`）：
  :func:`year_fraction` / :func:`coupon_dates` / :func:`accrued_interest` /
  :func:`next_coupon_date` / :func:`is_on_coupon_date` / :func:`settlement_date`
- 现金流与价格（:mod:`ficc.pricing.bond`）：
  :func:`cashflows` / :func:`dirty_price_from_ytm` / :func:`clean_price_from_ytm` /
  :func:`ytm_from_dirty_price` / :func:`ytm_from_clean_price`
- 风险指标（:mod:`ficc.pricing.analytics`）：
  :func:`macaulay_duration` / :func:`mod_duration` / :func:`convexity` /
  :func:`dv01` / :func:`dv01_finite_diff` / :func:`spot_dv01` /
  :func:`key_rate_durations`（即期曲线 KRD，鸭子类型接口）
- carry / roll-down 分解（:mod:`ficc.pricing.carry`，只做分解、不做信号）：
  :func:`carry_roll_down` / :func:`decompose_realized` / :func:`par_modified_duration` /
  :func:`resolve_funding_rate_pct`

约定：收益率一律为**百分数**；金额一律为**元/报价面值**（默认元/百元面值，
面值取 ``config.pricing.face_value``）；报价用净价，结算与 PnL 用全价。
口径来源见 ``docs/00_market_conventions.md``。

配置口径：各函数都接受关键字参数 ``cfg``（``Config | None``）；显式传入时
优先使用、不回落硬编码默认值，``None`` 时才读全局配置。
"""

from __future__ import annotations

from ficc.pricing.analytics import (
    convexity,
    dv01,
    dv01_finite_diff,
    key_rate_durations,
    macaulay_duration,
    mod_duration,
    spot_dv01,
)
from ficc.pricing.bond import (
    CF_COLUMNS,
    cashflows,
    clean_price_from_ytm,
    dirty_price_from_ytm,
    ytm_from_clean_price,
    ytm_from_dirty_price,
)
from ficc.pricing.carry import (
    FACTOR_TENORS,
    FundingRateUnavailableError,
    carry_roll_down,
    decompose_realized,
    factor_basis_value,
    ns_scenario_projection,
    par_modified_duration,
    resolve_funding_rate_pct,
    roll_down_aging,
)
from ficc.pricing.conventions import (
    Basis,
    ConventionError,
    CouponPeriod,
    accrual_basis,
    accrual_days,
    accrued_interest,
    annual_anniversaries,
    coupon_dates,
    coupon_period,
    day_count,
    days_in_year,
    is_leap_year,
    is_on_coupon_date,
    next_coupon_date,
    pricing_config,
    quote_face_value,
    settlement_date,
    validate_settlement,
    year_fraction,
)

__all__ = [
    "CF_COLUMNS",
    "FACTOR_TENORS",
    "Basis",
    "ConventionError",
    "CouponPeriod",
    "FundingRateUnavailableError",
    "accrual_basis",
    "accrual_days",
    "accrued_interest",
    "annual_anniversaries",
    "carry_roll_down",
    "cashflows",
    "clean_price_from_ytm",
    "convexity",
    "coupon_dates",
    "coupon_period",
    "day_count",
    "days_in_year",
    "decompose_realized",
    "dirty_price_from_ytm",
    "dv01",
    "dv01_finite_diff",
    "factor_basis_value",
    "is_leap_year",
    "is_on_coupon_date",
    "key_rate_durations",
    "macaulay_duration",
    "mod_duration",
    "next_coupon_date",
    "ns_scenario_projection",
    "par_modified_duration",
    "pricing_config",
    "quote_face_value",
    "resolve_funding_rate_pct",
    "roll_down_aging",
    "settlement_date",
    "spot_dv01",
    "validate_settlement",
    "year_fraction",
    "ytm_from_clean_price",
    "ytm_from_dirty_price",
]
