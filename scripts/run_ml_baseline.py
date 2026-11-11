#!/usr/bin/env python3
"""监督学习数据集 + 线性基线（Ridge vs 零预测）：只看 OOS 预测力，不做 alpha 宣称。

做了什么
--------
1. ``load_market`` + 冻结滚动配置（``roll_v1_trigger1y_parissue``、``benchmark_version=rolling``），
   默认 2019-01-28 → 2025-12-31，``--quick`` 到 2021-12-31；
2. 特征只用 t 及之前的数据（每个决策时点单独 as-of 视图，由
   :func:`ficc.ml.asof.build_training_matrix` 逐行构造）：level/slope/curvature、2s10s、
   carry 代理（5Y 票息 − FDR007）、20/60 日动量、EWMA 波动、FDR007 水平/5 日变化；
3. 标签 = **同一个引擎/账本/滚动/成本**生成的基准 NAV 的净收益
   ``bench_nav[label_end]/bench_nav[execution]-1``（next-close 执行；成本/票息/融资/滚动
   已在 NAV 内），由 :func:`ficc.ml.dataset.build_supervised_dataset` 产出并审计；
4. 模型：numpy 闭式 Ridge（训练窗内标准化，验证/测试复用训练统计，禁止全样本拟合）；
   alpha 网格只用**外层训练窗 + 验证窗**选择，测试窗只报告；零预测基线 = 训练标签均值；
5. 输出 ``reports/ml_dataset_manifest.json``、``reports/ml_dataset.csv``、
   ``reports/ml_dataset_summary.csv``、``reports/ml_baseline_metrics.csv``。

结论边界（必须随结果一起读）
--------------------------
- 单一历史（2019–2025 已被本项目研究过，不是 untouched holdout）；
- 标签是**基准市场净收益**（含成本/票息/融资/滚动），不是策略超额收益；
- 只报告样本外预测力（IC/R²/MAE），**没有**做成本后的组合回测，不构成"可交易 alpha"证据；
- 预处理/标准化只在训练窗拟合，验证/测试冻结使用；alpha 选择只看验证窗；
- 标签持有期 5 日、决策每 5 个交易日一次，相邻标签仍有部分重叠，IC 的独立性有限。

用法::

    PYTHONPATH=src .venv/bin/python scripts/run_ml_baseline.py --quick
    PYTHONPATH=src .venv/bin/python scripts/run_ml_baseline.py            # 全量
"""

from __future__ import annotations

import argparse
import itertools
import json
import sys
from dataclasses import dataclass
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
from run_long_sample import rolling_config  # noqa: E402
from run_strategies import load_market  # noqa: E402

from ficc.config import Config, get_config  # noqa: E402
from ficc.logging import get_logger  # noqa: E402
from ficc.ml.asof import FeatureSpec  # noqa: E402
from ficc.ml.dataset import (  # noqa: E402
    ClockError,
    InformationClock,
    attach_funding_column,
    build_supervised_dataset,
    dataset_manifest,
)
from ficc.ml.walkforward import (  # noqa: E402
    Split,
    WalkForwardSpec,
    generate_splits,
    validate_no_leakage,
)

log = get_logger("ml_baseline")

START = date(2019, 2, 1)
END = date(2025, 12, 31)
QUICK_END = date(2021, 12, 31)
FUNDING_COLUMN = "FDR007"
TREE_DEPTHS = (1, 2, 3)
ALPHA_GRID: tuple[float, ...] = (0.01, 0.1, 1.0, 10.0, 100.0)
MIN_TRAIN_ROWS = 30
MIN_VALID_ROWS = 5
MIN_TEST_ROWS = 3


