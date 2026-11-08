"""监督学习数据集契约测试（R43/§12.17）：时钟、账本口径标签、逐行因果性、walk-forward。

原则
----
- 确定性合成数据（固定日期/固定曲线），不联网、不读真实 parquet；
- 断言的是**数据契约**（标签来源、行级因果性、purge 边界、NaN 规则），不是绩效；
- 2019–2025 已被研究，本文件不把任何窗口称为 untouched holdout。
"""

from __future__ import annotations

import json
from datetime import date

import numpy as np
import pandas as pd
import pytest

from ficc.config import Config
from ficc.data.schema import CANONICAL_TENORS
from ficc.ml import (
    ClockError,
    FeatureSpec,
    InformationClock,
    LeakageError,
    assert_per_row_causality,
    attach_funding_column,
    benchmark_nav_for_labels,
    build_supervised_dataset,
    build_training_matrix,
    dataset_manifest,
    generate_splits,
    validate_no_leakage,
)
from ficc.ml.walkforward import WalkForwardSpec
from ficc.utils.calendar import TradingCalendar

N_DAYS = 160
_BASE_CURVE = {
    0.25: 1.90,
    0.5: 2.00,
    1.0: 2.10,
    2.0: 2.30,
    3.0: 2.45,
    5.0: 2.65,
    7.0: 2.80,
    10.0: 2.95,
    15.0: 3.10,
    20.0: 3.18,
    30.0: 3.25,
}
DATE_COLUMNS = (
    "decision_as_of",
    "available_through",
    "execution_date",
    "label_end_date",
    "label_available_after",
)


@pytest.fixture(scope="module")
def market() -> tuple[pd.DataFrame, pd.Series, TradingCalendar]:
    """确定性曲线 + 资金面；最后留 10 个交易日给结算日缓冲。"""
    index = pd.bdate_range("2023-01-02", periods=N_DAYS, name="date")
    step = np.arange(N_DAYS, dtype=float)
    panel = pd.DataFrame(
        {
            tenor: _BASE_CURVE[tenor] + 0.002 * step + 0.01 * np.sin(step / 7.0 + tenor)
            for tenor in CANONICAL_TENORS
        },
        index=index,
    )
    funding = pd.Series(1.80 + 0.001 * step + 0.01 * np.sin(step / 5.0), index=index, name="FDR007")
    calendar = TradingCalendar.from_dates(index)
    return panel, funding, calendar


def _specs() -> list[FeatureSpec]:
    return [
        FeatureSpec(name="level_5y", transform=lambda f: f["5.0"], inputs=("5.0",)),
        FeatureSpec(
            name="slope_2s10s",
            transform=lambda f: f["10.0"] - f["2.0"],
            inputs=("2.0", "10.0"),
        ),
        FeatureSpec(
            name="curvature_2_5_10",
            transform=lambda f: 2.0 * f["5.0"] - f["2.0"] - f["10.0"],
            inputs=("2.0", "5.0", "10.0"),
        ),
        FeatureSpec(
            name="mom_10_5y",
            transform=lambda f: f["5.0"] - f["5.0"].shift(10),
            inputs=("5.0",),
        ),
        FeatureSpec(
            name="funding_level",
            transform=lambda f: f["FDR007"],
            inputs=("FDR007",),
        ),
        FeatureSpec(
            name="funding_change_5",
            transform=lambda f: f["FDR007"] - f["FDR007"].shift(5),
            inputs=("FDR007",),
        ),
    ]


@pytest.fixture(scope="module")
def feature_panel(
    market: tuple[pd.DataFrame, pd.Series, TradingCalendar],
) -> pd.DataFrame:
    panel, funding, _calendar = market
    return attach_funding_column(panel, funding)


@pytest.fixture(scope="module")
def decisions(market: tuple[pd.DataFrame, pd.Series, TradingCalendar]) -> list[date]:
    _panel, _funding, calendar = market
    return list(calendar.dates()[30:130:5])


