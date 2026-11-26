# 02 数据字典（Data Dictionary）

> **版本**：v1.0（2026-11-06）
> **适用范围**：数据层全部落盘产物 + 质量报告 + manifest 血缘字段。
> **口径总原则**：收益率一律**百分数**（`2.35` = 2.35%，不是 0.0235）；期限一律**年**（浮点，`0.25` = 3M）。
> 与数据契约 [`src/ficc/data/schema.py`](../src/ficc/data/schema.py) 保持一致，契约层是唯一权威。

---

## 0. 一页速查

| 文件 | 内容 | 规模（2026-11-06 实测） |
|---|---|---|
| `data/raw/chinabond/<year>.xlsx` | 中债官网原始年度文件**不可变快照**（2002–2026，25 个）；旧版本归档在 `archive/<year>.<sha12>.xlsx` | 25 个文件，约 3.9 MB |
| `data/versions/<version_id>/` | **不可变版本目录**（内容寻址，发布单元）：`curve_long.parquet` + `curve_raw.parquet` + `repo_rates.parquet`(可选) + `manifest.json` | 每个版本一套完整产物 |
| `data/metadata/current.json` | **原子发布指针**：指向唯一有效的 `version_id`；读者优先走它 | 1 个 JSON |
| `data/processed/curve_long.parquet` | 旧路径兼容副本（= 当前版本 `curve_long.parquet`） | **67,472 行** / 6,204 交易日 / 11 个期限 |
| `data/interim/curve_raw.parquet` | 旧路径兼容副本（含 4/6/8/9Y、40/50Y 等非网格点） | **114,728 行** / 6,204 交易日 |
| `data/processed/repo_rates.parquet` | 旧路径兼容副本：资金面 FR/FDR 回购定盘利率宽表 | 5,454 行 / 2005-01-04 → 2026-11-06 |
| `data/metadata/manifest.json` | 旧路径兼容 manifest（内容 = 当前版本 manifest） | 每条年度文件一条记录 |
| `data/metadata/backfill_2y_report.json` | 2Y 回补报告（成功/失败逐日明细、`version_id`） | 按需生成 |
| `reports/data_quality_suspect.csv` | **可疑跳变点清单**（横截面不一致，只标记不剔除） | 289 行（全样本，2026-11-06 实测） |

生成命令：

```bash
python scripts/fetch_data.py --offline          # 无网络重建（CI / 复现基线）
python scripts/fetch_data.py --source chinabond # 在线增量（当年文件强制刷新，历史年份复用快照）
python scripts/fetch_data.py --allow-coverage-drop  # 显式允许历史交易日减少（默认报错）
PYTHONPATH=src python -m ficc.data.quality --config config/config.yaml   # 质量报告
python scripts/backfill_2y.py --limit 5 --dry-run         # 2Y 回补抽样试跑
python scripts/backfill_2y.py                             # 完整回补 772 天（需联网，约 15 分钟）
```

数据更新行为（`config/config.yaml → data`）：

| 配置 | 默认 | 含义 |
|---|---|---|
| `max_staleness_days` | `5` | 在线模式下最新数据落后今天超过 N 个自然日 → 告警（离线模式为固定截止日，仅 INFO） |
| `refresh_current_year` | `true` | 在线抓取当年（`year >= 今天年份`）文件时强制刷新，旧快照归档到 `data/raw/chinabond/archive/`；历史完整年度继续复用快照 |

---

## 1. 主数据：`curve_long.parquet`

### 1.1 字段表

| 字段 | 类型 | 单位 / 口径 | 取值范围（实测） | 示例 | 说明 |
|---|---|---|---|---|---|
| `date` | `datetime64[ns]` | 交易日（银行间日历，日频） | `2002-01-04` → `2026-11-05`，6,204 天 | `2026-11-05` | 官方估值发布日；无时区 |
| `tenor` | `float64` | 期限（**年**） | `{0.25, 0.5, 1, 2, 3, 5, 7, 10, 15, 20, 30}` | `10.0` | 标准期限网格，见 §1.2 |
| `ytm_pct` | `float64` | 到期收益率（**百分数**） | `0.5072` → `10.5366`（全样本）；近期 10Y ≈ 1.7–2.1 | `2.0425` | 中债国债收益率曲线（到期）标准期限点 |
| `term_label` | `object` | 官方期限标签（字符串） | `"3m","6m","1y",…,"30y"` | `"10y"` | 与 `tenor` 一一对应；便于回溯原始文件 |
| `source` | `object` | 数据源标识 | `"chinabond"` | `"chinabond"` | 备用源可能为 `"akshare"` |
| `curve_name` | `object` | 曲线口径（中债官方名称） | `"中债国债收益率曲线(到期)"` | 同左 | 提醒：是**到期收益率曲线**而非即期/远期曲线 |

