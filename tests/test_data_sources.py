"""数据源适配器测试（全部离线）。

覆盖：
1. 中债官方 xlsx 解析器：表头别名、标题行、多 sheet、坏行容错、单日文件曲线筛选；
2. 中债官网列表页 / 附件页 / 编码修复 / 重试逻辑（用 fake session，不联网）；
3. :class:`~ficc.data.sources.base.DataSource` 契约（空表必须报错）；
4. AKShare 可选依赖降级 + 分片规则（用 fake akshare，不联网）；
5. 真实 Phase 0 缓存的冒烟测试（文件不存在时 skip）。
"""

from __future__ import annotations

from datetime import date
from pathlib import Path
from typing import ClassVar

import pandas as pd
import pytest
import requests

from ficc.data.schema import CANONICAL_TENORS
from ficc.data.sources import akshare_source
from ficc.data.sources.akshare_source import AK_AVAILABLE, AkshareSource
from ficc.data.sources.base import DataSource, SourceError
from ficc.data.sources.chinabond import (
    ChinabondSource,
    _HttpClient,
    parse_daily_xlsx,
    parse_official_xlsx,
    parse_spike_csv,
)

ROOT = Path(__file__).resolve().parents[1]
SPIKE_DIR = ROOT / "data" / "raw_spike" / "chinabond_gzqx"
SPIKE_CSV = ROOT / "data" / "raw_spike" / "cb_gzqx_standard_terms_2002_2026.csv"


# ---------------------------------------------------------------------------
# 构造 xlsx 的工具
# ---------------------------------------------------------------------------
def _official_rows(
    days: list[str],
    *,
    labels: list[str] | None = None,
    values: float = 2.0,
) -> list[dict[str, object]]:
    labels = labels or [
        "0d",
        "1m",
        "2m",
        "3m",
        "6m",
        "9m",
        "1y",
        "2y",
        "3y",
        "5y",
        "7y",
        "10y",
        "15y",
        "20y",
        "30y",
        "40y",
        "50y",
    ]
    rows: list[dict[str, object]] = []
    for day in days:
        for idx, label in enumerate(labels):
            rows.append(
                {
                    "日期": day,
                    "标准期限说明": label,
                    "标准期限(年)": _label_to_years(label),
                    "收益率(%)": values + idx * 0.01,
                }
            )
    return rows


def _label_to_years(label: str) -> float:
    mapping = {
        "0d": 0.0,
        "1m": 0.08,
        "2m": 0.17,
        "3m": 0.25,
        "6m": 0.5,
        "9m": 0.75,
        "1y": 1.0,
        "2y": 2.0,
        "3y": 3.0,
        "5y": 5.0,
        "7y": 7.0,
        "10y": 10.0,
        "15y": 15.0,
        "20y": 20.0,
        "30y": 30.0,
        "40y": 40.0,
        "50y": 50.0,
    }
    return mapping[label]


def write_official_xlsx(
    path: Path,
    *,
    days: list[str],
    sheet_name: str = "sheet1",
    header_style: str = "label",
    title_row: bool = False,
) -> Path:
    """写一个官方风格 xlsx（用于解析器单测）。"""
    rows = _official_rows(days)
    frame = pd.DataFrame(rows)
    if header_style == "years_only":
        frame = frame.rename(columns={"标准期限说明": "期限", "标准期限(年)": "期限(年)"})
        frame = frame.rename(columns={"收益率(%)": "收益率"})
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        if title_row:
            pd.DataFrame([["中债国债收益率曲线标准期限信息"]]).to_excel(
                writer, sheet_name=sheet_name, header=False, index=False
            )
            frame.to_excel(writer, sheet_name=sheet_name, startrow=1, index=False)
        else:
            frame.to_excel(writer, sheet_name=sheet_name, index=False)
    return path


