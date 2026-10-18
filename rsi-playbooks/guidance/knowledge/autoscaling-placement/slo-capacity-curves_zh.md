<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 估算满足 SLO 的容量：校准服务曲线

[English](slo-capacity-curves.md) | 简体中文

## 现象与动机

峰值 token/s 不能说明一个副本能在 TTFT 与 token 间隔目标内服务多少请求。长度、缓存复用、批处理和目标分位数都会改变容量。只按 decode 吞吐确定副本，无法保护 prefill 队列与首 token 期限。

## 原理与分析

对请求数/秒的到达率 λ，按指定负载定义 `μ_SLO = sup{λ: 所需延迟和成功约束成立}`。初步副本目标为 `ceil(λ_demand/μ_SLO)`，但负载不均、缓存碎片和启动延迟使它不是保证。分离服务分别计算 P/D，传输也消耗首 token 预算。

近似排队模型用 `T(i)=α+iδ` 描述迭代：i 为活跃批量，α 为固定开销，δ 为每个活跃请求的增量时间，包含计算与 KV 访问。Birth–death 状态计入运行和等待请求；超过批次上限 B 时，仅 B 个请求活跃。状态相关服务估计占用、等待和平均 TTFT/ITL。

Poisson 到达与平均长度使它成为简洁均值模型，而非尾延迟预测器。内存可能先于计算限制容量，缓存 dtype、混合组、TP 复制和长输出改变这一边界，与权重 GEMM 加速不同。

## 设计

服务曲线绑定模型、设备、权重／缓存 dtype、runtime 和并行配置，吞吐旁保留延迟分位数与成功结果。Dynamo 已结合引擎容量、缓存命中和 decode acceptance length，模型未就绪时保持容量。

延迟反馈是另一种设计。llm-d 使用 `max(P90_TTFT/S_TTFT, P90_TPOT/S_TPOT)`，S_TTFT、S_TPOT 为对应延迟目标；规则优先预测直方图、缺失则回退实测，以一分钟速率窗口计算 P90，压力截到 2 后做 45 秒平滑。引用指南覆盖单 variant／单 SLO 对，不是分离阶段估计。

## 实现

1. 按到达率与请求组合建立单副本服务曲线，保留 offered arrivals 和完成／拒绝结果。
2. 校准迭代成本及内存可执行性，或拟合 α、δ 并求解状态相关排队模型，获得平均占用与延迟。
3. 找到满足声明延迟统计和成功约束的最大请求率，不用均值替换分位数目标。
4. 用总需求除以合格容量并向上取整；P/D 分别计算，计入传输。
5. 通过已有决策边界返回 recommendation，由 controller 应用容量上下界、稳定窗口、pending 约束并执行。

Foretoken 当前 queue/token 策略不是该校准估计器，但 recommendation 与 controller 分离提供了职责边界。llm-d 反馈路径通过 dead band 与 pending-capacity credit 驱动 KEDA。

## 影响与效果

排队模型论文在 H100/vLLM、BF16 Llama-3.1-8B 和 Qwen2.5-14B 上评估，批量上限 256、token 预算 8192。每模型保留 112 个轻／中负载点，平均相对 ITL 误差分别为 4.6%／7.9%，TTFT 误差为 13.6%／15.8%；接近饱和的运行被排除。这是均值预测精度，不是服务加速或尾延迟精度。校准容量体现延迟合格服务能力，更及时响应可能消耗更多副本，而非加快推理本身。

## 参考

- [An Approximate Queueing Model](https://arxiv.org/html/2609.20957v1)
- [Dynamo planner README](https://github.com/ai-dynamo/dynamo/blob/b2078c3127bd2ad3a70a6d39beeb06ff2d0a67fd/components/src/dynamo/planner/README.md)
- [Dynamo 吞吐实现](https://github.com/ai-dynamo/dynamo/blob/b2078c3127bd2ad3a70a6d39beeb06ff2d0a67fd/components/src/dynamo/planner/core/throughput_scaling.py)
- [llm-d SLO 指南](https://github.com/llm-d/llm-d/blob/277b34d062ff954435cf8b1b05efbd75090cf788/guides/workload-autoscaling/slo-aware/README.md)
- [llm-d recording rules](https://github.com/llm-d/llm-d/blob/277b34d062ff954435cf8b1b05efbd75090cf788/guides/workload-autoscaling/slo-aware/prometheus-rule.yaml)
- [自动扩缩容实现](../../../../control-plane/internal/autoscaling/README_zh.md)
- [Foretoken token-load 决策](../../../../control-plane/internal/autoscaling/algorithm/decision/token_load.go)
- [测量与仿真](../measurement-simulation/)
