"""基准组合（`portfolio/benchmark.py`）的离线单元测试。"""

from __future__ import annotations

from datetime import date

import numpy as np
import pytest

from ficc.config import get_config
from ficc.curve.curve import Curve
from ficc.portfolio.benchmark import build_benchmark, build_universe_bonds
from ficc.pricing import dirty_price_from_ytm

TENORS = np.array([0.25, 0.5, 1.0, 2.0, 3.0, 5.0, 7.0, 10.0, 15.0, 20.0, 30.0])
AS_OF = date(2024, 6, 28)


def _curve(rates: np.ndarray | None = None) -> Curve:
    if rates is None:
        rates = np.array([1.44, 1.45, 1.54, 1.63, 1.80, 1.98, 2.10, 2.21, 2.31, 2.36, 2.43])
    return Curve(as_of=AS_OF, tenors=TENORS, rates=rates, method="linear")


def _custom_weights(weights: dict[float, float]):
    cfg = get_config()
    benchmark = cfg.benchmark.model_copy(update={"ladder_weights": dict(weights)})
    return cfg.model_copy(update={"benchmark": benchmark})


def test_universe_bonds_terms() -> None:
    cfg = get_config()
    bonds = build_universe_bonds(_curve(), AS_OF, cfg=cfg)
    assert len(bonds) == len(cfg.portfolio.universe) == 4
    assert [b.code for b in bonds] == [
        f"SYN-{tenor:g}Y-{AS_OF:%Y%m%d}" for tenor in cfg.portfolio.universe
    ]
    for bond, tenor in zip(bonds, cfg.portfolio.universe, strict=True):
        assert bond.issue_date == AS_OF
        assert bond.maturity_date == date(AS_OF.year + int(tenor), AS_OF.month, AS_OF.day)
        assert bond.freq == (2 if tenor >= 10 else 1)
        assert bond.coupon_pct == pytest.approx(float(_curve().rate(tenor)), abs=1e-12)
        assert bond.face_value == pytest.approx(100.0)


def test_benchmark_structure_and_conservation() -> None:
    cfg = get_config()
    curve = _curve()
    bonds = build_universe_bonds(curve, AS_OF, cfg=cfg)
    benchmark = build_benchmark(curve, AS_OF, bonds=bonds, cfg=cfg)
    assert len(benchmark.legs) == len(cfg.portfolio.universe) == 4
    # 面值取整到 min_trade_lot，市值与 notional 相差 0.5 lot/腿 以内（R22 固定面值）
    tol = 0.5 * float(cfg.constraints.min_trade_lot) * len(benchmark.legs) / 100.0 * 1.05
    assert benchmark.market_value == pytest.approx(cfg.benchmark.notional, abs=tol)
    assert sum(leg.market_value for leg in benchmark.legs) == pytest.approx(
        cfg.benchmark.notional, abs=tol
    )
    assert benchmark.total_dv01 > 0
    assert set(benchmark.krd) == set(float(t) for t in cfg.risk.key_tenors_krd)
    assert sum(benchmark.krd.values()) == pytest.approx(benchmark.total_dv01, rel=1e-12)
    assert benchmark.duration > 0
    assert benchmark.weights == pytest.approx(
        {float(k): float(v) for k, v in cfg.benchmark.ladder_weights.items()}
    )
    assert benchmark.codes == tuple(bond.code for bond in bonds)


