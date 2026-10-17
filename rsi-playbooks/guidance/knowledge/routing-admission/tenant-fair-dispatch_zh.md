<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 避免租户饥饿：公平队列与有界派发

[English](tenant-fair-dispatch.md) | 简体中文

## 现象与动机

全局 FIFO 可能让一个租户的请求排在另一个租户上千请求的突发之后；严格 priority 排序也可能使低优先级流量饥饿。公平派发需要分开可执行 flow、共享有限容量，并在请求被遗弃后释放计量。

## 原理与分析

Priority 决定类别先后，公平性分配可执行 flow 的服务，容量上限限制活跃工作，过期限制有用等待；四者互不蕴含。llm-d 的 `globalstrict` 在优先级带内选队首，明确不保证租户公平。

Least-attained-service（LAS）偏好已获服务较少的程序，而非按请求数等分。llm-d 用输入／输出加权服务，输出权重为输入两倍，并按墙钟时间衰减历史。另一种 turn-priority 使用 `score = turnNumber + timeWeight × headWaitSeconds`，偏好依赖会话进度与局部性，而非租户等份服务。等待 credit 上限为 `timeWeight × requestTTL`，并不天然保证无饥饿。

## 设计

按可信身份建立逐 flow 队列，在 priority 与容量约束内选择队首。调用者可任意填写身份时，一个租户可以拆成多个 flow。Turn depth 要求一个 fairness identity 对应一个 session；default flow 的合计请求数不代表单个会话深度。

请求层拥有队列、预留、过期与派发，引擎拥有实际 token 服务，编排拥有副本。多 frontend 需明确计量范围，独立本地 counter 不保证 service 级公平。Foretoken 已把公平准入与实例评分分开，scorer 不再建立另一套队列。

## 实现

1. 解析可信租户／会话身份与 priority，由已有 admission owner 入队并记录过期时间。
2. 移除过期或取消队首，再检查 priority band 与 saturation 资格；llm-d priority holdback 会在压力上升时更早限制低优先级派发。
3. LAS 按实际经过时间衰减保留服务量，结合已获服务与队首等待选择；turn-priority 则比较会话深度加加权等待。
4. 预留容量、取出队首，经实例路由派发并记录 in-flight。
5. 完成后按报告的 token 使用量计费、释放 in-flight；错误／取消清理预留，物理资源由引擎释放。

AIBrix basic VTC 计入预计路由使用量，不按实际完成纠正；Production Stack 传递 priority。引擎 VTC／DLPM 的服务上界与 gateway 策略的范围不同。

## 影响与效果

逐 flow 队列让 dispatcher 无须清空突发，就能服务另一租户。LAS 区分 token 成本；turn-priority 有利于缓存局部性和依赖轮次，却可能延迟新会话。引用版本的 llm-d 默认 `0.05 × 60s = 3` 轮等待额度，首轮新会话无法在过期前超过第五轮会话。被遗弃的 in-flight 状态还会阻止淘汰。有界等待与生命周期释放限制滞留工作，实际 decode 公平仍由引擎负责。

## 参考

- [llm-d global-strict，commit 5253f555](https://github.com/llm-d/llm-d-inference-scheduler/blob/5253f55516f496129248766d1e4b745daba68887/pkg/epp/framework/plugins/flowcontrol/fairness/globalstrict/README.md)。
- [Program-aware fairness，同一 revision](https://github.com/llm-d/llm-d-inference-scheduler/blob/5253f55516f496129248766d1e4b745daba68887/pkg/epp/framework/plugins/flowcontrol/fairness/program-aware/README.md)。
- [Priority holdback，同一 revision](https://github.com/llm-d/llm-d-inference-scheduler/blob/5253f55516f496129248766d1e4b745daba68887/pkg/epp/framework/plugins/flowcontrol/usagelimits/priorityholdback/README.md)。
- [AIBrix VTC basic，commit d0500b03](https://github.com/vllm-project/aibrix/blob/d0500b0399cf35ad0bf95d073faa83c23308f463/pkg/plugins/gateway/algorithms/vtc/vtc_basic.go)及 [Production Stack routing，commit 4e076e8d](https://github.com/vllm-project/production-stack/blob/4e076e8de3698a3729b575e3a5ade65ffbb929cf/src/vllm_router/routers/routing_logic.py)。
- [VTC 方法](https://arxiv.org/html/2401.00588v2)与 [DLPM/D2LPM，2501.14312v1](https://arxiv.org/html/2501.14312v1)。
- [Foretoken admission owner](../../../../data-plane/frontend/src/admission/src/lib.rs)。
- [引擎公平性](../engine-scheduling/token-fairness-counters_zh.md)