def write_multi_sheet_xlsx(path: Path, *, days: list[str]) -> Path:
    with pd.ExcelWriter(path, engine="openpyxl") as writer:
        pd.DataFrame({"说明": ["本文件由中债估值中心提供"]}).to_excel(
            writer, sheet_name="说明", index=False
        )
        pd.DataFrame(_official_rows(days)).to_excel(writer, sheet_name="数据", index=False)
        pd.DataFrame().to_excel(writer, sheet_name="Sheet3", index=False)
    return path


# ---------------------------------------------------------------------------
# 官方 xlsx 解析
# ---------------------------------------------------------------------------
def test_parse_official_xlsx_maps_labels_and_dates(tmp_path: Path) -> None:
    path = write_official_xlsx(tmp_path / "2024.xlsx", days=["2024-01-02", "2024-01-03"])
    frame = parse_official_xlsx(path, year=2024)
    # 解析器保留全部 17 个官方期限点（含 0d），网格裁剪由 clean 层负责
    assert len(frame) == 34
    assert set(frame.columns) == {"date", "tenor", "ytm_pct", "term_label", "source", "curve_name"}
    assert frame["source"].unique().tolist() == ["chinabond"]
    assert frame["curve_name"].unique().tolist() == ["中债国债收益率曲线(到期)"]
    assert 0.0 in set(frame["tenor"])
    assert 5.0 in set(frame["tenor"])
    # 1M 官方写成 0.08 年，应按标签映射为 1/12
    one_month = frame.loc[frame["term_label"] == "1m", "tenor"].iloc[0]
    assert one_month == pytest.approx(1 / 12)
    assert pd.api.types.is_datetime64_any_dtype(frame["date"])
    assert frame["date"].min() == pd.Timestamp("2024-01-02")
    assert frame["ytm_pct"].dtype == "float64"


def test_parse_official_xlsx_handles_title_row_and_header_aliases(tmp_path: Path) -> None:
    path = write_official_xlsx(
        tmp_path / "2020.xlsx", days=["2020-01-02"], header_style="years_only", title_row=True
    )
    frame = parse_official_xlsx(path, year=2020)
    # 没有期限标签列时，用「标准期限(年)」反推（0.08 → 1/12 等）
    assert len(frame) == 17
    assert 0.0 in set(frame["tenor"])
    assert 1 / 12 in set(frame["tenor"])  # 0.08 年 → 1M
    assert 10.0 in set(frame["tenor"])


def test_parse_official_xlsx_selects_data_sheet(tmp_path: Path) -> None:
    path = write_multi_sheet_xlsx(tmp_path / "2018.xlsx", days=["2018-01-02"])
    frame = parse_official_xlsx(path, year=2018)
    assert len(frame) == 17
    assert frame["date"].iloc[0] == pd.Timestamp("2018-01-02")


def test_parse_official_xlsx_rejects_missing_columns(tmp_path: Path) -> None:
    path = tmp_path / "bad.xlsx"
    pd.DataFrame({"日期": ["2024-01-02"], "其他": [1]}).to_excel(path, index=False)
    with pytest.raises(SourceError, match=r"没有任何工作表|缺"):
        parse_official_xlsx(path, year=2024)


def test_parse_official_xlsx_rejects_too_many_bad_rows(tmp_path: Path) -> None:
    rows = _official_rows(["2024-01-02"])
    for row in rows:
        row["日期"] = "不是日期"
    path = tmp_path / "bad_dates.xlsx"
    pd.DataFrame(rows).to_excel(path, index=False)
    with pytest.raises(SourceError, match="无法解析"):
        parse_official_xlsx(path, year=2024)


def test_parse_official_xlsx_tolerates_few_bad_rows(tmp_path: Path) -> None:
    rows = _official_rows(["2024-01-02", "2024-01-03", "2024-01-04"])
    rows[3]["日期"] = "坏日期"  # 2 行坏 < 3 行容忍上限：丢弃并 warning，不整体失败
    rows[4]["日期"] = "坏日期"
    path = tmp_path / "few_bad.xlsx"
    pd.DataFrame(rows).to_excel(path, index=False)
    frame = parse_official_xlsx(path, year=2024)
    assert frame["date"].nunique() == 3
    assert len(frame) == 3 * 17 - 2


