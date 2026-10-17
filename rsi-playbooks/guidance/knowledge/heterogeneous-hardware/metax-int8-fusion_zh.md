<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 通过兼容布局的量化阶段融合减少 MetaX INT8 中间访存

[English](metax-int8-fusion.md) | 简体中文

## 现象与动机

MetaX MACA 的 dense INT8 路径可能包括输入布局 copy、量化、scaled GEMM 与输出处理。MoE 还增加专家布局、激活、再量化与 combine。兼容阶段间反复物化中间量增加流量；CUDA 风格接口不代表原生布局或 workspace 语义相同。

## 原理与分析

对称按 token 输入 scale 与按通道权重 scale 可写为：

```text
Y[m,n] ≈ sx[m] sw[n] Σk qx[m,k] qw[n,k] + bias[n]
```

动态输入量化先对每行沿 K 归约。分开的归一化和量化会先写 BF16，再读回并写 INT8／scale；相容融合 producer 可省中间流量，但两次归约和消费者布局仍须保留。

归一化维度跨 TP shard 时，平方和需要跨 shard 归约，不能因局部融合省略。MoE 路由行和专家 padding 改变 scale 位置，dense [M,K] 融合不能直接定义专家布局。

## 设计

Scaled GEMM 继续由插件选中的内核持有，在归一化与量化等相容 producer 边界融合，不替换原生 dispatch。vLLM-metax 的 `MctlassScaledMMLinearKernel` 取得权重／scale，将输入 contiguous、量化并调用 scaled-MM wrapper；静态 scale 由层提供，非对称输入使用 zero-point-aware 路径。

Compressed-tensors INT8 MoE 要求按通道权重和动态按 token 输入。Modular kernel 接收路由与共享专家数据，oracle 在支持范围内选择 Triton、batched Triton 或 batched DeepGEMM；专家布局由选中路径持有。

## 实现

对称 RMSNorm 与量化融合的核心计算：

```text
对每个 token 行 x：
    sumsq = sum(x[k]**2)
    归一化维度分片时，对 sumsq 做 all-reduce
    u[k] = x[k] * rsqrt(sumsq/global_K + epsilon) * gamma[k]
    u 转为原归一化输出 dtype
    amax = 消费者输入维度上的 max(abs(u[k]))
    一个 scale 跨 K shard 时，对 amax 做 MAX all-reduce
    sx = amax / 127
    sx == 0 时 qx[k]=0，否则 clamp(round(u[k]/sx), -127, 127)
    按 scaled-GEMM 消费布局写 qx 与 sx
scaled GEMM 累加 qx × qw，应用 sx × sw 和 bias
```

这是对称场景的融合算法，不表示插件已融合 RMSNorm；舍入和 scale 约定应匹配原量化器。MoE 将行及 scale 一起路由，执行 gate／up GEMM、SiLU-and-multiply，将激活再量化供 down GEMM 使用，再按选中 modular 路径应用路由权重和 combine。保留有效行 mask、反向路由及 stream／workspace 生命周期。使用匹配的 vLLM／插件／MACA SDK／算子库发行组合，以及满足 scale 粒度要求的 INT8 权重。

## 影响与效果

相容 producer 融合可省归一化中间量的一写一读及可能的一次 launch，但仍保留必要行归约与 INT8 输出。消费者接受原 stride／布局时，省冗余 contiguous copy 才真正减少流量。路由、再量化和 padding 仍可能主导，融合也可能损失并行度。
## 参考

- [MetaX dense INT8 注册与执行](https://github.com/MetaX-MACA/vLLM-metax/blob/cec91763dbe968fe4478fc066f57ecf5877c3965/vllm_metax/registry/linear_kernels/int8.py)。
- [当前 compressed-tensors INT8 MoE](https://github.com/MetaX-MACA/vLLM-metax/blob/cec91763dbe968fe4478fc066f57ecf5877c3965/vllm_metax/registry/quant_config/compressed_tensors_moe/compressed_tensors_moe_w8a8_int8.py)。
- [当前 INT8 MoE oracle](https://github.com/MetaX-MACA/vLLM-metax/blob/cec91763dbe968fe4478fc066f57ecf5877c3965/vllm_metax/model_executor/layers/fused_moe/oracle/int8.py)与[发行兼容说明](https://github.com/MetaX-MACA/vLLM-metax)。

 · [量化](../quantization/) · [知识索引](../README_zh.md)
