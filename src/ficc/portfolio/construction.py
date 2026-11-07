"""组合构建（Phase 2 第 4 步）：目标 KRD 向量 → 可执行成交。

流程
----
1. :func:`krd_matrix`：可交易债券对每个关键期限桶的 KRD 矩阵
   （**元/bp per 100 元面值，多头为正**，来自 ``bootstrap_spot`` + ``key_rate_durations``）；
2. :func:`solve_faces`：有界最小二乘 ``min ||A·(face/100) − target||²``，``face ≥ 0``
   （现券禁裸空），用 ``scipy.optimize.lsq_linear(method="bvls")``；解后按
   ``cfg.constraints.min_trade_lot`` **四舍五入到最近的整数倍**（避免碎单，同时
   不能让 25.0M 因浮点 24.999999 被 floor 成 24M）；
3. :func:`build_orders`：对比当前持仓面值，生成含 ``target_face``/``current_face``
   审计字段的买卖订单；``|Δface| < min_trade_lot`` 的碎单跳过，卖出超过持仓抛错。

单位与约定
----------
- 面值/金额：元；KRD：元/bp；多头为正（= 定价层 ``key_rate_durations`` 取负）。
- 矩阵列按 100 元面值计，因此组合 KRD = ``A @ (face/100)``。
- **不读未来信息**：只使用调用方给的决策日 ``curve``，禁止自行读取面板。
- 解不可达（如目标含负桶、面值上限生效、取整损失）时不静默：返回
  :class:`FaceSolution`（``dict`` 子类），在 ``.notes`` / ``.residual_norm`` 中披露，
  并记录 warning 日志。
"""

from __future__ import annotations

import math
import re
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import date
from typing import Any, Literal

import numpy as np
from scipy.optimize import lsq_linear

from ficc.config import Config, get_config
from ficc.curve.bootstrap import bootstrap_spot
from ficc.data.schema import BondStatic
from ficc.logging import get_logger
from ficc.portfolio.dates import remaining_years, settlement_for
from ficc.portfolio.types import TargetKRD
from ficc.pricing.analytics import key_rate_durations
from ficc.pricing.bond import dirty_price_from_ytm
from ficc.pricing.conventions import accrued_interest, day_count

__all__ = [
    "FaceSolution",
    "Order",
    "build_orders",
    "execution_price",
    "krd_matrix",
    "solve_faces",
    "spread_bp_for",
]

log = get_logger(__name__)

_EPS: float = 1e-9


# ---------------------------------------------------------------------------
# 工具
# ---------------------------------------------------------------------------
def _resolve_config(cfg: Config | None) -> Config:
    return cfg if cfg is not None else get_config()


def _resolve_key_tenors(key_tenors: Sequence[float] | None, cfg: Config) -> tuple[float, ...]:
    # 默认与基准/风险层契约一致：cfg.risk.key_tenors_krd（benchmark.py 也用这组桶）
    raw = list(key_tenors) if key_tenors is not None else list(cfg.risk.key_tenors_krd)
    tenors = tuple(sorted({float(t) for t in raw}))
    if not tenors:
        raise ValueError("key_tenors 不能为空")
    if any(t <= 0 or not math.isfinite(t) for t in tenors):
        raise ValueError(f"key_tenors 必须为正的有限数，实际 {raw!r}")
    return tenors


def _validate_bonds(bonds: Sequence[BondStatic]) -> tuple[BondStatic, ...]:
    bond_list = tuple(bonds)
    if not bond_list:
        raise ValueError("bonds 不能为空")
    codes = [bond.code for bond in bond_list]
    if len(set(codes)) != len(codes):
        raise ValueError(f"bonds 含重复 bond code: {codes}")
    return bond_list


def _spot_and_settlement(
    curve: Any, cfg: Config, calendar: object | None = None
) -> tuple[Any, date]:
    """即期曲线与**组合估值结算日**（R27：必须走 CN_IB 日历，不能再用默认工作日）。"""
    if not hasattr(curve, "as_of"):
        raise TypeError("curve 缺少必需成员 as_of：需要 ficc.curve.Curve")
    settlement = settlement_for(calendar, curve.as_of, cfg=cfg)
    spot = bootstrap_spot(curve, settlement=settlement, calendar=calendar)
    return spot, settlement