def test_parse_daily_xlsx_filters_treasury_and_maps_tenors(tmp_path: Path) -> None:
    rows = [
        {
            "日期": "2017-06-30",
            "曲线名称": "中债国债收益率曲线(到期)",
            "标准期限(年)": 0.0,
            "收益率(%)": 2.8293,
        },
        {
            "日期": "2017-06-30",
            "曲线名称": "中债国债收益率曲线(到期)",
            "标准期限(年)": 0.08,
            "收益率(%)": 3.0548,
        },
        {
            "日期": "2017-06-30",
            "曲线名称": "中债国债收益率曲线(到期)",
            "标准期限(年)": 2.0,
            "收益率(%)": 3.3,
        },
        {
            "日期": "2017-06-30",
            "曲线名称": "中债国债收益率曲线(到期)",
            "标准期限(年)": 10.0,
            "收益率(%)": 3.6,
        },
        {
            "日期": "2017-06-30",
            "曲线名称": "中债商业银行普通债收益率曲线(AAA)",
            "标准期限(年)": 8.0,
            "收益率(%)": 4.4,
        },
    ]
    path = tmp_path / "2017-06-30.xlsx"
    pd.DataFrame(rows).to_excel(path, index=False)
    frame = parse_daily_xlsx(path, day=date(2017, 6, 30))
    assert len(frame) == 4  # 保留 0d 原始点，商业银行曲线被过滤
    assert 0.0 in set(frame["tenor"])
    assert 2.0 in set(frame["tenor"]) and 10.0 in set(frame["tenor"])
    assert 1 / 12 in set(frame["tenor"])
    assert (frame["curve_name"] == "中债国债收益率曲线(到期)").all()


def test_parse_daily_xlsx_without_date_column(tmp_path: Path) -> None:
    """真实 downBzqxDetail xlsx 没有「日期」列，日期由调用方传入。"""
    rows = [
        {"曲线名称": "中债国债收益率曲线(到期)", "标准期限(年)": 0.08, "收益率(%)": 2.2663},
        {"曲线名称": "中债国债收益率曲线(到期)", "标准期限(年)": 2.0, "收益率(%)": 2.55},
        {"曲线名称": "中债国债收益率曲线(到期)", "标准期限(年)": 10.0, "收益率(%)": 2.80},
    ]
    path = tmp_path / "single.xlsx"
    pd.DataFrame(rows).to_excel(path, index=False)
    frame = parse_daily_xlsx(path, day=date(2016, 1, 4))
    assert len(frame) == 3
    assert frame["date"].nunique() == 1
    assert frame["date"].iloc[0] == pd.Timestamp("2016-01-04")
    assert 2.0 in set(frame["tenor"]) and 10.0 in set(frame["tenor"])


def test_parse_daily_xlsx_raises_when_no_treasury_curve(tmp_path: Path) -> None:
    rows = [
        {
            "日期": "2017-06-30",
            "曲线名称": "中债商业银行普通债收益率曲线(AAA)",
            "标准期限(年)": 10.0,
            "收益率(%)": 4.4,
        }
    ]
    path = tmp_path / "bad.xlsx"
    pd.DataFrame(rows).to_excel(path, index=False)
    with pytest.raises(SourceError, match="未找到国债曲线"):
        parse_daily_xlsx(path, day=date(2017, 6, 30))


def test_parse_spike_csv_if_available() -> None:
    if not SPIKE_CSV.exists():
        pytest.skip(f"缺少 Phase 0 合并长表：{SPIKE_CSV}")
    frame = parse_spike_csv(SPIKE_CSV)
    assert len(frame) > 100_000
    assert frame["date"].min().date() == date(2002, 1, 4)
    assert 10.0 in set(frame["tenor"])


