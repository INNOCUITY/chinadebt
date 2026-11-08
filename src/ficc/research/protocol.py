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

from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from ficc.backtest.engine import BacktestResult, run_backtest
from ficc.config import Config, get_config
from ficc.logging import get_logger
from ficc.portfolio.types import MarketContext, TargetKRD
from ficc.utils.calendar import TradingCalendar

__all__ = [
    "ExperimentProtocol",
    "PredictionEvaluation",
    "PredictionStrategy",
    "evaluate_predictions",
    "prediction_to_target",
    "protocol_config",
]

log = get_logger("research.protocol")

PROTOCOL_VERSION = "protocol_v1_2026-11-08"


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
    benchmark_version: str = "rolling"
    rolling_policy_version: str = "roll_v1_trigger1y_parissue"
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

    @property
    def return_window_trading_days(self) -> int:
        """标签/目标的实际收益窗口（决策+5、执行+1 → 4 个交易日）。"""
        return self.holding_days_from_decision - 1

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


def protocol_config(
    cfg: Config | None = None, protocol: ExperimentProtocol | None = None
) -> Config:
    """把协议要求的 rolling 基准/日频调仓写入配置副本（不改全局配置）。"""
    resolved = get_config() if cfg is None else cfg
    proto = protocol or ExperimentProtocol()
    rolling = resolved.portfolio.rolling.model_copy(update={"enabled": True})
    del proto  # 协议版本已写入 describe；此函数只负责配置对齐
    return resolved.model_copy(
        update={
            "portfolio": resolved.portfolio.model_copy(update={"rolling": rolling}),
            "backtest": resolved.backtest.model_copy(
                update={
                    "benchmark_version": "rolling",
                    "rebalance": "daily",
                }
            ),
        }
    )


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
    ) -> None:
        self._predictions = {pd.Timestamp(k).date(): float(v) for k, v in predictions.items()}
        self._protocol = protocol

    def target_krd(self, ctx: MarketContext) -> TargetKRD | None:
        prediction = self._predictions.get(ctx.as_of)
        if prediction is None:
            return None
        return prediction_to_target(
            prediction, ctx.benchmark, protocol=self._protocol, as_of=ctx.as_of
        )


@dataclass(frozen=True)
class PredictionEvaluation:
    """同一引擎/成本/基准下的预测→组合评估结果。"""

    protocol: dict[str, Any]
    metrics: dict[str, float]
    result: BacktestResult


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
) -> PredictionEvaluation:
    """用**同一回测引擎**把预测映射成仓位，评估成本后净收益与风险。

    这是协议评估器，不是模型研究：输入是已冻结的预测序列；输出净收益、超额、
    TE、成本、换手、暴露与拒单率。价差/费用/融资只由引擎账本扣一次。
    """
    proto = protocol or ExperimentProtocol()
    resolved = protocol_config(cfg, proto)
    strategy = PredictionStrategy(predictions, protocol=proto)
    result = run_backtest(
        strategy,
        panel=panel,
        calendar=calendar,
        funding=funding,
        cfg=resolved,
        start=start,
        end=end,
    )
    nav = result.nav
    bench = result.benchmark_nav
    active = nav.pct_change().dropna() - bench.pct_change().dropna()
    ann = resolved.backtest.annual_days
    total_ret = float(nav.iloc[-1] / nav.iloc[0] - 1.0)
    bench_ret = float(bench.iloc[-1] / bench.iloc[0] - 1.0)
    held = result.constraints[result.constraints["n_positions"] > 0]
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
        "n_decisions": float(len(predictions)),
    }
    return PredictionEvaluation(protocol=proto.describe(), metrics=metrics, result=result)
