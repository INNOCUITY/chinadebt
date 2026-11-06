"""中债估值中心（ChinaBond）官方数据源。

数据来源（2026-11-06 实测可用，详见 ``docs/00_data_feasibility.md``）
-------------------------------------------------------------------
1. **往年存档**：列表页
   ``https://www.chinabond.com.cn/zzsj/.../qxxz_zzgzqx/index[_1|_2].html``
   → 文章页 → 附件 ``./P0xxxx.xlsx``（每年一个文件，2002–2025）。
2. **当年数据**：
   ``https://yield.chinabond.com.cn/cbweb-mn/yc/downYearBzqx?year=YYYY&zblx=txy&ycDefId=...``
3. **单日回补**：
   ``https://yield.chinabond.com.cn/cbweb-mn/yc/downBzqxDetail?workTime=YYYY-MM-DD&ycDefId=...&zblx=txy``

官方 xlsx 字段（不同年份表头略有差异，代码做别名容错）：
``日期 / 标准期限说明 / 标准期限(年) / 收益率(%)``；单日文件为
``日期 / 曲线名称 / 标准期限(年) / 收益率(%)``。

实测坑位
--------
* 中债静态页响应头不带 charset，``requests`` 会误判成 ISO-8859-1 导致中文乱码，
  因此文本抓取统一强制 ``r.encoding = "utf-8"``。
* 年度文件存在**跨年重叠**（如 2024/2025 文件都含 2024-12-27~31，且数值一致），
  跨文件去重与冲突校验在 :mod:`ficc.data.clean` 中完成。
* 2016-01-04 → 2019-01-25 期间官方年度文件缺 2Y 期限点，需用单日接口逐日回补
  （见 :func:`ficc.data.loader.backfill_2y`）。
"""

from __future__ import annotations

import io
import re
import time
import warnings
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path
from typing import Any, ClassVar
from urllib.parse import urljoin

import numpy as np
import pandas as pd
import requests

from ficc.config import Config, get_config
from ficc.data.schema import LABEL_TO_TENOR, TENOR_LABELS
from ficc.data.sources.base import DataSource, SourceError
from ficc.logging import get_logger

log = get_logger(__name__)

# ---------------------------------------------------------------------------
# 常量：官网地址与曲线标识（来自 docs/00_data_feasibility.md 的实测）
# ---------------------------------------------------------------------------
ARCHIVE_URL = "https://www.chinabond.com.cn/zzsj/zzsj_zzjgcp/zzjgcp_cpxz/cpxz_qxxz/qxxz_zzgzqx/"
YIELD_BASE = "https://yield.chinabond.com.cn/cbweb-mn"
DEFAULT_YC_DEF_ID = "2c9081e50a2f9606010a3068cae70001"  # 中债国债收益率曲线(到期)
DEFAULT_CURVE_NAME = "中债国债收益率曲线(到期)"
ARCHIVE_PAGES: tuple[str, ...] = ("index", "index_1", "index_2")
MIN_XLSX_BYTES = 1_000
DATE_MIN_DEFAULT = date(2002, 1, 4)  # 官方数据最早一天

#: 官方文件表头别名（归一化后匹配，见 :func:`_norm_header`）
_COLUMN_ALIASES: dict[str, str] = {
    # 日期
    "日期": "date",
    "date": "date",
    "交易日期": "date",
    "估值日期": "date",
    "worktime": "date",
    # 期限标签
    "标准期限说明": "term_label",
    "标准期限": "term_label",
    "期限说明": "term_label",
    "term_label": "term_label",
    "label": "term_label",
    # 期限（年）
    "标准期限(年)": "term_years",
    "标准期限年": "term_years",
    "期限(年)": "term_years",
    "term_years": "term_years",
    "term": "term_years",
    # 收益率
    "收益率(%)": "ytm_pct",
    "收益率": "ytm_pct",
    "收益率%": "ytm_pct",
    "到期收益率(%)": "ytm_pct",
    "yield_pct": "ytm_pct",
    "yield(%)": "ytm_pct",
    # 曲线名称
    "曲线名称": "curve_name",
    "curve_name": "curve_name",
}

#: 由「年」反推标准期限标签时的容差（官方把 1M 写成 0.08、2M 写成 0.17）
_TENOR_MATCH_TOL = 0.01


@contextmanager
def _quiet_excel_warnings():
    """压掉 openpyxl 对官方文件「no default style」的 UserWarning（纯噪音）。"""
    with warnings.catch_warnings():
        warnings.filterwarnings("ignore", message=".*no default style.*")
        warnings.filterwarnings("ignore", message=".*Data Validation extension.*")
        yield


