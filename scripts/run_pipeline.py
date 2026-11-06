#!/usr/bin/env python3
"""全流程编排脚本（最小链路 → 完整研究）。

用法
----
    # 最小链路：数据 → 热力图
    PYTHONPATH=src python3 scripts/run_pipeline.py --stage data

    # 曲线：逐日拟合 NSS + 残差诊断 + 参数时序
    PYTHONPATH=src python3 scripts/run_pipeline.py --stage curve

    # 全流程
    PYTHONPATH=src python3 scripts/run_pipeline.py --all

设计原则
--------
- **每个 stage 幂等**：重复运行结果相同（不依赖当前时间/随机数）。
- **数据来源单一**：优先用 `data/processed/curve_long.parquet`（数据层产出），
  缺失时回退到 Phase 0 实测数据，并在日志里**明确说明回退**（不允许静默）。
- **产物可定位**：图 → `reports/figures/`，统计表 → `reports/`，都与报告一一对应。
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

# 允许直接 `python3 scripts/xxx.py` 运行（无需先 pip install -e）
_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from ficc.config import Config, get_config  # noqa: E402
from ficc.data.schema import CANONICAL_TENORS, TENOR_LABELS, validate_long_frame  # noqa: E402
from ficc.logging import get_logger  # noqa: E402

log = get_logger("pipeline")


# ---------------------------------------------------------------------------
# 数据加载
# ---------------------------------------------------------------------------
def load_long_frame(
    cfg: Config, *, start: date | None = None, end: date | None = None
) -> pd.DataFrame:
    """加载长表数据：优先数据层正式产物，缺失时回退到 Phase 0 实测数据。

    优先级：
      1. `ficc.data.loader.load_curve_long()`（数据层标准入口，已含去重与契约校验）
      2. `data/raw_spike/...csv`（Phase 0 实测，兜底；仍做去重与冲突检查）
    """
    from ficc.data.loader import load_curve_long

    try:
        df = load_curve_long(cfg=cfg)
        log.info("加载数据层产物: %s", cfg.paths.abs("processed") / "curve_long.parquet")
    except FileNotFoundError:
        df = _load_fallback_csv()
    return _finalize(df, start, end)


def _load_fallback_csv() -> pd.DataFrame:
    """兜底：读 Phase 0 实测长表（仅在数据层产物缺失时使用）。"""
    fallback = _ROOT / "data" / "raw_spike" / "cb_gzqx_standard_terms_2002_2026.csv"
    if not fallback.exists():
        raise FileNotFoundError(
            "找不到任何曲线数据。请先运行 `PYTHONPATH=src python3 scripts/fetch_data.py --offline`，"
            f"或确认 {fallback} 存在。"
        )
    log.warning("数据层产物缺失，回退到 Phase 0 实测数据: %s", fallback)
    raw = pd.read_csv(fallback)
    df = raw.rename(columns={"term_years": "tenor", "yield_pct": "ytm_pct"})
    return df[df["tenor"].isin(CANONICAL_TENORS)][["date", "tenor", "ytm_pct"]]


def _finalize(df: pd.DataFrame, start: date | None, end: date | None) -> pd.DataFrame:
    """日期过滤 + 契约校验（去重与冲突检查由数据层负责，这里只防御性兜底）。"""
    if {"date", "tenor", "ytm_pct"} - set(df.columns):
        raise ValueError(f"数据缺少必要列，实际列: {list(df.columns)}")
    df = df[["date", "tenor", "ytm_pct"]].copy()
    df["date"] = pd.to_datetime(df["date"])

    dup = df.duplicated(subset=["date", "tenor"], keep=False)
    if dup.any():
        conflict = df[dup].groupby(["date", "tenor"])["ytm_pct"].nunique()
        conflict = conflict[conflict > 1]
        if len(conflict) > 0:
            raise ValueError(
                f"发现 {len(conflict)} 组数值冲突的重复记录，需人工核查:\n{conflict.head()}"
            )
        log.info("兜底去重 %d 行（数值一致）", int(df.duplicated(subset=["date", "tenor"]).sum()))
        df = df.drop_duplicates(subset=["date", "tenor"], keep="first")

    if start is not None:
        df = df[df["date"] >= pd.Timestamp(start)]
    if end is not None:
        df = df[df["date"] <= pd.Timestamp(end)]

    out = validate_long_frame(df)
    log.info(
        "数据就绪: %d 行, %d 个交易日, %s → %s, %d 个期限点",
        len(out),
        out["date"].nunique(),
        out["date"].min().date(),
        out["date"].max().date(),
        out["tenor"].nunique(),
    )
    return out


def to_panel(df: pd.DataFrame) -> pd.DataFrame:
    """长表 → 宽表（index=date, columns=tenor, values=ytm_pct）。"""
    panel = df.pivot(index="date", columns="tenor", values="ytm_pct").sort_index()
    return panel.reindex(columns=list(CANONICAL_TENORS))


# ---------------------------------------------------------------------------
# Stage: data
# ---------------------------------------------------------------------------
def stage_data(cfg: Config, *, start: date | None, end: date | None) -> dict[str, Path]:
    from ficc.report.figures import plot_curve_heatmap  # 延迟导入，避免无谓依赖

    df = load_long_frame(cfg, start=start, end=end)
    panel = to_panel(df)

    fig_dir = cfg.paths.abs("reports") / "figures"
    out = plot_curve_heatmap(
        panel,
        fig_dir / "curve_heatmap.png",
        title=f"CGB Yield Curve {panel.index.min():%Y-%m} to {panel.index.max():%Y-%m}",
    )
    log.info("热力图: %s", out)

    # 数据摘要表（供报告直接引用）
    summary = pd.DataFrame(
        {
            "tenor": panel.columns,
            "label": [TENOR_LABELS.get(t, f"{t:g}Y") for t in panel.columns],
            "first_valid": [panel[c].first_valid_index() for c in panel.columns],
            "last_valid": [panel[c].last_valid_index() for c in panel.columns],
            "n_valid": [int(panel[c].notna().sum()) for c in panel.columns],
            "missing_pct": [float(panel[c].isna().mean() * 100) for c in panel.columns],
            "mean_pct": [float(panel[c].mean()) for c in panel.columns],
            "std_pct": [float(panel[c].std()) for c in panel.columns],
        }
    )
    out_csv = cfg.paths.abs("reports") / "curve_summary.csv"
    summary.to_csv(out_csv, index=False, encoding="utf-8-sig")
    log.info("数据摘要: %s", out_csv)
    print(summary.to_string(index=False))
    return {"heatmap": out, "summary": out_csv}


# ---------------------------------------------------------------------------
# Stage: curve
# ---------------------------------------------------------------------------
def stage_curve(
    cfg: Config, *, start: date | None, end: date | None, method: str = "nss"
) -> dict[str, Path]:
    """逐日拟合曲线并做残差诊断。"""
    from ficc.curve.fitting import fit_panel  # 曲线层（由 curve-quant 实现）
    from ficc.report.figures import (
        plot_curve_heatmap,
        plot_param_series,
        plot_residual_by_tenor,
        plot_residual_hist,
    )

    df = load_long_frame(cfg, start=start, end=end)
    panel = to_panel(df)
    fig_dir = cfg.paths.abs("reports") / "figures"
    fig_dir.mkdir(parents=True, exist_ok=True)

    # R07：必须显式透传配置的拟合权重，否则 YAML 改权重不会生效（默认是等权）
    weights = cfg.curve.weights
    log.info("开始逐日拟合（method=%s, weights=%s, %d 个交易日）…", method, weights, len(panel))
    params, residuals = fit_panel(panel, method=method, weights=weights)
    log.info(
        "拟合完成: %d 天, 失败 %d 天",
        len(params),
        int((~params.get("success", pd.Series(True, index=params.index))).sum()),
    )

    artifacts: dict[str, Path] = {}

    # 1) 参数面板（报告与 Dashboard 共用）
    params_csv = cfg.paths.abs("reports") / "curve_fit_params.csv"
    # 把实际使用的 model / weights 写入参数面板，便于回溯"这份产物用的是哪套目标函数"
    params = params.assign(model=method, weights_mode=str(weights))
    params.to_csv(params_csv, encoding="utf-8-sig")
    artifacts["params_csv"] = params_csv

    # 2) 残差统计（保留数值 tenor，方便排序与画图；另加可读标签）
    resid_long = residuals.stack(future_stack=True).rename("residual_bp").reset_index()
    resid_long.columns = ["date", "tenor", "residual_bp"]
    resid_long = resid_long.dropna(subset=["residual_bp"])
    resid_long["tenor_label"] = [
        TENOR_LABELS.get(float(t), f"{float(t):g}Y") for t in resid_long["tenor"]
    ]

    summary = (
        resid_long.groupby(["tenor", "tenor_label"])["residual_bp"]
        .agg(
            n="count",
            mean_bp="mean",
            std_bp="std",
            mae_bp=lambda s: np.abs(s).mean(),
            p95_abs_bp=lambda s: np.abs(s).quantile(0.95),
        )
        .reset_index()
        .sort_values("tenor")
    )
    resid_csv = cfg.paths.abs("reports") / "curve_residual_summary.csv"
    summary.to_csv(resid_csv, index=False, encoding="utf-8-sig")
    artifacts["residual_csv"] = resid_csv
    print("\n残差统计（bp）:")
    print(summary.to_string(index=False, float_format=lambda x: f"{x:.3f}"))

    all_resid = resid_long["residual_bp"].to_numpy()
    log.info(
        "全样本残差: mean=%.2fbp std=%.2fbp MAE=%.2fbp p95(|r|)=%.2fbp",
        all_resid.mean(),
        all_resid.std(),
        np.abs(all_resid).mean(),
        np.percentile(np.abs(all_resid), 95),
    )

    # 3) 图
    pw = plot_param_series(
        params, fig_dir / "curve_params.png", title=f"{method.upper()} Parameters"
    )
    artifacts.update(
        {
            "heatmap": plot_curve_heatmap(
                panel,
                fig_dir / "curve_heatmap.png",
                title=f"CGB Yield Curve {panel.index.min():%Y-%m} to {panel.index.max():%Y-%m}",
            ),
            "residual_hist": plot_residual_hist(all_resid, fig_dir / "curve_residual_hist.png"),
            "residual_by_tenor": plot_residual_by_tenor(
                resid_long, fig_dir / "curve_residual_by_tenor.png"
            ),
            "params": pw,
        }
    )
    for k, v in artifacts.items():
        log.info("产物 %-18s %s", k, v)
    return artifacts


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
STAGES = {
    "data": stage_data,
    "curve": stage_curve,
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="国债曲线研究全流程")
    parser.add_argument("--stage", choices=sorted(STAGES), help="只跑某个 stage")
    parser.add_argument("--all", action="store_true", help="跑全部 stage")
    parser.add_argument(
        "--start", type=date.fromisoformat, default=None, help="起始日期 YYYY-MM-DD"
    )
    parser.add_argument("--end", type=date.fromisoformat, default=None, help="结束日期 YYYY-MM-DD")
    parser.add_argument("--method", default=None, help="曲线拟合方法（ns/nss），默认读配置")
    parser.add_argument("--config", default=None, help="配置文件路径")
    args = parser.parse_args(argv)

    cfg = get_config(args.config)
    cfg.paths.ensure()
    method = args.method or cfg.curve.fit_method

    if not args.all and not args.stage:
        parser.error("请指定 --stage 或 --all")

    stages = sorted(STAGES) if args.all else [args.stage]
    results: dict[str, dict[str, Path]] = {}
    for name in stages:
        log.info("=" * 60)
        log.info("STAGE: %s", name)
        log.info("=" * 60)
        fn = STAGES[name]
        results[name] = (
            fn(cfg, start=args.start, end=args.end)
            if name == "data"
            else fn(cfg, start=args.start, end=args.end, method=method)
        )

    log.info("全部完成。产物目录: %s", cfg.paths.abs("reports"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
