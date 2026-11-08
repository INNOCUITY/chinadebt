# 中国国债收益率曲线量化交易与风险管理系统

> China Government Bond Yield Curve Trading & Risk System
> 面向总行资金中心 FICC 岗位的量化研究项目

一套以**中债国债收益率曲线**为数据基础，覆盖「曲线建模 → 相对价值策略 → DV01 中性组合 → 成本后回测 → 风险归因 → 交互演示」的端到端系统。

## 它回答什么问题

1. 利率债组合的收益，来自**久期 beta**、**Carry & 滚降**，还是**曲线形态择时**？
2. 扣除**交易成本与回购融资成本**后，还剩多少真 alpha？
3. 曲线**平移 / 陡平 / 蝶式** 25bp、或重演 2013 钱荒，组合亏多少？限额在哪？

## 数据边界（已实测，2026-11-06）

| 项 | 实测结果 |
|---|---|
| 主数据源 | 中债估值中心官网存档，`中债国债收益率曲线(到期)`，**2002-01-04 → 2026-11-05** |
| 规模 | 6,204 个交易日 / 114,779 行 / 25 个年度文件全部成功 / 收益率零缺失 |
| 交叉验证 | 与 AKShare 中债接口、东方财富 10Y 逐日比对，**最大绝对误差 0.0000** |
| 已知缺口 | 2Y 在 2016-01-04~2019-01-25 缺失 772 天 → **回测起点定 2019-01-28** |
| 不可得 | 个券历史估值（净价/久期）→ 走**合成债券**路线；真 DR007 → 用 FDR007 代理 |

> ⚠️ **数据许可**：中债数据版权归中央国债登记结算有限责任公司（"未经允许 请勿转载"）。
> 本仓库**不包含原始数据**，`data/` 已全部 `.gitignore`；克隆后运行 `make fetch` 自行下载。
> 仓库只提交抓取脚本、数据字典、汇总统计与图表。

## 文档索引

| 文档 | 内容 |
|------|------|
| [docs/01_project_plan.md](docs/01_project_plan.md) | **总体规划**：框架、代码结构、六周推进计划、验收标准 |
| [docs/00_data_feasibility.md](docs/00_data_feasibility.md) | 数据可得性实测结论（数据源/历史长度/许可） |
| [docs/00_market_conventions.md](docs/00_market_conventions.md) | 中债估值口径与国债交易实务惯例 |
| [docs/02_data_dictionary.md](docs/02_data_dictionary.md) | 字段字典 |
| [docs/03_methodology.md](docs/03_methodology.md) | **方法论与公式推导**（NS/NSS、定价、久期/DV01、归因、ML/RL 纪律） |
| [docs/code.md](docs/code.md) | **代码变更日志 + Code Review 区**（每次改动必记录） |

## 快速开始

```bash
make install          # 安装依赖（-e .）
make fetch            # 抓取数据 → data/raw/ + 快照 manifest
make fit              # 当前曲线拟合与残差摘要
make diagnostics      # 自由/锚定双产品，为 bias 提供残差面板
make backtest         # 基线策略回测（含成本）
make risk             # 风险报告：DV01/KRD/VaR/压力测试
make serve            # Streamlit 演示界面
make test             # 单元测试
```

## 目录结构（概览）

```
config/          全局与实验参数（所有金融口径集中在此）
data/            raw（只读快照）/ interim / processed / metadata（sha256 manifest）
src/ficc/
  data/          数据源适配、清洗、质量校验、契约 schema
  curve/         曲线拟合、插值、因子、情景冲击
  pricing/       定价、应计利息、久期、DV01、KRD、Carry & 滚降
  strategy/      策略：陡平 / 相对价值 / Carry & Roll-down
  portfolio/     固定券账本（ledger）+ 独立对账（reconcile）+ 仓位/约束
  backtest/      回测引擎、成交与成本、绩效、归因
  risk/          敞口、VaR/ES、压力测试、限额
  ml/            机器学习：标签/特征/模型/Purged Walk-Forward/可解释性
  rl/            强化学习：gymnasium 环境、奖励、PPO/SAC 训练、消融
  report/        图表与报告导出
apps/dashboard/  Streamlit 演示（曲线 / 策略 / 风险 / 回测）
tests/           单元测试（重点：定价口径、无未来信息泄漏）
reports/         最终研究报告（zh/ 与 en/）与图表
docs/code.md     代码变更日志 + Code Review
```

