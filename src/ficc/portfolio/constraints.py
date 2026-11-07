"""组合约束（Phase 2 第 3 步）：9 项约束与"相对基准 DV01 中性"的正式定义。

相对基准 DV01 中性
------------------
现券**禁裸空**（``portfolio.allow_short_cash_bond = false``），因此全多头组合的绝对
DV01 不可能为 0。本模块把"中性"定义为**KRD 向量贴近基准 KRD 向量 + 有界偏离**，
并同时受绝对 DV01 上限约束：

1. ``relative_krd``：对每个关键期限桶 ``b``，
   ``|KRD_p(b) − KRD_b(b)| ≤ relative_krd_tol × benchmark.total_dv01``
   （默认 30% × 基准总 DV01）；
2. ``total_dv01_ratio``：``KRD_p 总和 / benchmark.total_dv01`` 落在
   ``[total_dv01_ratio_min, total_dv01_ratio_max]``（默认 0.70–1.30）；
3. ``duration_band``：``|duration_p − duration_b| ≤ duration_tol × duration_b``
   （默认 ±20%，duration 为市值加权修正久期）；
4. ``absolute_dv01``：``total_dv01 ≤ cfg.risk.limits.max_dv01``；
5. ``krd_share``：``max_b |KRD_p(b)| / Σ_b |KRD_p(b)| ≤ cfg.risk.limits.max_krd_share``
   （集中度）；
6. ``single_bond_weight``：``market_value_i / nav ≤ min(cfg.portfolio.\
max_single_bond_weight, cfg.risk.limits.max_single_bond_weight)``；
7. ``leverage``：``market_value / nav ≤ cfg.backtest.max_leverage``；
8. ``cash_floor``：``cash ≥ constraints.min_cash_fraction × NAV``（负值=允许的回购融资上限）；
9. ``drawdown_stop``：``nav / nav_high_water − 1 ≥ −cfg.risk.limits.max_drawdown_stop``。

单位约定
--------
- 金额：元；面值：元；价格：元/百元面值。
- KRD / total_dv01：**元/bp，多头为正**（= 定价层 ``key_rate_durations`` 输出取负，
  后者对多头为负；这样与 ``types.BenchmarkPortfolio.total_dv01`` 的"多头为正"一致）。
- 关键期限桶默认取 ``cfg.risk.key_tenors_krd``（与基准 ``benchmark.krd`` 同一组桶，
  默认 1/2/3/5/7/10/15/20/30Y），可由 ``key_tenors`` 覆盖；KRD 用 ``bootstrap_spot(curve)`` 的即期曲线单点冲击计算。

输入兼容
--------
:func:`portfolio_risk` 的第一个参数可以是：

- ``LedgerSnapshot``（鸭子类型：有 ``positions`` / ``cash`` / ``nav``）——推荐，
  这样 ``cash`` / ``nav`` 取自账本，杠杆/现金下限/回撤约束才有意义；
- 或任意 ``Iterable[PositionView]``——此时 ``cash = 0``、``nav = market_value``，
  调用方可先构造再用 ``dataclasses.replace`` 填入真实 cash/nav。

``PortfolioRisk`` 额外携带 ``bond_weights``（code → market_value/nav），第 6 项
（单券权重）才能独立校验；若手工构造且未提供该字段，则该项按 0 处理（不误报）。

本模块不做交易、不读未来数据：只看调用方给的持仓与决策日曲线。
"""

from __future__ import annotations

import math
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from ficc.config import Config, get_config
from ficc.curve.bootstrap import bootstrap_spot
from ficc.data.schema import BondStatic
from ficc.portfolio.dates import remaining_years, settlement_for
from ficc.portfolio.types import BenchmarkPortfolio
from ficc.pricing.analytics import key_rate_durations, mod_duration
from ficc.pricing.bond import dirty_price_from_ytm

__all__ = [
    "ConstraintReport",
    "ConstraintViolation",
    "PortfolioRisk",
    "check_constraints",
    "portfolio_risk",
]

# 浮点边界容差：约束比较一律用 `<=`（边界通过），超出 eps 才算违反。
_EPS: float = 1e-9


def _resolve_config(cfg: Config | None) -> Config:
    """显式 cfg 优先；``None`` 时读全局配置（不回落硬编码默认值）。"""
    return cfg if cfg is not None else get_config()


