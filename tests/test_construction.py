"""组合构建测试（Phase 2 第 4 步）：KRD 矩阵、有界最小二乘面值、订单生成。

固定日期 + 合成曲线，全部离线、确定性。
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pytest

from ficc.curve.bootstrap import bootstrap_spot
from ficc.curve.curve import Curve
from ficc.data.schema import BondStatic
from ficc.portfolio import construction
from ficc.portfolio.construction import (
    FaceSolution,
    Order,
    build_orders,
    krd_matrix,
    solve_faces,
)
from ficc.portfolio.ledger import PositionView
from ficc.portfolio.types import TargetKRD
from ficc.pricing.analytics import key_rate_durations, mod_duration
from ficc.pricing.bond import dirty_price_from_ytm
from ficc.pricing.conventions import accrued_interest, day_count

AS_OF = date(2026, 5, 28)
TENORS = np.array([0.25, 0.5, 1.0, 2.0, 3.0, 5.0, 7.0, 10.0, 15.0, 20.0, 30.0])
BUCKETS = (2.0, 5.0, 10.0, 30.0)
LOT = 1_000_000.0


def flat_curve(level: float = 2.0, *, as_of: date = AS_OF) -> Curve:
    return Curve(as_of=as_of, tenors=TENORS, rates=np.full_like(TENORS, level), method="linear")


def par_bond(tenor: float, curve: Curve) -> BondStatic:
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


def universe(curve: Curve) -> list[BondStatic]:
    return [par_bond(tenor, curve) for tenor in BUCKETS]


def matrix(curve: Curve) -> np.ndarray:
    return krd_matrix(universe(curve), curve, key_tenors=BUCKETS)


def target_from_faces(matrix_: np.ndarray, faces: np.ndarray) -> TargetKRD:
    vector = matrix_ @ (faces / 100.0)
    return TargetKRD(
        as_of=AS_OF,
        krd={float(tenor): float(value) for tenor, value in zip(BUCKETS, vector, strict=True)},
        notes="test target",
    )


def positions(bonds: list[BondStatic], faces: list[float], curve: Curve) -> list[PositionView]:
    out: list[PositionView] = []
    for bond, face in zip(bonds, faces, strict=True):
        remaining = day_count(AS_OF, bond.maturity_date) / 365.0
        ytm = float(curve.rate(remaining))
        dirty = dirty_price_from_ytm(bond, AS_OF, ytm)
        ai = accrued_interest(bond, AS_OF)
        out.append(
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
                dv01=None,
            )
        )
    return out


# ---------------------------------------------------------------------------
# KRD 矩阵
# ---------------------------------------------------------------------------
def test_krd_matrix_matches_individual_key_rate_durations() -> None:
    curve = flat_curve()
    bonds = universe(curve)
    matrix_ = krd_matrix(bonds, curve, key_tenors=BUCKETS)

    assert matrix_.shape == (len(BUCKETS), len(bonds))
    assert np.all(matrix_ >= 0.0)  # 多头为正
    spot = bootstrap_spot(curve)
    for column, bond in enumerate(bonds):
        signed = key_rate_durations(bond, spot.settlement_date, spot, key_tenors=BUCKETS)
        expected = np.array([-float(signed[tenor]) for tenor in BUCKETS])
        assert np.allclose(matrix_[:, column], expected, atol=1e-10, rtol=0.0)


def test_krd_matrix_is_deterministic() -> None:
    curve = flat_curve()
    first = krd_matrix(universe(curve), curve, key_tenors=BUCKETS)
    second = krd_matrix(universe(curve), curve, key_tenors=BUCKETS)
    assert np.array_equal(first, second)


# ---------------------------------------------------------------------------
# 有界最小二乘解面值
# ---------------------------------------------------------------------------
def test_solve_single_bond_exact() -> None:
    curve = flat_curve()
    bond = par_bond(2.0, curve)
    target = TargetKRD(
        as_of=AS_OF,
        krd={
            tenor: float(value * 50_000_000.0 / 100.0)
            for tenor, value in zip(
                BUCKETS, krd_matrix([bond], curve, key_tenors=BUCKETS)[:, 0], strict=True
            )
        },
    )
    solution = solve_faces(target, [bond], curve)
    assert solution[bond.code] == pytest.approx(50_000_000.0, abs=1e-6)
    assert solution.reached
    assert solution.residual_norm == pytest.approx(0.0, abs=1e-6)
    assert all(face >= 0.0 for face in solution.values())


def test_solve_four_bonds_recover_target() -> None:
    curve = flat_curve()
    bonds = universe(curve)
    matrix_ = matrix(curve)
    faces_true = np.array([30_000_000.0, 20_000_000.0, 10_000_000.0, 40_000_000.0])
    target = target_from_faces(matrix_, faces_true)

    solution = solve_faces(target, bonds, curve)
    for bond, face in zip(bonds, faces_true, strict=True):
        assert solution[bond.code] == pytest.approx(face, abs=1e-6)
    assert solution.reached
    assert solution.residual_norm < 1e-6  # 元/bp
    assert solution.notes == ()
    # 残差量级：||A f/100 − target|| / ||target|| 远小于 1e-6
    assert solution.relative_residual < 1e-9


def test_solve_negative_target_is_absorbed_with_notes() -> None:
    curve = flat_curve()
    bonds = universe(curve)
    target = TargetKRD(
        as_of=AS_OF,
        krd={2.0: -50_000.0, 5.0: 20_000.0, 10.0: 20_000.0, 30.0: 20_000.0},
        notes="含负桶的目标",
    )
    solution = solve_faces(target, bonds, curve)
    assert all(face >= 0.0 for face in solution.values())  # 禁裸空
    assert solution.notes and any("负 DV01" in note for note in solution.notes)
    assert solution.residual_norm > 0.0  # 负目标被最小二乘吸收
    # 2Y 券不会被赋予负面值；解出来的组合至少无法命中负桶
    projected = matrix(curve) @ (np.array([solution[b.code] for b in bonds]) / 100.0)
    assert projected[0] >= 0.0
    assert projected[0] > target.krd[2.0]


def test_solve_rounds_to_nearest_lot_not_floor() -> None:
    """面值四舍五入到最近 lot：25.0M 不得被 floor 成 24M（每只券少 1 lot 的系统性偏差）。"""
    curve = flat_curve()
    bond = par_bond(2.0, curve)
    matrix_ = krd_matrix([bond], curve, key_tenors=BUCKETS)

    def target_for_face(face: float) -> TargetKRD:
        return TargetKRD(
            as_of=AS_OF,
            krd={
                tenor: float(value * face / 100.0)
                for tenor, value in zip(BUCKETS, matrix_[:, 0], strict=True)
            },
        )

    # 目标 25.0M：浮点 24.999999 也必须解回 25.0M（回归 bug：floor 得到 24.0M）
    solution = solve_faces(target_for_face(25_000_000.0), [bond], curve)
    assert solution[bond.code] == pytest.approx(25_000_000.0, abs=1e-9)
    assert solution.reached

    # 1.9M 四舍五入到最近 lot → 2.0M（不是向下取整的 1.0M）
    solution = solve_faces(target_for_face(1_900_000.0), [bond], curve)
    assert solution[bond.code] == pytest.approx(2 * LOT, abs=1e-9)
    assert solution.notes  # 取整损失必须披露
    assert not solution.reached


def test_solve_respects_max_face_per_bond() -> None:
    curve = flat_curve()
    bond = par_bond(2.0, curve)
    matrix_ = krd_matrix([bond], curve, key_tenors=BUCKETS)
    target = TargetKRD(
        as_of=AS_OF,
        krd={
            tenor: float(value * 100_000_000.0 / 100.0)
            for tenor, value in zip(BUCKETS, matrix_[:, 0], strict=True)
        },
    )
    solution = solve_faces(target, [bond], curve, max_face_per_bond=20_000_000.0)
    assert solution[bond.code] <= 20_000_000.0 + 1e-9
    assert solution[bond.code] == pytest.approx(20_000_000.0, abs=1e-9)
    assert any("上限" in note for note in solution.notes)


def test_solve_zero_target_returns_zero_faces() -> None:
    curve = flat_curve()
    bonds = universe(curve)
    target = TargetKRD(as_of=AS_OF, krd={tenor: 0.0 for tenor in BUCKETS})
    solution = solve_faces(target, bonds, curve)
    assert all(face == 0.0 for face in solution.values())
    assert solution.residual_norm == 0.0
    assert solution.notes and "全为 0" in solution.notes[0]


def test_solve_validates_inputs() -> None:
    curve = flat_curve()
    with pytest.raises(ValueError, match="bonds 不能为空"):
        krd_matrix([], curve)
    with pytest.raises(ValueError, match="重复"):
        krd_matrix([par_bond(2.0, curve), par_bond(2.0, curve)], curve)
    target = TargetKRD(as_of=AS_OF, krd={2.0: 1_000.0})
    with pytest.raises(ValueError, match="max_face_per_bond"):
        solve_faces(target, universe(curve), curve, max_face_per_bond=-1.0)


# ---------------------------------------------------------------------------
# 订单生成
# ---------------------------------------------------------------------------
def test_build_orders_audit_fields_and_side() -> None:
    curve = flat_curve()
    bonds = universe(curve)
    faces_true = np.array([30_000_000.0, 20_000_000.0, 10_000_000.0, 40_000_000.0])
    target = target_from_faces(matrix(curve), faces_true)
    current_faces = [10_000_000.0, 0.0, 5_000_000.0, 0.0]
    orders = build_orders(target, positions(bonds, current_faces, curve), bonds, curve)

    assert len(orders) == 4
    assert all(isinstance(order, Order) for order in orders)
    expected_deltas = {
        bonds[0].code: 20_000_000.0,
        bonds[1].code: 20_000_000.0,
        bonds[2].code: 5_000_000.0,
        bonds[3].code: 40_000_000.0,
    }
    for order in orders:
        assert order.side == "buy"
        assert order.face_value == pytest.approx(expected_deltas[order.bond.code], abs=1e-6)
        column = next(i for i, candidate in enumerate(bonds) if candidate.code == order.bond.code)
        assert order.target_face == pytest.approx(faces_true[column], abs=1e-6)
        assert order.current_face == pytest.approx(current_faces[column], abs=1e-6)
        assert order.clean_price == pytest.approx(100.0, abs=1e-9)  # 平价券
    # 确定性
    again = build_orders(target, positions(bonds, current_faces, curve), bonds, curve)
    assert again == orders


def test_build_orders_skips_delta_below_min_trade_lot() -> None:
    curve = flat_curve()
    bonds = universe(curve)
    target = target_from_faces(matrix(curve), np.array([30_000_000.0, 0.0, 0.0, 0.0]))
    # 当前持仓比目标多 0.5M（< 1M 最小成交单位）→ 碎单被跳过
    snapshot = positions(bonds, [30_500_000.0, 0.0, 0.0, 0.0], curve)
    orders = build_orders(target, snapshot, bonds, curve)
    assert all(order.bond.code != bonds[0].code for order in orders)
    assert orders == ()


def test_build_orders_keeps_exact_lot_delta() -> None:
    curve = flat_curve()
    bonds = universe(curve)
    target = target_from_faces(matrix(curve), np.array([30_000_000.0, 0.0, 0.0, 0.0]))
    snapshot = positions(bonds, [29_000_000.0, 0.0, 0.0, 0.0], curve)  # 差 1M = lot
    orders = build_orders(target, snapshot, bonds, curve)
    assert len(orders) == 1
    assert orders[0].side == "buy"
    assert orders[0].face_value == pytest.approx(LOT)


def test_build_orders_liquidates_bond_outside_universe() -> None:
    curve = flat_curve()
    bonds = universe(curve)
    legacy = BondStatic(
        code="LEGACY-7Y",
        issue_date=AS_OF,
        maturity_date=date(2033, 5, 28),
        coupon_pct=2.0,
        freq=1,
    )
    target = TargetKRD(as_of=AS_OF, krd={tenor: 0.0 for tenor in BUCKETS})
    snapshot = positions([*bonds, legacy], [0.0, 0.0, 0.0, 0.0, 5_000_000.0], curve)
    orders = build_orders(target, snapshot, bonds, curve)
    assert len(orders) == 1
    assert orders[0].bond.code == "LEGACY-7Y"
    assert orders[0].side == "sell"
    assert orders[0].face_value == pytest.approx(5_000_000.0)
    assert orders[0].target_face == 0.0


def test_build_orders_sell_exceeding_position_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    curve = flat_curve()
    bonds = universe(curve)
    bond = bonds[0]
    # 人为构造不可行目标（负面值）以触发卖出保护：卖出额 > 当前持仓
    monkeypatch.setattr(
        construction,
        "solve_faces",
        lambda *args, **kwargs: {bond.code: -5_000_000.0},
    )
    snapshot = positions([bond], [1_000_000.0], curve)
    target = TargetKRD(as_of=AS_OF, krd={2.0: 0.0})
    with pytest.raises(ValueError, match="超过当前持仓"):
        build_orders(target, snapshot, [bond], curve)


def test_build_orders_clean_price_matches_pricing_layer() -> None:
    curve = flat_curve()
    bond = BondStatic(
        code="PREMIUM",
        issue_date=date(2024, 6, 15),
        maturity_date=date(2034, 6, 15),
        coupon_pct=2.5,
        freq=2,
    )
    matrix_ = krd_matrix([bond], curve, key_tenors=BUCKETS)
    target = TargetKRD(
        as_of=AS_OF,
        krd={
            tenor: float(value * 30_000_000.0 / 100.0)
            for tenor, value in zip(BUCKETS, matrix_[:, 0], strict=True)
        },
    )
    order = build_orders(target, [], [bond], curve)[0]
    remaining = day_count(AS_OF, bond.maturity_date) / 365.0
    ytm = float(curve.rate(remaining))
    expected_clean = dirty_price_from_ytm(bond, AS_OF, ytm) - accrued_interest(bond, AS_OF)
    assert order.clean_price == pytest.approx(expected_clean, rel=1e-12)
    assert order.clean_price > 100.0  # 2.5% 票面高于 2% 曲线 → 溢价


def test_face_solution_is_dict_subclass() -> None:
    curve = flat_curve()
    bonds = universe(curve)
    target = TargetKRD(as_of=AS_OF, krd={tenor: 0.0 for tenor in BUCKETS})
    solution = solve_faces(target, bonds, curve)
    assert isinstance(solution, dict)
    assert isinstance(solution, FaceSolution)
