"""离线核对资金合同、参考期余额、发布时点及新任务准入；不训练或试目标。"""

import argparse
import hashlib
import json
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urljoin

import pandas as pd
from audit_funding_announcements import read, verified
from bs4 import BeautifulSoup
from fetch_funding_announcements import dump
from fetch_funding_contracts import BASE, CHANNELS, END

from ficc.data.funding_announcements import available_session, calendar_sessions
from ficc.data.funding_contracts import (
    monthly_coverage,
    paired_treasury,
    parse_mlf,
    parse_outright,
    parse_treasury,
)
from ficc.data.loader import load_funding_series

PLAN = Path("config/baselines/funding_contract_audit.json")


def source_records(root):
    """目录原响应与清单逐行核对，再验证正文散列；解析失败即停止。"""
    records, fingerprints, counts = [], [], {}
    for channel, (folder, module, start) in CHANNELS.items():
        manifest = read(root / channel / "index.json")
        if (manifest["start"], manifest["end"]) != (start, END):
            raise ValueError("collection scope differs")
        if not manifest["pages"] or manifest["pages"] != list(range(1, len(manifest["pages"]) + 1)):
            raise ValueError("missing index page")
        rows = []
        for page in manifest["pages"]:
            path = root / channel / "index" / f"{page}.html"
            body, meta = verified(path, BASE + folder + f"{module}-{page}.html")
            fingerprints.append(dict(path=str(path.relative_to(root)), sha256=meta["sha256"]))
            soup = BeautifulSoup(body, "html.parser")
            page_rows = [
                dict(
                    title=a.get_text(strip=True),
                    date=a.parent.parent.get_text(" ", strip=True)[-10:],
                    url=urljoin(BASE, a["href"]),
                )
                for a in soup.select("a[istitle]")
            ]
            if not page_rows:
                raise ValueError("empty index page")
            rows.extend(r for r in page_rows if start <= r["date"] <= END)
        last_meta = soup.select_one("input[name=article_paging_list_hidden]")
        if max(r["date"] for r in page_rows) >= start and page < int(last_meta["totalpage"]):
            raise ValueError("truncated index pagination")
        if rows != manifest["rows"] or len({r["url"] for r in rows}) != len(rows):
            raise ValueError("source index snapshot differs or overlaps")
        counts[channel] = len(rows)
        for row in rows:
            path = root / channel / "articles" / (row["url"].split("/")[-2] + ".html")
            body, meta = verified(path, row["url"])
            fingerprints.append(dict(path=str(path.relative_to(root)), sha256=meta["sha256"]))
            if channel.startswith("mlf"):
                parsed = parse_mlf(body, row, table=channel == "mlf_table")
            elif channel == "treasury":
                parsed = parse_treasury(body, row)
            else:
                parsed = parse_outright(body, row)
            records.append(
                dict(
                    parsed,
                    channel=channel,
                    sha256=meta["sha256"],
                    retrieved_at=meta["retrieved_at"],
                )
            )
    return records, fingerprints, counts


