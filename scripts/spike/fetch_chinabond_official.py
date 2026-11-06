#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
脚本名: fetch_chinabond_official.py
用途  : 从中国债券信息网（中债估值中心）公开页面，无需登录下载
        「中债国债收益率曲线（到期）标准期限」历史数据。
        数据源 = 中债官方 xlsx（每年一个文件，2002 年至今）。

已验证事实（2026-11-06 实测，本脚本即证据）:
  * 往年列表页（无登录/无验证码）:
    https://www.chinabond.com.cn/zzsj/zzsj_zzjgcp/zzjgcp_cpxz/cpxz_qxxz/qxxz_zzgzqx/
    分页 index.html / index_1.html / index_2.html，共 24 条（2002–2025）
  * 每条记录对应一个文章页，文章页附件 = ./P0xxxx.xlsx
  * 当年数据（2026）: yield.chinabond.com.cn 的 downYearBzqx 接口
      POST https://yield.chinabond.com.cn/cbweb-mn/yc/downYearBzqx?year=2026&...&ycDefId=2c9081e50a2f9606010a3068cae70001
      （ycDefId 来自 /cbweb-mn/yc/queryTree 中的「中债国债收益率曲线」节点）
  * 输出列: 日期 / 标准期限说明 / 标准期限(年) / 收益率(%)
  * 期限点: 0d,1m,2m,3m,6m,9m,1y,2y,3y,5y,7y,10y,15y,20y,30y,40y,50y

运行:
    /Users/aq/quant/china_debt/.venv-spike/bin/python scripts/spike/fetch_chinabond_official.py

产出:
    data/raw_spike/chinabond_gzqx/{year}.xlsx        原始官方文件（列名保持原样）
    data/raw_spike/cb_gzqx_standard_terms_2002_2026.csv  合并后长表
    data/raw_spike/_download_log.csv                 每个文件的下载结果
