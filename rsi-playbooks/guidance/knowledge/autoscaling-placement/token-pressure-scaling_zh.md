<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 确定副本数量：未命中 token 工作量与 KV 压力

[English](token-pressure-scaling.md) | 简体中文

## 现象与动机

十个 256-token 输入与十个 32K 输入队长相同，计算和 KV 需求却不同。前缀复用、DP rank 偏斜进一步改变容量。两个 rank 的 KV 压力为 0.95 和 0.15 时，平均 0.55 看似宽裕，热 rank 却可能停止准入。

## 原理与分析

状态包含未缓存排队 prefill、已调度／排队 decode 上下文、KV 容量、活跃请求和就绪 worker，并明确单位。排队 token 除以校准 prefill token/s 是积压秒数，上下文需求除以 KV token 容量是内存压力，均不自动等于 TTFT。共享前缀占据缓存，却不产生等量新 prefill，不能把原始输入长度直接求和当作执行工作。

跨模型服务方面，TRE 提出 `TSS_raw = (w_p*r_prefill + r_decode)/(active + w_q*waiting)`。速率为同一窗口的 token/s，分母为请求数，权重无量纲；平滑后再除以模型／SLO 对应健康边界 θ。TSS_raw 与 θ 都表示每个加权未完成请求获得的加权 token/s，比值无量纲，低于 1 表示校准服务不足。

## 设计

保留逐 engine 需求，不用 pool 均值隐藏热点。扩容响应热 engine，缩容检查剩余 engine 能否承受重新分配。新增容量仍需要路由把工作分散过去。

KV 按实际全注意力、MLA、滑动窗口或混合分配计量。权重与缓存 dtype 不同，FP8 权重加快计算，不减少 BF16 上下文驻留。未知输出、early EOS 和 rank 分片影响估计。请求层拥有在途 ledger，引擎拥有调度／缓存状态，controller 准备观测，算法只返回建议。

## 实现

1. 按 engine 收集未缓存 prefill 队列、已调度／排队 decode 上下文和 KV 容量，保留新鲜度与布局范围。
2. Foretoken prefill preset 用排队 token 除以 context length，自定义 `load` 使用绝对 token；decode 用已调度加排队上下文除以 KV 容量，aggregate 再加 queued prefill。
3. 任一 engine 满足扩容阈值时请求增加一个 Group；只有全部满足缩容阈值、估计重新分配后仍低于扩容阈值，才移除容量。
4. 由 controller 应用上下界与生命周期约束；encoder pool 或观测不可用／不完整／过期时，在配置范围内保持容量。

Foretoken 当前观测限定单 full-attention cache group、PCP/DCP=1。llm-d in-flight producer 扣除前缀命中，stream start 移除 prefill，stream end／TTL 淘汰请求，endpoint 删除后清理序列。其 P/D trigger 分别使用总 prefill backlog 除以实测吞吐、decode KV fraction 求和。TRE 则在共置 hot-switch 原型中单独校准 TSS 阈值。

## 影响与效果

Token 与 KV 信号能识别请求率不变时的长度变化，以及 rank 局部容量不足，减少请求数扩缩容的错配；单纯增加副本不能消除持续路由偏斜。输出预测可能高估早 EOS、低估长尾，详细但滞后的状态也可能不如实时粗信号。TSS 归一化提供校准后的跨模型比较，而不是把原始 token/s 当作等价服务。

## 参考

- [Foretoken token-load 算法](../../../../control-plane/internal/autoscaling/algorithm/decision/token_load.go)
- [自动扩缩容实现](../../../../control-plane/internal/autoscaling/README_zh.md)
- [llm-d 在途 producer](https://github.com/llm-d/llm-d-inference-scheduler/blob/5253f55516f496129248766d1e4b745daba68887/pkg/epp/framework/plugins/requestcontrol/dataproducer/inflightload/producer.go)
- [llm-d P/D prefill trigger](https://github.com/llm-d/llm-d/blob/277b34d062ff954435cf8b1b05efbd75090cf788/guides/workload-autoscaling/keda-epp-token-aware/pd-disaggregation/base/prefill-scaledobject.yaml)
- [llm-d decode trigger](https://github.com/llm-d/llm-d/blob/277b34d062ff954435cf8b1b05efbd75090cf788/guides/workload-autoscaling/keda-epp-token-aware/pd-disaggregation/base/decode-scaledobject.yaml)
- [TRE](https://arxiv.org/html/2609.29160v1)
- [TRE 公开实现](https://github.com/zxzx9898/Token-service-share_Rebalancing_Engine)
- [路由与准入](../routing-admission/)
