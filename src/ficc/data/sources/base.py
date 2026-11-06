"""数据源抽象层。

所有数据源适配器都继承 :class:`DataSource`，对外统一返回**长表**：

===============  ==========  ==================================================
列名             必需        含义
===============  ==========  ==================================================
``date``         ✓           交易日（datetime64[ns]，无时区）
``tenor``        ✓           期限（年，float；0.25 = 3M）
``ytm_pct``      ✓           到期收益率（**百分数**，2.35 表示 2.35%）
``term_label``   可选        原始期限标签（``"3m"`` / ``"10y"``）
``source``       可选        数据源标识（``"chinabond"`` / ``"akshare"``）
``curve_name``   可选        曲线名称（默认中债国债收益率曲线(到期)）
===============  ==========  ==================================================

设计约束
--------
1. 收益率一律百分数、期限一律年，与 :mod:`ficc.data.schema` 完全一致。
2. 数据源失败必须抛 :class:`SourceError`，消息里写清「哪个源 / 哪一年 / 哪一天 /
   哪个字段」。**禁止返回空 DataFrame 冒充成功**——AKShare 曾静默返回空表，
   导致下游把缺失当成"这段没数据"，这是本项目最危险的失败模式。
3. 适配器只负责「取数 + 解析 + 规范化」，不做网格裁剪与缺失填充；裁剪与显式
   缺失记录在 :mod:`ficc.data.clean`。
4. 适配器不做重试编排之外的落盘（原始快照由 :mod:`ficc.data.loader` 统一写）。
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from datetime import date
from typing import Any, ClassVar

import pandas as pd

from ficc.data.schema import DataValidationError, validate_long_frame
from ficc.logging import get_logger

log = get_logger(__name__)

LONG_REQUIRED_COLUMNS: tuple[str, ...] = ("date", "tenor", "ytm_pct")
LONG_OPTIONAL_COLUMNS: tuple[str, ...] = ("term_label", "source", "curve_name")
DEFAULT_CURVE_NAME = "中债国债收益率曲线(到期)"


class SourceError(RuntimeError):
    """数据源相关错误。

    用于网络失败、页面结构变化、附件缺失、返回空表、解析不出必需字段等。
    消息应当能直接定位问题，例如：
    ``"中债官网 2016 年 xlsx 解析失败：缺少列 ['收益率(%)']"``。
    """


class DataSource(ABC):
    """数据源抽象基类。

    Parameters
    ----------
    throttle_s : float
        两次网络请求之间的最小间隔（秒），用于限流、避免被上游封禁。
    """

    #: 数据源标识，会写进长表的 ``source`` 列
    name: ClassVar[str] = "base"

    def __init__(self, *, throttle_s: float = 0.0) -> None:
        self.throttle_s = max(0.0, float(throttle_s))
        self._last_request_at: float | None = None

    # ------------------------------------------------------------------
    # 子类必须实现
    # ------------------------------------------------------------------
    @abstractmethod
    def fetch(
        self,
        start: date | None = None,
        end: date | None = None,
        *,
        force: bool = False,
    ) -> pd.DataFrame:
        """抓取 ``[start, end]`` 区间的收益率长表。

        Parameters
        ----------
        start, end : date | None
            闭区间；``None`` 表示该数据源可提供的最早/最新。
        force : bool
            是否绕过本地缓存强制重新抓取。

        Returns
        -------
        pandas.DataFrame
            长表，至少包含 ``date`` / ``tenor`` / ``ytm_pct``。
        """

    def fetch_one(self, day: date) -> pd.DataFrame:
        """抓取单个交易日的数据（用于缺口回补）。

        默认实现直接报错：只有支持单日接口的数据源才覆写本方法。
        """
        raise NotImplementedError(f"{type(self).__name__} 不支持按单日抓取（day={day}）")

    # ------------------------------------------------------------------
    # 公共工具
    # ------------------------------------------------------------------
    def _throttle(self) -> None:
        """按 ``throttle_s`` 间隔休眠，保证请求限流。"""
        if self.throttle_s > 0 and self._last_request_at is not None:
            elapsed = time.monotonic() - self._last_request_at
            wait = self.throttle_s - elapsed
            if wait > 0:
                time.sleep(wait)
        self._last_request_at = time.monotonic()

    @staticmethod
    def normalize_long(
        df: Any,
        *,
        context: str,
        source: str,
        curve_name: str = DEFAULT_CURVE_NAME,
        max_bad_ratio: float = 0.01,
        min_good_rows: int = 1,
    ) -> pd.DataFrame:
        """把适配器解析结果规范化为长表并做契约校验。

        Parameters
        ----------
        df : Any
            解析后的 DataFrame，至少含 ``date`` / ``tenor`` / ``ytm_pct``。
        context : str
            错误上下文（例如 ``"中债官网 2016 年 xlsx"``），会拼进异常消息。
        source : str
            写入 ``source`` 列的数据源标识。
        curve_name : str
            写入 ``curve_name`` 列的曲线名称。
        max_bad_ratio : float
            允许丢弃的坏行比例上限；超过则抛 :class:`SourceError`
            （坏行包括日期/期限/收益率无法解析的记录）。
        min_good_rows : int
            有效行数下限，低于该值视为抓取失败。

        Raises
        ------
        SourceError
            返回 ``None``/空表、缺少必需列、坏行比例过高、或长表校验失败。
        """
        if df is None:
            raise SourceError(f"{context}: 数据源返回 None（禁止把失败当成功）")
        if not isinstance(df, pd.DataFrame):
            raise SourceError(f"{context}: 期望 pandas.DataFrame，实际 {type(df).__name__}")
        if df.empty:
            raise SourceError(f"{context}: 数据源返回空表（禁止静默降级）")

        missing = [c for c in LONG_REQUIRED_COLUMNS if c not in df.columns]
        if missing:
            raise SourceError(f"{context}: 缺少必需列 {missing}；实际列 {list(df.columns)}")

        out = df.copy()
        for col in LONG_OPTIONAL_COLUMNS:
            if col not in out.columns:
                out[col] = None
        out["source"] = source
        out["curve_name"] = curve_name
        if out["term_label"].isna().all():
            out["term_label"] = None

        # 0D 隔夜点不是曲线拟合网格点（schema 也禁止 tenor<=0），显式丢弃并记录。
        non_positive = out["tenor"] <= 0
        n_non_positive = int(non_positive.sum())
        if n_non_positive:
            log.debug("%s: 丢弃 %d 行 tenor<=0（隔夜点）", context, n_non_positive)
            out = out.loc[~non_positive].copy()

        out["date"] = pd.to_datetime(out["date"], errors="coerce")
        out["tenor"] = pd.to_numeric(out["tenor"], errors="coerce").astype("float64")
        out["ytm_pct"] = pd.to_numeric(out["ytm_pct"], errors="coerce").astype("float64")
        bad = out["date"].isna() | out["tenor"].isna() | out["ytm_pct"].isna()
        n_bad = int(bad.sum())
        n_total = len(out) + n_non_positive
        if n_bad:
            allowed = max(3, int(max_bad_ratio * max(n_total, 1)))
            sample = out.loc[bad].head(3).to_dict("records")
            message = (
                f"{context}: {n_bad}/{n_total} 行日期/期限/收益率无法解析"
                f"（允许上限 {allowed} 行），样例 {sample}"
            )
            if n_bad > allowed:
                raise SourceError(message)
            log.warning(message)
            out = out.loc[~bad].copy()

        if len(out) < min_good_rows:
            raise SourceError(f"{context}: 有效行数 {len(out)} < 下限 {min_good_rows}")

        # 同一年度文件内不应出现重复 (date, tenor)；若出现，保留最后一条并记录。
        dup = out.duplicated(subset=["date", "tenor"], keep=False)
        if dup.any():
            conflicting = out.loc[dup].groupby(["date", "tenor"])["ytm_pct"].nunique()
            n_conflict = int((conflicting > 1).sum())
            log.warning(
                "%s: 发现 %d 行重复 (date, tenor)，其中 %d 组取值不一致；保留最后一条",
                context,
                int(dup.sum()),
                n_conflict,
            )
            out = out.drop_duplicates(subset=["date", "tenor"], keep="last")

        try:
            return validate_long_frame(out)
        except DataValidationError as exc:
            raise SourceError(f"{context}: 长表契约校验失败：{exc}") from exc
