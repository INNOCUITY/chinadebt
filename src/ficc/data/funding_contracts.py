"""资金合同与余额披露：约定日期、实际成交和事前信息分别记录。"""

from __future__ import annotations

import re
from datetime import date

import pandas as pd
from bs4 import BeautifulSoup

from ficc.data.funding_announcements import chinese_integer, compact


def official_article(body: bytes, index: dict) -> tuple[dict, BeautifulSoup]:
    """核对原站标题及显示发布时间，不以采集日替代历史发布时间。"""
    soup = BeautifulSoup(body, "html.parser")
    title, clock, content = (soup.select_one(s) for s in ("h2", "#shijian", "#zoom"))
    if any(x is None for x in (title, clock, content)):
        raise ValueError("missing official structure")
    title_text = compact(title.get_text("", strip=True))
    if title_text != compact(index["title"]):
        raise ValueError("index title differs from article")
    m = re.search(r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2}", clock.get_text(" ", strip=True))
    if not m or m[0][:10] != index["date"]:
        raise ValueError("missing or conflicting publication timestamp")
    return dict(
        title=title_text,
        url=index["url"],
        published_at=pd.Timestamp(m[0], tz="Asia/Shanghai").isoformat(),
    ), content


def month(title: str) -> str:
    """抽取明确标注的参考月份，不用网页发布日期代替。"""
    m = re.search(r"(\d{4})年(\d{1,2})月", compact(title))
    if not m:
        raise ValueError("missing reference month")
    return f"{int(m[1]):04d}-{int(m[2]):02d}"


def explicit_date(value: str) -> str:
    """仅解析完整日期，不计算或顺延合同到期日。"""
    m = re.fullmatch(r"(\d{4})年(\d{1,2})月(\d{1,2})日", compact(value))
    if not m:
        raise ValueError("date not explicitly stated")
    return date(*(int(v) for v in m.groups())).isoformat()


def amount(value: str) -> float:
    """金额必须明确以亿元计量，缺失和横线不转换为零。"""
    m = re.fullmatch(r"(\d+(?:\.\d+)?)亿元", compact(value))
    if not m:
        raise ValueError("missing amount or unsupported unit")
    return float(m[1])


def parse_mlf(body: bytes, index: dict, *, table: bool) -> dict:
    """MLF 月报余额是已披露的月度观察，不补造成逐日实际存量。"""
    result, content = official_article(body, index)
    text = compact(content.get_text("", strip=True))
    result.update(instrument="mlf", reference_month=month(result["title"]))
    if "招标公告" in result["title"]:
        m = re.search(r"(\d{4}年\d{1,2}月\d{1,2}日).*?(\d+(?:\.\d+)?亿元)MLF操作", text)
        if not m:
            raise ValueError("unparsed forward MLF tender")
        if explicit_date(m[1])[:7] != result["reference_month"]:
            raise ValueError("MLF plan reference month differs from event")
        result.update(
            kind="planned_tender",
            event_date=explicit_date(m[1]),
            planned_amount_100m=amount(m[2]),
            actual_amount_100m=None,
            expected_amount_100m=None,
        )
        return result
    if table:
        m = re.fullmatch(
            r"时间中期借贷便利余额\(单位：亿元\)(\d{4}年\d{1,2}月)(\d+(?:\.\d+)?)", text
        )
        if not m or month(m[1]) != result["reference_month"]:
            raise ValueError("unparsed monthly balance table")
        balance = float(m[2])
        flow = None
    else:
        m = re.search(r"(?:期末|\d{1,2}月末|操作后,)中期借贷便利余额为(\d+(?:\.\d+)?)亿元", text)
        if not m:
            raise ValueError("unparsed monthly balance report")
        balance = float(m[1])
        m = re.search(r"开展中期借贷便利操作(?:共)?(\d+(?:\.\d+)?)亿元", text)
        flow = float(m[1]) if m else 0.0 if "未对金融机构开展中期借贷便利操作" in text else None
    kind = (
        "reported_post_operation_balance"
        if not table and "操作后," in text
        else "reported_monthly_balance"
    )
    if kind == "reported_post_operation_balance":
        m = re.search(
            r"(\d{4}年\d{1,2}月\d{1,2}日),人民银行开展(\d+(?:\.\d+)?亿元)中期借贷便利", text
        )
        if not m:
            raise ValueError("missing post-operation reference date")
        result["reported_operation_date"] = explicit_date(m[1])
        if result["reported_operation_date"][:7] != result["reference_month"]:
            raise ValueError("MLF balance reference month differs from operation")
        flow = amount(m[2])
    result.update(
        kind=kind,
        balance_100m=balance,
        reported_operation_100m=flow,
        explicit_maturity_date=None,
        explicit_value_date=None,
    )
    return result


