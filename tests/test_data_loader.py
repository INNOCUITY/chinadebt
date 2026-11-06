"""数据加载编排测试（全部离线，用 tmp_path + 合成 xlsx）。

覆盖：
1. 离线构建：``data/raw_spike`` 年度 xlsx → interim / processed / manifest 全链路；
2. manifest 血缘字段（source_url / fetched_at / sha256 / rows / date_min / date_max / tenors）；
3. 幂等性：重复构建产出相同 dataset sha256；
4. **失败不覆盖旧数据**：校验失败时旧 parquet/manifest 原样保留；
5. 官方年度文件跨年重叠：数值一致 → 去重；数值冲突 → 报错并列明细；
6. 清洗层：0D/非网格点丢弃、去重、``keep_off_grid``、缺少年度报错；
7. 2Y 回补：成功合并、失败记录、失败率超阈不落盘、dry-run、备份；
8. 真实数据回归：6204 个交易日 / 67,472 行 / 2Y 缺 772 点。
"""

from __future__ import annotations

import hashlib
import json
from datetime import date
from pathlib import Path

import pandas as pd
import pytest

from ficc.data.clean import clean_long_frame, dedupe_long_frame
from ficc.data.loader import (
    LoadPaths,
    backfill_2y,
    fetch_curve_data,
    load_curve_long,
    load_funding_series,
)
from ficc.data.schema import CANONICAL_TENORS, TENOR_LABELS, DataValidationError
from ficc.data.sources.base import SourceError

ROOT = Path(__file__).resolve().parents[1]
REAL_PROCESSED = ROOT / "data" / "processed" / "curve_long.parquet"
REAL_MANIFEST = ROOT / "data" / "metadata" / "manifest.json"


# ---------------------------------------------------------------------------
# 合成数据工具
# ---------------------------------------------------------------------------
def _tenor_label(tenor: float) -> str:
    return TENOR_LABELS[float(tenor)].lower()


def _value(day: pd.Timestamp, tenor: float) -> float:
    """确定性的合成收益率（同一天永远同值，便于构造跨年重叠）。"""
    return round(2.0 + 0.01 * tenor + (day.toordinal() % 97) * 0.001, 6)


def write_year_xlsx(
    path: Path,
    days: pd.DatetimeIndex,
    *,
    tenors: tuple[float, ...] = CANONICAL_TENORS,
    tweak: dict[tuple[str, float], float] | None = None,
) -> Path:
    """写官方风格年度 xlsx（``日期/标准期限说明/标准期限(年)/收益率(%)``）。"""
    rows = []
    for day in days:
        for tenor in tenors:
            rows.append(
                {
                    "日期": day.strftime("%Y-%m-%d"),
                    "标准期限说明": _tenor_label(tenor),
                    "标准期限(年)": float(tenor),
                    "收益率(%)": _value(day, tenor)
                    + (tweak or {}).get((day.strftime("%Y-%m-%d"), float(tenor)), 0.0),
                }
            )
    pd.DataFrame(rows).to_excel(path, index=False)
    return path


def make_spike_dir(
    tmp_path: Path,
    *,
    years: tuple[int, ...] = (2023, 2024),
    overlap_days: int = 0,
    conflict: bool = False,
) -> Path:
    """构造 ``raw_spike/chinabond_gzqx`` 离线缓存（两年，可选跨年重叠）。"""
    spike_dir = tmp_path / "raw_spike" / "chinabond_gzqx"
    spike_dir.mkdir(parents=True, exist_ok=True)
    previous_tail: list[pd.Timestamp] = []
    for year in years:
        days = pd.bdate_range(f"{year}-01-03", f"{year}-12-29")
        tweak = None
        if overlap_days and previous_tail:
            # 当年文件额外包含上一年最后 overlap_days 个交易日（复刻官方导出重叠）
            days = pd.DatetimeIndex(previous_tail[-overlap_days:]).append(days)
            if conflict:
                tweak = {
                    (str(previous_tail[-1].date()), 10.0): 0.5,
                }
        write_year_xlsx(spike_dir / f"{year}.xlsx", days, tweak=tweak)
        previous_tail = list(days)
    return spike_dir


def make_paths(tmp_path: Path, cfg) -> LoadPaths:
    return LoadPaths.from_config(
        cfg,
        raw=tmp_path / "data" / "raw",
        interim=tmp_path / "data" / "interim",
        processed=tmp_path / "data" / "processed",
        metadata=tmp_path / "data" / "metadata",
        raw_spike=tmp_path / "raw_spike",
    )


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _build(
    tmp_path: Path,
    cfg,
    *,
    raw_spike_dir: Path,
    start: str = "2023-01-01",
    end: str = "2024-12-31",
    min_rows: int = 100,
    **kwargs,
):
    paths = make_paths(tmp_path, cfg)
    return fetch_curve_data(
        start=start,
        end=end,
        offline=True,
        cfg=cfg,
        paths=paths,
        raw_spike_dir=raw_spike_dir,
        min_rows=min_rows,
        min_dates=1,
        write_funding=False,
        **kwargs,
    )