def summarize(records, scope):
    """各工具按自身业务期间统计，不把共月数或同日多期当新增市场样本。"""
    start, end = scope
    table = monthly_coverage(
        [r for r in records if r["channel"] == "mlf_table"], "2018-11", "2025-05"
    )
    news = [
        r
        for r in records
        if r["channel"] == "mlf_news"
        and r["kind"] != "planned_tender"
        and "2018-11" <= r["reference_month"] <= "2025-05"
    ]
    by_month = {r["reference_month"]: r for r in table}
    comparable = 0
    for row in news:
        if row["balance_100m"] != by_month[row["reference_month"]]["balance_100m"]:
            raise ValueError("MLF table and operation report amounts differ")
        comparable += 1
    # 数值一致不代表参考时点相同：操作后余额单独统计，不升级成月末 vintage。
    mlf_plans = [
        r
        for r in records
        if r["instrument"] == "mlf"
        and r["kind"] == "planned_tender"
        and start <= r["event_date"] <= end
    ]
    release_lags = [
        (
            pd.Timestamp(r["published_at"]).tz_localize(None).normalize()
            - pd.Period(r["reference_month"], freq="M").end_time.normalize()
        ).days
        for r in table
    ]
    pairs = paired_treasury([r for r in records if r["instrument"] == "central_treasury_deposit"])
    selected = [(p, a) for p, a in pairs if start <= a["stated_value_date"] <= end]
    leads = [
        (
            pd.Timestamp(p["stated_value_date"])
            - pd.Timestamp(p["published_at"]).tz_localize(None).normalize()
        ).days
        for p, _ in selected
    ]
    outright = monthly_coverage(
        [
            r
            for r in records
            if r["instrument"] == "outright_reverse_repo"
            and r["kind"] == "reported_monthly_operations"
        ],
        "2024-10",
        "2025-05",
    )
    return dict(
        mlf=dict(
            expected_months=79,
            verified_months=len(table),
            reference_period=["2018-11", "2025-05"],
            cross_report_equal_amounts=comparable,
            post_operation_not_month_end=sum(
                r["kind"] == "reported_post_operation_balance" for r in news
            ),
            table_publications_before_calendar_month_end=sum(x < 0 for x in release_lags),
            monthly_table_observations_available_by_scope_end=sum(
                r.get("available_session") is not None and r["available_session"] <= end
                for r in table
            ),
            advance_plans_in_scope=len(mlf_plans),
            monthly_release_lag_calendar_days=[min(release_lags), max(release_lags)],
            daily_actual_stock_verified=False,
            explicit_value_maturity_dates_verified=0,
            availability="Last published monthly observation only, with reference month and age. Post-operation reports have a different reference instant. Plans are not expectations or actual awards.",
        ),
        treasury=dict(
            paired_contracts=len(pairs),
            unique_value_dates=len({a["stated_value_date"] for _, a in pairs}),
            contracts_in_scope=len(selected),
            unique_value_dates_in_scope=len({a["stated_value_date"] for _, a in selected}),
            plan_lead_calendar_days=[min(leads), max(leads)],
            holiday_adjustment_clauses=sum(p["holiday_adjustment_clause"] for p, _ in pairs),
            actual_cash_settlements_verified=0,
            opening_stock_verified=False,
            by_year={
                str(y): sum(a["year"] == y for _, a in pairs)
                for y in sorted({a["year"] for _, a in pairs})
            },
            availability="Matched planned and awarded contractual dates; not proof of cash transfer. Contract issue sequences are contiguous within observed years; no all-government-liquidity coverage claim.",
        ),
        outright=dict(
            monthly_reports=len(outright),
            advance_plans_in_scope=sum(
                r["instrument"] == "outright_reverse_repo"
                and r["kind"] == "planned_tender"
                and start <= r["event_date"] <= end
                for r in records
            ),
            advance_plans_outside_scope=sum(
                r["instrument"] == "outright_reverse_repo"
                and r["kind"] == "planned_tender"
                and r["event_date"] > end
                for r in records
            ),
            daily_actual_stock_verified=False,
            explicit_value_maturity_dates_verified=0,
        ),
    )


def target_coverage(frame, sessions, scope):
    """核查原始观测覆盖，不填充资金目标；FR/FDR 不改名为 R/DR。"""
    frame = frame.copy()
    frame["date"] = pd.to_datetime(frame["date"], errors="raise")
    if frame["date"].duplicated().any():
        raise ValueError("duplicate funding observation date")
    days = pd.to_datetime([d for d in sessions if scope[0] <= d <= scope[1]])
    selected = frame.set_index("date").reindex(days)
    columns = [c for c in ("FDR001", "FDR007", "FR007", "DR001", "DR007", "R007") if c in selected]
    digest = hashlib.sha256(
        pd.util.hash_pandas_object(selected[columns], index=True).to_numpy().tobytes()
    ).hexdigest()
    return dict(
        expected_sessions=len(days),
        observed={c: int(selected[c].notna().sum()) for c in columns},
        missing_dates={
            c: selected.index[selected[c].isna()].strftime("%Y-%m-%d").tolist() for c in columns
        },
        missing_columns=[c for c in ("DR001", "DR007", "R007") if c not in selected],
        scope_sha256=digest,
        definition="FDR/FR: morning fixing, not full-session volume-weighted DR/R. No target imputation. Provider APIs exist; their full historical coverage, units, release timestamps and access have not yet been audited.",
    )


