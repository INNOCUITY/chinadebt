#!/usr/bin/env python3
"""VaR/ES 与压力测试脚本（Phase 3 / R39-R40）。

只消费「固定组合持仓 + 曲线」，**不消费归因分项**（未修正的因子归因不作为风险
解释真值）。R39/R40 后的口径：

1. **current_frozen（默认）**：冻结分析日（``--as-of``）的曲线/结算日/剩余期限/
   面值，只搬历史即期曲线关键点变化 ``Δz``，用 ``price_from_spot`` 重估当前组合
   （默认 price-only；``--include-carry`` 叠加分析日曲线下的持有期 carry/roll 与
   现金票息）；KRD 对照用同一即期曲线/冲击基的解析一阶导线性化。
2. **historical_aging（legacy 对照）**：旧“固定面值逐日历史 PnL 回看”
   （历史结算日/剩余期限/曲线，含票息现金），并列披露，不作为当前快照风险。
3. **压力测试**：情景在即期空间重写（平行=所有关键点同向；陡平/蝶式沿用
   ``Curve.shock`` 的期限形状并作用到关键点）；全重估与 KRD 共用
   ``price_from_spot`` + ``spot_shock``。历史情景用历史日 spot 曲线变动重演，
   默认不使用分析日之后的数据。

默认组合 = **分析日前 250 个交易日建仓的基准阶梯固定面值**（``build_benchmark``，
notional 1e8；``--portfolio-age`` 可调，与 VaR lookback 解耦）。``--positions-csv``
可读某策略逐券快照（``reports/strategy_*_positions.csv``，按
``SYN-<tenor>Y-<YYYYMMDD>`` 代码还原条款）。

用法::

    PYTHONPATH=src .venv/bin/python scripts/run_risk.py
    PYTHONPATH=src .venv/bin/python scripts/run_risk.py --quick
    PYTHONPATH=src .venv/bin/python scripts/run_risk.py --positions-csv \\
        reports/strategy_2s10s_positions.csv --as-of 2020-12-31
"""

from __future__ import annotations

import argparse
import re
import sys
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path

_ROOT = Path(__file__).resolve().parents[1]
_SRC = _ROOT / "src"
_SCRIPTS = _ROOT / "scripts"
for _path in (_SRC, _SCRIPTS):
    if str(_path) not in sys.path:
        sys.path.insert(0, str(_path))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402
from run_strategies import load_market  # noqa: E402

from ficc.config import Config, get_config  # noqa: E402
from ficc.curve.bootstrap import bootstrap_spot  # noqa: E402
from ficc.curve.curve import Curve  # noqa: E402
from ficc.data.schema import CANONICAL_TENORS, BondStatic  # noqa: E402
from ficc.logging import get_logger  # noqa: E402
from ficc.portfolio.benchmark import build_benchmark, build_universe_bonds  # noqa: E402
from ficc.portfolio.dates import settlement_for  # noqa: E402
from ficc.risk import (  # noqa: E402
    historical_var_es,
    parametric_var_es,
    pnl_pct_from_cny,
    portfolio_market_value,
    portfolio_pnl_current_portfolio_factors,
    portfolio_pnl_current_portfolio_krd,
    portfolio_pnl_fixed_revaluation,
    portfolio_pnl_krd_approx,
    price_from_spot,
    stress_test,
)

log = get_logger("run_risk")

TENORS = np.asarray(sorted(CANONICAL_TENORS), dtype=float)
DEFAULT_AS_OF = date(2020, 12, 31)
QUICK_WINDOW = 60
DEFAULT_PORTFOLIO_AGE = 250
_PRICE_FUNCTION = "price_from_spot"
_SHOCK_BASIS = "spot_key_linear_flat"
_KRD_METHOD = "spot_analytic_derivative"
# R39 审查反例（YTM 空间冻结口径，price-only）：默认基准组合、lookback=250。
REVIEW_TARGETS = {
    "var_cny": 318_246.69828,
    "es_cny": 433_273.83884,
}
_POSITION_CODE = re.compile(r"^SYN-(?P<tenor>\d+(?:\.\d+)?)Y-(?P<day>\d{8})$")


