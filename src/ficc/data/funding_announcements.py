"""央行公告的严格字段解析与因果可用时点；缺失不等于零操作。"""

from __future__ import annotations

import hashlib
import re
from datetime import date

import numpy as np
import pandas as pd
from bs4 import BeautifulSoup


def compact(text: str) -> str:
    """移除排版空格并统一全角符号，保留业务语义。"""
    return re.sub(r"\s+", "", text).translate(str.maketrans("，％（）", ",%()"))


def chinese_integer(value: str) -> int:
    """解析署名日期中的中文整数，不推定没有出现的日期。"""
    digits = dict(zip("零〇一二三四五六七八九", (0, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9), strict=True))
    if "十" in value:
        a, b = value.split("十")
        return (digits[a] if a else 1) * 10 + (digits[b] if b else 0)
    return int("".join(str(digits[c]) for c in value))


def signature_date(text: str) -> str | None:
    """以署名日期独立核对业务日期。"""
    match = re.search(
        r"([〇零一二三四五六七八九]{4})年([一二三四五六七八九十]+)月([一二三四五六七八九十]+)日",
        text,
    )
    if match:
        return date(*(chinese_integer(v) for v in match.groups())).isoformat()
    return None


def parse_announcement(body: bytes, url: str, index_date: str) -> dict:
    """解析原始公告：期限不自动转换成到期日，不混合逆回购与 MLF。"""
    soup = BeautifulSoup(body, "html.parser")
    content, title, toolbar = (
        soup.select_one(s)
        for s in (
            "#ewebeditor_content, #zoom",
            ".title-heading, h2",
            ".article-a-toolbar, #shijian",
        )
    )
    if any(v is None for v in (content, title, toolbar)):
        raise ValueError("missing official article structure")
    text = compact(content.get_text("", strip=True))
    title_text = compact(title.get_text("", strip=True))
    timestamp = re.search(
        r"\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}(?::\d{2})?", toolbar.get_text(" ", strip=True)
    )
    if not timestamp or timestamp[0][:10] != index_date:
        raise ValueError("index/article publication date mismatch or missing timestamp")
    published = pd.Timestamp(timestamp[0], tz="Asia/Shanghai").isoformat()
    result = dict(
        url=url,
        title=title_text,
        index_date=index_date,
        displayed_published_at=published,
        raw_sha256=hashlib.sha256(body).hexdigest(),
        body_sha256=hashlib.sha256(text.encode()).hexdigest(),
        operations=[],
        status="not_daily_operation_notice",
    )
    if "公开市场业务交易公告" not in title_text:
        return result
    signed = signature_date(text)
    dates = [
        date(int(y), int(m), int(d)).isoformat()
        for y, m, d in re.findall(r"(\d{4})年(\d{1,2})月(\d{1,2})日", text)
    ]
    business = signed or (dates[-1] if dates else None)
    if business is None:
        raise ValueError("missing independent business date")
    result.update(
        business_date=business, signature_date=signed, explicit_dates=dates, status="unresolved"
    )
    result["other_tool_mentions"] = [name for name in ("MLF", "TMLF", "PSL") if name in text]
    if "央行票据" in text and ("香港" in text or "CMU" in text):
        result["status"] = "offshore_bill_notice"
        return result
    if business > index_date:
        raise ValueError("future operation announcement needs separate event handling")
    zero = bool(
        re.search(r"不(?:开展|进行)(?:公开市场)?(?:逆回购|公开市场操作)", text)
        or "逆回购操作量为零" in text
        or "无逆回购操作" in text
    )
    for table in content.find_all("table"):
        if table.find("table") is not None:
            continue  # 排版外框包含真正业务表，不能重复解析。
        cells = [
            [
                compact(c.get_text("", strip=True))
                for c in tr.find_all(["td", "th"], recursive=False)
            ]
            for tr in table.find_all("tr")
        ]
        if not cells:
            continue
        heading = None
        for p in table.find_all_previous("p"):
            t = compact(p.get_text("", strip=True))
            if "操作情况" in t or "招标情况" in t:
                heading = t
                break
        if heading is None:
            raise ValueError("unidentified operation table")
        instrument = (
            "reverse_repo"
            if "逆回购" in heading and "买断式" not in heading
            else "tmlf"
            if "TMLF" in heading
            else "mlf"
            if "MLF" in heading or "中期借贷便利" in heading
            else "cash_bond_purchase"
            if "现券买断" in heading
            else "other"
        )
        if instrument in {"cash_bond_purchase", "other"}:
            if instrument == "other":
                raise ValueError("unregistered operation table")
            result.setdefault("other_table_headings", []).append(heading)
            continue
        headers = cells[0]

        def column(names: tuple[str, ...], headers=headers) -> int:
            for name in names:
                if name in headers:
                    return headers.index(name)
            raise ValueError(f"missing required column: {names}")

        tenor_i = column(("期限",))
        amount_i = column(("中标量", "操作量"))
        rate_i = column(("操作利率", "中标利率"))
        for values in cells[1:]:
            if len(values) != len(headers):
                raise ValueError("ragged operation table")
            tenor = re.fullmatch(
                r"(\d+)(天|个月|月|年)(\(可展期2次,实际期限为3年\))?", values[tenor_i]
            )
            amount = re.fullmatch(r"([\d,.]+)亿元", values[amount_i])
            rate = re.fullmatch(r"([\d.]+)%", values[rate_i])
            if not all((tenor, amount, rate)):
                raise ValueError("unrecognized amount, unit, tenor or rate")
            number, interest = float(amount[1].replace(",", "")), float(rate[1])
            if not np.isfinite([number, interest]).all() or number <= 0 or not 0 <= interest < 20:
                raise ValueError("invalid operation values")
            result["operations"].append(
                dict(
                    instrument=instrument,
                    tenor_value=int(tenor[1]),
                    tenor_unit=tenor[2],
                    tenor_raw=values[tenor_i],
                    amount_100m_yuan=number,
                    rate_pct=interest,
                    maturity_date=None,
                    maturity_status="not_explicitly_verified",
                    value_date=None,
                )
            )
    rr = [o for o in result["operations"] if o["instrument"] == "reverse_repo"]
    if rr and zero:
        raise ValueError("contradictory positive and zero reverse-repo statements")
    result["status"] = (
        "positive_operation"
        if rr
        else "explicit_zero"
        if zero
        else "other_operation_only"
        if result.get("other_table_headings")
        else "unresolved"
    )
    result["reverse_repo_amount_100m_yuan"] = (
        sum(o["amount_100m_yuan"] for o in rr) if rr else 0.0 if zero else None
    )
    result["maturity_mentions"] = re.findall(r"[^。；]{0,90}到期[^。；]{0,90}", text)
    result["revision_flag"] = any(w in title_text for w in ("更正", "修订", "勘误"))
    return result


