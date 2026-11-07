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
from datetime import date
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


# ---------------------------------------------------------------------------
# 横截面不一致：可疑跳变点标记（R-新，只标记不剔除）
# ---------------------------------------------------------------------------
from ficc.data.quality import (  # noqa: E402
    SUSPECT_COLUMNS,
    detect_suspect_dates,
    suspect_summary,
    write_suspect_report,
)


def _clean_panel_long(days: int = 120, *, seed_offset: float = 0.0) -> pd.DataFrame:
    """构造无异常的平滑面板（11 个期限 × N 个交易日，日变动 < 0.1bp）。"""
    dates = pd.bdate_range("2023-01-03", periods=days)
    base = {tenor: 2.0 + 0.08 * tenor + seed_offset for tenor in CANONICAL_TENORS}
    rows = []
    for index, day in enumerate(dates):
        for tenor, level in base.items():
            rows.append(
                {
                    "date": day,
                    "tenor": float(tenor),
                    "ytm_pct": round(level + 0.0005 * index, 6),
                }
            )
    return pd.DataFrame(rows)


def _panel_wide(long_df: pd.DataFrame) -> pd.DataFrame:
    return long_df.pivot(index="date", columns="tenor", values="ytm_pct").sort_index()


def test_suspect_clean_panel_has_no_false_positive() -> None:
    clean = _clean_panel_long()
    suspects = detect_suspect_dates(clean)
    assert suspects.empty
    assert list(suspects.columns) == list(SUSPECT_COLUMNS)
    assert suspect_summary(suspects)["n_total"] == 0


def test_suspect_detects_idiosyncratic_spike() -> None:
    clean = _clean_panel_long()
    panel = _panel_wide(clean)
    dates = list(panel.index)
    spike_date = dates[60]
    clean.loc[
        (clean["date"] == spike_date) & (clean["tenor"].astype(float) == 10.0),
        "ytm_pct",
    ] += 0.80  # +80bp 孤立尖峰（其他期限不动）
    suspects = detect_suspect_dates(clean)

    hits = suspects[
        (suspects["date"] == spike_date)
        & ((suspects["tenor"].astype(float) - 10.0).abs() < 1e-9)
        & (suspects["kind"] == "idiosyncratic_spike")
    ]
    assert len(hits) == 1
    row = hits.iloc[0]
    assert row["delta_bp"] == pytest.approx(80.0, abs=0.5)
    assert abs(row["median_cross_delta_bp"]) < 10.0
    assert row["severity"] in {"low", "medium", "high"}
    # 横截面其他期限不应被连带标记为同日的 idiosyncratic_spike
    same_day = suspects[
        (suspects["date"] == spike_date) & (suspects["kind"] == "idiosyncratic_spike")
    ]
    assert ((same_day["tenor"].astype(float) - 10.0).abs() > 1e-9).sum() == 0


def test_suspect_detects_reversal_with_roundtrip() -> None:
    clean = _clean_panel_long()
    panel = _panel_wide(clean)
    dates = list(panel.index)
    spike_date = dates[70]
    next_date = dates[71]
    tenor = 2.0
    mask = (clean["date"] == spike_date) & ((clean["tenor"].astype(float) - tenor).abs() < 1e-9)
    clean.loc[mask, "ytm_pct"] += 0.80  # 跳出去
    # 次日回到原趋势（下一交易日的值本就等于趋势，因此自然"跳回来"）
    suspects = detect_suspect_dates(clean)
    hits = suspects[
        (suspects["date"] == spike_date)
        & ((suspects["tenor"].astype(float) - tenor).abs() < 1e-9)
        & (suspects["kind"] == "reversal")
    ]
    assert len(hits) == 1
    row = hits.iloc[0]
    expected_roundtrip = 2 * 80.0  # ±80bp → 往返约 160bp
    assert row["roundtrip_bp"] == pytest.approx(expected_roundtrip, abs=1.0)
    assert row["delta_bp"] == pytest.approx(80.0, abs=0.5)
    assert row["next_value"] < row["value"]
    assert spike_date == pd.Timestamp(row["date"])
    # 回跳日（t+1）也可能被标记，但 t 的 reversal 必须存在
    assert next_date >= spike_date


def test_suspect_detects_level_outlier() -> None:
    clean = _clean_panel_long()
    panel = _panel_wide(clean)
    day = list(panel.index)[80]
    clean.loc[
        (clean["date"] == day) & ((clean["tenor"].astype(float) - 30.0).abs() < 1e-9),
        "ytm_pct",
    ] = 15.0  # >12% 越界
    suspects = detect_suspect_dates(clean)
    hits = suspects[
        (suspects["date"] == day)
        & ((suspects["tenor"].astype(float) - 30.0).abs() < 1e-9)
        & (suspects["kind"] == "level_outlier")
    ]
    assert len(hits) == 1
    assert hits.iloc[0]["value"] == pytest.approx(15.0)
    assert hits.iloc[0]["severity"] == "high"


