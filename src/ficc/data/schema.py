"""数据契约（schema）。

这一层是**跨模块的唯一约定**：数据源的输出、清洗后的中间表、建模输入都必须
满足这里的定义。任何字段改名/单位变化，都应该先改本文件、再改实现。

关键口径（来自 docs/00_market_conventions.md）
---------------------------------------------
- 收益率一律用**百分数**（`2.35` 表示 2.35%），不是小数。
- 期限一律用**年**（浮点，`0.25` = 3M）。
- 价格口径：`clean_price` 净价、`dirty_price` 全价、`accrued_interest` 应计利息，
  三者关系 `dirty = clean + accrued`，单位均为「元/百元面值」。
- 不带口径的裸 `price` 字段**不允许**出现，防止误用。
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

import numpy as np
import pandas as pd
from pydantic import BaseModel, ConfigDict, Field, model_validator

# ---------------------------------------------------------------------------
# 常量：统一期限网格（实测可得，见 docs/00_data_feasibility.md）
# ---------------------------------------------------------------------------
CANONICAL_TENORS: tuple[float, ...] = (0.25, 0.5, 1.0, 2.0, 3.0, 5.0, 7.0, 10.0, 15.0, 20.0, 30.0)
LABEL_TO_TENOR: dict[str, float] = {
    "0d": 0.0,
    "1m": 1 / 12,
    "2m": 2 / 12,
    "3m": 0.25,
    "6m": 0.5,
    "9m": 0.75,
    "1y": 1.0,
    "2y": 2.0,
    "3y": 3.0,
    "4y": 4.0,
    "5y": 5.0,
    "6y": 6.0,
    "7y": 7.0,
    "8y": 8.0,
    "9y": 9.0,
    "10y": 10.0,
    "15y": 15.0,
    "20y": 20.0,
    "30y": 30.0,
    "40y": 40.0,
    "50y": 50.0,
}
# 标准期限标签（用于图表与日志，可读性优先）
TENOR_LABELS: dict[float, str] = {
    0.0: "0D",
    1 / 12: "1M",
    2 / 12: "2M",
    0.25: "3M",
    0.5: "6M",
    0.75: "9M",
    1.0: "1Y",
    2.0: "2Y",
    3.0: "3Y",
    4.0: "4Y",
    5.0: "5Y",
    6.0: "6Y",
    7.0: "7Y",
    8.0: "8Y",
    9.0: "9Y",
    10.0: "10Y",
    15.0: "15Y",
    20.0: "20Y",
    30.0: "30Y",
    40.0: "40Y",
    50.0: "50Y",
}

# 长表列名（canonical long format）
LONG_COLUMNS: tuple[str, ...] = ("date", "tenor", "ytm_pct")
LONG_OPTIONAL_COLUMNS: tuple[str, ...] = ("curve_name", "source", "term_label")


class DataValidationError(ValueError):
    """数据不满足契约时抛出。消息里必须写明"哪一列/哪一行/违反了什么"。"""


# ---------------------------------------------------------------------------
# 单记录模型
# ---------------------------------------------------------------------------
class CurveQuote(BaseModel):
    """单个期限点、单个交易日的收益率观测。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    date: date
    tenor: float = Field(gt=0, le=50, description="期限（年）")
    ytm_pct: float = Field(ge=-5, le=30, description="到期收益率，百分数")
    curve_name: str = "中债国债收益率曲线(到期)"
    source: str = "chinabond"
    term_label: str | None = None


