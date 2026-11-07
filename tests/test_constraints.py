"""组合约束测试（Phase 2 第 3 步）：9 项约束边界、空组合、相对 KRD 中性。

固定日期 + 合成曲线，全部离线、确定性。
"""

from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import numpy as np
import pytest

from ficc.config import get_config
from ficc.curve.bootstrap import bootstrap_spot
from ficc.curve.curve import Curve
from ficc.data.schema import BondStatic
from ficc.portfolio.constraints import (
    ConstraintReport,
    PortfolioRisk,
    check_constraints,
    portfolio_risk,
)
from ficc.portfolio.ledger import PositionView
from ficc.portfolio.types import BenchmarkPortfolio
from ficc.pricing.analytics import dv01 as ytm_dv01
from ficc.pricing.analytics import key_rate_durations, mod_duration
from ficc.pricing.bond import dirty_price_from_ytm
from ficc.pricing.conventions import accrued_interest, day_count

AS_OF = date(2026, 5, 28)
TENORS = np.array([0.25, 0.5, 1.0, 2.0, 3.0, 5.0, 7.0, 10.0, 15.0, 20.0, 30.0])
BUCKETS = (2.0, 5.0, 10.0, 30.0)


# ---------------------------------------------------------------------------
# 夹具
# ---------------------------------------------------------------------------
def flat_curve(level: float = 2.0, *, as_of: date = AS_OF) -> Curve:
    return Curve(as_of=as_of, tenors=TENORS, rates=np.full_like(TENORS, level), method="linear")


def par_bond(tenor: float, curve: Curve) -> BondStatic:
    """期限 tenor 的合成平价债（票面=该期限曲线收益率）。"""
    years = round(tenor)
    maturity = date(AS_OF.year + years, AS_OF.month, AS_OF.day)
    freq = 1 if tenor <= 7.0 else 2
    return BondStatic(
        code=f"SYN-{tenor:g}Y",
        issue_date=AS_OF,
        maturity_date=maturity,
        coupon_pct=float(curve.rate(tenor)),
        freq=freq,
    )


def make_positions(bonds: list[BondStatic], faces: list[float], curve: Curve) -> list[PositionView]:
    positions: list[PositionView] = []
    for bond, face in zip(bonds, faces, strict=True):
        remaining = day_count(AS_OF, bond.maturity_date) / 365.0
        ytm = float(curve.rate(remaining))
        dirty = dirty_price_from_ytm(bond, AS_OF, ytm)
        ai = accrued_interest(bond, AS_OF)
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
                mod_duration=mod_duration(bond, AS_OF, ytm),
                dv01=ytm_dv01(bond, AS_OF, ytm) * face / 100.0,
            )
        )
    return positions


def make_risk(
    *,
    total_dv01: float = 100_000.0,
    krd: dict[float, float] | None = None,
    duration: float = 8.0,
    market_value: float = 100_000_000.0,
    cash: float = 0.0,
    nav: float = 100_000_000.0,
    bond_weights: dict[str, float] | None = None,
) -> PortfolioRisk:
    return PortfolioRisk(
        total_dv01=total_dv01,
        krd=krd
        if krd is not None
        else {2.0: 25_000.0, 5.0: 25_000.0, 10.0: 25_000.0, 30.0: 25_000.0},
        duration=duration,
        market_value=market_value,
        cash=cash,
        nav=nav,
        bond_weights=bond_weights
        if bond_weights is not None
        else {"A": 0.25, "B": 0.25, "C": 0.25, "D": 0.25},
    )


def make_benchmark(
    *,
    total_dv01: float = 100_000.0,
    krd: dict[float, float] | None = None,
    duration: float = 8.0,
) -> BenchmarkPortfolio:
    return BenchmarkPortfolio(
        as_of=AS_OF,
        legs=(),
        market_value=100_000_000.0,
        total_dv01=total_dv01,
        duration=duration,
        krd=krd
        if krd is not None
        else {2.0: 25_000.0, 5.0: 25_000.0, 10.0: 25_000.0, 30.0: 25_000.0},
    )


def report(
    risk: PortfolioRisk, benchmark: BenchmarkPortfolio, nav_high_water: float = 100_000_000.0
) -> ConstraintReport:
    return check_constraints(risk=risk, benchmark=benchmark, nav_high_water=nav_high_water)


def codes(report_: ConstraintReport) -> set[str]:
    return {violation.code for violation in report_.violations}


