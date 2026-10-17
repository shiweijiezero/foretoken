<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 通过微批调度平衡流水线并行的延迟与吞吐

[English](pipeline-microbatch-balance.md) | 简体中文

## 现象与动机

流水线并行 PP 按层切分，而不是切分每个权重矩阵。它可以跨节点容纳模型，让频繁 TP 通信留在节点内。但 collective 次数少，不保证单请求更快：请求仍需经过全部阶段，下一 token 还依赖上一 token 的采样反馈。

## 原理与分析

独立请求或合法 microbatch 可同时占据不同阶段。`P` 个等时阶段、`M` 个独立前向 microbatch、每阶段时间 `t`，理想仅前向流水线时间为 `(M+P-1)*t`，整体阶段利用率为 `M/(M+P-1)`。公式忽略传输、不均衡与服务调度，既不是训练 1F1B 公式，也不是 vLLM 当前执行顺序的性能承诺。

`P=4,M=1` 时，一个 microbatch 顺序经过四阶段；八个独立 microbatch 理想需要十一段阶段时间。这说明填充和排空气泡，不预测持续到达流量下的延迟。增加在途工作能改善吞吐，也可能增加单请求等待。

## 设计

将频繁 TP collective 留在高速域，将连续层范围分配给 PP 阶段。各阶段拥有自身权重与 attention 层 KV，消息包含模型实际中间张量，可能同时包括 residual stream 与 hidden state。

平衡的是阶段执行而非层数：embedding、输出投影、随上下文变化的 attention 与 MoE 倾斜让阶段耗时不同。独立请求可以占用不同阶段，同序列 chunk 与 decode token 则仍有缓存、采样依赖。使用引擎支持的不均匀切分，不随意移动模块。

## 实现

仅前向推理流水的过程如下：

1. 第零阶段接纳已就绪请求／microbatch，执行本地层。
2. 向下一阶段发送准确的中间张量集合；下一阶段等待就绪，执行本地层并继续转发。
3. 每次交接保留独立 microbatch 的身份、位置与缓存映射。
4. 最后阶段计算 logits／采样，将下一 token 与必要调度状态返回后，才接纳该序列下一次 decode。
5. 发送与消费者完成后才复用激活槽位；取消需协调所有持有该请求的阶段。

vLLM 已实现 PP、TP/PP 组合和支持的不均匀层切分。DeepEP 实验性 `PPBuffer` 是搬运原语，要求 RDMA-only 组、连续 CUDA 张量、32 字节指针／大小对齐，以及预先配置最大字节数和在途槽位，不替代服务调度。

## 影响与效果

PP 扩大跨节点模型容量，用激活交接替代部分跨节点 TP 同步。并发足够时，独立工作填充阶段、提高整体利用率；低并发请求仍须遍历全部阶段并等待自回归反馈，collective 少不保证 TPOT 更低。

更多在途 microbatch 以单请求排队与缓冲容量换取阶段占用。不均匀切分可降低最慢阶段瓶颈，更深流水则增加填充／排空气泡与取消／故障协调范围。理想利用率公式解释这些依赖，不是实测服务吞吐。

## 参考

- [6849ca69 的并行指南](https://github.com/vllm-project/vllm/blob/6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9/docs/serving/parallelism_scaling.md)
- [93eb6eb2 README](https://github.com/deepseek-ai/DeepEP/tree/93eb6eb238127e96c6d7a4a625a6dad158348509)
- [TP Collective 选择](tp-collective-selection_zh.md)
