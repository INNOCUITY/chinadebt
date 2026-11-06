# 代码变更日志（Code Changelog）

> 本文件是**项目唯一权威的代码变更记录**，也是 Code Review 的载体。
> 维护人：Lead（每轮实现完成后追加）。评审意见请写在文末「Code Review 区」。
>
> 版本：v4 ｜ 创建：2026-11-06 ｜ 最近更新：2026-11-06（第三轮：即期曲线/KRD/PCA/carry-roll，未出信号）

---

## 一、为什么需要这份文件

1. **可追溯**：任何一个数字（净值、残差、DV01）都能回溯到产生它的那次代码变更。
2. **可评审**：以"变更单元（C-xxx）"为粒度评审，而不是对着几千行 diff 看。
3. **可复现**：每条变更都记录验证命令，评审者可以自己跑一遍确认。

## 二、记录规范（新增条目必须遵守）

### 2.1 编号与粒度

- 编号：`C-001`、`C-002`… 顺序递增，**不复用、不删除**（作废的标 `~~删除线~~` 并说明原因）。
- 粒度：**一次提交 / 一个逻辑完整的变更** = 一条记录。不要把"写了 5 个模块"塞进一条。

### 2.2 每条记录的必填字段

| 字段 | 说明 |
|------|------|
| `变更内容` | 做了什么。用动词开头，讲清行为而非实现细节 |
| `原因` | 为什么必须做。关联到需求/文档/评审意见 |
| `影响面` | 改了哪些对外接口、哪些下游模块会受影响 |
| `验证方式` | **可执行的命令**，不是"测试过了" |
| `风险 / 遗留` | 已知问题、临时妥协、TODO |

### 2.3 变更类型标记

`[feat]` 新功能 ｜ `[fix]` 修 bug ｜ `[refactor]` 重构（行为不变） ｜ `[test]` 测试 ｜
`[docs]` 文档 ｜ `[chore]` 工程配置 ｜ `[perf]` 性能

---

## 三、变更索引

| 编号 | 日期 | 类型 | 范围 | 一句话 |
|------|------|------|------|--------|
| C-001 | 2026-11-06 | [chore] | 工程配置 | 建立 pyproject / Makefile / .gitignore / 目录骨架 |
| C-002 | 2026-11-06 | [docs] | 文档 | 项目规划、数据可行性实测、中债口径备忘录 |
| C-003 | 2026-11-06 | [feat] | 核心 | 配置系统 `config.py`（严格 pydantic 校验 + 指纹） |
| C-004 | 2026-11-06 | [feat] | 核心 | 数据契约 `data/schema.py`（长表校验 / 面板转换 / BondStatic） |
| C-005 | 2026-11-06 | [test] | 测试 | 测试基础设施 `tests/conftest.py`（离线夹具 + 官方算例标的） |
| C-006 | 2026-11-06 | [feat] | 数据层 | 中债官网+AKShare 抓取管道、清洗、质量报告、manifest、2Y 回补 |
| C-007 | 2026-11-06 | [feat] | 定价层 | 计息口径、全价/净价、YTM 反解、久期/凸性/DV01 |
| C-008 | 2026-11-06 | [feat] | 曲线层 | Curve 对象、NS/NSS 拟合、参数识别性修复、残差诊断 |
| C-009 | 2026-11-06 | [feat] | 编排/绘图 | `run_pipeline.py`（data/curve 两阶段）+ 统一绘图模块 |
| C-010 | 2026-11-06 | [docs] | 文档 | 数据字典、方法论、规划 v0.3、README、本文件 |
| C-011 | 2026-11-06 | [fix] | 工程配置 | ruff 配置非法选择器、matplotlib 缓存目录/兼容性、pandas 弃用告警 |
| C-012 | 2026-11-06 | [test] | 测试 | 测试基线确立：202 项全绿 |
| C-013 | 2026-11-06 | [fix] | 曲线层 | R01/R02/R09/R07/R10：有界最小二乘、因子口径、参数键、权重透传 |
| C-014 | 2026-11-06 | [fix] | 数据层 | R03/R04/R05/R06：血缘一致、整批原子提交、覆盖闸门、当年刷新 |
| C-015 | 2026-11-06 | [fix] | 定价层 | R08/R11：DV01 差分步长、T+N 结算；补财政部公告 E/F 外部对账 |
| C-016 | 2026-11-06 | [fix] | 配置/编排 | 平坦化符号、主流程权重透传、权重模式落盘 |
| C-017 | 2026-11-06 | [docs] | 文档 | 撤回过强断言、修正贴现应计公式、新增因子识别性/两套产品章节 |
| C-018 | 2026-11-06 | [chore] | 环境 | 项目本地 `.venv` 隔离环境 + `make setup`/`make check` 可复现 |
| C-019 | 2026-11-06 | [test] | 测试 | 测试基线升至 254 项全绿（评审反例全部固化为测试） |
| C-020 | 2026-11-06 | [feat] | 曲线层 | 即期曲线 bootstrap（合成假设显式声明，往返误差 2.84e-14） |
| C-021 | 2026-11-06 | [feat] | 定价层 | KRD 用即期曲线实装（sum(KRD) vs DV01 差 0.1–1.3%） |
| C-022 | 2026-11-06 | [feat] | 定价层 | Carry/Roll-down 分解（T−1Y 目标、事后五项恒等式） |
| C-023 | 2026-11-06 | [feat] | 曲线层 | PCA/因子正交性交叉校验 + 10Y 偏差持续性分析 |
| C-024 | 2026-11-06 | [feat] | 数据层 | 可疑跳变点标记（289 条，只标记不剔除） |
| C-025 | 2026-11-06 | [feat] | 编排 | run_pipeline 新增 factors/bias/krd/carry 四个 stage |
| C-026 | 2026-11-06 | [test] | 测试 | 测试基线升至 **319 项全绿** |

---

## 四、里程碑与测试基线

**当前基线（每次变更后必须 ≥ 此值）**

```
命令：PYTHONPATH=src python3 -m pytest tests/ -q
基线：319 passed / 0 failed / 1 deselected（network）/ 约 55 秒（含 6204 天真实数据回归）
```

| 时间 | 测试数 | 通过 | 失败 | 跳过 | 备注 |
|------|--------|------|------|------|------|
| 2026-11-06 初 | 0 | — | — | — | 骨架阶段 |
| 2026-11-06 一轮后 | 202 | 202 | 0 | 1（network） | 数据/定价/曲线/编排/绘图全覆盖 |
| 2026-11-06 评审修复后 | 254 | 254 | 0 | 1（network） | 新增 52 项：评审 R01–R11 反例 + 外部对账 E/F + 可观测因子 + 原子性/覆盖闸门 |
| 2026-11-06 第三轮（本轮） | **319** | 319 | 0 | 1（network） | 新增 65 项：bootstrap 19、PCA/因子 11、KRD 12、carry 16、可疑点 8（含真实数据回归） |

**统一验证命令（评审时按需执行）**

```bash
cd /Users/aq/quant/china_debt

# 1. 全部离线测试（不联网，必须全绿）
PYTHONPATH=src python3 -m pytest tests/ -q

# 2. 代码规范（ruff 装在项目本地 .venv，不污染 anaconda）
.venv/bin/ruff check src tests scripts
.venv/bin/ruff format --check src tests scripts

# 3. 配置可加载（改过 config.yaml 必跑）
PYTHONPATH=src python3 -c "from ficc.config import get_config; c=get_config(); print('cfg ok', c.hash())"

# 4. 数据管道（离线重建，不联网）
make data && make quality

# 5. 最小链路：数据 → 曲线拟合 → 图
make fit        # = run_pipeline.py --stage curve
```

---

## 五、详细变更记录

### C-001 [chore] 工程骨架

- **日期**：2026-11-06
- **变更内容**：建立 `pyproject.toml`、`Makefile`、`.gitignore`（**排除全部 `data/`**，中债数据不可再分发）、完整目录树。
- **原因**：项目从零启动，需要统一的构建/测试/格式化入口；数据许可要求原始数据不入库。
- **影响面**：全仓库。
- **验证方式**：`make help`；`python3 -c "import tomllib;tomllib.load(open('pyproject.toml','rb'))"`。
- **风险/遗留**：Makefile 中的 backtest/risk 等 target 依赖尚未实现的脚本。

### C-002 [docs] 规划与两份调研备忘录

- **日期**：2026-11-06
- **变更内容**：`docs/01_project_plan.md`、`docs/00_data_feasibility.md`（14 个接口实测）、`docs/00_market_conventions.md`（中债官方口径 + 两组自算校验）。
- **原因**：先验证"数据能不能拿到""金融口径对不对"两个致命风险，再写代码。
- **影响面**：`config/config.yaml` 的全部默认值来源于此。
- **验证方式**：见两份 memo 的"复现命令"章节。
- **风险/遗留**：memo 中 8 条 `[待核实]` 参数需后续复核。

### C-003 [feat] 配置系统

- **日期**：2026-11-06
- **文件**：`src/ficc/config.py`
- **变更内容**：14 个 pydantic 子模型严格映射 YAML（`extra="forbid"`）、`frozen=True`、`Config.hash()`（sha256 前 16 位，参与 run_id）、`PathsConfig.abs()/ensure()`、跨字段校验（日期区间、tenors 递增且 ≥4 点）。
- **原因**：金融项目最大的复现性风险是"参数散落 + 事后记不清假设"。集中 + 校验 + 指纹是唯一可靠解法。
- **影响面**：所有模块通过 `get_config()` 读参数；新增参数必须同时改 YAML 与模型（有意为之的摩擦）。
- **验证方式**：`PYTHONPATH=src python3 -c "from ficc.config import get_config;print(get_config().hash())"` → `456bbe889c035698`
- **风险/遗留**：`hash()` 随任何参数改动而变（设计意图：参数变了结果就不可比）。

