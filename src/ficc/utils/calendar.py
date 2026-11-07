"""交易日历与结算日推算（共享工具）。

**为什么单独放这里**：bootstrap（曲线层）与 KRD/结算（定价层）都必须用**同一套**
工作日规则，否则会出现"曲线参考日 2024-06-28、结算日 2024-06-29（周六）"这类
自相矛盾的契约（评审 R17）。

约定
----
- **观察日（observation date）**：曲线估值日，即 `Curve.as_of`。
- **结算日（settlement date）**：现金流/贴现的起点，= 观察日 + T+N 个**工作日**。
- 当前只跳过周六周日，法定节假日尚未接入（`config.data.trading_calendar = "CN_IB"`）。

TODO: 接入 CN_IB 银行间节假日表；接入后本模块是唯一修改点。
"""

from __future__ import annotations

from datetime import date, timedelta

__all__ = ["add_business_days", "is_business_day", "next_business_day", "settlement_date"]


def is_business_day(d: date) -> bool:
    """是否为工作日（当前：周一至周五；节假日暂未排除）。"""
    return d.weekday() < 5


def next_business_day(d: date) -> date:
    """返回 ``d`` 当天或之后最近的工作日。"""
    cur = d
    while not is_business_day(cur):
        cur += timedelta(days=1)
    return cur


def add_business_days(d: date, n: int) -> date:
    """从 ``d`` 起加 ``n`` 个工作日。

    规则：**先顺延到下一工作日，再逐个计数**。即周六的 T+1 是下周一（不是周日）。
    ``n=0`` 时返回 ``d`` 顺延后的工作日。
    """
    if n < 0:
        raise ValueError(f"n 不能为负，实际 {n}")
    cur = next_business_day(d)
    remaining = n
    while remaining > 0:
        cur += timedelta(days=1)
        if is_business_day(cur):
            remaining -= 1
    return cur


def settlement_date(trade_date: date, lag_days: int = 1) -> date:
    """现券结算日 = 交易日 + ``lag_days`` 个**工作日**（T+0/T+1/T+2…）。"""
    return add_business_days(trade_date, lag_days)
