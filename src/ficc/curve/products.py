"""拟合产品落盘与指纹校验（R12 —— pipeline 与诊断脚本共用的唯一实现）。

背景
----
此前 `scripts/fit_diagnostics.py::main()` 私有地写 12 个产品 CSV，pipeline 的
``stage_curve`` 拿不到，导致下游只能读"不知道哪次生成、用了什么配置"的
``curve_fit_residuals_nss_free.csv``（评审 R12）。本模块把生成逻辑收口成库函数：

- :func:`build_fit_products`：逐 (method, fit_mode) 生成
  ``curve_fit_{params,residuals,fitted}_{method}_{mode}.csv`` 与
  ``curve_fit_{method}_meta.json``（指纹）；``include_summary=True`` 时一并生成
  summary / bad_dates；
- :func:`build_fit_summary`：只从已落盘 CSV 生成 ``curve_fit_summary.csv`` 与
  ``curve_fit_bad_dates.csv``（可复用、可重跑）；
- :func:`read_fit_meta` / :func:`validate_fit_meta`：下游（如
  :func:`ficc.curve.bias.run_bias`）在校验"这份残差是不是本次、同配置、同日期
  范围生成"时使用；不一致必须**报错**，不得静默使用旧文件。

meta 指纹字段（固定契约）
------------------------
``config_hash`` / ``method`` / ``weights`` / ``start`` / ``end`` / ``n_days`` /
``generated_at`` / ``files{...}``。``files`` 内既含本 method 的
``free``/``anchored`` 三类产物的绝对路径，也可能含 ``summary``/``bad_dates``。
"""

from __future__ import annotations

import json
from collections.abc import Iterable, Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from ficc.config import Config, get_config
from ficc.curve.fitting import fit_panel
from ficc.logging import get_logger

__all__ = [
    "build_fit_products",
    "build_fit_summary",
    "read_fit_meta",
    "validate_fit_meta",
]

log = get_logger(__name__)

FIT_MODES: tuple[str, ...] = ("free", "anchored")
_PARAM_MODE = {"free": "free", "anchored": "stable"}
_RESID_MODE = {"free": "free", "anchored": "anchored"}
META_PATTERN = "curve_fit_{method}_meta.json"
RMSE_BAD_THRESHOLD_BP: float = 5.0


def _resolve_cfg(cfg: Config | None) -> Config:
    return cfg if cfg is not None else get_config()


def _resolve_out_dir(cfg: Config, out_dir: str | Path | None) -> Path:
    path = Path(out_dir) if out_dir is not None else cfg.paths.abs("reports")
    path.mkdir(parents=True, exist_ok=True)
    return path


def _truncate_panel(
    panel: pd.DataFrame,
    start: str | pd.Timestamp | None,
    end: str | pd.Timestamp | None,
) -> pd.DataFrame:
    if not isinstance(panel, pd.DataFrame):
        raise TypeError(f"panel 必须是 DataFrame，实际 {type(panel).__name__}")
    if panel.empty:
        raise ValueError("panel 为空")
    frame = panel.copy()
    if not isinstance(frame.index, pd.DatetimeIndex):
        frame.index = pd.to_datetime(frame.index, errors="raise")
    if start is not None:
        frame = frame.loc[frame.index >= pd.Timestamp(start)]
    if end is not None:
        frame = frame.loc[frame.index <= pd.Timestamp(end)]
    if frame.empty:
        raise ValueError(f"按 start={start!r} / end={end!r} 截断后 panel 为空")
    return frame.sort_index()