## 工程原则

- **可复现**：数据快照 sha256 + 配置 hash + git sha → `run_id`，同 id 必同结果。
- **无未来信息**：信号只读 `as_of <= t` 的数据，显式 T+1 成交，并有对抗性测试。
- **全价口径**：PnL = 全价变动 + 票息 − 融资成本 − 交易成本，绝不使用净价。
- **必须归因**：收益拆到 `carry / roll-down / 平移 / 形态 / 残差`，拆不出来就不下结论。
- **诚实**：结论允许是"没有稳健超额收益"。

## 环境与复现

```bash
make setup    # 创建项目本地 .venv 并安装全部依赖（推荐）
make check    # = ruff check + ruff format --check + pytest（663 项离线，exit 0）
```

- 所有命令优先使用 `.venv/bin/python`，但当前 .venv 仍继承 Anaconda 的 system-site-packages，完整隔离环境尚待重建。
- 2026-11-07 复核后修复：默认 `make check` 现为 **exit 0（663 passed / 1 deselected）**。修复方式是在 `pyproject.toml` 的 addopts 中同时禁用 `debugging` 与 `capture` 插件（本环境 pytest capture 会触发段错误）；**根因尚未定位**，只报告"命令稳定通过"，不把它当作环境问题已解决的证明。
- Python 环境、lint 规则与排除范围（`scripts/spike` 保持 Phase 0 原貌；`PD` 规则需 preview 暂未启用）见 [docs/code.md](docs/code.md)。
- 仓库为**私有**状态；`data/` 全部不入库（中债数据不可再分发），克隆后 `make data` 自行重建。

## 开发规范

- **每次代码变更都必须在 `docs/code.md` 追加一条记录**（编号、变更内容、原因、影响面、验证命令、风险遗留）。
- 评审意见写在 `docs/code.md` 的「Code Review 区」，按模板填写即可。
- 提交前必须通过：`make test`（离线全绿）+ `make lint`。
- ML/RL 是**必做项**（目标团队为智能交易团队），必须有样本外结果与消融实验。

## 状态

- [x] 项目规划与架构设计（v0.3，ML/RL 必做、中英双语、Dashboard 后置）
- [x] Phase 0 数据可行性实测（中债曲线 2002–2026，6204 交易日，25/25 文件成功）
- [x] Phase 0 金融口径确认（中债官方口径 + 交易实务，含自算校验）
- [x] Phase 0 数据契约 / 抓取管道生产化 / 2Y 回补（772/772 已验证）
- [x] Phase 1 定价（计息/全价/久期/DV01 + 财政部公告外部对账）
- [x] Phase 1 即期曲线 bootstrap（合成假设显式声明，往返误差 2.84e-14）
- [x] Phase 1 KRD 数值算子（即期曲线单点冲击；结算日契约 R17 已修复，sum(KRD) vs DV01 差 0.12%–1.35%）
- [x] Phase 1 因子接口（level_all/level_long/slope/curvature 固定契约；同口径 winsor PCA；验收映射显式化）
- [x] Phase 1 Carry/Roll-down（弦长口径 R18 已修 + 原券老化重估；账本级独立 PnL 仍待接入）
- [x] Phase 2 第 1 步 固定券账本（R24 收息权、R25 融资前向填充已修复）
- [x] Phase 2 第 2 步 独立对账（6 项执行 + 1 项归因显式 not run；含票息权益独立校验）
- [x] Phase 2 第 3 步 组合约束（有界主动风险 9 项 + 成交前完整闸门；统一基准与日期契约）
- [x] Phase 2 第 4 步 三个基线策略与记录（含价差成本；零 tilt 控制组 +0.37bp 验收通过）
- [ ] Phase 2 第 1–5 步金融验收：原 R23 零现金反例已修，第八轮复核 R31–R36 仍待处理（计划 §12.12）
- [x] Phase 2 第 5 步归因模块交付（桥接可复现；对偶基、独立估值及到期分类待验收）
- [ ] Phase 3 风险
- [ ] Phase 4 机器学习（必做）
- [ ] Phase 5 强化学习（必做）
- [ ] Phase 6 展示与报告（中英双语 + Dashboard）

