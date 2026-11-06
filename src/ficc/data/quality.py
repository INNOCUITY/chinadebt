"""数据质量报告：缺失交易日、跳变、期限覆盖矩阵、重复/重叠检测。

设计原则
--------
1. **只报告、不修复**：质量模块不填充、不删行；修复动作在
   :mod:`ficc.data.clean`（去重/网格对齐）或建模层（显式插值）。
2. **可打印 + 可编程**：每个检查都返回 ``DataFrame``，同时提供
   :meth:`QualityReport.print_report` 输出人类可读表格，便于 CLI/CI 留痕。
3. **交易日历近似**：项目暂未引入银行间节假日表，缺失交易日按"周一至周五
   且当天数据缺失"统计，因此**法定节假日也会出现在缺失清单里**。报告会标注
   "最长连续缺口"，真正的数据缺口通常表现为 2 个以上连续工作日缺失。

用法
----
>>> from ficc.data.quality import run_quality_checks
>>> report = run_quality_checks(curve_long, jump_threshold_bp=20.0)
>>> report.print_report()                     # 打印所有表格
>>> report.jumps.head()                       # 跳变明细 DataFrame
"""

from __future__ import annotations

import argparse
import json
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from ficc.config import get_config
from ficc.data.schema import CANONICAL_TENORS, TENOR_LABELS, DataValidationError
from ficc.logging import get_logger

log = get_logger(__name__)

WEEKDAY_NAMES = ["周一", "周二", "周三", "周四", "周五", "周六", "周日"]


def _tenor_label(tenor: float) -> str:
    """期限标签（用于表格列名）：``0.25`` → ``"3M"``。"""
    for value, label in TENOR_LABELS.items():
        if abs(float(tenor) - value) <= 1e-9:
            return label
    return f"{tenor:g}Y"


# ---------------------------------------------------------------------------
# 单项检查
# ---------------------------------------------------------------------------
def check_jumps(
    long_df: pd.DataFrame,
    *,
    threshold_bp: float = 20.0,
) -> pd.DataFrame:
    """检测单个期限的日间跳变（|Δy| > threshold_bp）。

    Returns
    -------
    pandas.DataFrame
        列：``date / tenor / term_label / prev_date / prev_ytm_pct / ytm_pct /
        diff_bp / direction``，按 ``diff_bp`` 绝对值降序。
    """
    columns = [
        "date",
        "tenor",
        "term_label",
        "prev_date",
        "prev_ytm_pct",
        "ytm_pct",
        "diff_bp",
        "direction",
    ]
    if long_df is None or long_df.empty:
        return pd.DataFrame(columns=columns)
    work = long_df.loc[:, ["date", "tenor", "ytm_pct"]].copy()
    work["date"] = pd.to_datetime(work["date"], errors="raise")
    work = (
        work.drop_duplicates(subset=["date", "tenor"], keep="last")
        .sort_values(["tenor", "date"])
        .reset_index(drop=True)
    )
    work["prev_ytm_pct"] = work.groupby("tenor")["ytm_pct"].shift(1)
    work["prev_date"] = work.groupby("tenor")["date"].shift(1)
    work["diff_bp"] = (work["ytm_pct"] - work["prev_ytm_pct"]) * 100.0
    hit = work.loc[
        work["prev_ytm_pct"].notna() & (work["diff_bp"].abs() > float(threshold_bp))
    ].copy()
    if hit.empty:
        return pd.DataFrame(columns=columns)
    hit["term_label"] = hit["tenor"].map(_tenor_label)
    hit["direction"] = np.where(hit["diff_bp"] > 0, "上行", "下行")
    hit["prev_date"] = pd.to_datetime(hit["prev_date"])
    hit = hit.loc[:, columns]
    return hit.reindex(hit["diff_bp"].abs().sort_values(ascending=False).index).reset_index(
        drop=True
    )


