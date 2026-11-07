"""基准组合（R22 修复版）：**买入持有、固定面值**的阶梯基准 + 每日 mark-to-market。

设计口径（R22）
---------------
审查指出"风险基准每天重配、绩效基准固定面值"是两套口径：引擎每日按固定 1 亿
市值重建面值，而绩效比较用起点固定面值买入持有；且估值用原始期限标签取收益率，
没有按老化后的剩余期限。本模块统一为：

1. :func:`build_universe_bonds`：起点按曲线 YTM 生成一批合成平价券，**全回测复用**；
2. :func:`build_benchmark`：**只在起点**按市值权重折算一次固定面值（并取整到
   ``min_trade_lot``），返回带 ``faces`` 的基准；
3. :func:`mark_benchmark` / :func:`mark_faces`：每日仅对**同一批固定面值**按
   **老化后剩余期限**估值，得到当日 KRD / 总 DV01 / 久期 / 市值。风险基准与绩效
   基准因此是同一个组合。

单位约定：金额元、面值元、价格元/百元面值、收益率百分数、KRD 元/bp（多头为正）。
"""

from __future__ import annotations

import re
from datetime import date, datetime, timedelta

import numpy as np

from ficc.config import Config, get_config
from ficc.curve.bootstrap import bootstrap_spot
from ficc.curve.curve import Curve
from ficc.data.schema import BondStatic
from ficc.logging import get_logger
from ficc.portfolio.dates import remaining_years, settlement_for
from ficc.portfolio.types import BenchmarkLeg, BenchmarkPortfolio
from ficc.pricing.analytics import key_rate_durations, mod_duration
from ficc.pricing.bond import dirty_price_from_ytm

__all__ = [
    "build_benchmark",
    "build_universe_bonds",
    "mark_benchmark",
    "mark_faces",
]

log = get_logger("portfolio_benchmark")

_CODE_TENOR = re.compile(r"SYN-(?P<tenor>\d+(?:\.\d+)?)Y-")


# ---------------------------------------------------------------------------
# 合成券条款
# ---------------------------------------------------------------------------
def _add_years(anchor: date, years: float) -> date:
    """``anchor + years``；闰日（2/29）顺延到 2/28，非整数年用 365.25 天近似。"""
    years_f = float(years)
    if abs(years_f - round(years_f)) < 1e-12:
        whole = round(years_f)
        try:
            return anchor.replace(year=anchor.year + whole)
        except ValueError:  # 2/29 -> 2/28
            return anchor.replace(year=anchor.year + whole, day=28)
    return anchor + timedelta(days=round(365.25 * years_f))


def _coupon_frequency(tenor: float) -> int:
    """合成券付息频率：2/3/5/7Y 年付；10Y 及以上半年付。"""
    return 2 if float(tenor) >= 10.0 else 1


def _resolve_cfg(cfg: Config | None) -> Config:
    return cfg if cfg is not None else get_config()


def _normalise_date(value: date | datetime) -> date:
    if isinstance(value, datetime):
        return value.date()
    if not isinstance(value, date):
        raise TypeError(f"as_of 必须是 datetime.date，实际 {type(value).__name__}")
    return value


def build_universe_bonds(
    curve: Curve, as_of: date, *, cfg: Config | None = None
) -> tuple[BondStatic, ...]:
    """按 ``cfg.portfolio.universe`` 生成一批合成平价券（全回测复用）。

    票面 = 起点曲线对应期限 YTM；起息 = ``as_of``；到期 = ``as_of + tenor`` 年；
    期限 ≥10Y 半年付息、其余年付。code 形如 ``SYN-10Y-20190128``。
    """
    resolved = _resolve_cfg(cfg)
    start = _normalise_date(as_of)
    tenors = [float(t) for t in resolved.portfolio.universe]
    if not tenors:
        raise ValueError("cfg.portfolio.universe 为空")
    if any((not np.isfinite(t)) or t <= 0 for t in tenors):
        raise ValueError(f"universe 期限必须为正数，实际 {tenors}")
    if len(set(tenors)) != len(tenors):
        raise ValueError(f"universe 期限重复: {tenors}")

    bonds: list[BondStatic] = []
    for tenor in tenors:
        ytm = float(curve.rate(tenor))
        if not np.isfinite(ytm):
            raise ValueError(f"曲线在 {tenor}Y 无有效收益率（NaN/inf）")
        bond = BondStatic(
            code=f"SYN-{tenor:g}Y-{start:%Y%m%d}",
            issue_date=start,
            maturity_date=_add_years(start, tenor),
            coupon_pct=float(max(ytm, 0.0)),
            freq=_coupon_frequency(tenor),  # type: ignore[arg-type]
            face_value=float(resolved.portfolio.synthetic.face_value),
            accrual="coupon",
            market="IB",
        )
        bonds.append(bond)
    return tuple(bonds)