def spread_bp_for(tenor: float, cfg: Config) -> float:
    """按期限分类取**双边**收益率价差（bp）：活跃 2/5/10Y、超长 30Y、其余非活跃。

    约定（R26）：配置里的 ``spread_bp_*`` 是**双边价差**，单边成本 = 双边/2，
    以**收益率 bp** 的形式作用在执行收益率上（买入 ytm−half、卖出 ytm+half）。
    """
    costs = cfg.costs
    if float(tenor) >= 30.0:
        return float(costs.spread_bp_ultra_long)
    if 2.0 <= float(tenor) <= 10.0:
        return float(costs.spread_bp_active)
    return float(costs.spread_bp_illiquid)


def execution_price(
    bond: BondStatic,
    curve: Any,
    *,
    side: str,
    calendar: object | None = None,
    cfg: Config | None = None,
) -> tuple[float, float, float, float]:
    """执行价契约（R26/R27 唯一入口）。

    Returns
    -------
    (clean_exec, clean_mid, spread_bp, spread_cost_per_100_face)
        ``clean_*`` 元/百元面值；``spread_bp`` 为**双边**收益率价差；
        ``spread_cost_per_100_face`` 为单边价差成本（元/百元面值）。
    """
    config = _resolve_config(cfg)
    spread_bp = spread_bp_for(_tenor_label(bond), config)
    clean_exec, clean_mid, spread_cost = _clean_price(
        bond, curve, calendar=calendar, cfg=config, side=side, spread_bp=spread_bp
    )
    return clean_exec, clean_mid, spread_bp, spread_cost


def _clean_price(
    bond: BondStatic,
    curve: Any,
    *,
    calendar: object | None,
    cfg: Config,
    side: str = "buy",
    spread_bp: float = 0.0,
) -> tuple[float, float, float]:
    """执行净价 / 中间净价 / 单边价差成本（元）。

    价格在**结算日**（观察日 T+N，CN_IB 日历）计算，收益率取老化后的剩余期限；
    半价差以收益率 bp 调整执行收益率（买入价更低、卖出价更高）。
    """
    settlement = settlement_for(calendar, curve.as_of, cfg=cfg)
    remaining = remaining_years(bond, settlement)
    if remaining <= 0.0:
        raise ValueError(f"{bond.code}: 剩余期限非正（{remaining}），不能报价")
    ytm_mid = float(curve.rate(remaining))
    half_bp = float(spread_bp) / 2.0
    if half_bp < 0:
        raise ValueError(f"spread_bp 不能为负，实际 {spread_bp!r}")
    # 1bp = 0.01 个百分点；买入收益率上浮 → 价格下降（付半价差）
    ytm_exec = ytm_mid - half_bp * 0.01 if side == "buy" else ytm_mid + half_bp * 0.01
    ai = accrued_interest(bond, settlement, cfg=cfg)
    clean_mid = dirty_price_from_ytm(bond, settlement, ytm_mid, cfg=cfg) - ai
    clean_exec = dirty_price_from_ytm(bond, settlement, ytm_exec, cfg=cfg) - ai
    spread_cost = abs(clean_exec - clean_mid)  # 元/百元面值
    return clean_exec, clean_mid, spread_cost


# ---------------------------------------------------------------------------
# KRD 矩阵
# ---------------------------------------------------------------------------
def krd_matrix(
    bonds: Sequence[BondStatic],
    curve: Any,
    *,
    key_tenors: Sequence[float] | None = None,
    calendar: object | None = None,
    cfg: Config | None = None,
) -> np.ndarray:
    """KRD 矩阵，shape ``(n_buckets, n_bonds)``，单位 **元/bp per 100 元面值**。

    列顺序与 ``bonds`` 一致；行顺序为排序后的关键期限（默认
    ``cfg.risk.key_tenors_krd``）。数值 = ``-key_rate_durations(...)``（多头为正）。
    """
    config = _resolve_config(cfg)
    tenors = _resolve_key_tenors(key_tenors, config)
    bond_list = _validate_bonds(bonds)
    spot, settlement = _spot_and_settlement(curve, config, calendar)

    matrix = np.zeros((len(tenors), len(bond_list)), dtype=float)
    for column, bond in enumerate(bond_list):
        signed = key_rate_durations(bond, settlement, spot, key_tenors=tenors, cfg=config)
        for row, tenor in enumerate(tenors):
            matrix[row, column] = -float(signed[float(tenor)])
    return matrix


