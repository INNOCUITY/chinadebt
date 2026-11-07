"""独立 PnL 对账（`src/ficc/portfolio/reconcile.py`）的单元测试。

覆盖要求：
- 干净合成数据：六项检查全通过（且逐项直调也通过）；
- 六个篡改测试：现金事件 / 持仓面值 / snapshot.nav / market_value /
  dirty_price / attribution 分项 → 对应检查必须 failed；
- 现金差额定位（detail 指出日期与 kind）；
- 空持仓/无交易：NAV=cash、PnL=0；
- 外部现金流：未声明必须报错，声明后通过；
- 容差边界：误差 == 容差通过，略超失败；
- 独立性证据：MTM 路径不读取 nav 序列（错误 nav 下 MTM 合计仍正确）。
"""

from __future__ import annotations

import inspect
from dataclasses import FrozenInstanceError

import numpy as np
import pandas as pd
import pytest

from ficc.portfolio.reconcile import (
    CheckResult,
    ReconciliationReport,
    check_attribution_closure,
    check_cash_identity,
    check_full_price_identity,
    check_nav_identity,
    check_pnl_identity,
    check_position_identity,
    mtm_daily_pnl,
    reconcile_all,
)
from ficc.utils.calendar import TradingCalendar

DATES = pd.to_datetime(["2024-06-03", "2024-06-04", "2024-06-05", "2024-06-06", "2024-06-07"])
FACE_A = 1_000_000.0
FACE_B = 500_000.0


def _attribution() -> dict[str, float]:
    """PnL 6,350 = carry 15,000 + fees −150 + price −8,500。"""
    return {"coupon_carry": 15_000.0, "fees": -150.0, "price_move": -8_500.0}


def _clean_fixture() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.Series]:
    """两券、一笔买入/一笔买入+票息；现金/持仓/NAV/MTM 全部自洽（PnL=6,350）。"""
    cashflows = pd.DataFrame(
        {
            "date": pd.to_datetime(
                ["2024-06-03", "2024-06-04", "2024-06-04", "2024-06-06", "2024-06-06", "2024-06-06"]
            ),
            "kind": ["initial", "trade_buy", "fee", "trade_buy", "fee", "coupon"],
            "amount": [1_000_000.0, -1_005_000.0, -100.0, -496_000.0, -50.0, 15_000.0],
            "bond_code": [None, "BOND_A", "BOND_A", "BOND_B", "BOND_B", "BOND_A"],
            "trade_id": [None, "T1", "T1", "T2", "T2", None],
            "note": ["初始资金", "买入A", "手续费", "买入B", "手续费", "票息"],
        }
    )
    trades = pd.DataFrame(
        {
            "trade_id": ["T1", "T2"],
            "bond_code": ["BOND_A", "BOND_B"],
            "side": ["buy", "buy"],
            "face_value": [FACE_A, FACE_B],
            "clean_price": [100.0, 99.0],
            "signal_date": pd.to_datetime(["2024-06-03", "2024-06-05"]),
            "trade_date": pd.to_datetime(["2024-06-03", "2024-06-05"]),
            "settlement_date": pd.to_datetime(["2024-06-04", "2024-06-06"]),
            "fee": [100.0, 50.0],
        }
    )
    rows: list[dict[str, object]] = []
    # day0：空仓，NAV=cash
    rows.append(
        {
            "as_of": DATES[0],
            "cash": 1_000_000.0,
            "nav": 1_000_000.0,
            "bond_code": np.nan,
            "face_value": np.nan,
            "clean_price": np.nan,
            "accrued_interest": np.nan,
            "dirty_price": np.nan,
            "market_value": 0.0,
        }
    )
    # day1/2：仅 A
    for i, (cash, dirty_a) in enumerate([(-5_100.0, 100.5), (-5_100.0, 100.8)], start=1):
        rows.append(
            {
                "as_of": DATES[i],
                "cash": cash,
                "nav": cash + FACE_A / 100.0 * dirty_a,
                "bond_code": "BOND_A",
                "face_value": FACE_A,
                "clean_price": dirty_a - 0.5,
                "accrued_interest": 0.5,
                "dirty_price": dirty_a,
                "market_value": FACE_A / 100.0 * dirty_a,
            }
        )
    # day3/4：A + B
    day3 = [
        ("BOND_A", FACE_A, 98.9, 0.5, 99.4),
        ("BOND_B", FACE_B, 99.0, 0.2, 99.2),
    ]
    day4 = [
        ("BOND_A", FACE_A, 99.1, 0.5, 99.6),
        ("BOND_B", FACE_B, 99.1, 0.2, 99.3),
    ]
    for i, (cash, positions) in enumerate([(-486_150.0, day3), (-486_150.0, day4)], start=3):
        nav = cash
        for code, face, clean, ai, dirty in positions:
            mv = face / 100.0 * dirty
            nav += mv
            rows.append(
                {
                    "as_of": DATES[i],
                    "cash": cash,
                    "nav": nav,
                    "bond_code": code,
                    "face_value": face,
                    "clean_price": clean,
                    "accrued_interest": ai,
                    "dirty_price": dirty,
                    "market_value": mv,
                }
            )
    snapshots = pd.DataFrame(rows)
    # 同组内 cash/nav 必须一致（快照长表重复填充）；NAV = cash + Σ市值
    for _day, block in snapshots.groupby("as_of"):
        cash = float(block["cash"].iloc[0])
        market_value = pd.to_numeric(block["market_value"], errors="coerce").fillna(0.0).sum()
        snapshots.loc[block.index, "cash"] = cash
        snapshots.loc[block.index, "nav"] = cash + float(market_value)
    nav = pd.Series(
        [
            1_000_000.0,
            999_900.0,
            1_002_900.0,
            1_003_850.0,
            1_006_350.0,
        ],
        index=DATES,
        dtype=float,
    )
    return trades, cashflows, snapshots, nav


