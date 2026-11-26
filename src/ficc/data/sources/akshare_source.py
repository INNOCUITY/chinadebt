"""AKShare 备用数据源（**可选依赖**）。

定位
----
中债官网是主源；AKShare 仅作为交叉验证 / 兜底。它的两个接口都有「静默返回
空表」的坑，必须按实测约束分片：

* ``ak.bond_china_yield``：窗口 **必须 ≤ 1 年**，跨年不报错而是返回 0 行 → 按年分片；
  覆盖 2006-03-01 起，仅 3M/6M/1Y/3Y/5Y/7Y/10Y/30Y 共 8 个期限点。
* ``ak.repo_rate_hist``：窗口 **必须 ≤ 1 个月**，超限抛 ``KeyError: 'frValueMap'``
  → 按月分片；FR001/FR007/FR014 自 2005-01-04 起，FDR 系列自 2017-05-31 起。

工程要求
--------
1. ``import akshare`` 失败**不能**让主流程崩溃：模块导入时降级为
   ``AK_AVAILABLE = False`` 并记 warning，调用时才抛 :class:`SourceError`。
2. 每个分片都检查返回值：空表 / ``None`` → 抛 :class:`SourceError`，绝不静默跳过；
   但 ``repo_rate_hist`` 在覆盖起点之前的历史月份允许为空（记 debug 日志）。
3. akshare 依赖较重且历史上会升级 pandas，**不要装进系统 anaconda**；
   需要时用项目内独立 venv（Phase 0 用的是 ``.venv-spike``）。
"""

from __future__ import annotations

from datetime import date, timedelta
from typing import Any, ClassVar

import pandas as pd

from ficc.config import Config, get_config
from ficc.data.schema import LABEL_TO_TENOR
from ficc.data.sources.base import DataSource, SourceError
from ficc.logging import get_logger

log = get_logger(__name__)

try:  # pragma: no cover - 取决于环境是否安装 akshare
    import akshare as ak

    _AK_IMPORT_ERROR: Exception | None = None
except ImportError as exc:  # pragma: no cover - 无 akshare 环境走这里
    ak = None  # type: ignore[assignment]
    _AK_IMPORT_ERROR = exc
    log.warning("未安装可选依赖 akshare，AKShare 数据源不可用（主源中债官网不受影响）：%s", exc)

#: 是否可用（测试与 loader 用它决定是否走 AKShare）
AK_AVAILABLE: bool = ak is not None

#: akshare ``bond_china_yield`` 的中文列名 → 标准期限（年）
_AK_YIELD_COLUMNS: dict[str, float] = {
    "3月": LABEL_TO_TENOR["3m"],
    "6月": LABEL_TO_TENOR["6m"],
    "1年": LABEL_TO_TENOR["1y"],
    "3年": LABEL_TO_TENOR["3y"],
    "5年": LABEL_TO_TENOR["5y"],
    "7年": LABEL_TO_TENOR["7y"],
    "10年": LABEL_TO_TENOR["10y"],
    "30年": LABEL_TO_TENOR["30y"],
}
_AK_YIELD_LABELS: dict[str, str] = {
    "3月": "3m",
    "6月": "6m",
    "1年": "1y",
    "3年": "3y",
    "5年": "5y",
    "7年": "7y",
    "10年": "10y",
    "30年": "30y",
}

#: akshare ``bond_china_yield`` 可提供的期限（用于 loader 的校验放宽）
AKSHARE_TENORS: tuple[float, ...] = tuple(_AK_YIELD_COLUMNS.values())

#: 回购利率列（与 ``repo_frr_fdr_hist.csv`` 一致）
REPO_COLUMNS: tuple[str, ...] = (
    "date",
    "FR001",
    "FR007",
    "FR014",
    "FDR001",
    "FDR007",
    "FDR014",
)


def _require_akshare(caller: str) -> Any:
    """断言 akshare 可用；不可用时给出可操作的错误信息。"""
    if ak is None:  # pragma: no cover - 无 akshare 环境
        raise SourceError(
            f"{caller} 需要可选依赖 akshare，但当前环境未安装"
            f"（{type(_AK_IMPORT_ERROR).__name__ if _AK_IMPORT_ERROR else 'ImportError'}）。"
            "请在项目独立 venv 内 `pip install akshare`，不要装进系统 anaconda。"
        )
    return ak


def _year_chunks(start: date, end: date) -> list[tuple[date, date]]:
    """把区间切成「按自然年」的闭区间分片（bond_china_yield 要求 ≤1 年窗口）。"""
    chunks: list[tuple[date, date]] = []
    cursor = start
    while cursor <= end:
        year_end = date(cursor.year, 12, 31)
        chunk_end = min(year_end, end)
        chunks.append((cursor, chunk_end))
        cursor = date(cursor.year + 1, 1, 1)
    return chunks


