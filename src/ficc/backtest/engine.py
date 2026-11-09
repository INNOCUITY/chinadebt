"""日频回测引擎（R22/R23/R26/R27 修复版）。

时序纪律
--------
- t 日收盘后生成信号（策略只能看到 ``history <= t``）；
- **t+1 日成交**（执行价 = t+1 收盘曲线、结算日 = 日历 T+1、含半价差）；
- 成交前用**最终取整面值 + 成交日价格 + 未结算现金/费用**做完整 9 项约束校验，
  不可行则缩减 tilt 重试，仍不可行就**拒单**（不静默回基准）。

基准口径（R22）
--------------
风险基准与绩效基准是**同一个买入持有组合**：起点构造固定面值，之后每日只做
mark-to-market（老化后剩余期限、CN_IB 结算日），并跑一个同成本口径的基准账本
得到 ``benchmark_nav``。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any, Protocol

import numpy as np
import pandas as pd

from ficc.config import Config, get_config
from ficc.curve.curve import Curve
from ficc.logging import get_logger
from ficc.portfolio.dates import settlement_for
from ficc.portfolio.ledger import Ledger, TradeRecord
from ficc.portfolio.types import BenchmarkPortfolio, MarketContext, TargetFaces, TargetKRD

log = get_logger("backtest.engine")

KEY_COLUMNS = [
    "date",
    "nav",
    "cash",
    "pending_cash",
    "market_value",
    "total_dv01",
    "duration",
    "leverage",
    "dv01_ratio",
    "bench_total_dv01",
    "bench_duration",
    "bench_nav",
]


class StrategyLike(Protocol):
    name: str

    def target_krd(self, ctx: MarketContext) -> TargetKRD: ...


@dataclass
class BacktestResult:
    strategy: str
    start: date
    end: date
    benchmark_version: str
    rolling_version: str
    roll_events: pd.DataFrame
    nav: pd.Series
    benchmark_nav: pd.Series
    exposure: pd.DataFrame
    trades: pd.DataFrame
    cashflows: pd.DataFrame
    costs: pd.DataFrame
    turnover: pd.Series
    constraints: pd.DataFrame
    signals: pd.DataFrame
    rejected: pd.DataFrame
    # 归因/对照实验输入（逐券日快照与基准账本；R23 补查要求的持久化输入）
    positions: pd.DataFrame
    benchmark_positions: pd.DataFrame
    benchmark_trades: pd.DataFrame
    benchmark_cashflows: pd.DataFrame
    # 每日真实闸门决策日志（含 policy roll / 无动作 / 被滚动取消的 pending 语义）
    gate_decisions: list[dict[str, Any]] = field(default_factory=list)

    def to_frames(self) -> dict[str, pd.DataFrame | pd.Series]:
        return {
            "nav": self.nav,
            "benchmark_nav": self.benchmark_nav,
            "exposure": self.exposure,
            "trades": self.trades,
            "cashflows": self.cashflows,
            "costs": self.costs,
            "turnover": self.turnover,
            "constraints": self.constraints,
            "signals": self.signals,
            "rejected": self.rejected,
            "positions": self.positions,
            "benchmark_positions": self.benchmark_positions,
            "benchmark_trades": self.benchmark_trades,
            "benchmark_cashflows": self.benchmark_cashflows,
        }


def _curve_at(panel: pd.DataFrame, tenors: list[float], d: date) -> Curve:
    row = panel.loc[pd.Timestamp(d)].reindex(tenors)
    if row.isna().any():
        raise ValueError(f"{d} 曲线存在缺失期限：{row[row.isna()].index.tolist()}")
    return Curve(as_of=d, tenors=np.asarray(tenors), rates=tuple(row.values), method="hermite")


def _funding_map(funding: pd.Series | None, days: list[date], cfg: Config) -> dict[date, float]:
    fallback = cfg.costs.repo_rate_fallback_const * 100.0
    series = funding.dropna() if funding is not None else None
    out: dict[date, float] = {}
    for d in days:
        if series is not None and not series.empty:
            prior = series.loc[:d]
            out[d] = float(prior.iloc[-1]) if len(prior) else fallback
        else:
            out[d] = fallback
    return out


# 可交易行动项（`drawdown_stop` 是历史结果，不能靠当日卖券消除，R31）
ACTION_CODES: frozenset[str] = frozenset(
    {
        "relative_krd",
        "total_dv01_ratio",
        "duration_band",
        "absolute_dv01",
        "krd_share",
        "single_bond_weight",
        "leverage",
        "cash_floor",
    }
)


def action_severity(report: Any) -> float:
    """归一化违规严重度（只统计可交易行动项）。"""
    worst = 0.0
    for violation in report.violations:
        if violation.code not in ACTION_CODES:
            continue
        limit = float(violation.limit)
        worst = max(worst, abs(float(violation.value) - limit) / max(abs(limit), 1.0))
    return worst


def allows_partial_de_risk(current_report: Any, candidate_report: Any) -> bool:
    """降风险例外：可行动项不新增 code 且严重度严格下降（历史回撤允许仍存在）。"""
    current_codes = {v.code for v in current_report.violations} & ACTION_CODES
    cand_codes = {v.code for v in candidate_report.violations} & ACTION_CODES
    if not cand_codes <= current_codes:
        return False
    current_sev = action_severity(current_report)
    return current_sev > 0.0 and action_severity(candidate_report) < current_sev * 0.999


def _per_100_dv01(bond: Any, curve: Any, settlement: date, *, calendar: Any, cfg: Config) -> float:
    """券在给定结算日的每 100 面值 DV01（元/bp/100 面值），用于滚动基准保持腿 DV01。"""
    from ficc.portfolio.dates import remaining_years
    from ficc.pricing.analytics import dv01 as analytics_dv01

    remaining = remaining_years(bond, settlement)
    ytm = float(curve.rate(remaining))
    return float(analytics_dv01(bond, settlement, ytm, cfg=cfg))


def _fee_estimate(face: float, clean_price: float, cfg: Config) -> float:
    """与账本 ``_trade_fee`` 同式的手续费/结算费估算（成交前现金预算用）。"""
    notional = face / 100.0 * clean_price
    return float(cfg.costs.fee_bp) / 1e4 * notional + float(cfg.costs.settlement_fee_cny_per_trade)


def run_backtest(
    strategy: StrategyLike,
    *,
    panel: pd.DataFrame,
    calendar: Any,
    funding: pd.Series | None = None,
    cfg: Config | None = None,
    start: date | None = None,
    end: date | None = None,
) -> BacktestResult:
    from ficc.portfolio.benchmark import build_benchmark, mark_faces
    from ficc.portfolio.constraints import check_constraints, portfolio_risk
    from ficc.portfolio.construction import build_orders, execution_price
    from ficc.portfolio.rolling import RollingUniverse, RollPolicy

    cfg = cfg or get_config()
    start = start or cfg.backtest.start_date
    end = end or (cfg.backtest.end_date or panel.index.max().date())
    days = list(calendar.trading_days_between(start, end))
    if len(days) < 2:
        raise ValueError(f"回测区间 {start}~{end} 内交易日不足 2 天")

    from ficc.data.schema import CANONICAL_TENORS

    curve_tenors = list(CANONICAL_TENORS)
    curve0 = _curve_at(panel, curve_tenors, days[0])
    roll_policy = RollPolicy.from_config(cfg)
    benchmark_version = str(cfg.backtest.benchmark_version)
    if roll_policy.enabled and benchmark_version != "rolling":
        raise ValueError(
            "portfolio.rolling.enabled=True 时 benchmark_version 必须为 'rolling'"
            "（滚动策略不能与买入持有基准混比）"
        )
    if (not roll_policy.enabled) and benchmark_version == "rolling":
        raise ValueError(
            "benchmark_version='rolling' 需要 portfolio.rolling.enabled=True"
            "（滚动基准必须有明确的滚动政策）"
        )
    rolling_universe = RollingUniverse.build(curve0, days[0], cfg=cfg)
    universe = rolling_universe.active()
    benchmark0 = build_benchmark(curve0, days[0], bonds=universe, calendar=calendar, cfg=cfg)
    bench_faces: dict[str, float] = dict(benchmark0.faces)
    bench_weights: dict[float, float] = dict(benchmark0.weights)
    bench_notional = float(benchmark0.notional or benchmark0.market_value)
    bench_spread_day = 0.0
    funding_map = _funding_map(funding, days, cfg)

    def _ledger() -> Ledger:
        return Ledger(
            calendar,
            initial_cash=cfg.backtest.init_cash,
            funding_rate_pct=funding_map[days[0]],
            funding_rate_by_date=funding_map,
            cfg=cfg,
        )

    ledger = _ledger()
    bench_ledger = _ledger()
    bench_bought = False

    nav_rows: dict[date, float] = {}
    bench_nav_rows: dict[date, float] = {}
    exposure_rows: list[dict[str, Any]] = []
    cost_rows: list[dict[str, Any]] = []
    constraint_rows: list[dict[str, Any]] = []
    turnover_rows: dict[date, float] = {}
    signal_rows: list[dict[str, Any]] = []
    rejected_rows: list[dict[str, Any]] = []
    # 价差成本按"每次真实成交"累计（基准买入与策略成交分开，避免污染策略成本）
    order_spread: dict[str, float] = {bond.code: 0.0 for bond in universe}
    bench_trade_rows: list[dict[str, Any]] = []
    trade_spread_rows: list[dict[str, Any]] = []
    roll_rows: list[dict[str, Any]] = []
    gate_decisions: list[dict[str, Any]] = []

    band_face = float(cfg.constraints.min_trade_lot) * float(cfg.constraints.rebalance_band_lots)
    last_rebalance_week: tuple[int, int] | None = None
    nav_high = float(cfg.backtest.init_cash)
    risk_off = False
    pending: TargetKRD | None = None
    pending_proposed: Any = None
    last_snapshot = None
    bench_snapshot = None

    # ------------------------------------------------------------------
    # 成交前完整校验（R23）
    # ------------------------------------------------------------------
    def _orders_from_faces(
        target_faces: dict[str, float], snapshot: Any, curve: Curve, d: date
    ) -> tuple[Any, ...]:
        """把逐券目标面值转成订单（高于最小整手才交易；非目标持仓全部卖出）。"""
        from ficc.portfolio.construction import Order

        current = {p.bond.code: p for p in (snapshot.positions if snapshot else ())}
        lot = float(cfg.constraints.min_trade_lot)
        orders: list[Order] = []
        codes = set(current) | set(target_faces)
        for code in sorted(codes):
            position = current.get(code)
            current_face = float(position.face_value) if position is not None else 0.0
            target_face = float(target_faces.get(code, 0.0))
            delta = target_face - current_face
            if abs(delta) < lot - 1e-9:
                continue
            bond = position.bond if position is not None else None
            if bond is None:
                # 目标中有、当前无：从 universe 找（仅新券场景由 policy roll 处理）
                bond = next((b for b in universe if b.code == code), None)
            if bond is None:
                continue
            side = "buy" if delta > 0 else "sell"
            face = round(abs(delta) / lot) * lot
            if face <= 0.0:
                continue
            clean_exec, mid, spread_bp, per100 = execution_price(
                bond, curve, side=side, calendar=calendar, cfg=cfg
            )
            orders.append(
                Order(
                    bond=bond,
                    side=side,
                    face_value=float(face),
                    clean_price=clean_exec,
                    target_face=target_face,
                    current_face=current_face,
                    mid_clean_price=mid,
                    spread_bp=spread_bp,
                    spread_cost=per100 * face / 100.0,
                )
            )
        return tuple(orders)

    def _policy_roll_orders(
        events: list[Any],
        snapshot: Any,
        curve: Curve,
        d: date,
        bench_faces: dict[str, float],
    ) -> tuple[Any, ...]:
        """机械换券订单：卖光旧券 + 按**基准同腿面值**买入新券（双方共用政策）。

        与基准完全一致的腿面值（而不是"保持策略旧面值"或"DV01 等价"）保证零 tilt 控制组
        在滚动日与基准同步；主动策略在该腿的偏离会在下一次信号中重新建立。
        """
        from ficc.portfolio.construction import Order

        positions = {p.bond.code: p for p in (snapshot.positions if snapshot else ())}
        lot = float(cfg.constraints.min_trade_lot)
        orders: list[Order] = []
        for event in events:
            position = positions.get(event.retired.code)
            old_face = float(position.face_value) if position is not None else 0.0
            if old_face <= 0.0:
                continue
            clean_exec, mid, spread_bp, per100 = execution_price(
                event.retired, curve, side="sell", calendar=calendar, cfg=cfg
            )
            orders.append(
                Order(
                    bond=event.retired,
                    side="sell",
                    face_value=old_face,
                    clean_price=clean_exec,
                    target_face=0.0,
                    current_face=old_face,
                    mid_clean_price=mid,
                    spread_bp=spread_bp,
                    spread_cost=per100 * old_face / 100.0,
                )
            )
            # 与基准共用同一政策：该腿滚动到**基准的新券面值**，零 tilt 因此与基准逐腿一致。
            target_face = float(bench_faces.get(event.issued.code, 0.0))
            new_face = round(target_face / lot) * lot if target_face > 0.0 else 0.0
            if new_face <= 0.0:
                continue
            clean_exec_b, mid_b, spread_bp_b, per100_b = execution_price(
                event.issued, curve, side="buy", calendar=calendar, cfg=cfg
            )
            orders.append(
                Order(
                    bond=event.issued,
                    side="buy",
                    face_value=new_face,
                    clean_price=clean_exec_b,
                    target_face=new_face,
                    current_face=0.0,
                    mid_clean_price=mid_b,
                    spread_bp=spread_bp_b,
                    spread_cost=per100_b * new_face / 100.0,
                )
            )
        return tuple(orders)

    def _candidate_trades(
        orders: tuple[Any, ...], *, signal_date: date, trade_date: date
    ) -> tuple[TradeRecord, ...]:
        settle = settlement_for(calendar, trade_date, cfg=cfg)
        return tuple(
            TradeRecord(
                trade_id=f"{strategy.name}-{trade_date:%Y%m%d}-{order.bond.code}-{order.side}",
                bond=order.bond,
                side=order.side,
                face_value=order.face_value,
                clean_price=order.clean_price,
                signal_date=signal_date,
                trade_date=trade_date,
                settlement_date=settle,
            )
            for order in orders
        )

    def _reports_for_path(
        path: tuple[Any, ...],
        curve: Curve,
        benchmark_d: BenchmarkPortfolio,
        nav_high: float,
    ) -> list[Any]:
        """对投影路径的**每个快照**跑完整 9 项约束（R37：不能只看最后一个时点）。"""
        reports = []
        for snap in path:
            risk = portfolio_risk(snap, curve, calendar=calendar, cfg=cfg)
            reports.append(
                check_constraints(
                    risk=risk, benchmark=benchmark_d, nav_high_water=nav_high, cfg=cfg
                )
            )
        return reports

    def _path_action_profile(reports: list[Any]) -> tuple[frozenset[str], float]:
        """路径上可交易行动项的并集与最差严重度（历史回撤不计，R31 口径）。"""
        codes: set[str] = set()
        severity = 0.0
        for report in reports:
            codes.update(
                str(violation.code)
                for violation in report.violations
                if violation.code in ACTION_CODES
            )
            severity = max(severity, float(action_severity(report)))
        return frozenset(codes), severity

    def project_and_validate(
        target: TargetKRD,
        *,
        curve: Curve,
        d: date,
        snapshot: Any,
        benchmark_d: BenchmarkPortfolio,
        nav_high_water: float,
        risk_off: bool = False,
        force_min_lot: bool = False,
        forced_orders: tuple[Any, ...] | None = None,
        attempts_log: list[dict[str, Any]] | None = None,
        forced_stage: str = "target_faces",
    ) -> tuple[tuple[Any, ...] | None, list[TargetKRD], str, bool, float]:
        """成交前完整校验（R31/R32 修复版）。

        返回 ``(可执行订单, 尝试过的目标列表, 拒单/降险原因, 是否降险执行, 选中的风险档位)``。

        实现要点：
        - **统一状态推进**：用 ``Ledger.simulate`` 克隆账本，把今日已发生事件推进到 d，
          加入候选成交，再逐自然日推进到 ``max(下一交易日, 结算日的下一交易日)``，
          结算/票息/费用/融资全部按账本规则预演（不另写近似公式，不读未来利率）；
        - **完整 9 项**：用投影快照的 cash/nav/positions 直接算风险；
        - **熔断阶梯**：``risk_off=True`` 时按 ``[1.0,0.8,0.6,0.4,0.2,0.0]×基准`` 降档；
        - **降风险偏序**：只对可交易行动项要求"不新增 code 且严重度下降"，历史回撤允许仍存在。
        """
        positions_before = () if snapshot is None else snapshot.positions
        # 当前状态的"路径报告"：只推进今日已发生事件（不提交候选），包含 as_of 当日。
        # 与候选路径使用同一批量口径比较，避免"当前快照 vs 候选末日"的不对称（R37）。
        try:
            current_path = ledger.simulate(d, curve, extra_trades=(), return_path=True)
        except ValueError:
            current_path = (ledger.snapshot(),) if hasattr(ledger, "snapshot") else ()
        current_reports = _reports_for_path(current_path, curve, benchmark_d, nav_high_water)
        current_codes, current_severity = _path_action_profile(current_reports)

        bench_krd = {float(k): float(v) for k, v in benchmark_d.krd.items()}
        dev = {t: float(target.krd.get(t, bench_krd[t])) - bench_krd[t] for t in bench_krd}
        attempted: list[TargetKRD] = []
        last_violation_codes = ""
        if forced_orders is not None:
            # 滚动政策：机械换券（卖旧 + 按 DV01 等价买新）必须同日执行；不做 tilt 缩放，
            # 只按 [1.0..0.0] 缩放买入面值以恢复可行性（卖出旧券始终保留）。
            lot = float(cfg.constraints.min_trade_lot)
            for scale in (1.0, 0.9, 0.8, 0.6, 0.4, 0.2, 0.0):
                attempted.append(target)
                scaled_list = []
                for order in forced_orders:
                    if order.side == "sell":
                        scaled_list.append(order)
                        continue
                    new_face = round(order.face_value * scale / lot) * lot
                    # R46：价差成本必须按**取整后的最终面值**重算，不能只乘缩放比例
                    unit_cost = (
                        order.spread_cost / order.face_value if order.face_value > 0.0 else 0.0
                    )
                    scaled_list.append(
                        order.__class__(
                            bond=order.bond,
                            side=order.side,
                            face_value=new_face,
                            clean_price=order.clean_price,
                            target_face=new_face,
                            current_face=0.0,
                            mid_clean_price=order.mid_clean_price,
                            spread_bp=order.spread_bp,
                            spread_cost=unit_cost * new_face,
                        )
                    )
                scaled = tuple(o for o in scaled_list if o.face_value > 0)
                scaled = tuple(order for order in scaled if order.face_value > 0)
                trades = _candidate_trades(scaled, signal_date=target.as_of, trade_date=d)
                try:
                    path = ledger.simulate(d, curve, extra_trades=trades, return_path=True)
                except ValueError:
                    continue
                reports = _reports_for_path(path, curve, benchmark_d, nav_high_water)
                feasible = all(report.ok for report in reports)
                if attempts_log is not None:
                    attempts_log.append(
                        {
                            "stage": forced_stage,
                            "scale": scale,
                            "target_total_dv01": None,
                            "target_total_face": float(sum(abs(o.face_value) for o in scaled)),
                            "order_faces": {o.bond.code: float(o.face_value) for o in scaled},
                            "order_sides": {o.bond.code: str(o.side) for o in scaled},
                            "feasible": bool(feasible),
                            "violation_codes": sorted(
                                {v.code for report in reports for v in report.violations}
                            ),
                        }
                    )
                if feasible:
                    return scaled, attempted, "", False, 1.0
                last_violation_codes = ";".join(
                    sorted({v.code for report in reports for v in report.violations})
                )
            return (
                None,
                attempted,
                f"滚动政策同日换券不可行（{last_violation_codes or '无违规码'}）",
                False,
                1.0,
            )

        level_factors = [1.0, 0.8, 0.6, 0.4, 0.2, 0.0] if risk_off else [1.0]

        for level in level_factors:
            for _iteration in range(6):
                cand = {t: level * bench_krd[t] + dev[t] for t in bench_krd}
                if min(cand.values()) < 0:
                    dev = {t: max(-level * bench_krd[t], dev[t]) for t in bench_krd}
                    cand = {t: level * bench_krd[t] + dev[t] for t in bench_krd}
                cand_target = TargetKRD(as_of=target.as_of, krd=cand, notes=target.notes)
                attempted.append(cand_target)
                orders = build_orders(
                    cand_target,
                    positions_before,
                    universe,
                    curve,
                    # 熔断时按最小整手交易（否则降风险幅度小会被 2-lot 死区吞掉）
                    min_trade_face=(
                        float(cfg.constraints.min_trade_lot)
                        if (risk_off or force_min_lot)
                        else band_face
                    ),
                    calendar=calendar,
                    cfg=cfg,
                )
                trades = _candidate_trades(orders, signal_date=target.as_of, trade_date=d)
                try:
                    # 默认 horizon = max(as_of, 最大结算日的下一交易日)，覆盖 T+lag 结算；
                    # return_path=True 逐时点检查，不再只看最后一个快照（R37）。
                    path = ledger.simulate(d, curve, extra_trades=trades, return_path=True)
                except ValueError:
                    # 历史成交已推进到 d（重复调用）等边界：显式失败而不是静默成交
                    return None, attempted, "投影失败：账本状态不允许模拟该候选", False, level
                reports = _reports_for_path(path, curve, benchmark_d, nav_high_water)
                feasible = all(report.ok for report in reports)
                if attempts_log is not None:
                    attempts_log.append(
                        {
                            "stage": "tilt",
                            "level": float(level),
                            "target_total_dv01": float(cand_target.total_dv01),
                            "order_faces": {
                                order.bond.code: float(order.face_value) for order in orders
                            },
                            "order_sides": {order.bond.code: str(order.side) for order in orders},
                            "target_total_face": float(
                                sum(abs(order.face_value) for order in orders)
                            ),
                            "feasible": bool(feasible),
                            "violation_codes": sorted(
                                {v.code for report in reports for v in report.violations}
                            ),
                        }
                    )
                if feasible:
                    return orders, attempted, "", False, level

                cand_codes, cand_severity = _path_action_profile(reports)
                last_violation_codes = ";".join(
                    sorted({v.code for report in reports for v in report.violations})
                )
                if risk_off and cand_codes <= current_codes and cand_severity < current_severity:
                    return orders, attempted, "partial_de_risk", True, level
                dev = {t: value * 0.5 for t, value in dev.items()}

        return (
            None,
            attempted,
            f"6 次投影仍不可行（{len(attempted)} 次尝试；最后违规={last_violation_codes or '无'}）",
            False,
            level_factors[-1] if level_factors else 1.0,
        )

    def _market_values(
        faces: dict[str, float], bonds: tuple[Any, ...], curve: Curve, d: date, config: Config
    ) -> dict[str, float]:
        from ficc.portfolio.dates import remaining_years
        from ficc.pricing.bond import dirty_price_from_ytm

        settle = settlement_for(calendar, d, cfg=config)
        out: dict[str, float] = {}
        for bond in bonds:
            face = float(faces.get(bond.code, 0.0))
            if face <= 0:
                continue
            ytm = float(curve.rate(remaining_years(bond, settle)))
            out[bond.code] = (
                face / 100.0 * float(dirty_price_from_ytm(bond, settle, ytm, cfg=config))
            )
        return out

    for d in days:
        curve_d = _curve_at(panel, curve_tenors, d)
        roll_events = rolling_universe.roll(curve_d, d)
        if roll_events:
            universe = rolling_universe.active()
            if bench_bought and benchmark_version == "rolling":
                settle = settlement_for(calendar, d, cfg=cfg)
                roll_signal = calendar.prev_trading_day(d, 1)
                for event in roll_events:
                    roll_row: dict[str, Any] = {
                        "date": d,
                        "tenor": float(event.tenor),
                        "retired_code": event.retired.code,
                        "issued_code": event.issued.code,
                        "bench_sell_trade_id": "",
                        "bench_buy_trade_id": "",
                        "strategy_trade_ids": "",
                        "status": "pending",
                        "reason": "",
                        "pending_status": "",
                        "gate_attempts": 0,
                    }
                    roll_rows.append(roll_row)
                    old_face = float(bench_faces.pop(event.retired.code, 0.0))
                    if old_face > 0.0:
                        clean_exec, mid, _bp, per100 = execution_price(
                            event.retired, curve_d, side="sell", calendar=calendar, cfg=cfg
                        )
                        roll_row["bench_sell_trade_id"] = (
                            f"bench-roll-{d:%Y%m%d}-{event.retired.code}-sell"
                        )
                        bench_ledger.submit(
                            TradeRecord(
                                trade_id=f"bench-roll-{d:%Y%m%d}-{event.retired.code}-sell",
                                bond=event.retired,
                                side="sell",
                                face_value=old_face,
                                clean_price=clean_exec,
                                signal_date=roll_signal,
                                trade_date=d,
                                settlement_date=settle,
                            )
                        )
                        bench_spread_day += per100 * old_face / 100.0
                        bench_trade_rows.append(
                            {
                                "trade_id": f"bench-roll-{d:%Y%m%d}-{event.retired.code}-sell",
                                "bond_code": event.retired.code,
                                "side": "sell",
                                "face_value": old_face,
                                "clean_price": clean_exec,
                                "mid_clean_price": mid,
                                "spread_cost": per100 * old_face / 100.0,
                                "signal_date": roll_signal,
                                "trade_date": d,
                                "settlement_date": settle,
                                "fee": 0.0,
                                "coupon_pct": float(event.retired.coupon_pct),
                                "freq": int(event.retired.freq),
                                "issue_date": event.retired.issue_date,
                                "maturity_date": event.retired.maturity_date,
                                "accrual": event.retired.accrual,
                                "market": event.retired.market,
                            }
                        )
                    # 滚动基准：用**旧券面值**买入新券（滚动政策保持腿面值；权重随价格
                    # 自然漂移，不做额外的权重再平衡，避免基准自身变成主动策略）。
                    face = old_face
                    clean_exec, mid, _bp, per100 = execution_price(
                        event.issued, curve_d, side="buy", calendar=calendar, cfg=cfg
                    )
                    if face > 0.0:
                        roll_row["bench_buy_trade_id"] = (
                            f"bench-roll-{d:%Y%m%d}-{event.issued.code}-buy"
                        )
                        bench_ledger.submit(
                            TradeRecord(
                                trade_id=f"bench-roll-{d:%Y%m%d}-{event.issued.code}-buy",
                                bond=event.issued,
                                side="buy",
                                face_value=float(face),
                                clean_price=clean_exec,
                                signal_date=roll_signal,
                                trade_date=d,
                                settlement_date=settle,
                            )
                        )
                        bench_faces[event.issued.code] = float(face)
                        bench_spread_day += per100 * face / 100.0
                        bench_trade_rows.append(
                            {
                                "trade_id": f"bench-roll-{d:%Y%m%d}-{event.issued.code}-buy",
                                "bond_code": event.issued.code,
                                "side": "buy",
                                "face_value": float(face),
                                "clean_price": clean_exec,
                                "mid_clean_price": mid,
                                "spread_cost": per100 * face / 100.0,
                                "signal_date": roll_signal,
                                "trade_date": d,
                                "settlement_date": settle,
                                "fee": 0.0,
                                "coupon_pct": float(event.issued.coupon_pct),
                                "freq": int(event.issued.freq),
                                "issue_date": event.issued.issue_date,
                                "maturity_date": event.issued.maturity_date,
                                "accrual": event.issued.accrual,
                                "market": event.issued.market,
                            }
                        )
            elif roll_events and bench_bought:
                raise ValueError("滚动政策与买入持有基准不兼容，请在配置中选择 rolling 基准")
        benchmark_d = mark_faces(
            bench_faces,
            bonds=universe,
            curve=curve_d,
            as_of=d,
            calendar=calendar,
            cfg=cfg,
            weights=bench_weights,
            notional=bench_notional,
        )

        # ---- 1) 基准买入持有（与策略同成本/价差/日期口径）----
        if not bench_bought and d == days[1]:
            settle = settlement_for(calendar, d, cfg=cfg)
            for bond in universe:
                face = float(bench_faces.get(bond.code, 0.0))
                if face <= 0:
                    continue
                clean_exec, _mid, _spread_bp, per100 = execution_price(
                    bond, curve_d, side="buy", calendar=calendar, cfg=cfg
                )
                bench_ledger.submit(
                    TradeRecord(
                        trade_id=f"bench-{bond.code}",
                        bond=bond,
                        side="buy",
                        face_value=face,
                        clean_price=clean_exec,
                        signal_date=days[0],
                        trade_date=d,
                        settlement_date=settle,
                    )
                )
                bench_spread_day += per100 * face / 100.0
                bench_trade_rows.append(
                    {
                        "trade_id": f"bench-{bond.code}",
                        "bond_code": bond.code,
                        "side": "buy",
                        "face_value": face,
                        "clean_price": clean_exec,
                        "mid_clean_price": _mid,
                        "spread_cost": per100 * face / 100.0,
                        "signal_date": days[0],
                        "trade_date": d,
                        "settlement_date": settle,
                        "fee": 0.0,
                        "coupon_pct": float(bond.coupon_pct),
                        "freq": int(bond.freq),
                        "issue_date": bond.issue_date,
                        "maturity_date": bond.maturity_date,
                        "accrual": bond.accrual,
                        "market": bond.market,
                    }
                )
            bench_bought = True

        # ---- 1.5) R38：滚动日双方同日换券 ----
        # 基准已在上面换券；策略若当日没有 pending 信号（weekly 非调仓日），按"当前 KRD
        # 不变、只换券"生成当日 policy 目标并**同日**执行，保证触发日双方显式换券。
        policy_roll_target: TargetKRD | None = None
        policy_orders: tuple[Any, ...] | None = None
        pending_cancelled_by_roll = False
        if roll_events and last_snapshot is not None:
            prev_day = calendar.prev_trading_day(d, 1)
            prev_curve = _curve_at(panel, curve_tenors, prev_day)
            current_risk = portfolio_risk(last_snapshot, prev_curve, calendar=calendar, cfg=cfg)
            deferred = pending is not None
            policy_roll_target = TargetKRD(
                as_of=pending.as_of if pending is not None else prev_day,
                krd=dict(current_risk.krd),
                notes=(
                    "滚动政策：触发日同日机械换券（原 pending 取消）"
                    if deferred
                    else "滚动政策：触发日同日机械换券"
                ),
            )
            policy_orders = _policy_roll_orders(roll_events, last_snapshot, curve_d, d, bench_faces)
            if deferred:
                # 滚动日机械换券优先：旧宇宙上生成的 pending 信号已过期，顺延到
                # 下一交易日/下一调仓日由信号重新生成；事件表记录 pending_status=cancelled_by_roll。
                for roll_row in roll_rows:
                    if roll_row["date"] == d:
                        # 没有延期队列：旧 pending 被**取消**，由当日收盘新信号替代。
                        roll_row["pending_status"] = "cancelled_by_roll"
                pending_cancelled_by_roll = True
                pending = None

        # ---- 2) 策略成交：先做完整成交前校验，再提交 ----
        executed = 0
        executed_notional = 0.0
        submitted_ids: list[str] = []
        attempted: list[Any] = []
        rejected_reason = ""
        partial_de_risk = False
        gate_level = 1.0
        gate_target: TargetKRD | None = None
        active_target = pending if pending is not None else policy_roll_target
        face_target = active_target if isinstance(active_target, TargetFaces) else None
        attempts_log: list[dict[str, Any]] = []
        active_proposed = pending_proposed if pending is not None else None
        gate_projected_faces: tuple[tuple[str, float], ...] = ()
        gate_accepted_orders: tuple[dict[str, Any], ...] = ()
        if face_target is not None:
            # 精确基准回放：按目标面值直接构造订单（不经 KRD 求解），仍走同一成交前闸门
            face_orders = _orders_from_faces(face_target.faces, last_snapshot, curve_d, d)
            policy_orders = face_orders
            active_target = TargetKRD(
                as_of=face_target.as_of, krd=dict(benchmark_d.krd), notes=face_target.notes
            )
            pending = None
        if active_target is not None:
            accepted_orders, attempted, reason, partial_de_risk, gate_level = project_and_validate(
                active_target,
                curve=curve_d,
                d=d,
                snapshot=last_snapshot,
                benchmark_d=benchmark_d,
                nav_high_water=nav_high,
                risk_off=risk_off,
                force_min_lot=policy_roll_target is not None,
                forced_orders=policy_orders,
                attempts_log=attempts_log,
                forced_stage=("policy_roll" if policy_roll_target is not None else "target_faces"),
            )
            if accepted_orders is None:
                rejected_reason = reason or "成交前约束不可行"
                rejected_rows.append(
                    {
                        "date": d,
                        "signal_date": active_target.as_of,
                        "reason": rejected_reason,
                        "target_total_dv01": active_target.total_dv01,
                        "n_attempts": len(attempted),
                        "gate_level": gate_level,
                        "risk_off": risk_off,
                        "notes": active_target.notes,
                    }
                )
            else:
                gate_target = attempted[-1] if attempted else active_target
                gate_projected_faces = tuple(
                    (
                        order.bond.code,
                        float(order.face_value) * (1.0 if order.side == "buy" else -1.0),
                    )
                    for order in accepted_orders
                )
                gate_accepted_orders = tuple(
                    {
                        "bond_code": order.bond.code,
                        "side": str(order.side),
                        "face_value": float(order.face_value),
                        "clean_price": float(order.clean_price),
                        "spread_cost": float(order.spread_cost),
                    }
                    for order in accepted_orders
                )
                settle = settlement_for(calendar, d, cfg=cfg)
                for order in accepted_orders:
                    ledger.submit(
                        TradeRecord(
                            trade_id=f"{strategy.name}-{d:%Y%m%d}-{order.bond.code}-{order.side}",
                            bond=order.bond,
                            side=order.side,
                            face_value=order.face_value,
                            clean_price=order.clean_price,
                            signal_date=gate_target.as_of,
                            trade_date=d,
                            settlement_date=settle,
                        )
                    )
                    executed += 1
                    submitted_ids.append(
                        f"{strategy.name}-{d:%Y%m%d}-{order.bond.code}-{order.side}"
                    )
                    executed_notional += order.face_value * order.clean_price / 100.0
                    order_spread[order.bond.code] = (
                        order_spread.get(order.bond.code, 0.0) + order.spread_cost
                    )
                    trade_spread_rows.append(
                        {
                            "trade_id": f"{strategy.name}-{d:%Y%m%d}-{order.bond.code}-{order.side}",
                            "spread_bp": order.spread_bp,
                            "mid_clean_price": order.mid_clean_price,
                            "spread_cost": order.spread_cost,
                        }
                    )
            pending = None

        # ---- 2.5) 真实闸门决策日志（R45：区分取消/替代/真正执行）----
        gate_decision: dict[str, Any] = {
            "date": d,
            "source": "none",
            "signal_date": None,
            "projected_target": None,
            "projected_faces": None,
            "status": "no_action",
            "pending_status": "cancelled_by_roll" if pending_cancelled_by_roll else "none",
            "gate_attempts": len(attempted),
            "executed_trade_ids": tuple(submitted_ids),
            "rejected": bool(rejected_reason),
            "reject_reason": rejected_reason,
            "gate_level": gate_level,
            "risk_off": risk_off,
            "attempts_detail": tuple(attempts_log),
            "accepted_orders": gate_accepted_orders,
            "proposed_target": active_proposed,
        }
        if policy_roll_target is not None:
            gate_decision.update(
                source="policy_roll",
                signal_date=policy_roll_target.as_of,
                projected_target=policy_roll_target,
                projected_faces=tuple(
                    (
                        order.bond.code,
                        float(order.face_value) * (1.0 if order.side == "buy" else -1.0),
                    )
                    for order in (policy_orders or ())
                ),
                accepted_orders=tuple(
                    {
                        "bond_code": order.bond.code,
                        "side": str(order.side),
                        "face_value": float(order.face_value),
                        "clean_price": float(order.clean_price),
                        "spread_cost": float(order.spread_cost),
                    }
                    for order in (policy_orders or ())
                ),
                status=(
                    "executed"
                    if submitted_ids
                    else ("rejected" if rejected_reason else "no_action")
                ),
            )
        elif active_target is not None:
            gate_decision.update(
                source="signal",
                signal_date=active_target.as_of,
                projected_target=attempted[-1] if attempted else active_target,
                projected_faces=gate_projected_faces,
                status=(
                    "partial_de_risk"
                    if partial_de_risk
                    else (
                        "executed"
                        if submitted_ids
                        else ("rejected" if rejected_reason else "no_action")
                    )
                ),
                pending_status="executed" if submitted_ids else "none",
            )
        gate_decisions.append(gate_decision)
        pending_proposed = None

        # ---- 3) 记账与记录 ----
        snapshot = ledger.process_day(d, curve_d)
        bench_snapshot = bench_ledger.process_day(d, curve_d)
        bench_nav_rows[d] = bench_snapshot.nav
        risk = portfolio_risk(snapshot, curve_d, calendar=calendar, cfg=cfg)
        if snapshot.positions:
            report = check_constraints(
                risk=risk, benchmark=benchmark_d, nav_high_water=nav_high, cfg=cfg
            )
        else:
            from ficc.portfolio.constraints import ConstraintReport

            report = ConstraintReport(as_of=d, violations=(), metrics={"n_positions": 0.0})

        nav_rows[d] = snapshot.nav
        nav_high = max(nav_high, snapshot.nav)
        turnover_rows[d] = executed_notional / snapshot.nav if snapshot.nav else 0.0

        exposure_rows.append(
            {
                "date": d,
                "nav": snapshot.nav,
                "cash": snapshot.cash,
                "pending_cash": float(getattr(snapshot, "pending_cash", 0.0)),
                "market_value": risk.market_value,
                "total_dv01": risk.total_dv01,
                "duration": risk.duration,
                "leverage": risk.market_value / snapshot.nav if snapshot.nav else float("nan"),
                "bench_total_dv01": benchmark_d.total_dv01,
                "bench_duration": benchmark_d.duration,
                "bench_nav": bench_snapshot.nav,
                "dv01_ratio": (
                    risk.total_dv01 / benchmark_d.total_dv01
                    if benchmark_d.total_dv01
                    else float("nan")
                ),
                **{f"krd_{int(k) if float(k).is_integer() else k}": v for k, v in risk.krd.items()},
                **{
                    f"bench_krd_{int(k) if float(k).is_integer() else k}": v
                    for k, v in benchmark_d.krd.items()
                },
            }
        )

        cf_all = ledger.cashflows_frame().assign(date=lambda f: pd.to_datetime(f["date"]))
        cf_today = cf_all.loc[cf_all["date"] == pd.Timestamp(d)]
        fee = -float(cf_today.loc[cf_today["kind"] == "fee", "amount"].sum())
        funding_cost = -float(cf_today.loc[cf_today["kind"] == "funding", "amount"].sum())
        spread_cost = float(sum(order_spread.values()))
        cost_rows.append(
            {
                "date": d,
                "fee": fee,
                "funding": funding_cost,
                "spread": spread_cost,
                "bench_spread": bench_spread_day,
                "total": fee + funding_cost + spread_cost,
            }
        )
        order_spread = {bond.code: 0.0 for bond in universe}
        bench_spread_day = 0.0

        constraint_rows.append(
            {
                "date": d,
                "ok": report.ok,
                "n_violations": len(report.violations),
                "violation_codes": ";".join(v.code for v in report.violations),
                "n_positions": len(snapshot.positions),
                "n_trades": executed,
                "rejected": bool(rejected_reason),
                "reject_reason": rejected_reason,
                "gate_mode": "partial_de_risk" if partial_de_risk else "normal",
                "gate_level": gate_level,
                "risk_off": risk_off,
                **{f"m_{k}": v for k, v in report.metrics.items()},
            }
        )

        # 滚动事件 → 成交映射（R38/计划 §12.15：记录事件到成交的关系）
        for roll_row in roll_rows:
            if roll_row["date"] != d or roll_row["status"] != "pending":
                continue
            matched = [
                trade_id
                for trade_id in submitted_ids
                if roll_row["retired_code"] in trade_id or roll_row["issued_code"] in trade_id
            ]
            roll_row["strategy_trade_ids"] = ";".join(matched)
            roll_row["gate_attempts"] = len(attempted)
            if matched:
                roll_row["status"] = "same_day"
            elif rejected_reason:
                roll_row["status"] = "rejected_constraint"
                roll_row["reason"] = rejected_reason
            else:
                roll_row["status"] = "missing_strategy_trade"

        # ---- 4) 下一日信号（只读 <= d 数据）----
        last_snapshot = snapshot
        iso = d.isocalendar()
        week_key = (int(iso[0]), int(iso[1]))
        is_rebalance_day = cfg.backtest.rebalance == "daily" or week_key != last_rebalance_week
        if not is_rebalance_day:
            pending = None
            continue
        last_rebalance_week = week_key
        drawdown_breach = any(v.code == "drawdown_stop" for v in report.violations)
        if drawdown_breach:
            risk_off = True
        elif risk_off:
            risk_off = False

        ctx = MarketContext(
            as_of=d,
            curve=curve_d,
            history=panel.loc[: pd.Timestamp(d)],
            benchmark=benchmark_d,
            nav=snapshot.nav,
            cash=snapshot.cash,
            positions=snapshot.positions,
            cfg=cfg,
        )
        proposed_target = strategy.target_krd(ctx)
        if risk_off:
            target = TargetKRD(as_of=d, krd=dict(benchmark_d.krd), notes="风控熔断：回到基准中性")
        else:
            target = proposed_target
        pending = target
        pending_proposed = proposed_target
        if target is None:
            # 零动作（RL hold）：不产生 pending，也不触发主动交易；滚动政策仍照常执行。
            signal_rows.append(
                {
                    "date": d,
                    "signal_date": None,
                    "target_total_dv01": 0.0,
                    "notes": "no_action（策略返回 None：持仓不动）",
                    "risk_off": risk_off,
                }
            )
        else:
            signal_rows.append(
                {
                    "date": d,
                    "signal_date": target.as_of,
                    "target_total_dv01": target.total_dv01,
                    "notes": target.notes,
                    "risk_off": risk_off,
                }
            )

    positions_df = ledger.snapshots_frame()
    bench_positions_df = bench_ledger.snapshots_frame()
    bench_trade_df = pd.DataFrame(bench_trade_rows)
    trades_df = ledger.trades_frame()
    if trade_spread_rows:
        spread_frame = pd.DataFrame(trade_spread_rows)
        trades_df = trades_df.merge(spread_frame, on="trade_id", how="left")
        trades_df["spread_cost"] = trades_df["spread_cost"].fillna(0.0)
    return BacktestResult(
        strategy=strategy.name,
        start=days[0],
        end=days[-1],
        benchmark_version=benchmark_version,
        rolling_version=roll_policy.version if roll_policy.enabled else "",
        roll_events=pd.DataFrame(roll_rows),
        gate_decisions=gate_decisions,
        nav=pd.Series(nav_rows, name="nav").sort_index(),
        benchmark_nav=pd.Series(bench_nav_rows, name="benchmark_nav").sort_index(),
        exposure=pd.DataFrame(exposure_rows),
        trades=trades_df,
        cashflows=ledger.cashflows_frame(),
        costs=pd.DataFrame(cost_rows),
        turnover=pd.Series(turnover_rows, name="turnover").sort_index(),
        constraints=pd.DataFrame(constraint_rows),
        signals=pd.DataFrame(signal_rows),
        rejected=pd.DataFrame(rejected_rows),
        positions=positions_df,
        benchmark_positions=bench_positions_df,
        benchmark_trades=bench_trade_df,
        benchmark_cashflows=bench_ledger.cashflows_frame(),
    )