def _norm_header(value: Any) -> str:
    """归一化表头：去 BOM/空白、全角括号转半角、转小写（ASCII）。

    例：``"标准期限（年） "`` → ``"标准期限(年)"``；``" 收益率(%)"`` → ``"收益率(%)"``。
    """
    text = str(value).replace("\ufeff", "").strip()
    text = re.sub(r"[\s\u3000]+", "", text)
    text = text.replace("（", "(").replace("）", ")").replace("：", ":")
    return text.lower() if text.isascii() else text


def _map_columns(columns: Any) -> dict[Any, str]:
    """把原始列名映射到规范列名；忽略 "Unnamed: n" 之类的空列。"""
    mapping: dict[Any, str] = {}
    used: set[str] = set()
    for col in columns:
        key = _norm_header(col)
        canonical = _COLUMN_ALIASES.get(key)
        if canonical is None or canonical in used:
            continue
        mapping[col] = canonical
        used.add(canonical)
    return mapping


def _to_numeric(series: pd.Series) -> pd.Series:
    """稳健转数值：兼容 ``"2.35%"``、``"2,351"``、全角空格等。"""
    if pd.api.types.is_numeric_dtype(series):
        return pd.to_numeric(series, errors="coerce")
    text = (
        series.astype("string")
        .str.replace("\ufeff", "", regex=False)
        .str.replace("\u3000", "", regex=False)
        .str.replace(",", "", regex=False)
        .str.replace("%", "", regex=False)
        .str.replace("％", "", regex=False)
        .str.strip()
    )
    return pd.to_numeric(text, errors="coerce")


def _tenor_from_years(years: float) -> float | None:
    """把「年」数值映射到标准期限；官方把 1M/2M 写成 0.08/0.17，需容差匹配。"""
    if pd.isna(years):
        return None
    value = float(years)
    if value <= 0:
        return None
    for label, tenor in LABEL_TO_TENOR.items():
        if label == "0d":
            continue
        if abs(value - tenor) <= _TENOR_MATCH_TOL:
            return tenor
    return round(value, 6)


def _map_years_to_tenors(series: pd.Series) -> pd.Series:
    """把「年」数值序列映射到标准期限（1M=0.08 → 1/12 等），保留非网格原值。"""
    values = _to_numeric(series).to_numpy(dtype=float)
    mapped = np.full(values.shape, np.nan, dtype=float)
    for label, tenor in LABEL_TO_TENOR.items():
        if label == "0d":
            continue
        hit = np.isclose(values, tenor, rtol=0.0, atol=_TENOR_MATCH_TOL) & np.isnan(mapped)
        mapped[hit] = float(tenor)
    remaining = np.isnan(mapped) & ~np.isnan(values)
    mapped[remaining] = np.round(values[remaining], 6)
    return pd.Series(mapped, index=series.index, dtype="float64")


def _label_from_tenor(tenor: float) -> str | None:
    """标准期限 → 官方标签（``0.25`` → ``"3m"``）；非网格点返回 ``None``。"""
    for label, value in LABEL_TO_TENOR.items():
        if abs(float(tenor) - value) <= 1e-9:
            return label
    return None


def _find_header_row(raw: pd.DataFrame, *, require_date: bool = True) -> int | None:
    """在前 10 行里寻找表头行。

    Parameters
    ----------
    require_date : bool
        年度文件要求有「日期」列；单日文件 ``downBzqxDetail`` 的实际 xlsx
        **没有日期列**（日期在文件名里），此时只需 收益率 + 期限 两列。
    """
    for idx in range(min(10, len(raw))):
        mapped = set(_map_columns(raw.iloc[idx].tolist()).values())
        has_term = "term_years" in mapped or "term_label" in mapped
        if not has_term or "ytm_pct" not in mapped:
            continue
        if require_date and "date" not in mapped:
            continue
        return idx
    return None


def _sheet_to_raw(
    xl: pd.ExcelFile, sheet: str, *, require_date: bool = True
) -> pd.DataFrame | None:
    """读取单个 sheet 并定位表头；失败返回 ``None``（由调用方汇总报错）。"""
    try:
        raw = xl.parse(sheet, header=None, dtype=object)
    except Exception as exc:
        log.debug("读取 sheet %s 失败：%r", sheet, exc)
        return None
    if raw.empty:
        return None
    header_row = _find_header_row(raw, require_date=require_date)
    if header_row is None:
        return None
    header = raw.iloc[header_row].tolist()
    body = raw.iloc[header_row + 1 :].reset_index(drop=True)
    # 按「列位置」建立映射：表头单元格 → 规范列名
    mapping: dict[int, str] = {}
    used: set[str] = set()
    for pos, cell in enumerate(header):
        canonical = _COLUMN_ALIASES.get(_norm_header(cell))
        if canonical is None or canonical in used or pos >= body.shape[1]:
            continue
        mapping[pos] = canonical
        used.add(canonical)
    if not mapping:
        return None
    positions = list(mapping)
    out = body.iloc[:, positions].copy()
    out.columns = [mapping[pos] for pos in positions]
    # 去掉全空行（官方文件尾部偶有空白行/注释行）
    key_cols = [c for c in ("date", "term_years", "term_label", "ytm_pct") if c in out.columns]
    return out.dropna(how="all", subset=key_cols).reset_index(drop=True)