# ---------------------------------------------------------------------------
# 面板 → 曲线 / 组合构造
# ---------------------------------------------------------------------------
def _panel_curve(panel: pd.DataFrame, day: date) -> Curve:
    row = panel.loc[pd.Timestamp(day), list(TENORS)]
    if row.isna().any():
        raise ValueError(f"{day} 曲线存在缺失期限：{row[row.isna()].index.tolist()}")
    return Curve(
        as_of=day,
        tenors=TENORS.copy(),
        rates=row.to_numpy(dtype=float),
        method="hermite",
    )


def _spot_market_value(
    faces: dict[str, float],
    bonds: tuple[BondStatic, ...],
    curve: Curve,
    calendar: object,
    cfg: Config,
) -> float:
    settlement = settlement_for(calendar, curve.as_of, cfg=cfg)
    spot = bootstrap_spot(curve, settlement=settlement, calendar=calendar)
    total = 0.0
    for bond in bonds:
        face = faces.get(bond.code, 0.0)
        if face > 0.0:
            total += face / 100.0 * price_from_spot(bond, settlement, spot, cfg=cfg)
    return float(total)


def _bonds_from_codes(codes: list[str], panel: pd.DataFrame, cfg: Config) -> tuple[BondStatic, ...]:
    """从合成券代码 ``SYN-<tenor>Y-<YYYYMMDD>`` 还原建仓日条款。"""
    by_code: dict[str, BondStatic] = {}
    for code in codes:
        match = _POSITION_CODE.match(str(code))
        if match is None:
            raise ValueError(
                f"无法从持仓代码 {code!r} 还原债券条款（仅支持 SYN-<tenor>Y-<YYYYMMDD>；"
                "其他券请同时提供 trades 条款）"
            )
        issue_day = datetime.strptime(match.group("day"), "%Y%m%d").date()
        if issue_day not in set(panel.index.date):
            raise ValueError(f"{code}: 建仓日 {issue_day} 不在曲线面板内")
        universe = build_universe_bonds(_panel_curve(panel, issue_day), issue_day, cfg=cfg)
        matched = [bond for bond in universe if bond.code == code]
        if not matched:
            raise ValueError(f"{code}: 不在 cfg.portfolio.universe 生成的合成券集合里")
        by_code[code] = matched[0]
    return tuple(sorted(by_code.values(), key=lambda bond: bond.maturity_date))


def _build_portfolio(
    args: argparse.Namespace,
    panel: pd.DataFrame,
    calendar: object,
    cfg: Config,
    analysis_day: date,
    trading_days: list[date],
) -> tuple[dict[str, float], tuple[BondStatic, ...], str]:
    if args.positions_csv:
        path = Path(args.positions_csv)
        if not path.exists():
            raise FileNotFoundError(f"持仓快照不存在: {path}")
        frame = pd.read_csv(path)
        missing = {"as_of", "bond_code", "face_value"} - set(frame.columns)
        if missing:
            raise ValueError(f"{path} 缺少列 {sorted(missing)}")
        frame["as_of"] = pd.to_datetime(frame["as_of"], errors="raise").dt.date
        rows = frame[(frame["as_of"] == analysis_day) & (frame["face_value"] > 0.0)]
        if rows.empty:
            raise ValueError(f"{path}: {analysis_day} 没有持仓行（face_value > 0）")
        faces = {
            str(code): float(face)
            for code, face in zip(rows["bond_code"], rows["face_value"], strict=True)
        }
        bonds = _bonds_from_codes(list(faces), panel, cfg)
        return faces, bonds, f"positions:{path.name}"

    # 固定组合：分析日前 --portfolio-age 个交易日建仓，之后买入持有；**与 VaR
    # lookback 解耦**（R39：lookback 变化不得改变面值/发行日）。
    age = int(args.portfolio_age)
    if age < 1:
        raise ValueError(f"--portfolio-age 必须 >= 1，实际 {age}")
    analysis_index = trading_days.index(analysis_day)
    issue_index = analysis_index - age
    if issue_index < 0:
        raise ValueError(
            f"--portfolio-age={age} 超过可用交易日（分析日前只有 {analysis_index} 个）"
        )
    issue_day = trading_days[issue_index]
    bonds = build_universe_bonds(_panel_curve(panel, issue_day), issue_day, cfg=cfg)
    benchmark = build_benchmark(
        _panel_curve(panel, analysis_day),
        analysis_day,
        bonds=bonds,
        calendar=calendar,
        cfg=cfg,
    )
    faces = {code: float(face) for code, face in benchmark.faces.items()}
    return (
        faces,
        bonds,
        f"benchmark ladder（{issue_day} 建仓、age={age} 交易日、"
        f"notional {cfg.benchmark.notional:,.0f}）",
    )