def audit(root, plan):
    if plan["scope"] != ["2018-12-01", "2025-05-30"] or plan["publication_collection_end"] != END:
        raise ValueError("unregistered audit scope")
    records, fingerprints, counts = source_records(root)
    calendar_path = Path(plan["calendar_path"])
    if hashlib.sha256(calendar_path.read_bytes()).hexdigest() != plan["calendar_sha256"]:
        raise ValueError("independent calendar changed")
    sessions = calendar_sessions(read(calendar_path))
    for rule in plan["rules"]:
        _, meta = verified(root / rule["cache"], rule["url"], rule["sha256"])
        fingerprints.append(dict(path=rule["cache"], sha256=meta["sha256"]))
    digest = hashlib.sha256(
        json.dumps(sorted(fingerprints, key=lambda r: r["path"]), sort_keys=True).encode()
    ).hexdigest()
    if digest != plan["raw_snapshot_sha256"]:
        raise ValueError("frozen contract sources changed")
    for r in records:
        # 2018 年暖机合同保留真实时间，不能把日历开始以前的发布强塞进首日。
        day = r["published_at"][:10]
        r["available_session"] = (
            available_session(r["published_at"], sessions)
            if sessions[0] <= day < sessions[-2]
            else None
        )
        r["extra_lag_session"] = (
            available_session(r["published_at"], sessions, 1) if r["available_session"] else None
        )
    summary = summarize(records, plan["scope"])
    # 复用已经冻结的普通公告审计原产物；核对散列，而不是重新跑旧预测。
    ordinary_path = Path(plan["ordinary_records_path"])
    if hashlib.sha256(ordinary_path.read_bytes()).hexdigest() != plan["ordinary_records_sha256"]:
        raise ValueError("ordinary operation audit changed")
    nominal = []
    for r in read(ordinary_path):
        for op in r["operations"]:
            if op["instrument"] == "reverse_repo":
                if (
                    op["tenor_unit"] != "天"
                    or op["value_date"] is not None
                    or op["maturity_date"] is not None
                ):
                    raise ValueError("ordinary contract coverage changed; review audit")
                nominal.append(
                    (
                        date.fromisoformat(r["business_date"])
                        + timedelta(days=int(op["tenor_value"]))
                    ).isoformat()
                )
    if any(d < sessions[0] or d > sessions[-1] for d in nominal):
        raise ValueError("calendar does not cover nominal-date diagnostic")
    target = target_coverage(load_funding_series(), sessions, plan["scope"])
    if target["scope_sha256"] != plan["target_scope_sha256"]:
        raise ValueError("frozen funding target snapshot changed")
    summary.update(
        version="funding-contract-audit-v1",
        scope=plan["scope"],
        archived_articles=sum(counts.values()),
        channel_counts=counts,
        raw_snapshot_sha256=digest,
        calendar_sha256=plan["calendar_sha256"],
        ordinary_reverse_repo=dict(
            rows=len(nominal),
            value_dates_verified=0,
            maturity_dates_verified=0,
            nominal_date_plus_tenor_non_sessions=sum(d not in sessions for d in nominal),
            nominal_dates_are_not_actual_maturity_dates=True,
            net_injection_and_stock_admitted=False,
        ),
        target=target,
        expectations=plan["expectations"],
        decision=plan["decision"],
        availability="Displayed source publication time; next independent session, plus one-session sensitivity. Current cached pages do not establish immutable historical vintages. No interpolation or zero filling of unknown operations, stock, expectations or targets.",
        sources=plan["rules"],
    )
    dump(root / "audited_records.json", records)
    dump(root / "audit.json", summary)
    return summary


if __name__ == "__main__":
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--root", type=Path, default=Path("datasets/funding-contract-v1"))
    p.add_argument("--plan", type=Path, default=PLAN)
    p.add_argument("--report", type=Path, default=Path("reports/funding_contract_coverage.json"))
    args = p.parse_args()
    result = audit(args.root, read(args.plan))
    dump(args.report, result)
    print(
        json.dumps(
            {
                k: result[k]
                for k in (
                    "archived_articles",
                    "mlf",
                    "treasury",
                    "outright",
                    "ordinary_reverse_repo",
                    "target",
                    "decision",
                )
            },
            ensure_ascii=False,
            indent=2,
        )
    )