### C-004 [feat] 数据契约

- **日期**：2026-11-06
- **文件**：`src/ficc/data/schema.py`
- **变更内容**：常量 `CANONICAL_TENORS`（11 点）/`LABEL_TO_TENOR`/`TENOR_LABELS`；模型 `CurveQuote`、`BondStatic`、`BondValuation`（强制 `dirty = clean + accrued`）；函数 `validate_long_frame()`（12 类检查）、`to_panel()`/`panel_to_long()`/`to_long_frame()`。
- **原因**：数据层与建模层之间需要唯一约定，并把"缺失值不许静默带入模型"变成硬约束。
- **影响面**：长表 `(date, tenor, ytm_pct)` 成为全项目数据总线；`BondStatic` 被定价层复用。
- **验证方式**：`PYTHONPATH=src python3 -m pytest tests/test_schema.py -q`（17 项）
- **风险/遗留**：`to_panel()` 对缺失点保留 `NaN`，是否插值由建模层显式决定（避免隐式插值引入未来信息）。

### C-005 [test] 测试基础设施

- **日期**：2026-11-06
- **文件**：`tests/conftest.py`、`pyproject.toml`
- **变更内容**：默认 `-m "not network"`（离线确定性）；夹具 `cfg`、`synthetic_long_df`、`synthetic_curve_frame`、`real_spike_csv`（缺失即 skip）、`near_par_bond`；提前设置 `MPLCONFIGDIR`；自动清配置缓存。
- **原因**：测试要能表达"金融正确性"，夹具直接来自官方算例而非随手编。
- **影响面**：所有测试复用。
- **验证方式**：`PYTHONPATH=src python3 -m pytest tests/ -q --collect-only`
- **风险/遗留**：合成数据用固定扰动而非随机数，保证确定性。

### C-006 [feat] 数据层：抓取管道 / 清洗 / 质量 / manifest / 2Y 回补

- **日期**：2026-11-06
- **文件**：`src/ficc/data/sources/{base,chinabond,akshare_source}.py`、`clean.py`、`quality.py`、`loader.py`、`scripts/{fetch_data,backfill_2y}.py`、`tests/test_data_{sources,loader,quality}.py`（57 项）、`docs/02_data_dictionary.md`
- **变更内容**：
  - **中债源**：往年走官网存档页→文章页→xlsx 附件；当年走 `downYearBzqx`；单日回补走 `downBzqxDetail`；强制 `r.encoding="utf-8"`（否则中文乱码）；重试+退避+限流。
  - **AKShare 源**：`bond_china_yield` 按年分片、`repo_rate_hist` 按月分片；**可选依赖**，未安装时 warning 降级不影响主流程。
  - **loader**：逐年解析→合并→**跨年重叠一致性校验后去重**→契约校验→原子写盘→manifest 血缘（source_url/fetched_at/sha256/rows/日期范围/tenors）+ 25 条年度记录。
  - **clean**：对齐 11 点网格、显式记录缺失（不填充）、丢弃 0d 与非网格点。
  - **quality**：缺日、跳变>20bp、期限覆盖、缺失率、年度文件重叠检测；`make quality` 可直接出报告。
  - **backfill_2y**：`downBzqxDetail` 逐日回补 2016-01-04~2019-01-25 的 2Y，每补一天用 10Y 与年度文件比对（容差 1e-4）。
- **原因**：Phase 0 的 spike 脚本只能证明"能抓"，不能证明"可复现、可审计、失败不污染数据"。生产化是后续一切研究的前提。
- **影响面**：产出 `data/processed/curve_long.parquet`（**67,472 行 / 6,204 交易日 / 2002-01-04→2026-11-05 / 11 个期限**）、`repo_rates.parquet`（5,454 行）、`interim/curve_raw.parquet`（114,728 行）、`manifest.json`（dataset sha `27c85052…`）。
- **验证方式**：
  ```bash
  make data        # 离线重建，约 5 秒，幂等（同 sha）
  make quality     # 质量报告
  PYTHONPATH=src python3 -m pytest tests/test_data_loader.py tests/test_data_sources.py tests/test_data_quality.py -q
  ```
- **风险/遗留**：
  - **发现并已处理**：中债官方年度文件存在跨年重叠——`2024.xlsx` 与 `2025.xlsx` 都含 2024-12-27/30/31（51 组 / 102 行），值完全一致。loader 去重前先做数值冲突校验，冲突则报错（绝不静默 `keep="first"`）。
  - 2Y 回补已在临时副本真实网络验证 **772/772 成功、10Y 逐日校验全过**，但基线**刻意不回补**（匹配 2019-01-28 回测起点）。
  - 无银行间节假日日历（质量报告按工作日近似，会标出 156 处"疑似缺日"，实为节假日）。
  - 2002–2004 官方极端跳变只报告、不修复。

### C-007 [feat] 定价层：计息口径 / 全价 / 久期 / 凸性 / DV01

- **日期**：2026-11-06
- **文件**：`src/ficc/pricing/{conventions,bond,analytics}.py`、`tests/test_{conventions,pricing,analytics}.py`（51 项）
- **变更内容**：
  - `year_fraction`（ACT/ACT 分段含 2/29；ACT/365 剔除 2/29）、`coupon_dates`、`coupon_period`（`w=T/TS`，**T 明确为结算日→下一个付息日**）、`accrued_interest` 三分支、`settlement_date`（跳周末）。
  - `cashflows`、`dirty_price_from_ytm`（中债 PV 公式）、`clean_price_from_ytm`、`ytm_from_dirty_price`/`ytm_from_clean_price`（brentq + 网格定位）。
  - `macaulay_duration`/`mod_duration`/`convexity`/`dv01`（**全价**×修正久期×1e-4）/`dv01_finite_diff`。
- **原因**：这是全项目金融正确性的地基，后续所有 PnL、风险、归因都建立在此。
- **影响面**：`dv01` 单位统一为「元/100 元面值/bp」；`key_rate_durations` 留 `NotImplementedError`（依赖即期曲线 bootstrap）。
- **验证方式**：
  ```bash
  PYTHONPATH=src python3 -m pytest tests/test_pricing.py tests/test_analytics.py tests/test_conventions.py -q
  ```
- **官方算例回归（全部命中）**：
  | 算例 | 期望 | 实测 |
  |---|---|---|
  | A 26附息国债10 @2026-05-28 y=1.72% | 全价 100.060519 / AI 0.060761 / 净价 99.999759 / 反解 1.719974% / modD 9.1162 / 凸性 92.03 / DV01 0.091217 | 全部一致（容差内） |
  | B 同券 @2026-11-04 y=1.61% | 101.7766 / 0.8086 / 100.9680 / modD 8.6947 / DV01 0.088492 | 全部一致 |
  | C 1Y 利随本清 @2026-11-05 | AI 0.9827 | 一致 |
  | D 30Y @y≈2.04% | modD 21.18 / DV01 0.2291 | 21.1758 / 0.229068（测试注明日期假设） |
- **风险/遗留**：
  - **发现文档错误**：memo §2.2 的贴现分支 `AI = 100 − Pd/T×t` 按字面在起息日得 AI≈100，与"利息单调累积"矛盾。实现按其经济含义 `AI=(100−Pd)×t/T`（折价线性摊销），docstring 写明差异，需中债细则复核。**建议同步修正 `docs/00_market_conventions.md`。**
  - KRD 未实现（依赖下轮的即期曲线 bootstrap），未用"总 DV01×期限占比"糊弄。

### C-008 [feat] 曲线层：Curve / NS / NSS / 拟合 / 参数识别性修复

- **日期**：2026-11-06
- **文件**：`src/ficc/curve/{__init__,_common,curve,ns,nss,fitting}.py`、`scripts/fit_diagnostics.py`、`tests/test_curve.py`(21) + `tests/test_curve_fit.py`(25)
- **变更内容**：
  - `Curve`（frozen dataclass）：线性/Hermite 插值、`rate()`、`forward()`、`shift()`、`shock(parallel/steepen/flatten/butterfly)`、`to_frame()`；外推 clamp 到 ≥0。
  - `fit_ns` / `fit_nss`：scipy `least_squares`，带边界、多起点、`(1-e^{-x})/x` 的 Taylor 展开避免 0/0；残差单位统一 bp。
  - `fit_curve` / `fit_panel` / `residual_summary`：面板逐日拟合，返回 `(参数面板, 残差面板)`。
  - **参数识别性修复**（Lead 在集成时发现的实质缺陷）：自由拟合下 `b0` 在 0–15 间乱跳（43% 超出合理区间、`|Δb0|` p99≈4.97pp），因子暴露不可用。修复为**稳定化估计**（`b0` 由长端观测锚定、`b1/b2/b3∈[-15,15]`、τ 网格），同时**残差仍取自自由拟合**以保住表达能力口径，并在参数面板中暴露 `b0_free`、`rmse_stable_bp`、`unstable` 三列完全透明。
- **原因**：曲线层是"专业性"的地基；而参数不稳定会直接毁掉 level/slope/curvature 因子与后续所有信号。
- **影响面**：`fit_panel(panel, method)` 的返回契约；`reports/curve_fit_params.csv` 的列结构。
- **验证方式**：
  ```bash
  PYTHONPATH=src python3 -m pytest tests/test_curve.py tests/test_curve_fit.py -q
  make fit       # 全样本 6,204 天拟合，约 2 秒
  ```