def tenor_missing_report(
    long_df: pd.DataFrame,
    *,
    grid: Sequence[float] = CANONICAL_TENORS,
) -> pd.DataFrame:
    """每个标准期限的覆盖情况（以数据集自身交易日为分母）。

    Returns
    -------
    pandas.DataFrame
        列：``tenor / term_label / n_present / n_expected / missing / missing_rate /
        first_date / last_date``。
    """
    if long_df is None or long_df.empty:
        return pd.DataFrame(
            columns=[
                "tenor",
                "term_label",
                "n_present",
                "n_expected",
                "missing",
                "missing_rate",
                "first_date",
                "last_date",
            ]
        )
    work = long_df.copy()
    work["date"] = pd.to_datetime(work["date"], errors="raise")
    all_dates = pd.DatetimeIndex(sorted(work["date"].unique()))
    n_expected = len(all_dates)
    rows: list[dict[str, Any]] = []
    for tenor in grid:
        sub = work.loc[np.isclose(work["tenor"].astype(float), float(tenor), atol=1e-6)]
        present_dates = pd.DatetimeIndex(sorted(sub["date"].unique()))
        n_present = len(present_dates)
        rows.append(
            {
                "tenor": float(tenor),
                "term_label": _tenor_label(float(tenor)),
                "n_present": n_present,
                "n_expected": n_expected,
                "missing": int(n_expected - n_present),
                "missing_rate": round((n_expected - n_present) / n_expected, 6)
                if n_expected
                else 0.0,
                "first_date": present_dates.min() if n_present else pd.NaT,
                "last_date": present_dates.max() if n_present else pd.NaT,
            }
        )
    return pd.DataFrame(rows)


def coverage_matrix(
    long_df: pd.DataFrame,
    *,
    grid: Sequence[float] = CANONICAL_TENORS,
) -> pd.DataFrame:
    """期限覆盖矩阵：行 = 年份，列 = 期限标签，值 = 当年有数据的交易日占比。

    返回的 DataFrame 额外包含 ``n_dates``（当年交易日数）列。
    """
    columns = ["n_dates", *[_tenor_label(float(t)) for t in grid]]
    if long_df is None or long_df.empty:
        return pd.DataFrame(columns=columns)
    work = long_df.copy()
    work["date"] = pd.to_datetime(work["date"], errors="raise")
    work["year"] = work["date"].dt.year
    matched, is_canonical = _match_grid(work["tenor"].to_numpy(), grid)
    work = work.loc[is_canonical].copy()
    work["tenor"] = matched[is_canonical]
    rows: dict[int, dict[str, float]] = {}
    for year, chunk in work.groupby("year", sort=True):
        n_dates = int(chunk["date"].nunique())
        record: dict[str, float] = {"n_dates": n_dates}
        for tenor in grid:
            sub = chunk.loc[np.isclose(chunk["tenor"], float(tenor), atol=1e-9)]
            record[_tenor_label(float(tenor))] = (
                round(sub["date"].nunique() / n_dates, 4) if n_dates else np.nan
            )
        rows[int(year)] = record
    frame = pd.DataFrame.from_dict(rows, orient="index")
    frame.index.name = "year"
    return frame.reindex(columns=columns)


def missing_days_report(long_df: pd.DataFrame) -> pd.DataFrame:
    """缺失的「工作日」及连续缺口（节假日也会计入，见模块 docstring）。

    Returns
    -------
    pandas.DataFrame
        列：``date / weekday / gap_id / gap_start / gap_end / gap_days``。
    """
    columns = ["date", "weekday", "gap_id", "gap_start", "gap_end", "gap_days"]
    if long_df is None or long_df.empty:
        return pd.DataFrame(columns=columns)
    dates = pd.DatetimeIndex(sorted(pd.to_datetime(long_df["date"]).unique()))
    if len(dates) == 0:
        return pd.DataFrame(columns=columns)
    all_days = pd.date_range(dates.min(), dates.max(), freq="D")
    weekdays = all_days[all_days.dayofweek < 5]
    missing = weekdays.difference(dates)
    if len(missing) == 0:
        return pd.DataFrame(columns=columns)
    gaps: list[dict[str, Any]] = []
    gap_id = 0
    run_start: pd.Timestamp | None = None
    prev: pd.Timestamp | None = None
    for current in missing:
        if run_start is None:
            run_start = current
            gap_id += 1
        elif prev is not None and weekdays.get_loc(current) - weekdays.get_loc(prev) != 1:
            gaps.extend(_gap_records(run_start, prev, gap_id))
            run_start = current
            gap_id += 1
        prev = current
    if run_start is not None and prev is not None:
        gaps.extend(_gap_records(run_start, prev, gap_id))
    return pd.DataFrame(gaps, columns=columns)


def _gap_records(start: pd.Timestamp, end: pd.Timestamp, gap_id: int) -> list[dict[str, Any]]:
    days = pd.date_range(start, end, freq="D")
    weekdays = days[days.dayofweek < 5]
    n_days = len(weekdays)
    return [
        {
            "date": day,
            "weekday": WEEKDAY_NAMES[day.dayofweek],
            "gap_id": gap_id,
            "gap_start": weekdays[0],
            "gap_end": weekdays[-1],
            "gap_days": n_days,
        }
        for day in weekdays
    ]


