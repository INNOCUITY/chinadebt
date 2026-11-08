"""固定持仓账本归因（Phase 2 第 5 步，task-19）：**独立重估解释账本**。

本模块把账本产出的四件套（``trades_frame`` / ``cashflows_frame`` /
``snapshots_frame`` / ``nav_series``）与曲线面板 ``panel`` 变成一份可对账的
归因账本：先做**精确桥接**（NAV 差分恒等于分项之和，残差单独报告、不用残差
补平），再把价格变动拆成 carry/roll 与曲线因子（level/slope/curvature/other），
逐券与逐日都给出口径一致的金额。

只消费 frame，不 import ``Ledger``、引擎或策略；第 d 日只读 ≤ d 的曲线、快照、
现金流与成交。价差已经进入执行价（``clean_price`` 含半价差），本模块**不再
扣减价差**：执行与日终估值之间的差只作为 ``slippage`` 独立披露。

一、精确桥接（逐日，单位：元）
------------------------------
::

    ΔNAV = price_pnl + coupon_cash + principal_cash - funding - fees
           + trade_settlement + pending_delta + slippage + trade_entry

- ``price_pnl``：仅**期初持仓**（前一日快照 ``face_value > 0`` 的券）逐券
  ``begin_face × (end_dirty - begin_dirty) / 100`` 之和；
- ``coupon_cash`` / ``principal_cash``：当日 ``coupon`` / ``principal`` 现金流
  之和（正数）；
- ``funding`` / ``fees``：当日 ``funding`` / ``fee`` 现金流之和取负（成本为正）；
- ``trade_settlement``：当日 ``trade_buy`` / ``trade_sell`` 现金流之和
  （买入为负、卖出为正）；
- ``pending_delta = pending_cash_d - pending_cash_{d-1}``：trade-date 会计下的
  未结算款变动。快照没有 ``pending_cash`` 列时，在本模块内用 trades 的
  ``trade_date <= d < settlement_date`` 重建（买入为负、卖出为正），并用
  结算日应计利息按账本同口径计算金额；docstring 在此明示该回退路径；
- ``slippage = Σ_{trade_date=d} sign × face × (dirty_end - dirty_trade) / 100``：
  ``dirty_trade = clean_price + AI(settlement_date)``（与账本结算金额同口径），
  ``sign`` 买入 +1 / 卖出 −1。它只披露「执行价 → 日终估值」的差，买入/卖出
  价差已经包含在 ``clean_price`` 里，**不再二次扣减**；同一估值口径下理论为 0；
- ``trade_entry = Σ_{trade_date=d} sign × face × dirty_trade / 100``：成交日
  经济持仓立即进入 MV（``+sign × trade_entry``），而未结算现金只以
  ``pending_delta``（``-sign × trade_entry``）出现。**少了这一项，成交日桥接会
  差一个成交金额**（账本是 trade-date 会计：MV 已含新仓、现金还没划转）。
  在结算日 ``trade_settlement + pending_delta`` 正好抵消，因此窗口合计仍等于
  账本 PnL；``trade_entry`` 是纯平衡项，不是残差补平。

逐日残差 ``bridge_residual`` 必须 ≤ ``1e-6 × max(1, |NAV_{d-1}|)``（测试验收），
同时逐日报告 ``residual_bp = bridge_residual / max(1, |NAV_{d-1}|) × 1e4``。
**归因不得用残差补平**：所有分项都是独立重估/独立现金流求和的结果。

估值日口径（必须明示）
----------------------
账本快照的 ``dirty_price`` 用**快照日 as_of** 估值（``Ledger._build_snapshot``：
``remaining = day_count(as_of, maturity)/365``）。因此本模块的 carry/因子重估
一律用**快照日 d 作为估值日**、用 ``remaining_years(d)`` 取曲线 YTM；``d`` 的
结算日（T+N）只作为信息列与 ``dirty_trade`` 的应计口径，不用于重新估值——否则
carry 与快照全价会差一个 T+N 的 carry，B 节恒等式无法精确成立。

二、价格变动拆分（逐券，carry + roll-down）
-------------------------------------------
对每只期初持仓券（同一张券、条款锁定、按原券老化）：

- ``dirty_roll``：用 **d-1 日曲线**在**估值日 d** 的剩余期限处重估全价——
  「同一曲线、时间前进」即 carry + roll-down；
- ``carry_roll = begin_face × (dirty_roll - begin_dirty) / 100``；
- ``curve_pnl = price_pnl - carry_roll``。两条路径都由独立重估给出：
  ``curve_pnl`` 也等于 ``begin_face × (end_dirty - dirty_roll) / 100``（逐日校验）。

逐日 ``Σ_bonds(carry_roll + curve_pnl) == price_pnl``（相对 1e-9 校验）。

三、曲线变动拆分（level / slope / curvature / other，固定约定）
--------------------------------------------------------------
以曲线面板的 **11 个标准期限日变化** ``Δy`` 定义可观测因子：

- ``Δlevel = Δy_10Y``（平行移动近似）；另用全期限等权均值做交叉校验，
  在 ``factors.level_mean_bp`` 与 ``level_mean_diff_bp = Δlevel - mean`` 报告两者差；
- ``Δslope = Δ(y_10Y - y_2Y)``；
- ``Δcurv = 2 × Δy_5Y - Δy_2Y - Δy_10Y``。

顺序重估（telescoping）使用上述因子的**单位响应对偶基**（写死为本模块约定）：

============ ==========================================================
步骤          单位响应形状（bp / 单位因子；节点间线性插值、两端平推）
============ ==========================================================
level         所有期限 +1bp
slope         2Y −1bp、10Y 0bp
curvature     2Y 0bp、5Y +0.5bp、10Y 0bp
============ ==========================================================

每步金额分别取 ``Δlevel / Δslope / Δcurv``。这样每一步对**观测因子**的改变量
精确等于该步金额（slope 一步的 ``Δ(y10−y2) = Δslope``；curvature 一步的
``2Δy5−Δy2−Δy10 = Δcurv``），并且不会把已经对齐的 10Y/2Y/5Y 再移动一次。

> 书面规格里的 slope（2Y −0.5、10Y +0.5）与 curvature（2Y +0.25、5Y −0.5、
> 10Y +0.25）形状系数与上述观测因子定义**不是对偶基**：若直接按 Δ 金额叠加，
> slope 步会把 10Y 再移动 0.5·Δslope（curvature 步会移动 2Y/10Y），造成
> level/slope 交叉污染。本模块改用上表的单位响应对偶基，效果等价于「把形状
> 归一化到观测因子变化 = 1，并保持 10Y / 2Y / 5Y 的先前对齐」。

最终「重建曲线」与 **真实 curve_d** 之间的差异（其余 8 个期限节点 + 插值
差异）整体作为 ``other_curve`` 重估披露（金额 + 占 NAV bp），**不**并入
``bridge_residual``，也不叫 residual。逐日
``level + slope + curvature + other_curve == curve_pnl``（相对 1e-9 校验）。

四、逐券/逐日输出与基准
------------------------
- ``daily``：桥接分项（见上）；
- ``factors``：``carry_roll``、四个曲线因子、``curve_pnl``、``price_pnl`` 与
  因子日变化诊断（bp）；
- ``bonds``：**期初持仓券**的逐券分解（新开仓当日不出现在 ``bonds``，其
  入账价值由 ``daily.trade_entry`` / ``slippage`` 承担；已到期待偿券
  ``end_dirty = 0``，其价格贡献由 ``other_curve`` 承接，见下）；
- ``residual_bp``：每日桥接残差占期初 NAV 的 bp；
- ``benchmark``：可选的基准归因结果，仅挂载（本函数不自动做相对归因，
  调用方可用两份 ``daily`` / ``factors`` 相减得到主动归因）。

到期日约定：到期日经济持仓已兑付，快照无该券；``end_dirty = 0``（本金现金流
另计），``carry_roll = level = slope = curvature = 0``，价格贡献全部进
``other_curve``。该约定只影响到期日当天，且保持桥接与逐券恒等式精确。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any

import numpy as np
import pandas as pd

from ficc.config import Config, get_config
from ficc.curve.curve import Curve, CurveMethod
from ficc.data.schema import BondStatic
from ficc.pricing.bond import dirty_price_from_ytm
from ficc.pricing.conventions import accrued_interest, day_count

__all__ = [
    "BONDS_COLUMNS",
    "DAILY_COLUMNS",
    "FACTORS_COLUMNS",
    "AttributionResult",
    "attribute_fixed_holdings",
]

# 输出列契约（列名即对账/报告契约；新增列只追加在末尾）。
DAILY_COLUMNS: tuple[str, ...] = (
    "date",
    "nav",
    "nav_delta",
    "price_pnl",
    "coupon_cash",
    "principal_cash",
    "funding",
    "fees",
    "trade_settlement",
    "pending_delta",
    "slippage",
    "bridge_residual",
    "trade_entry",
    "settlement_date",
    "model_price_pnl",
    "valuation_basis",
    "valuation_basis_nav_bp",
    "valuation_basis_breach",
    "principal_redemption",
)
FACTORS_COLUMNS: tuple[str, ...] = (
    "date",
    "carry_roll",
    "level",
    "slope",
    "curvature",
    "other_curve",
    "curve_pnl",
    "price_pnl",
    "level_bp",
    "level_mean_bp",
    "level_mean_diff_bp",
    "slope_bp",
    "curvature_bp",
    "other_curve_nav_bp",
    "snapshot_price_pnl",
    "valuation_basis",
)
BONDS_COLUMNS: tuple[str, ...] = (
    "date",
    "bond_code",
    "begin_face",
    "end_face",
    "begin_dirty",
    "end_dirty",
    "price_pnl",
    "carry_roll",
    "curve_pnl",
    "coupon_cash",
    "level",
    "slope",
    "curvature",
    "other_curve",
    "begin_ytm_pct",
    "remaining_years",
    "model_dirty_begin",
    "model_dirty_end",
    "valuation_basis",
)

# 因子节点（年）与桥接容差。
_LEVEL_TENOR = 10.0
_SLOPE_SHORT_TENOR = 2.0
_SLOPE_LONG_TENOR = 10.0
_CURV_SHORT_TENOR = 2.0
_CURV_BELLY_TENOR = 5.0
_CURV_LONG_TENOR = 10.0
_BRIDGE_ABS_TOL = 1e-6
# 独立估值检查容差：|snapshot−model| 超过 max(1 元, NAV×1e-8) 视为估值基差异常（R34）
VALUATION_ABS_TOL: float = 1.0
VALUATION_REL_NAV_TOL: float = 1e-8
_IDENTITY_REL_TOL = 1e-9
# 面板曲线的默认插值方法：与回测引擎 ``backtest.engine._curve_at``（CN_IB 面板
# 11 节点 + hermite）保持同一口径；账本若用其他插值，调用方显式传 curve_method。
_DEFAULT_CURVE_METHOD: CurveMethod = "hermite"
_KNOWN_KINDS = frozenset(
    {"initial", "trade_buy", "trade_sell", "coupon", "principal", "fee", "funding"}
)


# ---------------------------------------------------------------------------
# 结果对象
# ---------------------------------------------------------------------------
@dataclass
class AttributionResult:
    """一次固定持仓归因的完整结果。

    Attributes
    ----------
    strategy : str
        账本/策略名（基准可用 ``"benchmark"``）。
    daily : DataFrame
        逐日桥接分项，列见 :data:`DAILY_COLUMNS`。
    factors : DataFrame
        逐日价格/曲线因子分解，列见 :data:`FACTORS_COLUMNS`。
    bonds : DataFrame
        逐日期初持仓券分解，列见 :data:`BONDS_COLUMNS`。
    residual_bp : Series
        每日 ``bridge_residual / max(1, |NAV_{d-1}|) × 1e4``（bp），index 名
        ``date``。残差单独报告，绝不回填到任何分项。
    benchmark : AttributionResult | None
        可选基准归因结果，仅挂载；相对归因由调用方对两份结果相减。
    """

    strategy: str
    daily: pd.DataFrame
    factors: pd.DataFrame
    bonds: pd.DataFrame
    residual_bp: pd.Series
    benchmark: AttributionResult | None = None


# ---------------------------------------------------------------------------
# 输入规范化
# ---------------------------------------------------------------------------
def _as_date(value: Any, *, field: str = "date") -> date:
    """把 date/datetime/Timestamp/np.datetime64/ISO 字符串统一成 ``datetime.date``。"""
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, np.datetime64):
        return pd.Timestamp(value).date()
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError as exc:  # pragma: no cover - 取决于外部输入
            raise ValueError(f"{field} 无法解析为 ISO 日期: {value!r}") from exc
    raise TypeError(f"{field} 必须是 datetime.date，实际 {type(value).__name__}")


def _require_columns(frame: pd.DataFrame, columns: tuple[str, ...], name: str) -> None:
    missing = [column for column in columns if column not in frame.columns]
    if missing:
        raise ValueError(f"{name} 缺少必需列 {missing}；实际列={list(frame.columns)}")


def _normalise_nav(nav: pd.Series) -> pd.Series:
    if not isinstance(nav, pd.Series):
        raise TypeError(f"nav 必须是 pd.Series，实际 {type(nav).__name__}")
    if nav.empty:
        raise ValueError("nav 不能为空")
    values = pd.to_numeric(nav, errors="raise").to_numpy(dtype=float)
    if not np.isfinite(values).all():
        raise ValueError("nav 含 NaN/inf")
    index = pd.Index([_as_date(value, field="nav.index") for value in nav.index], name="date")
    series = pd.Series(values, index=index, name=nav.name or "nav", dtype=float)
    if series.index.has_duplicates:
        raise ValueError("nav 索引日期重复")
    if not series.index.is_monotonic_increasing:
        series = series.sort_index()
    return series


def _normalise_snapshots(snapshots: pd.DataFrame) -> pd.DataFrame:
    _require_columns(
        snapshots,
        ("as_of", "cash", "nav", "bond_code", "face_value", "dirty_price"),
        "snapshots",
    )
    frame = snapshots.copy()
    frame["as_of"] = [_as_date(value, field="snapshots.as_of") for value in frame["as_of"]]
    frame = frame.sort_values("as_of", kind="stable").reset_index(drop=True)
    return frame


def _normalise_cashflows(cashflows: pd.DataFrame) -> pd.DataFrame:
    _require_columns(cashflows, ("date", "kind", "amount"), "cashflows")
    frame = cashflows.copy()
    frame["date"] = [_as_date(value, field="cashflows.date") for value in frame["date"]]
    frame["kind"] = frame["kind"].astype(str)
    frame["amount"] = pd.to_numeric(frame["amount"], errors="raise").astype(float)
    unknown = set(frame["kind"]) - _KNOWN_KINDS
    if unknown:
        raise ValueError(f"cashflows 含未知 kind {sorted(unknown)}；已知={sorted(_KNOWN_KINDS)}")
    return frame


def _normalise_panel(panel: pd.DataFrame) -> pd.DataFrame:
    if not isinstance(panel, pd.DataFrame):
        raise TypeError(f"panel 必须是 pd.DataFrame，实际 {type(panel).__name__}")
    if panel.empty:
        raise ValueError("panel 不能为空")
    frame = panel.copy()
    frame.index = pd.Index(
        [_as_date(value, field="panel.index") for value in frame.index], name="date"
    )
    if frame.index.has_duplicates:
        raise ValueError("panel 索引日期重复")
    try:
        tenors = [float(column) for column in frame.columns]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"panel 列名必须是数值期限（年）: {list(frame.columns)}") from exc
    if any(not np.isfinite(tenor) or tenor <= 0.0 for tenor in tenors):
        raise ValueError(f"panel 期限必须为正有限数，实际 {tenors}")
    frame.columns = pd.Index(tenors, name="tenor")
    frame = frame.loc[:, ~frame.columns.duplicated(keep="first")]
    frame = frame.sort_index()
    frame = frame.reindex(sorted(frame.columns), axis=1)
    for required in (_SLOPE_SHORT_TENOR, _CURV_BELLY_TENOR, _LEVEL_TENOR):
        if not np.any(np.isclose(frame.columns.to_numpy(dtype=float), required)):
            raise ValueError(
                f"panel 缺少 {required:g}Y 期限列（因子定义需要 2Y/5Y/10Y）；"
                f"实际列={list(frame.columns)}"
            )
    return frame


# ---------------------------------------------------------------------------
# 交易视图（独立于 Ledger 重建结算金额）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _TradeView:
    """一笔成交的独立视图：条款 + 成交价 + 结算日全价成交金额（元）。"""

    trade_id: str
    code: str
    side: str
    face: float
    clean: float
    trade_date: date
    settlement_date: date
    bond: BondStatic
    dirty_trade: float
    gross: float


def _trade_views(
    trades: pd.DataFrame, cfg: Config
) -> tuple[dict[str, BondStatic], list[_TradeView]]:
    """把 ``trades_frame`` 变成独立视图，并锁定每只券的条款。"""
    _require_columns(
        trades,
        (
            "trade_id",
            "bond_code",
            "side",
            "face_value",
            "clean_price",
            "trade_date",
            "settlement_date",
            "coupon_pct",
            "freq",
            "issue_date",
            "maturity_date",
        ),
        "trades",
    )
    # 按成交日排序：条款由该券**最早一笔成交**锁定（账本保证同券条款不变；
    # 任何持仓日在快照里的券，其建仓成交必然 ≤ 该日，因此这是因果口径）。
    frame = trades.assign(
        trade_date=[_as_date(value, field="trades.trade_date") for value in trades["trade_date"]]
    ).sort_values("trade_date", kind="stable")
    statics: dict[str, BondStatic] = {}
    views: list[_TradeView] = []
    for _, row in frame.iterrows():
        code = str(row["bond_code"])
        bond = statics.get(code)
        if bond is None:
            accrual = row["accrual"] if "accrual" in frame.columns else "coupon"
            market = row["market"] if "market" in frame.columns else "IB"
            bond = BondStatic(
                code=code,
                issue_date=_as_date(row["issue_date"], field="trades.issue_date"),
                maturity_date=_as_date(row["maturity_date"], field="trades.maturity_date"),
                coupon_pct=float(row["coupon_pct"]),
                freq=int(row["freq"]),
                accrual=str(accrual) if pd.notna(accrual) else "coupon",
                market=str(market) if pd.notna(market) else "IB",
            )
            statics[code] = bond
        side = str(row["side"])
        if side not in ("buy", "sell"):
            raise ValueError(f"trades.side 必须是 'buy'/'sell'，实际 {side!r}")
        settlement = _as_date(row["settlement_date"], field="trades.settlement_date")
        dirty_trade = float(row["clean_price"]) + accrued_interest(bond, settlement, cfg=cfg)
        gross = float(row["face_value"]) / 100.0 * dirty_trade
        views.append(
            _TradeView(
                trade_id=str(row["trade_id"]),
                code=code,
                side=side,
                face=float(row["face_value"]),
                clean=float(row["clean_price"]),
                trade_date=_as_date(row["trade_date"], field="trades.trade_date"),
                settlement_date=settlement,
                bond=bond,
                dirty_trade=dirty_trade,
                gross=gross,
            )
        )
    views.sort(key=lambda view: (view.trade_date, view.trade_id))
    return statics, views


def _pending_from_trades(views: list[_TradeView], day: date) -> float:
    """按 trades 重建 ``day`` 日终的 ``pending_cash``（买入为负、卖出为正）。

    只统计已生效（``trade_date <= day``）且尚未结算（``settlement_date > day``）的
    成交；金额与账本同口径：``face/100 × (clean_price + AI(settlement_date))``。
    """
    total = 0.0
    for view in views:
        if view.trade_date <= day < view.settlement_date:
            total += -view.gross if view.side == "buy" else view.gross
    return total


# ---------------------------------------------------------------------------
# 曲线
# ---------------------------------------------------------------------------
def _curve(
    as_of: date, tenors: np.ndarray, rates: np.ndarray, method: CurveMethod = _DEFAULT_CURVE_METHOD
) -> Curve:
    return Curve(as_of=as_of, tenors=tenors.copy(), rates=rates.copy(), method=method)


def _panel_curve(
    day: date,
    tenors: np.ndarray,
    panel_frame: pd.DataFrame,
    method: CurveMethod,
) -> Curve:
    """按需取 ``panel`` 行构建曲线（d 日只读 d−1 与 d，绝不预读未来行）。"""
    if day not in panel_frame.index:
        raise ValueError(f"panel 缺少 {day} 的曲线（归因只用 ≤d 的曲线，d−1 与 d 都必须有）")
    rates = np.asarray(panel_frame.loc[day].to_numpy(dtype=float), dtype=float)
    if not np.isfinite(rates).all():
        raise ValueError(f"panel[{day}] 含 NaN/inf：{rates!r}")
    return _curve(day, tenors, rates, method)


def _tenor_index(tenors: np.ndarray, target: float) -> int:
    hits = np.flatnonzero(np.isclose(tenors, target))
    if len(hits) != 1:
        raise ValueError(f"panel 期限列中 {target:g}Y 不唯一/缺失（命中 {len(hits)} 个）")
    return int(hits[0])


# 因子节点（2Y/5Y/10Y）上的观测映射 F：rows = level/slope/curvature。
# - level  = Δy10
# - slope  = Δy10 − Δy2
# - curv   = 2Δy5 − Δy2 − Δy10
# 注意：这里 `level=Δy10` 与项目可观测因子 `level_all`（全期限等权）是不同口径，
# 仅用于归因分类，不能与因子研究里的 level_all 混用。
FACTOR_NODES: tuple[float, ...] = (2.0, 5.0, 10.0)
FACTOR_MATRIX: np.ndarray = np.array(
    [[0.0, 0.0, 1.0], [-1.0, 0.0, 1.0], [-1.0, 2.0, -1.0]], dtype=float
)
# 形状基 B = F^{-1}：单位因子冲击在各节点的 Δy；其余期限按节点值线性插值、两端平推。
BASIS_MATRIX: np.ndarray = np.linalg.inv(FACTOR_MATRIX)


def factor_shapes(tenors: np.ndarray) -> np.ndarray:
    """返回形状矩阵 ``(3, n_tenors)``：B 的三列插值到整条曲线。"""
    tenors_arr = np.asarray(tenors, dtype=float)
    matrix = np.vstack(
        [
            np.interp(tenors_arr, np.asarray(FACTOR_NODES), BASIS_MATRIX[:, column])
            for column in range(3)
        ]
    )
    nodal = np.vstack(
        [np.interp(np.asarray(FACTOR_NODES), tenors_arr, matrix[column]) for column in range(3)]
    ).T  # 每列是一个形状向量
    response = FACTOR_MATRIX @ nodal
    if not np.allclose(response, np.eye(3), atol=1e-12):
        raise RuntimeError(f"因子基不满足 F@B=I：\n{response}")
    return matrix


# ---------------------------------------------------------------------------
# 快照状态
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class _SnapshotState:
    """某一 ``as_of`` 的快照汇总（现金/NAV/pending + 逐券行）。"""

    cash: float
    nav: float
    pending: float | None
    rows: dict[str, dict[str, Any]]


def _snapshot_states(snapshot_frame: pd.DataFrame) -> dict[date, _SnapshotState]:
    states: dict[date, _SnapshotState] = {}
    for as_of, group in snapshot_frame.groupby("as_of", sort=True):
        first = group.iloc[0]
        pending: float | None = None
        if "pending_cash" in group.columns and pd.notna(first["pending_cash"]):
            pending = float(first["pending_cash"])
        rows: dict[str, dict[str, Any]] = {}
        for record in group.to_dict("records"):
            code = record.get("bond_code")
            if pd.notna(code):
                rows[str(code)] = record
        states[as_of] = _SnapshotState(
            cash=float(first["cash"]),
            nav=float(first["nav"]),
            pending=pending,
            rows=rows,
        )
    return states


# ---------------------------------------------------------------------------
# 现金流
# ---------------------------------------------------------------------------
def _cashflow_events(frame: pd.DataFrame) -> dict[date, list[tuple[str, float, str | None]]]:
    events: dict[date, list[tuple[str, float, str | None]]] = {}
    for row in frame.itertuples(index=False):
        code = getattr(row, "bond_code", None)
        events.setdefault(row.date, []).append(
            (str(row.kind), float(row.amount), None if pd.isna(code) else str(code))
        )
    return events


def _settlement_of(day: date, calendar: Any, cfg: Config) -> date:
    """``day`` 的 T+N 结算日（信息列 + 成交应计口径用，不用于快照重估）。"""
    method = getattr(calendar, "settlement_date", None)
    if not callable(method):
        raise TypeError(
            "calendar 缺少 settlement_date(trade_date, lag_days)：需要 "
            "ficc.utils.calendar.TradingCalendar 或同接口对象"
        )
    return _as_date(
        method(day, int(cfg.pricing.settlement_lag_days)), field="calendar.settlement_date"
    )


# ---------------------------------------------------------------------------
# 内部校验
# ---------------------------------------------------------------------------
def _check_identity(left: float, right: float, *, what: str, scale: float) -> None:
    tolerance = _IDENTITY_REL_TOL * max(1.0, abs(scale))
    if abs(left - right) > tolerance:
        raise RuntimeError(
            f"{what} 恒等式失败：{left:.12f} vs {right:.12f}"
            f"（差 {left - right:.3e}，容差 {tolerance:.3e}）；属实现错误，不允许静默"
        )


# ---------------------------------------------------------------------------
# 主函数
# ---------------------------------------------------------------------------
def attribute_fixed_holdings(
    *,
    strategy: str,
    nav: pd.Series,
    snapshots: pd.DataFrame,
    cashflows: pd.DataFrame,
    trades: pd.DataFrame,
    panel: pd.DataFrame,
    calendar: Any,
    cfg: Config | None = None,
    benchmark: AttributionResult | None = None,
    curve_method: CurveMethod = _DEFAULT_CURVE_METHOD,
) -> AttributionResult:
    """对**固定持仓账本**做逐日归因（精确桥接 + 独立重估 + 因子分解）。

    Parameters
    ----------
    strategy : str
        账本/策略名；基准账本用 ``"benchmark"``。
    nav : pandas.Series
        ``Ledger.nav_series()``（index=as_of 日期，值=NAV 元）。用于 NAV 差分；
        会与快照 NAV 交叉校验（不一致直接报错，避免用错误 NAV 自证）。
    snapshots : pandas.DataFrame
        ``Ledger.snapshots_frame()``（逐券逐日长表）。必需列：``as_of``、
        ``cash``、``nav``、``bond_code``、``face_value``、``dirty_price``；
        ``pending_cash`` 可选（缺失时按 trades 重建）。
    cashflows : pandas.DataFrame
        ``Ledger.cashflows_frame()``（date/kind/amount[/bond_code]）。
    trades : pandas.DataFrame
        ``Ledger.trades_frame()``，需含债券条款与三层日期列。
    panel : pandas.DataFrame
        YTM 曲线面板：index=日期，columns=期限（年，至少含 2Y/5Y/10Y，
        建议 11 个标准期限），values=YTM 百分数。第 d 日只读 d−1 与 d 两行。
    calendar : Any
        ``TradingCalendar`` 或同接口对象（提供 ``settlement_date(trade_date, lag)``）。
    cfg : Config | None
        显式配置优先；``None`` 时读全局配置（计息/结算滞后口径）。
    benchmark : AttributionResult | None
        可选基准归因结果；仅挂载到返回值，不做自动相对归因。
    curve_method : {"linear", "hermite", "ns", "nss"}, default "hermite"
        用 panel 节点重建曲线时的插值方法。默认 ``hermite`` 与回测引擎
        ``backtest.engine._curve_at`` 的 CN_IB 面板口径一致（账本快照正是用该口径
        估值）；若账本用其他插值，显式传入以免 carry/other 混入插值口径差。
        桥接用的日终全价优先取快照 ``dirty_price``，因此口径差不会进入
        ``bridge_residual``。

    Returns
    -------
    AttributionResult
        ``daily`` / ``factors`` / ``bonds`` / ``residual_bp``（列契约见模块 docstring）。

    Raises
    ------
    ValueError
        输入缺列、日期无法对齐、panel 缺日期/期限、nav 与快照不一致、未知现金流
        kind、trades 缺债券条款等。
    RuntimeError
        逐券/逐日内部恒等式（carry+curve == price、四因子和 == curve_pnl）失败：
        这是实现错误，不允许静默。
    TypeError
        参数类型非法。
    """
    if not isinstance(strategy, str) or not strategy:
        raise ValueError(f"strategy 必须是非空字符串，实际 {strategy!r}")
    if benchmark is not None and not isinstance(benchmark, AttributionResult):
        raise TypeError(
            f"benchmark 必须是 AttributionResult 或 None，实际 {type(benchmark).__name__}"
        )
    resolved = cfg if cfg is not None else get_config()

    nav_series = _normalise_nav(nav)
    snapshot_frame = _normalise_snapshots(snapshots)
    cashflow_frame = _normalise_cashflows(cashflows)
    panel_frame = _normalise_panel(panel)
    statics, trade_views = _trade_views(trades, resolved)

    dates = list(nav_series.index)
    states = _snapshot_states(snapshot_frame)
    snapshot_dates = set(states)
    nav_dates = set(dates)
    if snapshot_dates != nav_dates:
        missing_in_snap = sorted(nav_dates - snapshot_dates)
        missing_in_nav = sorted(snapshot_dates - nav_dates)
        raise ValueError(
            "nav 与 snapshots 的日期集合不一致："
            f"snapshots 缺 {missing_in_snap[:5]}，nav 缺 {missing_in_nav[:5]}"
        )
    for day in dates:
        tolerance = _BRIDGE_ABS_TOL * max(1.0, abs(float(nav_series.loc[day])))
        if abs(states[day].nav - float(nav_series.loc[day])) > tolerance:
            raise ValueError(
                f"{day}: 传入 nav={float(nav_series.loc[day]):.6f} 与快照 nav="
                f"{states[day].nav:.6f} 不一致（归因必须用账本 NAV，不接受外部改数）"
            )

    pending_by_date: dict[date, float] = {}
    for day in dates:
        value = states[day].pending
        pending_by_date[day] = _pending_from_trades(trade_views, day) if value is None else value

    tenors = panel_frame.columns.to_numpy(dtype=float)
    index_2y = _tenor_index(tenors, _SLOPE_SHORT_TENOR)
    index_5y = _tenor_index(tenors, _CURV_BELLY_TENOR)
    index_10y = _tenor_index(tenors, _LEVEL_TENOR)
    shapes = factor_shapes(tenors)
    level_shape, slope_shape, curvature_shape = shapes[0], shapes[1], shapes[2]

    events_by_date = _cashflow_events(cashflow_frame)
    coupon_by_day_bond: dict[date, dict[str, float]] = {}
    for day, events in events_by_date.items():
        bucket: dict[str, float] = {}
        for kind, amount, code in events:
            if kind == "coupon" and code is not None:
                bucket[code] = bucket.get(code, 0.0) + amount
        if bucket:
            coupon_by_day_bond[day] = bucket
    trades_by_date: dict[date, list[_TradeView]] = {}
    for view in trade_views:
        trades_by_date.setdefault(view.trade_date, []).append(view)

    daily_rows: list[dict[str, Any]] = []
    factor_rows: list[dict[str, Any]] = []
    bond_rows: list[dict[str, Any]] = []
    residual_bp_values: list[float] = []

    for index in range(1, len(dates)):
        previous_day = dates[index - 1]
        day = dates[index]
        previous_state = states[previous_day]
        state = states[day]
        nav_previous = float(nav_series.loc[previous_day])
        nav_day = float(nav_series.loc[day])

        previous_curve = _panel_curve(previous_day, tenors, panel_frame, curve_method)
        current_curve = _panel_curve(day, tenors, panel_frame, curve_method)
        previous_rates = np.asarray(previous_curve.rates, dtype=float)
        current_rates = np.asarray(current_curve.rates, dtype=float)
        # 曲线单位是百分数；因子日变化统一用 bp 报告，重估时再换回百分数。
        delta_bp = (current_rates - previous_rates) * 100.0
        nodal_delta = np.array(
            [delta_bp[index_2y], delta_bp[index_5y], delta_bp[index_10y]], dtype=float
        )
        factor_delta = FACTOR_MATRIX @ nodal_delta
        delta_level_bp = float(factor_delta[0])
        delta_slope_bp = float(factor_delta[1])
        delta_curvature_bp = float(factor_delta[2])
        delta_mean_bp = float(np.mean(delta_bp))

        level_rates = previous_rates + level_shape * delta_level_bp / 100.0
        slope_rates = level_rates + slope_shape * delta_slope_bp / 100.0
        curvature_rates = slope_rates + curvature_shape * delta_curvature_bp / 100.0
        level_curve = _curve(day, tenors, level_rates, curve_method)
        slope_curve = _curve(day, tenors, slope_rates, curve_method)
        curvature_curve = _curve(day, tenors, curvature_rates, curve_method)

        begin_rows = previous_state.rows
        end_rows = state.rows

        coupon_total = 0.0
        principal_total = 0.0
        funding_cost = 0.0
        fee_cost = 0.0
        trade_settlement = 0.0
        for kind, amount, _code in events_by_date.get(day, []):
            if kind == "coupon":
                coupon_total += amount
            elif kind == "principal":
                principal_total += amount
            elif kind == "funding":
                funding_cost -= amount
            elif kind == "fee":
                fee_cost -= amount
            elif kind in ("trade_buy", "trade_sell"):
                trade_settlement += amount

        slippage = 0.0
        trade_entry = 0.0
        for view in trades_by_date.get(day, []):
            sign = 1.0 if view.side == "buy" else -1.0
            end_dirty = _bond_mark(view.code, view.bond, day, end_rows, current_curve, resolved)
            slippage += sign * view.face * (end_dirty - view.dirty_trade) / 100.0
            trade_entry += sign * view.face * view.dirty_trade / 100.0

        price_total = 0.0
        model_price_total = 0.0
        basis_total = 0.0
        carry_total = 0.0
        level_total = 0.0
        slope_total = 0.0
        curvature_total = 0.0
        other_total = 0.0
        redemption_face = 0.0
        coupon_by_bond = coupon_by_day_bond.get(day, {})

        for code in sorted(begin_rows):
            record = begin_rows[code]
            bond = statics.get(code)
            if bond is None:
                raise ValueError(
                    f"{code}: 期初持仓券在 trades 中找不到条款（归因需要 coupon_pct/freq/"
                    "issue_date/maturity_date 才能独立重估）"
                )
            begin_face = float(record["face_value"])
            if begin_face <= 0.0:
                continue
            begin_dirty = record.get("dirty_price")
            if pd.isna(begin_dirty):
                raise ValueError(f"{code}@{previous_day}: 期初全价 dirty_price 缺失")
            begin_dirty = float(begin_dirty)
            begin_ytm = (
                float(record["ytm_pct"]) if pd.notna(record.get("ytm_pct")) else float("nan")
            )
            end_record = end_rows.get(code)
            end_face = float(end_record["face_value"]) if end_record is not None else 0.0
            remaining = day_count(day, bond.maturity_date) / 365.0
            model_begin = _model_dirty(bond, previous_day, previous_curve, resolved)

            matured = end_record is None and day >= bond.maturity_date
            if matured:
                # R35：到期退出 = 价格 → 现金的转换（赎回价值 100，末期票息走 coupon_cash）。
                # 市场曲线分项必须为零；本金现金在桥接里扣除 redemption_face，避免重复计收益。
                end_dirty_snapshot = 100.0
                model_end = 100.0
                carry_roll = begin_face * (100.0 - model_begin) / 100.0
                level = slope = curvature = other_curve = 0.0
                redemption_face += begin_face
            else:
                if end_record is not None:
                    end_dirty_snapshot = _snapshot_dirty(end_record, code, day)
                else:
                    # 当日全部卖出（未到期）：用清仓 mark 作为日终价；不产生独立估值差。
                    end_dirty_snapshot = _bond_mark(
                        code, bond, day, end_rows, current_curve, resolved
                    )
                model_end = _model_dirty(bond, day, current_curve, resolved)
                dirty_roll = _model_dirty(bond, day, previous_curve, resolved)
                dirty_level = _model_dirty(bond, day, level_curve, resolved)
                dirty_slope = _model_dirty(bond, day, slope_curve, resolved)
                dirty_curvature = _model_dirty(bond, day, curvature_curve, resolved)
                carry_roll = begin_face * (dirty_roll - model_begin) / 100.0
                level = begin_face * (dirty_level - dirty_roll) / 100.0
                slope = begin_face * (dirty_slope - dirty_level) / 100.0
                curvature = begin_face * (dirty_curvature - dirty_slope) / 100.0
                other_curve = begin_face * (model_end - dirty_curvature) / 100.0
                curve_model = begin_face * (model_end - model_begin) / 100.0 - carry_roll
                _check_identity(
                    level + slope + curvature + other_curve,
                    curve_model,
                    what=f"{code}@{day} 四因子和==model curve_pnl",
                    scale=curve_model,
                )

            price_snapshot = begin_face * (end_dirty_snapshot - begin_dirty) / 100.0
            price_model = begin_face * (model_end - model_begin) / 100.0
            valuation_basis = price_snapshot - price_model
            matured = end_record is None and day >= bond.maturity_date
            if matured:
                _check_identity(
                    level + slope + curvature + other_curve,
                    0.0,
                    what=f"{code}@{day} 到期市场曲线分项==0",
                    scale=price_snapshot,
                )

            bond_rows.append(
                {
                    "date": day,
                    "bond_code": code,
                    "begin_face": begin_face,
                    "end_face": end_face,
                    "begin_dirty": begin_dirty,
                    "end_dirty": end_dirty_snapshot,
                    "price_pnl": price_snapshot,
                    "carry_roll": carry_roll,
                    "curve_pnl": price_model - carry_roll,
                    "coupon_cash": float(coupon_by_bond.get(code, 0.0)),
                    "level": level,
                    "slope": slope,
                    "curvature": curvature,
                    "other_curve": other_curve,
                    "begin_ytm_pct": begin_ytm,
                    "remaining_years": remaining,
                    "model_dirty_begin": model_begin,
                    "model_dirty_end": model_end,
                    "valuation_basis": valuation_basis,
                }
            )
            price_total += price_snapshot
            model_price_total += price_model
            basis_total += valuation_basis
            carry_total += carry_roll
            level_total += level
            slope_total += slope
            curvature_total += curvature
            other_total += other_curve

        curve_total = model_price_total - carry_total
        _check_identity(
            level_total + slope_total + curvature_total + other_total,
            curve_total,
            what=f"{day} 四因子和==model curve_pnl",
            scale=curve_total,
        )
        _check_identity(
            model_price_total + basis_total,
            price_total,
            what=f"{day} snapshot price == model price + valuation basis",
            scale=price_total,
        )

        pending_delta = pending_by_date[day] - pending_by_date[previous_day]
        nav_delta = nav_day - nav_previous
        bridge_residual = nav_delta - (
            price_total
            + coupon_total
            + (principal_total - redemption_face)  # 到期本金是价格→现金转换，不重复计收益
            - funding_cost
            - fee_cost
            + trade_settlement
            + pending_delta
            + slippage
            + trade_entry
        )
        if not np.isfinite(bridge_residual):
            raise ValueError(f"{day}: 桥接残差非有限值，输入含 NaN/inf")

        basis_scale = max(VALUATION_ABS_TOL, VALUATION_REL_NAV_TOL * abs(nav_day))
        basis_breach = bool(abs(basis_total) > basis_scale)
        residual_bp_values.append(bridge_residual / max(1.0, abs(nav_previous)) * 1e4)
        daily_rows.append(
            {
                "date": day,
                "nav": nav_day,
                "nav_delta": nav_delta,
                "price_pnl": price_total,
                "coupon_cash": coupon_total,
                "principal_cash": principal_total,
                "funding": funding_cost,
                "fees": fee_cost,
                "trade_settlement": trade_settlement,
                "pending_delta": pending_delta,
                "slippage": slippage,
                "bridge_residual": bridge_residual,
                "trade_entry": trade_entry,
                "settlement_date": _settlement_of(day, calendar, resolved),
                "model_price_pnl": model_price_total,
                "valuation_basis": basis_total,
                "valuation_basis_nav_bp": basis_total / max(1.0, abs(nav_day)) * 1e4,
                "valuation_basis_breach": basis_breach,
                "principal_redemption": redemption_face,
            }
        )
        factor_rows.append(
            {
                "date": day,
                "carry_roll": carry_total,
                "level": level_total,
                "slope": slope_total,
                "curvature": curvature_total,
                "other_curve": other_total,
                "curve_pnl": curve_total,
                "price_pnl": model_price_total,
                "level_bp": delta_level_bp,
                "level_mean_bp": delta_mean_bp,
                "level_mean_diff_bp": delta_level_bp - delta_mean_bp,
                "slope_bp": delta_slope_bp,
                "curvature_bp": delta_curvature_bp,
                "other_curve_nav_bp": other_total / max(1.0, abs(nav_day)) * 1e4,
                "snapshot_price_pnl": price_total,
                "valuation_basis": basis_total,
            }
        )

    daily = pd.DataFrame(daily_rows, columns=list(DAILY_COLUMNS))
    factors = pd.DataFrame(factor_rows, columns=list(FACTORS_COLUMNS))
    bonds = pd.DataFrame(bond_rows, columns=list(BONDS_COLUMNS))
    residual_bp = pd.Series(
        residual_bp_values,
        index=pd.Index([row["date"] for row in daily_rows], name="date"),
        name="residual_bp",
        dtype=float,
    )
    return AttributionResult(
        strategy=strategy,
        daily=daily,
        factors=factors,
        bonds=bonds,
        residual_bp=residual_bp,
        benchmark=benchmark,
    )


def _snapshot_dirty(record: dict[str, Any], code: str, day: date) -> float:
    """读取快照日终全价（缺失则明确报错，不静默用模型价替代）。"""
    value = record.get("dirty_price")
    if pd.isna(value):
        raise ValueError(f"{code}@{day}: 日终全价 dirty_price 缺失")
    return float(value)


def _model_dirty(
    bond: BondStatic,
    settlement: date,
    curve: Curve,
    cfg: Config,
) -> float:
    """从**条款 + 结算日 + 曲线**独立重估全价（与账本口径一致：ACT/365、T+lag）。

    到期/已过到期日返回赎回价值 100（末期票息由现金事件单独处理，不并入价格）。
    """
    remaining = day_count(settlement, bond.maturity_date) / 365.0
    if remaining < 0.0:
        return 100.0
    if remaining == 0.0:
        # 结算日=到期日：全价 = 100 + 末期票息（AI 在到期日已满一个付息期）
        try:
            return 100.0 + float(accrued_interest(bond, settlement, cfg=cfg))
        except Exception:
            return 100.0
    ytm = float(curve.rate(remaining))
    return float(dirty_price_from_ytm(bond, settlement, ytm, cfg=cfg))


def _bond_mark(
    code: str,
    bond: BondStatic,
    day: date,
    end_rows: dict[str, dict[str, Any]],
    current_curve: Curve,
    cfg: Config,
) -> float:
    """``day`` 日终全价：优先用快照（账本口径）；不在快照（当日卖出/到期）则重估。

    快照里仍持有的券直接用 ``dirty_price``，保证与 NAV 中的 ``market_value``
    逐位一致；已清仓券按当日曲线独立重估（作为日终 mark），到期券为 0。
    """
    record = end_rows.get(code)
    if record is not None:
        value = record.get("dirty_price")
        if pd.isna(value):
            raise ValueError(f"{code}@{day}: 日终全价 dirty_price 缺失")
        return float(value)
    remaining = day_count(day, bond.maturity_date) / 365.0
    if remaining <= 0.0 or day >= bond.maturity_date:
        return 0.0
    return dirty_price_from_ytm(bond, day, float(current_curve.rate(remaining)), cfg=cfg)