- **实测结果**：
  | 指标 | NS | **NSS** |
  |---|---|---|
  | RMSE 均值/中位/p95 (bp) | 6.119 / 5.107 / 13.250 | **3.763 / 3.333 / 7.125** |
  | 残差 MAE (bp) | 5.021 | **2.992** |
  | 最差日 | 153.5bp | 156.0bp（2002-04-26） |
  - 稳定性验收：`b0∈[0.5,8]` **100%**，`|Δb0|` 中位 0.010pp、p99 **0.417pp**（目标 <0.5 ✓），53 天标记 `unstable`。
- **风险/遗留**：
  - `b2/b3/τ` 仍弱识别（参数图会跳），建议下轮用 Kalman/状态空间平滑；`b1` 基本稳定可用。
  - 极端畸变日（2002-04-26、2004-08-17、2013 钱荒形态）NSS 数学上无法表达，**不剔除**，已落 `reports/curve_fit_bad_dates.csv` 并写入 docstring——这是模型的真实局限，报告要如实呈现。
  - 即期曲线 bootstrap / `spot_curve()` 未做，阻塞定价层 KRD。

### C-009 [feat] 编排与绘图：run_pipeline + 统一绘图

- **日期**：2026-11-06
- **文件**：`scripts/run_pipeline.py`、`src/ficc/report/figures.py`、`tests/test_pipeline.py`(6) + `tests/test_figures.py`(13)
- **变更内容**：
  - `run_pipeline.py`：`--stage data|curve`、`--all`、`--start/--end/--method`；数据加载**优先数据层标准入口** `ficc.data.loader.load_curve_long()`，缺失才回退 Phase 0 CSV 并显式 warning。
  - `figures.py`：统一风格（英文标签、无 GUI 后端、深色网格）；`plot_curve_heatmap`/`plot_curve_snapshot`/`plot_residual_hist`（稳健裁剪 + 离群计数标注）/`plot_residual_by_tenor`（数值或字符串标签均可）/`plot_param_series`/`plot_nav`（含回撤子图）。
- **原因**：把"数据→曲线→图"串成一条可复现链路；绘图统一出口，避免报告与 Dashboard 各画一套。
- **影响面**：产物 `reports/figures/curve_*.png`、`reports/curve_{fit_params,residual_summary,summary}.csv`。
- **验证方式**：
  ```bash
  PYTHONPATH=src python3 scripts/run_pipeline.py --all
  PYTHONPATH=src python3 -m pytest tests/test_figures.py tests/test_pipeline.py -q
  ```
- **风险/遗留**：`figures.py` 用英文标签（中文字体在 CI 上易缺字变成豆腐块）；若后续要中文图，需引入字体文件。

### C-010 [docs] 文档体系：数据字典 / 方法论 / 规划 v0.3

- **日期**：2026-11-06
- **文件**：`docs/01_project_plan.md`（v0.3）、`docs/02_data_dictionary.md`、`docs/03_methodology.md`、`docs/code.md`、`README.md`、`Makefile`
- **变更内容**：
  - 规划 v0.3：**ML/RL 从"可选"改为"必做"**（目标团队为智能交易团队）、Dashboard 后置到最后一阶段、数据源定为「中债官网 + AKShare」（Wind/iFinD 作为出成果后的升级项）、报告中英双语、仓库先 private 后公开；新增 §2.3「面向智能交易团队的展示策略」、Phase 5 RL 详细设计（环境/奖励/消融/可解释）。
  - 数据字典：字段/单位/口径/取值范围/血缘/manifest 字段。
  - 方法论：NS/NSS、因子分解、情景冲击、定价公式、久期/DV01/KRD、Carry 与 Roll-down、DV01 中性构造、回测纪律、ML/RL 方法论纪律、常见错误对照表。
- **原因**：研究型项目的价值一半在"说清楚"；同时供面试者与评审人独立复核。
- **影响面**：无代码影响。
- **验证方式**：Markdown 渲染检查；`docs/03` 中每个公式都能在代码中找到对应实现与测试。

### C-011 [fix] 工程配置与绘图兼容性修复

- **日期**：2026-11-06
- **文件**：`pyproject.toml`、`src/ficc/report/figures.py`、`src/ficc/data/schema.py`、`tests/conftest.py`、`.gitignore`
- **变更内容**：
  1. **ruff 配置非法**：`select` 里写了 `"Pandas"`（不存在该选择器），导致整个 ruff 配置无法解析。改为 `["E","F","W","I","UP","B","SIM","NPY","RUF"]` 并忽略 `RUF001/2/3`（中文标点）。
  2. `extend-exclude = ["scripts/spike"]`：Phase 0 一次性实测脚本保持原貌以便回溯。
  3. `MPLCONFIGDIR` 指向项目内 `.mplconfig`（沙箱/CI 下 `~/.matplotlib` 不可写，会反复重建缓存并告警）。
  4. matplotlib 字体扫描日志压到 ERROR（启动噪声）。
  5. `panel.stack(dropna=True)` → `stack(future_stack=True)` + `dropna`（pandas ≥2.1 弃用告警）。
  6. `pcolormesh` 的 C 维度修正为 `(n_dates, n_tenors)`。
  7. `boxplot(labels=)` → 按版本自适应 `tick_labels=`（matplotlib 3.9 改名）。
  8. 残差直方图加**稳健横轴裁剪**（否则 421bp 离群值把主体压成一根线），并标注裁剪观测数。
- **原因**：这些是真实阻塞项（ruff 配置错误会让人误以为"lint 通过"其实从未生效）；绘图噪音与兼容性问题会污染 CI 与报告。
- **影响面**：lint/测试/绘图全链路。
- **验证方式**：
  ```bash
  .venv/bin/ruff check src tests scripts          # All checks passed
  .venv/bin/ruff format --check src tests scripts
  PYTHONPATH=src python3 -m pytest tests/ -q      # 202 passed
  ```

### C-012 [test] 测试基线确立

- **日期**：2026-11-06
- **文件**：全部 `tests/`
- **变更内容**：202 项测试，覆盖：配置校验（17）、数据契约（17）、数据源解析/加载/质量（57）、计息与定价（51）、曲线对象与拟合（46）、编排（6）、绘图（13）、官方算例回归、真实数据冒烟、参数稳定性、防重复/防冲突。
- **原因**：用户明确要求"代码的完成需要正确、并且有测试流程"；这也是数据/定价类项目唯一能证明正确性的手段。
- **影响面**：CI 门槛。
- **验证方式**：`make test` → `202 passed, 1 deselected in ~14s`
- **风险/遗留**：`network` 标记的联网测试默认跳过（抓取逻辑用 fake/mock 覆盖）；覆盖率尚未纳入门槛（`make cov` 可看）。

---

### C-013 [fix] 曲线层：评审 R01/R02/R09/R07/R10

- **日期**：2026-11-06
- **文件**：`src/ficc/curve/{_common,curve,fitting,factors}.py`、`scripts/fit_diagnostics.py`、`tests/test_curve*.py`、`tests/test_factors.py`
- **变更内容**：
  1. **R01 有界最小二乘**：`pinv + clip` 替换为**向量化精确有效集 BVLS**（`bounded_linear_least_squares`，触界重解自由系数 + KKT 校验），自由与锚定两条路径均替换；与 `scipy.optimize.lsq_linear(method="bvls")` 在 120 组边界活跃问题上相对误差 ≤ 2.4e-16。
  2. **R02 因子口径**：`unstable` 覆盖 `b0/b1/b2/b3/tau1/tau2`（阈值 8×MAD）+ 新增 `unstable_reason`；**撤回**"slope 可直接作因子"；新增 `factors.py`：`observable_factors`（level=10/15/20/30Y 均值、slope=10Y−2Y、curvature=2×5Y−2Y−10Y）与 `factor_jump_sensitivity`。
  3. **R09 参数键**：NS 规范键 `tau`，接受面板 `tau1` 别名；`Curve.from_params` 端到端一致；缺参数抛 `ValueError`，**禁止静默退化为线性插值**。
  4. **R07**：`fit_diagnostics.py` 显式传 `cfg.curve.weights`，并把权重模式写入 CSV/图标题。
  5. **R10**：`shock` 明确 kind=方向、bp=幅度（`bp<0` 抛错），`shift` 保留带符号平移。
  6. 自由/锚定两套产品的参数、拟合值、残差、指标**分别落盘**（`reports/curve_fit_{params,residuals,fitted}_{ns,nss}_{free,anchored}.csv`）。
- **原因**：评审 R01/R02 指出求解器与因子口径两处阻塞问题——不修则"残差=模型局限"与"slope 可作因子"两个结论都不成立。
- **影响面**：`fit_panel` 返回值语义（双产品）；`unstable_reason` 新列；NS 参数键。
- **验证方式**：
  ```bash
  .venv/bin/python -m pytest tests/test_curve.py tests/test_curve_fit.py tests/test_factors.py -q
  PYTHONPATH=src .venv/bin/python scripts/run_pipeline.py --stage curve
  ```
- **实测结果**：
  | 项 | 修复前 | 修复后 |
  |---|---|---|
  | 2002-04-26 RMSE | 156.035456bp | **94.018719bp**（评审 bvls 参考 93.827460，差 0.19bp 来自 τ 网格/tie-break） |
  | 2004-08-17 RMSE | 76.651005bp | **52.872232bp** |
  | 2023-10-12 b1 | −0.922731（未标记） | 已被标记 `unstable=True`（reason 含 b1，\|Δb1\|=1.4732pp） |
  | 自由 NSS RMSE 中位 | 3.332820（等权） | **3.320854**（等权）/ **3.364281**（配置权重） |
  | 锚定 NSS RMSE 中位 | 未单独验收 | **4.757679**（等权）/ **4.831230**（配置权重） |
