"""ML 接口层测试：as-of 无未来、特征变换不可越界、标签可用时点、walk-forward 切分。

原则
----
- 全部使用**确定性合成数据**（固定日期/固定数值），不联网、不依赖真实 parquet；
- 断言的是**接口纪律**（不泄漏、不重不漏、拒绝违规切分），不是策略绩效；
- 2019–2025 已被研究，本文件不把任何窗口称为 untouched holdout。
"""

from __future__ import annotations

import itertools
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

import ficc.ml as ml
from ficc.ml import (
    AsOfView,
    FeatureSpec,
    ForwardLabelSpec,
    LabelRecord,
    LeakageError,
    Split,
    WalkForwardSpec,
    assert_per_row_causality,
    build_asof_view,
    build_current_features,
    build_features,
    build_training_matrix,
    forward_returns,
    generate_splits,
    validate_no_leakage,
)
from ficc.utils.calendar import TradingCalendar

N_DAYS = 260
TENORS = (2.0, 5.0, 10.0)


@pytest.fixture(scope="module")
def market() -> tuple[pd.DataFrame, pd.Series, TradingCalendar]:
    """确定性曲线：每个期限每日 +1bp，便于检查标签符号与可用时点。"""
    index = pd.bdate_range("2023-01-02", periods=N_DAYS, name="date")
    step = np.arange(N_DAYS, dtype=float) * 0.01  # 每日 +1bp
    panel = pd.DataFrame(
        {tenor: 2.0 + 0.3 * (tenor - 2.0) + step for tenor in TENORS},
        index=index,
    )
    funding = pd.Series(1.8 + step, index=index, name="FDR007")
    calendar = TradingCalendar.from_dates(index)
    return panel, funding, calendar


def test_asof_view_only_exposes_history(market) -> None:
    panel, funding, calendar = market
    as_of = calendar.dates()[20]
    view = build_asof_view(panel, funding, calendar, as_of, availability_lag_days=1)

    expected_cutoff = calendar.prev_trading_day(as_of, 1)
    assert view.as_of == as_of
    assert view.available_through == expected_cutoff
    assert view.available_through < view.as_of
    # 硬边界：panel/funding 的最大索引 <= available_through
    assert view.panel.index.max() <= pd.Timestamp(view.available_through)
    assert view.funding is not None
    assert view.funding.index.max() <= pd.Timestamp(view.available_through)
    assert view.metadata["availability_lag_days"] == 1
    assert view.metadata["requested_as_of"] == as_of.isoformat()
    assert view.lag_days == 1

    history = view.history(3)
    assert len(history) == 3
    assert history.index.max() == pd.Timestamp(expected_cutoff)
    assert history.index.equals(panel.loc[: pd.Timestamp(expected_cutoff)].index[-3:])
    assert view.history(len(panel)).index.max() == pd.Timestamp(expected_cutoff)

    # lag=0 才能看到 as_of 当日；lag=2 退两个交易日
    view0 = build_asof_view(panel, funding, calendar, as_of, availability_lag_days=0)
    assert view0.available_through == as_of
    assert view0.panel.index.max() == pd.Timestamp(as_of)
    view2 = build_asof_view(panel, funding, calendar, as_of, availability_lag_days=2)
    assert view2.available_through == calendar.prev_trading_day(as_of, 2)

    with pytest.raises(ValueError):
        view.history(0)
    with pytest.raises(ValueError):
        build_asof_view(panel, funding, calendar, as_of, availability_lag_days=-1)


def test_asof_view_rejects_future_rows() -> None:
    index = pd.bdate_range("2024-01-01", periods=5, name="date")
    panel = pd.DataFrame({2.0: [1.0, 1.1, 1.2, 1.3, 1.4]}, index=index)
    calendar = TradingCalendar.from_dates(index)
    with pytest.raises(AssertionError):
        AsOfView(
            as_of=index[2].date(),
            panel=panel,
            calendar=calendar,
            available_through=index[2].date(),  # panel 里有 index[3]/index[4]
        )
    with pytest.raises(AssertionError):
        AsOfView(
            as_of=index[1].date(),
            panel=panel.loc[: index[1]],
            calendar=calendar,
            available_through=index[2].date(),  # 可用日不得晚于决策日
        )