def test_suspect_requires_columns() -> None:
    with pytest.raises(DataValidationError, match="缺少必需列"):
        detect_suspect_dates(pd.DataFrame({"date": ["2024-01-02"], "tenor": [1.0]}))
    with pytest.raises(DataValidationError, match="空表"):
        detect_suspect_dates(pd.DataFrame())


def test_write_suspect_report(tmp_path: Path) -> None:
    clean = _clean_panel_long()
    panel = _panel_wide(clean)
    spike_date = list(panel.index)[60]
    clean.loc[
        (clean["date"] == spike_date) & ((clean["tenor"].astype(float) - 10.0).abs() < 1e-9),
        "ytm_pct",
    ] += 0.80
    output = tmp_path / "suspects.csv"
    path, suspects, summary = write_suspect_report(clean, output=output)
    assert path == output
    assert output.exists()
    written = pd.read_csv(output, parse_dates=["date"])
    assert list(written.columns) == list(SUSPECT_COLUMNS)
    assert len(written) == len(suspects) > 0
    assert summary["n_total"] == len(suspects)
    assert summary["by_year"]
    assert summary["by_kind"].get("idiosyncratic_spike", 0) >= 1


def test_suspect_quality_cli(tmp_path: Path, cfg) -> None:
    """`python -m ficc.data.quality --suspect-report` 可写出清单并打印摘要。"""
    from ficc.data.quality import main as quality_main

    clean = _clean_panel_long()
    panel = _panel_wide(clean)
    spike_date = list(panel.index)[60]
    clean.loc[
        (clean["date"] == spike_date) & ((clean["tenor"].astype(float) - 10.0).abs() < 1e-9),
        "ytm_pct",
    ] += 0.80
    data_path = tmp_path / "curve.parquet"
    clean.to_parquet(data_path, index=False)
    output = tmp_path / "suspects.csv"
    exit_code = quality_main(
        [
            "--data",
            str(data_path),
            "--suspect-report",
            "--suspect-out",
            str(output),
            "--max-rows",
            "3",
        ]
    )
    assert exit_code == 0
    assert output.exists()
    written = pd.read_csv(output)
    assert "idiosyncratic_spike" in set(written["kind"])


def test_real_sample_lead_anomalies_are_flagged() -> None:
    """真实全样本：复核 Lead 给的 2002/2004 具体异常日期必须被标记。"""
    if not REAL_PROCESSED.exists():
        pytest.skip("缺少 data/processed/curve_long.parquet（先运行 fetch_data --offline）")
    frame = pd.read_parquet(REAL_PROCESSED)
    suspects = detect_suspect_dates(frame)
    assert not suspects.empty
    suspects["date"] = pd.to_datetime(suspects["date"])

    def flagged(day: str, tenor: float) -> pd.DataFrame:
        hit = suspects[
            (suspects["date"] == pd.Timestamp(day))
            & ((suspects["tenor"].astype(float) - tenor).abs() < 1e-9)
        ]
        return hit

    # 30Y：2002-04-26 10.5366% 尖峰（随后跳回）
    hit_30y = flagged("2002-04-26", 30.0)
    assert not hit_30y.empty
    assert hit_30y.iloc[0]["severity"] == "high"
    assert hit_30y.iloc[0]["roundtrip_bp"] > 1000.0

    # 短端：2004-08-18 的 0.25Y / 1Y / 2Y 跳回
    for tenor in (0.25, 1.0, 2.0):
        hit = flagged("2004-08-18", tenor)
        assert not hit.empty, f"2004-08-18 {tenor:g}Y 未被标记"
        assert hit.iloc[0]["kind"] == "reversal"
        assert hit.iloc[0]["delta_bp"] < -100.0

    # 分布应高度集中在 2002–2005（官方早期文件问题）
    early = suspects["date"].dt.year.isin([2002, 2003, 2004, 2005]).sum()
    assert early / len(suspects) >= 0.9
    summary = suspect_summary(suspects)
    assert summary["n_total"] == len(suspects)
    assert summary["by_year"].get("2002", 0) > 0
    assert summary["n_high"] > 0


# ---------------------------------------------------------------------------
# R20：统一重复校验（一致重复可去重、冲突必须报错、行序无关）
# ---------------------------------------------------------------------------
from ficc.data.quality import (  # noqa: E402
    filter_causally_usable,
)