# ---------------------------------------------------------------------------
# 特征（所有 transform 只能看到 available_through 及之前）
# ---------------------------------------------------------------------------
def feature_specs() -> list[FeatureSpec]:
    """监督学习特征：水平/斜率/曲率、2s10s、carry、动量、EWMA 波动、资金面。"""
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
            name="carry_proxy_5y",
            transform=lambda f: f["5.0"] - f[FUNDING_COLUMN],
            inputs=("5.0", FUNDING_COLUMN),
        ),
        FeatureSpec(
            name="mom_20_5y",
            transform=lambda f: f["5.0"] - f["5.0"].shift(20),
            inputs=("5.0",),
        ),
        FeatureSpec(
            name="mom_60_5y",
            transform=lambda f: f["5.0"] - f["5.0"].shift(60),
            inputs=("5.0",),
        ),
        FeatureSpec(
            name="ewma_vol_20_5y",
            transform=lambda f: f["5.0"].diff().ewm(span=20, adjust=False).std(),
            inputs=("5.0",),
        ),
        FeatureSpec(
            name="fdr007_level",
            transform=lambda f: f[FUNDING_COLUMN],
            inputs=(FUNDING_COLUMN,),
        ),
        FeatureSpec(
            name="fdr007_change_5",
            transform=lambda f: f[FUNDING_COLUMN] - f[FUNDING_COLUMN].shift(5),
            inputs=(FUNDING_COLUMN,),
        ),
    ]


# ---------------------------------------------------------------------------
# 决策日与 walk-forward（purge/embargo 由标签时钟决定）
# ---------------------------------------------------------------------------
def make_decisions(
    calendar: Any,
    start: date,
    end: date,
    clock: InformationClock,
    step_days: int,
) -> list[date]:
    """每 ``step_days`` 个交易日一个决策；只保留标签在 ``end`` 前可用的决策。"""
    if step_days < 1:
        raise ValueError(f"decision_step 必须 ≥ 1，实际 {step_days}")
    days = calendar.trading_days_between(start, end)
    decisions: list[date] = []
    for i in range(0, len(days), step_days):
        decision = days[i]
        try:
            if clock.label_available_after(decision, calendar) > end:
                break
        except ClockError:
            break
        decisions.append(decision)
    if len(decisions) < MIN_TRAIN_ROWS:
        raise ValueError(
            f"决策样本不足：{len(decisions)} 行（至少 {MIN_TRAIN_ROWS}）；"
            "请扩大样本区间或减小 --decision-step"
        )
    return decisions


