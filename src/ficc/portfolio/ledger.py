"""固定券账本（Phase 2 第 1 步）。

设计要点
--------
1. **条款锁定**：:class:`TradeRecord` 里的 ``BondStatic`` 是建仓时锁定的条款；
   持有期内票息/频率/到期日不变，只有剩余期限随 ``as_of`` 缩短。估值一律用
   剩余期限取 YTM + :func:`ficc.pricing.bond.dirty_price_from_ytm`，**不会**把
   持仓重建为"同期限新平价券"。
2. **三层日期**：``signal_date < trade_date <= settlement_date``，且
   ``settlement_date == calendar.settlement_date(trade_date, cfg.pricing.settlement_lag_days)``。
   成交在 ``trade_date`` 生效（经济敞口进入持仓），现金在 ``settlement_date``
   划转；未结算成交列入 ``pending_settlements``，**不计入已结算现金**。
3. **现金流事件**：票息/本金/融资/费用全部落成 :class:`CashFlowEvent`；
   付息日非交易日顺延到下一交易日；到期归还本金（+ 末期票息）；负现金按
   ``funding_rate_pct/365`` **逐自然日**计提（含周末/节假日），但融资成本在
   每次 :meth:`Ledger.process_day` 时**合并成一笔**、事件日期 = 该 ``as_of``
   （NAV 快照日），从而对账侧能把全部内部现金流映射到 NAV 日；支持零成本对照。
4. **合成券登记权益（R24）**：经济持仓（:attr:`_faces`，成交日生效）与**已结算
   面值**（:attr:`_settled_faces`，仅在结算日增减）分离；票息/本金归属按
   **付息日（或到期日）日初的已结算面值**判定（详见「登记权益规则」一节），
   因此付息日当天成交/结算的买方**不享本期票息**。
5. **NAV 恒等式（trade-date 会计）**：
   ``nav == cash + Σ(face/100 × dirty) + pending_cash``。成交在 ``trade_date``
   生效（经济敞口进持仓），现金在 ``settlement_date`` 划转；未结算成交的应付/应收
   以 ``pending_cash``（买入为负、卖出为正）计入 NAV，避免未结算期间把未付买入款
   双计一次。导出 :meth:`Ledger.snapshots_frame` 时按快照行**独立重算一次**并校验。
6. **可复现**：不依赖当前时间/随机数；同一组成交、曲线、参数逐位可复现。

登记权益规则（合成券，必须明示）
--------------------------------
- **归属判定日**：票息归属付息日、本金归属到期日的**日初**已结算面值
  （``entitlement = _settled_faces`` 的快照）；实现见
  :meth:`Ledger._process_calendar_day`。
- **享息条件**：结算日 ≤ 付息日前一自然日 的持仓享本期票息/本金。付息日
  （或到期日）当天**成交或结算**的买方**不享本期票息/本金**——经济风险已随成交
  转移，但登记权益尚未转移。
- **付息日当天卖出**：卖方在付息日日初仍登记在册，仍享本期票息；买方通过全价中
  的应计利息（付息日当天 AI 从 0 重新累积）补偿，这是债券市场惯例。
- **非交易日付息**：归属仍按**付息日（日历日）日初**的已结算面值判定，**支付**
  顺延到下一交易日（:meth:`Ledger._pay_or_defer`）；顺延只影响现金到账日，
  不改变归属日与金额。
- 该规则是**本项目的合成券简化契约**，不代表逐券真实登记结算规则；真实债券的
  登记日/除息日以中债/托管机构公告为准。

会计口径（必须明示）
--------------------
- ``cash`` 是**已结算现金**（不含未结算成交、不含应收票息/本金）。
- ``pending_cash`` 是**未结算成交在结算日将划转的带符号金额**：买入为负（未来
  付现）、卖出为正（未来收现），按 ``clean_price + accrued_interest(bond,
  settlement_date)`` 全价 × ``face/100`` 计算。
- ``face_value``（快照/持仓）是**经济持仓面值**（成交日生效）；``settled_face``
  是**已结算/登记面值**（结算日生效）。两者在未结算期间不同：买入成交日经济
  面值已增加、登记面值尚未增加；卖出成交日经济面值已减少、登记面值尚未减少。
- NAV（trade-date 会计）= ``cash + ΣMV + pending_cash``：成交日只反映价格变动，
  结算日 ``cash`` 变动、``pending_cash`` 归零，NAV 连续。旧版
  ``nav = cash + ΣMV`` 会在未结算期间把未付买入款双计，已由 C-013 修正。
- 手续费：``fee_bp`` 按**净价成交金额**（``face/100 × clean_price``）计，
  结算费按笔 + ``TradeRecord.fee`` 叠加，写 ``fee`` 事件（结算日）。
- 融资（R25）：FDR007 是**融资代理利率**（不是逐笔回购合同/真实成交成本）；
  每个**自然日**日终对负现金按 ``ACT/365`` 计提（含周末/节假日），缺少当日利率时
  取 **≤ 该日的最近已知利率**（历史 as-of 前向填充，**禁止取未来值**；早于所有
  已知日期才回退构造时的 ``funding_rate_pct``）；利息直接计入现金，对负现金等价
  于逐日复利。融资成本在每次 :meth:`Ledger.process_day` 时**合并为一笔**、
  事件日期 = ``as_of``（NAV 快照日），从而对账侧能把全部内部现金流映射到 NAV 日。

导出四件套（列名即对账契约）
----------------------------
- ``trades_frame``：``trade_id, bond_code, side, face_value, clean_price,
  signal_date, trade_date, settlement_date, fee, coupon_pct, freq, issue_date,
  maturity_date, accrual, market``
- ``cashflows_frame``：``date, kind, amount, bond_code, trade_id, note``
- ``snapshots_frame``：``as_of, cash, pending_cash, nav, bond_code, face_value,
  settled_face, remaining_years, ytm_pct, clean_price, accrued_interest,
  dirty_price, market_value, mod_duration, dv01, n_positions, n_pending, notes``
  （持仓级长表；每个快照至少一行，无持仓时持仓列为 NaN）
- ``nav_series``：``pd.Series``，index 名 ``as_of``、序列名 ``nav``

本模块只做账本，不含策略信号与仓位约束。
"""