def _normalize_parsed(
    df: pd.DataFrame,
    *,
    context: str,
    curve_name: str,
    max_invalid_ratio: float = 0.01,
) -> pd.DataFrame:
    """把「原始解析结果」规范化为长表（date/tenor/ytm_pct + 可选列）。

    期限解析优先级：``标准期限说明``（标签，最可靠）→ ``标准期限(年)`` 数值。
    无效行（日期/期限/收益率解析失败）先计数；比例过高则抛 :class:`SourceError`。
    """
    if df.empty:
        raise SourceError(f"{context}: 解析后为空表")

    out = df.copy()
    out["date"] = pd.to_datetime(out.get("date"), errors="coerce", format="mixed")

    label = out.get("term_label")
    if label is None:
        label_series = pd.Series(pd.NA, index=out.index, dtype="string")
    else:
        label_series = label.astype("string").str.strip().str.lower()
    tenor_from_label = label_series.map(LABEL_TO_TENOR)
    if "term_years" in out.columns:
        tenor_from_years = _map_years_to_tenors(out["term_years"])
    else:
        tenor_from_years = pd.Series(float("nan"), index=out.index, dtype="float64")
    out["tenor"] = tenor_from_label.where(tenor_from_label.notna(), tenor_from_years)
    out["ytm_pct"] = _to_numeric(out["ytm_pct"]) if "ytm_pct" in out.columns else float("nan")

    bad = out["date"].isna() | out["tenor"].isna() | out["ytm_pct"].isna()
    n_bad = int(bad.sum())
    n_total = len(out)
    if n_bad:
        allowed = max(3, int(max_invalid_ratio * max(n_total, 1)))
        sample = out.loc[bad].head(5).to_dict("records")
        message = (
            f"{context}: {n_bad}/{n_total} 行日期/期限/收益率无法解析"
            f"（允许上限 {allowed} 行）；样例 {sample}"
        )
        if n_bad > allowed:
            raise SourceError(message)
        log.warning(message)
        out = out.loc[~bad].copy()
    if out.empty:
        raise SourceError(f"{context}: 无有效记录（全部解析失败）")

    out["term_label"] = [
        _label_from_tenor(t) if lbl is None or pd.isna(lbl) else str(lbl).strip().lower()
        for t, lbl in zip(out["tenor"], label_series.loc[out.index], strict=False)
    ]
    out["date"] = pd.to_datetime(out["date"], errors="raise")
    out["tenor"] = pd.to_numeric(out["tenor"], errors="raise").astype("float64")
    out["ytm_pct"] = pd.to_numeric(out["ytm_pct"], errors="raise").astype("float64")
    out["source"] = "chinabond"
    out["curve_name"] = curve_name
    return out.loc[:, ["date", "tenor", "ytm_pct", "term_label", "source", "curve_name"]]


