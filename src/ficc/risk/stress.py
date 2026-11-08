"""压力测试：假设情景（平行/陡平/蝶式）+ 历史情景重演（R40 同源口径）。

R40 的关键修正：**全重估与 KRD 近似必须共用同一价格函数与同一冲击基**。
本模块统一用即期曲线：

- 基准价 ``P0 = price_from_spot(bond, settlement, spot0)``（:mod:`ficc.risk.spot`）；
- 冲击 ``spot_shock(spot0, key_tenors, shifts_bp)``（与 ``key_rate_durations``
  完全相同的“关键点间线性、两端平推”权重）；
- 全重估 ``pnl_full = Σ face/100 × [price_from_spot(spot_shocked) − P0]``；
- KRD 近似 ``pnl_krd = −Σ_k KRD_k × shifts_bp_k``（KRD 元/bp、多头为正；
  ``KRD_k`` 来自同一 ``spot0`` 上 ``key_rate_durations`` 的单点 +1bp 定价差分）。

因此两者之差只剩**真正的二阶/高阶项**（随冲击幅度平方收敛），不再混入
“YTM 定价 vs 即期 KRD”的一阶口径差。基准价、结算日、剩余期限对全重估与
KRD 完全相同。

情景定义（在即期空间重写，保留配置的 YTM 情景语义）
---------------------------------------------------
1. ``parallel``：所有关键点同向 ``bp``（可带符号）。配置的 ±100bp 直接作为
   即期关键点平行移动。
2. ``steepen`` / ``flatten`` / ``butterfly``：期限形状取自
   ``Curve.shock(kind, bp).rate(key_tenors) − curve.rate(key_tenors)``（与配置
   YTM 情景同语义、同幅度），再作为**即期关键点**的 shifts 施加。
3. ``historical``：在配置窗口内取长度为 ``window`` 个交易日的**即期曲线关键点
   累计变动** ``Δz = z_i − z_{i−window}``（对每日 YTM 曲线 bootstrap，按日期
   缓存 ``SpotCurve``；成本约为 1 次 bootstrap/日 + 每个冲击 1 次定价），
   把 ``Δz`` 叠加到**分析日即期曲线**上重演，取当前组合最不利（全重估 PnL
   最小）的路径。默认不使用分析日之后的数据（``end`` 截断到 ``curve.as_of``）；
   整段晚于分析日时返回 ``executed=False``，只有显式
   ``allow_future_history=True`` 才按假设情景重演并标注。

单位约定：金额元、面值元、价格元/百元面值、收益率百分数、KRD 元/bp（多头为正）。
``diff_bp = diff / NAV × 1e4``；NAV 为分析日固定组合即期全价市值（已扣到结算日）。
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
from ficc.portfolio.dates import settlement_for
from ficc.risk.spot import price_from_spot, spot_shock
from ficc.risk.var import (
    _as_date,
    _bond_map,
    _normalise_panel,
    _panel_curve,
    _portfolio_krd_positive,
    _resolve_config,
    _validate_faces,
)

__all__ = ["StressResult", "stress_test"]

_SHAPE_SCENARIOS = ("steepen", "flatten", "butterfly")
_EPS = 1e-9
_PRICE_FUNCTION = "price_from_spot"
_SHOCK_BASIS = "spot_key_linear_flat"


@dataclass(frozen=True)
class StressResult:
    """单一压力情景的结果（全重估与 KRD 近似同为即期定价/冲击基）。

    Attributes
    ----------
    scenario, pnl_full, pnl_krd, diff, diff_bp
        情景名、全重估 PnL、KRD 近似 PnL、差（元）与差占 NAV bp。
    error_ratio : |diff|/|pnl_full|；``pnl_full`` 为 0 时为 None。
    nav : 分析日固定组合即期全价市值（元）。
    executed : 是否实际执行（历史窗口超窗时显式 False）。
    note : 情景定义与假设披露。
    price_function, shock_basis : R40 口径说明（全重估与 KRD 共用）。
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
    price_function: str = _PRICE_FUNCTION
    shock_basis: str = _SHOCK_BASIS


