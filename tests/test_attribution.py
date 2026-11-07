"""固定持仓账本归因测试（Phase 2 第 5 步，task-19）。

覆盖 7 类场景（离线、确定性）：

1. 合成平曲线：carry 捕获票息应计，曲线因子 ≈ 0；
2. 平行上移 10bp：价格 PnL 全归 level，slope/curvature/other ≈ 0；
3. 陡峭化/平坦化与蝶式：按因子定义构造 ``curve_d``，主要落点正确、四因子和恒等；
4. 跨付息：票息现金增加、dirty 下降，桥接残差 < 1e-6，票息不重复计；
5. 减仓：卖出 40% 面值，桥接/逐券/因子三层恒等，slippage 独立披露且 ≈ 0；
6. 篡改检测：coupon 金额 +1 元 → 桥接残差 > 容差（证明不是自证）；
7. 无未来信息：替换 d 之后的 panel 数据，d 之前的归因输出逐位不变。

所有账本都用 ``Ledger`` + 真实 CN_IB 日历 + 合成曲线独立构造，不依赖引擎。
"""

from __future__ import annotations

from datetime import date, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from ficc.attribution import (
    BONDS_COLUMNS,
    DAILY_COLUMNS,
    FACTORS_COLUMNS,
    attribute_fixed_holdings,
)
from ficc.config import Config, get_config
from ficc.curve.curve import Curve
from ficc.data.schema import CANONICAL_TENORS, BondStatic
from ficc.portfolio import Ledger, TradeRecord
from ficc.pricing.bond import dirty_price_from_ytm
from ficc.pricing.conventions import accrued_interest
from ficc.utils.calendar import TradingCalendar

TENORS = np.asarray(sorted(CANONICAL_TENORS), dtype=float)
BRIDGE_TOL = 1e-6


# ---------------------------------------------------------------------------
# 夹具与工具（风格与 tests/test_ledger.py 一致，自造 mini ledger）
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def calendar() -> TradingCalendar:
    path = Path(__file__).resolve().parents[1] / "data" / "processed" / "curve_long.parquet"
    if not path.exists():
        pytest.skip(f"缺少中债实测数据 {path}，无法构造 CN_IB 日历")
    frame = pd.read_parquet(path)
    return TradingCalendar.from_dates(pd.to_datetime(frame["date"]).dt.date)


def zero_cost_cfg() -> Config:
    cfg = get_config()
    costs = cfg.costs.model_copy(update={"fee_bp": 0.0, "settlement_fee_cny_per_trade": 0.0})
    return cfg.model_copy(update={"costs": costs})


def flat_curve(as_of: date, level: float = 2.0) -> Curve:
    return Curve(
        as_of=as_of, tenors=TENORS.copy(), rates=np.full_like(TENORS, level), method="linear"
    )


def curve_with(as_of: date, changes: dict[float, float], base: float = 2.0) -> Curve:
    """基准水平 ``base`` + 指定期限节点变化 ``changes``（单位：bp）。"""
    rates = np.full_like(TENORS, base)
    for tenor, delta_bp in changes.items():
        hits = np.flatnonzero(np.isclose(TENORS, tenor))
        assert len(hits) == 1, f"测试构造错误：期限 {tenor} 不在标准节点"
        rates[hits[0]] += delta_bp / 100.0
    return Curve(as_of=as_of, tenors=TENORS.copy(), rates=rates, method="linear")


def panel_from_curves(curves: dict[date, Curve]) -> pd.DataFrame:
    data = {day: np.asarray(curve.rate(TENORS), dtype=float) for day, curve in curves.items()}
    frame = pd.DataFrame(data).T
    frame.columns = [float(tenor) for tenor in TENORS]
    frame.index = pd.Index(sorted(curves), name="date")
    return frame