# ---------------------------------------------------------------------------
# 解面值
# ---------------------------------------------------------------------------
class FaceSolution(dict):  # type: ignore[type-arg]
    """``dict[code, face]`` 解，附残差与不可达说明。

    Attributes
    ----------
    notes : tuple[str, ...]
        人类可读说明（负目标、面值上限、取整损失等）；**不可达时不静默**。
    residual_norm : float
        取整后 ``||A·(face/100) − target||``（元/bp）。
    raw_residual_norm : float
        取整前的最小二乘残差（元/bp）。
    target_norm : float
        ``||target||``（元/bp），用于判断相对残差量级。
    relative_residual : float
        ``residual_norm / max(target_norm, eps)``。
    reached : bool
        取整后相对残差是否 < 1e-6（近似可达）。
    """

    def __init__(
        self,
        faces: Mapping[str, float],
        *,
        notes: tuple[str, ...],
        residual_norm: float,
        raw_residual_norm: float,
        target_norm: float,
        relative_residual: float,
        reached: bool,
    ) -> None:
        super().__init__(faces)
        self.notes = notes
        self.residual_norm = float(residual_norm)
        self.raw_residual_norm = float(raw_residual_norm)
        self.target_norm = float(target_norm)
        self.relative_residual = float(relative_residual)
        self.reached = bool(reached)


def solve_faces(
    target: TargetKRD,
    bonds: Sequence[BondStatic],
    curve: Any,
    *,
    max_face_per_bond: float | None = None,
    calendar: object | None = None,
    cfg: Config | None = None,
) -> FaceSolution:
    """把目标 KRD 向量解成各券面值（有界最小二乘、面值非负、按最小成交单位取整）。

    Parameters
    ----------
    target : TargetKRD
        目标 KRD（元/bp，按关键期限分桶）；**以目标桶为拟合行**（空目标才用默认桶）。
    bonds : Sequence[BondStatic]
        可交易债券（顺序即矩阵列顺序）。
    curve : Curve
        决策日 YTM 曲线（不读未来数据）。
    max_face_per_bond : float | None
        单券面值上限（元），前置集中度约束；``None`` 不设上限。
    cfg : Config | None

    Returns
    -------
    FaceSolution
        ``dict[code, face]``（face 为元、非负、四舍五入到 ``min_trade_lot`` 的
        整数倍、且已满足上限）；同时带 ``.notes`` / ``.residual_norm`` 等审计字段。

    Notes
    -----
    目标不可达时（负桶、上限、取整）返回**最小二乘解**并在 ``notes`` 说明、记
    warning 日志；绝不静默假装精确命中。
    """
    config = _resolve_config(cfg)
    bond_list = _validate_bonds(bonds)
    lot = float(config.constraints.min_trade_lot)
    if not math.isfinite(lot) or lot <= 0:
        raise ValueError(f"min_trade_lot 必须为正的有限数，实际 {lot!r}")

    upper_face = math.inf
    notes: list[str] = []
    if max_face_per_bond is not None:
        upper_face = float(max_face_per_bond)
        if not math.isfinite(upper_face) or upper_face < 0:
            raise ValueError(f"max_face_per_bond 必须为非负有限数，实际 {max_face_per_bond!r}")
        # R29：上限必须先映射到可交易的整手面值集合，否则会出现 2.6M 这种非整手解
        upper_face = math.floor(upper_face / lot) * lot
        if upper_face < lot:
            raise ValueError(
                f"max_face_per_bond={max_face_per_bond!r} 小于一个整手 {lot:g} 元，无可行解"
            )
        notes.append(f"单券面值上限映射到整手：{upper_face:g} 元（原始 {max_face_per_bond!r}）")
    upper_units = upper_face / 100.0

    # 拟合行 = 目标 KRD 的桶（策略输出的目标向量）；target 为空时才用默认桶。
    # 不再把默认桶并入：否则目标里没有的桶会以 0 参与拟合，扭曲"精确可命中"的目标。
    if target.krd:
        tenors = sorted({float(t) for t in target.krd})
    else:
        tenors = list(_resolve_key_tenors(None, config))
    if len(tenors) < 2:
        raise ValueError(
            f"target.krd 至少需要 2 个期限桶（key_rate_durations 契约），实际 {tenors}"
        )
    matrix = krd_matrix(bond_list, curve, key_tenors=tenors, calendar=calendar, cfg=config)
    target_vector = np.array(
        [float(target.krd.get(float(tenor), 0.0)) for tenor in tenors], dtype=float
    )
    if not np.all(np.isfinite(target_vector)):
        raise ValueError("target.krd 含 NaN/inf")

    n_bonds = len(bond_list)
    if np.all(np.abs(target_vector) <= _EPS):
        x_units = np.zeros(n_bonds, dtype=float)
        solve_residual = np.zeros_like(target_vector)
        notes.append("目标 KRD 全为 0：不建仓（面值全 0）")
    else:
        bounds = (np.zeros(n_bonds, dtype=float), np.full(n_bonds, upper_units))
        result = lsq_linear(matrix, target_vector, bounds=bounds, method="bvls")
        x_units = np.asarray(result.x, dtype=float)
        solve_residual = matrix @ x_units - target_vector
        if not np.all(np.isfinite(x_units)):
            raise RuntimeError("lsq_linear 返回非有限解")

    raw_faces = x_units * 100.0
    # 四舍五入到最近的 min_trade_lot 整数倍。
    # 不能用 floor：25.0M 在浮点下可能是 24.9999999，floor 会系统性少一个 lot
    # （评审证据：4 腿各 25M 被解成 24/24/24/25M，合计少 4M ≈ 4%）。
    faces = np.round(raw_faces / lot) * lot
    faces = np.maximum(faces, 0.0)
    if max_face_per_bond is not None:
        faces = np.minimum(faces, upper_face)

    target_norm = float(np.linalg.norm(target_vector))
    raw_residual_norm = float(np.linalg.norm(solve_residual))
    final_residual = matrix @ (faces / 100.0) - target_vector
    residual_norm = float(np.linalg.norm(final_residual))
    relative_residual = residual_norm / target_norm if target_norm > _EPS else 0.0
    reached = relative_residual < 1e-6

    if np.any(target_vector < -_EPS):
        notes.append(
            "目标含负 DV01 桶，现券禁裸空：负目标由最小二乘吸收，"
            f"取整后残差 {residual_norm:.6g} 元/bp（相对 {relative_residual:.2%}）"
        )
    if max_face_per_bond is not None and np.any(np.isclose(raw_faces, upper_face, rtol=1e-9)):
        notes.append(f"单券面值上限 {upper_face:g} 元生效")
    if not reached and not any("负 DV01" in note for note in notes):
        notes.append(
            f"目标不可完全可达：取整后残差 {residual_norm:.6g} 元/bp"
            f"（相对 {relative_residual:.2%}）"
        )
    if relative_residual > 1e-3:
        log.warning(
            "solve_faces 残差偏大：target_norm=%.6g, residual=%.6g（相对 %.2f%%）",
            target_norm,
            residual_norm,
            relative_residual * 100.0,
        )

    faces_map = {bond.code: float(face) for bond, face in zip(bond_list, faces, strict=True)}
    return FaceSolution(
        faces_map,
        notes=tuple(notes),
        residual_norm=residual_norm,
        raw_residual_norm=raw_residual_norm,
        target_norm=target_norm,
        relative_residual=relative_residual,
        reached=reached,
    )