def _resolve_key_tenors(key_tenors: Sequence[float] | None, cfg: Config) -> tuple[float, ...]:
    """关键期限桶：显式参数优先，否则用 ``cfg.risk.key_tenors_krd``（升序去重）。"""
    # 默认与基准/风险层契约一致：cfg.risk.key_tenors_krd（benchmark.py 也用这组桶）
    raw = list(key_tenors) if key_tenors is not None else list(cfg.risk.key_tenors_krd)
    tenors = tuple(sorted({float(t) for t in raw}))
    if not tenors:
        raise ValueError("key_tenors 不能为空")
    if any(t <= 0 or not math.isfinite(t) for t in tenors):
        raise ValueError(f"key_tenors 必须为正的有限数，实际 {raw!r}")
    return tenors


def _spot_curve(curve: Any, cfg: Config, calendar: object | None) -> tuple[Any, date]:
    """bootstrap 即期曲线（KRD 的唯一曲线来源；R27：显式走 CN_IB 结算日）。"""
    if not hasattr(curve, "as_of"):
        raise TypeError("curve 缺少必需成员 as_of：需要 ficc.curve.Curve")
    settlement = settlement_for(calendar, curve.as_of, cfg=cfg)
    return bootstrap_spot(curve, settlement=settlement, calendar=calendar), settlement


def _bond_krd_positive(
    bond: BondStatic,
    spot: Any,
    settlement: date,
    key_tenors: Sequence[float],
    *,
    cfg: Config,
) -> dict[float, float]:
    """单券 KRD（**元/bp per 100 元面值，多头为正**）。

    定价层 :func:`key_rate_durations` 对多头返回负值（收益率上行 → 价格下跌），
    本项目组合层约定 DV01/KRD **多头为正**，因此这里统一取负号。
    """
    signed = key_rate_durations(bond, settlement, spot, key_tenors=key_tenors, cfg=cfg)
    return {float(tenor): -float(value) for tenor, value in signed.items()}


# ---------------------------------------------------------------------------
# 数据结构
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class ConstraintViolation:
    """一项约束违反。"""

    code: str
    detail: str
    value: float
    limit: float


@dataclass(frozen=True)
class ConstraintReport:
    """约束检查报告：``ok`` 为真当且仅当无违反项。"""

    as_of: date
    violations: tuple[ConstraintViolation, ...]
    metrics: Mapping[str, float]

    @property
    def ok(self) -> bool:
        return len(self.violations) == 0


@dataclass(frozen=True)
class PortfolioRisk:
    """组合风险视图（金额元；KRD/DV01 元/bp，多头为正）。"""

    total_dv01: float
    krd: Mapping[float, float]
    duration: float
    market_value: float
    cash: float
    nav: float
    # 扩展字段：单券权重（code → market_value/nav），供第 6 项约束独立校验。
    bond_weights: Mapping[str, float] = field(default_factory=dict)


# ---------------------------------------------------------------------------
# 组合风险
# ---------------------------------------------------------------------------
def portfolio_risk(
    positions: Any,
    curve: Any,
    *,
    key_tenors: Sequence[float] | None = None,
    calendar: object | None = None,
    cfg: Config | None = None,
) -> PortfolioRisk:
    """由持仓 + 决策日曲线计算组合风险（KRD/DV01/久期/市值/现金/NAV）。

    Parameters
    ----------
    positions : LedgerSnapshot | Iterable[PositionView]
        推荐传 ``LedgerSnapshot``（用其 ``cash`` / ``nav``）；传 PositionView 序列时
        ``cash=0``、``nav=market_value``。
    curve : Curve
        决策日 YTM 曲线（不得含未来信息）；KRD 经 ``bootstrap_spot(curve)``。
    key_tenors : Sequence[float] | None
        关键期限桶；``None`` 时用 ``cfg.risk.key_tenors_krd``。
    cfg : Config | None

    Returns
    -------
    PortfolioRisk
        ``total_dv01 = Σ_b KRD(b)``；``duration`` 为市值加权修正久期；
        ``krd`` 为各桶 KRD 之和（元/bp，多头为正）。

    Notes
    -----
    - 空组合返回全 0，不会除零；
    - 单券 KRD 的 face 换算：``KRD[元/bp] = KRD_per_100 × face / 100``。
    """
    config = _resolve_config(cfg)
    tenors = _resolve_key_tenors(key_tenors, config)

    if hasattr(positions, "positions") and hasattr(positions, "cash"):
        position_list = list(positions.positions)
        cash = float(positions.cash)
        nav = float(positions.nav)
    else:
        position_list = list(positions or [])
        cash = 0.0
        nav = 0.0

    spot, settlement = _spot_curve(curve, config, calendar)

    krd_accum: dict[float, float] = {tenor: 0.0 for tenor in tenors}
    market_value = 0.0
    weighted_duration = 0.0
    weights: dict[str, float] = {}

    for position in position_list:
        bond = position.bond
        face = float(position.face_value)
        if face < 0.0:
            raise ValueError(f"{bond.code}: 持仓面值不能为负，实际 {face}")
        mv = float(position.market_value)
        market_value += mv
        remaining = remaining_years(bond, settlement)
        if remaining <= 0.0:
            raise ValueError(f"{bond.code}: 剩余期限非正（{remaining}），持仓应已到期")
        per_100 = _bond_krd_positive(bond, spot, settlement, tenors, cfg=config)
        for tenor in tenors:
            krd_accum[tenor] += per_100[tenor] * face / 100.0

        duration = position.mod_duration
        if duration is None:
            duration = mod_duration(bond, settlement, float(curve.rate(remaining)), cfg=config)
        weighted_duration += mv * float(duration)
        if nav > _EPS:
            weights[bond.code] = weights.get(bond.code, 0.0) + mv / nav

    total_dv01 = float(sum(krd_accum.values()))
    duration_value = weighted_duration / market_value if market_value > _EPS else 0.0
    if nav == 0.0:
        nav = market_value
    return PortfolioRisk(
        total_dv01=total_dv01,
        krd={tenor: float(value) for tenor, value in krd_accum.items()},
        duration=float(duration_value),
        market_value=float(market_value),
        cash=float(cash),
        nav=float(nav),
        bond_weights=weights,
    )


