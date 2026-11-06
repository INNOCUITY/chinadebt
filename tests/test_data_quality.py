"""数据质量报告测试（离线，构造已知缺陷断言能被检出）。

覆盖：
1. 单日跳变 > 阈值（含阈值参数）；
2. 各期限缺失率 / 期限覆盖矩阵；
3. 缺失工作日与连续缺口；
4. 年度文件重叠（重复行数、日期范围、数值一致性、冲突识别）；
5. 空表/缺列必须报错；
6. 可打印报告；
7. 真实数据冒烟（文件不存在时 skip）。
"""

from __future__ import annotations

import io
from pathlib import Path

import pandas as pd
import pytest

from ficc.data.quality import (
    QualityReport,
    check_jumps,
    coverage_matrix,
    duplicate_report,
    missing_days_report,
    run_quality_checks,
    tenor_missing_report,
)
from ficc.data.schema import CANONICAL_TENORS, DataValidationError

ROOT = Path(__file__).resolve().parents[1]
REAL_PROCESSED = ROOT / "data" / "processed" / "curve_long.parquet"


def _dt(frame: pd.DataFrame) -> pd.DataFrame:
    """fixture 的 date 是 datetime.date，统一转成 datetime64 便于比较。"""
    out = frame.copy()
    out["date"] = pd.to_datetime(out["date"])
    return out


def _set_rate(frame: pd.DataFrame, day: str, tenor: float, value: float) -> pd.DataFrame:
    out = _dt(frame)
    mask = (out["date"] == pd.Timestamp(day)) & ((out["tenor"].astype(float) - tenor).abs() <= 1e-9)
    out.loc[mask, "ytm_pct"] = value
    return out


# ---------------------------------------------------------------------------
# 跳变
# ---------------------------------------------------------------------------
def test_jump_detected(synthetic_long_df: pd.DataFrame) -> None:
    frame = _set_rate(synthetic_long_df, "2023-01-05", 10.0, 3.16)  # 2.91 → 3.16 = +25bp
    jumps = check_jumps(frame, threshold_bp=20.0)
    assert len(jumps) == 1
    row = jumps.iloc[0]
    assert row["tenor"] == 10.0
    assert str(pd.Timestamp(row["date"]).date()) == "2023-01-05"
    assert row["diff_bp"] == pytest.approx(25.0, abs=0.05)
    assert row["direction"] == "上行"


def test_jump_below_threshold_ignored(synthetic_long_df: pd.DataFrame) -> None:
    frame = _set_rate(synthetic_long_df, "2023-01-05", 10.0, 3.00)  # +10bp
    assert check_jumps(frame, threshold_bp=20.0).empty
    assert len(check_jumps(frame, threshold_bp=5.0)) == 1


def test_jump_sorted_by_magnitude(synthetic_long_df: pd.DataFrame) -> None:
    frame = _dt(synthetic_long_df)
    frame = _set_rate(frame, "2023-01-05", 10.0, 3.50)  # 2.91 → 3.50 = +59bp
    frame = _set_rate(frame, "2023-01-05", 2.0, 2.85)  # 2.47 → 2.85 = +38bp
    jumps = check_jumps(frame, threshold_bp=20.0)
    assert len(jumps) == 2
    assert jumps["diff_bp"].abs().is_monotonic_decreasing


def test_quality_summary_jumps(synthetic_long_df: pd.DataFrame) -> None:
    frame = _set_rate(synthetic_long_df, "2023-01-05", 10.0, 3.16)
    report = run_quality_checks(frame, jump_threshold_bp=20.0)
    assert report.summary["n_jumps"] == 1
    assert report.summary["max_abs_jump_bp"] == pytest.approx(25.0, abs=0.05)
    assert report.summary["jump_threshold_bp"] == 20.0


# ---------------------------------------------------------------------------
# 缺失 / 覆盖
# ---------------------------------------------------------------------------
def test_tenor_missing_report(synthetic_long_df: pd.DataFrame) -> None:
    base = _dt(synthetic_long_df)
    frame = base.loc[
        ~((base["date"] == pd.Timestamp("2023-01-04")) & (base["tenor"].astype(float) == 2.0))
    ].copy()
    table = tenor_missing_report(frame)
    assert len(table) == len(CANONICAL_TENORS)
    row_2y = table.loc[table["tenor"].astype(float) == 2.0].iloc[0]
    assert row_2y["n_present"] == 2
    assert row_2y["n_expected"] == 3
    assert row_2y["missing"] == 1
    assert row_2y["missing_rate"] == pytest.approx(1 / 3)
    row_10y = table.loc[table["tenor"].astype(float) == 10.0].iloc[0]
    assert row_10y["missing"] == 0


def test_coverage_matrix(synthetic_long_df: pd.DataFrame) -> None:
    base = _dt(synthetic_long_df)
    frame = base.loc[
        ~((base["date"] == pd.Timestamp("2023-01-04")) & (base["tenor"].astype(float) == 2.0))
    ].copy()
    matrix = coverage_matrix(frame)
    assert "n_dates" in matrix.columns
    assert "2Y" in matrix.columns
    assert matrix.loc[2023, "n_dates"] == 3
    assert matrix.loc[2023, "2Y"] == pytest.approx(2 / 3, abs=1e-4)
    assert matrix.loc[2023, "10Y"] == pytest.approx(1.0)


