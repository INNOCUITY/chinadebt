"""pytest 公共夹具与全局配置。

原则
----
1. **默认离线**：`make test` 不联网。需要联网的测试打 `@pytest.mark.network`，
   默认被 pytest 的 `-m "not network"` 过滤掉。
2. **确定性**：所有夹具都用固定日期/固定数值，不用随机数；若必须随机，固定 seed。
3. **单一数据源**：夹具里的黄金数据来自 `docs/00_market_conventions.md` 的官方算例，
   改动夹具等于改动验收标准，必须同时更新文档。
"""

from __future__ import annotations

import os
import sys
from datetime import date
from pathlib import Path

# 必须在 import matplotlib 之前设置（沙箱里 ~/.matplotlib 不可写）
os.environ.setdefault("MPLCONFIGDIR", str(Path(__file__).resolve().parents[1] / ".mplconfig"))

import pandas as pd
import pytest

# 让测试无需 pip install -e 也能 import（CI 里通常已安装，这里是双保险）
_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))

from ficc.config import Config, clear_config_cache, get_config  # noqa: E402
from ficc.data.schema import CANONICAL_TENORS  # noqa: E402


@pytest.fixture(scope="session")
def cfg() -> Config:
    """真实配置文件（config/config.yaml）。配置本身也是被测对象。"""
    clear_config_cache()
    return get_config()


@pytest.fixture(autouse=True)
def _clean_config_cache() -> None:
    """每个测试前后清缓存，避免测试之间互相污染。"""
    clear_config_cache()
    yield
    clear_config_cache()


@pytest.fixture
def sample_dates() -> list[date]:
    return [date(2023, 1, 3), date(2023, 1, 4), date(2023, 1, 5)]


@pytest.fixture
def synthetic_long_df(sample_dates: list[date]) -> pd.DataFrame:
    """构造一条形态合理的合成曲线（正斜率、略凸），3 个交易日。"""
    base = {
        0.25: 2.10,
        0.5: 2.18,
        1.0: 2.30,
        2.0: 2.45,
        3.0: 2.55,
        5.0: 2.68,
        7.0: 2.78,
        10.0: 2.90,
        15.0: 3.02,
        20.0: 3.10,
        30.0: 3.18,
    }
    rows = []
    for i, d in enumerate(sample_dates):
        for t in CANONICAL_TENORS:
            # 用极小的固定扰动模拟日间变化，保证确定性（不用随机数）
            rows.append({"date": d, "tenor": t, "ytm_pct": base[t] + 0.01 * i})
    return pd.DataFrame(rows)


@pytest.fixture
def synthetic_curve_frame() -> pd.DataFrame:
    """单日、11 个期限点的曲线（用于曲线拟合的快速单测）。"""
    base = {
        0.25: 2.10,
        0.5: 2.18,
        1.0: 2.30,
        2.0: 2.45,
        3.0: 2.55,
        5.0: 2.68,
        7.0: 2.78,
        10.0: 2.90,
        15.0: 3.02,
        20.0: 3.10,
        30.0: 3.18,
    }
    return pd.DataFrame(
        {
            "date": [date(2023, 1, 3)] * len(base),
            "tenor": list(base),
            "ytm_pct": list(base.values()),
        }
    )


@pytest.fixture
def real_spike_csv() -> Path:
    """Phase 0 实测落盘的中债曲线（若不存在则跳过相关测试）。"""
    p = (
        Path(__file__).resolve().parents[1]
        / "data"
        / "raw_spike"
        / "cb_gzqx_standard_terms_2002_2026.csv"
    )
    if not p.exists():
        pytest.skip(f"缺少 Phase 0 实测数据: {p}（运行 scripts/spike/fetch_chinabond_official.py）")
    return p


@pytest.fixture
def near_par_bond():
    """26 附息国债 10：10Y、票面 1.72%、半年付、起息 2026-05-15、到期 2036-05-15。

    来自 docs/00_market_conventions.md §2.3 官方算例（财政部发行通知）。
    """
    from ficc.data.schema import BondStatic

    return BondStatic(
        code="260010.IB",
        issue_date=date(2026, 5, 15),
        maturity_date=date(2036, 5, 15),
        coupon_pct=1.72,
        freq=2,
        accrual="coupon",
    )