def project_risk(
    faces: Mapping[str, float],
    bonds: Sequence[BondStatic],
    curve: Any,
    *,
    calendar: object | None = None,
    cash: float = 0.0,
    pending_cash: float = 0.0,
    nav: float | None = None,
    key_tenors: Sequence[float] | None = None,
    cfg: Config | None = None,
) -> PortfolioRisk:
    """**交易前**按最终面值计算组合风险（R23：成交前统一校验用）。

    ``nav`` 为 ``None`` 时按 trade-date 会计计算：
    ``nav = cash + Σ(face/100 × dirty) + pending_cash``。现金与 pending 由调用方
    传入"交易后"口径（买入则现金不变、pending 变负，或调用方已合并）。
    """
    config = _resolve_config(cfg)
    tenors = _resolve_key_tenors(key_tenors, config)
    spot, settlement = _spot_curve(curve, config, calendar)
    bond_by_code = {bond.code: bond for bond in bonds}

    krd_accum: dict[float, float] = {tenor: 0.0 for tenor in tenors}
    market_value = 0.0
    weighted_duration = 0.0
    mv_by_code: dict[str, float] = {}
    for code, face in faces.items():
        if face < 0:
            raise ValueError(f"{code}: 面值不能为负，实际 {face}")
        if face == 0:
            continue
        bond = bond_by_code.get(code)
        if bond is None:
            raise ValueError(f"faces 中的 {code} 不在 bonds 里")
        remaining = remaining_years(bond, settlement)
        if remaining <= 0:
            raise ValueError(f"{code}: 剩余期限非正（{remaining}）")
        ytm = float(curve.rate(remaining))
        dirty = float(dirty_price_from_ytm(bond, settlement, ytm, cfg=config))
        mv = float(face) / 100.0 * dirty
        per_100 = _bond_krd_positive(bond, spot, settlement, tenors, cfg=config)
        for tenor in tenors:
            krd_accum[tenor] += per_100[tenor] * float(face) / 100.0
        duration = float(mod_duration(bond, settlement, ytm, cfg=config))
        market_value += mv
        weighted_duration += mv * duration
        mv_by_code[code] = mv

    total_dv01 = float(sum(krd_accum.values()))
    duration_value = weighted_duration / market_value if market_value > _EPS else 0.0
    nav_value = float(nav) if nav is not None else float(cash) + market_value + float(pending_cash)
    weights = {code: mv / nav_value for code, mv in mv_by_code.items()} if nav_value > _EPS else {}
    return PortfolioRisk(
        total_dv01=total_dv01,
        krd={tenor: float(value) for tenor, value in krd_accum.items()},
        duration=float(duration_value),
        market_value=float(market_value),
        cash=float(cash),
        nav=nav_value,
        bond_weights=weights,
    )


# ---------------------------------------------------------------------------
# 约束检查
# ---------------------------------------------------------------------------
def _violation(code: str, detail: str, value: float, limit: float) -> ConstraintViolation:
    return ConstraintViolation(code=code, detail=detail, value=float(value), limit=float(limit))