def make_bond(
    code: str,
    issue: date,
    maturity: date,
    coupon_pct: float = 2.0,
    freq: int = 2,
) -> BondStatic:
    return BondStatic(
        code=code, issue_date=issue, maturity_date=maturity, coupon_pct=coupon_pct, freq=freq
    )


def make_trade(
    calendar: TradingCalendar,
    cfg: Config,
    *,
    trade_id: str,
    bond: BondStatic,
    trade_date: date,
    side: str = "buy",
    face_value: float = 1_000_000.0,
    clean_price: float = 100.0,
) -> TradeRecord:
    return TradeRecord(
        trade_id=trade_id,
        bond=bond,
        side=side,  # type: ignore[arg-type]
        face_value=face_value,
        clean_price=clean_price,
        signal_date=trade_date - timedelta(days=3),
        trade_date=trade_date,
        settlement_date=calendar.settlement_date(trade_date, int(cfg.pricing.settlement_lag_days)),
    )


def run_ledger(
    calendar: TradingCalendar,
    cfg: Config,
    *,
    trades: list[TradeRecord],
    curves: dict[date, Curve],
    initial_cash: float = 2_000_000.0,
) -> Ledger:
    ledger = Ledger(calendar, initial_cash=initial_cash, funding_rate_pct=0.0, cfg=cfg)
    for trade in trades:
        ledger.submit(trade)
    for day in sorted(curves):
        ledger.process_day(day, curves[day])
    return ledger


def attribute(
    ledger: Ledger,
    panel: pd.DataFrame,
    calendar: TradingCalendar,
    cfg: Config,
    *,
    strategy: str = "test",
    benchmark=None,
):
    return attribute_fixed_holdings(
        strategy=strategy,
        nav=ledger.nav_series(),
        snapshots=ledger.snapshots_frame(),
        cashflows=ledger.cashflows_frame(),
        trades=ledger.trades_frame(),
        panel=panel,
        calendar=calendar,
        cfg=cfg,
        benchmark=benchmark,
    )


def _assert_factor_identities(result) -> None:
    """三层恒等：桥接残差、逐券 carry+curve、四因子和 == curve_pnl。"""
    assert result.daily["bridge_residual"].abs().max() < BRIDGE_TOL
    assert np.allclose(
        result.factors["carry_roll"] + result.factors["curve_pnl"],
        result.factors["price_pnl"],
        rtol=1e-9,
        atol=1e-9,
    )
    assert np.allclose(
        result.factors[["level", "slope", "curvature", "other_curve"]].sum(axis=1),
        result.factors["curve_pnl"],
        rtol=1e-9,
        atol=1e-9,
    )
    bond_sum = result.bonds.groupby("date", sort=True)[
        ["price_pnl", "carry_roll", "curve_pnl"]
    ].sum()
    aligned = result.factors.set_index("date")[["price_pnl", "carry_roll", "curve_pnl"]]
    pd.testing.assert_frame_equal(bond_sum, aligned, check_exact=False, rtol=1e-9, atol=1e-9)


