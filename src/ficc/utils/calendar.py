"""交易日历与结算日推算（共享工具）。

**为什么单独放这里**：bootstrap（曲线层）、KRD（定价层）、账本（portfolio）都必须用
**同一套**日历规则，否则会出现"曲线参考日 2024-06-28、结算日 2024-06-29（周六）"这类
自相矛盾的契约（评审 R17）。

本模块提供两层能力
------------------
1. **工作日规则**（`add_business_days` / `settlement_date`）：只跳过周六周日，
   用于没有历史数据的场合；简单、无外部依赖。
2. **真实 CN_IB 交易日历**（`TradingCalendar`）：由**实测中债估值日**构造，
   自动覆盖法定节假日与调休（例如 2024-10-01 不是交易日、2024-09-30 的 T+1 是 2024-10-08）。
   账本/回测等需要"真实交易日"的场景一律用这一层。

约定
----
- **观察日（observation date）**：曲线估值日，即 `Curve.as_of`。
- **结算日（settlement date）**：现金流/贴现起点 = 观察日 + T+N 个交易日。
- 日历覆盖范围只到数据最新日；区间外抛异常，不用猜测日期。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from datetime import date, datetime, timedelta

import pandas as pd

__all__ = [
    "TradingCalendar",
    "add_business_days",
    "is_business_day",
    "next_business_day",
    "settlement_date",
]


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


# ---------------------------------------------------------------------------
# 交易日历（由观测到的真实交易日构造）
# ---------------------------------------------------------------------------
class TradingCalendar:
    """银行间（CN_IB）交易日历：由**实际观测到的估值日**构造。

    为什么不用法定节假日表：中债每个**银行间工作日**发布估值曲线，因此
    `data/processed/curve_long.parquet` 的日期集合就是一段"实测的 CN_IB 交易日"。
    用它做日历，节假日、调休、临时休市都被真实数据覆盖，比手工维护节假日表更可靠。

    覆盖范围仅限观测区间（2002-01-04 → 数据最新日）；区间外抛异常而不是猜。

    Examples
    --------
    >>> cal = TradingCalendar.from_dates(panel.index)
    >>> cal.is_trading_day(date(2024, 6, 28))
    True
    >>> cal.settlement_date(date(2024, 6, 28), lag_days=1)
    datetime.date(2024, 7, 1)
    """

    def __init__(self, trading_days: Sequence[date]) -> None:
        uniq = sorted({d if isinstance(d, date) else d.date() for d in trading_days})
        if not uniq:
            raise ValueError("交易日历为空")
        self._days: tuple[date, ...] = tuple(uniq)
        self._set: frozenset[date] = frozenset(uniq)
        self._index: dict[date, int] = {d: i for i, d in enumerate(uniq)}

    # ---- 构造 ----------------------------------------------------------
    @classmethod
    def from_dates(cls, dates: Iterable[date | datetime | pd.Timestamp]) -> TradingCalendar:
        """从日期序列构造（接受 DatetimeIndex）。"""
        return cls([d.date() if hasattr(d, "date") else d for d in dates])

    # ---- 属性 ----------------------------------------------------------
    @property
    def first_day(self) -> date:
        return self._days[0]

    @property
    def last_day(self) -> date:
        return self._days[-1]

    def __len__(self) -> int:
        return len(self._days)

    def __contains__(self, d: object) -> bool:
        return isinstance(d, date) and d in self._set

    def dates(self) -> tuple[date, ...]:
        return self._days

    # ---- 判定与滚动 ----------------------------------------------------
    def is_trading_day(self, d: date) -> bool:
        return d in self._set

    def _check_range(self, d: date) -> None:
        if not (self.first_day <= d <= self.last_day):
            raise ValueError(
                f"日期 {d} 超出交易日历覆盖范围 [{self.first_day}, {self.last_day}]；"
                "新增数据后请重建日历，不要用猜测的日期"
            )

    def next_trading_day(self, d: date, n: int = 1) -> date:
        """d 之后（含 d 若为交易日）的第 n 个交易日。"""
        if n < 0:
            raise ValueError(f"n 不能为负，实际 {n}")
        self._check_range(d)
        idx = self._index.get(d)
        if idx is None:
            # d 不是交易日：落到其后第一个交易日
            idx = self._bisect_left(d) - 1
        target = idx + n
        if target >= len(self._days):
            raise ValueError(f"日期 {d} + {n} 个交易日超出日历末尾 {self.last_day}")
        return self._days[target]

    def prev_trading_day(self, d: date, n: int = 1) -> date:
        if n < 0:
            raise ValueError(f"n 不能为负，实际 {n}")
        self._check_range(d)
        idx = self._index.get(d)
        if idx is None:
            idx = self._bisect_left(d)
        target = idx - n
        if target < 0:
            raise ValueError(f"日期 {d} - {n} 个交易日早于日历起点 {self.first_day}")
        return self._days[target]

    def _bisect_left(self, d: date) -> int:
        lo, hi = 0, len(self._days)
        while lo < hi:
            mid = (lo + hi) // 2
            if self._days[mid] < d:
                lo = mid + 1
            else:
                hi = mid
        return lo

    def settlement_date(self, trade_date: date, lag_days: int = 1) -> date:
        """现券结算日 = 交易日 + ``lag_days`` 个**交易日**（T+N）。"""
        if lag_days < 0:
            raise ValueError(f"lag_days 不能为负，实际 {lag_days}")
        return self.next_trading_day(trade_date, lag_days)

    def trading_days_between(self, start: date, end: date) -> tuple[date, ...]:
        """[start, end] 区间内的交易日（含端点）。"""
        self._check_range(start)
        self._check_range(end)
        if end < start:
            return ()
        lo = self._bisect_left(start)
        hi = self._bisect_left(end)
        while hi < len(self._days) and self._days[hi] <= end:
            hi += 1
        return self._days[lo:hi]

    def to_frame(self) -> pd.DataFrame:
        """导出为 DataFrame（date 一列），便于审计与 diff。"""
        return pd.DataFrame({"trading_day": list(self._days)})