from __future__ import annotations

import bisect
import copy
import dataclasses
import math
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Literal

import pandas as pd

from ficc.config import Config, get_config
from ficc.data.schema import BondStatic
from ficc.pricing.analytics import dv01 as analytics_dv01
from ficc.pricing.analytics import mod_duration as analytics_mod_duration
from ficc.pricing.bond import dirty_price_from_ytm
from ficc.pricing.conventions import (
    accrued_interest,
    annual_anniversaries,
    coupon_dates,
    day_count,
)

__all__ = [
    "CashFlowEvent",
    "Ledger",
    "LedgerSnapshot",
    "PositionView",
    "TradeRecord",
]

Side = Literal["buy", "sell"]
EventKind = Literal["initial", "trade_buy", "trade_sell", "coupon", "principal", "fee", "funding"]

TRADES_COLUMNS: tuple[str, ...] = (
    "trade_id",
    "bond_code",
    "side",
    "face_value",
    "clean_price",
    "signal_date",
    "trade_date",
    "settlement_date",
    "fee",
    "coupon_pct",
    "freq",
    "issue_date",
    "maturity_date",
    "accrual",
    "market",
)
CASHFLOWS_COLUMNS: tuple[str, ...] = (
    "date",
    "kind",
    "amount",
    "bond_code",
    "trade_id",
    "note",
)
SNAPSHOTS_COLUMNS: tuple[str, ...] = (
    "as_of",
    "cash",
    "pending_cash",
    "nav",
    "bond_code",
    "face_value",
    "settled_face",
    "remaining_years",
    "ytm_pct",
    "clean_price",
    "accrued_interest",
    "dirty_price",
    "market_value",
    "mod_duration",
    "dv01",
    "n_positions",
    "n_pending",
    "notes",
)


# ---------------------------------------------------------------------------
# 记录类型
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class TradeRecord:
    """一笔成交（三层日期 + 建仓时锁定的债券条款）。"""

    trade_id: str
    bond: BondStatic
    side: Side
    face_value: float
    clean_price: float
    signal_date: date
    trade_date: date
    settlement_date: date
    fee: float = 0.0

    def __post_init__(self) -> None:
        if not isinstance(self.trade_id, str) or not self.trade_id:
            raise ValueError("trade_id 必须是非空字符串")
        if not isinstance(self.bond, BondStatic):
            raise TypeError(f"bond 必须是 BondStatic，实际 {type(self.bond).__name__}")
        if self.side not in ("buy", "sell"):
            raise ValueError(f"side 必须是 'buy'/'sell'，实际 {self.side!r}")
        for field_name in ("signal_date", "trade_date", "settlement_date"):
            value = getattr(self, field_name)
            if not isinstance(value, date) or isinstance(value, datetime):
                raise TypeError(f"{field_name} 必须是 datetime.date，实际 {type(value).__name__}")
        if not math.isfinite(self.face_value) or self.face_value <= 0:
            raise ValueError(f"face_value 必须为正数，实际 {self.face_value!r}")
        if not math.isfinite(self.clean_price) or self.clean_price < 0:
            raise ValueError(f"clean_price 必须为非负有限数，实际 {self.clean_price!r}")
        if not math.isfinite(self.fee) or self.fee < 0:
            raise ValueError(f"fee 必须为非负有限数，实际 {self.fee!r}")


@dataclass(frozen=True)
class CashFlowEvent:
    """一笔现金事件（正=流入，负=流出，单位元）。"""

    date: date
    kind: EventKind
    amount: float
    bond_code: str | None = None
    trade_id: str | None = None
    note: str = ""


@dataclass(frozen=True)
class PositionView:
    """某一 ``as_of`` 的持仓估值视图（金额元，价格元/百元面值）。

    ``face_value`` 是**经济持仓面值**（成交日生效）；``settled_face`` 是
    **已结算/登记面值**（结算日生效），票息/本金归属按后者在付息日/到期日日初的
    快照判定。未结算期间两者不同，例如买入成交日 ``face_value`` 已增加而
    ``settled_face`` 尚未增加。
    """

    bond: BondStatic
    face_value: float
    remaining_years: float
    ytm_pct: float
    clean_price: float
    accrued_interest: float
    dirty_price: float
    market_value: float
    mod_duration: float | None
    dv01: float | None
    settled_face: float = 0.0


@dataclass(frozen=True)
class LedgerSnapshot:
    """某一 ``as_of`` 的账本快照（trade-date 会计）。

    ``nav = cash + Σ market_value + pending_cash``：``cash`` 是已结算现金，
    ``pending_cash`` 是未结算成交在结算日将划转的带符号金额（买入为负、卖出为正），
    因此成交日经济敞口立即进入 NAV，而不会因现金未划转而虚增净值。
    """

    as_of: date
    cash: float
    pending_cash: float
    positions: tuple[PositionView, ...]
    nav: float
    pending_settlements: tuple[TradeRecord, ...]
    notes: tuple[str, ...] = ()