# ---------------------------------------------------------------------------
# 1. 合成平曲线：carry 捕获应计，曲线因子 ≈ 0
# ---------------------------------------------------------------------------
def test_flat_curve_carry_captures_accrual(calendar: TradingCalendar) -> None:
    cfg = zero_cost_cfg()
    bond = make_bond("FLAT", date(2024, 6, 15), date(2026, 6, 15))
    trade = make_trade(calendar, cfg, trade_id="b1", bond=bond, trade_date=date(2024, 6, 17))
    days = [date(2024, 6, 17), date(2024, 6, 18), date(2024, 6, 20)]
    curves = {day: flat_curve(day) for day in days}
    ledger = run_ledger(calendar, cfg, trades=[trade], curves=curves)
    result = attribute(ledger, panel_from_curves(curves), calendar, cfg)

    assert result.strategy == "test"
    assert list(result.daily.columns) == list(DAILY_COLUMNS)
    assert list(result.factors.columns) == list(FACTORS_COLUMNS)
    assert list(result.bonds.columns) == list(BONDS_COLUMNS)
    assert len(result.daily) == 2 and len(result.bonds) == 2
    assert result.residual_bp.index.name == "date"
    # residual_bp = bridge_residual / max(1, |NAV_{d-1}|) × 1e4
    nav_previous = ledger.nav_series().iloc[:-1].to_numpy(dtype=float)
    expected_bp = (
        result.daily["bridge_residual"].to_numpy(dtype=float)
        / np.maximum(1.0, np.abs(nav_previous))
        * 1e4
    )
    assert np.allclose(
        result.residual_bp.to_numpy(dtype=float), expected_bp, rtol=1e-12, atol=1e-12
    )
    _assert_factor_identities(result)

    # 平曲线、票面 = 曲线 → 价格变动 = 票息应计；level/slope/curvature/other ≈ 0
    for column in ("level", "slope", "curvature", "other_curve"):
        assert result.factors[column].abs().max() < 1e-9, column
    assert np.allclose(
        result.factors["price_pnl"], result.factors["carry_roll"], rtol=1e-9, atol=1e-9
    )
    assert np.allclose(result.factors["curve_pnl"], 0.0, atol=1e-9)

    # carry ≈ 票息应计（面值 × 2% / 365 × 自然日数），独立口径校验
    day_counts = np.asarray([(days[i] - days[i - 1]).days for i in range(1, len(days))])
    expected_accrual = 1_000_000.0 * 2.0 / 100.0 / 365.0 * day_counts
    assert np.allclose(result.factors["carry_roll"], expected_accrual, rtol=0.02)


# ---------------------------------------------------------------------------
# 2. 平行上移 10bp：价格 PnL 全归 level
# ---------------------------------------------------------------------------
def test_parallel_shift_is_all_level(calendar: TradingCalendar) -> None:
    cfg = zero_cost_cfg()
    bond = make_bond("PAR", date(2023, 6, 15), date(2029, 6, 15))
    trade = make_trade(calendar, cfg, trade_id="b1", bond=bond, trade_date=date(2024, 6, 17))
    days = [date(2024, 6, 17), date(2024, 6, 18)]
    curves = {days[0]: flat_curve(days[0]), days[1]: flat_curve(days[1], 2.10)}
    ledger = run_ledger(calendar, cfg, trades=[trade], curves=curves)
    result = attribute(ledger, panel_from_curves(curves), calendar, cfg)

    row = result.factors.iloc[0]
    assert abs(result.daily["bridge_residual"].iloc[0]) < BRIDGE_TOL
    _assert_factor_identities(result)

    scale = max(1.0, abs(row["level"]))
    assert row["level"] < 0.0  # 收益率上移 → 价格下跌
    assert abs(row["slope"]) <= 1e-6 * scale
    assert abs(row["curvature"]) <= 1e-6 * scale
    assert abs(row["other_curve"]) <= 1e-6 * scale
    assert row["level"] == pytest.approx(row["curve_pnl"], rel=1e-9)
    assert row["level_bp"] == pytest.approx(10.0)
    assert row["level_mean_bp"] == pytest.approx(10.0)
    assert row["level_mean_diff_bp"] == pytest.approx(0.0, abs=1e-12)