# ---------------------------------------------------------------------------
# 离线构建 + manifest
# ---------------------------------------------------------------------------
def test_offline_build_outputs_and_manifest(tmp_path: Path, cfg) -> None:
    spike = make_spike_dir(tmp_path)
    result = _build(tmp_path, cfg, raw_spike_dir=spike)

    paths = result.paths
    assert paths.curve_processed.exists()
    assert paths.curve_interim.exists()
    assert paths.manifest.exists()

    frame = load_curve_long(paths.curve_processed)
    days_2023 = pd.bdate_range("2023-01-03", "2023-12-29")
    days_2024 = pd.bdate_range("2024-01-03", "2024-12-29")
    expected_days = len(days_2023) + len(days_2024)
    assert frame["date"].nunique() == expected_days
    assert len(frame) == expected_days * len(CANONICAL_TENORS)
    assert set(frame["tenor"]) == set(CANONICAL_TENORS)
    assert frame["ytm_pct"].dtype == "float64"
    assert not frame["ytm_pct"].isna().any()

    manifest = json.loads(paths.manifest.read_text(encoding="utf-8"))
    assert manifest["schema_version"] == "1.0"
    assert manifest["params"]["offline"] is True
    dataset = manifest["dataset"]
    assert dataset["path"].endswith("curve_long.parquet")
    assert dataset["sha256"] == _sha256(paths.curve_processed)
    assert dataset["rows"] == len(frame)
    assert dataset["date_min"] == str(days_2023.min().date())
    assert dataset["date_max"] == str(days_2024.max().date())
    assert dataset["tenors"] == list(CANONICAL_TENORS)
    assert manifest["interim"]["sha256"] == _sha256(paths.curve_interim)

    assert len(manifest["sources"]) == 2
    for record in manifest["sources"]:
        assert record["year"] in (2023, 2024)
        assert len(record["sha256"]) == 64
        assert record["rows"] > 0
        assert record["date_min"] and record["date_max"]
        assert set(record["tenors"]) == set(CANONICAL_TENORS)
        assert record["source_url"]
        assert record["fetched_at"]
        assert Path(ROOT / record["path"]).exists() or Path(record["path"]).exists()
    assert manifest["checks"]["rows_removed_cross_file_duplicates"] == 0


def test_offline_build_is_idempotent(tmp_path: Path, cfg) -> None:
    spike = make_spike_dir(tmp_path)
    first = _build(tmp_path, cfg, raw_spike_dir=spike)
    manifest_first = json.loads(first.paths.manifest.read_text(encoding="utf-8"))
    sha_first = _sha256(first.paths.curve_processed)

    second = _build(tmp_path, cfg, raw_spike_dir=spike)
    manifest_second = json.loads(second.paths.manifest.read_text(encoding="utf-8"))
    sha_second = _sha256(second.paths.curve_processed)

    assert sha_first == sha_second
    assert manifest_first["dataset"] == manifest_second["dataset"]
    assert manifest_first["interim"] == manifest_second["interim"]
    # coverage_regression 依赖"上一版本基线"，首次构建与第二次构建天然不同；
    # 其余内容校验统计必须完全一致。
    checks_first = dict(manifest_first["checks"])
    checks_second = dict(manifest_second["checks"])
    checks_first.pop("coverage_regression", None)
    checks_second.pop("coverage_regression", None)
    assert checks_first == checks_second
    assert manifest_first["coverage_by_year"] == manifest_second["coverage_by_year"]


def test_validation_failure_preserves_old_data(tmp_path: Path, cfg) -> None:
    spike = make_spike_dir(tmp_path)
    first = _build(tmp_path, cfg, raw_spike_dir=spike)
    processed_before = _sha256(first.paths.curve_processed)
    manifest_before = first.paths.manifest.read_text(encoding="utf-8")

    with pytest.raises(DataValidationError, match=r"阈值|低于|交易日"):
        _build(tmp_path, cfg, raw_spike_dir=spike, min_rows=10**9, force=True)

    assert _sha256(first.paths.curve_processed) == processed_before
    assert first.paths.manifest.read_text(encoding="utf-8") == manifest_before


def test_missing_year_raises_and_keeps_old(tmp_path: Path, cfg) -> None:
    spike = make_spike_dir(tmp_path)
    first = _build(tmp_path, cfg, raw_spike_dir=spike)
    processed_before = _sha256(first.paths.curve_processed)
    with pytest.raises(SourceError, match="缺少年度文件"):
        _build(tmp_path, cfg, raw_spike_dir=spike, start="2022-01-01", end="2024-12-31")
    assert _sha256(first.paths.curve_processed) == processed_before


# ---------------------------------------------------------------------------
# 跨年度文件重叠（Lead 发现的数据问题）
# ---------------------------------------------------------------------------
def test_cross_year_overlap_is_deduped(tmp_path: Path, cfg) -> None:
    spike = make_spike_dir(tmp_path, overlap_days=3)
    result = _build(tmp_path, cfg, raw_spike_dir=spike)
    manifest = result.manifest
    # 2024 文件重复包含 2023 最后 3 个交易日的全部 11 个期限点
    assert manifest["checks"]["rows_removed_cross_file_duplicates"] == 3 * len(CANONICAL_TENORS)
    assert manifest["checks"]["duplicate_conflicts"] == 0
    # 去重后每天每个期限点唯一
    frame = result.frame
    assert not frame.duplicated(subset=["date", "tenor"]).any()
    assert manifest["dataset"]["n_dates"] == frame["date"].nunique()