class BondStatic(BaseModel):
    """债券静态条款。合成债券与真实债券共用此结构。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    code: str
    issue_date: date
    maturity_date: date
    coupon_pct: float = Field(ge=0, le=30, description="票面利率，百分数")
    freq: Literal[1, 2] = Field(description="付息频率：1=年付，2=半年付")
    face_value: float = 100.0
    accrual: Literal["coupon", "lump_sum", "discount"] = "coupon"
    market: Literal["IB", "EX"] = "IB"

    @model_validator(mode="after")
    def _check_dates(self) -> BondStatic:
        if self.maturity_date <= self.issue_date:
            raise ValueError(
                f"{self.code}: maturity_date({self.maturity_date}) 必须晚于 issue_date({self.issue_date})"
            )
        return self

    @property
    def is_synthetic(self) -> bool:
        return self.code.startswith("SYN-")


class BondValuation(BaseModel):
    """债券估值。注意：`dirty = clean + accrued` 由校验强制。"""

    model_config = ConfigDict(frozen=True, extra="forbid")

    date: date
    code: str
    clean_price: float
    dirty_price: float
    accrued_interest: float
    ytm_pct: float
    mod_duration: float | None = None
    dv01: float | None = Field(default=None, description="元/100 面值/bp")

    @model_validator(mode="after")
    def _check_full_price(self) -> BondValuation:
        diff = abs((self.clean_price + self.accrued_interest) - self.dirty_price)
        if diff > 1e-6:
            raise ValueError(
                f"{self.code}@{self.date}: 全价不等于净价+应计利息 "
                f"(clean={self.clean_price}, ai={self.accrued_interest}, dirty={self.dirty_price}, diff={diff:.2e})"
            )
        return self


# ---------------------------------------------------------------------------
# DataFrame 级校验与转换
# ---------------------------------------------------------------------------
def validate_long_frame(df: pd.DataFrame, *, require_full_grid: bool = False) -> pd.DataFrame:
    """校验长表（date, tenor, ytm_pct）并返回规范化副本。

    Parameters
    ----------
    df : DataFrame
        至少包含 `date` / `tenor` / `ytm_pct` 三列。
    require_full_grid : bool
        是否要求每个日期都包含 CANONICAL_TENORS 的全部期限。

    Returns
    -------
    DataFrame
        规范化后（dtype 正确、按 date, tenor 排序、重置索引）的副本。

    Raises
    ------
    DataValidationError
        列缺失、dtype 错误、重复 (date, tenor)、NaN 收益率、越界值等。
    """
    if not isinstance(df, pd.DataFrame):
        raise DataValidationError(f"期望 pandas.DataFrame，实际 {type(df).__name__}")
    missing = [c for c in LONG_COLUMNS if c not in df.columns]
    if missing:
        raise DataValidationError(f"长表缺少必需列: {missing}；实际列: {list(df.columns)}")

    out = df.copy()
    out["date"] = pd.to_datetime(out["date"], errors="coerce")
    if out["date"].isna().any():
        bad = out.index[out["date"].isna()].tolist()[:5]
        raise DataValidationError(f"date 列存在无法解析的值，行索引示例: {bad}")

    for col in ("tenor", "ytm_pct"):
        if not pd.api.types.is_numeric_dtype(out[col]):
            try:
                out[col] = pd.to_numeric(out[col], errors="raise")
            except (ValueError, TypeError) as exc:
                raise DataValidationError(f"{col} 列无法转为数值: {exc}") from exc

    if out["tenor"].isna().any():
        raise DataValidationError("tenor 列存在 NaN")
    if out["ytm_pct"].isna().any():
        n = int(out["ytm_pct"].isna().sum())
        raise DataValidationError(
            f"ytm_pct 列存在 {n} 个 NaN；缺失应在清洗阶段显式处理，不能静默带入模型"
        )

    if (out["tenor"] <= 0).any():
        raise DataValidationError("tenor 必须为正数")
    if (out["tenor"] > 50).any():
        raise DataValidationError("tenor 超过 50 年，超出本项目范围")
    if ((out["ytm_pct"] < -5) | (out["ytm_pct"] > 30)).any():
        bad = out.loc[(out["ytm_pct"] < -5) | (out["ytm_pct"] > 30), ["date", "tenor", "ytm_pct"]]
        raise DataValidationError(f"ytm_pct 越界（应为百分数且 -5~30）：\n{bad.head()}")

    dup = out.duplicated(subset=["date", "tenor"], keep=False)
    if dup.any():
        sample = out.loc[dup, ["date", "tenor", "ytm_pct"]].head()
        raise DataValidationError(f"(date, tenor) 存在重复记录 {int(dup.sum())} 行：\n{sample}")

    if require_full_grid:
        counts = out.groupby("date")["tenor"].nunique()
        bad_dates = counts[counts < len(CANONICAL_TENORS)]
        if len(bad_dates) > 0:
            raise DataValidationError(
                f"{len(bad_dates)} 个日期的期限点不足 {len(CANONICAL_TENORS)} 个，"
                f"示例: {bad_dates.head(3).to_dict()}"
            )

    out = out.sort_values(["date", "tenor"]).reset_index(drop=True)
    return out


def to_long_frame(quotes: Iterable[CurveQuote]) -> pd.DataFrame:
    """把 CurveQuote 序列转成长表（不经过 JSON 序列化，保持性能）。"""
    rows: list[dict[str, Any]] = [
        {
            "date": q.date,
            "tenor": q.tenor,
            "ytm_pct": q.ytm_pct,
            "curve_name": q.curve_name,
            "source": q.source,
            "term_label": q.term_label,
        }
        for q in quotes
    ]
    df = pd.DataFrame(
        rows, columns=["date", "tenor", "ytm_pct", "curve_name", "source", "term_label"]
    )
    return validate_long_frame(df)


def to_panel(long_df: pd.DataFrame, tenors: Iterable[float] | None = None) -> pd.DataFrame:
    """长表 → 宽表面板：index=date，columns=期限（年），values=收益率(%)。

    缺失的 (date, tenor) 组合保留为 NaN（不填充——是否插值由建模模块显式决定）。
    """
    valid = validate_long_frame(long_df)
    cols = list(tenors) if tenors is not None else list(CANONICAL_TENORS)
    panel = valid.pivot(index="date", columns="tenor", values="ytm_pct")
    # 只保留请求的期限，并按给定顺序排列（缺失列补 NaN）
    panel = panel.reindex(columns=cols)
    panel.columns = pd.Index(np.asarray(cols, dtype=float), name="tenor")
    panel = panel.sort_index()
    return panel


def panel_to_long(panel: pd.DataFrame) -> pd.DataFrame:
    """宽表面板 → 长表（丢弃 NaN）。"""
    # pandas>=2.1：用 future_stack 避免 FutureWarning；先 stack 再 dropna
    stacked = panel.stack(future_stack=True).rename("ytm_pct").reset_index()
    stacked = stacked.dropna(subset=["ytm_pct"])
    stacked.columns = ["date", "tenor", "ytm_pct"]
    return validate_long_frame(stacked)


@dataclass(frozen=True)
class DataQualityIssue:
    """一条数据质量问题记录。"""

    date: pd.Timestamp
    tenor: float
    kind: Literal["missing", "jump", "spike", "out_of_range"]
    detail: str
