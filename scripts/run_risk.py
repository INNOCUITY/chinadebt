#!/usr/bin/env python3
"""VaR/ES 与压力测试脚本（Phase 3 / task-24）。

只消费「固定组合持仓 + 曲线」，**不消费归因分项**（未修正的因子归因不作为风险
解释真值）。输出全重估与 KRD 近似的并列结果：

1. `reports/var_es.csv`：历史模拟 / 参数正态 / Cornish-Fisher 三种方法，
   ``[0.95, 0.99]`` 置信度下的 VaR 与 ES（全重估 vs KRD 近似，元与 NAV bp）；
2. `reports/stress_test.csv`：配置 ``cfg.risk.stress_scenarios`` 的平行/陡平/蝶式
   与历史窗口情景（全重估、KRD 近似、差异、近似误差占比）。

默认组合 = **基准阶梯的固定面值**（``build_benchmark``，notional 1e8）在
``--as-of`` 上估值；``--positions-csv`` 可读某策略的逐券快照
（``reports/strategy_*_positions.csv``，按 SYN-<tenor>Y-<YYYYMMDD> 代码还原条款）。

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
from ficc.curve.curve import Curve  # noqa: E402
from ficc.data.schema import CANONICAL_TENORS, BondStatic  # noqa: E402
from ficc.logging import get_logger  # noqa: E402
from ficc.portfolio.benchmark import build_benchmark, build_universe_bonds  # noqa: E402
from ficc.risk import (  # noqa: E402
    historical_var_es,
    parametric_var_es,
    pnl_pct_from_cny,
    portfolio_market_value,
    portfolio_pnl_fixed_revaluation,
    portfolio_pnl_krd_approx,
    stress_test,
)

log = get_logger("run_risk")

TENORS = np.asarray(sorted(CANONICAL_TENORS), dtype=float)
DEFAULT_AS_OF = date(2020, 12, 31)
QUICK_WINDOW = 60
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
    window_start: date,
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

    # 默认：基准阶梯固定面值。合成券在建仓日发行，保证覆盖整个 VaR 历史窗口。
    positions = trading_days.index(window_start)
    if positions == 0:
        raise ValueError(f"窗口起点 {window_start} 之前没有交易日，无法构造固定组合")
    issue_day = trading_days[positions - 1]
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
        f"benchmark ladder（建仓 {issue_day}，notional {cfg.benchmark.notional:,.0f}）",
    )


# ---------------------------------------------------------------------------
# VaR / ES 表
# ---------------------------------------------------------------------------
def _var_table(
    pnl_full: pd.Series,
    pnl_krd: pd.Series,
    *,
    nav: float,
    cfg: Config,
    window_start: date,
    window_end: date,
    horizon_days: int,
) -> pd.DataFrame:
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
                    pct_full, confidences=confidences, horizon_days=horizon_days
                )
                krd_results = historical_var_es(
                    pct_krd, confidences=confidences, horizon_days=horizon_days
                )
            else:
                full_results = parametric_var_es(
                    pct_full, confidences=confidences, horizon_days=horizon_days, dist=dist
                )
                krd_results = parametric_var_es(
                    pct_krd, confidences=confidences, horizon_days=horizon_days, dist=dist
                )
        except ValueError as exc:
            log.warning("跳过 %s：%s", label, exc)
            continue
        for full_result, krd_result in zip(full_results, krd_results, strict=True):
            var_full_cny = full_result.var_pct / 100.0 * nav
            var_krd_cny = krd_result.var_pct / 100.0 * nav
            es_full_cny = full_result.es_pct / 100.0 * nav
            es_krd_cny = krd_result.es_pct / 100.0 * nav
            rows.append(
                {
                    "method": label,
                    "confidence": full_result.confidence,
                    "horizon_days": full_result.horizon_days,
                    "window_start": window_start,
                    "window_end": window_end,
                    "n_obs": full_result.n_obs,
                    "nav": nav,
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
                    "pnl_std_full_cny": float(pnl_full.std(ddof=0)),
                }
            )
    return pd.DataFrame(rows)


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="固定组合 VaR/ES 与压力测试")
    parser.add_argument("--as-of", type=date.fromisoformat, default=DEFAULT_AS_OF)
    parser.add_argument("--window", type=int, default=None, help="VaR 历史窗口（交易日数）")
    parser.add_argument("--horizon", type=int, default=1, help="VaR 持有期（交易日）")
    parser.add_argument("--positions-csv", default=None, help="策略逐券持仓快照 CSV")
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

    window = int(args.window) if args.window is not None else int(cfg.risk.var_window)
    if args.quick:
        window = min(window, QUICK_WINDOW)
    max_window = len(trading_days) - 2
    if window > max_window:
        log.warning("窗口 %d 超过可用交易日，压缩为 %d", window, max_window)
        window = max_window
    if window < 10:
        raise ValueError(f"可用 VaR 窗口过短（{window} 个交易日 < 10）")
    window_start = trading_days[-window]
    horizon_days = int(args.horizon)
    if horizon_days + 1 > window:
        raise ValueError("--horizon 相对 --window 过大")

    faces, bonds, description = _build_portfolio(
        args, panel, calendar, cfg, analysis_day, window_start, trading_days
    )
    curve_analysis = _panel_curve(panel, analysis_day)
    nav = portfolio_market_value(faces, bonds, curve_analysis, calendar=calendar, cfg=cfg)

    log.info(
        "VaR 窗口 %s → %s（%d 个交易日），horizon=%d",
        window_start,
        analysis_day,
        window,
        horizon_days,
    )
    pnl_full = portfolio_pnl_fixed_revaluation(
        faces, bonds, panel, calendar, start=window_start, end=analysis_day, cfg=cfg
    )
    pnl_krd = portfolio_pnl_krd_approx(
        faces, bonds, panel, calendar, start=window_start, end=analysis_day, cfg=cfg
    )
    var_table = _var_table(
        pnl_full,
        pnl_krd,
        nav=nav,
        cfg=cfg,
        window_start=window_start,
        window_end=analysis_day,
        horizon_days=horizon_days,
    )
    var_table.to_csv(out / "var_es.csv", index=False, encoding="utf-8-sig")

    stress_results = stress_test(
        faces,
        bonds,
        curve_analysis,
        scenarios=cfg.risk.stress_scenarios,
        calendar=calendar,
        cfg=cfg,
        panel=panel,
        allow_future_history=bool(args.allow_future_history),
    )
    stress_table = pd.DataFrame(
        [asdict(result) for result in stress_results],
        columns=[
            "scenario",
            "pnl_full",
            "pnl_krd",
            "diff",
            "diff_bp",
            "error_ratio",
            "nav",
            "executed",
            "note",
        ],
    )
    stress_table.to_csv(out / "stress_test.csv", index=False, encoding="utf-8-sig")

    skipped = [result.scenario for result in stress_results if not result.executed]
    if skipped:
        log.warning("未执行的历史情景（窗口晚于分析日/数据不足）：%s", skipped)

    print("\n=== 固定组合 VaR / ES（全重估 vs KRD 近似；金额元，pct 为组合市值百分比）===")
    print(f"组合：{description}")
    print(
        f"分析日：{analysis_day}；NAV（全价市值）={nav:,.2f} 元；窗口 {window_start}→{analysis_day}"
    )
    view = var_table[
        [
            "method",
            "confidence",
            "n_obs",
            "var_full_cny",
            "var_krd_cny",
            "var_diff_cny",
            "var_diff_bp",
            "es_full_cny",
            "es_krd_cny",
            "es_diff_cny",
            "mean_diff_cny",
        ]
    ]
    with pd.option_context("display.width", 240):
        print(view.to_string(index=False, float_format=lambda value: f"{value:,.2f}"))

    print("\n=== 压力测试（全重估 vs KRD 近似）===")
    stress_view = stress_table[
        ["scenario", "executed", "pnl_full", "pnl_krd", "diff", "diff_bp", "error_ratio"]
    ].copy()
    stress_view["note"] = [result.note for result in stress_results]
    with pd.option_context("display.width", 260, "display.max_colwidth", 48):
        print(stress_view.to_string(index=False, float_format=lambda value: f"{value:,.2f}"))

    print("\n披露：")
    print("- 风险包只消费持仓 + 曲线，不使用归因分项（未修正的因子归因不作为风险解释）。")
    print("- 全重估 PnL = 全价变动 + 区间票息现金（T+N 结算日、老化剩余期限重估）。")
    print("- KRD 近似 = −Σ KRD×Δspot（不含 carry/roll、凸性与曲线形状高阶项）；")
    print("  平行大冲击（凸性）、蝶式（曲率）与历史多期限路径是主要误差来源，需并列解读。")
    print("- 历史情景默认只用到分析日的数据；--allow-future-history 时未来窗口按显式假设处理。")
    log.info("产物: %s, %s", out / "var_es.csv", out / "stress_test.csv")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
