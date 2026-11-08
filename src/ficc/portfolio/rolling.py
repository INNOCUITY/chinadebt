"""合成券滚动政策（§12.12-B 冻结版 roll_v1）。

政策（**冻结、版本化**，报告必须携带 ``version``）
--------------------------------------------------
- 触发：任一腿的**剩余期限 < ``roll_trigger_years``** 时，在该交易日把旧券换成新券；
- 新券：**平价发行**——票面 = 当日该期限曲线 YTM、起息 = 触发日、到期 = 起息 + 期限；
- 换券是**显式交易**（卖旧 + 买新），不静默替换：引擎在滚动日提交两笔成交，成本/价差按
  与普通交易相同的口径计提；
- 滚动基准（``benchmark_version="rolling"``）与本政策同源：每次滚动把基准腿换成新券，
  并把该腿市值再平衡回目标权重（同一成本口径），因此滚动基准与买入持有基准**不可混比**，
  必须按版本分别报告。

为什么不能用买持基准做长样本
----------------------------
买入持有基准的债券会到期退出，长样本下腿数逐渐减少、久期结构漂移；滚动基准保持同一
期限阶梯与久期结构，是长样本比较的合格对照。原买入持有基准（``buyhold``）保留用于
2019–2020 短窗口与历史报告复现。
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta

import numpy as np

from ficc.config import Config, get_config
from ficc.curve.curve import Curve
from ficc.data.schema import BondStatic
from ficc.logging import get_logger

__all__ = [
    "ROLL_POLICY_VERSION",
    "RollEvent",
    "RollPolicy",
    "RollingUniverse",
]

log = get_logger("portfolio.rolling")

ROLL_POLICY_VERSION = "roll_v1_trigger1y_parissue"
_CODE_TENOR = re.compile(r"SYN-(?P<tenor>\d+(?:\.\d+)?)Y-")


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
    """合成券付息频率：2/3/5/7Y 年付；10Y 及以上半年付（与基准/宇宙一致）。"""
    return 2 if float(tenor) >= 10.0 else 1


def _normalise_date(value: date | datetime) -> date:
    if isinstance(value, datetime):
        return value.date()
    if not isinstance(value, date):
        raise TypeError(f"date 必须是 datetime.date，实际 {type(value).__name__}")
    return value


def _tenor_of(bond: BondStatic) -> float:
    match = _CODE_TENOR.search(bond.code)
    if match:
        return float(match.group("tenor"))
    return round((bond.maturity_date - bond.issue_date).days / 365.25, 6)


@dataclass(frozen=True)
class RollPolicy:
    """滚动政策参数（从 ``cfg.portfolio.rolling`` 读取；冻结后只读）。"""

    enabled: bool = False
    trigger_years: float = 1.0
    version: str = ROLL_POLICY_VERSION

    @classmethod
    def from_config(cls, cfg: Config | None = None) -> RollPolicy:
        resolved = get_config() if cfg is None else cfg
        rolling = resolved.portfolio.rolling
        return cls(
            enabled=bool(rolling.enabled),
            trigger_years=float(rolling.roll_trigger_years),
            version=str(rolling.version),
        )

    def should_roll(self, bond: BondStatic, day: date) -> bool:
        """剩余期限（按自然日/365）小于触发阈值时需要滚动。"""
        remaining = (bond.maturity_date - _normalise_date(day)).days / 365.0
        return remaining < self.trigger_years

    def issue(self, curve: Curve, day: date, tenor: float) -> BondStatic:
        """平价发行新券：票面 = 当日该期限 YTM，起息 = day，到期 = day + tenor。"""
        day = _normalise_date(day)
        ytm = float(curve.rate(float(tenor)))
        if not np.isfinite(ytm):
            raise ValueError(f"曲线在 {tenor}Y 无有效收益率，无法平价发行")
        return BondStatic(
            code=f"SYN-{float(tenor):g}Y-{day:%Y%m%d}",
            issue_date=day,
            maturity_date=_add_years(day, float(tenor)),
            coupon_pct=float(max(ytm, 0.0)),
            freq=_coupon_frequency(float(tenor)),  # type: ignore[arg-type]
            face_value=100.0,
            accrual="coupon",
            market="IB",
        )


@dataclass(frozen=True)
class RollEvent:
    """一次滚动：旧券退出、新券替换（同一期限桶）。"""

    day: date
    tenor: float
    retired: BondStatic
    issued: BondStatic


@dataclass
class RollingUniverse:
    """维护"当前有效"的合成券集合；滚动时显式替换并记录事件。"""

    policy: RollPolicy
    tenors: tuple[float, ...]
    bonds: dict[float, BondStatic] = field(default_factory=dict)

    @classmethod
    def build(cls, curve: Curve, day: date, *, cfg: Config | None = None) -> RollingUniverse:
        resolved = get_config() if cfg is None else cfg
        policy = RollPolicy.from_config(resolved)
        tenors = tuple(float(t) for t in resolved.portfolio.universe)
        start = _normalise_date(day)
        bonds = {tenor: policy.issue(curve, start, tenor) for tenor in tenors}
        return cls(policy=policy, tenors=tenors, bonds=bonds)

    # ---- 只读视图 ------------------------------------------------------
    def active(self) -> tuple[BondStatic, ...]:
        return tuple(self.bonds[tenor] for tenor in self.tenors)

    def tenor_map(self) -> dict[str, float]:
        return {bond.code: tenor for tenor, bond in self.bonds.items()}

    def active_codes(self) -> set[str]:
        return {bond.code for bond in self.bonds.values()}

    # ---- 滚动 ----------------------------------------------------------
    def roll(self, curve: Curve, day: date) -> list[RollEvent]:
        """按政策检查每个期限桶；返回本次滚动事件（可能为空）。"""
        if not self.policy.enabled:
            return []
        as_of = _normalise_date(day)
        events: list[RollEvent] = []
        for tenor in self.tenors:
            current = self.bonds[tenor]
            if not self.policy.should_roll(current, as_of):
                continue
            issued = self.policy.issue(curve, as_of, tenor)
            if issued.code == current.code:  # pragma: no cover - 防御式
                raise RuntimeError(f"{as_of}: {tenor}Y 新券代码与旧券相同，滚动失败")
            self.bonds[tenor] = issued
            events.append(RollEvent(day=as_of, tenor=tenor, retired=current, issued=issued))
            log.info(
                "滚动 %sY: %s(剩余 %.2fY) -> %s（平价发行 coupon=%.4f%%）",
                tenor,
                current.code,
                (current.maturity_date - as_of).days / 365.0,
                issued.code,
                issued.coupon_pct,
            )
        return events