def test_benchmark_manual_numbers_on_flat_curve() -> None:
    """固定 2% 平曲线：手算 face/dv01，与 build_benchmark 逐腿对照。"""
    cfg = get_config()
    curve = Curve(as_of=AS_OF, tenors=TENORS, rates=np.full(TENORS.size, 2.0), method="linear")
    from ficc.curve.bootstrap import bootstrap_spot

    spot = bootstrap_spot(curve)
    settlement = spot.settlement_date
    benchmark = build_benchmark(curve, AS_OF, cfg=cfg)
    notional = float(cfg.benchmark.notional)
    lot = float(cfg.constraints.min_trade_lot)
    for leg, tenor in zip(benchmark.legs, cfg.portfolio.universe, strict=True):
        target_mv = notional * float(cfg.benchmark.ladder_weights[float(tenor)])
        # 面值取整到整手：误差不超过 0.5 lot × 全价/100
        assert abs(leg.market_value - target_mv) <= 0.5 * lot * 1.05
        assert leg.face_value == pytest.approx(round(leg.face_value / lot) * lot, abs=1e-6)
        dirty = dirty_price_from_ytm(leg.bond, spot.settlement_date, 2.0, cfg=cfg)
        assert leg.market_value == pytest.approx(leg.face_value / 100.0 * dirty, rel=1e-10)
        # 口径：leg.dv01 == 该腿即期 KRD 合计（不再用 YTM DV01 缩放——历史 bug 会让
        # 基准无法被组合构建精确复现，见 docs/code.md C-042）
        from ficc.pricing.analytics import key_rate_durations

        spot_krd = key_rate_durations(
            leg.bond, settlement, spot, key_tenors=list(cfg.risk.key_tenors_krd), cfg=cfg
        )
        expected_dv01 = sum(-float(v) for v in spot_krd.values()) * leg.face_value / 100.0
        assert leg.dv01 == pytest.approx(expected_dv01, rel=1e-9)
        # 每腿 KRD 分布之和 = 该腿 DV01
        assert sum(leg.krd.values()) == pytest.approx(leg.dv01, rel=1e-12)
        assert all(value >= 0 for value in leg.krd.values())


def test_benchmark_rejects_bad_weights_sum() -> None:
    cfg = _custom_weights({2.0: 0.3, 5.0: 0.3, 10.0: 0.3, 30.0: 0.3})
    with pytest.raises(ValueError, match="权重和"):
        build_benchmark(_curve(), AS_OF, cfg=cfg)


def test_benchmark_rejects_weight_keys_not_matching_universe() -> None:
    cfg = _custom_weights({2.0: 0.5, 10.0: 0.5})
    with pytest.raises(ValueError, match="universe"):
        build_benchmark(_curve(), AS_OF, cfg=cfg)


def test_benchmark_rejects_missing_bonds() -> None:
    cfg = get_config()
    bonds = build_universe_bonds(_curve(), AS_OF, cfg=cfg)
    with pytest.raises(ValueError, match="缺少"):
        build_benchmark(_curve(), AS_OF, bonds=bonds[:2], cfg=cfg)


def test_universe_bonds_rejects_bad_universe() -> None:
    cfg = get_config()
    bad = cfg.model_copy(update={"portfolio": cfg.portfolio.model_copy(update={"universe": []})})
    with pytest.raises(ValueError, match="universe"):
        build_universe_bonds(_curve(), AS_OF, cfg=bad)


def test_benchmark_is_exactly_reproducible_by_construction() -> None:
    """回归：基准的 KRD 必须能被 `solve_faces` 用同一批券精确复现。

    历史 bug：基准曾把"即期 KRD 形状"缩放到"YTM DV01 总量"，导致 KRD 与构建矩阵
    不同源，4 腿基准解出来残差 ~0.9%、面值取整系统性少一个 lot。
    """
    from ficc.portfolio.construction import solve_faces
    from ficc.portfolio.types import TargetKRD

    cfg = get_config()
    curve = _curve()
    bonds = build_universe_bonds(curve, AS_OF, cfg=cfg)
    benchmark = build_benchmark(curve, AS_OF, bonds=bonds, cfg=cfg)

    solved = solve_faces(TargetKRD(as_of=AS_OF, krd=dict(benchmark.krd)), bonds, curve, cfg=cfg)
    assert solved.reached is True
    assert solved.residual_norm < 1e-6
    for leg, face in zip(benchmark.legs, solved.values(), strict=True):
        assert face == pytest.approx(leg.face_value, rel=1e-9)