def _window(cashflows: pd.DataFrame, begin: pd.Timestamp, end: pd.Timestamp) -> pd.DataFrame:
    return cashflows[(cashflows["date"] > begin) & (cashflows["date"] <= end)].copy()


def _find(report: ReconciliationReport, name: str) -> CheckResult:
    matches = [check for check in report.checks if check.name == name]
    assert len(matches) == 1, f"报告里没有唯一检查 {name}: {[c.name for c in report.checks]}"
    return matches[0]


# ---------------------------------------------------------------------------
# 干净数据：六项全过（汇总 + 逐项直调）
# ---------------------------------------------------------------------------
def test_clean_fixture_all_six_checks_pass() -> None:
    trades, cashflows, snapshots, nav = _clean_fixture()
    report = reconcile_all(
        cashflows=cashflows,
        trades=trades,
        snapshots=snapshots,
        nav=nav,
        attribution=_attribution(),
        calendar=TradingCalendar.from_dates(pd.bdate_range("2024-06-03", "2024-06-10")),
    )
    assert report.passed, report.to_frame().to_string()
    assert [check.name for check in report.checks] == [
        "cash_identity",
        "position_identity",
        "nav_identity",
        "pnl_identity",
        "full_price_identity",
        "attribution_closure",
    ]
    frame = report.to_frame()
    assert set(frame.columns) == {"name", "passed", "max_abs_error", "tolerance", "detail"}
    assert frame["passed"].all()
    # 每个检查的 max_abs_error 都要落盘且 <= tolerance
    for check in report.checks:
        assert check.tolerance == pytest.approx(1e-6)
        assert check.max_abs_error is not None and check.max_abs_error <= check.tolerance