def test_cross_year_overlap_conflict_raises(tmp_path: Path, cfg) -> None:
    spike = make_spike_dir(tmp_path, overlap_days=2, conflict=True)
    with pytest.raises(DataValidationError, match="冲突"):
        _build(tmp_path, cfg, raw_spike_dir=spike)


def test_dedupe_reports_conflict_detail() -> None:
    frame = pd.DataFrame(
        {
            "date": ["2024-12-31", "2024-12-31", "2024-12-31"],
            "tenor": [10.0, 10.0, 2.0],
            "ytm_pct": [1.70, 1.75, 1.40],
            "source": ["2024.xlsx", "2025.xlsx", "2024.xlsx"],
        }
    )
    with pytest.raises(DataValidationError, match="冲突") as excinfo:
        dedupe_long_frame(frame, context="测试去重")
    message = str(excinfo.value)
    assert "2024-12-31" in message
    assert "1.700000" in message and "1.750000" in message


# ---------------------------------------------------------------------------
# 清洗层
# ---------------------------------------------------------------------------
def test_clean_drops_zero_offgrid_and_duplicates() -> None:
    rows = []
    for day in ("2024-01-02", "2024-01-03"):
        for tenor in CANONICAL_TENORS:
            rows.append({"date": day, "tenor": tenor, "ytm_pct": 2.0 + tenor * 0.01})
        rows.append({"date": day, "tenor": 0.0, "ytm_pct": 1.5})
        rows.append({"date": day, "tenor": 4.0, "ytm_pct": 2.1})
        rows.append({"date": day, "tenor": 40.0, "ytm_pct": 2.4})
    rows.append({"date": "2024-01-02", "tenor": 10.0, "ytm_pct": 2.0 + 10.0 * 0.01})
    frame = pd.DataFrame(rows)
    result = clean_long_frame(frame, context="测试清洗")
    assert result.stats.dropped_non_positive_tenor == 2
    assert result.stats.dropped_off_grid == 4  # 4Y/40Y 各 2 天
    assert result.stats.duplicates_removed == 1
    assert len(result.frame) == 2 * len(CANONICAL_TENORS)
    assert result.stats.n_dates == 2
    assert result.stats.missing_cells == 0


def test_clean_keep_off_grid() -> None:
    frame = pd.DataFrame(
        {
            "date": ["2024-01-02", "2024-01-02", "2024-01-02"],
            "tenor": [0.0, 4.0, 10.0],
            "ytm_pct": [1.5, 2.1, 2.3],
        }
    )
    result = clean_long_frame(frame, keep_off_grid=True, context="测试保留非网格")
    assert 4.0 in set(result.frame["tenor"])
    assert 10.0 in set(result.frame["tenor"])
    assert 0.0 not in set(result.frame["tenor"])
    # 缺失统计仍按标准网格（仅 10Y 命中）
    assert result.stats.missing_cells == len(CANONICAL_TENORS) - 1


def test_clean_rejects_conflicting_duplicates() -> None:
    frame = pd.DataFrame(
        {
            "date": ["2024-01-02", "2024-01-02"],
            "tenor": [10.0, 10.0],
            "ytm_pct": [2.3, 2.4],
        }
    )
    with pytest.raises(DataValidationError, match="冲突"):
        clean_long_frame(frame, context="测试冲突")


def test_load_curve_long_missing_raises(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError, match="fetch_data"):
        load_curve_long(tmp_path / "nope.parquet")


# ---------------------------------------------------------------------------
# 2Y 回补
# ---------------------------------------------------------------------------
def _base_curve_without_2y(days: pd.DatetimeIndex) -> pd.DataFrame:
    rows = []
    for day in days:
        for tenor in CANONICAL_TENORS:
            if tenor == 2.0:
                continue
            rows.append({"date": day, "tenor": tenor, "ytm_pct": _value(day, tenor)})
    return pd.DataFrame(rows)


def _write_processed(paths: LoadPaths, frame: pd.DataFrame) -> None:
    paths.processed.mkdir(parents=True, exist_ok=True)
    frame.to_parquet(paths.curve_processed, index=False)


def _good_fetcher(base: pd.DataFrame):
    panel = base.set_index(["date", "tenor"])["ytm_pct"].to_dict()

    def fetch(day: date) -> pd.DataFrame:
        rows = []
        for tenor in CANONICAL_TENORS:
            value = panel.get((pd.Timestamp(day), tenor))
            if value is None:
                value = _value(pd.Timestamp(day), tenor)
            rows.append({"date": pd.Timestamp(day), "tenor": tenor, "ytm_pct": value})
        return pd.DataFrame(rows)

    return fetch