def duplicate_report(long_df: pd.DataFrame) -> tuple[pd.DataFrame, dict[str, Any]]:
    """年度文件重叠 / 重复记录检测。

    Returns
    -------
    detail : pandas.DataFrame
        重复 ``(date, tenor)`` 明细：``date / tenor / n_rows / n_values / values /
        consistent``。无重复时为空表。
    summary : dict
        ``duplicate_groups / duplicate_rows / excess_rows / conflict_groups /
        values_consistent / date_min / date_max``。

    说明：中债年度文件跨年重叠是官方导出配置导致的，数值一致属于可安全去重的
    重复；``conflict_groups > 0`` 才是必须报错的真冲突。
    """
    summary: dict[str, Any] = {
        "duplicate_groups": 0,
        "duplicate_rows": 0,
        "excess_rows": 0,
        "conflict_groups": 0,
        "values_consistent": True,
        "date_min": None,
        "date_max": None,
    }
    columns = ["date", "tenor", "n_rows", "n_values", "values", "consistent"]
    if long_df is None or long_df.empty:
        return pd.DataFrame(columns=columns), summary
    work = long_df.copy()
    work["date"] = pd.to_datetime(work["date"], errors="raise")
    group = work.groupby(["date", "tenor"], dropna=False, sort=False)
    sizes = group.size()
    dup_keys = sizes[sizes > 1]
    if dup_keys.empty:
        return pd.DataFrame(columns=columns), summary
    dup_rows = work.merge(dup_keys.rename("n_rows").reset_index(), on=["date", "tenor"])
    detail = (
        group["ytm_pct"]
        .agg(n_values="nunique", values=lambda s: ";".join(f"{v:.6f}" for v in sorted(set(s))))
        .reset_index()
    )
    detail = detail.merge(dup_keys.rename("n_rows").reset_index(), on=["date", "tenor"])
    detail["consistent"] = detail["n_values"] == 1
    detail = detail.loc[
        :, ["date", "tenor", "n_rows", "n_values", "values", "consistent"]
    ].sort_values(["date", "tenor"])
    summary.update(
        duplicate_groups=len(dup_keys),
        duplicate_rows=len(dup_rows),
        excess_rows=int(len(dup_rows) - len(dup_keys)),
        conflict_groups=int((detail["n_values"] > 1).sum()),
        values_consistent=bool((detail["n_values"] == 1).all()),
        date_min=str(pd.to_datetime(dup_rows["date"]).min().date()),
        date_max=str(pd.to_datetime(dup_rows["date"]).max().date()),
    )
    return detail.reset_index(drop=True), summary


def _match_grid(tenors: np.ndarray, grid: Sequence[float]) -> tuple[np.ndarray, np.ndarray]:
    """向量化地把期限匹配到网格（内部工具）。"""
    values = np.asarray(tenors, dtype=float)
    grid_arr = np.asarray(list(grid), dtype=float)
    matched = np.full(values.shape, np.nan, dtype=float)
    for tenor in grid_arr:
        hit = np.isclose(values, tenor, rtol=0.0, atol=1e-6) & np.isnan(matched)
        matched[hit] = tenor
    return matched, ~np.isnan(matched)