def test_clean_fixture_each_check_directly_passes() -> None:
    trades, cashflows, snapshots, nav = _clean_fixture()
    first, last = snapshots["as_of"].min(), snapshots["as_of"].max()
    cash_begin = float(snapshots.loc[snapshots["as_of"] == first, "cash"].iloc[0])
    cash_end = float(snapshots.loc[snapshots["as_of"] == last, "cash"].iloc[0])
    assert check_cash_identity(
        _window(cashflows, first, last), cash_begin=cash_begin, cash_end=cash_end
    ).passed
    assert check_position_identity(
        trades,
        snapshots[snapshots["as_of"] == first],
        snapshots[snapshots["as_of"] == last],
        calendar=TradingCalendar.from_dates(pd.bdate_range("2024-06-03", "2024-06-10")),
    ).passed
    assert check_nav_identity(snapshots).passed
    assert check_pnl_identity(nav, cashflows, snapshots=snapshots).passed
    assert check_full_price_identity(snapshots).passed
    assert check_attribution_closure(6_350.0, _attribution()).passed


# ---------------------------------------------------------------------------
# 篡改检测（六个，逐一必须 failed）
# ---------------------------------------------------------------------------
def test_tamper_cash_event_is_detected() -> None:
    trades, cashflows, snapshots, nav = _clean_fixture()
    tampered = cashflows.copy()
    idx = tampered.index[tampered["kind"] == "coupon"][0]
    tampered.loc[idx, "amount"] = 15_100.0
    first, last = snapshots["as_of"].min(), snapshots["as_of"].max()
    result = check_cash_identity(
        _window(tampered, first, last),
        cash_begin=float(snapshots.loc[snapshots["as_of"] == first, "cash"].iloc[0]),
        cash_end=float(snapshots.loc[snapshots["as_of"] == last, "cash"].iloc[0]),
    )
    assert not result.passed
    assert result.max_abs_error == pytest.approx(100.0)
    # 明细必须定位到正确日期与 kind
    assert "2024-06-06" in result.detail and "coupon" in result.detail
    report = reconcile_all(
        cashflows=tampered, trades=trades, snapshots=snapshots, nav=nav, attribution=_attribution()
    )
    assert not report.passed
    assert not _find(report, "cash_identity").passed


def test_tamper_position_face_is_detected() -> None:
    trades, cashflows, snapshots, nav = _clean_fixture()
    tampered = trades.copy()
    tampered.loc[tampered["trade_id"] == "T1", "face_value"] = FACE_A + 100.0
    first, last = snapshots["as_of"].min(), snapshots["as_of"].max()
    result = check_position_identity(
        tampered,
        snapshots[snapshots["as_of"] == first],
        snapshots[snapshots["as_of"] == last],
    )
    assert not result.passed
    assert result.max_abs_error == pytest.approx(100.0)
    report = reconcile_all(
        cashflows=cashflows,
        trades=tampered,
        snapshots=snapshots,
        nav=nav,
        attribution=_attribution(),
    )
    assert not _find(report, "position_identity").passed


def test_tamper_snapshot_nav_is_detected() -> None:
    trades, cashflows, snapshots, nav = _clean_fixture()
    tampered = snapshots.copy()
    day = tampered["as_of"] == DATES[2]
    tampered.loc[day, "nav"] = tampered.loc[day, "nav"] + 0.01
    result = check_nav_identity(tampered)
    assert not result.passed
    assert result.max_abs_error == pytest.approx(0.01, abs=1e-9)
    nav_tampered = nav.copy()
    nav_tampered.loc[DATES[2]] = nav_tampered.loc[DATES[2]] + 0.01
    report = reconcile_all(
        cashflows=cashflows,
        trades=trades,
        snapshots=tampered,
        nav=nav_tampered,
        attribution=_attribution(),
    )
    assert not _find(report, "nav_identity").passed


def test_tamper_market_value_is_detected() -> None:
    trades, cashflows, snapshots, nav = _clean_fixture()
    tampered = snapshots.copy()
    day = (tampered["as_of"] == DATES[2]) & (tampered["bond_code"] == "BOND_A")
    tampered.loc[day, "market_value"] = tampered.loc[day, "market_value"] + 0.02
    result = check_nav_identity(tampered)
    assert not result.passed
    assert result.max_abs_error >= 0.02 - 1e-12
    report = reconcile_all(
        cashflows=cashflows, trades=trades, snapshots=tampered, nav=nav, attribution=_attribution()
    )
    assert not _find(report, "nav_identity").passed


