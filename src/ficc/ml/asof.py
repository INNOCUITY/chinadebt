"""As-of 数据视图与特征接口（ML 接口层，**只暴露决策时点已知的信息**）。

时序纪律（与 ``docs/03_methodology.md`` §10.14–§10.19、
``docs/01_project_plan.md`` §12.15/§12.16 一致）
------------------------------------------------------------
- 中债曲线是**日频收盘估值**：t 日曲线在 t 日收盘后才可用，t 日盘中/收盘决策只能看到
  ``≤ t - 1`` 个交易日的观测。``build_asof_view`` 默认 ``availability_lag_days=1`` 就是这
  条规则；
- 本模块**不读文件、不补数据、不插值**：调用方传入已经清洗好的 panel/funding，视图只做
  "按可得时点截断 + 断言不越界"；
- 任何特征/标签只要索引里出现 ``> available_through`` 的日期，一律抛 ``AssertionError``
  （不是静默截断）。这样"未来函数"会在测试里直接炸掉，而不是在绩效里悄悄变好。

**研究边界（必须写进 manifest/报告）**：2019–2025 已经被本项目研究过，不能命名为
"untouched holdout"；本模块只提供 as-of 切片与切分元数据，不做训练、不做绩效比较。
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date, datetime
from typing import Any

import pandas as pd

from ficc.utils.calendar import TradingCalendar

__all__ = [
    "AsOfView",
    "FeatureSpec",
    "build_asof_view",
    "build_features",
]


# ---------------------------------------------------------------------------
# 内部工具（labels.py 复用；不对外承诺 API）
# ---------------------------------------------------------------------------
def _coerce_date(value: Any, *, name: str) -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, pd.Timestamp):
        return value.date()
    if isinstance(value, date):
        return value
    raise TypeError(f"{name} 必须是 datetime.date，实际 {type(value).__name__}")


def _normalise_frame(frame: pd.DataFrame | pd.Series) -> pd.DataFrame | pd.Series:
    """统一为 **排序后的 DatetimeIndex**（不改变列、不填缺失）。"""
    if not isinstance(frame.index, pd.DatetimeIndex):
        index = pd.DatetimeIndex([pd.Timestamp(item) for item in frame.index])
    else:
        index = frame.index
    out = frame.copy()
    out.index = index
    out = out.sort_index()
    return out


def _index_last(frame: pd.DataFrame | pd.Series) -> pd.Timestamp | None:
    if len(frame.index) == 0:
        return None
    return pd.Timestamp(frame.index.max())


def _normalise_lag(value: Any) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"availability_lag_days 必须是整数，实际 {type(value).__name__}")
    if value < 0:
        raise ValueError(f"availability_lag_days 不能为负，实际 {value}")
    return value


def cutoff_for(calendar: TradingCalendar, as_of: date, availability_lag_days: int = 1) -> date:
    """返回 as-of 时点**可用数据的最后日期**。

    - ``lag=0``：as_of 当日（非交易日回退到其前一个交易日）；
    - ``lag>=1``：``calendar.prev_trading_day(as_of, lag)``。
    """
    as_of = _coerce_date(as_of, name="as_of")
    lag = _normalise_lag(availability_lag_days)
    if lag == 0:
        if calendar.is_trading_day(as_of):
            return as_of
        return calendar.prev_trading_day(as_of, 1)
    return calendar.prev_trading_day(as_of, lag)


# ---------------------------------------------------------------------------
# As-of 视图
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class AsOfView:
    """决策时点 ``as_of`` 能看到的**全部**数据（panel 已截断到 ``available_through``）。

    字段
    ----
    as_of : date
        决策日（信号日）。**允许**出现在 metadata，但 panel/funding 里不允许出现
        ``> available_through`` 的行。
    panel : DataFrame
        曲线面板（index=估值日，columns=期限）。只含 ``≤ available_through`` 的行。
    calendar : TradingCalendar
        交易日历（与回测同一套 CN_IB 实测日历）。
    funding : Series | None
        资金面序列（index=估值日），同样已按可得时点截断。
    benchmark_mark : float | Series | None
        可选的基准标记（例如基准 NAV/久期）。Series 会按同样规则截断。
    metadata : dict
        可审计元数据（``availability_lag_days`` / ``requested_as_of`` / ``n_rows`` 等）。
    available_through : date | None
        数据可得最后日期；为 None 时从 ``metadata["availability_lag_days"]`` 推导。
        ``history(n)`` 与所有特征/标签都以它为硬边界。
    """

    as_of: date
    panel: pd.DataFrame
    calendar: TradingCalendar
    funding: pd.Series | None = None
    benchmark_mark: float | pd.Series | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    available_through: date | None = None

    def __post_init__(self) -> None:
        as_of = _coerce_date(self.as_of, name="as_of")
        object.__setattr__(self, "as_of", as_of)
        if not isinstance(self.panel, pd.DataFrame):
            raise TypeError("AsOfView.panel 必须是 pandas.DataFrame")
        panel = _normalise_frame(self.panel)
        assert isinstance(panel, pd.DataFrame)
        object.__setattr__(self, "panel", panel)

        funding = self.funding
        if funding is not None:
            if not isinstance(funding, pd.Series):
                raise TypeError("AsOfView.funding 必须是 pandas.Series 或 None")
            normalised = _normalise_frame(funding)
            assert isinstance(normalised, pd.Series)
            funding = normalised
            object.__setattr__(self, "funding", funding)

        if self.available_through is None:
            lag = _normalise_lag(self.metadata.get("availability_lag_days", 1))
            available_through = cutoff_for(self.calendar, as_of, lag)
            object.__setattr__(self, "available_through", available_through)
        else:
            available_through = _coerce_date(self.available_through, name="available_through")
            object.__setattr__(self, "available_through", available_through)

        if available_through > as_of:
            raise AssertionError(
                f"available_through={available_through} 晚于决策日 as_of={as_of}：违反 as-of 纪律"
            )

        last = _index_last(panel)
        if last is not None and last > pd.Timestamp(available_through):
            raise AssertionError(
                f"panel 最后日期 {last.date()} 晚于 available_through={available_through}："
                "检测到未来数据"
            )
        if funding is not None:
            last_funding = _index_last(funding)
            if last_funding is not None and last_funding > pd.Timestamp(available_through):
                raise AssertionError(
                    f"funding 最后日期 {last_funding.date()} 晚于 "
                    f"available_through={available_through}：检测到未来数据"
                )

    # ---- 只读视图 ------------------------------------------------------
    def history(self, n: int) -> pd.DataFrame:
        """返回最近 ``n`` 行 panel（含 ``available_through`` 当日）。"""
        if isinstance(n, bool) or not isinstance(n, int):
            raise TypeError(f"n 必须是整数，实际 {type(n).__name__}")
        if n <= 0:
            raise ValueError(f"n 必须为正整数，实际 {n}")
        return self.panel.tail(n).copy()

    def funding_history(self, n: int) -> pd.Series | None:
        """返回最近 ``n`` 行资金面（无资金面时返回 None）。"""
        if self.funding is None:
            return None
        if isinstance(n, bool) or not isinstance(n, int):
            raise TypeError(f"n 必须是整数，实际 {type(n).__name__}")
        if n <= 0:
            raise ValueError(f"n 必须为正整数，实际 {n}")
        return self.funding.tail(n).copy()

    @property
    def lag_days(self) -> int:
        return _normalise_lag(self.metadata.get("availability_lag_days", 1))


def build_asof_view(
    panel: pd.DataFrame,
    funding: pd.Series | None,
    calendar: TradingCalendar,
    as_of: date,
    *,
    availability_lag_days: int = 1,
) -> AsOfView:
    """构造 ``as_of`` 的只读数据视图（**只暴露 ``≤ as_of - lag`` 的观测**）。

    Parameters
    ----------
    panel : DataFrame
        曲线面板（index=估值日，columns=期限；数值单位与项目其它模块一致，即 YTM 百分数）。
    funding : Series | None
        资金面序列（index=估值日）。
    calendar : TradingCalendar
        交易日历；用于把 "lag 天" 解释为 **lag 个交易日**。
    as_of : date
        决策日。
    availability_lag_days : int, default 1
        数据可得滞后（交易日）。``1`` 表示 t 日收盘后才有 t 日曲线，t 日决策只能看到
        ``≤ t-1``；``0`` 只用于教学/对照，不得用于正式回测。

    Raises
    ------
    AssertionError
        截断后仍出现 ``> available_through`` 的索引（说明输入/实现有未来数据）。
    """
    as_of = _coerce_date(as_of, name="as_of")
    lag = _normalise_lag(availability_lag_days)
    if not isinstance(calendar, TradingCalendar):
        raise TypeError("calendar 必须是 TradingCalendar")
    if not isinstance(panel, pd.DataFrame):
        raise TypeError("panel 必须是 pandas.DataFrame")
    if panel.columns.empty:
        raise ValueError("panel 至少需要一列")

    available_through = cutoff_for(calendar, as_of, lag)
    panel_norm = _normalise_frame(panel)
    assert isinstance(panel_norm, pd.DataFrame)
    sliced_panel = panel_norm.loc[: pd.Timestamp(available_through)]

    funding_norm: pd.Series | None = None
    sliced_funding: pd.Series | None = None
    if funding is not None:
        if not isinstance(funding, pd.Series):
            raise TypeError("funding 必须是 pandas.Series 或 None")
        funding_norm = _normalise_frame(funding)
        assert isinstance(funding_norm, pd.Series)
        sliced_funding = funding_norm.loc[: pd.Timestamp(available_through)]

    # 显式断言：不允许任何未来行混进来（不静默截断错误输入）
    last = _index_last(sliced_panel)
    if last is not None and last > pd.Timestamp(available_through):
        raise AssertionError(
            f"as_of={as_of} 的视图仍包含 {last.date()} 的观测（上限 {available_through}）"
        )
    if sliced_funding is not None:
        last_funding = _index_last(sliced_funding)
        if last_funding is not None and last_funding > pd.Timestamp(available_through):
            raise AssertionError(
                f"as_of={as_of} 的 funding 仍包含 {last_funding.date()} 的观测"
                f"（上限 {available_through}）"
            )

    metadata: dict[str, Any] = {
        "availability_lag_days": lag,
        "requested_as_of": as_of.isoformat(),
        "available_through": available_through.isoformat(),
        "n_rows": len(sliced_panel),
        "n_funding_rows": len(sliced_funding) if sliced_funding is not None else 0,
        "panel_columns": tuple(float(c) for c in sliced_panel.columns),
        "sample_note": "2019–2025 已被研究，不是 untouched holdout",
    }
    return AsOfView(
        as_of=as_of,
        panel=sliced_panel,
        calendar=calendar,
        funding=sliced_funding,
        benchmark_mark=None,
        metadata=metadata,
        available_through=available_through,
    )


# ---------------------------------------------------------------------------
# 特征
# ---------------------------------------------------------------------------
def _resolve_column(panel: pd.DataFrame, name: str) -> Any | None:
    """把 ``FeatureSpec.inputs`` 的名字解析到 panel 的实际列。

    先按原样匹配；再尝试 ``float(name)``（曲线面板的期限列是浮点数，而 inputs 的
    契约定为字符串，例如 ``"2.0"`` → ``2.0``）。
    """
    if name in panel.columns:
        return name
    try:
        numeric = float(name)
    except (TypeError, ValueError):
        return None
    return numeric if numeric in panel.columns else None


@dataclass(frozen=True)
class FeatureSpec:
    """一个特征定义：``transform`` 只接收 ``inputs`` 列的历史切片。

    约定
    ----
    - ``transform`` 的输入是 DataFrame（index=估值日，columns=``inputs``），**不含**未来行；
    - 输出必须是 ``Series`` 或 ``DataFrame``，且 index 必须是输入历史的子集；
    - 输出索引一旦超过 ``AsOfView.available_through``，``build_features`` 直接抛
      ``AssertionError``（不截断、不填充）。
    """

    name: str
    transform: Callable[[pd.DataFrame], pd.Series | pd.DataFrame]
    inputs: tuple[str, ...]

    def __post_init__(self) -> None:
        if not isinstance(self.name, str) or not self.name:
            raise ValueError("FeatureSpec.name 必须是非空字符串")
        if not callable(self.transform):
            raise TypeError("FeatureSpec.transform 必须可调用")
        if not isinstance(self.inputs, tuple):
            raise TypeError("FeatureSpec.inputs 必须是 tuple[str, ...]")
        for column in self.inputs:
            if not isinstance(column, str) or not column:
                raise ValueError(f"FeatureSpec.inputs 含非法列名：{column!r}")
        if len(set(self.inputs)) != len(self.inputs):
            raise ValueError(f"FeatureSpec {self.name} 的 inputs 含重复列")


def build_features(
    view: AsOfView, specs: tuple[FeatureSpec, ...] | list[FeatureSpec]
) -> pd.DataFrame:
    """在 as-of 视图上计算特征（**变换只能看到历史切片**）。

    - 每个 spec 只拿到 ``view.panel``（已截断）的 ``inputs`` 列，深拷贝后传入；
    - 输出拼接为一张宽表，列名为 ``spec.name``（多列输出用 ``name__原列名``）；
    - 任何输出索引 ``> view.available_through`` 或不在 ``view.panel.index`` 内 → 断言失败。

    Returns
    -------
    DataFrame
        index 为历史估值日（按时间升序），columns 为特征名。
    """
    if not isinstance(view, AsOfView):
        raise TypeError("view 必须是 AsOfView")
    spec_tuple = tuple(specs)
    for spec in spec_tuple:
        if not isinstance(spec, FeatureSpec):
            raise TypeError(f"specs 只能包含 FeatureSpec，实际 {type(spec).__name__}")

    if not spec_tuple:
        return pd.DataFrame(index=view.panel.index.copy())

    available = pd.Timestamp(view.available_through)
    history_index = view.panel.index
    frames: list[pd.DataFrame] = []
    seen: set[str] = set()
    for spec in spec_tuple:
        resolved: list[Any] = []
        missing: list[str] = []
        for column in spec.inputs:
            actual = _resolve_column(view.panel, column)
            if actual is None:
                missing.append(column)
            else:
                resolved.append(actual)
        if missing:
            raise KeyError(f"特征 {spec.name} 的输入列不存在：{missing}")
        if resolved:
            payload = view.panel.loc[:, resolved].copy(deep=True)
            # transform 看到的列名与 spec.inputs 完全一致（支持浮点期限列按 "2.0" 引用）
            payload.columns = list(spec.inputs)
        else:
            payload = view.panel.iloc[:, :0].copy(deep=True)
        # 防卫：传给 transform 的切片本身也不能含未来行
        last = _index_last(payload)
        if last is not None and last > available:
            raise AssertionError(f"特征 {spec.name} 的输入切片含未来行 {last.date()}")

        result = spec.transform(payload)
        if isinstance(result, pd.Series):
            frame = result.to_frame(name=spec.name)
        elif isinstance(result, pd.DataFrame):
            frame = result.copy()
            if frame.shape[1] == 1:
                frame.columns = [spec.name]
            else:
                frame.columns = [f"{spec.name}__{column}" for column in frame.columns]
        else:
            raise TypeError(
                f"特征 {spec.name} 的 transform 必须返回 Series/DataFrame，"
                f"实际 {type(result).__name__}"
            )

        frame.index = pd.DatetimeIndex(frame.index)
        last = _index_last(frame)
        if last is not None and last > available:
            raise AssertionError(
                f"特征 {spec.name} 输出索引 {last.date()} 超过 available_through="
                f"{view.available_through}：检测到未来信息"
            )
        if len(frame.index) and not frame.index.isin(history_index).all():
            raise AssertionError(f"特征 {spec.name} 输出索引不在 as-of 视图的历史索引内")
        for column in frame.columns:
            name = str(column)
            if name in seen:
                raise ValueError(f"特征列名重复：{name}")
            seen.add(name)
        frames.append(frame)

    features = pd.concat(frames, axis=1).sort_index()
    features.index.name = view.panel.index.name or "date"
    last = _index_last(features)
    if last is not None and last > available:
        raise AssertionError(
            f"特征表最后索引 {last.date()} 超过 available_through={view.available_through}"
        )
    return features
