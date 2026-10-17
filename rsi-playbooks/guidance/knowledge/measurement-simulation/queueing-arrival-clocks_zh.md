<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 诊断被遗漏的排队：按计划到达时间测量延迟

[English](queueing-arrival-clocks.md) | 简体中文 · [推理知识](../README_zh.md)

## 现象与动机

服务延迟看似稳定时，并发上限或过载压测端可能已在上游积累队列。只测实际发送到响应，会遗漏原定到达前后的等待。平均输入／输出长度还掩盖突发、长度关联，以及长期占用 KV 或打断 decode 的长请求。

## 原理与分析

计划到达 tₐ、实际发送 tₛ、完成 t𝒻 满足：

```text
到达口径延迟 = t𝒻−tₐ = 发送等待 + 请求延迟
发送等待 = tₛ−tₐ
```

闭环在服务变慢时自动降低发起负载：C 个客户端、响应时间 R、思考时间 Z，吞吐近似 C/(R+Z)。外部计划到达不随完成减速；丢失计划时间会造成 coordinated omission，即遗漏原本应计入的等待。

解释性 M/M/1 模型在泊松到达、独立指数服务和稳定单服务者前提下，平均响应为 `1/(μ−λ)`。λ=90 请求／秒时，μ=100 得到 100 ms，μ=95 得到 200 ms。连续组批不符合这些假设，算例说明排队敏感性，不预测引擎性能。

## 设计

每请求保留计划 offset、实际发送、首响应、终止时间、结果及输入／输出长度对，保持到达顺序与关联。准入窗口和排空时间分别统计，未完成尝试也保留。

Azure 2024 等时间戳／长度 trace 没有 prompt 原文，合成相同长度不能还原真实前缀复用或停止行为。按实际派发方式区分外部无上限调度、有上限重放和闭环，不能只因有 trace 文件就称作开环。

## 实现

尽量使用同一单调时钟计算：

```text
对每个计划请求 i：
    scheduled = run_origin + trace_offset[i]
    等待客户端准入时仍保留 scheduled
    记录 send、first_event 和 terminal
    send_delay[i] = send−scheduled
    request_latency[i] = terminal−send
    arrival_latency[i] = terminal−scheduled
    arrival_ttft[i] = first_event−scheduled
统计计划、发送、完成、失败与仍待处理的请求
按声明的统计总体汇总时间分布
```

发送等待上涨而请求延迟平稳，说明上游积压；两者均上涨，说明发送前后都有延迟。稳定且边界一致的总体可用 `L=λW` 检查在途数量、速率和停留时间，不能将已发送并发与到达口径 W 混用。

Foretoken 保留计划 offset，记录 send／replay delay 及 trace 相对计时；trace 记录使用 `open_loop=False`，普通延迟汇总仍是请求时钟。AIPerf 同样区分 active 与 credit 到达口径的 effective latency。

## 影响与效果

到达口径揭示客户端准入隐藏的等待，并分离压测端和服务延迟。保留 trace 关联可区分突发、长请求与平均长度效应。保留失败和未完成工作，避免只选完成请求而把过载显示为健康。这些计算诊断被遗漏的需求，不会提高压测端实际派发能力。

## 参考

- [Azure 推理数据说明](https://github.com/Azure/AzurePublicDataset/blob/master/AzureLLMInferenceDataset2024.md)：2024 代码／对话 trace 的字段、隐私省略与采集时间。
- [AIPerf effective 与 active 指标](https://github.com/ai-dynamo/aiperf/blob/e1daadd10faa395100384358131d784d6858c5ae/docs/reference/effective-vs-active-metrics.md)：引用版本的 credit 排队、有效延迟与统计总体。
- [Foretoken 调度](../../../../benchmarks/src/benchmarks/runs/scheduling.py)、[HTTP 执行](../../../../benchmarks/src/benchmarks/runs/http.py)和[指标](../../../../benchmarks/docs/metrics_zh.md)：`e6ae3df6` 的计划到达、准入限制、排空及 trace 相对计时。
