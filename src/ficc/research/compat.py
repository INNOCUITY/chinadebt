"""引擎能力探测：``backtest.benchmark_version``（buyhold / rolling）兼容层。

背景
----
滚动基准（``portfolio.rolling`` + ``backtest.benchmark_version``）由 Lead 并行实现，
本研究的脚本必须**不修改引擎**即可适配。适配顺序：

1. 若 ``run_backtest`` 的签名带 ``benchmark_version`` 参数 → 直接作为关键字传入；
2. 否则若 ``cfg.backtest`` 已有 ``benchmark_version`` 字段 → 用配置覆盖（``model_copy``）；
3. 否则滚动基准尚不可用：请求 ``rolling`` 时**明确告警并回退 buyhold**（输出中
   记录实际生效版本），绝不静默假装用了滚动基准。
"""

from __future__ import annotations

import inspect
from dataclasses import dataclass, field
from typing import Any

from ficc.backtest.engine import run_backtest
from ficc.config import Config

__all__ = ["BENCHMARK_VERSIONS", "BenchmarkResolution", "resolve_benchmark_version"]

BENCHMARK_VERSIONS: tuple[str, ...] = ("buyhold", "rolling")


@dataclass(frozen=True)
class BenchmarkResolution:
    """一次基准版本解析结果（实际生效版本 + 引擎调用参数 + 披露说明）。"""

    cfg: Config
    requested: str
    effective: str
    run_kwargs: dict[str, Any] = field(default_factory=dict)
    note: str = ""
    rolling_version: str = ""

    @property
    def fell_back(self) -> bool:
        return self.requested != self.effective


def _run_backtest_parameters() -> dict[str, inspect.Parameter]:
    return dict(inspect.signature(run_backtest).parameters)


def _engine_mentions_benchmark_version() -> bool:
    """引擎源码是否出现 ``benchmark_version``（用于确认配置字段真的被消费）。"""
    module = inspect.getmodule(run_backtest)
    try:
        source = inspect.getsource(module) if module is not None else ""
    except (OSError, TypeError):
        return False
    return "benchmark_version" in source


def resolve_benchmark_version(cfg: Config, requested: str = "buyhold") -> BenchmarkResolution:
    """把请求的 ``benchmark_version`` 解析成引擎可执行的配置/参数。

    Parameters
    ----------
    cfg : Config
        冻结主配置（不得修改；本函数只在内存里 ``model_copy``）。
    requested : {"buyhold", "rolling"}
        请求的基准版本。

    Returns
    -------
    BenchmarkResolution
        ``cfg`` 为实际使用的配置；``run_kwargs`` 需原样传给 ``run_backtest``；
        ``effective`` 为实际生效版本（滚动尚不可用时回退为 ``buyhold`` 并给出
        ``note``）。调用方必须把 ``effective`` 与 ``note`` 写进产物。
    """
    if requested not in BENCHMARK_VERSIONS:
        raise ValueError(f"benchmark_version 必须是 {BENCHMARK_VERSIONS}，实际 {requested!r}")

    parameters = _run_backtest_parameters()
    if "benchmark_version" in parameters:
        return BenchmarkResolution(
            cfg=cfg,
            requested=requested,
            effective=requested,
            run_kwargs={"benchmark_version": requested},
            note="引擎签名直接支持 benchmark_version",
        )
    if hasattr(cfg.backtest, "benchmark_version") and _engine_mentions_benchmark_version():
        updated = cfg
        note = "通过 cfg.backtest.benchmark_version 生效"
        rolling_version = ""
        rolling = getattr(getattr(cfg, "portfolio", None), "rolling", None)
        if rolling is not None and hasattr(rolling, "enabled"):
            # 滚动基准必须同时开启滚动政策（引擎校验）；buyhold 时显式关闭，避免配置残留
            enabled = requested == "rolling"
            updated = updated.model_copy(
                update={
                    "portfolio": cfg.portfolio.model_copy(
                        update={"rolling": rolling.model_copy(update={"enabled": enabled})}
                    )
                }
            )
            note += f"；portfolio.rolling.enabled={enabled}"
            if enabled:
                rolling_version = str(getattr(rolling, "version", ""))
                note += f"（滚动政策 {rolling_version}）"
        updated = updated.model_copy(
            update={
                "backtest": updated.backtest.model_copy(update={"benchmark_version": requested})
            }
        )
        return BenchmarkResolution(
            cfg=updated,
            requested=requested,
            effective=requested,
            run_kwargs={},
            note=note,
            rolling_version=rolling_version,
        )
    if requested == "buyhold":
        return BenchmarkResolution(
            cfg=cfg,
            requested=requested,
            effective="buyhold",
            run_kwargs={},
            note="当前引擎只有买持基准（默认口径）",
        )
    return BenchmarkResolution(
        cfg=cfg,
        requested=requested,
        effective="buyhold",
        run_kwargs={},
        note=(
            "引擎/配置尚未提供 benchmark_version=rolling（run_backtest 无该参数，"
            "cfg.backtest 无该字段）；本次按 buyhold 运行，不改引擎"
        ),
    )
