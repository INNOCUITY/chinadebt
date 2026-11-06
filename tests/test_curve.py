"""``Curve`` 对象的单元测试：求值、插值/外推、远期、冲击与校验。

测试口径
--------
- 收益率单位百分数，期限单位年；
- 冲击幅度单位 bp（``100bp = 1.00%``）；
- 全部断言都是**确定性的**，不依赖随机数与网络。
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError
from datetime import date, datetime

import numpy as np
import pandas as pd
import pytest

from ficc.curve._common import ns_loadings, ns_rate, nss_rate
from ficc.curve.curve import Curve

TENORS = np.array([0.25, 0.5, 1.0, 2.0, 3.0, 5.0, 7.0, 10.0, 15.0, 20.0, 30.0])
RATES = np.array([1.50, 1.55, 1.62, 1.72, 1.82, 1.98, 2.10, 2.22, 2.30, 2.34, 2.42])
AS_OF = date(2024, 6, 28)

# 一条形态"正常"（正斜率、微凸）的 NSS 曲线参数
NSS_TRUE = np.array([3.20, -1.80, 1.50, -0.80, 1.80, 7.0])
NSS_PARAMS = {
    "b0": 3.20,
    "b1": -1.80,
    "b2": 1.50,
    "b3": -0.80,
    "tau1": 1.80,
    "tau2": 7.0,
}


@pytest.fixture
def linear_curve() -> Curve:
    return Curve(as_of=AS_OF, tenors=TENORS, rates=RATES, method="linear")


@pytest.fixture
def hermite_curve() -> Curve:
    return Curve(as_of=AS_OF, tenors=TENORS, rates=RATES, method="hermite")


@pytest.fixture
def nss_curve() -> Curve:
    rates = nss_rate(NSS_TRUE, TENORS)
    return Curve(as_of=AS_OF, tenors=TENORS, rates=rates, method="nss", params=NSS_PARAMS)


# ---------------------------------------------------------------------------
# 构造与校验
# ---------------------------------------------------------------------------
def test_rate_on_nodes_is_exact(linear_curve: Curve, hermite_curve: Curve) -> None:
    """节点上的求值必须严格等于存储的 rates（不能有插值误差）。"""
    for curve in (linear_curve, hermite_curve):
        np.testing.assert_allclose(curve.rate(TENORS), RATES, rtol=0, atol=0)


def test_rate_scalar_and_array_shapes(hermite_curve: Curve) -> None:
    assert isinstance(hermite_curve.rate(5.0), float)
    out = hermite_curve.rate(np.array([1.0, 5.0, 10.0]))
    assert isinstance(out, np.ndarray)
    assert out.shape == (3,)


def test_rate_at_zero_tenor_is_finite(hermite_curve: Curve, nss_curve: Curve) -> None:
    """t→0 不能出现 0/0；参数化曲线在 0 处应等于 b0+b1。"""
    assert np.isfinite(linear(hermite_curve.rate(1e-9)))
    assert np.isfinite(hermite_curve.rate(0.0))
    assert nss_curve.rate(0.0) == pytest.approx(NSS_PARAMS["b0"] + NSS_PARAMS["b1"], abs=1e-8)


def linear(x: float) -> float:
    return float(x)


def test_interpolated_curve_is_monotone(linear_curve: Curve, hermite_curve: Curve) -> None:
    """节点间单调：给定单调上升的节点收益率，插值后仍单调不减。"""
    dense = np.linspace(TENORS[0], TENORS[-1], 500)
    for curve in (linear_curve, hermite_curve):
        values = np.asarray(curve.rate(dense))
        assert np.all(np.diff(values) >= -1e-12)


def test_hermite_does_not_overshoot(hermite_curve: Curve) -> None:
    """单调三次 Hermite 不应在节点间冲出节点值范围（PCHIP 的性质）。"""
    dense = np.linspace(TENORS[0], TENORS[-1], 2000)
    values = np.asarray(hermite_curve.rate(dense))
    assert values.min() >= RATES.min() - 1e-9
    assert values.max() <= RATES.max() + 1e-9


def test_extrapolation_never_negative(linear_curve: Curve, hermite_curve: Curve) -> None:
    """两端外推用端点斜率线性延伸，且不得产生负利率。"""
    for curve in (linear_curve, hermite_curve):
        short = np.asarray(curve.rate(np.array([0.001, 0.01, 0.1])))
        long = np.asarray(curve.rate(np.array([40.0, 50.0, 100.0])))
        assert np.all(short >= 0.0)
        assert np.all(long >= 0.0)
        # 外推是线性延伸（不是常数外推）：斜率应延续端点区间斜率
        slope = (RATES[1] - RATES[0]) / (TENORS[1] - TENORS[0])
        expected = RATES[0] + slope * (0.01 - TENORS[0])
        assert curve.rate(0.01) == pytest.approx(expected, rel=1e-12)


def test_invalid_construction() -> None:
    with pytest.raises(ValueError, match="严格递增"):
        Curve(as_of=AS_OF, tenors=np.array([1.0, 2.0, 2.0]), rates=np.array([1.0, 2.0, 3.0]))
    with pytest.raises(ValueError, match="长度不一致"):
        Curve(as_of=AS_OF, tenors=np.array([1.0, 2.0]), rates=np.array([1.0]))
    with pytest.raises(ValueError, match="NaN"):
        Curve(as_of=AS_OF, tenors=np.array([1.0, 2.0]), rates=np.array([1.0, np.nan]))
    with pytest.raises(ValueError, match="method"):
        Curve(as_of=AS_OF, tenors=np.array([1.0, 2.0]), rates=np.array([1.0, 2.0]), method="cubic")
    with pytest.raises(ValueError, match="至少需要 2 个"):
        Curve(as_of=AS_OF, tenors=np.array([1.0]), rates=np.array([1.0]))
    with pytest.raises(ValueError, match="正数"):
        Curve(as_of=AS_OF, tenors=np.array([-1.0, 2.0]), rates=np.array([1.0, 2.0]))
    with pytest.raises(TypeError, match="as_of"):
        Curve(as_of="2024-06-28", tenors=np.array([1.0, 2.0]), rates=np.array([1.0, 2.0]))


def test_datetime_as_of_is_converted() -> None:
    curve = Curve(as_of=datetime(2024, 6, 28, 17, 30), tenors=TENORS, rates=RATES, method="linear")
    assert curve.as_of == date(2024, 6, 28)
    assert isinstance(curve.as_of, date) and not isinstance(curve.as_of, datetime)


# ---------------------------------------------------------------------------
# 远期
# ---------------------------------------------------------------------------
def test_forward_formula() -> None:
    curve = Curve(as_of=AS_OF, tenors=np.array([5.0, 10.0]), rates=np.array([2.0, 2.2]))
    expected = (2.2 * 10.0 - 2.0 * 5.0) / (10.0 - 5.0)
    assert curve.forward(5.0, 10.0) == pytest.approx(expected, rel=1e-12)
    assert curve.forward(0.0, 5.0) == pytest.approx(2.0, rel=1e-12)


def test_forward_validation(linear_curve: Curve) -> None:
    with pytest.raises(ValueError, match="t2"):
        linear_curve.forward(5.0, 5.0)
    with pytest.raises(ValueError, match="t2"):
        linear_curve.forward(10.0, 5.0)
    with pytest.raises(ValueError, match="t1"):
        linear_curve.forward(-1.0, 5.0)


# ---------------------------------------------------------------------------
# 平行移动与情景冲击
# ---------------------------------------------------------------------------
def test_shift_parallel_exact(linear_curve: Curve) -> None:
    shifted = linear_curve.shift(25.0)
    np.testing.assert_allclose(shifted.rate(TENORS), RATES + 0.25, atol=1e-12)
    # shift 按"平移量"语义接受负数（与 shock 的"幅度非负"区分，R10）
    down = linear_curve.shift(-25.0)
    np.testing.assert_allclose(down.rate(TENORS), RATES - 0.25, atol=1e-12)
    # 原曲线不可变
    np.testing.assert_allclose(linear_curve.rate(TENORS), RATES, atol=0)


def test_shock_parallel_100bp(linear_curve: Curve, hermite_curve: Curve) -> None:
    """shock("parallel", 100) 后所有点 +1.00%。"""
    for curve in (linear_curve, hermite_curve):
        shocked = curve.shock("parallel", 100.0)
        delta = np.asarray(shocked.rate(TENORS)) - np.asarray(curve.rate(TENORS))
        np.testing.assert_allclose(delta, 1.00, atol=1e-12)


def test_shock_steepen_directions(linear_curve: Curve, nss_curve: Curve) -> None:
    """陡峭化：短端向下、长端向上（长短端方向必须正确）。"""
    a = linear_curve.shock("steepen", 25.0)
    short_delta, long_delta = (
        linear_curve.rate(0.25) - a.rate(0.25),
        a.rate(30.0) - linear_curve.rate(30.0),
    )
    assert short_delta > 0.0  # 原曲线短端更高 → 冲击后更低
    assert long_delta > 0.0  # 长端更高
    b = nss_curve.shock("steepen", 25.0)
    assert b.rate(30.0) > nss_curve.rate(30.0)
    assert b.rate(0.25) < nss_curve.rate(0.25)


def test_shock_flatten_is_opposite(linear_curve: Curve) -> None:
    steep = linear_curve.shock("steepen", 25.0)
    flat = linear_curve.shock("flatten", 25.0)
    np.testing.assert_allclose(
        steep.rate(TENORS) - linear_curve.rate(TENORS),
        -(flat.rate(TENORS) - linear_curve.rate(TENORS)),
        atol=1e-12,
    )


def test_shock_butterfly_belly_vs_wings(linear_curve: Curve, nss_curve: Curve) -> None:
    """蝶式：腹部向上、两端向下。"""
    for curve, belly_tenor in ((linear_curve, 2.0), (nss_curve, 2.0)):
        shocked = curve.shock("butterfly", 25.0)
        belly = shocked.rate(belly_tenor) - curve.rate(belly_tenor)
        short_end = shocked.rate(0.25) - curve.rate(0.25)
        long_end = shocked.rate(30.0) - curve.rate(30.0)
        assert belly > 0.0
        assert short_end < 0.0
        assert long_end < 0.0


def test_shock_parametric_matches_interpolation(nss_curve: Curve) -> None:
    """NSS 曲线上的冲击落在 {1, L1, L2} 张成的空间内，节点变化应可精确复现。"""
    steep = nss_curve.shock("steepen", 25.0)
    l1, _ = ns_loadings(TENORS, NSS_PARAMS["tau1"])
    expected = 0.25 * (0.5 - l1)
    np.testing.assert_allclose(steep.rate(TENORS) - nss_curve.rate(TENORS), expected, atol=1e-10)


def test_shock_invalid_input(linear_curve: Curve) -> None:
    with pytest.raises(ValueError, match="kind"):
        linear_curve.shock("twist", 25.0)
    with pytest.raises(ValueError, match="bp"):
        linear_curve.shock("parallel", np.nan)
    # R10：bp 是"幅度"，必须非负；方向由 kind 表达，下游不要再取负号
    with pytest.raises(ValueError, match="幅度"):
        linear_curve.shock("flatten", -25.0)


def test_shock_negative_bp_raises_all_kinds(linear_curve: Curve) -> None:
    for kind in ("parallel", "steepen", "flatten", "butterfly"):
        with pytest.raises(ValueError, match="幅度"):
            linear_curve.shock(kind, -1.0)


# ---------------------------------------------------------------------------
# 输出与便捷构造
# ---------------------------------------------------------------------------
def test_to_frame(linear_curve: Curve) -> None:
    frame = linear_curve.to_frame()
    assert isinstance(frame, pd.DataFrame)
    assert list(frame.columns) == ["tenor", "ytm_pct"]
    np.testing.assert_allclose(frame["tenor"].to_numpy(), TENORS)
    np.testing.assert_allclose(frame["ytm_pct"].to_numpy(), RATES)


def test_ns_curve_analytic_evaluation() -> None:
    """method="ns" 时 rate() 用参数解析求值。"""
    from ficc.curve._common import ns_rate

    params = {"b0": 3.0, "b1": -1.5, "b2": 1.2, "tau": 2.0}
    true = np.array([3.0, -1.5, 1.2, 2.0])
    curve = Curve(
        as_of=AS_OF,
        tenors=TENORS,
        rates=ns_rate(true, TENORS),
        method="ns",
        params=params,
    )
    np.testing.assert_allclose(curve.rate(TENORS), ns_rate(true, TENORS), atol=1e-12)
    # R09：参数化曲线缺参数必须抛异常，禁止静默退化成线性插值
    with pytest.raises(ValueError, match="参数不完整"):
        Curve(as_of=AS_OF, tenors=TENORS, rates=RATES, method="ns")
    with pytest.raises(ValueError, match="参数不完整"):
        Curve(
            as_of=AS_OF,
            tenors=TENORS,
            rates=RATES,
            method="nss",
            params={"b0": 3.0, "b1": -1.0},
        )


def test_from_params_accepts_ns_tau1_alias() -> None:
    """R09：NS 面板导出 tau1，from_params(method="ns") 必须接受该别名并规范化。"""
    params = {"b0": 3.0, "b1": -1.5, "b2": 1.2, "tau1": 2.0}
    curve = Curve.from_params(AS_OF, params, tenors=TENORS, method="ns")
    assert curve.params["tau"] == pytest.approx(2.0)
    expected = ns_rate(np.array([3.0, -1.5, 1.2, 2.0]), TENORS)
    np.testing.assert_allclose(curve.rate(TENORS), expected, atol=1e-10)
    # shock 也必须可用（参数完整）
    shocked = curve.shock("parallel", 100.0)
    np.testing.assert_allclose(shocked.rate(TENORS) - curve.rate(TENORS), 1.0, atol=1e-10)


def test_from_params_rejects_incomplete_params() -> None:
    with pytest.raises(ValueError, match="参数不完整"):
        Curve.from_params(AS_OF, {"b0": 3.0, "b1": -1.0}, tenors=TENORS, method="nss")
    with pytest.raises(ValueError, match=r"未知参数化方法|仅支持"):
        Curve.from_params(AS_OF, {"b0": 3.0}, tenors=TENORS, method="linear")


def test_from_params_builds_curve() -> None:
    curve = Curve.from_params(AS_OF, NSS_PARAMS, tenors=TENORS, method="nss")
    np.testing.assert_allclose(curve.rate(TENORS), nss_rate(NSS_TRUE, TENORS), atol=1e-12)
    assert curve.method == "nss"
    assert curve.params["tau2"] == pytest.approx(7.0)


def test_curve_is_immutable(linear_curve: Curve) -> None:
    with pytest.raises(FrozenInstanceError):
        linear_curve.method = "nss"  # type: ignore[misc]