def parse_official_xlsx(
    source: bytes | str | Path | io.BytesIO,
    *,
    year: int | None = None,
    curve_name: str = DEFAULT_CURVE_NAME,
    max_invalid_ratio: float = 0.01,
) -> pd.DataFrame:
    """解析中债官方「标准期限」年度 xlsx。

    Parameters
    ----------
    source : bytes | str | Path | BytesIO
        文件路径或文件内容。旧文件可能是 ``.xls``，pandas 会自动选择引擎。
    year : int | None
        仅用于错误信息（说明是哪一年的文件失败）。
    curve_name : str
        写入 ``curve_name`` 列的名称。
    max_invalid_ratio : float
        允许的坏行比例；超过则抛错（避免把"表头变了"当成正常空数据）。

    Returns
    -------
    pandas.DataFrame
        原始长表：``date / tenor / ytm_pct / term_label / source / curve_name``。
        **保留全部官方期限点（含 0D 隔夜点）**；0D 与非网格点由
        :func:`ficc.data.clean.clean_long_frame` 按建模网格显式丢弃并计数。

    Raises
    ------
    SourceError
        文件无法打开、找不到含必需列的工作表、或坏行比例过高。
    """
    tag = f"中债官方 {year} 年 xlsx" if year is not None else "中债官方 xlsx"
    try:
        with _quiet_excel_warnings():
            if isinstance(source, bytes):
                xl = pd.ExcelFile(io.BytesIO(source))
            elif isinstance(source, io.BytesIO):
                xl = pd.ExcelFile(source)
            else:
                xl = pd.ExcelFile(str(source))
    except Exception as exc:
        raise SourceError(f"{tag}: 无法打开文件（{type(exc).__name__}: {exc}）") from exc

    frames: list[pd.DataFrame] = []
    tried: list[str] = []
    for sheet in xl.sheet_names:
        df = _sheet_to_raw(xl, sheet)
        if df is None:
            tried.append(f"{sheet}(未找到表头)")
            continue
        mapped = set(df.columns)
        if "date" not in mapped or "ytm_pct" not in mapped:
            tried.append(f"{sheet}(缺日期/收益率列)")
            continue
        if "term_years" not in mapped and "term_label" not in mapped:
            tried.append(f"{sheet}(缺期限列)")
            continue
        df = df.copy()
        if "curve_name" not in df.columns:
            df["curve_name"] = curve_name
        frames.append(df)
        tried.append(f"{sheet}({len(df)} 行)")

    if not frames:
        raise SourceError(f"{tag}: 没有任何工作表包含 日期/标准期限/收益率 列；工作表情况 {tried}")

    combined = pd.concat(frames, ignore_index=True)
    normalized = _normalize_parsed(
        combined, context=tag, curve_name=curve_name, max_invalid_ratio=max_invalid_ratio
    )
    log.debug("%s: 工作表 %s，解析 %d 行", tag, tried, len(normalized))
    return normalized


def parse_spike_csv(
    path: str | Path,
    *,
    year: int | None = None,
    curve_name: str = DEFAULT_CURVE_NAME,
) -> pd.DataFrame:
    """解析 Phase 0 合并长表 ``cb_gzqx_standard_terms_2002_2026.csv``。

    该 CSV 是离线回退输入（字段：``date/term_label/term_years/yield_pct``），
    仅在官方 xlsx 缺失时使用。
    """
    tag = f"Phase 0 合并长表 {year} 年" if year is not None else "Phase 0 合并长表"
    try:
        raw = pd.read_csv(path)
    except Exception as exc:
        raise SourceError(f"{tag}: 无法读取 CSV {path}（{exc}）") from exc
    raw = raw.rename(columns={"yield_pct": "ytm_pct"})
    missing = [c for c in ("date", "term_years", "ytm_pct") if c not in raw.columns]
    if "term_label" in raw.columns:
        missing = [c for c in missing if c != "term_years"]
    if missing:
        raise SourceError(f"{tag}: 缺少列 {missing}；实际列 {list(raw.columns)}")
    return _normalize_parsed(raw, context=tag, curve_name=curve_name)


def parse_daily_xlsx(
    source: bytes | str | Path | io.BytesIO,
    *,
    day: date | None = None,
    curve_name: str = DEFAULT_CURVE_NAME,
    require_curve_name: bool = True,
) -> pd.DataFrame:
    """解析 ``downBzqxDetail`` 单日 xlsx。

    单日文件列：``日期 / 曲线名称 / 标准期限(年) / 收益率(%)``（没有期限文字标签，
    因此靠数值反推标准期限）。若返回了多条曲线，只保留国债曲线。
    """
    tag = f"中债单日文件 {day.isoformat() if day else ''}".strip()
    try:
        with _quiet_excel_warnings():
            if isinstance(source, bytes):
                xl = pd.ExcelFile(io.BytesIO(source))
            elif isinstance(source, io.BytesIO):
                xl = pd.ExcelFile(source)
            else:
                xl = pd.ExcelFile(str(source))
    except Exception as exc:
        raise SourceError(f"{tag}: 无法打开文件（{type(exc).__name__}: {exc}）") from exc

    frames: list[pd.DataFrame] = []
    for sheet in xl.sheet_names:
        # 单日文件实际没有「日期」列，日期由请求参数给出。
        df = _sheet_to_raw(xl, sheet, require_date=False)
        if df is None:
            continue
        if "ytm_pct" not in set(df.columns):
            continue
        if "term_years" not in df.columns and "term_label" not in df.columns:
            continue
        frames.append(df)
    if not frames:
        raise SourceError(f"{tag}: 未找到包含 标准期限/收益率 的工作表")

    raw = pd.concat(frames, ignore_index=True)
    if "date" not in raw.columns:
        if day is None:
            raise SourceError(f"{tag}: 文件没有日期列，且未提供 day 参数")
        raw["date"] = pd.Timestamp(day)
    if require_curve_name and "curve_name" in raw.columns:
        names = raw["curve_name"].astype("string").str.strip()
        mask = names.str.contains("国债", na=False)
        if not mask.any():
            actual = sorted(names.dropna().unique().tolist())[:5]
            raise SourceError(f"{tag}: 返回曲线中未找到国债曲线；实际曲线 {actual}")
        raw = raw.loc[mask].copy()
    else:
        raw["curve_name"] = curve_name
    normalized = _normalize_parsed(raw, context=tag, curve_name=curve_name)
    if day is not None:
        parsed_days = normalized["date"].dt.date.unique()
        if not (len(parsed_days) == 1 and parsed_days[0] == day):
            log.warning("%s: 请求日期 %s，文件实际包含日期 %s", tag, day, parsed_days[:5])
    return normalized


