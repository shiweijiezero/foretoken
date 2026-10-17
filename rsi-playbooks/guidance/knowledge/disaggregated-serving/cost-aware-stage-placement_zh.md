<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 降低服务成本：基于实测容量的异构阶段放置

[English](cost-aware-stage-placement.md) | 简体中文

## 现象与动机

峰值 FLOPs 最高的加速器不一定最适合每个推理阶段。Prefill、decode attention 和 MoE 专家的算力、带宽、驻留需求不同。阶段放置利用差异，但要保留受支持的执行与状态传输路径，不能逐算子独立选最低价格。

## 原理与分析

先区分可行性与性能：阶段／设备／dtype／并行组合必须能装载权重、保存状态、执行算子，并通过支持的 connector 交换状态。对可行组合，再测阶段延迟预算内的容量。

基础目标为 `min Σ_s,h n_s,h*c_h`，使用整数 worker 数，满足阶段容量、显存、传输和端到端 SLO；成本包括主机与网络份额。一个 P 处理 8 请求/s、两个 D 各处理 4，则完整路径为 8。把 P 换成便宜但只有 5 请求/s 的设备，系统上限降至 5，D 不变也无效。D 显存变小则可能先减少驻留并违反 TPOT。

## 设计

使用负载特定的容量曲线，不用峰值规格代替。联合选择硬件、副本数和工作切片，计入权重副本、转换、中转、拓扑及启动。AFD attention 受益于 HBM 和 KV 容量，专家充分组批后利用算力；P/D 也可分配不同设备，但 KV 交接必须可承受。

MegaScale-Infer 将 attention 放 H20、专家放 L40S。AIBrix optimizer 将负载映射到吞吐 bin，Melange 用整数副本与分配约束最小化成本。Dynamo 分别估计 P/D 容量；llm-d Kueue 借用管理 quota，不是阶段执行模型或 KV 迁移。

## 实现

基于容量曲线的分配搜索为：

```text
枚举支持的阶段/设备/dtype/并行组合
读取SLO容量曲线和物理内存需求
在设备预算内构造整数副本方案
将负载切片分给各阶段容量足够的方案
检查链路字节需求、拓扑和状态交接兼容性
计入权重、workspace、中转、主机及网络成本
排除违反端到端延迟或内存约束的分配
选成本最低的完整可行请求路径
建立各角色，模型就绪后才路由工作
```

这是阶段优化算法摘要，不是现成的全 backend 开关。Foretoken E/P/D 示例允许逐阶段资源和 engineArgs。BF16 MegaScale-Infer 与 FP8 FastAFD 工作点不同；跨厂商需要真实 kernel 和 connector 支持，不只匹配 tensor 维度。

## 影响与效果

MegaScale-Infer 的 H20-attention／L40S-expert 部署，按采购成本归一化的 decode 吞吐最高比 H20 vLLM 提高 3.24 倍、比 H20 TensorRT-LLM 提高 1.86 倍；包含 prefill 后，吞吐／成本最高提高 1.66 倍。研究用 BF16 权重、activation 和 KV，覆盖 Mixtral-8×22B、DBRX、scaled MoE，逐 token 目标 150 ms；生产来源流量的 prompt／output 中位数为 571／159 token。归一化采购价格属于历史条件，不是当前云租价。

这些是阶段放置与 AFD 执行共同产生的系统收益。FastAFD 实测为 GB200 NVL72 的 FP8 MoE，未来 Rubin／LPX 数字属于预测。更多 TP rank、专家小批、传输、功率限制与启动都可能抵消标价优势。

## 参考

- [MegaScale-Infer](https://arxiv.org/html/2504.02263)
- [FastAFD](https://haoailab.com/blogs/fastafd/)
- [optimizer](https://github.com/vllm-project/aibrix/blob/d0500b0399cf35ad0bf95d073faa83c23308f463/python/aibrix/aibrix/gpu_optimizer/optimizer/optimizer.py)
- [Melange solver](https://github.com/vllm-project/aibrix/blob/d0500b0399cf35ad0bf95d073faa83c23308f463/python/aibrix/aibrix/gpu_optimizer/optimizer/solver/melange/solver.py)
- [planner](https://github.com/ai-dynamo/dynamo/blob/b2078c3127bd2ad3a70a6d39beeb06ff2d0a67fd/components/src/dynamo/planner/core/throughput_scaling.py)
- [Kueue 重平衡指南](https://github.com/llm-d/llm-d/blob/277b34d062ff954435cf8b1b05efbd75090cf788/guides/workload-autoscaling/kueue-rebalancing/README.md)
- [E/P/D 示例](../../../../examples/feature/encoder-prefill-decode/README_zh.md)
- [异构硬件](../heterogeneous-hardware/)