def test_backfill_2y_success(tmp_path: Path, cfg) -> None:
    paths = make_paths(tmp_path, cfg)
    days = pd.bdate_range("2023-01-02", "2023-01-13")
    base = _base_curve_without_2y(days)
    _write_processed(paths, base)
    before = _sha256(paths.curve_processed)

    result = backfill_2y(
        start="2023-01-02",
        end="2023-01-13",
        fetcher=_good_fetcher(base),
        cfg=cfg,
        paths=paths,
    )
    assert result.n_requested == len(days)
    assert result.n_success == len(days)
    assert result.n_failed == 0
    assert result.report["status"] == "ok"
    assert result.report_path is not None and result.report_path.exists()
    assert result.output_path == paths.curve_processed
    assert _sha256(paths.curve_processed) != before

    frame = load_curve_long(paths.curve_processed)
    assert 2.0 in set(frame["tenor"])
    assert len(frame) == len(base) + len(days)
    assert frame.duplicated(subset=["date", "tenor"]).sum() == 0
    assert (paths.interim / "curve_long.pre_backfill.parquet").exists()

    report = json.loads(result.report_path.read_text(encoding="utf-8"))
    assert report["n_success"] == len(days)
    assert report["params"]["validate_against"] == 10.0
    assert report["params"]["tol"] == pytest.approx(1e-4)
    assert len(report["success_dates"]) == len(days)
    manifest = (
        json.loads(paths.manifest.read_text(encoding="utf-8")) if paths.manifest.exists() else None
    )
    # 测试未跑过 fetch_data → manifest 不存在；回补不因此失败
    assert manifest is None or manifest["backfill_2y"]["n_success"] == len(days)


def test_backfill_2y_mismatch_not_written(tmp_path: Path, cfg) -> None:
    paths = make_paths(tmp_path, cfg)
    days = pd.bdate_range("2023-01-02", "2023-01-06")
    base = _base_curve_without_2y(days)
    _write_processed(paths, base)
    before = _sha256(paths.curve_processed)

    def bad_fetcher(day: date) -> pd.DataFrame:
        frame = _good_fetcher(base)(day)
        frame.loc[frame["tenor"] == 10.0, "ytm_pct"] += 0.01  # 10Y 偏离 1bp*100
        return frame

    with pytest.raises(SourceError, match=r"失败率|全部失败"):
        backfill_2y(
            start="2023-01-02",
            end="2023-01-06",
            fetcher=bad_fetcher,
            max_fail_ratio=0.0,
            cfg=cfg,
            paths=paths,
        )
    assert _sha256(paths.curve_processed) == before
    report_path = paths.backfill_report
    assert report_path.exists()
    report = json.loads(report_path.read_text(encoding="utf-8"))
    # 全部日期 10Y 校验失败 → n_success=0，先命中 "全部失败" 分支并写报告
    assert report["status"] in {"failed_all", "failed_ratio"}
    assert report["n_success"] == 0
    assert report["n_failed"] == len(days)


def test_backfill_2y_all_fail_records_report(tmp_path: Path, cfg) -> None:
    paths = make_paths(tmp_path, cfg)
    days = pd.bdate_range("2023-01-02", "2023-01-04")
    base = _base_curve_without_2y(days)
    _write_processed(paths, base)
    before = _sha256(paths.curve_processed)

    def broken_fetcher(day: date) -> pd.DataFrame:
        raise RuntimeError(f"模拟网络失败 {day}")

    with pytest.raises(SourceError, match="全部失败"):
        backfill_2y(
            start="2023-01-02", end="2023-01-04", fetcher=broken_fetcher, cfg=cfg, paths=paths
        )
    assert _sha256(paths.curve_processed) == before
    report = json.loads(paths.backfill_report.read_text(encoding="utf-8"))
    assert report["status"] == "failed_all"
    assert len(report["failures"]) == len(days)


def test_backfill_2y_dry_run_does_not_write(tmp_path: Path, cfg) -> None:
    paths = make_paths(tmp_path, cfg)
    days = pd.bdate_range("2023-01-02", "2023-01-04")
    base = _base_curve_without_2y(days)
    _write_processed(paths, base)
    before = _sha256(paths.curve_processed)
    result = backfill_2y(
        start="2023-01-02",
        end="2023-01-04",
        fetcher=_good_fetcher(base),
        dry_run=True,
        cfg=cfg,
        paths=paths,
    )
    assert result.dry_run is True
    assert result.n_success == len(days)
    assert _sha256(paths.curve_processed) == before
    assert result.report_path is None


def test_backfill_2y_requires_processed(tmp_path: Path, cfg) -> None:
    paths = make_paths(tmp_path, cfg)
    with pytest.raises(FileNotFoundError, match="fetch_data"):
        backfill_2y(start="2023-01-02", end="2023-01-04", cfg=cfg, paths=paths)


def test_backfill_2y_nothing_to_do(tmp_path: Path, cfg) -> None:
    paths = make_paths(tmp_path, cfg)
    full = pd.DataFrame(
        [
            {"date": day, "tenor": tenor, "ytm_pct": _value(day, tenor)}
            for day in pd.bdate_range("2023-01-02", "2023-01-04")
            for tenor in CANONICAL_TENORS
        ]
    )
    _write_processed(paths, full)
    result = backfill_2y(start="2023-01-02", end="2023-01-04", cfg=cfg, paths=paths)
    assert result.n_requested == 0
    assert result.n_success == 0
    assert result.report["status"] == "nothing_to_do"