# ---------------------------------------------------------------------------
# 生成订单
# ---------------------------------------------------------------------------
@dataclass(frozen=True)
class Order:
    """一笔调仓订单（面值元；价格元/百元面值）。

    ``clean_price`` 是**执行净价**（已含半价差）；``mid_clean_price`` 是曲线中间价；
    ``spread_cost`` 是该笔交易的**单边价差成本（元）**，用于成本分解。
    """

    bond: BondStatic
    side: Literal["buy", "sell"]
    face_value: float
    clean_price: float
    target_face: float
    current_face: float
    mid_clean_price: float = 0.0
    spread_bp: float = 0.0
    spread_cost: float = 0.0


_CODE_TENOR = re.compile(r"SYN-(?P<tenor>\d+(?:\.\d+)?)Y-")


def _tenor_label(bond: BondStatic) -> float:
    """券的期限标签（用于价差分类；优先从 code 解析）。"""
    match = _CODE_TENOR.search(bond.code)
    if match:
        return float(match.group("tenor"))
    return day_count(bond.issue_date, bond.maturity_date) / 365.25


def _normalise_positions(
    snapshot_positions: Any,
) -> tuple[dict[str, BondStatic], dict[str, float]]:
    """把 LedgerSnapshot 或 PositionView 序列转成 ``{code: bond}`` 与 ``{code: face}``。"""
    if hasattr(snapshot_positions, "positions"):
        position_list = list(snapshot_positions.positions)
    else:
        position_list = list(snapshot_positions or [])
    bonds: dict[str, BondStatic] = {}
    faces: dict[str, float] = {}
    for position in position_list:
        bond = position.bond
        face = float(position.face_value)
        if face < 0.0:
            raise ValueError(f"{bond.code}: 当前持仓面值不能为负，实际 {face}")
        bonds[bond.code] = bond
        faces[bond.code] = faces.get(bond.code, 0.0) + face
    return bonds, faces