def parse_treasury(body: bytes, index: dict) -> dict:
    """分别提取事前计划和中标结果；结果日期不反向回填事前记录。"""
    result, content = official_article(body, index)
    text = compact(content.get_text("", strip=True))
    key = re.fullmatch(
        r"(\d{4})年中央国库现金管理商业银行定期存款\(([一二三四五六七八九十]+)期\)招投标(通知|结果)",
        result["title"],
    )
    if not key:
        raise ValueError("unregistered treasury title")
    result.update(
        instrument="central_treasury_deposit",
        year=int(key[1]),
        issue=chinese_integer(key[2]),
        kind="planned_tender" if key[3] == "通知" else "awarded_contract",
        actual_cash_settlement_verified=False,
        holiday_adjustment_clause="遇节假日顺延" in text,
    )
    if key[3] == "通知":
        a = re.search(r"本期操作量(\d+(?:\.\d+)?亿元)", text)
        v = re.search(r"起息日为(\d{4}年\d{1,2}月\d{1,2}日)", text)
        m = re.search(r"到期日为(\d{4}年\d{1,2}月\d{1,2}日)", text)
        if not all((a, v, m)):
            raise ValueError("unparsed planned treasury terms")
        result.update(
            planned_amount_100m=amount(a[1]),
            actual_amount_100m=None,
            rate_pct=None,
            stated_value_date=explicit_date(v[1]),
            stated_maturity_date=explicit_date(m[1]),
        )
    else:
        tables = [t for t in content.find_all("table") if t.find("table") is None]
        values = []
        for table in tables:
            cells = [
                [
                    compact(c.get_text("", strip=True))
                    for c in tr.find_all(["td", "th"], recursive=False)
                ]
                for tr in table.find_all("tr")
            ]
            if cells and cells[0] == ["名称", "中标总量", "起息日", "到期日", "中标利率"]:
                values.extend(cells[1:])
        if len(values) != 1 or len(values[0]) != 5:
            raise ValueError("unparsed treasury result table")
        v = values[0]
        if v[0] != result["title"].removesuffix("招投标结果"):
            raise ValueError("treasury contract identity mismatch")
        rate = re.fullmatch(r"(\d+(?:\.\d+)?)%", v[4])
        if not rate:
            raise ValueError("missing awarded rate")
        result.update(
            actual_amount_100m=amount(v[1]),
            planned_amount_100m=None,
            stated_value_date=explicit_date(v[2]),
            stated_maturity_date=explicit_date(v[3]),
            rate_pct=float(rate[1]),
        )
    if result["stated_maturity_date"] <= result["stated_value_date"]:
        raise ValueError("invalid contractual dates")
    return result