def test_tamper_dirty_price_is_detected() -> None:
    trades, cashflows, snapshots, nav = _clean_fixture()
    tampered = snapshots.copy()
    day = (tampered["as_of"] == DATES[3]) & (tampered["bond_code"] == "BOND_B")
    tampered.loc[day, "dirty_price"] = tampered.loc[day, "dirty_price"] + 0.01
    result = check_full_price_identity(tampered)
    assert not result.passed
    assert result.max_abs_error == pytest.approx(0.01, abs=1e-9)
    report = reconcile_all(
        cashflows=cashflows, trades=trades, snapshots=tampered, nav=nav, attribution=_attribution()
    )
    assert not _find(report, "full_price_identity").passed


def test_tamper_attribution_component_is_detected() -> None:
    attribution = _attribution()
    attribution["price_move"] += 1.0
    result = check_attribution_closure(6_350.0, attribution)
    assert not result.passed
    assert result.max_abs_error == pytest.approx(1.0)
    assert "残差（单独报告，不吸收）" in result.detail
    trades, cashflows, snapshots, nav = _clean_fixture()
    report = reconcile_all(
        cashflows=cashflows, trades=trades, snapshots=snapshots, nav=nav, attribution=attribution
    )
    assert not _find(report, "attribution_closure").passed


# ---------------------------------------------------------------------------
# 其它反例
# ---------------------------------------------------------------------------
def test_sell_exceeding_holdings_is_detected() -> None:
    _, _, snapshots, _ = _clean_fixture()
    trades = pd.DataFrame(
        {
            "trade_id": ["T1"],
            "bond_code": ["BOND_A"],
            "side": ["sell"],
            "face_value": [2 * FACE_A],
            "clean_price": [100.0],
            "signal_date": [DATES[0]],
            "trade_date": [DATES[0]],
            "settlement_date": [DATES[1]],
            "fee": [0.0],
        }
    )
    result = check_position_identity(
        trades,
        snapshots[snapshots["as_of"] == DATES[0]],
        snapshots[snapshots["as_of"] == DATES[-1]],
    )
    assert not result.passed
    assert "卖出超持仓" in result.detail


def test_settlement_date_inconsistency_is_detected() -> None:
    trades, _, snapshots, _ = _clean_fixture()
    bad = trades.copy()
    bad.loc[bad["trade_id"] == "T1", "settlement_date"] = pd.Timestamp("2024-06-05")
    result = check_position_identity(
        bad,
        snapshots[snapshots["as_of"] == DATES[0]],
        snapshots[snapshots["as_of"] == DATES[-1]],
        calendar=TradingCalendar.from_dates(pd.bdate_range("2024-06-03", "2024-06-10")),
    )
    assert not result.passed
    assert "结算日不符" in result.detail


def test_empty_portfolio_nav_equals_cash_and_pnl_zero() -> None:
    dates = pd.to_datetime(["2024-06-03", "2024-06-04", "2024-06-05"])
    snapshots = pd.DataFrame(
        {
            "as_of": dates,
            "cash": 1_000_000.0,
            "nav": 1_000_000.0,
            "bond_code": np.nan,
            "face_value": 0.0,
            "clean_price": np.nan,
            "accrued_interest": np.nan,
            "dirty_price": np.nan,
            "market_value": 0.0,
        }
    )
    cashflows = pd.DataFrame(
        {
            "date": [dates[0]],
            "kind": ["initial"],
            "amount": [1_000_000.0],
            "bond_code": [None],
            "trade_id": [None],
            "note": ["initial"],
        }
    )
    nav = pd.Series(1_000_000.0, index=dates)
    report = reconcile_all(
        cashflows=cashflows,
        trades=pd.DataFrame(
            columns=[
                "trade_id",
                "bond_code",
                "side",
                "face_value",
                "clean_price",
                "signal_date",
                "trade_date",
                "settlement_date",
                "fee",
            ]
        ),
        snapshots=snapshots,
        nav=nav,
        attribution={"cash": 0.0},
    )
    assert report.passed, report.to_frame().to_string()
    pnl = _find(report, "pnl_identity")
    assert pnl.passed and "MTM 路径合计=0.000000" in pnl.detail


