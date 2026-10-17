<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 减少启动开销：适配后端的图重放与流依赖

[English](backend-graph-replay.md) | 简体中文

## 现象与动机

短 decode 内核可能把大量时间花在重复主机提交上。图重放减少 launch，辅助 stream 重叠独立工作。CUDA、ROCm、MACA 接口相近，但 capture、communicator 顺序、stream 绑定与 workspace 生命周期不同。Warmup 成功不等于反复重放安全。

## 原理与分析

n 个 kernel 的重复启动成本约为 n·t_launch；capture 减少提交工作，不减少算术。真实 token 数 M 使用捕获大小 G 时增加 G−M 行 padding，并保留图 buffer。

解释性算例中，九个 token 重放十六 token 图会多算七行，其成本应与省下的启动比较。推测解码每请求有多个 query token，请求数并非完整形状 key。

主流产输入、辅助流算共享专家、主流合并时，读取前需 main→aux，消费前需 aux→main。分配须覆盖这些访问及后续重放。

## 设计

复用引擎既有 dispatcher，按 batch 描述、attention backend 与 capture 模式选择相容路径。vLLM 分开编译与 capture，支持 full／piecewise；`FULL_DECODE_ONLY` 排除 prefill，`FULL_AND_PIECEWISE` 对 uniform decode 用 full，其余用 piecewise，不支持的 key 走 eager。

图持有稳定输入和 workspace，辅助流保留显式依赖。各 rank 的 capture 参与与 collective 顺序一致，包含 eager／graph 混合路径。NCCL 文档的多 communicator 图 caveat 属于该库，不能视为 RCCL／MCCL 保证。

## 实现

分派与 stream 步骤如下：

1. 按实际 query token 数及 batch／backend 要求选受支持 bucket，为图实例分配持久输入、输出与 workspace。
2. Warmup 对应算子及实际 communicator 路径，再以一致 rank 参与捕获受支持操作，保留内部流事件依赖。
3. 新 batch 值复制到稳定输入 buffer，按模型要求初始化 padding 和 mask，再 replay；无相容 key 时由 dispatcher 走 eager 或 piecewise。
4. 主流记录 input-ready；辅助流等待、计算并记录 output-ready；主流等待后合并。所有访问流完成前不复用共享 workspace。
5. 图退役前保留 capture buffer，只消费真实 batch 输出。

AMD vLLM 指南按 attention backend 给出图模式选择。MetaX 使用插件与匹配原生库；引用版本 MACA all-gather 走基础 process-group，该实际路径而非直接 wrapper 决定 capture 相容性。

## 影响与效果

图重放减少重复 host launch，改善短步骤节奏；正确 stream 依赖让独立工作重叠且不读取过期结果。Padding 增加计算，图驻留内存减少容量，未覆盖的 eager 段仍可能主导。每个操作后全局同步会串行化本应保留的重叠。支持范围与持续进展由实际 backend／算子／communicator 组合决定。

## 参考

- [固定 commit 的 vLLM 图设计](https://github.com/vllm-project/vllm/blob/6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9/docs/design/cuda_graphs.md)。
- [NCCL 图执行契约](https://docs.nvidia.com/deeplearning/nccl/user-guide/docs/usage/cudagraph.html)。
- [AMD 图模式选择](https://rocm.docs.amd.com/projects/ai-ecosystem/en/latest/optimization/vllm-v1-optimization.html)。
- [MetaX communicator 实现](https://github.com/MetaX-MACA/vLLM-metax/blob/cec91763dbe968fe4478fc066f57ecf5877c3965/vllm_metax/distributed/device_communicators/cuda_communicator.py)。

 · [编译与 runtime](../compiler-runtime/) · [知识索引](../README_zh.md)
