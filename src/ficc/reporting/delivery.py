"""Normalize evidence without mixing cohorts or converting missing experiments into passes."""

import hashlib
import json
from pathlib import Path
from statistics import median

import pandas as pd


def read(root, name):
    return json.loads((root / name).read_text())


def sha(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


def baselines(matrix):
    rows = []
    for band, value in matrix["bands"].items():
        common = dict(
            cohort="rolling22_matched_test",
            start=matrix["test"][0],
            end=matrix["test"][1],
            band_lot=int(band),
            reference="same-band zero tilt",
            metric="cumulative net excess, bp (not annualized)",
            evidence="reports/ppo_matrix_review.json",
        )
        for model, stats in value["baselines"].items():
            rows.append({**common, "model": model, "seed": None, **stats})
        for stats in value["seeds"]:
            rows.append({**common, "model": "ppo", **stats})
    return pd.DataFrame(rows)


def ablations(root):
    rows = []

    def add(name, band, gains, source, comparator, net=None, gate=False, note=""):
        if len(gains) != 6:
            raise ValueError("expected three folds by two seeds")
        rows.append(
            dict(
                experiment=name,
                band_lot=int(band),
                cohort="development_folds_0_10_21_two_seeds",
                comparator=comparator,
                metric="paired validation net excess gain, bp",
                pairs=len(gains),
                median_gain_bp=median(gains),
                wins=sum(v > 1e-8 for v in gains),
                ties=sum(abs(v) <= 1e-8 for v in gains),
                losses=sum(v < -1e-8 for v in gains),
                median_net_vs_zero_bp=median(net) if net is not None else None,
                plateau_groups=None,
                expanded=gate,
                status="completed",
                evidence=source,
                note=note,
            )
        )

    source = "reports/ppo_factorial_validation.json"
    d = read(root, source)
    for band, entry in d["primary_paired"].items():
        add(
            "active_reward_scale100_clip5",
            band,
            entry["gains_bp"],
            source,
            "original continuous PPO",
            gate=d["expand_validation"],
            note="Combined factorial primary contrast; see source for all marginal and interaction effects.",
        )
    for band, combinations in d["absolute_vs_zero"].items():
        for name, entry in combinations.items():
            add(
                "factorial_" + name,
                band,
                entry["net_bp"],
                source,
                "same-band zero tilt",
                entry["net_bp"],
                d["expand_validation"],
                "Absolute validation excess for one frozen factorial cell; expansion belongs to the registered primary paired contrast.",
            )
    source = "reports/ppo_grid_review.json"
    d = read(root, source)["gate"]
    for band, entry in d["bands"].items():
        add(
            "four_action_active",
            band,
            entry["gain_bp"],
            source,
            "continuous PPO",
            entry["net_vs_zero_bp"],
            d["expand_validation"],
            "Action parameterization and exploration differ; not a capacity-only comparison.",
        )
        add(
            "active_vs_total_four_action",
            band,
            d["active_minus_total_grid_bp"][band],
            source,
            "four-action total reward",
        )
    source = "reports/ppo_critic_review.json"
    d = read(root, source)
    for band, entry in d["bands"].items():
        add(
            "zero_critic_active",
            band,
            entry["active_gain"],
            source,
            "original critic, active reward",
            entry["active_net"],
            d["expand_validation"],
        )
        add(
            "zero_critic_total",
            band,
            entry["total_gain"],
            source,
            "original critic, total reward",
            gate=d["expand_validation"],
        )
    for name, comparator, key in [
        ("convergence", "cash start update 2", "gain_vs_2"),
        ("invested", "cash start update 16", "gain_vs_cash_16"),
        ("actor_step", "invested standard actor update 16", "gain_vs_invested_16"),
        ("neutral", "slow actor update 16", "gain_vs_slow_16"),
        ("lambda", "neutral lambda=0.95 update 16", "gain_vs_neutral_16"),
    ]:
        source = f"reports/ppo_{name}_review.json"
        d = read(root, source)["summary"]
        for band, entry in d["paired"].items():
            add(
                name + "_16",
                band,
                entry[key],
                source,
                comparator,
                entry["net_excess_vs_zero_bp"],
                d["expand_validation"],
            )
            rows[-1]["plateau_groups"] = d["plateaus"]
    for name, note in [
        ("SAC", "No implementation or run; original-plan item deferred under training freeze."),
        (
            "risk_penalty_removal",
            "Hard constraints do not constitute an explicit risk penalty ablation.",
        ),
        ("RL_cost_removal", "Rule-based fixed-path cost addback is not RL training without costs."),
        (
            "five_way_matched",
            "Random/buy-and-hold/rule/ML/RL not all available on one matched period.",
        ),
    ]:
        rows.append(
            dict(
                experiment=name,
                status="not_completed",
                evidence="config/acceptance/delivery.json",
                note=note,
            )
        )
    return pd.DataFrame(rows)


def collect(root):
    plan = read(root, "config/acceptance/delivery.json")
    ids = [r["id"] for r in plan["items"]]
    if len(set(ids)) != len(ids) or not ids:
        raise ValueError("invalid acceptance item IDs")
    for item in plan["items"]:
        if item["status"] not in {"evidenced", "partial", "deferred"} or not item["evidence"]:
            raise ValueError("invalid acceptance evidence/status")
        for evidence in item["evidence"]:
            path = Path(evidence)
            if path.is_absolute() or ".." in path.parts:
                raise ValueError("evidence must be a project-relative path")
    matrix = read(root, "reports/ppo_matrix_review.json")
    lam = read(root, "reports/ppo_lambda_review.json")
    future = read(root, "reports/futures_signal_probe.json")
    coverage = read(root, "reports/futures_data_coverage.json")
    pivot = read(root, "reports/research_pivot_cost_audit.json")
    cost = read(root, "reports/cost_signal_review.json")
    checks = []
    for name in [
        "acceptance_summary",
        "acceptance_summary_rolling",
        "acceptance_summary_rolling_extended",
    ]:
        table = pd.read_csv(root / f"reports/{name}.csv")
        checks.append(
            dict(
                evidence=f"reports/{name}.csv",
                checks=len(table),
                passed=int(table.status.eq("PASS").sum()),
            )
        )
    if any(r["checks"] != r["passed"] for r in checks):
        raise ValueError("archived financial acceptance contains a failure")
    summary = dict(
        as_of=plan["as_of"],
        full_original_plan_accepted=all(r["status"] == "evidenced" for r in plan["items"]),
        strategy_claim=plan["strategy_claim"],
        budget_policy=plan["budget_policy"],
        forward_policy=plan["forward_policy"],
        test_window=matrix["test"],
        matched_ppo_median_net_bp={
            b: median(r["excess_bp"] for r in v["seeds"]) for b, v in matrix["bands"].items()
        },
        matched_baselines={b: v["baselines"] for b, v in matrix["bands"].items()},
        seed_mean_daily_inference={
            b: v["daily_seed_mean_inference"] for b, v in matrix["bands"].items()
        },
        lambda_plateaus=lam["summary"]["plateaus"],
        fixed_path_cost_restored_median_bp={
            b: r["restored_median_bp"] for b, r in pivot["bands"].items()
        },
        lambda_expand=lam["summary"]["expand_validation"],
        archived_acceptance=checks,
        futures_coverage={
            k: coverage[k] for k in ["rows", "dates", "first", "last", "variety_dates"]
        },
        futures_prediction_wins=future["primary"]["prediction_wins_at_2_lot"],
        futures_primary_median_bp=future["primary"]["median_bp"],
        futures_delay_median_bp=future["delay_sensitivity"]["median_bp"],
        futures_continue=future["consider_continuous_replay"],
        cost_signal={
            k: cost[k]
            for k in (
                "restored_prediction_wins",
                "restored_prediction_gate",
                "actual_cost_economics_gate",
                "route",
                "median_bp",
                "metric",
                "fixed_control",
            )
        },
    )
    return summary, plan, baselines(matrix), ablations(root)


def render_report(f, plan, english=False):
    en = english
    ppo = f["matched_ppo_median_net_bp"]
    b = f["matched_baselines"]
    v = f["futures_primary_median_bp"]
    cost = f["cost_signal"]
    lines = [
        "# China government bond research — delivery candidate"
        if en
        else "# 中国国债量化研究：当前交付候选报告",
        f"As of / 截至：{f['as_of']}。",
        (
            "**No robust post-cost alpha established. Original-plan acceptance remains incomplete.**"
            if en
            else "**尚无稳健的成本后超额证据，原计划完整验收尚未完成。**"
        ),
        "## Scope and method" if en else "## 范围与方法",
        (
            "The system joins yield-curve fitting, synthetic-bond pricing, DV01/KRD controls, whole-lot execution, settlement, financing, attribution and ML/PPO overlays in one ledger. Synthetic bonds and modeled execution do not establish live individual-bond liquidity. FDR007 is a funding proxy, not DR007. Observations stop before the decision; scalers fit training data; checkpoint selection uses validation windows."
            if en
            else "系统以同一账本串联曲线拟合、合成债券定价、DV01/KRD 约束、整手执行、结算融资、归因与 ML/PPO 倾斜策略。合成券和模拟执行不能证明实盘个券流动性；融资使用 FDR007 代理，不能等同 DR007。观测截止决策前，标准化只拟合训练段，checkpoint 只用验证窗选择。"
        ),
        (
            "The executed protocol has 22 rolling folds and five seeds; the original calendar split was 2019–22 / 2023 / 2024–25. These are different protocols. Historical tests subsequently reused for design are now development evidence. No adaptive-search-adjusted significance or new forward confirmation is claimed."
            if en
            else "实际执行为 22 个滚动折、5 个 seed，与原计划 2019–22／2023／2024–25 年代切分不同。原测试窗后来参与设计，后续结果属于开发证据，不宣称已校正自适应搜索的显著性或获得新时期确认。"
        ),
        "## Matched baselines" if en else "## 同口径基线",
        (
            f"The matched window is {f['test_window'][0]}–{f['test_window'][1]}. Median cumulative PPO net excess over same-band zero tilt is {ppo['2']:+.3f} bp (2 lot) and {ppo['1']:+.3f} bp (1 lot). Ridge is {b['2']['ridge']['excess_bp']:+.3f}/{b['1']['ridge']['excess_bp']:+.3f} bp; Tree is {b['2']['tree']['excess_bp']:+.3f}/{b['1']['tree']['excess_bp']:+.3f} bp. These are cumulative, not annualized values."
            if en
            else f"匹配期间为 {f['test_window'][0]} 至 {f['test_window'][1]}。PPO 相对同执行带零倾斜的累计净超额，五 seed 中位数在 2／1 lot 为 {ppo['2']:+.3f}／{ppo['1']:+.3f} bp。Ridge 为 {b['2']['ridge']['excess_bp']:+.3f}／{b['1']['ridge']['excess_bp']:+.3f} bp，Tree 为 {b['2']['tree']['excess_bp']:+.3f}／{b['1']['tree']['excess_bp']:+.3f} bp。这些是累计值，不是年化值。"
        ),
        "![Matched baselines](../delivery_baselines.png)",
        (
            "[Unified baseline CSV](../unified_baselines.csv) includes seed-level turnover, trades, and available tracking errors. Missing baseline tracking errors remain blank. [Rule-strategy long sample](../long_sample_summary.csv) has a different 2019–2025 cohort and is not mixed into this ranking. Zero tilt is not renamed buy-and-hold."
            if en
            else "[统一基线 CSV](../unified_baselines.csv)保留逐 seed 换手、成交数和已有跟踪误差；原摘要缺失的基线跟踪误差留空。[规则策略长样本](../long_sample_summary.csv)的 2019–2025 期间不同，不混入排名；零倾斜不改名为买入持有。"
        ),
        "## Inference and RL ablations" if en else "## 统计证据与 RL 消融",
    ]
    for band in ("2", "1"):
        s = f["seed_mean_daily_inference"][band]
        lines.append(
            f"- {band} lot: seed-mean daily excess, annualized / 逐日 seed 均值超额年化 {s['mean_ann_bp']:+.3f} bp; HAC 95% [{s['hac_ci_low_bp']:+.3f}, {s['hac_ci_high_bp']:+.3f}], block bootstrap 95% [{s['boot_ci_low_bp']:+.3f}, {s['boot_ci_high_bp']:+.3f}] bp; n={s['n_obs']}. "
        )
    lines.extend(
        [
            (
                "These intervals describe the archived seed-mean daily series, not the median-seed cumulative statistic or a correction for repeated model selection. [RL ablation CSV](../rl_ablation.csv) separates paired economic gains, absolute excess, platform counts, comparators and missing experiments. Total/active reward, scale, clipping, action discretization, critic initialization, start state, actor step, neutral initialization and GAE were examined; none justified expansion."
                if en
                else "上述区间描述归档的逐日 seed 均值序列，不是累计 seed 中位数的区间，也没有校正反复模型选择。[RL 消融 CSV](../rl_ablation.csv)区分配对增益、绝对净超额、平台数、比较对象及未完成项目。已检验总/主动奖励、尺度、截断、动作离散化、critic 初始化、开局持仓、actor 步长、中性初始化和 GAE，均未支持扩折。"
            ),
            (
                f"GAE λ=1 reaches {f['lambda_plateaus']}/6 joint platforms. Relative to λ=0.95, both bands have two wins, three ties and one loss; median gains are zero. Mean probability TV passing its threshold does not guarantee stable argmax actions. Value errors are measured on each policy's own trajectories; they do not identify a pure critic causal effect."
                if en
                else f"GAE λ=1 联合平台为 {f['lambda_plateaus']}/6；相对 λ=0.95 两档均为两胜三平一负，中位改善为零。平均概率 TV 过门不代表 argmax 交易指令稳定。价值误差基于各自策略的轨迹，不能识别纯 critic 因果效果。"
            ),
            (
                "Six reachable inventory paths reveal some optimal-action conflicts, but their average post-cost value is small, especially at 2 lot. One-step hindsight label opportunities are not continuous tradable returns. Keep active net reward, costs, present capacity and training budget frozen. A hold-relative reward based on policy-dependent holdings can change the economic objective and should not be added without a telescoping identity."
                if en
                else "六条合法可达持仓路径确实发现部分最优动作冲突，但平均成本后差额较小，尤其在 2 lot。单次事后标签空间不是连续可交易收益。继续冻结主动净奖励、成本、当前网络和训练预算；依赖策略持仓的 hold 反事实奖励可能改变经济目标，未经全期闭合证明不直接加入。"
            ),
            "## Newly audited information" if en else "## 新增信息审计",
            (
                "Official CFFEX monthly archives provide 1,573 sessions and 15,684 contract rows (2018-12-03–2025-05-30). Volume, open interest and settlement prices are available; bid–ask quotes, order-book depth, cash-bond trades, CTD basis and historical margin schedules are not validated. Nearest/next settlement spread is a calendar spread, not an execution spread."
                if en
                else "中金所官方月度包覆盖 1,573 个交易日、15,684 条合约记录（2018-12-03—2025-05-30）。成交量、持仓量和结算价可用；盘口、深度、现券成交、CTD 基差和历史保证金序列尚未核验。近月/次近月结算价差是跨月价差，不是执行买卖价差。"
            ),
            (
                "Daily reports are published after trading under [CFFEX Article 11](http://www.cffex.com.cn/u/cms/www/202003/271646475n34.pdf). Next-calendar-day midnight is an availability assumption, not an observed historical timestamp; one extra session of delay is tested. Independent pre-frozen vendor dates check complete-day coverage. Historical publication revisions remain unknown."
                if en
                else "[中金所信息管理办法第十一条](http://www.cffex.com.cn/u/cms/www/202003/271646475n34.pdf)规定每日信息在收盘后发布。次日零点是可用时点假设，不是实际历史时间戳；另检验多延迟一交易日。整日覆盖通过下载前冻结的另一来源日期检查；历史修订版本仍未知。"
            ),
            (
                f"The frozen four-feature Ridge probe wins against both original Ridge and the fit-mean RMSE in {f['futures_prediction_wins']}/3 folds. Median one-interval net-value gain versus original Ridge is {v['2']['gain_vs_original_bp']:+.3f}/{v['1']['gain_vs_original_bp']:+.3f} bp (2/1 lot), and versus the fit-selected continuous fixed control is {v['2']['gain_vs_continuous_fixed_bp']:+.3f}/{v['1']['gain_vs_continuous_fixed_bp']:+.3f} bp. The gate fails. These labels reset to benchmark holdings; they cannot be summed as portfolio PnL."
                if en
                else f"冻结的四特征 Ridge 检验，仅 {f['futures_prediction_wins']}/3 折同时降低相对原 Ridge 和拟合均值的 RMSE。2／1 lot 单区间净价值相对原 Ridge 的三折中位增量为 {v['2']['gain_vs_original_bp']:+.3f}／{v['1']['gain_vs_original_bp']:+.3f} bp，相对拟合段连续固定策略选档为 {v['2']['gain_vs_continuous_fixed_bp']:+.3f}／{v['1']['gain_vs_continuous_fixed_bp']:+.3f} bp。未过门；标签重置到基准持仓，不能累加为组合盈亏。"
            ),
            "![Futures signal RMSE](../delivery_futures.png)",
            (
                "Additional-delay median gains versus original Ridge are "
                if en
                else "额外延迟一日后，相对原 Ridge 的中位标签增量为 "
            )
            + f"{f['futures_delay_median_bp']['2']['gain_vs_original_bp']:+.3f}/{f['futures_delay_median_bp']['1']['gain_vs_original_bp']:+.3f} bp (2/1 lot).",
            "## Cost versus prediction diagnostic" if en else "## 成本与预测拆分终局",
            (
                f"All 181 interval records (724 action values) completed and reproduced locally; an independent augmented least-squares fit verified both target models. Cost-restored prediction beats the fitting-mean forecast in {cost['restored_prediction_wins']}/3 folds at 2 lot, below the required 2/3. Both registered gates fail. Median actual net gain versus original net-target Ridge is {cost['median_bp']['2']['actual_vs_net_selector_bp']:+.3f}/{cost['median_bp']['1']['actual_vs_net_selector_bp']:+.3f} bp; versus the restored-target fit-selected fixed action it is {cost['median_bp']['2']['actual_vs_restored_fixed_bp']:+.3f}/{cost['median_bp']['1']['actual_vs_restored_fixed_bp']:+.3f} bp (2/1 lot)."
                if en
                else f"181 个区间记录、724 个动作价值全部完成，本地复算及独立增广最小二乘核验通过。2 lot 的成本加回预测仅 {cost['restored_prediction_wins']}/3 折优于拟合均值预测，低于至少 2/3 的门槛；预测与真实成本经济门槛均失败。实际净价值相对原净目标 Ridge 的三折中位增量为 {cost['median_bp']['2']['actual_vs_net_selector_bp']:+.3f}／{cost['median_bp']['1']['actual_vs_net_selector_bp']:+.3f} bp，相对成本加回目标的拟合段固定动作则为 {cost['median_bp']['2']['actual_vs_restored_fixed_bp']:+.3f}／{cost['median_bp']['1']['actual_vs_restored_fixed_bp']:+.3f} bp（2／1 lot）。"
            ),
            (
                "The restored-target choices change holdings versus zero tilt in 44/47 intervals at 2 lot and 47/47 at 1 lot. Failure is therefore not simply blocked orders. The fixed control is long duration in all folds, selected by fitting-label means, not by a continuous fixed-strategy ledger. Values above are means of reset-state one-interval labels, not cumulative returns. Restoring realized costs does not create a reoptimized zero-cost environment. [Audited evidence](../cost_signal_review.json)."
                if en
                else "所选动作在 2 lot 的 44/47、1 lot 的 47/47 个区间改变了相对零倾斜的持仓，失败不能简单归于订单被挡住。三折固定对照均为加久期，由拟合段单次标签均值选出，并非连续固定账本的最优策略。上述数值为重置持仓后的单区间标签均值，不能累加为连续组合收益；已计成本加回也不等于零成本重优化环境。[完整复核](../cost_signal_review.json)。"
            ),
            "![Cost and prediction diagnostic](../delivery_cost_signal.png)",
            "## Acceptance and reproduction" if en else "## 验收与复现",
            (
                "Archived ledger acceptances: 71/71, 92/92 and 92/92. These are preserved prior results, not freshly rerun experiments. Report and UI correctness cannot replace missing research. The original-plan checklist follows; no waiver is inferred."
                if en
                else "已归档账本验收为 71/71、92/92、92/92，这是保留的既有结果，不是本轮重新执行。报告和界面正确不能替代未完成研究。以下逐项保留原计划差距，不推断任何豁免。"
            ),
        ]
    )
    for item in plan["items"]:
        lines.append(
            f"- `{item['id']}` — **{item['status']}**: {item['title']}. {item['remaining']} "
            f"[Evidence / 证据](../../{item['evidence'][0]})"
        )
    lines.extend(
        [
            (
                "`make reproduce` rebuilds both reports, summary tables and three Python figures from tracked evidence, without training or licensed raw data. `make delivery-check` detects source/summary drift. `make serve` opens four read-only pages. [Reproduction boundaries](../../docs/09_reproduction.md), [five-minute demonstration and Q&A](../../docs/05_defense_qa.md), [acceptance status](../acceptance_status.json)."
                if en
                else "`make reproduce` 从已提交证据重建双语报告、汇总表和三张 Python 图，不执行训练，也不需要许可原始数据。`make delivery-check` 检查源文件与产物漂移，`make serve` 启动四页只读展示。[复现边界](../../docs/09_reproduction.md)、[五分钟演示与问答](../../docs/05_defense_qa.md)、[验收状态](../acceptance_status.json)。"
            ),
            "## Next decision" if en else "## 下一步决策",
            (
                f"Stop this feature/action family under the registered route. After restoring realized incremental costs on the original PPO paths, median excess remains {f['fixed_path_cost_restored_median_bp']['2']:+.3f}/{f['fixed_path_cost_restored_median_bp']['1']:+.3f} bp (2/1 lot); the completed label diagnostic also fails its prediction gate. Neither result establishes universal signal absence. Do not launch the execution-frequency grid or resume PPO. Next audit historical funding-operation announcements, publication versions and full coverage before freezing any new predictive task. Four official sample announcements have been inspected; no complete series or new model is claimed. [Review and revised route](../../docs/10_research_pivot.md). Original-plan gaps and reserved forward data remain unchanged."
                if en
                else f"按冻结规则停止这套特征／动作组合。沿原 PPO 成交路径加回成本后，2／1 lot 中位超额仍为 {f['fixed_path_cost_restored_median_bp']['2']:+.3f}／{f['fixed_path_cost_restored_median_bp']['1']:+.3f} bp，本轮标签预测门槛也失败；均不能外推为市场普遍无信号。不启动交易频率网格或恢复 PPO。下一步先核验资金操作公告的历史发布版本和完整覆盖，再冻结新的预测任务。目前只核查了四份官方样例，尚未取得完整序列或训练新模型。[审查复核与新路线](../../docs/10_research_pivot.md)。原计划缺项与前瞻隔离保持不变。"
            ),
        ]
    )
    return "\n\n".join(lines) + "\n"