def check_constraints(
    *,
    risk: PortfolioRisk,
    benchmark: BenchmarkPortfolio,
    nav_high_water: float,
    cfg: Config | None = None,
) -> ConstraintReport:
    """检查 9 项组合约束（口径见模块 docstring），返回带全部实际值的报告。

    Parameters
    ----------
    risk : PortfolioRisk
        :func:`portfolio_risk` 的输出（或等价手工构造）。
    benchmark : BenchmarkPortfolio
        基准组合（``total_dv01`` / ``krd`` / ``duration``，单位元/bp、年）。
    nav_high_water : float
        历史 NAV 高水位（用于回撤熔断）；必须有限，``<=0`` 视为完全回撤。
    cfg : Config | None

    Returns
    -------
    ConstraintReport
        ``violations`` 为违反项（9 类 code 见模块 docstring）；``metrics`` 含每项
        实际值、阈值与每桶 KRD 偏离（键名如 ``krd_dev@10``），便于日报。
    """
    config = _resolve_config(cfg)
    constraints = config.constraints
    limits = config.risk.limits
    if not math.isfinite(nav_high_water):
        raise ValueError(f"nav_high_water 必须为有限数，实际 {nav_high_water!r}")

    eps = _EPS
    violations: list[ConstraintViolation] = []
    metrics: dict[str, float] = {}

    benchmark_total = float(benchmark.total_dv01)
    portfolio_total = float(risk.total_dv01)
    metrics["benchmark_total_dv01"] = benchmark_total
    metrics["portfolio_total_dv01"] = portfolio_total

    # ---- 1) relative_krd：单桶偏离 ≤ tol × 基准总 DV01 -------------------
    tol_abs = float(constraints.relative_krd_tol) * abs(benchmark_total)
    metrics["relative_krd_tol_abs"] = tol_abs
    bucket_tenors = sorted(set(risk.krd) | set(benchmark.krd))
    max_dev = 0.0
    for tenor in bucket_tenors:
        portfolio_bucket = float(risk.krd.get(tenor, 0.0))
        benchmark_bucket = float(benchmark.krd.get(tenor, 0.0))
        deviation = abs(portfolio_bucket - benchmark_bucket)
        metrics[f"krd_portfolio@{tenor:g}"] = portfolio_bucket
        metrics[f"krd_benchmark@{tenor:g}"] = benchmark_bucket
        metrics[f"krd_dev@{tenor:g}"] = deviation
        max_dev = max(max_dev, deviation)
        if deviation > tol_abs + eps:
            violations.append(
                _violation(
                    "relative_krd",
                    f"关键期限 {tenor:g}Y：|KRD_p−KRD_b|={deviation:.6g} 元/bp "
                    f"> {constraints.relative_krd_tol:.0%}×基准总 DV01={tol_abs:.6g}",
                    deviation,
                    tol_abs,
                )
            )
    metrics["relative_krd_max_dev"] = max_dev

    if abs(benchmark_total) > eps:
        ratio = portfolio_total / benchmark_total
    else:
        ratio = 1.0 if abs(portfolio_total) <= eps else math.inf
    metrics["total_dv01_ratio"] = ratio
    metrics["total_dv01_ratio_min"] = float(constraints.total_dv01_ratio_min)
    metrics["total_dv01_ratio_max"] = float(constraints.total_dv01_ratio_max)
    if ratio < constraints.total_dv01_ratio_min - eps:
        violations.append(
            _violation(
                "total_dv01_ratio",
                f"总 DV01 比例 {ratio:.6g} < 下限 {constraints.total_dv01_ratio_min:.6g}",
                ratio,
                float(constraints.total_dv01_ratio_min),
            )
        )
    elif ratio > constraints.total_dv01_ratio_max + eps:
        violations.append(
            _violation(
                "total_dv01_ratio",
                f"总 DV01 比例 {ratio:.6g} > 上限 {constraints.total_dv01_ratio_max:.6g}",
                ratio,
                float(constraints.total_dv01_ratio_max),
            )
        )

    # ---- 3) duration_band：|D_p − D_b| ≤ tol × D_b ----------------------
    benchmark_duration = float(benchmark.duration)
    duration_dev = abs(float(risk.duration) - benchmark_duration)
    duration_band = float(constraints.duration_tol) * abs(benchmark_duration)
    metrics["duration"] = float(risk.duration)
    metrics["benchmark_duration"] = benchmark_duration
    metrics["duration_dev"] = duration_dev
    metrics["duration_band_abs"] = duration_band
    if duration_dev > duration_band + eps:
        violations.append(
            _violation(
                "duration_band",
                f"久期偏离 {duration_dev:.6g} 年 > 容差 "
                f"{constraints.duration_tol:.0%}×基准久期 {benchmark_duration:.6g} 年",
                duration_dev,
                duration_band,
            )
        )

    # ---- 4) absolute_dv01 ------------------------------------------------
    abs_limit = float(limits.max_dv01)
    metrics["absolute_dv01"] = portfolio_total
    metrics["absolute_dv01_limit"] = abs_limit
    if portfolio_total > abs_limit + eps:
        violations.append(
            _violation(
                "absolute_dv01",
                f"总 DV01 {portfolio_total:.6g} 元/bp > 上限 {abs_limit:.6g}",
                portfolio_total,
                abs_limit,
            )
        )

    # ---- 5) krd_share：单桶集中度 ---------------------------------------
    total_abs = float(sum(abs(value) for value in risk.krd.values()))
    share = float(max((abs(value) for value in risk.krd.values()), default=0.0)) / (
        total_abs if total_abs > eps else 1.0
    )
    share_limit = float(limits.max_krd_share)
    metrics["krd_share"] = share
    metrics["krd_share_limit"] = share_limit
    if share > share_limit + eps:
        violations.append(
            _violation(
                "krd_share",
                f"最大单桶 KRD 占比 {share:.4f} > 上限 {share_limit:.4f}",
                share,
                share_limit,
            )
        )

    # ---- 6) single_bond_weight ------------------------------------------
    weight_limit = float(
        min(config.portfolio.max_single_bond_weight, limits.max_single_bond_weight)
    )
    max_weight = float(max(risk.bond_weights.values(), default=0.0))
    metrics["max_single_bond_weight"] = max_weight
    metrics["single_bond_weight_limit"] = weight_limit
    if max_weight > weight_limit + eps:
        worst = max(risk.bond_weights, key=risk.bond_weights.get)
        violations.append(
            _violation(
                "single_bond_weight",
                f"单券最大权重 {max_weight:.4f}（{worst}）> 上限 {weight_limit:.4f}",
                max_weight,
                weight_limit,
            )
        )

    # ---- 7) leverage -----------------------------------------------------
    if risk.nav > eps:
        leverage = float(risk.market_value) / float(risk.nav)
    else:
        leverage = 0.0 if abs(risk.market_value) <= eps else math.inf
    leverage_limit = float(config.backtest.max_leverage)
    metrics["leverage"] = leverage
    metrics["leverage_limit"] = leverage_limit
    if leverage > leverage_limit + eps:
        violations.append(
            _violation(
                "leverage",
                f"杠杆（市值/NAV）{leverage:.6g} > 上限 {leverage_limit:.6g}",
                leverage,
                leverage_limit,
            )
        )

    # ---- 8) cash_floor（相对 NAV 的现金预算）-----------------------------
    # 现券禁裸空但允许回购融资，因此现金下限按 NAV 比例设：-0.20 = 允许 20% 融资。
    cash_floor = float(constraints.min_cash_fraction) * float(risk.nav)
    metrics["cash"] = float(risk.cash)
    metrics["cash_floor"] = cash_floor
    metrics["min_cash_fraction"] = float(constraints.min_cash_fraction)
    if float(risk.cash) < cash_floor - eps:
        violations.append(
            _violation(
                "cash_floor",
                f"现金 {risk.cash:.2f} 元 < 下限 {cash_floor:.2f} 元"
                f"（{float(constraints.min_cash_fraction):.0%} × NAV {risk.nav:.2f}）",
                float(risk.cash),
                cash_floor,
            )
        )

    # ---- 9) drawdown_stop ------------------------------------------------
    drawdown = float(risk.nav) / nav_high_water - 1.0 if nav_high_water > eps else -1.0
    drawdown_limit = -float(limits.max_drawdown_stop)
    metrics["drawdown"] = drawdown
    metrics["drawdown_stop"] = drawdown_limit
    metrics["nav"] = float(risk.nav)
    metrics["nav_high_water"] = float(nav_high_water)
    if drawdown < drawdown_limit - eps:
        violations.append(
            _violation(
                "drawdown_stop",
                f"回撤 {drawdown:.4%} 超过熔断线 {drawdown_limit:.4%}",
                drawdown,
                drawdown_limit,
            )
        )

    return ConstraintReport(
        as_of=benchmark.as_of,
        violations=tuple(violations),
        metrics=metrics,
    )