def make_splits(
    calendar: Any,
    start: date,
    end: date,
    clock: InformationClock,
    mode: str,
) -> tuple[list[Split], WalkForwardSpec]:
    """按 ``WalkForwardSpec`` 生成滚动/扩展 split；purge/embargo = 标签可用偏移。"""
    days = calendar.trading_days_between(start, end)
    n_days = len(days)
    purge = clock.label_available_offset
    embargo = clock.label_available_offset
    n_test = min(63, max(10, n_days // 10))
    n_valid = min(63, max(10, n_days // 10))
    n_train = min(252, max(60, n_days // 2))
    # 区间偏短时逐步缩短窗口，保证至少能切出一组完整 split
    while n_train + n_valid + n_test + purge + embargo + 1 > n_days:
        if n_test <= 5 or n_valid <= 5 or n_train <= 30:
            raise ValueError(
                f"样本区间 {start}~{end}（{n_days} 交易日）太短，无法生成完整 walk-forward split"
            )
        n_test = max(5, n_test - 5)
        n_valid = max(5, n_valid - 5)
        n_train = max(30, n_train - 10)
    spec = WalkForwardSpec(
        train_start=days[0],
        train_end=days[n_train - 1],
        valid_start=days[n_train],
        valid_end=days[n_train + n_valid - 1],
        test_start=days[n_train + n_valid],
        test_end=days[n_train + n_valid + n_test - 1],
        purge_days=purge,
        embargo_days=embargo,
        step_days=n_test,
        expanding=(mode == "expanding"),
    )
    splits = generate_splits(start, end, calendar, spec)
    if not splits:
        raise ValueError(f"样本区间 {start}~{end} 无法生成 walk-forward split")
    validate_no_leakage(splits, clock.to_forward_label_spec())
    return splits, spec


def _rows_between(dataset: pd.DataFrame, start: date, end: date) -> pd.DataFrame:
    mask = (dataset.index >= pd.Timestamp(start)) & (dataset.index <= pd.Timestamp(end))
    return dataset.loc[mask]


def _assert_labels_available_by(frame: pd.DataFrame, boundary: date, *, context: str) -> None:
    """标签必须在后续窗口开始前可用（purge/embargo 的实际数据检查）。"""
    if frame.empty:
        return
    latest = pd.Timestamp(frame["label_available_after"].max())
    if latest > pd.Timestamp(boundary):
        raise AssertionError(
            f"{context}: 最晚标签可用日 {latest.date()} 晚于边界 {boundary}（purge/embargo 不足）"
        )


# ---------------------------------------------------------------------------
# numpy 闭式 Ridge（训练窗标准化；验证/测试复用训练统计）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class RidgeModel:
    """闭式 Ridge：``y = intercept + ((X - mean) / scale) @ coef``。"""

    coef: np.ndarray
    mean: np.ndarray
    scale: np.ndarray
    intercept: float

    def predict(self, features: np.ndarray) -> np.ndarray:
        standardized = (features - self.mean) / self.scale
        return standardized @ self.coef + self.intercept


def fit_ridge(features: np.ndarray, target: np.ndarray, alpha: float) -> RidgeModel:
    """闭式解 ``(Z'Z + αI) w = Z'(y - ȳ)``，``Z`` 用**训练窗**均值/标准差标准化。"""
    if alpha <= 0.0:
        raise ValueError(f"alpha 必须为正数，实际 {alpha}")
    x = np.asarray(features, dtype=float)
    y = np.asarray(target, dtype=float)
    if x.ndim != 2 or y.ndim != 1 or x.shape[0] != y.shape[0]:
        raise ValueError(f"训练矩阵形状不合法：X={x.shape}, y={y.shape}")
    mean = x.mean(axis=0)
    scale = x.std(axis=0, ddof=0)
    scale = np.where(scale > 0.0, scale, 1.0)
    standardized = (x - mean) / scale
    intercept = float(y.mean())
    gram = standardized.T @ standardized + float(alpha) * np.eye(standardized.shape[1])
    rhs = standardized.T @ (y - intercept)
    coef = np.linalg.solve(gram, rhs)
    return RidgeModel(coef=coef, mean=mean, scale=scale, intercept=intercept)


# ---------------------------------------------------------------------------
# 指标
# ---------------------------------------------------------------------------
def _rank_ic(target: np.ndarray, prediction: np.ndarray) -> float:
    """Spearman rank IC（常量预测/样本不足返回 NaN）。"""
    y = np.asarray(target, dtype=float)
    p = np.asarray(prediction, dtype=float)
    if len(y) < 3:
        return float("nan")
    y_rank = pd.Series(y).rank().to_numpy()
    p_rank = pd.Series(p).rank().to_numpy()
    if np.std(y_rank) == 0.0 or np.std(p_rank) == 0.0:
        return float("nan")
    return float(np.corrcoef(y_rank, p_rank)[0, 1])


def _r2(target: np.ndarray, prediction: np.ndarray) -> float:
    y = np.asarray(target, dtype=float)
    p = np.asarray(prediction, dtype=float)
    if len(y) < 2:
        return float("nan")
    ss_tot = float(((y - y.mean()) ** 2).sum())
    if ss_tot == 0.0:
        return float("nan")
    return float(1.0 - ((y - p) ** 2).sum() / ss_tot)


def _mae(target: np.ndarray, prediction: np.ndarray) -> float:
    if len(target) == 0:
        return float("nan")
    return float(
        np.mean(np.abs(np.asarray(target, dtype=float) - np.asarray(prediction, dtype=float)))
    )


@dataclass(frozen=True)
class TreeModel:
    """极简 CART 回归树（深度 1–3，仅作同协议非线性对照；不做调参研究）。"""

    feature: int | None = None
    threshold: float | None = None
    left: TreeModel | None = None
    right: TreeModel | None = None
    value: float = 0.0

    def predict(self, features: np.ndarray) -> np.ndarray:
        if self.feature is None:
            return np.full(len(features), self.value, dtype=float)
        out = np.empty(len(features), dtype=float)
        mask = features[:, self.feature] <= float(self.threshold)
        if mask.any():
            out[mask] = self.left.predict(features[mask])  # type: ignore[union-attr]
        if (~mask).any():
            out[~mask] = self.right.predict(features[~mask])  # type: ignore[union-attr]
        return out


def fit_tree(
    features: np.ndarray,
    target: np.ndarray,
    *,
    max_depth: int = 2,
    min_leaf: int = 8,
) -> TreeModel:
    """递归二分裂回归树；阈值只在训练特征分位点上搜索，不接触测试窗。"""
    if len(target) < 2 * min_leaf or max_depth <= 0:
        return TreeModel(value=float(np.mean(target)) if len(target) else 0.0)
    best = {"sse": float(((target - target.mean()) ** 2).sum()), "feature": None, "threshold": None}
    for feature in range(features.shape[1]):
        column = features[:, feature]
        quantiles = np.unique(np.quantile(column, [0.2, 0.35, 0.5, 0.65, 0.8]))
        for threshold in quantiles:
            left = column <= threshold
            n_left = int(left.sum())
            if n_left < min_leaf or len(target) - n_left < min_leaf:
                continue
            left_y, right_y = target[left], target[~left]
            sse = float(
                ((left_y - left_y.mean()) ** 2).sum() + ((right_y - right_y.mean()) ** 2).sum()
            )
            if sse < best["sse"] - 1e-15:
                best = {"sse": sse, "feature": feature, "threshold": float(threshold)}
    if best["feature"] is None:
        return TreeModel(value=float(target.mean()))
    mask = features[:, int(best["feature"])] <= float(best["threshold"])
    return TreeModel(
        feature=int(best["feature"]),
        threshold=float(best["threshold"]),
        left=fit_tree(features[mask], target[mask], max_depth=max_depth - 1, min_leaf=min_leaf),
        right=fit_tree(features[~mask], target[~mask], max_depth=max_depth - 1, min_leaf=min_leaf),
        value=float(target.mean()),
    )


# ---------------------------------------------------------------------------
# 评估（测试窗只报告，不参与任何选择）
# ---------------------------------------------------------------------------
def evaluate_splits(
    dataset: pd.DataFrame,
    splits: list[Split],
    feature_names: list[str],
) -> tuple[pd.DataFrame, dict[str, float], int, pd.DataFrame]:
    """逐 split：训练→选 alpha/树深（验证窗）→测试窗报告；返回 metrics/pooled/skipped/oos。"""
    rows: list[dict[str, Any]] = []
    pooled_y: list[np.ndarray] = []
    pooled_pred: list[np.ndarray] = []
    pooled_train_mean: list[np.ndarray] = []
    pooled_literal_zero: list[np.ndarray] = []
    pooled_tree: list[np.ndarray] = []
    oos_rows: list[dict[str, Any]] = []
    skipped = 0

    for split in splits:
        train = _rows_between(dataset, split.train_dates[0], split.train_dates[-1])
        valid = _rows_between(dataset, split.valid_dates[0], split.valid_dates[-1])
        test = _rows_between(dataset, split.test_dates[0], split.test_dates[-1])
        if len(train) < MIN_TRAIN_ROWS or len(valid) < MIN_VALID_ROWS or len(test) < MIN_TEST_ROWS:
            skipped += 1
            log.warning(
                "split %d 跳过：n_train=%d n_valid=%d n_test=%d（阈值 %d/%d/%d）",
                split.split_id,
                len(train),
                len(valid),
                len(test),
                MIN_TRAIN_ROWS,
                MIN_VALID_ROWS,
                MIN_TEST_ROWS,
            )
            continue

        # 数据层 purge/embargo：训练标签在验证窗开始前可用，验证标签在测试窗开始前可用
        _assert_labels_available_by(
            train, split.valid_dates[0], context=f"split {split.split_id} train→valid"
        )
        _assert_labels_available_by(
            valid, split.test_dates[0], context=f"split {split.split_id} valid→test"
        )

        x_train = train[feature_names].to_numpy(dtype=float)
        y_train = train["label"].to_numpy(dtype=float)
        x_valid = valid[feature_names].to_numpy(dtype=float)
        y_valid = valid["label"].to_numpy(dtype=float)
        x_test = test[feature_names].to_numpy(dtype=float)
        y_test = test["label"].to_numpy(dtype=float)

        # 内层选择：alpha 只用训练/验证窗；测试窗不参与
        best_alpha = float(ALPHA_GRID[0])
        best_valid_mae = float("inf")
        for alpha in ALPHA_GRID:
            candidate = fit_ridge(x_train, y_train, alpha)
            valid_mae = _mae(y_valid, candidate.predict(x_valid))
            if valid_mae < best_valid_mae:
                best_valid_mae = valid_mae
                best_alpha = float(alpha)
        model = fit_ridge(x_train, y_train, best_alpha)
        valid_pred = model.predict(x_valid)
        test_pred = model.predict(x_test)

        best_depth = int(TREE_DEPTHS[0])
        best_tree_mae = float("inf")
        tree_model: TreeModel | None = None
        for depth in TREE_DEPTHS:
            candidate_tree = fit_tree(x_train, y_train, max_depth=depth)
            tree_mae = _mae(y_valid, candidate_tree.predict(x_valid))
            if tree_mae < best_tree_mae:
                best_tree_mae = tree_mae
                best_depth = int(depth)
                tree_model = candidate_tree
        assert tree_model is not None
        tree_test_pred = tree_model.predict(x_test)

        train_mean = float(y_train.mean())
        mean_valid = np.full_like(y_valid, train_mean)
        mean_test = np.full_like(y_test, train_mean)
        literal_zero_test = np.zeros_like(y_test)
        literal_zero_valid = np.zeros_like(y_valid)

        rows.append(
            {
                "split_id": split.split_id,
                "mode": split.manifest.get("mode", "rolling"),
                "train_start": split.train_dates[0].isoformat(),
                "train_end": split.train_dates[-1].isoformat(),
                "valid_start": split.valid_dates[0].isoformat(),
                "valid_end": split.valid_dates[-1].isoformat(),
                "test_start": split.test_dates[0].isoformat(),
                "test_end": split.test_dates[-1].isoformat(),
                "n_train": len(train),
                "n_valid": len(valid),
                "n_test": len(test),
                "alpha": best_alpha,
                "valid_mae_ridge": best_valid_mae,
                "valid_mae_train_mean": _mae(y_valid, mean_valid),
                "valid_mae_literal_zero": _mae(y_valid, literal_zero_valid),
                "valid_ic_ridge": _rank_ic(y_valid, valid_pred),
                "test_ic_ridge": _rank_ic(y_test, test_pred),
                "test_r2_ridge": _r2(y_test, test_pred),
                "test_r2_train_mean": _r2(y_test, mean_test),
                "test_r2_literal_zero": _r2(y_test, literal_zero_test),
                "test_mae_ridge": _mae(y_test, test_pred),
                "test_mae_train_mean": _mae(y_test, mean_test),
                "test_mae_literal_zero": _mae(y_test, literal_zero_test),
                "test_tree_depth": best_depth,
                "test_ic_tree": _rank_ic(y_test, tree_test_pred),
                "test_r2_tree": _r2(y_test, tree_test_pred),
                "test_mae_tree": _mae(y_test, tree_test_pred),
                "test_label_mean": float(y_test.mean()),
                "test_pred_mean": float(test_pred.mean()),
            }
        )
        for i in range(len(y_test)):
            oos_rows.append(
                {
                    "split_id": int(split.split_id),
                    "decision_as_of": test["decision_as_of"].iloc[i],
                    "label": float(y_test[i]),
                    "pred_ridge": float(test_pred[i]),
                    "pred_tree": float(tree_test_pred[i]),
                    "pred_train_mean": float(train_mean),
                    "pred_literal_zero": 0.0,
                    "test_start": split.test_dates[0],
                    "test_end": split.test_dates[-1],
                }
            )
        pooled_y.append(y_test)
        pooled_pred.append(test_pred)
        pooled_train_mean.append(mean_test)
        pooled_literal_zero.append(literal_zero_test)
        pooled_tree.append(tree_test_pred)

    if not rows:
        raise ValueError("没有任何 split 满足最小样本要求，无法评估")
    metrics = pd.DataFrame(rows)
    pooled = {
        "n": float(sum(len(item) for item in pooled_y)),
        "ic": _rank_ic(np.concatenate(pooled_y), np.concatenate(pooled_pred)),
        "r2": _r2(np.concatenate(pooled_y), np.concatenate(pooled_pred)),
        "r2_train_mean": _r2(np.concatenate(pooled_y), np.concatenate(pooled_train_mean)),
        "r2_literal_zero": _r2(np.concatenate(pooled_y), np.concatenate(pooled_literal_zero)),
        "mae": _mae(np.concatenate(pooled_y), np.concatenate(pooled_pred)),
        "mae_train_mean": _mae(np.concatenate(pooled_y), np.concatenate(pooled_train_mean)),
        "mae_literal_zero": _mae(np.concatenate(pooled_y), np.concatenate(pooled_literal_zero)),
        "ic_tree": _rank_ic(np.concatenate(pooled_y), np.concatenate(pooled_tree)),
        "r2_tree": _r2(np.concatenate(pooled_y), np.concatenate(pooled_tree)),
        "mae_tree": _mae(np.concatenate(pooled_y), np.concatenate(pooled_tree)),
    }
    return metrics, pooled, skipped, pd.DataFrame(oos_rows)


def dataset_summary(dataset: pd.DataFrame, feature_names: list[str]) -> pd.DataFrame:
    """特征/标签描述统计（含缺失计数与样本首尾日期）。"""
    columns = [*feature_names, "label"]
    stats = dataset[columns].describe().T
    stats["n_nan"] = dataset[columns].isna().sum()
    stats["n_rows"] = len(dataset)
    stats["start"] = dataset.index.min().date().isoformat()
    stats["end"] = dataset.index.max().date().isoformat()
    stats = stats.rename_axis("column").reset_index()
    return stats[
        ["column", "n_rows", "count", "n_nan", "mean", "std", "min", "25%", "50%", "75%", "max"]
    ]


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="监督学习数据集 + 线性基线（OOS 预测力）")
    parser.add_argument("--start", type=date.fromisoformat, default=START)
    parser.add_argument("--end", type=date.fromisoformat, default=None)
    parser.add_argument("--quick", action="store_true", help="短窗 2019-01-28→2021-12-31")
    parser.add_argument("--mode", choices=["rolling", "expanding"], default="rolling")
    parser.add_argument("--holding-days", type=int, default=5)
    parser.add_argument("--decision-step", type=int, default=5)
    parser.add_argument("--config", default=None)
    args = parser.parse_args(argv)

    if args.holding_days < 2:
        parser.error("--holding-days 必须 ≥ 2（next-close 执行后至少一个交易日收益）")

    cfg: Config = rolling_config(get_config(args.config))
    if cfg.backtest.benchmark_version != "rolling":  # pragma: no cover - 防御式
        raise RuntimeError("ML 基线要求滚动基准版本（benchmark_version=rolling）")
    out = cfg.paths.abs("reports")
    out.mkdir(parents=True, exist_ok=True)

    panel, funding, calendar = load_market(cfg)
    requested_end = args.end or (QUICK_END if args.quick else END)
    end = min(requested_end, calendar.last_day)
    if end <= args.start:
        parser.error(
            f"--end={requested_end} 必须晚于 --start={args.start}（数据截至 {calendar.last_day}）"
        )

    clock = InformationClock(holding_days=args.holding_days)
    decisions = make_decisions(calendar, args.start, end, clock, args.decision_step)
    splits, spec = make_splits(calendar, args.start, end, clock, args.mode)
    log.info(
        "样本 %s→%s，决策 %d 个（每 %d 交易日），split %d 个（purge/embargo=%d 日），基准=%s",
        args.start,
        end,
        len(decisions),
        args.decision_step,
        len(splits),
        clock.label_available_offset,
        cfg.backtest.benchmark_version,
    )

    specs = feature_specs()
    feature_panel = attach_funding_column(panel, funding, FUNDING_COLUMN)
    from ficc.curve.curve import Curve
    from ficc.data.schema import CANONICAL_TENORS

    terms_day = calendar.prev_trading_day(decisions[0], 1)
    terms_rates = panel.loc[pd.Timestamp(terms_day)].reindex(list(CANONICAL_TENORS))
    if terms_rates.isna().any():
        raise ValueError(f"{terms_day} 曲线缺期限，无法初始化基准标签")
    initial_curve = Curve(
        as_of=terms_day,
        tenors=np.asarray(sorted(CANONICAL_TENORS), dtype=float),
        rates=terms_rates.to_numpy(dtype=float),
        method="linear",
    )
    dataset = build_supervised_dataset(
        feature_panel,
        funding,
        calendar,
        decisions,
        specs,
        cfg,
        clock,
        initial_curve=initial_curve,
    )
    feature_names = [str(name) for name in dataset.attrs["feature_names"]]
    log.info(
        "数据集 %d 行（输入 %d，删除 %d 行特征缺口），特征 %d 个",
        len(dataset),
        dataset.attrs["n_input_rows"],
        dataset.attrs["n_dropped_rows"],
        len(feature_names),
    )

    metrics, pooled, skipped, oos = evaluate_splits(dataset, splits, feature_names)
    summary = dataset_summary(dataset, feature_names)

    manifest = dataset_manifest(dataset)
    # R50：按真实标签窗口计算相邻重叠（默认 step=5、窗口 4 日 → 0），不再默认假设重叠
    label_windows = list(zip(dataset["execution_date"], dataset["label_end_date"], strict=True))
    overlaps = 0
    for (_s1, e1), (s2, _e2) in itertools.pairwise(label_windows):
        if s2 <= e1:
            overlaps += 1
    manifest["run"] = {
        "script": "scripts/run_ml_baseline.py",
        "quick": bool(args.quick),
        "requested_end": requested_end.isoformat(),
        "effective_end": end.isoformat(),
        "decision_step_days": args.decision_step,
        "mode": args.mode,
        "n_decisions": len(decisions),
        "n_splits_requested": len(splits),
        "n_splits_evaluated": len(metrics),
        "n_splits_skipped": skipped,
        "purge_days": spec.purge_days,
        "embargo_days": spec.embargo_days,
        "step_days": spec.step_days,
        "declared_window": {
            "train": [spec.train_start.isoformat(), spec.train_end.isoformat()],
            "valid": [spec.valid_start.isoformat(), spec.valid_end.isoformat()],
            "test": [spec.test_start.isoformat(), spec.test_end.isoformat()],
            "expanding": spec.expanding,
        },
        "model": {
            "type": "numpy_closed_form_ridge",
            "alpha_grid": list(ALPHA_GRID),
            "preprocessing": "train_window_standardize_zscore",
            "selection": "inner_valid_mae_only_test_never_used",
            "baselines": {
                "train_mean": "训练窗标签均值（旧称 zero 基线，已更名）",
                "literal_zero": "预测恒为 0",
            },
        },
        "label_window": {
            "holding_days_from_decision": int(args.holding_days),
            "return_window_trading_days": int(args.holding_days) - 1,
            "decision_step_days": int(args.decision_step),
            "n_adjacent_overlaps": overlaps,
            "overlap_rate": overlaps / max(len(label_windows) - 1, 1),
        },
        "pooled_oos": pooled,
        "limitations": [
            "单一历史：2019–2025 已被本项目研究，不是 untouched holdout",
            "标签是基准市场净收益（含成本/票息/融资/滚动），不是策略超额收益",
            "只报告 OOS 预测力；未做成本后的组合回测，不构成可交易 alpha 证据",
            f"标签窗口为 (execution, label_end]：holding={args.holding_days} 自决策日起算，"
            f"实际执行后 {args.holding_days - 1} 个交易日；"
            f"决策步长 {args.decision_step} 个交易日，相邻标签窗口重叠数={overlaps}"
            f"（按真实审计日期计算，非默认假设）",
            "Ridge/标准化只在训练窗拟合；测试窗不参与 alpha 选择或任何拟合",
        ],
    }

    metrics_path = out / "ml_baseline_metrics.csv"
    summary_path = out / "ml_dataset_summary.csv"
    manifest_path = out / "ml_dataset_manifest.json"
    dataset_path = out / "ml_dataset.csv"
    oos_path = out / "ml_oos_predictions.csv"
    metrics.to_csv(metrics_path, index=False, encoding="utf-8-sig")
    summary.to_csv(summary_path, index=False, encoding="utf-8-sig")
    dataset.to_csv(dataset_path, index=True, encoding="utf-8-sig")
    oos.to_csv(oos_path, index=False, encoding="utf-8-sig")
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2, default=str), encoding="utf-8"
    )

    print("\n=== 数据集 manifest ===")
    print(json.dumps(manifest, ensure_ascii=False, indent=2, default=str))
    print("\n=== 逐 split OOS 指标（测试窗只报告，不调参）===")
    cols = [
        "split_id",
        "n_train",
        "n_valid",
        "n_test",
        "alpha",
        "test_ic_ridge",
        "test_r2_ridge",
        "test_r2_train_mean",
        "test_r2_literal_zero",
        "test_mae_ridge",
        "test_mae_train_mean",
        "test_mae_literal_zero",
        "test_ic_tree",
        "test_r2_tree",
        "test_mae_tree",
    ]
    with pd.option_context("display.width", 220):
        print(metrics[cols].to_string(index=False, float_format=lambda x: f"{x:.6f}"))
    print(
        "\n=== Pooled OOS（不同 split 的测试窗不重叠）===\n"
        f"n={pooled['n']:.0f}  IC={pooled['ic']:.4f}  "
        f"R²={pooled['r2']:.4f}（train_mean {pooled['r2_train_mean']:.4f}；literal_zero {pooled['r2_literal_zero']:.4f}）  "
        f"MAE={pooled['mae']:.2e}（train_mean {pooled['mae_train_mean']:.2e}；literal_zero {pooled['mae_literal_zero']:.2e}）\n"
        f"tree: IC={pooled['ic_tree']:.4f} R²={pooled['r2_tree']:.4f} MAE={pooled['mae_tree']:.2e}"
    )
    print(
        "\n结论边界：以上是基准市场收益的样本外预测力，不是可交易 alpha；"
        "未做成本后的组合回测，且 2019–2025 是已研究样本。"
    )
    print(f"\n产物：{metrics_path} / {summary_path} / {manifest_path} / {dataset_path}")
    log.info("ML 基线完成：%d 个 split 评估，%d 个跳过", len(metrics), skipped)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
