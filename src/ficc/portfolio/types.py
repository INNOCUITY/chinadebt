"""组合层共享类型（Phase 2 第 3–4 步）。

这些 dataclass 是策略层、组合构建层、约束层与回测引擎之间的**唯一契约**。
任何跨层通信都只用这里的类型，避免各写一套 dict。

单位约定（与项目其它模块一致）
------------------------------
- 金额：元。
- 面值：元（100 元面值 = 100）。
- 价格：元/百元面值。
- 收益率：百分数（2.35 表示 2.35%）。
- DV01 / KRD：**元/bp**（全价口径，多头为正）。
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import date
from typing import TYPE_CHECKING

import numpy as np

if TYPE_CHECKING:  # 避免运行时循环导入
    import pandas as pd

    from ficc.config import Config
    from ficc.curve.curve import Curve
    from ficc.data.schema import BondStatic
    from ficc.portfolio.ledger import PositionView

__all__ = [
    "BenchmarkLeg",
    "BenchmarkPortfolio",
    "MarketContext",
    "TargetKRD",
]


@dataclass(frozen=True)
class BenchmarkLeg:
    """基准组合的一只成分券。"""

    bond: BondStatic
    face_value: float
    market_value: float
    dv01: float
    krd: Mapping[float, float] = field(default_factory=dict)


@dataclass(frozen=True)
class BenchmarkPortfolio:
    """买入持有阶梯基准。

    ``krd`` 与 ``total_dv01`` 的单位都是 **元/bp**；``weights`` 是各期限的目标市值权重
    （默认等权）。"相对基准 DV01 中性"就是让组合的 KRD 向量贴近这个 ``krd``。
    """

    as_of: date
    legs: tuple[BenchmarkLeg, ...]
    market_value: float
    total_dv01: float
    duration: float
    krd: Mapping[float, float]
    weights: Mapping[float, float] = field(default_factory=dict)
    # 买入持有基准的**固定面值**（起点确定后不再重配，R22）；每日只做 mark-to-market
    faces: Mapping[str, float] = field(default_factory=dict)
    notional: float = 0.0

    @property
    def codes(self) -> tuple[str, ...]:
        return tuple(leg.bond.code for leg in self.legs)


@dataclass(frozen=True)
class MarketContext:
    """策略在决策日 t 能看到的全部信息（**不得包含 t 之后的数据**）。

    ``history`` 是截至 ``as_of``（含）的曲线面板，已被引擎显式截断；
    策略层不允许自己去读文件或再取全样本。
    """

    as_of: date
    curve: Curve
    history: pd.DataFrame
    benchmark: BenchmarkPortfolio
    nav: float
    cash: float
    positions: tuple[PositionView, ...]
    cfg: Config


@dataclass(frozen=True)
class TargetFaces:
    """策略输出的**目标面值**（元，按券代码）。用于"精确基准回放"控制组。

    与 ``TargetKRD`` 不同，面值目标绕过 KRD 求解/整手映射，直接按基准逐腿面值下单，
    用于把"执行调度/求解偏差"从策略行为中分离出来（审查 R37/R38 计划）。
    """

    as_of: date
    faces: Mapping[str, float]
    notes: str = ""

    def __post_init__(self) -> None:
        if not self.faces:
            raise ValueError("TargetFaces.faces 不能为空")
        for code, face in self.faces.items():
            if not isinstance(code, str) or not code:
                raise ValueError(f"TargetFaces 的券代码非法：{code!r}")
            if not np.isfinite(face) or face < 0:
                raise ValueError(f"TargetFaces 面值必须是非负有限数：{code}={face!r}")

    @property
    def total_dv01(self) -> float:
        # 面值目标不携带 KRD；日志/审计只展示面值合计
        return float(sum(self.faces.values()))


@dataclass(frozen=True)
class TargetKRD:
    """策略输出的**目标 KRD 向量**（元/bp，按关键期限分桶）。

    约定：
    - ``krd`` 的键是关键期限（年），值是**该桶的目标 DV01**；
    - 多头组合下各桶通常为正；值必须有限；
    - ``notes`` 记录人类可读的调仓理由（Dashboard/报告直接展示）。
    """

    as_of: date
    krd: Mapping[float, float]
    notes: str = ""

    def __post_init__(self) -> None:
        for tenor, value in self.krd.items():
            if tenor <= 0:
                raise ValueError(f"关键期限必须为正数，实际 {tenor}")
            if not isinstance(value, (int, float)) or value != value:  # NaN 检查
                raise ValueError(f"tenor={tenor} 的 KRD 不是有限数值：{value!r}")

    @property
    def total_dv01(self) -> float:
        return float(sum(self.krd.values()))