# ---------------------------------------------------------------------------
# 3. 陡峭化 / 平坦化：主要落点在 slope（2Y 单独移动、10Y 不动）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize("two_year_bp", [-10.0, 10.0], ids=["steepen", "flatten"])
def test_slope_move_lands_in_slope_factor(calendar: TradingCalendar, two_year_bp: float) -> None:
    cfg = zero_cost_cfg()
    # 2024-06-18 时剩余期限恰为 2.0 年 → 双对偶基在该节点精确复现 Δy
    bond = make_bond("STEEP2", date(2023, 6, 15), date(2026, 6, 18))
    trade = make_trade(calendar, cfg, trade_id="b1", bond=bond, trade_date=date(2024, 6, 17))
    days = [date(2024, 6, 17), date(2024, 6, 18)]
    # 2Y 单独移动；按 Δcurv=0 设计节点，10Y 不动（Δlevel=0）
    changes = {
        2.0: two_year_bp,
        3.0: 0.75 * two_year_bp,
        5.0: 0.5 * two_year_bp,
        7.0: 0.25 * two_year_bp,
    }
    curves = {days[0]: flat_curve(days[0]), days[1]: curve_with(days[1], changes)}
    ledger = run_ledger(calendar, cfg, trades=[trade], curves=curves)
    result = attribute(ledger, panel_from_curves(curves), calendar, cfg)

    row = result.factors.iloc[0]
    _assert_factor_identities(result)
    scale = max(1.0, abs(row["curve_pnl"]))
    assert row["level_bp"] == pytest.approx(0.0, abs=1e-9)
    assert row["curvature_bp"] == pytest.approx(0.0, abs=1e-9)
    assert row["slope_bp"] == pytest.approx(-two_year_bp)
    # 主要落点在 slope；level/curvature/other 只剩浮点级
    assert abs(row["level"]) <= 1e-6 * scale
    assert abs(row["curvature"]) <= 1e-6 * scale
    assert abs(row["other_curve"]) <= 1e-6 * scale
    assert row["slope"] == pytest.approx(row["curve_pnl"], rel=1e-9)
    assert np.sign(row["slope"]) == np.sign(-two_year_bp)  # 收益率下行 → 价格上行


# ---------------------------------------------------------------------------
# 3'. 蝶式：主要落点在 curvature
# ---------------------------------------------------------------------------
def test_butterfly_move_lands_in_curvature_factor(calendar: TradingCalendar) -> None:
    cfg = zero_cost_cfg()
    day = date(2024, 6, 18)
    maturity = day + timedelta(days=1825)  # 剩余期限恰为 5.0 年
    bond = make_bond("BFLY5", date(2019, 6, 15), maturity)
    trade = make_trade(calendar, cfg, trade_id="b1", bond=bond, trade_date=date(2024, 6, 17))
    days = [date(2024, 6, 17), day]
    curves = {days[0]: flat_curve(days[0]), days[1]: curve_with(day, {5.0: 10.0})}
    ledger = run_ledger(calendar, cfg, trades=[trade], curves=curves)
    result = attribute(ledger, panel_from_curves(curves), calendar, cfg)

    row = result.factors.iloc[0]
    _assert_factor_identities(result)
    scale = max(1.0, abs(row["curve_pnl"]))
    assert row["level_bp"] == pytest.approx(0.0, abs=1e-9)
    assert row["slope_bp"] == pytest.approx(0.0, abs=1e-9)
    assert row["curvature_bp"] == pytest.approx(20.0)  # 2×(+10bp)
    assert abs(row["level"]) <= 1e-6 * scale
    assert abs(row["slope"]) <= 1e-6 * scale
    assert abs(row["other_curve"]) <= 1e-6 * scale
    assert row["curvature"] == pytest.approx(row["curve_pnl"], rel=1e-9)
    assert row["curvature"] < 0.0  # 5Y 上行 → 价格下跌
    assert np.isfinite(result.factors["other_curve_nav_bp"]).all()