- **风险/遗留**：`b1/b2/b3/τ` 仍是弱识别量，只能作诊断；若要作因子必须走因果方案（冻结训练窗口 τ / Kalman），已写入 docstring，本轮不实现。

### C-014 [fix] 数据层：评审 R03/R04/R05/R06

- **日期**：2026-11-06
- **文件**：`src/ficc/data/loader.py`、`sources/chinabond.py`、`scripts/fetch_data.py`、`tests/test_data_*.py`、`docs/02_data_dictionary.md`
- **变更内容**：
  1. **R03 血缘一致**：离线模式**只解析已登记的不可变快照**；源文件变化仅 WARNING 并提示 `--force`；`force` 才重新登记并归档旧快照。保证"参与计算的字节 == manifest 指向的字节"。
  2. **R04 整批原子提交**：所有产物先写 staging → 回读校验哈希 → 提交快照 → 原子改名到 `data/versions/<version_id>/` → 写旧路径兼容副本 → **最后**原子替换 `data/metadata/current.json`（失败回滚旧路径副本）。`load_curve_long()`/`load_funding_series()` 指针优先、回退旧路径、哈希损坏抛 `DataValidationError`。
  3. **R05 覆盖退化闸门**：逐年对照上一版本交易日集合，历史交易日消失默认报错（列出逐年 baseline→new 与缺失日期），`--allow-coverage-drop` 显式放行；新增年份允许；2Y 已知缺口不误报。
  4. **R06 当年刷新与新鲜度**：在线抓取当年文件强制刷新（staging→`archive/`→提交），历史完整年份复用快照；新增 `data.max_staleness_days=5`、`data.refresh_current_year=true`；资金面接在线刷新（akshare 可选，缺失/失败降级 CSV 并 WARNING）。
- **原因**：评审 R03/R04 为阻塞级——"数据版本不一致"会让任何回测结论失去可追溯性；R05/R06 决定系统能否日更、能否发现历史被截断。
- **影响面**：产物布局从"固定路径"变为"版本目录 + 指针"（旧路径保留兼容副本）；`current.json` 成为数据集版本的唯一真相源。
- **验证方式**：
  ```bash
  make data && make quality
  .venv/bin/python -m pytest tests/test_data_loader.py -q -k "r03 or r04 or r05 or r06"
  ```
- **实测结果**：
  - R03：源 +0.1pp 非 force 重建 → parquet sha 不变、manifest 各年 sha 不变、`version_id` 不变；force 后变化且旧快照进入 `archive/`。
  - R04：5 个写入边界（staging_curve/staging_raw/staging_manifest/legacy_copy/pointer）注入 `OSError` 均 → 抛错、指针不变、读者仍读到完整旧版本。
  - R05：删 2020 年 248 天 → 报错并列出明细；删 1 天即拦截；新增年份允许。
  - R06：当年缓存存在时下载调用 0→1；历史年 0 次；落后 >5 天告警。
- **风险/遗留**：`dataset.sha256` 因 parquet 写入器版本变化由 `27c850…` 变为 `c101371…`（内容未变）；以 `current.json` 为准。当前指针 `1750b99400f4d3eb`。

### C-015 [fix] 定价层：评审 R08/R11 + 外部对账补强

- **日期**：2026-11-06
- **文件**：`src/ficc/pricing/{analytics,conventions}.py`、`tests/test_{analytics,conventions,pricing}.py`、`tests/test_pricing_official.py`
- **变更内容**：
  1. **R08**：`dv01_finite_diff` 分母改为 `2*bp`。
  2. **R11**：`settlement_date` 改为**逐个有效工作日**计数（周末成交先顺延到下一工作日再计数，docstring 写明）。
  3. **证据补强**：新增 `tests/test_pricing_official.py`，纳入财政部公告配对案例 E/F，docstring 写明公告号/链接、锁定日期、舍入规则与起息日**假设**。
  4. **措辞**：A–D 改称"memo 自算算例"；"0.0026bp"改述为"净价取整到分后反解的差"；贴现应计 docstring 明确 `100−Pd/T×t` 是缺括号笔误。
  5. **R07 定价侧**：移除 `FACE=100`、`settlement lag=1` 硬编码，新增 `pricing_config` 与 `cfg=` 透传。
- **原因**：R08/R11 是"默认参数下测不出来"的典型缺陷；证据边界问题决定结论能否被外部复核。
- **影响面**：`dv01_finite_diff` 在 bp≠1 时的返回语义；`settlement_date` 的 T+N 行为。
- **验证方式**：`.venv/bin/python -m pytest tests/test_analytics.py tests/test_conventions.py tests/test_pricing_official.py -q`
- **实测结果**：解析 DV01=0.09121737484；bp=0.5 → **0.09121737895**（旧 0.04560868948）、bp=2 → **0.09121744067**（旧 0.18243488133）。周五 2026-11-06：T+1=11-09、**T+2=11-10**、**T+3=11-11**（旧 T+2/T+3 都是 11-09）。E：全价 99.98 反解 1.727293%（5/25）/1.727805%（5/26），与公告 1.73% 差 0.27bp；误按净价得 1.722170%。F：100.71 反解 1.584993%，与 1.59% 差 0.50bp；1.59% 正算 100.677590，与公告差 0.032410 元 ≈ 0.5×DV01。
- **风险/遗留**：F 的起息日 2026-03-25 为**反推假设**（使四位小数与 memo 一致），docstring 已标注未独立核实；bp=5 的相对偏差 4.5e-6 是中心差分 O(bp²) 截断，不引入 Richardson 外推以保持"±bp 中心差分"定义。

### C-016 [fix] 配置与编排：R10 符号、R07 权重透传

- **日期**：2026-11-06
- **文件**：`config/config.yaml`、`scripts/run_pipeline.py`
- **变更内容**：`risk.stress_scenarios` 中平坦化由 `bp: -25` 改为 `bp: 25`（kind 表方向、bp 表幅度）；`run_pipeline.py` 显式传 `cfg.curve.weights`，并把 `model`/`weights_mode` 写入参数面板。
- **原因**：R10 是"配置与 API 双重取反"导致风险情景方向错误的典型；R07 让 YAML 改权重不生效。
- **影响面**：风险情景数值方向；拟合产物的目标函数口径。
- **验证方式**：`PYTHONPATH=src .venv/bin/python -c "from ficc.config import get_config;print([(x['type'],x['bp']) for x in get_config().risk.stress_scenarios])"` → 平坦化为 `(flatten, 25)`；`make fit` 日志打印 `weights=inverse_maturity_bucket`。
- **风险/遗留**：无。

### C-017 [docs] 文档修订：撤回过强断言、修正公式、新增因子章节

- **日期**：2026-11-06
- **文件**：`docs/00_market_conventions.md`、`docs/01_project_plan.md`、`docs/03_methodology.md`
- **变更内容**：
  1. 贴现应计公式统一为 `AI = (100 − Pd) × t/T`，并注明原式 `100 − Pd/T×t` 是缺括号笔误。
  2. "0.0026bp"统一改述为"净价取整到分后反解与 1.72% 的差"，明确不是求根误差。
  3. 撤回"官方算例 A–D 全部对账"表述 → 改为"memo 自算算例 A/B/C/D + 财政部公告外部配对 E/F"。
  4. 新增 `docs/03` §4.0「可观测因子优先」、§4.5「模型参数识别性陷阱（含 2023-10-12 实测反例）」、§4.6「自由拟合与锚定拟合是两个产品」。
  5. 修正残差解释：必须先排除求解器误差，才能把极端日残差归因于模型局限。
- **原因**：评审指出多处结论超出证据边界；文档必须与修复后的实现同步，否则会误导后续研究。
- **影响面**：无代码影响；影响后续所有引用这些结论的分析。
- **验证方式**：`grep -rn "100 - Pd/T\|100 − Pd/T" docs/` 无残留；`docs/03` 中每个公式都能在代码中找到实现与测试。

### C-018 [chore] 项目本地隔离环境

- **日期**：2026-11-06
- **文件**：`Makefile`、`pyproject.toml`、`.gitignore`
- **变更内容**：`.venv` 建成完整开发环境（pandas 2.3.3 / numpy 2.5.4 / scipy 1.18.1 / matplotlib 3.11.2 / pytest 9.2.0 / ruff + editable `ficc`）；`Makefile` 优先使用 `.venv/bin/python`，新增 `make setup`；`make check` = ruff check + format check + pytest。
- **原因**：评审指出"当前 shell 直接 `make check` 因系统 Python 缺 pytest 失败"，且 anaconda 的 pytest 在 readline/pdb 导入时段错误——环境不固定会让"能复现"变成偶然。
- **影响面**：所有 make 目标；CI 基线。
- **验证方式**：`make check` → exit 0；`.venv/bin/python -c "import ficc, pandas, pytest; print('ok')"`。
- **风险/遗留**：`.venv` 启用了 `system-site-packages`（个别未安装依赖回退 anaconda），关键依赖均已在本 venv 内固定版本；`make setup` 可从零重建。

### C-019 [test] 评审反例固化为测试

- **日期**：2026-11-06
- **文件**：`tests/`
- **变更内容**：202 → **254** 项。新增覆盖：BVLS 与 scipy 交叉校验（120 组）、b1 跳变检测、可观测因子、自由/锚定双产品、R03 血缘、R04 五个写入边界注错、R05 覆盖退化、R06 当年刷新/新鲜度/资金面降级、R08 六个 bp 单位不变性、R11 T+N 矩阵（20 组）、财政部公告 E/F、NS 参数别名与缺参报错。
- **原因**：评审的每条反例都必须变成"修复前失败、修复后通过"的测试，否则问题会复发。
- **影响面**：CI 门槛与耗时（约 51 秒，含 6204 天真实数据回归）。
- **验证方式**：`make test`；`.venv/bin/python -m pytest tests/ -q -k "r01 or ... or r11"`。
- **风险/遗留**：真实网络测试仍默认跳过（`-m "not network"`），抓取逻辑用 mock 覆盖。