def test_feature_transform_cannot_read_future(market) -> None:
    panel, funding, calendar = market
    as_of = calendar.dates()[30]
    view = build_asof_view(panel, funding, calendar, as_of, availability_lag_days=1)

    # 正常特征：transform 只看到历史切片，列名与 inputs 一致
    seen: dict[str, pd.Timestamp] = {}

    def spread(frame: pd.DataFrame) -> pd.Series:
        seen["max"] = frame.index.max()
        return frame["10.0"] - frame["2.0"]

    features = build_features(
        view,
        [
            FeatureSpec(name="spread_2s10s", transform=spread, inputs=("2.0", "10.0")),
            FeatureSpec(name="level_5y", transform=lambda f: f["5.0"], inputs=("5.0",)),
        ],
    )
    assert list(features.columns) == ["spread_2s10s", "level_5y"]
    assert features.index.max() <= pd.Timestamp(view.available_through)
    assert seen["max"] <= pd.Timestamp(view.available_through)
    assert np.allclose(features["spread_2s10s"].tail(1), 0.3 * 8.0)

    # 恶意变换 1：直接返回未来日期 → 断言失败
    with pytest.raises(AssertionError):
        build_features(
            view,
            [
                FeatureSpec(
                    name="leak",
                    transform=lambda _f: pd.Series([1.0], index=[pd.Timestamp(as_of)]),
                    inputs=("2.0",),
                )
            ],
        )

    # 恶意变换 2：闭包读取全样本（含未来行）→ 断言失败
    with pytest.raises(AssertionError):
        build_features(
            view,
            [
                FeatureSpec(
                    name="leak_all",
                    transform=lambda _f: panel[2.0].copy(),
                    inputs=("2.0",),
                )
            ],
        )

    # 输出索引不在历史索引内也拒绝
    with pytest.raises(AssertionError):
        build_features(
            view,
            [
                FeatureSpec(
                    name="bad_index",
                    transform=lambda f: f.set_axis(
                        pd.Index([date(1990, 1, 2)] * len(f), name="date")
                    ),
                    inputs=("2.0",),
                )
            ],
        )

    with pytest.raises(KeyError):
        build_features(view, [FeatureSpec(name="missing", transform=lambda f: f, inputs=("99.0",))])
    with pytest.raises(ValueError):
        FeatureSpec(name="dup", transform=lambda f: f, inputs=("2.0", "2.0"))
    with pytest.raises(TypeError):
        FeatureSpec(name="bad", transform=lambda f: f, inputs="2.0")  # type: ignore[arg-type]


def test_forward_label_available_after(market) -> None:
    panel, _funding, calendar = market
    as_of = calendar.dates()[40]

    spec = ForwardLabelSpec(holding_days=5, entry="next_open", return_type="clean_total")
    record = forward_returns(panel, as_of, spec, calendar=calendar)
    expected = calendar.next_trading_day(as_of, 5)
    assert record.as_of == as_of
    assert record.available_after == expected
    assert expected > as_of  # as_of 时不可计算
    assert record.is_available_at(expected)
    assert not record.is_available_at(calendar.prev_trading_day(expected, 1))
    assert record.value < 0.0  # 收益率每日 +1bp → 净价收益为负
    assert isinstance(record.as_dict(), dict)

    carry = forward_returns(
        panel,
        as_of,
        ForwardLabelSpec(holding_days=5, entry="next_open", return_type="carry_total"),
        calendar=calendar,
    )
    assert carry.available_after == record.available_after
    assert carry.value > record.value  # carry_total 比 clean_total 多持有期 carry

    close_spec = ForwardLabelSpec(holding_days=5, entry="next_close")
    close_record = forward_returns(panel, as_of, close_spec, calendar=calendar)
    assert close_record.available_after == calendar.next_trading_day(as_of, 6)
    assert close_record.available_after > record.available_after

    one_day = forward_returns(panel, as_of, ForwardLabelSpec(holding_days=1), calendar=calendar)
    assert one_day.available_after == calendar.next_trading_day(as_of, 1)

    # 期限列名支持 "2y"/"10y" 这类标签（与项目 LABEL_TO_TENOR 一致）
    labelled = panel.rename(columns={2.0: "2y", 5.0: "5y", 10.0: "10y"})
    labelled_record = forward_returns(labelled, as_of, spec, calendar=calendar)
    assert labelled_record.available_after == record.available_after
    assert labelled_record.value == pytest.approx(record.value)

    # 标签不得伪装成"当期可计算"
    with pytest.raises(ValueError):
        LabelRecord(as_of=as_of, available_after=as_of, value=0.0)
    with pytest.raises(ValueError):
        LabelRecord(as_of=as_of, available_after=calendar.next_trading_day(as_of, 5), value=np.nan)

    # 预注册 horizon 之外直接拒绝（不允许按结果挑持有期）
    with pytest.raises(ValueError):
        forward_returns(panel, as_of, spec, calendar=calendar, horizon_bounds=(1, 3))

    # 持有期超出 panel 覆盖 → 不可计算
    with pytest.raises(ValueError):
        forward_returns(
            panel, calendar.dates()[-2], ForwardLabelSpec(holding_days=30), calendar=calendar
        )


