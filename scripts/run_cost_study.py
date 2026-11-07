#!/usr/bin/env python3
"""成本对照实验（§12.10 任务 A）：固定路径回放 + 决策变化 + 参数敏感性。

三组实验
--------
1. **固定成交路径的成本分解**（``reports/cost_study_fixed_path.csv``）

   先跑一次基线引擎得到 ``trades``，然后**只用同一批成交**重放账本，分别关闭
   ``spread`` / ``fee`` / ``funding``（见 :mod:`ficc.backtest.replay`）。策略与
   基准（买入持有账本）都按同一开关重放，因此可以回答"纯执行成本"对 NAV 和
   相对基准超额的贡献，不掺杂策略重新决策。

2. **允许重新决策的成本敏感性**（``reports/cost_study_decision_change.csv``）

   把三类成本分别置零后**重跑引擎**（``spread_bp_*`` / ``fee_bp+结算费`` /
   融资利率）。对照第 1 组可把总成本拆成"执行成本"与"行为反馈（换手/成交笔数
   变化）"。

3. **参数敏感性**（``reports/cost_sensitivity.csv``）

   - ``constraints.min_trade_lot`` ∈ {1e6, 5e6}
   - ``constraints.rebalance_band_lots`` ∈ {0, 2, 4}
   - ``backtest.rebalance`` ∈ {daily, weekly}（引擎未实现 weekly 时跳过并注明）
   - 价差乘数 ∈ {0, 0.5, 1, 2}（3 个 ``spread_bp_*`` 同乘）

   改变决策的变体用**引擎重跑**；价差乘数额外给一组**固定路径回放**（把
   ``clean_price`` 按 ``mid + m×(exec−mid)`` 重标），用于区分执行成本与行为反馈。

用法::

    PYTHONPATH=src .venv/bin/python scripts/run_cost_study.py
    PYTHONPATH=src .venv/bin/python scripts/run_cost_study.py --quick    # 1 策略/短窗口
    PYTHONPATH=src .venv/bin/python scripts/run_cost_study.py --start 2019-01-28 --end 2020-12-31

产物列名即对外契约（测试会断言列集合稳定），不要随意删列。
"""

from __future__ import annotations

import argparse
import inspect
import sys
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Any

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
_SCRIPTS = _ROOT / "scripts"
for _path in (_SRC, _SCRIPTS):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from run_strategies import (  # noqa: E402
    CarryRollStrategy,
    RelativeValueStrategy,
    SteepenerStrategy,
    ZeroTiltStrategy,
    load_market,
)

from ficc.backtest.engine import BacktestResult, run_backtest  # noqa: E402
from ficc.backtest.replay import (  # noqa: E402
    CostSwitches,
    ReplayResult,
    replay_engine_result,
    replay_trades,
)
from ficc.config import Config, get_config  # noqa: E402
from ficc.curve.curve import Curve  # noqa: E402
from ficc.curve.fitting import fit_panel  # noqa: E402
from ficc.data.schema import CANONICAL_TENORS  # noqa: E402
from ficc.portfolio.benchmark import build_universe_bonds  # noqa: E402
from ficc.utils.calendar import TradingCalendar  # noqa: E402

DEFAULT_START = date(2019, 1, 28)
DEFAULT_END = date(2020, 12, 31)
QUICK_START = date(2019, 1, 28)
QUICK_END = date(2019, 4, 30)

STRATEGY_ORDER = ("zero_tilt", "carry_roll", "2s10s", "rv")

FIXED_PATH_SCENARIOS: tuple[tuple[str, CostSwitches], ...] = (
    ("baseline", CostSwitches()),
    ("no_spread", CostSwitches(spread=False)),
    ("no_fee", CostSwitches(fee=False)),
    ("no_funding", CostSwitches(funding=False)),
    ("all_off", CostSwitches(spread=False, fee=False, funding=False)),
)

DECISION_SCENARIOS: tuple[str, ...] = (
    "engine_no_spread",
    "engine_no_fee",
    "engine_no_funding",
    "engine_all_off",
)

