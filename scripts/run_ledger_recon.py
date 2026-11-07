#!/usr/bin/env python3
"""账本 + 独立对账的真实数据端到端验证（Phase 2 第 1–2 步）。

场景（真实曲线 + 真实 FDR007 融资 + 实测 CN_IB 日历）：

    2019-01-29  买入 8000 万 10Y 券（signal 前一天）
    2020-04-01  卖出 3000 万（部分减仓）＋ 买入 5000 万 7Y 券（新券换入 = 显式成交）
    2020-12-31  持有到期末

验证内容：
1. 账本逐日跑通：三层日期、条款锁定、跨付息、部分减仓、新券换入、融资计提；
2. **独立对账**：现金 / 持仓 / NAV / PnL 双路径 / 全价 / （可选）归因闭合；
3. **零成本对照**：把所有费用与融资设为 0 再跑一遍，两账净值差应恰等于成本合计。

用法::

    PYTHONPATH=src .venv/bin/python scripts/run_ledger_recon.py
    # 失败时 exit 1，可直接用于 CI
"""

from __future__ import annotations

import sys
from datetime import date
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from ficc.config import Config, get_config  # noqa: E402
from ficc.curve.curve import Curve  # noqa: E402
from ficc.data.loader import load_curve_long, load_funding_series  # noqa: E402
from ficc.data.schema import CANONICAL_TENORS, BondStatic  # noqa: E402
from ficc.logging import get_logger  # noqa: E402
from ficc.portfolio.ledger import Ledger, TradeRecord  # noqa: E402
from ficc.portfolio.reconcile import reconcile_all  # noqa: E402
from ficc.pricing.bond import dirty_price_from_ytm  # noqa: E402
from ficc.pricing.conventions import accrued_interest  # noqa: E402
from ficc.utils.calendar import TradingCalendar  # noqa: E402

log = get_logger("ledger_recon")

# 场景参数（与文档 §10 一致）
START = date(2019, 1, 28)
END = date(2020, 12, 31)
INITIAL_CASH = 60_000_000.0  # 有意低于买入面值，让现金为负以检验融资成本路径
FACE_A = 80_000_000.0
FACE_A_SELL = 30_000_000.0
FACE_B = 50_000_000.0

BOND_A = BondStatic(
    code="SYN-10Y-2019",
    issue_date=date(2019, 1, 28),
    maturity_date=date(2029, 1, 28),
    coupon_pct=3.20,
    freq=2,
)
BOND_B = BondStatic(
    code="SYN-7Y-2020",
    issue_date=date(2020, 4, 1),
    maturity_date=date(2027, 4, 1),
    coupon_pct=2.80,
    freq=1,
)


# ---------------------------------------------------------------------------
# 数据准备
# ---------------------------------------------------------------------------
def load_market(cfg: Config) -> tuple[pd.DataFrame, pd.Series, TradingCalendar]:
    long = load_curve_long(cfg=cfg)
    panel = (
        long.pivot(index="date", columns="tenor", values="ytm_pct")
        .sort_index()
        .reindex(columns=list(CANONICAL_TENORS))
    )
    funding = load_funding_series(cfg=cfg).set_index("date")["FDR007"].sort_index()
    calendar = TradingCalendar.from_dates(panel.index)
    return panel, funding, calendar


def curve_at(panel: pd.DataFrame, d: date) -> Curve:
    row = panel.loc[pd.Timestamp(d)].reindex(CANONICAL_TENORS)
    if row.isna().any():
        raise ValueError(f"{d} 曲线存在缺失期限")
    return Curve(
        as_of=d, tenors=np.asarray(CANONICAL_TENORS), rates=tuple(row.values), method="hermite"
    )


def remaining_years(bond: BondStatic, d: date) -> float:
    return (bond.maturity_date - d).days / 365.25


def quoted_clean_price(bond: BondStatic, d: date, curve: Curve, settle: date) -> float:
    """按曲线 YTM 报价：clean = dirty(ytm) − AI。"""
    ytm = float(curve.rate(remaining_years(bond, settle)))
    dirty = dirty_price_from_ytm(bond, settle, ytm)
    return dirty - accrued_interest(bond, settle)


def funding_by_date(funding: pd.Series, days: list[date], cfg: Config) -> dict[date, float]:
    """把 FDR007（%）按交易日映射；缺失用配置常量（小数 → 百分数）。"""
    out: dict[date, float] = {}
    fallback = cfg.costs.repo_rate_fallback_const * 100.0
    for d in days:
        if d in funding.index:
            out[d] = float(funding.loc[d])
        else:
            prior = funding.loc[:d]
            out[d] = float(prior.iloc[-1]) if len(prior) else fallback
    return out