@pytest.fixture(scope="module")
def benchmark_nav(
    market: tuple[pd.DataFrame, pd.Series, TradingCalendar],
    decisions: list[date],
    cfg: Config,
) -> pd.Series:
    """与默认路径同起点的全区间一次引擎回放（起点=最早决策日，最后留结算缓冲）。"""
    panel, funding, calendar = market
    return benchmark_nav_for_labels(
        panel, funding, calendar, decisions[0], calendar.dates()[-6], cfg
    )


@pytest.fixture(scope="module")
def dataset(
    market: tuple[pd.DataFrame, pd.Series, TradingCalendar],
    feature_panel: pd.DataFrame,
    benchmark_nav: pd.Series,
    decisions: list[date],
    cfg: Config,
) -> pd.DataFrame:
    _panel, funding, calendar = market
    return build_supervised_dataset(
        feature_panel,
        funding,
        calendar,
        decisions,
        _specs(),
        cfg,
        InformationClock(),
        benchmark_nav=benchmark_nav,
    )


# ===========================================================================
# 1. 信息时钟
# ===========================================================================
def test_information_clock_boundaries(market) -> None:
    _panel, _funding, calendar = market
    days = calendar.dates()
    decision = days[20]
    clock = InformationClock()

    available = clock.available_through(decision, calendar)
    execution = clock.execution_date(decision, calendar)
    end = clock.label_end(decision, calendar)
    after = clock.label_available_after(decision, calendar)

    assert available == days[19]  # lag=1
    assert execution == days[21]  # next_close
    assert end == days[25]  # holding=5
    assert after == days[26]  # label_lag=1
    assert available < decision < execution <= end < after
    assert clock.execution_offset == 1
    assert clock.label_end_offset == 5
    assert clock.label_available_offset == 6

    # 自定义时钟：lag=2、holding=3、label lag=2
    custom = InformationClock(observation_lag_days=2, holding_days=3, label_lag_days=2)
    assert custom.available_through(decision, calendar) == days[18]
    assert custom.execution_date(decision, calendar) == days[21]
    assert custom.label_end(decision, calendar) == days[23]
    assert custom.label_available_after(decision, calendar) == days[25]
    assert custom.label_available_offset == 5

    # lag=0 时决策当日可见（仅教学/对照，不用于正式数据集默认值）
    lag0 = InformationClock(observation_lag_days=0)
    assert lag0.available_through(decision, calendar) == decision

    # 与现有 walk-forward 校验的 purge 偏移一致
    spec = clock.to_forward_label_spec()
    assert spec.entry == "next_close"
    assert spec.available_offset == clock.label_available_offset
    assert clock.describe()["label_available_offset_days"] == 6


def test_information_clock_rejects_bad_inputs_and_out_of_range(market) -> None:
    _panel, _funding, calendar = market
    days = calendar.dates()
    with pytest.raises(TypeError):
        InformationClock(observation_lag_days=True)  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        InformationClock(observation_lag_days=-1)
    with pytest.raises(ValueError):
        InformationClock(execution="next_open")  # type: ignore[arg-type]
    with pytest.raises(ValueError):
        InformationClock(holding_days=0)
    with pytest.raises(ValueError):
        InformationClock(label_lag_days=0)

    clock = InformationClock()
    # 日历起点之前没有可用观测
    with pytest.raises(ClockError, match="available_through"):
        clock.available_through(calendar.first_day, calendar)
    # 日历末尾之后没有执行日 / 标签结束日
    with pytest.raises(ClockError, match="执行日"):
        clock.execution_date(calendar.last_day, calendar)
    with pytest.raises(ClockError, match="label_end"):
        clock.label_end(calendar.last_day, calendar)
    with pytest.raises(TypeError):
        clock.label_end(days[10], "not-a-calendar")  # type: ignore[arg-type]


def test_dataset_requires_non_degenerate_holding(
    market, feature_panel, benchmark_nav, decisions, cfg
) -> None:
    _panel, funding, calendar = market
    with pytest.raises(ValueError, match="holding_days 必须 ≥ 2"):
        build_supervised_dataset(
            feature_panel,
            funding,
            calendar,
            decisions[:2],
            _specs(),
            cfg,
            InformationClock(holding_days=1),
            benchmark_nav=benchmark_nav,
        )