def _base_state(
    faces: dict[str, float],
    bond_by_code: dict[str, BondStatic],
    curve: Curve,
    *,
    calendar: Any,
    cfg: Config,
    key_tenors: Sequence[float],
) -> tuple[date, Any, dict[str, float], float, dict[float, float]]:
    """分析日基准：结算日、即期曲线、逐券即期全价、NAV、组合 KRD（元/bp）。"""
    settlement = settlement_for(calendar, curve.as_of, cfg=cfg)
    spot = bootstrap_spot(curve, settlement=settlement, calendar=calendar)
    base_prices = {
        code: price_from_spot(bond_by_code[code], settlement, spot, cfg=cfg)
        for code, face in faces.items()
        if face > 0.0
    }
    nav = float(sum(faces[code] / 100.0 * base_prices[code] for code in base_prices))
    if nav <= _EPS:
        raise ValueError(f"组合即期市值为非正：{nav!r}")
    krd = _portfolio_krd_positive(faces, bond_by_code, settlement, spot, key_tenors, cfg=cfg)
    return settlement, spot, base_prices, nav, krd


def _evaluate_shock(
    *,
    faces: dict[str, float],
    bond_by_code: dict[str, BondStatic],
    settlement: date,
    base_spot: Any,
    base_prices: dict[str, float],
    krd: dict[float, float],
    key_tenors: Sequence[float],
    shifts_bp: np.ndarray,
    cfg: Config,
) -> tuple[float, float]:
    """同一即期冲击基下返回 ``(pnl_full, pnl_krd)``（元）。"""
    shifts = np.asarray(shifts_bp, dtype=float)
    shocked_spot = spot_shock(base_spot, key_tenors, shifts)
    pnl_full = 0.0
    for code, face in faces.items():
        if face <= 0.0:
            continue
        pnl_full += (
            face
            / 100.0
            * (
                price_from_spot(bond_by_code[code], settlement, shocked_spot, cfg=cfg)
                - base_prices[code]
            )
        )
    krd_vector = np.asarray([float(krd[float(tenor)]) for tenor in key_tenors], dtype=float)
    pnl_krd = float(-np.dot(krd_vector, shifts))
    return float(pnl_full), pnl_krd


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


def _shape_shifts_bp(curve: Curve, kind: str, bp: float, key_tenors: Sequence[float]) -> np.ndarray:
    """把配置的 YTM 形状情景（Curve.shock）映射为即期关键点 shifts（bp）。"""
    shocked_curve = curve.shock(kind, bp)  # type: ignore[arg-type]
    return np.asarray(
        [
            (float(shocked_curve.rate(tenor)) - float(curve.rate(tenor))) * 100.0
            for tenor in key_tenors
        ],
        dtype=float,
    )