---

### C-020 [feat] 即期曲线 bootstrap（`src/ficc/curve/bootstrap.py`）

- **日期**：2026-11-06
- **变更内容**：新增 `SpotCurve`（`times / zero_rates_pct / method / assumptions / source_curve_method`，方法 `discount / zero_rate / forward / to_frame`）与 `bootstrap_spot(curve, *, freq_rule=None)`。按平价债券假设顺序剥离：<1Y 零息单利 `df=1/(1+yt)`、1–7Y 年付、10Y+ 半年付、零息利率线性插值、T+1 结算。
- **原因**：中债只公开到期（YTM）曲线，而 KRD、现金流贴现、远期利率都需要即期曲线。
- **影响面**：新增曲线层入口；定价层 KRD 依赖其鸭子类型接口。
- **验证方式**：
  ```bash
  .venv/bin/python -m pytest tests/test_bootstrap.py -q
  .venv/bin/python -m pytest tests/ -q -k bootstrap
  ```
- **实测结果**：三个代表日（2024-06-28 / 2013-06-20 / 2026-11-05）合成平价债往返重估 **最大误差 2.84e-14**（要求 1e-8）；平坦曲线解析解精确复现（年付 `z=100ln(1+y/100)`、单利 `z=100ln(1+yt)/t`）。
- **风险/遗留**：合成曲线非官方即期曲线；平价假设忽略真实样本券票面分布，短端与超长端误差更大（已写入 docstring 与 `docs/03` §2.4）。

### C-021 [feat] KRD 用即期曲线实装（`src/ficc/pricing/analytics.py`）

- **日期**：2026-11-06
- **变更内容**：`key_rate_durations(bond, settlement, spot, *, key_tenors, cfg)` 由 `NotImplementedError` 改为实装：即期曲线贴现 → 关键期限 +1bp（关键点间线性插值、两端平推）→ 重估现金流 → `KRD_k = PV_k − PV0`；新增 `spot_dv01` 供一致性校验。
- **原因**：评审确认 KRD 必须基于即期曲线，不能用"总 DV01 × 期限占比"；这是风险层的必要前置。
- **影响面**：风险层可直接引用 KRD 向量；接口为鸭子类型（`times / zero_rate / discount`），不 import 曲线层模块。
- **验证方式**：`.venv/bin/python -m pytest tests/test_analytics.py -q -k krd`
- **实测结果**：260010.IB（10Y/1.72%/半年付）@2026-05-28：KRD@10Y = −0.085451、sum(KRD) = −0.091961 vs DV01(YTM) = −0.091217（**相对差 0.82%**）；合成 30Y 平价债相对差 0.12%；`sum(KRD)` vs `DV01_spot` 相对差 ~1e-5。
- **风险/遗留**：与 YTM 口径 DV01 差 0.1–1.3% 属口径差异（复利/息票效应），已写入 docstring；时间口径 ACT/365，需与曲线定价日一致。

### C-022 [feat] Carry / Roll-down 分解（`src/ficc/pricing/carry.py`）

- **日期**：2026-11-06
- **变更内容**：`carry_roll_down`（事前）与 `decompose_realized`（事后五项分解）。滚动目标锁定 T−1Y；`D_mod` 由合成平价债全价中心差分解析算；融资利率缺失抛 `FundingRateUnavailableError`（**不默认 0**）。
- **原因**：Carry/Roll-down 是曲线策略的收益主体，也是下一轮回测归因的基础；评审要求先把它做对（T−1Y 而非 7Y）。
- **影响面**：为策略与归因层提供唯一口径；**本轮不产生任何信号/仓位**。
- **验证方式**：`.venv/bin/python -m pytest tests/test_carry.py -q`
- **实测结果**：曲线 1.5%+5bp/年、融资 2%、1 年：9Y 目标 roll = **+0.4109%**（与独立手算 rel 1e-10），错用 7Y = +0.9764%（高估 0.5655pct）；平坦 3%/融资 2% → carry = 1.0000%；+25bp 冲击下 `carry 0 + roll 0.4109 + level −2.0582 + residual 0.0224 = total`（误差 0）；真实数据 5Y/10Y/30Y 年化 carry+roll = 0.778 / 0.719 / 1.314%。
- **风险/遗留**：2Y 的 T−1Y 落到 1Y（利随本清/贴现），无法分解，横截面从 3Y 起算；`curve_level/shape` 为一阶因子近似，误差计入 residual。

### C-023 [feat] PCA / 因子正交性 + 10Y 偏差持续性（`src/ficc/curve/factors.py`, `bias.py`, `reporting.py`）

- **日期**：2026-11-06
- **变更内容**：`pca_factors`（全样本/250 日走前滚动/±10bp winsor 三口径）、`factor_crosscheck`、`orthogonalize`；新增 `bias.py`（滚动均值、ACF/AR(1)/半衰期、ADF/KPSS、分段、多期限对照、`bias_verdict`）与 `reporting.py`（即期对比、PCA 载荷、因子相关图）。
- **原因**：评审要求用可观测因子与 PC1–3 交叉校验，并在"碰信号"之前查清 10Y 系统性偏差是否持续。
- **影响面**：因子口径修正为 `level_all / level_long / slope / curvature`；PCA 降级为交叉校验工具。
- **验证方式**：`.venv/bin/python -m pytest tests/test_pca_factors.py -q`
- **实测结果（关键，含未达标结论）**：
  - level(长端均值)↔PC1 = **0.297**（未达 >0.95）；诊断性 `level_all`↔PC1 = **0.929** → 结论是**定义错位**（PC1 由短端资金面主导），未调整定义凑数；
  - slope↔PC2 = 0.324/0.750/0.426、curvature↔PC3 = 0.022/0.320/0.227（三口径）；
  - 正交化后三因子相关 < 1e-10；
  - 10Y 残差均值 +4.63bp、88.9% 日为正、ADF p=0.0011、KPSS p=0.010、AR(1) φ=0.884（半衰期 5.63 日）、分段 +2.23/+3.34/+7.16/+5.66bp；
  - **判定：持续性溢价，但不可直接作为独立错价信号**（无稳定锚、NSS 非无套利公允价、静态交易负 carry），只能作中性化调整项。
- **风险/遗留**：PCA 载荷时变明显，不可当固定参数；结论依赖 NSS 自由拟合残差，bootstrap 即期曲线落地后可复核。

### C-024 [feat] 数据质量：可疑跳变点标记（`src/ficc/data/quality.py`）

- **日期**：2026-11-06
- **变更内容**：新增 `detect_suspect_dates`（横截面不一致三判据：`idiosyncratic_spike` / `reversal` / `level_outlier`）、`suspect_summary`、`write_suspect_report`；`fetch_data.py` 增加 `--suspect-report`；`make quality` 加入该步骤。
- **原因**：本轮做 PCA 时发现 2002–2005 官方文件存在孤立跳变（30Y 2002-04-26 10.54%→次日 2.76%；2004-08-18 短端 9.37%→2.28%），会污染因子估计。
- **影响面**：**只标记不剔除**，不修改原始数据与版本目录；下游可据清单按 date 排除或 winsor。
- **验证方式**：`make quality`；`.venv/bin/python -m pytest tests/test_data_quality.py -q`
- **实测结果**：检出 **289 条**（2002:86 / 2003:81 / 2004:112 / 2005:9 / 2013:1；类型 idio 147 / reversal 139 / level 3；high 82）；三个指定异常全部命中（2002-04-26 30Y Δ+737.4bp、roundtrip 1515.2bp；2004-08-18 的 3M/1Y/2Y Δ−709.9/−628.4/−525.4bp）。
- **风险/遗留**：2013-06-27（钱荒真实行情）也被标记，需人工区分；阈值是经验值，已写入 docs/02 §6.1。

### C-025 [feat] 编排：新增四个 stage（`scripts/run_pipeline.py`）

- **日期**：2026-11-06
- **变更内容**：`--stage data|curve|factors|bias|krd|carry`，`--all` 按 `data → curve → factors → bias → krd → carry` 顺序执行；各 stage 统一签名并复用库函数（不重复实现）；`krd` 输出 `reports/krd_table.csv`（含 sum(KRD) vs DV01 相对差），`carry` 输出 `reports/carry_roll_table.csv`（3Y 起，年化横截面）。
- **原因**：让"即期曲线 → KRD → carry/roll"这条链一条命令可复现，且产物与报告一一对应。
- **影响面**：`make pipeline` 现在跑完整链路（约 20 秒）。
- **验证方式**：`PYTHONPATH=src .venv/bin/python scripts/run_pipeline.py --all`
- **实测结果**：6 个 stage 全部通过；KRD 交叉校验表：260010 0.82%、par2Y 1.35%、par5Y 1.35%、par10Y 0.74%、par30Y 0.12%。
- **风险/遗留**：`carry` stage 每 20 个交易日采样（非逐日），如需逐日请改 `sample = dates`。

### C-026 [test] 测试基线 254 → 319

- **日期**：2026-11-06
- **变更内容**：新增 65 项：bootstrap 19、PCA/因子 11、KRD 12、carry 16、可疑点 8（含真实数据回归与干净面板零误报）。
- **验证方式**：`make test` → `319 passed, 1 deselected`（约 55 秒）。
- **风险/遗留**：真实网络测试仍默认跳过。

---