行键：`(date, tenor)` **唯一**（契约层强制）；缺失的 `(date, tenor)` **不填充**（保留缺失事实，是否插值由建模模块显式决定）。

### 1.2 标准期限网格（`config.yaml → data.tenors`）

| 标签 | `tenor`（年） | 全样本天数 | 备注 |
|---|---|---|---|
| 3M | `0.25` | 6,204 | 全样本 |
| 6M | `0.5` | 6,204 | 全样本 |
| 1Y | `1.0` | 6,204 | 全样本 |
| 2Y | `2.0` | **5,432** | **2016-01-04 → 2019-01-25 缺 772 个交易日**（官方停发，需 `backfill_2y.py` 回补） |
| 3Y | `3.0` | 6,204 | 全样本 |
| 5Y | `5.0` | 6,204 | 全样本 |
| 7Y | `7.0` | 6,204 | 全样本 |
| 10Y | `10.0` | 6,204 | 全样本，最活跃基准期限 |
| 15Y | `15.0` | 6,204 | 全样本 |
| 20Y | `20.0` | 6,204 | 全样本 |
| 30Y | `30.0` | 6,204 | 全样本 |

**被剔除的原始点**（在 `interim` 保留、在 `processed` 剔除）：

* `0d` 隔夜点：**6,204 行**（每个交易日 1 行；契约要求 `tenor > 0`，不是曲线拟合网格点），
  manifest 记 `dropped_non_positive_tenor=6204`；
* 非网格期限点：**41,052 行**，manifest 记 `dropped_off_grid=41052`，包括
  `1m/2m/9m`（`0.0833/0.1667/0.75`）、`4y/6y/8y/9y`（2016 年起官方停发）、
  `40y/50y`（2009-11-30 起才有，与 30Y 高度相关，且不在 `config.data.tenors` 网格内）。

### 1.3 已知缺口与异常

| 项目 | 事实 | 处理 |
|---|---|---|
| 2Y 缺口 | 2016-01-04 → 2019-01-25，772 个交易日 | 回测起点设为 `2019-01-28`（config）；如需扩样本跑 `scripts/backfill_2y.py`（逐日 10Y 校验，容差 1e-4） |
| 年度文件跨年重叠 | 2024.xlsx 与 2025.xlsx 都含 2024-12-27/30/31（51 组 `(date, tenor)`，102 行，数值完全一致） | 清洗层先去重前做一致性校验，一致才去重（manifest 记 `rows_removed_cross_file_duplicates=51`） |
| 早期极端跳变 | 2002–2004 官方文件存在 >100bp 的单日跳变（最大 777.8bp） | 质量报告如实报告（`n_jumps`/`max_abs_jump_bp`），**不擅自修复**；建模时用稳健统计或截断窗口 |
| 交易日历 | 未引入银行间节假日表 | 质量报告按"周一至周五"近似，法定节假日会计入"缺失工作日"（看连续缺口 ≥2 天判断真缺口） |
| 离线源文件更新 | 快照已存在时**永远解析快照字节**；源文件变化只告警并提示 `--force` | 保证"参与计算的字节 == manifest 指向的字节"；`--force` 才会重新登记源文件并归档旧快照 |
| 历史交易日减少 | 默认报错（逐年覆盖退化闸门） | 需显式 `--allow-coverage-drop`；新增年份/新增交易日不受影响 |

---

## 2. 原始长表：`curve_raw.parquet`（interim）

字段与 `curve_long` 相同（`date/tenor/ytm_pct/term_label/source/curve_name`），差异：

* 保留全部官方期限点：`0d`、`1m/2m/9m/4y/6y/8y/9y/40y/50y` 等非网格点都在；
* 行数 114,728 = 25 个年度文件原始 114,779 行 − 跨年重复 51 行；
* 已做**跨年度文件合并 + 一致性去重**，但未做网格裁剪；
* 用途：回溯"数据去哪了"、复核清洗规则、写数据治理报告。

---