2026-11-07 第二轮评审（R12–R21）开发修复记录（新组合调用方与本机环境的当前状态见 R27/R30）：

- 当轮报告 `make check` → exit 0（366 项离线测试），配置禁用了 `debugging`；本轮默认入口复核失败，不能沿用此环境结论。
- `make pipeline` 六 stage **在空报告目录可端到端跑通**：拟合产品由 `build_fit_products` 当次生成并写 meta 指纹，`bias` 只消费指纹匹配的残差。
- `--config / --out-dir / --start / --end` 全链路透传，隔离运行不写默认目录。
- 更正后的数字：260010 的 10Y KRD 桶占 **92.84%**（原 98% 为算术错误）；各 KRD 与 YTM DV01 相对差 **0.12%–1.35%**；可疑清单 **289 条记录 / 229 个唯一点 / 109 个日期**，全部带 `known_at`；carry 横截面 87 个采样日（2019-01-28 → 2025-12-18）。
- 研究结论边界未放宽：10Y 残差只能称「持续的**模型相对**残差」，经济解释与交易性均标记 untested；2024–2025 已参与方法选择，不再称"未触碰测试集"。
- 详细证据见 [code.md R12–R21](docs/code.md)、[方法论 §4.7/§4.8/§7.3/§8.3](docs/03_methodology.md)、[计划 §12.6](docs/01_project_plan.md)。

**Phase 2 第十一轮（2026-11-08，R37–R41 修复）**：执行/滚动契约与风险口径已重做，验收范围仍分开报告。

- **R37 闸门**：投影路径逐时点检查（成交日、各结算日、最晚结算后检查点），不再只看最后快照；T+2 + cash_floor=0 的建仓在成交前被拒；rv 长样本 2023-07-04 的主动单券超限消失（0 违规）。
- **R38 滚动**：滚动日策略机械同日换券（卖光旧券 + 该腿滚动到基准新券面值，双方共用政策）；旧 pending 实际被**取消**（无延期队列），由当日收盘新信号替代；`roll_events.pending_status=cancelled_by_roll` 与 `gate_decisions` 记录真实决策（R45）。`roll_events` 记录事件→成交、状态与拒单原因；新增 `TargetFaces` 精确基准回放控制组，长样本超额 **0.0000bp**，四组均 7/7 同日、0 缺失。
- **R39 VaR**：改为冻结分析日组合 + 历史冲击（`analysis_as_of + frozen_positions + historical_shocks`）；95% VaR/ES **32.42/44.58 万元**（旧 aging 口径 35.67/49.67 万并列；审查的 YTM 冻结反例 31.82/43.33 万在测试中逐位复现）。
- **R40 风险近似**：全重估与 KRD 统一 `price_from_spot` + `spot_shock`；小冲击收敛 8.3e-4 / 4.3e-6 / 4.3e-7；平行 ±100bp 8.2%/8.3%、陡平蝶 25bp ≈1.08%、历史 ~1%（旧"蝶式 16.4%"是不同源误差，已纠正）。
- **R41 报告**：窗口契约改为 3×约 2 年 + 229 日尾窗；收益基期=前收 NAV；`pooled_cumulative_excess_bp`（全段 −120.58bp）与 `pooled_ann_excess_bp`（−13.37bp）、`mean_window_excess_bp`（−25.01bp）明确分列；carry 年化 **3.7358%**、MDD **−4.9915%**；`rolling_version` 填值；文档数字与 CSV 自动核对。
- **验收**：默认 buyhold `make acceptance` **71/71 PASS**；滚动 `make acceptance-rolling` **92/92 PASS**（2019–2021），延到 2025-12-31 同为 **92/92 PASS**（含对账逐项、跨换券归因、票息权益、现金对账、未来扰动、风险小冲击收敛）；滚动因果性/同日换券/拒单审计均有测试。两者范围不同，不能互相替代。
- **边界**：主 VaR 用即期空间（与审查 YTM 冻结空间差 +1.9%/+2.9%，已并列）；price-only VaR 不含持有期 carry/现金事件；滚动基准为面值保持政策；重叠窗口不做推断。ML/RL 仅可先做 as-of/split 接口与零信号基线，绩效比较待 rolling/风险验收固定后。