def _month_chunks(start: date, end: date) -> list[tuple[date, date]]:
    """把区间切成「按自然月」的闭区间分片（repo_rate_hist 要求 ≤1 个月窗口）。"""
    chunks: list[tuple[date, date]] = []
    cursor = start
    while cursor <= end:
        if cursor.month == 12:
            month_end = date(cursor.year + 1, 1, 1) - timedelta(days=1)
        else:
            month_end = date(cursor.year, cursor.month + 1, 1) - timedelta(days=1)
        chunk_end = min(month_end, end)
        chunks.append((cursor, chunk_end))
        cursor = chunk_end + timedelta(days=1)
    return chunks


class AkshareSource(DataSource):
    """AKShare 备用数据源。

    Parameters
    ----------
    cfg : Config | None
        项目配置；``None`` 时读 ``config/config.yaml``。
    throttle_s : float
        分片请求间隔（秒）；默认 0.2，避免上游限流。
    """

    name: ClassVar[str] = "akshare"

    def __init__(
        self,
        *,
        cfg: Config | None = None,
        throttle_s: float = 0.2,
        curve_name: str | None = None,
    ) -> None:
        self.cfg = cfg or get_config()
        super().__init__(throttle_s=throttle_s)
        self.curve_name = curve_name or self.cfg.data.curve_name
        self._ak = _require_akshare("AkshareSource")

    @property
    def available(self) -> bool:
        """AKShare 是否可用。"""
        return AK_AVAILABLE

    # ------------------------------------------------------------------
    # 收益率曲线
    # ------------------------------------------------------------------
    def _fetch_year(self, year: int, start: date, end: date) -> pd.DataFrame:
        """抓取单个年度的中债国债曲线（≤1 年窗口）。"""
        ak_module = _require_akshare("AkshareSource.fetch")
        try:
            raw = ak_module.bond_china_yield(
                start_date=start.strftime("%Y%m%d"), end_date=end.strftime("%Y%m%d")
            )
        except Exception as exc:
            raise SourceError(f"AKShare bond_china_yield 抓取 {year} 年失败：{exc!r}") from exc
        if raw is None or len(raw) == 0:
            raise SourceError(
                f"AKShare bond_china_yield 对 {start}~{end} 返回空表（已知跨年窗口会静默返回空，"
                "本方法已按年分片；若仍为空说明上游异常）"
            )
        return self._melt_yield(raw, year=year, start=start, end=end)

    def _melt_yield(self, raw: pd.DataFrame, *, year: int, start: date, end: date) -> pd.DataFrame:
        """宽表 → 长表，并从多曲线中筛出国债曲线。"""
        columns = [str(c) for c in raw.columns]
        name_col = next((c for c in columns if "曲线" in c or "名称" in c), None)
        date_col = next((c for c in columns if "日期" in c or c.lower() == "date"), None)
        if date_col is None or name_col is None:
            raise SourceError(f"AKShare bond_china_yield {year} 年返回列不符合预期：{columns}")
        value_cols = [c for c in columns if c in _AK_YIELD_COLUMNS]
        if not value_cols:
            raise SourceError(
                f"AKShare bond_china_yield {year} 年缺少期限列（期望 {list(_AK_YIELD_COLUMNS)}），"
                f"实际列 {columns}"
            )
        raw = raw.rename(columns={date_col: "date", name_col: "curve_name"})
        mask = raw["curve_name"].astype("string").str.contains("国债", na=False)
        if not mask.any():
            actual = sorted(raw["curve_name"].astype("string").dropna().unique())[:5]
            raise SourceError(f"AKShare bond_china_yield {year} 年没有国债曲线；实际曲线 {actual}")
        raw = raw.loc[mask].copy()
        long = raw.melt(
            id_vars=["date", "curve_name"],
            value_vars=value_cols,
            var_name="term_label",
            value_name="ytm_pct",
        )
        long["tenor"] = long["term_label"].map(_AK_YIELD_COLUMNS)
        long["term_label"] = long["term_label"].map(_AK_YIELD_LABELS)
        long["date"] = pd.to_datetime(long["date"], errors="coerce")
        long["ytm_pct"] = pd.to_numeric(long["ytm_pct"], errors="coerce")
        long = long.loc[long["ytm_pct"].notna() & long["date"].notna()].copy()
        long = long.loc[
            (long["date"].dt.date >= start) & (long["date"].dt.date <= end)
        ].reset_index(drop=True)
        if long.empty:
            raise SourceError(
                f"AKShare bond_china_yield {year} 年筛出国债曲线后为空（{start}~{end}）"
            )
        long["source"] = "akshare"
        return long.loc[:, ["date", "tenor", "ytm_pct", "term_label", "source", "curve_name"]]

    def fetch(
        self,
        start: date | None = None,
        end: date | None = None,
        *,
        force: bool = False,
    ) -> pd.DataFrame:
        """按年分片抓取收益率曲线长表（AKShare 起始覆盖 2006-03-01）。"""
        start = start or date(2006, 3, 1)
        end = end or date.today()
        if start > end:
            raise SourceError(f"起始日期 {start} 晚于结束日期 {end}")
        frames: list[pd.DataFrame] = []
        sources: list[dict[str, Any]] = []
        for chunk_start, chunk_end in _year_chunks(start, end):
            self._throttle()
            df = self._fetch_year(chunk_start.year, chunk_start, chunk_end)
            frames.append(df)
            sources.append(
                {
                    "year": chunk_start.year,
                    "source_url": (
                        "akshare://bond_china_yield"
                        f"?start={chunk_start:%Y%m%d}&end={chunk_end:%Y%m%d}"
                    ),
                    "rows_raw": len(df),
                    "date_min": str(df["date"].min().date()),
                    "date_max": str(df["date"].max().date()),
                }
            )
        merged = pd.concat(frames, ignore_index=True)
        merged = merged.drop_duplicates(subset=["date", "tenor"], keep="last")
        merged = self.normalize_long(
            merged,
            context=f"AKShare bond_china_yield {start}~{end}",
            source="akshare",
            curve_name=self.curve_name,
        )
        merged.attrs["sources"] = sources
        return merged

    # ------------------------------------------------------------------
    # 回购定盘利率（资金面）
    # ------------------------------------------------------------------
    def fetch_repo_rates(
        self,
        start: date | None = None,
        end: date | None = None,
    ) -> pd.DataFrame:
        """按月分片抓取 FR/FDR 回购定盘利率，返回宽表（列见 :data:`REPO_COLUMNS`）。

        Raises
        ------
        SourceError
            所有分片均为空、或返回列不符合预期。
        """
        ak_module = _require_akshare("AkshareSource.fetch_repo_rates")
        # AKShare 1.20.45 起更名；新接口仍返回 date/FR/FDR 百分数列。
        fetch_repo = getattr(ak_module, "interest_rate_repo_hist_cm", None)
        interface_name = "interest_rate_repo_hist_cm"
        if fetch_repo is None:
            fetch_repo = ak_module.repo_rate_hist
            interface_name = "repo_rate_hist"
        log.info("AKShare 资金面实际调用接口：%s", interface_name)
        start = start or date(2005, 1, 1)
        end = end or date.today()
        if start > end:
            raise SourceError(f"起始日期 {start} 晚于结束日期 {end}")
        frames: list[pd.DataFrame] = []
        empty_chunks: list[str] = []
        for chunk_start, chunk_end in _month_chunks(start, end):
            self._throttle()
            # 接口在个别月末会漏末日（实测 2011-01-31）；下一片回看一天补覆盖。
            # 查询跨度仍 <=31 天，且不请求 end 之后的数据。
            query_start = max(start, chunk_start - timedelta(days=1))
            try:
                raw = fetch_repo(
                    start_date=query_start.strftime("%Y%m%d"),
                    end_date=chunk_end.strftime("%Y%m%d"),
                )
            except Exception as exc:
                raise SourceError(
                    f"AKShare repo_rate_hist 抓取 {chunk_start:%Y-%m} 失败：{exc!r}"
                ) from exc
            if raw is None or len(raw) == 0:
                empty_chunks.append(f"{chunk_start:%Y-%m}")
                log.debug("AKShare repo_rate_hist %s 返回空表（覆盖起点前属正常）", chunk_start)
                continue
            frames.append(raw)
        if not frames:
            raise SourceError(
                f"AKShare repo_rate_hist 在 {start}~{end} 全部返回空表；空分片示例 "
                f"{empty_chunks[:6]}"
            )
        merged = pd.concat(frames, ignore_index=True)
        merged["date"] = pd.to_datetime(merged["date"], errors="coerce")
        merged = merged.loc[merged["date"].notna()].copy()
        for column in REPO_COLUMNS[1:]:
            if column not in merged.columns:
                merged[column] = float("nan")
            merged[column] = pd.to_numeric(merged[column], errors="coerce")
        duplicated = merged.loc[merged.duplicated("date", keep=False)]
        if not duplicated.empty:
            conflicts = duplicated.groupby("date")[list(REPO_COLUMNS[1:])].nunique(dropna=False)
            if (conflicts > 1).any().any():
                raise SourceError("资金面重叠分片同日数值冲突，拒绝静默覆盖")
        merged = merged.loc[(merged["date"].dt.date >= start) & (merged["date"].dt.date <= end)]
        merged = (
            merged.drop_duplicates(subset=["date"], keep="last")
            .sort_values("date")
            .reset_index(drop=True)
        )
        if merged.empty:
            raise SourceError("AKShare repo_rate_hist 合并后为空")
        merged = merged.loc[:, list(REPO_COLUMNS)]
        merged.attrs["sources"] = [
            {"source_url": "akshare://repo_rate_hist", "empty_chunks": empty_chunks}
        ]
        return merged


__all__ = [
    "AKSHARE_TENORS",
    "AK_AVAILABLE",
    "REPO_COLUMNS",
    "AkshareSource",
]