def test_external_flows_must_be_declared_then_pass() -> None:
    """窗口内 +50,000 出入金：不声明报错；显式声明后六项通过。"""
    trades, cashflows, snapshots, nav = _clean_fixture()
    injection = pd.Timestamp("2024-06-05")
    cash_tampered = (
        pd.concat(
            [
                cashflows,
                pd.DataFrame(
                    {
                        "date": [injection],
                        "kind": ["initial"],  # external kind：必须显式声明
                        "amount": [50_000.0],
                        "bond_code": [None],
                        "trade_id": [None],
                        "note": ["外部入金"],
                    }
                ),
            ],
            ignore_index=True,
        )
        .sort_values("date", kind="stable")
        .reset_index(drop=True)
    )
    snap_tampered = snapshots.copy()
    after = snap_tampered["as_of"] >= injection
    snap_tampered.loc[after, "cash"] = snap_tampered.loc[after, "cash"] + 50_000.0
    snap_tampered.loc[after, "nav"] = snap_tampered.loc[after, "nav"] + 50_000.0
    nav_tampered = nav.copy()
    nav_tampered.loc[nav_tampered.index >= injection] += 50_000.0

    # 未声明 → 必须报错（不能把入金当收益）
    with pytest.raises(ValueError, match="外部现金流"):
        check_pnl_identity(nav_tampered, cash_tampered, snapshots=snap_tampered)
    with pytest.raises(ValueError, match="外部现金流"):
        reconcile_all(
            cashflows=cash_tampered, trades=trades, snapshots=snap_tampered, nav=nav_tampered
        )

    declared = pd.Series({injection: 50_000.0})
    assert check_pnl_identity(
        nav_tampered, cash_tampered, external_flows=declared, snapshots=snap_tampered
    ).passed
    report = reconcile_all(
        cashflows=cash_tampered,
        trades=trades,
        snapshots=snap_tampered,
        nav=nav_tampered,
        attribution=_attribution(),
        external_flows=declared,
    )
    assert report.passed, report.to_frame().to_string()


def test_declared_external_flows_mismatch_fails() -> None:
    """声明金额与实际入金不一致 → PnL 未解释项应失败。"""
    _trades, cashflows, snapshots, nav = _clean_fixture()
    injection = pd.Timestamp("2024-06-05")
    cash_tampered = pd.concat(
        [
            cashflows,
            pd.DataFrame(
                {
                    "date": [injection],
                    "kind": ["initial"],
                    "amount": [50_000.0],
                    "bond_code": [None],
                    "trade_id": [None],
                    "note": ["外部入金"],
                }
            ),
        ],
        ignore_index=True,
    )
    snap_tampered = snapshots.copy()
    after = snap_tampered["as_of"] >= injection
    snap_tampered.loc[after, "cash"] += 50_000.0
    snap_tampered.loc[after, "nav"] += 50_000.0
    nav_tampered = nav.copy()
    nav_tampered.loc[nav_tampered.index >= injection] += 50_000.0
    result = check_pnl_identity(
        nav_tampered,
        cash_tampered,
        external_flows=pd.Series({injection: 60_000.0}),
        snapshots=snap_tampered,
    )
    assert not result.passed
    assert "声明外部现金流合计" in result.detail