def _spec_for_first_window(days: tuple[date, ...], **overrides) -> WalkForwardSpec:
    params = {
        "train_start": days[0],
        "train_end": days[59],
        "valid_start": days[60],
        "valid_end": days[79],
        "test_start": days[80],
        "test_end": days[99],
        "purge_days": 5,
        "embargo_days": 5,
        "step_days": 20,
        "expanding": False,
    }
    params.update(overrides)
    return WalkForwardSpec(**params)


def test_walkforward_splits_purge_embargo_and_no_overlap(market) -> None:
    _panel, _funding, calendar = market
    days = calendar.dates()
    start, end = days[0], days[-1]
    spec = _spec_for_first_window(days)
    splits = generate_splits(start, end, calendar, spec)

    assert len(splits) >= 3
    first = splits[0]
    assert first.train_idx[0] == 0
    assert first.train_idx[-1] == 59
    # purge/embargo 是**强制插入**的交易日，实际 valid/test 起点晚于声明日期
    assert first.valid_idx[0] == 65
    assert first.valid_idx[-1] == 84
    assert first.test_idx[0] == 90
    assert first.test_idx[-1] == 109
    assert first.valid_idx[0] - first.train_idx[-1] - 1 == spec.purge_days
    assert first.test_idx[0] - first.valid_idx[-1] - 1 == spec.embargo_days
    assert len(first.train_dates) == len(first.train_idx)
    assert first.train_dates[0] == days[0]
    assert first.valid_dates[0] == days[65]
    assert first.test_dates[-1] == days[109]

    # 窗口长度保持；滚动窗按 step_days 前移
    for earlier, later in itertools.pairwise(splits):
        assert later.train_idx[0] - earlier.train_idx[0] == spec.step_days
        assert len(later.train_idx) == len(earlier.train_idx)
        assert len(later.valid_idx) == len(earlier.valid_idx)
        assert len(later.test_idx) == len(earlier.test_idx)
        assert later.valid_idx[0] - later.train_idx[-1] - 1 == spec.purge_days
        assert later.test_idx[0] - later.valid_idx[-1] - 1 == spec.embargo_days

    # 不重不漏：step == test 长度时测试窗首尾相接、覆盖同一段连续区间
    test_positions = [pos for split in splits for pos in split.test_idx]
    assert len(test_positions) == len(set(test_positions))
    assert sorted(test_positions) == list(
        range(test_positions[0], test_positions[0] + len(test_positions))
    )

    manifest = first.manifest
    assert manifest["purge_days"] == 5
    assert manifest["embargo_days"] == 5
    assert manifest["test_usage"] == "final_evaluation_only"
    assert manifest["holdout_status"] == "researched_sample"
    assert "untouched holdout" in manifest["holdout_note"]
    assert manifest["mode"] == "rolling"

    # 声明边界之外 / 顺序错误都要报错
    with pytest.raises(ValueError):
        generate_splits(start, days[90], calendar, spec)
    with pytest.raises(ValueError):
        WalkForwardSpec(
            train_start=days[10],
            train_end=days[9],
            valid_start=days[20],
            valid_end=days[30],
            test_start=days[40],
            test_end=days[50],
            purge_days=5,
            embargo_days=5,
            step_days=5,
        )
    with pytest.raises(ValueError):
        WalkForwardSpec(
            train_start=days[0],
            train_end=days[10],
            valid_start=days[20],
            valid_end=days[30],
            test_start=days[40],
            test_end=days[50],
            purge_days=5,
            embargo_days=5,
            step_days=0,
        )


