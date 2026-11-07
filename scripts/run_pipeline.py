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
import json
import sys
from datetime import UTC, date, datetime
from pathlib import Path

# 允许直接 `python3 scripts/xxx.py` 运行（无需先 pip install -e）
_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from ficc.config import Config, get_config  # noqa: E402
from ficc.data.schema import CANONICAL_TENORS, TENOR_LABELS, BondStatic, validate_long_frame  # noqa: E402
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
def stage_data(
    cfg: Config,
    *,
    start: date | None = None,
    end: date | None = None,
    out_dir: str | Path | None = None,
    **_kw,
) -> dict[str, Path]:
    from ficc.report.figures import plot_curve_heatmap  # 延迟导入，避免无谓依赖

    df = load_long_frame(cfg, start=start, end=end)
    panel = to_panel(df)

    out_root = _out_dir(cfg, out_dir)
    fig_dir = out_root / "figures"
    fig_dir.mkdir(parents=True, exist_ok=True)
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
    out_csv = out_root / "curve_summary.csv"
    summary.to_csv(out_csv, index=False, encoding="utf-8-sig")
    log.info("数据摘要: %s", out_csv)
    print(summary.to_string(index=False))
    return {"heatmap": out, "summary": out_csv}


# ---------------------------------------------------------------------------
# 拟合产品（自由 / 锚定）的生成与"本次运行"守卫（R12）
# ---------------------------------------------------------------------------
PRODUCTS_META_NAME = "curve_fit_products_meta.json"
# 两套产品 = (fit_mode, param_mode, residuals_mode)；与拟合层约定一致
_FIT_MODES: tuple[tuple[str, str, str], ...] = (
    ("free", "free", "free"),
    ("anchored", "stable", "anchored"),
)


def _out_dir(cfg: Config, out_dir: str | Path | None) -> Path:
    """解析产物目录：显式传入优先，否则用配置里的 reports 目录（R13）。"""
    out = Path(out_dir) if out_dir is not None else cfg.paths.abs("reports")
    out.mkdir(parents=True, exist_ok=True)
    return out


def _desired_products_meta(cfg: Config, panel: pd.DataFrame, method: str) -> dict[str, object]:
    """本次运行要求的指纹：配置 + 方法 + 权重 + 样本区间 + 天数。"""
    return {
        "config_hash": cfg.hash(),
        "method": method,
        "weights": str(cfg.curve.weights),
        "start": str(panel.index.min().date()),
        "end": str(panel.index.max().date()),
        "n_days": len(panel),
    }


def _write_products(cfg: Config, panel: pd.DataFrame, method: str, out: Path) -> dict[str, Path]:
    """生成自由/锚定两套拟合产品，返回 `{key: path}`。

    优先使用曲线层的 `build_fit_products`；若尚未提供，回退到本地写出
    （两条路径的产物文件名/格式一致，见 docs/code.md R12）。
    """
    builder = None
    for mod, name in (
        ("ficc.curve.products", "build_fit_products"),
        ("ficc.curve.fitting", "build_fit_products"),
    ):
        try:
            module = __import__(mod, fromlist=[name])
        except ImportError:
            continue
        builder = getattr(module, name, None)
        if builder is not None:
            break

    if builder is not None:
        files = {k: Path(v) for k, v in builder(cfg, panel, method=method, out_dir=out).items()}
        return files

    log.warning("曲线层未提供 build_fit_products，改用 pipeline 内置写出（R12 临时路径）")
    from ficc.curve.fitting import fit_panel

    files: dict[str, Path] = {}
    for fit_mode, param_mode, residuals_mode in _FIT_MODES:
        params, residuals = fit_panel(
            panel,
            method=method,
            weights=cfg.curve.weights,
            param_mode=param_mode,
            residuals_mode=residuals_mode,
        )
        p_path = out / f"curve_fit_params_{method}_{fit_mode}.csv"
        r_path = out / f"curve_fit_residuals_{method}_{fit_mode}.csv"
        f_path = out / f"curve_fit_fitted_{method}_{fit_mode}.csv"
        params.to_csv(p_path, encoding="utf-8-sig")
        residuals.to_csv(r_path, encoding="utf-8-sig")
        (panel + residuals / 100.0).to_csv(f_path, encoding="utf-8-sig")
        files[f"params_{method}_{fit_mode}"] = p_path
        files[f"residuals_{method}_{fit_mode}"] = r_path
        files[f"fitted_{method}_{fit_mode}"] = f_path
    return files


