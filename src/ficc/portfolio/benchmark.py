"""基准组合：合成平价券宇宙 + 阶梯市值权重基准（Phase 2 第 3 步）。

设计口径
--------
- **一次生成、全回测复用**：:func:`build_universe_bonds` 只在回测起点按
  曲线 YTM 生成一批合成平价券（票面=该期限 YTM、起息=as_of、到期=as_of+tenor 年），
  之后引擎持有同一批 :class:`BondStatic` 按原券老化，不按新曲线重建（避免
  "每天换一张新券"的隐性交易/重定价）。
- **市值权重**：:func:`build_benchmark` 按 ``cfg.benchmark.ladder_weights`` 把
  ``notional`` 分配到各腿，``face = market_value / (dirty / 100)``。
- **DV01 / KRD 单位**：``元/bp``（全价口径，**多头为正**）。定价层
  :func:`ficc.pricing.analytics.key_rate_durations` 的符号约定是多头为负，
  本模块统一取负；每腿的总 DV01 用 YTM 解析口径 :func:`ficc.pricing.dv01`，
  其在关键期限上的**分布形状**用 bootstrap 即期曲线的 KRD 份额确定，并缩放到
  该腿 DV01 总量，因此 ``Σ krd == total_dv01``（便于 :func:`relative_tilt`
  做严格的相对基准 DV01 中性）。
- ``krd`` 的桶固定为 ``cfg.risk.key_tenors_krd``，未覆盖的桶补 0。

已知近似
--------
- 合成券的 ``times`` 走 bootstrap 的 ACT/365 名义年限近似，不是中债官方
  "即期对账"口径；
- 基准 KRD 是**即期曲线**口径，与 YTM DV01 存在 <2% 的口径差；本模块用总量
  对齐、形状沿用即期 KRD 的方式消除该差，报告需注明。
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
from ficc.portfolio.types import BenchmarkLeg, BenchmarkPortfolio
from ficc.pricing.analytics import dv01, key_rate_durations, mod_duration
from ficc.pricing.bond import dirty_price_from_ytm

__all__ = ["build_benchmark", "build_universe_bonds"]

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

    Parameters
    ----------
    curve : Curve
        回测起点（或该券起息日）的 YTM 曲线；票面 = ``curve.rate(tenor)``。
    as_of : date
        券的起息日，同时进入 code（``SYN-{tenor:g}Y-{as_of:%Y%m%d}``）。
    cfg : Config | None
        显式配置；``None`` 时才读全局配置。

    Returns
    -------
    tuple[BondStatic, ...]
        按 ``universe`` 顺序排列；同一 tenor 去重。

    Raises
    ------
    ValueError
        ``universe`` 为空/非正期限/重复，或曲线在该期限无有效收益率。
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
        coupon_pct = max(ytm, 0.0)
        bond = BondStatic(
            code=f"SYN-{tenor:g}Y-{start:%Y%m%d}",
            issue_date=start,
            maturity_date=_add_years(start, tenor),
            coupon_pct=float(coupon_pct),
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


def build_benchmark(
    curve: Curve,
    as_of: date,
    *,
    bonds: tuple[BondStatic, ...] | None = None,
    cfg: Config | None = None,
) -> BenchmarkPortfolio:
    """构造阶梯市值权重基准（``notional`` 按 ``ladder_weights`` 分配）。

    Parameters
    ----------
    curve : Curve
        ``as_of`` 当天的 YTM 曲线。
    as_of : date
        基准日（= 券起息日）。
    bonds : tuple[BondStatic, ...] | None
        复用一批已生成的合成券（推荐：引擎全回测传同一批，按原券老化）；
        ``None`` 时按当前曲线新生成。
    cfg : Config | None
        显式配置；``None`` 时才读全局配置。

    Returns
    -------
    BenchmarkPortfolio
        ``legs`` 每腿含 face/market_value/dv01/krd；``krd`` 桶 =
        ``cfg.risk.key_tenors_krd``（未覆盖补 0）；``total_dv01 = Σ leg.dv01``。

    Raises
    ------
    ValueError
        权重和≠1、腿期限不在 universe、总 DV01≤0、缺券等。
    """
    resolved = _resolve_cfg(cfg)
    start = _normalise_date(as_of)
    weights = _validate_weights(
        {float(k): float(v) for k, v in resolved.benchmark.ladder_weights.items()},
        [float(t) for t in resolved.portfolio.universe],
    )
    universe_bonds = (
        build_universe_bonds(curve, start, cfg=resolved) if bonds is None else tuple(bonds)
    )
    if not universe_bonds:
        raise ValueError("bonds 为空")
    bond_by_tenor: dict[float, BondStatic] = {}
    for bond in universe_bonds:
        tenor = round(_bond_tenor(bond), 10)
        if tenor in bond_by_tenor:
            raise ValueError(f"bonds 中期限 {tenor}Y 重复")
        bond_by_tenor[tenor] = bond
    missing = sorted(set(weights) - set(bond_by_tenor))
    if missing:
        raise ValueError(f"bonds 缺少 ladder_weights 对应的期限 {missing}Y")

    notional = float(resolved.benchmark.notional)
    if not np.isfinite(notional) or notional <= 0:
        raise ValueError(f"benchmark.notional 必须为正数，实际 {notional!r}")
    key_tenors = [float(t) for t in resolved.risk.key_tenors_krd]
    if not key_tenors:
        raise ValueError("cfg.risk.key_tenors_krd 为空")

    spot = bootstrap_spot(curve)
    settlement = spot.settlement_date
    if settlement is None:  # pragma: no cover - SpotCurve 必填
        raise ValueError("bootstrap_spot 未返回 settlement_date，无法计算 KRD")
    legs: list[BenchmarkLeg] = []
    krd: dict[float, float] = {tenor: 0.0 for tenor in key_tenors}
    total_dv01 = 0.0
    weighted_duration = 0.0
    for tenor in sorted(weights):
        weight = weights[tenor]
        bond = bond_by_tenor[tenor]
        market_value = notional * weight
        ytm = float(curve.rate(tenor))
        # 面值按**起息日（as_of）**全价折算：合成平价券 coupon=YTM 且 AI=0 → dirty=100
        dirty_issue = float(dirty_price_from_ytm(bond, start, ytm, cfg=resolved))
        if not np.isfinite(dirty_issue) or dirty_issue <= 0:
            raise ValueError(f"{tenor}Y 合成券起息日全价非正: {dirty_issue!r}")
        face = market_value / (dirty_issue / 100.0)
        # YTM DV01 仅用于交叉校验与 duration 汇总；KRD/总 DV01 以即期口径为准
        leg_dv01_ytm = float(dv01(bond, settlement, ytm, cfg=resolved)) * face / 100.0
        if not np.isfinite(leg_dv01_ytm) or leg_dv01_ytm <= 0:
            raise ValueError(f"{tenor}Y 腿 YTM DV01 非正: {leg_dv01_ytm!r}")
        leg_dv01 = leg_dv01_ytm  # 占位，稍后用即期 KRD 合计覆盖
        spot_krd = key_rate_durations(bond, settlement, spot, key_tenors=key_tenors, cfg=resolved)
        # 定价层 KRD 符号：多头为负；转成"多头为正"的元/bp。
        # 注意：**不做"按 YTM DV01 重新缩放"**——KRD 与总 DV01 必须同源于即期曲线，
        # 否则基准的 KRD 无法被组合构建（同样用即期 KRD）精确复现（历史 bug：
        # 缩放后 4 腿基准无法用 4 只券还原，残差 ~0.9%，并导致面值取整偏差）。
        positive_krd = {
            float(bucket): -float(value) * face / 100.0 for bucket, value in spot_krd.items()
        }
        spot_total = float(sum(positive_krd.values()))
        if spot_total > 0:
            leg_krd = {float(bucket): positive_krd.get(float(bucket), 0.0) for bucket in key_tenors}
        else:  # pragma: no cover - 防御式
            leg_krd = {float(bucket): 0.0 for bucket in key_tenors}
            leg_krd[key_tenors[0]] = leg_dv01
        leg_dv01 = spot_total  # 即期 KRD 合计（与 krd 同源）
        for bucket, value in leg_krd.items():
            krd[bucket] = krd.get(bucket, 0.0) + value
        total_dv01 += leg_dv01
        weighted_duration += market_value * float(mod_duration(bond, settlement, ytm, cfg=resolved))
        legs.append(
            BenchmarkLeg(
                bond=bond,
                face_value=float(face),
                market_value=float(market_value),
                dv01=leg_dv01,
                krd=leg_krd,
            )
        )

    krd = {float(tenor): float(krd.get(float(tenor), 0.0)) for tenor in key_tenors}
    total_dv01 = float(total_dv01)
    if not np.isfinite(total_dv01) or total_dv01 <= 0:
        raise ValueError(f"基准 total_dv01 必须为正数，实际 {total_dv01!r}")
    krd_total = float(sum(krd.values()))
    if abs(krd_total - total_dv01) > 1e-6 * max(total_dv01, 1.0):  # pragma: no cover
        raise RuntimeError(f"KRD 合计 {krd_total} 与 total_dv01 {total_dv01} 不一致")
    duration = weighted_duration / notional if notional else float("nan")
    benchmark = BenchmarkPortfolio(
        as_of=start,
        legs=tuple(legs),
        market_value=float(sum(leg.market_value for leg in legs)),
        total_dv01=total_dv01,
        duration=float(duration),
        krd=krd,
        weights={float(tenor): float(weight) for tenor, weight in weights.items()},
    )
    log.info(
        "基准组合: %d 腿, 市值 %.2f, 总DV01 %.2f 元/bp, KRD桶 %s, duration %.3fY",
        len(benchmark.legs),
        benchmark.market_value,
        benchmark.total_dv01,
        sorted(benchmark.krd),
        benchmark.duration,
    )
    return benchmark
