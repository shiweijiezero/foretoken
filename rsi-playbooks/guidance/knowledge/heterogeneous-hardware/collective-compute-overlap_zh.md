<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 减少 Collective 延迟：后端算法选择与计算重叠

[English](collective-compute-overlap.md) | 简体中文

## 现象与动机

张量／专家并行可能把瓶颈从 GEMM 转到通信。孤立 all-reduce 最快的配置，可能因占用计算资源或等待迟到 rank 而拖慢完整阶段。NCCL、RCCL、MCCL 接口相近，但拓扑、内核、传输与图行为各不相同。

## 原理与分析

粗略模型为 `T=α·steps+bytes/β`。Ring all-reduce 每 rank 传输约 `2(p−1)/p` 倍 payload，步骤数随 p 增长。小 decode 消息偏向延迟瓶颈，大 prefill 偏向带宽。解释性大小算例：BF16 hidden width 4096 时，每 token 为 8192 byte，1024 token 为 8 MiB；实际并行布局会改变消息次数。

独立重叠可接近 `max(T_compute,T_comm)+T_unoverlapped`，共享 SM／HBM 则会拉长两者。另一 stream 本身不创造独立工作，专家负载不均还可能让最慢 rank 持续主导。

## 设计

Collective 由引擎 communicator owner 管理，保持每 communicator 的操作顺序。仅将不依赖通信结果的计算重叠，例如独立分支或受支持的下一 tile。消费者等待设备完成，不以 host enqueue 返回代替。

算法／channel 通过受支持 backend 按实际消息和拓扑选择。AMD 提供 Quick Reduce 与 ROCm all-gather／reduce-scatter 专家路径；低位归约还需量化／反量化及可接受误差。MetaX 插件选择 all-to-all manager，并让 MACA all-gather 走基础 process group。

## 实现

包含独立分支 B 的阶段：

```text
主流计算并打包 payload A
在主流记录 A-ready
通信流等待 A-ready
各 rank 按相同 communicator 顺序提交 collective
主流计算不依赖 collective 输出的 B
通信流记录 collective-complete
主流等待该事件后 combine(A_result, B)
最后消费者完成后才复用 payload/workspace
```

EP 在 dispatch → 专家 GEMM → combine 中保留 token 路由顺序、专家计数、padding 与反向路由。仅通过受支持 owner 分块流水，确保所有 rank（包括无本地 token 的 rank）保持相同 collective 序列。低位路径先打包量化值和 scale，传输后重建受支持归约结果。CUDA 使用 NCCL，ROCm 使用 RCCL，MACA 使用插件的 MCCL／process-group 路径，不能互换图配置。当前 RCCL 源码位于 `rocm-systems/projects/rccl`。

## 影响与效果

重叠消除独立工作间的串行等待，相容打包减少布局 copy，按消息选 backend 路径分别处理延迟或带宽瓶颈。通信拖慢 GEMM、迟到 rank 主导、或打包／量化占主导时，收益可能消失。低位传输减少字节但改变数值行为，通信带宽提高不自动缩短完整服务阶段。

## 参考

- [当前 ROCm systems 源码](https://github.com/ROCm/rocm-systems/tree/f1b316f12c75430787032e99405834f59a026ae1/projects/rccl)。
- [AMD 通信与 Quick Reduce 指南](https://rocm.docs.amd.com/projects/ai-ecosystem/en/latest/optimization/vllm-v1-optimization.html)。
- [MetaX communicator 与 all-to-all 选择](https://github.com/MetaX-MACA/vLLM-metax/blob/cec91763dbe968fe4478fc066f57ecf5877c3965/vllm_metax/distributed/device_communicators/cuda_communicator.py)。
- [NCCL 图与 rank 参与契约](https://docs.nvidia.com/deeplearning/nccl/user-guide/docs/usage/cudagraph.html)。

 · [并行与通信](../parallelism-communication/) · [知识索引](../README_zh.md)