def test_walkforward_expanding_mode(market) -> None:
    _panel, _funding, calendar = market
    days = calendar.dates()
    spec = _spec_for_first_window(days, expanding=True)
    splits = generate_splits(days[0], days[-1], calendar, spec)
    assert len(splits) >= 2
    first, second = splits[0], splits[1]
    assert first.train_idx[0] == 0
    assert second.train_idx[0] == 0  # 起点固定
    assert second.train_idx[-1] > first.train_idx[-1]  # 训练窗扩展
    assert second.valid_idx[0] - second.train_idx[-1] - 1 == spec.purge_days
    assert second.test_idx[0] - second.valid_idx[-1] - 1 == spec.embargo_days
    assert second.manifest["mode"] == "expanding"


def test_walkforward_validate_rejects_leakage(market) -> None:
    _panel, _funding, calendar = market
    days = calendar.dates()
    spec = _spec_for_first_window(days)
    splits = generate_splits(days[0], days[-1], calendar, spec)

    # 与 purge 一致的标签持有期：通过
    assert validate_no_leakage(splits, ForwardLabelSpec(holding_days=5)) is True
    assert validate_no_leakage(splits[0], ForwardLabelSpec(holding_days=5)) is True
    assert validate_no_leakage(splits, ForwardLabelSpec(holding_days=5, entry="next_close")) is True
    # 持有期 6 + next_open：可用位置 = train_last + 6 = valid_start 前一个交易日 → 仍安全
    assert validate_no_leakage(splits, ForwardLabelSpec(holding_days=6)) is True
    # 持有期 7：训练标签在验证窗开始后才可用 → 必须拒绝
    with pytest.raises(LeakageError, match=r"purge|标签"):
        validate_no_leakage(splits, ForwardLabelSpec(holding_days=7))

    # purge=0 的切分会把训练标签推进验证窗
    leaky = generate_splits(days[0], days[-1], calendar, _spec_for_first_window(days, purge_days=0))
    with pytest.raises(LeakageError):
        validate_no_leakage(leaky, ForwardLabelSpec(holding_days=5))

    # embargo=0：验证标签在测试窗才可用
    no_embargo = generate_splits(
        days[0], days[-1], calendar, _spec_for_first_window(days, embargo_days=0)
    )
    with pytest.raises(LeakageError, match=r"embargo|标签"):
        validate_no_leakage(no_embargo, ForwardLabelSpec(holding_days=5))

    # 手工构造：验证/测试顺序颠倒
    bad = Split(
        train_idx=splits[0].train_idx,
        valid_idx=splits[0].valid_idx,
        test_idx=splits[0].valid_idx,
        manifest={"split_id": 999, "purge_days": 5, "embargo_days": 5},
    )
    with pytest.raises(LeakageError):
        validate_no_leakage([bad], ForwardLabelSpec(holding_days=5))

    # 手工构造：两个 split 的测试窗重叠
    duplicate = Split(
        train_idx=splits[0].train_idx,
        valid_idx=splits[0].valid_idx,
        test_idx=splits[1].test_idx,
        manifest={"split_id": 1000, "purge_days": 5, "embargo_days": 5},
    )
    # 与 splits[1] 的测试窗完全重叠
    with pytest.raises(LeakageError, match="重叠"):
        validate_no_leakage([splits[1], duplicate], ForwardLabelSpec(holding_days=5))


def test_ml_package_surface_is_stable() -> None:
    expected = {
        "AsOfView",
        "ClockError",
        "EntryConvention",
        "FeatureSpec",
        "ForwardLabelSpec",
        "InformationClock",
        "LabelRecord",
        "LeakageError",
        "ReturnType",
        "Split",
        "WalkForwardSpec",
        "assert_per_row_causality",
        "attach_funding_column",
        "benchmark_nav_for_labels",
        "build_asof_view",
        "build_current_features",
        "build_features",
        "build_supervised_dataset",
        "build_training_matrix",
        "dataset_manifest",
        "forward_returns",
        "generate_splits",
        "validate_no_leakage",
    }
    assert set(ml.__all__) == expected
    for name in ml.__all__:
        assert hasattr(ml, name), f"ficc.ml.__all__ 导出了不存在的名字 {name}"

    package_dir = Path(ml.__file__).resolve().parent
    files = {path.name for path in package_dir.glob("*.py")}
    assert files == {
        "__init__.py",
        "asof.py",
        "dataset.py",
        "labels.py",
        "market_features.py",
        "walkforward.py",
    }