# ---------------------------------------------------------------------------
# 场景构建与运行
# ---------------------------------------------------------------------------
def build_trades(calendar: TradingCalendar, panel: pd.DataFrame) -> list[TradeRecord]:
    lag = 1

    def _trade(tid, bond, side, face, trade_date: date, signal_date: date) -> TradeRecord:
        settle = calendar.settlement_date(trade_date, lag)
        price = quoted_clean_price(bond, trade_date, curve_at(panel, trade_date), settle)
        return TradeRecord(
            trade_id=tid,
            bond=bond,
            side=side,
            face_value=face,
            clean_price=round(price, 6),
            signal_date=signal_date,
            trade_date=trade_date,
            settlement_date=settle,
        )

    return [
        _trade("T1-buy-10Y", BOND_A, "buy", FACE_A, date(2019, 1, 29), date(2019, 1, 28)),
        _trade("T2-sell-10Y", BOND_A, "sell", FACE_A_SELL, date(2020, 4, 1), date(2020, 3, 31)),
        _trade("T3-buy-7Y", BOND_B, "buy", FACE_B, date(2020, 4, 1), date(2020, 3, 31)),
    ]


def run(cfg: Config, *, zero_cost: bool = False) -> dict:
    panel, funding, calendar = load_market(cfg)
    days = list(calendar.trading_days_between(START, END))
    funding_map = funding_by_date(funding, days, cfg)
    trades = build_trades(calendar, panel)
    trade_by_date: dict[date, list[TradeRecord]] = {}
    for t in trades:
        trade_by_date.setdefault(t.trade_date, []).append(t)

    ledger = Ledger(
        calendar,
        initial_cash=INITIAL_CASH,
        funding_rate_pct=0.0 if zero_cost else float(funding_map[days[0]]),
        funding_rate_by_date={d: (0.0 if zero_cost else r) for d, r in funding_map.items()},
        cfg=cfg,
    )
    for d in days:
        for t in trade_by_date.get(d, []):
            ledger.submit(t)
        ledger.process_day(d, curve_at(panel, d))

    return {
        "calendar": calendar,
        "trades": ledger.trades_frame(),
        "cashflows": ledger.cashflows_frame(),
        "snapshots": ledger.snapshots_frame(),
        "nav": ledger.nav_series(),
    }


def _zero_cost_cfg(cfg: Config) -> Config:
    """构造零成本配置：手续费/结算费/融资利率全部归零（用于对照实验）。"""
    raw = cfg.model_dump(mode="python")
    raw["costs"]["fee_bp"] = 0.0
    raw["costs"]["settlement_fee_cny_per_trade"] = 0.0
    raw["costs"]["spread_bp_active"] = 0.0
    raw["costs"]["spread_bp_illiquid"] = 0.0
    raw["costs"]["repo_rate_fallback_const"] = 0.0
    return Config.model_validate(raw)


def main() -> int:
    cfg = get_config()
    out_dir = cfg.paths.abs("reports")
    out_dir.mkdir(parents=True, exist_ok=True)

    log.info("运行账本场景（含成本）…")
    base = run(cfg)
    report = reconcile_all(
        cashflows=base["cashflows"],
        trades=base["trades"],
        snapshots=base["snapshots"],
        nav=base["nav"],
        calendar=base["calendar"],
    )

    log.info("运行零成本对照账本…")
    zero_cfg = _zero_cost_cfg(cfg)
    zero = run(zero_cfg, zero_cost=True)
    zero_nav = zero["nav"]

    costs = base["cashflows"].loc[base["cashflows"]["kind"].isin(["funding", "fee"])]
    total_cost = -float(costs["amount"].sum())
    funding_cost = -float(costs.loc[costs["kind"] == "funding", "amount"].sum())
    fee_cost = -float(costs.loc[costs["kind"] == "fee", "amount"].sum())
    nav_gap = float(base["nav"].iloc[-1] - zero_nav.iloc[-1])

    # 独立校验：含成本账本的期末净值应比零成本账本**低**成本合计（nav_gap 为负）
    cost_ok = abs(nav_gap + total_cost) < 1.0
    has_funding = len(costs[costs["kind"] == "funding"]) > 0

    print("\n=== 独立对账报告 ===")
    frame = report.to_frame()
    with pd.option_context("display.width", 200):
        print(frame.to_string(index=False))
    print(
        f"\n零成本对照：融资成本 {funding_cost:,.2f} 元 + 交易费用 {fee_cost:,.2f} 元 = 合计 {total_cost:,.2f} 元；"
        f"两账期末 NAV 差 {nav_gap:,.2f} 元（容差 1 元，{'通过' if cost_ok else '不通过'}）"
    )
    print(f"融资路径已被触发：{has_funding}（{'是' if has_funding else '否'}）")

    # 产物落盘（供报告与复核引用）
    report.to_frame().to_csv(
        out_dir / "reconciliation_report.csv", index=False, encoding="utf-8-sig"
    )
    base["nav"].rename("nav").to_csv(out_dir / "ledger_nav.csv", encoding="utf-8-sig")
    base["cashflows"].to_csv(out_dir / "ledger_cashflows.csv", index=False, encoding="utf-8-sig")
    base["trades"].to_csv(out_dir / "ledger_trades.csv", index=False, encoding="utf-8-sig")
    log.info("产物: %s", out_dir / "reconciliation_report.csv")

    if not report.passed:
        log.error("对账未通过，详见上表")
        return 1
    if not cost_ok:
        log.error("零成本对照未通过：净值差 %.2f != 成本合计 %.2f", nav_gap, total_cost)
        return 1
    if not has_funding:
        log.error("融资路径未被触发：请调低初始现金，确保现金为负以检验融资成本")
        return 1
    log.info("对账全部通过（含零成本对照与融资路径覆盖）")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