FIXED_PATH_COLUMNS: tuple[str, ...] = (
    "strategy",
    "scenario",
    "switches",
    "start",
    "end",
    "config_hash",
    "final_nav",
    "total_return",
    "benchmark_final_nav",
    "excess_bp",
    "total_fee",
    "total_funding",
    "total_spread",
    "total_cost",
    "nav_impact_cny",
    "nav_impact_bp",
    "excess_change_bp",
    "n_trades",
)

DECISION_COLUMNS: tuple[str, ...] = (
    "strategy",
    "scenario",
    "start",
    "end",
    "config_hash",
    "final_nav",
    "total_return",
    "benchmark_final_nav",
    "excess_bp",
    "total_fee",
    "total_funding",
    "total_spread",
    "total_cost",
    "nav_impact_cny",
    "nav_impact_bp",
    "excess_change_bp",
    "n_trades",
    "n_rejected",
    "total_turnover",
    "n_violation_days",
)

SENSITIVITY_COLUMNS: tuple[str, ...] = (
    "strategy",
    "variant",
    "value",
    "path",
    "start",
    "end",
    "config_hash",
    "total_return",
    "excess_bp",
    "total_fee",
    "total_funding",
    "total_spread",
    "total_cost",
    "n_trades",
    "total_turnover",
    "n_violation_days",
    "status",
)


# ---------------------------------------------------------------------------
# 策略与数据
# ---------------------------------------------------------------------------
def _residual_panel(panel: pd.DataFrame, cfg: Config, end: date) -> pd.DataFrame:
    """RV 策略用的 NSS 残差面板（观测 − 拟合）。

    与 ``scripts/run_strategies.py`` 同口径；只拟合到 ``end``（逐日横截面独立拟合，
    截断未来数据不影响 ≤end 的残差，同时更快且更符合"不读未来"）。
    """
    _params, residual_bp = fit_panel(
        panel.loc[: pd.Timestamp(end)],
        method=cfg.curve.fit_method,
        weights=cfg.curve.weights,
        param_mode="free",
        residuals_mode="free",
    )
    residual_bp.columns = [float(column) for column in residual_bp.columns]
    return -residual_bp


def build_strategy_factories(
    names: list[str], panel: pd.DataFrame, cfg: Config, end: date
) -> list[tuple[str, Callable[[], Any]]]:
    """按名称返回**策略工厂**（复用 run_strategies 的构造与 RV 残差预计算）。

    必须每次引擎运行都新建策略实例：``SteepenerStrategy`` 等策略带持仓状态
    （``_direction``），复用同一实例会让后一次回测继承上一次的方向，污染对照。
    """
    residuals: pd.DataFrame | None = None
    if "rv" in names:
        residuals = _residual_panel(panel, cfg, end)
    factories: dict[str, Callable[[], Any]] = {
        "zero_tilt": ZeroTiltStrategy,
        "carry_roll": CarryRollStrategy,
        "2s10s": SteepenerStrategy,
        "rv": lambda: RelativeValueStrategy(residuals=residuals),
    }
    return [(name, factories[name]) for name in names]


# ---------------------------------------------------------------------------
# 配置变体
# ---------------------------------------------------------------------------
def _with_costs(
    cfg: Config,
    *,
    spread: bool = True,
    fee: bool = True,
    funding: bool = True,
    spread_multiplier: float = 1.0,
) -> Config:
    """按开关生成成本配置变体（仅改成本参数，其它口径不动）。"""
    costs = cfg.costs
    if not spread:
        costs = costs.model_copy(
            update={
                "spread_bp_active": 0.0,
                "spread_bp_illiquid": 0.0,
                "spread_bp_ultra_long": 0.0,
            }
        )
    elif spread_multiplier != 1.0:
        costs = costs.model_copy(
            update={
                "spread_bp_active": costs.spread_bp_active * spread_multiplier,
                "spread_bp_illiquid": costs.spread_bp_illiquid * spread_multiplier,
                "spread_bp_ultra_long": costs.spread_bp_ultra_long * spread_multiplier,
            }
        )
    if not fee:
        costs = costs.model_copy(update={"fee_bp": 0.0, "settlement_fee_cny_per_trade": 0.0})
    if not funding:
        costs = costs.model_copy(update={"repo_rate_fallback_const": 0.0})
    return cfg.model_copy(update={"costs": costs})