# ---------------------------------------------------------------------------
# 真实数据回归（Lead 指定的锚点）
# ---------------------------------------------------------------------------
def test_real_dataset_regression_6204_days() -> None:
    if not REAL_PROCESSED.exists():
        pytest.skip("缺少 data/processed/curve_long.parquet（先运行 fetch_data --offline）")
    frame = load_curve_long(REAL_PROCESSED)
    assert len(frame) == 67_472
    assert frame["date"].nunique() == 6_204
    assert str(frame["date"].min().date()) == "2002-01-04"
    assert str(frame["date"].max().date()) == "2026-11-05"
    assert set(frame["tenor"]) == set(CANONICAL_TENORS)
    n_dates_with_2y = frame.loc[frame["tenor"] == 2.0, "date"].nunique()
    assert frame["date"].nunique() - n_dates_with_2y == 772
    if REAL_MANIFEST.exists():
        manifest = json.loads(REAL_MANIFEST.read_text(encoding="utf-8"))
        assert manifest["dataset"]["rows"] == 67_472
        assert manifest["dataset"]["n_dates"] == 6_204
        assert manifest["checks"]["rows_removed_cross_file_duplicates"] == 51
        assert manifest["checks"]["duplicate_conflicts"] == 0
        assert manifest["dataset"]["sha256"] == _sha256(REAL_PROCESSED)


def test_real_funding_series_if_available() -> None:
    path = ROOT / "data" / "processed" / "repo_rates.parquet"
    if not path.exists():
        pytest.skip("缺少 data/processed/repo_rates.parquet")
    frame = load_funding_series(path)
    assert len(frame) > 5_000
    assert {"FR007", "FDR007"}.issubset(frame.columns)
    assert str(frame["date"].min().date()) == "2005-01-04"


# ===========================================================================
# R03–R06 回归测试（数据版本一致性 / 整批原子 / 覆盖闸门 / 当年刷新）
# ===========================================================================
import logging  # noqa: E402

import ficc.data.loader as loader_module  # noqa: E402
from ficc.data.loader import REPO_CSV_NAME  # noqa: E402
from ficc.data.sources.chinabond import parse_official_xlsx  # noqa: E402


def _bump_xlsx_values(path: Path, delta: float) -> None:
    """把官方 xlsx 的所有收益率整体平移（模拟源文件被更新）。"""
    frame = pd.read_excel(path)
    frame["收益率(%)"] = frame["收益率(%)"].astype(float) + delta
    frame.to_excel(path, index=False)


def _drop_last_day_from_xlsx(path: Path) -> str:
    """删掉某年度 xlsx 的最后一个交易日（模拟历史年份被截断）。"""
    frame = pd.read_excel(path)
    days = sorted(pd.to_datetime(frame["日期"]).unique())
    dropped = pd.Timestamp(days[-1]).date()
    frame = frame.loc[pd.to_datetime(frame["日期"]).dt.date != dropped]
    frame.to_excel(path, index=False)
    return str(dropped)


def _baseline_with_pointer(tmp_path: Path, cfg) -> object:
    spike = make_spike_dir(tmp_path)
    result = _build(tmp_path, cfg, raw_spike_dir=spike)
    assert result.paths.current_pointer.exists()
    return spike, result


# ---------------------------------------------------------------------------
# R03：血缘一致（离线源文件更新后，非 force 仍解析已登记快照）
# ---------------------------------------------------------------------------
def test_r03_source_change_without_force_keeps_registered_snapshot(
    tmp_path: Path, cfg, caplog
) -> None:
    spike, first = _baseline_with_pointer(tmp_path, cfg)
    paths = first.paths
    processed_sha = _sha256(paths.curve_processed)
    manifest_first = json.loads(paths.manifest.read_text(encoding="utf-8"))
    source_hashes_first = {rec["year"]: rec["sha256"] for rec in manifest_first["sources"]}

    # 源文件被更新（所有收益率 +0.1pp），但不 force
    source_2024 = spike / "2024.xlsx"
    _bump_xlsx_values(source_2024, 0.1)
    with caplog.at_level(logging.WARNING, logger="ficc.data.loader"):
        second = _build(tmp_path, cfg, raw_spike_dir=spike)

    # 产物不变：实际解析的是已登记快照，而不是新源文件
    assert _sha256(paths.curve_processed) == processed_sha
    manifest_second = json.loads(paths.manifest.read_text(encoding="utf-8"))
    assert {rec["year"]: rec["sha256"] for rec in manifest_second["sources"]} == source_hashes_first
    assert any("不一致" in rec.message or "不可变快照" in rec.message for rec in caplog.records)
    # 血缘自洽：manifest 登记哈希 == 当前正式快照哈希
    snapshot_2024 = paths.snapshot_dir / "2024.xlsx"
    rec_2024 = next(rec for rec in manifest_second["sources"] if rec["year"] == 2024)
    assert rec_2024["sha256"] == _sha256(snapshot_2024)
    assert second.version_id == first.version_id

    # force 之后才采用源文件：产物变化，manifest 指向新快照
    third = _build(tmp_path, cfg, raw_spike_dir=spike, force=True)
    assert _sha256(paths.curve_processed) != processed_sha
    manifest_third = json.loads(paths.manifest.read_text(encoding="utf-8"))
    rec_2024_new = next(rec for rec in manifest_third["sources"] if rec["year"] == 2024)
    assert rec_2024_new["sha256"] == _sha256(source_2024)
    assert rec_2024_new["sha256"] == _sha256(snapshot_2024)
    assert third.version_id != first.version_id
    # 旧快照被归档，保留更新前版本（可回溯）
    archived = list((paths.snapshot_dir / "archive").glob("2024.*.xlsx"))
    assert archived
    frame = load_curve_long(cfg=cfg, paths=paths)
    assert len(frame) == len(third.frame)