## 3. 原始官方 xlsx 字段（`data/raw/chinabond/<year>.xlsx`）

年度文件（`www.chinabond.com.cn` 存档 + `downYearBzqx` 当年接口）：

| 原始列名 | 映射到 | 说明 |
|---|---|---|
| `日期` | `date` | 可能是 `datetime`、`"2016-01-04"` 或 `"2026/01/04"` 字符串，解析器统一容错 |
| `标准期限说明` | `term_label` | `0d/1m/2m/…/50y`，**期限解析优先用标签**（官方把 1M 写成 0.08 年） |
| `标准期限(年)` | `tenor` | 数值（年） |
| `收益率(%)` | `ytm_pct` | 百分数 |

单日文件（`downBzqxDetail`，2Y 回补用；**实际 xlsx 没有「日期」列**，日期由请求参数给出，
解析器用 `parse_daily_xlsx(path, day=...)` 注入）：

| 原始列名 | 映射到 | 说明 |
|---|---|---|
| `日期` | `date` | 请求日 |
| `曲线名称` | `curve_name` | 可能含多条曲线，解析器只保留含"国债"的曲线 |
| `标准期限(年)` | `tenor` | 数值反推标准期限（0.08 → 1/12 等） |
| `收益率(%)` | `ytm_pct` | 百分数 |

> 数据许可：中债数据版权归中央国债登记结算有限责任公司（"未经允许 请勿转载"）。
> `data/` 已全部 `.gitignore`，仓库**不提交原始数据**，只提交抓取脚本与汇总统计。

---

## 4. 资金面：`repo_rates.parquet`

| 字段 | 类型 | 单位 | 有效区间（实测） | 说明 |
|---|---|---|---|---|
| `date` | `datetime64[ns]` | 交易日 | 2005-01-04 → 2026-11-06（5,454 行） | CFETS 回购定盘利率发布日 |
| `FR001` | `float64` | **百分数** | 2005-01-04 起 | 银行间 1 天期回购定盘利率 |
| `FR007` | `float64` | 百分数 | 2005-01-04 起 | 7 天期回购定盘利率（资金面基准） |
| `FR014` | `float64` | 百分数 | 2005-01-04 起（4,332 个有效值） | 14 天期 |
| `FDR001` | `float64` | 百分数 | 2017-05-31 起（2,354 个有效值） | 银银间 1 天期回购定盘利率 |
| `FDR007` | `float64` | 百分数 | 2017-05-31 起（2,354 个有效值） | **DR007 的代理**（costs.repo_rate_source=FDR007） |
| `FDR014` | `float64` | 百分数 | 2017-05-31 起（2,354 个有效值） | 银银间 14 天期 |

* 数据来源：Phase 0 已落盘 `data/raw_spike/repo_frr_fdr_hist.csv`（AKShare `repo_rate_hist` 按月分片抓取）；
  生产在线模式下由 `ficc.data.sources.akshare_source.AkshareSource.fetch_repo_rates` 分片抓取（需安装 akshare）。
* 允许缺失、**不填充**；回购融资成本计算时应按使用日滚动取最近有效值（由成本模块显式处理）。

---

## 5. manifest 血缘：`data/metadata/manifest.json`

顶层结构：

| 键 | 类型 | 说明 |
|---|---|---|
| `schema_version` | `str` | manifest 结构版本，当前 `"1.0"` |
| `generated_at` | `str` | 生成时间（UTC ISO8601） |
| `generator` | `str` | 生成函数（`ficc.data.loader.fetch_curve_data`） |
| `params` | `object` | `source / offline / force / start / end / min_rows / min_dates / curve_name` |
| `dataset` | `object` | `curve_long.parquet` 的条目（见下） |
| `interim` | `object` | `curve_raw.parquet` 的条目 |
| `funding` | `object \| null` | `repo_rates.parquet` 条目（含 `source_url/source_sha256`） |
| `sources` | `list[object]` | **每个年度文件一条**血缘记录（sha256 即参与计算的快照字节） |
| `checks` | `object` | 清洗/校验统计（去重、丢弃、缺失、期限覆盖、`coverage_regression`） |
| `coverage_by_year` | `object` | 每年交易日数，用于覆盖退化对照 |
| `version_id` | `str` | 内容寻址版本 ID（曲线+原始长表+资金面 sha256 + schema 版本的前 16 位） |
| `artifact_hashes` | `object` | 版本内各文件 sha256（`curve_long/curve_raw/repo_rates`） |
| `backfill_2y` | `object`（可选） | 回补后追加的摘要（仅运行回补后存在） |