def test_missing_weekday_gap() -> None:
    days = pd.bdate_range("2023-01-02", "2023-01-13")
    rows = [
        {"date": day, "tenor": tenor, "ytm_pct": 2.0 + tenor * 0.01}
        for day in days
        for tenor in CANONICAL_TENORS
    ]
    frame = pd.DataFrame(rows)
    frame = frame.loc[~frame["date"].isin([pd.Timestamp("2023-01-04"), pd.Timestamp("2023-01-05")])]
    table = missing_days_report(frame)
    assert not table.empty
    gap = table.drop_duplicates(subset=["gap_id"]).iloc[0]
    assert gap["gap_days"] >= 2
    assert str(pd.Timestamp(gap["gap_start"]).date()) == "2023-01-04"


# ---------------------------------------------------------------------------
# 年度重叠 / 重复
# ---------------------------------------------------------------------------
def test_duplicate_overlap_consistent(synthetic_long_df: pd.DataFrame) -> None:
    base = _dt(synthetic_long_df)
    frame = pd.concat([base, base], ignore_index=True)
    detail, summary = duplicate_report(frame)
    assert summary["duplicate_groups"] == len(synthetic_long_df)  # 3 天 × 11 期限
    assert summary["duplicate_rows"] == 2 * len(synthetic_long_df)
    assert summary["excess_rows"] == len(synthetic_long_df)
    assert summary["conflict_groups"] == 0
    assert summary["values_consistent"] is True
    assert summary["date_min"] == "2023-01-03"
    assert summary["date_max"] == "2023-01-05"
    assert detail["consistent"].all()


def test_duplicate_conflict_detected(synthetic_long_df: pd.DataFrame) -> None:
    base = _dt(synthetic_long_df)
    duplicate = base.copy()
    duplicate.loc[0, "ytm_pct"] = duplicate.loc[0, "ytm_pct"] + 0.01
    frame = pd.concat([base, duplicate], ignore_index=True)
    detail, summary = duplicate_report(frame)
    assert summary["conflict_groups"] == 1
    assert summary["values_consistent"] is False
    conflicts = detail.loc[~detail["consistent"]]
    assert len(conflicts) == 1
    assert conflicts.iloc[0]["n_values"] == 2


def test_quality_report_reports_duplicates(synthetic_long_df: pd.DataFrame) -> None:
    base = _dt(synthetic_long_df)
    frame = pd.concat([base, base], ignore_index=True)
    report = run_quality_checks(frame)
    assert report.summary["dup_duplicate_groups"] == len(synthetic_long_df)
    assert report.summary["dup_values_consistent"] is True
    assert not report.duplicates.empty


# ---------------------------------------------------------------------------
# 边界 / 打印
# ---------------------------------------------------------------------------
def test_quality_rejects_empty_or_bad_frame() -> None:
    with pytest.raises(DataValidationError, match="空表"):
        run_quality_checks(pd.DataFrame())
    with pytest.raises(DataValidationError, match="缺少必需列"):
        run_quality_checks(pd.DataFrame({"date": ["2024-01-02"], "tenor": [1.0]}))


def test_print_report_contains_sections(synthetic_long_df: pd.DataFrame) -> None:
    report = run_quality_checks(synthetic_long_df)
    buffer = io.StringIO()
    report.print_report(file=buffer)
    text = buffer.getvalue()
    for section in ("数据质量报告", "单日跳变", "各期限覆盖", "期限覆盖矩阵", "年度文件重叠"):
        assert section in text


def test_report_to_frames_and_dict(synthetic_long_df: pd.DataFrame) -> None:
    report: QualityReport = run_quality_checks(synthetic_long_df)
    frames = report.to_frames()
    assert set(frames) == {
        "jumps",
        "missing_by_tenor",
        "missing_days",
        "coverage",
        "duplicates",
    }
    assert report.to_dict()["rows"] == len(synthetic_long_df)


# ---------------------------------------------------------------------------
# 真实数据冒烟
# ---------------------------------------------------------------------------
def test_real_data_quality_smoke() -> None:
    if not REAL_PROCESSED.exists():
        pytest.skip("缺少 data/processed/curve_long.parquet（先运行 fetch_data --offline）")
    frame = pd.read_parquet(REAL_PROCESSED)
    report = run_quality_checks(frame, jump_threshold_bp=20.0)
    assert report.summary["n_dates"] == 6_204
    assert report.summary["rows"] == 67_472
    assert report.summary["missing_cells"] == 772
    assert report.summary["missing_rate"] == pytest.approx(772 / 68_244, abs=5e-7)
    assert report.summary["dup_duplicate_groups"] == 0
    # 官方历史数据在 2002–2004 存在极端跳变，质量报告必须能捕捉到
    assert report.summary["n_jumps"] > 0
    assert report.summary["max_abs_jump_bp"] > 100
    # 2Y 在 2016–2019 缺口区间应体现为缺失率
    table = report.missing_by_tenor
    row_2y = table.loc[table["tenor"].astype(float) == 2.0].iloc[0]
    assert row_2y["n_present"] == 5_432
    assert row_2y["missing"] == 772