def _conflict_frame(frame: pd.DataFrame, day: pd.Timestamp, tenor: float) -> pd.DataFrame:
    """复制 frame 并在指定 (date, tenor) 的一行上制造 +0.5pp 冲突。"""
    out = frame.copy()
    mask = (out["date"] == day) & ((out["tenor"].astype(float) - tenor).abs() < 1e-9)
    positions = list(out.index[mask])
    assert len(positions) >= 2, "需要先构造重复行才能注入冲突"
    out.loc[positions[0], "ytm_pct"] = out.loc[positions[0], "ytm_pct"] + 0.5
    return out


def test_r20_conflicting_duplicates_raise_with_detail(tmp_path: Path) -> None:
    clean = _clean_panel_long()
    panel = _panel_wide(clean)
    day = list(panel.index)[10]
    duplicated = pd.concat([clean, clean], ignore_index=True)
    conflict = _conflict_frame(duplicated, day, 10.0)
    output = tmp_path / "suspects.csv"

    entry_points = [
        ("run_quality_checks", lambda f: run_quality_checks(f)),
        ("check_jumps", lambda f: check_jumps(f)),
        ("detect_suspect_dates", lambda f: detect_suspect_dates(f)),
        ("write_suspect_report", lambda f: write_suspect_report(f, output=output)),
    ]
    details: list[str] = []
    for name, call in entry_points:
        with pytest.raises(DataValidationError, match="冲突") as excinfo:
            call(conflict)
        message = str(excinfo.value)
        assert "拒绝静默去重" in message, name
        assert "2023-" in message, name
        assert "10" in message, name
        details.append(message.split("冲突明细", 1)[-1])
        if output.exists():
            output.unlink()
    # 四个入口共用同一套校验逻辑 → 冲突明细完全一致
    assert len(set(details)) == 1


def test_r20_consistent_duplicates_row_order_invariant(tmp_path: Path) -> None:
    clean = _clean_panel_long()
    panel = _panel_wide(clean)
    spike_day = list(panel.index)[60]
    clean.loc[
        (clean["date"] == spike_day) & ((clean["tenor"].astype(float) - 10.0).abs() < 1e-9),
        "ytm_pct",
    ] += 0.80
    duplicated = pd.concat([clean, clean], ignore_index=True)
    forward = duplicated.reset_index(drop=True)
    reversed_order = duplicated.iloc[::-1].reset_index(drop=True)

    report_forward = run_quality_checks(forward)
    report_reversed = run_quality_checks(reversed_order)
    for key in ("rows", "n_dates", "date_min", "date_max", "dup_duplicate_groups"):
        assert report_forward.summary[key] == report_reversed.summary[key], key
    assert report_forward.summary["dup_duplicate_groups"] > 0  # 一致重复仍被统计

    suspects_forward = detect_suspect_dates(forward)
    suspects_reversed = detect_suspect_dates(reversed_order)
    assert not suspects_forward.empty
    pd.testing.assert_frame_equal(suspects_forward, suspects_reversed)

    out_a = tmp_path / "a.csv"
    out_b = tmp_path / "b.csv"
    write_suspect_report(forward, output=out_a)
    write_suspect_report(reversed_order, output=out_b)
    assert out_a.read_text(encoding="utf-8-sig") == out_b.read_text(encoding="utf-8-sig")


def test_r20_conflict_error_row_order_invariant() -> None:
    clean = _clean_panel_long()
    panel = _panel_wide(clean)
    day = list(panel.index)[10]
    duplicated = pd.concat([clean, clean], ignore_index=True)
    conflict = _conflict_frame(duplicated, day, 10.0)
    forward = conflict.reset_index(drop=True)
    reversed_order = conflict.iloc[::-1].reset_index(drop=True)

    with pytest.raises(DataValidationError) as exc_forward:
        detect_suspect_dates(forward)
    with pytest.raises(DataValidationError) as exc_reversed:
        detect_suspect_dates(reversed_order)
    assert str(exc_forward.value) == str(exc_reversed.value)


def test_r20_real_curve_long_has_no_conflicts() -> None:
    if not REAL_PROCESSED.exists():
        pytest.skip("缺少 data/processed/curve_long.parquet（先运行 fetch_data --offline）")
    frame = pd.read_parquet(REAL_PROCESSED)
    report = run_quality_checks(frame, jump_threshold_bp=20.0)  # 不抛冲突
    assert report.summary["rows"] == 67_472
    detail, summary = duplicate_report(frame)
    assert summary["conflict_groups"] == 0
    assert detail.loc[~detail["consistent"]].empty


