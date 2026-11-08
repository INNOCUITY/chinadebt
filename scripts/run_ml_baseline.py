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

START = date(2019, 1, 28)
END = date(2025, 12, 31)
QUICK_END = date(2021, 12, 31)
FUNDING_COLUMN = "FDR007"
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


# ---------------------------------------------------------------------------
# 评估（测试窗只报告，不参与任何选择）
# ---------------------------------------------------------------------------
def evaluate_splits(
    dataset: pd.DataFrame,
    splits: list[Split],
    feature_names: list[str],
) -> tuple[pd.DataFrame, dict[str, float], int]:
    """逐 split：训练→选 alpha（验证窗）→测试窗报告；返回 metrics/pooled/skipped。"""
    rows: list[dict[str, Any]] = []
    pooled_y: list[np.ndarray] = []
    pooled_pred: list[np.ndarray] = []
    pooled_zero: list[np.ndarray] = []
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

        zero = float(y_train.mean())
        zero_valid = np.full_like(y_valid, zero)
        zero_test = np.full_like(y_test, zero)

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
                "valid_mae_zero": _mae(y_valid, zero_valid),
                "valid_ic_ridge": _rank_ic(y_valid, valid_pred),
                "test_ic_ridge": _rank_ic(y_test, test_pred),
                "test_r2_ridge": _r2(y_test, test_pred),
                "test_r2_zero": _r2(y_test, zero_test),
                "test_mae_ridge": _mae(y_test, test_pred),
                "test_mae_zero": _mae(y_test, zero_test),
                "test_label_mean": float(y_test.mean()),
                "test_pred_mean": float(test_pred.mean()),
            }
        )
        pooled_y.append(y_test)
        pooled_pred.append(test_pred)
        pooled_zero.append(zero_test)

    if not rows:
        raise ValueError("没有任何 split 满足最小样本要求，无法评估")
    metrics = pd.DataFrame(rows)
    pooled = {
        "n": float(sum(len(item) for item in pooled_y)),
        "ic": _rank_ic(np.concatenate(pooled_y), np.concatenate(pooled_pred)),
        "r2": _r2(np.concatenate(pooled_y), np.concatenate(pooled_pred)),
        "r2_zero": _r2(np.concatenate(pooled_y), np.concatenate(pooled_zero)),
        "mae": _mae(np.concatenate(pooled_y), np.concatenate(pooled_pred)),
        "mae_zero": _mae(np.concatenate(pooled_y), np.concatenate(pooled_zero)),
    }
    return metrics, pooled, skipped


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
    dataset = build_supervised_dataset(
        feature_panel,
        funding,
        calendar,
        decisions,
        specs,
        cfg,
        clock,
    )
    feature_names = [str(name) for name in dataset.attrs["feature_names"]]
    log.info(
        "数据集 %d 行（输入 %d，删除 %d 行特征缺口），特征 %d 个",
        len(dataset),
        dataset.attrs["n_input_rows"],
        dataset.attrs["n_dropped_rows"],
        len(feature_names),
    )

    metrics, pooled, skipped = evaluate_splits(dataset, splits, feature_names)
    summary = dataset_summary(dataset, feature_names)

    manifest = dataset_manifest(dataset)
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
            "zero_baseline": "train_label_mean",
        },
        "pooled_oos": pooled,
        "limitations": [
            "单一历史：2019–2025 已被本项目研究，不是 untouched holdout",
            "标签是基准市场净收益（含成本/票息/融资/滚动），不是策略超额收益",
            "只报告 OOS 预测力；未做成本后的组合回测，不构成可交易 alpha 证据",
            "标签 5 日持有、每 5 交易日决策，相邻标签仍有部分重叠，IC 独立性有限",
            "Ridge/标准化只在训练窗拟合；测试窗不参与 alpha 选择或任何拟合",
        ],
    }

    metrics_path = out / "ml_baseline_metrics.csv"
    summary_path = out / "ml_dataset_summary.csv"
    manifest_path = out / "ml_dataset_manifest.json"
    dataset_path = out / "ml_dataset.csv"
    metrics.to_csv(metrics_path, index=False, encoding="utf-8-sig")
    summary.to_csv(summary_path, index=False, encoding="utf-8-sig")
    dataset.to_csv(dataset_path, index=True, encoding="utf-8-sig")
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
        "test_r2_zero",
        "test_mae_ridge",
        "test_mae_zero",
    ]
    with pd.option_context("display.width", 220):
        print(metrics[cols].to_string(index=False, float_format=lambda x: f"{x:.6f}"))
    print(
        "\n=== Pooled OOS（不同 split 的测试窗不重叠）===\n"
        f"n={pooled['n']:.0f}  IC={pooled['ic']:.4f}  "
        f"R²={pooled['r2']:.4f}（零基线 {pooled['r2_zero']:.4f}）  "
        f"MAE={pooled['mae']:.2e}（零基线 {pooled['mae_zero']:.2e}）"
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