def _with_constraints(cfg: Config, **updates: float) -> Config:
    return cfg.model_copy(update={"constraints": cfg.constraints.model_copy(update=updates)})


def _with_rebalance(cfg: Config, mode: str) -> Config:
    return cfg.model_copy(update={"backtest": cfg.backtest.model_copy(update={"rebalance": mode})})


def _funding_for(cfg: Config, funding: pd.Series | None) -> pd.Series | None:
    """融资关闭（``repo_rate_fallback_const=0`` 且无理率序列）时返回 ``None``。"""
    return None if float(cfg.costs.repo_rate_fallback_const) == 0.0 else funding


# ---------------------------------------------------------------------------
# 指标
# ---------------------------------------------------------------------------
def _excess_bp(nav: pd.Series, benchmark_nav: pd.Series) -> float:
    total = float(nav.iloc[-1] / nav.iloc[0] - 1.0)
    bench = float(benchmark_nav.iloc[-1] / benchmark_nav.iloc[0] - 1.0)
    return (total - bench) * 1e4


def _engine_metrics(result: BacktestResult, cfg: Config) -> dict[str, Any]:
    cost = result.costs
    return {
        "final_nav": float(result.nav.iloc[-1]),
        "total_return": float(result.nav.iloc[-1] / result.nav.iloc[0] - 1.0),
        "benchmark_final_nav": float(result.benchmark_nav.iloc[-1]),
        "excess_bp": _excess_bp(result.nav, result.benchmark_nav),
        "total_fee": float(cost["fee"].sum()),
        "total_funding": float(cost["funding"].sum()),
        "total_spread": float(cost["spread"].sum()),
        "total_cost": float(cost["total"].sum()),
        "n_trades": len(result.trades),
        "n_rejected": len(result.rejected),
        "total_turnover": float(result.turnover.sum()),
        "n_violation_days": int((~result.constraints["ok"].astype(bool)).sum()),
    }


def _replay_metrics(replay: ReplayResult, benchmark: ReplayResult, cfg: Config) -> dict[str, Any]:
    cost = replay.costs
    return {
        "final_nav": float(replay.nav.iloc[-1]),
        "total_return": float(replay.nav.iloc[-1] / replay.nav.iloc[0] - 1.0),
        "benchmark_final_nav": float(benchmark.nav.iloc[-1]),
        "excess_bp": _excess_bp(replay.nav, benchmark.nav),
        "total_fee": float(cost["fee"].sum()),
        "total_funding": float(cost["funding"].sum()),
        "total_spread": float(cost["spread"].sum()),
        "total_cost": float(cost["total"].sum()),
    }


# ---------------------------------------------------------------------------
# 基准账本回放（trades 需要补齐静态条款）
# ---------------------------------------------------------------------------
def _curve_at(panel: pd.DataFrame, tenors: list[float], d: date) -> Curve:
    row = panel.loc[pd.Timestamp(d)].reindex(tenors)
    if row.isna().any():
        raise ValueError(f"{d} 曲线存在缺失期限：{row[row.isna()].index.tolist()}")
    return Curve(as_of=d, tenors=np.asarray(tenors), rates=tuple(row.values), method="hermite")


def _benchmark_trades(result: BacktestResult, panel: pd.DataFrame, cfg: Config) -> pd.DataFrame:
    """把引擎导出的基准成交补齐静态条款，供 ``replay_trades`` 重建 ``TradeRecord``。"""
    trades = getattr(result, "benchmark_trades", None)
    if trades is None or len(trades) == 0:
        return pd.DataFrame()
    if "coupon_pct" in trades.columns:
        return trades
    universe = build_universe_bonds(
        _curve_at(panel, list(CANONICAL_TENORS), result.start), result.start, cfg=cfg
    )
    by_code = {bond.code: bond for bond in universe}
    enriched = trades.copy()
    mapping = {
        "coupon_pct": "coupon_pct",
        "freq": "freq",
        "issue_date": "issue_date",
        "maturity_date": "maturity_date",
        "accrual": "accrual",
        "market": "market",
    }
    for column, attribute in mapping.items():
        enriched[column] = enriched["bond_code"].map(
            {code: getattr(bond, attribute) for code, bond in by_code.items()}
        )
    if "fee" not in enriched.columns:
        enriched["fee"] = 0.0
    unknown = enriched["coupon_pct"].isna()
    if bool(unknown.any()):
        raise ValueError(
            "基准成交含投资宇宙外的券，无法补齐静态条款："
            f"{sorted(set(enriched.loc[unknown, 'bond_code']))}"
        )
    return enriched


