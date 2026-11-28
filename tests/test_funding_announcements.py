"""资金公告：零/缺失、期限、工具与发布时间边界。"""

import copy
import json
from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from ficc.data.funding_announcements import (
    available_session,
    calendar_sessions,
    coverage_rows,
    funding_features,
    parse_announcement,
)


def article(text, table="", day="2024-01-02", primary=False):
    if primary:
        return f'<h2>公开市场业务交易公告[2024]第1号</h2><div id="shijian">{day} 09:15:10</div><div id="zoom">{text}{table}2024年1月2日</div>'.encode()
    return f'<h2 class="title-heading">公开市场业务交易公告2024年1号</h2><div class="article-a-toolbar">{day} 09:20</div><div id="ewebeditor_content">{text}{table}二〇二四年一月二日</div>'.encode()


def table(heading="逆回购操作情况", tenor="7天", amount="100亿元"):
    return f"<p>{heading}</p><table><tr><td>期限</td><td>投标量</td><td>中标量</td><td>中标利率</td></tr><tr><td>{tenor}</td><td>999亿元</td><td>{amount}</td><td>1.80%</td></tr></table>"


def parse(raw, day="2024-01-02"):
    return parse_announcement(raw, "https://www.pbc.gov.cn/example", day)


def test_positive_awarded_amount_and_no_inferred_maturity():
    r = parse(
        article(
            "",
            "<table><tr><td>"
            + table()
            + table(tenor="14天", amount="200亿元")
            + "</td></tr></table>",
        )
    )
    assert r["reverse_repo_amount_100m_yuan"] == 300
    assert [o["tenor_value"] for o in r["operations"]] == [7, 14]
    assert all(o["maturity_date"] is None and o["value_date"] is None for o in r["operations"])


@pytest.mark.parametrize("phrase", ["不开展逆回购操作", "不进行公开市场操作", "无逆回购操作"])
def test_zero_is_explicit_and_distinct_from_other_tools(phrase):
    r = parse(article(phrase, table("MLF操作情况", "1年", "3000亿元")))
    assert r["status"] == "explicit_zero" and r["reverse_repo_amount_100m_yuan"] == 0
    assert r["operations"][0]["instrument"] == "mlf"
    assert parse(article("今日公告但无可核验操作说明"))["status"] == "unresolved"


def test_tmlf_primary_source_and_contradiction():
    r = parse(
        article(
            "无逆回购操作", table("TMLF操作情况", "1年(可展期2次，实际期限为3年)"), primary=True
        )
    )
    assert r["operations"][0]["instrument"] == "tmlf"
    assert r["displayed_published_at"] == "2024-01-02T09:15:10+08:00"
    with pytest.raises(ValueError, match="contradictory"):
        parse(article("无逆回购操作", table()))
    with pytest.raises(ValueError, match="publication date mismatch"):
        parse(article("", table()), "2024-01-03")
    with pytest.raises(ValueError, match="unrecognized"):
        parse(article("", table(tenor="不详")))


def calendar():
    days = pd.bdate_range("2024-01-02", "2024-01-31").strftime("%Y-%m-%d").tolist()
    return dict(
        start=days[0],
        end=days[-1],
        annual_notices=[],
        amendments=[],
        sessions=days,
        special_cash_sessions=["2024-01-03"],
    )


def records():
    rows = []
    for day in calendar()["sessions"][:-2]:
        r = parse(article("", table()))
        r.update(business_date=day, displayed_published_at=day + "T09:20:00+08:00")
        rows.append(r)
    return rows


def test_missing_day_does_not_shrink_calendar_or_become_zero():
    cal = calendar()
    rs = records()
    end = rs[-1]["business_date"]
    reduced = [r for r in rs if r["business_date"] not in ["2024-01-03", "2024-01-05"]]
    c = {r["date"]: r for r in coverage_rows(reduced, cal, cal["start"], end)}
    assert c["2024-01-03"]["status"] == "special_cash_session_no_scheduled_notice"
    assert c["2024-01-03"]["amount"] is None
    assert c["2024-01-05"]["status"] == "missing" and c["2024-01-05"]["amount"] is None
    with pytest.raises(ValueError, match="coverage"):
        funding_features(reduced, ["2024-01-12"], cal, cal["start"], end)
    with pytest.raises(ValueError, match="duplicate"):
        coverage_rows(rs + rs[:1], cal, cal["start"], end)


def test_future_announcement_perturbation_and_extra_delay():
    cal = calendar()
    rs = records()
    end = rs[-1]["business_date"]
    cut = ["2024-01-12"]
    changed = copy.deepcopy(rs)
    for r in changed:
        if r["business_date"] >= cut[0]:
            r["reverse_repo_amount_100m_yuan"] = 9000
            r["operations"][0]["amount_100m_yuan"] = 9000
            r["operations"][0]["rate_pct"] = 10
    base = funding_features(rs, cut, cal, cal["start"], end)
    np.testing.assert_array_equal(base, funding_features(changed, cut, cal, cal["start"], end))
    assert available_session("2024-01-12T09:00:00+08:00", cal["sessions"]) == "2024-01-15"
    assert available_session("2024-01-12T09:00:00+08:00", cal["sessions"], 1) == "2024-01-16"
    with pytest.raises(ValueError, match="outside audited"):
        funding_features(rs, ["2026-12-01"], cal, cal["start"], end)


def test_independent_calendar_holiday_amendments_and_special_days():
    config = json.loads(Path("config/baselines/funding_sessions.json").read_text())
    days = calendar_sessions(config)
    assert "2019-04-28" in days and "2019-05-02" not in days
    assert "2020-02-01" not in days and "2020-02-03" in days
    assert "2023-12-31" in days and "2023-12-30" not in days
    config["sessions"].remove("2020-02-03")
    with pytest.raises(ValueError, match="source rules"):
        calendar_sessions(config)


def test_cached_source_identity_is_verified_without_network(tmp_path, monkeypatch):
    import hashlib

    import requests
    from fetch_funding_announcements import fetch

    raw = b"public archived response"
    path = tmp_path / "notice.html"
    path.write_bytes(raw)
    url = "https://www.pbc.gov.cn/example"
    meta = dict(url=url, params=None, sha256=hashlib.sha256(raw).hexdigest())
    path.with_suffix(".html.meta.json").write_text(json.dumps(meta))

    def forbidden(*args, **kwargs):
        raise AssertionError("cache verification must not request network")

    monkeypatch.setattr(requests, "get", forbidden)
    assert fetch(path, url) == raw
    path.write_bytes(b"changed")
    with pytest.raises(ValueError, match="identity differs"):
        fetch(path, url)
    with pytest.raises(ValueError, match="unregistered"):
        fetch(path, "https://example.invalid/notice")
