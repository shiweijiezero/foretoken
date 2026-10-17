<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 通过延迟预测路由与派发预留减少排队

[English](latency-predictive-routing.md) | 简体中文

## 现象与动机

队长只表示等待请求数，不表示新请求前面还有多少工作。三个未缓存的 16K-token 输入可能比十个 128-token 输入更重。引擎指标延迟还会让一段突发反复选择首次派发前看起来空闲的 endpoint。

## 原理与分析

`finish_estimate = current_wait_estimate + incremental_service_estimate`。

估计需区分未缓存输入、已有上下文、decode 人数和 worker 配置；长前缀命中可能反转两个队列的排序。Preble 使用测量得到的 token 成本、近期负载与驱逐成本，DualMap 在双候选中使用 pending prefill token。校准绑定模型、dtype、backend、拓扑和并行配置。

派发预留代表引擎观测尚未包含的工作。若已包含仍叠加，就会重复计量；归并需要单一生命周期 owner 与明确观测边界。

## 设计

为各可用 worker 预测增量延迟，并立即记入已派发但尚不可见的工作。引擎发布执行／缓存观测，请求层负责选择、预留与释放；Foretoken 的 request ledger 承担相应计量，集群编排负责容量。

Dynamo 已按 prefill 工作量、缓存 overlap、decode 成本和活跃请求评分；Production Stack 用 in-prefill 加 in-decoding 请求数，AIBrix 已提供 Preble。llm-d 有 XGBoost sidecar、生命周期 hook 和 endpoint TTFT/TPOT headroom，预测路由不只是替换最少请求选择。

## 实现

1. 将 worker 采样状态与该采样尚未包含的派发工作合并。
2. 使用语义一致的流式标签和 worker 校准配置，估计逐请求 TTFT/TPOT 与 SLO 余量。
3. llm-d 按 headroom 分组；全部缺失时回退到 KV／队列／前缀组合。全部不利时优先 idle 或较轻 deficit 的 endpoint，仍是排序而非拒绝。
4. 选择目的地，并在下一次派发读取负载前预留增量工作。
5. 执行观测包含该工作后归并预留；完成、错误或取消时释放剩余生命周期计量。

引用版本的 scorer 允许部分缺失预测以零 headroom 留在非负组，与全部缺失的回退不同。非流式 TTFT 训练标签取 EOS，实际表示完成延迟，TPOT 被中和，不能当作流式首 token 标签。

## 影响与效果

工作量预测能区分异构队列和缓存相关增量成本；预留减少指标空窗中的集中派发，又不重复引擎计量。低负载时预测开销可能超过收益，kernel 改变或状态滞后也可能在平均误差很小时反转排序。明确缺失数据与标签语义，才能保持选择结果的含义。

## 参考

- [Preble v2，2024-10-03](https://arxiv.org/html/2407.00023v2)及 [DualMap v1，2026-02-06](https://arxiv.org/html/2602.06502v1)：请求路由边界的成本预测。
- [llm-d predicted-latency producer，commit 5253f555](https://github.com/llm-d/llm-d-inference-scheduler/blob/5253f55516f496129248766d1e4b745daba68887/pkg/epp/framework/plugins/requestcontrol/dataproducer/predictedlatency/README.md)：生命周期 hook 与标签语义。
- [llm-d latency scorer，同一 revision](https://github.com/llm-d/llm-d-inference-scheduler/blob/5253f55516f496129248766d1e4b745daba68887/pkg/epp/framework/plugins/scheduling/scorer/latency/plugin.go)：实际分组与 fallback。
- [Dynamo selector，commit 7708f041](https://github.com/ai-dynamo/dynamo/blob/7708f041a965e35374af976ab8ad610b9537e3bc/lib/kv-router/src/scheduling/selector/reference.rs)。
- [Production Stack routing，commit 4e076e8d](https://github.com/vllm-project/production-stack/blob/4e076e8de3698a3729b575e3a5ade65ffbb929cf/src/vllm_router/routers/routing_logic.py)。
- [AIBrix 路由文档，commit d0500b03](https://github.com/vllm-project/aibrix/blob/d0500b0399cf35ad0bf95d073faa83c23308f463/docs/source/features/gateway-routing/prefix-cache-aware-routing.rst)。