def _replay_pair(
    result: BacktestResult,
    *,
    panel: pd.DataFrame,
    calendar: TradingCalendar,
    funding: pd.Series | None,
    cfg: Config,
    switches: CostSwitches,
    trades: pd.DataFrame | None = None,
    benchmark_trades: pd.DataFrame | None = None,
) -> tuple[ReplayResult, ReplayResult | None]:
    """重放策略账本与基准账本（同一开关、同一窗口）。"""
    replay = (
        replay_engine_result(
            result,
            panel=panel,
            calendar=calendar,
            funding=funding,
            cfg=cfg,
            switches=switches,
        )
        if trades is None
        else replay_trades(
            trades,
            panel=panel,
            calendar=calendar,
            funding=funding,
            cfg=cfg,
            start=result.start,
            end=result.end,
            switches=switches,
        )
    )
    bench_frame = (
        benchmark_trades if benchmark_trades is not None else _benchmark_trades(result, panel, cfg)
    )
    benchmark = (
        replay_trades(
            bench_frame,
            panel=panel,
            calendar=calendar,
            funding=funding,
            cfg=cfg,
            start=result.start,
            end=result.end,
            switches=switches,
        )
        if len(bench_frame)
        else None
    )
    return replay, benchmark


# ---------------------------------------------------------------------------
# 三组实验
# ---------------------------------------------------------------------------
def _check_replay_matches_engine(
    result: BacktestResult, replay: ReplayResult, benchmark: ReplayResult | None
) -> float:
    """验收第一条：全开回放 NAV 逐日等于引擎 NAV（< 1e-6 元）。"""
    if len(replay.nav) != len(result.nav):
        raise RuntimeError(f"回放 NAV 长度 {len(replay.nav)} 与引擎 {len(result.nav)} 不一致")
    error = float(np.max(np.abs(replay.nav.to_numpy() - result.nav.to_numpy())))
    if error >= 1e-6:
        raise RuntimeError(f"全开开关回放 NAV 与引擎不一致：max|Δ|={error:.3e} 元（阈值 1e-6）")
    if benchmark is not None and len(benchmark.nav) == len(result.benchmark_nav):
        bench_error = float(
            np.max(np.abs(benchmark.nav.to_numpy() - result.benchmark_nav.to_numpy()))
        )
        if bench_error >= 1e-6:
            raise RuntimeError(
                f"基准账本回放 NAV 与引擎不一致：max|Δ|={bench_error:.3e} 元（阈值 1e-6）"
            )
    return error