def build_orders(
    target: TargetKRD,
    snapshot_positions: Any,
    bonds: Sequence[BondStatic],
    curve: Any,
    *,
    min_trade_face: float | None = None,
    calendar: object | None = None,
    cfg: Config | None = None,
) -> tuple[Order, ...]:
    """对比目标面值与当前持仓，生成订单（跳过小于成交阈值的碎单）。

    ``min_trade_face`` 默认 ``min_trade_lot``；引擎传 ``rebalance_band_lots × lot``
    作为调仓死区（抑制"债券老化/整手取整"造成的日常抖单）。


    Parameters
    ----------
    target : TargetKRD
    snapshot_positions : LedgerSnapshot | Iterable[PositionView]
        当前持仓（用于 ``current_face``）。
    bonds : Sequence[BondStatic]
        可交易宇宙（``solve_faces`` 的自变量）；持有但不在宇宙内的券会生成**卖出**
        订单（目标面值 0，显式清仓，不静默保留）。
    curve : Curve
        决策日曲线（报净价 + KRD）。
    cfg : Config | None

    Returns
    -------
    tuple[Order, ...]
        确定性顺序：先按 ``bonds`` 顺序，再按持有但非宇宙券的 code 排序。
        跳过 ``|Δface| < min_trade_lot`` 的碎单；``Δface`` 恰等于最小成交单位时保留。

    Raises
    ------
    ValueError
        卖出面值超过当前持仓，或目标面值为负。
    """
    config = _resolve_config(cfg)
    lot = float(config.constraints.min_trade_lot)
    threshold = float(min_trade_face) if min_trade_face is not None else lot
    if not math.isfinite(threshold) or threshold <= 0:
        raise ValueError(f"min_trade_face 必须为正的有限数，实际 {min_trade_face!r}")
    threshold = max(threshold, lot)
    bond_list = _validate_bonds(bonds)
    solution = solve_faces(target, bond_list, curve, calendar=calendar, cfg=config)
    current_bonds, current_faces = _normalise_positions(snapshot_positions)

    universe_codes = [bond.code for bond in bond_list]
    extra_codes = sorted(set(current_faces) - set(universe_codes))

    orders: list[Order] = []
    for bond in bond_list:
        target_face = float(solution.get(bond.code, 0.0))
        current_face = float(current_faces.get(bond.code, 0.0))
        delta = target_face - current_face
        if abs(delta) < threshold - _EPS:
            continue
        if delta < 0.0:
            if -delta > current_face + _EPS:
                raise ValueError(
                    f"{bond.code}: 卖出面值 {-delta} 超过当前持仓 {current_face}"
                    f"（目标面值 {target_face}；现券禁裸空）"
                )
            side: Literal["buy", "sell"] = "sell"
        else:
            side = "buy"
        clean_exec, clean_mid, spread_bp, spread_per_100 = execution_price(
            bond, curve, side=side, calendar=calendar, cfg=config
        )
        orders.append(
            Order(
                bond=bond,
                side=side,
                face_value=abs(delta),
                clean_price=clean_exec,
                target_face=target_face,
                current_face=current_face,
                mid_clean_price=clean_mid,
                spread_bp=spread_bp,
                spread_cost=abs(delta) / 100.0 * spread_per_100,
            )
        )

    for code in extra_codes:
        bond = current_bonds[code]
        current_face = float(current_faces[code])
        delta = -current_face
        if abs(delta) < threshold - _EPS:
            continue
        clean_exec, clean_mid, spread_bp, spread_per_100 = execution_price(
            bond, curve, side="sell", calendar=calendar, cfg=config
        )
        orders.append(
            Order(
                bond=bond,
                side="sell",
                face_value=abs(delta),
                clean_price=clean_exec,
                target_face=0.0,
                current_face=current_face,
                mid_clean_price=clean_mid,
                spread_bp=spread_bp,
                spread_cost=abs(delta) / 100.0 * spread_per_100,
            )
        )
    return tuple(orders)