def _ensure_products(
    cfg: Config,
    panel: pd.DataFrame,
    method: str,
    out: Path,
    *,
    force: bool = False,
) -> dict[str, Path]:
    """确保"本次配置 + 本次区间"的产品存在；不匹配则重新生成（禁止消费旧产物）。

    R12：`bias` 以前直接读一个来源不明的残差 CSV。这里用 meta 指纹保证
    "产物 ↔ 配置 ↔ 区间"一致；预置的错误日期/权重旧文件会被识别并重建。
    """
    out.mkdir(parents=True, exist_ok=True)
    meta_path = out / PRODUCTS_META_NAME
    want = _desired_products_meta(cfg, panel, method)

    if not force and meta_path.exists():
        try:
            have = json.loads(meta_path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            have = {}
        same_fingerprint = all(have.get(k) == want[k] for k in want)
        files_ok = bool(have.get("files")) and all(Path(p).exists() for p in have["files"].values())
        if same_fingerprint and files_ok:
            log.info("复用本次配置的拟合产品（指纹匹配）: %s", meta_path)
            return {k: Path(v) for k, v in have["files"].items()}
        log.warning(
            "已有拟合产品与当前配置/区间不一致（R12 守卫），将重新生成：%s",
            {k: (have.get(k), want[k]) for k in want if have.get(k) != want[k]},
        )

    files = _write_products(cfg, panel, method, out)
    meta = {
        **want,
        "generated_at": datetime.now(UTC).isoformat(timespec="seconds"),
        "files": {k: str(v) for k, v in files.items()},
    }
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    log.info("已生成拟合产品与指纹: %s", meta_path)
    return files


# ---------------------------------------------------------------------------
# Stage: curve
# ---------------------------------------------------------------------------
def stage_curve(
    cfg: Config,
    *,
    start: date | None = None,
    end: date | None = None,
    method: str = "nss",
    out_dir: str | Path | None = None,
    **_kw,
) -> dict[str, Path]:
    """逐日拟合曲线，**当次生成**自由/锚定两套产品并做残差诊断（R12）。"""
    from ficc.report.figures import (
        plot_curve_heatmap,
        plot_param_series,
        plot_residual_by_tenor,
        plot_residual_hist,
    )

    out = _out_dir(cfg, out_dir)
    fig_dir = out / "figures"
    fig_dir.mkdir(parents=True, exist_ok=True)

    df = load_long_frame(cfg, start=start, end=end)
    panel = to_panel(df)
    log.info(
        "开始逐日拟合（method=%s, weights=%s, %d 个交易日, %s → %s）…",
        method,
        cfg.curve.weights,
        len(panel),
        panel.index.min().date(),
        panel.index.max().date(),
    )

    files = _ensure_products(cfg, panel, method, out, force=True)
    log.info(
        "两套产品已就绪: %s",
        sorted(k for k in files if k.startswith("residuals_")),
    )

    # 汇总参数面板（兼容旧的单文件消费方，指向自由拟合）
    params = pd.read_csv(files[f"params_{method}_free"], index_col=0, parse_dates=True)
    params.assign(model=method, weights_mode=str(cfg.curve.weights)).to_csv(
        out / "curve_fit_params.csv", encoding="utf-8-sig"
    )

    # 残差统计（用自由拟合，代表模型表达能力）
    residuals = pd.read_csv(files[f"residuals_{method}_free"], index_col=0, parse_dates=True)
    residuals.columns = pd.Index([float(c) for c in residuals.columns], name="tenor")
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
    resid_csv = out / "curve_residual_summary.csv"
    summary.to_csv(resid_csv, index=False, encoding="utf-8-sig")

    all_resid = resid_long["residual_bp"].to_numpy()
    log.info(
        "自由拟合残差: mean=%.2fbp std=%.2fbp MAE=%.2fbp p95(|r|)=%.2fbp",
        all_resid.mean(),
        all_resid.std(),
        np.abs(all_resid).mean(),
        np.percentile(np.abs(all_resid), 95),
    )

    artifacts: dict[str, Path] = dict(files)
    artifacts["params_csv"] = out / "curve_fit_params.csv"
    artifacts["residual_csv"] = resid_csv
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
            "params": plot_param_series(
                params, fig_dir / "curve_params.png", title=f"{method.upper()} Parameters (free)"
            ),
        }
    )
    for k, v in artifacts.items():
        log.info("产物 %-22s %s", k, v)
    return artifacts


