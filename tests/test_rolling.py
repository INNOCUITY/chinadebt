"""滚动政策 roll_v1 与滚动基准版本化的测试（§12.12-B）。

覆盖：
1. 政策触发条件（剩余期限 < 1Y）与平价发行条款；
2. 滚动宇宙只在触发桶替换、事件记录完整、代码唯一；
3. 引擎：滚动基准版本化、显式卖旧买新、滚动基准与买持基准不可混比（配置校验）；
4. 长样本短窗口冒烟：无违规、基准逐日有 4 腿、滚动交易出现在 2Y 桶。
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pytest

from ficc.backtest.engine import run_backtest
from ficc.config import get_config
from ficc.curve.curve import Curve
from ficc.data.loader import load_curve_long, load_funding_series
from ficc.data.schema import CANONICAL_TENORS, BondStatic
from ficc.portfolio.rolling import RollingUniverse, RollPolicy
from ficc.portfolio.types import TargetKRD
from ficc.utils.calendar import TradingCalendar

TENORS = np.asarray(sorted(CANONICAL_TENORS), dtype=float)


def _curve(d: date, level: float = 2.0) -> Curve:
    return Curve(as_of=d, tenors=TENORS.copy(), rates=np.full_like(TENORS, level), method="linear")


def _bond(years: float = 2.0, issue: date = date(2019, 1, 28)) -> BondStatic:
    maturity = issue.replace(year=issue.year + int(years))
    return BondStatic(
        code=f"SYN-{years:g}Y-{issue:%Y%m%d}",
        issue_date=issue,
        maturity_date=maturity,
        coupon_pct=2.0,
        freq=1,
    )


def test_roll_policy_trigger_and_par_issue() -> None:
    policy = RollPolicy(enabled=True, trigger_years=1.0)
    bond = _bond(2.0, date(2019, 1, 28))
    assert not policy.should_roll(bond, date(2020, 1, 28))  # 剩余 1.0Y 不触发（严格小于）
    assert policy.should_roll(bond, date(2020, 2, 3))  # 剩余 < 1Y

    new = policy.issue(_curve(date(2020, 2, 3), 3.0), date(2020, 2, 3), 2.0)
    assert new.issue_date == date(2020, 2, 3)
    assert new.maturity_date == date(2022, 2, 3)
    assert new.coupon_pct == pytest.approx(3.0)  # 平价发行：票面 = 当日该期限 YTM
    assert new.freq == 1
    assert new.code == "SYN-2Y-20200203"


def test_rolling_universe_replaces_only_triggered_tenor() -> None:
    cfg = get_config()
    rolling = cfg.portfolio.rolling.model_copy(update={"enabled": True})
    cfg = cfg.model_copy(
        update={"portfolio": cfg.portfolio.model_copy(update={"rolling": rolling})}
    )
    start = date(2019, 1, 28)
    universe = RollingUniverse.build(_curve(start), start, cfg=cfg)
    initial = {tenor: bond.code for tenor, bond in universe.bonds.items()}
    events = universe.roll(_curve(start, 2.5), start)
    assert events == []  # 刚发行，不滚动

    future = date(2020, 2, 3)
    events = universe.roll(_curve(future, 2.5), future)
    assert [event.tenor for event in events] == [2.0]
    assert events[0].retired.code == initial[2.0]
    assert events[0].issued.issue_date == future
    assert universe.bonds[2.0].code == events[0].issued.code
    # 其他桶不变
    for tenor in (5.0, 10.0, 30.0):
        assert universe.bonds[tenor].code == initial[tenor]
    assert len(universe.active()) == len(cfg.portfolio.universe)


def test_engine_requires_consistent_benchmark_version() -> None:
    cfg = get_config()
    rolling = cfg.portfolio.rolling.model_copy(update={"enabled": True})
    only_rolling = cfg.model_copy(
        update={"portfolio": cfg.portfolio.model_copy(update={"rolling": rolling})}
    )
    with pytest.raises(ValueError, match="benchmark_version"):
        # 配置校验发生在 run_backtest 内部，先用最小数据触发到校验点

        from ficc.data.loader import load_curve_long

        long = load_curve_long(cfg=only_rolling)
        panel = long.pivot(index="date", columns="tenor", values="ytm_pct").sort_index()
        calendar = TradingCalendar.from_dates(panel.index)
        run_backtest(
            _ZeroTilt(),
            panel=panel,
            calendar=calendar,
            funding=None,
            cfg=only_rolling,
            start=date(2019, 1, 28),
            end=date(2019, 2, 15),
        )


class _ZeroTilt:
    name = "zero"

    def target_krd(self, ctx):
        return TargetKRD(as_of=ctx.as_of, krd=dict(ctx.benchmark.krd))


@pytest.fixture(scope="module")
def market():
    cfg = get_config()
    long = load_curve_long(cfg=cfg)
    panel = (
        long.pivot(index="date", columns="tenor", values="ytm_pct")
        .sort_index()
        .reindex(columns=list(CANONICAL_TENORS))
    )
    funding = load_funding_series(cfg=cfg).set_index("date")["FDR007"].dropna().sort_index()
    calendar = TradingCalendar.from_dates(panel.index)
    return cfg, panel, funding, calendar


def _rolling_cfg(cfg):
    rolling = cfg.portfolio.rolling.model_copy(update={"enabled": True})
    return cfg.model_copy(
        update={
            "portfolio": cfg.portfolio.model_copy(update={"rolling": rolling}),
            "backtest": cfg.backtest.model_copy(update={"benchmark_version": "rolling"}),
        }
    )


def test_engine_rolling_benchmark_and_explicit_rolls(market) -> None:
    cfg, panel, funding, calendar = market
    rolling = _rolling_cfg(cfg)
    result = run_backtest(
        _ZeroTilt(),
        panel=panel,
        calendar=calendar,
        funding=funding,
        cfg=rolling,
        start=date(2019, 1, 28),
        end=date(2021, 12, 31),
    )
    assert result.benchmark_version == "rolling"
    assert result.rolling_version.startswith("roll_v1")
    roll_trades = result.benchmark_trades[
        result.benchmark_trades["trade_id"].str.startswith("bench-roll")
    ]
    assert len(roll_trades) >= 2  # 2020-02、2021-02 两次 2Y 滚动（每次卖旧+买新）
    assert {"sell", "buy"} <= set(roll_trades["side"])
    # 策略侧也应有对应卖出（旧券退出）
    sells = result.trades[result.trades["side"] == "sell"]
    assert len(sells) >= 1
    assert result.constraints["ok"].all()
    # 从建仓日起，每日基准仍有 4 条腿（滚动基准不缩腿）
    legs_per_day = result.benchmark_positions.groupby("as_of")["bond_code"].nunique()
    assert (legs_per_day.iloc[1:] == 4).all()


def test_rolling_and_buyhold_benchmarks_differ(market) -> None:
    """滚动基准与买入持有基准是不同版本：同窗口 NAV 不同且不可混比。"""
    cfg, panel, funding, calendar = market
    rolling = _rolling_cfg(cfg)
    r_roll = run_backtest(
        _ZeroTilt(),
        panel=panel,
        calendar=calendar,
        funding=funding,
        cfg=rolling,
        start=date(2019, 1, 28),
        end=date(2021, 12, 31),
    )
    r_buy = run_backtest(
        _ZeroTilt(),
        panel=panel,
        calendar=calendar,
        funding=funding,
        cfg=cfg,
        start=date(2019, 1, 28),
        end=date(2019, 12, 31),  # 买入持有基准在 2Y 券到期前有效
    )
    assert r_buy.benchmark_version == "buyhold"
    assert len(r_buy.benchmark_trades) == 4  # 买入持有：只在起点买一次
    assert not np.isclose(r_roll.benchmark_nav.iloc[-1], r_buy.benchmark_nav.iloc[-1], rtol=1e-9)


def test_roll_events_map_to_same_day_strategy_trades(market) -> None:
    """R38/§12.15：滚动事件到成交的映射必须完整，且策略同日换券。"""
    cfg, panel, funding, calendar = market
    rolling = _rolling_cfg(cfg)
    result = run_backtest(
        _ZeroTilt(),
        panel=panel,
        calendar=calendar,
        funding=funding,
        cfg=rolling,
        start=date(2019, 1, 28),
        end=date(2021, 12, 31),
    )
    events = result.roll_events
    assert len(events) >= 2
    assert set(events["status"]) <= {"same_day", "rejected_constraint"}
    rejected = events[events["status"] == "rejected_constraint"]
    if len(rejected):
        assert (rejected["reason"] != "").all()  # 拒单必须可审计
    same_day = events[events["status"] == "same_day"]
    assert (same_day["strategy_trade_ids"] != "").all()
    assert (events["bench_sell_trade_id"] != "").all()
    assert (events["bench_buy_trade_id"] != "").all()


def test_exact_benchmark_replay_control_is_zero(market) -> None:
    """精确基准面值回放控制组：同政策、同成交，超额必须为 0（分离执行/求解偏差）。"""
    from ficc.portfolio.types import TargetFaces

    class _Exact:
        name = "zero_exact"

        def target_krd(self, ctx):
            return TargetFaces(
                as_of=ctx.as_of,
                faces={leg.bond.code: leg.face_value for leg in ctx.benchmark.legs},
                notes="exact replay",
            )

    cfg, panel, funding, calendar = market
    rolling = _rolling_cfg(cfg)
    result = run_backtest(
        _Exact(),
        panel=panel,
        calendar=calendar,
        funding=funding,
        cfg=rolling,
        start=date(2019, 1, 28),
        end=date(2021, 12, 31),
    )
    excess = result.nav.iloc[-1] / result.benchmark_nav.iloc[-1] - 1.0
    assert abs(excess) < 1e-12
    assert set(result.roll_events["status"]) == {"same_day"}
    assert (result.roll_events["strategy_trade_ids"] != "").all()