def _bond_tenor(bond: BondStatic) -> float:
    """券的期限标签（优先从 code 解析，回退到到期-起息天数/365.25）。"""
    match = _CODE_TENOR.search(bond.code)
    if match:
        return float(match.group("tenor"))
    days = (bond.maturity_date - bond.issue_date).days
    return round(days / 365.25, 6)


def _validate_weights(weights: dict[float, float], universe: list[float]) -> dict[float, float]:
    if not weights:
        raise ValueError("cfg.benchmark.ladder_weights 为空")
    for tenor, weight in weights.items():
        if not np.isfinite(tenor) or tenor <= 0:
            raise ValueError(f"ladder_weights 期限非法: {tenor!r}")
        if not np.isfinite(weight) or weight < 0:
            raise ValueError(f"ladder_weights[{tenor}] 必须是非负有限数，实际 {weight!r}")
    total = float(sum(weights.values()))
    if abs(total - 1.0) > 1e-9:
        raise ValueError(f"ladder_weights 权重和必须为 1，实际 {total!r}")
    universe_set = {round(float(t), 10) for t in universe}
    weights_keys = {round(float(t), 10) for t in weights}
    if weights_keys != universe_set:
        raise ValueError(
            f"ladder_weights 的期限 {sorted(weights)} 必须与 portfolio.universe "
            f"{sorted(universe)} 完全一致（基准腿=universe 券）"
        )
    return {float(tenor): float(weight) for tenor, weight in weights.items()}


def _bond_map(bonds: tuple[BondStatic, ...]) -> dict[float, BondStatic]:
    if not bonds:
        raise ValueError("bonds 为空")
    out: dict[float, BondStatic] = {}
    for bond in bonds:
        tenor = round(_bond_tenor(bond), 10)
        if tenor in out:
            raise ValueError(f"bonds 中期限 {tenor}Y 重复")
        out[tenor] = bond
    return out


# ---------------------------------------------------------------------------
# 基准构造（固定面值）
# ---------------------------------------------------------------------------
def build_benchmark(
    curve: Curve,
    as_of: date,
    *,
    bonds: tuple[BondStatic, ...] | None = None,
    calendar: object | None = None,
    cfg: Config | None = None,
) -> BenchmarkPortfolio:
    """在起点构造**买入持有、固定面值**的阶梯基准。

    ``face_i = notional × w_i / (dirty_i / 100)``，其中 ``dirty`` 用**结算日**
    （观察日 T+N，CN_IB 日历）与老化前剩余期限计算；面值取整到
    ``constraints.min_trade_lot`` 的整数倍，使引擎/基准买入可精确复现。
    """
    resolved = _resolve_cfg(cfg)
    start = _normalise_date(as_of)
    weights = _validate_weights(
        {float(k): float(v) for k, v in resolved.benchmark.ladder_weights.items()},
        [float(t) for t in resolved.portfolio.universe],
    )
    universe = build_universe_bonds(curve, start, cfg=resolved) if bonds is None else tuple(bonds)
    bond_by_tenor = _bond_map(universe)
    missing = sorted(set(weights) - set(bond_by_tenor))
    if missing:
        raise ValueError(f"bonds 缺少 ladder_weights 对应的期限 {missing}Y")

    notional = float(resolved.benchmark.notional)
    if not np.isfinite(notional) or notional <= 0:
        raise ValueError(f"benchmark.notional 必须为正数，实际 {notional!r}")
    settlement = settlement_for(calendar, start, cfg=resolved)
    lot = float(resolved.constraints.min_trade_lot)

    faces: dict[str, float] = {}
    for tenor in sorted(weights):
        bond = bond_by_tenor[tenor]
        market_value = notional * weights[tenor]
        ytm = float(curve.rate(remaining_years(bond, settlement)))
        dirty = float(dirty_price_from_ytm(bond, settlement, ytm, cfg=resolved))
        if not np.isfinite(dirty) or dirty <= 0:
            raise ValueError(f"{tenor}Y 合成券起息日全价非正: {dirty!r}")
        raw_face = market_value / (dirty / 100.0)
        face = round(raw_face / lot) * lot
        if face <= 0:
            raise ValueError(f"{tenor}Y 腿面值取整后非正: raw={raw_face!r}, lot={lot!r}")
        faces[bond.code] = float(face)

    log.info(
        "基准构造（固定面值）: %d 腿, notional %.2f, 面值(万) %s",
        len(faces),
        notional,
        {code: round(face / 1e4, 2) for code, face in faces.items()},
    )
    return mark_faces(
        faces,
        bonds=universe,
        curve=curve,
        as_of=start,
        calendar=calendar,
        cfg=resolved,
        weights=weights,
        notional=notional,
    )


