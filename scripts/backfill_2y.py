#!/usr/bin/env python3
"""回补 2016-01-04 → 2019-01-25 期间官方年度文件缺失的 2Y 期限点。

背景
----
中债 2016 年起停发 4/6/8/9Y，且年度文件在 2016-01-04 → 2019-01-25 缺 2Y
（772 个交易日，见 ``docs/00_data_feasibility.md``）。单日接口
``downBzqxDetail`` 可逐日取回，但必须校验：用同一天的 **10Y** 与年度文件比对，
容差 ``1e-4``（0.01bp），一致才认为该日数据可信。

行为
----
* 逐日抓取 → 校验 10Y → 把 2Y 合并为**新版本**并原子发布
  （``data/versions/<version_id>/`` + ``data/metadata/current.json``，旧路径兼容副本同步）；
* 失败日期写进 ``data/metadata/backfill_2y_report.json``（不静默跳过）；
* 失败率超阈值 / 全部失败 → 抛错且**不覆盖**旧 parquet；
* 成功发布前把旧曲线备份到 ``data/interim/curve_long.pre_backfill.parquet``。

用法
----
::

    python scripts/backfill_2y.py                      # 跑配置里的默认区间
    python scripts/backfill_2y.py --limit 5 --dry-run  # 抽样试跑，不写盘
    python scripts/backfill_2y.py --start 2018-01-01 --end 2018-03-31

注意：本脚本需要联网（中债 yield 单日接口）。离线环境请先用
``python scripts/fetch_data.py --offline`` 重建基线数据。
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ficc.config import get_config  # noqa: E402
from ficc.data.loader import LoadPaths, backfill_2y  # noqa: E402
from ficc.logging import get_logger  # noqa: E402

log = get_logger("scripts.backfill_2y")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="用中债单日接口回补 2016–2019 缺失的 2Y 期限点（逐日 10Y 校验）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument(
        "--start", default=None, help="起始日期 YYYY-MM-DD（默认 config.backfill.start）"
    )
    parser.add_argument(
        "--end", default=None, help="结束日期 YYYY-MM-DD（默认 config.backfill.end）"
    )
    parser.add_argument("--config", default=None, help="配置文件路径（默认 config/config.yaml）")
    parser.add_argument(
        "--limit", type=int, default=None, help="只处理前 N 个缺失交易日（调试/分批）"
    )
    parser.add_argument("--tol", type=float, default=1e-4, help="10Y 校验容差（百分点）")
    parser.add_argument("--against", type=float, default=10.0, help="校验用期限（年）")
    parser.add_argument("--throttle", type=float, default=None, help="请求间隔秒（默认 config）")
    parser.add_argument("--max-fail-ratio", type=float, default=0.05, help="允许的最大失败率")
    parser.add_argument("--dry-run", action="store_true", help="只抓取校验，不写盘")
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        cfg = get_config(args.config)
        paths = LoadPaths.from_config(cfg)
        result = backfill_2y(
            start=args.start,
            end=args.end,
            validate_against=args.against,
            tol=args.tol,
            throttle_s=args.throttle,
            limit=args.limit,
            dry_run=args.dry_run,
            max_fail_ratio=args.max_fail_ratio,
            cfg=cfg,
            paths=paths,
        )
    except KeyboardInterrupt:
        print("[backfill_2y] 用户中断；旧数据未改动", file=sys.stderr)
        return 130
    except Exception as exc:
        log.error("回补失败：%s", exc)
        print(f"[backfill_2y] 失败：{exc}", file=sys.stderr)
        print("[backfill_2y] 旧数据未改动（失败不覆盖）", file=sys.stderr)
        return 1

    print("=" * 72)
    print("2Y 缺口回补结果")
    print("=" * 72)
    print(
        f"  目标区间        : {result.report['params']['start']} → {result.report['params']['end']}"
    )
    print(f"  待回补交易日    : {result.n_requested}")
    print(f"  成功            : {result.n_success}")
    print(f"  失败            : {result.n_failed}")
    print(
        f"  校验基准        : {result.report['params']['validate_against']:g}Y "
        f"（容差 {result.report['params']['tol']:g}）"
    )
    print(
        f"  状态            : {result.report.get('status')}"
        f"{'（dry-run，未写盘）' if result.dry_run else ''}"
    )
    if result.report_path is not None:
        print(f"  报告            : {result.report_path}")
    if not result.dry_run:
        print(f"  输出            : {result.output_path}")
    if not result.failures.empty:
        print("\n-- 失败明细（前 20 条，全部记录见报告）--")
        print(result.failures.head(20).to_string(index=False))
    print("=" * 72)
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(main())
