"""固定组合 VaR / ES：全重估 vs KRD 近似（计划 §12.12-B）。

本模块只消费「持仓 + 曲线」，**不消费归因分项**，也不依赖引擎/账本状态：
给定固定面值 ``faces``、债券条款 ``bonds``、曲线面板 ``panel`` 与交易日历
``calendar``，输出逐日 PnL 与三种 VaR/ES 口径（历史模拟、参数正态、
Cornish-Fisher）。全重估与 KRD 近似在同一窗口、同一面板上并列计算，差异
单独报告，不用任何一方回填另一方。

口径（必须明示）
----------------
1. **固定组合（买入持有）**：面值固定、窗口内不交易、不滚动。逐日 PnL 在
   **T+N 结算日**估值（与账本/基准 mark 同口径），全价用
   :func:`ficc.pricing.bond.dirty_price_from_ytm` 重估：

   ``PnL_t = Σ face/100 × (dirty(settle_t) − dirty(settle_{t−1})) + coupon_cash_t``

   其中 ``coupon_cash_t`` 是 ``(settle_{t−1}, settle_t]`` 区间内到期的票息
   （规则附息券按 ``face/100 × coupon_pct / freq`` 计；利随本清/贴现券在到期前
   无期间现金流）。任务书给出的 ``PnL = Σ face/100 × Δdirty`` 是
   ``include_coupon_cash=False`` 的 price-only 子口径；默认包含票息现金，否则
   付息日会出现一个票息金额的机械负跳，使 VaR/ES 与不含现金项的 KRD 近似
   不可比（该跳不是市场风险）。

2. **KRD 近似**：``PnL_t ≈ −Σ_b KRD_b(t−1) × Δy_b(t)``，其中
   ``KRD_b`` 为组合在 ``t−1`` 日的关键期限 KRD（元/bp，多头为正，来自
   :func:`ficc.portfolio.constraints.project_risk` 的即期曲线 KRD），
   ``Δy_b`` 为关键期限 YTM 日变化（bp）。该近似**不含 carry/roll、凸性和
   非平行形状的高阶效应**，因此只作为近似对照，不能替代全重估。

3. **VaR/ES 单位**：``historical_var_es`` / ``parametric_var_es`` 的输入
   ``pnl`` 是「占组合价值的百分比」序列（百分数单位，``0.05`` = 0.05%），
   输出 ``VarResult.var_pct / es_pct`` 是**正的损失百分比**（同样百分数单位）。
   Python 调用方若只有元口径 PnL，应先除以 NAV 再乘 100（脚本即如此）。
   分位数/ES 允许为负（该置信度下历史分位仍为盈利时，如实报告，不截断）。

4. **horizon_days**：>1 时按重叠 h 日累计 PnL 计算（历史法与参数法都用同一
   聚合样本），``n_obs = 原始样本数 − h + 1``；重叠窗口**不独立**，仅用于报告。
   ES 为尾部条件均值（``E[Loss | Loss ≥ VaR]``）。

5. **因果性/可复现**：只读窗口内曲线行；不使用随机数；历史情景用真实历史
   数据重演（见 :mod:`ficc.risk.stress`）。日频 PnL 的周末/假期跨度为多个
   自然日（结算日推进），这是交易日频口径，不做缩放。
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime
from typing import Any, Literal

import numpy as np
import pandas as pd
from scipy import stats

from ficc.config import Config, get_config
from ficc.curve.bootstrap import bootstrap_spot
from ficc.curve.curve import Curve
from ficc.data.schema import BondStatic
from ficc.portfolio.constraints import project_risk
from ficc.portfolio.dates import remaining_years, settlement_for
from ficc.pricing.bond import dirty_price_from_ytm
from ficc.pricing.conventions import coupon_dates

__all__ = [
    "VarResult",
    "historical_var_es",
    "parametric_var_es",
    "pnl_pct_from_cny",
    "portfolio_market_value",
    "portfolio_pnl_fixed_revaluation",
    "portfolio_pnl_krd_approx",
]

# 面板曲线插值方法：与回测引擎 ``backtest.engine._curve_at`` 的 CN_IB 面板口径
# 一致（11 节点 + hermite）。账本/基准的每日 mark 用同一口径。
_CURVE_METHOD = "hermite"

_DEFAULT_CONFIDENCES: tuple[float, ...] = (0.95, 0.99)
DistKind = Literal["normal", "cornish_fisher"]


# ---------------------------------------------------------------------------
# 结果对象
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class VarResult:
    """单一置信度下的 VaR/ES 结果（损失为正，单位：组合价值的百分比）。

    Attributes
    ----------
    method : str
        ``"historical"`` / ``"parametric_normal"`` / ``"parametric_cornish_fisher"``。
    confidence : float
        置信度（0.95 / 0.99）。
    horizon_days : int
        持有期（交易日）。
    var_pct : float
        VaR（正的损失百分比；负值表示该分位仍为盈利，如实报告）。
    es_pct : float
        ES（尾部条件均值，正的损失百分比）。
    n_obs : int
        用于分位数/矩估计的（聚合后）观测数。
    """

    method: str
    confidence: float
    horizon_days: int
    var_pct: float
    es_pct: float
    n_obs: int


# ---------------------------------------------------------------------------
# 输入规范化与组合估值（stress.py 复用这些内部工具）
# ---------------------------------------------------------------------------
def _as_date(value: Any, *, field: str = "date") -> date:
    if isinstance(value, datetime):
        return value.date()
    if isinstance(value, date):
        return value
    if isinstance(value, np.datetime64):
        return pd.Timestamp(value).date()
    if isinstance(value, str):
        try:
            return date.fromisoformat(value)
        except ValueError as exc:  # pragma: no cover - 取决于外部输入
            raise ValueError(f"{field} 无法解析为 ISO 日期: {value!r}") from exc
    raise TypeError(f"{field} 必须是 datetime.date，实际 {type(value).__name__}")


def _resolve_config(cfg: Config | None) -> Config:
    return cfg if cfg is not None else get_config()


def _normalise_panel(panel: pd.DataFrame) -> pd.DataFrame:
    """panel：index=日期（date/Timestamp），columns=期限（年），values=YTM 百分数。"""
    if not isinstance(panel, pd.DataFrame):
        raise TypeError(f"panel 必须是 pd.DataFrame，实际 {type(panel).__name__}")
    if panel.empty:
        raise ValueError("panel 不能为空")
    frame = panel.copy()
    frame.index = pd.Index(
        [_as_date(value, field="panel.index") for value in frame.index], name="date"
    )
    if frame.index.has_duplicates:
        raise ValueError("panel 索引日期重复")
    try:
        tenors = [float(column) for column in frame.columns]
    except (TypeError, ValueError) as exc:
        raise ValueError(f"panel 列名必须是数值期限（年）: {list(frame.columns)}") from exc
    if any(not np.isfinite(t) or t <= 0.0 for t in tenors):
        raise ValueError(f"panel 期限必须为正有限数，实际 {tenors}")
    frame.columns = pd.Index(tenors, name="tenor")
    frame = frame.loc[:, ~frame.columns.duplicated(keep="first")]
    frame = frame.sort_index()
    frame = frame.reindex(sorted(frame.columns), axis=1)
    return frame


def _panel_dates(panel_frame: pd.DataFrame) -> list[date]:
    return list(panel_frame.index)


def _panel_curve(panel_frame: pd.DataFrame, day: date) -> Curve:
    if day not in panel_frame.index:
        raise ValueError(f"panel 缺少 {day} 的曲线行")
    rates = np.asarray(panel_frame.loc[day].to_numpy(dtype=float), dtype=float)
    if not np.isfinite(rates).all():
        raise ValueError(f"panel[{day}] 含 NaN/inf：{rates!r}")
    tenors = panel_frame.columns.to_numpy(dtype=float)
    return Curve(as_of=day, tenors=tenors.copy(), rates=rates, method=_CURVE_METHOD)


def _bond_map(bonds: list[BondStatic] | tuple[BondStatic, ...]) -> dict[str, BondStatic]:
    if not bonds:
        raise ValueError("bonds 不能为空")
    mapping: dict[str, BondStatic] = {}
    for bond in bonds:
        if not isinstance(bond, BondStatic):
            raise TypeError(f"bonds 元素必须是 BondStatic，实际 {type(bond).__name__}")
        if bond.code in mapping:
            raise ValueError(f"bonds 中 {bond.code} 重复")
        mapping[bond.code] = bond
    return mapping


def _validate_faces(faces: Any, bond_by_code: dict[str, BondStatic]) -> dict[str, float]:
    if not hasattr(faces, "items"):
        raise TypeError(f"faces 必须是 Mapping[code, face]，实际 {type(faces).__name__}")
    cleaned: dict[str, float] = {}
    for code, face in dict(faces).items():
        code_str = str(code)
        value = float(face)
        if not np.isfinite(value) or value < 0.0:
            raise ValueError(f"{code_str}: 面值必须是非负有限数，实际 {face!r}")
        if code_str not in bond_by_code:
            raise ValueError(f"faces 中的 {code_str} 不在 bonds 里")
        cleaned[code_str] = value
    if not any(value > 0.0 for value in cleaned.values()):
        raise ValueError("固定组合面值全为 0")
    return cleaned


def _mark_portfolio(
    faces: dict[str, float],
    bond_by_code: dict[str, BondStatic],
    curve: Curve,
    *,
    calendar: Any,
    cfg: Config,
) -> tuple[dict[str, float], float, date]:
    """按 ``curve`` 对固定面值做全价 mark。

    Returns
    -------
    (dirty_by_code, market_value, settlement)
        ``dirty`` 元/百元面值、``market_value`` 元、``settlement`` 为估值结算日。
    """
    settlement = settlement_for(calendar, curve.as_of, cfg=cfg)
    dirty_by_code: dict[str, float] = {}
    market_value = 0.0
    for code, face in faces.items():
        if face <= 0.0:
            continue
        bond = bond_by_code[code]
        remaining = remaining_years(bond, settlement)
        if remaining <= 0.0:
            raise ValueError(
                f"{code}: 估值日 {settlement} 剩余期限 {remaining:.6f} 年 ≤ 0，"
                "固定组合券已到期；请改用账本/滚动路径"
            )
        ytm = float(curve.rate(remaining))
        dirty = float(dirty_price_from_ytm(bond, settlement, ytm, cfg=cfg))
        dirty_by_code[code] = dirty
        market_value += face / 100.0 * dirty
    if market_value <= 0.0:
        raise ValueError(f"组合市值为非正：{market_value!r}")
    return dirty_by_code, float(market_value), settlement


def portfolio_market_value(
    faces: Any,
    bonds: list[BondStatic] | tuple[BondStatic, ...],
    curve: Curve,
    *,
    calendar: Any,
    cfg: Config | None = None,
) -> float:
    """固定面值组合在 ``curve``（结算日 T+N）上的全价市值（元）。"""
    resolved = _resolve_config(cfg)
    bond_by_code = _bond_map(bonds)
    cleaned = _validate_faces(faces, bond_by_code)
    _, market_value, _ = _mark_portfolio(
        cleaned, bond_by_code, curve, calendar=calendar, cfg=resolved
    )
    return market_value


def _coupon_schedule(bond: BondStatic) -> tuple[date, ...]:
    """期间票息日（不含起息日）；利随本清/贴现券为空。"""
    if bond.accrual != "coupon":
        return ()
    return tuple(coupon_dates(bond)[1:])


def _coupon_cash_between(
    face: float, bond: BondStatic, start: date, end: date, schedule: tuple[date, ...]
) -> float:
    """``(start, end]`` 区间内到期的票息现金（元）。

    按**付息日**（而非顺延后的到账日）计入：付息日与估值日的关系决定全价是否
    已经除息，两者用同一日期口径才能让「价格下降 + 票息现金」在付息日相互抵消；
    顺延只影响现金到账时点，日频误差 ≤1 个交易日，不影响区间总收益。
    """
    if not schedule or face <= 0.0 or end <= start:
        return 0.0
    freq = int(bond.freq)
    per_coupon = face / 100.0 * float(bond.coupon_pct) / float(freq)
    count = sum(1 for coupon_day in schedule if start < coupon_day <= end)
    return float(count) * per_coupon


def _window_positions(panel_dates: list[date], start: date, end: date) -> list[int]:
    """窗口内日期在 panel 中的下标；首日必须有前一行（用于首日 PnL）。"""
    start_day = _as_date(start, field="start")
    end_day = _as_date(end, field="end")
    if end_day < start_day:
        raise ValueError(f"end({end_day}) 早于 start({start_day})")
    positions = [index for index, day in enumerate(panel_dates) if start_day <= day <= end_day]
    if not positions:
        raise ValueError(f"panel 在 [{start_day}, {end_day}] 内没有交易日")
    if positions[0] == 0:
        raise ValueError(
            f"窗口首日 {panel_dates[0]} 之前没有曲线，无法计算首日 PnL；请把 start 后移"
        )
    return positions


# ---------------------------------------------------------------------------
# 固定组合逐日 PnL：全重估
# ---------------------------------------------------------------------------
def portfolio_pnl_fixed_revaluation(
    faces: Any,
    bonds: list[BondStatic] | tuple[BondStatic, ...],
    panel: pd.DataFrame,
    calendar: Any,
    *,
    start: date,
    end: date,
    cfg: Config | None = None,
    include_coupon_cash: bool = True,
) -> pd.Series:
    """固定面值组合的逐日 PnL（**全重估**，元）。

    ``PnL_t = Σ face/100 × (dirty_t − dirty_{t−1}) + coupon_cash_t``：
    ``dirty`` 在 ``t−1``/``t`` 各自的 T+N 结算日、用各自曲线按老化后剩余期限
    重估；``coupon_cash_t`` 为 ``(settle_{t−1}, settle_t]`` 内到期票息。

    Parameters
    ----------
    faces : Mapping[str, float]
        固定面值（元）。
    bonds : Sequence[BondStatic]
        债券条款（须覆盖 faces 的全部 code）。
    panel : DataFrame
        YTM 面板：index=日期、columns=期限、values=百分数。
    calendar : Any
        ``TradingCalendar`` 或同接口对象（提供 ``settlement_date``）。
    start, end : date
        窗口（含端点）；``start`` 必须之前还有一行 panel 以计算首日 PnL。
    cfg : Config | None
    include_coupon_cash : bool, default True
        是否计入区间票息现金。``False`` 时退化为任务书原式
        ``Σ face/100 × Δdirty``（price-only；付息日有机械负跳）。

    Returns
    -------
    pandas.Series
        index=窗口交易日（name="date"），name="pnl_full_revaluation"，单位元。
    """
    resolved = _resolve_config(cfg)
    bond_by_code = _bond_map(bonds)
    cleaned = _validate_faces(faces, bond_by_code)
    panel_frame = _normalise_panel(panel)
    panel_dates = _panel_dates(panel_frame)
    positions = _window_positions(panel_dates, start, end)
    schedules = {
        code: _coupon_schedule(bond_by_code[code]) for code in cleaned if cleaned[code] > 0.0
    }

    pnl_values: list[float] = []
    index: list[date] = []
    for position in positions:
        day = panel_dates[position]
        previous_day = panel_dates[position - 1]
        current_curve = _panel_curve(panel_frame, day)
        previous_curve = _panel_curve(panel_frame, previous_day)
        settle_current = settlement_for(calendar, day, cfg=resolved)
        settle_previous = settlement_for(calendar, previous_day, cfg=resolved)
        total = 0.0
        for code, face in cleaned.items():
            if face <= 0.0:
                continue
            bond = bond_by_code[code]
            remaining_current = remaining_years(bond, settle_current)
            remaining_previous = remaining_years(bond, settle_previous)
            if remaining_current <= 0.0 or remaining_previous <= 0.0:
                raise ValueError(
                    f"{code}: 结算日剩余期限 ≤0（{previous_day}→{day}），固定组合券已到期"
                )
            dirty_current = dirty_price_from_ytm(
                bond, settle_current, float(current_curve.rate(remaining_current)), cfg=resolved
            )
            dirty_previous = dirty_price_from_ytm(
                bond, settle_previous, float(previous_curve.rate(remaining_previous)), cfg=resolved
            )
            total += face / 100.0 * (dirty_current - dirty_previous)
            if include_coupon_cash:
                total += _coupon_cash_between(
                    face, bond, settle_previous, settle_current, schedules[code]
                )
        pnl_values.append(float(total))
        index.append(day)
    return pd.Series(
        pnl_values,
        index=pd.Index(index, name="date"),
        name="pnl_full_revaluation",
        dtype=float,
    )


# ---------------------------------------------------------------------------
# 固定组合逐日 PnL：KRD 近似
# ---------------------------------------------------------------------------
def portfolio_pnl_krd_approx(
    faces: Any,
    bonds: list[BondStatic] | tuple[BondStatic, ...],
    panel: pd.DataFrame,
    calendar: Any,
    *,
    start: date,
    end: date,
    cfg: Config | None = None,
) -> pd.Series:
    """固定面值组合的逐日 PnL（**KRD 线性近似**，元）。

    ``PnL_t ≈ −Σ_b KRD_b(t−1) × (z_b(t) − z_b(t−1))``：
    - ``KRD_b``：组合在 ``t−1`` 日关键期限 KRD（元/bp，多头为正），来自
      :func:`ficc.portfolio.constraints.project_risk`（定义在**即期曲线**单点
      冲击上）；
    - ``Δz_b``：关键期限**零息（即期）利率**变化（bp）。KRD 是对即期曲线定义的
      敏感度，因此 Δ 也必须取即期曲线：对 ``t−1``/``t`` 两天的 YTM 曲线分别
      bootstrap 后取关键期限零息利率之差。直接用 YTM 差会把「YTM→即期」的映射
      误差当成近似误差（纯平行冲击下即可差 2–3%）。

    该近似不含 carry/roll、凸性与非平行形状的高阶项，必须与全重估并列报告。
    ``PnL_t`` 的符号约定：收益率上行 → 多头 KRD 为正 → PnL 为负。

    Returns
    -------
    pandas.Series
        index=窗口交易日（name="date"），name="pnl_krd_approx"，单位元。
    """
    resolved = _resolve_config(cfg)
    bond_by_code = _bond_map(bonds)
    cleaned = _validate_faces(faces, bond_by_code)
    panel_frame = _normalise_panel(panel)
    panel_dates = _panel_dates(panel_frame)
    positions = _window_positions(panel_dates, start, end)
    key_tenors = sorted(float(t) for t in resolved.risk.key_tenors_krd)
    if not key_tenors:
        raise ValueError("cfg.risk.key_tenors_krd 为空")

    pnl_values: list[float] = []
    index: list[date] = []
    for position in positions:
        day = panel_dates[position]
        previous_day = panel_dates[position - 1]
        current_curve = _panel_curve(panel_frame, day)
        previous_curve = _panel_curve(panel_frame, previous_day)
        settle_current = settlement_for(calendar, day, cfg=resolved)
        settle_previous = settlement_for(calendar, previous_day, cfg=resolved)
        risk_previous = project_risk(
            cleaned,
            list(bond_by_code.values()),
            previous_curve,
            calendar=calendar,
            cfg=resolved,
        )
        spot_current = bootstrap_spot(current_curve, settlement=settle_current, calendar=calendar)
        spot_previous = bootstrap_spot(
            previous_curve, settlement=settle_previous, calendar=calendar
        )
        delta_bp = np.asarray(
            [
                (float(spot_current.zero_rate(tenor)) - float(spot_previous.zero_rate(tenor)))
                * 100.0
                for tenor in key_tenors
            ],
            dtype=float,
        )
        krd = np.asarray([float(risk_previous.krd[tenor]) for tenor in key_tenors], dtype=float)
        pnl_values.append(float(-np.dot(krd, delta_bp)))
        index.append(day)
    return pd.Series(
        pnl_values,
        index=pd.Index(index, name="date"),
        name="pnl_krd_approx",
        dtype=float,
    )


# ---------------------------------------------------------------------------
# VaR / ES
# ---------------------------------------------------------------------------
def pnl_pct_from_cny(pnl_cny: pd.Series | np.ndarray, portfolio_value: float) -> pd.Series:
    """把元口径 PnL 转成「占组合价值的百分比」（百分数单位；脚本/报告用）。"""
    value = float(portfolio_value)
    if not np.isfinite(value) or value <= 0.0:
        raise ValueError(f"portfolio_value 必须为正有限数，实际 {portfolio_value!r}")
    series = pd.Series(pnl_cny, dtype=float)
    return series / value * 100.0


def _coerce_pnl(pnl: Any) -> np.ndarray:
    values = np.asarray(pd.to_numeric(pd.Series(pnl), errors="raise"), dtype=float)
    values = values[np.isfinite(values)]
    if values.size < 2:
        raise ValueError(f"PnL 样本至少需要 2 个有限观测，实际 {values.size}")
    return values


def _aggregate_over_horizon(values: np.ndarray, horizon_days: int) -> np.ndarray:
    horizon = int(horizon_days)
    if horizon < 1:
        raise ValueError(f"horizon_days 必须 >= 1，实际 {horizon_days!r}")
    if horizon == 1:
        return values
    if values.size < horizon + 1:
        raise ValueError(
            f"horizon_days={horizon} 需要至少 {horizon + 1} 个观测，实际 {values.size}"
        )
    windows = np.lib.stride_tricks.sliding_window_view(values, horizon)
    return np.asarray(windows.sum(axis=1), dtype=float)


def _validate_confidences(confidences: tuple[float, ...] | list[float]) -> tuple[float, ...]:
    cleaned: list[float] = []
    for confidence in confidences:
        value = float(confidence)
        if not (0.0 < value < 1.0):
            raise ValueError(f"置信度必须在 (0, 1) 内，实际 {confidence!r}")
        cleaned.append(value)
    if not cleaned:
        raise ValueError("confidences 不能为空")
    return tuple(cleaned)


def _historical_tail(aggregated: np.ndarray, confidence: float) -> tuple[float, float, int]:
    losses = -aggregated
    var = float(np.quantile(losses, confidence))
    tail = losses[losses >= var]
    if tail.size == 0:  # 极小样本时线性分位可能没有严格超过的尾部点
        tail = np.asarray([float(losses.max())], dtype=float)
    es = float(tail.mean())
    return var, es, int(losses.size)


def historical_var_es(
    pnl: pd.Series | np.ndarray,
    *,
    confidences: tuple[float, ...] | list[float] = _DEFAULT_CONFIDENCES,
    horizon_days: int = 1,
) -> list[VarResult]:
    """历史模拟 VaR/ES（经验分位数 + 尾部条件均值）。

    Parameters
    ----------
    pnl : Series | array-like
        逐期 PnL，单位=占组合价值的百分比（百分数单位，损失为负）。
    confidences : Sequence[float]
        置信度列表（默认 0.95/0.99）。
    horizon_days : int
        >1 时用重叠 h 日累计 PnL 计算（同一聚合样本）。

    Returns
    -------
    list[VarResult]
        每个置信度一个结果；``method="historical"``。
    """
    values = _coerce_pnl(pnl)
    aggregated = _aggregate_over_horizon(values, horizon_days)
    results: list[VarResult] = []
    for confidence in _validate_confidences(confidences):
        var, es, n_obs = _historical_tail(aggregated, confidence)
        results.append(
            VarResult(
                method="historical",
                confidence=confidence,
                horizon_days=int(horizon_days),
                var_pct=var,
                es_pct=es,
                n_obs=n_obs,
            )
        )
    return results


def _cornish_fisher_quantile(z: np.ndarray, skew: float, excess_kurtosis: float) -> np.ndarray:
    """Cornish-Fisher 展开：把标准正态分位 ``z`` 修正到给定偏度/超额峰度。"""
    z2 = z * z
    z3 = z2 * z
    return (
        z
        + (z2 - 1.0) * skew / 6.0
        + (z3 - 3.0 * z) * excess_kurtosis / 24.0
        - (2.0 * z3 - 5.0 * z) * skew * skew / 36.0
    )


def _normal_es(mean: float, std: float, confidence: float) -> float:
    tail_prob = 1.0 - confidence
    z = float(stats.norm.ppf(tail_prob))
    return float(-mean + std * float(stats.norm.pdf(z)) / tail_prob)


def _cornish_fisher_es(
    mean: float,
    std: float,
    skew: float,
    excess_kurtosis: float,
    confidence: float,
    *,
    grid: int = 4000,
) -> float:
    """CF 的 ES：对修正分位函数在尾部 0..p 上数值积分（等距中点规则）。

    ``ES = −(1/p)∫_0^p Q(u) du``，``Q(u)=μ+σ·z_cf(Φ^{-1}(u))``；当偏度/峰度
    都≈0 时退化为正态解析式（避免积分误差）。CF 只保证一阶修正，样本极端时
    可能非单调；调用方应同时报告 ``n_obs`` 与经验 VaR 对照。
    """
    tail_prob = 1.0 - confidence
    if abs(skew) < 1e-12 and abs(excess_kurtosis) < 1e-12:
        return _normal_es(mean, std, confidence)
    if std == 0.0:
        return float(-mean)
    u = (np.arange(grid, dtype=float) + 0.5) * tail_prob / grid
    z = stats.norm.ppf(u)
    quantiles = mean + std * _cornish_fisher_quantile(z, skew, excess_kurtosis)
    return float(-np.mean(quantiles))


def parametric_var_es(
    pnl: pd.Series | np.ndarray,
    *,
    confidences: tuple[float, ...] | list[float] = _DEFAULT_CONFIDENCES,
    horizon_days: int = 1,
    dist: DistKind = "normal",
) -> list[VarResult]:
    """参数法 VaR/ES（正态或 Cornish-Fisher 偏度/峰度修正）。

    参数由（按 ``horizon_days`` 聚合后的）样本矩估计：均值/总体标准差；
    Cornish-Fisher 另用无偏偏度与超额峰度（``scipy.stats`` bias=False）。
    正态 ES 有解析式；CF ES 对修正分位函数在尾部做数值积分。

    Raises
    ------
    ValueError
        ``dist`` 非法；CF 在样本 < 4 个时无法估计偏度/峰度。
    """
    if dist not in ("normal", "cornish_fisher"):
        raise ValueError(f"dist 必须是 'normal'/'cornish_fisher'，实际 {dist!r}")
    values = _coerce_pnl(pnl)
    aggregated = _aggregate_over_horizon(values, horizon_days)
    if aggregated.size < 2:
        raise ValueError("参数法至少需要 2 个观测")
    mean = float(np.mean(aggregated))
    std = float(np.std(aggregated, ddof=0))
    if dist == "cornish_fisher":
        if aggregated.size < 4:
            raise ValueError(
                f"Cornish-Fisher 需要至少 4 个观测估计偏度/峰度，实际 {aggregated.size}"
            )
        skew = float(stats.skew(aggregated, bias=False))
        excess_kurtosis = float(stats.kurtosis(aggregated, fisher=True, bias=False))
    else:
        skew = 0.0
        excess_kurtosis = 0.0
    method = "parametric_normal" if dist == "normal" else "parametric_cornish_fisher"
    results: list[VarResult] = []
    for confidence in _validate_confidences(confidences):
        z = float(stats.norm.ppf(1.0 - confidence))
        if dist == "normal":
            var = float(-(mean + std * z))
            es = _normal_es(mean, std, confidence)
        else:
            z_cf = float(_cornish_fisher_quantile(np.asarray([z]), skew, excess_kurtosis)[0])
            var = float(-(mean + std * z_cf))
            es = _cornish_fisher_es(mean, std, skew, excess_kurtosis, confidence)
        results.append(
            VarResult(
                method=method,
                confidence=confidence,
                horizon_days=int(horizon_days),
                var_pct=var,
                es_pct=es,
                n_obs=int(aggregated.size),
            )
        )
    return results