# ---------------------------------------------------------------------------
# 4. 跨付息：票息到账、dirty 下降、桥接精确、票息不重复计
# ---------------------------------------------------------------------------
def test_cross_coupon_bridge_exact_and_coupon_once(calendar: TradingCalendar) -> None:
    cfg = zero_cost_cfg()
    bond = make_bond("CPN", date(2024, 6, 15), date(2026, 6, 15))
    trade = make_trade(calendar, cfg, trade_id="b1", bond=bond, trade_date=date(2024, 6, 17))
    days = [date(2024, 6, 17), date(2024, 6, 18), date(2024, 12, 13), date(2024, 12, 16)]
    curves = {day: flat_curve(day) for day in days}
    ledger = run_ledger(calendar, cfg, trades=[trade], curves=curves)
    result = attribute(ledger, panel_from_curves(curves), calendar, cfg)

    _assert_factor_identities(result)
    coupons = ledger.cashflows_frame()
    coupons = coupons[coupons["kind"] == "coupon"]
    assert coupons["date"].tolist() == [date(2024, 12, 16)]  # 12-15 为周日 → 顺延
    assert float(coupons["amount"].iloc[0]) == pytest.approx(10_000.0)

    coupon_row = result.daily[result.daily["date"] == date(2024, 12, 16)].iloc[0]
    assert coupon_row["coupon_cash"] == pytest.approx(10_000.0)
    assert coupon_row["price_pnl"] < 0.0  # 除息：dirty 下降
    assert coupon_row["price_pnl"] + coupon_row["coupon_cash"] == pytest.approx(
        coupon_row["nav_delta"], abs=BRIDGE_TOL
    )
    # 票息只计一次：nav_delta = carry_roll + coupon_cash（平曲线下 curve 因子为 0）
    factor_row = result.factors[result.factors["date"] == date(2024, 12, 16)].iloc[0]
    assert coupon_row["nav_delta"] == pytest.approx(
        factor_row["carry_roll"] + coupon_row["coupon_cash"], abs=BRIDGE_TOL
    )
    assert result.bonds[result.bonds["date"] == date(2024, 12, 16)][
        "coupon_cash"
    ].sum() == pytest.approx(10_000.0)


# ---------------------------------------------------------------------------
# 5. 减仓：卖出 40% 面值，三层恒等 + slippage 披露且 ≈ 0
# ---------------------------------------------------------------------------
def test_partial_sell_bridge_bonds_and_factors_balance(calendar: TradingCalendar) -> None:
    cfg = zero_cost_cfg()
    bond = make_bond("RED", date(2024, 6, 15), date(2026, 6, 15))
    buy = make_trade(calendar, cfg, trade_id="b1", bond=bond, trade_date=date(2024, 6, 17))
    sell_settlement = calendar.settlement_date(date(2024, 6, 24), 1)
    # 按「日终估值价」报价：clean = dirty(6-24) − AI(结算日) → 执行价 = 日终 mark
    clean_sell = dirty_price_from_ytm(bond, date(2024, 6, 24), 2.0) - accrued_interest(
        bond, sell_settlement
    )
    sell = make_trade(
        calendar,
        cfg,
        trade_id="s1",
        bond=bond,
        trade_date=date(2024, 6, 24),
        side="sell",
        face_value=400_000.0,
        clean_price=clean_sell,
    )
    days = [date(2024, 6, 17), date(2024, 6, 18), date(2024, 6, 24), sell_settlement]
    curves = {day: flat_curve(day) for day in days}
    ledger = run_ledger(calendar, cfg, trades=[buy, sell], curves=curves)
    result = attribute(ledger, panel_from_curves(curves), calendar, cfg)

    _assert_factor_identities(result)
    cashflows = ledger.cashflows_frame()
    gross_sell = float(cashflows.loc[cashflows["kind"] == "trade_sell", "amount"].iloc[0])
    assert gross_sell > 0.0

    trade_row = result.daily[result.daily["date"] == date(2024, 6, 24)].iloc[0]
    settle_row = result.daily[result.daily["date"] == sell_settlement].iloc[0]
    assert trade_row["slippage"] == pytest.approx(0.0, abs=1e-6)  # 独立披露，未吸收其他项
    assert trade_row["trade_entry"] == pytest.approx(-gross_sell, rel=1e-12)
    assert trade_row["pending_delta"] == pytest.approx(gross_sell, rel=1e-12)
    assert trade_row["trade_entry"] + trade_row["pending_delta"] == pytest.approx(0.0, abs=1e-9)
    assert settle_row["trade_settlement"] == pytest.approx(gross_sell, rel=1e-12)
    assert settle_row["pending_delta"] == pytest.approx(-gross_sell, rel=1e-12)
    assert settle_row["slippage"] == pytest.approx(0.0, abs=1e-6)

    sell_day = result.bonds[result.bonds["date"] == date(2024, 6, 24)].iloc[0]
    assert sell_day["begin_face"] == pytest.approx(1_000_000.0)
    assert sell_day["end_face"] == pytest.approx(600_000.0)