## 六、待办与已知问题

| # | 类型 | 描述 | 计划 / 责任 |
|---|------|------|-------------|
| 1 | 缺陷 | 节假日日历未实现（按工作日近似，质量报告会标出节假日为"疑似缺日"） | Phase 2 回测层引入 `CN_IB` 真实日历（`settlement_date` 已按工作日计数，接日历即可） |
| 2 | 缺口 | 2Y 在 2016-01-04→2019-01-25 缺失 772 天 | `scripts/backfill_2y.py` 已验证 772/772；需要时启用 |
| 3 | 缺口 | ~~即期曲线 bootstrap / KRD~~ → 已完成（C-020/C-021） | 风险层可直接用 KRD |
| 4 | 弱识别 | NSS 的 `b1/b2/b3/τ` 仍弱识别，只能作诊断 | 已用可观测因子规避；已新增 `level_all` 以对齐 PC1（见 `docs/03` §4.7） |
| 4b | 结论 | 10Y 存在 +4.63bp 持续性溢价，但**不可作独立错价信号** | 仅作中性化调整项；短期偏离需另做 z-score（未做） |
| 4c | 数据 | 2002–2005 有 289 个可疑跳变点（官方文件问题） | 已标记不剔除；PCA 需 winsor 或按 date 排除 |
| 5 | 待核实 | 交易手续费率、价差分位数等 8 项经验值 | 见 `docs/00_market_conventions.md` §9 |
| 6 | 待核实 | 公告案例 F 的起息日 2026-03-25 为反推假设 | 查财政部原始公告核实 |
| 7 | 设计 | `Config` 严格模式会拒绝新增 YAML 键 | 有意为之；加参数必须同步改模型并登记本文件 |
| 8 | 环境 | `.venv` 启用了 `system-site-packages`（个别依赖回退 anaconda） | 关键依赖已在 venv 内固定；`make setup` 可重建 |
| 9 | lint | `scripts/spike` 被排除；`PD`（pandas）规则需 preview 未启用 | 已在 pyproject 注释说明原因；如需启用另开变更 |
| 10 | 证据 | `forward()` 仅 YTM 近似；YTM 拟合 ≠ 即期曲线；双模型不可混用于归因 | 在 `docs/03` §2 已声明，bootstrap 落地后统一 |

## 七、Code Review 区（请在此写评审意见）

> 填写方式：复制下方模板追加到本节末尾，Lead 会在下轮变更中逐条响应并更新状态。

### 模板

| 字段 | 内容 |
|------|------|
| 关联变更 | C-xxx |
| 位置 | `文件路径:行号` |
| 严重级别 | 阻塞 / 重要 / 建议 / 疑问 |
| 意见 | 具体问题与期望的改法 |
| 状态 | 待处理 / 处理中 / 已解决 |

### 评审意见记录

#### 2026-11-06 独立复核：当前实现尚不宜作为策略与风险层的已验收地基

本次审查阅读了 `docs/` 六份文档，核对数据、定价、曲线、配置、编排及相关测试。
范围是当前文件树：工作目录没有 Git 仓库，无法核对历史 diff 或此前的运行记录。
本轮只登记评审结果，未修业务实现；所有故障注入、重建和图表输出均重定向到临时目录，未覆盖项目基线数据。

**已独立复现的基线**：

- 曲线长表 67,472 行、6,204 个交易日、2002-01-04 至 2026-11-05、11 个期限；资金面 5,454 行。
- 当前 manifest 的 25 个年度快照哈希及 curve_long 哈希均匹配实际文件。临时目录离线重建两次，curve_long 哈希彼此一致且等于项目基线。
- 2024/2025 年文件重叠 51 组、102 行，数值一致，去除 51 行；人为制造不同收益率的重复键，正确抛出 `DataValidationError`。检查后使用 `keep="first"` 或 `keep="last"` 本身没有问题，关键是检查不能被绕过。
- 等权自由拟合：NS RMSE 中位数 5.106710bp，NSS 3.332820bp，NSS 全样本残差 MAE 2.991775bp，改善约 34.74%。这只是当前求解器输出，不能据此证明找到了约束最优解，见 R01。
- NSS 锚定参数对应的 RMSE 中位数为 **4.758624bp**，不是 3.332820bp。2013-06-20 自由/锚定 RMSE 分别为 2.624815/10.848425bp。
- b0 范围 [1.754600, 5.579025]，日差绝对值 p99 为 0.4165095 个百分点；这是 10/15/20/30Y 观测均值的稳定性，不能证明所有 NSS 参数可识别。
- `make lint` 退出码 0，check 通过、46 个文件 format 通过，`scripts/spike` 按配置排除。当前未启用 `PD` 规则；不能将“lint 通过”理解为 pandas 专项规则全部通过，也不能从当前代码证明过去确实跑过 lint。
- 当前 shell 的默认 `python3` 是系统 Python，直接 `make check` 在 pytest 导入处失败。Anaconda 环境直接运行 pytest 又在 readline/pdb 导入时段错误。使用 `PYTHONPATH=src /Users/aq/anaconda3/bin/python -m pytest tests/ -q -p no:debugging` 后，**202 项离线测试通过**（network 默认排除）。这是环境限制及绕过方式，不是业务测试失败。
- 用临时配置重定向 reports 后，`run_pipeline.py --stage curve` 完整运行成功，四张图及参数/残差 CSV 均生成。已有测试未覆盖下列反例。

**R01 [P1 / 阻塞] 系数裁剪不是有界最小二乘，残差被错误解释为模型能力上限**

