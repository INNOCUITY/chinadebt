"""远期收益标签（ML 接口层）：**只在离线/训练时生成，永不参与当日决策**。

时序纪律
--------
标签天然是"未来信息"：``as_of`` 时点它**不可计算**。因此本模块：

1. 每条标签都带 ``available_after``（该标签最早可被观测/使用的日期），调用方只能在该
   日期之后把标签喂给模型；
2. 函数 docstring/返回类型都明确：``forward_returns`` 是**离线标签生成器**，不得在
   as-of 特征管线或实盘信号里调用；
3. ``holding_days`` 决定 walk-forward 的 purge 边界（见 :mod:`ficc.ml.walkforward`），
   避免训练标签与验证窗口重叠。

返回值定义（**接口原型，不是正式定价模型**）
--------------------------------------------
``panel`` 是曲线面板（index=估值日，columns=关键期限/年，值=YTM 百分数）。标签用
**平价券一阶近似**把曲线变动映射为持有期收益（费用/票息税/融资成本**不计入**；
正式标签应复用账本/定价层，不在本接口层重写）：

- 价格收益 ≈ ``-D_mod × Δy``，``D_mod = tenor / (1 + y0/100)``；
- carry ≈ ``y0 × 持有自然日 / 365``；
- ``clean_total`` = 价格收益；``carry_total`` = 价格收益 + carry；
- 多列时按列**等权平均**成一个标量（曲线组合口径），单列时即该列。

这只是为了让接口有可计算、可复现的标签；**不能据此写绩效结论**。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Literal

import numpy as np
import pandas as pd

from ficc.data.schema import LABEL_TO_TENOR
from ficc.ml.asof import _coerce_date, _normalise_frame
from ficc.utils.calendar import TradingCalendar

__all__ = [
    "EntryConvention",
    "ForwardLabelSpec",
    "LabelRecord",
    "ReturnType",
    "forward_returns",
]

EntryConvention = Literal["next_open", "next_close"]
ReturnType = Literal["clean_total", "carry_total"]

_ENTRY_OFFSET: dict[str, int] = {"next_open": 0, "next_close": 1}


@dataclass(frozen=True)
class ForwardLabelSpec:
    """远期收益标签定义。

    Parameters
    ----------
    holding_days : int
        持有期（**交易日**数，必须 ≥ 1）。它同时决定 walk-forward 的 purge 下界。
    entry : {"next_open", "next_close"}
        - ``next_open``：以 as-of 收盘价作为"次日开盘"的代理（panel 无盘中 open），
          收益区间 ``[as_of, as_of + holding_days]``；``available_after = as_of + h``；
        - ``next_close``：次日收盘入场，收益区间 ``[as_of+1, as_of+1+h]``；
          ``available_after = as_of + 1 + h``。
        两种约定都保证标签在 as_of **不可计算**。``next_open`` 用前收代理开盘价是
        明确披露的近似，不是真实开盘价。
    return_type : {"clean_total", "carry_total"}
        - ``clean_total``：仅净价变动（不含 carry）；
        - ``carry_total``：净价变动 + 持有期 carry（含票息/骑乘近似）。
    """

    holding_days: int
    entry: EntryConvention = "next_open"
    return_type: ReturnType = "clean_total"

    def __post_init__(self) -> None:
        if isinstance(self.holding_days, bool) or not isinstance(self.holding_days, int):
            raise TypeError(f"holding_days 必须是整数，实际 {type(self.holding_days).__name__}")
        if self.holding_days < 1:
            raise ValueError(f"holding_days 必须 ≥ 1，实际 {self.holding_days}")
        if self.entry not in _ENTRY_OFFSET:
            raise ValueError(f"entry 必须是 next_open/next_close，实际 {self.entry!r}")
        if self.return_type not in ("clean_total", "carry_total"):
            raise ValueError(
                f"return_type 必须是 clean_total/carry_total，实际 {self.return_type!r}"
            )

    @property
    def entry_offset(self) -> int:
        """入场日相对 as_of 的交易日偏移（0 或 1）。"""
        return _ENTRY_OFFSET[self.entry]

    @property
    def available_offset(self) -> int:
        """标签可用的交易日偏移 = 入场偏移 + 持有期（用于 walk-forward 位置空间校验）。"""
        return self.entry_offset + self.holding_days


@dataclass(frozen=True)
class LabelRecord:
    """一条远期收益标签：``available_after`` 是它最早可被使用的时间。

    ``available_after`` 必须严格晚于 ``as_of``；否则构造直接失败（防止把未来标签
    伪装成当期信息）。
    """

    as_of: date
    available_after: date
    value: float

    def __post_init__(self) -> None:
        as_of = _coerce_date(self.as_of, name="as_of")
        available_after = _coerce_date(self.available_after, name="available_after")
        object.__setattr__(self, "as_of", as_of)
        object.__setattr__(self, "available_after", available_after)
        if available_after <= as_of:
            raise ValueError(
                f"标签 available_after={available_after} 不晚于 as_of={as_of}：标签必须是未来信息"
            )
        value = float(self.value)
        if not np.isfinite(value):
            raise ValueError(f"标签值必须是有限数，实际 {self.value!r}")
        object.__setattr__(self, "value", value)

    def is_available_at(self, when: date) -> bool:
        """``when`` 当天是否已经能拿到该标签。"""
        return _coerce_date(when, name="when") >= self.available_after

    def as_dict(self) -> dict[str, str | float]:
        return {
            "as_of": self.as_of.isoformat(),
            "available_after": self.available_after.isoformat(),
            "value": float(self.value),
        }


def _validate_horizon_bounds(horizon_bounds: tuple[int, int] | None) -> tuple[int, int] | None:
    if horizon_bounds is None:
        return None
    if not isinstance(horizon_bounds, (tuple, list)) or len(horizon_bounds) != 2:
        raise TypeError("horizon_bounds 必须是 (min_days, max_days)")
    low, high = horizon_bounds
    for name, value in (("min_days", low), ("max_days", high)):
        if isinstance(value, bool) or not isinstance(value, int):
            raise TypeError(f"horizon_bounds.{name} 必须是整数")
    if low < 1 or high < low:
        raise ValueError(f"horizon_bounds 非法：{horizon_bounds}")
    return int(low), int(high)


def _tenor_of(column: object) -> float | None:
    """把 panel 列名解析为期限（年）；支持 ``2.0``、``"2.0"``、``"2y"``。"""
    if isinstance(column, bool):
        return None
    if isinstance(column, (int, float)):
        return float(column)
    key = str(column).strip().lower()
    if key in LABEL_TO_TENOR:
        return float(LABEL_TO_TENOR[key])
    try:
        return float(key)
    except (TypeError, ValueError):
        return None


def forward_returns(
    panel: pd.DataFrame,
    as_of: date,
    spec: ForwardLabelSpec,
    *,
    calendar: TradingCalendar,
    horizon_bounds: tuple[int, int] | None = None,
) -> LabelRecord:
    """生成 ``as_of`` 决策对应的**远期收益标签**。

    .. warning::
       标签生成器只允许在**离线 / 训练 / 评估**阶段调用，并且必须携带
       ``LabelRecord.available_after``。**禁止**在 as-of 特征管线、实盘信号或任何
       "t 日决策只看 t 日数据"的路径里调用本函数；它读的正是 as_of 之后的数据。

    Parameters
    ----------
    panel : DataFrame
        曲线面板（index=估值日，columns=期限/年，值=YTM 百分数）。
    as_of : date
        决策日（信号日）。
    spec : ForwardLabelSpec
        持有期、入场约定、收益口径。
    calendar : TradingCalendar
        交易日历（持有期按交易日推进）。
    horizon_bounds : (int, int), optional
        预注册的持有期允许范围（交易日）。``spec.holding_days`` 超出即报错，
        防止"按结果挑 horizon"。

    Returns
    -------
    LabelRecord
        ``value`` 为小数收益（0.001 = 10bp），``available_after`` 严格晚于 ``as_of``。

    Raises
    ------
    ValueError
        ``as_of``/``exit`` 超出 panel 或日历覆盖；horizon 越界；panel 非数值。
    """
    as_of = _coerce_date(as_of, name="as_of")
    if not isinstance(spec, ForwardLabelSpec):
        raise TypeError("spec 必须是 ForwardLabelSpec")
    if not isinstance(panel, pd.DataFrame):
        raise TypeError("panel 必须是 pandas.DataFrame")
    if panel.columns.empty:
        raise ValueError("panel 至少需要一列期限")
    if not isinstance(calendar, TradingCalendar):
        raise TypeError("calendar 必须是 TradingCalendar")

    bounds = _validate_horizon_bounds(horizon_bounds)
    if bounds is not None and not (bounds[0] <= spec.holding_days <= bounds[1]):
        raise ValueError(f"holding_days={spec.holding_days} 超出预注册 horizon_bounds={bounds}")

    frame = _normalise_frame(panel)
    assert isinstance(frame, pd.DataFrame)

    # 决策日必须是交易日；非交易日回退到其前一个交易日（显式记录在返回值里）
    anchor = as_of if calendar.is_trading_day(as_of) else calendar.prev_trading_day(as_of, 1)
    if spec.entry == "next_open":
        entry = anchor
        exit_ = calendar.next_trading_day(anchor, spec.holding_days)
    else:
        entry = calendar.next_trading_day(anchor, 1)
        exit_ = calendar.next_trading_day(entry, spec.holding_days)

    entry_ts = pd.Timestamp(entry)
    exit_ts = pd.Timestamp(exit_)
    if entry_ts not in frame.index:
        raise ValueError(f"入场日 {entry} 不在 panel 索引内，无法计算标签")
    if exit_ts not in frame.index:
        raise ValueError(f"出场日 {exit_} 不在 panel 索引内，标签尚不可计算")

    entry_row = frame.loc[entry_ts]
    exit_row = frame.loc[exit_ts]
    if isinstance(entry_row, pd.DataFrame) or isinstance(exit_row, pd.DataFrame):
        raise ValueError("panel 索引存在重复日期，无法唯一确定标签")
    if exit_ <= entry:
        raise ValueError(f"出场日 {exit_} 不晚于入场日 {entry}")

    days = float((exit_ - entry).days)
    values: list[float] = []
    for column in frame.columns:
        tenor = _tenor_of(column)
        if tenor is None:
            raise ValueError(f"panel 列必须是期限（年）或 '2y' 这类标签，实际 {column!r}")
        if tenor <= 0:
            raise ValueError(f"期限必须为正数，实际 {column!r}")
        y0 = float(entry_row[column])
        y1 = float(exit_row[column])
        if not (np.isfinite(y0) and np.isfinite(y1)):
            raise ValueError(f"期限 {column} 的收益率不是有限数：{y0!r}/{y1!r}")
        mod_duration = tenor / (1.0 + y0 / 100.0)
        price_return_pct = -mod_duration * (y1 - y0)
        carry_pct = y0 * days / 365.0
        if spec.return_type == "carry_total":
            total_pct = price_return_pct + carry_pct
        else:
            total_pct = price_return_pct
        values.append(total_pct / 100.0)

    value = float(np.mean(values))
    if not np.isfinite(value):
        raise ValueError("标签值不是有限数，请检查 panel 数据质量")
    return LabelRecord(as_of=as_of, available_after=exit_, value=value)