# ---------------------------------------------------------------------------
# R21：可疑点 known_at 因果可用性
# ---------------------------------------------------------------------------
def test_r21_known_at_synthetic_causality() -> None:
    clean = _clean_panel_long()
    panel = _panel_wide(clean)
    dates = list(panel.index)
    spike_day = dates[60]
    clean.loc[
        (clean["date"] == spike_day) & ((clean["tenor"].astype(float) - 10.0).abs() < 1e-9),
        "ytm_pct",
    ] += 0.80
    suspects = detect_suspect_dates(clean)

    # 全表无缺失、无早于 date 的 known_at（无前视）
    assert suspects["known_at"].notna().all()
    assert (suspects["known_at"] >= suspects["date"]).all()

    spike_hits = suspects[
        (suspects["kind"] == "idiosyncratic_spike")
        & (suspects["date"] == spike_day)
        & ((suspects["tenor"].astype(float) - 10.0).abs() < 1e-9)
    ]
    assert len(spike_hits) == 1
    assert spike_hits.iloc[0]["known_at"] == spike_hits.iloc[0]["date"]

    reversal_hits = suspects[
        (suspects["kind"] == "reversal")
        & (suspects["date"] == spike_day)
        & ((suspects["tenor"].astype(float) - 10.0).abs() < 1e-9)
    ]
    assert len(reversal_hits) == 1
    reversal_row = reversal_hits.iloc[0]
    assert reversal_row["known_at"] > reversal_row["date"]
    assert reversal_row["known_at"] == dates[61]  # 下一交易日


def test_r21_known_at_real_data_causality() -> None:
    if not REAL_PROCESSED.exists():
        pytest.skip("缺少 data/processed/curve_long.parquet（先运行 fetch_data --offline）")
    frame = pd.read_parquet(REAL_PROCESSED)
    suspects = detect_suspect_dates(frame)

    assert suspects["known_at"].notna().all()
    assert (suspects["known_at"] >= suspects["date"]).all()
    reversal = suspects.loc[suspects["kind"] == "reversal"]
    assert len(reversal) > 0
    assert (reversal["known_at"] > reversal["date"]).all()
    non_reversal = suspects.loc[suspects["kind"] != "reversal"]
    assert (non_reversal["known_at"] == non_reversal["date"]).all()

    # 具体复核：reversal 的 known_at = 下一交易日
    hit_30y = suspects[
        (suspects["date"] == pd.Timestamp("2002-04-26"))
        & ((suspects["tenor"].astype(float) - 30.0).abs() < 1e-9)
        & (suspects["kind"] == "reversal")
    ]
    assert not hit_30y.empty
    assert pd.Timestamp(hit_30y.iloc[0]["known_at"]) == pd.Timestamp("2002-04-27")
    for tenor in (0.25, 1.0, 2.0):
        hit = suspects[
            (suspects["date"] == pd.Timestamp("2004-08-18"))
            & ((suspects["tenor"].astype(float) - tenor).abs() < 1e-9)
            & (suspects["kind"] == "reversal")
        ]
        assert not hit.empty
        assert pd.Timestamp(hit.iloc[0]["known_at"]) == pd.Timestamp("2004-08-19")

    # 因果过滤：2004-08-18 当天不可用，2004-08-19 收盘后才可用
    before = filter_causally_usable(suspects, date(2004, 8, 18))
    after = filter_causally_usable(suspects, date(2004, 8, 19))
    assert (before["known_at"] <= pd.Timestamp("2004-08-18")).all()
    assert len(after) > len(before)
    assert (after["known_at"] <= pd.Timestamp("2004-08-19")).all()


def test_r21_csv_contains_known_at(tmp_path: Path) -> None:
    clean = _clean_panel_long()
    panel = _panel_wide(clean)
    spike_day = list(panel.index)[60]
    clean.loc[
        (clean["date"] == spike_day) & ((clean["tenor"].astype(float) - 10.0).abs() < 1e-9),
        "ytm_pct",
    ] += 0.80
    output = tmp_path / "suspects.csv"
    _, _, summary = write_suspect_report(clean, output=output)
    written = pd.read_csv(output, parse_dates=["date", "known_at"])
    assert "known_at" in written.columns
    assert written["known_at"].notna().all()
    assert (written["known_at"] >= written["date"]).all()
    assert summary["known_at"]["n_missing"] == 0
    assert summary["known_at"]["reversal_all_next_day"] is True
    assert summary["known_at"]["n_later"] == int((written["known_at"] > written["date"]).sum())


def test_r21_filter_causally_usable_requires_known_at() -> None:
    legacy = pd.DataFrame(
        {
            "date": [pd.Timestamp("2024-01-02")],
            "tenor": [10.0],
            "term_label": ["10Y"],
            "kind": ["idiosyncratic_spike"],
            "prev_value": [2.0],
            "value": [9.0],
            "next_value": [2.0],
            "delta_bp": [700.0],
            "median_cross_delta_bp": [0.0],
            "roundtrip_bp": [float("nan")],
            "severity": ["high"],
        }
    )
    with pytest.raises(DataValidationError, match="known_at"):
        filter_causally_usable(legacy, date(2024, 1, 2))
    empty = filter_causally_usable(pd.DataFrame(), date(2024, 1, 2))
    assert empty.empty
