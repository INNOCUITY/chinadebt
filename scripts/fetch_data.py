#!/usr/bin/env python3
"""抓取并构建中债国债收益率曲线数据集（生产版，带 manifest 血缘）。

产出
----
* ``data/versions/<version_id>/``           **不可变版本目录**（内容寻址）
  * ``curve_long.parquet``                  清洗后建模用长表（标准期限网格）
  * ``curve_raw.parquet``                   合并后的原始长表（含非网格点）
  * ``repo_rates.parquet``                  资金面 FR/FDR 回购定盘利率（可选）
  * ``manifest.json``                       sha256 / 行数 / 日期范围 / 期限 / 来源
* ``data/metadata/current.json``            **原子发布指针**（读者只看到完整版本）
* ``data/raw/chinabond/<year>.xlsx``        官方原始文件不可变快照（旧版本归档在 archive/）
* 旧路径兼容副本：``data/interim/``、``data/processed/``、``data/metadata/manifest.json``

用法
----
::

    # 无网络重建（只用 data/raw_spike 的 25 个官方 xlsx，CI 默认路径）
    python scripts/fetch_data.py --offline

    # 在线增量（已存在的年度快照会直接复用，只补缺失年份/当年）
    python scripts/fetch_data.py --source chinabond

    # 指定区间并强制重抓（会刷新 data/raw/chinabond 快照）
    python scripts/fetch_data.py --start 2019-01-01 --end 2026-11-05 --force

    # AKShare 兜底（可选依赖；仅 2006 起、8 个期限点）
    python scripts/fetch_data.py --source akshare

    # 只读已发布数据，标记横截面不一致的可疑跳变点（不改原始数据）
    python scripts/fetch_data.py --suspect-report

退出码
------
0 = 成功；1 = 抓取/校验/写盘失败（旧数据保留，错误信息在 stderr）；2 = 参数错误。
"""

from __future__ import annotations

import argparse
import sys
from datetime import date
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from ficc.config import get_config  # noqa: E402
from ficc.data.loader import LoadPaths, fetch_curve_data, load_curve_long  # noqa: E402
from ficc.data.quality import print_suspect_summary, write_suspect_report  # noqa: E402
from ficc.logging import get_logger  # noqa: E402

log = get_logger("scripts.fetch_data")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="抓取/重建中债国债收益率曲线数据集（含 manifest 血缘）",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--start", default=None, help="起始日期 YYYY-MM-DD（默认 config）")
    parser.add_argument("--end", default=None, help="结束日期 YYYY-MM-DD（默认 config/最新）")
    parser.add_argument(
        "--source", default="chinabond", choices=["chinabond", "akshare"], help="数据源"
    )
    parser.add_argument(
        "--offline",
        action="store_true",
        help="离线模式：只用 data/raw_spike 缓存重建，保证无网络可跑",
    )
    parser.add_argument("--force", action="store_true", help="忽略本地快照/缓存，强制重新抓取")
    parser.add_argument("--config", default=None, help="配置文件路径（默认 config/config.yaml）")
    parser.add_argument(
        "--raw-spike-dir",
        default=None,
        help="离线缓存目录（默认 data/raw_spike，自动识别 chinabond_gzqx 子目录）",
    )
    parser.add_argument("--min-rows", type=int, default=1000, help="落盘行数下限（数据完整性闸门）")
    parser.add_argument(
        "--skip-funding", action="store_true", help="跳过资金面 repo_rates.parquet 构建"
    )
    parser.add_argument(
        "--allow-coverage-drop",
        action="store_true",
        help="显式允许历史交易日减少（默认逐年覆盖退化会报错）",
    )
    parser.add_argument(
        "--suspect-report",
        action="store_true",
        help="只读已发布数据，标记横截面不一致的可疑跳变点（不抓取、不改原始数据）",
    )
    parser.add_argument(
        "--suspect-out",
        default=None,
        help="可疑点 CSV 输出路径（默认 reports/data_quality_suspect.csv）",
    )
    parser.add_argument("--suspect-top", type=int, default=10, help="摘要打印 TopN 可疑点")
    return parser