# ---------------------------------------------------------------------------
# R04：整批原子提交（任一写入边界失败 → 指针不变、公开读取仍是旧版本）
# ---------------------------------------------------------------------------
def _snapshot_legacy_state(paths) -> dict[str, str]:
    return {
        "processed": _sha256(paths.curve_processed),
        "interim": _sha256(paths.curve_interim),
        "manifest": _sha256(paths.manifest),
        "pointer": _sha256(paths.current_pointer),
    }


@pytest.mark.parametrize(
    "boundary",
    ["staging_curve", "staging_raw", "staging_manifest", "legacy_copy", "pointer"],
)
def test_r04_atomic_boundaries_keep_old_version(
    tmp_path: Path, cfg, monkeypatch, boundary: str
) -> None:
    spike, first = _baseline_with_pointer(tmp_path, cfg)
    paths = first.paths
    state_before = _snapshot_legacy_state(paths)
    rows_before = len(first.frame)

    # 制造"新内容"：源文件 +0.1 并 force，确保没有注入失败时一定会发布新版本
    _bump_xlsx_values(spike / "2024.xlsx", 0.1)

    real_parquet = loader_module._write_parquet_atomic
    real_json = loader_module._write_json_atomic

    if boundary == "staging_curve":

        def fake_parquet(frame, path):
            if path.name == "curve_long.parquet" and ".staging" in str(path):
                raise OSError(f"注入失败：{path}")
            return real_parquet(frame, path)

        monkeypatch.setattr(loader_module, "_write_parquet_atomic", fake_parquet)
    elif boundary == "staging_raw":

        def fake_parquet(frame, path):
            if path.name == "curve_raw.parquet" and ".staging" in str(path):
                raise OSError(f"注入失败：{path}")
            return real_parquet(frame, path)

        monkeypatch.setattr(loader_module, "_write_parquet_atomic", fake_parquet)
    elif boundary == "staging_manifest":

        def fake_json(payload, path):
            if path.name == "manifest.json" and ".staging" in str(path):
                raise OSError(f"注入失败：{path}")
            return real_json(payload, path)

        monkeypatch.setattr(loader_module, "_write_json_atomic", fake_json)
    else:  # pointer

        def fake_json(payload, path):
            if path.name == "current.json":
                raise OSError(f"注入失败：{path}")
            return real_json(payload, path)

        monkeypatch.setattr(loader_module, "_write_json_atomic", fake_json)

    with pytest.raises(OSError, match="注入失败"):
        _build(tmp_path, cfg, raw_spike_dir=spike, force=True)

    # ① 指针未更新；② 旧路径兼容副本未变（指针失败时已回滚）；③ 公开读取仍是旧版本
    assert _sha256(paths.current_pointer) == state_before["pointer"]
    assert _snapshot_legacy_state(paths) == state_before
    old = load_curve_long(cfg=cfg, paths=paths)
    assert len(old) == rows_before
    pointer = json.loads(paths.current_pointer.read_text(encoding="utf-8"))
    assert (
        _sha256(pointer["dataset"]["path"] and Path(pointer["dataset"]["path"]))
        == pointer["dataset"]["sha256"]
    )


def test_r04_version_layout_and_pointer(tmp_path: Path, cfg) -> None:
    _, first = _baseline_with_pointer(tmp_path, cfg)
    paths = first.paths
    pointer = json.loads(paths.current_pointer.read_text(encoding="utf-8"))
    version_dir = paths.versions_dir / pointer["version_id"]
    assert version_dir.is_dir()
    for name in ("curve_long.parquet", "curve_raw.parquet", "manifest.json"):
        assert (version_dir / name).exists()
    assert pointer["manifest_path"].endswith("manifest.json")
    assert pointer["dataset"]["sha256"] == _sha256(version_dir / "curve_long.parquet")
    assert pointer["dataset"]["sha256"] == _sha256(paths.curve_processed)
    assert first.version_id == pointer["version_id"]
    manifest = json.loads((version_dir / "manifest.json").read_text(encoding="utf-8"))
    assert manifest["version_id"] == pointer["version_id"]
    assert manifest["artifact_hashes"]["curve_long"] == pointer["dataset"]["sha256"]