# ---------------------------------------------------------------------------
# 汇总报告
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class QualityReport:
    """质量报告汇总（所有明细都是 DataFrame，可直接落盘/画图）。"""

    summary: dict[str, Any]
    jumps: pd.DataFrame
    missing_by_tenor: pd.DataFrame
    missing_days: pd.DataFrame
    coverage: pd.DataFrame
    duplicates: pd.DataFrame

    def to_frames(self) -> dict[str, pd.DataFrame]:
        """返回所有明细表（键名适合做文件名）。"""
        return {
            "jumps": self.jumps,
            "missing_by_tenor": self.missing_by_tenor,
            "missing_days": self.missing_days,
            "coverage": self.coverage,
            "duplicates": self.duplicates,
        }

    def to_dict(self) -> dict[str, Any]:
        """只返回 summary（可 JSON 序列化）。"""
        return dict(self.summary)

    def print_report(
        self,
        *,
        max_rows: int = 20,
        file: Any | None = None,
    ) -> None:
        """打印人类可读的质量报告。"""
        out = file or sys.stdout

        def emit(text: str = "") -> None:
            print(text, file=out)

        emit("=" * 78)
        emit("数据质量报告  Data Quality Report")
        emit("=" * 78)
        for key, value in self.summary.items():
            emit(f"  {key:<28}: {value}")
        emit("")
        emit(
            f"-- 1. 单日跳变 > {self.summary.get('jump_threshold_bp', 20.0)}bp"
            f"（{len(self.jumps)} 条）--"
        )
        emit(self._preview(self.jumps, max_rows))
        emit("")
        emit("-- 2. 各期限覆盖 / 缺失率 --")
        emit(self._preview(self.missing_by_tenor, max_rows))
        emit("")
        emit("-- 3. 期限覆盖矩阵（行=年份，值=当年覆盖占比）--")
        emit(self._preview(self.coverage, max_rows, show_index=True))
        emit("")
        emit("-- 4. 缺失工作日 / 连续缺口（含法定节假日，见 docstring）--")
        gap_view = self.missing_days
        if not gap_view.empty:
            gap_view = gap_view.drop_duplicates(subset=["gap_id"])
        emit(self._preview(gap_view, max_rows))
        emit("")
        emit("-- 5. 年度文件重叠 / 重复检测 --")
        emit(self._preview(self.duplicates, max_rows))
        emit("=" * 78)

    @staticmethod
    def _preview(frame: pd.DataFrame, max_rows: int, *, show_index: bool = False) -> str:
        if frame is None or frame.empty:
            return "  （无）"
        with pd.option_context(
            "display.max_columns", 30, "display.width", 160, "display.max_rows", max_rows
        ):
            text = frame.head(max_rows).to_string(index=show_index)
        if len(frame) > max_rows:
            text += f"\n  ... 共 {len(frame)} 行，仅显示前 {max_rows} 行"
        return text


def run_quality_checks(
    long_df: pd.DataFrame,
    *,
    jump_threshold_bp: float = 20.0,
    grid: Sequence[float] = CANONICAL_TENORS,
    start: date | None = None,
    end: date | None = None,
) -> QualityReport:
    """一次性跑完所有质量检查。

    Parameters
    ----------
    long_df : pandas.DataFrame
        长表（``date / tenor / ytm_pct``）。允许包含重复行——重复会先被
        单独统计进 ``duplicates``，再在后续检查中按最后一条去重。
    jump_threshold_bp : float
        跳变阈值（bp）。
    start, end : date | None
        可选区间过滤。

    Raises
    ------
    DataValidationError
        输入为空表或缺少必需列。
    """
    if long_df is None or not isinstance(long_df, pd.DataFrame):
        raise DataValidationError(f"质量检查：期望 DataFrame，实际 {type(long_df).__name__}")
    if long_df.empty:
        raise DataValidationError("质量检查：输入为空表，无法评估")
    missing = [c for c in ("date", "tenor", "ytm_pct") if c not in long_df.columns]
    if missing:
        raise DataValidationError(f"质量检查：缺少必需列 {missing}；实际列 {list(long_df.columns)}")

    work = long_df.copy()
    work["date"] = pd.to_datetime(work["date"], errors="raise")
    work["tenor"] = pd.to_numeric(work["tenor"], errors="raise")
    work["ytm_pct"] = pd.to_numeric(work["ytm_pct"], errors="raise")
    if start is not None:
        work = work.loc[work["date"].dt.date >= start]
    if end is not None:
        work = work.loc[work["date"].dt.date <= end]
    if work.empty:
        raise DataValidationError(f"质量检查：区间 [{start}, {end}] 内无数据")

    duplicate_detail, duplicate_summary = duplicate_report(work)
    clean_work = work.drop_duplicates(subset=["date", "tenor"], keep="last").reset_index(drop=True)

    jumps = check_jumps(clean_work, threshold_bp=jump_threshold_bp)
    missing_by_tenor = tenor_missing_report(clean_work, grid=grid)
    coverage = coverage_matrix(clean_work, grid=grid)
    missing_days = missing_days_report(clean_work)

    n_dates = int(clean_work["date"].nunique())
    tensors = sorted(float(t) for t in clean_work["tenor"].unique())
    expected_cells = n_dates * len(list(grid))
    present_cells = int(_match_grid(clean_work["tenor"].to_numpy(), grid)[1].sum())
    missing_cells = max(0, expected_cells - present_cells)
    date_min = str(clean_work["date"].min().date())
    date_max = str(clean_work["date"].max().date())
    longest_gap = int(missing_days["gap_days"].max()) if not missing_days.empty else 0
    max_jump = float(jumps["diff_bp"].abs().max()) if not jumps.empty else 0.0

    summary: dict[str, Any] = {
        "rows": len(clean_work),
        "n_dates": n_dates,
        "date_min": date_min,
        "date_max": date_max,
        "n_tenors": len(tensors),
        "tenors": tensors,
        "jump_threshold_bp": float(jump_threshold_bp),
        "n_jumps": len(jumps),
        "max_abs_jump_bp": round(max_jump, 4),
        "expected_cells": int(expected_cells),
        "missing_cells": missing_cells,
        "missing_rate": round(missing_cells / expected_cells, 6) if expected_cells else 0.0,
        "n_missing_weekdays": len(missing_days),
        "n_gap_runs": int(missing_days["gap_id"].nunique()) if not missing_days.empty else 0,
        "longest_gap_weekdays": longest_gap,
        **{f"dup_{k}": v for k, v in duplicate_summary.items()},
    }
    return QualityReport(
        summary=summary,
        jumps=jumps,
        missing_by_tenor=missing_by_tenor,
        missing_days=missing_days,
        coverage=coverage,
        duplicates=duplicate_detail,
    )


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------
def _default_data_path(config_path: str | None) -> Path:
    cfg = get_config(config_path)
    path = cfg.paths.abs("processed") / "curve_long.parquet"
    if path.exists():
        return path
    fallback = cfg.root / "data" / "raw_spike" / "cb_gzqx_standard_terms_2002_2026.csv"
    if fallback.exists():
        log.warning("未找到 %s，回退到 Phase 0 合并长表 %s", path, fallback)
        return fallback
    return path


