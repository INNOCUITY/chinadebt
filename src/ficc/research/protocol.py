"""跨模型比较的共同实验协议（冻结版 ``protocol_v1_2026-11-08``）。

目的
----
ML（监督学习）与 RL 必须使用**同一信息时钟、同一目标定义、同一预测→仓位映射、
同一成本/风险/滚动政策**才能比较。本模块只定义协议与评估器，不训练模型、不选参；
模型研究（常数/Ridge/树/RL 多 seed）在协议冻结后另行开展。

冻结的关键选择
--------------
- **信息时钟**：中债曲线 t 日收盘估值，决策在 t 日收盘后、只能看到 ``≤ t−1``
  （``information_lag_days=1``）；执行 ``next_close``（t+1 收盘）。
- **持有期定义（显式选择）**：``holding_days_from_decision=5``——标签/目标窗口
  ``(t+1, t+5]`` 即执行后 **4 个交易日**收益。不混用"执行后再 5 日"的旧结果。
- **决策步长**：``decision_step_days=5``（ML 默认每 5 个交易日一个决策；默认样本
  相邻标签窗口不重叠；若改为每日决策必须重新验收重叠率）。
- **目标**：默认 ``market_benchmark_net_return``——同一滚动基准的净收益
  （成本/票息/融资/滚动入 NAV）。主动超额目标是**另一个**协议版本，不能与市场
  收益模型混比。
- **预测→仓位映射**：``prediction_to_target`` 把预测 z∈[−1,1] 映射为对基准 KRD
  的**同比例久期倾斜**：``target_krd[t] = bench_krd[t] × (1 + tilt_strength × z)``；
  ``tilt_strength=0.10``（最大 10% DV01 倾斜）。映射在样本外评估前固定，不按结果调参。
- **成本**：与规则策略完全相同的引擎账本（价差/手续费/融资/滚动），不另写成本模型。
- **样本标签**：2019–2025 为 ``researched_sample``，不是 untouched holdout。

使用
----
>>> protocol = ExperimentProtocol()
>>> target = prediction_to_target(0.5, ctx.benchmark, protocol=protocol)
>>> evaluation = evaluate_predictions(predictions, panel, funding, calendar, cfg, protocol)
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from ficc.backtest.engine import BacktestResult, run_backtest
from ficc.config import Config, get_config
from ficc.curve.curve import Curve
from ficc.data.schema import CANONICAL_TENORS
from ficc.logging import get_logger
from ficc.portfolio.benchmark import build_benchmark, build_universe_bonds
from ficc.portfolio.types import MarketContext, TargetFaces, TargetKRD
from ficc.utils.calendar import TradingCalendar

__all__ = [
    "ExperimentProtocol",
    "PredictionEvaluation",
    "PredictionStrategy",
    "ProtocolError",
    "ProtocolMismatchError",
    "evaluate_predictions",
    "evaluate_target_faces",
    "prediction_to_target",
    "prediction_to_z",
    "protocol_config",
    "protocol_fingerprint",
    "training_objective",
]

log = get_logger("research.protocol")

PROTOCOL_VERSION = "protocol_v1_2026-11-08"


class ProtocolError(ValueError):
    """协议使用错误（网格/目标/字段不符合冻结契约）。"""


class ProtocolMismatchError(ProtocolError):
    """实际经济配置与协议声明不一致（滚动政策/基准版本/阈值等）。"""


@dataclass(frozen=True)
class ExperimentProtocol:
    """共同实验协议（冻结；修改需升版本并重跑全部结果）。"""

    version: str = PROTOCOL_VERSION
    information_lag_days: int = 1
    execution: str = "next_close"
    holding_days_from_decision: int = 5
    decision_step_days: int = 5
    label_target: str = "market_benchmark_net_return"
    tilt_strength: float = 0.10
    z_scale: float = 0.01  # 原始预期收益（小数）→ z：z=clip(raw/z_scale,-1,1)
    terminal_fee_treatment: str = "deduct_terminal_fees"
    missing_prediction_policy: str = "hold"  # hold | benchmark_on_grid
    benchmark_version: str = "rolling"
    rolling_policy_version: str = "roll_v1_trigger1y_parissue"
    rolling_trigger_years: float = 1.0
    sample_tag: str = "researched_sample"
    cost_model: str = "engine_ledger_default"
    metadata: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.information_lag_days < 1:
            raise ValueError("information_lag_days 必须 >= 1")
        if self.holding_days_from_decision < 2:
            raise ValueError("holding_days_from_decision 必须 >= 2（执行后至少 1 日收益）")
        if self.decision_step_days < 1:
            raise ValueError("decision_step_days 必须 >= 1")
        if not (0.0 < self.tilt_strength <= 1.0):
            raise ValueError("tilt_strength 必须在 (0,1]")
        if self.execution != "next_close":
            raise ValueError(f"当前协议只支持 next_close，实际 {self.execution!r}")
        if self.label_target not in {
            "market_benchmark_net_return",
            "active_excess_vs_benchmark",
        }:
            raise ValueError(f"未知 label_target: {self.label_target!r}")
        if not (self.z_scale > 0.0):
            raise ValueError("z_scale 必须为正数")
        if self.missing_prediction_policy not in {"hold", "benchmark_on_grid"}:
            raise ValueError(f"未知 missing_prediction_policy: {self.missing_prediction_policy!r}")
        if self.terminal_fee_treatment not in {"deduct_terminal_fees", "gross_reward"}:
            raise ValueError(f"未知 terminal_fee_treatment: {self.terminal_fee_treatment!r}")
        if self.version == PROTOCOL_VERSION:
            frozen = {
                "label_target": "market_benchmark_net_return",
                "execution": "next_close",
                "holding_days_from_decision": 5,
                "decision_step_days": 5,
                "tilt_strength": 0.10,
                "benchmark_version": "rolling",
                "rolling_trigger_years": 1.0,
                "information_lag_days": 1,
                "z_scale": 0.01,
                "terminal_fee_treatment": "deduct_terminal_fees",
                "missing_prediction_policy": "hold",
            }
            for name, expected in frozen.items():
                actual = getattr(self, name)
                if actual != expected:
                    raise ProtocolError(
                        f"{PROTOCOL_VERSION} 冻结字段 {name}={actual!r}，期望 {expected!r}；"
                        "修改经济设定必须升协议版本并重跑全部结果"
                    )

    @property
    def return_window_trading_days(self) -> int:
        """标签/目标的实际收益窗口（决策+5、执行+1 → 4 个交易日）。"""
        return self.holding_days_from_decision - 1

    def decision_dates(self, calendar: TradingCalendar, start: date, end: date) -> list[date]:
        """协议决策网格：自 start 起每 ``decision_step_days`` 个交易日一个决策日。"""
        days = list(calendar.trading_days_between(start, end))
        if not days:
            raise ProtocolError(f"[{start}, {end}] 无交易日")
        return days[:: self.decision_step_days]

    def describe(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["return_window_trading_days"] = self.return_window_trading_days
        payload["clocks"] = {
            "ml": {
                "observation_lag_days": self.information_lag_days,
                "decision_step_days": self.decision_step_days,
                "execution": self.execution,
                "holding_days_from_decision": self.holding_days_from_decision,
                "label_available_after": (
                    f"decision + {self.holding_days_from_decision} 交易日"
                    f"（+{self.information_lag_days} 可得滞后）"
                ),
            },
            "rl": {
                "decision_frequency": "每个交易日收盘后（训练时可采样为 step 日）",
                "observation": "同一引擎 MarketContext（≤ as_of）",
                "execution": self.execution,
                "reward": "同一账本 NAV 的相邻估值日变化（价差只扣一次）",
            },
            "comparison_rule": (
                "跨模型比较时把所有策略采样到 decision_step_days 的同一决策日；"
                "RL 非决策日返回 hold（不主动交易）"
            ),
        }
        return payload


def _config_economics(cfg: Config) -> dict[str, Any]:
    """协议指纹包含的**完整经济配置**（审查 R60：费率/资金/杠杆/风险限制均须入指纹）。"""
    return {
        "backtest": {
            "init_cash": float(cfg.backtest.init_cash),
            "max_leverage": float(cfg.backtest.max_leverage),
            "rebalance": str(cfg.backtest.rebalance),
            "benchmark_version": str(cfg.backtest.benchmark_version),
            "annual_days": int(cfg.backtest.annual_days),
            "accrual_days": int(cfg.backtest.accrual_days),
            "roll_down_step_years": float(cfg.backtest.roll_down_step_years),
        },
        "portfolio": cfg.portfolio.model_dump(mode="json"),
        "constraints": cfg.constraints.model_dump(mode="json"),
        "costs": cfg.costs.model_dump(mode="json"),
        "risk": cfg.risk.model_dump(mode="json"),
        "benchmark": cfg.benchmark.model_dump(mode="json"),
        "curve": cfg.curve.model_dump(mode="json"),
        "pricing": cfg.pricing.model_dump(mode="json"),
    }


def protocol_fingerprint(
    cfg: Config | None = None, protocol: ExperimentProtocol | None = None
) -> str:
    """协议 + 实际经济配置的指纹（配置变化后指纹必须变化）。"""
    proto = protocol or ExperimentProtocol()
    resolved = get_config() if cfg is None else cfg
    payload = {
        "protocol": proto.describe(),
        "economics": _config_economics(resolved),
    }
    blob = json.dumps(payload, sort_keys=True, ensure_ascii=False, default=str)
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def protocol_config(
    cfg: Config | None = None, protocol: ExperimentProtocol | None = None
) -> Config:
    """把协议要求的 rolling 基准/日频调仓写入配置副本（不改全局配置）。"""
    resolved = get_config() if cfg is None else cfg
    proto = protocol or ExperimentProtocol()
    rolling = resolved.portfolio.rolling.model_copy(
        update={
            "enabled": True,
            "roll_trigger_years": proto.rolling_trigger_years,
            "version": proto.rolling_policy_version,
        }
    )
    aligned = resolved.model_copy(
        update={
            "portfolio": resolved.portfolio.model_copy(update={"rolling": rolling}),
            "backtest": resolved.backtest.model_copy(
                update={
                    "benchmark_version": proto.benchmark_version,
                    "rebalance": "daily",
                }
            ),
        }
    )
    # 实际配置必须与声明一致（不能只改一个标志就宣称冻结）
    if aligned.backtest.benchmark_version != proto.benchmark_version:
        raise ProtocolMismatchError("基准版本与协议不一致")
    if abs(aligned.portfolio.rolling.roll_trigger_years - proto.rolling_trigger_years) > 1e-12:
        raise ProtocolMismatchError(
            f"滚动触发={aligned.portfolio.rolling.roll_trigger_years} 与协议 "
            f"{proto.rolling_trigger_years} 不一致"
        )
    return aligned


def prediction_to_z(raw_prediction: float, protocol: ExperimentProtocol | None = None) -> float:
    """原始收益预测（小数）→ 冻结的 z∈[−1,1]：``clip(raw/z_scale, −1, 1)``。

    例：z_scale=0.01 表示预测 +1% 收益 → z=+1（最大 10% DV01 倾斜）。该尺度属于协议，
    不得按验证集结果调整；模型训练目标应输出与 z_scale 同单位的收益预测。
    """
    proto = protocol or ExperimentProtocol()
    if not np.isfinite(raw_prediction):
        raise ValueError(f"raw_prediction 必须是有限数：{raw_prediction!r}")
    return float(np.clip(float(raw_prediction) / proto.z_scale, -1.0, 1.0))


def training_objective(
    total_ledger_reward: float,
    accrued_terminal_fees: float,
    protocol: ExperimentProtocol | None = None,
) -> float:
    """训练回报的最终口径：账本 NAV 累计 reward 扣除终止未付费用（协议冻结）。"""
    proto = protocol or ExperimentProtocol()
    if proto.terminal_fee_treatment == "gross_reward":
        return float(total_ledger_reward)
    return float(total_ledger_reward) - float(accrued_terminal_fees)


def prediction_to_target(
    prediction: float,
    benchmark: Any,
    *,
    protocol: ExperimentProtocol | None = None,
    as_of: date | None = None,
) -> TargetKRD:
    """固定映射：z∈[−1,1] → 基准 KRD 的同比例久期倾斜。

    ``target_krd[t] = bench_krd[t] × (1 + tilt_strength × z)``；z=0 即回到基准，
    z=+1 最大加久期 10%，z=−1 最大减久期 10%。映射在样本外评估前冻结。
    """
    proto = protocol or ExperimentProtocol()
    if not np.isfinite(prediction):
        raise ValueError(f"prediction 必须是有限数：{prediction!r}")
    z = float(np.clip(prediction, -1.0, 1.0))
    factor = 1.0 + proto.tilt_strength * z
    krd = {float(k): float(v) * factor for k, v in benchmark.krd.items()}
    day = as_of if as_of is not None else getattr(benchmark, "as_of", None)
    if day is None:
        raise ValueError("prediction_to_target 需要 as_of 或 benchmark.as_of")
    return TargetKRD(
        as_of=day,
        krd=krd,
        notes=f"{proto.version}: prediction={z:+.6f}, factor={factor:.6f}",
    )


class PredictionStrategy:
    """按决策日预测值生成目标 KRD；非决策日返回 None（hold，不主动交易）。"""

    name = "protocol_predictions"

    def __init__(
        self,
        predictions: Mapping[date, float],
        *,
        protocol: ExperimentProtocol,
        prediction_is_z: bool = False,
        decision_grid: set[date] | None = None,
        benchmark_krd_history: Mapping[date, Mapping[float, float]] | None = None,
        calendar: TradingCalendar | None = None,
        initial_benchmark_as_of: date | None = None,
    ) -> None:
        self._predictions = {pd.Timestamp(k).date(): float(v) for k, v in predictions.items()}
        self._protocol = protocol
        self._prediction_is_z = bool(prediction_is_z)
        self._decision_grid = set(decision_grid) if decision_grid is not None else None
        self._benchmark_krd_history = (
            {
                pd.Timestamp(k).date(): {float(t): float(v) for t, v in krd.items()}
                for k, krd in benchmark_krd_history.items()
            }
            if benchmark_krd_history is not None
            else None
        )
        self._calendar = calendar
        self._lag = int(protocol.information_lag_days)
        self._seen_benchmark: dict[date, dict[float, float]] = {}
        self._initial_benchmark_as_of = initial_benchmark_as_of

    def _lagged_benchmark(self, ctx: MarketContext) -> Any:
        """R72：与 RL 相同，使用决策日 t−lag 的基准 KRD，而不是当日 ctx.benchmark。"""
        # 先记录当日基准（用于后续 t−lag 查找）；映射本身绝不回看当日。
        self._seen_benchmark[ctx.as_of] = {float(t): float(v) for t, v in ctx.benchmark.krd.items()}
        if self._calendar is None:
            return ctx.benchmark
        available = self._calendar.prev_trading_day(ctx.as_of, self._lag)
        krd = None
        if self._benchmark_krd_history is not None:
            krd = self._benchmark_krd_history.get(available)
        if krd is None:
            krd = self._seen_benchmark.get(available)
        if (
            krd is None
            and self._initial_benchmark_as_of is not None
            and self._benchmark_krd_history is not None
            and self._decision_grid is not None
            and ctx.as_of == min(self._decision_grid)
        ):
            # 显式/默认初始化契约：首决策允许使用**初始条款日**的基准 KRD（不晚于 start−lag）。
            krd = self._benchmark_krd_history.get(self._initial_benchmark_as_of)
            available = self._initial_benchmark_as_of
        if krd is None:
            return None
        from types import SimpleNamespace

        return SimpleNamespace(krd=krd, as_of=available)

    def target_krd(self, ctx: MarketContext) -> TargetKRD | None:
        prediction = self._predictions.get(ctx.as_of)
        if prediction is None:
            # R68：只有公共决策网格日允许"缺预测→零目标"；非网格日必须 hold。
            if (
                self._protocol.missing_prediction_policy != "benchmark_on_grid"
                or self._decision_grid is None
                or ctx.as_of not in self._decision_grid
            ):
                return None
            prediction = 0.0
        z = (
            float(np.clip(prediction, -1.0, 1.0))
            if self._prediction_is_z
            else prediction_to_z(prediction, self._protocol)
        )
        benchmark = self._lagged_benchmark(ctx)
        if benchmark is None:
            return None
        return prediction_to_target(z, benchmark, protocol=self._protocol, as_of=ctx.as_of)


class FixedFaceStrategy:
    """跨入口控制实验：在协议网格日返回**预先固定**的逐券目标面值，其余日 hold。"""

    name = "protocol_fixed_faces"

    def __init__(self, targets: Mapping[date, Mapping[str, float]]) -> None:
        self._targets = {
            pd.Timestamp(k).date(): {str(c): float(v) for c, v in faces.items()}
            for k, faces in targets.items()
        }

    def target_krd(self, ctx: MarketContext) -> Any:
        faces = self._targets.get(ctx.as_of)
        if faces is None:
            return None
        return TargetFaces(as_of=ctx.as_of, faces=dict(faces), notes="cross_entry_fixed_faces")


def _evaluation_metrics(
    result: BacktestResult,
    *,
    n_decisions: int,
    protocol: ExperimentProtocol,
    n_rejected: int | None = None,
) -> dict[str, float]:
    nav = result.nav
    bench = result.benchmark_nav
    active = nav.pct_change().dropna() - bench.pct_change().dropna()
    ann = int(getattr(result, "annual_days", 250) or 250)
    bench_used = bench
    total_ret = float(nav.iloc[-1] / nav.iloc[0] - 1.0)
    bench_ret = float(bench_used.iloc[-1] / bench_used.iloc[0] - 1.0)
    held = result.constraints[result.constraints["n_positions"] > 0]
    return {
        "n_days": float(len(nav)),
        "total_return": total_ret,
        "benchmark_total_return": bench_ret,
        "active_excess_bp": float((total_ret - bench_ret) * 1e4),
        "tracking_error_bp": float(active.std(ddof=0) * np.sqrt(ann) * 1e4)
        if len(active) > 1
        else float("nan"),
        "max_drawdown": float((nav / nav.cummax() - 1.0).min()),
        "total_fee": float(result.costs["fee"].sum()),
        "total_funding": float(result.costs["funding"].sum()),
        "total_spread": float(result.costs["spread"].sum()),
        "total_cost": float(result.costs["total"].sum()),
        "total_turnover": float(result.turnover.sum()),
        "n_trades": float(len(result.trades)),
        "n_rejected": float(len(result.rejected) if n_rejected is None else int(n_rejected)),
        "reject_rate_per_decision": float(
            (len(result.rejected) if n_rejected is None else int(n_rejected)) / max(n_decisions, 1)
        ),
        "avg_abs_dv01_ratio_dev": float((result.exposure["dv01_ratio"] - 1.0).abs().mean()),
        "violation_days_held": float((~held["ok"]).sum()),
        "n_decisions": float(n_decisions),
    }


@dataclass(frozen=True)
class PredictionEvaluation:
    """同一引擎/成本/基准下的预测→组合评估结果。"""

    protocol: dict[str, Any]
    metrics: dict[str, float]
    result: BacktestResult


def _available_initial_curve(
    panel: pd.DataFrame,
    calendar: TradingCalendar,
    start: date,
    lag: int,
) -> Curve:
    """默认初始化：只使用 start−lag 已可得且期限完整的曲线；缺历史直接拒绝（不向未来补）。"""
    available = calendar.prev_trading_day(start, lag)
    row = panel.loc[pd.Timestamp(available)].reindex(list(CANONICAL_TENORS))
    if row.isna().any():
        raise ProtocolError(
            f"start={start} 前第 {lag} 个交易日 {available} 曲线缺期限 "
            f"{row[row.isna()].index.tolist()}；请推迟 start 或显式传入可得 initial_curve"
        )
    return Curve(
        as_of=available,
        tenors=np.asarray(sorted(CANONICAL_TENORS), dtype=float),
        rates=row.to_numpy(dtype=float),
        method="linear",
    )


def _anchor_benchmark_krd_history(
    panel: pd.DataFrame,
    funding: pd.Series | None,
    calendar: TradingCalendar,
    cfg: Config,
    *,
    start: date,
    end: date,
    initial_curve: Curve | None,
) -> dict[date, dict[float, float]]:
    """R72：从真实基准账本导出逐日基准 KRD，供 ML 使用 t−lag as-of 映射。

    该 anchor 与评估引擎使用相同起点/初始条款；不读取预测日当日基准作为映射输入。
    """

    class _Hold:
        name = "protocol_anchor"

        def target_krd(self, ctx: MarketContext) -> None:
            return None

    terms_day = initial_curve.as_of if initial_curve is not None else start
    anchor_start = terms_day
    result = run_backtest(
        _Hold(),
        panel=panel,
        calendar=calendar,
        funding=funding,
        cfg=cfg,
        start=anchor_start,
        end=end,
        initial_curve=initial_curve,
    )
    history: dict[date, dict[float, float]] = {}
    # 用 initial_curve 或 start 曲线构造初始基准 KRD，供首个决策日映射使用。
    try:
        rates = panel.loc[pd.Timestamp(terms_day)].reindex(list(CANONICAL_TENORS))
        if not rates.isna().any():
            terms_curve = Curve(
                as_of=terms_day,
                tenors=np.asarray(sorted(CANONICAL_TENORS), dtype=float),
                rates=rates.to_numpy(dtype=float),
                method="linear",
            )
            bonds = build_universe_bonds(terms_curve, terms_day, cfg=cfg)
            bench = build_benchmark(terms_curve, terms_day, bonds=bonds, calendar=calendar, cfg=cfg)
            history[terms_day] = {float(t): float(v) for t, v in bench.krd.items()}
    except (KeyError, ValueError):
        pass
    exposure = result.exposure
    if "date" not in exposure.columns:
        return history
    for _, row in exposure.iterrows():
        krd = {
            float(column[len("bench_krd_") :]): float(row[column])
            for column in exposure.columns
            if isinstance(column, str) and column.startswith("bench_krd_")
        }
        history[pd.Timestamp(row["date"]).date()] = krd
    return history


def evaluate_predictions(
    predictions: Mapping[date, float],
    panel: pd.DataFrame,
    funding: pd.Series | None,
    calendar: TradingCalendar,
    cfg: Config | None = None,
    *,
    protocol: ExperimentProtocol | None = None,
    start: date | None = None,
    end: date | None = None,
    prediction_is_z: bool = False,
    initial_curve: Curve | None = None,
    benchmark_krd_history: Mapping[date, Mapping[float, float]] | None = None,
) -> PredictionEvaluation:
    """用**同一回测引擎**把预测映射成仓位，评估成本后净收益与风险。

    这是协议评估器，不是模型研究：输入是已冻结的预测序列；输出净收益、超额、
    TE、成本、换手、暴露与拒单率。价差/费用/融资只由引擎账本扣一次。
    """
    proto = protocol or ExperimentProtocol()
    resolved = protocol_config(cfg, proto)
    decision_start = start or resolved.backtest.start_date
    decision_end = end or (resolved.backtest.end_date or panel.index.max().date())
    grid = proto.decision_dates(calendar, decision_start, decision_end)
    supplied = {pd.Timestamp(k).date() for k in predictions}
    off_grid = sorted(supplied - set(grid))
    if off_grid:
        raise ProtocolError(
            f"{len(off_grid)} 个预测日期不在协议决策网格（每 {proto.decision_step_days} 交易日）上，"
            f"例如 {off_grid[:5]}；连续/每日预测不能绕过公共入口的网格约束"
        )
    if initial_curve is None:
        initial_curve = _available_initial_curve(
            panel, calendar, decision_start, int(proto.information_lag_days)
        )
    if benchmark_krd_history is None:
        benchmark_krd_history = _anchor_benchmark_krd_history(
            panel,
            funding,
            calendar,
            resolved,
            start=initial_curve.as_of,
            end=decision_end,
            initial_curve=initial_curve,
        )
    strategy = PredictionStrategy(
        predictions,
        protocol=proto,
        prediction_is_z=prediction_is_z,
        decision_grid=set(grid),
        benchmark_krd_history=benchmark_krd_history,
        calendar=calendar,
        initial_benchmark_as_of=initial_curve.as_of,
    )
    result = run_backtest(
        strategy,
        panel=panel,
        calendar=calendar,
        funding=funding,
        cfg=resolved,
        start=start,
        end=end,
        initial_curve=initial_curve,
    )
    nav = result.nav
    bench = result.benchmark_nav
    active = nav.pct_change().dropna() - bench.pct_change().dropna()
    ann = resolved.backtest.annual_days
    bench_used = bench
    total_ret = float(nav.iloc[-1] / nav.iloc[0] - 1.0)
    bench_ret = float(bench_used.iloc[-1] / bench_used.iloc[0] - 1.0)
    held = result.constraints[result.constraints["n_positions"] > 0]
    from ficc.rl.env import benchmark_terminal_state_from_result, terminal_state_from_result

    terminal = terminal_state_from_result(result, calendar=calendar, cfg=resolved)
    bench_terminal = benchmark_terminal_state_from_result(result, calendar=calendar, cfg=resolved)
    metrics = {
        "n_days": float(len(nav)),
        "total_return": total_ret,
        "benchmark_total_return": bench_ret,
        "active_excess_bp": float((total_ret - bench_ret) * 1e4),
        "tracking_error_bp": float(active.std(ddof=0) * np.sqrt(ann) * 1e4)
        if len(active) > 1
        else float("nan"),
        "max_drawdown": float((nav / nav.cummax() - 1.0).min()),
        "total_fee": float(result.costs["fee"].sum()),
        "total_funding": float(result.costs["funding"].sum()),
        "total_spread": float(result.costs["spread"].sum()),
        "total_cost": float(result.costs["total"].sum()),
        "total_turnover": float(result.turnover.sum()),
        "n_trades": float(len(result.trades)),
        "n_rejected": float(len(result.rejected)),
        "reject_rate_per_decision": float(len(result.rejected) / max(len(predictions), 1)),
        "avg_abs_dv01_ratio_dev": float((result.exposure["dv01_ratio"] - 1.0).abs().mean()),
        "violation_days_held": float((~held["ok"]).sum()),
        "n_decisions": float(
            len(result.signals[result.signals["signal_date"].notna()])
            if "signal_date" in result.signals
            else 0
        ),
        "n_hold_days": float(
            len(result.signals[result.signals["signal_date"].isna()])
            if "signal_date" in result.signals
            else 0
        ),
        "n_input_predictions": float(len(predictions)),
        # R66：终止义务与净终值口径（与 training_objective 的 deduct_terminal_fees 一致）
        "terminal_nav": float(terminal.nav_mark_to_market),
        "terminal_accrued_fees": float(terminal.accrued_fees),
        "terminal_unsettled_cash": float(terminal.unsettled_cash),
        "net_terminal_nav": float(terminal.net_terminal_value),
        "net_terminal_return": float(terminal.net_terminal_value / nav.iloc[0] - 1.0)
        if abs(nav.iloc[0]) > 0
        else float("nan"),
        "benchmark_terminal_accrued_fees": float(bench_terminal.accrued_fees),
        "benchmark_terminal_unsettled_cash": float(bench_terminal.unsettled_cash),
        "net_benchmark_return": float(
            (bench.iloc[-1] - bench_terminal.accrued_fees) / bench.iloc[0] - 1.0
        )
        if abs(bench.iloc[0]) > 0
        else float("nan"),
        "net_active_excess_bp": float(
            (
                terminal.net_terminal_value / nav.iloc[0]
                - (bench.iloc[-1] - bench_terminal.accrued_fees) / bench.iloc[0]
            )
            * 1e4
        )
        if abs(nav.iloc[0]) > 0 and abs(bench.iloc[0]) > 0
        else float("nan"),
    }
    return PredictionEvaluation(
        protocol={
            **proto.describe(),
            "fingerprint": protocol_fingerprint(resolved, proto),
        },
        metrics=metrics,
        result=result,
    )


def evaluate_target_faces(
    targets: Mapping[date, Mapping[str, float]],
    panel: pd.DataFrame,
    funding: pd.Series | None,
    calendar: TradingCalendar,
    cfg: Config | None = None,
    *,
    protocol: ExperimentProtocol | None = None,
    start: date | None = None,
    end: date | None = None,
    initial_curve: Curve | None = None,
) -> PredictionEvaluation:
    """跨入口控制：把**固定目标面值**经同一引擎/成本口径跑成组合轨迹。"""
    proto = protocol or ExperimentProtocol()
    resolved = protocol_config(cfg, proto)
    decision_start = start or resolved.backtest.start_date
    decision_end = end or (resolved.backtest.end_date or panel.index.max().date())
    grid = set(proto.decision_dates(calendar, decision_start, decision_end))
    supplied = {pd.Timestamp(k).date() for k in targets}
    off_grid = sorted(supplied - grid)
    if off_grid:
        raise ProtocolError(f"{len(off_grid)} 个目标日期不在协议决策网格上，例如 {off_grid[:5]}")
    result = run_backtest(
        FixedFaceStrategy(targets),
        panel=panel,
        calendar=calendar,
        funding=funding,
        cfg=resolved,
        start=decision_start,
        end=decision_end,
        initial_curve=initial_curve,
    )
    return PredictionEvaluation(
        protocol={**proto.describe(), "fingerprint": protocol_fingerprint(resolved, proto)},
        metrics=_evaluation_metrics(result, n_decisions=len(targets), protocol=proto),
        result=result,
    )