# ===========================================================================
# 2. 数据集列 / manifest
# ===========================================================================
def test_dataset_columns_contract_and_manifest(dataset, decisions, cfg) -> None:
    assert isinstance(dataset.index, pd.DatetimeIndex)
    assert dataset.index.equals(pd.DatetimeIndex([pd.Timestamp(d) for d in decisions], name="date"))
    expected_columns = [*DATE_COLUMNS, "label", *[spec.name for spec in _specs()]]
    assert list(dataset.columns) == expected_columns
    assert len(dataset) == len(decisions)
    assert dataset["label"].notna().all()
    assert np.isfinite(dataset["label"].to_numpy(dtype=float)).all()
    for name in [spec.name for spec in _specs()]:
        assert dataset[name].notna().all(), f"特征 {name} 含 NaN"

    attrs = dataset.attrs
    assert attrs["builder"] == "build_supervised_dataset"
    assert attrs["config_hash"] == cfg.hash()
    assert attrs["benchmark_version"] == cfg.backtest.benchmark_version
    assert attrs["rolling_version"] == (
        cfg.portfolio.rolling.version if cfg.portfolio.rolling.enabled else ""
    )
    assert attrs["clock"]["observation_lag_days"] == 1
    assert attrs["clock"]["holding_days"] == 5
    assert attrs["clock"]["label_lag_days"] == 1
    assert attrs["feature_names"] == [spec.name for spec in _specs()]
    assert attrs["benchmark_nav_source"] == "provided"
    assert attrs["n_rows"] == len(dataset)

    manifest = dataset_manifest(dataset)
    assert manifest["n_rows"] == len(dataset)
    assert manifest["n_features"] == len(_specs())
    assert manifest["features"] == [spec.name for spec in _specs()]
    assert manifest["start"] == dataset.index.min().date().isoformat()
    assert manifest["end"] == dataset.index.max().date().isoformat()
    assert manifest["config_hash"] == cfg.hash()
    assert manifest["label"]["execution"] == "next_close"
    assert manifest["label"]["holding_days"] == 5
    assert manifest["clock"]["label_available_offset_days"] == 6
    assert manifest["n_dropped_rows"] == 0
    assert manifest["holdout_status"] == "researched_sample"
    json.dumps(manifest)  # 必须 JSON 友好

    stripped = dataset[["label"]].copy()
    stripped.attrs = {}
    with pytest.raises(ValueError, match="attrs"):
        dataset_manifest(stripped)


def test_dataset_has_no_future_columns(dataset) -> None:
    # 每行日期顺序：可见数据 ≤ 决策日 < 执行日 ≤ 标签结束日 < 标签可用日
    assert (dataset["available_through"] <= dataset["decision_as_of"]).all()
    assert (dataset["decision_as_of"] < dataset["execution_date"]).all()
    assert (dataset["execution_date"] <= dataset["label_end_date"]).all()
    assert (dataset["label_end_date"] < dataset["label_available_after"]).all()
    # 不应出现"已经算好的未来"列（基准 NAV / 预测 / 前向曲线）
    forbidden = {"benchmark_nav", "prediction", "fitted", "future_return", "next_return"}
    assert not (forbidden & set(dataset.columns))
    # 特征列全部是当前/历史值，不含未来标签的副本
    assert "label" in dataset.columns


# ===========================================================================
# 3. 标签 = 同一引擎账本的 benchmark_nav
# ===========================================================================
def test_label_matches_engine_benchmark_nav(dataset, benchmark_nav) -> None:
    for position in (0, len(dataset) - 1):
        row = dataset.iloc[position]
        expected = (
            benchmark_nav.loc[pd.Timestamp(row["label_end_date"])]
            / benchmark_nav.loc[pd.Timestamp(row["execution_date"])]
            - 1.0
        )
        assert float(row["label"]) == pytest.approx(expected, rel=1e-9, abs=1e-12)


