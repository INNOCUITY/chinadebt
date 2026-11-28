# 复现入口与边界 / Reproduction

## 公开交付复现（无原始数据、无训练）

推荐干净 Python 3.11 环境。项目原始数据和模型权重不入 Git。

```bash
python3.11 -m venv .venv
.venv/bin/python -m pip install -e '.[dev,dashboard]'
make reproduce
make delivery-check
make serve
```

`make reproduce` 从已提交的研究摘要重建 `reports/unified_baselines.csv`、`rl_ablation.csv`、`delivery_facts.json`、`acceptance_status.json`、中英双语报告及两张 Python 图。它不生成新策略结果、不下载行情、不跑 RL。所有输入散列及文本输出散列见 `reports/delivery_manifest.json`；PNG 会因渲染环境有字节差异，因此检查数值来源和图存在性，不跨平台声称像素完全一致。

Dashboard 位于 `apps/dashboard/app.py`，四页读取已落盘结果，明确日期、样本期间、执行带和未完成项目。它不支持界面临时计算任意日期的账本；新日期须先离线回放。这是原计划“任意日期可复现”的剩余差距。

## CI 与测试

```bash
# Linux CI 使用同一 Torch 版本的 CPU wheel
.venv/bin/python -m pip install torch==2.9.1 --index-url https://download.pytorch.org/whl/cpu
make test-public
make lint
```

macOS 使用平台适配的 `torch==2.9.1` wheel，不使用 Linux CPU 索引。`scripts/run_public_tests.py` 要求 Torch 已安装，避免 PPO 测试静默跳过；禁用可选 readline 仅为兼容本地 Anaconda 的扩展冲突。

`.github/workflows/ci.yml` 对 main push/PR 执行 pytest、ruff、交付漂移检查和公开报告重建。选择清单在 `config/acceptance/public_tests.txt`，包括定价、风险约束、构仓、ML 时序、防泄漏、PPO GAE/终止费用、新数据及四页展示测试。**这是公开契约测试，不是全部真实数据集成测试。** 真实数据环境下另用 `make check` 和三套 `make acceptance*`。本轮不重复执行已经通过的全实验，旧 71/92/92 项验收作为归档证据。

## 新增期货信息检验

```bash
make futures-data
PYTHONPATH=src:scripts .venv/bin/python scripts/probe_futures_signal.py \
  --archive /path/to/paired-action-label-archive
```

第一步下载官方 201812—202505 月度包并核对独立冻结日期，保存在忽略目录 `datasets/cffex-eod-v1`。固定的 `contracts.parquet`、`features.parquet`、原现券市场、拟合/留出标签都有 SHA256 检查，任何快照变化均失败，不允许自动重写冻结散列。Parquet 字节可能随库版本不同；完整复算应使用原快照与相应环境，不将重新编码误判为同一归档。参考环境清单：`config/baselines/server-v2.freeze.txt` 与 `ppo-cu126.freeze.txt`。

预测检验另需原现券/资金快照及 `fold-{0,10,21}/fit_labels.json`，具体散列在 `config/experiments/futures_signal_probe.json`。旧留出报告为 `reports/action_value_holdout.json`。这些标签缓存和价格快照不是公开 Git 的一部分，不能声称仅 clone 即重训复算。原标签生成入口与配置为 `scripts/run_action_value_probe.py`、`config/experiments/action_value_probe.json`；训练窗口标签、配套留出报告须同一血缘，已有第 21 折浮点归档差异不可混用。

本次原始官方数据与旧源交易日期一致，但历史发布时间与修订版本无完整档案。次日零点只是时点假设，额外一交易日延迟为敏感性，不作精确历史发布时间保证。原始数据不随公开交付重新分发。

## 完整研究复现的前置条件

完整复现需要三类独立输入：冻结现券曲线/资金数据及交易日历；训练配置和配对标签；checkpoint、模型标准化参数和采样轨迹。分别依 `docs/04_server_guide.md`、`docs/05_server_execution_20261126.md`、`docs/07_ppo_training_design.md` 核验环境和输入散列。`make repro` 保留旧曲线流水线入口，已改正说明，不冒充从原数据到所有研究结论的一键命令。

当前交付既不新增模型容量/轮数，也不启动 SAC 或风险惩罚训练。未达到研究门槛，前瞻数据仍不使用。前瞻第一个合格决策日为 2026-12-07，但完整 12 区间仍需独立日历覆盖；不得由新行情本身推定应有日期。

## English quick guide

`make reproduce` rebuilds public evidence summaries, bilingual reports and Python figures. It does **not** reproduce private-data research, retrain PPO, or rerun financial acceptances. `make delivery-check` verifies source hashes and deterministic text outputs. `make serve` shows four read-only pages. CI runs the explicit public contract suite, including Torch CPU tests and Streamlit page tests. Full integration requires the archived licensed market snapshots, exact label lineage and checkpoints; missing or mismatched inputs fail closed. No credentials, server endpoints or raw data belong in Git.

## 成本/预测诊断复核边界

`make reproduce` 另生成 `reports/delivery_cost_signal.png`，从公开的 `reports/cost_signal_review.json` 绘图，不重新拟合。该报告含逐区间派生预测/价值和原记录散列，不包含市场输入或原始标签文件。完整复算需要忽略目录中的原始 181 条区间记录及原拟合归档。

原始 `run_cost_signal_diagnostic.py summary` 会核验运行代码指纹，必须在冻结源码 `125a3f7`、原配套数据/拟合标签的隔离目录执行；不能在随后添加报告代码的工作区冒充旧版本。先保留下载的服务器 summary，再保存冻结代码本地重算的 summary。之后可在当前版本执行：

```bash
python scripts/review_cost_signal.py --root <区间记录目录> \
  --server-summary <服务器原始汇总> --local-summary <冻结版本本地汇总>
```

该命令检查源身份、成本闭合、区间边界，并用独立增广最小二乘复算预测、动作和门槛。它不重跑市场账本，也不接触前瞻行情。资金公告已另行完成完整审计及一项冻结跨年预测；与原成本标签是不同任务，不能混报成绩。

资金公告审计、冻结预测与数据边界见 [专项复现记录](11_funding_data_feasibility.md)。四张 Python 汇总图均可由 `make reproduce` 离线重建；原始公告、行情和逐行预测仍不进入公开仓库。


## 资金合同、余额与预期审计

新增 `scripts/fetch_funding_contracts.py` 与 `scripts/audit_funding_contracts.py`，前者限定历史公告范围，后者完全离线核对目录、正文、参考月、计划/结果、发布时间及独立日历。规则与汇总分别在 `config/baselines/funding_contract_audit.json`、`reports/funding_contract_coverage.json`；来源版本改变会失败，不自动更新冻结散列。复算还需忽略目录内的普通公告解析快照与本地资金序列，公开 CI 使用合成边界测试。[字段、范围与限制](12_funding_contract_audit.md)。此入口仅审计数据，没有新预测目标/模型实验，也不启动训练；双语报告及 Dashboard 已同步。