# ---------------------------------------------------------------------------
# 内部工具
# ---------------------------------------------------------------------------
def _resolve_config(cfg: Config | None) -> Config:
    """显式 ``cfg`` 优先；``None`` 时读全局配置（不回落硬编码默认值）。"""
    return cfg if cfg is not None else get_config()


def _check_date(value: Any, *, field: str) -> date:
    if not isinstance(value, date) or isinstance(value, datetime):
        raise TypeError(f"{field} 必须是 datetime.date，实际 {type(value).__name__}")
    return value


def _coupon_amount(bond: BondStatic, face_value: float, coupon_date: date) -> float:
    """一次付息的现金金额（元）。

    - 规则附息：``coupon_pct/100 × face / freq``；
    - 利随本清：只在到期日一次性支付累计票息 ``coupon_pct/100 × face × 整年数``；
    - 贴现/零息：无票息。
    """
    annual_coupon = bond.coupon_pct / 100.0 * face_value  # 元/年
    if bond.accrual == "coupon":
        return annual_coupon / float(bond.freq)
    if bond.accrual == "lump_sum" and coupon_date == bond.maturity_date:
        complete_years = len(annual_anniversaries(bond)) - 1
        return annual_coupon * complete_years
    return 0.0


# ---------------------------------------------------------------------------
# 账本
# ---------------------------------------------------------------------------
class Ledger:
    """固定券账本：现金 + 锁条款持仓 + 现金流事件 + 逐日快照。"""

    def __init__(
        self,
        calendar: Any,
        *,
        initial_cash: float,
        funding_rate_pct: float,
        funding_rate_by_date: Mapping[date, float] | None = None,
        cfg: Config | None = None,
    ) -> None:
        for attr in ("is_trading_day", "settlement_date", "next_trading_day", "first_day"):
            if not hasattr(calendar, attr):
                raise TypeError(
                    f"calendar 缺少必需成员 {attr!r}：需要 ficc.utils.calendar.TradingCalendar"
                )
        if not math.isfinite(initial_cash):
            raise ValueError(f"initial_cash 必须为有限数，实际 {initial_cash!r}")
        rate = float(funding_rate_pct)
        if not math.isfinite(rate) or rate < 0:
            raise ValueError(f"funding_rate_pct 必须是非负有限数，实际 {funding_rate_pct!r}")

        self._calendar = calendar
        self._cfg = _resolve_config(cfg)
        self._initial_cash = float(initial_cash)
        self._cash = float(initial_cash)
        self._funding_rate_pct = rate
        self._funding_rates: dict[date, float] = {}
        for day, value in dict(funding_rate_by_date or {}).items():
            self._funding_rates[_check_date(day, field="funding_rate_by_date 的日期")] = float(
                value
            )
        self._funding_dates_sorted: list[date] | None = None  # 懒排序缓存（前向填充用）

        self._trades: list[TradeRecord] = []
        self._trade_ids: set[str] = set()
        self._known_bonds: dict[str, BondStatic] = {}
        self._events: list[CashFlowEvent] = []
        self._snapshots: list[LedgerSnapshot] = []
        self._positions: dict[str, BondStatic] = {}  # code -> 锁定条款
        self._faces: dict[str, float] = {}  # code -> 经济持仓面值（成交日生效）
        self._settled_faces: dict[str, float] = {}  # code -> 已结算/登记面值（结算日生效）
        self._coupon_schedules: dict[str, tuple[date, ...]] = {}
        self._applied: set[str] = set()
        self._settled: set[str] = set()
        # 非交易日票息/本金顺延后的应付日 -> [(kind, amount, bond_code, note)]
        self._receivables: dict[date, list[tuple[EventKind, float, str, str]]] = {}
        self._last_processed: date | None = None

    # ---- 只读状态 ------------------------------------------------------
    @property
    def cash(self) -> float:
        """已结算现金（元）。"""
        return self._cash

    @property
    def initial_cash(self) -> float:
        return self._initial_cash

    @property
    def funding_rate_pct(self) -> float:
        return self._funding_rate_pct

    def set_funding_rate(self, effective_date: date, rate_pct: float) -> None:
        """逐日设置融资利率（百分数）。

        未覆盖的自然日按 **≤ 该日的最近已知利率** 前向填充（历史 as-of 口径，
        不取未来值）；只有早于所有已知日期的自然日才回退构造时的基础利率
        ``funding_rate_pct``。因此本方法只影响 ``effective_date`` 及其之后
        未显式覆盖的日期，不会把周末/假期利率打回期初值。
        """
        day = _check_date(effective_date, field="effective_date")
        rate = float(rate_pct)
        if not math.isfinite(rate) or rate < 0:
            raise ValueError(f"rate_pct 必须是非负有限数，实际 {rate_pct!r}")
        self._funding_rates[day] = rate
        self._funding_dates_sorted = None  # 失效缓存

    def _rate_on(self, day: date) -> float:
        """``day`` 当日适用的融资利率：≤day 的最近已知值（前向填充），否则基础利率。"""
        if self._funding_dates_sorted is None:
            self._funding_dates_sorted = sorted(self._funding_rates)
        dates = self._funding_dates_sorted
        index = bisect.bisect_right(dates, day) - 1
        if index < 0:
            return self._funding_rate_pct
        return self._funding_rates[dates[index]]

    # ---- 只读投影（执行闸门用：统一状态推进，不另写近似账本）----------
    def clone(self) -> Ledger:
        """深拷贝账本（含全部内部状态），供 :meth:`simulate` 做只读预演。

        原账本不受任何影响；克隆体与原账本共享同一个 calendar/config 对象
        （只读使用）。
        """
        return copy.deepcopy(self)

    def _projection_copy(self) -> Ledger:
        """轻量投影副本：只复制**当前状态**（不含历史事件/快照/成交），O(状态) 而非 O(历史)。

        `clone()` 用于需要完整历史的测试；执行闸门每天调用 `simulate`，必须用这个轻量版本，
        否则深拷贝 480 天历史会让回测退化为 O(n²)。
        """
        other = Ledger.__new__(Ledger)
        other._calendar = self._calendar
        other._cfg = self._cfg
        other._initial_cash = self._initial_cash
        other._cash = self._cash
        other._funding_rate_pct = self._funding_rate_pct
        other._funding_rates = dict(self._funding_rates)
        other._funding_dates_sorted = (
            None if self._funding_dates_sorted is None else list(self._funding_dates_sorted)
        )
        other._trade_ids = set(self._trade_ids)
        other._known_bonds = dict(self._known_bonds)
        other._positions = dict(self._positions)
        other._faces = dict(self._faces)
        other._settled_faces = dict(self._settled_faces)
        other._coupon_schedules = dict(self._coupon_schedules)
        other._applied = set(self._applied)
        other._settled = set(self._settled)
        other._receivables = {day: list(items) for day, items in self._receivables.items()}
        other._last_processed = self._last_processed
        # 历史容器：成交引用保留（待结算成交的结算日推进需要），事件/快照不复制
        other._trades = list(self._trades)
        other._events = []
        other._snapshots = []
        return other

    @staticmethod
    def _curve_proxy(curve: Any, day: date) -> Any:
        """把给定曲线改日期成 ``day`` 的估值代理（价格不变，仅推进时间轴）。"""
        if getattr(curve, "as_of", None) == day:
            return curve
        if dataclasses.is_dataclass(curve):
            return dataclasses.replace(curve, as_of=day)
        raise TypeError(
            "curve 必须是 dataclass（如 ficc.curve.Curve）才能做投影日期代理；"
            f"实际 {type(curve).__name__}"
        )

    def simulate(
        self,
        as_of: date,
        curve: Any,
        *,
        extra_trades: tuple[TradeRecord, ...] = (),
        horizon: date | None = None,
        return_path: bool = False,
    ) -> LedgerSnapshot | tuple[LedgerSnapshot, ...]:
        """预演"推进今日已发生事件 + 加入候选成交 + 推进到检查时点"后的快照。

        - **不读未来**：克隆后把资金利率表截断到 ``<= as_of``，未来自然日只能
          前向填充最近已知利率；曲线用调用方给的 ``curve`` 作估值代理（价格不变）。
        - **统一状态推进**：直接复用 ``process_day`` 的自然日循环，结算队列、
          票息/本金（含顺延）、费用、融资全部按账本规则逐日推进，不另写近似公式。
        - ``horizon`` 默认 = ``max(as_of, 候选成交最大结算日的下一个交易日)``：
          覆盖 T+lag 结算以及结算后到下一个 NAV 检查点之间的周末/长假融资。
        - ``return_path=True`` 时返回**投影窗口内的逐日快照序列**（含 as_of 当日），
          供成交前闸门逐时点检查（不能只看最后一个快照，R37）。
        """
        as_of = _check_date(as_of, field="as_of")
        if self._last_processed is not None and as_of < self._last_processed:
            raise ValueError(f"simulate 的 as_of({as_of}) 早于已处理日({self._last_processed})")
        extra = tuple(extra_trades)
        for trade in extra:
            if not isinstance(trade, TradeRecord):
                raise TypeError("extra_trades 必须是 TradeRecord 序列")

        projection = self._projection_copy()
        projection._funding_rates = {
            day: rate for day, rate in projection._funding_rates.items() if day <= as_of
        }
        projection._funding_dates_sorted = None

        for trade in extra:
            if (
                projection._last_processed is not None
                and trade.trade_date <= projection._last_processed
            ):
                raise ValueError(
                    f"候选成交 {trade.trade_id} 的 trade_date({trade.trade_date}) "
                    f"不晚于已处理日({projection._last_processed})"
                )
            projection.submit(trade)

        settle_dates = [trade.settlement_date for trade in extra]
        default_horizon = as_of
        if settle_dates:
            latest_settle = max(settle_dates)
            default_horizon = max(as_of, self._calendar.next_trading_day(latest_settle, 1))
        target = _check_date(horizon or default_horizon, field="horizon")
        if target < as_of:
            raise ValueError(f"horizon({target}) 不能早于 as_of({as_of})")

        path: list[LedgerSnapshot] = []
        day = (
            as_of
            if projection._last_processed is None
            else projection._last_processed + timedelta(days=1)
        )
        while day <= target:
            path.append(projection.process_day(day, self._curve_proxy(curve, day)))
            day += timedelta(days=1)
        if not path:  # pragma: no cover - target >= as_of 时至少处理一天
            path.append(projection.process_day(target, self._curve_proxy(curve, target)))
        return tuple(path) if return_path else path[-1]

    # ---- 提交成交 ------------------------------------------------------
    def submit(self, trade: TradeRecord) -> None:
        """校验并登记一笔成交（尚未产生现金/持仓，等 ``process_day`` 生效）。"""
        if not isinstance(trade, TradeRecord):
            raise TypeError(f"trade 必须是 TradeRecord，实际 {type(trade).__name__}")
        if trade.trade_id in self._trade_ids:
            raise ValueError(f"trade_id 重复：{trade.trade_id!r}")
        if not (trade.signal_date < trade.trade_date):
            raise ValueError(
                f"{trade.trade_id}: 三层日期要求 signal_date < trade_date，"
                f"实际 {trade.signal_date} / {trade.trade_date}"
            )
        if trade.settlement_date < trade.trade_date:
            raise ValueError(
                f"{trade.trade_id}: settlement_date({trade.settlement_date}) 早于 "
                f"trade_date({trade.trade_date})"
            )
        if not self._calendar.is_trading_day(trade.trade_date):
            raise ValueError(
                f"{trade.trade_id}: 成交日 {trade.trade_date} 不是交易日（CN_IB）；"
                "成交必须在交易日执行"
            )
        lag = int(self._cfg.pricing.settlement_lag_days)
        expected = self._calendar.settlement_date(trade.trade_date, lag)
        if trade.settlement_date != expected:
            raise ValueError(
                f"{trade.trade_id}: 结算日 {trade.settlement_date} 与 "
                f"calendar.settlement_date({trade.trade_date}, T+{lag})={expected} 不一致"
            )
        if trade.trade_date >= trade.bond.maturity_date:
            raise ValueError(
                f"{trade.trade_id}: 成交日 {trade.trade_date} 不早于到期日 "
                f"{trade.bond.maturity_date}"
            )
        if trade.settlement_date >= trade.bond.maturity_date:
            raise ValueError(
                f"{trade.trade_id}: 结算日 {trade.settlement_date} 不早于到期日 "
                f"{trade.bond.maturity_date}（到期后不能交割）"
            )
        if self._last_processed is not None and trade.trade_date <= self._last_processed:
            raise ValueError(
                f"{trade.trade_id}: 成交日 {trade.trade_date} 不晚于已处理日 "
                f"{self._last_processed}，不能回溯建仓"
            )
        # 条款锁定：同一 bond code 的条款不得被后续成交改写
        locked = self._known_bonds.get(trade.bond.code)
        if locked is not None and locked != trade.bond:
            raise ValueError(
                f"{trade.trade_id}: 债券 {trade.bond.code} 条款与已锁定条款不一致"
                "（票息/频率/到期日/计息方式必须保持不变）"
            )
        self._known_bonds[trade.bond.code] = trade.bond
        self._trades.append(trade)
        self._trade_ids.add(trade.trade_id)

    # ---- 逐日处理 ------------------------------------------------------
    def process_day(self, as_of: date, curve: Any) -> LedgerSnapshot:
        """处理到 ``as_of``（含）并返回当日快照。

        ``curve`` 需提供 ``rate(t)``（百分数）；若暴露 ``as_of`` 则必须等于 ``as_of``。
        """
        as_of = _check_date(as_of, field="as_of")
        if not hasattr(curve, "rate"):
            raise TypeError("curve 缺少必需成员 rate(t)：需要 ficc.curve.Curve 或同接口对象")
        curve_as_of = getattr(curve, "as_of", None)
        if curve_as_of is not None and curve_as_of != as_of:
            raise ValueError(f"curve.as_of={curve_as_of} 与估值日 as_of={as_of} 不一致（日期契约）")
        if self._last_processed is not None and as_of <= self._last_processed:
            raise ValueError(
                f"process_day 必须按日期严格递增：上次 {self._last_processed}，本次 {as_of}"
            )

        start = as_of if self._last_processed is None else self._last_processed + timedelta(days=1)
        if self._last_processed is None:
            self._events.append(
                CashFlowEvent(
                    date=as_of,
                    kind="initial",
                    amount=self._initial_cash,
                    note="期初现金",
                )
            )
            # 首次处理：不允许存在早于起点的未生效成交
            for trade in self._trades:
                if trade.trade_date < start:
                    raise ValueError(
                        f"trade {trade.trade_id} 的 trade_date={trade.trade_date} 早于首次"
                        f"处理日 {as_of}；请从更早日期开始逐日 process_day"
                    )

        day = start
        funding_total = 0.0
        while day <= as_of:
            self._process_calendar_day(day)
            funding_total += self._accrue_funding(day)
            day += timedelta(days=1)
        span_days = (as_of - start).days + 1
        if funding_total != 0.0:
            # 融资按「处理日」合并入账，事件日期 = as_of（NAV 快照日）：
            # 对账侧要求窗口内全部内部现金流都能映射到 NAV 快照日。
            self._events.append(
                CashFlowEvent(
                    date=as_of,
                    kind="funding",
                    amount=funding_total,
                    note=f"融资成本 {start}→{as_of} 共 {span_days} 个自然日（365 基数，合并入账）",
                )
            )
        self._last_processed = as_of

        snapshot = self._build_snapshot(as_of, curve)
        self._snapshots.append(snapshot)
        return snapshot

    def _process_calendar_day(self, day: date) -> None:
        """处理单个日历日（按经济事件顺序）。

        顺序固定为：

        1. **日初登记权益快照**：``entitlement = dict(self._settled_faces)``——
           票息/本金归属只看付息日/到期日**日初**的已结算面值，因此当天成交或
           当天结算的买方不进入本日权益（R24）；
        2. 成交日：经济敞口生效（:attr:`_faces` 变化），现金尚未划转；
        3. 结算日：现金划转 + 费用，并更新 :attr:`_settled_faces`（买入增加、
           卖出减少，卖出超过已结算面值抛错）；同一结算日先处理买入再处理卖出
           （DVP 净额顺序），避免同券同日买卖的结算顺序依赖；
        4. 票息：用第 1 步的 ``entitlement`` 而非 :attr:`_faces` 计金额；
        5. 到期：本金按 ``entitlement`` 兑付并清空经济/登记持仓（末期票息已在上一步）；
        6. 顺延到账的票息/本金（非交易日付息/到期，见 :meth:`_pay_or_defer`）。
        """
        # 1) 日初登记权益快照：票息/本金归属 = 付息日/到期日日初的已结算面值
        entitlement = dict(self._settled_faces)
        # 2) 成交日：经济敞口生效（持仓变化），现金尚未划转
        for trade in self._trades:
            if trade.trade_date == day and trade.trade_id not in self._applied:
                self._apply_trade(trade)
        # 3) 结算日：现金划转 + 费用 + 已结算面值更新（先买入后卖出）
        for side in ("buy", "sell"):
            for trade in self._trades:
                if (
                    trade.settlement_date == day
                    and trade.trade_id not in self._settled
                    and trade.side == side
                ):
                    self._settle_trade(trade)
        # 4) 票息（付息日日初已结算面值；非交易日顺延支付）
        for code, schedule in self._coupon_schedules.items():
            if day not in schedule:
                continue
            face = entitlement.get(code, 0.0)
            if face <= 0.0:
                continue
            bond = self._known_bonds.get(code)
            if bond is None:
                continue
            amount = _coupon_amount(bond, face, day)
            if amount <= 0.0:
                continue
            self._pay_or_defer("coupon", amount, code, day, f"付息日 {day}")
        # 5) 到期：本金按到期日日初已结算面值兑付 + 持仓归零（末期票息已在上一步处理）
        for code, face in entitlement.items():
            if face <= 0.0:
                continue
            bond = self._known_bonds.get(code)
            if bond is None or day != bond.maturity_date:
                continue
            economic = self._faces.get(code, 0.0)
            if abs(economic - face) > 1e-9:
                raise RuntimeError(
                    f"{code}: 到期日 {day} 的经济持仓 {economic} 与登记权益 {face} 不一致；"
                    "结算日必须早于到期日（submit 已校验，内部状态异常）"
                )
            self._pay_or_defer("principal", face, code, day, f"到期日 {day}")
            self._faces.pop(code, None)
            self._positions.pop(code, None)
            self._settled_faces.pop(code, None)
        # 6) 顺延到账的票息/本金
        for kind, amount, code, note in self._receivables.pop(day, []):
            self._cash += amount
            self._events.append(
                CashFlowEvent(date=day, kind=kind, amount=amount, bond_code=code, note=note)
            )

    def _accrue_funding(self, day: date) -> float:
        """对 ``day`` 日终负现金计提一天融资（ACT/365，含周末/节假日）。

        利率取 ``≤ day 的最近已知值``（历史 as-of 前向填充，**不取未来值**）；
        早于所有已知日期才回退构造时的 ``funding_rate_pct``。返回当日利息（负值）；
        **不**写事件——由 :meth:`process_day` 统一合并入账（事件日期 = NAV 日）。
        """
        if self._cash >= 0.0:
            return 0.0
        rate = self._rate_on(day)
        interest = self._cash * rate / 100.0 / 365.0  # 负值
        if interest != 0.0:
            self._cash += interest
        return interest

    def _apply_trade(self, trade: TradeRecord) -> None:
        """成交日：更新**经济持仓**面值（现金与已结算/登记面值不动）。"""
        code = trade.bond.code
        locked = self._positions.get(code)
        if locked is not None and locked != trade.bond:
            raise ValueError(f"{trade.trade_id}: 债券 {code} 条款与持仓锁定条款不一致")
        if trade.side == "buy":
            self._positions.setdefault(code, trade.bond)
            self._faces[code] = self._faces.get(code, 0.0) + trade.face_value
            if code not in self._coupon_schedules:
                self._coupon_schedules[code] = tuple(coupon_dates(trade.bond)[1:])
        else:
            available = self._faces.get(code, 0.0)
            if trade.face_value > available + 1e-9:
                raise ValueError(
                    f"{trade.trade_id}: 卖出面值 {trade.face_value} 超过 {code} 可用持仓 "
                    f"{available}（禁止裸卖空）"
                )
            remaining = available - trade.face_value
            if remaining <= 1e-9:
                self._faces.pop(code, None)
                self._positions.pop(code, None)
            else:
                self._faces[code] = remaining
        self._applied.add(trade.trade_id)

    def _settle_trade(self, trade: TradeRecord) -> None:
        """结算日：现金划转 + 费用 + **已结算/登记面值**增减（R24）。

        已结算面值只在结算日变化：买入增加、卖出减少；卖出超过已结算面值直接
        抛错（登记权益不能卖空）。经济持仓面值由 :meth:`_apply_trade` 在成交日维护。
        """
        bond = trade.bond
        ai = accrued_interest(bond, trade.settlement_date, cfg=self._cfg)
        gross = trade.face_value / 100.0 * (trade.clean_price + ai)
        fee = self._trade_fee(trade)
        if trade.side == "buy":
            self._cash -= gross
            self._settled_faces[bond.code] = (
                self._settled_faces.get(bond.code, 0.0) + trade.face_value
            )
            self._events.append(
                CashFlowEvent(
                    date=trade.settlement_date,
                    kind="trade_buy",
                    amount=-gross,
                    bond_code=bond.code,
                    trade_id=trade.trade_id,
                    note=f"买入结算 {trade.face_value:g} 面值",
                )
            )
        else:
            available = self._settled_faces.get(bond.code, 0.0)
            if trade.face_value > available + 1e-9:
                raise ValueError(
                    f"{trade.trade_id}: 卖出结算面值 {trade.face_value} 超过 {bond.code} "
                    f"结算日日初已结算面值 {available}（登记权益不足，禁止卖空已结算面值）"
                )
            remaining = available - trade.face_value
            self._cash += gross
            if remaining <= 1e-9:
                self._settled_faces.pop(bond.code, None)
            else:
                self._settled_faces[bond.code] = remaining
            self._events.append(
                CashFlowEvent(
                    date=trade.settlement_date,
                    kind="trade_sell",
                    amount=gross,
                    bond_code=bond.code,
                    trade_id=trade.trade_id,
                    note=f"卖出结算 {trade.face_value:g} 面值",
                )
            )
        if fee > 0.0:
            self._cash -= fee
            self._events.append(
                CashFlowEvent(
                    date=trade.settlement_date,
                    kind="fee",
                    amount=-fee,
                    bond_code=bond.code,
                    trade_id=trade.trade_id,
                    note="手续费+结算费",
                )
            )
        self._settled.add(trade.trade_id)

    def _trade_fee(self, trade: TradeRecord) -> float:
        costs = self._cfg.costs
        notional = trade.face_value / 100.0 * trade.clean_price  # 净价成交金额
        return (
            float(costs.fee_bp) / 1e4 * notional
            + float(costs.settlement_fee_cny_per_trade)
            + float(trade.fee)
        )

    def _pay_or_defer(
        self, kind: EventKind, amount: float, bond_code: str, scheduled: date, note: str
    ) -> None:
        """交易日当日到账；非交易日顺延到下一交易日。"""
        if self._calendar.is_trading_day(scheduled):
            self._cash += amount
            self._events.append(
                CashFlowEvent(
                    date=scheduled, kind=kind, amount=amount, bond_code=bond_code, note=note
                )
            )
            return
        pay_date = self._calendar.next_trading_day(scheduled, 1)
        self._receivables.setdefault(pay_date, []).append(
            (kind, amount, bond_code, note + "（顺延）")
        )

    # ---- 快照 ----------------------------------------------------------
    def _build_snapshot(self, as_of: date, curve: Any) -> LedgerSnapshot:
        positions: list[PositionView] = []
        for code in sorted(self._faces):
            face = self._faces[code]
            if face <= 0.0:
                continue
            bond = self._positions[code]
            remaining = day_count(as_of, bond.maturity_date) / 365.0
            if remaining <= 0.0:
                raise ValueError(
                    f"{code}: as_of={as_of} 已到/超过到期日 {bond.maturity_date}，持仓应已兑付"
                )
            ytm = float(curve.rate(remaining))
            dirty = dirty_price_from_ytm(bond, as_of, ytm, cfg=self._cfg)
            ai = accrued_interest(bond, as_of, cfg=self._cfg)
            mod = analytics_mod_duration(bond, as_of, ytm, cfg=self._cfg)
            dv01_per_100 = analytics_dv01(bond, as_of, ytm, cfg=self._cfg)
            positions.append(
                PositionView(
                    bond=bond,
                    face_value=face,
                    remaining_years=remaining,
                    ytm_pct=ytm,
                    clean_price=dirty - ai,
                    accrued_interest=ai,
                    dirty_price=dirty,
                    market_value=face / 100.0 * dirty,
                    mod_duration=mod,
                    dv01=dv01_per_100 * face / 100.0,
                    settled_face=self._settled_faces.get(code, 0.0),
                )
            )
        pending = tuple(
            trade
            for trade in self._trades
            if trade.trade_id in self._applied and trade.trade_id not in self._settled
        )
        pending_cash = 0.0
        for trade in pending:
            ai = accrued_interest(trade.bond, trade.settlement_date, cfg=self._cfg)
            gross = trade.face_value / 100.0 * (trade.clean_price + ai)
            pending_cash += -gross if trade.side == "buy" else gross
        notes: list[str] = []
        if pending:
            notes.append(
                f"存在 {len(pending)} 笔未结算成交（pending_settlements），"
                f"pending_cash={pending_cash:.2f} 元"
            )
        # 经济持仓 vs 已结算/登记面值：未结算成交期间两者不同；票息/本金归属按
        # 付息日/到期日日初的已结算面值（R24）。只在确实不一致时提示，避免噪音。
        mismatches = sorted(
            (
                code,
                self._faces.get(code, 0.0),
                self._settled_faces.get(code, 0.0),
            )
            for code in set(self._faces) | set(self._settled_faces)
            if abs(self._faces.get(code, 0.0) - self._settled_faces.get(code, 0.0)) > 1e-9
        )
        if mismatches:
            shown = "；".join(
                f"{code} 经济{face:g}/登记{settled:g}" for code, face, settled in mismatches[:3]
            )
            notes.append(
                "经济持仓面值≠已结算/登记面值（票息/本金归属按付息日/到期日日初的"
                f"已结算面值）：{shown}"
            )
        outstanding = sum(
            amount for items in self._receivables.values() for _kind, amount, _code, _note in items
        )
        if outstanding:
            notes.append(f"应收顺延票息/本金 {outstanding:.2f} 元尚未到账")
        queued = [trade for trade in self._trades if trade.trade_date > as_of]
        if queued:
            notes.append(f"另有 {len(queued)} 笔未来成交尚未生效")
        market_value = sum(position.market_value for position in positions)
        nav = self._cash + market_value + pending_cash
        if not math.isclose(
            nav, self._cash + market_value + pending_cash, rel_tol=0.0, abs_tol=1e-9
        ):
            raise RuntimeError("NAV 恒等式校验失败（内部实现错误）")
        return LedgerSnapshot(
            as_of=as_of,
            cash=self._cash,
            pending_cash=pending_cash,
            positions=tuple(positions),
            nav=nav,
            pending_settlements=pending,
            notes=tuple(notes),
        )

    # ---- 导出（对账契约） ----------------------------------------------
    def trades_frame(self) -> pd.DataFrame:
        """成交明细（列名见模块 docstring 的契约）。"""
        rows: list[dict[str, Any]] = []
        for trade in self._trades:
            bond = trade.bond
            rows.append(
                {
                    "trade_id": trade.trade_id,
                    "bond_code": bond.code,
                    "side": trade.side,
                    "face_value": trade.face_value,
                    "clean_price": trade.clean_price,
                    "signal_date": trade.signal_date,
                    "trade_date": trade.trade_date,
                    "settlement_date": trade.settlement_date,
                    "fee": trade.fee,
                    "coupon_pct": bond.coupon_pct,
                    "freq": int(bond.freq),
                    "issue_date": bond.issue_date,
                    "maturity_date": bond.maturity_date,
                    "accrual": bond.accrual,
                    "market": bond.market,
                }
            )
        return pd.DataFrame(rows, columns=list(TRADES_COLUMNS))

    def cashflows_frame(self) -> pd.DataFrame:
        """现金事件（列名见模块 docstring 的契约）。"""
        rows = [
            {
                "date": event.date,
                "kind": event.kind,
                "amount": event.amount,
                "bond_code": event.bond_code,
                "trade_id": event.trade_id,
                "note": event.note,
            }
            for event in self._events
        ]
        return pd.DataFrame(rows, columns=list(CASHFLOWS_COLUMNS))

    def snapshots_frame(self) -> pd.DataFrame:
        """逐日快照长表；**独立重算 NAV** 并校验恒等式。"""
        rows: list[dict[str, Any]] = []
        for snapshot in self._snapshots:
            base: dict[str, Any] = {
                "as_of": snapshot.as_of,
                "cash": snapshot.cash,
                "pending_cash": snapshot.pending_cash,
                "nav": snapshot.nav,
                "n_positions": len(snapshot.positions),
                "n_pending": len(snapshot.pending_settlements),
                "notes": "; ".join(snapshot.notes),
            }
            if not snapshot.positions:
                rows.append(
                    {
                        **base,
                        "bond_code": None,
                        "face_value": float("nan"),
                        "settled_face": float("nan"),
                        "remaining_years": float("nan"),
                        "ytm_pct": float("nan"),
                        "clean_price": float("nan"),
                        "accrued_interest": float("nan"),
                        "dirty_price": float("nan"),
                        "market_value": 0.0,
                        "mod_duration": float("nan"),
                        "dv01": float("nan"),
                    }
                )
                continue
            for position in snapshot.positions:
                rows.append(
                    {
                        **base,
                        "bond_code": position.bond.code,
                        "face_value": position.face_value,
                        "settled_face": position.settled_face,
                        "remaining_years": position.remaining_years,
                        "ytm_pct": position.ytm_pct,
                        "clean_price": position.clean_price,
                        "accrued_interest": position.accrued_interest,
                        "dirty_price": position.dirty_price,
                        "market_value": position.market_value,
                        "mod_duration": position.mod_duration,
                        "dv01": position.dv01,
                    }
                )
        frame = pd.DataFrame(rows, columns=list(SNAPSHOTS_COLUMNS))
        self._verify_nav_identity(frame)
        return frame

    def _verify_nav_identity(self, frame: pd.DataFrame) -> None:
        """独立重算 ``nav = cash + Σ market_value + pending_cash``，与快照存储值比对。"""
        for snapshot in self._snapshots:
            mask = frame["as_of"] == snapshot.as_of
            values = frame.loc[mask, "market_value"].fillna(0.0).to_numpy(dtype=float)
            cash = float(frame.loc[mask, "cash"].iloc[0])
            pending_cash = float(frame.loc[mask, "pending_cash"].iloc[0])
            recomputed = cash + float(values.sum()) + pending_cash
            if not math.isclose(recomputed, snapshot.nav, rel_tol=1e-10, abs_tol=1e-8):
                raise RuntimeError(
                    f"NAV 恒等式失败 @ {snapshot.as_of}: 存储 {snapshot.nav} vs "
                    f"独立重算 {recomputed}"
                )

    def nav_series(self) -> pd.Series:
        """NAV 时间序列：index 名 ``as_of``、序列名 ``nav``。"""
        index = pd.Index([snapshot.as_of for snapshot in self._snapshots], name="as_of")
        values = [snapshot.nav for snapshot in self._snapshots]
        return pd.Series(values, index=index, name="nav", dtype=float)