def test_r04_pointer_falls_back_to_legacy_path(tmp_path: Path, cfg) -> None:
    _, first = _baseline_with_pointer(tmp_path, cfg)
    paths = first.paths
    pointer = json.loads(paths.current_pointer.read_text(encoding="utf-8"))
    version_dir = paths.versions_dir / pointer["version_id"]
    for child in version_dir.iterdir():
        child.unlink()
    version_dir.rmdir()
    frame = load_curve_long(cfg=cfg, paths=paths)
    assert len(frame) == len(first.frame)
    funding = (
        load_funding_series(cfg=cfg, paths=paths) if paths.funding_processed.exists() else None
    )
    assert funding is None or len(funding) > 0


# ---------------------------------------------------------------------------
# R05：逐年覆盖退化闸门
# ---------------------------------------------------------------------------
def test_r05_one_day_drop_raises(tmp_path: Path, cfg) -> None:
    spike, first = _baseline_with_pointer(tmp_path, cfg)
    paths = first.paths
    state_before = _snapshot_legacy_state(paths)
    dropped = _drop_last_day_from_xlsx(spike / "2024.xlsx")

    with pytest.raises(DataValidationError, match=r"逐年覆盖退化|历史交易日"):
        _build(tmp_path, cfg, raw_spike_dir=spike, force=True)

    assert _snapshot_legacy_state(paths) == state_before
    assert dropped in {
        str(day) for day in pd.to_datetime(pd.read_parquet(paths.curve_processed)["date"]).dt.date
    }


def test_r05_allow_coverage_drop(tmp_path: Path, cfg) -> None:
    spike, first = _baseline_with_pointer(tmp_path, cfg)
    _drop_last_day_from_xlsx(spike / "2024.xlsx")
    result = _build(tmp_path, cfg, raw_spike_dir=spike, force=True, allow_coverage_drop=True)
    assert result.coverage is not None
    assert result.coverage["allowed"] is True
    assert result.coverage["n_missing_dates"] == 1
    assert result.manifest["checks"]["coverage_regression"]["allowed"] is True
    assert len(result.frame) < len(first.frame)


def test_r05_new_year_is_allowed(tmp_path: Path, cfg) -> None:
    spike = tmp_path / "raw_spike" / "chinabond_gzqx"
    spike.mkdir(parents=True)
    write_year_xlsx(spike / "2023.xlsx", pd.bdate_range("2023-01-03", "2023-12-29"))
    first = _build(
        tmp_path,
        cfg,
        raw_spike_dir=spike,
        start="2023-01-01",
        end="2023-12-31",
    )
    write_year_xlsx(spike / "2024.xlsx", pd.bdate_range("2024-01-03", "2024-12-29"))
    second = _build(
        tmp_path,
        cfg,
        raw_spike_dir=spike,
        start="2023-01-01",
        end="2024-12-31",
    )
    assert second.coverage is not None
    assert second.coverage["n_missing_dates"] == 0
    assert second.version_id != first.version_id
    assert set(second.frame["date"].dt.year) == {2023, 2024}


def test_r05_known_2y_gap_not_mistaken_as_date_loss(tmp_path: Path, cfg) -> None:
    spike = tmp_path / "raw_spike" / "chinabond_gzqx"
    spike.mkdir(parents=True)
    tenors_without_2y = tuple(t for t in CANONICAL_TENORS if t != 2.0)
    for year in (2016, 2017):
        write_year_xlsx(
            spike / f"{year}.xlsx",
            pd.bdate_range(f"{year}-01-04", f"{year}-12-29"),
            tenors=tenors_without_2y,
        )
    first = _build(
        tmp_path,
        cfg,
        raw_spike_dir=spike,
        start="2016-01-01",
        end="2017-12-31",
        min_rows=10,
    )
    second = _build(
        tmp_path,
        cfg,
        raw_spike_dir=spike,
        start="2016-01-01",
        end="2017-12-31",
        min_rows=10,
    )
    assert first.coverage is not None and second.coverage is not None
    assert second.coverage["n_missing_dates"] == 0
    assert 2.0 not in set(second.frame["tenor"])


# ---------------------------------------------------------------------------
# R06：当年刷新 / 新鲜度 / 资金面在线刷新
# ---------------------------------------------------------------------------
def test_r06_config_defaults(cfg) -> None:
    assert cfg.data.max_staleness_days == 5
    assert cfg.data.refresh_current_year is True


def test_r06_staleness_detection() -> None:
    from ficc.data.loader import _check_staleness

    stale = _check_staleness(date(2026, 10, 1), as_of=date(2026, 11, 6), max_staleness_days=5)
    assert stale.stale is True
    assert stale.staleness_days == 36
    assert stale.to_dict()["threshold_days"] == 5
    fresh = _check_staleness(date(2026, 11, 4), as_of=date(2026, 11, 6), max_staleness_days=5)
    assert fresh.stale is False
    offline = _check_staleness(
        date(2020, 1, 2), as_of=date(2026, 11, 6), max_staleness_days=5, offline=True
    )
    assert offline.stale is True and offline.offline is True


