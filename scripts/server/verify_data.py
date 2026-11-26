"""运行前硬校验：必须有真实曲线和 FDR007，且覆盖冻结研究窗口。"""

from __future__ import annotations

import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT / "src"))

import numpy as np  # noqa: E402
import pandas as pd  # noqa: E402

from ficc.data.loader import load_curve_long, load_funding_series  # noqa: E402
from ficc.data.schema import CANONICAL_TENORS  # noqa: E402


def main() -> None:
    for name in ("curve_long.parquet", "repo_rates.parquet"):
        if not (ROOT / "data" / "processed" / name).is_file():
            raise ValueError(f"缺少 data/processed/{name}；请导入完整数据包或离线重建")
    pointer = ROOT / "data" / "metadata" / "current.json"
    if pointer.exists():
        payload = json.loads(pointer.read_text())
        for key in ("dataset", "funding"):
            entry = payload.get(key) or {}
            if entry.get("path"):
                path = Path(entry["path"])
                resolved = (ROOT / path).resolve()
                if path.is_absolute() or not resolved.is_relative_to(ROOT / "data"):
                    raise ValueError(f"current.json {key} 路径不可移植：{path}")
    curve = load_curve_long()
    funding = load_funding_series()
    panel = curve.pivot(index="date", columns="tenor", values="ytm_pct")
    sample = panel.loc["2019-01-31":"2025-12-31"].reindex(columns=list(CANONICAL_TENORS))
    if (
        sample.empty
        or sample.index.min() != pd.Timestamp("2019-01-31")
        or sample.index.max() != pd.Timestamp("2025-12-31")
        or not np.isfinite(sample.to_numpy()).all()
    ):
        raise ValueError("曲线未完整覆盖 2019-01-31→2025-12-31 的所需期限；不自动插值")
    if "FDR007" not in funding:
        raise ValueError("缺少 FDR007，拒绝以配置常数冒充真实融资")
    valid = funding.loc[np.isfinite(funding["FDR007"])]
    if (
        valid.empty
        or valid["date"].min() > pd.Timestamp("2019-01-31")
        or valid["date"].max() < pd.Timestamp("2025-12-31")
    ):
        raise ValueError("真实 FDR007 覆盖不足；请同步资金面数据")
    # 部分旧测试直接读 processed；不能让它与 pointer 指向的输入分叉。
    for frame, name, keys, values in (
        (curve, "curve_long.parquet", ["date", "tenor"], ["ytm_pct"]),
        (funding, "repo_rates.parquet", ["date"], ["FDR007"]),
    ):
        legacy = pd.read_parquet(ROOT / "data" / "processed" / name)
        legacy["date"] = pd.to_datetime(legacy["date"])
        left = frame.set_index(keys)[values].sort_index()
        right = legacy.set_index(keys)[values].sort_index()
        if not left.index.equals(right.index) or not np.allclose(
            left.to_numpy(), right.to_numpy(), rtol=0, atol=1e-12, equal_nan=True
        ):
            raise ValueError(f"processed/{name} 与 current 版本不同，需统一重建后再验收")
    print(
        json.dumps(
            {
                "curve_rows": len(curve),
                "curve_dates": curve["date"].nunique(),
                "curve_end": str(curve["date"].max().date()),
                "funding_rows": len(valid),
                "funding_end": str(valid["date"].max().date()),
                "calendar_source": "与项目一致：曲线观测交易日",
                "frozen_window": ["2019-02-01", "2025-12-31"],
            },
            ensure_ascii=False,
            indent=2,
        )
    )


if __name__ == "__main__":
    main()
