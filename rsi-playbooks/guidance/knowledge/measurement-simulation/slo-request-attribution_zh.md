<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 诊断 SLO 违约：逐请求联合达标检查与归因

[English](slo-request-attribution.md) | 简体中文 · [推理知识](../README_zh.md)

## 现象与动机

吞吐提高时，满足延迟要求的请求可能更少。仅成功请求的分位数隐藏失败和取消，token 总量又偏向长输出。诊断应先识别每次尝试是否满足全部服务目标（SLO），再指出违反了哪些条件。

## 原理与分析

N 次尝试跟踪到终止结果，观察时长 T；仅成功且全部条件达标时记 gᵢ=1。达标率为 `Σgᵢ/N`，请求有效吞吐为 `Σgᵢ/T`，token 有效吞吐为 `Σgᵢnᵢ/T`，其中 nᵢ 是达标请求的实际输出计数。

解释性算例中，每秒完成 100 个、60 个达标，与每秒完成 80 个、76 个达标，会得到相反的吞吐与有效吞吐排序。两个条件各 95% 达标时，联合最低可为 90%；相乘隐含独立性。

`p99(Q+P+D+O)` 也不等于阶段 p99 之和，各阶段可能由不同请求主导且会重叠。一次长停顿可以让最大间隔违约，而平均 TPOT 仍合格。

## 设计

先逐尝试计算联合达标，再汇总。每请求保存违约集合与负载类别，不强行把重叠违约归为一个原因。先选出端到端尾部请求，再看这些请求的因果阶段、过载、rank 与缓存环境。

明确统计总体和时间分母。整轮 T 包含排空、不含 warmup；固定稳态窗口统计其完成量，并另保留未完成工作。同一突发内请求相关，不确定性重采样以独立运行或时间块为单位。

## 实现

逐请求计算如下：

```text
对每次测量尝试：
    成功时 violations = {}，失败时为 {failure}
    对每个要求的 (指标, 上限)：
        读取请求指标
        缺失或超过上限时，将指标加入 violations
    compliant = violations 为空
    保留请求 ID、类别、达标状态与违约集合
attainment = 达标数 / 尝试数
request_goodput = 达标数 / T
仅在所需计数齐全时：
    token_goodput = 达标请求实际输出 token 总数 / T
按类别和违约集合汇总计数
按总延迟选尾部请求，再检查各自阶段记录
```

Foretoken TTFT 使用首个非空 `choices`，可能是 role／finish；请求 ITL SLO 使用最大接收 chunk 间隔，usage-only chunk 不推进计时。AIPerf 的平均 decode interval 是另一标准。Foretoken 一秒曲线将整条请求 token 计入完成窗口，不表示设备瞬时生产；合并窗口按计数，不无权平均达标率。

## 影响与效果

联合计算识别“某项通过、另一项失败”的请求，并保留违约类型间的重叠。分类计数能揭示完成混合变化或丢弃慢请求造成的表面尾延迟改善。尾部请求归因避免相加无关阶段分位数。统一计时和 token 来源，防止将 chunk 分组或缺失 usage 误当推理收益。

## 参考

- [Foretoken 指标](../../../../benchmarks/docs/metrics_zh.md)、[chunk 计时](../../../../benchmarks/src/benchmarks/integrations/streaming.py)和[HTTP runner](../../../../benchmarks/src/benchmarks/runs/http.py)：`e6ae3df6` 的联合条件、失败、接收 chunk 与完成窗口。
- [AIPerf 指标](https://github.com/ai-dynamo/aiperf/blob/e1daadd10faa395100384358131d784d6858c5ae/docs/metrics-reference.md)与[有效指标](https://github.com/ai-dynamo/aiperf/blob/e1daadd10faa395100384358131d784d6858c5ae/docs/reference/effective-vs-active-metrics.md)：引用版本的统计总体和归一化差异。
- [Vidur，MLSys 2024](https://www.microsoft.com/en-us/research/wp-content/uploads/2024/05/vidur_mlsys24.pdf)，§§7.2–7.3：接近容量时的误差放大，以及其评估系统中随负载变化的 SLO／成本取舍。