`dataset` / `interim` 条目字段：

| 字段 | 类型 | 示例 | 说明 |
|---|---|---|---|
| `name` | `str` | `"curve_long"` | 数据集名 |
| `path` | `str` | `"data/processed/curve_long.parquet"` | 相对项目根路径 |
| `sha256` | `str` | `"c1013717…"` | 文件内容 sha256（数据版本指纹，随 pandas/pyarrow 写入版本略有差异，以 `current.json` 为准） |
| `bytes` | `int` | `354899` | 文件字节数 |
| `rows` | `int` | `67472` | 行数 |
| `n_dates` | `int` | `6204` | 交易日数 |
| `date_min` / `date_max` | `str` | `"2002-01-04"` / `"2026-11-05"` | 日期范围 |
| `tenors` | `list[float]` | `[0.25, …, 30.0]` | 期限集合（年） |
| `columns` | `list[str]` | `["date","tenor",…]` | 列顺序（落盘契约） |
| `missing_cells` / `expected_cells` / `missing_rate` | `int/int/float` | `772 / 68244 / 0.011312` | 标准网格缺失统计 |
| `known_gap_2y` | `object` | `{"start":"2016-01-04","end":"2019-01-25","rows_in_range":7720}` | 已知 2Y 缺口标注 |
| `backfilled_cells` | `int`（可选） | `772` | 回补后新增的 2Y 点数 |

`sources[i]` 字段（每条年度文件）：

| 字段 | 类型 | 示例 | 说明 |
|---|---|---|---|
| `year` | `int` | `2024` | 年度 |
| `path` | `str` | `"data/raw/chinabond/2024.xlsx"` | 快照路径 |
| `source_url` | `str` | 官网附件 URL 或 `file://…/data/raw_spike/…` | 数据来源（离线模式为本地缓存 URI） |
| `fetched_at` | `str` | `"2026-11-06T13:04:15+08:00"` | 抓取时间（离线为源文件 mtime） |
| `sha256` | `str` | 64 位 hex | 原始文件内容哈希 |
| `rows` | `int` | `4712` | 解析后行数（含 0d 与非网格点，原始口径） |
| `date_min` / `date_max` | `str` | `"2002-01-04"` / `"2002-12-31"` | 该文件日期范围 |
| `tenors` | `list[float]` | `[1/12, …, 50.0]` | 该文件出现的期限 |

`checks` 关键字段：

| 字段 | 实测值 | 含义 |
|---|---|---|
| `rows_raw_before_dedupe` | `114779` | 25 个年度文件合并后、去重前行数 |
| `rows_raw_merged` | `114728` | 合并年度文件后、去重后行数 |
| `rows_removed_cross_file_duplicates` | `51` | 跨年重叠删除行数（数值一致性校验通过） |
| `duplicate_groups` | `51` | 重复组数（17 个期限 × 3 个重叠日） |
| `duplicate_rows_in_files` | `102` | 参与重复组的原始行数（51 组 × 2 行） |
| `duplicate_conflicts` | `0` | 数值冲突组数（>0 会直接报错、不落盘） |
| `dropped_non_positive_tenor` | `6204` | 丢弃的 0D 隔夜点数 |
| `dropped_off_grid` | `41052` | 丢弃的非网格期限点数 |
| `dropped_nan` | `0` | 收益率 NaN 行数（官方数据零缺失） |
| `n_dates` | `6204` | 清洗后交易日数 |
| `missing_cells` / `expected_cells` | `772 / 68244` | 标准网格缺失 / 应有 |
| `required_tenors` / `present_tenors` | 11 个标准期限 | 契约要求 vs 实际覆盖 |

### 5.1 版本目录与发布指针 `current.json`

**目录结构**（`data/versions/<version_id>/`，内容寻址、不可变）：

```
data/
├── versions/
│   └── 3f2a9c1e8b7d4a60/           # version_id = 内容哈希前 16 位
│       ├── curve_long.parquet
│       ├── curve_raw.parquet
│       ├── repo_rates.parquet      # 可选（write_funding=True 时）
│       └── manifest.json
├── metadata/
│   ├── current.json                # 原子发布指针（唯一"当前版本"入口）
│   └── manifest.json               # 旧路径兼容副本
├── processed/                      # 旧路径兼容副本（与当前版本字节一致）
│   ├── curve_long.parquet
│   └── repo_rates.parquet
└── interim/curve_raw.parquet       # 旧路径兼容副本
```