def test_default_engine_path_matches_direct_benchmark_recompute(
    market, feature_panel, benchmark_nav, cfg
) -> None:
    """不注入 NAV 时只用一次 run_backtest；标签与直接基准 NAV 复算一致（rel 1e-9）。"""
    _panel, funding, calendar = market
    days = calendar.dates()
    clock = InformationClock()
    window = [days[50], days[80]]
    built = build_supervised_dataset(
        feature_panel,
        funding,
        calendar,
        window,
        _specs(),
        cfg,
        clock,
    )
    assert built.attrs["benchmark_nav_source"] == "engine_single_run"
    # 合成券/滚动随起点重建：直接复算必须用**同一起点**（最早决策日）
    direct = benchmark_nav_for_labels(
        feature_panel,
        funding,
        calendar,
        window[0],
        clock.label_end(window[-1], calendar),
        cfg,
    )
    for _, row in built.iterrows():
        expected = (
            direct.loc[pd.Timestamp(row["label_end_date"])]
            / direct.loc[pd.Timestamp(row["execution_date"])]
            - 1.0
        )
        assert float(row["label"]) == pytest.approx(expected, rel=1e-9, abs=1e-12)


# ===========================================================================
# 4. 逐行因果性
# ===========================================================================
def test_future_perturbation_does_not_change_dataset_row(
    market, feature_panel, benchmark_nav, cfg
) -> None:
    _panel, funding, calendar = market
    clock = InformationClock()
    specs = _specs()
    sample = list(calendar.dates()[40:80:5])
    base = build_supervised_dataset(
        feature_panel, funding, calendar, sample, specs, cfg, clock, benchmark_nav=benchmark_nav
    )

    # 显式复用现有逐行因果性校验
    matrix = build_training_matrix(feature_panel, funding, calendar, sample, specs)
    assert (
        assert_per_row_causality(
            matrix, feature_panel, calendar, specs, funding=funding, sample_rows=3
        )
        is True
    )

    target = sample[2]
    cutoff = clock.available_through(target, calendar)
    perturbed_panel = feature_panel.copy(deep=True)
    future = perturbed_panel.index > pd.Timestamp(cutoff)
    assert future.any()
    perturbed_panel.loc[future] = perturbed_panel.loc[future] * 3.0 + 7.0
    perturbed_funding = funding.copy(deep=True)
    future_funding = perturbed_funding.index > pd.Timestamp(cutoff)
    perturbed_funding.loc[future_funding] = perturbed_funding.loc[future_funding] * 3.0 + 7.0

    rebuilt = build_supervised_dataset(
        perturbed_panel,
        perturbed_funding,
        calendar,
        [target],
        specs,
        cfg,
        clock,
        benchmark_nav=benchmark_nav,
    )
    feature_names = [spec.name for spec in specs]
    np.testing.assert_allclose(
        base.loc[[pd.Timestamp(target)], feature_names].to_numpy(dtype=float),
        rebuilt.loc[[pd.Timestamp(target)], feature_names].to_numpy(dtype=float),
        rtol=0.0,
        atol=1e-12,
    )

    # 之后的决策日能看到被修改的数据 → 证明扰动确实生效
    later = sample[-1]
    later_rebuilt = build_supervised_dataset(
        perturbed_panel,
        perturbed_funding,
        calendar,
        [later],
        specs,
        cfg,
        clock,
        benchmark_nav=benchmark_nav,
    )
    assert not np.allclose(
        base.loc[[pd.Timestamp(later)], feature_names].to_numpy(dtype=float),
        later_rebuilt.loc[[pd.Timestamp(later)], feature_names].to_numpy(dtype=float),
        rtol=0.0,
        atol=1e-12,
    )


