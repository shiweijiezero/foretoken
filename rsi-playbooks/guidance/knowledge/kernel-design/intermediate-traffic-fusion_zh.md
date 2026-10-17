<!--
SPDX-License-Identifier: Apache-2.0
SPDX-FileCopyrightText: Copyright contributors to the Foretoken project
-->

# 按上下游布局融合算子，减少中间访存

[English](intermediate-traffic-fusion.md) | 简体中文

## 现象与动机

归一化、激活和布局转换单独很短，连续执行却产生多次启动、分配和全张量遍历。瓶颈往往是中间表示跨越算子边界，而非单个算子的算术。

## 原理与分析

一个张量有 N 个元素、每个 s 字节，省去独立写回与随后读取，逻辑流量约减少 `2Ns` 字节。实际 HBM 节省取决于缓存驻留。

融合还需匹配生产者的 lane 布局与消费者读取方式。矩阵累加器按矩阵指令安排，CUTLASS epilogue 会先交换和转换数据，再合并写回。融合更多工作会延长存活区间，可能增加寄存器、共享内存和同步。

## 设计

vLLM v0.31.0 在 packed QKV 上融合逐 head Q/K RMSNorm 与 RoPE，V 保持不变。图匹配 pass 只在 dtype、head 几何、epsilon 和旋转布局符合条件时替换。

替代路径使用 `auto_functionalized`，把原生算子的 mutation 表达为图中返回的更新张量。是否能复用物理存储，还取决于其他消费者和别名关系。

## 实现

被匹配的计算序列为：

```text
将 packed QKV 拆成 Q、K、V 视图
把 Q 和 K 按 head 重排
对每个 Q/K head：
    normalized = x * reciprocal_sqrt(mean(x*x) + epsilon) * weight
    按位置与 RoPE 约定旋转 normalized
返回变换后的 Q、K 和保持不变的 V
```

编译器将该序列替换成融合的 packed-QKV 调用，再拆分其返回值。引用版本支持 FP16/BF16、64/128/256 的 head dimension、`1e-5` 或 `1e-6` 的 epsilon，以及 NeoX 或交错旋转布局。Pass 统一 view/reshape 的图表示，让等价计算能够匹配。

若下游接受相应布局，直接写入消费者所需格式可进一步省去转换；仍需原值的其他消费者必须继续取得原值。

## 影响与效果

值在融合执行内部继续使用，可减少中间张量流量和启动边界。当这些开销主导完整层时，融合最有价值。

额外寄存器压力或效率较低的 GEMM 也可能超过节省。Decode 与 prefill 的形状和启动成本比例不同，适合的融合边界可以不同。现有编译器是否已经融合该序列，决定替代实现还能省下多少工作。

## 参考

- [vLLM QK norm/RoPE 融合](https://github.com/vllm-project/vllm/blob/db9527a46873454610df6dbedf79a36d6bf1a7f6/vllm/compilation/passes/fusion/qk_norm_rope_fusion.py)：匹配模式、支持形状与替代执行。
- [vLLM 融合 pass](https://github.com/vllm-project/vllm/tree/db9527a46873454610df6dbedf79a36d6bf1a7f6/vllm/compilation/passes/fusion)。
- [CUTLASS epilogue 与访存结构](https://github.com/NVIDIA/cutlass/blob/main/media/docs/cpp/efficient_gemm.md)。
- [PyTorch 自定义算子集成](https://docs.pytorch.org/tutorials/advanced/cpp_custom_ops.html)：元数据、mutation 和 alias 契约。
