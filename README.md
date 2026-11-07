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
make check    # = ruff check + ruff format --check + pytest（540 项离线，exit 0）
```

- 所有命令优先使用 `.venv/bin/python`，但当前 .venv 仍继承 Anaconda 的 system-site-packages，完整隔离环境尚待重建。
- 2026-11-07 复核后修复：默认 `make check` 现为 **exit 0（540 passed / 1 deselected）**。修复方式是在 `pyproject.toml` 的 addopts 中同时禁用 `debugging` 与 `capture` 插件（本环境 pytest capture 会触发段错误）；**根因尚未定位**，只报告"命令稳定通过"，不把它当作环境问题已解决的证明。
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
- [ ] Phase 2 第 1–4 步金融验收：修复已交付并重跑，R23 现金预算补查仍待修；归因/滚动实验待完成（计划 §12.10）
- [ ] Phase 2 第 5 步 归因接入账本持仓（当前对账已留闭合接口）
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

**Phase 2 第 1–5 步 + 执行边界 + 对照实验（2026-11-07，第八轮）**

- **执行边界已关闭**：现金预算按**结算时点**汇总（今日 pending/费用 + 新成交 T+1 + 明日票息 + 负现金一天融资），费用/票息进入 NAV 分母；审查反例（`min_cash_fraction=0`）4 次建仓全部成交前拒单、0 违规。回撤熔断按风险阶梯降档，并允许"只降不增"的降风险订单执行（`gate_mode=partial_de_risk`）。
- **因果性**：扰动 CUTOFF 之后的曲线/融资/RV 残差缓存，截止 CUTOFF 的净值/成交/信号/敞口前缀逐位不变。
- **归因（Phase 2 第 5 步）**：`src/ficc/attribution/` 实现账本独立重估——精确桥接（价格/票息/本金/成交/融资/费用/未结算/交易日内平衡项）+ 逐券 carry/roll + level/slope/curvature/other 顺序重估；480 天桥接残差 ≤3.6e-8 元（相对 NAV ~3e-16），四因子和恒等于 curve_pnl（≤9.1e-13）；篡改 coupon +1 元 → 残差 −1.000000；**other_curve 显式披露（基准 −23.7 万 / −20.8bp），不用残差补平**。价差已在执行价，归因不重复扣。
- **成本对照**：固定路径回放 == 引擎（max|ΔNAV|=0）；2 年价差 7.1–15.6bp（占成本 70–90%）、费用 0.3–2.5bp、融资 0–1.5bp；成本全部置零后超额变为 carry +6.1bp / 2s10s +5.4bp / rv +12.9bp → **信号在成本前是正的，价差抵消了收益**；`rv` 含成本仍为正（+1.74bp），不能与其他两个策略统一定性为"成本后失败"。
- **参数敏感性（结论不稳）**：lot 5M 时 2s10s 超额 +33.5bp、carry −8.0bp；死区=0 抖单成本爆炸（carry −58.4bp）；weekly 下 carry −26.3bp、rv −3.6bp —— **单点配置结论不能外推**。
- **措辞更正**：累计超额与年化 TE 的大小比较不是显著性检验；零 tilt 的 +0.37bp 是一次控制路径偏差（整手/死区/次日执行），不是统计噪声分布。更准确的结论：**当前配置与历史窗口内尚未建立稳健超额证据；价差是主要成本项；是否由成本抵消信号收益取决于执行参数，需多窗口/区间估计验证**。
- 详见 [方法论 §10.7–10.12](docs/03_methodology.md)、[计划 §12.10–12.11](docs/01_project_plan.md)、[code.md C-063~C-069](docs/code.md)。