**发布协议（R04 整批原子事务）**：

1. 所有产物先写 `data/.staging/version-*/`（曲线 → 原始长表 → 资金面 → manifest），
   并回读校验 manifest 登记的 sha256 与实际字节一致；
2. 新下载/更新的原始 xlsx 快照先 staging，整批校验通过后统一提交到
   `data/raw/chinabond/`（旧快照归档）；
3. 原子 `os.replace` 把 staging 目录改名为 `data/versions/<version_id>/`；
4. 同步旧路径兼容副本（`processed/interim/metadata/manifest.json`）；
5. **最后**原子替换 `data/metadata/current.json`。

任一步失败：`current.json` 保持旧值，旧路径副本尽力回滚；读者（`load_curve_long` /
`load_funding_series`）通过指针只会看到**完整旧版本**或**完整新版本**，不存在半成品。

**`current.json` 格式**：

```json
{
  "schema_version": "1.0",
  "version_id": "3f2a9c1e8b7d4a60",
  "published_at": "2026-11-06T08:30:00+00:00",
  "generator": "ficc.data.loader.fetch_curve_data",
  "dataset":      {"name": "curve_long", "path": "data/versions/3f2a.../curve_long.parquet",
                   "sha256": "...", "rows": 67472, "n_dates": 6204,
                   "date_min": "2002-01-04", "date_max": "2026-11-05",
                   "tenors": [0.25, 0.5, 1.0, 2.0, 3.0, 5.0, 7.0, 10.0, 15.0, 20.0, 30.0],
                   "missing_cells": 772, "expected_cells": 68244},
  "interim":      {"name": "curve_raw", "path": "data/versions/.../curve_raw.parquet", "...": "..."},
  "funding":      {"name": "repo_rates", "path": "data/versions/.../repo_rates.parquet", "...": "..."},
  "manifest_path": "data/versions/3f2a.../manifest.json",
  "coverage_by_year": {"2002": 248, "2003": 245, "...": 0},
  "checks": {"n_dates": 6204, "missing_cells": 772, "coverage_regression": {"allowed": false, "...": "..."}}
}
```

**读取规则**（`load_curve_long()` / `load_funding_series()`）：

* 有 `current.json` 且指向文件存在、sha256 校验通过 → 返回该版本（数据契约校验后）；
* 指针缺失、文件缺失 → 告警并回退旧路径 `data/processed/*.parquet`（向后兼容）；
* 指针声明 sha256 与实际文件不一致 → 抛 `DataValidationError`（版本损坏不允许静默降级）。

---

## 6. 质量报告字段（`python -m ficc.data.quality --suspect-report`）

`QualityReport.summary` 主要键：

| 键 | 含义 | 全样本实测 |
|---|---|---|
| `rows` / `n_dates` | 行数 / 交易日数 | 67,472 / 6,204 |
| `date_min` / `date_max` | 日期范围 | 2002-01-04 / 2026-11-05 |
| `n_tenors` / `tenors` | 期限数 / 期限集合 | 11 / 标准网格 |
| `jump_threshold_bp` | 跳变阈值（bp） | 20.0 |
| `n_jumps` / `max_abs_jump_bp` | 超阈值跳变条数 / 最大跳变 | 1,814 / 777.8bp（集中在 2002–2004） |
| `expected_cells` / `missing_cells` / `missing_rate` | 网格应有 / 缺失 / 缺失率 | 68,244 / 772 / 1.13% |
| `n_missing_weekdays` / `n_gap_runs` / `longest_gap_weekdays` | 缺失工作日 / 连续缺口段数 / 最长缺口 | 443 / 156 / 6（含法定节假日） |
| `dup_duplicate_groups` / `dup_duplicate_rows` | 重复组数 / 重复行数 | 0（清洗后数据） |
| `dup_conflict_groups` / `dup_values_consistent` | 冲突组数 / 数值是否一致 | 0 / `true` |

明细 DataFrame：