# ---------------------------------------------------------------------------
# 容差边界（<= 通过，略超失败）
# ---------------------------------------------------------------------------
def test_tolerance_boundary_cash_identity() -> None:
    events = pd.DataFrame(
        {
            "date": [DATES[0]],
            "kind": ["fee"],
            "amount": [0.0],
            "bond_code": [None],
            "trade_id": [None],
            "note": [None],
        }
    )
    # 用二进制可精确表示的 0.5 元，避免浮点表示误差干扰边界语义
    exact = check_cash_identity(events, cash_begin=100.0, cash_end=100.5, tolerance=0.5)
    assert exact.passed, exact.detail
    assert exact.max_abs_error == pytest.approx(0.5)
    over = check_cash_identity(events, cash_begin=100.0, cash_end=100.5, tolerance=0.4)
    assert not over.passed, over.detail
    # 默认容差 1e-6 同样按 <= 判定（用可精确表示的小量）
    assert check_cash_identity(
        events, cash_begin=100.0, cash_end=100.0 + 1e-6, tolerance=1e-5
    ).passed


# ---------------------------------------------------------------------------
# PnL 两条路径的独立性证据
# ---------------------------------------------------------------------------
def test_mtm_path_does_not_read_nav() -> None:
    _trades, cashflows, snapshots, nav = _clean_fixture()
    # MTM 路径的公开实现不接收 nav 参数
    assert "nav" not in inspect.signature(mtm_daily_pnl).parameters
    mtm = mtm_daily_pnl(snapshots, cashflows)
    assert float(mtm.sum()) == pytest.approx(6_350.0)
    np.testing.assert_allclose(
        mtm.to_numpy(),
        np.array([-100.0, 3_000.0, 950.0, 2_500.0]),
        atol=1e-9,
    )
    # 故意改错某一天的 nav：MTM 合计仍正确，但两路径不一致 → 检查失败
    wrong_nav = nav.copy()
    wrong_nav.loc[DATES[2]] = wrong_nav.loc[DATES[2]] + 500.0
    result = check_pnl_identity(wrong_nav, cashflows, snapshots=snapshots)
    assert not result.passed
    assert "MTM 路径合计=6350.000000" in result.detail
    # 正确 nav 下通过
    assert check_pnl_identity(nav, cashflows, snapshots=snapshots).passed


# ---------------------------------------------------------------------------
# 其它契约/API 行为
# ---------------------------------------------------------------------------
def test_report_api_and_raise_if_failed() -> None:
    result = CheckResult(name="x", passed=True, detail="ok", max_abs_error=0.0, tolerance=1e-6)
    report = ReconciliationReport(checks=(result,))
    assert report.passed
    assert report.to_frame().iloc[0]["name"] == "x"
    report.raise_if_failed()
    bad = CheckResult(name="y", passed=False, detail="boom", max_abs_error=1.0, tolerance=1e-6)
    with pytest.raises(ValueError, match="y"):
        ReconciliationReport(checks=(result, bad)).raise_if_failed()
    with pytest.raises(FrozenInstanceError):
        result.passed = False  # type: ignore[misc]


def test_attribution_without_attribution_gives_five_checks() -> None:
    trades, cashflows, snapshots, nav = _clean_fixture()
    report = reconcile_all(cashflows=cashflows, trades=trades, snapshots=snapshots, nav=nav)
    assert report.passed
    assert len(report.checks) == 5
    assert "attribution_closure" not in {check.name for check in report.checks}


def test_cashflows_unknown_kind_raises() -> None:
    bad = pd.DataFrame(
        {
            "date": [DATES[0]],
            "kind": ["mystery"],
            "amount": [1.0],
            "bond_code": [None],
            "trade_id": [None],
            "note": [None],
        }
    )
    with pytest.raises(ValueError, match="未知类型"):
        check_cash_identity(bad, cash_begin=0.0, cash_end=1.0)


def test_attribution_long_table_and_series() -> None:
    long_table = pd.DataFrame(
        {"component": ["carry", "fees", "price"], "amount": [15_000.0, -150.0, -8_500.0]}
    )
    assert check_attribution_closure(6_350.0, long_table).passed
    series = pd.Series({"carry": 15_000.0, "fees": -150.0, "price": -8_500.0})
    assert check_attribution_closure(6_350.0, series).passed
    wide = pd.DataFrame({"carry": [15_000.0], "fees": [-150.0], "price": [-8_500.0]})
    assert check_attribution_closure(6_350.0, wide).passed


