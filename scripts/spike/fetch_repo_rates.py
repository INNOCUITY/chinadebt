#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
脚本名: fetch_repo_rates.py
用途  : 抓取 CFETS 回购定盘利率（FR001/FR007/FR014）与银银间回购定盘利率
        （FDR001/FDR007/FDR014，与 DR007 同源/近似）全历史。
        akshare 的 repo_rate_hist 有“起止日期需在 1 个月内”的硬限制，
        因此本脚本按月分片循环抓取（已验证 2006-01 起 FR 可得）。

数据来源（无需登录）:
    POST https://www.chinamoney.com.cn/ags/ms/cm-u-bk-currency/FrrHis

产出:
    data/raw_spike/repo_frr_fdr_hist.csv
    data/raw_spike/_repo_rate_log.json
"""

import json
import time
from pathlib import Path

import pandas as pd
import akshare as ak

ROOT = Path(__file__).resolve().parents[2]
RAW = ROOT / "data" / "raw_spike"

START_YEAR = 2005
END_YEAR = 2026


def months():
    for y in range(START_YEAR, END_YEAR + 1):
        for m in range(1, 13):
            end_m = m + 1
            end_y = y
            if end_m == 13:
                end_m, end_y = 1, y + 1
            if (y, m) > (2026, 11):
                return
            yield f"{y}{m:02d}01", f"{end_y}{end_m:02d}01"


def main():
    frames, log = [], []
    for i, (s, e) in enumerate(months()):
        try:
            df = ak.repo_rate_hist(start_date=s, end_date=e)
            if df is not None and len(df):
                frames.append(df)
                log.append({"start": s, "end": e, "rows": len(df), "ok": True})
            else:
                log.append({"start": s, "end": e, "rows": 0, "ok": True})
        except Exception as ex:  # noqa: BLE001
            log.append({"start": s, "end": e, "rows": 0, "ok": False,
                        "err": repr(ex)[:200]})
        if i % 24 == 0:
            print(f"  ...{s} 累计 {sum(len(f) for f in frames):,} 行")
        time.sleep(0.15)

    out = pd.concat(frames, ignore_index=True)
    out = out.drop_duplicates(subset=["date"]).sort_values("date").reset_index(drop=True)
    out = out[["date", "FR001", "FR007", "FR014", "FDR001", "FDR007", "FDR014"]]
    out.to_csv(RAW / "repo_frr_fdr_hist.csv", index=False, encoding="utf-8-sig")
    (RAW / "_repo_rate_log.json").write_text(
        json.dumps(log, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\n总行数 {len(out):,}  日期 {out['date'].min()} → {out['date'].max()}")
    for c in ["FR001", "FR007", "FDR001", "FDR007"]:
        s = pd.to_datetime(out["date"]).dt.date
        valid = out[c].notna()
        print(f"  {c}: 非空 {valid.sum():,}/{len(out):,}  "
              f"首个有效 {s[valid].min() if valid.any() else '-'}  "
              f"最后 {s[valid].max() if valid.any() else '-'}")
    print(out.head(3).to_string())
    print(out.tail(3).to_string())


if __name__ == "__main__":
    main()