# ---------------------------------------------------------------------------
# portfolio_risk
# ---------------------------------------------------------------------------
def test_portfolio_risk_matches_per_bond_krd() -> None:
    curve = flat_curve()
    bonds = [par_bond(tenor, curve) for tenor in BUCKETS]
    faces = [30_000_000.0, 20_000_000.0, 10_000_000.0, 40_000_000.0]
    positions = make_positions(bonds, faces, curve)
    risk = portfolio_risk(positions, curve, key_tenors=BUCKETS)

    spot = bootstrap_spot(curve)
    expected = {tenor: 0.0 for tenor in BUCKETS}
    for bond, face in zip(bonds, faces, strict=True):
        signed = key_rate_durations(bond, spot.settlement_date, spot, key_tenors=BUCKETS)
        for tenor in BUCKETS:
            expected[tenor] += -float(signed[tenor]) * face / 100.0
    for tenor in BUCKETS:
        assert risk.krd[tenor] == pytest.approx(expected[tenor], rel=1e-10)
    assert risk.total_dv01 == pytest.approx(sum(expected.values()), rel=1e-12)
    assert risk.total_dv01 > 0.0  # 多头为正
    assert risk.market_value == pytest.approx(sum(p.market_value for p in positions), rel=1e-12)
    # 市值加权修正久期手算
    manual_duration = sum(p.market_value * p.mod_duration for p in positions) / risk.market_value
    assert risk.duration == pytest.approx(manual_duration, rel=1e-12)
    # 只传 PositionView 时 cash=0、nav=market_value
    assert risk.cash == 0.0
    assert risk.nav == pytest.approx(risk.market_value)


def test_portfolio_risk_accepts_ledger_snapshot() -> None:
    curve = flat_curve()
    bond = par_bond(10.0, curve)
    positions = make_positions([bond], [50_000_000.0], curve)
    snapshot = SimpleNamespace(
        positions=tuple(positions), cash=1_234_567.0, nav=positions[0].market_value + 1_234_567.0
    )
    risk = portfolio_risk(snapshot, curve, key_tenors=BUCKETS)
    assert risk.cash == pytest.approx(1_234_567.0)
    assert risk.nav == pytest.approx(snapshot.nav)
    assert risk.bond_weights[bond.code] == pytest.approx(positions[0].market_value / snapshot.nav)


def test_empty_portfolio_does_not_crash() -> None:
    curve = flat_curve()
    risk = portfolio_risk([], curve, key_tenors=BUCKETS)
    assert risk.total_dv01 == 0.0
    assert risk.duration == 0.0
    assert risk.market_value == 0.0
    assert all(value == 0.0 for value in risk.krd.values())
    report_ = report(risk, make_benchmark(), nav_high_water=1.0)
    assert isinstance(report_, ConstraintReport)
    # 空组合总 DV01 比例为 0、久期偏离、回撤 —— 必然有违反项；但不会除零崩溃
    assert not report_.ok
    assert {"total_dv01_ratio", "duration_band", "drawdown_stop"} <= codes(report_)
    assert report_.metrics["total_dv01_ratio"] == pytest.approx(0.0)


# ---------------------------------------------------------------------------
# 9 项约束：边界（刚好通过 / 刚好越界）
# ---------------------------------------------------------------------------
def test_baseline_passes_all_constraints() -> None:
    report_ = report(make_risk(), make_benchmark())
    assert report_.ok, [(v.code, v.detail) for v in report_.violations]
    assert report_.as_of == AS_OF


def test_portfolio_equals_benchmark_relative_krd_is_zero() -> None:
    curve = flat_curve()
    bonds = [par_bond(tenor, curve) for tenor in BUCKETS]
    positions = make_positions(bonds, [25_000_000.0] * 4, curve)
    risk = portfolio_risk(positions, curve, key_tenors=BUCKETS)
    benchmark = make_benchmark(
        total_dv01=risk.total_dv01, krd=dict(risk.krd), duration=risk.duration
    )
    report_ = report(risk, benchmark, nav_high_water=risk.nav)
    assert report_.metrics["relative_krd_max_dev"] == pytest.approx(0.0, abs=1e-9)
    assert report_.metrics["total_dv01_ratio"] == pytest.approx(1.0, abs=1e-12)
    assert report_.metrics["duration_dev"] == pytest.approx(0.0, abs=1e-12)
    assert codes(report_).isdisjoint({"relative_krd", "total_dv01_ratio", "duration_band"})


