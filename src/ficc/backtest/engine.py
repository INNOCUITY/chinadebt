"""日频回测引擎：策略 → 目标 KRD → 组合构建 → T+1 成交 → 账本 → 记录。

时序纪律（与 `docs/03_methodology.md` §11.1 一致）
--------------------------------------------------
- t 日收盘后生成信号（策略只能看到 `history <= t`）；
- **t+1 日成交**（执行价 = t+1 收盘曲线），结算日 = 日历 T+1；
- 每日账本 `process_day` 负责票息/本金/融资/结算与估值。

这一层只做编排，不实现任何金融口径：
- 基准组合：`portfolio/benchmark.py`
- 目标 KRD → 面值/订单：`portfolio/construction.py`
- 组合风险与约束：`portfolio/constraints.py`
- 记账与现金流：`portfolio/ledger.py`
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
from ficc.portfolio.ledger import Ledger, TradeRecord
from ficc.portfolio.types import BenchmarkPortfolio, MarketContext, TargetKRD

log = get_logger("backtest.engine")


class StrategyLike(Protocol):
    """策略只需实现这一个方法（与 `ficc.strategy.base.Strategy` 兼容）。"""

    name: str

    def target_krd(self, ctx: MarketContext) -> TargetKRD: ...


@dataclass
class BacktestResult:
    """一次回测的全部记录（每项都是可直接落盘的 DataFrame/Series）。"""

    strategy: str
    start: date
    end: date
    nav: pd.Series
    exposure: pd.DataFrame
    trades: pd.DataFrame
    cashflows: pd.DataFrame
    costs: pd.DataFrame
    turnover: pd.Series
    constraints: pd.DataFrame
    signals: pd.DataFrame

    def to_frames(self) -> dict[str, pd.DataFrame | pd.Series]:
        return {
            "nav": self.nav,
            "exposure": self.exposure,
            "trades": self.trades,
            "cashflows": self.cashflows,
            "costs": self.costs,
            "turnover": self.turnover,
            "constraints": self.constraints,
            "signals": self.signals,
        }


def _curve_at(panel: pd.DataFrame, tenors: list[float], d: date) -> Curve:
    row = panel.loc[pd.Timestamp(d)].reindex(tenors)
    if row.isna().any():
        raise ValueError(f"{d} 曲线存在缺失期限：{row[row.isna()].index.tolist()}")
    return Curve(as_of=d, tenors=np.asarray(tenors), rates=tuple(row.values), method="hermite")


def _funding_map(funding: pd.Series | None, days: list[date], cfg: Config) -> dict[date, float]:
    """把 FDR007（%）映射到交易日；缺失时用前值，最后回退配置常量。"""
    fallback = cfg.costs.repo_rate_fallback_const * 100.0
    out: dict[date, float] = {}
    for d in days:
        if funding is not None and not funding.empty:
            prior = funding.loc[:d]
            out[d] = float(prior.iloc[-1]) if len(prior) else fallback
        else:
            out[d] = fallback
    return out


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
    """跑一个策略的日频回测。

    Parameters
    ----------
    strategy : StrategyLike
        实现 ``target_krd(ctx) -> TargetKRD`` 的对象；只允许读 ``ctx.history``（已截断）。
    panel : DataFrame
        index=日期、columns=期限（年）、values=收益率(%)，**全样本**；引擎负责按日截断。
    calendar : TradingCalendar
        实测 CN_IB 日历（决定交易日与结算日）。
    funding : Series | None
        index=日期 的融资利率（%）；None 时用 ``cfg.costs.repo_rate_fallback_const``。
    """
    from ficc.portfolio.benchmark import build_benchmark, build_universe_bonds
    from ficc.portfolio.constraints import check_constraints, portfolio_risk
    from ficc.portfolio.construction import build_orders, krd_matrix, solve_faces
    from ficc.pricing.analytics import mod_duration
    from ficc.pricing.bond import dirty_price_from_ytm

    cfg = cfg or get_config()
    start = start or cfg.backtest.start_date
    end = end or (cfg.backtest.end_date or panel.index.max().date())
    days = list(calendar.trading_days_between(start, end))
    if len(days) < 2:
        raise ValueError(f"回测区间 {start}~{end} 内交易日不足 2 天")

    # 曲线用**全期限**（bootstrap 需要短端锚点）；投资宇宙只用 portfolio.universe
    from ficc.data.schema import CANONICAL_TENORS

    curve_tenors = list(CANONICAL_TENORS)
    curve0 = _curve_at(panel, curve_tenors, days[0])
    universe = build_universe_bonds(curve0, days[0], cfg=cfg)
    universe_tenor = {
        bond.code: float(t) for bond, t in zip(universe, cfg.portfolio.universe, strict=True)
    }
    funding_map = _funding_map(funding, days, cfg)
    lag = int(cfg.pricing.settlement_lag_days)

    ledger = Ledger(
        calendar,
        initial_cash=cfg.backtest.init_cash,
        funding_rate_pct=funding_map[days[0]],
        funding_rate_by_date=funding_map,
        cfg=cfg,
    )

    nav_rows: dict[date, float] = {}
    exposure_rows: list[dict[str, Any]] = []
    cost_rows: list[dict[str, Any]] = []
    constraint_rows: list[dict[str, Any]] = []
    turnover_rows: dict[date, float] = {}
    signal_rows: list[dict[str, Any]] = []

    nav_high = float(cfg.backtest.init_cash)
    risk_off = False
    pending: TargetKRD | None = None
    last_snapshot = None

    # ------------------------------------------------------------------
    # 目标投影：把策略 tilt 收缩到"构建后仍满足约束"的可行域
    # ------------------------------------------------------------------
    def project_target(
        target: TargetKRD,
        benchmark_d: BenchmarkPortfolio,
        curve: Curve,
        d: date,
        nav: float,
    ) -> TargetKRD:
        """构造后真实风险校验：4 只券表达 9 个桶会互相牵连，只看目标会低估偏离。"""
        tol = float(cfg.constraints.relative_krd_tol)
        budget = tol * benchmark_d.total_dv01
        dur_tol = float(cfg.constraints.duration_tol)
        A = krd_matrix(universe, curve, cfg=cfg)
        key_tenors = list(benchmark_d.krd.keys())
        settle = calendar.settlement_date(d, lag)
        durations: dict[float, float] = {}
        dirty: dict[float, float] = {}
        for bond in universe:
            tenor = universe_tenor[bond.code]
            ytm = float(curve.rate((bond.maturity_date - settle).days / 365.25))
            durations[tenor] = float(mod_duration(bond, settle, ytm))
            dirty[tenor] = float(dirty_price_from_ytm(bond, settle, ytm, cfg=cfg))

        bench_krd = {float(k): float(v) for k, v in benchmark_d.krd.items()}
        dev = {t: float(target.krd.get(t, bench_krd[t])) - bench_krd[t] for t in bench_krd}
        for iteration in range(6):
            cand = {t: bench_krd[t] + dev[t] for t in bench_krd}
            if min(cand.values()) < 0:
                dev = {t: max(-bench_krd[t], dev[t]) for t in bench_krd}
                cand = {t: bench_krd[t] + dev[t] for t in bench_krd}
            faces = solve_faces(TargetKRD(as_of=target.as_of, krd=cand), universe, curve, cfg=cfg)
            f = np.array([float(v) for v in faces.values()], dtype=float) / 100.0
            krd_vec = A @ f
            krd_map = {float(k): float(v) for k, v in zip(key_tenors, krd_vec, strict=True)}
            total = float(sum(krd_map.values()))
            mv = {t: float(faces[code]) / 100.0 * dirty[t] for code, t in universe_tenor.items()}
            mv_total = float(sum(mv.values()))
            dur = sum(mv[t] * durations[t] for t in mv) / mv_total if mv_total > 0 else 0.0

            max_dev = max(abs(krd_map[t] - bench_krd[t]) for t in bench_krd)
            dur_ok = (
                benchmark_d.duration <= 0
                or abs(dur - benchmark_d.duration) <= dur_tol * benchmark_d.duration
            )
            total_ok = (
                float(cfg.constraints.total_dv01_ratio_min) * benchmark_d.total_dv01
                <= total
                <= float(cfg.constraints.total_dv01_ratio_max) * benchmark_d.total_dv01
            )
            weight_limit = min(
                float(cfg.portfolio.max_single_bond_weight),
                float(cfg.risk.limits.max_single_bond_weight),
            )
            max_weight = max(mv.values()) / nav if nav > 0 and mv else 0.0
            weight_ok = max_weight <= weight_limit + 1e-6
            if max_dev <= budget and dur_ok and total_ok and weight_ok:
                # 返回**可达的** KRD（A@faces 的结果），保证 build_orders 能精确复现同一组面值
                note = f"{target.notes} [引擎约束投影]".strip() if iteration > 0 else target.notes
                return TargetKRD(as_of=target.as_of, krd=krd_map, notes=note.strip())
            dev = {t: v * 0.5 for t, v in dev.items()}

        return TargetKRD(
            as_of=target.as_of, krd=bench_krd, notes=f"{target.notes} [引擎收缩到基准]".strip()
        )

    for d in days:
        curve_d = _curve_at(panel, curve_tenors, d)
        benchmark_d = build_benchmark(curve_d, d, bonds=universe, cfg=cfg)

        # ---- 1) 执行昨日信号（T+1）----
        executed = 0
        executed_notional = 0.0
        if pending is not None:
            positions_before = () if last_snapshot is None else last_snapshot.positions
            orders = build_orders(pending, positions_before, universe, curve_d, cfg=cfg)
            for order in orders:
                trade = TradeRecord(
                    trade_id=f"{strategy.name}-{d:%Y%m%d}-{order.bond.code}-{order.side}",
                    bond=order.bond,
                    side=order.side,
                    face_value=order.face_value,
                    clean_price=order.clean_price,
                    signal_date=pending.as_of,
                    trade_date=d,
                    settlement_date=calendar.settlement_date(d, lag),
                )
                ledger.submit(trade)
                executed += 1
                executed_notional += order.face_value * order.clean_price / 100.0
            pending = None

        # ---- 2) 记账、估值、风险与约束 ----
        snapshot = ledger.process_day(d, curve_d)
        risk = portfolio_risk(snapshot, curve_d, cfg=cfg)
        if snapshot.positions:
            report = check_constraints(
                risk=risk, benchmark=benchmark_d, nav_high_water=nav_high, cfg=cfg
            )
        else:
            # 首日/空仓：没有持仓时谈不上"相对基准中性"，记为预热期而不是违规
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
                "pending_cash": getattr(snapshot, "pending_cash", 0.0),
                "market_value": risk.market_value,
                "total_dv01": risk.total_dv01,
                "duration": risk.duration,
                "leverage": risk.market_value / snapshot.nav if snapshot.nav else float("nan"),
                "bench_total_dv01": benchmark_d.total_dv01,
                "bench_duration": benchmark_d.duration,
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

        # ---- 3) 成本（当日实际发生的费用与融资）----
        cf_all = ledger.cashflows_frame()
        cf_all = cf_all.assign(date=pd.to_datetime(cf_all["date"]))
        cf_today = cf_all.loc[cf_all["date"] == pd.Timestamp(d)]
        fee = -float(cf_today.loc[cf_today["kind"] == "fee", "amount"].sum())
        funding_cost = -float(cf_today.loc[cf_today["kind"] == "funding", "amount"].sum())
        cost_rows.append(
            {"date": d, "fee": fee, "funding": funding_cost, "total": fee + funding_cost}
        )

        constraint_rows.append(
            {
                "date": d,
                "ok": report.ok,
                "n_violations": len(report.violations),
                "violation_codes": ";".join(v.code for v in report.violations),
                "n_positions": len(snapshot.positions),
                "n_trades": executed,
                **{f"m_{k}": v for k, v in report.metrics.items()},
            }
        )

        # ---- 4) 生成下一日信号（只读 <= d 的数据）----
        last_snapshot = snapshot
        drawdown_breach = any(v.code == "drawdown_stop" for v in report.violations)
        if drawdown_breach:
            risk_off = True
        elif risk_off and not drawdown_breach:
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
            target = project_target(strategy.target_krd(ctx), benchmark_d, curve_d, d, snapshot.nav)
        pending = target
        signal_rows.append(
            {
                "date": d,
                "target_total_dv01": target.total_dv01,
                "notes": target.notes,
                "risk_off": risk_off,
            }
        )

    return BacktestResult(
        strategy=strategy.name,
        start=days[0],
        end=days[-1],
        nav=pd.Series(nav_rows, name="nav").sort_index(),
        exposure=pd.DataFrame(exposure_rows),
        trades=ledger.trades_frame(),
        cashflows=ledger.cashflows_frame(),
        costs=pd.DataFrame(cost_rows),
        turnover=pd.Series(turnover_rows, name="turnover").sort_index(),
        constraints=pd.DataFrame(constraint_rows),
        signals=pd.DataFrame(signal_rows),
    )