def test_parse_real_official_xlsx_if_available() -> None:
    path = SPIKE_DIR / "2002.xlsx"
    if not path.exists():
        pytest.skip(f"缺少官方 xlsx：{path}")
    frame = parse_official_xlsx(path, year=2002)
    assert len(frame) > 4_000
    assert frame["date"].min().date() == date(2002, 1, 4)
    assert 30.0 in set(frame["tenor"])


# ---------------------------------------------------------------------------
# DataSource 契约
# ---------------------------------------------------------------------------
def test_datasource_is_abstract() -> None:
    with pytest.raises(TypeError):
        DataSource()  # type: ignore[abstract]


def test_normalize_long_rejects_empty_or_missing_columns() -> None:
    with pytest.raises(SourceError, match="空表"):
        DataSource.normalize_long(pd.DataFrame(), context="测试", source="x")
    with pytest.raises(SourceError, match="缺少必需列"):
        DataSource.normalize_long(
            pd.DataFrame({"date": ["2024-01-02"]}), context="测试", source="x"
        )
    with pytest.raises(SourceError, match="None"):
        DataSource.normalize_long(None, context="测试", source="x")


def test_normalize_long_drops_zero_tenor_and_dedupes() -> None:
    frame = pd.DataFrame(
        {
            "date": ["2024-01-02", "2024-01-02", "2024-01-02"],
            "tenor": [0.0, 1.0, 1.0],
            "ytm_pct": [1.5, 2.0, 2.0],
        }
    )
    out = DataSource.normalize_long(frame, context="测试", source="x")
    assert len(out) == 1
    assert out["tenor"].iloc[0] == 1.0


# ---------------------------------------------------------------------------
# 官网 HTTP 逻辑（fake session）
# ---------------------------------------------------------------------------
class FakeResponse:
    def __init__(self, content: bytes, *, status_code: int = 200, encoding: str | None = None):
        self.content = content
        self.status_code = status_code
        self.encoding = encoding

    @property
    def text(self) -> str:
        return self.content.decode(self.encoding or "utf-8", errors="replace")


class FakeSession:
    """按 URL 返回预置内容的最小 requests.Session 替身。"""

    def __init__(self, mapping: dict[str, bytes], *, status: int = 200):
        self.mapping = mapping
        self.status = status
        self.headers: dict[str, str] = {}
        self.calls: list[str] = []

    def get(self, url: str, timeout: float | None = None, headers: dict | None = None):
        self.calls.append(url)
        if url in self.mapping:
            return FakeResponse(self.mapping[url], status_code=200)
        return FakeResponse(b"", status_code=self.status)


def test_list_archive_years_parses_titles(tmp_path: Path) -> None:
    page0 = (
        "<a href='./t2025.html' title='2025年标准期限国债收益率曲线'>x</a>"
        "<a href='./t2024.html' title='2024年标准期限国债收益率曲线'>y</a>"
    ).encode()
    page1 = "<a href='./t2002.html' title='2002年标准期限国债收益率曲线'>z</a>".encode()
    session = FakeSession({})
    source = ChinabondSource(cfg=_cfg(), session=session)
    # 用 monkeypatch 方式替换请求结果
    urls = [
        "https://www.chinabond.com.cn/zzsj/zzsj_zzjgcp/zzjgcp_cpxz/cpxz_qxxz/qxxz_zzgzqx/index.html",
        "https://www.chinabond.com.cn/zzsj/zzsj_zzjgcp/zzjgcp_cpxz/cpxz_qxxz/qxxz_zzgzqx/index_1.html",
        "https://www.chinabond.com.cn/zzsj/zzsj_zzjgcp/zzjgcp_cpxz/cpxz_qxxz/qxxz_zzgzqx/index_2.html",
    ]
    session.mapping = {urls[0]: page0, urls[1]: page1, urls[2]: b"<html></html>"}
    years = source.list_archive_years()
    assert set(years) == {2025, 2024, 2002}
    assert years[2025].endswith("./t2025.html") or years[2025].endswith("t2025.html")