* `jumps`：`date / tenor / term_label / prev_date / prev_ytm_pct / ytm_pct / diff_bp / direction`（按 |Δ| 降序）。
* `missing_by_tenor`：`tenor / term_label / n_present / n_expected / missing / missing_rate / first_date / last_date`。
* `coverage`：行=年份、列=期限标签 + `n_dates`，值=当年该期限覆盖的交易日占比（0–1）。
* `missing_days`：`date / weekday / gap_id / gap_start / gap_end / gap_days`（每个缺失工作日一行，同一连续缺口共享 `gap_id`）。
* `duplicates`：`date / tenor / n_rows / n_values / values / consistent`（年度文件重叠检测；`consistent=false` 即真冲突）。

### 6.1 可疑跳变点清单：`reports/data_quality_suspect.csv`

**用途**：标记**横截面不一致型**异常点（多为中债官方文件在 2002–2005 的孤立跳变），
供 PCA/因子分析等下游做离线敏感性分析；用于滚动研究时必须另外处理标签可得时间。**只标记，不剔除、不改原始数据**——
是否过滤属于研究结论，不属于数据清洗。

生成命令：

```bash
make quality                                  # 质量报告 + 可疑清单
python scripts/fetch_data.py --suspect-report # 只读已发布数据，仅生成清单
python -m ficc.data.quality --suspect-report --suspect-out reports/data_quality_suspect.csv
```

**检测判据**（`ficc.data.quality.detect_suspect_dates`，纯函数、无网络）：

| `kind` | 判据 | 默认阈值 |
|---|---|---|
| `idiosyncratic_spike` | 单期限日变动 `|Δ| > spike_bp`，且同日**其他期限日变动中位数** `|median| < cross_median_bp` → 孤立异常点 | `spike_bp=50`；`cross_median_bp=10` |
| `reversal` | `t-1→t` 与 `t→t+1` 方向相反、两段幅度均超阈值，且 t 的变动显著偏离同日其他期限中位数（跳出去又跳回来） | `spike_bp=50`；`reversal_bp=40` |
| `level_outlier` | 收益率超出合理区间（默认全局 `[0.2%, 12%]`，可用 `level_bounds` 按期限分档） | `level_min=0.2`；`level_max=12` |

横截面判据用于筛选人工复核候选，**不能保证区分数据错误与真实行情**。局部期限的真实流动性冲击也会触发，例如下述 2013 钱荒日期；不得把所有命中点称为已确认的官方错误。

**字段表**：

| 字段 | 类型 | 含义 |
|---|---|---|
| `date` | ISO date | 异常点所在的**当日**（`reversal` 为跳出去（或跳回）的那一天） |
| `tenor` | float | 期限（年） |
| `term_label` | str | 期限标签（`3M/1Y/10Y/30Y`…） |
| `kind` | str | `idiosyncratic_spike` / `reversal` / `level_outlier` |
| `prev_value` | float | 前一交易日收益率（%） |
| `value` | float | 当日收益率（%） |
| `next_value` | float | 下一交易日收益率（%） |
| `delta_bp` | float | `(value - prev_value) × 100`，单位 bp |
| `median_cross_delta_bp` | float | 同日**其他期限**日变动中位数（bp），用于横截面一致性判断 |
| `roundtrip_bp` | float | `|Δ_t| + |Δ_{t+1}|`（仅 `reversal` 行有值，其余为 NaN） |
| `severity` | str | `high` / `medium` / `low`（`|Δ|≥200bp` 或往返≥300bp 为 high；≥100/150 为 medium；`level_outlier` 恒为 high） |

同一 `(date, tenor)` 可能同时命中不同类型（例如既 `idiosyncratic_spike` 又 `reversal`），
报告按 `severity` 后 `|delta_bp|` 降序排列。

**全样本实测（2026-11-07 复核现有产物）**：共 **289 条命中记录、229 个唯一 (date, tenor) 点、109 个日期**；96.54% 的命中记录在 2002–2004。共 **289** 条（high 82 / medium 67 / low 140），
按类型 147 / 139 / 3；按年份 `2002:86、2003:81、2004:112、2005:9、2013:1`；
按期限 30Y:111、20Y:63、3M:25、6M:23、15Y:23 等。典型命中：

| date | tenor | kind | delta_bp | roundtrip_bp | 说明 |
|---|---|---|---|---|---|
| 2002-04-26 | 30Y | reversal | +737.4 | 1515.2 | 10.5366% → 2.7586%，单日尖峰后跳回 |
| 2004-08-18 | 3M | reversal | −709.9 | 765.0 | 9.3742% → 2.2754%，短端异常回归 |
| 2004-08-18 | 1Y | reversal | −628.4 | 678.2 | 8.8229% → 2.5387% |
| 2004-08-18 | 2Y | reversal | −525.4 | 568.5 | 8.1279% → 2.8742% |

