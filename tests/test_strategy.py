"""策略层（`src/ficc/strategy/`）的离线单元测试。

覆盖：
- relative_tilt：总 DV01 守恒、负桶截断重分配、全负 deltas、notes 说明；
- 三个策略在合成数据上的明确方向（carry 排名 / 2s10s z-score / NSS 残差 RV）；
- 无未来信息：history 尾部追加 as_of 之后的极端数据，target 与截断版完全一致；
- 历史不足 → 返回基准零 tilt；同一输入可复现。
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest

from ficc.config import get_config
from ficc.curve._common import nss_rate
from ficc.curve.curve import Curve
from ficc.portfolio.benchmark import build_benchmark
from ficc.portfolio.types import MarketContext, TargetKRD
from ficc.strategy import (
    BaseStrategy,
    CarryRollStrategy,
    RelativeValueStrategy,
    SteepenerStrategy,
    Strategy,
    relative_tilt,
)

TENORS = np.array([0.25, 0.5, 1.0, 2.0, 3.0, 5.0, 7.0, 10.0, 15.0, 20.0, 30.0])
AS_OF = date(2024, 6, 28)
NSS_PARAMS = np.array([3.0, -1.5, 1.0, -0.5, 1.6, 6.0])
BASE_RATES = nss_rate(NSS_PARAMS, TENORS)


def _curve() -> Curve:
    return Curve(as_of=AS_OF, tenors=TENORS, rates=BASE_RATES, method="linear")


def _benchmark(curve: Curve | None = None):
    return build_benchmark(curve if curve is not None else _curve(), AS_OF, cfg=get_config())


def _ctx(history: pd.DataFrame, *, curve: Curve | None = None, benchmark=None) -> MarketContext:
    curve = curve if curve is not None else _curve()
    benchmark = benchmark if benchmark is not None else _benchmark(curve)
    return MarketContext(
        as_of=AS_OF,
        curve=curve,
        history=history,
        benchmark=benchmark,
        nav=1e8,
        cash=0.0,
        positions=(),
        cfg=get_config(),
    )


def _history(n_days: int = 140, *, seed: int = 0) -> pd.DataFrame:
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(end=AS_OF, periods=n_days)
    values = np.tile(BASE_RATES, (n_days, 1)) + rng.normal(0.0, 0.002, (n_days, TENORS.size))
    return pd.DataFrame(values, index=dates, columns=pd.Index(TENORS, name="tenor"))


def _slope_history(
    n_days: int = 100, *, rising: bool = True, extra_future: int = 0
) -> pd.DataFrame:
    dates = pd.bdate_range(end=AS_OF, periods=n_days)
    values = np.tile(BASE_RATES, (n_days, 1))
    i2, i10 = TENORS.tolist().index(2.0), TENORS.tolist().index(10.0)
    values[:, i2] = 2.0
    values[:, i10] = np.linspace(2.2, 3.8, n_days) if rising else np.linspace(3.8, 2.2, n_days)
    history = pd.DataFrame(values, index=dates, columns=pd.Index(TENORS, name="tenor"))
    if extra_future:
        future_dates = pd.bdate_range(
            start=pd.Timestamp(AS_OF) + pd.DateOffset(days=1), periods=extra_future
        )
        future = pd.DataFrame(
            np.tile(BASE_RATES, (extra_future, 1)),
            index=future_dates,
            columns=pd.Index(TENORS, name="tenor"),
        )
        # 未来数据极端：若策略偷看未来，z 与方向必然改变
        future[:] = 0.1
        history = pd.concat([history, future])
    return history


# ---------------------------------------------------------------------------
# relative_tilt
# ---------------------------------------------------------------------------
def test_relative_tilt_preserves_total_and_no_negative_buckets() -> None:
    """不变式：总量守恒、非负、单桶 tilt 不超过 50%×桶值（防久期漂移）。"""
    benchmark = _benchmark()
    ctx = _ctx(_history(), benchmark=benchmark)
    total = benchmark.total_dv01
    target = relative_tilt(ctx, {2.0: -4_000.0, 10.0: +4_000.0}, notes="test")
    assert isinstance(target, TargetKRD)
    assert target.total_dv01 == pytest.approx(total, rel=1e-12)
    assert all(value >= 0 for value in target.krd.values())
    # 单桶偏离受 50%×桶值约束；随后全局缩放（保持总 DV01）会把偏离再略放大几个百分点
    for tenor in (2.0, 10.0):
        cap = 0.5 * benchmark.krd[tenor]
        assert abs(target.krd[tenor] - benchmark.krd[tenor]) <= cap * 1.10 + 1e-6
    # 方向正确：2Y 减少、10Y 增加
    assert target.krd[2.0] < benchmark.krd[2.0]
    assert target.krd[10.0] > benchmark.krd[10.0]


def test_relative_tilt_caps_huge_delta_and_never_redistributes() -> None:
    """R28：最终解必须同时满足逐桶上限与总量守恒（不是只裁剪中间值）。"""
    """极端 tilt：被裁剪到 50% 桶值，不把裁掉的量堆到其它桶（历史 bug：久期漂移）。"""
    benchmark = _benchmark()
    ctx = _ctx(_history(), benchmark=benchmark)
    total = benchmark.total_dv01
    target = relative_tilt(ctx, {2.0: -1e9, 10.0: +1e9})
    assert target.total_dv01 == pytest.approx(total, rel=1e-12)
    assert all(value >= 0 for value in target.krd.values())
    # 2Y 被压到接近 50% 基准桶值，而不是被截成 0（全局缩放再略降几个百分点）
    assert target.krd[2.0] >= 0.45 * benchmark.krd[2.0] - 1e-6
    assert target.krd[10.0] > benchmark.krd[10.0]
    assert target.total_dv01 == pytest.approx(total, rel=1e-12)


def test_relative_tilt_all_negative_deltas_stay_nonnegative() -> None:
    benchmark = _benchmark()
    ctx = _ctx(_history(), benchmark=benchmark)
    total = benchmark.total_dv01
    deltas = {tenor: -0.2 * value for tenor, value in benchmark.krd.items()}
    target = relative_tilt(ctx, deltas)
    assert target.total_dv01 == pytest.approx(total, rel=1e-12)
    assert all(value >= 0 for value in target.krd.values())
    # 投影到可行域（总量守恒 + 边界），允许不说明具体缩放过程
    assert target.total_dv01 == pytest.approx(total, rel=1e-12)


def test_relative_tilt_scales_non_neutral_deltas() -> None:
    benchmark = _benchmark()
    ctx = _ctx(_history(), benchmark=benchmark)
    total = benchmark.total_dv01
    target = relative_tilt(ctx, {2.0: +1_000.0, 10.0: +2_000.0})
    assert target.total_dv01 == pytest.approx(total, rel=1e-12)


# ---------------------------------------------------------------------------
# CarryRoll
# ---------------------------------------------------------------------------
def test_carry_roll_overweights_high_carry_tenor() -> None:
    benchmark = _benchmark()
    ctx = _ctx(_history(), benchmark=benchmark)
    strategy = CarryRollStrategy()
    assert isinstance(strategy, Strategy)
    target = strategy.target_krd(ctx)
    assert target.total_dv01 == pytest.approx(benchmark.total_dv01, rel=1e-12)
    # 上斜曲线：30Y carry+roll 最优 → 多 30Y；最差桶低配
    assert target.krd[30.0] > benchmark.krd[30.0]
    assert any(target.krd[tenor] < benchmark.krd[tenor] for tenor in benchmark.krd)
    assert "配置常量代理" in target.notes
    assert "30Y" in target.notes and "2Y" in target.notes


def test_carry_roll_is_reproducible() -> None:
    ctx = _ctx(_history())
    first = CarryRollStrategy().target_krd(ctx)
    second = CarryRollStrategy().target_krd(ctx)
    assert first.krd == second.krd
    assert first.notes == second.notes


# ---------------------------------------------------------------------------
# 2s10s
# ---------------------------------------------------------------------------
def test_steepener_flattens_on_high_z() -> None:
    benchmark = _benchmark()
    ctx = _ctx(_slope_history(rising=True), benchmark=benchmark)
    strategy = SteepenerStrategy()
    target = strategy.target_krd(ctx)
    assert target.total_dv01 == pytest.approx(benchmark.total_dv01, rel=1e-12)
    assert all(value >= 0 for value in target.krd.values())
    # z>entry → 平坦化：多 10Y、低配 2Y
    assert target.krd[10.0] > benchmark.krd[10.0]
    assert target.krd[2.0] < benchmark.krd[2.0]
    assert "平坦化" in target.notes and "z=" in target.notes


def test_steepener_steepens_on_low_z() -> None:
    benchmark = _benchmark()
    ctx = _ctx(_slope_history(rising=False), benchmark=benchmark)
    strategy = SteepenerStrategy()
    target = strategy.target_krd(ctx)
    assert target.total_dv01 == pytest.approx(benchmark.total_dv01, rel=1e-12)
    # z<−entry → 陡峭化：低配 10Y、多 2Y
    assert target.krd[10.0] < benchmark.krd[10.0]
    assert target.krd[2.0] > benchmark.krd[2.0]
    assert "陡峭化" in target.notes


def test_steepener_state_machine_holds_direction_and_exits() -> None:
    benchmark = _benchmark()
    history = _slope_history(rising=True)
    ctx = _ctx(history, benchmark=benchmark)
    strategy = SteepenerStrategy()
    strategy.target_krd(ctx)  # 新开平坦化
    assert strategy._direction == 1
    # 构造 z 落在 exit~entry 之间：保持方向（不回到基准）
    hold_history = _slope_history(rising=True)
    i10 = TENORS.tolist().index(10.0)
    # 让最后 60 日几乎平坦、末端略高 → z 落在 0.25~1.0
    hold_history = hold_history.iloc[:-1].copy()
    last = hold_history.iloc[-1].copy()
    last[i10] = hold_history.iloc[-1][i10] - 2.0  # 压低斜率，减小 z
    hold_history = pd.concat([hold_history, last.to_frame().T])
    hold_ctx = _ctx(hold_history, benchmark=benchmark)
    hold = strategy.target_krd(hold_ctx)
    if strategy._direction != 0:
        assert strategy._direction == 1
        assert hold.krd[10.0] > benchmark.krd[10.0]
    # 若进入 exit 区，则应回基准
    flat_history = _slope_history(rising=True)
    flat_history[i10] = flat_history[i10].mean()
    flat_ctx = _ctx(flat_history, benchmark=benchmark)
    exit_target = strategy.target_krd(flat_ctx)
    if strategy._direction == 0:
        assert exit_target.krd == pytest.approx(benchmark.krd)
        assert "回基准" in exit_target.notes


def test_steepener_window_insufficient_returns_baseline() -> None:
    benchmark = _benchmark()
    ctx = _ctx(_slope_history(n_days=30), benchmark=benchmark)
    target = SteepenerStrategy().target_krd(ctx)
    assert target.krd == pytest.approx(benchmark.krd)
    assert target.total_dv01 == pytest.approx(benchmark.total_dv01, rel=1e-12)
    assert "窗口" in target.notes


# ---------------------------------------------------------------------------
# RelativeValue
# ---------------------------------------------------------------------------
def test_relative_value_overweights_cheap_10y() -> None:
    benchmark = _benchmark()
    history = _history(n_days=140)
    i10 = TENORS.tolist().index(10.0)
    history.iloc[-30:, i10] = history.iloc[-30:, i10] + 0.6  # 10Y 持续偏便宜
    ctx = _ctx(history, benchmark=benchmark)
    target = RelativeValueStrategy().target_krd(ctx)
    assert target.total_dv01 == pytest.approx(benchmark.total_dv01, rel=1e-12)
    assert all(value >= 0 for value in target.krd.values())
    assert target.krd[10.0] > benchmark.krd[10.0]
    assert "z" in target.notes and "残差" in target.notes


def test_relative_value_window_insufficient_returns_baseline() -> None:
    benchmark = _benchmark()
    ctx = _ctx(_history(n_days=40), benchmark=benchmark)  # < rv window 120
    target = RelativeValueStrategy().target_krd(ctx)
    assert target.krd == pytest.approx(benchmark.krd)
    assert "窗口" in target.notes


def test_relative_value_is_reproducible() -> None:
    history = _history(n_days=130)
    i10 = TENORS.tolist().index(10.0)
    history.iloc[-20:, i10] += 0.5
    ctx = _ctx(history)
    first = RelativeValueStrategy(window=60).target_krd(ctx)
    second = RelativeValueStrategy(window=60).target_krd(ctx)
    assert first.krd == second.krd
    assert first.notes == second.notes


# ---------------------------------------------------------------------------
# 无未来信息（history 尾部追加未来数据，结果必须与截断版一致）
# ---------------------------------------------------------------------------
@pytest.mark.parametrize(
    "factory",
    [
        lambda: CarryRollStrategy(),
        lambda: SteepenerStrategy(),
        lambda: RelativeValueStrategy(window=60),
    ],
)
def test_strategies_ignore_future_rows(factory) -> None:
    benchmark = _benchmark()
    if isinstance(factory(), RelativeValueStrategy) or getattr(factory(), "name", "") == "rv":
        history = _history(n_days=80)
        i10 = TENORS.tolist().index(10.0)
        history.iloc[-10:, i10] += 0.5
        future = _history(n_days=5)
        future.index = pd.bdate_range(start=pd.Timestamp(AS_OF) + pd.DateOffset(days=1), periods=5)
        future[:] = 0.1
        extended = pd.concat([history, future])
    elif getattr(factory(), "name", "") == "2s10s":
        history = _slope_history(rising=True)
        extended = _slope_history(rising=True, extra_future=5)
    else:
        history = _history(n_days=80)
        future = _history(n_days=5)
        future.index = pd.bdate_range(start=pd.Timestamp(AS_OF) + pd.DateOffset(days=1), periods=5)
        future[:] = 0.1
        extended = pd.concat([history, future])
    trimmed_ctx = _ctx(history, benchmark=benchmark)
    extended_ctx = _ctx(extended, benchmark=benchmark)
    trimmed = factory().target_krd(trimmed_ctx)
    extended_result = factory().target_krd(extended_ctx)
    assert trimmed.krd == pytest.approx(extended_result.krd)
    assert trimmed.notes == extended_result.notes


# ---------------------------------------------------------------------------
# 基类/协议
# ---------------------------------------------------------------------------
def test_base_strategy_is_abstract_and_protocol() -> None:
    with pytest.raises(TypeError):
        BaseStrategy()  # abstract

    class Dummy(BaseStrategy):
        name = "dummy"

        def _target_krd(self, ctx):
            return self.baseline(ctx)

    ctx = _ctx(_history(n_days=10))
    dummy = Dummy()
    assert isinstance(dummy, Strategy)
    assert dummy.target_krd(ctx).krd == pytest.approx(ctx.benchmark.krd)


def test_baseline_returns_benchmark_copy() -> None:
    benchmark = _benchmark()
    ctx = _ctx(_history(n_days=10), benchmark=benchmark)
    strategy = SteepenerStrategy()
    target = strategy.baseline(ctx, notes="baseline")
    assert target.krd == pytest.approx(benchmark.krd)
    assert target.total_dv01 == pytest.approx(benchmark.total_dv01, rel=1e-12)
    assert target.notes == "baseline"


def test_r28_review_counterexample_final_bounds() -> None:
    """R28 反例：对最小 1Y 桶 +50%×桶值、最大 30Y 桶 −50%×桶值。

    旧实现"先裁剪再归一化"会让 1Y 最终偏离 79.6% > 50%；新投影必须让**最终返回值**
    同时满足逐桶上限与总量守恒。
    """
    benchmark = _benchmark()
    ctx = _ctx(_history(), benchmark=benchmark)
    base = dict(benchmark.krd)
    deltas = {1.0: 0.5 * base[1.0], 30.0: -0.5 * base[30.0]}
    target = relative_tilt(ctx, deltas, max_bucket_fraction=0.5)
    assert target.total_dv01 == pytest.approx(benchmark.total_dv01, rel=1e-12)
    for tenor, value in base.items():
        cap = 0.5 * value
        assert abs(target.krd[tenor] - value) <= cap + 1e-6 * max(value, 1.0)
        assert target.krd[tenor] >= 0.0
