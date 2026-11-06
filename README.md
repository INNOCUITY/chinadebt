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
make fit              # 曲线拟合 NS/NSS + PCA
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
  portfolio/     账本、仓位、约束
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
make check    # = ruff check + ruff format --check + pytest（254 项，全部离线）
```

- 所有命令优先使用 `.venv/bin/python`，不依赖 anaconda 或系统 Python。
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
- [x] Phase 1 定价（计息/全价/久期/DV01，官方算例 A–D 全部命中）
- [ ] Phase 1 剩余：因子层（PCA）、即期曲线 bootstrap、Carry 分解
- [ ] Phase 2 策略与回测
- [ ] Phase 3 风险
- [ ] Phase 4 机器学习（必做）
- [ ] Phase 5 强化学习（必做）
- [ ] Phase 6 展示与报告（中英双语 + Dashboard）
