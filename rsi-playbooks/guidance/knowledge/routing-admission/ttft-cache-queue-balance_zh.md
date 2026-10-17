<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 权衡前缀复用与排队成本，降低首 Token 延迟

[English](ttft-cache-queue-balance.md) | 简体中文

## 现象与动机

前缀命中只有在 worker 真正执行请求后才省下计算。把所有共享前缀请求送到最暖副本，可能积累比节省 prefill 更久的等待。目标应是用户看到的 TTFT，而非最高命中率。

## 原理与分析

`estimated_TTFT = ingress_wait + worker_wait + cache_ready_time + uncached_prefill_time + response_overhead`。

这是分析分解，并非所有 router 的精确公式。索引报告的状态可能仍需从主存或远端恢复，命中长度不能单独描述就绪时间。

例如 10K 输入在 A 复用 9K，剩余 prefill 30 ms；B 未命中，prefill 220 ms。A/B 排队分别为 300/20 ms 时，B 的预计 240 ms 优于 A 的 330 ms；A 排队降到 40 ms 时，则 A 更快。这组示例成本说明亲和性必须与排队竞争。

## 设计

在可用候选中比较复用节省、增量工作与负载。Preble 估计近期计算、新 prefill 与驱逐成本。DualMap 将请求限制为两个 hash 候选，优先复用；预计 TTFT 违反 SLO 时切向低负载候选。自适应 prefix key 分散热家族、保留冷前缀局部性；排队迁移只去预定 backup，并要求预计收益为正。

引擎拥有缓存有效性、分配和逐轮调度，请求层拥有候选资格、评分与派发计量，编排拥有布局／容量。Frontend 本地观测保持本地范围。

## 实现

1. 过滤健康、兼容候选，获取可复用前缀与负载观测。
2. 区分 GPU 已就绪复用和需要恢复的状态，估计未命中后缀与排队成本。
3. 按选定机制评分。Dynamo 使用基于 block 的 prefill／decode 成本及 overlap credit，积压超过最低负载候选时衰减设备 credit；衰减用新请求大小归一化额外 backlog，不是毫秒估计。
4. 选中后立即计入派发，使下一请求看到新增压力。

AIBrix 混合标准化 prefix／load 分数，引用默认为 `5 × prefix + 4 × load_balance`，也支持有负载资格的 cache-first。Production Stack 从命中比例扣除相对 in-flight 请求负载。Foretoken 的 `kv_least_loaded`、`prefix`、`token_load` 和 `two_tier` 表达偏好，不是校准传输时间；llm-d 也提供可组合的 prefix／queue／load／latency 评分。

## 影响与效果

权衡缓存与排队能减少亲和热点，又保留廉价复用。低负载时暖位置通常占优，竞争较强时未命中 worker 也可能更早交付首 token。请求数负载会漏掉 prefill 工作差异，驱逐、滞后观测和 worker 速度差异也会反转排序。Session affinity 保留位置，却不证明 KV 仍驻留。

## 参考

- [Preble，2407.00023v2，2024-10-03](https://arxiv.org/html/2407.00023v2)：按执行时间结合利用/探索与驱逐成本。
- [DualMap，2602.06502v1，2026-02-06](https://arxiv.org/html/2602.06502v1)：双候选与按 SLO 切换缓存/负载。
- [Dynamo selector，commit 7708f041](https://github.com/ai-dynamo/dynamo/blob/7708f041a965e35374af976ab8ad610b9537e3bc/lib/kv-router/src/scheduling/selector/reference.rs)：backlog 与 overlap-credit 衰减。
- [AIBrix prefix routing，commit d0500b03](https://github.com/vllm-project/aibrix/blob/d0500b0399cf35ad0bf95d073faa83c23308f463/docs/source/features/gateway-routing/prefix-cache-aware-routing.rst)。
- [Production Stack routing，commit 4e076e8d](https://github.com/vllm-project/production-stack/blob/4e076e8de3698a3729b575e3a5ade65ffbb929cf/src/vllm_router/routers/routing_logic.py)。
- [llm-d prefix scorer，commit 5253f555](https://github.com/llm-d/llm-d-inference-scheduler/blob/5253f55516f496129248766d1e4b745daba68887/pkg/epp/framework/plugins/scheduling/scorer/prefix/README.md)。
- [Foretoken 路由](../../../../data-plane/frontend/src/router/README_zh.md)。
