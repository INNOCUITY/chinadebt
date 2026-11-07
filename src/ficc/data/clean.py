"""清洗层：网格对齐、跨文件去重（含冲突校验）、显式记录缺失。

职责边界
--------
* **清洗层负责去重**：官方年度文件存在跨年重叠（如 2024/2025 文件都含
  2024-12-27~31），必须在这里合并去重。
* **契约层负责把关**：:func:`ficc.data.schema.validate_long_frame` 会拒绝任何
  重复 ``(date, tenor)`` 与 NaN 收益率。两层分工不混。

去重纪律（2026-11-06 实测发现的数据问题）
----------------------------------------------
1. 先做**数值一致性校验**：同一 ``(date, tenor)`` 出现不同 ``ytm_pct`` 说明是真正的
   数据冲突，必须抛 :class:`~ficc.data.schema.DataValidationError` 并列出明细。
2. 一致性通过后才去重（保留最后一条），并记录删除了多少行/多少组。
3. **禁止静默填充缺失**：网格上缺失的 ``(date, tenor)`` 只统计、不插值；是否插值
   由建模模块显式决定（见 :mod:`ficc.data.quality` 的缺失报告）。
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from ficc.data.schema import CANONICAL_TENORS, DataValidationError, validate_long_frame
from ficc.logging import get_logger

log = get_logger(__name__)

#: 判定「属于标准网格」的期限容差（年）。官方数值最多到小数点后 2 位，
#: 1e-6 足以吸收浮点误差，又不会把 0.25 与 0.24 混为一谈。
TENOR_ATOL = 1e-6


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class DedupeResult:
    """去重结果。

    Attributes
    ----------
    frame : pandas.DataFrame
        去重后的长表（保持原顺序）。
    rows_in, rows_out : int
        去重前后行数。
    rows_removed : int
        删除的重复行数。
    groups_removed : int
        删除的重复 key 组数。
    rows_involved : int
        参与重复组的**总行数**（例如"51 组 × 2 行 = 102 行"），用于数据治理报告。
    """

    frame: pd.DataFrame
    rows_in: int
    rows_out: int
    rows_removed: int
    groups_removed: int
    rows_involved: int = 0


@dataclass(frozen=True)
class CleanStats:
    """清洗统计（写进 manifest，作为数据治理证据）。"""

    rows_in: int
    rows_out: int
    dropped_nan: int
    dropped_non_positive_tenor: int
    dropped_off_grid: int
    dropped_out_of_range: int
    duplicates_removed: int
    duplicate_conflicts: int
    n_dates: int
    tenors: tuple[float, ...]
    expected_cells: int
    missing_cells: int

    def to_dict(self) -> dict[str, Any]:
        """转成可 JSON 序列化的字典。"""
        return {
            "rows_in": self.rows_in,
            "rows_out": self.rows_out,
            "dropped_nan": self.dropped_nan,
            "dropped_non_positive_tenor": self.dropped_non_positive_tenor,
            "dropped_off_grid": self.dropped_off_grid,
            "dropped_out_of_range": self.dropped_out_of_range,
            "duplicates_removed": self.duplicates_removed,
            "duplicate_conflicts": self.duplicate_conflicts,
            "n_dates": self.n_dates,
            "tenors": [float(t) for t in self.tenors],
            "expected_cells": self.expected_cells,
            "missing_cells": self.missing_cells,
            "missing_rate": round(self.missing_cells / self.expected_cells, 6)
            if self.expected_cells
            else 0.0,
        }


@dataclass(frozen=True)
class CleanResult:
    """清洗结果：``frame`` + ``stats``。"""

    frame: pd.DataFrame
    stats: CleanStats


# ---------------------------------------------------------------------------
# 重复 / 冲突
# ---------------------------------------------------------------------------
def find_duplicate_conflicts(
    df: pd.DataFrame,
    *,
    subset: Sequence[str] = ("date", "tenor"),
) -> pd.DataFrame:
    """找出「同一 key 但收益率取值不一致」的组。

    Returns
    -------
    pandas.DataFrame
        列：``date / tenor / n_values / values / n_rows / sources``。
        无冲突时返回空表（但列齐全）。
    """
    empty = pd.DataFrame(columns=["date", "tenor", "n_values", "values", "n_rows", "sources"])
    if df is None or df.empty:
        return empty
    missing = [c for c in (*subset, "ytm_pct") if c not in df.columns]
    if missing:
        raise DataValidationError(f"重复检测缺少列 {missing}；实际列 {list(df.columns)}")
    work = df.copy()
    work["date"] = pd.to_datetime(work["date"], errors="coerce")
    work = work.loc[work["date"].notna()].copy()
    work["ytm_pct"] = pd.to_numeric(work["ytm_pct"], errors="coerce")
    if work.empty:
        return empty
    group = work.groupby(list(subset), dropna=False, sort=False)
    nunique = group["ytm_pct"].nunique()
    conflict_keys = nunique[nunique > 1]
    if conflict_keys.empty:
        return empty
    key_cols = list(subset)
    marked = work.merge(conflict_keys.rename("_n_values").reset_index(), on=key_cols, how="inner")
    rows: list[dict[str, Any]] = []
    for key, chunk in marked.groupby(key_cols, dropna=False, sort=False):
        if not isinstance(key, tuple):
            key = (key,)
        values = sorted({float(v) for v in chunk["ytm_pct"].dropna().tolist()})
        if len(values) < 2:
            continue
        sources: list[str] = []
        if "source" in chunk.columns:
            sources = sorted({str(s) for s in chunk["source"].dropna().unique()})
        record = dict(zip(subset, key, strict=False))
        record.update(
            {
                "n_values": len(values),
                "values": ";".join(f"{v:.6f}" for v in values),
                "n_rows": len(chunk),
                "sources": ";".join(sources),
            }
        )
        rows.append(record)
    return pd.DataFrame(rows, columns=empty.columns)


def dedupe_long_frame(
    df: pd.DataFrame,
    *,
    subset: Sequence[str] = ("date", "tenor"),
    context: str = "长表",
    keep: str = "last",
) -> DedupeResult:
    """按 ``(date, tenor)`` 去重，**去重前先校验数值一致性**。

    Raises
    ------
    DataValidationError
        同一 key 出现多个不同 ``ytm_pct``（真正的数据冲突），消息含冲突明细。
    """
    if df is None or not isinstance(df, pd.DataFrame):
        raise DataValidationError(f"{context}: 期望 DataFrame，实际 {type(df).__name__}")
    rows_in = len(df)
    conflicts = find_duplicate_conflicts(df, subset=subset)
    if not conflicts.empty:
        detail = conflicts.head(10).to_string(index=False)
        raise DataValidationError(
            f"{context}: 发现 {len(conflicts)} 组 (date, tenor) 收益率冲突，"
            f"拒绝静默去重；明细（最多 10 组）：\n{detail}"
        )
    work = df.copy()
    work["date"] = pd.to_datetime(work["date"], errors="coerce")
    before = len(work)
    key_cols = list(subset)
    group_sizes = work.groupby(key_cols, dropna=False, sort=False).size()
    groups_removed = int((group_sizes > 1).sum())
    rows_involved = int(work.duplicated(subset=key_cols, keep=False).sum())
    deduped = work.drop_duplicates(subset=key_cols, keep=keep).reset_index(drop=True)
    rows_removed = before - len(deduped)
    if rows_removed:
        log.info(
            "%s: 去除跨文件重叠 %d 行（%d 组 (date, tenor)），数值一致、校验通过",
            context,
            rows_removed,
            groups_removed,
        )
    return DedupeResult(
        frame=deduped,
        rows_in=rows_in,
        rows_out=len(deduped),
        rows_removed=int(rows_removed),
        groups_removed=int(groups_removed),
        rows_involved=int(rows_involved),
    )


# ---------------------------------------------------------------------------
# 网格对齐
# ---------------------------------------------------------------------------
def match_canonical_tenors(
    tenors: Iterable[float],
    *,
    grid: Sequence[float] = CANONICAL_TENORS,
    atol: float = TENOR_ATOL,
) -> tuple[np.ndarray, np.ndarray]:
    """把期限数值匹配到标准网格。

    Returns
    -------
    matched : numpy.ndarray
        命中的标准期限值（未命中为 NaN）。
    is_canonical : numpy.ndarray
        布尔掩码，True 表示该期限在网格上。
    """
    values = np.asarray(list(tenors), dtype=float)
    grid_arr = np.asarray(list(grid), dtype=float)
    matched = np.full(values.shape, np.nan, dtype=float)
    for tenor in grid_arr:
        hit = np.isclose(values, tenor, rtol=0.0, atol=atol) & np.isnan(matched)
        matched[hit] = tenor
    return matched, ~np.isnan(matched)


def clean_long_frame(
    df: pd.DataFrame,
    *,
    start: date | None = None,
    end: date | None = None,
    grid: Sequence[float] = CANONICAL_TENORS,
    keep_off_grid: bool = False,
    context: str = "长表",
) -> CleanResult:
    """清洗长表：类型规范化 → 去零/去非网格 → 去重 → 区间裁剪 → 契约校验。

    处理顺序（每一步都计数，便于回溯"数据去哪了"）：

    1. 丢弃 ``date/tenor/ytm_pct`` 为 NaN 的行（计数，不填充）；
    2. 丢弃 ``tenor <= 0``（0D 隔夜点，不是曲线拟合网格点）；
    3. 期限对齐 :data:`~ficc.data.schema.CANONICAL_TENORS`；``keep_off_grid=False``
       时丢弃 1M/2M/9M/4Y/6Y/8Y/9Y/40Y/50Y 等非网格点；
    4. 按 ``(date, tenor)`` 去重，去重前先做冲突校验；
    5. 按 ``[start, end]`` 裁剪；
    6. 用 :func:`~ficc.data.schema.validate_long_frame` 做最终契约校验；
    7. 统计网格应有点数 vs 实际点数（**缺失只记录、不填充**）。

    Parameters
    ----------
    keep_off_grid : bool
        是否保留非网格期限点（默认 False，与 config ``data.tenors`` 一致）。
        保留时 ``missing_cells`` 仍按标准网格统计（用于质量报告）。

    Raises
    ------
    DataValidationError
        缺少必需列、重复 key 数值冲突、最终契约校验失败。
    """
    if df is None or not isinstance(df, pd.DataFrame):
        raise DataValidationError(f"{context}: 期望 DataFrame，实际 {type(df).__name__}")
    if df.empty:
        raise DataValidationError(f"{context}: 输入为空表")
    missing = [c for c in ("date", "tenor", "ytm_pct") if c not in df.columns]
    if missing:
        raise DataValidationError(f"{context}: 缺少必需列 {missing}；实际列 {list(df.columns)}")

    work = df.copy()
    rows_in = len(work)

    work["date"] = pd.to_datetime(work["date"], errors="coerce")
    work["tenor"] = pd.to_numeric(work["tenor"], errors="coerce").astype("float64")
    work["ytm_pct"] = pd.to_numeric(work["ytm_pct"], errors="coerce").astype("float64")
    nan_mask = work["date"].isna() | work["tenor"].isna() | work["ytm_pct"].isna()
    dropped_nan = int(nan_mask.sum())
    if dropped_nan:
        log.warning("%s: 丢弃 %d 行（日期/期限/收益率为 NaN）", context, dropped_nan)
        work = work.loc[~nan_mask].copy()

    non_positive = work["tenor"] <= 0
    dropped_non_positive = int(non_positive.sum())
    if dropped_non_positive:
        log.debug("%s: 丢弃 %d 行 tenor<=0（0D 隔夜点）", context, dropped_non_positive)
        work = work.loc[~non_positive].copy()

    matched, is_canonical = match_canonical_tenors(work["tenor"].to_numpy(), grid=grid)
    dropped_off_grid = 0
    if keep_off_grid:
        work["tenor"] = np.where(is_canonical, matched, work["tenor"].to_numpy())
    else:
        dropped_off_grid = int((~is_canonical).sum())
        if dropped_off_grid:
            off_values = sorted({round(float(v), 4) for v in work.loc[~is_canonical, "tenor"]})
            log.info(
                "%s: 丢弃 %d 行非标准网格期限 %s（保留 %d 个网格点）",
                context,
                dropped_off_grid,
                off_values[:12],
                len(grid),
            )
        work = work.loc[is_canonical].copy()

    deduped = dedupe_long_frame(work, context=f"{context} 去重")
    work = deduped.frame

    dropped_out_of_range = 0
    if start is not None or end is not None:
        day = work["date"].dt.date
        range_mask = pd.Series(True, index=work.index)
        if start is not None:
            range_mask &= day >= start
        if end is not None:
            range_mask &= day <= end
        dropped_out_of_range = int((~range_mask).sum())
        if dropped_out_of_range:
            log.info("%s: 按区间 [%s, %s] 裁剪掉 %d 行", context, start, end, dropped_out_of_range)
        work = work.loc[range_mask].copy()

    if work.empty:
        raise DataValidationError(
            f"{context}: 清洗后为空（rows_in={rows_in}, 区间=[{start}, {end}]）"
        )

    try:
        frame = validate_long_frame(work)
    except DataValidationError as exc:
        raise DataValidationError(f"{context}: 清洗后契约校验失败：{exc}") from exc

    counts = missing_grid_counts(frame, grid=grid)
    stats = CleanStats(
        rows_in=rows_in,
        rows_out=len(frame),
        dropped_nan=dropped_nan,
        dropped_non_positive_tenor=dropped_non_positive,
        dropped_off_grid=dropped_off_grid,
        dropped_out_of_range=dropped_out_of_range,
        duplicates_removed=deduped.rows_removed,
        duplicate_conflicts=0,
        n_dates=int(frame["date"].nunique()),
        tenors=tuple(sorted(float(t) for t in frame["tenor"].unique())),
        expected_cells=counts["expected_cells"],
        missing_cells=counts["missing_cells"],
    )
    log.info(
        "%s: %d → %d 行，%d 个交易日，%d 个期限，缺失网格点 %d/%d",
        context,
        stats.rows_in,
        stats.rows_out,
        stats.n_dates,
        len(stats.tenors),
        stats.missing_cells,
        stats.expected_cells,
    )
    return CleanResult(frame=frame, stats=stats)


def missing_grid_counts(
    df: pd.DataFrame,
    *,
    grid: Sequence[float] = CANONICAL_TENORS,
) -> dict[str, int]:
    """统计标准网格的应有点数 / 实有点数 / 缺失点数（不填充）。

    说明：``expected_cells = 交易日数 × 网格期限数``。缺失包含两类：
    ① 官方停发期限（如 2016–2019 的 2Y）；② 单日个别点缺失。
    """
    if df is None or df.empty:
        return {"expected_cells": 0, "present_cells": 0, "missing_cells": 0}
    dates = pd.to_datetime(df["date"], errors="coerce").dropna()
    n_dates = int(dates.nunique())
    _matched, is_canonical = match_canonical_tenors(
        pd.to_numeric(df["tenor"], errors="coerce").dropna().to_numpy(), grid=grid
    )
    present = int(is_canonical.sum())
    expected = n_dates * len(list(grid))
    return {
        "expected_cells": int(expected),
        "present_cells": present,
        "missing_cells": max(0, int(expected) - present),
    }


__all__ = [
    "CleanResult",
    "CleanStats",
    "DedupeResult",
    "clean_long_frame",
    "dedupe_long_frame",
    "find_duplicate_conflicts",
    "match_canonical_tenors",
    "missing_grid_counts",
]
