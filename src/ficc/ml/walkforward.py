"""Walk-forward 切分（ML 接口层）：**不重叠、不泄漏、可审计**。

研究边界（必须随结果一起报告）
------------------------------
- **2019–2025 已经被本项目研究过**，任何切分都不能把这段样本重新命名为
  "untouched holdout"；manifest 里固定写 ``holdout_status="researched_sample"``。
- 测试集**只用于最后评估**：本模块只生成索引与边界，不训练、不调参、不比较绩效。
- 模型选择/预处理必须在训练窗内完成（内层 walk-forward）；外层测试窗不参与任何选择。

切分规则
--------
1. 由 ``WalkForwardSpec`` 声明的首个窗口给出 **训练/验证/测试的交易日长度**；
2. 相邻窗口之间**强制插入** ``purge_days`` 个交易日（训练 → 验证）与
   ``embargo_days`` 个交易日（验证 → 测试），因此实际的 valid_start/test_start 可能晚于
   spec 里声明的日期——以 ``Split`` 的日期与 manifest 为准；
3. 之后每个 split 整体前移 ``step_days`` 个交易日（滚动窗口）；``expanding=True`` 时
   训练窗起点固定、终点随验证窗推进（扩展窗口）；
4. 只输出**完整**窗口；``test_end`` 超出样本区间的尾部分片直接丢弃；
5. 同一批 splits 的测试窗两两不重叠（最后评估不重复使用同一段样本）。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from ficc.ml.asof import _coerce_date
from ficc.ml.labels import ForwardLabelSpec
from ficc.utils.calendar import TradingCalendar

__all__ = [
    "LeakageError",
    "Split",
    "WalkForwardSpec",
    "generate_splits",
    "validate_no_leakage",
]

_HOLDOUT_NOTE = (
    "2019–2025 已被研究，不是 untouched holdout；测试集只用于最后评估，"
    "模型选择/预处理必须在训练窗内完成"
)


class LeakageError(ValueError):
    """切分违反 purge/embargo/不重叠纪律（信息泄漏）。"""


@dataclass(frozen=True)
class WalkForwardSpec:
    """首个 walk-forward 窗口的声明边界与推进规则。

    声明的起止日期决定**窗口长度**（交易日数）；实际切分会在窗口之间插入
    ``purge_days`` / ``embargo_days``，所以实际日期可能后移（见 :func:`generate_splits`）。

    Parameters
    ----------
    train_start, train_end, valid_start, valid_end, test_start, test_end : date
        首个窗口的**声明**边界（必须严格递增）。只有窗口长度与首个训练窗起点参与
        生成，实际的 purge/embargo 由下面两个字段强制插入。
    purge_days : int
        训练窗与验证窗之间**删除**的交易日数。必须 **≥ 标签可用偏移**
        （``ForwardLabelSpec.available_offset``），否则 :func:`validate_no_leakage` 报错。
    embargo_days : int
        验证窗与测试窗之间删除的交易日数。
    step_days : int
        每个 split 前移的交易日数（≥1）。
    expanding : bool, default False
        ``False`` = 滚动窗口（训练窗长度固定）；``True`` = 扩展窗口（训练起点固定）。
    """

    train_start: date
    train_end: date
    valid_start: date
    valid_end: date
    test_start: date
    test_end: date
    purge_days: int
    embargo_days: int
    step_days: int
    expanding: bool = False

    def __post_init__(self) -> None:
        names = ("train_start", "train_end", "valid_start", "valid_end", "test_start", "test_end")
        for name in names:
            object.__setattr__(self, name, _coerce_date(getattr(self, name), name=name))
        if not (
            self.train_start
            <= self.train_end
            < self.valid_start
            <= self.valid_end
            < self.test_start
            <= self.test_end
        ):
            raise ValueError(
                "WalkForwardSpec 的窗口必须严格递增："
                "train_start ≤ train_end < valid_start ≤ valid_end < test_start ≤ test_end；"
                f"实际 {self.train_start}..{self.test_end}"
            )
        for name in ("purge_days", "embargo_days", "step_days"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int):
                raise TypeError(f"{name} 必须是整数")
            if name == "step_days" and value < 1:
                raise ValueError(f"step_days 必须 ≥ 1，实际 {value}")
            if name != "step_days" and value < 0:
                raise ValueError(f"{name} 不能为负，实际 {value}")
        if not isinstance(self.expanding, bool):
            raise TypeError("expanding 必须是 bool")


@dataclass(frozen=True)
class Split:
    """一个 walk-forward 切分。

    ``*_idx`` 是**全局交易日位置**（``calendar.trading_days_between(start, end)`` 的
    0-based 下标），``*_dates`` 是对应日期；位置空间让 purge 校验不需要重新查日历。
    ``manifest`` 是可直接落盘/进报告的元数据（JSON 友好）。
    """

    train_idx: tuple[int, ...]
    valid_idx: tuple[int, ...]
    test_idx: tuple[int, ...]
    train_dates: tuple[date, ...] = ()
    valid_dates: tuple[date, ...] = ()
    test_dates: tuple[date, ...] = ()
    manifest: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        for name in ("train_idx", "valid_idx", "test_idx"):
            values = getattr(self, name)
            if not isinstance(values, tuple) or not values:
                raise ValueError(f"Split.{name} 不能为空")
            if any(isinstance(v, bool) or not isinstance(v, int) for v in values):
                raise TypeError(f"Split.{name} 必须是整数位置")
            if any(v < 0 for v in values):
                raise ValueError(f"Split.{name} 不能含负位置")
        for idx_name, date_name in (
            ("train_idx", "train_dates"),
            ("valid_idx", "valid_dates"),
            ("test_idx", "test_dates"),
        ):
            dates = getattr(self, date_name)
            if dates and len(dates) != len(getattr(self, idx_name)):
                raise ValueError(f"Split.{date_name} 与 {idx_name} 长度不一致")

    @property
    def split_id(self) -> int:
        return int(self.manifest.get("split_id", -1))

    def describe(self) -> dict[str, Any]:
        """单行摘要（报告/日志用）。"""
        return {
            "split_id": self.split_id,
            "train": [self.train_idx[0], self.train_idx[-1]],
            "valid": [self.valid_idx[0], self.valid_idx[-1]],
            "test": [self.test_idx[0], self.test_idx[-1]],
            "mode": self.manifest.get("mode", "rolling"),
            "purge_days": self.manifest.get("purge_days"),
            "embargo_days": self.manifest.get("embargo_days"),
        }


def _window_length(calendar: TradingCalendar, start: date, end: date, *, name: str) -> int:
    days = calendar.trading_days_between(start, end)
    if not days:
        raise ValueError(f"{name} 窗口 [{start}, {end}] 内没有交易日")
    return len(days)


def generate_splits(
    start: date,
    end: date,
    calendar: TradingCalendar,
    spec: WalkForwardSpec,
) -> list[Split]:
    """按 ``spec`` 生成不重叠、带 purge/embargo 的 walk-forward 切分。

    Parameters
    ----------
    start, end : date
        样本区间（含端点）。``spec`` 的首个训练窗必须落在区间内。
    calendar : TradingCalendar
        交易日历。
    spec : WalkForwardSpec
        窗口长度、purge/embargo、步长与滚动/扩展模式。

    Returns
    -------
    list[Split]
        按时间升序；每个 split 的 ``test_idx`` 两两不重叠。区间太短无法容纳一个完整
        窗口时返回空列表。

    Raises
    ------
    ValueError
        ``spec`` 的首个训练窗起点早于 ``start``，或首个测试窗终点晚于 ``end``。
    """
    start = _coerce_date(start, name="start")
    end = _coerce_date(end, name="end")
    if end <= start:
        raise ValueError(f"end={end} 必须晚于 start={start}")
    if not isinstance(calendar, TradingCalendar):
        raise TypeError("calendar 必须是 TradingCalendar")
    if not isinstance(spec, WalkForwardSpec):
        raise TypeError("spec 必须是 WalkForwardSpec")
    if spec.train_start < start or spec.test_end > end:
        raise ValueError(
            "spec 的首个窗口必须落在 [start, end] 内："
            f"spec=[{spec.train_start}, {spec.test_end}], 样本=[{start}, {end}]"
        )

    days = calendar.trading_days_between(start, end)
    if len(days) < 3:
        raise ValueError("样本区间至少需要 3 个交易日")
    position = {day: index for index, day in enumerate(days)}
    if spec.train_start not in position:
        raise ValueError(f"spec.train_start={spec.train_start} 不在样本交易日内")

    n_train = _window_length(calendar, spec.train_start, spec.train_end, name="train")
    n_valid = _window_length(calendar, spec.valid_start, spec.valid_end, name="valid")
    n_test = _window_length(calendar, spec.test_start, spec.test_end, name="test")
    first_anchor = position[spec.train_start]
    last_pos = len(days) - 1

    splits: list[Split] = []
    for split_id in range(10_000):  # 防御式上限，正常远达不到
        shift = split_id * spec.step_days
        if spec.expanding:
            # 训练窗起点固定，终点落在验证窗前的 purge 边界
            first_valid_start = first_anchor + n_train + spec.purge_days
            valid_start_pos = first_valid_start + shift
            train_start_pos = first_anchor
            train_end_pos = valid_start_pos - spec.purge_days - 1
        else:
            train_start_pos = first_anchor + shift
            train_end_pos = train_start_pos + n_train - 1
            valid_start_pos = train_end_pos + 1 + spec.purge_days

        valid_end_pos = valid_start_pos + n_valid - 1
        test_start_pos = valid_end_pos + 1 + spec.embargo_days
        test_end_pos = test_start_pos + n_test - 1

        if train_start_pos < 0 or test_end_pos > last_pos:
            break
        if train_end_pos < train_start_pos or valid_start_pos <= train_end_pos:
            break

        train_idx = tuple(range(train_start_pos, train_end_pos + 1))
        valid_idx = tuple(range(valid_start_pos, valid_end_pos + 1))
        test_idx = tuple(range(test_start_pos, test_end_pos + 1))
        manifest: dict[str, Any] = {
            "split_id": split_id,
            "mode": "expanding" if spec.expanding else "rolling",
            "train": [days[train_idx[0]].isoformat(), days[train_idx[-1]].isoformat()],
            "valid": [days[valid_idx[0]].isoformat(), days[valid_idx[-1]].isoformat()],
            "test": [days[test_idx[0]].isoformat(), days[test_idx[-1]].isoformat()],
            "n_train": len(train_idx),
            "n_valid": len(valid_idx),
            "n_test": len(test_idx),
            "purge_days": spec.purge_days,
            "embargo_days": spec.embargo_days,
            "step_days": spec.step_days,
            "declared_window": {
                "train": [spec.train_start.isoformat(), spec.train_end.isoformat()],
                "valid": [spec.valid_start.isoformat(), spec.valid_end.isoformat()],
                "test": [spec.test_start.isoformat(), spec.test_end.isoformat()],
            },
            "purge_note": (
                "purge 交易日数必须 ≥ 标签可用偏移（entry_offset + holding_days）；"
                "由 validate_no_leakage 强制校验"
            ),
            "holdout_status": "researched_sample",
            "holdout_note": _HOLDOUT_NOTE,
            "test_usage": "final_evaluation_only",
        }
        splits.append(
            Split(
                train_idx=train_idx,
                valid_idx=valid_idx,
                test_idx=test_idx,
                train_dates=tuple(days[i] for i in train_idx),
                valid_dates=tuple(days[i] for i in valid_idx),
                test_dates=tuple(days[i] for i in test_idx),
                manifest=manifest,
            )
        )
    return splits


def validate_no_leakage(
    splits: list[Split] | tuple[Split, ...] | Split,
    label_spec: ForwardLabelSpec,
) -> bool:
    """校验切分没有信息泄漏；违规抛 :class:`LeakageError`。

    逐条检查（每个 split）：

    1. 训练/验证/测试在位置空间严格递增且互不相交；
    2. purge：训练集最后一条标签的可用位置 ``train_idx[-1] + label_spec.available_offset``
       **不晚于** ``valid_idx[0]``（标签持有期把训练标签推到验证窗即视为泄漏）；
    3. embargo：验证集最后一条标签的可用位置不晚于 ``test_idx[0]``；
    4. 所有 split 的测试窗**两两不重叠**（最后评估不重复使用同一段样本）。

    Parameters
    ----------
    splits : Sequence[Split]
        由 :func:`generate_splits` 生成（也可手工构造位置做反例测试）。
    label_spec : ForwardLabelSpec
        标签定义；其 ``available_offset`` 决定 purge 的最小值。

    Returns
    -------
    bool
        ``True``（不泄漏时）。为便于链式调用/断言返回 True，违规抛异常。

    Raises
    ------
    LeakageError
        任一检查失败；消息含 split_id、违规位置与所需 purge/embargo。
    """
    if not isinstance(label_spec, ForwardLabelSpec):
        raise TypeError("label_spec 必须是 ForwardLabelSpec")
    split_list = (splits,) if isinstance(splits, Split) else tuple(splits)
    for split in split_list:
        if not isinstance(split, Split):
            raise TypeError(f"splits 只能包含 Split，实际 {type(split).__name__}")

    offset = label_spec.available_offset
    seen_test: dict[int, int] = {}
    for split in split_list:
        sid = split.split_id
        train = split.train_idx
        valid = split.valid_idx
        test = split.test_idx
        manifest = split.manifest
        purge_required = int(manifest.get("purge_days", 0))
        embargo_required = int(manifest.get("embargo_days", 0))

        if not (train[-1] < valid[0] and valid[-1] < test[0]):
            raise LeakageError(
                f"split {sid}: 窗口顺序错误 train_end={train[-1]}, "
                f"valid=[{valid[0]},{valid[-1]}], test_start={test[0]}"
            )
        if (
            len(set(train) & set(valid))
            or len(set(valid) & set(test))
            or len(set(train) & set(test))
        ):
            raise LeakageError(f"split {sid}: 训练/验证/测试索引互相重叠")

        gap_train_valid = valid[0] - train[-1] - 1
        if gap_train_valid < purge_required:
            raise LeakageError(
                f"split {sid}: purge 不足（训练→验证间隔 {gap_train_valid} 个交易日，"
                f"要求 ≥ {purge_required}）"
            )
        gap_valid_test = test[0] - valid[-1] - 1
        if gap_valid_test < embargo_required:
            raise LeakageError(
                f"split {sid}: embargo 不足（验证→测试间隔 {gap_valid_test} 个交易日，"
                f"要求 ≥ {embargo_required}）"
            )

        train_label_available = train[-1] + offset
        if train_label_available > valid[0]:
            raise LeakageError(
                f"split {sid}: 训练集最后一条标签在位置 {train_label_available} 才可用，"
                f"晚于验证起点 {valid[0]}（holding_days={label_spec.holding_days}, "
                f"entry={label_spec.entry}, 需要 purge ≥ {offset}，"
                f"实际 purge={purge_required}）"
            )
        valid_label_available = valid[-1] + offset
        if valid_label_available > test[0]:
            raise LeakageError(
                f"split {sid}: 验证集最后一条标签在位置 {valid_label_available} 才可用，"
                f"晚于测试起点 {test[0]}（需要 embargo ≥ {offset}，"
                f"实际 embargo={embargo_required}）"
            )

        for pos in test:
            if pos in seen_test:
                raise LeakageError(
                    f"split {sid}: 测试窗与 split {seen_test[pos]} 重叠（位置 {pos}）"
                )
            seen_test[pos] = sid
    return True
