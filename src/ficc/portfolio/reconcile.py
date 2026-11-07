"""独立 PnL 对账（Phase 2 第 2 步）。

设计原则（评审 R14 / Phase 2）
------------------------------
对账必须走**独立于账本的实现路径**。只验证"各项相加 = total"是用恒等式自证，
不能证明归因正确；因此本模块的六项检查都从原始事件/快照**重新计算**：

1. :func:`check_cash_identity`：``cash_begin + Σ(事件金额) == cash_end``，
   按事件重算（不复用账本累计值），失败时列出差额附近/金额最大的事件；
2. :func:`check_position_identity`：``face_begin + Σbuy − Σsell == face_end``，
   另查卖出不超过持仓、signal≤trade、trade 与 settlement 日期符合交易日历；
3. :func:`check_nav_identity`：由快照 ``cash + Σ market_value`` 独立重算 NAV，
   并校验 ``market_value == face/100 × dirty_price``；
4. :func:`check_pnl_identity`：两条**互不依赖**的路径：
   - NAV 路径：逐日 ``ΔNAV − 外部现金流``；
   - MTM 路径：由持仓市值逐日变化 + 当日内部现金流（**不读 nav 序列**，
     见 :func:`mtm_daily_pnl`）。
   两路径的差即"未解释项"，必须 < 容差；外部现金流必须显式声明，
   窗口内出现 external 类事件却未声明时直接报错（防止把出入金当收益）；
5. :func:`check_full_price_identity`：``dirty_price == clean_price + accrued_interest``；
6. :func:`check_attribution_closure`：``Σ attribution.components − ledger_pnl``
   的**残差单独报告**（不吸收进任何分项），残差必须 < 容差；人为改动任一分项
   都应使检查失败（篡改检测，证明不是自证）。

数据契约（只消费纯数据，不 import Ledger）
------------------------------------------
- ``trades``: ``trade_id, bond_code, side, face_value, clean_price,
  signal_date, trade_date, settlement_date, fee``
- ``cashflows``: ``date, kind, amount, bond_code, trade_id, note``
  （``kind`` ∈ initial/trade_buy/trade_sell/coupon/principal/fee/funding/
  deposit/withdrawal/external；后四类视为外部现金流）
- ``snapshots``: 长表，``as_of, cash, nav`` + 每持仓一行
  ``bond_code, face_value, clean_price, accrued_interest, dirty_price,
  market_value``（可带 ``snapshot_id``）。无持仓的估值日允许一行占位
  （``bond_code`` 为 NaN，市值视为 0）。
- ``nav``: ``pd.Series``，index=日期，values=净值。
- ``attribution``: ``Series`` 或含 ``component``/``amount`` 的 DataFrame（长表），
  或一行宽表（数值列即分项）。

容差与边界
----------
六项检查默认容差均为 **1e-6（元）**；判定统一为 ``误差 <= 容差`` 通过
（恰好等于容差通过，略超失败）。可通过各函数的 ``tolerance=`` 覆盖。

单位：金额为元，面值为元，价格为"每 100 元面值"的价格，误差单位为元/价格点
（全价检查为价格点，仍用 1e-6 容差）。
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd

from ficc.utils.calendar import settlement_date as _calendar_settlement_date

__all__ = [
    "ATTRIBUTION_TOLERANCE",
    "CASH_TOLERANCE",
    "DEFAULT_TOLERANCE",
    "EXTERNAL_FLOW_KINDS",
    "FULL_PRICE_TOLERANCE",
    "NAV_TOLERANCE",
    "PNL_TOLERANCE",
    "POSITION_TOLERANCE",
    "CheckResult",
    "ReconciliationReport",
    "check_attribution_closure",
    "check_cash_identity",
    "check_full_price_identity",
    "check_nav_identity",
    "check_pnl_identity",
    "check_position_identity",
    "mtm_daily_pnl",
    "reconcile_all",
]

# 默认容差（元 / 价格点）
DEFAULT_TOLERANCE: float = 1e-6
CASH_TOLERANCE: float = DEFAULT_TOLERANCE
POSITION_TOLERANCE: float = DEFAULT_TOLERANCE
NAV_TOLERANCE: float = DEFAULT_TOLERANCE
PNL_TOLERANCE: float = DEFAULT_TOLERANCE
FULL_PRICE_TOLERANCE: float = DEFAULT_TOLERANCE
ATTRIBUTION_TOLERANCE: float = DEFAULT_TOLERANCE

# 现金流 kind 分类：外部现金流（出入金）必须显式声明后才能计入 PnL 扣除
EXTERNAL_FLOW_KINDS: frozenset[str] = frozenset(
    {"initial", "deposit", "withdrawal", "external", "external_flow", "capital", "distribution"}
)
INTERNAL_FLOW_KINDS: frozenset[str] = frozenset(
    {"trade_buy", "trade_sell", "coupon", "principal", "fee", "funding"}
)
ALLOWED_FLOW_KINDS: frozenset[str] = EXTERNAL_FLOW_KINDS | INTERNAL_FLOW_KINDS

CASHFLOW_COLUMNS: tuple[str, ...] = ("date", "kind", "amount")
TRADE_COLUMNS: tuple[str, ...] = (
    "trade_id",
    "bond_code",
    "side",
    "face_value",
    "trade_date",
    "settlement_date",
)
SNAPSHOT_COLUMNS: tuple[str, ...] = (
    "as_of",
    "cash",
    "nav",
    "market_value",
    "face_value",
    "dirty_price",
)
FULL_PRICE_COLUMNS: tuple[str, ...] = ("clean_price", "accrued_interest", "dirty_price")


# ---------------------------------------------------------------------------
# 结果类型
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CheckResult:
    """单项对账结果。

    ``max_abs_error`` 为该检查的独立重算误差；``tolerance`` 为判定阈值；
    判定规则固定为 ``max_abs_error <= tolerance``（容差边界含等号）。
    """

    name: str
    passed: bool
    detail: str
    max_abs_error: float | None = None
    tolerance: float | None = None


@dataclass(frozen=True)
class ReconciliationReport:
    """六项（或五项 + 归因）对账结果集合。"""

    checks: tuple[CheckResult, ...]

    @property
    def passed(self) -> bool:
        """所有检查均通过才为 True。"""
        return all(check.passed for check in self.checks)

    def to_frame(self) -> pd.DataFrame:
        """转为明细表（含 name/passed/max_abs_error/tolerance/detail）。"""
        return pd.DataFrame(
            [
                {
                    "name": check.name,
                    "passed": bool(check.passed),
                    "max_abs_error": check.max_abs_error,
                    "tolerance": check.tolerance,
                    "detail": check.detail,
                }
                for check in self.checks
            ]
        )

    def raise_if_failed(self) -> None:
        """任一项失败则抛出 ``ValueError``（附失败项与详情）。"""
        failed = [check for check in self.checks if not check.passed]
        if not failed:
            return
        lines = [f"对账失败（{len(failed)}/{len(self.checks)} 项）:"]
        lines.extend(f"- {check.name}: {check.detail}" for check in failed)
        raise ValueError("\n".join(lines))


# ---------------------------------------------------------------------------
# 通用校验/归一化工具（对账侧独立实现，不依赖账本内部累计值）
# ---------------------------------------------------------------------------
def _require_frame(frame: Any, name: str, columns: tuple[str, ...] | None = None) -> pd.DataFrame:
    if not isinstance(frame, pd.DataFrame):
        raise TypeError(f"{name} 必须是 DataFrame，实际 {type(frame).__name__}")
    if columns is not None:
        missing = [col for col in columns if col not in frame.columns]
        if missing:
            raise ValueError(f"{name} 缺少必需列 {missing}；实际列 {list(frame.columns)}")
    return frame


def _optional_float(value: Any, name: str) -> float:
    try:
        number = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} 不是数值: {value!r}") from exc
    return number


def _number(value: Any, name: str, *, allow_nan: bool = False) -> float:
    number = _optional_float(value, name)
    if not np.isfinite(number) and not allow_nan:
        raise ValueError(f"{name} 含 NaN/inf: {value!r}")
    return number


def _to_datetime_series(series: pd.Series, name: str) -> pd.Series:
    values = pd.to_datetime(series, errors="raise")
    if values.isna().any():
        raise ValueError(f"{name} 含无法解析的日期")
    return values


def _normalise_cashflows(cashflows: pd.DataFrame) -> pd.DataFrame:
    """校验并归一化现金流事件（不改变金额，不做累计）。"""
    frame = _require_frame(cashflows, "cashflows", CASHFLOW_COLUMNS).copy()
    frame["date"] = _to_datetime_series(frame["date"], "cashflows.date")
    frame["kind"] = frame["kind"].astype(str).str.strip().str.lower()
    unknown = sorted(set(frame["kind"]) - ALLOWED_FLOW_KINDS)
    if unknown:
        raise ValueError(f"cashflows.kind 含未知类型 {unknown}；允许 {sorted(ALLOWED_FLOW_KINDS)}")
    frame["amount"] = [
        _number(value, f"cashflows.amount[{index}]") for index, value in frame["amount"].items()
    ]
    return frame.sort_values(["date", "kind"], kind="stable").reset_index(drop=True)


def _external_events(cashflows: pd.DataFrame) -> pd.DataFrame:
    """外部现金流事件（必须由调用方显式声明才允许影响 PnL 扣除）。"""
    return cashflows[cashflows["kind"].isin(EXTERNAL_FLOW_KINDS)].copy()


def _external_flow_series(
    external_flows: Any,
    *,
    index: pd.DatetimeIndex,
    tolerance: float,
) -> pd.Series:
    """把 ``external_flows`` 归一化为按日期的 Series（升序、无重复日）。

    接受 ``Series``（index=日期）、``DataFrame(date, amount)`` 或 ``Mapping``。
    显式声明是唯一入口：未声明时不得静默把出入金当成收益。
    """
    if external_flows is None:
        return pd.Series(0.0, index=index, dtype=float)
    if isinstance(external_flows, pd.Series):
        series = external_flows.copy()
        series.index = pd.to_datetime(series.index, errors="raise")
    elif isinstance(external_flows, pd.DataFrame):
        frame = _require_frame(external_flows, "external_flows", ("date", "amount"))
        series = pd.Series(
            frame["amount"].to_numpy(dtype=float),
            index=pd.to_datetime(frame["date"], errors="raise"),
        )
    elif isinstance(external_flows, dict):
        series = pd.Series(
            {pd.Timestamp(key): float(value) for key, value in external_flows.items()}
        )
    else:
        raise TypeError(
            "external_flows 必须是 Series/DataFrame(date, amount)/dict，"
            f"实际 {type(external_flows).__name__}"
        )
    if series.empty:
        return pd.Series(0.0, index=index, dtype=float)
    series = pd.to_numeric(series, errors="raise").astype(float)
    if not np.all(np.isfinite(series.to_numpy())):
        raise ValueError("external_flows 含 NaN/inf")
    # 只保留窗口内的出入金；窗口外的资金已在 nav_begin 里，不能重复扣减
    outside = series.index.difference(index)
    if len(outside):
        raise ValueError(
            f"external_flows 含 NAV 快照日之外的日期 {[d.date().isoformat() for d in outside[:5]]}；"
            "无法对齐逐日 PnL，请显式放到快照日"
        )
    duplicated = series.groupby(level=0).sum()
    del tolerance  # 保留参数以便未来按容差聚合，当前严格按日期求和
    return duplicated.reindex(index).fillna(0.0)


def _positions_face(positions: Any, name: str) -> pd.Series:
    """从持仓帧/Series/dict 得到 ``bond_code → face_value``（组内求和）。"""
    if positions is None:
        return pd.Series(dtype=float)
    if isinstance(positions, pd.Series):
        series = positions.astype(float)
        series.index = series.index.astype(str)
        return series.groupby(level=0).sum()
    if isinstance(positions, dict):
        return pd.Series({str(k): float(v) for k, v in positions.items()}, dtype=float)
    frame = _require_frame(positions, name)
    if "bond_code" not in frame.columns or "face_value" not in frame.columns:
        raise ValueError(f"{name} 需要 bond_code/face_value 两列；实际 {list(frame.columns)}")
    frame = frame.dropna(subset=["bond_code"]).copy()
    frame["bond_code"] = frame["bond_code"].astype(str)
    frame["face_value"] = pd.to_numeric(frame["face_value"], errors="raise").astype(float)
    valid = frame.dropna(subset=["face_value"])
    return valid.groupby("bond_code", sort=True)["face_value"].sum()


def _market_value_by_date(snapshots: pd.DataFrame) -> pd.Series:
    """按估值日汇总持仓市值（无持仓的占位行计 0）。"""
    frame = _require_frame(snapshots, "snapshots", ("as_of", "market_value")).copy()
    frame["as_of"] = _to_datetime_series(frame["as_of"], "snapshots.as_of")
    mv = pd.to_numeric(frame["market_value"], errors="coerce")
    placeholder = frame.get("bond_code", pd.Series(np.nan, index=frame.index)).isna()
    if (~placeholder & mv.isna()).any():
        bad = frame.loc[~placeholder & mv.isna(), "as_of"].head(3)
        raise ValueError(
            f"snapshots.market_value 含 NaN（非占位行）: {[d.date().isoformat() for d in bad]}"
        )
    frame["_mv"] = mv.fillna(0.0)
    return frame.groupby("as_of")["_mv"].sum().sort_index()


def _nav_and_internal_events(
    cashflows: pd.DataFrame, nav_index: pd.DatetimeIndex
) -> tuple[pd.Series, pd.Series]:
    """内部现金流按快照日汇总；外部 kind 事件不计入（独立性来自事件分类）。"""
    frame = cashflows
    window = frame[(frame["date"] > nav_index[0]) & (frame["date"] <= nav_index[-1])]
    outside = window[~window["date"].isin(nav_index)]
    if len(outside):
        raise ValueError(
            "cashflows 含窗口内但不在 NAV 快照日的日期 "
            f"{[d.date().isoformat() for d in outside['date'].head(5)]}；"
            "无法与逐日 PnL 对齐"
        )
    internal = window[~window["kind"].isin(EXTERNAL_FLOW_KINDS)]
    daily = internal.groupby("date")["amount"].sum().reindex(nav_index).fillna(0.0)
    return daily.astype(float), internal


def _nav_series(nav: Any) -> pd.Series:
    if isinstance(nav, pd.DataFrame):
        if "nav" not in nav.columns:
            raise ValueError("nav 为 DataFrame 时必须含 nav 列")
        nav = nav["nav"]
    if not isinstance(nav, pd.Series):
        raise TypeError(f"nav 必须是 Series，实际 {type(nav).__name__}")
    series = pd.to_numeric(nav, errors="raise").astype(float)
    series.index = pd.to_datetime(series.index, errors="raise")
    if series.index.has_duplicates:
        raise ValueError("nav.index 含重复日期")
    series = series.sort_index()
    if series.empty:
        raise ValueError("nav 为空")
    if not np.all(np.isfinite(series.to_numpy())):
        raise ValueError("nav 含 NaN/inf")
    return series


# ---------------------------------------------------------------------------
# 1. 现金恒等式
# ---------------------------------------------------------------------------
def check_cash_identity(
    cashflows: pd.DataFrame,
    *,
    cash_begin: float,
    cash_end: float,
    tolerance: float | None = None,
) -> CheckResult:
    """``cash_begin + Σ(events.amount) == cash_end``（按事件重算）。

    失败时 detail 会列出金额最大的事件（date/kind/amount），便于定位。
    """
    tol = CASH_TOLERANCE if tolerance is None else float(tolerance)
    frame = _normalise_cashflows(cashflows)
    begin = _number(cash_begin, "cash_begin")
    end = _number(cash_end, "cash_end")
    total = float(frame["amount"].sum()) if len(frame) else 0.0
    recalculated = begin + total
    error = abs(recalculated - end)
    passed = error <= tol
    lines = [
        f"cash_begin={begin:.6f}, Σevents={total:.6f}（{len(frame)} 笔）, "
        f"重算 cash_end={recalculated:.6f}, 账面 cash_end={end:.6f}, 差额={recalculated - end:.6f}",
        f"判定：|差额|={error:.6g} {'<=' if passed else '>'} 容差 {tol:g}",
    ]
    if len(frame):
        # 失败时必须能定位到具体事件：列出全部事件（≤12 笔）或金额最大的 8 笔
        limit = 12 if len(frame) <= 12 else 8
        top = frame.loc[frame["amount"].abs().sort_values(ascending=False).index[:limit]]
        shown = "; ".join(
            f"{row['date'].date().isoformat()} {row['kind']} {row['amount']:+.6f}"
            for _, row in top.iterrows()
        )
        label = "全部事件" if len(frame) <= 12 else "金额最大的 8 笔事件"
        lines.append(f"{label}：{shown}")
    return CheckResult(
        name="cash_identity",
        passed=bool(passed),
        detail="；".join(lines),
        max_abs_error=error,
        tolerance=tol,
    )


# ---------------------------------------------------------------------------
# 2. 持仓恒等式
# ---------------------------------------------------------------------------
def check_position_identity(
    trades: pd.DataFrame,
    positions_begin: Any,
    positions_end: Any,
    *,
    tolerance: float | None = None,
    calendar: Any = None,
    settlement_lag_days: int = 1,
) -> CheckResult:
    """``face_begin + Σbuy − Σsell == face_end``，并核对卖出/日期一致性。

    - 卖出不得超过**当时**持仓（按 trade_date 顺序滚动）；
    - signal_date ≤ trade_date；
    - settlement_date == trade_date + ``settlement_lag_days`` 个交易日
      （提供 ``calendar``（TradingCalendar）时按其交易日历，否则按工作日规则）；
    - 交易日/结算日必须是交易日。
    """
    tol = POSITION_TOLERANCE if tolerance is None else float(tolerance)
    frame = _require_frame(trades, "trades", TRADE_COLUMNS).copy()
    begin = _positions_face(positions_begin, "positions_begin")
    end = _positions_face(positions_end, "positions_end")
    if len(frame):
        frame["side"] = frame["side"].astype(str).str.strip().str.lower()
        unknown = sorted(set(frame["side"]) - {"buy", "sell"})
        if unknown:
            raise ValueError(f"trades.side 只能是 buy/sell，实际含 {unknown}")
        frame["face_value"] = [
            _number(value, f"trades.face_value[{index}]")
            for index, value in frame["face_value"].items()
        ]
        if (frame["face_value"] <= 0).any():
            raise ValueError("trades.face_value 必须为正数")
        frame["trade_date"] = _to_datetime_series(frame["trade_date"], "trades.trade_date")
        frame["settlement_date"] = _to_datetime_series(
            frame["settlement_date"], "trades.settlement_date"
        )
        if "signal_date" in frame.columns:
            frame["signal_date"] = _to_datetime_series(frame["signal_date"], "trades.signal_date")
    frame = frame.sort_values(["trade_date", "trade_id"], kind="stable").reset_index(drop=True)

    violations: list[str] = []
    holdings: dict[str, float] = {str(code): float(face) for code, face in begin.items()}
    for _, row in frame.iterrows():
        code = str(row["bond_code"])
        face = float(row["face_value"])
        if row["side"] == "buy":
            holdings[code] = holdings.get(code, 0.0) + face
        else:
            current = holdings.get(code, 0.0)
            if face > current + tol:
                violations.append(
                    f"卖出超持仓 {code} {row['trade_date'].date().isoformat()} "
                    f"卖出{face:.2f} > 持有{current:.2f}"
                )
            holdings[code] = current - face
        # 日期一致性
        if (
            "signal_date" in row
            and pd.notna(row["signal_date"])
            and row["signal_date"] > row["trade_date"]
        ):
            violations.append(
                f"signal_date 晚于 trade_date: {row.get('trade_id')} "
                f"{row['signal_date'].date()} > {row['trade_date'].date()}"
            )
        trade_day = row["trade_date"].date()
        settle_day = row["settlement_date"].date()
        try:
            if calendar is not None:
                expected = calendar.settlement_date(trade_day, int(settlement_lag_days))
                trade_ok = calendar.is_trading_day(trade_day)
                settle_ok = calendar.is_trading_day(settle_day)
            else:
                expected = _calendar_settlement_date(trade_day, int(settlement_lag_days))
                trade_ok = trade_day.weekday() < 5
                settle_ok = settle_day.weekday() < 5
        except ValueError as exc:  # 日历覆盖范围外：明确记为无法核对
            violations.append(f"日历无法核对 {row.get('trade_id')}: {exc}")
            continue
        if not trade_ok:
            violations.append(f"trade_date 非交易日: {row.get('trade_id')} {trade_day}")
        if not settle_ok:
            violations.append(f"settlement_date 非交易日: {row.get('trade_id')} {settle_day}")
        if settle_day != expected:
            violations.append(
                f"结算日不符: {row.get('trade_id')} trade={trade_day} "
                f"settle={settle_day} 期望={expected}"
            )

    # 独立重算各券期末面值
    buy = frame[frame["side"] == "buy"].groupby(frame["bond_code"].astype(str))["face_value"].sum()
    sell = (
        frame[frame["side"] == "sell"].groupby(frame["bond_code"].astype(str))["face_value"].sum()
    )
    codes = sorted(set(begin.index) | set(end.index) | set(buy.index) | set(sell.index))
    errors: dict[str, float] = {}
    for code in codes:
        recalculated = (
            float(begin.get(code, 0.0)) + float(buy.get(code, 0.0)) - float(sell.get(code, 0.0))
        )
        errors[code] = abs(recalculated - float(end.get(code, 0.0)))
    face_error = max(errors.values()) if errors else 0.0
    passed = face_error <= tol and not violations
    worst = max(errors, key=errors.get) if errors else "-"
    lines = [
        f"期末面值独立重算（{len(codes)} 只券）：最大误差={face_error:.6f}（券 {worst}），"
        f"容差={tol:g}",
        f"卖出/日期校验：{'无异常' if not violations else str(len(violations)) + ' 项异常'}",
    ]
    if violations:
        lines.append("异常明细：" + "；".join(violations[:5]))
    return CheckResult(
        name="position_identity",
        passed=bool(passed),
        detail="；".join(lines),
        max_abs_error=face_error,
        tolerance=tol,
    )


# ---------------------------------------------------------------------------
# 3. NAV 恒等式
# ---------------------------------------------------------------------------
def _snapshot_groups(snapshots: pd.DataFrame) -> list[tuple[pd.Timestamp, pd.DataFrame]]:
    frame = _require_frame(snapshots, "snapshots", SNAPSHOT_COLUMNS).copy()
    frame["as_of"] = _to_datetime_series(frame["as_of"], "snapshots.as_of")
    if "snapshot_id" in frame.columns and frame["snapshot_id"].notna().any():
        frame["_group"] = frame["snapshot_id"].astype(str)
    else:
        frame["_group"] = frame["as_of"].dt.strftime("%Y-%m-%d")
    groups: list[tuple[pd.Timestamp, pd.DataFrame]] = []
    for _, block in frame.groupby("_group", sort=False):
        as_of = block["as_of"].min()
        groups.append((as_of, block))
    groups.sort(key=lambda item: item[0])
    seen: set[pd.Timestamp] = set()
    for as_of, _ in groups:
        if as_of in seen:
            raise ValueError(f"同一快照日出现多个 snapshot_id: {as_of.date()}")
        seen.add(as_of)
    return groups


def _pending_cash_by_date(snapshots: pd.DataFrame) -> pd.Series | None:
    """按快照日汇总 ``pending_cash``（trade-date 会计：买入为负、卖出为正）。

    列不存在返回 ``None``（旧版输入）；存在时同一快照日必须唯一（portfolio 级
    标量，长表行重复填充）。缺失值按 0 处理并在调用方 detail 中注明。
    """
    if "pending_cash" not in snapshots.columns:
        return None
    frame = snapshots.loc[:, ["as_of", "pending_cash"]].copy()
    frame["as_of"] = _to_datetime_series(frame["as_of"], "snapshots.as_of")
    frame["pending_cash"] = pd.to_numeric(frame["pending_cash"], errors="coerce")
    values: dict[pd.Timestamp, float] = {}
    for as_of, block in frame.groupby("as_of", sort=True):
        finite = block["pending_cash"].dropna()
        if finite.empty:
            values[as_of] = 0.0
        elif finite.nunique() > 1:
            raise ValueError(f"{as_of.date()} 快照内 pending_cash 不唯一: {sorted(set(finite))}")
        else:
            values[as_of] = float(finite.iloc[0])
    return pd.Series(values).sort_index()


def check_nav_identity(
    snapshots: pd.DataFrame,
    *,
    tolerance: float | None = None,
) -> CheckResult:
    """由快照 ``cash + Σ market_value`` 独立重算 NAV，并核对市值公式。"""
    tol = NAV_TOLERANCE if tolerance is None else float(tolerance)
    groups = _snapshot_groups(snapshots)
    if not groups:
        raise ValueError("snapshots 为空")
    pending_series = _pending_cash_by_date(snapshots)
    pending_note = (
        "旧版输入无 pending_cash，按 0 处理（未结算会计未覆盖）"
        if pending_series is None
        else "已按 trade-date 会计计入 pending_cash"
    )
    nav_errors: list[tuple[pd.Timestamp, float, float, float]] = []
    mv_errors: list[tuple[pd.Timestamp, str, float, float, float]] = []
    for as_of, block in groups:
        cash_values = pd.to_numeric(block["cash"], errors="raise").astype(float).unique()
        nav_values = pd.to_numeric(block["nav"], errors="raise").astype(float).unique()
        if cash_values.size != 1 or not np.isfinite(cash_values[0]):
            raise ValueError(f"{as_of.date()} 快照内 cash 不唯一/非有限: {cash_values}")
        if nav_values.size != 1 or not np.isfinite(nav_values[0]):
            raise ValueError(f"{as_of.date()} 快照内 nav 不唯一/非有限: {nav_values}")
        market_value = pd.to_numeric(block["market_value"], errors="coerce")
        face = pd.to_numeric(block["face_value"], errors="coerce")
        dirty = pd.to_numeric(block["dirty_price"], errors="coerce")
        placeholder = block.get("bond_code", pd.Series(np.nan, index=block.index)).isna()
        active = ~placeholder
        if active.any() and market_value[active].isna().any():
            raise ValueError(f"{as_of.date()} 存在非占位行但 market_value 为 NaN")
        mv_sum = float(market_value.fillna(0.0).sum())
        pending = float(pending_series.loc[as_of]) if pending_series is not None else 0.0
        recalculated = float(cash_values[0]) + mv_sum + pending
        nav_errors.append(
            (as_of, abs(recalculated - float(nav_values[0])), recalculated, float(nav_values[0]))
        )
        for _, row in block[active].iterrows():
            face_value = face.loc[row.name]
            dirty_price = dirty.loc[row.name]
            mv_value = market_value.loc[row.name]
            if (
                not np.isfinite(face_value)
                or not np.isfinite(dirty_price)
                or not np.isfinite(mv_value)
            ):
                raise ValueError(f"{as_of.date()} {row.get('bond_code')} 市值三要素含 NaN")
            expected = face_value / 100.0 * dirty_price
            mv_errors.append(
                (as_of, str(row.get("bond_code")), abs(expected - mv_value), expected, mv_value)
            )
    nav_error = max((item[1] for item in nav_errors), default=0.0)
    mv_error = max((item[2] for item in mv_errors), default=0.0)
    max_error = max(nav_error, mv_error)
    passed = max_error <= tol
    worst_nav = max(nav_errors, key=lambda item: item[1]) if nav_errors else None
    worst_mv = max(mv_errors, key=lambda item: item[2]) if mv_errors else None
    lines = [
        f"NAV 独立重算 {len(nav_errors)} 个快照日（cash + Σ市值 + pending_cash）："
        f"最大误差={nav_error:.6f}；"
        f"市值公式 face/100×dirty 校验 {len(mv_errors)} 行：最大误差={mv_error:.6f}；容差={tol:g}",
        pending_note,
    ]
    if worst_nav is not None and nav_error > 0:
        lines.append(
            f"NAV 最大误差日 {worst_nav[0].date()}: 重算={worst_nav[2]:.6f} vs 账面={worst_nav[3]:.6f}"
        )
    if worst_mv is not None and mv_error > 0:
        lines.append(
            f"市值最大误差 {worst_mv[0].date()} {worst_mv[1]}: "
            f"期望={worst_mv[3]:.6f} vs 记录={worst_mv[4]:.6f}"
        )
    return CheckResult(
        name="nav_identity",
        passed=bool(passed),
        detail="；".join(lines),
        max_abs_error=max_error,
        tolerance=tol,
    )


# ---------------------------------------------------------------------------
# 4. PnL 恒等式（两条独立路径 + 显式外部现金流）
# ---------------------------------------------------------------------------
def mtm_daily_pnl(
    snapshots: pd.DataFrame,
    cashflows: pd.DataFrame,
    *,
    tolerance: float = DEFAULT_TOLERANCE,
) -> pd.Series:
    """**MTM 路径**逐日 PnL：``ΔMV + Δpending_cash + 当日内部现金流``。

    - ``pending_cash`` 是 trade-date 会计的未结算款（买入为负、卖出为正）：
      成交日持仓已计入市值但现金未划转，故必须加 ``Δpending_cash`` 才能与
      NAV 路径对上；
    - 旧版快照无 ``pending_cash`` 列时按 0 处理（无未结算会计）；
    - 关键：本函数**不接收也不读取 nav 序列**，用于证明 PnL 对账不是"用 NAV 自证"。
    - 外部现金流（kind 属于 :data:`EXTERNAL_FLOW_KINDS`）不计入 PnL。
    """
    del tolerance  # 仅供调用方统一签名；本函数不做容差判定
    frame = _normalise_cashflows(cashflows)
    mv = _market_value_by_date(snapshots)
    dates = mv.index
    if len(dates) < 1:
        raise ValueError("snapshots 为空")
    internal_daily, _ = _nav_and_internal_events(frame, dates)
    delta_mv = mv.diff().iloc[1:]
    pending = _pending_cash_by_date(snapshots)
    if pending is None:
        delta_pending = pd.Series(0.0, index=dates[1:])
    else:
        delta_pending = pending.reindex(dates).diff().iloc[1:]
    daily = delta_mv + delta_pending + internal_daily.iloc[1:]
    daily.name = "mtm_daily_pnl"
    return daily


def _declared_external_series(
    external_flows: Any,
    *,
    nav_index: pd.DatetimeIndex,
    tolerance: float,
) -> tuple[pd.Series, float]:
    """返回按交易日的已声明外部现金流（仅窗口内）及窗口内总和。"""
    declared = _external_flow_series(external_flows, index=nav_index, tolerance=tolerance)
    window = declared.copy()
    window.iloc[0] = 0.0  # nav_begin 已包含首日之前的资金
    total = float(window.sum())
    return window, total


def check_pnl_identity(
    nav: pd.Series,
    cashflows: pd.DataFrame,
    *,
    external_flows: Any = None,
    snapshots: pd.DataFrame | None = None,
    tolerance: float | None = None,
) -> CheckResult:
    """PnL 恒等式：NAV 差分路径 vs MTM 路径互校，残差单独报告。

    Parameters
    ----------
    nav : pandas.Series
        NAV 序列（index=日期）。**只用于 NAV 路径**。
    cashflows : pandas.DataFrame
        现金流事件（含外部现金流时必须同时给 ``external_flows``）。
    external_flows : Series/DataFrame/dict | None
        显式声明的外部现金流；窗口内出现 external 类事件却未声明时抛
        ``ValueError``（防止把出入金当收益）。
    snapshots : pandas.DataFrame | None
        持仓快照（MTM 路径必需；``reconcile_all`` 总会传入）。为 ``None`` 时
        只能做 NAV 路径的自我一致性检查，detail 会明确标注"MTM 路径未执行"。
    tolerance : float | None
        默认 1e-6。

    Notes
    -----
    - NAV 路径：``ΔNAV_t − 外部现金流_t``；合计 = ``nav_end − nav_begin − Σexternal``。
    - MTM 路径：:func:`mtm_daily_pnl`（不读 NAV）。
    - 未解释项 = 两路径逐日差与合计差的较大值，必须 <= 容差。
    """
    tol = PNL_TOLERANCE if tolerance is None else float(tolerance)
    nav_series = _nav_series(nav)
    frame = _normalise_cashflows(cashflows)
    nav_index = nav_series.index
    external_window, external_total = _declared_external_series(
        external_flows, nav_index=nav_index, tolerance=tol
    )

    # 未声明外部现金流但窗口内存在 external 类事件 → 直接拒绝
    if external_flows is None:
        events = _external_events(frame)
        events = events[(events["date"] > nav_index[0]) & (events["date"] <= nav_index[-1])]
        if len(events) and abs(float(events["amount"].sum())) > tol:
            sample = "; ".join(
                f"{row['date'].date().isoformat()} {row['kind']} {row['amount']:+.6f}"
                for _, row in events.head(3).iterrows()
            )
            raise ValueError(
                "检测到窗口内外部现金流但未显式声明 external_flows，"
                "拒绝把它当成投资收益；请传入 external_flows。事件：" + sample
            )

    # 若账本把外部现金流也记进了 cashflows 的 external 类事件，则声明金额必须与之一致
    actual_external = _external_events(frame)
    actual_external = actual_external[
        (actual_external["date"] > nav_index[0]) & (actual_external["date"] <= nav_index[-1])
    ]
    if len(actual_external):
        actual_total = float(actual_external["amount"].sum())
        if abs(actual_total - external_total) > tol:
            return CheckResult(
                name="pnl_identity",
                passed=False,
                detail=(
                    f"声明外部现金流合计={external_total:.6f} 与 cashflows 中 external 类事件合计="
                    f"{actual_total:.6f} 不一致（差 {actual_total - external_total:.6f}）；"
                    "请检查 external_flows 声明或事件 kind"
                ),
                max_abs_error=abs(actual_total - external_total),
                tolerance=tol,
            )

    nav_daily = nav_series.diff().iloc[1:] - external_window.reindex(nav_index[1:]).fillna(0.0)
    total_nav = float(nav_series.iloc[-1] - nav_series.iloc[0] - external_total)

    if snapshots is None:
        detail = (
            f"NAV 路径合计={total_nav:.6f}（ΔNAV={float(nav_series.iloc[-1] - nav_series.iloc[0]):.6f}"
            f" − 外部现金流={external_total:.6f}）；"
            "未提供 snapshots，MTM 独立路径未执行；容差="
            f"{tol:g}"
        )
        return CheckResult(
            name="pnl_identity",
            passed=True,
            detail=detail,
            max_abs_error=None,
            tolerance=tol,
        )

    mtm_daily = mtm_daily_pnl(snapshots, frame)
    aligned_mtm = mtm_daily.reindex(nav_index[1:])
    if aligned_mtm.isna().any():
        raise ValueError("MTM 路径与 NAV 快照日无法对齐（存在缺失日期）")
    diff = (nav_daily - aligned_mtm).astype(float)
    daily_error = float(np.max(np.abs(diff.to_numpy()))) if len(diff) else 0.0
    total_mtm = float(aligned_mtm.sum())
    total_error = abs(total_nav - total_mtm)
    max_error = max(daily_error, total_error)
    passed = max_error <= tol
    worst_date = diff.abs().idxmax() if len(diff) else None
    pending_used = _pending_cash_by_date(snapshots) is not None
    lines = [
        f"NAV 路径合计={total_nav:.6f}；MTM 路径合计={total_mtm:.6f}；合计差={total_error:.6g}",
        "MTM 路径 = ΔMV + Δpending_cash + 内部现金流"
        if pending_used
        else "MTM 路径 = ΔMV + 内部现金流（旧版输入无 pending_cash）",
        f"逐日最大差={daily_error:.6g}"
        + (f"（{worst_date.date()}）" if worst_date is not None else ""),
        f"未解释项={max_error:.6g} {'<=' if passed else '>'} 容差 {tol:g}",
        f"外部现金流（已声明，窗口内合计）={external_total:.6f}；NAV 首末={float(nav_series.iloc[0]):.6f}→{float(nav_series.iloc[-1]):.6f}",
    ]
    return CheckResult(
        name="pnl_identity",
        passed=bool(passed),
        detail="；".join(lines),
        max_abs_error=max_error,
        tolerance=tol,
    )


# ---------------------------------------------------------------------------
# 5. 全价恒等式
# ---------------------------------------------------------------------------
def check_full_price_identity(
    positions: pd.DataFrame,
    *,
    tolerance: float | None = None,
) -> CheckResult:
    """``dirty_price == clean_price + accrued_interest``（逐行，占位行跳过）。"""
    tol = FULL_PRICE_TOLERANCE if tolerance is None else float(tolerance)
    frame = _require_frame(positions, "positions", FULL_PRICE_COLUMNS).copy()
    clean = pd.to_numeric(frame["clean_price"], errors="coerce")
    accrued = pd.to_numeric(frame["accrued_interest"], errors="coerce")
    dirty = pd.to_numeric(frame["dirty_price"], errors="coerce")
    placeholder = clean.isna() & accrued.isna() & dirty.isna()
    active = ~placeholder
    if active.any():
        if clean[active].isna().any() or accrued[active].isna().any() or dirty[active].isna().any():
            raise ValueError("positions 存在非占位行但 clean/AI/dirty 缺失")
        errors = (dirty[active] - (clean[active] + accrued[active])).abs()
        max_error = float(errors.max())
        worst_index = errors.idxmax()
        worst = frame.loc[worst_index]
        bond = worst.get("bond_code", "-")
        as_of = worst.get("as_of")
        where = f"{pd.Timestamp(as_of).date() if pd.notna(as_of) else '?'} {bond}"
    else:
        max_error = 0.0
        where = "-"
    passed = max_error <= tol
    detail = (
        f"全价=净价+应计校验 {int(active.sum())} 行：最大误差={max_error:.6f}"
        f"（{where}）；容差={tol:g}"
    )
    return CheckResult(
        name="full_price_identity",
        passed=bool(passed),
        detail=detail,
        max_abs_error=max_error,
        tolerance=tol,
    )


# ---------------------------------------------------------------------------
# 6. 归因闭合（残差单独报告）
# ---------------------------------------------------------------------------
def _attribution_total(attribution: Any) -> tuple[float, dict[str, float]]:
    """归一化归因分项 → (合计, {分项名: 金额})。"""
    if attribution is None:
        raise ValueError("attribution 不能为 None")
    if isinstance(attribution, pd.Series):
        values = pd.to_numeric(attribution, errors="raise").astype(float)
        components = {str(key): float(value) for key, value in values.items()}
    elif isinstance(attribution, pd.DataFrame):
        if "component" in attribution.columns:
            amount_col = next(
                (
                    col
                    for col in ("amount", "value", "contribution", "pnl")
                    if col in attribution.columns
                ),
                None,
            )
            if amount_col is None:
                raise ValueError("attribution 长表需要 amount/value/contribution 列")
            components = {
                str(row["component"]): float(row[amount_col]) for _, row in attribution.iterrows()
            }
        else:
            numeric = attribution.select_dtypes(include=[np.number])
            if numeric.shape[1] == 0:
                raise ValueError("attribution 宽表没有数值列")
            components = {}
            for column in numeric.columns:
                values = numeric[column].dropna()
                if len(values) > 1:
                    raise ValueError(f"attribution 宽表列 {column} 不应有多行")
                if len(values):
                    components[str(column)] = float(values.iloc[0])
    elif isinstance(attribution, dict):
        components = {str(key): float(value) for key, value in attribution.items()}
    else:
        raise TypeError(f"attribution 类型不支持: {type(attribution).__name__}")
    total = float(sum(components.values()))
    return total, components


def _ledger_pnl_total(ledger_pnl: Any) -> float:
    if isinstance(ledger_pnl, pd.Series):
        return float(pd.to_numeric(ledger_pnl, errors="raise").sum())
    if isinstance(ledger_pnl, pd.DataFrame):
        if "pnl" in ledger_pnl.columns:
            return float(pd.to_numeric(ledger_pnl["pnl"], errors="raise").sum())
        return float(pd.to_numeric(ledger_pnl.to_numpy().ravel(), errors="raise").sum())
    return float(ledger_pnl)


def check_attribution_closure(
    ledger_pnl: Any,
    attribution: Any,
    *,
    tolerance: float | None = None,
) -> CheckResult:
    """``Σ attribution.components − ledger_pnl`` 的残差**单独报告**并判定。"""
    tol = ATTRIBUTION_TOLERANCE if tolerance is None else float(tolerance)
    total, components = _attribution_total(attribution)
    ledger_total = _ledger_pnl_total(ledger_pnl)
    residual = total - ledger_total
    max_error = abs(residual)
    passed = max_error <= tol
    detail_items = ", ".join(f"{name}={value:+.6f}" for name, value in components.items())
    detail = (
        f"Σcomponents={total:.6f}（分项：{detail_items or '无'}）；"
        f"ledger_pnl={ledger_total:.6f}；残差（单独报告，不吸收）={residual:+.6f}；"
        f"判定：|残差|={max_error:.6g} {'<=' if passed else '>'} 容差 {tol:g}"
    )
    return CheckResult(
        name="attribution_closure",
        passed=bool(passed),
        detail=detail,
        max_abs_error=max_error,
        tolerance=tol,
    )


# ---------------------------------------------------------------------------
# 汇总
# ---------------------------------------------------------------------------
def _snapshot_positions(snapshots: pd.DataFrame, *, first: bool) -> pd.DataFrame:
    groups = _snapshot_groups(snapshots)
    _, block = groups[0] if first else groups[-1]
    columns = [col for col in ("bond_code", "face_value") if col in block.columns]
    if len(columns) < 2:
        raise ValueError("snapshots 缺少 bond_code/face_value，无法重算持仓")
    return block.loc[:, columns].copy()


def _snapshot_cash(snapshots: pd.DataFrame, *, first: bool) -> float:
    groups = _snapshot_groups(snapshots)
    _, block = groups[0] if first else groups[-1]
    values = pd.to_numeric(block["cash"], errors="raise").astype(float).unique()
    if values.size != 1:
        raise ValueError("快照内 cash 不唯一")
    return float(values[0])


def reconcile_all(
    *,
    cashflows: pd.DataFrame,
    trades: pd.DataFrame,
    snapshots: pd.DataFrame,
    nav: pd.Series,
    attribution: Any = None,
    external_flows: Any = None,
    tolerance: float | None = None,
    calendar: Any = None,
) -> ReconciliationReport:
    """执行完整对账（独立重算；残差单独报告）。

    与账本的对接约定：只消费 ``trades_frame()`` / ``cashflows_frame()`` /
    ``snapshots_frame()`` / ``nav_series()`` 的列，不 import Ledger 类。
    现金恒等式的窗口取 ``(首个快照日, 末个快照日]``：首日快照的 cash 已包含
    此前所有事件（含 initial 出资），因此只累加窗口内事件，避免重复计算。

    Parameters
    ----------
    attribution : Series/DataFrame/dict | None
        归因分项；为 ``None`` 时不生成 attribution 检查（其余五项仍执行）。
    external_flows : Series/DataFrame/dict | None
        显式外部现金流声明；未声明但窗口内出现 external 类事件会抛错。
    """
    base_tol = DEFAULT_TOLERANCE if tolerance is None else float(tolerance)
    cash = _normalise_cashflows(cashflows)
    snap = _require_frame(snapshots, "snapshots", SNAPSHOT_COLUMNS)
    trades_frame = _require_frame(trades, "trades", TRADE_COLUMNS)
    nav_series = _nav_series(nav)

    groups = _snapshot_groups(snap)
    first_date = groups[0][0]
    last_date = groups[-1][0]
    cash_begin = _snapshot_cash(snap, first=True)
    cash_end = _snapshot_cash(snap, first=False)
    window_events = cash[(cash["date"] > first_date) & (cash["date"] <= last_date)]

    checks: list[CheckResult] = []
    checks.append(
        check_cash_identity(
            window_events, cash_begin=cash_begin, cash_end=cash_end, tolerance=base_tol
        )
    )
    checks.append(
        check_position_identity(
            trades_frame,
            _snapshot_positions(snap, first=True),
            _snapshot_positions(snap, first=False),
            tolerance=base_tol,
            calendar=calendar,
        )
    )
    checks.append(check_nav_identity(snap, tolerance=base_tol))
    checks.append(
        check_pnl_identity(
            nav_series,
            cash,
            external_flows=external_flows,
            snapshots=snap,
            tolerance=base_tol,
        )
    )
    checks.append(check_full_price_identity(snap, tolerance=base_tol))
    if attribution is not None:
        ledger_pnl = float(
            nav_series.iloc[-1]
            - nav_series.iloc[0]
            - _declared_external_series(
                external_flows, nav_index=nav_series.index, tolerance=base_tol
            )[1]
        )
        checks.append(check_attribution_closure(ledger_pnl, attribution, tolerance=base_tol))
    return ReconciliationReport(checks=tuple(checks))


def _demo() -> None:  # pragma: no cover - 仅脚本自检
    """最小自检：构造空持仓三天的 NAV=cash 场景。"""
    dates = pd.to_datetime(["2024-06-03", "2024-06-04", "2024-06-05"])
    snapshots = pd.DataFrame(
        {
            "as_of": dates,
            "cash": 1_000_000.0,
            "nav": 1_000_000.0,
            "bond_code": np.nan,
            "face_value": 0.0,
            "clean_price": np.nan,
            "accrued_interest": np.nan,
            "dirty_price": np.nan,
            "market_value": 0.0,
        }
    )
    cashflows = pd.DataFrame(
        {
            "date": [dates[0]],
            "kind": ["initial"],
            "amount": [1_000_000.0],
            "bond_code": [None],
            "trade_id": [None],
            "note": ["initial capital"],
        }
    )
    report = reconcile_all(
        cashflows=cashflows,
        trades=pd.DataFrame(columns=TRADE_COLUMNS),
        snapshots=snapshots,
        nav=pd.Series([1_000_000.0] * 3, index=dates),
        attribution={"cash": 0.0},
    )
    print(report.to_frame().to_string(index=False))
    report.raise_if_failed()


if __name__ == "__main__":
    _demo()