def test_http_client_fixes_wrong_charset() -> None:
    html = ("<a title='2025年标准期限'>中债国债收益率曲线标准期限信息下载</a>").encode()
    session = FakeSession({"https://example.com/a.html": html})
    client = _HttpClient(session=session, retries=1, backoff_s=0.0)
    text = client.get_text("https://example.com/a.html", context="测试")
    assert "2025年标准期限" in text


def test_http_client_retries_and_raises_specific_error() -> None:
    class BrokenSession:
        headers: ClassVar[dict[str, str]] = {}
        calls = 0

        def get(self, *args, **kwargs):
            type(self).calls += 1
            raise requests.ConnectionError("boom")

    client = _HttpClient(session=BrokenSession(), retries=2, backoff_s=0.0)
    with pytest.raises(SourceError, match="2 次尝试"):
        client.get_bytes("https://example.com/x.xlsx", context="下载测试")
    assert BrokenSession.calls == 2


def test_fetch_year_uses_cache_without_network(tmp_path: Path) -> None:
    class NoNetworkSession:
        headers: ClassVar[dict[str, str]] = {}
        calls = 0

        def get(self, *args, **kwargs):
            type(self).calls += 1
            raise AssertionError("不应访问网络：缓存已存在")

    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    write_official_xlsx(cache_dir / "2024.xlsx", days=["2024-01-02"])
    source = ChinabondSource(cfg=_cfg(), cache_dir=cache_dir, session=NoNetworkSession())
    frame = source.fetch_year(2024, force=False)
    assert len(frame) == 17
    assert NoNetworkSession.calls == 0


def test_attachment_url_from_article_page() -> None:
    article = "https://www.chinabond.com.cn/arrive/2025/t20250101_1.html"
    html = "<a href='./P0120250101.xlsx'>附件：标准期限信息下载</a>".encode()
    source = ChinabondSource(cfg=_cfg(), session=FakeSession({article: html}))
    url = source.attachment_url(article)
    assert url.endswith("P0120250101.xlsx")


# ---------------------------------------------------------------------------
# AKShare 源（可选依赖 + fake akshare）
# ---------------------------------------------------------------------------
class FakeAkshare:
    def __init__(self, *, empty_yield: bool = False, empty_repo: bool = False):
        self.empty_yield = empty_yield
        self.empty_repo = empty_repo
        self.yield_calls: list[tuple[str, str]] = []
        self.repo_calls: list[tuple[str, str]] = []

    def bond_china_yield(self, start_date: str, end_date: str) -> pd.DataFrame:
        self.yield_calls.append((start_date, end_date))
        if self.empty_yield:
            return pd.DataFrame()
        day = f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:]}"
        return pd.DataFrame(
            {
                "曲线名称": ["中债国债收益率曲线", "中债商业银行普通债收益率曲线(AAA)"],
                "日期": [day, day],
                "3月": [2.0, 2.5],
                "6月": [2.1, 2.6],
                "1年": [2.2, 2.7],
                "3年": [2.3, 2.8],
                "5年": [2.4, 2.9],
                "7年": [2.5, 3.0],
                "10年": [2.6, 3.1],
                "30年": [2.9, 3.4],
            }
        )

    def repo_rate_hist(self, start_date: str, end_date: str) -> pd.DataFrame:
        self.repo_calls.append((start_date, end_date))
        if self.empty_repo:
            return pd.DataFrame()
        return pd.DataFrame(
            {
                "date": [f"{start_date[:4]}-{start_date[4:6]}-{start_date[6:]}"],
                "FR001": [2.0],
                "FR007": [2.1],
                "FR014": [2.2],
                "FDR001": [1.9],
                "FDR007": [2.0],
                "FDR014": [2.1],
            }
        )