def mark_faces(
    faces: dict[str, float],
    *,
    bonds: tuple[BondStatic, ...],
    curve: Curve,
    as_of: date,
    calendar: object | None = None,
    cfg: Config | None = None,
    weights: dict[float, float] | None = None,
    notional: float | None = None,
) -> BenchmarkPortfolio:
    """对固定面值做每日 mark-to-market（老化后剩余期限；真实 CN_IB 结算日）。"""
    resolved = _resolve_cfg(cfg)
    start = _normalise_date(as_of)
    settlement = settlement_for(calendar, start, cfg=resolved)
    key_tenors = [float(t) for t in resolved.risk.key_tenors_krd]
    if not key_tenors:
        raise ValueError("cfg.risk.key_tenors_krd 为空")
    spot = bootstrap_spot(curve, settlement=settlement, calendar=calendar)
    bond_by_code = {bond.code: bond for bond in bonds}

    legs: list[BenchmarkLeg] = []
    krd: dict[float, float] = {t: 0.0 for t in key_tenors}
    total_dv01 = 0.0
    total_mv = 0.0
    weighted_duration = 0.0
    for code, face in faces.items():
        bond = bond_by_code.get(code)
        if bond is None:
            raise ValueError(f"faces 中的 {code} 不在 bonds 里")
        remaining = remaining_years(bond, settlement)
        if remaining <= 0:
            raise ValueError(f"{code} 在 {settlement} 已到期，无法估值")
        ytm = float(curve.rate(remaining))
        dirty = float(dirty_price_from_ytm(bond, settlement, ytm, cfg=resolved))
        market_value = float(face) / 100.0 * dirty
        spot_krd = key_rate_durations(bond, settlement, spot, key_tenors=key_tenors, cfg=resolved)
        positive = {float(k): -float(v) * float(face) / 100.0 for k, v in spot_krd.items()}
        leg_krd = {float(t): positive.get(float(t), 0.0) for t in key_tenors}
        leg_dv01 = float(sum(leg_krd.values()))
        duration = float(mod_duration(bond, settlement, ytm, cfg=resolved))
        for tenor, value in leg_krd.items():
            krd[tenor] = krd.get(tenor, 0.0) + value
        total_dv01 += leg_dv01
        total_mv += market_value
        weighted_duration += market_value * duration
        legs.append(
            BenchmarkLeg(
                bond=bond,
                face_value=float(face),
                market_value=market_value,
                dv01=leg_dv01,
                krd=leg_krd,
            )
        )
    if total_mv <= 0 or total_dv01 <= 0:
        raise ValueError(f"基准 mark 非正: mv={total_mv!r}, dv01={total_dv01!r}")
    return BenchmarkPortfolio(
        as_of=start,
        legs=tuple(legs),
        market_value=float(total_mv),
        total_dv01=float(total_dv01),
        duration=float(weighted_duration / total_mv),
        krd={float(t): float(krd[t]) for t in key_tenors},
        weights=(
            {float(k): float(v) for k, v in weights.items()}
            if weights is not None
            else {round(_bond_tenor(leg.bond), 10): leg.market_value / total_mv for leg in legs}
        ),
        faces={code: float(face) for code, face in faces.items()},
        notional=float(notional if notional is not None else total_mv),
    )


def mark_benchmark(
    benchmark: BenchmarkPortfolio,
    *,
    bonds: tuple[BondStatic, ...],
    curve: Curve,
    as_of: date,
    calendar: object | None = None,
    cfg: Config | None = None,
) -> BenchmarkPortfolio:
    """对已有基准的**固定面值**做当日 mark（风险基准与绩效基准同一组合）。"""
    if not benchmark.faces:
        raise ValueError("benchmark.faces 为空：请用 build_benchmark 构造（含固定面值）")
    return mark_faces(
        dict(benchmark.faces),
        bonds=bonds,
        curve=curve,
        as_of=as_of,
        calendar=calendar,
        cfg=cfg,
        weights=dict(benchmark.weights),
        notional=float(benchmark.notional or benchmark.market_value),
    )
