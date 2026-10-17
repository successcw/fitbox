# 睡眠统计口径与来源

本文解释已经实现的统计，不是诊断或调压方案。

## 数据来源

- Apple Health 日汇总提供整晚起止时间、各阶段总时长。
- Health Auto Export 不汇总 JSON 提供逐段开始、结束和阶段名称。
- ResMed STR 提供治疗会话及日摘要；PLD 提供呼吸频率、漏气等时间序列；EVE 提供设备标记事件。
- 手表按 `FITBOX_TIMEZONE` 转换；EDF 没有时区，假设机器时钟与本地一致，无法自动证明两台设备时钟已校准。

## 已实现的统计

1. AHI 使用设备治疗期间的分母，关机后的事件未知。瑞思迈公布的治疗目标为每小时少于 5 次。[来源](https://www.resmed.com/en-us/sleep-health/resources/faqs/)
2. 呼吸频率中位数与前 7 个有效夜间摘要比较，这是个人趋势参考，不是厂商正常范围。
3. 漏气 P95 是 95% 有效时间不超过的漏气值。日报采用 24 L/min 参考值；AirView 原规则是整晚漏气 P95 超过 24 L/min 连续 5 天才进入对应关注组，日报不复现其全部规则。[来源](https://document.resmed.com/en-epn/documents/products/data-management/airview/action-groups/1019698-AirView-Action-Groups-Guide-EMEA-ENG.pdf)
4. 分期对照：合并同阶段重复区间，拒绝不同阶段重叠冲突。将手表区间与 PAP 会话及 PLD 采样时间相交，计算时间加权呼吸频率中位数、漏气 P95 和有效覆盖。覆盖少于 80% 或有效时间少于 5 分钟时，表格不显示阶段数值。两个阈值都是工程选择，不是临床标准。
5. 停机前 10 分钟的有效 PLD/EVE 检查留在本地分析 JSON；不从停机推断用户摘机、焦虑或觉醒的原因。STR 会话边界约精确到分钟。

## 不能据此推断什么

手表分期是消费级算法估计，不能替代脑电睡眠分期。[Apple 验证白皮书](https://www.apple.com/health/pdf/Estimating_Sleep_Stages_from_Apple_Watch_Oct_2025.pdf)、[AASM 立场](https://pmc.ncbi.nlm.nih.gov/articles/PMC5940440/)。分阶段对照是本项目自行实现的描述统计，不是瑞思迈官方分期算法。

某阶段漏气较低不证明它导致更好的深睡，也不证明阶段本身改变面罩密合。体位、张口、压力、时段和测量误差可能混杂。瑞思迈说明，整体密合良好也可能有翻身或张口带来的短暂漏气。[来源](https://ap.resmed.com/knowledge/why-did-i-receive-an-email-saying-my-mask-is-leaking)

单晚差异不等于统计显著；波形样本彼此相关，不能把每两秒采样当成独立实验。没有数据不填 0，不据此自动调压或给出诊断。
