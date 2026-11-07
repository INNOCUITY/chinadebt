"""固定成交路径的成本回放（研究对照实验 §12.10 任务 A）。

用途
----
引擎（``backtest.engine``）给出一次**完整决策路径**的成交集合 ``trades``。本模块
**不重新决策**，只用同一批成交按账本的结算流逐日重放，并允许把三类成本分别关闭：

- ``spread``：执行价从"含半价差的执行净价"改回"曲线中间净价"；
- ``fee``：手续费（``fee_bp`` × 净价成交额）与结算费（元/笔）置零；
- ``funding``：融资利率置零（含 ``funding_rate_by_date`` 逐日利率）。

因此可以把 NAV 变化**纯分解**为"价差 / 费用 / 融资"三块，不混入策略行为反馈。

核心验收
--------
三开关全开（默认）时，回放 NAV 必须与引擎 NAV **逐日一致**（< 1e-6 元）。这不是
自证：回放只读取 ``trades`` 表与市场数据（曲线/资金/日历），独立重建
:class:`~ficc.portfolio.ledger.TradeRecord` 并逐日跑 :class:`~ficc.portfolio.ledger.Ledger`。

口径说明（必须明示）
--------------------
1. **价差只披露、不重复扣**：``spread`` 已包含在 ``trades.clean_price`` 里；成本表
   的 ``spread`` 列只把 ``trades.spread_cost`` 按成交日归集展示，**不会**再从 NAV
   里扣一次。``spread=False`` 时改用 ``mid_clean_price``（缺失时按
   ``mid = clean ∓ spread_cost / (face/100)`` 反推；买入取减号、卖出取加号，与
   ``construction.execution_price`` 的"买入付半价差、卖出收低半价差"约定一致）。
2. **费用基数固定为原始执行净价**：费用按引擎原始 ``clean_price`` 预计算并写入
   ``TradeRecord.fee``，重放账本的手续费配置置零。这样 ``spread=False`` 只改变
   PnL 成交价、不改变计费基数，``spread`` 开关的 NAV 差才严格等于价差成本（否则
   手续费会随成交价一起变小，污染分解）。
3. **融资口径与引擎完全一致**：每个交易日取"≤ 该日最近已知 FDR007"（as-of 前向
   填充，不读未来），逐自然日 ACT/365 计提，按 NAV 日合并入账。
4. **不读未来**：重建第 ``t`` 日快照只用 ``≤ t`` 的成交与 ``t`` 日曲线；回放起点
   为请求窗口首日，之前的成交一律不进入账本。
5. **分解非严格可加**：关闭某项成本会改变逐日现金；若策略/基准出现负现金，
   融资成本会随现金一起变化（融资反馈），因此"单项开关的 NAV 影响"之和与
   "全部关闭的总影响"不必逐分逐厘相等（方向与量级仍可解释，逐日成本列照实披露）。

导出契约
--------
``ReplayResult.costs`` 列固定为 ``date, fee, funding, spread, total``：

- ``fee`` / ``funding``：当日账本事件金额（正数 = 成本）；
- ``spread``：当日成交的价差成本（仅披露；``spread=False`` 时为 0）；
- ``total = fee + funding + spread``（与引擎 ``BacktestResult.costs`` 同口径）。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import numpy as np
import pandas as pd

from ficc.backtest.engine import BacktestResult
from ficc.config import Config, get_config
from ficc.curve.curve import Curve
from ficc.data.schema import CANONICAL_TENORS, BondStatic
from ficc.portfolio.ledger import Ledger, TradeRecord

__all__ = [
    "COSTS_COLUMNS",
    "CostSwitches",
    "ReplayResult",
    "replay_engine_result",
    "replay_trades",
]

COSTS_COLUMNS: tuple[str, ...] = ("date", "fee", "funding", "spread", "total")

#: 重建 ``TradeRecord`` 所需的静态条款列（与账本 ``trades_frame`` 契约一致）
_STATIC_COLUMNS: tuple[str, ...] = (
    "bond_code",
    "clean_price",
    "signal_date",
    "trade_date",
    "settlement_date",
    "coupon_pct",
    "freq",
    "issue_date",
    "maturity_date",
    "accrual",
    "market",
)


@dataclass(frozen=True)
class CostSwitches:
    """成本开关：``True`` = 该项成本生效，``False`` = 置零。"""

    spread: bool = True
    fee: bool = True
    funding: bool = True


@dataclass
class ReplayResult:
    """固定路径回放结果。

    Attributes
    ----------
    nav : pd.Series
        逐交易日 NAV（index 为 ``datetime.date``，序列名 ``nav``）。
    cashflows : pd.DataFrame
        重放账本的全部现金事件（列契约见 ``Ledger.cashflows_frame``）。
    costs : pd.DataFrame
        逐日成本分解，列固定为 ``COSTS_COLUMNS``。
    """

    nav: pd.Series
    cashflows: pd.DataFrame
    costs: pd.DataFrame


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------
def _as_date(value: Any, *, field: str) -> date:
    """把 ``date`` / ``datetime`` / ``pd.Timestamp`` / ``np.datetime64`` 归一为 ``date``。"""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, pd.Timestamp):
        return value.date()
    if isinstance(value, np.datetime64):
        return pd.Timestamp(value).date()
    raise TypeError(f"{field} 不是日期类型：{type(value).__name__}")


def _curve_at(panel: pd.DataFrame, tenors: list[float], d: date) -> Curve:
    """取 ``d`` 日曲线（与引擎 ``_curve_at`` 同口径：11 个标准期限、Hermite 插值）。"""
    row = panel.loc[pd.Timestamp(d)].reindex(tenors)
    if row.isna().any():
        raise ValueError(f"{d} 曲线存在缺失期限：{row[row.isna()].index.tolist()}")
    return Curve(as_of=d, tenors=np.asarray(tenors), rates=tuple(row.values), method="hermite")


def _funding_map(
    funding: pd.Series | None, days: list[date], cfg: Config, *, enabled: bool
) -> dict[date, float]:
    """逐日融资利率（百分数）。``enabled=False`` 时全为 0。

    与引擎一致：每个交易日取 ``≤ 该日最近已知利率``（历史 as-of 前向填充，**禁止
    取未来值**）；早于全部已知日期才回退 ``cfg.costs.repo_rate_fallback_const``。
    """
    if not enabled:
        return dict.fromkeys(days, 0.0)
    fallback = float(cfg.costs.repo_rate_fallback_const) * 100.0
    out: dict[date, float] = {}
    for d in days:
        if funding is not None and not funding.empty:
            prior = funding.loc[:d]
            out[d] = float(prior.iloc[-1]) if len(prior) else fallback
        else:
            out[d] = fallback
    return out


def _fee_of(face: float, clean_price: float, cfg: Config) -> float:
    """手续费 + 结算费（元），与账本 ``Ledger._trade_fee`` 同式。"""
    notional = face / 100.0 * clean_price
    return float(cfg.costs.fee_bp) / 1e4 * notional + float(cfg.costs.settlement_fee_cny_per_trade)


def _ledger_cfg(cfg: Config) -> Config:
    """重放账本用的配置：手续费配置置零（费用已预计算进 ``TradeRecord.fee``）。

    这样 ``spread=False`` 换用中间价时不会顺带改变手续费基数，价差开关的 NAV
    差才严格等于价差成本；其余配置（计息/定价/日历/初始现金）保持原样。
    """
    costs = cfg.costs.model_copy(update={"fee_bp": 0.0, "settlement_fee_cny_per_trade": 0.0})
    return cfg.model_copy(update={"costs": costs})


def _mid_clean_price(row: Any, *, spread_cost: float) -> float:
    """由成交行还原曲线中间净价。

    优先使用引擎导出的 ``mid_clean_price``；旧版导出只带 ``spread_cost`` 时按
    ``mid = clean −/+ spread_cost/(face/100)`` 反推（买入减、卖出加），误差在
    1 ulp 量级，足够成本分解使用。
    """
    mid = row.get("mid_clean_price")
    if mid is not None and np.isfinite(float(mid)):
        return float(mid)
    face = float(row["face_value"])
    per100 = float(spread_cost) / (face / 100.0) if face > 0 else 0.0
    sign = 1.0 if str(row["side"]) == "buy" else -1.0
    return float(row["clean_price"]) - sign * per100


def _row_get(row: Any, key: str) -> Any:
    """``Series.get`` 的统一封装（``None`` 表示缺失，区分 NaN 由调用方判断）。"""
    return row.get(key, None)


def _build_trade_records(
    trades: pd.DataFrame, cfg: Config, switches: CostSwitches
) -> tuple[list[TradeRecord], pd.DataFrame]:
    """把成交表重建为 ``TradeRecord`` 列表，并返回附带价差披露列的成交副本。"""
    if trades is None or len(trades) == 0:
        return [], pd.DataFrame(columns=[*list(_STATIC_COLUMNS), "spread_cost"])
    missing = [column for column in _STATIC_COLUMNS if column not in trades.columns]
    if missing:
        raise ValueError(
            "trades 缺少重建 TradeRecord 必需的列："
            f"{missing}（需要与 Ledger.trades_frame 契约一致；基准账本成交请先补静态条款）"
        )

    records: list[TradeRecord] = []
    spread_rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    for row in trades.to_dict("records"):
        trade_id = str(row["trade_id"])
        if trade_id in seen:
            raise ValueError(f"trades 含重复 trade_id：{trade_id!r}")
        seen.add(trade_id)
        bond = BondStatic(
            code=str(row["bond_code"]),
            issue_date=_as_date(row["issue_date"], field="issue_date"),
            maturity_date=_as_date(row["maturity_date"], field="maturity_date"),
            coupon_pct=float(row["coupon_pct"]),
            freq=int(row["freq"]),
            accrual=str(row["accrual"]),
            market=str(row["market"]),
        )
        exec_price = float(row["clean_price"])
        raw_spread_cost = _row_get(row, "spread_cost")
        spread_cost = (
            float(raw_spread_cost)
            if raw_spread_cost is not None and np.isfinite(float(raw_spread_cost))
            else 0.0
        )
        clean_price = (
            _mid_clean_price(row, spread_cost=spread_cost) if not switches.spread else exec_price
        )
        fee = _fee_of(float(row["face_value"]), exec_price, cfg) if switches.fee else 0.0
        records.append(
            TradeRecord(
                trade_id=trade_id,
                bond=bond,
                side=str(row["side"]),
                face_value=float(row["face_value"]),
                clean_price=clean_price,
                signal_date=_as_date(row["signal_date"], field="signal_date"),
                trade_date=_as_date(row["trade_date"], field="trade_date"),
                settlement_date=_as_date(row["settlement_date"], field="settlement_date"),
                fee=fee,
            )
        )
        spread_rows.append(
            {
                "trade_date": _as_date(row["trade_date"], field="trade_date"),
                "spread_cost": spread_cost if switches.spread else 0.0,
            }
        )
    return records, pd.DataFrame(spread_rows)


def _costs_frame(
    days: list[date],
    cashflows: pd.DataFrame,
    trades: pd.DataFrame,
    switches: CostSwitches,
) -> pd.DataFrame:
    """逐日成本分解：``fee``/``funding`` 来自现金事件，``spread`` 按成交日归集披露。"""
    fee_by_day: dict[date, float] = {}
    funding_by_day: dict[date, float] = {}
    if len(cashflows):
        for row in cashflows.itertuples(index=False):
            day = _as_date(row.date, field="cashflow date")
            if row.kind == "fee":
                fee_by_day[day] = fee_by_day.get(day, 0.0) - float(row.amount)
            elif row.kind == "funding":
                funding_by_day[day] = funding_by_day.get(day, 0.0) - float(row.amount)
    spread_by_day: dict[date, float] = {}
    if switches.spread and len(trades):
        for row in trades.itertuples(index=False):
            spread_by_day[row.trade_date] = spread_by_day.get(row.trade_date, 0.0) + float(
                row.spread_cost
            )
    rows: list[dict[str, Any]] = []
    for day in days:
        fee = fee_by_day.get(day, 0.0)
        funding = funding_by_day.get(day, 0.0)
        spread = spread_by_day.get(day, 0.0)
        rows.append(
            {
                "date": day,
                "fee": fee,
                "funding": funding,
                "spread": spread,
                "total": fee + funding + spread,
            }
        )
    return pd.DataFrame(rows, columns=list(COSTS_COLUMNS))


def _window_days(
    calendar: Any,
    trades: pd.DataFrame,
    start: date | None,
    end: date | None,
) -> list[date]:
    """确定回放窗口的交易日集合。"""
    if start is None or end is None:
        if trades is None or len(trades) == 0:
            raise ValueError("trades 为空时必须显式提供 start/end")
        trade_dates = [_as_date(v, field="trade_date") for v in trades["trade_date"]]
        start = start or min(trade_dates)
        end = end or max(trade_dates)
    days = list(calendar.trading_days_between(start, end))
    if not days:
        raise ValueError(f"回放窗口 {start}~{end} 内没有交易日")
    return days


def _filter_trades(trades: pd.DataFrame, start: date, end: date) -> pd.DataFrame:
    """只保留成交日落在回放窗口内的成交（窗口外成交不得进入账本）。"""
    if trades is None or len(trades) == 0:
        return pd.DataFrame(columns=list(_STATIC_COLUMNS))
    mask = [start <= _as_date(v, field="trade_date") <= end for v in trades["trade_date"]]
    return trades.loc[np.asarray(mask, dtype=bool)].reset_index(drop=True)


def _replay(
    trades: pd.DataFrame,
    *,
    panel: pd.DataFrame,
    calendar: Any,
    funding: pd.Series | None,
    cfg: Config,
    start: date,
    end: date,
    switches: CostSwitches,
) -> ReplayResult:
    days = _window_days(calendar, trades, start, end)
    window_trades = _filter_trades(trades, days[0], days[-1])
    records, spread_frame = _build_trade_records(window_trades, cfg, switches)
    funding_map = _funding_map(funding, days, cfg, enabled=switches.funding)
    ledger = Ledger(
        calendar,
        initial_cash=float(cfg.backtest.init_cash),
        funding_rate_pct=funding_map[days[0]],
        funding_rate_by_date=funding_map,
        cfg=_ledger_cfg(cfg),
    )
    # 账本要求按成交日顺序提交（同日内部再按买/卖分序结算）；稳定排序保留原顺序。
    for record in sorted(records, key=lambda trade: trade.trade_date):
        ledger.submit(record)

    curve_tenors = list(CANONICAL_TENORS)
    nav: dict[date, float] = {}
    for day in days:
        nav[day] = ledger.process_day(day, _curve_at(panel, curve_tenors, day)).nav

    cashflows = ledger.cashflows_frame()
    costs = _costs_frame(days, cashflows, spread_frame, switches)
    return ReplayResult(
        nav=pd.Series(nav, name="nav").sort_index(),
        cashflows=cashflows,
        costs=costs,
    )


# ---------------------------------------------------------------------------
# 公共入口
# ---------------------------------------------------------------------------
def replay_trades(
    trades: pd.DataFrame,
    *,
    panel: pd.DataFrame,
    calendar: Any,
    funding: pd.Series | None = None,
    cfg: Config | None = None,
    start: date | None = None,
    end: date | None = None,
    switches: CostSwitches | None = None,
) -> ReplayResult:
    """按固定成交路径重放策略账本，输出 NAV / 现金流 / 成本分解。

    Parameters
    ----------
    trades : pd.DataFrame
        成交表（列契约见 ``Ledger.trades_frame``；额外需要 ``spread_cost``，可选
        ``mid_clean_price``）。基准账本的成交经过静态条款补齐后同样适用。
    panel : pd.DataFrame
        曲线面板（index = 日期，columns = 标准期限，值 = 收益率百分数）。
    calendar : TradingCalendar
        真实 CN_IB 交易日历（结算日/交易日判定唯一来源）。
    funding : pd.Series | None
        FDR007 日序列（index = 日期）；``None`` 时退回收盘常量。
    cfg : Config | None
        全局配置（``None`` 读当前配置）。
    start, end : date | None
        回放窗口；缺省时取成交日的最小/最大值（仅用于便捷调用，逐日 NAV 会短于
        引擎窗口）。
    switches : CostSwitches | None
        成本开关；``None`` = 三项全开（用于验证与引擎 NAV 一致）。

    Returns
    -------
    ReplayResult
    """
    config = get_config() if cfg is None else cfg
    day_switches = switches if switches is not None else CostSwitches()
    if trades is None:
        trades = pd.DataFrame()
    days = _window_days(calendar, trades, start, end)
    return _replay(
        trades,
        panel=panel,
        calendar=calendar,
        funding=funding,
        cfg=config,
        start=days[0],
        end=days[-1],
        switches=day_switches,
    )


def replay_engine_result(
    result: BacktestResult,
    *,
    panel: pd.DataFrame,
    calendar: Any,
    funding: pd.Series | None = None,
    cfg: Config | None = None,
    switches: CostSwitches | None = None,
) -> ReplayResult:
    """对一次引擎回测的成交路径做固定路径回放（窗口取 ``result.start/end``）。"""
    return replay_trades(
        result.trades,
        panel=panel,
        calendar=calendar,
        funding=funding,
        cfg=cfg,
        start=result.start,
        end=result.end,
        switches=switches,
    )