def available_session(published: str, sessions: list[str], extra_lag: int = 0) -> str:
    """公告显示发布日期之后第一个银行间交易日，再加指定延迟。"""
    if extra_lag < 0:
        raise ValueError("negative publication lag")
    if sessions != sorted(set(sessions)):
        raise ValueError("invalid independent session calendar")
    day = pd.Timestamp(published).tz_convert("Asia/Shanghai").date().isoformat()
    pos = int(np.searchsorted(sessions, day, side="right")) + extra_lag
    if pos >= len(sessions):
        raise ValueError("calendar does not cover publication availability")
    return sessions[pos]


def calendar_sessions(config: dict) -> list[str]:
    """从独立年历及修订重建日期；公告或曲线缺行不能缩短日历。"""
    days = pd.date_range(config["start"], config["end"])
    sessions = {d.date().isoformat() for d in days if d.weekday() < 5}
    for rule in config["annual_notices"] + config["amendments"]:
        sessions.difference_update(rule["closed"])
        sessions.update(rule["opened"])
    result = sorted(d for d in sessions if config["start"] <= d <= config["end"])
    if result != config["sessions"]:
        raise ValueError("materialized independent calendar differs from source rules")
    return result


def coverage_rows(records: list[dict], calendar: dict, start: str, end: str) -> list[dict]:
    """覆盖失败保留 null；特殊现金开市日不推断为央行零操作。"""
    sessions = calendar_sessions(calendar)
    expected = [d for d in sessions if start <= d <= end]
    special = set(calendar["special_cash_sessions"])
    by_day = {}
    for r in records:
        if r["status"] not in {"positive_operation", "explicit_zero"}:
            continue
        day = r["business_date"]
        if day not in expected or day in by_day:
            raise ValueError("unexpected or duplicate reverse-repo business date")
        by_day[day] = r
    result = []
    for day in expected:
        record = by_day.get(day)
        if record:
            result.append(
                dict(
                    date=day,
                    status=record["status"],
                    amount=record["reverse_repo_amount_100m_yuan"],
                    url=record["url"],
                )
            )
        else:
            result.append(
                dict(
                    date=day,
                    status="special_cash_session_no_scheduled_notice"
                    if day in special
                    else "missing",
                    amount=None,
                    url=None,
                )
            )
    return result


def funding_features(
    records: list[dict],
    cutoffs: list[str],
    calendar: dict,
    start: str,
    end: str,
    *,
    extra_lag: int = 0,
) -> pd.DataFrame:
    """仅使用截止日已可用的公告，最近五个例行公告日；不推导净投放。"""
    coverage = coverage_rows(records, calendar, start, end)
    if any(r["status"] == "missing" for r in coverage):
        raise ValueError("unresolved announcement coverage; cannot construct features")
    sessions = calendar_sessions(calendar)
    events = sorted(
        (
            dict(r, available=available_session(r["displayed_published_at"], sessions, extra_lag))
            for r in records
            if r["status"] in {"positive_operation", "explicit_zero"}
        ),
        key=lambda r: (r["available"], r["business_date"]),
    )
    rows = []
    for cutoff in cutoffs:
        if not start <= cutoff <= end:
            raise ValueError("feature cutoff outside audited history")
        known = [r for r in events if r["available"] <= cutoff]
        last = known[-5:]
        sevens = [
            o["rate_pct"]
            for r in known
            for o in r["operations"]
            if o["instrument"] == "reverse_repo"
            and o["tenor_unit"] == "天"
            and o["tenor_value"] == 7
        ]
        if len(last) < 5 or not sevens:
            rows.append([np.nan] * 3)
            continue
        ops = [o for r in last for o in r["operations"] if o["instrument"] == "reverse_repo"]
        if any(o["tenor_unit"] != "天" for o in ops):
            raise ValueError("unsupported reverse-repo tenor unit")
        total = sum(r["reverse_repo_amount_100m_yuan"] for r in last)
        longer = sum(o["amount_100m_yuan"] for o in ops if o["tenor_value"] > 7)
        rows.append([np.log1p(total), sevens[-1], longer / total if total else 0.0])
    return pd.DataFrame(
        rows,
        index=pd.to_datetime(cutoffs),
        columns=["log_gross_last5_notices", "last_7d_rate_pct", "longer_tenor_amount_share_last5"],
    )