def asof_record(records: list[dict], cutoff: str, kind: str) -> dict | None:
    """只能选择截止时点已发布的同类记录；计划不是实际，缺失不是零。"""
    deadline = pd.Timestamp(cutoff)
    if deadline.tz is None:
        raise ValueError("cutoff requires timezone")
    if len({r["instrument"] for r in records}) > 1:
        raise ValueError("select a single instrument before asof lookup")
    if len({(r.get("year"), r.get("issue")) for r in records}) > 1:
        raise ValueError("select a single contract before asof lookup")
    known = []
    for r in records:
        published = pd.Timestamp(r["published_at"])
        if published.tz is None:
            raise ValueError("publication requires timezone")
        available = r.get("available_session")
        if (
            r["kind"] == kind
            and available
            and published <= deadline
            and pd.Timestamp(available, tz="Asia/Shanghai") <= deadline
        ):
            known.append(r)
    return max(known, key=lambda r: pd.Timestamp(r["published_at"])) if known else None


def monthly_coverage(records: list[dict], start: str, end: str) -> list[dict]:
    """按独立月份检查覆盖；不从已经抓到的月份定义期待范围。"""
    expected = [str(m) for m in pd.period_range(start, end, freq="M")]
    selected = [r for r in records if start <= r["reference_month"] <= end]
    keys = [r["reference_month"] for r in selected]
    if len(keys) != len(set(keys)) or set(keys) != set(expected):
        raise ValueError("missing or duplicate reference month")
    return sorted(selected, key=lambda r: r["reference_month"])


def paired_treasury(records: list[dict]) -> list[tuple[dict, dict]]:
    """核对逐期计划与中标合同；不因金额相同而混淆同日多期。"""
    pairs = {}
    for row in records:
        key = (row["year"], row["issue"])
        group = pairs.setdefault(key, {})
        if row["kind"] in group:
            raise ValueError("duplicate treasury contract record")
        group[row["kind"]] = row
    result = []
    for _key, group in sorted(pairs.items()):
        if set(group) != {"planned_tender", "awarded_contract"}:
            raise ValueError("missing notice or result")
        plan, actual = group["planned_tender"], group["awarded_contract"]
        if (
            any(plan[k] != actual[k] for k in ("stated_value_date", "stated_maturity_date"))
            or plan["planned_amount_100m"] != actual["actual_amount_100m"]
        ):
            raise ValueError("planned and awarded treasury terms differ")
        if pd.Timestamp(plan["published_at"]) >= pd.Timestamp(actual["published_at"]):
            raise ValueError("plan not published before award")
        result.append((plan, actual))
    for year in {k[0] for k in pairs}:
        issues = {k[1] for k in pairs if k[0] == year}
        if issues != set(range(1, max(issues) + 1)):
            raise ValueError("gap in observed treasury issue sequence")
    return result


def parse_outright(body: bytes, index: dict) -> dict:
    """月度总量没有单笔起息日；事前招标不自动转成已成交。"""
    result, content = official_article(body, index)
    text = compact(content.get_text("", strip=True))
    result.update(
        instrument="outright_reverse_repo", explicit_value_date=None, explicit_maturity_date=None
    )
    if "招标公告" in result["title"]:
        m = re.search(r"(\d{4}年\d{1,2}月\d{1,2}日).*?开展(\d+亿元)买断式逆回购操作", text)
        if not m:
            raise ValueError("unparsed outright plan")
        result.update(
            kind="planned_tender",
            event_date=explicit_date(m[1]),
            planned_amount_100m=amount(m[2]),
            actual_amount_100m=None,
        )
    else:
        m = re.search(r"(\d{4}年\d{1,2}月).*?开展了(\d+亿元)买断式逆回购操作", text)
        if not m:
            raise ValueError("unparsed outright monthly report")
        rows = re.findall(r"([36])个月\((91|182)天\)(\d+亿元)", text)
        if (
            not rows
            or any((a, b) not in [("3", "91"), ("6", "182")] for a, b, _ in rows)
            or sum(amount(v) for _, _, v in rows) != amount(m[2])
        ):
            raise ValueError("unreconciled outright tenor amounts")
        result.update(
            kind="reported_monthly_operations",
            reference_month=month(m[1]),
            actual_amount_100m=amount(m[2]),
            tenor_rows=[dict(tenor_days=int(b), amount_100m=amount(v)) for _, b, v in rows],
        )
    return result
