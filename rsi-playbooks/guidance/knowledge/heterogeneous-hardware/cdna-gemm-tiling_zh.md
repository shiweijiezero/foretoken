<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 按形状调整分块与布局，提高 CDNA 矩阵执行效率

[English](cdna-gemm-tiling.md) | 简体中文

## 现象与动机

为大 prefill 调优的 GEMM，在 decode 或低负载专家中可能浪费大多数行。AMD CDNA 需要按形状组织矩阵指令、wavefront 布局和片上共享内存（LDS）暂存，不应复制 CUDA GPU 的 launch 参数。MoE 总 batch 大小不等于每专家 GEMM 的 token 数。

## 原理与分析

X[M,K]W[K,N] 的工作量为 2MKN，最小逻辑流量是读一次 X/W、写一次 Y；tile 重读、scale 和 spill 增加流量。小 M 的权重复用有限，大 M 提高算术强度。大 tile 增加复用，同时占用寄存器／LDS 并可能降低驻留度。

AMD 规格中，CDNA3 gfx942 MI300X／MI325X 是 64 线程 wavefront、64 KiB LDS；CDNA4 gfx950 MI350X／MI355X 同为 64 线程、160 KiB LDS。指令布局随 dtype 和架构变化。解释性形状算例中，M=4 使用 M=128 tile 会浪费多数行；split-K 增加工作组，同时增加部分结果存储与归约。

## 设计

按架构、dtype、M/N/K 与量化布局选择受支持的库路径。AMD 指南提供 AITER linear／skinny GEMM、hipBLASLt、FP8 padding 和 CDNA4 FP4 assembly GEMM。原生 CDNA4 MXFP4 要求 MI350X／MI355X 与匹配权重；vLLM 的 gfx942 groupwise-int4 转换属于另一种表示。

操作数 tile 映射到目标 MFMA 指令的 lane，权重打包和 scale 布局为该消费者准备，不能各自独立调整。MoE 按专家分组 token，并根据每专家行数选择调度。

## 实现

分块 GEMM 的核心步骤：

```text
每工作组负责输出 tile (m0,n0)
初始化 FP32 或目标支持的累加寄存器
沿 K 按 BK 分块：
    从 HBM 带边界保护读取 X[BM,BK] 与 W[BK,BN]
    按指令相容布局暂存到 LDS/寄存器
    tile 复用前同步生产/消费访问
    按所需 block scale 执行 MFMA fragment 并累加
应用剩余行/通道 scale 及受支持的 bias/激活后处理
只存储有效 M,N 元素
```

寄存器／LDS 预算允许时，双缓冲使下块加载与当前计算重叠。Split-K 计算互不重叠的 K 区间，再用独立或受支持融合归约完成后处理。保留 dtype 累加和量化 scale 语义，包含 padding 行。AITER GEMM／MoE 算子及 tuning 配置拥有这些调度，`op_tests/test_gemm_a8w8.py` 和 `test_moe.py` 提供实际输入布局；BF16、FP16、FP8、MXFP4 分别选择路径。

## 影响与效果

形状适配分块减少 padding 计算，改善权重复用或可用工作组并行度；相容操作数与 scale 布局减少消费边界的转换流量。大 tile 也可能增加 spill 或降低驻留度，split-K 增加归约流量。CDNA4 更大 LDS 扩展暂存选择，实际分块仍决定寄存器压力和驻留度。微小 decode、大 prefill 与不均匀专家负载各有不同取舍。

## 参考

- [AMD 架构规格](https://rocm.docs.amd.com/en/latest/reference/gpu-arch-specs.html)。
- [当前 vLLM/AITER 优化指南](https://rocm.docs.amd.com/projects/ai-ecosystem/en/latest/optimization/vllm-v1-optimization.html)。
- [固定 commit 的 AITER](https://github.com/ROCm/aiter/tree/c09dc31787c21cd7fa7acbe4addef256766bd58a)。
- [vLLM MXFP4 布局与转换](https://github.com/vllm-project/vllm/blob/6849ca696ac9fbecec29a8a8c47f6ee18aea1ee9/vllm/model_executor/layers/quantization/mxfp4.py)。

 · [Kernel 设计](../kernel-design/) · [知识索引](../README_zh.md)
