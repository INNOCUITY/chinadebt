"""日频收益的 HAC（Newey-West）与固定块 bootstrap 区间估计。

**预注册参数（写死在常量里，禁止按结果调整）**
------------------------------------------------
- ``HAC_LAG = 5``：Newey-West 滞后阶数（Bartlett 核）；
- ``BOOTSTRAP_BLOCK = 20``：固定块长度（交易日）；
- ``BOOTSTRAP_REPS = 2000``：bootstrap 次数；
- ``BOOTSTRAP_SEED = 20261108``：固定随机种子（``numpy.random.default_rng``）；
- 年化口径 ``ANNUAL_DAYS = 250``、95% 区间用标准正态临界值 ``Z_95``。

**统计口径与限制（必须随结果一起披露）**
- 输入是日频主动收益 ``active_t = r_strategy,t − r_benchmark,t``（小数，不是 bp）；
- 年化均值 = ``mean(active) × 250``，报告时 ×1e4 转 bp；
- HAC：``Var(mean) = (γ0 + 2Σ_{j=1..L} (1−j/(L+1))γ_j) / n``，t = mean / se；
  CI = mean_ann ± Z_95 × se_ann。仅修正**时间序列自相关**，不修正横截面/多策略多重检验；
- 固定块 bootstrap：有放回抽取长度为 20 的连续块拼接到原长度，重算年化均值，
  取 2.5%/97.5% 分位为 CI；双侧 p 值违反 H0: mean=0 用**百分位法**
  ``p = min(1, 2×min(P(boot≤0), P(boot≥0)))``（+1 平滑，非 studentized）；
- **重叠窗口不是独立样本**：重叠窗口的 CI/p 值只作描述；非重叠窗口数少时
  区间同样只是描述，不能当显著性检验；
- 序列长度 < 2 或常数序列（se=0）时返回 NaN，不伪造区间。
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

import numpy as np

__all__ = [
    "ANNUAL_DAYS",
    "BOOTSTRAP_BLOCK",
    "BOOTSTRAP_REPS",
    "BOOTSTRAP_SEED",
    "HAC_LAG",
    "Z_95",
    "InferenceSummary",
    "annualized_inference",
    "block_bootstrap",
    "newey_west",
]

#: 年化交易日数（与 ``cfg.backtest.annual_days`` 默认一致；调用方显式传入时以调用方为准）
ANNUAL_DAYS = 250
#: 预注册：Newey-West 滞后阶数
HAC_LAG = 5
#: 预注册：固定块 bootstrap 的块长度（交易日）
BOOTSTRAP_BLOCK = 20
#: 预注册：bootstrap 次数
BOOTSTRAP_REPS = 2000
#: 预注册：固定随机种子
BOOTSTRAP_SEED = 20261108
#: 双侧 95% 标准正态临界值
Z_95 = 1.959963984540054


@dataclass(frozen=True)
class InferenceSummary:
    """一组日频收益的 HAC + 块 bootstrap 区间估计结果（单位：bp / 年化）。"""

    n_obs: int
    mean_ann_bp: float
    hac_lag: int
    hac_se_ann_bp: float
    hac_t: float
    hac_ci_low_bp: float
    hac_ci_high_bp: float
    boot_block: int
    boot_reps: int
    boot_seed: int
    boot_mean_ann_bp: float
    boot_ci_low_bp: float
    boot_ci_high_bp: float
    boot_p_value: float
    extra: dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> dict[str, Any]:
        """展开为可写入 CSV 的一行（``extra`` 原样合并）。"""
        return {
            "n_obs": self.n_obs,
            "mean_ann_bp": self.mean_ann_bp,
            "hac_lag": self.hac_lag,
            "hac_se_ann_bp": self.hac_se_ann_bp,
            "hac_t": self.hac_t,
            "hac_ci_low_bp": self.hac_ci_low_bp,
            "hac_ci_high_bp": self.hac_ci_high_bp,
            "boot_block": self.boot_block,
            "boot_reps": self.boot_reps,
            "boot_seed": self.boot_seed,
            "boot_mean_ann_bp": self.boot_mean_ann_bp,
            "boot_ci_low_bp": self.boot_ci_low_bp,
            "boot_ci_high_bp": self.boot_ci_high_bp,
            "boot_p_value": self.boot_p_value,
            **self.extra,
        }


def _as_1d(values: Any) -> np.ndarray:
    arr = np.asarray(values, dtype=float).reshape(-1)
    arr = arr[np.isfinite(arr)]
    return arr


def newey_west(values: Any, *, lag: int = HAC_LAG) -> tuple[float, float, float]:
    """Newey-West 均值标准误（Bartlett 核）。

    Returns
    -------
    (mean, se, t)
        ``mean`` 为样本均值（与输入同单位）；``se`` 为均值标准误；``t = mean/se``。
        样本不足 2 或 se 为 0 时返回 ``(nan, nan, nan)``（不伪造显著性）。
    """
    if lag < 0:
        raise ValueError(f"lag 必须非负，实际 {lag!r}")
    arr = _as_1d(values)
    n = arr.size
    if n < 2:
        return float("nan"), float("nan"), float("nan")
    mean = float(arr.mean())
    deviation = arr - mean
    gamma0 = float(deviation @ deviation) / n
    variance = gamma0
    upper = min(int(lag), n - 1)
    for j in range(1, upper + 1):
        weight = 1.0 - j / (lag + 1.0)
        gamma_j = float(deviation[j:] @ deviation[:-j]) / n
        variance += 2.0 * weight * gamma_j
    variance = max(variance, 0.0)
    se = float(np.sqrt(variance / n))
    t_stat = mean / se if se > 0.0 else float("nan")
    return mean, se, t_stat


def block_bootstrap(
    values: Any,
    *,
    block: int = BOOTSTRAP_BLOCK,
    reps: int = BOOTSTRAP_REPS,
    seed: int = BOOTSTRAP_SEED,
    annual_days: int = ANNUAL_DAYS,
) -> dict[str, float]:
    """固定块 bootstrap 的年化均值区间与双侧 p 值（单位 bp）。

    块长度 ``block``（交易日）、``reps`` 次、``rng = numpy.random.default_rng(seed)``；
    返回 ``boot_mean_ann_bp / boot_ci_low_bp / boot_ci_high_bp / boot_p_value``。
    样本不足或参数非法时对应值为 NaN。
    """
    if block < 1 or reps < 1:
        raise ValueError(f"block/reps 必须为正整数，实际 block={block!r}, reps={reps!r}")
    arr = _as_1d(values)
    n = arr.size
    nan = float("nan")
    if n < 2:
        return {
            "boot_mean_ann_bp": nan,
            "boot_ci_low_bp": nan,
            "boot_ci_high_bp": nan,
            "boot_p_value": nan,
        }
    effective_block = min(int(block), n)
    blocks_per_sample = int(np.ceil(n / effective_block))
    rng = np.random.default_rng(seed)
    starts = rng.integers(0, n - effective_block + 1, size=(int(reps), blocks_per_sample))
    offsets = np.arange(effective_block)
    indices = (starts[:, :, None] + offsets[None, None, :]).reshape(
        int(reps), blocks_per_sample * effective_block
    )[:, :n]
    samples = arr[indices]
    boot_means_ann_bp = samples.mean(axis=1) * float(annual_days) * 1e4
    low, high = np.percentile(boot_means_ann_bp, [2.5, 97.5])
    n_reps = boot_means_ann_bp.size
    left = int(np.count_nonzero(boot_means_ann_bp <= 0.0))
    right = int(np.count_nonzero(boot_means_ann_bp >= 0.0))
    raw_p = 2.0 * min((left + 1) / (n_reps + 1), (right + 1) / (n_reps + 1))
    return {
        "boot_mean_ann_bp": float(boot_means_ann_bp.mean()),
        "boot_ci_low_bp": float(low),
        "boot_ci_high_bp": float(high),
        "boot_p_value": float(min(1.0, raw_p)),
    }


def annualized_inference(
    values: Any,
    *,
    annual_days: int = ANNUAL_DAYS,
    lag: int = HAC_LAG,
    block: int = BOOTSTRAP_BLOCK,
    reps: int = BOOTSTRAP_REPS,
    seed: int = BOOTSTRAP_SEED,
) -> InferenceSummary:
    """对日频收益做 HAC + 块 bootstrap 的**年化均值**区间估计（bp）。

    Parameters
    ----------
    values : array-like
        日频收益（小数）。主动收益或策略收益均可，调用方负责口径（如策略−基准）。
    annual_days, lag, block, reps, seed
        年化天数与预注册统计参数；默认即预注册值，脚本内只在 ``--quick`` 时减少
        bootstrap 次数以便测试（在输出中显式记录 ``boot_reps``）。
    """
    arr = _as_1d(values)
    mean, se, t_stat = newey_west(arr, lag=lag)
    boot = block_bootstrap(arr, block=block, reps=reps, seed=seed, annual_days=annual_days)
    scale = float(annual_days) * 1e4
    return InferenceSummary(
        n_obs=int(arr.size),
        mean_ann_bp=mean * scale if np.isfinite(mean) else float("nan"),
        hac_lag=int(lag),
        hac_se_ann_bp=se * scale if np.isfinite(se) else float("nan"),
        hac_t=float(t_stat),
        hac_ci_low_bp=(mean - Z_95 * se) * scale if np.isfinite(se) else float("nan"),
        hac_ci_high_bp=(mean + Z_95 * se) * scale if np.isfinite(se) else float("nan"),
        boot_block=int(block),
        boot_reps=int(reps),
        boot_seed=int(seed),
        boot_mean_ann_bp=boot["boot_mean_ann_bp"],
        boot_ci_low_bp=boot["boot_ci_low_bp"],
        boot_ci_high_bp=boot["boot_ci_high_bp"],
        boot_p_value=boot["boot_p_value"],
    )