class _FakeFundingSource:
    """假 AKShare 资金面源（1200 个交易日）。"""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail

    def fetch_repo_rates(self, start=None, end=None):
        if self.fail:
            raise SourceError("模拟 akshare 失败")
        days = pd.bdate_range("2005-01-04", periods=1200)
        return pd.DataFrame(
            {
                "date": days,
                "FR001": 2.0,
                "FR007": 2.1,
                "FR014": 2.2,
                "FDR001": 1.9,
                "FDR007": 2.0,
                "FDR014": 2.1,
            }
        )


def _write_funding_csv(spike_dir: Path, rows: int = 1200) -> Path:
    """在离线缓存根目录写一份合法资金面 CSV（akshare 不可用时的降级输入）。"""
    days = pd.bdate_range("2005-01-04", periods=rows)
    csv_path = spike_dir.parent / REPO_CSV_NAME
    pd.DataFrame(
        {
            "date": days,
            "FR001": 1.5,
            "FR007": 1.6,
            "FR014": 1.7,
            "FDR001": 1.4,
            "FDR007": 1.5,
            "FDR014": 1.6,
        }
    ).to_csv(csv_path, index=False)
    return csv_path


def _patch_online_curve_loader(monkeypatch, spike_dir: Path) -> None:
    """让在线构建直接使用本地合成 xlsx（避免真实联网）。"""

    def fake_online(ctx, staging_root):
        frames = [
            parse_official_xlsx(path, year=int(path.stem))
            for path in sorted(spike_dir.glob("*.xlsx"))
        ]
        return frames, [], []

    monkeypatch.setattr(loader_module, "_load_online_chinabond", fake_online)


def test_r06_online_funding_refresh(tmp_path: Path, cfg, monkeypatch) -> None:
    spike = make_spike_dir(tmp_path)
    paths = make_paths(tmp_path, cfg)
    _patch_online_curve_loader(monkeypatch, spike)
    monkeypatch.setattr(loader_module, "AK_AVAILABLE", True)
    monkeypatch.setattr(loader_module, "AkshareSource", lambda cfg=None: _FakeFundingSource())

    result = fetch_curve_data(
        start="2023-01-01",
        end="2024-12-31",
        source="chinabond",
        offline=False,
        cfg=cfg,
        paths=paths,
        raw_spike_dir=spike,
        min_rows=100,
        min_dates=1,
        write_funding=True,
    )
    assert result.manifest["funding"] is not None
    assert result.manifest["funding"]["source_url"] == "akshare://repo_rate_hist"
    funding = load_funding_series(cfg=cfg, paths=paths)
    assert len(funding) == 1200
    assert str(funding["date"].min().date()) == "2005-01-04"


def test_r06_online_funding_falls_back_to_csv(tmp_path: Path, cfg, monkeypatch, caplog) -> None:
    spike = make_spike_dir(tmp_path)
    paths = make_paths(tmp_path, cfg)
    _patch_online_curve_loader(monkeypatch, spike)
    monkeypatch.setattr(loader_module, "AK_AVAILABLE", True)
    monkeypatch.setattr(
        loader_module, "AkshareSource", lambda cfg=None: _FakeFundingSource(fail=True)
    )
    csv_path = _write_funding_csv(spike)

    with caplog.at_level(logging.WARNING, logger="ficc.data.loader"):
        result = fetch_curve_data(
            start="2023-01-01",
            end="2024-12-31",
            source="chinabond",
            offline=False,
            cfg=cfg,
            paths=paths,
            raw_spike_dir=spike,
            min_rows=100,
            min_dates=1,
            write_funding=True,
        )
    assert result.manifest["funding"] is not None
    assert result.manifest["funding"]["source_url"].endswith(csv_path.name)
    assert any("降级" in rec.message or "失败" in rec.message for rec in caplog.records)
    funding = load_funding_series(cfg=cfg, paths=paths)
    assert len(funding) == 1200
    assert float(funding["FR007"].iloc[0]) == pytest.approx(1.6, abs=1e-9)


def test_r03_offline_works_from_registered_snapshots_only(tmp_path: Path, cfg) -> None:
    """raw_spike 源文件被清理后，仍应能解析已登记快照复现（R03）。"""
    spike, first = _baseline_with_pointer(tmp_path, cfg)
    source_2024 = spike / "2024.xlsx"
    source_2024.unlink()
    second = _build(tmp_path, cfg, raw_spike_dir=spike)
    assert _sha256(first.paths.curve_processed) == _sha256(second.paths.curve_processed)
    assert second.version_id == first.version_id


def test_r04_corrupted_version_file_raises(tmp_path: Path, cfg) -> None:
    _, first = _baseline_with_pointer(tmp_path, cfg)
    paths = first.paths
    pointer = json.loads(paths.current_pointer.read_text(encoding="utf-8"))
    version_curve = paths.versions_dir / pointer["version_id"] / "curve_long.parquet"
    version_curve.write_bytes(b"corrupted")
    with pytest.raises(DataValidationError, match="哈希校验失败"):
        load_curve_long(cfg=cfg, paths=paths)
