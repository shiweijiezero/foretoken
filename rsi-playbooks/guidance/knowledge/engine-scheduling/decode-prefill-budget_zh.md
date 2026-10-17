<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 通过 Token 预算控制预填充分块，减少解码停顿

[English](decode-prefill-budget.md) | 简体中文

## 现象与动机

长输入与正在生成的请求共用 GPU 时，一次 prefill 可能让已有流长时间收不到 token。切小输入能缩短每次打断，但固定小块也可能降低计算效率、重复调度与访存。目标是在下一 token 的交付预算内，尽量多推进 prefill。

## 原理与分析

访存受限的 decode 与计算密集的 prefill 可以组成混合批次。Sarathi-Serve 用有界 prefill 工作量限制单轮耗时，约束为：

`iteration_time(decode_population, context_lengths, prefill_tokens) <= delivery_budget`。

交付预算是预留主机调度与响应发送后，活跃流还能等待的时间。相同 token 数的耗时会随上下文、MoE 专家激活、collective 和推测验证而变化。GPU tile 大小也会影响效率，预算稍小反而可能选中更差的矩阵形状。

例如 64 个请求各做一次普通单 token decode，512-token 预算还剩 448 个 prefill token，8,192-token 输入至少需 19 轮；2,048-token 预算还剩 1,984，至少需五轮。这是计量示例，不是延迟实测。

## 设计

先为已有 decode 留出工作量，再用剩余 token 和时间容量处理未命中的输入后缀。大块有利于 prefill 效率和 TTFT，小块保留更频繁的 decode 机会。Sarathi 按尾部 token 间隔确定固定预算；SOLA 则按 TTFT/TPOT 压力调整工作量与执行顺序，用实际迭代时间修正拟合成本。

引擎负责分块进度、token 选择、KV 分配与 graph 执行。请求层选实例、计量请求；集群编排提供容量，不生成逐轮 schedule。

## 实现

1. 读取运行请求、剩余输入、token/input 上限和实际可用 KV 容量。
2. 按引擎真实口径安排已有 decode，包括 draft slot 与验证 token。
3. 根据剩余容量及延迟曲面选择 prefill 块；曲面绑定模型、权重／缓存 dtype、backend 和并行配置。
4. 分配 KV slot，执行混合批次，只推进真正执行工作的已计算进度。
5. 自适应调度再用实际迭代耗时更新成本估计，将新的 SLO 压力用于下一轮。

vLLM V1 已有 chunked prefill 和先运行、后等待的调度，还包含编码器预算与混合状态对齐，选块时保留这些约束。

## 影响与效果

分块减少长 prefill 对设备的连续占用，并利用 decode 之外的计算余量；代价是在首 token 完成时间与单次生成间隔之间取舍。更小的块增加轮次、attention 读取和 collective，MoE 还可能反复读取专家权重，[层组 prefill](moe-layered-prefill_zh.md)针对这一开销。纯 decode 没有 prefill 干扰可消除，高并发 decode 也可能用尽交付预算。

## 参考

- [Sarathi-Serve，arXiv 2403.02310v1](https://arxiv.org/html/2403.02310v1)：混合批次、预算测量与 tile quantization。
- [SOLA，MLSys 2025，§§4–5](https://proceedings.mlsys.org/paper_files/paper/2025/file/bc82dbfbfa43232be85b8d9838f49c3e-Paper-Conference.pdf)：逐轮工作量控制和在线成本修正；论文使用 vLLM 0.4.2 集成。
- [vLLM 官方优化指南](https://docs.vllm.ai/en/latest/configuration/optimization/)：当前分块与抢占建议。
- [vLLM V1 scheduler，commit 6849ca69](https://github.com/vllm-project/vllm/blob/6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9/vllm/v1/core/sched/scheduler.py)：`schedule`、token/input 预算与分配约束。
