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
    nav_high = float(cfg.backtest.init_cash)
    risk_off = False
    pending: TargetKRD | None = None
    last_snapshot = None
    bench_snapshot = None

    # ------------------------------------------------------------------
    # 成交前完整校验（R23）
    # ------------------------------------------------------------------
    def project_and_validate(
        target: TargetKRD,
        *,
        curve: Curve,
        d: date,
        snapshot: Any,
        benchmark_d: BenchmarkPortfolio,
        nav_high_water: float,
    ) -> tuple[dict[str, float] | None, list[TargetKRD], str]:
        """返回 (可执行面值, 尝试过的目标列表, 拒单原因)。``None`` 表示拒单。"""
        positions_before = () if snapshot is None else snapshot.positions
        current_faces = {p.bond.code: float(p.face_value) for p in positions_before}
        cash_prev = 0.0 if snapshot is None else float(snapshot.cash)
        pending_prev = 0.0 if snapshot is None else float(getattr(snapshot, "pending_cash", 0.0))
        # 上一日未结算成交在今日结算：把它视作可用现金（费用也从这里扣，保守起见不重复扣）
        cash_available = cash_prev + pending_prev

        bench_krd = {float(k): float(v) for k, v in benchmark_d.krd.items()}
        dev = {t: float(target.krd.get(t, bench_krd[t])) - bench_krd[t] for t in bench_krd}
        attempted: list[TargetKRD] = []

        for _iteration in range(6):
            cand = {t: bench_krd[t] + dev[t] for t in bench_krd}
            if min(cand.values()) < 0:
                dev = {t: max(-bench_krd[t], dev[t]) for t in bench_krd}
                cand = {t: bench_krd[t] + dev[t] for t in bench_krd}
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
            # 交易后最终面值（含宇宙外清仓）
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
            pending_after = settlement_cash  # T+1 结算前全部为未结算付款
            risk_proj = project_risk(
                faces_after,
                universe,
                curve,
                calendar=calendar,
                cash=cash_available,
                pending_cash=pending_after,
                nav=cash_available
                + sum(_mv for _mv in _market_values(faces_after, universe, curve, d, cfg).values())
                + pending_after,
                cfg=cfg,
            )
            report = check_constraints(
                risk=risk_proj, benchmark=benchmark_d, nav_high_water=nav_high_water, cfg=cfg
            )
            cash_ok = (
                risk_proj.cash - fee_total
                >= float(cfg.constraints.min_cash_fraction) * risk_proj.nav
            )
            if report.ok and cash_ok:
                return faces_after, attempted, ""
            dev = {t: v * 0.5 for t, v in dev.items()}

        return None, attempted, f"6 次投影仍不可行（{len(attempted)} 次尝试）"

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
                        "face_value": face,
                        "clean_price": clean_exec,
                        "mid_clean_price": _mid,
                        "spread_cost": per100 * face / 100.0,
                    }
                )
            bench_bought = True

        # ---- 2) 策略成交：先做完整成交前校验，再提交 ----
        executed = 0
        executed_notional = 0.0
        rejected_reason = ""
        gate_target: TargetKRD | None = None
        if pending is not None:
            faces_ok, attempted, reason = project_and_validate(
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
                    _spread_cost = abs(delta) / 100.0 * per100
                    order_spread[bond.code] = order_spread.get(bond.code, 0.0) + _spread_cost
                    trade_spread_rows.append(
                        {
                            "trade_id": f"{strategy.name}-{d:%Y%m%d}-{bond.code}-{side}",
                            "spread_bp": _spread_bp,
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
                **{f"m_{k}": v for k, v in report.metrics.items()},
            }
        )

        # ---- 4) 下一日信号（只读 <= d 数据）----
        last_snapshot = snapshot
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
    )
