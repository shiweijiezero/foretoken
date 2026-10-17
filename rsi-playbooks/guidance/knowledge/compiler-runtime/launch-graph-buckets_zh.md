<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 按实际负载分桶捕获图，减少启动开销

[English](launch-graph-buckets.md) | 简体中文

## 现象与动机

短 decode 内核之间的主机启动间隙会让 GPU 等待。CUDA Graph 可以减少提交次数，但服务批次的 token 数、请求几何、推测长度和 adapter 组合持续变化。逐组合捕获增加启动时间与内存，过粗的桶又会执行过多 padding 工作。

## 原理与分析

图重放使用捕获时的操作与地址，多个兼容批次可以补齐到同一捕获尺寸。实际 token 数为 b、捕获尺寸为 B 时，额外的 B−b 个位置虽不产生用户输出，仍可能参与计算。

成本应包括 `补齐后执行时间 + 分派／启动成本`。单看覆盖率不够：更多命中可能伴随更大 padding，图内存还会挤占 KV。决定取舍的是引擎实际 step 分布，而不只是客户端并发。

## 设计

vLLM v0.31.0 分别注册 FULL 与 PIECEWISE key。FULL 捕获完整兼容执行，PIECEWISE 捕获支持的区域，其余操作留在图外。Key 按需要包含 token 数、请求几何与 LoRA 状态；uniform 推测解码也可以每请求包含多个 query token。

PIECEWISE 放宽请求数和 uniformity，FULL 保留后端元数据所需的几何。捕获集合由 attention 后端、混合层和编译模式共同约束。不兼容操作需要切换执行路径，而不是单纯增加 token 桶。

## 实现

引擎在确定 attention 能力后，建立 token 数到捕获尺寸的映射并注册有效 key。运行时流程为：

```text
若图关闭或批次超过捕获上限：执行 eager 并返回
将 token 数补齐到对应桶
按请求几何和 adapter 状态生成 descriptor
若存在允许的 FULL key：重放 FULL
否则若存在允许的放宽 PIECEWISE key：重放相应片段
否则：执行 eager
```

Graph wrapper 消费选中的 key 与模式。捕获尺寸和后端兼容性由引擎管理；随意放宽请求数字段可能使已捕获的 attention 元数据失效。

## 影响与效果

重放用一次图提交代替多次主机内核提交，减少小计算 step 暴露的启动间隙。按负载选择桶，可在保留高频形状重放的同时减少补齐计算。

更多桶会增加捕获工作与常驻内存。如果挤占 KV，即使启动更快，也可能降低驻留序列数或增加抢占。完整重放还受参与操作限制，需要不兼容行为的批次继续走分段图或 eager。

## 参考

- [vLLM v0.31.0 CUDA Graph 设计](https://docs.vllm.ai/en/v0.31.0/design/cuda_graphs/)。
- [Dispatcher 实现](https://github.com/vllm-project/vllm/blob/db9527a46873454610df6dbedf79a36d6bf1a7f6/vllm/v1/cudagraph_dispatcher.py)：padding、注册 key、LoRA case 和选择顺序。
- [PyTorch v2.14 CUDA Graph Trees](https://github.com/pytorch/pytorch/blob/v2.14.0/docs/source/user_guide/torch_compiler/torch.compiler_cudagraph_trees.md)：具体形状捕获与内存生命周期。