> 2013-06-27（3M −60bp，其他期限中位数 −3bp）也被标记，但属于"钱荒"真实极端行情，
> 下游可结合宏观事件人工复核；标记≠剔除。

**下游用法与可得时间（2026-11-07 修订）**：

- `idiosyncratic_spike`、`level_outlier` 的判据最早在当日曲线发布后可得；`reversal` 使用 `t+1`，最早下一观测日可确认。当前 CSV 没有 `known_at`，其中 `next_value` 也不可当日使用。全样本清单仅作事后诊断；回测需实现可得时间字段和仅依赖历史的过滤。
- 不要先 `drop` 日期再直接 `diff()`：这会把跨越缺口的多日变动当作单日变动。现有 109 个日期的删除会产生 **72 个跨缺口差分**。离线排除示例应保留原交易日索引，将被标记日置 NaN，再差分并删除不完整差分，因而同时屏蔽异常日前后相邻收益率变化。
- winsor 对照应先统一处理逐期限 Δy，再从同一矩阵计算 PCA 与可观测因子；不能分别对聚合前/后的对象截尾却称为“同口径”。固定阈值也会压低真实极端风险，应并列保留未截尾压力场景。

```python
sus = pd.read_csv("reports/data_quality_suspect.csv", parse_dates=["date"])
# 仅限事后敏感性分析；不是可直接供历史交易使用的当日标签
masked = panel.copy()
masked.loc[masked.index.intersection(sus["date"].unique())] = float("nan")
delta_bp = masked.diff().mul(100).dropna(how="any")
```

**入口契约待修复（R20）**：生产长表的冲突去重已有校验，但新 `quality._panel_from_long` 使用 `keep="last"`，`reporting.load_panel` 使用 `keep="first"` 而未先检查冲突。两者应复用数据层校验；当前不应绕过生产验证入口喂入未经验证的重复长表。

**防护**：检测函数只读 `curve_long`，不写 `data/`；报告输出固定写入 `reports/`，
不修改版本目录与 manifest。CLI 参数 `--suspect-out` 可改输出路径（测试用）。

---

### 6.2 `known_at`：可疑点的因果可用性（R21）

`reports/data_quality_suspect.csv` 新增 `known_at` 列，表示**该条标记最早可被决策使用的时间**：

| `kind` | `known_at` | 原因 |
|--------|------------|------|
| `reversal` | **下一交易日** | 判据用到 t+1 的数据（跳出去又跳回），当天收盘时不可知 |
| `idiosyncratic_spike` | 当日 | 只用截面一致性，收盘后可判定 |
| `level_outlier` | 当日 | 只用当日水平区间 |

**使用纪律**：下游只允许使用 `known_at <= decision_date` 的记录（`quality.filter_causally_usable()` 已提供）；
**禁止**把 `reversal` 标记当作当日过滤条件，否则就是前视偏差。
实测：289 条记录中 `known_at` 无缺失；139 条 `reversal` 全部满足 `known_at > date`（如 2004-08-18 → 2004-08-19、
2002-04-26 → 2002-04-27），150 条 spike/level 满足 `known_at == date`。

### 6.3 重复记录的校验规则（R20）

质量入口（`run_quality_checks`、`check_jumps`、`detect_suspect_dates`、`write_suspect_report`）统一走
`_dedupe_validated`：**一致重复可去重、冲突重复抛 `DataValidationError` 并列出 date/tenor/各取值**，
结果与输入行序无关。不再有裸 `keep="first"/"last"`。

## 7. 2Y 回补报告（`backfill_2y_report.json`）

| 字段 | 说明 |
|---|---|
| `params` | `start/end/tenor/validate_against/tol/limit/dry_run`（默认校验 10Y，容差 1e-4 = 0.01bp） |
| `n_requested` / `n_success` / `n_failed` | 待补 / 成功 / 失败交易日数 |
| `success_dates` | 成功回补的日期列表（ISO） |
| `failures` | `[{date, reason}]`，逐日失败原因（缺 2Y、缺 10Y、10Y 与年度文件不一致、网络失败等） |
| `output` | `path`（旧路径副本）/`version_path`（版本目录）/`sha256`/`rows` |
| `version_id` | 回补成功后发布的版本 ID（成功时存在） |
| `current_pointer` | 发布指针相对路径（成功时存在） |
| `status` | `ok` / `failed_all` / `failed_ratio` / `dry_run` / `nothing_to_do` |