class _HttpClient:
    """带重试/退避/限流的 HTTP 客户端。

    单独抽出来便于测试注入 fake ``requests.Session``，不触碰真实网络。
    """

    def __init__(
        self,
        *,
        timeout_s: float = 40.0,
        retries: int = 3,
        backoff_s: float = 1.5,
        throttle_s: float = 0.0,
        user_agent: str = "Mozilla/5.0",
        session: Any | None = None,
    ) -> None:
        self.timeout_s = float(timeout_s)
        self.retries = max(1, int(retries))
        self.backoff_s = max(0.0, float(backoff_s))
        self.throttle_s = max(0.0, float(throttle_s))
        self._last_request_at: float | None = None
        if session is None:
            self.session = requests.Session()
            self.session.headers.update({"User-Agent": user_agent})
        else:
            self.session = session
            if hasattr(self.session, "headers"):
                self.session.headers.setdefault("User-Agent", user_agent)

    def _throttle(self) -> None:
        if self.throttle_s > 0 and self._last_request_at is not None:
            elapsed = time.monotonic() - self._last_request_at
            wait = self.throttle_s - elapsed
            if wait > 0:
                time.sleep(wait)
        self._last_request_at = time.monotonic()

    def get(
        self,
        url: str,
        *,
        context: str,
        referer: str | None = None,
        min_bytes: int = 0,
        expect: str = "either",
    ) -> requests.Response:
        """GET 一个 URL，失败时按配置重试/退避，最终抛 :class:`SourceError`。

        Parameters
        ----------
        context : str
            错误上下文（写清哪一年/哪一天/哪个接口）。
        min_bytes : int
            响应体最小字节数，防止把错误页当数据。
        expect : {"either", "text", "bytes"}
            只影响错误信息措辞，不做内容判断。
        """
        headers = {"Referer": referer} if referer else {}
        last_error: str = "未知错误"
        for attempt in range(1, self.retries + 1):
            self._throttle()
            try:
                response = self.session.get(url, timeout=self.timeout_s, headers=headers)
            except requests.RequestException as exc:
                last_error = f"{type(exc).__name__}: {exc}"
                if attempt < self.retries:
                    time.sleep(self.backoff_s * (2 ** (attempt - 1)))
                continue
            if response.status_code != 200:
                last_error = f"HTTP {response.status_code}"
                # 4xx 是确定性失败，重试无意义
                if response.status_code < 500 or attempt >= self.retries:
                    break
                time.sleep(self.backoff_s * (2 ** (attempt - 1)))
                continue
            content = response.content
            if min_bytes and len(content) < min_bytes:
                last_error = f"响应体仅 {len(content)} 字节（< {min_bytes}）"
                if attempt < self.retries:
                    time.sleep(self.backoff_s * (2 ** (attempt - 1)))
                continue
            return response
        raise SourceError(
            f"{context}: 请求失败（{self.retries} 次尝试后）url={url}；原因={last_error}"
        )

    def get_text(self, url: str, *, context: str, referer: str | None = None) -> str:
        """抓取 HTML 文本；强制 UTF-8 以规避中债静态页编码误判。"""
        response = self.get(url, context=context, referer=referer, min_bytes=50)
        encoding = (response.encoding or "").lower()
        if not encoding or encoding == "iso-8859-1":
            response.encoding = "utf-8"
        return response.text

    def get_bytes(
        self, url: str, *, context: str, referer: str | None = None, min_bytes: int = MIN_XLSX_BYTES
    ) -> bytes:
        response = self.get(
            url, context=context, referer=referer, min_bytes=min_bytes, expect="bytes"
        )
        return response.content


