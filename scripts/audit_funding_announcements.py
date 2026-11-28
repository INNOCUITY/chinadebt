"""审计冻结历史公告及独立日历；只有新操作量通过时才允许后续预测。"""

import argparse
import hashlib
import json
from collections import Counter
from pathlib import Path
from urllib.parse import urljoin

from bs4 import BeautifulSoup
from fetch_funding_announcements import BASE, END, START, dump, notice_key

from ficc.data.funding_announcements import (
    available_session,
    calendar_sessions,
    coverage_rows,
    parse_announcement,
)


def read(path):
    return json.loads(path.read_text())


def verified(path, url=None, expected_sha=None):
    body, meta = path.read_bytes(), read(path.with_suffix(path.suffix + ".meta.json"))
    digest = hashlib.sha256(body).hexdigest()
    if (
        digest != meta["sha256"]
        or (url and meta["url"] != url)
        or (expected_sha and digest != expected_sha)
    ):
        raise ValueError("source cache hash or URL differs")
    return body, meta


def audit(root, calendar_path):
    calendar = read(calendar_path)
    sessions = calendar_sessions(calendar)
    sources = []
    for rule in calendar["annual_notices"] + calendar["amendments"] + calendar["evidence"]:
        body, meta = verified(
            root / rule.get("cache", "calendar/" + rule["url"].split("/")[-1]),
            rule["url"],
            rule["sha256"],
        )
        sources.append(dict(url=rule["url"], sha256=meta["sha256"]))
    cfets = read(root / "index_manifest.json")
    primary = read(root / "pbc-index/manifest.json")
    if any((m["start"], m["end"]) != (START, END) for m in (cfets, primary)):
        raise ValueError("frozen collection range differs")
    # 重新解析原目录，防止缓存清单静默删行。
    collected = []
    for page in cfets["pages"]:
        body, _ = verified(
            root / f"index/omo-{page['page']}.json",
            BASE + "/ags/ms/cm-s-notice-query/contents",
            page["sha256"],
        )
        collected.extend(r for r in json.loads(body)["records"] if START <= r["releaseDate"] <= END)
    if collected != cfets["rows"] or len({r["contentId"] for r in collected}) != len(collected):
        raise ValueError("CFETS index snapshot differs or overlaps")
    pbc_rows = []
    base = "https://www.pbc.gov.cn/zhengcehuobisi/125207/125213/125431/125475/"
    for page in primary["pages"]:
        body, _ = verified(root / f"pbc-index/{page}.html", base + f"17081-{page}.html")
        for a in BeautifulSoup(body, "html.parser").select("a[istitle]"):
            day = a.parent.parent.get_text(" ", strip=True)[-10:]
            if START <= day <= END:
                pbc_rows.append(
                    dict(title=a.get_text(strip=True), url=urljoin(base, a["href"]), date=day)
                )
    if pbc_rows != primary["rows"] or len({notice_key(r["title"]) for r in pbc_rows}) != len(
        pbc_rows
    ):
        raise ValueError("primary index snapshot differs or overlaps")
    pbc_map = {notice_key(r["title"]): r for r in pbc_rows}
    records, excluded, seen, cache_manifest = [], [], set(), []
    items = [
        (
            root / f"articles/{r['contentId']}.html",
            urljoin(BASE, r["url"] or r["draftPath"]),
            r["releaseDate"],
            r["title"],
        )
        for r in cfets["rows"]
    ]
    items += [
        (root / "pbc-supplement" / r["filename"], r["url"], r["date"], r["title"])
        for r in read(root / "pbc-supplement/index.json")
    ]
    for path, url, day, title in items:
        body, meta = verified(path, url)
        cache_manifest.append(
            dict(url=url, sha256=meta["sha256"], retrieved_at=meta["retrieved_at"])
        )
        if "公开市场业务交易公告" not in title:
            excluded.append(dict(title=title, url=url, reason="non_daily_notice"))
            continue
        key = notice_key(title)
        if key in seen or key not in pbc_map or pbc_map[key]["date"] != day:
            raise ValueError("cross-source daily notice identity/date differs")
        seen.add(key)
        r = parse_announcement(body, url, day)
        if notice_key(r["title"]) != key or r["status"] == "unresolved" or r.get("revision_flag"):
            raise ValueError("unresolved daily notice")
        r["retrieved_at"] = meta["retrieved_at"]
        r["available_session"] = available_session(r["displayed_published_at"], sessions)
        r["extra_lag_session"] = available_session(r["displayed_published_at"], sessions, 1)
        records.append(r)
    if seen != set(pbc_map):
        raise ValueError("uncollected primary daily notices")
    coverage = coverage_rows(records, calendar, START, END)
    ordinary = [r for r in records if r["status"] in {"positive_operation", "explicit_zero"}]
    ops = [o for r in ordinary for o in r["operations"] if o["instrument"] == "reverse_repo"]
    data_hash = hashlib.sha256(json.dumps(cache_manifest, sort_keys=True).encode()).hexdigest()
    result = dict(
        version="funding-announcement-audit-v1",
        scope=[START, END],
        passed_gross_announcements=not any(r["status"] == "missing" for r in coverage),
        passed_net_injection=False,
        cfets_articles=len(cfets["rows"]),
        primary_daily_notices=len(pbc_rows),
        primary_supplements=len(items) - len(cfets["rows"]),
        total_archived_articles=len(items),
        parsed_status=dict(Counter(r["status"] for r in records)),
        excluded_non_daily=len(excluded),
        coverage_status=dict(Counter(r["status"] for r in coverage)),
        expected_bank_sessions=len(coverage),
        missing_dates=[r["date"] for r in coverage if r["status"] == "missing"],
        special_cash_sessions=[
            r for r in coverage if r["status"] == "special_cash_session_no_scheduled_notice"
        ],
        reverse_repo_rows=len(ops),
        tenor_counts=dict(Counter(o["tenor_raw"] for o in ops)),
        maturity_dates_verified=sum(o["maturity_date"] is not None for o in ops),
        value_dates_verified=sum(o["value_date"] is not None for o in ops),
        publication_business_date_differences=sum(
            r["index_date"] != r["business_date"] for r in ordinary
        ),
        multiline_notices=sum(
            sum(o["instrument"] == "reverse_repo" for o in r["operations"]) > 1 for r in ordinary
        ),
        mixed_tool_zero_notices=sum(
            r["status"] == "explicit_zero" and bool(r["operations"]) for r in ordinary
        ),
        by_year={
            y: dict(Counter(r["status"] for r in coverage if r["date"].startswith(y)))
            for y in sorted({r["date"][:4] for r in coverage})
        },
        raw_manifest_sha256=data_hash,
        calendar_sha256=hashlib.sha256(calendar_path.read_bytes()).hexdigest(),
        sources=sources,
        availability="Next independent interbank session after displayed publication date; additional one-session lag sensitivity. Current page snapshot is not immutable vintage proof.",
        special_days="Statutory holidays with special cash-market opening, not scheduled ordinary OMO working days. No announcement found in either source; amounts remain null, not asserted zero.",
        maturity_limit="Tenor is known; value/maturity dates not explicitly verified. No net injection, maturity schedule or outstanding stock may be inferred.",
    )
    dump(root / "audited_records.json", records)
    dump(root / "audited_coverage.json", coverage)
    dump(root / "raw_manifest.json", cache_manifest)
    dump(root / "audit.json", result)
    return result


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, default=Path("datasets/pbc-omo-v1"))
    p.add_argument("--calendar", type=Path, default=Path("config/baselines/funding_sessions.json"))
    p.add_argument("--report", type=Path, default=Path("reports/funding_data_coverage.json"))
    args = p.parse_args()
    result = audit(args.root, args.calendar)
    dump(args.report, result)
    print(
        json.dumps(
            {
                k: result[k]
                for k in (
                    "passed_gross_announcements",
                    "total_archived_articles",
                    "parsed_status",
                    "coverage_status",
                    "missing_dates",
                    "tenor_counts",
                )
            },
            ensure_ascii=False,
        )
    )
    if not result["passed_gross_announcements"]:
        raise SystemExit(1)