# ---------------------------------------------------------------------------
# 6. 篡改检测：coupon 金额 +1 元 → 桥接残差 > 容差
# ---------------------------------------------------------------------------
def test_tampered_coupon_is_detected_by_bridge_residual(calendar: TradingCalendar) -> None:
    cfg = zero_cost_cfg()
    bond = make_bond("TMP", date(2024, 6, 15), date(2026, 6, 15))
    trade = make_trade(calendar, cfg, trade_id="b1", bond=bond, trade_date=date(2024, 6, 17))
    days = [date(2024, 6, 17), date(2024, 6, 18), date(2024, 12, 13), date(2024, 12, 16)]
    curves = {day: flat_curve(day) for day in days}
    ledger = run_ledger(calendar, cfg, trades=[trade], curves=curves)
    panel = panel_from_curves(curves)
    clean = attribute(ledger, panel, calendar, cfg)
    assert clean.daily["bridge_residual"].abs().max() < BRIDGE_TOL

    tampered = ledger.cashflows_frame()
    coupon_index = tampered.index[tampered["kind"] == "coupon"]
    assert len(coupon_index) == 1
    tampered.loc[coupon_index[0], "amount"] += 1.0
    dirty_result = attribute_fixed_holdings(
        strategy="tampered",
        nav=ledger.nav_series(),
        snapshots=ledger.snapshots_frame(),
        cashflows=tampered,
        trades=ledger.trades_frame(),
        panel=panel,
        calendar=calendar,
        cfg=cfg,
    )
    coupon_day = date(2024, 12, 16)
    residual = float(
        dirty_result.daily.loc[dirty_result.daily["date"] == coupon_day, "bridge_residual"].iloc[0]
    )
    assert abs(residual) > BRIDGE_TOL
    assert residual == pytest.approx(-1.0, abs=1e-9)
    # 残差只落在被篡改的那一天，其余日期仍精确
    others = dirty_result.daily[dirty_result.daily["date"] != coupon_day]
    assert others["bridge_residual"].abs().max() < BRIDGE_TOL


