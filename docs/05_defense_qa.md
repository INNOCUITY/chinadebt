# 五分钟演示与问答 / Five-minute demonstration

演示前：`make delivery-check`，随后 `make serve`。只读归档；展示正常不代表完整研究验收通过。

## 演示顺序

1. **0:00–1:00，曲线与数据**：说明中债曲线、合成券、FDR007 代理及覆盖边界。展示期货覆盖；明确跨月结算价差不是盘口价差。
2. **1:00–2:15，基线与回测**：切换 2/1 lot，指出同窗口 22 折、5 seed。PPO 中位净超额为 −3.93/−33.31 bp，Ridge 在两档变号。查看逐 seed 成交与换手，不跨期间混排策略。
3. **2:15–3:30，RL 与消融**：看 λ=1 的价值误差、净超额与概率图；3/6 平台，两档中位改善为零。展示“概率接近但 argmax 改变”和动作未必变持仓的解释。
4. **3:30–4:30，风险与验收**：风险为 2020-12-31 的历史组合快照；VaR/ES 与压力重估口径不同。账本三套历史验收 71/92/92 全通过。SAC、显式风险惩罚、RL 去成本等保留未完成状态。
5. **4:30–5:00，结论**：工程可以交付负面研究证据；不扩容量/轮数，保留前瞻期。报告可下载，公开复现和全研究复现的边界分别说明。

English opening: “This project tests whether a curve-based bond overlay adds value after execution and financing costs. The ledger and PPO workflow are reproducible, but robust alpha has not been established. I will show matched baselines, execution evidence and the remaining acceptance gaps.”

English closing: “We freeze training capacity and budget. New inputs must establish availability and incremental prediction before entering a policy. Public reports rebuild from archived evidence; full research additionally requires the exact source snapshots and model archives.”

## 十个问题

1. **赚钱了吗？** 没有稳健超额证据。PPO 跨 seed 中位数为负；主配置个别正收益不能盖过 1 lot 退化和固定策略对照。
2. **是不是训练太少？** 2→8、16 次更新及学习率/初始化/λ 对照均未过门；当前瓶颈证据不支持继续增加预算。
3. **为什么不用 Transformer？** 每训练窗约 47 个决策，同窗反复训练不增加市场样本。没有容量不足的证据。
4. **主动奖励是不是更好？** 去掉共同市场收益改善信号表达，但价值误差、优势与实际成交未稳定改善。净奖励含交易、融资和终止费用，经济目标保持不变。
5. **为什么不给 hold 基线？** 依赖策略持仓的反事实 hold 可改变全期目标；目前作为单步动作价值诊断，不能直接逐步加总到 PPO 奖励。
6. **怎么防止未来数据？** 前一交易日截止、训练段统计、purge、独立冻结日历、散列匹配和未来扰动测试。期货真实历史发布时间缺档，使用保守假设及额外延迟对照，明确局限。
7. **成交量和盘口有什么作用？** 成交量/OI 可反映市场活动，本次四特征增量未过门。日频结算价不能恢复盘口或真实执行滑点，仍需合规取得带时间戳的报价/深度。
8. **平台稳定为什么还不赚钱？** 概率平均 TV 小不代表最大概率动作不变；风险投影、整手和死区也可令不同动作得到相同持仓。收益改善有时只是停止亏损交易。
9. **SAC、风险惩罚、DSR 去哪了？** 明确列为未完成或部分完成；硬约束不是风险惩罚，HAC 不是 DSR。不因整理报告而改成完成。
10. **别人如何复现？** 公开 clone 后 `make reproduce` 与 `make test-public` 检查汇总/契约；完整研究还需相同许可数据、标签与 checkpoint。原始数据和访问凭据不公开，缺输入立即报错。
