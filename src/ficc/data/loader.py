"""数据加载编排层：抓取 / 读缓存 → 解析 → 合并去重 → 校验 → 落盘 + manifest。

流水线
------
::

    [离线] data/raw_spike/chinabond_gzqx/*.xlsx  ─┐
    [在线] 中债官网（快照缓存 data/raw/chinabond/*.xlsx）─┤
                                                      ├→ 解析长表
    [备用] AKShare（可选依赖）────────────────────────┘
                                                      ↓
                            合并年度文件 → 跨文件去重（冲突即报错）
                                                      ↓
                        clean（网格对齐 / 显式缺失） → 契约校验
                                                      ↓
        data/interim/curve_raw.parquet
        data/processed/curve_long.parquet
        data/metadata/manifest.json（sha256 + rows + 日期范围 + 期限）

硬性纪律
--------
1. **写盘前先校验**：行数、日期范围、期限覆盖、NaN、重复全部检查通过才落盘；
   任何一步失败都保留旧文件并抛异常。
2. **失败绝不覆盖旧数据**：中间文件先写临时文件再 ``os.replace``；抓取失败时
   旧 parquet / 旧 manifest 原样保留。
3. **去重先校验一致性**：官方年度文件存在跨年重叠，数值一致才去重；数值冲突
   抛 :class:`~ficc.data.schema.DataValidationError` 并列出明细（见
   :func:`ficc.data.clean.dedupe_long_frame`）。
4. **manifest 记全链路血缘**：每个年度文件一行记录（source_url / fetched_at /
   sha256 / rows / date_min / date_max / tenors），最终数据集再记一份 sha256。

常用入口
--------
>>> result = fetch_curve_data(offline=True)          # 无网络重建
>>> result = fetch_curve_data(start="2024-01-01")    # 在线增量（命中缓存）
>>> curve = load_curve_long()                        # 下游读取
>>> backfill_2y()                                    # 回补 2016–2019 缺失的 2Y
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any

import numpy as np
import pandas as pd

from ficc.config import Config, get_config
from ficc.data.clean import (
    CleanResult,
    DedupeResult,
    clean_long_frame,
    dedupe_long_frame,
    missing_grid_counts,
)
from ficc.data.schema import DataValidationError, validate_long_frame
from ficc.data.sources.akshare_source import AK_AVAILABLE, AkshareSource
from ficc.data.sources.base import SourceError
from ficc.data.sources.chinabond import (
    DEFAULT_CURVE_NAME,
    ChinabondSource,
    parse_official_xlsx,
    parse_spike_csv,
)
from ficc.logging import get_logger

log = get_logger(__name__)

SCHEMA_VERSION = "1.0"
CURVE_DATASET = "curve_long"
CURVE_RAW_DATASET = "curve_raw"
FUNDING_DATASET = "repo_rates"
MANIFEST_FILENAME = "manifest.json"
BACKFILL_REPORT_FILENAME = "backfill_2y_report.json"
SPIKE_CSV_NAME = "cb_gzqx_standard_terms_2002_2026.csv"
REPO_CSV_NAME = "repo_frr_fdr_hist.csv"
CURRENT_POINTER_FILENAME = "current.json"
VERSIONS_DIRNAME = "versions"
SNAPSHOT_ARCHIVE_DIRNAME = "archive"
STAGING_DIRNAME = ".staging"

#: 落盘长表列（顺序固定，保证 parquet 可复现）
CURVE_COLUMNS: tuple[str, ...] = (
    "date",
    "tenor",
    "ytm_pct",
    "term_label",
    "source",
    "curve_name",
)
RAW_COLUMNS: tuple[str, ...] = CURVE_COLUMNS


# ---------------------------------------------------------------------------
# 路径与选项
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class LoadPaths:
    """数据目录集合（默认来自 config ``paths``，测试可全部重定向到 tmp_path）。"""

    raw: Path
    interim: Path
    processed: Path
    metadata: Path
    raw_spike: Path

    @classmethod
    def from_config(
        cls,
        cfg: Config | None = None,
        *,
        raw: str | Path | None = None,
        interim: str | Path | None = None,
        processed: str | Path | None = None,
        metadata: str | Path | None = None,
        raw_spike: str | Path | None = None,
    ) -> LoadPaths:
        """从配置解析目录；显式传入的参数优先（测试用）。"""
        cfg = cfg or get_config()
        root = cfg.root

        def resolve(explicit: str | Path | None, key: str) -> Path:
            if explicit is not None:
                path = Path(explicit)
                return path if path.is_absolute() else root / path
            return cfg.paths.abs(key)

        return cls(
            raw=resolve(raw, "raw"),
            interim=resolve(interim, "interim"),
            processed=resolve(processed, "processed"),
            metadata=resolve(metadata, "metadata"),
            raw_spike=resolve(raw_spike, "raw_spike")
            if raw_spike is not None
            else root / "data" / "raw_spike",
        )

    @property
    def snapshot_dir(self) -> Path:
        """中债官方 xlsx 不可变快照目录。"""
        return self.raw / "chinabond"

    @property
    def curve_interim(self) -> Path:
        return self.interim / "curve_raw.parquet"

    @property
    def curve_processed(self) -> Path:
        return self.processed / "curve_long.parquet"

    @property
    def funding_processed(self) -> Path:
        return self.processed / "repo_rates.parquet"

    @property
    def manifest(self) -> Path:
        """旧版 manifest 路径（向后兼容，仍会随发布同步写一份）。"""
        return self.metadata / MANIFEST_FILENAME

    @property
    def backfill_report(self) -> Path:
        return self.metadata / BACKFILL_REPORT_FILENAME

    @property
    def data_root(self) -> Path:
        """``data/`` 根目录（version 目录与指针都挂在这里）。"""
        return self.processed.parent

    @property
    def versions_dir(self) -> Path:
        """内容寻址的不可变版本目录：``data/versions/<version_id>/``。"""
        return self.data_root / VERSIONS_DIRNAME

    @property
    def current_pointer(self) -> Path:
        """原子发布指针：``data/metadata/current.json``。"""
        return self.metadata / CURRENT_POINTER_FILENAME

    @property
    def staging_root(self) -> Path:
        """整批事务的临时暂存根目录（失败后整体清理）。"""
        return self.data_root / STAGING_DIRNAME

    def ensure(self) -> None:
        """创建所有需要的目录（幂等）。"""
        for path in (
            self.raw,
            self.interim,
            self.processed,
            self.metadata,
            self.snapshot_dir,
            self.versions_dir,
            self.staging_root,
        ):
            path.mkdir(parents=True, exist_ok=True)


@dataclass(frozen=True)
class SourceRecord:
    """manifest 中的一条数据源血缘记录。"""

    year: int | None
    path: str
    source_url: str
    fetched_at: str | None
    sha256: str
    rows: int
    date_min: str | None
    date_max: str | None
    tenors: tuple[float, ...]

    def to_dict(self) -> dict[str, Any]:
        return {
            "year": self.year,
            "path": self.path,
            "source_url": self.source_url,
            "fetched_at": self.fetched_at,
            "sha256": self.sha256,
            "rows": self.rows,
            "date_min": self.date_min,
            "date_max": self.date_max,
            "tenors": [float(t) for t in self.tenors],
        }


@dataclass(frozen=True)
class LoadResult:
    """一次数据构建的结果。"""

    frame: pd.DataFrame
    raw_frame: pd.DataFrame
    clean: CleanResult
    paths: LoadPaths
    manifest: dict[str, Any]
    manifest_path: Path
    records: tuple[SourceRecord, ...]
    duplicate_stats: dict[str, int]
    version_id: str = ""
    version_dir: Path | None = None
    current_pointer_path: Path | None = None
    staleness: dict[str, Any] | None = None
    coverage: dict[str, Any] | None = None

    def summary(self) -> str:
        """一行式摘要（CLI 打印用）。"""
        ds = self.manifest["dataset"]
        return (
            f"{CURVE_DATASET}: {ds['rows']:,} 行 / {ds['n_dates']:,} 交易日 / "
            f"{ds['date_min']} → {ds['date_max']} / {len(ds['tenors'])} 个期限；"
            f"manifest → {self.manifest_path}"
        )


@dataclass(frozen=True)
class _Context:
    """内部运行上下文。"""

    cfg: Config
    paths: LoadPaths
    start: date
    end: date | None
    source: str
    offline: bool
    force: bool
    min_rows: int
    min_dates: int
    write_funding: bool
    allow_coverage_drop: bool = False


# ---------------------------------------------------------------------------
# 通用小工具
# ---------------------------------------------------------------------------
def _utc_now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _to_date(value: str | date | None) -> date | None:
    if value is None:
        return None
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    return date.fromisoformat(str(value))


def _sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _mtime_iso(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(timespec="seconds")


def _rel(path: Path, root: Path) -> str:
    """尽量返回相对项目根的 posix 路径（manifest 可移植）。"""
    try:
        return path.resolve().relative_to(root.resolve()).as_posix()
    except ValueError:
        return str(path)


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """原子写字节（先写临时文件再 replace）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


