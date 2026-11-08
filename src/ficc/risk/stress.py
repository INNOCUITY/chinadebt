"""压力测试：假设情景（平行/陡平/蝶式）+ 历史情景重演（计划 §12.12-B）。

对同一固定面值组合，每个情景同时给出：

- **全重估**：用冲击后的曲线逐券重估 T+N 结算日全价，
  ``pnl_full = Σ face/100 × (dirty_shocked − dirty_base)``；
- **KRD 近似**：``pnl_krd = −Σ_b KRD_b × Δz_b``（关键期限 KRD 元/bp；Δz 为
  **即期曲线**关键期限零息利率变化，bp——KRD 定义在即期曲线上，用 YTM 差会
  引入 YTM→即期映射误差）；
- 差异 ``diff = pnl_full − pnl_krd``、占 NAV 的 bp、以及
  ``error_ratio = diff / |pnl_full|``（近似误差占情景 PnL 的比例）。

情景定义
--------
1. ``parallel``：``Curve.shift(bp)``，``bp`` 可带符号（配置里有 ±100bp）；
2. ``steepen`` / ``flatten`` / ``butterfly``：``Curve.shock(kind, bp)``，
   ``bp`` 为**幅度**（≥0），方向由 ``kind`` 表达（与配置注释一致）；
3. ``historical``：取配置窗口 ``[start, end]`` 内、长度为 ``window`` 个交易日
   的**累计曲线变动** ``Δy = y_i − y_{i−window}``（逐期限节点），把该变动整体
   叠加到**当前**曲线上重演，取对当前组合**最不利（全重估 PnL 最小）**的那个
   路径。定义是「历史变动路径重演」而不是「历史当日重复」：只搬运曲线变动，
   不搬历史价位。

   默认**不使用分析日之后的数据**：``end`` 会被截断到 ``curve.as_of``；若窗口
   整段晚于分析日则返回 ``executed=False`` 的行（不静默当 0 处理）。确实要做
   「把未来已实现路径当假设情景」时，显式传 ``allow_future_history=True``，
   返回行的 ``note`` 会标注该假设。

单位约定：金额元、面值元、价格元/百元面值、收益率百分数、KRD 元/bp（多头为正）。
``diff_bp = diff / NAV × 1e4``；NAV 为分析日固定组合全价市值（已扣到结算日）。
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any

import numpy as np
import pandas as pd

from ficc.config import Config
from ficc.curve.bootstrap import bootstrap_spot
from ficc.curve.curve import Curve
from ficc.data.schema import BondStatic
from ficc.portfolio.constraints import project_risk
from ficc.portfolio.dates import remaining_years
from ficc.pricing.bond import dirty_price_from_ytm
from ficc.risk.var import (
    _as_date,
    _bond_map,
    _mark_portfolio,
    _normalise_panel,
    _resolve_config,
    _validate_faces,
)

__all__ = ["StressResult", "stress_test"]

_SHAPE_SCENARIOS = ("steepen", "flatten", "butterfly")
_EPS = 1e-9


@dataclass(frozen=True)
class StressResult:
    """单一压力情景的结果。

    Attributes
    ----------
    scenario : str
        情景名（配置里的 ``name``）。
    pnl_full : float
        全重估情景 PnL（元）。
    pnl_krd : float
        KRD 近似情景 PnL（元）。
    diff : float
        ``pnl_full − pnl_krd``（元）。
    diff_bp : float
        差异占 NAV 的 bp（``diff / NAV × 1e4``）。
    error_ratio : float | None
        ``|diff| / |pnl_full|``；``pnl_full`` 为 0 时为 None。
    nav : float
        分析日固定组合全价市值（元），用于 bp 换算。
    executed : bool
        是否实际执行（历史窗口超出可用数据/分析日时显式 False，不静默当 0）。
    note : str
        情景定义与假设披露（含历史重演窗口、未来数据假设等）。
    """

    scenario: str
    pnl_full: float
    pnl_krd: float
    diff: float
    diff_bp: float
    error_ratio: float | None = None
    nav: float = 0.0
    executed: bool = True
    note: str = ""


def _curve_method_for_replay(curve: Curve) -> str:
    """历史重演需要能由节点重建的插值曲线；参数化曲线降级 hermite。"""
    method = str(curve.method)
    return method if method in ("linear", "hermite") else "hermite"


def _evaluate_shock(
    *,
    faces: dict[str, float],
    bond_by_code: dict[str, BondStatic],
    base_curve: Curve,
    shocked_curve: Curve,
    dirty_base: dict[str, float],
    settlement: date,
    krd: dict[float, float],
    key_tenors: Sequence[float],
    calendar: Any,
    cfg: Config,
) -> tuple[float, float, float]:
    """返回 ``(pnl_full, pnl_krd, max_abs_delta_bp)``。

    KRD 近似用**即期曲线**关键期限零息利率变化：对基准/冲击 YTM 曲线分别
    bootstrap（同一结算日），取关键期限 ``zero_rate`` 之差（bp）。
    """
    pnl_full = 0.0
    for code, face in faces.items():
        if face <= 0.0:
            continue
        bond = bond_by_code[code]
        remaining = remaining_years(bond, settlement)
        if remaining <= 0.0:
            raise ValueError(f"{code}: 结算日 {settlement} 剩余期限 ≤ 0，无法压力重估")
        shocked_dirty = dirty_price_from_ytm(
            bond,
            settlement,
            float(shocked_curve.rate(remaining)),
            cfg=cfg,
        )
        pnl_full += face / 100.0 * (shocked_dirty - dirty_base[code])
    spot_base = bootstrap_spot(base_curve, settlement=settlement, calendar=calendar)
    spot_shocked = bootstrap_spot(shocked_curve, settlement=settlement, calendar=calendar)
    delta_bp = np.asarray(
        [
            (float(spot_shocked.zero_rate(tenor)) - float(spot_base.zero_rate(tenor))) * 100.0
            for tenor in key_tenors
        ],
        dtype=float,
    )
    krd_vector = np.asarray([float(krd[tenor]) for tenor in key_tenors], dtype=float)
    pnl_krd = float(-np.dot(krd_vector, delta_bp))
    return float(pnl_full), pnl_krd, float(np.max(np.abs(delta_bp))) if delta_bp.size else 0.0


def _make_result(
    *,
    name: str,
    pnl_full: float,
    pnl_krd: float,
    nav: float,
    note: str,
    executed: bool = True,
) -> StressResult:
    diff = float(pnl_full - pnl_krd)
    diff_bp = float(diff / nav * 1e4) if nav > _EPS else 0.0
    error_ratio = float(abs(diff) / abs(pnl_full)) if abs(pnl_full) > _EPS else None
    return StressResult(
        scenario=name,
        pnl_full=float(pnl_full),
        pnl_krd=float(pnl_krd),
        diff=diff,
        diff_bp=diff_bp,
        error_ratio=error_ratio,
        nav=float(nav),
        executed=bool(executed),
        note=note,
    )


def _historical_scenario(
    scenario: Mapping[str, Any],
    *,
    name: str,
    faces: dict[str, float],
    bond_by_code: dict[str, BondStatic],
    base_curve: Curve,
    dirty_base: dict[str, float],
    settlement: date,
    krd: dict[float, float],
    key_tenors: Sequence[float],
    nav: float,
    calendar: Any,
    panel: pd.DataFrame | None,
    allow_future_history: bool,
    cfg: Config,
) -> StressResult:
    if panel is None:
        raise ValueError(
            f"历史情景 {name!r} 需要 panel（历史曲线面板）；stress_test(..., panel=panel)"
        )
    try:
        start = _as_date(scenario["start"], field=f"scenario[{name}].start")
        end = _as_date(scenario["end"], field=f"scenario[{name}].end")
    except KeyError as exc:
        raise ValueError(f"历史情景 {name!r} 缺少 start/end 字段") from exc
    window = int(scenario.get("window", 1))
    if window < 1:
        raise ValueError(f"历史情景 {name!r} 的 window 必须 >= 1，实际 {window!r}")

    panel_frame = _normalise_panel(panel)
    panel_dates = list(panel_frame.index)
    as_of = base_curve.as_of
    clipped_end = end if allow_future_history else min(end, as_of)
    usable = [day for day in panel_dates if start <= day <= clipped_end]
    if start > clipped_end or len(usable) < window + 1:
        reason = (
            f"窗口 [{start}, {end}] 晚于分析日 {as_of}，未执行（避免未来数据）"
            if start > as_of
            else f"窗口 [{start}, {end}] 可用交易日 {len(usable)} 个 < window+1={window + 1}，未执行"
        )
        return _make_result(
            name=name,
            pnl_full=0.0,
            pnl_krd=0.0,
            nav=nav,
            note=reason,
            executed=False,
        )

    tenors = panel_frame.columns.to_numpy(dtype=float)
    rates = np.asarray([panel_frame.loc[day].to_numpy(dtype=float) for day in usable], dtype=float)
    if not np.isfinite(rates).all():
        raise ValueError(f"历史情景 {name!r} 的 panel 窗口含 NaN/inf")
    base_rates = np.asarray(base_curve.rate(tenors), dtype=float)
    replay_method = _curve_method_for_replay(base_curve)
    future_used = bool(allow_future_history and any(day > as_of for day in usable))

    worst: tuple[float, float, date, date, float] | None = None
    for index in range(window, len(usable)):
        delta = rates[index] - rates[index - window]
        shocked_curve = Curve(
            as_of=as_of,
            tenors=tenors.copy(),
            rates=base_rates + delta,
            method=replay_method,  # type: ignore[arg-type]
        )
        pnl_full, pnl_krd, _ = _evaluate_shock(
            faces=faces,
            bond_by_code=bond_by_code,
            base_curve=base_curve,
            shocked_curve=shocked_curve,
            dirty_base=dirty_base,
            settlement=settlement,
            krd=krd,
            key_tenors=key_tenors,
            calendar=calendar,
            cfg=cfg,
        )
        if worst is None or pnl_full < worst[0]:
            worst = (pnl_full, pnl_krd, usable[index - window], usable[index], pnl_full)
    assert worst is not None  # len(usable) >= window+1 保证至少一条路径
    pnl_full, pnl_krd, from_day, to_day, _ = worst
    note = (
        f"历史变动重演：{from_day}→{to_day} 共 {window} 个交易日的曲线变动，"
        f"叠加到分析日 {as_of} 曲线上，取当前组合最不利路径"
    )
    if future_used:
        note += "；⚠️ 该窗口晚于分析日，属显式假设情景（allow_future_history=True）"
    elif end > as_of:
        note += f"；原窗口 end={end} 已截断到分析日 {as_of}（避免未来数据）"
    return _make_result(
        name=name,
        pnl_full=pnl_full,
        pnl_krd=pnl_krd,
        nav=nav,
        note=note,
        executed=True,
    )


def stress_test(
    faces: Any,
    bonds: Sequence[BondStatic],
    curve: Curve,
    *,
    scenarios: Sequence[Mapping[str, Any]] | None = None,
    calendar: Any,
    cfg: Config | None = None,
    panel: pd.DataFrame | None = None,
    allow_future_history: bool = False,
) -> list[StressResult]:
    """对固定面值组合执行压力情景（全重估 + KRD 近似并列）。

    Parameters
    ----------
    faces : Mapping[str, float]
        固定面值（元）。
    bonds : Sequence[BondStatic]
        债券条款；须覆盖 faces 的全部 code。
    curve : Curve
        分析日曲线（``as_of`` 为分析日）；情景在它之上施加冲击。
    scenarios : Sequence[Mapping] | None
        情景列表；``None`` 时用 ``cfg.risk.stress_scenarios``。每项需含
        ``type``（parallel/steepen/flatten/butterfly/historical）与 ``name``；
        假设情景用 ``bp``，历史情景用 ``start``/``end``/可选 ``window``。
    calendar : Any
        ``TradingCalendar`` 或同接口对象（T+N 结算日）。
    cfg : Config | None
    panel : DataFrame | None
        历史曲线面板（历史情景必需；index=日期、columns=期限）。
    allow_future_history : bool, default False
        历史窗口晚于分析日时是否允许作为**显式假设**重演；默认 False（避免未来
        数据），超窗情景返回 ``executed=False``。

    Returns
    -------
    list[StressResult]
        与 ``scenarios`` 同序；未执行的历史情景也在列表里（``executed=False``）。
    """
    resolved = _resolve_config(cfg)
    if not isinstance(curve, Curve):
        raise TypeError(f"curve 必须是 Curve，实际 {type(curve).__name__}")
    bond_by_code = _bond_map(list(bonds))
    cleaned = _validate_faces(faces, bond_by_code)
    dirty_base, nav, settlement = _mark_portfolio(
        cleaned, bond_by_code, curve, calendar=calendar, cfg=resolved
    )
    risk = project_risk(
        cleaned,
        list(bond_by_code.values()),
        curve,
        calendar=calendar,
        cfg=resolved,
    )
    key_tenors = sorted(float(t) for t in resolved.risk.key_tenors_krd)
    if not key_tenors:
        raise ValueError("cfg.risk.key_tenors_krd 为空")
    scenario_list = list(resolved.risk.stress_scenarios) if scenarios is None else list(scenarios)
    if not scenario_list:
        raise ValueError("scenarios 不能为空（cfg.risk.stress_scenarios 也为空）")

    results: list[StressResult] = []
    for position, raw in enumerate(scenario_list, start=1):
        if not isinstance(raw, Mapping):
            raise TypeError(f"scenarios[{position}] 必须是 Mapping，实际 {type(raw).__name__}")
        scenario = dict(raw)
        name = str(scenario.get("name") or f"scenario_{position}")
        kind = str(scenario.get("type") or "").strip().lower()
        if kind == "historical":
            results.append(
                _historical_scenario(
                    scenario,
                    name=name,
                    faces=cleaned,
                    bond_by_code=bond_by_code,
                    base_curve=curve,
                    dirty_base=dirty_base,
                    settlement=settlement,
                    krd=dict(risk.krd),
                    key_tenors=key_tenors,
                    nav=nav,
                    calendar=calendar,
                    panel=panel,
                    allow_future_history=allow_future_history,
                    cfg=resolved,
                )
            )
            continue
        if kind == "parallel":
            if "bp" not in scenario:
                raise ValueError(f"情景 {name!r} 缺少 bp")
            bp = float(scenario["bp"])
            if not np.isfinite(bp):
                raise ValueError(f"情景 {name!r} 的 bp 非有限: {scenario['bp']!r}")
            shocked_curve = curve.shift(bp)
            note = f"平行移动 {bp:+g}bp（Curve.shift；带符号）"
        elif kind in _SHAPE_SCENARIOS:
            if "bp" not in scenario:
                raise ValueError(f"情景 {name!r} 缺少 bp")
            bp = float(scenario["bp"])
            if not np.isfinite(bp) or bp < 0.0:
                raise ValueError(
                    f"情景 {name!r} 的 {kind} bp 是幅度，必须是非负有限数，"
                    f"实际 {scenario['bp']!r}（方向由 type 表达）"
                )
            shocked_curve = curve.shock(kind, bp)  # type: ignore[arg-type]
            note = f"形状冲击 {kind} {bp:g}bp（Curve.shock；方向由 type 表达）"
        else:
            raise ValueError(
                f"未知情景类型 {kind!r}（情景 {name!r}）；支持 parallel/steepen/"
                "flatten/butterfly/historical"
            )
        pnl_full, pnl_krd, _ = _evaluate_shock(
            faces=cleaned,
            bond_by_code=bond_by_code,
            base_curve=curve,
            shocked_curve=shocked_curve,
            dirty_base=dirty_base,
            settlement=settlement,
            krd=dict(risk.krd),
            key_tenors=key_tenors,
            calendar=calendar,
            cfg=resolved,
        )
        results.append(
            _make_result(
                name=name,
                pnl_full=pnl_full,
                pnl_krd=pnl_krd,
                nav=nav,
                note=note,
                executed=True,
            )
        )
    return results