# ===========================================================================
# R43：逐决策时点训练矩阵 + 逐样本因果性（禁止整段历史一次算完再截断）
# ===========================================================================
def _level_spread_specs() -> list[FeatureSpec]:
    return [
        FeatureSpec(name="level_2y", transform=lambda f: f["2.0"], inputs=("2.0",)),
        FeatureSpec(
            name="spread_2s10s",
            transform=lambda f: f["10.0"] - f["2.0"],
            inputs=("2.0", "10.0"),
        ),
        FeatureSpec(
            name="mean5_10y",
            transform=lambda f: f["10.0"].rolling(5).mean(),
            inputs=("10.0",),
        ),
    ]


def _decision_dates(calendar: TradingCalendar, start: int = 40, stop: int = 90, step: int = 5):
    return calendar.dates()[start:stop:step]


def test_r43_build_features_is_current_row_only(market) -> None:
    """旧多行 API 必须收窄为"当前决策单行"，否则会跨行泄漏。"""
    panel, funding, calendar = market
    as_of = calendar.dates()[40]
    view = build_asof_view(panel, funding, calendar, as_of, availability_lag_days=1)
    specs = _level_spread_specs()

    with pytest.warns(FutureWarning, match="当前决策"):
        legacy = build_features(view, specs)
    current = build_current_features(view, specs)

    assert len(legacy) == 1
    assert len(current) == 1
    assert legacy.index.equals(current.index)
    available = pd.Timestamp(view.available_through)
    assert current.index[0] == available
    assert current.index.max() <= pd.Timestamp(view.available_through)
    assert list(current.columns) == ["level_2y", "spread_2s10s", "mean5_10y"]
    assert current["level_2y"].iloc[0] == pytest.approx(panel.loc[available, 2.0])
    # 旧实现会在历史每一行都返回特征；现在只剩当前可得日这一行
    assert not current.index.duplicated().any()


def test_r43_training_matrix_rows_are_decision_dates_and_use_per_t_view(
    market, monkeypatch
) -> None:
    import ficc.ml.asof as asof_module

    panel, funding, calendar = market
    decisions = sorted(_decision_dates(calendar, 40, 75, 5))
    calls: list[object] = []
    original = asof_module.build_asof_view

    def counting_build(*args, **kwargs):
        calls.append(kwargs.get("as_of", args[3] if len(args) > 3 else None))
        return original(*args, **kwargs)

    monkeypatch.setattr(asof_module, "build_asof_view", counting_build)
    matrix = build_training_matrix(
        panel, funding, calendar, decisions, _level_spread_specs(), availability_lag_days=1
    )

    # 每个决策时点单独建视图（次数 == 决策日数），而不是一次性历史切片
    assert len(calls) == len(decisions)
    expected_index = pd.DatetimeIndex([pd.Timestamp(d) for d in decisions], name="date")
    assert matrix.index.equals(expected_index)
    assert matrix.attrs["builder"] == "per_decision_asof"
    assert matrix.attrs["availability_lag_days"] == 1
    for decision in decisions:
        expected_cutoff = calendar.prev_trading_day(decision, 1)
        assert (
            matrix.attrs["available_through"][decision.isoformat()] == expected_cutoff.isoformat()
        )


def test_r43_future_perturbation_does_not_change_t_row(market) -> None:
    panel, funding, calendar = market
    decisions = _decision_dates(calendar)
    specs = _level_spread_specs()
    matrix = build_training_matrix(panel, funding, calendar, decisions, specs)

    target = decisions[2]
    cutoff = calendar.prev_trading_day(target, 1)
    perturbed_panel = panel.copy()
    future = perturbed_panel.index > pd.Timestamp(cutoff)
    assert future.any()
    perturbed_panel.loc[future] = perturbed_panel.loc[future] * 3.0 + 7.0
    perturbed_funding = funding.copy()
    future_funding = perturbed_funding.index > pd.Timestamp(cutoff)
    perturbed_funding.loc[future_funding] = perturbed_funding.loc[future_funding] * 3.0 + 7.0

    rebuilt = build_training_matrix(perturbed_panel, perturbed_funding, calendar, decisions, specs)
    pd.testing.assert_frame_equal(
        rebuilt.loc[[pd.Timestamp(target)]],
        matrix.loc[[pd.Timestamp(target)]],
        rtol=0.0,
        atol=1e-12,
    )
    # 之后决策日可以看到被修改的数据 → 行变化，证明扰动确实生效
    later = decisions[-1]
    assert not np.allclose(
        rebuilt.loc[pd.Timestamp(later)].to_numpy(dtype=float),
        matrix.loc[pd.Timestamp(later)].to_numpy(dtype=float),
        rtol=0.0,
        atol=1e-12,
        equal_nan=True,
    )