class ChinabondSource(DataSource):
    """中债估值中心官方数据源。

    Parameters
    ----------
    cfg : Config | None
        项目配置；``None`` 时读取 ``config/config.yaml``。
    cache_dir : str | Path | None
        原始 xlsx 快照目录（**读**路径）。存在缓存时优先读缓存（除非 ``force=True``
        或当年刷新）。
    staging_dir : str | Path | None
        下载内容先写入的临时目录（**写**路径）。由 loader 在整批校验通过后提交，
        保证失败时正式快照不被半途替换（R04）；``None`` 表示直接原子写快照。
    session : Any | None
        注入的 requests.Session（测试用 fake session 可完全离线）。
    yc_def_id : str | None
        曲线 ID，默认取配置里的「中债国债收益率曲线(到期)」。
    """

    name: ClassVar[str] = "chinabond"

    def __init__(
        self,
        *,
        cfg: Config | None = None,
        cache_dir: str | Path | None = None,
        staging_dir: str | Path | None = None,
        throttle_s: float | None = None,
        timeout_s: float | None = None,
        retries: int | None = None,
        backoff_s: float | None = None,
        user_agent: str | None = None,
        session: Any | None = None,
        yc_def_id: str | None = None,
        curve_name: str = DEFAULT_CURVE_NAME,
    ) -> None:
        self.cfg = cfg or get_config()
        http_cfg = self.cfg.data.http
        super().__init__(
            throttle_s=http_cfg.throttle_s if throttle_s is None else float(throttle_s)
        )
        self.http = _HttpClient(
            timeout_s=http_cfg.timeout_s if timeout_s is None else timeout_s,
            retries=http_cfg.retries if retries is None else retries,
            backoff_s=http_cfg.backoff_s if backoff_s is None else backoff_s,
            throttle_s=self.throttle_s,
            user_agent=http_cfg.user_agent if user_agent is None else user_agent,
            session=session,
        )
        self.cache_dir = Path(cache_dir) if cache_dir is not None else None
        self.staging_dir = Path(staging_dir) if staging_dir is not None else None
        self.yc_def_id = yc_def_id or self.cfg.data.yc_def_id or DEFAULT_YC_DEF_ID
        self.curve_name = curve_name
        self._archive_years: dict[int, str] | None = None
        self._warmed = False

    # ------------------------------------------------------------------
    # 列表页 / 附件
    # ------------------------------------------------------------------
    def list_archive_years(self, *, force: bool = False) -> dict[int, str]:
        """抓取往年存档列表，返回 ``{year: 文章页绝对 URL}``（升序）。"""
        if self._archive_years is not None and not force:
            return self._archive_years
        found: dict[int, str] = {}
        for page in ARCHIVE_PAGES:
            page_url = urljoin(ARCHIVE_URL, f"{page}.html")
            try:
                html = self.http.get_text(page_url, context=f"中债存档列表页 {page}.html")
            except SourceError:
                if page == ARCHIVE_PAGES[0]:
                    raise
                log.debug("存档列表分页 %s 不存在或抓取失败，停止分页", page_url)
                break
            before = len(found)
            for href, title in _iter_anchors(html):
                match = re.search(r"(\d{4})\s*年.*?标准期限", title or "")
                if not match:
                    continue
                year = int(match.group(1))
                found.setdefault(year, urljoin(page_url, href))
            if len(found) == before and page != ARCHIVE_PAGES[0]:
                break
        if not found:
            raise SourceError("中债存档列表页未解析出任何年度记录（页面结构可能已变化）")
        self._archive_years = dict(sorted(found.items()))
        return self._archive_years

    def attachment_url(self, article_url: str) -> str:
        """从文章页解析 xlsx 附件绝对 URL。"""
        html = self.http.get_text(article_url, context=f"中债文章页 {article_url}")
        match = re.search(
            r"href=['\"](\./P[^'\"]+\.(?:xlsx|xls|zip))['\"]", html, flags=re.IGNORECASE
        )
        if not match:
            raise SourceError(f"中债文章页未找到 xlsx 附件：{article_url}")
        href = match.group(1)
        if href.lower().endswith(".zip"):
            raise SourceError(f"中债附件为 zip（需人工解压），当前不支持：{article_url}")
        return urljoin(article_url, href)

    # ------------------------------------------------------------------
    # 下载
    # ------------------------------------------------------------------
    def _warm_session(self) -> None:
        """首次访问 yield 子站前建立会话（获取 Cookie/Referer）。"""
        if self._warmed:
            return
        warm_url = f"{YIELD_BASE}/yield_main?locale=zh_CN"
        try:
            self.http.get_text(warm_url, context="中债 yield 子站预热")
        except SourceError as exc:
            log.debug("预热 yield 子站失败（继续尝试下载）：%s", exc)
        self._warmed = True

    def download_year_xlsx(self, year: int) -> tuple[bytes, str]:
        """下载某年官方 xlsx，返回 ``(文件字节, 来源 URL)``。"""
        current_year = date.today().year
        if year >= current_year:
            self._warm_session()
            url = (
                f"{YIELD_BASE}/yc/downYearBzqx?year={year}&&wrjxCBFlag=0&&zblx=txy"
                f"&&ycDefId={self.yc_def_id}&&locale=zh_CN"
            )
            data = self.http.get_bytes(
                url,
                context=f"下载 {year} 年整年曲线（downYearBzqx）",
                referer=f"{YIELD_BASE}/yield_main?locale=zh_CN",
            )
            return data, url
        archive = self.list_archive_years()
        if year not in archive:
            available = sorted(archive)
            raise SourceError(
                f"中债存档列表中没有 {year} 年（可用年份 {available[0]}–{available[-1]}）；"
                "当年数据应由 downYearBzqx 接口提供"
            )
        article_url = archive[year]
        file_url = self.attachment_url(article_url)
        data = self.http.get_bytes(file_url, context=f"下载 {year} 年官方 xlsx 附件")
        return data, file_url

    def fetch_date(self, day: date) -> pd.DataFrame:
        """用 ``downBzqxDetail`` 抓取单个交易日的完整曲线（2Y 回补用）。

        注意：单日接口的参数名是 **``ycDefIds``（复数）**，并且需要带上
        ``dxbj/qxlx/yqqxN/yqqxK/wrjxCBFlag`` 等页面默认参数，否则中债返回
        "您所访问的页面不存在" 的 HTML（2026-11-06 实测；当年接口
        ``downYearBzqx`` 反而用单数 ``ycDefId``）。
        """
        self._warm_session()
        url = (
            f"{YIELD_BASE}/yc/downBzqxDetail?ycDefIds={self.yc_def_id}&&zblx=txy"
            f"&&workTime={day.isoformat()}&&dxbj=&&qxlx=&&yqqxN=N&&yqqxK=K"
            "&&wrjxCBFlag=0&&locale=zh_CN"
        )
        data = self.http.get_bytes(
            url,
            context=f"下载 {day.isoformat()} 单日曲线（downBzqxDetail）",
            min_bytes=500,
        )
        try:
            df = parse_daily_xlsx(data, day=day, curve_name=self.curve_name)
        except SourceError as exc:
            raise SourceError(f"{day.isoformat()} 单日曲线解析失败：{exc}") from exc
        df.attrs.update(
            source_url=url,
            fetched_at=datetime.now().astimezone().isoformat(timespec="seconds"),
            day=day.isoformat(),
        )
        return df

    # ------------------------------------------------------------------
    # 年度抓取
    # ------------------------------------------------------------------
    def cache_path(self, year: int) -> Path | None:
        """该年度缓存文件路径（未配置 cache_dir 时返回 None）。"""
        return None if self.cache_dir is None else self.cache_dir / f"{year}.xlsx"

    def _write_target(self, year: int) -> Path | None:
        """本次抓取的落盘目标：优先 staging 目录，否则正式快照目录。"""
        if self.staging_dir is not None:
            return self.staging_dir / f"{year}.xlsx"
        return self.cache_path(year)

    def fetch_year(
        self,
        year: int,
        *,
        force: bool = False,
        refresh_current_year: bool | None = None,
    ) -> pd.DataFrame:
        """抓取单个年度。

        缓存策略（R06）
        ---------------
        * **历史完整年度**：默认只读不可变快照，不重复下载；
        * **当年**（``year >= date.today().year``）：若 ``cfg.data.refresh_current_year``
          为真，即使快照已存在也强制下载刷新（旧快照归档到 ``<cache>/archive/``）；
        * ``force=True``：所有年份都忽略缓存、强制刷新。

        ``staging_dir`` 配置时，下载内容先写入 staging（由 loader 在整批校验通过后
        统一提交），失败不会覆盖正式快照；独立使用（未配置 staging）时直接原子写
        快照并先归档旧版本（R04）。

        Returns
        -------
        pandas.DataFrame
            长表；``attrs`` 记录 ``source_url/fetched_at/sha256/snapshot_path/
            refreshed/archived_path/current_year`` 供 manifest 登记。
        """
        cache = self.cache_path(year)
        current_year = year >= date.today().year
        refresh = (
            bool(self.cfg.data.refresh_current_year)
            if refresh_current_year is None
            else bool(refresh_current_year)
        )
        need_download = bool(force) or (current_year and refresh)
        fetched_at = datetime.now().astimezone().isoformat(timespec="seconds")
        refreshed = False
        archived: Path | None = None
        target = self._write_target(year)

        if cache is not None and cache.exists() and not need_download:
            data = cache.read_bytes()
            source_url = cache.resolve().as_uri()
            fetched_at = _mtime_iso(cache)
            log.debug("读取中债 %d 年本地不可变快照：%s", year, cache)
            df = parse_official_xlsx(data, year=year, curve_name=self.curve_name)
        else:
            data, source_url = self.download_year_xlsx(year)
            # 先解析校验、再落快照：坏文件绝不覆盖已有的好快照。
            df = parse_official_xlsx(data, year=year, curve_name=self.curve_name)
            if target is not None:
                if self.staging_dir is None and cache is not None and cache.exists():
                    archived = _archive_cached_snapshot(cache)
                _atomic_write_bytes(target, data)
                refreshed = True
                log.info(
                    "写入中债原始快照：%s（%d 字节，当年刷新=%s）",
                    target,
                    len(data),
                    current_year and refresh,
                )
        df.attrs.update(
            source_url=source_url,
            fetched_at=fetched_at,
            sha256=_sha256_bytes(data),
            year=year,
            snapshot_path=str(target) if target is not None else None,
            refreshed=refreshed,
            archived_path=str(archived) if archived is not None else None,
            current_year=current_year,
        )
        log.info("中债 %d 年：%d 行，%s → %s", year, len(df), *_date_span(df))
        return df

    def fetch(
        self,
        start: date | None = None,
        end: date | None = None,
        *,
        force: bool = False,
    ) -> pd.DataFrame:
        """抓取 ``[start, end]`` 区间（按年度循环；年度文件缓存复用）。

        返回的长表 ``attrs["sources"]`` 记录每个年度文件的抓取元信息，
        供 loader 写入 manifest。
        """
        start = start or DATE_MIN_DEFAULT
        end = end or date.today()
        if start > end:
            raise SourceError(f"起始日期 {start} 晚于结束日期 {end}")
        frames: list[pd.DataFrame] = []
        sources: list[dict[str, Any]] = []
        for year in range(start.year, end.year + 1):
            df = self.fetch_year(year, force=force)
            frames.append(df)
            record = dict(df.attrs)
            record["rows_raw"] = len(df)
            sources.append(record)
        merged = pd.concat(frames, ignore_index=True)
        merged = merged.loc[(merged["date"].dt.date >= start) & (merged["date"].dt.date <= end)]
        merged = merged.reset_index(drop=True)
        merged.attrs["sources"] = sources
        return merged