**第十二轮（R42–R46）**：多日 VaR 改为"先聚合 h 日冲击、carry 只计一次"（平 3% h=5 carry 58,967.64/样本，旧 5× 已消除；3%→4%→3% 净冲击 PnL=0）；ML 增加逐决策时点训练矩阵与逐行因果性校验（`shift(-1)` 反例 2019-02-11 行不变）；RL 零动作改为 **hold（不主动成交）**，`projected_target` 取引擎 `gate_decisions` 真实决策，pending 取消语义显式化；forced 缩放按最终整数面值重算价差成本。验收：buyhold 71/71、rolling 92/92（2019–2021 与 2025 延长样本同口径），carry 2022-03-21 被动超限单独披露。

**监督学习数据集与线性基线（2026-11-08）**：信息时钟 lag=1 / next-close / h=5 / label_lag=1；账本口径标签（同引擎一次回放 benchmark_nav，成本/票息/融资/滚动已入）；逐决策时点特征 + 逐行因果性；线性 Ridge vs 零基线 22 split、n=277：OOS IC **0.020**、R² **−0.120**（零 −0.056）、MAE 2.09e-3（零 2.00e-3）——**未跑赢零基线**，负结果保留，不构成 alpha 证据。产物 `reports/ml_dataset*.csv/json`、`reports/ml_baseline_metrics.csv`；`make ml-baseline`。

**RL 动作契约（2026-11-08）**：`ActionSpec` 显式 Hold/TargetKRD/TargetFaces；`StepRecord` 记录 action_outcome/substitution/policy_actions；`terminate()` 返回未结算义务（默认不强制平仓）；`rollout` 单次引擎 O(n) 训练路径，与 step O(n²) 逐位一致。

**C 阶段接口（仅接口，未做绩效比较）**：`src/ficc/ml/`（`build_current_features` 当前决策工作表；`build_training_matrix` 逐决策时点构造；`assert_per_row_causality` 逐行因果性；`available_after` 标签；walk-forward purge/embargo；`researched_sample` 标注）；`src/ficc/rl/`（`RebalanceEnv` 复用同一引擎/账本/闸门，记录原动作→提交目标→实际成交/拒单、gate_level、reward=账本 NAV 差）。已知限制：env 每步重放 O(n²)、闸门内部重试档位未暴露（仅有 gate_attempts）、ML 标签为曲线一阶代理；绩效比较等 rolling/风险验收固定后再做。

**第十二轮独立审查（2026-11-08）**：默认 check、62 项 buyhold 和 79 项 rolling 验收实跑通过，
一日风险 CSV 复现；79 项默认范围是 2019-01-28→2021-12-31、2 次换券。
同一入口延长至 2025-12-31 实跑 **78/79 PASS、exit 1**，唯一失败为 carry 已披露的 1 天被动超限；
其余全部通过，保留 FAIL，不将七年报告改称全绿。
新发现多日 VaR 聚合/carry 重计（R42）、历史特征行未保证逐行因果性（R43）、RL 零动作会主动交易（R44）、
滚动动作替代审计失真（R45）和缩放取整后的价差报告错误（R46）。这些问题不推翻已通过的默认一日结果，
但训练前置不止“暴露重试档位 + 替换标签”；先验收样本和动作语义。
详见[方法论 §10.21](docs/03_methodology.md)、[推进计划 §12.17](docs/01_project_plan.md)、[审查记录 R42–R46](docs/code.md)。本轮只更新文档。

**历史轮次（R31–R36）**：执行状态、对偶因子基、独立估值、到期退出与统一验收——见方法论 §10.7–10.13 与 code.md C-071~C-079。
