"""数据源适配器包。

* :mod:`~ficc.data.sources.chinabond` —— 主源：中债估值中心官网（年度存档 + 单日接口）
* :mod:`~ficc.data.sources.akshare_source` —— 兜底：AKShare（可选依赖，缺失时优雅降级）

统一契约见 :class:`~ficc.data.sources.base.DataSource`。
"""

from __future__ import annotations

from ficc.data.sources.base import DataSource, SourceError
from ficc.data.sources.chinabond import (
    ChinabondSource,
    parse_daily_xlsx,
    parse_official_xlsx,
    parse_spike_csv,
)

__all__ = [
    "ChinabondSource",
    "DataSource",
    "SourceError",
    "parse_daily_xlsx",
    "parse_official_xlsx",
    "parse_spike_csv",
]