# ===========================================================================
# 未结算会计（trade-date）：pending_cash 支持
# ===========================================================================
PENDING_DATES = pd.to_datetime(["2024-06-04", "2024-06-05", "2024-06-06", "2024-06-07"])


def _pending_fixture() -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.Series]:
    """成交日未结算：day1 买入（持仓当天计入）、day2 才划款。

    快照同时含 ``pending_cash``（买入 pending 为负），NAV 按
    ``cash + ΣMV + pending_cash``；MTM 路径需加 ``Δpending_cash`` 才能对上。
    """
    cashflows = pd.DataFrame(
        {
            "date": pd.to_datetime(["2024-06-04", "2024-06-06", "2024-06-06", "2024-06-07"]),
            "kind": ["initial", "trade_buy", "fee", "coupon"],
            "amount": [1_000_000.0, -1_005_000.0, -100.0, 15_000.0],
            "bond_code": [None, "BOND_A", "BOND_A", "BOND_A"],
            "trade_id": [None, "T1", "T1", None],
            "note": ["初始资金", "买入A结算", "手续费", "票息"],
        }
    )
    trades = pd.DataFrame(
        {
            "trade_id": ["T1"],
            "bond_code": ["BOND_A"],
            "side": ["buy"],
            "face_value": [1_000_000.0],
            "clean_price": [100.0],
            "signal_date": pd.to_datetime(["2024-06-05"]),
            "trade_date": pd.to_datetime(["2024-06-05"]),
            "settlement_date": pd.to_datetime(["2024-06-06"]),
            "fee": [100.0],
        }
    )
    snapshots = pd.DataFrame(
        [
            {
                "as_of": PENDING_DATES[0],
                "cash": 1_000_000.0,
                "pending_cash": 0.0,
                "nav": 1_000_000.0,
                "bond_code": np.nan,
                "face_value": np.nan,
                "clean_price": np.nan,
                "accrued_interest": np.nan,
                "dirty_price": np.nan,
                "market_value": 0.0,
            },
            {
                "as_of": PENDING_DATES[1],
                "cash": 1_000_000.0,
                "pending_cash": -1_005_100.0,
                "nav": 999_900.0,
                "bond_code": "BOND_A",
                "face_value": 1_000_000.0,
                "clean_price": 100.0,
                "accrued_interest": 0.5,
                "dirty_price": 100.5,
                "market_value": 1_005_000.0,
            },
            {
                "as_of": PENDING_DATES[2],
                "cash": -5_100.0,
                "pending_cash": 0.0,
                "nav": 1_002_900.0,
                "bond_code": "BOND_A",
                "face_value": 1_000_000.0,
                "clean_price": 100.3,
                "accrued_interest": 0.5,
                "dirty_price": 100.8,
                "market_value": 1_008_000.0,
            },
            {
                "as_of": PENDING_DATES[3],
                "cash": 9_900.0,
                "pending_cash": 0.0,
                "nav": 1_003_900.0,
                "bond_code": "BOND_A",
                "face_value": 1_000_000.0,
                "clean_price": 98.9,
                "accrued_interest": 0.5,
                "dirty_price": 99.4,
                "market_value": 994_000.0,
            },
        ]
    )
    nav = pd.Series(
        [1_000_000.0, 999_900.0, 1_002_900.0, 1_003_900.0],
        index=PENDING_DATES,
        dtype=float,
    )
    return trades, cashflows, snapshots, nav


