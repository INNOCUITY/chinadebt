"""风险包测试（Phase 3，task-24）：VaR/ES + 压力测试，全重估 vs KRD 近似。

覆盖：
1. 历史模拟 / 参数正态 / Cornish-Fisher 的已知分布合理性（正态样本 VaR≈1.645σ、
   ES 为尾部条件均值、CF 对偏度方向、horizon 聚合、小样本差异）；
2. 固定组合全重估：平曲线 PnL=应计；付息日票息现金抵消除息跳空；
3. KRD 近似 vs 全重估：平行小冲击接近、非平行/蝶式显著偏离；
4. 压力测试：短久期 +100bp 平行近似 <2%、长久期凸性误差显著、蝶式误差大于
   陡峭化、历史情景可复现且默认不使用分析日之后的数据；
5. 输入契约与错误路径。

全部离线、确定性；用自造 mini 组合（不依赖引擎/账本）。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from scipy import stats

from ficc.config import get_config
from ficc.curve.curve import Curve
from ficc.data.schema import CANONICAL_TENORS, BondStatic
from ficc.portfolio.dates import settlement_for
from ficc.pricing.conventions import accrued_interest
from ficc.risk import (
    StressResult,
    historical_var_es,
    parametric_var_es,
    pnl_pct_from_cny,
    portfolio_pnl_fixed_revaluation,
    portfolio_pnl_krd_approx,
    stress_test,
)
from ficc.utils.calendar import add_business_days

TENORS = np.asarray(sorted(CANONICAL_TENORS), dtype=float)
AS_OF = date(2021, 6, 30)


# ---------------------------------------------------------------------------
# 夹具与工具
# ---------------------------------------------------------------------------
class _WeekdayCalendar:
    """最简工作日历：T+N = N 个工作日（测试用；生产传 CN_IB TradingCalendar）。"""

    def settlement_date(self, day: date, lag_days: int = 1) -> date:
        return add_business_days(day, lag_days)

    def is_trading_day(self, day: date) -> bool:
        return day.weekday() < 5


@pytest.fixture(scope="module")
def calendar() -> _WeekdayCalendar:
    return _WeekdayCalendar()


def business_dates(start: date, count: int) -> list[date]:
    days = [start]
    while len(days) < count:
        days.append(add_business_days(days[-1], 1))
    return days


def make_bond(
    code: str,
    issue: date,
    maturity: date,
    coupon_pct: float = 2.0,
    freq: int = 1,
) -> BondStatic:
    return BondStatic(
        code=code, issue_date=issue, maturity_date=maturity, coupon_pct=coupon_pct, freq=freq
    )


def aligned_bond(code: str, as_of: date, tenor: float, coupon_pct: float = 2.0) -> BondStatic:
    """起息/到期都落在同一月日，避免非整数年导致的不规则末期付息。"""
    issue = as_of.replace(year=as_of.year - 3)
    maturity = as_of.replace(year=as_of.year + int(tenor))
    freq = 2 if tenor >= 10 else 1
    return make_bond(code, issue, maturity, coupon_pct=coupon_pct, freq=freq)


def flat_curve(as_of: date, level: float = 2.0) -> Curve:
    return Curve(
        as_of=as_of, tenors=TENORS.copy(), rates=np.full_like(TENORS, level), method="hermite"
    )


def panel_from_rows(dates: list[date], rows: list[np.ndarray]) -> pd.DataFrame:
    frame = pd.DataFrame(np.asarray(rows, dtype=float), index=dates, columns=TENORS)
    frame.index = pd.Index(dates, name="date")
    return frame


def flat_panel(dates: list[date], level: float = 2.0) -> pd.DataFrame:
    return panel_from_rows(dates, [np.full_like(TENORS, level) for _ in dates])


# ---------------------------------------------------------------------------
# 1. VaR / ES
# ---------------------------------------------------------------------------
def test_historical_var_es_matches_known_normal() -> None:
    rng = np.random.default_rng(20260101)
    pnl = pd.Series(rng.normal(0.0, 1.0, 50000))
    results = historical_var_es(pnl, confidences=(0.95, 0.99))
    assert [r.method for r in results] == ["historical", "historical"]
    assert results[0].confidence == pytest.approx(0.95)
    assert results[0].var_pct == pytest.approx(1.645, abs=0.03)
    assert results[0].es_pct == pytest.approx(2.062, abs=0.06)
    assert results[1].var_pct == pytest.approx(2.326, abs=0.04)
    assert results[1].es_pct == pytest.approx(2.665, abs=0.10)
    for result in results:
        assert result.es_pct >= result.var_pct  # ES 是尾部条件均值，必 >= VaR（损失口径）
        assert result.n_obs == 50000
        assert result.horizon_days == 1


def test_parametric_normal_var_es_analytic() -> None:
    rng = np.random.default_rng(7)
    pnl = pd.Series(rng.normal(0.1, 2.0, 20000))
    result = parametric_var_es(pnl, confidences=(0.95,), dist="normal")[0]
    mean = float(pnl.mean())
    std = float(pnl.std(ddof=0))
    z = float(stats.norm.ppf(0.05))
    expected_var = -(mean + std * z)
    expected_es = -mean + std * float(stats.norm.pdf(z)) / 0.05
    assert result.method == "parametric_normal"
    assert result.var_pct == pytest.approx(expected_var, rel=1e-12)
    assert result.es_pct == pytest.approx(expected_es, rel=1e-12)
    # 大样本对称分布下 CF 应与正态接近
    cf = parametric_var_es(pnl, confidences=(0.95,), dist="cornish_fisher")[0]
    assert cf.var_pct == pytest.approx(result.var_pct, rel=0.05)


def test_cornish_fisher_adjusts_for_skew() -> None:
    rng = np.random.default_rng(11)
    # 右偏样本：左尾比正态更薄 → CF 的 VaR 小于正态 VaR
    raw = rng.gamma(shape=4.0, scale=1.0, size=20000)
    pnl = pd.Series(raw - raw.mean())
    normal = parametric_var_es(pnl, confidences=(0.95,), dist="normal")[0]
    cf = parametric_var_es(pnl, confidences=(0.95,), dist="cornish_fisher")[0]
    assert stats.skew(pnl, bias=False) > 0.5
    assert cf.method == "parametric_cornish_fisher"
    assert cf.var_pct < normal.var_pct
    assert cf.es_pct <= normal.es_pct * 1.001
    assert cf.n_obs == normal.n_obs == 20000


def test_es_is_conditional_tail_mean() -> None:
    pnl = pd.Series(np.linspace(-10.0, 10.0, 101))
    confidence = 0.90
    result = historical_var_es(pnl, confidences=(confidence,))[0]
    losses = -pnl.to_numpy(dtype=float)
    var = float(np.quantile(losses, confidence))
    expected_es = float(losses[losses >= var].mean())
    assert result.var_pct == pytest.approx(var, rel=1e-12)
    assert result.es_pct == pytest.approx(expected_es, rel=1e-12)
    assert result.es_pct >= result.var_pct


def test_horizon_aggregation_and_small_sample() -> None:
    rng = np.random.default_rng(3)
    pnl = pd.Series(rng.normal(0.0, 1.0, 2000))
    one_day = parametric_var_es(pnl, confidences=(0.95,), dist="normal")[0]
    five_day = parametric_var_es(pnl, horizon_days=5, confidences=(0.95,), dist="normal")[0]
    assert five_day.var_pct == pytest.approx(one_day.var_pct * np.sqrt(5.0), rel=0.03)
    assert five_day.n_obs == len(pnl) - 5 + 1
    hist_five = historical_var_es(pnl, horizon_days=5, confidences=(0.95,))[0]
    assert hist_five.n_obs == len(pnl) - 5 + 1
    # 小样本 + 厚尾：历史分位比正态更保守
    small = pd.Series(np.r_[rng.normal(0.0, 0.1, 27), -8.0, -6.0, -4.0])
    hist = historical_var_es(small, confidences=(0.95,))[0]
    normal = parametric_var_es(small, confidences=(0.95,), dist="normal")[0]
    assert hist.var_pct > normal.var_pct * 1.3


def test_var_input_validation_and_pct_conversion() -> None:
    with pytest.raises(ValueError, match="置信度"):
        historical_var_es(pd.Series([-1.0, 1.0]), confidences=(0.0,))
    with pytest.raises(ValueError, match="dist"):
        parametric_var_es(pd.Series([-1.0, 1.0, 2.0]), dist="student_t")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="horizon_days"):
        parametric_var_es(pd.Series([-1.0, 1.0, 2.0]), horizon_days=3)
    with pytest.raises(ValueError, match="Cornish-Fisher"):
        parametric_var_es(pd.Series([-1.0, 1.0, 2.0]), dist="cornish_fisher")
    with pytest.raises(ValueError, match="PnL"):
        historical_var_es(pd.Series([1.0]))

    cny = pd.Series([-1_000.0, 500.0])
    pct = pnl_pct_from_cny(cny, 100_000.0)
    assert pct.tolist() == pytest.approx([-1.0, 0.5])
    with pytest.raises(ValueError, match="portfolio_value"):
        pnl_pct_from_cny(cny, 0.0)


# ---------------------------------------------------------------------------
# 2. 固定组合全重估
# ---------------------------------------------------------------------------
def test_fixed_revaluation_flat_curve_is_accrual(calendar: _WeekdayCalendar) -> None:
    dates = business_dates(date(2021, 1, 4), 15)
    panel = flat_panel(dates)
    bond = make_bond("ACC", date(2018, 6, 30), date(2026, 6, 30), coupon_pct=2.0, freq=1)
    faces = {bond.code: 1_000_000.0}
    start, end = dates[1], dates[-1]
    pnl = portfolio_pnl_fixed_revaluation(
        faces, [bond], panel, calendar, start=start, end=end, include_coupon_cash=False
    )
    assert pnl.index.name == "date" and pnl.name == "pnl_full_revaluation"
    assert len(pnl) == len(dates) - 1
    for observation, value in pnl.items():
        previous = observations_previous(dates, observation)
        settle_current = settlement_for(calendar, observation)
        settle_previous = settlement_for(calendar, previous)
        expected = (
            1_000_000.0
            / 100.0
            * (accrued_interest(bond, settle_current) - accrued_interest(bond, settle_previous))
        )
        assert value == pytest.approx(expected, rel=0.02)
        assert value > 0.0
    # 窗口内没有票息 → 含票息现金的默认口径与 price-only 相同
    with_coupon = portfolio_pnl_fixed_revaluation(
        faces, [bond], panel, calendar, start=start, end=end
    )
    pd.testing.assert_series_equal(pnl, with_coupon)


def observations_previous(dates: list[date], day: date) -> date:
    return dates[dates.index(day) - 1]


def test_coupon_cash_offsets_ex_coupon_jump(calendar: _WeekdayCalendar) -> None:
    dates = business_dates(date(2021, 6, 1), 22)  # 覆盖 2021-06-15 付息日
    panel = flat_panel(dates)
    bond = make_bond("CPN", date(2018, 6, 15), date(2026, 6, 15), coupon_pct=2.0, freq=2)
    faces = {bond.code: 1_000_000.0}
    price_only = portfolio_pnl_fixed_revaluation(
        faces, [bond], panel, calendar, start=dates[1], end=dates[-1], include_coupon_cash=False
    )
    with_coupon = portfolio_pnl_fixed_revaluation(
        faces, [bond], panel, calendar, start=dates[1], end=dates[-1]
    )
    coupon_amount = 1_000_000.0 / 100.0 * 2.0 / 2.0  # 10,000 元
    drop_day = price_only.idxmin()
    assert float(price_only.min()) < -0.9 * coupon_amount  # 除息：全价机械下降近一个票息
    assert float(with_coupon.loc[drop_day]) > 0.0  # 计入票息现金后不再跳空
    difference = with_coupon - price_only
    assert float(difference.sum()) == pytest.approx(coupon_amount, rel=1e-9)
    assert float(difference.loc[drop_day]) == pytest.approx(coupon_amount, rel=1e-9)


# ---------------------------------------------------------------------------
# 3. KRD 近似 vs 全重估（逐日 PnL 序列）
# ---------------------------------------------------------------------------
def test_krd_approx_parallel_close_and_nonparallel_diverges(
    calendar: _WeekdayCalendar,
) -> None:
    dates = business_dates(date(2021, 1, 4), 40)
    bonds = [
        aligned_bond("K2", AS_OF, 2.0, coupon_pct=2.0),
        aligned_bond("K5", AS_OF, 5.0, coupon_pct=2.0),
        aligned_bond("K10", AS_OF, 10.0, coupon_pct=3.0),
    ]
    # 起息改到窗口之前，避免“窗口内没有历史价”问题
    bonds = [
        make_bond(b.code, date(2018, 6, 30), b.maturity_date, b.coupon_pct, int(b.freq))
        for b in bonds
    ]
    faces = {bond.code: 1_000_000.0 for bond in bonds}
    rng = np.random.default_rng(7)
    count = len(dates)
    parallel = rng.normal(0.0, 3.0, count)
    steep = rng.normal(0.0, 3.0, count)
    butterfly = rng.normal(0.0, 3.0, count)

    def build(mode: str) -> pd.DataFrame:
        rows: list[np.ndarray] = []
        for index in range(count):
            row = np.full_like(TENORS, 2.0)
            if mode == "parallel":
                row = row + parallel[index] / 100.0
            elif mode == "steep":
                row[np.isclose(TENORS, 2.0)] += steep[index] / 100.0
                row[np.isclose(TENORS, 10.0)] -= steep[index] / 100.0
            else:
                row[np.isclose(TENORS, 5.0)] += butterfly[index] / 100.0
                row[np.isclose(TENORS, 2.0)] -= butterfly[index] / 200.0
                row[np.isclose(TENORS, 10.0)] -= butterfly[index] / 200.0
            rows.append(row)
        return panel_from_rows(dates, rows)

    errors: dict[str, float] = {}
    for mode in ("parallel", "steep", "butterfly"):
        panel = build(mode)
        full = portfolio_pnl_fixed_revaluation(
            faces, bonds, panel, calendar, start=dates[5], end=dates[-1]
        )
        approx = portfolio_pnl_krd_approx(
            faces, bonds, panel, calendar, start=dates[5], end=dates[-1]
        )
        difference = full - approx
        errors[mode] = float(np.sqrt(np.mean(difference**2)) / full.std(ddof=0))
        assert full.corr(approx) > 0.9

    assert errors["parallel"] < 0.10
    assert errors["steep"] > 2.0 * errors["parallel"]  # 非平行日变化：线性桶近似误差明显放大
    assert errors["butterfly"] > 0.40  # 蝶式（曲率）在桶间几乎不可由线性 KRD 解释


# ---------------------------------------------------------------------------
# 4. 压力测试
# ---------------------------------------------------------------------------
def _scenario(name: str, kind: str, bp: float) -> dict[str, object]:
    return {"name": name, "type": kind, "bp": bp}


def test_stress_parallel_short_duration_approx_close(calendar: _WeekdayCalendar) -> None:
    curve = flat_curve(AS_OF)
    bond = aligned_bond("S2", AS_OF, 2.0)
    results = stress_test(
        {bond.code: 1_000_000.0},
        [bond],
        curve,
        scenarios=[_scenario("平行上移100bp", "parallel", 100.0)],
        calendar=calendar,
    )
    result = results[0]
    assert isinstance(result, StressResult)
    assert result.executed is True
    assert result.pnl_full < 0.0 and result.pnl_krd < 0.0
    assert result.nav > 0.0
    assert result.diff == pytest.approx(result.pnl_full - result.pnl_krd)
    assert result.diff_bp == pytest.approx(result.diff / result.nav * 1e4)
    # 短久期 + 线性近似：+100bp 的凸性/映射误差应 <2%
    assert result.error_ratio is not None
    assert result.error_ratio < 0.02


def test_stress_long_duration_convexity_error(calendar: _WeekdayCalendar) -> None:
    curve = flat_curve(AS_OF)
    bond = aligned_bond("S30", AS_OF, 30.0, coupon_pct=3.5)
    results = stress_test(
        {bond.code: 1_000_000.0},
        [bond],
        curve,
        scenarios=[
            _scenario("平行上移25bp", "parallel", 25.0),
            _scenario("平行上移100bp", "parallel", 100.0),
        ],
        calendar=calendar,
    )
    cheap, large = results
    assert large.pnl_full < 0.0 and large.pnl_krd < 0.0
    # 长久期 + 大冲击：KRD 线性近似忽略凸性，误差显著且随幅度放大
    assert cheap.error_ratio is not None and large.error_ratio is not None
    assert large.error_ratio > 0.05
    assert large.error_ratio > cheap.error_ratio * 2.0


def test_stress_butterfly_error_larger_than_steepener(calendar: _WeekdayCalendar) -> None:
    curve = flat_curve(AS_OF)
    bond = aligned_bond("S10", AS_OF, 10.0, coupon_pct=3.0)
    results = stress_test(
        {bond.code: 1_000_000.0},
        [bond],
        curve,
        scenarios=[
            _scenario("平行25bp", "parallel", 25.0),
            _scenario("陡峭化25bp", "steepen", 25.0),
            _scenario("蝶式25bp", "butterfly", 25.0),
        ],
        calendar=calendar,
    )
    _parallel, steep, butterfly = results
    for result in results:
        assert result.error_ratio is not None
    # 陡峭化能被 KRD 的关键期限桶较好捕捉（不是主要失效项）；蝶式/曲率才是
    assert steep.error_ratio is not None and steep.error_ratio < 0.05
    assert butterfly.error_ratio is not None and butterfly.error_ratio > 0.03
    assert butterfly.error_ratio > 2.5 * steep.error_ratio


def test_stress_historical_reproducible_and_no_future(calendar: _WeekdayCalendar) -> None:
    dates = business_dates(date(2021, 1, 4), 80)
    as_of = dates[60]
    rows: list[np.ndarray] = []
    for index in range(len(dates)):
        level = 2.0 + 0.30 * np.sin(index / 9.0)
        slope = 0.15 * np.sin(index / 13.0)
        rows.append(level + slope * (TENORS / 30.0) + 0.05 * np.cos(index / 5.0))
    panel = panel_from_rows(dates, rows)
    curve = Curve(
        as_of=as_of,
        tenors=TENORS.copy(),
        rates=panel.loc[as_of].to_numpy(dtype=float),
        method="hermite",
    )
    bond = make_bond("H10", date(2010, 6, 30), date(2035, 6, 30), coupon_pct=3.0, freq=2)
    faces = {bond.code: 5_000_000.0}
    scenario = {
        "name": "历史重演",
        "type": "historical",
        "start": dates[10],
        "end": dates[40],
        "window": 5,
    }
    first = stress_test(faces, [bond], curve, scenarios=[scenario], calendar=calendar, panel=panel)
    second = stress_test(faces, [bond], curve, scenarios=[scenario], calendar=calendar, panel=panel)
    assert first == second  # dataclass 逐字段可复现
    assert first[0].executed is True
    assert "历史变动重演" in first[0].note
    assert first[0].error_ratio is not None

    # 窗口整段晚于分析日：默认不执行（避免未来数据），显式允许才重演
    future = {
        "name": "未来窗口",
        "type": "historical",
        "start": dates[65],
        "end": dates[75],
        "window": 5,
    }
    skipped = stress_test(faces, [bond], curve, scenarios=[future], calendar=calendar, panel=panel)[
        0
    ]
    assert skipped.executed is False
    assert "晚于分析日" in skipped.note
    assert skipped.pnl_full == 0.0 and skipped.pnl_krd == 0.0
    allowed = stress_test(
        faces,
        [bond],
        curve,
        scenarios=[future],
        calendar=calendar,
        panel=panel,
        allow_future_history=True,
    )[0]
    assert allowed.executed is True
    assert "显式假设" in allowed.note


def test_stress_validation_errors(calendar: _WeekdayCalendar) -> None:
    curve = flat_curve(AS_OF)
    bond = aligned_bond("V", AS_OF, 5.0)
    faces = {bond.code: 1_000_000.0}
    with pytest.raises(ValueError, match="未知情景类型"):
        stress_test(
            faces, [bond], curve, scenarios=[{"name": "x", "type": "twist"}], calendar=calendar
        )
    with pytest.raises(ValueError, match="非负"):
        stress_test(
            faces,
            [bond],
            curve,
            scenarios=[_scenario("x", "steepen", -25.0)],
            calendar=calendar,
        )
    with pytest.raises(ValueError, match="缺少 bp"):
        stress_test(
            faces, [bond], curve, scenarios=[{"name": "x", "type": "parallel"}], calendar=calendar
        )
    with pytest.raises(ValueError, match="panel"):
        stress_test(
            faces,
            [bond],
            curve,
            scenarios=[
                {
                    "name": "h",
                    "type": "historical",
                    "start": date(2020, 1, 1),
                    "end": date(2020, 2, 1),
                }
            ],
            calendar=calendar,
        )


def test_stress_real_panel_config_scenarios(calendar: _WeekdayCalendar) -> None:
    """真实曲线面板 + 默认配置情景：历史窗口可复现；分析日之后的情景默认不参与。"""
    path = Path(__file__).resolve().parents[1] / "data" / "processed" / "curve_long.parquet"
    if not path.exists():
        pytest.skip(f"缺少中债实测数据 {path}")
    cfg = get_config()
    long = pd.read_parquet(path)
    panel = (
        long.pivot(index="date", columns="tenor", values="ytm_pct")
        .sort_index()
        .reindex(columns=list(TENORS))
    )
    as_of = date(2020, 12, 31)
    curve = Curve(
        as_of=as_of,
        tenors=TENORS.copy(),
        rates=panel.loc[pd.Timestamp(as_of)].to_numpy(dtype=float),
        method="hermite",
    )
    bond = make_bond("R10", date(2010, 6, 30), date(2035, 6, 30), coupon_pct=3.0, freq=2)
    faces = {bond.code: 1_000_000.0}
    first = stress_test(
        faces,
        [bond],
        curve,
        scenarios=cfg.risk.stress_scenarios,
        calendar=calendar,
        cfg=cfg,
        panel=panel,
    )
    second = stress_test(
        faces,
        [bond],
        curve,
        scenarios=cfg.risk.stress_scenarios,
        calendar=calendar,
        cfg=cfg,
        panel=panel,
    )
    assert first == second
    assert len(first) == len(cfg.risk.stress_scenarios)
    by_name = {result.scenario: result for result in first}
    assert by_name["2013钱荒"].executed is True
    assert by_name["2020疫情"].executed is True
    assert by_name["2022理财赎回潮"].executed is False  # 晚于 as_of=2020-12-31
    assert "2013-" in by_name["2013钱荒"].note
    for result in first:
        if result.executed:
            assert np.isfinite(result.pnl_full) and np.isfinite(result.pnl_krd)
            assert result.nav > 0.0
