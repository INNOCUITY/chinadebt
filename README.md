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
make check    # = ruff check + ruff format --check + pytest（551 项离线，exit 0）
```

- 所有命令优先使用 `.venv/bin/python`，但当前 .venv 仍继承 Anaconda 的 system-site-packages，完整隔离环境尚待重建。
- 2026-11-07 复核后修复：默认 `make check` 现为 **exit 0（551 passed / 1 deselected）**。修复方式是在 `pyproject.toml` 的 addopts 中同时禁用 `debugging` 与 `capture` 插件（本环境 pytest capture 会触发段错误）；**根因尚未定位**，只报告"命令稳定通过"，不把它当作环境问题已解决的证明。
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

**Phase 2 第九轮（2026-11-08，R31–R36 修复）**：执行状态、经济归因与统一验收已打通，仍不宣称金融验收全部完成。

- **R31 熔断**：`risk_off` 真正传入闸门；熔断按 `[1.0..0.0]×基准` 阶梯降档并记录 `gate_level`；降风险偏序只看可行动项（历史回撤允许仍在）；熔断时按最小整手交易。回撤场景实测 gate_level=0.8、4 笔卖出、DV01 0.85×→0.80×。
- **R32 现金预算**：`Ledger.clone/simulate` 统一状态推进（今日结算/票息/费用 + 候选成交 + 长假逐日融资），资金利率截断到 ≤as_of；闸门不再手写"一天融资"近似。审查反例（平 3%、5% 融资、跨春节）复跑：现金最低 −8,020,926.79 元、下限余量 +275.58 元，**无 cash_floor 违规**。
- **R33 因子基**：观测矩阵 F × 形状基 B=F⁻¹，断言 `F@B=I`；2/5/7/10/30Y 不同剩余期限的纯因子场景 other≈0。
- **R34 独立估值**：从条款+观察日+曲线独立重估 `dirty_model`，`valuation_basis` 单列并设阈值；真实 480 天 basis=0、breach=0；快照价格/市值/NAV 同步 +10,000 元的反例被捕获，不再进入 other_curve。
- **R35 到期退出**：到期按赎回价值 100 + 末期票息现金处理，桥接扣除本金赎回；平曲线到期市场曲线分项=0。
- **统一验收（R36/§12.12）**：`make acceptance` 在同一入口实跑策略/基准/归因/独立估值/对账 7 项（归因不再是 not run）/约束/零 tilt 控制；全量 **33 项 PASS / 0 FAIL**，缺检查即 FAIL。
- **成本对照报告更正**：按 CSV 修正费用/融资归零的超额变化（RV 费用归零 +4.14bp、融资归零 +2.99bp；carry −2.63/−5.38；2s10s −0.76/−1.75）；2s10s 全部成本归零后重跑与固定路径相差 1,612.64 元、去价差 77 vs 71 笔、换手 +3.32%，不再写"≤2 元/<0.4%"；固定路径与重跑的手续费计价基数已分别说明。
- **结论边界**：去价差使 carry/2s10s 历史超额由负转正、RV 超额上升 → 价差对该窗口影响大；但成本前历史超额为正**不等于**存在稳定预测信号；lot/死区能明显改变符号，需检查实际主动风险暴露，不能只看收益符号。因子 `other_curve` 仍约 −21bp（2 年），是三因子未覆盖期限变化的真实量，与 carry/roll、估值基差分开报告。
- 详见 [方法论 §10.7–10.13](docs/03_methodology.md)、[计划 §12.12–12.13](docs/01_project_plan.md)、[code.md C-071~C-079](docs/code.md)。