def _write_csv(frame: pd.DataFrame, path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    frame.to_csv(path, encoding="utf-8-sig")
    return path


def _overall_stats(
    params: pd.DataFrame,
    residuals: pd.DataFrame,
    *,
    method: str,
    fit_mode: str,
    cfg: Config,
    panel_start: pd.Timestamp,
    panel_end: pd.Timestamp,
) -> dict[str, Any]:
    rmse = params["rmse_bp"].dropna().to_numpy(dtype=float)
    flat = residuals.to_numpy(dtype=float).ravel()
    flat = flat[np.isfinite(flat)]
    b0 = params["b0"].to_numpy(dtype=float)
    jump = np.abs(np.diff(b0))
    jump = jump[np.isfinite(jump)]
    return {
        "method": method.upper(),
        "fit_mode": fit_mode,
        "weights_mode": str(cfg.curve.weights),
        "config_hash": cfg.hash(),
        "scope": "overall",
        "tenor": np.nan,
        "panel_start": panel_start.date().isoformat(),
        "panel_end": panel_end.date().isoformat(),
        "n_days": float(len(params)),
        "n_obs": float(flat.size),
        "rmse_mean_bp": float(np.mean(rmse)) if rmse.size else np.nan,
        "rmse_median_bp": float(np.median(rmse)) if rmse.size else np.nan,
        "rmse_p95_bp": float(np.percentile(rmse, 95)) if rmse.size else np.nan,
        "rmse_max_bp": float(np.max(rmse)) if rmse.size else np.nan,
        "resid_mean_bp": float(np.mean(flat)) if flat.size else np.nan,
        "resid_std_bp": float(np.std(flat)) if flat.size else np.nan,
        "mae_bp": float(np.mean(np.abs(flat))) if flat.size else np.nan,
        "p95_abs_bp": float(np.percentile(np.abs(flat), 95)) if flat.size else np.nan,
        "n_over_5bp": float(np.sum(rmse > RMSE_BAD_THRESHOLD_BP)),
        "n_fail": float((~params["success"].astype(bool)).sum()) if "success" in params else np.nan,
        "n_unstable": float(params["unstable"].sum()) if "unstable" in params else np.nan,
        "b0_in_range_pct": float(np.mean((b0 >= 0.5) & (b0 <= 8.0)) * 100.0),
        "b0_jump_p99_pp": float(np.percentile(jump, 99)) if jump.size else np.nan,
        "b0_jump_max_pp": float(np.max(jump)) if jump.size else np.nan,
    }


def _tenor_stats(
    residuals: pd.DataFrame,
    *,
    method: str,
    fit_mode: str,
    cfg: Config,
    panel_start: pd.Timestamp,
    panel_end: pd.Timestamp,
    n_days: int,
) -> list[dict[str, Any]]:
    long = residuals.stack().rename("residual_bp").reset_index()
    long.columns = ["date", "tenor", "residual_bp"]
    long = long.dropna(subset=["residual_bp"])
    rows: list[dict[str, Any]] = []
    for tenor, block in long.groupby("tenor"):
        values = block["residual_bp"].to_numpy(dtype=float)
        rows.append(
            {
                "method": method.upper(),
                "fit_mode": fit_mode,
                "weights_mode": str(cfg.curve.weights),
                "config_hash": cfg.hash(),
                "scope": "tenor",
                "tenor": float(tenor),
                "panel_start": panel_start.date().isoformat(),
                "panel_end": panel_end.date().isoformat(),
                "n_days": float(n_days),
                "n_obs": float(values.size),
                "rmse_mean_bp": np.nan,
                "rmse_median_bp": np.nan,
                "rmse_p95_bp": np.nan,
                "rmse_max_bp": np.nan,
                "resid_mean_bp": float(np.mean(values)),
                "resid_std_bp": float(np.std(values, ddof=0)),
                "mae_bp": float(np.mean(np.abs(values))),
                "p95_abs_bp": float(np.percentile(np.abs(values), 95)),
                "n_over_5bp": np.nan,
                "n_fail": np.nan,
                "n_unstable": np.nan,
                "b0_in_range_pct": np.nan,
                "b0_jump_p99_pp": np.nan,
                "b0_jump_max_pp": np.nan,
            }
        )
    return rows


def build_fit_summary(
    cfg: Config | None,
    *,
    out_dir: str | Path | None = None,
    methods: Sequence[str] = ("ns", "nss"),
    fit_modes: Sequence[str] = FIT_MODES,
) -> dict[str, Path]:
    """由**已落盘**的 params/residuals CSV 生成 summary 与 bad_dates。

    读取 ``out_dir`` 下的 ``curve_fit_params_{method}_{mode}.csv`` 与
    ``curve_fit_residuals_{method}_{mode}.csv``；缺文件的 (method, mode) 跳过。
    """
    resolved = _resolve_cfg(cfg)
    directory = _resolve_out_dir(resolved, out_dir)
    rows: list[dict[str, Any]] = []
    bad_frames: list[pd.DataFrame] = []
    for method in methods:
        for fit_mode in fit_modes:
            params_path = directory / f"curve_fit_params_{method}_{fit_mode}.csv"
            residuals_path = directory / f"curve_fit_residuals_{method}_{fit_mode}.csv"
            if not params_path.exists() or not residuals_path.exists():
                continue
            params = pd.read_csv(params_path, index_col=0, parse_dates=True)
            residuals = pd.read_csv(residuals_path, index_col=0, parse_dates=True)
            if params.empty or residuals.empty:
                continue
            panel_start = residuals.index.min()
            panel_end = residuals.index.max()
            rows.append(
                _overall_stats(
                    params,
                    residuals,
                    method=method,
                    fit_mode=fit_mode,
                    cfg=resolved,
                    panel_start=panel_start,
                    panel_end=panel_end,
                )
            )
            rows.extend(
                _tenor_stats(
                    residuals,
                    method=method,
                    fit_mode=fit_mode,
                    cfg=resolved,
                    panel_start=panel_start,
                    panel_end=panel_end,
                    n_days=len(params),
                )
            )
            if fit_mode == "free":
                worst = params.loc[params["rmse_bp"] > RMSE_BAD_THRESHOLD_BP].copy()
                if not worst.empty:
                    worst = worst.sort_values("rmse_bp", ascending=False)
                    worst.insert(0, "method", method.upper())
                    worst.insert(1, "weights_mode", str(resolved.curve.weights))
                    worst.insert(2, "config_hash", resolved.hash())
                    bad_frames.append(worst)
    artifacts: dict[str, Path] = {}
    if not rows:
        raise FileNotFoundError(
            f"{directory} 下没有可用的 curve_fit_params/residuals CSV；请先生成拟合产品"
        )
    summary = pd.DataFrame(rows)
    summary_path = _write_csv(summary, directory / "curve_fit_summary.csv")
    artifacts["summary"] = summary_path
    if bad_frames:
        bad = pd.concat(bad_frames).sort_values("rmse_bp", ascending=False)
        bad.index.name = "date"
        bad_path = _write_csv(bad, directory / "curve_fit_bad_dates.csv")
        artifacts["bad_dates"] = bad_path
    else:
        empty = pd.DataFrame(columns=["date", "method", "rmse_bp"])
        artifacts["bad_dates"] = _write_csv(empty, directory / "curve_fit_bad_dates.csv")
    # 把 summary 路径写回每个存在的 meta
    for method in methods:
        meta_path = directory / META_PATTERN.format(method=method)
        if meta_path.exists():
            _update_meta_files(meta_path, artifacts)
    return artifacts


def build_fit_products(
    cfg: Config | None,
    panel: pd.DataFrame,
    *,
    method: str = "nss",
    out_dir: str | Path | None = None,
    fit_modes: Iterable[str] = FIT_MODES,
    include_summary: bool = True,
    start: str | pd.Timestamp | None = None,
    end: str | pd.Timestamp | None = None,
) -> dict[str, Path]:
    """生成某方法的自由/锚定拟合并落盘（含 meta 指纹），返回产物路径字典。

    Parameters
    ----------
    cfg : Config | None
        配置（``None`` 时读默认配置）；``config_hash`` 写入 meta。
    panel : pandas.DataFrame
        收益率宽表（index=日期，columns=期限年，values=百分数）。
    method : {"ns", "nss"}
        拟合模型。
    out_dir : str | Path | None
        输出目录；默认 ``cfg.paths.abs("reports")``。
    fit_modes : Iterable[str]
        要生成的模式（``free``/``anchored``）。
    include_summary : bool
        是否调用 :func:`build_fit_summary` 一并生成 summary/bad_dates。
    start, end : 可选日期边界
        先截断 panel 再拟合；meta 的 ``start``/``end`` 记录**实际**样本区间。

    Returns
    -------
    dict[str, Path]
        键：``params_{method}_{mode}`` / ``residuals_{method}_{mode}`` /
        ``fitted_{method}_{mode}`` / ``meta``（+ 可选 ``summary`` / ``bad_dates``）。
    """
    resolved = _resolve_cfg(cfg)
    directory = _resolve_out_dir(resolved, out_dir)
    method = str(method).lower()
    if method not in ("ns", "nss"):
        raise ValueError(f"method 必须是 'ns' 或 'nss'，实际 {method!r}")
    modes = tuple(str(mode).lower() for mode in fit_modes)
    unknown = [mode for mode in modes if mode not in FIT_MODES]
    if unknown:
        raise ValueError(f"fit_modes 含未知模式 {unknown}；可选 {FIT_MODES}")

    frame = _truncate_panel(panel, start, end)
    panel_start = frame.index.min()
    panel_end = frame.index.max()
    artifacts: dict[str, Path] = {}
    files: dict[str, Any] = {}
    for mode in modes:
        params, residuals = fit_panel(
            frame,
            method=method,
            weights=resolved.curve.weights,
            param_mode=_PARAM_MODE[mode],
            residuals_mode=_RESID_MODE[mode],
        )
        params_path = _write_csv(params, directory / f"curve_fit_params_{method}_{mode}.csv")
        residuals_path = _write_csv(
            residuals, directory / f"curve_fit_residuals_{method}_{mode}.csv"
        )
        fitted = frame + residuals / 100.0
        fitted_path = _write_csv(fitted, directory / f"curve_fit_fitted_{method}_{mode}.csv")
        artifacts[f"params_{method}_{mode}"] = params_path
        artifacts[f"residuals_{method}_{mode}"] = residuals_path
        artifacts[f"fitted_{method}_{mode}"] = fitted_path
        files[mode] = {
            "params": str(params_path),
            "residuals": str(residuals_path),
            "fitted": str(fitted_path),
        }
    meta = {
        "config_hash": resolved.hash(),
        "method": method,
        "weights": str(resolved.curve.weights),
        "start": panel_start.date().isoformat(),
        "end": panel_end.date().isoformat(),
        "n_days": len(frame),
        "generated_at": datetime.now(UTC).isoformat(),
        "files": files,
    }
    meta_path = directory / META_PATTERN.format(method=method)
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    artifacts["meta"] = meta_path
    log.info(
        "拟合产品已落盘: method=%s, modes=%s, 样本 %s→%s, %d 天, meta=%s",
        method,
        list(modes),
        meta["start"],
        meta["end"],
        meta["n_days"],
        meta_path,
    )
    if include_summary:
        artifacts.update(build_fit_summary(resolved, out_dir=directory))
    return artifacts


def _update_meta_files(meta_path: Path, extra: dict[str, Path]) -> None:
    """把额外产物（summary/bad_dates）路径写回 meta 的 ``files``。"""
    meta = json.loads(meta_path.read_text(encoding="utf-8"))
    files = dict(meta.get("files", {}))
    for key, path in extra.items():
        files[key] = str(path)
    meta["files"] = files
    meta_path.write_text(json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")


# ---------------------------------------------------------------------------
# meta 读取与校验（供下游使用）
# ---------------------------------------------------------------------------
def read_fit_meta(meta_path: str | Path) -> dict[str, Any]:
    """读取 meta JSON 并校验必需字段齐全。"""
    path = Path(meta_path)
    if not path.exists():
        raise FileNotFoundError(f"缺少拟合产品 meta 指纹: {path}")
    meta = json.loads(path.read_text(encoding="utf-8"))
    required = {
        "config_hash",
        "method",
        "weights",
        "start",
        "end",
        "n_days",
        "generated_at",
        "files",
    }
    missing = sorted(required - set(meta))
    if missing:
        raise ValueError(f"meta 缺少必需字段 {missing}: {path}")
    return meta


def _find_meta_for_residuals(residuals_csv: Path) -> Path:
    """在残差文件同目录下找到引用它的 meta（按文件内容匹配，不按命名猜测）。"""
    target = residuals_csv.resolve()
    candidates = sorted(residuals_csv.parent.glob("curve_fit_*_meta.json"))
    matched: list[Path] = []
    for candidate in candidates:
        try:
            meta = read_fit_meta(candidate)
        except (ValueError, FileNotFoundError):
            continue
        residuals_paths = {
            Path(str(entry.get("residuals", ""))).resolve()
            for entry in meta.get("files", {}).values()
            if isinstance(entry, dict)
        }
        if target in residuals_paths:
            matched.append(candidate)
    if not matched:
        raise ValueError(
            f"{residuals_csv} 没有对应的 meta 指纹文件（同目录 curve_fit_*_meta.json）；"
            "无法验证该残差是否本次/同配置生成，拒绝使用。请用 "
            "build_fit_products() 重新生成"
        )
    if len(matched) > 1:
        raise ValueError(f"{residuals_csv} 被多个 meta 引用: {matched}；请清理旧产品")
    return matched[0]


def validate_fit_meta(
    cfg: Config | None,
    residuals_csv: str | Path,
    *,
    start: str | pd.Timestamp | None = None,
    end: str | pd.Timestamp | None = None,
    meta_path: str | Path | None = None,
    residuals: pd.DataFrame | None = None,
) -> dict[str, Any]:
    """校验残差文件与当前 cfg/日期范围一致；不一致**抛异常**（不静默使用）。

    Raises
    ------
    ValueError
        config_hash / weights / 残差实际日期范围 / 请求日期范围与 meta 不符，
        或缺少 meta 指纹。
    """
    resolved = _resolve_cfg(cfg)
    residuals_file = Path(residuals_csv)
    if not residuals_file.exists():
        raise FileNotFoundError(f"残差文件不存在: {residuals_file}")
    meta = read_fit_meta(
        meta_path if meta_path is not None else _find_meta_for_residuals(residuals_file)
    )
    if meta["config_hash"] != resolved.hash():
        raise ValueError(
            f"残差文件 config_hash={meta['config_hash']} 与当前配置 "
            f"config_hash={resolved.hash()} 不一致；拒绝使用旧产物"
        )
    if str(meta.get("weights")) != str(resolved.curve.weights):
        raise ValueError(
            f"残差文件 weights={meta.get('weights')!r} 与当前配置 "
            f"weights={str(resolved.curve.weights)!r} 不一致"
        )
    frame = (
        residuals
        if residuals is not None
        else pd.read_csv(residuals_file, index_col=0, parse_dates=True)
    )
    if frame.empty:
        raise ValueError(f"残差文件为空: {residuals_file}")
    fs, fe = pd.Timestamp(meta["start"]), pd.Timestamp(meta["end"])
    actual_start, actual_end = frame.index.min(), frame.index.max()
    if abs((actual_start - fs).days) > 0 or abs((actual_end - fe).days) > 0:
        raise ValueError(
            f"残差文件实际日期范围 {actual_start.date()}→{actual_end.date()} "
            f"与 meta 记录的 {fs.date()}→{fe.date()} 不符（文件被替换或过期）"
        )
    if len(frame) != int(meta["n_days"]):
        raise ValueError(f"残差文件行数 {len(frame)} 与 meta n_days={meta['n_days']} 不符")
    if start is not None and pd.Timestamp(start) < fs:
        raise ValueError(f"请求 start={start} 早于残差样本起点 {fs.date()}")
    if end is not None and pd.Timestamp(end) > fe:
        raise ValueError(f"请求 end={end} 晚于残差样本终点 {fe.date()}")
    return meta