# ---------------------------------------------------------------------------
# 模块级辅助函数（loader 也会用）
# ---------------------------------------------------------------------------
def _iter_anchors(html: str) -> list[tuple[str, str]]:
    """提取 HTML 中所有 ``<a href title/text>``，返回 ``[(href, 文本)]``。"""
    out: list[tuple[str, str]] = []
    pattern = re.compile(r"<a\b([^>]*)>(.*?)</a>", flags=re.IGNORECASE | re.DOTALL)
    for attrs, inner in pattern.findall(html):
        href_match = re.search(r"href\s*=\s*['\"]([^'\"]+)['\"]", attrs, flags=re.IGNORECASE)
        if href_match is None:
            continue
        title_match = re.search(r"title\s*=\s*['\"]([^'\"]*)['\"]", attrs, flags=re.IGNORECASE)
        text = re.sub(r"<[^>]+>", "", inner)
        title = title_match.group(1) if title_match else text
        out.append((href_match.group(1), (title or text).strip()))
    return out


def _sha256_bytes(data: bytes) -> str:
    import hashlib

    return hashlib.sha256(data).hexdigest()


def _sha256_file(path: Path) -> str:
    import hashlib

    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _archive_cached_snapshot(cache: Path) -> Path | None:
    """把旧快照复制到 ``<cache>/archive/<year>.<sha12>.xlsx``（保留更新前版本）。"""
    if not cache.exists():
        return None
    sha = _sha256_file(cache)
    archive_dir = cache.parent / "archive"
    dest = archive_dir / f"{cache.stem}.{sha[:12]}{cache.suffix}"
    if dest.exists():
        return dest
    _atomic_write_bytes(dest, cache.read_bytes())
    log.info("归档旧版中债快照：%s", dest)
    return dest


def _mtime_iso(path: Path) -> str:
    return datetime.fromtimestamp(path.stat().st_mtime).astimezone().isoformat(timespec="seconds")


def _date_span(df: pd.DataFrame) -> tuple[str, str]:
    return (
        str(pd.to_datetime(df["date"]).min().date()),
        str(pd.to_datetime(df["date"]).max().date()),
    )


def _atomic_write_bytes(path: Path, data: bytes) -> None:
    """原子写文件：先写临时文件再 ``os.replace``，避免半截文件。"""
    import os
    import tempfile

    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(tmp, path)
    except BaseException:
        Path(tmp).unlink(missing_ok=True)
        raise


__all__ = [
    "ARCHIVE_URL",
    "DEFAULT_CURVE_NAME",
    "DEFAULT_YC_DEF_ID",
    "TENOR_LABELS",
    "YIELD_BASE",
    "ChinabondSource",
    "parse_daily_xlsx",
    "parse_official_xlsx",
    "parse_spike_csv",
]
