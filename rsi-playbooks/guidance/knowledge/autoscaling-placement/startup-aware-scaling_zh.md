<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 提前准备所需容量：按启动延迟预测 token 需求

[English](startup-aware-scaling.md) | 简体中文

## 现象与动机

请求扩容后，容量可能几分钟后才可用。采集、平滑、调度、镜像／权重加载、graph 预热和路由发布都增加延迟。启动需 180 秒而只预测下一个 15 秒采样时，无论模型多复杂都可能晚到；应预测容量真正就绪时的需求。

## 原理与分析

对每 T 时间采样一次、单位为 token/s 的需求 x，先平滑 `x̂_t = α*x_t + (1−α)*x̂_(t−T)`。分解论文按启动跨度 Δ 外推：

`x̂_(t+Δ) = x̂_t + slope*(Δ−T/2)`，其中 `slope = (x̂_t − x̂_(t−T))/T`。

Δ、T 使用相同时间单位，α 无量纲；半间隔修正对应两个观测点之间的斜率中心。再加 `β*min(σ, γ*abs(x̂_(t+Δ)))`：σ 为预测误差标准差，β 控制余量，γ 限制余量相对需求的比例。Prefill、decode 分别计算。

预测能提前响应可见爬坡，不能预知无预告跳变；暖容量下界或准入策略承担这种不确定性，也产生真实成本。Checkpoint 存储、CPU 反序列化、量化和 graph 会独立改变启动，不等于稳态吞吐变化。

## 设计

分别跟踪 requested、starting、ready、routable 与 draining。两个可路由副本加两个启动副本，当前服务能力为两份、已承诺容量为四份。Starting 抵扣重复创建，不抵扣当前排队；失败／不可调度与正常预热不同。

Controller 准备生命周期观测并负责执行，预测通过算法边界返回 recommendation。Foretoken 保持正在转换的目标、在当前 controller 进程保存稳定历史，但公开策略尚未实现需求预测。

## 实现

1. 收集新鲜需求与生命周期观测，包括对应配置从启动到可路由的延迟。
2. 分别平滑各阶段工作量、计算趋势，并外推到实测就绪跨度。
3. 加入有界预测余量，用投影工作量除以校准阶段容量，得到副本目标。
4. 计入 pending 承诺而不把它算作即时吞吐，将失败启动与正常就绪预期区分。
5. 返回目标，由 controller 应用上下界、稳定规则并创建；后续观测归并真实就绪状态与预测误差。

Dynamo 提供 constant／ARIMA／Prophet／Kalman 的 `predict_next` 与 pending-start 处理，单步接口本身不保证覆盖启动跨度。引用 AIBrix predictive 字段已接受／验证，但 controller 未消费，实际执行仍是反馈式。

## 影响与效果

论文的受控硬件对比使用 Qwen2.5-7B-Instruct、vLLM 0.11、每副本一张 A100 40GB，五分钟 5→15→5 QPS 轨迹，256–4096-token Pareto 输入，最大输出 256。注入 60 秒启动延迟、TTFT 目标 2 秒时，反馈 KEDA 报告TTFT 超标率 63.5%、P95 TTFT 24.0 秒；EWMA 启动跨度预测加有界余量为 3.7%、1.6 秒。TTFT 超标率分母为完成请求。

该硬件对比预测运行加等待请求数，不是分别预测 token 流，验证的是提前供应容量的效果。精度未指定，结果来自一条受控轨迹，不是五 seed 模拟。提前扩容增加承诺容量，并非 kernel 加速。

## 参考

- [Decomposing Predictive Kubernetes Autoscaling](https://arxiv.org/html/2609.20874v1)
- [Dynamo 预测器](https://github.com/ai-dynamo/dynamo/blob/b2078c3127bd2ad3a70a6d39beeb06ff2d0a67fd/components/src/dynamo/planner/core/load/predictors.py)
- [Dynamo 吞吐 scaler](https://github.com/ai-dynamo/dynamo/blob/b2078c3127bd2ad3a70a6d39beeb06ff2d0a67fd/components/src/dynamo/planner/core/throughput_scaling.py)
- [AIBrix 预测文档](https://github.com/vllm-project/aibrix/blob/d0500b0399cf35ad0bf95d073faa83c23308f463/docs/source/features/scaling-performance/autoscaling/predictive-autoscaling.rst)
- [AIBrix autoscaler 代码](https://github.com/vllm-project/aibrix/blob/d0500b0399cf35ad0bf95d073faa83c23308f463/pkg/controller/podautoscaler/autoscaler.go)
- [Foretoken 扩缩容架构](../../../../control-plane/internal/autoscaling/README_zh.md)
- [Foretoken 公开策略](../../../../docs/autoscaling_zh.md)
- [模型加载与存储](../model-loading-storage/)