def test_r43_past_data_change_is_visible(market) -> None:
    panel, funding, calendar = market
    target = _decision_dates(calendar)[2]
    specs = _level_spread_specs()
    matrix = build_training_matrix(panel, funding, calendar, [target], specs)
    cutoff = calendar.prev_trading_day(target, 1)

    changed = panel.copy()
    changed.loc[pd.Timestamp(cutoff), 2.0] = changed.loc[pd.Timestamp(cutoff), 2.0] + 1.0
    rebuilt = build_training_matrix(changed, funding, calendar, [target], specs)
    assert not np.allclose(
        rebuilt.loc[pd.Timestamp(target)].to_numpy(dtype=float),
        matrix.loc[pd.Timestamp(target)].to_numpy(dtype=float),
        rtol=0.0,
        atol=1e-12,
        equal_nan=True,
    )


def test_r43_lag_boundary(market) -> None:
    panel, funding, calendar = market
    target = _decision_dates(calendar)[2]
    specs = _level_spread_specs()
    lag1 = build_training_matrix(panel, funding, calendar, [target], specs)
    cutoff = calendar.prev_trading_day(target, 1)

    # 修改 cutoff 当日（可见）→ 行变化
    visible = panel.copy()
    visible.loc[pd.Timestamp(cutoff), 2.0] += 1.0
    assert not np.allclose(
        build_training_matrix(visible, funding, calendar, [target], specs)
        .loc[pd.Timestamp(target)]
        .to_numpy(dtype=float),
        lag1.loc[pd.Timestamp(target)].to_numpy(dtype=float),
        rtol=0.0,
        atol=1e-12,
        equal_nan=True,
    )

    # 修改 target 当日（lag=1 下不可见）→ 行不变
    invisible = panel.copy()
    invisible.loc[pd.Timestamp(target), 2.0] += 999.0
    pd.testing.assert_frame_equal(
        build_training_matrix(invisible, funding, calendar, [target], specs).loc[
            [pd.Timestamp(target)]
        ],
        lag1.loc[[pd.Timestamp(target)]],
        rtol=0.0,
        atol=1e-12,
    )

    # lag=0 时 target 当日可见 → 行变化
    lag0 = build_training_matrix(panel, funding, calendar, [target], specs, availability_lag_days=0)
    changed0 = panel.copy()
    changed0.loc[pd.Timestamp(target), 2.0] += 1.0
    rebuilt0 = build_training_matrix(
        changed0, funding, calendar, [target], specs, availability_lag_days=0
    )
    assert not np.allclose(
        rebuilt0.loc[pd.Timestamp(target)].to_numpy(dtype=float),
        lag0.loc[pd.Timestamp(target)].to_numpy(dtype=float),
        rtol=0.0,
        atol=1e-12,
        equal_nan=True,
    )


def test_r43_shift_minus_one_is_contained(market) -> None:
    """当前决策 API 中 shift(-1) 只能得到 NaN，不会伪造未来值。"""
    panel, funding, calendar = market
    decisions = _decision_dates(calendar, 40, 80, 5)
    shift_spec = FeatureSpec(
        name="next_2y", transform=lambda f: f["2.0"].shift(-1), inputs=("2.0",)
    )
    matrix = build_training_matrix(panel, funding, calendar, decisions, [shift_spec])
    assert matrix["next_2y"].isna().all(), "shift(-1) 在当前决策行不应取到未来值"
    # 修改 t+1 的未来数据：t 行必须逐位不变（这里全 NaN 也一致）
    assert (
        assert_per_row_causality(
            matrix, panel, calendar, [shift_spec], funding=funding, sample_rows=3
        )
        is True
    )


