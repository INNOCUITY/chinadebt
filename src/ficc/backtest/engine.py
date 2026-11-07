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

from dataclasses import dataclass
from datetime import date
from typing import Any, Protocol

import numpy as np
import pandas as pd

from ficc.config import Config, get_config
from ficc.curve.curve import Curve
from ficc.logging import get_logger
from ficc.portfolio.dates import settlement_for
from ficc.portfolio.ledger import Ledger, TradeRecord
from ficc.portfolio.types import BenchmarkPortfolio, MarketContext, TargetKRD

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
    out: dict[date, float] = {}
    for d in days:
        if funding is not None and not funding.empty:
            prior = funding.loc[:d]
            out[d] = float(prior.iloc[-1]) if len(prior) else fallback
        else:
            out[d] = fallback
    return out


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
    from ficc.portfolio.benchmark import build_benchmark, build_universe_bonds, mark_benchmark
    from ficc.portfolio.constraints import check_constraints, portfolio_risk, project_risk
    from ficc.portfolio.construction import build_orders, execution_price
    from ficc.pricing.conventions import accrued_interest

    cfg = cfg or get_config()
    start = start or cfg.backtest.start_date
    end = end or (cfg.backtest.end_date or panel.index.max().date())
    days = list(calendar.trading_days_between(start, end))
    if len(days) < 2:
        raise ValueError(f"回测区间 {start}~{end} 内交易日不足 2 天")

    from ficc.data.schema import CANONICAL_TENORS

    curve_tenors = list(CANONICAL_TENORS)
    curve0 = _curve_at(panel, curve_tenors, days[0])
    universe = build_universe_bonds(curve0, days[0], cfg=cfg)
    benchmark0 = build_benchmark(curve0, days[0], bonds=universe, calendar=calendar, cfg=cfg)
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
    bench_spread_cost = 0.0
    bench_trade_rows: list[dict[str, Any]] = []
    trade_spread_rows: list[dict[str, Any]] = []

    band_face = float(cfg.constraints.min_trade_lot) * float(cfg.constraints.rebalance_band_lots)
    fee_pending_next = 0.0  # 上一交易日成交、今日结算将扣的费用
    last_rebalance_week: tuple[int, int] | None = None
    nav_high = float(cfg.backtest.init_cash)
    risk_off = False
    pending: TargetKRD | None = None
    last_snapshot = None
    bench_snapshot = None

    # ------------------------------------------------------------------
    # 成交前完整校验（R23）
    # ------------------------------------------------------------------
    def _report_severity(report: Any) -> float:
        """违规严重度（用于"降风险订单允许执行"的单调改进判定）。"""
        worst = 0.0
        for violation in report.violations:
            limit = float(violation.limit)
            denom = max(abs(limit), 1.0)
            worst = max(worst, abs(float(violation.value) - limit) / denom)
        return worst

    def _coupon_in_window(d: date, settled_faces: dict[str, float]) -> float:
        """投影窗口 (d, 下一交易日] 内应付票息（按**日初已结算面值**归属，R24）。"""
        from ficc.pricing.conventions import coupon_dates

        next_day = calendar.next_trading_day(d, 1)
        total = 0.0
        for bond in universe:
            settled = float(settled_faces.get(bond.code, 0.0))
            if settled <= 0.0:
                continue
            for coupon_day in coupon_dates(bond):
                if d < coupon_day <= next_day:
                    total += settled / 100.0 * float(bond.coupon_pct) / float(bond.freq)
                    break
        return total

    def project_and_validate(
        target: TargetKRD,
        *,
        curve: Curve,
        d: date,
        snapshot: Any,
        benchmark_d: BenchmarkPortfolio,
        nav_high_water: float,
        risk_off: bool = False,
    ) -> tuple[dict[str, float] | None, list[TargetKRD], str, bool]:
        """成交前完整校验（R23 补查修复版）。

        返回 ``(可执行面值, 尝试过的目标列表, 拒单/降险原因, 是否降险执行)``；
        ``None`` 表示拒单。现金预算按**各结算时点**汇总：

        - 今日结算：上一交易日 pending 全部到账，同时扣其费用；
        - 今日新成交：T+1 结算，形成新的待结算付款/收款；
        - 明日（下一交易日）：票息应收（按日初已结算面值）、新成交费用、未结算付款、
          以及负现金的一天融资；
        - 用**结算完成后的现金与 NAV** 做杠杆/单券权重/现金下限/NAV 分母的统一校验。
        """
        positions_before = () if snapshot is None else snapshot.positions
        current_faces = {p.bond.code: float(p.face_value) for p in positions_before}
        cash_prev = float(cfg.backtest.init_cash) if snapshot is None else float(snapshot.cash)
        pending_prev = 0.0 if snapshot is None else float(getattr(snapshot, "pending_cash", 0.0))
        settled_faces = (
            {}
            if snapshot is None
            else {
                p.bond.code: float(getattr(p, "settled_face", p.face_value))
                for p in positions_before
            }
        )
        cash_today = cash_prev + pending_prev  # 今日结算完成后可用现金
        coupon_next = _coupon_in_window(d, settled_faces)
        rate_pct = float(funding_map.get(d, cfg.costs.repo_rate_fallback_const * 100.0))

        # 当前组合的违规基线（被动超限时允许"只降不增"的降风险订单）
        mv_current = _market_values(current_faces, universe, curve, d, cfg)
        nav_current = cash_today - fee_pending_next + sum(mv_current.values())
        current_risk = project_risk(
            current_faces,
            universe,
            curve,
            calendar=calendar,
            cash=cash_today - fee_pending_next,
            pending_cash=0.0,
            nav=nav_current,
            cfg=cfg,
        )
        current_report = check_constraints(
            risk=current_risk, benchmark=benchmark_d, nav_high_water=nav_high_water, cfg=cfg
        )
        current_severity = _report_severity(current_report)
        current_codes = {v.code for v in current_report.violations}

        bench_krd = {float(k): float(v) for k, v in benchmark_d.krd.items()}
        dev = {t: float(target.krd.get(t, bench_krd[t])) - bench_krd[t] for t in bench_krd}
        attempted: list[TargetKRD] = []
        # 风控熔断时按风险规模阶梯降档：即使基准目标本身不可行，也要能执行降低风险的订单
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
                    min_trade_face=band_face,
                    calendar=calendar,
                    cfg=cfg,
                )
                faces_after = dict(current_faces)
                settlement_cash = 0.0
                fee_total = 0.0
                for order in orders:
                    signed = 1.0 if order.side == "buy" else -1.0
                    faces_after[order.bond.code] = (
                        faces_after.get(order.bond.code, 0.0) + signed * order.face_value
                    )
                    settle = settlement_for(calendar, d, cfg=cfg)
                    ai = accrued_interest(order.bond, settle, cfg=cfg)
                    amount = (order.clean_price + ai) * order.face_value / 100.0
                    settlement_cash += -amount if order.side == "buy" else amount
                    fee_total += _fee_estimate(order.face_value, order.clean_price, cfg)
                faces_after = {k: max(v, 0.0) for k, v in faces_after.items()}

                # 结算完成后（d+1）的现金：今日到账 pending − 今日费用 + 新成交待结算 + 票息 − 融资
                cash_end = cash_today - fee_pending_next + settlement_cash - fee_total + coupon_next
                if cash_end < 0.0:
                    cash_end += cash_end * rate_pct / 100.0 / 365.0  # 一天融资（负值）
                mv_after = _market_values(faces_after, universe, curve, d, cfg)
                nav_after = cash_end + sum(mv_after.values())
                risk_proj = project_risk(
                    faces_after,
                    universe,
                    curve,
                    calendar=calendar,
                    cash=cash_end,
                    pending_cash=0.0,
                    nav=nav_after,
                    cfg=cfg,
                )
                report = check_constraints(
                    risk=risk_proj,
                    benchmark=benchmark_d,
                    nav_high_water=nav_high_water,
                    cfg=cfg,
                )
                if report.ok:
                    return faces_after, attempted, "", False
                # 降风险订单：只允许"当前已违规 → 候选不引入新违规类型且严重度下降"，
                # 避免把"从空仓建到超限组合"误判成降风险（R23 补查边界）。
                cand_codes = {v.code for v in report.violations}
                if (
                    current_severity > 0.0
                    and cand_codes <= current_codes
                    and _report_severity(report) < current_severity * 0.999
                ):
                    return faces_after, attempted, "partial_de_risk", True
                dev = {t: value * 0.5 for t, value in dev.items()}

        return None, attempted, f"6 次投影仍不可行（{len(attempted)} 次尝试）", False

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
        benchmark_d = mark_benchmark(
            benchmark0, bonds=universe, curve=curve_d, as_of=d, calendar=calendar, cfg=cfg
        )

        # ---- 1) 基准买入持有（与策略同成本/价差/日期口径）----
        if not bench_bought and d == days[1]:
            settle = settlement_for(calendar, d, cfg=cfg)
            for bond in universe:
                face = float(benchmark0.faces.get(bond.code, 0.0))
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
                bench_spread_cost += per100 * face / 100.0
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

        # ---- 2) 策略成交：先做完整成交前校验，再提交 ----
        executed = 0
        executed_notional = 0.0
        rejected_reason = ""
        partial_de_risk = False
        gate_target: TargetKRD | None = None
        if pending is not None:
            faces_ok, attempted, reason, partial_de_risk = project_and_validate(
                pending,
                curve=curve_d,
                d=d,
                snapshot=last_snapshot,
                benchmark_d=benchmark_d,
                nav_high_water=nav_high,
            )
            if faces_ok is None:
                rejected_reason = reason or "成交前约束不可行"
                rejected_rows.append(
                    {
                        "date": d,
                        "signal_date": pending.as_of,
                        "reason": rejected_reason,
                        "target_total_dv01": pending.total_dv01,
                        "n_attempts": len(attempted),
                        "notes": pending.notes,
                    }
                )
                fee_pending_next = 0.0  # 拒单：没有新费用
            else:
                gate_target = attempted[-1]
                current_faces = (
                    {}
                    if last_snapshot is None
                    else {p.bond.code: p.face_value for p in last_snapshot.positions}
                )
                settle = settlement_for(calendar, d, cfg=cfg)
                for bond in universe:
                    target_face = float(faces_ok.get(bond.code, 0.0))
                    current_face = float(current_faces.get(bond.code, 0.0))
                    delta = target_face - current_face
                    if abs(delta) < band_face - 1e-9:
                        continue
                    side = "buy" if delta > 0 else "sell"
                    clean_exec, _mid, _spread_bp, per100 = execution_price(
                        bond, curve_d, side=side, calendar=calendar, cfg=cfg
                    )
                    ledger.submit(
                        TradeRecord(
                            trade_id=f"{strategy.name}-{d:%Y%m%d}-{bond.code}-{side}",
                            bond=bond,
                            side=side,
                            face_value=abs(delta),
                            clean_price=clean_exec,
                            signal_date=gate_target.as_of,
                            trade_date=d,
                            settlement_date=settle,
                        )
                    )
                    executed += 1
                    executed_notional += abs(delta) * clean_exec / 100.0
                    fee_pending_next += _fee_estimate(abs(delta), clean_exec, cfg)
                    _spread_cost = abs(delta) / 100.0 * per100
                    order_spread[bond.code] = order_spread.get(bond.code, 0.0) + _spread_cost
                    trade_spread_rows.append(
                        {
                            "trade_id": f"{strategy.name}-{d:%Y%m%d}-{bond.code}-{side}",
                            "spread_bp": _spread_bp,
                            "mid_clean_price": _mid,
                            "spread_cost": _spread_cost,
                        }
                    )
            pending = None

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
                "bench_spread": bench_spread_cost if d == days[1] else 0.0,
                "total": fee + funding_cost + spread_cost,
            }
        )
        order_spread = {bond.code: 0.0 for bond in universe}

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
                **{f"m_{k}": v for k, v in report.metrics.items()},
            }
        )

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
        if risk_off:
            target = TargetKRD(as_of=d, krd=dict(benchmark_d.krd), notes="风控熔断：回到基准中性")
        else:
            target = strategy.target_krd(ctx)
        pending = target
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