> 失败率超过 `max_fail_ratio`（默认 5%）或全部失败时**不覆盖**旧 `curve_long.parquet`，只写本报告。
> 成功写盘前会把旧文件备份到 `data/interim/curve_long.pre_backfill.parquet`。

**实测验证（2026-11-06）**：在 `data/processed/curve_long.parquet` 的临时副本上跑完整回补，
`n_requested=772 / n_success=772 / n_failed=0`，逐日 10Y 校验全部通过，回补后
67,472 → **68,244 行**、2Y 覆盖 6,204/6,204 个交易日、`status=ok`。
基线数据（`current.json` 指向的当前版本）**保持不回补**，以匹配回测起点 2019-01-28；
需要扩样本时再显式执行回补脚本并更新 manifest。

---

## 8. 字段口径的 6 条铁律

1. **收益率是百分数**：`2.35` 表示 2.35%；转小数只在建模入口显式 `/100`，绝不混用。
2. **期限是年**：`0.25` = 3M；`1/12` = 1M（非网格，processed 里不存在）。
3. **不填充缺失**：`curve_long` 里没有的点就是官方没有；插值只能由建模层显式做并记录方法。
4. **不静默覆盖**：任何写盘前校验（行数/日期范围/期限覆盖/NaN/重复）失败 → 抛错、保留旧文件。
5. **重复先校验再合并**：同一 `(date, tenor)` 数值不一致 = 冲突 = 报错；一致才去重。
6. **到期收益率 ≠ 总收益**：`ytm_pct` 是估值曲线上的 YTM，不是持有期收益；PnL 必须用全价 + 票息 − 融资成本 − 交易成本另算（见 `docs/00_market_conventions.md`）。

---

## 9. 复现与版本

```bash
# 基线数据（离线，约 5 秒，产出上述全部文件）
python scripts/fetch_data.py --offline

# 数据版本指纹（写报告 / run_id 时引用；以发布指针为准）
python - <<'PY'
import json
p = json.load(open("data/metadata/current.json"))
print("version_id  =", p["version_id"])
print("data_sha256 =", p["dataset"]["sha256"])
print("rows/date_range =", p["dataset"]["rows"], p["dataset"]["date_min"], p["dataset"]["date_max"])
PY
```

* 基线指纹（2026-11-06，由项目 `.venv` 写出）：`dataset.sha256 = c10137171ea2a76c…`，`version_id = 1750b99400f4d3eb`；完整值以 `data/metadata/current.json` 为准（sha256 会随 pandas/pyarrow 写入版本略有差异，内容不变）。
* 数据变化 → sha256 变化 → 旧回测结果自动失效、可追溯（`run_id = sha256(config + data_manifest + git_sha)`）。


### 9.1 服务器独立抓取版本（2026-11-26）

服务器冻结到2026-11-05，曲线67472行、资金5453行；旧资金5454行中的2026-11-06
不在此次截点内。共同截止内每日所有收益率/FR/FDR按小数10位哈希逐日一致；无缺失新增日期、
无重复键。中债2026.xlsx随官网更新原始SHA改变，截止内曲线parquet SHA仍为
`c10137171ea2a76ccb60328d01d2a5baced36a3467531ed8d4b2741ba0675f10`。

AKShare 1.20.70实际接口为`interest_rate_repo_hist_cm`，兼容旧`repo_rate_hist`；
现有loader manifest的`akshare://repo_rate_hist`仍为历史逻辑来源标识，实际接口名、
AKShare版本和命令可查抓取日志及环境锁。分片回看一天以覆盖月末漏取，重复日期先比对全部
FR/FDR数值，冲突报错，不使用keep-last静默吞掉。2022-12-31及2023-12-31无FDR007
观测（旧数据同样缺失），账本使用最近已知值，不生成假观测。

服务器raw_spike兼容CSV由新抓取parquet导出，派生身份/输入输出SHA写入
`DERIVED_FIXTURE_PROVENANCE.json`，用于原有真实数据测试与离线重建；不冒称旧Phase0文件。
服务器离线重建后的两张表逐值最大差均为0；资金parquet容器SHA不同，数值一致。
完整快照位置及核验命令见[服务器指南](04_server_guide.md)。