def test_r43_historical_table_built_once_is_detected(market) -> None:
    """历史表"一次性算完再截断"必须被 assert_per_row_causality 识破。"""
    panel, funding, calendar = market
    decisions = _decision_dates(calendar, 40, 80, 5)
    shift_spec = FeatureSpec(
        name="next_2y", transform=lambda f: f["2.0"].shift(-1), inputs=("2.0",)
    )
    # 旧实现：对整段历史做一次 shift(-1)，再按决策日截取 → 行 t 用到了 t 当日值
    leaky_series = panel[2.0].shift(-1)
    legacy_rows = []
    for decision in decisions:
        cutoff = calendar.prev_trading_day(decision, 1)
        legacy_rows.append(leaky_series.loc[pd.Timestamp(cutoff)])
    legacy_matrix = pd.DataFrame(
        {"next_2y": legacy_rows},
        index=pd.DatetimeIndex([pd.Timestamp(d) for d in decisions], name="date"),
    )
    legacy_matrix.attrs["availability_lag_days"] = 1

    # 逐决策重建得到 NaN；与旧表不一致 → 明确报错，而不是静默训练
    with pytest.raises(AssertionError, match="逐决策时点重建"):
        assert_per_row_causality(legacy_matrix, panel, calendar, [shift_spec], funding=funding)


def test_r43_assert_per_row_causality_passes_normal_features(market) -> None:
    panel, funding, calendar = market
    decisions = _decision_dates(calendar)
    specs = _level_spread_specs()
    matrix = build_training_matrix(panel, funding, calendar, decisions, specs)
    assert (
        assert_per_row_causality(
            matrix, panel, calendar, specs, funding=funding, availability_lag_days=1
        )
        is True
    )
    # 显式声明 lag 与矩阵不一致 → 拒绝
    with pytest.raises(AssertionError, match="availability_lag_days"):
        assert_per_row_causality(
            matrix, panel, calendar, specs, funding=funding, availability_lag_days=2
        )


def test_r43_training_matrix_input_validation(market) -> None:
    panel, funding, calendar = market
    decisions = _decision_dates(calendar)
    specs = _level_spread_specs()
    with pytest.raises(ValueError, match="不能为空"):
        build_training_matrix(panel, funding, calendar, [], specs)
    with pytest.raises(ValueError, match="重复"):
        build_training_matrix(panel, funding, calendar, [decisions[0], decisions[0]], specs)
    with pytest.raises(TypeError, match="日期序列"):
        build_training_matrix(panel, funding, calendar, decisions[0], specs)  # type: ignore[arg-type]


def test_r43_review_counterexample_2019_02_11_shift_next() -> None:
    """复现审查反例（2019-02-11/12）：逐决策构造后 t 行不受 t+1 修改影响。"""
    days = pd.bdate_range("2019-02-04", "2019-02-15", name="date")
    panel = pd.DataFrame(
        {2.0: np.linspace(2.50, 2.61, len(days)), 10.0: np.linspace(3.00, 3.11, len(days))},
        index=days,
    )
    calendar = TradingCalendar.from_dates(days)
    t = date(2019, 2, 11)
    t_next = date(2019, 2, 12)
    shift_spec = FeatureSpec(
        name="next_2y", transform=lambda f: f["2.0"].shift(-1), inputs=("2.0",)
    )

    baseline = build_training_matrix(panel, None, calendar, [t], [shift_spec])
    assert baseline.loc[pd.Timestamp(t), "next_2y"] != pytest.approx(12.5543)
    assert np.isnan(baseline.loc[pd.Timestamp(t), "next_2y"])

    # 旧式"整段历史一次 shift(-1) 再截断"会把 t+1 的值带进 t 行（审查现象）
    legacy_value = panel[2.0].shift(-1).loc[pd.Timestamp(t)]
    assert legacy_value == pytest.approx(panel.loc[pd.Timestamp(t_next), 2.0])

    corrupted = panel.copy()
    corrupted.loc[pd.Timestamp(t_next), 2.0] = 12.5543
    rebuilt = build_training_matrix(corrupted, None, calendar, [t], [shift_spec])
    pd.testing.assert_frame_equal(
        rebuilt.loc[[pd.Timestamp(t)]],
        baseline.loc[[pd.Timestamp(t)]],
        rtol=0.0,
        atol=1e-12,
    )
    # 修改的 t+1 数据没有进入 t 行（否则会看到 12.5543）
    assert rebuilt.loc[pd.Timestamp(t), "next_2y"] != pytest.approx(12.5543)
    assert (
        assert_per_row_causality(
            baseline, panel, calendar, [shift_spec], availability_lag_days=1, sample_rows=1
        )
        is True
    )