# ---------------------------------------------------------------------------
# Stage: factors（即期曲线对比 + PCA + 因子交叉校验）
# ---------------------------------------------------------------------------
def stage_factors(
    cfg: Config,
    *,
    start: date | None = None,
    end: date | None = None,
    out_dir: str | Path | None = None,
    **_kw,
) -> dict[str, Path]:
    """调用曲线层的报告构建器（显式传 cfg / 面板 / 日期边界 / 输出目录）。"""
    from ficc.curve.reporting import build_reports

    out = _out_dir(cfg, out_dir)
    panel = to_panel(load_long_frame(cfg, start=start, end=end))
    files = build_reports(cfg=cfg, panel=panel, start=start, end=end, out_dir=out)
    return {k: Path(v) for k, v in files.items()}


# ---------------------------------------------------------------------------
# Stage: bias（各期限拟合残差的持续性 / 10Y 系统性偏差）
# ---------------------------------------------------------------------------
def stage_bias(
    cfg: Config,
    *,
    start: date | None = None,
    end: date | None = None,
    method: str = "nss",
    out_dir: str | Path | None = None,
    **_kw,
) -> dict[str, Path]:
    """用**本次生成**的自由拟合残差做偏差持续性分析（R12：不消费来源不明的旧文件）。"""
    from ficc.curve.bias import run_bias

    out = _out_dir(cfg, out_dir)
    panel = to_panel(load_long_frame(cfg, start=start, end=end))
    products = _ensure_products(cfg, panel, method, out, force=False)
    residuals_csv = products[f"residuals_{method}_free"]
    log.info("bias 输入残差（本次指纹校验通过）: %s", residuals_csv)
    files = run_bias(
        cfg=cfg,
        residuals_csv=residuals_csv,
        panel=panel,
        out_dir=out,
        start=start,
        end=end,
    )
    return {k: Path(v) for k, v in files.items()}


# ---------------------------------------------------------------------------
# Stage: krd（即期曲线 KRD，含与 YTM DV01 的交叉校验）
# ---------------------------------------------------------------------------
def _par_bond(settlement: date, tenor: float, coupon_pct: float, freq: int) -> BondStatic:
    """构造用于 KRD 的合成平价债（与 bootstrap 的平价假设一致：票面=该期限 YTM、价格 100）。"""
    years = int(tenor)
    maturity = date(settlement.year + years, settlement.month, settlement.day)
    return BondStatic(
        code=f"SYN-{tenor:g}Y-{settlement:%Y%m%d}",
        issue_date=settlement,
        maturity_date=maturity,
        coupon_pct=coupon_pct,
        freq=freq,
    )