def _write_parquet_atomic(frame: pd.DataFrame, path: Path) -> None:
    """原子写 parquet（失败不影响旧文件）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        frame.to_parquet(tmp, index=False)
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def _write_json_atomic(payload: Any, path: Path) -> None:
    text = json.dumps(payload, ensure_ascii=False, indent=2, default=str)
    _atomic_write_bytes(path, text.encode("utf-8"))


def _date_span(frame: pd.DataFrame) -> tuple[str | None, str | None]:
    if frame is None or frame.empty:
        return None, None
    dates = pd.to_datetime(frame["date"], errors="coerce").dropna()
    if dates.empty:
        return None, None
    return str(dates.min().date()), str(dates.max().date())


def _tenors_of(frame: pd.DataFrame) -> tuple[float, ...]:
    if frame is None or frame.empty:
        return ()
    return tuple(
        sorted(float(t) for t in pd.to_numeric(frame["tenor"], errors="coerce").dropna().unique())
    )


def _frame_dataset_entry(
    frame: pd.DataFrame,
    path: Path,
    *,
    root: Path,
    name: str,
    **extra: Any,
) -> dict[str, Any]:
    """构造 manifest 里的数据集条目（含 sha256 / 行数 / 日期范围）。

    曲线/原始长表含 ``tenor`` 列时额外记录期限集合；资金面宽表没有 ``tenor``，
    因此只记录日期范围与列名。
    """
    date_min, date_max = _date_span(frame)
    entry: dict[str, Any] = {
        "name": name,
        "path": _rel(path, root),
        "sha256": _sha256_file(path),
        "bytes": int(path.stat().st_size),
        "rows": len(frame),
        "date_min": date_min,
        "date_max": date_max,
        "columns": [str(c) for c in frame.columns],
    }
    if "date" in frame.columns:
        entry["n_dates"] = int(pd.to_datetime(frame["date"]).nunique()) if len(frame) else 0
    if "tenor" in frame.columns:
        entry["tenors"] = [float(t) for t in _tenors_of(frame)]
    entry.update(extra)
    return entry


def _context(
    *,
    cfg: Config,
    paths: LoadPaths,
    start: date,
    end: date | None,
    source: str,
    offline: bool,
    force: bool,
    min_rows: int,
    min_dates: int,
    write_funding: bool,
    allow_coverage_drop: bool = False,
) -> _Context:
    return _Context(
        cfg=cfg,
        paths=paths,
        start=start,
        end=end,
        source=source,
        offline=offline,
        force=force,
        min_rows=min_rows,
        min_dates=min_dates,
        write_funding=write_funding,
        allow_coverage_drop=allow_coverage_drop,
    )


# ---------------------------------------------------------------------------
# 离线 / 在线读取
# ---------------------------------------------------------------------------
def _offline_csv_candidates(paths: LoadPaths) -> list[Path]:
    """离线合并长表 CSV 的可能位置。"""
    return [
        paths.raw_spike / SPIKE_CSV_NAME,
        paths.raw_spike.parent / SPIKE_CSV_NAME,
    ]


def _required_years(ctx: _Context) -> list[int]:
    """根据区间推导需要抓取的年份列表。"""
    end = ctx.end or date.today()
    if ctx.start > end:
        raise DataValidationError(f"start({ctx.start}) 晚于 end({end})")
    return list(range(ctx.start.year, end.year + 1))


def _staging_dir(paths: LoadPaths, tag: str) -> Path:
    """创建唯一 staging 目录（整批事务用；失败后由调用方清理）。"""
    path = paths.staging_root / f"{tag}-{os.getpid()}-{uuid.uuid4().hex[:8]}"
    path.mkdir(parents=True, exist_ok=False)
    return path


def _rmtree_quiet(path: Path | None) -> None:
    if path is None:
        return
    try:
        shutil.rmtree(path, ignore_errors=True)
    except OSError:  # pragma: no cover - 清理失败不影响主流程
        log.debug("清理临时目录失败：%s", path)


def _stage_bytes(staging_root: Path, name: str, data: bytes) -> Path:
    """把待提交的快照字节写入 staging，返回 staged 路径。"""
    path = staging_root / name
    _atomic_write_bytes(path, data)
    return path


def _archive_snapshot(snapshot: Path) -> Path | None:
    """把既有正式快照归档到 ``<snapshot_dir>/archive/<name>.<sha12><suffix>``。"""
    if not snapshot.exists():
        return None
    sha = _sha256_file(snapshot)
    dest = (
        snapshot.parent / SNAPSHOT_ARCHIVE_DIRNAME / f"{snapshot.stem}.{sha[:12]}{snapshot.suffix}"
    )
    if dest.exists():
        return dest
    _atomic_write_bytes(dest, snapshot.read_bytes())
    log.info("归档旧版数据快照：%s", dest)
    return dest


def _commit_pending_snapshots(pending: Sequence[tuple[Path, Path]]) -> list[tuple[str, str]]:
    """把 staging 中的快照原子提交为正式快照（先归档旧版本）。

    Returns
    -------
    list[tuple[str, str]]
        ``[(final_path_relative, archived_path_relative), ...]``，写入 manifest checks。
    """
    committed: list[tuple[str, str]] = []
    for staged, final in pending:
        # 内容未变（例如每轮都从同一 CSV 构建资金面）→ 不重复归档/写盘
        if final.exists() and _sha256_file(final) == _sha256_file(staged):
            continue
        archived = _archive_snapshot(final)
        _atomic_write_bytes(final, staged.read_bytes())
        log.info("提交数据快照：%s（旧版本归档：%s）", final, archived)
        committed.append((str(final), str(archived) if archived is not None else ""))
    return committed


def _snapshot_bytes_and_pending(
    *,
    source_path: Path,
    snapshot: Path,
    staging_root: Path,
    force: bool,
    context: str,
) -> tuple[bytes, Path, list[tuple[Path, Path]]]:
    """决定「参与计算的字节」并返回待提交快照。

    R03 为保证「参与计算的字节 == manifest 指向的字节」，规则为：

    * 正式快照已存在且非 force：**只解析快照字节**；源文件变化只告警并提示
      ``--force``，绝不出现"新数据配旧哈希"；
    * 快照不存在或 force：解析源文件字节，并把同一份字节写入 staging，
      待整批校验通过后再提交为正式快照。

    Returns
    -------
    (data, registered_snapshot, pending)
        data 为实际解析的字节；registered_snapshot 为 manifest 指向的正式快照路径；
        pending 为 ``(staged, final)`` 列表（无需提交时为空）。
    """
    source_bytes = source_path.read_bytes()
    source_sha = _sha256_bytes(source_bytes)
    if snapshot.exists() and not force:
        snapshot_bytes = snapshot.read_bytes()
        snapshot_sha = _sha256_bytes(snapshot_bytes)
        if snapshot_sha != source_sha:
            log.warning(
                "%s：源文件 %s（sha=%s…）与不可变快照 %s（sha=%s…）不一致；"
                "为保证血缘一致，本次仍解析已登记快照。如需采用源文件请加 --force",
                context,
                source_path.name,
                source_sha[:12],
                snapshot.name,
                snapshot_sha[:12],
            )
        return snapshot_bytes, snapshot, []
    staged = _stage_bytes(staging_root, snapshot.name, source_bytes)
    return source_bytes, snapshot, [(staged, snapshot)]


def _load_offline(
    ctx: _Context, staging_root: Path
) -> tuple[list[pd.DataFrame], list[SourceRecord], list[tuple[Path, Path]]]:
    """离线：从 ``data/raw_spike`` 的年度 xlsx（或合并 CSV）重建。

    R03：若 ``data/raw/chinabond/<year>.xlsx`` 不可变快照已存在，则**只解析快照**，
    源文件变化仅告警；保证 manifest 登记的 sha256 就是参与计算的字节（见
    :func:`_snapshot_bytes_and_pending`）。
    """
    search_dirs = [ctx.paths.raw_spike, ctx.paths.raw_spike / "chinabond_gzqx"]
    source_by_year: dict[int, Path] = {}
    for directory in search_dirs:
        if not directory.exists():
            continue
        for path in directory.glob("*.xlsx"):
            if path.stem.isdigit():
                source_by_year.setdefault(int(path.stem), path)
    # 已登记不可变快照也算作离线输入：即便 raw_spike 源文件被清理，仍可复现
    snapshot_years = (
        {int(path.stem) for path in ctx.paths.snapshot_dir.glob("*.xlsx") if path.stem.isdigit()}
        if ctx.paths.snapshot_dir.exists()
        else set()
    )
    available_years = sorted(set(source_by_year) | snapshot_years)
    frames: list[pd.DataFrame] = []
    records: list[SourceRecord] = []
    pending: list[tuple[Path, Path]] = []
    if available_years:
        required = _required_years(ctx)
        missing_years = [year for year in required if year not in available_years]
        if missing_years:
            raise SourceError(
                f"离线缓存缺少年度文件 {missing_years}；已有年份 "
                f"{available_years[0]}–{available_years[-1]}（目录 {ctx.paths.raw_spike}）"
            )
        log.info(
            "离线模式：解析 %d 个官方年度文件（%d–%d）",
            len(available_years),
            available_years[0],
            available_years[-1],
        )
        for year in available_years:
            snapshot = ctx.paths.snapshot_dir / f"{year}.xlsx"
            source_path = source_by_year.get(year)
            if source_path is None:
                # 源文件缺失但快照仍在：直接解析已登记快照（R03 原则）
                log.warning("离线源文件缺失，改用已登记快照：%s", snapshot)
                data, registered, pending_year = snapshot.read_bytes(), snapshot, []
            else:
                data, registered, pending_year = _snapshot_bytes_and_pending(
                    source_path=source_path,
                    snapshot=snapshot,
                    staging_root=staging_root,
                    force=ctx.force,
                    context=f"离线 {year} 年",
                )
            pending.extend(pending_year)
            frame = parse_official_xlsx(data, year=year, curve_name=DEFAULT_CURVE_NAME)
            frames.append(frame)
            records.append(
                SourceRecord(
                    year=year,
                    path=_rel(registered, ctx.paths.raw.parent.parent),
                    source_url=registered.resolve().as_uri(),
                    fetched_at=_mtime_iso(registered) if registered.exists() else _utc_now_iso(),
                    sha256=_sha256_bytes(data),
                    rows=len(frame),
                    date_min=_date_span(frame)[0],
                    date_max=_date_span(frame)[1],
                    tenors=_tenors_of(frame),
                )
            )
        return frames, records, pending

    csv_candidates = [path for path in _offline_csv_candidates(ctx.paths) if path.exists()]
    if not csv_candidates:
        raise SourceError(
            "离线模式找不到任何缓存："
            f"目录 {ctx.paths.raw_spike} 下无 *.xlsx，也没有 {SPIKE_CSV_NAME}；"
            "请先运行 scripts/spike/fetch_chinabond_official.py 或去掉 --offline"
        )
    csv_path = csv_candidates[0]
    log.warning("离线模式未找到年度 xlsx，回退到合并长表 %s", csv_path)
    frame = parse_spike_csv(csv_path)
    frame = frame.loc[
        (frame["date"].dt.date >= ctx.start) & (ctx.end is None or frame["date"].dt.date <= ctx.end)
    ].reset_index(drop=True)
    if frame.empty:
        raise SourceError(f"离线合并长表 {csv_path} 在区间 [{ctx.start}, {ctx.end}] 内无数据")
    years = sorted(frame["date"].dt.year.unique())
    required = _required_years(ctx)
    missing_years = [year for year in required if year not in years]
    if missing_years:
        raise SourceError(
            f"离线合并长表 {csv_path} 缺少年份 {missing_years}（实际 {years[0]}–{years[-1]}）"
        )
    csv_sha = _sha256_file(csv_path)
    records = [
        SourceRecord(
            year=int(year),
            path=_rel(csv_path, ctx.paths.raw.parent.parent),
            source_url=csv_path.resolve().as_uri(),
            fetched_at=_mtime_iso(csv_path),
            sha256=csv_sha,
            rows=int((frame["date"].dt.year == year).sum()),
            date_min=_date_span(frame.loc[frame["date"].dt.year == year])[0],
            date_max=_date_span(frame.loc[frame["date"].dt.year == year])[1],
            tenors=_tenors_of(frame.loc[frame["date"].dt.year == year]),
        )
        for year in years
    ]
    return [frame], records, []


def _load_online_chinabond(
    ctx: _Context, staging_root: Path
) -> tuple[list[pd.DataFrame], list[SourceRecord], list[tuple[Path, Path]]]:
    """在线：中债官网逐年抓取。

    R04/R06 约束
    ------------
    * 历史完整年度：命中 ``data/raw/chinabond`` 不可变快照，不重复下载；
    * 当年文件：按 ``cfg.data.refresh_current_year`` 强制刷新（旧快照归档）；
    * 新下载的 xlsx 先写入 staging，整批校验通过后再统一提交为正式快照，
      避免"抓取中途失败、正式快照已被部分替换"。
    """
    source = ChinabondSource(
        cfg=ctx.cfg,
        cache_dir=ctx.paths.snapshot_dir,
        staging_dir=staging_root / "snapshots",
    )
    (staging_root / "snapshots").mkdir(parents=True, exist_ok=True)
    frames: list[pd.DataFrame] = []
    records: list[SourceRecord] = []
    pending: list[tuple[Path, Path]] = []
    for year in _required_years(ctx):
        frame = source.fetch_year(year, force=ctx.force)
        frames.append(frame)
        attrs = frame.attrs
        registered = ctx.paths.snapshot_dir / f"{year}.xlsx"
        staged_path = Path(str(attrs.get("snapshot_path"))) if attrs.get("snapshot_path") else None
        if staged_path is not None and staged_path != registered and staged_path.exists():
            pending.append((staged_path, registered))
        records.append(
            SourceRecord(
                year=year,
                path=_rel(registered, ctx.paths.raw.parent.parent),
                source_url=str(attrs.get("source_url", "")),
                fetched_at=str(attrs.get("fetched_at", "")) or None,
                sha256=str(attrs.get("sha256") or ""),
                rows=len(frame),
                date_min=_date_span(frame)[0],
                date_max=_date_span(frame)[1],
                tenors=_tenors_of(frame),
            )
        )
    return frames, records, pending


def _load_online_akshare(
    ctx: _Context, staging_root: Path
) -> tuple[list[pd.DataFrame], list[SourceRecord], list[tuple[Path, Path]]]:
    """在线备用：AKShare（可选依赖）。"""
    if not AK_AVAILABLE:
        raise SourceError(
            "AKShare 未安装（可选依赖），无法使用 --source akshare；"
            "主源请用 --source chinabond，或安装 akshare 后重试"
        )
    source = AkshareSource(cfg=ctx.cfg)
    merged = source.fetch(ctx.start, ctx.end)
    frames = [merged]
    records: list[SourceRecord] = []
    pending: list[tuple[Path, Path]] = []
    snapshot_staging = staging_root / "akshare"
    snapshot_staging.mkdir(parents=True, exist_ok=True)
    for item in merged.attrs.get("sources", []):
        year = int(item["year"])
        sub = merged.loc[merged["date"].dt.year == year].reset_index(drop=True)
        staged = snapshot_staging / f"curve_{year}.parquet"
        _write_parquet_atomic(sub, staged)
        final = ctx.paths.raw / "akshare" / f"curve_{year}.parquet"
        pending.append((staged, final))
        records.append(
            SourceRecord(
                year=year,
                path=_rel(final, ctx.paths.raw.parent.parent),
                source_url=str(item.get("source_url", "akshare://bond_china_yield")),
                fetched_at=_mtime_iso(staged),
                sha256=_sha256_file(staged),
                rows=len(sub),
                date_min=_date_span(sub)[0],
                date_max=_date_span(sub)[1],
                tenors=_tenors_of(sub),
            )
        )
    return frames, records, pending


# ---------------------------------------------------------------------------
# 合并 / 去重 / 校验
# ---------------------------------------------------------------------------
def _concat_frames(frames: Sequence[pd.DataFrame], *, context: str) -> pd.DataFrame:
    if not frames:
        raise SourceError(f"{context}: 没有可合并的数据（frames 为空）")
    merged = pd.concat(frames, ignore_index=True)
    if merged.empty:
        raise SourceError(f"{context}: 合并后为空表")
    return merged


def _dedupe_raw(raw: pd.DataFrame, *, context: str) -> tuple[pd.DataFrame, DedupeResult]:
    """跨年度合并后去重：先校验数值一致性，一致才去重。"""
    result = dedupe_long_frame(raw, context=context)
    return result.frame, result


def _filter_range(frame: pd.DataFrame, start: date, end: date | None) -> pd.DataFrame:
    days = pd.to_datetime(frame["date"]).dt.date
    mask = days >= start
    if end is not None:
        mask &= days <= end
    return frame.loc[mask].reset_index(drop=True)


def _known_gap_2y(ctx: _Context) -> tuple[date, date] | None:
    """从配置读取已知 2Y 缺口区间（用于校验豁免）。"""
    for gap in ctx.cfg.data.known_gaps:
        field = str(gap.get("field", ""))
        if field == "2y":
            return _to_date(gap.get("start")), _to_date(gap.get("end"))
    return None


def _validate_curve_dataset(cleaned: CleanResult, ctx: _Context) -> None:
    """落盘前校验：行数 / 日期范围 / 期限覆盖 / 数值完整性。

    Raises
    ------
    DataValidationError
        任何一项不达标都抛错，**调用方不得写盘**（旧文件保留）。
    """
    frame = cleaned.frame
    stats = cleaned.stats
    if frame.empty:
        raise DataValidationError("清洗后数据为空，拒绝落盘")
    if len(frame) < ctx.min_rows:
        raise DataValidationError(
            f"清洗后仅 {len(frame):,} 行 < 阈值 {ctx.min_rows:,} 行，疑似抓取不完整，拒绝落盘"
        )
    if frame["ytm_pct"].isna().any():
        raise DataValidationError("清洗后仍存在 NaN 收益率，拒绝落盘")
    if stats.n_dates < ctx.min_dates:
        raise DataValidationError(
            f"交易日仅 {stats.n_dates} 天 < 阈值 {ctx.min_dates}，疑似抓取不完整，拒绝落盘"
        )

    date_min, date_max = _date_span(frame)
    min_date, max_date = date.fromisoformat(date_min), date.fromisoformat(date_max)
    if min_date < ctx.start:
        raise DataValidationError(f"数据最早日期 {min_date} 早于请求起点 {ctx.start}")
    if min_date > ctx.start + timedelta(days=10):
        raise DataValidationError(
            f"数据最早日期 {min_date} 晚于请求起点 {ctx.start} 超过 10 天，可能整段缺失，拒绝落盘"
        )
    if ctx.end is not None:
        if max_date > ctx.end:
            raise DataValidationError(f"数据最晚日期 {max_date} 晚于请求终点 {ctx.end}")
        if max_date < ctx.end - timedelta(days=10):
            raise DataValidationError(
                f"数据最晚日期 {max_date} 早于请求终点 {ctx.end} 超过 10 天，"
                "可能是最新数据未抓到，拒绝落盘"
            )

    required_tenors = set(float(t) for t in ctx.cfg.data.tenors)
    if ctx.source == "akshare":
        from ficc.data.sources.akshare_source import AKSHARE_TENORS

        required_tenors = set(float(t) for t in AKSHARE_TENORS)
    present = set(stats.tenors)
    missing = sorted(required_tenors - present)
    if missing:
        known = _known_gap_2y(ctx)
        permit_2y_gap = (
            missing == [2.0] and known is not None and min_date >= known[0] and max_date <= known[1]
        )
        if not permit_2y_gap:
            raise DataValidationError(
                f"缺少关键期限 {missing}（已有 {sorted(present)}）；"
                "若区间落在已知 2Y 缺口内请先运行 scripts/backfill_2y.py"
            )

    # 每个期限的最新日期不能明显落后于数据集最后一天（防"某个期限只抓到一半"）
    stale: list[str] = []
    for tenor in sorted(present):
        sub = frame.loc[frame["tenor"] == tenor]
        last = pd.to_datetime(sub["date"]).max().date()
        lag = (max_date - last).days
        if lag <= 30:
            continue
        known = _known_gap_2y(ctx)
        if tenor == 2.0 and known is not None and last >= known[1]:
            continue
        stale.append(f"{tenor:g}Y 最后日期 {last}（落后 {lag} 天）")
    if stale:
        raise DataValidationError("以下期限数据明显滞后，拒绝落盘：" + "；".join(stale))

    log.info(
        "落盘前校验通过：%d 行 / %d 交易日 / %s→%s / 缺失网格点 %d",
        len(frame),
        stats.n_dates,
        date_min,
        date_max,
        stats.missing_cells,
    )


# ---------------------------------------------------------------------------
# 资金面（FR/FDR）辅助数据集（支持在线刷新）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class FundingBuild:
    """资金面构建结果（尚未落盘）。"""

    frame: pd.DataFrame
    source_url: str
    source_sha256: str | None = None
    pending_snapshot: tuple[Path, Path] | None = None


def _normalise_funding(frame: pd.DataFrame) -> pd.DataFrame:
    """规范化资金面宽表：date 解析、数值化、按日去重。"""
    if frame is None or len(frame) == 0:
        raise DataValidationError("资金面序列为空")
    work = frame.copy()
    work["date"] = pd.to_datetime(work["date"], errors="coerce")
    work = work.loc[work["date"].notna()].copy()
    for column in work.columns:
        if column != "date":
            work[column] = pd.to_numeric(work[column], errors="coerce")
    return (
        work.drop_duplicates(subset=["date"], keep="last")
        .sort_values("date")
        .reset_index(drop=True)
    )


def _validate_funding(frame: pd.DataFrame) -> None:
    """资金面数据完整性闸门（失败只跳过资金面，不影响曲线主流程）。"""
    if len(frame) < 100:
        raise DataValidationError(f"资金面序列仅 {len(frame)} 行，疑似不完整")
    if "FR007" not in frame.columns or frame["FR007"].notna().sum() < 50:
        raise DataValidationError("资金面序列缺少足够的 FR007 数据")


def _fetch_funding_online(ctx: _Context, staging_dir: Path) -> FundingBuild | None:
    """在线刷新资金面（AKShare 可选依赖；失败降级本地 CSV）。

    R06：``repo_rate_hist`` 按月分片由 :class:`AkshareSource` 负责；akshare 缺失时
    记 warning 并返回 None，由调用方降级到 Phase 0 CSV。
    """
    if not AK_AVAILABLE:
        log.info("未安装可选依赖 akshare，资金面在线刷新跳过，降级本地 %s", REPO_CSV_NAME)
        return None
    try:
        source = AkshareSource(cfg=ctx.cfg)
        raw = source.fetch_repo_rates(date(2005, 1, 1), ctx.end or date.today())
        frame = _normalise_funding(raw)
        _validate_funding(frame)
        staged = staging_dir / "repo_rates_akshare.parquet"
        _write_parquet_atomic(frame, staged)
        final = ctx.paths.raw / "funding" / "repo_rates_akshare.parquet"
        log.info(
            "资金面在线刷新：%d 行（%s → %s）",
            len(frame),
            frame["date"].min().date(),
            frame["date"].max().date(),
        )
        return FundingBuild(
            frame=frame,
            source_url="akshare://repo_rate_hist",
            source_sha256=_sha256_file(staged),
            pending_snapshot=(staged, final),
        )
    except Exception as exc:
        log.warning("资金面在线刷新失败，降级本地 CSV：%s", exc)
        return None


def _load_funding_csv(ctx: _Context, staging_dir: Path) -> FundingBuild | None:
    """从 Phase 0 落盘的 ``repo_frr_fdr_hist.csv`` 构建资金面（离线/降级路径）。"""
    candidates = [
        ctx.paths.raw_spike / REPO_CSV_NAME,
        ctx.paths.raw_spike.parent / REPO_CSV_NAME,
    ]
    csv_path = next((path for path in candidates if path.exists()), None)
    if csv_path is None:
        log.warning("未找到 %s，跳过资金面数据集", REPO_CSV_NAME)
        return None
    try:
        frame = _normalise_funding(pd.read_csv(csv_path))
        _validate_funding(frame)
        staged = _stage_bytes(staging_dir, REPO_CSV_NAME, csv_path.read_bytes())
        final = ctx.paths.raw / "funding" / REPO_CSV_NAME
        log.info(
            "资金面序列（本地 CSV）：%d 行，%s → %s",
            len(frame),
            frame["date"].min().date(),
            frame["date"].max().date(),
        )
        return FundingBuild(
            frame=frame,
            source_url=csv_path.resolve().as_uri(),
            source_sha256=_sha256_file(csv_path),
            pending_snapshot=(staged, final),
        )
    except Exception as exc:
        log.warning("资金面数据集构建失败（跳过）：%s", exc)
        return None


def _prepare_funding(ctx: _Context, staging_root: Path) -> FundingBuild | None:
    """构建资金面：在线模式先尝试在线刷新，失败/离线则用本地 CSV。"""
    if not ctx.write_funding:
        return None
    staging_dir = staging_root / "funding"
    staging_dir.mkdir(parents=True, exist_ok=True)
    if not ctx.offline:
        online = _fetch_funding_online(ctx, staging_dir)
        if online is not None:
            return online
    return _load_funding_csv(ctx, staging_dir)


# ---------------------------------------------------------------------------
# 数据新鲜度（R06）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class StalenessInfo:
    """数据新鲜度检查结果。"""

    max_date: date | None
    as_of: date
    staleness_days: int
    threshold_days: int
    stale: bool
    offline: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "max_date": self.max_date.isoformat() if self.max_date else None,
            "as_of": self.as_of.isoformat(),
            "staleness_days": int(self.staleness_days),
            "threshold_days": int(self.threshold_days),
            "stale": bool(self.stale),
            "offline": bool(self.offline),
        }


def _check_staleness(
    max_date: date | None,
    *,
    as_of: date | None = None,
    max_staleness_days: int = 5,
    offline: bool = False,
) -> StalenessInfo:
    """检查最新数据是否落后今天超过 ``max_staleness_days``（R06）。

    在线模式超过阈值 → ``logger.warning``（CLI 也打印提示）；离线模式使用固定
    截止日，属于设计内行为，只记 INFO，不告警。
    """
    today = as_of or date.today()
    if max_date is None:
        return StalenessInfo(None, today, 0, int(max_staleness_days), False, offline)
    staleness_days = (today - max_date).days
    stale = staleness_days > int(max_staleness_days)
    info = StalenessInfo(
        max_date=max_date,
        as_of=today,
        staleness_days=staleness_days,
        threshold_days=int(max_staleness_days),
        stale=stale,
        offline=offline,
    )
    if stale:
        if offline:
            log.info(
                "离线模式使用固定截止日 %s（落后今天 %d 天 > 阈值 %d 天），按设计接受",
                max_date,
                staleness_days,
                max_staleness_days,
            )
        else:
            log.warning(
                "数据新鲜度不足：最新数据 %s 落后今天 %d 天 > 阈值 %d 天；"
                "请检查网络/中债发布状态（当年文件已按配置强制刷新）",
                max_date,
                staleness_days,
                max_staleness_days,
            )
    return info


# ---------------------------------------------------------------------------
# 逐年覆盖退化闸门（R05）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class CoverageCheck:
    """逐年覆盖退化检查结果。"""

    baseline_label: str | None
    years: dict[int, dict[str, int]]
    missing_dates: tuple[str, ...]
    allowed: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "baseline": self.baseline_label,
            "years": {str(year): stats for year, stats in self.years.items()},
            "missing_dates": list(self.missing_dates[:20]),
            "n_missing_dates": len(self.missing_dates),
            "allowed": bool(self.allowed),
        }


def _resolve_pointer_path(value: str | Path, root: Path) -> Path:
    """解析 pointer/manifest 中的路径（相对项目根或绝对路径）。"""
    path = Path(value)
    return path if path.is_absolute() else root / path


def _read_current_pointer(paths: LoadPaths) -> dict[str, Any] | None:
    """读取已发布的 ``current.json`` 指针；不存在/损坏时返回 None 并告警。"""
    pointer = paths.current_pointer
    if not pointer.exists():
        return None
    try:
        payload = json.loads(pointer.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as exc:
        log.warning("current.json 读取失败（回退旧路径）：%s", exc)
        return None
    if not isinstance(payload, dict) or "version_id" not in payload:
        log.warning("current.json 内容不符合预期（回退旧路径）：%s", pointer)
        return None
    return payload


def _previous_curve_frame(paths: LoadPaths, cfg: Config) -> tuple[pd.DataFrame | None, str | None]:
    """读取上一版本曲线（覆盖退化基线）：优先版本指针，其次旧版路径。"""
    pointer = _read_current_pointer(paths)
    if pointer is not None:
        entry = pointer.get("dataset") or {}
        path_value = entry.get("path")
        if path_value:
            path = _resolve_pointer_path(path_value, cfg.root)
            if path.exists():
                try:
                    return pd.read_parquet(path), f"version:{pointer['version_id']}"
                except Exception as exc:
                    log.warning("上一版本曲线读取失败，回退旧路径：%s", exc)
    if paths.curve_processed.exists():
        try:
            return pd.read_parquet(
                paths.curve_processed
            ), "legacy:data/processed/curve_long.parquet"
        except Exception as exc:
            log.warning("旧版曲线读取失败，跳过覆盖退化检查：%s", exc)
    return None, None


def _check_coverage_regression(
    new_frame: pd.DataFrame,
    previous_frame: pd.DataFrame | None,
    *,
    baseline_label: str | None,
    allow: bool,
) -> CoverageCheck:
    """逐年覆盖退化检查：历史交易日集合只允许新增，不允许消失（R05）。

    说明：以**交易日集合**而非仅总行数判定——整年历史被压缩时总行数仍可能过阈值，
    但日期集合必然缺失；该检查不需要节假日日历即可保护已存在历史。已知 2Y 缺口
    不影响日期集合（当天其他期限仍在），因此不会被误报。
    """
    if previous_frame is None or previous_frame.empty:
        return CoverageCheck(None, {}, (), True)
    new_dates = set(pd.to_datetime(new_frame["date"]).dt.date)
    previous_dates = set(pd.to_datetime(previous_frame["date"]).dt.date)
    if not new_dates:
        raise DataValidationError("新版本没有任何交易日，拒绝发布")
    # 上界取新/旧最大日期的较大值：即使新版本把最后几天截掉，也必须能检测到；
    # 下界用新版本起点（显式收窄 start 属于范围变更，不视为退化）。
    low = min(new_dates)
    high = max(max(new_dates), max(previous_dates))
    baseline_dates = {day for day in previous_dates if low <= day <= high}
    missing = sorted(baseline_dates - new_dates)
    years: dict[int, dict[str, int]] = {}
    for year in sorted({day.year for day in baseline_dates} | {day.year for day in new_dates}):
        baseline_count = sum(1 for day in baseline_dates if day.year == year)
        new_count = sum(1 for day in new_dates if day.year == year)
        if baseline_count or new_count:
            years[year] = {
                "baseline": baseline_count,
                "new": new_count,
                "drop": baseline_count - new_count,
            }
    allowed = bool(allow) or not missing
    if missing and not allow:
        detail = "；".join(
            f"{year}年 基线{stats['baseline']}→新{stats['new']}（-{stats['drop']}）"
            for year, stats in years.items()
            if stats["drop"] > 0
        )
        sample = ", ".join(str(day) for day in missing[:8])
        raise DataValidationError(
            f"逐年覆盖退化检查失败（基线 {baseline_label}）：历史交易日消失 {len(missing)} 天；"
            f"{detail}；缺失日期示例 {sample}。如确需删除历史请显式传 --allow-coverage-drop"
        )
    if missing and allow:
        log.warning(
            "覆盖退化已被显式允许：历史交易日消失 %d 天（基线 %s）", len(missing), baseline_label
        )
    return CoverageCheck(
        baseline_label=baseline_label,
        years=years,
        missing_dates=tuple(str(day) for day in missing),
        allowed=allowed,
    )


# ---------------------------------------------------------------------------
# manifest
# ---------------------------------------------------------------------------
def _artifact_entry(
    frame: pd.DataFrame,
    artifact_path: Path,
    *,
    root: Path,
    output_dir: Path,
    name: str,
    **extra: Any,
) -> dict[str, Any]:
    """构造版本目录中的数据集条目：哈希取自实际文件，路径指向发布目录。"""
    entry = _frame_dataset_entry(frame, artifact_path, root=root, name=name, **extra)
    entry["path"] = _rel(output_dir / artifact_path.name, root)
    return entry


def _build_manifest(
    ctx: _Context,
    *,
    cleaned: CleanResult,
    raw_frame: pd.DataFrame,
    records: Sequence[SourceRecord],
    dedupe: DedupeResult,
    funding: FundingBuild | None,
    coverage: CoverageCheck | None,
    output_dir: Path | None = None,
    staging_dir: Path | None = None,
    version_id: str | None = None,
) -> dict[str, Any]:
    """构造 manifest。

    ``staging_dir`` 为文件实际写入位置（用于计算 sha256），``output_dir`` 为最终
    发布目录（manifest 中的路径指向这里）。两者分离是为了在发布前就写出带正确
    路径与哈希的 manifest。
    """
    root = ctx.cfg.root
    frame = cleaned.frame
    stats = cleaned.stats
    counts = missing_grid_counts(frame)
    known_gap = _known_gap_2y(ctx)
    known_gap_cells = 0
    if known_gap is not None:
        gap_days = frame["date"].dt.date
        in_gap = gap_days.between(known_gap[0], known_gap[1])
        known_gap_cells = int(in_gap.sum())
    file_dir = staging_dir if staging_dir is not None else ctx.paths.processed
    dest_dir = output_dir if output_dir is not None else ctx.paths.processed
    curve_file = (
        (file_dir / "curve_long.parquet") if staging_dir is not None else ctx.paths.curve_processed
    )
    interim_file = (
        (file_dir / "curve_raw.parquet") if staging_dir is not None else ctx.paths.curve_interim
    )

    funding_entry: dict[str, Any] | None = None
    if funding is not None:
        funding_file = (
            file_dir / "repo_rates.parquet"
            if staging_dir is not None
            else ctx.paths.funding_processed
        )
        funding_entry = _artifact_entry(
            funding.frame,
            funding_file,
            root=root,
            output_dir=dest_dir,
            name=FUNDING_DATASET,
        )
        funding_entry["source_url"] = funding.source_url
        funding_entry["source_sha256"] = funding.source_sha256

    manifest: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _utc_now_iso(),
        "generator": "ficc.data.loader.fetch_curve_data",
        "params": {
            "source": ctx.source,
            "offline": ctx.offline,
            "force": ctx.force,
            "start": ctx.start.isoformat(),
            "end": ctx.end.isoformat() if ctx.end else None,
            "min_rows": ctx.min_rows,
            "min_dates": ctx.min_dates,
            "curve_name": DEFAULT_CURVE_NAME,
            "allow_coverage_drop": bool(ctx.allow_coverage_drop),
        },
        "dataset": _artifact_entry(
            frame,
            curve_file,
            root=root,
            output_dir=dest_dir,
            name=CURVE_DATASET,
            missing_cells=int(stats.missing_cells),
            expected_cells=int(stats.expected_cells),
            missing_rate=round(stats.missing_cells / stats.expected_cells, 6)
            if stats.expected_cells
            else 0.0,
            known_gap_2y={
                "start": known_gap[0].isoformat() if known_gap else None,
                "end": known_gap[1].isoformat() if known_gap else None,
                "rows_in_range": known_gap_cells,
            },
        ),
        "interim": _artifact_entry(
            raw_frame,
            interim_file,
            root=root,
            output_dir=dest_dir,
            name=CURVE_RAW_DATASET,
        ),
        "funding": funding_entry,
        "sources": [record.to_dict() for record in records],
        "coverage_by_year": {str(year): int(count) for year, count in _year_counts(frame).items()},
        "checks": {
            "rows_raw_before_dedupe": int(dedupe.rows_in),
            "rows_raw_merged": len(dedupe.frame),
            "rows_removed_cross_file_duplicates": int(dedupe.rows_removed),
            "duplicate_groups": int(dedupe.groups_removed),
            "duplicate_rows_in_files": int(dedupe.rows_involved),
            "duplicate_conflicts": 0,
            **stats.to_dict(),
            "required_tenors": [float(t) for t in ctx.cfg.data.tenors],
            "present_tenors": [float(t) for t in stats.tenors],
            "missing_grid_cells": int(counts["missing_cells"]),
            "expected_grid_cells": int(counts["expected_cells"]),
            "coverage_regression": coverage.to_dict() if coverage is not None else None,
        },
    }
    if version_id is not None:
        manifest["version_id"] = version_id
    return manifest


# ---------------------------------------------------------------------------
# 版本目录 + 指针发布（R04：整批原子事务）
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class StagedVersion:
    """已写入 staging、尚未发布的版本。"""

    staging_dir: Path
    final_dir: Path
    version_id: str
    manifest: dict[str, Any]
    files: dict[str, Path]
    hashes: dict[str, str]


@dataclass(frozen=True)
class PublishedVersion:
    """已发布版本（版本目录 + 指针）。"""

    version_id: str
    version_dir: Path
    current_pointer: Path
    manifest_path: Path
    reused: bool
    pointer: dict[str, Any]


def _version_id(hashes: dict[str, str]) -> str:
    """版本 ID = 内容哈希（曲线 + 原始长表 + 资金面 + schema 版本）。"""
    payload = "|".join(
        [
            SCHEMA_VERSION,
            hashes.get("curve_long", ""),
            hashes.get("curve_raw", ""),
            hashes.get("repo_rates", ""),
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:16]


def _stage_version(
    ctx: _Context,
    *,
    curve_frame: pd.DataFrame,
    raw_frame: pd.DataFrame,
    funding: FundingBuild | None,
    manifest_factory: Callable[[Path, str, dict[str, Path]], dict[str, Any]],
) -> StagedVersion:
    """把所有产物写入 staging 版本目录，计算内容哈希并写 manifest。

    staging 目录在失败时由本函数清理；成功由 :func:`_publish_version` 原子改名发布。
    """
    staging = _staging_dir(ctx.paths, "version")
    try:
        files: dict[str, Path] = {}
        curve_path = staging / "curve_long.parquet"
        _write_parquet_atomic(curve_frame.loc[:, list(CURVE_COLUMNS)], curve_path)
        files["curve_long"] = curve_path
        raw_path = staging / "curve_raw.parquet"
        _write_parquet_atomic(raw_frame, raw_path)
        files["curve_raw"] = raw_path
        if funding is not None:
            funding_path = staging / "repo_rates.parquet"
            _write_parquet_atomic(funding.frame, funding_path)
            files["repo_rates"] = funding_path
        hashes = {key: _sha256_file(path) for key, path in files.items()}
        version_id = _version_id(hashes)
        final_dir = ctx.paths.versions_dir / version_id
        manifest = manifest_factory(final_dir, version_id, dict(files))
        manifest["version_id"] = version_id
        manifest["artifact_hashes"] = dict(hashes)
        manifest_path = staging / "manifest.json"
        _write_json_atomic(manifest, manifest_path)
        files["manifest"] = manifest_path
        # 发布前回读校验：manifest 登记的哈希必须等于实际写出的字节（R03/R04）
        dataset = manifest.get("dataset") or {}
        interim = manifest.get("interim") or {}
        if dataset.get("sha256") != hashes.get("curve_long"):
            raise DataValidationError("staging 曲线文件哈希与 manifest 登记不一致，拒绝发布")
        if interim.get("sha256") != hashes.get("curve_raw"):
            raise DataValidationError("staging 原始长表哈希与 manifest 登记不一致，拒绝发布")
        if funding is not None and (manifest.get("funding") or {}).get("sha256") != hashes.get(
            "repo_rates"
        ):
            raise DataValidationError("staging 资金面文件哈希与 manifest 登记不一致，拒绝发布")
        return StagedVersion(
            staging_dir=staging,
            final_dir=final_dir,
            version_id=version_id,
            manifest=manifest,
            files=files,
            hashes=hashes,
        )
    except BaseException:
        _rmtree_quiet(staging)
        raise


def _publish_legacy(ctx: _Context, version_dir: Path, manifest: dict[str, Any]) -> None:
    """把版本产物同步到旧路径（向后兼容直接读 ``data/processed`` 的脚本）。"""
    pairs: list[tuple[str, Path]] = [
        ("curve_long.parquet", ctx.paths.curve_processed),
        ("curve_raw.parquet", ctx.paths.curve_interim),
    ]
    if manifest.get("funding"):
        pairs.append(("repo_rates.parquet", ctx.paths.funding_processed))
    for name, dest in pairs:
        source = version_dir / name
        if source.exists():
            _atomic_write_bytes(dest, source.read_bytes())
    _write_json_atomic(manifest, ctx.paths.manifest)


def _restore_legacy(ctx: _Context, previous_pointer: dict[str, Any] | None) -> None:
    """指针发布失败时，把旧路径兼容副本回滚到上一版本（R04）。"""
    if previous_pointer is None:
        return
    root = ctx.cfg.root
    try:
        mapping = [
            ("dataset", "curve_long.parquet", ctx.paths.curve_processed),
            ("interim", "curve_raw.parquet", ctx.paths.curve_interim),
        ]
        for key, _name, dest in mapping:
            entry = previous_pointer.get(key) or {}
            if not entry.get("path"):
                continue
            source = _resolve_pointer_path(entry["path"], root)
            if source.exists():
                _atomic_write_bytes(dest, source.read_bytes())
        funding_entry = previous_pointer.get("funding") or {}
        if funding_entry.get("path"):
            source = _resolve_pointer_path(funding_entry["path"], root)
            if source.exists():
                _atomic_write_bytes(ctx.paths.funding_processed, source.read_bytes())
        manifest_path = previous_pointer.get("manifest_path")
        if manifest_path:
            source = _resolve_pointer_path(manifest_path, root)
            if source.exists():
                _cert = json.loads(source.read_text(encoding="utf-8"))
                _write_json_atomic(_cert, ctx.paths.manifest)
        log.warning(
            "指针发布失败：旧路径兼容副本已回滚到上一版本 %s", previous_pointer.get("version_id")
        )
    except Exception as exc:
        log.warning("旧路径兼容副本回滚失败（指针未变，读者仍读旧版本）：%s", exc)


def _build_pointer(
    ctx: _Context,
    staged: StagedVersion,
    *,
    manifest_path: Path,
) -> dict[str, Any]:
    """构造 ``current.json`` 指针内容（只含发布所需信息）。"""
    manifest = staged.manifest
    return {
        "schema_version": SCHEMA_VERSION,
        "version_id": staged.version_id,
        "published_at": _utc_now_iso(),
        "generator": manifest.get("generator", "ficc.data.loader"),
        "dataset": manifest.get("dataset"),
        "interim": manifest.get("interim"),
        "funding": manifest.get("funding"),
        "manifest_path": _rel(manifest_path, ctx.cfg.root),
        "coverage_by_year": manifest.get("coverage_by_year", {}),
        "checks": {
            key: manifest.get("checks", {}).get(key)
            for key in ("n_dates", "expected_cells", "missing_cells", "coverage_regression")
        },
    }


def _publish_version(ctx: _Context, staged: StagedVersion) -> PublishedVersion:
    """原子发布：版本目录改名 → 旧路径兼容副本 → 原子替换 ``current.json``。

    任一步失败时 ``current.json`` 保持旧值；旧路径副本会尽力回滚，因此读者只会
    看到完整旧版本或完整新版本（R04）。
    """
    paths = ctx.paths
    previous_pointer = _read_current_pointer(paths)
    final_dir = staged.final_dir
    reused = final_dir.exists()
    if reused:
        for key in ("curve_long", "curve_raw"):
            expected = staged.hashes.get(key)
            candidate = final_dir / staged.files[key].name
            if not candidate.exists() or _sha256_file(candidate) != expected:
                raise DataValidationError(
                    f"版本目录 {final_dir} 已存在但内容与本次构建不一致，拒绝复用"
                )
        _rmtree_quiet(staged.staging_dir)
    else:
        paths.versions_dir.mkdir(parents=True, exist_ok=True)
        os.replace(staged.staging_dir, final_dir)
    manifest_path = final_dir / "manifest.json"
    try:
        _publish_legacy(ctx, final_dir, staged.manifest)
        pointer = _build_pointer(ctx, staged, manifest_path=manifest_path)
        _write_json_atomic(pointer, paths.current_pointer)
    except BaseException:
        _restore_legacy(ctx, previous_pointer)
        raise
    log.info(
        "发布数据版本：version_id=%s（复用=%s）→ %s",
        staged.version_id,
        reused,
        paths.current_pointer,
    )
    return PublishedVersion(
        version_id=staged.version_id,
        version_dir=final_dir,
        current_pointer=paths.current_pointer,
        manifest_path=manifest_path,
        reused=reused,
        pointer=pointer,
    )


def _year_counts(frame: pd.DataFrame) -> dict[int, int]:
    """按年份统计交易日数（用于 manifest/覆盖检查）。"""
    if frame is None or frame.empty:
        return {}
    days = pd.to_datetime(frame["date"]).dt.year
    return {int(year): int(count) for year, count in days.value_counts().sort_index().items()}


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------
def fetch_curve_data(
    *,
    start: str | date | None = None,
    end: str | date | None = None,
    source: str = "chinabond",
    offline: bool = False,
    force: bool = False,
    cfg: Config | None = None,
    paths: LoadPaths | None = None,
    raw_spike_dir: str | Path | None = None,
    min_rows: int = 1000,
    min_dates: int = 100,
    write_funding: bool = True,
    allow_coverage_drop: bool = False,
) -> LoadResult:
    """构建曲线数据集并发布为不可变版本（``data/versions/<version_id>/`` + 指针）。

    与旧实现的差异（R03/R04/R05/R06）
    ---------------------------------
    * **R03 血缘一致**：离线模式若已有不可变快照，永远解析快照字节；源文件变化
      只告警并提示 ``--force``，保证"参与计算的字节 == manifest 指向的字节"；
    * **R04 整批原子**：所有产物先写 staging 版本目录并校验哈希，提交快照后
      原子改名发布，最后原子替换 ``data/metadata/current.json``；任一步失败读者
      仍只看到完整旧版本（旧路径兼容副本会尽力回滚）；
    * **R05 覆盖闸门**：逐年对照上一版本交易日集合，历史交易日消失默认报错，
      除非 ``allow_coverage_drop=True``（CLI: ``--allow-coverage-drop``）；
    * **R06 当年刷新**：在线模式按配置强制刷新当年文件并保留旧快照归档；最新
      数据落后超过 ``cfg.data.max_staleness_days`` 时告警。

    Parameters
    ----------
    start, end : str | date | None
        闭区间；``None`` 用 config 的 ``data.start_date / data.end_date``。
    source : {"chinabond", "akshare"}
        主源为 chinabond；akshare 仅作为可选兜底。
    offline : bool
        True 时只用 ``data/raw_spike`` 缓存（年度 xlsx 优先，合并 CSV 兜底），
        保证无网络也能重建。
    force : bool
        忽略本地快照重新抓取/解析（离线模式会把源文件重新登记为快照）。
    cfg : Config | None
        测试可注入配置。
    paths : LoadPaths | None
        测试可把输出目录重定向到 tmp_path。
    raw_spike_dir : str | Path | None
        离线缓存目录（默认 ``data/raw_spike``）。
    min_rows : int
        落盘行数下限（数据完整性闸门）。
    min_dates : int
        落盘交易日数下限（默认 100；单测可传 1）。
    write_funding : bool
        是否顺带构建资金面序列（在线模式会先尝试在线刷新）。
    allow_coverage_drop : bool
        显式允许历史交易日减少（默认 False，出现覆盖退化会报错）。

    Raises
    ------
    SourceError
        数据源抓取/解析失败（旧版本保留）。
    DataValidationError
        校验/覆盖退化失败（旧版本保留）。
    FileNotFoundError
        离线缓存完全不存在。
    """
    cfg = cfg or get_config()
    source = source.lower()
    if source not in {"chinabond", "akshare"}:
        raise ValueError(f"未知数据源 {source!r}，可选 'chinabond' / 'akshare'")
    if offline and source != "chinabond":
        raise SourceError(
            f"离线模式只能重建中债数据（当前 --source {source}）；"
            "离线缓存来自 data/raw_spike 的中债年度 xlsx"
        )
    paths = paths or LoadPaths.from_config(cfg, raw_spike=raw_spike_dir)
    start_date = _to_date(start) or cfg.data.start_date
    end_date = _to_date(end) or cfg.data.end_date
    if end_date is not None and end_date < start_date:
        raise ValueError(f"end({end_date}) 早于 start({start_date})")
    ctx = _context(
        cfg=cfg,
        paths=paths,
        start=start_date,
        end=end_date,
        source=source,
        offline=offline,
        force=force,
        min_rows=min_rows,
        min_dates=min_dates,
        write_funding=write_funding,
        allow_coverage_drop=allow_coverage_drop,
    )
    ctx.paths.ensure()
    log.info(
        "开始构建数据：source=%s offline=%s 区间=[%s, %s] force=%s",
        source,
        offline,
        start_date,
        end_date,
        force,
    )
    batch_staging = _staging_dir(ctx.paths, "batch")
    pending: list[tuple[Path, Path]] = []
    try:
        if offline:
            frames, records, pending = _load_offline(ctx, batch_staging)
        elif source == "chinabond":
            frames, records, pending = _load_online_chinabond(ctx, batch_staging)
        else:
            frames, records, pending = _load_online_akshare(ctx, batch_staging)

        merged = _concat_frames(frames, context="年度文件合并")
        raw_frame, dedupe = _dedupe_raw(merged, context="年度文件合并去重")
        raw_filtered = _filter_range(raw_frame, start_date, end_date)
        if raw_filtered.empty:
            raise DataValidationError(f"合并后区间 [{start_date}, {end_date}] 内无数据，拒绝发布")

        cleaned = clean_long_frame(
            raw_filtered,
            start=start_date,
            end=end_date,
            context="中债年度文件合并后清洗",
        )
        _validate_curve_dataset(cleaned, ctx)

        # R05：逐年覆盖退化闸门（对照上一版本交易日集合）
        previous_frame, baseline_label = _previous_curve_frame(ctx.paths, cfg)
        coverage = _check_coverage_regression(
            cleaned.frame,
            previous_frame,
            baseline_label=baseline_label,
            allow=ctx.allow_coverage_drop,
        )

        # R06：数据新鲜度检查
        _data_min, data_max = _date_span(cleaned.frame)
        assert data_max is not None
        staleness = _check_staleness(
            date.fromisoformat(data_max),
            max_staleness_days=cfg.data.max_staleness_days,
            offline=ctx.offline,
        )

        # 资金面（在线刷新；失败降级 CSV；辅助数据不阻断主流程）
        funding = _prepare_funding(ctx, batch_staging)
        if funding is not None and funding.pending_snapshot is not None:
            pending.append(funding.pending_snapshot)

        raw_out = raw_filtered.loc[:, [c for c in RAW_COLUMNS if c in raw_filtered.columns]]

        def manifest_factory(
            final_dir: Path, version_id: str, files: dict[str, Path]
        ) -> dict[str, Any]:
            staging_dir = files["curve_long"].parent
            return _build_manifest(
                ctx,
                cleaned=cleaned,
                raw_frame=raw_out,
                records=records,
                dedupe=dedupe,
                funding=funding,
                coverage=coverage,
                output_dir=final_dir,
                staging_dir=staging_dir,
                version_id=version_id,
            )

        staged = _stage_version(
            ctx,
            curve_frame=cleaned.frame,
            raw_frame=raw_out,
            funding=funding,
            manifest_factory=manifest_factory,
        )
        # 快照提交在版本发布之前：任何失败都不会更换 current.json
        committed_snapshots = _commit_pending_snapshots(pending)
        pending = []
        published = _publish_version(ctx, staged)

        result = LoadResult(
            frame=cleaned.frame,
            raw_frame=raw_out,
            clean=cleaned,
            paths=ctx.paths,
            manifest=staged.manifest,
            manifest_path=ctx.paths.manifest,
            records=tuple(records),
            duplicate_stats={
                "rows_removed": int(dedupe.rows_removed),
                "groups_removed": int(dedupe.groups_removed),
            },
            version_id=published.version_id,
            version_dir=published.version_dir,
            current_pointer_path=published.current_pointer,
            staleness=staleness.to_dict(),
            coverage=coverage.to_dict(),
        )
        if committed_snapshots:
            log.info("本次提交 %d 个数据快照", len(committed_snapshots))
        log.info("构建完成：%s", result.summary())
        return result
    finally:
        _rmtree_quiet(batch_staging)


def _load_from_pointer(
    paths: LoadPaths,
    key: str,
    cfg: Config,
) -> pd.DataFrame | None:
    """从 ``current.json`` 指针读取指定数据集；不存在/文件缺失返回 None（回退旧路径）。

    Raises
    ------
    DataValidationError
        指针声明的 sha256 与实际文件不一致（版本损坏，不允许静默降级）。
    """
    pointer = _read_current_pointer(paths)
    if pointer is None:
        return None
    entry = pointer.get(key)
    if not isinstance(entry, dict) or not entry.get("path"):
        return None
    path = _resolve_pointer_path(entry["path"], cfg.root)
    if not path.exists():
        log.warning("current.json 指向的 %s 文件不存在，回退旧路径：%s", key, path)
        return None
    expected = entry.get("sha256")
    if expected and _sha256_file(path) != expected:
        raise DataValidationError(
            f"版本文件哈希校验失败：{path} 实际 {_sha256_file(path)[:12]}…"
            f" != 指针登记 {str(expected)[:12]}…"
        )
    return pd.read_parquet(path)


def load_curve_long(
    path: str | Path | None = None,
    *,
    cfg: Config | None = None,
    paths: LoadPaths | None = None,
) -> pd.DataFrame:
    """读取已发布的 ``curve_long``（下游标准入口）。

    优先解析 ``data/metadata/current.json`` 指针指向的不可变版本；指针缺失时回退
    旧路径 ``data/processed/curve_long.parquet``（向后兼容）。无论哪条路径，读取
    后都做契约校验。

    Raises
    ------
    FileNotFoundError
        指针与旧路径都不存在（提示先运行 fetch_data）。
    DataValidationError
        版本哈希不一致或数据不满足契约。
    """
    cfg = cfg or get_config()
    if path is not None:
        parquet = Path(path)
        if not parquet.exists():
            raise FileNotFoundError(
                f"未找到 {parquet}；请先运行 `python scripts/fetch_data.py --offline`"
            )
        frame = pd.read_parquet(parquet)
    else:
        resolved_paths = paths or LoadPaths.from_config(cfg)
        frame = _load_from_pointer(resolved_paths, "dataset", cfg)
        if frame is None:
            parquet = resolved_paths.curve_processed
            if not parquet.exists():
                raise FileNotFoundError(
                    f"未找到 {parquet}（也没有可用的 current.json 指针）；"
                    "请先运行 `python scripts/fetch_data.py --offline`"
                )
            frame = pd.read_parquet(parquet)
    frame["date"] = pd.to_datetime(frame["date"], errors="raise")
    return validate_long_frame(frame)


def load_funding_series(
    path: str | Path | None = None,
    *,
    cfg: Config | None = None,
    paths: LoadPaths | None = None,
) -> pd.DataFrame:
    """读取资金面序列（优先指针版本，回退旧路径 ``data/processed/repo_rates.parquet``）。"""
    cfg = cfg or get_config()
    if path is not None:
        parquet = Path(path)
        if not parquet.exists():
            raise FileNotFoundError(f"未找到 {parquet}；请先运行 fetch_data")
        frame = pd.read_parquet(parquet)
    else:
        resolved_paths = paths or LoadPaths.from_config(cfg)
        frame = _load_from_pointer(resolved_paths, "funding", cfg)
        if frame is None:
            parquet = resolved_paths.funding_processed
            if not parquet.exists():
                raise FileNotFoundError(
                    f"未找到 {parquet}（也没有可用的 current.json 指针）；请先运行 fetch_data"
                )
            frame = pd.read_parquet(parquet)
    frame["date"] = pd.to_datetime(frame["date"], errors="raise")
    return frame.sort_values("date").reset_index(drop=True)


# ---------------------------------------------------------------------------
# 2Y 缺口回补
# ---------------------------------------------------------------------------
def _load_published_manifest(paths: LoadPaths, cfg: Config) -> dict[str, Any] | None:
    """读取当前已发布版本的 manifest（回补时沿用 sources/interim/funding 血缘）。"""
    pointer = _read_current_pointer(paths)
    candidates: list[Path] = []
    if pointer is not None and pointer.get("manifest_path"):
        candidates.append(_resolve_pointer_path(pointer["manifest_path"], cfg.root))
    candidates.append(paths.manifest)
    for candidate in candidates:
        if not candidate.exists():
            continue
        try:
            payload = json.loads(candidate.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError) as exc:
            log.warning("manifest 读取失败（%s）：%s", candidate, exc)
            continue
        if isinstance(payload, dict):
            return payload
    return None


def _load_published_frame(
    paths: LoadPaths,
    cfg: Config,
    key: str,
    *,
    fallback: Path | None = None,
) -> pd.DataFrame | None:
    """读取当前版本中的某个 parquet 数据集（interim/funding），失败返回 None。"""
    pointer = _read_current_pointer(paths)
    candidates: list[Path] = []
    if pointer is not None:
        entry = pointer.get(key) or {}
        if entry.get("path"):
            candidates.append(_resolve_pointer_path(entry["path"], cfg.root))
    if fallback is not None:
        candidates.append(fallback)
    for candidate in candidates:
        if candidate.exists():
            try:
                return pd.read_parquet(candidate)
            except Exception as exc:
                log.warning("读取上一版本 %s 失败：%s", candidate, exc)
    return None


@dataclass(frozen=True)
class BackfillResult:
    """2Y 回补结果。"""

    n_requested: int
    n_success: int
    n_failed: int
    output_path: Path
    report_path: Path | None
    failures: pd.DataFrame
    report: dict[str, Any]
    dry_run: bool = False
    version_id: str = ""
    current_pointer_path: Path | None = None


#: 单日抓取函数签名：输入日期，返回该日长表
Fetcher = Callable[[date], pd.DataFrame]


def _extract_rate(frame: pd.DataFrame, tenor: float) -> float | None:
    """从单日长表中取指定期限的收益率；不存在返回 None。"""
    if frame is None or frame.empty:
        return None
    tenor_values = pd.to_numeric(frame["tenor"], errors="coerce")
    hit = frame.loc[tenor_values.sub(tenor).abs() <= 1e-6]
    if hit.empty:
        return None
    value = pd.to_numeric(hit["ytm_pct"], errors="coerce").dropna()
    return float(value.iloc[-1]) if not value.empty else None


def backfill_2y(
    *,
    start: str | date | None = None,
    end: str | date | None = None,
    tenor: float = 2.0,
    validate_against: float = 10.0,
    tol: float = 1e-4,
    fetcher: Fetcher | None = None,
    throttle_s: float | None = None,
    limit: int | None = None,
    dry_run: bool = False,
    max_fail_ratio: float = 0.05,
    cfg: Config | None = None,
    paths: LoadPaths | None = None,
) -> BackfillResult:
    """用 ``downBzqxDetail`` 逐日回补缺失的 2Y 期限点。

    回补流程
    --------
    1. 读取 ``data/processed/curve_long.parquet``，找出区间内缺 2Y 的交易日；
    2. 逐日调用单日接口，取 2Y（目标）与 10Y（校验基准）；
    3. 用 ``validate_against``（默认 10Y）与年度文件逐日比对，容差 ``tol``；
       不一致/缺字段的日期记为失败，**不写入、不静默跳过**；
    4. 失败率超过 ``max_fail_ratio`` 或全部失败 → 抛错、不落盘；
    5. 成功行合并回 ``curve_long.parquet``（先备份旧文件到 interim），并写
       ``data/metadata/backfill_2y_report.json``。

    Parameters
    ----------
    fetcher : Callable[[date], DataFrame] | None
        单日抓取函数；``None`` 时用 :class:`ChinabondSource`。测试可注入 fake。
    limit : int | None
        只处理前 N 天（调试/分批用；此时也会落盘并记录）。
    dry_run : bool
        只抓取与校验，不写盘。

    Raises
    ------
    FileNotFoundError
        尚未运行 fetch_data 产出 curve_long.parquet。
    SourceError
        全部失败或失败率超阈值。
    DataValidationError
        合并后契约校验失败。
    """
    cfg = cfg or get_config()
    paths = paths or LoadPaths.from_config(cfg)
    processed = paths.curve_processed
    if not processed.exists():
        raise FileNotFoundError(
            f"未找到 {processed}；请先运行 `python scripts/fetch_data.py --offline`"
        )
    current = load_curve_long(processed, cfg=cfg)
    # 兼容只有 3 列的最小长表（测试/外部输入），可选列缺失时补默认值
    for column, default in (
        ("term_label", None),
        ("source", "chinabond"),
        ("curve_name", DEFAULT_CURVE_NAME),
    ):
        if column not in current.columns:
            current[column] = default
    start_date = _to_date(start) or cfg.backfill.start
    end_date = _to_date(end) or cfg.backfill.end
    if end_date < start_date:
        raise ValueError(f"end({end_date}) 早于 start({start_date})")
    if throttle_s is None:
        throttle_s = cfg.backfill.throttle_s

    target_tenor = float(tenor)
    against = float(validate_against)
    days = current["date"].dt.date
    have_tenor = np.isclose(current["tenor"].astype(float).to_numpy(), target_tenor, atol=1e-6)
    dates_with_tenor = set(current.loc[have_tenor, "date"].dt.date)
    all_days = set(days.loc[(days >= start_date) & (days <= end_date)])
    missing_days = sorted(all_days - dates_with_tenor)
    if limit is not None:
        missing_days = missing_days[: int(limit)]
    log.info(
        "2Y 回补：区间 [%s, %s] 内缺失 %d 个交易日（目标期限 %gY，校验基准 %gY，容差 %g）",
        start_date,
        end_date,
        len(missing_days),
        target_tenor,
        against,
        tol,
    )
    if not missing_days:
        return BackfillResult(
            n_requested=0,
            n_success=0,
            n_failed=0,
            output_path=processed,
            report_path=None,
            failures=pd.DataFrame(columns=["date", "reason"]),
            report={"status": "nothing_to_do"},
            dry_run=dry_run,
        )

    reference = (
        current.loc[np.isclose(current["tenor"].astype(float), against, atol=1e-6)]
        .assign(day=lambda df: df["date"].dt.date)
        .set_index("day")["ytm_pct"]
        .to_dict()
    )
    if fetcher is None:
        source = ChinabondSource(cfg=cfg, cache_dir=paths.snapshot_dir, throttle_s=throttle_s)
        fetcher = source.fetch_date

    new_rows: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for day in missing_days:
        try:
            frame = fetcher(day)
        except Exception as exc:
            failures.append({"date": day, "reason": f"抓取失败：{exc!r}"})
            log.warning("2Y 回补 %s 抓取失败：%r", day, exc)
            continue
        if frame is None or frame.empty:
            failures.append({"date": day, "reason": "抓取返回空表"})
            continue
        target_value = _extract_rate(frame, target_tenor)
        if target_value is None:
            failures.append({"date": day, "reason": f"单日文件缺 {target_tenor:g}Y"})
            continue
        reference_value = _extract_rate(frame, against)
        expected = reference.get(day)
        if reference_value is None:
            failures.append({"date": day, "reason": f"单日文件缺校验基准 {against:g}Y，无法核对"})
            continue
        if expected is None:
            failures.append({"date": day, "reason": f"年度文件缺校验基准 {against:g}Y，无法核对"})
            continue
        diff = abs(float(reference_value) - float(expected))
        if diff > float(tol):
            failures.append(
                {
                    "date": day,
                    "reason": (
                        f"{against:g}Y 校验失败：单日 {reference_value:.6f} vs "
                        f"年度 {expected:.6f}，差 {diff:.6f} > {tol:g}"
                    ),
                }
            )
            continue
        new_rows.append(
            {
                "date": pd.Timestamp(day),
                "tenor": target_tenor,
                "ytm_pct": float(target_value),
                "term_label": "2y",
                "source": "chinabond:downBzqxDetail",
                "curve_name": DEFAULT_CURVE_NAME,
            }
        )

    failures_frame = pd.DataFrame(failures, columns=["date", "reason"])
    n_failed = len(failures_frame)
    n_success = len(new_rows)
    log.info("2Y 回补结果：成功 %d，失败 %d / %d", n_success, n_failed, len(missing_days))

    report: dict[str, Any] = {
        "schema_version": SCHEMA_VERSION,
        "generated_at": _utc_now_iso(),
        "generator": "ficc.data.loader.backfill_2y",
        "params": {
            "start": start_date.isoformat(),
            "end": end_date.isoformat(),
            "tenor": target_tenor,
            "validate_against": against,
            "tol": float(tol),
            "limit": limit,
            "dry_run": dry_run,
        },
        "n_requested": len(missing_days),
        "n_success": n_success,
        "n_failed": n_failed,
        "success_dates": [str(pd.Timestamp(row["date"]).date()) for row in new_rows],
        "failures": [
            {"date": str(item["date"]), "reason": str(item["reason"])} for item in failures
        ],
        "output": None,
        "status": "pending",
    }

    def _write_failure_report(status: str) -> None:
        # 失败也要留痕（不是静默跳过）；旧 parquet 保持不动。
        report["status"] = status
        _write_json_atomic(report, paths.backfill_report)
        log.warning("2Y 回补失败报告已写入 %s", paths.backfill_report)

    if n_success == 0:
        _write_failure_report("failed_all")
        raise SourceError(
            f"2Y 回补全部失败（{n_failed} 天）：{failures_frame.head(5).to_dict('records')}；"
            f"报告见 {paths.backfill_report}，旧数据未改动"
        )
    if n_failed / len(missing_days) > float(max_fail_ratio):
        _write_failure_report("failed_ratio")
        raise SourceError(
            f"2Y 回补失败率 {n_failed / len(missing_days):.1%} 超过阈值 "
            f"{float(max_fail_ratio):.1%}（成功 {n_success} / 失败 {n_failed}），拒绝落盘；"
            f"失败样例 {failures_frame.head(5).to_dict('records')}，报告见 {paths.backfill_report}"
        )

    new_frame = pd.DataFrame(new_rows, columns=list(CURVE_COLUMNS))
    combined = pd.concat([current.loc[:, list(CURVE_COLUMNS)], new_frame], ignore_index=True)
    combined = combined.drop_duplicates(subset=["date", "tenor"], keep="last")
    combined = validate_long_frame(combined.sort_values(["date", "tenor"]).reset_index(drop=True))
    expected_rows = len(current) + n_success
    if len(combined) != expected_rows:
        raise DataValidationError(
            f"回补合并后行数 {len(combined)} != 预期 {expected_rows}，拒绝落盘"
        )

    if dry_run:
        report["status"] = "dry_run"
        log.info("dry-run：不写盘")
        return BackfillResult(
            n_requested=len(missing_days),
            n_success=n_success,
            n_failed=n_failed,
            output_path=processed,
            report_path=None,
            failures=failures_frame,
            report=report,
            dry_run=True,
        )

    backup = paths.interim / "curve_long.pre_backfill.parquet"
    _write_parquet_atomic(current, backup)

    # 回补结果作为一个新版本原子发布（复用旧版本的 interim/funding/血缘）
    previous_manifest = _load_published_manifest(paths, cfg)
    raw_frame = _load_published_frame(
        paths,
        cfg,
        "interim",
        fallback=paths.curve_interim,
    )
    if raw_frame is None:
        log.warning("未找到上一版本原始长表，回补版本 interim 暂用曲线长表占位")
        raw_frame = combined.loc[:, list(CURVE_COLUMNS)].copy()
    funding_frame = _load_published_frame(
        paths,
        cfg,
        "funding",
        fallback=paths.funding_processed,
    )
    previous_funding_meta = (previous_manifest or {}).get("funding") or {}
    funding_build = (
        FundingBuild(
            frame=funding_frame,
            source_url=str(previous_funding_meta.get("source_url", "")),
            source_sha256=previous_funding_meta.get("source_sha256"),
        )
        if funding_frame is not None
        else None
    )
    backfill_ctx = _context(
        cfg=cfg,
        paths=paths,
        start=start_date,
        end=end_date,
        source="chinabond",
        offline=False,
        force=False,
        min_rows=0,
        min_dates=0,
        write_funding=False,
    )
    batch_staging = _staging_dir(paths, "backfill")
    try:

        def manifest_factory(
            final_dir: Path, version_id: str, files: dict[str, Path]
        ) -> dict[str, Any]:
            if previous_manifest is not None:
                manifest = json.loads(json.dumps(previous_manifest))
            else:
                manifest = {
                    "schema_version": SCHEMA_VERSION,
                    "sources": [],
                    "checks": {},
                    "params": {},
                }
            manifest["generated_at"] = _utc_now_iso()
            manifest["generator"] = "ficc.data.loader.backfill_2y"
            manifest["version_id"] = version_id
            manifest["params"] = {
                **(manifest.get("params") or {}),
                "backfill_tenor": target_tenor,
                "backfill_start": start_date.isoformat(),
                "backfill_end": end_date.isoformat(),
            }
            previous_dataset = (previous_manifest or {}).get("dataset") or {}
            dataset_entry = _artifact_entry(
                combined,
                files["curve_long"],
                root=cfg.root,
                output_dir=final_dir,
                name=CURVE_DATASET,
            )
            for extra_key in ("expected_cells", "known_gap_2y", "missing_rate"):
                if extra_key in previous_dataset:
                    dataset_entry[extra_key] = previous_dataset[extra_key]
            previous_missing = int(previous_dataset.get("missing_cells", 0))
            dataset_entry["missing_cells"] = max(0, previous_missing - n_success)
            dataset_entry["backfilled_cells"] = int(n_success)
            manifest["dataset"] = dataset_entry
            manifest["interim"] = _artifact_entry(
                raw_frame,
                files["curve_raw"],
                root=cfg.root,
                output_dir=final_dir,
                name=CURVE_RAW_DATASET,
            )
            if funding_build is not None and "repo_rates" in files:
                funding_entry = _artifact_entry(
                    funding_build.frame,
                    files["repo_rates"],
                    root=cfg.root,
                    output_dir=final_dir,
                    name=FUNDING_DATASET,
                )
                funding_entry["source_url"] = funding_build.source_url
                funding_entry["source_sha256"] = funding_build.source_sha256
                manifest["funding"] = funding_entry
            else:
                manifest["funding"] = (previous_manifest or {}).get("funding")
            manifest["backfill_2y"] = {
                "generated_at": report["generated_at"],
                "n_requested": report["n_requested"],
                "n_success": report["n_success"],
                "n_failed": report["n_failed"],
                "validate_against": report["params"]["validate_against"],
                "tol": report["params"]["tol"],
                "report_path": _rel(paths.backfill_report, cfg.root),
            }
            return manifest

        staged = _stage_version(
            backfill_ctx,
            curve_frame=combined,
            raw_frame=raw_frame,
            funding=funding_build,
            manifest_factory=manifest_factory,
        )
        published = _publish_version(backfill_ctx, staged)
    finally:
        _rmtree_quiet(batch_staging)

    report["status"] = "ok"
    report["version_id"] = published.version_id
    report["current_pointer"] = _rel(published.current_pointer, cfg.root)
    report["output"] = {
        "path": _rel(processed, cfg.root),
        "version_path": _rel(published.version_dir / "curve_long.parquet", cfg.root),
        "sha256": _sha256_file(published.version_dir / "curve_long.parquet"),
        "rows": len(combined),
    }
    _write_json_atomic(report, paths.backfill_report)
    return BackfillResult(
        n_requested=len(missing_days),
        n_success=n_success,
        n_failed=n_failed,
        output_path=processed,
        report_path=paths.backfill_report,
        failures=failures_frame,
        report=report,
        dry_run=False,
        version_id=published.version_id,
        current_pointer_path=published.current_pointer,
    )


__all__ = [
    "BACKFILL_REPORT_FILENAME",
    "CURRENT_POINTER_FILENAME",
    "CURVE_COLUMNS",
    "CURVE_DATASET",
    "MANIFEST_FILENAME",
    "VERSIONS_DIRNAME",
    "BackfillResult",
    "LoadPaths",
    "LoadResult",
    "SourceRecord",
    "backfill_2y",
    "fetch_curve_data",
    "load_curve_long",
    "load_funding_series",
]
