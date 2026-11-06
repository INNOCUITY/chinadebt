"""数据契约（schema）测试。

这是数据进入模型前的最后一道闸门，所以测试要覆盖**每一条拒绝规则**：
坏数据必须报错，且报错信息要能定位到具体行/日期。
"""

from __future__ import annotations

from datetime import date

import numpy as np
import pandas as pd
import pytest
from pydantic import ValidationError

from ficc.data.schema import (
    CANONICAL_TENORS,
    LABEL_TO_TENOR,
    BondStatic,
    BondValuation,
    CurveQuote,
    DataValidationError,
    panel_to_long,
    to_long_frame,
    to_panel,
    validate_long_frame,
)


# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------
def test_canonical_tenors_are_sane() -> None:
    assert len(CANONICAL_TENORS) == 11
    assert tuple(sorted(CANONICAL_TENORS)) == CANONICAL_TENORS
    assert CANONICAL_TENORS[0] == 0.25
    assert CANONICAL_TENORS[-1] == 30.0


def test_label_mapping_covers_official_labels() -> None:
    """中债官方 xlsx 的期限标签必须都能映射。"""
    for label in ("0d", "1m", "3m", "6m", "9m", "1y", "2y", "5y", "10y", "30y"):
        assert label in LABEL_TO_TENOR
    assert LABEL_TO_TENOR["3m"] == pytest.approx(0.25)
    assert LABEL_TO_TENOR["10y"] == pytest.approx(10.0)


# ---------------------------------------------------------------------------
# 长表校验
# ---------------------------------------------------------------------------
def test_validate_long_frame_ok(synthetic_long_df: pd.DataFrame) -> None:
    out = validate_long_frame(synthetic_long_df)
    assert list(out.columns[:3]) == ["date", "tenor", "ytm_pct"]
    assert pd.api.types.is_datetime64_any_dtype(out["date"])
    assert out["tenor"].dtype == np.float64
    # 必须按 (date, tenor) 排序
    assert out[["date", "tenor"]].equals(
        out.sort_values(["date", "tenor"]).reset_index(drop=True)[["date", "tenor"]]
    )


def test_validate_long_frame_rejects_missing_column() -> None:
    df = pd.DataFrame({"date": [date(2024, 1, 2)], "tenor": [1.0]})
    with pytest.raises(DataValidationError, match="缺少必需列"):
        validate_long_frame(df)


def test_validate_long_frame_rejects_nan_yield() -> None:
    df = pd.DataFrame(
        {"date": [date(2024, 1, 2)] * 2, "tenor": [1.0, 2.0], "ytm_pct": [2.0, np.nan]}
    )
    with pytest.raises(DataValidationError, match="NaN"):
        validate_long_frame(df)


def test_validate_long_frame_rejects_duplicate_points() -> None:
    df = pd.DataFrame({"date": [date(2024, 1, 2)] * 2, "tenor": [1.0, 1.0], "ytm_pct": [2.0, 2.1]})
    with pytest.raises(DataValidationError, match="重复"):
        validate_long_frame(df)


def test_validate_long_frame_rejects_out_of_range() -> None:
    df = pd.DataFrame({"date": [date(2024, 1, 2)], "tenor": [1.0], "ytm_pct": [85.0]})
    with pytest.raises(DataValidationError, match="越界"):
        validate_long_frame(df)


def test_validate_long_frame_rejects_bad_date() -> None:
    df = pd.DataFrame({"date": ["not-a-date"], "tenor": [1.0], "ytm_pct": [2.0]})
    with pytest.raises(DataValidationError, match="date"):
        validate_long_frame(df)


def test_validate_long_frame_requires_full_grid_when_asked() -> None:
    df = pd.DataFrame(
        {"date": [date(2024, 1, 2)] * 3, "tenor": [1.0, 2.0, 3.0], "ytm_pct": [2.0, 2.1, 2.2]}
    )
    with pytest.raises(DataValidationError, match="期限点不足"):
        validate_long_frame(df, require_full_grid=True)
    # 不要求全网格时应该通过
    assert len(validate_long_frame(df)) == 3


# ---------------------------------------------------------------------------
# 面板转换
# ---------------------------------------------------------------------------
def test_panel_roundtrip(synthetic_long_df: pd.DataFrame) -> None:
    panel = to_panel(synthetic_long_df)
    assert isinstance(panel.index, pd.DatetimeIndex)
    assert list(panel.columns) == list(CANONICAL_TENORS)
    assert panel.shape == (3, len(CANONICAL_TENORS))

    back = panel_to_long(panel)
    orig = validate_long_frame(synthetic_long_df)
    pd.testing.assert_frame_equal(
        back.reset_index(drop=True)[["date", "tenor", "ytm_pct"]],
        orig[["date", "tenor", "ytm_pct"]],
        check_dtype=False,
    )