def stage_krd(
    cfg: Config,
    *,
    start: date | None = None,
    end: date | None = None,
    out_dir: str | Path | None = None,
    **_kw,
) -> dict[str, Path]:
    """在真实曲线上计算 KRD，并与 YTM 口径 DV01 交叉校验（R17：统一结算日）。"""
    from ficc.curve.bootstrap import bootstrap_spot
    from ficc.curve.curve import Curve
    from ficc.pricing.analytics import dv01, key_rate_durations

    out = _out_dir(cfg, out_dir)
    panel = to_panel(load_long_frame(cfg, start=start, end=end))
    as_of = panel.index.max()
    key_tenors = [float(t) for t in cfg.risk.key_tenors_krd]
    rows: list[dict[str, object]] = []

    def _curve_at(d: pd.Timestamp) -> Curve:
        y = panel.loc[d].reindex(CANONICAL_TENORS)
        return Curve(
            as_of=d.date(),
            tenors=np.asarray(CANONICAL_TENORS),
            rates=tuple(y.values),
            method="hermite",
        )

    def _krd_case(case: str, d: pd.Timestamp, bond: BondStatic, ytm_pct: float) -> None:
        curve = _curve_at(d)
        spot = bootstrap_spot(curve)
        # R17：结算日必须来自曲线本身（观察日 + T+N 工作日），不能拿观察日顶替
        settlement = getattr(spot, "settlement_date", None) or d.date()
        if settlement <= d.date():
            raise ValueError(f"{case}: 结算日({settlement}) 必须晚于观察日({d.date()})")
        krd = key_rate_durations(bond, settlement, spot, key_tenors=key_tenors)
        d01 = dv01(bond, settlement, ytm_pct)
        total = sum(krd.values())
        rel = abs(total + d01) / abs(d01) * 100 if d01 else float("nan")
        for k, v in krd.items():
            rows.append(
                {
                    "case": case,
                    "observation_date": d.date(),
                    "settlement_date": settlement,
                    "key_tenor_y": k,
                    "krd": v,
                    "sum_krd": total,
                    "dv01_ytm": -d01,
                    "rel_diff_pct": rel,
                }
            )

    # case 1：最新观察日上的合成平价债（2/5/10/30Y）
    for tenor in (2.0, 5.0, 10.0, 30.0):
        freq = 2 if tenor >= 10 else 1
        coupon = float(_curve_at(as_of).rate(tenor))
        bond = _par_bond(as_of.date(), tenor, coupon, freq)
        _krd_case(f"par{tenor:g}Y@{as_of.date()}", as_of, bond, coupon)

    # case 2：真实个券 26 附息国债 10（10Y / 1.72% / 半年付）@ 2026-05-28
    settle_ref = pd.Timestamp(date(2026, 5, 28))
    if settle_ref in panel.index:
        bond = BondStatic(
            code="260010.IB",
            issue_date=date(2026, 5, 15),
            maturity_date=date(2036, 5, 15),
            coupon_pct=1.72,
            freq=2,
        )
        _krd_case("260010.IB@2026-05-28", settle_ref, bond, 1.72)

    table = pd.DataFrame(rows)
    out_csv = out / "krd_table.csv"
    table.to_csv(out_csv, index=False, encoding="utf-8-sig")

    summary = (
        table.groupby("case")
        .agg(
            observation_date=("observation_date", "first"),
            settlement_date=("settlement_date", "first"),
            sum_krd=("sum_krd", "first"),
            dv01_ytm=("dv01_ytm", "first"),
            rel_diff_pct=("rel_diff_pct", "first"),
            n_buckets=("krd", "size"),
        )
        .reset_index()
        .sort_values("case")
    )
    print("\nKRD 与 DV01 交叉校验（相对差 <2% 为口径可接受）:")
    print(summary.to_string(index=False, float_format=lambda x: f"{x:.6f}"))
    log.info("KRD 明细: %s", out_csv)
    return {"krd_table": out_csv}


