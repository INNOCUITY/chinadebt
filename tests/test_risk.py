"""风险包测试（Phase 3，task-25/R39+R40）：VaR/ES + 压力测试，同源即期口径。

覆盖：
1. 历史模拟 / 参数正态 / Cornish-Fisher 的已知分布合理性、ES 尾部均值、
   horizon 聚合、小样本差异；
2. legacy historical-aging 全重估（平曲线=应计、付息日票息现金抵消除息跳空）
   与旧 KRD 近似的平行/非平行偏离；
3. **R39 当前冻结组合 + 历史即期冲击**：复现审查反例 95% VaR/ES
   （31.82/43.33 万元，允许 ±3% 的 YTM→即期定价映射差），与历史老化口径并列；
   lookback 不改变组合；carry 开关对分布的单调影响；
4. **R40 同源即期定价**：`spot_shock` 单点 +1bp 与 `key_rate_durations` 逐位一致、
   解析 KRD 与中心差分一致、小冲击收敛（蝶式 0.001bp/陡平 0.01bp <1e-4，
   平行 1bp <1e-3），大幅平行/蝶式仍保留高阶残差；
5. 压力情景（平行/陡平/蝶式/历史）可复现、默认不使用分析日之后的数据。

全部离线、确定性；用自造 mini 组合（不依赖引擎/账本）；仅审查反例复现用到
仓库内的真实中债面板（缺失时 skip）。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest
from scipy import stats

from ficc.config import get_config
from ficc.curve.bootstrap import bootstrap_spot
from ficc.curve.curve import Curve
from ficc.data.schema import CANONICAL_TENORS, BondStatic
from ficc.portfolio.benchmark import build_benchmark, build_universe_bonds
from ficc.portfolio.dates import remaining_years, settlement_for
from ficc.pricing.analytics import key_rate_durations
from ficc.pricing.bond import dirty_price_from_ytm
from ficc.pricing.conventions import accrued_interest
from ficc.risk import (
    StressResult,
    historical_var_es,
    key_rate_weights,
    parametric_var_es,
    pnl_pct_from_cny,
    portfolio_krd_from_spot,
    portfolio_market_value,
    portfolio_pnl_current_portfolio_factors,
    portfolio_pnl_current_portfolio_krd,
    portfolio_pnl_fixed_revaluation,
    portfolio_pnl_krd_approx,
    price_from_spot,
    spot_shock,
    stress_test,
)
from ficc.utils.calendar import TradingCalendar, add_business_days

TENORS = np.asarray(sorted(CANONICAL_TENORS), dtype=float)
AS_OF = date(2021, 6, 30)
CONF = 0.95
# R39 审查反例（docs/code.md R39；YTM 空间冻结、price-only、默认基准组合）
REVIEW_VAR = 318_246.69828
REVIEW_ES = 433_273.83884
AGING_VAR = 356_731.66623
AGING_ES = 496_682.83234


# ---------------------------------------------------------------------------
# 夹具与工具
# ---------------------------------------------------------------------------
class _WeekdayCalendar:
    """最简工作日历：T+N = N 个工作日（生产传 CN_IB TradingCalendar）。"""

    def settlement_date(self, day: date, lag_days: int = 1) -> date:
        return add_business_days(day, lag_days)

    def is_trading_day(self, day: date) -> bool:
        return day.weekday() < 5

    def next_trading_day(self, day: date, n: int = 1) -> date:
        result = day
        for _ in range(n):
            result = add_business_days(result, 1)
        return result


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
    """起息/到期落在同一月日，避免非整数年导致的不规则末期付息。"""
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


def varying_panel(dates: list[date]) -> pd.DataFrame:
    """确定性变化的曲线：水平/斜率/曲率都有日间变化（用于冻结/老化对照）。"""
    rows: list[np.ndarray] = []
    count = len(dates)
    for index in range(count):
        level = 2.0 + 0.25 * np.sin(index / 7.0) + 0.3 * index / count
        slope = 0.25 * np.cos(index / 5.0)
        belly = 0.20 * np.sin(index / 3.0) * np.exp(-((TENORS - 5.0) ** 2) / 50.0)
        rows.append(level + slope * (TENORS / 30.0) + belly)
    return panel_from_rows(dates, rows)


def _flat_scenarios() -> list[dict[str, object]]:
    return [
        {"name": "收敛-平行1bp", "type": "parallel", "bp": 1.0},
        {"name": "收敛-陡峭化0.01bp", "type": "steepen", "bp": 0.01},
        {"name": "收敛-平坦化0.01bp", "type": "flatten", "bp": 0.01},
        {"name": "收敛-蝶式0.001bp", "type": "butterfly", "bp": 0.001},
        {"name": "平行25bp", "type": "parallel", "bp": 25.0},
        {"name": "平行100bp", "type": "parallel", "bp": 100.0},
        {"name": "蝶式25bp", "type": "butterfly", "bp": 25.0},
        {"name": "陡峭化25bp", "type": "steepen", "bp": 25.0},
    ]


def _ts(dates: list[date]) -> pd.Index:
    return pd.Index(dates, name="date")


# ---------------------------------------------------------------------------
# 1. VaR / ES 解析与统计性质
# ---------------------------------------------------------------------------
def test_historical_var_es_matches_known_normal() -> None:
    rng = np.random.default_rng(20260101)
    pnl = pd.Series(rng.normal(0.0, 1.0, 50000))
    results = historical_var_es(pnl, confidences=(0.95, 0.99))
    assert [result.method for result in results] == ["historical", "historical"]
    assert results[0].var_pct == pytest.approx(1.645, abs=0.03)
    assert results[0].es_pct == pytest.approx(2.062, abs=0.06)
    assert results[1].var_pct == pytest.approx(2.326, abs=0.04)
    assert results[1].es_pct == pytest.approx(2.665, abs=0.10)
    for result in results:
        assert result.es_pct >= result.var_pct
        assert result.n_obs == 50000


def test_parametric_normal_var_es_analytic() -> None:
    rng = np.random.default_rng(7)
    pnl = pd.Series(rng.normal(0.1, 2.0, 20000))
    result = parametric_var_es(pnl, confidences=(0.95,), dist="normal")[0]
    mean = float(pnl.mean())
    std = float(pnl.std(ddof=0))
    z = float(stats.norm.ppf(0.05))
    assert result.var_pct == pytest.approx(-(mean + std * z), rel=1e-12)
    assert result.es_pct == pytest.approx(-mean + std * float(stats.norm.pdf(z)) / 0.05, rel=1e-12)
    cf = parametric_var_es(pnl, confidences=(0.95,), dist="cornish_fisher")[0]
    assert cf.var_pct == pytest.approx(result.var_pct, rel=0.05)


def test_cornish_fisher_adjusts_for_skew() -> None:
    rng = np.random.default_rng(11)
    raw = rng.gamma(shape=4.0, scale=1.0, size=20000)
    pnl = pd.Series(raw - raw.mean())
    normal = parametric_var_es(pnl, confidences=(0.95,), dist="normal")[0]
    cf = parametric_var_es(pnl, confidences=(0.95,), dist="cornish_fisher")[0]
    assert stats.skew(pnl, bias=False) > 0.5
    assert cf.method == "parametric_cornish_fisher"
    assert cf.var_pct < normal.var_pct


def test_es_is_conditional_tail_mean() -> None:
    pnl = pd.Series(np.linspace(-10.0, 10.0, 101))
    result = historical_var_es(pnl, confidences=(0.90,))[0]
    losses = -pnl.to_numpy(dtype=float)
    var = float(np.quantile(losses, 0.90))
    expected_es = float(losses[losses >= var].mean())
    assert result.var_pct == pytest.approx(var, rel=1e-12)
    assert result.es_pct == pytest.approx(expected_es, rel=1e-12)


def test_horizon_aggregation_and_input_validation() -> None:
    rng = np.random.default_rng(3)
    pnl = pd.Series(rng.normal(0.0, 1.0, 2000))
    one_day = parametric_var_es(pnl, confidences=(0.95,), dist="normal")[0]
    five_day = parametric_var_es(pnl, horizon_days=5, confidences=(0.95,), dist="normal")[0]
    assert five_day.var_pct == pytest.approx(one_day.var_pct * np.sqrt(5.0), rel=0.03)
    assert five_day.n_obs == len(pnl) - 5 + 1
    assert historical_var_es(pnl, horizon_days=5, confidences=(0.95,))[0].n_obs == len(pnl) - 4

    with pytest.raises(ValueError, match="置信度"):
        historical_var_es(pd.Series([-1.0, 1.0]), confidences=(0.0,))
    with pytest.raises(ValueError, match="dist"):
        parametric_var_es(pd.Series([-1.0, 1.0, 2.0]), dist="student_t")  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="Cornish-Fisher"):
        parametric_var_es(pd.Series([-1.0, 1.0, 2.0]), dist="cornish_fisher")
    assert pnl_pct_from_cny(pd.Series([-1_000.0]), 100_000.0).iloc[0] == pytest.approx(-1.0)


# ---------------------------------------------------------------------------
# 2. legacy historical-aging 全重估（保留对照）
# ---------------------------------------------------------------------------
def test_legacy_revaluation_flat_curve_is_accrual(calendar: _WeekdayCalendar) -> None:
    dates = business_dates(date(2021, 1, 4), 15)
    panel = flat_panel(dates)
    bond = make_bond("ACC", date(2018, 6, 30), date(2026, 6, 30), coupon_pct=2.0, freq=1)
    faces = {bond.code: 1_000_000.0}
    pnl = portfolio_pnl_fixed_revaluation(
        faces, [bond], panel, calendar, start=dates[1], end=dates[-1], include_coupon_cash=False
    )
    assert pnl.index.name == "date" and pnl.name == "pnl_full_revaluation"
    for observation, value in pnl.items():
        previous = dates[dates.index(observation) - 1]
        expected = (
            1_000_000.0
            / 100.0
            * (
                accrued_interest(bond, settlement_for(calendar, observation))
                - accrued_interest(bond, settlement_for(calendar, previous))
            )
        )
        assert value == pytest.approx(expected, rel=0.02)
    with_coupon = portfolio_pnl_fixed_revaluation(
        faces, [bond], panel, calendar, start=dates[1], end=dates[-1]
    )
    pd.testing.assert_series_equal(pnl, with_coupon)


def test_legacy_coupon_cash_offsets_ex_coupon_jump(calendar: _WeekdayCalendar) -> None:
    dates = business_dates(date(2021, 6, 1), 22)
    panel = flat_panel(dates)
    bond = make_bond("CPN", date(2018, 6, 15), date(2026, 6, 15), coupon_pct=2.0, freq=2)
    faces = {bond.code: 1_000_000.0}
    price_only = portfolio_pnl_fixed_revaluation(
        faces, [bond], panel, calendar, start=dates[1], end=dates[-1], include_coupon_cash=False
    )
    with_coupon = portfolio_pnl_fixed_revaluation(
        faces, [bond], panel, calendar, start=dates[1], end=dates[-1]
    )
    coupon_amount = 1_000_000.0 / 100.0 * 2.0 / 2.0
    drop_day = price_only.idxmin()
    assert float(price_only.min()) < -0.9 * coupon_amount
    assert float(with_coupon.loc[drop_day]) > 0.0
    assert float((with_coupon - price_only).sum()) == pytest.approx(coupon_amount, rel=1e-9)


def test_legacy_krd_approx_parallel_close_nonparallel_diverges(
    calendar: _WeekdayCalendar,
) -> None:
    dates = business_dates(date(2021, 1, 4), 40)
    bonds = [
        make_bond("K2", date(2018, 6, 30), date(2023, 6, 30), 2.0, 1),
        make_bond("K5", date(2018, 6, 30), date(2026, 6, 30), 2.0, 1),
        make_bond("K10", date(2018, 6, 30), date(2031, 6, 30), 3.0, 2),
    ]
    faces = {bond.code: 1_000_000.0 for bond in bonds}
    rng = np.random.default_rng(7)
    count = len(dates)
    parallel = rng.normal(0.0, 3.0, count)
    steep = rng.normal(0.0, 3.0, count)
    butterfly = rng.normal(0.0, 3.0, count)

    def panel(mode: str) -> pd.DataFrame:
        rows: list[np.ndarray] = []
        for index in range(count):
            row = np.full_like(TENORS, 2.0)
            if mode == "parallel":
                row += parallel[index] / 100.0
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
        frame = panel(mode)
        full = portfolio_pnl_fixed_revaluation(
            faces, bonds, frame, calendar, start=dates[5], end=dates[-1]
        )
        approx = portfolio_pnl_krd_approx(
            faces, bonds, frame, calendar, start=dates[5], end=dates[-1]
        )
        errors[mode] = float(np.sqrt(np.mean((full - approx) ** 2)) / full.std(ddof=0))
    assert errors["parallel"] < 0.10
    assert errors["steep"] > 2.0 * errors["parallel"]
    assert errors["butterfly"] > 0.40


# ---------------------------------------------------------------------------
# 3. R39：current_frozen（当前组合 + 历史即期冲击）
# ---------------------------------------------------------------------------
def _load_real_market() -> tuple[pd.DataFrame, TradingCalendar] | None:
    path = Path(__file__).resolve().parents[1] / "data" / "processed" / "curve_long.parquet"
    if not path.exists():
        return None
    long = pd.read_parquet(path)
    panel = (
        long.pivot(index="date", columns="tenor", values="ytm_pct")
        .sort_index()
        .reindex(columns=list(TENORS))
    )
    return panel, TradingCalendar.from_dates(panel.index)


def _curve_at(panel: pd.DataFrame, day: date) -> Curve:
    key: object = pd.Timestamp(day) if isinstance(panel.index, pd.DatetimeIndex) else day
    return Curve(
        as_of=day,
        tenors=TENORS.copy(),
        rates=panel.loc[key, list(TENORS)].to_numpy(dtype=float),
        method="hermite",
    )


def _var_es_cny(pnl: pd.Series, nav: float, confidence: float = CONF) -> tuple[float, float]:
    result = historical_var_es(pnl_pct_from_cny(pnl, nav), confidences=(confidence,))[0]
    return result.var_pct / 100.0 * nav, result.es_pct / 100.0 * nav


def test_r39_current_frozen_reproduces_review_counterexample() -> None:
    market = _load_real_market()
    if market is None:
        pytest.skip("缺少中债实测面板，跳过审查反例复现")
    panel, calendar = market
    cfg = get_config()
    as_of = date(2020, 12, 31)
    trading_days = [day for day in calendar.dates() if day <= as_of]
    issue_day = trading_days[-251]  # 分析日前 250 个交易日建仓（与 lookback 解耦）
    bonds = build_universe_bonds(_curve_at(panel, issue_day), issue_day, cfg=cfg)
    benchmark = build_benchmark(
        _curve_at(panel, as_of), as_of, bonds=bonds, calendar=calendar, cfg=cfg
    )
    faces = {code: float(face) for code, face in benchmark.faces.items()}
    nav = benchmark.market_value

    frozen = portfolio_pnl_current_portfolio_factors(
        faces, bonds, panel, as_of, lookback=250, include_carry=False, cfg=cfg, calendar=calendar
    )
    frozen_var, frozen_es = _var_es_cny(frozen, nav)
    # 主函数是即期定价口径，与审查的纯 YTM 冻结口径存在 YTM→即期映射差，要求 ±3%
    assert frozen_var == pytest.approx(REVIEW_VAR, rel=0.03)
    assert frozen_es == pytest.approx(REVIEW_ES, rel=0.03)
    # 同一 basis 的 KRD 近似与全重估接近
    frozen_krd = portfolio_pnl_current_portfolio_krd(
        faces, bonds, panel, as_of, lookback=250, include_carry=False, cfg=cfg, calendar=calendar
    )
    krd_var, krd_es = _var_es_cny(frozen_krd, nav)
    assert abs(krd_var / frozen_var - 1.0) < 0.05
    assert abs(krd_es / frozen_es - 1.0) < 0.05

    # 纯 YTM 空间冻结口径（审查反例原始定义）应逐位复现 31.82/43.33 万元
    curve_analysis = _curve_at(panel, as_of)
    settlement = settlement_for(calendar, as_of, cfg=cfg)
    base_dirty = {
        bond.code: dirty_price_from_ytm(
            bond, settlement, float(curve_analysis.rate(remaining_years(bond, settlement))), cfg=cfg
        )
        for bond in bonds
    }
    analysis_rates = np.asarray(curve_analysis.rates, dtype=float)
    ytm_pnl: list[float] = []
    previous_day = trading_days[-251]
    for day in trading_days[-250:]:
        delta = panel.loc[pd.Timestamp(day)].to_numpy(dtype=float) - panel.loc[
            pd.Timestamp(previous_day)
        ].to_numpy(dtype=float)
        shocked = Curve(
            as_of=as_of, tenors=TENORS.copy(), rates=analysis_rates + delta, method="hermite"
        )
        total = 0.0
        for bond in bonds:
            remaining = remaining_years(bond, settlement)
            total += (
                faces[bond.code]
                / 100.0
                * (
                    dirty_price_from_ytm(bond, settlement, float(shocked.rate(remaining)), cfg=cfg)
                    - base_dirty[bond.code]
                )
            )
        ytm_pnl.append(total)
        previous_day = day
    ytm_var, ytm_es = _var_es_cny(pd.Series(ytm_pnl), nav)
    assert ytm_var == pytest.approx(REVIEW_VAR, rel=1e-6)
    assert ytm_es == pytest.approx(REVIEW_ES, rel=1e-6)

    # 旧 historical-aging 口径并列披露：35.67/49.67 万元
    aging = portfolio_pnl_fixed_revaluation(
        faces,
        bonds,
        panel,
        calendar,
        start=trading_days[-250],
        end=as_of,
        cfg=cfg,
    )
    aging_var, aging_es = _var_es_cny(aging, nav)
    assert aging_var == pytest.approx(AGING_VAR, rel=0.03)
    assert aging_es == pytest.approx(AGING_ES, rel=0.03)
    assert abs(frozen_var / aging_var - 1.0) > 0.05  # 两种口径对象不同，必须并列


def test_r39_lookback_does_not_change_portfolio(calendar: _WeekdayCalendar) -> None:
    dates = business_dates(date(2021, 1, 4), 130)
    panel = varying_panel(dates)
    as_of = dates[-1]
    bond = make_bond("FZ", date(2018, 6, 30), date(2028, 6, 30), coupon_pct=3.0, freq=2)
    faces = {bond.code: 10_000_000.0}
    short = portfolio_pnl_current_portfolio_factors(
        faces, [bond], panel, as_of, lookback=60, cfg=get_config(), calendar=calendar
    )
    long_result = portfolio_pnl_current_portfolio_factors(
        faces, [bond], panel, as_of, lookback=120, cfg=get_config(), calendar=calendar
    )
    assert len(short) == 60 and len(long_result) == 120
    # 组合/分析日/冲击定义都与 lookback 无关：重叠历史样本逐位相同
    pd.testing.assert_series_equal(long_result.loc[short.index], short, check_exact=True)
    assert np.isfinite(short.to_numpy()).all()
    # 改变 lookback 也不改变分析日组合价值（面值/条款冻结）
    nav = portfolio_market_value(
        faces, [bond], _curve_at(panel, as_of), calendar=calendar, cfg=get_config()
    )
    assert np.isfinite(nav) and nav > 0.0


def test_r39_include_carry_monotone(calendar: _WeekdayCalendar) -> None:
    dates = business_dates(date(2021, 1, 4), 80)
    panel = flat_panel(dates)
    as_of = dates[-1]
    bond = make_bond("CARRY", date(2018, 6, 30), date(2028, 6, 30), coupon_pct=2.0, freq=1)
    faces = {bond.code: 10_000_000.0}
    cfg = get_config()
    price_only = portfolio_pnl_current_portfolio_factors(
        faces, [bond], panel, as_of, lookback=60, include_carry=False, cfg=cfg, calendar=calendar
    )
    with_carry = portfolio_pnl_current_portfolio_factors(
        faces, [bond], panel, as_of, lookback=60, include_carry=True, cfg=cfg, calendar=calendar
    )
    carry = float((with_carry - price_only).mean())
    assert carry > 0.0  # 正 carry：多头持有收益
    assert float((with_carry - price_only).std(ddof=0)) < 1e-9  # 只平移均值
    nav = portfolio_market_value(faces, [bond], _curve_at(panel, as_of), calendar=calendar, cfg=cfg)
    var_no_carry, es_no_carry = _var_es_cny(price_only, nav)
    var_carry, es_carry = _var_es_cny(with_carry, nav)
    assert var_carry < var_no_carry  # 正 carry 使损失分布整体右移 → VaR/ES 下降
    assert es_carry < es_no_carry
    # 持有期 carry 近似 = 面值 × 票息 / 365 × 1 日（票面=曲线，价差≈0）
    expected = 10_000_000.0 * 2.0 / 100.0 / 365.0
    assert carry == pytest.approx(expected, rel=0.02)


# ---------------------------------------------------------------------------
# 4. R40：同源即期定价与冲击基
# ---------------------------------------------------------------------------
def _spot_setup(calendar: _WeekdayCalendar):
    cfg = get_config()
    curve = flat_curve(AS_OF)
    settlement = settlement_for(calendar, AS_OF, cfg=cfg)
    spot = bootstrap_spot(curve, settlement=settlement, calendar=calendar)
    bonds = [
        aligned_bond("S2", AS_OF, 2.0),
        aligned_bond("S10", AS_OF, 10.0, coupon_pct=3.0),
        aligned_bond("S30", AS_OF, 30.0, coupon_pct=3.5),
    ]
    return cfg, curve, settlement, spot, bonds


def test_spot_shock_single_bump_equals_key_rate_durations() -> None:
    calendar = _WeekdayCalendar()
    cfg, _curve, settlement, spot, bonds = _spot_setup(calendar)
    keys = np.asarray(sorted(float(t) for t in cfg.risk.key_tenors_krd), dtype=float)
    base_prices = {bond.code: price_from_spot(bond, settlement, spot, cfg=cfg) for bond in bonds}
    for bond in bonds:
        krd = key_rate_durations(bond, settlement, spot, key_tenors=keys, cfg=cfg)
        for index, tenor in enumerate(keys):
            bump = np.zeros(keys.size)
            bump[index] = 1.0
            shocked = spot_shock(spot, keys, bump)
            difference = (
                price_from_spot(bond, settlement, shocked, cfg=cfg) - base_prices[bond.code]
            )
            assert difference == pytest.approx(float(krd[float(tenor)]), rel=1e-9, abs=1e-12)


def test_spot_shock_preserves_metadata_and_zero_shift_identity() -> None:
    calendar = _WeekdayCalendar()
    cfg, _curve, settlement, spot, bonds = _spot_setup(calendar)
    keys = np.asarray(sorted(float(t) for t in cfg.risk.key_tenors_krd), dtype=float)
    identity = spot_shock(spot, keys, np.zeros(keys.size))
    assert identity.as_of == spot.as_of
    assert identity.observation_date == spot.observation_date
    assert identity.settlement_date == spot.settlement_date
    assert identity.calendar is spot.calendar
    assert identity.assumptions["shock_basis"] == "spot_key_linear_flat"
    for bond in bonds:
        assert price_from_spot(bond, settlement, identity, cfg=cfg) == pytest.approx(
            price_from_spot(bond, settlement, spot, cfg=cfg), rel=1e-14
        )
    # key_rate_weights：每列权重和恒为 1（平行=所有关键点同向时曲线整体平移）
    times = np.linspace(0.1, 40.0, 50)
    weights = key_rate_weights(keys, times)
    assert np.allclose(weights.sum(axis=0), 1.0)


def test_portfolio_krd_analytic_matches_central_difference() -> None:
    calendar = _WeekdayCalendar()
    cfg, _curve, settlement, spot, bonds = _spot_setup(calendar)
    keys = np.asarray(sorted(float(t) for t in cfg.risk.key_tenors_krd), dtype=float)
    faces = {bond.code: 10_000_000.0 for bond in bonds}
    analytic = portfolio_krd_from_spot(faces, bonds, settlement, spot, keys, cfg=cfg)
    eps_bp = 0.01
    for index, tenor in enumerate(keys):
        up = np.zeros(keys.size)
        down = np.zeros(keys.size)
        up[index] = eps_bp
        down[index] = -eps_bp
        central_signed = 0.0
        for bond in bonds:
            face = faces[bond.code]
            central_signed += (
                face
                / 100.0
                * (
                    price_from_spot(bond, settlement, spot_shock(spot, keys, up), cfg=cfg)
                    - price_from_spot(bond, settlement, spot_shock(spot, keys, down), cfg=cfg)
                )
                / (2.0 * eps_bp)
            )
        assert -central_signed == pytest.approx(analytic[float(tenor)], rel=1e-6)
        # 与 key_rate_durations 的 1bp 单边差分差 <0.2%（R40 指出的 FD 偏差）
        fd = sum(
            -float(
                key_rate_durations(bond, settlement, spot, key_tenors=keys, cfg=cfg)[float(tenor)]
            )
            * faces[bond.code]
            / 100.0
            for bond in bonds
        )
        assert analytic[float(tenor)] == pytest.approx(fd, rel=0.002)


def test_r40_small_shock_convergence() -> None:
    calendar = _WeekdayCalendar()
    cfg = get_config()
    curve = flat_curve(AS_OF)
    bonds = [
        aligned_bond("C2", AS_OF, 2.0),
        aligned_bond("C5", AS_OF, 5.0),
        aligned_bond("C10", AS_OF, 10.0, coupon_pct=3.0),
        aligned_bond("C30", AS_OF, 30.0, coupon_pct=3.5),
    ]
    faces = {bond.code: 25_000_000.0 for bond in bonds}
    results = stress_test(
        faces, bonds, curve, scenarios=_flat_scenarios(), calendar=calendar, cfg=cfg
    )
    by_name = {result.scenario: result for result in results}
    assert by_name["收敛-平行1bp"].error_ratio < 1e-3
    assert by_name["收敛-陡峭化0.01bp"].error_ratio < 1e-4
    assert by_name["收敛-平坦化0.01bp"].error_ratio < 1e-4
    assert by_name["收敛-蝶式0.001bp"].error_ratio < 1e-4
    assert by_name["收敛-蝶式0.001bp"].error_ratio < 1e-6


def test_r40_large_shocks_keep_higher_order_residuals() -> None:
    calendar = _WeekdayCalendar()
    cfg = get_config()
    curve = flat_curve(AS_OF)
    bonds = [
        aligned_bond("L10", AS_OF, 10.0, coupon_pct=3.0),
        aligned_bond("L30", AS_OF, 30.0, coupon_pct=3.5),
    ]
    faces = {bond.code: 25_000_000.0 for bond in bonds}
    results = stress_test(
        faces, bonds, curve, scenarios=_flat_scenarios(), calendar=calendar, cfg=cfg
    )
    by_name = {result.scenario: result for result in results}
    assert by_name["平行100bp"].error_ratio > 0.02  # 凸性：大平行冲击不可用线性近似
    assert by_name["蝶式25bp"].error_ratio > 0.005  # 曲率/桶间形状的高阶项
    for result in results:
        if result.executed:
            assert result.price_function == "price_from_spot"
            assert result.shock_basis == "spot_key_linear_flat"
            assert result.diff == pytest.approx(result.pnl_full - result.pnl_krd)


# ---------------------------------------------------------------------------
# 5. 压力情景（历史/配置）
# ---------------------------------------------------------------------------
def test_stress_parallel_short_duration_close(calendar: _WeekdayCalendar) -> None:
    cfg = get_config()
    curve = flat_curve(AS_OF)
    bond = aligned_bond("S2", AS_OF, 2.0)
    result = stress_test(
        {bond.code: 1_000_000.0},
        [bond],
        curve,
        scenarios=[{"name": "平行上移100bp", "type": "parallel", "bp": 100.0}],
        calendar=calendar,
        cfg=cfg,
    )[0]
    assert isinstance(result, StressResult)
    assert result.executed is True and result.nav > 0.0
    assert result.pnl_full < 0.0 and result.pnl_krd < 0.0
    assert result.diff_bp == pytest.approx(result.diff / result.nav * 1e4)
    assert result.error_ratio is not None and result.error_ratio < 0.02


def test_stress_long_duration_convexity_error(calendar: _WeekdayCalendar) -> None:
    cfg = get_config()
    curve = flat_curve(AS_OF)
    bond = aligned_bond("S30", AS_OF, 30.0, coupon_pct=3.5)
    results = stress_test(
        {bond.code: 1_000_000.0},
        [bond],
        curve,
        scenarios=[
            {"name": "平行上移25bp", "type": "parallel", "bp": 25.0},
            {"name": "平行上移100bp", "type": "parallel", "bp": 100.0},
        ],
        calendar=calendar,
        cfg=cfg,
    )
    cheap, large = results
    assert cheap.error_ratio is not None and large.error_ratio is not None
    assert large.error_ratio > 0.05
    assert large.error_ratio > cheap.error_ratio * 2.0


def test_stress_historical_reproducible_and_no_future(calendar: _WeekdayCalendar) -> None:
    dates = business_dates(date(2021, 1, 4), 80)
    as_of = dates[60]
    panel = varying_panel(dates)
    curve = _curve_at(panel, as_of)
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
    assert first == second
    assert first[0].executed is True
    assert "历史即期曲线变动重演" in first[0].note
    assert first[0].error_ratio is not None

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
            scenarios=[{"name": "x", "type": "steepen", "bp": -25.0}],
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
    market = _load_real_market()
    if market is None:
        pytest.skip("缺少中债实测面板")
    panel, _real_calendar = market
    cfg = get_config()
    as_of = date(2020, 12, 31)
    curve = _curve_at(panel, as_of)
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
    assert by_name["2022理财赎回潮"].executed is False
    assert "2013-" in by_name["2013钱荒"].note
    for result in first:
        if result.executed:
            assert np.isfinite(result.pnl_full) and np.isfinite(result.pnl_krd)
            assert result.nav > 0.0


# ---------------------------------------------------------------------------
# 6. R42：多日 VaR 先聚合冲击、carry 只计一次
# ---------------------------------------------------------------------------
@pytest.fixture(scope="module")
def real_default_setup():
    """默认 2020-12-31 组合/面值（发行 2019-12-30，真实 CN_IB 日历）。"""
    market = _load_real_market()
    if market is None:
        pytest.skip("缺少中债实测面板，跳过 R42 审查反例复现")
    panel, calendar = market
    cfg = get_config()
    as_of = date(2020, 12, 31)
    trading_days = [day for day in calendar.dates() if day <= as_of]
    issue_day = trading_days[-251]
    bonds = build_universe_bonds(_curve_at(panel, issue_day), issue_day, cfg=cfg)
    benchmark = build_benchmark(
        _curve_at(panel, as_of), as_of, bonds=bonds, calendar=calendar, cfg=cfg
    )
    faces = {code: float(face) for code, face in benchmark.faces.items()}
    return panel, calendar, bonds, faces, benchmark, trading_days


def _flat_panel_like(panel: pd.DataFrame, level: float = 3.0) -> pd.DataFrame:
    return pd.DataFrame(level, index=panel.index, columns=list(TENORS))


def test_r42_carry_once_flat_curve_h5(real_default_setup) -> None:
    """平 3% 曲线、默认组合、lookback=20、h=5：carry 每样本只加一次。"""
    panel, calendar, bonds, faces, benchmark, _tdays = real_default_setup
    cfg = get_config()
    as_of = date(2020, 12, 31)
    flat3 = _flat_panel_like(panel)
    no_carry = portfolio_pnl_current_portfolio_factors(
        faces,
        bonds,
        flat3,
        as_of,
        lookback=20,
        horizon_days=5,
        include_carry=False,
        cfg=cfg,
        calendar=calendar,
    )
    with_carry = portfolio_pnl_current_portfolio_factors(
        faces,
        bonds,
        flat3,
        as_of,
        lookback=20,
        horizon_days=5,
        include_carry=True,
        cfg=cfg,
        calendar=calendar,
    )
    assert len(no_carry) == 20 - 5 + 1  # n_obs = lookback − h + 1
    assert np.allclose(no_carry.to_numpy(), 0.0)  # 平曲线、累计 Δz=0
    increment = (with_carry - no_carry).to_numpy()
    assert np.allclose(increment, increment[0])  # 同一常数，逐样本只加一次
    carry_once = float(increment[0])
    assert carry_once == pytest.approx(58_967.63878, rel=1e-9)
    assert carry_once != pytest.approx(294_838.19390, rel=1e-6)  # 不是旧的 5×
    # VaR/ES 相对 price-only 只平移一次 carry（常数正 PnL → 负损失值）
    nav = benchmark.market_value
    var_no, es_no = _var_es_cny(no_carry, nav)
    var_with, es_with = _var_es_cny(with_carry, nav)
    assert var_no == pytest.approx(0.0, abs=1e-9)
    assert es_no == pytest.approx(0.0, abs=1e-9)
    assert var_with == pytest.approx(-carry_once, rel=1e-9)
    assert es_with == pytest.approx(-carry_once, rel=1e-9)
    # 旧 bug 的聚合值（5 个重叠样本各加一次 h 日 carry）是 5×carry_once
    assert 5.0 * carry_once == pytest.approx(294_838.19390, rel=1e-9)


def test_r42_two_day_net_shock_zero(real_default_setup) -> None:
    """3%→4%→3% 两日净冲击为零：先聚合再重估的 h=2 PnL 应为 0。"""
    panel, calendar, bonds, faces, _benchmark, trading_days = real_default_setup
    cfg = get_config()
    as_of = date(2020, 12, 31)
    path_panel = _flat_panel_like(panel)
    path_panel.loc[pd.Timestamp(trading_days[-2])] = 4.0  # 12-30 置 4%，12-31 回 3%
    one_day = portfolio_pnl_current_portfolio_factors(
        faces,
        bonds,
        path_panel,
        as_of,
        lookback=20,
        horizon_days=1,
        include_carry=False,
        cfg=cfg,
        calendar=calendar,
    )
    last_two = one_day.iloc[-2:].to_numpy()
    assert last_two[0] == pytest.approx(-7_680_370.65631, rel=1e-9)
    assert last_two[1] == pytest.approx(9_144_227.62110, rel=1e-9)
    # 旧错误做法：把两个一日重估 PnL 相加（先各自施加在同一基准价上）
    assert float(last_two.sum()) == pytest.approx(1_463_856.96480, rel=1e-9)

    two_day = portfolio_pnl_current_portfolio_factors(
        faces,
        bonds,
        path_panel,
        as_of,
        lookback=20,
        horizon_days=2,
        include_carry=False,
        cfg=cfg,
        calendar=calendar,
    )
    assert len(two_day) == 20 - 2 + 1
    # 末日窗口 12-29(3%) → 12-31(3%)：累计 Δz=0，一次重估 PnL=0
    assert two_day.iloc[-1] == pytest.approx(0.0, abs=1e-6)
    assert not np.allclose(two_day.to_numpy(), 0.0)  # 其它窗口仍有冲击


def test_r42_h2_cumulative_equals_direct_revaluation(calendar: _WeekdayCalendar) -> None:
    """h=2 累计冲击重估 == 对累计 Δz 直接做一次逐券重估。"""
    dates = business_dates(date(2021, 1, 4), 30)
    panel = varying_panel(dates)
    as_of = dates[-1]
    cfg = get_config()
    bond = make_bond("H2", date(2018, 6, 30), date(2031, 6, 30), coupon_pct=3.0, freq=2)
    faces = {bond.code: 10_000_000.0}
    two_day = portfolio_pnl_current_portfolio_factors(
        faces,
        [bond],
        panel,
        as_of,
        lookback=10,
        horizon_days=2,
        include_carry=False,
        cfg=cfg,
        calendar=calendar,
    )
    assert len(two_day) == 10 - 2 + 1
    keys = np.asarray(sorted(float(t) for t in cfg.risk.key_tenors_krd), dtype=float)
    levels = dates[-(10 + 1) :]
    start_day, end_day = levels[-3], levels[-1]

    def spot_for(day: date):
        settlement = settlement_for(calendar, day, cfg=cfg)
        return bootstrap_spot(_curve_at(panel, day), settlement=settlement, calendar=calendar)

    spot_start = spot_for(start_day)
    spot_end = spot_for(end_day)
    shifts = np.asarray(
        [
            (float(spot_end.zero_rate(tenor)) - float(spot_start.zero_rate(tenor))) * 100.0
            for tenor in keys
        ],
        dtype=float,
    )
    settlement = settlement_for(calendar, as_of, cfg=cfg)
    base_spot = bootstrap_spot(_curve_at(panel, as_of), settlement=settlement, calendar=calendar)
    shocked = spot_shock(base_spot, keys, shifts)
    direct = (
        10_000_000.0
        / 100.0
        * (
            price_from_spot(bond, settlement, shocked, cfg=cfg)
            - price_from_spot(bond, settlement, base_spot, cfg=cfg)
        )
    )
    assert float(two_day.iloc[-1]) == pytest.approx(direct, rel=1e-12)


def test_r42_horizon_fields_and_pre_aggregated(calendar: _WeekdayCalendar) -> None:
    """h>1：样本数=lookback−h+1；参数法/CF 直接用 h 日分布，不乘 sqrt(h)。"""
    dates = business_dates(date(2021, 1, 4), 40)
    panel = varying_panel(dates)
    as_of = dates[-1]
    cfg = get_config()
    bond = make_bond("AGG", date(2018, 6, 30), date(2031, 6, 30), coupon_pct=3.0, freq=2)
    faces = {bond.code: 10_000_000.0}
    one_day = portfolio_pnl_current_portfolio_factors(
        faces, [bond], panel, as_of, lookback=20, horizon_days=1, cfg=cfg, calendar=calendar
    )
    three_day = portfolio_pnl_current_portfolio_factors(
        faces, [bond], panel, as_of, lookback=20, horizon_days=3, cfg=cfg, calendar=calendar
    )
    assert len(one_day) == 20 and len(three_day) == 20 - 3 + 1
    nav = portfolio_market_value(faces, [bond], _curve_at(panel, as_of), calendar=calendar, cfg=cfg)
    pct = pnl_pct_from_cny(three_day, nav)
    hist = historical_var_es(pct, confidences=(0.95,), horizon_days=3, pre_aggregated=True)[0]
    normal = parametric_var_es(
        pct, confidences=(0.95,), horizon_days=3, dist="normal", pre_aggregated=True
    )[0]
    cf = parametric_var_es(
        pct, confidences=(0.95,), horizon_days=3, dist="cornish_fisher", pre_aggregated=True
    )[0]
    for result in (hist, normal, cf):
        assert result.horizon_days == 3
        assert result.n_obs == 20 - 3 + 1
    # 正态参数法直接由 h 日累计样本矩给出（不乘 sqrt(3)、不二次聚合）
    mean = float(pct.mean())
    std = float(pct.std(ddof=0))
    z = float(stats.norm.ppf(0.05))
    assert normal.var_pct == pytest.approx(-(mean + std * z), rel=1e-12)
    # 与“1 日分布 × sqrt(3)”明显不同
    one_day_pct = pnl_pct_from_cny(one_day, nav)
    one_day_var = parametric_var_es(one_day_pct, confidences=(0.95,), dist="normal")[0].var_pct
    assert not np.isclose(normal.var_pct, one_day_var * np.sqrt(3.0), rtol=1e-6)


def test_r42_h1_regression_matches_pre_r42(real_default_setup) -> None:
    """默认 h=1、price-only 结果与修复前一致（324,181.45 / 445,806.66 元）。"""
    panel, calendar, bonds, faces, benchmark, _tdays = real_default_setup
    cfg = get_config()
    frozen = portfolio_pnl_current_portfolio_factors(
        faces,
        bonds,
        panel,
        date(2020, 12, 31),
        lookback=250,
        horizon_days=1,
        include_carry=False,
        cfg=cfg,
        calendar=calendar,
    )
    var, es = _var_es_cny(frozen, benchmark.market_value)
    assert var == pytest.approx(324_181.45365517546, rel=1e-9)
    assert es == pytest.approx(445_806.6647392585, rel=1e-9)