# ===========================================================================
# 5. walk-forward：purge/embargo 由 label_available_after 决定
# ===========================================================================
def test_walkforward_purge_uses_label_available_after(market, feature_panel, cfg):
    _panel, funding, calendar = market
    clock = InformationClock()
    days = calendar.dates()
    sample_start, sample_end = days[0], days[130]
    sample_decisions = list(days[20:131:5])
    # 注入 NAV 的起点必须与默认路径一致（最早决策日）
    nav = benchmark_nav_for_labels(
        feature_panel,
        funding,
        calendar,
        sample_decisions[0],
        clock.label_end(sample_decisions[-1], calendar),
        cfg,
    )
    data = build_supervised_dataset(
        feature_panel,
        funding,
        calendar,
        sample_decisions,
        _specs(),
        cfg,
        clock,
        benchmark_nav=nav,
    )

    n_train, n_valid, n_test = 50, 20, 20
    spec = WalkForwardSpec(
        train_start=days[0],
        train_end=days[n_train - 1],
        valid_start=days[n_train],
        valid_end=days[n_train + n_valid - 1],
        test_start=days[n_train + n_valid],
        test_end=days[n_train + n_valid + n_test - 1],
        purge_days=clock.label_available_offset,
        embargo_days=clock.label_available_offset,
        step_days=n_test,
        expanding=False,
    )
    splits = generate_splits(sample_start, sample_end, calendar, spec)
    assert len(splits) >= 2
    assert validate_no_leakage(splits, clock.to_forward_label_spec()) is True

    for split in splits:
        train = data.loc[
            (data.index >= pd.Timestamp(split.train_dates[0]))
            & (data.index <= pd.Timestamp(split.train_dates[-1]))
        ]
        valid = data.loc[
            (data.index >= pd.Timestamp(split.valid_dates[0]))
            & (data.index <= pd.Timestamp(split.valid_dates[-1]))
        ]
        test = data.loc[
            (data.index >= pd.Timestamp(split.test_dates[0]))
            & (data.index <= pd.Timestamp(split.test_dates[-1]))
        ]
        assert len(train) and len(valid) and len(test)
        # 训练标签在验证窗开始前全部可用；验证标签在测试窗开始前全部可用
        assert pd.Timestamp(train["label_available_after"].max()) <= pd.Timestamp(
            split.valid_dates[0]
        )
        assert pd.Timestamp(valid["label_available_after"].max()) <= pd.Timestamp(
            split.test_dates[0]
        )

    # purge 少一个交易日 → 现有泄漏检测必须拒绝
    leaky_spec = WalkForwardSpec(
        train_start=days[0],
        train_end=days[n_train - 1],
        valid_start=days[n_train],
        valid_end=days[n_train + n_valid - 1],
        test_start=days[n_train + n_valid],
        test_end=days[n_train + n_valid + n_test - 1],
        purge_days=clock.label_available_offset - 2,
        embargo_days=clock.label_available_offset,
        step_days=n_test,
        expanding=False,
    )
    leaky = generate_splits(sample_start, sample_end, calendar, leaky_spec)
    with pytest.raises(LeakageError):
        validate_no_leakage(leaky, clock.to_forward_label_spec())


# ===========================================================================
# 6. NaN 规则与输入校验
# ===========================================================================
def test_nan_rows_are_dropped_and_disclosed(market, feature_panel, cfg) -> None:
    _panel, funding, calendar = market
    days = calendar.dates()
    # 前几个决策的 mom_10 预热不足 → 特征 NaN → 整行删除
    early = list(days[5:40:5])
    clock = InformationClock()
    nav = benchmark_nav_for_labels(
        feature_panel, funding, calendar, early[0], clock.label_end(early[-1], calendar), cfg
    )
    dropped = build_supervised_dataset(
        feature_panel, funding, calendar, early, _specs(), cfg, clock, benchmark_nav=nav
    )
    assert dropped.attrs["n_input_rows"] == len(early)
    assert dropped.attrs["n_dropped_rows"] > 0
    assert dropped.attrs["n_rows"] == len(early) - dropped.attrs["n_dropped_rows"]
    assert len(dropped.attrs["dropped_rows"]) == dropped.attrs["n_dropped_rows"]
    assert all(
        row["reason"] == "feature_warmup_or_missing_input" for row in dropped.attrs["dropped_rows"]
    )
    for spec in _specs():
        assert dropped[spec.name].notna().all()
    manifest = dataset_manifest(dropped)
    assert manifest["n_dropped_rows"] == dropped.attrs["n_dropped_rows"]
    assert len(manifest["dropped_rows"]) == dropped.attrs["n_dropped_rows"]

    # 显式允许保留 NaN 时，不删除行但披露规则
    retained = build_supervised_dataset(
        feature_panel,
        funding,
        calendar,
        early,
        _specs(),
        cfg,
        clock,
        benchmark_nav=nav,
        require_complete=False,
    )
    assert retained.attrs["n_rows"] == len(early)
    assert retained.attrs["n_dropped_rows"] == 0
    assert "保留" in retained.attrs["nan_rule"]
    assert retained[[spec.name for spec in _specs()]].isna().any().any()


