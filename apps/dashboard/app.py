"""Four read-only views of committed evidence. No market loader or strategy replay."""

import json
from pathlib import Path

import pandas as pd
import plotly.express as px
import streamlit as st

ROOT = Path(__file__).resolve().parents[2]
REPORTS = ROOT / "reports"


def read(name):
    return json.loads((REPORTS / name).read_text())


def main():
    st.set_page_config(page_title="国债研究证据 | Bond research", layout="wide")
    st.title("国债研究证据 / Bond research evidence")
    st.caption("2026-11-28 交付候选 · 只读归档结果 · No robust post-cost alpha established")
    page = st.sidebar.radio(
        "页面 / Page",
        ["曲线与数据 / Data", "基线与回测 / Baselines", "RL 与消融 / RL", "风险与验收 / Risk"],
    )
    st.sidebar.info("不执行回测或训练。新时期保留；原计划仍有未完成项。")
    facts = read("delivery_facts.json")
    if page.startswith("曲线"):
        st.subheader("曲线覆盖与新增期货信息")
        st.dataframe(pd.read_csv(REPORTS / "curve_summary.csv"), use_container_width=True)
        st.caption("曲线为历史覆盖摘要，不是实时行情。期货特征仅使用 2025-05 及以前的归档。")
        st.dataframe(pd.read_csv(REPORTS / "curve_fit_summary.csv"), use_container_width=True)
        st.json(facts["futures_coverage"])
        st.warning(
            "近月–次近月结算价差不是买卖价差；没有历史盘口、CTD 基差或逐日保证金的完整时点证据。"
        )
        st.image(str(REPORTS / "delivery_futures.png"))
        st.subheader("资金操作公告：数据通过，预测增量未过门")
        st.json(facts["funding_audit"])
        st.caption("特殊现金开市日为 null；未核实起息/到期日，不构造净投放。2026 年前瞻期未使用。")
        st.image(str(REPORTS / "delivery_funding.png"))
        st.subheader("合同与余额：部分信息可用，尚未启动新模型")
        st.json(facts["funding_contracts"])
        st.caption(
            "MLF 月份余额不是每日存量；定存约定日期不是现金划转确认；FDR 不是全天加权 DR。先核对资金风险目标，不恢复 PPO。"
        )
    elif page.startswith("基线"):
        st.subheader("同期间、同执行带、同账本")
        df = pd.read_csv(REPORTS / "unified_baselines.csv")
        band = st.selectbox("执行带 / Lot band", [2, 1])
        selected = df.loc[df.band_lot.eq(band)].copy()
        st.caption(
            f"{facts['test_window'][0]} – {facts['test_window'][1]} · 累计成本后超额（bp），非年化。PPO 为逐 seed。"
        )
        selected["label"] = selected.apply(
            lambda r: f"PPO / {int(r.seed)}" if pd.notna(r.seed) else r.model, axis=1
        )
        st.plotly_chart(
            px.bar(
                selected,
                x="label",
                y="excess_bp",
                color="model",
                labels={"excess_bp": "Net excess vs zero tilt (bp)", "label": "Strategy / seed"},
            ),
            use_container_width=True,
        )
        st.dataframe(selected, use_container_width=True)
        st.download_button(
            "下载统一基线 CSV",
            (REPORTS / "unified_baselines.csv").read_bytes(),
            "unified_baselines.csv",
            "text/csv",
        )
        with st.expander("独立历史规则样本：不得与上图直接排名"):
            st.dataframe(pd.read_csv(REPORTS / "long_sample_summary.csv"), use_container_width=True)
    elif page.startswith("RL"):
        st.subheader("奖励、动作、价值估计与执行结果")
        cost = facts["cost_signal"]
        st.error(
            f"最新成本/预测诊断：{cost['restored_prediction_wins']}/3 折降低预测误差，两个冻结门槛均失败。停止当前特征/动作组合。"
        )
        st.image(str(REPORTS / "delivery_cost_signal.png"))
        st.caption(
            "单区间重置标签均值，非累计收益。固定对照按拟合标签均值选档；成本加回不等于零成本重训。"
        )
        st.warning("λ=1 平台 3/6；两档相对 λ=0.95 的中位改善均为零。未过扩折门槛。")
        st.dataframe(pd.read_csv(REPORTS / "rl_ablation.csv"), use_container_width=True)
        figure = st.selectbox(
            "已归档诊断 / Archived diagnostic",
            ["ppo_lambda_value.png", "ppo_lambda_training.png", "ppo_lambda_probabilities.png"],
        )
        st.image(str(REPORTS / figure))
        st.caption("概率 TV 过门不等于 argmax 动作稳定；各自策略的 critic 误差不是同分布因果比较。")
        st.download_button(
            "下载消融 CSV",
            (REPORTS / "rl_ablation.csv").read_bytes(),
            "rl_ablation.csv",
            "text/csv",
        )
    else:
        st.subheader("历史风险快照与原计划审计")
        st.caption("风险数据对应归档 2020-12-31 组合；不是今天的敞口。")
        st.dataframe(pd.read_csv(REPORTS / "var_es.csv"), use_container_width=True)
        st.dataframe(pd.read_csv(REPORTS / "stress_test.csv"), use_container_width=True)
        st.json(facts["archived_acceptance"])
        status = read("acceptance_status.json")
        st.error("完整原计划验收尚未通过：SAC、风险惩罚、RL 去成本、完整五方等缺口仍保留。")
        st.dataframe(
            pd.DataFrame(status["items"])[["id", "plan", "status", "title", "remaining"]],
            use_container_width=True,
        )
        st.caption(facts["forward_policy"])
    with st.sidebar.expander("双语报告 / Reports"):
        for language in ("zh", "en"):
            st.download_button(
                language.upper(),
                (REPORTS / language / "final_report.md").read_bytes(),
                f"final_report_{language}.md",
                "text/markdown",
            )


if __name__ == "__main__":
    main()
