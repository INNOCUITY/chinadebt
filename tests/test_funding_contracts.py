"""资金合同与余额：真实日期、独立覆盖、发布边界与计划/结果分离。"""

import copy
import json
from pathlib import Path

import pandas as pd
import pytest
from audit_funding_contracts import target_coverage

from ficc.data.funding_contracts import (
    amount,
    asof_record,
    explicit_date,
    monthly_coverage,
    paired_treasury,
    parse_mlf,
    parse_outright,
    parse_treasury,
)


def article(title, content, clock="2025-05-02 18:00:00"):
    index = dict(title=title, date=clock[:10], url="https://www.pbc.gov.cn/example")
    body = f'<h2>{title}</h2><div id="shijian">{clock}</div><div id="zoom">{content}</div>'.encode()
    return body, index


def treasury(issue="一", planned=True, amount_text="100亿元"):
    title = f"2025年中央国库现金管理商业银行定期存款（{issue}期）招投标"
    if planned:
        return parse_treasury(
            *article(
                title + "通知",
                f"本期操作量{amount_text}。起息日为2025年5月6日，到期日为2025年8月5日（遇节假日顺延）。",
            )
        )
    cells = [title.removesuffix("招投标"), amount_text, "2025年5月6日", "2025年8月5日", "2.1%"]
    html = (
        "<table><tr>"
        + "".join(f"<td>{c}</td>" for c in ["名称", "中标总量", "起息日", "到期日", "中标利率"])
        + "</tr><tr>"
        + "".join(f"<td>{c}</td>" for c in cells)
        + "</tr></table>"
    )
    return parse_treasury(*article(title + "结果", html, "2025-05-06 10:30:00"))


def test_mlf_reference_month_and_release_are_not_interchangeable():
    r = parse_mlf(
        *article(
            "2025年4月中期借贷便利开展情况", "时间中期借贷便利余额（单位：亿元）2025年4月46570"
        ),
        table=True,
    )
    assert r["reference_month"] == "2025-04"
    assert r["published_at"] == "2025-05-02T18:00:00+08:00"
    assert r["balance_100m"] == 46570 and r["explicit_maturity_date"] is None
    assert r["reported_operation_100m"] is None


def test_post_operation_balance_is_not_month_end_balance():
    r = parse_mlf(
        *article(
            "2025年2月中期借贷便利开展情况",
            "2025年2月25日，人民银行开展3000亿元中期借贷便利操作。操作后，中期借贷便利余额为40940亿元。",
            "2025-02-25 09:43:00",
        ),
        table=False,
    )
    assert r["kind"] == "reported_post_operation_balance"
    assert r["reported_operation_date"] == "2025-02-25"
    assert r["reported_operation_100m"] == 3000
    assert r["explicit_value_date"] is None


def test_explicit_zero_flow_does_not_zero_balance_or_missing_flow():
    r = parse_mlf(
        *article(
            "2020年2月中期借贷便利开展情况",
            "2020年2月未对金融机构开展中期借贷便利操作，期末中期借贷便利余额为35000亿元。",
        ),
        table=False,
    )
    assert r["reported_operation_100m"] == 0 and r["balance_100m"] == 35000
    r = parse_mlf(
        *article("2020年2月中期借贷便利开展情况", "期末中期借贷便利余额为35000亿元。"), table=False
    )
    assert r["reported_operation_100m"] is None


def test_mlf_plan_is_neither_award_nor_survey_expectation():
    r = parse_mlf(
        *article("2025年5月中期借贷便利招标公告", "2025年5月23日，将开展5000亿元MLF操作。"),
        table=False,
    )
    assert r["kind"] == "planned_tender" and r["planned_amount_100m"] == 5000
    assert r["actual_amount_100m"] is None and r["expected_amount_100m"] is None


@pytest.mark.parametrize("value", ["7天", "5月6日", "2025年2月30日"])
def test_no_inferred_or_invalid_calendar_dates(value):
    with pytest.raises(ValueError):
        explicit_date(value)


@pytest.mark.parametrize("value", ["", "—", "10万元", "100"])
def test_missing_or_wrong_unit_amount_never_becomes_zero(value):
    with pytest.raises(ValueError):
        amount(value)


def test_month_deleted_or_duplicated_fails_independent_coverage():
    rows = [dict(reference_month=m) for m in ["2025-01", "2025-02", "2025-03"]]
    assert len(monthly_coverage(rows, "2025-01", "2025-03")) == 3
    for changed in (rows[1:], [*rows, rows[0]]):
        with pytest.raises(ValueError, match="missing or duplicate"):
            monthly_coverage(changed, "2025-01", "2025-03")