def test_pending_cash_trade_date_accounting_reconciles() -> None:
    trades, cashflows, snapshots, nav = _pending_fixture()
    # NAV 重算公式：cash + ΣMV + pending_cash
    nav_check = check_nav_identity(snapshots)
    assert nav_check.passed, nav_check.detail
    assert nav_check.max_abs_error == pytest.approx(0.0, abs=1e-12)
    assert "pending_cash" in nav_check.detail
    # 路径 A/B：ΔMV + Δpending + 内部现金流 与 ΔNAV 一致
    pnl = check_pnl_identity(nav, cashflows, snapshots=snapshots)
    assert pnl.passed, pnl.detail
    assert pnl.max_abs_error is not None and pnl.max_abs_error <= 1e-6
    assert "Δpending_cash" in pnl.detail
    mtm = mtm_daily_pnl(snapshots, cashflows)
    np.testing.assert_allclose(mtm.to_numpy(), np.array([-100.0, 3_000.0, 1_000.0]), atol=1e-9)
    assert float(mtm.sum()) == pytest.approx(3_900.0)
    # 去掉 pending_cash 列（旧版数据）后逐日 MTM 明显不同（证明确实用了 pending；
    # 首末 pending 均为 0 时合计可能巧合相同，故比较逐日序列）
    mtm_old = mtm_daily_pnl(snapshots.drop(columns=["pending_cash"]), cashflows)
    assert not np.allclose(mtm_old.to_numpy(), mtm.to_numpy())
    np.testing.assert_allclose(mtm_old.to_numpy(), np.array([1_005_000.0, -1_002_100.0, 1_000.0]))
    # 六项全量对账（归因=carry 15,000 + fees −100 + price −11,000）
    report = reconcile_all(
        cashflows=cashflows,
        trades=trades,
        snapshots=snapshots,
        nav=nav,
        attribution={"coupon_carry": 15_000.0, "fees": -100.0, "price_move": -11_000.0},
        calendar=TradingCalendar.from_dates(pd.bdate_range("2024-06-04", "2024-06-11")),
    )
    assert report.passed, report.to_frame().to_string()


def test_old_snapshot_without_pending_cash_is_flagged_in_detail() -> None:
    """旧版输入无 pending_cash：按 0 处理但 detail 必须明确标注，不能静默。"""
    _, _, snapshots, _ = _clean_fixture()  # 旧版快照
    assert "pending_cash" not in snapshots.columns
    result = check_nav_identity(snapshots)
    assert result.passed
    assert "旧版输入无 pending_cash" in result.detail


def test_tamper_pending_cash_fails_nav_identity() -> None:
    _, _, snapshots, _ = _pending_fixture()
    tampered = snapshots.copy()
    day = tampered["as_of"] == PENDING_DATES[1]
    tampered.loc[day, "pending_cash"] = tampered.loc[day, "pending_cash"] + 100.0
    result = check_nav_identity(tampered)
    assert not result.passed
    assert result.max_abs_error == pytest.approx(100.0)
    # 同一篡改也会让两条 PnL 路径失配（Δpending 被改动）
    _, cashflows, _, nav = _pending_fixture()
    assert not check_pnl_identity(nav, cashflows, snapshots=tampered).passed
    trades, _, _, _ = _pending_fixture()
    report = reconcile_all(cashflows=cashflows, trades=trades, snapshots=tampered, nav=nav)
    assert not report.passed
    assert not _find(report, "nav_identity").passed


def test_nav_recalc_formula_includes_pending_cash() -> None:
    """NAV 重算公式显式验证：cash + Σmarket_value + pending_cash == nav。"""
    _, _, snapshots, _ = _pending_fixture()
    for _as_of, block in snapshots.groupby("as_of"):
        cash = float(block["cash"].iloc[0])
        pending = float(block["pending_cash"].iloc[0])
        mv = float(pd.to_numeric(block["market_value"], errors="coerce").fillna(0.0).sum())
        nav = float(block["nav"].iloc[0])
        assert cash + mv + pending == pytest.approx(nav, abs=1e-9)
    result = check_nav_identity(snapshots)
    assert result.passed
    assert result.max_abs_error == pytest.approx(0.0, abs=1e-12)