def _read_any(path: Path) -> pd.DataFrame:
    if path.suffix == ".parquet":
        frame = pd.read_parquet(path)
    elif path.suffix in (".csv", ".txt"):
        frame = pd.read_csv(path)
        rename = {
            "term_years": "tenor",
            "yield_pct": "ytm_pct",
            "日期": "date",
            "标准期限(年)": "tenor",
            "收益率(%)": "ytm_pct",
        }
        frame = frame.rename(columns={k: v for k, v in rename.items() if k in frame.columns})
    else:
        raise ValueError(f"不支持的数据格式：{path}")
    if "date" in frame.columns:
        frame["date"] = pd.to_datetime(frame["date"], errors="raise")
    return frame


def main(argv: Sequence[str] | None = None) -> int:
    """CLI 入口：``python -m ficc.data.quality --config config/config.yaml``。"""
    parser = argparse.ArgumentParser(description="数据质量报告（缺失/跳变/覆盖/重复）")
    parser.add_argument("--data", default=None, help="长表 parquet/csv 路径")
    parser.add_argument("--config", default=None, help="配置文件路径（默认 config/config.yaml）")
    parser.add_argument("--start", default=None, help="起始日期 YYYY-MM-DD")
    parser.add_argument("--end", default=None, help="结束日期 YYYY-MM-DD")
    parser.add_argument("--jump-bp", type=float, default=20.0, help="跳变阈值（bp）")
    parser.add_argument("--max-rows", type=int, default=20, help="每张表最多打印行数")
    parser.add_argument("--json", default=None, help="把摘要写入 JSON 文件")
    args = parser.parse_args(argv)

    data_path = Path(args.data) if args.data else _default_data_path(args.config)
    if not data_path.exists():
        print(
            f"[quality] 未找到数据 {data_path}；请先运行 `python scripts/fetch_data.py --offline`",
            file=sys.stderr,
        )
        return 2
    start = date.fromisoformat(args.start) if args.start else None
    end = date.fromisoformat(args.end) if args.end else None
    try:
        frame = _read_any(data_path)
        report = run_quality_checks(
            frame,
            jump_threshold_bp=args.jump_bp,
            start=start,
            end=end,
        )
    except (DataValidationError, ValueError, OSError) as exc:
        print(f"[quality] 失败：{exc}", file=sys.stderr)
        return 1
    report.print_report(max_rows=args.max_rows)
    if args.json:
        out = Path(args.json)
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(
            json.dumps(report.to_dict(), ensure_ascii=False, indent=2, default=str),
            encoding="utf-8",
        )
        print(f"[quality] 摘要已写入 {out}")
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(main())
