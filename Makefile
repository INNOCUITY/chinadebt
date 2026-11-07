.PHONY: help setup install recon strategies cost-study attribution data fetch quality fit diagnostics pipeline backtest risk sensitivity report serve test lint fmt check repro clean

# 优先使用项目本地 .venv（含 pandas/scipy/pytest/ruff），保证在任何 shell 下 `make check` 都能复现；
# 没有 .venv 时回退系统 python3（此时请先 `make setup`）。
PY ?= $(shell [ -x .venv/bin/python ] && echo .venv/bin/python || echo python3)
export PYTHONPATH := src
# ruff 装在项目本地 .venv（不污染 anaconda）；缺失时回退到 python3 -m ruff
RUFF := $(shell [ -x .venv/bin/ruff ] && echo .venv/bin/ruff || echo "$(PY) -m ruff")

help:  ## 显示所有可用命令
	@grep -E '^[a-zA-Z_-]+:.*?## .*$$' $(MAKEFILE_LIST) | awk 'BEGIN {FS = ":.*?## "}; {printf "  \033[36m%-14s\033[0m %s\n", $$1, $$2}'

setup:  ## 创建项目本地 .venv 并安装全部开发依赖（推荐先跑这个）
	python3 -m venv .venv
	.venv/bin/pip install -U pip
	.venv/bin/pip install -e ".[dashboard,dev]"

install:  ## 安装为可编辑包（含 dashboard 与 dev 依赖）
	$(PY) -m pip install -e ".[dashboard,dev]"

# ------------------------- 数据 -------------------------
data:  ## 离线重建数据（用本地已缓存的官方 xlsx，不联网）
	$(PY) scripts/fetch_data.py --offline

fetch:  ## 在线抓取数据并生成快照 + manifest
	$(PY) scripts/fetch_data.py

quality:  ## 数据质量报告（缺日/跳变/重叠/期限覆盖 + 可疑跳变清单）
	$(PY) -m ficc.data.quality --config config/config.yaml --suspect-report

backfill:  ## 回补 2Y 期限点（2016-01-04~2019-01-25，可选）
	$(PY) scripts/backfill_2y.py --throttle 0.3

# ------------------------- 账本与对账（Phase 2）-------------------------
recon:  ## 固定券账本 + 独立对账（真实曲线/融资/CN_IB 日历；失败 exit 1）
	$(PY) scripts/run_ledger_recon.py

strategies:  ## 三个基线策略回测（含成本/换手/暴露/约束/逐券快照）
	$(PY) scripts/run_strategies.py

cost-study:  ## 成本对照实验（固定路径回放 + 决策敏感性 + 参数敏感性）
	$(PY) scripts/run_cost_study.py --quick

attribution:  ## 账本归因（固定持仓：桥接 + carry/roll + 因子拆分）
	$(PY) scripts/run_attribution.py

# ------------------------- 研究 -------------------------
fit:  ## 曲线拟合 + 残差诊断（最小链路：热力图 + NSS 残差）
	$(PY) scripts/run_pipeline.py --stage curve

diagnostics:  ## 曲线层深度诊断（参数时序/分期限残差/失败日清单）
	$(PY) scripts/fit_diagnostics.py

pipeline:  ## 全流程：数据 → 曲线 →（后续阶段）
	$(PY) scripts/run_pipeline.py --all

backtest:  ## 跑基线回测
	$(PY) scripts/run_backtest.py --config config/config.yaml

risk:  ## 生成风险报告（敞口/VaR/压力测试）
	$(PY) scripts/run_pipeline.py --stage risk

sensitivity:  ## 参数敏感性网格
	$(PY) scripts/run_sensitivity.py --config config/config.yaml

report:  ## 生成研究报告与图表
	$(PY) scripts/build_report.py

serve:  ## 启动 Streamlit 演示
	$(PY) -m streamlit run apps/dashboard/app.py

# ------------------------- 质量 -------------------------
test:  ## 单元测试（离线，必须全绿）
	$(PY) -m pytest tests/ -q

test-network:  ## 含联网测试（抓取相关）
	$(PY) -m pytest tests/ -q -m network

cov:  ## 覆盖率报告
	$(PY) -m pytest tests/ -q --cov=src/ficc --cov-report=term-missing

lint:  ## 代码检查
	$(RUFF) check src tests scripts
	$(RUFF) format --check src tests scripts

fmt:  ## 自动格式化
	$(RUFF) format src tests scripts
	$(RUFF) check --fix src tests scripts

check: lint test  ## 提交前的完整检查

repro:  ## 一键全流程复现（数据 → 报告）
	$(PY) scripts/run_pipeline.py --all

clean:  ## 清理缓存与中间产物
	find . -type d -name __pycache__ -exec rm -rf {} + 2>/dev/null || true
	rm -rf .pytest_cache .ruff_cache .mypy_cache build dist *.egg-info