# ---------------------------------------------------------------------------
# VaR / ES 表
# ---------------------------------------------------------------------------
_VAR_COLUMNS = [
    "portfolio_basis",
    "price_function",
    "shock_basis",
    "krd_price_function",
    "method",
    "confidence",
    "horizon_days",
    "lookback",
    "n_obs",
    "window_start",
    "window_end",
    "nav",
    "include_carry",
    "shock_aggregation",
    "carry_treatment",
    "var_full_pct",
    "var_full_cny",
    "var_krd_pct",
    "var_krd_cny",
    "var_diff_cny",
    "var_diff_bp",
    "es_full_pct",
    "es_full_cny",
    "es_krd_pct",
    "es_krd_cny",
    "es_diff_cny",
    "es_diff_bp",
    "mean_pnl_full_cny",
    "mean_pnl_krd_cny",
    "mean_diff_cny",
]


def _var_rows(
    *,
    portfolio_basis: str,
    pnl_full: pd.Series,
    pnl_krd: pd.Series,
    nav: float,
    cfg: Config,
    window_start: date,
    window_end: date,
    lookback: int,
    horizon_days: int,
    include_carry: bool,
    price_function: str,
    shock_basis: str,
    pre_aggregated: bool,
    shock_aggregation: str,
    carry_treatment: str,
) -> list[dict[str, object]]:
    pct_full = pnl_pct_from_cny(pnl_full, nav)
    pct_krd = pnl_pct_from_cny(pnl_krd, nav)
    confidences = tuple(float(c) for c in cfg.risk.confidence)
    rows: list[dict[str, object]] = []
    methods = (
        ("historical", None),
        ("parametric_normal", "normal"),
        ("parametric_cornish_fisher", "cornish_fisher"),
    )
    for label, dist in methods:
        try:
            if dist is None:
                full_results = historical_var_es(
                    pct_full,
                    confidences=confidences,
                    horizon_days=horizon_days,
                    pre_aggregated=pre_aggregated,
                )
                krd_results = historical_var_es(
                    pct_krd,
                    confidences=confidences,
                    horizon_days=horizon_days,
                    pre_aggregated=pre_aggregated,
                )
            else:
                full_results = parametric_var_es(
                    pct_full,
                    confidences=confidences,
                    horizon_days=horizon_days,
                    dist=dist,
                    pre_aggregated=pre_aggregated,
                )
                krd_results = parametric_var_es(
                    pct_krd,
                    confidences=confidences,
                    horizon_days=horizon_days,
                    dist=dist,
                    pre_aggregated=pre_aggregated,
                )
        except ValueError as exc:
            log.warning("跳过 %s/%s：%s", portfolio_basis, label, exc)
            continue
        for full_result, krd_result in zip(full_results, krd_results, strict=True):
            var_full_cny = full_result.var_pct / 100.0 * nav
            var_krd_cny = krd_result.var_pct / 100.0 * nav
            es_full_cny = full_result.es_pct / 100.0 * nav
            es_krd_cny = krd_result.es_pct / 100.0 * nav
            rows.append(
                {
                    "portfolio_basis": portfolio_basis,
                    "price_function": price_function,
                    "shock_basis": shock_basis,
                    "krd_price_function": _KRD_METHOD,
                    "method": label,
                    "confidence": full_result.confidence,
                    "horizon_days": full_result.horizon_days,
                    "lookback": lookback,
                    "n_obs": full_result.n_obs,
                    "window_start": window_start,
                    "window_end": window_end,
                    "nav": nav,
                    "include_carry": include_carry,
                    "shock_aggregation": shock_aggregation,
                    "carry_treatment": carry_treatment,
                    "var_full_pct": full_result.var_pct,
                    "var_full_cny": var_full_cny,
                    "var_krd_pct": krd_result.var_pct,
                    "var_krd_cny": var_krd_cny,
                    "var_diff_cny": var_full_cny - var_krd_cny,
                    "var_diff_bp": (var_full_cny - var_krd_cny) / nav * 1e4,
                    "es_full_pct": full_result.es_pct,
                    "es_full_cny": es_full_cny,
                    "es_krd_pct": krd_result.es_pct,
                    "es_krd_cny": es_krd_cny,
                    "es_diff_cny": es_full_cny - es_krd_cny,
                    "es_diff_bp": (es_full_cny - es_krd_cny) / nav * 1e4,
                    "mean_pnl_full_cny": float(pnl_full.mean()),
                    "mean_pnl_krd_cny": float(pnl_krd.mean()),
                    "mean_diff_cny": float((pnl_full - pnl_krd).mean()),
                }
            )
    return rows


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="固定组合 VaR/ES 与压力测试（R39/R40）")
    parser.add_argument("--as-of", type=date.fromisoformat, default=DEFAULT_AS_OF)
    parser.add_argument("--window", type=int, default=None, help="VaR 历史 lookback（交易日数）")
    parser.add_argument("--horizon", type=int, default=1, help="VaR 持有期（交易日）")
    parser.add_argument(
        "--portfolio-age",
        type=int,
        default=DEFAULT_PORTFOLIO_AGE,
        help="默认组合在分析日前多少个交易日建仓（固定，与 lookback 解耦）",
    )
    parser.add_argument("--positions-csv", default=None, help="策略逐券持仓快照 CSV")
    parser.add_argument(
        "--include-carry",
        action="store_true",
        help="current_frozen 叠加分析日曲线下的持有期 carry/roll + 现金票息",
    )
    parser.add_argument("--allow-future-history", action="store_true")
    parser.add_argument("--config", default=None)
    parser.add_argument("--quick", action="store_true", help="短窗口（60 个交易日）")
    args = parser.parse_args(argv)
    if args.horizon < 1:
        parser.error("--horizon 必须 >= 1")

    cfg = get_config(args.config)
    out = cfg.paths.abs("reports")
    out.mkdir(parents=True, exist_ok=True)
    panel, _funding, calendar = load_market(cfg)
    panel = panel.reindex(columns=list(TENORS))

    trading_days = [day for day in calendar.dates() if day <= args.as_of]
    if not trading_days:
        raise ValueError(f"曲线面板在 {args.as_of} 之前没有交易日")
    analysis_day = trading_days[-1]
    if analysis_day != args.as_of:
        log.warning("--as-of %s 不是交易日，改用最近交易日 %s", args.as_of, analysis_day)

    lookback = int(args.window) if args.window is not None else int(cfg.risk.var_window)
    if args.quick:
        lookback = min(lookback, QUICK_WINDOW)
    max_lookback = len(trading_days) - 2
    if lookback > max_lookback:
        log.warning("lookback %d 超过可用交易日，压缩为 %d", lookback, max_lookback)
        lookback = max_lookback
    if lookback < 10:
        raise ValueError(f"可用 VaR lookback 过短（{lookback} 个交易日 < 10）")
    window_start = trading_days[-lookback]
    horizon_days = int(args.horizon)
    if horizon_days + 1 > lookback:
        raise ValueError("--horizon 相对 --window 过大")

    faces, bonds, description = _build_portfolio(
        args, panel, calendar, cfg, analysis_day, trading_days
    )
    curve_analysis = _panel_curve(panel, analysis_day)
    nav_spot = _spot_market_value(faces, bonds, curve_analysis, calendar, cfg)
    nav_ytm = portfolio_market_value(faces, bonds, curve_analysis, calendar=calendar, cfg=cfg)

    log.info(
        "current_frozen：分析日 %s 冻结组合，历史 lookback=%d（即期 Δz），horizon=%d",
        analysis_day,
        lookback,
        horizon_days,
    )
    frozen_full = portfolio_pnl_current_portfolio_factors(
        faces,
        bonds,
        panel,
        analysis_day,
        lookback=lookback,
        horizon_days=horizon_days,
        include_carry=bool(args.include_carry),
        cfg=cfg,
        calendar=calendar,
    )
    frozen_krd = portfolio_pnl_current_portfolio_krd(
        faces,
        bonds,
        panel,
        analysis_day,
        lookback=lookback,
        horizon_days=horizon_days,
        include_carry=bool(args.include_carry),
        cfg=cfg,
        calendar=calendar,
    )
    var_rows = _var_rows(
        portfolio_basis="current_frozen",
        pnl_full=frozen_full,
        pnl_krd=frozen_krd,
        nav=nav_spot,
        cfg=cfg,
        window_start=window_start,
        window_end=analysis_day,
        lookback=lookback,
        horizon_days=horizon_days,
        include_carry=bool(args.include_carry),
        price_function=_PRICE_FUNCTION,
        shock_basis=_SHOCK_BASIS,
        pre_aggregated=True,
        shock_aggregation="h_day_cumulative",
        carry_treatment="once_per_path" if args.include_carry else "none",
    )

    # legacy：固定面值历史逐日老化（含票息现金），仅作并列披露。
    try:
        aging_full = portfolio_pnl_fixed_revaluation(
            faces,
            bonds,
            panel,
            calendar,
            start=window_start,
            end=analysis_day,
            cfg=cfg,
        )
        aging_krd = portfolio_pnl_krd_approx(
            faces,
            bonds,
            panel,
            calendar,
            start=window_start,
            end=analysis_day,
            cfg=cfg,
        )
        var_rows += _var_rows(
            portfolio_basis="historical_aging",
            pnl_full=aging_full,
            pnl_krd=aging_krd,
            nav=nav_ytm,
            cfg=cfg,
            window_start=window_start,
            window_end=analysis_day,
            lookback=lookback,
            horizon_days=horizon_days,
            include_carry=True,
            price_function="dirty_price_from_ytm",
            shock_basis="ytm_curve(aging)+spot_key_daily",
            pre_aggregated=False,
            shock_aggregation="overlapping_h_day_sum",
            carry_treatment="per_day_cash_included(legacy)",
        )
    except ValueError as exc:
        log.warning("historical_aging 旧口径不可用（%s），仅报告 current_frozen", exc)

    var_table = pd.DataFrame(var_rows, columns=_VAR_COLUMNS)
    var_table.to_csv(out / "var_es.csv", index=False, encoding="utf-8-sig")

    # 压力测试：配置情景 + 小冲击收敛检查（R40 验收证据）。
    convergence = [
        {"name": "[收敛]平行1bp", "type": "parallel", "bp": 1.0},
        {"name": "[收敛]陡峭化0.01bp", "type": "steepen", "bp": 0.01},
        {"name": "[收敛]平坦化0.01bp", "type": "flatten", "bp": 0.01},
        {"name": "[收敛]蝶式0.001bp", "type": "butterfly", "bp": 0.001},
    ]
    stress_results = stress_test(
        faces,
        bonds,
        curve_analysis,
        scenarios=list(cfg.risk.stress_scenarios) + convergence,
        calendar=calendar,
        cfg=cfg,
        panel=panel,
        allow_future_history=bool(args.allow_future_history),
    )
    stress_table = pd.DataFrame(
        [asdict(result) for result in stress_results],
        columns=[
            "scenario",
            "executed",
            "pnl_full",
            "pnl_krd",
            "diff",
            "diff_bp",
            "error_ratio",
            "nav",
            "price_function",
            "shock_basis",
            "note",
        ],
    )
    stress_table.to_csv(out / "stress_test.csv", index=False, encoding="utf-8-sig")

    skipped = [result.scenario for result in stress_results if not result.executed]
    if skipped:
        log.warning("未执行的历史情景（窗口晚于分析日/数据不足）：%s", skipped)

    print("\n=== 固定组合 VaR / ES（金额元；current_frozen 为 R39 默认口径）===")
    print(f"组合：{description}")
    frozen_n_obs = len(frozen_full)
    print(
        f"分析日：{analysis_day}；NAV（current_frozen 即期全价）={nav_spot:,.2f} 元；"
        f"lookback={lookback}，horizon={horizon_days}，h 日样本 n_obs={frozen_n_obs}"
    )
    view = var_table[
        [
            "portfolio_basis",
            "method",
            "confidence",
            "horizon_days",
            "n_obs",
            "var_full_cny",
            "var_krd_cny",
            "var_diff_bp",
            "es_full_cny",
            "es_krd_cny",
            "es_diff_bp",
        ]
    ]
    with pd.option_context("display.width", 240):
        print(view.to_string(index=False, float_format=lambda value: f"{value:,.2f}"))

    frozen_95 = var_table[
        (var_table["portfolio_basis"] == "current_frozen")
        & (var_table["method"] == "historical")
        & (var_table["confidence"].round(6) == 0.95)
        & (~var_table["include_carry"])
    ]
    if not frozen_95.empty:
        row = frozen_95.iloc[0]
        for label, column, target in (
            ("VaR", "var_full_cny", REVIEW_TARGETS["var_cny"]),
            ("ES", "es_full_cny", REVIEW_TARGETS["es_cny"]),
        ):
            diff_pct = (float(row[column]) / target - 1.0) * 100.0
            log.info(
                "R39 审查反例对照（current_frozen 95%% %s）：本脚本 %.2f 元 vs 反例 %.2f 元（%+.2f%%）",
                label,
                float(row[column]),
                target,
                diff_pct,
            )

    print("\n=== 压力测试（全重估 vs KRD；同一 price_from_spot + spot_shock）===")
    stress_view = stress_table[
        [
            "scenario",
            "executed",
            "pnl_full",
            "pnl_krd",
            "diff",
            "diff_bp",
            "error_ratio",
            "price_function",
            "shock_basis",
        ]
    ]
    with pd.option_context("display.width", 260, "display.max_colwidth", 32):
        print(stress_view.to_string(index=False, float_format=lambda value: f"{value:,.6g}"))

    print("\n披露：")
    print("- current_frozen：冻结分析日曲线/结算日/剩余期限/面值，先构造 h 日**累计**即期关键点")
    print("  Δz=z_{t+h}−z_t，一次性冲击重估一次；carry 每样本只加一次（price-only 不加）。")
    print("  h>1 时 n_obs=lookback−h+1（重叠样本，不作独立样本推断）；参数法/CF 直接用 h 日")
    print("  累计 PnL 分布，不再乘 sqrt(h) 或二次聚合。")
    print("- historical_aging：旧固定面值逐日老化回看（含票息现金），随 lookback 改变组合，")
    print("  仅并列披露，不能作为当前快照风险；两者差异见 var_es.csv 的 portfolio_basis 列。")
    print("- 压力测试：平行=即期关键点同向；陡平/蝶式=Curve.shock 期限形状作用于即期关键点；")
    print("  历史=历史日即期曲线变动重演。全重估/KRD 共用 price_from_spot + spot_shock，")
    print("  KRD 用同一即期曲线的解析一阶导（消除 1bp 单边差分偏差）。")
    print("- 风险包只消费持仓+曲线，不使用归因分项（未修正的因子归因不作为风险解释）。")
    log.info("产物: %s, %s", out / "var_es.csv", out / "stress_test.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