def test_treasury_same_day_two_issues_and_contradictions():
    p, a = treasury(), treasury(planned=False)
    p2, a2 = treasury("二"), treasury("二", False)
    assert len(paired_treasury([p, a, p2, a2])) == 2
    assert a["stated_maturity_date"] == "2025-08-05"
    assert not a["actual_cash_settlement_verified"] and p["holiday_adjustment_clause"]
    for changed in ([p], [p, a, a], [p, treasury(planned=False, amount_text="101亿元")], [p2, a2]):
        with pytest.raises(ValueError):
            paired_treasury(changed)


def test_asof_plan_cannot_read_future_award_and_unknown_is_not_zero():
    p, a = treasury(), treasury(planned=False)
    p["available_session"] = "2025-05-06"
    a["available_session"] = "2025-05-07"
    cutoff = "2025-05-06T09:00:00+08:00"
    assert asof_record([p, a], cutoff, "awarded_contract") is None
    assert asof_record([p, a], cutoff, "planned_tender")["actual_amount_100m"] is None
    changed = copy.deepcopy(a)
    changed["actual_amount_100m"] = 99999
    assert asof_record([p, changed], cutoff, "planned_tender") == p
    assert asof_record([p, a], "2025-05-02T23:00:00+08:00", "planned_tender") is None
    assert asof_record([p, a], "2025-05-07T09:00:00+08:00", "awarded_contract") == a
    with pytest.raises(ValueError, match="timezone"):
        asof_record([p], "2025-05-06", "planned_tender")


def test_asof_uses_absolute_time_and_keeps_contracts_separate():
    r = dict(
        instrument="mlf",
        kind="reported_monthly_balance",
        published_at="2025-05-02T09:00:00+08:00",
        available_session="2025-05-06",
        balance_100m=10,
    )
    later = dict(r, published_at="2025-05-02T02:00:00+00:00", balance_100m=20)
    assert asof_record([r, later], "2025-05-06T00:00:00+08:00", r["kind"]) == later
    assert (
        asof_record([dict(r, available_session=None)], "2025-05-06T00:00:00+08:00", r["kind"])
        is None
    )
    with pytest.raises(ValueError, match="single contract"):
        asof_record([treasury(), treasury("二")], "2025-05-06T00:00:00+08:00", "planned_tender")


def test_outright_monthly_totals_do_not_imply_cash_dates():
    r = parse_outright(
        *article(
            "公开市场买断式逆回购业务公告[2025]第5号",
            "2025年5月开展了7000亿元买断式逆回购操作。3个月（91天）4000亿元6个月（182天）3000亿元",
        )
    )
    assert r["reference_month"] == "2025-05" and r["actual_amount_100m"] == 7000
    assert r["explicit_maturity_date"] is None and r["explicit_value_date"] is None
    with pytest.raises(ValueError, match="unreconciled"):
        parse_outright(
            *article("公告", "2025年5月开展了7000亿元买断式逆回购操作。3个月（91天）4000亿元")
        )


def test_publication_identity_and_missing_clock_fail():
    body, index = article("2025年4月中期借贷便利开展情况", "unused")
    for changed in (dict(index, date="2025-05-03"), dict(index, title="other")):
        with pytest.raises(ValueError):
            parse_mlf(body, changed, table=True)


def test_target_coverage_does_not_impute_or_relabel_fixing():
    frame = pd.DataFrame(dict(date=["2025-05-06"], FDR007=[1.5]))
    r = target_coverage(frame, ["2025-05-06", "2025-05-07"], ["2025-05-06", "2025-05-07"])
    assert r["observed"] == {"FDR007": 1}
    assert r["missing_dates"]["FDR007"] == ["2025-05-07"]
    assert r["missing_columns"] == ["DR001", "DR007", "R007"]
    with pytest.raises(ValueError, match="duplicate"):
        target_coverage(pd.concat([frame, frame]), ["2025-05-06"], ["2025-05-06", "2025-05-06"])


def test_audit_summary_preserves_research_and_forward_boundaries():
    r = json.loads(Path("reports/funding_contract_coverage.json").read_text())
    plan = json.loads(Path("config/baselines/funding_contract_audit.json").read_text())
    assert r["raw_snapshot_sha256"] == plan["raw_snapshot_sha256"]
    assert r["mlf"]["verified_months"] == r["mlf"]["expected_months"] == 79
    assert (
        r["treasury"]["contracts_in_scope"] == 46
        and r["treasury"]["unique_value_dates_in_scope"] == 43
    )
    assert not r["ordinary_reverse_repo"]["net_injection_and_stock_admitted"]
    assert r["expectations"]["unobserved_expectations"] is None
    for key in [
        "new_experiment_frozen",
        "new_experiment_run",
        "ppo_training_added",
        "forward_period_accessed",
    ]:
        assert r["decision"][key] is False