def test_relative_krd_boundary() -> None:
    benchmark = make_benchmark(total_dv01=100_000.0)  # 限额 = 30% × 100k = 30k
    at_limit = {2.0: 55_000.0, 5.0: 25_000.0, 10.0: 25_000.0, 30.0: 25_000.0}
    report_ = report(make_risk(krd=at_limit), benchmark)
    assert "relative_krd" not in codes(report_)
    assert report_.metrics["relative_krd_max_dev"] == pytest.approx(30_000.0)

    just_over = {2.0: 55_001.0, 5.0: 25_000.0, 10.0: 25_000.0, 30.0: 25_000.0}
    report_ = report(make_risk(krd=just_over), benchmark)
    assert "relative_krd" in codes(report_)
    violation = next(v for v in report_.violations if v.code == "relative_krd")
    assert violation.value == pytest.approx(30_001.0)
    assert violation.limit == pytest.approx(30_000.0)


def test_total_dv01_ratio_boundary() -> None:
    lower = {2.0: 10_000.0, 5.0: 20_000.0, 10.0: 20_000.0, 30.0: 20_000.0}  # 比例 0.70
    report_ = report(make_risk(total_dv01=70_000.0, krd=lower), make_benchmark())
    assert "total_dv01_ratio" not in codes(report_)
    assert report_.metrics["total_dv01_ratio"] == pytest.approx(0.70)

    under = {2.0: 9_000.0, 5.0: 20_000.0, 10.0: 20_000.0, 30.0: 20_000.0}  # 0.69
    report_ = report(make_risk(total_dv01=69_000.0, krd=under), make_benchmark())
    assert "total_dv01_ratio" in codes(report_)

    upper = {2.0: 40_000.0, 5.0: 30_000.0, 10.0: 30_000.0, 30.0: 30_000.0}  # 1.30
    report_ = report(make_risk(total_dv01=130_000.0, krd=upper), make_benchmark())
    assert "total_dv01_ratio" not in codes(report_)

    over = {2.0: 40_001.0, 5.0: 30_000.0, 10.0: 30_000.0, 30.0: 30_000.0}  # 1.30001
    report_ = report(make_risk(total_dv01=130_001.0, krd=over), make_benchmark())
    assert "total_dv01_ratio" in codes(report_)


def test_duration_band_boundary() -> None:
    # 基准久期 8 年，容差 ±20% => ±1.6 年
    report_ = report(make_risk(duration=9.6), make_benchmark(duration=8.0))
    assert "duration_band" not in codes(report_)
    report_ = report(make_risk(duration=6.4), make_benchmark(duration=8.0))
    assert "duration_band" not in codes(report_)
    report_ = report(make_risk(duration=9.600001), make_benchmark(duration=8.0))
    assert "duration_band" in codes(report_)
    assert report_.metrics["duration_band_abs"] == pytest.approx(1.6)


def test_absolute_dv01_boundary_uses_config_limit() -> None:
    cfg = get_config()
    limit = cfg.risk.limits.max_dv01
    at_limit_krd = {2.0: limit / 4.0, 5.0: limit / 4.0, 10.0: limit / 4.0, 30.0: limit / 4.0}
    benchmark = make_benchmark(total_dv01=limit, krd=dict(at_limit_krd), duration=8.0)
    report_ = report(make_risk(total_dv01=limit, krd=at_limit_krd), benchmark)
    assert "absolute_dv01" not in codes(report_)
    assert report_.metrics["absolute_dv01_limit"] == pytest.approx(limit)

    too_big_krd = {2.0: limit / 4.0, 5.0: limit / 4.0, 10.0: limit / 4.0, 30.0: limit / 4.0 + 1.0}
    report_ = report(make_risk(total_dv01=limit + 1.0, krd=too_big_krd), benchmark)
    assert "absolute_dv01" in codes(report_)


def test_krd_share_boundary() -> None:
    # 上限 0.5：{50k, 50k, 0, 0} 集中度恰为 0.5
    equal = {2.0: 50_000.0, 5.0: 50_000.0, 10.0: 0.0, 30.0: 0.0}
    report_ = report(make_risk(total_dv01=100_000.0, krd=equal), make_benchmark(krd=dict(equal)))
    assert "krd_share" not in codes(report_)
    assert report_.metrics["krd_share"] == pytest.approx(0.5)

    concentrated = {2.0: 50_001.0, 5.0: 50_000.0, 10.0: 0.0, 30.0: 0.0}
    report_ = report(
        make_risk(total_dv01=100_001.0, krd=concentrated), make_benchmark(krd=dict(concentrated))
    )
    assert "krd_share" in codes(report_)