def _historical_scenario(
    scenario: Mapping[str, Any],
    *,
    name: str,
    faces: dict[str, float],
    bond_by_code: dict[str, BondStatic],
    base_curve: Curve,
    settlement: date,
    base_spot: Any,
    base_prices: dict[str, float],
    krd: dict[float, float],
    key_tenors: Sequence[float],
    nav: float,
    panel: pd.DataFrame | None,
    allow_future_history: bool,
    calendar: Any,
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
            name=name, pnl_full=0.0, pnl_krd=0.0, nav=nav, note=reason, executed=False
        )

    # 历史日 spot 序列：每个交易日 1 次 bootstrap（缓存），之后只做冲击重估。
    spot_cache: dict[date, Any] = {}

    def spot_for(day: date) -> Any:
        if day not in spot_cache:
            spot_cache[day] = bootstrap_spot(
                _panel_curve(panel_frame, day),
                settlement=settlement_for(calendar, day, cfg=cfg),
                calendar=calendar,
            )
        return spot_cache[day]

    series = [spot_for(day) for day in usable]
    future_used = bool(allow_future_history and any(day > as_of for day in usable))
    worst: tuple[float, float, date, date] | None = None
    for index in range(window, len(usable)):
        shifts = np.asarray(
            [
                (
                    float(series[index].zero_rate(tenor))
                    - float(series[index - window].zero_rate(tenor))
                )
                * 100.0
                for tenor in key_tenors
            ],
            dtype=float,
        )
        pnl_full, pnl_krd = _evaluate_shock(
            faces=faces,
            bond_by_code=bond_by_code,
            settlement=settlement,
            base_spot=base_spot,
            base_prices=base_prices,
            krd=krd,
            key_tenors=key_tenors,
            shifts_bp=shifts,
            cfg=cfg,
        )
        if worst is None or pnl_full < worst[0]:
            worst = (pnl_full, pnl_krd, usable[index - window], usable[index])
    assert worst is not None
    pnl_full, pnl_krd, from_day, to_day = worst
    note = (
        f"历史即期曲线变动重演：{from_day}→{to_day} 共 {window} 个交易日的关键点 Δz，"
        f"叠加到分析日 {as_of} 即期曲线上，取当前组合最不利路径"
        "（历史日 spot 由当日 YTM 曲线 bootstrap 并缓存）"
    )
    if future_used:
        note += "；⚠️ 该窗口晚于分析日，属显式假设情景（allow_future_history=True）"
    elif end > as_of:
        note += f"；原窗口 end={end} 已截断到分析日 {as_of}（避免未来数据）"
    return _make_result(
        name=name, pnl_full=pnl_full, pnl_krd=pnl_krd, nav=nav, note=note, executed=True
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
    """对固定面值组合执行压力情景（全重估 + KRD 近似，同一即期定价/冲击基）。

    Parameters
    ----------
    faces : Mapping[str, float]
        固定面值（元）。
    bonds : Sequence[BondStatic]
        债券条款；须覆盖 faces 的全部 code。
    curve : Curve
        分析日 YTM 曲线（``as_of`` 为分析日）。它用于：历史情景的 bootstrap 与
        形状情景的期限形状（``Curve.shock`` 语义）；分析日即期曲线由它
        bootstrap 得到。**全重估与 KRD 都只在这个即期曲线上做冲击**。
    scenarios : Sequence[Mapping] | None
        ``None`` 时用 ``cfg.risk.stress_scenarios``。每项含 ``type``
        （parallel/steepen/flatten/butterfly/historical）与 ``name``；假设情景用
        ``bp``，历史情景用 ``start``/``end``/可选 ``window``。
    calendar : Any
        ``TradingCalendar`` 或同接口对象（T+N 结算日）。
    cfg : Config | None
    panel : DataFrame | None
        历史曲线面板（历史情景必需）。
    allow_future_history : bool, default False
        历史窗口晚于分析日时是否作为显式假设重演。

    Returns
    -------
    list[StressResult]
        与 ``scenarios`` 同序；未执行的历史情景保留 ``executed=False`` 行。
    """
    resolved = _resolve_config(cfg)
    if not isinstance(curve, Curve):
        raise TypeError(f"curve 必须是 Curve，实际 {type(curve).__name__}")
    bond_by_code = _bond_map(list(bonds))
    cleaned = _validate_faces(faces, bond_by_code)
    key_tenors = sorted(float(tenor) for tenor in resolved.risk.key_tenors_krd)
    if not key_tenors:
        raise ValueError("cfg.risk.key_tenors_krd 为空")
    settlement, base_spot, base_prices, nav, krd = _base_state(
        cleaned,
        bond_by_code,
        curve,
        calendar=calendar,
        cfg=resolved,
        key_tenors=key_tenors,
    )
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
                    settlement=settlement,
                    base_spot=base_spot,
                    base_prices=base_prices,
                    krd=krd,
                    key_tenors=key_tenors,
                    nav=nav,
                    panel=panel,
                    allow_future_history=allow_future_history,
                    calendar=calendar,
                    cfg=resolved,
                )
            )
            continue
        if kind not in ("parallel", *_SHAPE_SCENARIOS):
            raise ValueError(
                f"未知情景类型 {kind!r}（情景 {name!r}）；支持 parallel/steepen/"
                "flatten/butterfly/historical"
            )
        if "bp" not in scenario:
            raise ValueError(f"情景 {name!r} 缺少 bp")
        bp = float(scenario["bp"])
        if not np.isfinite(bp):
            raise ValueError(f"情景 {name!r} 的 bp 非有限: {scenario['bp']!r}")
        if kind == "parallel":
            shifts = np.full(len(key_tenors), bp, dtype=float)
            note = f"即期关键点全部 {bp:+g}bp（平行；配置 YTM 平行语义）"
        elif kind in _SHAPE_SCENARIOS:
            if bp < 0.0:
                raise ValueError(
                    f"情景 {name!r} 的 {kind} bp 是幅度，必须是非负有限数，"
                    f"实际 {scenario['bp']!r}（方向由 type 表达）"
                )
            shifts = _shape_shifts_bp(curve, kind, bp, key_tenors)
            note = (
                f"即期关键点按 Curve.shock 的 {kind} {bp:g}bp 期限形状平移"
                "（与配置 YTM 情景同语义，作用在即期关键点）"
            )
        else:  # pragma: no cover - 已在上方拦截未知类型
            raise ValueError(f"未知情景类型 {kind!r}（情景 {name!r}）")
        pnl_full, pnl_krd = _evaluate_shock(
            faces=cleaned,
            bond_by_code=bond_by_code,
            settlement=settlement,
            base_spot=base_spot,
            base_prices=base_prices,
            krd=krd,
            key_tenors=key_tenors,
            shifts_bp=shifts,
            cfg=resolved,
        )
        results.append(
            _make_result(name=name, pnl_full=pnl_full, pnl_krd=pnl_krd, nav=nav, note=note)
        )
    return results