def _cfg():
    from ficc.config import get_config

    return get_config()


def test_akshare_source_requires_dependency() -> None:
    if AK_AVAILABLE:
        pytest.skip("当前环境已安装 akshare，跳过降级测试")
    with pytest.raises(SourceError, match="akshare"):
        AkshareSource(cfg=_cfg())


def test_akshare_yield_sharding(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeAkshare()
    monkeypatch.setattr(akshare_source, "ak", fake)
    source = AkshareSource(cfg=_cfg(), throttle_s=0.0)
    frame = source.fetch(date(2023, 6, 1), date(2024, 2, 1))
    # 必须按年分片，且每片 ≤ 1 年
    assert len(fake.yield_calls) == 2
    for start, end in fake.yield_calls:
        assert pd.Timestamp(start) <= pd.Timestamp(end)
    # AKShare 只有 8 个期限点（无 2Y/15Y/20Y）
    assert 3.0 in set(frame["tenor"]) and 30.0 in set(frame["tenor"])
    assert len(set(frame["tenor"])) == 8
    # 只保留国债曲线；两段分片各 8 个期限点
    assert (frame["curve_name"].str.contains("国债")).all()
    assert len(frame) == 16
    assert frame["date"].nunique() == 2


def test_akshare_yield_empty_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeAkshare(empty_yield=True)
    monkeypatch.setattr(akshare_source, "ak", fake)
    source = AkshareSource(cfg=_cfg(), throttle_s=0.0)
    with pytest.raises(SourceError, match="空表"):
        source.fetch(date(2024, 1, 1), date(2024, 2, 1))


def test_akshare_repo_monthly_sharding(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeAkshare()
    monkeypatch.setattr(akshare_source, "ak", fake)
    source = AkshareSource(cfg=_cfg(), throttle_s=0.0)
    frame = source.fetch_repo_rates(date(2024, 1, 15), date(2024, 3, 10))
    assert len(fake.repo_calls) == 3
    # 每月分片窗口 ≤ 31 天
    for start, end in fake.repo_calls:
        assert (pd.Timestamp(end) - pd.Timestamp(start)).days <= 31
    assert list(frame.columns) == ["date", "FR001", "FR007", "FR014", "FDR001", "FDR007", "FDR014"]
    assert len(frame) == 3


def test_akshare_repo_all_empty_raises(monkeypatch: pytest.MonkeyPatch) -> None:
    fake = FakeAkshare(empty_repo=True)
    monkeypatch.setattr(akshare_source, "ak", fake)
    source = AkshareSource(cfg=_cfg(), throttle_s=0.0)
    with pytest.raises(SourceError, match="全部返回空表"):
        source.fetch_repo_rates(date(2024, 1, 1), date(2024, 2, 1))


# ---------------------------------------------------------------------------
# 真实网络测试（默认被 pytest -m "not network" 过滤）
# ---------------------------------------------------------------------------
@pytest.mark.network
def test_network_fetch_single_day_real() -> None:
    source = ChinabondSource(cfg=_cfg(), throttle_s=0.2)
    frame = source.fetch_date(date(2024, 6, 28))
    assert not frame.empty
    assert {0.25, 1.0, 2.0, 5.0, 10.0, 30.0}.issubset(set(frame["tenor"]))
    assert CANONICAL_TENORS[0] in set(frame["tenor"])


# ---------------------------------------------------------------------------
# R06：当年缓存强制刷新 / 历史年度缓存复用 / 旧快照归档
# ---------------------------------------------------------------------------
def _current_year_days(count: int) -> list[str]:
    year = date.today().year
    days = pd.bdate_range(f"{year}-01-02", periods=count)
    return [day.strftime("%Y-%m-%d") for day in days]


def test_current_year_cache_is_refreshed_when_configured(tmp_path: Path, monkeypatch) -> None:
    cfg = _cfg()
    year = date.today().year
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    write_official_xlsx(cache_dir / f"{year}.xlsx", days=_current_year_days(1))
    staging = tmp_path / "staging"
    staging.mkdir(parents=True, exist_ok=True)
    new_xlsx = tmp_path / "new.xlsx"
    write_official_xlsx(new_xlsx, days=_current_year_days(3))

    source = ChinabondSource(cfg=cfg, cache_dir=cache_dir, staging_dir=staging)
    calls: list[int] = []

    def fake_download(y: int):
        calls.append(y)
        return new_xlsx.read_bytes(), "http://example.com/new.xlsx"

    monkeypatch.setattr(source, "download_year_xlsx", fake_download)
    old_cache_bytes = (cache_dir / f"{year}.xlsx").read_bytes()
    frame = source.fetch_year(year)
    assert calls == [year]
    assert frame["date"].nunique() == 3
    # 下载内容先写 staging，正式快照在 loader 提交前保持不变
    assert (staging / f"{year}.xlsx").exists()
    assert (cache_dir / f"{year}.xlsx").read_bytes() == old_cache_bytes
    assert source.fetch_year(year, refresh_current_year=False)["date"].nunique() == 1
    assert calls == [year]  # 显式关闭刷新后命中旧缓存，不再下载


def test_historical_year_uses_cache_without_downloading(tmp_path: Path, monkeypatch) -> None:
    cfg = _cfg()
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    write_official_xlsx(cache_dir / "2019.xlsx", days=["2019-01-02", "2019-01-03"])
    source = ChinabondSource(cfg=cfg, cache_dir=cache_dir)

    def no_download(y: int):
        raise AssertionError(f"历史年份 {y} 不应重新下载")

    monkeypatch.setattr(source, "download_year_xlsx", no_download)
    frame = source.fetch_year(2019)
    assert frame["date"].nunique() == 2


def test_current_year_refresh_can_be_disabled(tmp_path: Path, monkeypatch) -> None:
    cfg = _cfg()
    year = date.today().year
    cfg2 = cfg.model_copy(
        update={"data": cfg.data.model_copy(update={"refresh_current_year": False})}
    )
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    write_official_xlsx(cache_dir / f"{year}.xlsx", days=_current_year_days(2))
    source = ChinabondSource(cfg=cfg2, cache_dir=cache_dir)

    def no_download(y: int):
        raise AssertionError("refresh_current_year=False 时不应下载当年文件")

    monkeypatch.setattr(source, "download_year_xlsx", no_download)
    frame = source.fetch_year(year)
    assert frame["date"].nunique() == 2


def test_current_year_refresh_archives_old_snapshot(tmp_path: Path, monkeypatch) -> None:
    """没有 staging（独立使用 source）时，刷新前必须归档旧快照。"""
    cfg = _cfg()
    year = date.today().year
    cache_dir = tmp_path / "cache"
    cache_dir.mkdir(parents=True, exist_ok=True)
    cache_path = write_official_xlsx(cache_dir / f"{year}.xlsx", days=_current_year_days(1))
    old_sha = __import__("hashlib").sha256(cache_path.read_bytes()).hexdigest()
    new_xlsx = tmp_path / "new.xlsx"
    write_official_xlsx(new_xlsx, days=_current_year_days(4))

    source = ChinabondSource(cfg=cfg, cache_dir=cache_dir)
    monkeypatch.setattr(
        source, "download_year_xlsx", lambda y: (new_xlsx.read_bytes(), "http://example.com/x")
    )
    frame = source.fetch_year(year)
    assert frame["date"].nunique() == 4
    archived = list((cache_dir / "archive").glob(f"{year}.*.xlsx"))
    assert archived
    assert any(old_sha[:12] in path.name for path in archived)
    assert cache_path.read_bytes() == new_xlsx.read_bytes()