def test_single_bond_weight_boundary() -> None:
    weights = {"A": 0.30, "B": 0.25, "C": 0.25, "D": 0.20}
    report_ = report(make_risk(bond_weights=weights), make_benchmark())
    assert "single_bond_weight" not in codes(report_)
    assert report_.metrics["max_single_bond_weight"] == pytest.approx(0.30)

    weights_over = {"A": 0.300001, "B": 0.25, "C": 0.25, "D": 0.199999}
    report_ = report(make_risk(bond_weights=weights_over), make_benchmark())
    assert "single_bond_weight" in codes(report_)


def test_leverage_boundary() -> None:
    report_ = report(
        make_risk(market_value=200_000_000.0, nav=100_000_000.0), make_benchmark()
    )  # 2.0 上限
    assert "leverage" not in codes(report_)
    assert report_.metrics["leverage"] == pytest.approx(2.0)

    report_ = report(make_risk(market_value=200_000_001.0, nav=100_000_000.0), make_benchmark())
    assert "leverage" in codes(report_)


def test_cash_floor_boundary() -> None:
    """现金下限 = min_cash_fraction × NAV（默认 -20%）。"""
    from ficc.config import get_config

    nav = 100_000_000.0
    limit = get_config().constraints.min_cash_fraction * nav
    report_ = report(make_risk(cash=limit + 1.0, nav=nav), make_benchmark())
    assert "cash_floor" not in codes(report_)
    report_ = report(make_risk(cash=limit - 1.0, nav=nav), make_benchmark())
    assert "cash_floor" in codes(report_)
    assert report_.metrics["cash_floor"] == pytest.approx(limit)


def test_drawdown_stop_boundary() -> None:
    # 高水位 1 亿、最大回撤 5%：NAV=9500 万 恰好通过；9499.9 万 越界
    report_ = report(make_risk(nav=95_000_000.0), make_benchmark(), nav_high_water=100_000_000.0)
    assert "drawdown_stop" not in codes(report_)
    assert report_.metrics["drawdown"] == pytest.approx(-0.05)

    report_ = report(make_risk(nav=94_999_000.0), make_benchmark(), nav_high_water=100_000_000.0)
    assert "drawdown_stop" in codes(report_)
    assert report_.metrics["drawdown_stop"] == pytest.approx(-0.05)


# ---------------------------------------------------------------------------
# metrics 手算
# ---------------------------------------------------------------------------
def test_metrics_values_match_hand_calculation() -> None:
    report_ = report(
        make_risk(
            total_dv01=110_000.0,
            krd={2.0: 30_000.0, 5.0: 25_000.0, 10.0: 30_000.0, 30.0: 25_000.0},
            duration=8.8,
            market_value=120_000_000.0,
            cash=2_000_000.0,
            nav=100_000_000.0,
            bond_weights={"A": 0.35, "B": 0.25, "C": 0.22, "D": 0.18},
        ),
        make_benchmark(),
        nav_high_water=105_000_000.0,
    )
    metrics = report_.metrics
    assert metrics["benchmark_total_dv01"] == pytest.approx(100_000.0)
    assert metrics["portfolio_total_dv01"] == pytest.approx(110_000.0)
    assert metrics["total_dv01_ratio"] == pytest.approx(1.10)
    assert metrics["relative_krd_max_dev"] == pytest.approx(5_000.0)  # 2Y/10Y 偏离 5k
    assert metrics["relative_krd_tol_abs"] == pytest.approx(30_000.0)
    assert metrics["duration_dev"] == pytest.approx(0.8)
    assert metrics["krd_share"] == pytest.approx(30_000.0 / 110_000.0)
    assert metrics["max_single_bond_weight"] == pytest.approx(0.35)
    assert metrics["leverage"] == pytest.approx(1.2)
    assert metrics["cash"] == pytest.approx(2_000_000.0)
    assert metrics["drawdown"] == pytest.approx(100_000_000.0 / 105_000_000.0 - 1.0)
    assert metrics["krd_dev@10"] == pytest.approx(5_000.0)
    assert "single_bond_weight" in codes(report_)  # 0.35 > 0.30