# ---------------------------------------------------------------------------
# 7. 无未来信息：替换 d 之后的 panel，d 之前的归因输出逐位不变
# ---------------------------------------------------------------------------
def test_no_future_information_in_panel(calendar: TradingCalendar) -> None:
    cfg = zero_cost_cfg()
    bond = make_bond("FUTR", date(2023, 6, 15), date(2029, 6, 15))
    trade = make_trade(calendar, cfg, trade_id="b1", bond=bond, trade_date=date(2024, 6, 17))
    days = [date(2024, 6, 17), date(2024, 6, 18), date(2024, 6, 19), date(2024, 6, 20)]
    curves = {
        days[0]: flat_curve(days[0]),
        days[1]: curve_with(days[1], {2.0: -5.0, 5.0: -2.5}),
        days[2]: flat_curve(days[2], 2.10),
        days[3]: curve_with(days[3], {2.0: 2.0, 5.0: 8.0, 10.0: 2.0}),
    }
    ledger = run_ledger(calendar, cfg, trades=[trade], curves=curves)
    panel = panel_from_curves(curves)
    base = attribute(ledger, panel, calendar, cfg)

    cutoff = days[1]
    perturbed_panel = panel.copy()
    future_dates = [day for day in perturbed_panel.index if day > cutoff]
    assert future_dates  # 确实有 d 之后的数据被替换
    for offset, day in enumerate(future_dates, start=1):
        # 每天扰动幅度不同：保证扰动后「日变化」也变，从而归因行必然变化
        perturbed_panel.loc[day, :] = perturbed_panel.loc[day, :] + 50.0 * offset
    perturbed = attribute_fixed_holdings(
        strategy="perturbed",
        nav=ledger.nav_series(),
        snapshots=ledger.snapshots_frame(),
        cashflows=ledger.cashflows_frame(),
        trades=ledger.trades_frame(),
        panel=perturbed_panel,
        calendar=calendar,
        cfg=cfg,
    )

    prefix = base.daily["date"] <= cutoff
    for left, right in (
        (base.daily[prefix], perturbed.daily[prefix]),
        (base.factors[prefix], perturbed.factors[prefix]),
        (
            base.bonds[base.bonds["date"] <= cutoff],
            perturbed.bonds[perturbed.bonds["date"] <= cutoff],
        ),
    ):
        pd.testing.assert_frame_equal(
            left.reset_index(drop=True), right.reset_index(drop=True), check_exact=True
        )
    pd.testing.assert_series_equal(
        base.residual_bp.iloc[:2], perturbed.residual_bp.iloc[:2], check_exact=True
    )
    # 扰动确实生效：cutoff 之后的曲线因子发生变化（否则测试无意义）
    assert not base.factors[~prefix].equals(perturbed.factors[~prefix])


# ---------------------------------------------------------------------------
# 基准账本 / 单日边界 / 参数校验
# ---------------------------------------------------------------------------
def test_benchmark_run_has_same_structure(calendar: TradingCalendar) -> None:
    cfg = zero_cost_cfg()
    bond = make_bond("BM", date(2023, 6, 15), date(2029, 6, 15))
    trade = make_trade(calendar, cfg, trade_id="b1", bond=bond, trade_date=date(2024, 6, 17))
    days = [date(2024, 6, 17), date(2024, 6, 18)]
    curves = {days[0]: flat_curve(days[0]), days[1]: flat_curve(days[1], 2.10)}
    ledger = run_ledger(calendar, cfg, trades=[trade], curves=curves)
    panel = panel_from_curves(curves)

    benchmark_result = attribute(ledger, panel, calendar, cfg, strategy="benchmark")
    assert benchmark_result.strategy == "benchmark"
    assert list(benchmark_result.daily.columns) == list(DAILY_COLUMNS)
    assert benchmark_result.daily["bridge_residual"].abs().max() < BRIDGE_TOL

    strategy_result = attribute(ledger, panel, calendar, cfg, benchmark=benchmark_result)
    assert strategy_result.benchmark is benchmark_result
    # 同一账本 → 主动归因（相减）为 0
    assert np.allclose(strategy_result.daily["price_pnl"], benchmark_result.daily["price_pnl"])
    with pytest.raises(TypeError, match="benchmark"):
        attribute(ledger, panel, calendar, cfg, benchmark="not-a-result")


def test_single_snapshot_returns_empty_frames(calendar: TradingCalendar) -> None:
    cfg = zero_cost_cfg()
    bond = make_bond("ONE", date(2024, 6, 15), date(2026, 6, 15))
    trade = make_trade(calendar, cfg, trade_id="b1", bond=bond, trade_date=date(2024, 6, 17))
    curves = {date(2024, 6, 17): flat_curve(date(2024, 6, 17))}
    ledger = run_ledger(calendar, cfg, trades=[trade], curves=curves)
    result = attribute(ledger, panel_from_curves(curves), calendar, cfg)

    assert result.daily.empty and result.factors.empty and result.bonds.empty
    assert result.residual_bp.empty
    assert list(result.daily.columns) == list(DAILY_COLUMNS)
    assert list(result.factors.columns) == list(FACTORS_COLUMNS)
    assert list(result.bonds.columns) == list(BONDS_COLUMNS)