def test_panel_keeps_nan_for_missing_points(synthetic_long_df: pd.DataFrame) -> None:
    """缺失点必须保留为 NaN，不能被静默填充（是否插值由建模层决定）。"""
    df = synthetic_long_df[
        ~((synthetic_long_df["date"] == date(2023, 1, 3)) & (synthetic_long_df["tenor"] == 10.0))
    ]
    panel = to_panel(df)
    assert np.isnan(panel.loc[pd.Timestamp("2023-01-03"), 10.0])
    assert not np.isnan(panel.loc[pd.Timestamp("2023-01-04"), 10.0])


# ---------------------------------------------------------------------------
# 模型
# ---------------------------------------------------------------------------
def test_curve_quote_rejects_negative_tenor() -> None:
    with pytest.raises(ValidationError):
        CurveQuote(date=date(2024, 1, 2), tenor=-1.0, ytm_pct=2.0)


def test_curve_quote_builds_long_frame() -> None:
    quotes = [
        CurveQuote(date=date(2024, 1, 2), tenor=t, ytm_pct=2.0 + 0.01 * t)
        for t in (1.0, 2.0, 3.0, 5.0)
    ]
    df = to_long_frame(quotes)
    assert len(df) == 4
    assert set(df["tenor"]) == {1.0, 2.0, 3.0, 5.0}


def test_bond_static_validates_dates() -> None:
    with pytest.raises(ValidationError, match="必须晚于"):
        BondStatic(
            code="X",
            issue_date=date(2030, 1, 1),
            maturity_date=date(2029, 1, 1),
            coupon_pct=2.0,
            freq=2,
        )


def test_bond_static_accepts_official_example(near_par_bond) -> None:
    """26 附息国债 10 的关键条款（财政部发行通知）。"""
    assert near_par_bond.coupon_pct == pytest.approx(1.72)
    assert near_par_bond.freq == 2
    assert near_par_bond.accrual == "coupon"
    assert (near_par_bond.maturity_date - near_par_bond.issue_date).days > 3650


def test_bond_valuation_enforces_full_price_identity() -> None:
    """全价 = 净价 + 应计利息，必须由模型强制。"""
    ok = BondValuation(
        date=date(2026, 5, 28),
        code="260010.IB",
        clean_price=99.999759,
        accrued_interest=0.060761,
        dirty_price=100.060520,
        ytm_pct=1.72,
    )
    assert ok.dirty_price == pytest.approx(ok.clean_price + ok.accrued_interest, abs=1e-6)

    with pytest.raises(ValidationError, match="全价不等于"):
        BondValuation(
            date=date(2026, 5, 28),
            code="260010.IB",
            clean_price=100.0,
            accrued_interest=0.060761,
            dirty_price=100.0,
            ytm_pct=1.72,
        )


# ---------------------------------------------------------------------------
# 真实数据冒烟（数据不存在时自动 skip）
# ---------------------------------------------------------------------------
def test_real_spike_data_satisfies_contract(real_spike_csv) -> None:
    """Phase 0 实测数据必须能通过契约校验（证明契约与真实数据一致）。

    注意：官方"年度文件"存在跨年重叠——实测 2024-12-27/30/31 三天同时出现在
    `2024.xlsx` 与 `2025.xlsx` 中，且**值完全相同**。因此清洗层必须去重，
    而契约层（本模块）应当拒绝重复，两者分工明确。
    """
    raw = pd.read_csv(real_spike_csv)
    df = raw.rename(columns={"term_years": "tenor", "yield_pct": "ytm_pct"})[
        ["date", "tenor", "ytm_pct"]
    ]
    df = df[df["tenor"].isin(CANONICAL_TENORS)]

    # 重叠部分的值必须完全一致，否则属于真正的数据冲突（需要人工核查）
    dup = df.duplicated(subset=["date", "tenor"], keep=False)
    if dup.any():
        spread = df[dup].groupby(["date", "tenor"])["ytm_pct"].nunique()
        assert (spread <= 1).all(), (
            f"年度文件重叠处存在不同数值，需人工核查:\n{spread[spread > 1].head()}"
        )

    df = df.drop_duplicates(subset=["date", "tenor"], keep="first")
    out = validate_long_frame(df)
    assert len(out) > 50_000
    assert out["date"].min() <= pd.Timestamp("2003-01-01")
