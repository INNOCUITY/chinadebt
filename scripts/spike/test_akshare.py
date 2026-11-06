#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
脚本名: test_akshare.py
用途  : 在 .venv-spike（Python 3.13 + akshare 1.20.35）中实测各数据接口，
        记录字段 / 起止日期 / 条数 / 耗时 / 失败原因，并把真实样本落盘到
        data/raw_spike/ak_*.csv（列名保持原样）。

每个用例硬超时 90s（signal.alarm），失败不阻塞后续用例。

运行:
    .venv-spike/bin/python scripts/spike/test_akshare.py
产出:
    data/raw_spike/ak_<name>.csv
    data/raw_spike/_ak_interface_log.json
"""

import json
import signal
import time
import traceback
from pathlib import Path

import pandas as pd
import akshare as ak

ROOT = Path(__file__).resolve().parents[2]
RAW = ROOT / "data" / "raw_spike"
RAW.mkdir(parents=True, exist_ok=True)

PER_CALL_TIMEOUT = 120
RESULTS = []


class Timeout(Exception):
    pass


def _alarm(signum, frame):
    raise Timeout(f"单次调用超过 {PER_CALL_TIMEOUT}s")


def run(name, fn, note="", expect_dates=("date", "日期")):
    """执行一个接口用例，记录结果并落盘 CSV"""
    rec = {"name": name, "ok": False, "note": note, "rows": 0, "cols": 0,
           "columns": [], "date_min": None, "date_max": None,
           "elapsed_s": None, "error": None}
    signal.signal(signal.SIGALRM, _alarm)
    signal.alarm(PER_CALL_TIMEOUT)
    t0 = time.time()
    try:
        df = fn()
        signal.alarm(0)
        rec["elapsed_s"] = round(time.time() - t0, 2)
        if df is None or len(df) == 0:
            rec["error"] = "返回空 DataFrame"
            RESULTS.append(rec)
            print(f"[{name}] EMPTY  ({rec['elapsed_s']}s)")
            return None
        df.to_csv(RAW / f"ak_{name}.csv", index=False, encoding="utf-8-sig")
        rec.update(ok=True, rows=len(df), cols=len(df.columns),
                   columns=[str(c) for c in df.columns])
        for c in expect_dates:
            if c in df.columns:
                s = pd.to_datetime(df[c], errors="coerce").dropna()
                if len(s):
                    rec["date_min"], rec["date_max"] = str(s.min().date()), str(s.max().date())
                    break
        RESULTS.append(rec)
        print(f"[{name}] OK rows={len(df):,} cols={len(df.columns)} "
              f"dates={rec['date_min']}..{rec['date_max']} ({rec['elapsed_s']}s)")
        print(df.head(3).to_string())
        return df
    except Exception as e:  # noqa: BLE001
        signal.alarm(0)
        rec["elapsed_s"] = round(time.time() - t0, 2)
        rec["error"] = f"{type(e).__name__}: {e}"[:500]
        rec["trace"] = traceback.format_exc()[-800:]
        RESULTS.append(rec)
        print(f"[{name}] FAIL ({rec['elapsed_s']}s): {rec['error']}")
        return None


# ---------------------------------------------------------------- 用例
def t_bond_china_yield_short():
    return ak.bond_china_yield(start_date="20250102", end_date="20250110")


def t_bond_china_yield_long():
    return ak.bond_china_yield(start_date="20150101", end_date="20261105")


def t_zh_us_rate():
    return ak.bond_zh_us_rate(start_date="19901219")


def t_close_return():
    return ak.bond_china_close_return(symbol="国债", period="1",
                                      start_date="20240101", end_date="20240131")


def t_close_return_map():
    return ak.bond_china_close_return_map()


def t_repo_rate_hist():
    return ak.repo_rate_hist(start_date="20200101", end_date="20261231")


def t_treasury_index():
    return ak.bond_treasury_index_cbond(indicator="财富", period="5Y")


def t_composite_index():
    return ak.bond_composite_index_cbond(indicator="财富", period="总值")


def t_exchange_daily_10y():
    # 22附息国债? 上交所国债现券；用一只长期活跃券验证交易所现券收盘价
    return ak.bond_zh_hs_daily(symbol="sh019547")


def t_exchange_spot():
    return ak.bond_zh_hs_spot()


def t_cffex_hist():
    return ak.futures_hist_daily_cffex(date="20261105")


def t_cffex_daily():
    return ak.futures_daily_cffex(date="20261105")


def t_futures_zh_spot():
    return ak.futures_zh_spot(symbol="T0", market="CF", adjust="0")


def t_rate_interbank():
    return ak.rate_interbank(market="上海银行同业拆借市场", symbol="Shibor人民币",
                             indicator="隔夜")


CASES = [
    ("bond_china_yield_2025-01", t_bond_china_yield_short, "中债收益率曲线，短区间"),
    ("bond_china_yield_2015-2026", t_bond_china_yield_long, "中债收益率曲线，2015→今，全历史"),
    ("bond_zh_us_rate", t_zh_us_rate, "东财-中美国债收益率"),
    ("bond_china_close_return", t_close_return, "中债收盘收益率曲线"),
    ("bond_china_close_return_map", t_close_return_map, "收盘收益率曲线品种映射"),
    ("repo_rate_hist", t_repo_rate_hist, "CFETS 回购定盘利率(FR/DR)"),
    ("bond_treasury_index_cbond", t_treasury_index, "中债-国债指数(财富,5Y)"),
    ("bond_composite_index_cbond", t_composite_index, "中债-综合指数(财富)"),
    ("bond_zh_hs_daily_019547", t_exchange_daily_10y, "交易所国债现券日行情(新浪)"),
    ("bond_zh_hs_spot", t_exchange_spot, "交易所债券实时行情(新浪)"),
    ("futures_hist_daily_cffex", t_cffex_hist, "中金所日交易数据"),
    ("futures_daily_cffex", t_cffex_daily, "中金所日交易数据(旧)"),
    ("futures_zh_spot_T0", t_futures_zh_spot, "国债期货 T 主力实时(新浪)"),
    ("rate_interbank", t_rate_interbank, "Shibor 隔夜"),
]


if __name__ == "__main__":
    for name, fn, note in CASES:
        print("=" * 70)
        try:
            run(name, fn, note)
        except Exception:  # noqa: BLE001
            traceback.print_exc()
    (RAW / "_ak_interface_log.json").write_text(
        json.dumps(RESULTS, ensure_ascii=False, indent=2), encoding="utf-8")
    ok = sum(1 for r in RESULTS if r["ok"])
    print("=" * 70)
    print(f"接口实测: {ok}/{len(RESULTS)} 成功")
    for r in RESULTS:
        flag = "OK  " if r["ok"] else "FAIL"
        print(f"  {flag} {r['name']:<32} rows={r['rows']:>7,} "
              f"{r['date_min'] or '-'}..{r['date_max'] or '-'} {r['error'] or ''}")