def test_dataset_input_and_label_errors(market, feature_panel, benchmark_nav, decisions, cfg):
    _panel, funding, calendar = market
    clock = InformationClock()

    with pytest.raises(ValueError, match="不能为空"):
        build_supervised_dataset(
            feature_panel, funding, calendar, [], _specs(), cfg, clock, benchmark_nav=benchmark_nav
        )
    with pytest.raises(ValueError, match="重复"):
        build_supervised_dataset(
            feature_panel,
            funding,
            calendar,
            [decisions[0], decisions[0]],
            _specs(),
            cfg,
            clock,
            benchmark_nav=benchmark_nav,
        )
    with pytest.raises(ValueError, match="交易日"):
        build_supervised_dataset(
            feature_panel,
            funding,
            calendar,
            [date(2023, 1, 7)],  # 周六
            _specs(),
            cfg,
            clock,
            benchmark_nav=benchmark_nav,
        )

    # 基准 NAV 缺标签结束日 → 明确报错，不静默 NaN
    incomplete = benchmark_nav.drop(index=pd.Timestamp(clock.label_end(decisions[0], calendar)))
    with pytest.raises(ValueError, match="基准 NAV 缺少"):
        build_supervised_dataset(
            feature_panel,
            funding,
            calendar,
            decisions[:2],
            _specs(),
            cfg,
            clock,
            benchmark_nav=incomplete,
        )

    # 特征列名与保留列冲突
    clash = [FeatureSpec(name="label", transform=lambda f: f["5.0"], inputs=("5.0",))]
    with pytest.raises(ValueError, match="保留列"):
        build_supervised_dataset(
            feature_panel,
            funding,
            calendar,
            decisions[:2],
            clash,
            cfg,
            clock,
            benchmark_nav=benchmark_nav,
        )

    # 滚动政策开启但基准版本不是 rolling → 拒绝
    inconsistent = cfg.model_copy(
        update={
            "portfolio": cfg.portfolio.model_copy(
                update={"rolling": cfg.portfolio.rolling.model_copy(update={"enabled": True})}
            ),
            "backtest": cfg.backtest.model_copy(update={"benchmark_version": "buyhold"}),
        }
    )
    with pytest.raises(ValueError, match="rolling"):
        benchmark_nav_for_labels(
            feature_panel,
            funding,
            calendar,
            calendar.dates()[0],
            calendar.dates()[-6],
            inconsistent,
        )
    # 即使注入 NAV，数据集也必须拒绝"滚动政策 + 非 rolling 基准版本"的配置
    with pytest.raises(ValueError, match="rolling"):
        build_supervised_dataset(
            feature_panel,
            funding,
            calendar,
            decisions[:2],
            _specs(),
            inconsistent,
            clock,
            benchmark_nav=benchmark_nav,
        )


def test_attach_funding_column(market, feature_panel) -> None:
    panel, funding, _calendar = market
    assert "FDR007" in feature_panel.columns
    assert "FDR007" not in panel.columns
    assert feature_panel["FDR007"].notna().all()
    assert attach_funding_column(panel, None).equals(panel)
    with pytest.raises(ValueError, match="已有"):
        attach_funding_column(panel, funding, name="2.0")
    with pytest.raises(TypeError):
        attach_funding_column(panel, funding.to_numpy())  # type: ignore[arg-type]