# ---------------------------------------------------------------------------
# Stage: carry（真实曲线上的 carry / roll-down 横截面）
# ---------------------------------------------------------------------------
def stage_carry(
    cfg: Config,
    *,
    start: date | None = None,
    end: date | None = None,
    out_dir: str | Path | None = None,
    **kw,
) -> dict[str, Path]:
    """在真实历史曲线上计算 carry/roll 横截面（只做分解，不产生任何信号/仓位）。"""
    from ficc.curve.curve import Curve
    from ficc.data.loader import load_funding_series
    from ficc.pricing.carry import carry_roll_down

    out = _out_dir(cfg, out_dir)
    panel = to_panel(load_long_frame(cfg, start=start, end=end))
    funding = load_funding_series(cfg=cfg).set_index("date")

    sample_start = (
        pd.Timestamp(start) if start is not None else pd.Timestamp(cfg.backtest.start_date)
    )
    sample_end = (
        pd.Timestamp(end)
        if end is not None
        else (pd.Timestamp(cfg.backtest.end_date) if cfg.backtest.end_date else panel.index.max())
    )
    dates = panel.loc[sample_start:sample_end].index
    # 每 20 个交易日采样一次，够看横截面与时间序列，又不会把 pipeline 拖慢
    sample = dates[::20]

    rows: list[dict[str, object]] = []
    for d in sample:
        y = panel.loc[d].reindex(CANONICAL_TENORS)
        if y.isna().any():
            continue
        curve = Curve(
            as_of=d.date(),
            tenors=np.asarray(CANONICAL_TENORS),
            rates=tuple(y.values),
            method="hermite",
        )
        repo = funding["FDR007"].asof(d) if len(funding) else None
        if repo is None or pd.isna(repo):
            continue
        for tenor in (2.0, 5.0, 10.0, 30.0):
            try:
                res = carry_roll_down(
                    tenor, curve, horizon_days=1, funding_rate_pct=float(repo), cfg=cfg
                )
            except Exception as exc:
                log.warning("carry 计算失败 %s %sY: %s", d.date(), tenor, exc)
                continue
            rows.append({"date": d, "tenor_y": tenor, "funding_pct": float(repo), **res})

    table = pd.DataFrame(rows)
    out_csv = out / "carry_roll_table.csv"
    table.to_csv(out_csv, index=False, encoding="utf-8-sig")
    if not table.empty:
        summary = (
            table.groupby("tenor_y")[["carry_annual_pct", "roll_annual_pct", "total_annual_pct"]]
            .mean()
            .reset_index()
        )
        print("\ncarry / roll 年化均值（%，真实曲线 + FDR007 融资）:")
        print(summary.to_string(index=False, float_format=lambda x: f"{x:.3f}"))
    log.info("carry/roll 明细: %s", out_csv)
    return {"carry_table": out_csv}


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
STAGES = {
    "data": stage_data,
    "curve": stage_curve,
    "factors": stage_factors,
    "bias": stage_bias,
    "krd": stage_krd,
    "carry": stage_carry,
}
# --all 的执行顺序：数据 → 拟合 → 因子 → 偏差 → KRD → carry
ALL_ORDER = ["data", "curve", "factors", "bias", "krd", "carry"]


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
    parser.add_argument(
        "--out-dir", default=None, help="产物目录（默认读配置 reports 路径；用于隔离运行）"
    )
    args = parser.parse_args(argv)

    cfg = get_config(args.config)
    cfg.paths.ensure()
    method = args.method or cfg.curve.fit_method

    if not args.all and not args.stage:
        parser.error("请指定 --stage 或 --all")

    stages = ALL_ORDER if args.all else [args.stage]
    results: dict[str, dict[str, Path]] = {}
    for name in stages:
        log.info("=" * 60)
        log.info("STAGE: %s", name)
        log.info("=" * 60)
        fn = STAGES[name]
        results[name] = fn(
            cfg,
            start=args.start,
            end=args.end,
            method=method,
            out_dir=args.out_dir,
        )

    out_used = _out_dir(cfg, args.out_dir)
    log.info("全部完成。产物目录: %s（out_dir=%s）", out_used, args.out_dir or "默认")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
