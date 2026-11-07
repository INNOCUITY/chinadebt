"""组合层的日期契约（R27）：观察日 / 结算日 / 存量估值日 / 剩余期限的唯一入口。

为什么需要单独一层
------------------
曲线层、账本层、组合层对"日期"的用途不同：

- **观察日（observation date）**：曲线估值日 = ``Curve.as_of``；
- **交易结算日（settlement date）**：成交日 + T+N 个**真实交易日**（CN_IB 日历）；
- **存量估值日**：组合持仓按哪个结算日折现——统一用"观察日的 T+N"；
- **剩余期限**：``(到期日 − 结算日) / 365``（ACT/365，与定价层一致）。

R17 只修了曲线层的默认 settlement，R27 指出组合层（benchmark/construction/
constraints/engine）仍各自调用默认 bootstrap，导致 2019-09-30 的 spot 参考结算日
是 10-01 而账本是 10-08。本模块把口径集中到一处，所有组合层调用都必须显式传
``calendar``。

约定
----
- 生产路径必须传 ``TradingCalendar``；``calendar=None`` 时退回简单工作日规则
  （``add_business_days``），仅用于单测/兼容，会记录 warning。
"""

from __future__ import annotations

from datetime import date, datetime

from ficc.config import Config, get_config
from ficc.logging import get_logger
from ficc.pricing.conventions import day_count
from ficc.utils.calendar import add_business_days

__all__ = [
    "remaining_years",
    "settlement_for",
    "trade_settlement",
]

log = get_logger("portfolio.dates")


def _lag(cfg: Config | None) -> int:
    resolved = get_config() if cfg is None else cfg
    return int(resolved.pricing.settlement_lag_days)


def settlement_for(
    calendar: object | None, observation: date, *, cfg: Config | None = None
) -> date:
    """观察日 → 存量估值/现金流贴现起点（T+N 个真实交易日）。"""
    if isinstance(observation, datetime):
        raise TypeError(
            f"observation 必须是 datetime.date（非 datetime），实际 {type(observation).__name__}"
        )
    if not isinstance(observation, date):
        raise TypeError(f"observation 必须是 datetime.date，实际 {type(observation).__name__}")
    lag = _lag(cfg)
    if calendar is None:
        log.warning("组合层未传 CN_IB 日历，退回简单工作日规则（仅测试/兼容路径）")
        return add_business_days(observation, lag)
    if not callable(getattr(calendar, "settlement_date", None)):
        raise TypeError(
            "calendar 必须提供 settlement_date(observation, lag)（TradingCalendar 接口）"
        )
    settlement = calendar.settlement_date(observation, lag)  # type: ignore[attr-defined]
    if not isinstance(settlement, date):
        raise TypeError(f"calendar.settlement_date 返回类型非法: {type(settlement).__name__}")
    return settlement


def trade_settlement(
    calendar: object | None, trade_date: date, *, cfg: Config | None = None
) -> date:
    """成交日 → 结算日（与账本 ``Ledger`` 使用同一份日历与 lag）。"""
    return settlement_for(calendar, trade_date, cfg=cfg)


def remaining_years(bond: object, settlement: date) -> float:
    """剩余年限 = (到期日 − 结算日)/365（ACT/365，算头不算尾）。"""
    maturity = bond.maturity_date  # type: ignore[attr-defined]
    return day_count(settlement, maturity) / 365.0