"""

import os
import re
import sys
import time
import io
from pathlib import Path

import requests
import pandas as pd

ROOT = Path(__file__).resolve().parents[2]
RAW = ROOT / "data" / "raw_spike"
SRC = RAW / "chinabond_gzqx"
SRC.mkdir(parents=True, exist_ok=True)

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")
HEADERS = {"User-Agent": UA}

ARCHIVE = "https://www.chinabond.com.cn/zzsj/zzsj_zzjgcp/zzjgcp_cpxz/cpxz_qxxz/qxxz_zzgzqx/"
YIELD_BASE = "https://yield.chinabond.com.cn/cbweb-mn"
YC_DEF_ID = "2c9081e50a2f9606010a3068cae70001"  # 中债国债收益率曲线(到期)

session = requests.Session()
session.headers.update(HEADERS)


def get(url, **kw):
    kw.setdefault("timeout", 40)
    r = session.get(url, **kw)
    r.raise_for_status()
    # 中债静态页 header 不带 charset，requests 会误判成 ISO-8859-1 → 中文乱码
    if not r.encoding or r.encoding.lower() == "iso-8859-1":
        r.encoding = "utf-8"
    return r


def list_archived_years():
    """返回 [(year:int, article_url:str), ...]，倒序（新年份在前）"""
    out = []
    for page in ["index", "index_1", "index_2"]:
        html = get(f"{ARCHIVE}{page}.html").text
        for href, title in re.findall(r"<a href='([^']+)'\s*\n?\s*title='([^']+)'", html):
            m = re.search(r"(\d{4})年.*标准期限", title)
            if not m:
                continue
            out.append((int(m.group(1)), href))
    # 去重 + 按年份排序
    seen, uniq = set(), []
    for y, u in out:
        if y not in seen:
            seen.add(y)
            uniq.append((y, u))
    uniq.sort(key=lambda x: x[0])
    return uniq


def attachment_url(article_url):
    html = get(article_url).text
    m = re.search(r"href=['\"](\./P[^'\"]+\.(?:xlsx|xls|zip))['\"]", html)
    if not m:
        raise RuntimeError(f"附件未找到: {article_url}")
    rel = m.group(1)[2:]  # 去掉 ./
    return article_url.rsplit("/", 1)[0] + "/" + rel


def download_year(y, article_url, log):
    dest = SRC / f"{y}.xlsx"
    try:
        url = attachment_url(article_url)
        r = get(url)
        dest.write_bytes(r.content)
        log.append({"year": y, "url": url, "bytes": len(r.content),
                    "ok": True, "note": ""})
        print(f"  [{y}] OK {len(r.content):>8,} B  {url.split('/')[-1]}")
    except Exception as e:  # noqa: BLE001
        log.append({"year": y, "url": article_url, "bytes": 0,
                    "ok": False, "note": repr(e)[:300]})
        print(f"  [{y}] FAIL {e!r}")


def download_current_year(year, log):
    """当年数据走 yield.chinabond.com.cn 的整年下载接口"""
    dest = SRC / f"{year}.xlsx"
    try:
        session.get(f"{YIELD_BASE}/yield_main?locale=zh_CN", timeout=40)
        url = (f"{YIELD_BASE}/yc/downYearBzqx?year={year}&&wrjxCBFlag=0&&zblx=txy"
               f"&&ycDefId={YC_DEF_ID}&&locale=zh_CN")
        r = session.get(url, headers={"Referer": f"{YIELD_BASE}/yield_main?locale=zh_CN"},
                        timeout=60)
        r.raise_for_status()
        if not dest.exists() or len(r.content) > dest.stat().st_size:
            dest.write_bytes(r.content)
        log.append({"year": year, "url": url, "bytes": len(r.content),
                    "ok": True, "note": "current-year endpoint"})
        print(f"  [{year}] OK {len(r.content):>8,} B  (downYearBzqx 当年接口)")
    except Exception as e:  # noqa: BLE001
        log.append({"year": year, "url": "", "bytes": 0, "ok": False, "note": repr(e)[:300]})
        print(f"  [{year}] FAIL {e!r}")


def read_year_file(path):
    """读官方 xlsx；老文件可能是 xls，用 pandas 自动识别引擎"""
    xls = pd.ExcelFile(path)
    df = xls.parse(xls.sheet_names[0], header=0)
    # 老文件偶有多余空列/空格
    df = df.loc[:, [c for c in df.columns if not str(c).startswith("Unnamed")]]
    df.columns = [str(c).strip() for c in df.columns]
    return df


def main():
    log = []
    print("== 1. 抓取往年（2002–2025）官方存档 ==")
    years = list_archived_years()
    print(f"   共发现 {len(years)} 个年度: {years[0][0]}–{years[-1][0]}")
    for y, art in years:
        download_year(y, art, log)
        time.sleep(0.4)

    print("== 2. 抓取当年（2026）官方接口 ==")
    download_current_year(2026, log)
    time.sleep(0.3)

    pd.DataFrame(log).to_csv(RAW / "_download_log.csv", index=False, encoding="utf-8-sig")
    ok = sum(1 for x in log if x["ok"])
    print(f"   下载结果: {ok}/{len(log)} 成功")

    print("== 3. 合并所有年度 ==")
    frames, per_year = [], []
    for p in sorted(SRC.glob("*.xlsx")):
        y = int(p.stem)
        try:
            df = read_year_file(p)
        except Exception as e:  # noqa: BLE001
            print(f"   解析失败 {p.name}: {e!r}")
            continue
        df["_source_file"] = p.name
        frames.append(df)
        per_year.append({"year": y, "rows": len(df), "cols": ";".join(map(str, df.columns[:6]))})

    all_df = pd.concat(frames, ignore_index=True)
    # 统一列名
    rename = {"日期": "date", "标准期限说明": "term_label", "标准期限(年)": "term_years",
              "收益率(%)": "yield_pct"}
    all_df = all_df.rename(columns=rename)
    for c in ["date", "term_label", "term_years", "yield_pct"]:
        if c not in all_df.columns:
            print(f"!! 缺少列 {c}，实际列: {list(all_df.columns)}")
            sys.exit(1)
    all_df["date"] = pd.to_datetime(all_df["date"], errors="coerce")
    all_df["term_years"] = pd.to_numeric(all_df["term_years"], errors="coerce")
    all_df["yield_pct"] = pd.to_numeric(all_df["yield_pct"], errors="coerce")
    bad = all_df[all_df["date"].isna() | all_df["yield_pct"].isna()]
    if len(bad):
        print(f"   !! {len(bad)} 行解析失败，样例:\n{bad.head(3).to_string()}")
    out = all_df.dropna(subset=["date", "term_years", "yield_pct"]).copy()
    out = out.sort_values(["date", "term_years"]).reset_index(drop=True)

    csv_path = RAW / "cb_gzqx_standard_terms_2002_2026.csv"
    out.to_csv(csv_path, index=False, encoding="utf-8-sig")

    print("\n== 4. 结果概览 ==")
    print(f"   总行数      : {len(out):,}")
    print(f"   日期范围    : {out['date'].min().date()} → {out['date'].max().date()}")
    print(f"   交易日数    : {out['date'].nunique():,}")
    print(f"   期限点      : {sorted(out['term_label'].unique())}")
    print(f"   列名(原始)  : {list(frames[0].columns)}")
    print("\n   每年覆盖:")
    cov = (out.assign(y=out["date"].dt.year)
              .groupby("y")
              .agg(交易天数=("date", "nunique"),
                   首个=("date", "min"), 最后=("date", "max"),
                   缺失收益率=("yield_pct", lambda s: int(s.isna().sum()))))
    print(cov.to_string())
    print(f"\n   CSV 已写入: {csv_path}")


if __name__ == "__main__":
    main()
