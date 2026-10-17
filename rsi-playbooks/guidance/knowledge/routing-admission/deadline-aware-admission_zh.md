<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 通过预测期限可达性准入提高 SLO 有效吞吐

[English](deadline-aware-admission.md) | 简体中文

## 现象与动机

过载时转发所有请求，可能把 GPU 时间花在已经无法达标的工作上，并拖慢本来可达标的请求。选出相对最不差的 worker，不等于证明期限可达。准入决定执行、等待还是拒绝；路由为可执行请求选目的地。

## 原理与分析

300-ms TTFT 目标在准备与入口等待消耗 100 ms 后，只剩 200 ms。如果最好 worker 还需排队 120 ms、prefill 100 ms，就已缺少 20 ms；给下游重置一个 300-ms timeout 会隐藏入口耗时。

`remaining_slack = arrival_deadline - now - estimated_downstream_latency`。

TTFT 检查首 token 工作，完成期限还需包含生成。Request deadline、queue TTL 和 TPOT 各有独立语义。负余量表示预测不可达，模型误差可能造成误拒或过载。

SCORPIO 联合 TTFT 拒绝、期限队列与 TPOT 检查。Virtual batch size 是异构 token 频率比例之和，新增运行集合的预测 TPOT 必须满足其中最严格目标；准入与 decode credit 因而依赖同一套校准延迟与服务频率假设。

## 设计

各阶段保留原始到达时间与可信目标。入口负责队列、准入预留和响应结果，引擎负责批次可执行性与物理资源释放，集群编排提供较慢的容量响应。Scorer 排序 worker，不修改全局准入容量。

llm-d 的 saturation／flow-control 准入与 headroom scorer 是不同路径；预测不利仍可能选 idle 或较小 deficit 的 endpoint。Production Stack 传递 priority，而非预测拒绝。Foretoken 的角色等待、并发计量和期限结果承担队列与生命周期边界。

## 实现

1. 捕获到达时间，解析可信目标，生成绝对期限；下游不重置时钟。
2. 估计各可用目的地剩余首 token 工作，包括排队、缓存准备、未命中 prefill 与发送开销。
3. 用绑定部署模型、dtype、backend、并行配置的估计，检查首 token 余量及新增集合的 TPOT／内存可执行性。
4. 为可执行请求预留容量并派发；否则仅在队列策略和剩余期限仍允许有效派发时等待，对不可达请求记录拒绝或过期。
5. 容量变化后重新判断等待工作。取消或完成时释放入口计量，引擎异步释放自己的资源。

## 影响与效果

可达性准入减少预测会迟到的工作，能够保留更多有效完成。激进拒绝可能不利于长输入、严格类别或建模不足的租户；等待缓解短突发，也会消耗期限。

`goodput = 同时满足全部声明 SLO 的完成请求数 / 时间`。

全到达达标率把拒绝和超时计为未达标，除非 benchmark 明确采用另一契约。1,000 个到达请求中，拒绝 300 后有效完成 650，全到达达标率是 65%，而非已接纳口径的 93%；相比有效完成 500，这增加了有用服务，分母不变。SCORPIO 同样将拒绝计为 SLO 未满足。

## 参考

- [SCORPIO，2505.23022v2，2026-08-26](https://arxiv.org/html/2505.23022v2)：TTFT/TPOT 联合准入与拒绝计量。
- [llm-d admission，commit 5253f555](https://github.com/llm-d/llm-d-inference-scheduler/blob/5253f55516f496129248766d1e4b745daba68887/pkg/epp/requestcontrol/admission.go)。
- [llm-d latency scorer，同一 revision](https://github.com/llm-d/llm-d-inference-scheduler/blob/5253f55516f496129248766d1e4b745daba68887/pkg/epp/framework/plugins/scheduling/scorer/latency/plugin.go)：排序与拒绝的区别。
- [Production Stack routing，commit 4e076e8d](https://github.com/vllm-project/production-stack/blob/4e076e8de3698a3729b575e3a5ade65ffbb929cf/src/vllm_router/routers/routing_logic.py)：现有 priority 传递。
- [Foretoken admission](../../../../data-plane/frontend/src/admission/src/lib.rs)与[到达时间捕获](../../../../data-plane/frontend/src/server/src/admission.rs)：现有结果与时间边界。