def _print_summary(result) -> None:
    ds = result.manifest["dataset"]
    checks = result.manifest["checks"]
    print("=" * 72)
    print("中债国债收益率曲线数据集构建完成")
    print("=" * 72)
    print(
        f"  数据源          : {result.manifest['params']['source']}"
        f"（offline={result.manifest['params']['offline']}）"
    )
    print(f"  交易日          : {ds['n_dates']:,}")
    print(f"  日期范围        : {ds['date_min']} → {ds['date_max']}")
    print(f"  清洗后行数      : {ds['rows']:,}")
    print(f"  期限点          : {ds['tenors']}")
    print(
        f"  网格缺失点      : {ds['missing_cells']:,} / {ds['expected_cells']:,}"
        f"（{ds['missing_rate']:.2%}，2Y 缺口已在 manifest 标注）"
    )
    print(
        f"  跨文件重复      : {checks['rows_removed_cross_file_duplicates']} 行"
        f"（数值一致性校验通过）"
    )
    print(f"  年度血缘记录    : {len(result.manifest['sources'])} 条")
    print(f"  版本目录        : {result.version_dir}")
    print(f"  version_id      : {result.version_id}")
    print(f"  发布指针        : {result.current_pointer_path}")
    print(f"  曲线长表        : {ds['path']}")
    print(f"  原始长表        : {result.manifest['interim']['path']}")
    print(f"  旧路径兼容副本  : {result.paths.curve_processed}")
    print(f"  manifest        : {result.manifest_path}")
    if result.manifest.get("funding"):
        print(f"  资金面序列      : {result.manifest['funding']['path']}")
    print(f"  dataset sha256  : {ds['sha256'][:16]}…")
    staleness = result.staleness or {}
    if staleness.get("stale"):
        print(
            f"  ⚠ 数据新鲜度    : 最新 {staleness.get('max_date')} 落后 "
            f"{staleness.get('staleness_days')} 天（阈值 {staleness.get('threshold_days')} 天）"
        )
    coverage = result.coverage or {}
    if coverage.get("n_missing_dates"):
        print(
            f"  覆盖退化        : 允许减少 {coverage.get('n_missing_dates')} 个历史交易日"
            f"（--allow-coverage-drop）"
        )
    print("=" * 72)


def _run_suspect_report(args: argparse.Namespace) -> int:
    """只读已发布数据，输出可疑跳变清单（不触发抓取、不改原始数据）。"""
    try:
        cfg = get_config(args.config)
        frame = load_curve_long(cfg=cfg)
        start = date.fromisoformat(args.start) if args.start else None
        end = date.fromisoformat(args.end) if args.end else None
        suspect_path, suspects, _ = write_suspect_report(
            frame,
            cfg=cfg,
            output=args.suspect_out,
            start=start,
            end=end,
        )
    except Exception as exc:
        log.error("可疑点检测失败：%s", exc)
        print(f"[suspect] 失败：{exc}", file=sys.stderr)
        return 1
    print_suspect_summary(suspects, output_path=suspect_path, top_n=args.suspect_top)
    print(f"[suspect] 明细已写入 {suspect_path}（原始数据未改动）")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.suspect_report:
        return _run_suspect_report(args)
    try:
        cfg = get_config(args.config)
        paths = LoadPaths.from_config(cfg, raw_spike=args.raw_spike_dir)
        result = fetch_curve_data(
            start=args.start,
            end=args.end,
            source=args.source,
            offline=args.offline,
            force=args.force,
            cfg=cfg,
            paths=paths,
            min_rows=args.min_rows,
            write_funding=not args.skip_funding,
            allow_coverage_drop=args.allow_coverage_drop,
        )
    except KeyboardInterrupt:
        print("[fetch] 用户中断；旧数据未改动", file=sys.stderr)
        return 130
    except Exception as exc:
        log.error("构建失败：%s", exc)
        print(f"[fetch] 失败：{exc}", file=sys.stderr)
        print("[fetch] 已保留旧数据（写盘前校验未通过时不覆盖）", file=sys.stderr)
        return 1
    _print_summary(result)
    return 0


if __name__ == "__main__":  # pragma: no cover - CLI
    raise SystemExit(main())