def _fixed_path_rows(
    strategy_name: str,
    result: BacktestResult,
    *,
    panel: pd.DataFrame,
    calendar: TradingCalendar,
    funding: pd.Series | None,
    cfg: Config,
) -> list[dict[str, Any]]:
    """实验 1：固定成交路径下逐项关闭成本，分解 NAV 影响与相对基准超额变化。"""
    benchmark_trades = _benchmark_trades(result, panel, cfg)
    rows: list[dict[str, Any]] = []
    baseline_nav: float | None = None
    baseline_excess: float | None = None
    baseline_final: float | None = None
    for scenario, switches in FIXED_PATH_SCENARIOS:
        replay, benchmark = _replay_pair(
            result,
            panel=panel,
            calendar=calendar,
            funding=funding,
            cfg=cfg,
            switches=switches,
            benchmark_trades=benchmark_trades,
        )
        if scenario == "baseline":
            _check_replay_matches_engine(result, replay, benchmark)
            baseline_nav = float(replay.nav.iloc[-1])
            baseline_final = float(result.nav.iloc[-1])
            baseline_excess = (
                _excess_bp(replay.nav, benchmark.nav) if benchmark is not None else float("nan")
            )
        if baseline_nav is None or baseline_excess is None or baseline_final is None:
            raise RuntimeError("固定路径实验必须先跑 baseline 场景")
        metrics = (
            _replay_metrics(replay, benchmark, cfg)
            if benchmark is not None
            else {
                "final_nav": float(replay.nav.iloc[-1]),
                "total_return": float(replay.nav.iloc[-1] / replay.nav.iloc[0] - 1.0),
                "benchmark_final_nav": float("nan"),
                "excess_bp": float("nan"),
                "total_fee": float(replay.costs["fee"].sum()),
                "total_funding": float(replay.costs["funding"].sum()),
                "total_spread": float(replay.costs["spread"].sum()),
                "total_cost": float(replay.costs["total"].sum()),
            }
        )
        nav_impact = float(metrics["final_nav"] - baseline_nav)
        rows.append(
            {
                "strategy": strategy_name,
                "scenario": scenario,
                "switches": (
                    f"spread={switches.spread},fee={switches.fee},funding={switches.funding}"
                ),
                "start": str(result.start),
                "end": str(result.end),
                "config_hash": cfg.hash(),
                "final_nav": metrics["final_nav"],
                "total_return": metrics["total_return"],
                "benchmark_final_nav": metrics["benchmark_final_nav"],
                "excess_bp": metrics["excess_bp"],
                "total_fee": metrics["total_fee"],
                "total_funding": metrics["total_funding"],
                "total_spread": metrics["total_spread"],
                "total_cost": metrics["total_cost"],
                "nav_impact_cny": nav_impact,
                "nav_impact_bp": nav_impact / baseline_nav * 1e4 if baseline_nav else 0.0,
                "excess_change_bp": (
                    metrics["excess_bp"] - baseline_excess
                    if np.isfinite(metrics["excess_bp"]) and np.isfinite(baseline_excess)
                    else float("nan")
                ),
                "n_trades": len(result.trades),
            }
        )
    return rows


def _decision_rows(
    strategy_factory: Callable[[], Any],
    *,
    panel: pd.DataFrame,
    calendar: TradingCalendar,
    funding: pd.Series | None,
    cfg: Config,
    start: date,
    end: date,
) -> list[dict[str, Any]]:
    """实验 2：把成本置零后重跑引擎（决策可变），区分执行成本与行为反馈。"""
    variant_cfgs = {
        "baseline": cfg,
        "engine_no_spread": _with_costs(cfg, spread=False),
        "engine_no_fee": _with_costs(cfg, fee=False),
        "engine_no_funding": _with_costs(cfg, funding=False),
        "engine_all_off": _with_costs(cfg, spread=False, fee=False, funding=False),
    }
    rows: list[dict[str, Any]] = []
    baseline_metrics: dict[str, Any] | None = None
    for scenario, variant_cfg in variant_cfgs.items():
        result = run_backtest(
            strategy_factory(),
            panel=panel,
            calendar=calendar,
            funding=_funding_for(variant_cfg, funding),
            cfg=variant_cfg,
            start=start,
            end=end,
        )
        metrics = _engine_metrics(result, variant_cfg)
        if scenario == "baseline":
            baseline_metrics = metrics
        baseline = baseline_metrics or metrics
        nav_impact = float(metrics["final_nav"] - baseline["final_nav"])
        rows.append(
            {
                "strategy": result.strategy,
                "scenario": scenario,
                "start": str(result.start),
                "end": str(result.end),
                "config_hash": variant_cfg.hash(),
                **metrics,
                "nav_impact_cny": nav_impact,
                "nav_impact_bp": (
                    nav_impact / baseline["final_nav"] * 1e4 if baseline["final_nav"] else 0.0
                ),
                "excess_change_bp": metrics["excess_bp"] - baseline["excess_bp"],
            }
        )
    return rows


def _weekly_supported() -> bool:
    """检测引擎是否已实现 ``backtest.rebalance=weekly``（不改引擎，只探测）。"""
    module = inspect.getmodule(run_backtest)
    try:
        source = inspect.getsource(module) if module is not None else ""
    except (OSError, TypeError):
        return False
    return "weekly" in source or "isocalendar" in source or "last_rebalance_week" in source


