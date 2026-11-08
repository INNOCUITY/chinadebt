"""监督学习数据集契约（R43/§12.17）：**逐决策时点特征 + 账本口径市场标签**。

契约（必须与 ``docs/01_project_plan.md`` §12.17、``docs/03_methodology.md`` §10.20 一致）
----------------------------------------------------------------------------------
1. **信息时钟**（:class:`InformationClock`）：中债曲线是 t 日收盘估值，t 日收盘决策只能
   看到 ``≤ t - observation_lag_days`` 的观测；执行按 ``next_close``（decision+1 交易日
   收盘）；标签在 ``label_end`` 收盘后才可用，且再晚 ``label_lag_days`` 个交易日。
2. **特征**：只能通过 :func:`ficc.ml.asof.build_training_matrix` **逐决策时点**构造
   （每个 t 单独 as-of 视图、只取当前行），不得整段历史一次算完再截断；构造后还会跑
   :func:`ficc.ml.asof.assert_per_row_causality` 做逐行因果性复核。
3. **标签（ledger 口径）**：用**同一个** :func:`ficc.backtest.engine.run_backtest`、
   同一账本/滚动政策/成本/融资生成**全区间一次**的 ``benchmark_nav``（精确基准回放控制组），
   标签 = ``bench_nav[label_end] / bench_nav[execution] - 1``，其中
   ``execution = decision + 1`` 交易日（next-close），``label_end = decision + holding_days``
   交易日。基准 NAV 已经计入买入价差/手续费、票息、融资成本与滚动政策，是**与策略无关
   的市场标签**；标签**不使用未来曲线直接合成**，也不在循环里重跑引擎。
4. **行级审计列**：``decision_as_of / available_through / execution_date /
   label_end_date / label_available_after / label`` + 特征列；每行满足
   ``available_through ≤ decision_as_of``、``execution_date ≤ label_end_date``、
   ``label_available_after > label_end_date``。
5. **NaN 规则**：特征因预热期（如 60 日动量、2Y 缺口）不足而缺失的行默认**整行删除**
   并在 ``attrs``/manifest 中逐行披露；标签缺失（基准 NAV 缺日期）直接报错，不静默保留。

研究边界：2019–2025 已被本项目研究过，不能命名为 "untouched holdout"；本模块只产出
数据集与 manifest，不做绩效结论。基金/融资变动对特征的影响必须通过"把资金面作为
panel 的显式列"传入（见 :func:`attach_funding_column`），因为 ``FeatureSpec`` 只绑定
panel 列；``funding`` 参数仍同时用于基准账本的融资成本。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

import numpy as np
import pandas as pd

from ficc.config import Config
from ficc.ml.asof import (
    FeatureSpec,
    _coerce_date,
    _normalise_decision_dates,
    assert_per_row_causality,
    build_training_matrix,
)
from ficc.ml.labels import ForwardLabelSpec
from ficc.portfolio.types import MarketContext, TargetFaces
from ficc.utils.calendar import TradingCalendar

__all__ = [
    "ClockError",
    "InformationClock",
    "attach_funding_column",
    "benchmark_nav_for_labels",
    "build_supervised_dataset",
    "dataset_manifest",
]

_HOLDOUT_NOTE = (
    "2019–2025 已被本项目研究过，不是 untouched holdout；数据集只用于研究/训练，测试窗只做最后评估"
)

LABEL_DEFINITION = (
    "benchmark_nav[label_end] / benchmark_nav[execution_date] - 1；"
    "execution_date = decision + 1 交易日（next-close），"
    "label_end = decision + holding_days 交易日；基准 NAV 由同一 run_backtest 账本"
    "（含成本/票息/融资/滚动政策）生成，是与策略无关的市场标签"
)

DATE_COLUMNS: tuple[str, ...] = (
    "decision_as_of",
    "available_through",
    "execution_date",
    "label_end_date",
    "label_available_after",
)
RESERVED_COLUMNS: frozenset[str] = frozenset((*DATE_COLUMNS, "label"))


class ClockError(ValueError):
    """信息时钟无法在给定交易日历上确定某个时点（缺数据/越界）。"""


@dataclass(frozen=True)
class InformationClock:
    """决策 → 数据可得 → 执行 → 标签结束 → 标签可用的交易日时钟。

    Parameters
    ----------
    observation_lag_days : int, default 1
        数据可得滞后（交易日）：``available_through = prev_trading_day(decision, lag)``。
        ``1`` 表示 t 日收盘后才有 t 日曲线，t 日决策只能看到 ``≤ t-1``。
    execution : {"next_close"}, default "next_close"
        执行约定：decision+1 交易日**收盘**成交（与引擎 ``next_close`` 一致）。
    holding_days : int, default 5
        标签 horizon（交易日）：``label_end = decision + holding_days``。
    label_lag_days : int, default 1
        标签在 ``label_end`` 收盘后再过多少个交易日才可用（≥1，保证严格晚于 label_end）。

    Notes
    -----
    标签区间是 ``(execution_date, label_end]``（即 ``bench_nav[t+h]/bench_nav[t+1]``）。
    它同时给出 ``label_available_offset = holding_days + label_lag_days``，与
    ``ForwardLabelSpec(holding_days, entry="next_close").available_offset`` 相等，
    因此 walk-forward 的 purge 可以直接复用现有
    :func:`ficc.ml.walkforward.validate_no_leakage`。
    """

    observation_lag_days: int = 1
    execution: Literal["next_close"] = "next_close"
    holding_days: int = 5
    label_lag_days: int = 1

    def __post_init__(self) -> None:
        lag = self.observation_lag_days
        if isinstance(lag, bool) or not isinstance(lag, int):
            raise TypeError(f"observation_lag_days 必须是整数，实际 {type(lag).__name__}")
        if lag < 0:
            raise ValueError(f"observation_lag_days 不能为负，实际 {lag}")
        if self.execution != "next_close":
            raise ValueError(f"execution 只支持 'next_close'，实际 {self.execution!r}")
        holding = self.holding_days
        if isinstance(holding, bool) or not isinstance(holding, int):
            raise TypeError(f"holding_days 必须是整数，实际 {type(holding).__name__}")
        if holding < 1:
            raise ValueError(f"holding_days 必须 ≥ 1，实际 {holding}")
        label_lag = self.label_lag_days
        if isinstance(label_lag, bool) or not isinstance(label_lag, int):
            raise TypeError(f"label_lag_days 必须是整数，实际 {type(label_lag).__name__}")
        if label_lag < 1:
            raise ValueError(
                f"label_lag_days 必须 ≥ 1（标签必须严格晚于 label_end 才可用），实际 {label_lag}"
            )

    # ---- 派生属性 ------------------------------------------------------
    @property
    def execution_offset(self) -> int:
        """执行日相对决策日的交易日偏移（next_close → 1）。"""
        return 1

    @property
    def label_end_offset(self) -> int:
        """``label_end`` 相对决策日的交易日偏移。"""
        return self.holding_days

    @property
    def label_available_offset(self) -> int:
        """标签可用日相对决策日的交易日偏移（= holding_days + label_lag_days）。"""
        return self.holding_days + self.label_lag_days

    @property
    def label_definition(self) -> str:
        return LABEL_DEFINITION

    # ---- 时钟边界（均按交易日历；越界抛明确异常）------------------------
    def _require_calendar(self, calendar: TradingCalendar) -> TradingCalendar:
        if not isinstance(calendar, TradingCalendar):
            raise TypeError("calendar 必须是 TradingCalendar")
        return calendar

    def available_through(self, decision_as_of: Any, calendar: TradingCalendar) -> date:
        """决策时点能看到的**最后**观测日（``≤ decision - lag``）。"""
        calendar = self._require_calendar(calendar)
        decision = _coerce_date(decision_as_of, name="decision_as_of")
        lag = self.observation_lag_days
        try:
            if lag == 0:
                if calendar.is_trading_day(decision):
                    return decision
                return calendar.prev_trading_day(decision, 1)
            return calendar.prev_trading_day(decision, lag)
        except ValueError as exc:
            raise ClockError(
                f"决策日 {decision} 的 available_through 无法确定（lag={lag} 交易日）：{exc}"
            ) from exc

    def execution_date(self, decision_as_of: Any, calendar: TradingCalendar) -> date:
        """执行日（decision + 1 交易日，next-close 成交）。"""
        calendar = self._require_calendar(calendar)
        decision = _coerce_date(decision_as_of, name="decision_as_of")
        try:
            return calendar.next_trading_day(decision, self.execution_offset)
        except ValueError as exc:
            raise ClockError(f"决策日 {decision} 的执行日无法确定：{exc}") from exc

    def label_end(self, decision_as_of: Any, calendar: TradingCalendar) -> date:
        """标签区间结束日（decision + holding_days 交易日收盘）。"""
        calendar = self._require_calendar(calendar)
        decision = _coerce_date(decision_as_of, name="decision_as_of")
        try:
            return calendar.next_trading_day(decision, self.label_end_offset)
        except ValueError as exc:
            raise ClockError(
                f"决策日 {decision} 的 label_end（+{self.label_end_offset} 交易日）无法确定：{exc}"
            ) from exc

    def label_available_after(self, decision_as_of: Any, calendar: TradingCalendar) -> date:
        """标签最早可被观测/使用的日期（label_end + label_lag_days 交易日）。"""
        calendar = self._require_calendar(calendar)
        decision = _coerce_date(decision_as_of, name="decision_as_of")
        try:
            end = self.label_end(decision, calendar)
            return calendar.next_trading_day(end, self.label_lag_days)
        except ClockError:
            raise
        except ValueError as exc:
            raise ClockError(f"决策日 {decision} 的 label_available_after 无法确定：{exc}") from exc

    # ---- 与现有接口的对接 ----------------------------------------------
    def to_forward_label_spec(self) -> ForwardLabelSpec:
        """转成 walk-forward 校验用的 ``ForwardLabelSpec``（只用于 purge 边界）。

        ``ForwardLabelSpec(h, entry="next_close").available_offset == h + 1``，与本时钟的
        ``label_available_offset`` 相同；但本契约的标签收益终点是 ``decision + h``，
        比该 spec 的 ``exit = decision + 1 + h`` 早一个交易日（以 :data:`LABEL_DEFINITION`
        为准）。
        """
        return ForwardLabelSpec(holding_days=self.holding_days, entry="next_close")

    def describe(self) -> dict[str, Any]:
        """JSON 友好的时钟描述（写入 dataset.attrs / manifest）。"""
        return {
            "observation_lag_days": self.observation_lag_days,
            "execution": self.execution,
            "holding_days": self.holding_days,
            "label_lag_days": self.label_lag_days,
            "execution_offset_days": self.execution_offset,
            "label_end_offset_days": self.label_end_offset,
            "label_available_offset_days": self.label_available_offset,
            "label_definition": self.label_definition,
            "calendar_rule": "所有边界按 TradingCalendar 的交易日推进；越界抛 ClockError",
        }


# ---------------------------------------------------------------------------
# 基准 NAV（全区间一次 run_backtest）
# ---------------------------------------------------------------------------
class _ExactBenchmarkReplay:
    """精确基准回放控制组：目标 = 基准逐腿面值（只用于生成 benchmark_nav）。

    .. note::
       本策略不是可交易策略、也不参与绩效比较；它让策略侧与基准侧共享同一执行/成本/
       滚动口径，从而保证 ``run_backtest`` 产出的 ``benchmark_nav`` 不受策略求解偏差影响。
    """

    name = "ml_benchmark_replay"

    def target_krd(self, ctx: MarketContext) -> TargetFaces:
        return TargetFaces(
            as_of=ctx.as_of,
            faces={leg.bond.code: leg.face_value for leg in ctx.benchmark.legs},
            notes="ML 标签：精确基准面值回放（仅生成 benchmark NAV）",
        )


def _validate_benchmark_config(cfg: Config) -> None:
    """滚动政策与基准版本必须一致（与引擎的硬校验相同）。"""
    rolling_enabled = bool(cfg.portfolio.rolling.enabled)
    benchmark_version = str(cfg.backtest.benchmark_version)
    if rolling_enabled and benchmark_version != "rolling":
        raise ValueError(
            "portfolio.rolling.enabled=True 时 benchmark_version 必须是 'rolling'"
            f"（实际 {benchmark_version!r}）：滚动基准必须单独版本化"
        )
    if (not rolling_enabled) and benchmark_version == "rolling":
        raise ValueError("benchmark_version='rolling' 需要 portfolio.rolling.enabled=True")


def benchmark_nav_for_labels(
    panel: pd.DataFrame,
    funding: pd.Series | None,
    calendar: TradingCalendar,
    start: date,
    end: date,
    cfg: Config,
) -> pd.Series:
    """用**一次** :func:`ficc.backtest.engine.run_backtest` 生成全区间 ``benchmark_nav``。

    参数与 :func:`build_supervised_dataset` 相同；``benchmark_version`` 必须与配置一致
    （滚动政策开启时基准必须为 ``rolling``，与引擎的硬校验一致）。

    Returns
    -------
    pandas.Series
        index=估值日，values=基准净值和；已计入成本/票息/融资/滚动政策。
    """
    if not isinstance(panel, pd.DataFrame):
        raise TypeError("panel 必须是 pandas.DataFrame")
    if not isinstance(calendar, TradingCalendar):
        raise TypeError("calendar 必须是 TradingCalendar")
    if not isinstance(cfg, Config):
        raise TypeError("cfg 必须是 Config")
    start = _coerce_date(start, name="start")
    end = _coerce_date(end, name="end")
    if end <= start:
        raise ValueError(f"基准区间 end={end} 必须晚于 start={start}")
    _validate_benchmark_config(cfg)

    from ficc.backtest.engine import run_backtest

    result = run_backtest(
        _ExactBenchmarkReplay(),
        panel=panel,
        calendar=calendar,
        funding=funding,
        cfg=cfg,
        start=start,
        end=end,
    )
    nav = result.benchmark_nav.astype(float).copy()
    nav.index = pd.DatetimeIndex([pd.Timestamp(item) for item in nav.index])
    nav = nav.sort_index()
    nav.name = "benchmark_nav"
    if nav.empty:
        raise ValueError("run_backtest 返回的 benchmark_nav 为空")
    if nav.isna().any():
        raise ValueError("benchmark_nav 含 NaN，无法作为标签基准")
    if not np.isfinite(nav.to_numpy(dtype=float)).all():
        raise ValueError("benchmark_nav 含非有限值，无法作为标签基准")
    return nav


# ---------------------------------------------------------------------------
# 特征输入辅助
# ---------------------------------------------------------------------------
def attach_funding_column(
    panel: pd.DataFrame,
    funding: pd.Series | None,
    name: str = "FDR007",
) -> pd.DataFrame:
    """把资金面序列作为 **panel 的数值列**附加，供 ``FeatureSpec`` 使用。

    为什么需要它：``FeatureSpec`` 的 ``inputs`` 只解析 panel 列，``build_asof_view`` 不会
    把 ``funding`` 参数注入 transform；因此 carry/资金面特征必须显式成为 panel 的一列。
    该列只参与特征计算，``run_backtest`` 的曲线定价仍只取 ``CANONICAL_TENORS``。

    Parameters
    ----------
    panel : DataFrame
        曲线面板（index=估值日）。
    funding : Series | None
        资金面序列；``None`` 时原样返回 panel。
    name : str, default "FDR007"
        列名；不得与面板已有列冲突（避免静默覆盖期限列）。

    Returns
    -------
    pandas.DataFrame
        新面板（深拷贝），最后一列为资金面。
    """
    if not isinstance(panel, pd.DataFrame):
        raise TypeError("panel 必须是 pandas.DataFrame")
    if funding is None:
        return panel.copy(deep=True)
    if not isinstance(funding, pd.Series):
        raise TypeError("funding 必须是 pandas.Series 或 None")
    if not isinstance(name, str) or not name:
        raise ValueError("资金面列名必须是非空字符串")
    if name in panel.columns:
        raise ValueError(f"panel 已有列 {name!r}，拒绝覆盖（请显式换名或先重命名）")
    try:  # 与 asof._resolve_column 一致：字符串列名可能对应浮点期限列
        numeric_name = float(name)
    except (TypeError, ValueError):
        numeric_name = None
    if numeric_name is not None and numeric_name in panel.columns:
        raise ValueError(f"panel 已有期限列 {numeric_name!r}（列名 {name!r} 解析后冲突），拒绝覆盖")
    aligned = funding.copy(deep=True)
    aligned.index = pd.DatetimeIndex([pd.Timestamp(item) for item in aligned.index])
    aligned = aligned.sort_index()
    if aligned.index.has_duplicates:
        raise ValueError("funding 索引含重复日期，无法作为特征列")
    out = panel.copy(deep=True)
    out[name] = aligned.reindex(out.index)
    return out


# ---------------------------------------------------------------------------
# 数据集构造
# ---------------------------------------------------------------------------
def _rolling_version(cfg: Config) -> str:
    if not cfg.portfolio.rolling.enabled:
        return ""
    return str(cfg.portfolio.rolling.version)


def _normalise_benchmark_nav(nav: pd.Series) -> pd.Series:
    if not isinstance(nav, pd.Series):
        raise TypeError("benchmark_nav 必须是 pandas.Series")
    out = nav.astype(float).copy()
    out.index = pd.DatetimeIndex([pd.Timestamp(item) for item in out.index])
    out = out.sort_index()
    if out.index.has_duplicates:
        raise ValueError("benchmark_nav 索引含重复日期")
    if out.empty:
        raise ValueError("benchmark_nav 为空")
    return out


def _nav_value(nav: pd.Series, when: date, *, role: str, decision: date) -> float:
    key = pd.Timestamp(when)
    if key not in nav.index:
        raise ValueError(
            f"decision={decision}: 基准 NAV 缺少{role} {when}（benchmark_nav 覆盖 "
            f"[{nav.index.min().date()}, {nav.index.max().date()}]）"
        )
    value = nav.loc[key]
    if isinstance(value, pd.Series):  # pragma: no cover - 已拒绝重复索引
        raise ValueError(f"基准 NAV 在 {when} 有多行")
    number = float(value)
    if not np.isfinite(number) or number <= 0.0:
        raise ValueError(f"decision={decision}: 基准 NAV 在{role} {when} 非正/非有限：{number!r}")
    return number


def _validate_feature_specs(specs: Any) -> tuple[FeatureSpec, ...]:
    spec_tuple = tuple(specs)
    for spec in spec_tuple:
        if not isinstance(spec, FeatureSpec):
            raise TypeError(f"feature_specs 只能包含 FeatureSpec，实际 {type(spec).__name__}")
    return spec_tuple


def build_supervised_dataset(
    panel: pd.DataFrame,
    funding: pd.Series | None,
    calendar: TradingCalendar,
    decision_dates: Any,
    feature_specs: Any,
    cfg: Config,
    clock: InformationClock | None = None,
    *,
    benchmark_nav: pd.Series | None = None,
    require_complete: bool = True,
    causality_check: bool = True,
) -> pd.DataFrame:
    """构造监督学习样本表（逐决策时点特征 + 同一账本口径的市场标签）。

    Parameters
    ----------
    panel : DataFrame
        曲线面板（index=估值日，columns 为期限；可额外含**数值**特征列，例如
        :func:`attach_funding_column` 附加的资金面——定价/回测只取 ``CANONICAL_TENORS``）。
    funding : Series | None
        资金面序列；用于基准账本的融资成本（以及调用方自行附加到 panel 做特征）。
    calendar : TradingCalendar
        交易日历；所有时钟边界按它推进。
    decision_dates : Iterable[date]
        决策日（信号日）序列；去重、升序。
    feature_specs : Sequence[FeatureSpec]
        特征定义；只允许使用决策时点可得的数据（逐时点 as-of 构造 + 因果性复核）。
    cfg : Config
        配置；``benchmark_version`` 与 ``portfolio.rolling.enabled`` 必须一致。
    clock : InformationClock, optional
        信息时钟；默认 ``InformationClock()``（lag=1、next_close、5 日、标签滞后 1 日）。
    benchmark_nav : Series, optional
        若已用 :func:`benchmark_nav_for_labels` 生成过全区间基准 NAV，可注入复用（避免重复
        跑引擎）。默认 ``None``：只跑**一次**全区间 ``run_backtest``（起点 = 最早决策日）。
        .. warning::
           注入的 NAV 必须与默认路径**用同一起点**生成：基准在起点用当时的曲线重建合成券
           （发行日/票面随起点变化，滚动政策同理），不同起点的 NAV 不能视为同一条市场标签。
    require_complete : bool, default True
        ``True``：特征含 NaN 的行整行删除（并在 attrs 披露）；``False``：保留并披露 NaN 规则。
    causality_check : bool, default True
        是否调用 :func:`assert_per_row_causality` 做逐行因果性复核（推荐开启）。

    Returns
    -------
    pandas.DataFrame
        index=决策日（DatetimeIndex），列 = 5 个审计日期列 + ``label`` + 特征列；
        ``attrs`` 含 ``config_hash / benchmark_version / rolling_version / clock`` 等。

    Raises
    ------
    ValueError
        决策/标签越界、基准 NAV 缺日期、特征全部缺失等。
    ClockError
        信息时钟无法在给定日历上确定边界。
    AssertionError
        特征变换读取未来（``assert_per_row_causality``）。
    """
    clock = InformationClock() if clock is None else clock
    if not isinstance(clock, InformationClock):
        raise TypeError("clock 必须是 InformationClock")
    if not isinstance(panel, pd.DataFrame):
        raise TypeError("panel 必须是 pandas.DataFrame")
    if panel.columns.empty:
        raise ValueError("panel 至少需要一列")
    if not isinstance(calendar, TradingCalendar):
        raise TypeError("calendar 必须是 TradingCalendar")
    if not isinstance(cfg, Config):
        raise TypeError("cfg 必须是 Config")
    _validate_benchmark_config(cfg)
    if clock.holding_days < 2:
        raise ValueError(
            "holding_days 必须 ≥ 2：next_close 在 decision+1 成交，"
            "label_end=decision+holding_days 必须晚于执行日才有非零收益区间"
        )
    spec_tuple = _validate_feature_specs(feature_specs)
    decisions = _normalise_decision_dates(decision_dates)
    non_trading = [decision for decision in decisions if not calendar.is_trading_day(decision)]
    if non_trading:
        raise ValueError(
            "decision_dates 必须是交易日：非交易日会破坏 next-close 执行/基准起点口径，"
            f"实际 {non_trading[:3]}"
        )

    # ---- 1) 每行时钟边界（先全部算出，越界尽早报错）----------------------
    rows: list[dict[str, Any]] = []
    for decision in decisions:
        available = clock.available_through(decision, calendar)
        execution = clock.execution_date(decision, calendar)
        label_end = clock.label_end(decision, calendar)
        label_after = clock.label_available_after(decision, calendar)
        if available > decision:
            raise AssertionError(f"decision={decision}: available_through={available} 晚于决策日")
        if execution > label_end:
            raise ValueError(
                f"decision={decision}: execution_date={execution} 晚于 label_end={label_end}"
            )
        if label_after <= label_end:
            raise AssertionError(
                f"decision={decision}: label_available_after={label_after} 不晚于 "
                f"label_end={label_end}"
            )
        rows.append(
            {
                "decision_as_of": pd.Timestamp(decision),
                "available_through": pd.Timestamp(available),
                "execution_date": pd.Timestamp(execution),
                "label_end_date": pd.Timestamp(label_end),
                "label_available_after": pd.Timestamp(label_after),
            }
        )
    meta = pd.DataFrame(
        rows, index=pd.DatetimeIndex([pd.Timestamp(d) for d in decisions], name="date")
    )

    # ---- 2) 逐决策时点特征（禁止整段一次算完再截断）----------------------
    features = build_training_matrix(
        panel,
        funding,
        calendar,
        decisions,
        spec_tuple,
        availability_lag_days=clock.observation_lag_days,
    )
    feature_names = [str(column) for column in features.columns]
    clashes = sorted(set(feature_names) & RESERVED_COLUMNS)
    if clashes:
        raise ValueError(f"特征列名与数据集保留列冲突：{clashes}")
    if features.index.equals(meta.index) is False:
        features.index = pd.DatetimeIndex(
            [pd.Timestamp(item) for item in features.index], name="date"
        )
        features = features.sort_index()
    if not features.index.equals(meta.index):
        raise AssertionError("特征矩阵索引与决策日索引不一致")

    # 复核 build_training_matrix 的逐行数据截止日与时钟一致
    through = features.attrs.get("available_through", {})
    for decision, available in zip(decisions, meta["available_through"].tolist(), strict=True):
        recorded = through.get(decision.isoformat())
        if recorded != pd.Timestamp(available).date().isoformat():
            raise AssertionError(
                f"decision={decision}: build_training_matrix 的 available_through={recorded} "
                f"与时钟 {pd.Timestamp(available).date()} 不一致"
            )

    if causality_check and feature_names:
        assert_per_row_causality(
            features,
            panel,
            calendar,
            spec_tuple,
            funding=funding,
            availability_lag_days=clock.observation_lag_days,
            sample_rows=min(5, len(decisions)),
        )

    # ---- 3) 基准 NAV（全区间只跑一次引擎）与标签 -------------------------
    execution_dates = [pd.Timestamp(item).date() for item in meta["execution_date"]]
    label_end_dates = [pd.Timestamp(item).date() for item in meta["label_end_date"]]
    if benchmark_nav is None:
        nav = benchmark_nav_for_labels(
            panel,
            funding,
            calendar,
            min(decisions),
            max(label_end_dates),
            cfg,
        )
        nav_source = "engine_single_run"
    else:
        nav = _normalise_benchmark_nav(benchmark_nav)
        nav_source = "provided"
    labels = [
        _nav_value(nav, end, role="标签结束日", decision=decision)
        / _nav_value(nav, execution, role="执行日", decision=decision)
        - 1.0
        for decision, execution, end in zip(
            decisions, execution_dates, label_end_dates, strict=True
        )
    ]
    dataset = meta.copy()
    dataset["label"] = np.asarray(labels, dtype=float)
    feature_block = features.reset_index(drop=True)
    feature_block.index = dataset.index
    dataset = pd.concat([dataset, feature_block], axis=1)
    dataset.index = pd.DatetimeIndex([pd.Timestamp(d) for d in decisions], name="date")

    # ---- 4) 行级契约断言 ------------------------------------------------
    if not (dataset["available_through"] <= dataset["decision_as_of"]).all():
        raise AssertionError("存在 available_through > decision_as_of 的行（未来数据）")
    if not (dataset["execution_date"] <= dataset["label_end_date"]).all():
        raise AssertionError("存在 execution_date > label_end_date 的行")
    if not (dataset["label_available_after"] > dataset["label_end_date"]).all():
        raise AssertionError("存在 label_available_after ≤ label_end_date 的行")
    if not (dataset["label_end_date"] > dataset["decision_as_of"]).all():
        raise AssertionError("存在 label_end_date ≤ decision_as_of 的行")
    if dataset["label"].isna().any():
        raise ValueError("标签含 NaN：基准 NAV 不完整")
    if not np.isfinite(dataset["label"].to_numpy(dtype=float)).all():
        raise ValueError("标签含非有限值")

    # ---- 5) NaN 规则 ----------------------------------------------------
    n_input = len(dataset)
    nan_rows: list[dict[str, str]] = []
    dropped_rows: list[dict[str, str]] = []
    if feature_names:
        nan_mask = dataset[feature_names].isna().any(axis=1)
        if nan_mask.any():
            nan_rows = [
                {
                    "decision_as_of": pd.Timestamp(idx).date().isoformat(),
                    "reason": "feature_warmup_or_missing_input",
                }
                for idx in dataset.index[nan_mask.to_numpy()]
            ]
            if require_complete:
                dataset = dataset.loc[~nan_mask].copy()
                dropped_rows = nan_rows
    nan_rule = (
        "特征列在决策时点含 NaN 的行整行删除（预热期/输入缺口），逐行披露在 attrs；"
        "标签 NaN 直接报错"
        if require_complete
        else "特征列 NaN 保留（require_complete=False），逐行披露在 attrs；标签 NaN 直接报错"
    )
    if dataset.empty:
        raise ValueError(f"所有 {n_input} 行都因特征 NaN 被删除；请增加历史预热或检查输入列")

    # ---- 6) attrs -------------------------------------------------------
    dataset.attrs = {
        "builder": "build_supervised_dataset",
        "config_hash": cfg.hash(),
        "benchmark_version": str(cfg.backtest.benchmark_version),
        "rolling_version": _rolling_version(cfg),
        "clock": clock.describe(),
        "observation_lag_days": clock.observation_lag_days,
        "holding_days": clock.holding_days,
        "label_lag_days": clock.label_lag_days,
        "label_available_offset_days": clock.label_available_offset,
        "label_name": "label",
        "label_definition": LABEL_DEFINITION,
        "label_source": "run_backtest.benchmark_nav",
        "benchmark_nav_source": nav_source,
        "benchmark_nav_window": [
            nav.index.min().date().isoformat(),
            nav.index.max().date().isoformat(),
        ],
        "feature_names": feature_names,
        "n_features": len(feature_names),
        "n_input_rows": n_input,
        "n_rows": len(dataset),
        "n_dropped_rows": len(dropped_rows),
        "dropped_rows": dropped_rows,
        "n_nan_feature_rows": len(nan_rows),
        "nan_rows": nan_rows,
        "nan_rule": nan_rule,
        "per_row_causality": bool(causality_check and feature_names),
        "holdout_status": "researched_sample",
        "holdout_note": _HOLDOUT_NOTE,
    }
    return dataset


# ---------------------------------------------------------------------------
# Manifest
# ---------------------------------------------------------------------------
def dataset_manifest(dataset: pd.DataFrame) -> dict[str, Any]:
    """把数据集的可审计契约写成 JSON 友好 dict（脚本另存为 manifest.json）。

    Raises
    ------
    TypeError
        输入不是 DataFrame。
    ValueError
        输入不是 :func:`build_supervised_dataset` 的输出（缺 attrs）。
    """
    if not isinstance(dataset, pd.DataFrame):
        raise TypeError("dataset 必须是 pandas.DataFrame")
    attrs = dataset.attrs
    required = (
        "config_hash",
        "benchmark_version",
        "rolling_version",
        "clock",
        "feature_names",
        "label_definition",
    )
    missing = [key for key in required if key not in attrs]
    if missing:
        raise ValueError(
            f"dataset 缺少数据集契约 attrs {missing}；请使用 build_supervised_dataset 的输出"
        )
    index = pd.DatetimeIndex(dataset.index)
    if index.empty:
        raise ValueError("dataset 为空，无法生成 manifest")
    feature_names = [str(name) for name in attrs["feature_names"]]
    date_columns = [column for column in DATE_COLUMNS if column in dataset.columns]
    return {
        "dataset": "supervised_ml_dataset",
        "builder": attrs.get("builder", "build_supervised_dataset"),
        "n_rows": len(dataset),
        "n_features": len(feature_names),
        "start": index.min().date().isoformat(),
        "end": index.max().date().isoformat(),
        "features": feature_names,
        "columns": [str(column) for column in dataset.columns],
        "date_columns": date_columns,
        "label": {
            "name": attrs.get("label_name", "label"),
            "definition": attrs["label_definition"],
            "source": attrs.get("label_source", "run_backtest.benchmark_nav"),
            "benchmark_nav_source": attrs.get("benchmark_nav_source"),
            "benchmark_nav_window": attrs.get("benchmark_nav_window"),
            "execution": attrs["clock"].get("execution"),
            "holding_days": attrs["clock"].get("holding_days"),
            "label_lag_days": attrs["clock"].get("label_lag_days"),
            "available_after_rule": (
                "label_available_after = label_end + label_lag_days 个交易日；严格晚于 label_end"
            ),
        },
        "clock": dict(attrs["clock"]),
        "config_hash": attrs["config_hash"],
        "benchmark_version": attrs["benchmark_version"],
        "rolling_version": attrs["rolling_version"],
        "n_input_rows": int(attrs.get("n_input_rows", len(dataset))),
        "n_dropped_rows": int(attrs.get("n_dropped_rows", 0)),
        "dropped_rows": list(attrs.get("dropped_rows", [])),
        "n_nan_feature_rows": int(attrs.get("n_nan_feature_rows", 0)),
        "nan_rows": list(attrs.get("nan_rows", [])),
        "nan_rule": attrs.get("nan_rule"),
        "causality": {
            "per_row_checked": bool(attrs.get("per_row_causality", False)),
            "rule": "每个决策行只依赖 available_through 及之前的数据（逐行扰动复核）",
        },
        "row_contract": [
            "available_through <= decision_as_of",
            "execution_date <= label_end_date",
            "label_available_after > label_end_date",
            "feature columns contain no NaN (unless nan_rule says otherwise)",
        ],
        "holdout_status": attrs.get("holdout_status", "researched_sample"),
        "holdout_note": attrs.get("holdout_note", _HOLDOUT_NOTE),
        "research_boundary": (
            "单一历史（2019–2025 已研究）；标签是基准市场净收益（含成本/票息/融资/滚动），"
            "不是策略超额收益；本 manifest 不含绩效结论"
        ),
    }