- 关联变更：C-008。
- 位置：`src/ficc/curve/_common.py:415-417`、`:529-530`。
- 意见：`pinv(X) @ y` 后逐坐标 `clip`，只保证参数在边界内，不保证固定 τ 时的约束最优性；NS/NSS 载荷相关，碰到边界时必须重新优化其他系数。默认面板 `refine=False`，把这个结果直接当最终解并标记 success。
- 证据：相同真实数据、相同 NSS τ 网格、相同 b 边界、相同等权目标，仅改用 `scipy.optimize.lsq_linear(..., method="bvls")`，2002-04-26 RMSE **156.035456 → 93.827460bp**；2004-08-17 **76.651005 → 52.872232bp**。第一天有界解 τ=(6.733412, 8.185000)，b=(20, -14.188416, -16.321091, -20)。原解局部精修至 1000 次评估也仅得到 116.264702bp，说明仅增加精修次数不能代替正确的网格内求解。
- 期望：自由与锚定两条路径都实现真正的有界线性最小二乘；增加边界活跃的反例，与独立求解器交叉校验；重新生成诊断统计，撤回“当前最差日残差全是数学上无法表达”和“精修差异均 <0.05bp”的断言。剩余误差仍可能来自模型局限，不能反过来声称已全部解决。
- 参考：[SciPy 有界线性最小二乘定义](https://docs.scipy.org/doc/scipy/reference/generated/scipy.optimize.lsq_linear.html)。
- 状态：✅ **已解决**（C-013）
- **Lead 响应**：`pinv+clip` 已替换为向量化精确有效集 **BVLS**（触界重解自由系数 + KKT 校验），自由/锚定两条路径均替换；与 `scipy.optimize.lsq_linear(method="bvls")` 在 120 组随机边界活跃问题上相对误差 ≤ 2.4e-16。复测：2002-04-26 **156.035456 → 94.018719bp**、2004-08-17 **76.651005 → 52.872232bp**（与你给出的 bvls 参考 93.827460 相差 0.19bp，来自 τ 网格与 tie-break，已在测试注明）。已撤回"最差日残差全是数学上无法表达"与"精修差异 <0.05bp"两处断言。回归测试：`test_bounded_lstsq_matches_lsq_linear`、`test_r01_*`。

**R02 [P1 / 阻塞] b1 仍有换解跳变，当前稳定性标记无法保护 slope 因子**

- 关联变更：C-008。
- 位置：`src/ficc/curve/fitting.py:218-222`、`:462-469`。
- 意见：`unstable` 只检查 b0，但文档和诊断脚本称 b1 已可直接作因子。锚定 b0 后，b1 仍受 τ 和曲率系数的补偿影响，不能仅凭 b0 稳定验收 level/slope。
- 证据：2023-10-11 → 10-12，各节点收益率最大变动仅 0.04 个百分点（4bp），b0 由 2.843725 变为 2.839150；b1 却从 **-2.395903 变为 -0.922731**（跳 147.317170bp）。τ1 从 0.222332 变为 1.5，τ2 从 0.481116 变为 30；两天 `unstable=False`。全样本 NSS 的 |Δb1| p99 为 0.899533 个百分点。
- 期望：撤回“slope 已可直接使用”；优先以可观测期限利差作 slope，或用训练窗口确定并冻结 τ、因果状态模型等方案；对所有实际使用的因子做跳变/扰动敏感性检验。未来平滑不得利用 t 之后观测。
- 状态：✅ **已解决**（C-013）
- **Lead 响应**：`unstable` 现覆盖 `b0/b1/b2/b3/tau1/tau2`（阈值 8×MAD），并新增 `unstable_reason`；2023-10-12 已标记 `unstable=True`（reason 含 b1，\|Δb1\|=1.4732pp，全样本 b1 触发 429 天）。已撤回"slope 可直接作因子"，新增 `src/ficc/curve/factors.py`：`observable_factors`（level/slope/curvature 由观测直接构造）+ `factor_jump_sensitivity`；同日可观测因子日变动 < 5bp。因果方案（冻结 τ / Kalman）写入 docstring，本轮不实现。测试：`test_b1_jump_detection`、`test_observable_factors`。

**R03 [P1 / 阻塞] 离线输入更新后，manifest 可能为新数据登记旧快照哈希**

- 关联变更：C-006。
- 位置：`src/ficc/data/loader.py:391-402`、`:467-479`。
- 意见：先解析 `raw_spike` 源文件；若目标快照已存在且内容不同，`_copy_snapshot(force=False)` 保留旧快照；随后 `_record_for_snapshot` 却取旧快照的 sha256，并配上新解析数据的元信息。产物与登记的输入版本因此脱节。
- 证据：临时目录先建 2026 年基线，再把源 xlsx 收益率全部增加 0.1 个百分点，非 force 重建成功；processed 收益率随之增加 0.1，但 manifest 的 source hash 仍等于未更新的旧快照。
- 期望：不可变模式应直接解析所登记的旧快照，或在源/快照不同时报错，或另存带内容哈希的新版本；必须保证参与计算的字节与 manifest 指向的字节一致。加入源文件变化后的重建测试。
- 状态：✅ **已解决**（C-014）
- **Lead 响应**：离线模式改为**只解析已登记的不可变快照**；源文件变化仅 WARNING 并提示 `--force`；`force` 才重新登记并归档旧快照。新增测试断言：源 xlsx 全 +0.1pp 后非 force 重建 → parquet sha 不变、manifest 各年 sha 不变、`version_id` 不变；force 后产物变化、manifest 指向新快照、旧快照进入 `archive/`。即"参与计算的字节 == manifest 指向的字节"。

**R04 [P1 / 阻塞] 单文件原子写入不能保证整批产物失败回滚**

- 关联变更：C-006。
- 位置：`src/ficc/data/loader.py:940-953`。
- 意见：interim、processed、funding、manifest 依次替换；后续写入失败时，前面的文件已变更。在线逐年强制抓取也会在整批验证前替换部分 raw 快照。因此“旧 parquet / manifest 原样保留”只覆盖抓取/验证阶段的部分失败路径。
- 证据：临时目录改变输入后，将 `_write_json_atomic` 注入 `OSError`；函数失败，但 processed 哈希已变，manifest 字节仍是旧值。
- 期望：整批输出先写入新的版本目录、完成校验与哈希后再原子切换版本指针；或者实现明确的事务回滚。对每个写入边界注入异常，验证读者只会看到完整旧版本或完整新版本。
- 状态：✅ **已解决**（C-014）
- **Lead 响应**：改为**版本目录 + 原子指针**：staging 写入并回读校验 → 提交快照 → 原子改名到 `data/versions/<version_id>/` → 写旧路径兼容副本 → **最后**原子替换 `data/metadata/current.json`（失败回滚副本）。对 5 个写入边界（staging_curve / staging_raw / staging_manifest / legacy_copy / pointer）注入 `OSError`：均抛错、指针不变、`load_curve_long()`/`load_funding_series()` 仍返回完整旧版本。指针缺失回退旧路径，版本文件哈希损坏抛 `DataValidationError`。

**R05 [P1 / 重要] 完整性闸门可接受几乎整年历史丢失**

- 关联变更：C-006。
- 位置：`src/ficc/data/loader.py:616-696`。
- 意见：总行数/总交易日的固定下限、全局首尾日期、期限集合不能识别中间年度被截断。只要年度文件存在且可解析，即使全年只剩一天，也会通过。
- 证据：在内存中将 2020 年压缩到一个交易日（删除 2,728 行、248 个交易日），其他年份保留；`_validate_curve_dataset` 仍通过，输出为 64,744 行、5,956 天。
- 期望：对历史年份/日期键集合与既有版本做覆盖退化检查；异常减少须明确批准或标注。补入逐年覆盖及缺口校验，不能只靠最终绘图时才发现。这里无需依赖完整节假日日历就能保护已存在历史。
- 状态：✅ **已解决**（C-014）
- **Lead 响应**：新增**逐年覆盖退化检查**：对照上一版本逐年交易日集合，历史交易日消失默认报错并列出"逐年 baseline→new + 缺失日期"，`--allow-coverage-drop` 显式放行；新增年份/新增交易日允许；2Y 已知缺口不误报。删 1 个历史交易日即被拦截（原反例删 248 天已能被识别）。

**R06 [P2 / 重要] 默认在线抓取不会刷新当年缓存，日更配置未兑现**

- 关联变更：C-006。
- 位置：`src/ficc/data/sources/chinabond.py:738-743`；`src/ficc/data/loader.py:647` 附近。
- 意见：已存在年度缓存时，不区分完整历史年和仍增长的当年，一律跳过下载。默认 `end_date=null` 又不做最新日期落后检查，`make fetch` 会持续成功返回同一份旧数据。
- 证据：临时缓存中已有 2026.xlsx，替换下载方法为调用即抛错，`fetch_year(2026)` 仍成功，下载调用次数为 0。
- 期望：在线模式刷新当年文件并保留版本，历史完整年度继续复用；离线模式明确接受固定截止日。资金面也需要单独接通在线刷新：当前 `_build_funding` 只读 spike CSV，未调用已有 `fetch_repo_rates` 接口。
- 状态：✅ **已解决**（C-014）
- **Lead 响应**：在线抓取分年处理——**当年强制刷新**（staging→`archive/`→提交），历史完整年份复用快照（下载调用 0 次）；新增 `data.max_staleness_days=5`（落后告警）与 `data.refresh_current_year=true`（均可配）；资金面接入在线刷新（akshare 可选，缺失/失败降级 CSV 并 WARNING）。测试：`test_current_year_cache_is_refreshed_when_configured` 等 8 项。

**R07 [P2 / 重要] 主流程忽略配置的拟合权重**

- 关联变更：C-003、C-008、C-009。
- 位置：`scripts/run_pipeline.py:181`；`scripts/fit_diagnostics.py:356`。
- 意见：配置和方法论指定 `inverse_maturity_bucket`，但调用不传 weights，底层默认 None，即等权。修改 YAML 不会改变实际目标函数，研究配置与产物不一致。
- 证据：全样本 NSS 等权 RMSE 中位 3.332820bp / MAE 2.991775bp；显式传配置权重后为 **3.371859bp / 2.923847bp**。
- 期望：主入口显式传 `cfg.curve.weights`，产物保存实际权重模式；补主流程配置透传测试。其他配置也应审计：自定义 cfg 没有传入底层 `get_config()` 调用，`data.tenors` 清洗仍使用默认常量。
- 状态：✅ **已解决**（C-016 + C-013）
- **Lead 响应**：`run_pipeline.py` 与 `fit_diagnostics.py` 均显式传 `cfg.curve.weights`，并把 `model`/`weights_mode` 写入参数面板。实测可复现你给出的两组数：等权 NSS RMSE 中位 **3.320854 / MAE 2.991775**；配置权重 **3.364281 / MAE 2.8337**。默认走配置。另审计并移除了定价层的 `FACE=100`、`settlement lag=1` 硬编码，改为 `cfg=` 透传。

**R08 [P2 / 重要] 非 1bp 差分步长下 DV01 单位错误**

- 关联变更：C-007。
- 位置：`src/ficc/pricing/analytics.py:158-161`。
- 意见：返回 `(price_down-price_up)/2` 漏除 `bp`，实际是给定冲击幅度的价格变化，不是每 1bp 的敏感度；默认 bp=1 的测试无法发现。
- 证据：算例 A 解析 DV01=0.09121737484；bp=0.5 时返回 **0.04560868948**，bp=2 时返回 **0.18243488133**，分别少/多一倍。
- 期望：分母改为 `2*bp`，增加 0.1/0.5/2bp 的单位不变性检验。解析 DV01 与默认 1bp 算例目前未发现此问题。
- 状态：✅ **已解决**（C-015）
- **Lead 响应**：分母改为 `2*bp`。实测（解析 DV01 = 0.09121737484）：bp=0.5 → **0.09121737895**（旧 0.04560868948）、bp=1 → 0.09121739130、bp=2 → **0.09121744067**（旧 0.18243488133）。新增 bp∈{0.1,0.25,0.5,1,2,5} 单位不变性 + `bp≤0` 抛错测试。关于 bp=5：绝对偏差 4.1e-7，相对 4.5e-6 是**中心差分 O(bp²) 截断**而非单位错误——保持"±bp 中心差分"定义、不引入 Richardson 外推，测试 docstring 已说明。

**R09 [P2 / 重要] NS 面板不能按公开参数接口重建 Curve**

- 关联变更：C-008。
- 位置：`src/ficc/curve/fitting.py:442` 附近；`src/ficc/curve/curve.py:415`。
- 意见：NS 面板只导出 `tau1`，`Curve.from_params(method="ns")` 却强制读取 `tau`；将面板的一行用于曲线重建会报缺参数。直接构造 Curve 的路径还会因 `_param_array` 缺键静默退化为线性插值。
- 证据：传入 `b0=3,b1=-1,b2=2,tau1=1.5`，`Curve.from_params(..., method="ns")` 抛 `ns 曲线缺少参数 'tau'`。
- 期望：统一 NS τ 键或在构造入口规范化别名；用真实面板行做 rate/shift/重建端到端测试，参数缺失不得静默换模型。
- 状态：✅ **已解决**（C-013）
- **Lead 响应**：NS 规范键为 `tau`，`from_params` 接受面板的 `tau1` 别名并规范化；端到端测试：真实面板一行 → `Curve.from_params` → `rate()` 与拟合值一致（<1e-10）；**缺参数抛 `ValueError`，禁止静默退化插值**。

**R10 [P2 / 重要] 平坦化情景的幅度符号与 API 双重反向**

- 关联变更：C-003、C-008。
- 位置：`config/config.yaml:216`；`src/ficc/curve/curve.py` 的 `_shock_delta_pct`。
- 意见：`flatten` 内部已经取 `steepen` 的负号，配置再传 -25，使“平坦化25bp”实际等于“陡峭化25bp”，风险情景遗漏相反方向。
- 证据：对同一 Curve，`shock("flatten", -25).rates` 与 `shock("steepen", 25).rates` 全部相同。
- 期望：统一 kind 表示方向、bp 表示幅度的接口约定，按当前实现将平坦化配置设为 +25；增加从真实配置读取情景的方向测试。风险模块尚未实现，但当前配置与 Curve 组合已可复现此错误。
- 状态：✅ **已解决**（C-016 + C-013）
- **Lead 响应**：配置改为 `flatten: bp: 25`；`shock` 明确 **kind=方向、bp=幅度（必须 ≥0）**，负值抛 `ValueError`，带符号平移改用 `shift(bp)`；新增"从真实配置读取情景并断言方向"的测试。实测 `flatten(25) != steepen(25)`。

**R11 [P2 / 重要] T+2/T+3 结算按自然日推进后只修正终点**

- 关联变更：C-007。
- 位置：`src/ficc/pricing/conventions.py:216-218`。
- 意见：先加 lag_days 个自然日、仅在终点跳周末，不能计算跨周末的多个工作日滞后。这与已知“未接节假日日历”是两个不同问题。
- 证据：`settlement_date(date(2026,11,6),2)`（周五 T+2）返回 11-09 周一，按当前周一至周五日历应为 11-10 周二。
- 期望：逐个有效工作日计数，之后再接 CN_IB 真实日历；分别测试 T+0/1/2/3 和跨周末。默认 T+1 周末样例不能覆盖此错误。
- 状态：✅ **已解决**（C-015）
- **Lead 响应**：改为逐个有效工作日计数（周末成交先顺延到下一工作日再计数，docstring 写明）。实测周五 2026-11-06：T+1=**11-09**、T+2=**11-10**、T+3=**11-11**（旧实现 T+2/T+3 均为 11-09）。新增 T+0/1/2/3 × {周一/周三/周五/周六/周日} 20 组矩阵测试。

**三个指定问题的审查结论**：

1. **跨年重叠**：已正确解决当前一致重复与数值冲突问题。需要继续修复的是血缘、覆盖退化与整批提交一致性，不应把这些与去重混为一件事。
2. **参数识别性**：长端锚定是人为确定 level 的合理建模选择，不是统计上恢复了全部参数识别。自由残差与锚定因子可以作为两个研究产品并存，但应分别保存完整参数、拟合值、残差、误差指标及模型口径；目前只额外保存 b0_free 无法从参数 CSV 重建自由曲线。锚定改变拟合曲线，不能称为“只是把同一条曲线换一组参数”。NSS 的 3.33bp 不能用于背书锚定因子的 4.76bp 曲线，b1 更不能直接投入信号。
3. **ruff**：当前修复有效、真实执行通过。但静态检查不验证数值最优性、金融单位、原子事务或配置透传；还需要固定可工作的 Python 环境，并明确 lint 的规则与排除范围。

**定价与后续研究的证据边界**：

- A 券价格、应计、解析久期/凸性/DV01 及反解数值已通过现有回归。所谓 0.0026bp 是“净价取整为 100 后反解”相对 1.72% 的差，不应描述成求根算法的数值误差。
- “A–D 全部官方对账”表述过强：A/B 黄金数主要来自 memo 自算，D 的起息/结算日期是测试自行假设；`test_pricing.py` 没有纳入 memo §2.4 的财政部公告 99.98/1.73、100.71/1.59 配对案例。下一轮应增加独立外部报价夹具，并锁定估值日、结算日及舍入规则。
- 贴现应计 `(100-Pd)*t/T` 满足起息日为 0、到期为总折价，当前实现方向正确；`100-Pd/T*t` 是缺括号的文档错误，仍残留在方法论、市场惯例、规划和配置中。本轮尝试访问所引 2025/2026 官方 PDF 未成功，未把现行全部计息细则称为已独立复核。
- 双模型不能直接混用于收益归因；YTM 拟合也不等于已获得即期曲线。bootstrap 需显式指定合成票息/价格假设，`forward()` 当前仅是文档已声明的 YTM 近似，KRD 未实现应继续保持显式缺口。
- 当前 NSS 10Y 自由残差均值约 +4.933bp、MAE 5.382bp；残差均值回归策略之前要检查期限系统偏差与时间持续性，不能把成品估值曲线的再拟合误差直接视为可交易错价。

建议修复顺序：R01/R02（数值求解与因子口径）→ R03/R04/R05/R06（数据版本与更新）→ R07–R11（配置和接口）；完成独立反例回归后再进入 PCA、bootstrap 和策略回测。

### Lead 响应汇总（2026-11-06）

**11 条问题全部关闭**，每条都有"修复前失败、修复后通过"的回归测试；测试基线 202 → **254 项全绿**，
`make check` 在项目 `.venv` 下 exit 0。

| 编号 | 状态 | 关键证据 |
|------|------|----------|
| R01 | ✅ | 极端日 2002-04-26：156.04 → **94.02bp**；BVLS 与 scipy 相对误差 ≤2.4e-16 |
| R02 | ✅ | 2023-10-12 已标记 unstable；新增可观测因子；撤回 slope 结论 |
| R03 | ✅ | 源变更后非 force 重建：parquet/manifest/version_id 均不变 |
| R04 | ✅ | 5 个写入边界注错：指针不变、读者仍读完整旧版本 |
| R05 | ✅ | 删 1 个历史交易日即报错；`--allow-coverage-drop` 显式放行 |
| R06 | ✅ | 当年下载调用 0→1；历史年 0 次；落后 >5 天告警 |
| R07 | ✅ | 等权 3.3209 vs 配置权重 3.3643bp，均可复现，默认走配置 |
| R08 | ✅ | bp=0.5/2 分别为 0.09121737895 / 0.09121744067 |
| R09 | ✅ | `from_params(tau1)` 可用；缺参数抛错不再静默退化 |
| R10 | ✅ | `flatten(25) ≠ steepen(25)`；bp<0 抛错 |
| R11 | ✅ | 周五 T+2=11-10、T+3=11-11 |

**关于你指出的两点元问题，也一并处理**：

1. **"ruff 通过不能代替正确性测试"**：完全同意。已在 C-011/C-018 明确 `make check` 的三段构成
   （`ruff check` + `ruff format --check` + `pytest`），并在本文件写明 **lint 的规则与排除范围**：
   `scripts/spike` 为 Phase 0 一次性脚本、保持原貌以便回溯；`PD`（pandas）规则需 `preview=true`，暂未启用，
   因此不能说"pandas 专项规则全过"。静态检查不验证数值最优性、金融单位、事务原子性与配置透传——
   这些全部由 R01–R11 的数值/行为测试承担。
2. **环境**：`.venv` 已建成完整环境（pandas 2.3.3 / numpy 2.5.4 / scipy 1.18.1 / pytest 9.2.0 / ruff），
   `Makefile` 优先使用 `.venv/bin/python`，`make setup` 可从零重建；不再依赖 anaconda 的 pytest。

**证据边界（按你的意见修订）**：

- A–D 一律改称"**memo 自算算例**"；新增财政部公告**外部配对 E/F**（`tests/test_pricing_official.py`），
  并锁定日期、舍入规则与假设。E 与公告差 0.27bp；F 差 0.50bp，其起息日为反推假设、已标注。
- "0.0026bp"改述为"净价取整到分后反解与 1.72% 的差"，不再称为求根误差。
- 贴现应计确认 `(100−Pd)×t/T` 方向正确，docs 中 `100−Pd/T×t` 的缺括号笔误已统一修正。
- **不混用两个模型**：自由拟合（表达能力）与锚定拟合（稳定 level）作为两个产品分别落盘、分别验收，
  禁止用 3.32bp 背书锚定曲线的 4.76bp。
- `forward()` 仍是 YTM 近似、bootstrap 未做、KRD 保持显式缺口——这些没有被"修复"掩盖。
- NSS 10Y 自由残差均值约 +4.9bp，在把它当作"可交易错价"之前，必须先检验期限系统偏差的时间持续性。

---

**本轮建议优先评审的三处**（Lead 自评，供参考）：

1. ~~参数稳定化方案~~ → **评审结论：不可混用**。已改为两个独立产品（`free` / `anchored`），
   各自保存参数、拟合值、残差、指标，各自验收；不再出现"参数来自 A、残差来自 B"的解释困难。
2. ~~贴现分支公式~~ → **评审确认 `(100−Pd)×t/T` 方向正确**（起息 0、到期为总折价），
   `100−Pd/T×t` 是缺括号文档错误，docs 已统一修正。
3. ~~失败不覆盖策略~~ → **评审指出覆盖不全**。已升级为版本目录 + 原子指针的整批事务，
   并对 5 个写入边界逐一注入异常验证；部分年份成功/部分失败的路径也在 staging 阶段即被拦截。

---

## 八、附：变更记录的自检清单

每次追加记录前，Lead 必须逐条确认：

- [x] 变更内容与 `git diff` 一致，没有遗漏文件
- [x] 每个新增的公开函数都有中文 docstring 与类型注解
- [x] 新增参数已同步到 `config/config.yaml` 与 `src/ficc/config.py`
- [x] `make test` 全绿（**254**，`.venv` 环境）
- [x] `make lint` 通过（`ruff check` + `ruff format --check`）
- [x] 若改了数据契约，`docs/02_data_dictionary.md` 已同步
- [x] 若改了金融口径，`docs/03_methodology.md` 与官方算例测试已同步
- [x] 风险/遗留项已写入第六节