def _scale_spread_trades(trades: pd.DataFrame, multiplier: float) -> pd.DataFrame | None:
    """固定路径下的价差乘数：``clean = mid + m×(exec − mid)``，价差成本同乘。"""
    if trades is None or len(trades) == 0 or "mid_clean_price" not in trades.columns:
        return None
    scaled = trades.copy()
    mid = scaled["mid_clean_price"].astype(float)
    scaled["clean_price"] = mid + float(multiplier) * (scaled["clean_price"].astype(float) - mid)
    if "spread_cost" in scaled.columns:
        scaled["spread_cost"] = scaled["spread_cost"].astype(float) * float(multiplier)
    return scaled


def _sensitivity_variants(quick: bool, weekly_ok: bool) -> list[tuple[str, list[Any]]]:
    """参数敏感性网格（quick 模式只保留少量取值）。"""
    if quick:
        grid: list[tuple[str, list[Any]]] = [
            ("min_trade_lot", [1_000_000.0, 5_000_000.0]),
            ("rebalance_band_lots", [0.0, 2.0]),
            ("backtest.rebalance", ["daily", "weekly"] if weekly_ok else ["daily"]),
            ("spread_multiplier", [0.0, 1.0]),
        ]
    else:
        grid = [
            ("min_trade_lot", [1_000_000.0, 5_000_000.0]),
            ("rebalance_band_lots", [0.0, 2.0, 4.0]),
            ("backtest.rebalance", ["daily", "weekly"] if weekly_ok else ["daily"]),
            ("spread_multiplier", [0.0, 0.5, 1.0, 2.0]),
        ]
    return grid


#: 死区=0 的等价实现：组合层 ``build_orders`` 要求成交阈值严格为正，
#: 用 1e-9 个 lot（远小于 1 手）实现"无死区"，再被 ``min_trade_lot`` 兜底，
#: 语义与 ``rebalance_band_lots=0`` 一致（记录值仍是 0）。
_ZERO_BAND_EPS = 1e-9


def _variant_cfg(cfg: Config, variant: str, value: Any) -> Config:
    if variant == "min_trade_lot":
        return _with_constraints(cfg, min_trade_lot=float(value))
    if variant == "rebalance_band_lots":
        band = float(value)
        return _with_constraints(cfg, rebalance_band_lots=_ZERO_BAND_EPS if band == 0.0 else band)
    if variant == "backtest.rebalance":
        return _with_rebalance(cfg, str(value))
    if variant == "spread_multiplier":
        return _with_costs(cfg, spread_multiplier=float(value))
    raise KeyError(f"未知敏感性变体：{variant!r}")


