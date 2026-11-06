#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
脚本名: fetch_cffex_futures.py
用途  : 抓取中金所国债期货（T / TF / TS / TL）历史日行情。
        中金所每个交易日提供一个 CSV：
            http://www.cffex.com.cn/sj/hqsj/rtj/{yyyymm}/{dd}/{yyyymmdd}_1.csv
        注意：akshare 的 futures_hist_daily_cffex 在 2023 年之前的老日期上会报
        ValueError: Length mismatch（旧文件 15 列、新文件 14 列），所以这里自写
        向量化容错解析——实测 2015-03-20 / 2018-01-02 / 2023-04-21 均可正常下载。

已验证: T 合约自 2015-03-20 上市首日起可抓；TF 自 2013-09-06 起。

用法:
    .venv-spike/bin/python scripts/spike/fetch_cffex_futures.py --dates 20150320,20261105
    .venv-spike/bin/python scripts/spike/fetch_cffex_futures.py --start 20260101 --end 20261105
产出:
    data/raw_spike/cffex_gzqh_{start}_{end}.csv
"""

import argparse
import io
import time
from pathlib import Path

import pandas as pd
import requests

ROOT = Path(__file__).resolve().parents[2]
RAW = ROOT / "data" / "raw_spike"
RAW.mkdir(parents=True, exist_ok=True)

URL = "http://www.cffex.com.cn/sj/hqsj/rtj/{ym}/{dd}/{d}_1.csv"
HEADERS = {"User-Agent": ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                          "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36")}


def parse_one(date: str):
    url = URL.format(ym=date[:6], dd=date[6:], d=date)
    r = requests.get(url, headers=HEADERS, timeout=30)
    if r.status_code != 200 or len(r.content) < 100:
        return None
    df = pd.read_csv(io.BytesIO(r.content), encoding="gbk")
    df.columns = [str(c).strip() for c in df.columns]
    code_col = "合约代码"
    df[code_col] = df[code_col].astype(str).str.strip()
    df = df[~df[code_col].isin(["小计", "合计"])]
    want = df[code_col].str.match(r"^(T|TF|TS|TL)\d{4}$")
    df = df[want].copy()
    if df.empty:
        return None
    df["date"] = date
    df["variety"] = df[code_col].str.extract(r"^([A-Za-z]+)")[0]
    keep = {"合约代码": "symbol", "今开盘": "open", "最高价": "high", "最低价": "low",
            "今收盘": "close", "成交量": "volume", "今结算": "settle",
            "前结算": "pre_settle", "持仓量": "open_interest",
            "成交金额": "turnover", "持仓变化": "oi_change"}
    df = df.rename(columns=keep)
    cols = ["symbol", "date", "open", "high", "low", "close", "volume",
            "open_interest", "turnover", "settle", "pre_settle", "variety"]
    return df[[c for c in cols if c in df.columns]]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dates", help="逗号分隔 yyyymmdd")
    ap.add_argument("--start")
    ap.add_argument("--end")
    args = ap.parse_args()

    if args.dates:
        dates = [d.strip() for d in args.dates.split(",") if d.strip()]
    else:
        dates = [d.strftime("%Y%m%d") for d in
                 pd.bdate_range(args.start, args.end)]

    frames = []
    for i, d in enumerate(dates):
        try:
            df = parse_one(d)
            if df is not None:
                frames.append(df)
        except Exception as e:  # noqa: BLE001
            print(f"  {d} FAIL {e!r}")
        time.sleep(0.1)
        if (i + 1) % 100 == 0:
            print(f"  ...{d} 已抓 {len(frames)} 天")

    if not frames:
        print("没有数据")
        return
    out = pd.concat(frames, ignore_index=True)
    out = out.sort_values(["date", "symbol"]).reset_index(drop=True)
    tag = f"{dates[0]}_{dates[-1]}" if len(dates) > 1 else dates[0]
    path = RAW / f"cffex_gzqh_{tag}.csv"
    out.to_csv(path, index=False, encoding="utf-8-sig")
    print(f"行数 {len(out):,}  日期 {out['date'].min()} → {out['date'].max()}  "
          f"品种 {sorted(out['variety'].unique())}")
    print(out.head(6).to_string())
    print(out.tail(3).to_string())
    print(f"已写 {path}")


if __name__ == "__main__":
    main()