def _sensitivity_rows(
    strategy_factory: Callable[[], Any],
    strategy_name: str,
    baseline_result: BacktestResult,
    *,
    panel: pd.DataFrame,
    calendar: TradingCalendar,
    funding: pd.Series | None,
    cfg: Config,
    start: date,
    end: date,
    quick: bool,
    weekly_ok: bool,
) -> list[dict[str, Any]]:
    """实验 3：参数敏感性（改变决策 → 引擎重跑；价差乘数另加固定路径回放）。"""
    rows: list[dict[str, Any]] = []
    baseline_metrics = _engine_metrics(baseline_result, cfg)
    rows.append(
        {
            "strategy": strategy_name,
            "variant": "baseline",
            "value": "",
            "path": "engine",
            "start": str(baseline_result.start),
            "end": str(baseline_result.end),
            "config_hash": cfg.hash(),
            **baseline_metrics,
            "status": "ok",
        }
    )
    for variant, values in _sensitivity_variants(quick, weekly_ok):
        for value in values:
            if variant == "backtest.rebalance" and str(value) == "weekly" and not weekly_ok:
                rows.append(
                    {
                        "strategy": strategy_name,
                        "variant": variant,
                        "value": str(value),
                        "path": "engine",
                        "start": str(baseline_result.start),
                        "end": str(baseline_result.end),
                        "config_hash": cfg.hash(),
                        "total_return": float("nan"),
                        "excess_bp": float("nan"),
                        "total_fee": float("nan"),
                        "total_funding": float("nan"),
                        "total_spread": float("nan"),
                        "total_cost": float("nan"),
                        "n_trades": 0,
                        "total_turnover": float("nan"),
                        "n_violation_days": float("nan"),
                        "status": "skipped: 引擎未实现 backtest.rebalance=weekly（未改引擎）",
                    }
                )
                continue
            variant_cfg = _variant_cfg(cfg, variant, value)
            try:
                result = run_backtest(
                    strategy_factory(),
                    panel=panel,
                    calendar=calendar,
                    funding=_funding_for(variant_cfg, funding),
                    cfg=variant_cfg,
                    start=start,
                    end=end,
                )
            except (ValueError, RuntimeError, KeyError) as exc:
                rows.append(
                    {
                        "strategy": strategy_name,
                        "variant": variant,
                        "value": str(value),
                        "path": "engine",
                        "start": str(start),
                        "end": str(end),
                        "config_hash": variant_cfg.hash(),
                        "total_return": float("nan"),
                        "excess_bp": float("nan"),
                        "total_fee": float("nan"),
                        "total_funding": float("nan"),
                        "total_spread": float("nan"),
                        "total_cost": float("nan"),
                        "n_trades": 0,
                        "total_turnover": float("nan"),
                        "n_violation_days": float("nan"),
                        "status": f"error: {type(exc).__name__}: {exc}",
                    }
                )
                continue
            metrics = _engine_metrics(result, variant_cfg)
            status = "ok"
            if variant == "rebalance_band_lots" and float(value) == 0.0:
                status = "ok: band=0 以 1e-9 lot 等价实现（组合层要求成交阈值>0）"
            rows.append(
                {
                    "strategy": strategy_name,
                    "variant": variant,
                    "value": str(value),
                    "path": "engine",
                    "start": str(result.start),
                    "end": str(result.end),
                    "config_hash": variant_cfg.hash(),
                    **metrics,
                    "status": status,
                }
            )
            if variant == "spread_multiplier":
                scaled = _scale_spread_trades(baseline_result.trades, float(value))
                benchmark_trades = _benchmark_trades(baseline_result, panel, cfg)
                if scaled is None:
                    continue
                replay, benchmark = _replay_pair(
                    baseline_result,
                    panel=panel,
                    calendar=calendar,
                    funding=funding,
                    cfg=cfg,
                    switches=CostSwitches(),
                    trades=scaled,
                    benchmark_trades=(
                        _scale_spread_trades(benchmark_trades, float(value))
                        if len(benchmark_trades)
                        else benchmark_trades
                    ),
                )
                if benchmark is not None:
                    metrics = _replay_metrics(replay, benchmark, cfg)
                else:
                    metrics = {
                        "total_return": float(replay.nav.iloc[-1] / replay.nav.iloc[0] - 1.0),
                        "excess_bp": float("nan"),
                    }
                rows.append(
                    {
                        "strategy": strategy_name,
                        "variant": variant,
                        "value": str(value),
                        "path": "fixed_replay",
                        "start": str(baseline_result.start),
                        "end": str(baseline_result.end),
                        "config_hash": cfg.hash(),
                        "total_return": metrics.get("total_return", float("nan")),
                        "excess_bp": metrics.get("excess_bp", float("nan")),
                        "total_fee": float(replay.costs["fee"].sum()),
                        "total_funding": float(replay.costs["funding"].sum()),
                        "total_spread": float(replay.costs["spread"].sum()),
                        "total_cost": float(replay.costs["total"].sum()),
                        "n_trades": len(baseline_result.trades),
                        "total_turnover": float(baseline_result.turnover.sum()),
                        "n_violation_days": float("nan"),
                        "status": "ok",
                    }
                )
    return rows


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="成本对照实验（固定路径 + 决策变化 + 参数敏感性）")
    parser.add_argument("--start", type=date.fromisoformat, default=None)
    parser.add_argument("--end", type=date.fromisoformat, default=None)
    parser.add_argument("--config", default=None)
    parser.add_argument(
        "--quick", action="store_true", help="只跑 1 个策略 + 少量变体（测试用，默认短窗口）"
    )
    args = parser.parse_args(argv)

    cfg = get_config(args.config)
    start = args.start or (QUICK_START if args.quick else DEFAULT_START)
    end = args.end or (QUICK_END if args.quick else DEFAULT_END)
    if end <= start:
        parser.error(f"--end({end}) 必须晚于 --start({start})")

    names = ["carry_roll"] if args.quick else list(STRATEGY_ORDER)
    panel, funding, calendar = load_market(cfg)
    strategy_factories = build_strategy_factories(names, panel, cfg, end)
    weekly_ok = _weekly_supported()
    if not weekly_ok:
        print("[warn] 引擎未实现 backtest.rebalance=weekly：敏感性中该变体将跳过（不改引擎）")

    fixed_rows: list[dict[str, Any]] = []
    decision_rows: list[dict[str, Any]] = []
    sensitivity_rows: list[dict[str, Any]] = []
    for strategy_name, strategy_factory in strategy_factories:
        print(f"\n[cost-study] 回测策略 {strategy_name}（{start} → {end}）")
        result = run_backtest(
            strategy_factory(),
            panel=panel,
            calendar=calendar,
            funding=funding,
            cfg=cfg,
            start=start,
            end=end,
        )
        fixed_rows.extend(
            _fixed_path_rows(
                strategy_name,
                result,
                panel=panel,
                calendar=calendar,
                funding=funding,
                cfg=cfg,
            )
        )
        decision_rows.extend(
            _decision_rows(
                strategy_factory,
                panel=panel,
                calendar=calendar,
                funding=funding,
                cfg=cfg,
                start=start,
                end=end,
            )
        )
        sensitivity_rows.extend(
            _sensitivity_rows(
                strategy_factory,
                strategy_name,
                result,
                panel=panel,
                calendar=calendar,
                funding=funding,
                cfg=cfg,
                start=start,
                end=end,
                quick=args.quick,
                weekly_ok=weekly_ok,
            )
        )

    out = cfg.paths.abs("reports")
    out.mkdir(parents=True, exist_ok=True)
    frames = {
        "cost_study_fixed_path.csv": pd.DataFrame(fixed_rows, columns=list(FIXED_PATH_COLUMNS)),
        "cost_study_decision_change.csv": pd.DataFrame(
            decision_rows, columns=list(DECISION_COLUMNS)
        ),
        "cost_sensitivity.csv": pd.DataFrame(sensitivity_rows, columns=list(SENSITIVITY_COLUMNS)),
    }
    for filename, frame in frames.items():
        frame.to_csv(out / filename, index=False, encoding="utf-8-sig")

    print("\n=== 1) 固定成交路径的成本分解（回放，不改变交易路径）===")
    fixed = frames["cost_study_fixed_path.csv"]
    show = fixed[
        [
            "strategy",
            "scenario",
            "final_nav",
            "excess_bp",
            "total_fee",
            "total_funding",
            "total_spread",
            "nav_impact_cny",
            "nav_impact_bp",
            "excess_change_bp",
        ]
    ]
    with pd.option_context("display.width", 260):
        print(show.to_string(index=False, float_format=lambda x: f"{x:.4f}"))

    print("\n=== 2) 允许重新决策的成本敏感性（引擎重跑）===")
    decision = frames["cost_study_decision_change.csv"]
    show = decision[
        [
            "strategy",
            "scenario",
            "final_nav",
            "excess_bp",
            "n_trades",
            "total_turnover",
            "total_cost",
            "nav_impact_cny",
            "excess_change_bp",
        ]
    ]
    with pd.option_context("display.width", 260):
        print(show.to_string(index=False, float_format=lambda x: f"{x:.4f}"))

    print("\n=== 3) 参数敏感性（lot/死区/频率/价差）===")
    sensitivity = frames["cost_sensitivity.csv"]
    show = sensitivity[
        [
            "strategy",
            "variant",
            "value",
            "path",
            "total_return",
            "excess_bp",
            "total_cost",
            "n_trades",
            "total_turnover",
            "n_violation_days",
            "status",
        ]
    ]
    with pd.option_context("display.width", 300):
        print(show.to_string(index=False, float_format=lambda x: f"{x:.4f}"))

    print(f"\n产物目录: {out}")
    for filename in frames:
        print(f"  - {out / filename}")
    print(
        "\n验收：三开关全开时回放 NAV 与引擎逐日一致（< 1e-6 元）；"
        "固定路径分解与决策变化对照可区分执行成本与行为反馈。"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
